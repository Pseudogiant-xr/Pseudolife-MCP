//! `ensure_schema` (spec P3, P6): `pseudolife_memory/storage/schema.py`
//! lines 1069-1664 at master 35b8f5d2, transcribed statement for statement.
//!
//! Every statement below runs in the same order as the Python, inside the one
//! explicit transaction the caller opens. Order matters beyond correctness:
//! each `ADD COLUMN` appends a column, so a reordered statement changes the
//! ordinal positions the database-state harness compares. Statements without
//! parameters go over the simple query protocol (psycopg sends a
//! parameterless `execute` with `PQsendQuery`); the two parameterized ones
//! (the healing updates and the `schema_version` upsert) use the extended
//! protocol, as psycopg does for any statement with bind parameters.

use tokio_postgres::Client;
use tokio_postgres::types::Type;

/// `SCHEMA_META_VERSION` (schema.py:20).
pub const SCHEMA_META_VERSION: i64 = 55;

/// `_EXPECTED_EMBEDDING_DIM` (schema.py:1069).
pub const EXPECTED_EMBEDDING_DIM: i32 = 1024;

/// The generated copy of `SCHEMA_SQL`, with the one header line
/// `gen_schema_sql.py` prepends (`-- schema_version NN\n`).
const SCHEMA_SQL_FILE: &str = include_str!("schema.sql");

/// `SCHEMA_SQL` byte for byte: the generated file minus its header line, so
/// the batch the server receives is exactly the Python constant.
pub fn schema_sql() -> &'static str {
    match SCHEMA_SQL_FILE.strip_prefix("-- schema_version ") {
        Some(rest) => rest.split_once('\n').map_or("", |(_, body)| body),
        None => SCHEMA_SQL_FILE,
    }
}

/// The schema version the generated copy was written for (its header line).
#[cfg_attr(not(test), allow(dead_code))]
pub fn schema_sql_version() -> Option<i64> {
    SCHEMA_SQL_FILE
        .strip_prefix("-- schema_version ")?
        .split_once('\n')?
        .0
        .trim()
        .parse()
        .ok()
}

/// The `RuntimeError` text of `_refuse_on_embedding_dim_mismatch`
/// (schema.py:1131-1145), exactly.
pub fn dim_mismatch_message(live_dim: i32) -> String {
    let want = EXPECTED_EMBEDDING_DIM;
    format!(
        "Refusing to start: entries.embedding is vector({live_dim}) \
         but this build's schema expects vector({want}). \
         ensure_schema is additive-only and will NOT alter vector \
         dimensions in place -- that would either half-migrate four \
         tables at startup or write {want}-d vectors \
         into a {live_dim}-d column, corrupting the bank. Run the \
         human-gated migration first: `python ops/migrate_embeddings.py` \
         (backs up, requires the daemon stopped, re-embeds every row \
         through the real embedder, and moves all four embedding \
         columns from vector({live_dim}) to vector({want})). \
         Never run the daemon against a bank you have not migrated."
    )
}

pub enum SchemaError {
    /// The embedding-dimension refusal (a Python `RuntimeError`).
    Refused(String),
    Db(tokio_postgres::Error),
}

impl From<tokio_postgres::Error> for SchemaError {
    fn from(e: tokio_postgres::Error) -> Self {
        SchemaError::Db(e)
    }
}

/// The first column of the first row a simple query returned, as text
/// (`None` for SQL NULL or no row).
pub(crate) async fn simple_value(
    client: &Client,
    sql: &str,
) -> Result<Option<Option<String>>, tokio_postgres::Error> {
    for msg in client.simple_query(sql).await? {
        if let tokio_postgres::SimpleQueryMessage::Row(row) = msg {
            return Ok(Some(row.get(0).map(str::to_owned)));
        }
    }
    Ok(None)
}

/// `_refuse_on_embedding_dim_mismatch` (schema.py:1105-1145).
async fn refuse_on_embedding_dim_mismatch(client: &Client) -> Result<(), SchemaError> {
    let row = simple_value(
        client,
        "SELECT atttypmod FROM pg_attribute \
         WHERE attrelid = to_regclass('public.entries') \
         AND attname = 'embedding' AND attnum > 0 AND NOT attisdropped",
    )
    .await?;
    let Some(value) = row else {
        return Ok(()); // fresh install: entries doesn't exist yet
    };
    // atttypmod is int4 NOT NULL; an unparseable value cannot occur.
    let live_dim: i32 = value.as_deref().unwrap_or("-1").parse().unwrap_or(-1);
    if live_dim > 0 && live_dim != EXPECTED_EMBEDDING_DIM {
        return Err(SchemaError::Refused(dim_mismatch_message(live_dim)));
    }
    Ok(())
}

/// `_backfill_trace_invalidations` (schema.py:1072-1088).
const BACKFILL_TRACE_INVALIDATIONS: &str = "
        INSERT INTO memory_trace_invalidations
          (entity_norm, attribute_norm, source_entry_id,
           invalidated_at, cause)
        SELECT t.entity_norm, t.attribute_norm, t.entry_id,
               e.superseded_at, 'source_superseded'
        FROM memory_traces t
        JOIN entries e ON e.id = t.entry_id
        WHERE e.superseded_at IS NOT NULL
        ON CONFLICT (entity_norm, attribute_norm, source_entry_id)
        DO NOTHING
        ";

/// The body of `ensure_schema`'s transaction (schema.py:1162-1663). The
/// caller has sent `BEGIN` and commits (or rolls back) around it.
pub async fn ensure_schema_in_transaction(client: &Client) -> Result<(), SchemaError> {
    let x = |sql: &'static str| client.batch_execute(sql);

    // 1166: the dimension probe, before any DDL.
    refuse_on_embedding_dim_mismatch(client).await?;
    // 1171-1173
    x("SET LOCAL lock_timeout = '5s'; SET LOCAL statement_timeout = '30s';").await?;
    // 1174
    x("CREATE EXTENSION IF NOT EXISTS vector;").await?;
    // 1179-1182
    let had_trace_invalidations = matches!(
        simple_value(
            client,
            "SELECT to_regclass('public.memory_trace_invalidations')"
        )
        .await?,
        Some(Some(_))
    );
    // 1183: SCHEMA_SQL as one batch.
    client.batch_execute(schema_sql()).await?;
    // 1184-1187
    if !had_trace_invalidations {
        x(BACKFILL_TRACE_INVALIDATIONS).await?;
    }
    // 1190-1193 (v13)
    x(
        "ALTER TABLE entries ADD COLUMN IF NOT EXISTS reinforcements \
       INTEGER NOT NULL DEFAULT 0",
    )
    .await?;
    // 1195-1197 (v14)
    x("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS session_key TEXT").await?;
    // 1199-1201 (v15)
    x("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS parent_id TEXT").await?;
    // 1208-1214 (v21)
    x("ALTER TABLE entity_proposals ADD COLUMN IF NOT EXISTS decided_by TEXT").await?;
    x("ALTER TABLE entity_proposals ADD COLUMN IF NOT EXISTS \
       decided_at DOUBLE PRECISION")
    .await?;
    // 1220-1224 (v30), in the tuple's order.
    for ddl in [
        "judge_verdict TEXT",
        "judge_confidence REAL",
        "judge_note TEXT",
        "judge_model TEXT",
        "judged_at DOUBLE PRECISION",
    ] {
        client
            .batch_execute(&format!(
                "ALTER TABLE entity_proposals ADD COLUMN IF NOT EXISTS {ddl}"
            ))
            .await?;
    }
    // 1231-1234 (v23)
    x(
        "ALTER TABLE facts ADD COLUMN IF NOT EXISTS freshness_class \
       TEXT NOT NULL DEFAULT 'evergreen'",
    )
    .await?;
    // 1242-1246 (v26)
    x("ALTER TABLE facts ADD COLUMN IF NOT EXISTS kind \
       TEXT NOT NULL DEFAULT 'scalar'")
    .await?;
    x("ALTER TABLE facts ADD COLUMN IF NOT EXISTS value_norm TEXT").await?;
    // 1247
    x("DROP INDEX IF EXISTS facts_slot_current_uq").await?;
    // 1252-1257 (v24)
    x("CREATE TABLE IF NOT EXISTS entity_kinds (\
       entity_norm TEXT PRIMARY KEY, kind TEXT NOT NULL, \
       origin TEXT NOT NULL, confidence REAL, \
       decided_at DOUBLE PRECISION NOT NULL)")
    .await?;
    // 1260-1274 (v21)
    x("
            CREATE TABLE IF NOT EXISTS merge_decisions (
              id BIGSERIAL PRIMARY KEY,
              proposal_id BIGINT,
              entity_display TEXT,
              into_display TEXT,
              status TEXT NOT NULL,
              score REAL,
              reason TEXT,
              decided_by TEXT,
              decided_at DOUBLE PRECISION NOT NULL
            )
            ")
    .await?;
    // 1284-1300 (v27)
    x("
            CREATE TABLE IF NOT EXISTS dream_runs (
              id BIGSERIAL PRIMARY KEY,
              started_at DOUBLE PRECISION NOT NULL,
              finished_at DOUBLE PRECISION,
              cursor_before DOUBLE PRECISION NOT NULL,
              cursor_after DOUBLE PRECISION,
              pulled INTEGER NOT NULL DEFAULT 0,
              claims INTEGER NOT NULL DEFAULT 0,
              tallies JSONB NOT NULL DEFAULT '{}',
              status TEXT NOT NULL DEFAULT 'running',
              extractor TEXT,
              writer_id TEXT,
              rolled_back_at DOUBLE PRECISION
            )
            ")
    .await?;
    // 1302-1304
    x("CREATE INDEX IF NOT EXISTS dream_runs_started_idx \
       ON dream_runs (started_at DESC)")
    .await?;
    // 1305-1329
    x("
            CREATE TABLE IF NOT EXISTS dream_run_slots (
              id BIGSERIAL PRIMARY KEY,
              run_id BIGINT NOT NULL REFERENCES dream_runs(id)
                ON DELETE CASCADE,
              seq INTEGER NOT NULL,
              entity TEXT NOT NULL,
              attribute TEXT NOT NULL,
              entity_norm TEXT NOT NULL,
              attribute_norm TEXT NOT NULL,
              kind TEXT NOT NULL,
              op TEXT,
              prev_kind TEXT,
              prev_value TEXT,
              prev_status TEXT,
              prev_confidence REAL,
              prev_support TEXT,
              new_value TEXT,
              action TEXT NOT NULL,
              src_entry_id BIGINT,
              at DOUBLE PRECISION NOT NULL
            )
            ")
    .await?;
    // 1330-1332
    x("CREATE INDEX IF NOT EXISTS dream_run_slots_run_idx \
       ON dream_run_slots (run_id, seq)")
    .await?;
    // 1344-1363 (v28)
    x("
            CREATE TABLE IF NOT EXISTS chronicle_events (
              id BIGSERIAL PRIMARY KEY,
              occurred_at TIMESTAMPTZ,
              occurred_phrase TEXT,
              recorded_at DOUBLE PRECISION NOT NULL,
              actor TEXT NOT NULL,
              actor_norm TEXT NOT NULL,
              description TEXT NOT NULL,
              description_norm TEXT NOT NULL,
              episode TEXT,
              src_entry_id BIGINT,
              hlc_phys BIGINT,
              hlc_logical INT,
              writer_id TEXT,
              invalidated_at DOUBLE PRECISION
            )
            ")
    .await?;
    // 1364-1369
    x("CREATE INDEX IF NOT EXISTS chronicle_events_actor_idx \
       ON chronicle_events (actor_norm, occurred_at)")
    .await?;
    x("CREATE INDEX IF NOT EXISTS chronicle_events_episode_idx \
       ON chronicle_events (episode, occurred_at)")
    .await?;
    // 1373-1375
    x("ALTER TABLE dream_run_slots ADD COLUMN IF NOT EXISTS \
       chronicle_event_id BIGINT")
    .await?;
    // 1380 (v29)
    x("ALTER TABLE facts ADD COLUMN IF NOT EXISTS stance TEXT").await?;
    // 1388-1391 (v35): table-major, then column.
    for table in ["entries", "facts"] {
        for col in ["authority", "distortion_tolerance"] {
            client
                .batch_execute(&format!(
                    "ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} TEXT"
                ))
                .await?;
        }
    }
    // 1404-1416 (v31)
    x("
            CREATE TABLE IF NOT EXISTS retrieval_events (
              id BIGSERIAL PRIMARY KEY,
              query_text TEXT NOT NULL,
              origin TEXT NOT NULL DEFAULT 'search',
              session_id TEXT,
              episode_id TEXT,
              served JSONB NOT NULL DEFAULT '[]',
              created_at DOUBLE PRECISION NOT NULL
            )
            ")
    .await?;
    // 1425-1427 (v32)
    x("ALTER TABLE retrieval_events ADD COLUMN IF NOT EXISTS \
       params JSONB")
    .await?;
    // 1428-1433
    x("CREATE INDEX IF NOT EXISTS retrieval_events_session_idx \
       ON retrieval_events (session_id, created_at DESC)")
    .await?;
    x("CREATE INDEX IF NOT EXISTS retrieval_events_created_idx \
       ON retrieval_events (created_at)")
    .await?;
    // 1434-1445
    x("
            CREATE TABLE IF NOT EXISTS retrieval_uses (
              event_id BIGINT NOT NULL REFERENCES retrieval_events(id)
                ON DELETE CASCADE,
              entry_id BIGINT NOT NULL,
              used_via TEXT NOT NULL,
              created_at DOUBLE PRECISION NOT NULL,
              PRIMARY KEY (event_id, entry_id, used_via)
            )
            ")
    .await?;
    // 1454-1464 (v33)
    x("
            CREATE TABLE IF NOT EXISTS slot_reads (
              entity_norm    TEXT NOT NULL,
              attribute_norm TEXT NOT NULL,
              read_count     BIGINT NOT NULL DEFAULT 0,
              last_read_at   DOUBLE PRECISION,
              PRIMARY KEY (entity_norm, attribute_norm)
            )
            ")
    .await?;
    // 1471-1474
    if !crate::mutants::active("drop-alter-tail") {
        x("ALTER TABLE entries ADD COLUMN IF NOT EXISTS \
           explicit_reinforcements INTEGER NOT NULL DEFAULT 0")
        .await?;
    }
    // 1479-1484 (v38)
    x("ALTER TABLE entries ADD COLUMN IF NOT EXISTS dream_state TEXT").await?;
    x("ALTER TABLE entries ALTER COLUMN dream_state SET DEFAULT 'pending'").await?;
    // 1485-1496: add the check constraint only when absent.
    let has_check = simple_value(
        client,
        "SELECT 1 FROM pg_constraint \
         WHERE conname = 'entries_dream_state_check' \
         AND conrelid = 'public.entries'::regclass",
    )
    .await?
    .is_some();
    if !has_check {
        x(
            "ALTER TABLE entries ADD CONSTRAINT entries_dream_state_check \
           CHECK (dream_state IS NULL OR dream_state IN \
           ('pending', 'acknowledged', 'legacy-covered'))",
        )
        .await?;
    }
    // 1502-1504 (v34)
    x("ALTER TABLE retrieval_events ADD COLUMN IF NOT EXISTS \
       served_facts JSONB")
    .await?;
    // 1508-1514: drop the old episode FK only when present.
    let has_fk = simple_value(
        client,
        "SELECT 1 FROM pg_constraint WHERE conname = 'entries_episode_id_fkey'",
    )
    .await?
    .is_some();
    if has_fk {
        x("ALTER TABLE entries DROP CONSTRAINT entries_episode_id_fkey").await?;
    }
    // 1519
    x("DROP INDEX IF EXISTS entries_embedding_idx").await?;
    // 1536-1563 (v19/v26): the four healing updates, status as a bind
    // parameter (`%s`), in the tuple's order.
    for (table, status, extra_where) in [
        ("facts", "current", " AND kind = 'scalar'"),
        ("facts", "contested", ""),
        ("world_facts", "current", ""),
        ("lessons", "current", ""),
    ] {
        let sql = format!(
            "
                UPDATE {table} SET status = 'superseded',
                       superseded_at = COALESCE(superseded_at,
                                                EXTRACT(EPOCH FROM now()))
                WHERE id IN (
                  SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                      PARTITION BY entity_norm, attribute_norm
                      ORDER BY last_confirmed DESC, id DESC) AS rn
                    FROM {table} WHERE status = $1{extra_where}) d
                  WHERE d.rn > 1)
                "
        );
        client.execute(&sql, &[&status]).await?;
    }
    // 1564-1580
    x(
        "CREATE UNIQUE INDEX IF NOT EXISTS facts_slot_current_scalar_uq \
       ON facts (entity_norm, attribute_norm) \
       WHERE status = 'current' AND kind = 'scalar'",
    )
    .await?;
    x("CREATE UNIQUE INDEX IF NOT EXISTS facts_member_current_uq \
       ON facts (entity_norm, attribute_norm, value_norm) \
       WHERE status = 'current' AND kind = 'member'")
    .await?;
    x("CREATE UNIQUE INDEX IF NOT EXISTS facts_slot_contested_uq \
       ON facts (entity_norm, attribute_norm) WHERE status = 'contested'")
    .await?;
    // 1589-1595 (v36), in the tuple's order.
    for ddl in [
        "judge_verdict TEXT",
        "judge_confidence REAL",
        "judge_note TEXT",
        "judge_model TEXT",
        "judged_at DOUBLE PRECISION",
        "judge_relation TEXT",
        "decided_by TEXT",
        "decided_at DOUBLE PRECISION",
    ] {
        client
            .batch_execute(&format!(
                "ALTER TABLE edge_proposals ADD COLUMN IF NOT EXISTS {ddl}"
            ))
            .await?;
    }
    // 1598-1601
    for ddl in [
        "judge2_verdict TEXT",
        "judge2_confidence REAL",
        "judge2_model TEXT",
        "judged2_at DOUBLE PRECISION",
    ] {
        client
            .batch_execute(&format!(
                "ALTER TABLE entity_proposals ADD COLUMN IF NOT EXISTS {ddl}"
            ))
            .await?;
    }
    // 1602-1618
    x("
            CREATE TABLE IF NOT EXISTS curation_judgments (
              store      TEXT NOT NULL,
              a_key      TEXT NOT NULL,
              b_key      TEXT NOT NULL,
              verdict    TEXT NOT NULL,
              keep       TEXT,
              fold       TEXT,
              confidence REAL,
              note       TEXT,
              model      TEXT,
              judged_at  DOUBLE PRECISION NOT NULL,
              PRIMARY KEY (store, a_key, b_key)
            )
            ")
    .await?;
    // 1619-1627
    x(
        "CREATE UNIQUE INDEX IF NOT EXISTS world_facts_slot_current_uq \
       ON world_facts (entity_norm, attribute_norm) \
       WHERE status = 'current'",
    )
    .await?;
    x("CREATE UNIQUE INDEX IF NOT EXISTS lessons_slot_current_uq \
       ON lessons (entity_norm, attribute_norm) WHERE status = 'current'")
    .await?;
    // 1638-1652 (v37)
    x("
            CREATE TABLE IF NOT EXISTS store_decisions (
              id             BIGSERIAL PRIMARY KEY,
              store          TEXT NOT NULL,
              entity_norm    TEXT NOT NULL,
              attribute_norm TEXT NOT NULL,
              action         TEXT NOT NULL,
              decided_by     TEXT,
              reason         TEXT,
              record         JSONB,
              decided_at     DOUBLE PRECISION NOT NULL
            )
            ")
    .await?;
    // 1653-1656
    x("CREATE INDEX IF NOT EXISTS store_decisions_slot_idx \
       ON store_decisions (store, entity_norm, attribute_norm, \
       decided_at DESC)")
    .await?;
    // 1657-1662: the schema_version upsert, `str(55)` bound as text. No
    // newer-schema guard: a bank stamped higher is rewritten (spec P6).
    let version = SCHEMA_META_VERSION.to_string();
    client
        .execute_typed(
            "
            INSERT INTO meta (key, value) VALUES ('schema_version', $1::jsonb)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            ",
            &[(&version, Type::TEXT)],
        )
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_sql_drops_only_the_header_line() {
        assert_eq!(schema_sql_version(), Some(SCHEMA_META_VERSION));
        assert!(schema_sql().starts_with("\nCREATE TABLE IF NOT EXISTS meta ("));
        assert_eq!(
            schema_sql().len() + "-- schema_version 55\n".len(),
            SCHEMA_SQL_FILE.len()
        );
    }

    #[test]
    fn dim_message_matches_python() {
        // Expected text produced by schema.py's f-string for live_dim=384.
        let want = "Refusing to start: entries.embedding is vector(384) but this build's \
schema expects vector(1024). ensure_schema is additive-only and will NOT alter vector \
dimensions in place -- that would either half-migrate four tables at startup or write \
1024-d vectors into a 384-d column, corrupting the bank. Run the human-gated migration \
first: `python ops/migrate_embeddings.py` (backs up, requires the daemon stopped, \
re-embeds every row through the real embedder, and moves all four embedding columns \
from vector(384) to vector(1024)). Never run the daemon against a bank you have not migrated.";
        assert_eq!(dim_mismatch_message(384), want);
    }
}
