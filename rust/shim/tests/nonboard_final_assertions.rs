mod auth_fixture;
mod common;
use auth_fixture::DisposableHome;
use pseudolife_stdio::{
    credentials::CredentialProvider,
    lifecycle::{self, ClientUpdates, Runtime},
};
use serde_json::{Value, json};
use std::{
    fs,
    io::{self, Read, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Command, Output},
    sync::mpsc,
    time::{Duration, Instant, SystemTime},
};

fn python() -> PathBuf {
    let selected = std::env::var_os("PSEUDOLIFE_MCP_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| "python".into());
    // Resolve before the disposable child clears PATH and changes directory.
    lifecycle::find_executable(selected)
        .expect("selected Python runtime for the disposable fixture")
}
fn health() -> Value {
    json!({"status":"ok","auth":false,"version":"99.0.0","updates":{"unattended_clients":true}})
}
fn updates(home: &DisposableHome) -> ClientUpdates {
    ClientUpdates::new(
        home.path("state"),
        python(),
        "0.16.0",
        "fixture-command",
        true,
    )
}

#[test]
fn assertion_child() {
    let Ok(mode) = std::env::var("NONBOARD_ASSERTION_CHILD") else {
        return;
    };
    if mode == "auth" {
        let provider = CredentialProvider::new(None, None).unwrap();
        for value in [
            json!({"status":"ok","auth":false}),
            json!({"status":"ok"}),
            json!({"status":"degraded","auth":false,"db":"error"}),
        ] {
            lifecycle::require_credential_for_auth("http://127.0.0.1:8765", &value, &provider)
                .unwrap();
        }
    } else if mode == "cache" {
        let home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).unwrap();
        let cache =
            pseudolife_stdio::cache::HandshakeCache::from_environment("http://127.0.0.1:8765")
                .unwrap();
        assert!(
            cache
                .path()
                .starts_with(PathBuf::from(home).join(".pseudolife-mcp/handshake-cache"))
        );
        assert_eq!(
            cache.path().is_absolute(),
            PathBuf::from(
                std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).unwrap()
            )
            .is_absolute()
        );
    } else if mode == "runtime" {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let runtime = rt.block_on(Runtime::from_environment()).unwrap();
        // Match the production scheduling seam: emit and flush before updating.
        assert!(
            runtime
                .instructions_note
                .contains("update --clients-only --tag 99.0.0")
        );
        assert!(
            !PathBuf::from(
                std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).unwrap()
            )
            .join(".pseudolife-mcp/update-clients.99.0.0.attempt")
            .exists()
        );
        println!("first-frame:{}", runtime.instructions_note);
        std::io::stdout().flush().unwrap();
        println!(
            "runtime-note:{}",
            runtime.client_updates_after_first_frame()
        );
    } else {
        panic!("unknown fixture mode");
    }
}
fn child(home: &DisposableHome, mode: &str) -> Command {
    let mut cmd = Command::new(std::env::current_exe().unwrap());
    cmd.args(["--exact", "assertion_child", "--nocapture"])
        .env_clear()
        .env(
            "SystemRoot",
            std::env::var_os("SystemRoot").unwrap_or_default(),
        )
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0)
        .env("NONBOARD_ASSERTION_CHILD", mode)
        .env("PSEUDOLIFE_MCP_PYTHON", python());
    cmd
}
#[test]
fn test_startup_credential_check_is_silent_when_auth_is_off() {
    let home = DisposableHome::new();
    let out = child(&home, "auth").output().unwrap();
    assert!(out.status.success());
    assert!(
        out.stderr.is_empty(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
#[test]
fn test_the_next_session_reads_the_last_attempts_result() {
    let home = DisposableHome::new();
    let now = SystemTime::now();
    let mut count = 0;
    updates(&home).unattended_with("http://127.0.0.1:8765", &health(), now, |_, _| {
        count += 1;
        Ok(4242)
    });
    let result = home.path("state/update-clients.99.0.0.result");
    fs::write(&result, "2\n").unwrap();
    let failed = updates(&home).unattended_with("http://127.0.0.1:8765", &health(), now, |_, _| {
        panic!("repeat")
    });
    assert!(failed.contains("failed (exit 2") && failed.contains("--clients-only --tag 99.0.0"));
    fs::write(&result, "0\n").unwrap();
    let finished =
        updates(&home).unattended_with("http://127.0.0.1:8765", &health(), now, |_, _| {
            panic!("repeat")
        });
    assert!(finished.contains("finished") && finished.contains("Start a new session"));
    fs::remove_file(&result).unwrap();
    assert!(
        updates(&home)
            .unattended_with("http://127.0.0.1:8765", &health(), now, |_, _| panic!(
                "repeat"
            ))
            .contains("still running")
    );
    assert_eq!(count, 1);
}
#[test]
fn test_two_sessions_starting_together_spawn_one_run() {
    let home = DisposableHome::new();
    let mut calls = 0;
    let mut opens = 0;
    let note = updates(&home).unattended_with_attempt(
        "http://127.0.0.1:8765",
        &health(),
        SystemTime::now(),
        |_, _| {
            calls += 1;
            Ok(4242)
        },
        |options, path| {
            opens += 1;
            // The other session wins between metadata inspection and exclusive open.
            fs::write(path, "other\n")?;
            options.open(path)
        },
    );
    assert_eq!(opens, 1);
    assert_eq!(calls, 0);
    assert_eq!(
        note,
        format!(
            "Pseudolife-MCP: an unattended client update to 99.0.0 was just started by another session (log {}); this shim is 0.16.0. Start a new session when it finishes.",
            home.path("state").join("update-clients.log").display()
        )
    );
    assert!(!note.contains("--clients-only"));
    assert_eq!(
        fs::read_to_string(home.path("state/update-clients.99.0.0.attempt")).unwrap(),
        "other\n"
    );
}

fn runtime_update_health(socket: TcpListener, cancelled: mpsc::Receiver<()>) -> io::Result<()> {
    socket.set_nonblocking(true)?;
    let deadline = Instant::now() + Duration::from_secs(5);
    let (mut stream, _) = loop {
        match socket.accept() {
            Ok(connection) => break connection,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                match cancelled.recv_timeout(Duration::from_millis(5)) {
                    Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => {
                        return Err(io::Error::new(
                            io::ErrorKind::Interrupted,
                            "child exited before GET /health",
                        ));
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                }
                if Instant::now() >= deadline {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "child never issued GET /health",
                    ));
                }
            }
            Err(error) => return Err(error),
        }
    };
    stream.set_nonblocking(false)?;
    stream.set_read_timeout(Some(Duration::from_secs(3)))?;
    stream.set_write_timeout(Some(Duration::from_secs(3)))?;
    let mut bytes = vec![];
    let mut chunk = [0; 4096];
    let deadline = Instant::now() + Duration::from_secs(3);
    while !bytes.windows(4).any(|p| p == b"\r\n\r\n") {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() || bytes.len() >= 8192 {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "incomplete GET /health",
            ));
        }
        stream.set_read_timeout(Some(left))?;
        let n = stream.read(&mut chunk)?;
        if n == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "incomplete GET /health",
            ));
        }
        bytes.extend_from_slice(&chunk[..n]);
    }
    assert!(String::from_utf8_lossy(&bytes).starts_with("GET /health "));
    let body = health().to_string();
    write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

#[test]
fn runtime_update_fixture_cancels_when_child_exits_before_health() {
    let socket = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = socket.local_addr().unwrap();
    let (cancel, cancelled) = mpsc::channel();
    let (finished, completion) = mpsc::channel();
    let worker = std::thread::spawn(move || {
        finished
            .send(runtime_update_health(socket, cancelled))
            .unwrap();
    });
    cancel.send(()).unwrap();
    let result = completion.recv_timeout(Duration::from_secs(1));
    if result.is_err() {
        // Release the old blocking implementation before reporting the RED.
        let mut connection = std::net::TcpStream::connect(address).unwrap();
        connection
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        connection
            .write_all(b"GET /health HTTP/1.1\r\nHost: fixture\r\n\r\n")
            .unwrap();
        connection.read_to_end(&mut Vec::new()).unwrap();
        completion
            .recv_timeout(Duration::from_secs(3))
            .unwrap()
            .unwrap();
    }
    worker.join().unwrap();
    assert_eq!(
        result
            .expect("health listener ignored child cancellation")
            .unwrap_err()
            .kind(),
        io::ErrorKind::Interrupted
    );
}

fn runtime_update_fixture() -> (DisposableHome, Output, Value) {
    runtime_update_fixture_at(None)
}

#[test]
fn built_binary_launches_updater_only_after_first_flushed_frame() {
    let home = DisposableHome::new();
    let package = home.path("pseudolife_memory");
    fs::create_dir(&package).unwrap();
    fs::write(package.join("__init__.py"), "").unwrap();
    let sentinel = home.path("updater.json");
    fs::write(package.join("cli.py"), format!(
        "import json,pathlib,sys\npathlib.Path(sys.argv[sys.argv.index('--result-file')+1]).write_text('0\\n')\npathlib.Path({}).write_text(json.dumps(sys.argv[1:]))\n",
        serde_json::to_string(sentinel.to_str().unwrap()).unwrap()
    )).unwrap();
    let health_gate = std::sync::Arc::new(common::ResponseGate::default());
    let fixture = common::Fixture::start_with(common::Setup {
        health: vec![common::Reply::Gated(
            health_gate.clone(),
            Box::new(common::Reply::Http(200, health().to_string())),
        )],
        ..Default::default()
    });
    let interpreter = python();
    let mut shim = common::Shim::start_with_env(
        &fixture.url,
        &[
            ("PSEUDOLIFE_MCP_PYTHON", interpreter.to_str().unwrap()),
            ("PYTHONPATH", home.0.to_str().unwrap()),
        ],
    );
    let attempt = shim
        .state_dir()
        .join(".pseudolife-mcp/update-clients.99.0.0.attempt");
    let deadline = Instant::now() + Duration::from_secs(5);
    while !fixture
        .records
        .lock()
        .unwrap()
        .iter()
        .any(|record| record.verb == "GET" && record.path == "/health")
    {
        assert!(
            Instant::now() < deadline,
            "built binary did not request health"
        );
        assert!(!sentinel.exists(), "updater ran before health request");
        assert!(!attempt.exists(), "updater attempted before health request");
        std::thread::sleep(Duration::from_millis(5));
    }
    // The local startup probe times out at 250 ms; hold it for a bounded
    // interval below that deadline, checking the persistent markers throughout.
    assert_updater_absent(&sentinel, &attempt, Duration::from_millis(100));
    health_gate.release();
    let deadline = Instant::now() + Duration::from_secs(5);
    while !fixture
        .records
        .lock()
        .unwrap()
        .iter()
        .any(|record| record.message["method"] == "initialize")
    {
        assert!(
            Instant::now() < deadline,
            "built binary did not complete startup"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
    // Startup is observable at the upstream boundary; no client frame exists.
    assert_updater_absent(&sentinel, &attempt, Duration::from_millis(200));
    shim.send(json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"updater-frame-proof","version":"1"}}}));
    assert!(
        shim.receive()["result"]["instructions"]
            .as_str()
            .unwrap()
            .contains("99.0.0")
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    let argv: Vec<String> = loop {
        if let Ok(bytes) = fs::read(&sentinel)
            && let Ok(argv) = serde_json::from_slice(&bytes)
        {
            break argv;
        }
        assert!(
            Instant::now() < deadline,
            "first flushed frame did not trigger updater callback"
        );
        std::thread::sleep(Duration::from_millis(5));
    };
    assert_eq!(
        &argv[..5],
        [
            "update",
            "--clients-only",
            "--tag",
            "99.0.0",
            "--result-file"
        ]
    );
    assert_eq!(
        Path::new(&argv[5]),
        shim.state_dir()
            .join(".pseudolife-mcp/update-clients.99.0.0.result")
    );
    assert_eq!(
        fs::read_to_string(&argv[5]).unwrap(),
        if cfg!(windows) { "0\r\n" } else { "0\n" }
    );
    shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
    assert!(shim.finish().is_empty());
    assert!(
        shim.stderr()
            .contains("started the unattended client update")
    );
    fixture.assert_deleted();
}

fn assert_updater_absent(sentinel: &Path, attempt: &Path, delay: Duration) {
    let deadline = Instant::now() + delay;
    loop {
        assert!(!sentinel.exists(), "updater ran before first flushed frame");
        assert!(
            !attempt.exists(),
            "updater attempted before first flushed frame"
        );
        if Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
}
fn runtime_update_fixture_at(profile: Option<&str>) -> (DisposableHome, Output, Value) {
    let home = DisposableHome::new();
    let package = home.path("pseudolife_memory");
    fs::create_dir(&package).unwrap();
    fs::write(package.join("__init__.py"), "").unwrap();
    let report = home.path("spawn.json");
    let code = format!(
        "import json,sys,pathlib\nargs=sys.orig_argv\nr=pathlib.Path(sys.argv[sys.argv.index('--result-file')+1])\nr.write_text('0\\n')\npathlib.Path({}).write_text(json.dumps({{'executable':sys.executable,'argv':args,'pid':__import__('os').getpid()}}))\n",
        serde_json::to_string(report.to_str().unwrap()).unwrap()
    );
    fs::write(package.join("cli.py"), code).unwrap();
    let socket = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", socket.local_addr().unwrap());
    let (cancel, cancelled) = mpsc::channel();
    let endpoint = std::thread::spawn(move || runtime_update_health(socket, cancelled));
    let mut command = child(&home, "runtime");
    command
        .current_dir(&home.0)
        .env("PYTHONPATH", &home.0)
        .env("PSEUDOLIFE_MCP_DAEMON_URL", url)
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1");
    if let Some(profile) = profile {
        command.env("HOME", profile).env("USERPROFILE", profile);
    }
    let out = command.output();
    let _ = cancel.send(());
    let served = endpoint.join().unwrap();
    let out = out.unwrap();
    served.unwrap_or_else(|error| {
        panic!(
            "health fixture: {error}; child status: {}; child stderr: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr)
        )
    });
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let until = Instant::now() + Duration::from_secs(5);
    let value = loop {
        if let Ok(bytes) = fs::read(&report)
            && let Ok(v) = serde_json::from_slice(&bytes)
        {
            break v;
        }
        assert!(Instant::now() < until, "fake updater did not finish");
        std::thread::sleep(Duration::from_millis(10));
    };
    // The disposable module finishes after reporting. No installer is imported.
    assert_eq!(
        fs::read_to_string(
            profile
                .map_or_else(|| home.0.clone(), |p| home.path(p))
                .join(".pseudolife-mcp/update-clients.99.0.0.result")
        )
        .unwrap(),
        if cfg!(windows) { "0\r\n" } else { "0\n" }
    );
    (home, out, value)
}
#[test]
fn test_shim_runs_the_client_half_unattended_when_the_daemon_is_newer() {
    let (home, out, record) = runtime_update_fixture();
    let exe: PathBuf = record["executable"].as_str().unwrap().into();
    let expected = Command::new(python())
        .args([
            "-c",
            "import sys,json;print(json.dumps([sys.executable,sys.orig_argv[0]]))",
        ])
        .output()
        .unwrap();
    let expected: Vec<String> = serde_json::from_slice(&expected.stdout).unwrap();
    assert_eq!(exe, Path::new(&expected[0]));
    let argv = record["argv"].as_array().unwrap();
    assert_eq!(argv.len(), 9);
    // Windows venv's redirector records its base executable in orig_argv.
    assert_eq!(
        Path::new(argv[0].as_str().unwrap()),
        Path::new(&expected[1])
    );
    assert_eq!(
        &argv[1..8],
        &[
            json!("-m"),
            json!("pseudolife_memory.cli"),
            json!("update"),
            json!("--clients-only"),
            json!("--tag"),
            json!("99.0.0"),
            json!("--result-file")
        ]
    );
    assert_eq!(
        Path::new(argv[8].as_str().unwrap()),
        home.path(".pseudolife-mcp/update-clients.99.0.0.result")
    );
    assert!(home.path(".pseudolife-mcp/update-clients.log").is_file());
    let note = String::from_utf8(out.stdout).unwrap();
    assert!(note.contains("99.0.0") && note.contains("unattended"));
    assert!(String::from_utf8(out.stderr).unwrap().contains("[shim]"));
}
#[test]
fn test_shim_accept_health_gives_a_manual_remedy_before_the_postframe_update() {
    let (_, out, _) = runtime_update_fixture();
    let stderr = String::from_utf8(out.stderr).unwrap();
    assert!(stderr.contains("unattended") && stderr.contains("99.0.0"));
    assert!(stderr.contains("python ops/update_clients.py"));
    assert!(
        stderr.find("then start a new session.").unwrap()
            < stderr.find("started the unattended client update").unwrap()
    );
}

#[test]
fn test_proxy_serves_the_board_checkin_it_was_told_to() {
    use std::{
        net::TcpStream,
        sync::{
            Arc,
            atomic::{AtomicBool, AtomicUsize, Ordering},
        },
    };
    for (ready, channel) in [(false, false), (true, false), (false, true), (true, true)] {
        let backend = common::Fixture::start();
        let upstream = backend.url.strip_prefix("http://").unwrap().to_owned();
        let socket = TcpListener::bind("127.0.0.1:0").unwrap();
        socket.set_nonblocking(true).unwrap();
        let address = socket.local_addr().unwrap();
        let stopped = Arc::new(AtomicBool::new(false));
        let probes = Arc::new(AtomicUsize::new(0));
        let (stop, count) = (stopped.clone(), probes.clone());
        let worker = std::thread::spawn(move || {
            let mut handlers = vec![];
            while !stop.load(Ordering::SeqCst) {
                match socket.accept() {
                    Ok((mut stream, _)) => {
                        let upstream = upstream.clone();
                        let count = count.clone();
                        handlers.push(std::thread::spawn(move||{
                            stream.set_nonblocking(false).unwrap();stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
                            let mut bytes=vec![];let mut chunk=[0;4096];
                            loop {
                                let n=stream.read(&mut chunk).unwrap();if n==0{return}bytes.extend_from_slice(&chunk[..n]);
                                if let Some(split)=bytes.windows(4).position(|p|p==b"\r\n\r\n") {
                                    let headers=String::from_utf8_lossy(&bytes[..split]);let length=headers.lines().find_map(|l|l.to_ascii_lowercase().strip_prefix("content-length:").and_then(|v|v.trim().parse::<usize>().ok())).unwrap_or(0);
                                    if bytes.len()>=split+4+length{break}
                                }
                            }
                            if bytes.starts_with(b"GET /api/hook/coordination-start ") {
                                count.fetch_add(1,Ordering::SeqCst);
                                write!(stream,"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nready").unwrap();
                            }else if bytes.starts_with(b"POST /api/coordination/") {
                                let value=if bytes.starts_with(b"POST /api/coordination/context "){json!({"bank_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","principal":"fixture"})}else if bytes.starts_with(b"POST /api/coordination/register "){json!({"agent_id":"fixture-agent","credential":"fixture-agent-key"})}else{json!({"generation":1,"messages":[]})};
                                let body=value.to_string();write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
                            }else{
                                let mut target=TcpStream::connect(upstream).unwrap();target.set_read_timeout(Some(Duration::from_secs(5))).unwrap();target.write_all(&bytes).unwrap();
                                let mut response=vec![];target.read_to_end(&mut response).unwrap();stream.write_all(&response).unwrap();
                            }
                        }));
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(2))
                    }
                    Err(e) => panic!("disposable forwarder: {e}"),
                }
            }
            for handle in handlers {
                handle.join().unwrap();
            }
        });
        let url = format!("http://{address}");
        let value = if channel {
            channel_initialization(&url, ready)
        } else {
            let mut shim = common::Shim::start_with_env(
                &url,
                &[
                    ("PSEUDOLIFE_WRITER_ID", "codex"),
                    (
                        "PSEUDOLIFE_AGENT_COORDINATION",
                        if ready { "1" } else { "0" },
                    ),
                    ("PSEUDOLIFE_MCP_TOKEN", "fixture-only-token"),
                    ("PSEUDOLIFE_AGENT_WAKE", "0"),
                ],
            );
            shim.send(json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"instructions-proof","version":"1"}}}));
            let value = shim.receive();
            shim.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
            assert!(shim.finish().is_empty());
            value
        };
        let mut expected = if ready {
            format!(
                "Disposable hanging-call fixture. {}",
                pseudolife_stdio::board::policy::CHECKIN
            )
        } else {
            "Disposable hanging-call fixture.".into()
        };
        if channel {
            expected.push_str(lifecycle::CHANNEL_SAFETY);
        }
        assert_eq!(value["result"]["instructions"], expected);
        assert!(
            value["result"]["instructions"]
                .as_str()
                .unwrap()
                .starts_with("Disposable hanging-call fixture.")
        );
        assert_eq!(
            probes.load(Ordering::SeqCst),
            usize::from(ready && !channel)
        );
        backend.assert_deleted();
        stopped.store(true, Ordering::SeqCst);
        worker.join().unwrap();
    }
}

fn channel_initialization(url: &str, ready: bool) -> Value {
    use std::{
        io::{BufRead, BufReader},
        process::{Child, Stdio},
    };
    struct OwnedChild(Child);
    impl Drop for OwnedChild {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let home = DisposableHome::new();
    let mut child = OwnedChild(
        Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .arg("channel")
            .env("HOME", &home.0)
            .env("USERPROFILE", &home.0)
            .env("PSEUDOLIFE_AGENT_STATE_DIR", &home.0)
            .env("PSEUDOLIFE_MCP_DAEMON_URL", url)
            .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
            .env(
                "PSEUDOLIFE_AGENT_COORDINATION",
                if ready { "1" } else { "0" },
            )
            .env("PSEUDOLIFE_WRITER_ID", "fixture-client")
            .env("PSEUDOLIFE_MCP_TOKEN", "fixture-only-token")
            .env_remove("PSEUDOLIFE_MCP_TOKEN_FILE")
            .env("PSEUDOLIFE_AGENT_WAKE", "0")
            .env_remove("PSEUDOLIFE_MCP_SHARED_HOST")
            .env_remove("PSEUDOLIFE_AGENT_STATE")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let mut input = child.0.stdin.take().unwrap();
    let mut output = BufReader::new(child.0.stdout.take().unwrap());
    writeln!(input,"{}",json!({"jsonrpc":"2.0","id":"open","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"channel-instructions-proof","version":"1"}}})).unwrap();
    input.flush().unwrap();
    let mut frame = String::new();
    output.read_line(&mut frame).unwrap();
    let value = serde_json::from_str(&frame).unwrap();
    writeln!(
        input,
        "{}",
        json!({"jsonrpc":"2.0","method":"notifications/initialized"})
    )
    .unwrap();
    drop(input);
    let mut rest = String::new();
    output.read_to_string(&mut rest).unwrap();
    assert!(rest.is_empty());
    assert!(child.0.wait().unwrap().success());
    value
}

#[test]
fn cache_environment_home_matches_the_selected_profile() {
    let home = DisposableHome::new();
    let out = child(&home, "cache").output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}

#[test]
fn relative_profile_homes_keep_python_path_spelling() {
    let home = DisposableHome::new();
    let out = child(&home, "cache")
        .current_dir(&home.0)
        .env("HOME", "relative-profile")
        .env("USERPROFILE", "relative-profile")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let (_, out, record) = runtime_update_fixture_at(Some("relative-profile"));
    let result = Path::new("relative-profile")
        .join(".pseudolife-mcp")
        .join("update-clients.99.0.0.result");
    assert_eq!(Path::new(record["argv"][8].as_str().unwrap()), result);
    assert!(!result.is_absolute());
    assert!(
        String::from_utf8(out.stderr).unwrap().contains(
            &Path::new("relative-profile")
                .join(".pseudolife-mcp")
                .join("update-clients.log")
                .display()
                .to_string()
        )
    );
}
