mod auth_fixture;
use auth_fixture::DisposableHome;
use pseudolife_stdio::lifecycle::{self, DaemonControl, EnsureOptions};
use serde_json::{Value, json};
use std::{
    io::{self, Read, Write},
    net::TcpListener,
    time::Duration,
};

#[derive(Default)]
struct Control {
    now: Duration,
    probes: Vec<Duration>,
    spawns: usize,
    notes: Vec<String>,
    health_after: Option<usize>,
}
impl DaemonControl for Control {
    type Child = ();
    fn now(&self) -> Duration {
        self.now
    }
    async fn sleep(&mut self, duration: Duration) {
        self.now += duration;
    }
    async fn probe(&mut self, _: &str, timeout: Duration) -> Option<Value> {
        self.probes.push(timeout);
        self.health_after
            .filter(|after| self.probes.len() >= *after)
            .map(|_| json!({"status":"ok"}))
    }
    fn spawn(&mut self) -> io::Result<()> {
        self.spawns += 1;
        Ok(())
    }
    fn exited(&mut self, _: &mut ()) -> Option<i32> {
        None
    }
    fn note(&mut self, note: &str) {
        self.notes.push(note.into());
    }
}
#[tokio::test]
async fn test_no_spawn_env_falsy_value_still_spawns() {
    let home = DisposableHome::new();
    for raw in [
        None,
        Some(""),
        Some("0"),
        Some("false"),
        Some("no"),
        Some("off"),
    ] {
        let url = "http://127.0.0.1:18765";
        let mut control = Control {
            health_after: Some(3),
            ..Default::default()
        };
        assert!(
            lifecycle::ensure_daemon(
                url,
                &mut control,
                &EnsureOptions::new(url, lifecycle::spawn_disabled(raw), &home.0)
            )
            .await
            .unwrap()
            .is_some()
        );
        assert_eq!(control.spawns, 1);
    }
}
#[tokio::test]
async fn test_loopback_daemon_url_forms_still_spawn() {
    let home = DisposableHome::new();
    for url in [
        "http://localhost:8765",
        "http://127.0.0.1:8765",
        "http://127.2.3.4:8765",
        "http://[::1]:8765",
    ] {
        let mut control = Control {
            health_after: Some(3),
            ..Default::default()
        };
        assert!(
            lifecycle::ensure_daemon(url, &mut control, &EnsureOptions::new(url, false, &home.0))
                .await
                .unwrap()
                .is_some()
        );
        assert_eq!(control.spawns, 1);
    }
}
#[tokio::test]
async fn test_non_loopback_daemon_url_starts_without_the_daemon_and_names_the_remote_remedy() {
    let home = DisposableHome::new();
    let url = "http://100.64.0.2:8765";
    let mut control = Control::default();
    assert!(
        lifecycle::ensure_daemon(url, &mut control, &EnsureOptions::new(url, false, &home.0))
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(control.spawns, 0);
    let notes = control.notes.join("\n");
    for required in ["100.64.0.2:8765", "another machine", "retr"] {
        assert!(notes.contains(required));
    }
    assert!(!notes.contains("docker compose"));
}
#[test]
fn test_external_daemon_waits_fit_the_host_startup_budget() {
    assert!(lifecycle::EXTERNAL_WAIT + lifecycle::REMOTE_PROBE_TIMEOUT < Duration::from_secs(10));
    assert!(lifecycle::EXTERNAL_WAIT + Duration::from_millis(500) < Duration::from_secs(10));
}
#[tokio::test]
async fn test_remote_daemon_probes_use_the_remote_timeout() {
    let home = DisposableHome::new();
    let url = "http://100.64.0.2:8765";
    let mut control = Control {
        health_after: Some(3),
        ..Default::default()
    };
    lifecycle::ensure_daemon(url, &mut control, &EnsureOptions::new(url, false, &home.0))
        .await
        .unwrap();
    assert!(!control.probes.is_empty());
    assert!(
        control
            .probes
            .iter()
            .all(|probe| *probe <= lifecycle::REMOTE_PROBE_TIMEOUT)
    );
    assert_eq!(control.probes[0], lifecycle::REMOTE_PROBE_TIMEOUT);
}
#[test]
fn test_run_shim_stops_before_daemon_traffic_when_it_holds_no_credential() {
    auth_startup(false, false);
}
#[test]
fn degraded_auth_health_refuses_an_unusable_token_file() {
    auth_startup(true, true);
}
fn auth_startup(degraded: bool, missing_file: bool) {
    let home = DisposableHome::new();
    let socket = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", socket.local_addr().unwrap());
    let worker = std::thread::spawn(move || {
        let (mut stream, _) = socket.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut input = [0; 8192];
        let size = stream.read(&mut input).unwrap();
        assert!(String::from_utf8_lossy(&input[..size]).starts_with("GET /health "));
        let body = json!({"status":if degraded{"degraded"}else{"ok"},"auth":true}).to_string();
        write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
        socket
    });
    let mut command = std::process::Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
    command
        .env_clear()
        .env(
            "SystemRoot",
            std::env::var_os("SystemRoot").unwrap_or_default(),
        )
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0)
        .env("PSEUDOLIFE_AGENT_STATE_DIR", &home.0)
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
        .env("PSEUDOLIFE_MCP_DAEMON_URL", url);
    command.env(
        "PSEUDOLIFE_MCP_PYTHON",
        std::env::var_os("PSEUDOLIFE_MCP_PYTHON").unwrap_or_else(|| "python".into()),
    );
    if missing_file {
        command.env("PSEUDOLIFE_MCP_TOKEN_FILE", home.path("missing-token"));
    }
    let output = command.output().unwrap();
    let socket = worker.join().unwrap();
    socket.set_nonblocking(true).unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert!(
        socket.accept().is_err(),
        "MCP or episode request escaped auth startup gate"
    );
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.to_lowercase().contains("auth") || missing_file);
    if missing_file {
        assert!(stderr.contains("missing-token"));
        assert!(stderr.contains("PSEUDOLIFE_MCP_TOKEN_FILE"));
    }
}
