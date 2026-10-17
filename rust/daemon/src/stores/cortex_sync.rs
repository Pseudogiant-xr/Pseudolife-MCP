//! Per-slot write-through of the resident cortex: `sync_cortex_slots`
//! (`storage/sync.py:225`) with `_record_to_row` (`:170`), `_link_entity_ids`
//! (`:211`), `PostgresStorage._replace_slot_rows` (`storage/postgres.py:1835`,
//! columns `:122-138`), `meta_set` (`:2665`) and `advance_dream_cursor`
//! (`:2675`).

use std::collections::BTreeSet;

use anyhow::{Result, bail};
use serde_json::{Value, json};
use tokio_postgres::Client;

use super::cortex::{CortexRecord, CortexStore, SlotKey, norm_key};
use super::cortex_write::{SUPERSESSION_LOG_CAP, norm_value};

/// `_CORTEX_LOG_KEY`, `_CORTEX_CURSOR_KEY` (`storage/sync.py`).
const LOG_KEY: &str = "cortex_supersession_log";
const CURSOR_KEY: &str = "cortex_dream_cursor";

/// What the slot rewrite wrote, for the caller to clear its dirty marks.
#[derive(Debug, Default)]
pub struct Synced {
    /// The slots rewritten (cleared from `dirty_slots` on success).
    pub slots: BTreeSet<SlotKey>,
    /// `(record index, new facts.id)` for every row inserted.
    pub ids: Vec<(usize, i64)>,
}

/// One `facts` row (`_record_to_row`) before entity linking.
struct Row<'a> {
    index: usize,
    r: &'a CortexRecord,
    entity_id: Option<i64>,
    object_entity_id: Option<i64>,
}

fn sorted(v: &[String]) -> Value {
    json!(v.iter().collect::<BTreeSet<_>>())
}

async fn entity_id(db: &Client, name: &str) -> Result<Option<i64>> {
    let node = crate::storage::graph::find_entity_body(db, &crate::storage::graph::norm_name(name))
        .await?;
    Ok(node["id"].as_i64())
}

/// The first half of `sync_cortex_slots`: rewrite every dirty slot
/// (delete, then insert its records in record order, one transaction).
/// Nothing is cleared here: the caller clears what [`Synced`] names once
/// this returns, so a failed sync retries on the next save, as Python keeps
/// its dirty marks.
pub async fn sync_slot_rows(db: &Client, store: &CortexStore) -> Result<Synced> {
    let mut out = Synced::default();
    let slots = store.dirty_slots.clone();
    if slots.is_empty() {
        return Ok(out);
    }
    let mut rows: Vec<Row<'_>> = store
        .records
        .iter()
        .enumerate()
        .filter(|(_, r)| slots.contains(&r.key))
        .map(|(index, r)| Row {
            index,
            r,
            entity_id: None,
            object_entity_id: None,
        })
        .collect();
    // `_link_entity_ids`: point lookups outside the rewrite transaction.
    crate::txn::with_client(db, |db| {
        let rows = &mut rows;
        async move {
            for row in rows.iter_mut() {
                row.entity_id = entity_id(db, &row.r.entity).await?;
                if !row.r.value.is_empty() {
                    row.object_entity_id = entity_id(db, &row.r.value).await?;
                }
            }
            Ok::<_, anyhow::Error>(())
        }
    })
    .await?;
    out.ids = crate::txn::run(db, |db| {
        let (rows, slots) = (&rows, &slots);
        async move { replace_slot_rows(db, slots, rows).await }
    })
    .await?;
    out.slots = slots;
    Ok(out)
}

/// The second half of `sync_cortex_slots`: when the log or cursor changed,
/// `meta_set` the newest 200 log rows, then advance the cursor (a
/// transaction each). Returns whether it wrote.
pub async fn sync_meta(db: &Client, store: &CortexStore) -> Result<bool> {
    if !store.meta_dirty {
        return Ok(false);
    }
    let start = store
        .supersession_log
        .len()
        .saturating_sub(SUPERSESSION_LOG_CAP);
    let log = Value::Array(store.supersession_log[start..].to_vec());
    crate::txn::set_meta(db, LOG_KEY, &log).await?;
    advance_dream_cursor(db, store.dream_cursor).await?;
    Ok(true)
}

/// `_replace_slot_rows`: `DELETE ... WHERE (entity_norm, attribute_norm) IN
/// (sorted slots)`, then one INSERT per row (`_insert_canonical_rows`).
async fn replace_slot_rows(
    db: &Client,
    slots: &BTreeSet<SlotKey>,
    rows: &[Row<'_>],
) -> Result<Vec<(usize, i64)>, anyhow::Error> {
    let ents: Vec<&str> = slots.iter().map(|s| s.0.as_str()).collect();
    let attrs: Vec<&str> = slots.iter().map(|s| s.1.as_str()).collect();
    db.execute(
        "DELETE FROM facts WHERE (entity_norm, attribute_norm) IN \
         (SELECT * FROM unnest($1::text[], $2::text[]))",
        &[&ents, &attrs],
    )
    .await?;
    let mut ids = Vec::with_capacity(rows.len());
    for row in rows {
        let r = row.r;
        let origin = Some(r.origin()).filter(|o| !o.is_empty());
        let support = sorted(&r.support);
        let provenance = sorted(&r.provenance);
        let embedding: Option<Vec<f32>> = r.embedding.as_deref().map(<[f32]>::to_vec);
        let freshness = if r.freshness_class.is_empty() {
            "evergreen"
        } else {
            r.freshness_class.as_str()
        };
        let kind = if r.kind.is_empty() {
            "scalar"
        } else {
            r.kind.as_str()
        };
        let value_norm = (r.kind == "member").then(|| norm_value(&r.value));
        let version = if r.version == 0 { 1 } else { r.version };
        let (entity_norm, attribute_norm) = (norm_key(&r.entity), norm_key(&r.attribute));
        let id: i64 = db
            .query_one(
                "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, value, \
                 polarity, status, confidence, origin, support, provenance, asserted_at, \
                 last_confirmed, supersedes_value, superseded_by_value, superseded_at, \
                 embedding, entity_id, object_entity_id, freshness_class, kind, value_norm, \
                 stance, authority, distortion_tolerance, tx_time, valid_time, hlc_phys, \
                 hlc_logical, writer_id, session_id, version) VALUES ($1, $2, $3, $4, $5, $6, \
                 $7, $8::float8, $9, $10, $11, $12, $13, $14, $15, $16, $17::real[]::vector, \
                 $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30, $31, $32) \
                 RETURNING id",
                &[
                    &r.entity,
                    &r.attribute,
                    &entity_norm,
                    &attribute_norm,
                    &r.value,
                    &r.polarity,
                    &r.status,
                    &r.confidence,
                    &origin,
                    &support,
                    &provenance,
                    &r.asserted_at,
                    &r.last_confirmed,
                    &r.supersedes_value,
                    &r.superseded_by_value,
                    &r.superseded_at,
                    &embedding,
                    &row.entity_id,
                    &row.object_entity_id,
                    &freshness,
                    &kind,
                    &value_norm,
                    &r.stance,
                    &r.authority,
                    &r.distortion_tolerance,
                    &r.tx_time,
                    &r.valid_time,
                    &r.hlc_phys,
                    &r.hlc_logical,
                    &r.writer_id,
                    &r.session_id,
                    &version,
                ],
            )
            .await?
            .get(0);
        ids.push((row.index, id));
    }
    Ok(ids)
}

fn cursor_number(v: &Value) -> Result<f64> {
    let n = match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        Value::Bool(b) => Some(f64::from(u8::from(*b))),
        _ => None,
    };
    match n {
        Some(n) if n.is_finite() => Ok(n),
        _ => bail!("invalid_legacy_dream_cursor: stored cursor must be finite"),
    }
}

/// `advance_dream_cursor`: lock the stored cursor, keep the larger value.
async fn advance_dream_cursor(db: &Client, value: f64) -> Result<f64> {
    if !value.is_finite() {
        bail!("invalid_legacy_dream_cursor: cursor must be finite");
    }
    crate::txn::run(db, |db| async move {
        let row = db
            .query_opt(
                "SELECT value FROM meta WHERE key = 'cortex_dream_cursor' FOR UPDATE",
                &[],
            )
            .await?;
        let current = match row {
            None => 0.0,
            Some(row) => cursor_number(&row.get::<_, Value>(0))?,
        };
        let persisted = if value > current { value } else { current };
        db.execute(
            "INSERT INTO meta (key, value) VALUES ($1, $2) \
             ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            &[&CURSOR_KEY, &json!(persisted)],
        )
        .await?;
        Ok::<_, anyhow::Error>(persisted)
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stored_cursor_reads_like_python_float() {
        assert_eq!(cursor_number(&json!(2.5)).unwrap(), 2.5);
        assert_eq!(cursor_number(&json!(3)).unwrap(), 3.0);
        assert_eq!(cursor_number(&json!(" 1.5 ")).unwrap(), 1.5);
        assert_eq!(cursor_number(&json!(true)).unwrap(), 1.0);
        assert!(cursor_number(&json!(null)).is_err());
        assert!(cursor_number(&json!("inf")).is_err());
    }
}
