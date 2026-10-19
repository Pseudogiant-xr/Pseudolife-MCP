//! The cortex write primitives (`memory/cortex.py` `CortexStore.write_fact`
//! and its helpers), for the branches `memory_fact_set` can reach: support in
//! {user, action, agent}, no stance, no provenance, never `force_contend`,
//! never `dream`. Every mutation marks its slot in `dirty_slots`; every audit
//! row marks `meta_dirty` (`_log`). Persistence is `cortex_sync.rs`.
//!
//! Not ported because no MCP call reaches them: the quarantine park
//! (`force_contend`), the dream guards and tier marks (`dream=True`), and the
//! assistant-origin park (`support="assistant"`). `fact_set.rs` never passes
//! those inputs.

use std::sync::Arc;

use serde_json::{Map, Value, json};

use super::cortex::{
    CortexRecord, CortexStore, SlotKey, norm_key, py_casefold, py_isspace, slot_key,
};
use crate::labels::Resolved;

/// `SUPERSESSION_LOG_CAP` (`cortex.py:148`).
pub const SUPERSESSION_LOG_CAP: usize = 200;
/// `_ECHO_CONTAINMENT`, `_ECHO_NEGATORS`, `_ECHO_MIN_TOKENS` (`cortex.py:83-94`).
const ECHO_CONTAINMENT: f64 = 0.75;
const ECHO_NEGATORS: [&str; 5] = ["not", "no", "never", "none", "without"];
const ECHO_MIN_TOKENS: usize = 3;
/// `_DIRECT_TIERS` (`cortex.py:210`).
const DIRECT_TIERS: [&str; 2] = ["user", "action"];
/// The refusal `write_fact` raises at a set-valued slot (`cortex.py:503`).
pub const SET_SLOT_REFUSAL: &str =
    "slot holds a set; use add_member/remove_member \u{2014} scalar writes are rejected";

/// `_norm_value` (`cortex.py:73`): strip and casefold.
pub fn norm_value(s: &str) -> String {
    py_casefold(s.trim_matches(py_isspace))
}

/// `_norm_freshness` (`cortex.py:62`): a known class, else `evergreen`.
pub fn norm_freshness(c: &str) -> String {
    let c = py_casefold(c.trim_matches(py_isspace));
    if ["evergreen", "slow", "volatile"].contains(&c.as_str()) {
        c
    } else {
        "evergreen".into()
    }
}

/// `_norm_support` (`cortex.py:199`): a known tier, else None.
pub fn norm_support(s: Option<&str>) -> Option<&'static str> {
    let s = py_casefold(s.unwrap_or("").trim_matches(py_isspace));
    super::cortex::SUPPORT_PRECEDENCE
        .into_iter()
        .find(|t| *t == s)
}

/// `_rank` (`cortex.py:265`): the provenance tier rank; unknown is 0.
fn rank(origin: Option<&str>) -> i32 {
    match py_casefold(origin.unwrap_or("").trim_matches(py_isspace)).as_str() {
        "user" => 3,
        "action" => 2,
        "agent" => 1,
        _ => 0,
    }
}

/// ASCII alphanumeric runs (`_VALUE_TOKEN_RE`, `[A-Za-z0-9]+`).
fn value_tokens(s: &str) -> std::collections::BTreeSet<&str> {
    s.split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|t| !t.is_empty())
        .collect()
}

/// `_is_compression_echo` (`cortex.py:97`): `new` is a strictly shorter
/// restatement whose tokens (at least three) are nearly all in `cur`, with
/// no negator on either side and no novel digit-bearing token.
pub fn is_compression_echo(new: &str, cur: &str) -> bool {
    let (ns, cs) = (norm_value(new), norm_value(cur));
    if ns.is_empty() || ns.chars().count() >= cs.chars().count() {
        return false;
    }
    let nt = value_tokens(&ns);
    if nt.len() < ECHO_MIN_TOKENS {
        return false;
    }
    let ct = value_tokens(&cs);
    if nt
        .iter()
        .chain(ct.iter())
        .any(|t| ECHO_NEGATORS.contains(t))
    {
        return false;
    }
    if nt
        .difference(&ct)
        .any(|t| t.chars().any(|c| c.is_ascii_digit()))
    {
        return false;
    }
    nt.intersection(&ct).count() as f64 / nt.len() as f64 >= ECHO_CONTAINMENT
}

/// `_pick` (`cortex.py:237`): `INHERIT` takes the landing record's label,
/// an explicit value (None clears) wins.
fn pick(label: &Resolved, inherit: Option<&Option<String>>) -> Option<String> {
    match label {
        Resolved::Inherit => inherit.cloned().flatten(),
        Resolved::Set(v) => v.clone(),
    }
}

/// Python `max(a, b)` / `min(a, b)` on floats: the first unless the second
/// is strictly greater / smaller.
pub(super) fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}
pub(super) fn py_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

pub(super) fn add_unique(v: &mut Vec<String>, s: &str) {
    if !v.iter().any(|x| x == s) {
        v.push(s.to_string());
    }
}

/// `_mark_tier(rec, sup, dream=False)`: a direct write at a user or action
/// tier clears that tier's dream mark. Called before adding `sup`.
pub(super) fn mark_tier_direct(rec: &mut CortexRecord, sup: &str) {
    if DIRECT_TIERS.contains(&sup) {
        let mark = format!("dream:{sup}");
        rec.provenance.retain(|p| *p != mark);
    }
}

/// One `memory_fact_set` write, as `service.cortex_write` hands it to
/// `CortexStore.write_fact`.
pub struct FactWrite<'a> {
    pub entity: &'a str,
    pub attribute: &'a str,
    pub value: &'a str,
    pub embedding: Arc<[f32]>,
    pub slot_embedding: Option<Arc<[f32]>>,
    pub confidence: f64,
    /// The normalised support tier (`_norm_support`).
    pub support: Option<&'static str>,
    /// `time.time()` at the write; also its `tx_time` and `valid_time`.
    pub now: f64,
    pub hlc: (i64, i64),
    pub writer_id: Option<String>,
    pub session_id: Option<String>,
    pub freshness_class: String,
    pub authority: Resolved,
    pub distortion_tolerance: Resolved,
}

/// The record a `_log` row is written against: its slot, its value
/// (`old_value`) and its confidence (the base of `confidence_delta`).
pub(crate) struct LogAnchor {
    pub entity: String,
    pub attribute: String,
    pub value: String,
    pub confidence: f64,
}

impl LogAnchor {
    pub(super) fn of(r: &CortexRecord) -> LogAnchor {
        LogAnchor {
            entity: r.entity.clone(),
            attribute: r.attribute.clone(),
            value: r.value.clone(),
            confidence: r.confidence,
        }
    }
}

/// The rest of a `_log` call.
pub(crate) struct LogRow<'a> {
    pub new_value: &'a str,
    pub new_conf: f64,
    pub t: f64,
    pub decision: &'a str,
    pub reason: &'a str,
    pub writer_id: Option<&'a str>,
    pub session_id: Option<&'a str>,
}

/// `WriteResult`: the action and the record it names (an index into
/// `records`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WriteOutcome {
    pub action: &'static str,
    pub record: usize,
}

impl CortexStore {
    /// The configuration `CortexStore.__init__` takes, and the resident
    /// supersession log and dream cursor `hydrate_cortex` loads
    /// (`storage/sync.py:312-313`) from the startup metadata.
    pub fn configure(&mut self, cfg: &crate::config::CortexConfig, metadata: &Value) {
        self.protect_provenance = cfg.protect_provenance;
        self.supersede_confidence_margin = cfg.supersede_confidence_margin;
        self.reinforce_rate = cfg.reinforce_rate;
        self.supersession_log = metadata["cortex_supersession_log"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        self.dream_cursor = metadata["cortex_dream_cursor"].as_f64().unwrap_or(0.0);
    }

    /// `_active_contender(key)`: the first `contested` record at the slot.
    pub(super) fn active_contender(&self, key: &SlotKey) -> Option<usize> {
        self.contested
            .get(key)?
            .iter()
            .copied()
            .find(|&i| self.records[i].status == "contested")
    }

    fn reinforce(&self, c: f64) -> f64 {
        py_min(1.0, c + (1.0 - c) * self.reinforce_rate)
    }

    /// `_reinforce` then `min(1, max(.., confidence))`, as confirm does.
    pub(super) fn confirmed_confidence(&self, old: f64, confidence: f64) -> f64 {
        if crate::mutants::active("mcp-fact-set-no-confirm-reinforce") {
            return py_max(old, confidence);
        }
        py_min(1.0, py_max(self.reinforce(old), confidence))
    }

    /// `_should_supersede` (`cortex.py:715`): HLC order first, the wall
    /// clock only to break an HLC tie, then the confidence margin.
    fn should_supersede(&self, cur: &CortexRecord, conf: f64, hlc: (i64, i64), t: f64) -> bool {
        let cur_hlc = (
            cur.hlc_phys.unwrap_or(0),
            i64::from(cur.hlc_logical.unwrap_or(0)),
        );
        if hlc < cur_hlc {
            return false;
        }
        if hlc == cur_hlc && t < cur.asserted_at {
            return false;
        }
        conf >= cur.confidence - self.supersede_confidence_margin
    }

    /// `_log` (`cortex.py:731`): one audit row, capped in place.
    #[allow(clippy::too_many_arguments)]
    fn log(
        &mut self,
        anchor: usize,
        new_value: &str,
        new_conf: f64,
        t: f64,
        decision: &str,
        reason: &str,
        w: &FactWrite<'_>,
    ) {
        let anchor = LogAnchor::of(&self.records[anchor]);
        let row = LogRow {
            new_value,
            new_conf,
            t,
            decision,
            reason,
            writer_id: w.writer_id.as_deref(),
            session_id: w.session_id.as_deref(),
        };
        self.log_row(&anchor, &row);
    }

    /// `_log` against an explicit anchor: a record that may not be in
    /// `records` (the cap's rejected member) or a write without a writer
    /// (`remove_member`).
    pub(crate) fn log_row(&mut self, anchor: &LogAnchor, w: &LogRow<'_>) {
        self.meta_dirty = true;
        let mut row = Map::new();
        row.insert("entity".into(), json!(anchor.entity));
        row.insert("attribute".into(), json!(anchor.attribute));
        row.insert("old_value".into(), json!(anchor.value));
        row.insert("new_value".into(), json!(w.new_value));
        row.insert("decision".into(), json!(w.decision));
        row.insert("reason".into(), json!(w.reason));
        row.insert(
            "confidence_delta".into(),
            json!(crate::store::round4(w.new_conf - anchor.confidence)),
        );
        row.insert("timestamp".into(), json!(w.t));
        row.insert("writer_id".into(), json!(w.writer_id));
        row.insert("session_id".into(), json!(w.session_id));
        self.supersession_log.push(Value::Object(row));
        if self.supersession_log.len() > SUPERSESSION_LOG_CAP {
            let excess = self.supersession_log.len() - SUPERSESSION_LOG_CAP;
            self.supersession_log.drain(..excess);
        }
    }

    /// A new record for this write (`_insert`'s and `_contend`'s fields).
    fn new_record(
        &self,
        w: &FactWrite<'_>,
        status: &str,
        supersedes: Option<String>,
        inherit_from: Option<usize>,
    ) -> CortexRecord {
        let inherit = inherit_from.map(|i| &self.records[i]);
        let mut rec = CortexRecord {
            id: 0,
            entity: w.entity.to_string(),
            attribute: w.attribute.to_string(),
            entity_norm: norm_key(w.entity),
            attribute_norm: norm_key(w.attribute),
            value: w.value.to_string(),
            polarity: "+".into(),
            status: status.into(),
            confidence: w.confidence,
            origin_column: None,
            support: w.support.map(|s| vec![s.to_string()]).unwrap_or_default(),
            provenance: Vec::new(),
            asserted_at: w.now,
            last_confirmed: w.now,
            supersedes_value: supersedes,
            superseded_by_value: None,
            superseded_at: None,
            embedding: Some(w.embedding.clone()),
            slot_embedding: w.slot_embedding.clone(),
            entity_id: None,
            object_entity_id: None,
            freshness_class: norm_freshness(&w.freshness_class),
            kind: "scalar".into(),
            value_norm: None,
            stance: None,
            authority: pick(&w.authority, inherit.map(|r| &r.authority)),
            distortion_tolerance: pick(
                &w.distortion_tolerance,
                inherit.map(|r| &r.distortion_tolerance),
            ),
            tx_time: Some(w.now),
            valid_time: Some(w.now),
            hlc_phys: Some(w.hlc.0),
            hlc_logical: Some(w.hlc.1 as i32),
            writer_id: w.writer_id.clone(),
            session_id: w.session_id.clone(),
            version: 1,
            key: (String::new(), String::new()),
            unit: None,
        };
        rec.refresh_derived();
        rec
    }

    /// `_insert`: append a current record and index it.
    fn insert(
        &mut self,
        w: &FactWrite<'_>,
        supersedes: Option<String>,
        inherit_from: Option<usize>,
    ) -> usize {
        let rec = self.new_record(w, "current", supersedes, inherit_from);
        let key = rec.key.clone();
        self.records.push(rec);
        let i = self.records.len() - 1;
        self.current.insert(key, i);
        i
    }

    /// `_confirm` (`cortex.py:621`): the standing record absorbs the write.
    fn confirm(&mut self, idx: usize, w: &FactWrite<'_>) -> WriteOutcome {
        let conf = self.confirmed_confidence(self.records[idx].confidence, w.confidence);
        let cur = &mut self.records[idx];
        cur.stance = None;
        cur.authority = pick(&w.authority, Some(&cur.authority));
        cur.distortion_tolerance = pick(&w.distortion_tolerance, Some(&cur.distortion_tolerance));
        cur.last_confirmed = w.now;
        if let Some(sup) = w.support {
            mark_tier_direct(cur, sup);
            add_unique(&mut cur.support, sup);
        }
        cur.confidence = conf;
        if w.slot_embedding.is_some() && cur.slot_embedding.is_none() {
            cur.slot_embedding = w.slot_embedding.clone();
        }
        cur.tx_time = Some(w.now);
        cur.hlc_phys = Some(w.hlc.0);
        cur.hlc_logical = Some(w.hlc.1 as i32);
        if let Some(wid) = w.writer_id.as_ref().filter(|s| !s.is_empty()) {
            cur.writer_id = Some(wid.clone());
        }
        if let Some(sid) = w.session_id.as_ref().filter(|s| !s.is_empty()) {
            cur.session_id = Some(sid.clone());
        }
        WriteOutcome {
            action: "confirmed",
            record: idx,
        }
    }

    /// `_settle_matching_contender` (`cortex.py:1331`): a write that made the
    /// parked contender's value current supersedes the contender and keeps
    /// every non-assistant tier that backed it.
    pub(super) fn settle_matching_contender(
        &mut self,
        key: &SlotKey,
        new: usize,
        w: &FactWrite<'_>,
    ) {
        let Some(c) = self.active_contender(key) else {
            return;
        };
        if norm_value(&self.records[c].value) != norm_value(&self.records[new].value) {
            return;
        }
        let carried: Vec<String> = self.records[c]
            .support
            .iter()
            .filter(|s| *s != "assistant")
            .cloned()
            .collect();
        let contender_prov = self.records[c].provenance.clone();
        {
            let rec = &mut self.records[new];
            for sup in &carried {
                if !DIRECT_TIERS.contains(&sup.as_str()) {
                    continue;
                }
                let mark = format!("dream:{sup}");
                if contender_prov.contains(&mark) {
                    if !rec.support.contains(sup) && !rec.provenance.contains(&mark) {
                        rec.provenance.push(mark);
                    }
                } else {
                    rec.provenance.retain(|p| *p != mark);
                }
            }
            for sup in &carried {
                add_unique(&mut rec.support, sup);
            }
        }
        let (new_value, new_conf) = (
            self.records[new].value.clone(),
            self.records[new].confidence,
        );
        let contender = &mut self.records[c];
        contender.status = "superseded".into();
        contender.superseded_at = Some(w.now);
        contender.superseded_by_value = Some(new_value.clone());
        self.log(
            c,
            &new_value,
            new_conf,
            w.now,
            "resolved",
            "contender_value_now_current",
            w,
        );
    }

    /// `_contend` (`cortex.py:1238`) at a slot with a current record: park
    /// the write as the slot's one contender (or confirm the matching one).
    pub(super) fn contend(&mut self, cur: usize, w: &FactWrite<'_>, reason: &str) -> WriteOutcome {
        let key = self.records[cur].key.clone();
        if let Some(e) = self.active_contender(&key)
            && norm_value(&self.records[e].value) == norm_value(w.value)
        {
            let conf = self.confirmed_confidence(self.records[e].confidence, w.confidence);
            let ex = &mut self.records[e];
            ex.stance = None;
            ex.authority = pick(&w.authority, Some(&ex.authority));
            ex.distortion_tolerance = pick(&w.distortion_tolerance, Some(&ex.distortion_tolerance));
            ex.last_confirmed = w.now;
            if let Some(sup) = w.support {
                mark_tier_direct(ex, sup);
                add_unique(&mut ex.support, sup);
            }
            ex.confidence = conf;
            if let Some(wid) = w.writer_id.as_ref().filter(|s| !s.is_empty()) {
                ex.writer_id = Some(wid.clone());
            }
            if let Some(sid) = w.session_id.as_ref().filter(|s| !s.is_empty()) {
                ex.session_id = Some(sid.clone());
            }
            ex.tx_time = Some(w.now);
            ex.hlc_phys = Some(w.hlc.0);
            ex.hlc_logical = Some(w.hlc.1 as i32);
            self.log(
                cur,
                w.value,
                w.confidence,
                w.now,
                "contested",
                "contender_confirmed",
                w,
            );
            return WriteOutcome {
                action: "contested",
                record: e,
            };
        }
        let mut supersedes = None;
        if let Some(e) = self.active_contender(&key) {
            let ex = &mut self.records[e];
            ex.status = "superseded".into();
            ex.superseded_at = Some(w.now);
            ex.superseded_by_value = Some(w.value.to_string());
            supersedes = Some(ex.value.clone());
        }
        let rec = self.new_record(w, "contested", supersedes, Some(cur));
        self.records.push(rec);
        let i = self.records.len() - 1;
        self.contested.entry(key).or_default().push(i);
        self.log(cur, w.value, w.confidence, w.now, "contested", reason, w);
        WriteOutcome {
            action: "contested",
            record: i,
        }
    }

    /// `write_fact` (`cortex.py:438-619`) for the inputs `memory_fact_set`
    /// passes. Refuses a slot that holds current set members.
    pub fn write_fact(&mut self, w: &FactWrite<'_>) -> Result<WriteOutcome, &'static str> {
        let key = slot_key(w.entity, w.attribute);
        if self.members.get(&key).is_some_and(|m| !m.is_empty()) {
            return Err(SET_SLOT_REFUSAL);
        }
        self.dirty_slots.insert(key.clone());
        let Some(idx) = self.current.get(&key).copied() else {
            let new = self.insert(w, None, None);
            self.settle_matching_contender(&key, new, w);
            return Ok(WriteOutcome {
                action: "inserted",
                record: new,
            });
        };
        if norm_value(&self.records[idx].value) == norm_value(w.value) {
            return Ok(self.confirm(idx, w));
        }
        if is_compression_echo(w.value, &self.records[idx].value) {
            self.log(
                idx,
                w.value,
                w.confidence,
                w.now,
                "confirm",
                "compression_echo",
                w,
            );
            return Ok(self.confirm(idx, w));
        }
        let cur_origin = self.records[idx].origin();
        let tier_ok = !self.protect_provenance || rank(w.support) >= rank(Some(cur_origin));
        if tier_ok
            && self.should_supersede(&self.records[idx], w.confidence, w.hlc, w.now)
            && !crate::mutants::active("mcp-fact-set-no-supersede")
        {
            let cur = &mut self.records[idx];
            cur.status = "superseded".into();
            cur.superseded_at = Some(w.now);
            cur.superseded_by_value = Some(w.value.to_string());
            let old = cur.value.clone();
            self.log(
                idx,
                w.value,
                w.confidence,
                w.now,
                "supersede",
                "newer_wins",
                w,
            );
            let new = self.insert(w, Some(old), Some(idx));
            self.settle_matching_contender(&key, new, w);
            return Ok(WriteOutcome {
                action: "superseded",
                record: new,
            });
        }
        let reason = if tier_ok {
            "below_confidence_margin"
        } else {
            "tier_downgrade"
        };
        if !self.protect_provenance {
            // Legacy newer-wins: the conflicting value is dropped.
            self.log(idx, w.value, w.confidence, w.now, "contested", reason, w);
            return Ok(WriteOutcome {
                action: "contested",
                record: idx,
            });
        }
        Ok(self.contend(idx, w, reason))
    }
}

impl CortexStore {
    /// `forget` (`cortex.py:1610-1636`): hard-delete every record (current
    /// and superseded) at an entity, or at one exact slot, marking each
    /// slot dirty so the sync deletes its rows. The indexes are rebuilt
    /// from the kept records without the load-time healing. Returns the
    /// number removed.
    pub fn forget(&mut self, entity: &str, attribute: Option<&str>) -> usize {
        let ne = norm_key(entity);
        let na = attribute.map(norm_key);
        let before = self.records.len();
        let mut dirty = Vec::new();
        self.records.retain(|r| {
            let hit = r.key.0 == ne && na.as_ref().is_none_or(|a| r.key.1 == *a);
            if hit {
                dirty.push(r.key.clone());
            }
            !hit
        });
        let removed = before - self.records.len();
        self.dirty_slots.extend(dirty);
        if removed > 0 {
            self.current.clear();
            self.members.clear();
            self.contested.clear();
            for (i, r) in self.records.iter().enumerate() {
                match r.status.as_str() {
                    "current" if r.kind == "member" => {
                        self.members.entry(r.key.clone()).or_default().push(i)
                    }
                    "current" => {
                        self.current.insert(r.key.clone(), i);
                    }
                    "contested" => self.contested.entry(r.key.clone()).or_default().push(i),
                    _ => {}
                }
            }
        }
        removed
    }

    /// `retire_unsupported` (`cortex.py:1162-1181`): every current value at
    /// a slot whose last source was forgotten becomes `retired` at `now`
    /// (`superseded_by_value` names the source), each with an audit row;
    /// the slot leaves both indexes and is marked dirty.
    pub fn retire_unsupported(
        &mut self,
        entity: &str,
        attribute: &str,
        source_entry_id: i64,
        now: f64,
    ) -> usize {
        let key = (norm_key(entity), norm_key(attribute));
        let reason = format!("forgotten source entry {source_entry_id}");
        let mut count = 0;
        for i in 0..self.records.len() {
            if self.records[i].key != key || self.records[i].status != "current" {
                continue;
            }
            let r = &mut self.records[i];
            r.status = "retired".into();
            r.superseded_at = Some(now);
            r.superseded_by_value = Some(reason.clone());
            let anchor = LogAnchor::of(&self.records[i]);
            self.log_retired(&anchor, now, &reason);
            count += 1;
        }
        if count > 0 {
            self.current.remove(&key);
            self.members.remove(&key);
            self.dirty_slots.insert(key);
        }
        count
    }

    /// `_log(rec, None, rec.confidence, t, "retired", reason)`: no new value,
    /// a zero confidence delta, no writer.
    fn log_retired(&mut self, anchor: &LogAnchor, t: f64, reason: &str) {
        self.meta_dirty = true;
        let mut row = Map::new();
        row.insert("entity".into(), json!(anchor.entity));
        row.insert("attribute".into(), json!(anchor.attribute));
        row.insert("old_value".into(), json!(anchor.value));
        row.insert("new_value".into(), Value::Null);
        row.insert("decision".into(), json!("retired"));
        row.insert("reason".into(), json!(reason));
        row.insert("confidence_delta".into(), json!(0.0));
        row.insert("timestamp".into(), json!(t));
        row.insert("writer_id".into(), Value::Null);
        row.insert("session_id".into(), Value::Null);
        self.supersession_log.push(Value::Object(row));
        if self.supersession_log.len() > SUPERSESSION_LOG_CAP {
            let excess = self.supersession_log.len() - SUPERSESSION_LOG_CAP;
            self.supersession_log.drain(..excess);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stores::cortex::tests::{rec, store};

    fn write<'a>(
        e: &'a str,
        a: &'a str,
        v: &'a str,
        sup: &'static str,
        conf: f64,
        t: f64,
    ) -> FactWrite<'a> {
        FactWrite {
            entity: e,
            attribute: a,
            value: v,
            embedding: vec![1.0, 0.0].into(),
            slot_embedding: Some(vec![0.0, 1.0].into()),
            confidence: conf,
            support: Some(sup),
            now: t,
            hlc: ((t * 1000.0) as i64, 0),
            writer_id: Some("w".into()),
            session_id: None,
            freshness_class: "evergreen".into(),
            authority: Resolved::Inherit,
            distortion_tolerance: Resolved::Inherit,
        }
    }

    fn log_reasons(s: &CortexStore) -> Vec<(String, String)> {
        s.supersession_log
            .iter()
            .map(|r| {
                (
                    r["decision"].as_str().unwrap().to_string(),
                    r["reason"].as_str().unwrap().to_string(),
                )
            })
            .collect()
    }

    #[test]
    fn insert_then_confirm_reinforces_and_unions_support() {
        let mut s = CortexStore::default();
        let r = s
            .write_fact(&write("Dev Box", "OS", "Debian", "agent", 0.7, 10.0))
            .unwrap();
        assert_eq!(r.action, "inserted");
        assert!(!s.meta_dirty, "an insert logs nothing");
        let r2 = s
            .write_fact(&write("dev_box", "os", " debian ", "user", 0.5, 20.0))
            .unwrap();
        assert_eq!(
            r2,
            WriteOutcome {
                action: "confirmed",
                record: r.record
            }
        );
        let c = &s.records[r.record];
        // min(1, max(0.7 + 0.3 * 0.34, 0.5))
        assert!((c.confidence - (0.7 + 0.3 * 0.34)).abs() < 1e-12);
        assert_eq!(c.origin(), "user");
        assert_eq!(c.last_confirmed, 20.0);
        assert_eq!(c.asserted_at, 10.0);
        assert_eq!(c.valid_time, Some(10.0));
        assert_eq!(c.tx_time, Some(20.0));
        assert_eq!(c.hlc_phys, Some(20000));
        assert_eq!(s.dirty_slots.len(), 1);
        assert!(!s.meta_dirty);
    }

    #[test]
    fn compression_echo_confirms_and_logs() {
        let mut s = CortexStore::default();
        s.write_fact(&write(
            "pipe",
            "schedule",
            "the deploy pipeline runs nightly on the bench box",
            "agent",
            0.7,
            1.0,
        ))
        .unwrap();
        let r = s
            .write_fact(&write(
                "pipe",
                "schedule",
                "deploy pipeline runs nightly",
                "agent",
                0.7,
                2.0,
            ))
            .unwrap();
        assert_eq!(r.action, "confirmed");
        assert_eq!(s.records.len(), 1);
        assert_eq!(
            log_reasons(&s),
            [("confirm".into(), "compression_echo".into())]
        );
        assert!(s.meta_dirty);
    }

    #[test]
    fn echo_rules_match_python() {
        // Values from cortex._is_compression_echo on CPython 3.11.
        assert!(is_compression_echo(
            "deploy pipeline runs nightly",
            "the deploy pipeline runs nightly on the bench box"
        ));
        assert!(!is_compression_echo(
            "pipeline runs",
            "the pipeline runs nightly"
        ));
        assert!(!is_compression_echo(
            "pipeline not running now",
            "the pipeline not running now really"
        ));
        assert!(!is_compression_echo(
            "api port 9090 open",
            "the api port 8080 open today"
        ));
        assert!(!is_compression_echo("the same text", "the same text"));
        assert!(!is_compression_echo(
            "alpha beta gamma delta",
            "alpha beta zeta theta omega x"
        ));
    }

    #[test]
    fn newer_user_write_supersedes_and_inherits_labels() {
        let mut s = CortexStore::default();
        let mut first = write("db", "host", "10.0.0.5", "agent", 0.8, 1.0);
        first.authority = Resolved::Set(Some("directive".into()));
        s.write_fact(&first).unwrap();
        let r = s
            .write_fact(&write("db", "host", "10.0.0.9", "user", 0.8, 2.0))
            .unwrap();
        assert_eq!(r.action, "superseded");
        let old = &s.records[0];
        assert_eq!(old.status, "superseded");
        assert_eq!(old.superseded_by_value.as_deref(), Some("10.0.0.9"));
        assert_eq!(old.superseded_at, Some(2.0));
        let new = &s.records[r.record];
        assert_eq!(new.supersedes_value.as_deref(), Some("10.0.0.5"));
        assert_eq!(new.authority.as_deref(), Some("directive"));
        assert_eq!(s.lookup("db", "host").unwrap().value, "10.0.0.9");
        assert_eq!(log_reasons(&s), [("supersede".into(), "newer_wins".into())]);
        assert_eq!(s.supersession_log[0]["confidence_delta"], json!(0.0));
    }

    #[test]
    fn weaker_tier_and_low_confidence_park_one_contender() {
        let mut s = CortexStore::default();
        s.write_fact(&write("db", "host", "a", "user", 0.9, 1.0))
            .unwrap();
        let r = s
            .write_fact(&write("db", "host", "b", "agent", 0.9, 2.0))
            .unwrap();
        assert_eq!(r.action, "contested");
        assert_eq!(s.records[r.record].status, "contested");
        // The same value again refreshes the contender.
        let r2 = s
            .write_fact(&write("db", "host", "B ", "agent", 0.5, 3.0))
            .unwrap();
        assert_eq!(r2, r);
        assert!((s.records[r.record].confidence - (0.9 + 0.1 * 0.34)).abs() < 1e-12);
        // A user write below the margin parks a new value and retires the old contender.
        let r3 = s
            .write_fact(&write("db", "host", "c", "user", 0.5, 4.0))
            .unwrap();
        assert_eq!(r3.action, "contested");
        assert_ne!(r3.record, r.record);
        assert_eq!(s.records[r.record].status, "superseded");
        assert_eq!(s.records[r3.record].supersedes_value.as_deref(), Some("b"));
        assert_eq!(s.contenders_for("db", "host").len(), 1);
        assert_eq!(s.lookup("db", "host").unwrap().value, "a");
        assert_eq!(
            log_reasons(&s),
            [
                ("contested".into(), "tier_downgrade".into()),
                ("contested".into(), "contender_confirmed".into()),
                ("contested".into(), "below_confidence_margin".into()),
            ]
        );
        // Writing the contender's value with enough weight settles it.
        let r4 = s
            .write_fact(&write("db", "host", "c", "user", 0.9, 5.0))
            .unwrap();
        assert_eq!(r4.action, "superseded");
        assert_eq!(s.records[r3.record].status, "superseded");
        assert_eq!(
            s.records[r3.record].superseded_by_value.as_deref(),
            Some("c")
        );
        assert!(s.contenders_for("db", "host").is_empty());
        assert_eq!(
            log_reasons(&s).last().unwrap().1,
            "contender_value_now_current"
        );
    }

    #[test]
    fn protect_provenance_off_drops_the_low_confidence_value() {
        let mut s = CortexStore {
            protect_provenance: false,
            ..CortexStore::default()
        };
        s.write_fact(&write("db", "host", "a", "user", 0.9, 1.0))
            .unwrap();
        // Tier is ignored: an agent write supersedes.
        assert_eq!(
            s.write_fact(&write("db", "host", "b", "agent", 0.9, 2.0))
                .unwrap()
                .action,
            "superseded"
        );
        let r = s
            .write_fact(&write("db", "host", "c", "agent", 0.1, 3.0))
            .unwrap();
        assert_eq!(r.action, "contested");
        assert_eq!(s.records[r.record].value, "b");
        assert_eq!(s.records.len(), 2);
        assert!(s.contenders_for("db", "host").is_empty());
    }

    #[test]
    fn an_older_hlc_never_supersedes() {
        let mut s = CortexStore::default();
        let mut first = write("db", "host", "a", "agent", 0.8, 10.0);
        first.hlc = (5000, 3);
        s.write_fact(&first).unwrap();
        let mut late = write("db", "host", "b", "agent", 0.8, 20.0);
        late.hlc = (5000, 2);
        assert_eq!(s.write_fact(&late).unwrap().action, "contested");
        assert_eq!(log_reasons(&s)[0].1, "below_confidence_margin");
    }

    #[test]
    fn a_set_slot_refuses_and_stays_clean() {
        let mut m = rec("box", "users", "alice");
        m.kind = "member".into();
        let mut s = store(vec![m]);
        assert_eq!(
            s.write_fact(&write("Box", "Users", "bob", "user", 0.8, 1.0)),
            Err(SET_SLOT_REFUSAL)
        );
        assert!(s.dirty_slots.is_empty());
        // Only removed members: a scalar write is accepted.
        s.records[0].status = "removed".into();
        s.reindex();
        assert_eq!(
            s.write_fact(&write("Box", "Users", "bob", "user", 0.8, 1.0))
                .unwrap()
                .action,
            "inserted"
        );
    }

    #[test]
    fn the_log_keeps_the_newest_two_hundred() {
        let mut s = CortexStore::default();
        s.write_fact(&write("e", "a", "v0", "user", 0.9, 1.0))
            .unwrap();
        for i in 1..=205 {
            let v = format!("v{i}");
            s.write_fact(&write("e", "a", &v, "user", 0.9, 1.0 + i as f64))
                .unwrap();
        }
        assert_eq!(s.supersession_log.len(), SUPERSESSION_LOG_CAP);
        assert_eq!(s.supersession_log[199]["new_value"], json!("v205"));
        assert_eq!(s.supersession_log[0]["new_value"], json!("v6"));
    }

    #[test]
    fn a_direct_confirm_clears_the_dream_mark() {
        let mut r0 = rec("e", "a", "v");
        r0.support = vec!["user".into()];
        r0.provenance = vec!["dream:user".into(), "ep1".into()];
        let mut s = store(vec![r0]);
        s.write_fact(&write("e", "a", "v", "user", 0.5, 2000.0))
            .unwrap();
        assert_eq!(s.records[0].provenance, ["ep1"]);
    }

    #[test]
    fn support_and_freshness_normalise_like_python() {
        assert_eq!(norm_support(Some(" User ")), Some("user"));
        assert_eq!(norm_support(Some("boss")), None);
        assert_eq!(norm_support(None), None);
        assert_eq!(norm_freshness("Volatile"), "volatile");
        assert_eq!(norm_freshness("weekly"), "evergreen");
    }

    /// Write sequences recorded from `memory/cortex.py` on CPython 3.11
    /// (`harness/gen_w2e_cortex_write_goldens.py`): every action, record,
    /// final record field, slot index and audit row must match. Ops `add`
    /// and `remove` replay `add_member` / `remove_member`
    /// (`cortex_members.rs`); op `resolve` replays `resolve`
    /// (`cortex_resolve.rs`), whose None answer is a null action.
    const GOLDEN: &str = include_str!("../../harness/goldens/w2e-cortex-write.json");

    fn label(v: &Value) -> Resolved {
        match v {
            Value::String(s) if s == "inherit" => Resolved::Inherit,
            Value::Object(o) => Resolved::Set(o["set"].as_str().map(str::to_string)),
            other => panic!("bad label {other}"),
        }
    }

    fn record_json(r: &CortexRecord) -> Value {
        let sorted = |v: &[String]| {
            let mut v = v.to_vec();
            v.sort();
            json!(v)
        };
        json!({
            "entity": r.entity, "attribute": r.attribute, "value": r.value,
            "polarity": r.polarity, "status": r.status, "kind": r.kind,
            "confidence": r.confidence, "asserted_at": r.asserted_at,
            "last_confirmed": r.last_confirmed, "supersedes_value": r.supersedes_value,
            "superseded_by_value": r.superseded_by_value, "superseded_at": r.superseded_at,
            "tx_time": r.tx_time, "valid_time": r.valid_time, "hlc_phys": r.hlc_phys,
            "hlc_logical": r.hlc_logical, "writer_id": r.writer_id,
            "session_id": r.session_id, "freshness_class": r.freshness_class,
            "stance": r.stance, "authority": r.authority,
            "distortion_tolerance": r.distortion_tolerance,
            "support": sorted(&r.support), "provenance": sorted(&r.provenance),
            "slot_embedding": r.slot_embedding.is_some(),
            "embedding": r.embedding.as_deref().map(|e| e.iter().map(|&x| f64::from(x)).collect::<Vec<_>>()),
        })
    }

    fn embedding(v: &Value) -> Arc<[f32]> {
        v.as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_f64().unwrap() as f32)
            .collect::<Vec<_>>()
            .into()
    }

    /// One golden write: `(action, record, members_count)`.
    fn replay(
        s: &mut CortexStore,
        x: &Value,
    ) -> (Option<&'static str>, Option<usize>, Option<usize>) {
        use crate::stores::cortex_members::MemberWrite;
        let text = |k: &str| x[k].as_str().unwrap().to_string();
        let (e, a) = (text("entity"), text("attribute"));
        let now = x["now"].as_f64().unwrap();
        let count = |s: &CortexStore| Some(s.members(&e, &a).len());
        let op = x["op"].as_str().unwrap_or("fact");
        if op == "resolve" {
            let hlc = x["hlc"]
                .as_array()
                .map(|h| (h[0].as_i64().unwrap(), h[1].as_i64().unwrap()));
            let got = s.resolve(&e, &a, x["accept"].as_bool().unwrap(), now, "user", hlc);
            return (got.map(|g| g.action), got.map(|g| g.record), None);
        }
        let v = text("value");
        let (action, record, count) = match op {
            "add" => {
                let mw = MemberWrite {
                    entity: &e,
                    attribute: &a,
                    member: &v,
                    embedding: embedding(&x["embedding"]),
                    confidence: x["confidence"].as_f64().unwrap(),
                    now,
                    hlc: (x["hlc"][0].as_i64().unwrap(), x["hlc"][1].as_i64().unwrap()),
                    writer_id: x["writer_id"].as_str().map(str::to_string),
                    session_id: x["session_id"].as_str().map(str::to_string),
                };
                let got = s.add_member(&mw);
                (got.action, got.record, count(s))
            }
            "remove" => {
                let got = s.remove_member(&e, &a, &v, now);
                (got.action, got.record, count(s))
            }
            _ => {
                let emb = if x["embedding"].is_null() {
                    vec![1.0, 0.0].into()
                } else {
                    embedding(&x["embedding"])
                };
                let fw = FactWrite {
                    entity: &e,
                    attribute: &a,
                    value: &v,
                    embedding: emb,
                    slot_embedding: Some(vec![0.0, 1.0].into()),
                    confidence: x["confidence"].as_f64().unwrap(),
                    support: norm_support(x["support"].as_str()),
                    now,
                    hlc: (x["hlc"][0].as_i64().unwrap(), x["hlc"][1].as_i64().unwrap()),
                    writer_id: x["writer_id"].as_str().map(str::to_string),
                    session_id: x["session_id"].as_str().map(str::to_string),
                    freshness_class: text("freshness_class"),
                    authority: label(&x["authority"]),
                    distortion_tolerance: label(&x["distortion_tolerance"]),
                };
                let got = s.write_fact(&fw).unwrap();
                (got.action, Some(got.record), None)
            }
        };
        (Some(action), record, count)
    }

    #[test]
    fn write_sequences_match_python() {
        let golden: Value = serde_json::from_str(GOLDEN).unwrap();
        let golden = golden.as_object().unwrap();
        assert!(golden.len() >= 3);
        for (name, seq) in golden {
            let c = &seq["config"];
            let mut s = CortexStore {
                protect_provenance: c["protect_provenance"].as_bool().unwrap(),
                supersede_confidence_margin: c["supersede_confidence_margin"].as_f64().unwrap(),
                reinforce_rate: c["reinforce_rate"].as_f64().unwrap(),
                ..CortexStore::default()
            };
            for (i, x) in seq["writes"].as_array().unwrap().iter().enumerate() {
                let (action, record, count) = replay(&mut s, x);
                let want = &seq["results"][i];
                assert_eq!(json!(action), want["action"], "{name} write {i}");
                assert_eq!(
                    record.map(|r| r as u64),
                    want["record"].as_u64(),
                    "{name} write {i}"
                );
                assert_eq!(
                    count.map(|c| c as u64),
                    want["members_count"].as_u64(),
                    "{name} write {i}"
                );
            }
            let records: Vec<Value> = s.records.iter().map(record_json).collect();
            let want_records = seq["records"].as_array().unwrap();
            assert_eq!(records.len(), want_records.len(), "{name}");
            for (i, (got, want)) in records.iter().zip(want_records).enumerate() {
                assert_eq!(got, want, "{name} record {i}");
            }
            assert_eq!(
                Value::Array(s.supersession_log.clone()),
                seq["log"],
                "{name} log"
            );
            let dirty: Vec<Value> = s.dirty_slots.iter().map(|k| json!([k.0, k.1])).collect();
            assert_eq!(
                Value::Array(dirty),
                seq["dirty_slots"],
                "{name} dirty slots"
            );
            assert_eq!(json!(s.meta_dirty), seq["meta_dirty"], "{name}");
            let mut current: Vec<_> = s.current.iter().collect();
            current.sort();
            let current: Vec<Value> = current
                .into_iter()
                .map(|(k, i)| json!([[k.0, k.1], i]))
                .collect();
            assert_eq!(Value::Array(current), seq["current"], "{name} current");
            let mut members: Vec<_> = s.members.iter().collect();
            members.sort();
            let members: Vec<Value> = members
                .into_iter()
                .map(|(k, v)| json!([[k.0, k.1], v]))
                .collect();
            assert_eq!(Value::Array(members), seq["members"], "{name} members");
        }
    }
}
