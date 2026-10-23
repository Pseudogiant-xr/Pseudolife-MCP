//! The set-valued slot primitives (`memory/cortex.py`
//! `CortexStore.add_member` 754-1032, `_insert_member` 1034-1081,
//! `remove_member` 1083-1135).
//!
//! `memory_set_add` and `memory_set_remove` pass no support tier, no
//! provenance and never `dream`. The assistant-origin guards
//! (`support="assistant"`, `cortex.py:891` and `:995`), the conversion tier
//! guard (`:897`), the dream direct-write guard and tier mark (`dream=True`,
//! `:901`, `:1029`), provenance, and the removal refusal (`:1121-1128`) are
//! ported for the dream's member ops, which reach them through
//! `set_members::set_add` and `set_members::set_remove`.

use std::sync::{Arc, LazyLock};

use regex::Regex;
use serde_json::{Value, json};

use super::cortex::{CortexRecord, CortexStore, SlotKey, py_isspace, slot_key};
use super::cortex_write::{
    FactWrite, LogAnchor, LogRow, add_unique, clean_prov, direct_backed, mark_new_record,
    mark_tier, norm_support, norm_value, py_max, py_min, rank, union_prov,
};
use crate::labels::Resolved;

/// `MEMBER_DEDUP_COSINE` (`cortex.py:157`).
pub const MEMBER_DEDUP_COSINE: f64 = 0.9;
/// `MAX_CURRENT_MEMBERS` (`cortex.py:162`).
pub const MAX_CURRENT_MEMBERS: usize = 100;
/// The converted scalar's `superseded_by_value` (`cortex.py:939`).
const CONVERTED: &str = "(converted to set)";

/// `_AGGREGATE_VALUE_RE` (`cortex.py:122`). The `regex` crate's `\d` is
/// the Unicode decimal-digit class, as Python's is for a `str` pattern.
static AGGREGATE_VALUE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[$€£]?[+-]?\d").expect("aggregate regex"));

/// `_is_aggregate_value` (`cortex.py:125`): a number-led value.
pub fn is_aggregate_value(value: &str) -> bool {
    AGGREGATE_VALUE.is_match(value.trim_matches(py_isspace))
}

/// The sum of float32 products, accumulated in f64 and rounded once to
/// f32: torch's float32 `dot` and `norm` run a BLAS reduction (FMA, its own
/// summation order) that this cannot reproduce; f64 accumulation lands
/// within one f32 step of it (measured on 2,000 unit 1024-vectors: 37%
/// identical, the rest at most 4.5e-8 apart in the cosine).
fn sum_products(a: &[f32], b: &[f32]) -> f32 {
    a.iter()
        .zip(b)
        .map(|(&x, &y)| f64::from(x * y))
        .sum::<f64>() as f32
}

/// The dedup cosine (`cortex.py:975-977`): float32 `m @ e / (m.norm() *
/// e.norm() + 1e-12)`, or 0.0 when the member has no embedding.
pub fn member_cosine(member: Option<&[f32]>, emb: &[f32]) -> f64 {
    let Some(m) = member else {
        return 0.0;
    };
    let denom = sum_products(m, m).sqrt() * sum_products(emb, emb).sqrt() + 1e-12_f32;
    f64::from(sum_products(m, emb) / denom)
}

/// One member add, as `service.set_add` hands it to `add_member`.
pub struct MemberWrite<'a> {
    pub entity: &'a str,
    pub attribute: &'a str,
    pub member: &'a str,
    pub embedding: Arc<[f32]>,
    pub confidence: f64,
    /// `time.time()` at the write.
    pub now: f64,
    pub hlc: (i64, i64),
    pub writer_id: Option<String>,
    pub session_id: Option<String>,
    /// `support` as passed (`set_add`'s `origin`): the guards read it
    /// normalised, but a new member row stores it verbatim
    /// (`_insert_member(..., support=support)`, `cortex.py:1028`).
    pub support: Option<String>,
    /// The incoming provenance as passed (`_clean_prov` runs at each use).
    pub provenance: Vec<String>,
    /// A dream member op.
    pub dream: bool,
}

impl MemberWrite<'_> {
    /// `_norm_support(support)`.
    fn sup(&self) -> Option<&'static str> {
        norm_support(self.support.as_deref())
    }

    /// The write as `add_member` hands it to `_contend` and
    /// `_settle_matching_contender`: the normalised tier, the cleaned
    /// provenance, the `evergreen` default class, no stance, both labels
    /// inherited.
    fn as_fact(&self, slot_embedding: Option<Arc<[f32]>>) -> FactWrite<'_> {
        FactWrite {
            entity: self.entity,
            attribute: self.attribute,
            value: self.member,
            embedding: self.embedding.clone(),
            slot_embedding,
            confidence: self.confidence,
            support: self.sup(),
            now: self.now,
            hlc: self.hlc,
            writer_id: self.writer_id.clone(),
            session_id: self.session_id.clone(),
            freshness_class: "evergreen".into(),
            authority: Resolved::Inherit,
            distortion_tolerance: Resolved::Inherit,
            provenance: clean_prov(&self.provenance),
            stance: None,
            force_contend: false,
            dream: self.dream,
        }
    }
}

/// `WriteResult` of a member write: the action and the record it names,
/// None for a record Python never stores (invalid, capped, not found).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MemberOutcome {
    pub action: &'static str,
    pub record: Option<usize>,
}

fn outcome(action: &'static str, record: Option<usize>) -> MemberOutcome {
    MemberOutcome { action, record }
}

/// The fields `_insert_member` (`cortex.py:1034`) gives a member row that
/// do not come from its arguments.
#[allow(clippy::too_many_arguments)]
fn member_record(
    entity: &str,
    attribute: &str,
    value: &str,
    polarity: &str,
    embedding: Option<Arc<[f32]>>,
    confidence: f64,
    t: f64,
    hlc: (i64, i64),
) -> CortexRecord {
    let mut rec = CortexRecord {
        id: 0,
        entity: entity.to_string(),
        attribute: attribute.to_string(),
        entity_norm: super::cortex::norm_key(entity),
        attribute_norm: super::cortex::norm_key(attribute),
        value: value.to_string(),
        polarity: polarity.to_string(),
        status: "current".into(),
        confidence,
        origin_column: None,
        support: Vec::new(),
        provenance: Vec::new(),
        asserted_at: t,
        last_confirmed: t,
        supersedes_value: None,
        superseded_by_value: None,
        superseded_at: None,
        embedding,
        slot_embedding: None,
        entity_id: None,
        object_entity_id: None,
        freshness_class: "evergreen".into(),
        kind: "member".into(),
        value_norm: None,
        stance: None,
        authority: None,
        distortion_tolerance: None,
        tx_time: Some(t),
        valid_time: Some(t),
        hlc_phys: Some(hlc.0),
        hlc_logical: Some(hlc.1 as i32),
        writer_id: None,
        session_id: None,
        version: 1,
        key: (String::new(), String::new()),
        unit: None,
    };
    rec.refresh_derived();
    rec
}

impl CortexStore {
    /// Append a current member row and register it (`_insert_member`).
    fn push_member(&mut self, rec: CortexRecord) -> usize {
        let key = rec.key.clone();
        self.records.push(rec);
        let i = self.records.len() - 1;
        self.members.entry(key).or_default().push(i);
        i
    }

    /// The one-way scalar-to-set conversion (`cortex.py:936-970`): the
    /// scalar is superseded and logged, the drops of a non-default class
    /// and of its labels are stamped on that log row, and the old value
    /// rides on as a member with its own support, provenance and clocks.
    fn convert_to_set(&mut self, idx: usize, key: &SlotKey, w: &MemberWrite<'_>) {
        self.dirty_slots.insert(key.clone());
        {
            let cur = &mut self.records[idx];
            cur.status = "superseded".into();
            cur.superseded_at = Some(w.now);
            cur.superseded_by_value = Some(CONVERTED.into());
        }
        self.current.remove(key);
        let cur = self.records[idx].clone();
        self.log_row(
            &LogAnchor::of(&cur),
            &LogRow {
                new_value: w.member,
                new_conf: w.confidence,
                t: w.now,
                decision: "convert_to_set",
                reason: "member_add_to_scalar",
                writer_id: w.writer_id.as_deref(),
                session_id: w.session_id.as_deref(),
            },
        );
        if let Some(Value::Object(row)) = self.supersession_log.last_mut() {
            if !cur.freshness_class.is_empty() && cur.freshness_class != "evergreen" {
                row.insert("dropped_freshness_class".into(), json!(cur.freshness_class));
            }
            for (field, label) in [
                ("dropped_authority", &cur.authority),
                ("dropped_distortion_tolerance", &cur.distortion_tolerance),
            ] {
                if let Some(v) = label.as_ref().filter(|v| !v.is_empty()) {
                    row.insert(field.into(), json!(v));
                }
            }
        }
        let mut rec = member_record(
            &cur.entity,
            &cur.attribute,
            &cur.value,
            &cur.polarity,
            cur.embedding.clone(),
            cur.confidence,
            cur.asserted_at,
            w.hlc,
        );
        rec.provenance = cur.provenance.clone();
        rec.support = cur.support.clone();
        rec.writer_id = cur.writer_id.clone();
        rec.session_id = cur.session_id.clone();
        self.push_member(rec);
    }

    /// The guarded add equals the current scalar (`cortex.py:875-882`,
    /// `:915-923`): confirm it (last_confirmed, provenance, support and
    /// confidence; tx_time, hlc and writer are not advanced).
    fn confirm_scalar_by_member(&mut self, idx: usize, w: &MemberWrite<'_>) -> MemberOutcome {
        let conf = self.confirmed_confidence(self.records[idx].confidence, w.confidence);
        let cur = &mut self.records[idx];
        cur.last_confirmed = w.now;
        union_prov(cur, &w.provenance);
        if let Some(sup) = w.sup() {
            mark_tier(cur, sup, w.dream);
            add_unique(&mut cur.support, sup);
        }
        cur.confidence = conf;
        outcome("confirmed", Some(idx))
    }

    /// `add_member` (`cortex.py:754-1032`).
    pub fn add_member(&mut self, w: &MemberWrite<'_>) -> MemberOutcome {
        let key = slot_key(w.entity, w.attribute);
        let nv = norm_value(w.member);
        if nv.is_empty() {
            // Rejected before touching any state.
            return outcome("member_invalid", None);
        }
        let sup = w.sup();
        if let Some(idx) = self.current.get(&key).copied() {
            if is_aggregate_value(&self.records[idx].value) {
                // A number-led total is protected, never converted.
                self.dirty_slots.insert(key.clone());
                if nv == norm_value(&self.records[idx].value) {
                    return self.confirm_scalar_by_member(idx, w);
                }
                let slot_embedding = self.records[idx].slot_embedding.clone();
                let parked = self.contend(
                    Some(idx),
                    &w.as_fact(slot_embedding),
                    "member_add_blocked_aggregate",
                );
                return outcome(parked.action, Some(parked.record));
            }
            let cur_origin = self.records[idx].origin();
            let assistant_blocked = sup == Some("assistant") && cur_origin != "assistant";
            let tier_blocked =
                sup.is_some() && self.protect_provenance && rank(sup) < rank(Some(cur_origin));
            let direct_blocked =
                w.dream && self.protect_provenance && direct_backed(Some(&self.records[idx]));
            if assistant_blocked || tier_blocked || direct_blocked {
                // The one-way conversion never runs against a value this
                // add may not change; it parks (or confirms an equal value).
                self.dirty_slots.insert(key.clone());
                if nv == norm_value(&self.records[idx].value) {
                    return self.confirm_scalar_by_member(idx, w);
                }
                let reason = if assistant_blocked {
                    "member_add_blocked_assistant"
                } else if tier_blocked {
                    "member_add_blocked_tier"
                } else {
                    "member_add_blocked_direct"
                };
                let slot_embedding = self.records[idx].slot_embedding.clone();
                let parked = self.contend(Some(idx), &w.as_fact(slot_embedding), reason);
                return outcome(parked.action, Some(parked.record));
            }
            if !crate::mutants::active("mcp-set-add-no-convert") {
                self.convert_to_set(idx, &key, w);
            }
        }
        // Dedup against the current members: the same normalised value, or
        // an embedding at least MEMBER_DEDUP_COSINE close.
        let members = self.members.get(&key).cloned().unwrap_or_default();
        for &i in &members {
            let m = &self.records[i];
            let same_norm = norm_value(&m.value) == nv;
            let near = !crate::mutants::active("mcp-set-add-no-cosine-dedup")
                && member_cosine(m.embedding.as_deref(), &w.embedding) >= MEMBER_DEDUP_COSINE;
            if same_norm || near {
                self.dirty_slots.insert(key.clone());
                let m = &mut self.records[i];
                m.last_confirmed = w.now;
                union_prov(m, &w.provenance);
                // The confirming tier accumulates (a dream confirm only on
                // an exact match); an assistant corroboration never does.
                if let Some(sup) = sup.filter(|s| *s != "assistant")
                    && (same_norm || !w.dream)
                {
                    mark_tier(m, sup, w.dream);
                    add_unique(&mut m.support, sup);
                }
                m.confidence = py_min(1.0, py_max(m.confidence, w.confidence));
                return outcome("member_confirmed", Some(i));
            }
        }
        if sup == Some("assistant")
            && members
                .iter()
                .any(|&i| self.records[i].origin() != "assistant")
        {
            // Joining a set another tier built changes it: park,
            // currentless (`cortex.py:995-1009`).
            self.dirty_slots.insert(key.clone());
            let parked = self.contend(None, &w.as_fact(None), "member_add_blocked_assistant");
            return outcome(parked.action, Some(parked.record));
        }
        if members.len() >= MAX_CURRENT_MEMBERS {
            // Logged against the rejected value itself; nothing is dirty.
            let anchor = LogAnchor {
                entity: w.entity.to_string(),
                attribute: w.attribute.to_string(),
                value: w.member.to_string(),
                confidence: w.confidence,
            };
            self.log_row(
                &anchor,
                &LogRow {
                    new_value: w.member,
                    new_conf: w.confidence,
                    t: w.now,
                    decision: "member_capped",
                    reason: "max_current_members",
                    writer_id: w.writer_id.as_deref(),
                    session_id: w.session_id.as_deref(),
                },
            );
            return outcome("member_capped", None);
        }
        self.dirty_slots.insert(key.clone());
        let mut rec = member_record(
            w.entity,
            w.attribute,
            w.member,
            "+",
            Some(w.embedding.clone()),
            w.confidence,
            w.now,
            w.hlc,
        );
        rec.writer_id = w.writer_id.clone();
        rec.session_id = w.session_id.clone();
        rec.provenance = clean_prov(&w.provenance);
        if let Some(raw) = w.support.as_ref().filter(|s| !s.is_empty()) {
            rec.support = vec![raw.clone()];
        }
        mark_new_record(&mut rec, sup, w.dream);
        let i = self.push_member(rec);
        self.settle_matching_contender(&key, i, &w.as_fact(None));
        outcome("member_added", Some(i))
    }

    /// `remove_member` (`cortex.py:1083-1135`): retract the first current
    /// member whose normalised value matches. `support` is the retracting
    /// tier as passed; an assistant retraction of another tier's member, a
    /// lower-tier retraction under `protect_provenance`, or a dream
    /// retraction of a direct-backed member is refused with nothing
    /// mutated (`member_remove_refused`, the target record).
    pub fn remove_member(
        &mut self,
        entity: &str,
        attribute: &str,
        member: &str,
        now: f64,
        support: Option<&str>,
        dream: bool,
    ) -> MemberOutcome {
        let key = slot_key(entity, attribute);
        let nv = norm_value(member);
        let pos = self.members.get(&key).and_then(|idxs| {
            idxs.iter()
                .position(|&i| norm_value(&self.records[i].value) == nv)
        });
        let Some(pos) = pos else {
            return outcome("member_not_found", None);
        };
        let target = &self.records[self.members[&key][pos]];
        let sup = norm_support(support);
        if (sup == Some("assistant") && target.origin() != "assistant")
            || (sup.is_some() && self.protect_provenance && rank(sup) < rank(Some(target.origin())))
            || (dream && self.protect_provenance && direct_backed(Some(target)))
        {
            return outcome("member_remove_refused", Some(self.members[&key][pos]));
        }
        if crate::mutants::active("mcp-set-remove-noop") {
            let i = self.members[&key][pos];
            return outcome("member_removed", Some(i));
        }
        self.dirty_slots.insert(key.clone());
        let i = self
            .members
            .get_mut(&key)
            .map(|idxs| idxs.remove(pos))
            .expect("member index");
        let rec = &mut self.records[i];
        rec.status = "removed".into();
        rec.superseded_at = Some(now);
        let anchor = LogAnchor::of(rec);
        self.log_row(
            &anchor,
            &LogRow {
                new_value: member,
                new_conf: anchor.confidence,
                t: now,
                decision: "member_removed",
                reason: "user_removed",
                writer_id: None,
                session_id: None,
            },
        );
        outcome("member_removed", Some(i))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stores::cortex::tests::{rec, store};

    fn add<'a>(e: &'a str, a: &'a str, m: &'a str, emb: Vec<f32>, t: f64) -> MemberWrite<'a> {
        MemberWrite {
            entity: e,
            attribute: a,
            member: m,
            embedding: emb.into(),
            confidence: 0.7,
            now: t,
            hlc: ((t * 1000.0) as i64, 0),
            writer_id: Some("w".into()),
            session_id: None,
            support: None,
            provenance: Vec::new(),
            dream: false,
        }
    }

    #[test]
    fn aggregate_values_match_python() {
        // `cortex._is_aggregate_value` on CPython 3.11.
        for (v, want) in [
            ("32", true),
            (" $1,500", true),
            ("\u{20ac}5", true),
            ("\u{a3}-3", true),
            ("+4", true),
            ("-2 degrees", true),
            ("$ 5", false),
            ("x32", false),
            ("\u{663}\u{662}", true),
            ("\u{b2}5", false),
            ("", false),
            ("  7", true),
            ("$$5", false),
            ("\u{ff11}\u{ff12}", true),
            ("\u{20ac}+1", true),
            ("\u{1c}9", true),
        ] {
            assert_eq!(is_aggregate_value(v), want, "{v:?}");
        }
    }

    #[test]
    fn cosine_is_float32_and_zero_without_an_embedding() {
        // Python: 0.9000000357627869 for these float32 vectors.
        let c = member_cosine(Some(&[0.9, 0.435_889_9]), &[1.0, 0.0]);
        assert_eq!(c, 0.900_000_035_762_786_9);
        assert_eq!(member_cosine(None, &[1.0, 0.0]), 0.0);
        assert_eq!(member_cosine(Some(&[0.0, 0.0]), &[0.0, 0.0]), 0.0);
    }

    #[test]
    fn a_hydrated_member_without_an_embedding_never_dedups_by_cosine() {
        let mut m = rec("box", "tags", "alpha");
        m.kind = "member".into();
        m.embedding = None;
        m.refresh_derived();
        let mut s = store(vec![m]);
        let r = s.add_member(&add("Box", "Tags", "beta", vec![1.0, 0.0], 5.0));
        assert_eq!(r.action, "member_added");
        assert_eq!(s.members("box", "tags").len(), 2);
    }

    #[test]
    fn invalid_and_not_found_touch_nothing() {
        let mut s = CortexStore::default();
        let r = s.add_member(&add("box", "tags", " \u{1c} ", vec![1.0], 1.0));
        assert_eq!(r, outcome("member_invalid", None));
        let r = s.remove_member("box", "tags", "x", 2.0, None, false);
        assert_eq!(r, outcome("member_not_found", None));
        assert!(s.records.is_empty() && s.dirty_slots.is_empty() && !s.meta_dirty);
    }

    #[test]
    fn a_conversion_keeps_the_old_value_and_drops_labels_on_the_log() {
        let mut old = rec("Box", "Owner", "alice");
        old.support = vec!["user".into(), "agent".into()];
        old.provenance = vec!["ep1".into()];
        old.authority = Some("directive".into());
        old.writer_id = Some("w0".into());
        old.asserted_at = 10.0;
        let mut s = store(vec![old]);
        let r = s.add_member(&add("box", "owner", "bob", vec![0.0, 1.0], 20.0));
        assert_eq!(r.action, "member_added");
        assert_eq!(s.records[0].status, "superseded");
        assert_eq!(s.records[0].superseded_by_value.as_deref(), Some(CONVERTED));
        assert!(s.lookup("box", "owner").is_none());
        let conv = &s.records[1];
        assert_eq!(
            (conv.kind.as_str(), conv.value.as_str()),
            ("member", "alice")
        );
        assert_eq!(
            (conv.asserted_at, conv.tx_time, conv.valid_time),
            (10.0, Some(10.0), Some(10.0))
        );
        assert_eq!(conv.hlc_phys, Some(20_000));
        assert_eq!(conv.support, ["user", "agent"]);
        assert_eq!(conv.writer_id.as_deref(), Some("w0"));
        assert!(conv.authority.is_none());
        let log = &s.supersession_log[0];
        assert_eq!(log["decision"], json!("convert_to_set"));
        assert_eq!(log["dropped_authority"], json!("directive"));
        assert!(log.get("dropped_freshness_class").is_none());
        assert_eq!(s.members("Box", "Owner").len(), 2);
    }
}
