//! `memory_graph_review`'s mutating actions (`mcp_server.py:2366-2431`):
//! `propose`, `relate`, `dismiss_pair`, and the id actions `accept_link`,
//! `reject_link`, `accept_merge`, `accept_junk` and `reject_entity`, singly
//! or as a `proposal_ids` batch.
//!
//! Commit boundaries follow the service (`service.py`): every store call
//! commits on its own unless Python wraps several in `storage.transaction()`.
//! `accept_link` is upsert then status (two commits); `accept_merge` and
//! `accept_junk` are audit, fold or delete, then status (three);
//! `reject_link` and `reject_entity` retire the automatic decision first
//! (`confirm_human_proposal`, its own transaction), and `reject_entity` then
//! writes status, audit and pair closure as one; `dismiss_pair` writes the
//! decision index and the dismissal as one. Python's nested `_txn` blocks are
//! savepoints inside those transactions; they change no data and are not
//! reproduced. The whole call runs under the writer lock (a batch included),
//! where Python takes the service lock per service call.

use serde_json::{Map, Number, Value, json};
use tokio_postgres::Client;

use super::{
    DECISION_INDEX_KEY, DECISION_RECENT_CAP, DECISION_RECORD_PREFIX, get_meta, internal,
    merge_evidence, parse_graph, py_json, py_truthy, py_type, recent_ids, should_fold,
    version_is_one,
};
use crate::graph_review::{
    BRANCH_PREFIXES, CMD_PREFIXES, FILE_EXT, relate_relations, third_relate_relation,
};
use crate::mcp::dispatch::ToolError;
use crate::service::Ready;
use crate::storage::graph::{self, norm_name, py_lower};
use crate::storage::py_strip;

/// `service._REVIEWED_EDGE_MIN_CONF`.
const REVIEWED_EDGE_MIN_CONF: f64 = 0.7;
/// `service._JUNK_KEEP_PREFIX`.
const JUNK_KEEP_PREFIX: &str = "junk:";
/// `decided_by` for every call from the MCP surface.
const DECIDED_BY: &str = "agent";

/// `mcp_server._REVIEW_NOTES`, keyed on `reason`.
const REVIEW_NOTES: &[(&str, &str)] = &[
    (
        "bad_pair",
        "Nothing dismissed: src and dst are blank or name the same entity or slot; pass two distinct ones.",
    ),
    (
        "bad_store",
        "Nothing dismissed: store must be \"lesson\" or \"world\".",
    ),
    (
        "not_pending",
        "Nothing settled: no pending proposal has this id (already decided or unknown); list again for current ids.",
    ),
    (
        "stale_review",
        "Not applied: the finding changed after it was listed; list again and decide afresh.",
    ),
    (
        "nothing_retired",
        "Nothing restored: no retired slot matches this key or entity.",
    ),
    (
        "slot_live",
        "Nothing restored: the slot already holds a live value.",
    ),
];

fn now() -> f64 {
    crate::episodes::now()
}

// ── argument planning (mcp_server.memory_graph_review) ───────────────────

/// A proposal id as Python holds it: the bound integer, echoed back as
/// given, and its row id when it fits a BIGINT (a larger one matches no
/// row, as psycopg's NUMERIC parameter would not).
#[derive(Debug, Clone)]
pub(super) struct Pid {
    id: Option<i64>,
    echo: Value,
}

impl Pid {
    fn new(v: Value) -> Self {
        Pid {
            id: v.as_i64(),
            echo: v,
        }
    }

    /// `str(int(proposal_id))`.
    fn text(&self) -> String {
        crate::pyjson::py_str(&self.echo)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum IdAction {
    AcceptLink,
    RejectLink,
    AcceptMerge,
    AcceptJunk,
    RejectEntity,
}

impl IdAction {
    fn parse(action: &str) -> Option<Self> {
        Some(match action {
            "accept_link" => IdAction::AcceptLink,
            "reject_link" => IdAction::RejectLink,
            "accept_merge" => IdAction::AcceptMerge,
            "accept_junk" => IdAction::AcceptJunk,
            "reject_entity" => IdAction::RejectEntity,
            _ => return None,
        })
    }
}

#[derive(Debug)]
pub(super) enum Plan {
    Propose(Vec<Value>),
    Relate {
        src: String,
        relation: String,
        dst: String,
    },
    Dismiss {
        src: String,
        dst: String,
    },
    Ids {
        action: String,
        kind: IdAction,
        pids: Vec<Pid>,
        batch: bool,
    },
}

/// A truthy string argument (`if not src`), else None.
fn text_arg(a: &Map<String, Value>, k: &str) -> Option<String> {
    a.get(k)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// The tool's argument checks, in `mcp_server`'s order. `Err` is the answer
/// Python returns before calling the service.
pub(super) fn plan(action: &str, a: &Map<String, Value>) -> Result<Plan, Value> {
    match action {
        "propose" => match a.get("proposals") {
            Some(Value::Array(items)) if !items.is_empty() => Ok(Plan::Propose(items.clone())),
            _ => Err(json!({"error": "proposals_required"})),
        },
        "relate" => match (
            text_arg(a, "src"),
            text_arg(a, "dst"),
            text_arg(a, "relation"),
        ) {
            (Some(src), Some(dst), Some(relation)) => Ok(Plan::Relate { src, relation, dst }),
            _ => Err(json!({"error": "src_relation_dst_required"})),
        },
        "dismiss_pair" => match (text_arg(a, "src"), text_arg(a, "dst")) {
            (Some(src), Some(dst)) => Ok(Plan::Dismiss { src, dst }),
            _ => Err(json!({"error": "src_dst_required"})),
        },
        other => {
            let Some(kind) = IdAction::parse(other) else {
                return Err(json!({"error": "unknown_action", "actions": [
                    "list", "propose", "relate", "dismiss_pair", "dismiss_slot_pair",
                    "restore_slot", "accept_link", "reject_link", "accept_merge",
                    "accept_junk", "reject_entity"]}));
            };
            if let Some(ids) = coerce_id_list(a.get("proposal_ids")).filter(|v| !v.is_empty()) {
                return Ok(Plan::Ids {
                    action: other.to_string(),
                    kind,
                    pids: ids.into_iter().map(Pid::new).collect(),
                    batch: true,
                });
            }
            match a.get("proposal_id") {
                None | Some(Value::Null) => {
                    Err(json!({"error": "proposal_id_required", "action": other}))
                }
                Some(v) => Ok(Plan::Ids {
                    action: other.to_string(),
                    kind,
                    pids: vec![Pid::new(v.clone())],
                    batch: false,
                }),
            }
        }
    }
}

/// An integer JSON number from its decimal text (arbitrary precision).
fn int_value(digits: &str) -> Value {
    let digits = match digits.strip_prefix('-') {
        Some(rest) if rest.trim_start_matches('0').is_empty() => "0",
        _ => digits,
    };
    serde_json::from_str::<Number>(digits).map_or(Value::Null, Value::Number)
}

/// Python's `int(text)` for a `str`: surrounding whitespace, a sign, ASCII
/// digits with single underscores between them.
fn py_int_str(s: &str) -> Option<Value> {
    let t = py_strip(s);
    let (neg, body) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    if body.is_empty()
        || body.starts_with('_')
        || body.ends_with('_')
        || body.contains("__")
        || !body.chars().all(|c| c.is_ascii_digit() || c == '_')
    {
        return None;
    }
    let digits: String = body.chars().filter(|c| *c != '_').collect();
    let digits = digits.trim_start_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    Some(int_value(&if neg {
        format!("-{digits}")
    } else {
        digits.to_string()
    }))
}

/// Python's `int(value)` for a decoded JSON value, or the message of the
/// TypeError / ValueError it raises.
fn py_int(v: &Value) -> Result<Value, String> {
    match v {
        Value::Bool(b) => Ok(json!(i64::from(*b))),
        Value::Number(n) if super::is_int_literal(n) => Ok(int_value(&n.to_string())),
        Value::Number(n) => {
            let x: f64 = n.to_string().parse().unwrap_or(f64::NAN);
            if x.is_nan() {
                return Err("cannot convert float NaN to integer".into());
            }
            if x.is_infinite() {
                return Err("cannot convert float infinity to integer".into());
            }
            Ok(int_value(&format!("{:.0}", x.trunc())))
        }
        Value::String(s) => py_int_str(s).ok_or_else(|| {
            format!(
                "invalid literal for int() with base 10: {}",
                crate::storage::py_repr(s)
            )
        }),
        other => Err(format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            py_type(other)
        )),
    }
}

/// `mcp_server._coerce_id_list`: a list, or a JSON string holding one, of
/// values `int()` accepts; anything else is None.
fn coerce_id_list(value: Option<&Value>) -> Option<Vec<Value>> {
    let decoded;
    let items = match value? {
        Value::Null => return None,
        Value::String(s) => {
            decoded = crate::mcp::py_loads::loads(s)?;
            match &decoded {
                Value::Array(items) => items,
                _ => return None,
            }
        }
        Value::Array(items) => items,
        _ => return None,
    };
    items.iter().map(|v| py_int(v).ok()).collect()
}

// ── relation_quality.py ──────────────────────────────────────────────────

const PERSON: &[&str] = &["user", "the user", "i", "me", "admin", "operator"];
const RUNTIME: &[&str] = &[
    "docker",
    "docker-desktop",
    "windows",
    "windows 11",
    "windows box",
    "windows host",
    "linux",
    "wsl",
    "host",
    "vm",
    "kubernetes",
    "k8s",
    "container",
    "4090",
    "gpu",
    "cpu",
    "dx11",
    "dx12",
];
const DATASTORE: &[&str] = &[
    "postgres",
    "postgresql",
    "pg",
    "chromadb",
    "redis",
    "valkey",
    "sqlite",
    "kafka",
    "rabbitmq",
    "bank",
];

/// `re.fullmatch(r"v?\d+(?:\.\d+)*", n)`; `\d` is any Unicode decimal digit.
fn is_version(n: &str) -> bool {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| regex::Regex::new(r"^v?\d+(?:\.\d+)*$").expect("version pattern"))
        .is_match(n)
}

/// `relation_quality.infer_type`: a coarse entity type, or None.
pub(super) fn infer_type(name: &str) -> Option<&'static str> {
    let n = py_lower(py_strip(name));
    let n = n.as_str();
    if n.is_empty() {
        return None;
    }
    if CMD_PREFIXES.iter().any(|p| n.starts_with(p)) || is_version(n) {
        return Some("concept");
    }
    if n.starts_with("schema") || matches!(n, "branch" | "master" | "main") {
        return Some("concept");
    }
    if FILE_EXT.iter().any(|e| n.ends_with(e)) {
        return Some("file");
    }
    // `^(feat|...)/.+`: at least one character other than a newline.
    if BRANCH_PREFIXES.iter().any(|p| {
        n.strip_prefix(p)
            .and_then(|r| r.strip_prefix('/'))
            .and_then(|r| r.chars().next())
            .is_some_and(|c| c != '\n')
    }) {
        return Some("concept");
    }
    if PERSON.contains(&n) {
        return Some("person");
    }
    if RUNTIME.contains(&n) || n.starts_with("docker-") || n.starts_with("windows") {
        return Some("runtime");
    }
    if DATASTORE.contains(&n) || n.ends_with("-db") || n.contains("database") {
        return Some("datastore");
    }
    if n.contains("daemon")
        || n.ends_with("-service")
        || n.ends_with("-server")
        || n.ends_with("-worker")
        || n.contains("sidecar")
        || n == "gateway"
    {
        return Some("service");
    }
    let identifier = n.chars().next().is_some_and(|c| c.is_ascii_lowercase())
        && n.chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_');
    if (identifier && n.contains('_')) || n.ends_with("()") {
        return Some("tool");
    }
    None
}

/// `TYPE_CONSTRAINTS[relation]`: the allowed source and target types.
fn type_constraint(relation: &str) -> Option<(&'static [&'static str], &'static [&'static str])> {
    Some(match relation {
        "runs-on" => (
            &[
                "service",
                "process",
                "component",
                "tool",
                "file",
                "datastore",
            ][..],
            &["runtime", "host"][..],
        ),
        "hosts" => (
            &["runtime", "host"][..],
            &["service", "process", "component", "datastore"][..],
        ),
        "stores-data-in" => (
            &["service", "process", "tool"][..],
            &["datastore", "file"][..],
        ),
        "part-of" => (
            &["component", "service", "file", "datastore"][..],
            &["component", "service"][..],
        ),
        _ => return None,
    })
}

/// `relation_quality.is_hard_type_violation`.
pub(super) fn is_hard_type_violation(src: &str, relation: &str, dst: &str) -> bool {
    let Some((src_ok, dst_ok)) = type_constraint(relation) else {
        return false;
    };
    match (infer_type(src), infer_type(dst)) {
        (Some(st), Some(dt)) => !src_ok.contains(&st) || !dst_ok.contains(&dt),
        _ => false,
    }
}

/// `relation_quality.edge_confidence`.
pub(super) fn edge_confidence(src: &str, relation: &str, dst: &str) -> f64 {
    let mut base = if relation == "related-to" { 0.45 } else { 0.70 };
    if is_hard_type_violation(src, relation, dst) {
        base *= 0.25;
    }
    crate::graph_review::round3(base)
}

// ── review_decisions.py: the automatic-decision index ────────────────────

/// `_index(storage)`: the stored index normalized to its five keys, or a
/// fresh one when it is missing, not an object, or not version 1.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct Index {
    next_seq: Value,
    cursors: Map<String, Value>,
    active: Map<String, Value>,
    recent_ids: Vec<Value>,
}

impl Index {
    fn fresh() -> Self {
        Index {
            next_seq: json!(1),
            cursors: Map::new(),
            active: Map::new(),
            recent_ids: Vec::new(),
        }
    }

    /// What `_index` builds from the stored value (already `py_json`ed),
    /// raising what Python raises, in its evaluation order.
    fn from_stored(stored: Option<Value>) -> Result<Self, String> {
        let Some(Value::Object(m)) = stored else {
            return Ok(Self::fresh());
        };
        if !version_is_one(m.get("version")) {
            return Ok(Self::fresh());
        }
        // `max(1, int(value.get("next_seq", 1)))`.
        let next_seq = match m.get("next_seq") {
            None => json!(1),
            Some(v) => {
                let n = py_int(v)?;
                let text = n.to_string();
                if text.starts_with('-') || text == "0" {
                    json!(1)
                } else {
                    n
                }
            }
        };
        let cursors = py_dict(m.get("cursors"))?;
        let active = py_dict(m.get("active"))?;
        let recent_ids = recent_ids(Some(Value::Object(m)))?;
        Ok(Index {
            next_seq,
            cursors,
            active,
            recent_ids,
        })
    }

    /// The value `set_meta(_INDEX_KEY, index)` writes, in Python's key order.
    fn to_value(&self) -> Value {
        json!({
            "version": 1,
            "next_seq": self.next_seq,
            "cursors": self.cursors,
            "active": self.active,
            "recent_ids": self.recent_ids,
        })
    }

    /// `_touch_recent`.
    fn touch_recent(&mut self, decision_id: &Value) {
        self.recent_ids.retain(|v| v != decision_id);
        self.recent_ids.push(decision_id.clone());
        let skip = self.recent_ids.len().saturating_sub(DECISION_RECENT_CAP);
        self.recent_ids.drain(..skip);
    }
}

/// A dict key as `json.dumps` writes it.
fn json_key(k: &Value) -> Result<String, String> {
    match k {
        Value::String(s) => Ok(s.clone()),
        Value::Bool(b) => Ok(b.to_string()),
        Value::Null => Ok("null".into()),
        Value::Number(_) => Ok(crate::pyjson::py_str(k)),
        other => Err(format!("unhashable type: '{}'", py_type(other))),
    }
}

/// `dict(value or {})` over a decoded JSON value.
fn py_dict(v: Option<&Value>) -> Result<Map<String, Value>, String> {
    let v = match v {
        Some(v) if py_truthy(v) => v,
        _ => return Ok(Map::new()),
    };
    let length = |i: usize, n: usize| {
        format!("dictionary update sequence element #{i} has length {n}; 2 is required")
    };
    match v {
        Value::Object(m) => Ok(m.clone()),
        Value::String(_) => Err(length(0, 1)),
        Value::Array(items) => {
            let mut out = Map::new();
            for (i, item) in items.iter().enumerate() {
                let (k, val) = match item {
                    Value::Array(p) if p.len() == 2 => (p[0].clone(), p[1].clone()),
                    Value::Array(p) => return Err(length(i, p.len())),
                    Value::String(s) => {
                        let cs: Vec<char> = s.chars().collect();
                        if cs.len() != 2 {
                            return Err(length(i, cs.len()));
                        }
                        (json!(cs[0].to_string()), json!(cs[1].to_string()))
                    }
                    Value::Object(o) => {
                        let ks: Vec<&String> = o.keys().collect();
                        if ks.len() != 2 {
                            return Err(length(i, ks.len()));
                        }
                        (json!(ks[0]), json!(ks[1]))
                    }
                    _ => {
                        return Err(format!(
                            "cannot convert dictionary update sequence element #{i} to a sequence"
                        ));
                    }
                };
                out.insert(json_key(&k)?, val);
            }
            Ok(out)
        }
        other => Err(format!("'{}' object is not iterable", py_type(other))),
    }
}

async fn set_meta_body(c: &Client, key: &str, value: &Value) -> Result<(), ToolError> {
    c.execute(
        "INSERT INTO meta (key, value) VALUES ($1, $2::jsonb) \
         ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        &[&key, value],
    )
    .await
    .map_err(internal)?;
    Ok(())
}

async fn load_index(c: &Client) -> Result<Index, ToolError> {
    let stored = get_meta(c, DECISION_INDEX_KEY)
        .await
        .map_err(internal)?
        .map(py_json);
    Index::from_stored(stored).map_err(ToolError::Invalid)
}

/// `_retire`: end one automatic decision's record and drop it from the
/// active index.
async fn retire(
    c: &Client,
    index: &mut Index,
    target: &str,
    summary: &Value,
    state: &str,
    reason: &str,
) -> Result<(), ToolError> {
    let decision_id = match summary {
        Value::Object(m) => m
            .get("decision_id")
            .cloned()
            .ok_or_else(|| internal("KeyError: 'decision_id'"))?,
        Value::String(_) => {
            return Err(ToolError::Invalid(
                "string indices must be integers, not 'str'".into(),
            ));
        }
        Value::Array(_) => {
            return Err(ToolError::Invalid(
                "list indices must be integers or slices, not str".into(),
            ));
        }
        other => {
            return Err(ToolError::Invalid(format!(
                "'{}' object is not subscriptable",
                py_type(other)
            )));
        }
    };
    let Value::String(id) = &decision_id else {
        return Err(ToolError::Invalid(format!(
            "can only concatenate str (not \"{}\") to str",
            py_type(&decision_id)
        )));
    };
    let key = format!("{DECISION_RECORD_PREFIX}{id}");
    // `decision_record(...) or {}`, then `marker.update(...)`.
    let mut marker = match get_meta(c, &key).await.map_err(internal)?.map(py_json) {
        Some(Value::Object(m)) => m,
        _ => Map::new(),
    };
    marker.insert("state".into(), json!(state));
    marker.insert("ended_at".into(), json!(now()));
    marker.insert("end_reason".into(), json!(reason));
    set_meta_body(c, &key, &Value::Object(marker)).await?;
    index.touch_recent(&decision_id);
    if index.active.get(target).and_then(|s| s.get("decision_id")) == Some(&decision_id) {
        index.active.shift_remove(target);
    }
    Ok(())
}

/// `confirm_human_pair` without its transaction: retire every automatic
/// decision on the pair, then rewrite the index, even when nothing matched.
async fn confirm_human_pair_body(c: &Client, a: &str, b: &str) -> Result<(), ToolError> {
    let mut pair = [a.to_string(), b.to_string()];
    pair.sort();
    let pair = json!(pair);
    let mut index = load_index(c).await?;
    let snapshot: Vec<(String, Value)> = index
        .active
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let mut retired = 0;
    for (target, summary) in snapshot {
        let Value::Object(s) = &summary else {
            return Err(internal(format!(
                "AttributeError: '{}' object has no attribute 'get'",
                py_type(&summary)
            )));
        };
        if s.get("pair") != Some(&pair) {
            continue;
        }
        retire(
            c,
            &mut index,
            &target,
            &summary,
            "human_confirmed",
            "human confirmed pair",
        )
        .await?;
        retired += 1;
    }
    if retired == 0 && crate::mutants::active("mcp-review-pair-index-noop") {
        return Ok(());
    }
    set_meta_body(c, DECISION_INDEX_KEY, &index.to_value()).await
}

/// `confirm_human_proposal` without its transaction: the index is written
/// only when the target had an active decision.
async fn confirm_human_proposal_body(c: &Client, queue: &str, pid: &Pid) -> Result<(), ToolError> {
    let target = format!("{queue}:{}", pid.text());
    let mut index = load_index(c).await?;
    let Some(summary) = index.active.get(&target).filter(|s| py_truthy(s)).cloned() else {
        return Ok(());
    };
    retire(
        c,
        &mut index,
        &target,
        &summary,
        "human_confirmed",
        "human confirmed terminal",
    )
    .await?;
    set_meta_body(c, DECISION_INDEX_KEY, &index.to_value()).await
}

/// A tool error raised inside `txn::run`, so the transaction rolls back.
struct Tx(ToolError);

impl From<tokio_postgres::Error> for Tx {
    fn from(e: tokio_postgres::Error) -> Self {
        Tx(internal(e))
    }
}

async fn in_txn<'c, T, F, Fut>(client: &'c Client, body: F) -> Result<T, ToolError>
where
    F: FnOnce(&'c Client) -> Fut,
    Fut: std::future::Future<Output = Result<T, ToolError>>,
{
    crate::txn::run(client, |c| async move { body(c).await.map_err(Tx) })
        .await
        .map_err(|Tx(e)| e)
}

async fn reads<'c, T, F, Fut>(client: &'c Client, body: F) -> Result<T, ToolError>
where
    F: FnOnce(&'c Client) -> Fut,
    Fut: std::future::Future<Output = Result<T, ToolError>>,
{
    crate::txn::with_client(client, |c| async move { body(c).await.map_err(Tx) })
        .await
        .map_err(|Tx(e)| e)
}

// ── storage/postgres.py statements ───────────────────────────────────────

/// A float bound to a REAL column: PostgreSQL's float8-to-float4 cast,
/// which refuses an overflow or an underflow to zero.
#[allow(clippy::result_large_err)]
fn real(x: f64) -> Result<f32, ToolError> {
    let r = x as f32;
    if (r.is_infinite() && x.is_finite()) || (r == 0.0 && x != 0.0) {
        return Err(internal("value out of range for type real"));
    }
    Ok(r)
}

/// A proposal's `similarity` as psycopg binds it into the REAL column.
#[allow(clippy::result_large_err)]
fn real_param(v: Option<&Value>) -> Result<Option<f32>, ToolError> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(n)) => match n.as_i64() {
            Some(i) if super::is_int_literal(n) => Ok(Some(i as f32)),
            _ => real(n.to_string().parse().map_err(internal)?).map(Some),
        },
        // An untyped literal PostgreSQL parses as a REAL.
        Some(Value::String(s)) => {
            let t = s.trim_matches(|c: char| c.is_ascii_whitespace());
            let x: f32 = t.parse().map_err(internal)?;
            let spelled_inf = t.trim_start_matches(['+', '-']).to_ascii_lowercase();
            if x.is_infinite() && !spelled_inf.starts_with("inf") {
                return Err(internal("value out of range for type real"));
            }
            Ok(Some(x))
        }
        Some(other) => Err(internal(format!(
            "a {} cannot be stored as real",
            py_type(other)
        ))),
    }
}

/// PostgreSQL's `float8out` text (shortest digits, `%g`-style switch to
/// exponent form below 1e-4 and from 1e15).
fn pg_float8_text(x: f64) -> String {
    if x.is_nan() {
        return "NaN".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0" } else { "0" }.into();
    }
    let sci = format!("{x:e}");
    let (mant, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let sign = if mant.starts_with('-') { "-" } else { "" };
    let digits: String = mant.chars().filter(char::is_ascii_digit).collect();
    if (-4..15).contains(&exp) {
        let body = if exp < 0 {
            format!("0.{}{digits}", "0".repeat((-exp - 1) as usize))
        } else {
            let whole = exp as usize + 1;
            if digits.len() <= whole {
                format!("{digits}{}", "0".repeat(whole - digits.len()))
            } else {
                format!("{}.{}", &digits[..whole], &digits[whole..])
            }
        };
        format!("{sign}{body}")
    } else {
        let frac = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        let esign = if exp < 0 { '-' } else { '+' };
        format!(
            "{sign}{}{frac}e{esign}{:02}",
            &digits[..1],
            exp.unsigned_abs()
        )
    }
}

/// A proposal's `rationale` as psycopg binds it into the TEXT column.
#[allow(clippy::result_large_err)]
fn text_param(v: Option<&Value>) -> Result<Option<String>, ToolError> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(Value::Bool(b)) => Ok(Some(b.to_string())),
        Some(Value::Number(n)) if super::is_int_literal(n) => {
            Ok(Some(crate::pyjson::py_str(&Value::Number(n.clone()))))
        }
        Some(Value::Number(n)) => Ok(Some(pg_float8_text(
            n.to_string().parse().map_err(internal)?,
        ))),
        Some(other) => Err(internal(format!(
            "a {} cannot be stored as text",
            py_type(other)
        ))),
    }
}

/// `insert_proposal` (3400-3412). A conflict still burns the sequence value.
#[allow(clippy::too_many_arguments)]
async fn insert_proposal(
    client: &Client,
    src_id: i64,
    relation: &str,
    dst_id: i64,
    confidence: f32,
    similarity: Option<f32>,
    rationale: Option<String>,
    source: &str,
    created_at: f64,
) -> Result<Option<i64>, ToolError> {
    in_txn(client, |c| async move {
        Ok(c.query_opt(
            "INSERT INTO edge_proposals \
             (src_id, relation, dst_id, confidence, similarity, rationale, source, created_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8) \
             ON CONFLICT (src_id, relation, dst_id) DO NOTHING RETURNING id",
            &[
                &src_id,
                &relation,
                &dst_id,
                &confidence,
                &similarity,
                &rationale,
                &source,
                &created_at,
            ],
        )
        .await
        .map_err(internal)?
        .map(|r| r.get(0)))
    })
    .await
}

/// `dismiss_pair` (3414-3425) on a borrowed client.
async fn dismiss_pair_body(c: &Client, a: &str, b: &str) -> Result<bool, ToolError> {
    let mut pair = [a, b];
    pair.sort();
    Ok(c.query_opt(
        "INSERT INTO dismissed_pairs (a_norm, b_norm, dismissed_at) \
             VALUES ($1, $2, $3) ON CONFLICT DO NOTHING RETURNING a_norm",
        &[&pair[0], &pair[1], &now()],
    )
    .await
    .map_err(internal)?
    .is_some())
}

/// The `get_proposal` fields the accept path reads.
struct LinkRow {
    src_id: i64,
    relation: String,
    dst_id: i64,
    confidence: f64,
    status: String,
}

async fn get_proposal(client: &Client, pid: &Pid) -> Result<Option<LinkRow>, ToolError> {
    let Some(id) = pid.id else {
        return Ok(None);
    };
    reads(client, |c| async move {
        Ok(c.query_opt(
            "SELECT src_id, relation, dst_id, confidence::text::double precision, status \
             FROM edge_proposals WHERE id = $1",
            &[&id],
        )
        .await
        .map_err(internal)?
        .map(|r| LinkRow {
            src_id: r.get(0),
            relation: r.get(1),
            dst_id: r.get(2),
            confidence: r.get(3),
            status: r.get(4),
        }))
    })
    .await
}

/// `set_proposal_status` (3603-3630): one transaction that locks the row,
/// settles it only while pending, and closes an analyzer row's pair.
async fn set_proposal_status(
    client: &Client,
    pid: &Pid,
    status: &str,
    decided_at: f64,
) -> Result<bool, ToolError> {
    let Some(id) = pid.id else {
        return Ok(false);
    };
    in_txn(client, |c| async move {
        let Some(row) = c
            .query_opt(
                "SELECT p.source, s.canonical, d.canonical \
                 FROM edge_proposals p \
                 JOIN entities s ON s.id = p.src_id \
                 JOIN entities d ON d.id = p.dst_id \
                 WHERE p.id = $1 FOR UPDATE",
                &[&id],
            )
            .await
            .map_err(internal)?
        else {
            return Ok(false);
        };
        let n = c
            .execute(
                "UPDATE edge_proposals SET status = $1, \
                 decided_by = COALESCE($2, decided_by), \
                 decided_at = COALESCE($3, decided_at) WHERE id = $4 AND status = 'pending'",
                &[&status, &DECIDED_BY, &decided_at, &id],
            )
            .await
            .map_err(internal)?;
        let (source, a, b): (String, String, String) = (row.get(0), row.get(1), row.get(2));
        if n > 0
            && source == "analyzer"
            && matches!(status, "accepted" | "rejected" | "retyped")
            && !a.is_empty()
            && !b.is_empty()
            && a != b
        {
            let mut pair = [a, b];
            pair.sort();
            c.execute(
                "INSERT INTO dismissed_pairs \
                 (a_norm, b_norm, dismissed_at) VALUES ($1, $2, $3) \
                 ON CONFLICT DO NOTHING",
                &[&pair[0], &pair[1], &decided_at],
            )
            .await
            .map_err(internal)?;
        }
        Ok(n > 0)
    })
    .await
}

/// The `get_entity_proposal` fields the settle paths read.
struct EntityRow {
    kind: String,
    entity_id: i64,
    into_id: Option<i64>,
    score: Option<f32>,
    reason: Option<String>,
    status: String,
}

async fn get_entity_proposal(client: &Client, pid: &Pid) -> Result<Option<EntityRow>, ToolError> {
    let Some(id) = pid.id else {
        return Ok(None);
    };
    reads(client, |c| async move {
        Ok(c.query_opt(
            "SELECT kind, entity_id, into_id, score, reason, status \
             FROM entity_proposals WHERE id = $1",
            &[&id],
        )
        .await
        .map_err(internal)?
        .map(|r| EntityRow {
            kind: r.get(0),
            entity_id: r.get(1),
            into_id: r.get(2),
            score: r.get(3),
            reason: r.get(4),
            status: r.get(5),
        }))
    })
    .await
}

/// `set_entity_proposal_status` (3922-3931): no pending guard.
async fn set_entity_proposal_status_body(
    c: &Client,
    id: i64,
    status: &str,
    decided_at: f64,
) -> Result<bool, ToolError> {
    Ok(c.execute(
        "UPDATE entity_proposals SET status = $1, \
         decided_by = COALESCE($2, decided_by), \
         decided_at = COALESCE($3, decided_at) WHERE id = $4",
        &[&status, &DECIDED_BY, &decided_at, &id],
    )
    .await
    .map_err(internal)?
        > 0)
}

/// `judge_agreement.proposal_votes` over `entity_proposal_votes`' columns
/// (3962-3974): each opinion with a verdict, or None.
async fn entity_proposal_votes_body(c: &Client, id: i64) -> Result<Option<Value>, ToolError> {
    let Some(r) = c
        .query_opt(
            "SELECT judge_verdict, judge_confidence::text::double precision, judge_model, \
             judge_served_model, judge_note, \
             judge2_verdict, judge2_confidence::text::double precision, judge2_model, \
             judge2_served_model, \
             judge3_verdict, judge3_confidence::text::double precision, judge3_model, \
             judge3_served_model \
             FROM entity_proposals WHERE id = $1",
            &[&id],
        )
        .await
        .map_err(internal)?
    else {
        return Ok(None);
    };
    let opinion = |base: usize| Opinion {
        verdict: r.get(base),
        confidence: r.get(base + 1),
        model: r.get(base + 2),
        served: r.get(base + 3),
    };
    Ok(proposal_votes(
        r.get::<_, Option<String>>(4).as_deref(),
        [opinion(0), opinion(5), opinion(9)],
    ))
}

/// One judge opinion's columns on an `entity_proposals` row.
#[derive(Debug, Default)]
pub(super) struct Opinion {
    verdict: Option<String>,
    confidence: Option<f64>,
    model: Option<String>,
    served: Option<String>,
}

/// `judge_agreement.proposal_votes`: `{judge: {verdict, confidence, model,
/// [served_model,] relation}}` for each opinion with a verdict, or None.
pub(super) fn proposal_votes(note: Option<&str>, opinions: [Opinion; 3]) -> Option<Value> {
    let (rel1, rel2) = relate_relations(note);
    let rel3 = third_relate_relation(note);
    let mut out = Map::new();
    for ((name, rel), o) in [("judge", rel1), ("judge2", rel2), ("judge3", rel3)]
        .into_iter()
        .zip(opinions)
    {
        let Some(verdict) = o.verdict.filter(|v| !v.is_empty()) else {
            continue;
        };
        let (confidence, model, served) = (o.confidence, o.model, o.served);
        let mut vote = Map::new();
        vote.insert("verdict".into(), json!(verdict));
        vote.insert("confidence".into(), json!(confidence.unwrap_or(0.0)));
        vote.insert("model".into(), json!(model));
        if let Some(s) = served.filter(|s| !s.is_empty()) {
            vote.insert("served_model".into(), json!(s));
        }
        let relation = if verdict == "relate" { rel } else { None };
        vote.insert("relation".into(), json!(relation));
        out.insert(name.into(), Value::Object(vote));
    }
    (!out.is_empty()).then_some(Value::Object(out))
}

/// `record_merge_decision` (3933-3952) on a borrowed client.
#[allow(clippy::too_many_arguments)]
async fn record_merge_decision_body(
    c: &Client,
    proposal_id: i64,
    entity_display: &str,
    into_display: Option<&str>,
    status: &str,
    score: Option<f32>,
    reason: Option<&str>,
    decided_at: f64,
    votes: Option<Value>,
) -> Result<i64, ToolError> {
    Ok(c.query_one(
        "INSERT INTO merge_decisions \
         (proposal_id, entity_display, into_display, status, score, \
          reason, decided_by, decided_at, votes) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id",
        &[
            &proposal_id,
            &entity_display,
            &into_display,
            &status,
            &score,
            &reason,
            &DECIDED_BY,
            &decided_at,
            &votes,
        ],
    )
    .await
    .map_err(internal)?
    .get(0))
}

// ── service.py bodies ────────────────────────────────────────────────────

fn displays(g: &Value) -> (Map<String, Value>, Map<String, Value>) {
    let mut disp = Map::new();
    let mut canon = Map::new();
    for e in g["entities"].as_array().into_iter().flatten() {
        let id = e["id"].to_string();
        disp.insert(id.clone(), e["display"].clone());
        canon.insert(id, e["canonical"].clone());
    }
    (disp, canon)
}

/// `disp.get(eid)`: the display, or None.
fn disp_get(disp: &Map<String, Value>, id: Option<i64>) -> Value {
    id.and_then(|i| disp.get(&i.to_string()).cloned())
        .unwrap_or(Value::Null)
}

/// `disp.get(eid, "?")`.
fn disp_or_unknown(disp: &Map<String, Value>, id: Option<i64>) -> String {
    disp_get(disp, id).as_str().unwrap_or("?").to_string()
}

/// `f"{value}"` of an optional string.
fn py_fmt(s: &Option<String>) -> String {
    s.clone().unwrap_or_else(|| "None".into())
}

/// `service.graph_propose_links` (8809-8854), source `deep-dream`.
async fn propose(ready: &Ready, proposals: &[Value]) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let known: Vec<String> = match graph::load_relations(client).await.map_err(internal)? {
        Value::Array(rows) => rows
            .iter()
            .filter_map(|r| r.get("name").and_then(Value::as_str))
            .filter(|n| !matches!(*n, "prefers" | "avoids"))
            .map(str::to_string)
            .collect(),
        _ => Vec::new(),
    };
    let empty = json!("");
    let (mut proposed, mut skipped) = (0i64, 0i64);
    for p in proposals {
        let field = |k: &str| p.get(k).unwrap_or(&empty);
        let src = crate::pyjson::py_str(field("src"));
        let dst = crate::pyjson::py_str(field("dst"));
        let raw = crate::pyjson::py_str(field("relation"));
        let relation = crate::graph_read::resolve_relation(&known, &raw)
            .0
            .unwrap_or_else(|| "related-to".to_string());
        let violation = is_hard_type_violation(&src, &relation, &dst)
            && !crate::mutants::active("mcp-review-propose-keep-violations");
        if src.is_empty() || dst.is_empty() || norm_name(&src) == norm_name(&dst) || violation {
            skipped += 1;
            continue;
        }
        // Both nodes are minted before the insert: a conflicting proposal
        // still leaves them.
        let se = crate::graph_write_tools::resolve_or_create(client, &src, None).await?;
        let de = crate::graph_write_tools::resolve_or_create(client, &dst, None).await?;
        let confidence = real(edge_confidence(&src, &relation, &dst))?;
        let similarity = real_param(p.get("similarity"))?;
        let rationale = text_param(p.get("rationale"))?;
        let id = insert_proposal(
            client,
            se["id"].as_i64().unwrap_or(0),
            &relation,
            de["id"].as_i64().unwrap_or(0),
            confidence,
            similarity,
            rationale,
            "deep-dream",
            now(),
        )
        .await?;
        if id.is_some() {
            proposed += 1;
        } else {
            skipped += 1;
        }
    }
    Ok(json!({"proposed": proposed, "skipped": skipped}))
}

/// `service.graph_dismiss_duplicate` (7897-7942) from the MCP surface.
async fn dismiss_duplicate(ready: &Ready, a: &str, b: &str) -> Result<Value, ToolError> {
    let bad = || json!({"dismissed": false, "reason": "bad_pair", "a": a, "b": b});
    let (an, bn) = (norm_name(a), norm_name(b));
    if an.is_empty() || bn.is_empty() || an == bn {
        return Ok(bad());
    }
    let client = ready.storage.client();
    let g = graph::load_graph(client).await.map_err(internal)?;
    // The stored canonical by canonical or normalized display, first entity
    // (by id) wins; aliases are not consulted.
    let mut canon_by_norm: Map<String, Value> = Map::new();
    let mut canon_of: Map<String, Value> = Map::new();
    for e in g["entities"].as_array().into_iter().flatten() {
        let canonical = e["canonical"].as_str().unwrap_or("").to_string();
        let display = e["display"].as_str().unwrap_or("");
        canon_by_norm
            .entry(canonical.clone())
            .or_insert_with(|| json!(canonical));
        canon_by_norm
            .entry(norm_name(display))
            .or_insert_with(|| json!(canonical));
        canon_of.insert(e["id"].to_string(), json!(canonical));
    }
    if crate::mutants::active("mcp-review-dismiss-aliases")
        && let Some(aliases) = g["aliases"].as_object()
    {
        for (eid, names) in aliases {
            for alias in names.as_array().into_iter().flatten() {
                if let (Some(alias), Some(c)) = (alias.as_str(), canon_of.get(eid)) {
                    canon_by_norm
                        .entry(alias.to_string())
                        .or_insert_with(|| c.clone());
                }
            }
        }
    }
    let resolve = |n: String| {
        canon_by_norm
            .get(&n)
            .and_then(Value::as_str)
            .map_or(n, str::to_string)
    };
    let (an, bn) = (resolve(an), resolve(bn));
    if an == bn {
        return Ok(bad());
    }
    let new = in_txn(client, |c| async move {
        confirm_human_pair_body(c, &an, &bn).await?;
        dismiss_pair_body(c, &an, &bn).await
    })
    .await?;
    Ok(json!({"dismissed": true, "new": new, "a": a, "b": b}))
}

/// The `relate` action: `graph_relate(..., origin="agent")`, then the pair
/// dismissal.
async fn relate(ready: &Ready, src: &str, relation: &str, dst: &str) -> Result<Value, ToolError> {
    let mut args = Map::new();
    args.insert("src".into(), json!(src));
    args.insert("relation".into(), json!(relation));
    args.insert("dst".into(), json!(dst));
    args.insert("origin".into(), json!("agent"));
    let mut out = crate::graph_write_tools::relate(ready, &args).await?;
    if out.get("error").is_some_and(py_truthy) {
        return Ok(out);
    }
    let dismissed = dismiss_duplicate(ready, src, dst).await?;
    let flag = dismissed.get("dismissed").is_some_and(py_truthy);
    if let Value::Object(m) = &mut out {
        m.insert("pair_dismissed".into(), json!(flag));
    }
    Ok(out)
}

/// `{"accepted"|"rejected": False, "reason": "not_pending", "id": id}`.
fn not_pending(key: &str, pid: &Pid) -> Value {
    let mut m = Map::new();
    m.insert(key.into(), json!(false));
    m.insert("reason".into(), json!("not_pending"));
    m.insert("id".into(), pid.echo.clone());
    Value::Object(m)
}

/// `service.graph_accept_proposal` (8867-8922) without a retype.
async fn accept_link(ready: &Ready, pid: &Pid) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let Some(prop) = get_proposal(client, pid)
        .await?
        .filter(|p| p.status == "pending")
    else {
        return Ok(not_pending("accepted", pid));
    };
    // The reviewed-edge floor, stored as a confirming action.
    let confidence = if crate::mutants::active("mcp-review-accept-no-floor") {
        prop.confidence
    } else {
        prop.confidence.max(REVIEWED_EDGE_MIN_CONF)
    };
    graph::upsert_edge(
        client,
        graph::Edge {
            src_id: prop.src_id,
            relation: &prop.relation,
            dst_id: prop.dst_id,
            confidence,
            origin: Some("action"),
            revive: true,
            source_entry_ids: &[],
        },
    )
    .await
    .map_err(internal)?;
    set_proposal_status(client, pid, "accepted", now()).await?;
    let g = graph::load_graph(client).await.map_err(internal)?;
    let (disp, _) = displays(&g);
    Ok(json!({
        "accepted": true,
        "src": disp_get(&disp, Some(prop.src_id)),
        "relation": prop.relation,
        "dst": disp_get(&disp, Some(prop.dst_id)),
        "status": "accepted",
    }))
}

/// `service.graph_reject_proposal` (8930-8946): the automatic decision is
/// retired first, whatever the id names.
async fn reject_link(ready: &Ready, pid: &Pid) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    in_txn(client, |c| async move {
        confirm_human_proposal_body(c, "link", pid).await
    })
    .await?;
    let ok = set_proposal_status(client, pid, "rejected", now()).await?;
    Ok(json!({"rejected": ok, "id": pid.echo}))
}

/// A pending entity proposal of `kind`, else None.
async fn pending_entity(
    client: &Client,
    pid: &Pid,
    kind: &str,
) -> Result<Option<(i64, EntityRow)>, ToolError> {
    Ok(get_entity_proposal(client, pid)
        .await?
        .filter(|p| p.status == "pending" && p.kind == kind)
        .and_then(|p| pid.id.map(|id| (id, p))))
}

/// `service.graph_accept_entity_merge` (8965-8999): audit, fold, status.
async fn accept_merge(ready: &Ready, pid: &Pid) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let Some((id, prop)) = pending_entity(client, pid, "merge").await? else {
        return Ok(not_pending("accepted", pid));
    };
    let g = graph::load_graph(client).await.map_err(internal)?;
    let (disp, _) = displays(&g);
    let (_, edges) = parse_graph(&g);
    let facts = reads(client, |c| async move {
        super::entity_fact_counts(c).await.map_err(internal)
    })
    .await?;
    let evidence = merge_evidence(&edges, &facts);
    let (mut frm, mut into) = (Some(prop.entity_id), prop.into_id);
    if should_fold(frm, into, &evidence) && !crate::mutants::active("mcp-review-accept-no-fold") {
        std::mem::swap(&mut frm, &mut into);
    }
    let decided_at = now();
    // The audit row is written first: the proposal row CASCADEs away with
    // the folded entity.
    let (from_disp, into_disp) = (disp_or_unknown(&disp, frm), disp_or_unknown(&disp, into));
    let reason = prop.reason.clone();
    in_txn(client, |c| async move {
        let votes = entity_proposal_votes_body(c, id).await?;
        record_merge_decision_body(
            c,
            id,
            &from_disp,
            Some(&into_disp),
            "accepted",
            prop.score,
            reason.as_deref(),
            decided_at,
            votes,
        )
        .await
    })
    .await?;
    let ok = match (frm, into) {
        (Some(f), Some(i)) => graph::merge_entity(client, f, i).await.map_err(internal)?,
        _ => false,
    };
    in_txn(client, |c| async move {
        set_entity_proposal_status_body(c, id, "accepted", decided_at).await
    })
    .await?;
    Ok(json!({"accepted": ok, "from": disp_get(&disp, frm), "into": disp_get(&disp, into)}))
}

/// `service.graph_accept_entity_junk` (9006-9032): audit, delete, status.
async fn accept_junk(ready: &Ready, pid: &Pid) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let Some((id, prop)) = pending_entity(client, pid, "junk").await? else {
        return Ok(not_pending("accepted", pid));
    };
    let g = graph::load_graph(client).await.map_err(internal)?;
    let (disp, _) = displays(&g);
    let display = disp_or_unknown(&disp, Some(prop.entity_id));
    let reason = format!("junk: {}", py_fmt(&prop.reason));
    in_txn(client, |c| async move {
        let votes = entity_proposal_votes_body(c, id).await?;
        record_merge_decision_body(
            c,
            id,
            &display,
            None,
            "accepted",
            prop.score,
            Some(&reason),
            now(),
            votes,
        )
        .await
    })
    .await?;
    let ok = graph::delete_entity(client, prop.entity_id)
        .await
        .map_err(internal)?;
    in_txn(client, |c| async move {
        set_entity_proposal_status_body(c, id, "accepted", now()).await
    })
    .await?;
    Ok(json!({"accepted": ok, "entity": disp_get(&disp, Some(prop.entity_id))}))
}

/// `service.graph_reject_entity_proposal` (9039-9083): retire the automatic
/// decision, then status, audit and pair closure in one transaction.
async fn reject_entity(ready: &Ready, pid: &Pid) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let decided_at = now();
    let prop = get_entity_proposal(client, pid).await?;
    if let Some(p) = &prop {
        let kind = p.kind.clone();
        in_txn(client, |c| async move {
            confirm_human_proposal_body(c, &kind, pid).await
        })
        .await?;
    }
    let (Some(prop), Some(id)) = (prop.filter(|p| p.status == "pending"), pid.id) else {
        return Ok(not_pending("rejected", pid));
    };
    let ok = in_txn(client, |c| async move {
        let votes = entity_proposal_votes_body(c, id).await?;
        let ok = set_entity_proposal_status_body(c, id, "rejected", decided_at).await?;
        if !ok {
            return Ok(false);
        }
        let g = graph::load_graph_body(c).await.map_err(internal)?;
        let (disp, canon) = displays(&g);
        let canon_get = |id: Option<i64>| {
            id.and_then(|i| canon.get(&i.to_string()))
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string)
        };
        let skip_dismiss = crate::mutants::active("mcp-review-reject-no-dismiss");
        if prop.kind == "merge" {
            record_merge_decision_body(
                c,
                id,
                &disp_or_unknown(&disp, Some(prop.entity_id)),
                Some(&disp_or_unknown(&disp, prop.into_id)),
                "rejected",
                prop.score,
                prop.reason.as_deref(),
                decided_at,
                votes,
            )
            .await?;
            if let (Some(a), Some(b)) = (canon_get(Some(prop.entity_id)), canon_get(prop.into_id))
                && a != b
                && !skip_dismiss
            {
                dismiss_pair_body(c, &a, &b).await?;
            }
        } else if prop.kind == "junk" {
            record_merge_decision_body(
                c,
                id,
                &disp_or_unknown(&disp, Some(prop.entity_id)),
                None,
                "rejected",
                prop.score,
                Some(&format!("junk keep: {}", py_fmt(&prop.reason))),
                decided_at,
                votes,
            )
            .await?;
            if let Some(canonical) = canon_get(Some(prop.entity_id))
                && !skip_dismiss
            {
                let key = format!("{JUNK_KEEP_PREFIX}{canonical}");
                dismiss_pair_body(c, &key, &key).await?;
            }
        }
        Ok(true)
    })
    .await?;
    Ok(json!({"rejected": ok, "id": pid.echo}))
}

/// `_noted(out, _REVIEW_NOTES)`: a known `reason` gains its `note`.
fn noted(mut out: Value) -> Value {
    if let Value::Object(m) = &mut out
        && let Some(Value::String(code)) = m.get("reason")
        && let Some((_, note)) = REVIEW_NOTES.iter().find(|(c, _)| c == code)
        && !m.contains_key("note")
    {
        m.insert("note".into(), json!(note));
    }
    out
}

async fn settle(ready: &Ready, kind: IdAction, pid: &Pid) -> Result<Value, ToolError> {
    match kind {
        IdAction::AcceptLink => accept_link(ready, pid).await,
        IdAction::RejectLink => reject_link(ready, pid).await,
        IdAction::AcceptMerge => accept_merge(ready, pid).await,
        IdAction::AcceptJunk => accept_junk(ready, pid).await,
        IdAction::RejectEntity => reject_entity(ready, pid).await,
    }
}

/// `{"action", "settled", "results"}`: a result settles when its
/// `accepted` or `rejected` is truthy.
fn batch_answer(action: &str, results: Vec<Value>) -> Value {
    let settled = results
        .iter()
        .filter(|r| {
            ["accepted", "rejected"]
                .iter()
                .any(|k| r.get(*k).is_some_and(py_truthy))
        })
        .count();
    json!({"action": action, "settled": settled, "results": results})
}

/// Run a planned action under the writer lock.
pub(super) async fn run(ready: &Ready, plan: Plan) -> Result<Value, ToolError> {
    match plan {
        Plan::Propose(proposals) => propose(ready, &proposals).await,
        Plan::Relate { src, relation, dst } => relate(ready, &src, &relation, &dst).await,
        Plan::Dismiss { src, dst } => Ok(noted(dismiss_duplicate(ready, &src, &dst).await?)),
        Plan::Ids {
            action,
            kind,
            pids,
            batch,
        } => {
            let mut results = Vec::with_capacity(pids.len());
            for pid in &pids {
                results.push(noted(settle(ready, kind, pid).await?));
            }
            if batch {
                Ok(batch_answer(&action, results))
            } else {
                Ok(results.pop().unwrap_or(Value::Null))
            }
        }
    }
}

#[cfg(test)]
#[path = "graph_review_actions_tests.rs"]
mod tests;
