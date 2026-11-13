//! The cases of the frozen Python setup's tests
//! (`tests/test_codex_hook_setup_edges.py`, `tests/test_codex_hook_launcher.py`)
//! against the native step, plus the golden the Python output is pinned to.
use super::definitions::*;
use super::*;
use crate::cli::installer::fake_codex::{FakeCodex, THREAD};
use sha2::{Digest, Sha256};
use std::{
    cell::{Cell, RefCell},
    collections::BTreeMap,
    fs,
    rc::Rc,
    time::Duration,
};

fn checkout() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        // Private, as a profile folder is: the shared configuration writer
        // refuses a file another account may write.
        let path = std::env::temp_dir().join(format!("w7-hooks-{}", uuid::Uuid::new_v4().simple()));
        crate::board::state::private_dir(&path).unwrap();
        let path = crate::setup::codex::resolved_path(&path).unwrap();
        Self(path)
    }
    /// A Codex home whose path holds a space, so commands quote it.
    fn home(&self, name: &str) -> PathBuf {
        let home = self.0.join(name).join("w7 hooks").join("codex");
        fs::create_dir_all(&home).unwrap();
        crate::setup::codex::resolved_path(&home).unwrap()
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

// -- the golden recorded from the Python setup --------------------------------

fn golden() -> Value {
    let family = if cfg!(windows) { "windows" } else { "posix" };
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("src/cli/installer/codex_hooks/testdata")
        .join(format!("definitions-{family}.json"));
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

#[test]
fn the_definitions_are_the_python_setups_byte_for_byte() {
    let golden = golden();
    let definitions = golden["definitions"].as_object().unwrap();
    assert!(!definitions.is_empty());
    for (literal, expected) in definitions {
        let home = Path::new(literal);
        let bundle = hooks_root(home).join("0123456789abcdef0123");
        assert_eq!(
            Value::Object(launcher_definitions(home)),
            expected["launcher"],
            "{literal}"
        );
        assert_eq!(
            Value::Object(manual_definitions(&bundle, true)),
            expected["manual_marker"],
            "{literal}"
        );
        assert_eq!(
            Value::Object(manual_definitions(&bundle, false)),
            expected["manual_plain"],
            "{literal}"
        );
    }
}

fn substitute(value: &Value, from: &[(&str, String)]) -> Value {
    match value {
        Value::String(text) => {
            let mut text = text.clone();
            for (mark, real) in from {
                text = text.replace(mark, real);
            }
            Value::String(text)
        }
        Value::Array(items) => Value::Array(items.iter().map(|v| substitute(v, from)).collect()),
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| (k.clone(), substitute(v, from)))
                .collect(),
        ),
        other => other.clone(),
    }
}

#[test]
fn install_writes_the_python_setups_hooks_json_for_every_scenario() {
    let golden = golden();
    let legacy = legacy_commands(&checkout()).unwrap();
    for (name, scenario) in golden["scenarios"].as_object().unwrap() {
        let temp = Temp::new();
        let home = temp.home(name);
        let old = hooks_root(&home).join("0123456789abcdef0123");
        let home_text = home.to_string_lossy().into_owned();
        let old_text = old.to_string_lossy().into_owned();
        if !scenario["input"].is_null() {
            fs::create_dir_all(&old).unwrap();
            let given = substitute(
                &scenario["input"],
                &[("{OLD}", old_text.clone()), ("{HOME}", home_text.clone())],
            );
            fs::write(home.join("hooks.json"), serde_json::to_vec(&given).unwrap()).unwrap();
        }
        let plugin = scenario["plugin"] == true;
        let mut report = Report::default();
        manual::install(
            &home,
            &mut report,
            plugin,
            &checkout().join("plugin/hooks"),
            &legacy,
        )
        .unwrap();
        let first = fs::read(home.join("hooks.json")).unwrap();
        manual::install(
            &home,
            &mut Report::default(),
            plugin,
            &checkout().join("plugin/hooks"),
            &legacy,
        )
        .unwrap();
        assert_eq!(
            fs::read(home.join("hooks.json")).unwrap(),
            first,
            "{name}: a rerun changed hooks.json"
        );
        let written: Value = serde_json::from_slice(&first).unwrap();
        let back = substitute(
            &written,
            &[
                (old_text.as_str(), "{OLD}".into()),
                (home_text.as_str(), "{HOME}".into()),
            ],
        );
        assert_eq!(back, scenario["output"], "{name}");
        let mut text = String::from_utf8(first).unwrap();
        for (real, mark) in [(old_text.as_str(), "{OLD}"), (home_text.as_str(), "{HOME}")] {
            let escaped = serde_json::to_string(real).unwrap();
            text = text.replace(&escaped[1..escaped.len() - 1], mark);
        }
        assert_eq!(
            text,
            scenario["output_text"].as_str().unwrap(),
            "{name}: file bytes"
        );
        if !plugin {
            let pointer = fs::read_to_string(hooks_root(&home).join(POINTER)).unwrap();
            let files = bundle_bytes(&checkout().join("plugin/hooks"), &SCRIPTS).unwrap();
            assert_eq!(pointer.trim(), bundle_digest(&files));
            for (launcher, data) in LAUNCHERS {
                assert_eq!(fs::read(hooks_root(&home).join(launcher)).unwrap(), data);
            }
        }
        if !scenario["input"].is_null() {
            // A bundle from before the launcher is never removed: a running
            // session's commands may still name it.
            assert!(old.is_dir(), "{name}");
            assert!(
                !report.backups.is_empty(),
                "{name}: the displaced file is kept"
            );
        }
    }
}

#[test]
fn the_python_files_bytes_are_a_rerun_noop_for_the_native_install() {
    // A hooks.json the Python setup wrote is already what native writes.
    let temp = Temp::new();
    let home = temp.home("noop");
    let golden = golden();
    let text = golden["scenarios"]["fresh"]["output_text"]
        .as_str()
        .unwrap()
        .replace(
            "{HOME}",
            serde_json::to_string(&home.to_string_lossy())
                .unwrap()
                .trim_matches('"'),
        );
    fs::write(home.join("hooks.json"), &text).unwrap();
    let mut report = Report::default();
    let legacy = legacy_commands(&checkout()).unwrap();
    manual::install(
        &home,
        &mut report,
        false,
        &checkout().join("plugin/hooks"),
        &legacy,
    )
    .unwrap();
    assert_eq!(fs::read_to_string(home.join("hooks.json")).unwrap(), text);
    assert!(report.backups.is_empty());
}

// -- a fake host ------------------------------------------------------------------

/// The overrides and environment a session was started with.
type Opened = (Value, Vec<(String, Option<String>)>);

struct Shared {
    codex: RefCell<FakeCodex>,
    opened: RefCell<Vec<Opened>>,
    turned: Cell<bool>,
    closed_after_turn: Cell<bool>,
}

struct FakeSession {
    shared: Rc<Shared>,
    events: Vec<Value>,
}

impl Rpc for FakeSession {
    async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
        let result = {
            let mut codex = self.shared.codex.borrow_mut();
            let result = codex.call(method, params);
            self.events.append(&mut codex.pending);
            result
        };
        if method == "turn/start" {
            self.shared.turned.set(true);
        }
        result
    }
}

impl Session for FakeSession {
    fn events(&self) -> &[Value] {
        &self.events
    }
    async fn receive(&mut self, _timeout: Duration) -> Result<Value, SetupError> {
        Err(SetupError("fixture: no further messages"))
    }
    async fn close(&mut self) {
        if self.shared.turned.get() {
            self.shared.closed_after_turn.set(true);
        }
    }
}

struct Launcher(Rc<Shared>);
impl Codex for Launcher {
    type Session = FakeSession;
    async fn open(
        &self,
        overrides: &Value,
        environment: &[(String, Option<String>)],
    ) -> Result<FakeSession, SetupError> {
        self.0
            .opened
            .borrow_mut()
            .push((overrides.clone(), environment.to_vec()));
        self.0.codex.borrow_mut().session_overrides = overrides
            .as_object()
            .filter(|o| !o.is_empty())
            .map(|_| overrides.clone());
        Ok(FakeSession {
            shared: self.0.clone(),
            events: vec![],
        })
    }
}

#[derive(Clone)]
struct FakeDaemon {
    shared: Rc<Shared>,
    healthy: bool,
    checkin: bool,
    opens: bool,
    closes: bool,
}
impl Daemon for FakeDaemon {
    async fn healthy(&self) -> bool {
        self.healthy
    }
    async fn serves_checkin(&self) -> bool {
        self.checkin
    }
    async fn episode_open(&self, thread: &str) -> Result<bool, SetupError> {
        assert_eq!(thread, THREAD);
        Ok(self.opens
            && self.shared.turned.get()
            && !(self.closes && self.shared.closed_after_turn.get()))
    }
    fn health_wait(&self) -> Duration {
        Duration::ZERO
    }
    fn hook_wait(&self) -> Duration {
        Duration::from_millis(10)
    }
}

struct FakeHost {
    shared: Rc<Shared>,
    daemon: RefCell<Option<FakeDaemon>>,
    env: BTreeMap<String, String>,
    connected: RefCell<Option<(String, Option<String>)>>,
}

impl Host for FakeHost {
    type Codex = Launcher;
    type Daemon = FakeDaemon;
    fn codex(&self, _home: &Path, _cwd: &Path) -> Result<Launcher, Fail> {
        Ok(Launcher(self.shared.clone()))
    }
    fn daemon(&self, url: String, token: Option<String>) -> FakeDaemon {
        *self.connected.borrow_mut() = Some((url, token));
        self.daemon.borrow().clone().unwrap()
    }
    fn var(&self, key: &str) -> Option<String> {
        self.env.get(key).cloned()
    }
}

/// A Codex home with config.toml and hooks.json a user wrote, and a fake
/// Codex over it whose turn reports every hook healthy.
struct World {
    _temp: Temp,
    home: PathBuf,
    host: FakeHost,
    seeded: Vec<(PathBuf, Vec<u8>)>,
}

impl World {
    fn new() -> Self {
        let temp = Temp::new();
        let home = temp.home("world");
        let config = home.join("config.toml");
        fs::write(&config, b"# Preserve trust and preferences\n[hooks.state.other]\ntrusted_hash = \"user-hash\"\n").unwrap();
        let hooks = home.join("hooks.json");
        fs::write(
            &hooks,
            br#"{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"echo user"}]}]}}"#,
        )
        .unwrap();
        let seeded = [config, hooks]
            .into_iter()
            .map(|p| {
                let b = fs::read(&p).unwrap();
                (p, b)
            })
            .collect();
        let mut codex = FakeCodex::new(
            &home,
            json!({"mcp_servers": {"pseudolife-memory": {"command": "/opt/pseudolife-mcp", "args": [], "env": {}}}}),
        );
        codex.manual_from = Some(home.join("hooks.json"));
        let digests = temp.0.join("digests");
        codex.turn_mark =
            Some(digests.join(format!("{:x}.mark", Sha256::digest(THREAD.as_bytes()))));
        let shared = Rc::new(Shared {
            codex: RefCell::new(codex),
            opened: RefCell::new(vec![]),
            turned: Cell::new(false),
            closed_after_turn: Cell::new(false),
        });
        let daemon = FakeDaemon {
            shared: shared.clone(),
            healthy: true,
            checkin: false,
            opens: true,
            closes: true,
        };
        let env = BTreeMap::from([(
            "PSEUDOLIFE_DIGEST_DIR".to_owned(),
            digests.to_string_lossy().into_owned(),
        )]);
        let host = FakeHost {
            shared,
            daemon: RefCell::new(Some(daemon)),
            env,
            connected: RefCell::new(None),
        };
        let world = Self {
            _temp: temp,
            home,
            host,
            seeded,
        };
        world.healthy_turn(3, 2);
        world
    }
    fn codex(&self) -> std::cell::RefMut<'_, FakeCodex> {
        self.host.shared.codex.borrow_mut()
    }
    fn daemon(&self, change: impl FnOnce(&mut FakeDaemon)) {
        change(self.host.daemon.borrow_mut().as_mut().unwrap());
    }
    fn healthy_turn(&self, starts: usize, prompts: usize) {
        let mut runs = Vec::new();
        for _ in 0..starts {
            runs.push(json!({"eventName": "sessionStart", "status": "completed",
                "entries": [{"text": "Session episode: e1\nUse memory_agents(action=list) at the first task"}]}));
        }
        for _ in 0..prompts {
            runs.push(
                json!({"eventName": "userPromptSubmit", "status": "completed", "entries": []}),
            );
        }
        self.codex().turn_runs = runs;
    }
    /// The plugin as Codex lists it, from a copy of the checkout's hooks.
    fn plugin(&self) -> Vec<Value> {
        let directory = self.home.join("plugin").join("hooks");
        copy_dir(&checkout().join("plugin/hooks"), &directory);
        let manifest: Value =
            serde_json::from_slice(&fs::read(directory.join("hooks.json")).unwrap()).unwrap();
        let mut listed = Vec::new();
        for (event, groups) in manifest["hooks"].as_object().unwrap() {
            for (g, group) in groups.as_array().unwrap().iter().enumerate() {
                for (h, handler) in group["hooks"].as_array().unwrap().iter().enumerate() {
                    listed.push(json!({
                        "key": format!("plugin-{event}-{g}-{h}"), "currentHash": format!("sha256:{}", "a".repeat(64)),
                        "eventName": format!("{}{}", event[..1].to_lowercase(), &event[1..]),
                        "enabled": true, "trustStatus": "untrusted", "isManaged": false,
                        "sourcePath": directory.join("hooks.json"), "source": "plugin",
                        "pluginId": PLUGIN_ID, "handlerType": "command",
                        "command": handler["commandWindows"],
                    }));
                }
            }
        }
        listed
    }
    fn unchanged(&self) -> bool {
        self.seeded
            .iter()
            .all(|(path, bytes)| fs::read(path).unwrap() == *bytes)
    }
    async fn run(&self, source: &str, trust: &str, instructions: &str) -> Report {
        let mut options = Options {
            source: source.into(),
            trust: trust.into(),
            instructions: instructions.into(),
            non_interactive: true,
            checkout: checkout(),
        };
        let (approved, fallback) = consent(&mut options, false, &mut &b""[..], &mut Vec::new());
        setup(&options, &self.home, &self.host, approved, fallback).await
    }
    fn methods(&self) -> Vec<String> {
        self.codex().calls.iter().map(|(m, _)| m.clone()).collect()
    }
}

fn copy_dir(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        if entry.file_type().unwrap().is_file() {
            fs::copy(entry.path(), to.join(entry.file_name())).unwrap();
        }
    }
}

fn read_only(methods: &[String]) -> bool {
    methods
        .iter()
        .all(|m| m == "config/read" || m == "hooks/list")
}

// -- consent and the report ------------------------------------------------------------

#[test]
fn consent_never_reads_input_off_a_terminal_and_eof_is_not_approval() {
    let mut options = Options {
        source: "auto".into(),
        trust: "ask".into(),
        instructions: "auto".into(),
        non_interactive: false,
        checkout: checkout(),
    };
    assert_eq!(
        consent(
            &mut options.clone(),
            false,
            &mut &b"1\n"[..],
            &mut Vec::new()
        ),
        (false, false)
    );
    assert_eq!(
        consent(&mut options.clone(), true, &mut &b""[..], &mut Vec::new()),
        (false, false)
    );
    assert_eq!(
        consent(&mut options.clone(), true, &mut &b"\n"[..], &mut Vec::new()),
        (true, true)
    );
    assert_eq!(
        consent(&mut options, true, &mut &b"2\n"[..], &mut Vec::new()),
        (false, true)
    );
    let mut ask = Options {
        source: "auto".into(),
        trust: "ask".into(),
        instructions: "append".into(),
        non_interactive: false,
        checkout: checkout(),
    };
    let mut prompt = Vec::new();
    assert_eq!(
        consent(&mut ask, true, &mut &b"y\n"[..], &mut prompt),
        (true, true)
    );
    assert!(String::from_utf8(prompt).unwrap().ends_with("[y/N] "));
    let mut yes = Options {
        trust: "yes".into(),
        ..ask.clone()
    };
    assert_eq!(
        consent(&mut yes, false, &mut &b""[..], &mut Vec::new()),
        (true, true)
    );
}

#[test]
fn options_need_a_checkout_and_known_choices() {
    let strings =
        |items: &[&str]| -> Vec<String> { items.iter().map(|s| (*s).to_owned()).collect() };
    assert!(
        parse(&strings(&["--source", "auto"]))
            .unwrap_err()
            .contains("--checkout")
    );
    assert!(parse(&strings(&["--checkout", "/r", "--source", "both"])).is_err());
    assert!(parse(&strings(&["--checkout", "/r", "--trust", "maybe"])).is_err());
    assert!(
        parse(&strings(&[
            "--checkout",
            "/r",
            "--non-interactive",
            "--non-interactive"
        ]))
        .is_err()
    );
    let parsed = parse(&strings(&[
        "--checkout",
        "/r",
        "--instructions",
        "append",
        "--non-interactive",
    ]))
    .unwrap()
    .unwrap();
    assert_eq!(
        (
            parsed.source.as_str(),
            parsed.trust.as_str(),
            parsed.instructions.as_str()
        ),
        ("auto", "ask", "append")
    );
}

#[test]
fn the_report_keeps_the_fields_the_installers_read_and_its_exit_code() {
    let report = Report {
        source: "plugin".into(),
        status: "unavailable".into(),
        instructions: "appended".into(),
        recovery: Some("why".into()),
        ..Report::default()
    };
    let json = report.json();
    for key in ["status", "source", "instructions", "recovery", "backups"] {
        assert!(json.get(key).is_some(), "{key}");
    }
    assert_eq!(report.exit(), 0, "appended instructions are a success");
    let pending = Report {
        status: "pending".into(),
        instructions: "skipped".into(),
        ..Report::default()
    };
    assert_eq!(pending.exit(), 1);
    assert_eq!(pending.json()["recovery"], Value::Null);
}

// -- the edge cases -----------------------------------------------------------------

#[tokio::test]
async fn unapproved_setup_preserves_hook_files_and_trust() {
    for source in ["auto", "manual", "plugin"] {
        for trust in ["no", "ask"] {
            let world = World::new();
            if source == "plugin" {
                let plugin = world.plugin();
                world.codex().hooks = plugin;
            }
            let report = world.run(source, trust, "auto").await;
            assert_eq!(
                report.status, "pending",
                "{source}/{trust}: {:?}",
                report.recovery
            );
            assert_eq!(report.instructions, "skipped");
            assert!(world.unchanged(), "{source}/{trust}");
            assert!(!world.home.join("pseudolife").exists(), "{source}/{trust}");
            assert!(report.backups.is_empty());
            assert!(
                read_only(&world.methods()),
                "{source}/{trust}: {:?}",
                world.methods()
            );
            assert!(
                report
                    .recovery
                    .as_deref()
                    .is_some_and(|r| r.contains("not installed") || r.contains("await")),
                "{source}/{trust}: {:?}",
                report.recovery
            );
            assert!(!report.recovery.unwrap().contains("python"));
        }
    }
}

#[tokio::test]
async fn explicitly_disabled_hooks_survive_approval_and_get_the_fallback() {
    for disabled in ["feature", "hook"] {
        let world = World::new();
        let mut plugin = world.plugin();
        if disabled == "hook" {
            plugin[0]["enabled"] = json!(false);
            let first_event = plugin[0]["eventName"].clone();
            assert!(
                optional_most(first_event.as_str().unwrap()).is_none(),
                "the first handler is a lifecycle one"
            );
        } else {
            world.codex().overlay = json!({"features": {"hooks": false}});
        }
        world.codex().hooks = plugin;
        let report = world.run("auto", "yes", "auto").await;
        assert_eq!(report.status, "unavailable", "{disabled}");
        assert!(
            report.recovery.as_deref().unwrap().contains("disabled"),
            "{disabled}: {:?}",
            report.recovery
        );
        assert_eq!(report.instructions, "appended");
        assert!(
            fs::read_to_string(world.home.join("AGENTS.md"))
                .unwrap()
                .contains("memory_search")
        );
        assert!(world.unchanged());
        assert!(read_only(&world.methods()));
    }
}

#[tokio::test]
async fn an_unexpected_plugin_is_not_trusted_or_replaced() {
    for problem in ["partial", "script-mismatch"] {
        let world = World::new();
        let mut plugin = world.plugin();
        if problem == "partial" {
            plugin.retain(|h| h["eventName"] != "sessionEnd");
        } else {
            fs::write(
                world.home.join("plugin/hooks/lifecycle.ps1"),
                "Write-Output 'custom user script'\n",
            )
            .unwrap();
        }
        world.codex().hooks = plugin;
        let report = world.run("auto", "yes", "auto").await;
        assert_eq!(report.status, "unavailable", "{problem}");
        assert_eq!(report.source, "plugin");
        assert_eq!(report.instructions, "appended");
        assert!(world.unchanged());
        assert!(!world.home.join("pseudolife/hooks").exists());
        assert!(
            world.codex().writes().is_empty(),
            "{problem}: nothing trusted"
        );
    }
}

#[tokio::test]
async fn an_approved_plugin_setup_trusts_every_listed_hook_and_verifies() {
    let dropped_sets: [&[&str]; 4] = [
        &[],
        &["stop"],
        &["subagentStart", "subagentStop"],
        &["stop", "subagentStart", "subagentStop", "preToolUse"],
    ];
    for dropped in dropped_sets {
        let world = World::new();
        let mut plugin = world.plugin();
        plugin.retain(|h| !dropped.contains(&h["eventName"].as_str().unwrap()));
        let keys: Vec<String> = plugin
            .iter()
            .map(|h| h["key"].as_str().unwrap().to_owned())
            .collect();
        world.codex().hooks = plugin;
        let report = world.run("auto", "yes", "auto").await;
        assert_eq!(report.status, "ready", "{dropped:?}: {:?}", report.recovery);
        assert_eq!(report.source, "plugin");
        assert_eq!(report.instructions, "covered-by-hooks");
        let writes = world
            .codex()
            .writes()
            .into_iter()
            .cloned()
            .collect::<Vec<_>>();
        let mut written: Vec<String> = writes[0]["edits"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["keyPath"].as_str().unwrap().to_owned())
            .collect();
        let mut expected: Vec<String> = keys
            .iter()
            .map(|k| crate::setup::codex::dotted(&["hooks", "state", k, "trusted_hash"]))
            .collect();
        written.sort();
        expected.sort();
        assert_eq!(written, expected, "{dropped:?}");
        // The mailbox tool is approved with consent, nothing else.
        assert_eq!(report.mailbox_approval.as_deref(), Some("set"));
        assert_eq!(
            world.codex().user["mcp_servers"]["pseudolife-memory"]["tools"]["memory_message"]["approval_mode"],
            "approve"
        );
        assert!(
            report
                .mailbox_approval_notice
                .unwrap()
                .contains("No other tool was approved")
        );
        // The check ran in an isolated runtime: other servers off.
        let opened = world.host.shared.opened.borrow();
        let (overrides, _) = opened.last().unwrap();
        assert_eq!(
            overrides["mcp_servers"]["pseudolife-memory"],
            json!({"enabled": false})
        );
        assert_eq!(
            overrides["model_providers.pseudolife_hook_fixture.base_url"],
            "http://127.0.0.1:1/v1"
        );
        // The prompt hook's cursor was removed after the check.
        assert!(!world.codex().turn_mark.clone().unwrap().exists());
    }
}

#[tokio::test]
async fn a_disabled_optional_handler_does_not_block_setup() {
    for disabled in [
        &["stop"][..],
        &["subagentStart", "subagentStop"],
        &["preToolUse"],
    ] {
        let world = World::new();
        let mut plugin = world.plugin();
        let mut off = Vec::new();
        for hook in plugin.iter_mut() {
            if disabled.contains(&hook["eventName"].as_str().unwrap()) {
                hook["enabled"] = json!(false);
                off.push(hook["key"].as_str().unwrap().to_owned());
            }
        }
        let total = plugin.len();
        world.codex().hooks = plugin;
        let report = world.run("auto", "yes", "auto").await;
        assert_eq!(
            report.status, "ready",
            "{disabled:?}: {:?}",
            report.recovery
        );
        let writes = world
            .codex()
            .writes()
            .into_iter()
            .cloned()
            .collect::<Vec<_>>();
        let edits = writes[0]["edits"].as_array().unwrap();
        assert_eq!(edits.len(), total - off.len());
        assert!(edits.iter().all(|e| {
            off.iter()
                .all(|k| !e["keyPath"].as_str().unwrap().contains(k.as_str()))
        }));
    }
}

#[tokio::test]
async fn an_optional_handler_listed_twice_or_too_many_handlers_are_refused() {
    let world = World::new();
    let mut plugin = world.plugin();
    let stop = plugin
        .iter()
        .find(|h| h["eventName"] == "stop")
        .unwrap()
        .clone();
    let mut twice = stop.clone();
    twice["key"] = json!("plugin-Stop-9-9");
    plugin.push(twice);
    world.codex().hooks = plugin;
    let report = world.run("auto", "yes", "auto").await;
    assert_eq!(report.status, "unavailable", "{:?}", report.recovery);
    assert!(world.codex().writes().is_empty());
}

#[tokio::test]
async fn plugin_commands_must_keep_their_manifest_roles() {
    let world = World::new();
    let mut plugin = world.plugin();
    let start = plugin
        .iter()
        .position(|h| h["eventName"] == "sessionStart")
        .unwrap();
    let prompt = plugin
        .iter()
        .position(|h| h["eventName"] == "userPromptSubmit")
        .unwrap();
    let (a, b) = (
        plugin[start]["command"].clone(),
        plugin[prompt]["command"].clone(),
    );
    plugin[start]["command"] = b;
    plugin[prompt]["command"] = a;
    world.codex().hooks = plugin;
    let report = world.run("auto", "yes", "auto").await;
    assert!(
        report.recovery.as_deref().unwrap().contains("differ"),
        "{:?}",
        report.recovery
    );
}

#[tokio::test]
async fn plugin_commands_may_name_either_platform_and_an_expanded_root() {
    for (field, expand) in [
        ("commandWindows", false),
        ("command", false),
        ("commandWindows", true),
        ("command", true),
    ] {
        let world = World::new();
        let mut plugin = world.plugin();
        let manifest: Value =
            serde_json::from_slice(&fs::read(world.home.join("plugin/hooks/hooks.json")).unwrap())
                .unwrap();
        let root = world.home.join("plugin").to_string_lossy().into_owned();
        for hook in plugin.iter_mut() {
            let key = hook["key"].as_str().unwrap().to_owned();
            let parts: Vec<&str> = key.rsplitn(3, '-').collect();
            let (h, g) = (
                parts[0].parse::<usize>().unwrap(),
                parts[1].parse::<usize>().unwrap(),
            );
            let event = parts[2].trim_start_matches("plugin-");
            let mut command = manifest["hooks"][event][g]["hooks"][h][field]
                .as_str()
                .unwrap()
                .to_owned();
            if expand {
                command = command
                    .replace("${CLAUDE_PLUGIN_ROOT}", &root)
                    .replace("$env:CLAUDE_PLUGIN_ROOT", &root);
            }
            hook["command"] = json!(command);
        }
        world.codex().hooks = plugin;
        let report = world.run("auto", "yes", "auto").await;
        assert_eq!(
            report.status, "ready",
            "{field}/{expand}: {:?}",
            report.recovery
        );
    }
}

#[tokio::test]
async fn manual_setup_installs_trusts_and_verifies_then_a_rerun_is_quiet() {
    let world = World::new();
    let report = world.run("manual", "yes", "auto").await;
    assert_eq!(report.status, "ready", "{:?}", report.recovery);
    assert_eq!(report.source, "manual");
    let root = hooks_root(&world.home);
    let current = fs::read_to_string(root.join(POINTER)).unwrap();
    assert!(is_bundle_name(current.trim()));
    let hooks: Value =
        serde_json::from_slice(&fs::read(world.home.join("hooks.json")).unwrap()).unwrap();
    // The user's own hook stays first; ours follow in role order.
    assert_eq!(
        hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"],
        "echo user"
    );
    assert_eq!(hooks["hooks"]["SessionStart"].as_array().unwrap().len(), 4);
    let first_writes = world.codex().writes().len();
    assert!(first_writes >= 1);
    // A rerun: nothing to install, trust or approve; still ready.
    world.host.shared.turned.set(false);
    world.host.shared.closed_after_turn.set(false);
    let again = world.run("manual", "yes", "auto").await;
    assert_eq!(again.status, "ready", "{:?}", again.recovery);
    assert_eq!(
        world.codex().writes().len(),
        first_writes,
        "a rerun wrote config"
    );
    assert_eq!(again.mailbox_approval.as_deref(), Some("already"));
    assert!(again.backups.is_empty(), "{:?}", again.backups);
}

#[tokio::test]
async fn verification_counts_every_selected_hook_and_needs_the_prompt_cursor() {
    let world = World::new();
    world.healthy_turn(2, 2);
    let report = world.run("manual", "yes", "auto").await;
    assert_eq!(report.status, "unavailable");
    assert!(
        report
            .recovery
            .as_deref()
            .unwrap()
            .contains("SessionStart did not return"),
        "{:?}",
        report.recovery
    );
    assert_eq!(
        report.instructions, "appended",
        "the fallback follows a failed check"
    );

    let world = World::new();
    world.codex().turn_mark = None;
    let report = world.run("manual", "yes", "auto").await;
    assert!(
        report
            .recovery
            .as_deref()
            .unwrap()
            .contains("UserPromptSubmit did not return"),
        "{:?}",
        report.recovery
    );
}

#[tokio::test]
async fn verification_expects_the_checkin_only_where_the_board_works_and_the_episode_closes() {
    let world = World::new();
    world.daemon(|d| d.checkin = true);
    world.healthy_turn(3, 2);
    world
        .codex()
        .turn_runs
        .iter_mut()
        .for_each(|r| r["entries"] = json!([{"text": "Session episode: e1"}]));
    let report = world.run("manual", "yes", "auto").await;
    assert!(
        report
            .recovery
            .as_deref()
            .unwrap()
            .contains("CoordinationStart"),
        "{:?}",
        report.recovery
    );

    let mut world = World::new();
    world.daemon(|d| d.checkin = true);
    world
        .host
        .env
        .insert("PSEUDOLIFE_AGENT_COORDINATION".into(), "0".into());
    world
        .codex()
        .turn_runs
        .iter_mut()
        .for_each(|r| r["entries"] = json!([{"text": "Session episode: e1"}]));
    let report = world.run("manual", "yes", "auto").await;
    assert_eq!(
        report.status, "ready",
        "an opt-out expects no check-in: {:?}",
        report.recovery
    );

    let world = World::new();
    world.daemon(|d| d.closes = false);
    let report = world.run("manual", "yes", "auto").await;
    assert!(
        report
            .recovery
            .as_deref()
            .unwrap()
            .contains("SessionEnd did not close"),
        "{:?}",
        report.recovery
    );

    let world = World::new();
    world.daemon(|d| d.healthy = false);
    let report = world.run("manual", "yes", "auto").await;
    assert!(
        report
            .recovery
            .as_deref()
            .unwrap()
            .contains("memory daemon is not ready"),
        "{:?}",
        report.recovery
    );
}

#[tokio::test]
async fn legacy_commands_migrate_exactly_and_lookalikes_stay() {
    let world = World::new();
    let legacy = legacy_commands(&checkout()).unwrap();
    let hooks = json!({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": legacy[0]}]},
                         {"hooks": [{"type": "command", "command": format!("{} --extra", legacy[0])}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "pseudolife-mcp prompt-hook"}]}],
    }});
    fs::write(
        world.home.join("hooks.json"),
        serde_json::to_vec(&hooks).unwrap(),
    )
    .unwrap();
    let report = world.run("manual", "yes", "auto").await;
    assert_eq!(report.status, "ready", "{:?}", report.recovery);
    let written: Value =
        serde_json::from_slice(&fs::read(world.home.join("hooks.json")).unwrap()).unwrap();
    let commands: Vec<String> = written["hooks"]
        .as_object()
        .unwrap()
        .values()
        .flat_map(|g| g.as_array().unwrap().clone())
        .flat_map(|g| g["hooks"].as_array().unwrap().clone())
        .map(|h| h["command"].as_str().unwrap().to_owned())
        .collect();
    assert!(!commands.contains(&legacy[0]));
    assert!(!commands.contains(&"pseudolife-mcp prompt-hook".to_owned()));
    assert!(
        commands.contains(&format!("{} --extra", legacy[0])),
        "a lookalike stays"
    );
}

#[tokio::test]
async fn a_custom_platform_override_stops_migration_without_duplicates() {
    for custom in ["command", "commandWindows"] {
        let world = World::new();
        let legacy = legacy_commands(&checkout()).unwrap();
        let mut handler =
            json!({"type": "command", "command": legacy[0], "commandWindows": legacy[0]});
        handler[custom] = json!("custom user command");
        let hooks = json!({"hooks": {"SessionStart": [{"hooks": [handler]}]}});
        let bytes = serde_json::to_vec(&hooks).unwrap();
        fs::write(world.home.join("hooks.json"), &bytes).unwrap();
        let report = world.run("manual", "yes", "auto").await;
        assert_eq!(report.status, "unavailable", "{custom}");
        assert!(
            report
                .recovery
                .as_deref()
                .unwrap()
                .contains("custom platform command"),
            "{custom}: {:?}",
            report.recovery
        );
        assert_eq!(fs::read(world.home.join("hooks.json")).unwrap(), bytes);
    }
}

#[tokio::test]
async fn an_unknown_or_unversioned_config_api_does_not_write() {
    for layers in [
        json!([]),
        json!([{"name": {"type": "user", "file": "unused"}}]),
    ] {
        let world = World::new();
        world.codex().layers = Some(layers);
        let report = world.run("manual", "yes", "auto").await;
        assert_eq!(report.status, "unavailable", "{:?}", report.recovery);
        assert!(world.codex().writes().is_empty());
    }
}

#[tokio::test]
async fn skip_appends_only_on_request_and_never_twice() {
    let world = World::new();
    let report = world.run("skip", "no", "append").await;
    assert_eq!(
        (report.status.as_str(), report.instructions.as_str()),
        ("skipped", "appended")
    );
    assert_eq!(report.exit(), 0);
    let again = world.run("skip", "no", "append").await;
    assert_eq!(again.instructions, "present");
    let skipped = World::new().run("skip", "no", "auto").await;
    assert_eq!(skipped.instructions, "skipped");
    assert_eq!(skipped.exit(), 1);
    assert!(
        world.host.shared.opened.borrow().is_empty(),
        "skip never starts Codex"
    );
}

// -- the launcher, bundle and pointer ---------------------------------------------------

fn installed_world() -> World {
    let world = World::new();
    let legacy = legacy_commands(&checkout()).unwrap();
    manual::install(
        &world.home,
        &mut Report::default(),
        false,
        &checkout().join("plugin/hooks"),
        &legacy,
    )
    .unwrap();
    world
}

fn listed_manual(world: &World) -> Vec<Hook> {
    let listing = world.codex().call("hooks/list", json!({})).unwrap();
    listing["data"][0]["hooks"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Hook::parse)
        .filter(|h| manual::owned(h, &world.home))
        .collect()
}

#[test]
fn verification_refuses_a_changed_launcher_bundle_or_pointer() {
    for damage in ["launcher", "bundle", "pointer", "missing-bundle"] {
        let world = installed_world();
        let root = hooks_root(&world.home);
        let current = fs::read_to_string(root.join(POINTER))
            .unwrap()
            .trim()
            .to_owned();
        match damage {
            "launcher" => fs::write(root.join("run.sh"), "echo changed\n").unwrap(),
            "bundle" => fs::write(
                root.join(&current).join("session-start.sh"),
                "echo changed\n",
            )
            .unwrap(),
            "pointer" => fs::write(root.join(POINTER), "../../elsewhere\n").unwrap(),
            _ => fs::remove_dir_all(root.join(&current)).unwrap(),
        }
        let hooks = listed_manual(&world);
        let refs: Vec<&Hook> = hooks.iter().collect();
        assert_eq!(refs.len(), 6, "{damage}");
        assert!(manual::vet(&refs, &world.home, true).is_err(), "{damage}");
    }
}

#[test]
fn an_approved_setup_repairs_the_launcher_and_pointer() {
    for damage in ["launcher", "pointer", "missing-pointer"] {
        let world = installed_world();
        let root = hooks_root(&world.home);
        match damage {
            "launcher" => fs::write(root.join("run.ps1"), "# edited\n").unwrap(),
            "pointer" => fs::write(root.join(POINTER), "not-a-bundle\n").unwrap(),
            _ => fs::remove_file(root.join(POINTER)).unwrap(),
        }
        let hooks = listed_manual(&world);
        let refs: Vec<&Hook> = hooks.iter().collect();
        manual::vet(&refs, &world.home, false).unwrap_or_else(|e| panic!("{damage}: {e}"));
        let legacy = legacy_commands(&checkout()).unwrap();
        manual::install(
            &world.home,
            &mut Report::default(),
            false,
            &checkout().join("plugin/hooks"),
            &legacy,
        )
        .unwrap();
        manual::vet(&refs, &world.home, true).unwrap_or_else(|e| panic!("{damage}: {e}"));
    }
}

#[test]
fn an_old_bundle_the_checkout_matches_is_never_pruned() {
    let world = World::new();
    let files = bundle_bytes(&checkout().join("plugin/hooks"), &SCRIPTS).unwrap();
    let bundle = hooks_root(&world.home).join(bundle_digest(&files));
    fs::create_dir_all(&bundle).unwrap();
    for (name, data) in &files {
        fs::write(bundle.join(name), data).unwrap();
    }
    let old = manual_definitions(&bundle, true);
    let hooks = json!({"hooks": {
        "SessionStart": manual_roles("sessionStart").iter().map(|r| json!({"hooks": [old[*r]]})).collect::<Vec<_>>(),
        "UserPromptSubmit": manual_roles("userPromptSubmit").iter().map(|r| json!({"hooks": [old[*r]]})).collect::<Vec<_>>(),
        "SessionEnd": [{"hooks": [old["SessionEnd"]]}],
    }});
    fs::write(
        world.home.join("hooks.json"),
        serde_json::to_vec(&hooks).unwrap(),
    )
    .unwrap();
    let legacy = legacy_commands(&checkout()).unwrap();
    manual::install(
        &world.home,
        &mut Report::default(),
        false,
        &checkout().join("plugin/hooks"),
        &legacy,
    )
    .unwrap();
    assert_eq!(
        fs::read_to_string(hooks_root(&world.home).join(POINTER))
            .unwrap()
            .trim(),
        bundle.file_name().unwrap().to_string_lossy()
    );
    // It was not written by this call, so it is not a launcher bundle.
    assert!(
        fs::read_to_string(hooks_root(&world.home).join(LAUNCHED))
            .unwrap_or_default()
            .trim()
            .is_empty()
    );
}
