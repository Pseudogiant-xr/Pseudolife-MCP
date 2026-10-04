use super::state;
use std::{
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};
pub const LEASE_SECONDS: f64 = 60.0;
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |time| time.as_secs_f64())
}
pub fn armed_until(digest: Option<&Path>) -> f64 {
    armed_until_at(digest, now())
}
pub fn armed_until_at(digest: Option<&Path>, now: f64) -> f64 {
    let Some(digest) = digest else {
        return 0.0;
    };
    let stem = digest.file_stem().unwrap_or_default().to_string_lossy();
    let mut paths = vec![
        digest.with_extension("wake-armed"),
        digest.with_extension("wait-armed"),
    ];
    if let Some(parent) = digest.parent()
        && let Ok(entries) = std::fs::read_dir(parent)
    {
        paths.extend(
            entries
                .filter_map(Result::ok)
                .map(|entry| entry.path())
                .filter(|path| {
                    let name = path.file_name().unwrap_or_default().to_string_lossy();
                    name.starts_with(&format!("{stem}."))
                        && (name.ends_with(".wake-armed") || name.ends_with(".wait-armed"))
                }),
        );
    }
    let mut latest: f64 = 0.0;
    for path in paths {
        let Ok(bytes) = state::read(&path, 256) else {
            continue;
        };
        if !bytes.is_ascii() {
            continue;
        }
        let Ok(text) = std::str::from_utf8(&bytes) else {
            continue;
        };
        let lines = text.lines().collect::<Vec<_>>();
        if lines.len() != 2 || lines[0].is_empty() {
            continue;
        }
        let suffix = path.extension().unwrap_or_default().to_string_lossy();
        if path != digest.with_extension("wake-armed")
            && path != digest.with_extension("wait-armed")
            && path.file_name().unwrap_or_default()
                != std::ffi::OsStr::new(&format!("{stem}.{}.{suffix}", lines[0]))
        {
            continue;
        }
        if suffix == "wake-armed"
            && !state::read(&digest.with_extension("wake"), 256).is_ok_and(|owner| {
                owner.is_ascii() && String::from_utf8_lossy(&owner).trim() == lines[0]
            })
        {
            continue;
        }
        if let Ok(expiry) = lines[1].trim().parse::<f64>()
            && expiry.is_finite()
            && now < expiry
            && expiry <= now + LEASE_SECONDS
        {
            latest = latest.max(expiry);
        }
    }
    latest
}
pub fn alphanumeric(value: char) -> bool {
    value.is_alphanumeric()
}
pub fn whitespace(value: char) -> bool {
    value.is_whitespace()
}
pub fn reason(value: &str) -> String {
    let compact = value
        .split(whitespace)
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    let result = compact
        .chars()
        .take(60)
        .filter(|c| alphanumeric(*c) || " _-".contains(*c))
        .collect::<String>();
    if result.is_empty() {
        "unknown".into()
    } else {
        result
    }
}
