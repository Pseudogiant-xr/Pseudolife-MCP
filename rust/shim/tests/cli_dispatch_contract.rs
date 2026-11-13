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
            "234a2eb42ddc228da9f229fff3d6637fe531cbd4b19f95097813eb6938e65ef6"
        } else {
            "5806d34185cdd94f2c83af42426d2954a6202abd10aba75d6dcfd9e53a65b0ef"
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
    // `console` answers every bare form itself now (with this machine's own
    // registration it would ask the daemon for a sign-in link and open a
    // browser), so it gets the one argv that defers before it reads a
    // credential: help at a width other than 80 columns. The emptied home,
    // the closed daemon address and the browser that is no program fence a
    // mode that stops deferring off the machine's real daemon and browser.
    let home = std::env::temp_dir().join(format!("pl-dispatch-home-{}", std::process::id()));
    std::fs::create_dir_all(&home).unwrap();
    let cases: [(&str, &[&str]); 5] = [
        ("maintainer", &[]),
        ("console", &["--help"]),
        ("test-login", &[]),
        ("conductor-watch", &[]),
        ("delegate-watch", &[]),
    ];
    for (mode, args) in cases {
        let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .arg(mode)
            .args(args)
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
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        let expected = bytes(&format!(
            "pseudolife-stdio: mode '{mode}' is deferred in this candidate\n"
        ));
        assert_eq!(output.stderr, expected);
    }
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
