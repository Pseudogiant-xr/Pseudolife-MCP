//! Two of the dream's post-consolidation stages (`service_dream.py`):
//!
//! * `infer_outcomes_stage` (497-568): outcome signals inferred for closed
//!   session roots that hold entries and no signal, under the meta
//!   `outcome_inference_cursor` `{ts, retry}`;
//! * `generate_digests_stage` (570-708): one narrative digest per closed
//!   session root, a `source="digest"` entry stamped to the summarized
//!   episode, under the meta `session_digest_cursor`.
//!
//! Both have the same shape: the candidates under the writer lock (Python's
//! service lock), the model call unlocked, then a locked re-check and
//! write. A transport failure halts the stage with the cursor held; a
//! malformed reply gets two attempts, then the cursor advances past the
//! root. The helpers are `_load/_save_infer_cursor`,
//! `_episode_inference_context`, `_pending_inference_candidates`,
//! `_load/_save_digest_cursor`, `_pending_digest_candidates` and
//! `_store_digest` (3134-3283).

use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::AtomicI64;

use serde_json::{Map, Value, json};

use super::extractor::Extractor;
use super::service::{by_float, closed_roots, episode_entry_counts, get_meta, meta_float, subtree};
use super::stage_calls::split_session_context;
use crate::bank::Entry;
use crate::episodes::EpisodeLog;
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};

/// `_INFER_CURSOR_KEY` / `_DIGEST_CURSOR_KEY`.
const INFER_CURSOR_KEY: &str = "outcome_inference_cursor";
const DIGEST_CURSOR_KEY: &str = "session_digest_cursor";
/// `_pending_inference_candidates`' default `limit`.
const INFER_CANDIDATE_LIMIT: usize = 8;

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

fn stage_result(scanned: usize, written: usize) -> Value {
    json!({"scanned": scanned, "written": written})
}

fn skipped(why: &str) -> Value {
    json!({"scanned": 0, "written": 0, "skipped": why})
}

// ---------------------------------------------------------------------------
// Cursors
// ---------------------------------------------------------------------------

/// A stage cursor as `_load_infer_cursor` / `_load_digest_cursor` build it:
/// `ts` as a float, `retry` as a dict copy and, for digests, the
/// `regenerate` and `context_deferred` lists when the stored value has them.
#[derive(Debug, Clone, PartialEq)]
struct Cursor {
    ts: f64,
    retry: Map<String, Value>,
    regenerate: Option<Vec<Value>>,
    context_deferred: Option<Vec<Value>>,
}

/// `dict(x)` for a decoded JSON value.
fn py_dict(v: &Value) -> Result<Map<String, Value>, String> {
    match v {
        Value::Object(m) => Ok(m.clone()),
        Value::Array(pairs) => {
            let mut out = Map::new();
            for p in pairs {
                match p.as_array().map(Vec::as_slice) {
                    Some([Value::String(k), v]) => {
                        out.insert(k.clone(), v.clone());
                    }
                    _ => return Err(format!("cannot convert {p} to a dict entry")),
                }
            }
            Ok(out)
        }
        other => Err(format!("cannot convert {other} to dict")),
    }
}

/// `list(x)` for a decoded JSON value.
fn py_list(v: &Value) -> Result<Vec<Value>, String> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::Object(m) => Ok(m.keys().map(|k| json!(k)).collect()),
        Value::String(s) => Ok(s.chars().map(|c| json!(c.to_string())).collect()),
        other => Err(format!("'{other}' object is not iterable")),
    }
}

/// `int(x)` for a stored retry count.
fn py_int(v: &Value) -> Result<i64, String> {
    match v {
        Value::Bool(b) => Ok(i64::from(*b)),
        Value::Number(n) => match (n.as_i64(), n.as_f64()) {
            (Some(i), _) => Ok(i),
            (None, Some(x)) if x.is_finite() => Ok(x.trunc() as i64),
            _ => Err(format!("cannot convert {n} to integer")),
        },
        Value::String(s) => super::extractor::py_int_str(s)
            .and_then(|i| i64::try_from(i).ok())
            .ok_or_else(|| format!("invalid literal for int() with base 10: {s:?}")),
        other => Err(format!(
            "int() argument must be a string or a number, not {other}"
        )),
    }
}

impl Cursor {
    fn fresh() -> Self {
        Cursor {
            ts: 0.0,
            retry: Map::new(),
            regenerate: None,
            context_deferred: None,
        }
    }

    async fn load(ready: &Ready, key: &str, digest: bool) -> Result<Cursor, ToolError> {
        let Some(Value::Object(raw)) = get_meta(ready.storage.client(), key).await? else {
            return Ok(Cursor::fresh());
        };
        let ts = meta_float(raw.get("ts"), 0.0).map_err(internal)?;
        let retry = match raw.get("retry") {
            None => Map::new(),
            Some(v) => py_dict(v).map_err(internal)?,
        };
        let mut cur = Cursor {
            ts,
            retry,
            regenerate: None,
            context_deferred: None,
        };
        if digest {
            if let Some(v) = raw.get("regenerate") {
                cur.regenerate = Some(py_list(v).map_err(internal)?);
            }
            if let Some(v) = raw.get("context_deferred") {
                cur.context_deferred = Some(py_list(v).map_err(internal)?);
            }
        }
        Ok(cur)
    }

    fn to_json(&self) -> Value {
        let mut m = Map::new();
        m.insert("ts".into(), json!(self.ts));
        m.insert("retry".into(), Value::Object(self.retry.clone()));
        if let Some(r) = &self.regenerate {
            m.insert("regenerate".into(), Value::Array(r.clone()));
        }
        if let Some(c) = &self.context_deferred {
            m.insert("context_deferred".into(), Value::Array(c.clone()));
        }
        Value::Object(m)
    }

    /// `set_meta(key, cur)`: one upsert, its own transaction.
    async fn save(&self, ready: &Ready, key: &str) -> Result<(), ToolError> {
        crate::txn::set_meta(ready.storage.client(), key, &self.to_json())
            .await
            .map_err(internal)
    }

    /// `int(cur["retry"].get(rid, 0)) + 1`.
    fn attempts(&self, rid: &str) -> Result<i64, ToolError> {
        match self.retry.get(rid) {
            None => Ok(1),
            Some(v) => py_int(v).map(|n| n + 1).map_err(internal),
        }
    }

    fn queued(&self, rid: &str) -> bool {
        self.regenerate
            .as_deref()
            .unwrap_or(&[])
            .iter()
            .any(|v| v.as_str() == Some(rid))
    }

    /// `cur[key] = [e for e in cur.get(key, []) if e != rid]` for both lists.
    fn drop_root(&mut self, rid: &str) {
        for list in [&mut self.regenerate, &mut self.context_deferred] {
            let kept: Vec<Value> = list
                .take()
                .unwrap_or_default()
                .into_iter()
                .filter(|v| v.as_str() != Some(rid))
                .collect();
            *list = Some(kept);
        }
    }
}

// ---------------------------------------------------------------------------
// Candidates
// ---------------------------------------------------------------------------

/// `_episode_inference_context(root, subtree)`: every daemon-visible line of
/// a session (status and log entries included, digests not), entries by
/// timestamp, band by band before the stable sort.
fn inference_context(
    ready: &Ready,
    bands: &[String],
    log: &EpisodeLog,
    root: &str,
    ids: &[String],
) -> String {
    let title = log
        .get(root)
        .map(|e| e.title.clone())
        .filter(|t| !t.is_empty())
        .unwrap_or_else(|| "(untitled)".into());
    let mut lines = vec![format!("Session: {title}")];
    let set: HashSet<&str> = ids.iter().map(String::as_str).collect();
    for e in &log.episodes {
        if set.contains(e.id.as_str()) && e.id != root && !e.title.is_empty() {
            lines.push(format!("Sub-task: {}", e.title));
        }
    }
    let bank = ready.bank.read().expect("bank lock");
    let mut entries: Vec<&Entry> = Vec::new();
    for band in bands {
        entries.extend(bank.entries.iter().filter(|e| {
            e.band == *band
                && e.episode_id.as_deref().is_some_and(|ep| set.contains(ep))
                && e.source != "digest"
        }));
    }
    entries.sort_by(|a, b| by_float(a.ts, b.ts));
    for en in entries {
        let mark = if en.superseded_at.is_some_and(|t| t != 0.0) {
            " [superseded]"
        } else {
            ""
        };
        lines.push(format!("- ({}){mark} {}", en.source, en.text));
    }
    lines.join("\n")
}

/// `count_signals_for_episodes(ids)`.
async fn count_signals(ready: &Ready, ids: &[String]) -> Result<i64, ToolError> {
    if ids.is_empty() {
        return Ok(0);
    }
    let ids = ids.to_vec();
    crate::txn::with_client(ready.storage.client(), |c| async move {
        c.query_one(
            "SELECT COUNT(*) FROM outcome_signals WHERE episode_id = ANY($1)",
            &[&ids],
        )
        .await
        .map(|r| r.get::<_, i64>(0))
    })
    .await
    .map_err(internal)
}

struct InferCandidate {
    root_id: String,
    ended_at: f64,
    context: String,
}

/// `_pending_inference_candidates(limit=8)`: closed session roots past the
/// cursor, oldest first, with at least one subtree entry and no subtree
/// signal. Caller holds the writer lock.
async fn inference_candidates(
    svc: &Service,
    ready: &Ready,
    log: &EpisodeLog,
) -> Result<Vec<InferCandidate>, ToolError> {
    let cur = Cursor::load(ready, INFER_CURSOR_KEY, false).await?;
    let counts = episode_entry_counts(ready);
    let mut roots = closed_roots(log, |e| e.ended_at.is_some_and(|t| t > cur.ts));
    roots.sort_by(|a, b| by_float(a.ended_at.unwrap_or(0.0), b.ended_at.unwrap_or(0.0)));
    let mut out = Vec::new();
    for root in roots {
        let ids = subtree(log, &root.id);
        if ids
            .iter()
            .map(|i| counts.get(i).copied().unwrap_or(0))
            .sum::<usize>()
            == 0
        {
            continue;
        }
        if count_signals(ready, &ids).await? > 0 {
            continue;
        }
        out.push(InferCandidate {
            root_id: root.id.clone(),
            ended_at: root.ended_at.unwrap_or(0.0),
            context: inference_context(ready, &svc.config.memory.bands, log, &root.id, &ids),
        });
        if out.len() >= INFER_CANDIDATE_LIMIT {
            break;
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// infer_outcomes_stage
// ---------------------------------------------------------------------------

/// `add_signal(task, outcome, about, detail, origin="inferred",
/// episode_id=...)`: one insert, its own transaction.
async fn add_inferred_signal(
    ready: &Ready,
    c: &super::stage_calls::OutcomeClaim,
    episode_id: &str,
) -> Result<(), ToolError> {
    let fields = [
        c.task.as_str(),
        c.outcome.as_str(),
        c.about.as_deref().unwrap_or(""),
        c.detail.as_deref().unwrap_or(""),
    ];
    // psycopg refuses a parameter holding a reply's lone surrogate.
    if let Some(e) = super::extractor::unencodable(&fields) {
        return Err(internal(e));
    }
    let now = crate::episodes::now();
    crate::txn::run(ready.storage.client(), |db| async move {
        db.execute(
            "INSERT INTO outcome_signals (task, outcome, about, detail, polarity, origin, \
             episode_id, created_at) VALUES ($1, $2, $3, $4, NULL, 'inferred', $5, $6)",
            &[&c.task, &c.outcome, &c.about, &c.detail, &episode_id, &now],
        )
        .await
        .map(|_| ())
    })
    .await
    .map_err(internal)
}

/// `infer_outcomes_stage(extractor)`.
pub async fn infer_outcomes_stage(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    extractor: &Extractor,
) -> Result<Value, ToolError> {
    let m = &svc.config.memory;
    if !(m.lessons_enabled && m.lessons_infer_outcomes && m.lessons_infer_outcomes_max_signals > 0)
    {
        return Ok(skipped("disabled"));
    }
    let Some(compat) = extractor.as_compat() else {
        return Ok(skipped("no-extractor"));
    };
    let cap = m.lessons_infer_outcomes_max_signals;
    let candidates = {
        let ws = ready.write.lock().await;
        inference_candidates(svc, ready, &ws.log).await?
    };
    let (mut scanned, mut written) = (0, 0);
    for cand in candidates {
        let claims = match compat.infer_outcomes(&cand.context, cap).await {
            Ok(claims) => claims,
            Err(e) => {
                eprintln!("outcome inference halted ({e:?}); cursor held");
                break;
            }
        };
        scanned += 1;
        let _ws = ready.write.lock().await;
        let mut cur = Cursor::load(ready, INFER_CURSOR_KEY, false).await?;
        let rid = cand.root_id.as_str();
        // STRICTLY greater: same-tick siblings share ended_at.
        if cur.ts > cand.ended_at || count_signals(ready, &[cand.root_id.clone()]).await? > 0 {
            continue;
        }
        let Some(claims) = claims else {
            let attempts = cur.attempts(rid)?;
            if attempts >= 2 && !crate::mutants::active("mcp-dream-stage-infer-retry-forever") {
                cur.retry.remove(rid);
                cur.ts = cand.ended_at;
                cur.save(ready, INFER_CURSOR_KEY).await?;
                eprintln!(
                    "outcome inference: advancing past episode {rid} after {attempts} malformed attempts"
                );
                continue;
            }
            cur.retry.insert(rid.to_string(), json!(attempts));
            cur.save(ready, INFER_CURSOR_KEY).await?;
            eprintln!(
                "outcome inference: malformed output for episode {rid} (attempt {attempts}); \
                 will retry next dream"
            );
            break;
        };
        for c in &claims {
            match add_inferred_signal(ready, c, rid).await {
                Ok(()) => written += 1,
                Err(e) => eprintln!("inferred signal skipped ({e:?})"),
            }
        }
        cur.retry.remove(rid);
        cur.ts = cand.ended_at;
        cur.save(ready, INFER_CURSOR_KEY).await?;
    }
    Ok(stage_result(scanned, written))
}

// ---------------------------------------------------------------------------
// generate_digests_stage
// ---------------------------------------------------------------------------

struct DigestCandidate {
    root_id: String,
    ended_at: f64,
    started_at: f64,
    title: String,
    n_entries: usize,
    context: String,
}

/// The episode ids that already hold a live digest entry.
fn digested(ready: &Ready) -> HashSet<String> {
    let bank = ready.bank.read().expect("bank lock");
    bank.entries
        .iter()
        .filter(|e| e.source == "digest" && e.superseded_at.is_none())
        .filter_map(|e| e.episode_id.clone().filter(|ep| !ep.is_empty()))
        .collect()
}

/// `_pending_digest_candidates()`: closed session roots past the cursor (or
/// queued for regeneration) with subtree entries and no live digest,
/// invalidated contexts rotated behind the rest, capped at
/// `digest_max_per_cycle`. Caller holds the writer lock.
async fn digest_candidates(
    svc: &Service,
    ready: &Ready,
    log: &EpisodeLog,
) -> Result<Vec<DigestCandidate>, ToolError> {
    let cfg = &svc.config.dream;
    let cap = usize::try_from(cfg.digest_max_per_cycle).unwrap_or(0);
    let cur = Cursor::load(ready, DIGEST_CURSOR_KEY, true).await?;
    let counts = episode_entry_counts(ready);
    let done = digested(ready);
    let regenerate: HashSet<&str> = cur
        .regenerate
        .as_deref()
        .unwrap_or(&[])
        .iter()
        .filter_map(Value::as_str)
        .collect();
    let mut deferred = std::collections::HashMap::new();
    for (i, v) in cur
        .context_deferred
        .as_deref()
        .unwrap_or(&[])
        .iter()
        .enumerate()
    {
        if let Some(rid) = v.as_str() {
            deferred.insert(rid.to_string(), i as i64);
        }
    }
    let mut roots = closed_roots(log, |e| {
        e.ended_at.is_some_and(|t| t > cur.ts) || regenerate.contains(e.id.as_str())
    });
    roots.sort_by(|a, b| {
        let da = deferred.get(&a.id).copied().unwrap_or(-1);
        let db = deferred.get(&b.id).copied().unwrap_or(-1);
        da.cmp(&db)
            .then_with(|| by_float(a.ended_at.unwrap_or(0.0), b.ended_at.unwrap_or(0.0)))
    });
    let mut out = Vec::new();
    for root in roots {
        if done.contains(&root.id) {
            continue;
        }
        let ids = subtree(log, &root.id);
        let n: usize = ids
            .iter()
            .map(|i| counts.get(i).copied().unwrap_or(0))
            .sum();
        if n == 0 {
            continue;
        }
        out.push(DigestCandidate {
            root_id: root.id.clone(),
            ended_at: root.ended_at.unwrap_or(0.0),
            started_at: root.started_at,
            title: if root.title.is_empty() {
                "(untitled)".into()
            } else {
                root.title.clone()
            },
            n_entries: n,
            context: inference_context(ready, &svc.config.memory.bands, log, &root.id, &ids),
        });
        // `len(out) >= cap` (a cap of zero still yields the first root).
        if out.len() >= cap {
            break;
        }
    }
    Ok(out)
}

/// `time.strftime("%Y-%m-%d", time.gmtime(ts))`.
fn utc_day(ts: f64) -> String {
    chrono::DateTime::from_timestamp(ts.floor() as i64, 0)
        .map(|d| d.format("%Y-%m-%d").to_string())
        .unwrap_or_default()
}

/// `_store_digest(text, episode_id, title)`: one entry outside the CMS gate
/// pipeline (no surprise gate, contradiction decay or slot extraction) in
/// the first band at surprise 1.0, the capacity victim dropped first,
/// stamped to the summarized episode, tagged `digest`. Caller holds the
/// writer lock.
async fn store_digest(
    svc: &Service,
    ready: &Ready,
    text: &str,
    episode_id: &str,
    title: &str,
) -> Result<(), ToolError> {
    // The embedder (or psycopg) refuses a reply's lone surrogate.
    if let Some(e) = super::extractor::unencodable(&[text]) {
        return Err(internal(e));
    }
    let row = crate::fact_set::embed(ready, text.to_string())
        .await?
        .to_vec();
    let cfg = &svc.config.memory;
    if let Some(idx) = crate::store::first_band_victim(ready, cfg, &[]) {
        crate::store::evict_resident(ready, idx).await;
    }
    let band0 = cfg.bands[0].clone();
    let mut entry = Entry {
        band: band0.clone(),
        stored_band: band0,
        text: text.to_string(),
        surprise: 1.0,
        surprise_value: 1.0,
        embedding: row.clone(),
        ts: crate::episodes::now(),
        access_count: AtomicI64::new(0),
        source: "digest".into(),
        seq: crate::episodes::next_seq(),
        tags: vec![json!("digest")],
        dream_state: Some("pending".into()),
        episode_id: Some(episode_id.to_string()),
        episode_title: Some(title.to_string()),
        ..Default::default()
    };
    let id = {
        let (e, row) = (&entry, &row);
        crate::txn::run(ready.storage.client(), |c| async move {
            crate::store::insert_entry_row(c, e, row).await
        })
        .await
        .map_err(|e| internal(format!("entry insert failed: {e}")))?
    };
    entry.id = id;
    let mut unit = row;
    crate::bank::normalize_query(&mut unit);
    ready.bank.write().expect("bank lock").push(entry, &unit);
    Ok(())
}

/// The digest of one candidate: one call, or map-reduce over the context
/// split (`None` as soon as a part is malformed).
async fn summarize(
    compat: &super::extractor::Compat,
    context: &str,
    context_chars: i64,
    target: i64,
) -> Result<Option<String>, super::extractor::ExtractFailure> {
    let parts = split_session_context(context, context_chars);
    if parts.len() == 1 {
        return compat.summarize_session(&parts[0], target).await;
    }
    let mut segs: Vec<String> = Vec::new();
    for part in &parts {
        match compat.summarize_session(part, target).await? {
            Some(seg) => segs.push(seg),
            None => return Ok(None),
        }
    }
    if crate::mutants::active("mcp-dream-stage-digest-no-reduce") {
        return Ok(Some(segs.join("\n\n")));
    }
    compat.summarize_session(&segs.join("\n\n"), target).await
}

/// `generate_digests_stage(extractor)`.
pub async fn generate_digests_stage(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    extractor: &Extractor,
) -> Result<Value, ToolError> {
    let cfg = &svc.config.dream;
    if !cfg.digest_enabled {
        return Ok(skipped("disabled"));
    }
    let Some(compat) = extractor.as_compat() else {
        return Ok(skipped("no-extractor"));
    };
    let target = cfg.digest_target_chars;
    let candidates = {
        let ws = ready.write.lock().await;
        digest_candidates(svc, ready, &ws.log).await?
    };
    let (mut scanned, mut written) = (0, 0);
    for cand in candidates {
        let digest = match summarize(compat, &cand.context, cfg.digest_context_chars, target).await
        {
            Ok(d) => d,
            Err(e) => {
                eprintln!("session digest halted ({e:?}); cursor held");
                break;
            }
        };
        scanned += 1;
        let ws = ready.write.lock().await;
        let mut cur = Cursor::load(ready, DIGEST_CURSOR_KEY, true).await?;
        let rid = cand.root_id.as_str();
        let existing = digested(ready).contains(rid);
        if (cur.ts > cand.ended_at && !cur.queued(rid)) || existing {
            continue;
        }
        let now_context = ws.log.get(rid).map(|_| {
            let ids = subtree(&ws.log, rid);
            inference_context(ready, &svc.config.memory.bands, &ws.log, rid, &ids)
        });
        if now_context.as_deref() != Some(cand.context.as_str()) {
            // A forget changed the input while the call ran unlocked: keep
            // the root queued and rotate it behind the rest.
            let mut regen: Vec<String> = Vec::new();
            for v in cur.regenerate.as_deref().unwrap_or(&[]) {
                match v.as_str() {
                    Some(s) => regen.push(s.to_string()),
                    None => return Err(internal(format!("'<' not supported for {v}"))),
                }
            }
            regen.push(rid.to_string());
            regen.sort();
            regen.dedup();
            cur.regenerate = Some(regen.into_iter().map(Value::String).collect());
            let mut deferred: Vec<Value> = cur
                .context_deferred
                .take()
                .unwrap_or_default()
                .into_iter()
                .filter(|v| v.as_str() != Some(rid))
                .collect();
            deferred.push(json!(rid));
            cur.context_deferred = Some(deferred);
            cur.save(ready, DIGEST_CURSOR_KEY).await?;
            continue;
        }
        let fail = |cur: &mut Cursor, what: &str| -> Result<bool, ToolError> {
            let attempts = cur.attempts(rid)?;
            if attempts >= 2 {
                cur.retry.remove(rid);
                cur.drop_root(rid);
                cur.ts = cur.ts.max(cand.ended_at);
                eprintln!("session digest: advancing past episode {rid} after {attempts} {what}");
                return Ok(true);
            }
            cur.retry.insert(rid.to_string(), json!(attempts));
            eprintln!("session digest: {what} for episode {rid} (attempt {attempts}); will retry");
            Ok(false)
        };
        let Some(digest) = digest else {
            let advanced = fail(&mut cur, "malformed attempts")?;
            cur.save(ready, DIGEST_CURSOR_KEY).await?;
            if advanced {
                continue;
            }
            break;
        };
        let d0 = utc_day(if cand.started_at != 0.0 {
            cand.started_at
        } else {
            cand.ended_at
        });
        let d1 = utc_day(cand.ended_at);
        let span = if d0 == d1 {
            d0
        } else {
            format!("{d0}\u{2013}{d1}")
        };
        let header = format!(
            "Session digest: {} ({span}, {} entries)",
            cand.title, cand.n_entries
        );
        if let Err(e) =
            store_digest(svc, ready, &format!("{header}\n{digest}"), rid, &cand.title).await
        {
            eprintln!("session digest: write failed for episode {rid}: {e:?}");
            let advanced = fail(&mut cur, "failed writes")?;
            cur.save(ready, DIGEST_CURSOR_KEY).await?;
            if advanced {
                continue;
            }
            break;
        }
        written += 1;
        cur.retry.remove(rid);
        cur.drop_root(rid);
        cur.ts = cur.ts.max(cand.ended_at);
        cur.save(ready, DIGEST_CURSOR_KEY).await?;
    }
    Ok(stage_result(scanned, written))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cursor_loads_like_python() {
        assert_eq!(py_int(&json!(2.9)), Ok(2));
        assert_eq!(py_int(&json!(" 3 ")), Ok(3));
        assert!(py_int(&json!([1])).is_err());
        assert_eq!(
            py_dict(&json!([["a", 1]])).unwrap(),
            json!({"a": 1}).as_object().unwrap().clone()
        );
        assert_eq!(py_list(&json!("ab")).unwrap(), vec![json!("a"), json!("b")]);
        let mut cur = Cursor::fresh();
        cur.regenerate = Some(vec![json!("r"), json!("x")]);
        cur.drop_root("r");
        assert_eq!(
            cur.to_json(),
            json!({"ts": 0.0, "retry": {}, "regenerate": ["x"], "context_deferred": []})
        );
    }

    #[test]
    fn digest_day_is_utc() {
        assert_eq!(utc_day(0.0), "1970-01-01");
        assert_eq!(utc_day(1_700_000_000.9), "2023-11-14");
    }
}
