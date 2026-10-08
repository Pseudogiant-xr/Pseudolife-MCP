#![forbid(unsafe_code)]
mod common;
use common::LeaseHome as Home;
use std::{
    io::{Read, Write},
    net::TcpListener,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

struct Owned(Child);
impl Drop for Owned {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
#[test]
fn fixture_followed_process() {
    if std::env::var_os("LEASE_HOLD_FIXTURE").is_some() {
        let mut input = Vec::new();
        std::io::stdin().read_to_end(&mut input).unwrap();
    }
}
fn target() -> Owned {
    Owned(
        Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "fixture_followed_process", "--nocapture"])
            .env("LEASE_HOLD_FIXTURE", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    )
}
fn hold(home: &Home, target: &Owned, board: Option<&str>) -> Owned {
    let mut command = board.map_or_else(|| home.command(), |url| home.board_command(url));
    command.args([
        "lease",
        "hold",
        "resource",
        "--while-pid",
        &target.0.id().to_string(),
    ]);
    if board.is_none() {
        command.arg("--no-board");
    }
    Owned(
        command
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    )
}
fn assert_held(home: &Home, holder: &mut Owned) {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        assert!(
            holder.0.try_wait().unwrap().is_none(),
            "hold exited before target"
        );
        if home.0.join("lease-resource.lock").exists()
            && home
                .call(&["lease", "check", "resource", "--json"])
                .status
                .code()
                == Some(1)
        {
            return;
        }
        assert!(Instant::now() < deadline, "local hold missing");
        thread::sleep(Duration::from_millis(10));
    }
}
fn finish(mut target: Owned, mut holder: Owned) -> std::process::Output {
    drop(target.0.stdin.take());
    assert!(target.0.wait().unwrap().success());
    let deadline = Instant::now() + Duration::from_secs(25);
    while holder.0.try_wait().unwrap().is_none() {
        assert!(
            Instant::now() < deadline,
            "hold did not release after target exit"
        );
        thread::sleep(Duration::from_millis(10));
    }
    let status = holder.0.wait().unwrap();
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    holder
        .0
        .stdout
        .take()
        .unwrap()
        .read_to_end(&mut stdout)
        .unwrap();
    holder
        .0
        .stderr
        .take()
        .unwrap()
        .read_to_end(&mut stderr)
        .unwrap();
    std::process::Output {
        status,
        stdout,
        stderr,
    }
}
fn native(text: &str) -> Vec<u8> {
    text.replace('\n', if cfg!(windows) { "\r\n" } else { "\n" })
        .into_bytes()
}

#[test]
fn dead_pid_does_not_create_lock_stamp_or_contact_board() {
    let home = Home::new();
    let pid = home.completed_pid();
    let output = home.call(&["lease", "hold", "resource", "--while-pid", &pid.to_string()]);
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        native(&format!(
            "lease: pid {pid} is already gone; nothing to hold 'resource' for\n"
        ))
    );
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}
#[cfg(windows)]
#[test]
fn windows_max_pid_is_a_valid_dead_pid() {
    let home = Home::new();
    let output = home.call(&["lease", "hold", "resource", "--while-pid", "4294967295"]);
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        native("lease: pid 4294967295 is already gone; nothing to hold 'resource' for\n")
    );
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}
#[cfg(unix)]
#[test]
fn posix_out_of_range_pid_refuses_before_lock_or_board_effects() {
    let home = Home::new();
    let output = home.call(&["lease", "hold", "resource", "--while-pid", "4294967295"]);
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let usage = include_str!("../src/cli/lease/assets/hold_usage.txt");
    assert_eq!(
        output.stderr,
        native(&format!(
            "{usage}pseudolife-mcp lease hold: error: argument --while-pid: not a process id: '4294967295'\n"
        ))
    );
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}
#[test]
fn local_hold_follows_exit_and_releases_without_stopping_target() {
    let home = Home::new();
    let mut followed = target();
    let mut holder = hold(&home, &followed, None);
    assert_held(&home, &mut holder);
    assert!(followed.0.try_wait().unwrap().is_none());
    let output = finish(followed, holder);
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        native(
            "lease: board skipped: --no-board was given; 'resource' is held by the local lock alone\n"
        )
    );
    assert_eq!(
        home.call(&["lease", "check", "resource", "--json"])
            .status
            .code(),
        Some(0)
    );
}
#[test]
fn contended_timeout_does_not_stop_followed_process() {
    let home = Home::new();
    let mut followed = target();
    let mut holder = hold(&home, &followed, None);
    assert_held(&home, &mut holder);
    let output = home.call(&[
        "lease",
        "hold",
        "resource",
        "--while-pid",
        &followed.0.id().to_string(),
        "--no-board",
        "--timeout",
        "0",
    ]);
    assert_eq!(output.status.code(), Some(75));
    assert!(output.stdout.is_empty());
    assert!(
        String::from_utf8(output.stderr)
            .unwrap()
            .contains("after 0s (--timeout); nothing is held")
    );
    assert!(followed.0.try_wait().unwrap().is_none());
    assert_held(&home, &mut holder);
    assert_eq!(finish(followed, holder).status.code(), Some(0));
}
#[test]
fn forbidden_bearer_stops_mirror_after_acquisition_and_keeps_local_hold() {
    for (token, reason) in [
        (
            "bad\tvalue",
            "the bearer credential is unusable (credential file contains an invalid bearer token)",
        ),
        ("bad\x7fvalue", "the board header is not understood"),
    ] {
        let home = Home::new();
        let mut followed = target();
        let mut holder = Owned(
            home.board_command("http://127.0.0.1:1")
                .env("PSEUDOLIFE_MCP_TOKEN", token)
                .args([
                    "lease",
                    "hold",
                    "resource",
                    "--while-pid",
                    &followed.0.id().to_string(),
                ])
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap(),
        );
        assert_held(&home, &mut holder);
        assert!(followed.0.try_wait().unwrap().is_none());
        let output = finish(followed, holder);
        assert!(home.0.join("instance.id").exists());
        assert_eq!(output.status.code(), Some(0));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            native(&format!(
                "lease: board skipped: {reason}; 'resource' is held by the local lock alone\n"
            ))
        );
        assert_eq!(
            home.call(&["lease", "check", "resource", "--json"])
                .status
                .code(),
            Some(0)
        );
    }
}
fn registration_peer(raw: &'static [u8]) -> (String, thread::JoinHandle<Vec<u8>>) {
    let socket = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
    socket.set_nonblocking(true).unwrap();
    let url = format!("http://{}", socket.local_addr().unwrap());
    let task = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut stream = loop {
            match socket.accept() {
                Ok((stream, _)) => break stream,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(Instant::now() < deadline);
                    thread::sleep(Duration::from_millis(10));
                }
                Err(e) => panic!("fixture accept: {e}"),
            }
        };
        stream.set_nonblocking(false).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut received = Vec::new();
        let mut chunk = [0; 4096];
        let (body_start, length) = loop {
            let count = stream.read(&mut chunk).unwrap();
            assert!(count > 0);
            received.extend_from_slice(&chunk[..count]);
            if let Some(end) = received.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
                let header = std::str::from_utf8(&received[..end]).unwrap();
                let length = header
                    .lines()
                    .find_map(|line| {
                        line.split_once(':')
                            .filter(|(key, _)| key.eq_ignore_ascii_case("content-length"))
                            .map(|(_, value)| value.trim().parse::<usize>().unwrap())
                    })
                    .unwrap();
                break (end + 4, length);
            }
        };
        while received.len() < body_start + length {
            let count = stream.read(&mut chunk).unwrap();
            assert!(count > 0);
            received.extend_from_slice(&chunk[..count]);
        }
        write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",raw.len()).unwrap();
        stream.write_all(raw).unwrap();
        received
    });
    (url, task)
}
#[test]
fn registration_admission_failures_stop_mirror_but_keep_local_hold() {
    for (reply, reason) in [
        (
            br#"{"agent_id":"bad\u007fvalue","credential":"\ud800"}"#.as_slice(),
            "HTTP_REPLY_NOT_UNDERSTOOD",
        ),
        (
            br#"{"agent_id":"bad\u007fvalue","credential":""}"#,
            "HTTP_FORBIDDEN_INPUT_REFUSED: invalid agent_id header",
        ),
        (
            br#"{"agent_id":"fixture-agent","credential":"\u00e9"}"#,
            "registration headers are not understood",
        ),
    ] {
        let home = Home::new();
        let mut followed = target();
        let (url, peer) = registration_peer(reply);
        let mut holder = hold(&home, &followed, Some(&url));
        let received = peer.join().unwrap();
        assert!(received.starts_with(b"POST /api/coordination/register "));
        let body = received
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .unwrap()
            + 4;
        let body: serde_json::Value = serde_json::from_slice(&received[body..]).unwrap();
        assert!(body["label"].as_str().unwrap().starts_with("lease-hold@"));
        assert_eq!(body["task"], format!("resource: pid {}", followed.0.id()));
        assert_held(&home, &mut holder);
        assert!(followed.0.try_wait().unwrap().is_none());
        let output = finish(followed, holder);
        assert_eq!(output.status.code(), Some(0));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            native(&format!(
                "lease: board skipped: {reason}; 'resource' is held by the local lock alone\n"
            ))
        );
        assert_eq!(
            home.call(&["lease", "check", "resource", "--json"])
                .status
                .code(),
            Some(0)
        );
    }
}
#[cfg(unix)]
#[test]
fn term_releases_holder_without_signalling_followed_process() {
    let home = Home::new();
    let mut followed = target();
    let mut holder = hold(&home, &followed, None);
    assert_held(&home, &mut holder);
    let pid = rustix::process::Pid::from_raw(holder.0.id() as i32).unwrap();
    rustix::process::kill_process(pid, rustix::process::Signal::TERM).unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while holder.0.try_wait().unwrap().is_none() {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(holder.0.wait().unwrap().code(), Some(143));
    assert!(followed.0.try_wait().unwrap().is_none());
    let mut stderr = String::new();
    holder
        .0
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap();
    assert!(stderr.contains(&format!(
        "lease: stopped by SIGTERM; releasing 'resource' (pid {} is left running)",
        followed.0.id()
    )));
    assert_eq!(
        home.call(&["lease", "check", "resource", "--json"])
            .status
            .code(),
        Some(0)
    );
}
