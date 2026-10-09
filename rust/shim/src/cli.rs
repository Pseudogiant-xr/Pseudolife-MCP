//! UTF-8 CLI streams and scalar argv at Python oracle 686b3f95c4a4d4e6c2d81e1b76be9901945a8da1.
use std::fmt::Write as _;
use std::io::{self, Write};
use std::process::ExitCode;

mod backup;
pub mod board_audit;
mod briefing_hook;
mod connect;
mod doctor;
mod doorbell_seen;
mod episode;
mod hook_http;
mod hook_json;
pub mod lease;
pub mod maintainer;
mod move_cli;
mod pairing;
mod test_login;
mod transfer;
mod version;
pub mod wait_mail;

const HELP: &str = include_str!("cli_help.txt");
const DEFERRED_MODES: &[&str] = &[
    "serve",
    "embedded",
    "coordination-recovery",
    "board-audit",
    "doctor",
    "connect",
    "invite",
    "test-login",
    "pair",
    "expose",
    "tunnel",
    "move",
    "update",
    "backup",
    "export",
    "import",
    "maintainer",
];

fn text_bytes(text: &str) -> Vec<u8> {
    // Python's Windows console text stream translates LF even when redirected.
    if cfg!(windows) {
        text.replace('\n', "\r\n").into_bytes()
    } else {
        text.as_bytes().to_vec()
    }
}

fn mode_repr(mode: &str) -> String {
    let quote = if mode.contains('\'') && !mode.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = String::new();
    result.push(quote);
    for character in mode.chars() {
        match character {
            '\\' => result.push_str("\\\\"),
            '\t' => result.push_str("\\t"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            value if value == quote => {
                result.push('\\');
                result.push(value);
            }
            value
                if crate::board::claims::forbidden(value)
                    || matches!(
                        value,
                        '\u{a0}' | '\u{1680}' | '\u{2000}'
                            ..='\u{200a}'
                                | '\u{2028}'
                                | '\u{2029}'
                                | '\u{202f}'
                                | '\u{205f}'
                                | '\u{3000}'
                    ) =>
            {
                let point = value as u32;
                if point <= 0xff {
                    let _ = write!(result, "\\x{point:02x}");
                } else if point <= 0xffff {
                    let _ = write!(result, "\\u{point:04x}");
                } else {
                    let _ = write!(result, "\\U{point:08x}");
                }
            }
            value => result.push(value),
        }
    }
    result.push(quote);
    result
}

/// Handle leaves before daemon attachment; default/shim/channel keep the proxy path.
pub fn dispatch(mode: Option<&str>) -> Option<ExitCode> {
    let mode = mode.unwrap_or("shim");
    if mode == "backup"
        && let Some(code) = backup::run(std::env::args_os().skip(2).collect())
    {
        return Some(ExitCode::from(code));
    }
    if mode == "wait-mail" {
        return Some(ExitCode::from(wait_mail::run(
            std::env::args_os().skip(2).collect(),
        )));
    }
    if matches!(mode, "shim" | "channel") {
        return None;
    }
    if matches!(mode, "briefing" | "prompt-hook") {
        return Some(briefing_hook::run(mode));
    }
    if matches!(mode, "help" | "-h" | "--help") {
        return Some(
            if io::stdout().lock().write_all(&text_bytes(HELP)).is_ok() {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            },
        );
    }
    if matches!(mode, "version" | "--version") {
        return Some(
            if io::stdout()
                .lock()
                .write_all(&text_bytes(&version::text()))
                .is_ok()
            {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            },
        );
    }
    if mode == "doorbell-prompt-seen" {
        return Some(doorbell_seen::run());
    }
    if mode == "doctor"
        && let Some(code) = doctor::run(std::env::args_os().skip(2).collect())
    {
        return Some(code);
    }
    if matches!(mode, "export" | "import")
        && let Some(code) = transfer::run(mode, std::env::args_os().skip(2).collect())
    {
        return Some(code);
    }
    if mode == "test-login"
        && let Some(code) = test_login::run(std::env::args_os().skip(2).collect())
    {
        return Some(code);
    }
    if mode == "connect"
        && let Some(code) = connect::run(std::env::args_os().skip(2).collect())
    {
        return Some(code);
    }
    if matches!(mode, "invite" | "pair")
        && let Some(code) = pairing::run(mode)
    {
        return Some(code);
    }
    if mode == "move"
        && let Some(code) = move_cli::run(std::env::args_os().skip(2).collect())
    {
        return Some(ExitCode::from(code));
    }
    let (message, code) = if DEFERRED_MODES.contains(&mode) {
        (
            format!(
                "pseudolife-stdio: mode {} is deferred in this candidate\n",
                mode_repr(mode)
            ),
            1,
        )
    } else {
        (
            format!(
                "unknown mode {}; see: pseudolife-mcp --help\n",
                mode_repr(mode)
            ),
            2,
        )
    };
    Some(
        if io::stderr().lock().write_all(&text_bytes(&message)).is_ok() {
            ExitCode::from(code)
        } else {
            ExitCode::FAILURE
        },
    )
}

/// Hook leaves read stdin before making an unauthenticated health request.
pub async fn dispatch_episode(mode: Option<&str>) -> Option<ExitCode> {
    match mode {
        Some(mode @ ("episode-start" | "episode-end")) => Some(episode::run(mode).await),
        _ => None,
    }
}
