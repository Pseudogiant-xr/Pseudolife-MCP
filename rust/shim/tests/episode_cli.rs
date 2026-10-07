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
    let mut child = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .args([OsStr::new(mode), OsStr::new("--help"), tail])
        .env("PSEUDOLIFE_MCP_DAEMON_URL", "invalid-origin")
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
        .env("CUDA_VISIBLE_DEVICES", "-1")
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
        let output = invoke(mode, b"{\"session_id\":\"key\"}");
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
