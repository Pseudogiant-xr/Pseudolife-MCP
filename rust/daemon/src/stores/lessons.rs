//! The lesson store (spec LS1-LS10, FG13-FG16): slot-keyed procedural
//! memory keyed by `(task, aspect)`, hydrated from `lessons`. Python:
//! `memory/lessons.py` (`LessonRecord`, `LessonStore.current_records`,
//! `search`, `retire`); `storage/sync.py` `_lesson_record_to_row`,
//! `sync_lesson_slots` (409-450), `hydrate_lessons` (474-505);
//! `storage/postgres.py` `load_lessons` (1998-2008). The dream's synthesis
//! is not ported; `memory_forget` retires lessons (scope `lesson`, and the
//! source cascade of scope `memory`).

use std::collections::{BTreeSet, HashMap};
use std::sync::{Arc, RwLock};

use anyhow::Result;
use serde_json::{Value, json};
use tokio_postgres::Client;

use crate::stores::cortex::{SlotKey, dot_f32, norm_key, slot_key, unit_f32};

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
    /// The stored vector, as the slot rewrite writes it back.
    pub embedding: Option<Arc<[f32]>>,
    pub tx_time: Option<f64>,
    pub valid_time: Option<f64>,
    pub hlc_phys: Option<i64>,
    pub hlc_logical: Option<i32>,
    pub writer_id: Option<String>,
    pub session_id: Option<String>,
    pub version: i32,
    pub key: SlotKey,
    /// `embedding / (||embedding|| + 1e-12)` in f32, as `search` normalises.
    pub unit: Option<Arc<[f32]>>,
}

/// A set's members in sorted order (`sorted(set(...))`).
fn sorted_set(items: &[String]) -> Vec<String> {
    let mut v = items.to_vec();
    v.sort();
    v.dedup();
    v
}

impl LessonRecord {
    /// `_lesson_record_to_dict` (`service.py:411-430`) plus the sorted
    /// `support` the retire/restore audit adds (`service.py:5546`).
    pub fn audit_snapshot(&self) -> Value {
        json!({
            "task": self.entity,
            "aspect": self.attribute,
            "lesson": self.value,
            "about": self.about,
            "polarity": self.polarity,
            "outcome": self.outcome,
            "status": self.status,
            "confidence": crate::store::round4(self.confidence),
            "origin": self.origin,
            "provenance": sorted_set(&self.provenance),
            "asserted_at": self.asserted_at,
            "last_confirmed": self.last_confirmed,
            "supersedes_value": self.supersedes_value,
            "superseded_by_value": self.superseded_by_value,
            "superseded_at": self.superseded_at,
            "support": sorted_set(&self.support),
        })
    }
}

#[derive(Debug, Clone, Default)]
pub struct LessonStore {
    pub records: Vec<LessonRecord>,
    /// Slots mutated since the last storage sync (`dirty_slots`).
    pub dirty_slots: BTreeSet<SlotKey>,
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

    /// `retire` (`memory/lessons.py:258-282`): every CURRENT record at the
    /// task, or at one exact `(task, aspect)` slot, becomes `retired` at
    /// `now`, kept, its slot dirty. Returns the retired record indexes.
    pub fn retire(&mut self, task: &str, aspect: Option<&str>, now: f64) -> Vec<usize> {
        let ne = norm_key(task);
        let na = aspect.map(norm_key);
        let mut out = Vec::new();
        for (i, r) in self.records.iter_mut().enumerate() {
            if r.status == "current" && r.key.0 == ne && na.as_ref().is_none_or(|a| r.key.1 == *a) {
                r.status = "retired".into();
                r.superseded_at = Some(now);
                self.dirty_slots.insert(r.key.clone());
                out.push(i);
            }
        }
        out
    }

    /// `_refresh_source_retired_lessons_locked` (`service.py:5173-5203`):
    /// mirror the cascade's committed retirements, matched by stable record
    /// identity `(entity_norm, attribute_norm, value, asserted_at)`, onto
    /// still-current records. Dirty slots are kept.
    pub fn mirror_source_retired(
        &mut self,
        retired: &[(String, String, String, f64, Option<f64>)],
    ) {
        let by_identity: HashMap<(&str, &str, &str, u64), Option<f64>> = retired
            .iter()
            .map(|(e, a, v, at, sup)| ((e.as_str(), a.as_str(), v.as_str(), at.to_bits()), *sup))
            .collect();
        for r in &mut self.records {
            let id = (
                r.key.0.as_str(),
                r.key.1.as_str(),
                r.value.as_str(),
                r.asserted_at.to_bits(),
            );
            if r.status == "current"
                && let Some(sup) = by_identity.get(&id)
            {
                r.status = "retired".into();
                r.superseded_at = *sup;
                r.superseded_by_value = Some(SOURCE_FORGOTTEN.into());
            }
        }
    }
}

/// The cascade's retirement cause (`storage/postgres.py:1350`).
pub const SOURCE_FORGOTTEN: &str = "source_forgotten";

/// The resident lesson store as `Ready` holds it: readers take a snapshot,
/// the writer (serialized by `Ready.write`) mutates in place.
#[derive(Default)]
pub struct SharedLessons(RwLock<Arc<LessonStore>>);

impl SharedLessons {
    pub fn new(store: LessonStore) -> Self {
        SharedLessons(RwLock::new(Arc::new(store)))
    }

    pub fn snapshot(&self) -> Arc<LessonStore> {
        self.0.read().expect("lessons lock").clone()
    }

    pub fn write<T>(&self, f: impl FnOnce(&mut LessonStore) -> T) -> T {
        let mut guard = self.0.write().expect("lessons lock");
        f(Arc::make_mut(&mut guard))
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
                 supersedes_value, superseded_by_value, superseded_at, embedding::real[], \
                 tx_time, valid_time, hlc_phys, hlc_logical, writer_id, session_id, version \
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
            embedding: embedding.map(Arc::from),
            tx_time: r.get(17),
            valid_time: r.get(18),
            hlc_phys: r.get(19),
            hlc_logical: r.get(20),
            writer_id: r.get(21),
            session_id: r.get(22),
            version: r.get(23),
        });
    }
    Ok(LessonStore {
        records,
        dirty_slots: BTreeSet::new(),
    })
}

/// `sync_lesson_slots` (`storage/sync.py:438-450`): the dirty slots' records
/// (in record order) rewritten in one transaction (`_replace_slot_rows`:
/// DELETE the sorted slots, then one INSERT per row), each row's subject and
/// `about` object linked to their graph entities (`_link_entity_ids`).
/// Returns the slots written, for the caller to clear.
pub async fn sync_slot_rows(db: &Client, store: &LessonStore) -> Result<BTreeSet<SlotKey>> {
    let slots = store.dirty_slots.clone();
    if slots.is_empty() {
        return Ok(slots);
    }
    let rows: Vec<&LessonRecord> = store
        .records
        .iter()
        .filter(|r| slots.contains(&r.key))
        .collect();
    // `_link_entity_ids` runs before the transaction, a lookup per row.
    let mut links = Vec::with_capacity(rows.len());
    for r in &rows {
        let subject = entity_id(db, &r.entity).await?;
        let object = match r.about.as_deref() {
            Some(about) if !about.is_empty() => entity_id(db, about).await?,
            _ => None,
        };
        links.push((subject, object));
    }
    crate::txn::run(db, |c| {
        let (slots, rows, links) = (&slots, &rows, &links);
        async move {
            let ents: Vec<&str> = slots.iter().map(|s| s.0.as_str()).collect();
            let attrs: Vec<&str> = slots.iter().map(|s| s.1.as_str()).collect();
            c.execute(
                "DELETE FROM lessons WHERE (entity_norm, attribute_norm) IN \
                 (SELECT * FROM unnest($1::text[], $2::text[]))",
                &[&ents, &attrs],
            )
            .await?;
            for (r, (subject, object)) in rows.iter().zip(links.iter()) {
                let embedding: Option<Vec<f32>> = r.embedding.as_deref().map(<[f32]>::to_vec);
                let version = if r.version == 0 { 1 } else { r.version };
                let origin = r.origin.as_deref().filter(|o| !o.is_empty());
                c.execute(
                    "INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, value, \
                     about, polarity, outcome, status, confidence, origin, support, provenance, \
                     asserted_at, last_confirmed, supersedes_value, superseded_by_value, \
                     superseded_at, embedding, entity_id, object_entity_id, tx_time, \
                     valid_time, hlc_phys, hlc_logical, writer_id, session_id, version) \
                     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::float8, $11, $12, $13, \
                     $14, $15, $16, $17, $18, $19::real[]::vector, $20, $21, $22, $23, $24, \
                     $25, $26, $27, $28)",
                    &[
                        &r.entity,
                        &r.attribute,
                        &norm_key(&r.entity),
                        &norm_key(&r.attribute),
                        &r.value,
                        &r.about,
                        &r.polarity,
                        &r.outcome,
                        &r.status,
                        &r.confidence,
                        &origin,
                        &json!(sorted_set(&r.support)),
                        &json!(sorted_set(&r.provenance)),
                        &r.asserted_at,
                        &r.last_confirmed,
                        &r.supersedes_value,
                        &r.superseded_by_value,
                        &r.superseded_at,
                        &embedding,
                        subject,
                        object,
                        &r.tx_time,
                        &r.valid_time,
                        &r.hlc_phys,
                        &r.hlc_logical,
                        &r.writer_id,
                        &r.session_id,
                        &version,
                    ],
                )
                .await?;
            }
            Ok::<_, tokio_postgres::Error>(())
        }
    })
    .await?;
    Ok(slots)
}

/// `storage.find_entity(norm_name(name))["id"]`, or none.
async fn entity_id(db: &Client, name: &str) -> Result<Option<i64>> {
    let n = crate::storage::graph::norm_name(name);
    let found = crate::storage::graph::find_entity(db, &n).await?;
    Ok(found.get("id").and_then(Value::as_i64))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(task: &str, aspect: &str, status: &str) -> LessonRecord {
        LessonRecord {
            entity: task.into(),
            attribute: aspect.into(),
            value: "do the thing".into(),
            about: None,
            polarity: "+".into(),
            outcome: "success".into(),
            confidence: 0.7,
            status: status.into(),
            origin: None,
            support: vec!["b".into(), "a".into(), "a".into()],
            provenance: vec!["ep:2".into(), "ep:1".into()],
            asserted_at: 10.0,
            last_confirmed: 11.0,
            supersedes_value: None,
            superseded_by_value: None,
            superseded_at: None,
            embedding: None,
            tx_time: None,
            valid_time: None,
            hlc_phys: None,
            hlc_logical: None,
            writer_id: None,
            session_id: None,
            version: 1,
            key: slot_key(task, aspect),
            unit: None,
        }
    }

    #[test]
    fn retire_takes_current_records_at_the_task_or_slot() {
        let mut s = LessonStore {
            records: vec![
                rec("Deploy Engine", "approach", "current"),
                rec("deploy-engine", "pitfall", "current"),
                rec("Deploy Engine", "approach", "superseded"),
                rec("Other", "approach", "current"),
            ],
            ..Default::default()
        };
        assert_eq!(s.retire("deploy engine", Some("Approach"), 5.0), vec![0]);
        assert_eq!(s.retire("DEPLOY ENGINE", None, 6.0), vec![1]);
        assert_eq!(s.records[1].superseded_at, Some(6.0));
        assert_eq!(s.records[2].status, "superseded");
        assert_eq!(s.dirty_slots.len(), 2);
    }

    #[test]
    fn audit_snapshot_sorts_sets() {
        let v = rec("t", "a", "retired").audit_snapshot();
        assert_eq!(v["support"], json!(["a", "b"]));
        assert_eq!(v["provenance"], json!(["ep:1", "ep:2"]));
        assert_eq!(v["confidence"], json!(0.7));
    }
}
