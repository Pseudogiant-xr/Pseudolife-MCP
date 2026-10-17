//! The resident cortex: every `facts` row, hydrated in id order, with the
//! slot indexes `CortexStore._reindex_current` keeps (`memory/cortex.py`),
//! and the read API the cortex-first search block uses.
//!
//! Hydration is `storage/sync.py:hydrate_cortex` over
//! `PostgresStorage.load_facts` (`storage/postgres.py:1918`, `ORDER BY id`).
//! Contenders are not a separate table: they are `facts` rows with
//! `status = 'contested'`, indexed here by slot key.
//!
//! The indexes are `pub(crate)` so the write slice can extend them in place:
//! an append pushes onto `records` and inserts the new index into `current`,
//! `members` or `contested` under the record's `key`. The write primitives
//! live in `cortex_write.rs`, their persistence in `cortex_sync.rs`.

use std::collections::{BTreeSet, HashMap};
use std::sync::{Arc, RwLock};

use anyhow::{Result, bail};
use serde_json::Value;
use tokio_postgres::Client;

#[cfg(test)]
pub const DIM: usize = 1024;

/// `ASSISTANT_FACT_SCORE_MULT` (`memory/cortex.py:229`).
pub const ASSISTANT_FACT_SCORE_MULT: f64 = 0.85;

/// `SUPPORT_PRECEDENCE` (`memory/cortex.py`): strongest tier first.
pub const SUPPORT_PRECEDENCE: [&str; 4] = ["user", "action", "agent", "assistant"];

/// A normalised `(entity, attribute)` slot identity (`CortexRecord.key`).
pub type SlotKey = (String, String);

/// Python `str.isspace`: Unicode White_Space plus the four information
/// separators U+001C..U+001F, which Python counts and Rust does not.
pub fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python `str.casefold` for the slot key. Full lowercase mapping plus the
/// two folds that differ from it in practice (`ß` to `ss`, final sigma to
/// sigma); the rarer special foldings are a declared divergence.
pub fn py_casefold(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.to_lowercase().chars() {
        match c {
            'ß' | 'ẞ' => out.push_str("ss"),
            'ς' => out.push('σ'),
            c => out.push(c),
        }
    }
    out
}

/// `_norm_key` (`memory/cortex.py:54`): strip, casefold, fold every run of
/// separators (`[\s._\-/]+`) to one hyphen, strip hyphens.
pub fn norm_key(s: &str) -> String {
    let folded = py_casefold(s.trim_matches(py_isspace));
    let mut out = String::with_capacity(folded.len());
    let mut in_sep = false;
    for c in folded.chars() {
        if py_isspace(c) || matches!(c, '.' | '_' | '-' | '/') {
            if !in_sep {
                out.push('-');
                in_sep = true;
            }
        } else {
            out.push(c);
            in_sep = false;
        }
    }
    out.trim_matches('-').to_string()
}

pub fn slot_key(entity: &str, attribute: &str) -> SlotKey {
    (norm_key(entity), norm_key(attribute))
}

/// One `facts` row: every column, plus the derived slot key and the
/// search-normalised embedding.
#[derive(Clone, Debug)]
// Every `facts` column, for the write slice (W2-E) as much as for reads.
#[allow(dead_code)]
pub struct CortexRecord {
    pub id: i64,
    pub entity: String,
    pub attribute: String,
    /// The stored `entity_norm` / `attribute_norm` columns. The slot key is
    /// recomputed from `entity` / `attribute` (`CortexRecord.key`), as Python
    /// does; these are kept for the write slice.
    pub entity_norm: String,
    pub attribute_norm: String,
    pub value: String,
    pub polarity: String,
    pub status: String,
    /// PostgreSQL REAL decoded from its decimal text, as psycopg reads it.
    pub confidence: f64,
    /// The stored `origin` column. Python never reads it back: the served
    /// `origin` is derived from `support` (see [`CortexRecord::origin`]).
    pub origin_column: Option<String>,
    /// `support` as stored (a JSON list); Python holds it as a set.
    pub support: Vec<String>,
    /// `provenance` string members as stored; Python holds them as a set.
    pub provenance: Vec<String>,
    pub asserted_at: f64,
    pub last_confirmed: f64,
    pub supersedes_value: Option<String>,
    pub superseded_by_value: Option<String>,
    pub superseded_at: Option<f64>,
    /// Shared so that cloning the store (a writer that meets a reader's
    /// snapshot) does not copy the vectors.
    pub embedding: Option<Arc<[f32]>>,
    /// The value-free slot embedding (`f"{entity} {attribute}"`). Resident
    /// only: Python keeps it in RAM for the dream's slot resolver and never
    /// persists it, so hydrated records carry None (`cortex.py:300`).
    pub slot_embedding: Option<Arc<[f32]>>,
    pub entity_id: Option<i64>,
    pub object_entity_id: Option<i64>,
    /// `row.freshness_class or "evergreen"` (`sync.py:hydrate_cortex`).
    pub freshness_class: String,
    /// `row.kind or "scalar"`.
    pub kind: String,
    pub value_norm: Option<String>,
    pub stance: Option<String>,
    pub authority: Option<String>,
    pub distortion_tolerance: Option<String>,
    pub tx_time: Option<f64>,
    pub valid_time: Option<f64>,
    pub hlc_phys: Option<i64>,
    pub hlc_logical: Option<i32>,
    pub writer_id: Option<String>,
    pub session_id: Option<String>,
    /// `row.version or 1`.
    pub version: i32,
    /// `(norm_key(entity), norm_key(attribute))`; a mutator that changes
    /// `entity` or `attribute` must recompute it.
    pub(crate) key: SlotKey,
    /// `embedding / (||embedding|| + 1e-12)` in f32, the per-row
    /// normalisation `CortexStore.search` applies (`cortex.py:1524`).
    pub(crate) unit: Option<Arc<[f32]>>,
}

impl CortexRecord {
    pub fn key(&self) -> &SlotKey {
        &self.key
    }

    /// `CortexRecord.origin`: the strongest tier in `support`, or "".
    pub fn origin(&self) -> &'static str {
        SUPPORT_PRECEDENCE
            .into_iter()
            .find(|t| self.support.iter().any(|s| s == t))
            .unwrap_or("")
    }

    /// Recompute the derived fields after a mutation of `entity`,
    /// `attribute` or `embedding`.
    pub(crate) fn refresh_derived(&mut self) {
        self.key = slot_key(&self.entity, &self.attribute);
        self.unit = self.embedding.as_deref().map(|e| Arc::from(unit_f32(e)));
    }
}

/// `v / (||v|| + 1e-12)` in f32 (torch's `x / (x.norm() + 1e-12)`).
pub fn unit_f32(v: &[f32]) -> Vec<f32> {
    let n = v
        .iter()
        .map(|&x| (x as f64) * (x as f64))
        .sum::<f64>()
        .sqrt() as f32;
    let d = n + 1e-12_f32;
    v.iter().map(|&x| x / d).collect()
}

/// Dot product of two f32 vectors, accumulated in f64 and returned as the
/// f32 a float32 matmul yields.
pub fn dot_f32(a: &[f32], b: &[f32]) -> f32 {
    a.iter()
        .zip(b)
        .map(|(&x, &y)| (x as f64) * (y as f64))
        .sum::<f64>() as f32
}

#[derive(Clone)]
pub struct CortexStore {
    /// Every row, in `facts.id` order at hydration, appended after.
    pub(crate) records: Vec<CortexRecord>,
    /// Slot -> the one current scalar record.
    pub(crate) current: HashMap<SlotKey, usize>,
    /// Slot -> current member records, in record order.
    pub(crate) members: HashMap<SlotKey, Vec<usize>>,
    /// Slot -> contested records, in record order (at most one after the
    /// load-time healing).
    pub(crate) contested: HashMap<SlotKey, Vec<usize>>,
    /// `memory.cortex.protect_provenance` (`CortexStore.__init__`).
    pub(crate) protect_provenance: bool,
    /// `memory.cortex.supersede_confidence_margin`.
    pub(crate) supersede_confidence_margin: f64,
    /// `memory.cortex.reinforce_rate`.
    pub(crate) reinforce_rate: f64,
    /// Slots mutated since the last storage sync (`dirty_slots`), sorted as
    /// `_replace_slot_rows` sorts them.
    pub(crate) dirty_slots: BTreeSet<SlotKey>,
    /// The supersession log or dream cursor changed since the last sync.
    pub(crate) meta_dirty: bool,
    /// `supersession_log`: the resident audit rows, seeded from
    /// `meta.cortex_supersession_log` at startup.
    pub(crate) supersession_log: Vec<Value>,
    /// `dream_cursor`, seeded from `meta.cortex_dream_cursor`.
    pub(crate) dream_cursor: f64,
}

impl Default for CortexStore {
    fn default() -> Self {
        CortexStore {
            records: Vec::new(),
            current: HashMap::new(),
            members: HashMap::new(),
            contested: HashMap::new(),
            protect_provenance: true,
            supersede_confidence_margin: 0.15,
            reinforce_rate: 0.34,
            dirty_slots: BTreeSet::new(),
            meta_dirty: false,
            supersession_log: Vec::new(),
            dream_cursor: 0.0,
        }
    }
}

/// The resident cortex as `Ready` holds it. Readers take a snapshot (an
/// `Arc` clone under a brief read lock) and never hold the lock across an
/// await; the writer, serialized by `Ready.write`, mutates in place through
/// [`SharedCortex::write`], which copies the store only when a reader still
/// holds an older snapshot.
#[derive(Default)]
pub struct SharedCortex(RwLock<Arc<CortexStore>>);

impl SharedCortex {
    pub fn new(store: CortexStore) -> Self {
        SharedCortex(RwLock::new(Arc::new(store)))
    }

    /// The current store, consistent for as long as the caller keeps it.
    pub fn snapshot(&self) -> Arc<CortexStore> {
        self.0.read().expect("cortex lock").clone()
    }

    /// Run `f` on the store under the write lock. `f` must not block.
    pub fn write<T>(&self, f: impl FnOnce(&mut CortexStore) -> T) -> T {
        let mut guard = self.0.write().expect("cortex lock");
        f(Arc::make_mut(&mut guard))
    }
}

impl CortexStore {
    #[allow(dead_code)] // the write slice's accessor
    pub fn records(&self) -> &[CortexRecord] {
        &self.records
    }

    /// `_reindex_current` (`memory/cortex.py:1797`): rebuild the indexes and
    /// self-heal duplicate live scalars (keep the most recently confirmed,
    /// demote the rest to `superseded`). Returns the demoted record indexes;
    /// Python marks their slots dirty and persists them on its next save,
    /// which belongs to the write slice.
    pub(crate) fn reindex(&mut self) -> Vec<usize> {
        self.current.clear();
        self.members.clear();
        self.contested.clear();
        let mut seen_contested: HashMap<SlotKey, usize> = HashMap::new();
        let mut demoted = Vec::new();
        for i in 0..self.records.len() {
            let (status, is_member, key) = {
                let r = &self.records[i];
                (r.status.clone(), r.kind == "member", r.key.clone())
            };
            if status == "current" && is_member {
                self.members.entry(key).or_default().push(i);
            } else if status == "current" || status == "contested" {
                let map = if status == "current" {
                    &mut self.current
                } else {
                    &mut seen_contested
                };
                let keep = match map.get(&key).copied() {
                    None => i,
                    Some(prev) => {
                        let (keep, drop) = if self.records[i].last_confirmed
                            >= self.records[prev].last_confirmed
                        {
                            (i, prev)
                        } else {
                            (prev, i)
                        };
                        let (keep_lc, keep_val) = (
                            self.records[keep].last_confirmed,
                            self.records[keep].value.clone(),
                        );
                        let loser = &mut self.records[drop];
                        loser.status = "superseded".into();
                        if loser.superseded_at.is_none() {
                            loser.superseded_at = Some(keep_lc);
                        }
                        loser.superseded_by_value = Some(keep_val);
                        demoted.push(drop);
                        keep
                    }
                };
                map.insert(key, keep);
            }
        }
        for (key, i) in seen_contested {
            self.contested.insert(key, vec![i]);
        }
        demoted
    }

    /// `current_records()`: every `current` record, in record order.
    pub fn current_records(&self) -> impl Iterator<Item = &CortexRecord> {
        self.records.iter().filter(|r| r.status == "current")
    }

    /// `members(entity, attribute)`: current members of a set slot, in
    /// insertion order.
    #[allow(dead_code)] // read API for the write slice (W2-E) and the tests
    pub fn members(&self, entity: &str, attribute: &str) -> Vec<&CortexRecord> {
        self.members_by_key(&slot_key(entity, attribute))
    }

    #[allow(dead_code)] // read API for the write slice (W2-E) and the tests
    pub fn members_by_key(&self, key: &SlotKey) -> Vec<&CortexRecord> {
        self.members
            .get(key)
            .map(|v| v.iter().map(|&i| &self.records[i]).collect())
            .unwrap_or_default()
    }

    /// `lookup(entity, attribute)` (`memory/cortex.py:1519`): the slot's one
    /// current scalar record.
    pub fn lookup(&self, entity: &str, attribute: &str) -> Option<&CortexRecord> {
        let i = *self.current.get(&slot_key(entity, attribute))?;
        let r = &self.records[i];
        (r.status == "current").then_some(r)
    }

    /// `slot_kind` (`memory/cortex.py:1193`): `"scalar"` with a current
    /// scalar, `"set"` with current members or any historical member row,
    /// else None.
    pub fn slot_kind(&self, entity: &str, attribute: &str) -> Option<&'static str> {
        let key = slot_key(entity, attribute);
        if self.current.contains_key(&key) {
            return Some("scalar");
        }
        let has_members = self.members.get(&key).is_some_and(|v| !v.is_empty());
        if has_members
            || self
                .records
                .iter()
                .any(|r| r.key == key && r.kind == "member")
        {
            return Some("set");
        }
        None
    }

    /// `members(entity, attribute, include_removed=True)`: every member row
    /// at the slot, in record order.
    pub fn member_rows(&self, entity: &str, attribute: &str) -> Vec<&CortexRecord> {
        let key = slot_key(entity, attribute);
        self.records
            .iter()
            .filter(|r| r.key == key && r.kind == "member")
            .collect()
    }

    /// `candidates_for` (`memory/cortex.py:1701-1737`): nearby slots for an
    /// empty lookup. Same-entity current records at other attributes, most
    /// recently asserted or confirmed first (`score` null, `why`
    /// "same_entity"), then embedding-similar slots above `min_score`
    /// (`score` the cosine to four places, `why` "similar_slot"), at most
    /// `top_k`; never the queried slot.
    pub fn candidates_for(
        &self,
        entity: &str,
        attribute: &str,
        query: Option<&[f32]>,
        top_k: usize,
        min_score: f64,
    ) -> Vec<serde_json::Value> {
        let (key_ent, key_attr) = slot_key(entity, attribute);
        let mut same: Vec<&CortexRecord> = self
            .current_records()
            .filter(|r| r.key.0 == key_ent && r.key.1 != key_attr)
            .collect();
        // Stable, by -max(asserted_at, last_confirmed).
        same.sort_by(|a, b| {
            let (x, y) = (
                a.asserted_at.max(a.last_confirmed),
                b.asserted_at.max(b.last_confirmed),
            );
            y.total_cmp(&x)
        });
        let mut out: Vec<serde_json::Value> = same
            .iter()
            .take(top_k)
            .map(|r| {
                serde_json::json!({"entity": r.entity, "attribute": r.attribute, "value": r.value,
                                   "score": null, "why": "same_entity"})
            })
            .collect();
        if let Some(q) = query
            && out.len() < top_k
        {
            let mut seen: std::collections::HashSet<SlotKey> =
                same.iter().map(|r| r.key.clone()).collect();
            seen.insert((key_ent, key_attr));
            for (i, s) in self.search(q, top_k * 2, min_score) {
                let r = &self.records[i];
                if !seen.insert(r.key.clone()) {
                    continue;
                }
                out.push(
                    serde_json::json!({"entity": r.entity, "attribute": r.attribute,
                                            "value": r.value,
                                            "score": crate::search::round4(s),
                                            "why": "similar_slot"}),
                );
                if out.len() >= top_k {
                    break;
                }
            }
        }
        out
    }

    /// `contenders_for(entity, attribute)`: active (`contested`) records at
    /// the slot, in record order.
    pub fn contenders_for(&self, entity: &str, attribute: &str) -> Vec<&CortexRecord> {
        let key = slot_key(entity, attribute);
        self.contested
            .get(&key)
            .map(|v| {
                v.iter()
                    .map(|&i| &self.records[i])
                    .filter(|r| r.status == "contested")
                    .collect()
            })
            .unwrap_or_default()
    }

    /// `CortexStore.search` (`memory/cortex.py:1508`): cosine of the query
    /// against every current record with an embedding; positive cosines of
    /// assistant-origin records are multiplied by
    /// [`ASSISTANT_FACT_SCORE_MULT`]; `min_score` applies after that
    /// penalty; stable descending sort; cut to `top_k`. Returns
    /// `(record index, score)`.
    pub fn search(&self, q: &[f32], top_k: usize, min_score: f64) -> Vec<(usize, f64)> {
        let qn = unit_f32(q);
        let mut scored: Vec<(usize, f64)> = Vec::new();
        for (i, r) in self.records.iter().enumerate() {
            if r.status != "current" {
                continue;
            }
            let Some(unit) = r.unit.as_deref() else {
                continue;
            };
            let s = dot_f32(unit, &qn) as f64;
            let assistant = !crate::mutants::active("w2d-cortex-no-assistant-penalty")
                && r.origin() == "assistant";
            let s = if s > 0.0 && assistant {
                s * ASSISTANT_FACT_SCORE_MULT
            } else {
                s
            };
            if s >= min_score {
                scored.push((i, s));
            }
        }
        scored.sort_by(|a, b| b.1.total_cmp(&a.1));
        scored.truncate(top_k);
        scored
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

/// `hydrate_cortex` (`storage/sync.py:273`) over `load_facts`
/// (`storage/postgres.py:1918`): every `facts` row in id order, then the
/// index rebuild. A row whose vector differs from the live dimension refuses, as
/// `_refuse_on_stale_hydrated_dims` does at service init.
pub async fn hydrate(client: &Client, dim: usize) -> Result<CortexStore> {
    crate::txn::with_client(
        client,
        |client| async move { hydrate_locked(client, dim).await },
    )
    .await
}

fn check_dimension(id: i64, embedding: Option<&[f32]>, dim: usize) -> Result<()> {
    if let Some(e) = embedding
        && e.len() != dim
    {
        bail!("fact {id} has a {}-dim vector, expected {dim}", e.len());
    }
    Ok(())
}

async fn hydrate_locked(client: &Client, dim: usize) -> Result<CortexStore> {
    let rows = client
        .query(
            "SELECT id, entity, attribute, entity_norm, attribute_norm, value, polarity, \
             status, confidence::text, origin, support, provenance, asserted_at, last_confirmed, \
             supersedes_value, superseded_by_value, superseded_at, embedding::real[], \
             entity_id, object_entity_id, freshness_class, kind, value_norm, stance, \
             authority, distortion_tolerance, tx_time, valid_time, hlc_phys, hlc_logical, \
             writer_id, session_id, version FROM facts ORDER BY id",
            &[],
        )
        .await?;
    let mut records = Vec::with_capacity(rows.len());
    for r in rows {
        let id: i64 = r.get(0);
        let embedding: Option<Vec<f32>> = r.get(17);
        check_dimension(id, embedding.as_deref(), dim)?;
        let entity: String = r.get(1);
        let attribute: String = r.get(2);
        let freshness: Option<String> = r.get(20);
        let kind: Option<String> = r.get(21);
        let version: Option<i32> = r.get(32);
        let mut rec = CortexRecord {
            id,
            key: (String::new(), String::new()),
            unit: None,
            entity,
            attribute,
            entity_norm: r.get(3),
            attribute_norm: r.get(4),
            value: r.get(5),
            polarity: r.get(6),
            status: r.get(7),
            confidence: r.get::<_, String>(8).parse()?,
            origin_column: r.get(9),
            support: json_strings(r.get(10)),
            provenance: json_strings(r.get(11)),
            asserted_at: r.get(12),
            last_confirmed: r.get(13),
            supersedes_value: r.get(14),
            superseded_by_value: r.get(15),
            superseded_at: r.get(16),
            embedding: embedding.map(Arc::from),
            slot_embedding: None,
            entity_id: r.get(18),
            object_entity_id: r.get(19),
            freshness_class: freshness
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "evergreen".into()),
            kind: kind
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "scalar".into()),
            value_norm: r.get(22),
            stance: r.get(23),
            authority: r.get(24),
            distortion_tolerance: r.get(25),
            tx_time: r.get(26),
            valid_time: r.get(27),
            hlc_phys: r.get(28),
            hlc_logical: r.get(29),
            writer_id: r.get(30),
            session_id: r.get(31),
            version: version.filter(|&v| v != 0).unwrap_or(1),
        };
        rec.refresh_derived();
        records.push(rec);
    }
    let mut store = CortexStore {
        records,
        ..CortexStore::default()
    };
    // `_reindex_current` marks each demoted record's slot dirty, so the
    // next save persists the load-time healing (`cortex.py:1975`).
    let demoted = store.reindex();
    for i in demoted {
        let key = store.records[i].key.clone();
        store.dirty_slots.insert(key);
    }
    Ok(store)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    fn indexed(records: Vec<CortexRecord>) -> CortexStore {
        let mut s = CortexStore {
            records,
            ..Default::default()
        };
        s.reindex();
        s
    }

    fn member(entity: &str, attribute: &str, value: &str, status: &str) -> CortexRecord {
        let mut r = rec(entity, attribute, value);
        r.kind = "member".into();
        r.status = status.into();
        r
    }

    #[test]
    fn lookup_is_case_and_separator_insensitive_and_current_only() {
        let mut old = rec("Dev Box", "OS", "Debian 12");
        old.status = "superseded".into();
        let s = indexed(vec![old, rec("Dev Box", "OS", "Debian 13")]);
        assert_eq!(
            s.lookup("dev_box", "os").map(|r| r.value.as_str()),
            Some("Debian 13")
        );
        assert!(s.lookup("dev box", "kernel").is_none());
    }

    #[test]
    fn slot_kind_follows_python() {
        let s = indexed(vec![
            rec("box", "os", "debian"),
            member("box", "users", "alice", "current"),
            member("box", "groups", "admins", "removed"),
        ]);
        assert_eq!(s.slot_kind("box", "os"), Some("scalar"));
        assert_eq!(s.slot_kind("box", "users"), Some("set"));
        // Only a removed member row: still a set slot (historical members).
        assert_eq!(s.slot_kind("box", "groups"), Some("set"));
        assert_eq!(s.slot_kind("box", "kernel"), None);
        let rows: Vec<&str> = s
            .member_rows("box", "groups")
            .iter()
            .map(|r| r.value.as_str())
            .collect();
        assert_eq!(rows, ["admins"]);
        assert!(s.members("box", "groups").is_empty());
    }

    #[test]
    fn candidates_put_same_entity_first_then_similar_slots() {
        let mut older = rec("box", "kernel", "6.1");
        older.last_confirmed = 500.0;
        older.asserted_at = 500.0;
        let mut newer = rec("box", "shell", "zsh");
        newer.last_confirmed = 2000.0;
        let mut similar = rec("laptop", "os", "macOS");
        similar.embedding = Some(vec![1.0, 0.0].into());
        similar.refresh_derived();
        let mut far = rec("printer", "toner", "low");
        far.embedding = Some(vec![0.0, 1.0].into());
        far.refresh_derived();
        let s = indexed(vec![older, newer, rec("box", "os", "debian"), similar, far]);
        let got = s.candidates_for("box", "os", Some(&[1.0, 0.0]), 5, 0.35);
        let slots: Vec<(String, String)> = got
            .iter()
            .map(|c| {
                (
                    c["attribute"].as_str().unwrap().into(),
                    c["why"].as_str().unwrap().into(),
                )
            })
            .collect();
        assert_eq!(
            slots,
            [
                ("shell".to_string(), "same_entity".to_string()),
                ("kernel".to_string(), "same_entity".to_string()),
                ("os".to_string(), "similar_slot".to_string()),
            ]
        );
        assert_eq!(got[0]["score"], serde_json::Value::Null);
        assert_eq!(got[2]["entity"], "laptop");
        assert_eq!(got[2]["score"], serde_json::json!(1.0));
    }

    pub(crate) fn rec(entity: &str, attribute: &str, value: &str) -> CortexRecord {
        let mut r = CortexRecord {
            id: 0,
            entity: entity.into(),
            attribute: attribute.into(),
            entity_norm: norm_key(entity),
            attribute_norm: norm_key(attribute),
            value: value.into(),
            polarity: "+".into(),
            status: "current".into(),
            confidence: 0.7,
            origin_column: None,
            support: vec!["user".into()],
            provenance: Vec::new(),
            asserted_at: 1000.0,
            last_confirmed: 1000.0,
            supersedes_value: None,
            superseded_by_value: None,
            superseded_at: None,
            embedding: None,
            slot_embedding: None,
            entity_id: None,
            object_entity_id: None,
            freshness_class: "evergreen".into(),
            kind: "scalar".into(),
            value_norm: None,
            stance: None,
            authority: None,
            distortion_tolerance: None,
            tx_time: None,
            valid_time: None,
            hlc_phys: None,
            hlc_logical: None,
            writer_id: None,
            session_id: None,
            version: 1,
            key: (String::new(), String::new()),
            unit: None,
        };
        r.refresh_derived();
        r
    }

    pub(crate) fn axis(i: usize, cos: f32) -> Vec<f32> {
        // A unit vector at cosine `cos` to e0, in the e0/e_i plane.
        let mut v = vec![0.0_f32; DIM];
        v[0] = cos;
        v[i] = (1.0 - cos * cos).max(0.0).sqrt();
        v
    }

    pub(crate) fn store(mut recs: Vec<CortexRecord>) -> CortexStore {
        for (i, r) in recs.iter_mut().enumerate() {
            r.id = i as i64 + 1;
            r.refresh_derived();
        }
        let mut s = CortexStore {
            records: recs,
            ..CortexStore::default()
        };
        s.reindex();
        s
    }

    #[test]
    fn hydration_dimension_follows_live_embedder() {
        for dim in [384, 1024] {
            assert!(check_dimension(1, Some(&vec![0.0; dim]), dim).is_ok());
            assert!(check_dimension(2, None, dim).is_ok());
            assert!(check_dimension(3, Some(&vec![0.0; dim + 1]), dim).is_err());
        }
        assert!(check_dimension(4, Some(&vec![0.0; 1024]), 384).is_err());
    }

    #[test]
    fn norm_key_matches_python() {
        assert_eq!(norm_key("  NEBULA-SERPENT "), "nebula-serpent");
        assert_eq!(norm_key("nebula serpent"), "nebula-serpent");
        assert_eq!(norm_key("nebula_._serpent"), "nebula-serpent");
        assert_eq!(norm_key("-/Payments DB/-"), "payments-db");
        assert_eq!(norm_key("Straße"), "strasse");
        assert_eq!(norm_key("a\u{1c}b"), "a-b");
        assert_eq!(norm_key("bench server?"), "bench-server?");
        assert_eq!(norm_key(""), "");
    }

    #[test]
    fn origin_is_strongest_support_tier() {
        let mut r = rec("e", "a", "v");
        r.support = vec!["assistant".into(), "agent".into()];
        assert_eq!(r.origin(), "agent");
        r.support = vec!["assistant".into()];
        assert_eq!(r.origin(), "assistant");
        r.support = vec!["other".into()];
        assert_eq!(r.origin(), "");
    }

    #[test]
    fn search_penalises_positive_assistant_cosines_before_the_floor() {
        let mut user = rec("u", "a", "v");
        user.embedding = Some(axis(1, 0.5).into());
        let mut asst = rec("s", "a", "v");
        asst.support = vec!["assistant".into()];
        asst.embedding = Some(axis(2, 0.5).into());
        let mut neg = rec("n", "a", "v");
        neg.support = vec!["assistant".into()];
        neg.embedding = Some(axis(3, -0.3).into());
        let mut old = rec("o", "a", "v");
        old.status = "superseded".into();
        old.embedding = Some(axis(4, 0.9).into());
        let s = store(vec![user, asst, neg, old]);
        let q = axis(5, 1.0);
        let hits = s.search(&q, 5, -1.0);
        assert_eq!(hits.iter().map(|h| h.0).collect::<Vec<_>>(), vec![0, 1, 2]);
        assert!((hits[1].1 - 0.425).abs() < 1e-6);
        assert!((hits[2].1 + 0.3).abs() < 1e-6, "negative cosine not scaled");
        // The floor applies to the penalised score: 0.425 < 0.45.
        assert_eq!(s.search(&q, 5, 0.45).len(), 1);
        assert_eq!(s.search(&q, 1, -1.0).len(), 1);
    }

    #[test]
    fn reindex_collects_members_and_heals_duplicate_scalars() {
        let mut a = rec("E", "attr", "old");
        a.last_confirmed = 5.0;
        let mut b = rec("e", "ATTR", "new");
        b.last_confirmed = 9.0;
        let mut m1 = rec("e", "tags", "x");
        m1.kind = "member".into();
        let mut m2 = rec("e", "tags", "y");
        m2.kind = "member".into();
        let mut c = rec("e", "attr", "rival");
        c.status = "contested".into();
        let s = store(vec![a, b, m1, m2, c]);
        assert_eq!(s.records[0].status, "superseded");
        assert_eq!(s.records[0].superseded_by_value.as_deref(), Some("new"));
        assert_eq!(s.records[0].superseded_at, Some(9.0));
        assert_eq!(s.current.get(&slot_key("e", "attr")), Some(&1));
        let members: Vec<_> = s
            .members("E", "Tags")
            .iter()
            .map(|r| r.value.clone())
            .collect();
        assert_eq!(members, vec!["x", "y"]);
        assert_eq!(s.contenders_for("e", "attr")[0].value, "rival");
        assert!(s.contenders_for("e", "tags").is_empty());
    }
}
