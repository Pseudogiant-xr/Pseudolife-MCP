use super::*;
use crate::cli::update::Options;
use crate::cli::update::clients::Output;
use std::{cell::RefCell, ffi::OsString, time::Duration};

const OWN: &str = "1.0.0";
const TARGET: &str = "9.9.9";

fn args(list: &[&str]) -> Vec<String> {
    list.iter().map(|s| s.to_string()).collect()
}

/// `child_args` as `full_handoff` calls it: `tagged` as the parser read it.
fn child(list: &[&str]) -> Vec<String> {
    let argv = args(list);
    let parsed = Options::parse(argv.iter().map(OsString::from).collect()).unwrap();
    child_args(&argv, TARGET, parsed.tag.is_some())
}

#[test]
fn the_child_gets_the_users_arguments_with_the_target_pinned_and_no_result_file() {
    assert_eq!(
        child(&["--daemon-only", "--json"]),
        args(&["update", "--daemon-only", "--json", "--tag", TARGET])
    );
    // A tag the user named is the target already: it is not given twice.
    assert_eq!(
        child(&["--tag", TARGET, "--reinstall"]),
        args(&["update", "--tag", TARGET, "--reinstall"])
    );
    assert_eq!(
        child(&[&format!("--tag={TARGET}")]),
        args(&["update", &format!("--tag={TARGET}")])
    );
    // This process writes the result file from the child's report.
    assert_eq!(
        child(&[
            "--unattended",
            "--result-file",
            "/r",
            "--result-file=/s",
            "--daemon-url",
            "http://x"
        ]),
        args(&[
            "update",
            "--unattended",
            "--daemon-url",
            "http://x",
            "--tag",
            TARGET
        ])
    );
    // A value that looks like an option is the option's value, as the parser
    // reads it: `--tag` here is a rollback suffix, not a tag, so the target
    // is still pinned, and `--result-file` here is the env file's name.
    assert_eq!(
        child(&["--rollback-tag", "--tag", "--env-file", "--result-file"]),
        args(&[
            "update",
            "--rollback-tag",
            "--tag",
            "--env-file",
            "--result-file",
            "--tag",
            TARGET
        ])
    );
}

#[test]
fn every_value_option_takes_a_value_in_the_parser() {
    for option in crate::cli::update::options::VALUE_OPTIONS {
        let refused = Options::parse(vec![OsString::from(*option)])
            .expect_err(&format!("{option} parsed without a value"));
        assert_eq!(refused.message, format!("{option} needs a value"));
    }
    // A flag takes none.
    assert!(Options::parse(vec![OsString::from("--json")]).is_ok());
}

/// Answers the two probes of a staged client.
struct Probe {
    start: (i32, String),
    protocols: (i32, String),
}
impl Probe {
    fn speaking(protocols: &str) -> Self {
        Self {
            start: (0, "pseudolife-mcp native-update-v1\n".into()),
            protocols: (0, format!("{TARGET}\n{protocols}\n")),
        }
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
        let (code, text) = match args {
            ["update", handoff::START_PROBE] => self.start.clone(),
            ["update", handoff::PROBE] => self.protocols.clone(),
            other => panic!("unexpected probe {other:?}"),
        };
        Output {
            code,
            bytes: text.into_bytes(),
        }
    }
}

type Call = (PathBuf, Vec<String>, Vec<(String, OsString)>, bool);
/// Records its call; `run` writes `report` (if any) where the child would
/// and returns `result`.
struct Spawn {
    calls: RefCell<Vec<Call>>,
    report: Option<Value>,
    result: Spawned,
}
impl Spawn {
    fn new(report: Option<Value>, result: Spawned) -> Self {
        Self {
            calls: RefCell::new(vec![]),
            report,
            result,
        }
    }
}
impl FullRun for Spawn {
    fn run(
        &self,
        executable: &Path,
        args: &[String],
        env: &[(String, OsString)],
        quiet: bool,
    ) -> Spawned {
        self.calls
            .borrow_mut()
            .push((executable.to_owned(), args.to_vec(), env.to_vec(), quiet));
        if let Some(report) = &self.report {
            let file = env
                .iter()
                .find(|(k, _)| k == handoff::HANDOFF_REPORT_ENV)
                .map(|(_, v)| PathBuf::from(v))
                .expect("the report variable");
            fs::write(file, serde_json::to_vec(report).unwrap()).unwrap();
        }
        self.result.clone()
    }
}

struct Folder(PathBuf);
impl Folder {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("full-handoff-test-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Folder {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn run(
    folder: &Folder,
    stage: Result<(), String>,
    probe: &Probe,
    spawn: &Spawn,
) -> (Attempt, Vec<String>) {
    let mut notes = vec![];
    let argv = args(&["--daemon-only"]);
    let attempt = attempt(
        Child {
            target: TARGET,
            own: OWN,
            folder: &folder.0,
            argv: &argv,
            tagged: false,
            env: handoff::child_env(4242, &folder.0.join(REPORT), None),
            quiet: false,
        },
        |dir| {
            stage.map(|()| {
                let exe = dir.join("staged-client");
                fs::write(&exe, b"x").unwrap();
                exe
            })
        },
        probe,
        spawn,
        &mut |note| notes.push(note),
    );
    (attempt, notes)
}

#[test]
fn a_client_that_cannot_be_fetched_falls_back_in_one_line_without_a_child() {
    let folder = Folder::new();
    let spawn = Spawn::new(None, Spawned::Exited(0));
    let (attempt, notes) = run(
        &folder,
        Err("release archive checksum mismatch".into()),
        &Probe::speaking(handoff::FULL_UPDATE),
        &spawn,
    );
    let Attempt::Fallback(note) = attempt else {
        panic!("a fetch failure ran a child: {attempt:?}")
    };
    assert!(
        note.contains("could not fetch release 9.9.9's client"),
        "{note}"
    );
    assert!(note.contains("checksum mismatch"), "{note}");
    assert!(
        note.ends_with("this update continues with 1.0.0's code"),
        "{note}"
    );
    assert!(!note.contains('\n'));
    assert!(spawn.calls.borrow().is_empty());
    assert!(notes.is_empty(), "announced a hand-off that never ran");
}

#[test]
fn a_client_that_does_not_start_or_answers_wrongly_falls_back() {
    for (probe, said) in [
        (
            Probe {
                start: (127, "exec format error\n".into()),
                protocols: (0, String::new()),
            },
            "did not start for this update (exit 127: exec format error)",
        ),
        (
            Probe {
                start: (0, "pseudolife-mcp native-update-v1\n".into()),
                protocols: (0, "9.9.8\nfull-update-v1\n".into()),
            },
            "its probe reported \"9.9.8\", not 9.9.9",
        ),
        (
            Probe {
                start: (0, "pseudolife-mcp native-update-v1\n".into()),
                protocols: (2, "usage\n".into()),
            },
            "its protocol probe ended with exit 2",
        ),
    ] {
        let folder = Folder::new();
        let spawn = Spawn::new(None, Spawned::Exited(0));
        let (attempt, notes) = run(&folder, Ok(()), &probe, &spawn);
        let Attempt::Fallback(note) = attempt else {
            panic!("{said}: ran a child")
        };
        assert!(note.contains(said), "{note}");
        assert!(spawn.calls.borrow().is_empty());
        assert!(notes.is_empty());
    }
}

#[test]
fn a_target_that_predates_the_protocol_runs_on_this_code() {
    let folder = Folder::new();
    let spawn = Spawn::new(None, Spawned::Exited(0));
    let (attempt, _) = run(
        &folder,
        Ok(()),
        &Probe::speaking(handoff::CLIENT_HANDOFF),
        &spawn,
    );
    assert_eq!(
        attempt,
        Attempt::Fallback(
            "release 9.9.9's client predates running the whole update itself; this update continues with 1.0.0's code".into()
        )
    );
    assert!(spawn.calls.borrow().is_empty());
    // A protocol whose name only contains the token does not count.
    let (attempt, _) = run(
        &folder,
        Ok(()),
        &Probe::speaking("client-handoff-v1 full-update-v10"),
        &spawn,
    );
    assert!(matches!(attempt, Attempt::Fallback(_)));
    assert!(spawn.calls.borrow().is_empty());
}

#[test]
fn the_child_runs_the_update_with_the_markers_and_its_report_comes_back() {
    let folder = Folder::new();
    let report = json!({"mode": "release", "steps": ["child step"], "ok": true, "exit_code": 0,
        "a_newer_field": {"kept": true}});
    let spawn = Spawn::new(Some(report.clone()), Spawned::Exited(0));
    let (attempt, notes) = run(
        &folder,
        Ok(()),
        &Probe::speaking("client-handoff-v1 full-update-v1"),
        &spawn,
    );
    assert_eq!(
        attempt,
        Attempt::Ran {
            code: 0,
            report: Some(report),
            failure: None
        }
    );
    assert_eq!(notes.len(), 1, "{notes:?}");
    assert!(
        notes[0].starts_with("release 9.9.9 is newer than this updater (1.0.0)"),
        "{notes:?}"
    );
    let calls = spawn.calls.borrow();
    let (executable, child, env, quiet) = &calls[0];
    assert_eq!(executable, &folder.0.join("staged-client"));
    assert_eq!(child, &args(&["update", "--daemon-only", "--tag", TARGET]));
    assert!(!quiet);
    let value = |key: &str| {
        env.iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.to_string_lossy().into_owned())
    };
    // The lock holder the child borrows from, which also stops it handing
    // off again, and the report it writes.
    assert_eq!(value(handoff::HANDOFF_ENV).as_deref(), Some("4242"));
    assert_eq!(
        value(handoff::HANDOFF_REPORT_ENV).map(PathBuf::from),
        Some(folder.0.join(REPORT))
    );
}

#[test]
fn a_child_that_never_started_falls_back_and_one_that_ran_never_does() {
    // It could not be started: nothing changed, so this code runs the update.
    let folder = Folder::new();
    let spawn = Spawn::new(None, Spawned::NotStarted("/x is missing".into()));
    let (attempt, _) = run(
        &folder,
        Ok(()),
        &Probe::speaking(handoff::FULL_UPDATE),
        &spawn,
    );
    assert_eq!(
        attempt,
        Attempt::Fallback(
            "release 9.9.9's client did not start for this update (/x is missing); this update continues with 1.0.0's code".into()
        )
    );
    // It ran and waiting for it failed: its result is the run's.
    let spawn = Spawn::new(None, Spawned::Failed("it ran longer than 4 hours".into()));
    let (attempt, _) = run(
        &folder,
        Ok(()),
        &Probe::speaking(handoff::FULL_UPDATE),
        &spawn,
    );
    assert_eq!(
        attempt,
        Attempt::Ran {
            code: -1,
            report: None,
            failure: Some("it ran longer than 4 hours".into())
        }
    );
}

fn update() -> Update {
    let mut update = Update::new(Options::parse(vec![]).unwrap());
    update.o.as_json = true;
    update.report.steps = vec!["parent step".into()];
    // Not "docker": the tier check asks the command probe, never PATH.
    update.docker = "docker-fixture".into();
    update
}

#[test]
fn the_childs_report_is_the_run_with_this_runs_steps_first() {
    let mut u = update();
    u.adopt(
        TARGET,
        0,
        Some(
            json!({"mode": "release", "steps": ["child step"], "ok": true, "exit_code": 0,
            "codex_reapproval": "approve the hooks", "a_newer_field": 7}),
        ),
        None,
        true,
    );
    let handed = u.handed.as_ref().unwrap();
    assert_eq!(handed["steps"], json!(["parent step", "child step"]));
    assert_eq!(handed["a_newer_field"], 7);
    assert_eq!(handed["exit_code"], 0);
    assert_eq!(u.report.exit_code, 0);
    assert!(u.report.ok);
    assert_eq!(u.report.codex_reapproval, "approve the hooks");
}

#[test]
fn the_childs_exit_code_and_ok_are_the_runs() {
    // The process's code, even when its report says otherwise.
    let mut u = update();
    u.adopt(
        TARGET,
        4,
        Some(json!({"steps": [], "ok": true, "exit_code": 0})),
        None,
        true,
    );
    assert_eq!(u.report.exit_code, 4);
    // "Nothing to do" (3) and "held off" (4) stay ok, as on this code.
    assert!(u.report.ok);
    assert_eq!(u.handed.as_ref().unwrap()["ok"], true);
    let mut u = update();
    u.adopt(
        TARGET,
        0,
        Some(json!({"steps": [], "ok": false})),
        None,
        true,
    );
    assert_eq!(u.report.exit_code, 0);
    assert!(!u.report.ok);
    // A signal ended the child: 128 + the signal.
    let mut u = update();
    u.adopt(
        TARGET,
        130,
        Some(json!({"steps": [], "ok": false})),
        None,
        true,
    );
    assert_eq!(u.report.exit_code, 130);
}

#[test]
fn a_child_without_a_report_or_that_was_not_waited_for_fails_the_run_with_its_cause() {
    for (code, failure, quiet, said, exit) in [
        (
            0,
            None,
            true,
            "release 9.9.9's updater ended with exit 0 without writing its report; run the update again without --json to see its output",
            0,
        ),
        (
            3,
            None,
            false,
            "release 9.9.9's updater ended with exit 3 without writing its report; its output is above",
            3,
        ),
        (
            -1,
            Some("it ran longer than 4 hours".to_owned()),
            true,
            "this run stopped waiting for release 9.9.9's updater (it ran longer than 4 hours)",
            1,
        ),
    ] {
        let mut u = update();
        u.adopt(TARGET, code, None, failure, quiet);
        assert_eq!(u.report.exit_code, exit, "{said}");
        assert!(!u.report.ok);
        let handed = u.handed.as_ref().unwrap();
        assert_eq!(handed["ok"], false);
        let steps: Vec<_> = handed["steps"].as_array().unwrap().iter().collect();
        assert_eq!(steps[0], "parent step");
        assert!(
            steps.iter().any(|s| s.as_str().unwrap().contains(said)),
            "{steps:?}"
        );
    }
}

#[test]
fn staging_lives_beside_the_runtimes_is_never_a_runtime_and_is_cleared() {
    let folder = Folder::new();
    let layout = crate::runtime::Layout {
        root: folder.0.join("runtimes"),
        launcher: folder.0.join("bin").join(crate::runtime::BINARY),
        user_bin: None,
        user_path: false,
    };
    // A leftover from a run that was killed, with a valid-looking marker.
    let staging = Staging::create(&layout.root).unwrap();
    let leftover = staging.path().to_owned();
    fs::write(leftover.join(crate::runtime::BINARY), b"\x7fELFstaged").unwrap();
    fs::write(leftover.join(crate::runtime::MARKER), b"{}").unwrap();
    // A live run of another data directory holds its folder's lock: the
    // next staging leaves it alone.
    let live = Staging::create(&layout.root).unwrap();
    let mut held = staging;
    held.lock.release();
    std::mem::forget(held);
    assert!(leftover.starts_with(layout.root.join(STAGED)));
    assert!(crate::runtime::list_runtimes(&layout).is_empty());
    assert!(crate::runtime::current_runtime(&layout).is_none());
    // Pruning neither counts it nor removes it (only the staging does).
    let pruned = crate::runtime::remove_unused(
        &layout,
        Ok(crate::runtime::Evidence {
            processes: vec![],
            registrations: Default::default(),
            tunnels: Default::default(),
        }),
    );
    assert!(pruned["error"].is_null(), "{pruned}");
    assert_eq!(pruned["removed"], json!([]), "{pruned}");
    assert_eq!(pruned["kept"], json!([]), "{pruned}");
    assert!(leftover.exists());
    // The next runtime takes the first sequence number.
    let image = folder.0.join("image");
    #[cfg(windows)]
    fs::write(&image, b"MZimage").unwrap();
    #[cfg(target_os = "linux")]
    fs::write(&image, b"\x7fELFimage").unwrap();
    #[cfg(target_os = "macos")]
    fs::write(&image, b"\xcf\xfa\xed\xfeimage").unwrap();
    let installed = {
        let _lock = crate::runtime::UpdateLock::acquire(&layout).unwrap();
        crate::runtime::install_binary(&layout, "9.9.9", "pseudolife-mcp==9.9.9", None, &image)
            .unwrap()
    };
    assert_eq!(installed.name(), "000001");
    assert_eq!(
        crate::runtime::current_runtime(&layout).map(|r| r.path),
        Some(installed.path.clone())
    );
    // The next locked run's staging clears the dead leftover, keeps the live
    // one, and makes its own folder.
    let next = Staging::create(&layout.root).unwrap();
    assert!(!leftover.exists(), "a leftover staging folder survived");
    assert!(
        live.path().is_dir(),
        "a live run's staging folder was removed"
    );
    assert!(next.path().is_dir());
    let path = next.path().to_owned();
    drop(next);
    assert!(!path.exists(), "the staging folder outlived the run");
    assert!(!path.with_extension("lock").exists());
}

#[test]
fn a_run_handed_this_one_never_hands_off_again() {
    let mut u = update();
    u.release_lookup = Some(Box::new(|| Ok(Some("99.0.0".into()))));
    u.command_probe = Some(|args| panic!("the child probed the host: {args:?}"));
    u.health_probe = Some(|| panic!("the child read /health"));
    u.o.handoff.from = Some("4242".into());
    assert_eq!(u.full_handoff_target(), None);
}

#[test]
fn no_full_hand_off_to_the_running_version_or_older_or_outside_a_release_update() {
    for target in [env!("CARGO_PKG_VERSION"), "0.1.0"] {
        // A named tag that is not newer: nothing is asked of Docker.
        let mut u = update();
        u.o.tag = Some(target.into());
        u.command_probe = Some(|args| panic!("probed the host for an older tag: {args:?}"));
        assert_eq!(u.full_handoff_target(), None, "{target}");
        // A discovered release that is not newer.
        let mut u = update();
        let found = target.to_owned();
        u.release_lookup = Some(Box::new(move || Ok(Some(found.clone()))));
        u.command_probe = Some(|args| {
            assert_eq!(args[..3], ["inspect", "-f", "{{.Id}}"], "{args:?}");
            (0, "fixture-daemon".into())
        });
        assert_eq!(u.full_handoff_target(), None, "{target}");
    }
    let tweaks: [fn(&mut Options); 6] = [
        |o| o.checkout = Some(PathBuf::from("/repo")),
        |o| o.clients_only = true,
        |o| o.check = true,
        |o| o.schedule = Some("03:00".into()),
        |o| o.unschedule = true,
        |o| o.shadow = true,
    ];
    for tweak in tweaks {
        let mut u = update();
        tweak(&mut u.o);
        // A Docker host, so only the option can stop the run before it looks
        // up a release.
        u.command_probe = Some(|_| (0, "fixture-daemon".into()));
        u.release_lookup = Some(Box::new(|| panic!("looked up a release")));
        assert_eq!(u.full_handoff_target(), None);
    }
}

#[test]
fn a_client_machine_keeps_the_client_hand_off_without_asking_github() {
    let mut u = update();
    u.release_lookup = Some(Box::new(|| panic!("a client machine looked up a release")));
    // The container is absent: a client machine.
    u.command_probe = Some(|args| {
        assert_eq!(args[0], "inspect");
        (1, "Error: No such object: pseudolife-mcp-daemon".into())
    });
    assert_eq!(u.full_handoff_target(), None);
}

#[test]
fn release_discovery_is_asked_once_per_run_whatever_it_answered() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static ASKED: AtomicUsize = AtomicUsize::new(0);
    let mut u = update();
    u.release_lookup = Some(Box::new(|| {
        ASKED.fetch_add(1, Ordering::SeqCst);
        Err(crate::cli::update::UpdateError::usage(
            "the GitHub release check timed out",
        ))
    }));
    let first = u.target_version().unwrap_err();
    let second = u.target_version().unwrap_err();
    assert_eq!(ASKED.load(Ordering::SeqCst), 1);
    assert_eq!(
        (first.message, first.exit_code),
        (second.message, second.exit_code)
    );
}
