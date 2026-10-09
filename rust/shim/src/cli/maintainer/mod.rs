//! `pseudolife-mcp maintainer` (maintainer_cli.py) for its canonical argv:
//! `enrol-code [--no-wait]`, `confirm <prefix>`, `revoke <prefix>`,
//! `reset [--yes]` and `list`, against the bank named by an explicit
//! `PSEUDOLIFE_MCP_DATABASE_URL`. `setup`, help, every other argv shape, the
//! lite tier's embedded instance and the daemon-container re-run defer
//! before any effect: `run` answers `None` and the dispatcher prints the
//! mode's deferral line.
mod bank;
mod display;
use std::{
    ffi::OsString,
    io::{self, Write},
};

/// One canonical action, as `maintainer_cli.main` parses it.
#[derive(Debug, PartialEq, Eq)]
enum Action {
    EnrolCode { wait: bool },
    Confirm(String),
    Revoke(String),
    Reset { yes: bool },
    List,
}

/// The argv shapes real producers use (the docs, `maintainer setup`, the
/// guide). Anything else, including help, `setup`, `--poll`, repeated or
/// reordered options and abbreviations, is `None`.
fn parse(arguments: &[OsString]) -> Option<Action> {
    let arguments: Vec<&str> = arguments
        .iter()
        .map(|argument| argument.to_str())
        .collect::<Option<_>>()?;
    Some(match arguments.as_slice() {
        ["enrol-code"] => Action::EnrolCode { wait: true },
        ["enrol-code", "--no-wait"] => Action::EnrolCode { wait: false },
        // `main` inserts `--` before a lone prefix that starts with `-`
        // (a base64url id does one time in 64), so every value but these
        // three is the prefix.
        ["confirm", prefix] if prefix_argument(prefix) => Action::Confirm((*prefix).to_owned()),
        ["revoke", prefix] if prefix_argument(prefix) => Action::Revoke((*prefix).to_owned()),
        ["reset"] => Action::Reset { yes: false },
        ["reset", "--yes"] => Action::Reset { yes: true },
        ["list"] => Action::List,
        _ => return None,
    })
}

fn prefix_argument(value: &str) -> bool {
    !matches!(value, "-h" | "--help" | "--")
}

/// Run the action after `maintainer`; `None` defers before any effect.
pub fn run(arguments: Vec<OsString>) -> Option<u8> {
    let action = parse(&arguments)?;
    let dsn = dsn()?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .ok()?;
    runtime.block_on(bank::run(&dsn, action))
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

/// `print(..., file=out)` lines on stdout, flushed; `Err` when stdout
/// refused them.
fn print(text: &str) -> Result<(), ()> {
    let mut stdout = io::stdout().lock();
    stdout
        .write_all(&super::text_bytes(text))
        .and_then(|()| stdout.flush())
        .map_err(|_| ())
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
            Some(Action::EnrolCode { wait: true })
        );
        assert_eq!(
            parse(&argv(&["enrol-code", "--no-wait"])),
            Some(Action::EnrolCode { wait: false })
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
            &["enrol-code", "--no-wait", "--no-wait"],
            &["enrol-code", "--no-w"],
            &["enrol-code", "--poll", "0.1"],
            &["enrol-code", "--poll=0.1", "--no-wait"],
            &["confirm"],
            &["confirm", "-h"],
            &["confirm", "--help"],
            &["confirm", "--"],
            &["confirm", "--", "-abcdef"],
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
