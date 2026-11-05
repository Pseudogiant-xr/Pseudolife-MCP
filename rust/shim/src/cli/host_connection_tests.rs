use super::*;
use std::{collections::HashMap, io::Write, path::Path};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("codex-cli-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(root.join("pseudolife")).unwrap();
        Self(root)
    }

    fn env(&self) -> HashMap<String, OsString> {
        HashMap::from([
            ("CODEX_THREAD_ID".into(), "fixture-thread".into()),
            ("CODEX_HOME".into(), self.0.clone().into_os_string()),
        ])
    }

    fn connection(&self, token: &str) -> PathBuf {
        let path = codex::connection_path(&self.0);
        let data = serde_json::json!({
            "version": 1,
            "daemon_url": encode64("https://fixture.example/"),
            "token_file": encode64(token),
        });
        private_file(&path, &serde_json::to_vec(&data).unwrap());
        path
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn private_file(path: &Path, bytes: &[u8]) {
    let mut opened = crate::board::state::create(path).unwrap();
    opened.file.write_all(bytes).unwrap();
}

fn encode64(text: &str) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut encoded = String::new();
    for chunk in text.as_bytes().chunks(3) {
        let value = ((chunk[0] as u32) << 16)
            | ((chunk.get(1).copied().unwrap_or(0) as u32) << 8)
            | chunk.get(2).copied().unwrap_or(0) as u32;
        for shift in [18, 12, 6, 0] {
            encoded.push(ALPHABET[((value >> shift) & 63) as usize] as char);
        }
        for _ in chunk.len()..3 {
            encoded.pop();
        }
        for _ in chunk.len()..3 {
            encoded.push('=');
        }
    }
    encoded
}

fn discover(
    env: &HashMap<String, OsString>,
    mode: &str,
    args: &[&str],
) -> Result<Environment, SetupError> {
    discover_from(
        mode,
        &args.iter().map(|value| (*value).into()).collect::<Vec<_>>(),
        |key| env.get(key).cloned(),
    )
}

#[test]
fn managed_pair_is_selected_without_ambient_credentials_for_both_codex_markers() {
    let fixture = Fixture::new();
    let token = fixture.0.join("token-λ");
    private_file(&token, b"fixture-bearer\n");
    let connection = fixture.connection(token.to_str().unwrap());
    let before = std::fs::read(&connection).unwrap();
    for marker in ["CODEX_THREAD_ID", "CODEX_SESSION_ID"] {
        let mut env = fixture.env();
        env.remove("CODEX_THREAD_ID");
        env.insert(marker.into(), "fixture-session".into());
        for mode in ["lease", "doctor"] {
            let found = discover(&env, mode, &[]).unwrap();
            assert_eq!(
                found.additions(),
                &[
                    (URL_KEY.into(), "https://fixture.example".into()),
                    (FILE_KEY.into(), token.to_str().unwrap().into()),
                ]
            );
        }
    }
    assert_eq!(std::fs::read(connection).unwrap(), before);
    assert!(std::fs::read(&token).unwrap() == b"fixture-bearer\n");
}

#[test]
fn matching_ambient_sources_keep_their_credential_source_and_only_add_the_origin() {
    let fixture = Fixture::new();
    let saved = fixture.0.join("saved-token");
    let ambient = fixture.0.join("ambient-token");
    private_file(&saved, b"fixture-bearer\r\n");
    private_file(&ambient, b"fixture-bearer\n");
    fixture.connection(saved.to_str().unwrap());
    for key in [TOKEN_KEY, FILE_KEY] {
        let mut env = fixture.env();
        env.insert(
            key.into(),
            if key == TOKEN_KEY {
                "fixture-bearer".into()
            } else {
                ambient.clone().into_os_string()
            },
        );
        let found = discover(&env, "lease", &["check", "full-suite"]).unwrap();
        assert_eq!(
            found.additions(),
            &[(URL_KEY.into(), "https://fixture.example".into())]
        );
    }
}

#[test]
fn mismatched_or_unreadable_ambient_credentials_refuse_with_the_one_origin_fix() {
    let fixture = Fixture::new();
    let saved = fixture.0.join("saved-token");
    private_file(&saved, b"saved-fixture-bearer");
    fixture.connection(saved.to_str().unwrap());
    for (key, value) in [
        (TOKEN_KEY, OsString::from("different-fixture-bearer")),
        (FILE_KEY, fixture.0.join("missing-token").into_os_string()),
        (FILE_KEY, OsString::new()),
    ] {
        let mut env = fixture.env();
        env.insert(key.into(), value);
        let error = discover(&env, "doctor", &[]).unwrap_err().to_string();
        assert!(error.ends_with("Set PSEUDOLIFE_MCP_DAEMON_URL to the intended daemon and retry."));
        assert!(
            !error.contains("saved-fixture-bearer") && !error.contains("different-fixture-bearer")
        );
    }
}

#[test]
fn ambient_file_wins_over_a_different_literal() {
    let fixture = Fixture::new();
    let token = fixture.0.join("saved-token");
    private_file(&token, b"fixture-bearer");
    fixture.connection(token.to_str().unwrap());
    let mut env = fixture.env();
    env.insert(FILE_KEY.into(), token.into_os_string());
    env.insert(TOKEN_KEY.into(), "different-fixture-bearer".into());
    assert!(discover(&env, "lease", &[]).is_ok());
}

#[test]
fn an_ambient_bearer_requires_a_readable_saved_bearer() {
    for saved_path in [None, Some("missing-token")] {
        let fixture = Fixture::new();
        let path = saved_path.map(|name| fixture.0.join(name));
        fixture.connection(path.as_deref().and_then(Path::to_str).unwrap_or(""));
        let mut env = fixture.env();
        env.insert(TOKEN_KEY.into(), "fixture-bearer".into());
        let error = discover(&env, "lease", &[]).unwrap_err().to_string();
        assert!(error.ends_with("Set PSEUDOLIFE_MCP_DAEMON_URL to the intended daemon and retry."));
    }
}

#[test]
fn an_explicit_origin_or_non_codex_command_never_reads_bad_managed_state() {
    let fixture = Fixture::new();
    private_file(&codex::connection_path(&fixture.0), b"malformed");
    let mut env = fixture.env();
    for origin in ["", "https://explicit.example"] {
        env.insert(URL_KEY.into(), origin.into());
        assert!(discover(&env, "lease", &[]).unwrap().additions().is_empty());
    }
    env.remove(URL_KEY);
    env.remove("CODEX_THREAD_ID");
    assert!(
        discover(&env, "doctor", &[])
            .unwrap()
            .additions()
            .is_empty()
    );
}

#[test]
fn offline_options_only_bypass_discovery_before_the_command_separator() {
    let fixture = Fixture::new();
    private_file(&codex::connection_path(&fixture.0), b"malformed");
    let env = fixture.env();
    for (mode, option) in [
        ("lease", "--help"),
        ("lease", "-h"),
        ("lease", "--no-board"),
        ("doctor", "--help"),
        ("doctor", "-h"),
        ("doctor", "--disposable-proof"),
    ] {
        assert!(
            discover(&env, mode, &[option])
                .unwrap()
                .additions()
                .is_empty()
        );
        assert!(discover(&env, mode, &["--", "child", option]).is_err());
    }
    assert!(discover(&env, "doctor", &["--no-board"]).is_err());
    assert!(discover(&env, "lease", &["--disposable-proof"]).is_err());
}

#[test]
fn missing_managed_state_preserves_the_ordinary_connection_path() {
    let fixture = Fixture::new();
    assert!(
        discover(&fixture.env(), "lease", &[])
            .unwrap()
            .additions()
            .is_empty()
    );
}

#[test]
fn tokenless_selection_is_explicit_and_operators_ignore_bad_managed_state() {
    let fixture = Fixture::new();
    fixture.connection("");
    let found = discover(&fixture.env(), "doctor", &[]).unwrap();
    assert!(
        found
            .additions()
            .contains(&(TOKEN_KEY.into(), String::new()))
    );
    std::fs::remove_file(codex::connection_path(&fixture.0)).unwrap();
    private_file(&codex::connection_path(&fixture.0), b"malformed");
    for action in ["break", "conductor", "delegate", "designate"] {
        assert!(
            discover(&fixture.env(), "lease", &[action, "fixture"])
                .unwrap()
                .additions()
                .is_empty()
        );
    }
}

#[test]
fn managed_state_must_decode_and_name_a_valid_daemon_origin() {
    for value in [
        serde_json::json!({"daemon_url": "not-base64", "token_file": ""}),
        serde_json::json!({"daemon_url": encode64("https://fixture.example/private"), "token_file": ""}),
    ] {
        let fixture = Fixture::new();
        private_file(
            &codex::connection_path(&fixture.0),
            &serde_json::to_vec(&value).unwrap(),
        );
        assert!(discover(&fixture.env(), "doctor", &[]).is_err());
    }
}

#[cfg(unix)]
#[test]
fn a_public_managed_connection_is_refused() {
    use std::os::unix::fs::PermissionsExt;
    let fixture = Fixture::new();
    let path = fixture.connection("");
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o644)).unwrap();
    assert!(discover(&fixture.env(), "doctor", &[]).is_err());
}
