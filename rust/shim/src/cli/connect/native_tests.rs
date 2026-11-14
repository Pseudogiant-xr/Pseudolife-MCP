//! The User-scope daemon URL and the Codex rows, through their seams: a fake
//! registry backend and a fake Codex app-server. No test reads or writes
//! the real registry, the real Codex home or a real Codex.
use super::discover::{self, Ctx};
use super::user_env::{self, Backend, Value};
use super::*;
use serde_json::{Value as Json, json};
use std::cell::{Cell, RefCell};
use std::path::{Path, PathBuf};
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
    // A registration this run rewrites, as discovery plans one.
    row.json = Some(discover::JsonSpec {
        path: PathBuf::from("fixture/.claude.json"),
        pointer: vec!["mcpServers".into(), SERVER.into()],
        edits: vec![(URL_KEY.to_owned(), Some(NEW.to_owned()))],
    });
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
    // With only the User-scope value to change, it never claims they moved.
    let alone = apply_user_env(&[user_row()], NEW, &fake).unwrap_err();
    assert!(alone.starts_with("the Windows User-scope"), "{alone}");
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

// ---- the unattended-update schedules, through a fake scheduler ----

/// (client, url, time, shaped, why) for one schedule installed.
type Installed = (
    bool,
    Option<&'static str>,
    Option<&'static str>,
    bool,
    Option<&'static str>,
);

#[derive(Default)]
struct FakeScheduler {
    installed: Vec<Installed>,
    /// (other kind, reason) when `--schedule` here would refuse.
    refuse_here: Option<(bool, String)>,
    refuse_write: Option<String>,
    made: RefCell<Vec<(bool, String, String)>>,
}
impl schedule::Scheduler for FakeScheduler {
    fn found(&self) -> Vec<schedule::Found> {
        self.installed
            .iter()
            .map(|(client, url, time, shaped, why)| {
                let task = if *client { "client" } else { "daemon" };
                schedule::Found {
                    client: *client,
                    place: format!("the scheduled task '{task}'"),
                    time: time.map(str::to_owned),
                    url: url.map(str::to_owned),
                    shaped: *shaped,
                    why: why.map(str::to_owned),
                    remove: format!("schtasks /Delete /TN \"{task}\" /F"),
                }
            })
            .collect()
    }
    fn preflight(&self, _client: bool, _url: &str) -> Result<(), schedule::Refusal> {
        match &self.refuse_here {
            Some((other_kind, reason)) => Err(schedule::Refusal {
                other_kind: *other_kind,
                reason: reason.clone(),
            }),
            None => Ok(()),
        }
    }
    fn recreate(&self, client: bool, when: &str, url: &str) -> Result<(), String> {
        if let Some(cause) = &self.refuse_write {
            return Err(cause.clone());
        }
        self.made
            .borrow_mut()
            .push((client, when.to_owned(), url.to_owned()));
        Ok(())
    }
    fn cancelled(&self) -> bool {
        false
    }
}

const LOCAL_OLD: &str = "http://127.0.0.1:8765";
const LOCAL_NEW: &str = "http://127.0.0.1:8766";

fn schedule_rows(fake: &Rc<FakeScheduler>, url: &str, remote: bool) -> Vec<discover::Row> {
    let mut context = ctx(CLIENTS.to_vec(), url);
    context.remote = remote;
    schedule::with_backend(fake.clone(), || schedule::rows(&context).ok().unwrap())
}

#[test]
fn a_local_daemon_schedule_naming_another_daemon_is_re_created_keeping_its_time() {
    let fake = Rc::new(FakeScheduler {
        installed: vec![(false, Some(LOCAL_OLD), Some("03:07"), true, None)],
        ..FakeScheduler::default()
    });
    let rows = schedule_rows(&fake, LOCAL_NEW, false);
    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    assert_eq!(
        (row.client, row.place, row.state),
        ("unattended-update", "schedule", "change")
    );
    assert_eq!(
        row.changes,
        vec![(
            "--daemon-url".to_owned(),
            Some(LOCAL_OLD.to_owned()),
            Some(LOCAL_NEW.to_owned())
        )]
    );
    let (notes, failures) = schedule::apply(&rows, LOCAL_NEW, true, fake.as_ref());
    assert!(failures.is_empty());
    assert_eq!(
        *fake.made.borrow(),
        vec![(false, "03:07".to_owned(), LOCAL_NEW.to_owned())]
    );
    assert!(notes[0].contains("daily at 03:07"), "{notes:?}");
}

#[test]
fn a_client_schedule_follows_a_remote_daemon_but_a_daemon_schedule_does_not() {
    let fake = Rc::new(FakeScheduler {
        installed: vec![
            (false, Some(OLD), Some("03:07"), true, None),
            (true, Some(OLD), Some("04:30"), true, None),
        ],
        ..FakeScheduler::default()
    });
    let rows = schedule_rows(&fake, NEW, true);
    assert_eq!(rows.len(), 2);
    // The daemon's schedule updates a daemon on this machine, which is no
    // longer the one in use: a manual row, as the oracle words it.
    assert_eq!(rows[0].state, "manual");
    assert!(
        rows[0]
            .detail
            .contains("pseudolife-mcp update --unschedule"),
        "{}",
        rows[0].detail
    );
    // The client schedule names the daemon it follows: re-created.
    assert_eq!(
        (rows[1].place, rows[1].state),
        ("client-schedule", "change")
    );
    schedule::apply(&rows, NEW, true, fake.as_ref());
    assert_eq!(
        *fake.made.borrow(),
        vec![(true, "04:30".to_owned(), NEW.to_owned())]
    );
}

#[test]
fn a_schedule_already_naming_the_daemon_is_current_and_none_is_ever_created() {
    let current = Rc::new(FakeScheduler {
        installed: vec![(
            true,
            Some("http://10.0.0.7:8765/"),
            Some("04:30"),
            true,
            None,
        )],
        ..FakeScheduler::default()
    });
    let rows = schedule_rows(&current, NEW, true);
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].state, "current");
    schedule::apply(&rows, NEW, true, current.as_ref());
    // With no schedule installed there is no row, and nothing is written.
    let none = Rc::new(FakeScheduler::default());
    let rows = schedule_rows(&none, NEW, true);
    assert!(rows.is_empty());
    schedule::apply(&rows, NEW, true, none.as_ref());
    assert!(current.made.borrow().is_empty() && none.made.borrow().is_empty());
}

#[test]
fn a_schedule_connect_cannot_re_create_as_written_is_left_with_the_command() {
    // Near miss beside each guard: the same schedule shaped as `--schedule`
    // writes it, with a URL and time, of the kind written here, re-creates.
    let shaped: Installed = (true, Some(OLD), Some("04:30"), true, None);
    for (installed, refuse_here, says) in [
        (
            (true, Some(OLD), Some("04:30"), false, None),
            None,
            "it is not exactly what `update --schedule` writes",
        ),
        (
            (true, None, Some("04:30"), false, None),
            None,
            "names no `--daemon-url` connect can read",
        ),
        (
            (
                true,
                Some(OLD),
                Some("04:30"),
                false,
                Some("no verified stable native launcher"),
            ),
            None,
            "cannot tell whether it is the one `update --schedule` writes (no verified stable native launcher)",
        ),
        (
            (true, Some(OLD), None, true, None),
            None,
            "its daily time could not be read",
        ),
        (
            shaped,
            Some((false, "no bearer resolves for the scheduled run".to_owned())),
            "connect cannot re-create it here (no bearer resolves for the scheduled run); fix that",
        ),
    ] {
        let fake = Rc::new(FakeScheduler {
            installed: vec![installed],
            refuse_here: refuse_here.clone(),
            ..FakeScheduler::default()
        });
        let rows = schedule_rows(&fake, NEW, true);
        assert_eq!(rows[0].state, "manual", "{says}");
        assert!(rows[0].detail.contains(says), "{}", rows[0].detail);
        let when = installed.2.unwrap_or("HH:MM");
        assert!(
            rows[0].detail.ends_with(&format!(
                "`pseudolife-mcp update --schedule {when} --daemon-url {NEW}`"
            )),
            "{}",
            rows[0].detail
        );
        schedule::apply(&rows, NEW, true, fake.as_ref());
        assert!(fake.made.borrow().is_empty());
    }
    let fake = Rc::new(FakeScheduler {
        installed: vec![shaped],
        ..FakeScheduler::default()
    });
    assert_eq!(schedule_rows(&fake, NEW, true)[0].state, "change");
}

#[test]
fn a_schedule_of_the_other_kind_names_its_removal_before_any_schedule_step() {
    // `--schedule` here would write the daemon's schedule beside this client
    // one: the fix removes exactly this one first, never adds a second.
    let fake = Rc::new(FakeScheduler {
        installed: vec![(true, Some(OLD), Some("04:30"), true, None)],
        refuse_here: Some((
            true,
            "this machine runs the daemon container, so `update --schedule` here writes the daemon's schedule instead".to_owned(),
        )),
        ..FakeScheduler::default()
    });
    let rows = schedule_rows(&fake, NEW, true);
    assert_eq!(rows[0].state, "manual");
    let detail = &rows[0].detail;
    let remove = detail
        .find("remove this one with `schtasks /Delete /TN \"client\" /F`")
        .expect(detail);
    let schedule = detail
        .find("pseudolife-mcp update --schedule")
        .expect(detail);
    assert!(remove < schedule, "{detail}");
    assert!(
        detail.contains("writes the daemon's schedule instead"),
        "{detail}"
    );
}

#[test]
fn a_hand_edited_url_is_a_manual_row_not_a_deferral() {
    // URLs the oracle's urlsplit would treat in ways the native port defers
    // on: an IPvFuture host, a character outside printable ASCII.
    for url in ["http://[v1.fe]:8765", "http://10.0.0.5:8765\u{e9}"] {
        let fake = Rc::new(FakeScheduler {
            installed: vec![(true, Some(url), Some("04:30"), false, None)],
            ..FakeScheduler::default()
        });
        let rows = schedule_rows(&fake, NEW, true);
        assert_eq!(rows[0].state, "manual", "{url}");
        assert!(
            rows[0].detail.contains("a daemon URL connect cannot show"),
            "{}",
            rows[0].detail
        );
    }
}

#[test]
fn a_schedule_that_cannot_be_re_created_says_what_changed_and_the_command_that_finishes_it() {
    let fake = Rc::new(FakeScheduler {
        installed: vec![(true, Some(OLD), Some("04:30"), true, None)],
        refuse_write: Some("Task Scheduler registration failed".to_owned()),
        ..FakeScheduler::default()
    });
    let rows = schedule_rows(&fake, NEW, true);
    let (notes, failures) = schedule::apply(&rows, NEW, true, fake.as_ref());
    assert!(notes.is_empty());
    assert_eq!(
        failures,
        vec![format!(
            "the client registrations now name {NEW} (backups above), but the scheduled task 'client' was not re-created (Task Scheduler registration failed); finish it with `pseudolife-mcp update --schedule 04:30 --daemon-url {NEW}`"
        )]
    );
    // With no client file re-pointed, it never claims the registrations moved.
    let (_, failures) = schedule::apply(&rows, NEW, false, fake.as_ref());
    assert!(
        failures[0].starts_with("the scheduled task 'client' was not re-created"),
        "{}",
        failures[0]
    );
}
