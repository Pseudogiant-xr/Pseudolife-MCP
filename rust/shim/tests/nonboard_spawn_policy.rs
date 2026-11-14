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
fn daemon_launch_never_starts_python_and_accepts_only_a_serve_command() {
    assert!(DaemonLaunch::from_settings(None, None).unwrap().is_none());
    assert!(
        DaemonLaunch::from_settings(Some(OsStr::new("")), None)
            .unwrap()
            .is_none()
    );
    // PSEUDOLIFE_MCP_PYTHON no longer starts a Python daemon.
    assert!(
        DaemonLaunch::from_settings(Some(OsStr::new("fixture-python")), None)
            .unwrap()
            .is_none()
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

#[test]
fn an_unconfigured_or_python_only_shim_names_the_docker_tier_and_the_serve_command() {
    use pseudolife_stdio::cli::retired::{DOCKER_INSTALL, DOCKER_START};
    use pseudolife_stdio::lifecycle::{
        NO_CONFIGURED_SPAWN_NOTE, PYTHON_SPAWN_RETIRED_NOTE, spawn_settings,
    };
    for note in [NO_CONFIGURED_SPAWN_NOTE, PYTHON_SPAWN_RETIRED_NOTE] {
        assert!(note.contains(DOCKER_START), "{note}");
        assert!(note.contains(DOCKER_INSTALL), "{note}");
        assert!(note.contains("PSEUDOLIFE_MCP_SERVE_COMMAND"), "{note}");
    }
    assert!(!NO_CONFIGURED_SPAWN_NOTE.contains("PSEUDOLIFE_MCP_PYTHON"));
    assert!(PYTHON_SPAWN_RETIRED_NOTE.contains("PSEUDOLIFE_MCP_PYTHON no longer starts"));
    // Neither setting: nothing starts, and the reason is the unconfigured note.
    let (launch, reason) = spawn_settings(None, None);
    assert!(launch.is_none());
    assert_eq!(reason, Some(NO_CONFIGURED_SPAWN_NOTE));
    // Python alone: nothing starts, and the reason names the retired setting.
    let (launch, reason) = spawn_settings(Some(OsStr::new("fixture-python")), None);
    assert!(launch.is_none());
    assert_eq!(reason, Some(PYTHON_SPAWN_RETIRED_NOTE));
    // The near miss: a serve command still starts a daemon, Python or not.
    for python in [None, Some(OsStr::new("fixture-python"))] {
        let (launch, reason) = spawn_settings(python, Some(r#"["fixture-serve", "--flag"]"#));
        let launch = launch.expect("a serve command starts a daemon");
        assert_eq!(launch.program, OsStr::new("fixture-serve"));
        assert_eq!(reason, None);
    }
}

#[test]
fn an_unreachable_unconfigured_daemon_tells_the_client_how_to_start_one() {
    use pseudolife_stdio::lifecycle::{
        INVALID_SERVE_COMMAND_NOTE, NO_CONFIGURED_SPAWN_NOTE, PYTHON_SPAWN_RETIRED_NOTE,
        unreachable_remedy,
    };
    let remedy = unreachable_remedy(Some(NO_CONFIGURED_SPAWN_NOTE)).unwrap();
    assert!(
        remedy.contains(pseudolife_stdio::cli::retired::DOCKER_START),
        "{remedy}"
    );
    assert!(remedy.contains("PSEUDOLIFE_MCP_SERVE_COMMAND"), "{remedy}");
    assert!(
        unreachable_remedy(Some(PYTHON_SPAWN_RETIRED_NOTE))
            .unwrap()
            .contains("PSEUDOLIFE_MCP_PYTHON no longer starts")
    );
    // A configured launch adds nothing here; a serve command that failed to
    // parse gets its own remedy.
    assert_eq!(unreachable_remedy(None), None);
    assert!(
        unreachable_remedy(Some(INVALID_SERVE_COMMAND_NOTE))
            .unwrap()
            .contains("is not a JSON argv array")
    );
    // The note reaches the instructions the client shows its agent.
    let runtime = Runtime {
        url: "http://127.0.0.1:9".into(),
        provider: CredentialProvider::new(None, None).unwrap(),
        session: SessionIdentity::new(None, None),
        health: None,
        instructions_note: remedy.clone(),
        cache: None,
    };
    let shown = runtime.instructions(None, false, "", false).unwrap();
    assert!(
        shown.contains("did not answer when this session started"),
        "{shown}"
    );
    assert!(shown.contains(&remedy), "{shown}");
}
