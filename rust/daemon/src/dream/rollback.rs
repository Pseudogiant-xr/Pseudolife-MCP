//! `memory_dream(action="rollback")`: `service_dream.dream_rollback`
//! (2570-2925). The latest committed dream pass is reverted by replaying
//! its v27 pre-image journal (`dream_run_slots`) in reverse through the
//! normal write paths (supersede-back: nothing is deleted but the run's
//! chronicle events). Refused when a newer run is `failed` or `running`, or
//! when `run_id` names anything but the latest committed run. Source traces
//! are kept and the dream cursor is not rewound.

use std::collections::{BTreeSet, HashSet};
use std::sync::Arc;

use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use crate::episodes::{self, Caller};
use crate::fact_resolve::{ResolveArgs, cortex_resolve};
use crate::fact_set::{CortexWriteArgs, LabelArg, cortex_write, save_cortex};
use crate::graph_review_tools::actions::{in_txn, reads};
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};
use crate::set_members::{SetAddArgs, SetRemoveArgs, set_add, set_remove};
use crate::stores::cortex::CortexRecord;
use crate::stores::cortex_write::norm_value;

/// `_DIRECT_TIERS` (`cortex.py:210`).
const DIRECT_TIERS: [&str; 2] = ["user", "action"];

/// `_TIER_RANK` (`cortex.py:254`).
fn tier_rank(t: &str) -> Option<i32> {
    match t {
        "user" => Some(3),
        "action" => Some(2),
        "agent" => Some(1),
        "assistant" => Some(0),
        _ => None,
    }
}

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

/// One `dream_runs` row as the rollback reads it.
struct Run {
    id: i64,
    started_at: f64,
    finished_at: Option<f64>,
    status: String,
}

/// One `dream_run_slots` journal row.
#[derive(Clone)]
struct Slot {
    seq: i64,
    entity: String,
    attribute: String,
    kind: String,
    prev_kind: Option<String>,
    prev_value: Option<String>,
    prev_status: Option<String>,
    prev_confidence: Option<f64>,
    prev_support: Option<String>,
    new_value: Option<String>,
    action: String,
    at: f64,
    chronicle_event_id: Option<i64>,
}

impl Slot {
    fn prev(&self) -> &str {
        self.prev_value.as_deref().unwrap_or("")
    }

    fn written(&self) -> &str {
        self.new_value.as_deref().unwrap_or("")
    }

    /// `row["prev_support"] or "agent"`.
    fn prev_support_or_agent(&self) -> String {
        self.prev_support
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "agent".into())
    }

    /// `float(row["prev_confidence"] or 0.55)`.
    fn prev_confidence_or(&self) -> f64 {
        self.prev_confidence.filter(|c| *c != 0.0).unwrap_or(0.55)
    }
}

const RUN_SQL: &str = "SELECT id, started_at, finished_at, status FROM dream_runs";

fn run_of(r: &tokio_postgres::Row) -> Run {
    Run {
        id: r.get(0),
        started_at: r.get(1),
        finished_at: r.get(2),
        status: r.get(3),
    }
}

async fn latest_committed(c: &Client) -> Result<Option<Run>, ToolError> {
    Ok(c.query_opt(
        &format!("{RUN_SQL} WHERE status = 'committed' ORDER BY id DESC LIMIT 1"),
        &[],
    )
    .await
    .map_err(internal)?
    .as_ref()
    .map(run_of))
}

async fn newer_than(c: &Client, id: i64) -> Result<Vec<Run>, ToolError> {
    Ok(
        c.query(&format!("{RUN_SQL} WHERE id > $1 ORDER BY id"), &[&id])
            .await
            .map_err(internal)?
            .iter()
            .map(run_of)
            .collect(),
    )
}

async fn journal(c: &Client, id: i64) -> Result<Vec<Slot>, ToolError> {
    Ok(c.query(
        "SELECT seq, entity, attribute, kind, prev_kind, prev_value, prev_status, \
         prev_confidence::text::double precision, prev_support, new_value, action, at, \
         chronicle_event_id FROM dream_run_slots WHERE run_id = $1 ORDER BY seq",
        &[&id],
    )
    .await
    .map_err(internal)?
    .iter()
    .map(|r| Slot {
        seq: i64::from(r.get::<_, i32>(0)),
        entity: r.get(1),
        attribute: r.get(2),
        kind: r.get(3),
        prev_kind: r.get(4),
        prev_value: r.get(5),
        prev_status: r.get(6),
        prev_confidence: r.get(7),
        prev_support: r.get(8),
        new_value: r.get(9),
        action: r.get(10),
        at: r.get(11),
        chronicle_event_id: r.get(12),
    })
    .collect())
}

fn set_of(v: &[String]) -> BTreeSet<String> {
    v.iter().cloned().collect()
}

/// The first record of `recs` with the greatest `superseded_at or 0`
/// (Python's `max`, which keeps the first of equal maxima).
fn newest<'a>(recs: impl Iterator<Item = &'a CortexRecord>) -> Option<&'a CortexRecord> {
    let mut best: Option<&CortexRecord> = None;
    for r in recs {
        let at = r.superseded_at.unwrap_or(0.0);
        if best.is_none_or(|b| at > b.superseded_at.unwrap_or(0.0)) {
            best = Some(r);
        }
    }
    best
}

/// The run-window context the tier recovery reads.
struct Window {
    started_at: f64,
    finished_at: Option<f64>,
    rollback_started: f64,
}

/// `(support, dream marks)` recovered for the displaced record.
type Tiers = (BTreeSet<String>, BTreeSet<String>);

/// The replay's state: the service, the target run's window, the caller.
struct Replay<'a> {
    svc: &'a Service,
    ready: &'a Arc<Ready>,
    caller: &'a Caller,
    writer: &'a str,
    window: Window,
}

impl Replay<'_> {
    fn records(&self, row: &Slot) -> Vec<CortexRecord> {
        let snap = self.ready.cortex.snapshot();
        snap.records_for(&row.entity, &row.attribute)
            .into_iter()
            .map(|i| snap.records[i].clone())
            .collect()
    }

    /// `_prev_stance(row)`.
    async fn prev_stance(&self, row: &Slot) -> Option<String> {
        let want = norm_value(row.prev());
        let _ws = self.ready.write.lock().await;
        let recs = self.records(row);
        newest(recs.iter().filter(|r| {
            r.status == "superseded"
                && r.stance.as_deref().is_some_and(|s| !s.is_empty())
                && norm_value(&r.value) == want
        }))
        .and_then(|r| r.stance.clone())
    }

    /// `_prev_label(row, attr)`.
    async fn prev_label(&self, row: &Slot, authority: bool) -> Option<String> {
        let want = norm_value(row.prev());
        let _ws = self.ready.write.lock().await;
        let recs = self.records(row);
        newest(
            recs.iter()
                .filter(|r| r.status == "superseded" && norm_value(&r.value) == want),
        )
        .and_then(|r| {
            if authority {
                r.authority.clone()
            } else {
                r.distortion_tolerance.clone()
            }
        })
    }

    /// `_prev_tiers(row)`, the caller holding the lock: the support and
    /// dream marks of the record this run displaced, with any direct
    /// backing that arrived since; None when audit history was compacted.
    fn prev_tiers(&self, row: &Slot) -> Option<Tiers> {
        let want = norm_value(row.prev());
        let kind = if row.prev_kind.as_deref() == Some("set") {
            "member"
        } else {
            "scalar"
        };
        let all = self.records(row);
        let recs: Vec<&CortexRecord> = all
            .iter()
            .filter(|r| (r.kind == kind || r.status == "contested") && norm_value(&r.value) == want)
            .collect();
        let lo = self.window.started_at;
        let hi = self
            .window
            .finished_at
            .unwrap_or(self.window.rollback_started);
        let row_at = if row.at != 0.0 { row.at } else { hi };
        let displaced = |r: &CortexRecord| {
            let at = r.superseded_at.unwrap_or(0.0);
            if !(lo <= at && at <= hi.min(row_at)) {
                return false;
            }
            r.status != "superseded"
                || r.kind != "scalar"
                || row.kind != "scalar"
                || norm_value(r.superseded_by_value.as_deref().unwrap_or(""))
                    == norm_value(row.written())
        };
        let prev =
            newest(recs.iter().copied().filter(|r| {
                matches!(r.status.as_str(), "superseded" | "removed") && displaced(r)
            }))?;
        let mut support = set_of(&prev.support);
        let mut marks: BTreeSet<String> = prev
            .provenance
            .iter()
            .filter(|p| p.starts_with("dream:"))
            .cloned()
            .collect();
        for rec in recs
            .iter()
            .filter(|r| matches!(r.status.as_str(), "current" | "contested"))
        {
            let carried: BTreeSet<String> = if rec.status == "contested" {
                rec.support
                    .iter()
                    .filter(|t| *t != "assistant")
                    .cloned()
                    .collect()
            } else {
                set_of(&rec.support)
            };
            let mut direct: BTreeSet<String> = support
                .iter()
                .filter(|t| !marks.contains(&format!("dream:{t}")))
                .cloned()
                .collect();
            direct.extend(
                carried
                    .iter()
                    .filter(|t| !rec.provenance.contains(&format!("dream:{t}")))
                    .cloned(),
            );
            support.extend(carried.iter().cloned());
            marks.extend(
                rec.provenance
                    .iter()
                    .filter(|p| p.starts_with("dream:"))
                    .cloned(),
            );
            for t in &direct {
                marks.remove(&format!("dream:{t}"));
            }
        }
        Some((support, marks))
    }

    /// `_direct_backing(row, rec, own)`, the caller holding the lock: every
    /// direct tier now backing the restored value.
    fn direct_backing(&self, row: &Slot, rec: usize, own: &HashSet<String>) -> BTreeSet<String> {
        let want = norm_value(row.prev());
        let snap = self.ready.cortex.snapshot();
        let mut keep = BTreeSet::new();
        for i in snap.records_for(&row.entity, &row.attribute) {
            let r = &snap.records[i];
            if norm_value(&r.value) != want {
                continue;
            }
            let direct: BTreeSet<String> = r
                .support
                .iter()
                .filter(|t| {
                    DIRECT_TIERS.contains(&t.as_str())
                        && !r.provenance.contains(&format!("dream:{t}"))
                })
                .cloned()
                .collect();
            if i == rec {
                keep.extend(direct.into_iter().filter(|t| !own.contains(t)));
            } else if matches!(r.status.as_str(), "current" | "contested")
                || (matches!(r.status.as_str(), "superseded" | "removed")
                    && r.superseded_at.unwrap_or(0.0) >= self.window.rollback_started)
            {
                keep.extend(direct);
            }
        }
        keep
    }

    /// `_restore_tiers(rec, tiers, keep)`, the caller holding the lock.
    fn restore_tiers(&self, rec: usize, tiers: Option<&Tiers>, keep: &BTreeSet<String>) {
        self.ready.cortex.write(|s| {
            let r = &mut s.records[rec];
            match tiers {
                Some((support, marks)) => {
                    let mut sup: BTreeSet<String> = support.clone();
                    sup.extend(keep.iter().cloned());
                    let dropped: BTreeSet<String> =
                        keep.iter().map(|t| format!("dream:{t}")).collect();
                    let mut prov: BTreeSet<String> = r
                        .provenance
                        .iter()
                        .filter(|p| !p.starts_with("dream:"))
                        .cloned()
                        .collect();
                    prov.extend(marks.iter().filter(|m| !dropped.contains(*m)).cloned());
                    r.support = sup.into_iter().collect();
                    r.provenance = prov.into_iter().collect();
                }
                None if !keep.is_empty() => {
                    // Compacted history: the journal-tier write stands, and
                    // the direct backing found now survives on top of it.
                    let mut sup = set_of(&r.support);
                    sup.extend(keep.iter().cloned());
                    r.support = sup.into_iter().collect();
                    r.provenance
                        .retain(|p| !keep.iter().any(|t| *p == format!("dream:{t}")));
                }
                None => return,
            }
            let key = r.key().clone();
            s.dirty_slots.insert(key);
        });
    }

    /// `lookup(entity, attribute)` by index: the slot's current scalar.
    fn current_index(&self, row: &Slot) -> Option<usize> {
        let snap = self.ready.cortex.snapshot();
        let key = crate::stores::cortex::slot_key(&row.entity, &row.attribute);
        snap.current
            .get(&key)
            .copied()
            .filter(|&i| snap.records[i].status == "current")
    }

    /// The index of `current_index` when it holds `value`.
    fn current_holding(&self, row: &Slot, value: &str) -> Option<usize> {
        let snap = self.ready.cortex.snapshot();
        self.current_index(row)
            .filter(|&i| norm_value(&snap.records[i].value) == norm_value(value))
    }

    /// The first current member holding `value`, by index.
    fn member_holding(&self, row: &Slot, value: &str) -> Option<usize> {
        let snap = self.ready.cortex.snapshot();
        let want = norm_value(value);
        let key = crate::stores::cortex::slot_key(&row.entity, &row.attribute);
        snap.members
            .get(&key)
            .into_iter()
            .flatten()
            .copied()
            .find(|&i| {
                snap.records[i].status == "current" && norm_value(&snap.records[i].value) == want
            })
    }

    /// `_rewrite_prev(row)`: rewrite the previous value through the normal
    /// write path (resolving a contest it raises), then its tiers.
    async fn rewrite_prev(&self, row: &Slot) -> Result<String, ToolError> {
        let tiers = {
            let _ws = self.ready.write.lock().await;
            self.prev_tiers(row)
        };
        let stance = self.prev_stance(row).await;
        let authority = self.prev_label(row, true).await;
        let distortion = self.prev_label(row, false).await;
        let mut a = CortexWriteArgs::new(&row.entity, &row.attribute, row.prev());
        a.confidence = row.prev_confidence_or();
        a.support = Some(row.prev_support_or_agent());
        a.stance = stance;
        a.authority = LabelArg::Requested(authority);
        a.distortion_tolerance = LabelArg::Requested(distortion);
        let res = cortex_write(
            self.svc,
            self.ready,
            self.caller,
            self.writer.to_string(),
            a,
        )
        .await?;
        let mut own: HashSet<String> = [row.prev_support_or_agent()].into();
        if res.get("action").and_then(Value::as_str) == Some("contested") {
            // Rollback is explicit authority: a low-confidence previous
            // value still wins the slot back (the resolve path).
            cortex_resolve(
                self.svc,
                self.ready,
                ResolveArgs::new(&row.entity, &row.attribute, true),
            )
            .await?;
            own.insert("user".into());
        }
        let _ws = self.ready.write.lock().await;
        match self.current_holding(row, row.prev()) {
            Some(cur) => {
                let keep = self.direct_backing(row, cur, &own);
                self.restore_tiers(cur, tiers.as_ref(), &keep);
                Ok("reverted".into())
            }
            None => Ok("partial:value_not_restored".into()),
        }
    }

    /// `_restore_contender(row)`: re-park a contender the reverted write
    /// settled, in the run's window or this rollback's.
    async fn restore_contender(&self, row: &Slot) -> Option<String> {
        if crate::mutants::active("mcp-dream-rollback-contender-lost") {
            return None;
        }
        let _ws = self.ready.write.lock().await;
        let now = episodes::now();
        let (since, until, rb) = (
            self.window.started_at,
            self.window.finished_at,
            self.window.rollback_started,
        );
        let i = self.ready.cortex.write(|s| {
            s.restore_settled_contender(
                &row.entity,
                &row.attribute,
                row.written(),
                since,
                until,
                now,
            )
            .or_else(|| {
                s.restore_settled_contender(
                    &row.entity,
                    &row.attribute,
                    row.written(),
                    rb,
                    None,
                    now,
                )
            })
        })?;
        Some(self.ready.cortex.snapshot().records[i].value.clone())
    }

    /// `_restore_confirmed_tiers(row)`.
    async fn restore_confirmed_tiers(&self, row: &Slot) -> String {
        let Some(prior) = row.prev_support.as_deref().and_then(tier_rank) else {
            return "skipped:confirmed".into();
        };
        let _ws = self.ready.write.lock().await;
        let rec = if row.action == "confirmed" {
            self.current_holding(row, row.prev())
        } else {
            self.member_holding(row, row.prev())
        };
        let Some(rec) = rec else {
            return "skipped:superseded_by_later".into();
        };
        let retained_set =
            row.action == "member_confirmed" && row.prev_kind.as_deref() == Some("scalar");
        let raised: BTreeSet<String> = {
            let snap = self.ready.cortex.snapshot();
            let r = &snap.records[rec];
            r.support
                .iter()
                .filter(|t| {
                    tier_rank(t).unwrap_or(0) > prior
                        && r.provenance.contains(&format!("dream:{t}"))
                })
                .cloned()
                .collect()
        };
        if raised.is_empty() || crate::mutants::active("mcp-dream-rollback-tiers-kept") {
            return if retained_set {
                "partial:set_retained".into()
            } else {
                "skipped:confirmed".into()
            };
        }
        self.ready.cortex.write(|s| {
            let r = &mut s.records[rec];
            r.support.retain(|t| !raised.contains(t));
            r.provenance
                .retain(|p| !raised.iter().any(|t| *p == format!("dream:{t}")));
            let key = r.key().clone();
            s.dirty_slots.insert(key);
        });
        if retained_set {
            "partial:set_retained".into()
        } else {
            "reverted".into()
        }
    }

    /// `retire_current` under the lock: `reverted` or `skipped:already_gone`.
    async fn retire(&self, row: &Slot) -> String {
        let _ws = self.ready.write.lock().await;
        let now = episodes::now();
        match self
            .ready
            .cortex
            .write(|s| s.retire_current(&row.entity, &row.attribute, now))
        {
            Some(_) => "reverted".into(),
            None => "skipped:already_gone".into(),
        }
    }

    /// One journal row's reversal: `(outcome, contender restored)`.
    async fn reverse(&self, row: &Slot) -> Result<(String, Option<String>), ToolError> {
        let mut restored = None;
        let outcome = if row.action == "contested" {
            // The snapshot is released before the resolve, whose store write
            // would otherwise copy the whole store to keep it.
            let parked = self
                .ready
                .cortex
                .snapshot()
                .contenders_for(&row.entity, &row.attribute)
                .iter()
                .any(|c| norm_value(&c.value) == norm_value(row.written()));
            if parked {
                cortex_resolve(
                    self.svc,
                    self.ready,
                    ResolveArgs::new(&row.entity, &row.attribute, false),
                )
                .await?;
                "reverted".into()
            } else {
                "skipped:superseded_by_later".into()
            }
        } else if row.kind == "event" {
            // Chronicle rows are additive-only: the reversal of an insert
            // deletes the exact row the journal names.
            let mut deleted = false;
            if let Some(id) = row
                .chronicle_event_id
                .filter(|_| !crate::mutants::active("mcp-dream-rollback-event-kept"))
            {
                let _ws = self.ready.write.lock().await;
                deleted = in_txn(self.ready.storage.client(), |c| async move {
                    Ok(
                        c.execute("DELETE FROM chronicle_events WHERE id = $1", &[&id])
                            .await
                            .map_err(internal)?
                            > 0,
                    )
                })
                .await?;
            }
            if deleted {
                "reverted".into()
            } else {
                "skipped:already_gone".into()
            }
        } else if matches!(row.action.as_str(), "confirmed" | "member_confirmed") {
            self.restore_confirmed_tiers(row).await
        } else if row.kind == "scalar" {
            match row.action.as_str() {
                "inserted" if row.prev_status.is_none() => {
                    let o = self.retire(row).await;
                    restored = self.restore_contender(row).await;
                    o
                }
                "superseded" => {
                    let o = self.rewrite_prev(row).await?;
                    restored = self.restore_contender(row).await;
                    o
                }
                // The reversal of a two-man promotion restores the previous
                // current; one onto an empty slot reverses like an insert.
                "quarantine_promoted" if row.prev_status.is_none() => self.retire(row).await,
                "quarantine_promoted" => self.rewrite_prev(row).await?,
                _ => "skipped:no_reversal".into(),
            }
        } else if row.action == "member_added" {
            set_remove(
                self.svc,
                self.ready,
                SetRemoveArgs::new(&row.entity, &row.attribute, row.written()),
            )
            .await?;
            let mut o = "reverted".to_string();
            if row.prev_kind.as_deref() == Some("scalar")
                && row.prev_value.as_deref().is_some_and(|v| !v.is_empty())
            {
                let survivors: Vec<String> = {
                    let _ws = self.ready.write.lock().await;
                    let snap = self.ready.cortex.snapshot();
                    snap.members(&row.entity, &row.attribute)
                        .iter()
                        .map(|m| norm_value(&m.value))
                        .collect()
                };
                if survivors == [norm_value(row.prev())] {
                    // The sole survivor is the converted scalar: unwind the
                    // one-way scalar-to-set conversion.
                    set_remove(
                        self.svc,
                        self.ready,
                        SetRemoveArgs::new(&row.entity, &row.attribute, row.prev()),
                    )
                    .await?;
                    o = self.rewrite_prev(row).await?;
                } else {
                    o = "partial:set_retained".into();
                }
            }
            restored = self.restore_contender(row).await;
            o
        } else if row.action == "member_removed" {
            let tiers = {
                let _ws = self.ready.write.lock().await;
                self.prev_tiers(row)
            };
            let mut a = SetAddArgs::new(&row.entity, &row.attribute, row.prev());
            a.confidence = row.prev_confidence_or();
            a.origin = Some(row.prev_support_or_agent());
            let res = set_add(
                self.svc,
                self.ready,
                self.caller,
                self.writer.to_string(),
                a,
            )
            .await?;
            {
                let _ws = self.ready.write.lock().await;
                if let Some(rec) = self.member_holding(row, row.prev()) {
                    let own: HashSet<String> = [row.prev_support_or_agent()].into();
                    let keep = self.direct_backing(row, rec, &own);
                    self.restore_tiers(rec, tiers.as_ref(), &keep);
                }
            }
            // An assistant-origin member re-added into a set that has since
            // gained a stronger member parks as a contender instead.
            if res.get("action").and_then(Value::as_str) == Some("contested") {
                "partial:member_not_restored".into()
            } else {
                "reverted".into()
            }
        } else {
            "skipped:no_reversal".into()
        };
        Ok((outcome, restored))
    }
}

/// The Python exception class a reversal failure names in its outcome
/// (`partial:error:<class>`).
fn error_class(e: &ToolError) -> &'static str {
    match e {
        ToolError::Invalid(_) => "ValueError",
        // save_cortex preserves Python's PersistenceError as an internal
        // error with this fixed prefix (fact_set.rs). Keep its class in
        // per-row outcomes while later reversals and the final save proceed.
        ToolError::Internal(cause) if cause.to_string().starts_with("cortex save failed: ") => {
            "PersistenceError"
        }
        _ => "RuntimeError",
    }
}

/// `dream_rollback(run_id=run_id)`. `run_id` is None when omitted or null,
/// and `Some(None)` for an integer no run id can equal (beyond i64), which
/// is stale as in Python.
pub(crate) async fn rollback(
    svc: &Service,
    ready: &Arc<Ready>,
    caller: &Caller,
    writer: &str,
    run_id: Option<Option<i64>>,
) -> Result<Value, ToolError> {
    let client = ready.storage.client();
    let target = {
        let _ws = ready.write.lock().await;
        reads(client, |c| async move { latest_committed(c).await }).await?
    };
    let Some(target) = target else {
        return Ok(json!({"error": "no_committed_run"}));
    };
    if run_id.is_some_and(|r| r != Some(target.id)) {
        return Ok(json!({"error": "stale_run_id", "latest": target.id}));
    }
    let newer = {
        let _ws = ready.write.lock().await;
        let id = target.id;
        reads(client, |c| async move { newer_than(c, id).await }).await?
    };
    let blocking: Vec<Value> = newer
        .iter()
        .filter(|r| matches!(r.status.as_str(), "failed" | "running"))
        .map(|r| json!({"id": r.id, "status": r.status}))
        .collect();
    if !blocking.is_empty() {
        return Ok(json!({"error": "newer_unjournaled_runs", "runs": blocking}));
    }
    let rows = {
        let _ws = ready.write.lock().await;
        let id = target.id;
        reads(client, |c| async move { journal(c, id).await }).await?
    };
    let replay = Replay {
        svc,
        ready,
        caller,
        writer,
        window: Window {
            started_at: target.started_at,
            finished_at: target.finished_at,
            rollback_started: episodes::now(),
        },
    };
    let mut counts: Vec<(String, i64)> = vec![
        ("reverted".into(), 0),
        ("skipped".into(), 0),
        ("partial".into(), 0),
    ];
    let mut details = Vec::new();
    for row in rows.iter().rev() {
        let (outcome, restored) = match replay.reverse(row).await {
            Ok(x) => x,
            Err(e) => {
                eprintln!(
                    "rollback: reversal failed at seq {} ({})",
                    row.seq,
                    super::judge::err_text(&e)
                );
                (format!("partial:error:{}", error_class(&e)), None)
            }
        };
        let bucket = outcome.split(':').next().unwrap_or("").to_string();
        match counts.iter_mut().find(|(b, _)| *b == bucket) {
            Some((_, n)) => *n += 1,
            None => counts.push((bucket, 1)),
        }
        let mut detail = Map::new();
        detail.insert("seq".into(), json!(row.seq));
        detail.insert("entity".into(), json!(row.entity));
        detail.insert("attribute".into(), json!(row.attribute));
        detail.insert("action".into(), json!(row.action));
        detail.insert("outcome".into(), json!(outcome));
        if let Some(r) = restored {
            detail.insert("contender_restored".into(), json!(r));
        }
        details.push(Value::Object(detail));
    }
    {
        let _ws = ready.write.lock().await;
        save_cortex(svc, ready).await?;
        if crate::mutants::active("mcp-dream-rollback-audit-kept") {
            return Ok(json!({"run_id": target.id}));
        }
        let (id, at) = (target.id, episodes::now());
        in_txn(client, |c| async move {
            c.execute(
                "UPDATE dream_runs SET status='rolled_back', rolled_back_at=$1 WHERE id=$2",
                &[&at, &id],
            )
            .await
            .map_err(internal)?;
            Ok(())
        })
        .await?;
    }
    let mut out = Map::new();
    out.insert("run_id".into(), json!(target.id));
    for (b, n) in counts {
        out.insert(b, json!(n));
    }
    out.insert("details".into(), Value::Array(details));
    Ok(Value::Object(out))
}
