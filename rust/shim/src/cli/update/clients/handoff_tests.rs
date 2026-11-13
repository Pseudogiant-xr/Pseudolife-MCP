use super::*;
use crate::cli::update::clients::command::Output;
use std::cell::RefCell;

const OWN: &str = "1.0.0";
const TARGET: &str = "9.9.9";
const URL: &str = "http://127.0.0.1:8765";

struct Fixture {
    path: PathBuf,
    layout: runtime::Layout,
}
impl Fixture {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("native-handoff-test-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&path).unwrap();
        let layout = runtime::Layout {
            root: path.join("runtimes"),
            launcher: path.join("bin").join(runtime::BINARY),
            user_bin: None,
            user_path: false,
        };
        Self { path, layout }
    }
    /// A verified native runtime directory of `version` (a header-only
    /// executable: the fakes answer for it, nothing runs it).
    fn runtime(&self, version: &str) -> runtime::Runtime {
        let binary = self.path.join(format!("fixture-{version}"));
        #[cfg(windows)]
        let header = b"MZ".as_slice();
        #[cfg(target_os = "linux")]
        let header = b"\x7fELF".as_slice();
        #[cfg(target_os = "macos")]
        let header = b"\xcf\xfa\xed\xfe".as_slice();
        let mut bytes = header.to_vec();
        bytes.extend_from_slice(version.as_bytes());
        fs::write(&binary, bytes).unwrap();
        let _lock = runtime::UpdateLock::acquire(&self.layout).unwrap();
        runtime::install_binary(
            &self.layout,
            version,
            &format!("pseudolife-mcp=={version}"),
            None,
            &binary,
        )
        .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

fn shim(state: &str, runtime: &runtime::Runtime) -> Value {
    json!({"state": state, "detail": "runtime installed", "runtime": runtime.path})
}

fn parent(allow_downgrade: bool) -> Parent<'static> {
    Parent {
        own_version: OWN,
        daemon_url: URL,
        allow_downgrade,
        quiet: true,
        retry_program: "pseudolife-mcp-old".into(),
        pid: 4242,
        runtime_dir: Some(PathBuf::from("/runtimes/000001")),
    }
}

/// Answers the two probes; `protocols` is the second probe's (code, text).
struct Probe {
    start: (i32, String),
    protocols: (i32, String),
    calls: RefCell<Vec<Vec<String>>>,
}
impl Probe {
    fn new(protocols: (i32, &str)) -> Self {
        Self {
            start: (0, "pseudolife-mcp native-update-v1\n".into()),
            protocols: (protocols.0, protocols.1.into()),
            calls: RefCell::new(vec![]),
        }
    }
    fn current() -> Self {
        Self::new((0, &format!("{TARGET}\n{CLIENT_HANDOFF}\n")))
    }
}
impl Runner for Probe {
    fn run(
        &self,
        _executable: &Path,
        args: &[&str],
        _cwd: Option<&Path>,
        _timeout: Duration,
        _env: &[(&str, &str)],
    ) -> Output {
        self.calls
            .borrow_mut()
            .push(args.iter().map(|s| s.to_string()).collect());
        let (code, text) = match args {
            ["update", START_PROBE] => self.start.clone(),
            ["update", PROBE] => self.protocols.clone(),
            other => panic!("unexpected probe {other:?}"),
        };
        Output {
            code,
            bytes: text.into_bytes(),
        }
    }
}

type Call = (PathBuf, Vec<String>, Vec<(String, OsString)>, bool);
struct Spawn<F> {
    calls: RefCell<Vec<Call>>,
    report_seen: RefCell<Option<PathBuf>>,
    f: F,
}
impl<F: Fn(&Path) -> Result<i32, String>> Spawn<F> {
    fn new(f: F) -> Self {
        Self {
            calls: RefCell::new(vec![]),
            report_seen: RefCell::new(None),
            f,
        }
    }
}
impl<F: Fn(&Path) -> Result<i32, String>> Spawner for Spawn<F> {
    fn run(
        &self,
        executable: &Path,
        args: &[String],
        env: &[(String, OsString)],
        quiet: bool,
    ) -> Result<i32, String> {
        self.calls
            .borrow_mut()
            .push((executable.to_owned(), args.to_vec(), env.to_vec(), quiet));
        let report = env
            .iter()
            .find(|(k, _)| k == HANDOFF_REPORT_ENV)
            .map(|(_, v)| PathBuf::from(v))
            .expect("the child is told where to write its report");
        *self.report_seen.borrow_mut() = Some(report.clone());
        (self.f)(&report)
    }
}

fn writes(report: Value, code: i32) -> impl Fn(&Path) -> Result<i32, String> {
    move |path: &Path| {
        fs::write(path, serde_json::to_vec(&report).unwrap()).unwrap();
        Ok(code)
    }
}

fn run(
    shim: &Value,
    parent: &Parent,
    probe: &Probe,
    spawn: &impl Spawner,
) -> (Option<(Value, Option<Outcome>)>, Vec<String>) {
    let mut notes = vec![];
    let result = hand_off(shim, parent, probe, spawn, &mut |note| match note {
        Note::Step(text) => notes.push(text),
        Note::Shim(row) => notes.push(format!("SHIM {}", row["state"])),
    });
    (result, notes)
}

fn child_report() -> Value {
    json!({"tier":"client","mode":"clients","target":TARGET,"current":TARGET,
        "steps":["continuing the client side","WARNING: something minor"],
        "rollback":{},"codex_reapproval":"approve the hooks again",
        "clients":{"shim":{"state":format!("current:{TARGET}"),"detail":"already installed"},
                   "plugin":{"state":"current","detail":"plugin cache current"},"ok":true},
        "ok":true,"exit_code":0})
}

#[test]
fn hand_off_runs_the_remaining_steps_in_the_new_runtime() {
    let f = Fixture::new();
    let runtime = f.runtime(TARGET);
    let shim = shim(&format!("installed:{TARGET}"), &runtime);
    let probe = Probe::current();
    let spawn = Spawn::new(writes(child_report(), 0));
    let (result, notes) = run(&shim, &parent(false), &probe, &spawn);
    let (ladder, outcome) = result.expect("handed off");
    // The install this run did, not the child's "already installed".
    assert_eq!(ladder["shim"], shim);
    assert_eq!(ladder["handoff"]["state"], format!("handed-off:{TARGET}"));
    assert_eq!(ladder["plugin"]["state"], "current");
    assert_eq!(ladder["ok"], true);
    let outcome = outcome.unwrap();
    assert_eq!(outcome.exit_code, 0);
    assert_eq!(outcome.version, TARGET);
    assert_eq!(outcome.codex, "approve the hooks again");
    assert_eq!(outcome.steps.len(), 2);
    assert_eq!(notes.len(), 2);
    assert!(notes[0].starts_with("SHIM"));
    assert!(notes[1].contains("continue in runtime"), "{notes:?}");
    let calls = spawn.calls.borrow();
    let (executable, args, env, quiet) = &calls[0];
    assert_eq!(executable, &runtime.executable());
    assert_eq!(
        args,
        &[
            "update",
            "--clients-only",
            "--tag",
            TARGET,
            "--daemon-url",
            URL
        ]
    );
    assert!(*quiet);
    let get = |key: &str| env.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone());
    assert_eq!(get(HANDOFF_ENV), Some("4242".into()));
    assert_eq!(get(HANDOFF_RUNTIME_ENV), Some("/runtimes/000001".into()));
    // The report was read, then its private scratch directory removed.
    let report = spawn.report_seen.borrow().clone().unwrap();
    assert!(!report.exists());
    assert!(!report.parent().unwrap().exists());
}

#[test]
fn allow_downgrade_is_forwarded_and_reinstall_and_result_file_never_are() {
    let f = Fixture::new();
    let runtime = f.runtime(TARGET);
    let probe = Probe::current();
    let spawn = Spawn::new(writes(child_report(), 0));
    run(
        &shim(&format!("installed:{TARGET}"), &runtime),
        &parent(true),
        &probe,
        &spawn,
    );
    let args = spawn.calls.borrow()[0].1.clone();
    assert_eq!(args.last().map(String::as_str), Some("--allow-downgrade"));
    assert!(
        !args
            .iter()
            .any(|a| a.contains("reinstall") || a.contains("result-file")),
        "{args:?}"
    );
    assert_eq!(child_args(TARGET, URL, false).len(), 6);
}

#[test]
fn a_child_that_cannot_start_fails_with_the_cause_and_the_retry_and_no_fallback() {
    let f = Fixture::new();
    let runtime = f.runtime(TARGET);
    let probe = Probe::current();
    let spawn = Spawn::new(|_: &Path| Err("the runtime's executable is missing".into()));
    let (result, _) = run(
        &shim(&format!("installed:{TARGET}"), &runtime),
        &parent(false),
        &probe,
        &spawn,
    );
    let (ladder, outcome) = result.expect("a failed hand-off is still the ladder");
    assert!(outcome.is_none());
    assert_eq!(ladder["ok"], false);
    assert_eq!(ladder["handoff"]["state"], "failed");
    let detail = ladder["handoff"]["detail"].as_str().unwrap();
    assert!(
        detail.contains("the runtime's executable is missing"),
        "{detail}"
    );
    let url = crate::cli::update::seams::quote(URL);
    assert!(
        detail.contains(&format!(
            "pseudolife-mcp-old update --clients-only --tag {TARGET} --daemon-url {url} --reinstall"
        )),
        "{detail}"
    );
    // A downgrade this run was allowed stays allowed in its retry.
    let spawn = Spawn::new(|_: &Path| Err("missing".into()));
    let (result, _) = run(
        &shim(&format!("installed:{TARGET}"), &runtime),
        &parent(true),
        &probe,
        &spawn,
    );
    let ladder = result.unwrap().0;
    let detail = ladder["handoff"]["detail"].as_str().unwrap();
    assert!(
        detail.contains(&format!("--daemon-url {url} --allow-downgrade --reinstall")),
        "{detail}"
    );
    // The old binary's plugin, Codex and tunnel steps never stand in.
    for step in ["plugin", "codex", "tunnel"] {
        assert!(ladder.get(step).is_none(), "{step} ran in the old binary");
    }
}

#[test]
fn a_runtime_that_does_not_start_is_never_handed_the_steps() {
    let f = Fixture::new();
    let runtime = f.runtime(TARGET);
    let mut probe = Probe::current();
    probe.start = (1, "error while loading shared libraries\n".into());
    let spawn = Spawn::new(writes(child_report(), 0));
    let (result, _) = run(
        &shim(&format!("installed:{TARGET}"), &runtime),
        &parent(false),
        &probe,
        &spawn,
    );
    let (ladder, _) = result.unwrap();
    assert_eq!(ladder["handoff"]["state"], "failed");
    assert!(
        ladder["handoff"]["detail"]
            .as_str()
            .unwrap()
            .contains("exit 1: error while loading shared libraries")
    );
    assert!(spawn.calls.borrow().is_empty());
}

#[test]
fn a_target_that_predates_the_protocol_is_finished_here_with_one_line() {
    let f = Fixture::new();
    let runtime = f.runtime(TARGET);
    for answer in [
        (
            2,
            "unrecognized argument: --client-handoff-protocols\n".to_owned(),
        ),
        (0, format!("{TARGET}\n\n")),
        (0, format!("{TARGET}\nclient-handoff-v0\n")),
    ] {
        let probe = Probe::new((answer.0, &answer.1));
        let spawn = Spawn::new(writes(child_report(), 0));
        let (result, notes) = run(
            &shim(&format!("installed:{TARGET}"), &runtime),
            &parent(false),
            &probe,
            &spawn,
        );
        assert!(result.is_none(), "{answer:?}");
        assert_eq!(notes.len(), 1, "{notes:?}");
        assert!(
            notes[0].contains("predates the client hand-off")
                && notes[0].contains(&format!("({OWN})")),
            "{notes:?}"
        );
        assert!(spawn.calls.borrow().is_empty());
    }
}

#[test]
fn no_hand_off_without_another_runtime() {
    let f = Fixture::new();
    let same = f.runtime(OWN);
    let probe = Probe::current();
    let spawn = Spawn::new(writes(child_report(), 0));
    for shim in [
        shim(&format!("current:{OWN}"), &same),
        shim("failed", &same),
        json!({"state":"not-registered","detail":"no registration"}),
        json!({"state":"failed","detail":"native shim update failed"}),
        json!({"state":"editable","detail":"left","runtime":"/elsewhere"}),
    ] {
        let (result, notes) = run(&shim, &parent(false), &probe, &spawn);
        assert!(result.is_none(), "{shim}");
        assert!(notes.is_empty());
    }
    assert!(probe.calls.borrow().is_empty());
    assert!(spawn.calls.borrow().is_empty());
}

#[test]
fn a_failed_shim_step_that_installed_the_runtime_still_hands_off() {
    let f = Fixture::new();
    let runtime = f.runtime(TARGET);
    let probe = Probe::current();
    let mut child = child_report();
    child["clients"]["shim"]["state"] = json!(format!("installed:{TARGET}"));
    let spawn = Spawn::new(writes(child, 0));
    let (result, _) = run(&shim("failed", &runtime), &parent(false), &probe, &spawn);
    let (ladder, _) = result.unwrap();
    // The child's retry of the shim step keeps its own row.
    assert_eq!(ladder["shim"]["state"], format!("installed:{TARGET}"));
    assert_eq!(spawn.calls.borrow().len(), 1);
}

#[test]
fn a_probe_reporting_another_version_fails() {
    let f = Fixture::new();
    let runtime = f.runtime(TARGET);
    let probe = Probe::new((0, &format!("8.0.0\n{CLIENT_HANDOFF}\n")));
    let spawn = Spawn::new(writes(child_report(), 0));
    let (result, _) = run(
        &shim(&format!("installed:{TARGET}"), &runtime),
        &parent(false),
        &probe,
        &spawn,
    );
    let detail = result.unwrap().0["handoff"]["detail"].to_string();
    assert!(detail.contains("not 9.9.9"), "{detail}");
    assert!(spawn.calls.borrow().is_empty());
}

#[test]
fn a_child_without_a_report_or_without_its_client_steps_fails() {
    let f = Fixture::new();
    let runtime = f.runtime(TARGET);
    let probe = Probe::current();
    let silent = Spawn::new(|_: &Path| Ok(3));
    let (result, _) = run(
        &shim(&format!("installed:{TARGET}"), &runtime),
        &parent(false),
        &probe,
        &silent,
    );
    let (ladder, outcome) = result.unwrap();
    assert!(outcome.is_none());
    assert!(
        ladder["handoff"]["detail"]
            .as_str()
            .unwrap()
            .contains("ended with exit 3 without writing its report")
    );
    let mut stopped = child_report();
    stopped["clients"] = Value::Null;
    stopped["steps"] = json!(["WARNING: another pseudolife-mcp update is running (pid 7)"]);
    let early = Spawn::new(writes(stopped, 2));
    let (result, _) = run(
        &shim(&format!("installed:{TARGET}"), &runtime),
        &parent(false),
        &probe,
        &early,
    );
    let (ladder, outcome) = result.unwrap();
    assert_eq!(ladder["ok"], false);
    let detail = ladder["handoff"]["detail"].as_str().unwrap();
    assert!(
        detail.contains("started but stopped before the client steps (exit 2): another pseudolife-mcp update is running (pid 7)"),
        "{detail}"
    );
    assert_eq!(outcome.unwrap().exit_code, 2);
}

#[test]
fn a_failing_child_carries_its_exit_code_and_fails_the_ladder() {
    let f = Fixture::new();
    let runtime = f.runtime(TARGET);
    let probe = Probe::current();
    let mut child = child_report();
    child["ok"] = json!(false);
    child["clients"]["plugin"] = json!({"state":"failed","detail":"plugin cache refresh failed"});
    let spawn = Spawn::new(writes(child, 5));
    let (result, _) = run(
        &shim(&format!("installed:{TARGET}"), &runtime),
        &parent(false),
        &probe,
        &spawn,
    );
    let (ladder, outcome) = result.unwrap();
    assert_eq!(ladder["ok"], false);
    assert_eq!(ladder["plugin"]["state"], "failed");
    assert_eq!(outcome.unwrap().exit_code, 5);
}

#[test]
fn only_an_all_digit_pid_marks_a_handed_off_run() {
    let got = inherited(Some("4242".into()), Some("/tmp/report.json".into()));
    assert_eq!(got.from.as_deref(), Some("4242"));
    assert_eq!(got.report, Some(PathBuf::from("/tmp/report.json")));
    for pid in ["", "12a", "-1", " 7"] {
        assert_eq!(
            inherited(Some(pid.into()), Some("/tmp/report.json".into())),
            Inherited::default(),
            "{pid:?}"
        );
    }
    assert_eq!(
        inherited(None, Some("/tmp/report.json".into())),
        Inherited::default()
    );
}

#[test]
fn a_handed_off_run_never_hands_off_again() {
    let marked = inherited(Some("4242".into()), None);
    assert!(hands_off(None, &Inherited::default()));
    assert!(
        !hands_off(None, &marked),
        "the marker stops a second hand-off"
    );
    assert!(!hands_off(Some(Path::new("/repo")), &Inherited::default()));
}

#[test]
fn the_probe_lists_this_version_and_the_protocol() {
    assert_eq!(
        probe_text(),
        format!("{}\nclient-handoff-v1\n", env!("CARGO_PKG_VERSION"))
    );
}

#[test]
fn a_handed_off_run_borrows_the_update_lock_from_its_parent_only() {
    use crate::cli::update::UpdateLock;
    let f = Fixture::new();
    let dir = f.path.join("data");
    fs::create_dir_all(&dir).unwrap();
    let holder = UpdateLock::acquire_in(&dir, None).unwrap();
    let pid = std::process::id().to_string();
    let pid_file = dir.join("update.lock.pid");
    assert_eq!(fs::read_to_string(&pid_file).unwrap().trim(), pid);
    let refused = |from: Option<&str>| {
        UpdateLock::acquire_in(&dir, from)
            .err()
            .expect("refused")
            .message
    };
    assert!(refused(None).contains(&format!(
        "another pseudolife-mcp update is running (pid {pid})"
    )));
    assert!(refused(Some("1")).contains("another pseudolife-mcp update is running"));
    let borrowed = UpdateLock::acquire_in(&dir, Some(&pid)).unwrap();
    assert!(borrowed.borrowed());
    drop(borrowed);
    // The borrower neither rewrote nor removed the holder's pid.
    assert_eq!(fs::read_to_string(&pid_file).unwrap().trim(), pid);
    assert!(refused(None).contains("another pseudolife-mcp update is running"));
    drop(holder);
    assert!(!pid_file.exists());
    let taken = UpdateLock::acquire_in(&dir, Some(&pid)).unwrap();
    assert!(!taken.borrowed(), "a free lock is taken, not borrowed");
}

/// The frozen Python updater is what installed 0.23.x hosts run: the names
/// it sends and reads must stay the ones this module sends and reads.
#[test]
fn the_contract_matches_the_python_updater() {
    let source = fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../pseudolife_memory/update_cli.py"),
    )
    .expect("the Python updater beside the Rust tree");
    let constant = |name: &str| -> String {
        let line = source
            .lines()
            .find(|l| l.starts_with(&format!("{name} = \"")))
            .unwrap_or_else(|| panic!("{name} not found"));
        line.split('"').nth(1).unwrap().to_owned()
    };
    assert_eq!(constant("CLIENT_HANDOFF"), CLIENT_HANDOFF);
    assert_eq!(constant("HANDOFF_ENV"), HANDOFF_ENV);
    assert_eq!(constant("HANDOFF_REPORT_ENV"), HANDOFF_REPORT_ENV);
    assert_eq!(constant("HANDOFF_RUNTIME_ENV"), HANDOFF_RUNTIME_ENV);
    // What a Python parent runs after its interpreter and bootstrap, token
    // for token, is what a native parent runs after the executable.
    let flat: String = source.split_whitespace().collect::<Vec<_>>().join(" ");
    let python_argv = "\"pseudolife_memory.cli\", \"update\", \"--clients-only\", \"--tag\", version, \"--daemon-url\", self.o.daemon_url]";
    assert!(flat.contains(python_argv), "Python hand-off argv changed");
    assert!(flat.contains("if self.o.allow_downgrade: argv.append(\"--allow-downgrade\")"));
    assert_eq!(
        child_args("V", "U", true),
        [
            "update",
            "--clients-only",
            "--tag",
            "V",
            "--daemon-url",
            "U",
            "--allow-downgrade"
        ]
    );
    // The pid a Python child honours (`handoff_from.isdigit()`) and the
    // values a Python parent sends.
    assert!(flat.contains("if handoff_from and handoff_from.isdigit():"));
    assert!(flat.contains("HANDOFF_ENV: str(os.getpid()), HANDOFF_REPORT_ENV: str(report_file),"));
    let env = child_env(77, Path::new("r.json"), Some(Path::new("rt")));
    assert_eq!(
        env,
        [
            (HANDOFF_ENV.to_owned(), OsString::from("77")),
            (HANDOFF_REPORT_ENV.to_owned(), OsString::from("r.json")),
            (HANDOFF_RUNTIME_ENV.to_owned(), OsString::from("rt")),
        ]
    );
    // The report fields each side reads from the other's.
    for read in [
        "child.get(\"steps\")",
        "child.get(\"codex_reapproval\")",
        "child.get(\"clients\")",
        "child.get(\"ok\")",
    ] {
        assert!(flat.contains(read), "{read}");
    }
    let report = serde_json::to_value(crate::cli::update::Report::default()).unwrap();
    for key in ["steps", "codex_reapproval", "clients", "ok", "exit_code"] {
        assert!(report.get(key).is_some(), "native report lacks {key}");
    }
}

/// The real spawner: the child's exit code comes back, the hand-off variables
/// reach it, and an executable that is not there is a start failure, not an
/// exit code.
#[test]
fn the_native_spawner_reports_the_exit_code_or_why_it_could_not_start() {
    #[cfg(windows)]
    let (shell, script) = (
        // cmd.exe misreads a forward slash in its own path.
        PathBuf::from(std::env::var_os("SystemRoot").unwrap())
            .join("System32")
            .join("cmd.exe"),
        vec![
            "/d".to_owned(),
            "/c".to_owned(),
            // No quotes: Rust escapes them in a way cmd.exe does not read.
            "if %PSEUDOLIFE_UPDATE_HANDOFF%==77 (exit 7) else (exit 3)".to_owned(),
        ],
    );
    #[cfg(unix)]
    let (shell, script) = (
        PathBuf::from("/bin/sh"),
        vec![
            "-c".to_owned(),
            "[ \"$PSEUDOLIFE_UPDATE_HANDOFF\" = 77 ] && exit 7; exit 3".to_owned(),
        ],
    );
    let env = child_env(77, Path::new("report.json"), None);
    assert_eq!(NativeSpawner.run(&shell, &script, &env, true), Ok(7));
    assert_eq!(NativeSpawner.run(&shell, &script, &[], true), Ok(3));
    let f = Fixture::new();
    let missing = f.path.join(runtime::BINARY);
    let error = NativeSpawner
        .run(&missing, &child_args(TARGET, URL, false), &env, true)
        .unwrap_err();
    assert!(error.contains("is missing"), "{error}");
}

/// How the parent folds the child's part into its own result: the child's
/// steps and Codex steps are the run's, a clients-only run carries the
/// child's exit code, and after a daemon deploy a failure is the client
/// half's (5).
#[test]
fn the_parent_folds_the_childs_result_into_its_own() {
    use crate::cli::update::{Options, Update};
    let outcome = |exit_code| Outcome {
        runtime: "000002".into(),
        version: TARGET.into(),
        exit_code,
        steps: vec!["child step".into()],
        codex: "approve again".into(),
    };
    let update = |clients_only: bool, tier: &str| {
        let mut update = Update::new(Options {
            as_json: true,
            clients_only,
            ..Options::default()
        });
        update.report.tier = tier.into();
        update
    };
    let mut ok = update(true, "client");
    ok.fold_handoff(
        json!({"ok": true, "plugin": {"state": "current"}}),
        outcome(0),
    );
    assert_eq!((ok.report.ok, ok.report.exit_code), (true, 0));
    assert_eq!(ok.report.steps, ["child step"]);
    assert_eq!(ok.report.codex_reapproval, "approve again");
    assert_eq!(
        ok.report.clients.as_ref().unwrap()["plugin"]["state"],
        "current"
    );
    for (clients_only, tier, child, expected) in [
        (true, "docker", 7, 7),
        (false, "client", 7, 7),
        (false, "docker", 7, CLIENT_STEP_FAILED),
        (true, "client", 0, CLIENT_STEP_FAILED),
    ] {
        let mut failed = update(clients_only, tier);
        failed.fold_handoff(json!({"ok": false}), outcome(child));
        assert!(!failed.report.ok);
        assert_eq!(
            failed.report.exit_code, expected,
            "{clients_only} {tier} {child}"
        );
        let warning = failed.report.steps.last().unwrap();
        assert!(
            warning
                .starts_with("WARNING: the client steps in runtime 000002 (9.9.9) ended with exit"),
            "{warning}"
        );
        assert_eq!(
            warning.ends_with("; the daemon update itself succeeded"),
            !clients_only && tier != "client"
        );
    }
}
