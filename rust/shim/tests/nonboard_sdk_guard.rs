mod auth_fixture;
mod common;
use auth_fixture::DisposableHome;
use pseudolife_stdio::sdk_guard::{PROBE_MODULE, require_sdk};
use std::{net::TcpListener, path::PathBuf, process::Command, time::Duration};
fn python() -> PathBuf {
    std::env::var_os("PSEUDOLIFE_MCP_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| "python".into())
}
fn expected(module: &str) -> String {
    let output=Command::new(python()).args(["-c","import json,sys,importlib.metadata;print(json.dumps([sys.executable,importlib.metadata.version('mcp')]))"]).output().unwrap();
    let identity: Vec<String> = serde_json::from_slice(&output.stdout).unwrap();
    format!(
        "[shim] this environment's MCP SDK (mcp {}) predates v2 — the shim needs mcp>=2.1 (no {module}).\n  Fix:  \"{}\" -m pip install -U \"mcp>=2.1,<3\"\n  (or re-run the repo installer, which registers the project venv's shim)",
        identity[1], identity[0]
    )
}
#[tokio::test]
async fn test_sdk_guard_passes_on_a_v2_environment() {
    require_sdk(&python(), PROBE_MODULE).await.unwrap();
    let fixture = common::Fixture::start();
    let mut shim = common::Shim::start(&fixture.url);
    shim.send(serde_json::json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"sdk-guard","version":"1"}}}));
    assert!(shim.receive().get("result").is_some());
    shim.send(serde_json::json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
    assert!(shim.finish().is_empty());
    assert!(shim.stderr().is_empty());
    fixture.assert_deleted();
}
#[tokio::test]
async fn test_sdk_guard_survives_a_fully_absent_mcp() {
    let error = require_sdk(&python(), "pseudolife_test_absent_parent.sub")
        .await
        .unwrap_err();
    assert_eq!(error.code, 1);
    assert_eq!(error.message, expected("pseudolife_test_absent_parent.sub"));
}
#[tokio::test]
async fn test_sdk_guard_names_the_fix_and_exits_before_daemon_traffic() {
    let home = DisposableHome::new();
    // Shadow only the disposable child's mcp package; the chosen interpreter
    // and its installed distribution remain unchanged.
    let package = home.path("mcp");
    std::fs::create_dir(&package).unwrap();
    std::fs::write(package.join("__init__.py"), b"").unwrap();
    let socket = TcpListener::bind("127.0.0.1:0").unwrap();
    socket.set_nonblocking(true).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .current_dir(&home.0)
        .env("PYTHONPATH", &home.0)
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0)
        .env("PSEUDOLIFE_MCP_PYTHON", python())
        .env("PSEUDOLIFE_AGENT_STATE_DIR", &home.0)
        .env(
            "PSEUDOLIFE_MCP_DAEMON_URL",
            format!("http://{}", socket.local_addr().unwrap()),
        )
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert!(socket.accept().is_err(), "SDK guard issued daemon traffic");
    let stderr = String::from_utf8(output.stderr).unwrap();
    let mut expected = expected(PROBE_MODULE);
    expected.push('\n');
    if cfg!(windows) {
        expected = expected.replace('\n', "\r\n");
    }
    assert_eq!(stderr, expected);
}
#[test]
fn probe_process_tree_is_reaped_before_failure_returns() {
    let home = DisposableHome::new();
    let package = home.path("mcp");
    std::fs::create_dir(&package).unwrap();
    let pidfile = home.path("grandchild.pid");
    let address = TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap();
    let ready = home.path("grandchild.ready");
    let code=format!("import subprocess,sys,pathlib,time\np=subprocess.Popen([sys.executable,'-c',{}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))\npathlib.Path({}).write_text(str(p.pid))\nready=pathlib.Path({})\nfor _ in range(200):\n    if ready.exists(): break\n    time.sleep(.01)\nassert ready.exists()\n",serde_json::to_string(&format!("import socket,time,pathlib;s=socket.socket();s.bind(('127.0.0.1',{}));s.listen();pathlib.Path({}).write_text('ready');time.sleep(30)",address.port(),serde_json::to_string(ready.to_str().unwrap()).unwrap())).unwrap(),serde_json::to_string(pidfile.to_str().unwrap()).unwrap(),serde_json::to_string(ready.to_str().unwrap()).unwrap());
    std::fs::write(package.join("__init__.py"), code).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .current_dir(&home.0)
        .env("PYTHONPATH", &home.0)
        .env("PSEUDOLIFE_MCP_PYTHON", python())
        .env("PSEUDOLIFE_AGENT_STATE_DIR", &home.0)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(pidfile.is_file());
    assert!(ready.is_file());
    // A new bind proves the owned descendant cannot keep its listener alive.
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    loop {
        if TcpListener::bind(address).is_ok() {
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "probe child kept its socket"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}
