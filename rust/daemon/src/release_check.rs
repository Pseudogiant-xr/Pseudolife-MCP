//! Background release knowledge (`release_check.py`), never a request-path fetch.

use serde_json::Value;
use std::cmp::Ordering;
use std::sync::atomic::{AtomicBool, Ordering as AtomicOrdering};
use std::sync::{Arc, RwLock};
use std::time::Duration;
use tokio::sync::watch;
use tokio::task::JoinHandle;

const PYPI_JSON: &str = "https://pypi.org/pypi/pseudolife-mcp/json";
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

fn version_shape(value: &str) -> bool {
    (1..=32).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".+-".contains(&b))
}

/// Numeric tuple comparison without narrowing Python's integer components.
fn version_key(value: &str) -> Option<Vec<&str>> {
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

fn compare_keys(a: &[&str], b: &[&str]) -> Ordering {
    for (a, b) in a.iter().zip(b) {
        let cmp = a.len().cmp(&b.len()).then_with(|| a.cmp(b));
        if cmp != Ordering::Equal {
            return cmp;
        }
    }
    a.len().cmp(&b.len())
}

// W2-D's session-start briefing consumes this when its hook handler lands.
#[allow(dead_code)]
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
    if let Some(plugin) = plugin.filter(|p| version_shape(p) && *p != daemon) {
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

    async fn run(&self, interval: f64, mut stop: watch::Receiver<bool>) {
        let client = match reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
        {
            Ok(client) => client,
            Err(e) => {
                eprintln!("release check client failed: {e}");
                return;
            }
        };
        loop {
            if *stop.borrow() {
                return;
            }
            let latest = tokio::select! { _ = stop.changed() => return, result = fetch_latest(&client) => result };
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
            self.record(latest, now);
            let delay = if self.snapshot().latest_release.is_none() {
                interval.min(RETRY_AFTER_FAILURE_SECONDS)
            } else {
                interval
            };
            let Ok(delay) = Duration::try_from_secs_f64(delay) else {
                return;
            };
            tokio::select! { _ = stop.changed() => return, _ = tokio::time::sleep(delay) => {} }
        }
    }
}

async fn fetch_latest(client: &reqwest::Client) -> Option<String> {
    let response = client
        .get(PYPI_JSON)
        .send()
        .await
        .ok()?
        .error_for_status()
        .ok()?;
    let data: Value = response.json().await.ok()?;
    let version = data.get("info")?.get("version")?.as_str()?;
    (version_shape(version) && version_key(version).is_some()).then(|| version.into())
}

#[cfg(test)]
mod tests {
    use super::*;
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
