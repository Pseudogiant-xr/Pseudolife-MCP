//! Durable startup inputs shared with the read/write slices.
//! Required loads publish together; a pending clock reseed blocks service.

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::sync::RwLock;
use tokio_postgres::Client;

pub struct StartupState {
    pub episodes: Vec<Value>,
    pub current_episode: Option<String>,
    pub cortex: Vec<Value>,
    pub world: Vec<Value>,
    pub lessons: Vec<Value>,
    pub metadata: Value,
    clock: RwLock<StartupClock>,
}

#[derive(Clone, Copy)]
pub struct StartupClock {
    pub hlc_highwater: (i64, i64),
    pub hlc_reseed_pending: bool,
}

impl StartupState {
    /// Read the input stamp and its readiness flag together before ticking.
    pub fn clock_state(&self) -> StartupClock {
        *self.clock.read().expect("startup clock lock")
    }

    /// Late clock failures retain every loaded store and retry only this read.
    pub async fn reseed(&self, client: &Client) -> Result<()> {
        self.clock
            .write()
            .expect("startup clock lock")
            .hlc_reseed_pending = true;
        let row = crate::txn::with_client(client, |client| {
            client.query_opt(
                "SELECT value FROM meta WHERE key = 'coordination_hlc_highwater'",
                &[],
            )
        })
        .await?;
        let durable: Value = row.map(|row| row.get(0)).unwrap_or(Value::Null);
        let hlc_highwater = highwater(&[&self.cortex, &self.world, &self.lessons], &durable)?;
        *self.clock.write().expect("startup clock lock") = StartupClock {
            hlc_highwater,
            hlc_reseed_pending: false,
        };
        Ok(())
    }

    #[allow(dead_code)] // Read/write slices adopt this snapshot at their seam.
    pub fn dump(&self) -> Value {
        let clock = self.clock_state();
        json!({"episodes": self.episodes, "current_episode": self.current_episode,
               "cortex": self.cortex, "world": self.world, "lessons": self.lessons,
               "metadata": self.metadata,
               "hlc_highwater": [clock.hlc_highwater.0, clock.hlc_highwater.1],
               "hlc_reseed_pending": clock.hlc_reseed_pending})
    }
}

fn highwater(records: &[&[Value]], durable: &Value) -> Result<(i64, i64)> {
    if crate::mutants::active("startup-hlc-zero") {
        return Ok((0, 0));
    }
    let mut best = (0, 0);
    for rows in records {
        for row in *rows {
            let physical = if row["hlc_phys"].is_null() {
                0
            } else {
                row["hlc_phys"]
                    .as_i64()
                    .context("invalid record clock physical stamp")?
            };
            if physical != 0 {
                let logical = if row["hlc_logical"].is_null() {
                    0
                } else {
                    row["hlc_logical"]
                        .as_i64()
                        .context("invalid record clock logical stamp")?
                };
                best = best.max((physical, logical));
            }
        }
    }
    if !durable.is_null() {
        let parts = durable
            .as_array()
            .filter(|v| v.len() == 2)
            .context("invalid coordination clock high-water mark")?;
        let physical = parts[0]
            .as_i64()
            .filter(|v| *v >= 0)
            .context("invalid coordination clock high-water mark")?;
        let logical = parts[1]
            .as_i64()
            .filter(|v| *v >= 0)
            .context("invalid coordination clock high-water mark")?;
        best = best.max((physical, logical));
    }
    Ok(best)
}

fn number_or_zero(value: &Value) -> Result<f64> {
    if value.is_null() || value == &Value::Bool(false) {
        return Ok(0.0);
    }
    value
        .as_f64()
        .filter(|n| n.is_finite())
        .context("invalid startup timestamp or cursor")
}

fn identity_metadata(raw: &Value) -> Result<Value> {
    let pointer = &raw["active_session_pointer"];
    let active = match pointer
        .get("session_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        Some(session) => json!([session, number_or_zero(&pointer["ts"])?]),
        None => Value::Null,
    };
    let mut tombstones = serde_json::Map::new();
    if let Some(values) = raw["episode_tombstones"]
        .as_object()
        .filter(|_| !crate::mutants::active("startup-drop-tombstones"))
    {
        for (id, value) in values {
            if let Some(key) = value
                .get("session_key")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                tombstones.insert(
                    id.clone(),
                    json!([
                        key,
                        number_or_zero(&value["ended_at"])?,
                        value["title"].as_str().unwrap_or("")
                    ]),
                );
            }
        }
    }
    let mut deferred = serde_json::Map::new();
    if let Some(values) = raw["deferred_empty_roots"].as_object() {
        for (id, value) in values {
            deferred.insert(id.clone(), Value::from(number_or_zero(value)?));
        }
    }
    let log = match &raw["cortex_supersession_log"] {
        Value::Null | Value::Bool(false) => json!([]),
        Value::Array(rows) => Value::Array(rows.clone()),
        _ => bail!("invalid cortex supersession log"),
    };
    Ok(
        json!({"active_session": active, "episode_tombstones": tombstones,
              "deferred_empty_roots": deferred, "cortex_supersession_log": log,
              "cortex_dream_cursor": number_or_zero(&raw["cortex_dream_cursor"])?}),
    )
}

/// The allowlist deliberately excludes secrets and unrelated subsystem meta.
pub async fn metadata(client: &Client) -> Result<Value> {
    let rows = crate::txn::with_client(client, |client| {
        client.query(
            "SELECT key, value FROM meta WHERE key IN \
        ('active_session_pointer', 'episode_tombstones', 'deferred_empty_roots', \
         'cortex_supersession_log', 'cortex_dream_cursor')",
            &[],
        )
    })
    .await?;
    let mut values = serde_json::Map::new();
    for row in rows {
        values.insert(row.try_get(0)?, row.try_get(1)?);
    }
    Ok(Value::Object(values))
}

async fn canonical_rows(client: &Client, table: &str) -> Result<Vec<Value>> {
    // table is selected by this module, never a caller's SQL identifier.
    let columns = match table {
        "facts" => {
            "id, entity, attribute, entity_norm, attribute_norm, value, polarity, status, confidence, \
            origin, support, provenance, asserted_at, last_confirmed, supersedes_value, superseded_by_value, \
            superseded_at, embedding, entity_id, object_entity_id, freshness_class, kind, value_norm, stance, \
            authority, distortion_tolerance, tx_time, valid_time, hlc_phys, hlc_logical, writer_id, session_id, version"
        }
        "world_facts" => {
            "id, entity, attribute, entity_norm, attribute_norm, value, polarity, status, confidence, \
            origin, support, provenance, asserted_at, last_confirmed, supersedes_value, superseded_by_value, \
            superseded_at, embedding, source_url, source_quote, retrieved_at, freshness_class, content_hash, \
            source_doc_id, tx_time, valid_time, hlc_phys, hlc_logical, writer_id, session_id, version"
        }
        "lessons" => {
            "id, entity, attribute, entity_norm, attribute_norm, value, about, polarity, outcome, status, \
            confidence, origin, support, provenance, asserted_at, last_confirmed, supersedes_value, \
            superseded_by_value, superseded_at, embedding, entity_id, object_entity_id, tx_time, valid_time, \
            hlc_phys, hlc_logical, writer_id, session_id, version"
        }
        _ => unreachable!("startup table is fixed by the caller"),
    };
    let sql = format!(
        "SELECT to_jsonb(t) - 'embedding', embedding::real[], confidence::text \
                       FROM (SELECT {columns} FROM {table}) t ORDER BY id"
    );
    let mut values = Vec::new();
    let rows = crate::txn::with_client(client, |client| client.query(&sql, &[])).await?;
    for row in rows {
        let mut data: Value = row.try_get(0)?;
        let embedding: Option<Vec<f32>> = row.try_get(1)?;
        let confidence: String = row.try_get(2)?;
        // psycopg's text-format REAL decoder parses the server's shortest
        // decimal as a Python double; binary float32 promotion differs.
        data["confidence"] = Value::from(confidence.parse::<f64>()?);
        // PostgreSQL's JSON conversion can spell integral doubles as integers;
        // preserve the loader's Python float type for these SQL columns.
        for field in [
            "asserted_at",
            "last_confirmed",
            "superseded_at",
            "tx_time",
            "valid_time",
            "retrieved_at",
        ] {
            if let Some(value) = data.get(field).filter(|v| !v.is_null()) {
                let number = value.as_f64().context("invalid canonical timestamp")?;
                data[field] = Value::from(number);
            }
        }
        data["embedding"] = match embedding {
            Some(vector) => Value::Array(
                vector
                    .into_iter()
                    .map(|v| Value::from(f64::from(v)))
                    .collect(),
            ),
            None => Value::Null,
        };
        if data["version"].is_null() || data["version"] == 0 {
            data["version"] = Value::from(1);
        }
        for field in ["support", "provenance"] {
            if data[field].is_null() {
                data[field] = json!([]);
            }
            if !data[field].is_array() {
                bail!("invalid {table}.{field}");
            }
        }
        values.push(data);
    }
    Ok(values)
}

/// Required stores load into locals. Any error drops the entire candidate;
/// callers attach the loaded snapshot to Ready before its late clock reseed.
pub async fn hydrate(
    client: &Client,
    raw_metadata: Value,
    mut stale_dims: Vec<usize>,
    dim: usize,
) -> Result<StartupState> {
    let rows = crate::txn::with_client(client, |client| {
        client.query(
            "SELECT to_jsonb(e) FROM (SELECT id, title, hint, started_at, ended_at, \
                closed_by_new_start, session_key, parent_id FROM episodes) e ORDER BY started_at",
            &[],
        )
    })
    .await
    .context("entry hydration failed")?;
    let mut episodes: Vec<Value> = rows
        .into_iter()
        .map(|r| r.try_get(0))
        .collect::<Result<_, _>>()?;
    for episode in &mut episodes {
        for field in ["started_at", "ended_at"] {
            if !episode[field].is_null() {
                episode[field] = Value::from(
                    episode[field]
                        .as_f64()
                        .context("invalid episode timestamp")?,
                );
            }
        }
    }
    let current_episode = episodes
        .iter()
        .rev()
        .find(|e| e["ended_at"].is_null())
        .and_then(|e| e["id"].as_str())
        .map(String::from);
    let cortex = canonical_rows(client, "facts")
        .await
        .context("cortex hydration failed")?;
    // service.py checks entries and Cortex here, before world/lesson loads.
    // NULL Cortex vectors are permitted; every non-null vector must fit.
    if !crate::mutants::active("startup-skip-cortex-dims") {
        stale_dims.extend(cortex.iter().filter_map(|row| {
            row["embedding"]
                .as_array()
                .map(Vec::len)
                .filter(|length| *length != dim)
        }));
    }
    crate::bank::refuse_stale_dims(stale_dims)?;
    let world = if crate::mutants::active("startup-skip-world") {
        vec![]
    } else {
        canonical_rows(client, "world_facts")
            .await
            .context("world cortex hydration failed")?
    };
    let lessons = canonical_rows(client, "lessons")
        .await
        .context("lesson hydration failed")?;
    let metadata = identity_metadata(&raw_metadata)?;
    Ok(StartupState {
        episodes,
        current_episode,
        cortex,
        world,
        lessons,
        metadata,
        clock: RwLock::new(StartupClock {
            hlc_highwater: (0, 0),
            hlc_reseed_pending: true,
        }),
    })
}

pub async fn candidate(
    client: &Client,
    memory: &crate::config::MemoryConfig,
    metadata: Value,
    dim: usize,
) -> Result<(crate::bank::Bank, StartupState)> {
    let (bank, stale_dims) = crate::bank::hydrate_config(client, memory, dim)
        .await
        .context("entry hydration failed")?;
    let startup = hydrate(client, metadata, stale_dims, dim).await?;
    Ok((bank, startup))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn highwater_includes_every_store_and_the_durable_coordination_clock() {
        let cortex = vec![json!({"hlc_phys": 100, "hlc_logical": 2})];
        let world = vec![json!({"hlc_phys": 101, "hlc_logical": 0})];
        let lessons = vec![json!({"hlc_phys": 101, "hlc_logical": 3})];
        assert_eq!(
            highwater(&[&cortex, &world, &lessons], &json!([102, 0])).unwrap(),
            (102, 0)
        );
        assert_eq!(
            highwater(&[&cortex, &world, &lessons], &Value::Null).unwrap(),
            (101, 3)
        );
    }

    #[test]
    fn malformed_durable_highwater_never_becomes_ready() {
        for value in [
            json!(true),
            json!([1]),
            json!([1, 2, 3]),
            json!([-1, 0]),
            json!([1, true]),
            json!([1.5, 0]),
            json!(["1", 0]),
        ] {
            assert!(highwater(&[], &value).is_err(), "{value}");
        }
    }

    #[test]
    fn identity_defaults_and_tombstone_filter_match_startup() {
        let raw = json!({"active_session_pointer": {"session_id": "s", "ts": null},
            "episode_tombstones": {"e": {"session_key": "s", "ended_at": 4.5, "title": "saved"},
                                   "ignored": {"title": "no key"}},
            "deferred_empty_roots": {"root": null}, "cortex_dream_cursor": 7.5});
        let metadata = identity_metadata(&raw).unwrap();
        assert_eq!(metadata["active_session"], json!(["s", 0.0]));
        assert_eq!(
            metadata["episode_tombstones"],
            json!({"e": ["s", 4.5, "saved"]})
        );
        assert_eq!(metadata["deferred_empty_roots"], json!({"root": 0.0}));
        assert_eq!(metadata["cortex_supersession_log"], json!([]));
    }

    #[tokio::test]
    async fn db_startup_inputs_dump() {
        let Ok(dsn) = std::env::var("PL_PGS_STARTUP_DSN") else {
            return;
        };
        let config = crate::config::load(&std::path::PathBuf::from(
            std::env::var("PL_PGS_CONFIG").unwrap(),
        ))
        .unwrap();
        let parsed = crate::storage::parse_dsn(&dsn).unwrap();
        assert!(
            parsed
                .get_dbname()
                .is_some_and(|name| name.starts_with("pl_cf_pgs_"))
        );
        let storage = crate::storage::Storage::open(&dsn).await.unwrap();
        let output = match async {
            let raw = metadata(storage.client()).await?;
            candidate(storage.client(), &config.memory, raw, crate::bank::DIM).await
        }
        .await
        {
            Ok((bank, startup)) => match startup.reseed(storage.client()).await {
                Ok(()) => {
                    json!({"ok": true, "startup": startup.dump(), "entries": bank.entries_dump()})
                }
                Err(error) => json!({"ok": false, "error": format!("{error:#}"),
                        "retained": {"startup": startup.dump(), "entries": bank.entries_dump()}}),
            },
            Err(error) => {
                let mut output = json!({"ok": false, "error": format!("{error:#}")});
                if let Some(stale) = error.downcast_ref::<crate::bank::StaleDims>() {
                    output["stale_dims"] = json!({"count": stale.count, "dims": stale.dims});
                }
                output
            }
        };
        let path = std::env::var("PL_PGS_STARTUP_OUT").unwrap();
        std::fs::write(path, serde_json::to_vec(&output).unwrap()).unwrap();
        storage.close().await;
    }
}
