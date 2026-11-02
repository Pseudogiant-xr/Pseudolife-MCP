use super::*;
use std::{
    io::{Read, Write},
    net::TcpListener,
    sync::{Arc, Mutex},
};

#[test]
fn board_unavailable_malformed_or_truncated_fails_closed() {
    for reply in [
        json!({}),
        json!({"agents":null}),
        json!({"agents":{}}),
        json!({"agents":[{}]}),
        json!({"agents":[1]}),
        json!({"agents":[],"enabled":false}),
        json!({"agents":[],"error":"refused"}),
        json!({"agents":[],"truncated":true}),
    ] {
        assert!(decode_activity(reply).is_err());
    }
}

#[test]
fn active_sessions_hold_and_idle_sessions_keep_their_runtime() {
    let board=decode_activity(json!({"agents":[{"agent_id":"self","label":BOARD_LABEL},{"agent_id":"peer","label":"fixture","task":"working"}],"idle_omitted":3})).unwrap();
    assert_eq!(board.active.len(), 1);
    assert_eq!(board.idle, 3);
    let error = recheck(Ok(board), "99.0.0").unwrap_err();
    assert_eq!(error.exit_code, 4);
    assert!(error.message.contains("became active during the backup"));
    let board = decode_activity(
        json!({"agents":[{"agent_id":"self","label":BOARD_LABEL}],"idle_omitted":7}),
    )
    .unwrap();
    assert!(recheck(Ok(board), "99.0.0").is_ok());
}

#[test]
fn unreadable_board_immediately_before_recreate_holds_without_movement() {
    let error = recheck(Err("unavailable".into()), "99.0.0").unwrap_err();
    assert_eq!(error.exit_code, 4);
    assert!(error.message.contains("the daemon was not recreated"));
}

#[test]
fn health_version_missing_is_unable_to_check_and_lock_io_is_not_busy() {
    for health in [
        json!({}),
        json!({"version":null}),
        json!({"version":"unknown"}),
    ] {
        assert_eq!(health_version(&health).unwrap_err().exit_code, 2);
    }
    assert!(!busy(&UpdateError::usage("cannot take the update lock")));
    assert!(busy(&UpdateError::usage(
        "another pseudolife-mcp update is running (pid unknown); nothing was changed"
    )));
}

type Requests = Arc<Mutex<Vec<Vec<u8>>>>;
fn fixture(replies: Vec<(u16, String, Value)>) -> (String, Requests, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let requests = Arc::new(Mutex::new(vec![]));
    let capture = requests.clone();
    let thread = std::thread::spawn(move || {
        for (status, extra, reply) in replies {
            let deadline = std::time::Instant::now() + Duration::from_secs(5);
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(
                            std::time::Instant::now() < deadline,
                            "fixture request missing"
                        );
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    Err(error) => panic!("fixture accept: {error}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut bytes = Vec::new();
            let mut chunk = [0; 4096];
            loop {
                let n = stream.read(&mut chunk).unwrap();
                assert!(n > 0, "incomplete request");
                bytes.extend_from_slice(&chunk[..n]);
                if let Some(split) = bytes.windows(4).position(|p| p == b"\r\n\r\n") {
                    let headers = String::from_utf8_lossy(&bytes[..split]);
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            line.to_ascii_lowercase()
                                .strip_prefix("content-length:")
                                .and_then(|s| s.trim().parse::<usize>().ok())
                        })
                        .unwrap_or(0);
                    if bytes.len() >= split + 4 + length {
                        break;
                    }
                }
                assert!(bytes.len() < 65536);
            }
            capture.lock().unwrap().push(bytes);
            let body = reply.to_string();
            write!(stream,"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n{extra}\r\n{body}",body.len()).unwrap();
        }
    });
    (url, requests, thread)
}

#[test]
fn actual_board_transport_registers_then_reads_with_instance_credentials() {
    let (url, requests, thread) = fixture(vec![
        (
            200,
            String::new(),
            json!({"agent_id":"fixture-address","credential":"fixture-key"}),
        ),
        (200, String::new(), json!({"agents":[],"idle_omitted":2})),
    ]);
    let board = board_activity_with(&url, "fixture-bearer").unwrap();
    assert!(board.active.is_empty());
    assert_eq!(board.idle, 2);
    thread.join().unwrap();
    let requests = requests.lock().unwrap();
    assert_eq!(requests.len(), 2);
    let first = String::from_utf8_lossy(&requests[0]);
    let second = String::from_utf8_lossy(&requests[1]);
    assert!(first.starts_with("POST /api/coordination/register "));
    assert!(second.starts_with("POST /api/coordination/agents "));
    assert!(
        second
            .to_ascii_lowercase()
            .contains("authorization: bearer fixture-bearer\r\n")
    );
    assert!(
        second
            .to_ascii_lowercase()
            .contains("x-pl-agent-key: fixture-key\r\n")
    );
}

#[test]
fn redirect_cannot_forward_bearer_or_board_keys() {
    let sink = TcpListener::bind("127.0.0.1:0").unwrap();
    sink.set_nonblocking(true).unwrap();
    let (url, _, thread) = fixture(vec![(
        307,
        format!("Location: http://{}/stolen\r\n", sink.local_addr().unwrap()),
        json!({}),
    )]);
    let result = post_json(
        &url,
        Some("fixture-bearer"),
        json!({}),
        Some(("fixture-address", "fixture-key")),
    )
    .unwrap();
    assert_eq!(result["http_status"], 307);
    thread.join().unwrap();
    assert_eq!(
        sink.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}

#[test]
fn refusal_never_echoes_untrusted_server_text() {
    let marker = "untrusted-secret-fixture";
    let (url, _, thread) = fixture(vec![(403, String::new(), json!({"error":marker}))]);
    let reply = post_json(&url, None, json!({}), None).unwrap();
    thread.join().unwrap();
    assert!(!reply.to_string().contains(marker));
    assert_eq!(reply["error"], "bearer refused");
}

#[test]
fn log_guard_resets_on_unwind_without_losing_the_failure_line() {
    let root = std::env::temp_dir().join(format!("unattended-log-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&root).unwrap();
    let path = root.join(LOG_NAME);
    let result = std::panic::catch_unwind(|| {
        LOG.with(|log| log.replace(Some(File::create(&path).unwrap())));
        let _guard = LogGuard;
        log_step("fixture failure");
        panic!("fixture unwind");
    });
    assert!(result.is_err());
    assert!(LOG.with(|log| log.borrow().is_none()));
    assert!(
        fs::read_to_string(path)
            .unwrap()
            .contains("fixture failure")
    );
    fs::remove_dir_all(root).unwrap();
}
