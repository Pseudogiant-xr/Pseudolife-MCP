//! The world cortex (spec WD1-WD12): slot-keyed canonical store for
//! external facts, kept apart from the personal cortex in `world_facts`.
//! Python: `memory/world_cortex.py` (`WorldRecord`, `WorldCortexStore`);
//! `storage/sync.py` `_world_record_to_row` (320-348), `sync_world_slots`
//! (351-359), `hydrate_world_cortex` (372-404); `storage/postgres.py`
//! `load_world_facts` (1977-1987) and `_replace_slot_rows` (1846-1866).
//!
//! Every world fact is `origin='source'`: no tier guard, no contenders; a
//! newer source supersedes. Freshness is read-time (`retrieved_at` decay).

use std::collections::{BTreeSet, HashMap};
use std::sync::{Arc, RwLock};

use anyhow::Result;
use serde_json::{Value, json};
use tokio_postgres::Client;

use crate::read::cortex_search::{decay_factor, normalize_class, ttl};
use crate::stores::cortex::{SlotKey, dot_f32, norm_key, slot_key, unit_f32};
use crate::stores::cortex_write::norm_value;

/// `world_cortex.ORIGIN`.
pub const ORIGIN: &str = "source";

#[derive(Debug, Clone, PartialEq)]
pub struct WorldRecord {
    pub entity: String,
    pub attribute: String,
    pub value: String,
    pub polarity: String,
    pub confidence: f64,
    pub status: String,
    pub source_url: String,
    pub source_quote: String,
    pub freshness_class: String,
    pub retrieved_at: f64,
    pub content_hash: Option<String>,
    pub source_doc_id: Option<i64>,
    pub asserted_at: f64,
    pub last_confirmed: f64,
    pub supersedes_value: Option<String>,
    pub superseded_by_value: Option<String>,
    pub superseded_at: Option<f64>,
    pub embedding: Option<Arc<[f32]>>,
    pub tx_time: Option<f64>,
    pub valid_time: Option<f64>,
    pub hlc_phys: Option<i64>,
    pub hlc_logical: Option<i32>,
    pub writer_id: Option<String>,
    pub session_id: Option<String>,
    pub version: i32,
    /// `(norm_key(entity), norm_key(attribute))`.
    pub key: SlotKey,
    /// `embedding / (||embedding|| + 1e-12)` in f32, as `search` normalises.
    pub unit: Option<Arc<[f32]>>,
}

impl WorldRecord {
    /// `_world_record_snapshot` (`service.py:506-519`): the verbatim fields
    /// for the retire/restore audit, no read-time decay applied.
    pub fn audit_snapshot(&self) -> Value {
        json!({
            "entity": self.entity, "attribute": self.attribute, "value": self.value,
            "polarity": self.polarity, "status": self.status,
            "confidence": crate::store::round4(self.confidence), "origin": ORIGIN,
            "source_url": self.source_url, "source_quote": self.source_quote,
            "freshness_class": self.freshness_class,
            "retrieved_at": self.retrieved_at, "content_hash": self.content_hash,
            "source_doc_id": self.source_doc_id, "asserted_at": self.asserted_at,
            "last_confirmed": self.last_confirmed,
        })
    }

    fn refresh_derived(&mut self) {
        self.key = slot_key(&self.entity, &self.attribute);
        self.unit = self.embedding.as_deref().map(|e| Arc::from(unit_f32(e)));
    }

    /// `effective_confidence(now)`: stored confidence times the class decay
    /// over the age since `retrieved_at`, clamped to [0, 1].
    pub fn effective_confidence(&self, now: f64) -> f64 {
        (self.confidence * decay_factor(&self.freshness_class, now - self.retrieved_at))
            .clamp(0.0, 1.0)
    }

    /// `is_stale(now)`: past twice the class TTL since `retrieved_at`.
    pub fn is_stale(&self, now: f64) -> bool {
        ttl(&self.freshness_class).is_some_and(|t| now - self.retrieved_at > 2.0 * t)
    }
}

/// The citation and timing of one `write_fact`.
pub struct WorldWrite<'a> {
    pub confidence: f64,
    pub source_url: &'a str,
    pub source_quote: &'a str,
    pub freshness_class: &'a str,
    pub retrieved_at: Option<f64>,
    pub content_hash: Option<&'a str>,
    pub now: f64,
    pub hlc: (i64, i64),
    pub writer_id: Option<&'a str>,
    pub session_id: Option<&'a str>,
}

#[derive(Debug, Clone, Default)]
pub struct WorldStore {
    pub records: Vec<WorldRecord>,
    current: HashMap<SlotKey, usize>,
    pub dirty_slots: BTreeSet<SlotKey>,
}

impl WorldStore {
    pub fn from_records(records: Vec<WorldRecord>) -> WorldStore {
        let mut s = WorldStore {
            records,
            ..Default::default()
        };
        s.reindex();
        s
    }

    /// `_current = {r.key: i for current records}`: the last wins.
    fn reindex(&mut self) {
        self.current = self
            .records
            .iter()
            .enumerate()
            .filter(|(_, r)| r.status == "current")
            .map(|(i, r)| (r.key.clone(), i))
            .collect();
    }

    /// `retire` (`world_cortex.py:259-282`): every CURRENT record at the
    /// entity, or at one exact slot, becomes `retired` at `now`, kept, its
    /// slot dirty; the current index is rebuilt. Returns the retired record
    /// indexes.
    pub fn retire(&mut self, entity: &str, attribute: Option<&str>, now: f64) -> Vec<usize> {
        let ne = norm_key(entity);
        let na = attribute.map(norm_key);
        let mut out = Vec::new();
        for (i, r) in self.records.iter_mut().enumerate() {
            if r.status == "current" && r.key.0 == ne && na.as_ref().is_none_or(|a| r.key.1 == *a) {
                r.status = "retired".into();
                r.superseded_at = Some(now);
                self.dirty_slots.insert(r.key.clone());
                out.push(i);
            }
        }
        if !out.is_empty() {
            self.reindex();
        }
        out
    }

    /// `lookup`: the slot's current record.
    #[allow(dead_code)] // read API for `world_lookup` (the console) and the tests
    pub fn lookup(&self, entity: &str, attribute: &str) -> Option<&WorldRecord> {
        let r = &self.records[*self.current.get(&slot_key(entity, attribute))?];
        (r.status == "current").then_some(r)
    }

    /// `write_fact`: insert at an empty slot; confirm a same value (refresh
    /// the citation and timing, lift confidence); supersede a different one
    /// unless the candidate's HLC is older than the current record's (then
    /// `stale`, nothing changed but the slot marked dirty). Returns the
    /// action and the answered record's index.
    pub fn write_fact(
        &mut self,
        entity: &str,
        attribute: &str,
        value: &str,
        embedding: Option<Arc<[f32]>>,
        w: &WorldWrite<'_>,
    ) -> (&'static str, usize) {
        let t = w.now;
        let ra = w.retrieved_at.unwrap_or(t);
        let fc = normalize_class(w.freshness_class).to_string();
        let key = slot_key(entity, attribute);
        self.dirty_slots.insert(key.clone());
        let Some(&idx) = self.current.get(&key) else {
            return (
                "inserted",
                self.insert(entity, attribute, value, embedding, w, &fc, ra, None),
            );
        };
        if norm_value(&self.records[idx].value) == norm_value(value)
            && !crate::mutants::active("mcp-world-set-no-confirm")
        {
            let cur = &mut self.records[idx];
            cur.last_confirmed = t;
            cur.retrieved_at = ra;
            cur.freshness_class = fc;
            cur.confidence = cur.confidence.max(w.confidence).min(1.0);
            if !w.source_url.is_empty() {
                cur.source_url = w.source_url.to_string();
            }
            if !w.source_quote.is_empty() {
                cur.source_quote = w.source_quote.to_string();
            }
            if let Some(h) = w.content_hash.filter(|h| !h.is_empty()) {
                cur.content_hash = Some(h.to_string());
            }
            cur.tx_time = Some(t);
            cur.hlc_phys = Some(w.hlc.0);
            cur.hlc_logical = Some(w.hlc.1 as i32);
            if let Some(wid) = w.writer_id.filter(|s| !s.is_empty()) {
                cur.writer_id = Some(wid.to_string());
            }
            if let Some(sid) = w.session_id.filter(|s| !s.is_empty()) {
                cur.session_id = Some(sid.to_string());
            }
            return ("confirmed", idx);
        }
        let cur = &self.records[idx];
        let cur_hlc = (
            cur.hlc_phys.unwrap_or(0),
            i64::from(cur.hlc_logical.unwrap_or(0)),
        );
        if w.hlc < cur_hlc || (w.hlc == cur_hlc && t < cur.asserted_at) {
            return ("stale", idx);
        }
        let old_value = cur.value.clone();
        let cur = &mut self.records[idx];
        cur.status = "superseded".into();
        cur.superseded_at = Some(t);
        cur.superseded_by_value = Some(value.to_string());
        let new = self.insert(
            entity,
            attribute,
            value,
            embedding,
            w,
            &fc,
            ra,
            Some(old_value),
        );
        ("superseded", new)
    }

    #[allow(clippy::too_many_arguments)]
    fn insert(
        &mut self,
        entity: &str,
        attribute: &str,
        value: &str,
        embedding: Option<Arc<[f32]>>,
        w: &WorldWrite<'_>,
        fc: &str,
        ra: f64,
        supersedes: Option<String>,
    ) -> usize {
        let mut rec = WorldRecord {
            entity: entity.to_string(),
            attribute: attribute.to_string(),
            value: value.to_string(),
            polarity: "+".into(),
            confidence: w.confidence,
            status: "current".into(),
            source_url: w.source_url.to_string(),
            source_quote: w.source_quote.to_string(),
            freshness_class: fc.to_string(),
            retrieved_at: ra,
            content_hash: w.content_hash.map(str::to_string),
            source_doc_id: None,
            asserted_at: w.now,
            last_confirmed: w.now,
            supersedes_value: supersedes,
            superseded_by_value: None,
            superseded_at: None,
            embedding,
            tx_time: Some(w.now),
            valid_time: Some(w.now),
            hlc_phys: Some(w.hlc.0),
            hlc_logical: Some(w.hlc.1 as i32),
            writer_id: w.writer_id.map(str::to_string),
            session_id: w.session_id.map(str::to_string),
            version: 1,
            key: (String::new(), String::new()),
            unit: None,
        };
        rec.refresh_derived();
        self.records.push(rec);
        let i = self.records.len() - 1;
        self.current.insert(self.records[i].key.clone(), i);
        i
    }

    /// `search`: current records with an embedding, cosine against the
    /// normalised query in f32, at or above `min_score`, stably sorted by
    /// descending score, cut to `top_k`. Returns `(record index, score)`.
    /// A stored vector whose length differs from the query's fails, as
    /// torch's stack and matmul raise (Python checks no world dimension at
    /// load).
    pub fn search(&self, q: &[f32], top_k: usize, min_score: f64) -> Result<Vec<(usize, f64)>> {
        if let Some(r) = self
            .records
            .iter()
            .filter(|r| r.status == "current")
            .find(|r| r.unit.as_deref().is_some_and(|u| u.len() != q.len()))
        {
            anyhow::bail!(
                "world fact {}/{} has a {}-dim vector, the query {}",
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

/// The resident world store as `Ready` holds it: readers take a snapshot,
/// the writer (serialized by `Ready.write`) mutates in place.
#[derive(Default)]
pub struct SharedWorld(RwLock<Arc<WorldStore>>);

impl SharedWorld {
    pub fn new(store: WorldStore) -> Self {
        SharedWorld(RwLock::new(Arc::new(store)))
    }

    pub fn snapshot(&self) -> Arc<WorldStore> {
        self.0.read().expect("world lock").clone()
    }

    pub fn write<T>(&self, f: impl FnOnce(&mut WorldStore) -> T) -> T {
        let mut guard = self.0.write().expect("world lock");
        f(Arc::make_mut(&mut guard))
    }
}

/// `hydrate_world_cortex` over `load_world_facts`: every `world_facts` row in
/// id order (`retrieved_at or 0.0`, `source_url or ""`, `freshness_class or
/// "volatile"`), then the current index.
pub async fn hydrate(client: &Client) -> Result<WorldStore> {
    let rows = crate::txn::with_client(client, |client| async move {
        client
            .query(
                "SELECT entity, attribute, value, polarity, confidence::text, status, \
                 source_url, source_quote, freshness_class, retrieved_at, content_hash, \
                 source_doc_id, asserted_at, last_confirmed, supersedes_value, \
                 superseded_by_value, superseded_at, embedding::real[], tx_time, valid_time, \
                 hlc_phys, hlc_logical, writer_id, session_id, version \
                 FROM world_facts ORDER BY id",
                &[],
            )
            .await
    })
    .await?;
    let mut records = Vec::with_capacity(rows.len());
    for r in rows {
        let embedding: Option<Vec<f32>> = r.get(17);
        let version: Option<i32> = r.get(24);
        let mut rec = WorldRecord {
            entity: r.get(0),
            attribute: r.get(1),
            value: r.get(2),
            polarity: r.get(3),
            confidence: r.get::<_, String>(4).parse()?,
            status: r.get(5),
            source_url: r.get::<_, Option<String>>(6).unwrap_or_default(),
            source_quote: r.get::<_, Option<String>>(7).unwrap_or_default(),
            freshness_class: r
                .get::<_, Option<String>>(8)
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "volatile".into()),
            retrieved_at: r.get::<_, Option<f64>>(9).unwrap_or(0.0),
            content_hash: r.get(10),
            source_doc_id: r.get(11),
            asserted_at: r.get(12),
            last_confirmed: r.get(13),
            supersedes_value: r.get(14),
            superseded_by_value: r.get(15),
            superseded_at: r.get(16),
            embedding: embedding.map(Arc::from),
            tx_time: r.get(18),
            valid_time: r.get(19),
            hlc_phys: r.get(20),
            hlc_logical: r.get(21),
            writer_id: r.get(22),
            session_id: r.get(23),
            version: version.filter(|&v| v != 0).unwrap_or(1),
            key: (String::new(), String::new()),
            unit: None,
        };
        rec.refresh_derived();
        records.push(rec);
    }
    Ok(WorldStore::from_records(records))
}

/// `sync_world_slots`: the dirty slots' rows (in record order) rewritten in
/// one transaction (`_replace_slot_rows`: DELETE the sorted slots, then one
/// INSERT per row). Returns the slots written, for the caller to clear.
pub async fn sync_slot_rows(db: &Client, store: &WorldStore) -> Result<BTreeSet<SlotKey>> {
    let slots = store.dirty_slots.clone();
    if slots.is_empty() {
        return Ok(slots);
    }
    let rows: Vec<&WorldRecord> = store
        .records
        .iter()
        .filter(|r| slots.contains(&r.key))
        .collect();
    crate::txn::run(db, |c| {
        let (slots, rows) = (&slots, &rows);
        async move {
            let ents: Vec<&str> = slots.iter().map(|s| s.0.as_str()).collect();
            let attrs: Vec<&str> = slots.iter().map(|s| s.1.as_str()).collect();
            c.execute(
                "DELETE FROM world_facts WHERE (entity_norm, attribute_norm) IN \
                 (SELECT * FROM unnest($1::text[], $2::text[]))",
                &[&ents, &attrs],
            )
            .await?;
            for r in rows.iter() {
                let embedding: Option<Vec<f32>> = r.embedding.as_deref().map(<[f32]>::to_vec);
                let freshness = if r.freshness_class.is_empty() {
                    "volatile"
                } else {
                    r.freshness_class.as_str()
                };
                let version = if r.version == 0 { 1 } else { r.version };
                c.execute(
                    "INSERT INTO world_facts (entity, attribute, entity_norm, attribute_norm, \
                     value, polarity, status, confidence, origin, support, provenance, \
                     asserted_at, last_confirmed, supersedes_value, superseded_by_value, \
                     superseded_at, embedding, source_url, source_quote, retrieved_at, \
                     freshness_class, content_hash, source_doc_id, tx_time, valid_time, \
                     hlc_phys, hlc_logical, writer_id, session_id, version) VALUES ($1, $2, \
                     $3, $4, $5, $6, $7, $8::float8, $9, $10, $11, $12, $13, $14, $15, $16, \
                     $17::real[]::vector, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, \
                     $28, $29, $30)",
                    &[
                        &r.entity,
                        &r.attribute,
                        &norm_key(&r.entity),
                        &norm_key(&r.attribute),
                        &r.value,
                        &r.polarity,
                        &r.status,
                        &r.confidence,
                        &ORIGIN,
                        &json!([ORIGIN]),
                        &json!([]),
                        &r.asserted_at,
                        &r.last_confirmed,
                        &r.supersedes_value,
                        &r.superseded_by_value,
                        &r.superseded_at,
                        &embedding,
                        &r.source_url,
                        &r.source_quote,
                        &r.retrieved_at,
                        &freshness,
                        &r.content_hash,
                        &r.source_doc_id,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn write<'a>(now: f64, hlc: (i64, i64), url: &'a str) -> WorldWrite<'a> {
        WorldWrite {
            confidence: 0.85,
            source_url: url,
            source_quote: "",
            freshness_class: "volatile",
            retrieved_at: None,
            content_hash: None,
            now,
            hlc,
            writer_id: Some("w"),
            session_id: None,
        }
    }

    #[test]
    fn insert_confirm_supersede_and_stale_follow_python() {
        let mut s = WorldStore::default();
        let (a, i) = s.write_fact(
            "Rust",
            "latest version",
            "1.90",
            None,
            &write(10.0, (10, 0), "https://a"),
        );
        assert_eq!((a, i), ("inserted", 0));
        // Same value (normalised): confirm, citation kept when empty.
        let (a, i) = s.write_fact(
            "rust",
            "Latest-Version",
            " 1.90 ",
            None,
            &write(20.0, (20, 0), ""),
        );
        assert_eq!((a, i), ("confirmed", 0));
        assert_eq!(s.records[0].source_url, "https://a");
        assert_eq!(s.records[0].last_confirmed, 20.0);
        assert_eq!(s.records[0].tx_time, Some(20.0));
        assert_eq!(s.records[0].asserted_at, 10.0);
        // A different value supersedes.
        let (a, i) = s.write_fact(
            "Rust",
            "latest version",
            "1.91",
            None,
            &write(30.0, (30, 0), ""),
        );
        assert_eq!((a, i), ("superseded", 1));
        assert_eq!(s.records[0].status, "superseded");
        assert_eq!(s.records[0].superseded_by_value.as_deref(), Some("1.91"));
        assert_eq!(s.records[1].supersedes_value.as_deref(), Some("1.90"));
        assert_eq!(
            s.lookup("RUST", "latest version").map(|r| r.value.as_str()),
            Some("1.91")
        );
        // An older clock never clobbers the current record.
        let (a, i) = s.write_fact(
            "Rust",
            "latest version",
            "1.89",
            None,
            &write(40.0, (5, 0), ""),
        );
        assert_eq!((a, i), ("stale", 1));
        assert_eq!(s.records.len(), 2);
        assert!(s.dirty_slots.contains(&slot_key("rust", "latest version")));
    }

    #[test]
    fn freshness_decays_from_retrieved_at() {
        let mut s = WorldStore::default();
        let mut w = write(0.0, (1, 0), "");
        w.retrieved_at = Some(0.0);
        s.write_fact("x", "y", "z", None, &w);
        let r = &s.records[0];
        let day = 86400.0;
        assert_eq!(r.effective_confidence(0.0), 0.85);
        assert!(!r.is_stale(42.0 * day));
        assert!(r.is_stale(42.0 * day + 1.0));
        assert!((r.effective_confidence(21.0 * day) - 0.85 * 0.4).abs() < 1e-12);
    }
}
