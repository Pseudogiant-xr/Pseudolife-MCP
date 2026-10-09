#![forbid(unsafe_code)]
mod common;
use common::cli::native_text as bytes;
use sha2::{Digest, Sha256};
use std::process::Command;

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
        // SHA-256 of pinned cli.py::_USAGE UTF-8 bytes, with Windows CRLF.
        let expected = if cfg!(windows) {
            "9ccc588d6cecc91c1cd69cae360941e501552ec7c5a5a18d8881f6c9b4af4eba"
        } else {
            "759a7ecda00a610211478fa811804ee5bba9092f55ebf7478bed54c041c9230a"
        };
        assert_eq!(format!("{:x}", Sha256::digest(&output.stdout)), expected);
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
    for mode in ["board-audit", "maintainer", "test-login"] {
        let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .arg(mode)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        let expected = if mode == "board-audit" {
            bytes(concat!(
                "board-audit: this path is deferred; ",
                "native board-audit covers canonical verify, export and redact\n"
            ))
        } else {
            bytes(&format!(
                "pseudolife-stdio: mode '{mode}' is deferred in this candidate\n"
            ))
        };
        assert_eq!(output.stderr, expected);
    }
}
