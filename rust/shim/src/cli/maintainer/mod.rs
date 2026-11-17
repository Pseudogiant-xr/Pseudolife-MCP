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

/// Parse the host actions; prefix validation remains inside the transaction.
fn parse(arguments: &[OsString]) -> Result<Action, String> {
    let arguments = arguments
        .iter()
        .map(|argument| argument.to_str())
        .collect::<Option<Vec<_>>>()
        .ok_or("argument is not valid Unicode; retype it as Unicode text")?;
    if arguments.first() == Some(&"enrol-code") {
        let mut wait = true;
        let mut poll = 2.0;
        let mut items = arguments[1..].iter();
        while let Some(item) = items.next() {
            match *item {
                "--no-wait" => wait = false,
                "--poll" => {
                    let value = items.next().ok_or("--poll needs seconds; use --poll 2")?;
                    poll = value.parse().map_err(|_| "--poll needs numeric seconds; use --poll 2")?;
                }
                item if item.starts_with("--poll=") => {
                    poll = item[7..].parse().map_err(|_| "--poll needs numeric seconds; use --poll 2")?;
                }
                item if item.starts_with("--no-wait=") => return Err("--no-wait takes no value; use --no-wait".into()),
                item if item.len() > 2 && item.starts_with("--") && "--no-wait".starts_with(item) =>
                    return Err("abbreviated option; use --no-wait in full".into()),
                item if item.len() > 2 && item.starts_with("--") && "--poll".starts_with(item) =>
                    return Err("abbreviated option; use --poll SECONDS in full".into()),
                _ => return Err("unexpected enrol-code argument; run pseudolife-mcp maintainer enrol-code --help".into()),
            }
        }
        return Ok(Action::EnrolCode { wait, poll });
    }
    match arguments.as_slice() {
        ["confirm", prefix] if prefix_argument(prefix) => Ok(Action::Confirm((*prefix).to_owned())),
        ["revoke", prefix] if prefix_argument(prefix) => Ok(Action::Revoke((*prefix).to_owned())),
        ["confirm", "--", prefix] => Ok(Action::Confirm((*prefix).to_owned())),
        ["revoke", "--", prefix] => Ok(Action::Revoke((*prefix).to_owned())),
        ["reset"] => Ok(Action::Reset { yes: false }),
        ["reset", "--yes"] => Ok(Action::Reset { yes: true }),
        ["list"] => Ok(Action::List),
        [] => Err("an action is required; run pseudolife-mcp maintainer --help".into()),
        ["confirm", ..] => Err(
            "confirm needs one credential prefix; use pseudolife-mcp maintainer confirm -- PREFIX"
                .into(),
        ),
        ["revoke", ..] => Err(
            "revoke needs one credential prefix; use pseudolife-mcp maintainer revoke -- PREFIX"
                .into(),
        ),
        ["reset", option]
            if *option == "-y" || (option.starts_with("--") && "--yes".starts_with(*option)) =>
        {
            Err("abbreviated option; use --yes in full".into())
        }
        ["reset", ..] => {
            Err("reset accepts --yes once; use pseudolife-mcp maintainer reset --yes".into())
        }
        ["list", ..] => Err("list takes no arguments; use pseudolife-mcp maintainer list".into()),
        [word, ..] => {
            let choices = ["enrol-code", "confirm", "revoke", "reset", "list", "setup"];
            let matches: Vec<_> = choices
                .into_iter()
                .filter(|action| !word.is_empty() && action.starts_with(*word))
                .collect();
            if matches.is_empty() {
                Err("unrecognized maintainer action; run pseudolife-mcp maintainer --help".into())
            } else {
                Err(format!(
                    "abbreviated maintainer action; spell {} in full",
                    matches.join(" or ")
                ))
            }
        }
    }
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

/// Every host action is answered natively; setup retains its separate path.
pub fn run(arguments: Vec<OsString>) -> Option<i32> {
    if arguments.first().is_some_and(|value| value == "setup") {
        return Some(i32::from(setup::run(
            arguments.into_iter().skip(1).collect(),
        )));
    }
    if let Some(help) = help(&arguments) {
        let _ = print(&help.replace("\r\n", "\n"));
        return Some(0);
    }
    let action = match parse(&arguments) {
        Ok(action) => action,
        Err(message) => {
            crate::stderrln!("error: {message}");
            return Some(2);
        }
    };
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            crate::stderrln!(
                "error: could not create maintainer runtime ({error}); free system resources and retry"
            );
            return Some(2);
        }
    };
    runtime.block_on(async {
        match dsn() {
            Ok(Some(dsn)) => return bank::run(&dsn, action).await.map(i32::from),
            Ok(None) => {},
            Err(message) => { crate::stderrln!("error: {message}"); return Some(2); }
        }
        // A running daemon container wins: a completed Docker migration can
        // leave a stopped lite cluster behind (ops/preflight_lite.py).
        if let Some(code) = crate::pg::resolution::run_in_daemon("maintainer", &arguments, None, None).await {
            return Some(code);
        }
        if let Some(bank) = crate::cli::retired::find_lite_bank(None) {
            crate::stderrln!("error: {}", crate::cli::retired::lite_bank_refusal(&bank));
            return Some(2);
        }
        crate::stderrln!("error: no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL (Docker tier: start the daemon, or run `docker exec -it pseudolife-mcp-daemon pseudolife-mcp maintainer ...` on its host)");
        Some(2)
    })
}

/// Missing or empty settings retain the existing container and retired-bank checks.
fn dsn() -> Result<Option<crate::pg::Dsn>, &'static str> {
    let Some(value) = std::env::var_os("PSEUDOLIFE_MCP_DATABASE_URL").filter(|v| !v.is_empty())
    else {
        return Ok(None);
    };
    let value = value.into_string().map_err(
        |_| "PSEUDOLIFE_MCP_DATABASE_URL is not Unicode; set it to a Unicode PostgreSQL URL",
    )?;
    crate::pg::Dsn::parse(&value).map(Some).map_err(|error| match error {
        crate::pg::Error::UnsupportedOption(ref option) if option == "sslmode" =>
            "PSEUDOLIFE_MCP_DATABASE_URL has an unsupported sslmode; use disable, prefer, require, verify-ca or verify-full",
        crate::pg::Error::UnsupportedOption(_) =>
            "PSEUDOLIFE_MCP_DATABASE_URL has an unsupported option; use only host, port, user, password, dbname, sslmode and sslrootcert",
        crate::pg::Error::InvalidOption("host") =>
            "PSEUDOLIFE_MCP_DATABASE_URL needs one TCP host; set it to a single-host PostgreSQL URL",
        crate::pg::Error::InvalidOption("port") =>
            "PSEUDOLIFE_MCP_DATABASE_URL has an invalid port; set its port to 1 through 65535",
        _ => "PSEUDOLIFE_MCP_DATABASE_URL is not an admitted PostgreSQL connection setting; set it to a valid single-host PostgreSQL URL",
    })
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
    fn canonical_shapes_parse_and_every_other_shape_is_rejected() {
        assert_eq!(
            parse(&argv(&["enrol-code"])).ok(),
            Some(Action::EnrolCode {
                wait: true,
                poll: 2.0
            })
        );
        assert_eq!(
            parse(&argv(&["enrol-code", "--no-wait"])).ok(),
            Some(Action::EnrolCode {
                wait: false,
                poll: 2.0
            })
        );
        assert_eq!(
            parse(&argv(&["confirm", "-abcdef"])).ok(),
            Some(Action::Confirm("-abcdef".into()))
        );
        assert_eq!(
            parse(&argv(&["revoke", ""])).ok(),
            Some(Action::Revoke(String::new()))
        );
        assert_eq!(
            parse(&argv(&["reset"])).ok(),
            Some(Action::Reset { yes: false })
        );
        assert_eq!(
            parse(&argv(&["reset", "--yes"])).ok(),
            Some(Action::Reset { yes: true })
        );
        assert_eq!(parse(&argv(&["list"])).ok(), Some(Action::List));
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
            assert_eq!(parse(&argv(deferred)).ok(), None, "{deferred:?}");
        }
    }

    #[test]
    fn malformed_commands_are_native_usage_errors() {
        for items in [
            &[][..],
            &["list", "extra"],
            &["enrol-code", "--no-w"],
            &["enrol-code", "--poll"],
            &["enrol-code", "--poll", "not-a-number"],
            &["confirm"],
            &["reset", "--ye"],
            &["reset", "--yes", "--yes"],
            &["lis"],
        ] {
            assert_eq!(run(argv(items)), Some(2), "{items:?}");
        }
        assert_eq!(
            parse(&argv(&["confirm", "--", "--help"])).ok(),
            Some(Action::Confirm("--help".into()))
        );
        assert_eq!(
            parse(&argv(&["enrol-code", "--poll", "-1", "--no-wait"])).ok(),
            Some(Action::EnrolCode {
                wait: false,
                poll: -1.0
            })
        );
    }

    #[test]
    fn a_prefix_after_the_delimiter_is_a_literal_value() {
        for prefix in ["--help", "-h", "--", "-abcdef"] {
            assert_eq!(
                parse(&argv(&["confirm", "--", prefix])).ok(),
                Some(Action::Confirm(prefix.into()))
            );
            assert_eq!(
                parse(&argv(&["revoke", "--", prefix])).ok(),
                Some(Action::Revoke(prefix.into()))
            );
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
    fn non_unicode_arguments_refuse_natively() {
        use std::os::unix::ffi::OsStringExt;
        let arguments = vec![OsString::from("confirm"), OsString::from_vec(vec![0xff; 8])];
        assert_eq!(run(arguments), Some(2));
    }

    #[cfg(windows)]
    #[test]
    fn non_unicode_arguments_refuse_natively() {
        use std::os::windows::ffi::OsStringExt;
        let arguments = vec![
            OsString::from("confirm"),
            OsString::from_wide(&[0xd800, 0x61, 0x62, 0x63, 0x64, 0x65]),
        ];
        assert_eq!(parse(&arguments), None);
    }
}
