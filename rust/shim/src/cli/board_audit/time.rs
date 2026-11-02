//! Python's ISO parser and naive `datetime.timestamp(fold=0)` convention.
use chrono::{Datelike, NaiveDate, NaiveDateTime};
#[cfg(not(windows))]
use chrono::{Local, LocalResult, TimeZone};

fn digits(text: &str) -> Option<u32> {
    (!text.is_empty() && text.bytes().all(|c| c.is_ascii_digit()))
        .then(|| text.parse().ok())
        .flatten()
}

fn clock(text: &str) -> Option<(u32, u32, u32, u32)> {
    let (whole, fraction) = text
        .split_once(['.', ','])
        .map_or((text, None), |(a, b)| (a, Some(b)));
    if !whole.is_ascii() {
        return None;
    }
    let parts: Vec<_> = if whole.contains(':') {
        whole.split(':').collect()
    } else {
        if !matches!(whole.len(), 2 | 4 | 6) {
            return None;
        }
        (0..whole.len())
            .step_by(2)
            .map(|i| &whole[i..i + 2])
            .collect()
    };
    if parts.is_empty() || parts.len() > 3 || parts.iter().any(|p| p.len() != 2) {
        return None;
    }
    let h = digits(parts[0])?;
    let m = parts.get(1).map_or(Some(0), |v| digits(v))?;
    let s = parts.get(2).map_or(Some(0), |v| digits(v))?;
    let micros = if let Some(fraction) = fraction {
        if fraction.is_empty() || !fraction.bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        fraction
            .bytes()
            .take(6)
            .chain(std::iter::repeat(b'0'))
            .take(6)
            .fold(0, |n, c| n * 10 + u32::from(c - b'0'))
    } else {
        0
    };
    Some((h, m, s, micros))
}

pub(super) fn iso(value: &str) -> Option<f64> {
    parse(value, false)
}

/// Suite durations use naive.astimezone().timestamp(), which chooses the
/// offset at the implied instant for a gap, unlike naive.timestamp().
pub(super) fn stamp(value: &str) -> Option<f64> {
    parse(value, true)
}

fn parse(value: &str, astimezone: bool) -> Option<f64> {
    let week = value.as_bytes().get(4) == Some(&b'W') || value.as_bytes().get(5) == Some(&b'W');
    let date_len = if week {
        if value.as_bytes().get(4) == Some(&b'-') {
            if value.as_bytes().get(8) == Some(&b'-') {
                10
            } else {
                8
            }
        } else if value.as_bytes().get(7).is_some_and(u8::is_ascii_digit) {
            8
        } else {
            7
        }
    } else if value.as_bytes().get(4) == Some(&b'-') {
        10
    } else {
        8
    };
    let date_text = value.get(..date_len)?;
    let date = if week {
        let compact = date_text.replace('-', "");
        let y = digits(compact.get(..4)?)? as i32;
        let w = digits(compact.get(5..7)?)?;
        let d = compact
            .get(7..)
            .filter(|v| !v.is_empty())
            .map_or(Some(1), digits)?;
        let weekday = match d {
            1 => chrono::Weekday::Mon,
            2 => chrono::Weekday::Tue,
            3 => chrono::Weekday::Wed,
            4 => chrono::Weekday::Thu,
            5 => chrono::Weekday::Fri,
            6 => chrono::Weekday::Sat,
            7 => chrono::Weekday::Sun,
            _ => return None,
        };
        NaiveDate::from_isoywd_opt(y, w, weekday)?
    } else {
        NaiveDate::parse_from_str(
            date_text,
            if date_len == 10 { "%Y-%m-%d" } else { "%Y%m%d" },
        )
        .ok()?
    };
    if !(1..=9999).contains(&date.year()) {
        return None;
    }
    let rest = value.get(date_len..)?;
    let (h, m, s, us, offset) = if rest.is_empty() {
        (0, 0, 0, 0, None)
    } else {
        // fromisoformat permits any one Unicode separator, including a space.
        let rest = rest.get(rest.chars().next()?.len_utf8()..)?;
        let at = rest.find(['+', '-', 'Z']);
        let (body, zone) = at.map_or((rest, None), |at| (&rest[..at], Some(&rest[at..])));
        let (h, m, s, us) = clock(body)?;
        let offset = match zone {
            None => None,
            Some("Z") => Some(0_i64),
            Some(zone) => {
                let (h, m, s, us) = clock(&zone[1..])?;
                let offset = (i64::from(h) * 3600 + i64::from(m) * 60 + i64::from(s)) * 1_000_000
                    + i64::from(us);
                if offset >= 86_400_000_000 {
                    return None;
                }
                // Python treats a fractional all-zero offset as UTC.
                let offset = if h == 0 && m == 0 && s == 0 {
                    0
                } else {
                    offset
                };
                Some(if zone.starts_with('-') {
                    -offset
                } else {
                    offset
                })
            }
        };
        (h, m, s, us, offset)
    };
    if h > 23 || m > 59 || s > 59 {
        return None;
    }
    let naive = date.and_hms_micro_opt(h, m, s, us)?;
    let micros = match offset {
        Some(offset) => naive.and_utc().timestamp_micros().checked_sub(offset)?,
        None => {
            let micros = local_micros(naive)?;
            if astimezone {
                #[cfg(not(windows))]
                let offset = i64::from(
                    Local
                        .timestamp_opt(micros.div_euclid(1_000_000), 0)
                        .single()?
                        .offset()
                        .local_minus_utc(),
                );
                #[cfg(windows)]
                let offset =
                    local_seconds(micros.div_euclid(1_000_000))? - micros.div_euclid(1_000_000);
                naive
                    .and_utc()
                    .timestamp_micros()
                    .checked_sub(offset * 1_000_000)?
            } else {
                micros
            }
        }
    };
    // CPython divides integer microseconds, rounding once even for old dates.
    format!("{micros}e-6").parse().ok()
}

#[cfg(not(windows))]
fn local_micros(naive: NaiveDateTime) -> Option<i64> {
    match Local.from_local_datetime(&naive) {
        LocalResult::Single(t) => Some(t.timestamp_micros()),
        LocalResult::Ambiguous(a, b) => Some(a.timestamp_micros().min(b.timestamp_micros())),
        LocalResult::None => {
            // fold=0 uses the offset before a gap (and so the later UTC instant).
            // Look back across even a date-line transition; never use today's offset.
            for hours in [1, 2, 4, 8, 16, 32, 64] {
                let before = naive.checked_sub_signed(chrono::Duration::hours(hours))?;
                let offset = match Local.from_local_datetime(&before) {
                    LocalResult::Single(t) => Some(t.offset().local_minus_utc()),
                    LocalResult::Ambiguous(a, _) => Some(a.offset().local_minus_utc()),
                    LocalResult::None => None,
                };
                if let Some(offset) = offset {
                    return naive
                        .and_utc()
                        .timestamp_micros()
                        .checked_sub(i64::from(offset) * 1_000_000);
                }
            }
            None
        }
    }
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn local_micros(naive: NaiveDateTime) -> Option<i64> {
    // Python uses the CRT local zone on Windows, including an inherited TZ;
    // chrono's Windows Local instead uses the Windows system zone alone.
    use chrono::Timelike;
    static TZ: std::sync::Once = std::sync::Once::new();
    TZ.call_once(|| unsafe { libc::tzset() });
    // CPython's local-time inverse: choose the earlier solution in a fold,
    // the later implied solution in a gap (datetime's default fold=0).
    let t = naive.and_utc().timestamp();
    let a = local_seconds(t)? - t;
    let u1 = t - a;
    let t1 = local_seconds(u1)?;
    let b = if t1 == t {
        let probe = u1 - 86400;
        let b = local_seconds(probe)? - probe;
        if a == b {
            return Some(u1 * 1_000_000 + i64::from(naive.nanosecond() / 1000));
        }
        b
    } else {
        t1 - u1
    };
    let u2 = t - b;
    let t2 = local_seconds(u2)?;
    let result = if t2 == t {
        u2
    } else if t1 == t {
        u1
    } else {
        u1.max(u2)
    };
    Some(result * 1_000_000 + i64::from(naive.nanosecond() / 1000))
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn local_seconds(seconds: i64) -> Option<i64> {
    let seconds: libc::time_t = seconds.try_into().ok()?;
    let mut tm = std::mem::MaybeUninit::<libc::tm>::uninit();
    // SAFETY: localtime_s initializes tm on success and receives valid
    // pointers to correctly sized storage; no process environment changes.
    if unsafe { libc::localtime_s(tm.as_mut_ptr(), &seconds) } != 0 {
        return None;
    }
    let tm = unsafe { tm.assume_init() };
    NaiveDate::from_ymd_opt(
        tm.tm_year + 1900,
        (tm.tm_mon + 1).try_into().ok()?,
        tm.tm_mday.try_into().ok()?,
    )?
    .and_hms_opt(
        tm.tm_hour.try_into().ok()?,
        tm.tm_min.try_into().ok()?,
        tm.tm_sec.try_into().ok()?,
    )
    .map(|t| t.and_utc().timestamp())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn iso_spellings_and_microsecond_boundary() {
        for value in [
            "2023-11-14T22:13:20Z",
            "20231114 221320+00",
            "2023-W46-2T23:13:20+0100",
        ] {
            assert_eq!(iso(value), Some(1_700_000_000.0), "{value}");
        }
        assert_eq!(iso("1970-01-01T00:00:59.999999+00:01"), Some(-0.000001));
        assert_eq!(iso("2026-02-30"), None);
        assert_eq!(iso("2026-01-01T😀aa"), None);
        assert_eq!(iso("2026-01-01T24"), None);
        assert_eq!(iso("2026-01-01T12:00:60"), None);
    }
}
