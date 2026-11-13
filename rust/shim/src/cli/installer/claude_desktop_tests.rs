//! The cases of `tests/test_register_claude_desktop.py` (the frozen Python
//! registrar's), against the native step, in throwaway directories.
use super::*;
use std::collections::BTreeMap;
use std::fs;

const TOKEN: &str = "t0k3n-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx";
const OTHER: &str = "0th3r-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy";
const COMMAND: &str = "/abs/pseudolife-mcp";

struct Dir(PathBuf);
impl Dir {
    fn new() -> Self {
        // Private, as a profile folder is: the shared client configuration
        // writer refuses a file another account may write.
        let path = std::env::temp_dir().join(format!("w7-desktop-{}", uuid::Uuid::new_v4().simple()));
        crate::board::state::private_dir(&path).unwrap();
        Self(path)
    }
    fn config(&self) -> PathBuf {
        self.0.join("claude_desktop_config.json")
    }
    fn token_file(&self) -> PathBuf {
        self.0.join("private").join("claude-desktop.token")
    }
    fn write_token(&self, path: &Path, token: &str) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        crate::board::state::atomic_create(path, token.as_bytes()).unwrap();
    }
    fn read(&self) -> Value {
        serde_json::from_slice(&fs::read(self.config()).unwrap()).unwrap()
    }
    fn seed(&self, value: Value) -> Vec<u8> {
        let bytes = serde_json::to_vec_pretty(&value).unwrap();
        fs::write(self.config(), &bytes).unwrap();
        bytes
    }
}
impl Drop for Dir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

struct Run {
    env: BTreeMap<String, String>,
    probe: Option<bool>,
    probed: std::cell::RefCell<Vec<(String, Vec<String>)>>,
}
impl Run {
    fn new() -> Self {
        Self {
            env: BTreeMap::new(),
            probe: Some(true),
            probed: Default::default(),
        }
    }
    fn env(mut self, key: &str, value: &str) -> Self {
        self.env.insert(key.into(), value.into());
        self
    }
    fn probe(mut self, answer: Option<bool>) -> Self {
        self.probe = answer;
        self
    }
    fn go(&self, dir: &Dir, args: &[&str]) -> Outcome {
        let mut all = vec!["--command", COMMAND];
        let config = dir.config().to_string_lossy().into_owned();
        if !args.contains(&"--no-config") {
            all.extend(["--config", config.as_str()]);
        }
        all.extend(args.iter().filter(|a| **a != "--no-config"));
        let strings: Vec<String> = all.iter().map(|s| (*s).to_owned()).collect();
        let options = parse(&strings).unwrap().unwrap();
        let lookup = |key: &str| self.env.get(key).cloned();
        let probe = |command: &str, scrub: &[&str]| {
            self.probed.borrow_mut().push((
                command.to_owned(),
                scrub.iter().map(|s| (*s).to_owned()).collect(),
            ));
            self.probe
        };
        let host = Host {
            lookup: &lookup,
            home: dir.0.join("home"),
            windows: false,
            macos: false,
            probe: &probe,
        };
        register(&options, &host)
    }
}

fn text(outcome: &Outcome) -> String {
    [outcome.lines.clone(), outcome.warnings.clone()]
        .concat()
        .join("\n")
}

fn entry(config: &Value) -> &Value {
    &config["mcpServers"][SERVER]
}

// -- config path --------------------------------------------------------------

#[test]
fn windows_prefers_an_existing_msix_package_cache_and_falls_back_to_appdata() {
    let dir = Dir::new();
    let local = dir.0.join("Local");
    let roaming = dir.0.join("Roaming");
    let lookup_both = |key: &str| match key {
        "LOCALAPPDATA" => Some(local.to_string_lossy().into_owned()),
        "APPDATA" => Some(roaming.to_string_lossy().into_owned()),
        _ => None,
    };
    let appdata = roaming.join("Claude").join("claude_desktop_config.json");
    assert_eq!(
        config_candidates(true, false, &lookup_both, &dir.0),
        vec![appdata.clone()]
    );
    // A package family without the cache folder is not Desktop's copy.
    fs::create_dir_all(local.join("Packages").join("Claude_empty")).unwrap();
    let cache = local.join("Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude");
    fs::create_dir_all(&cache).unwrap();
    assert_eq!(
        config_candidates(true, false, &lookup_both, &dir.0),
        vec![cache.join("claude_desktop_config.json"), appdata]
    );
    // Without either variable the profile's AppData still resolves.
    let none = |_: &str| None;
    assert_eq!(
        config_candidates(true, false, &none, &dir.0),
        vec![dir.0.join("AppData/Roaming/Claude/claude_desktop_config.json")]
    );
}

#[test]
fn macos_linux_and_xdg_config_homes() {
    let home = Path::new("/home/u");
    let none = |_: &str| None;
    assert_eq!(
        config_candidates(false, true, &none, home),
        vec![home.join("Library/Application Support/Claude/claude_desktop_config.json")]
    );
    assert_eq!(
        config_candidates(false, false, &none, home),
        vec![home.join(".config/Claude/claude_desktop_config.json")]
    );
    let xdg = |key: &str| (key == "XDG_CONFIG_HOME").then(|| "/xdg".to_owned());
    assert_eq!(
        config_candidates(false, false, &xdg, home),
        vec![Path::new("/xdg").join("Claude/claude_desktop_config.json")]
    );
}

#[test]
fn without_config_the_resolved_path_is_used_and_the_other_windows_copy_named() {
    let dir = Dir::new();
    let local = dir.0.join("Local");
    let roaming = dir.0.join("Roaming");
    let cache = local.join("Packages/Claude_x/LocalCache/Roaming/Claude");
    fs::create_dir_all(&cache).unwrap();
    fs::create_dir_all(roaming.join("Claude")).unwrap();
    fs::write(roaming.join("Claude/claude_desktop_config.json"), "{}").unwrap();
    let env = BTreeMap::from([
        ("LOCALAPPDATA".to_owned(), local.to_string_lossy().into_owned()),
        ("APPDATA".to_owned(), roaming.to_string_lossy().into_owned()),
    ]);
    let lookup = |key: &str| env.get(key).cloned();
    let probe = |_: &str, _: &[&str]| Some(true);
    let host = Host {
        lookup: &lookup,
        home: dir.0.clone(),
        windows: true,
        macos: false,
        probe: &probe,
    };
    let options = parse(&["--command".into(), COMMAND.into()]).unwrap().unwrap();
    let outcome = register(&options, &host);
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert!(cache.join("claude_desktop_config.json").is_file());
    assert!(text(&outcome).contains("another Desktop config also exists"));
    assert_eq!(
        fs::read(roaming.join("Claude/claude_desktop_config.json")).unwrap(),
        b"{}"
    );
}

// -- the entry and the merge ------------------------------------------------------

#[test]
fn a_fresh_config_gets_an_absolute_command_writer_no_spawn_and_url() {
    let dir = Dir::new();
    let outcome = Run::new().go(&dir, &[]);
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert_eq!(outcome.state, "written");
    let config = dir.read();
    assert_eq!(
        config,
        json!({"mcpServers": {SERVER: {"command": COMMAND, "env": {
            "PSEUDOLIFE_WRITER_ID": "claude-desktop", "PSEUDOLIFE_MCP_NO_SPAWN": "1",
            "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765"}}}})
    );
    assert!(config["mcpServers"].get(LEGACY_SERVER).is_none());
    // UTF-8, no byte-order mark, trailing newline.
    let bytes = fs::read(dir.config()).unwrap();
    assert!(!bytes.starts_with(b"\xef\xbb\xbf"));
    assert!(bytes.ends_with(b"}\n"));
}

#[test]
fn command_and_token_files_must_be_absolute_and_nothing_is_touched() {
    let dir = Dir::new();
    for args in [
        vec!["--token-file", "relative.token"],
        vec!["--default-token-file", "relative.token"],
    ] {
        let outcome = Run::new().go(&dir, &args);
        assert_eq!(outcome.code, EXIT_REFUSED);
        assert!(text(&outcome).contains("absolute"));
    }
    let strings: Vec<String> = ["--command", "pseudolife-mcp", "--config"]
        .iter()
        .map(|s| (*s).to_owned())
        .chain([dir.config().to_string_lossy().into_owned()])
        .collect();
    let options = parse(&strings).unwrap().unwrap();
    let none = |_: &str| None;
    let probe = |_: &str, _: &[&str]| Some(true);
    let outcome = register(
        &options,
        &Host {
            lookup: &none,
            home: dir.0.clone(),
            windows: false,
            macos: false,
            probe: &probe,
        },
    );
    assert_eq!(outcome.code, EXIT_REFUSED);
    assert!(!dir.config().exists());
    assert!(is_absolute("C:\\Users\\u\\bin\\pseudolife-mcp.exe"));
    assert!(is_absolute("/usr/bin/pseudolife-mcp"));
    assert!(!is_absolute("bin/pseudolife-mcp"));
}

#[test]
fn every_other_key_server_and_user_env_is_kept_and_args_dropped() {
    let dir = Dir::new();
    dir.seed(json!({
        "globalShortcut": "Ctrl+Space",
        "mcpServers": {
            "other": {"command": "/x"},
            SERVER: {"command": "/old", "args": ["shim"], "env": {
                "HTTPS_PROXY": "http://proxy.example.com", "PSEUDOLIFE_MCP_DAEMON_URL": "http://old"}}
        }
    }));
    let outcome = Run::new().go(&dir, &[]);
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    let config = dir.read();
    assert_eq!(config["globalShortcut"], "Ctrl+Space");
    assert_eq!(config["mcpServers"]["other"], json!({"command": "/x"}));
    let ours = entry(&config);
    assert!(ours.get("args").is_none());
    assert_eq!(ours["env"]["HTTPS_PROXY"], "http://proxy.example.com");
    assert_eq!(ours["env"]["PSEUDOLIFE_MCP_DAEMON_URL"], "http://127.0.0.1:8765");
    assert!(text(&outcome).contains("stale 'args'"));
}

#[test]
fn a_rerun_changes_nothing_takes_no_backup_and_says_so_in_one_line() {
    let dir = Dir::new();
    assert_eq!(Run::new().go(&dir, &[]).code, EXIT_OK);
    let before = fs::read(dir.config()).unwrap();
    let again = Run::new().go(&dir, &[]);
    assert_eq!(again.code, EXIT_OK);
    assert_eq!(again.state, "unchanged");
    assert!(again.backup.is_none());
    assert_eq!(fs::read(dir.config()).unwrap(), before);
    assert_eq!(
        again
            .lines
            .iter()
            .filter(|l| l.starts_with("entry:"))
            .collect::<Vec<_>>(),
        [&format!("entry: unchanged ({SERVER} already registered this way)")]
    );
}

#[test]
fn a_change_backs_up_the_displaced_file_privately() {
    let dir = Dir::new();
    let original = dir.seed(json!({"mcpServers": {"other": {"command": "/x"}}}));
    let outcome = Run::new().go(&dir, &[]);
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    let backup = outcome.backup.clone().expect("a backup");
    assert_eq!(fs::read(&backup).unwrap(), original);
    // The shared writer keeps the displaced file in its private recovery
    // folder beside the configuration (the file itself becomes owner-only
    // with the writer's #1545 fix).
    let folder = backup.parent().unwrap();
    assert!(
        folder
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with(".client-native-backup-"),
        "{}",
        backup.display()
    );
    assert!(!backup.to_string_lossy().starts_with(r"\\?\"), "{}", backup.display());
    assert_eq!(
        fs::canonicalize(folder.parent().unwrap()).unwrap(),
        fs::canonicalize(&dir.0).unwrap()
    );
}

#[test]
fn a_byte_order_mark_is_read_and_never_written() {
    let dir = Dir::new();
    let mut bytes = b"\xef\xbb\xbf".to_vec();
    bytes.extend(br#"{"mcpServers": {"other": {"command": "/x"}}}"#);
    fs::write(dir.config(), &bytes).unwrap();
    assert_eq!(Run::new().go(&dir, &[]).code, EXIT_OK);
    let written = fs::read(dir.config()).unwrap();
    assert!(!written.starts_with(b"\xef\xbb\xbf"));
    assert_eq!(dir.read()["mcpServers"]["other"]["command"], "/x");
}

#[test]
fn malformed_non_utf8_and_non_object_configs_are_refused_untouched() {
    for bytes in [&b"{not json"[..], &b"\xff\xfe{}"[..], &b"[1, 2]"[..]] {
        let dir = Dir::new();
        fs::write(dir.config(), bytes).unwrap();
        let outcome = Run::new().go(&dir, &[]);
        assert_eq!(outcome.code, EXIT_REFUSED, "{}", text(&outcome));
        assert!(text(&outcome).contains("fix or move it first") || text(&outcome).contains("not a JSON object"));
        assert_eq!(fs::read(dir.config()).unwrap(), bytes);
    }
}

#[test]
fn non_object_shapes_are_refused_and_null_servers_treated_as_empty() {
    for shape in [
        json!({"mcpServers": []}),
        json!({"mcpServers": {SERVER: "x"}}),
        json!({"mcpServers": {SERVER: {"env": "x"}}}),
    ] {
        let dir = Dir::new();
        let before = dir.seed(shape);
        let outcome = Run::new().go(&dir, &[]);
        assert_eq!(outcome.code, EXIT_REFUSED, "{}", text(&outcome));
        assert_eq!(fs::read(dir.config()).unwrap(), before);
    }
    let dir = Dir::new();
    dir.seed(json!({"mcpServers": null}));
    assert_eq!(Run::new().go(&dir, &[]).code, EXIT_OK);
    assert_eq!(entry(&dir.read())["command"], COMMAND);
}

#[test]
fn a_dry_run_prints_the_entry_and_writes_nothing() {
    let dir = Dir::new();
    let outcome = Run::new().go(&dir, &["--dry-run"]);
    assert_eq!(outcome.code, EXIT_OK);
    assert_eq!(outcome.state, "dry-run");
    assert!(!dir.config().exists());
    assert!(text(&outcome).contains("dry run: nothing written"));
    assert!(text(&outcome).contains(SERVER));
}

// -- the legacy name ---------------------------------------------------------------

fn legacy(env: Value) -> Value {
    let mut env = env;
    env["PSEUDOLIFE_WRITER_ID"] = json!("claude-desktop");
    json!({"command": "/old/pseudolife-mcp", "env": env, "keep": "me"})
}

#[test]
fn an_owned_legacy_entry_moves_with_its_hand_added_settings() {
    let dir = Dir::new();
    dir.seed(json!({"mcpServers": {LEGACY_SERVER: legacy(json!({"HTTPS_PROXY": "p"}))}}));
    let outcome = Run::new().go(&dir, &[]);
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    let config = dir.read();
    assert!(config["mcpServers"].get(LEGACY_SERVER).is_none());
    let ours = entry(&config);
    assert_eq!(ours["keep"], "me");
    assert_eq!(ours["env"]["HTTPS_PROXY"], "p");
    assert_eq!(ours["command"], COMMAND);
    assert!(text(&outcome).contains("migrated mcpServers.pseudolife-memory"));
    // Stable on rerun.
    let again = Run::new().go(&dir, &[]);
    assert_eq!(again.state, "unchanged");
}

#[test]
fn an_owned_legacy_literal_token_moves_into_the_file() {
    let dir = Dir::new();
    dir.seed(json!({"mcpServers": {LEGACY_SERVER: legacy(json!({"PSEUDOLIFE_MCP_TOKEN": TOKEN}))}}));
    let file = dir.token_file().to_string_lossy().into_owned();
    let outcome = Run::new().go(&dir, &["--default-token-file", &file]);
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert_eq!(fs::read(dir.token_file()).unwrap(), TOKEN.as_bytes());
    let ours = entry(&dir.read()).clone();
    assert!(ours["env"].get("PSEUDOLIFE_MCP_TOKEN").is_none());
    assert_eq!(ours["env"][FILE_KEY], json!(file));
    assert!(!fs::read_to_string(dir.config()).unwrap().contains(TOKEN));
    assert!(!text(&outcome).contains(TOKEN));
}

#[test]
fn a_foreign_legacy_entry_is_left_alone_reported_and_its_token_not_harvested() {
    let dir = Dir::new();
    let foreign = json!({"command": "/theirs", "env": {"PSEUDOLIFE_MCP_TOKEN": TOKEN}});
    dir.seed(json!({"mcpServers": {LEGACY_SERVER: foreign.clone()}}));
    let file = dir.token_file().to_string_lossy().into_owned();
    let outcome = Run::new().go(&dir, &["--default-token-file", &file]);
    assert_eq!(outcome.code, EXIT_NO_CREDENTIAL, "{}", text(&outcome));
    let config = dir.read();
    assert_eq!(config["mcpServers"][LEGACY_SERVER], foreign);
    assert!(entry(&config).is_object());
    assert!(!dir.token_file().exists());
    assert!(text(&outcome).contains("was not written by this installer"));
}

#[test]
fn when_both_names_exist_the_desktop_entry_wins_and_the_old_fills_gaps() {
    let dir = Dir::new();
    dir.seed(json!({"mcpServers": {
        LEGACY_SERVER: legacy(json!({"HTTPS_PROXY": "old", "ONLY_OLD": "1"})),
        SERVER: {"command": "/new", "env": {"HTTPS_PROXY": "new"}},
    }}));
    let outcome = Run::new().go(&dir, &["--dry-run"]);
    assert!(text(&outcome).contains("would keep the pseudolife-desktop value of HTTPS_PROXY"));
    let outcome = Run::new().go(&dir, &[]);
    assert_eq!(outcome.code, EXIT_OK);
    let ours = entry(&dir.read()).clone();
    assert_eq!(ours["env"]["HTTPS_PROXY"], "new");
    assert_eq!(ours["env"]["ONLY_OLD"], "1");
    assert_eq!(ours["keep"], "me");
    assert!(text(&outcome).contains("kept the pseudolife-desktop value of HTTPS_PROXY"));
}

#[test]
fn a_refused_run_leaves_the_legacy_entry_in_place() {
    let dir = Dir::new();
    let before = dir.seed(json!({"mcpServers": {LEGACY_SERVER: legacy(json!({})), SERVER: "not an object"}}));
    let outcome = Run::new().go(&dir, &[]);
    assert_eq!(outcome.code, EXIT_REFUSED);
    assert_eq!(fs::read(dir.config()).unwrap(), before);
}

// -- the credential ----------------------------------------------------------------

#[test]
fn a_token_from_the_environment_is_written_owner_only_and_never_printed() {
    let dir = Dir::new();
    let file = dir.token_file().to_string_lossy().into_owned();
    let run = Run::new().env("PL_TEST_DESKTOP_TOKEN", TOKEN);
    let outcome = run.go(
        &dir,
        &["--token-file", &file, "--token-from-env", "PL_TEST_DESKTOP_TOKEN"],
    );
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert_eq!(fs::read(dir.token_file()).unwrap(), TOKEN.as_bytes());
    crate::board::state::open(&dir.token_file()).expect("token file is owner-only");
    assert_eq!(entry(&dir.read())["env"][FILE_KEY], json!(file));
    assert!(!text(&outcome).contains(TOKEN));
    assert!(!fs::read_to_string(dir.config()).unwrap().contains(TOKEN));
    // The probe was asked not to pass the token's variable on.
    assert_eq!(
        run.probed.borrow().as_slice(),
        [(COMMAND.to_owned(), vec!["PL_TEST_DESKTOP_TOKEN".to_owned()])]
    );
}

#[test]
fn a_literal_token_in_the_entry_moves_into_the_file_and_an_env_token_wins() {
    let dir = Dir::new();
    dir.seed(json!({"mcpServers": {SERVER: {"command": "/old", "env": {"PSEUDOLIFE_MCP_TOKEN": OTHER}}}}));
    let file = dir.token_file().to_string_lossy().into_owned();
    let outcome = Run::new().go(&dir, &["--default-token-file", &file]);
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert_eq!(fs::read(dir.token_file()).unwrap(), OTHER.as_bytes());
    assert!(text(&outcome).contains("migrated the literal"));
    let ours = entry(&dir.read()).clone();
    assert!(ours["env"].get("PSEUDOLIFE_MCP_TOKEN").is_none());

    let dir = Dir::new();
    dir.seed(json!({"mcpServers": {SERVER: {"command": "/old", "env": {"PSEUDOLIFE_MCP_TOKEN": OTHER}}}}));
    let file = dir.token_file().to_string_lossy().into_owned();
    let outcome = Run::new().env("T", TOKEN).go(
        &dir,
        &["--default-token-file", &file, "--token-from-env", "T"],
    );
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert_eq!(fs::read(dir.token_file()).unwrap(), TOKEN.as_bytes());
    assert!(text(&outcome).contains("dropped the literal"));
}

#[test]
fn an_existing_valid_token_file_is_used_as_is() {
    let dir = Dir::new();
    dir.write_token(&dir.token_file(), TOKEN);
    let file = dir.token_file().to_string_lossy().into_owned();
    let outcome = Run::new().go(&dir, &["--default-token-file", &file]);
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert!(text(&outcome).contains("using the existing"));
    assert_eq!(entry(&dir.read())["env"][FILE_KEY], json!(file));
}

#[test]
fn an_unusable_existing_token_file_is_refused_and_the_config_untouched() {
    let dir = Dir::new();
    dir.write_token(&dir.token_file(), "has a\nnewline");
    let file = dir.token_file().to_string_lossy().into_owned();
    let outcome = Run::new().go(&dir, &["--default-token-file", &file]);
    assert_eq!(outcome.code, EXIT_REFUSED, "{}", text(&outcome));
    assert!(text(&outcome).contains("cannot be used"));
    assert!(!dir.config().exists());
}

#[test]
fn no_token_anywhere_registers_without_a_credential_and_exits_3() {
    let dir = Dir::new();
    let file = dir.token_file().to_string_lossy().into_owned();
    let outcome = Run::new().go(&dir, &["--default-token-file", &file]);
    assert_eq!(outcome.code, EXIT_NO_CREDENTIAL, "{}", text(&outcome));
    assert_eq!(outcome.state, "written");
    assert!(entry(&dir.read())["env"].get(FILE_KEY).is_none());
    assert!(text(&outcome).contains("WITHOUT a credential"));
}

#[test]
fn a_token_source_without_a_token_file_is_refused() {
    let dir = Dir::new();
    let outcome = Run::new().env("T", TOKEN).go(&dir, &["--token-from-env", "T"]);
    assert_eq!(outcome.code, EXIT_REFUSED);
    assert!(text(&outcome).contains("no token-file path"));
    assert!(!dir.config().exists());
}

#[test]
fn a_configured_custom_file_is_preserved_validated_and_not_rotated() {
    let dir = Dir::new();
    let custom = dir.0.join("custom.token");
    dir.write_token(&custom, OTHER);
    let custom_text = custom.to_string_lossy().into_owned();
    dir.seed(json!({"mcpServers": {SERVER: {"command": "/old", "env": {FILE_KEY: custom_text}}}}));
    let default = dir.token_file().to_string_lossy().into_owned();
    let outcome = Run::new().env("T", TOKEN).go(
        &dir,
        &["--default-token-file", &default, "--token-from-env", "T"],
    );
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert_eq!(entry(&dir.read())["env"][FILE_KEY], json!(custom_text));
    assert_eq!(fs::read(&custom).unwrap(), OTHER.as_bytes(), "not rotated");
    assert!(!dir.token_file().exists());
}

#[test]
fn a_missing_configured_or_explicit_file_is_refused_without_rewriting() {
    let dir = Dir::new();
    let gone = dir.0.join("gone.token").to_string_lossy().into_owned();
    let before = dir.seed(json!({"mcpServers": {SERVER: {"command": "/old", "env": {FILE_KEY: gone}}}}));
    let outcome = Run::new().go(&dir, &[]);
    assert_eq!(outcome.code, EXIT_REFUSED, "{}", text(&outcome));
    assert!(text(&outcome).contains("configured token file"));
    assert_eq!(fs::read(dir.config()).unwrap(), before);
    let explicit = dir.0.join("explicit.token").to_string_lossy().into_owned();
    let outcome = Run::new().go(&dir, &["--token-file", &explicit]);
    assert_eq!(outcome.code, EXIT_REFUSED, "{}", text(&outcome));
    assert!(text(&outcome).contains("explicit token file"));
    assert_eq!(fs::read(dir.config()).unwrap(), before);
}

#[test]
fn a_validated_existing_file_removes_a_superseded_literal() {
    let dir = Dir::new();
    dir.write_token(&dir.token_file(), TOKEN);
    let file = dir.token_file().to_string_lossy().into_owned();
    dir.seed(json!({"mcpServers": {SERVER: {"command": "/old", "env": {FILE_KEY: file, "PSEUDOLIFE_MCP_TOKEN": OTHER}}}}));
    let outcome = Run::new().go(&dir, &[]);
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert!(entry(&dir.read())["env"].get("PSEUDOLIFE_MCP_TOKEN").is_none());
    assert_eq!(fs::read(dir.token_file()).unwrap(), TOKEN.as_bytes());
}

#[test]
fn a_token_map_with_one_desktop_principal_is_written_without_printing_it() {
    let dir = Dir::new();
    let file = dir.token_file().to_string_lossy().into_owned();
    let map = format!("{OTHER}:codex, {TOKEN}:Claude-Desktop");
    let outcome = Run::new().env("M", &map).go(
        &dir,
        &["--default-token-file", &file, "--tokens-from-env", "M"],
    );
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert_eq!(fs::read(dir.token_file()).unwrap(), TOKEN.as_bytes());
    assert!(!text(&outcome).contains(TOKEN) && !text(&outcome).contains(OTHER));
}

#[test]
fn a_token_map_without_exactly_one_desktop_credential_is_refused() {
    for map in [
        format!("{OTHER}:codex"),
        format!("{TOKEN}:claude-desktop,{OTHER}:claude-desktop"),
        format!("{TOKEN}:default"),
    ] {
        let dir = Dir::new();
        let file = dir.token_file().to_string_lossy().into_owned();
        let outcome = Run::new().env("M", &map).go(
            &dir,
            &["--default-token-file", &file, "--tokens-from-env", "M"],
        );
        assert_eq!(outcome.code, EXIT_REFUSED, "{map}: {}", text(&outcome));
        assert!(!dir.config().exists());
        assert!(!text(&outcome).contains(TOKEN));
    }
}

#[test]
fn the_token_map_parser_follows_the_daemon_rule() {
    assert_eq!(
        parse_token_map("a:b:Codex, ,bad, :x, t:default, t2:maintainer, a:b:dup,c:d"),
        vec![
            ("a:b".to_owned(), "codex".to_owned()),
            ("c".to_owned(), "d".to_owned())
        ]
    );
}

// -- the client probe ----------------------------------------------------------------

#[test]
fn a_client_that_cannot_read_a_token_file_is_refused_before_anything_is_written() {
    let dir = Dir::new();
    let file = dir.token_file().to_string_lossy().into_owned();
    let outcome = Run::new()
        .env("T", TOKEN)
        .probe(Some(false))
        .go(&dir, &["--token-file", &file, "--token-from-env", "T"]);
    assert_eq!(outcome.code, EXIT_OLD_SHIM, "{}", text(&outcome));
    assert!(!dir.config().exists());
    assert!(!dir.token_file().exists());
    let text = text(&outcome);
    assert!(text.contains(COMMAND) && text.contains(TOKEN_FILE_MARKER));
    assert!(text.contains("--skip-shim-check"));
    assert!(!text.contains(TOKEN));
    // A dry run reports it too.
    let dry = Run::new()
        .env("T", TOKEN)
        .probe(Some(false))
        .go(&dir, &["--token-file", &file, "--token-from-env", "T", "--dry-run"]);
    assert_eq!(dry.code, EXIT_OLD_SHIM);
}

#[test]
fn the_probe_runs_only_with_a_token_file_and_no_evidence_is_a_note() {
    let dir = Dir::new();
    let run = Run::new();
    assert_eq!(run.go(&dir, &[]).code, EXIT_OK);
    assert!(run.probed.borrow().is_empty(), "no token file, no probe");
    let file = dir.token_file().to_string_lossy().into_owned();
    let outcome = Run::new()
        .env("T", TOKEN)
        .probe(None)
        .go(&dir, &["--default-token-file", &file, "--token-from-env", "T"]);
    assert_eq!(outcome.code, EXIT_OK, "{}", text(&outcome));
    assert!(text(&outcome).contains("could not verify"));
    let skipped = Run::new().env("T", TOKEN).probe(Some(false)).go(
        &dir,
        &["--token-file", &file, "--token-from-env", "T", "--skip-shim-check"],
    );
    assert_eq!(skipped.code, EXIT_OK, "{}", text(&skipped));
    assert!(text(&skipped).contains("skipped (--skip-shim-check)"));
}

#[test]
fn a_real_probe_reads_help_on_stdout_or_stderr_and_never_hangs() {
    let dir = Dir::new();
    let script = |name: &str, body: &str| -> String {
        if cfg!(windows) {
            let path = dir.0.join(format!("{name}.cmd"));
            fs::write(&path, format!("@echo off\r\n{body}\r\n")).unwrap();
            path.to_string_lossy().into_owned()
        } else {
            let path = dir.0.join(name);
            fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
            }
            path.to_string_lossy().into_owned()
        }
    };
    let quick = Duration::from_secs(20);
    let capable = script("capable", "echo usage: set PSEUDOLIFE_MCP_TOKEN_FILE");
    assert_eq!(shim_reads_token_file(&capable, &[], quick), Some(true));
    let on_stderr = script("stderr", "echo PSEUDOLIFE_MCP_TOKEN_FILE 1>&2");
    assert_eq!(shim_reads_token_file(&on_stderr, &[], quick), Some(true));
    let released = script("released", "echo usage: PSEUDOLIFE_MCP_TOKEN only");
    assert_eq!(shim_reads_token_file(&released, &[], quick), Some(false));
    let pre_help = script("prehelp", "echo unknown mode: --help\nexit 2");
    assert_eq!(shim_reads_token_file(&pre_help, &[], quick), Some(false));
    let silent = script("silent", "exit 0");
    assert_eq!(shim_reads_token_file(&silent, &[], quick), None);
    assert_eq!(
        shim_reads_token_file(&dir.0.join("missing").to_string_lossy(), &[], quick),
        None
    );
    // A wrapper that ignores --help and waits on stdin gets end-of-file.
    let reader = if cfg!(windows) {
        script("reader", "more\necho done")
    } else {
        script("reader", "cat\necho done")
    };
    assert_eq!(shim_reads_token_file(&reader, &[], quick), Some(false));
}

#[test]
fn the_native_help_advertises_the_token_file_form() {
    // The probe reads this text from the installed native client.
    let help = include_str!("../../cli_help.txt");
    assert!(help.contains(TOKEN_FILE_MARKER));
}

#[test]
fn options_parse_and_refuse_alternatives_and_repeats() {
    let strings = |items: &[&str]| -> Vec<String> { items.iter().map(|s| (*s).to_owned()).collect() };
    assert!(parse(&strings(&[])).is_err(), "--command is required");
    assert!(
        parse(&strings(&[
            "--command", COMMAND, "--token-file", "/a", "--default-token-file", "/b"
        ]))
        .is_err()
    );
    assert!(parse(&strings(&["--command", COMMAND, "--json", "--json"])).is_err());
    assert!(parse(&strings(&["--command"])).is_err());
    assert_eq!(parse(&strings(&["--help"])), Ok(None));
    let parsed = parse(&strings(&["--command", COMMAND, "--writer-id", "claude-desktop"]))
        .unwrap()
        .unwrap();
    assert_eq!(parsed.daemon_url, DEFAULT_DAEMON_URL);
    assert_eq!(parsed.writer_id, "claude-desktop");
}
