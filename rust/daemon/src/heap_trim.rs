//! glibc heap trimming (`utils/heap_trim.py`).

const DEFAULT_INTERVAL_SECONDS: f64 = 60.0;

pub fn interval(raw: Option<&str>) -> Result<f64, String> {
    let raw = raw.map(crate::storage::py_strip).unwrap_or("");
    let value = if raw.is_empty() {
        DEFAULT_INTERVAL_SECONDS
    } else {
        raw.parse::<f64>().unwrap_or(f64::NAN)
    };
    if !value.is_finite() {
        return Err(
            "PSEUDOLIFE_MALLOC_TRIM_SECONDS must be a number of seconds (0 disables)".into(),
        );
    }
    Ok(value)
}

pub fn available() -> bool {
    cfg!(all(target_os = "linux", target_env = "gnu"))
}

pub fn trim_once() {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    {
        let before = read_anon_bytes();
        trim_glibc();
        if let (Some(before), Some(after)) = (before, read_anon_bytes()) {
            // Python's measured log threshold (2026-09-23 allocator probe):
            // below 64 MiB the change was mostly resident-memory jitter.
            if before - after >= 64 * 1024 * 1024 {
                eprintln!(
                    "heap trim: returned {} MiB of freed heap to the OS",
                    (before - after) / (1024 * 1024)
                );
            }
        }
    }
}

#[cfg(all(target_os = "linux", target_env = "gnu"))]
fn read_anon_bytes() -> Option<i64> {
    std::fs::read_to_string("/proc/self/status")
        .ok()?
        .lines()
        .find(|line| line.starts_with("RssAnon:"))?
        .split_whitespace()
        .nth(1)?
        .parse::<i64>()
        .ok()?
        .checked_mul(1024)
}

/// Invariant: glibc owns all allocator state; malloc_trim accepts a scalar
/// pad and exposes no borrowed pointer or handle. It is thread safe and
/// runs on a blocking worker without holding a service lock.
#[cfg(all(target_os = "linux", target_env = "gnu"))]
fn trim_glibc() {
    unsafe {
        libc::malloc_trim(0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn default_disable_and_bad_intervals() {
        assert_eq!(interval(None), Ok(60.0));
        assert_eq!(interval(Some("  ")), Ok(60.0));
        assert_eq!(interval(Some("0")), Ok(0.0));
        assert_eq!(interval(Some("-1")), Ok(-1.0));
        for raw in ["abc", "NaN", "inf"] {
            assert!(interval(Some(raw)).is_err());
        }
    }
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    #[test]
    fn actual_glibc_trim_runs() {
        trim_once();
        assert!(read_anon_bytes().is_some());
    }
}
