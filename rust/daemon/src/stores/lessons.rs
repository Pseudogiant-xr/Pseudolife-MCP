//! The lesson store (spec LS1-LS10), read side: slot-keyed procedural
//! memory keyed by `(task, aspect)`, hydrated from `lessons`. Python:
//! `memory/lessons.py` (`LessonRecord`, `LessonStore.current_records`,
//! `search`); `storage/sync.py` `hydrate_lessons` (474-505);
//! `storage/postgres.py` `load_lessons` (1998-2008). The dream writes
//! lessons; no Rust path writes them yet, so the store is immutable after
//! init.

use std::sync::Arc;

use anyhow::Result;
use serde_json::Value;
use tokio_postgres::Client;

use crate::stores::cortex::{SlotKey, dot_f32, slot_key, unit_f32};

#[derive(Debug, Clone, PartialEq)]
pub struct LessonRecord {
    /// The task type.
    pub entity: String,
    /// The aspect.
    pub attribute: String,
    /// The lesson text.
    pub value: String,
    pub about: Option<String>,
    pub polarity: String,
    pub outcome: String,
    pub confidence: f64,
    pub status: String,
    pub origin: Option<String>,
    pub support: Vec<String>,
    pub provenance: Vec<String>,
    pub asserted_at: f64,
    pub last_confirmed: f64,
    pub supersedes_value: Option<String>,
    pub superseded_by_value: Option<String>,
    pub superseded_at: Option<f64>,
    pub key: SlotKey,
    /// `embedding / (||embedding|| + 1e-12)` in f32, as `search` normalises.
    pub unit: Option<Arc<[f32]>>,
}

#[derive(Debug, Clone, Default)]
pub struct LessonStore {
    pub records: Vec<LessonRecord>,
}

impl LessonStore {
    /// `current_records`: every current lesson, in record order.
    pub fn current_records(&self) -> impl Iterator<Item = &LessonRecord> {
        self.records.iter().filter(|r| r.status == "current")
    }

    /// `search`: current lessons with an embedding, cosine against the
    /// normalised query in f32, at or above `min_score`, stably sorted by
    /// descending score, cut to `top_k`. A stored vector of another length
    /// fails, as torch's stack and matmul raise.
    pub fn search(&self, q: &[f32], top_k: usize, min_score: f64) -> Result<Vec<(usize, f64)>> {
        if let Some(r) = self
            .current_records()
            .find(|r| r.unit.as_deref().is_some_and(|u| u.len() != q.len()))
        {
            anyhow::bail!(
                "lesson {}/{} has a {}-dim vector, the query {}",
                r.entity,
                r.attribute,
                r.unit.as_deref().map_or(0, <[f32]>::len),
                q.len()
            );
        }
        let qn = unit_f32(q);
        let mut scored: Vec<(usize, f64)> = self
            .records
            .iter()
            .enumerate()
            .filter(|(_, r)| r.status == "current")
            .filter_map(|(i, r)| r.unit.as_deref().map(|u| (i, dot_f32(u, &qn) as f64)))
            .filter(|(_, s)| *s >= min_score)
            .collect();
        scored.sort_by(|a, b| b.1.total_cmp(&a.1));
        scored.truncate(top_k);
        Ok(scored)
    }
}

fn json_strings(v: Option<Value>) -> Vec<String> {
    match v {
        Some(Value::Array(a)) => a
            .into_iter()
            .filter_map(|x| match x {
                Value::String(s) => Some(s),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// `hydrate_lessons` over `load_lessons`: every `lessons` row in id order.
pub async fn hydrate(client: &Client) -> Result<LessonStore> {
    let rows = crate::txn::with_client(client, |client| async move {
        client
            .query(
                "SELECT entity, attribute, value, about, polarity, outcome, confidence::text, \
                 status, origin, support, provenance, asserted_at, last_confirmed, \
                 supersedes_value, superseded_by_value, superseded_at, embedding::real[] \
                 FROM lessons ORDER BY id",
                &[],
            )
            .await
    })
    .await?;
    let mut records = Vec::with_capacity(rows.len());
    for r in rows {
        let entity: String = r.get(0);
        let attribute: String = r.get(1);
        let embedding: Option<Vec<f32>> = r.get(16);
        records.push(LessonRecord {
            key: slot_key(&entity, &attribute),
            entity,
            attribute,
            value: r.get(2),
            about: r.get(3),
            polarity: r.get(4),
            outcome: r.get(5),
            confidence: r.get::<_, String>(6).parse()?,
            status: r.get(7),
            origin: r.get(8),
            support: json_strings(r.get(9)),
            provenance: json_strings(r.get(10)),
            asserted_at: r.get(11),
            last_confirmed: r.get(12),
            supersedes_value: r.get(13),
            superseded_by_value: r.get(14),
            superseded_at: r.get(15),
            unit: embedding.as_deref().map(|e| Arc::from(unit_f32(e))),
        });
    }
    Ok(LessonStore { records })
}
