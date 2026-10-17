//! `mcp_server._check_as_of`: an `as_of` the history read can parse
//! (`MemoryService._parse_as_of`): `float(value)`, else
//! `datetime.fromisoformat(str(value)).timestamp()`.
//!
//! `fromisoformat` follows CPython's C implementation
//! (`Modules/_datetimemodule.c`, 3.11/3.12), which runs on UTF-8 bytes.
//! `timestamp()` on a naive date-time converts through local time: CPython's
//! `local_to_seconds` also converts the instant a day earlier, so a naive
//! instant on 0001-01-01 fails in every time zone (measured on 3.12.13,
//! Linux, UTC, New York and Tokyo). Near the end of 9999 the outcome depends
//! on the zone's offset; this follows UTC, the container default (declared,
//! MCP-BIND-AS-OF-PYTHON). Windows refuses naive instants before 1970 with
//! OSError, which `_check_as_of` does not catch.

use chrono::{NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Weekday};
use serde_json::Value;

pub const REFUSAL: &str = "as_of takes an ISO-8601 date or date-time (2026-10-01, \
2026-10-01T12:00:00+00:00) or epoch seconds, not relative words like 'yesterday'";

/// Whether `_parse_as_of` accepts the bound value (`str | float | None`).
pub fn parses(value: &Value) -> bool {
    match value {
        Value::String(s) => {
            py_float(s).is_some()
                || parse(s).is_some_and(|p| {
                    !(p.naive() && (p.year(), p.month(), p.day()) == (1, 1, 1))
                        || crate::mutants::active("mcp-bind-asof-year-one")
                })
        }
        _ => true,
    }
}

/// `MemoryService._parse_as_of` on a bound value that `parses` accepted:
/// `float(value)`, else `datetime.fromisoformat(value).timestamp()` (an
/// aware value at its offset, a naive one in the daemon's local time).
pub fn epoch(value: &Value) -> Option<f64> {
    match value {
        Value::Null => None,
        Value::String(s) => py_float(s).or_else(|| parse(s).and_then(|p| p.timestamp())),
        other => crate::mcp::binding::float_arg(other),
    }
}

/// A parsed `fromisoformat` value.
struct Parsed {
    date: NaiveDate,
    /// Hour, minute, second, microsecond.
    clock: [u32; 4],
    /// The UTC offset in microseconds; None when naive.
    offset_us: Option<i64>,
}

impl Parsed {
    fn naive(&self) -> bool {
        self.offset_us.is_none()
    }

    fn year(&self) -> i32 {
        chrono::Datelike::year(&self.date)
    }

    fn month(&self) -> u32 {
        chrono::Datelike::month(&self.date)
    }

    fn day(&self) -> u32 {
        chrono::Datelike::day(&self.date)
    }

    /// `datetime.timestamp()`: microseconds since the epoch over 10**6,
    /// as CPython's `total_seconds` divides them. A naive value takes the
    /// local offset; at a DST fold the earlier instant (`fold=0`); a time a
    /// spring-forward gap skips is not reproduced (declared,
    /// MCP-BIND-AS-OF-PYTHON).
    fn timestamp(&self) -> Option<f64> {
        let [h, m, s, us] = self.clock;
        let wall = NaiveDateTime::new(self.date, NaiveTime::from_hms_opt(h, m, s)?);
        let seconds = match self.offset_us {
            Some(offset) => {
                let utc_us = wall.and_utc().timestamp() * 1_000_000 + i64::from(us) - offset;
                return Some(utc_us as f64 / 1e6);
            }
            None => chrono::Local
                .from_local_datetime(&wall)
                .earliest()?
                .timestamp(),
        };
        Some((seconds * 1_000_000 + i64::from(us)) as f64 / 1e6)
    }
}

/// Python's `float(str)`: surrounding whitespace, single underscores
/// between digits, `inf` / `infinity` / `nan` in any case. Non-ASCII decimal
/// digits, which Python also reads, are declared (MCP-BIND-AS-OF-DIGITS).
fn py_float(raw: &str) -> Option<f64> {
    let s = crate::http::py_strip(raw);
    if s.contains('_') {
        let b = s.as_bytes();
        let ok = b.iter().enumerate().all(|(i, &c)| {
            c != b'_'
                || (i > 0
                    && b[i - 1].is_ascii_digit()
                    && b.get(i + 1).is_some_and(u8::is_ascii_digit))
        });
        if !ok {
            return None;
        }
        return s.replace('_', "").parse().ok();
    }
    s.parse().ok()
}

/// The byte at `i`, NUL past the end (the C code reads a NUL-terminated
/// buffer).
fn at(b: &[u8], i: usize) -> u8 {
    b.get(i).copied().unwrap_or(0)
}

/// `parse_digits`: `n` ASCII digits starting at `p`.
fn digits(b: &[u8], p: usize, n: usize) -> Option<(u32, usize)> {
    let mut value = 0u32;
    for k in 0..n {
        let c = at(b, p + k);
        if !c.is_ascii_digit() {
            return None;
        }
        value = value * 10 + u32::from(c - b'0');
    }
    Some((value, p + n))
}

/// `_find_isoformat_datetime_separator`; None for its -1.
fn separator(b: &[u8]) -> Option<usize> {
    let len = b.len();
    if len == 7 {
        return Some(7);
    }
    if at(b, 4) == b'-' {
        if at(b, 5) == b'W' {
            if len > 8 && at(b, 8) == b'-' {
                if len == 9 {
                    return None;
                }
                if len > 10 && at(b, 10).is_ascii_digit() {
                    return Some(8);
                }
                return Some(10);
            }
            return Some(8);
        }
        return Some(10);
    }
    if at(b, 4) == b'W' {
        let mut idx = 7;
        while idx < len && at(b, idx).is_ascii_digit() {
            idx += 1;
        }
        if idx < 9 {
            return Some(idx);
        }
        return Some(if idx % 2 == 0 { 7 } else { 8 });
    }
    Some(8)
}

/// `parse_isoformat_date` plus the date constructor's range checks.
fn date(b: &[u8], len: usize) -> Option<NaiveDate> {
    let (year, mut p) = digits(b, 0, 4)?;
    let sep = at(b, p) == b'-';
    if sep {
        p += 1;
    }
    if at(b, p) == b'W' {
        let (week, q) = digits(b, p + 1, 2)?;
        p = q;
        let mut day = 1;
        if p < len {
            if sep {
                if at(b, p) != b'-' {
                    return None;
                }
                p += 1;
            }
            day = digits(b, p, 1)?.0;
        }
        if !(1..=7).contains(&day) {
            return None;
        }
        let weekday = Weekday::try_from(day as u8 - 1).expect("weekday");
        return NaiveDate::from_isoywd_opt(year as i32, week, weekday)
            .filter(|d| (1..=9999).contains(&chrono::Datelike::year(d)));
    }
    let (month, mut p) = digits(b, p, 2)?;
    if sep {
        if at(b, p) != b'-' {
            return None;
        }
        p += 1;
    }
    let (day, _) = digits(b, p, 2)?;
    NaiveDate::from_ymd_opt(year as i32, month, day).filter(|_| (1..=9999).contains(&year))
}

/// `parse_hh_mm_ss_ff` over `[p, end)`: (status, hour, minute, second,
/// microsecond), status 0 at the end, 1 with trailing text, Err refused.
fn hh_mm_ss_ff(b: &[u8], mut p: usize, end: usize) -> Result<(u8, [u32; 4]), ()> {
    let mut vals = [0u32; 4];
    let mut has_sep = true;
    for i in 0..3 {
        let (v, q) = digits(b, p, 2).ok_or(())?;
        vals[i] = v;
        p = q;
        let c = at(b, p);
        p += 1;
        if i == 0 {
            has_sep = c == b':';
        }
        if p >= end {
            return Ok((u8::from(c != 0), vals));
        } else if has_sep && c == b':' {
            continue;
        } else if c == b'.' || c == b',' {
            break;
        } else if !has_sep {
            p -= 1;
        } else {
            return Err(());
        }
    }
    let to_parse = (end - p).min(6);
    // CPython 3.11 refuses a separator with no fraction digits after it.
    if to_parse == 0 {
        return Err(());
    }
    let (mut micro, mut q) = digits(b, p, to_parse).ok_or(())?;
    if (1..6).contains(&to_parse) {
        micro *= [100000, 10000, 1000, 100, 10][to_parse - 1];
    }
    while at(b, q).is_ascii_digit() {
        q += 1;
    }
    vals[3] = micro;
    Ok((u8::from(at(b, q) != 0), vals))
}

/// `parse_isoformat_time` plus the time and timezone constructors' checks:
/// None when refused, else the clock and the offset (None when naive).
fn time(b: &[u8], start: usize) -> Option<([u32; 4], Option<i64>)> {
    let end = b.len();
    let mut tz = start;
    loop {
        if matches!(at(b, tz), b'Z' | b'+' | b'-') {
            break;
        }
        tz += 1;
        if tz >= end {
            break;
        }
    }
    let (rv, parts) = hh_mm_ss_ff(b, start, tz).ok()?;
    let [hour, minute, second, _] = parts;
    let clock = hour <= 23 && minute <= 59 && second <= 59;
    if tz == end {
        return (rv == 0 && clock).then_some((parts, None));
    }
    if at(b, tz) == b'Z' {
        return (at(b, tz + 1) == 0 && clock).then_some((parts, Some(0)));
    }
    let sign = if at(b, tz) == b'-' { -1 } else { 1 };
    let (rv, [h, m, s, us]) = hh_mm_ss_ff(b, tz + 1, end).ok()?;
    if crate::mutants::active("mcp-bind-asof-offset-range") && (h > 23 || m > 59) {
        return None;
    }
    // timezone(): strictly inside one day; the parts are not range-checked.
    let offset_us =
        (u64::from(h) * 3600 + u64::from(m) * 60 + u64::from(s)) * 1_000_000 + u64::from(us);
    (rv == 0 && clock && offset_us < 86_400_000_000)
        .then_some((parts, Some(sign * offset_us as i64)))
}

/// `datetime.fromisoformat(value)`, when it succeeds.
fn parse(value: &str) -> Option<Parsed> {
    if value.chars().count() < 7 {
        return None;
    }
    let b = value.as_bytes();
    let sep = separator(b)?;
    let date = date(b, sep)?;
    let (clock, offset_us) = if b.len() > sep {
        // The separator is any one character, however many UTF-8 bytes.
        let width = value[sep..].chars().next().map_or(1, char::len_utf8);
        time(b, sep + width)?
    } else {
        ([0; 4], None)
    };
    Some(Parsed {
        date,
        clock,
        offset_us,
    })
}

#[cfg(test)]
fn fromisoformat(value: &str) -> bool {
    parse(value).is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_shapes() {
        for ok in [
            "2026-10-01",
            "20261001",
            "2026-W40",
            "2026W40",
            "2026-W40-4",
            "2026W404",
            "2026-10-01T12:00:00+00:00",
            "2026-10-01 12:00",
            "2026-10-01T12",
            "2026-10-01T1230",
            "2026-10-01T12:30:45.5Z",
            "2026-10-01T12:30:45,123456789",
            "2026-10-01T12:30-05:30",
            "2026-10-01T12:30+0530",
            "2026-10-01x12:30",
            "2026-10-01\u{e9}12:00",
            "2026-10-01T12:30:45:67",
        ] {
            assert!(fromisoformat(ok), "{ok}");
        }
        for bad in [
            "yesterday",
            "2026-1001",
            "2026-13-01",
            "2026-02-30",
            "0000-01-01",
            "2026-10-01T",
            "2026-10-01T24:00",
            "2026-10-01T12:30Zx",
            "2026-10-01T12:30+24:00",
            "2026-W54",
            "2026-10-01T12:3045",
            "2026-10-01T12:30:45.",
        ] {
            assert!(!fromisoformat(bad), "{bad}");
        }
    }

    #[test]
    fn naive_instants_on_the_first_day_fail_timestamp() {
        // CPython 3.12.13 on Linux (UTC, New York, Tokyo): ValueError for
        // naive 0001-01-01 at any time; 0001-01-02 and aware values convert.
        for refused in ["0001-01-01", "0001-01-01T12:00", "0001-01-01T23:59:59.999"] {
            assert!(!parses(&Value::String(refused.into())), "{refused}");
        }
        for accepted in [
            "0001-01-02",
            "0001-01-01T00:00:00+00:00",
            "0001-01-01T12:00Z",
        ] {
            assert!(parses(&Value::String(accepted.into())), "{accepted}");
        }
    }

    #[test]
    fn python_float_spellings() {
        assert_eq!(py_float(" 1_000.5 "), Some(1000.5));
        assert_eq!(py_float("1__0"), None);
        assert_eq!(py_float("_1"), None);
        assert!(py_float("-INFINITY").is_some_and(f64::is_infinite));
        assert!(py_float("nan").is_some());
        assert_eq!(py_float("0x10"), None);
    }
}
