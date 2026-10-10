//! Read-only hydration of a schema-55 bank into a resident matrix (spec S7).

use crate::config::{MemoryConfig, StartupBandSpec};
use anyhow::Result;
use serde_json::Value;
use tokio_postgres::Client;

#[cfg(test)]
pub const DIM: usize = 1024;

pub struct Entry {
    pub id: i64,
    pub embedding: Vec<f32>,
    pub band: String,
    /// The band column as read, before seating.
    pub stored_band: String,
    pub text: String,
    pub surprise: f32,
    /// psycopg's text-format REAL value, before a reader's float32 arithmetic.
    pub surprise_value: f64,
    pub ts: f64,
    pub access_count: i32,
    pub reinforcements: i32,
    pub last_logical_turn: Option<i32>,
    pub dream_state: Option<String>,
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
}

pub struct Bank {
    pub dim: usize,
    pub entries: Vec<Entry>,
    /// Row-major, L2-normalized, `entries.len() * dim`.
    pub matrix: Vec<f32>,
}

impl Bank {
    #[allow(dead_code)] // The startup harness observes the storage-row shape.
    pub fn entries_dump(&self) -> Value {
        let mut entries: Vec<&Entry> = self.entries.iter().collect();
        entries.sort_by_key(|e| e.id);
        Value::Array(entries.iter().map(|e| serde_json::json!({
            "id": e.id, "band": e.band, "text": e.text,
            "embedding": e.embedding.iter().map(|v| f64::from(*v)).collect::<Vec<_>>(),
            "surprise": e.surprise_value, "ts": e.ts, "access_count": e.access_count,
            "source": e.source, "superseded_at": e.superseded_at,
            "superseded_by_text": e.superseded_by_text, "last_logical_turn": e.last_logical_turn,
            "episode_id": e.episode_id, "episode_title": e.episode_title, "tags": e.tags, "slots": e.slots,
            "authority": e.authority, "distortion_tolerance": e.distortion_tolerance,
            "dream_state": e.dream_state, "reinforcements": e.reinforcements
        })).collect())
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
/// rebalancing uses hydrate_config; this compatibility entry point seats names.
#[allow(dead_code)]
pub async fn hydrate(client: &Client, bands: &[String], dim: usize) -> Result<Bank> {
    let (bank, stale) = hydrate_inner(client, bands, None, dim).await?;
    refuse_stale_dims(stale)?;
    Ok(bank)
}

/// Startup checks these dimensions together with Cortex after both loads.
pub async fn hydrate_config(
    client: &Client,
    memory: &MemoryConfig,
    dim: usize,
) -> Result<(Bank, Vec<usize>)> {
    if crate::mutants::active("startup-skip-writer-guard") {
        return hydrate_locked(client, &memory.bands, Some(memory), dim).await;
    }
    hydrate_inner(client, &memory.bands, Some(memory), dim).await
}

async fn hydrate_inner(
    client: &Client,
    bands: &[String],
    memory: Option<&MemoryConfig>,
    dim: usize,
) -> Result<(Bank, Vec<usize>)> {
    crate::txn::with_client(client, |client| async move {
        hydrate_locked(client, bands, memory, dim).await
    })
    .await
}

// The caller owns the shared writer lock; every relocation uses this borrowed
// client directly so another statement cannot inherit its pending transaction.
async fn hydrate_locked(
    client: &Client,
    bands: &[String],
    memory: Option<&MemoryConfig>,
    dim: usize,
) -> Result<(Bank, Vec<usize>)> {
    let rows = client
        .query(
            "SELECT id, band, text, embedding::real[], surprise::text, ts, access_count, source, \
             superseded_at, superseded_by_text, episode_id, episode_title, tags, slots, \
             authority, distortion_tolerance, reinforcements, last_logical_turn, dream_state \
             FROM entries ORDER BY id",
            &[],
        )
        .await?;
    let mut entries = Vec::with_capacity(rows.len());
    let mut matrix = Vec::with_capacity(rows.len() * dim);
    let mut stale: Vec<usize> = Vec::new();
    for r in rows {
        // A NULL vector fails hydration (torch.as_tensor(None) in Python):
        // an ordinary error, recorded as not_ready.
        let Some(mut emb) = r.try_get::<_, Option<Vec<f32>>>(3)? else {
            anyhow::bail!("entry {} has no embedding", r.get::<_, i64>(0));
        };
        let original_embedding = emb.clone();
        if emb.len() == dim {
            normalize(&mut emb);
            matrix.extend_from_slice(&emb);
        } else {
            // Seated and stamped like any row, then refused after the
            // write-back (`_refuse_on_stale_hydrated_dims` runs after
            // `hydrate_cms`).
            stale.push(emb.len());
            matrix.extend(std::iter::repeat_n(0.0, dim));
        }
        let tags: Option<Value> = r.get(12);
        let slots: Option<Value> = r.get(13);
        let surprise_value = r
            .try_get::<_, Option<String>>(4)?
            .unwrap_or_else(|| "0".into())
            .parse::<f64>()?;
        entries.push(Entry {
            id: r.get(0),
            embedding: original_embedding,
            stored_band: r.get(1),
            // Hydration seats a row whose band left the preset in the first
            // band and reconciles its stamp (storage/sync.py:116-145). Python
            // also writes the new stamp back; the read-only spike does not.
            band: {
                let b: String = r.get(1);
                if memory.is_some() || bands.contains(&b) {
                    b
                } else {
                    bands[0].clone()
                }
            },
            text: r.get(2),
            surprise: surprise_value as f32,
            surprise_value,
            ts: r.get::<_, Option<f64>>(5).unwrap_or(0.0),
            access_count: r.get::<_, Option<i32>>(6).unwrap_or(0),
            reinforcements: r.try_get(16)?,
            last_logical_turn: r.try_get(17)?,
            dream_state: r.try_get(18)?,
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
        });
    }
    if let Some(memory) = memory {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_secs_f64();
        let (seats, moves) =
            seating_plan(&entries, &memory.band_specs, now, memory.retention_boost);
        for (index, depth) in moves {
            entries[index]
                .band
                .clone_from(&memory.band_specs[depth].name);
            if crate::mutants::active("startup-skip-stamp") {
                continue;
            }
            if let Err(error) = client
                .execute(
                    "UPDATE entries SET band = $1 WHERE id = $2",
                    &[&entries[index].band, &entries[index].id],
                )
                .await
            {
                eprintln!("rebalance write-through failed: {error}");
            }
        }
        let old_matrix = matrix;
        matrix = Vec::with_capacity(old_matrix.len());
        let mut old_entries: Vec<Option<Entry>> = entries.into_iter().map(Some).collect();
        entries = Vec::with_capacity(old_entries.len());
        for (depth, indices) in seats.iter().enumerate() {
            for &index in indices {
                let mut entry = old_entries[index]
                    .take()
                    .expect("startup seating preserves unique entries");
                // hydrate_cms reconciles only stamps not already changed by
                // rebalance; a failed cosmetic hop is not silently retried.
                if entry.band != memory.band_specs[depth].name {
                    entry.band.clone_from(&memory.band_specs[depth].name);
                    if !crate::mutants::active("startup-skip-stamp")
                        && let Err(error) = client
                            .execute(
                                "UPDATE entries SET band = $1 WHERE id = $2",
                                &[&entry.band, &entry.id],
                            )
                            .await
                    {
                        eprintln!(
                            "band-stamp write-through failed for entry {}: {error}",
                            entry.id
                        );
                    }
                }
                entries.push(entry);
                matrix.extend_from_slice(&old_matrix[index * dim..(index + 1) * dim]);
            }
        }
    }
    for e in entries.iter().filter(|_| memory.is_none()) {
        if e.stored_band != e.band {
            // Python wraps the one UPDATE in BEGIN/COMMIT; a single
            // autocommit statement is the same atomic write, and leaves no
            // transaction open on the shared session if this future is dropped.
            if let Err(err) = client
                .execute(
                    "UPDATE entries SET band = $1 WHERE id = $2",
                    &[&e.band, &e.id],
                )
                .await
            {
                eprintln!("band-stamp write-through failed for entry {}: {err}", e.id);
            }
        }
    }
    Ok((
        Bank {
            dim,
            entries,
            matrix,
        },
        stale,
    ))
}

pub fn refuse_stale_dims(mut stale: Vec<usize>) -> Result<()> {
    if !stale.is_empty() {
        stale.sort_unstable();
        let count = stale.len();
        stale.dedup();
        return Err(anyhow::Error::new(StaleDims { count, dims: stale }));
    }
    Ok(())
}

/// Hydrated rows whose vectors do not fit the embedder: the caller records
/// `init_refusal` (`service.py:1142-1210`).
#[derive(Debug)]
pub struct StaleDims {
    pub count: usize,
    pub dims: Vec<usize>,
}

impl std::fmt::Display for StaleDims {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{} hydrated row(s) embedded at the wrong dimension",
            self.count
        )
    }
}

impl std::error::Error for StaleDims {}

fn retention_score(entry: &Entry, policy: &str, now: f64, boost: f64) -> f64 {
    let age = (now - entry.ts).max(1.0);
    let base = match policy {
        "balanced" => f64::from(entry.access_count) / age + entry.surprise_value * 0.1,
        "surprise_heavy" => f64::from(entry.access_count) / age + entry.surprise_value,
        "recency_heavy" => f64::from(entry.access_count) * (1.0 + 2.0_f64.powf(-age / 3600.0)),
        _ => unreachable!("config validates named retention policies"),
    };
    let weight = match entry.source.as_str() {
        "user_msg" | "user" => 1.5,
        "system" => 10.0,
        "tool_call" => 0.5,
        "llm_thinking" => 0.2,
        _ => 1.0,
    };
    let score = (base + 1.0) * weight + boost * f64::from(entry.reinforcements).ln_1p();
    if entry.superseded_at.is_some() {
        score * 0.05
    } else {
        score
    }
}

#[cfg(test)]
fn startup_seating(
    entries: &[Entry],
    bands: &[StartupBandSpec],
    now: f64,
    boost: f64,
) -> Vec<Vec<usize>> {
    seating_plan(entries, bands, now, boost).0
}

fn seating_plan(
    entries: &[Entry],
    bands: &[StartupBandSpec],
    now: f64,
    boost: f64,
) -> (Vec<Vec<usize>>, Vec<(usize, usize)>) {
    let mut seats = vec![Vec::new(); bands.len()];
    let mut moves = Vec::new();
    for (i, entry) in entries.iter().enumerate() {
        let band = bands
            .iter()
            .rposition(|b| b.name == entry.stored_band)
            .unwrap_or(0);
        seats[band].push(i);
    }
    if crate::mutants::active("startup-skip-seating") {
        return (seats, moves);
    }
    for depth in 0..bands.len().saturating_sub(1) {
        let overflow = seats[depth].len().saturating_sub(bands[depth].max_entries);
        if overflow == 0 {
            continue;
        }
        let mut spill = seats[depth].clone();
        // Stable ties retain the source band/insertion order, as nsmallest.
        spill.sort_by(|&a, &b| {
            retention_score(&entries[a], &bands[depth].retention_policy, now, boost).total_cmp(
                &retention_score(&entries[b], &bands[depth].retention_policy, now, boost),
            )
        });
        spill.truncate(overflow);
        let moved: std::collections::HashSet<usize> = spill.iter().copied().collect();
        seats[depth].retain(|index| !moved.contains(index));
        moves.extend(spill.iter().map(|&index| (index, depth + 1)));
        seats[depth + 1].extend(spill);
    }
    (seats, moves)
}

#[cfg(test)]
mod startup_tests {
    use super::*;

    fn entry(id: i64, surprise: f32) -> Entry {
        Entry {
            id,
            embedding: vec![0.0; DIM],
            band: "old".into(),
            stored_band: "old".into(),
            text: format!("entry {id}"),
            surprise,
            surprise_value: f64::from(surprise),
            ts: 0.0,
            access_count: 0,
            reinforcements: 0,
            last_logical_turn: None,
            dream_state: None,
            source: "test".into(),
            superseded_at: None,
            superseded_by_text: None,
            episode_id: None,
            episode_title: None,
            tags: vec![],
            slots: vec![],
            authority: None,
            distortion_tolerance: None,
        }
    }

    #[test]
    fn restored_unknown_band_spills_without_losing_deep_overflow() {
        let entries = vec![entry(1, 0.1), entry(2, 0.2), entry(3, 0.3), entry(4, 0.4)];
        let bands = vec![
            StartupBandSpec {
                name: "hot".into(),
                max_entries: 1,
                retention_policy: "balanced".into(),
            },
            StartupBandSpec {
                name: "deep".into(),
                max_entries: 1,
                retention_policy: "balanced".into(),
            },
        ];
        let seats = startup_seating(&entries, &bands, 100.0, 0.0);
        assert_eq!(seats[0], vec![3]);
        assert_eq!(seats[1], vec![0, 1, 2]);
        assert_eq!(seats.iter().map(Vec::len).sum::<usize>(), entries.len());
    }

    #[tokio::test]
    async fn db_seating_recovers_an_abandoned_writer_transaction() {
        let Ok(dsn) = std::env::var("PL_PGS_WRITER_GUARD_DSN") else {
            return;
        };
        let parsed = crate::storage::parse_dsn(&dsn).unwrap();
        assert!(
            parsed
                .get_dbname()
                .is_some_and(|name| name.starts_with("pl_cf_pgs_"))
        );
        let storage = std::sync::Arc::new(crate::storage::Storage::open(&dsn).await.unwrap());
        let actual =
            crate::storage::schema::simple_value(storage.client(), "SELECT current_database()")
                .await
                .unwrap()
                .unwrap()
                .unwrap();
        assert!(actual.starts_with("pl_cf_pgs_"));
        let vector = format!("[1{}]", ",0".repeat(DIM - 1));
        storage.client().execute(
                "INSERT INTO entries (band, text, embedding, ts) VALUES ('retired', 'writer guard fixture', $1::text::vector, 1000.25)",
            &[&vector],
        ).await.unwrap();
        let (begun, received) = tokio::sync::oneshot::channel();
        let writer = storage.clone();
        let task = tokio::spawn(async move {
            crate::txn::run(writer.client(), |client| async move {
                client.execute(
                    "INSERT INTO meta (key,value) VALUES ('pending_writer_guard', 'true'::jsonb)", &[],
                ).await?;
                begun.send(()).unwrap();
                std::future::pending::<Result<(), tokio_postgres::Error>>().await
            }).await
        });
        received.await.unwrap();
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        let memory = MemoryConfig::default();
        let (bank, stale) = hydrate_config(storage.client(), &memory, DIM)
            .await
            .unwrap();
        assert!(stale.is_empty());
        assert_eq!(bank.entries[0].band, "flat");
        let (observer, connection) = parsed.connect(tokio_postgres::NoTls).await.unwrap();
        let observer_task = tokio::spawn(connection);
        let band: String = observer
            .query_one("SELECT band FROM entries", &[])
            .await
            .unwrap()
            .get(0);
        let marker: i64 = storage
            .client()
            .query_one(
                "SELECT count(*) FROM meta WHERE key='pending_writer_guard'",
                &[],
            )
            .await
            .unwrap()
            .get(0);
        drop(observer);
        observer_task.await.unwrap().unwrap();
        std::sync::Arc::try_unwrap(storage)
            .ok()
            .expect("fixture owns writer")
            .close()
            .await;
        assert_eq!(
            (band.as_str(), marker),
            ("flat", 0),
            "seating inherited the abandoned transaction"
        );
    }
}
