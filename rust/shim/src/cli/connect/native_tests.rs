//! The User-scope daemon URL and the Codex rows, through their seams: a fake
//! registry backend and a fake Codex app-server. No test reads or writes
//! the real registry, the real Codex home or a real Codex.
use super::discover::{self, Ctx};
use super::user_env::{self, Backend, Value};
use super::*;
use serde_json::{Value as Json, json};
use std::cell::{Cell, RefCell};
use std::path::Path;
use std::rc::Rc;

const NEW: &str = "http://10.0.0.7:8765";
const OLD: &str = "http://10.0.0.5:8765";

fn ctx(clients: Vec<&'static str>, url: &str) -> Ctx {
    Ctx {
        url: url.to_owned(),
        remote: true,
        token_file: None,
        clients,
        layout: None,
    }
}

#[derive(Default)]
struct FakeEnv {
    user: RefCell<Option<Value>>,
    machine: Option<Value>,
    user_error: Option<String>,
    set_error: Option<String>,
    sets: RefCell<Vec<Value>>,
    broadcasts: Cell<u32>,
}
impl Backend for FakeEnv {
    fn user(&self, _name: &str) -> Result<Option<Value>, String> {
        match &self.user_error {
            Some(cause) => Err(cause.clone()),
            None => Ok(self.user.borrow().clone()),
        }
    }
    fn machine(&self, _name: &str) -> Result<Option<Value>, String> {
        Ok(self.machine.clone())
    }
    fn set_user(&self, _name: &str, value: &Value) -> Result<(), String> {
        if let Some(cause) = &self.set_error {
            return Err(cause.clone());
        }
        self.sets.borrow_mut().push(value.clone());
        *self.user.borrow_mut() = Some(value.clone());
        Ok(())
    }
    fn broadcast(&self) {
        self.broadcasts.set(self.broadcasts.get() + 1);
    }
}

fn value(kind: &str, data: &str) -> Value {
    Value {
        kind: kind.into(),
        data: data.into(),
    }
}

fn env_rows(fake: FakeEnv) -> Vec<discover::Row> {
    user_env::with_backend(Rc::new(fake), || {
        discover::windows_environment_rows(&ctx(vec![], NEW))
            .ok()
            .unwrap()
    })
}

#[test]
fn a_user_scope_url_naming_another_daemon_is_a_change_row() {
    let rows = env_rows(FakeEnv {
        user: RefCell::new(Some(value("REG_EXPAND_SZ", OLD))),
        ..FakeEnv::default()
    });
    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    assert_eq!(
        (row.client, row.place, row.state),
        ("environment", "user", "change")
    );
    assert_eq!(
        row.changes,
        vec![(
            URL_KEY.to_owned(),
            Some(OLD.to_owned()),
            Some(NEW.to_owned())
        )]
    );
    assert!(row.notes[0].contains("programs already running keep the old value"));
    assert_eq!(row.user_env, Some(value("REG_EXPAND_SZ", OLD)));
}

#[test]
fn a_user_scope_url_that_matches_or_is_absent_adds_no_row_and_none_is_created() {
    assert!(
        env_rows(FakeEnv {
            user: RefCell::new(Some(value("REG_SZ", NEW))),
            ..FakeEnv::default()
        })
        .is_empty()
    );
    // Same daemon, spelled with a trailing slash: still current.
    assert!(
        env_rows(FakeEnv {
            user: RefCell::new(Some(value("REG_SZ", &format!("{NEW}/")))),
            ..FakeEnv::default()
        })
        .is_empty()
    );
    assert!(env_rows(FakeEnv::default()).is_empty());
    // An empty value is unset on Windows: never re-pointed into existence.
    assert!(
        env_rows(FakeEnv {
            user: RefCell::new(Some(value("REG_SZ", ""))),
            ..FakeEnv::default()
        })
        .is_empty()
    );
}

#[test]
fn a_machine_scope_url_with_no_user_value_over_it_is_a_manual_row() {
    let rows = env_rows(FakeEnv {
        machine: Some(value("REG_SZ", OLD)),
        ..FakeEnv::default()
    });
    assert_eq!(rows.len(), 1);
    assert_eq!((rows[0].place, rows[0].state), ("machine", "manual"));
    assert!(
        rows[0]
            .detail
            .contains("connect never changes Machine scope")
    );
    assert!(
        rows[0]
            .detail
            .contains(&user_env::powershell_set(URL_KEY, NEW, "User"))
    );
    // A User-scope value overrides Machine scope: only it is re-pointed.
    let rows = env_rows(FakeEnv {
        user: RefCell::new(Some(value("REG_SZ", OLD))),
        machine: Some(value("REG_SZ", OLD)),
        ..FakeEnv::default()
    });
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].place, "user");
    // A Machine-scope value naming this daemon needs nothing.
    assert!(
        env_rows(FakeEnv {
            machine: Some(value("REG_SZ", NEW)),
            ..FakeEnv::default()
        })
        .is_empty()
    );
}

#[test]
fn an_unreadable_user_scope_is_a_manual_row_naming_the_cause() {
    let rows = env_rows(FakeEnv {
        user_error: Some("reg.exe could not be started: denied".into()),
        ..FakeEnv::default()
    });
    assert_eq!(rows[0].state, "manual");
    assert!(
        rows[0]
            .detail
            .contains("reg.exe could not be started: denied")
    );
}

fn claude_change_row() -> discover::Row {
    let mut row = discover::row("claude-code", "registration", None, Some(SERVER), "change")
        .ok()
        .unwrap();
    row.changes = vec![(
        URL_KEY.to_owned(),
        Some(OLD.to_owned()),
        Some(NEW.to_owned()),
    )];
    row
}

fn user_row() -> discover::Row {
    env_rows(FakeEnv {
        user: RefCell::new(Some(value("REG_EXPAND_SZ", OLD))),
        ..FakeEnv::default()
    })
    .remove(0)
}

#[test]
fn applying_re_points_the_user_scope_keeping_its_type_and_broadcasts() {
    // The User-scope row is found after another change row.
    let rows = vec![claude_change_row(), user_row()];
    let fake = FakeEnv::default();
    let note = apply_user_env(&rows, NEW, &fake).unwrap().unwrap();
    assert_eq!(*fake.sets.borrow(), vec![value("REG_EXPAND_SZ", NEW)]);
    assert_eq!(fake.broadcasts.get(), 1);
    assert!(note.contains(&format!("now names {NEW} (it named {OLD})")));
    assert!(note.contains("programs already running keep the old value"));
}

#[test]
fn without_a_user_scope_row_nothing_is_written() {
    let fake = FakeEnv::default();
    assert_eq!(apply_user_env(&[claude_change_row()], NEW, &fake), Ok(None));
    assert!(fake.sets.borrow().is_empty());
    assert_eq!(fake.broadcasts.get(), 0);
}

#[test]
fn a_failed_user_scope_write_says_what_changed_and_the_command_that_finishes_it() {
    let fake = FakeEnv {
        set_error: Some("Access is denied.".into()),
        ..FakeEnv::default()
    };
    let error = apply_user_env(&[claude_change_row(), user_row()], NEW, &fake).unwrap_err();
    assert!(
        error.contains(&format!("the client registrations now name {NEW}")),
        "{error}"
    );
    assert!(
        error.contains("was not changed (Access is denied.)"),
        "{error}"
    );
    assert!(error.contains(&format!("still names {OLD}")), "{error}");
    assert!(
        error.ends_with(&user_env::powershell_set(URL_KEY, NEW, "User")),
        "{error}"
    );
    assert_eq!(fake.broadcasts.get(), 0);
}

// ---- Codex, through a fake app-server ----

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("connect-codex-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&path).unwrap();
        let path = paths::strip_verbatim(std::fs::canonicalize(&path).unwrap());
        Self(path)
    }
    fn config(&self) -> PathBuf {
        self.0.join("config.toml")
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Codex's configuration, shared by every client the opener hands out.
#[derive(Clone)]
struct FakeCodex {
    config: Rc<RefCell<Json>>,
    fail_open: Option<&'static str>,
    reject_write: bool,
}
impl FakeCodex {
    fn new(home: &Path, env: Json) -> Self {
        let values = json!({"mcp_servers": {"pseudolife-memory": {"command": "pseudolife-mcp", "env": env}}});
        Self {
            config: Rc::new(RefCell::new(json!({
                "config": values,
                "layers": [{"name": {"type": "user", "file": home.join("config.toml")},
                            "version": "version-a", "config": values}],
            }))),
            fail_open: None,
            reject_write: false,
        }
    }
}
impl crate::setup::codex::Rpc for FakeCodex {
    async fn rpc(
        &mut self,
        method: &str,
        params: Json,
    ) -> Result<Json, crate::setup::codex::SetupError> {
        let mut config = self.config.borrow_mut();
        if method == "config/batchWrite" {
            if self.reject_write {
                return Err(crate::setup::codex::SetupError::rejected("fixture refusal"));
            }
            for edit in params["edits"].as_array().unwrap() {
                assert_eq!(
                    edit["keyPath"],
                    "\"mcp_servers\".\"pseudolife-memory\".\"env\""
                );
                config["layers"][0]["config"]["mcp_servers"]["pseudolife-memory"]["env"] =
                    edit["value"].clone();
                config["config"]["mcp_servers"]["pseudolife-memory"]["env"] = edit["value"].clone();
            }
            config["layers"][0]["version"] = json!("version-b");
            // Codex answers a write with the user layer's new version.
            return Ok(json!({"version": "version-b"}));
        }
        Ok(config.clone())
    }
}
impl codex::Opener for FakeCodex {
    type Client = FakeCodex;
    async fn open(
        &self,
        _home: &Path,
        _cwd: &Path,
    ) -> Result<FakeCodex, crate::setup::codex::SetupError> {
        match self.fail_open {
            Some(reason) => Err(crate::setup::codex::SetupError(reason)),
            None => Ok(self.clone()),
        }
    }
}

fn block<T>(future: impl std::future::Future<Output = T>) -> T {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(future)
}

#[test]
fn a_codex_registration_naming_another_daemon_plans_both_codex_rows() {
    let home = Home::new();
    let fake = FakeCodex::new(
        &home.0,
        json!({URL_KEY: OLD, TOKEN_KEY: "literal-fixture-token"}),
    );
    let rows = block(codex::rows_for(
        &ctx(vec!["codex"], NEW),
        &home.config(),
        Some("pseudolife-mcp".into()),
        &fake,
    ))
    .ok()
    .unwrap();
    assert_eq!(rows.len(), 2);
    let (registration, connection) = (&rows[0], &rows[1]);
    assert_eq!(
        (registration.place, registration.state),
        ("registration", "change")
    );
    let mut keys: Vec<&str> = registration
        .changes
        .iter()
        .map(|(k, _, _)| k.as_str())
        .collect();
    keys.sort_unstable();
    let mut wanted = vec![URL_KEY, NO_SPAWN_KEY, TOKEN_KEY, FILE_KEY];
    wanted.sort_unstable();
    assert_eq!(keys, wanted);
    // The literal token is never shown, and moves into Codex's token copy.
    let token = registration
        .changes
        .iter()
        .find(|(k, _, _)| k == TOKEN_KEY)
        .unwrap();
    assert_eq!(token.1.as_deref(), Some("<literal token>"));
    assert!(
        registration
            .notes
            .iter()
            .any(|n| n.contains("moves into Codex's token copy"))
    );
    assert!(matches!(
        registration.credential,
        Some(Some(Cred::Literal(_)))
    ));
    assert_eq!(
        (connection.place, connection.state),
        ("connection", "change")
    );
    assert_eq!(
        connection.changes[0],
        ("daemon_url".to_owned(), None, Some(NEW.to_owned()))
    );
    assert!(
        rows.iter()
            .all(|r| r.codex.as_deref() == Some(home.0.as_path()))
    );
}

#[test]
fn a_codex_that_does_not_start_is_a_manual_row_naming_why() {
    let home = Home::new();
    let mut fake = FakeCodex::new(&home.0, json!({URL_KEY: OLD}));
    fake.fail_open = Some("Codex CLI is unavailable; install or update Codex, then rerun setup.");
    let rows = block(codex::rows_for(
        &ctx(vec!["codex"], NEW),
        &home.config(),
        Some("x".into()),
        &fake,
    ))
    .ok()
    .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].state, "manual");
    assert!(rows[0].detail.starts_with("Codex CLI is unavailable"));
    assert!(
        rows[0]
            .detail
            .contains("Edit [mcp_servers.pseudolife-memory.env] where it is set")
    );
}

#[test]
fn without_a_codex_registration_a_url_server_or_unreadable_file_is_manual_and_otherwise_absent() {
    let home = Home::new();
    let fake = FakeCodex::new(&home.0, json!({}));
    let run = |text: Option<&str>| {
        match text {
            Some(text) => std::fs::write(home.config(), text).unwrap(),
            None => {
                let _ = std::fs::remove_file(home.config());
            }
        }
        let rows = block(codex::rows_for(
            &ctx(vec!["codex"], NEW),
            &home.config(),
            None,
            &fake,
        ))
        .ok()
        .unwrap();
        (rows[0].state, rows[0].detail.clone())
    };
    let (state, detail) = run(Some(
        "[mcp_servers.pseudolife-memory]\nurl = \"https://example.com/mcp\"\n",
    ));
    assert_eq!(state, "manual");
    assert!(detail.contains("a url server"), "{detail}");
    let (state, detail) = run(Some("[mcp_servers.pseudolife-memory\nnot toml"));
    assert_eq!(state, "manual");
    assert!(detail.contains("not valid TOML"), "{detail}");
    assert_eq!(run(Some("[other]\nx = 1\n")).0, "absent");
    assert_eq!(run(None).0, "absent");
}

#[test]
fn applying_the_codex_rows_writes_through_the_app_server_and_records_every_file() {
    let home = Home::new();
    let token = home.0.join("t.token");
    crate::board::state::atomic_create(&token, b"fixture-token").unwrap();
    let fake = FakeCodex::new(
        &home.0,
        json!({URL_KEY: OLD, FILE_KEY: token.to_str().unwrap()}),
    );
    let context = ctx(vec!["codex"], NEW);
    let mut rows = block(codex::rows_for(
        &context,
        &home.config(),
        Some("pseudolife-mcp".into()),
        &fake,
    ))
    .ok()
    .unwrap();
    let mut steps = Vec::new();
    let mut refs: Vec<&mut discover::Row> = rows.iter_mut().collect();
    if let Err(failure) = block(codex::apply(&context, &mut refs, &mut steps, &fake)) {
        panic!("{}: {}", failure.path.display(), failure.reason);
    }
    let env = fake.config.borrow()["config"]["mcp_servers"]["pseudolife-memory"]["env"].clone();
    assert_eq!(env[URL_KEY], NEW);
    // config.toml, the connection file and the token copy are recorded.
    assert_eq!(steps.len(), 3);
    let connection = crate::setup::codex::connection_path(&home.0);
    assert_eq!(
        crate::setup::codex::read_connection(&connection)
            .unwrap()
            .map(|(url, _)| url),
        Some(NEW.to_owned())
    );
    assert!(steps[1].original.is_none() && steps[1].written.is_some());
    assert!(rows[1].created, "the connection file was created");
}

#[test]
fn a_failed_codex_write_leaves_its_files_to_its_own_compensation() {
    // The app-server transaction undoes or keeps its own files; the run's
    // snapshot rollback must not restore them over another writer's.
    let home = Home::new();
    let token = home.0.join("t.token");
    crate::board::state::atomic_create(&token, b"fixture-token").unwrap();
    let mut fake = FakeCodex::new(
        &home.0,
        json!({URL_KEY: OLD, FILE_KEY: token.to_str().unwrap()}),
    );
    let context = ctx(vec!["codex"], NEW);
    let mut rows = block(codex::rows_for(
        &context,
        &home.config(),
        Some("pseudolife-mcp".into()),
        &fake,
    ))
    .ok()
    .unwrap();
    fake.reject_write = true;
    let mut steps = Vec::new();
    let mut refs: Vec<&mut discover::Row> = rows.iter_mut().collect();
    let failure = block(codex::apply(&context, &mut refs, &mut steps, &fake)).expect_err("refused");
    assert!(failure.reason.contains("fixture refusal") || !failure.reason.is_empty());
    assert!(
        steps.is_empty(),
        "Codex's files left the run's rollback list"
    );
    let env = fake.config.borrow()["config"]["mcp_servers"]["pseudolife-memory"]["env"].clone();
    assert_eq!(env[URL_KEY], OLD, "the refused write changed nothing");
}

#[test]
fn before_pairing_the_codex_plan_shows_the_token_placeholder_as_written() {
    let home = Home::new();
    let fake = FakeCodex::new(&home.0, json!({URL_KEY: OLD}));
    let mut context = ctx(vec!["codex"], NEW);
    context.token_file = Some(PAIRED_TOKEN_FILE.to_owned());
    let rows = block(codex::rows_for(
        &context,
        &home.config(),
        Some("pseudolife-mcp".into()),
        &fake,
    ))
    .ok()
    .unwrap();
    let file = rows[0]
        .changes
        .iter()
        .find(|(k, _, _)| k == FILE_KEY)
        .unwrap();
    assert_eq!(file.2.as_deref(), Some(PAIRED_TOKEN_FILE));
}

#[test]
fn a_codex_configuration_changed_after_the_plan_is_refused_untouched() {
    let home = Home::new();
    let token = home.0.join("t.token");
    crate::board::state::atomic_create(&token, b"fixture-token").unwrap();
    let fake = FakeCodex::new(
        &home.0,
        json!({URL_KEY: OLD, FILE_KEY: token.to_str().unwrap()}),
    );
    let context = ctx(vec!["codex"], NEW);
    let mut rows = block(codex::rows_for(
        &context,
        &home.config(),
        Some("pseudolife-mcp".into()),
        &fake,
    ))
    .ok()
    .unwrap();
    // Someone edits Codex's configuration between the plan and the write.
    for root in ["/config", "/layers/0/config"] {
        fake.config.borrow_mut().pointer_mut(root).unwrap()["mcp_servers"]["pseudolife-memory"]["env"]
            ["EXTRA"] = json!("added later");
    }
    let mut steps = Vec::new();
    let mut refs: Vec<&mut discover::Row> = rows.iter_mut().collect();
    let failure = block(codex::apply(&context, &mut refs, &mut steps, &fake)).expect_err("refused");
    assert!(
        failure
            .reason
            .starts_with("Codex's configuration changed after the plan was shown"),
        "{}",
        failure.reason
    );
    assert!(steps.is_empty());
    let env = fake.config.borrow()["config"]["mcp_servers"]["pseudolife-memory"]["env"].clone();
    assert_eq!(env[URL_KEY], OLD);
}
