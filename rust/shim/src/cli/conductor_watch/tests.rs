//! `conductor-watch` before its lock, against texts the oracle printed
//! (`conductor_watch_cli.py` with its conductor-watch defect fixes). The
//! decision tests
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
    for text in ["\"1_0\"", "\"\u{661}\""] {
        assert!(read(text).is_err(), "{text}");
    }
}

#[test]
fn record_pids_outside_the_probes_range_name_no_watcher() {
    // `_running` refuses a pid at or past 2**31 (the probes overflow) and
    // reads int()'s OverflowError (an infinite pid) as no watcher; anything
    // at or below 0 is not alive.
    let read = |text: &str| record_pid(Some(&pyjson::loads(text, DEPTH).unwrap()));
    for text in [
        "Infinity",
        "-Infinity",
        "2147483648",
        "4294967296",
        "-2147483649",
        "99999999999999999999999",
        "1e10",
        "-1e300",
        "2147483648.5",
        "\"2147483648\"",
        "\" -99999999999 \"",
    ] {
        assert_eq!(read(text).ok(), Some(None), "{text}");
    }
    assert_eq!(read("2147483647").ok(), Some(Some(2_147_483_647)));
    assert_eq!(read("2147483647.9").ok(), Some(Some(2_147_483_647)));
}

#[test]
fn an_unwritable_stop_request_is_refused_with_the_reason() {
    let scratch = Scratch::new();
    let stop = scratch.0.join("s.stop");
    fs::create_dir(&stop).unwrap();
    let reason = strerror(&write_stop(&stop).unwrap_err()).unwrap();
    assert_eq!(
        reason,
        if cfg!(windows) {
            "Permission denied"
        } else {
            "Is a directory"
        }
    );
    assert_eq!(
        stop_refusal(&stop, &reason).unwrap(),
        format!(
            "cannot write {} ({reason}); nothing changed. Make {} writable, then run --stop again",
            stop.display(),
            scratch.0.display()
        )
    );
    // A relative state's directory is pathlib's ".".
    assert_eq!(
        stop_refusal(Path::new("s.stop"), "Permission denied").unwrap(),
        "cannot write s.stop (Permission denied); nothing changed. \
         Make . writable, then run --stop again"
    );
}

#[cfg(windows)]
#[test]
fn a_hidden_stop_file_is_refused_as_the_c_runtime_refuses_it() {
    use std::os::windows::ffi::OsStrExt;
    #[allow(unsafe_code)]
    fn set_attributes(path: &Path, attributes: u32) {
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
        // SAFETY: a NUL-terminated wide path that outlives the call.
        let done = unsafe {
            windows_sys::Win32::Storage::FileSystem::SetFileAttributesW(wide.as_ptr(), attributes)
        };
        assert_ne!(done, 0);
    }
    let scratch = Scratch::new();
    for attributes in [0x2, 0x4] {
        let stop = scratch.0.join("s.stop");
        fs::write(&stop, b"earlier").unwrap();
        set_attributes(&stop, attributes);
        let error = write_stop(&stop).unwrap_err();
        assert_eq!(strerror(&error).as_deref(), Some("Permission denied"));
        set_attributes(&stop, 0x80);
        assert_eq!(fs::read(&stop).unwrap(), b"earlier");
        fs::remove_file(&stop).unwrap();
    }
    let stop = scratch.0.join("s.stop");
    fs::write(&stop, b"earlier").unwrap();
    write_stop(&stop).unwrap();
    assert_eq!(fs::read(&stop).unwrap(), b"");
}

#[test]
fn stop_errors_read_as_python_reports_them() {
    let os = io::Error::from_raw_os_error;
    if cfg!(windows) {
        // The C runtime's errno for the Win32 code, then its message.
        for (code, text) in [
            (2, "No such file or directory"),
            (3, "No such file or directory"),
            (5, "Permission denied"),
            (19, "Permission denied"),
            (32, "Permission denied"),
            (36, "Permission denied"),
            (112, "No space left on device"),
        ] {
            assert_eq!(strerror(&os(code)).as_deref(), Some(text), "{code}");
        }
        assert_eq!(strerror(&os(1450)), None);
    } else {
        assert_eq!(
            strerror(&os(libc::EACCES)).as_deref(),
            Some("Permission denied")
        );
        assert_eq!(
            strerror(&os(libc::EISDIR)).as_deref(),
            Some("Is a directory")
        );
    }
    assert_eq!(strerror(&io::Error::other("x")), None);
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
}

#[test]
fn a_record_whose_start_cannot_be_proven_is_decided_by_the_states_lock() {
    let scratch = Scratch::new();
    let state = scratch.state();
    let (pid, _) = me();
    // No start time recorded: the pid alone proves nothing (it may be a
    // reused pid), so a free lock means no watcher.
    scratch.pid_file(&format!(
        "{{\"pid\": \" {pid} \", \"agent_id\": \"w1\", \"owner\": \"{OWNER}\"}}"
    ));
    let out = decided(&["--stop", "--state", &state]);
    assert_eq!(
        out.stdout,
        format!("conductor-watch: no watcher running for {state}; nothing changed\n")
    );
    assert_eq!(out.stop_file, None);
    assert!(
        decided(&["--owner", OWNER, "--state", &state])
            .start
            .is_some()
    );
    // A held lock is a watcher, whatever its record lacks.
    let mut lock = super::super::lease::lock::Lock::new(scratch.0.join("s.lock"));
    assert!(matches!(lock.acquire(), Ok(true)));
    let out = decided(&["--stop", "--state", &state]);
    assert_eq!(
        out.stdout,
        "conductor-watch w1 asked to stop; it exits at its next poll\n"
    );
    assert!(
        decided(&["--owner", OWNER, "--state", &state])
            .start
            .is_none()
    );
    lock.release();
}

#[test]
fn a_stat_name_that_is_not_utf8_still_gives_the_start_time() {
    let mut raw = b"4242 (a\xff) b) S".to_vec();
    for field in 4..=22 {
        raw.extend(format!(" {field}").as_bytes());
    }
    assert_eq!(stat_start(&raw).as_deref(), Some("22"));
    assert_eq!(stat_start(b"4242 (short) S 1 2"), None);
    assert_eq!(stat_start(b"\xff\xfe"), None);
}

#[test]
fn one_watcher_per_owner_answers_from_another_state_file() {
    let scratch = Scratch::new();
    let home = scratch.0.join("home");
    let (lock_path, note) = owner_files(&home, OWNER);
    fs::create_dir_all(lock_path.parent().unwrap()).unwrap();
    // The owner's watcher runs on another state file (this test process,
    // dated as a watcher, holds the owner's lock).
    let other = scratch.0.join("elsewhere.json");
    let (pid, start) = me();
    fs::write(
        scratch.0.join("elsewhere.pid"),
        format!("{{\"pid\": {pid}, \"process_start\": \"{start}\", \"agent_id\": \"w9\", \"owner\": \"{OWNER}\"}}"),
    )
    .unwrap();
    fs::write(
        &note,
        format!(
            "{{\"state\": {}}}",
            serde_json::json!(other.display().to_string())
        ),
    )
    .unwrap();
    let mut lock = super::super::lease::lock::Lock::new(lock_path);
    assert!(matches!(lock.acquire(), Ok(true)));
    let key = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    let vars = [(key, home.to_str().unwrap())];
    let state = scratch.state();
    // A start for the same owner on this state is answered by that watcher.
    let out = decide_with(&["--owner", OWNER, "--state", &state], &vars).unwrap();
    assert!(out.start.is_none());
    assert_eq!(
        out.stdout,
        format!(
            "conductor-watch w9 already running for {OWNER}; park with park_needs=\"decision events\" park_clear_by=w9\n"
        )
    );
    // --stop for that owner from here stops that watcher.
    let out = decide_with(&["--stop", "--owner", OWNER, "--state", &state], &vars).unwrap();
    assert_eq!(out.stop_file, Some(scratch.0.join("elsewhere.stop")));
    // Near miss: another owner starts.
    let out = decide_with(&["--owner", OTHER, "--state", &state], &vars).unwrap();
    assert!(out.start.is_some());
    lock.release();
    // With the owner's lock free the note proves nothing.
    let out = decide_with(&["--owner", OWNER, "--state", &state], &vars).unwrap();
    assert!(out.start.is_some());
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
fn a_start_is_decided_before_any_effect_and_edge_spellings_still_defer() {
    let scratch = Scratch::new();
    let state = scratch.state();
    for (argv, detach) in [
        (&["--owner", OWNER, "--state", &state][..], false),
        (&["--owner", OWNER, "--state", &state, "--detach"], true),
    ] {
        let out = decided(argv);
        let start = out.start.expect("a start");
        assert_eq!(start.owner, OWNER);
        assert_eq!(start.args.detach, detach);
        assert_eq!(start.files.state, Path::new(&state));
        assert!(out.stdout.is_empty() && out.stderr.is_empty());
    }
    for argv in [&["--sto"][..], &["--stop", "--state", &format!("{state}.")]] {
        assert!(decide_with(argv, &[]).is_err(), "{argv:?}");
    }
    // Refused or answered before any effect now, no longer deferred.
    assert_eq!(decided(&["--stop", "--state", ""]).code, 2);
    assert_eq!(
        decided(&["--stop", "--interval", "inf", "--state", &state]).code,
        0
    );
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

#[test]
fn a_python_watchers_macos_start_time_reads_as_ps_wrote_it() {
    // `ps -o lstart=`: weekday, month, space-padded day, time, year.
    let rendered = lstart(1_700_000_000).unwrap();
    let words: Vec<&str> = rendered.split_whitespace().collect();
    assert_eq!(words.len(), 5, "{rendered}");
    assert_eq!(rendered.len(), 24, "{rendered}");
    assert_eq!(words[4], "2023");
    // Only on macOS does the legacy form name the running process.
    let legacy = J::Str(rendered.clone());
    assert_eq!(
        legacy_start_matches(&legacy, Some("1700000000.123456")),
        cfg!(target_os = "macos")
    );
    assert!(!legacy_start_matches(&legacy, Some("1700000001.000000")));
    assert!(!legacy_start_matches(
        &J::Str("1".into()),
        Some("1700000000.0")
    ));
}
