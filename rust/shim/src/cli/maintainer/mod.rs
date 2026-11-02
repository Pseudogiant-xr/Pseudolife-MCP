//! Native host passkey administration, help and polling, over PostgreSQL or
//! the selected daemon container. Guided setup is integrated separately.
mod bank;
mod display;
pub mod setup;
mod sqlstate;
use std::{
    ffi::OsString,
    io::{self, Write},
};

/// One canonical action, as `maintainer_cli.main` parses it.
#[derive(Debug, PartialEq)]
enum Action {
    EnrolCode { wait: bool, poll: f64 },
    Confirm(String),
    Revoke(String),
    Reset { yes: bool },
    List,
}

/// The argv shapes real producers use (the docs, `maintainer setup`, the
/// guide), including explicit polling and the prefix delimiter. Help is
/// handled before parsing; guided setup is integrated separately.
fn parse(arguments: &[OsString]) -> Option<Action> {
    let arguments: Vec<&str> = arguments
        .iter()
        .map(|argument| argument.to_str())
        .collect::<Option<_>>()?;
    if arguments.first() == Some(&"enrol-code") {
        let mut wait = true;
        let mut poll = 2.0;
        let mut items = arguments[1..].iter();
        while let Some(item) = items.next() {
            match *item {
                "--no-wait" => wait = false,
                "--poll" => poll = items.next()?.parse().ok()?,
                item if item.starts_with("--poll=") => poll = item[7..].parse().ok()?,
                _ => return None,
            }
        }
        return Some(Action::EnrolCode { wait, poll });
    }
    Some(match arguments.as_slice() {
        // `main` inserts `--` before a lone prefix that starts with `-`
        // (a base64url id does one time in 64), so every value but these
        // three is the prefix.
        ["confirm", prefix] | ["confirm", "--", prefix] if prefix_argument(prefix) => {
            Action::Confirm((*prefix).to_owned())
        }
        ["revoke", prefix] | ["revoke", "--", prefix] if prefix_argument(prefix) => {
            Action::Revoke((*prefix).to_owned())
        }
        ["reset"] => Action::Reset { yes: false },
        ["reset", "--yes"] => Action::Reset { yes: true },
        ["list"] => Action::List,
        _ => return None,
    })
}

fn prefix_argument(value: &str) -> bool {
    !matches!(value, "-h" | "--help" | "--")
}

fn help(arguments: &[OsString]) -> Option<&'static str> {
    if !arguments
        .iter()
        .take_while(|arg| *arg != "--")
        .any(|arg| arg == "--help" || arg == "-h")
    {
        return None;
    }
    Some(match arguments.first().and_then(|arg| arg.to_str()) {
        Some("enrol-code") => include_str!("enrol-code_help.txt"),
        Some("confirm") => include_str!("confirm_help.txt"),
        Some("revoke") => include_str!("revoke_help.txt"),
        Some("reset") => include_str!("reset_help.txt"),
        Some("list") => include_str!("list_help.txt"),
        _ => include_str!("top_help.txt"),
    })
}

/// Run the action after `maintainer`; `None` defers before any effect.
pub fn run(arguments: Vec<OsString>) -> Option<u8> {
    if arguments.first().is_some_and(|value| value == "setup") {
        return Some(setup::run(arguments.into_iter().skip(1).collect()));
    }
    if let Some(help) = help(&arguments) {
        let _ = print(&help.replace("\r\n", "\n"));
        return Some(0);
    }
    let action = parse(&arguments)?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .ok()?;
    runtime.block_on(async {
        if let Some(dsn) = dsn() {
            return bank::run(&dsn, action).await;
        }
        if std::env::var_os("PSEUDOLIFE_MCP_DATABASE_URL").is_some_and(|v| !v.is_empty()) {
            // Preserve the shared client's explicit DSN-domain boundary.
            return None;
        }
        if let Some(code) = crate::pg::resolution::run_in_daemon("maintainer", &arguments, None, None).await {
            return Some(code as u8);
        }
        crate::stderrln!("error: no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL, or run where the lite tier's data dir holds one (Docker tier: start the daemon, or run `docker exec -it pseudolife-mcp-daemon pseudolife-mcp maintainer ...` on its host)");
        Some(2)
    })
}

/// The configured DSN, or `None` (no or empty variable, a value that is not
/// Unicode, or a DSN spelling the native client does not admit): the lite
/// tier's embedded bank and the container re-run are reached only then.
fn dsn() -> Option<crate::pg::Dsn> {
    let value = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL").ok()?;
    if value.is_empty() {
        return None;
    }
    crate::pg::Dsn::parse(&value).ok()
}

/// `print(..., file=out)` lines on stdout, flushed; the error when stdout
/// refused them.
fn print(text: &str) -> io::Result<()> {
    let mut stdout = io::stdout().lock();
    stdout
        .write_all(&super::text_bytes(text))
        .and_then(|()| stdout.flush())
}

/// How an unbuffered write of a whole report ended.
#[derive(Debug, PartialEq, Eq)]
enum Written {
    All,
    /// Refused before any byte reached stdout.
    Nothing,
    /// Refused after some bytes did.
    Part,
}

/// Where a report's bytes go.
#[derive(Debug, PartialEq, Eq)]
enum Sink {
    /// std's stdout, which hands a Windows console UTF-16 through
    /// `WriteConsoleW`, as CPython's console writer (`_WindowsConsoleIO`)
    /// does: a label such as `☎` renders whatever the console's code page.
    Console,
    /// The UTF-8 bytes through a counted duplicate of the descriptor.
    Counted,
}

/// A Windows console takes the console path; anything else (a pipe, a
/// file, any POSIX stream, where CPython writes the encoded bytes) the
/// counted one.
fn report_sink(windows: bool, terminal: bool) -> Sink {
    if windows && terminal {
        Sink::Console
    } else {
        Sink::Counted
    }
}

/// `bytes` (UTF-8 text) to stdout on the path [`report_sink`] picks. A
/// console write that fails may already have shown text, so it is `Part`.
fn write_report(bytes: &[u8]) -> Written {
    use std::io::IsTerminal;
    match report_sink(cfg!(windows), io::stdout().is_terminal()) {
        Sink::Console => {
            let mut stdout = io::stdout().lock();
            match stdout.write_all(bytes).and_then(|()| stdout.flush()) {
                Ok(()) => Written::All,
                Err(_) => Written::Part,
            }
        }
        Sink::Counted => write_unbuffered(bytes),
    }
}

/// `bytes` to stdout through a duplicate of its descriptor, so every byte a
/// write reports has left this process (std's own stdout buffers lines).
fn write_unbuffered(bytes: &[u8]) -> Written {
    #[cfg(unix)]
    let handle = std::os::fd::AsFd::as_fd(&io::stdout()).try_clone_to_owned();
    #[cfg(windows)]
    let handle = std::os::windows::io::AsHandle::as_handle(&io::stdout()).try_clone_to_owned();
    let Ok(handle) = handle else {
        return Written::Nothing;
    };
    write_all_counted(&mut std::fs::File::from(handle), bytes)
}

fn write_all_counted(sink: &mut impl Write, mut bytes: &[u8]) -> Written {
    let mut wrote = false;
    while !bytes.is_empty() {
        match sink.write(bytes) {
            Ok(0) | Err(_) if !wrote => return Written::Nothing,
            Ok(0) | Err(_) => return Written::Part,
            Ok(count) => {
                wrote = true;
                bytes = &bytes[count..];
            }
        }
    }
    Written::All
}

/// The class CPython's print raises for a refused stdout: a pipe whose
/// reader closed is `BrokenPipeError` on POSIX and `OSError` (EINVAL) on
/// Windows; anything else is reported as `OSError`.
fn stdout_error_class(error: &io::Error) -> &'static str {
    if error.kind() == io::ErrorKind::BrokenPipe && !cfg!(windows) {
        "BrokenPipeError"
    } else {
        "OSError"
    }
}

/// The exit status after a committed change whose report stdout refused:
/// CPython's interpreter-shutdown flush fails there and exits 120.
const STDOUT_REFUSED: u8 = 120;

/// `_REFUSALS`, printed as `refused: ...` on stderr with exit 1.
fn refused(code: &str) -> u8 {
    let text = match code {
        "enrolment_closed" => {
            "a passkey is already pending or active; revoke it or run `pseudolife-mcp maintainer reset` first"
        }
        "credential_not_found" => {
            "no single passkey matches that prefix (in the state this command needs); run `pseudolife-mcp maintainer list`"
        }
        "invalid_request" => "the prefix must be at least 6 characters of the credential id",
        other => other,
    };
    crate::stderrln!("refused: {text}");
    1
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(items: &[&str]) -> Vec<OsString> {
        items.iter().map(OsString::from).collect()
    }

    #[test]
    fn canonical_shapes_parse_and_every_other_shape_defers() {
        assert_eq!(
            parse(&argv(&["enrol-code"])),
            Some(Action::EnrolCode {
                wait: true,
                poll: 2.0
            })
        );
        assert_eq!(
            parse(&argv(&["enrol-code", "--no-wait"])),
            Some(Action::EnrolCode {
                wait: false,
                poll: 2.0
            })
        );
        assert_eq!(
            parse(&argv(&["confirm", "-abcdef"])),
            Some(Action::Confirm("-abcdef".into()))
        );
        assert_eq!(
            parse(&argv(&["revoke", ""])),
            Some(Action::Revoke(String::new()))
        );
        assert_eq!(parse(&argv(&["reset"])), Some(Action::Reset { yes: false }));
        assert_eq!(
            parse(&argv(&["reset", "--yes"])),
            Some(Action::Reset { yes: true })
        );
        assert_eq!(parse(&argv(&["list"])), Some(Action::List));
        for deferred in [
            &[][..],
            &["setup"],
            &["setup", "--check"],
            &["--help"],
            &["list", "--help"],
            &["enrol-code", "--no-w"],
            &["confirm"],
            &["confirm", "-h"],
            &["confirm", "--help"],
            &["confirm", "--"],
            &["confirm", "abcdef", "abcdef"],
            &["revoke", "--"],
            &["reset", "--yes", "--yes"],
            &["reset", "--ye"],
            &["reset", "-y"],
            &["list", "extra"],
            &["lis"],
            &["enrol"],
        ] {
            assert_eq!(parse(&argv(deferred)), None, "{deferred:?}");
        }
    }

    /// A sink that takes `room` bytes (three at a time), then refuses.
    struct Closing {
        room: usize,
        taken: Vec<u8>,
    }

    impl Write for Closing {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.room == 0 {
                return Err(io::ErrorKind::BrokenPipe.into());
            }
            let count = bytes.len().min(self.room).min(3);
            self.room -= count;
            self.taken.extend_from_slice(&bytes[..count]);
            Ok(count)
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn an_unbuffered_write_says_whether_any_byte_left() {
        for (room, expected) in [
            (0, Written::Nothing),
            (1, Written::Part),
            (9, Written::Part),
            (10, Written::All),
            (99, Written::All),
        ] {
            let mut sink = Closing {
                room,
                taken: Vec::new(),
            };
            assert_eq!(
                write_all_counted(&mut sink, b"0123456789"),
                expected,
                "{room}"
            );
            assert_eq!(sink.taken, &b"0123456789"[..room.min(10)]);
        }
    }

    #[test]
    fn only_a_windows_console_takes_the_console_path() {
        // A Windows console renders UTF-16 through WriteConsoleW (CPython's
        // _WindowsConsoleIO); bytes written to it are read in its code page.
        assert_eq!(report_sink(true, true), Sink::Console);
        assert_eq!(report_sink(true, false), Sink::Counted);
        assert_eq!(report_sink(false, true), Sink::Counted);
        assert_eq!(report_sink(false, false), Sink::Counted);
    }

    #[cfg(unix)]
    #[test]
    fn non_unicode_arguments_defer() {
        use std::os::unix::ffi::OsStringExt;
        let arguments = vec![OsString::from("confirm"), OsString::from_vec(vec![0xff; 8])];
        assert_eq!(parse(&arguments), None);
    }

    #[cfg(windows)]
    #[test]
    fn non_unicode_arguments_defer() {
        use std::os::windows::ffi::OsStringExt;
        let arguments = vec![
            OsString::from("confirm"),
            OsString::from_wide(&[0xd800, 0x61, 0x62, 0x63, 0x64, 0x65]),
        ];
        assert_eq!(parse(&arguments), None);
    }
}
