//! `time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))` through the C
//! runtime's `localtime`, as CPython calls it: the UCRT's `localtime_s` on
//! Windows (which reads an inherited `TZ` before the system zone),
//! `localtime_r` elsewhere.

/// The last second this answers (2999-12-31T23:59:59Z): every platform's
/// `localtime` converts it; past it the oracle may raise, so callers defer.
pub(super) const LAST: i64 = 32_503_679_999;
/// The first: Windows' CRT refuses a negative time (the oracle raises
/// `OSError`); glibc converts one, and from 1019 on `%Y` has four digits.
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

/// The local minute of `seconds` ([`FIRST`] ..= [`LAST`]), or `None`.
pub(super) fn minute(seconds: i64) -> Option<String> {
    if !(FIRST..=LAST).contains(&seconds) {
        return None;
    }
    let tm = local(seconds)?;
    Some(format!(
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
        assert!(minute(FIRST - 1).is_none());
        assert!(minute(LAST + 1).is_none());
        assert_eq!(minute(-1).is_some(), cfg!(not(windows)));
        let first = minute(0).unwrap();
        assert!(first.starts_with("1970-01-01") || first.starts_with("1969-12-31"));
        assert!(minute(LAST).unwrap().starts_with("29") || minute(LAST).unwrap().starts_with("30"));
    }
}
