mod auth_fixture;
mod common;
use auth_fixture::DisposableHome;
use common::Shim;
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::Duration,
};

#[test]
fn test_each_operation_uses_one_fresh_credential_snapshot() {
    let home = DisposableHome::new();
    let token = home.private_token("token", b"old-fixture-token");
    let socket = TcpListener::bind("127.0.0.1:0").unwrap();
    socket.set_nonblocking(true).unwrap();
    let url = format!("http://{}", socket.local_addr().unwrap());
    let stopped = Arc::new(AtomicBool::new(false));
    let stop = stopped.clone();
    let release = Arc::new((Mutex::new(false), Condvar::new()));
    let gate = release.clone();
    let records = Arc::new(Mutex::new(Vec::<(String, Value)>::new()));
    let observed = records.clone();
    let (blocked, initialized) = mpsc::channel();
    let server = std::thread::spawn(move || {
        let mut tasks = Vec::new();
        while !stop.load(Ordering::SeqCst) {
            match socket.accept() {
                Ok((mut stream, _)) => {
                    let (records, release, blocked) =
                        (observed.clone(), gate.clone(), blocked.clone());
                    tasks.push(std::thread::spawn(move || {
                        let (request, body) = read_request(&mut stream);
                        let message: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
                        let auth = request.lines().find_map(|line| line.split_once(':').filter(|(name, _)| name.eq_ignore_ascii_case("authorization")).map(|(_, value)| value.trim().to_owned())).unwrap_or_default();
                        let result = if request.starts_with("GET /health ") { json!({"status":"ok"}) }
                        else if message["method"] == "initialize" {
                            let old = { let mut records = records.lock().unwrap(); let old = records.iter().any(|(_, message)| message["method"] == "initialize"); records.push((auth.clone(), message.clone())); old };
                            if old && auth == "Bearer old-fixture-token" {
                                blocked.send(()).unwrap();
                                let (lock, wake) = &*release;
                                let ready = wake.wait_timeout_while(lock.lock().unwrap(), Duration::from_secs(5), |ready| !*ready).unwrap();
                                assert!(*ready.0, "rotation test never released initialize");
                            }
                            json!({"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"rotation","version":"1"}})
                        } else if message["method"] == "tools/call" {
                            records.lock().unwrap().push((auth, message.clone()));
                            json!({"content":[{"type":"text","text":message["params"]["arguments"]["label"]}],"isError":false})
                        } else { json!({}) };
                        let (status, value) = if request.starts_with("GET /mcp ") { (405, Value::Null) }
                        else if request.starts_with("DELETE ") || message.get("id").is_none() && !request.starts_with("GET /health ") { (204, Value::Null) }
                        else if message.get("id").is_some() { (200, json!({"jsonrpc":"2.0","id":message["id"],"result":result})) }
                        else { (200, result) };
                        let body = if status == 204 { String::new() } else { value.to_string() };
                        let _ = write!(stream, "HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nMcp-Session-Id: rotation\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
                    }));
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(2))
                }
                Err(error) => panic!("fixture accept: {error}"),
            }
        }
        for task in tasks {
            task.join().unwrap();
        }
    });
    let mut shim = Shim::start_with_env(
        &url,
        &[("PSEUDOLIFE_MCP_TOKEN_FILE", token.to_str().unwrap())],
    );
    shim.send(json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"rotation","version":"1"}}}));
    assert!(shim.receive().get("result").is_some());
    shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
    shim.send(json!({"jsonrpc":"2.0","id":"old","method":"tools/call","params":{"name":"read","arguments":{"label":"old"}}}));
    initialized.recv_timeout(Duration::from_secs(3)).unwrap();
    std::fs::write(&token, b"new-fixture-token").unwrap();
    shim.send(json!({"jsonrpc":"2.0","id":"new","method":"tools/call","params":{"name":"read","arguments":{"label":"new"}}}));
    *release.0.lock().unwrap() = true;
    release.1.notify_all();
    let mut replies = std::collections::HashMap::new();
    while replies.len() < 2 {
        let value = shim.receive();
        if let Some(id) = value["id"].as_str() {
            replies.insert(id.to_owned(), value);
        }
    }
    assert_eq!(
        replies["old"]["error"]["data"],
        json!({"classification":"credential_unavailable","phase":"initialize","operation_outcome":"not_dispatched"})
    );
    assert_eq!(replies["new"]["result"]["content"][0]["text"], "new");
    assert!(shim.finish().is_empty());
    stopped.store(true, Ordering::SeqCst);
    server.join().unwrap();
    let records = records.lock().unwrap();
    assert!(
        records
            .iter()
            .any(|(auth, message)| auth == "Bearer old-fixture-token"
                && message["method"] == "initialize")
    );
    let calls: Vec<_> = records
        .iter()
        .filter(|(_, message)| message["method"] == "tools/call")
        .collect();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].0, "Bearer new-fixture-token");
    assert_eq!(calls[0].1["params"]["arguments"]["label"], "new");
}
fn read_request(stream: &mut TcpStream) -> (String, Vec<u8>) {
    stream.set_nonblocking(false).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut bytes = Vec::new();
    let mut chunk = [0; 4096];
    let end = loop {
        let read = stream.read(&mut chunk).unwrap();
        if read == 0 {
            return (String::new(), Vec::new());
        }
        bytes.extend_from_slice(&chunk[..read]);
        if let Some(end) = bytes.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
            break end + 4;
        }
    };
    let header = String::from_utf8(bytes[..end].to_vec()).unwrap();
    let length = header
        .lines()
        .find_map(|line| {
            line.split_once(':')
                .filter(|(name, _)| name.eq_ignore_ascii_case("content-length"))
                .map(|(_, value)| value.trim().parse::<usize>().unwrap())
        })
        .unwrap_or(0);
    while bytes.len() < end + length {
        let read = stream.read(&mut chunk).unwrap();
        assert_ne!(read, 0);
        bytes.extend_from_slice(&chunk[..read]);
    }
    (header, bytes[end..end + length].to_vec())
}
