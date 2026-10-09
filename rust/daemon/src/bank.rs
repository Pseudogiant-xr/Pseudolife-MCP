//! The resident bank: hydration of a schema-55 bank into a resident matrix
//! (spec S7), extended in place by the write path (spec W1.9-W1.10).
//!
//! Mutation paths (CLAUDE.md "Derived state"): `hydrate` builds the bank;
//! `push` appends one stored entry and its row of the matrix; `remove`
//! drops one evicted entry and its row. Nothing else edits `entries` or
//! `matrix`, so the two stay aligned row for row and in `entries` order,
//! which is id order (the tie order search relies on).

use anyhow::{Result, bail};
use serde_json::Value;
use std::sync::OnceLock;
use std::sync::atomic::AtomicI64;
use tokio_postgres::Client;

pub const DIM: usize = 1024;

#[derive(Default)]
pub struct Entry {
    pub id: i64,
    pub band: String,
    /// The band column as read, before seating.
    pub stored_band: String,
    pub text: String,
    /// A double in memory, as Python keeps a fresh entry's score; the
    /// column is float4, so a hydrated value is that float widened.
    pub surprise: f64,
    pub ts: f64,
    /// Bumped under a read guard by search (`cms.py:1752`).
    pub access_count: AtomicI64,
    pub source: String,
    pub superseded_at: Option<f64>,
    pub superseded_by_text: Option<String>,
    pub episode_id: Option<String>,
    pub episode_title: Option<String>,
    pub tags: Vec<String>,
    /// `[entity, attribute, value, polarity]` rows as stored.
    pub slots: Vec<Value>,
    pub authority: Option<String>,
    pub distortion_tolerance: Option<String>,
    /// `entries.reinforcements` (eviction's retention term).
    #[cfg_attr(not(feature = "harness"), allow(dead_code))]
    pub reinforcements: i32,
    /// `MemoryEntry.seq`: process-wide store order, never persisted; the
    /// tie-break of contiguity's stream order (W2-D).
    #[allow(dead_code)]
    pub seq: u64,
    /// `MemoryEntry.cue_flags`: the contradiction scan's (gain, loss) memo.
    #[cfg_attr(not(feature = "harness"), allow(dead_code))]
    pub cues: OnceLock<(bool, bool)>,
}

pub struct Bank {
    pub entries: Vec<Entry>,
    /// Row-major, L2-normalized, `entries.len() * DIM`.
    pub matrix: Vec<f32>,
}

#[cfg_attr(not(feature = "harness"), allow(dead_code))]
impl Bank {
    /// Append one entry with its (already L2-normalized) row.
    pub fn push(&mut self, entry: Entry, row: &[f32]) {
        debug_assert_eq!(row.len(), DIM);
        self.entries.push(entry);
        self.matrix.extend_from_slice(row);
    }

    /// Remove the entry at `idx` and its row, keeping order.
    pub fn remove(&mut self, idx: usize) -> Entry {
        self.matrix.drain(idx * DIM..(idx + 1) * DIM);
        self.entries.remove(idx)
    }

    pub fn row(&self, idx: usize) -> &[f32] {
        &self.matrix[idx * DIM..(idx + 1) * DIM]
    }
}

fn normalize(v: &mut [f32]) {
    // torch F.normalize(p=2, eps=1e-12)
    let n = v.iter().map(|x| x * x).sum::<f32>().sqrt().max(1e-12);
    v.iter_mut().for_each(|x| *x /= n);
}

/// `hydrate_cms` (`storage/sync.py:107-155`): every row in id order, seated
/// in its band, or in the first band when the preset no longer has it; a
/// stale stamp is then written back (`update_entry(id, band=...)`, one
/// transaction each; a failed write is logged, never fatal). Capacity
/// rebalancing on hydration is not ported (declared divergence).
pub async fn hydrate(client: &Client, bands: &[String]) -> Result<Bank> {
    let rows = client
        .query(
            "SELECT id, band, text, embedding::real[], surprise, ts, access_count, source, \
             superseded_at, superseded_by_text, episode_id, episode_title, tags, slots, \
             authority, distortion_tolerance, reinforcements FROM entries ORDER BY id",
            &[],
        )
        .await?;
    let mut entries = Vec::with_capacity(rows.len());
    let mut matrix = Vec::with_capacity(rows.len() * DIM);
    for r in rows {
        let mut emb: Vec<f32> = r.get(3);
        if emb.len() != DIM {
            bail!(
                "entry {} has a {}-dim vector, expected {DIM}",
                r.get::<_, i64>(0),
                emb.len()
            );
        }
        normalize(&mut emb);
        matrix.extend_from_slice(&emb);
        let tags: Option<Value> = r.get(12);
        let slots: Option<Value> = r.get(13);
        entries.push(Entry {
            id: r.get(0),
            stored_band: r.get(1),
            // Hydration seats a row whose band left the preset in the first
            // band and reconciles its stamp (storage/sync.py:116-145). Python
            // also writes the new stamp back; the read-only spike does not.
            band: {
                let b: String = r.get(1);
                if bands.contains(&b) {
                    b
                } else {
                    bands[0].clone()
                }
            },
            text: r.get(2),
            surprise: r.get::<_, Option<f32>>(4).unwrap_or(0.0).into(),
            ts: r.get::<_, Option<f64>>(5).unwrap_or(0.0),
            access_count: AtomicI64::new(r.get::<_, Option<i32>>(6).unwrap_or(0).into()),
            source: r.get::<_, Option<String>>(7).unwrap_or_default(),
            superseded_at: r.get(8),
            superseded_by_text: r.get(9),
            episode_id: r.get(10),
            episode_title: r.get(11),
            tags: match tags {
                Some(Value::Array(a)) => a
                    .into_iter()
                    .filter_map(|t| t.as_str().map(String::from))
                    .collect(),
                _ => Vec::new(),
            },
            slots: match slots {
                Some(Value::Array(a)) => a,
                _ => Vec::new(),
            },
            authority: r.get(14),
            distortion_tolerance: r.get(15),
            reinforcements: r.get::<_, Option<i32>>(16).unwrap_or(0),
            seq: crate::episodes::next_seq(),
            cues: OnceLock::new(),
        });
    }
    for e in &entries {
        if e.stored_band != e.band {
            let write = async {
                client.batch_execute("BEGIN").await?;
                client
                    .execute(
                        "UPDATE entries SET band = $1 WHERE id = $2",
                        &[&e.band, &e.id],
                    )
                    .await?;
                client.batch_execute("COMMIT").await
            };
            if let Err(err) = write.await {
                let _ = client.batch_execute("ROLLBACK").await;
                eprintln!("band-stamp write-through failed for entry {}: {err}", e.id);
            }
        }
    }
    Ok(Bank { entries, matrix })
}

pub fn normalize_query(v: &mut [f32]) {
    normalize(v)
}
