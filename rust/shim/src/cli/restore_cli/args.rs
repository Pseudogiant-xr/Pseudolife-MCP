//! restore's command line, in argparse's shape: every option as `--opt
//! value` or `--opt=value` (repeated, the last wins), a unique prefix of an
//! option, `-h`/`--help`, `--` ending the options, values that are negative
//! numbers, and argparse's errors (an ambiguous prefix, a missing value, an
//! explicit argument to a flag, a bad `--db-port`, the missing
//! `--backup-file`, unrecognized arguments). Every argv gets an answer.
use std::{ffi::OsString, path::PathBuf};

use super::super::backup::args as backup_args;

pub(super) const HELP: &str = include_str!("restore_help.txt");

/// The help's usage paragraph, which is also printed before an error.
pub(super) fn usage() -> &'static str {
    let end = HELP.find("\n\n").expect("help has a usage paragraph");
    &HELP[..=end]
}

#[derive(Debug, Clone)]
pub(super) struct Args {
    pub backup_file: PathBuf,
    pub state_archive: Option<PathBuf>,
    pub yes: bool,
    pub leave_stopped: bool,
    pub container: Option<String>,
    pub daemon_container: Option<String>,
    pub db: Option<String>,
    pub user: Option<String>,
    /// The `--db-port` integer; `None` when the option was not given.
    pub db_port: Option<i128>,
    pub daemon_url: Option<String>,
    pub safety_dir: Option<PathBuf>,
}

pub(super) enum Parsed {
    Args(Box<Args>),
    Help,
    Error(String),
}

/// The option strings in registration order (an ambiguity message lists
/// matches in this order).
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

/// The action's name in messages.
fn action_name(option: &str) -> &str {
    if matches!(option, "-h" | "--help") {
        "-h/--help"
    } else {
        option
    }
}

/// How one argument is read before any is consumed.
#[derive(Debug, PartialEq)]
enum Class<'a> {
    /// A positional, or the value of the option before it.
    Value,
    /// An option, with any `=value`.
    Option(&'static str, Option<&'a str>),
    /// Looks like an option but names none: an unrecognized argument.
    Unknown,
    /// `--`: every later argument is a positional.
    End,
    /// An ambiguous `--` prefix, refused before anything else.
    Ambiguous(String),
}

/// An ASCII negative number (`-1`, `-.5`, `-1.5`), which argparse reads as
/// a value.
fn negative_number(raw: &str) -> bool {
    let Some(digits) = raw.strip_prefix('-') else {
        return false;
    };
    let all = |text: &str| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
    all(digits)
        || digits.split_once('.').is_some_and(|(whole, fraction)| {
            whole.bytes().all(|b| b.is_ascii_digit()) && all(fraction)
        })
}

fn classify(raw: &str) -> Class<'_> {
    if !raw.starts_with('-') || raw == "-" {
        return Class::Value;
    }
    if raw == "--" {
        return Class::End;
    }
    // `-h<rest>`: a cluster that starts with `-h`, which prints the help.
    if let Some(rest) = raw.strip_prefix("-h")
        && !rest.is_empty()
    {
        return Class::Option("-h", Some(rest.strip_prefix('=').unwrap_or(rest)));
    }
    if let Some(option) = OPTIONS.iter().find(|option| **option == raw) {
        return Class::Option(option, None);
    }
    if raw.starts_with("--") {
        let (name, value) = match raw.split_once('=') {
            Some((name, value)) => (name, Some(value)),
            None => (raw, None),
        };
        if let Some(option) = OPTIONS.iter().find(|option| **option == name) {
            return Class::Option(option, value);
        }
        let matches: Vec<&'static str> = OPTIONS
            .iter()
            .copied()
            .filter(|option| option.starts_with(name))
            .collect();
        match matches.len() {
            0 => {}
            1 => return Class::Option(matches[0], value),
            _ => {
                return Class::Ambiguous(format!(
                    "ambiguous option: {raw} could match {}",
                    matches.join(", ")
                ));
            }
        }
    } else if negative_number(raw) {
        return Class::Value;
    }
    if raw.contains(' ') {
        return Class::Value;
    }
    Class::Unknown
}

/// C `isspace`, which surrounds an integer's digits.
fn int_space(byte: u8) -> bool {
    matches!(byte, b'\t' | b'\n' | 0x0b | 0x0c | b'\r' | b' ')
}

/// A decimal integer with optional sign, surrounding spaces and single
/// underscores between digits; `Err` for anything else (non-ASCII digits
/// included). Values past i128 saturate; every one is outside the port
/// range, the only use made of the number.
pub(super) fn python_int(value: &str) -> Result<i128, ()> {
    if !value.is_ascii() {
        return Err(());
    }
    let bytes = value.as_bytes();
    let start = bytes.iter().position(|b| !int_space(*b)).ok_or(())?;
    let end = bytes.iter().rposition(|b| !int_space(*b)).unwrap() + 1;
    let mut text = &bytes[start..end];
    let negative = text.first() == Some(&b'-');
    if matches!(text.first(), Some(b'+' | b'-')) {
        text = &text[1..];
    }
    if text.is_empty() || !text[0].is_ascii_digit() || !text[text.len() - 1].is_ascii_digit() {
        return Err(());
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
            _ => return Err(()),
        }
    }
    Ok(if negative { -number } else { number })
}

pub(super) fn parse(arguments: &[OsString]) -> Parsed {
    let Some(values) = arguments
        .iter()
        .map(|value| value.to_str())
        .collect::<Option<Vec<&str>>>()
    else {
        return Parsed::Error(
            "every argument must be valid Unicode text; rename the file or pass a path without undecodable bytes".into(),
        );
    };
    // Every argument is classified (an ambiguity refused) first.
    let mut classes = Vec::with_capacity(values.len());
    let mut ended = false;
    for raw in &values {
        let class = if ended { Class::Value } else { classify(raw) };
        match class {
            Class::Ambiguous(message) => return Parsed::Error(message),
            Class::End => {
                ended = true;
                classes.push(Class::End);
            }
            class => classes.push(class),
        }
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
            Class::End => {
                index += 1;
                continue;
            }
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
            if option == "-h" {
                return Parsed::Help;
            }
            if let Some(value) = explicit {
                return Parsed::Error(format!(
                    "argument {}: ignored explicit argument {}",
                    action_name(option),
                    super::super::mode_repr(value)
                ));
            }
            match option {
                "-h" | "--help" => return Parsed::Help,
                "--yes" => args.yes = true,
                _ => args.leave_stopped = true,
            }
            continue;
        }
        let value = match explicit {
            Some(value) => value,
            None => {
                if classes.get(index) != Some(&Class::Value) {
                    return Parsed::Error(format!("argument {option}: expected one argument"));
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
            "--db-port" => match python_int(value) {
                Ok(number) => args.db_port = Some(number),
                Err(()) => {
                    return Parsed::Error(format!(
                        "argument --db-port: invalid int value: {}",
                        super::super::mode_repr(value)
                    ));
                }
            },
            _ => unreachable!(),
        }
    }
    if !backup_file {
        return Parsed::Error("the following arguments are required: --backup-file".into());
    }
    if !extras.is_empty() {
        return Parsed::Error(format!("unrecognized arguments: {}", extras.join(" ")));
    }
    Parsed::Args(Box::new(args))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse_strs(values: &[&str]) -> Parsed {
        let values: Vec<OsString> = values.iter().map(OsString::from).collect();
        parse(&values)
    }

    fn message(values: &[&str]) -> String {
        match parse_strs(values) {
            Parsed::Error(message) => message,
            Parsed::Help => "<help>".into(),
            Parsed::Args(_) => "<args>".into(),
        }
    }

    #[test]
    fn integers_are_decimal_ascii() {
        for (value, expected) in [
            ("8765", Ok(8765)),
            (" 5432\t", Ok(5432)),
            ("\x0b7\x0c", Ok(7)),
            ("\x1c7\x1f", Err(())),
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
            ("\u{0661}", Err(())),
        ] {
            assert_eq!(python_int(value), expected, "{value:?}");
        }
        assert_eq!(python_int(&"9".repeat(5000)), Ok(i128::MAX));
    }

    #[test]
    fn negative_numbers_are_values_and_other_dashes_unknown() {
        assert_eq!(classify("-1"), Class::Value);
        assert_eq!(classify("-.5"), Class::Value);
        assert_eq!(classify("-1.5"), Class::Value);
        assert_eq!(classify("-"), Class::Value);
        assert_eq!(classify("-x y"), Class::Value);
        assert_eq!(classify("-x"), Class::Unknown);
        assert_eq!(classify("-1."), Class::Unknown);
        assert_eq!(classify("--bogus"), Class::Unknown);
        assert_eq!(classify("-hx"), Class::Option("-h", Some("x")));
        assert_eq!(classify("-h=x"), Class::Option("-h", Some("x")));
        assert_eq!(classify("-\u{0661}"), Class::Unknown);
        assert_eq!(classify("--"), Class::End);
        assert_eq!(classify("--back"), Class::Option("--backup-file", None));
        assert_eq!(
            classify("--back=x"),
            Class::Option("--backup-file", Some("x"))
        );
    }

    #[test]
    fn ambiguity_is_raised_before_any_action() {
        assert_eq!(
            message(&["--db-port", "abc", "--d", "x"]),
            "ambiguous option: --d could match --daemon-container, --db, --db-port, --daemon-url"
        );
        assert_eq!(
            message(&["--da=1"]),
            "ambiguous option: --da=1 could match --daemon-container, --daemon-url"
        );
    }

    #[test]
    fn errors_follow_argparse_order() {
        assert_eq!(
            message(&["--db-port", "abc", "--bogus"]),
            "argument --db-port: invalid int value: 'abc'"
        );
        assert_eq!(
            message(&["--bogus"]),
            "the following arguments are required: --backup-file"
        );
        assert_eq!(
            message(&["--backup-file", "x", "--bogus", "y"]),
            "unrecognized arguments: --bogus y"
        );
        assert_eq!(
            message(&["--backup-file", "--yes"]),
            "argument --backup-file: expected one argument"
        );
        assert_eq!(
            message(&["--yes=1"]),
            "argument --yes: ignored explicit argument '1'"
        );
        assert_eq!(
            message(&["--help=", "--backup-file", "x"]),
            "argument -h/--help: ignored explicit argument ''"
        );
        assert_eq!(message(&["--db-port", "1", "-h", "--bogus"]), "<help>");
        assert_eq!(
            message(&["--db-port", "x", "-h"]),
            "argument --db-port: invalid int value: 'x'"
        );
        assert_eq!(message(&["--backup-file", "-1"]), "<args>");
        assert_eq!(
            message(&["--backup-file", "x", "--bogus", "\u{e9}"]),
            "unrecognized arguments: --bogus \u{e9}"
        );
        assert_eq!(
            message(&["--backup-file", "x", "--", "--yes"]),
            "unrecognized arguments: --yes"
        );
        assert_eq!(message(&["--back", "x", "--ye"]), "<args>");
        assert_eq!(message(&["-hx"]), "<help>");
        assert_eq!(message(&["-h=x"]), "<help>");
    }

    #[test]
    fn repeated_options_keep_the_last_value() {
        let Parsed::Args(args) = parse_strs(&[
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
    fn usage_is_the_help_paragraph() {
        assert!(
            usage().starts_with("usage: pseudolife-mcp restore [-h] --backup-file BACKUP_FILE\n")
        );
        assert!(usage().ends_with("[--safety-dir SAFETY_DIR]\n"));
    }
}
