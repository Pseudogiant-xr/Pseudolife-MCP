//! The deep dream's merge judge: `service_dream.deep_dream_judge`
//! (`service_dream.py:4525-5002`) with its extractors, model-identity
//! checks and the two-vote and three-judge tiebreak gates (3849-4524), the
//! judge request and its reply parsing (`memory/dream.py:425-466,
//! 1661-1716`), and the dream-judge decisions it applies
//! (`service.py:8965-9083`, `decided_by="dream-judge"`).
//!
//! The sweep calls [`deep_dream_judge`] once per tick. Lock windows follow
//! Python's: the model calls run with the writer lock released, the reads
//! and writes under it, and each verdict's writes are one transaction (the
//! `with self._storage.transaction()` around it). The evidence signing runs
//! on a blocking thread with the lock released.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};

use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use super::extractor::{self, Compat, ExtractFailure, field, py_str, truthy as py_truthy};
use super::prompts::{JUDGE_SYSTEM_PROMPT, RELATION_NAMES};
use super::review_judgments::{self as rj, Review, Row, fnum, id_of, internal, now, vf, vi, vs};
use crate::config::DeepDreamConfig;
use crate::graph_review::{relate_relations, third_relate_relation};
use crate::graph_review_tools::actions::{in_txn, reads};
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};
use crate::storage::graph::{norm_name, py_lower};

/// `MERGE_REJECT_CLASS` (`memory/graph_review.py:25`).
const MERGE_REJECT_CLASS: [&str; 2] = ["reject", "relate"];
/// `_JUDGE_ALIASES`: names that ask an endpoint for whatever it serves.
const JUDGE_ALIASES: [&str; 3] = ["judge", "extractor", "bench"];
/// `_TIEBREAK_REFUSALS`.
const REFUSE_UNKNOWN_SECOND: &str = "tiebreak needs the second vote's served model";
const REFUSE_SAME_MODEL: &str = "tiebreak needs a distinct third model";

fn err_text(e: &ToolError) -> String {
    match e {
        ToolError::Refused { code, detail, .. } => match detail {
            Some(d) => format!("{code}: {d}"),
            None => code.clone(),
        },
        ToolError::Invalid(s) | ToolError::FileNotFound(s) => s.clone(),
        ToolError::Internal(e) => e.to_string(),
    }
}

/// Python `str(s)[:n]`: the first `n` code points.
fn chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Python `f"{x:.2f}"`: the exact binary value rounded half to even.
fn fixed2(x: f64) -> String {
    let wide = format!("{x:.30}");
    let Some(dot) = wide.find('.') else {
        return format!("{x:.2}");
    };
    let (head, tail) = wide.split_at(dot);
    let digits = &tail[1..];
    let tie = digits.as_bytes().get(2) == Some(&b'5') && digits[3..].bytes().all(|b| b == b'0');
    if !tie {
        return format!("{x:.2}");
    }
    let last = digits.as_bytes()[1] - b'0';
    if last % 2 == 0 {
        format!("{head}.{}", &digits[..2])
    } else {
        format!("{x:.2}")
    }
}

/// `merge_verdict_class`.
fn verdict_class(v: Option<&str>) -> Option<&str> {
    match v {
        Some(x) if MERGE_REJECT_CLASS.contains(&x) => Some("reject"),
        other => other,
    }
}

/// `merge_verdict_token`.
fn verdict_token(verdict: &str, relation: Option<&str>) -> String {
    match relation.filter(|r| !r.is_empty()) {
        Some(r) if verdict == "relate" => format!("relate:{r}"),
        _ => verdict.to_string(),
    }
}

/// `note_with_tail(note, tail, cap=400)`.
fn note_with_tail(note: Option<&str>, tail: &str) -> String {
    let note = note.unwrap_or("");
    let (free, structured) = match note.find(" | 2nd (") {
        Some(cut) => (&note[..cut], &note[cut..]),
        None => (note, ""),
    };
    let keep = relate_head(free).map_or(0, |h| h.chars().count()) as i64;
    let room = 400 - structured.chars().count() as i64 - tail.chars().count() as i64;
    let budget = room.max(keep) as usize;
    format!("{}{structured}{tail}", chars(free, budget))
}

/// `_RELATE_TOKEN.match(note)`: a leading `relate:[a-z0-9-]+`.
fn relate_head(note: &str) -> Option<&str> {
    let rest = note.strip_prefix("relate:")?;
    let n = rest
        .bytes()
        .take_while(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-')
        .count();
    (n > 0).then(|| &note[..7 + n])
}

// ── the judge endpoint ───────────────────────────────────────────────────

static NEXT_IDENT: AtomicU64 = AtomicU64::new(1);

/// An `OpenAICompatExtractor` the judge calls: its client, the model its
/// endpoint reported serving on the last call (`served_model`), and its
/// object identity (Python's `is`).
pub(crate) struct JudgeEx {
    pub compat: Compat,
    pub served: Option<String>,
    ident: u64,
}

impl JudgeEx {
    fn new(compat: Compat) -> Self {
        JudgeEx {
            compat,
            served: None,
            ident: NEXT_IDENT.fetch_add(1, Ordering::Relaxed),
        }
    }

    fn model(&self) -> &str {
        &self.compat.model
    }
}

fn set(v: &Option<String>) -> bool {
    v.as_deref().is_some_and(|s| !s.is_empty())
}

fn env_key(name: &str) -> Option<String> {
    extractor::process_env(name).filter(|v| !v.is_empty())
}

/// `_judge_extractor(None, model=model)`: `judge_url`'s own endpoint, else
/// the dream extractor (`build_extractor_with_fallback`), with `model`
/// swapped onto it.
async fn judge_extractor(service: &Service, model: Option<&str>) -> Option<JudgeEx> {
    let cfg = &service.config.memory.deep_dream;
    let dream = &service.config.dream;
    let model = model.filter(|m| !m.is_empty());
    if set(&cfg.judge_url) {
        let name = model
            .map(str::to_string)
            .or_else(|| cfg.judge_model.clone().filter(|m| !m.is_empty()))
            .unwrap_or_else(|| "judge".into());
        return Some(JudgeEx::new(Compat::new(
            cfg.judge_url.as_deref().unwrap_or(""),
            &name,
            env_key("PSEUDOLIFE_JUDGE_API_KEY"),
            dream.extractor_max_tokens,
            dream.extractor_timeout_seconds,
            None,
        )));
    }
    let (ex, _) = extractor::build_extractor_with_fallback(dream).await.ok()?;
    let compat = ex.as_compat()?.clone();
    Some(JudgeEx::new(match model {
        Some(m) if compat.model != m => Compat::new(
            &compat.base_url,
            m,
            compat.api_key.clone(),
            compat.max_tokens,
            compat.timeout,
            Some(compat.extra_body.clone()),
        ),
        _ => compat,
    }))
}

/// `_judge_second_extractor()`.
async fn judge_second_extractor(service: &Service) -> Option<JudgeEx> {
    let cfg = &service.config.memory.deep_dream;
    let dream = &service.config.dream;
    if set(&cfg.judge_second_url) {
        let name = cfg
            .judge_second_model
            .clone()
            .filter(|m| !m.is_empty())
            .unwrap_or_else(|| "judge".into());
        return Some(JudgeEx::new(Compat::new(
            cfg.judge_second_url.as_deref().unwrap_or(""),
            &name,
            env_key("PSEUDOLIFE_JUDGE_SECOND_API_KEY"),
            dream.extractor_max_tokens,
            dream.extractor_timeout_seconds,
            None,
        )));
    }
    if set(&cfg.judge_second_model) {
        return judge_extractor(service, cfg.judge_second_model.as_deref()).await;
    }
    None
}

/// `_judge_third_extractor()`: never the first opinion's model by default.
async fn judge_third_extractor(service: &Service) -> Option<JudgeEx> {
    let cfg = &service.config.memory.deep_dream;
    let dream = &service.config.dream;
    if set(&cfg.judge_third_url) {
        let name = cfg
            .judge_third_model
            .clone()
            .filter(|m| !m.is_empty())
            .unwrap_or_else(|| "judge".into());
        return Some(JudgeEx::new(Compat::new(
            cfg.judge_third_url.as_deref().unwrap_or(""),
            &name,
            env_key("PSEUDOLIFE_JUDGE_THIRD_API_KEY"),
            dream.extractor_max_tokens,
            dream.extractor_timeout_seconds,
            None,
        )));
    }
    if set(&cfg.judge_third_model) {
        return judge_extractor(service, cfg.judge_third_model.as_deref()).await;
    }
    None
}

// ── the judge request and its reply ──────────────────────────────────────

/// `format_judge_proposal(p)` (`memory/dream.py:427-462`).
pub(crate) fn format_judge_proposal(p: &Row) -> String {
    let cap: Option<usize> = match p.get("snippet_chars") {
        None | Some(Value::Null) => Some(240),
        Some(v) => {
            let n = v.as_f64().unwrap_or(0.0).max(0.0) as usize;
            (n > 0).then_some(n)
        }
    };
    let side = |s: Option<&Value>| -> String {
        let empty = Map::new();
        let s = match s {
            Some(Value::Object(m)) if !m.is_empty() => m,
            _ => &empty,
        };
        let snips: Vec<String> = match s.get("snippets") {
            Some(Value::Array(items)) => items
                .iter()
                .take(2)
                .map(|x| {
                    let t = py_str(x);
                    match cap {
                        Some(c) => chars(&t, c),
                        None => t,
                    }
                })
                .collect(),
            _ => Vec::new(),
        };
        let snips = snips.join("; ");
        let display = s.get("display").map_or_else(|| "?".to_string(), py_str);
        let degree = s.get("degree").map_or_else(|| "0".to_string(), py_str);
        let scopes = match s.get("scopes") {
            Some(v) if py_truthy(v) => py_str(v),
            _ => "[]".to_string(),
        };
        let evidence = if snips.is_empty() {
            " evidence: none".to_string()
        } else {
            format!(" evidence: {snips}")
        };
        format!("'{display}' (degree {degree}, scopes {scopes}){evidence}")
    };
    let reason = match p.get("reason") {
        Some(v) if py_truthy(v) => py_str(v),
        _ => "?".to_string(),
    };
    let score = match p.get("score") {
        None | Some(Value::Null) => String::new(),
        Some(v) => format!(" (score {})", py_str(v)),
    };
    let mut out = format!(
        "[{}] FROM {}\n    INTO {}\n    detector: {reason}{score}",
        p.get("n").map_or_else(String::new, py_str),
        side(p.get("from")),
        side(p.get("into")),
    );
    if p.get("low_differential").is_some_and(py_truthy) {
        out.push_str(
            "\n    caution: LOW-DIFFERENTIAL evidence \u{2014} the shown \
             snippets cannot tell the sides apart (heavy overlap, one \
             side empty, or one side's evidence contained in the \
             other's). Judge from the name-shape rules (2-5), degrees \
             and scopes; \"leave\" is legitimate when those are \
             inconclusive (rule 6).",
        );
    }
    out
}

/// One validated verdict (`judge_merges`' dict).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Verdict {
    pub n: usize,
    pub verdict: String,
    pub confidence: f64,
    pub note: String,
    pub relation: Option<String>,
}

/// `judge_merges`' validation loop (`memory/dream.py:1683-1716`). `Err` is
/// an exception the loop does not catch (`int()` of an infinity).
pub(crate) fn parse_merge_verdicts(
    raw: &[Value],
    known: &HashSet<usize>,
) -> Result<Vec<Verdict>, String> {
    let mut out = Vec::new();
    for v in raw {
        let Value::Object(v) = v else { continue };
        let id = v.get("id").unwrap_or(&Value::Null);
        let n = match extractor::py_int(id).map_err(|u| u.message)? {
            Some(n) => n,
            None => continue,
        };
        let verdict = py_lower(&field(v, "verdict"));
        let n = match usize::try_from(n) {
            Ok(n) if known.contains(&n) => n,
            _ => continue,
        };
        if !matches!(verdict.as_str(), "accept" | "reject" | "relate" | "leave") {
            continue;
        }
        let confidence = extractor::confidence(v, 0.5).map_err(|u| u.message)?;
        let note = chars(&field(v, "note"), 200);
        let (mut verdict, mut relation) = (verdict, None);
        if verdict == "reject" || verdict == "relate" {
            let raw_rel = match v.get("relation") {
                Some(r) if py_truthy(r) => py_str(r),
                _ => String::new(),
            };
            let rel = norm_name(&raw_rel);
            if RELATION_NAMES.contains(&rel.as_str()) {
                verdict = "relate".into();
                relation = Some(rel);
            } else {
                verdict = "reject".into();
            }
        }
        out.push(Verdict {
            n,
            verdict,
            confidence,
            note,
            relation,
        });
    }
    Ok(out)
}

/// `ex.judge_merges(proposals)`: one call; the endpoint's served model is
/// updated as Python's `_judge_request` stores it. `Err` is `str(exc)`.
async fn judge_merges(ex: &mut JudgeEx, proposals: &[Row]) -> Result<Vec<Verdict>, String> {
    if proposals.is_empty() {
        return Ok(Vec::new());
    }
    let user: Vec<String> = proposals.iter().map(format_judge_proposal).collect();
    let (served, result) = ex
        .compat
        .judge_request(
            JUDGE_SYSTEM_PROMPT,
            &user.join("\n\n"),
            proposals.len(),
            "judge_merges",
        )
        .await;
    if let Some(s) = served {
        ex.served = s;
    }
    let raw = result.map_err(|e| match e {
        ExtractFailure::Extractor(x) => x.message,
        ExtractFailure::Uncaught(u) => u.message,
    })?;
    let known: HashSet<usize> = proposals
        .iter()
        .filter_map(|p| p.get("n").and_then(Value::as_u64))
        .map(|n| n as usize)
        .collect();
    parse_merge_verdicts(&raw, &known)
}

/// `_stamp_skipped(verdicts, rows)`.
fn stamp_skipped(verdicts: &mut Vec<Verdict>, rows: &[Row]) {
    let returned: HashSet<usize> = verdicts.iter().map(|v| v.n).collect();
    for r in rows {
        let n = r.get("n").and_then(Value::as_u64).unwrap_or(0) as usize;
        if !returned.contains(&n) {
            verdicts.push(Verdict {
                n,
                verdict: "leave".into(),
                confidence: 0.0,
                note: "model returned no verdict".into(),
                relation: None,
            });
        }
    }
}

/// `_relate_needs_relation(verdicts)`.
fn relate_needs_relation(verdicts: &mut [Verdict]) {
    for v in verdicts {
        if v.verdict == "relate" && v.relation.as_deref().is_none_or(str::is_empty) {
            v.verdict = "reject".into();
        }
    }
}

/// The verdicts in apply order: reject-class and keep first, stable.
fn apply_order(verdicts: &[Verdict]) -> Vec<Verdict> {
    let mut v = verdicts.to_vec();
    v.sort_by_key(|x| !matches!(x.verdict.as_str(), "reject" | "relate" | "keep"));
    v
}

// ── model identity ───────────────────────────────────────────────────────

/// `_DATED_SNAPSHOT.fullmatch(rest)`: `-` then eight digits or a
/// `YYYY-MM-DD` date (`\d` is any Unicode decimal).
fn dated_snapshot(rest: &str) -> bool {
    let d = |c: char| crate::graph_review::is_decimal(c);
    let cs: Vec<char> = rest.chars().collect();
    if cs.first() != Some(&'-') {
        return false;
    }
    let tail = &cs[1..];
    (tail.len() == 8 && tail.iter().all(|c| d(*c)))
        || (tail.len() == 10
            && tail.iter().enumerate().all(
                |(i, c)| {
                    if i == 4 || i == 7 { *c == '-' } else { d(*c) }
                },
            ))
}

/// `_served_model_mismatch(ex)`: `(requested, served)` or None.
fn served_model_mismatch(ex: &JudgeEx) -> Option<(String, String)> {
    let requested = ex.model();
    let served = ex.served.as_deref()?;
    if requested.is_empty()
        || served.is_empty()
        || JUDGE_ALIASES.contains(&requested)
        || served == requested
        || (served.starts_with(requested) && dated_snapshot(&served[requested.len()..]))
    {
        return None;
    }
    Some((requested.to_string(), served.to_string()))
}

/// `_report_served_model(out, opinion, ex)`.
fn report_served_model(out: &mut Map<String, Value>, opinion: &str, ex: &JudgeEx) {
    let Some((requested, served)) = served_model_mismatch(ex) else {
        return;
    };
    eprintln!(
        "deep-dream judge: {opinion} opinion asked for model {requested} but the endpoint served {served}"
    );
    bump(out, "served_model_mismatch", 1);
    if let Some(Value::Array(list)) = out.get_mut("served_model_mismatches") {
        list.push(json!({"opinion": opinion, "requested": requested, "served": served}));
    }
}

/// `_model_name(ex, fallback)`.
fn model_name(ex: &JudgeEx, fallback: Option<&str>) -> String {
    ex.served
        .clone()
        .filter(|s| !s.is_empty())
        .or_else(|| fallback.filter(|f| !f.is_empty()).map(str::to_string))
        .unwrap_or_else(|| {
            if ex.model().is_empty() {
                "OpenAICompatExtractor".into()
            } else {
                ex.model().to_string()
            }
        })
}

/// `_second_opinion_distinct(review, row, ex, ex2, model2)`; `ex2` None is
/// the first opinion's own endpoint object.
async fn second_opinion_distinct(
    c: &Client,
    review: &Review,
    row: &Row,
    ex2: Option<&JudgeEx>,
    ex: &JudgeEx,
    model2: &str,
) -> Result<bool, ToolError> {
    let first_model = vs(row, "judge_model").unwrap_or("");
    let first_served = review.observed(c, row).await?.filter(|s| !s.is_empty());
    let other = ex2.unwrap_or(ex);
    let served2 = other.served.as_deref().filter(|s| !s.is_empty());
    let configured2 = other.model();
    let served_ok = if crate::mutants::active("mcp-dream-judge-no-served-check") {
        true
    } else {
        matches!((first_served.as_deref(), served2), (Some(a), Some(b)) if a != b)
    };
    Ok(served_ok
        && ex2.is_some()
        && first_model != model2
        && !(!configured2.is_empty() && first_model == configured2))
}

/// `_third_model_taken(row, ex, ex3, second_model)`.
fn third_model_taken(row: &Row, ex: &JudgeEx, ex3: &JudgeEx, second_model: Option<&str>) -> bool {
    if crate::mutants::active("mcp-dream-judge-third-same-model") {
        return false;
    }
    if ex3.ident == ex.ident {
        return true;
    }
    let name3 = ex3.model();
    if name3.is_empty() || JUDGE_ALIASES.contains(&name3) {
        return false;
    }
    let mut taken: Vec<Option<&str>> = vec![
        Some(ex.model()),
        vs(row, "judge_model"),
        vs(row, "judge2_model"),
        vs(row, "judge2_served_model"),
    ];
    if let Some(m) = second_model.filter(|m| !m.is_empty() && !JUDGE_ALIASES.contains(m)) {
        taken.push(Some(m));
    }
    taken.contains(&Some(name3))
}

/// `_tiebreak_refusal(review, row, configured3)`.
async fn tiebreak_refusal(
    c: &Client,
    review: &Review,
    row: &Row,
    configured3: Option<&str>,
) -> Result<Option<&'static str>, ToolError> {
    let Some(second) = vs(row, "judge2_served_model").filter(|s| !s.is_empty()) else {
        return Ok(Some("unknown_second"));
    };
    let first = review.observed(c, row).await?.filter(|s| !s.is_empty());
    let third = vs(row, "judge3_served_model").filter(|s| !s.is_empty());
    let earlier: HashSet<&str> = [
        first.as_deref(),
        Some(second),
        vs(row, "judge_model"),
        vs(row, "judge2_model"),
    ]
    .into_iter()
    .flatten()
    .filter(|m| !m.is_empty())
    .collect();
    let ok = first.is_some()
        && third.is_some_and(|t| !earlier.contains(t))
        && vs(row, "judge3_model").is_none_or(|m| !earlier.contains(m))
        && !configured3.is_some_and(|m| !m.is_empty() && earlier.contains(m));
    Ok((!ok).then_some("same_model"))
}

type Vote = (Option<String>, Option<String>, f64);

/// `_tiebreak_majority(row, e, cfg)`: the action and the majority's votes.
fn tiebreak_majority(
    row: &Row,
    e: &Row,
    cfg: &DeepDreamConfig,
) -> Option<(&'static str, Vec<Vote>)> {
    if cfg.judge_tiebreak_mode != "auto" || !rj::truthy(row, "judge3_verdict") {
        return None;
    }
    let note = vs(row, "judge_note");
    let (rel1, rel2) = relate_relations(note);
    let rel3 = vs(row, "judge3_relation")
        .filter(|r| !r.is_empty())
        .map(str::to_string)
        .or_else(|| third_relate_relation(note));
    let conf = |k: &str| vf(row, k).unwrap_or(0.0);
    let s = |k: &str| vs(row, k).map(str::to_string);
    let votes: Vec<Vote> = vec![
        (s("judge_verdict"), rel1, conf("judge_confidence")),
        (s("judge2_verdict"), rel2, conf("judge2_confidence")),
        (s("judge3_verdict"), rel3, conf("judge3_confidence")),
    ];
    let mean = |side: &[Vote]| side.iter().map(|v| v.2).sum::<f64>() / side.len() as f64;
    let rejects: Vec<Vote> = votes
        .iter()
        .filter(|v| verdict_class(v.0.as_deref()) == Some("reject"))
        .cloned()
        .collect();
    let accepts: Vec<Vote> = votes
        .iter()
        .filter(|v| v.0.as_deref() == Some("accept"))
        .cloned()
        .collect();
    let need = if crate::mutants::active("mcp-dream-judge-tiebreak-minority") {
        1
    } else {
        2
    };
    if rejects.len() >= need
        && matches!(cfg.judge_mode.as_str(), "auto-reject" | "auto")
        && mean(&rejects) >= cfg.judge_reject_min_confidence_2
    {
        return Some(("reject", rejects));
    }
    if accepts.len() >= 2
        && cfg.judge_mode == "auto"
        && e.get("low_differential") == Some(&Value::Bool(false))
        && mean(&accepts) >= cfg.judge_accept_min_confidence
    {
        return Some(("accept", accepts));
    }
    None
}

/// `out[key] = out.get(key, 0) + n`.
fn bump(out: &mut Map<String, Value>, key: &str, n: i64) {
    let cur = out.get(key).and_then(Value::as_i64).unwrap_or(0);
    out.insert(key.to_string(), json!(cur + n));
}

// ── storage writes (`storage/postgres.py`) ───────────────────────────────

/// `set_entity_proposal_judgment` (3895-3910).
#[allow(clippy::too_many_arguments)]
async fn set_first_judgment(
    c: &Client,
    id: i64,
    verdict: &str,
    confidence: f64,
    note: Option<&str>,
    model: &str,
    at: f64,
    served: Option<&str>,
) -> Result<bool, ToolError> {
    Ok(c.execute(
        "UPDATE entity_proposals SET judge_verdict=$1, \
         judge_confidence=$2, judge_note=$3, judge_model=$4, \
         judged_at=$5, judge_served_model=$6 \
         WHERE id=$7 AND status='pending'",
        &[
            &verdict,
            &(confidence as f32),
            &note,
            &model,
            &at,
            &served,
            &id,
        ],
    )
    .await
    .map_err(internal)?
        > 0)
}

/// `set_entity_proposal_second_judgment` / `_third_judgment` (3806-3849):
/// `n` is 2 or 3.
#[allow(clippy::too_many_arguments)]
async fn set_later_judgment(
    c: &Client,
    n: u8,
    id: i64,
    verdict: Option<&str>,
    confidence: Option<f64>,
    model: Option<&str>,
    at: Option<f64>,
    note: Option<&str>,
    served: Option<&str>,
    policy: Option<&str>,
) -> Result<bool, ToolError> {
    let sql = format!(
        "UPDATE entity_proposals SET judge{n}_verdict=$1, \
         judge{n}_confidence=$2, judge{n}_model=$3, judged{n}_at=$4, \
         judge_note = COALESCE($5, judge_note), \
         judge{n}_served_model = COALESCE($6, judge{n}_served_model), \
         judge{n}_policy = COALESCE($7, judge{n}_policy) \
         WHERE id=$8 AND status='pending'"
    );
    let conf = confidence.map(|x| x as f32);
    Ok(c.execute(
        &sql,
        &[&verdict, &conf, &model, &at, &note, &served, &policy, &id],
    )
    .await
    .map_err(internal)?
        > 0)
}

struct Graph {
    display: HashMap<i64, Option<String>>,
    canonical: HashMap<i64, String>,
    edges: Vec<(i64, i64)>,
    review_edges: Vec<crate::graph_review::Edge>,
}

async fn load_graph(c: &Client) -> Result<Graph, ToolError> {
    let g = crate::storage::graph::load_graph_body(c)
        .await
        .map_err(internal)?;
    let mut display = HashMap::new();
    let mut canonical = HashMap::new();
    for e in g["entities"].as_array().into_iter().flatten() {
        let id = e["id"].as_i64().unwrap_or(0);
        display.insert(id, e["display"].as_str().map(str::to_string));
        canonical.insert(id, e["canonical"].as_str().unwrap_or_default().to_string());
    }
    let mut edges = Vec::new();
    let mut review_edges = Vec::new();
    for e in g["edges"].as_array().into_iter().flatten() {
        let (s, d) = (
            e["src_id"].as_i64().unwrap_or(0),
            e["dst_id"].as_i64().unwrap_or(0),
        );
        edges.push((s, d));
        review_edges.push(crate::graph_review::Edge {
            src_id: s,
            relation: e["relation"].as_str().unwrap_or_default().to_string(),
            dst_id: d,
            confidence: e["confidence"].as_f64().unwrap_or(0.0),
            origin: e["origin"].as_str().map(str::to_string),
        });
    }
    Ok(Graph {
        display,
        canonical,
        edges,
        review_edges,
    })
}

impl Graph {
    /// `disp.get(eid, "?")` (a NULL display is written as `?`, as the
    /// review actions write it).
    fn disp_or_unknown(&self, id: Option<i64>) -> String {
        id.and_then(|i| self.display.get(&i).cloned().flatten())
            .unwrap_or_else(|| "?".into())
    }
}

/// `dismissed_pairs()`.
async fn dismissed_pairs(c: &Client) -> Result<HashSet<(String, String)>, ToolError> {
    Ok(c.query("SELECT a_norm, b_norm FROM dismissed_pairs", &[])
        .await
        .map_err(internal)?
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect())
}

/// `_graph_reject_entity_proposal_locked(id, decided_by="dream-judge",
/// _review_guard=...)` in the caller's transaction (`service.py:9039-9083`;
/// a dream-judge reject retires no automatic decision).
async fn reject_locked(c: &Client, review: &Review, row: &Row, id: i64) -> Result<bool, ToolError> {
    if !review.current(c, row, false, false).await? {
        return Ok(false);
    }
    let decided_at = now();
    let Some(prop) = rj::get_entity_proposal(c, id).await? else {
        return Ok(false);
    };
    if vs(&prop, "status") != Some("pending") {
        return Ok(false);
    }
    let votes = crate::graph_review_tools::actions::entity_proposal_votes_body(c, id).await?;
    let ok = c
        .execute(
            "UPDATE entity_proposals SET status = $1, \
             decided_by = COALESCE($2, decided_by), \
             decided_at = COALESCE($3, decided_at) WHERE id = $4",
            &[&"rejected", &"dream-judge", &decided_at, &id],
        )
        .await
        .map_err(internal)?
        > 0;
    if !ok {
        return Ok(false);
    }
    let g = load_graph(c).await?;
    if vs(&prop, "kind") == Some("merge") {
        let (entity, into) = (vi(&prop, "entity_id"), vi(&prop, "into_id"));
        crate::graph_review_tools::actions::record_merge_decision_by(
            c,
            id,
            &g.disp_or_unknown(entity),
            Some(&g.disp_or_unknown(into)),
            "rejected",
            vf(&prop, "score").map(|x| x as f32),
            vs(&prop, "reason"),
            "dream-judge",
            decided_at,
            votes,
        )
        .await?;
        let canon = |id: Option<i64>| {
            id.and_then(|i| g.canonical.get(&i).cloned())
                .filter(|s| !s.is_empty())
        };
        if let (Some(a), Some(b)) = (canon(entity), canon(into))
            && a != b
        {
            let mut pair = [a, b];
            pair.sort();
            c.execute(
                "INSERT INTO dismissed_pairs (a_norm, b_norm, dismissed_at) \
                 VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                &[&pair[0], &pair[1], &now()],
            )
            .await
            .map_err(internal)?;
        }
    }
    Ok(true)
}

/// `_graph_accept_entity_merge_locked(id, decided_by="dream-judge",
/// _review_guard=...)` in the caller's transaction (`service.py:8965-8999`):
/// the audit, the fold over current evidence, the status.
async fn accept_locked(c: &Client, review: &Review, row: &Row, id: i64) -> Result<bool, ToolError> {
    if !review.current(c, row, false, false).await? {
        return Ok(false);
    }
    let Some(prop) = rj::get_entity_proposal(c, id).await? else {
        return Ok(false);
    };
    if vs(&prop, "status") != Some("pending") || vs(&prop, "kind") != Some("merge") {
        return Ok(false);
    }
    let g = load_graph(c).await?;
    let deg = crate::graph_review::degree_counts(&g.review_edges);
    let facts: HashMap<i64, i64> = c
        .query(
            "SELECT entity_id, COUNT(*) FROM facts \
             WHERE status = 'current' AND entity_id IS NOT NULL GROUP BY entity_id",
            &[],
        )
        .await
        .map_err(internal)?
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect();
    let ev = |id: Option<i64>| {
        id.map_or(0, |i| {
            deg.get(&i).copied().unwrap_or(0) as i64 + facts.get(&i).copied().unwrap_or(0)
        })
    };
    let (mut frm, mut into) = (vi(&prop, "entity_id"), vi(&prop, "into_id"));
    if ev(frm) > ev(into) {
        std::mem::swap(&mut frm, &mut into);
    }
    let decided_at = now();
    let votes = crate::graph_review_tools::actions::entity_proposal_votes_body(c, id).await?;
    crate::graph_review_tools::actions::record_merge_decision_by(
        c,
        id,
        &g.disp_or_unknown(frm),
        Some(&g.disp_or_unknown(into)),
        "accepted",
        vf(&prop, "score").map(|x| x as f32),
        vs(&prop, "reason"),
        "dream-judge",
        decided_at,
        votes,
    )
    .await?;
    let ok = match (frm, into) {
        (Some(f), Some(i)) => crate::storage::graph::merge_entity_body(c, f, i)
            .await
            .map_err(internal)?,
        _ => false,
    };
    c.execute(
        "UPDATE entity_proposals SET status = $1, \
         decided_by = COALESCE($2, decided_by), \
         decided_at = COALESCE($3, decided_at) WHERE id = $4",
        &[&"accepted", &"dream-judge", &decided_at, &id],
    )
    .await
    .map_err(internal)?;
    Ok(ok)
}

/// `review.apply(row, graph_reject_entity_proposal, id,
/// decided_by="dream-judge")`: the reject, its versioned record, and the
/// memo's retry when nothing applied.
async fn apply_reject(
    c: &Client,
    review: &Review,
    row: &Row,
    id: i64,
    ex_served: Option<&str>,
) -> Result<bool, ToolError> {
    let prior = rj::capture_proposal_pair(c, row).await?;
    let rejected = reject_locked(c, review, row, id).await?;
    if rejected {
        rj::record_proposal_terminal(c, review, row, &prior, ex_served).await?;
    } else {
        review.retry(c, row).await?;
    }
    Ok(rejected)
}

/// `review.apply(row, graph_accept_entity_merge, id, decided_by=...)`.
async fn apply_accept(
    c: &Client,
    review: &mut Review,
    row: &Row,
    id: i64,
) -> Result<bool, ToolError> {
    let accepted = accept_locked(c, review, row, id).await?;
    if accepted {
        // A structural edit changes graph-wide differential evidence.
        review.valid.clear();
    } else {
        review.retry(c, row).await?;
    }
    Ok(accepted)
}

/// What the tick's automatic applies share.
struct Ctx<'a> {
    service: &'a Service,
    cfg: &'a DeepDreamConfig,
    dismissed: HashSet<(String, String)>,
    canon_by_id: HashMap<i64, String>,
    /// `snap` (`_snapshot_once`): the snapshot written, once per call.
    snap: Option<Option<String>>,
    ex_served: Option<String>,
}

impl Ctx<'_> {
    fn pair_dismissed(&self, row: &Row) -> bool {
        let canon = |k: &str| {
            vi(row, k)
                .and_then(|i| self.canon_by_id.get(&i).cloned())
                .unwrap_or_default()
        };
        let mut pair = [canon("entity_id"), canon("into_id")];
        pair.sort();
        let [a, b] = pair;
        self.dismissed.contains(&(a, b))
    }
}

async fn tick_reads(
    c: &Client,
) -> Result<(HashSet<(String, String)>, HashMap<i64, String>), ToolError> {
    let dismissed = dismissed_pairs(c).await?;
    let g = load_graph(c).await?;
    Ok((dismissed, g.canonical))
}

/// `_merge_accept_refusal(review, row, e, snap)`.
async fn merge_accept_refusal(
    c: &Client,
    ctx: &mut Ctx<'_>,
    review: &Review,
    row: &Row,
    e: &Row,
) -> Result<Option<String>, ToolError> {
    let name = |k: &str| {
        e.get(k)
            .and_then(|s| s.get("display"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    let (a, b) = (name("from"), name("into"));
    if vs(row, "reason")
        .unwrap_or("")
        .starts_with("analyzer-duplicate")
    {
        return Ok(Some(
            "auto-accept refused: analyzer-filed rows are unmeasured".into(),
        ));
    }
    if crate::graph_review::merge_veto(&a, &b).is_some() {
        return Ok(Some("auto-accept refused: name veto".into()));
    }
    if crate::graph_consolidation::variant_conflict(&a, &b) {
        return Ok(Some("auto-accept refused: variant conflict".into()));
    }
    if ctx.snap.is_none() {
        let keep = ctx.cfg.snapshot_keep;
        ctx.snap =
            Some(super::deep::write_graph_snapshot_body(c, &ctx.service.data_dir, keep).await?);
    }
    if ctx.snap.as_ref().is_some_and(Option::is_none) {
        review.retry(c, row).await?;
        return Ok(Some("auto-accept refused: graph snapshot failed".into()));
    }
    Ok(None)
}

/// `_file_relate_link(out, row, e, votes)`: the number of link proposals
/// filed (0 or 1).
async fn file_relate_link(
    c: &Client,
    row: &Row,
    e: &Row,
    votes: &[Vote],
) -> Result<i64, ToolError> {
    let relates: Vec<(f64, String)> = votes
        .iter()
        .filter(|v| v.0.as_deref() == Some("relate"))
        .filter_map(|v| {
            v.1.clone()
                .filter(|r| !r.is_empty() && r != "related-to")
                .map(|r| (v.2, r))
        })
        .collect();
    let Some(first) = relates.first() else {
        return Ok(0);
    };
    let (conf, relation) =
        relates.iter().skip(1).fold(
            first.clone(),
            |best, x| if x.0 > best.0 { x.clone() } else { best },
        );
    let names: Vec<String> = match crate::storage::graph::load_relations_body(c)
        .await
        .map_err(internal)?
    {
        Value::Array(rows) => rows
            .iter()
            .filter_map(|r| r.get("name").and_then(Value::as_str).map(str::to_string))
            .collect(),
        _ => Vec::new(),
    };
    if !names.contains(&relation) {
        return Ok(0);
    }
    let g = load_graph(c).await?;
    let disp = |id: Option<i64>| id.and_then(|i| g.display.get(&i).cloned().flatten());
    let stored = (vi(row, "entity_id"), vi(row, "into_id"));
    let shown = |k: &str| {
        e.get(k)
            .and_then(|s| s.get("display"))
            .and_then(Value::as_str)
            .map(str::to_string)
    };
    let shown = (shown("from"), shown("into"));
    if shown.0 == shown.1 {
        return Ok(0);
    }
    let (src_id, dst_id) = if shown == (disp(stored.0), disp(stored.1)) {
        stored
    } else if shown == (disp(stored.1), disp(stored.0)) {
        (stored.1, stored.0)
    } else {
        return Ok(0);
    };
    let (Some(src_id), Some(dst_id)) = (src_id, dst_id) else {
        return Ok(0);
    };
    let pair: HashSet<i64> = [src_id, dst_id].into_iter().collect();
    if g.edges
        .iter()
        .any(|&(s, d)| [s, d].into_iter().collect::<HashSet<i64>>() == pair)
    {
        return Ok(0);
    }
    if c.query_opt(
        "SELECT 1 FROM edge_proposals WHERE (src_id=$1 AND dst_id=$2) \
             OR (src_id=$2 AND dst_id=$1) LIMIT 1",
        &[&src_id, &dst_id],
    )
    .await
    .map_err(internal)?
    .is_some()
    {
        return Ok(0);
    }
    // `_graph_propose_links_locked([...], source="merge-judge-relate",
    // _by_id=True)` (`service.py:8809-8854`).
    let known: Vec<String> = names
        .iter()
        .filter(|n| !matches!(n.as_str(), "prefers" | "avoids"))
        .cloned()
        .collect();
    let src = disp(Some(src_id)).unwrap_or_else(|| "None".into());
    let dst = disp(Some(dst_id)).unwrap_or_else(|| "None".into());
    let resolved = crate::graph_read::resolve_relation(&known, &relation)
        .0
        .unwrap_or_else(|| "related-to".into());
    if src.is_empty()
        || dst.is_empty()
        || norm_name(&src) == norm_name(&dst)
        || crate::graph_review_tools::actions::is_hard_type_violation(&src, &resolved, &dst)
    {
        return Ok(0);
    }
    let confidence =
        crate::graph_review_tools::actions::edge_confidence(&src, &resolved, &dst) as f32;
    let rationale = format!(
        "merge judge: relate ({relation}) {} on merge proposal #{}, rejected as distinct",
        fixed2(conf),
        id_of(row)
    );
    let inserted = c
        .query_opt(
            "INSERT INTO edge_proposals \
             (src_id, relation, dst_id, confidence, similarity, rationale, source, created_at) \
             VALUES ($1, $2, $3, $4, NULL, $5, 'merge-judge-relate', $6) \
             ON CONFLICT (src_id, relation, dst_id) DO NOTHING RETURNING id",
            &[&src_id, &resolved, &dst_id, &confidence, &rationale, &now()],
        )
        .await
        .map_err(internal)?;
    Ok(i64::from(inserted.is_some()))
}

/// `_file_relate_link_isolated`: in a savepoint of its own, so a failed
/// filing rolls back only itself.
async fn file_relate_link_isolated(
    c: &Client,
    out: &mut Map<String, Value>,
    row: &Row,
    e: &Row,
    votes: &[Vote],
) -> Result<(), ToolError> {
    c.batch_execute("SAVEPOINT merge_judge_relate")
        .await
        .map_err(internal)?;
    match file_relate_link(c, row, e, votes).await {
        Ok(n) => {
            c.batch_execute("RELEASE SAVEPOINT merge_judge_relate")
                .await
                .map_err(internal)?;
            bump(out, "relate_links_filed", n);
        }
        Err(err) => {
            c.batch_execute("ROLLBACK TO SAVEPOINT merge_judge_relate")
                .await
                .map_err(internal)?;
            eprintln!(
                "deep-dream judge: relate link for merge proposal #{} not filed: {}",
                id_of(row),
                err_text(&err)
            );
            bump(out, "relate_link_errors", 1);
        }
    }
    Ok(())
}

// ── the tiebreak ─────────────────────────────────────────────────────────

/// `_tiebreak_refuse(out, row, reason, counter)`.
async fn tiebreak_refuse(
    c: &Client,
    out: &mut Map<String, Value>,
    row: &mut Row,
    reason: &str,
    counter: &str,
) -> Result<(), ToolError> {
    let note = vs(row, "judge_note").unwrap_or("").to_string();
    if note.contains(&format!("| {reason}")) {
        return Ok(());
    }
    let note = format!("{note} | {reason}");
    if set_later_judgment(
        c,
        3,
        id_of(row),
        vs(row, "judge3_verdict"),
        vf(row, "judge3_confidence"),
        vs(row, "judge3_model"),
        vf(row, "judged3_at"),
        Some(&note),
        None,
        None,
    )
    .await?
    {
        row.insert("judge_note".into(), json!(note));
        bump(out, counter, 1);
    }
    Ok(())
}

/// `_tiebreak_apply(out, review, row, e, snap, dismissed, canon_by_id)`.
async fn tiebreak_apply(
    c: &Client,
    ctx: &mut Ctx<'_>,
    out: &mut Map<String, Value>,
    review: &mut Review,
    row: &mut Row,
    e: &Row,
) -> Result<(), ToolError> {
    let Some((action, votes)) = tiebreak_majority(row, e, ctx.cfg) else {
        return Ok(());
    };
    for opinion in ["second", "third"] {
        if rj::vote_endpoint_moved(c, row, opinion, None).await? {
            return Ok(());
        }
    }
    let id = vi(e, "id").unwrap_or_else(|| id_of(row));
    if action == "accept" && ctx.pair_dismissed(row) {
        let rejected = apply_reject(c, review, row, id, ctx.ex_served.as_deref()).await?;
        bump(out, "auto_rejected", i64::from(rejected));
        bump(out, "auto_accept_refused", 1);
        return Ok(());
    }
    let configured3 = ctx.cfg.judge_third_model.clone().filter(|m| !m.is_empty());
    if let Some(refusal) = tiebreak_refusal(c, review, row, configured3.as_deref()).await? {
        let (reason, counter) = if refusal == "unknown_second" {
            (REFUSE_UNKNOWN_SECOND, "tiebreak_refused_unknown_second")
        } else {
            (REFUSE_SAME_MODEL, "tiebreak_refused_same_model")
        };
        return tiebreak_refuse(c, out, row, reason, counter).await;
    }
    if action == "accept"
        && let Some(reason) = merge_accept_refusal(c, ctx, review, row, e).await?
    {
        return tiebreak_refuse(c, out, row, &reason, "auto_accept_refused").await;
    }
    let s = |k: &str| vs(row, k).map(str::to_string);
    review.second_policy = s("judge2_policy");
    review.second_model = s("judge2_served_model");
    review.third_policy = s("judge3_policy");
    review.third_model = s("judge3_served_model");
    if action == "reject" {
        if apply_reject(c, review, row, id, ctx.ex_served.as_deref()).await? {
            bump(out, "auto_rejected", 1);
            bump(out, "tiebreak_applied", 1);
            file_relate_link_isolated(c, out, row, e, &votes).await?;
        }
    } else if apply_accept(c, review, row, id).await? {
        bump(out, "auto_accepted", 1);
        bump(out, "tiebreak_applied", 1);
    }
    Ok(())
}

/// `_tiebreak_replay(out, review, rows)`: the tiebreak from votes already
/// recorded, with no model call. Takes the lock.
async fn tiebreak_replay(
    ready: &Ready,
    ctx_base: (&Service, &DeepDreamConfig, Option<String>),
    out: &mut Map<String, Value>,
    review: &mut Review,
    rows: Vec<Row>,
) -> Result<(), ToolError> {
    let client = ready.storage.client();
    let enriched = review.rows(&rows);
    let _lock = ready.write.lock().await;
    let (dismissed, canon_by_id) = reads(client, |c| async move { tick_reads(c).await }).await?;
    let mut ctx = Ctx {
        service: ctx_base.0,
        cfg: ctx_base.1,
        dismissed,
        canon_by_id,
        snap: None,
        ex_served: ctx_base.2,
    };
    {
        let r = &mut *review;
        let rows = &rows;
        reads(
            client,
            |c| async move { r.validate_recorded(c, rows).await },
        )
        .await?;
    }
    for (row, e) in rows.into_iter().zip(enriched) {
        let mut row = row;
        let (r, o, cx) = (&mut *review, &mut *out, &mut ctx);
        let (row_ref, e_ref) = (&mut row, &e);
        in_txn(client, |c| async move {
            if r.current(c, row_ref, true, true).await? {
                tiebreak_apply(c, cx, o, r, row_ref, e_ref).await?;
            }
            Ok(())
        })
        .await?;
    }
    Ok(())
}

// ── deep_dream_judge ─────────────────────────────────────────────────────

/// The evidence rows the model is shown (`proposals`).
fn proposals_of(enriched: &[Row], cfg: &DeepDreamConfig) -> Vec<Row> {
    enriched
        .iter()
        .enumerate()
        .map(|(i, e)| {
            let mut m = Map::new();
            m.insert("n".into(), json!(i + 1));
            m.insert("from".into(), e.get("from").cloned().unwrap_or(Value::Null));
            m.insert("into".into(), e.get("into").cloned().unwrap_or(Value::Null));
            m.insert(
                "reason".into(),
                e.get("reason").cloned().unwrap_or(Value::Null),
            );
            m.insert(
                "score".into(),
                e.get("score").cloned().unwrap_or(Value::Null),
            );
            m.insert(
                "low_differential".into(),
                e.get("low_differential").cloned().unwrap_or(Value::Null),
            );
            m.insert("snippet_chars".into(), json!(cfg.judge_snippet_max_chars));
            m
        })
        .collect()
}

/// Same endpoint (object, or URL and key): a failure there ends the tick.
fn same_endpoint(other: Option<&JudgeEx>, ex: &JudgeEx) -> bool {
    match other {
        None => true,
        Some(o) => {
            o.ident == ex.ident
                || (o.compat.base_url == ex.compat.base_url
                    && o.compat.api_key == ex.compat.api_key)
        }
    }
}

/// The sweep's condition for logging a judge tick (`memory/dream.py:2493-2512`).
pub(crate) fn worth_logging(judged: &Map<String, Value>) -> bool {
    let truthy = |k: &str| judged.get(k).is_some_and(py_truthy);
    let reopened = judged
        .get("reconsideration")
        .and_then(|r| r.get("reopened"))
        .is_some_and(py_truthy);
    reopened
        || [
            "judged",
            "second_opinions",
            "auto_reject_refused_same_model",
            "served_model_mismatch",
            "second_opinion_error",
            "third_opinions",
            "tiebreak_refused_same_model",
            "tiebreak_refused_unknown_second",
            "tiebreak_applied",
            "auto_rejected",
            "auto_accepted",
            "third_opinion_error",
        ]
        .iter()
        .any(|k| truthy(k))
}

/// `deep_dream_judge()`: never fails into the sweep; an error is the
/// answer's `error` field.
pub(crate) async fn deep_dream_judge(service: &Service, ready: &Ready) -> Map<String, Value> {
    let cfg = &service.config.memory.deep_dream;
    let one = |k: &str, v: Value| {
        let mut m = Map::new();
        m.insert("judged".into(), json!(0));
        m.insert(k.into(), v);
        m
    };
    if !cfg.judges_enabled {
        return one("skipped", json!("judges_disabled"));
    }
    if !matches!(cfg.judge_mode.as_str(), "shadow" | "auto-reject" | "auto") {
        return one("skipped", json!("disabled"));
    }
    match judge_tick(service, ready).await {
        Ok(out) => out,
        Err(e) => {
            eprintln!("deep-dream judge failed: {e}");
            one("error", json!(e))
        }
    }
}

/// The judge's `try` body.
async fn judge_tick(service: &Service, ready: &Ready) -> Result<Map<String, Value>, String> {
    let cfg = &service.config.memory.deep_dream;
    let client = ready.storage.client();
    let e2s = |e: ToolError| err_text(&e);
    let cap = cfg.judge_batch.max(1);
    let (pending, has_terminals) = {
        let _lock = ready.write.lock().await;
        reads(client, |c| async move {
            Ok((
                rj::pending_rows(c).await?,
                rj::has_active_decisions(c).await?,
            ))
        })
        .await
        .map_err(e2s)?
    };
    if pending.is_empty() && !has_terminals {
        let mut m = Map::new();
        m.insert("judged".into(), json!(0));
        return Ok(m);
    }
    let Some(mut ex) = judge_extractor(service, None).await else {
        let mut m = Map::new();
        m.insert("judged".into(), json!(0));
        m.insert("skipped".into(), json!("no_judge_extractor"));
        return Ok(m);
    };
    let current_model = rj::observed_model(&ex.compat, ex.served.as_deref()).await;
    let mut review = Review::new(&service.config, &ex.compat, pending, current_model);
    let reconsidered = {
        let _lock = ready.write.lock().await;
        let reconsidered =
            rj::refresh_proposal_terminals(client, &review, cap, ex.served.as_deref())
                .await
                .map_err(e2s)?;
        let r = &mut review;
        reads(client, |c| async move {
            r.pending = rj::pending_rows(c).await?;
            r.prepare(c, cap).await
        })
        .await
        .map_err(e2s)?;
        reconsidered
    };
    review.sign().await.map_err(e2s)?;
    let pending = {
        let _lock = ready.write.lock().await;
        let r = &mut review;
        let served = ex.served.clone();
        in_txn(
            client,
            |c| async move { r.refresh(c, served.as_deref()).await },
        )
        .await
        .map_err(e2s)?;
        review.pending.clone()
    };
    let first: Vec<Row> = pending
        .iter()
        .filter(|p| !rj::truthy(p, "judge_verdict"))
        .cloned()
        .collect();
    let awaiting: Vec<Row> = if cfg.judge_second_opinion {
        pending
            .iter()
            .filter(|p| rj::truthy(p, "judge_verdict") && !rj::truthy(p, "judge2_verdict"))
            .cloned()
            .collect()
    } else {
        Vec::new()
    };
    let same_model_second = !set(&cfg.judge_second_url)
        && (!set(&cfg.judge_second_model) || cfg.judge_second_model.as_deref() == Some(ex.model()));
    let mut second: Vec<Row> = if same_model_second {
        Vec::new()
    } else {
        awaiting.clone()
    };
    let skipped = if same_model_second { awaiting.len() } else { 0 };
    let split: Vec<Row> = if matches!(cfg.judge_tiebreak_mode.as_str(), "shadow" | "auto") {
        pending
            .iter()
            .filter(|p| {
                rj::truthy(p, "judge_verdict")
                    && rj::truthy(p, "judge2_verdict")
                    && !rj::truthy(p, "judge3_verdict")
                    && verdict_class(vs(p, "judge_verdict"))
                        != verdict_class(vs(p, "judge2_verdict"))
            })
            .cloned()
            .collect()
    } else {
        Vec::new()
    };
    let mut third: Vec<Row> = Vec::new();
    let mut ex3: Option<JudgeEx> = None;
    let mut third_skips: Vec<(&str, usize)> = Vec::new();
    if !split.is_empty() {
        ex3 = judge_third_extractor(service).await;
        match &ex3 {
            None => third_skips.push(("third_opinion_skipped_unconfigured", split.len())),
            Some(x3) => {
                third = split
                    .iter()
                    .filter(|p| !third_model_taken(p, &ex, x3, cfg.judge_second_model.as_deref()))
                    .cloned()
                    .collect();
                if third.len() < split.len() {
                    third_skips.push((
                        "third_opinion_skipped_same_model",
                        split.len() - third.len(),
                    ));
                }
            }
        }
    }
    let mut replay: Vec<Row> = Vec::new();
    if cfg.judge_tiebreak_mode == "auto" {
        let refused = [
            format!("| {REFUSE_UNKNOWN_SECOND}"),
            format!("| {REFUSE_SAME_MODEL}"),
            "| auto-accept refused".to_string(),
        ];
        for p in &pending {
            if replay.len() as i64 >= cap {
                break;
            }
            let note = vs(p, "judge_note").unwrap_or("");
            if rj::truthy(p, "judge3_verdict")
                && !refused.iter().any(|r| note.contains(r.as_str()))
                && tiebreak_majority(p, &review.rows(std::slice::from_ref(p))[0], cfg).is_some()
            {
                replay.push(p.clone());
            }
        }
    }
    if first.is_empty() && second.is_empty() && third.is_empty() && replay.is_empty() {
        let mut idle = Map::new();
        idle.insert("judged".into(), json!(0));
        idle.insert("reconsideration".into(), reconsidered);
        for (k, n) in &third_skips {
            idle.insert((*k).into(), json!(n));
        }
        if skipped > 0 {
            idle.insert("second_opinion_skipped_same_model".into(), json!(skipped));
        }
        return Ok(idle);
    }
    let mut out = Map::new();
    for (k, v) in [
        ("judged", json!(0)),
        ("auto_rejected", json!(0)),
        ("auto_accepted", json!(0)),
        ("second_opinions", json!(0)),
        ("pending_unjudged", json!(0)),
        ("second_opinion_skipped_same_model", json!(skipped)),
        ("auto_reject_refused_same_model", json!(0)),
        ("third_opinions", json!(0)),
        ("tiebreak_applied", json!(0)),
        ("tiebreak_refused_same_model", json!(0)),
        ("tiebreak_refused_unknown_second", json!(0)),
    ] {
        out.insert(k.into(), v);
    }
    for (k, n) in &third_skips {
        out.insert((*k).into(), json!(n));
    }
    for (k, v) in [
        ("relate_links_filed", json!(0)),
        ("served_model_mismatch", json!(0)),
        ("served_model_mismatches", json!([])),
        ("reconsideration", reconsidered),
        ("model", json!(ex.model())),
        ("mode", json!(cfg.judge_mode)),
    ] {
        out.insert(k.into(), v);
    }
    if !replay.is_empty() {
        tiebreak_replay(
            ready,
            (service, cfg, ex.served.clone()),
            &mut out,
            &mut review,
            replay,
        )
        .await
        .map_err(e2s)?;
    }

    // Second opinions first: rows judged on an earlier sweep.
    let mut ex2: Option<JudgeEx> = None;
    let mut verdicts2: Vec<Verdict> = Vec::new();
    let batch2: Vec<Row> = second.iter().take(cap as usize).cloned().collect();
    let mut enriched2: Vec<Row> = Vec::new();
    let mut proposals2: Vec<Row> = Vec::new();
    let mut second_observed: Option<String> = None;
    if !second.is_empty() {
        ex2 = judge_second_extractor(service).await;
        eprintln!(
            "deep-dream judge: second opinion on {} pending merge proposal(s) (mode {})",
            batch2.len(),
            cfg.judge_mode
        );
        enriched2 = review.rows(&batch2);
        proposals2 = proposals_of(&enriched2, cfg);
        let result = match ex2.as_mut() {
            Some(x2) => {
                second_observed = rj::observed_model(&x2.compat, x2.served.as_deref()).await;
                judge_merges(x2, &proposals2).await
            }
            None => {
                second_observed = rj::observed_model(&ex.compat, ex.served.as_deref()).await;
                judge_merges(&mut ex, &proposals2).await
            }
        };
        match result {
            Ok(v) => verdicts2 = v,
            Err(err) => {
                if same_endpoint(ex2.as_ref(), &ex) {
                    return Err(err);
                }
                eprintln!(
                    "deep-dream judge: second opinion failed, first opinions continue: {err}"
                );
                out.insert("second_opinion_error".into(), json!(err));
                second.clear();
            }
        }
    }
    if !second.is_empty() {
        stamp_skipped(&mut verdicts2, &proposals2);
        relate_needs_relation(&mut verdicts2);
        {
            let other = ex2.as_ref().unwrap_or(&ex);
            report_served_model(&mut out, "second", other);
        }
        let model2 = model_name(ex2.as_ref().unwrap_or(&ex), None);
        let at = now();
        let (dismissed, canon_by_id) = {
            let _lock = ready.write.lock().await;
            reads(client, |c| async move { tick_reads(c).await })
                .await
                .map_err(e2s)?
        };
        let mut ctx = Ctx {
            service,
            cfg,
            dismissed,
            canon_by_id,
            snap: None,
            ex_served: ex.served.clone(),
        };
        let _lock = ready.write.lock().await;
        {
            let other = ex2.as_ref().unwrap_or(&ex);
            let served2 = other.served.clone();
            let compat2 = other.compat.clone();
            let observed2 = second_observed.clone();
            let r = &mut review;
            let b = &batch2;
            let config = &service.config;
            reads(client, |c| async move {
                r.validate(
                    c,
                    config,
                    b,
                    served2.as_deref(),
                    Some(&compat2),
                    observed2.as_deref(),
                    "second",
                )
                .await
            })
            .await
            .map_err(e2s)?;
        }
        for v in apply_order(&verdicts2) {
            let e = enriched2[v.n - 1].clone();
            let row = batch2[v.n - 1].clone();
            let (r, o, cx) = (&mut review, &mut out, &mut ctx);
            let (x2, x1) = (ex2.as_ref(), &ex);
            let model2 = model2.as_str();
            in_txn(client, |c| async move {
                second_verdict(c, cx, o, r, x1, x2, &row, &e, &v, model2, at).await
            })
            .await
            .map_err(e2s)?;
        }
    }

    // Third opinions next, on the split rows.
    let mut budget = cap - second.iter().take(cap as usize).count() as i64;
    if !third.is_empty()
        && budget > 0
        && let Some(x3) = ex3.as_mut()
    {
        let mut batch3: Vec<Row> = third.iter().take(budget as usize).cloned().collect();
        eprintln!(
            "deep-dream judge: third opinion on {} split merge proposal(s) (mode {}, tiebreak {})",
            batch3.len(),
            cfg.judge_mode,
            cfg.judge_tiebreak_mode
        );
        let enriched3 = review.rows(&batch3);
        let proposals3 = proposals_of(&enriched3, cfg);
        let third_observed = rj::observed_model(&x3.compat, x3.served.as_deref()).await;
        let mut verdicts3 = Vec::new();
        match judge_merges(x3, &proposals3).await {
            Ok(v) => verdicts3 = v,
            Err(err) => {
                if same_endpoint(Some(x3), &ex) {
                    return Err(err);
                }
                eprintln!("deep-dream judge: third opinion failed, first opinions continue: {err}");
                out.insert("third_opinion_error".into(), json!(err));
                batch3.clear();
            }
        }
        if !batch3.is_empty() {
            budget -= batch3.len() as i64;
            tiebreak_record(
                ready,
                service,
                &mut out,
                &mut review,
                &ex,
                x3,
                batch3,
                enriched3,
                verdicts3,
                proposals3,
                third_observed,
            )
            .await
            .map_err(e2s)?;
        }
    }

    if !first.is_empty() && budget > 0 {
        let batch: Vec<Row> = first.iter().take(budget as usize).cloned().collect();
        eprintln!(
            "deep-dream judge: judging {} pending merge proposal(s) (mode {})",
            batch.len(),
            cfg.judge_mode
        );
        let enriched = review.rows(&batch);
        let proposals = proposals_of(&enriched, cfg);
        let mut verdicts = judge_merges(&mut ex, &proposals).await?;
        stamp_skipped(&mut verdicts, &proposals);
        relate_needs_relation(&mut verdicts);
        report_served_model(&mut out, "first", &ex);
        let fallback = out.get("model").and_then(Value::as_str).map(str::to_string);
        let model = model_name(&ex, fallback.as_deref());
        out.insert("model".into(), json!(model));
        let at = now();
        let _lock = ready.write.lock().await;
        {
            let r = &mut review;
            let b = &batch;
            let served = ex.served.clone();
            let config = &service.config;
            reads(client, |c| async move {
                r.validate(c, config, b, served.as_deref(), None, None, "second")
                    .await
            })
            .await
            .map_err(e2s)?;
        }
        let mut ctx = Ctx {
            service,
            cfg,
            dismissed: HashSet::new(),
            canon_by_id: HashMap::new(),
            snap: None,
            ex_served: ex.served.clone(),
        };
        for v in apply_order(&verdicts) {
            let e = enriched[v.n - 1].clone();
            let row = batch[v.n - 1].clone();
            let (r, o, cx) = (&mut review, &mut out, &mut ctx);
            let served = ex.served.clone();
            let model = model.as_str();
            in_txn(client, |c| async move {
                first_verdict(c, cx, o, r, &row, &e, &v, model, served.as_deref(), at).await
            })
            .await
            .map_err(e2s)?;
        }
    }
    let judged = out.get("judged").and_then(Value::as_i64).unwrap_or(0);
    out.insert(
        "pending_unjudged".into(),
        json!((first.len() as i64 - judged).max(0)),
    );
    Ok(out)
}

/// One first-opinion verdict (its transaction).
#[allow(clippy::too_many_arguments)]
async fn first_verdict(
    c: &Client,
    ctx: &mut Ctx<'_>,
    out: &mut Map<String, Value>,
    review: &mut Review,
    row: &Row,
    e: &Row,
    v: &Verdict,
    model: &str,
    served: Option<&str>,
    at: f64,
) -> Result<(), ToolError> {
    if !review.current(c, row, false, false).await? {
        return Ok(());
    }
    let mut note = (!v.note.is_empty()).then(|| v.note.clone());
    if v.verdict == "relate" {
        let rel = v.relation.clone().unwrap_or_else(|| "None".into());
        note = Some(match note {
            Some(n) => format!("relate:{rel} | {n}"),
            None => format!("relate:{rel}"),
        });
    }
    let id = vi(e, "id").unwrap_or_else(|| id_of(row));
    let ok = set_first_judgment(
        c,
        id,
        &v.verdict,
        v.confidence,
        note.as_deref(),
        model,
        at,
        served,
    )
    .await?;
    if !ok {
        return Ok(());
    }
    review.record(c, row, served).await?;
    bump(out, "judged", 1);
    let bar = ctx.cfg.judge_reject_min_confidence;
    let passes = if crate::mutants::active("mcp-dream-judge-confidence-bar") {
        v.confidence > bar
    } else {
        v.confidence >= bar
    };
    if matches!(ctx.cfg.judge_mode.as_str(), "auto-reject" | "auto")
        && MERGE_REJECT_CLASS.contains(&v.verdict.as_str())
        && passes
        && apply_reject(c, review, row, id, ctx.ex_served.as_deref()).await?
    {
        bump(out, "auto_rejected", 1);
        file_relate_link_isolated(
            c,
            out,
            row,
            e,
            &[(Some(v.verdict.clone()), v.relation.clone(), v.confidence)],
        )
        .await?;
    }
    Ok(())
}

/// One second-opinion verdict (its transaction).
#[allow(clippy::too_many_arguments)]
async fn second_verdict(
    c: &Client,
    ctx: &mut Ctx<'_>,
    out: &mut Map<String, Value>,
    review: &mut Review,
    ex: &JudgeEx,
    ex2: Option<&JudgeEx>,
    row: &Row,
    e: &Row,
    v: &Verdict,
    model2: &str,
    at: f64,
) -> Result<(), ToolError> {
    let id = vi(e, "id").unwrap_or_else(|| id_of(row));
    let v1 = vs(row, "judge_verdict").map(str::to_string);
    let c1 = vf(row, "judge_confidence").unwrap_or(0.0);
    let (v2, c2) = (v.verdict.as_str(), v.confidence);
    let note1 = chars(vs(row, "judge_note").unwrap_or(""), 160);
    let tag = if verdict_class(v1.as_deref()) != verdict_class(Some(v2)) {
        "split"
    } else {
        "agree"
    };
    let token2 = verdict_token(v2, v.relation.as_deref());
    let note = format!("{note1} | 2nd ({model2}): {token2} {} [{tag}]", fixed2(c2));
    if !review.current(c, row, true, false).await? {
        return Ok(());
    }
    let served2 = ex2.unwrap_or(ex).served.clone();
    let ok = set_later_judgment(
        c,
        2,
        id,
        Some(v2),
        Some(c2),
        Some(model2),
        Some(at),
        Some(&note),
        served2.as_deref(),
        review.second_policy.as_deref(),
    )
    .await?;
    if !ok {
        return Ok(());
    }
    bump(out, "second_opinions", 1);
    let mean = (c1 + c2) / 2.0;
    let cfg = ctx.cfg;
    let reject_pair = if crate::mutants::active("mcp-dream-judge-one-vote") {
        verdict_class(Some(v2)) == Some("reject")
    } else {
        verdict_class(v1.as_deref()) == Some("reject") && verdict_class(Some(v2)) == Some("reject")
    };
    if reject_pair
        && matches!(cfg.judge_mode.as_str(), "auto-reject" | "auto")
        && mean >= cfg.judge_reject_min_confidence_2
    {
        let distinct = crate::mutants::active("mcp-dream-judge-same-model-reject")
            || second_opinion_distinct(c, review, row, ex2, ex, model2).await?;
        if !distinct {
            let refused = chars(
                &format!("{note} | auto-reject needs a distinct second model"),
                400,
            );
            set_later_judgment(
                c,
                2,
                id,
                Some(v2),
                Some(c2),
                Some(model2),
                Some(at),
                Some(&refused),
                None,
                None,
            )
            .await?;
            bump(out, "auto_reject_refused_same_model", 1);
            return Ok(());
        }
        if apply_reject(c, review, row, id, ctx.ex_served.as_deref()).await? {
            bump(out, "auto_rejected", 1);
            let rel1 = relate_relations(vs(row, "judge_note")).0;
            file_relate_link_isolated(
                c,
                out,
                row,
                e,
                &[
                    (v1.clone(), rel1, c1),
                    (Some(v2.to_string()), v.relation.clone(), c2),
                ],
            )
            .await?;
        }
    } else if v1.as_deref() == Some("accept")
        && v2 == "accept"
        && cfg.judge_mode == "auto"
        && e.get("low_differential") == Some(&Value::Bool(false))
        && mean >= cfg.judge_accept_min_confidence
    {
        if ctx.pair_dismissed(row) {
            let rejected = apply_reject(c, review, row, id, ctx.ex_served.as_deref()).await?;
            bump(out, "auto_rejected", i64::from(rejected));
            bump(out, "auto_accept_refused", 1);
            return Ok(());
        }
        let reason = if !second_opinion_distinct(c, review, row, ex2, ex, model2).await? {
            Some("auto-accept needs a distinct second model".to_string())
        } else {
            merge_accept_refusal(c, ctx, review, row, e).await?
        };
        if let Some(reason) = reason {
            let refused = chars(&format!("{note} | {reason}"), 400);
            set_later_judgment(
                c,
                2,
                id,
                Some(v2),
                Some(c2),
                Some(model2),
                Some(at),
                Some(&refused),
                None,
                None,
            )
            .await?;
            bump(out, "auto_accept_refused", 1);
            return Ok(());
        }
        if apply_accept(c, review, row, id).await? {
            bump(out, "auto_accepted", 1);
        }
    }
    Ok(())
}

/// `_tiebreak_record(...)`: record the third opinions, then the tiebreak.
/// Takes the lock.
#[allow(clippy::too_many_arguments)]
async fn tiebreak_record(
    ready: &Ready,
    service: &Service,
    out: &mut Map<String, Value>,
    review: &mut Review,
    ex: &JudgeEx,
    ex3: &JudgeEx,
    batch3: Vec<Row>,
    enriched3: Vec<Row>,
    mut verdicts3: Vec<Verdict>,
    proposals3: Vec<Row>,
    third_observed: Option<String>,
) -> Result<(), ToolError> {
    let client = ready.storage.client();
    let cfg = &service.config.memory.deep_dream;
    stamp_skipped(&mut verdicts3, &proposals3);
    relate_needs_relation(&mut verdicts3);
    report_served_model(out, "third", ex3);
    let model3 = model_name(ex3, None);
    let served3 = ex3.served.clone();
    let at = now();
    let _lock = ready.write.lock().await;
    let (dismissed, canon_by_id) = reads(client, |c| async move { tick_reads(c).await }).await?;
    let mut ctx = Ctx {
        service,
        cfg,
        dismissed,
        canon_by_id,
        snap: None,
        ex_served: ex.served.clone(),
    };
    {
        let r = &mut *review;
        let b = &batch3;
        let compat3 = ex3.compat.clone();
        let (s3, o3) = (served3.clone(), third_observed.clone());
        let config = &service.config;
        reads(client, |c| async move {
            r.validate(
                c,
                config,
                b,
                s3.as_deref(),
                Some(&compat3),
                o3.as_deref(),
                "third",
            )
            .await
        })
        .await?;
    }
    let policy3 = review.third_policy.clone();
    for v in apply_order(&verdicts3) {
        let e = enriched3[v.n - 1].clone();
        let mut row = batch3[v.n - 1].clone();
        let (r, o, cx) = (&mut *review, &mut *out, &mut ctx);
        let (model3, served3, policy3) = (model3.as_str(), served3.as_deref(), policy3.as_deref());
        in_txn(client, |c| async move {
            if !r.current(c, &row, true, true).await? {
                return Ok(());
            }
            let classes: Vec<Option<&str>> = [
                vs(&row, "judge_verdict"),
                vs(&row, "judge2_verdict"),
                Some(v.verdict.as_str()),
            ]
            .into_iter()
            .map(verdict_class)
            .collect();
            let count = |k: &str| classes.iter().filter(|c| **c == Some(k)).count();
            let tally = if count("reject") >= 2 {
                "2-1 reject"
            } else if count("accept") >= 2 {
                "2-1 accept"
            } else {
                "no majority"
            };
            let (v3, c3) = (v.verdict.clone(), v.confidence);
            let token3 = verdict_token(&v3, v.relation.as_deref());
            let note = note_with_tail(
                vs(&row, "judge_note"),
                &format!(" | 3rd ({model3}): {token3} {} [{tally}]", fixed2(c3)),
            );
            let id = vi(&e, "id").unwrap_or_else(|| id_of(&row));
            if !set_later_judgment(
                c,
                3,
                id,
                Some(&v3),
                Some(c3),
                Some(model3),
                Some(at),
                Some(&note),
                served3,
                policy3,
            )
            .await?
            {
                return Ok(());
            }
            bump(o, "third_opinions", 1);
            row.insert("judge3_verdict".into(), json!(v3));
            row.insert("judge3_confidence".into(), fnum(Some(c3)));
            row.insert("judge3_model".into(), json!(model3));
            row.insert("judged3_at".into(), fnum(Some(at)));
            row.insert("judge3_served_model".into(), json!(served3));
            row.insert("judge3_policy".into(), json!(policy3));
            row.insert("judge_note".into(), json!(note));
            row.insert("judge3_relation".into(), json!(v.relation));
            tiebreak_apply(c, cx, o, r, &mut row, &e).await
        })
        .await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed2_rounds_like_python() {
        // f"{x:.2f}" under CPython 3.11.
        for (x, want) in [
            (0.125, "0.12"),
            (0.375, "0.38"),
            (0.625, "0.62"),
            (0.875, "0.88"),
            (0.8, "0.80"),
            (0.005, "0.01"),
            (0.015, "0.01"),
            (1.0, "1.00"),
            (0.0, "0.00"),
            (0.555, "0.56"),
        ] {
            assert_eq!(fixed2(x), want, "{x}");
        }
    }

    #[test]
    fn dated_snapshots_are_the_requested_model() {
        assert!(dated_snapshot("-20240806"));
        assert!(dated_snapshot("-2024-08-06"));
        assert!(!dated_snapshot("-mini"));
        assert!(!dated_snapshot("-2024080"));
    }

    #[test]
    fn note_tail_keeps_structure() {
        let tail = " | 3rd (m3): reject 0.90 [2-1 reject]";
        let note = format!(
            "relate:uses {} | 2nd (m2): reject 0.80 [split]",
            "x".repeat(500)
        );
        let out = note_with_tail(Some(&note), tail);
        assert!(out.starts_with("relate:uses "));
        assert!(out.ends_with(&format!(" | 2nd (m2): reject 0.80 [split]{tail}")));
        assert_eq!(out.chars().count(), 400);
    }
}

#[cfg(test)]
mod golden_tests {
    use super::*;

    const GOLDEN: &str = include_str!("../../harness/goldens/w2e-merge-judge.json");

    fn golden() -> Value {
        serde_json::from_str(GOLDEN).expect("golden")
    }

    #[test]
    fn proposals_format_as_python_does() {
        for case in golden()["formatted"].as_array().expect("cases") {
            let p = case[0].as_object().expect("proposal");
            assert_eq!(format_judge_proposal(p), case[1].as_str().expect("text"));
        }
    }

    #[test]
    fn verdicts_validate_as_python_does() {
        let g = golden();
        let raw = g["raw_verdicts"].as_array().expect("raw").clone();
        let known: HashSet<usize> = [1, 2, 3, 4].into_iter().collect();
        let got: Vec<Value> = parse_merge_verdicts(&raw, &known)
            .expect("parse")
            .into_iter()
            .map(|v| {
                json!({"n": v.n, "verdict": v.verdict, "confidence": v.confidence,
                       "note": v.note, "relation": v.relation})
            })
            .collect();
        let want = g["verdicts"].as_array().expect("verdicts");
        assert_eq!(got.len(), want.len());
        for (a, b) in got.iter().zip(want) {
            assert_eq!(a["n"], b["n"]);
            assert_eq!(a["verdict"], b["verdict"]);
            assert_eq!(a["confidence"].as_f64(), b["confidence"].as_f64());
            assert_eq!(a["note"], b["note"]);
            assert_eq!(a["relation"], b["relation"]);
        }
    }

    #[test]
    fn note_tails_match_python() {
        for case in golden()["notes"].as_array().expect("notes") {
            assert_eq!(
                note_with_tail(case[0].as_str(), case[1].as_str().expect("tail")),
                case[2].as_str().expect("note")
            );
        }
    }

    #[test]
    fn served_model_mismatches_match_python() {
        for case in golden()["served"].as_array().expect("served") {
            let mut ex = JudgeEx::new(Compat::new(
                "http://x/v1",
                case[0].as_str().expect("model"),
                None,
                1,
                1.0,
                None,
            ));
            ex.served = case[1].as_str().map(str::to_string);
            let got = served_model_mismatch(&ex).map(|(a, b)| json!([a, b]));
            assert_eq!(got.unwrap_or(Value::Null), case[2]);
        }
    }

    #[test]
    fn the_policy_and_its_fingerprint_match_python() {
        let g = golden();
        let config = crate::config::Config::default();
        let mut extra = Map::new();
        extra.insert("cache_prompt".into(), json!(false));
        let compat = Compat::new(
            "http://127.0.0.1:9/v1/",
            "judge-one",
            None,
            2048,
            240.0,
            Some(extra),
        );
        let policy = rj::judging_policy(&config, &compat, JUDGE_SYSTEM_PROMPT);
        assert_eq!(
            Value::Object(rj::signed_deep_dream(&config.memory.deep_dream)),
            g["signed_deep_dream"]
        );
        assert_eq!(
            rj::fingerprint(&policy),
            g["policy_fingerprint"].as_str().expect("fp")
        );
        assert_eq!(
            rj::fingerprint(&g["sample"]),
            g["sample_fingerprint"].as_str().expect("fp")
        );
    }
}
