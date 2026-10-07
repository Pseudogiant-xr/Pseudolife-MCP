#![forbid(unsafe_code)]
mod common;
use common::LeaseHome as Home;
use common::cli::native_text as text;
use std::{
    io::{Read, Write},
    net::TcpListener,
    thread,
    time::{Duration, Instant},
};

fn reply_once(raw: &'static [u8]) -> (String, thread::JoinHandle<String>) {
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let peer = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(45);
        let mut stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(Instant::now() < deadline, "coordination request missing");
                    thread::sleep(Duration::from_millis(10));
                }
                Err(error) => panic!("HTTP peer: {error}"),
            }
        };
        stream.set_nonblocking(false).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut received = Vec::new();
        let mut buffer = [0; 4096];
        let (body_start, length) = loop {
            let count = stream.read(&mut buffer).unwrap();
            assert!(count > 0);
            received.extend_from_slice(&buffer[..count]);
            if let Some(end) = received.windows(4).position(|v| v == b"\r\n\r\n") {
                let header = std::str::from_utf8(&received[..end]).unwrap();
                let length = header
                    .lines()
                    .find_map(|line| {
                        line.split_once(':')
                            .filter(|(name, _)| name.eq_ignore_ascii_case("content-length"))
                            .map(|(_, value)| value.trim().parse::<usize>().unwrap())
                    })
                    .unwrap_or(0);
                break (end + 4, length);
            }
        };
        while received.len() < body_start + length {
            let count = stream.read(&mut buffer).unwrap();
            assert!(count > 0);
            received.extend_from_slice(&buffer[..count]);
        }
        let header = String::from_utf8(received[..body_start].to_vec()).unwrap();
        write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", raw.len()).unwrap();
        stream.write_all(raw).unwrap();
        header
    });
    (url, peer)
}
#[test]
fn malformed_reply_refuses_before_effects_but_valid_forbidden_fields_keep_r2() {
    for (raw, diagnostic) in [
        (
            br#"{"agent_id":"bad\u007fvalue","credential":"\ud800"}"#.as_slice(),
            "HTTP_REPLY_NOT_UNDERSTOOD",
        ),
        (
            br#"{"agent_id":"bad\u007fvalue","credential":""}"#,
            "HTTP_FORBIDDEN_INPUT_REFUSED: invalid agent_id header",
        ),
        (
            br#"{"agent_id":"","credential":"bad\u007fvalue"}"#,
            "HTTP_FORBIDDEN_INPUT_REFUSED: invalid credential header",
        ),
    ] {
        let home = Home::board();
        let (url, peer) = reply_once(raw);
        let output = home
            .board_command(&url)
            .args([
                "lease",
                "run",
                "resource",
                "--timeout",
                "0",
                "--",
                "missing-reduced-child",
            ])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert_eq!(output.stderr, text(&format!("lease: {diagnostic}\n")));
        assert!(!home.0.join("lease-resource.lock").exists());
        assert_eq!(
            std::fs::read(home.0.join("instance.id")).unwrap(),
            b"0123456789ab\n"
        );
        assert!(
            peer.join()
                .unwrap()
                .starts_with("POST /api/coordination/register ")
        );
    }
}
#[test]
fn malformed_list_reply_refuses_without_reporting_local_free() {
    for action in ["check", "list"] {
        let home = Home::board();
        let (url, peer) = reply_once(br#"{"leases":[],"extra":NaN}"#);
        let output = home
            .board_command(&url)
            .args(["lease", action, "resource", "--json"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert_eq!(output.stderr, text("lease: HTTP_REPLY_NOT_UNDERSTOOD\n"));
        assert!(!home.0.join("lease-resource.lock").exists());
        assert_eq!(
            std::fs::read(home.0.join("instance.id")).unwrap(),
            b"0123456789ab\n"
        );
        assert!(
            peer.join()
                .unwrap()
                .starts_with("POST /api/coordination/leases ")
        );
    }
}
#[test]
fn operator_actions_have_exact_refusal_before_state() {
    for (arguments, diagnostic) in [
        (
            vec!["lease", "break", "resource"],
            "pseudolife-stdio: lease action 'break' is deferred in this candidate\n",
        ),
        (
            vec!["lease", "delegate", "resource", "fixture-agent"],
            "pseudolife-stdio: lease action 'delegate' is deferred in this candidate\n",
        ),
        (
            vec!["lease", "designate", "resource", "fixture-agent"],
            "pseudolife-mcp lease designate is deprecated: use pseudolife-mcp lease delegate\npseudolife-stdio: lease action 'delegate' is deferred in this candidate\n",
        ),
    ] {
        let home = Home::new();
        let output = home.call(&arguments);
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert_eq!(output.stderr, text(diagnostic));
        assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
    }
}

#[test]
fn opaque_lease_arguments_refuse_before_state() {
    #[cfg(unix)]
    let opaque = {
        use std::os::unix::ffi::OsStringExt;
        std::ffi::OsString::from_vec(vec![b'x', 0xff])
    };
    #[cfg(windows)]
    let opaque = {
        use std::os::windows::ffi::OsStringExt;
        std::ffi::OsString::from_wide(&[b'x' as u16, 0xd800])
    };
    for action in ["check", "list", "run"] {
        let home = Home::new();
        let output = home
            .command()
            .args(["lease", action])
            .arg(&opaque)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            text("lease: arguments must be valid Unicode\n")
        );
        assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
    }
}

#[test]
fn leading_zero_durations_use_numeric_bounds_without_an_interpreter_digit_cap() {
    let home = Home::new();
    let value = "0".repeat(4300) + "30";
    for option in ["--timeout", "--expect", "--ttl"] {
        let output = home.call(&[
            "lease",
            "run",
            "resource",
            "--no-board",
            option,
            &value,
            "--",
            "missing-reduced-child",
        ]);
        assert_eq!(output.status.code(), Some(127));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            text(
                "lease: board skipped: --no-board was given; waiting on the local lock for 'resource' alone (not FIFO)\nlease: command not found: missing-reduced-child\n"
            )
        );
        assert!(home.0.join("lease-resource.lock").exists());
    }
}
