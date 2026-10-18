//! `memory_supersede` and `memory_reinstate` (spec CO1-CO10, RI1-RI9):
//! `mcp_server.memory_supersede` → `service.supersede` →
//! `_apply_correction_locked`, and `mcp_server.memory_reinstate` →
//! `service.reinstate` → `PostgresStorage.reinstate_entry`.
//!
//! A correction retires its target and stores the replacement in one
//! transaction (`_stage_correction_locked` under `storage.transaction()`),
//! then publishes the result to the resident bank. Python stages a clone of
//! the bank, commits, and publishes the clone; this daemon builds the new
//! entry first (`store::admit`, nothing resident changes), commits, and then
//! applies the same changes in place. Both leave the resident bank untouched
//! when the transaction rolls back.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::config::MemoryConfig;
use crate::episodes::{self, Caller};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFuture};
use crate::service::Ready;
use crate::store::{self, Admit, NewEntry, py_strip, round4};

/// `DERIVED_FLAGGED_CAP` (`service.py:453`).
const DERIVED_FLAGGED_CAP: usize = 50;
/// `_AUTHORITY_RANK` (`memory/labels.py:254`).
const AUTHORITY_RANK: [(&str, u8); 3] = [("quoted", 3), ("directive", 2), ("observation", 1)];
/// `_DISTORTION_RANK` (`memory/labels.py:255-256`).
const DISTORTION_RANK: [(&str, u8); 5] = [
    ("constraint", 5),
    ("procedural", 4),
    ("belief", 3),
    ("preference", 2),
    ("episodic", 1),
];
const RESOLVE_NOTE: &str =
    "No entries changed. Resolve the reported targets and resubmit exact IDs or unique texts.";

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

fn from_store(e: store::ToolError) -> ToolError {
    match e {
        store::ToolError::Invalid(text) => ToolError::Invalid(text),
        store::ToolError::Internal(text) => internal(text),
    }
}

/// The tools this module serves on `/mcp` (`mcp::dispatch::registry`).
pub fn mcp_tools() -> Vec<(&'static str, crate::mcp::dispatch::ToolFn)> {
    vec![
        ("memory_supersede", mcp_memory_supersede),
        ("memory_reinstate", mcp_memory_reinstate),
    ]
}

/// `_strictest` (`memory/labels.py:259-265`): the highest-ranked label, the
/// first of equals; unknown values rank nothing.
fn strictest<'a>(
    values: impl Iterator<Item = Option<&'a str>>,
    rank: &[(&str, u8)],
) -> Option<String> {
    let mut best: Option<&str> = None;
    let mut best_rank = 0u8;
    for v in values {
        let r = v
            .and_then(|v| rank.iter().find(|(name, _)| *name == v))
            .map_or(0, |(_, r)| *r);
        if r > best_rank {
            best = v;
            best_rank = r;
        }
    }
    best.map(str::to_string)
}

/// `_correction_noop` (`service.py:2250-2263`) as `_correction_noted`
/// (`mcp_server.py:753-760`) frames it: the sentence moves to `note`.
fn noop(reason: &str, note: &str, target_errors: Vec<Value>) -> Value {
    json!({
        "superseded_count": 0,
        "superseded_texts": [],
        "superseded_ids": [],
        "new_memory_stored": false,
        "derived_flagged": [],
        "derived_flagged_total": 0,
        "derived_flagged_truncated": false,
        "reason": reason,
        "target_errors": target_errors,
        "note": note,
    })
}

/// A resolved correction target.
struct Target {
    id: i64,
    text: String,
    authority: Option<String>,
    distortion: Option<String>,
}

/// A JSON integer that a `StrictInt` bound: whether it is positive, and its
/// value when it fits a row id. Python's `int` has no bound; past BIGINT no
/// row matches.
fn int_value(v: &Value) -> (bool, Option<i64>) {
    match v.as_i64() {
        Some(i) => (i > 0, Some(i)),
        None => (!v.to_string().starts_with('-'), None),
    }
}

/// `_resolve_correction_targets_locked` (`service.py:3104-3199`) for one
/// selector: the target, or the no-op answer. No write, no retrieval effect.
async fn resolve_target(
    ready: &Ready,
    cfg: &MemoryConfig,
    old_text: Option<&str>,
    entry_id: Option<&Value>,
) -> Result<Target, Value> {
    let by_id = match (old_text, entry_id) {
        (Some(_), None) => false,
        (None, Some(_)) => true,
        _ => {
            return Err(noop(
                "invalid_selector",
                "Supply exactly one of entry IDs or exact texts.",
                vec![],
            ));
        }
    };
    let mut id = None;
    if let Some(v) = entry_id {
        let (positive, fits) = int_value(v);
        if !positive {
            return Err(noop(
                "invalid_selector",
                "Entry IDs must be positive integers, not coerced values.",
                vec![],
            ));
        }
        id = fits;
    }
    if let Some(t) = old_text
        && py_strip(t).is_empty()
    {
        return Err(noop(
            "empty_input",
            "Exact-text targets must be non-empty.",
            vec![],
        ));
    }

    let outcome: Result<Target, Map<String, Value>> = {
        let bank = ready.bank.read().expect("bank lock");
        // Python walks `band.entries` band by band; the bank is one list.
        let band_pos = |name: &str| cfg.bands.iter().position(|b| b == name).unwrap_or(0);
        let mut matches: Vec<usize> = (0..bank.entries.len())
            .filter(|&i| {
                let e = &bank.entries[i];
                if by_id {
                    id == Some(e.id)
                } else {
                    old_text == Some(e.text.as_str())
                }
            })
            .collect();
        matches.sort_by_key(|&i| (band_pos(&bank.entries[i].band), i));
        let live: Vec<usize> = matches
            .iter()
            .copied()
            .filter(|&i| bank.entries[i].superseded_at.is_none())
            .collect();
        let mut error = Map::new();
        if by_id {
            error.insert("entry_id".into(), entry_id.cloned().unwrap_or(Value::Null));
        } else {
            error.insert("text".into(), json!(old_text));
        }
        // A retired twin is history, not a rival target, for an exact text;
        // an id must map to exactly one resident.
        let ambiguous = if by_id {
            matches.len() != 1
        } else {
            live.len() > 1 && !crate::mutants::active("mcp-supersede-text-first-live")
        };
        if matches.is_empty() {
            error.insert("reason".into(), json!("target_not_found"));
            Err(error)
        } else if ambiguous {
            error.insert("reason".into(), json!("ambiguous_target"));
            let candidates: Vec<Value> = matches
                .iter()
                .map(|&i| {
                    let e = &bank.entries[i];
                    json!({
                        "id": e.id,
                        "source": e.source,
                        "episode_id": e.episode_id,
                        "superseded": e.superseded_at.is_some(),
                    })
                })
                .collect();
            error.insert("candidates".into(), Value::Array(candidates));
            Err(error)
        } else if live.is_empty() {
            error.insert("reason".into(), json!("target_superseded"));
            error.insert(
                "superseded_by_text".into(),
                json!(bank.entries[matches[0]].superseded_by_text),
            );
            Err(error)
        } else {
            let e = &bank.entries[live[0]];
            Ok(Target {
                id: e.id,
                text: e.text.clone(),
                authority: e.authority.clone(),
                distortion: e.distortion_tolerance.clone(),
            })
        }
    };
    let target = match outcome {
        Ok(t) => t,
        Err(error) => {
            let reason = error["reason"].as_str().unwrap_or_default().to_string();
            return Err(noop(&reason, RESOLVE_NOTE, vec![Value::Object(error)]));
        }
    };
    let holders = {
        let bank = ready.bank.read().expect("bank lock");
        bank.entries.iter().filter(|e| e.id == target.id).count()
    };
    if holders != 1 {
        return Err(noop(
            "ambiguous_target",
            "A selected row ID has multiple resident entries; retry after recovery.",
            vec![],
        ));
    }

    // A failed insert can leave a phantom row id resident; never correct it.
    let ids = vec![target.id];
    let rows = ready
        .storage
        .client()
        .query("SELECT id FROM entries WHERE id = ANY($1)", &[&ids])
        .await;
    let Ok(rows) = rows else {
        return Err(noop(
            "target_unavailable",
            "Could not verify selected rows; retry when storage is available.",
            vec![],
        ));
    };
    let existing: HashSet<i64> = rows.iter().map(|r| r.get(0)).collect();
    if !existing.contains(&target.id) {
        return Err(noop(
            "target_unavailable",
            "Selected rows are no longer persisted; reselect after recovery.",
            vec![json!({"entry_id": target.id, "reason": "target_unavailable"})],
        ));
    }
    Ok(target)
}

/// Why a correction transaction did not commit.
enum Fail {
    Db(tokio_postgres::Error),
    /// A `ValueError` from `supersede_entries`.
    Value(String),
    /// `_ReplacementRejected`: the meta filter refused the replacement.
    Rejected,
}

impl From<tokio_postgres::Error> for Fail {
    fn from(e: tokio_postgres::Error) -> Self {
        Fail::Db(e)
    }
}

/// `_derived_from_entries_locked` (`service.py:4359-4405`): the slots the
/// source entries helped form, live slots first, then by normalized key,
/// named by their current records, else any record, else the key itself.
fn derived_rows(ready: &Ready, traces: &[(String, String, i64)]) -> Vec<Value> {
    use crate::stores::cortex::norm_key;
    let mut by_slot: BTreeMap<(String, String), Vec<i64>> = BTreeMap::new();
    for (e, a, id) in traces {
        by_slot.entry((e.clone(), a.clone())).or_default().push(*id);
    }
    let cortex = ready.cortex.snapshot();
    let mut disp: HashMap<(String, String), (String, String)> = HashMap::new();
    let mut live: HashSet<(String, String)> = HashSet::new();
    for r in cortex.current_records() {
        let key = (norm_key(&r.entity), norm_key(&r.attribute));
        disp.entry(key.clone())
            .or_insert_with(|| (r.entity.clone(), r.attribute.clone()));
        live.insert(key);
    }
    if !crate::mutants::active("mcp-supersede-derived-current-only") {
        for r in cortex.records() {
            disp.entry((norm_key(&r.entity), norm_key(&r.attribute)))
                .or_insert_with(|| (r.entity.clone(), r.attribute.clone()));
        }
    }
    let mut slots: Vec<((String, String), Vec<i64>)> = by_slot.into_iter().collect();
    slots.sort_by(|(a, _), (b, _)| (!live.contains(a), a).cmp(&(!live.contains(b), b)));
    slots
        .into_iter()
        .map(|(key, mut src)| {
            src.sort_unstable();
            src.dedup();
            let (entity, attribute) = disp.get(&key).cloned().unwrap_or_else(|| key.clone());
            json!({
                "entity": entity,
                "attribute": attribute,
                "has_current_value": live.contains(&key),
                "source_entry_ids": src,
            })
        })
        .collect()
}

/// `memory_supersede` on the MCP surface: bound arguments and the caller's
/// session into the body, which runs to completion in its own task.
fn mcp_memory_supersede<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let caller = Caller {
            header_session: call.ident.header_session.clone(),
            transport_session: call.ident.transport_session.clone(),
        };
        let cfg = app.service.config.memory.clone();
        let args = call.args.clone();
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { supersede(&r2, &cfg, &caller, &args).await })
            .await
            .map_err(internal)?
    })
}

/// `service.supersede` (`service.py:3201-3229`) through
/// `_apply_correction_under_mutation_lock_locked` (`:2927-3102`).
async fn supersede(
    ready: &Arc<Ready>,
    cfg: &MemoryConfig,
    caller: &Caller,
    args: &Map<String, Value>,
) -> Result<Value, ToolError> {
    let mut ws = ready.write.lock().await;
    let new_text = py_strip(args.get("new_text").and_then(Value::as_str).unwrap_or(""));
    if new_text.is_empty() {
        return Ok(noop(
            "empty_input",
            "Replacement text must be non-empty.",
            vec![],
        ));
    }
    let old_text = args.get("old_text").and_then(Value::as_str);
    let entry_id = args.get("entry_id").filter(|v| !v.is_null());
    let target = match resolve_target(ready, cfg, old_text, entry_id).await {
        Ok(t) => t,
        Err(answer) => return Ok(answer),
    };

    // `_inherited_labels` (`service.py:682-692`): the new text's own label
    // wins its axis, else the strictest label across the replaced entries.
    let authority = crate::labels::infer_authority(new_text)
        .map(str::to_string)
        .or_else(|| strictest([target.authority.as_deref()].into_iter(), &AUTHORITY_RANK));
    let distortion = crate::labels::infer_distortion_tolerance(new_text)
        .map(str::to_string)
        .or_else(|| strictest([target.distortion.as_deref()].into_iter(), &DISTORTION_RANK));
    let embedder = ready.embedder.clone();
    let owned = new_text.to_string();
    let row = tokio::task::spawn_blocking(move || embedder.embed(&owned))
        .await
        .map_err(internal)?
        .map_err(internal)?;
    let session = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
    let ids = vec![target.id];
    let new = NewEntry {
        text: new_text,
        row,
        source: "correction",
        tags: Vec::new(),
        session_key: session.as_deref(),
        attribution: None,
        authority,
        distortion,
    };
    // `stamp = time.time()` precedes the replacement's store (`ts`).
    let stamp = episodes::now();
    let bypass = !crate::mutants::active("mcp-supersede-surprise-gate");
    let admission = match store::admit(ready, cfg, &ws, new, bypass, &ids)
        .await
        .map_err(from_store)?
    {
        Admit::Admitted(adm) => Some(adm),
        Admit::Filtered | Admit::BelowGate { .. } => None,
    };
    let victim = admission.as_ref().and_then(|adm| {
        let bank = ready.bank.read().expect("bank lock");
        adm.victim.map(|i| {
            let e = &bank.entries[i];
            (
                e.id,
                e.source.clone(),
                e.superseded_at.is_some() || ids.contains(&e.id),
            )
        })
    });

    let traces_on = cfg.traces_enabled;
    let client = ready.storage.client();
    let committed = {
        let (ids, adm, victim) = (&ids, admission.as_ref(), victim.as_ref());
        crate::txn::run(client, |c| async move {
            // `supersede_entries` (`storage/postgres.py:962-1029`).
            let rows = c
                .query(
                    "SELECT id, superseded_at FROM entries \
                     WHERE id = ANY($1) ORDER BY id FOR UPDATE",
                    &[ids],
                )
                .await?;
            let found: HashSet<i64> = rows.iter().map(|r| r.get(0)).collect();
            let missing: Vec<String> = ids
                .iter()
                .filter(|i| !found.contains(i))
                .map(i64::to_string)
                .collect();
            let retired: Vec<String> = rows
                .iter()
                .filter(|r| r.get::<_, Option<f64>>(1).is_some())
                .map(|r| r.get::<_, i64>(0).to_string())
                .collect();
            if !missing.is_empty() || !retired.is_empty() {
                let mut detail = Vec::new();
                if !missing.is_empty() {
                    detail.push(format!("missing=[{}]", missing.join(", ")));
                }
                if !retired.is_empty() {
                    detail.push(format!("already_retired=[{}]", retired.join(", ")));
                }
                return Err(Fail::Value(format!(
                    "supersede_entries: {}",
                    detail.join(", ")
                )));
            }
            c.execute(
                "UPDATE entries SET superseded_at = $1, superseded_by_text = $2 \
                 WHERE id = ANY($3)",
                &[&stamp, &new_text, ids],
            )
            .await?;
            if !crate::mutants::active("mcp-supersede-no-trace-invalidation") {
                c.execute(
                    "INSERT INTO memory_trace_invalidations \
                     (entity_norm, attribute_norm, source_entry_id, invalidated_at, cause) \
                     SELECT entity_norm, attribute_norm, entry_id, $1, 'source_superseded' \
                     FROM memory_traces WHERE entry_id = ANY($2) \
                     ON CONFLICT (entity_norm, attribute_norm, source_entry_id) DO NOTHING",
                    &[&stamp, ids],
                )
                .await?;
            }
            let traces: Vec<(String, String, i64)> = if traces_on {
                c.query(
                    "SELECT entity_norm, attribute_norm, entry_id FROM memory_traces \
                     WHERE entry_id = ANY($1) ORDER BY entity_norm, attribute_norm, entry_id",
                    &[ids],
                )
                .await?
                .iter()
                .map(|r| (r.get(0), r.get(1), r.get(2)))
                .collect()
            } else {
                Vec::new()
            };
            let adm = adm.ok_or(Fail::Rejected)?;
            if let Some((vid, vsource, vsup)) = victim {
                store::delete_evicted_rows(c, *vid, vsource, *vsup).await?;
            }
            let id = store::insert_entry_row(c, &adm.entry, &adm.row).await?;
            Ok((traces, id))
        })
        .await
    };
    let (traces, new_id) = match committed {
        Ok(v) => v,
        Err(Fail::Rejected) => {
            return Ok(noop(
                "replacement_rejected",
                "Replacement was rejected; no source entries were retired.",
                vec![],
            ));
        }
        Err(Fail::Value(text)) => return Err(ToolError::Invalid(text)),
        Err(Fail::Db(e)) => return Err(internal(e)),
    };
    let mut adm = admission.expect("a committed correction stored its replacement");

    // Publication: what the staged bank holds after commit.
    store::record_surprise(&mut ws, cfg, &adm.per_band);
    {
        let mut bank = ready.bank.write().expect("bank lock");
        if let Some((vid, vsource, _)) = &victim {
            if let Some(i) = bank.entries.iter().position(|e| e.id == *vid) {
                bank.remove(i);
            }
            crate::read::stats::record_true_drop();
            eprintln!("capacity eviction true drop: entry_id={vid} source={vsource:?}");
        }
        if !crate::mutants::active("mcp-supersede-resident-stale") {
            for e in bank.entries.iter_mut().filter(|e| ids.contains(&e.id)) {
                e.superseded_at = Some(stamp);
                e.superseded_by_text = Some(new_text.to_string());
            }
        }
        adm.entry.id = new_id;
        let unit = std::mem::take(&mut adm.unit);
        bank.push(adm.entry, &unit);
    }
    if let Some(a) = adm.anchor {
        ws.last_entity_seen = Some(a);
    }
    ws.interaction_count += 1;

    let derived = derived_rows(ready, &traces);
    let total = derived.len();
    let capped: Vec<Value> = derived.into_iter().take(DERIVED_FLAGGED_CAP).collect();
    let truncated = capped.len() < total;
    Ok(json!({
        "superseded_count": 1,
        "superseded_texts": [target.text],
        "superseded_ids": ids,
        "new_memory_stored": true,
        "new_memory_surprise": round4(adm.overall),
        "derived_flagged": capped,
        "derived_flagged_total": total,
        "derived_flagged_truncated": truncated,
    }))
}

// ── memory_reinstate ─────────────────────────────────────────────────────

/// `int(text, 16)` (`Objects/longobject.c`): surrounding whitespace, a sign,
/// a `0x` prefix, single underscores between digits; Unicode decimal digits
/// count as their ASCII values.
fn py_int16(text: &str) -> bool {
    let t = py_strip(text);
    let t = t.strip_prefix(['+', '-']).unwrap_or(t);
    let (t, prefixed) = match t.strip_prefix("0x").or_else(|| t.strip_prefix("0X")) {
        Some(rest) => (rest, true),
        None => (t, false),
    };
    let digit = |c: char| c.is_ascii_hexdigit() || unicode_nd(c);
    let mut prev_underscore = !prefixed;
    let mut digits = 0usize;
    for c in t.chars() {
        if c == '_' {
            if prev_underscore {
                return false;
            }
            prev_underscore = true;
        } else if digit(c) {
            digits += 1;
            prev_underscore = false;
        } else {
            return false;
        }
    }
    digits > 0 && !t.ends_with('_')
}

/// A Unicode decimal digit (category Nd), which `int()` reads as its value.
fn unicode_nd(c: char) -> bool {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"^\p{Nd}$").expect("static pattern"));
    re.is_match(c.encode_utf8(&mut [0; 4]))
}

/// `uuid.UUID(text)` succeeds (`Lib/uuid.py`): `urn:` and `uuid:` removed,
/// braces stripped from the ends, hyphens removed, 32 characters, base 16.
fn py_uuid_parses(text: &str) -> bool {
    let hex = text.replace("urn:", "").replace("uuid:", "");
    let hex = hex.trim_matches(['{', '}']).replace('-', "");
    hex.chars().count() == 32 && py_int16(&hex)
}

/// `str(uuid.UUID(text)) == text`: lowercase, hyphenated 8-4-4-4-12.
fn is_canonical_uuid(text: &str) -> bool {
    let b = text.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, &c)| match i {
            8 | 13 | 18 | 23 => c == b'-',
            _ => c.is_ascii_digit() || (b'a'..=b'f').contains(&c),
        })
}

/// `json.dumps(s, ensure_ascii=False)` for a string.
fn json_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// The validated reinstatement request (`_reinstatement_request`,
/// `storage/postgres.py:1031-1084`).
struct Request {
    entry_id: Option<i64>,
    /// The id as bound, echoed in a replay's answer.
    raw_id: Value,
    operation_id: String,
    sha256: String,
    text_sha256: String,
    source_sha256: String,
    superseded_at: f64,
    superseded_by_text_sha256: String,
    evidence_sha256: String,
    reviewer_ids: Value,
    reason: String,
    decided_by: String,
}

fn invalid(detail: &str) -> ToolError {
    ToolError::Invalid(format!("invalid_request: {detail}"))
}

#[allow(clippy::result_large_err)]
/// `_reinstatement_request`: validate in Python's order, then fingerprint
/// the canonical JSON payload.
fn reinstatement_request(
    args: &Map<String, Value>,
    decided_by: &str,
) -> Result<Request, ToolError> {
    let s = |k: &str| {
        args.get(k)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    let raw_id = args.get("entry_id").cloned().unwrap_or(Value::Null);
    let (positive, entry_id) = int_value(&raw_id);
    if !positive {
        return Err(invalid("entry_id must be a positive integer"));
    }
    let operation_id = s("operation_id");
    if !is_canonical_uuid(&operation_id) {
        return Err(invalid(if py_uuid_parses(&operation_id) {
            "operation_id must be canonical"
        } else {
            "operation_id must be a UUID"
        }));
    }
    let hashes = [
        ("expected_text_sha256", s("expected_text_sha256")),
        ("expected_source_sha256", s("expected_source_sha256")),
        (
            "expected_superseded_by_text_sha256",
            s("expected_superseded_by_text_sha256"),
        ),
        ("evidence_packet_sha256", s("evidence_packet_sha256")),
    ];
    let lower_hex =
        |v: &str| v.len() == 64 && v.bytes().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'));
    if hashes.iter().any(|(_, v)| !lower_hex(v)) {
        return Err(invalid("hashes must be lowercase SHA-256"));
    }
    let superseded_at = args
        .get("expected_superseded_at")
        .and_then(crate::mcp::binding::float_arg)
        .filter(|x| x.is_finite())
        .ok_or_else(|| invalid("expected_superseded_at must be finite"))?;
    let reviewers = args.get("reviewer_ids").cloned().unwrap_or(Value::Null);
    let ok_reviewers = reviewers.as_array().is_some_and(|items| {
        !items.is_empty()
            && items
                .iter()
                .all(|v| v.as_str().is_some_and(|s| !py_strip(s).is_empty()))
    });
    if !ok_reviewers {
        return Err(invalid("reviewer_ids must be non-empty strings"));
    }
    let reason = s("reason");
    if py_strip(&reason).is_empty() {
        return Err(invalid("reason must be non-empty"));
    }
    if py_strip(decided_by).is_empty() {
        return Err(invalid("decided_by must be non-empty"));
    }

    // json.dumps(payload, sort_keys=True, separators=(",", ":"),
    // ensure_ascii=False): the keys below are in sorted order.
    let mut out = String::from("{");
    let field = |out: &mut String, key: &str, first: bool| {
        if !first {
            out.push(',');
        }
        json_str(out, key);
        out.push(':');
    };
    field(&mut out, "decided_by", true);
    json_str(&mut out, decided_by);
    field(&mut out, "entry_id", false);
    out.push_str(&raw_id.to_string());
    field(&mut out, "evidence_packet_sha256", false);
    json_str(&mut out, &hashes[3].1);
    field(&mut out, "expected_source_sha256", false);
    json_str(&mut out, &hashes[1].1);
    field(&mut out, "expected_superseded_at", false);
    out.push_str(&crate::pyjson::float_repr(superseded_at));
    field(&mut out, "expected_superseded_by_text_sha256", false);
    json_str(&mut out, &hashes[2].1);
    field(&mut out, "expected_text_sha256", false);
    json_str(&mut out, &hashes[0].1);
    field(&mut out, "operation_id", false);
    json_str(&mut out, &operation_id);
    field(&mut out, "reason", false);
    json_str(&mut out, &reason);
    field(&mut out, "reviewer_ids", false);
    out.push('[');
    for (i, v) in reviewers.as_array().into_iter().flatten().enumerate() {
        if i > 0 {
            out.push(',');
        }
        json_str(&mut out, v.as_str().unwrap_or(""));
    }
    out.push(']');
    field(&mut out, "version", false);
    out.push('1');
    out.push('}');
    Ok(Request {
        entry_id,
        raw_id: raw_id.clone(),
        operation_id,
        sha256: hex::encode(Sha256::digest(out.as_bytes())),
        text_sha256: hashes[0].1.clone(),
        source_sha256: hashes[1].1.clone(),
        superseded_at,
        superseded_by_text_sha256: hashes[2].1.clone(),
        evidence_sha256: hashes[3].1.clone(),
        reviewer_ids: reviewers,
        reason,
        decided_by: decided_by.to_string(),
    })
}

fn digest(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}

/// `memory_reinstate` on the MCP surface (`mcp_server.py:1379-1418`): the
/// default principal is refused before the service runs.
fn mcp_memory_reinstate<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let principal = call.ident.principal.clone();
        if principal == crate::mcp::identity::DEFAULT_PRINCIPAL
            && !crate::mutants::active("mcp-reinstate-default-principal")
        {
            return Err(ToolError::Invalid("named_principal_required".into()));
        }
        let ready = app.service.ensure_init().await.map_err(internal)?;
        let args = call.args.clone();
        let cfg = app.service.config.memory.clone();
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { reinstate(&r2, &cfg, &args, &principal).await })
            .await
            .map_err(internal)?
    })
}

/// `service.reinstate` (`service.py:2681-2807`).
async fn reinstate(
    ready: &Arc<Ready>,
    cfg: &MemoryConfig,
    args: &Map<String, Value>,
    decided_by: &str,
) -> Result<Value, ToolError> {
    let _ws = ready.write.lock().await;
    let req = reinstatement_request(args, decided_by)?;
    let client = ready.storage.client();
    // PostgreSQL is authoritative for admission and replay: the target's row
    // is mirrored into the resident bank first (`service.py:2728-2734`).
    if !crate::mutants::active("mcp-reinstate-no-mirror") {
        mirror_target(ready, cfg, req.entry_id).await?;
    }
    let durable: Option<String> = client
        .query_opt(
            "SELECT request_sha256 FROM entry_reinstatement_decisions \
             WHERE operation_id = $1::text::uuid",
            &[&req.operation_id],
        )
        .await
        .map_err(internal)?
        .map(|r| r.get(0));
    let extras = |mut v: Value| {
        if let Some(m) = v.as_object_mut() {
            m.insert("cortex_changed".into(), json!(false));
            m.insert("trace_invalidations_changed".into(), json!(0));
        }
        v
    };
    if let Some(sha) = durable {
        if sha != req.sha256 {
            return Err(ToolError::Invalid("operation_conflict".into()));
        }
        return reinstate_entry(client, &req).await.map(extras);
    }

    {
        let bank = ready.bank.read().expect("bank lock");
        let residents: Vec<&crate::bank::Entry> = bank
            .entries
            .iter()
            .filter(|e| req.entry_id == Some(e.id))
            .collect();
        let Some(target) = residents.first() else {
            return Err(ToolError::Invalid("target_not_found".into()));
        };
        if residents.len() != 1 {
            return Err(ToolError::Invalid("resident_durable_mismatch".into()));
        }
        let Some(superseded_at) = target.superseded_at else {
            return Err(ToolError::Invalid("target_not_retired".into()));
        };
        let Some(by_text) = &target.superseded_by_text else {
            return Err(ToolError::Invalid("retirement_text_missing".into()));
        };
        if digest(&target.text) != req.text_sha256
            || digest(&target.source) != req.source_sha256
            || superseded_at != req.superseded_at
            || digest(by_text) != req.superseded_by_text_sha256
        {
            return Err(ToolError::Invalid("preimage_mismatch".into()));
        }
    }

    let result = reinstate_entry(client, &req).await?;
    // The post-commit read Python publishes: the row is live again.
    if !crate::mutants::active("mcp-reinstate-resident-stale") {
        let mut bank = ready.bank.write().expect("bank lock");
        for e in bank
            .entries
            .iter_mut()
            .filter(|e| req.entry_id == Some(e.id))
        {
            e.superseded_at = None;
            e.superseded_by_text = None;
        }
    }
    Ok(extras(result))
}

/// `_reinstatement_target_mirrored` (`service.py:2604-2640`), published: the
/// one target row's durable state in the resident bank. No row drops every
/// resident holder; a row without a holder is loaded (`row_to_entry`, seated
/// in its band or the first); otherwise the holders take the row's
/// retirement. An id past BIGINT has no row and no holder.
async fn mirror_target(
    ready: &Ready,
    cfg: &MemoryConfig,
    id: Option<i64>,
) -> Result<(), ToolError> {
    let Some(id) = id else { return Ok(()) };
    let row = ready
        .storage
        .client()
        .query_opt(
            "SELECT id, band, text, embedding::real[], surprise::text, ts, access_count,              source, superseded_at, superseded_by_text, episode_id, episode_title, tags,              slots, authority, distortion_tolerance, reinforcements, last_logical_turn,              dream_state FROM entries WHERE id = $1",
            &[&id],
        )
        .await
        .map_err(|e| internal(format!("reinstatement commit reconciliation required: {e}")))?;
    let mut bank = ready.bank.write().expect("bank lock");
    let holders: Vec<usize> = (0..bank.entries.len())
        .filter(|&i| bank.entries[i].id == id)
        .collect();
    match row {
        None => {
            for &i in holders.iter().rev() {
                bank.remove(i);
            }
        }
        Some(r) if holders.is_empty() => {
            let embedding: Vec<f32> = r.get::<_, Option<Vec<f32>>>(3).unwrap_or_default();
            let mut unit = embedding.clone();
            if unit.len() == bank.dim {
                crate::bank::normalize_query(&mut unit);
            } else {
                unit = vec![0.0; bank.dim];
            }
            let stored_band: String = r.get(1);
            let band = if cfg.bands.contains(&stored_band) {
                stored_band.clone()
            } else {
                cfg.bands[0].clone()
            };
            let surprise_value = r
                .get::<_, Option<String>>(4)
                .unwrap_or_else(|| "0".into())
                .parse::<f64>()
                .unwrap_or(0.0);
            let ts = r.get::<_, Option<f64>>(5).unwrap_or(0.0);
            let array = |v: Option<Value>| match v {
                Some(Value::Array(a)) => a,
                _ => Vec::new(),
            };
            let entry = crate::bank::Entry {
                id,
                seq: episodes::next_seq(),
                embedding,
                band,
                stored_band,
                text: r.get(2),
                surprise: surprise_value as f32,
                surprise_value,
                // `MemoryEntry.__post_init__` replaces a zero timestamp.
                ts: if ts == 0.0 { episodes::now() } else { ts },
                access_count: std::sync::atomic::AtomicI64::new(
                    r.get::<_, Option<i32>>(6).unwrap_or(0) as i64,
                ),
                reinforcements: r.get::<_, Option<i32>>(16).unwrap_or(0),
                last_logical_turn: r.get(17),
                dream_state: r.get(18),
                source: r.get::<_, Option<String>>(7).unwrap_or_default(),
                superseded_at: r.get(8),
                superseded_by_text: r.get(9),
                episode_id: r.get(10),
                episode_title: r.get(11),
                tags: array(r.get(12)),
                slots: array(r.get(13)),
                authority: r.get(14),
                distortion_tolerance: r.get(15),
                ..Default::default()
            };
            bank.push(entry, &unit);
        }
        Some(r) => {
            let (at, by): (Option<f64>, Option<String>) = (r.get(8), r.get(9));
            for i in holders {
                bank.entries[i].superseded_at = at;
                bank.entries[i].superseded_by_text = by.clone();
            }
        }
    }
    Ok(())
}

/// `PostgresStorage.reinstate_entry` (`storage/postgres.py:1107-1209`):
/// one transaction, serialized per operation id, append-only audit.
async fn reinstate_entry(
    client: &tokio_postgres::Client,
    req: &Request,
) -> Result<Value, ToolError> {
    enum Stop {
        Db(tokio_postgres::Error),
        Value(&'static str),
        ShortWrite,
    }
    impl From<tokio_postgres::Error> for Stop {
        fn from(e: tokio_postgres::Error) -> Self {
            Stop::Db(e)
        }
    }
    let out = crate::txn::run(client, |c| async move {
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 41))",
            &[&req.operation_id],
        )
        .await?;
        let prior: Option<String> = c
            .query_opt(
                "SELECT request_sha256 FROM entry_reinstatement_decisions \
                 WHERE operation_id = $1::text::uuid",
                &[&req.operation_id],
            )
            .await?
            .map(|r| r.get(0));
        if let Some(sha) = prior {
            if sha != req.sha256 {
                return Err(Stop::Value("operation_conflict"));
            }
            let current = match req.entry_id {
                Some(id) => c
                    .query_opt("SELECT superseded_at FROM entries WHERE id = $1", &[&id])
                    .await?
                    .map(|r| r.get::<_, Option<f64>>(0)),
                None => None,
            };
            let state = match current {
                None => "missing",
                Some(None) => "live",
                Some(Some(_)) => "retired_again",
            };
            return Ok(json!({
                "action": "already_reinstated", "decision": "committed",
                "operation_id": req.operation_id, "entry_id": req.raw_id,
                "current_state": state, "changed_by_this_call": false,
                "idempotent_replay": true,
            }));
        }
        let Some(id) = req.entry_id else {
            return Err(Stop::Value("target_not_found"));
        };
        let Some(row) = c
            .query_opt(
                "SELECT text, source, superseded_at, superseded_by_text \
                 FROM entries WHERE id = $1 FOR UPDATE",
                &[&id],
            )
            .await?
        else {
            return Err(Stop::Value("target_not_found"));
        };
        let (text, source): (String, String) = (row.get(0), row.get(1));
        let Some(superseded_at) = row.get::<_, Option<f64>>(2) else {
            return Err(Stop::Value("target_not_retired"));
        };
        let Some(by_text) = row.get::<_, Option<String>>(3) else {
            return Err(Stop::Value("retirement_text_missing"));
        };
        if digest(&text) != req.text_sha256
            || digest(&source) != req.source_sha256
            || superseded_at != req.superseded_at
            || digest(&by_text) != req.superseded_by_text_sha256
        {
            return Err(Stop::Value("preimage_mismatch"));
        }
        let invalidations: i64 = c
            .query_one(
                "SELECT count(*) FROM memory_trace_invalidations WHERE source_entry_id = $1",
                &[&id],
            )
            .await?
            .get(0);
        if invalidations > 0 && !crate::mutants::active("mcp-reinstate-ignore-invalidations") {
            return Err(Stop::Value("trace_invalidations_present"));
        }
        c.execute(
            "INSERT INTO entry_reinstatement_decisions \
             (operation_id, entry_id, request_sha256, entry_text_sha256, \
             entry_source_sha256, prior_superseded_at, prior_superseded_by_text, \
             prior_superseded_by_text_sha256, evidence_packet_sha256, reviewer_ids, \
             reason, decided_by, decided_at) VALUES ($1::text::uuid, $2, $3, $4, $5, \
             $6, $7, $8, $9, $10, $11, $12, $13)",
            &[
                &req.operation_id,
                &id,
                &req.sha256,
                &req.text_sha256,
                &req.source_sha256,
                &superseded_at,
                &by_text,
                &req.superseded_by_text_sha256,
                &req.evidence_sha256,
                &req.reviewer_ids,
                &req.reason,
                &req.decided_by,
                &episodes::now(),
            ],
        )
        .await?;
        let updated = c
            .execute(
                "UPDATE entries SET superseded_at = NULL, superseded_by_text = NULL \
                 WHERE id = $1 AND superseded_at = $2 AND superseded_by_text = $3",
                &[&id, &superseded_at, &by_text],
            )
            .await?;
        if updated != 1 {
            return Err(Stop::ShortWrite);
        }
        Ok(json!({
            "action": "reinstated", "decision": "committed",
            "operation_id": req.operation_id, "entry_id": id,
            "current_state": "live", "changed_by_this_call": true,
            "idempotent_replay": false,
        }))
    })
    .await;
    match out {
        Ok(v) => Ok(v),
        Err(Stop::Value(code)) => Err(ToolError::Invalid(code.into())),
        Err(Stop::ShortWrite) => Err(internal("reinstatement short write")),
        Err(Stop::Db(e)) => Err(internal(e)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strictest_matches_python() {
        let v = [Some("observation"), None, Some("quoted"), Some("directive")];
        assert_eq!(
            strictest(v.into_iter(), &AUTHORITY_RANK).as_deref(),
            Some("quoted")
        );
        assert_eq!(
            strictest([None, Some("bogus")].into_iter(), &AUTHORITY_RANK),
            None
        );
        let d = [Some("episodic"), Some("constraint"), Some("belief")];
        assert_eq!(
            strictest(d.into_iter(), &DISTORTION_RANK).as_deref(),
            Some("constraint")
        );
    }

    #[test]
    fn uuid_forms_classify_like_python() {
        let canon = "0f1e2d3c-4b5a-4968-8776-655443322110";
        assert!(is_canonical_uuid(canon));
        for parses in [
            "0F1E2D3C-4B5A-4968-8776-655443322110",
            "{0f1e2d3c-4b5a-4968-8776-655443322110}",
            "urn:uuid:0f1e2d3c-4b5a-4968-8776-655443322110",
            "0f1e2d3c4b5a49688776655443322110",
            "0x1e2d3c4b5a49688776655443322110",
            "+f1e2d3c4b5a49688776655443322110",
            "0f1e_2d3c4b5a4968877665544332211",
        ] {
            assert!(!is_canonical_uuid(parses), "{parses}");
            assert!(py_uuid_parses(parses), "{parses}");
        }
        for bad in [
            "",
            "not-a-uuid",
            "0f1e2d3c-4b5a-4968-8776-65544332211g",
            "0f1e__2d3c4b5a496887766554433221",
            "_0f1e2d3c4b5a4968877665544332211",
            "0f1e2d3c4b5a4968877665544332211_",
        ] {
            assert!(!py_uuid_parses(bad), "{bad}");
        }
    }

    #[test]
    fn request_json_escapes_like_python() {
        let mut out = String::new();
        json_str(&mut out, "a\"b\\c\nd\u{1}\u{7f}é");
        assert_eq!(out, "\"a\\\"b\\\\c\\nd\\u0001\u{7f}é\"");
    }
}
