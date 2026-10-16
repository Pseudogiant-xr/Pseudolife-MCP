mod auth_fixture;
mod common;
use auth_fixture::DisposableHome;
use pseudolife_stdio::{
    credentials::CredentialProvider,
    lifecycle::{DaemonLaunch, Runtime, SessionIdentity},
};
use serde_json::json;
use std::{ffi::OsStr, process::Command};

#[test]
fn daemon_launch_has_no_implicit_python_and_accepts_only_explicit_settings() {
    assert!(DaemonLaunch::from_settings(None, None).unwrap().is_none());
    assert!(
        DaemonLaunch::from_settings(Some(OsStr::new("")), None)
            .unwrap()
            .is_none()
    );
    let python = DaemonLaunch::from_settings(Some(OsStr::new("fixture-python")), None)
        .unwrap()
        .unwrap();
    assert_eq!(python.program, OsStr::new("fixture-python"));
    assert_eq!(
        python
            .args
            .iter()
            .map(|arg| arg.to_str().unwrap())
            .collect::<Vec<_>>(),
        ["-m", "pseudolife_memory.cli", "serve"]
    );
    let explicit = DaemonLaunch::from_settings(
        Some(OsStr::new("ignored-python")),
        Some(r#"["fixture-serve", "an argument", ""]"#),
    )
    .unwrap()
    .unwrap();
    assert_eq!(explicit.program, OsStr::new("fixture-serve"));
    assert_eq!(
        explicit
            .args
            .iter()
            .map(|arg| arg.to_str().unwrap())
            .collect::<Vec<_>>(),
        ["an argument", ""]
    );
    for invalid in ["[]", r#"[""]"#, r#"["serve", 1]"#, r#""serve""#, "not-json"] {
        let error = DaemonLaunch::from_settings(None, Some(invalid)).unwrap_err();
        assert_eq!(error.code, 1);
        assert_eq!(
            error.message,
            "[shim] PSEUDOLIFE_MCP_SERVE_COMMAND must be a JSON argv array with a nonempty executable"
        );
    }
}

#[test]
fn available_daemon_initializes_without_a_python_runtime() {
    let fixture = common::Fixture::start();
    let mut shim = common::Shim::start_with_env(
        &fixture.url,
        &[("PSEUDOLIFE_MCP_PYTHON", "absent-fixture-python")],
    );
    shim.send(json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"no-python-startup","version":"1"}}}));
    assert!(shim.receive().get("result").is_some());
    shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
    assert!(shim.finish().is_empty());
    assert!(shim.stderr().is_empty());
    fixture.assert_deleted();
}

#[test]
fn remote_update_child() {
    if std::env::var_os("REMOTE_UPDATE_TEST_CHILD").is_none() {
        return;
    }
    let runtime = Runtime {
        url: "https://daemon.example.test".into(),
        provider: CredentialProvider::new(None, None).unwrap(),
        session: SessionIdentity::new(None, None),
        health: Some(
            json!({"status":"ok","version":"99.0.0","updates":{"unattended_clients":true}}),
        ),
        instructions_note: String::new(),
        cache: None,
    };
    assert!(runtime.client_updates_after_first_frame().is_empty());
}

#[test]
fn remote_url_never_launches_the_python_updater() {
    let home = DisposableHome::new();
    let output = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "remote_update_child"])
        .env_clear()
        .env(
            "SystemRoot",
            std::env::var_os("SystemRoot").unwrap_or_default(),
        )
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0)
        .env("PSEUDOLIFE_MCP_PYTHON", "absent-fixture-python")
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
        .env("REMOTE_UPDATE_TEST_CHILD", "1")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "remote callback child: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        !home.path(".pseudolife-mcp").exists(),
        "remote callback attempted a client update"
    );
    assert!(output.stderr.is_empty());
}
