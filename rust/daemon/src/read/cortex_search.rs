//! The cortex-first facts block of `GET /api/search`
//! (`web/routes.py:_search`): `MemoryService.cortex_search`
//! (`service.py:4499`) and `attach_served_facts` (`service.py:5974`,
//! `storage/postgres.py:2310`).
//!
//! Order of work, as Python runs it: dense search over current facts
//! (assistant penalty, floor, stable sort), optional BM25 fusion, set-slot
//! grouping, constraint pinning, stale demotion, evidence-supersession
//! annotation, then the slot-read telemetry write.

// The route wiring lands with the integration of this slice.

use std::collections::{BTreeSet, HashMap};

use anyhow::Result;
use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use crate::health::round4;
use crate::search::{Bm25Knobs, bm25_tokens};
use crate::stores::cortex::{CortexRecord, CortexStore, SlotKey, dot_f32, norm_key, unit_f32};

const DAY: f64 = 86400.0;
const STALE_WARNING: &str = "stale — re-verify before relying on this value";
const STALE_QUARANTINE_WRAPPER: &str = "(stale — re-verify; last known value below)";

/// The configuration the cortex block reads. YAML keys and defaults:
///
/// | field | key | default |
/// |---|---|---|
/// | `enabled` | `memory.cortex.enabled` | true |
/// | `search_first` | `memory.cortex.search_first` | true |
/// | `guard_min_score` | `memory.cortex.guard_min_score` | 0.2 |
/// | `pin_constraints` | `memory.cortex.pin_constraints` | true |
/// | `read_tracking` | `memory.cortex.read_tracking` | true |
/// | `stale_policy` | `memory.search.stale_policy` | "annotate" |
/// | `bm25` | `Some` iff `memory.bm25.cortex_enabled` (false); then `memory.bm25.{k1,b,weight,top_n,min_score}` | None |
/// | `traces_enabled` | `memory.traces.enabled` | true |
/// | `retrieval_log_enabled` | `memory.retrieval_log.enabled` | true |
///
/// `enabled`, `search_first` and `guard_min_score` are read by the route
/// (`web/routes.py:368-371`), which calls [`cortex_search`] only when both
/// switches are on and the query is non-blank, with
/// `min_score = guard_min_score`.
#[derive(Clone)]
pub struct CortexKnobs {
    pub enabled: bool,
    pub search_first: bool,
    pub guard_min_score: f64,
    pub pin_constraints: bool,
    pub read_tracking: bool,
    pub stale_policy: String,
    pub bm25: Option<Bm25Knobs>,
    pub traces_enabled: bool,
    pub retrieval_log_enabled: bool,
}

impl CortexKnobs {
    /// The knobs from the loaded config (`CortexConfig`, `memory.search.stale_policy`,
    /// `memory.bm25.cortex_enabled`, `memory.traces.enabled`, `memory.retrieval_log.enabled`).
    pub fn from_config(c: &crate::config::Config) -> CortexKnobs {
        let m = &c.memory;
        let b = &m.bm25;
        CortexKnobs {
            enabled: m.cortex.enabled,
            search_first: m.cortex.search_first,
            guard_min_score: m.cortex.guard_min_score,
            pin_constraints: m.cortex.pin_constraints,
            read_tracking: m.cortex.read_tracking,
            stale_policy: m.search.stale_policy.clone(),
            bm25: b.cortex_enabled.then_some(Bm25Knobs {
                k1: b.k1,
                b: b.b,
                weight: b.weight,
                top_n: b.top_n.max(0) as usize,
                min_norm: b.min_score,
            }),
            traces_enabled: m.traces_enabled,
            retrieval_log_enabled: m.retrieval_log_enabled,
        }
    }
}

impl Default for CortexKnobs {
    fn default() -> Self {
        Self {
            enabled: true,
            search_first: true,
            guard_min_score: 0.2,
            pin_constraints: true,
            read_tracking: true,
            stale_policy: "annotate".into(),
            bm25: None,
            traces_enabled: true,
            retrieval_log_enabled: true,
        }
    }
}

// ---- freshness (memory/freshness.py) and age (memory/context_builder.py) ----

/// `freshness.normalize_class`: unknown classes read as volatile.
fn normalize_class(c: &str) -> &'static str {
    let c = crate::stores::cortex::py_casefold(c.trim_matches(crate::stores::cortex::py_isspace));
    match c.as_str() {
        "evergreen" => "evergreen",
        "slow" => "slow",
        _ => "volatile",
    }
}

fn ttl(c: &str) -> Option<f64> {
    match normalize_class(c) {
        "evergreen" => None,
        "slow" => Some(270.0 * DAY),
        _ => Some(21.0 * DAY),
    }
}

/// `freshness.decay_factor`.
fn decay_factor(c: &str, age: f64) -> f64 {
    let class = normalize_class(c);
    let Some(ttl) = ttl(class) else {
        return 1.0;
    };
    let age = age.max(0.0);
    let floor = if class == "slow" { 0.5 } else { 0.4 };
    if age >= ttl {
        return floor;
    }
    1.0 - (1.0 - floor) * (age / ttl)
}

/// `last_confirmed or asserted_at`: the freshness anchor.
fn anchor(r: &CortexRecord) -> f64 {
    if r.last_confirmed != 0.0 {
        r.last_confirmed
    } else {
        r.asserted_at
    }
}

/// `CortexRecord.effective_confidence` (`freshness.effective_confidence`).
fn effective_confidence(r: &CortexRecord, now: f64) -> f64 {
    let age = now - anchor(r);
    (r.confidence as f64 * decay_factor(&r.freshness_class, age)).clamp(0.0, 1.0)
}

/// `CortexRecord.is_stale` (`freshness.is_stale`): past 2xTTL.
fn is_stale(r: &CortexRecord, now: f64) -> bool {
    match ttl(&r.freshness_class) {
        None => false,
        Some(ttl) => now - anchor(r) > 2.0 * ttl,
    }
}

/// Python's float `//` (`float_floor_div`), for the age buckets.
fn py_floordiv(vx: f64, wx: f64) -> f64 {
    let mut m = vx % wx;
    let mut div = (vx - m) / wx;
    if m != 0.0 && ((wx < 0.0) != (m < 0.0)) {
        m += wx;
        div -= 1.0;
    }
    let _ = m;
    if div != 0.0 {
        let f = div.floor();
        if div - f > 0.5 { f + 1.0 } else { f }
    } else {
        0.0_f64.copysign(vx / wx)
    }
}

/// `context_builder._relative_time`.
pub fn relative_time(timestamp: f64, now: f64) -> String {
    if timestamp == 0.0 {
        return "unknown time".into();
    }
    let age = (now - timestamp).max(0.0);
    let plural = |n: i64| if n != 1 { "s" } else { "" };
    if age < 60.0 {
        return "just now".into();
    }
    if age < 3600.0 {
        let n = py_floordiv(age, 60.0) as i64;
        return format!("{n} minute{} ago", plural(n));
    }
    if age < 86400.0 {
        let n = py_floordiv(age, 3600.0) as i64;
        return format!("{n} hour{} ago", plural(n));
    }
    let n = py_floordiv(age, 86400.0) as i64;
    format!("{n} day{} ago", plural(n))
}

// ---- record serialisation (service.py:272-358) ----

/// `_apply_stale_policy` (`service.py:272`).
fn apply_stale_policy(d: &mut Map<String, Value>, policy: &str) {
    if d.get("stale") != Some(&Value::Bool(true)) || policy == "annotate" {
        return;
    }
    if policy == "demote" {
        d.insert("warning".into(), json!(STALE_WARNING));
    } else if policy == "quarantine" {
        let v = d.get("value").cloned().unwrap_or(Value::Null);
        d.insert("last_known_value".into(), v);
        d.insert("value".into(), json!(STALE_QUARANTINE_WRAPPER));
    }
}

fn sorted_unique(v: &[String]) -> Vec<String> {
    v.iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn opt_str(s: &Option<String>) -> Value {
    s.as_ref().map_or(Value::Null, |s| json!(s))
}

fn opt_f64(x: Option<f64>) -> Value {
    x.map_or(Value::Null, |x| json!(x))
}

/// `_cortex_record_to_dict(rec, relative_age=True, stale_policy)`
/// (`service.py:297`).
pub fn record_to_dict(r: &CortexRecord, stale_policy: &str, now: f64) -> Map<String, Value> {
    let mut d = Map::new();
    d.insert("entity".into(), json!(r.entity));
    d.insert("attribute".into(), json!(r.attribute));
    d.insert("value".into(), json!(r.value));
    d.insert("polarity".into(), json!(r.polarity));
    d.insert("status".into(), json!(r.status));
    d.insert("kind".into(), json!(r.kind));
    d.insert("confidence".into(), json!(round4(r.confidence as f64)));
    d.insert("origin".into(), json!(r.origin()));
    d.insert("support".into(), json!(sorted_unique(&r.support)));
    d.insert("provenance".into(), json!(sorted_unique(&r.provenance)));
    d.insert("asserted_at".into(), json!(r.asserted_at));
    d.insert("last_confirmed".into(), json!(r.last_confirmed));
    d.insert("freshness_class".into(), json!(r.freshness_class));
    d.insert(
        "effective_confidence".into(),
        json!(round4(effective_confidence(r, now))),
    );
    d.insert("stale".into(), json!(is_stale(r, now)));
    d.insert("supersedes_value".into(), opt_str(&r.supersedes_value));
    d.insert(
        "superseded_by_value".into(),
        opt_str(&r.superseded_by_value),
    );
    d.insert("superseded_at".into(), opt_f64(r.superseded_at));
    d.insert("tx_time".into(), opt_f64(r.tx_time));
    d.insert("valid_time".into(), opt_f64(r.valid_time));
    d.insert("writer_id".into(), opt_str(&r.writer_id));
    d.insert("session_id".into(), opt_str(&r.session_id));
    for (k, v) in [
        ("stance", &r.stance),
        ("authority", &r.authority),
        ("distortion_tolerance", &r.distortion_tolerance),
    ] {
        if let Some(s) = v.as_ref().filter(|s| !s.is_empty()) {
            d.insert(k.into(), json!(s));
        }
    }
    let age_anchor = r.tx_time.filter(|&t| t != 0.0).unwrap_or(r.asserted_at);
    d.insert("age".into(), json!(relative_time(age_anchor, now)));
    apply_stale_policy(&mut d, stale_policy);
    d
}

/// `_contender_fields` (`service.py:347`).
fn contender_fields(d: &mut Map<String, Value>, conts: &[&CortexRecord]) {
    match conts.first() {
        None => {
            d.insert("contested".into(), json!(false));
        }
        Some(c) => {
            d.insert("contested".into(), json!(true));
            d.insert("contender_value".into(), json!(c.value));
            d.insert("contender_origin".into(), json!(c.origin()));
        }
    }
}

// ---- storage reads and writes (storage/postgres.py) ----

/// `traces_for_slot` (`storage/postgres.py:3951`).
async fn traces_for_slot(db: &Client, key: &SlotKey) -> Result<Vec<i64>> {
    Ok(db
        .query(
            "SELECT entry_id FROM memory_traces \
             WHERE entity_norm = $1 AND attribute_norm = $2 ORDER BY entry_id",
            &[&key.0, &key.1],
        )
        .await?
        .iter()
        .map(|r| r.get(0))
        .collect())
}

struct Invalidation {
    source_entry_id: i64,
    invalidated_at: f64,
    cause: String,
}

/// `trace_invalidations_for_slots` (`storage/postgres.py:3987`).
async fn trace_invalidations_for_slots(
    db: &Client,
    slots: &[SlotKey],
) -> Result<HashMap<SlotKey, Vec<Invalidation>>> {
    let mut out: HashMap<SlotKey, Vec<Invalidation>> = HashMap::new();
    if slots.is_empty() {
        return Ok(out);
    }
    let es: Vec<&str> = slots.iter().map(|k| k.0.as_str()).collect();
    let attrs: Vec<&str> = slots.iter().map(|k| k.1.as_str()).collect();
    let rows = db
        .query(
            "SELECT i.entity_norm, i.attribute_norm, i.source_entry_id, \
                    i.invalidated_at, i.cause \
             FROM memory_trace_invalidations i \
             JOIN unnest($1::text[], $2::text[]) AS k(e, a) \
               ON i.entity_norm = k.e AND i.attribute_norm = k.a \
             ORDER BY i.entity_norm, i.attribute_norm, i.source_entry_id",
            &[&es, &attrs],
        )
        .await?;
    for r in rows {
        out.entry((r.get(0), r.get(1)))
            .or_default()
            .push(Invalidation {
                source_entry_id: r.get(2),
                invalidated_at: r.get(3),
                cause: r.get(4),
            });
    }
    Ok(out)
}

/// `bump_slot_reads` (`storage/postgres.py:4253`): every slot upserted
/// together, stamped `now`.
async fn bump_slot_reads(db: &Client, slots: &[SlotKey], now: f64) -> Result<()> {
    if slots.is_empty() {
        return Ok(());
    }
    // One statement (the slots are distinct), so the write is atomic without
    // an open transaction on the shared session that a cancel could strand.
    let ents: Vec<&str> = slots.iter().map(|(e, _)| e.as_str()).collect();
    let attrs: Vec<&str> = slots.iter().map(|(_, a)| a.as_str()).collect();
    db.execute(
        "INSERT INTO slot_reads (entity_norm, attribute_norm, read_count, last_read_at)          SELECT e, a, 1, $3 FROM unnest($1::text[], $2::text[]) AS u(e, a)          ON CONFLICT (entity_norm, attribute_norm) DO UPDATE SET          read_count = slot_reads.read_count + 1,          last_read_at = EXCLUDED.last_read_at",
        &[&ents, &attrs, &now],
    )
    .await?;
    Ok(())
}

// ---- the search ----

/// `_cortex_bm25_fuse` (`service.py:4724`): a BM25 pool over every current
/// record's composed text `"{entity} — {attribute}: {value}"`, fused per
/// record (not per slot) into the dense hits.
fn bm25_fuse(
    store: &CortexStore,
    query: &str,
    hits: Vec<(usize, f64)>,
    k: Bm25Knobs,
    top_k: usize,
) -> Vec<(usize, f64)> {
    let docs: Vec<(usize, Vec<String>)> = store
        .records
        .iter()
        .enumerate()
        .filter(|(_, r)| r.status == "current")
        .map(|(i, r)| {
            (
                i,
                bm25_tokens(&format!("{} — {}: {}", r.entity, r.attribute, r.value)),
            )
        })
        .collect();
    if docs.is_empty() {
        return hits;
    }
    let n = docs.len() as f64;
    let avg = docs.iter().map(|(_, t)| t.len()).sum::<usize>() as f64 / n;
    let mut df: HashMap<&str, usize> = HashMap::new();
    for (_, toks) in &docs {
        let uniq: BTreeSet<&str> = toks.iter().map(String::as_str).collect();
        for t in uniq {
            *df.entry(t).or_default() += 1;
        }
    }
    let q = bm25_tokens(query);
    let mut scored: Vec<(usize, f64)> = Vec::new();
    if !q.is_empty() {
        for (idx, toks) in &docs {
            if toks.is_empty() {
                continue;
            }
            let mut tf: HashMap<&str, usize> = HashMap::new();
            for t in toks {
                *tf.entry(t.as_str()).or_default() += 1;
            }
            let ratio = if avg != 0.0 {
                toks.len() as f64 / avg
            } else {
                0.0
            };
            let norm = 1.0 - k.b + k.b * ratio;
            let mut s = 0.0;
            for qt in &q {
                let f = *tf.get(qt.as_str()).unwrap_or(&0) as f64;
                if f == 0.0 {
                    continue;
                }
                let d = *df.get(qt.as_str()).unwrap_or(&0) as f64;
                if d == 0.0 {
                    continue;
                }
                let idf = (1.0 + (n - d + 0.5) / (d + 0.5)).ln();
                if idf <= 0.0 {
                    continue;
                }
                s += idf * (f * (k.k1 + 1.0)) / (f + k.k1 * norm);
            }
            if s > 0.0 {
                scored.push((*idx, s));
            }
        }
    }
    scored.sort_by(|a, b| b.1.total_cmp(&a.1));
    scored.truncate(k.top_n);
    // normalize_scores: min-max; a single hit or a flat set is 1.0.
    let mx = scored.iter().map(|s| s.1).fold(f64::MIN, f64::max);
    let mn = scored.iter().map(|s| s.1).fold(f64::MAX, f64::min);
    let lex: Vec<(usize, f64)> = scored
        .iter()
        .map(|&(i, s)| {
            let v = if scored.len() == 1 || mx - mn <= 0.0 {
                1.0
            } else {
                (s - mn) / (mx - mn)
            };
            (i, v)
        })
        .filter(|&(_, v)| v >= k.min_norm)
        .collect();
    let lex_of: HashMap<usize, f64> = lex.iter().copied().collect();
    let mut fused: Vec<(usize, f64)> = Vec::with_capacity(hits.len() + lex.len());
    let mut seen = std::collections::HashSet::new();
    for (i, s) in hits {
        let s = match lex_of.get(&i) {
            Some(b) => s + k.weight * b,
            None => s,
        };
        fused.push((i, s));
        seen.insert(i);
    }
    for (i, s) in lex {
        if !seen.contains(&i) {
            fused.push((i, k.weight * s));
        }
    }
    fused.sort_by(|a, b| b.1.total_cmp(&a.1));
    fused.truncate(top_k);
    fused
}

/// `compose_set_value` (`memory/cortex.py:165`): ranked members first,
/// score-descending (stable), then the unranked ones in insertion order;
/// `"m1; m2 (N members)"` and the best score.
pub fn compose_set_value(member_values: &[&str], ranked: &[(usize, f64)]) -> (String, Option<f64>) {
    let mut sorted = ranked.to_vec();
    sorted.sort_by(|a, b| b.1.total_cmp(&a.1));
    let ranked_idx: Vec<usize> = sorted.iter().map(|p| p.0).collect();
    let mut ordered: Vec<&str> = ranked_idx.iter().map(|&i| member_values[i]).collect();
    ordered.extend(
        member_values
            .iter()
            .enumerate()
            .filter(|(i, _)| !ranked_idx.contains(i))
            .map(|(_, v)| *v),
    );
    let value = format!("{} ({} members)", ordered.join("; "), member_values.len());
    (value, sorted.first().map(|p| p.1))
}

/// `_scalar_fact_entry` (`service.py:4637`).
async fn scalar_fact_entry(
    store: &CortexStore,
    db: &Client,
    r: &CortexRecord,
    score: f64,
    knobs: &CortexKnobs,
    now: f64,
) -> Result<Map<String, Value>> {
    let mut d = record_to_dict(r, &knobs.stale_policy, now);
    d.insert("score".into(), json!(round4(score)));
    contender_fields(&mut d, &store.contenders_for(&r.entity, &r.attribute));
    d.insert(
        "source_entries".into(),
        json!(traces_for_slot(db, r.key()).await?),
    );
    Ok(d)
}

fn max_opt(it: impl Iterator<Item = f64>) -> Option<f64> {
    it.fold(None, |m, x| {
        Some(m.map_or(x, |m: f64| if x > m { x } else { m }))
    })
}

/// The set-slot entry `cortex_search` builds for a grouped slot
/// (`service.py:4570-4623`).
async fn set_entry(
    store: &CortexStore,
    db: &Client,
    first: &CortexRecord,
    ranked: &[(usize, f64)],
    knobs: &CortexKnobs,
    now: f64,
) -> Result<Map<String, Value>> {
    let key = first.key().clone();
    let idxs: Vec<usize> = store.members.get(&key).cloned().unwrap_or_default();
    let all: Vec<&CortexRecord> = idxs.iter().map(|&i| &store.records[i]).collect();
    let pairs: Vec<(usize, f64)> = ranked
        .iter()
        .filter_map(|&(ri, s)| idxs.iter().position(|&m| m == ri).map(|p| (p, s)))
        .collect();
    let values: Vec<&str> = all.iter().map(|m| m.value.as_str()).collect();
    let (value, score) = compose_set_value(&values, &pairs);
    let anchor_t = max_opt(
        all.iter()
            .map(|m| m.tx_time.filter(|&t| t != 0.0).unwrap_or(m.asserted_at)),
    );
    let last_confirmed = max_opt(all.iter().map(|m| anchor(m)));
    let mut d = Map::new();
    d.insert("kind".into(), json!("set"));
    d.insert("entity".into(), json!(first.entity));
    d.insert("attribute".into(), json!(first.attribute));
    d.insert("value".into(), json!(value));
    d.insert(
        "source_entries".into(),
        json!(traces_for_slot(db, &key).await?),
    );
    d.insert(
        "members".into(),
        Value::Array(
            all.iter()
                .map(|m| Value::Object(record_to_dict(m, &knobs.stale_policy, now)))
                .collect(),
        ),
    );
    d.insert("score".into(), json!(score.map_or(0.0, round4)));
    d.insert("contested".into(), json!(false));
    d.insert("last_confirmed".into(), opt_f64(last_confirmed));
    d.insert("asserted_at".into(), opt_f64(anchor_t));
    d.insert(
        "age".into(),
        match anchor_t.filter(|&t| t != 0.0) {
            Some(t) => json!(relative_time(t, now)),
            None => Value::Null,
        },
    );
    Ok(d)
}

/// `_scope_word_char` (`service.py:613`): `_`, alphanumerics, and
/// combining marks (Unicode category M). Python's `isalnum` and category M
/// are approximated by `char::is_alphanumeric` and the main combining
/// blocks for non-ASCII text (declared divergence).
fn scope_word_char(c: char) -> bool {
    if c.is_ascii() {
        return c == '_' || c.is_ascii_alphanumeric();
    }
    let circled = matches!(c as u32, 0x24B6..=0x24E9 | 0x1F130..=0x1F149 | 0x1F150..=0x1F169 | 0x1F170..=0x1F189);
    let mark = matches!(c as u32,
        0x0300..=0x036F | 0x0483..=0x0489 | 0x0591..=0x05BD | 0x0610..=0x061A
        | 0x064B..=0x065F | 0x0900..=0x0903 | 0x093A..=0x094F | 0x0951..=0x0957
        | 0x0962..=0x0963 | 0x1AB0..=0x1AFF | 0x1DC0..=0x1DFF | 0x20D0..=0x20FF
        | 0x302A..=0x302F | 0x3099..=0x309A | 0xFE00..=0xFE0F | 0xFE20..=0xFE2F);
    (c.is_alphanumeric() && !circled) || mark
}

fn is_apos(c: char) -> bool {
    c == '\'' || c == '\u{2019}'
}

/// `_scope_bounded` (`service.py:622`), over byte offsets of `q`.
fn scope_bounded(q: &str, i: usize, j: usize) -> bool {
    let mut back = q[..i].chars().rev();
    if let Some(before) = back.next() {
        if scope_word_char(before) {
            return false;
        }
        if is_apos(before) && back.next().is_some_and(scope_word_char) {
            return false;
        }
    }
    let mut fwd = q[j..].chars();
    if let Some(after) = fwd.next() {
        if scope_word_char(after) {
            return false;
        }
        if is_apos(after) {
            let rest: Vec<char> = fwd.take(2).collect();
            if rest.first().is_some_and(|&c| scope_word_char(c)) {
                let possessive = rest[0] == 's' && rest.get(1).is_none_or(|&c| !scope_word_char(c));
                if !possessive {
                    return false;
                }
            }
        }
    }
    true
}

/// `_entity_in_query` (`service.py:645`): the normalised entity occurs in
/// the normalised query as a word-bounded run.
pub fn entity_in_query(entity: &str, query: &str) -> bool {
    let e = norm_key(entity);
    if e.is_empty() {
        return false;
    }
    let q = norm_key(query);
    let mut from = 0;
    while let Some(off) = q[from..].find(&e) {
        let i = from + off;
        if scope_bounded(&q, i, i + e.len()) {
            return true;
        }
        // Advance one character, as `str.find(e, i + 1)` does.
        from = i + q[i..].chars().next().map_or(1, char::len_utf8);
    }
    false
}

enum Slotted {
    Scalar(usize, f64),
    Set(SlotKey),
}

/// `_pin_constraint_facts` (`service.py:4652`).
#[allow(clippy::too_many_arguments)]
async fn pin_constraint_facts(
    store: &CortexStore,
    db: &Client,
    qvec: &[f32],
    query: &str,
    entries: Vec<Map<String, Value>>,
    top_k: usize,
    min_score: f64,
    knobs: &CortexKnobs,
    now: f64,
) -> Result<Vec<Map<String, Value>>> {
    let pins: Vec<&CortexRecord> = store
        .current_records()
        .filter(|r| {
            r.kind == "scalar"
                && r.distortion_tolerance.as_deref() == Some("constraint")
                && entity_in_query(&r.entity, query)
        })
        .collect();
    if pins.is_empty() {
        return Ok(entries);
    }
    let k = top_k.max(1);
    let cap = (k / 2).max(1);
    let is_set = |e: &Map<String, Value>| e.get("kind").and_then(Value::as_str) == Some("set");
    let ea = |e: &Map<String, Value>| {
        (
            e.get("entity")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            e.get("attribute")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        )
    };
    // Raw (display) entity/attribute -> position in `entries`, scalars only;
    // a later duplicate wins, as the dict comprehension does.
    let mut keyed: HashMap<(String, String), usize> = HashMap::new();
    for (p, e) in entries.iter().enumerate() {
        if !is_set(e) {
            keyed.insert(ea(e), p);
        }
    }
    let qn = unit_f32(qvec);
    let mut scored: Vec<(f64, usize, &CortexRecord)> = Vec::new();
    for (i, r) in pins.iter().enumerate() {
        let raw = (r.entity.clone(), r.attribute.clone());
        let score = if let Some(&p) = keyed.get(&raw) {
            entries[p]
                .get("score")
                .and_then(Value::as_f64)
                .unwrap_or(0.0)
        } else if let Some(unit) = r.unit.as_deref() {
            dot_f32(&qn, unit) as f64
        } else {
            0.0
        };
        if score < min_score {
            continue;
        }
        scored.push((score, i, r));
    }
    scored.sort_by(|a, b| b.0.total_cmp(&a.0).then(a.1.cmp(&b.1)));
    scored.truncate(cap);
    if scored.is_empty() {
        return Ok(entries);
    }
    let mut entries: Vec<Option<Map<String, Value>>> = entries.into_iter().map(Some).collect();
    let mut pinned = Vec::with_capacity(scored.len());
    let mut pinned_keys = std::collections::HashSet::new();
    for &(score, _, r) in &scored {
        let raw = (r.entity.clone(), r.attribute.clone());
        let mut d = match keyed.get(&raw) {
            Some(&p) if entries[p].is_some() => entries[p].take().unwrap_or_default(),
            _ => scalar_fact_entry(store, db, r, score, knobs, now).await?,
        };
        d.insert("pinned".into(), json!(true));
        pinned.push(d);
        pinned_keys.insert(raw);
    }
    let rest = entries
        .into_iter()
        .flatten()
        .filter(|e| is_set(e) || !pinned_keys.contains(&ea(e)));
    Ok(pinned.into_iter().chain(rest).take(k).collect())
}

/// `_annotate_evidence_supersession` -> `_annotate_trace_invalidations`
/// (`service.py:4282, 4312`): flag a served fact whose source memory was
/// corrected after the fact was last confirmed.
async fn annotate_evidence_supersession(
    db: &Client,
    rows: &mut [Map<String, Value>],
    knobs: &CortexKnobs,
) -> Result<()> {
    if !knobs.traces_enabled || rows.is_empty() {
        return Ok(());
    }
    let mut targets: Vec<(usize, SlotKey, Option<f64>)> = Vec::new();
    for (p, row) in rows.iter().enumerate() {
        let (Some(e), Some(a)) = (
            row.get("entity").and_then(Value::as_str),
            row.get("attribute").and_then(Value::as_str),
        ) else {
            continue;
        };
        let truthy = |k: &str| row.get(k).and_then(Value::as_f64).filter(|&x| x != 0.0);
        let seen = truthy("last_confirmed").or_else(|| truthy("asserted_at"));
        targets.push((p, (norm_key(e), norm_key(a)), seen));
    }
    if targets.is_empty() {
        return Ok(());
    }
    let slots: Vec<SlotKey> = targets
        .iter()
        .map(|t| t.1.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let inv = trace_invalidations_for_slots(db, &slots).await?;
    for (p, slot, seen) in targets {
        let Some(seen) = seen else { continue };
        let ids: BTreeSet<i64> = inv
            .get(&slot)
            .map(|v| {
                v.iter()
                    .filter(|ev| ev.cause == "source_superseded" && ev.invalidated_at > seen)
                    .map(|ev| ev.source_entry_id)
                    .collect()
            })
            .unwrap_or_default();
        if ids.is_empty() {
            continue;
        }
        let n = ids.len();
        let row = &mut rows[p];
        row.insert("re_verify".into(), json!(true));
        row.insert(
            "re_verify_reason".into(),
            json!(format!(
                "derived from {n} source {} corrected since this fact was last confirmed",
                if n == 1 { "memory" } else { "memories" }
            )),
        );
    }
    Ok(())
}

/// `MemoryService.cortex_search` (`service.py:4499`) with `bm25=None` (the
/// route passes none, so `knobs.bm25` alone decides the lexical pool).
/// Returns the `entries` list in Python's order and shape, after writing the
/// slot-read telemetry for the served slots. A telemetry failure is logged
/// and swallowed, as `_track_slot_reads` does.
#[allow(clippy::too_many_arguments)]
pub async fn cortex_search(
    store: &CortexStore,
    db: &Client,
    qvec: &[f32],
    query: &str,
    top_k: usize,
    min_score: f64,
    knobs: &CortexKnobs,
    now: f64,
) -> Result<Vec<Value>> {
    let mut hits = store.search(qvec, top_k, min_score);
    if let Some(b) = knobs.bm25
        && !query.is_empty()
    {
        hits = bm25_fuse(store, query, hits, b, top_k);
    }
    // Group set members by slot at the first (best-scoring) member's place.
    let mut groups: HashMap<SlotKey, Vec<(usize, f64)>> = HashMap::new();
    let mut order: Vec<Slotted> = Vec::new();
    for &(i, s) in &hits {
        let r = &store.records[i];
        if r.kind == "member" {
            let g = groups.entry(r.key().clone()).or_default();
            if g.is_empty() {
                order.push(Slotted::Set(r.key().clone()));
            }
            g.push((i, s));
        } else {
            order.push(Slotted::Scalar(i, s));
        }
    }
    let mut entries: Vec<Map<String, Value>> = Vec::with_capacity(order.len());
    for item in order {
        match item {
            Slotted::Scalar(i, s) => {
                entries.push(scalar_fact_entry(store, db, &store.records[i], s, knobs, now).await?)
            }
            Slotted::Set(key) => {
                let ranked = &groups[&key];
                let first = &store.records[ranked[0].0];
                entries.push(set_entry(store, db, first, ranked, knobs, now).await?);
            }
        }
    }
    if knobs.pin_constraints {
        entries = pin_constraint_facts(
            store, db, qvec, query, entries, top_k, min_score, knobs, now,
        )
        .await?;
    }
    if knobs.stale_policy == "demote" {
        // `_demote_stale`: stable, stale last.
        entries.sort_by_key(|d| d.get("stale") == Some(&Value::Bool(true)));
    }
    annotate_evidence_supersession(db, &mut entries, knobs).await?;
    let slots: Vec<SlotKey> = entries
        .iter()
        .map(|e| {
            (
                norm_key(e.get("entity").and_then(Value::as_str).unwrap_or("")),
                norm_key(e.get("attribute").and_then(Value::as_str).unwrap_or("")),
            )
        })
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if knobs.read_tracking
        && !slots.is_empty()
        && !crate::mutants::active("w2d-cortex-skip-slot-reads")
        && let Err(e) = bump_slot_reads(db, &slots, now).await
    {
        eprintln!("slot-read telemetry failed: {e:#}");
    }
    Ok(entries.into_iter().map(Value::Object).collect())
}

/// `MemoryService.attach_served_facts` (`service.py:5974`) ->
/// `PostgresStorage.attach_served_facts` (`storage/postgres.py:2310`): the
/// served-facts payload on the search's retrieval event. The caller gates
/// on `memory.retrieval_log.enabled` and a present event id (`routes.py`
/// attaches only a non-empty block). Never fails the search: a write error
/// or a row count of zero is logged and `Ok` is returned.
pub async fn attach_served_facts(db: &Client, event_id: i64, facts: &[Value]) -> Result<()> {
    if facts.is_empty() {
        return Ok(());
    }
    let s = |f: &Value, k: &str| f.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    let flag = |f: &Value, k: &str| f.get(k).and_then(Value::as_bool).unwrap_or(false);
    let payload = Value::Array(
        facts
            .iter()
            .enumerate()
            .map(|(rank, f)| {
                let mut m = Map::new();
                m.insert("entity_norm".into(), json!(norm_key(&s(f, "entity"))));
                m.insert("attribute_norm".into(), json!(norm_key(&s(f, "attribute"))));
                m.insert("rank".into(), json!(rank));
                m.insert(
                    "score".into(),
                    f.get("score").cloned().unwrap_or(Value::Null),
                );
                m.insert(
                    "kind".into(),
                    f.get("kind").cloned().unwrap_or_else(|| json!("scalar")),
                );
                m.insert("contested".into(), json!(flag(f, "contested")));
                m.insert("pinned".into(), json!(flag(f, "pinned")));
                Value::Object(m)
            })
            .collect(),
    );
    match db
        .execute(
            "UPDATE retrieval_events SET served_facts = $1 WHERE id = $2",
            &[&payload, &event_id],
        )
        .await
    {
        Ok(0) => eprintln!("served-facts attach matched no event row (id={event_id})"),
        Ok(_) => {}
        Err(e) => eprintln!("served-facts attach failed: {e:#}"),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stores::cortex::tests::{axis, rec, store};

    #[test]
    fn relative_time_buckets_match_python() {
        let now = 1_000_000.0;
        assert_eq!(relative_time(0.0, now), "unknown time");
        assert_eq!(relative_time(now - 59.0, now), "just now");
        assert_eq!(relative_time(now + 500.0, now), "just now");
        assert_eq!(relative_time(now - 60.0, now), "1 minute ago");
        assert_eq!(relative_time(now - 150.0, now), "2 minutes ago");
        assert_eq!(relative_time(now - 3600.0, now), "1 hour ago");
        assert_eq!(relative_time(now - 86399.0, now), "23 hours ago");
        assert_eq!(relative_time(now - 86400.0 * 3.5, now), "3 days ago");
        assert_eq!(relative_time(now - 86400.0, now), "1 day ago");
    }

    #[test]
    fn freshness_matches_python_curve() {
        let mut r = rec("e", "a", "v");
        r.confidence = 0.8;
        r.freshness_class = "volatile".into();
        r.last_confirmed = 0.0;
        r.asserted_at = 0.0;
        // Half the volatile TTL: 1 - 0.6 * 0.5 = 0.7.
        assert!((effective_confidence(&r, 10.5 * DAY) - 0.8_f32 as f64 * 0.7).abs() < 1e-12);
        assert!(!is_stale(&r, 42.0 * DAY));
        assert!(is_stale(&r, 42.0 * DAY + 1.0));
        r.freshness_class = "Bogus".into(); // normalize_class: unknown is volatile
        assert!(is_stale(&r, 43.0 * DAY));
        r.freshness_class = "evergreen".into();
        assert!(!is_stale(&r, 1e12));
        assert!((effective_confidence(&r, 1e12) - 0.8_f32 as f64).abs() < 1e-12);
    }

    #[test]
    fn compose_set_value_matches_python() {
        let (v, s) = compose_set_value(&["a", "b", "c"], &[(2, 0.4), (0, 0.5)]);
        assert_eq!(v, "a; c; b (3 members)");
        assert_eq!(s, Some(0.5));
    }

    #[test]
    fn entity_scope_is_word_bounded() {
        assert!(entity_in_query(
            "payments-db",
            "where is payments db hosted"
        ));
        assert!(!entity_in_query("db", "the payments-database"));
        assert!(entity_in_query("bench server", "start the bench server?"));
        assert!(entity_in_query("bench server", "the bench server's config"));
        assert!(!entity_in_query("Don", "I don't know"));
        assert!(!entity_in_query("bench", "benches"));
        assert!(!entity_in_query("", "anything"));
        assert!(entity_in_query("Ünïcode", "about ünïcode now"));
        // A combining mark continues the word it follows.
        assert!(!entity_in_query("cafe", "cafe\u{301} society"));
    }

    #[test]
    fn stale_policy_shapes() {
        let mut r = rec("e", "a", "v");
        r.freshness_class = "volatile".into();
        let now = 1000.0 + 50.0 * DAY;
        let d = record_to_dict(&r, "annotate", now);
        assert_eq!(d["stale"], json!(true));
        assert!(!d.contains_key("warning"));
        let d = record_to_dict(&r, "demote", now);
        assert_eq!(d["warning"], json!(STALE_WARNING));
        let d = record_to_dict(&r, "quarantine", now);
        assert_eq!(d["value"], json!(STALE_QUARANTINE_WRAPPER));
        assert_eq!(d["last_known_value"], json!("v"));
    }

    /// The Rust half of `harness/w2d_cortex_check.py`: hydrate the cortex
    /// from `W2D_CORTEX_DSN` (a disposable bank), run every case in
    /// `W2D_CORTEX_IN` with the query vector Python used, attach a non-empty
    /// block to the case's retrieval event as the route does, and write the
    /// entries to `W2D_CORTEX_OUT`.
    #[tokio::test]
    #[ignore = "differential harness only: needs a disposable bank"]
    async fn differential_cases_from_harness() {
        let dsn = std::env::var("W2D_CORTEX_DSN").expect("W2D_CORTEX_DSN");
        let input: Value = serde_json::from_str(
            &std::fs::read_to_string(std::env::var("W2D_CORTEX_IN").expect("W2D_CORTEX_IN"))
                .expect("read input"),
        )
        .expect("input json");
        let (db, conn) = tokio_postgres::connect(&dsn, tokio_postgres::NoTls)
            .await
            .expect("connect");
        tokio::spawn(conn);
        let kj = &input["knobs"];
        let knobs = CortexKnobs {
            pin_constraints: kj["pin_constraints"].as_bool().unwrap(),
            read_tracking: kj["read_tracking"].as_bool().unwrap(),
            stale_policy: kj["stale_policy"].as_str().unwrap().into(),
            traces_enabled: kj["traces_enabled"].as_bool().unwrap(),
            bm25: kj["bm25"].as_object().map(|b| Bm25Knobs {
                k1: b["k1"].as_f64().unwrap(),
                b: b["b"].as_f64().unwrap(),
                weight: b["weight"].as_f64().unwrap(),
                top_n: b["top_n"].as_u64().unwrap() as usize,
                min_norm: b["min_score"].as_f64().unwrap(),
            }),
            ..CortexKnobs::default()
        };
        let now = input["now"].as_f64().unwrap();
        let store = crate::stores::cortex::hydrate(&db).await.expect("hydrate");
        let mut results = Vec::new();
        for case in input["cases"].as_array().unwrap() {
            let q: Vec<f32> = case["vec"]
                .as_array()
                .unwrap()
                .iter()
                .map(|x| x.as_f64().unwrap() as f32)
                .collect();
            let query = case["query"].as_str().unwrap();
            let entries = cortex_search(
                &store,
                &db,
                &q,
                query,
                case["top_k"].as_u64().unwrap() as usize,
                case["min_score"].as_f64().unwrap(),
                &knobs,
                now,
            )
            .await
            .expect("cortex_search");
            if !entries.is_empty()
                && let Some(evt) = case["event_id"].as_i64()
            {
                attach_served_facts(&db, evt, &entries)
                    .await
                    .expect("attach");
            }
            results.push(json!({"query": query, "entries": entries}));
        }
        std::fs::write(
            std::env::var("W2D_CORTEX_OUT").expect("W2D_CORTEX_OUT"),
            serde_json::to_string(&json!({"results": results})).unwrap(),
        )
        .expect("write output");
    }

    #[test]
    fn bm25_fusion_boosts_and_injects_per_record() {
        let mut a = rec("alpha", "port", "8080");
        a.embedding = Some(axis(1, 0.6));
        let mut b = rec("beta", "port", "zeta_identifier");
        b.embedding = Some(axis(2, 0.1));
        let s = store(vec![a, b]);
        let q = axis(3, 1.0);
        let dense = s.search(&q, 5, 0.2);
        assert_eq!(dense.len(), 1);
        let knobs = Bm25Knobs {
            k1: 1.5,
            b: 0.75,
            weight: 0.3,
            top_n: 20,
            min_norm: 0.1,
        };
        let fused = bm25_fuse(&s, "zeta_identifier", dense, knobs, 5);
        assert_eq!(fused.len(), 2);
        assert_eq!(fused[0].0, 0);
        assert_eq!(fused[1], (1, 0.3));
    }
}
