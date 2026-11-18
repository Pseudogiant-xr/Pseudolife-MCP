//! Native Console diagnostics and isolation from Python-only settings.
//! Windows also tests help settings in test_console_browser.py.
#![cfg(unix)]
use std::path::Path;
use std::process::Command;

fn console(home: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
    command.env_clear().current_dir(home).arg("console");
    for key in [
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "CODEX_HOME",
    ] {
        command.env(key, home);
    }
    command
        .env("BROWSER", "console-test-no-browser")
        .env("PATH", home)
        .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1");
    command
}

#[test]
fn python_integer_limit_never_defers_console() {
    let home = std::env::temp_dir().join(format!("console-environment-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&home).unwrap();
    for value in ["0", "640", "4300", "invalid"] {
        let output = console(&home)
            .arg("--help")
            .env("COLUMNS", "100")
            .env("FORCE_COLOR", "1")
            .env("PYTHONINTMAXSTRDIGITS", value)
            .output()
            .unwrap();
        assert!(output.status.success(), "{value}: {:?}", output);
        assert!(String::from_utf8_lossy(&output.stdout).contains("usage:"));
    }
    std::fs::remove_dir_all(home).unwrap();
}

#[test]
fn rejected_settings_and_arguments_do_not_echo_credentials() {
    use std::{ffi::OsStr, os::unix::ffi::OsStrExt};

    let home = std::env::temp_dir().join(format!("console-invalid-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&home).unwrap();
    let invalid = OsStr::from_bytes(b"fixture-secret\xff");
    for (setting, value, diagnosis) in [
        ("PSEUDOLIFE_MCP_DAEMON_URL", invalid, "not valid Unicode"),
        ("PSEUDOLIFE_MCP_TOKEN_FILE", invalid, "not valid Unicode"),
        ("PSEUDOLIFE_MCP_TOKEN", invalid, "not valid Unicode"),
        (
            "PSEUDOLIFE_MCP_TOKEN",
            OsStr::new("fixture-secret-é"),
            "printable ASCII",
        ),
        (
            "PSEUDOLIFE_MCP_DAEMON_URL",
            OsStr::new("http://fixture-secret:例"),
            "is not an http(s) address",
        ),
        (
            "PSEUDOLIFE_MCP_TOKEN_FILE",
            home.as_os_str(),
            "private regular file",
        ),
    ] {
        let output = console(&home)
            .arg("--no-open")
            .env("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
            .env(setting, value)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(3), "{setting}: {diagnosis}");
        assert!(output.stdout.is_empty());
        let text = String::from_utf8_lossy(&output.stderr);
        assert!(
            text.contains(setting) && text.contains(diagnosis),
            "expected {setting}: {diagnosis}"
        );
        assert!(!text.contains("fixture-secret") && !text.contains("/ui/#/signin"));
    }
    let out = console(&home)
        .args([OsStr::new("--to"), invalid])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stdout.is_empty());
    let error = String::from_utf8_lossy(&out.stderr);
    assert!(error.contains("argument is not valid Unicode"));
    assert!(!error.contains("fixture-secret"));

    std::fs::write(home.join("config.toml"), "fixture-secret = [").unwrap();
    let out = console(&home).arg("--list").output().unwrap();
    assert_eq!(out.status.code(), Some(3));
    assert!(out.stdout.is_empty());
    let error = String::from_utf8_lossy(&out.stderr);
    assert!(error.contains("saved client registration"));
    assert!(!error.contains("fixture-secret"));
    std::fs::remove_dir_all(home).unwrap();
}

#[test]
#[cfg(target_os = "linux")]
fn help_names_a_closed_output_stream() {
    let home = std::env::temp_dir().join(format!("console-output-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&home).unwrap();
    let out = console(&home)
        .arg("--help")
        .env("COLUMNS", "80")
        .stdout(
            std::fs::File::options()
                .write(true)
                .open("/dev/full")
                .unwrap(),
        )
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(120));
    assert!(out.stdout.is_empty());
    assert_eq!(out.stderr, b"pseudolife-mcp console: stdout could not be written; restore stdout before running console again\n");
    std::fs::remove_dir_all(home).unwrap();
}
