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
        // SHA-256 of cli_help.txt (cli.py::_USAGE with serve and embedded refused), with Windows CRLF.
        let expected = if cfg!(windows) {
            "111c7063f965b5b8161c0a00b51b718e76ade454ff9e40825ec108a41eeb2cdd"
        } else {
            "9eea2f71b867ffca4f188d957a6fa4fc040f231114721afcab8cd7b76e534418"
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
    // `tunnel` still declines help before attempting a tunnel operation;
    // `move` answers it natively. The empty home and closed address isolate
    // either command if its dispatch behavior changes.
    let home = std::env::temp_dir().join(format!("pl-dispatch-home-{}", std::process::id()));
    std::fs::create_dir_all(&home).unwrap();
    for mode in ["move", "tunnel"] {
        let output = common::cli::cleared_command(std::path::Path::new(env!(
            "CARGO_BIN_EXE_pseudolife-stdio"
        )))
        .args([mode, "--help"])
        .env("COLUMNS", "100")
        .env("HOME", &home)
        .env("USERPROFILE", &home)
        .env("APPDATA", &home)
        .env("LOCALAPPDATA", &home)
        .env("XDG_CONFIG_HOME", &home)
        .env("CLAUDE_CONFIG_DIR", &home)
        .env("CODEX_HOME", &home)
        .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9")
        .env("BROWSER", "pl-dispatch-no-browser")
        .env_remove("PSEUDOLIFE_MCP_TOKEN")
        .env_remove("PSEUDOLIFE_MCP_TOKEN_FILE")
        .output()
        .unwrap();
        if mode == "move" {
            assert_eq!(output.status.code(), Some(0));
            assert!(output.stderr.is_empty());
            assert!(output.stdout.starts_with(b"usage: pseudolife-mcp move"));
            continue;
        }
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        let expected = bytes(&format!(
            "pseudolife-stdio: mode '{mode}' is deferred in this candidate\n"
        ));
        assert_eq!(output.stderr, expected);
    }
    std::fs::remove_dir_all(home).unwrap();
}

#[test]
fn update_dispatches_to_the_native_updater_help() {
    let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .args(["update", "--help"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stderr.is_empty());
    assert!(String::from_utf8_lossy(&output.stdout).contains("--engine python|rust"));
}

#[test]
fn completed_modes_answer_help_and_argument_errors_without_deferring() {
    let home = std::env::temp_dir().join(format!("pl-dispatch-completed-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&home).unwrap();
    for mode in [
        "conductor-watch",
        "delegate-watch",
        "expose",
        "test-login",
        "maintainer",
        "console",
    ] {
        for (argument, code) in [("--help", 0), ("--bad-dispatch-fixture-option", 2)] {
            let output = common::cli::cleared_command(std::path::Path::new(env!(
                "CARGO_BIN_EXE_pseudolife-stdio"
            )))
            .args([mode, argument])
            .current_dir(&home)
            .env("HOME", &home)
            .env("USERPROFILE", &home)
            .env("APPDATA", &home)
            .env("LOCALAPPDATA", &home)
            .env("XDG_CONFIG_HOME", &home)
            .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9")
            .env("BROWSER", "pl-dispatch-no-browser")
            .output()
            .unwrap();
            assert_eq!(output.status.code(), Some(code), "{mode} {argument}");
            let text = format!(
                "{}{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            assert!(!text.contains("is deferred in this candidate"), "{mode}");
            assert!(!text.contains("unknown mode"), "{mode}");
            if code == 0 {
                assert!(!output.stdout.is_empty());
                if mode == "delegate-watch" {
                    assert_eq!(
                        output.stderr,
                        bytes(
                            "pseudolife-mcp delegate-watch is deprecated: use pseudolife-mcp conductor-watch\n"
                        )
                    );
                } else {
                    assert!(output.stderr.is_empty());
                }
            }
        }
    }
    std::fs::remove_dir_all(home).unwrap();
}
