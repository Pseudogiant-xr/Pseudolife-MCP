//! Passkey listing times use UTC with whole seconds and a trailing Z.
//! Expanded years retain their sign. Unsupported timestamps refuse the
//! listing before any output; local time zones do not affect the report.
use chrono::{TimeZone, Utc};

pub(super) struct Key {
    pub credential_id: String,
    pub label: String,
    pub state: String,
    pub enrolled_by: String,
    pub active_from: Option<f64>,
    pub last_used_at: Option<f64>,
    pub flagged_at: Option<f64>,
}

pub(super) fn key_prefix(credential_id: &str) -> String {
    credential_id.chars().take(12).collect()
}

pub(super) struct Clock;

pub(super) fn utc_clock() -> Clock {
    Clock
}

impl Clock {
    /// Floored whole seconds, including dates before the epoch and expanded years.
    pub fn when(&self, value: Option<f64>) -> Option<String> {
        let Some(value) = value else {
            return Some("-".into());
        };
        if !value.is_finite() {
            return None;
        }
        let utc = Utc.timestamp_opt(value.floor() as i64, 0).single()?;
        Some(utc.format("%Y-%m-%dT%H:%M:%SZ").to_string())
    }
}

pub(super) fn line(key: &Key, clock: &Clock) -> Option<String> {
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
    const LAST: f64 = 253_402_300_799.0;

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

    #[test]
    fn utc_listing_times_do_not_defer_for_zones_or_epoch_boundaries() {
        let clock = utc_clock();
        assert_eq!(clock.when(Some(-1.25)), Some("1969-12-31T23:59:58Z".into()));
        assert_eq!(
            clock.when(Some(1_791_000_000.75)),
            Some("2026-10-03T04:00:00Z".into())
        );
        assert_eq!(
            clock.when(Some(LAST + 1.0)),
            Some("+10000-01-01T00:00:00Z".into())
        );
        assert_eq!(clock.when(None), Some("-".into()));
    }

    #[test]
    fn label_repr_prefix_state_width_and_flags_stay_intact() {
        assert_eq!(
            line(&key("Tom's phone ☎"), &utc_clock()).unwrap(),
            "-dashKeyAAAA  active   \"Tom's phone ☎\"  enrolled_by=bootstrap  active_from=2026-10-03T04:00:00Z  last_used=-"
        );
        let mut flagged = key("it's \"twin\"");
        flagged.state = "pending".into();
        flagged.enrolled_by = "phoneKey_quarantineAAA".into();
        flagged.flagged_at = Some(1.0);
        let text = line(&flagged, &utc_clock()).unwrap();
        assert_eq!(
            text,
            r#"-dashKeyAAAA  pending  'it\'s "twin"'  enrolled_by=phoneKey_qua  active_from=2026-10-03T04:00:00Z  last_used=-  FLAGGED (sign count went backwards)"#
        );
        flagged.flagged_at = Some(0.0);
        assert!(!line(&flagged, &utc_clock()).unwrap().contains("FLAGGED"));
        flagged.flagged_at = Some(f64::NAN);
        assert!(line(&flagged, &utc_clock()).unwrap().contains("FLAGGED"));
        flagged.state = "suspended".into();
        assert!(
            line(&flagged, &utc_clock())
                .unwrap()
                .contains("  suspended 'it")
        );
    }

    #[test]
    fn invalid_timestamps_have_no_display_but_adjacent_valid_dates_do() {
        let clock = utc_clock();
        for value in [
            f64::NAN,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::MAX,
            f64::MIN,
        ] {
            assert_eq!(clock.when(Some(value)), None);
        }
        for value in [
            -1.0,
            0.0,
            LAST,
            LAST + 1.0,
            1_760_000_000.0,
            1_798_675_200.0,
        ] {
            assert!(clock.when(Some(value)).unwrap().ends_with('Z'));
        }
        let mut invalid = key("label");
        invalid.last_used_at = Some(f64::NAN);
        assert!(line(&invalid, &clock).is_none());
    }
}
