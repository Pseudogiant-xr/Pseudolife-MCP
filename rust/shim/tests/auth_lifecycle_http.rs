mod auth_fixture;
use auth_fixture::DisposableHome;
use pseudolife_stdio::{
    credentials::CredentialProvider,
    lifecycle::{Runtime, SessionIdentity, post_episode, probe_health},
};
use serde_json::json;
use std::{
    io::{Read, Write},
    net::TcpListener,
    sync::mpsc,
    time::Duration,
};

fn server(
    status: &str,
    body: &str,
    extra: &str,
) -> (String, mpsc::Receiver<String>, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Length: {}\r\nContent-Type: application/json\r\nConnection: close\r\n{extra}\r\n{body}",
        body.len()
    );
    let (send, receive) = mpsc::channel();
    let worker = std::thread::spawn(move || {
        listener.set_nonblocking(true).unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        let (mut stream, _) = loop {
            match listener.accept() {
                Ok(connection) => break connection,
                Err(error)
                    if error.kind() == std::io::ErrorKind::WouldBlock
                        && std::time::Instant::now() < deadline =>
                {
                    std::thread::sleep(Duration::from_millis(5))
                }
                Err(_) => panic!("disposable endpoint received no request"),
            }
        };
        // Accepted sockets can inherit the listener's nonblocking mode on Windows.
        stream.set_nonblocking(false).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut request = Vec::new();
        let mut chunk = [0; 1024];
        while !request.windows(4).any(|bytes| bytes == b"\r\n\r\n") && request.len() < 8192 {
            let read = stream.read(&mut chunk).unwrap();
            if read == 0 {
                break;
            }
            request.extend_from_slice(&chunk[..read]);
        }
        stream.write_all(response.as_bytes()).unwrap();
        let _ = send.send(String::from_utf8_lossy(&request).into_owned());
    });
    (url, receive, worker)
}
#[test]
fn test_runtime_environment_child() {
    if std::env::var_os("AUTH_LIFECYCLE_TEST_CHILD").is_none() {
        return;
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let result = runtime.block_on(Runtime::from_environment(std::path::Path::new(
        "unused-fixture-python",
    )));
    assert!(result.is_ok());
    println!("disposable startup ran");
}
fn startup_stderr(body: &str) -> String {
    let home = DisposableHome::new();
    let (url, request, worker) = server("200 OK", body, "");
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "test_runtime_environment_child", "--nocapture"])
        .env_clear()
        // Winsock resolves its native provider through SystemRoot even for
        // numeric loopback endpoints. No user home or application env survives.
        .env(
            "SystemRoot",
            std::env::var_os("SystemRoot").unwrap_or_default(),
        )
        .env("AUTH_LIFECYCLE_TEST_CHILD", "1")
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0)
        .env("PSEUDOLIFE_AGENT_STATE_DIR", &home.0)
        .env("PSEUDOLIFE_MCP_DAEMON_URL", url)
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "disposable startup child: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        String::from_utf8_lossy(&output.stdout).contains("disposable startup ran"),
        "child stdout: {}",
        String::from_utf8_lossy(&output.stdout)
    );
    request.recv().unwrap();
    worker.join().unwrap();
    String::from_utf8(output.stderr).unwrap()
}
#[test]
fn test_shim_accept_health_says_one_line_when_it_is_not_the_daemon_version() {
    let stderr = startup_stderr("{\"version\":\"0.1.0\"}");
    assert_eq!(stderr.lines().count(), 1);
    assert!(stderr.starts_with(
        "[shim] this shim is pseudolife-mcp 0.16.0 but the daemon at http://127.0.0.1:"
    ));
    assert!(stderr.contains("is 0.1.0 — run pseudolife-mcp update --clients-only --tag 0.1.0"));
    assert!(stderr.ends_with(if cfg!(windows) {
        "then start a new session.\r\n"
    } else {
        "then start a new session.\n"
    }));
}
#[test]
fn test_shim_accept_health_is_quiet_when_versions_match_or_are_unknown() {
    for body in [
        "{}",
        "{\"version\":\"0.16.0\"}",
        "{\"version\":\"malformed value\"}",
        "{\"version\":7}",
    ] {
        assert!(startup_stderr(body).is_empty());
    }
}
#[tokio::test]
async fn test_probe_health_returns_the_payload_of_a_degraded_daemon() {
    let (url, request, worker) = server(
        "503 Service Unavailable",
        "{\"status\":\"degraded\",\"init_refusal\":\"dim mismatch\"}",
        "",
    );
    let health = probe_health(&url, Duration::from_secs(2)).await.unwrap();
    assert_eq!(health["init_refusal"], "dim mismatch");
    assert!(request.recv().unwrap().starts_with("GET /health "));
    worker.join().unwrap();
}
#[tokio::test]
async fn test_probe_health_rejects_non_json_and_follows_credential_free_redirects() {
    let (url, request, worker) = server("503 Service Unavailable", "not-json", "");
    assert!(probe_health(&url, Duration::from_secs(2)).await.is_none());
    request.recv().unwrap();
    worker.join().unwrap();
    let (destination, request, destination_worker) = server("200 OK", "{\"status\":\"ok\"}", "");
    let location = format!("Location: {destination}/health\r\n");
    let (url, redirected_request, redirect_worker) = server("302 Found", "", &location);
    assert_eq!(
        probe_health(&url, Duration::from_secs(2)).await.unwrap()["status"],
        "ok"
    );
    assert!(
        !request
            .recv()
            .unwrap()
            .to_lowercase()
            .contains("authorization:")
    );
    redirected_request.recv().unwrap();
    redirect_worker.join().unwrap();
    destination_worker.join().unwrap();
}
#[tokio::test]
async fn test_episode_post_never_forwards_bearer_across_redirect() {
    let destination = TcpListener::bind("127.0.0.1:0").unwrap();
    destination.set_nonblocking(true).unwrap();
    let location = format!(
        "Location: http://{}/leak\r\n",
        destination.local_addr().unwrap()
    );
    let (url, request, worker) = server("302 Found", "{}", &location);
    let provider = CredentialProvider::new(Some("fixture-secret".into()), None).unwrap();
    post_episode(
        &url,
        &provider,
        "/api/episode/end",
        &json!({"session_key":"fixture"}),
    )
    .await;
    assert!(
        request
            .recv()
            .unwrap()
            .to_lowercase()
            .contains("authorization: bearer fixture-secret\r\n")
    );
    assert!(destination.accept().is_err());
    worker.join().unwrap();
}
#[tokio::test]
async fn test_post_episode_is_best_effort() {
    let endpoint = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", endpoint.local_addr().unwrap());
    drop(endpoint);
    post_episode(
        &url,
        &CredentialProvider::new(None, None).unwrap(),
        "/api/episode/end",
        &json!({"session_key":"fixture"}),
    )
    .await;
    let home = DisposableHome::new();
    post_episode(
        &url,
        &CredentialProvider::new(None, Some(home.path("missing"))).unwrap(),
        "/api/episode/end",
        &json!({"session_key":"fixture"}),
    )
    .await;
}
#[tokio::test]
async fn test_own_episode_closes_at_shutdown_but_host_episode_stays_open() {
    let (url, request, worker) = server("200 OK", "{}", "");
    let mut runtime = Runtime {
        url,
        provider: CredentialProvider::new(None, None).unwrap(),
        session: SessionIdentity::new(None, None),
        health: None,
        instructions_note: String::new(),
        cache: None,
    };
    runtime.close_episode().await;
    assert!(
        request
            .recv()
            .unwrap()
            .starts_with("POST /api/episode/end ")
    );
    worker.join().unwrap();
    let endpoint = TcpListener::bind("127.0.0.1:0").unwrap();
    endpoint.set_nonblocking(true).unwrap();
    runtime.url = format!("http://{}", endpoint.local_addr().unwrap());
    runtime.session = SessionIdentity::new(None, Some("0b9c5f3e-7a1d-4c2e-9f8a-2d4e6b8c0a1f"));
    runtime.close_episode().await;
    assert!(endpoint.accept().is_err());
}
