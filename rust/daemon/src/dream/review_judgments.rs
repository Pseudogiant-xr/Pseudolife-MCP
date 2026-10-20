//! The merge judge's bookkeeping: `memory/review_judgments.py` for
//! `kind="merge"` (fingerprints, the judging policy, response identity,
//! moved vote endpoints and the evidence signing of one judge tick) and the
//! proposal half of `memory/review_decisions.py` (the versioned automatic
//! reject records and their reconsideration).
//!
//! A pending row is the `pending_entity_proposals` dict Python reads
//! (`storage/postgres.py:3777-3804`), as a JSON object with its keys and
//! value types: REAL columns decode through their text form, as psycopg
//! reads them. Every function taking a `&Client` runs inside the caller's
//! `txn::run` / `txn::with_client` body, under the writer lock where Python
//! holds the service lock.
//!
//! Python raises on a malformed meta value (a memo or index that is not an
//! object); here such a value reads as empty (declared in
//! `divergences.md`).

use std::collections::{HashMap, HashSet};

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tokio_postgres::Client;

use crate::config::{Config, DeepDreamConfig};
use crate::dream::deep::{self, JudgeEvidence};
use crate::dream::extractor::Compat;
use crate::graph_review::EntityProposal;
use crate::mcp::dispatch::ToolError;

/// A pending merge row (`pending_entity_proposals`' dict).
pub(crate) type Row = Map<String, Value>;

pub(crate) const KIND: &str = "merge";
const MEMO_KEY: &str = "review_judgments_v1_merge";
const IDENTITY_PREFIX: &str = "review_response_identity_v1_";
const INDEX_KEY: &str = "automatic_review_decisions_v1";
const RECORD_PREFIX: &str = "automatic_review_decision_v1:";
const RECENT_CAP: usize = 100;

pub(crate) fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

pub(crate) fn now() -> f64 {
    crate::episodes::now()
}

// ── row access ───────────────────────────────────────────────────────────

pub(crate) fn vs<'a>(row: &'a Row, key: &str) -> Option<&'a str> {
    row.get(key).and_then(Value::as_str)
}

/// `row.get(key)` truthy, for a string column.
pub(crate) fn truthy(row: &Row, key: &str) -> bool {
    vs(row, key).is_some_and(|s| !s.is_empty())
}

pub(crate) fn vf(row: &Row, key: &str) -> Option<f64> {
    row.get(key).and_then(Value::as_f64)
}

pub(crate) fn vi(row: &Row, key: &str) -> Option<i64> {
    row.get(key).and_then(Value::as_i64)
}

pub(crate) fn id_of(row: &Row) -> i64 {
    vi(row, "id").unwrap_or(0)
}

/// A float as a JSON number Python holds as `float`.
pub(crate) fn fnum(x: Option<f64>) -> Value {
    x.and_then(serde_json::Number::from_f64)
        .map_or(Value::Null, Value::Number)
}

pub(crate) fn opt_str(v: Option<&str>) -> Value {
    v.map_or(Value::Null, |s| json!(s))
}

/// Python `==` between two decoded column values: numbers by value.
pub(crate) fn py_eq(a: Option<&Value>, b: Option<&Value>) -> bool {
    let a = a.unwrap_or(&Value::Null);
    let b = b.unwrap_or(&Value::Null);
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x.as_f64() == y.as_f64(),
        _ => a == b,
    }
}

/// `pending_entity_proposals` (3777-3804), the merge rows Python keeps
/// (`p.get("kind") == "merge"`), in its order and key order.
pub(crate) const PENDING_SQL: &str = "SELECT p.id, p.kind, p.entity_id, p.into_id, \
     p.score::text::double precision, p.reason, p.status, p.created_at, \
     p.judge_verdict, p.judge_confidence::text::double precision, p.judge_note, \
     p.judge_model, p.judged_at, p.judge2_verdict, \
     p.judge2_confidence::text::double precision, p.judge2_model, p.judged2_at, \
     p.judge3_verdict, p.judge3_confidence::text::double precision, p.judge3_model, \
     p.judged3_at, p.judge2_served_model, p.judge2_policy, p.judge3_served_model, \
     p.judge3_policy, e.display, i.display \
     FROM entity_proposals p \
     JOIN entities e ON e.id = p.entity_id \
     LEFT JOIN entities i ON i.id = p.into_id \
     WHERE p.status = 'pending' ORDER BY p.kind, p.score DESC NULLS LAST, p.id";

#[derive(Clone, Copy)]
enum Col {
    I,
    OptI,
    F,
    S,
}

fn decode(r: &tokio_postgres::Row, cols: &[(&str, Col)]) -> Row {
    let mut m = Map::new();
    for (i, (name, ty)) in cols.iter().enumerate() {
        let v = match ty {
            Col::I => json!(r.get::<_, i64>(i)),
            Col::OptI => r.get::<_, Option<i64>>(i).map_or(Value::Null, |x| json!(x)),
            Col::F => fnum(r.get::<_, Option<f64>>(i)),
            Col::S => opt_str(r.get::<_, Option<String>>(i).as_deref()),
        };
        m.insert((*name).to_string(), v);
    }
    m
}

const PENDING_COLS: [(&str, Col); 27] = [
    ("id", Col::I),
    ("kind", Col::S),
    ("entity_id", Col::I),
    ("into_id", Col::OptI),
    ("score", Col::F),
    ("reason", Col::S),
    ("status", Col::S),
    ("created_at", Col::F),
    ("judge_verdict", Col::S),
    ("judge_confidence", Col::F),
    ("judge_note", Col::S),
    ("judge_model", Col::S),
    ("judged_at", Col::F),
    ("judge2_verdict", Col::S),
    ("judge2_confidence", Col::F),
    ("judge2_model", Col::S),
    ("judged2_at", Col::F),
    ("judge3_verdict", Col::S),
    ("judge3_confidence", Col::F),
    ("judge3_model", Col::S),
    ("judged3_at", Col::F),
    ("judge2_served_model", Col::S),
    ("judge2_policy", Col::S),
    ("judge3_served_model", Col::S),
    ("judge3_policy", Col::S),
    ("entity", Col::S),
    ("into", Col::S),
];

/// The pending merge rows.
pub(crate) async fn pending_rows(c: &Client) -> Result<Vec<Row>, ToolError> {
    Ok(c.query(PENDING_SQL, &[])
        .await
        .map_err(internal)?
        .iter()
        .map(|r| decode(r, &PENDING_COLS))
        .filter(|row| vs(row, "kind") == Some(KIND))
        .collect())
}

const PROPOSAL_COLS: [(&str, Col); 16] = [
    ("id", Col::I),
    ("kind", Col::S),
    ("entity_id", Col::I),
    ("into_id", Col::OptI),
    ("score", Col::F),
    ("reason", Col::S),
    ("status", Col::S),
    ("created_at", Col::F),
    ("decided_by", Col::S),
    ("decided_at", Col::F),
    ("judge_verdict", Col::S),
    ("judge_confidence", Col::F),
    ("judge2_verdict", Col::S),
    ("judge2_confidence", Col::F),
    ("judge3_verdict", Col::S),
    ("judge3_confidence", Col::F),
];

/// `get_entity_proposal` (3912-3920).
pub(crate) async fn get_entity_proposal(c: &Client, id: i64) -> Result<Option<Row>, ToolError> {
    Ok(c.query_opt(
        "SELECT id, kind, entity_id, into_id, score::text::double precision, reason, status, \
         created_at, decided_by, decided_at, judge_verdict, \
         judge_confidence::text::double precision, judge2_verdict, \
         judge2_confidence::text::double precision, judge3_verdict, \
         judge3_confidence::text::double precision \
         FROM entity_proposals WHERE id = $1",
        &[&id],
    )
    .await
    .map_err(internal)?
    .map(|r| decode(&r, &PROPOSAL_COLS)))
}

/// A row as the enrichment reads it.
pub(crate) fn entity_proposal(row: &Row) -> EntityProposal {
    let s = |k: &str| vs(row, k).map(str::to_string);
    EntityProposal {
        id: id_of(row),
        kind: s("kind").unwrap_or_default(),
        entity_id: vi(row, "entity_id"),
        into_id: vi(row, "into_id"),
        score: vf(row, "score"),
        reason: s("reason"),
        judge_verdict: s("judge_verdict"),
        judge_confidence: vf(row, "judge_confidence"),
        judge_note: s("judge_note"),
        judge_model: s("judge_model"),
        judge2_verdict: s("judge2_verdict"),
        judge2_confidence: vf(row, "judge2_confidence"),
        judge2_model: s("judge2_model"),
        judge3_verdict: s("judge3_verdict"),
        judge3_confidence: vf(row, "judge3_confidence"),
        judge3_model: s("judge3_model"),
        entity: s("entity"),
        into: s("into"),
    }
}

// ── meta ─────────────────────────────────────────────────────────────────

pub(crate) async fn get_meta(c: &Client, key: &str) -> Result<Option<Value>, ToolError> {
    Ok(
        c.query_opt("SELECT value FROM meta WHERE key = $1", &[&key])
            .await
            .map_err(internal)?
            .and_then(|r| r.get::<_, Option<Value>>(0)),
    )
}

/// `get_meta(key) or {}` as a dict.
async fn get_meta_map(c: &Client, key: &str) -> Result<Map<String, Value>, ToolError> {
    Ok(match get_meta(c, key).await? {
        Some(Value::Object(m)) => m,
        _ => Map::new(),
    })
}

pub(crate) async fn set_meta(c: &Client, key: &str, value: &Value) -> Result<(), ToolError> {
    if let Some(text) = lone_surrogate_json(value) {
        // `json.dumps` escapes a lone surrogate (`\ud83d`), which PostgreSQL's
        // jsonb input refuses: the statement fails there, as Python's does.
        c.execute(
            "INSERT INTO meta (key, value) VALUES ($1, $2::text::jsonb) \
             ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            &[&key, &text],
        )
        .await
        .map_err(|e| internal(psycopg_text(&e)))?;
        return Ok(());
    }
    c.execute(
        "INSERT INTO meta (key, value) VALUES ($1, $2::jsonb) \
         ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        &[&key, value],
    )
    .await
    .map_err(internal)?;
    Ok(())
}

/// A server error as psycopg's `str(exc)` lays it out: the primary message,
/// then its DETAIL, HINT and CONTEXT lines (psycopg's CONTEXT also names the
/// statement's parameter, which tokio-postgres does not report).
fn psycopg_text(e: &tokio_postgres::Error) -> String {
    let Some(db) = e.as_db_error() else {
        return e.to_string();
    };
    let mut text = db.message().to_string();
    for (name, part) in [
        ("DETAIL", db.detail()),
        ("HINT", db.hint()),
        ("CONTEXT", db.where_()),
    ] {
        if let Some(part) = part {
            text.push_str(&format!("\n{name}:  {part}"));
        }
    }
    text
}

/// `json.dumps(value)` when a string in it holds a reply's lone-surrogate
/// stand-in ([`crate::mcp::py_loads::loads_reply`]), with each stand-in
/// escaped as the lone surrogate Python escapes; None when none does.
fn lone_surrogate_json(value: &Value) -> Option<String> {
    use crate::dream::extractor::lone_surrogate;
    fn stand_ins(v: &Value, out: &mut Vec<char>) {
        match v {
            Value::String(s) => out.extend(s.chars().filter(|c| lone_surrogate(*c).is_some())),
            Value::Array(items) => items.iter().for_each(|x| stand_ins(x, out)),
            Value::Object(m) => m.iter().for_each(|(k, x)| {
                out.extend(k.chars().filter(|c| lone_surrogate(*c).is_some()));
                stand_ins(x, out);
            }),
            _ => {}
        }
    }
    let mut found = Vec::new();
    stand_ins(value, &mut found);
    if found.is_empty() || crate::mutants::active("mcp-dream-judge-encode-unchecked") {
        return None;
    }
    found.sort_unstable();
    found.dedup();
    let mut text = crate::pyjson::dumps(value);
    for c in found {
        let mut buf = [0u16; 2];
        let pair: String = c
            .encode_utf16(&mut buf)
            .iter()
            .map(|u| format!("\\u{u:04x}"))
            .collect();
        let lone = format!("\\u{:04x}", lone_surrogate(c).unwrap_or(0));
        text = text.replace(&pair, &lone);
    }
    Some(text)
}

// ── fingerprints and the judging policy ──────────────────────────────────

/// `fingerprint(value)`: the SHA-256 of `json.dumps(value, sort_keys=True,
/// separators=(",", ":"))`.
pub(crate) fn fingerprint(v: &Value) -> String {
    let text = crate::pyjson::dumps_sorted_compact(v);
    hex::encode(Sha256::digest(text.as_bytes()))
}

/// `_SIGNED_WHEN_SET`: knobs signed only while set (their unset values).
fn unset(key: &str, v: &Value) -> Option<bool> {
    let empty_or_none = matches!(v, Value::Null) || v.as_str() == Some("");
    match key {
        "judge_second_url" | "judge_third_model" | "judge_third_url" => Some(empty_or_none),
        "link_two_vote_min_confidence" => {
            Some(v.is_number() && !v.is_boolean() && v.as_f64() == Some(0.7))
        }
        _ => None,
    }
}

/// `signed_deep_dream`: the deep_dream config as review fingerprints sign
/// it, without the v56 mode knobs and the post-2026-09-14 knobs at their
/// unset values.
pub(crate) fn signed_deep_dream(cfg: &DeepDreamConfig) -> Map<String, Value> {
    let mut view = cfg.as_dict.clone();
    let drop: Vec<String> = view
        .iter()
        .filter(|(k, v)| unset(k, v) == Some(true))
        .map(|(k, _)| k.clone())
        .collect();
    for k in drop {
        view.shift_remove(&k);
    }
    for k in ["link_judge_two_vote", "judge_tiebreak_mode"] {
        view.shift_remove(k);
    }
    view
}

/// `judging_policy(service, extractor, prompt)`.
pub(crate) fn judging_policy(config: &Config, ex: &Compat, prompt: &str) -> Value {
    json!({
        "version": 1,
        "prompt": prompt,
        "deep_dream": signed_deep_dream(&config.memory.deep_dream),
        "model": ex.model,
        "endpoint": ex.base_url,
        "max_tokens": ex.max_tokens,
        "extra_body": ex.extra_body,
        "thinking": false,
        "override": config.dream.extractor_model_override,
        "effort": config.dream.extractor_reasoning_effort,
    })
}

/// `_evidence(value)`: dicts without `n`, `id` and every `judge*` key.
pub(crate) fn evidence(v: &Value) -> Value {
    match v {
        Value::Object(m) => Value::Object(
            m.iter()
                .filter(|(k, _)| k.as_str() != "n" && k.as_str() != "id" && !k.starts_with("judge"))
                .map(|(k, x)| (k.clone(), evidence(x)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(evidence).collect()),
        other => other.clone(),
    }
}

/// `observed_model(extractor)`: a launch-default alias asks the endpoint
/// (`fetch_served_model`); any other name reports its last served model.
pub(crate) async fn observed_model(ex: &Compat, served: Option<&str>) -> Option<String> {
    if !ex.base_url.is_empty()
        && matches!(ex.model.as_str(), "extractor" | "bench" | "judge")
        && !crate::mutants::active("mcp-dream-judge-alias-unprobed")
    {
        return crate::dream::extractor::fetch_served_model(&ex.base_url).await;
    }
    served.map(str::to_string)
}

/// `record_response_identity`.
pub(crate) async fn record_response_identity(
    c: &Client,
    queue: &str,
    policy: &str,
    model: Option<&str>,
) -> Result<(), ToolError> {
    if let Some(m) = model.filter(|m| !crate::storage::py_strip(m).is_empty()) {
        set_meta(
            c,
            &format!("{IDENTITY_PREFIX}{queue}"),
            &json!({"policy": policy, "model": m}),
        )
        .await?;
    }
    Ok(())
}

/// `last_response_identity`.
pub(crate) async fn last_response_identity(
    c: &Client,
    queue: &str,
    policy: &str,
) -> Result<Option<String>, ToolError> {
    let saved = get_meta_map(c, &format!("{IDENTITY_PREFIX}{queue}")).await?;
    Ok(
        if saved.get("policy").and_then(Value::as_str) == Some(policy) {
            saved
                .get("model")
                .and_then(Value::as_str)
                .map(str::to_string)
        } else {
            None
        },
    )
}

/// `vote_endpoint_moved(service, "merge", row, opinion, cache)`.
pub(crate) async fn vote_endpoint_moved(
    c: &Client,
    row: &Row,
    opinion: &str,
    cache: Option<&mut HashMap<(String, String), Option<String>>>,
) -> Result<bool, ToolError> {
    let n = if opinion == "second" { 2 } else { 3 };
    let Some(policy) = vs(row, &format!("judge{n}_policy")).filter(|p| !p.is_empty()) else {
        return Ok(false);
    };
    let key = (opinion.to_string(), policy.to_string());
    let queue = format!("{KIND}_{opinion}");
    let now = match cache {
        Some(cache) => {
            if let Some(v) = cache.get(&key) {
                v.clone()
            } else {
                let v = last_response_identity(c, &queue, policy).await?;
                cache.insert(key, v.clone());
                v
            }
        }
        None => last_response_identity(c, &queue, policy).await?,
    };
    let stored = vs(row, &format!("judge{n}_served_model")).filter(|s| !s.is_empty());
    Ok(matches!((now.as_deref(), stored), (Some(a), Some(b)) if !a.is_empty() && a != b))
}

/// `_without_tail(note, marker)`.
fn without_tail(note: Option<&str>, marker: &str) -> String {
    let note = note.unwrap_or("");
    match note.find(&format!(" {marker}")) {
        Some(cut) => note[..cut].to_string(),
        None => note.to_string(),
    }
}

// ── signing ──────────────────────────────────────────────────────────────

/// `_sign` (pure): `(fingerprints, packs)` by proposal id.
#[allow(clippy::type_complexity, clippy::result_large_err)]
pub(crate) fn sign_rows(
    rows: &[Row],
    ev: &JudgeEvidence,
    cfg: &DeepDreamConfig,
    policy: &Value,
) -> Result<(HashMap<String, String>, HashMap<String, Row>), ToolError> {
    let props: Vec<EntityProposal> = rows.iter().map(entity_proposal).collect();
    let enriched = deep::judge_enrich_from(&props, ev, cfg)?;
    let mut sigs = HashMap::new();
    let mut packs = HashMap::new();
    for (p, row) in rows.iter().zip(enriched) {
        let Value::Object(pack) = row else { continue };
        let key = id_of(p).to_string();
        let mut signed = pack.clone();
        if signed.contains_key("group") {
            signed.insert("group".into(), Value::Null);
        }
        let sig = fingerprint(&json!({
            "proposal": evidence(&Value::Object(p.clone())),
            "evidence": evidence(&Value::Object(signed)),
            "policy": policy,
        }));
        sigs.insert(key.clone(), sig);
        packs.insert(key, pack);
    }
    Ok((sigs, packs))
}

/// `ReviewJudgments(service, "merge", extractor, pending, current_model)`:
/// one judge tick's state.
pub(crate) struct Review {
    pub cfg: DeepDreamConfig,
    pub pending: Vec<Row>,
    pub current_model: Option<String>,
    pub request_policy: String,
    pub policy: Value,
    pub expected: HashMap<String, String>,
    pub enriched: HashMap<String, Row>,
    to_sign: Vec<Row>,
    evidence: Option<JudgeEvidence>,
    pub valid: HashSet<i64>,
    pub second_policy: Option<String>,
    pub second_model: Option<String>,
    pub third_policy: Option<String>,
    pub third_model: Option<String>,
}

impl Review {
    pub(crate) fn new(
        config: &Config,
        ex: &Compat,
        pending: Vec<Row>,
        current_model: Option<String>,
    ) -> Self {
        let policy = judging_policy(config, ex, crate::dream::prompts::JUDGE_SYSTEM_PROMPT);
        Review {
            cfg: config.memory.deep_dream.clone(),
            pending,
            current_model,
            request_policy: fingerprint(&policy),
            policy,
            expected: HashMap::new(),
            enriched: HashMap::new(),
            to_sign: Vec::new(),
            evidence: None,
            valid: HashSet::new(),
            second_policy: None,
            second_model: None,
            third_policy: None,
            third_model: None,
        }
    }

    /// `observed(proposal)`: the served model recorded with its verdict.
    pub(crate) async fn observed(
        &self,
        c: &Client,
        row: &Row,
    ) -> Result<Option<String>, ToolError> {
        let memo = get_meta_map(c, MEMO_KEY).await?;
        Ok(match memo.get(&id_of(row).to_string()) {
            Some(Value::Object(saved)) => saved
                .get("served_model")
                .and_then(Value::as_str)
                .map(str::to_string),
            _ => None,
        })
    }

    /// `signatures(pending)`: the evidence is read here (caller holds the
    /// lock).
    pub(crate) async fn signatures(
        &self,
        c: &Client,
        rows: &[Row],
    ) -> Result<HashMap<String, String>, ToolError> {
        if rows.is_empty() {
            return Ok(HashMap::new());
        }
        let ev = deep::judge_evidence_body(c).await.map_err(internal)?;
        Ok(sign_rows(rows, &ev, &self.cfg, &self.policy)?.0)
    }

    /// `prepare(limit)`: read the evidence for the verdict rows and the
    /// first `limit` unjudged rows.
    pub(crate) async fn prepare(&mut self, c: &Client, limit: i64) -> Result<(), ToolError> {
        let cap = limit.max(0) as usize;
        let unjudged: HashSet<i64> = self
            .pending
            .iter()
            .filter(|p| !truthy(p, "judge_verdict"))
            .take(cap)
            .map(id_of)
            .collect();
        self.to_sign = self
            .pending
            .iter()
            .filter(|p| truthy(p, "judge_verdict") || unjudged.contains(&id_of(p)))
            .cloned()
            .collect();
        self.evidence = if self.to_sign.is_empty() {
            None
        } else {
            Some(deep::judge_evidence_body(c).await.map_err(internal)?)
        };
        Ok(())
    }

    /// `sign()`, lock released, on a blocking thread.
    #[allow(clippy::result_large_err)]
    pub(crate) async fn sign(&mut self) -> Result<(), ToolError> {
        let rows = std::mem::take(&mut self.to_sign);
        let ev = self.evidence.take();
        match ev {
            Some(ev) if !rows.is_empty() => {
                let cfg = self.cfg.clone();
                let policy = self.policy.clone();
                let (sigs, packs) =
                    tokio::task::spawn_blocking(move || sign_rows(&rows, &ev, &cfg, &policy))
                        .await
                        .map_err(internal)??;
                self.expected = sigs;
                self.enriched = packs;
            }
            _ => {
                self.expected = HashMap::new();
                self.enriched = HashMap::new();
            }
        }
        Ok(())
    }

    /// `rows(batch)`: the signed packs, numbered in batch order.
    pub(crate) fn rows(&self, batch: &[Row]) -> Vec<Row> {
        batch
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let mut pack = self
                    .enriched
                    .get(&id_of(p).to_string())
                    .cloned()
                    .unwrap_or_default();
                pack.insert("n".into(), json!(i + 1));
                pack
            })
            .collect()
    }

    /// `refresh()`: clear every verdict whose evidence, policy or served
    /// model moved, then each later vote whose endpoint moved. One
    /// transaction (the caller's).
    pub(crate) async fn refresh(
        &mut self,
        c: &Client,
        ex_served: Option<&str>,
    ) -> Result<(), ToolError> {
        let mut memo = get_meta_map(c, MEMO_KEY).await?;
        let live: HashSet<String> = self.pending.iter().map(|p| id_of(p).to_string()).collect();
        memo.retain(|k, _| live.contains(k));
        let observations = [
            self.current_model.clone(),
            ex_served.map(str::to_string),
            last_response_identity(c, KIND, &self.request_policy).await?,
        ];
        let no_clear = crate::mutants::active("mcp-dream-judge-no-stale-clear");
        for p in self.pending.iter_mut() {
            if !truthy(p, "judge_verdict") || no_clear {
                continue;
            }
            let key = id_of(p).to_string();
            let matches = match memo.get(&key) {
                Some(Value::Object(saved)) => {
                    saved.get("fingerprint").and_then(Value::as_str)
                        == self.expected.get(&key).map(String::as_str)
                        && observations.iter().all(|m| match m.as_deref() {
                            None | Some("") => true,
                            Some(m) => saved.get("served_model").and_then(Value::as_str) == Some(m),
                        })
                }
                _ => false,
            };
            if matches {
                continue;
            }
            c.execute(
                "UPDATE entity_proposals SET judge_verdict=NULL, \
                 judge_confidence=NULL, judge_note=NULL, judge_model=NULL, \
                 judged_at=NULL, judge_served_model=NULL, \
                 judge2_verdict=NULL, judge2_confidence=NULL, \
                 judge2_model=NULL, judged2_at=NULL, judge3_verdict=NULL, \
                 judge3_confidence=NULL, judge3_model=NULL, judged3_at=NULL, \
                 judge2_served_model=NULL, judge2_policy=NULL, \
                 judge3_served_model=NULL, judge3_policy=NULL \
                 WHERE id=$1 AND status='pending'",
                &[&id_of(p)],
            )
            .await
            .map_err(internal)?;
            for (k, v) in p.iter_mut() {
                if k.starts_with("judge") {
                    *v = Value::Null;
                }
            }
            memo.shift_remove(&key);
        }
        set_meta(c, MEMO_KEY, &Value::Object(memo)).await?;
        self.clear_moved_votes(c).await
    }

    /// `_clear_moved_votes`.
    async fn clear_moved_votes(&mut self, c: &Client) -> Result<(), ToolError> {
        let mut cache = HashMap::new();
        for p in self.pending.iter_mut() {
            if !truthy(p, "judge_verdict") || !truthy(p, "judge2_verdict") {
                continue;
            }
            if crate::mutants::active("mcp-dream-judge-moved-not-cleared") {
                continue;
            }
            if vote_endpoint_moved(c, p, "second", Some(&mut cache)).await? {
                let note = without_tail(vs(p, "judge_note"), "| 2nd (");
                c.execute(
                    "UPDATE entity_proposals SET judge2_verdict=NULL, \
                     judge2_confidence=NULL, judge2_model=NULL, judged2_at=NULL, \
                     judge2_served_model=NULL, judge2_policy=NULL, \
                     judge3_verdict=NULL, judge3_confidence=NULL, \
                     judge3_model=NULL, judged3_at=NULL, \
                     judge3_served_model=NULL, judge3_policy=NULL, judge_note=$1 \
                     WHERE id=$2 AND status='pending'",
                    &[&note, &id_of(p)],
                )
                .await
                .map_err(internal)?;
                for (k, v) in p.iter_mut() {
                    if ["judge2", "judged2", "judge3", "judged3"]
                        .iter()
                        .any(|pre| k.starts_with(pre))
                    {
                        *v = Value::Null;
                    }
                }
                p.insert("judge_note".into(), json!(note));
            } else if truthy(p, "judge3_verdict")
                && vote_endpoint_moved(c, p, "third", Some(&mut cache)).await?
            {
                let note = without_tail(vs(p, "judge_note"), "| 3rd (");
                c.execute(
                    "UPDATE entity_proposals SET judge3_verdict=NULL, \
                     judge3_confidence=NULL, judge3_model=NULL, judged3_at=NULL, \
                     judge3_served_model=NULL, judge3_policy=NULL, judge_note=$1 \
                     WHERE id=$2 AND status='pending'",
                    &[&note, &id_of(p)],
                )
                .await
                .map_err(internal)?;
                for (k, v) in p.iter_mut() {
                    if k.starts_with("judge3") || k.starts_with("judged3") {
                        *v = Value::Null;
                    }
                }
                p.insert("judge_note".into(), json!(note));
            }
        }
        Ok(())
    }

    /// `validate(batch, response_extractor=, expected_model=, opinion=)`:
    /// `response` is the second or third opinion's endpoint (None: the
    /// first's), `served` the responding endpoint's served model.
    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn validate(
        &mut self,
        c: &Client,
        config: &Config,
        batch: &[Row],
        served: Option<&str>,
        response: Option<&Compat>,
        expected_model: Option<&str>,
        opinion: &str,
    ) -> Result<(), ToolError> {
        let response_policy = match response {
            Some(r) => fingerprint(&judging_policy(
                config,
                r,
                crate::dream::prompts::JUDGE_SYSTEM_PROMPT,
            )),
            None => self.request_policy.clone(),
        };
        let queue = match response {
            Some(_) => format!("{KIND}_{opinion}"),
            None => KIND.to_string(),
        };
        record_response_identity(c, &queue, &response_policy, served).await?;
        self.second_policy = None;
        self.second_model = None;
        self.third_policy = None;
        self.third_model = None;
        if response.is_some() {
            if opinion == "third" {
                self.third_policy = Some(response_policy);
                self.third_model = served.map(str::to_string);
            } else {
                self.second_policy = Some(response_policy);
                self.second_model = served.map(str::to_string);
            }
        }
        let expected = match response {
            None => self.current_model.clone(),
            Some(_) => expected_model.map(str::to_string),
        };
        if let Some(want) = expected.filter(|m| !m.is_empty())
            && served != Some(want.as_str())
        {
            self.valid.clear();
            return Ok(());
        }
        self.revalidate(c, batch).await
    }

    /// `validate_recorded(batch)`: the evidence check alone.
    pub(crate) async fn validate_recorded(
        &mut self,
        c: &Client,
        batch: &[Row],
    ) -> Result<(), ToolError> {
        self.second_policy = None;
        self.second_model = None;
        self.third_policy = None;
        self.third_model = None;
        self.revalidate(c, batch).await
    }

    async fn revalidate(&mut self, c: &Client, batch: &[Row]) -> Result<(), ToolError> {
        let ids: HashSet<i64> = batch.iter().map(id_of).collect();
        let live: Vec<Row> = pending_rows(c)
            .await?
            .into_iter()
            .filter(|p| ids.contains(&id_of(p)))
            .collect();
        let sigs = self.signatures(c, &live).await?;
        self.valid = live
            .iter()
            .filter(|p| {
                let key = id_of(p).to_string();
                sigs.contains_key(&key) && sigs.get(&key) == self.expected.get(&key)
            })
            .map(id_of)
            .collect();
        Ok(())
    }

    /// `current(proposal, first_opinion=, second_opinion=)`.
    pub(crate) async fn current(
        &self,
        c: &Client,
        row: &Row,
        first_opinion: bool,
        second_opinion: bool,
    ) -> Result<bool, ToolError> {
        if !self.valid.contains(&id_of(row)) {
            return Ok(false);
        }
        let Some(live) = get_entity_proposal(c, id_of(row)).await? else {
            return Ok(false);
        };
        if vs(&live, "status") != Some("pending") {
            return Ok(false);
        }
        let same = |keys: [&str; 2]| keys.iter().all(|k| py_eq(live.get(*k), row.get(*k)));
        if first_opinion && !same(["judge_verdict", "judge_confidence"]) {
            return Ok(false);
        }
        if second_opinion && !same(["judge2_verdict", "judge2_confidence"]) {
            return Ok(false);
        }
        Ok(true)
    }

    /// `record(proposal)`.
    pub(crate) async fn record(
        &self,
        c: &Client,
        row: &Row,
        served: Option<&str>,
    ) -> Result<(), ToolError> {
        let mut memo = get_meta_map(c, MEMO_KEY).await?;
        let key = id_of(row).to_string();
        memo.insert(
            key.clone(),
            json!({"fingerprint": self.expected.get(&key), "served_model": served}),
        );
        set_meta(c, MEMO_KEY, &Value::Object(memo)).await
    }

    /// `retry(proposal)`.
    pub(crate) async fn retry(&self, c: &Client, row: &Row) -> Result<(), ToolError> {
        let mut memo = get_meta_map(c, MEMO_KEY).await?;
        memo.shift_remove(&id_of(row).to_string());
        set_meta(c, MEMO_KEY, &Value::Object(memo)).await
    }
}

// ── review_decisions.py: the automatic reject records ────────────────────

/// `_index(storage)`.
struct DIndex {
    next_seq: i64,
    cursors: Map<String, Value>,
    active: Map<String, Value>,
    recent_ids: Vec<Value>,
}

impl DIndex {
    async fn load(c: &Client) -> Result<Self, ToolError> {
        let fresh = DIndex {
            next_seq: 1,
            cursors: Map::new(),
            active: Map::new(),
            recent_ids: Vec::new(),
        };
        let Some(Value::Object(m)) = get_meta(c, INDEX_KEY).await? else {
            return Ok(fresh);
        };
        if m.get("version").and_then(Value::as_i64) != Some(1)
            && m.get("version").and_then(Value::as_f64) != Some(1.0)
        {
            return Ok(fresh);
        }
        let dict = |k: &str| match m.get(k) {
            Some(Value::Object(d)) => d.clone(),
            _ => Map::new(),
        };
        let mut recent: Vec<Value> = match m.get("recent_ids") {
            Some(Value::Array(a)) => a.clone(),
            _ => Vec::new(),
        };
        let skip = recent.len().saturating_sub(RECENT_CAP);
        recent.drain(..skip);
        Ok(DIndex {
            next_seq: m
                .get("next_seq")
                .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|x| x as i64)))
                .unwrap_or(1)
                .max(1),
            cursors: dict("cursors"),
            active: dict("active"),
            recent_ids: recent,
        })
    }

    fn value(&self) -> Value {
        json!({"version": 1, "next_seq": self.next_seq, "cursors": self.cursors,
               "active": self.active, "recent_ids": self.recent_ids})
    }

    fn touch_recent(&mut self, decision_id: &Value) {
        self.recent_ids.retain(|v| v != decision_id);
        self.recent_ids.push(decision_id.clone());
        let skip = self.recent_ids.len().saturating_sub(RECENT_CAP);
        self.recent_ids.drain(..skip);
    }
}

fn seq_of(summary: &Value) -> i64 {
    summary
        .get("seq")
        .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|x| x as i64)))
        .unwrap_or(0)
}

/// `decision_record(storage, decision_id)`.
async fn decision_record(
    c: &Client,
    decision_id: &str,
) -> Result<Option<Map<String, Value>>, ToolError> {
    Ok(
        match get_meta(c, &format!("{RECORD_PREFIX}{decision_id}")).await? {
            Some(Value::Object(m)) => Some(m),
            _ => None,
        },
    )
}

/// `_retire`.
async fn retire(
    c: &Client,
    index: &mut DIndex,
    target: &str,
    summary: &Value,
    state: &str,
    reason: &str,
) -> Result<(), ToolError> {
    let decision_id = summary.get("decision_id").cloned().unwrap_or(Value::Null);
    let id_text = decision_id.as_str().unwrap_or_default().to_string();
    let mut marker = decision_record(c, &id_text).await?.unwrap_or_default();
    marker.insert("state".into(), json!(state));
    marker.insert("ended_at".into(), json!(now()));
    marker.insert("end_reason".into(), json!(reason));
    set_meta(
        c,
        &format!("{RECORD_PREFIX}{id_text}"),
        &Value::Object(marker),
    )
    .await?;
    index.touch_recent(&decision_id);
    if index.active.get(target).and_then(|s| s.get("decision_id")) == Some(&decision_id) {
        index.active.shift_remove(target);
    }
    Ok(())
}

/// `has_active_decisions(storage, "merge")`.
pub(crate) async fn has_active_decisions(c: &Client) -> Result<bool, ToolError> {
    let index = DIndex::load(c).await?;
    Ok(index
        .active
        .values()
        .any(|row| row.get("queue").and_then(Value::as_str) == Some(KIND)))
}

/// `_canonical_pair(storage, "merge", proposal)`.
async fn canonical_pair(c: &Client, row: &Row) -> Result<Option<[String; 2]>, ToolError> {
    let g = crate::storage::graph::load_graph_body(c)
        .await
        .map_err(internal)?;
    let canon = |id: Option<i64>| -> Option<String> {
        let id = id?;
        g["entities"]
            .as_array()?
            .iter()
            .find(|e| e["id"].as_i64() == Some(id))
            .and_then(|e| e["canonical"].as_str())
            .map(str::to_string)
    };
    let (left, right) = (canon(vi(row, "entity_id")), canon(vi(row, "into_id")));
    Ok(match (left, right) {
        (Some(l), Some(r)) if !l.is_empty() && !r.is_empty() && l != r => {
            let mut pair = [l, r];
            pair.sort();
            Some(pair)
        }
        _ => None,
    })
}

/// `_dismissed_at(storage, pair)`.
async fn dismissed_at(c: &Client, pair: Option<&[String; 2]>) -> Result<Option<f64>, ToolError> {
    let Some([a, b]) = pair else {
        return Ok(None);
    };
    Ok(c.query_opt(
        "SELECT dismissed_at FROM dismissed_pairs WHERE a_norm=$1 AND b_norm=$2",
        &[a, b],
    )
    .await
    .map_err(internal)?
    .map(|r| r.get::<_, f64>(0)))
}

/// `capture_proposal_pair(review, proposal)`.
pub(crate) struct PriorPair {
    pair: Option<[String; 2]>,
    dismissed_at: Option<f64>,
}

pub(crate) async fn capture_proposal_pair(c: &Client, row: &Row) -> Result<PriorPair, ToolError> {
    let pair = canonical_pair(c, row).await?;
    let at = dismissed_at(c, pair.as_ref()).await?;
    Ok(PriorPair {
        pair,
        dismissed_at: at,
    })
}

/// `_write_new(storage, target, marker)` in the caller's transaction.
async fn write_new(c: &Client, target: &str, marker: Map<String, Value>) -> Result<(), ToolError> {
    let mut index = DIndex::load(c).await?;
    if let Some(previous) = index.active.get(target).filter(|v| v.is_object()).cloned() {
        retire(
            c,
            &mut index,
            target,
            &previous,
            "superseded",
            "new automatic decision",
        )
        .await?;
    }
    let seq = index.next_seq;
    index.next_seq = seq + 1;
    let decision_id = json!(seq.to_string());
    let mut record = Map::new();
    record.insert("version".into(), json!(1));
    record.insert("decision_id".into(), decision_id.clone());
    record.insert("seq".into(), json!(seq));
    record.insert("target".into(), json!(target));
    record.insert("state".into(), json!("active"));
    record.insert("recorded_at".into(), json!(now()));
    for (k, v) in marker {
        record.insert(k, v);
    }
    let summary = json!({"decision_id": decision_id, "seq": seq,
                         "queue": record["queue"], "pair": record.get("pair")});
    index.active.insert(target.to_string(), summary);
    index.touch_recent(&decision_id);
    set_meta(c, &format!("{RECORD_PREFIX}{seq}"), &Value::Object(record)).await?;
    set_meta(c, INDEX_KEY, &index.value()).await
}

/// `record_proposal_terminal(review, proposal, action="reject",
/// prior_pair=...)`: record one successful dream-judge merge reject.
pub(crate) async fn record_proposal_terminal(
    c: &Client,
    review: &Review,
    row: &Row,
    prior: &PriorPair,
    extractor_served: Option<&str>,
) -> Result<(), ToolError> {
    let id = id_of(row);
    let Some(current) = get_entity_proposal(c, id).await? else {
        return Ok(());
    };
    if vs(&current, "status") != Some("rejected")
        || vs(&current, "decided_by") != Some("dream-judge")
    {
        return Ok(());
    }
    let Some(signature) = review
        .expected
        .get(&id.to_string())
        .filter(|s| !s.is_empty())
    else {
        return Ok(());
    };
    let pair = canonical_pair(c, row).await?;
    if prior.pair != pair || prior.dismissed_at.is_some() {
        return Ok(());
    }
    let pair_at = dismissed_at(c, pair.as_ref()).await?;
    if pair.is_some() && pair_at.is_none() {
        return Ok(());
    }
    let served = review
        .observed(c, row)
        .await?
        .filter(|s| !s.is_empty())
        .or_else(|| review.current_model.clone().filter(|s| !s.is_empty()))
        .or_else(|| extractor_served.map(str::to_string));
    let mut marker = Map::new();
    marker.insert("queue".into(), json!(KIND));
    marker.insert("target_type".into(), json!("proposal"));
    marker.insert("proposal_id".into(), json!(id));
    marker.insert("action".into(), json!("reject"));
    marker.insert("proposal".into(), Value::Object(row.clone()));
    marker.insert("fingerprint".into(), json!(signature));
    marker.insert("served_model".into(), json!(served));
    marker.insert("decided_by".into(), current["decided_by"].clone());
    marker.insert("second_policy".into(), json!(review.second_policy));
    marker.insert("second_model".into(), json!(review.second_model));
    marker.insert("third_policy".into(), json!(review.third_policy));
    marker.insert("third_model".into(), json!(review.third_model));
    marker.insert("decided_at".into(), current["decided_at"].clone());
    marker.insert("terminal_status".into(), current["status"].clone());
    marker.insert("pair".into(), json!(pair));
    marker.insert("dismissed_at".into(), fnum(pair_at));
    write_new(c, &format!("{KIND}:{id}"), marker).await
}

/// `_batch(index, "merge", limit)`.
fn batch(index: &DIndex, limit: i64) -> Vec<(String, Value)> {
    let mut rows: Vec<(String, Value)> = index
        .active
        .iter()
        .filter(|(_, s)| s.get("queue").and_then(Value::as_str) == Some(KIND))
        .map(|(t, s)| (t.clone(), s.clone()))
        .collect();
    rows.sort_by_key(|(_, s)| seq_of(s));
    let cursor = index
        .cursors
        .get(KIND)
        .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|x| x as i64)))
        .unwrap_or(0);
    let (after, before): (Vec<_>, Vec<_>) = rows.into_iter().partition(|(_, s)| seq_of(s) > cursor);
    after
        .into_iter()
        .chain(before)
        .take(limit.max(1) as usize)
        .collect()
}

/// `_proposal_projection(review, marker, display)`.
async fn proposal_projection(
    c: &Client,
    marker: &Map<String, Value>,
    display: &HashMap<i64, Value>,
) -> Result<(Option<Row>, Option<Row>), ToolError> {
    let pid = marker
        .get("proposal_id")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let Some(current) = get_entity_proposal(c, pid).await? else {
        return Ok((None, None));
    };
    let mut projection = match marker.get("proposal") {
        Some(Value::Object(m)) => m.clone(),
        _ => Map::new(),
    };
    let keys: Vec<String> = projection.keys().cloned().collect();
    for k in keys {
        if current.contains_key(&k)
            && !k.starts_with("judge")
            && !matches!(k.as_str(), "status" | "decided_by" | "decided_at")
        {
            projection.insert(k.clone(), current[&k].clone());
        }
    }
    if projection.contains_key("status") {
        projection.insert("status".into(), json!("pending"));
    }
    if projection.contains_key("decided_by") {
        projection.insert("decided_by".into(), Value::Null);
    }
    if projection.contains_key("decided_at") {
        projection.insert("decided_at".into(), Value::Null);
    }
    let disp = |k: &str| {
        vi(&projection, k)
            .and_then(|id| display.get(&id).cloned())
            .unwrap_or(Value::Null)
    };
    let (entity, into) = (disp("entity_id"), disp("into_id"));
    projection.insert("entity".into(), entity);
    projection.insert("into".into(), into);
    Ok((Some(current), Some(projection)))
}

/// `_owns_terminal(storage, marker, current)`.
async fn owns_terminal(
    c: &Client,
    marker: &Map<String, Value>,
    current: Option<&Row>,
) -> Result<bool, ToolError> {
    let Some(current) = current else {
        return Ok(false);
    };
    if !py_eq(current.get("status"), marker.get("terminal_status"))
        || !py_eq(current.get("decided_by"), marker.get("decided_by"))
        || !py_eq(current.get("decided_at"), marker.get("decided_at"))
    {
        return Ok(false);
    }
    let pair = marker_pair(marker);
    let Some(pair) = pair else {
        return Ok(true);
    };
    let at = dismissed_at(c, Some(&pair)).await?;
    Ok(py_eq(Some(&fnum(at)), marker.get("dismissed_at")))
}

fn marker_pair(marker: &Map<String, Value>) -> Option<[String; 2]> {
    match marker.get("pair") {
        Some(Value::Array(p)) if p.len() == 2 => Some([
            p[0].as_str().unwrap_or_default().to_string(),
            p[1].as_str().unwrap_or_default().to_string(),
        ]),
        _ => None,
    }
}

/// `_reopen_proposal(storage, marker)` in the caller's transaction:
/// `Err` is the RuntimeError of a dismissal whose ownership changed.
async fn reopen_proposal(c: &Client, marker: &Map<String, Value>) -> Result<bool, ToolError> {
    let pid = marker
        .get("proposal_id")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let current = get_entity_proposal(c, pid).await?;
    if !owns_terminal(c, marker, current.as_ref()).await? {
        return Ok(false);
    }
    let status = marker
        .get("terminal_status")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let decided_by = marker.get("decided_by").and_then(Value::as_str);
    let decided_at = marker.get("decided_at").and_then(Value::as_f64);
    let n = c
        .execute(
            "UPDATE entity_proposals SET status='pending', decided_by=NULL, decided_at=NULL, \
             judge_verdict=NULL, judge_confidence=NULL, judge_note=NULL, \
             judge_model=NULL, judged_at=NULL, judge_served_model=NULL, \
             judge2_verdict=NULL, judge2_confidence=NULL, \
             judge2_model=NULL, judged2_at=NULL, \
             judge2_served_model=NULL, judge2_policy=NULL, \
             judge3_verdict=NULL, judge3_confidence=NULL, \
             judge3_model=NULL, judged3_at=NULL, \
             judge3_served_model=NULL, judge3_policy=NULL \
             WHERE id=$1 AND status=$2 AND decided_by=$3 AND decided_at=$4",
            &[&pid, &status, &decided_by, &decided_at],
        )
        .await
        .map_err(internal)?;
    if n != 1 {
        return Ok(false);
    }
    if let Some([a, b]) = marker_pair(marker) {
        let at = marker.get("dismissed_at").and_then(Value::as_f64);
        let removed = c
            .execute(
                "DELETE FROM dismissed_pairs WHERE a_norm=$1 AND b_norm=$2 AND dismissed_at=$3",
                &[&a, &b, &at],
            )
            .await
            .map_err(internal)?;
        if removed != 1 {
            return Err(internal("automatic dismissal ownership changed"));
        }
    }
    Ok(true)
}

/// `refresh_proposal_terminals(review, limit=cap)`: reconsider one fair,
/// bounded batch of automatic merge rejects. Each Python transaction is
/// its own `txn::run`; the caller holds the writer lock.
pub(crate) async fn refresh_proposal_terminals(
    client: &Client,
    review: &Review,
    limit: i64,
    ex_served: Option<&str>,
) -> Result<Value, ToolError> {
    use crate::graph_review_tools::actions::{in_txn, reads};
    let index = reads(client, |c| async move { DIndex::load(c).await }).await?;
    let selected = batch(&index, limit);
    if selected.is_empty() {
        return Ok(json!({"considered": 0, "reopened": 0, "superseded": 0, "remaining": 0}));
    }
    let display: HashMap<i64, Value> = reads(client, |c| async move {
        let g = crate::storage::graph::load_graph_body(c)
            .await
            .map_err(internal)?;
        Ok(g["entities"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|e| (e["id"].as_i64().unwrap_or(0), e["display"].clone()))
            .collect())
    })
    .await?;
    let mut candidates: Vec<(String, Value, Map<String, Value>, Row)> = Vec::new();
    let mut superseded = 0i64;
    for (target, summary) in &selected {
        let did = summary
            .get("decision_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let marker = {
            let did = did.clone();
            reads(client, |c| async move { decision_record(c, &did).await }).await?
        };
        let reason = match &marker {
            Some(m) if m.get("queue").and_then(Value::as_str) == Some(KIND) => {
                let (current, projection) = {
                    let (m, d) = (m.clone(), &display);
                    reads(
                        client,
                        |c| async move { proposal_projection(c, &m, d).await },
                    )
                    .await?
                };
                let owned = {
                    let (m, cur) = (m.clone(), current.clone());
                    reads(client, |c| async move {
                        owns_terminal(c, &m, cur.as_ref()).await
                    })
                    .await?
                };
                if owned {
                    candidates.push((
                        target.clone(),
                        summary.clone(),
                        m.clone(),
                        projection.unwrap_or_default(),
                    ));
                    continue;
                }
                "terminal row or dismissal ownership changed"
            }
            _ => "automatic decision record missing or invalid",
        };
        let (t, did2) = (target.clone(), did.clone());
        let retired = in_txn(client, |c| async move {
            let mut live = DIndex::load(c).await?;
            let Some(s) = live.active.get(&t).cloned() else {
                return Ok(false);
            };
            if s.get("decision_id").and_then(Value::as_str) != Some(did2.as_str()) {
                return Ok(false);
            }
            retire(c, &mut live, &t, &s, "externally_superseded", reason).await?;
            set_meta(c, INDEX_KEY, &live.value()).await?;
            Ok(true)
        })
        .await?;
        superseded += i64::from(retired);
    }
    let projections: Vec<Row> = candidates.iter().map(|x| x.3.clone()).collect();
    let signatures = if candidates.is_empty() {
        HashMap::new()
    } else {
        reads(client, |c| async move {
            review.signatures(c, &projections).await
        })
        .await?
    };
    let observations = [
        review.current_model.clone(),
        ex_served.map(str::to_string),
        reads(client, |c| async move {
            last_response_identity(c, KIND, &review.request_policy).await
        })
        .await?,
    ];
    let mut reopened = 0i64;
    let ignore_evidence = crate::mutants::active("mcp-dream-judge-evidence-no-reopen");
    for (target, summary, marker, projection) in &candidates {
        let key = id_of(projection).to_string();
        let mut changed = signatures.get(&key).map(String::as_str)
            != marker.get("fingerprint").and_then(Value::as_str);
        if ignore_evidence {
            changed = false;
        }
        for m in observations.iter().flatten() {
            if !m.is_empty() && marker.get("served_model").and_then(Value::as_str) != Some(m) {
                changed = true;
            }
        }
        for opinion in ["second", "third"] {
            let Some(policy) = marker
                .get(&format!("{opinion}_policy"))
                .and_then(Value::as_str)
                .filter(|p| !p.is_empty())
            else {
                continue;
            };
            let queue = format!("{KIND}_{opinion}");
            let model = {
                let (q, p) = (queue.clone(), policy.to_string());
                reads(client, |c| async move {
                    last_response_identity(c, &q, &p).await
                })
                .await?
            };
            if let Some(model) = model.filter(|m| !m.is_empty())
                && marker
                    .get(&format!("{opinion}_model"))
                    .and_then(Value::as_str)
                    != Some(model.as_str())
            {
                changed = true;
            }
        }
        if !changed {
            continue;
        }
        let did = summary
            .get("decision_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let (t, m) = (target.clone(), marker.clone());
        let done = in_txn(client, |c| async move {
            let mut live = DIndex::load(c).await?;
            let Some(s) = live.active.get(&t).cloned() else {
                return Ok(false);
            };
            if s.get("decision_id").and_then(Value::as_str) != Some(did.as_str()) {
                return Ok(false);
            }
            if !reopen_proposal(c, &m).await? {
                return Ok(false);
            }
            retire(
                c,
                &mut live,
                &t,
                &s,
                "reopened",
                "evidence, policy, or observed model changed",
            )
            .await?;
            set_meta(c, INDEX_KEY, &live.value()).await?;
            Ok(true)
        })
        .await?;
        reopened += i64::from(done);
    }
    let last_seq = selected.last().map(|(_, s)| seq_of(s)).unwrap_or(0);
    let remaining = in_txn(client, |c| async move {
        let mut live = DIndex::load(c).await?;
        live.cursors.insert(KIND.into(), json!(last_seq));
        set_meta(c, INDEX_KEY, &live.value()).await?;
        Ok(live
            .active
            .values()
            .filter(|r| r.get("queue").and_then(Value::as_str) == Some(KIND))
            .count())
    })
    .await?;
    Ok(json!({"considered": selected.len(), "reopened": reopened,
              "superseded": superseded, "remaining": remaining}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_lone_surrogate_dumps_as_python_escapes_it() {
        // json.dumps({"policy": "p", "model": "x\ud83d", "e": "é"}), CPython 3.11.
        let stand_in = char::from_u32(crate::mcp::py_loads::LONE_SURROGATE_BASE + 0x3d)
            .expect("stand-in for U+D83D");
        let v = json!({"policy": "p", "model": format!("x{stand_in}"), "e": "\u{e9}"});
        assert_eq!(
            lone_surrogate_json(&v).as_deref(),
            Some(
                format!(
                    r#"{{"policy": "p", "model": "x{b}ud83d", "e": "{b}u00e9"}}"#,
                    b = char::from(0x5c)
                )
                .as_str()
            )
        );
        assert_eq!(lone_surrogate_json(&json!({"model": "judge-one"})), None);
    }

    #[test]
    fn fingerprint_matches_python() {
        // fingerprint({"b": [1, 2.0, None], "a": "é", "c": {"z": True, "y": 0.1}})
        // under CPython 3.11.
        let v = json!({"b": [1, 2.0, null], "a": "\u{e9}", "c": {"z": true, "y": 0.1}});
        assert_eq!(
            crate::pyjson::dumps_sorted_compact(&v),
            "{\"a\":\"\\u00e9\",\"b\":[1,2.0,null],\"c\":{\"y\":0.1,\"z\":true}}"
        );
    }

    #[test]
    fn signed_view_drops_modes_and_unset_knobs() {
        let cfg = DeepDreamConfig::default();
        let view = signed_deep_dream(&cfg);
        for k in [
            "link_judge_two_vote",
            "judge_tiebreak_mode",
            "judge_second_url",
            "judge_third_model",
            "judge_third_url",
            "link_two_vote_min_confidence",
        ] {
            assert!(!view.contains_key(k), "{k}");
        }
        assert_eq!(view.len(), 48);
        assert_eq!(view["judge_second_model"], json!(""));
    }

    #[test]
    fn without_tail_cuts_before_the_marker() {
        assert_eq!(
            without_tail(Some("a | 2nd (m): reject 0.90 [agree]"), "| 2nd ("),
            "a"
        );
        assert_eq!(without_tail(Some("a"), "| 3rd ("), "a");
        assert_eq!(without_tail(None, "| 3rd ("), "");
    }
}
