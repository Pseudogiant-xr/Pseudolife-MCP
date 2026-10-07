#![forbid(unsafe_code)]
use std::{
    io::Write,
    process::{Command, Stdio},
};

fn invoke(mode: &str, input: &[u8]) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .args([mode, "--help", "ignored"])
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
fn outside_hook_field_types_do_not_reach_origin_or_network() {
    for mode in ["episode-start", "episode-end"] {
        for input in [
            r#"{"session_id":42}"#,
            r#"{"session_id":true}"#,
            r#"{"session_id":NaN}"#,
            r#"{"session_id":Infinity}"#,
            r#"{"session_id":[]}"#,
            r#"{"session_id":{}}"#,
            r#"{"session_id":null}"#,
            r#"{"session_id":"key","cwd":42}"#,
            r#"{"session_id":"key","cwd":false}"#,
            r#"{"session_id":"key","cwd":[]}"#,
            r#"{"session_id":"key","cwd":{}}"#,
            "{\"session_id\":\"raw\nline\"}",
        ] {
            let output = invoke(mode, input.as_bytes());
            assert!(output.status.success());
            assert!(output.stdout.is_empty() && output.stderr.is_empty());
        }
    }
}

#[test]
fn admitted_string_fields_and_host_path_units_reach_origin_validation() {
    for mode in ["episode-start", "episode-end"] {
        for input in [
            r#"{"session_id":"42"}"#,
            r#"{"session_id":"key","cwd":null}"#,
            r#"{"session_id":"key","cwd":"\\\\server\\share"}"#,
            r#"{"session_id":"key","cwd":"/missing-\udc80"}"#,
            r#"{"session_id":"key","cwd":"C:\\missing-\ud800"}"#,
            r#"{"session_id":"old","session_id":"new"}"#,
        ] {
            let output = invoke(mode, input.as_bytes());
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
}
