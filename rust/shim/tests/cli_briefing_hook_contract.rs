#![forbid(unsafe_code)]
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

fn parser_command() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
    command
        .env_clear()
        .env("PATH", "")
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1");
    for name in ["SYSTEMROOT", "WINDIR"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command
}

#[test]
fn public_columns_ascii_fallback_and_narrow_whitespace() {
    let baseline = parser_command()
        .args(["briefing", "--help"])
        .env("COLUMNS", "80")
        .output()
        .unwrap();
    assert_eq!(baseline.status.code(), Some(0));
    for width in ["٤٠", "4_0"] {
        let output = parser_command()
            .args(["briefing", "--help"])
            .env("COLUMNS", width)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(0));
        assert_eq!(output.stdout, baseline.stdout);
        assert!(output.stderr.is_empty());
    }
    let narrow = parser_command()
        .args(["briefing", "--help"])
        .env("COLUMNS", "1")
        .output()
        .unwrap();
    assert_eq!(narrow.status.code(), Some(0));
    assert!(narrow.stderr.is_empty());
    let line = if cfg!(windows) {
        b"    Code/Codex \r\n".as_slice()
    } else {
        b"    Code/Codex \n".as_slice()
    };
    assert!(narrow.stdout.windows(line.len()).any(|bytes| bytes == line));
}

#[test]
fn public_parser_prescans_late_ambiguity_before_help() {
    let output = parser_command()
        .args(["briefing", "--help", "--max"])
        .env("COLUMNS", "40")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let expected = "usage: pseudolife-mcp briefing\n       [-h] [--max-unsure MAX_UNSURE]\n       [--max-lessons MAX_LESSONS]\n       [--max-world MAX_WORLD]\n       [--hook-json] [--coordination]\npseudolife-mcp briefing: error: ambiguous option: --max could match --max-unsure, --max-lessons, --max-world\n";
    let expected = if cfg!(windows) {
        expected.replace('\n', "\r\n")
    } else {
        expected.into()
    };
    assert_eq!(output.stderr, expected.as_bytes());
}

#[test]
fn public_help_closed_output_has_native_failure_contract() {
    for unbuffered in [false, true] {
        let mut command = parser_command();
        command
            .args(["briefing", "--help"])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if unbuffered {
            command.env("PYTHONUNBUFFERED", "1");
        }
        let mut child = command.spawn().unwrap();
        drop(child.stdout.take());
        let deadline = Instant::now() + Duration::from_secs(8);
        while child.try_wait().unwrap().is_none() {
            if Instant::now() >= deadline {
                child.kill().unwrap();
                break;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        let output = child.wait_with_output().unwrap();
        assert_eq!(output.status.code(), Some(1));
        let expected = "pseudolife-mcp briefing: output failed\n";
        let expected = if cfg!(windows) {
            expected.replace('\n', "\r\n")
        } else {
            expected.into()
        };
        assert_eq!(output.stderr, expected.as_bytes());
    }
}

#[test]
fn public_prompt_leaf_fetches_and_advances_cursor_without_python() {
    prompt_leaf(false, false);
}

#[test]
fn public_prompt_closed_output_preserves_existing_and_new_mark() {
    for existing in [false, true] {
        prompt_leaf(true, existing);
    }
}

fn prompt_leaf(closed_output: bool, existing: bool) {
    let home = std::env::temp_dir().join(format!("pseudolife-hook-test-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    if existing {
        fs::create_dir(home.join("digests")).unwrap();
        fs::write(
            home.join("digests")
                .join(format!("{:x}.mark", Sha256::digest(b"public-hook"))),
            b"77.0\n",
        )
        .unwrap();
    }
    #[cfg(unix)]
    if existing {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(home.join("digests"), fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(
            home.join("digests")
                .join(format!("{:x}.mark", Sha256::digest(b"public-hook"))),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
    }
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    let peer = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(8);
        let mut stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(error)
                    if error.kind() == std::io::ErrorKind::WouldBlock
                        && Instant::now() < deadline =>
                {
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(error) => panic!("bounded peer: {error}"),
            }
        };
        stream.set_nonblocking(false).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut bytes = Vec::new();
        while !bytes.ends_with(b"\r\n\r\n") {
            let mut byte = [0];
            stream.read_exact(&mut byte).unwrap();
            bytes.push(byte[0]);
        }
        let request = String::from_utf8(bytes).unwrap();
        let target = if existing {
            "GET /api/hook/memory-changes?session_id=public-hook&since=77.0 HTTP/1.1\r\n"
        } else {
            "GET /api/hook/memory-changes?session_id=public-hook HTTP/1.1\r\n"
        };
        assert!(request.starts_with(target));
        assert!(
            request
                .to_ascii_lowercase()
                .contains("authorization: bearer synthetic-hook\r\n")
        );
        let body = "123..\r\n café  \r\n";
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        stream.flush().unwrap();
    });
    let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
    command.env_clear();
    for name in ["SYSTEMROOT", "WINDIR"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command.args(["prompt-hook", "--help", "ignored"]);
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        command.arg(std::ffi::OsStr::from_bytes(b"\xff"));
    }
    let mut child = command
        .env("HOME", &home)
        .env("USERPROFILE", &home)
        .env("LOCALAPPDATA", &home)
        .env("PSEUDOLIFE_DIGEST_DIR", home.join("digests"))
        .env("PSEUDOLIFE_MCP_DAEMON_URL", format!("http://{address}"))
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
        .env("PSEUDOLIFE_MCP_TOKEN", "synthetic-hook")
        .env("PSEUDOLIFE_MCP_PYTHON", "missing-fixture-interpreter")
        .env("PATH", "")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    if closed_output {
        drop(child.stdout.take());
    }
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"{\"session_id\":\"public-hook\"}")
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(8);
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            child.kill().unwrap();
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    let output = child.wait_with_output().unwrap();
    peer.join().unwrap();
    let mark = home
        .join("digests")
        .join(format!("{:x}.mark", Sha256::digest(b"public-hook")));
    let mark_bytes = fs::read(&mark).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&mark).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            fs::metadata(home.join("digests"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
    }
    fs::remove_dir_all(home).unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stderr.is_empty());
    let text = "{\"hookSpecificOutput\": {\"hookEventName\": \"UserPromptSubmit\", \"additionalContext\": \" caf\\u00e9  \"}}\n";
    let expected = if cfg!(windows) {
        text.replace('\n', "\r\n")
    } else {
        text.to_owned()
    };
    if closed_output {
        assert!(output.stdout.is_empty());
        assert_eq!(
            mark_bytes,
            if existing {
                b"77.0\n".as_slice()
            } else {
                b"".as_slice()
            }
        );
    } else {
        assert_eq!(output.stdout, expected.as_bytes());
        assert_eq!(mark_bytes, b"123..\n");
    }
}

#[test]
fn public_prompt_malformed_ignored_input_sends_no_request() {
    let home =
        std::env::temp_dir().join(format!("pseudolife-hook-refusal-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    for input in [
        r#"{"session_id":"sess-1","other":NaN}"#,
        r#"{"session_id":"sess-1","other":Infinity}"#,
        r#"{"session_id":"sess-1","other":"\ud800"}"#,
    ] {
        let mut child = parser_command()
            .arg("prompt-hook")
            .env("HOME", &home)
            .env("USERPROFILE", &home)
            .env("LOCALAPPDATA", &home)
            .env("PSEUDOLIFE_DIGEST_DIR", home.join("digests"))
            .env(
                "PSEUDOLIFE_MCP_DAEMON_URL",
                format!("http://{}", listener.local_addr().unwrap()),
            )
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(input.as_bytes())
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(8);
        while child.try_wait().unwrap().is_none() {
            if Instant::now() >= deadline {
                child.kill().unwrap();
                break;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        let output = child.wait_with_output().unwrap();
        assert_eq!(output.status.code(), Some(0));
        assert!(output.stdout.is_empty() && output.stderr.is_empty());
        assert!(!home.join("digests").exists());
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
    }
    fs::remove_dir_all(home).unwrap();
}
