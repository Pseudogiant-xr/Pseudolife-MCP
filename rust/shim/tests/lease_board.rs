#![forbid(unsafe_code)]
mod common;
use common::LeaseHome as Home;
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    thread,
    time::{Duration, Instant},
};

/// A bounded HTTP peer observes the actual public binary, including headers.
fn server<F>(count: usize, mut answer: F) -> (String, thread::JoinHandle<Vec<(String, Value)>>)
where
    F: FnMut(usize, &str, &Value) -> (u16, Value) + Send + 'static,
{
    server_raw(count, move |index, header, body| {
        let (status, reply) = answer(index, header, body);
        (status, serde_json::to_vec(&reply).unwrap())
    })
}
fn server_raw<F>(count: usize, mut answer: F) -> (String, thread::JoinHandle<Vec<(String, Value)>>)
where
    F: FnMut(usize, &str, &Value) -> (u16, Vec<u8>) + Send + 'static,
{
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let task = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(45);
        let mut requests = vec![];
        for index in 0..count {
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(v) => break v,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(
                            Instant::now() < deadline,
                            "HTTP peer did not receive request {index}"
                        );
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(e) => panic!("HTTP peer: {e}"),
                }
            };
            stream.set_nonblocking(false).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            stream
                .set_write_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut bytes = vec![];
            let mut buffer = [0u8; 4096];
            let (end, length) = loop {
                let n = stream.read(&mut buffer).unwrap();
                assert!(n > 0, "HTTP headers ended early");
                bytes.extend_from_slice(&buffer[..n]);
                assert!(bytes.len() < 65536);
                if let Some(end) = bytes.windows(4).position(|v| v == b"\r\n\r\n") {
                    let header = std::str::from_utf8(&bytes[..end]).unwrap();
                    let length = header
                        .lines()
                        .find_map(|l| {
                            l.split_once(':')
                                .filter(|(k, _)| k.eq_ignore_ascii_case("content-length"))
                                .map(|(_, v)| v.trim().parse::<usize>().unwrap())
                        })
                        .unwrap_or(0);
                    break (end + 4, length);
                }
            };
            while bytes.len() < end + length {
                let n = stream.read(&mut buffer).unwrap();
                assert!(n > 0);
                bytes.extend_from_slice(&buffer[..n]);
                assert!(bytes.len() < 65536);
            }
            let header = std::str::from_utf8(&bytes[..end]).unwrap().to_owned();
            let body: Value = serde_json::from_slice(&bytes[end..end + length]).unwrap();
            let (status, raw) = answer(index, &header, &body);
            write!(stream,"HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",raw.len()).unwrap();
            stream.write_all(&raw).unwrap();
            requests.push((header, body));
        }
        requests
    });
    (url, task)
}
fn registered() -> Value {
    json!({"agent_id":"fixture-agent","credential":"fixture-key"})
}
fn has_header(header: &str, key: &str, value: &str) -> bool {
    header.lines().any(|line| {
        line.split_once(':')
            .is_some_and(|(k, v)| k.eq_ignore_ascii_case(key) && v.trim() == value)
    })
}

#[test]
fn unsupported_clock_dates_render_unknown_and_preserve_json() {
    for waiter in [false, true] {
        for json_output in [false, true] {
            let home = Home::board();
            let (url, peer) = server(1, move |_, header, body| {
                assert!(header.starts_with("POST /api/coordination/leases "));
                assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
                assert_eq!(body, &json!({"name":"resource"}));
                (
                    200,
                    json!({"leases":[{"name":"resource","holder":{"label":"holder"},
                    "expected_end":if waiter { Value::Null } else { json!(67767976233532800.0) },
                    "queued":if waiter {1} else {0},
                    "queue":if waiter {json!([{"label":"waiter","enqueued_at":67767976233532800.0}])} else {json!([])}}]}),
                )
            });
            let mut command = home.board_command(&url);
            command.args(["lease", "check", "resource"]);
            if json_output {
                command.arg("--json");
            }
            let output = command.output().unwrap();
            assert_eq!(output.status.code(), Some(1));
            assert!(output.stderr.is_empty());
            if json_output {
                let reply: Value = serde_json::from_slice(&output.stdout).unwrap();
                let stamp = if waiter {
                    &reply["board"]["queue"][0]["enqueued_at"]
                } else {
                    &reply["board"]["expected_end"]
                };
                assert_eq!(stamp, &json!(67767976233532800.0));
            } else {
                assert!(
                    String::from_utf8(output.stdout)
                        .unwrap()
                        .contains(if waiter {
                            "waiter since ?"
                        } else {
                            ", expected end ?"
                        })
                );
            }
            assert_eq!(
                fs::read(home.0.join("instance.id")).unwrap(),
                b"0123456789ab\n"
            );
            assert!(!home.0.join("lease-resource.lock").exists());
            assert_eq!(peer.join().unwrap().len(), 1);
        }
    }
}

#[test]
fn timestamp_clocks_use_local_dates_and_floor_fractional_seconds() {
    use chrono::{Local, TimeZone};

    let today = Local::now().date_naive();
    for date in [today, today.pred_opt().unwrap()] {
        let local = Local
            .from_local_datetime(&date.and_hms_opt(12, 34, 59).unwrap())
            .earliest()
            .unwrap();
        let expected = if date == today {
            "12:34".to_owned()
        } else {
            format!("{} 12:34", date.format("%Y-%m-%d"))
        };
        // Rounding this producer's fractional second would cross into 12:35.
        let stamp = json!(local.timestamp() as f64 + 0.999);
        let home = Home::board();
        let (url, peer) = server(1, move |_, header, _| {
            assert!(header.starts_with("POST /api/coordination/leases "));
            assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
            (
                200,
                json!({"leases":[{"name":"resource","holder":{"label":"holder"},
                    "expected_end":stamp,"queued":1,
                    "queue":[{"label":"waiter","enqueued_at":stamp}]}]}),
            )
        });
        let mut command = home.board_command(&url);
        let output = command
            .args(["lease", "check", "resource"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stderr.is_empty());
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(
            text.contains(&format!(", expected end {expected}")),
            "local date {date}: {text:?}"
        );
        assert!(text.contains(&format!("waiter since {expected}")));
        assert!(!home.0.join("lease-resource.lock").exists());
        assert_eq!(peer.join().unwrap().len(), 1);
    }
}

#[test]
fn nonascii_headers_fall_back_but_surrogate_replies_refuse_before_effects() {
    for (field, surrogate) in [
        ("agent_id", false),
        ("credential", false),
        ("agent_id", true),
        ("credential", true),
    ] {
        let home = Home::board();
        let (url, peer) = server_raw(1, move |_, header, _| {
            assert!(header.starts_with("POST /api/coordination/register "));
            assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
            let mut reply = registered();
            reply[field] = json!("nonascii-é");
            let raw = if surrogate {
                // A Python string can contain an unpaired JSON surrogate.
                if field == "agent_id" {
                    br#"{"agent_id":"agent-\ud800","credential":"fixture-key"}"#.to_vec()
                } else {
                    br#"{"agent_id":"fixture-agent","credential":"key-\ud800"}"#.to_vec()
                }
            } else {
                serde_json::to_vec(&reply).unwrap()
            };
            (200, raw)
        });
        let output = home
            .board_command(&url)
            .args([
                "lease",
                "run",
                "resource",
                "--timeout",
                "0",
                "--",
                "missing-review-child",
            ])
            .output()
            .unwrap();
        assert!(output.stdout.is_empty());
        let newline = if cfg!(windows) { "\r\n" } else { "\n" };
        if surrogate {
            assert_eq!(output.status.code(), Some(1));
            assert_eq!(
                output.stderr,
                format!("lease: HTTP_REPLY_NOT_UNDERSTOOD{newline}").as_bytes()
            );
            assert!(!home.0.join("lease-resource.lock").exists());
        } else {
            assert_eq!(output.status.code(), Some(127));
            assert_eq!(output.stderr, format!("lease: board skipped: registration headers are not understood; waiting on the local lock for 'resource' alone (not FIFO){newline}lease: command not found: missing-review-child{newline}").as_bytes());
            assert!(home.0.join("lease-resource.lock").exists());
        }
        assert_eq!(peer.join().unwrap().len(), 1);
    }
}

#[test]
fn queued_timeout_leaves_its_ticket_without_starting_a_child() {
    let home = Home::board();
    let (url, peer) = server(3, |index, header, body| {
        assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
        match index {
            0 => {
                assert!(header.starts_with("POST /api/coordination/register "));
                assert_eq!(body["capabilities"], json!({"resumable":false}));
                assert_eq!(body["wake_enabled"], false);
                (200, registered())
            }
            1 => {
                assert!(header.starts_with("POST /api/coordination/lease "));
                assert!(has_header(header, "X-PL-Agent-Key", "fixture-key"));
                (200, json!({"state":"queued","position":2,"queued":2}))
            }
            _ => {
                assert!(header.starts_with("POST /api/coordination/release "));
                assert_eq!(body["name"], "resource");
                (200, json!({"released":true}))
            }
        }
    });
    let output = home
        .board_command(&url)
        .args([
            "lease",
            "run",
            "resource",
            "--timeout",
            "0",
            "--",
            "missing-board-child",
        ])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(75));
    assert!(output.stdout.is_empty());
    let text = String::from_utf8_lossy(&output.stderr);
    assert!(text.contains("position 2 of 2"));
    assert!(!text.contains("command not found"));
    assert!(!home.0.join("lease-resource.lock").exists());
    assert_eq!(peer.join().unwrap().len(), 3);
}

#[test]
fn queued_ticket_is_polled_then_granted_before_child_start() {
    let home = Home::board();
    let start = Instant::now();
    let (url, peer) = server(4, move |index, header, body| match index {
        0 => (200, registered()),
        1 | 2 => {
            assert!(header.starts_with("POST /api/coordination/lease "));
            assert!(has_header(header, "X-PL-Agent", "fixture-agent"));
            assert!(has_header(header, "X-PL-Agent-Key", "fixture-key"));
            assert_eq!(body["name"], "resource");
            assert_eq!(body["expect"], 604800);
            if index == 2 {
                assert!(start.elapsed() >= Duration::from_secs(5));
            }
            (
                200,
                json!({"state":if index == 1 {"queued"} else {"held"},"position":1,"queued":1}),
            )
        }
        _ => {
            assert!(header.starts_with("POST /api/coordination/release "));
            (200, json!({"released":true}))
        }
    });
    let output = home
        .board_command(&url)
        .args([
            "lease",
            "run",
            "resource",
            "--expect",
            "7d",
            "--timeout",
            "10",
            "--",
            "missing-board-child",
        ])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(127));
    let text = String::from_utf8_lossy(&output.stderr);
    assert!(text.contains("position 1 of 1"));
    assert!(text.contains("command not found: missing-board-child"));
    assert!(!text.contains("gave up waiting"));
    assert_eq!(peer.join().unwrap().len(), 4);
    assert!(
        fs::File::open(home.0.join("lease-resource.lock"))
            .unwrap()
            .try_lock()
            .is_ok()
    );
}
#[test]
fn board_grant_precedes_local_lock_and_release_follows_its_close() {
    let home = Home::board();
    let path = home.0.join("lease-resource.lock");
    let external = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(&path)
        .unwrap();
    external.lock().unwrap();
    let (url, peer) = server(3, |index, header, _| match index {
        0 => (200, registered()),
        1 => (200, json!({"state":"held","fence":7})),
        _ => {
            assert!(header.starts_with("POST /api/coordination/release "));
            (200, json!({"released":true}))
        }
    });
    let output = home
        .board_command(&url)
        .args([
            "lease",
            "run",
            "resource",
            "--timeout",
            "0",
            "--",
            "missing-board-child",
        ])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(75));
    assert!(String::from_utf8_lossy(&output.stderr).contains("the board granted 'resource'"));
    assert!(!String::from_utf8_lossy(&output.stderr).contains("command not found"));
    peer.join().unwrap();
    drop(external);
}
#[test]
fn renewal_keeps_original_expectation_and_purpose_and_loss_does_not_stop_child() {
    let home = Home::board();
    let path = home.0.join("lease-resource.lock");
    let released_path = path.clone();
    let start = Instant::now();
    let (url, peer) = server(4, move |index, header, body| match index {
        0 => (200, registered()),
        1 | 2 => {
            assert!(header.starts_with("POST /api/coordination/lease "));
            assert_eq!(body["ttl"], 30);
            assert_eq!(body["expect"], 604800);
            assert_eq!(body["purpose"], "fixture purpose");
            if index == 2 {
                assert!(start.elapsed() >= Duration::from_secs(10));
                let file = fs::File::open(&released_path).unwrap();
                assert!(matches!(
                    file.try_lock(),
                    Err(std::fs::TryLockError::WouldBlock)
                ));
            }
            (200, json!({"state":if index==1{"held"}else{"queued"}}))
        }
        _ => {
            assert!(header.starts_with("POST /api/coordination/release "));
            let file = fs::File::open(&released_path).unwrap();
            assert!(
                file.try_lock().is_ok(),
                "local lock must be closed before HTTP release"
            );
            (200, json!({"released":true}))
        }
    });
    let helper = std::env::current_exe().unwrap();
    let output = home
        .board_command(&url)
        .args([
            "lease",
            "run",
            "resource",
            "--ttl",
            "30",
            "--expect",
            "7d",
            "--purpose",
            "fixture purpose",
            "--timeout",
            "0",
            "--",
        ])
        .arg(helper)
        .args(["--exact", "fixture_child", "--nocapture"])
        .env("LEASE_FIXTURE_CHILD", "1")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&output.stdout).contains("fixture-child-ended"));
    let text = String::from_utf8_lossy(&output.stderr);
    assert_eq!(text.matches("the board no longer shows").count(), 1);
    assert!(!text.contains("fixture-key") && !text.contains("fixture-bearer"));
    peer.join().unwrap();
}
#[test]
fn fixture_child() {
    if std::env::var_os("LEASE_FIXTURE_CHILD").is_some() {
        thread::sleep(Duration::from_millis(11200));
        println!("fixture-child-ended");
    }
}

#[cfg(unix)]
#[test]
fn signal_child() {
    if std::env::var_os("LEASE_SIGNAL_CHILD").is_some() {
        fs::write(
            std::env::var("LEASE_SIGNAL_READY").unwrap(),
            std::process::id().to_string(),
        )
        .unwrap();
        thread::sleep(Duration::from_secs(60));
    }
}

#[cfg(unix)]
#[test]
fn sigterm_stops_owned_child_before_releasing_local_lock() {
    use rustix::process::{Pid, Signal, kill_process, test_kill_process};

    let home = Home::board();
    let ready = home.0.join("child-ready");
    let helper = std::env::current_exe().unwrap();
    let mut command = home.board_command("http://127.0.0.1:1");
    command
        .args(["lease", "run", "resource", "--no-board", "--"])
        .arg(helper)
        .args(["--exact", "signal_child", "--nocapture"])
        .env("LEASE_SIGNAL_CHILD", "1")
        .env("LEASE_SIGNAL_READY", &ready);
    let mut child = command.spawn().unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !ready.exists() {
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("owned child did not become ready");
        }
        thread::sleep(Duration::from_millis(10));
    }
    let pid = child.id();
    kill_process(
        Pid::from_raw(pid.try_into().unwrap()).unwrap(),
        Signal::TERM,
    )
    .unwrap();
    let stopped = Instant::now() + Duration::from_secs(10);
    let status = loop {
        if let Some(s) = child.try_wait().unwrap() {
            break s;
        }
        if Instant::now() >= stopped {
            let _ = child.kill();
            let _ = child.wait();
            panic!("lease did not stop");
        }
        thread::sleep(Duration::from_millis(10));
    };
    assert_eq!(status.code(), Some(143));
    let file = fs::File::open(home.0.join("lease-resource.lock")).unwrap();
    assert!(file.try_lock().is_ok());
    let child_pid = fs::read_to_string(ready).unwrap().parse::<i32>().unwrap();
    assert_eq!(
        test_kill_process(Pid::from_raw(child_pid).unwrap()),
        Err(rustix::io::Errno::SRCH)
    );
}

#[test]
fn own_stale_hold_is_free_but_foreign_hold_is_held() {
    let home = Home::board();
    fs::write(home.0.join("lease-resource.lock"), b"").unwrap();
    for (label, expected_exit) in [
        ("lease-hold@0123456789ab", 0),
        ("lease-hold@abcdef012345", 1),
        ("lease-run", 1),
    ] {
        let label = label.to_owned();
        let (url, peer) = server(1, move |_, header, body| {
            assert!(header.starts_with("POST /api/coordination/leases "));
            assert!(!has_header(header, "X-PL-Agent", "fixture-agent"));
            assert_eq!(*body, json!({"name":"resource"}));
            (
                200,
                json!({"leases":[{"name":"resource","holder":{"label":label},"queued":0,"queue":[]}]}),
            )
        });
        let output = home
            .board_command(&url)
            .args(["lease", "check", "resource", "--json"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(expected_exit));
        assert!(output.stderr.is_empty());
        peer.join().unwrap();
    }
}

#[test]
fn malformed_grant_or_queue_reply_releases_board_record_and_leaves_lock_reacquirable() {
    use std::sync::{Arc, Mutex};

    for state in ["held", "queued"] {
        let home = Home::board();
        let path = home.0.join("lease-resource.lock");
        fs::write(&path, b"existing lock\n").unwrap();
        let record = Arc::new(Mutex::new(None));
        let board_record = record.clone();
        let (url, peer) = server_raw(4, move |index, header, body| {
            assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
            let reply = match index {
                0 => {
                    assert!(header.starts_with("POST /api/coordination/register "));
                    registered()
                }
                1 => {
                    assert!(header.starts_with("POST /api/coordination/lease "));
                    assert!(has_header(header, "X-PL-Agent", "fixture-agent"));
                    assert!(has_header(header, "X-PL-Agent-Key", "fixture-key"));
                    assert_eq!(body["name"], "resource");
                    // The board commits before its HTTP 200 reply becomes malformed.
                    *board_record.lock().unwrap() = Some(state);
                    return (
                        200,
                        format!(r#"{{"state":"{state}","fence":NaN}}"#).into_bytes(),
                    );
                }
                2 => {
                    assert!(header.starts_with("POST /api/coordination/release "));
                    assert!(has_header(header, "X-PL-Agent", "fixture-agent"));
                    assert!(has_header(header, "X-PL-Agent-Key", "fixture-key"));
                    assert_eq!(*body, json!({"name":"resource"}));
                    assert_eq!(board_record.lock().unwrap().take(), Some(state));
                    json!({"released":true})
                }
                _ => {
                    assert!(header.starts_with("POST /api/coordination/leases "));
                    assert_eq!(*body, json!({"name":"resource"}));
                    assert!(board_record.lock().unwrap().is_none());
                    json!({"leases":[]})
                }
            };
            (200, serde_json::to_vec(&reply).unwrap())
        });
        let output = home
            .board_command(&url)
            .args([
                "lease",
                "run",
                "resource",
                "--timeout",
                "0",
                "--",
                "missing-board-child",
            ])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            "lease: HTTP_REPLY_NOT_UNDERSTOOD\n"
                .replace('\n', if cfg!(windows) { "\r\n" } else { "\n" })
                .as_bytes()
        );
        assert!(
            record.lock().unwrap().is_none(),
            "board record survived malformed {state} reply"
        );
        assert_eq!(fs::read(&path).unwrap(), b"existing lock\n");
        let file = fs::File::open(&path).unwrap();
        assert!(file.try_lock().is_ok());
        drop(file);
        let checked = home
            .board_command(&url)
            .args(["lease", "check", "resource", "--json"])
            .output()
            .unwrap();
        assert_eq!(checked.status.code(), Some(0));
        assert!(checked.stderr.is_empty());
        let report: Value = serde_json::from_slice(&checked.stdout).unwrap();
        assert_eq!(report["held"], false);
        assert_eq!(report["local"]["state"], "free");
        assert_eq!(peer.join().unwrap().len(), 4);
    }
}

#[test]
fn nonproducer_json_reply_is_refused_without_a_report() {
    for value in [r#""\ud800""#, "NaN", "Infinity", "-Infinity", "1e400"] {
        let home = Home::board();
        let raw = format!(r#"{{"leases":[{{"name":"resource","holder":{{"label":{value}}},"expected_end":NaN}}]}}"#).into_bytes();
        let (url, peer) = server_raw(1, move |_, header, body| {
            assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
            assert_eq!(body["name"], "resource");
            (200, raw.clone())
        });
        let output = home
            .board_command(&url)
            .args(["lease", "check", "resource", "--json"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            "lease: HTTP_REPLY_NOT_UNDERSTOOD\n"
                .replace('\n', if cfg!(windows) { "\r\n" } else { "\n" })
                .as_bytes()
        );
        assert_eq!(peer.join().unwrap().len(), 1);
    }
}

#[test]
fn surrogate_replies_are_refused_without_aliasing_or_state_changes() {
    for name in [
        b"\\ud800".as_slice(),
        b"ab\\udfff".as_slice(),
        b"\\ud800\\ud800".as_slice(),
        b"\xed\xa0\x80\xed\xb0\x80".as_slice(),
        b"emoji-\xf0\x9f\x98\x80-\\ud800".as_slice(),
    ] {
        for json in [false, true] {
            let home = Home::board();
            fs::write(home.0.join("lease-.lock"), b"").unwrap();
            let before = fs::read(home.0.join("instance.id")).unwrap();
            let mut raw = b"{\"leases\":[{\"name\":\"".to_vec();
            raw.extend_from_slice(name);
            raw.extend_from_slice(b"\",\"holder\":{\"label\":\"h\"}}]}");
            let (url, peer) = server_raw(1, move |_, header, body| {
                assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
                assert!(body["name"].is_null());
                (200, raw.clone())
            });
            let mut command = home.board_command(&url);
            command.args(["lease", "list"]);
            if json {
                command.arg("--json");
            }
            let output = command.output().unwrap();
            assert_eq!(output.status.code(), Some(1));
            assert!(output.stdout.is_empty());
            assert_eq!(
                output.stderr,
                "lease: HTTP_REPLY_NOT_UNDERSTOOD\n"
                    .replace('\n', if cfg!(windows) { "\r\n" } else { "\n" })
                    .as_bytes()
            );
            assert_eq!(peer.join().unwrap().len(), 1);
            assert_eq!(fs::read(home.0.join("instance.id")).unwrap(), before);
            assert_eq!(fs::read(home.0.join("lease-.lock")).unwrap(), b"");
            assert_eq!(fs::read_dir(&home.0).unwrap().count(), 2);
        }
    }
}

#[test]
fn raw_surrogate_name_cannot_alias_a_scalar_and_discount_its_holder() {
    let home = Home::board();
    fs::write(home.0.join("lease-_-31237b17.lock"), b"").unwrap();
    let (url, peer) = server_raw(1, |_, header, body| {
        assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
        assert_eq!(body["name"], "\u{10000}");
        (200, b"{\"leases\":[{\"name\":\"\xf0\x90\x80\x80\",\"holder\":{\"label\":\"claim\"}},{\"name\":\"\xed\xa0\x80\xed\xb0\x80\",\"holder\":{\"label\":\"lease-hold@0123456789ab\"}}]}".to_vec())
    });
    let output = home
        .board_command(&url)
        .args(["lease", "check", "\u{10000}", "--json"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        "lease: HTTP_REPLY_NOT_UNDERSTOOD\n"
            .replace('\n', if cfg!(windows) { "\r\n" } else { "\n" })
            .as_bytes()
    );
    assert_eq!(fs::read(home.0.join("lease-_-31237b17.lock")).unwrap(), b"");
    assert_eq!(peer.join().unwrap().len(), 1);
}

#[test]
#[cfg(windows)]
fn exact_stdout_buffer_boundary_is_buffered_through_8192_bytes() {
    use std::{
        process::Stdio,
        sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
            mpsc,
        },
    };
    for target in [8191, 8192, 8193] {
        let home = Home::board();
        let length = Arc::new(AtomicUsize::new(7000));
        let peer_length = length.clone();
        let (ready, proceed) = mpsc::channel();
        let (url, peer) = server(2, move |index, header, _| {
            assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
            if index == 1 {
                proceed.recv_timeout(Duration::from_secs(5)).unwrap();
            }
            (
                200,
                json!({"leases":[{"name":"resource","holder":{"label":"h","extra":"x".repeat(peer_length.load(Ordering::SeqCst))}}]}),
            )
        });
        let ordinary = home
            .board_command(&url)
            .args(["lease", "check", "resource", "--json"])
            .output()
            .unwrap();
        assert_eq!(ordinary.status.code(), Some(1));
        let measured = ordinary.stdout.len() - 2;
        length.store(7000 + target - measured, Ordering::SeqCst);
        let mut child = home
            .board_command(&url)
            .args(["lease", "check", "resource", "--json"])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        drop(child.stdout.take());
        ready.send(()).unwrap();
        let output = child.wait_with_output().unwrap();
        let (code, diagnostic) = if target <= 8192 {
            (120, "lease: stdout write failed\r\n")
        } else {
            (
                70,
                "lease: check of 'resource' failed (stdout write failed); neither held nor free is known\r\n",
            )
        };
        assert_eq!(output.status.code(), Some(code), "target {target}");
        assert_eq!(output.stderr, diagnostic.as_bytes());
        assert_eq!(peer.join().unwrap().len(), 2);
    }
}

#[test]
#[cfg(windows)]
fn large_list_closed_stdout_returns_one_with_terminal_error() {
    use std::{process::Stdio, sync::mpsc};
    let home = Home::board();
    for index in 0..65 {
        fs::write(
            home.0
                .join(format!("lease-{index:03}-{}.lock", "x".repeat(80))),
            b"",
        )
        .unwrap();
    }
    let (ready, proceed) = mpsc::channel();
    let (url, peer) = server(1, move |_, header, _| {
        assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
        proceed.recv_timeout(Duration::from_secs(5)).unwrap();
        (200, json!({"leases":[]}))
    });
    let mut child = home
        .board_command(&url)
        .args(["lease", "list", "--json"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    drop(child.stdout.take());
    ready.send(()).unwrap();
    let output = child.wait_with_output().unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(output.stderr, b"stdout write failed\r\n");
    assert_eq!(peer.join().unwrap().len(), 1);
}

#[test]
fn nonascii_stamp_does_not_discount_an_existing_board_holder() {
    let home = Home::board();
    let stamp = b"\xc2\xa00123456789ab\xc2\xa0";
    fs::write(home.0.join("instance.id"), stamp).unwrap();
    fs::write(home.0.join("lease-resource.lock"), b"").unwrap();
    let (url, peer) = server(1, |_, header, _| {
        assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
        (
            200,
            json!({"leases":[{"name":"resource","holder":{"label":"lease-hold@0123456789ab"}}]}),
        )
    });
    let output = home
        .board_command(&url)
        .args(["lease", "check", "resource", "--json"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["held"], true);
    assert_eq!(value["local"]["state"], "free");
    assert_eq!(fs::read(home.0.join("instance.id")).unwrap(), stamp);
    peer.join().unwrap();
}

#[test]
fn noniterable_queue_is_a_failed_check_without_a_partial_report() {
    for queue in ["1", "1.5", "true"] {
        let home = Home::board();
        let raw = format!(r#"{{"leases":[{{"name":"resource","holder":{{"label":"h"}},"queued":1,"queue":{queue}}}]}}"#).into_bytes();
        let (url, peer) = server_raw(1, move |_, header, _| {
            assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
            (200, raw.clone())
        });
        let output = home
            .board_command(&url)
            .args(["lease", "check", "resource"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(70));
        assert!(output.stdout.is_empty());
        let text = "lease: check of 'resource' failed (queue is not understood); neither held nor free is known\n".to_owned();
        let expected = if cfg!(windows) {
            text.replace('\n', "\r\n")
        } else {
            text
        };
        assert_eq!(output.stderr, expected.as_bytes());
        peer.join().unwrap();
    }
}

#[test]
#[cfg(windows)]
fn closed_stdout_matches_small_flush_and_large_check_failures() {
    use std::{process::Stdio, sync::mpsc};
    for length in [0, 20000] {
        let home = Home::board();
        let (ready, proceed) = mpsc::channel();
        let (url, peer) = server(1, move |_, header, _| {
            assert!(has_header(header, "Authorization", "Bearer fixture-bearer"));
            proceed.recv_timeout(Duration::from_secs(5)).unwrap();
            (
                200,
                json!({"leases":[{"name":"resource","holder":{"label":"h","extra":"x".repeat(length)}}]}),
            )
        });
        let mut child = home
            .board_command(&url)
            .args(["lease", "check", "resource", "--json"])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        drop(child.stdout.take());
        ready.send(()).unwrap();
        let output = child.wait_with_output().unwrap();
        let (code, expected) = if length == 0 {
            (120, "lease: stdout write failed\r\n")
        } else {
            (
                70,
                "lease: check of 'resource' failed (stdout write failed); neither held nor free is known\r\n",
            )
        };
        assert_eq!(output.status.code(), Some(code));
        assert_eq!(output.stderr, expected.as_bytes());
        peer.join().unwrap();
    }
}
