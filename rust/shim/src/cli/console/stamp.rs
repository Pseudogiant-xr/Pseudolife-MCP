//! `time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))` through the C
//! runtime's `localtime`, as CPython calls it: the UCRT's `localtime_s` on
//! Windows (which reads an inherited `TZ` before the system zone),
//! `localtime_r` elsewhere.

/// The range this command renders consistently across platforms.
const LAST: i64 = 32_503_679_999;
#[cfg(windows)]
const FIRST: i64 = 0;
#[cfg(not(windows))]
const FIRST: i64 = -30_000_000_000;

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

/// The local minute, when the time can be converted and rendered.
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
        let shown = |seconds| minute(seconds).unwrap();
        assert!(minute(LAST + 1).is_none());
        assert!(minute(FIRST - 1).is_none());
        if !cfg!(windows) {
            assert!(shown(-1).starts_with("19"));
        }
        assert!(minute(i64::MAX).is_none());
        assert!(minute(i64::MIN).is_none());
        let first = shown(0);
        assert!(first.starts_with("1970-01-01") || first.starts_with("1969-12-31"));
        assert!(shown(LAST).starts_with("29") || shown(LAST).starts_with("30"));
    }
}
