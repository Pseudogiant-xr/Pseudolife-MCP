//! The audit command's argparse option grammar and pathlib display spelling.
use super::super::lease::args::decimal_digit;
use std::ffi::OsString;

pub(super) type Head = (String, String);

pub(super) enum Action {
    VerifyInput { input: String, expect: Option<Head> },
    VerifyBank { expect: Option<Head> },
    Export(Export),
    Redact { message_id: String, reason: String },
    Error(String),
    Help(&'static str),
}

pub(super) struct Export {
    pub project: Option<String>,
    pub task: Option<String>,
    pub agent: Option<String>,
    pub since: Option<f64>,
    pub until: Option<f64>,
    pub out: Option<String>,
}

fn usage(action: &str) -> String {
    format!(
        "{}\n",
        super::help::text(action)
            .split("\n\n")
            .next()
            .expect("usage")
    )
}

fn help(action: &str, token: &str) -> Option<Action> {
    // A short -h cluster invokes help before consuming its remaining characters.
    if token.starts_with("-h") && !token.starts_with("--") && !token.starts_with("-h=") {
        return Some(Action::Help(super::help::text(action)));
    }
    if let Some((_, value)) = token.split_once('=') {
        return error(
            action,
            format!(
                "argument -h/--help: ignored explicit argument {}",
                super::super::pyjson::py_value_repr(&super::super::pyjson::J::Str(value.into()))
            ),
        );
    }
    Some(Action::Help(super::help::text(action)))
}

fn error(action: &str, message: impl std::fmt::Display) -> Option<Action> {
    let suffix = if action.is_empty() {
        String::new()
    } else {
        format!(" {action}")
    };
    Some(Action::Error(format!(
        "{}pseudolife-mcp board-audit{suffix}: error: {message}",
        usage(action)
    )))
}

/// `None` retains the separately deferred stats/transport domains.
pub(super) fn parse(arguments: &[OsString]) -> Option<Action> {
    let tokens = arguments
        .iter()
        .map(|v| v.to_str())
        .collect::<Option<Vec<_>>>()?;
    let mut root_unknown = Vec::new();
    let mut action_index = 0;
    while let Some(token) = tokens.get(action_index) {
        if *token == "--" {
            break;
        }
        let option = token.split_once('=').map_or(*token, |(key, _)| key);
        if option == "-h"
            || option.starts_with("-h") && !option.starts_with("--")
            || option.starts_with("--") && "--help".starts_with(option)
        {
            return help("", token);
        }
        if !option_token(token, &[]) || *token == "--" {
            break;
        }
        root_unknown.push(*token);
        action_index += 1;
    }
    let Some(action) = tokens.get(action_index).copied() else {
        return error("", "the following arguments are required: action");
    };
    let rest = &tokens[action_index + 1..];
    let allowed: &[&str] = match action {
        "verify" => &["--expect-head", "--input"],
        "export" => &[
            "--project",
            "--task",
            "--agent",
            "--since",
            "--until",
            "--out",
        ],
        "redact" => &["--message-id", "--reason"],
        "stats" => &[
            "--since",
            "--until",
            "--input",
            "--durations",
            "--out",
            "--append",
        ],
        _ => {
            return error(
                "",
                format!(
                    "argument action: invalid choice: {} (choose from 'export', 'verify', 'redact', 'stats')",
                    super::super::pyjson::py_value_repr(&super::super::pyjson::J::Str(
                        action.into()
                    ))
                ),
            );
        }
    };
    let mut seen: Vec<(&str, &str)> = Vec::new();
    let mut unknown = root_unknown;
    let mut index = 0;
    while index < rest.len() {
        let token = rest[index];
        index += 1;
        if token == "--" {
            unknown.extend_from_slice(&rest[index - 1..]);
            break;
        }
        let (option, joined) = token
            .split_once('=')
            .map_or((token, None), |(k, v)| (k, Some(v)));
        let mut matches: Vec<_> = allowed.iter().copied().filter(|v| *v == option).collect();
        if matches.is_empty() && option.starts_with("--") {
            matches = allowed
                .iter()
                .copied()
                .filter(|v| v.starts_with(option))
                .collect();
        }
        if option == "-h"
            || option.starts_with("-h") && !option.starts_with("--")
            || option.starts_with("--") && "--help".starts_with(option)
        {
            return help(action, token);
        }
        if matches.len() > 1 {
            return error(
                action,
                format!(
                    "ambiguous option: {option} could match {}",
                    matches.join(", ")
                ),
            );
        }
        let Some(name) = matches.first().copied() else {
            unknown.push(token);
            continue;
        };
        let value = match joined {
            Some(value) => value,
            None => match rest
                .get(index)
                .copied()
                .filter(|v| !option_token(v, allowed))
            {
                Some(value) => {
                    index += 1;
                    value
                }
                None => return error(action, format!("argument {name}: expected one argument")),
            },
        };
        // argparse converts every occurrence, including a value overwritten later.
        if name == "--expect-head"
            && let Err(message) = head(value)
        {
            return error(action, format!("argument --expect-head: {message}"));
        }
        if matches!(name, "--since" | "--until") && time(value).is_none() {
            if epoch(value).is_some() {
                return error(
                    action,
                    format!("argument {name}: expected epoch seconds or an ISO 8601 date/time"),
                );
            }
            // Python admits more ISO spellings than the native converter.
            // Defer every non-numeric failure, including overwritten options.
            return None;
        }
        if let Some(slot) = seen.iter_mut().find(|(key, _)| *key == name) {
            slot.1 = value;
        } else {
            seen.push((name, value));
        }
    }
    let get = |name: &str| seen.iter().find(|(key, _)| *key == name).map(|(_, v)| *v);
    if action == "redact" {
        let missing: Vec<_> = allowed
            .iter()
            .copied()
            .filter(|name| get(name).is_none())
            .collect();
        if !missing.is_empty() {
            return error(
                action,
                format!(
                    "the following arguments are required: {}",
                    missing.join(", ")
                ),
            );
        }
    }
    if !unknown.is_empty() {
        return error("", format!("unrecognized arguments: {}", unknown.join(" ")));
    }
    if ["--since", "--until"]
        .iter()
        .any(|name| get(name).is_some_and(|value| time(value).is_none()))
    {
        return None;
    }
    match action {
        "verify" => {
            let expect = get("--expect-head").and_then(|value| head(value).ok());
            match get("--input").filter(|v| !v.is_empty()) {
                Some(input) => Some(Action::VerifyInput {
                    input: path(input),
                    expect,
                }),
                None => Some(Action::VerifyBank { expect }),
            }
        }
        "export" => Some(Action::Export(Export {
            project: get("--project").map(str::to_owned),
            task: get("--task").map(str::to_owned),
            agent: get("--agent").map(str::to_owned),
            since: get("--since").and_then(time),
            until: get("--until").and_then(time),
            out: get("--out").filter(|v| !v.is_empty()).map(path),
        })),
        "stats" => None,
        _ => Some(Action::Redact {
            message_id: get("--message-id")?.to_owned(),
            reason: get("--reason")?.to_owned(),
        }),
    }
}

fn option_token(value: &str, allowed: &[&str]) -> bool {
    if !value.starts_with('-') || value == "-" {
        return false;
    }
    let option = value.split_once('=').map_or(value, |(key, _)| key);
    if value.starts_with("-h")
        || option.starts_with("--")
            && allowed
                .iter()
                .copied()
                .chain(["--help"])
                .any(|name| name.starts_with(option))
    {
        return true;
    }
    // argparse's negative-number regex uses Unicode digits and `$` permits
    // one final newline; unknown tokens containing a space are positional.
    let number = value[1..].strip_suffix('\n').unwrap_or(&value[1..]);
    let digits = |v: &str| v.chars().all(|c| decimal_digit(c).is_some());
    let negative = !number.is_empty() && digits(number)
        || number
            .split_once('.')
            .is_some_and(|(a, b)| digits(a) && !b.is_empty() && digits(b));
    !negative && !value.contains(' ')
}

/// Path's lexical normalization; it never resolves symlinks or `..`.
pub(super) fn path(value: &str) -> String {
    if cfg!(windows) {
        let value = value.replace('/', "\\");
        let normalized: std::path::PathBuf = std::path::Path::new(&value)
            .components()
            .filter(|component| *component != std::path::Component::CurDir)
            .collect();
        return if normalized.as_os_str().is_empty() {
            ".".into()
        } else {
            normalized.to_string_lossy().into_owned()
        };
    }
    let root = if value.starts_with("//") && !value.starts_with("///") {
        "//"
    } else if value.starts_with('/') {
        "/"
    } else {
        ""
    };
    let parts: Vec<_> = value
        .split('/')
        .filter(|v| !v.is_empty() && *v != ".")
        .collect();
    let result = format!("{root}{}", parts.join("/"));
    if result.is_empty() {
        ".".into()
    } else {
        result
    }
}

fn head(value: &str) -> Result<Head, String> {
    let shape_error = || "expected SEQ:HASH as verify prints them".to_owned();
    let (seq, digest) = value.split_once(':').ok_or_else(shape_error)?;
    if seq.is_empty() || !seq.chars().all(python_digit) {
        return Err(shape_error());
    }
    if seq.chars().count() > 4300 || seq.chars().any(|c| decimal_digit(c).is_none()) {
        return Err(format!(
            "invalid _head value: {}",
            super::super::pyjson::py_value_repr(&super::super::pyjson::J::Str(value.into()))
        ));
    }
    let seq: String = seq
        .chars()
        .map(|c| decimal_digit(c).and_then(|digit| char::from_digit(digit, 10)))
        .collect::<Option<_>>()
        .expect("decimal digits checked");
    if seq.is_empty()
        || seq.bytes().all(|c| c == b'0')
        || digest.len() != 64
        || !digest.bytes().all(|c| matches!(c,b'0'..=b'9'|b'a'..=b'f'))
    {
        return Err(shape_error());
    }
    Ok((seq.trim_start_matches('0').to_owned(), digest.to_owned()))
}

// CPython 3.11 str.isdigit includes these Unicode 14 digit characters
// in addition to decimal digits; int() rejects them after that shape check.
fn python_digit(c: char) -> bool {
    decimal_digit(c).is_some()
        || matches!(c as u32,
        178..=179 | 185 | 4969..=4977 | 6618 | 8304 | 8308..=8313 |
        8320..=8329 | 9312..=9320 | 9332..=9340 | 9352..=9360 | 9450 |
        9461..=9469 | 9471 | 10102..=10110 | 10112..=10120 | 10122..=10130 |
        68160..=68163 | 69216..=69224 | 69714..=69722 | 127232..=127242)
}

fn time(value: &str) -> Option<f64> {
    match epoch(value) {
        Some(number) => number.is_finite().then_some(number),
        None => iso(value),
    }
}

fn epoch(value: &str) -> Option<f64> {
    let chars: Vec<_> = value.trim().chars().collect();
    let mut normalized = String::new();
    for (index, c) in chars.iter().copied().enumerate() {
        if c == '_' {
            if index == 0
                || index + 1 == chars.len()
                || decimal_digit(chars[index - 1]).is_none()
                || decimal_digit(chars[index + 1]).is_none()
            {
                return None;
            }
        } else {
            normalized.push(
                decimal_digit(c)
                    .and_then(|digit| char::from_digit(digit, 10))
                    .unwrap_or(c),
            );
        }
    }
    normalized.parse().ok()
}

fn iso(value: &str) -> Option<f64> {
    let offset_start = value.len().checked_sub(6)?;
    let fraction = value.get(19..offset_start)?;
    let microseconds = if fraction.is_empty() {
        0
    } else {
        let digits = fraction.strip_prefix('.')?;
        if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        // datetime.fromisoformat truncates sub-microsecond digits.
        digits
            .bytes()
            .take(6)
            .chain(std::iter::repeat(b'0'))
            .take(6)
            .fold(0, |value, byte| value * 10 + i64::from(byte - b'0'))
    };
    let whole = format!("{}{}", value.get(..19)?, value.get(offset_start..)?);
    let value = whole.as_str();
    let bytes = value.as_bytes();
    let shape = b"dddd-dd-ddTdd:dd:dd+dd:dd";
    for (index, (byte, want)) in bytes.iter().zip(shape).enumerate() {
        let ok = match want {
            b'd' => byte.is_ascii_digit(),
            b'+' => matches!(byte, b'+' | b'-'),
            other => byte == other,
        };
        if !ok || (index == 19 && !matches!(byte, b'+' | b'-')) {
            return None;
        }
    }
    let number = |range: std::ops::Range<usize>| value[range].parse::<i64>().ok();
    let (year, month, day) = (number(0..4)?, number(5..7)?, number(8..10)?);
    let (hour, minute, second) = (number(11..13)?, number(14..16)?, number(17..19)?);
    let (offset_hours, offset_minutes) = (number(20..22)?, number(23..25)?);
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let days_in_month = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => return None,
    };
    if year < 1
        || day < 1
        || day > days_in_month
        || hour > 23
        || minute > 59
        || second > 59
        || offset_hours > 23
        || offset_minutes > 59
    {
        return None;
    }
    // Howard Hinnant's days_from_civil.
    let shifted = if month <= 2 { year - 1 } else { year };
    let era = shifted.div_euclid(400);
    let year_of_era = shifted - era * 400;
    let month_index = (month + 9) % 12;
    let day_of_year = (153 * month_index + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146097 + day_of_era - 719468;
    let sign = if bytes[19] == b'-' { -1 } else { 1 };
    let offset = sign * (offset_hours * 3600 + offset_minutes * 60);
    let seconds = days * 86400 + hour * 3600 + minute * 60 + second - offset;
    // Python divides total integer microseconds. Decimal parsing rounds that
    // ratio once, including dates whose microseconds exceed exact f64 integers.
    let total_microseconds = seconds * 1_000_000 + microseconds;
    format!("{total_microseconds}e-6").parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    fn argv(items: &[&str]) -> Vec<OsString> {
        items.iter().map(OsString::from).collect()
    }
    #[test]
    fn joined_abbreviated_and_repeated_options() {
        assert!(
            matches!(parse(&argv(&["verify", "--input=bad", "--in", "./good//"])),
            Some(Action::VerifyInput { input, .. }) if input == "good")
        );
        assert!(matches!(
            parse(&argv(&["verify", "--input"])),
            Some(Action::Error(_))
        ));
        let digest = "a".repeat(64);
        assert_eq!(head(&format!("00012:{digest}")), Ok(("12".into(), digest)));
    }
    #[test]
    fn pathlib_paths_keep_parent_components() {
        if cfg!(windows) {
            assert_eq!(path("./missing//archive.jsonl/"), "missing\\archive.jsonl");
        } else {
            assert_eq!(path("./a//../b/"), "a/../b");
            assert_eq!(path("//a/./b"), "//a/b");
            assert_eq!(path("///a/"), "/a");
        }
        assert_eq!(time("-5"), Some(-5.0));
        assert_eq!(time("1.7e9"), Some(1.7e9));
        assert_eq!(time("1970-01-01T00:00:59.999999+00:01"), Some(-0.000001));
    }
}
