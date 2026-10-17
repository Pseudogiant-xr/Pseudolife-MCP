//! `memory_stats` over MCP (spec "memory_stats over MCP"): the bank's vital
//! signs, as `MemoryService.stats` (service.py:3402-3480) serves them over
//! `ContinuumMemorySystem.stats` (memory/cms.py:2811-2875).
//!
//! The process-local counters live here as statics: like Python's, they
//! start at zero with the process and count only what this process did.

use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};

/// `CAPACITY_WARNING_FRACTION` (memory/cms.py:162).
const CAPACITY_WARNING_FRACTION: f64 = 0.8;

/// `_tier_queries` (retrievals that counted access) and `_tier_hits` (per
/// band, the retrievals whose candidate pool held it), under one lock so a
/// snapshot never pairs a query count with hits from a later query.
struct Tier {
    queries: u64,
    hits: BTreeMap<String, u64>,
}
static TIER: Mutex<Tier> = Mutex::new(Tier {
    queries: 0,
    hits: BTreeMap::new(),
});
/// `_true_drops`: capacity evictions out of the last band.
static TRUE_DROPS: AtomicU64 = AtomicU64::new(0);
/// `_retrieval_log_errors`: swallowed retrieval-log write failures.
static RETRIEVAL_LOG_ERRORS: AtomicU64 = AtomicU64::new(0);
/// `_slot_read_errors`: swallowed slot-read telemetry failures.
static SLOT_READ_ERRORS: AtomicU64 = AtomicU64::new(0);

/// `cms.retrieve` with `count_access` (cms.py:1430-1437): one query, and one
/// hit for each band that contributed a candidate to the pool.
pub fn record_query<'a>(hit_bands: impl IntoIterator<Item = &'a str>) {
    let mut tier = TIER.lock().expect("tier lock");
    tier.queries += 1;
    if crate::mutants::active("mcp-stats-no-tier-hits") {
        return;
    }
    for band in hit_bands {
        *tier.hits.entry(band.to_string()).or_insert(0) += 1;
    }
}

/// `_on_band_evict` for the last band (cms.py:2748).
pub fn record_true_drop() {
    TRUE_DROPS.fetch_add(1, Ordering::Relaxed);
}

/// `self._retrieval_log_errors += 1`.
pub fn record_retrieval_log_error() {
    RETRIEVAL_LOG_ERRORS.fetch_add(1, Ordering::Relaxed);
}

/// `self._slot_read_errors += 1`.
pub fn record_slot_read_error() {
    SLOT_READ_ERRORS.fetch_add(1, Ordering::Relaxed);
}

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![("memory_stats", mcp_memory_stats)]
}

/// `memory_stats` (mcp_server.py:1423-1428): `service.stats()` as is.
fn mcp_memory_stats<'a>(app: &'a crate::http::App, _call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        stats(app)
            .await
            .map(Value::Object)
            .map_err(ToolError::Internal)
    })
}

/// Python `round(x, n)`: correctly rounded from the exact binary value,
/// ties to even.
fn py_round(x: f64, n: usize) -> f64 {
    format!("{x:.n$}").parse().unwrap_or(x)
}

/// `f"{n:,}"`.
fn grouped(n: usize) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// Python truthiness of a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|x| x != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python `int(v)` for what a JSON meta value can hold; `None` where Python
/// raises. Strings take the ASCII integer spellings only.
fn py_int(v: &Value) -> Option<Value> {
    match v {
        Value::Bool(b) => Some(json!(i64::from(*b))),
        Value::Number(n) => {
            let text = n.to_string();
            if !text.contains(['.', 'e', 'E']) {
                return Some(Value::Number(n.clone()));
            }
            let x = n.as_f64().filter(|x| x.is_finite())?.trunc();
            if x == 0.0 {
                return Some(json!(0));
            }
            serde_json::from_str(&format!("{x:.0}")).ok()
        }
        Value::String(s) => {
            let t = s.trim();
            let body = t.strip_prefix(['+', '-']).unwrap_or(t);
            let ok = !body.is_empty()
                && !body.starts_with('_')
                && !body.ends_with('_')
                && !body.contains("__")
                && body.chars().all(|c| c.is_ascii_digit() || c == '_');
            if !ok {
                return None;
            }
            let digits: String = t.chars().filter(|&c| c != '_').collect();
            let digits = digits.strip_prefix('+').unwrap_or(&digits);
            let neg = digits.starts_with('-');
            let mag = digits.trim_start_matches('-').trim_start_matches('0');
            let canon = match (neg, mag.is_empty()) {
                (_, true) => "0".to_string(),
                (true, false) => format!("-{mag}"),
                (false, false) => mag.to_string(),
            };
            serde_json::from_str(&canon).ok()
        }
        _ => None,
    }
}

/// `capacity_warning()` (cms.py:2774-2809): the last band near capacity.
fn capacity_warning(name: &str, size: usize, capacity: usize) -> Value {
    if capacity == 0 || crate::mutants::active("mcp-stats-no-capacity-warning") {
        return Value::Null;
    }
    let fill = size as f64 / capacity as f64;
    if fill < CAPACITY_WARNING_FRACTION {
        return Value::Null;
    }
    let state = if size >= capacity {
        "is full: every new memory now permanently deletes the lowest-retention entry".to_string()
    } else {
        // `{fill:.0%}`: the float times 100, formatted as `.0f`.
        format!(
            "is {:.0}% full: at capacity every new memory will permanently delete the \
             lowest-retention entry",
            fill * 100.0
        )
    };
    json!({
        "band": name,
        "size": size,
        "capacity": capacity,
        "fill": py_round(fill, 3),
        "message": format!(
            "Band {} holds {} of {} entries and {} (superseded entries first). Raise its \
             max_entries with a custom preset — see 'MIRAS preset flat' in \
             docs/guide/configuration.md.",
            crate::storage::py_repr(name),
            grouped(size),
            grouped(capacity),
            state
        ),
    })
}

/// `service.stats()`: the CMS block, then the Postgres block.
async fn stats(app: &crate::http::App) -> anyhow::Result<Map<String, Value>> {
    let ready = app
        .service
        .ensure_init()
        .await
        .map_err(|e| anyhow::anyhow!(e))?;
    let cfg = &app.service.config.memory;
    let specs = &cfg.band_specs;
    let Some(last) = specs.last() else {
        anyhow::bail!("no memory bands configured");
    };
    // Entries carry their band's name; a repeated custom name seats in the
    // last band that bears it (startup seating, as Python's name lookup),
    // so the earlier ones hold none.
    let sizes: Vec<usize> = {
        let bank = ready.bank.read().expect("bank lock");
        specs
            .iter()
            .enumerate()
            .map(|(i, s)| {
                if specs.iter().rposition(|o| o.name == s.name) == Some(i) {
                    bank.entries.iter().filter(|e| e.band == s.name).count()
                } else {
                    0
                }
            })
            .collect()
    };
    let (queries, hits) = {
        let tier = TIER.lock().expect("tier lock");
        (tier.queries, tier.hits.clone())
    };
    let interaction_count = ready.write.lock().await.interaction_count;

    let mut out = Map::new();
    let bands: Vec<Value> = specs
        .iter()
        .zip(&sizes)
        .map(|(s, &size)| {
            let h = hits.get(&s.name).copied().unwrap_or(0);
            json!({
                "name": s.name,
                "size": size,
                "capacity": s.max_entries,
                "update_interval": s.update_interval,
                "retention_policy": s.retention_policy,
                "hit_rate": py_round(h as f64 / queries.max(1) as f64, 4),
                "hit_count": h,
            })
        })
        .collect();
    out.insert("bands".into(), Value::Array(bands));
    out.insert("preset".into(), json!(cfg.preset));
    // `+ reference.size`: no reference bank is read (divergences.md).
    out.insert("total_memories".into(), json!(sizes.iter().sum::<usize>()));
    out.insert("interaction_count".into(), json!(interaction_count));
    out.insert("logical_turn_count".into(), json!(0));
    out.insert("retrieval_queries".into(), json!(queries));
    out.insert(
        "true_drops".into(),
        json!(TRUE_DROPS.load(Ordering::Relaxed)),
    );
    out.insert(
        "capacity_warning".into(),
        capacity_warning(&last.name, sizes[sizes.len() - 1], last.max_entries),
    );
    out.insert("weights_reset".into(), json!(false));
    out.insert("slot_index_shadow_divergences".into(), json!(0));
    // The v0.4 aliases (cms.py:253-258): by name, else by position; the
    // name lookup is a dict, so a repeated name resolves to its last band.
    let named = |n: &str| specs.iter().rposition(|s| s.name == n);
    let instant = named("instant").unwrap_or(0);
    let short_term = named("short_term").unwrap_or(usize::from(specs.len() > 1));
    let long_term = named("long_term").unwrap_or(specs.len() - 1);
    for (key, i) in [
        ("instant", instant),
        ("short_term", short_term),
        ("long_term", long_term),
    ] {
        out.insert(format!("{key}_bank_size"), json!(sizes[i]));
        out.insert(format!("{key}_bank_capacity"), json!(specs[i].max_entries));
    }
    out.insert("reference_bank_size".into(), json!(0));
    out.insert("reference_document_count".into(), json!(0));

    let enabled = cfg.retrieval_log_enabled;
    let read_tracking = cfg.cortex.read_tracking;
    let db = ready.storage.client();
    let tail = crate::txn::with_client(db, |db| async move {
        let mut tail = Map::new();
        let communities: i64 = db
            .query_one("SELECT count(*) FROM communities", &[])
            .await?
            .get(0);
        tail.insert("communities".into(), json!(communities));
        let digest = meta(db, "graph_digest").await?;
        let digest_at = match digest {
            Some(v) if truthy(&v) => match v {
                Value::Object(m) => m.get("computed_at").cloned().unwrap_or(Value::Null),
                // `.get` on a non-dict: Python raises out of stats().
                _ => anyhow::bail!("graph_digest meta value is not an object"),
            },
            _ => Value::Null,
        };
        tail.insert("graph_digest_at".into(), digest_at);
        let (total, last_drop) = true_drops(db).await;
        tail.insert("true_drops_total".into(), total);
        tail.insert("last_true_drop".into(), last_drop);
        let mut log = match retrieval_log_health(db).await {
            Ok(m) => m,
            Err(e) => {
                eprintln!("retrieval-log health read failed: {e:#}");
                let Value::Object(m) = json!({
                    "events": null, "uses": null, "lesson_searches": null,
                    "last_event_at": null, "unavailable": true,
                }) else {
                    unreachable!()
                };
                m
            }
        };
        log.insert("enabled".into(), json!(enabled));
        log.insert(
            "write_errors".into(),
            json!(RETRIEVAL_LOG_ERRORS.load(Ordering::Relaxed)),
        );
        tail.insert("retrieval_log".into(), Value::Object(log));
        let now = crate::episodes::now();
        let mut audit = match read_audit(db, now).await {
            Ok(m) => m,
            Err(e) => {
                eprintln!("read-audit computation failed: {e:#}");
                let mut m = Map::new();
                m.insert("unavailable".into(), json!(true));
                m
            }
        };
        let graduation = match graduation_report(db, now).await {
            Ok(rows) => rows,
            Err(e) => {
                eprintln!("graduation report failed: {e:#}");
                Vec::new()
            }
        };
        audit.insert("graduation_candidates".into(), Value::Array(graduation));
        audit.insert("slot_tracking_enabled".into(), json!(read_tracking));
        audit.insert(
            "slot_read_write_errors".into(),
            json!(SLOT_READ_ERRORS.load(Ordering::Relaxed)),
        );
        tail.insert("read_audit".into(), Value::Object(audit));
        Ok::<_, anyhow::Error>(tail)
    })
    .await?;
    out.extend(tail);
    Ok(out)
}

async fn meta(db: &tokio_postgres::Client, key: &str) -> anyhow::Result<Option<Value>> {
    Ok(db
        .query_opt("SELECT value FROM meta WHERE key = $1", &[&key])
        .await?
        .map(|r| r.get(0)))
}

/// `true_drops_total` and `last_true_drop` (service.py:3412-3433), from the
/// durable `capacity_true_drops` record; any failure serves both as null.
async fn true_drops(db: &tokio_postgres::Client) -> (Value, Value) {
    let read = async {
        let v = meta(db, "capacity_true_drops").await?;
        let drops = match v {
            Some(v) if truthy(&v) => v,
            _ => json!({}),
        };
        let Value::Object(m) = &drops else {
            anyhow::bail!("capacity drop record is not an object");
        };
        let count = m.get("count").cloned().unwrap_or(json!(0));
        let total = py_int(&count).ok_or_else(|| anyhow::anyhow!("count is not an integer"))?;
        let last = if m.is_empty() {
            Value::Null
        } else {
            let get = |k: &str| m.get(k).cloned().unwrap_or(Value::Null);
            json!({
                "at": get("last_at"),
                "entry_id": get("last_entry_id"),
                "source": get("last_source"),
                "superseded": get("last_superseded"),
            })
        };
        Ok::<_, anyhow::Error>((total, last))
    };
    match read.await {
        Ok(pair) => pair,
        Err(e) => {
            eprintln!("capacity drop record unreadable: {e:#}");
            (Value::Null, Value::Null)
        }
    }
}

/// `retrieval_log_health` (storage/postgres.py:2451-2479).
async fn retrieval_log_health(db: &tokio_postgres::Client) -> anyhow::Result<Map<String, Value>> {
    let ev = db
        .query_one(
            "SELECT COUNT(*), MAX(created_at) FROM retrieval_events",
            &[],
        )
        .await?;
    let uses: i64 = db
        .query_one("SELECT COUNT(*) FROM retrieval_uses", &[])
        .await?
        .get(0);
    let lessons: i64 = db
        .query_one("SELECT COUNT(*) FROM lesson_search_events", &[])
        .await?
        .get(0);
    let Value::Object(m) = json!({
        "events": ev.get::<_, i64>(0),
        "last_event_at": ev.get::<_, Option<f64>>(1),
        "uses": uses,
        "lesson_searches": lessons,
    }) else {
        unreachable!()
    };
    Ok(m)
}

/// `read_audit` (storage/postgres.py:2481-2575): never-read fractions and
/// the read/write balance. Sums are cast to bigint so each decodes as one
/// type; the values are unchanged.
async fn read_audit(db: &tokio_postgres::Client, t: f64) -> anyhow::Result<Map<String, Value>> {
    let agg = db
        .query_one(
            "SELECT count(*), count(*) FILTER (WHERE access_count = 0), \
             COALESCE(sum(access_count), 0)::bigint, \
             COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY access_count), 0) \
             FROM entries",
            &[],
        )
        .await?;
    let (total, never, reads_total, median): (i64, i64, i64, f64) =
        (agg.get(0), agg.get(1), agg.get(2), agg.get(3));
    let mut by_age = Map::new();
    let d14 = t - 14.0 * 86400.0;
    let d45 = t - 45.0 * 86400.0;
    for (key, sql, params) in [
        ("lt_14d", "ts >= $1", vec![d14]),
        ("d14_45", "ts >= $1 AND ts < $2", vec![d45, d14]),
        ("gt_45d", "ts < $1", vec![d45]),
    ] {
        let refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> = params
            .iter()
            .map(|p| p as &(dyn tokio_postgres::types::ToSql + Sync))
            .collect();
        let row = db
            .query_one(
                &format!(
                    "SELECT count(*), count(*) FILTER (WHERE access_count = 0) \
                     FROM entries WHERE {sql}"
                ),
                &refs,
            )
            .await?;
        by_age.insert(
            key.into(),
            json!({"n": row.get::<_, i64>(0), "never_read": row.get::<_, i64>(1)}),
        );
    }
    let worst: Vec<Value> = db
        .query(
            "SELECT source, count(*) AS n, \
             count(*) FILTER (WHERE access_count = 0) AS never \
             FROM entries GROUP BY source HAVING count(*) >= 5 \
             ORDER BY count(*) FILTER (WHERE access_count = 0)::float \
             / count(*) DESC LIMIT 5",
            &[],
        )
        .await?
        .iter()
        .map(|r| {
            let (n, never): (i64, i64) = (r.get(1), r.get(2));
            json!({
                "source": r.get::<_, String>(0),
                "n": n,
                "never_read": never,
                "never_read_pct": py_round(100.0 * never as f64 / n as f64, 1),
            })
        })
        .collect();
    let top_share: i64 = db
        .query_one(
            "SELECT COALESCE(sum(access_count), 0)::bigint FROM (\
             SELECT access_count FROM entries ORDER BY access_count DESC \
             LIMIT GREATEST(1, (SELECT count(*) / 10 FROM entries))) d",
            &[],
        )
        .await?
        .get(0);
    let slots = db
        .query_one(
            "SELECT (SELECT count(DISTINCT (entity_norm, attribute_norm)) \
             FROM facts WHERE status = 'current'), \
             (SELECT count(*) FROM slot_reads), \
             (SELECT COALESCE(sum(read_count), 0)::bigint FROM slot_reads)",
            &[],
        )
        .await?;
    let reinf = db
        .query_one(
            "SELECT COALESCE(sum(reinforcements), 0)::bigint, \
             COALESCE(sum(explicit_reinforcements), 0)::bigint FROM entries",
            &[],
        )
        .await?;
    let never_read_pct = if total != 0 {
        py_round(100.0 * never as f64 / total as f64, 1)
    } else {
        0.0
    };
    let top_decile = if reads_total != 0 {
        json!(py_round(top_share as f64 / reads_total as f64, 3))
    } else {
        Value::Null
    };
    let Value::Object(m) = json!({
        "entries": {
            "total": total,
            "never_read": never,
            "never_read_pct": never_read_pct,
            "reads_total": reads_total,
            "reads_median": median,
            "top_decile_read_share": top_decile,
            "by_age": by_age,
        },
        "worst_sources": worst,
        "slots": {
            "current_slots": slots.get::<_, i64>(0),
            "slots_read": slots.get::<_, i64>(1),
            "reads_total": slots.get::<_, i64>(2),
        },
        "reinforcements": {
            "total": reinf.get::<_, i64>(0),
            "explicit": reinf.get::<_, i64>(1),
        },
    }) else {
        unreachable!()
    };
    Ok(m)
}

/// `graduation_report` with its defaults (storage/postgres.py:2325-2381):
/// entries served in at least 60% of the distinct sessions that searched in
/// the last 30 days, once there are at least 8 such sessions.
async fn graduation_report(db: &tokio_postgres::Client, t: f64) -> anyhow::Result<Vec<Value>> {
    if crate::mutants::active("mcp-stats-no-graduation") {
        return Ok(Vec::new());
    }
    let (window_days, min_sessions, min_share, limit) = (30.0f64, 8i64, 0.6f64, 10i64);
    let rows = db
        .query(
            "WITH ev AS (\
               SELECT id, session_id, served FROM retrieval_events \
               WHERE created_at >= $1 AND session_id IS NOT NULL\
             ), \
             tot AS (SELECT COUNT(DISTINCT session_id) AS n FROM ev), \
             hits AS (\
               SELECT DISTINCT (elem->>'entry_id')::bigint AS entry_id, ev.session_id \
               FROM ev, LATERAL jsonb_array_elements(ev.served) AS elem \
               WHERE elem ? 'entry_id'\
             ) \
             SELECT h.entry_id, COUNT(*) AS sessions_served, t.n, \
                    e.source, e.access_count, e.text \
             FROM hits h CROSS JOIN tot t JOIN entries e ON e.id = h.entry_id \
             WHERE t.n >= $2 \
             GROUP BY h.entry_id, t.n, e.source, e.access_count, e.text \
             HAVING COUNT(*) >= $3::float8 * t.n \
             ORDER BY COUNT(*) DESC, h.entry_id \
             LIMIT $4",
            &[
                &(t - window_days * 86400.0),
                &min_sessions,
                &min_share,
                &limit,
            ],
        )
        .await?;
    Ok(rows
        .iter()
        .map(|r| {
            let (served, total): (i64, i64) = (r.get(1), r.get(2));
            let text: Option<String> = r.get(5);
            json!({
                "entry_id": r.get::<_, i64>(0),
                "sessions_served": served,
                "sessions_total": total,
                "share": py_round(served as f64 / total as f64, 3),
                "source": r.get::<_, String>(3),
                "access_count": r.get::<_, i32>(4),
                "text": text.unwrap_or_default().chars().take(200).collect::<String>(),
            })
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grouping_and_rounding_match_python() {
        assert_eq!(grouped(0), "0");
        assert_eq!(grouped(999), "999");
        assert_eq!(grouped(5250), "5,250");
        assert_eq!(grouped(1234567), "1,234,567");
        assert_eq!(py_round(0.125, 2), 0.12);
        assert_eq!(py_round(2.0 / 3.0, 4), 0.6667);
        assert_eq!(py_round(0.8, 3), 0.8);
    }

    #[test]
    fn the_capacity_warning_names_both_states() {
        assert_eq!(capacity_warning("flat", 4199, 5250), Value::Null);
        let w = capacity_warning("flat", 4200, 5250);
        assert_eq!(w["fill"], json!(0.8));
        assert_eq!(
            w["message"],
            json!(
                "Band 'flat' holds 4,200 of 5,250 entries and is 80% full: at capacity every \
                 new memory will permanently delete the lowest-retention entry (superseded \
                 entries first). Raise its max_entries with a custom preset — see 'MIRAS \
                 preset flat' in docs/guide/configuration.md."
            )
        );
        let full = capacity_warning("flat", 5250, 5250);
        assert!(
            full["message"]
                .as_str()
                .unwrap()
                .contains("and is full: every new memory now permanently deletes")
        );
        assert_eq!(capacity_warning("flat", 3, 0), Value::Null);
    }

    #[test]
    fn int_follows_python_for_meta_values() {
        assert_eq!(py_int(&json!(7)), Some(json!(7)));
        assert_eq!(py_int(&json!(7.9)), Some(json!(7)));
        assert_eq!(py_int(&json!(-7.9)), Some(json!(-7)));
        assert_eq!(py_int(&json!(-0.5)), Some(json!(0)));
        assert_eq!(py_int(&json!(true)), Some(json!(1)));
        assert_eq!(py_int(&json!(" 1_000 ")), Some(json!(1000)));
        assert_eq!(py_int(&json!("-007")), Some(json!(-7)));
        assert_eq!(py_int(&json!("3.0")), None);
        assert_eq!(py_int(&json!("1__0")), None);
        assert_eq!(py_int(&Value::Null), None);
        assert_eq!(py_int(&json!([1])), None);
    }

    #[test]
    fn truthiness_follows_python() {
        for v in [
            json!(null),
            json!(0),
            json!(0.0),
            json!(""),
            json!([]),
            json!({}),
            json!(false),
        ] {
            assert!(!truthy(&v), "{v}");
        }
        for v in [
            json!(1),
            json!("x"),
            json!([0]),
            json!({"a": 1}),
            json!(true),
        ] {
            assert!(truthy(&v), "{v}");
        }
    }
}
