#![forbid(unsafe_code)]
mod common;
use common::{LeaseHome as Home, cli::native_text};
use std::{
    ffi::OsStr,
    process::{Command, Output},
};

const REFUSAL: &str = "{\"ok\": false, \"error\": \"ExplicitDisposableDatabaseRequired\", \"recovery\": \"Set PSEUDOLIFE_TEST_DATABASE_URL to an explicitly disposable fixture server; no configured bank or bench default is used.\"}\n";

fn command(home: &Home) -> Command {
    let mut command = home.command();
    command
        .env("COLUMNS", "80")
        .env("CODEX_HOME", home.0.join("codex"))
        .env("CLAUDE_CONFIG_DIR", home.0.join("claude"))
        .env("XDG_CONFIG_HOME", home.0.join("config"));
    command
}

fn refusal(output: Output) {
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(output.stdout, native_text(REFUSAL));
    assert!(output.stderr.is_empty());
}

fn deferred(output: Output) {
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        native_text("pseudolife-stdio: mode 'doctor' is deferred in this candidate\n")
    );
}

#[test]
fn original_absent_and_empty_disposable_dsn_cells() {
    let home = Home::new();
    for dsn in [None, Some(OsStr::new(""))] {
        let mut command = command(&home);
        if let Some(dsn) = dsn {
            command.env("PSEUDOLIFE_TEST_DATABASE_URL", dsn);
        }
        refusal(
            command
                .args(["doctor", "--disposable-proof"])
                .output()
                .unwrap(),
        );
        assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
    }
}

#[test]
fn canonical_saved_state_requests_stay_deferred_without_mutation() {
    let home = Home::new();
    let path = home.0.join("owned-state.json");
    let sentinel = b"owned state sentinel\n";
    std::fs::write(&path, sentinel).unwrap();
    for dsn in ["", "postgresql://fixture.invalid/disposable"] {
        for proof_first in [None, Some(false), Some(true)] {
            let mut command = command(&home);
            command
                .env("PSEUDOLIFE_TEST_DATABASE_URL", dsn)
                .arg("doctor");
            if proof_first == Some(true) {
                command.arg("--disposable-proof");
            }
            command.arg("--agent-state").arg(&path);
            if proof_first == Some(false) {
                command.arg("--disposable-proof");
            }
            deferred(command.output().unwrap());
        }
    }
    assert_eq!(std::fs::read(&path).unwrap(), sentinel);
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 1);
}

#[test]
fn canonical_options_allow_any_order_and_the_four_hosts() {
    let home = Home::new();
    for host in ["codex", "claude-code", "claude-desktop", "generic"] {
        for arguments in [
            vec!["--disposable-proof", "--timeout", "20", "--host", host],
            vec!["--disposable-proof", "--host", host, "--timeout", "20"],
            vec!["--timeout", "20", "--disposable-proof", "--host", host],
            vec!["--timeout", "20", "--host", host, "--disposable-proof"],
            vec!["--host", host, "--disposable-proof", "--timeout", "20"],
            vec!["--host", host, "--timeout", "20", "--disposable-proof"],
        ] {
            refusal(
                command(&home)
                    .arg("doctor")
                    .args(arguments)
                    .output()
                    .unwrap(),
            );
        }
    }
    for timeout in ["0.5", ".5", "1.", "00020.0"] {
        refusal(
            command(&home)
                .args(["doctor", "--timeout", timeout, "--disposable-proof"])
                .output()
                .unwrap(),
        );
    }
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}

#[test]
fn noncanonical_arguments_defer_without_parser_diagnostics() {
    let home = Home::new();
    for arguments in [
        vec!["--time", "20"],
        vec!["--timeout=20"],
        vec!["--host=generic"],
        vec!["--agent-state=owned-state.json"],
        vec!["--disposable-proof=false"],
        vec!["--dispos"],
        vec!["--h"],
        vec!["--help"],
        vec!["-h"],
        vec!["--"],
        vec!["unexpected"],
        vec!["--host", "invalid"],
        vec!["--host"],
        vec!["--timeout"],
        vec!["--agent-state"],
        vec!["--agent-state", ""],
        vec!["--agent-state", "-owned-state.json"],
        vec!["--disposable-proof"],
        vec!["--timeout", "20", "--timeout", "30"],
        vec!["--host", "generic", "--host", "codex"],
    ] {
        deferred(
            command(&home)
                .args(["doctor", "--disposable-proof"])
                .args(arguments)
                .output()
                .unwrap(),
        );
    }
    let overflow = "9".repeat(400);
    for timeout in [
        "",
        ".",
        "0",
        "-1",
        "-1\n",
        "-.5\n",
        "+20",
        "2_0",
        "nan",
        "inf",
        "1e2",
        " 20",
        "20\n",
        "1.2.3",
        "２０",
        &overflow,
    ] {
        deferred(
            command(&home)
                .args(["doctor", "--disposable-proof", "--timeout", timeout])
                .output()
                .unwrap(),
        );
    }
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}

#[test]
fn normal_diagnostics_nonempty_proof_and_unsupported_help_stay_deferred() {
    let home = Home::new();
    deferred(command(&home).arg("doctor").output().unwrap());
    deferred(
        command(&home)
            .args(["doctor", "--disposable-proof", "--help"])
            .output()
            .unwrap(),
    );
    for dsn in [" ", "postgresql://fixture.invalid/disposable"] {
        deferred(
            command(&home)
                .env("PSEUDOLIFE_TEST_DATABASE_URL", dsn)
                .args(["doctor", "--disposable-proof"])
                .output()
                .unwrap(),
        );
    }
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}

#[test]
fn non_unicode_nonempty_dsn_stays_deferred() {
    #[cfg(unix)]
    let dsn = {
        use std::os::unix::ffi::OsStringExt;
        std::ffi::OsString::from_vec(vec![0xff])
    };
    #[cfg(windows)]
    let dsn = {
        use std::os::windows::ffi::OsStringExt;
        std::ffi::OsString::from_wide(&[0xd800])
    };
    let home = Home::new();
    deferred(
        command(&home)
            .env("PSEUDOLIFE_TEST_DATABASE_URL", dsn)
            .args(["doctor", "--disposable-proof"])
            .output()
            .unwrap(),
    );
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}
