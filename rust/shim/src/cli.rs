//! UTF-8 CLI streams and scalar argv at Python oracle 1d444aa1bc432431e5fcbc07a55e1fe661c0f8e4.
use std::fmt::Write as _;
use std::io::{self, Write};
use std::process::ExitCode;

mod backup;
pub mod board_audit;
mod briefing_hook;
mod conductor_watch;
mod connect;
mod console;
pub mod coordination_recovery;
mod doctor;
mod doorbell_seen;
mod episode;
mod expose;
mod hook_http;
mod hook_json;
mod host_connection;
mod installer;
pub mod lease;
pub mod maintainer;
mod migrate_flat;
mod move_cli;
mod ownership;
mod pairing;
pub(crate) mod program_lookup;
mod pyjson;
mod restore_cli;
pub mod retired;
mod test_login;
mod transfer;
mod tunnel;
mod update;
pub(crate) use update::tunnel_guard;
mod version;
pub mod wait_mail;

pub(crate) fn process_table() -> Result<Option<connect::ProcessTable>, ()> {
    connect::process_table()
}

const HELP: &str = include_str!("cli_help.txt");
/// Modes the native client recognises but hands back; none since `tunnel`
/// answers or refuses every argv itself.
const DEFERRED_MODES: &[&str] = &[];

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
    if mode == "private-path-check" {
        return Some(ExitCode::from(crate::private_path::hook_command(
            std::env::args_os().skip(2).collect(),
        )));
    }
    if mode == "migrate-flat" {
        return Some(ExitCode::from(migrate_flat::run(
            std::env::args_os().skip(2).collect(),
        )));
    }
    if mode == "update" {
        return Some(ExitCode::from(update::run(
            std::env::args_os().skip(2).collect(),
        )));
    }
    if mode == "runtime" {
        return Some(ExitCode::from(crate::runtime::run(
            std::env::args_os().skip(2).collect(),
        )));
    }
    if mode == "installer" {
        return Some(ExitCode::from(installer::run(
            std::env::args_os().skip(2).collect(),
        )));
    }
    if mode == "backup" {
        return Some(
            backup::run(std::env::args_os().skip(2).collect())
                .map(ExitCode::from)
                .expect("native backup dispatch answers every invocation"),
        );
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
    if mode == "doctor" {
        return Some(
            doctor::run(std::env::args_os().skip(2).collect())
                .expect("native doctor dispatch answers every invocation"),
        );
    }
    if matches!(mode, "export" | "import") {
        return Some(
            transfer::run(mode, std::env::args_os().skip(2).collect())
                .expect("native transfer dispatch answers export and import"),
        );
    }
    if mode == "test-login"
        && let Some(code) = test_login::run(std::env::args_os().skip(2).collect())
    {
        return Some(code);
    }
    if mode == "connect" {
        return Some(
            connect::run(std::env::args_os().skip(2).collect())
                .expect("native connect dispatch answers every invocation"),
        );
    }
    if matches!(mode, "invite" | "pair") {
        return Some(pairing::run(mode).expect("native pairing dispatch answers invite and pair"));
    }
    if mode == "move" {
        let arguments: Vec<std::ffi::OsString> = std::env::args_os().skip(2).collect();
        // Every move argv ends here: the recovery forms, then help and the
        // early refusals, then the transaction, and otherwise a usage
        // refusal that names the first problem.
        let code = restore_cli::move_native::run_rollback(&arguments)
            .or_else(|| move_cli::run(arguments.clone()))
            .or_else(|| restore_cli::move_native::run(&arguments))
            .unwrap_or_else(|| move_cli::invalid_arguments(&arguments));
        return Some(ExitCode::from(code));
    }
    // Hidden: the target half of `move`'s preflight, run over ssh.
    if mode == "move-target" {
        let arguments: Vec<std::ffi::OsString> = std::env::args_os().skip(2).collect();
        return Some(ExitCode::from(restore_cli::move_native::target::run(
            &arguments,
        )));
    }
    if mode == "tunnel" {
        return Some(tunnel::run(std::env::args_os().skip(2).collect()));
    }
    if mode == "restore"
        && let Some(code) = restore_cli::run(std::env::args_os().skip(2).collect())
    {
        return Some(code);
    }
    if mode == "console" {
        return Some(console::run(std::env::args_os().skip(2).collect()));
    }
    if mode == "embedded" {
        crate::stderrln!("{}", retired::embedded_message());
        return Some(ExitCode::from(1));
    }
    if mode == "expose"
        && let Some(code) = expose::run(std::env::args_os().skip(2).collect())
    {
        return Some(code);
    }
    if matches!(mode, "conductor-watch" | "delegate-watch")
        && let Some(code) = conductor_watch::run(mode, std::env::args_os().skip(2).collect())
    {
        return Some(code);
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
