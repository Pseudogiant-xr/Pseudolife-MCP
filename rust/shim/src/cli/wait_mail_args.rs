//! Native CLI options and ASCII numeric inputs for wait-mail.
use std::{
    ffi::OsString,
    path::{Component, Path, PathBuf},
};

pub fn columns() -> usize {
    std::env::var("COLUMNS")
        .ok()
        .and_then(|raw| columns_value(&raw))
        .unwrap_or(80)
}
fn columns_value(raw: &str) -> Option<usize> {
    if raw.is_empty() || !raw.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    raw.parse().ok().filter(|value| *value > 0)
}
fn wrap(text: &str, width: usize) -> Vec<String> {
    let mut lines = Vec::new();
    let mut line = String::new();
    for word in text.split_ascii_whitespace() {
        if !line.is_empty() && line.len() + 1 + word.len() > width {
            lines.push(std::mem::take(&mut line));
        }
        if !line.is_empty() {
            line.push(' ');
        }
        line.push_str(word);
    }
    if !line.is_empty() {
        lines.push(line);
    }
    lines
}
pub fn usage(columns: usize) -> String {
    let text = super::HELP.split("\n\n").next().unwrap();
    if columns == 80 {
        text.to_owned() + "\n"
    } else {
        wrap(text, columns).join("\n") + "\n"
    }
}
pub fn help(columns: usize) -> String {
    if columns == 80 {
        return super::HELP.into();
    }
    // Other widths use a simple word wrapper, without argparse's layout
    // thresholds, action groups or splitting words at extreme widths.
    super::HELP
        .lines()
        .map(|line| wrap(line, columns).join("\n"))
        .collect::<Vec<_>>()
        .join("\n")
        + "\n"
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
    if !number.is_empty() && number.bytes().all(|b| b.is_ascii_digit()) {
        return true;
    }
    if let Some((left, right)) = number.split_once('.') {
        return left.bytes().all(|b| b.is_ascii_digit())
            && !right.is_empty()
            && right.bytes().all(|b| b.is_ascii_digit());
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
            2 => args.digest = Some(pathlib(Path::new(&value))),
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

/// pathlib's spelling of a path, as Python's diagnostics print it:
/// components rejoined with the native separator, `.` components dropped
/// (an empty result is `.`), and on POSIX exactly two leading slashes kept.
pub(super) fn pathlib(path: &Path) -> PathBuf {
    let mut spelled: PathBuf = path
        .components()
        .filter(|component| !matches!(component, Component::CurDir))
        .collect();
    if spelled.as_os_str().is_empty() {
        spelled = PathBuf::from(".");
    }
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let raw = path.as_os_str().as_bytes();
        if raw.starts_with(b"//") && !raw.starts_with(b"///") {
            let mut doubled = OsString::from("/");
            doubled.push(spelled.as_os_str());
            spelled = PathBuf::from(doubled);
        }
    }
    spelled
}

fn python_float(value: &str) -> Option<f64> {
    // CLI producers use finite ASCII decimal, optionally with an exponent.
    let original = value;
    let value = value.strip_prefix(['+', '-']).unwrap_or(value);
    let (mantissa, exponent) = value
        .split_once(['e', 'E'])
        .map_or((value, None), |(left, right)| (left, Some(right)));
    let mut dots = 0;
    let mut digits = 0;
    for byte in mantissa.bytes() {
        if byte == b'.' {
            dots += 1;
        } else if byte.is_ascii_digit() {
            digits += 1;
        } else {
            return None;
        }
    }
    if digits == 0 || dots > 1 {
        return None;
    }
    if let Some(exponent) = exponent {
        let exponent = exponent.strip_prefix(['+', '-']).unwrap_or(exponent);
        if exponent.is_empty() || !exponent.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
    }
    // Parse the original sign as well as the validated numeric spelling.
    original
        .parse::<f64>()
        .ok()
        .filter(|number| number.is_finite())
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
    fn formatter_default_help_and_simple_usage_keep_the_declared_text() {
        assert_eq!(help(80), super::super::HELP);
        assert_eq!(
            usage(40),
            "usage: pseudolife-mcp wait-mail [-h]\n[--session-id SESSION_ID | --digest\nDIGEST] [--timeout TIMEOUT] [--interval\nINTERVAL]\n"
        );
        for columns in [40, 120] {
            assert_eq!(
                help(columns).split_ascii_whitespace().collect::<Vec<_>>(),
                super::super::HELP
                    .split_ascii_whitespace()
                    .collect::<Vec<_>>()
            );
        }
    }
    #[test]
    fn small_terminal_widths_keep_whole_words_without_argparse_thresholds() {
        for columns in [1, 2, 3, 7, 12, 13] {
            assert_eq!(
                help(columns).split_ascii_whitespace().collect::<Vec<_>>(),
                super::super::HELP
                    .split_ascii_whitespace()
                    .collect::<Vec<_>>()
            );
            assert!(help(columns).contains("pseudolife-mcp"));
            assert!(help(columns).contains("addressed"));
        }
        assert_eq!(
            usage(1),
            "usage:\npseudolife-mcp\nwait-mail\n[-h]\n[--session-id\nSESSION_ID\n|\n--digest\nDIGEST]\n[--timeout\nTIMEOUT]\n[--interval\nINTERVAL]\n"
        );
    }
    #[test]
    fn float_spellings_and_argparse_option_boundaries() {
        assert!(python_float(" ٠.٠٢٥ ").is_none());
        assert!(python_float("1_0e-3").is_none());
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
            "argument --timeout: expected one argument"
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

#[cfg(test)]
#[path = "wait_mail_args_reduction_tests.rs"]
mod reduction_tests;
