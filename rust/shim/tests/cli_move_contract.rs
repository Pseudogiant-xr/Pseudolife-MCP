#![forbid(unsafe_code)]
//! `move` without a daemon: argparse help and usage, the refusals made
//! before any effect, the native answers to the argv that passes them (a
//! dry run's own refusals, and its preflight refusing when no ssh is on an
//! absolute PATH entry), and the deferral of shapes the native parser does
//! not take. The differential proof against the Python oracle is the CLI
//! harness (`rust/cli_harness --row move`); the dry run against both hosts
//! is unit-tested in `restore_cli/move_native/tests.rs`.
#[path = "common/cli.rs"]
mod cli;
use std::{
    fs,
    io::ErrorKind,
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Command, Output},
};

const INVALID_ARGUMENTS: &str =
    "move: invalid arguments. Use --help for supported forms and pass option values separately.\n";
const BAD_TO: &str = "move: --to takes an ssh target such as user@box or a ~/.ssh/config alias\n";
const BAD_URL: &str = "move: --target-url must be an http(s) origin such as \
                       http://100.64.0.2:8765 (no path, query or credentials)\n";
const BAD_CHECKOUT: &str = "move: --target-checkout holds a control character\n";
const URL_REQUIRED: &str = "move: --target-url is required: name the URL clients will use to reach the target's daemon, such as http://100.64.0.2:8765. Nothing was changed\n";
const NO_CHECKOUT: &str = "move: --target-checkout is not supported: move goes to an installed (release) Docker bank on the target, with the native pseudolife-mcp client there; drop --target-checkout. Nothing was changed\n";
const NO_SSH: &str = "move: no ssh program is on PATH (only absolute PATH entries are searched); install the OpenSSH client, then retry. Nothing was changed\n";
/// A dry run that passes every argv check.
const DRY: &[&str] = &[
    "--to",
    "root@box",
    "--target-url",
    "http://100.64.0.2:8765",
    "--no-keep-tokens",
    "--dry-run",
];
const USAGE: &str = "usage: pseudolife-mcp move [-h] --to SSH-TARGET [--target-checkout PATH]
                           [--target-url URL] [--no-keep-tokens] [--resume]
                           [--dry-run] [--expose-target] [--target-release]
                           [--target-env-file TARGET_ENV_FILE]
                           [--target-compose-file TARGET_COMPOSE_FILE]
                           [--target-work-dir TARGET_WORK_DIR] [--yes]
                           [--json]
";

struct Home(PathBuf);

impl Home {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!("move-{label}-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}

impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// A cleared environment whose PATH holds nothing (no docker or ssh) and
/// whose every proxy and daemon variable names `listener`.
fn run(home: &Path, listener: &str, columns: &str, args: &[&str]) -> Output {
    let mut command: Command =
        cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("move")
        .args(args)
        .current_dir(home)
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("PATH", home)
        .env("COLUMNS", columns)
        .env("PSEUDOLIFE_MCP_DAEMON_URL", listener);
    for key in [
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ] {
        command.env(key, listener);
    }
    command.output().unwrap()
}

fn listener() -> (TcpListener, String) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    (listener, url)
}

fn assert_quiet(listener: &TcpListener) {
    match listener.accept() {
        Err(error) if error.kind() == ErrorKind::WouldBlock => {}
        Ok((_, peer)) => panic!("the leaf opened a connection from {peer}"),
        Err(error) => panic!("accept failed: {error}"),
    }
}

fn assert_answer(output: &Output, code: i32, stdout: &str, stderr: &str) {
    assert_eq!(output.status.code(), Some(code));
    assert_eq!(output.stdout, cli::native_text(stdout));
    assert_eq!(output.stderr, cli::native_text(stderr));
}

fn listing(home: &Path) -> Vec<PathBuf> {
    fs::read_dir(home)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect()
}

#[test]
fn recovery_commands_validate_the_path_and_distinguish_a_missing_journal() {
    let home = Home::new("recovery");
    let journal = home.0.join("carry.json");
    let recovery_error = |args: &[&str], code| {
        let result = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")))
            .args(["move-target", "recovery-commands"])
            .args(args)
            .current_dir(&home.0)
            .env("HOME", &home.0)
            .env("USERPROFILE", &home.0)
            .env("PATH", &home.0)
            .output()
            .unwrap();
        assert_eq!(result.status.code(), Some(code));
        assert!(result.stdout.is_empty());
        String::from_utf8(result.stderr).unwrap()
    };
    let usage = "usage: pseudolife-mcp move-target recovery-commands <carry.json>\n";
    assert_eq!(recovery_error(&[], 2), usage);
    assert_eq!(recovery_error(&["~/carry.json", "extra"], 2), usage);
    assert_eq!(
        recovery_error(&["carry.json"], 2),
        "move-target: name the carry journal with an absolute path or ~/ on this host\n",
    );
    let missing = recovery_error(&["~/carry.json"], 4);
    assert!(listing(&home.0).is_empty());
    fs::write(&journal, b"invalid JSON").unwrap();
    let invalid = recovery_error(&["~/carry.json"], 4);
    assert_ne!(missing, invalid);
    assert_eq!(
        missing,
        format!(
            "move-target: no carry journal at {journal:?}. If this is the move's intact folder, the move never changed the environment file and there is nothing to restore there. Otherwise locate the move's carry.json.\n"
        ),
    );
    assert_eq!(
        invalid,
        format!(
            "move-target: cannot read or parse the target carry journal at {journal:?}. Verify the file before continuing.\n"
        ),
    );
    assert_eq!(fs::read(journal).unwrap(), b"invalid JSON");
}

#[test]
fn settling_exposure_requires_explicit_idle_confirmation() {
    let home = Home::new("settle-exposure");
    for suffix in [&[][..], &["--unknown"][..]] {
        let output = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")))
            .args(["move-target", "settle-exposure", "~/move folder", "8443"])
            .args(suffix)
            .env("HOME", &home.0)
            .env("USERPROFILE", &home.0)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(
            String::from_utf8(output.stderr)
                .unwrap()
                .contains("--confirmed-idle")
        );
    }
    assert!(listing(&home.0).is_empty());
}

#[test]
fn valid_argv_gets_the_native_answer_without_a_connection() {
    let home = Home::new("valid");
    let (socket, url) = listener();
    for (args, code, stderr) in [
        (&["--to", "root@box"][..], 2, URL_REQUIRED),
        (&["--to", "root@box", "--dry-run"], 2, URL_REQUIRED),
        (&["--to", "root@box", "--resume", "--yes"], 2, URL_REQUIRED),
        (&["--to", "root@box", "--json"], 2, URL_REQUIRED),
        (
            &[
                "--to",
                "root@box",
                "--target-checkout",
                "/srv/pl",
                "--target-url",
                "http://100.64.0.2:8765",
                "--no-keep-tokens",
                "--resume",
                "--dry-run",
                "--yes",
                "--json",
            ],
            2,
            NO_CHECKOUT,
        ),
        (
            &["--to", "box", "--target-url", "https://box.example.com/"],
            4,
            NO_SSH,
        ),
        (
            &["--to", "box", "--target-checkout", "a\tb"],
            2,
            NO_CHECKOUT,
        ),
        (
            &["--to", "box", "--target-url", "http://[::1]:8765"],
            4,
            NO_SSH,
        ),
        // Every argv check passes: the preflight's first need, ssh, is not
        // on PATH (it holds only the empty home).
        (DRY, 4, NO_SSH),
        // So does the move itself: it is refused at the same need, before
        // anything is changed or asked.
        (&DRY[..DRY.len() - 1], 4, NO_SSH),
        (&[&DRY[..DRY.len() - 1], &["--yes"]].concat(), 4, NO_SSH),
    ] {
        let output = run(&home.0, &url, "80", args);
        assert_answer(&output, code, "", stderr);
        assert_quiet(&socket);
    }
    // Under --json the preflight's refusal is one JSON document on stdout.
    let output = run(&home.0, &url, "80", &[DRY, &["--json"]].concat());
    assert_eq!(output.status.code(), Some(4));
    assert!(output.stderr.is_empty(), "{output:?}");
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["dry_run"], true);
    assert_eq!(report["changed"], false);
    assert_eq!(
        report["error"],
        NO_SSH.trim_start_matches("move: ").trim_end()
    );
    assert_quiet(&socket);
    assert!(
        listing(&home.0).is_empty(),
        "the native answer wrote a file"
    );
}

#[test]
fn an_ssh_planted_in_the_working_directory_is_never_run() {
    let home = Home::new("planted");
    let (socket, url) = listener();
    let (name, body): (&str, &[u8]) = if cfg!(windows) {
        ("ssh.cmd", b"@echo ran> planted-ran\r\n")
    } else {
        ("ssh", b"#!/bin/sh\ntouch planted-ran\n")
    };
    let planted = home.0.join(name);
    fs::write(&planted, body).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&planted, fs::Permissions::from_mode(0o755)).unwrap();
    }
    // PATH names the working directory only by relative and empty entries.
    let separator = if cfg!(windows) { ";" } else { ":" };
    let mut command: Command =
        cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    let output = command
        .arg("move")
        .args(DRY)
        .current_dir(&home.0)
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0)
        .env("PATH", [".", "", "bin"].join(separator))
        .env("COLUMNS", "80")
        .env("PSEUDOLIFE_MCP_DAEMON_URL", &url)
        .output()
        .unwrap();
    assert_answer(&output, 4, "", NO_SSH);
    assert!(!home.0.join("planted-ran").exists(), "the planted ssh ran");
    assert_quiet(&socket);
}

#[test]
fn unsupported_argument_shapes_receive_native_usage() {
    let home = Home::new("shapes");
    let (socket, url) = listener();
    for args in [
        &["--to=root@box"][..],
        &["--to"],
        &["--to", "-1"],
        &["--to", "-oProxyCommand=evil"],
        &["--dry", "--to", "root@box"],
        &["--yes", "--yes", "--to", "root@box"],
        &["--to", "root@box", "extra"],
    ] {
        assert_answer(&run(&home.0, &url, "80", args), 2, "", INVALID_ARGUMENTS);
    }
    let narrow_help = run(&home.0, &url, "80", &["--help"]);
    let wide_help = run(&home.0, &url, "100", &["--help"]);
    assert_eq!(wide_help.status.code(), Some(0));
    assert!(wide_help.stderr.is_empty());
    assert_eq!(narrow_help.stdout, wide_help.stdout);
    assert_answer(&run(&home.0, &url, "100", &[]), 2, "", INVALID_ARGUMENTS);
    assert_quiet(&socket);
    assert!(listing(&home.0).is_empty());
}

#[test]
fn help_and_usage_at_80_columns() {
    let home = Home::new("help");
    let (socket, url) = listener();
    for flag in ["--help", "-h"] {
        let output = run(&home.0, &url, "80", &[flag]);
        assert_eq!(output.status.code(), Some(0));
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(text.starts_with(&String::from_utf8(cli::native_text(USAGE)).unwrap()));
        assert!(text.contains("--json                one JSON report on stdout"));
        // The example target is an ordinary account, never a root login.
        assert!(
            text.contains("(user@box, or a") && !text.contains("root@"),
            "{text}"
        );
        assert!(output.stderr.is_empty());
    }
    let required =
        format!("{USAGE}pseudolife-mcp move: error: the following arguments are required: --to\n");
    assert_answer(&run(&home.0, &url, "80", &[]), 2, "", &required);
    assert_answer(
        &run(&home.0, &url, "80", &["--yes", "--json"]),
        2,
        "",
        &required,
    );
    assert_quiet(&socket);
}

#[test]
fn refusals_before_any_effect() {
    let home = Home::new("refuse");
    let (socket, url) = listener();
    for (args, stderr) in [
        (
            &["--to", "root@box", "--target-url", "http://box:8765/api"][..],
            BAD_URL,
        ),
        (&["--to", "", "--target-url", "ftp://box"], BAD_URL),
        (&["--to", "root@box", "--target-url", "ftp://x/y"], BAD_URL),
        (&["--to", ""], BAD_TO),
        (&["--to", "root@box extra"], BAD_TO),
        (&["--to", "root\u{1c}box", "--json"], BAD_TO),
        (&["--to", "root\u{1}box"], BAD_TO),
        (&["--to", "a b", "--target-url", "http://box:8765/"], BAD_TO),
        (
            &["--to", "root@box", "--target-checkout", "/srv/x\ny"],
            BAD_CHECKOUT,
        ),
        (
            &["--to", "root@box", "--target-checkout", "\r//"],
            BAD_CHECKOUT,
        ),
    ] {
        assert_answer(&run(&home.0, &url, "80", args), 2, "", stderr);
    }
    // Python reports Mover.run's refusals under --json as a JSON report;
    // the native client answers these argv checks as text, as it does
    // without --json.
    assert_answer(
        &run(&home.0, &url, "80", &["--json", "--to", "root\u{1}box"]),
        2,
        "",
        BAD_TO,
    );
    assert_answer(
        &run(
            &home.0,
            &url,
            "80",
            &["--to", "box", "--target-checkout", "x\n", "--json"],
        ),
        2,
        "",
        NO_CHECKOUT,
    );
    assert_quiet(&socket);
    assert!(listing(&home.0).is_empty(), "a refusal wrote a file");
}

/// The leaf's own source, as compiled into the binary under test.
const LEAF: &str = include_str!("../src/cli/move_cli.rs");

/// Names through which Rust code reaches the network, a child process, the
/// file system or raw OS handles. A direct `TcpStream::connect` to the live
/// daemon's port would not cross the proxy and listener checks above, so the
/// leaf's source is held to this list instead.
const IO_NAMES: &[&str] = &[
    "std::net",
    "TcpStream",
    "TcpListener",
    "UdpSocket",
    "ToSocketAddrs",
    "reqwest",
    "hyper",
    "tokio",
    "std::process",
    "Command",
    "std::fs",
    "fs::",
    "File::",
    "OpenOptions",
    "std::os",
    "libc",
    "windows_sys",
    "winapi",
    "unsafe",
    "extern",
];

#[test]
fn the_leaf_source_reaches_no_io_beyond_its_answer() {
    let hits: Vec<&str> = IO_NAMES
        .iter()
        .copied()
        .filter(|name| LEAF.contains(name))
        .collect();
    assert!(hits.is_empty(), "move_cli.rs names I/O APIs: {hits:?}");
    // No crate module (they hold the daemon, board and database clients):
    // only the dispatcher's stream encoder and the tests' glob import.
    assert!(!LEAF.contains("crate::"), "move_cli.rs uses a crate module");
    for (at, _) in LEAF.match_indices("super::") {
        let rest = &LEAF[at..];
        assert!(
            rest.starts_with("super::text_bytes(") || rest.starts_with("super::*;"),
            "move_cli.rs reaches {}",
            &rest[..rest.len().min(40)]
        );
    }
}

const MOVE_ID: &str = "20261114-080000-abcdef01";

/// A move record in `home`, as a move on this host would have left it.
fn plant(home: &Path, body: &str) {
    let folder = home.join(".pseudolife-mcp").join("moves").join(MOVE_ID);
    fs::create_dir_all(&folder).unwrap();
    fs::write(folder.join("record.json"), body).unwrap();
}

fn record(phase: &str, committed: bool, flags: &str) -> String {
    format!(
        r#"{{"version":1,"move_id":"{MOVE_ID}","target":"dev@target","target_url":"http://100.64.0.2:8765","source_bank":"75d0c7a836eb6af5","phase":"{phase}","committed":{committed},"flags":{{{flags}}}}}"#
    )
}

fn rollback(home: &Path, listener: &str, args: &[&str]) -> Output {
    let mut command: Command =
        cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("move")
        .args(args)
        .current_dir(home)
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("PATH", home)
        .env("PSEUDOLIFE_MCP_DAEMON_URL", listener)
        .stdin(std::process::Stdio::null());
    command.output().unwrap()
}

#[test]
fn an_unfinished_move_is_named_before_anything_else_is_asked() {
    let home = Home::new("unfinished");
    let (socket, url) = listener();
    plant(
        &home.0,
        &record(
            "moving",
            false,
            r#""claimed":true,"source_stopped":true,"fenced":true"#,
        ),
    );
    let want = format!(
        "move: move {MOVE_ID} to dev@target did not finish: its record (Moving) says the last thing it may have done is the source database fenced, and it is not committed. Roll it back first with pseudolife-mcp move --rollback {MOVE_ID}. Nothing was changed\n"
    );
    // A dry run, the move itself and a resume all meet it first, before the
    // ssh lookup that would otherwise refuse them (PATH holds no ssh).
    for args in [DRY, &DRY[..DRY.len() - 1], &[DRY, &["--resume"]].concat()] {
        assert_answer(&run(&home.0, &url, "80", args), 4, "", &want);
    }
    assert_quiet(&socket);
}

#[test]
fn rollback_answers_from_the_record_and_refuses_what_it_cannot_do() {
    let home = Home::new("rollback");
    let (socket, url) = listener();
    // A malformed ID never names a path.
    let output = rollback(&home.0, &url, &["--rollback", "../../etc"]);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("is not a move ID"));
    // No record of that move here.
    let output = rollback(&home.0, &url, &["--rollback", MOVE_ID]);
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("this host has no record of move"),
        "{output:?}"
    );
    // A committed move is not rolled back: the bank lives on the target.
    plant(&home.0, &record("moved", true, r#""moved_written":true"#));
    let output = rollback(&home.0, &url, &["--rollback", MOVE_ID, "--yes"]);
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("a committed move is not rolled back"),
        "{output:?}"
    );
    // One already rolled back has nothing to do.
    plant(&home.0, &record("rolled_back", false, r#""fenced":true"#));
    let output = rollback(&home.0, &url, &["--rollback", MOVE_ID, "--json"]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["exit"], 0);
    // Rolled back: the bank never moved, however often this is asked.
    assert_eq!(report["moved"], false);
    assert!(
        report["message"]
            .as_str()
            .unwrap()
            .contains("nothing to do")
    );
    // An unfinished one needs --yes without a terminal.
    plant(
        &home.0,
        &record("moving", false, r#""source_stopped":true"#),
    );
    let output = rollback(&home.0, &url, &["--rollback", MOVE_ID]);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("this run is not interactive: re-run with --yes"),
        "{output:?}"
    );
    assert_quiet(&socket);
}

#[test]
fn resume_without_a_move_to_resume_is_refused_before_any_change() {
    let home = Home::new("resume");
    let (socket, url) = listener();
    let output = run(&home.0, &url, "80", &[DRY, &["--resume"]].concat());
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("this host's records hold no move to root@box to resume"),
        "{output:?}"
    );
    assert!(listing(&home.0).is_empty(), "the refusal wrote a file");
    assert_quiet(&socket);
}

#[test]
fn a_move_record_this_client_cannot_read_blocks_a_move() {
    let home = Home::new("unreadable");
    let (socket, url) = listener();
    plant(&home.0, "{ this is not a move record");
    let output = run(&home.0, &url, "80", DRY);
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("is not a move record"), "{stderr}");
    assert!(stderr.contains("could be an unfinished move"), "{stderr}");
    assert_quiet(&socket);
}

/// A record of a move to `root@box` whose rollback left the target holding
/// its restored bank, with `extra` fields.
fn rolled_back_to_box(extra: &str) -> String {
    format!(
        r#"{{"version":1,"move_id":"{MOVE_ID}","target":"root@box","target_url":"http://100.64.0.2:8765","source_bank":"75d0c7a836eb6af5","phase":"rolled_back","committed":false,"flags":{{"claimed":true,"target_restored":true}}{extra}}}"#
    )
}

#[test]
fn release_target_answers_from_the_record_and_names_what_it_keeps() {
    let home = Home::new("release");
    let (socket, url) = listener();
    let release = |args: &[&str]| rollback(&home.0, &url, args);
    // A malformed ID never names a path.
    let output = release(&["--release-target", "../../etc"]);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    // No record of that move here.
    let output = release(&["--release-target", MOVE_ID]);
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("this host has no record of move"),
        "{output:?}"
    );
    // A committed move's target is not released.
    plant(&home.0, &record("moved", true, r#""moved_written":true"#));
    let output = release(&["--release-target", MOVE_ID, "--yes"]);
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("passed its commit point"),
        "{output:?}"
    );
    // A move not yet rolled back is rolled back first.
    plant(
        &home.0,
        &record("moving", false, r#""source_stopped":true"#),
    );
    let output = release(&["--release-target", MOVE_ID, "--yes"]);
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("roll it back first"),
        "{output:?}"
    );
    // One that never restored into the target has nothing to give back.
    plant(&home.0, &record("rolled_back", false, r#""claimed":true"#));
    let output = release(&["--release-target", MOVE_ID, "--yes"]);
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("nothing to give back"),
        "{output:?}"
    );
    // One whose target keeps the restored bank needs --yes without a
    // terminal: it replaces the target's bank.
    plant(&home.0, &rolled_back_to_box(""));
    let output = release(&["--release-target", MOVE_ID]);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("this run is not interactive: re-run with --yes"),
        "{output:?}"
    );
    // Released: --json names the files that keep the moved bank.
    plant(
        &home.0,
        &rolled_back_to_box(r#","kept":["/home/dev/k/a.sql.gz","/home/dev/k/a.tgz"]"#)
            .replace("rolled_back", "released"),
    );
    let output = release(&["--release-target", MOVE_ID, "--json"]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["moved"], false);
    assert_eq!(report["released"], true);
    assert_eq!(report["ending"], "given_back");
    assert_eq!(
        report["kept"],
        serde_json::json!(["/home/dev/k/a.sql.gz", "/home/dev/k/a.tgz"])
    );
    assert!(
        report["message"]
            .as_str()
            .unwrap()
            .contains("/home/dev/k/a.sql.gz")
    );
    // Read and found holding nothing of the move: closed, not given back.
    plant(
        &home.0,
        &rolled_back_to_box(r#","ending":"nothing_there""#).replace("rolled_back", "released"),
    );
    let output = release(&["--release-target", MOVE_ID, "--json"]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["released"], false);
    assert_eq!(report["ending"], "nothing_there");
    assert_quiet(&socket);
}

#[test]
fn a_resume_keeps_the_moves_own_folder_and_never_resumes_over_parked_writes() {
    let home = Home::new("resume-dir");
    let (socket, url) = listener();
    let resume = [DRY, &["--resume"]].concat();
    // The move's files on the target are in ~/w1: a resume names no other.
    plant(&home.0, &rolled_back_to_box(r#","work_dir":"~/w1""#));
    let output = run(
        &home.0,
        &url,
        "80",
        &[&resume[..], &["--target-work-dir", "~/w2"]].concat(),
    );
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains(&format!(
            "move {MOVE_ID} keeps its files on root@box in ~/w1"
        )),
        "{output:?}"
    );
    // A target that took writes is never resumed over; the refusal names
    // the way out.
    plant(
        &home.0,
        &rolled_back_to_box(r#","parked_writes":"entries: 1 rows""#),
    );
    let output = run(&home.0, &url, "80", &resume);
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains(&format!("pseudolife-mcp move --release-target {MOVE_ID}")),
        "{stderr}"
    );
    assert_quiet(&socket);
}

#[test]
fn a_release_that_did_not_finish_and_a_commit_killed_before_its_follow_ups_are_named() {
    let home = Home::new("release-unfinished");
    let (socket, url) = listener();
    plant(
        &home.0,
        &rolled_back_to_box("").replace("rolled_back", "releasing"),
    );
    let want = format!(
        "move: move {MOVE_ID} was rolled back, and giving root@box back its own bank did not finish; finish it first with pseudolife-mcp move --release-target {MOVE_ID}. Nothing was changed\n"
    );
    assert_answer(&run(&home.0, &url, "80", DRY), 4, "", &want);
    // Past its commit point, with steps left: --rollback names them.
    plant(
        &home.0,
        &record(
            "moved",
            true,
            r#""moved_written":true"#,
        )
        .replace(
            r#""flags""#,
            r#""follow_ups":["ssh dev@target systemctl --user enable --now pseudolife-update.timer"],"flags""#,
        ),
    );
    let output = rollback(&home.0, &url, &["--rollback", MOVE_ID, "--json"]);
    assert_eq!(output.status.code(), Some(5), "{output:?}");
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["moved"], true);
    assert!(
        report["message"]
            .as_str()
            .unwrap()
            .contains("systemctl --user enable --now pseudolife-update.timer"),
        "{report}"
    );
    assert_quiet(&socket);
}

#[test]
fn no_other_move_starts_while_an_earlier_moves_target_holds_this_hosts_bank() {
    let home = Home::new("held");
    let (socket, url) = listener();
    plant(&home.0, &rolled_back_to_box(""));
    let want = format!(
        "move: move {MOVE_ID} left root@box holding a copy of this host's bank behind its gate (it was rolled back after its restore ran there); first finish it with pseudolife-mcp move --to root@box --target-url http://100.64.0.2:8765 --no-keep-tokens --resume, or give root@box back its own bank with pseudolife-mcp move --release-target {MOVE_ID}. Nothing was changed
"
    );
    // Another target, and this one without --resume, meet it before
    // anything else is asked.
    let other: Vec<&str> = DRY
        .iter()
        .map(|w| if *w == "root@box" { "u@elsewhere" } else { w })
        .collect();
    assert_answer(&run(&home.0, &url, "80", &other), 4, "", &want);
    assert_answer(&run(&home.0, &url, "80", DRY), 4, "", &want);
    // Resuming it goes on (to the ssh lookup, which PATH lacks here).
    let output = run(&home.0, &url, "80", &[DRY, &["--resume"]].concat());
    assert!(
        !String::from_utf8_lossy(&output.stderr).contains("holding a copy"),
        "{output:?}"
    );
    assert_quiet(&socket);
}

#[test]
fn giving_up_a_target_for_good_asks_first() {
    let home = Home::new("gone-ask");
    let (socket, url) = listener();
    plant(
        &home.0,
        &rolled_back_to_box("").replace("rolled_back", "releasing"),
    );
    // Without a terminal it asks for --yes: it gives up a host for good.
    let output = rollback(
        &home.0,
        &url,
        &["--release-target", MOVE_ID, "--target-gone"],
    );
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("re-run with --yes"),
        "{output:?}"
    );
    // --target-gone belongs to --release-target only.
    let output = rollback(
        &home.0,
        &url,
        &["--rollback", MOVE_ID, "--target-gone", "--yes"],
    );
    assert_ne!(output.status.code(), Some(0), "{output:?}");
    assert_quiet(&socket);
}

#[test]
fn target_gone_asks_this_hosts_docker_and_the_target_before_it_changes_the_record() {
    let home = Home::new("gone");
    let (socket, url) = listener();
    let body = rolled_back_to_box(r#","kept":["/home/dev/k/a.sql.gz"]"#)
        .replace("rolled_back", "releasing");
    plant(&home.0, &body);
    // With --yes it must still find this host's daemon serving and the
    // target silent: no docker here, so it refuses and keeps the record.
    let output = rollback(
        &home.0,
        &url,
        &[
            "--release-target",
            MOVE_ID,
            "--target-gone",
            "--yes",
            "--json",
        ],
    );
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["released"], false);
    assert_eq!(report["kept"], serde_json::json!(["/home/dev/k/a.sql.gz"]));
    assert!(
        report["message"].as_str().unwrap().contains("docker"),
        "{report}"
    );
    let folder = home.0.join(".pseudolife-mcp/moves").join(MOVE_ID);
    assert_eq!(
        fs::read_to_string(folder.join("record.json")).unwrap(),
        body
    );
    assert_quiet(&socket);
}

#[test]
fn a_given_up_target_stops_holding_this_host_and_names_what_is_left_to_clear_by_hand() {
    let home = Home::new("given-up");
    let (socket, url) = listener();
    plant(
        &home.0,
        &rolled_back_to_box(r#","kept":["/home/dev/k/a.sql.gz"],"ending":"given_up""#)
            .replace("rolled_back", "given_up"),
    );
    // Its answers say it was given up for good, never given back, and what
    // is left to clear on the target by hand.
    let output = rollback(&home.0, &url, &["--rollback", MOVE_ID]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("gave root@box up"), "{stdout}");
    assert!(stdout.contains("that is final here"), "{stdout}");
    assert!(!stdout.contains("was given back"), "{stdout}");
    let output = rollback(
        &home.0,
        &url,
        &["--release-target", MOVE_ID, "--target-gone", "--json"],
    );
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["kept"], serde_json::json!(["/home/dev/k/a.sql.gz"]));
    assert_eq!(report["ending"], "given_up");
    assert_eq!(report["released"], false);
    assert!(
        report["message"]
            .as_str()
            .unwrap()
            .contains("already given up"),
        "{report}"
    );
    // A release of it is refused from the record: nothing is asked of any
    // host, whatever --yes says.
    let output = rollback(&home.0, &url, &["--release-target", MOVE_ID, "--yes"]);
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("that is final here"),
        "{output:?}"
    );
    // A resume of it is refused with the same true cause.
    let resume = [DRY, &["--resume"]].concat();
    let output = run(&home.0, &url, "80", &resume);
    assert_ne!(output.status.code(), Some(0), "{output:?}");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("is not resumed"), "{stderr}");
    assert!(stderr.contains("that is final here"), "{stderr}");
    // A newer move to that target, parked after the target was rebuilt, is
    // the one a resume names.
    let newer = "20261114-090000-abcdef02";
    let folder = home.0.join(".pseudolife-mcp").join("moves").join(newer);
    fs::create_dir_all(&folder).unwrap();
    fs::write(
        folder.join("record.json"),
        rolled_back_to_box(r#","parked_writes":"public.entries: 4 on the target, 3 in the dump""#)
            .replace(MOVE_ID, newer),
    )
    .unwrap();
    let output = run(&home.0, &url, "80", &resume);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains(&format!("move --release-target {newer}")),
        "{stderr}"
    );
    assert!(!stderr.contains("that is final here"), "{stderr}");
    // A newer committed move to that target: the resume does not name the
    // given-up one, and the given-up record's steps name the newer move.
    fs::write(
        folder.join("record.json"),
        record("complete", true, r#""moved_written":true"#)
            .replace(MOVE_ID, newer)
            .replace("dev@target", "root@box"),
    )
    .unwrap();
    let output = run(&home.0, &url, "80", &resume);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(!stderr.contains(&format!("move {MOVE_ID} to")), "{stderr}");
    let output = rollback(&home.0, &url, &["--release-target", MOVE_ID]);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains(&format!(
            "a newer move to root@box, move {newer} (committed"
        )),
        "{stderr}"
    );
    assert!(stderr.contains("Check first"), "{stderr}");
    fs::remove_dir_all(&folder).unwrap();
    // It no longer holds this host: a move elsewhere goes on to the ssh
    // lookup (which PATH lacks here).
    let other: Vec<&str> = DRY
        .iter()
        .map(|w| if *w == "root@box" { "u@elsewhere" } else { w })
        .collect();
    let output = run(&home.0, &url, "80", &other);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        !stderr.contains("holding a copy") && !stderr.contains("did not finish"),
        "{stderr}"
    );
    assert_quiet(&socket);
}
