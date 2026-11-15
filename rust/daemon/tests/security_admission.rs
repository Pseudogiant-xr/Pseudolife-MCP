//! The daemon process answering every request admission case of
//! `security_admission/cases.rs` over a raw socket, Linux only, each
//! configuration with an unreachable bank: the closed one (bearer) really
//! bound on 0.0.0.0, as `security-remote-auth` configures it, and the open
//! one (`PSEUDOLIFE_MCP_TRUST_BIND`) on 127.0.0.1.
//!
//! The open configuration never gets a wildcard listener: tokenless, it
//! would answer any LAN client that names a loopback Host, so the harness
//! too refuses to start one (`run.py` `loopback_bind_fixture`). Its
//! configured-0.0.0.0 answers are the in-process test's
//! (`src/http_security_tests.rs`, every OS), and whether either
//! configuration starts off loopback is `tests/security_startup.rs`'s. Not
//! on macOS or Windows, where a wildcard listener can raise a desktop
//! firewall prompt.

#[path = "security_admission/cases.rs"]
mod cases;

#[cfg(target_os = "linux")]
mod process {
    use super::cases::{CLOSED, CLOSED_ENV, Case, OPEN, OPEN_ENV, mismatches};
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpStream;
    use std::os::unix::process::CommandExt;
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

    fn free_port(host: &str) -> u16 {
        std::net::TcpListener::bind((host, 0))
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
    }

    /// Start the daemon with `vars` (which name its host) and nothing of the
    /// caller's environment; retry on another port if the chosen one was
    /// taken. `auth` is the mode its listening line must report.
    fn start(name: &str, vars: &[(&str, &str)], auth: bool) -> Daemon {
        let host = vars
            .iter()
            .find(|(k, _)| *k == "PSEUDOLIFE_MCP_HOST")
            .map_or("127.0.0.1", |(_, v)| *v);
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
            let port = free_port(host);
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
            // SAFETY: prctl is async-signal-safe; it only asks the kernel to
            // kill the daemon if the test thread that started it dies, so a
            // killed test run leaves no listener behind.
            unsafe {
                command.pre_exec(|| {
                    libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
                    Ok(())
                });
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
            let listening = format!("daemon: listening on {host}:{port} (auth={auth})");
            let deadline = Instant::now() + Duration::from_secs(50);
            let mut log = Vec::new();
            loop {
                match seen.recv_timeout(Duration::from_millis(100)) {
                    Ok(line) => {
                        let ready = line == listening;
                        log.push(line);
                        if ready {
                            // Keep draining stderr so the daemon never blocks on it.
                            std::thread::spawn(move || for _ in seen {});
                            return daemon;
                        }
                        continue;
                    }
                    // The reader ends when the daemon's stderr closes.
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                }
                assert!(
                    Instant::now() < deadline,
                    "{name}: not listening after 50 s: {log:#?}"
                );
            }
            let _ = daemon.child.wait();
            let taken = log
                .iter()
                .any(|l| l.starts_with(&format!("bind {host}:{port}:")));
            assert!(
                taken,
                "{name}: the daemon exited before listening: {log:#?}"
            );
            drop(daemon);
        }
        panic!("{name}: no free port after three tries");
    }

    /// One case as the harness's client sends it, on its own connection.
    fn send(port: u16, case: &Case) -> (u16, Vec<(String, String)>, Vec<u8>) {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(30)))
            .unwrap();
        stream
            .set_write_timeout(Some(Duration::from_secs(30)))
            .unwrap();
        let mut head = format!("{} {} HTTP/1.1\r\n", case.method, case.path).into_bytes();
        if !case
            .headers
            .iter()
            .any(|(name, _)| name.eq_ignore_ascii_case("host"))
        {
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

    fn run_table(name: &str, vars: &[(&str, &str)], auth: bool, table: &[Case]) {
        let daemon = start(name, vars, auth);
        let mut failures = Vec::new();
        for case in table {
            let (status, headers, body) = send(daemon.port, case);
            failures.extend(mismatches(case, status, &headers, &body));
        }
        assert!(failures.is_empty(), "{name}:\n{}", failures.join("\n"));
    }

    #[test]
    fn a_trusted_tokenless_daemon_admits_loopback_browsers_only() {
        let loopback: Vec<(&str, &str)> = OPEN_ENV
            .iter()
            .map(|&(k, v)| {
                if k == "PSEUDOLIFE_MCP_HOST" {
                    (k, "127.0.0.1")
                } else {
                    (k, v)
                }
            })
            .collect();
        run_table("open", &loopback, false, OPEN);
    }

    #[test]
    fn a_wildcard_daemon_with_a_bearer_still_gates_every_path() {
        run_table("closed", CLOSED_ENV, true, CLOSED);
    }
}
