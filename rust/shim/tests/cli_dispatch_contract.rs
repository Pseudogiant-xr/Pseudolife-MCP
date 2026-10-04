#![forbid(unsafe_code)]
use std::process::Command;

fn bytes(text: &str) -> Vec<u8> {
    if cfg!(windows) {
        text.replace('\n', "\r\n").into_bytes()
    } else {
        text.as_bytes().to_vec()
    }
}

#[test]
fn help_aliases_ignore_trailing_arguments_before_daemon_attachment() {
    for arguments in [
        vec!["help"],
        vec!["-h"],
        vec!["--help"],
        vec!["help", "anything"],
        vec!["--help", "--invalid"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .args(arguments)
            .env("PSEUDOLIFE_MCP_PYTHON", "missing-fixture-interpreter")
            .env("PSEUDOLIFE_MCP_URL", "http://127.0.0.1:1")
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(0));
        assert_eq!(output.stdout, bytes(include_str!("../src/cli_help.txt")));
        assert!(output.stderr.is_empty());
    }
}

#[test]
fn unknown_mode_uses_python_quotes_and_exit_two() {
    for (mode, representation) in [
        ("bogus", "'bogus'"),
        ("", "''"),
        ("isn't-a-mode", "\"isn't-a-mode\""),
        ("'\"mode", "'\\'\"mode'"),
        ("mode\t\n\r\x01", "'mode\\t\\n\\r\\x01'"),
        ("mode\u{a0}\u{2028}\u{3000}", "'mode\\xa0\\u2028\\u3000'"),
        ("mémoire—🧠", "'mémoire—🧠'"),
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .args([mode, "--help"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            bytes(&format!(
                "unknown mode {representation}; see: pseudolife-mcp --help\n"
            ))
        );
    }
}

#[test]
fn recognized_deferred_modes_are_not_reported_as_python_unknown_modes() {
    for mode in [
        "version",
        "--version",
        "lease",
        "board-audit",
        "maintainer",
        "test-login",
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .arg(mode)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            bytes(&format!(
                "pseudolife-stdio: mode '{mode}' is deferred in this candidate\n"
            ))
        );
    }
}
