use pseudolife_stdio::setup::codex::{
    Rpc, SetupError, apply_replace, plan_registration, read_connection,
};
use pseudolife_stdio::setup::codex::{plan_replace, user_config};
use serde_json::{Map, Value, json};
use std::path::Path;

struct Home(std::path::PathBuf);
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
        }
    }
}
impl Rpc for Fake {
    async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
        self.calls.push((method.to_owned(), params.clone()));
        if method == "config/batchWrite" {
            if self.reject {
                return Err(SetupError("fixture refusal"));
            }
            assert_eq!(
                params["expectedVersion"],
                self.config["layers"][0]["version"]
            );
            for edit in params["edits"].as_array().unwrap() {
                if edit["keyPath"] == "\"mcp_servers\".\"pseudolife-memory\"" {
                    for root in ["/config", "/layers/0/config"] {
                        self.config.pointer_mut(root).unwrap()["mcp_servers"]["pseudolife-memory"] =
                            edit["value"].clone();
                    }
                    continue;
                }
                assert_eq!(
                    edit["keyPath"],
                    "\"mcp_servers\".\"pseudolife-memory\".\"env\""
                );
                self.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"] =
                    edit["value"].clone();
                self.config["config"]["mcp_servers"]["pseudolife-memory"]["env"] =
                    edit["value"].clone();
            }
            self.config["layers"][0]["version"] = json!(format!("version-{}", self.calls.len()));
            if self.rejected_after_competitor {
                self.rejected_after_competitor = false;
                let file = self.config["layers"][0]["name"]["file"].as_str().unwrap();
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
                self.config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"]["UNRELATED"] =
                    json!("concurrent change");
            }
            if self.typed_extra {
                self.config["config"]["mcp_servers"]["pseudolife-memory"]["enabled"] = json!(true);
            }
            return Ok(json!({"version":self.config["layers"][0]["version"]}));
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
    assert!(apply_replace(&mut client, &home.0, plan).await.is_err());
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
async fn bootstrap_refuses_project_forwarded_file_before_migrating_user_literal() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.config["config"]["mcp_servers"]["pseudolife-memory"]["env_vars"] =
        json!(["PSEUDOLIFE_MCP_TOKEN_FILE"]);
    assert!(
        pseudolife_stdio::setup::codex::configure_credential_file(
            &mut client,
            &home.0,
            &home.0,
            None,
            None
        )
        .await
        .is_err()
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
fn replace_refuses_project_forwarded_no_spawn() {
    let home = Home::new();
    let mut config = config(&home.0);
    config["config"]["mcp_servers"]["pseudolife-memory"]["env_vars"] =
        json!(["PSEUDOLIFE_MCP_NO_SPAWN"]);
    assert!(plan_replace(&config, &home.0, "https://example.com", None, true).is_err());
}

#[test]
fn replace_accepts_user_owned_forwarded_no_spawn() {
    let home = Home::new();
    let mut config = config(&home.0);
    for root in ["/config", "/layers/0/config"] {
        config.pointer_mut(root).unwrap()["mcp_servers"]["pseudolife-memory"]["env_vars"] =
            json!(["PSEUDOLIFE_MCP_NO_SPAWN"]);
    }
    let plan = plan_replace(&config, &home.0, "https://example.com", None, true).unwrap();
    assert_eq!(plan.env()["PSEUDOLIFE_MCP_NO_SPAWN"], "1");
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
        home.join("pseudolife").join("token").to_str().unwrap()
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
    assert_eq!(
        read_connection(&connection).unwrap(),
        Some((
            "https://example.com".to_owned(),
            token.to_str().unwrap().to_owned()
        ))
    );
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
                "reject" => {
                    println!(
                        "{}",
                        json!({"id":id,"error":{"message":"fixture-secret-not-a-diagnostic"}})
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
async fn rpc_rejection_redacts_child_diagnostics() {
    let home = Home::new();
    let mut client = fixture_client(&home.0, "reject").await;
    let error = client.rpc("config/read", json!({})).await.unwrap_err();
    assert!(!error.to_string().contains("fixture-secret"));
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
    assert!(
        pseudolife_stdio::setup::codex::configure_credential_file(
            &mut client,
            &home.0,
            &home.0,
            None,
            None
        )
        .await
        .is_err()
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
