//! The pinned leaf uses stock Python 3.11 argparse, including abbreviations.
use std::{ffi::OsString, path::PathBuf};

pub fn columns() -> usize {
    // shutil.get_terminal_size uses positive COLUMNS ahead of its pipe fallback.
    std::env::var("COLUMNS")
        .ok()
        .and_then(|raw| {
            let raw = raw.trim();
            let raw = raw.strip_prefix('+').unwrap_or(raw);
            let digits = raw
                .chars()
                .map(|c| decimal(c).map_or(c, |d| char::from(b'0' + d as u8)))
                .collect::<String>();
            if digits.as_bytes().iter().enumerate().any(|(i, c)| {
                *c == b'_'
                    && (i == 0
                        || i + 1 == digits.len()
                        || !digits.as_bytes()[i - 1].is_ascii_digit()
                        || !digits.as_bytes()[i + 1].is_ascii_digit())
            }) {
                return None;
            }
            digits
                .replace('_', "")
                .parse::<usize>()
                .ok()
                .filter(|n| *n > 0)
        })
        .unwrap_or(80)
}

pub fn usage(columns: usize) -> String {
    const PROG: &str = "pseudolife-mcp wait-mail";
    let options = [
        "[-h]",
        "[--session-id SESSION_ID | --digest DIGEST]",
        "[--timeout TIMEOUT]",
        "[--interval INTERVAL]",
    ];
    let width = columns.saturating_sub(2);
    let full = format!("usage: {PROG} {}", options.join(" "));
    if full.len() <= width {
        return full + "\n";
    }
    let short_prog = 4 * (7 + PROG.len()) <= 3 * width;
    let indent = if short_prog { 8 + PROG.len() } else { 7 };
    let mut line = if short_prog {
        format!("usage: {PROG}")
    } else {
        " ".repeat(indent)
    };
    let mut result = if short_prog {
        String::new()
    } else {
        format!("usage: {PROG}\n")
    };
    let mut has_part = short_prog;
    for part in options {
        if has_part && line.len() + 1 + part.len() > width {
            result.push_str(&line);
            result.push('\n');
            line = " ".repeat(indent);
            has_part = false;
        }
        if has_part {
            line.push(' ');
        }
        line.push_str(part);
        has_part = true;
    }
    result + &line + "\n"
}

fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut result = Vec::new();
    let mut line = String::new();
    for mut word in text.split_ascii_whitespace() {
        if !line.is_empty() && line.len() + 1 + word.len() <= width {
            line.push(' ');
            line.push_str(word);
            continue;
        }
        if word.len() <= width {
            if !line.is_empty() {
                result.push(std::mem::take(&mut line));
            }
            line.push_str(word);
            continue;
        }
        if !line.is_empty() && line.len() < width {
            line.push(' ');
        }
        while word.len() > width - line.len() {
            let count = width - line.len();
            line.push_str(&word[..count]);
            word = &word[count..];
            result.push(std::mem::take(&mut line));
        }
        line.push_str(word);
    }
    if !line.is_empty() {
        result.push(line);
    }
    result
}

pub fn help(columns: usize) -> String {
    let width = columns.saturating_sub(2);
    let mut result = usage(columns) + "\n";
    result.push_str(&wrap("Wait until the daemon rings this session for addressed mail (plain mail never wakes it), print the mail, exit. Exit 0: a ring (the mail on stdout); 3: timeout; 2: nothing to wait on.", width.max(11)).join("\n"));
    result.push_str("\n\noptions:\n");
    let position = 24.min(width.saturating_sub(20).max(4));
    let action_width = position - 4;
    let help_width = width.saturating_sub(position).max(11);
    for (label, text) in [
        ("-h, --help", "show this help message and exit"),
        (
            "--session-id SESSION_ID",
            "host session id keying the digest, e.g. a Codex thread id (default: CLAUDE_CODE_SESSION_ID)",
        ),
        (
            "--digest DIGEST",
            "coordination digest file (<64 hex digits>.txt) to watch instead",
        ),
        (
            "--timeout TIMEOUT",
            "seconds to wait, at most 86400 (default 14400)",
        ),
        (
            "--interval INTERVAL",
            "seconds between checks, 0.01 to 60 (default 2)",
        ),
    ] {
        result.push_str("  ");
        result.push_str(label);
        if label.len() <= action_width {
            result.push_str(&" ".repeat(action_width - label.len() + 2));
        } else {
            result.push('\n');
            result.push_str(&" ".repeat(position));
        }
        result.push_str(&wrap(text, help_width).join(&format!("\n{}", " ".repeat(position))));
        result.push('\n');
    }
    result
}
pub struct Args {
    pub session: Option<OsString>,
    pub digest: Option<PathBuf>,
    pub timeout: f64,
    pub interval: f64,
}
pub enum Parsed {
    Help,
    Args(Args),
}
const OPTIONS: [&str; 5] = [
    "--help",
    "--session-id",
    "--digest",
    "--timeout",
    "--interval",
];

fn negative_number(text: &str) -> bool {
    let Some(number) = text.strip_prefix('-') else {
        return false;
    };
    if !number.is_empty() && number.chars().all(|c| decimal(c).is_some()) {
        return true;
    }
    if let Some((left, right)) = number.split_once('.') {
        return left.chars().all(|c| decimal(c).is_some())
            && !right.is_empty()
            && right.chars().all(|c| decimal(c).is_some());
    }
    false
}
fn optional(value: &OsString) -> bool {
    let text = value.to_string_lossy();
    text.starts_with('-') && text != "-" && !text.contains(' ') && !negative_number(&text)
}

fn explicit_value(value: &OsString) -> OsString {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::{OsStrExt, OsStringExt};
        let bytes = value.as_bytes();
        OsString::from_vec(bytes[bytes.iter().position(|b| *b == b'=').unwrap() + 1..].to_vec())
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::{OsStrExt, OsStringExt};
        let units = value.encode_wide().collect::<Vec<_>>();
        OsString::from_wide(
            &units[units
                .iter()
                .position(|unit| *unit == u16::from(b'='))
                .unwrap()
                + 1..],
        )
    }
}

pub fn parse(values: Vec<OsString>) -> Result<Parsed, String> {
    // argparse classifies optional tokens before running actions such as help.
    for value in &values {
        let raw = super::os_display(value);
        if raw == "--" {
            break;
        }
        let name = raw.split_once('=').map_or(raw.as_str(), |(name, _)| name);
        let matching = OPTIONS
            .iter()
            .filter(|option| name.starts_with("--") && option.starts_with(name))
            .copied()
            .collect::<Vec<_>>();
        if matching.len() > 1 {
            return Err(format!(
                "ambiguous option: {raw} could match {}",
                matching.join(", ")
            ));
        }
    }
    let mut args = Args {
        session: None,
        digest: None,
        timeout: 14400.0,
        interval: 2.0,
    };
    let mut source: Option<usize> = None;
    let mut extras = Vec::new();
    let mut i = 0;
    while i < values.len() {
        let raw = super::os_display(&values[i]);
        if raw == "--" {
            extras.extend(values[i..].iter().map(super::os_display));
            break;
        }
        let (name, explicit) = raw
            .split_once('=')
            .map_or((raw.as_str(), None), |(n, v)| (n, Some(v)));
        let (option, explicit) =
            if name == "-h" || name.starts_with("-h") && !name.starts_with("--") {
                (
                    Some(0),
                    // Short clusters run the help action before the tail.
                    if name.len() > 2 { None } else { explicit },
                )
            } else {
                let matching = OPTIONS
                    .iter()
                    .enumerate()
                    .filter(|(_, option)| option.starts_with(name) && name.starts_with("--"))
                    .map(|(i, _)| i)
                    .collect::<Vec<_>>();
                if matching.len() > 1 {
                    return Err(format!(
                        "ambiguous option: {raw} could match {}",
                        matching
                            .iter()
                            .map(|i| OPTIONS[*i])
                            .collect::<Vec<_>>()
                            .join(", ")
                    ));
                }
                (matching.first().copied(), explicit)
            };
        let Some(option) = option else {
            extras.push(raw);
            i += 1;
            continue;
        };
        let label = if option == 0 {
            "-h/--help"
        } else {
            OPTIONS[option]
        };
        if option == 0 {
            if explicit.is_some() {
                return Err(format!(
                    "argument {label}: ignored explicit argument {}",
                    super::os_repr(&explicit_value(&values[i]))
                ));
            }
            return Ok(Parsed::Help);
        }
        let value = if explicit.is_some() {
            explicit_value(&values[i])
        } else {
            i += 1;
            if i == values.len() || values[i] == "--" || optional(&values[i]) {
                return Err(format!("argument {label}: expected one argument"));
            }
            values[i].clone()
        };
        let number = if option >= 3 {
            Some(python_float(&value.to_string_lossy()).ok_or_else(|| {
                format!(
                    "argument {label}: invalid float value: {}",
                    super::os_repr(&value)
                )
            })?)
        } else {
            None
        };
        if option <= 2 {
            if let Some(previous) = source
                && previous != option
            {
                return Err(format!(
                    "argument {label}: not allowed with argument {}",
                    OPTIONS[previous]
                ));
            }
            source = Some(option);
        }
        match option {
            1 => args.session = Some(value),
            2 => args.digest = Some(PathBuf::from(value).components().collect()),
            3 => args.timeout = number.unwrap(),
            4 => args.interval = number.unwrap(),
            _ => unreachable!(),
        }
        i += 1;
    }
    if !extras.is_empty() {
        return Err(format!("unrecognized arguments: {}", extras.join(" ")));
    }
    if !(0.0 < args.timeout && args.timeout <= 86400.0) {
        return Err("--timeout must be more than 0 and at most 86400 seconds".into());
    }
    if !(0.01 <= args.interval && args.interval <= 60.0) {
        return Err("--interval must be from 0.01 to 60 seconds".into());
    }
    if let Some(path) = &args.digest
        && !path
            .file_name()
            .and_then(|v| v.to_str())
            .is_some_and(|name| {
                name.len() == 68
                    && name.ends_with(".txt")
                    && name.as_bytes()[..64]
                        .iter()
                        .copied()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
    {
        return Err("--digest must name a coordination digest file (<64 hex digits>.txt)".into());
    }
    Ok(Parsed::Args(args))
}

fn decimal(c: char) -> Option<u32> {
    // Unicode 14 decimal-zero table also used by the pinned runtime-name parser.
    const ZEROS: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x16a60, 0x16ac0,
        0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e950, 0x1fbf0,
    ];
    ZEROS
        .iter()
        .find_map(|zero| (c as u32).checked_sub(*zero).filter(|n| *n < 10))
}
fn python_float(value: &str) -> Option<f64> {
    let text = value.trim_matches(char::is_whitespace);
    let text = text
        .chars()
        .map(|c| decimal(c).map_or(c, |n| char::from(b'0' + n as u8)))
        .collect::<String>();
    let bytes = text.as_bytes();
    if bytes.iter().enumerate().any(|(i, b)| {
        *b == b'_'
            && (i == 0
                || i + 1 == bytes.len()
                || !bytes[i - 1].is_ascii_digit()
                || !bytes[i + 1].is_ascii_digit())
    }) {
        return None;
    }
    let text = text.replace('_', "").to_ascii_lowercase();
    let unsigned = text.strip_prefix(['+', '-']).unwrap_or(&text);
    if matches!(unsigned, "nan" | "inf" | "infinity") {
        let value = if unsigned == "nan" {
            f64::NAN
        } else {
            f64::INFINITY
        };
        return Some(if text.starts_with('-') { -value } else { value });
    }
    if unsigned.is_empty()
        || !unsigned
            .bytes()
            .all(|b| b.is_ascii_digit() || b".e+-".contains(&b))
    {
        return None;
    }
    text.parse().ok()
}

pub fn general(value: f64) -> String {
    let scientific = format!("{value:.5e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap();
    let exponent = exponent.parse::<i32>().unwrap();
    if !(-4..6).contains(&exponent) {
        format!(
            "{}e{exponent:+03}",
            mantissa.trim_end_matches('0').trim_end_matches('.')
        )
    } else {
        let text = format!("{:.*}", (5 - exponent).max(0) as usize, value);
        if text.contains('.') {
            text.trim_end_matches('0').trim_end_matches('.').into()
        } else {
            text
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn formatter_default_and_group_wrapping_match_pinned_argparse() {
        assert_eq!(help(80), super::super::HELP);
        assert_eq!(
            usage(40),
            "usage: pseudolife-mcp wait-mail\n       [-h]\n       [--session-id SESSION_ID | --digest DIGEST]\n       [--timeout TIMEOUT]\n       [--interval INTERVAL]\n"
        );
        assert!(help(120).contains("  --session-id SESSION_ID\n                        host session id keying the digest, e.g. a Codex thread id (default: CLAUDE_CODE_SESSION_ID)\n"));
    }
    #[test]
    fn formatter_description_minimum_does_not_clamp_usage() {
        let boundary = help(13);
        let description = boundary.split("\n\n").nth(1).unwrap();
        assert!(description.contains("session for\naddressed\nmail (plain\n"));
        for columns in [1, 2, 3, 7, 12] {
            let rendered = help(columns);
            assert_eq!(rendered.split("\n\n").nth(1).unwrap(), description);
            assert!(rendered.starts_with(&usage(columns)));
        }
        assert_eq!(
            usage(12),
            "usage: pseudolife-mcp wait-mail\n       [-h]\n       [--session-id SESSION_ID | --digest DIGEST]\n       [--timeout TIMEOUT]\n       [--interval INTERVAL]\n"
        );
    }
    #[test]
    fn float_spellings_and_argparse_option_boundaries() {
        assert_eq!(python_float(" ٠.٠٢٥ "), Some(0.025));
        assert_eq!(python_float("1_0e-3"), Some(0.01));
        assert!(python_float("1__0").is_none());
        assert_eq!(general(0.000012), "1.2e-05");
        assert_eq!(general(0.025), "0.025");
        assert_eq!(
            parse(vec!["--timeout".into(), "-1e2".into()])
                .err()
                .unwrap(),
            "argument --timeout: expected one argument"
        );
    }
    #[test]
    fn unicode_negative_option_and_ascii_control_space_match_argparse() {
        assert_eq!(
            parse(vec!["--timeout".into(), "-٠.١".into()])
                .err()
                .unwrap(),
            "--timeout must be more than 0 and at most 86400 seconds"
        );
        assert!(python_float("\u{1c}0.025").is_none());
        assert!(
            parse(vec![
                "--digest".into(),
                format!("{}.txt", "é".repeat(32)).into()
            ])
            .is_err()
        );
    }
}
