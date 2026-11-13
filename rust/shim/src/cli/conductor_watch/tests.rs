//! `conductor-watch` before its lock, against texts the oracle printed
//! (`conductor_watch_cli.py` at this branch's base). The decision tests
//! read no process environment: each passes its own variables and a state
//! under a fresh temporary directory; a "running watcher" is this test
//! process, dated by `process_start` as the oracle dates one.
use super::*;
use std::{collections::HashMap, sync::atomic::AtomicUsize};

const OWNER: &str = "0123456789abcdef0123456789abcdef";
const OTHER: &str = "fedcba9876543210fedcba9876543210";

struct Scratch(PathBuf);

impl Scratch {
    fn new() -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let path =
            std::env::temp_dir().join(format!("pl-conductor-watch-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }
    fn state(&self) -> String {
        self.0.join("s.json").to_str().unwrap().to_owned()
    }
    fn pid_file(&self, body: &str) {
        fs::write(self.0.join("s.pid"), body).unwrap();
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn decide_with(argv: &[&str], vars: &[(&str, &str)]) -> Res<Out> {
    let vars: HashMap<String, OsString> = vars
        .iter()
        .map(|(key, value)| ((*key).to_owned(), OsString::from(value)))
        .collect();
    let var = move |name: &str| vars.get(name).cloned();
    let context = Context {
        var: &var,
        columns_80: true,
        colour_stdout: false,
        colour_stderr: false,
        utf8_argv: true,
    };
    let argv: Vec<String> = argv.iter().map(|item| (*item).to_owned()).collect();
    decide(&argv, &context)
}

fn decided(argv: &[&str]) -> Out {
    decide_with(argv, &[]).expect("answered natively")
}

fn me() -> (u32, String) {
    let pid = std::process::id();
    (pid, process_start(pid).unwrap().unwrap())
}

#[test]
fn with_suffix_replaces_the_last_suffix_only() {
    for (path, want) in [
        ("w/s.json", "w/s.pid"),
        ("w/state", "w/state.pid"),
        ("w/a.b.json", "w/a.b.pid"),
        ("w/.hidden", "w/.hidden.pid"),
    ] {
        assert_eq!(
            with_suffix(Path::new(path), ".pid").unwrap(),
            Path::new(want),
            "{path}"
        );
    }
    assert_eq!(
        with_suffix(Path::new("d/k.txt"), ".dwatch.json").unwrap(),
        Path::new("d/k.dwatch.json")
    );
    for path in ["", ".", "w/a.", "w/.."] {
        assert!(with_suffix(Path::new(path), ".pid").is_err(), "{path:?}");
    }
}

#[test]
fn record_pids_read_as_int_does() {
    let read = |text: &str| record_pid(Some(&pyjson::loads(text, DEPTH).unwrap()));
    for (text, want) in [
        ("12", Some(12)),
        ("-4", Some(-4)),
        ("true", Some(1)),
        ("false", Some(0)),
        ("12.9", Some(12)),
        ("-0.5", Some(0)),
        ("NaN", None),
        ("null", None),
        ("[1]", None),
        ("{}", None),
        ("\" 12 \"", Some(12)),
        ("\"\\u000b12\\u000c\"", Some(12)),
        // int() strips C isspace only: U+001C..U+001F make it raise.
        ("\"\\u001c12\"", None),
        ("\"12\\u001f\"", None),
        ("\"+7\"", Some(7)),
        ("\"007\"", Some(7)),
        ("\"12x\"", None),
        ("\"\"", None),
        ("\"+\"", None),
        ("\"1 2\"", None),
        ("\"\\ud800\"", None),
    ] {
        assert_eq!(read(text).ok(), Some(want), "{text}");
    }
    assert_eq!(record_pid(None).ok(), Some(None));
    for text in ["Infinity", "4294967296", "1e10", "\"1_0\"", "\"\u{661}\""] {
        assert!(read(text).is_err(), "{text}");
    }
}

#[test]
fn printed_values_are_str_with_surrogates_replaced() {
    assert_eq!(py_str(None), "None");
    assert_eq!(py_str(Some(&J::Str("id".into()))), "id");
    assert_eq!(py_str(Some(&J::Wtf(vec![0x61, 0xd800]))), "a?");
    let value = pyjson::loads(r#"{"id": [1, 2.5, true, null, "é"]}"#, DEPTH).unwrap();
    assert_eq!(py_str(Some(&value)), "{'id': [1, 2.5, True, None, 'é']}");
}

#[test]
fn help_and_usage_errors_are_argparse_bytes() {
    let help = decided(&["--stop", "-h"]);
    assert_eq!(help.stdout, args::HELP);
    assert_eq!(help.code, 0);
    let error = decided(&["--bogus"]);
    assert_eq!(error.code, 2);
    assert!(error.stderr.starts_with(args::usage()));
    assert!(
        error
            .stderr
            .ends_with("pseudolife-mcp conductor-watch: error: unrecognized arguments: --bogus\n")
    );
}

#[test]
fn owner_refusals_name_the_flag() {
    let out = decided(&[]);
    assert_eq!(
        out.stderr,
        "conductor-watch: no session to read the owner from (CLAUDE_CODE_SESSION_ID is unset); \
         pass --owner <32-hex board agent id of the session to wake>\n"
    );
    assert_eq!(out.code, 2);
    let out = decided(&["--owner", "it's"]);
    assert_eq!(
        out.stderr,
        "conductor-watch: --owner \"it's\" is not an agent id (32 lowercase hex); \
         pass --owner <32-hex board agent id of the session to wake>\n"
    );
}

#[test]
fn stop_without_a_way_to_find_the_watcher_exits_2() {
    let out = decided(&["--stop", "--owner", "zz"]);
    assert_eq!(
        out.stderr,
        "conductor-watch: no session to find the watcher by; pass --owner <id> or --state <path>\n"
    );
    assert_eq!(out.code, 2);
}

#[test]
fn stop_with_no_watcher_changes_nothing() {
    let scratch = Scratch::new();
    let state = scratch.state();
    let out = decided(&["--stop", "--state", &state]);
    assert_eq!(
        out.stdout,
        format!("conductor-watch: no watcher running for {state}; nothing changed\n")
    );
    assert_eq!(out.stop_file, None);
    for body in [
        "{not json",
        "[1, 2]",
        "{\"pid\": 0}",
        "{\"pid\": null}",
        "{}",
        "\u{feff}{\"pid\": 1}",
    ] {
        scratch.pid_file(body);
        assert_eq!(
            decided(&["--stop", "--state", &state]).stop_file,
            None,
            "{body}"
        );
    }
    let (pid, _) = me();
    scratch.pid_file(&format!("{{\"pid\": {pid}, \"process_start\": \"0\"}}"));
    assert_eq!(decided(&["--stop", "--state", &state]).stop_file, None);
    scratch.pid_file(&format!("{{\"pid\": {pid}, \"process_start\": 12}}"));
    assert_eq!(decided(&["--stop", "--state", &state]).stop_file, None);
}

#[test]
fn stop_asks_a_running_watcher() {
    let scratch = Scratch::new();
    let state = scratch.state();
    let (pid, start) = me();
    scratch.pid_file(&format!(
        "{{\"pid\": {pid}, \"process_start\": \"{start}\", \"agent_id\": \"w1\"}}"
    ));
    let out = decided(&["--stop", "--state", &state]);
    assert_eq!(
        out.stdout,
        "conductor-watch w1 asked to stop; it exits at its next poll\n"
    );
    assert_eq!(out.stop_file, Some(scratch.0.join("s.stop")));
    scratch.pid_file(&format!("{{\"pid\": \" {pid} \"}}"));
    let out = decided(&["--stop", "--state", &state]);
    assert_eq!(
        out.stdout,
        "conductor-watch None asked to stop; it exits at its next poll\n"
    );
}

#[test]
fn a_running_watcher_answers_a_start() {
    let scratch = Scratch::new();
    let state = scratch.state();
    let (pid, start) = me();
    scratch.pid_file(&format!(
        "{{\"pid\": {pid}, \"process_start\": \"{start}\", \"agent_id\": \"w1\", \"owner\": \"{OWNER}\"}}"
    ));
    for detach in [false, true] {
        let mut argv = vec!["--owner", OWNER, "--state", &state];
        if detach {
            argv.push("--detach");
        }
        let out = decided(&argv);
        assert_eq!(
            out.stdout,
            format!(
                "conductor-watch w1 already running for {OWNER}; \
                 park with park_needs=\"decision events\" park_clear_by=w1\n"
            )
        );
        assert_eq!(out.code, 0);
        argv[1] = OTHER;
        let out = decided(&argv);
        assert_eq!(
            out.stderr,
            format!(
                "conductor-watch: {state} belongs to the watcher for {OWNER}; \
                 pass --state <another path> to watch for {OTHER}\n"
            )
        );
        assert_eq!(out.code, 2);
    }
}

#[test]
fn the_watch_itself_defers_before_any_effect() {
    let scratch = Scratch::new();
    let state = scratch.state();
    for argv in [
        &["--owner", OWNER, "--state", &state][..],
        &["--owner", OWNER, "--state", &state, "--detach"],
        &["--sto"],
        &["--stop", "--state", ""],
        &["--stop", "--interval", "inf", "--state", &state],
    ] {
        assert!(decide_with(argv, &[]).is_err(), "{argv:?}");
    }
    assert!(decide_with(&["--stop"], &[("PYTHONINTMAXSTRDIGITS", "0")]).is_err());
    assert_eq!(fs::read_dir(&scratch.0).unwrap().count(), 0);
}

#[test]
fn the_default_state_is_per_owner_in_the_home() {
    let scratch = Scratch::new();
    let home = scratch.0.to_str().unwrap();
    let key = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    let out = decide_with(&["--stop", "--owner", OWNER], &[(key, home)]).unwrap();
    let state = scratch
        .0
        .join(".pseudolife-mcp")
        .join("watch")
        .join(format!("{OWNER}.json"));
    assert_eq!(
        out.stdout,
        format!(
            "conductor-watch: no watcher running for {}; nothing changed\n",
            state.display()
        )
    );
}
