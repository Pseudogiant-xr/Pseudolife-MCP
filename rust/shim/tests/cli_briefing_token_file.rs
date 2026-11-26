//! `pseudolife-mcp briefing` reads the bearer from `PSEUDOLIFE_MCP_TOKEN_FILE`,
//! the setting the installers write, not only from `PSEUDOLIFE_MCP_TOKEN`.
mod auth_fixture;
use auth_fixture::DisposableHome;
use std::{
    io::{Read, Write},
    net::TcpListener,
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

/// Runs `briefing` against a scripted daemon and returns its output and the
/// request heads the daemon received.
fn briefing(home: &Path, env: &[(&str, &std::ffi::OsStr)]) -> (std::process::Output, Vec<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let stopped = stop.clone();
    let peer = std::thread::spawn(move || {
        let replies = ["{}", "{\"markdown\":\"briefed\"}"];
        let mut requests = Vec::new();
        while !stopped.load(std::sync::atomic::Ordering::SeqCst) && requests.len() < replies.len() {
            let mut stream = match listener.accept() {
                Ok((stream, _)) => stream,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(5));
                    continue;
                }
                Err(error) => panic!("fixture peer: {error}"),
            };
            stream.set_nonblocking(false).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut request = Vec::new();
            while !request.ends_with(b"\r\n\r\n") {
                let mut byte = [0];
                stream.read_exact(&mut byte).unwrap();
                request.push(byte[0]);
            }
            let body = replies[requests.len()];
            requests.push(String::from_utf8(request).unwrap());
            write!(
                stream,
                "HTTP/1.1 200 fixture\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
            stream.flush().unwrap();
        }
        requests
    });
    let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
    command
        .env_clear()
        .env("PATH", "")
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("LOCALAPPDATA", home)
        .env("PSEUDOLIFE_MCP_DAEMON_URL", format!("http://{address}"))
        .arg("briefing")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for name in ["SYSTEMROOT", "WINDIR"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    for (name, value) in env {
        command.env(name, value);
    }
    let mut child = command.spawn().unwrap();
    let deadline = Instant::now() + Duration::from_secs(8);
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            child.kill().unwrap();
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    let output = child.wait_with_output().unwrap();
    stop.store(true, std::sync::atomic::Ordering::SeqCst);
    (output, peer.join().unwrap())
}

fn line(text: &str) -> Vec<u8> {
    if cfg!(windows) {
        format!("{text}\r\n").into_bytes()
    } else {
        format!("{text}\n").into_bytes()
    }
}

#[test]
fn briefing_sends_the_token_file_bearer_without_a_token_variable() {
    let home = DisposableHome::new();
    let token = home.private_token("client.token", b"file-secret\n");
    let (output, requests) = briefing(&home.0, &[("PSEUDOLIFE_MCP_TOKEN_FILE", token.as_os_str())]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    assert_eq!(output.stdout, line("briefed"));
    assert!(output.stderr.is_empty(), "{output:?}");
    assert_eq!(requests.len(), 2, "{requests:?}");
    assert!(requests[0].starts_with("GET /health HTTP/1.1\r\n"));
    assert!(!requests[0].to_ascii_lowercase().contains("\r\nauthorization:"));
    assert!(requests[1].starts_with("GET /api/briefing?"));
    assert!(
        requests[1]
            .to_ascii_lowercase()
            .contains("\r\nauthorization: bearer file-secret\r\n"),
        "{requests:?}"
    );
}

#[test]
fn briefing_prefers_the_token_file_over_a_stale_token_variable() {
    let home = DisposableHome::new();
    let token = home.private_token("client.token", b"file-secret");
    let (output, requests) = briefing(
        &home.0,
        &[
            ("PSEUDOLIFE_MCP_TOKEN_FILE", token.as_os_str()),
            ("PSEUDOLIFE_MCP_TOKEN", "stale-secret".as_ref()),
        ],
    );
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    assert_eq!(output.stdout, line("briefed"));
    let headers = requests[1].to_ascii_lowercase();
    assert!(headers.contains("\r\nauthorization: bearer file-secret\r\n"), "{requests:?}");
    assert!(!headers.contains("stale-secret"));
}

#[test]
fn briefing_with_a_missing_token_file_sends_no_payload_request() {
    let home = DisposableHome::new();
    let missing = home.path("absent.token");
    let (output, requests) = briefing(
        &home.0,
        &[
            ("PSEUDOLIFE_MCP_TOKEN_FILE", missing.as_os_str()),
            ("PSEUDOLIFE_MCP_TOKEN", "stale-secret".as_ref()),
        ],
    );
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    assert!(output.stdout.is_empty() && output.stderr.is_empty(), "{output:?}");
    assert_eq!(requests.len(), 1, "{requests:?}");
    assert!(requests[0].starts_with("GET /health HTTP/1.1\r\n"));
}
