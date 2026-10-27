//! The CMS counters row (`cms_counters`, schema v57; maintainer decision D2):
//! what `weights.pt` held before, persisted in the bank by autosave and the
//! clean-exit flush and restored at startup (`service.py` `_load_counters`,
//! `_persist_all`; `postgres.py` `cms_counters_get` / `cms_counters_put`;
//! `cms.py` `counters_state`, `apply_counters`, `reset_counters`).
//!
//! The Rust daemon reads and writes the row only. A pre-v57 `weights.pt` is
//! a torch file this daemon does not read (D2: no port of the `.pt`
//! format); finding one beside a bank with no row is reported, never
//! imported (`pending_import`), and the Python daemon imports it.

use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use tokio_postgres::Client;

/// The row as `cms_counters_get` returns it.
#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub preset_name: String,
    pub interaction_count: i64,
    pub logical_turn_count: i64,
    pub tier_queries: i64,
    pub tier_hits: Value,
    pub surprise_history: Value,
    pub consolidation_events: Value,
    pub updated_at: f64,
}

/// The counters this daemon does not maintain yet, carried from the row so
/// a save writes back what it read instead of zeroing them
/// (`_logical_turn_count`, `_consolidation_events`).
#[derive(Debug, Clone)]
pub struct Carry {
    pub logical_turn_count: i64,
    pub consolidation_events: Value,
}

impl Default for Carry {
    fn default() -> Self {
        Carry {
            logical_turn_count: 0,
            consolidation_events: json!([]),
        }
    }
}

/// `cms_counters_get` (postgres.py:2118-2126): the row, or `None` when the
/// bank has none yet.
pub async fn get(client: &Client) -> Result<Option<Row>, tokio_postgres::Error> {
    let row = client
        .query_opt(
            "SELECT preset_name, interaction_count, logical_turn_count, tier_queries, \
             tier_hits, surprise_history, consolidation_events, updated_at \
             FROM cms_counters WHERE id = 1",
            &[],
        )
        .await?;
    Ok(row.map(|r| Row {
        preset_name: r.get(0),
        interaction_count: r.get(1),
        logical_turn_count: r.get(2),
        tier_queries: r.get(3),
        tier_hits: r.get(4),
        surprise_history: r.get(5),
        consolidation_events: r.get(6),
        updated_at: r.get(7),
    }))
}

/// `counters_state()` (cms.py:2294-2313) for this daemon's resident
/// counters: every current band has a hit count and a history (zero and
/// empty when it has none), as `reset_counters` seeds them, plus any band
/// the restored row named.
pub fn state(
    preset: &str,
    bands: &[String],
    interaction_count: u64,
    surprise_history: &HashMap<String, Vec<f64>>,
    tier: (u64, BTreeMap<String, u64>),
    carry: &Carry,
) -> Row {
    let (tier_queries, mut hits) = tier;
    let mut history: BTreeMap<String, Vec<f64>> = surprise_history
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    for band in bands {
        hits.entry(band.clone()).or_insert(0);
        history.entry(band.clone()).or_default();
    }
    Row {
        preset_name: preset.to_string(),
        interaction_count: interaction_count as i64,
        logical_turn_count: carry.logical_turn_count,
        tier_queries: tier_queries as i64,
        tier_hits: json!(hits),
        surprise_history: json!(history),
        consolidation_events: carry.consolidation_events.clone(),
        updated_at: 0.0,
    }
}

/// What `apply_counters` (cms.py:2325-2337) restores from a row: per-band
/// maps merge over the current bands (a band the row does not name keeps
/// its value; one it names that the preset no longer has is kept).
pub struct Applied {
    pub interaction_count: u64,
    pub surprise_history: HashMap<String, Vec<f64>>,
    pub tier_queries: u64,
    pub tier_hits: Vec<(String, u64)>,
    pub carry: Carry,
}

pub fn apply(row: &Row, mut surprise_history: HashMap<String, Vec<f64>>) -> Applied {
    if let Some(map) = row.surprise_history.as_object() {
        for (band, values) in map {
            let values = values
                .as_array()
                .map(|a| a.iter().filter_map(Value::as_f64).collect())
                .unwrap_or_default();
            surprise_history.insert(band.clone(), values);
        }
    }
    let tier_hits = row
        .tier_hits
        .as_object()
        .map(|m| {
            m.iter()
                .filter_map(|(k, v)| v.as_u64().map(|n| (k.clone(), n)))
                .collect()
        })
        .unwrap_or_default();
    Applied {
        interaction_count: row.interaction_count.max(0) as u64,
        surprise_history,
        tier_queries: row.tier_queries.max(0) as u64,
        tier_hits,
        carry: Carry {
            logical_turn_count: row.logical_turn_count,
            consolidation_events: if row.consolidation_events.is_null() {
                json!([])
            } else {
                row.consolidation_events.clone()
            },
        },
    }
}

/// `cms_counters_put` (postgres.py:2128-2150): replace the row (`id = 1`),
/// stamped with the wall clock.
pub async fn put(client: &Client, row: &Row) -> Result<(), tokio_postgres::Error> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    crate::txn::run(client, |c| async move {
        c.execute(
            "INSERT INTO cms_counters (id, preset_name, interaction_count, \
             logical_turn_count, tier_queries, tier_hits, surprise_history, \
             consolidation_events, updated_at) \
             VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8) \
             ON CONFLICT (id) DO UPDATE SET \
             preset_name = EXCLUDED.preset_name, \
             interaction_count = EXCLUDED.interaction_count, \
             logical_turn_count = EXCLUDED.logical_turn_count, \
             tier_queries = EXCLUDED.tier_queries, \
             tier_hits = EXCLUDED.tier_hits, \
             surprise_history = EXCLUDED.surprise_history, \
             consolidation_events = EXCLUDED.consolidation_events, \
             updated_at = EXCLUDED.updated_at",
            &[
                &row.preset_name,
                &row.interaction_count,
                &row.logical_turn_count,
                &row.tier_queries,
                &row.tier_hits,
                &row.surprise_history,
                &row.consolidation_events,
                &now,
            ],
        )
        .await
        .map(|_| ())
    })
    .await
}

/// A pre-v57 `weights.pt` (or its `.bak`) beside a bank with no counters
/// row: the counters it holds would be lost if this daemon saved fresh ones
/// over them. Python's `_load_counters` imports it; this daemon reports the
/// step instead (D2, read as the maintainer's coordinator ruled on
/// 2026-10-27). `None` once the row exists or no such file is there.
pub fn pending_import(save_dir: &Path, has_row: bool) -> Option<String> {
    if has_row {
        return None;
    }
    let found = ["weights.pt", "weights.pt.bak"]
        .iter()
        .map(|n| save_dir.join(n))
        .find(|p| p.exists())?;
    Some(format!(
        "{} holds this bank's CMS counters, which the Rust daemon cannot import; \
         start the Python daemon (0.19 or later) once on this bank to import them \
         into cms_counters, then restart this daemon",
        found.display()
    ))
}

/// The `(entries, superseded, cortex)` signature `_entry_fingerprint`
/// (service.py:3660-3685) gates autosave on: it changes on store, delete,
/// supersede, consolidate and fact writes, not on reads.
pub type Fingerprint = (usize, usize, (usize, usize, i64));

pub fn fingerprint(ready: &crate::service::Ready) -> Fingerprint {
    let (total, superseded) = {
        let bank = ready.bank.read().expect("bank lock");
        let superseded = bank
            .entries
            .iter()
            .filter(|e| e.superseded_at.is_some())
            .count();
        (bank.entries.len(), superseded)
    };
    let cortex = ready.cortex.snapshot();
    let confirmed: f64 = cortex.records().iter().map(|r| r.last_confirmed).sum();
    // `round(sum, 3)`, kept as thousandths so the signature compares exactly.
    let confirmed = (confirmed * 1000.0).round() as i64;
    (
        total,
        superseded,
        (
            cortex.records().len(),
            cortex.supersession_log.len(),
            confirmed,
        ),
    )
}

/// The row as a JSON object, for logs and tests.
#[allow(dead_code)]
pub fn to_json(row: &Row) -> Value {
    let mut m = Map::new();
    m.insert("preset_name".into(), json!(row.preset_name));
    m.insert("interaction_count".into(), json!(row.interaction_count));
    m.insert("logical_turn_count".into(), json!(row.logical_turn_count));
    m.insert("tier_queries".into(), json!(row.tier_queries));
    m.insert("tier_hits".into(), row.tier_hits.clone());
    m.insert("surprise_history".into(), row.surprise_history.clone());
    m.insert(
        "consolidation_events".into(),
        row.consolidation_events.clone(),
    );
    Value::Object(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bands() -> Vec<String> {
        vec!["flat".into()]
    }

    #[test]
    fn a_fresh_state_names_every_band_with_zero_and_empty() {
        let row = state(
            "flat",
            &bands(),
            0,
            &HashMap::new(),
            (0, BTreeMap::new()),
            &Carry::default(),
        );
        assert_eq!(
            to_json(&row),
            json!({"preset_name": "flat", "interaction_count": 0, "logical_turn_count": 0,
                   "tier_queries": 0, "tier_hits": {"flat": 0},
                   "surprise_history": {"flat": []}, "consolidation_events": []})
        );
    }

    #[test]
    fn applying_a_row_merges_per_band_maps_and_carries_the_rest() {
        let row = Row {
            preset_name: "continuum".into(),
            interaction_count: 42,
            logical_turn_count: 7,
            tier_queries: 9,
            tier_hits: json!({"old_band": 3, "flat": 5}),
            surprise_history: json!({"old_band": [0.5], "flat": [0.25, 0.75]}),
            consolidation_events: json!([{"band": "x", "at": 1.0}]),
            updated_at: 100.0,
        };
        let mut resident = HashMap::new();
        resident.insert("other".to_string(), vec![0.1]);
        let applied = apply(&row, resident);
        assert_eq!(applied.interaction_count, 42);
        assert_eq!(applied.tier_queries, 9);
        assert_eq!(applied.surprise_history["flat"], vec![0.25, 0.75]);
        assert_eq!(applied.surprise_history["other"], vec![0.1], "kept");
        assert_eq!(applied.surprise_history["old_band"], vec![0.5], "kept");
        let mut tier = BTreeMap::new();
        for (k, v) in &applied.tier_hits {
            tier.insert(k.clone(), *v);
        }
        let back = state(
            "flat",
            &bands(),
            applied.interaction_count,
            &applied.surprise_history,
            (applied.tier_queries, tier),
            &applied.carry,
        );
        assert_eq!(back.logical_turn_count, 7, "carried, never zeroed");
        assert_eq!(back.consolidation_events, row.consolidation_events);
        assert_eq!(back.tier_hits, json!({"flat": 5, "old_band": 3}));
        assert_eq!(back.preset_name, "flat", "the running preset is written");
    }

    #[test]
    fn a_weights_file_beside_a_row_less_bank_is_reported() {
        let dir = std::env::temp_dir().join(format!("pl-counters-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(pending_import(&dir, false), None);
        std::fs::write(dir.join("weights.pt.imported"), b"x").unwrap();
        assert_eq!(pending_import(&dir, false), None, "already imported");
        std::fs::write(dir.join("weights.pt.bak"), b"x").unwrap();
        let message = pending_import(&dir, false).expect("reported");
        assert!(message.contains("weights.pt.bak"), "{message}");
        assert_eq!(pending_import(&dir, true), None, "the row wins");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
