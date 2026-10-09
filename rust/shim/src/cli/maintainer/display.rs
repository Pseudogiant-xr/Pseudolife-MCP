//! `maintainer list` lines: `key_prefix`, the label's `repr`, and `_when`'s
//! `time.strftime("%Y-%m-%d %H:%M", time.localtime(value))`.
//!
//! Local time is shown only where the native zone provably agrees with
//! CPython's: `TZ` unset (the C runtime and glibc honour it, Chrono does not
//! read it the same way), a value in years 1970-9999 (Windows' C runtime
//! refuses negative times; CPython's own range ends far later). On Windows
//! the C runtime applies the zone's current rules to every year, while
//! Chrono asks Windows for each year's rules, so a value is shown there only
//! when it, and two days either side, fall in the current local year with
//! one offset: no year boundary or DST transition is near. Anything else
//! makes the caller defer the whole listing before printing a line.
use chrono::{DateTime, Datelike, Local, Offset, TimeZone};

/// One passkey row as `MaintainerStore.passkeys` returns it.
pub(super) struct Key {
    pub credential_id: String,
    pub label: String,
    pub state: String,
    pub enrolled_by: String,
    pub active_from: Option<f64>,
    pub last_used_at: Option<f64>,
    pub flagged_at: Option<f64>,
}

/// `key_prefix`: the first 12 code points.
pub(super) fn key_prefix(credential_id: &str) -> String {
    credential_id.chars().take(12).collect()
}

/// How a timestamp becomes local text; tests inject a fixed zone.
pub(super) struct Clock<Tz: TimeZone> {
    pub zone: Tz,
    pub year: i32,
    pub per_year_rules: bool,
    pub tz_set: bool,
}

pub(super) fn local_clock() -> Clock<Local> {
    Clock {
        zone: Local,
        year: Local::now().year(),
        per_year_rules: cfg!(windows),
        tz_set: std::env::var_os("TZ").is_some(),
    }
}

/// The latest value whose local year stays four digits (9999-12-31 UTC).
const LAST: f64 = 253_402_300_799.0;
const TWO_DAYS: i64 = 2 * 86_400;

impl<Tz: TimeZone> Clock<Tz> {
    fn local(&self, seconds: i64) -> Option<DateTime<Tz>> {
        self.zone.timestamp_opt(seconds, 0).single()
    }

    /// `_when(value)`, or `None` where the native answer is not proven equal.
    pub fn when(&self, value: Option<f64>) -> Option<String> {
        let Some(value) = value else {
            return Some("-".into());
        };
        if self.tz_set || !value.is_finite() || !(0.0..=LAST).contains(&value) {
            return None;
        }
        // time.localtime floors a float to whole seconds.
        let seconds = value.floor() as i64;
        let local = self.local(seconds)?;
        if self.per_year_rules {
            let offset = local.offset().fix();
            for neighbour in [seconds - TWO_DAYS, seconds, seconds + TWO_DAYS] {
                let near = self.local(neighbour)?;
                if near.year() != self.year || near.offset().fix() != offset {
                    return None;
                }
            }
        }
        Some(local.naive_local().format("%Y-%m-%d %H:%M").to_string())
    }
}

/// One `list` line (without its newline), or `None` to defer.
pub(super) fn line<Tz: TimeZone>(key: &Key, clock: &Clock<Tz>) -> Option<String> {
    let flagged = key.flagged_at.is_some_and(|value| value != 0.0);
    Some(format!(
        "{}  {:<8} {}  enrolled_by={}  active_from={}  last_used={}{}",
        key_prefix(&key.credential_id),
        key.state,
        super::super::mode_repr(&key.label),
        key_prefix(&key.enrolled_by),
        clock.when(key.active_from)?,
        clock.when(key.last_used_at)?,
        if flagged {
            "  FLAGGED (sign count went backwards)"
        } else {
            ""
        }
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::FixedOffset;

    fn key(label: &str) -> Key {
        Key {
            credential_id: "-dashKeyAAAAAAAAAAAAAA".into(),
            label: label.into(),
            state: "active".into(),
            enrolled_by: "bootstrap".into(),
            active_from: Some(1_791_000_000.75),
            last_used_at: None,
            flagged_at: None,
        }
    }

    fn fixed(per_year_rules: bool) -> Clock<FixedOffset> {
        Clock {
            zone: FixedOffset::east_opt(2 * 3600).unwrap(),
            year: 2026,
            per_year_rules,
            tz_set: false,
        }
    }

    #[test]
    fn a_line_matches_the_python_f_string() {
        assert_eq!(
            line(&key("Tom's phone \u{260e}"), &fixed(false)).unwrap(),
            "-dashKeyAAAA  active   \"Tom's phone \u{260e}\"  enrolled_by=bootstrap  active_from=2026-10-03 06:00  last_used=-"
        );
        let mut flagged = key("it's \"twin\"");
        flagged.state = "pending".into();
        flagged.enrolled_by = "phoneKey_quarantineAAA".into();
        flagged.flagged_at = Some(1.0);
        assert_eq!(
            line(&flagged, &fixed(true)).unwrap(),
            "-dashKeyAAAA  pending  'it\\'s \"twin\"'  enrolled_by=phoneKey_qua  active_from=2026-10-03 06:00  last_used=-  FLAGGED (sign count went backwards)"
        );
        // flagged_at is a float: 0.0 is falsy in Python, NaN is truthy.
        flagged.flagged_at = Some(0.0);
        assert!(!line(&flagged, &fixed(false)).unwrap().contains("FLAGGED"));
        flagged.flagged_at = Some(f64::NAN);
        assert!(line(&flagged, &fixed(false)).unwrap().contains("FLAGGED"));
        // A state longer than the width is not cut.
        flagged.state = "suspended".into();
        assert!(
            line(&flagged, &fixed(false))
                .unwrap()
                .contains("  suspended 'it")
        );
    }

    #[test]
    fn time_is_floored_and_out_of_domain_values_defer() {
        let clock = fixed(false);
        assert_eq!(
            clock.when(Some(59.999)).unwrap(),
            "1970-01-01 02:00",
            "floored, then shifted"
        );
        for value in [f64::NAN, f64::INFINITY, -1.0, LAST + 1.0] {
            assert_eq!(clock.when(Some(value)), None, "{value}");
        }
        assert_eq!(clock.when(None).unwrap(), "-");
        let tz = Clock {
            tz_set: true,
            ..fixed(false)
        };
        assert_eq!(tz.when(Some(1_791_000_000.0)), None);
        assert_eq!(tz.when(None).unwrap(), "-");
    }

    #[test]
    fn per_year_rules_show_only_the_current_year_away_from_its_edges() {
        let clock = fixed(true);
        // 2026-10-03 is shown; 2025 and 2026-12-31 are not.
        assert!(clock.when(Some(1_791_000_000.0)).is_some());
        assert_eq!(clock.when(Some(1_760_000_000.0)), None);
        assert_eq!(clock.when(Some(1_798_675_200.0)), None);
        // The same values on a zone with history (unix) are shown.
        assert!(fixed(false).when(Some(1_760_000_000.0)).is_some());
        assert!(fixed(false).when(Some(1_798_675_200.0)).is_some());
    }
}
