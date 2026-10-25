//! The store-curation judge the sweep runs between the junk and candidate
//! judges (`memory/dream.py` `run_sweep_once` 2513-2530):
//! `service_dream.deep_dream_judge_curation` (5426-5735) over the lesson and
//! world duplicate listings, with `curation_safety.py`'s judgment memo and
//! bindings, policy fingerprint, automatic distinct dismissals and their
//! reconsideration (`curation_policy_fingerprint`, `_judgment_signature`,
//! `curation_judgment_state`, `record_bound_curation_judgment`,
//! `apply_auto_distinct`, `refresh_auto_dismissals`, `can_auto_fold`,
//! 144-720), and the request and reply parsing (`memory/dream.py`
//! `format_slot_pair`, `judge_slot_pairs`).
//!
//! In `auto`, a duplicate `can_auto_fold` allows is recorded as a pending
//! action and not applied: the safe-class fold (`apply_slot_duplicate`) is
//! not ported yet (task P3b-4b, `divergences.md`).

#![allow(clippy::result_large_err)]

use std::collections::{HashMap, HashSet};

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::watch;
use tokio_postgres::Client;

use super::extractor::{ExtractFailure, py_str, truthy as py_truthy};
use super::judge::{JudgeEx, STOPPED, check_stop, err_text, judge_extractor, model_name, or_stop};
use super::prompts::SLOT_JUDGE_SYSTEM_PROMPT;
use super::queue_judges::{text_field, verdict_head};
use super::review_judgments::{self as rj, Row, internal, now, vs};
use crate::config::Config;
use crate::graph_review_tools::actions::{dismiss_pair_body, in_txn, reads};
use crate::graph_review_tools::slots::{
    Store, evidence_fingerprint, lesson_dict, slot_key_text, snapshot_record, targeted_snapshots,
    world_dict,
};
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};

/// Snapshots by slot (`_resident_records`' shape).
type Snaps = HashMap<(String, String), Map<String, Value>>;

const BINDINGS_META: &str = "curation_judgment_evidence_v1";
const AUTO_DISMISSALS_META: &str = "curation_auto_dismissals_v1";
const AUTO_REFRESH_CURSOR_META: &str = "curation_auto_dismissal_refresh_cursor_v1";
const FIRST_STORE_META: &str = "curation_judge_first_store";

// ── the evidence ─────────────────────────────────────────────────────────

/// `PairEvidence`: both complete current records, captured before the
/// request is sent.
#[derive(Debug, Clone)]
pub(crate) struct PairEvidence {
    pub store: Store,
    pub a: Map<String, Value>,
    pub b: Map<String, Value>,
    pub a_fingerprint: String,
    pub b_fingerprint: String,
}

fn snap_slot(s: &Map<String, Value>) -> (String, String) {
    (
        vs(s, "entity_norm").unwrap_or("").to_string(),
        vs(s, "attribute_norm").unwrap_or("").to_string(),
    )
}

/// `capture_pair_evidence(store, a, b)`; None when both sides name one slot.
pub(crate) fn capture_pair_evidence(
    store: Store,
    a: &Map<String, Value>,
    b: &Map<String, Value>,
) -> Option<PairEvidence> {
    if snap_slot(a) == snap_slot(b) {
        return None;
    }
    Some(PairEvidence {
        store,
        a: a.clone(),
        b: b.clone(),
        a_fingerprint: evidence_fingerprint(a),
        b_fingerprint: evidence_fingerprint(b),
    })
}

/// `_pair_id(evidence)`: the store and the two sorted slots.
pub(crate) fn pair_id(ev: &PairEvidence) -> String {
    let mut slots = [snap_slot(&ev.a), snap_slot(&ev.b)];
    slots.sort();
    let v = json!([
        ev.store.name(),
        [[slots[0].0, slots[0].1], [slots[1].0, slots[1].1]]
    ]);
    hex::encode(Sha256::digest(
        crate::pyjson::dumps_sorted_compact(&v).as_bytes(),
    ))
}

/// `_judgment_signature(evidence, policy_fingerprint)`.
pub(crate) fn judgment_signature(ev: &PairEvidence, policy: &str) -> String {
    let mut sides = [
        (snap_slot(&ev.a), ev.a_fingerprint.clone()),
        (snap_slot(&ev.b), ev.b_fingerprint.clone()),
    ];
    sides.sort_by(|x, y| x.0.cmp(&y.0));
    let sides: Vec<Value> = sides
        .iter()
        .map(|((e, a), fp)| json!({"slot": [e, a], "fingerprint": fp}))
        .collect();
    let mut m = Map::new();
    m.insert("store".into(), json!(ev.store.name()));
    m.insert("sides".into(), Value::Array(sides));
    m.insert("policy".into(), json!(policy));
    evidence_fingerprint(&m)
}

/// `_listing_key(side)`: the slot key as the duplicate listing spells it.
fn listing_key(side: &Map<String, Value>) -> String {
    let (e, a) = snap_slot(side);
    slot_key_text(&e, &a)
}

/// `curation_pair_keys(evidence)`.
pub(crate) fn pair_keys(ev: &PairEvidence) -> (String, String) {
    let mut k = [listing_key(&ev.a), listing_key(&ev.b)];
    k.sort();
    let [a, b] = k;
    (a, b)
}

/// `curation_policy_fingerprint(service, extractor, observed_model=...)`.
pub(crate) fn policy_fingerprint(
    config: &Config,
    ex: &super::extractor::Compat,
    observed: Option<&str>,
) -> String {
    let mut m = Map::new();
    m.insert("version".into(), json!(1));
    m.insert("prompt".into(), json!(SLOT_JUDGE_SYSTEM_PROMPT));
    m.insert(
        "deep_dream".into(),
        Value::Object(rj::signed_deep_dream(&config.memory.deep_dream)),
    );
    m.insert("extractor_model".into(), json!(ex.model));
    m.insert("extractor_url".into(), json!(ex.base_url));
    m.insert("max_tokens".into(), json!(ex.max_tokens));
    m.insert("timeout".into(), rj::fnum(Some(ex.timeout)));
    m.insert("extra_body".into(), Value::Object(ex.extra_body.clone()));
    m.insert("judge_thinking".into(), json!(false));
    m.insert("observed_model".into(), json!(observed));
    m.insert(
        "override".into(),
        json!(config.dream.extractor_model_override),
    );
    m.insert(
        "effort".into(),
        json!(config.dream.extractor_reasoning_effort),
    );
    evidence_fingerprint(&m)
}

/// `curation_observed_model(extractor)`: a positively observed served
/// identity, probing a launch-default alias.
async fn observed_model(ex: &JudgeEx) -> Option<String> {
    if let Some(s) = ex.served.as_deref().filter(|s| !s.is_empty()) {
        return Some(s.to_string());
    }
    if !ex.compat.base_url.is_empty()
        && matches!(ex.compat.model.as_str(), "extractor" | "bench" | "judge")
    {
        return super::extractor::fetch_served_model(&ex.compat.base_url)
            .await
            .filter(|s| !s.is_empty());
    }
    None
}

/// `curation_judgment_state(judgment, bindings, evidence, policy, now=,
/// horizon=)`: `stale`, `pending`, or a settled action state.
pub(crate) fn judgment_state(
    judgment: Option<&Map<String, Value>>,
    bindings: &Map<String, Value>,
    ev: &PairEvidence,
    policy: &str,
    at: f64,
    horizon: f64,
) -> String {
    let Some(judgment) = judgment else {
        return "stale".into();
    };
    let Some(judged_at) = judgment.get("judged_at").and_then(Value::as_f64) else {
        return "stale".into();
    };
    let time_fresh = (at - judged_at) < horizon;
    let (signature, action) = match bindings.get(&pair_id(ev)) {
        Some(Value::String(s)) => (Some(s.clone()), "settled".to_string()),
        Some(Value::Object(b)) => (
            b.get("signature")
                .and_then(Value::as_str)
                .map(str::to_string),
            match b.get("action") {
                Some(v) if py_truthy(v) => py_str(v),
                _ => "settled".to_string(),
            },
        ),
        _ => return "stale".into(),
    };
    if !time_fresh || signature.as_deref() != Some(judgment_signature(ev, policy).as_str()) {
        return "stale".into();
    }
    action
}

/// `curation_bound_observed_model(bindings, evidence)`.
pub(crate) fn bound_observed_model(
    bindings: &Map<String, Value>,
    ev: &PairEvidence,
) -> Option<String> {
    match bindings.get(&pair_id(ev)) {
        Some(Value::Object(b)) => b.get("observed_model").filter(|v| py_truthy(v)).map(py_str),
        _ => None,
    }
}

/// `curation_bound_action(bindings, evidence)`.
pub(crate) fn bound_action(bindings: &Map<String, Value>, ev: &PairEvidence) -> Option<String> {
    match bindings.get(&pair_id(ev)) {
        Some(Value::String(_)) => Some("settled".into()),
        Some(Value::Object(b)) => Some(match b.get("action") {
            Some(v) if py_truthy(v) => py_str(v),
            _ => "settled".into(),
        }),
        _ => None,
    }
}

/// `can_auto_fold(pair, verdict)`: `(allowed, reason)` for the narrow
/// duplicate classes safe for automatic retirement.
pub(crate) fn can_auto_fold(ev: &PairEvidence, verdict: &Row) -> (bool, String) {
    let reject = |r: &str| (false, r.to_string());
    let kind = match verdict.get("verdict") {
        Some(v) => py_str(v),
        None => "duplicate".into(),
    };
    if kind != "duplicate" {
        return reject("verdict_not_duplicate");
    }
    let keep = verdict.get("keep").and_then(Value::as_str);
    if !matches!(keep, Some("a" | "b")) {
        return reject("invalid_keep");
    }
    let (survivor, loser) = if keep == Some("a") {
        (&ev.a, &ev.b)
    } else {
        (&ev.b, &ev.a)
    };
    match verdict.get("fold") {
        None | Some(Value::Null) => {}
        Some(Value::String(s)) if s.is_empty() => {}
        Some(f) if Some(f) == survivor.get("value") => {}
        _ => return reject("invented_fold"),
    }
    if vs(survivor, "status") != Some("current") || vs(loser, "status") != Some("current") {
        return reject("evidence_not_current");
    }
    let same = |k: &str| survivor.get(k) == loser.get(k);
    for f in ["polarity", "origin"] {
        if !same(f) {
            return reject(&format!("conflicting_{f}"));
        }
    }
    let fields: &[&str] = match ev.store {
        Store::Lesson => &["outcome", "about"],
        Store::World => &[
            "source_url",
            "source_quote",
            "freshness_class",
            "content_hash",
            "source_doc_id",
            "confidence",
            "support",
            "provenance",
        ],
    };
    for f in fields {
        if !rj::py_eq(survivor.get(*f), loser.get(*f)) {
            return reject(&format!("conflicting_{f}"));
        }
    }
    let text = |m: &Map<String, Value>| match m.get("value") {
        Some(v) if py_truthy(v) => crate::storage::py_strip(&py_str(v)).to_string(),
        _ => String::new(),
    };
    let (kept, removed) = (text(survivor), text(loser));
    if kept.is_empty() || removed.is_empty() {
        return reject("empty_value");
    }
    if kept != removed {
        return reject("no_exact_safe_relation");
    }
    if ev.store == Store::World {
        let age = |m: &Map<String, Value>| {
            let f = |k: &str| m.get(k).and_then(Value::as_f64).unwrap_or(0.0);
            (f("retrieved_at"), f("last_confirmed"), f("asserted_at"))
        };
        if age(survivor) < age(loser) {
            return reject("stale_world_survivor");
        }
    }
    (true, "exact".into())
}

/// `interleave(pending, stale)` over `order`: within each store pending
/// first, the stores alternating.
pub(crate) fn interleave<T: Clone>(
    pending: &[(Store, T)],
    stale: &[(Store, T)],
    order: [Store; 2],
) -> Vec<(bool, Store, T)> {
    let take = |list: &[(Store, T)], s: Store| -> Vec<T> {
        list.iter()
            .filter(|(st, _)| *st == s)
            .map(|(_, t)| t.clone())
            .collect()
    };
    let mut p: HashMap<&'static str, std::collections::VecDeque<T>> = HashMap::new();
    let mut q: HashMap<&'static str, std::collections::VecDeque<T>> = HashMap::new();
    for s in [Store::Lesson, Store::World] {
        p.insert(s.name(), take(pending, s).into());
        q.insert(s.name(), take(stale, s).into());
    }
    let mut out = Vec::new();
    while p.values().any(|v| !v.is_empty()) || q.values().any(|v| !v.is_empty()) {
        for s in order {
            if let Some(t) = p.get_mut(s.name()).and_then(|v| v.pop_front()) {
                out.push((true, s, t));
            } else if let Some(t) = q.get_mut(s.name()).and_then(|v| v.pop_front()) {
                out.push((false, s, t));
            }
        }
    }
    out
}

// ── the request and the reply ────────────────────────────────────────────

/// `format_slot_pair(p)`.
pub(crate) fn format_slot_pair(p: &Row) -> String {
    let store = p.get("store").cloned().unwrap_or(Value::Null);
    let side = |tag: &str, s: Option<&Value>| -> String {
        let empty = Map::new();
        let s = match s {
            Some(Value::Object(m)) => m,
            _ => &empty,
        };
        let get = |k: &str| s.get(k).cloned().unwrap_or(Value::Null);
        let extra = if store.as_str() == Some("lesson") {
            format!(
                " [polarity {}, outcome {}, about {}]",
                py_str(&get("polarity")),
                py_str(&get("outcome")),
                match get("about") {
                    Value::String(a) => crate::storage::py_repr(&a),
                    other => py_str(&other),
                }
            )
        } else {
            match s.get("source_url") {
                Some(u) if py_truthy(u) => format!(" [source {}]", py_str(u)),
                _ => String::new(),
            }
        };
        let or_q = |k: &str| s.get(k).map_or_else(|| "?".to_string(), py_str);
        let value = s.get("value").map_or_else(String::new, py_str);
        format!(
            "    {tag} '{}' | '{}'{extra}: {}",
            or_q("entity"),
            or_q("attribute"),
            value.chars().take(240).collect::<String>()
        )
    };
    format!(
        "[{}] store={} cosine {}\n{}\n{}",
        p.get("n").map_or_else(String::new, py_str),
        py_str(&store),
        p.get("similarity")
            .map_or_else(|| "None".to_string(), py_str),
        side("A", p.get("a")),
        side("B", p.get("b")),
    )
}

/// `judge_slot_pairs`' validation loop, as Python's dicts (`n`, verdict,
/// keep, fold, confidence, note): a duplicate naming no survivor
/// degrades to a leave.
pub(crate) fn parse_slot_verdicts(
    raw: &[Value],
    known: &HashSet<usize>,
) -> Result<Vec<Row>, String> {
    let mut out = Vec::new();
    for v in raw {
        let Some((n, mut verdict, confidence)) =
            verdict_head(v, known, &["duplicate", "distinct", "leave"])?
        else {
            continue;
        };
        let (mut keep, mut fold) = (Value::Null, Value::Null);
        if verdict == "duplicate" {
            let raw_keep = match v.get("keep") {
                Some(k) if py_truthy(k) => py_str(k),
                _ => String::new(),
            };
            let k = crate::storage::graph::py_lower(crate::storage::py_strip(&raw_keep));
            keep = if k.is_empty() { Value::Null } else { json!(k) };
            fold = text_field(v, "fold", 600).map_or(Value::Null, Value::String);
            if !matches!(keep.as_str(), Some("a" | "b")) {
                verdict = "leave".into();
                keep = Value::Null;
                fold = Value::Null;
            }
        }
        let mut m = Map::new();
        m.insert("n".into(), json!(n));
        m.insert("verdict".into(), json!(verdict));
        m.insert("keep".into(), keep);
        m.insert("fold".into(), fold);
        m.insert("confidence".into(), rj::fnum(Some(confidence)));
        m.insert(
            "note".into(),
            text_field(v, "note", 400).map_or(Value::Null, Value::String),
        );
        out.push(m);
    }
    Ok(out)
}

async fn judge_slot_pairs(ex: &mut JudgeEx, rows: &[Row]) -> Result<Vec<Row>, String> {
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    let user: Vec<String> = rows.iter().map(format_slot_pair).collect();
    let (served, result) = ex
        .compat
        .judge_request(
            SLOT_JUDGE_SYSTEM_PROMPT,
            &user.join("\n\n"),
            rows.len(),
            "judge_slot_pairs",
        )
        .await;
    if let Some(s) = served {
        ex.served = s;
    }
    let raw = result.map_err(|e| match e {
        ExtractFailure::Extractor(x) => x.message,
        ExtractFailure::Uncaught(u) => u.message,
    })?;
    let known: HashSet<usize> = rows
        .iter()
        .filter_map(|r| r.get("n").and_then(Value::as_u64))
        .map(|n| n as usize)
        .collect();
    parse_slot_verdicts(&raw, &known)
}

/// `_stamp_skipped(verdicts, rows, keep=None, fold=None)`.
fn stamp_skipped(verdicts: &mut Vec<Row>, rows: &[Row]) {
    let returned: HashSet<u64> = verdicts
        .iter()
        .filter_map(|v| v.get("n").and_then(Value::as_u64))
        .collect();
    for r in rows {
        let n = r.get("n").and_then(Value::as_u64).unwrap_or(0);
        if !returned.contains(&n) {
            let mut m = Map::new();
            m.insert("n".into(), json!(n));
            m.insert("verdict".into(), json!("leave"));
            m.insert("confidence".into(), json!(0.0));
            m.insert("note".into(), json!("model returned no verdict"));
            m.insert("keep".into(), Value::Null);
            m.insert("fold".into(), Value::Null);
            verdicts.push(m);
        }
    }
}

// ── storage ──────────────────────────────────────────────────────────────

/// `curation_judgments(store)`: `{(a_key, b_key): row}`.
async fn curation_judgments(c: &Client, store: Store) -> Result<Snaps, ToolError> {
    Ok(c.query(
        "SELECT a_key, b_key, verdict, keep, fold, confidence::text::double precision, note, \
         model, judged_at FROM curation_judgments WHERE store = $1",
        &[&store.name()],
    )
    .await
    .map_err(internal)?
    .iter()
    .map(|r| {
        let mut m = Map::new();
        let t = |i: usize| {
            r.get::<_, Option<String>>(i)
                .map_or(Value::Null, Value::String)
        };
        m.insert("verdict".into(), t(2));
        m.insert("keep".into(), t(3));
        m.insert("fold".into(), t(4));
        m.insert("confidence".into(), rj::fnum(r.get(5)));
        m.insert("note".into(), t(6));
        m.insert("model".into(), t(7));
        m.insert("judged_at".into(), rj::fnum(r.get(8)));
        ((r.get(0), r.get(1)), m)
    })
    .collect())
}

async fn bindings(c: &Client) -> Result<Map<String, Value>, ToolError> {
    Ok(match rj::get_meta(c, BINDINGS_META).await? {
        Some(Value::Object(m)) => m,
        _ => Map::new(),
    })
}

async fn markers(c: &Client) -> Result<Map<String, Value>, ToolError> {
    Ok(match rj::get_meta(c, AUTO_DISMISSALS_META).await? {
        Some(Value::Object(m)) => m,
        _ => Map::new(),
    })
}

/// `_resident_records(service, store)`: slot to the current record's
/// snapshot, the last record of a slot winning.
fn resident_snapshots(ready: &Ready, store: Store) -> Result<Snaps, ToolError> {
    let mut out = HashMap::new();
    match store {
        Store::Lesson => {
            let s = ready.lessons.snapshot();
            for r in s.records.iter().filter(|r| r.status == "current") {
                let snap = snapshot_record(Store::Lesson, &lesson_dict(r), r.embedding.as_deref())?;
                out.insert(snap_slot(&snap), snap);
            }
        }
        Store::World => {
            let s = ready.world.snapshot();
            for r in s.records.iter().filter(|r| r.status == "current") {
                let snap = snapshot_record(Store::World, &world_dict(r), r.embedding.as_deref())?;
                out.insert(snap_slot(&snap), snap);
            }
        }
    }
    Ok(out)
}

/// The listing-keyed snapshots the judge captures its evidence from (keys
/// as `_slot_key(*r.key)` spells them).
fn listing_snapshots(
    ready: &Ready,
    store: Store,
) -> Result<HashMap<String, Map<String, Value>>, ToolError> {
    let mut out = HashMap::new();
    let mut put = |key: &(String, String), snap: Map<String, Value>| {
        out.insert(slot_key_text(&key.0, &key.1), snap);
    };
    match store {
        Store::Lesson => {
            let s = ready.lessons.snapshot();
            for r in s.current_records() {
                put(
                    &r.key,
                    snapshot_record(Store::Lesson, &lesson_dict(r), r.embedding.as_deref())?,
                );
            }
        }
        Store::World => {
            let s = ready.world.snapshot();
            for r in s.records.iter().filter(|r| r.status == "current") {
                put(
                    &r.key,
                    snapshot_record(Store::World, &world_dict(r), r.embedding.as_deref())?,
                );
            }
        }
    }
    Ok(out)
}

fn evidence_state(current: &Snaps, ev: &PairEvidence) -> &'static str {
    match (
        current.get(&snap_slot(&ev.a)),
        current.get(&snap_slot(&ev.b)),
    ) {
        (Some(a), Some(b)) => {
            if evidence_fingerprint(a) != ev.a_fingerprint
                || evidence_fingerprint(b) != ev.b_fingerprint
            {
                "changed"
            } else {
                "current"
            }
        }
        _ => "missing",
    }
}

/// `_resident_evidence_state(service, evidence)`.
fn resident_evidence_state(ready: &Ready, ev: &PairEvidence) -> Result<&'static str, ToolError> {
    Ok(evidence_state(&resident_snapshots(ready, ev.store)?, ev))
}

/// `_locked_evidence_state(storage, evidence)` in the caller's
/// transaction: both slots' current rows locked and re-fingerprinted.
async fn locked_evidence_state(c: &Client, ev: &PairEvidence) -> Result<&'static str, ToolError> {
    let (a, b) = (snap_slot(&ev.a), snap_slot(&ev.b));
    let mut current = HashMap::new();
    for snap in targeted_snapshots(c, ev.store, &a, &b).await? {
        if vs(&snap, "status") == Some("current") {
            let slot = snap_slot(&snap);
            if slot == a || slot == b {
                current.insert(slot, snap);
            }
        }
    }
    Ok(evidence_state(&current, ev))
}

/// `record_bound_curation_judgment(..., locked=True)`: both rows
/// revalidated, then the opinion and its binding persisted together.
#[allow(clippy::too_many_arguments)]
async fn record_bound_judgment(
    client: &Client,
    ready: &Ready,
    ev: &PairEvidence,
    verdict: &Row,
    policy: &str,
    model: &str,
    at: f64,
    observed: Option<&str>,
    requested_policy: &str,
    action_state: &str,
) -> Result<bool, ToolError> {
    if resident_evidence_state(ready, ev)? != "current" {
        return Ok(false);
    }
    in_txn(client, |c| async move {
        if locked_evidence_state(c, ev).await? != "current" {
            return Ok(false);
        }
        let (a_key, b_key) = pair_keys(ev);
        let text = |k: &str| match verdict.get(k) {
            Some(Value::String(s)) => Some(s.clone()),
            Some(Value::Null) | None => None,
            Some(v) => Some(py_str(v)),
        };
        let verdict_text = match verdict.get("verdict") {
            Some(v) if py_truthy(v) => py_str(v),
            _ => "leave".into(),
        };
        let confidence = verdict
            .get("confidence")
            .and_then(Value::as_f64)
            .map(|x| x as f32);
        c.execute(
            "INSERT INTO curation_judgments (store, a_key, b_key, verdict, \
             keep, fold, confidence, note, model, judged_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) \
             ON CONFLICT (store, a_key, b_key) DO UPDATE SET \
             verdict = EXCLUDED.verdict, keep = EXCLUDED.keep, \
             fold = EXCLUDED.fold, confidence = EXCLUDED.confidence, \
             note = EXCLUDED.note, model = EXCLUDED.model, \
             judged_at = EXCLUDED.judged_at",
            &[
                &ev.store.name(),
                &a_key,
                &b_key,
                &verdict_text,
                &text("keep"),
                &text("fold"),
                &confidence,
                &text("note"),
                &model,
                &at,
            ],
        )
        .await
        .map_err(internal)?;
        let mut raw = [
            vs(&ev.a, "key").unwrap_or("").to_string(),
            vs(&ev.b, "key").unwrap_or("").to_string(),
        ];
        raw.sort();
        if (raw[0].as_str(), raw[1].as_str()) != (a_key.as_str(), b_key.as_str()) {
            // A row under the raw evidence keys, from before the names
            // followed the listing spelling: nothing reads it any more.
            c.execute(
                "DELETE FROM curation_judgments WHERE store=$1 AND a_key=$2 AND b_key=$3",
                &[&ev.store.name(), &raw[0], &raw[1]],
            )
            .await
            .map_err(internal)?;
        }
        let mut b = bindings(c).await?;
        b.insert(
            pair_id(ev),
            json!({"signature": judgment_signature(ev, policy), "action": action_state,
                   "observed_model": observed, "requested_policy": requested_policy}),
        );
        rj::set_meta(c, BINDINGS_META, &Value::Object(b)).await?;
        Ok(true)
    })
    .await
}

/// `_set_action_state(storage, evidence, state)` in the caller's
/// transaction; `Err` when the binding is missing (Python's RuntimeError).
async fn set_action_state(c: &Client, ev: &PairEvidence, state: &str) -> Result<(), ToolError> {
    let mut b = bindings(c).await?;
    let id = pair_id(ev);
    let Some(Value::Object(binding)) = b.get(&id).cloned() else {
        return Err(internal("curation judgment binding is missing"));
    };
    let mut binding = binding;
    binding.insert("action".into(), json!(state));
    b.insert(id, Value::Object(binding));
    rj::set_meta(c, BINDINGS_META, &Value::Object(b)).await
}

/// `apply_auto_distinct(service, evidence, policy_guard=..., settle_judgment=True)`
/// under the writer lock: `true` when the pair was dismissed.
async fn apply_auto_distinct(
    client: &Client,
    ready: &Ready,
    ev: &PairEvidence,
    guard: bool,
) -> Result<bool, ToolError> {
    if !guard {
        return Ok(false);
    }
    if resident_evidence_state(ready, ev)? != "current" {
        return Ok(false);
    }
    in_txn(client, |c| async move {
        if locked_evidence_state(c, ev).await? != "current" {
            return Ok(false);
        }
        let id = pair_id(ev);
        let binding = bindings(c).await?.get(&id).cloned();
        let bound = match &binding {
            Some(Value::Object(b)) => b.clone(),
            _ => Map::new(),
        };
        let truthy = |k: &str| bound.get(k).is_some_and(py_truthy);
        if !matches!(binding, Some(Value::Object(_)))
            || !truthy("requested_policy")
            || !truthy("observed_model")
        {
            return Ok(false);
        }
        let (ka, kb) = pair_keys(ev);
        let (a, b) = (
            format!("{}:{ka}", ev.store.name()),
            format!("{}:{kb}", ev.store.name()),
        );
        let new = dismiss_pair_body(c, &a, &b).await?;
        let mut m = markers(c).await?;
        // A pre-existing unmarked row is a human verdict: it is not
        // relabelled as automatic.
        if (new || m.contains_key(&id))
            && !crate::mutants::active("mcp-dream-curation-distinct-unmarked")
        {
            let side = |s: &Map<String, Value>| {
                json!({"key": listing_key(s), "entity_norm": s.get("entity_norm"),
                       "attribute_norm": s.get("attribute_norm")})
            };
            m.insert(
                id.clone(),
                json!({"store": ev.store.name(), "a": side(&ev.a), "b": side(&ev.b),
                       "a_fingerprint": ev.a_fingerprint, "b_fingerprint": ev.b_fingerprint,
                       "requested_policy": bound.get("requested_policy"),
                       "observed_model": bound.get("observed_model")}),
            );
            rj::set_meta(c, AUTO_DISMISSALS_META, &Value::Object(m)).await?;
            rj::set_meta(c, AUTO_REFRESH_CURSOR_META, &json!(id)).await?;
        }
        set_action_state(c, ev, "applied").await?;
        Ok(true)
    })
    .await
}

/// `refresh_auto_dismissals(service, locked=True, ...)`: reopen the
/// automatic dismissals evidence or a known policy invalidated. Caller
/// holds the writer lock.
async fn refresh_auto_dismissals(
    client: &Client,
    ready: &Ready,
    check: Option<(&str, Option<&str>, Option<&str>, i64)>,
) -> Result<i64, ToolError> {
    let m = reads(client, |c| async move { markers(c).await }).await?;
    if m.is_empty() {
        return Ok(0);
    }
    let current: HashMap<&'static str, Snaps> = [
        ("lesson", resident_snapshots(ready, Store::Lesson)?),
        ("world", resident_snapshots(ready, Store::World)?),
    ]
    .into();
    let mut ids: Vec<String> = m.keys().cloned().collect();
    ids.sort();
    if let Some((_, _, _, limit)) = check {
        let cap = limit.max(1) as usize;
        let cursor = reads(client, |c| async move {
            rj::get_meta(c, AUTO_REFRESH_CURSOR_META).await
        })
        .await?;
        let cursor = match cursor {
            Some(v) if py_truthy(&v) => py_str(&v),
            _ => String::new(),
        };
        let (after, before): (Vec<String>, Vec<String>) = ids
            .into_iter()
            .partition(|id| id.as_str() > cursor.as_str());
        ids = after.into_iter().chain(before).take(cap).collect();
    }
    let mut stale: Vec<(String, Value)> = Vec::new();
    for id in &ids {
        let marker = m.get(id).cloned().unwrap_or(Value::Null);
        let store = marker.get("store").and_then(Value::as_str).unwrap_or("");
        let Some(cur) = current.get(store) else {
            stale.push((id.clone(), marker));
            continue;
        };
        let side = |k: &str| -> Option<Map<String, Value>> {
            match marker.get(k) {
                Some(Value::Object(s)) if !s.is_empty() => Some(s.clone()),
                _ => None,
            }
        };
        let (a, b) = (side("a"), side("b"));
        let live = |s: &Option<Map<String, Value>>| s.as_ref().and_then(|s| cur.get(&snap_slot(s)));
        let (la, lb) = (live(&a), live(&b));
        let fp = |k: &str| marker.get(k).and_then(Value::as_str);
        let moved = match (la, lb) {
            (Some(la), Some(lb)) => {
                Some(evidence_fingerprint(la).as_str()) != fp("a_fingerprint")
                    || Some(evidence_fingerprint(lb).as_str()) != fp("b_fingerprint")
            }
            _ => true,
        };
        if moved {
            stale.push((id.clone(), marker));
            continue;
        }
        let (a, b) = (a.unwrap_or_default(), b.unwrap_or_default());
        if vs(&a, "key") != Some(listing_key(&a).as_str())
            || vs(&b, "key") != Some(listing_key(&b).as_str())
        {
            stale.push((id.clone(), marker));
            continue;
        }
        if let Some((policy, observed, response, _)) = check {
            if marker.get("requested_policy").and_then(Value::as_str) != Some(policy) {
                stale.push((id.clone(), marker));
                continue;
            }
            let known = [observed, response];
            if known
                .iter()
                .flatten()
                .any(|model| marker.get("observed_model").and_then(Value::as_str) != Some(*model))
            {
                stale.push((id.clone(), marker));
            }
        }
    }
    let advance = check.is_some() && !ids.is_empty();
    if stale.is_empty() && !advance {
        return Ok(0);
    }
    let last = ids.last().cloned();
    let n = stale.len() as i64;
    in_txn(client, |c| async move {
        let mut m = m;
        for (id, marker) in &stale {
            let key = |k: &str| {
                marker
                    .get(k)
                    .and_then(|s| s.get("key"))
                    .and_then(Value::as_str)
            };
            let store = marker.get("store").and_then(Value::as_str).unwrap_or("");
            if let (true, Some(a), Some(b)) =
                (matches!(store, "lesson" | "world"), key("a"), key("b"))
                && !a.is_empty()
                && !b.is_empty()
            {
                let mut pair = [format!("{store}:{a}"), format!("{store}:{b}")];
                pair.sort();
                c.execute(
                    "DELETE FROM dismissed_pairs WHERE a_norm=$1 AND b_norm=$2",
                    &[&pair[0], &pair[1]],
                )
                .await
                .map_err(internal)?;
            }
            m.shift_remove(id);
        }
        rj::set_meta(c, AUTO_DISMISSALS_META, &Value::Object(m)).await?;
        if advance && let Some(last) = &last {
            rj::set_meta(c, AUTO_REFRESH_CURSOR_META, &json!(last)).await?;
        }
        Ok(())
    })
    .await?;
    Ok(n)
}

// ── the tick ─────────────────────────────────────────────────────────────

fn answer(pairs: &[(&str, Value)]) -> Map<String, Value> {
    pairs
        .iter()
        .map(|(k, v)| ((*k).to_string(), v.clone()))
        .collect()
}

/// `deep_dream_judge_curation()`: never fails into the sweep.
pub(crate) async fn deep_dream_judge_curation(
    service: &Service,
    stop: &watch::Receiver<bool>,
) -> Map<String, Value> {
    let cfg = &service.config.memory.deep_dream;
    if !cfg.judges_enabled {
        return answer(&[("judged", json!(0)), ("skipped", json!("judges_disabled"))]);
    }
    if !matches!(
        cfg.curation_judge_mode.as_str(),
        "shadow" | "auto-distinct" | "auto"
    ) {
        return answer(&[("judged", json!(0)), ("skipped", json!("disabled"))]);
    }
    let mut reopened = 0i64;
    match curation_tick(service, stop, &mut reopened).await {
        Ok(out) => out,
        Err(e) if e == STOPPED => answer(&[("judged", json!(0)), ("skipped", json!("stopping"))]),
        Err(e) => {
            eprintln!("deep-dream curation judge failed: {e}");
            answer(&[
                ("judged", json!(0)),
                ("error", json!(e)),
                ("reopened_auto_dismissals", json!(reopened)),
            ])
        }
    }
}

/// One listed pair with its captured evidence.
#[derive(Clone)]
struct Item {
    pair: Map<String, Value>,
    ev: PairEvidence,
}

fn store_of(name: &str) -> Store {
    if name == "world" {
        Store::World
    } else {
        Store::Lesson
    }
}

/// The curation judge's `try` body; `reopened` is kept for the error answer.
#[allow(clippy::too_many_lines)]
async fn curation_tick(
    service: &Service,
    stop: &watch::Receiver<bool>,
    reopened: &mut i64,
) -> Result<Map<String, Value>, String> {
    let cfg = &service.config.memory.deep_dream;
    let mode = cfg.curation_judge_mode.clone();
    let cap = cfg.judge_batch.max(1) as usize;
    let e2s = |e: ToolError| err_text(&e);
    let ready = service.ensure_init_inner().await?;
    let client = ready.storage.client();
    let reopened_now =
        |r: i64| answer(&[("judged", json!(0)), ("reopened_auto_dismissals", json!(r))]);
    let (automatic, mut dismissed, lesson_recs, world_recs, full, memo, binds, first_store) = {
        let _lock = ready.write.lock().await;
        *reopened = refresh_auto_dismissals(client, &ready, None)
            .await
            .map_err(e2s)?;
        let (automatic, dismissed, memo_l, memo_w, binds, first) = reads(client, |c| async move {
            let dismissed: HashSet<(String, String)> = c
                .query("SELECT a_norm, b_norm FROM dismissed_pairs", &[])
                .await
                .map_err(internal)?
                .iter()
                .map(|r| (r.get(0), r.get(1)))
                .collect();
            Ok((
                !markers(c).await?.is_empty(),
                dismissed,
                curation_judgments(c, Store::Lesson).await?,
                curation_judgments(c, Store::World).await?,
                bindings(c).await?,
                rj::get_meta(c, FIRST_STORE_META).await?,
            ))
        })
        .await
        .map_err(e2s)?;
        let (lesson_recs, world_recs) =
            super::deep::curation_records(&ready, cfg.snippet_max_chars);
        let full = [
            (
                "lesson",
                listing_snapshots(&ready, Store::Lesson).map_err(e2s)?,
            ),
            (
                "world",
                listing_snapshots(&ready, Store::World).map_err(e2s)?,
            ),
        ];
        let first = match first {
            Some(Value::String(s)) if !s.is_empty() => s,
            Some(v) if py_truthy(&v) => py_str(&v),
            _ => "lesson".into(),
        };
        (
            automatic,
            dismissed,
            lesson_recs,
            world_recs,
            full,
            [memo_l, memo_w],
            binds,
            first,
        )
    };
    let mut ex: Option<JudgeEx> = None;
    let mut requested_policy = String::new();
    let mut pre_observed: Option<String> = None;
    let mut last_observed: Option<String> = None;
    let mut scheduling_observed: Option<String> = None;
    if automatic {
        let Some(x) = or_stop(stop, judge_extractor(service, None)).await? else {
            return Ok(answer(&[
                ("judged", json!(0)),
                ("skipped", json!("no_judge_extractor")),
                ("reopened_auto_dismissals", json!(*reopened)),
            ]));
        };
        requested_policy = policy_fingerprint(&service.config, &x.compat, None);
        pre_observed = or_stop(stop, observed_model(&x)).await?;
        let _lock = ready.write.lock().await;
        let rp = &requested_policy;
        last_observed = reads(client, |c| async move {
            rj::last_response_identity(c, "curation", rp).await
        })
        .await
        .map_err(e2s)?;
        scheduling_observed = pre_observed.clone().or_else(|| last_observed.clone());
        *reopened += refresh_auto_dismissals(
            client,
            &ready,
            Some((
                &requested_policy,
                pre_observed.as_deref(),
                last_observed.as_deref(),
                cap as i64,
            )),
        )
        .await
        .map_err(e2s)?;
        dismissed = reads(client, |c| async move {
            Ok(c.query("SELECT a_norm, b_norm FROM dismissed_pairs", &[])
                .await
                .map_err(internal)?
                .iter()
                .map(|r| (r.get(0), r.get(1)))
                .collect())
        })
        .await
        .map_err(e2s)?;
        ex = Some(x);
    }
    let dups = |recs: &[crate::graph_consolidation::SlotRecord], store: &str| {
        crate::graph_consolidation::slot_duplicate_candidates(
            recs,
            cfg.curation_min_similarity,
            cfg.curation_top_k,
            &super::deep::store_dismissed(&dismissed, store),
        )
    };
    let mut items: Vec<Item> = Vec::new();
    for (store, list) in [
        ("lesson", dups(&lesson_recs, "lesson")),
        ("world", dups(&world_recs, "world")),
    ] {
        let snaps = &full.iter().find(|(s, _)| *s == store).expect("store").1;
        for pair in list {
            let Value::Object(pair) = pair else { continue };
            let (Some(l), Some(r)) = (
                vs(&pair, "a_key").and_then(|k| snaps.get(k)),
                vs(&pair, "b_key").and_then(|k| snaps.get(k)),
            ) else {
                continue;
            };
            if let Some(ev) = capture_pair_evidence(store_of(store), l, r) {
                items.push(Item {
                    pair: pair.clone(),
                    ev,
                });
            }
        }
    }
    if items.is_empty() {
        return Ok(reopened_now(*reopened));
    }
    if ex.is_none() {
        let Some(x) = or_stop(stop, judge_extractor(service, None)).await? else {
            return Ok(answer(&[
                ("judged", json!(0)),
                ("skipped", json!("no_judge_extractor")),
                ("reopened_auto_dismissals", json!(*reopened)),
            ]));
        };
        requested_policy = policy_fingerprint(&service.config, &x.compat, None);
        pre_observed = or_stop(stop, observed_model(&x)).await?;
        let _lock = ready.write.lock().await;
        let rp = &requested_policy;
        last_observed = reads(client, |c| async move {
            rj::last_response_identity(c, "curation", rp).await
        })
        .await
        .map_err(e2s)?;
        scheduling_observed = pre_observed.clone().or_else(|| last_observed.clone());
        ex = Some(x);
    }
    let mut ex = ex.expect("judge extractor");
    let at = now();
    let horizon = cfg.curation_rejudge_days * 86400.0;
    let memo_of = |s: Store| {
        if s == Store::Lesson {
            &memo[0]
        } else {
            &memo[1]
        }
    };
    let state = |item: &Item| -> String {
        let (a, b) = {
            let mut k = [
                vs(&item.pair, "a_key").unwrap_or("").to_string(),
                vs(&item.pair, "b_key").unwrap_or("").to_string(),
            ];
            k.sort();
            let [a, b] = k;
            (a, b)
        };
        let judgment = memo_of(item.ev.store).get(&(a, b));
        let prior_observed = bound_observed_model(&binds, &item.ev);
        let prior_action = bound_action(&binds, &item.ev);
        let deferred = prior_action.as_deref() == Some("deferred_identity_unknown");
        let observed = if deferred {
            prior_observed.clone()
        } else {
            scheduling_observed
                .clone()
                .or_else(|| prior_observed.clone())
        };
        let policy = policy_fingerprint(&service.config, &ex.compat, observed.as_deref());
        let js = judgment_state(judgment, &binds, &item.ev, &policy, at, horizon);
        let known_changed = prior_observed.is_some()
            && [pre_observed.as_deref(), last_observed.as_deref()]
                .iter()
                .flatten()
                .any(|m| Some(*m) != prior_observed.as_deref());
        if !deferred && known_changed {
            return "stale".into();
        }
        if js == "pending"
            && (pre_observed.is_none()
                || prior_observed.is_none()
                || pre_observed != prior_observed)
        {
            return "stale".into();
        }
        js
    };
    let classified: Vec<(Item, String)> = items
        .into_iter()
        .map(|i| {
            let s = state(&i);
            (i, s)
        })
        .collect();
    let order = if first_store == "world" {
        [Store::World, Store::Lesson]
    } else {
        [Store::Lesson, Store::World]
    };
    let pending: Vec<(Store, Item)> = classified
        .iter()
        .filter(|(_, s)| s == "pending")
        .map(|(i, _)| (i.ev.store, i.clone()))
        .collect();
    let todo: Vec<(Store, Item)> = classified
        .iter()
        .filter(|(_, s)| s == "stale")
        .map(|(i, _)| (i.ev.store, i.clone()))
        .collect();
    if todo.is_empty() && pending.is_empty() {
        return Ok(reopened_now(*reopened));
    }
    let scheduled: Vec<(bool, Store, Item)> = interleave(&pending, &todo, order)
        .into_iter()
        .take(cap)
        .collect();
    let replay: Vec<Item> = scheduled
        .iter()
        .filter(|s| s.0)
        .map(|s| s.2.clone())
        .collect();
    let batch: Vec<Item> = scheduled
        .iter()
        .filter(|s| !s.0)
        .map(|s| s.2.clone())
        .collect();
    let next = if scheduled.last().map(|s| s.1) == Some(Store::Lesson) {
        "world"
    } else {
        "lesson"
    };
    {
        let _lock = ready.write.lock().await;
        in_txn(client, |c| async move {
            rj::set_meta(c, FIRST_STORE_META, &json!(next)).await
        })
        .await
        .map_err(e2s)?;
    }
    if !batch.is_empty() {
        eprintln!(
            "deep-dream curation judge: judging {} duplicate listing(s) (mode {mode})",
            batch.len()
        );
    }
    let rows: Vec<Row> = batch
        .iter()
        .enumerate()
        .map(|(i, it)| {
            let mut m = Map::new();
            m.insert("n".into(), json!(i + 1));
            m.insert("store".into(), json!(it.ev.store.name()));
            m.insert(
                "similarity".into(),
                it.pair.get("similarity").cloned().unwrap_or(Value::Null),
            );
            m.insert("a".into(), Value::Object(it.ev.a.clone()));
            m.insert("b".into(), Value::Object(it.ev.b.clone()));
            m.insert(
                "a_key".into(),
                it.pair.get("a_key").cloned().unwrap_or(Value::Null),
            );
            m.insert(
                "b_key".into(),
                it.pair.get("b_key").cloned().unwrap_or(Value::Null),
            );
            m
        })
        .collect();
    let mut verdicts: Vec<Row> = Vec::new();
    if !rows.is_empty() {
        check_stop(stop)?;
        verdicts = or_stop(stop, judge_slot_pairs(&mut ex, &rows)).await??;
        stamp_skipped(&mut verdicts, &rows);
    }
    let policy_changed = |r: i64| {
        answer(&[
            ("judged", json!(0)),
            ("reason", json!("policy_changed")),
            ("reopened_auto_dismissals", json!(r)),
        ])
    };
    if policy_fingerprint(&service.config, &ex.compat, None) != requested_policy {
        return Ok(policy_changed(*reopened));
    }
    let observed = or_stop(stop, observed_model(&ex)).await?;
    {
        let _lock = ready.write.lock().await;
        if policy_fingerprint(&service.config, &ex.compat, None) != requested_policy {
            return Ok(policy_changed(*reopened));
        }
        let (rp, ob) = (&requested_policy, observed.as_deref());
        in_txn(client, |c| async move {
            rj::record_response_identity(c, "curation", rp, ob).await
        })
        .await
        .map_err(e2s)?;
    }
    if pre_observed.is_some() && observed != pre_observed {
        return Ok(answer(&[
            ("judged", json!(0)),
            ("applied", json!(0)),
            ("reason", json!("served_model_changed")),
            ("observed_before", json!(pre_observed)),
            ("observed_after", json!(observed)),
            ("reopened_auto_dismissals", json!(*reopened)),
        ]));
    }
    let model = observed.clone().unwrap_or_else(|| model_name(&ex, None));
    let bound_policy = policy_fingerprint(&service.config, &ex.compat, observed.as_deref());
    let response_served = ex.served.clone();
    let (mut judged, mut applied, mut deferred_unknown) = (0i64, 0i64, 0i64);
    let mut actions: Vec<(Item, Row)> = Vec::new();
    for it in &replay {
        let mut keys = [
            vs(&it.pair, "a_key").unwrap_or("").to_string(),
            vs(&it.pair, "b_key").unwrap_or("").to_string(),
        ];
        keys.sort();
        let [a, b] = keys;
        let saved = memo_of(it.ev.store)
            .get(&(a, b))
            .cloned()
            .unwrap_or_default();
        let mut v = Map::new();
        v.insert(
            "verdict".into(),
            saved.get("verdict").cloned().unwrap_or(Value::Null),
        );
        v.insert(
            "keep".into(),
            saved.get("keep").cloned().unwrap_or(Value::Null),
        );
        v.insert(
            "fold".into(),
            saved.get("fold").cloned().unwrap_or(Value::Null),
        );
        let conf = match saved.get("confidence") {
            Some(c) if py_truthy(c) => c.clone(),
            _ => json!(0.0),
        };
        v.insert("confidence".into(), conf);
        v.insert(
            "note".into(),
            saved.get("note").cloned().unwrap_or(Value::Null),
        );
        actions.push((it.clone(), v));
    }
    for v in &verdicts {
        let n = v.get("n").and_then(Value::as_u64).unwrap_or(0) as usize;
        let Some(it) = batch.get(n.wrapping_sub(1)) else {
            continue;
        };
        let conf = v.get("confidence").and_then(Value::as_f64).unwrap_or(0.0);
        let verdict = vs(v, "verdict").unwrap_or("");
        let wants = (verdict == "distinct"
            && matches!(mode.as_str(), "auto-distinct" | "auto")
            && conf >= cfg.curation_distinct_min_confidence)
            || (verdict == "duplicate"
                && mode == "auto"
                && conf >= cfg.curation_forget_min_confidence);
        let defer = wants && observed.is_none();
        let action_state = if defer {
            "deferred_identity_unknown"
        } else if wants {
            "pending"
        } else {
            "settled"
        };
        let ok = {
            let _lock = ready.write.lock().await;
            record_bound_judgment(
                client,
                &ready,
                &it.ev,
                v,
                &bound_policy,
                &model,
                at,
                observed.as_deref(),
                &requested_policy,
                action_state,
            )
            .await
            .map_err(e2s)?
        };
        if !ok {
            continue;
        }
        judged += 1;
        deferred_unknown += i64::from(defer);
        if wants && !defer {
            actions.push((it.clone(), v.clone()));
        }
    }
    for (it, v) in &actions {
        let conf = v.get("confidence").and_then(Value::as_f64).unwrap_or(0.0);
        let verdict = vs(v, "verdict").unwrap_or("");
        let _lock = ready.write.lock().await;
        // `policy_guard() and curation_judgment_saved(...)`, under the
        // apply's lock.
        let guard = observed.is_some()
            && policy_fingerprint(&service.config, &ex.compat, None) == requested_policy
            && ex.served == response_served
            && {
                let ev = &it.ev;
                reads(client, |c| async move {
                    Ok(curation_judgments(c, ev.store)
                        .await?
                        .contains_key(&pair_keys(ev)))
                })
                .await
                .map_err(e2s)?
            };
        if verdict == "distinct"
            && matches!(mode.as_str(), "auto-distinct" | "auto")
            && conf >= cfg.curation_distinct_min_confidence
        {
            applied += i64::from(
                apply_auto_distinct(client, &ready, &it.ev, guard)
                    .await
                    .map_err(e2s)?,
            );
        } else if verdict == "duplicate"
            && mode == "auto"
            && conf >= cfg.curation_forget_min_confidence
            && !can_auto_fold(&it.ev, v).0
        {
            // A duplicate the safe-fold rule allows is not applied: the
            // fold is not ported (task P3b-4b), so its action stays pending
            // and replays on the next tick. One it refuses settles manual.
            if !crate::mutants::active("mcp-dream-curation-manual-unsettled") {
                let ev = &it.ev;
                in_txn(client, |c| async move {
                    set_action_state(c, ev, "manual").await
                })
                .await
                .map_err(e2s)?;
            }
        }
    }
    let pending_unjudged = (todo.len() as i64 - judged).max(0);
    Ok(answer(&[
        ("judged", json!(judged)),
        ("applied", json!(applied)),
        ("pending_unjudged", json!(pending_unjudged)),
        ("deferred_identity_unknown", json!(deferred_unknown)),
        ("reopened_auto_dismissals", json!(*reopened)),
        ("model", json!(model)),
        ("mode", json!(mode)),
    ]))
}

#[cfg(test)]
#[path = "curation_judge_tests.rs"]
mod tests;
