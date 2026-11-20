//! Background release discovery, never a request-path fetch.
//! Contract: docs/dev/release-assets.md, "How a release is discovered".

use serde_json::Value;
use std::cmp::Ordering;
use std::sync::atomic::{AtomicBool, Ordering as AtomicOrdering};
use std::sync::{Arc, RwLock};
use std::time::Duration;
use tokio::sync::watch;
use tokio::task::JoinHandle;

const GITHUB_RELEASE: &str =
    "https://api.github.com/repos/Pseudogiant-xr/Pseudolife-MCP/releases/latest";
const RETRY_AFTER_FAILURE_SECONDS: f64 = 900.0;

#[derive(Clone, Default)]
pub struct Snapshot {
    pub enabled: bool,
    pub latest_release: Option<String>,
    pub checked_at: f64,
    pub failed_at: f64,
}

#[derive(Default)]
pub struct ReleaseCheck {
    started: AtomicBool,
    state: RwLock<Snapshot>,
}

pub(crate) fn version_shape(value: &str) -> bool {
    (1..=32).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".+-".contains(&b))
}

/// Package metadata uses PEP 440 (`0.20.0rc1`), while Cargo uses a
/// semver prerelease (`0.20.0-rc1`). Only known prerelease spellings are
/// interchangeable; unknown suffixes and different release tuples stay distinct.
pub(crate) fn same_version(a: &str, b: &str) -> bool {
    if a == b {
        return true;
    }
    static PRERELEASE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"(?i)^([0-9]+(?:\.[0-9]+)*)-?(alpha|beta|preview|pre|rc|a|b|c)(?:[.-]?([0-9]+))?(\+[a-z0-9.-]+)?$")
            .expect("prerelease version pattern")
    });
    let key = |value: &str| {
        let parts = PRERELEASE.captures(value)?;
        let kind = match parts[2].to_ascii_lowercase().as_str() {
            "a" | "alpha" => "a",
            "b" | "beta" => "b",
            _ => "rc",
        };
        let number = parts
            .get(3)
            .map_or("0", |n| n.as_str())
            .trim_start_matches('0');
        Some((
            parts[1].to_owned(),
            kind,
            if number.is_empty() { "0" } else { number }.to_owned(),
            parts.get(4).map(|v| v.as_str().to_owned()),
        ))
    };
    matches!((key(a), key(b)), (Some(a), Some(b)) if a == b)
}

/// Numeric tuple comparison without narrowing Python's integer components.
pub(crate) fn version_key(value: &str) -> Option<Vec<&str>> {
    let mut end = 0;
    let bytes = value.as_bytes();
    while end < bytes.len() && bytes[end].is_ascii_digit() {
        end += 1;
    }
    if end == 0 {
        return None;
    }
    loop {
        if bytes.get(end) != Some(&b'.') || !bytes.get(end + 1).is_some_and(u8::is_ascii_digit) {
            break;
        }
        end += 1;
        while end < bytes.len() && bytes[end].is_ascii_digit() {
            end += 1;
        }
    }
    Some(
        value[..end]
            .split('.')
            .map(|s| {
                let n = s.trim_start_matches('0');
                if n.is_empty() { "0" } else { n }
            })
            .collect(),
    )
}

pub(crate) fn compare_keys(a: &[&str], b: &[&str]) -> Ordering {
    for (a, b) in a.iter().zip(b) {
        let cmp = a.len().cmp(&b.len()).then_with(|| a.cmp(b));
        if cmp != Ordering::Equal {
            return cmp;
        }
    }
    a.len().cmp(&b.len())
}

/// `update_notice`: one line when the daemon knows of a release newer than
/// itself, naming the plugin's version when that differs too.
pub fn update_notice(
    daemon: &str,
    latest: Option<&str>,
    plugin: Option<&str>,
    command: &str,
) -> String {
    let Some(latest) = latest.filter(|s| version_shape(s)) else {
        return String::new();
    };
    if !version_shape(daemon) {
        return String::new();
    }
    let (Some(l), Some(d)) = (version_key(latest), version_key(daemon)) else {
        return String::new();
    };
    if compare_keys(&l, &d) != Ordering::Greater {
        return String::new();
    }
    let mut location = format!("daemon {daemon}");
    if let Some(plugin) = plugin.filter(|p| version_shape(p) && !same_version(p, daemon)) {
        location.push_str(&format!(", plugin {plugin}"));
    }
    format!(
        "Pseudolife-MCP: release {latest} is available ({location}) — run {command} update, then start a new session."
    )
}

impl ReleaseCheck {
    pub fn snapshot(&self) -> Snapshot {
        self.state.read().expect("release snapshot").clone()
    }

    pub fn record(&self, latest: Option<String>, now: f64) {
        let mut state = self.state.write().expect("release snapshot");
        if let Some(latest) = latest {
            state.latest_release = Some(latest);
            state.checked_at = now;
        } else {
            if crate::mutants::active("release-forget-last-good") {
                state.latest_release = None;
            }
            state.failed_at = now;
        }
    }

    pub fn start(
        self: &Arc<Self>,
        enabled: bool,
        off_switch: Option<&str>,
        interval: f64,
        stop: watch::Receiver<bool>,
    ) -> Option<JoinHandle<()>> {
        if !enabled
            || off_switch.is_some_and(|v| crate::storage::py_strip(v) == "0")
            || self.started.swap(true, AtomicOrdering::SeqCst)
        {
            return None;
        }
        self.state.write().expect("release snapshot").enabled = true;
        let me = self.clone();
        Some(tokio::spawn(async move {
            me.run(interval, stop).await;
        }))
    }

    async fn run(&self, interval: f64, stop: watch::Receiver<bool>) {
        let client = match reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .redirect(reqwest::redirect::Policy::none())
            .build()
        {
            Ok(client) => client,
            Err(_) => return,
        };
        self.run_with_fetch(interval, stop, || {
            fetch_latest(&client, |request| client.execute(request))
        })
        .await;
    }

    async fn run_with_fetch<F, Fut>(
        &self,
        interval: f64,
        mut stop: watch::Receiver<bool>,
        mut fetch: F,
    ) where
        F: FnMut() -> Fut,
        Fut: std::future::Future<Output = FetchResult>,
    {
        loop {
            if *stop.borrow() {
                return;
            }
            let result =
                tokio::select! { _ = stop.changed() => return, result = fetch() => result };
            let now = unix_seconds();
            let delay = result.delay(interval, self.snapshot().latest_release.is_some());
            self.record(result.latest, now);
            let Ok(delay) = Duration::try_from_secs_f64(delay) else {
                return;
            };
            tokio::select! { _ = stop.changed() => return, _ = tokio::time::sleep(delay) => {} }
        }
    }
}

#[derive(Default)]
struct FetchResult {
    latest: Option<String>,
    // Incomplete publication and rate limits never use the initial-failure retry.
    normal_schedule: bool,
    retry_after: f64,
}

impl FetchResult {
    fn delay(&self, interval: f64, has_cached_release: bool) -> f64 {
        if self.normal_schedule || has_cached_release || self.latest.is_some() {
            interval.max(self.retry_after)
        } else {
            interval.min(RETRY_AFTER_FAILURE_SECONDS)
        }
    }
}

async fn fetch_latest<F, Fut, E>(client: &reqwest::Client, send: F) -> FetchResult
where
    F: FnOnce(reqwest::Request) -> Fut,
    Fut: std::future::Future<Output = Result<reqwest::Response, E>>,
{
    let Ok(request) = client
        .get(GITHUB_RELEASE)
        .header(reqwest::header::USER_AGENT, "pseudolife-daemon")
        .header(reqwest::header::ACCEPT, "application/vnd.github+json")
        .timeout(Duration::from_secs(5))
        .build()
    else {
        return FetchResult::default();
    };
    let Ok(response) = send(request).await else {
        return FetchResult::default();
    };
    if matches!(response.status().as_u16(), 403 | 429) {
        return FetchResult {
            normal_schedule: true,
            retry_after: rate_limit_wait(response.headers(), unix_seconds()),
            ..FetchResult::default()
        };
    }
    if response.status() != reqwest::StatusCode::OK {
        return FetchResult::default();
    }
    let Ok(data) = response.json::<Value>().await else {
        return FetchResult::default();
    };
    release_metadata(&data)
}

fn unix_seconds() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn rate_limit_wait(headers: &reqwest::header::HeaderMap, now: f64) -> f64 {
    let header = |name| headers.get(name).and_then(|v| v.to_str().ok());
    let seconds = |s: &str| {
        (!s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
            .then(|| s.parse::<u64>().ok())
            .flatten()
    };
    let retry = header("retry-after").and_then(|s| {
        seconds(s).map(|n| n as f64).or_else(|| {
            chrono::DateTime::parse_from_rfc2822(s)
                .ok()
                .map(|date| date.timestamp() as f64 - now)
        })
    });
    retry
        .or_else(|| {
            header("x-ratelimit-reset")
                .and_then(seconds)
                .map(|n| n as f64 - now)
        })
        .unwrap_or(0.0)
        .clamp(0.0, 3600.0)
}

fn release_metadata(data: &Value) -> FetchResult {
    if data["draft"] != false || data["prerelease"] != false {
        return FetchResult::default();
    }
    let Some(version) = data["tag_name"].as_str().and_then(|s| s.strip_prefix('v')) else {
        return FetchResult::default();
    };
    // Mirror update::plain_release and update::version_key's integer range.
    let parts: Vec<_> = version.split('.').collect();
    if !version_shape(version)
        || !(2..=4).contains(&parts.len())
        || parts.iter().any(|s| {
            s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) || s.parse::<u64>().is_err()
        })
    {
        return FetchResult::default();
    }
    FetchResult {
        latest: complete_assets(data, version).then(|| version.to_owned()),
        normal_schedule: true,
        ..FetchResult::default()
    }
}

fn complete_assets(data: &Value, version: &str) -> bool {
    let Some(assets) = data["assets"].as_array() else {
        return false;
    };
    // SHA256SUMS uploads first, not last. All six archives must also be present.
    // Keep these names aligned with docs/dev/release-assets.md and its publisher.
    let mut names = vec![
        "RELEASE-READY.json".to_owned(),
        "SHA256SUMS".to_owned(),
        format!("pseudolife-compose-{version}.tar.gz"),
    ];
    for (target, extension) in [
        ("x86_64-unknown-linux-gnu", "tar.gz"),
        ("aarch64-unknown-linux-gnu", "tar.gz"),
        ("x86_64-apple-darwin", "tar.gz"),
        ("aarch64-apple-darwin", "tar.gz"),
        ("x86_64-pc-windows-msvc", "zip"),
    ] {
        names.push(format!("pseudolife-mcp-{version}-{target}.{extension}"));
    }
    names.iter().all(|name| {
        let mut matches = assets.iter().filter(|asset| asset["name"] == *name);
        matches.next().is_some_and(|asset| {
            asset["state"] == "uploaded" && asset["size"].as_u64().is_some_and(|n| n > 0)
        }) && matches.next().is_none()
    })
}

#[cfg(test)]
#[path = "release_check/tests.rs"]
mod github_tests;

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn start_gates_and_health_snapshot_are_idempotent_without_network() {
        let check = Arc::new(ReleaseCheck::default());
        let stop = watch::channel(true).0;
        assert!(check.start(false, None, 60.0, stop.subscribe()).is_none());
        assert!(
            check
                .start(true, Some(" 0 "), 60.0, stop.subscribe())
                .is_none()
        );
        assert!(!check.snapshot().enabled);
        let task = check.start(true, None, 60.0, stop.subscribe()).unwrap();
        assert!(check.start(true, None, 60.0, stop.subscribe()).is_none());
        task.await.unwrap();
        let snapshot = check.snapshot();
        assert!(snapshot.enabled);
        assert!(snapshot.latest_release.is_none());
        assert_eq!(snapshot.checked_at, 0.0);
    }
    #[test]
    fn compatibility_prerelease_spelling_is_not_a_plugin_mismatch() {
        assert_eq!(
            update_notice("0.20.0-rc1", Some("0.21.0"), Some("0.20.0rc1"), "pl"),
            "Pseudolife-MCP: release 0.21.0 is available (daemon 0.20.0-rc1) — run pl update, then start a new session."
        );
    }

    #[test]
    fn compatibility_python_prerelease_spelling_equivalence() {
        let cases: Value =
            serde_json::from_str(include_str!("../harness/goldens/version-spelling.json")).unwrap();
        for case in cases.as_array().unwrap() {
            let python = case["python"].as_str().unwrap();
            let native = case["native"].as_str().unwrap();
            let equal = case["equal"].as_bool().unwrap();
            assert_eq!(same_version(python, native), equal, "{case}");
            assert_eq!(same_version(native, python), equal, "{case}");
        }
        for (a, b) in [
            ("0.20.0-custom1", "0.20.0custom1"),
            ("0.20.0-rc1.extra", "0.20.0rc1"),
            ("0.20.0-rc.", "0.20.0rc0"),
            ("0.20.0-alpha-", "0.20.0a0"),
            ("0.20.0.dev1", "0.20.0"),
            ("0.20.0", "0.20"),
            ("not-a-version", "another-version"),
        ] {
            assert!(!same_version(a, b), "{a} / {b}");
        }
    }

    #[test]
    fn live_python_notice_goldens() {
        let golden: Value =
            serde_json::from_str(include_str!("../harness/goldens/maintenance-policy.json"))
                .unwrap();
        for case in golden["notices"].as_array().unwrap() {
            assert_eq!(
                update_notice(
                    case["daemon"].as_str().unwrap(),
                    case["latest"].as_str(),
                    case["plugin"].as_str(),
                    case["command"].as_str().unwrap()
                ),
                case["expected"].as_str().unwrap()
            );
        }
    }

    #[test]
    fn failure_keeps_the_last_good_release() {
        let state = ReleaseCheck::default();
        state.record(Some("0.16.0".into()), 100.0);
        state.record(None, 110.0);
        let snapshot = state.snapshot();
        assert_eq!(snapshot.latest_release.as_deref(), Some("0.16.0"));
        assert_eq!(snapshot.checked_at, 100.0);
        assert_eq!(snapshot.failed_at, 110.0);
    }

    #[test]
    fn offer_matches_python_numeric_tuple_and_exact_text() {
        assert_eq!(
            update_notice("0.15.0", Some("0.16.0"), Some("0.14.0"), "pseudolife-mcp"),
            "Pseudolife-MCP: release 0.16.0 is available (daemon 0.15.0, plugin 0.14.0) — run pseudolife-mcp update, then start a new session."
        );
        for latest in ["0.15.0", "0.14.0", "0.16.0\nEXTRA", "x", ""] {
            assert!(update_notice("0.15.0", Some(latest), None, "pseudolife-mcp").is_empty());
        }
        assert!(!update_notice("1.0", Some("1.0.0"), None, "pseudolife-mcp").is_empty());
        assert!(
            !update_notice(
                "9",
                Some("99999999999999999999999999999999"),
                None,
                "pseudolife-mcp"
            )
            .is_empty()
        );
    }
}
