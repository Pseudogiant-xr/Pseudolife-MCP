//! `GET /health` (spec section H, `daemon.py:231-409`).

use crate::config::{BuildStamp, Config};
use crate::service::Service;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::path::Path;

/// `SCHEMA_META_VERSION` (`storage/schema.py:20`).
pub const SCHEMA: i64 = 55;
const NEAR_LIMIT_FRACTION: f64 = 0.90;
const HOOK_SCRIPTS: [&str; 9] = [
    "lifecycle.ps1",
    "session-start.sh",
    "user-prompt-submit.sh",
    "coordination-start.sh",
    "coordination-prompt.sh",
    "session-end.sh",
    "stop-wake.sh",
    "subagent-board-guard.sh",
    "subagent-board.sh",
];

/// Python `round(x, 4)` (correctly rounded, half-even on the exact binary value).
pub fn round4(x: f64) -> f64 {
    format!("{x:.4}").parse().unwrap_or(x)
}

fn read_trimmed(path: &Path) -> Option<String> {
    std::fs::read(path)
        .ok()
        .and_then(|b| String::from_utf8(b).ok())
        .map(|s| s.trim().to_string())
}

fn int(text: Option<&str>) -> Option<i64> {
    text?.trim().parse().ok()
}

fn keyed_ints(text: Option<&str>) -> Vec<(String, i64)> {
    let mut out: Vec<(String, i64)> = Vec::new();
    for line in text.unwrap_or("").lines() {
        let (key, value) = line.split_once(' ').unwrap_or((line, ""));
        if let Some(v) = int(Some(value)) {
            out.retain(|(k, _)| k != key);
            out.push((key.to_string(), v));
        }
    }
    out
}

fn lookup(kv: &[(String, i64)], key: &str) -> Option<i64> {
    kv.iter().find(|(k, _)| k == key).map(|(_, v)| *v)
}

/// `utils/memory_headroom.read_memory_headroom`.
pub fn memory_headroom(cgroup_root: &Path, proc_self: &Path) -> Value {
    let mut rss = Map::new();
    if let Some(status) = read_trimmed(&proc_self.join("status")) {
        for line in status.lines() {
            let (key, rest) = line.split_once(':').unwrap_or((line, ""));
            let name = match key {
                "VmRSS" => "rss_bytes",
                "VmHWM" => "rss_peak_bytes",
                _ => continue,
            };
            if let Some(kb) = int(rest.split_whitespace().next()) {
                rss.insert(name.into(), json!(kb * 1024));
            }
        }
    }
    let mut dir = None;
    if let Some(cg) = read_trimmed(&proc_self.join("cgroup")) {
        for line in cg.lines() {
            if let Some(p) = line.strip_prefix("0::") {
                let nested = cgroup_root.join(p.trim().trim_start_matches('/'));
                if nested.join("memory.current").exists() {
                    dir = Some(nested);
                    break;
                }
            }
        }
    }
    if dir.is_none() && cgroup_root.join("memory.current").exists() {
        dir = Some(cgroup_root.to_path_buf());
    }
    let current = dir
        .as_ref()
        .and_then(|d| int(read_trimmed(&d.join("memory.current")).as_deref()));
    if let (Some(d), Some(current)) = (dir, current) {
        let limit = int(read_trimmed(&d.join("memory.max")).as_deref());
        let events = keyed_ints(read_trimmed(&d.join("memory.events")).as_deref());
        let stat = keyed_ints(read_trimmed(&d.join("memory.stat")).as_deref());
        let working_set = (current - lookup(&stat, "inactive_file").unwrap_or(0)).max(0);
        let fraction = limit
            .filter(|l| *l != 0)
            .map(|l| working_set as f64 / l as f64);
        let mut ev = Map::new();
        for k in ["max", "oom", "oom_kill"] {
            if let Some(v) = lookup(&events, k) {
                ev.insert(k.into(), json!(v));
            }
        }
        let mut out = Map::new();
        out.insert("source".into(), json!("cgroup"));
        out.insert("current_bytes".into(), json!(current));
        out.insert("working_set_bytes".into(), json!(working_set));
        out.insert("limit_bytes".into(), json!(limit));
        out.insert("used_fraction".into(), json!(fraction.map(round4)));
        out.insert(
            "near_limit".into(),
            json!(fraction.map(|f| f >= NEAR_LIMIT_FRACTION)),
        );
        out.insert("events".into(), Value::Object(ev));
        for key in ["anon", "file"] {
            if let Some(v) = lookup(&stat, key) {
                out.insert(format!("{key}_bytes"), json!(v));
            }
        }
        out.extend(rss);
        return Value::Object(out);
    }
    if !rss.is_empty() {
        let mut out = Map::new();
        out.insert("source".into(), json!("process"));
        out.insert("near_limit".into(), Value::Null);
        out.extend(rss);
        return Value::Object(out);
    }
    json!({"source": "unavailable"})
}

/// `plugin_hooks.daemon_hooks_digest` with the plugin dir from
/// `PSEUDOLIFE_PLUGIN_DIR` (the beside-the-package fallback has no Rust
/// equivalent: a declared divergence).
pub fn hooks_digest(plugin_dir: Option<&Path>) -> Option<String> {
    let dir = plugin_dir?;
    if !dir.join("hooks").join(HOOK_SCRIPTS[0]).is_file() {
        return None;
    }
    let mut digest = Sha256::new();
    for name in HOOK_SCRIPTS {
        let body = std::fs::read(dir.join("hooks").join(name)).ok()?;
        let mut normalized = Vec::with_capacity(body.len());
        let mut i = 0;
        while i < body.len() {
            if body[i] == b'\r' && body.get(i + 1) == Some(&b'\n') {
                i += 1;
                continue;
            }
            normalized.push(body[i]);
            i += 1;
        }
        digest.update(name.as_bytes());
        digest.update(b"\0");
        digest.update(&normalized);
        digest.update(b"\0");
    }
    Some(hex::encode(digest.finalize()))
}

/// `daemon._last_backup`.
pub fn last_backup(data_dir: &Path) -> Option<Value> {
    let raw = std::fs::read(data_dir.join("last-backup.json")).ok()?;
    let raw = raw.strip_prefix(b"\xef\xbb\xbf".as_slice()).unwrap_or(&raw);
    let record: Value = serde_json::from_slice(raw).ok()?;
    let at_value = record.as_object()?.get("created_at")?;
    // `str(record["created_at"])`: only a string can parse as the timestamp.
    let at = at_value.as_str()?;
    let created = strptime_backup(at)?;
    let age_s = (chrono::Utc::now().naive_utc() - created).num_microseconds()? as f64 / 1e6;
    let rotation = match record.get("rotation") {
        None => "unknown".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => py_str(other),
    };
    Some(json!({"at": at, "age_hours": round1(age_s / 3600.0), "rotation": rotation}))
}

/// `datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ")`: CPython's `_strptime`
/// field patterns (one-digit fields, a space-padded day) and its
/// case-insensitive literals; then a real-date check.
fn strptime_backup(at: &str) -> Option<chrono::NaiveDateTime> {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| {
        regex::Regex::new(
            r"(?i)\A(\d\d\d\d)-(1[0-2]|0[1-9]|[1-9])-(3[01]|[12]\d|0[1-9]|[1-9]| [1-9])T(2[0-3]|[0-1]\d|\d):([0-5]\d|\d):(6[0-1]|[0-5]\d|\d)Z\z",
        )
        .expect("valid pattern")
    });
    let c = re.captures(at)?;
    let n = |i: usize| c[i].trim().parse::<u32>().ok();
    let date = chrono::NaiveDate::from_ymd_opt(n(1)? as i32, n(2)?, n(3)?)?;
    date.and_hms_opt(n(4)?, n(5)?, n(6)?)
}

fn round1(x: f64) -> f64 {
    format!("{x:.1}").parse().unwrap_or(x)
}

/// `str(value)` for the JSON scalars a record can hold.
fn py_str(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => other.to_string(),
    }
}

fn build_json(b: &BuildStamp) -> Value {
    json!({"git_sha": b.git_sha, "dirty": b.dirty, "built_at": b.built_at, "source": b.source})
}

fn extractor(config: &Config) -> &'static str {
    if !config.dream.enabled {
        return "disabled";
    }
    let env = |k: &str| std::env::var(k).ok();
    if config.dream.extractor_configured(&env) {
        // No stall tracker in this slice: a configured extractor is never "stalled".
        "configured"
    } else {
        "none"
    }
}

/// `_build_health_payload`, in Python's key order.
pub async fn payload(svc: &Service) -> Value {
    let mut p = Map::new();
    p.insert("status".into(), json!("ok"));
    p.insert("version".into(), json!(env!("CARGO_PKG_VERSION")));
    p.insert("schema".into(), json!(SCHEMA));
    p.insert("storage".into(), json!("postgres"));
    p.insert("auth".into(), json!(svc.auth_configured));
    p.insert("bank".into(), Value::Null);
    p.insert("persist_errors".into(), json!(0));
    if let Some(b) = &svc.env.build {
        p.insert("build".into(), build_json(b));
    }
    let c = &svc.config;
    p.insert(
        "coordination".into(),
        json!({"enabled": c.coordination.enabled, "wake": c.coordination.wake.to_json()}),
    );
    // No release check runs in this slice: the runtime flag stays off and
    // nothing has been read from the package index (declared divergence).
    p.insert(
        "updates".into(),
        json!({
            "check_releases": false,
            "latest_release": null,
            "checked_at": 0.0,
            "unattended_clients": c.updates.unattended_clients,
            "unattended_daemon": c.updates.unattended_daemon,
        }),
    );
    p.insert("extractor".into(), json!(extractor(c)));
    if let Some(d) = hooks_digest(svc.env.plugin_dir.as_deref()) {
        p.insert("hooks_digest".into(), json!(d));
    }
    let snap = svc.snapshot();
    if let Some(r) = &snap.init_refusal {
        p.insert("status".into(), json!("degraded"));
        p.insert("init_refusal".into(), json!(r));
    }
    if let Some(r) = &snap.not_ready {
        p.insert("status".into(), json!("degraded"));
        p.insert("not_ready".into(), json!(r));
    }
    p.insert(
        "memory".into(),
        memory_headroom(Path::new("/sys/fs/cgroup"), Path::new("/proc/self")),
    );
    if let Some(e) = &snap.embedder {
        p.insert("embedder".into(), e.clone());
    }
    if let Some(storage) = snap
        .storage
        .as_ref()
        .filter(|_| !crate::mutants::active("health-omits-db"))
    {
        match storage.ping().await {
            Ok(()) => {
                p.insert("db".into(), json!("ok"));
                let bank = storage
                    .cached_bank_id()
                    .await
                    .filter(|id| !id.is_empty())
                    .map(|id| hex::encode(Sha256::digest(id.as_bytes()))[..16].to_string());
                p.insert("bank".into(), json!(bank));
            }
            Err(e) => {
                p.insert("status".into(), json!("degraded"));
                p.insert("db".into(), json!(format!("error: {e}")));
            }
        }
    }
    if let Some(b) = last_backup(&svc.data_dir) {
        p.insert("last_backup".into(), b);
    }
    Value::Object(p)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("pl-health-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn headroom_unavailable_process_and_cgroup_shapes() {
        let root = dir("hr");
        let (cg, proc_self) = (root.join("cg"), root.join("proc"));
        std::fs::create_dir_all(&cg).unwrap();
        std::fs::create_dir_all(&proc_self).unwrap();
        assert_eq!(
            memory_headroom(&cg, &proc_self),
            json!({"source": "unavailable"})
        );
        std::fs::write(
            proc_self.join("status"),
            "Name:\tx\nVmHWM:\t  2048 kB\nVmRSS:\t  1024 kB\n",
        )
        .unwrap();
        assert_eq!(
            memory_headroom(&cg, &proc_self),
            json!({"source": "process", "near_limit": null, "rss_peak_bytes": 2097152, "rss_bytes": 1048576})
        );
        std::fs::write(cg.join("memory.current"), "950\n").unwrap();
        std::fs::write(cg.join("memory.max"), "1000\n").unwrap();
        std::fs::write(cg.join("memory.events"), "low 0\nmax 7\noom_kill 1\n").unwrap();
        std::fs::write(
            cg.join("memory.stat"),
            "anon 600\nfile 300\ninactive_file 30\n",
        )
        .unwrap();
        let v = memory_headroom(&cg, &proc_self);
        assert_eq!(v["source"], "cgroup");
        assert_eq!(v["working_set_bytes"], 920);
        assert_eq!(v["used_fraction"], json!(0.92));
        assert_eq!(v["near_limit"], true);
        assert_eq!(v["events"], json!({"max": 7, "oom_kill": 1}));
        assert_eq!(
            (v["anon_bytes"].clone(), v["file_bytes"].clone()),
            (json!(600), json!(300))
        );
        std::fs::write(cg.join("memory.max"), "max\n").unwrap();
        let v = memory_headroom(&cg, &proc_self);
        assert_eq!(
            (v["limit_bytes"].clone(), v["near_limit"].clone()),
            (Value::Null, Value::Null)
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn hooks_digest_normalizes_crlf_and_needs_every_script() {
        let root = dir("hooks");
        let hooks = root.join("hooks");
        std::fs::create_dir_all(&hooks).unwrap();
        assert_eq!(hooks_digest(Some(&root)), None);
        for name in HOOK_SCRIPTS {
            std::fs::write(hooks.join(name), format!("echo {name}\r\n")).unwrap();
        }
        let crlf = hooks_digest(Some(&root)).unwrap();
        for name in HOOK_SCRIPTS {
            std::fs::write(hooks.join(name), format!("echo {name}\n")).unwrap();
        }
        assert_eq!(hooks_digest(Some(&root)).unwrap(), crlf);
        std::fs::remove_file(hooks.join("subagent-board.sh")).unwrap();
        assert_eq!(hooks_digest(Some(&root)), None);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn last_backup_parses_only_the_python_format() {
        let root = dir("bk");
        assert_eq!(last_backup(&root), None);
        std::fs::write(
            root.join("last-backup.json"),
            b"\xef\xbb\xbf{\"created_at\": \"2026-10-09T00:00:00Z\"}",
        )
        .unwrap();
        let v = last_backup(&root).unwrap();
        assert_eq!(
            (v["at"].clone(), v["rotation"].clone()),
            (json!("2026-10-09T00:00:00Z"), json!("unknown"))
        );
        std::fs::write(
            root.join("last-backup.json"),
            "{\"created_at\": \"2026-10-09 00:00:00\"}",
        )
        .unwrap();
        assert_eq!(last_backup(&root), None);
        // CPython strptime, probed 2026-10-09.
        let write = |at: &str| {
            std::fs::write(
                root.join("last-backup.json"),
                format!("{{\"created_at\": \"{at}\"}}"),
            )
            .unwrap()
        };
        for ok in [
            "2026-10-09t00:00:00z",
            "2026-1-9T0:0:0Z",
            "2026-10- 9T00:00:00Z",
        ] {
            write(ok);
            assert!(last_backup(&root).is_some(), "{ok}");
        }
        for bad in [
            "2026-10-09T24:00:00Z",
            "2026-10-09T23:59:60Z",
            "02026-10-09T00:00:00Z",
            "2026-10-09T00:00:00Z ",
        ] {
            write(bad);
            assert!(last_backup(&root).is_none(), "{bad}");
        }
        std::fs::write(
            root.join("last-backup.json"),
            "{\"created_at\": \"2026-10-09T00:00:00Z\", \"rotation\": true}",
        )
        .unwrap();
        assert_eq!(last_backup(&root).unwrap()["rotation"], "True");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn round4_matches_python_round() {
        assert_eq!(round4(0.92), 0.92);
        assert_eq!(round4(0.123_45), 0.1235); // 0.12345 is above the tie in binary
        assert_eq!(round4(2.0 / 3.0), 0.6667);
    }
}
