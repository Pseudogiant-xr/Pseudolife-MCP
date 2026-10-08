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

fn usage_error(output: Output, message: &str) {
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.starts_with("usage: pseudolife-stdio doctor "));
    let ending = String::from_utf8(native_text(&format!(
        "pseudolife-stdio doctor: error: {message}\n"
    )))
    .unwrap();
    assert!(stderr.ends_with(&ending));
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
fn original_incompatible_owned_agent_state_cell() {
    let home = Home::new();
    let path = home.0.join("owned-state.json");
    let sentinel = b"owned state sentinel\n";
    std::fs::write(&path, sentinel).unwrap();
    let output = command(&home)
        .args(["doctor", "--disposable-proof", "--agent-state"])
        .arg(&path)
        .output()
        .unwrap();
    usage_error(output, "--disposable-proof cannot use a saved --agent-state");
    assert_eq!(std::fs::read(&path).unwrap(), sentinel);
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 1);
}

#[test]
fn refusal_uses_parsed_options_and_original_timeout_predicate() {
    let home = Home::new();
    refusal(
        command(&home)
            .args([
                "doctor",
                "--time=1",
                "--ho",
                "claude-code",
                "--dispos",
                "--timeout",
                "2_0",
            ])
            .output()
            .unwrap(),
    );
    for timeout in ["nan", "inf"] {
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
fn parser_and_timeout_errors_precede_state_and_dsn_checks() {
    let home = Home::new();
    for (arguments, message) in [
        (
            vec!["--timeout", "0", "--agent-state=", "--host", "invalid"],
            "argument --host: invalid choice: 'invalid' (choose from 'codex', 'claude-code', 'claude-desktop', 'generic')",
        ),
        (
            vec!["--timeout", "bad", "--agent-state="],
            "argument --timeout: invalid float value: 'bad'",
        ),
        (
            vec!["--timeout", "0", "--agent-state="],
            "--timeout must be positive",
        ),
        (
            vec!["--agent-state="],
            "--disposable-proof cannot use a saved --agent-state",
        ),
        (
            vec!["--host", "--agent-state="],
            "argument --host: expected one argument",
        ),
        (
            vec!["--disposable-proof=false"],
            "argument --disposable-proof: ignored explicit argument 'false'",
        ),
    ] {
        usage_error(
            command(&home)
                .args(["doctor", "--disposable-proof"])
                .args(arguments)
                .output()
                .unwrap(),
            message,
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
