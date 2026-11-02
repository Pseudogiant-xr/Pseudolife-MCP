use pseudolife_stdio::setup::codex::{
    Rpc, SetupError, apply_replace, plan_registration, plan_replace, read_connection, user_config,
};
use serde_json::{Map, Value, json};
use std::path::Path;

fn captured_layers(home: &Home, trusted: bool) -> Fake {
    // Project/user/system order and disabledReason shape captured from an
    // isolated Codex 0.166.0 config/read on 2026-11-02. Paths, versions and
    // unrelated configuration are redacted in the fixture.
    let captured: Value =
        serde_json::from_str(include_str!("fixtures/codex-layer-order.json")).unwrap();
    let mut client = Fake::new(&home.0);
    let user_values = client.config["config"].clone();
    client.config = captured[if trusted { "trusted" } else { "untrusted" }].clone();
    for layer in client.config["layers"].as_array_mut().unwrap() {
        match layer["name"]["type"].as_str().unwrap() {
            "project" => {
                layer["name"]["dotCodexFolder"] = json!(home.0.join("project").join(".codex"))
            }
            "user" => {
                layer["name"]["file"] = json!(home.0.join("config.toml"));
                layer["config"] = user_values.clone();
                layer["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"] =
                    json!("1");
            }
            "system" => {
                layer["name"]["file"] = json!(home.0.join("system.toml"));
                layer["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"] =
                    json!("0");
            }
            other => panic!("unexpected captured layer {other}"),
        }
    }
    client.config["config"] = client.config["layers"][1]["config"].clone();
    if trusted {
        client.project_no_spawn = Some("0".into());
        client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"] =
            json!("0");
    }
    client
}

#[tokio::test]
async fn captured_codex_layers_refuse_active_project_before_any_write() {
    let home = Home::new();
    let mut client = captured_layers(&home, true);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let error = apply_replace(&mut client, &home.0, plan)
        .await
        .err()
        .unwrap()
        .to_string();
    assert!(error.contains(&format!(
        "{:?}",
        home.0.join("project").join(".codex").join("config.toml")
    )));
    assert!(
        client
            .calls
            .iter()
            .all(|(method, _)| method != "config/batchWrite")
    );
    assert!(!home.0.join("pseudolife").exists());
}

#[tokio::test]
async fn captured_codex_layers_ignore_disabled_project_and_lower_system() {
    let home = Home::new();
    let mut client = captured_layers(&home, false);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    assert!(apply_replace(&mut client, &home.0, plan).await.is_ok());
}

#[tokio::test]
async fn disabled_project_credentials_do_not_claim_the_user_key() {
    for key in ["PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE"] {
        let home = Home::new();
        let mut client = captured_layers(&home, false);
        for layer in client.config["layers"].as_array_mut().unwrap() {
            layer["config"]["mcp_servers"]["pseudolife-memory"]["env"] = json!({key:""});
        }
        client.config["config"] = client.config["layers"][1]["config"].clone();
        let config = client.config.clone();
        let error = pseudolife_stdio::setup::codex::configure_credential_file(
            &mut client,
            &home.0,
            &home.0,
            Some(config),
            Some(("http://localhost:8765", None)),
        )
        .await
        .unwrap_err()
        .to_string();
        // An empty file path may fail credential decoding first; either way
        // the ignored project cannot be named as the owner requiring repair.
        assert!(!error.contains("comes from"));
        assert!(!home.0.join("pseudolife").exists());
    }
}

#[tokio::test]
async fn unchanged_plan_refuses_effective_connection_changed_after_planning() {
    for no_spawn in [false, true] {
        for key in ["PSEUDOLIFE_MCP_DAEMON_URL", "PSEUDOLIFE_MCP_TOKEN_FILE"] {
            let home = Home::new();
            let mut client = Fake::new(&home.0);
            let plan =
                plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
            apply_replace(&mut client, &home.0, plan).await.unwrap();
            let plan = plan_replace(
                &client.config,
                &home.0,
                "https://example.com",
                None,
                no_spawn,
            )
            .unwrap();
            assert_eq!(plan.env(), plan.previous_env());
            let connection = home.0.join("pseudolife/connection.json");
            let before = std::fs::read(&connection).unwrap();
            client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"][key] =
                json!("project-changed-value");
            client.calls.clear();
            assert!(apply_replace(&mut client, &home.0, plan).await.is_err());
            assert_eq!(std::fs::read(&connection).unwrap(), before);
            assert!(
                client
                    .calls
                    .iter()
                    .all(|(method, _)| method != "config/batchWrite")
            );
        }
    }
}

#[tokio::test]
async fn project_authority_diagnostic_uses_installed_codex_origin_shape() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let folder = home.0.join("project").join(".codex");
    client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"] =
        json!("0");
    client.config["layers"].as_array_mut().unwrap().insert(0, json!({"name":{"type":"project","dotCodexFolder":folder},"version":"project-a","config":{"mcp_servers":{"pseudolife-memory":{"env":{"PSEUDOLIFE_MCP_NO_SPAWN":"0"}}}}}));
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let error = apply_replace(&mut client, &home.0, plan)
        .await
        .err()
        .unwrap()
        .to_string();
    assert!(error.contains(&format!("{:?}", folder.join("config.toml"))));
    assert!(error.contains("PSEUDOLIFE_MCP_NO_SPAWN"));
    assert!(!home.0.join("pseudolife").exists());
}

#[tokio::test]
async fn write_ack_unknown_pending_write_keeps_token_after_baseline_reads() {
    struct Deferred {
        inner: Fake,
        pending: Option<Value>,
    }
    impl Rpc for Deferred {
        async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
            if method == "config/batchWrite" {
                self.pending = Some(params);
                return Err(SetupError("fixture write timed out while still pending"));
            }
            self.inner.rpc(method, params).await
        }
    }
    let home = Home::new();
    let mut client = Deferred {
        inner: Fake::new(&home.0),
        pending: None,
    };
    let plan = plan_replace(
        &client.inner.config,
        &home.0,
        "https://example.com",
        None,
        true,
    )
    .unwrap();
    assert!(apply_replace(&mut client, &home.0, plan).await.is_err());
    // Read snapshots returned the baseline, but the timed-out request remains
    // pending and may commit after the failed apply has returned to its caller.
    let pending = client.pending.take().unwrap();
    client
        .inner
        .rpc("config/batchWrite", pending)
        .await
        .unwrap();
    let token = home.0.join("pseudolife").join("token");
    assert!(
        token.is_file(),
        "the later committed write must retain its token"
    );
    assert_same_file(
        Path::new(client.inner.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"].as_str().unwrap()),
        &token,
    );
}

#[tokio::test]
async fn write_ack_restore_retry_survives_one_read_error() {
    retry_after_restore_ack(false, false).await;
}
#[tokio::test]
async fn write_ack_restore_retry_survives_cancelled_readback() {
    retry_after_restore_ack(true, false).await;
}
#[tokio::test]
async fn write_ack_restore_retry_still_refuses_a_competing_version() {
    retry_after_restore_ack(false, true).await;
}

async fn retry_after_restore_ack(cancel: bool, competing: bool) {
    struct Fault {
        inner: Fake,
        writes: usize,
        fail_read: bool,
        cancel: bool,
    }
    impl Rpc for Fault {
        async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
            if method == "config/read" && self.fail_read {
                self.fail_read = false;
                if self.cancel {
                    return std::future::pending().await;
                }
                return Err(SetupError(
                    "fixture one-shot read error after restore acknowledgement",
                ));
            }
            let result = self.inner.rpc(method, params).await;
            if method == "config/batchWrite" {
                self.writes += 1;
                if self.writes == 2 {
                    self.fail_read = true;
                }
            }
            result
        }
    }
    let home = Home::new();
    let mut client = Fault {
        inner: Fake::new(&home.0),
        writes: 0,
        fail_read: false,
        cancel,
    };
    let plan = plan_replace(
        &client.inner.config,
        &home.0,
        "https://example.com",
        None,
        true,
    )
    .unwrap();
    let mut applied = apply_replace(&mut client, &home.0, plan).await.unwrap();
    if cancel {
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(20),
                applied.rollback(&mut client, &home.0)
            )
            .await
            .is_err()
        );
    } else {
        assert!(applied.rollback(&mut client, &home.0).await.is_err());
    }
    assert_eq!(client.writes, 2);
    let token = home.0.join("pseudolife").join("token");
    let connection = home.0.join("pseudolife/connection.json");
    assert!(token.is_file() && connection.is_file());
    if competing {
        client.inner.config["layers"][0]["version"] = json!("competing-after-restoration");
        assert!(applied.rollback(&mut client, &home.0).await.is_err());
        assert!(token.is_file() && connection.is_file());
    } else {
        applied.rollback(&mut client, &home.0).await.unwrap();
        assert!(!token.exists() && !connection.exists());
    }
    assert_eq!(
        client.writes, 2,
        "retry verifies the acknowledged restore without a second write"
    );
}

#[tokio::test]
async fn lower_system_no_spawn_does_not_override_the_user_connection() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    apply_replace(&mut client, &home.0, plan).await.unwrap();
    client.config["layers"].as_array_mut().unwrap().push(json!({"name":{"type":"system","file":home.0.join("system.toml")},"version":"system-a","config":{"mcp_servers":{"pseudolife-memory":{"env":{"PSEUDOLIFE_MCP_NO_SPAWN":"0","PSEUDOLIFE_MCP_TOKEN":""}}}}}));
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    client.calls.clear();
    assert!(apply_replace(&mut client, &home.0, plan).await.is_ok());
    assert!(
        client
            .calls
            .iter()
            .all(|(method, _)| method != "config/batchWrite")
    );
    // Empty credentials have the same precedence rule: a lower system value
    // must not redirect a user-key repair to the system configuration.
    let env = &mut client.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"];
    env.as_object_mut()
        .unwrap()
        .remove("PSEUDOLIFE_MCP_TOKEN_FILE");
    env["PSEUDOLIFE_MCP_TOKEN"] = json!("");
    client.config["config"] = client.config["layers"][0]["config"].clone();
    let config = client.config.clone();
    let error = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &home.0,
        &home.0,
        Some(config),
        Some(("https://example.com", None)),
    )
    .await
    .unwrap_err()
    .to_string();
    assert!(error.contains("empty in Codex's user"));
    assert!(!error.contains("system.toml"));
}

#[tokio::test]
async fn static_project_no_spawn_refusal_precedes_token_and_config_writes() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.project_no_spawn = Some("0".into());
    client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"] =
        json!("0");
    client.config["layers"].as_array_mut().unwrap().insert(0, json!({"name":{"type":"project","dotCodexFolder":home.0.join("project")},"version":"project-a","config":{"mcp_servers":{"pseudolife-memory":{"env":{"PSEUDOLIFE_MCP_NO_SPAWN":"0"}}}}}));
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let error = apply_replace(&mut client, &home.0, plan)
        .await
        .err()
        .unwrap()
        .to_string();
    assert!(error.contains("project") && error.contains("PSEUDOLIFE_MCP_NO_SPAWN"));
    assert!(error.contains("remove that key"));
    assert!(!error.contains("changed during setup"));
    assert!(
        client
            .calls
            .iter()
            .all(|(method, _)| method != "config/batchWrite")
    );
    assert!(!home.0.join("pseudolife/token").exists());
    assert!(!home.0.join("pseudolife/connection.json").exists());
    assert!(!std::fs::read_dir(&home.0).unwrap().any(|entry| {
        entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .contains(".bak-")
    }));
}

#[tokio::test]
async fn unchanged_connection_still_checks_effective_no_spawn_before_success() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    apply_replace(&mut client, &home.0, plan).await.unwrap();
    let connection = home.0.join("pseudolife/connection.json");
    let before = std::fs::read(&connection).unwrap();
    let project = home.0.join("project").join("config.toml");
    for (value, named_layer) in [("0", true), ("0", false), ("true", true)] {
        client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"] =
            json!(value);
        client.config["layers"]
            .as_array_mut()
            .unwrap()
            .retain(|layer| layer["name"]["type"] == "user");
        if named_layer {
            client.config["layers"].as_array_mut().unwrap().insert(0, json!({"name":{"type":"project","dotCodexFolder":project.parent().unwrap()},"version":"project-a","config":{"mcp_servers":{"pseudolife-memory":{"env":{"PSEUDOLIFE_MCP_NO_SPAWN":value}}}}}));
        }
        let plan =
            plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
        assert_eq!(plan.env(), plan.previous_env());
        client.calls.clear();
        let result = apply_replace(&mut client, &home.0, plan).await;
        if value == "0" {
            let error = result
                .err()
                .expect("false effective no-spawn must refuse")
                .to_string();
            assert!(
                error.contains(if named_layer {
                    "project"
                } else {
                    "project or managed"
                }) && error.contains("PSEUDOLIFE_MCP_NO_SPAWN")
            );
            assert!(error.contains("remove that key"));
        } else {
            assert!(result.is_ok());
        }
        assert!(
            client
                .calls
                .iter()
                .all(|(method, _)| method != "config/batchWrite")
        );
        assert_eq!(std::fs::read(&connection).unwrap(), before);
    }
}

#[tokio::test]
async fn equal_empty_user_and_project_credentials_name_the_project_owner() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_TOKEN"] =
        json!("");
    client.config["config"] = client.config["layers"][0]["config"].clone();
    client.config["layers"].as_array_mut().unwrap().insert(0, json!({"name":{"type":"project","dotCodexFolder":home.0.join("project")},"version":"project-a","config":{"mcp_servers":{"pseudolife-memory":{"env":{"PSEUDOLIFE_MCP_TOKEN":""}}}}}));
    let config = client.config.clone();
    let error = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &home.0,
        &home.0,
        Some(config),
        Some(("http://localhost:8765", None)),
    )
    .await
    .unwrap_err()
    .to_string();
    assert!(error.contains("project") && error.contains("PSEUDOLIFE_MCP_TOKEN"));
    assert!(!error.contains("empty in Codex's user"));
    assert!(client.calls.is_empty());
    assert!(!home.0.join("pseudolife").exists());
}

#[test]
fn concurrent_first_lock_publication_is_private_and_never_replaces_the_winner() {
    use std::sync::{Arc, Condvar, Mutex, mpsc};
    use std::time::Duration;
    type Release = Arc<(Mutex<bool>, Condvar)>;
    struct Gated {
        inner: Fake,
        arrived: mpsc::Sender<()>,
        release: Release,
    }
    impl Rpc for Gated {
        async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
            if method == "config/batchWrite" {
                self.arrived.send(()).unwrap();
                let (lock, changed) = &*self.release;
                let (released, timeout) = changed
                    .wait_timeout_while(lock.lock().unwrap(), Duration::from_secs(10), |v| !*v)
                    .unwrap();
                assert!(
                    *released && !timeout.timed_out(),
                    "lock contenders did not finish in time"
                );
            }
            self.inner.rpc(method, params).await
        }
    }
    for _ in 0..8 {
        let home = Home::new();
        let release: Release = Arc::new((Mutex::new(false), Condvar::new()));
        let (arrived, arrivals) = mpsc::channel();
        let results = std::thread::scope(|scope| {
            let handles: Vec<_> = (0..8)
                .map(|_| {
                    let release = release.clone();
                    let arrived = arrived.clone();
                    let home = home.0.clone();
                    scope.spawn(move || {
                        let mut client = Gated {
                            inner: Fake::new(&home),
                            arrived: arrived.clone(),
                            release,
                        };
                        let plan = plan_registration(
                            &client.inner.config,
                            &home,
                            &home.join("pseudolife-mcp"),
                            &[],
                            &Map::new(),
                        )
                        .unwrap();
                        let runtime = tokio::runtime::Builder::new_current_thread()
                            .enable_all()
                            .build()
                            .unwrap();
                        let result = runtime.block_on(plan.apply(&mut client, &home));
                        if let Err(error) = &result {
                            arrived.send(()).unwrap();
                            assert_eq!(
                                error.to_string(),
                                "Another Codex setup is in progress; retry after it finishes."
                            );
                        }
                        result.is_ok()
                    })
                })
                .collect();
            drop(arrived);
            let all_arrived =
                (0..8).all(|_| arrivals.recv_timeout(Duration::from_secs(10)).is_ok());
            // Release even after a missing/panicked contender: scoped joins must
            // finish and expose the failure rather than hang at a barrier.
            *release.0.lock().unwrap() = true;
            release.1.notify_all();
            let results = handles
                .into_iter()
                .map(|handle| handle.join().unwrap())
                .collect::<Vec<_>>();
            assert!(
                all_arrived,
                "a lock contender failed before reporting its result"
            );
            results
        });
        assert_eq!(results.iter().filter(|won| **won).count(), 1);
        let lock = home.0.join(".pseudolife-setup.lock");
        let before = pseudolife_stdio::board::state::open(&lock)
            .unwrap()
            .identity;
        let mut client = Fake::new(&home.0);
        let plan = plan_registration(
            &client.config,
            &home.0,
            &home.0.join("pseudolife-mcp"),
            &[],
            &Map::new(),
        )
        .unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(plan.apply(&mut client, &home.0)).unwrap();
        assert_eq!(
            before,
            pseudolife_stdio::board::state::open(&lock)
                .unwrap()
                .identity
        );
    }
}

struct Home(std::path::PathBuf);
fn assert_same_file(left: &Path, right: &Path) {
    assert_eq!(
        std::fs::canonicalize(left).unwrap(),
        std::fs::canonicalize(right).unwrap()
    );
}
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("pseudolife-setup-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&path).unwrap();
        std::fs::write(path.join("config.toml"), "# foreign configuration\n").unwrap();
        Self(path)
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct Fake {
    config: Value,
    calls: Vec<(String, Value)>,
    reject: bool,
    override_env: bool,
    concurrent_env: bool,
    typed_extra: bool,
    rejected_after_competitor: bool,
    project_no_spawn: Option<String>,
    foreign_connection_after_write: bool,
    lost_write_reply: bool,
}

#[cfg(windows)]
#[allow(unsafe_code)] // Both calls use live, bounded UTF-16 buffers.
fn short_windows_path(path: &Path) -> std::path::PathBuf {
    use std::os::windows::ffi::{OsStrExt, OsStringExt};
    use windows_sys::Win32::Storage::FileSystem::GetShortPathNameW;
    let path: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let needed = unsafe { GetShortPathNameW(path.as_ptr(), std::ptr::null_mut(), 0) };
    assert_ne!(
        needed, 0,
        "GetShortPathNameW failed for the disposable fixture"
    );
    let mut output = vec![0; needed as usize];
    let written = unsafe { GetShortPathNameW(path.as_ptr(), output.as_mut_ptr(), needed) };
    assert!(
        written > 0 && written < needed,
        "GetShortPathNameW did not return a complete path"
    );
    std::ffi::OsString::from_wide(&output[..written as usize]).into()
}

#[cfg(windows)]
#[tokio::test]
async fn short_codex_home_accepts_safe_token_aliases_and_keeps_writes_scoped() {
    struct ShortReadback {
        inner: Fake,
        token: std::path::PathBuf,
    }
    impl Rpc for ShortReadback {
        async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
            let mut response = self.inner.rpc(method, params).await?;
            if method == "config/read" && self.token.exists() {
                for root in ["/config", "/layers/0/config"] {
                    let env = &mut response.pointer_mut(root).unwrap()["mcp_servers"]["pseudolife-memory"]
                        ["env"];
                    if env.get("PSEUDOLIFE_MCP_TOKEN_FILE").is_some() {
                        env["PSEUDOLIFE_MCP_TOKEN_FILE"] = json!(self.token);
                    }
                }
            }
            Ok(response)
        }
    }
    let home = Home::new();
    // Home's final component deliberately exceeds the 8.3 length limit.
    let short = short_windows_path(&home.0);
    assert_ne!(
        short.file_name(),
        home.0.file_name(),
        "the disposable test volume did not provide an 8.3 alias; this regression requires short-name creation enabled"
    );
    assert_same_file(&short, &home.0);
    let mut client = ShortReadback {
        inner: Fake::new(&short),
        token: short.join("pseudolife/token"),
    };
    let plan = plan_replace(
        &client.inner.config,
        &short,
        "https://example.com",
        None,
        true,
    )
    .unwrap();
    let requested = plan.env()["PSEUDOLIFE_MCP_TOKEN_FILE"].clone();
    assert_ne!(requested, json!(client.token));
    let mut applied = apply_replace(&mut client, &short, plan).await.unwrap();
    assert_same_file(Path::new(requested.as_str().unwrap()), &client.token);
    let write = &client
        .inner
        .calls
        .iter()
        .find(|(method, _)| method == "config/batchWrite")
        .unwrap()
        .1;
    assert_eq!(write["edits"].as_array().unwrap().len(), 1);
    assert_eq!(
        write["edits"][0]["keyPath"],
        "\"mcp_servers\".\"pseudolife-memory\".\"env\""
    );
    assert_eq!(
        write["edits"][0]["value"]["PSEUDOLIFE_MCP_TOKEN_FILE"],
        requested
    );

    // A different spelling in the effective layer has the same authority;
    // another spelling returned after planning also leaves the no-op valid.
    client.inner.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"] =
        json!(client.token);
    let plan = plan_replace(
        &client.inner.config,
        &short,
        "https://example.com",
        None,
        true,
    )
    .unwrap();
    let writes = client
        .inner
        .calls
        .iter()
        .filter(|(method, _)| method == "config/batchWrite")
        .count();
    apply_replace(&mut client, &short, plan).await.unwrap();
    let config = client.inner.config.clone();
    pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &short,
        &short,
        Some(config),
        None,
    )
    .await
    .unwrap();
    assert_eq!(
        client
            .inner
            .calls
            .iter()
            .filter(|(method, _)| method == "config/batchWrite")
            .count(),
        writes
    );
    applied.rollback(&mut client, &short).await.unwrap();
    assert!(!client.token.exists());
}

#[cfg(windows)]
#[test]
fn short_token_aliases_refuse_missing_unsafe_and_different_files() {
    let home = Home::new();
    let short = short_windows_path(&home.0);
    assert_ne!(
        short.file_name(),
        home.0.file_name(),
        "the disposable test volume did not provide an 8.3 alias; this regression requires short-name creation enabled"
    );
    let token = home.0.join("token");
    let alias = short.join("token");
    let mut config = config(&home.0);
    for root in ["/config", "/layers/0/config"] {
        let env = &mut config.pointer_mut(root).unwrap()["mcp_servers"]["pseudolife-memory"]["env"];
        env.as_object_mut().unwrap().remove("PSEUDOLIFE_MCP_TOKEN");
        env["PSEUDOLIFE_MCP_TOKEN_FILE"] = json!(token);
    }
    config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"] =
        json!(alias);
    assert!(plan_replace(&config, &short, "https://example.com", None, true).is_err());
    // A hard link makes both spellings unsafe for the private-file boundary.
    pseudolife_stdio::board::state::atomic_create(&token, b"fixture-token").unwrap();
    let other = home.0.join("other-token");
    std::fs::hard_link(&token, &other).unwrap();
    assert!(plan_replace(&config, &short, "https://example.com", None, true).is_err());
    std::fs::remove_file(&other).unwrap();
    pseudolife_stdio::board::state::atomic_create(&other, b"fixture-token").unwrap();
    config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"] =
        json!(other);
    assert!(plan_replace(&config, &short, "https://example.com", None, true).is_err());
}

#[cfg(windows)]
#[test]
fn config_path_uses_ordinary_windows_spelling_when_equivalent() {
    let home = Home::new();
    let user = user_config(&config(&home.0), &home.0).unwrap();
    assert!(!user.path.to_string_lossy().starts_with(r"\\?\"));
    assert_eq!(
        std::fs::canonicalize(user.path).unwrap(),
        std::fs::canonicalize(home.0.join("config.toml")).unwrap()
    );
}

#[cfg(windows)]
#[test]
fn config_path_retains_verbatim_spelling_for_long_paths() {
    let home = Home::new();
    let long = home.0.join("a".repeat(120)).join("b".repeat(120));
    std::fs::create_dir_all(&long).unwrap();
    std::fs::write(long.join("config.toml"), "# fixture\n").unwrap();
    let user = user_config(&config(&long), &long).unwrap();
    assert!(user.path.to_string_lossy().starts_with(r"\\?\"));
    assert_eq!(
        std::fs::canonicalize(user.path).unwrap(),
        std::fs::canonicalize(long.join("config.toml")).unwrap()
    );
}

#[cfg(windows)]
#[tokio::test]
async fn junction_codex_home_writes_resolved_token_files() {
    use std::os::windows::process::CommandExt;
    let home = Home::new();
    let real = home.0.join("real-home");
    let linked = home.0.join("junction-home");
    std::fs::create_dir(&real).unwrap();
    std::fs::write(real.join("config.toml"), "# fixture\n").unwrap();
    let result = std::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command",
            "New-Item -ItemType Junction -Path $env:PSEUDOLIFE_TEST_LINK -Target $env:PSEUDOLIFE_TEST_TARGET | Out-Null"])
        .env("PSEUDOLIFE_TEST_LINK", &linked)
        .env("PSEUDOLIFE_TEST_TARGET", &real)
        .stdin(std::process::Stdio::null())
        .creation_flags(0x08000000)
        .output().unwrap();
    assert!(
        result.status.success(),
        "disposable junction could not be created"
    );
    let mut client = Fake::new(&linked);
    let report = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &linked,
        &linked,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(report["credential_file_configured"], true);
    assert_eq!(
        std::fs::canonicalize(report["credential_file_path"].as_str().unwrap()).unwrap(),
        std::fs::canonicalize(real.join("pseudolife/token")).unwrap()
    );
    let link_metadata = std::fs::symlink_metadata(&linked).unwrap();
    use std::os::windows::fs::MetadataExt;
    assert_ne!(link_metadata.file_attributes() & 0x400, 0);
    // Remove just the link, leaving the disposable target to Home's cleanup.
    std::fs::remove_dir(&linked).unwrap();
}

#[tokio::test]
async fn isolated_credential_bootstrap_fixture() {
    if std::env::var_os("PSEUDOLIFE_TEST_CREDENTIAL_HOME").is_none() {
        return;
    }
    let home = std::path::PathBuf::from(std::env::var_os("CODEX_HOME").unwrap());
    std::fs::create_dir_all(&home).unwrap();
    std::fs::write(home.join("config.toml"), "# fixture\n").unwrap();
    let token = home.parent().unwrap().join("credential");
    pseudolife_stdio::board::state::atomic_create(&token, b"fixture-token").unwrap();
    let mut client = Fake::new(&home);
    for root in ["/config", "/layers/0/config"] {
        let env =
            client.config.pointer_mut(root).unwrap()["mcp_servers"]["pseudolife-memory"]["env"]
                .as_object_mut()
                .unwrap();
        env.remove("PSEUDOLIFE_MCP_TOKEN");
        env.insert("PSEUDOLIFE_MCP_TOKEN_FILE".into(), json!("~/credential"));
    }
    let report = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &home,
        &home,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(report["credential_file_path"], json!(token));
}

async fn isolated_fixture(name: &str, mode: &str) {
    use process_wrap::tokio::{CommandWrap, KillOnDrop};
    let home = Home::new();
    let mut command = tokio::process::Command::new(std::env::current_exe().unwrap());
    command
        .args(["--exact", name, "--nocapture"])
        .env_clear()
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0)
        .env("CODEX_HOME", home.0.join(".codex"))
        .env("LOCALAPPDATA", home.0.join("local"))
        .env("APPDATA", home.0.join("roaming"))
        .env("PSEUDOLIFE_TEST_CREDENTIAL_HOME", mode)
        .env("PATH", &home.0)
        .env("PATHEXT", ".CMD;.EXE")
        .current_dir(&home.0)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    if mode.starts_with("no-spawn-") {
        command.env(
            "PSEUDOLIFE_MCP_NO_SPAWN",
            if mode.ends_with("true") { "yes" } else { "0" },
        );
    }
    for key in ["SYSTEMROOT", "WINDIR"] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    let mut command = CommandWrap::from(command);
    command.wrap(KillOnDrop);
    #[cfg(windows)]
    command.wrap(process_wrap::tokio::JobObject);
    #[cfg(unix)]
    command.wrap(process_wrap::tokio::ProcessGroup::leader());
    let child = command.spawn().unwrap();
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(20),
        Box::into_pin(child.wait_with_output()),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(
        result.status.success(),
        "isolated fixture failed: {}",
        String::from_utf8_lossy(&result.stdout)
    );
}

#[tokio::test]
async fn credential_bootstrap_expands_home_relative_source() {
    isolated_fixture("isolated_credential_bootstrap_fixture", "tilde").await;
}

#[cfg(windows)]
#[tokio::test]
async fn isolated_cmd_launcher_fixture() {
    if std::env::var_os("PSEUDOLIFE_TEST_CREDENTIAL_HOME").is_none() {
        return;
    }
    let home = std::path::PathBuf::from(std::env::var_os("HOME").unwrap());
    let wrapper = home.join("codex.cmd");
    std::fs::write(
        &wrapper,
        format!(
            "@\"{}\" --exact rpc_fixture --nocapture\r\n",
            std::env::current_exe().unwrap().display()
        ),
    )
    .unwrap();
    let executable = pseudolife_stdio::setup::codex::resolve_codex().unwrap();
    assert_eq!(executable, wrapper);
    let mut client = pseudolife_stdio::setup::codex::Client::open_with_environment(
        &executable,
        &home,
        &home,
        &json!({}),
        &[("PSEUDOLIFE_TEST_CODEX_RPC".into(), Some("reply".into()))],
    )
    .await
    .unwrap();
    assert_eq!(
        client.rpc("config/read", json!({})).await.unwrap(),
        json!({"received":{}})
    );
    client.close().await;
}

#[cfg(windows)]
#[tokio::test]
async fn npm_cmd_launcher_is_discovered_and_started() {
    isolated_fixture("isolated_cmd_launcher_fixture", "cmd").await;
}
impl Fake {
    fn new(home: &Path) -> Self {
        Self {
            config: config(home),
            calls: vec![],
            reject: false,
            override_env: false,
            concurrent_env: false,
            typed_extra: false,
            rejected_after_competitor: false,
            project_no_spawn: None,
            foreign_connection_after_write: false,
            lost_write_reply: false,
        }
    }
}
impl Rpc for Fake {
    async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
        self.calls.push((method.to_owned(), params.clone()));
        let user = self.config["layers"]
            .as_array()
            .unwrap()
            .iter()
            .position(|layer| layer["name"]["type"] == "user")
            .unwrap();
        if method == "config/batchWrite" {
            if self.reject {
                return Err(SetupError::rejected("fixture refusal"));
            }
            assert_eq!(
                params["expectedVersion"],
                self.config["layers"][user]["version"]
            );
            for edit in params["edits"].as_array().unwrap() {
                if edit["keyPath"] == "\"mcp_servers\".\"pseudolife-memory\"" {
                    for root in ["/config".to_owned(), format!("/layers/{user}/config")] {
                        self.config.pointer_mut(&root).unwrap()["mcp_servers"]["pseudolife-memory"] =
                            edit["value"].clone();
                    }
                    continue;
                }
                assert_eq!(
                    edit["keyPath"],
                    "\"mcp_servers\".\"pseudolife-memory\".\"env\""
                );
                self.config["layers"][user]["config"]["mcp_servers"]["pseudolife-memory"]["env"] =
                    edit["value"].clone();
                self.config["config"]["mcp_servers"]["pseudolife-memory"]["env"] =
                    edit["value"].clone();
            }
            self.config["layers"][user]["version"] = json!(format!("version-{}", self.calls.len()));
            if self.lost_write_reply {
                self.lost_write_reply = false;
                return Err(SetupError("fixture lost write acknowledgement"));
            }
            if self.rejected_after_competitor {
                self.rejected_after_competitor = false;
                let file = self.config["layers"][user]["name"]["file"]
                    .as_str()
                    .unwrap();
                let connection = Path::new(file)
                    .parent()
                    .unwrap()
                    .join("pseudolife")
                    .join("connection.json");
                pseudolife_stdio::board::state::atomic_create(
                    &connection,
                    b"winner connection refers to the shared token",
                )
                .unwrap();
                return Err(SetupError(
                    "fixture expectedVersion rejection after another writer won",
                ));
            }
            if self.override_env {
                self.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_DAEMON_URL"] =
                    json!("https://override.example.com");
            }
            if self.concurrent_env {
                self.config["layers"][user]["config"]["mcp_servers"]["pseudolife-memory"]["env"]
                    ["UNRELATED"] = json!("concurrent change");
            }
            if self.typed_extra {
                self.config["config"]["mcp_servers"]["pseudolife-memory"]["enabled"] = json!(true);
            }
            if let Some(value) = &self.project_no_spawn {
                self.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"] =
                    json!(value);
            }
            if self.foreign_connection_after_write {
                self.foreign_connection_after_write = false;
                let home = Path::new(
                    self.config["layers"][user]["name"]["file"]
                        .as_str()
                        .unwrap(),
                )
                .parent()
                .unwrap();
                pseudolife_stdio::board::state::atomic_create(
                    &home.join("pseudolife/connection.json"),
                    b"foreign malformed connection",
                )
                .unwrap();
            }
            return Ok(json!({"version":self.config["layers"][user]["version"]}));
        }
        assert_eq!(method, "config/read");
        Ok(self.config.clone())
    }
}

#[tokio::test]
async fn rejected_cas_never_undoes_a_competitor_or_deletes_its_adopted_token() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let expected = plan.env().clone();
    client.rejected_after_competitor = true;
    let error = apply_replace(&mut client, &home.0, plan)
        .await
        .err()
        .unwrap();
    assert!(
        error
            .to_string()
            .contains("changed while the write outcome was unconfirmed")
    );
    assert_eq!(
        client.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"],
        json!(expected)
    );
    assert!(home.0.join("pseudolife").join("token").is_file());
    assert_eq!(
        std::fs::read(home.0.join("pseudolife").join("connection.json")).unwrap(),
        b"winner connection refers to the shared token"
    );
    assert_eq!(
        client
            .calls
            .iter()
            .filter(|(method, _)| method == "config/batchWrite")
            .count(),
        1
    );
}

#[tokio::test]
async fn detailed_readback_failure_reports_only_completed_compensation() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.override_env = true;
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let failure =
        pseudolife_stdio::setup::codex::apply_replace_detailed(&mut client, &home.0, plan)
            .await
            .err()
            .unwrap();
    assert!(!failure.recovery_pending);
    assert!(failure.recovery.is_none());
    assert!(Path::new(failure.report["backup"].as_str().unwrap()).is_file());
    assert_eq!(failure.report["created"], json!([]));
    assert_eq!(failure.rollback.len(), 2);
    assert_eq!(failure.rollback[0]["state"], "restored");
    assert_eq!(failure.rollback[1]["state"], "removed");
    assert!(!home.0.join("pseudolife/token").exists());
    assert!(!format!("{failure:?} {}", failure.report).contains("fixture-token"));
}

#[tokio::test]
async fn detailed_rejected_write_does_not_claim_a_config_restore() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.reject = true;
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let failure =
        pseudolife_stdio::setup::codex::apply_replace_detailed(&mut client, &home.0, plan)
            .await
            .err()
            .unwrap();
    assert!(!failure.recovery_pending);
    assert_eq!(failure.rollback.len(), 1);
    assert_eq!(failure.rollback[0]["state"], "removed");
    assert!(Path::new(failure.report["backup"].as_str().unwrap()).is_file());
}

#[tokio::test]
async fn detailed_unknown_write_has_no_recovery_capability() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.rejected_after_competitor = true;
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let failure =
        pseudolife_stdio::setup::codex::apply_replace_detailed(&mut client, &home.0, plan)
            .await
            .err()
            .unwrap();
    assert!(failure.recovery_pending);
    assert!(failure.recovery.is_none());
    assert!(
        failure
            .rollback
            .iter()
            .all(|event| event["state"] == "left")
    );
    assert!(home.0.join("pseudolife/token").is_file());
}

#[tokio::test]
async fn detailed_lost_reply_without_connection_does_not_authorize_token_cleanup() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.lost_write_reply = true;
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let expected = plan.env().clone();
    let failure =
        pseudolife_stdio::setup::codex::apply_replace_detailed(&mut client, &home.0, plan)
            .await
            .err()
            .unwrap();
    assert!(failure.recovery_pending);
    assert!(failure.recovery.is_none());
    assert!(home.0.join("pseudolife/token").is_file());
    assert!(!home.0.join("pseudolife/connection.json").exists());
    assert_eq!(
        client.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"],
        json!(expected)
    );
}

#[tokio::test]
async fn detailed_known_write_keeps_fenced_recovery_after_rollback_conflict() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.override_env = true;
    client.concurrent_env = true;
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let mut failure =
        pseudolife_stdio::setup::codex::apply_replace_detailed(&mut client, &home.0, plan)
            .await
            .err()
            .unwrap();
    assert!(failure.recovery_pending);
    let before = client.config.clone();
    assert!(
        failure
            .recovery
            .as_mut()
            .unwrap()
            .rollback(&mut client, &home.0)
            .await
            .is_err()
    );
    assert_eq!(client.config, before);
    assert!(home.0.join("pseudolife/token").is_file());
    assert!(
        failure.rollback[0]["detail"]
            .as_str()
            .unwrap()
            .contains("another writer")
    );
}

#[tokio::test]
async fn detailed_connection_failure_preserves_a_foreign_file_and_its_possible_token() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.foreign_connection_after_write = true;
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let failure =
        pseudolife_stdio::setup::codex::apply_replace_detailed(&mut client, &home.0, plan)
            .await
            .err()
            .unwrap();
    assert!(failure.recovery_pending);
    assert!(failure.recovery.is_none());
    assert_eq!(failure.rollback[0]["state"], "restored");
    assert_eq!(failure.rollback[1]["state"], "left");
    assert_eq!(failure.rollback[2]["state"], "left");
    assert_eq!(
        std::fs::read(home.0.join("pseudolife/connection.json")).unwrap(),
        b"foreign malformed connection"
    );
    assert!(home.0.join("pseudolife/token").is_file());
    assert_eq!(
        failure.reason.to_string(),
        "The managed Codex connection file is unsafe or malformed; repair or remove it before retrying."
    );
}

#[tokio::test]
async fn detailed_later_rollback_reports_completed_events_and_safe_rerun() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let mut applied = apply_replace(&mut client, &home.0, plan).await.unwrap();
    let events = applied
        .rollback_detailed(&mut client, &home.0)
        .await
        .unwrap();
    assert_eq!(events.len(), 3);
    assert_eq!(events[0]["state"], "restored");
    assert_eq!(events[1]["state"], "removed");
    assert_eq!(events[2]["state"], "removed");
    assert!(!home.0.join("pseudolife/token").exists());
    assert!(!home.0.join("pseudolife/connection.json").exists());
    assert!(
        applied
            .rollback_detailed(&mut client, &home.0)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn detailed_later_rollback_reports_conflict_without_fabricated_file_restores() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let mut applied = apply_replace(&mut client, &home.0, plan).await.unwrap();
    client.config["layers"][0]["version"] = json!("competing-version");
    let failure = applied
        .rollback_detailed(&mut client, &home.0)
        .await
        .unwrap_err();
    assert!(failure.recovery_pending);
    assert!(failure.recovery.is_none());
    assert_eq!(failure.rollback.len(), 1);
    assert_eq!(failure.rollback[0]["state"], "left");
    assert!(home.0.join("pseudolife/token").is_file());
    assert!(home.0.join("pseudolife/connection.json").is_file());
}

#[tokio::test]
async fn recovery_retry_preserves_token_adopted_after_config_compensation() {
    recovery_after_token_conflict(true).await;
}

#[tokio::test]
async fn recovery_retry_finishes_owned_token_when_compensated_config_is_unchanged() {
    recovery_after_token_conflict(false).await;
}

async fn recovery_after_token_conflict(competing_config: bool) {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let expected = plan.env().clone();
    let mut applied = apply_replace(&mut client, &home.0, plan).await.unwrap();
    let token = home.0.join("pseudolife").join("token");
    let retained = home.0.join("pseudolife").join("retained-token");
    std::fs::rename(&token, &retained).unwrap();
    pseudolife_stdio::board::state::atomic_create(&token, b"fixture-token").unwrap();
    let failure = applied
        .rollback_detailed(&mut client, &home.0)
        .await
        .unwrap_err();
    assert!(failure.recovery_pending);
    assert_eq!(
        failure
            .rollback
            .iter()
            .map(|event| event["state"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["restored", "removed", "left"]
    );
    // Restore the exact owned inode: an identity refusal alone cannot make
    // this test pass. Only the configuration fence protects its new consumer.
    std::fs::remove_file(&token).unwrap();
    std::fs::rename(&retained, &token).unwrap();
    if competing_config {
        client.config["layers"][0]["version"] = json!("another-connection");
        for root in ["/config", "/layers/0/config"] {
            client.config.pointer_mut(root).unwrap()["mcp_servers"]["pseudolife-memory"]["env"] =
                json!(expected);
        }
        let before = client.config.clone();
        let failure = applied
            .rollback_detailed(&mut client, &home.0)
            .await
            .unwrap_err();
        assert!(failure.recovery_pending);
        assert_eq!(failure.rollback.len(), 1);
        assert_eq!(
            failure.rollback[0]["file"],
            json!(home.0.join("config.toml"))
        );
        assert_eq!(failure.rollback[0]["state"], "left");
        assert_eq!(client.config, before);
        assert!(token.is_file());
    } else {
        let events = applied
            .rollback_detailed(&mut client, &home.0)
            .await
            .unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["file"], json!(token));
        assert_eq!(events[0]["state"], "removed");
        assert!(!token.exists());
    }
}

#[tokio::test]
async fn bootstrap_refuses_project_forwarded_file_before_migrating_user_literal() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.config["config"]["mcp_servers"]["pseudolife-memory"]["env_vars"] =
        json!(["PSEUDOLIFE_MCP_TOKEN_FILE"]);
    let error = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &home.0,
        &home.0,
        None,
        None,
    )
    .await
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("No connection settings were changed")
    );
    assert!(!home.0.join("pseudolife").join("token").exists());
    assert!(
        !client
            .calls
            .iter()
            .any(|(method, _)| method == "config/batchWrite")
    );
}

#[test]
fn isolated_forwarded_no_spawn_fixture() {
    let Ok(mode) = std::env::var("PSEUDOLIFE_TEST_CREDENTIAL_HOME") else {
        return;
    };
    if !mode.starts_with("no-spawn-") {
        return;
    }
    let home = Home::new();
    let mut config = config(&home.0);
    config["config"]["mcp_servers"]["pseudolife-memory"]["env_vars"] =
        json!(["PSEUDOLIFE_MCP_NO_SPAWN"]);
    if mode.contains("user") {
        config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env_vars"] =
            json!(["PSEUDOLIFE_MCP_NO_SPAWN"]);
    }
    let result = plan_replace(&config, &home.0, "https://example.com", None, true);
    assert_eq!(result.unwrap().env()["PSEUDOLIFE_MCP_NO_SPAWN"], "1");
}

#[tokio::test]
async fn replace_persists_no_spawn_over_project_forwarding() {
    isolated_fixture(
        "isolated_forwarded_no_spawn_fixture",
        "no-spawn-project-false",
    )
    .await;
}

#[tokio::test]
async fn replace_accepts_user_owned_forwarded_no_spawn() {
    isolated_fixture("isolated_forwarded_no_spawn_fixture", "no-spawn-user-false").await;
}

#[tokio::test]
async fn replace_persists_no_spawn_despite_installer_only_forwarded_truth() {
    isolated_fixture(
        "isolated_forwarded_no_spawn_fixture",
        "no-spawn-project-true",
    )
    .await;
}

#[test]
fn replace_persists_no_spawn_despite_truthy_project_value() {
    let home = Home::new();
    for value in ["1", "true", "yes", " ON "] {
        let mut config = config(&home.0);
        config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"] =
            json!(value);
        let plan = plan_replace(&config, &home.0, "https://example.com", None, true).unwrap();
        assert_eq!(plan.env()["PSEUDOLIFE_MCP_NO_SPAWN"], "1");
    }
}

#[tokio::test]
async fn replace_updates_user_false_under_truthy_project_no_spawn() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"] =
        json!("0");
    client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"] =
        json!("yes");
    client.project_no_spawn = Some("yes".into());
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    assert_eq!(plan.env()["PSEUDOLIFE_MCP_NO_SPAWN"], "1");
    apply_replace(&mut client, &home.0, plan).await.unwrap();
    assert_eq!(
        client.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"],
        "1"
    );
    assert_eq!(
        client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_NO_SPAWN"],
        "yes"
    );
}

#[tokio::test]
async fn bootstrap_authority_error_identifies_project_file_and_key() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let file = home.0.join("project").join("config.toml");
    let settings =
        json!({"mcp_servers":{"pseudolife-memory":{"env_vars":["PSEUDOLIFE_MCP_TOKEN_FILE"]}}});
    client.config["layers"].as_array_mut().unwrap().insert(
        0,
        json!({
            "name":{"type":"project","dotCodexFolder":file.parent().unwrap()},"config":settings
        }),
    );
    client.config["config"]["mcp_servers"]["pseudolife-memory"]["env_vars"] =
        json!(["PSEUDOLIFE_MCP_TOKEN_FILE"]);
    let error = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &home.0,
        &home.0,
        None,
        None,
    )
    .await
    .unwrap_err()
    .to_string();
    assert!(error.contains(&format!("{:?}", file)));
    assert!(error.contains("PSEUDOLIFE_MCP_TOKEN_FILE"));
    assert!(error.contains("remove"));
    assert!(!error.contains("fixture-token"));
    assert!(!home.0.join("pseudolife").exists());
}

#[tokio::test]
async fn bootstrap_accepts_the_same_forwarded_key_in_user_layer() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    for root in ["/config", "/layers/0/config"] {
        client.config.pointer_mut(root).unwrap()["mcp_servers"]["pseudolife-memory"]["env_vars"] =
            json!(["PSEUDOLIFE_MCP_TOKEN_FILE"]);
    }
    let report = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &home.0,
        &home.0,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(report["credential_file_configured"], true);
}

#[tokio::test]
async fn linked_codex_home_resolves_before_private_writes() {
    let home = Home::new();
    let linked = home.0.join("linked-home");
    let real = home.0.join("real-home");
    std::fs::create_dir(&real).unwrap();
    std::fs::write(real.join("config.toml"), "# fixture\n").unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&real, &linked).unwrap();
    #[cfg(windows)]
    if let Err(error) = std::os::windows::fs::symlink_dir(&real, &linked) {
        eprintln!("linked home fixture unavailable: {:?}", error.kind());
        return;
    }
    let mut client = Fake::new(&linked);
    let plan = plan_replace(&client.config, &linked, "https://example.com", None, true).unwrap();
    let mut applied = apply_replace(&mut client, &linked, plan).await.unwrap();
    assert!(real.join("pseudolife/token").is_file());
    applied.rollback(&mut client, &linked).await.unwrap();
    let report = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &linked,
        &linked,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(report["credential_file_configured"], true);
    assert!(real.join("pseudolife/token").is_file());
}

#[tokio::test]
async fn concurrent_connection_setups_preserve_the_winners_credential() {
    struct Pausing {
        inner: Fake,
        reached: Option<tokio::sync::oneshot::Sender<()>>,
        release: Option<tokio::sync::oneshot::Receiver<()>>,
    }
    impl Rpc for Pausing {
        async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
            if method == "config/batchWrite"
                && let Some(reached) = self.reached.take()
            {
                reached.send(()).unwrap();
                self.release.take().unwrap().await.unwrap();
            }
            self.inner.rpc(method, params).await
        }
    }
    let home = Home::new();
    let (reached, ready) = tokio::sync::oneshot::channel();
    let (release, continue_write) = tokio::sync::oneshot::channel();
    let mut first = Pausing {
        inner: Fake::new(&home.0),
        reached: Some(reached),
        release: Some(continue_write),
    };
    let mut second = Fake::new(&home.0);
    let first_plan = plan_replace(
        &first.inner.config,
        &home.0,
        "https://example.com",
        None,
        true,
    )
    .unwrap();
    let second_plan =
        plan_replace(&second.config, &home.0, "https://example.com", None, true).unwrap();
    let competing = async {
        ready.await.unwrap();
        let rejected = apply_replace(&mut second, &home.0, second_plan)
            .await
            .is_err();
        release.send(()).unwrap();
        assert!(rejected);
        assert!(second.calls.is_empty());
    };
    let (winner, ()) = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        tokio::join!(apply_replace(&mut first, &home.0, first_plan), competing)
    })
    .await
    .unwrap();
    let mut winner = winner.unwrap();
    assert!(home.0.join("pseudolife").join("token").exists());
    assert!(
        read_connection(&home.0.join("pseudolife").join("connection.json"))
            .unwrap()
            .is_some()
    );
    winner.rollback(&mut first, &home.0).await.unwrap();
    assert!(!home.0.join("pseudolife").join("token").exists());
}

fn config(home: &Path) -> Value {
    let server = json!({"command":"pseudolife-mcp", "env": {
        "PSEUDOLIFE_MCP_DAEMON_URL":"http://localhost:8765",
        "PSEUDOLIFE_MCP_TOKEN":"fixture-token", "UNRELATED":"keep"
    }});
    let values = json!({"mcp_servers":{"pseudolife-memory":server,"foreign":{"command":"other"}}});
    json!({"config": values, "layers":[{"name":{"type":"user","file":home.join("config.toml")},
        "version":"version-a", "config":values}]})
}

#[test]
fn replace_preserves_foreign_env_and_migrates_literal() {
    let home = std::env::temp_dir().join("pseudolife-codex-plan-only");
    let original = config(&home);
    let plan = plan_replace(&original, &home, "https://example.com/", None, true).unwrap();
    assert_eq!(plan.env()["UNRELATED"], "keep");
    assert_eq!(
        plan.env()["PSEUDOLIFE_MCP_DAEMON_URL"],
        "https://example.com"
    );
    assert_eq!(plan.env()["PSEUDOLIFE_MCP_NO_SPAWN"], "1");
    assert!(plan.env().get("PSEUDOLIFE_MCP_TOKEN").is_none());
    assert_eq!(
        plan.env()["PSEUDOLIFE_MCP_TOKEN_FILE"],
        user_config(&original, &home)
            .unwrap()
            .path
            .parent()
            .unwrap()
            .join("pseudolife")
            .join("token")
            .to_str()
            .unwrap()
    );
    assert_eq!(original, config(&home));
}

#[test]
fn replace_refuses_layer_and_forwarded_credential_authority() {
    let home = std::env::temp_dir().join("pseudolife-codex-plan-only");
    let mut original = config(&home);
    original["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_TOKEN"] =
        json!("project-token");
    assert!(plan_replace(&original, &home, "https://example.com", None, false).is_err());
    let mut original = config(&home);
    original["config"]["mcp_servers"]["pseudolife-memory"]["env_vars"] =
        json!(["PSEUDOLIFE_MCP_TOKEN_FILE"]);
    assert!(plan_replace(&original, &home, "https://example.com", None, false).is_err());
}

#[test]
fn user_layer_requires_unique_versioned_nonprofile_authority() {
    let home = std::env::temp_dir().join("pseudolife-codex-plan-only");
    let mut original = config(&home);
    assert!(user_config(&original, &home).is_ok());
    original["layers"][0]["name"]["profile"] = json!("work");
    assert!(user_config(&original, &home).is_err());
    let mut original = config(&home);
    original["layers"][0]["version"] = json!("");
    assert!(user_config(&original, &home).is_err());
    let mut original = config(&home);
    let duplicate = original["layers"][0].clone();
    original["layers"].as_array_mut().unwrap().push(duplicate);
    assert!(user_config(&original, &home).is_err());
}

#[test]
fn token_file_wins_and_explicit_no_spawn_is_preserved() {
    let home = std::env::temp_dir().join("pseudolife-codex-plan-only");
    let mut original = config(&home);
    for root in ["/config", "/layers/0/config"] {
        let env = original
            .pointer_mut(&format!("{root}/mcp_servers/pseudolife-memory/env"))
            .unwrap();
        env["PSEUDOLIFE_MCP_TOKEN_FILE"] = json!(home.join("external-token"));
        env["PSEUDOLIFE_MCP_NO_SPAWN"] = json!("yes");
    }
    let plan = plan_replace(&original, &home, "https://example.com", None, true).unwrap();
    assert_eq!(plan.env()["PSEUDOLIFE_MCP_NO_SPAWN"], "yes");
    assert_eq!(
        plan.env()["PSEUDOLIFE_MCP_TOKEN_FILE"],
        home.join("external-token").to_str().unwrap()
    );
    assert!(plan.env().get("PSEUDOLIFE_MCP_TOKEN").is_none());
}

#[tokio::test]
async fn apply_has_private_backup_token_connection_and_quiet_rerun() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let applied = apply_replace(&mut client, &home.0, plan).await.unwrap();
    let report = applied.report();
    let backup = Path::new(report["backup"].as_str().unwrap());
    assert_eq!(
        pseudolife_stdio::board::state::read(backup, 4096).unwrap(),
        b"# foreign configuration\n"
    );
    assert!(!report.to_string().contains("fixture-token"));
    let token = home.0.join("pseudolife").join("token");
    assert!(
        pseudolife_stdio::credentials::CredentialProvider::new(None, Some(token.clone()))
            .unwrap()
            .snapshot()
            .unwrap()
            .token()
            == Some("fixture-token")
    );
    let connection = home.0.join("pseudolife").join("connection.json");
    let (url, saved_token) = read_connection(&connection).unwrap().unwrap();
    assert_eq!(url, "https://example.com");
    assert_same_file(Path::new(&saved_token), &token);
    assert_eq!(
        std::fs::read_to_string(&connection)
            .unwrap()
            .lines()
            .count(),
        5
    );
    let before = client
        .calls
        .iter()
        .filter(|(method, _)| method == "config/batchWrite")
        .count();
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let rerun = apply_replace(&mut client, &home.0, plan).await.unwrap();
    assert_eq!(
        client
            .calls
            .iter()
            .filter(|(method, _)| method == "config/batchWrite")
            .count(),
        before
    );
    assert!(rerun.report()["changed"].as_array().unwrap().is_empty());
    assert!(rerun.report()["connection_backup"].is_null());
}

#[tokio::test]
async fn unsafe_connection_refuses_before_token_or_rpc_write() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let connection = home.0.join("pseudolife").join("connection.json");
    std::fs::create_dir_all(connection.parent().unwrap()).unwrap();
    pseudolife_stdio::board::state::atomic_create(&connection, b"not JSON").unwrap();
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    assert!(apply_replace(&mut client, &home.0, plan).await.is_err());
    assert!(client.calls.is_empty());
    assert!(!home.0.join("pseudolife").join("token").exists());
    assert_eq!(std::fs::read(&connection).unwrap(), b"not JSON");
}

#[tokio::test]
async fn refused_rpc_rolls_back_new_token_and_keeps_old_configuration() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.reject = true;
    let old = client.config.clone();
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    assert!(apply_replace(&mut client, &home.0, plan).await.is_err());
    assert_eq!(client.config, old);
    assert!(!home.0.join("pseudolife").join("token").exists());
    assert!(!home.0.join("pseudolife").join("connection.json").exists());
}

#[tokio::test]
async fn failed_effective_verification_rolls_back_with_fresh_version() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.override_env = true;
    let old = client.config["layers"][0]["config"].clone();
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    assert!(apply_replace(&mut client, &home.0, plan).await.is_err());
    assert_eq!(client.config["layers"][0]["config"], old);
    let writes = client
        .calls
        .iter()
        .filter(|(m, _)| m == "config/batchWrite")
        .collect::<Vec<_>>();
    assert_eq!(writes.len(), 2);
    assert_ne!(
        writes[0].1["expectedVersion"],
        writes[1].1["expectedVersion"]
    );
    assert!(!home.0.join("pseudolife").join("token").exists());
}

#[tokio::test]
async fn rollback_preserves_intervening_edit() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let mut applied = apply_replace(&mut client, &home.0, plan).await.unwrap();
    client.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"]["UNRELATED"] =
        json!("concurrent change");
    assert!(applied.rollback(&mut client, &home.0).await.is_err());
    assert_eq!(
        client.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"]["UNRELATED"],
        "concurrent change"
    );
    assert!(home.0.join("pseudolife").join("token").exists());
}

#[test]
fn rpc_fixture() {
    use std::io::{BufRead, Write};
    let Ok(mode) = std::env::var("PSEUDOLIFE_TEST_CODEX_RPC") else {
        return;
    };
    for line in std::io::stdin().lock().lines() {
        let value: Value = serde_json::from_str(&line.unwrap()).unwrap();
        let Some(id) = value.get("id") else {
            continue;
        };
        let result = if value["method"] == "initialize" {
            json!({})
        } else {
            match mode.as_str() {
                "timeout" => {
                    std::thread::sleep(std::time::Duration::from_secs(30));
                    json!({})
                }
                "exit" => std::process::exit(0),
                "large" => json!({"padding":"x".repeat(1_500_000)}),
                "reject" => {
                    println!(
                        "{}",
                        json!({"id":id,"error":{"code":-32600,"message":"fixture-secret-not-a-diagnostic"}})
                    );
                    std::io::stdout().flush().unwrap();
                    continue;
                }
                _ => json!({"received":value["params"]}),
            }
        };
        println!("{}", json!({"method":"notification","params":{}}));
        println!("{}", json!({"id":id,"result":result}));
        std::io::stdout().flush().unwrap();
    }
}

async fn fixture_client(home: &Path, mode: &str) -> pseudolife_stdio::setup::codex::Client {
    let mut command = tokio::process::Command::new(std::env::current_exe().unwrap());
    command
        .args(["--exact", "rpc_fixture", "--nocapture"])
        .env_clear()
        .env("PSEUDOLIFE_TEST_CODEX_RPC", mode)
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("CODEX_HOME", home)
        .current_dir(home);
    for key in ["SYSTEMROOT", "WINDIR"] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    pseudolife_stdio::setup::codex::Client::spawn(command, std::time::Duration::from_secs(3))
        .await
        .unwrap()
}

#[tokio::test]
async fn rpc_matches_ids_ignores_notifications_and_closes_child() {
    let home = Home::new();
    let mut client = fixture_client(&home.0, "reply").await;
    assert_eq!(
        client
            .rpc("config/read", json!({"includeLayers":true}))
            .await
            .unwrap(),
        json!({"received":{"includeLayers":true}})
    );
    client.close().await;
}

#[tokio::test]
async fn repeated_large_rpc_replies_do_not_consume_notification_history() {
    let home = Home::new();
    let mut client = fixture_client(&home.0, "large").await;
    for _ in 0..5 {
        let response = client.rpc("config/read", json!({})).await.unwrap();
        assert_eq!(response["padding"].as_str().unwrap().len(), 1_500_000);
    }
    assert_eq!(client.events().len(), 6);
    assert!(
        client
            .events()
            .iter()
            .all(|event| event["method"] == "notification")
    );
    client.close().await;
}

#[tokio::test]
async fn symlinked_config_uses_the_original_home_for_verification_and_rollback() {
    let home = Home::new();
    let target = home.0.join("dotfiles/codex-settings.toml");
    std::fs::create_dir(target.parent().unwrap()).unwrap();
    std::fs::rename(home.0.join("config.toml"), &target).unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&target, home.0.join("config.toml")).unwrap();
    #[cfg(windows)]
    if let Err(error) = std::os::windows::fs::symlink_file(&target, home.0.join("config.toml")) {
        eprintln!(
            "symlinked configuration fixture unavailable: {:?}",
            error.kind()
        );
        return;
    }
    let mut client = Fake::new(&home.0);
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    let mut applied = apply_replace(&mut client, &home.0, plan).await.unwrap();
    assert!(
        read_connection(&home.0.join("pseudolife/connection.json"))
            .unwrap()
            .is_some()
    );
    applied.rollback(&mut client, &home.0).await.unwrap();
    assert_eq!(
        client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_TOKEN"],
        "fixture-token"
    );
    assert!(!home.0.join("pseudolife/token").exists());
    assert!(
        std::fs::symlink_metadata(home.0.join("config.toml"))
            .unwrap()
            .file_type()
            .is_symlink()
    );
}

#[tokio::test]
async fn rpc_rejection_redacts_child_diagnostics() {
    let home = Home::new();
    let mut client = fixture_client(&home.0, "reject").await;
    let error = client.rpc("config/read", json!({})).await.unwrap_err();
    assert!(!error.to_string().contains("fixture-secret"));
    assert_eq!(
        error,
        SetupError::rejected(
            "Codex rejected the setup request; update Codex or review its configuration before retrying."
        )
    );
    client.close().await;
}

#[tokio::test]
async fn rpc_bounds_silent_or_exited_child() {
    for mode in ["timeout", "exit"] {
        let home = Home::new();
        let mut client = fixture_client(&home.0, mode).await;
        let start = std::time::Instant::now();
        assert!(client.rpc("config/read", json!({})).await.is_err());
        assert!(start.elapsed() < std::time::Duration::from_secs(10));
        drop(client);
    }
}

#[tokio::test]
async fn registration_is_scoped_and_refuses_a_foreign_same_name_entry() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    for root in ["/config", "/layers/0/config"] {
        client.config.pointer_mut(root).unwrap()["mcp_servers"]
            .as_object_mut()
            .unwrap()
            .remove("pseudolife-memory");
    }
    let mut env = serde_json::Map::new();
    env.insert("PSEUDOLIFE_WRITER_ID".into(), json!("codex"));
    let command = home.0.join("pseudolife-mcp.exe");
    let plan = plan_registration(&client.config, &home.0, &command, &[], &env).unwrap();
    plan.apply(&mut client, &home.0).await.unwrap();
    assert_eq!(
        client.config["config"]["mcp_servers"]["foreign"]["command"],
        "other"
    );
    let plan = plan_registration(&client.config, &home.0, &command, &[], &env).unwrap();
    assert!(!plan.changed());
    client.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["command"] =
        json!("foreign-server");
    assert!(plan_registration(&client.config, &home.0, &command, &[], &env).is_err());
}

#[test]
fn fresh_registration_refuses_a_project_owned_server() {
    let home = Home::new();
    let mut original = config(&home.0);
    original["layers"][0]["config"]["mcp_servers"]
        .as_object_mut()
        .unwrap()
        .remove("pseudolife-memory");
    assert!(
        plan_registration(
            &original,
            &home.0,
            &home.0.join("pseudolife-mcp"),
            &[],
            &serde_json::Map::new()
        )
        .is_err()
    );
}

#[tokio::test]
#[ignore = "requires an installed Codex CLI; run explicitly with an isolated home"]
async fn installed_codex_round_trip_preserves_foreign_configuration() {
    let home = Home::new();
    let text = "# keep this comment\n[mcp_servers.foreign]\ncommand = \"other-server\"\n[mcp_servers.pseudolife-memory]\ncommand = \"pseudolife-mcp\"\n[mcp_servers.pseudolife-memory.env]\nPSEUDOLIFE_MCP_TOKEN = \"fixture-token\"\nUNRELATED = \"keep\"\n";
    std::fs::write(home.0.join("config.toml"), text).unwrap();
    let executable = pseudolife_stdio::setup::codex::resolve_codex().unwrap();
    let mut command = tokio::process::Command::new(executable);
    command
        .args(["app-server", "--stdio"])
        .env_clear()
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0)
        .env("CODEX_HOME", &home.0)
        .env("LOCALAPPDATA", home.0.join("local"))
        .env("APPDATA", home.0.join("roaming"))
        .current_dir(&home.0);
    for key in ["SYSTEMROOT", "WINDIR", "PATH"] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    let mut client =
        pseudolife_stdio::setup::codex::Client::spawn(command, std::time::Duration::from_secs(20))
            .await
            .unwrap();
    let config = client.read_config(&home.0).await.unwrap();
    let plan = plan_replace(&config, &home.0, "https://example.com", None, true).unwrap();
    let mut applied = apply_replace(&mut client, &home.0, plan).await.unwrap();
    let current = client.read_config(&home.0).await.unwrap();
    assert_eq!(
        current["config"]["mcp_servers"]["foreign"]["command"],
        "other-server"
    );
    assert!(
        std::fs::read_to_string(home.0.join("config.toml"))
            .unwrap()
            .contains("# keep this comment")
    );
    applied.rollback(&mut client, &home.0).await.unwrap();
    assert!(!home.0.join("pseudolife").join("token").exists());
    assert!(!home.0.join("pseudolife").join("connection.json").exists());
    let current = client.read_config(&home.0).await.unwrap();
    assert_eq!(
        current["config"]["mcp_servers"]["pseudolife-memory"]["env"]["UNRELATED"],
        "keep"
    );
    client.close().await;
}

#[tokio::test]
async fn credential_bootstrap_preserves_implicit_url_and_foreign_env() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    for root in ["/config", "/layers/0/config"] {
        client.config.pointer_mut(root).unwrap()["mcp_servers"]["pseudolife-memory"]["env"]
            .as_object_mut()
            .unwrap()
            .remove("PSEUDOLIFE_MCP_DAEMON_URL");
    }
    let result = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &home.0,
        &home.0,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(result["daemon_url"], "http://127.0.0.1:8765");
    assert_eq!(result["credential_file_configured"], true);
    let env = &client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"];
    assert!(env.get("PSEUDOLIFE_MCP_DAEMON_URL").is_none());
    assert!(env.get("PSEUDOLIFE_MCP_TOKEN").is_none());
    assert_eq!(env["UNRELATED"], "keep");
}

#[tokio::test]
async fn credential_bootstrap_refuses_overrides_before_creating_token() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_TOKEN"] =
        json!("project-fixture");
    let error = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &home.0,
        &home.0,
        None,
        None,
    )
    .await
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("No connection settings were changed")
    );
    assert!(!home.0.join("pseudolife").join("token").exists());
    assert!(
        client
            .calls
            .iter()
            .all(|(method, _)| method != "config/batchWrite")
    );
}

#[tokio::test]
async fn fresh_tokenless_installer_records_origin_without_config_write() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    for root in ["/config", "/layers/0/config"] {
        client.config.pointer_mut(root).unwrap()["mcp_servers"]
            .as_object_mut()
            .unwrap()
            .remove("pseudolife-memory");
    }
    let result = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut client,
        &home.0,
        &home.0,
        None,
        Some(("https://example.com", None)),
    )
    .await
    .unwrap();
    assert_eq!(result["credential_file_configured"], false);
    assert_eq!(result["connection_configured"], true);
    assert_eq!(
        read_connection(&home.0.join("pseudolife").join("connection.json")).unwrap(),
        Some(("https://example.com".into(), "".into()))
    );
    assert!(
        client
            .calls
            .iter()
            .all(|(method, _)| method != "config/batchWrite")
    );
}

#[tokio::test]
async fn failed_rollback_keeps_token_needed_by_persisted_configuration() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.override_env = true;
    client.concurrent_env = true;
    let plan = plan_replace(&client.config, &home.0, "https://example.com", None, true).unwrap();
    assert!(apply_replace(&mut client, &home.0, plan).await.is_err());
    assert!(home.0.join("pseudolife").join("token").exists());
    assert_eq!(
        client.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"]["UNRELATED"],
        "concurrent change"
    );
}

#[tokio::test]
async fn registration_verification_accepts_typed_defaults_outside_the_edit() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.typed_extra = true;
    let mut env = Map::new();
    env.insert("PSEUDOLIFE_WRITER_ID".into(), json!("codex"));
    let plan = plan_registration(
        &client.config,
        &home.0,
        &home.0.join("pseudolife-mcp.exe"),
        &[],
        &env,
    )
    .unwrap();
    assert!(plan.apply(&mut client, &home.0).await.is_ok());
}

#[test]
fn python_plan_parity_fixture() {
    let Some(path) = std::env::var_os("PSEUDOLIFE_CODEX_PLAN_FIXTURE") else {
        return;
    };
    let request: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    let results = request["cases"]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| {
            let result = plan_replace(
                &case["config"],
                Path::new(case["home"].as_str().unwrap()),
                case["url"].as_str().unwrap(),
                case["token_file"].as_str().map(Path::new),
                case["no_spawn"].as_bool().unwrap(),
            );
            match result {
                Ok(plan) => json!({"ok":true,"before":plan.previous_env(),"after":plan.env()}),
                Err(_) => json!({"ok":false}),
            }
        })
        .collect::<Vec<_>>();
    std::fs::write(
        request["output"].as_str().unwrap(),
        serde_json::to_vec(&results).unwrap(),
    )
    .unwrap();
}

#[tokio::test]
async fn empty_user_credential_names_user_layer_and_project_near_miss_keeps_its_authority() {
    struct NoWrites;
    impl Rpc for NoWrites {
        async fn rpc(&mut self, _: &str, _: Value) -> Result<Value, SetupError> {
            panic!("credential refusal must precede every RPC write");
        }
    }
    let home = Home::new();
    let mut user = config(&home.0);
    user["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_TOKEN"] =
        json!("");
    user["config"] = user["layers"][0]["config"].clone();
    let before = std::fs::read(home.0.join("config.toml")).unwrap();
    let error = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut NoWrites,
        &home.0,
        &home.0,
        Some(user.clone()),
        Some(("http://localhost:8765", None)),
    )
    .await
    .unwrap_err()
    .to_string();
    assert!(error.contains("user configuration") && error.contains("PSEUDOLIFE_MCP_TOKEN"));
    assert!(!error.contains("project or managed"));
    user["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"]
        .as_object_mut()
        .unwrap()
        .remove("PSEUDOLIFE_MCP_TOKEN");
    let project = home.0.join("project").join("config.toml");
    user["layers"].as_array_mut().unwrap().insert(0, json!({"name":{"type":"project","dotCodexFolder":project.parent().unwrap()},"version":"project-a","config":{"mcp_servers":{"pseudolife-memory":{"env":{"PSEUDOLIFE_MCP_TOKEN":""}}}}}));
    let error = pseudolife_stdio::setup::codex::configure_credential_file(
        &mut NoWrites,
        &home.0,
        &home.0,
        Some(user),
        Some(("http://localhost:8765", None)),
    )
    .await
    .unwrap_err()
    .to_string();
    assert!(error.contains("project") && error.contains("comes from"));
    assert!(!error.contains("empty in Codex's user"));
    assert_eq!(std::fs::read(home.0.join("config.toml")).unwrap(), before);
    assert!(!home.0.join("pseudolife").exists());
}
