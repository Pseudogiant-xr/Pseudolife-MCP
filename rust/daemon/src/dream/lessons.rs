//! Lesson synthesis, the dream stage that drains pending outcome signals
//! into lessons: `service_dream.synthesize_lessons` (746-1019) with
//! `_pending_lesson_signals` (710-744) and `_staged_slot` (315-340), over
//! `storage/postgres.py` `prune_signals`, `pending_signals`,
//! `lesson_synthesis_transaction`, `lesson_batch_status`,
//! `lesson_lineage_sources`, `consume_signals` and `record_store_decision`.
//!
//! The selection runs under the writer lock (Python's service lock), the
//! extraction unlocked (`extract_lessons` for clustering signals,
//! `extract_rules` for signals under the rule contract), then one
//! transaction revalidates the selected rows, writes every claim on a
//! staged copy of the lesson store (each claim in a savepoint, its RAM
//! write undone when it fails), rewrites the dirty slots and acknowledges
//! the handled signals. The staged store is published only after the
//! commit.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use super::extractor::{ExtractFailure, Extractor};
use super::stage_calls::{LessonClaim, Signal, is_rule_signal};
use crate::episodes::Caller;
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};
use crate::stores::lessons::{LessonStore, LessonWrite};

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// `id` + `_SIGNAL_COLS` + `used_ids`.
const SIGNAL_COLS: &str =
    "id, task, outcome, about, detail, polarity, origin, episode_id, created_at, used_ids";

fn signal_row(r: &tokio_postgres::Row) -> Signal {
    Signal {
        id: r.get(0),
        task: r.get(1),
        outcome: r.get(2),
        about: r.get(3),
        detail: r.get(4),
        polarity: r.get(5),
        origin: r.get(6),
        episode_id: r.get(7),
        created_at: r.get(8),
        used_ids: r.get(9),
    }
}

/// `signal_source_ids`: the entries a signal's outcome credited
/// (`used_ids.credited`, positive integers only).
fn signal_source_ids(s: &Signal) -> BTreeSet<i64> {
    let credited = s
        .used_ids
        .as_ref()
        .and_then(Value::as_object)
        .and_then(|m| m.get("credited"))
        .and_then(Value::as_array);
    credited
        .map(|items| {
            items
                .iter()
                .filter(|v| v.is_i64() || v.is_u64())
                .filter_map(Value::as_i64)
                .filter(|&i| i > 0)
                .collect()
        })
        .unwrap_or_default()
}

/// `signal_sources_survive`: it credits nothing, or one credited entry
/// still exists.
fn sources_survive(sources: &BTreeSet<i64>, existing: &HashSet<i64>) -> bool {
    sources.is_empty() || sources.iter().any(|i| existing.contains(i))
}

/// `pending_signals(limit, since_ts, after=...)`: unconsumed signals,
/// oldest first.
async fn pending_signals(
    c: &Client,
    limit: Option<i64>,
    since: Option<f64>,
    after: Option<(f64, i64)>,
) -> Result<Vec<Signal>, tokio_postgres::Error> {
    let mut sql = format!("SELECT {SIGNAL_COLS} FROM outcome_signals WHERE consumed_at IS NULL");
    let mut params: Vec<Box<dyn tokio_postgres::types::ToSql + Sync + Send>> = Vec::new();
    if let Some(t) = since {
        params.push(Box::new(t));
        sql.push_str(&format!(" AND created_at >= ${}", params.len()));
    }
    if let Some((t, id)) = after {
        params.push(Box::new(t));
        params.push(Box::new(id));
        sql.push_str(&format!(
            " AND (created_at, id) > (${}, ${})",
            params.len() - 1,
            params.len()
        ));
    }
    sql.push_str(" ORDER BY created_at, id");
    if let Some(n) = limit {
        sql.push_str(&format!(" LIMIT {n}"));
    }
    let refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> = params
        .iter()
        .map(|p| p.as_ref() as &(dyn tokio_postgres::types::ToSql + Sync))
        .collect();
    Ok(c.query(&sql, &refs).await?.iter().map(signal_row).collect())
}

/// The ids among `ids` that still have an entry row.
async fn existing_entries(
    c: &Client,
    ids: &[i64],
    lock: bool,
) -> Result<HashSet<i64>, tokio_postgres::Error> {
    if ids.is_empty() {
        return Ok(HashSet::new());
    }
    let sql = if lock {
        "SELECT id FROM entries WHERE id = ANY($1) ORDER BY id FOR UPDATE"
    } else {
        "SELECT id FROM entries WHERE id = ANY($1)"
    };
    Ok(c.query(sql, &[&ids])
        .await?
        .iter()
        .map(|r| r.get::<_, i64>(0))
        .collect())
}

/// `_pending_lesson_signals(limit, since_ts)`: a capped batch rotated by
/// the process-local cursor (wrapping to the oldest), signals whose
/// credited entries are all gone held back, sorted oldest first. Sets the
/// selected cursor when anything was selected.
async fn select_signals(
    ready: &Ready,
    limit: Option<i64>,
    since: Option<f64>,
) -> Result<Vec<Signal>, ToolError> {
    let state = &ready.dream_run;
    let after = if limit.is_none() {
        None
    } else {
        *state.lesson_cursor.lock().expect("lesson cursor")
    };
    let client = ready.storage.client();
    let selected = crate::txn::with_client(client, |c| async move {
        let mut selected = pending_signals(c, limit, since, after).await?;
        if let (Some(_), Some(n)) = (after, limit)
            && (selected.len() as i64) < n
        {
            let wrapped = pending_signals(c, Some(n - selected.len() as i64), since, None).await?;
            let seen: HashSet<i64> = selected.iter().map(|s| s.id).collect();
            selected.extend(wrapped.into_iter().filter(|s| !seen.contains(&s.id)));
        }
        Ok::<_, tokio_postgres::Error>(selected)
    })
    .await
    .map_err(internal)?;
    if let Some(last) = selected.last() {
        *state.lesson_selected.lock().expect("lesson cursor") = Some((last.created_at, last.id));
    }
    let deps: Vec<BTreeSet<i64>> = selected.iter().map(signal_source_ids).collect();
    let all: Vec<i64> = deps
        .iter()
        .flatten()
        .copied()
        .collect::<BTreeSet<i64>>()
        .into_iter()
        .collect();
    let existing =
        crate::txn::with_client(
            client,
            |c| async move { existing_entries(c, &all, false).await },
        )
        .await
        .map_err(internal)?;
    let mut eligible: Vec<Signal> = selected
        .into_iter()
        .zip(deps.iter())
        .filter(|(_, d)| sources_survive(d, &existing))
        .map(|(s, _)| s)
        .collect();
    eligible.sort_by(|a, b| {
        a.created_at
            .partial_cmp(&b.created_at)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.id.cmp(&b.id))
    });
    Ok(eligible)
}

/// `lesson_batch_status(signals, handled_ids, lock=...)`: `pending` while
/// every selected row is unchanged and unconsumed, `consumed` when exactly
/// the handled ones are, else `changed`.
async fn batch_status(
    c: &Client,
    signals: &[Signal],
    handled: &[i64],
    lock: bool,
) -> Result<&'static str, tokio_postgres::Error> {
    let ids: Vec<i64> = signals.iter().map(|s| s.id).collect();
    let sql = format!(
        "SELECT {SIGNAL_COLS}, consumed_at FROM outcome_signals WHERE id = ANY($1) ORDER BY id{}",
        if lock { " FOR UPDATE" } else { "" }
    );
    let rows = c.query(&sql, &[&ids]).await?;
    let expected: HashMap<i64, &Signal> = signals.iter().map(|s| (s.id, s)).collect();
    if rows.len() != expected.len() {
        return Ok("changed");
    }
    if rows
        .iter()
        .any(|r| expected.get(&r.get::<_, i64>(0)) != Some(&&signal_row(r)))
    {
        return Ok("changed");
    }
    let consumed: Vec<(i64, bool)> = rows
        .iter()
        .map(|r| (r.get(0), r.get::<_, Option<f64>>(10).is_some()))
        .collect();
    if consumed.iter().all(|(_, c)| !c) {
        return Ok("pending");
    }
    let handled: HashSet<i64> = handled.iter().copied().collect();
    if !handled.is_empty() && consumed.iter().all(|(id, c)| *c == handled.contains(id)) {
        return Ok("consumed");
    }
    Ok("changed")
}

/// `lesson_lineage_sources(entity_norm, attribute_norm, lesson,
/// asserted_at)`: the trusted source entries of one exact lesson.
async fn lineage_sources(
    c: &Client,
    key: (&str, &str),
    lesson: &str,
    asserted_at: f64,
) -> Result<HashSet<i64>, tokio_postgres::Error> {
    let rows = c
        .query(
            "SELECT record FROM store_decisions WHERE store = 'lesson' \
             AND entity_norm = $1 AND attribute_norm = $2 \
             AND action = 'lineage' AND decided_by = 'lesson_synthesis' \
             AND reason = 'verified_dependencies'",
            &[&key.0, &key.1],
        )
        .await?;
    let mut out = HashSet::new();
    for r in rows {
        let Some(Value::Object(audit)) = r.get::<_, Option<Value>>(0) else {
            continue;
        };
        if audit.get("lesson").and_then(Value::as_str) != Some(lesson)
            || audit.get("asserted_at").and_then(Value::as_f64) != Some(asserted_at)
            || audit.get("asserted_at").is_some_and(Value::is_boolean)
        {
            continue;
        }
        if let Some(Value::Array(ids)) = audit.get("source_entry_ids") {
            out.extend(
                ids.iter()
                    .filter(|v| v.is_i64() || v.is_u64())
                    .filter_map(Value::as_i64),
            );
        }
    }
    Ok(out)
}

/// `consume_signals(ids)`: the rows newly marked consumed.
async fn consume_signals(c: &Client, ids: &[i64]) -> Result<u64, tokio_postgres::Error> {
    if ids.is_empty() {
        return Ok(0);
    }
    let now = crate::episodes::now();
    c.execute(
        "UPDATE outcome_signals SET consumed_at = $1 WHERE id = ANY($2) AND consumed_at IS NULL",
        &[&now, &ids],
    )
    .await
}

/// The lineage of one claim (`synthesize_lessons.lineage`): its provenance
/// tokens and valid time.
fn lineage(sources: &[&Signal], batch: bool) -> (Vec<String>, Option<f64>) {
    let mut prov: BTreeSet<String> = BTreeSet::new();
    if batch {
        prov.insert("lineage:batch".into());
    }
    for s in sources {
        prov.insert(format!("signal:{}", s.id));
        if let Some(ep) = s.episode_id.as_deref().filter(|e| !e.is_empty()) {
            prov.insert(format!("episode:{ep}"));
        }
        prov.extend(signal_source_ids(s).iter().map(|i| format!("entry:{i}")));
    }
    if !sources.is_empty()
        && sources
            .iter()
            .all(|s| s.origin.as_deref() == Some("inferred"))
    {
        prov.insert("inferred".into());
    }
    let valid = sources
        .iter()
        .map(|s| s.created_at)
        .fold(None, |m: Option<f64>, t| {
            Some(m.map_or(t, |m| if t < m { t } else { m }))
        });
    (prov.into_iter().collect(), valid)
}

fn failure_text(e: &ExtractFailure) -> String {
    match e {
        ExtractFailure::Extractor(x) => x.message.clone(),
        ExtractFailure::Uncaught(u) => u.message.clone(),
    }
}

/// A failure inside the commit transaction: a body error, or a statement
/// of the adapter itself (BEGIN, the commit probe, COMMIT).
enum TxFail {
    Tool(ToolError),
    Pg(tokio_postgres::Error),
}

impl From<ToolError> for TxFail {
    fn from(e: ToolError) -> Self {
        TxFail::Tool(e)
    }
}

impl From<tokio_postgres::Error> for TxFail {
    fn from(e: tokio_postgres::Error) -> Self {
        TxFail::Pg(e)
    }
}

fn tx_fail(e: TxFail) -> ToolError {
    match e {
        TxFail::Tool(t) => t,
        TxFail::Pg(p) => internal(p),
    }
}

/// What the commit transaction reports back.
#[derive(Default)]
struct Batch {
    skipped: bool,
    written: usize,
    deduped: usize,
    write_errors: usize,
    handled: Vec<i64>,
    writes_finished: bool,
}

/// `synthesize_lessons(extractor)`.
pub async fn synthesize_lessons(
    svc: &Arc<Service>,
    ready: &Arc<Ready>,
    caller: &Caller,
    writer: &str,
    extractor: &Extractor,
) -> Result<Value, ToolError> {
    let m = &svc.config.memory;
    if !(m.lessons_enabled && m.lessons_synthesize_in_dream) {
        return Ok(json!({"signals": 0, "lessons": 0, "skipped": "disabled"}));
    }
    let now = crate::episodes::now();
    let cutoff = now - m.lessons_signal_retention_days as f64 * 86400.0;
    let retry_days = m.lessons_signal_retry_days;
    let since = (retry_days > 0).then(|| now - retry_days as f64 * 86400.0);
    let cap = (m.lessons_synthesis_max_signals != 0).then_some(m.lessons_synthesis_max_signals);
    let state = &ready.dream_run;
    let signals = {
        let _ws = ready.write.lock().await;
        crate::txn::run(ready.storage.client(), |c| async move {
            c.execute(
                "DELETE FROM outcome_signals WHERE created_at < $1",
                &[&cutoff],
            )
            .await
            .map(|_| ())
        })
        .await
        .map_err(internal)?;
        let signals = select_signals(ready, cap, since).await?;
        if signals.is_empty() {
            let selected = *state.lesson_selected.lock().expect("lesson cursor");
            *state.lesson_cursor.lock().expect("lesson cursor") = selected;
        }
        signals
    };
    if signals.is_empty() {
        return Ok(json!({"signals": 0, "lessons": 0}));
    }
    let selected_cursor = *state.lesson_selected.lock().expect("lesson cursor");
    let all_inferred = signals
        .iter()
        .all(|s| s.origin.as_deref() == Some("inferred"));
    let Some(compat) = extractor.as_compat() else {
        return Ok(json!({"signals": signals.len(), "lessons": 0, "skipped": "no-extractor"}));
    };
    let rule_mode = m.lessons_rule_mode;
    let (rule_sigs, plain): (Vec<Signal>, Vec<Signal>) = signals
        .iter()
        .cloned()
        .partition(|s| is_rule_signal(s, rule_mode));
    let mut errors: Vec<String> = Vec::new();
    let mut plain_claims: Vec<LessonClaim> = Vec::new();
    let mut rule_claims: Vec<LessonClaim> = Vec::new();
    let mut rule_failed: HashSet<i64> = HashSet::new();
    let mut rule_empty: HashSet<i64> = HashSet::new();
    if !plain.is_empty() {
        match compat.extract_lessons(&plain).await {
            Ok(c) => plain_claims = c,
            Err(e) => errors.push(format!("plain extraction: {}", failure_text(&e))),
        }
    }
    if !rule_sigs.is_empty() {
        let ids: HashSet<i64> = rule_sigs.iter().map(|s| s.id).collect();
        match compat.extract_rules(&rule_sigs).await {
            Ok(r) => {
                rule_claims = r.claims;
                rule_failed = r
                    .failed_ids
                    .into_iter()
                    .filter(|i| ids.contains(i))
                    .collect();
                rule_empty = r
                    .empty_ids
                    .into_iter()
                    .filter(|i| ids.contains(i))
                    .collect();
            }
            Err(e) => {
                errors.push(format!("rule extraction: {}", failure_text(&e)));
                rule_failed = ids;
            }
        }
    }
    {
        let _ws = ready.write.lock().await;
        *state.lesson_cursor.lock().expect("lesson cursor") = selected_cursor;
    }
    let plain_handled: Vec<i64> = if plain_claims.is_empty() {
        Vec::new()
    } else {
        plain.iter().map(|s| s.id).collect()
    };
    let mut rule_handled: Vec<i64> = rule_sigs
        .iter()
        .map(|s| s.id)
        .filter(|i| !rule_failed.contains(i) && !rule_empty.contains(i))
        .collect();
    if rule_claims.len() != rule_handled.len() {
        errors.push("rule extraction did not identify every unhandled signal".into());
        rule_claims.clear();
        rule_handled.clear();
    }
    let claim_signals: Vec<Option<i64>> = std::iter::repeat_n(None, plain_claims.len())
        .chain(rule_handled.iter().copied().map(Some))
        .collect();
    let claims: Vec<LessonClaim> = plain_claims.into_iter().chain(rule_claims).collect();
    let mut out = Map::new();
    out.insert("signals".into(), json!(signals.len()));
    out.insert("lessons".into(), json!(0));
    out.insert("deduped".into(), json!(0));
    if !rule_sigs.is_empty() {
        out.insert("rule_signals".into(), json!(rule_sigs.len()));
    }
    if !rule_failed.is_empty() {
        out.insert("rules_failed".into(), json!(rule_failed.len()));
    }
    if !rule_empty.is_empty() {
        out.insert("rules_empty".into(), json!(rule_empty.len()));
    }
    if !errors.is_empty() {
        out.insert("error".into(), json!(errors.join("; ")));
    }
    if claims.is_empty() {
        return Ok(Value::Object(out));
    }
    let by_id: HashMap<i64, &Signal> = signals.iter().map(|s| (s.id, s)).collect();
    let plain_refs: Vec<&Signal> = plain.iter().collect();
    let dedup_thr = m.lessons_synthesis_dedup_min_similarity;

    let ws = ready.write.lock().await;
    crate::forget_tools::ensure_lessons_refreshed(ready)
        .await
        .map_err(internal)?;
    let mut batch = Batch::default();
    let result: Result<(), ToolError> = async {
        // Claim embeddings before the transaction (pure CPU, no SQL).
        let mut embeddings = Vec::with_capacity(claims.len());
        for c in &claims {
            let text = crate::lesson_write::embed_text(&c.task, &c.aspect, &c.lesson);
            if let Some(e) = super::extractor::unencodable(&[&text]) {
                return Err(internal(e));
            }
            embeddings.push(crate::fact_set::embed(ready, text).await?);
        }
        let session = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
        let mut staged: LessonStore = (*ready.lessons.snapshot()).clone();
        let handled_status: Vec<i64> = plain_handled.iter().chain(&rule_handled).copied().collect();
        let client = ready.storage.client();
        let bref = &mut batch;
        let staged_ref = &mut staged;
        let ctx = (
            &signals,
            &claims,
            &embeddings,
            &claim_signals,
            &by_id,
            &plain_refs,
        );
        crate::txn::run::<(), TxFail, _, _>(client, |c| async move {
            let (signals, claims, embeddings, claim_signals, by_id, plain_refs) = ctx;
            // `lesson_synthesis_transaction`: lock the contributors' source
            // rows, then revalidate and lock the selected signals.
            let contributors: Vec<&Signal> = signals
                .iter()
                .filter(|s| handled_status.contains(&s.id))
                .collect();
            let ids: Vec<i64> = contributors
                .iter()
                .flat_map(|s| signal_source_ids(s))
                .collect::<BTreeSet<i64>>()
                .into_iter()
                .collect();
            let existing = existing_entries(c, &ids, true)
                .await
                .map_err(|e| TxFail::Tool(internal(e)))?;
            let mut source_status: HashMap<i64, bool> =
                handled_status.iter().map(|&i| (i, false)).collect();
            for s in &contributors {
                source_status.insert(s.id, sources_survive(&signal_source_ids(s), &existing));
            }
            let current = batch_status(c, signals, &[], true)
                .await
                .map_err(internal)?
                == "pending";
            if !current {
                bref.skipped = true;
                return Ok(());
            }
            let plain_current = plain_handled.iter().all(|i| source_status[i]);
            let live = |sid: &Option<i64>| match sid {
                None => plain_current,
                Some(i) => source_status[i],
            };
            if !claim_signals.iter().any(live) {
                bref.skipped = true;
                return Ok(());
            }
            // A dirty current record from an earlier failed write joins
            // the graph in this transaction.
            for r in staged_ref.records.clone() {
                if staged_ref.dirty_slots.contains(&r.key) && r.status == "current" {
                    crate::lesson_write::link_lesson_graph_in(
                        c,
                        &r.entity,
                        r.about.as_deref(),
                        &r.polarity,
                    )
                    .await?;
                }
            }
            let mut failed_rule: HashSet<i64> = HashSet::new();
            let mut plain_landed = 0usize;
            for ((claim, emb), sid) in claims
                .iter()
                .zip(embeddings.iter())
                .zip(claim_signals.iter())
            {
                if !live(sid) {
                    if let Some(i) = sid {
                        failed_rule.insert(*i);
                    }
                    continue;
                }
                let sources: Vec<&Signal> = match sid {
                    None => plain_refs.clone(),
                    Some(i) => vec![by_id[i]],
                };
                let (provenance, valid_time) = lineage(&sources, sid.is_none());
                let landed: Result<bool, ToolError> = async {
                    if dedup_thr != 0.0
                        && claim.aspect != "rule"
                        && duplicate(staged_ref, claim, emb, dedup_thr)?
                    {
                        return Ok(false);
                    }
                    let undo = staged_ref.slot_undo(&claim.task, &claim.aspect);
                    c.batch_execute("SAVEPOINT lesson_claim")
                        .await
                        .map_err(internal)?;
                    let wrote = write_claim(
                        c,
                        staged_ref,
                        claim,
                        emb,
                        LessonWrite {
                            about: claim.about.as_deref(),
                            outcome: &claim.outcome,
                            polarity: &claim.polarity,
                            confidence: if all_inferred { 0.4 } else { claim.confidence },
                            origin: Some("agent"),
                            provenance: &provenance,
                            support: &[],
                            now: crate::episodes::now(),
                            hlc: Some(ready.tick_hlc()),
                            valid_time,
                            writer_id: Some(writer),
                            session_id: session.as_deref(),
                        },
                        now,
                    )
                    .await;
                    match wrote {
                        Ok(()) => {
                            c.batch_execute("RELEASE SAVEPOINT lesson_claim")
                                .await
                                .map_err(internal)?;
                            Ok(true)
                        }
                        Err(e) => {
                            staged_ref.undo(undo);
                            c.batch_execute("ROLLBACK TO SAVEPOINT lesson_claim")
                                .await
                                .map_err(internal)?;
                            Err(e)
                        }
                    }
                }
                .await;
                match landed {
                    Ok(false) => bref.deduped += 1,
                    Ok(true) => bref.written += 1,
                    Err(e) => {
                        bref.write_errors += 1;
                        if let Some(i) = sid {
                            failed_rule.insert(*i);
                        }
                        eprintln!("lesson claim failed ({e:?})");
                        continue;
                    }
                }
                if sid.is_none() {
                    plain_landed += 1;
                }
            }
            let mut handled: Vec<i64> = if plain_landed > 0
                || crate::mutants::active("mcp-dream-stage-lessons-ack-unlanded")
            {
                plain_handled.clone()
            } else {
                Vec::new()
            };
            handled.extend(rule_handled.iter().filter(|i| !failed_rule.contains(i)));
            let synced = crate::stores::lessons::sync_slot_rows_in(c, staged_ref)
                .await
                .map_err(internal)?;
            for key in &synced {
                staged_ref.dirty_slots.remove(key);
            }
            let consumed = consume_signals(c, &handled).await.map_err(internal)?;
            if consumed != handled.len() as u64 {
                return Err(TxFail::Tool(internal(
                    "lesson acknowledgement count changed",
                )));
            }
            bref.handled = handled;
            bref.writes_finished = true;
            Ok::<(), TxFail>(())
        })
        .await
        .map_err(tx_fail)?;
        if !batch.skipped {
            ready.lessons.write(|s| *s = staged);
        }
        Ok(())
    }
    .await;
    match result {
        Ok(()) if batch.skipped => {
            out.insert("skipped".into(), json!("signals-changed"));
        }
        Ok(()) => {
            out.insert("lessons".into(), json!(batch.written));
            out.insert("deduped".into(), json!(batch.deduped));
            if batch.write_errors > 0 {
                out.insert("write_errors".into(), json!(batch.write_errors));
            }
        }
        Err(e) => {
            svc.persist_errors
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            errors.push(error_text(&e));
            out.insert("error".into(), json!(errors.join("; ")));
            if batch.write_errors > 0 {
                out.insert("write_errors".into(), json!(batch.write_errors));
            }
            if batch.writes_finished {
                // COMMIT may have landed despite the error: reconcile.
                match recover(ready, &signals, &batch.handled).await {
                    Ok("consumed") => {
                        out.insert("lessons".into(), json!(batch.written));
                        out.insert("deduped".into(), json!(batch.deduped));
                    }
                    Ok(_) => {}
                    Err(why) => {
                        let text = format!(
                            "{}; lesson commit reconciliation required: {why}",
                            out["error"].as_str().unwrap_or("")
                        );
                        out.insert("error".into(), json!(text));
                        out.insert("reconciliation_required".into(), json!(true));
                    }
                }
            }
            eprintln!("lesson batch failed ({e:?}); retry or durable reconciliation required");
        }
    }
    drop(ws);
    Ok(Value::Object(out))
}

/// `str(exc)` of a failed batch, as near as the daemon can say it.
fn error_text(e: &ToolError) -> String {
    match e {
        ToolError::Internal(x) => x.to_string(),
        other => format!("{other:?}"),
    }
}

/// `_recover_lesson_synthesis`: a committed batch is rehydrated; a rolled
/// back one leaves the resident store as it was.
async fn recover(
    ready: &Ready,
    signals: &[Signal],
    handled: &[i64],
) -> Result<&'static str, String> {
    let client = ready.storage.client();
    let status = crate::txn::with_client(client, |c| async move {
        batch_status(c, signals, handled, false).await
    })
    .await
    .map_err(|e| e.to_string())?;
    match status {
        "consumed" => {
            let restored = crate::stores::lessons::hydrate(client)
                .await
                .map_err(|e| e.to_string())?;
            ready.lessons.write(|s| *s = restored);
            Ok(status)
        }
        "pending" => Ok(status),
        _ => Err("selected lesson signals changed during commit recovery".into()),
    }
}

/// `_lesson_duplicate_locked`: a current lesson at another slot with the
/// same polarity at or above the threshold (top three by cosine).
fn duplicate(
    staged: &LessonStore,
    c: &LessonClaim,
    emb: &[f32],
    threshold: f64,
) -> Result<bool, ToolError> {
    let key = crate::stores::cortex::slot_key(&c.task, &c.aspect);
    let hits = staged.search(emb, 3, threshold).map_err(internal)?;
    Ok(hits.into_iter().any(|(i, _)| {
        let r = &staged.records[i];
        r.key != key && r.polarity == c.polarity
    }))
}

/// One claim's write inside its savepoint: the staged store write and
/// graph link, then the lineage audit for the sources it newly verifies.
#[allow(clippy::too_many_arguments)]
async fn write_claim(
    c: &Client,
    staged: &mut LessonStore,
    claim: &LessonClaim,
    emb: &Arc<[f32]>,
    w: LessonWrite<'_>,
    now: f64,
) -> Result<(), ToolError> {
    let provenance = w.provenance;
    // psycopg refuses a reply's lone surrogate in the graph link's `about`
    // (the claim's other texts were refused by the embedder already).
    if let Some(e) = super::extractor::unencodable(&[claim.about.as_deref().unwrap_or("")]) {
        return Err(internal(e));
    }
    let (action, i) = crate::lesson_write::write_staged(
        c,
        staged,
        &claim.task,
        &claim.aspect,
        &claim.lesson,
        emb.clone(),
        &w,
    )
    .await?;
    let mut source_ids: Vec<i64> = provenance
        .iter()
        .filter_map(|p| p.strip_prefix("entry:"))
        .filter_map(|n| n.parse().ok())
        .collect();
    source_ids.sort_unstable();
    let (key, value, asserted_at) = {
        let r = &staged.records[i];
        (r.key.clone(), r.value.clone(), r.asserted_at)
    };
    if action == "confirmed" {
        let known = lineage_sources(c, (&key.0, &key.1), &value, asserted_at)
            .await
            .map_err(internal)?;
        source_ids = if known.is_empty() {
            Vec::new()
        } else {
            source_ids
                .into_iter()
                .filter(|i| !known.contains(i))
                .collect()
        };
    } else if action != "inserted" && action != "superseded" {
        source_ids.clear();
    }
    if !source_ids.is_empty() && !crate::mutants::active("mcp-dream-stage-lessons-no-lineage") {
        let record = json!({"lesson": value, "asserted_at": asserted_at,
                            "source_entry_ids": source_ids});
        crate::forget_tools::record_store_decision(
            c,
            "lesson",
            (&key.0, &key.1),
            "lineage",
            Some("lesson_synthesis"),
            Some("verified_dependencies"),
            &record,
            now,
        )
        .await
        .map_err(internal)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sig(id: i64, origin: Option<&str>, used: Value, at: f64) -> Signal {
        Signal {
            id,
            task: "t".into(),
            outcome: "success".into(),
            about: None,
            detail: None,
            polarity: None,
            origin: origin.map(str::to_string),
            episode_id: Some("ep".into()),
            created_at: at,
            used_ids: Some(used),
        }
    }

    #[test]
    fn source_ids_keep_positive_integers_only() {
        let s = sig(
            1,
            None,
            json!({"credited": [3, -1, 0, 2.0, true, "4", 7]}),
            1.0,
        );
        assert_eq!(signal_source_ids(&s), BTreeSet::from([3, 7]));
        let s = sig(1, None, json!({"credited": "3"}), 1.0);
        assert!(signal_source_ids(&s).is_empty());
        assert!(sources_survive(&BTreeSet::new(), &HashSet::new()));
        assert!(!sources_survive(&BTreeSet::from([1]), &HashSet::from([2])));
    }

    #[test]
    fn lineage_names_sources_and_the_inferred_batch() {
        let a = sig(1, Some("inferred"), json!({"credited": [5]}), 20.0);
        let b = sig(2, Some("inferred"), json!(null), 10.0);
        let (prov, valid) = lineage(&[&a, &b], true);
        assert_eq!(
            prov,
            [
                "entry:5",
                "episode:ep",
                "inferred",
                "lineage:batch",
                "signal:1",
                "signal:2"
            ]
        );
        assert_eq!(valid, Some(10.0));
        let c = sig(3, None, json!({}), 5.0);
        let (prov, _) = lineage(&[&c], false);
        assert_eq!(prov, ["episode:ep", "signal:3"]);
    }
}
