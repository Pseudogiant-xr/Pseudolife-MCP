//! restore's ArgumentParser, for the argv shapes whose outcome is certain.
//!
//! Answered: every option spelled in full, as `--opt value` or
//! `--opt=value`, repeated or not (argparse keeps the last), `-h` alone,
//! unknown `--` options and positionals (argparse's "unrecognized
//! arguments"), an ambiguous `--` prefix, values that are negative numbers,
//! and argparse's own errors (a missing value, an explicit argument to a
//! flag, a bad `--db-port`, the missing `--backup-file`). Deferred (`None`):
//! abbreviations, `--`, single-dash forms other than `-h` and `-` (option
//! clusters), non-Unicode argv, non-ASCII in any text argparse would echo or
//! in a single-dash argument or `--db-port` value, and any help or usage
//! when argparse would not wrap at 80 columns.
use std::{ffi::OsString, path::PathBuf};

use super::super::backup::args as backup_args;

pub(super) const HELP: &str = include_str!("restore_help.txt");

/// The help's usage paragraph, which argparse also prints before an error.
pub(super) fn usage() -> &'static str {
    let end = HELP.find("\n\n").expect("help has a usage paragraph");
    &HELP[..=end]
}

pub(super) struct Args {
    pub backup_file: PathBuf,
    pub state_archive: Option<PathBuf>,
    pub yes: bool,
    pub leave_stopped: bool,
    pub container: Option<String>,
    pub daemon_container: Option<String>,
    pub db: Option<String>,
    pub user: Option<String>,
    /// `int(value)`; `None` when the option was not given.
    pub db_port: Option<i128>,
    pub daemon_url: Option<String>,
    pub safety_dir: Option<PathBuf>,
}

pub(super) enum Parsed {
    Args(Box<Args>),
    Help,
    Error(String),
}

/// argparse's option strings in registration order (its ambiguity message
/// lists matches in this order).
const OPTIONS: [&str; 13] = [
    "-h",
    "--help",
    "--backup-file",
    "--state-archive",
    "--yes",
    "--leave-stopped",
    "--container",
    "--daemon-container",
    "--db",
    "--user",
    "--db-port",
    "--daemon-url",
    "--safety-dir",
];

fn flag(option: &str) -> bool {
    matches!(option, "-h" | "--help" | "--yes" | "--leave-stopped")
}

/// argparse's action name in its messages (`'/'.join(option_strings)`).
fn action_name(option: &str) -> &str {
    if matches!(option, "-h" | "--help") {
        "-h/--help"
    } else {
        option
    }
}

/// How argparse classifies one argument before consuming any.
#[derive(Debug, PartialEq)]
enum Class<'a> {
    /// 'A': a positional, or the value of the option before it.
    Value,
    /// 'O' for an option string, with any `=value`.
    Option(&'static str, Option<&'a str>),
    /// 'O' that names no option: an unrecognized argument.
    Unknown,
    /// argparse's "ambiguous option" error, raised while classifying.
    Ambiguous(String),
}

/// `ArgumentParser._parse_optional` for this parser; `None` defers.
fn classify(raw: &str) -> Option<Class<'_>> {
    if !raw.starts_with('-') || raw == "-" {
        return Some(Class::Value);
    }
    if raw == "--" {
        return None;
    }
    if let Some(option) = OPTIONS.iter().find(|option| **option == raw) {
        return Some(Class::Option(option, None));
    }
    if let Some((name, value)) = raw.split_once('=')
        && let Some(option) = OPTIONS.iter().find(|option| **option == name)
    {
        // `-h=...` continues as an option cluster in argparse.
        return if *option == "-h" {
            None
        } else {
            Some(Class::Option(option, Some(value)))
        };
    }
    if raw.starts_with("--") {
        let prefix = raw.split_once('=').map_or(raw, |(name, _)| name);
        let matches: Vec<_> = OPTIONS
            .iter()
            .filter(|option| option.starts_with(prefix))
            .collect();
        match matches.len() {
            0 => {}
            1 => return None,
            _ => {
                let names: Vec<&str> = matches.iter().map(|option| **option).collect();
                return Some(Class::Ambiguous(format!(
                    "ambiguous option: {raw} could match {}",
                    names.join(", ")
                )));
            }
        }
    } else {
        // A single-dash argument: `-h...` is a cluster; argparse's
        // negative-number test reads Unicode digits and `$` before a final
        // newline, which this leaf does not reproduce.
        if raw.starts_with("-h") || !raw.is_ascii() || raw.contains('\n') {
            return None;
        }
        let digits = &raw[1..];
        let negative = !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit())
            || digits.split_once('.').is_some_and(|(whole, fraction)| {
                whole.bytes().all(|b| b.is_ascii_digit())
                    && !fraction.is_empty()
                    && fraction.bytes().all(|b| b.is_ascii_digit())
            });
        if negative {
            return Some(Class::Value);
        }
    }
    if raw.contains(' ') {
        return Some(Class::Value);
    }
    Some(Class::Unknown)
}

/// C `isspace`, which `PyLong_FromString` strips. An ASCII str reaches it
/// unchanged, so the separators U+001C..U+001F (`str.isspace`, not C
/// `isspace`) are not stripped and make `int()` raise.
fn python_space(byte: u8) -> bool {
    matches!(byte, b'\t' | b'\n' | 0x0b | 0x0c | b'\r' | b' ')
}

/// CPython applies its integer digit limit (4300 by default, moved by
/// `PYTHONINTMAXSTRDIGITS`) only to texts longer than this many characters
/// (`_PY_LONG_MAX_STR_DIGITS_THRESHOLD`); a longer value is not decided here.
const INT_LIMIT_FREE: usize = 640;

/// `int(value)` for an ASCII value: `Some(Ok(n))`, `Some(Err(()))` for a
/// ValueError, `None` when the value is not ASCII (Python also reads
/// Unicode digits and spaces there) or long enough for the digit limit to
/// apply. Values past i128 saturate; every one of them is outside the port
/// range, the only use made of the number.
pub(super) fn python_int(value: &str) -> Option<Result<i128, ()>> {
    if !value.is_ascii() || value.len() > INT_LIMIT_FREE {
        return None;
    }
    let bytes = value.as_bytes();
    let start = bytes.iter().position(|b| !python_space(*b));
    let Some(start) = start else {
        return Some(Err(()));
    };
    let end = bytes.iter().rposition(|b| !python_space(*b)).unwrap() + 1;
    let mut text = &bytes[start..end];
    let negative = text.first() == Some(&b'-');
    if matches!(text.first(), Some(b'+' | b'-')) {
        text = &text[1..];
    }
    if text.is_empty() || !text[0].is_ascii_digit() || !text[text.len() - 1].is_ascii_digit() {
        return Some(Err(()));
    }
    let mut number: i128 = 0;
    for (index, byte) in text.iter().enumerate() {
        match byte {
            b'0'..=b'9' => {
                number = number
                    .saturating_mul(10)
                    .saturating_add(i128::from(byte - b'0'));
            }
            b'_' if text[index + 1].is_ascii_digit() => {}
            _ => return Some(Err(())),
        }
    }
    Some(Ok(if negative { -number } else { number }))
}

/// argparse wraps help and usage at `$COLUMNS - 2` (else the terminal's
/// width, else 80): only the 80-column text is reproduced.
fn wrapped_at_80() -> bool {
    backup_args::columns_supported() && backup_args::width() == 78
}

pub(super) fn parse(arguments: &[OsString]) -> Option<Parsed> {
    let values: Vec<&str> = arguments
        .iter()
        .map(|value| value.to_str())
        .collect::<Option<_>>()?;
    // argparse classifies every argument (raising an ambiguity) first.
    let mut classes = Vec::with_capacity(values.len());
    for raw in &values {
        let class = classify(raw)?;
        if let Class::Ambiguous(message) = class {
            return error(message);
        }
        classes.push(class);
    }
    let mut args = Args {
        backup_file: PathBuf::new(),
        state_archive: None,
        yes: false,
        leave_stopped: false,
        container: None,
        daemon_container: None,
        db: None,
        user: None,
        db_port: None,
        daemon_url: None,
        safety_dir: None,
    };
    let mut backup_file = false;
    let mut extras: Vec<&str> = Vec::new();
    let mut index = 0;
    while index < values.len() {
        let (option, explicit) = match &classes[index] {
            Class::Value | Class::Unknown => {
                extras.push(values[index]);
                index += 1;
                continue;
            }
            Class::Option(option, explicit) => (*option, *explicit),
            Class::Ambiguous(_) => unreachable!(),
        };
        index += 1;
        if flag(option) {
            if let Some(value) = explicit {
                return error(format!(
                    "argument {}: ignored explicit argument {}",
                    action_name(option),
                    super::super::mode_repr(value)
                ));
            }
            match option {
                "-h" | "--help" => return wrapped_at_80().then_some(Parsed::Help),
                "--yes" => args.yes = true,
                _ => args.leave_stopped = true,
            }
            continue;
        }
        let value = match explicit {
            Some(value) => value,
            None => {
                if classes.get(index) != Some(&Class::Value) {
                    return error(format!("argument {option}: expected one argument"));
                }
                index += 1;
                values[index - 1]
            }
        };
        let path = || backup_args::pathlib(std::path::Path::new(value));
        match option {
            "--backup-file" => {
                args.backup_file = path();
                backup_file = true;
            }
            "--state-archive" => args.state_archive = Some(path()),
            "--safety-dir" => args.safety_dir = Some(path()),
            "--container" => args.container = Some(value.to_owned()),
            "--daemon-container" => args.daemon_container = Some(value.to_owned()),
            "--db" => args.db = Some(value.to_owned()),
            "--user" => args.user = Some(value.to_owned()),
            "--daemon-url" => args.daemon_url = Some(value.to_owned()),
            "--db-port" => match python_int(value)? {
                Ok(number) => args.db_port = Some(number),
                Err(()) => {
                    return error(format!(
                        "argument --db-port: invalid int value: {}",
                        super::super::mode_repr(value)
                    ));
                }
            },
            _ => unreachable!(),
        }
    }
    if !backup_file {
        return error("the following arguments are required: --backup-file".into());
    }
    if !extras.is_empty() {
        return error(format!("unrecognized arguments: {}", extras.join(" ")));
    }
    Some(Parsed::Args(Box::new(args)))
}

/// argparse's `error()`: usage and the message on stderr, exit 2. Deferred
/// when the text would not be the 80-column ASCII the oracle prints.
fn error(message: String) -> Option<Parsed> {
    (wrapped_at_80() && message.is_ascii()).then_some(Parsed::Error(message))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse_strs(values: &[&str]) -> Option<Parsed> {
        let values: Vec<OsString> = values.iter().map(OsString::from).collect();
        parse(&values)
    }

    fn message(values: &[&str]) -> Option<String> {
        match parse_strs(values)? {
            Parsed::Error(message) => Some(message),
            Parsed::Help => Some("<help>".into()),
            Parsed::Args(_) => Some("<args>".into()),
        }
    }

    #[test]
    fn python_int_matches_cpython_for_ascii_spellings() {
        // Expected values printed by CPython 3.11 `int(value)`.
        for (value, expected) in [
            ("8765", Ok(8765)),
            (" 5432\t", Ok(5432)),
            ("\x0b7\x0c", Ok(7)),
            // An ASCII str reaches `PyLong_FromString` unchanged, which strips
            // C `isspace` only: the separators U+001C..U+001F stay and fail.
            ("\x1c7\x1f", Err(())),
            ("\x1f7", Err(())),
            ("+7", Ok(7)),
            ("-1", Ok(-1)),
            ("1_0", Ok(10)),
            ("007", Ok(7)),
            ("1__0", Err(())),
            ("_1", Err(())),
            ("1_", Err(())),
            ("0x10", Err(())),
            ("1 0", Err(())),
            ("", Err(())),
            ("   ", Err(())),
            ("+", Err(())),
            ("- 1", Err(())),
            ("1.0", Err(())),
        ] {
            assert_eq!(python_int(value), Some(expected), "{value:?}");
        }
        assert_eq!(python_int("\u{0661}"), None);
        // Past 640 characters CPython's integer digit limit (4300 unless the
        // environment moves it) may refuse the value: not decided here.
        assert!(python_int(&"1".repeat(640)).is_some());
        assert_eq!(python_int(&"1".repeat(641)), None);
    }

    #[test]
    fn negative_numbers_are_values_and_other_dashes_unknown() {
        assert_eq!(classify("-1"), Some(Class::Value));
        assert_eq!(classify("-.5"), Some(Class::Value));
        assert_eq!(classify("-1.5"), Some(Class::Value));
        assert_eq!(classify("-"), Some(Class::Value));
        assert_eq!(classify("-x y"), Some(Class::Value));
        assert_eq!(classify("-x"), Some(Class::Unknown));
        assert_eq!(classify("-1."), Some(Class::Unknown));
        assert_eq!(classify("--bogus"), Some(Class::Unknown));
        assert_eq!(classify("-hx"), None);
        assert_eq!(classify("-h=x"), None);
        assert_eq!(classify("--"), None);
        assert_eq!(classify("--back"), None);
        assert_eq!(classify("-\u{0661}"), None);
    }

    #[test]
    fn ambiguity_is_raised_before_any_action() {
        if !wrapped_at_80() {
            return;
        }
        assert_eq!(
            message(&["--db-port", "abc", "--d", "x"]).as_deref(),
            Some(
                "ambiguous option: --d could match --daemon-container, --db, --db-port, --daemon-url"
            )
        );
        assert_eq!(
            message(&["--da=1"]).as_deref(),
            Some("ambiguous option: --da=1 could match --daemon-container, --daemon-url")
        );
    }

    #[test]
    fn errors_follow_argparse_order() {
        if !wrapped_at_80() {
            return;
        }
        assert_eq!(
            message(&["--db-port", "abc", "--bogus"]).as_deref(),
            Some("argument --db-port: invalid int value: 'abc'")
        );
        assert_eq!(
            message(&["--bogus"]).as_deref(),
            Some("the following arguments are required: --backup-file")
        );
        assert_eq!(
            message(&["--backup-file", "x", "--bogus", "y"]).as_deref(),
            Some("unrecognized arguments: --bogus y")
        );
        assert_eq!(
            message(&["--backup-file", "--yes"]).as_deref(),
            Some("argument --backup-file: expected one argument")
        );
        assert_eq!(
            message(&["--yes=1"]).as_deref(),
            Some("argument --yes: ignored explicit argument '1'")
        );
        assert_eq!(
            message(&["--help=", "--backup-file", "x"]).as_deref(),
            Some("argument -h/--help: ignored explicit argument ''")
        );
        assert_eq!(
            message(&["--db-port", "1", "-h", "--bogus"]).as_deref(),
            Some("<help>")
        );
        assert_eq!(
            message(&["--db-port", "x", "-h"]).as_deref(),
            Some("argument --db-port: invalid int value: 'x'")
        );
        assert_eq!(message(&["--backup-file", "-1"]).as_deref(), Some("<args>"));
        assert_eq!(message(&["--backup-file", "x", "--bogus", "\u{e9}"]), None);
    }

    #[test]
    fn repeated_options_keep_the_last_value() {
        let Some(Parsed::Args(args)) = parse_strs(&[
            "--backup-file",
            "a",
            "--db-port=1",
            "--backup-file=b",
            "--db-port",
            "2",
        ]) else {
            panic!("expected arguments");
        };
        assert_eq!(args.backup_file, PathBuf::from("b"));
        assert_eq!(args.db_port, Some(2));
    }

    #[test]
    fn echoed_values_are_quoted_as_python_repr() {
        // Expected strings printed by CPython 3.11 `repr(value)`.
        for (value, expected) in [
            ("\x7f\x0bab", r"'\x7f\x0bab'"),
            ("it's", r#""it's""#),
            ("say \"hi\"", r#"'say "hi"'"#),
            ("both ' and \"", r#"'both \' and "'"#),
            ("back\\slash", r"'back\\slash'"),
            ("\x00\x1f\t\n\r", r"'\x00\x1f\t\n\r'"),
        ] {
            assert_eq!(super::super::super::mode_repr(value), expected, "{value:?}");
        }
    }

    #[test]
    fn usage_is_the_help_paragraph() {
        assert!(
            usage().starts_with("usage: pseudolife-mcp restore [-h] --backup-file BACKUP_FILE\n")
        );
        assert!(usage().ends_with("[--safety-dir SAFETY_DIR]\n"));
    }
}
