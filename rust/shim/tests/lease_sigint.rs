#![cfg(unix)]
#![deny(unsafe_code)]

mod common;
use common::LeaseHome as Home;
use rustix::process::{Pid, Signal, kill_process, kill_process_group, test_kill_process};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    os::unix::process::CommandExt,
    process::{Child, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

// Change only the forked fixture's disposition, never the test runner's.
#[allow(unsafe_code)]
mod disposition {
    use std::{io, os::unix::process::CommandExt, process::Command};

    pub fn set(command: &mut Command, ignored: bool) {
        // SAFETY: the child callback uses only initialized stack storage and
        // async-signal-safe sigaction before exec. No allocation or locks.
        unsafe {
            command.pre_exec(move || {
                let mut action: libc::sigaction = std::mem::zeroed();
                action.sa_sigaction = if ignored {
                    libc::SIG_IGN
                } else {
                    libc::SIG_DFL
                };
                if libc::sigaction(libc::SIGINT, &action, std::ptr::null_mut()) != 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
}

struct Board {
    url: String,
    stop: Arc<AtomicBool>,
    peer: Option<thread::JoinHandle<Vec<String>>>,
}
impl Board {
    fn new() -> Self {
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let peer = thread::spawn(move || {
            let mut routes = Vec::new();
            while !stopped.load(Ordering::SeqCst) {
                let (mut stream, _) = match listener.accept() {
                    Ok(stream) => stream,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    Err(e) => panic!("fixture accept: {e}"),
                };
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                stream
                    .set_write_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut reader = BufReader::new(&mut stream);
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let route = line.split_whitespace().nth(1).unwrap().to_owned();
                let mut length = 0;
                loop {
                    line.clear();
                    assert!(reader.read_line(&mut line).unwrap() > 0);
                    if line == "\r\n" {
                        break;
                    }
                    if let Some((key, value)) = line.split_once(':')
                        && key.eq_ignore_ascii_case("content-length")
                    {
                        length = value.trim().parse::<usize>().unwrap();
                    }
                }
                let mut body = vec![0; length];
                reader.read_exact(&mut body).unwrap();
                let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
                let reply = match route.as_str() {
                    "/api/coordination/register" => {
                        r#"{"agent_id":"fixture-agent","credential":"fixture-key"}"#
                    }
                    "/api/coordination/lease" => {
                        assert_eq!(body["name"], "resource");
                        r#"{"state":"held"}"#
                    }
                    "/api/coordination/release" => {
                        assert_eq!(body["name"], "resource");
                        "{}"
                    }
                    _ => panic!("unexpected fixture route: {route}"),
                };
                write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{reply}", reply.len()).unwrap();
                routes.push(route);
            }
            routes
        });
        Self {
            url,
            stop,
            peer: Some(peer),
        }
    }
    fn finish(&mut self) -> Vec<String> {
        self.stop.store(true, Ordering::SeqCst);
        self.peer.take().unwrap().join().unwrap()
    }
}
impl Drop for Board {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(peer) = self.peer.take() {
            let _ = peer.join();
        }
    }
}

struct Owned(Option<Child>);
impl Drop for Owned {
    fn drop(&mut self) {
        if let Some(child) = &mut self.0 {
            // A failed lease may have exited while its fixture child remains.
            let _ = kill_process_group(Pid::from_raw(child.id() as i32).unwrap(), Signal::KILL);
            let _ = child.wait();
        }
    }
}

#[test]
fn fixture_child_completes() {
    let Some(home) = std::env::var_os("LEASE_SIGINT_CHILD_HOME") else {
        return;
    };
    let home = std::path::PathBuf::from(home);
    fs::write(home.join("ready"), std::process::id().to_string()).unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !home.join("finish").exists() {
        assert!(Instant::now() < deadline, "fixture child was not released");
        thread::sleep(Duration::from_millis(5));
    }
    fs::write(home.join("completed"), b"child-cleaned-up").unwrap();
}

fn run(ignored: bool) {
    let home = Home::board();
    let mut board = Board::new();
    let mut command = home.board_command(&board.url);
    command
        .args(["lease", "run", "resource", "--"])
        .arg(std::env::current_exe().unwrap())
        .args(["--exact", "fixture_child_completes", "--nocapture"])
        .env("LEASE_SIGINT_CHILD_HOME", &home.0)
        .process_group(0)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    disposition::set(&mut command, ignored);
    let mut owned = Owned(Some(command.spawn().unwrap()));
    let lease = owned.0.as_mut().unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    while !home.0.join("ready").exists() {
        assert!(
            Instant::now() < deadline,
            "actual binary never started its child"
        );
        assert!(
            lease.try_wait().unwrap().is_none(),
            "lease exited before child readiness"
        );
        thread::sleep(Duration::from_millis(5));
    }
    let child_pid = Pid::from_raw(
        fs::read_to_string(home.0.join("ready"))
            .unwrap()
            .parse()
            .unwrap(),
    )
    .unwrap();
    let lock = fs::File::open(home.0.join("lease-resource.lock")).unwrap();
    assert!(
        lock.try_lock().is_err(),
        "child runs under a real held lock"
    );
    kill_process(Pid::from_raw(lease.id() as i32).unwrap(), Signal::INT).unwrap();
    // Keep the child waiting while the normal-disposition arm handles SIGINT.
    thread::sleep(Duration::from_millis(100));
    assert!(lease.try_wait().unwrap().is_none());
    fs::write(home.0.join("finish"), b"finish").unwrap();
    while lease.try_wait().unwrap().is_none() {
        assert!(
            Instant::now() < deadline,
            "lease did not finish child cleanup"
        );
        thread::sleep(Duration::from_millis(5));
    }
    let output = owned.0.take().unwrap().wait_with_output().unwrap();
    assert_eq!(output.status.code(), Some(if ignored { 0 } else { 130 }));
    assert_eq!(
        output.stderr,
        if ignored {
            b"".as_slice()
        } else {
            b"lease: interrupted; releasing 'resource'\n".as_slice()
        }
    );
    assert_eq!(
        fs::read(home.0.join("completed")).unwrap(),
        b"child-cleaned-up"
    );
    assert_eq!(test_kill_process(child_pid), Err(rustix::io::Errno::SRCH));
    assert!(
        lock.try_lock().is_ok(),
        "lease releases the lock after child exit"
    );
    assert_eq!(
        board.finish(),
        [
            "/api/coordination/register",
            "/api/coordination/lease",
            "/api/coordination/release"
        ]
    );
}

#[test]
fn inherited_ignored_sigint_keeps_child_completion_exit_zero_and_empty_stderr() {
    run(true);
}

#[test]
fn default_sigint_keeps_exit_130_and_child_cleanup() {
    run(false);
}
