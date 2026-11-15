//! The daemon process really bound on 0.0.0.0 (unix only), answering every
//! request admission case of `security_admission/cases.rs` over a raw
//! socket: the open configuration (`PSEUDOLIFE_MCP_TRUST_BIND`) and the
//! closed one (bearer), each with an unreachable bank, as the harness's
//! `security-remote-open` and `security-remote-auth` start it.
//!
//! Unix only because a wildcard listener on a Windows desktop can raise a
//! firewall prompt; the same table runs on every OS in process
//! (`src/http_security_tests.rs`), and the startup decision for both
//! configurations is `tests/security_startup.rs`'s.

#[path = "security_admission/cases.rs"]
mod cases;

#[cfg(unix)]
mod wildcard {
    use super::cases::{CLOSED, CLOSED_ENV, Case, OPEN, OPEN_ENV};
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpStream;
    use std::path::PathBuf;
    use std::process::{Child, Command, Stdio};
    use std::sync::mpsc;
    use std::time::{Duration, Instant};

    struct Daemon {
        child: Child,
        port: u16,
        home: PathBuf,
    }

    impl Drop for Daemon {
        fn drop(&mut self) {
            let _ = self.child.kill();
            let _ = self.child.wait();
            let _ = std::fs::remove_dir_all(&self.home);
        }
    }

    fn free_port() -> u16 {
        std::net::TcpListener::bind("0.0.0.0:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
    }

    /// Start the daemon on 0.0.0.0 with `vars` and nothing of the caller's
    /// environment; retry on another port if the chosen one was taken.
    fn start(name: &str, vars: &[(&str, &str)]) -> Daemon {
        for _ in 0..3 {
            let nanos = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let home = std::env::temp_dir().join(format!(
                "pl-security-admission-{name}-{}-{nanos}",
                std::process::id()
            ));
            std::fs::create_dir_all(home.join("data")).unwrap();
            let port = free_port();
            let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-daemon"));
            command
                .env_clear()
                .current_dir(&home)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::piped());
            for key in ["PATH", "TMPDIR"] {
                if let Some(value) = std::env::var_os(key) {
                    command.env(key, value);
                }
            }
            command
                .env("HOME", &home)
                .env("XDG_CONFIG_HOME", &home)
                .env("XDG_DATA_HOME", &home)
                .env("XDG_CACHE_HOME", &home)
                .env("PSEUDOLIFE_MCP_DATA_DIR", home.join("data"))
                .env(
                    "PSEUDOLIFE_MCP_DATABASE_URL",
                    "postgresql://nobody:nothing@127.0.0.1:9/pl_security_admission_none",
                )
                .env("PSEUDOLIFE_RELEASE_CHECK", "0")
                .env("PSEUDOLIFE_MCP_PORT", port.to_string());
            for (key, value) in vars {
                command.env(key, value);
            }
            let mut child = command.spawn().expect("spawn the daemon");
            let stderr = child.stderr.take().unwrap();
            let (lines, seen) = mpsc::channel();
            std::thread::spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    if lines.send(line).is_err() {
                        break;
                    }
                }
            });
            let mut daemon = Daemon { child, port, home };
            let deadline = Instant::now() + Duration::from_secs(120);
            let mut log = Vec::new();
            let listening = format!("daemon: listening on 0.0.0.0:{port} (auth=");
            loop {
                match seen.recv_timeout(Duration::from_millis(100)) {
                    Ok(line) => {
                        let ready = line.starts_with(&listening);
                        log.push(line);
                        if ready {
                            // Keep draining stderr so the daemon never blocks on it.
                            std::thread::spawn(move || for _ in seen {});
                            return daemon;
                        }
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                }
                if let Ok(Some(_)) = daemon.child.try_wait() {
                    break;
                }
                assert!(Instant::now() < deadline, "{name}: not listening after 120 s: {log:#?}");
            }
            let taken = log.iter().any(|l| l.starts_with(&format!("bind 0.0.0.0:{port}:")));
            assert!(taken, "{name}: the daemon exited before listening: {log:#?}");
            drop(daemon);
        }
        panic!("{name}: no free port after three tries");
    }

    /// One case as the harness's client sends it, on its own connection.
    fn send(port: u16, case: &Case) -> (u16, Vec<(String, String)>, Vec<u8>) {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(30))).unwrap();
        let mut head = format!("{} {} HTTP/1.1\r\n", case.method, case.path).into_bytes();
        if !case.headers.iter().any(|(name, _)| name.eq_ignore_ascii_case("host")) {
            head.extend_from_slice(format!("Host: 127.0.0.1:{port}\r\n").as_bytes());
        }
        for (name, value) in case.headers {
            head.extend_from_slice(format!("{name}: ").as_bytes());
            head.extend_from_slice(value);
            head.extend_from_slice(b"\r\n");
        }
        let body = case.body.bytes();
        if let Some(bytes) = &body {
            head.extend_from_slice(format!("Content-Length: {}\r\n", bytes.len()).as_bytes());
        }
        head.extend_from_slice(b"\r\n");
        stream.write_all(&head).unwrap();
        if let Some(bytes) = &body {
            // A refusal answers before the body is read and discards the
            // rest; a write the daemon stopped reading is not a failure here.
            let _ = stream.write_all(bytes);
        }
        let mut reader = BufReader::new(stream);
        let mut status_line = String::new();
        reader.read_line(&mut status_line).unwrap();
        let status: u16 = status_line
            .split(' ')
            .nth(1)
            .and_then(|s| s.parse().ok())
            .unwrap_or_else(|| panic!("{}: no status in {status_line:?}", case.name));
        let mut headers = Vec::new();
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let line = line.trim_end_matches(['\r', '\n']);
            if line.is_empty() {
                break;
            }
            let (name, value) = line.split_once(':').unwrap();
            headers.push((name.to_ascii_lowercase(), value.trim().to_string()));
        }
        let length: usize = headers
            .iter()
            .find(|(n, _)| n == "content-length")
            .map(|(_, v)| v.parse().unwrap())
            .unwrap_or_else(|| panic!("{}: no Content-Length in {headers:?}", case.name));
        let mut body = vec![0; length];
        reader.read_exact(&mut body).unwrap();
        (status, headers, body)
    }

    fn run_table(name: &str, vars: &[(&str, &str)], table: &[Case]) {
        let daemon = start(name, vars);
        let mut failures = Vec::new();
        for case in table {
            let (status, headers, body) = send(daemon.port, case);
            let e = &case.expect;
            let header = |n: &str| headers.iter().find(|(k, _)| k == n).map(|(_, v)| v.as_str());
            let mut wrong = Vec::new();
            if status != e.status {
                wrong.push(format!("status {status}, want {}", e.status));
            }
            if body != e.body {
                wrong.push(format!("body {:?}", String::from_utf8_lossy(&body)));
            }
            if header("content-type") != Some(e.content_type) {
                wrong.push(format!("content-type {:?}", header("content-type")));
            }
            if header("cache-control") != e.cache_control {
                wrong.push(format!("cache-control {:?}", header("cache-control")));
            }
            if (header("x-content-type-options") == Some("nosniff")) != e.nosniff {
                wrong.push(format!("x-content-type-options {:?}", header("x-content-type-options")));
            }
            failures.extend(wrong.into_iter().map(|w| format!("{} ({}): {w}", case.name, e.kind)));
        }
        assert!(failures.is_empty(), "{name}:\n{}", failures.join("\n"));
    }

    #[test]
    fn a_trusted_wildcard_daemon_still_admits_loopback_browsers_only() {
        run_table("open", OPEN_ENV, OPEN);
    }

    #[test]
    fn a_wildcard_daemon_with_a_bearer_still_gates_every_path() {
        run_table("closed", CLOSED_ENV, CLOSED);
    }
}
