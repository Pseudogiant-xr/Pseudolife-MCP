//! `time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))` through the C
//! runtime's `localtime`, as CPython calls it: the UCRT's `localtime_s` on
//! Windows (which reads an inherited `TZ` before the system zone),
//! `localtime_r` elsewhere.

/// The last second this formats (2999-12-31T23:59:59Z): every platform's
/// `localtime` converts it; past it, where `localtime` still converts, the
/// oracle's `strftime` is not reproduced, so callers defer.
pub(super) const LAST: i64 = 32_503_679_999;
/// The first: Windows' CRT refuses a negative time (the oracle raises
/// `OSError`, [`Minute::Refused`]); glibc converts one, and from 1019 on
/// `%Y` has four digits.
#[cfg(windows)]
pub(super) const FIRST: i64 = 0;
#[cfg(not(windows))]
pub(super) const FIRST: i64 = -30_000_000_000;

#[cfg(windows)]
#[allow(unsafe_code)]
fn local(seconds: i64) -> Option<libc::tm> {
    let seconds: libc::time_t = seconds;
    // SAFETY: tm holds integer fields only, for which zero is valid.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    // SAFETY: the CRT reads this process's TZ; both pointers are valid for
    // the call and tm is correctly sized.
    unsafe {
        libc::tzset();
        if libc::localtime_s(&mut tm, &seconds) != 0 {
            return None;
        }
    }
    Some(tm)
}

// POSIX's tzset, which the libc crate does not bind on every target.
#[cfg(not(windows))]
#[allow(unsafe_code)]
unsafe extern "C" {
    fn tzset();
}

#[cfg(not(windows))]
#[allow(unsafe_code)]
fn local(seconds: i64) -> Option<libc::tm> {
    let seconds: libc::time_t = seconds;
    // SAFETY: tm holds integer fields and a pointer that localtime_r sets;
    // zero is a valid starting value for each.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    // SAFETY: both pointers are valid for the call; tzset only reads TZ.
    unsafe {
        tzset();
        if libc::localtime_r(&seconds, &mut tm).is_null() {
            return None;
        }
    }
    Some(tm)
}

/// What `_stamp(seconds)` does with a whole number of seconds.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum Minute {
    /// The local minute ([`FIRST`] ..= [`LAST`]).
    Shown(String),
    /// The C runtime's `localtime` refuses it: the oracle's `OSError`.
    Refused,
    /// `localtime` converts it, outside the formatted range.
    Unreproduced,
}

/// The local minute of `seconds`, as [`Minute`] says.
pub(super) fn minute(seconds: i64) -> Minute {
    let Some(tm) = local(seconds) else {
        return Minute::Refused;
    };
    if !(FIRST..=LAST).contains(&seconds) {
        return Minute::Unreproduced;
    }
    Minute::Shown(format!(
        "{:04}-{:02}-{:02} {:02}:{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_answered_range_is_bounded() {
        let shown = |seconds| match minute(seconds) {
            Minute::Shown(text) => text,
            other => panic!("{seconds}: {other:?}"),
        };
        assert_eq!(minute(LAST + 1), Minute::Unreproduced);
        // Windows' CRT refuses a negative time; glibc converts it.
        if cfg!(windows) {
            assert_eq!(minute(-1), Minute::Refused);
        } else {
            assert_eq!(minute(FIRST - 1), Minute::Unreproduced);
            assert!(shown(-1).starts_with("19"));
        }
        // Past any year localtime holds: refused, as the oracle's OSError.
        assert_eq!(minute(i64::MAX), Minute::Refused);
        assert_eq!(minute(i64::MIN), Minute::Refused);
        let first = shown(0);
        assert!(first.starts_with("1970-01-01") || first.starts_with("1969-12-31"));
        assert!(shown(LAST).starts_with("29") || shown(LAST).starts_with("30"));
    }
}
