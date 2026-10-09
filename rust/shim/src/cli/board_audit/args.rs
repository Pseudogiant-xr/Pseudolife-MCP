//! Canonical board-audit argv only. Abbreviations, `--opt=value`, repeated
//! options, `-`-prefixed values, help and every other argparse rule defer.
use std::ffi::OsString;

pub(super) type Head = (i64, String);

pub(super) enum Action {
    VerifyInput { input: String, expect: Option<Head> },
    VerifyBank { expect: Option<Head> },
    Export(Export),
    Redact { message_id: String, reason: String },
}

pub(super) struct Export {
    pub project: Option<String>,
    pub task: Option<String>,
    pub agent: Option<String>,
    pub since: Option<f64>,
    pub until: Option<f64>,
    pub out: Option<String>,
}

/// `None` is a deferral: the shape is outside the native candidate.
pub(super) fn parse(arguments: &[OsString]) -> Option<Action> {
    let (action, rest) = arguments.split_first()?;
    let action = action.to_str()?;
    let allowed: &[&str] = match action {
        "verify" => &["--input", "--expect-head"],
        "export" => &[
            "--project",
            "--task",
            "--agent",
            "--since",
            "--until",
            "--out",
        ],
        "redact" => &["--message-id", "--reason"],
        _ => return None,
    };
    if rest.len() % 2 != 0 {
        return None;
    }
    let mut seen: Vec<(&str, &str)> = Vec::new();
    for pair in rest.chunks(2) {
        let option = pair[0].to_str()?;
        let value = pair[1].to_str()?;
        if !allowed.contains(&option) || seen.iter().any(|(name, _)| *name == option) {
            return None;
        }
        seen.push((option, value));
    }
    let get = |name: &str| seen.iter().find(|(key, _)| *key == name).map(|(_, v)| *v);
    match action {
        "verify" => {
            let expect = optional(get("--expect-head"), head)?;
            match get("--input") {
                Some(input) if path_value(input) => Some(Action::VerifyInput {
                    input: input.to_owned(),
                    expect,
                }),
                Some(_) => None,
                None => Some(Action::VerifyBank { expect }),
            }
        }
        "export" => Some(Action::Export(Export {
            project: optional(get("--project"), text_value)?,
            task: optional(get("--task"), text_value)?,
            agent: optional(get("--agent"), text_value)?,
            since: optional(get("--since"), time)?,
            until: optional(get("--until"), time)?,
            out: match get("--out") {
                Some(out) if path_value(out) => Some(out.to_owned()),
                Some(_) => return None,
                None => None,
            },
        })),
        _ => Some(Action::Redact {
            message_id: text_value(get("--message-id")?)?,
            reason: text_value(get("--reason")?)?,
        }),
    }
}

/// An absent option, or a present one whose value admits (`None` defers).
fn optional<T>(value: Option<&str>, admit: fn(&str) -> Option<T>) -> Option<Option<T>> {
    match value {
        None => Some(None),
        Some(value) => admit(value).map(Some),
    }
}

/// argparse reads a `-`-prefixed token as an option (or a negative number);
/// none of those spellings is ported.
fn text_value(value: &str) -> Option<String> {
    (!value.starts_with('-')).then(|| value.to_owned())
}

/// A file argument that `pathlib.Path` prints back unchanged, so diagnostics
/// naming it are byte-equal: no `.` component, no repeated or trailing
/// separator and, on Windows, no `/` (pathlib rewrites it to `\`).
fn path_value(value: &str) -> bool {
    if value.is_empty() || (value.starts_with('-') && value != "-") {
        return false;
    }
    let separators: &[char] = if cfg!(windows) { &['\\'] } else { &['/'] };
    if cfg!(windows) && value.contains('/') {
        return false;
    }
    let mut parts = value.split(separators);
    let first = parts.next().unwrap_or_default();
    let rest: Vec<_> = parts.collect();
    if first == "." {
        return false;
    }
    // A leading separator (an absolute POSIX or drive-rooted path) is fine
    // once; every later component must be a real name.
    rest.iter().all(|part| !part.is_empty() && *part != ".")
}

/// `SEQ:HASH` as verify prints them: a positive decimal without leading zeros.
fn head(value: &str) -> Option<Head> {
    let (seq, digest) = value.split_once(':')?;
    if seq.is_empty()
        || seq.starts_with('0')
        || seq.len() > 18
        || !seq.bytes().all(|c| c.is_ascii_digit())
        || digest.len() != 64
        || !digest
            .bytes()
            .all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'))
    {
        return None;
    }
    Some((seq.parse().ok()?, digest.to_owned()))
}

/// Epoch seconds as plain decimal digits, or an ISO 8601 date-time with an
/// explicit `+HH:MM`/`-HH:MM` offset (never a local-time reading).
fn time(value: &str) -> Option<f64> {
    let decimal = |text: &str| !text.is_empty() && text.bytes().all(|c| c.is_ascii_digit());
    if let Some((whole, fraction)) = value.split_once('.') {
        return (decimal(whole) && decimal(fraction))
            .then(|| value.parse::<f64>().ok())
            .flatten()
            .filter(|v| v.is_finite());
    }
    if decimal(value) {
        return value.parse::<f64>().ok().filter(|v| v.is_finite());
    }
    iso(value)
}

fn iso(value: &str) -> Option<f64> {
    let bytes = value.as_bytes();
    if bytes.len() != 25 {
        return None;
    }
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
    Some((days * 86400 + hour * 3600 + minute * 60 + second - offset) as f64)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(items: &[&str]) -> Vec<OsString> {
        items.iter().map(OsString::from).collect()
    }

    #[test]
    fn canonical_shapes_and_deferrals() {
        assert!(matches!(
            parse(&argv(&["verify"])),
            Some(Action::VerifyBank { expect: None })
        ));
        let head = format!("12:{}", "a".repeat(64));
        assert!(matches!(
            parse(&argv(&[
                "verify",
                "--expect-head",
                &head,
                "--input",
                "x.jsonl"
            ])),
            Some(Action::VerifyInput {
                expect: Some((12, _)),
                ..
            })
        ));
        for deferred in [
            vec!["verify", "--input"],
            vec!["verify", "--input", "a", "--input", "b"],
            vec!["verify", "--input=a"],
            vec!["verify", "--in", "a"],
            vec!["verify", "--expect-head", "012:x"],
            vec!["export", "--since", "-5"],
            vec!["export", "--since", "1e3"],
            vec!["export", "--since", " 5"],
            vec!["export", "--since", "2026-01-01T00:00:00"],
            vec!["export", "--agent", "-x"],
            vec!["redact", "--message-id", "a"],
            vec!["stats"],
            vec!["--help"],
        ] {
            assert!(parse(&argv(&deferred)).is_none(), "{deferred:?}");
        }
    }

    #[test]
    fn iso_offsets_are_absolute() {
        assert_eq!(time("1970-01-01T00:33:20+00:00"), Some(2000.0));
        assert_eq!(time("1970-01-01T01:33:20+01:00"), Some(2000.0));
        assert_eq!(time("2024-02-29T00:00:00-00:30"), Some(1709166600.0));
        assert_eq!(time("2023-02-29T00:00:00+00:00"), None);
        assert_eq!(time("2000.5"), Some(2000.5));
    }
}
