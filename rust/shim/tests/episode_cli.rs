#![forbid(unsafe_code)]
use std::{
    ffi::OsStr,
    io::Write,
    process::{Command, Stdio},
};

fn invoke(mode: &str, input: &[u8]) -> std::process::Output {
    invoke_with_tail(mode, input, OsStr::new("ignored"))
}

fn invoke_with_tail(mode: &str, input: &[u8], tail: &OsStr) -> std::process::Output {
    invoke_with_digit_limit(mode, input, tail, "4300")
}

fn invoke_with_digit_limit(
    mode: &str,
    input: &[u8],
    tail: &OsStr,
    limit: &str,
) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .args([OsStr::new(mode), OsStr::new("--help"), tail])
        .env("PSEUDOLIFE_MCP_DAEMON_URL", "invalid-origin")
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
        .env("CUDA_VISIBLE_DEVICES", "-1")
        .env("PYTHONINTMAXSTRDIGITS", limit)
        .env_remove("PSEUDOLIFE_MCP_TOKEN")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(input).unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn configured_integer_limits_control_whether_origin_validation_is_reached() {
    for mode in ["episode-start", "episode-end"] {
        for (limit, digits, accepted) in [
            ("640", 640, true),
            ("640", 641, false),
            ("0", 4301, true),
            ("5000", 4301, true),
            ("5000", 5001, false),
        ] {
            let input = format!("{{\"session_id\":{}}}", "9".repeat(digits));
            let output =
                invoke_with_digit_limit(mode, input.as_bytes(), OsStr::new("ignored"), limit);
            assert_eq!(output.status.code(), Some(i32::from(accepted)));
            assert!(output.stdout.is_empty());
            assert_eq!(output.stderr.is_empty(), !accepted);
        }
    }
}

#[test]
fn absent_keys_and_invalid_stdin_exit_silently_before_origin_validation() {
    for mode in ["episode-start", "episode-end"] {
        for input in [
            b"".as_slice(),
            b"{",
            b"[]",
            b"null",
            b"{}",
            b"\xff",
            b"{\"session_id\":false}",
            b"{\"session_id\":0}",
        ] {
            let output = invoke(mode, input);
            assert!(output.status.success());
            assert!(output.stdout.is_empty() && output.stderr.is_empty());
        }
    }
}

#[test]
fn present_key_preserves_the_invalid_origin_diagnostic() {
    for mode in ["episode-start", "episode-end"] {
        let output = invoke(mode, b"{\"session_id\":true}");
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        let message = format!("{}\n", pseudolife_stdio::daemon_url::INVALID_URL_MESSAGE);
        assert_eq!(
            output.stderr,
            if cfg!(windows) {
                message.replace('\n', "\r\n")
            } else {
                message
            }
            .into_bytes()
        );
    }
}

#[test]
fn nested_stdin_preserves_the_public_python_recursion_boundary() {
    for mode in ["episode-start", "episode-end"] {
        for (depth, leaf, accepted) in [
            (700, "0", true),
            (988, "0", true),
            (989, "0", false),
            (987, "[]", true),
            (988, "[]", false),
            (997, "0", false),
            (1001, "0", false),
        ] {
            let input = format!(
                "{{\"session_id\":\"key\",\"ignored\":{}{}{} }}",
                "[".repeat(depth),
                leaf,
                "]".repeat(depth)
            );
            let output = invoke(mode, input.as_bytes());
            assert_eq!(output.status.code(), Some(i32::from(accepted)));
            assert!(output.stdout.is_empty());
            assert_eq!(output.stderr.is_empty(), !accepted);
        }
    }
}

#[test]
fn ignored_opaque_tail_reaches_episode_key_and_origin_handling() {
    #[cfg(unix)]
    let opaque = {
        use std::os::unix::ffi::OsStringExt;
        std::ffi::OsString::from_vec(vec![0xff])
    };
    #[cfg(windows)]
    let opaque = {
        use std::os::windows::ffi::OsStringExt;
        std::ffi::OsString::from_wide(&[0xd800])
    };
    for mode in ["episode-start", "episode-end"] {
        for input in [b"{}".as_slice(), b"{"] {
            let output = invoke_with_tail(mode, input, &opaque);
            assert!(output.status.success());
            assert!(output.stdout.is_empty() && output.stderr.is_empty());
        }
        let output = invoke_with_tail(mode, b"{\"session_id\":\"key\"}", &opaque);
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        let message = format!("{}\n", pseudolife_stdio::daemon_url::INVALID_URL_MESSAGE);
        assert_eq!(
            output.stderr,
            if cfg!(windows) {
                message.replace('\n', "\r\n")
            } else {
                message
            }
            .into_bytes()
        );
    }
}
