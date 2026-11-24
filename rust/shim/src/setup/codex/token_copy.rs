//! Whether Codex's registration already uses Codex's own copy of the token a
//! token file holds (`installer codex-token-copy`, issue #1908).
//!
//! A client-only install with `--token-file X` registers Codex's private copy
//! of X's token (`token_copy_path`). `connect --token-file X` selects X itself
//! (`plan_replace`), so the installers' rerun would re-point a registration
//! whose inputs did not change. The installers treat a `connect` plan whose
//! only changes are Codex's rows (`installer connect-report codex-only`) as
//! current only when this answers `same`: `connect`'s Codex rows would change
//! nothing but that one pointer, in the registration and in the hooks'
//! connection file, and the copy holds the same token as X. Every other answer
//! applies the plan, so a rotated token, another file, a new daemon URL or a
//! missing setting is handled exactly as before. Nothing here writes, and no
//! token is ever reported.
use super::*;

/// The installers skip Codex's `connect` on this state only.
pub const SAME: &str = "same";
/// The copy holds another token than the file given: a rotation.
pub const ROTATED: &str = "rotated";
/// The registration names another token file, or none.
pub const NOT_COPY: &str = "not-copy";
/// `connect` would also change another connection setting.
pub const OTHER_CHANGES: &str = "other-changes";
/// Codex's user configuration has no Pseudolife server.
pub const UNREGISTERED: &str = "unregistered";
/// Something could not be read or verified.
pub const UNKNOWN: &str = "unknown";

fn answer(state: &str, detail: impl Into<String>) -> Value {
    json!({"state": state, "detail": detail.into()})
}

/// The answer for Codex's `config/read` result `config` (with layers), the
/// Codex home `home`, the daemon URL and the token file the installer was
/// given: `{"state": ..., "detail": ...}`, the detail saying why when the
/// state is not [`SAME`]. Reads the two token files through the private-file
/// boundary and compares them in constant time.
pub fn token_copy_state(config: &Value, home: &Path, url: &str, token_file: &Path) -> Value {
    decide(config, home, url, token_file).unwrap_or_else(|error| answer(UNKNOWN, error.to_string()))
}

fn decide(config: &Value, home: &Path, url: &str, token_file: &Path) -> Result<Value, SetupError> {
    // connect validates the URL with its own parser; the two agree on plain
    // ASCII origins, so anything else is left to connect.
    if !url.is_ascii() {
        return Ok(answer(
            UNKNOWN,
            "the daemon URL is not plain ASCII; connect decides it",
        ));
    }
    let home = config_path(home)?;
    let user = user_config(config, &home)?;
    let Some(user_server) = server(&user.config).filter(|s| !s.is_empty()) else {
        return Ok(answer(
            UNREGISTERED,
            "Codex's user configuration has no pseudolife-memory server",
        ));
    };
    let before = environment(user_server)?;
    let copy = json!(token_copy_path(&home));
    let registered = before.get(FILE_KEY);
    if !registered.is_some_and(Value::is_string)
        || !same_env_value(FILE_KEY, registered, Some(&copy))
    {
        return Ok(answer(
            NOT_COPY,
            format!("the registration's {FILE_KEY} is not Codex's own token copy"),
        ));
    }
    // What connect plans, local or remote: anything but the token file
    // pointer is a change connect must still make.
    let mut planned_url = String::new();
    for no_spawn in [false, true] {
        let plan = plan_replace(config, &home, url, Some(token_file), no_spawn)?;
        let mut after = plan.env().clone();
        let mut previous = plan.previous_env().clone();
        after.remove(FILE_KEY);
        previous.remove(FILE_KEY);
        if after != previous {
            let mut keys: Vec<&String> = after.keys().chain(previous.keys()).collect();
            keys.sort();
            keys.dedup();
            keys.retain(|key| after.get(*key) != previous.get(*key));
            let keys: Vec<&str> = keys.into_iter().map(String::as_str).collect();
            return Ok(answer(
                OTHER_CHANGES,
                format!("connect would also change {}", keys.join(", ")),
            ));
        }
        planned_url = plan.url.clone();
    }
    // The hooks' connection file is connect's second Codex row.
    match read_connection(&connection_path(&home))? {
        Some((recorded_url, recorded_file))
            if recorded_url == planned_url
                && same_env_value(FILE_KEY, Some(&json!(recorded_file)), Some(&copy)) => {}
        _ => {
            return Ok(answer(
                OTHER_CHANGES,
                "connect would also rewrite the hooks' connection file",
            ));
        }
    }
    let read = |path: PathBuf| {
        CredentialProvider::new(None, Some(path))
            .and_then(|provider| provider.snapshot())
            .ok()
            .and_then(|snapshot| snapshot.token().map(str::to_owned))
    };
    let given = crate::credentials::absolute_expanded(token_file)
        .ok()
        .and_then(read);
    let Some(given) = given else {
        return Ok(answer(
            UNKNOWN,
            "the token file given cannot be read as a token file",
        ));
    };
    let Some(held) = read(token_copy_path(&home)) else {
        return Ok(answer(UNKNOWN, "Codex's token copy cannot be read"));
    };
    if crate::board::identity::constant_time_equal(given.as_bytes(), held.as_bytes()) {
        Ok(answer(
            SAME,
            "the registration names Codex's own token copy, which holds the same token",
        ))
    } else {
        Ok(answer(
            ROTATED,
            "Codex's token copy holds another token than the file given",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const URL: &str = "https://daemon.example.com";
    const TOKEN: &str = "fixture-installer-bearer";
    const OTHER: &str = "fixture-rotated-bearer";

    /// A Codex home after a client-only install: Codex's own token copy, the
    /// hooks' connection file, and the operator's token file beside them.
    struct Home(PathBuf);

    impl Home {
        fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("codex-token-copy-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&path).unwrap();
            let home = Self(resolved_path(&path).unwrap());
            crate::board::state::private_dir(&home.0.join("pseudolife")).unwrap();
            home.write(&home.copy(), TOKEN);
            home.connection(URL, &home.copy());
            home.write(&home.operator(), TOKEN);
            home
        }

        fn copy(&self) -> String {
            token_copy_path(&self.0).to_string_lossy().into_owned()
        }

        fn operator(&self) -> String {
            self.0.join("operator.token").to_string_lossy().into_owned()
        }

        fn write(&self, path: &str, token: &str) {
            let _ = std::fs::remove_file(path);
            crate::board::state::atomic_create(Path::new(path), format!("{token}\n").as_bytes())
                .unwrap();
        }

        fn connection(&self, url: &str, file: &str) {
            let path = connection_path(&self.0);
            let _ = std::fs::remove_file(&path);
            crate::board::state::atomic_create(&path, &connection_bytes(url, file)).unwrap();
        }

        /// Codex's `config/read` answer with `env` registered in the user layer.
        fn config(&self, env: Value) -> Value {
            let server = json!({"command": "/opt/pseudolife-mcp", "args": [], "env": env});
            json!({
                "config": {"mcp_servers": {SERVER: server}},
                "layers": [{"name": {"type": "user", "file": self.0.join("config.toml")},
                            "version": "fixture-version", "config": {"mcp_servers": {SERVER: server}}}]
            })
        }

        fn registered(&self) -> Value {
            json!({
                "PSEUDOLIFE_WRITER_ID": "codex", "PSEUDOLIFE_MCP_NO_SPAWN": "1",
                URL_KEY: URL, FILE_KEY: self.copy(),
            })
        }

        fn state(&self, config: &Value, url: &str) -> Value {
            let answer = token_copy_state(config, &self.0, url, Path::new(&self.operator()));
            for token in [TOKEN, OTHER] {
                assert!(!answer.to_string().contains(token), "{answer}");
            }
            answer
        }
    }

    impl Drop for Home {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn with(mut env: Value, key: &str, value: Option<&str>) -> Value {
        match value {
            Some(value) => env[key] = json!(value),
            None => {
                env.as_object_mut().unwrap().remove(key);
            }
        }
        env
    }

    #[test]
    fn the_copy_holding_the_given_files_token_is_the_same() {
        let home = Home::new();
        let answer = home.state(&home.config(home.registered()), URL);
        assert_eq!(answer["state"], SAME, "{answer}");
        // A trailing slash is the same daemon, as connect spells it.
        let answer = home.state(&home.config(home.registered()), &format!("{URL}/"));
        assert_eq!(answer["state"], SAME, "{answer}");
    }

    #[test]
    fn a_copy_holding_another_token_is_a_rotation() {
        let home = Home::new();
        home.write(&home.operator(), OTHER);
        let answer = home.state(&home.config(home.registered()), URL);
        assert_eq!(answer["state"], ROTATED, "{answer}");
    }

    #[test]
    fn a_registration_naming_another_file_is_not_the_copy() {
        let home = Home::new();
        let other = home.0.join("other.token").to_string_lossy().into_owned();
        home.write(&other, TOKEN);
        for file in [Some(other.as_str()), Some(home.operator().as_str()), None] {
            let env = with(home.registered(), FILE_KEY, file);
            let answer = home.state(&home.config(env), URL);
            assert_eq!(answer["state"], NOT_COPY, "{file:?}: {answer}");
        }
    }

    #[test]
    fn any_other_change_connect_would_make_keeps_connect() {
        let home = Home::new();
        let cases = [
            with(
                home.registered(),
                URL_KEY,
                Some("https://elsewhere.example.com"),
            ),
            with(home.registered(), NO_SPAWN_KEY, None),
            with(home.registered(), TOKEN_KEY, Some(TOKEN)),
        ];
        for env in cases {
            let answer = home.state(&home.config(env.clone()), URL);
            assert_eq!(answer["state"], OTHER_CHANGES, "{env}: {answer}");
        }
        // A different daemon given to the rerun, too.
        let answer = home.state(
            &home.config(home.registered()),
            "https://elsewhere.example.com",
        );
        assert_eq!(answer["state"], OTHER_CHANGES, "{answer}");
    }

    #[test]
    fn a_connection_file_connect_would_rewrite_keeps_connect() {
        let home = Home::new();
        let config = home.config(home.registered());
        home.connection("https://elsewhere.example.com", &home.copy());
        assert_eq!(home.state(&config, URL)["state"], OTHER_CHANGES);
        home.connection(URL, &home.operator());
        assert_eq!(home.state(&config, URL)["state"], OTHER_CHANGES);
        std::fs::remove_file(connection_path(&home.0)).unwrap();
        assert_eq!(home.state(&config, URL)["state"], OTHER_CHANGES);
    }

    #[test]
    fn no_server_in_the_user_layer_is_unregistered() {
        let home = Home::new();
        let config = json!({
            "config": {},
            "layers": [{"name": {"type": "user", "file": home.0.join("config.toml")},
                        "version": "fixture-version", "config": {}}]
        });
        assert_eq!(home.state(&config, URL)["state"], UNREGISTERED);
    }

    #[test]
    fn what_cannot_be_read_or_verified_is_unknown() {
        let home = Home::new();
        let config = home.config(home.registered());
        // A connection setting from a stronger layer: connect refuses it.
        let mut foreign = config.clone();
        foreign["config"]["mcp_servers"][SERVER]["env"][URL_KEY] =
            json!("https://project.example.com");
        assert_eq!(home.state(&foreign, URL)["state"], UNKNOWN);
        assert_eq!(
            home.state(&config, "https://dæmon.example.com")["state"],
            UNKNOWN
        );
        std::fs::remove_file(home.operator()).unwrap();
        let answer = home.state(&config, URL);
        assert_eq!(answer["state"], UNKNOWN, "{answer}");
        assert_eq!(
            answer["detail"],
            "the token file given cannot be read as a token file"
        );
        home.write(&home.operator(), TOKEN);
        std::fs::remove_file(home.copy()).unwrap();
        let answer = home.state(&config, URL);
        assert_eq!(answer["state"], UNKNOWN, "{answer}");
        assert_eq!(answer["detail"], "Codex's token copy cannot be read");
        assert_eq!(home.state(&json!({}), URL)["state"], UNKNOWN);
    }
}
