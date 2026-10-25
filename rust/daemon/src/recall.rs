//! `memory_recall` (spec RC1-RC17): the multi-hop walk search → graph
//! expansion → re-query. `mcp_server.memory_recall` (2872-2951, with its
//! caps 2729-2869) → `service.recall` (`service.py:9085-9294`) →
//! `memory/recall.py` `run_recall` (226-382) and `recall_state_to_dict`
//! (385-418). Seeds come from the mechanical driver, or under
//! `memory.recall.driver: llm` from the dream's extraction model
//! (`LLMController`, `simple_complete`: `memory/recall.py:421-492`) over the
//! dream extractor's no-redirect client.
//!
//! Like Python's, the walk holds no lock of its own: each search takes the
//! search guard and each graph read the writer lock, as Python's `search`
//! and `graph_neighborhood` each take the service lock; the model call
//! holds neither.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc;
use std::time::Instant;

use serde_json::{Map, Value, json};

use crate::graph_read_tools::graph_neighborhood;
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFuture};
use crate::read::search_route::{self, Request};
use crate::recall_mentions::mentions;
use crate::service::{Ready, Service};
use crate::storage::graph::{self, norm_name};
use crate::storage::py_strip;

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// `_RECALL_MAX_ENTITIES`, `_RECALL_MAX_EDGES`, `_RECALL_MAX_TEXTS`,
/// `_RECALL_MAX_FACTS_PER_ENTITY`, `_RECALL_SEED_TEXT_BUDGET` and
/// `_RECALL_TEXT_CHARS` (`mcp_server.py:2729-2751`).
const MAX_ENTITIES: usize = 10;
const MAX_EDGES: usize = 15;
const MAX_TEXTS: usize = 6;
const MAX_FACTS_PER_ENTITY: usize = 5;
const SEED_TEXT_BUDGET: usize = 3;
const TEXT_CHARS: usize = 200;

/// The tools this module serves on `/mcp` (`mcp::dispatch::registry`).
pub fn mcp_tools() -> Vec<(&'static str, crate::mcp::dispatch::ToolFn)> {
    vec![("memory_recall", mcp_memory_recall)]
}

/// A bound integer as `i64`, saturating past its range (Python clamps any
/// integer the binder admits).
fn int_saturating(v: &Value) -> i64 {
    v.as_i64().unwrap_or_else(|| {
        if v.to_string().starts_with('-') {
            i64::MIN
        } else {
            i64::MAX
        }
    })
}

/// Python's `seq[:n]` for a possibly negative `n`.
fn py_prefix<T: Clone>(v: &[T], n: i64) -> Vec<T> {
    let len = v.len() as i64;
    let end = if n < 0 { (len + n).max(0) } else { n.min(len) };
    v[..end as usize].to_vec()
}

/// The caller identity a search's retrieval event carries.
#[derive(Clone)]
struct Ident {
    header_session: Option<String>,
    transport_session: Option<String>,
}

fn mcp_memory_recall<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let query = a.get("query").and_then(Value::as_str).unwrap_or("");
        let hops = a.get("hops").map_or(3, int_saturating);
        let top_k = a.get("top_k").map_or(5, int_saturating);
        let verbose = a.get("verbose").and_then(Value::as_bool).unwrap_or(false);
        let ident = Ident {
            header_session: call.ident.header_session.clone(),
            transport_session: call.ident.transport_session.clone(),
        };
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let beyond = a.get("top_k").filter(|n| n.as_i64().is_none()).cloned();
        let out = recall(
            &app.service,
            &ready,
            query,
            Some(hops),
            Some(top_k),
            beyond,
            &ident,
        )
        .await?;
        Ok(project(out, top_k, verbose))
    })
}

// ── the MCP caps and projection (mcp_server.py:2754-2951) ──────────────────

/// `_hop_quota_select`: `cap` items, `cap // H` per distinct hop, the
/// remainder to the latest hops, unused slots granted latest-first; within a
/// hop, the earliest items.
fn hop_quota_select<T: Clone>(items: &[(T, i64)], cap: usize) -> Vec<T> {
    if cap == 0 || items.is_empty() {
        return Vec::new();
    }
    if crate::mutants::active("mcp-recall-flat-caps") {
        return items.iter().take(cap).map(|(i, _)| i.clone()).collect();
    }
    let mut buckets: BTreeMap<i64, Vec<T>> = BTreeMap::new();
    for (item, hop) in items {
        buckets.entry(*hop).or_default().push(item.clone());
    }
    let hop_order: Vec<i64> = buckets.keys().copied().collect();
    let h = hop_order.len();
    let (base, extra) = (cap / h, cap % h);
    let mut take: BTreeMap<i64, usize> = BTreeMap::new();
    for (i, hop) in hop_order.iter().enumerate() {
        let quota = base + usize::from(i >= h - extra);
        take.insert(*hop, quota.min(buckets[hop].len()));
    }
    let mut remaining = cap - take.values().sum::<usize>();
    for hop in hop_order.iter().rev() {
        if remaining == 0 {
            break;
        }
        let spare = buckets[hop].len() - take[hop];
        if spare == 0 {
            continue;
        }
        let grant = spare.min(remaining);
        *take.get_mut(hop).unwrap() += grant;
        remaining -= grant;
    }
    hop_order
        .iter()
        .flat_map(|hop| buckets[hop][..take[hop]].to_vec())
        .collect()
}

/// `_cap_recall_texts`: the cap split between the seed search and the hop
/// re-queries, a short hop pool backfilled from the seed texts.
fn cap_texts(texts: &[Value], seed_text_count: usize, top_k: i64) -> Vec<Value> {
    let split = seed_text_count.min(texts.len());
    let (seed, hop) = texts.split_at(split);
    let budget = (SEED_TEXT_BUDGET as i64)
        .min(top_k)
        .min(MAX_TEXTS as i64)
        .max(0) as usize;
    let mut seed_take = budget.min(seed.len());
    let hop_take = (MAX_TEXTS - seed_take).min(hop.len());
    let short = (MAX_TEXTS - seed_take) - hop_take;
    if short > 0 {
        seed_take = (seed_take + short).min(seed.len());
    }
    seed[..seed_take]
        .iter()
        .chain(&hop[..hop_take])
        .cloned()
        .collect()
}

/// `_compact_recall_fact`.
fn compact_fact(f: &Value) -> Value {
    let mut out = Map::new();
    out.insert(
        "attribute".into(),
        f.get("attribute").cloned().unwrap_or(Value::Null),
    );
    out.insert(
        "value".into(),
        f.get("value").cloned().unwrap_or(Value::Null),
    );
    if truthy(f.get("re_verify")) {
        out.insert("re_verify".into(), json!(true));
        out.insert(
            "re_verify_reason".into(),
            f.get("re_verify_reason").cloned().unwrap_or(Value::Null),
        );
    }
    for k in ["authority", "distortion_tolerance", "pinned"] {
        if truthy(f.get(k)) {
            out.insert(k.into(), f[k].clone());
        }
    }
    Value::Object(out)
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
    }
}

/// `_compact_recall_text`: 200 characters and an ellipsis.
fn compact_text(t: &Value) -> Value {
    let s = t.as_str().unwrap_or("");
    if s.chars().count() <= TEXT_CHARS {
        return t.clone();
    }
    json!(format!(
        "{}…",
        s.chars().take(TEXT_CHARS).collect::<String>()
    ))
}

/// `memory_recall`'s body after `service.recall`: the entity, fact, edge and
/// text caps, the bookkeeping keys dropped, and the compact projection.
fn project(mut out: Map<String, Value>, top_k: i64, verbose: bool) -> Value {
    let entity_hop = out.get("entity_hop").cloned().unwrap_or_else(|| json!({}));
    let seed_text_count = out
        .get("seed_text_count")
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    let entities: Vec<(Value, i64)> = out
        .get("entities")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|e| {
            let name = e.get("entity").and_then(Value::as_str).unwrap_or("");
            (
                e.clone(),
                entity_hop.get(name).and_then(Value::as_i64).unwrap_or(0),
            )
        })
        .collect();
    let capped: Vec<Value> = hop_quota_select(&entities, MAX_ENTITIES)
        .into_iter()
        .map(|mut e| {
            let facts: Vec<Value> = e
                .get("facts")
                .and_then(Value::as_array)
                .map(|f| f.iter().take(MAX_FACTS_PER_ENTITY).cloned().collect())
                .unwrap_or_default();
            e["facts"] = json!(facts);
            e
        })
        .collect();
    let surviving: HashSet<String> = capped
        .iter()
        .filter_map(|e| e.get("entity").and_then(Value::as_str).map(str::to_string))
        .collect();
    let edges: Vec<Value> = out
        .get("edges")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut edge_hop: Vec<i64> = out
        .get("edge_hop")
        .and_then(Value::as_array)
        .map(|h| h.iter().filter_map(Value::as_i64).collect())
        .unwrap_or_default();
    if edge_hop.len() != edges.len() {
        edge_hop = vec![0; edges.len()];
    }
    let (mut both, mut rest) = (Vec::new(), Vec::new());
    for (e, h) in edges.iter().zip(&edge_hop) {
        let end = |k: &str| {
            e.get(k)
                .and_then(Value::as_str)
                .is_some_and(|n| surviving.contains(n))
        };
        if end("src") && end("dst") {
            both.push((e.clone(), *h));
        } else {
            rest.push((e.clone(), *h));
        }
    }
    let mut picked = hop_quota_select(&both, MAX_EDGES);
    if picked.len() < MAX_EDGES {
        picked.extend(hop_quota_select(&rest, MAX_EDGES - picked.len()));
    }
    let texts: Vec<Value> = out
        .get("texts")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let texts = cap_texts(&texts, seed_text_count, top_k);
    out.insert("entities".into(), json!(capped));
    out.insert("edges".into(), json!(picked));
    out.insert("texts".into(), json!(texts));
    out.shift_remove("entity_hop");
    out.shift_remove("edge_hop");
    out.shift_remove("seed_text_count");
    if !verbose {
        let entities: Vec<Value> = out["entities"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|n| {
                json!({"entity": n.get("entity").cloned().unwrap_or(Value::Null),
                    "facts": n.get("facts").and_then(Value::as_array).into_iter().flatten()
                        .map(compact_fact).collect::<Vec<_>>()})
            })
            .collect();
        let edges: Vec<Value> = out["edges"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|e| {
                json!({"src": e.get("src").cloned().unwrap_or(Value::Null),
                    "relation": e.get("relation").cloned().unwrap_or(Value::Null),
                    "dst": e.get("dst").cloned().unwrap_or(Value::Null)})
            })
            .collect();
        let texts: Vec<Value> = out["texts"]
            .as_array()
            .into_iter()
            .flatten()
            .map(compact_text)
            .collect();
        out.insert("entities".into(), json!(entities));
        out.insert("edges".into(), json!(edges));
        out.insert("texts".into(), json!(texts));
    }
    Value::Object(out)
}

// ── service.recall (service.py:9085-9163) ──────────────────────────────────

/// `_recall_vocab` and `_graph_degrees` from one graph load: display names
/// by id, then each entity's aliases (`ORDER BY alias`, grouped by first
/// alias), empty names dropped, first occurrence kept; and the asserted
/// degree by display name.
async fn vocab_and_degrees(
    ready: &Ready,
    with_degrees: bool,
) -> Result<(Vec<String>, HashMap<String, usize>), ToolError> {
    let g = {
        let _ws = ready.write.lock().await;
        graph::load_graph(ready.storage.client())
            .await
            .map_err(internal)?
    };
    let entities = g["entities"].as_array().cloned().unwrap_or_default();
    let mut names: Vec<String> = entities
        .iter()
        .filter_map(|e| e["display"].as_str().map(str::to_string))
        .collect();
    for al in g["aliases"]
        .as_object()
        .into_iter()
        .flat_map(|m| m.values())
    {
        names.extend(
            al.as_array()
                .into_iter()
                .flatten()
                .filter_map(|s| s.as_str().map(str::to_string)),
        );
    }
    let mut seen = HashSet::new();
    let vocab: Vec<String> = names
        .into_iter()
        .filter(|n| !n.is_empty() && seen.insert(n.clone()))
        .collect();
    let mut degrees = HashMap::new();
    if with_degrees {
        let edges: Vec<crate::graph_read::StoredEdge> =
            serde_json::from_value(g["edges"].clone()).map_err(internal)?;
        let named: Vec<crate::graph_read::NamedEntity> =
            serde_json::from_value(g["entities"].clone()).map_err(internal)?;
        degrees = crate::graph_read::degrees_by_name(&edges, &named)
            .into_iter()
            .collect();
    }
    Ok((vocab, degrees))
}

/// `_hub_threshold`: `max(floor, sorted(degrees)[idx])` with
/// `idx = min(len - 1, int(len * percentile / 100))` (a negative index
/// counts from the end, as Python's does).
fn hub_threshold(
    degrees: &HashMap<String, usize>,
    percentile: f64,
    floor: i64,
) -> Result<i64, String> {
    let mut vals: Vec<i64> = degrees.values().map(|&d| d as i64).collect();
    vals.sort_unstable();
    if vals.is_empty() {
        return Ok(floor);
    }
    let len = vals.len() as i64;
    let raw = len as f64 * percentile / 100.0;
    if !raw.is_finite() {
        // `int(nan)` is a ValueError (invalid_argument), `int(inf)` an
        // OverflowError (internal).
        return Err(if raw.is_nan() {
            "nan:cannot convert float NaN to integer".into()
        } else {
            "cannot convert float infinity to integer".into()
        });
    }
    let idx = (len - 1).min(raw.trunc() as i64);
    let at = if idx < 0 { len + idx } else { idx };
    if at < 0 {
        return Err("list index out of range".into());
    }
    Ok(floor.max(vals[at as usize]))
}

/// The walk's state (`RecallState`).
#[derive(Default)]
struct State {
    seeds: Vec<String>,
    entities: Vec<String>,
    entity_facts: HashMap<String, Vec<Value>>,
    entity_hop: Vec<(String, i64)>,
    texts: Vec<String>,
    seed_text_count: usize,
    edges: Vec<Value>,
    edge_hop: Vec<i64>,
    paths: Vec<Value>,
    iterations: i64,
    low_confidence: bool,
    searches_issued: i64,
    truncated: bool,
}

/// The knobs `run_recall` takes, normalized as `service.recall` and
/// `run_recall` normalize them.
struct Knobs {
    hops: i64,
    top_k: i64,
    top_k_beyond: Option<Value>,
    max_entities: i64,
    gate: bool,
    degrees: HashMap<String, usize>,
    hub_threshold: Option<i64>,
    expand_budget: Option<i64>,
    max_searches_per_hop: Option<i64>,
    max_total_searches: Option<i64>,
    time_budget: Option<f64>,
    skip_part_of: bool,
}

impl Knobs {
    /// `degree_fn(n) or 0`.
    fn degree(&self, name: &str) -> i64 {
        self.degrees.get(name).map_or(0, |&d| d as i64)
    }
}

/// `MechanicalController.seed_entities`: the vocabulary names the query
/// mentions, else those the seed hits mention.
fn seed_entities(query: &str, hits: &[String], vocab: &[String]) -> Vec<String> {
    let q: Vec<String> = vocab
        .iter()
        .filter(|n| mentions(query, n) || crate::mutants::active("mcp-recall-seed-any"))
        .cloned()
        .collect();
    if !q.is_empty() {
        return q;
    }
    let joined = hits.join(" ");
    vocab
        .iter()
        .filter(|n| mentions(&joined, n))
        .cloned()
        .collect()
}

// ── the llm driver (memory/recall.py:421-492) ──────────────────────────────

/// `simple_complete`'s timeout (`no_redirect.urlopen(req, timeout=30.0)`).
const LLM_TIMEOUT_S: f64 = 30.0;

/// Where `simple_complete` sends the seed prompt.
struct LlmEndpoint {
    base: String,
    model: String,
    key: Option<String>,
}

/// Which controller names the seeds (`service.recall`): `LLMController`
/// for the driver "llm", with its endpoint (None when unconfigured), else
/// `MechanicalController`. Both re-query with the mechanical rule.
enum Driver {
    Mechanical,
    Llm(Option<LlmEndpoint>),
}

/// `simple_complete`'s endpoint: `os.environ.get(name) or
/// memory.dream.<key>` for the base URL, the model and the API key (neither
/// `extractor_source` nor `extractor_model_override` applies, unlike the
/// dream's `resolve_endpoints`); None when the base or the model is empty.
/// An empty key sends no `Authorization` header (`Compat::new`).
fn llm_endpoint(
    dream: &crate::config::DreamConfig,
    env: &dyn Fn(&str) -> Option<String>,
) -> Option<LlmEndpoint> {
    let pick = |name: &str, cfg: &Option<String>| {
        env(name)
            .filter(|v| !v.is_empty())
            .or_else(|| cfg.clone())
            .filter(|v| !v.is_empty())
    };
    Some(LlmEndpoint {
        base: pick("PSEUDOLIFE_DREAM_BASE_URL", &dream.extractor_base_url)?,
        model: pick("PSEUDOLIFE_DREAM_MODEL", &dream.extractor_model)?,
        key: pick("PSEUDOLIFE_DREAM_API_KEY", &dream.extractor_api_key),
    })
}

/// `_seed_prompt`: the first 200 vocabulary names and the first 5 seed
/// hits (empty ones included).
fn seed_prompt(query: &str, hits: &[String], vocab: &[String]) -> String {
    let allowed = vocab
        .iter()
        .take(200)
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(", ");
    let context = hits
        .iter()
        .take(5)
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(" ");
    format!(
        "You resolve which known entities a question is about. From the ALLOWED list only, \
         return a JSON array of the entity names the question/context refers to (the subjects \
         to look up). Return [] if none.\n\nALLOWED: {allowed}\n\nQUESTION: {query}\n\n\
         CONTEXT: {context}\n\nJSON array:"
    )
}

/// `_parse_name_list`: the text from the first `[` to the last `]` as
/// `json.loads` reads it, its strings kept in order. A lone surrogate
/// escape parses to a stand-in (`py_loads::loads_reply`) where Python keeps
/// the surrogate; neither can equal a vocabulary name read from the bank.
fn parse_name_list(raw: &str) -> Vec<String> {
    let (Some(start), Some(end)) = (raw.find('['), raw.rfind(']')) else {
        return Vec::new();
    };
    if end <= start {
        return Vec::new();
    }
    match crate::mcp::py_loads::loads_reply(&raw[start..=end]) {
        Some(Value::Array(items)) => items
            .into_iter()
            .filter_map(|v| match v {
                Value::String(s) => Some(s),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// `simple_complete`'s body, `json.dumps` byte for byte.
fn completion_body(model: &str, prompt: &str) -> String {
    crate::pyjson::dumps(&json!({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 256,
        "stream": false,
    }))
}

/// The text `json.loads(bytes)` decodes: `json.detect_encoding`'s choice
/// of UTF-8 (a BOM skipped), UTF-16 or UTF-32. None where Python's decode
/// fails, and where it would keep a surrogate (`surrogatepass`), which no
/// Rust string holds: either way an empty reply.
fn json_bytes_text(b: &[u8]) -> Option<String> {
    fn utf16(b: &[u8], be: bool) -> Option<String> {
        if !b.len().is_multiple_of(2) {
            return None;
        }
        let units: Vec<u16> = b
            .chunks(2)
            .map(|c| {
                let pair = [c[0], c[1]];
                if be {
                    u16::from_be_bytes(pair)
                } else {
                    u16::from_le_bytes(pair)
                }
            })
            .collect();
        String::from_utf16(&units).ok()
    }
    fn utf32(b: &[u8], be: bool) -> Option<String> {
        if !b.len().is_multiple_of(4) {
            return None;
        }
        b.chunks(4)
            .map(|c| {
                let quad = [c[0], c[1], c[2], c[3]];
                char::from_u32(if be {
                    u32::from_be_bytes(quad)
                } else {
                    u32::from_le_bytes(quad)
                })
            })
            .collect()
    }
    let utf8 = |b: &[u8]| std::str::from_utf8(b).ok().map(str::to_string);
    if let Some(rest) = b.strip_prefix(&[0x00, 0x00, 0xFE, 0xFF]) {
        return utf32(rest, true);
    }
    if let Some(rest) = b.strip_prefix(&[0xFF, 0xFE, 0x00, 0x00]) {
        return utf32(rest, false);
    }
    if let Some(rest) = b.strip_prefix(&[0xFE, 0xFF]) {
        return utf16(rest, true);
    }
    if let Some(rest) = b.strip_prefix(&[0xFF, 0xFE]) {
        return utf16(rest, false);
    }
    if let Some(rest) = b.strip_prefix(&[0xEF, 0xBB, 0xBF]) {
        return utf8(rest);
    }
    match b {
        [0, 0, _, _, ..] => utf32(b, true),
        [0, _, _, _, ..] => utf16(b, true),
        [_, 0, 0, 0, ..] => utf32(b, false),
        [_, 0, _, _, ..] => utf16(b, false),
        [0, _] => utf16(b, true),
        [_, 0] => utf16(b, false),
        _ => utf8(b),
    }
}

/// `simple_complete`: POST the prompt to `<base>/chat/completions` through
/// the dream extractor's no-redirect client (a redirect is refused, never
/// followed, so the key reaches no other host; a URL with userinfo is
/// refused before connecting), the key only in `Authorization: Bearer`.
/// Answers `data["choices"][0]["message"]["content"] or ""`, and "" on any
/// failure.
async fn simple_complete(at: &LlmEndpoint, prompt: &str, timeout: f64) -> Value {
    let client = crate::dream::extractor::Compat::new(
        &at.base,
        &at.model,
        at.key.clone(),
        256,
        timeout,
        None,
    );
    let Some(bytes) = client
        .post_reply_bytes(completion_body(&at.model, prompt))
        .await
    else {
        return json!("");
    };
    let data = json_bytes_text(&bytes).and_then(|t| crate::mcp::py_loads::loads_reply(&t));
    match data
        .as_ref()
        .and_then(crate::dream::extractor::reply_content)
    {
        Some(c) if crate::dream::extractor::truthy(c) => c.clone(),
        _ => json!(""),
    }
}

/// `LLMController.seed_entities`: the names the model returns that are in
/// the vocabulary, in the model's order, duplicates kept (the walk drops
/// them). The call holds no lock. `_parse_name_list` runs outside
/// `simple_complete`'s `try`, so a truthy content that is no string escapes
/// as Python's AttributeError (`.find`): an internal error.
async fn llm_seed_entities(
    at: Option<&LlmEndpoint>,
    timeout: f64,
    query: &str,
    hits: &[String],
    vocab: &[String],
) -> Result<Vec<String>, ToolError> {
    let raw = match at {
        Some(at) => simple_complete(at, &seed_prompt(query, hits, vocab), timeout).await,
        None => json!(""),
    };
    let Value::String(raw) = raw else {
        return Err(internal(format!(
            "AttributeError: a non-string recall seed reply has no attribute 'find': {raw}"
        )));
    };
    let known: HashSet<&str> = vocab.iter().map(String::as_str).collect();
    Ok(parse_name_list(&raw)
        .into_iter()
        .filter(|n| {
            known.contains(n.as_str()) || crate::mutants::active("mcp-recall-llm-no-vocab-filter")
        })
        .collect())
}

/// `_select_frontier`: seeds always expand; other names under the hub
/// threshold, by ascending degree then name, cut to the budget.
fn select_frontier(frontier: &[String], seed_set: &HashSet<String>, k: &Knobs) -> Vec<String> {
    if !k.gate {
        return frontier.to_vec();
    }
    let mut seeds: Vec<String> = frontier
        .iter()
        .filter(|n| seed_set.contains(*n))
        .cloned()
        .collect();
    let mut others: Vec<String> = frontier
        .iter()
        .filter(|n| !seed_set.contains(*n))
        .cloned()
        .collect();
    if let Some(t) = k.hub_threshold
        && !crate::mutants::active("mcp-recall-no-hub-gate")
    {
        others.retain(|n| k.degree(n) < t);
    }
    others.sort_by(|a, b| (k.degree(a), a.as_str()).cmp(&(k.degree(b), b.as_str())));
    if let Some(b) = k.expand_budget {
        others = py_prefix(&others, b);
    }
    seeds.extend(others);
    seeds
}

/// `_requery_order`: by mentions in the seed hits (descending), degree
/// (ascending, 0 with the gate off), then name.
fn requery_order(newly: &[String], hits: &[String], k: &Knobs) -> Vec<String> {
    let mut keyed: Vec<(i64, i64, String)> = newly
        .iter()
        .map(|n| {
            let count = hits.iter().filter(|t| mentions(t, n)).count() as i64;
            let degree = if k.gate { k.degree(n) } else { 0 };
            (-count, degree, n.clone())
        })
        .collect();
    keyed.sort();
    keyed.into_iter().map(|(_, _, n)| n).collect()
}

/// One search as `service.search(q, top_k)`: the search guard, access
/// accrual and the retrieval event included.
async fn search(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    q: &str,
    k: &Knobs,
    ident: &Ident,
) -> Result<Vec<String>, ToolError> {
    let req = Request {
        query: py_strip(q).to_string(),
        top_k: k.top_k,
        top_k_beyond: k.top_k_beyond.clone(),
        sources: None,
        bands: None,
        tags: None,
        episodes: None,
        min_score: None,
        disable_recency_boost: false,
        rerank: None,
        bm25: None,
        count_access: true,
        header_session: ident.header_session.clone(),
        transport_session: ident.transport_session.clone(),
    };
    let _guard = crate::read::search_guard().lock().await;
    let out = search_route::run(svc, ready, req).await.map_err(internal)?;
    Ok(out
        .body
        .get("entries")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|e| {
            e.get("text")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        })
        .collect())
}

/// `run_recall` (`memory/recall.py:226-382`): the driver names the seeds;
/// the expansion and the re-queries are the same for both.
async fn run_recall(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    vocab: &[String],
    query: &str,
    driver: &Driver,
    k: &Knobs,
    ident: &Ident,
) -> Result<State, ToolError> {
    let mut st = State::default();
    let started = Instant::now();
    let out_of_time = |started: &Instant| {
        k.time_budget
            .is_some_and(|b| started.elapsed().as_secs_f64() >= b)
    };

    st.searches_issued += 1;
    let hits = search(svc, ready, query, k, ident).await?;
    for t in &hits {
        if !t.is_empty() && !st.texts.contains(t) {
            st.texts.push(t.clone());
        }
    }
    st.seed_text_count = st.texts.len();
    let seeds = match driver {
        Driver::Mechanical => seed_entities(query, &hits, vocab),
        Driver::Llm(at) => {
            llm_seed_entities(at.as_ref(), LLM_TIMEOUT_S, query, &hits, vocab).await?
        }
    };
    if seeds.is_empty() {
        st.low_confidence = true;
        return Ok(st);
    }
    let mut dedup = HashSet::new();
    st.seeds = seeds
        .into_iter()
        .filter(|s| dedup.insert(s.clone()))
        .collect();
    if st.seeds.len() as i64 > k.max_entities {
        st.seeds = py_prefix(&st.seeds, k.max_entities);
    }
    let mut seen: HashSet<String> = st.seeds.iter().cloned().collect();
    st.entities.extend(st.seeds.iter().cloned());
    for s in &st.seeds {
        st.entity_hop.push((s.clone(), 0));
    }
    let seed_set: HashSet<String> = st.seeds.iter().cloned().collect();
    let mut frontier = st.seeds.clone();
    while !frontier.is_empty() && st.iterations < k.hops && (seen.len() as i64) < k.max_entities {
        if out_of_time(&started) {
            st.truncated = true;
            break;
        }
        st.iterations += 1;
        let hop = st.iterations;
        let mut newly: Vec<String> = Vec::new();
        let mut rel_touch: HashMap<String, BTreeSet<String>> = HashMap::new();
        let mut stopped = false;
        for name in select_frontier(&frontier, &seed_set, k) {
            if out_of_time(&started) {
                st.truncated = true;
                stopped = true;
                break;
            }
            let nb = {
                let _ws = ready.write.lock().await;
                graph_neighborhood(ready, &name, 1, true, None).await?
            };
            if nb.get("found").and_then(Value::as_bool) != Some(true) {
                continue;
            }
            for node in nb["nodes"].as_array().into_iter().flatten() {
                let en = node.get("entity").and_then(Value::as_str).unwrap_or("");
                if en.is_empty() {
                    continue;
                }
                st.entity_facts.entry(en.to_string()).or_insert_with(|| {
                    node.get("facts")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default()
                });
                if seen.insert(en.to_string()) {
                    newly.push(en.to_string());
                    st.entities.push(en.to_string());
                    st.entity_hop.push((en.to_string(), hop));
                }
            }
            for ed in nb["edges"].as_array().into_iter().flatten() {
                let key = (ed.get("src"), ed.get("relation"), ed.get("dst"));
                let dup = st
                    .edges
                    .iter()
                    .any(|e| (e.get("src"), e.get("relation"), e.get("dst")) == key);
                if !dup {
                    st.edges.push(json!({"src": ed.get("src"), "relation": ed.get("relation"),
                        "dst": ed.get("dst"), "derived": ed.get("derived").cloned().unwrap_or(json!(false))}));
                    st.edge_hop.push(hop);
                }
                if k.skip_part_of {
                    let (src, dst) = (
                        ed.get("src").and_then(Value::as_str),
                        ed.get("dst").and_then(Value::as_str),
                    );
                    let rel = ed
                        .get("relation")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    if src == Some(name.as_str()) && dst.is_some_and(|d| !d.is_empty()) {
                        rel_touch
                            .entry(dst.unwrap().to_string())
                            .or_default()
                            .insert(rel);
                    } else if dst == Some(name.as_str()) && src.is_some_and(|s| !s.is_empty()) {
                        rel_touch
                            .entry(src.unwrap().to_string())
                            .or_default()
                            .insert(rel);
                    }
                }
            }
            for p in nb["paths"].as_array().into_iter().flatten() {
                if !st.paths.contains(p) {
                    st.paths.push(p.clone());
                }
            }
            if seen.len() as i64 >= k.max_entities {
                break;
            }
        }
        let mut targets = newly.clone();
        if k.skip_part_of {
            targets.retain(|n| {
                rel_touch
                    .get(n)
                    .is_none_or(|r| r.len() != 1 || !r.contains("part-of"))
            });
        }
        if let Some(cap) = k.max_searches_per_hop
            && targets.len() as i64 > cap
        {
            targets = py_prefix(&requery_order(&targets, &hits, k), cap);
        }
        if !stopped {
            for name in &targets {
                if k.max_total_searches
                    .is_some_and(|m| st.searches_issued >= m)
                {
                    st.truncated = true;
                    stopped = true;
                    break;
                }
                if out_of_time(&started) {
                    st.truncated = true;
                    stopped = true;
                    break;
                }
                st.searches_issued += 1;
                if crate::mutants::active("mcp-recall-no-requery") {
                    continue;
                }
                for t in search(svc, ready, &format!("{query} {name}"), k, ident).await? {
                    if !t.is_empty() && !st.texts.contains(&t) {
                        st.texts.push(t);
                    }
                }
            }
        }
        if stopped {
            break;
        }
        frontier = newly;
    }
    Ok(st)
}

/// `_pin_recalled_constraints`: on each seed, constraint facts first,
/// marked `pinned`.
fn pin_constraints(st: &mut State) {
    for name in st.seeds.clone() {
        let Some(facts) = st.entity_facts.get(&name) else {
            continue;
        };
        let is_pin =
            |f: &Value| f.get("distortion_tolerance").and_then(Value::as_str) == Some("constraint");
        let mut pins: Vec<Value> = facts.iter().filter(|f| is_pin(f)).cloned().collect();
        if pins.is_empty() {
            continue;
        }
        for f in &mut pins {
            f["pinned"] = json!(true);
        }
        pins.extend(facts.iter().filter(|f| !is_pin(f)).cloned());
        st.entity_facts.insert(name, pins);
    }
}

/// `_annotate_recalled_facts` with `_annotate_trace_invalidations`
/// (`service.py:9189-9294,4300-4328`): a fact matched to its slot on the
/// alias-resolved entity, attribute and value carries `re_verify` when a
/// source of it was corrected after its last confirmation.
async fn annotate(
    ready: &Ready,
    cfg: &crate::config::MemoryConfig,
    st: &mut State,
) -> Result<(), ToolError> {
    if st.entity_facts.is_empty()
        || !cfg.traces_enabled
        || crate::mutants::active("mcp-recall-no-annotate")
    {
        return Ok(());
    }
    let _ws = ready.write.lock().await;
    let client = ready.storage.client();
    let g = graph::load_graph(client).await.map_err(internal)?;
    let entities = g["entities"].as_array().cloned().unwrap_or_default();
    let amap = {
        let ents: Vec<crate::graph_read::Entity> = entities
            .iter()
            .map(|e| crate::graph_read::Entity {
                id: e["id"].as_i64().unwrap_or(0),
                canonical: e["canonical"].as_str().unwrap_or("").to_string(),
            })
            .collect();
        let al: BTreeMap<i64, Vec<String>> = g["aliases"]
            .as_object()
            .into_iter()
            .flatten()
            .filter_map(|(k, v)| {
                Some((
                    k.parse().ok()?,
                    v.as_array()?
                        .iter()
                        .filter_map(|s| s.as_str().map(str::to_string))
                        .collect(),
                ))
            })
            .collect();
        crate::graph_read::alias_canonical_map(&ents, &al)
    };
    let mut node_of: HashMap<String, String> = HashMap::new();
    for e in &entities {
        let c = e["canonical"].as_str().unwrap_or("").to_string();
        node_of.insert(c.clone(), c);
    }
    for (alias, canonical) in &amap {
        node_of
            .entry(alias.clone())
            .or_insert_with(|| canonical.clone());
    }
    for e in &entities {
        let c = e["canonical"].as_str().unwrap_or("").to_string();
        node_of
            .entry(norm_name(e["display"].as_str().unwrap_or("")))
            .or_insert(c);
    }
    type Slot = (String, String);
    let mut resolved: HashMap<(String, String, String), (Slot, f64)> = HashMap::new();
    {
        let cortex = ready.cortex.snapshot();
        for rec in cortex.current_records() {
            let n = norm_name(&rec.entity);
            let key = (
                amap.get(&n).cloned().unwrap_or(n),
                rec.attribute.clone(),
                rec.value.clone(),
            );
            let seen = if rec.last_confirmed != 0.0 {
                rec.last_confirmed
            } else {
                rec.asserted_at
            };
            let replace = resolved.get(&key).is_none_or(|prev| seen > prev.1);
            if replace {
                let slot = (
                    crate::stores::cortex::norm_key(&rec.entity),
                    crate::stores::cortex::norm_key(&rec.attribute),
                );
                resolved.insert(key, (slot, seen));
            }
        }
    }
    // (entity, fact index, slot, seen)
    let mut targets: Vec<(String, usize, Slot, f64)> = Vec::new();
    for (entity, facts) in &st.entity_facts {
        let n = norm_name(entity);
        let node = node_of.get(&n).cloned().unwrap_or(n);
        for (i, fact) in facts.iter().enumerate() {
            let key = (
                node.clone(),
                fact.get("attribute")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                fact.get("value")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
            );
            if let Some((slot, seen)) = resolved.get(&key) {
                targets.push((entity.clone(), i, slot.clone(), *seen));
            }
        }
    }
    if targets.is_empty() {
        return Ok(());
    }
    let slots: Vec<Slot> = targets
        .iter()
        .map(|t| t.2.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let inval = crate::txn::with_client(client, |c| async move {
        crate::read::cortex_search::trace_invalidations_for_slots(c, &slots).await
    })
    .await
    .map_err(internal)?;
    for (entity, i, slot, seen) in targets {
        if seen == 0.0 {
            continue;
        }
        let ids: BTreeSet<i64> = inval
            .get(&slot)
            .into_iter()
            .flatten()
            .filter(|ev| ev.cause == "source_superseded" && ev.invalidated_at > seen)
            .map(|ev| ev.source_entry_id)
            .collect();
        if ids.is_empty() {
            continue;
        }
        let count = ids.len();
        if let Some(f) = st.entity_facts.get_mut(&entity).and_then(|f| f.get_mut(i)) {
            f["re_verify"] = json!(true);
            f["re_verify_reason"] = json!(format!(
                "derived from {count} source {} corrected since this fact was last confirmed",
                if count == 1 { "memory" } else { "memories" }
            ));
        }
    }
    Ok(())
}

/// `service.recall` (`service.py:9109-9163`) and `recall_state_to_dict`
/// (`memory/recall.py:385-418`).
async fn recall(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    query: &str,
    hops: Option<i64>,
    top_k: Option<i64>,
    top_k_beyond: Option<Value>,
    ident: &Ident,
) -> Result<Map<String, Value>, ToolError> {
    let cfg = &svc.config.memory;
    let rc = &cfg.recall;
    let hops = hops.unwrap_or(rc.default_hops).clamp(1, 5);
    let top_k = top_k.unwrap_or(rc.default_top_k).max(1);
    let driver = std::env::var("PSEUDOLIFE_RECALL_DRIVER").unwrap_or_else(|_| rc.driver.clone());
    let query = py_strip(query);
    if query.is_empty() {
        let blank = json!({"query": "", "seeds": [], "entities": [], "edges": [],
            "paths": [], "texts": [], "iterations": 0, "hops": hops,
            "low_confidence": true, "entity_hop": {}, "edge_hop": [],
            "seed_text_count": 0});
        let Value::Object(m) = blank else {
            unreachable!()
        };
        return Ok(m);
    }
    let (vocab, degrees) = vocab_and_degrees(ready, rc.hub_gate).await?;
    let driver = if driver == "llm" {
        Driver::Llm(llm_endpoint(
            &svc.config.dream,
            &crate::dream::extractor::process_env,
        ))
    } else {
        Driver::Mechanical
    };
    let threshold = if rc.hub_gate {
        Some(
            hub_threshold(&degrees, rc.hub_percentile, rc.hub_floor).map_err(|e| {
                match e.strip_prefix("nan:") {
                    Some(m) => ToolError::Invalid(m.to_string()),
                    None => internal(e),
                }
            })?,
        )
    } else {
        None
    };
    // `x or None`, then `run_recall`'s `max(0, int(x or 0)) or None`.
    let positive = |x: i64| (x > 0).then_some(x);
    let knobs = Knobs {
        hops,
        top_k,
        top_k_beyond,
        max_entities: rc.max_entities,
        gate: rc.hub_gate,
        degrees,
        hub_threshold: threshold,
        expand_budget: (rc.expand_budget != 0).then_some(rc.expand_budget),
        max_searches_per_hop: positive(rc.max_searches_per_hop),
        max_total_searches: positive(rc.max_total_searches),
        time_budget: (rc.time_budget_seconds > 0.0).then_some(rc.time_budget_seconds),
        skip_part_of: rc.skip_part_of_expansion,
    };
    let mut st = run_recall(svc, ready, &vocab, query, &driver, &knobs, ident).await?;
    if cfg.cortex.pin_constraints && !crate::mutants::active("mcp-recall-no-pin") {
        pin_constraints(&mut st);
    }
    annotate(ready, cfg, &mut st).await?;

    let mut out = Map::new();
    out.insert("query".into(), json!(query));
    out.insert("seeds".into(), json!(st.seeds));
    let entities: Vec<Value> = st
        .entities
        .iter()
        .map(|n| json!({"entity": n, "facts": st.entity_facts.get(n).cloned().unwrap_or_default()}))
        .collect();
    out.insert("entities".into(), json!(entities));
    out.insert("edges".into(), json!(st.edges));
    out.insert("paths".into(), json!(st.paths));
    out.insert("texts".into(), json!(st.texts));
    out.insert("iterations".into(), json!(st.iterations));
    out.insert("hops".into(), json!(hops));
    out.insert("low_confidence".into(), json!(st.low_confidence));
    let hop_map: Map<String, Value> = st
        .entity_hop
        .iter()
        .map(|(n, h)| (n.clone(), json!(h)))
        .collect();
    out.insert("entity_hop".into(), Value::Object(hop_map));
    out.insert("edge_hop".into(), json!(st.edge_hop));
    out.insert("seed_text_count".into(), json!(st.seed_text_count));
    if st.truncated {
        out.insert("truncated".into(), json!(true));
        out.insert("searches_issued".into(), json!(st.searches_issued));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::sync::Mutex;

    // Expected values below come from running CPython 3.11 on
    // `memory/recall.py` (`_seed_prompt`, `_parse_name_list`) and on the
    // body `simple_complete` builds (`json.dumps`), 2026-10-25.

    /// The prompt for `_seed_prompt("q é", ["a", "b"], ["Web App", "pg"])`.
    const PY_SMALL_PROMPT: &str = "You resolve which known entities a question is about. From the ALLOWED list only, return a JSON array of the entity names the question/context refers to (the subjects to look up). Return [] if none.\n\nALLOWED: Web App, pg\n\nQUESTION: q \u{e9}\n\nCONTEXT: a b\n\nJSON array:";

    /// `simple_complete`'s body for model "m" and the prompt above.
    const PY_SMALL_BODY: &str = r#"{"model": "m", "messages": [{"role": "user", "content": "You resolve which known entities a question is about. From the ALLOWED list only, return a JSON array of the entity names the question/context refers to (the subjects to look up). Return [] if none.\n\nALLOWED: Web App, pg\n\nQUESTION: q \u00e9\n\nCONTEXT: a b\n\nJSON array:"}], "temperature": 0.0, "max_tokens": 256, "stream": false}"#;

    fn strings(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn seed_prompt_matches_python() {
        assert_eq!(
            seed_prompt("q é", &strings(&["a", "b"]), &strings(&["Web App", "pg"])),
            PY_SMALL_PROMPT
        );
        // The caps: 200 vocabulary names, 5 hits (an empty hit kept).
        let mut vocab: Vec<String> = (0..205).map(|i| format!("E{i}")).collect();
        vocab.push("Ünïcode".into());
        let hits = strings(&["h1", "", "h3 with \"quotes\"", "h4", "h5", "h6 dropped"]);
        let p = seed_prompt("What runs on host-1?", &hits, &vocab);
        assert_eq!(p.chars().count(), 1379);
        assert!(p.contains(", E198, E199\n\nQUESTION: What runs on host-1?\n\n"));
        assert!(!p.contains("E200"));
        assert!(p.ends_with("\n\nCONTEXT: h1  h3 with \"quotes\" h4 h5\n\nJSON array:"));
    }

    #[test]
    fn name_lists_parse_as_python() {
        let cases: [(&str, &[&str]); 11] = [
            ("", &[]),
            ("no brackets", &[]),
            ("]x[", &[]),
            ("[bad json", &[]),
            (
                r#"Sure: ["Web App", 42, null, "pg", ["x"], {"a": 1}, "Web App"] ok"#,
                &["Web App", "pg", "Web App"],
            ),
            ("[1] and [2]", &[]),
            (r#"["a"] tail ["b"]"#, &[]),
            (r#"[NaN, "x", Infinity]"#, &["x"]),
            // Python keeps the lone surrogate; its stand-in is dropped by
            // the vocabulary filter on both sides, and "y" survives on both.
            (r#"["\ud800", "y"]"#, &["\u{10f800}", "y"]),
            ("[]", &[]),
            (r#"  ["é", "\u00e9"]"#, &["é", "é"]),
        ];
        for (raw, want) in cases {
            assert_eq!(parse_name_list(raw), strings(want), "{raw:?}");
        }
    }

    #[test]
    fn request_body_matches_python_json_dumps() {
        assert_eq!(completion_body("m", PY_SMALL_PROMPT), PY_SMALL_BODY);
    }

    #[test]
    fn endpoint_resolves_env_or_config() {
        let mut d = crate::config::DreamConfig {
            extractor_base_url: Some("http://cfg/v1/".into()),
            extractor_model: Some("cfg-model".into()),
            extractor_api_key: Some("cfg-key".into()),
            // Neither applies to simple_complete.
            extractor_source: "config".into(),
            extractor_model_override: Some("override".into()),
            ..Default::default()
        };
        let none = |_: &str| None;
        let at = llm_endpoint(&d, &none).unwrap();
        assert_eq!(
            (at.base.as_str(), at.model.as_str(), at.key.as_deref()),
            ("http://cfg/v1/", "cfg-model", Some("cfg-key"))
        );
        let env = |n: &str| match n {
            "PSEUDOLIFE_DREAM_BASE_URL" => Some("http://env/v1".to_string()),
            "PSEUDOLIFE_DREAM_MODEL" => Some(String::new()),
            "PSEUDOLIFE_DREAM_API_KEY" => Some("env-key".to_string()),
            _ => None,
        };
        let at = llm_endpoint(&d, &env).unwrap();
        assert_eq!(
            (at.base.as_str(), at.model.as_str(), at.key.as_deref()),
            ("http://env/v1", "cfg-model", Some("env-key"))
        );
        // `if not base or not model: return ""`; an empty key sends none.
        d.extractor_model = Some(String::new());
        assert!(llm_endpoint(&d, &none).is_none());
        d.extractor_model = Some("m".into());
        d.extractor_base_url = None;
        assert!(llm_endpoint(&d, &none).is_none());
        d.extractor_base_url = Some("http://cfg".into());
        d.extractor_api_key = Some(String::new());
        assert_eq!(llm_endpoint(&d, &none).unwrap().key, None);
    }

    #[test]
    fn reply_bytes_decode_as_json_loads_bytes() {
        let text = r#"{"a": "é"}"#;
        assert_eq!(json_bytes_text(text.as_bytes()).as_deref(), Some(text));
        let mut bom = vec![0xEF, 0xBB, 0xBF];
        bom.extend_from_slice(text.as_bytes());
        assert_eq!(json_bytes_text(&bom).as_deref(), Some(text));
        let le: Vec<u8> = text.encode_utf16().flat_map(u16::to_le_bytes).collect();
        let be: Vec<u8> = text.encode_utf16().flat_map(u16::to_be_bytes).collect();
        assert_eq!(json_bytes_text(&le).as_deref(), Some(text));
        assert_eq!(json_bytes_text(&be).as_deref(), Some(text));
        let mut le_bom = vec![0xFF, 0xFE];
        le_bom.extend_from_slice(&le);
        assert_eq!(json_bytes_text(&le_bom).as_deref(), Some(text));
        let le32: Vec<u8> = text
            .chars()
            .flat_map(|c| (c as u32).to_le_bytes())
            .collect();
        assert_eq!(json_bytes_text(&le32).as_deref(), Some(text));
        assert_eq!(json_bytes_text(b"\xff\x00\x00"), None);
    }

    /// A one-endpoint server: records each request (head and body, as
    /// text) and answers it with `raw`, or holds the connection unanswered
    /// for a while when `raw` is empty.
    fn serve(raw: Vec<u8>) -> (String, Arc<Mutex<Vec<String>>>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let log = seen.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { return };
                let mut buf = Vec::new();
                let mut chunk = [0u8; 65536];
                loop {
                    let n = s.read(&mut chunk).unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(end) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8_lossy(&buf[..end]).to_ascii_lowercase();
                        let len = head
                            .lines()
                            .find_map(|l| l.strip_prefix("content-length:"))
                            .map_or(0, |v| v.trim().parse().unwrap_or(0));
                        if buf.len() >= end + 4 + len {
                            break;
                        }
                    }
                }
                log.lock()
                    .unwrap()
                    .push(String::from_utf8_lossy(&buf).into_owned());
                if raw.is_empty() {
                    std::thread::sleep(std::time::Duration::from_secs(5));
                } else {
                    let _ = s.write_all(&raw);
                }
            }
        });
        (format!("http://127.0.0.1:{port}"), seen)
    }

    fn answer(status_line: &str, body: &[u8], headers: &[(&str, &str)]) -> Vec<u8> {
        let mut head = format!(
            "HTTP/1.1 {status_line}\r\nContent-Length: {}\r\nConnection: close\r\n",
            body.len()
        );
        for (k, v) in headers {
            head.push_str(&format!("{k}: {v}\r\n"));
        }
        head.push_str("\r\n");
        let mut out = head.into_bytes();
        out.extend_from_slice(body);
        out
    }

    fn envelope(content: Value) -> Vec<u8> {
        crate::pyjson::dumps(&json!({"choices": [{"message": {"content": content}}]})).into_bytes()
    }

    fn at(base: &str, key: Option<&str>) -> LlmEndpoint {
        LlmEndpoint {
            base: base.into(),
            model: "m".into(),
            key: key.map(str::to_string),
        }
    }

    #[tokio::test]
    async fn the_seed_call_posts_python_bytes_with_the_key_only_when_set() {
        let (origin, seen) = serve(answer("200 OK", &envelope(json!("[\"pg\"]")), &[]));
        let base = format!("{origin}/v1/");
        let got = simple_complete(&at(&base, Some("k-1")), PY_SMALL_PROMPT, 5.0).await;
        assert_eq!(got, json!("[\"pg\"]"));
        let got = simple_complete(&at(&base, None), PY_SMALL_PROMPT, 5.0).await;
        assert_eq!(got, json!("[\"pg\"]"));
        let seen = seen.lock().unwrap().clone();
        assert_eq!(seen.len(), 2);
        for (i, req) in seen.iter().enumerate() {
            assert!(
                req.starts_with("POST /v1/chat/completions HTTP/1.1\r\n"),
                "{req}"
            );
            assert!(req.ends_with(&format!("\r\n\r\n{PY_SMALL_BODY}")), "{req}");
            let auth = req
                .lines()
                .filter(|l| l.to_ascii_lowercase().starts_with("authorization:"))
                .collect::<Vec<_>>();
            if i == 0 {
                assert_eq!(auth, ["authorization: Bearer k-1"]);
            } else {
                assert!(auth.is_empty(), "{req}");
            }
        }
    }

    #[tokio::test]
    async fn a_redirect_is_refused_and_its_target_gets_nothing() {
        let (target, target_seen) = serve(answer("200 OK", &envelope(json!("[\"pg\"]")), &[]));
        let location = format!("{target}/v1/chat/completions");
        let (origin, seen) = serve(answer("302 Found", b"", &[("Location", &location)]));
        let got = simple_complete(&at(&format!("{origin}/v1"), Some("k-1")), "p", 5.0).await;
        assert_eq!(got, json!(""));
        assert_eq!(seen.lock().unwrap().len(), 1);
        assert!(target_seen.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn every_failure_is_an_empty_reply() {
        // A refused connection, a timeout, a non-2xx answer, a body that is
        // no JSON, an envelope without content, a falsy content.
        let dead = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let dead_base = format!("http://{}/v1", dead.local_addr().unwrap());
        drop(dead);
        assert_eq!(
            simple_complete(&at(&dead_base, None), "p", 5.0).await,
            json!("")
        );
        let (hold, hold_seen) = serve(Vec::new());
        let t = Instant::now();
        assert_eq!(
            simple_complete(&at(&format!("{hold}/v1"), None), "p", 0.5).await,
            json!("")
        );
        assert!(t.elapsed().as_secs_f64() < 4.0);
        assert_eq!(hold_seen.lock().unwrap().len(), 1);
        for raw in [
            answer(
                "500 Internal Server Error",
                &envelope(json!("[\"pg\"]")),
                &[],
            ),
            answer("200 OK", b"not json", &[]),
            answer("200 OK", br#"{"choices": []}"#, &[]),
            answer("200 OK", br#"{"choices": [{"message": {}}]}"#, &[]),
            answer("200 OK", &envelope(json!(null)), &[]),
            answer("200 OK", &envelope(json!(0)), &[]),
            answer("200 OK", &envelope(json!([])), &[]),
        ] {
            let (origin, _) = serve(raw.clone());
            let got = simple_complete(&at(&format!("{origin}/v1"), None), "p", 5.0).await;
            assert_eq!(got, json!(""), "{}", String::from_utf8_lossy(&raw));
        }
        // A UTF-8 BOM is no failure: json.loads(bytes) skips it.
        let mut bom = vec![0xEF, 0xBB, 0xBF];
        bom.extend_from_slice(&envelope(json!("[\"pg\"]")));
        let (origin, _) = serve(answer("200 OK", &bom, &[]));
        let got = simple_complete(&at(&format!("{origin}/v1"), None), "p", 5.0).await;
        assert_eq!(got, json!("[\"pg\"]"));
        // userinfo in the base URL: refused before connecting.
        let (origin, seen) = serve(answer("200 OK", &envelope(json!("[\"pg\"]")), &[]));
        let base = origin.replace("http://", "http://user:pw@") + "/v1";
        assert_eq!(
            simple_complete(&at(&base, Some("k")), "p", 5.0).await,
            json!("")
        );
        assert!(seen.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn llm_seeds_keep_vocabulary_names_in_model_order() {
        let vocab = strings(&["Web App", "pg", "Redis"]);
        let reply = r#"Sure: ["Redis", "Nobody", 42, "pg", "Redis"] ok"#;
        let (origin, seen) = serve(answer("200 OK", &envelope(json!(reply)), &[]));
        let ep = at(&format!("{origin}/v1"), None);
        let got = llm_seed_entities(Some(&ep), 5.0, "q", &strings(&["hit"]), &vocab).await;
        assert_eq!(got.ok(), Some(strings(&["Redis", "pg", "Redis"])));
        let req = seen.lock().unwrap()[0].clone();
        let body = completion_body("m", &seed_prompt("q", &strings(&["hit"]), &vocab));
        assert!(req.ends_with(&body), "{req}");
        // No endpoint configured: no seeds, no request.
        let got = llm_seed_entities(None, 5.0, "q", &[], &vocab).await;
        assert_eq!(got.ok(), Some(Vec::new()));
        // A truthy non-string content escapes `_parse_name_list` as
        // Python's AttributeError does: an internal error.
        let (origin, _) = serve(answer("200 OK", &envelope(json!(5)), &[]));
        let ep = at(&format!("{origin}/v1"), None);
        let got = llm_seed_entities(Some(&ep), 5.0, "q", &[], &vocab).await;
        assert!(matches!(got, Err(ToolError::Internal(_))));
    }
}
