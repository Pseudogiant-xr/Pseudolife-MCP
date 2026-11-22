use super::*;
use crate::bank::DIM;
use crate::config::StartupBandSpec;
use crate::storage::Storage;
use std::time::Duration;

// ---- pure ----------------------------------------------------------------

fn entry(id: i64, band: &str) -> Entry {
    Entry {
        id,
        embedding: vec![0.5; 4],
        band: band.into(),
        stored_band: band.into(),
        text: format!("entry {id}"),
        ts: 100.0 + id as f64,
        source: "user".into(),
        ..Default::default()
    }
}

fn hydrated(entry: Entry) -> Hydrated {
    let stored_ts = entry.ts;
    Hydrated {
        entry,
        row: Ok(vec![0.5; 4]),
        stored_ts,
    }
}

fn bands(spec: &[(&str, usize)]) -> Vec<StartupBandSpec> {
    spec.iter()
        .map(|(name, max_entries)| StartupBandSpec {
            name: (*name).into(),
            max_entries: *max_entries,
            retention_policy: "balanced".into(),
            update_interval: Value::from(1),
        })
        .collect()
}

#[test]
fn diff_separates_added_changed_and_deleted_rows() {
    let resident: HashMap<i64, i64> = [(1, 10), (2, 20), (3, 30), (4, UNKNOWN_VERSION)].into();
    let current = [(1, 10), (2, 21), (4, 40), (6, 60), (5, 50)];
    assert_eq!(
        diff(&resident, &current),
        Diff {
            added: vec![5, 6],
            changed: vec![2, 4],
            deleted: 1,
        }
    );
    assert_eq!(
        diff(
            &resident,
            &[(1, 10), (2, 20), (3, 30), (4, UNKNOWN_VERSION)]
        ),
        Diff::default()
    );
}

#[test]
fn only_telemetry_columns_count_as_unchanged_content() {
    let resident = entry(1, "flat");
    let mut fresh = entry(1, "flat");
    fresh.access_count = 9.into();
    fresh.reinforcements = 3;
    fresh.last_logical_turn = Some(4);
    assert!(same_content(&resident, &hydrated(fresh)));
    let edits: [fn(&mut Entry); 14] = [
        |e| e.text.push('!'),
        |e| e.embedding[0] = 0.25,
        |e| e.stored_band = "other".into(),
        |e| e.surprise_value = 0.5,
        |e| e.ts += 1.0,
        |e| e.source = "system".into(),
        |e| e.superseded_at = Some(1.0),
        |e| e.superseded_by_text = Some("newer".into()),
        |e| e.episode_id = Some("ep".into()),
        |e| e.episode_title = Some("title".into()),
        |e| e.tags = vec![Value::from("t")],
        |e| e.slots = vec![Value::from("s")],
        |e| e.authority = Some("directive".into()),
        |e| e.dream_state = Some("consolidated".into()),
    ];
    for edit in edits {
        let mut fresh = entry(1, "flat");
        edit(&mut fresh);
        assert!(!same_content(&resident, &hydrated(fresh)));
    }
    let mut fresh = entry(1, "flat");
    fresh.distortion_tolerance = Some("belief".into());
    assert!(!same_content(&resident, &hydrated(fresh)));
    // A stored zero timestamp is replaced at hydration: never comparable.
    let mut zero = hydrated(entry(1, "flat"));
    zero.stored_ts = 0.0;
    assert!(!same_content(&resident, &zero));
}

#[test]
fn seating_moves_only_when_a_band_overflows() {
    let entries = [entry(1, "a"), entry(2, "a"), entry(3, "b")];
    assert!(!crate::bank::seating_moves(
        &entries,
        &bands(&[("flat", 1)])
    ));
    assert!(!crate::bank::seating_moves(
        &entries,
        &bands(&[("a", 2), ("b", 1)])
    ));
    assert!(crate::bank::seating_moves(
        &entries,
        &bands(&[("a", 1), ("b", 9)])
    ));
    // What spills from above counts against the next band's capacity.
    let entries = [entry(1, "a"), entry(2, "a"), entry(3, "b")];
    assert!(crate::bank::seating_moves(
        &entries,
        &bands(&[("a", 1), ("b", 1), ("c", 9)])
    ));
}

// ---- database: a disposable bank named by PL_SHADOW_REFRESH_DSN ----------

/// One test at a time: they share the disposable bank and its lease (the
/// service-level tests in `service_shadow_tests.rs` too).
pub(crate) static DB: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// Set by the CI step that supplies the database: a skipped test would
/// count as passed, so with it set every database test runs or panics.
pub(crate) fn required() -> bool {
    std::env::var_os("PL_SHADOW_REFRESH_REQUIRED").is_some_and(|v| v == "1")
}

pub(crate) fn test_dsn() -> Option<String> {
    let dsn = std::env::var("PL_SHADOW_REFRESH_DSN")
        .ok()
        .filter(|d| !d.is_empty());
    match &dsn {
        None if required() => {
            panic!("PL_SHADOW_REFRESH_REQUIRED=1 but PL_SHADOW_REFRESH_DSN is unset or empty")
        }
        None => eprintln!("PL_SHADOW_REFRESH_DSN unset: skipping database test"),
        Some(dsn) => assert!(
            crate::storage::parse_dsn(dsn)
                .unwrap()
                .get_dbname()
                .is_some_and(|name| name.starts_with("pl_cf_")),
            "the shadow refresh tests clear their bank: name a disposable pl_cf_ database"
        ),
    }
    dsn
}

#[tokio::test]
async fn db_candidate_edges_preserve_decimal_confidence() {
    // Exercises dream::candidates::load_inputs here because the hosted
    // PostgreSQL lane selects this module's db_ tests.
    let Some(dsn) = test_dsn() else { return };
    let _serial = DB.lock().await;
    let fixture = Fixture::open(&dsn, MemoryConfig::default()).await;
    let client = fixture.writer.client();
    client
        .batch_execute(
            "BEGIN;
             TRUNCATE entities, entries, lessons, dismissed_pairs CASCADE;
             INSERT INTO entities (id, canonical, display, etype, created_at)
                 VALUES (11, 'candidate-dst', 'Candidate dst', NULL, 2.5),
                        (7, 'candidate-src', 'Candidate src', 'artifact', 1.25);
             INSERT INTO entity_aliases (alias, entity_id)
                 VALUES ('src-z', 7), ('dst-a', 11), ('src-a', 7);
             INSERT INTO edges (id, src_id, relation, dst_id, confidence, origin, asserted_at, superseded_at)
                 VALUES (31, 7, 'uses', 11, 0.7, 'fixture', 17.25, NULL),
                        (32, 11, 'uses', 7, 0.3, 'old', 10, 12);
             INSERT INTO entries (id, band, text, embedding, ts) VALUES
                 (41, 'flat', 'candidate evidence',
                  (ARRAY[0,0,0.25,0,0,0,0,0,0,-0.75]::real[]
                   || array_fill(0.0::real, ARRAY[1014]))::vector, 3);
             INSERT INTO entity_sources (entity_id, source, updated_at)
                 VALUES (7, 'zeta', 4), (7, 'alpha', 4), (11, 'beta', 4);
             INSERT INTO memory_traces (entity_norm, attribute_norm, entry_id, created_at)
                 VALUES ('candidate-src', 'build', 41, 4), ('candidate-dst', 'build', 41, 4);
             INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, value, status,
                                confidence, asserted_at, last_confirmed, entity_id)
                 VALUES ('Candidate src', 'os', 'candidate-src', 'os', 'a', 'current', 1, 5, 5, 7),
                        ('Candidate src', 'owner', 'candidate-src', 'owner', 'b', 'current', 1, 5, 5, 7),
                        ('Unlinked', 'os', 'unlinked', 'os', 'c', 'current', 1, 5, 5, NULL),
                        ('Candidate dst', 'os', 'candidate-dst', 'os', 'd', 'superseded', 1, 5, 5, 11);
             INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, value, status,
                                  confidence, asserted_at, last_confirmed, entity_id, object_entity_id)
                 VALUES ('Candidate src', 'uses', 'candidate-src', 'uses', 'Candidate dst',
                         'current', 1, 5, 5, 7, 11);
             INSERT INTO entity_proposals (id, kind, entity_id, into_id, score, reason, status, created_at)
                 VALUES (51, 'merge', 7, 11, 0.6, 'fixture merge', 'pending', 6.25),
                        (52, 'junk', 11, NULL, 0.2, 'already reviewed', 'dismissed', 6);
             INSERT INTO edge_proposals (id, src_id, relation, dst_id, confidence, similarity,
                                         rationale, source, created_at, status)
                 VALUES (61, 7, 'uses', 11, 0.8, 0.75, 'fixture link', 'fixture', 7.25, 'pending'),
                        (62, 11, 'uses', 7, 0.4, NULL, NULL, 'fixture', 7, 'dismissed');
             INSERT INTO dismissed_pairs (a_norm, b_norm, dismissed_at)
                 VALUES ('candidate-src', 'related', 9), ('candidate-dst', 'other', 8);",
        )
        .await
        .expect("seed each candidate loader input");
    let inputs = crate::dream::candidates::load_inputs(client)
        .await
        .expect("read candidate inputs");
    client.batch_execute("ROLLBACK").await.unwrap();
    fixture.close().await;

    use serde_json::json;
    let crate::dream::candidates::CandInputs {
        entities,
        mut aliases,
        edges,
        entries,
        scopes,
        traces,
        mut facts,
        mut fact_texts,
        pending_entities,
        lesson_refs,
        pending_links,
        dismissed,
    } = inputs;
    assert_eq!(
        json!(entities),
        json!([
            {"id": 7, "canonical": "candidate-src", "display": "Candidate src",
             "etype": "artifact", "created_at": 1.25},
            {"id": 11, "canonical": "candidate-dst", "display": "Candidate dst",
             "etype": null, "created_at": 2.5},
        ])
    );
    aliases.sort_by_key(|(id, _)| *id);
    assert_eq!(
        json!(aliases),
        json!([[7, ["src-a", "src-z"]], [11, ["dst-a"]]])
    );
    assert_eq!(
        json!(edges),
        json!([{
            "id": 31, "src_id": 7, "dst_id": 11, "relation": "uses",
            "confidence": 0.7, "origin": "fixture", "asserted_at": 17.25,
        }])
    );
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0].id, 41);
    assert_eq!(entries[0].text, json!("candidate evidence"));
    let mut vector = vec![0.0; DIM];
    vector[2] = 0.25;
    vector[9] = -0.75;
    assert_eq!(entries[0].embedding.as_deref(), Some(vector.as_slice()));
    assert_eq!(
        json!(scopes),
        json!([[7, ["alpha", "zeta"]], [11, ["beta"]]])
    );
    assert_eq!(
        json!(traces),
        json!([["candidate-dst", [41]], ["candidate-src", [41]]])
    );
    facts.sort();
    assert_eq!(facts, [(7, 2)]);
    fact_texts.sort();
    assert_eq!(
        json!(fact_texts),
        json!([["Candidate src", 2], ["Unlinked", 1]])
    );
    assert_eq!(lesson_refs, [11, 7]);
    assert_eq!(
        json!(dismissed),
        json!([["candidate-dst", "other"], ["candidate-src", "related"]])
    );
    assert_eq!(
        json!(pending_entities),
        json!([{
            "id": 51, "kind": "merge", "entity_id": 7, "into_id": 11, "score": 0.6,
            "reason": "fixture merge", "status": "pending", "created_at": 6.25,
            "judge_verdict": null, "judge_confidence": null, "judge_note": null,
            "judge_model": null, "judged_at": null, "judge2_verdict": null,
            "judge2_confidence": null, "judge2_model": null, "judged2_at": null,
            "judge3_verdict": null, "judge3_confidence": null, "judge3_model": null,
            "judged3_at": null, "judge2_served_model": null, "judge2_policy": null,
            "judge3_served_model": null, "judge3_policy": null,
            "entity": "Candidate src", "into": "Candidate dst",
        }])
    );
    assert_eq!(
        json!(pending_links),
        json!([{
            "id": 61, "src_id": 7, "relation": "uses", "dst_id": 11, "confidence": 0.8,
            "similarity": 0.75, "rationale": "fixture link", "source": "fixture",
            "created_at": 7.25, "status": "pending", "judge_verdict": null,
            "judge_confidence": null, "judge_note": null, "judge_model": null,
            "judged_at": null, "judge_relation": null, "judge2_verdict": null,
            "judge2_confidence": null, "judge2_model": null, "judged2_at": null,
            "judge2_relation": null, "judge2_served_model": null, "judge2_policy": null,
            "src": "Candidate src", "dst": "Candidate dst",
        }])
    );
}

pub(crate) fn vector(k: usize) -> String {
    let mut v = vec!["0"; DIM];
    v[k] = "1";
    format!("[{}]", v.join(","))
}

/// Empties every table the resident stores read.
pub(crate) const CLEAR: &str = "DELETE FROM entries; DELETE FROM facts; DELETE FROM world_facts; \
     DELETE FROM lessons; DELETE FROM episodes; DELETE FROM cms_counters; \
     DELETE FROM meta WHERE key IN ('active_session_pointer', 'episode_tombstones', \
     'deferred_empty_roots', 'cortex_supersession_log', 'cortex_dream_cursor', \
     'coordination_hlc_highwater', 'bank_id', 'flat_band_migration_v1')";

/// The writer session that seeds and mutates the bank, and a read-only
/// session that reads it as the shadow does (so hydration never writes a
/// band stamp back).
struct Fixture {
    writer: Storage,
    reader: Client,
    reader_task: tokio::task::JoinHandle<()>,
    memory: MemoryConfig,
    /// Holds no config.yaml: a pending flat migration refuses.
    data_dir: std::path::PathBuf,
}

struct Resident {
    bank: RwLock<Bank>,
    state: ShadowState,
}

impl Fixture {
    async fn open(dsn: &str, memory: MemoryConfig) -> Fixture {
        let writer = Storage::open(dsn).await.unwrap();
        writer.client().batch_execute(CLEAR).await.unwrap();
        let (reader, reader_task) = crate::storage::connect(
            &crate::storage::parse_dsn(dsn).unwrap(),
            Duration::from_secs(10),
        )
        .await
        .unwrap();
        reader
            .batch_execute("SET default_transaction_read_only = on")
            .await
            .unwrap();
        Fixture {
            writer,
            reader,
            reader_task,
            memory,
            data_dir: std::env::temp_dir().join("pl-shadow-refresh-no-config"),
        }
    }

    async fn close(self) {
        drop(self.reader);
        let _ = self.reader_task.await;
        self.writer.close().await;
    }

    async fn exec(&self, sql: &str) {
        self.writer.client().batch_execute(sql).await.unwrap();
    }

    async fn insert(&self, band: &str, text: &str, k: usize) -> i64 {
        self.writer
            .client()
            .query_one(
                "INSERT INTO entries (band, text, embedding, ts, source) \
                 VALUES ($1, $2, $3::text::vector, $4, 'user') RETURNING id",
                &[&band, &text, &vector(k), &(1000.0 + k as f64)],
            )
            .await
            .unwrap()
            .get(0)
    }

    async fn marker(&self) -> Marker {
        self.writer.shadow_marker().await.unwrap()
    }

    /// A cold start's bank, as the full rebuild builds it.
    async fn rebuilt(&self) -> Bank {
        let raw = crate::startup::metadata(&self.reader).await.unwrap();
        crate::startup::candidate(&self.reader, &self.memory, raw, DIM)
            .await
            .unwrap()
            .0
    }

    /// The service's full-rebuild order: probe, then one read of the row
    /// versions and meta (the startup metadata included), then hydrate.
    async fn resident(&self) -> Resident {
        self.resident_with(async {}).await.0
    }

    /// [`Fixture::resident`], running `between` after the pre-read and
    /// before the hydration; also the startup state hydrated.
    async fn resident_with(
        &self,
        between: impl std::future::Future<Output = ()>,
    ) -> (Resident, crate::startup::StartupState) {
        let marker = self.marker().await;
        let (inputs, raw) = preread_then(&self.reader, between).await.unwrap();
        let (bank, startup) = crate::startup::candidate(&self.reader, &self.memory, raw, DIM)
            .await
            .unwrap();
        let state = ShadowState::new(marker, inputs, &bank);
        (
            Resident {
                bank: RwLock::new(bank),
                state,
            },
            startup,
        )
    }

    async fn refresh(&self, resident: &Resident) -> Outcome {
        let marker = self.marker().await;
        refresh(
            &self.reader,
            &self.memory,
            DIM,
            &self.data_dir,
            &resident.state,
            &marker,
            &resident.bank,
        )
        .await
        .unwrap()
    }

    async fn partial(&self, resident: &Resident) -> Refresh {
        match self.refresh(resident).await {
            Outcome::Partial(refresh) => *refresh,
            Outcome::Full(reason) => panic!("expected an incremental refresh, got: {reason}"),
        }
    }

    /// The resident bank equals what a full rebuild would build now, order
    /// included.
    async fn assert_matches_rebuild(&self, resident: &Resident) {
        let fresh = self.rebuilt().await;
        let bank = resident.bank.read().unwrap();
        assert_eq!(bank.entries_dump(), fresh.entries_dump());
        let order = |b: &Bank| b.entries.iter().map(|e| e.id).collect::<Vec<_>>();
        assert_eq!(order(&bank), order(&fresh));
        assert_eq!(bank.matrix, fresh.matrix);
    }
}

/// Install a refresh as the service does: a rebuilt bank replaces the old
/// one, otherwise the old one is patched in place.
fn install(resident: &mut Resident, mut refresh: Refresh) {
    match refresh.bank.take() {
        Some(bank) => *resident.bank.write().unwrap() = bank,
        None => refresh.take_bank_delta().apply(&resident.bank),
    }
    resident.state = refresh.state;
}

fn seq_of(resident: &Resident, id: i64) -> u64 {
    let bank = resident.bank.read().unwrap();
    bank.entries.iter().find(|e| e.id == id).unwrap().seq
}

fn flat() -> MemoryConfig {
    MemoryConfig::default()
}

fn two_bands(first_capacity: usize) -> MemoryConfig {
    MemoryConfig {
        bands: vec!["a".into(), "b".into()],
        band_specs: bands(&[("a", first_capacity), ("b", 100)]),
        ..MemoryConfig::default()
    }
}

#[tokio::test]
async fn db_telemetry_change_patches_the_bank_in_place() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let fx = Fixture::open(&dsn, flat()).await;
    let alpha = fx.insert("flat", "alpha", 1).await;
    fx.insert("flat", "beta", 2).await;
    let mut resident = fx.resident().await;
    let seq = seq_of(&resident, alpha);
    // What Python's read path writes for a served or fetched entry.
    fx.exec(&format!(
        "UPDATE entries SET access_count = access_count + 3, reinforcements = reinforcements + 2, \
         explicit_reinforcements = explicit_reinforcements + 1 WHERE id = {alpha}"
    ))
    .await;
    let refresh = fx.partial(&resident).await;
    assert!(
        refresh.bank.is_none(),
        "a telemetry change rebuilt the bank"
    );
    assert_eq!(
        refresh.stats,
        Stats {
            patched: 1,
            appended: 0,
            reloaded: vec![],
        }
    );
    assert!(refresh.counters.is_none() && !refresh.startup_changed());
    install(&mut resident, refresh);
    {
        let bank = resident.bank.read().unwrap();
        let e = bank.entries.iter().find(|e| e.id == alpha).unwrap();
        assert_eq!(e.access_count.load(Ordering::Relaxed), 3);
        assert_eq!(e.reinforcements, 2);
    }
    // The same resident entry, not a rebuilt one.
    assert_eq!(seq_of(&resident, alpha), seq);
    fx.assert_matches_rebuild(&resident).await;
    fx.close().await;
}

#[tokio::test]
async fn db_counters_change_reloads_only_the_counters() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let fx = Fixture::open(&dsn, flat()).await;
    fx.insert("flat", "alpha", 1).await;
    let resident = fx.resident().await;
    fx.exec(
        "INSERT INTO cms_counters (id, preset_name, interaction_count, logical_turn_count, \
         tier_queries, tier_hits, surprise_history, consolidation_events, updated_at) \
         VALUES (1, 'flat', 7, 0, 0, '{}', '{}', '[]', 1.0)",
    )
    .await;
    let refresh = fx.partial(&resident).await;
    assert_eq!(
        refresh.stats,
        Stats {
            patched: 0,
            appended: 0,
            reloaded: vec!["cms_counters"],
        }
    );
    assert!(refresh.bank.is_none() && !refresh.startup_changed());
    assert!(refresh.cortex.is_none() && refresh.world.is_none() && refresh.lessons.is_none());
    let row = refresh
        .counters
        .clone()
        .expect("the write state is rebuilt");
    assert_eq!(row.expect("the counters row").interaction_count, 7);
    fx.close().await;
}

#[tokio::test]
async fn db_added_entry_is_appended_and_searchable() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let fx = Fixture::open(&dsn, flat()).await;
    let alpha = fx.insert("flat", "alpha", 1).await;
    fx.insert("flat", "beta", 2).await;
    let mut resident = fx.resident().await;
    let seq = seq_of(&resident, alpha);
    fx.insert("flat", "gamma", 7).await;
    let refresh = fx.partial(&resident).await;
    assert!(refresh.bank.is_none(), "an addition rebuilt the bank");
    assert_eq!(
        refresh.stats,
        Stats {
            patched: 0,
            appended: 1,
            reloaded: vec![],
        }
    );
    install(&mut resident, refresh);
    assert_eq!(seq_of(&resident, alpha), seq);
    {
        let bank = resident.bank.read().unwrap();
        let mut query = vec![0.0; DIM];
        query[7] = 1.0;
        let params = crate::search::Params {
            min_score: Some(0.0),
            count_access: false,
            ..crate::search::fixture_params("", 3)
        };
        let ranked = crate::search::rank(&bank, &query, &params, None, &[], None);
        let top = ranked.hits[0].entry().expect("an entry hit");
        assert_eq!(bank.entries[top].text, "gamma");
    }
    fx.assert_matches_rebuild(&resident).await;
    // Nothing changed since: the next refresh does nothing.
    let refresh = fx.partial(&resident).await;
    assert_eq!(refresh.stats, Stats::default());
    fx.close().await;
}

#[tokio::test]
async fn db_content_change_and_deletion_rebuild_only_the_bank() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let fx = Fixture::open(&dsn, flat()).await;
    let alpha = fx.insert("flat", "alpha", 1).await;
    let beta = fx.insert("flat", "beta", 2).await;
    let mut resident = fx.resident().await;
    fx.exec(&format!(
        "UPDATE entries SET text = 'alpha, edited', superseded_at = 5 WHERE id = {alpha}"
    ))
    .await;
    let refresh = fx.partial(&resident).await;
    assert!(refresh.bank.is_some(), "a content change kept the bank");
    assert_eq!(refresh.stats.reloaded, vec!["entries"]);
    assert!(refresh.cortex.is_none() && refresh.counters.is_none() && !refresh.startup_changed());
    install(&mut resident, refresh);
    fx.assert_matches_rebuild(&resident).await;
    fx.exec(&format!("DELETE FROM entries WHERE id = {beta}"))
        .await;
    let refresh = fx.partial(&resident).await;
    assert!(refresh.bank.is_some(), "a deletion kept the bank");
    assert_eq!(refresh.stats.reloaded, vec!["entries"]);
    install(&mut resident, refresh);
    fx.assert_matches_rebuild(&resident).await;
    assert_eq!(resident.bank.read().unwrap().entries.len(), 1);
    fx.close().await;
}

#[tokio::test]
async fn db_commit_during_a_refresh_forces_another() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let fx = Fixture::open(&dsn, flat()).await;
    let alpha = fx.insert("flat", "alpha", 1).await;
    let beta = fx.insert("flat", "beta", 2).await;
    let mut resident = fx.resident().await;
    // The probe sees the first change; the second commits after it, while
    // the refresh reads.
    fx.exec(&format!(
        "UPDATE entries SET access_count = 1 WHERE id = {alpha}"
    ))
    .await;
    let probed = fx.marker().await;
    fx.exec(&format!(
        "UPDATE entries SET access_count = 2 WHERE id = {beta}"
    ))
    .await;
    let outcome = refresh(
        &fx.reader,
        &fx.memory,
        DIM,
        &fx.data_dir,
        &resident.state,
        &probed,
        &resident.bank,
    )
    .await
    .unwrap();
    let Outcome::Partial(refresh) = outcome else {
        panic!("expected an incremental refresh")
    };
    assert_eq!(
        refresh.state.marker, probed,
        "the post-refresh marker was recorded"
    );
    install(&mut resident, *refresh);
    assert_ne!(
        fx.marker().await,
        resident.state.marker,
        "no later refresh would run"
    );
    let again = fx.partial(&resident).await;
    assert!(again.bank.is_none());
    install(&mut resident, again);
    fx.assert_matches_rebuild(&resident).await;

    // A commit between the version read and the hydration: the bank holds
    // the newer row under an older version, so the next refresh fetches
    // and compares it instead of trusting it, and keeps the bank.
    let edit = format!("UPDATE entries SET text = 'alpha, during hydration' WHERE id = {alpha}");
    let (mut resident, _) = fx.resident_with(fx.exec(&edit)).await;
    let refresh = fx.partial(&resident).await;
    assert!(refresh.bank.is_none(), "a row already resident was rebuilt");
    install(&mut resident, refresh);
    fx.assert_matches_rebuild(&resident).await;
    assert_eq!(fx.partial(&resident).await.stats, Stats::default());
    fx.close().await;
}

#[tokio::test]
async fn db_episode_and_meta_changes_rebuild_the_write_state_only() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let fx = Fixture::open(&dsn, flat()).await;
    fx.insert("flat", "alpha", 1).await;
    let mut resident = fx.resident().await;
    fx.exec("INSERT INTO episodes (id, title, started_at) VALUES ('shadow-refresh', 'Open', 5)")
        .await;
    let refresh = fx.partial(&resident).await;
    assert_eq!(refresh.stats.reloaded, vec!["episodes"]);
    assert!(refresh.bank.is_none() && refresh.metadata.is_none());
    let (episodes, current) = refresh.episodes.as_ref().unwrap();
    assert_eq!(episodes.len(), 1);
    assert_eq!(current.as_deref(), Some("shadow-refresh"));
    assert_eq!(refresh.counters, Some(None), "the write state is rebuilt");
    install(&mut resident, refresh);
    fx.exec(
        "INSERT INTO meta (key, value) VALUES \
         ('active_session_pointer', '{\"session_id\": \"s1\", \"ts\": 5}')",
    )
    .await;
    let refresh = fx.partial(&resident).await;
    assert_eq!(refresh.stats.reloaded, vec!["meta"]);
    let metadata = refresh.metadata.as_ref().expect("identity metadata");
    assert_eq!(metadata["active_session"], serde_json::json!(["s1", 5.0]));
    assert!(!refresh.cortex_meta && !refresh.clock && refresh.counters.is_some());
    install(&mut resident, refresh);
    fx.exec("INSERT INTO meta (key, value) VALUES ('cortex_dream_cursor', '12.5')")
        .await;
    let refresh = fx.partial(&resident).await;
    assert!(refresh.cortex_meta && refresh.metadata.is_some());
    install(&mut resident, refresh);
    fx.exec("INSERT INTO meta (key, value) VALUES ('bank_id', '\"another-bank\"')")
        .await;
    assert!(matches!(fx.refresh(&resident).await, Outcome::Full(_)));
    fx.close().await;
}

#[tokio::test]
async fn db_multi_band_banks_patch_only_while_seating_is_score_free() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    // Within capacity: telemetry is patched, an addition rebuilds (seating
    // orders by stored band before id).
    let fx = Fixture::open(&dsn, two_bands(10)).await;
    let alpha = fx.insert("a", "alpha", 1).await;
    fx.insert("b", "beta", 2).await;
    let mut resident = fx.resident().await;
    fx.exec(&format!(
        "UPDATE entries SET access_count = 4 WHERE id = {alpha}"
    ))
    .await;
    let refresh = fx.partial(&resident).await;
    assert!(refresh.bank.is_none());
    assert_eq!(refresh.stats.patched, 1);
    install(&mut resident, refresh);
    fx.assert_matches_rebuild(&resident).await;
    fx.insert("a", "gamma", 3).await;
    let refresh = fx.partial(&resident).await;
    assert!(refresh.bank.is_some(), "a multi-band addition was appended");
    install(&mut resident, refresh);
    fx.assert_matches_rebuild(&resident).await;
    fx.close().await;

    // Over capacity the spill follows retention scores: telemetry rebuilds.
    let fx = Fixture::open(&dsn, two_bands(1)).await;
    let alpha = fx.insert("a", "alpha", 1).await;
    fx.insert("a", "beta", 2).await;
    let mut resident = fx.resident().await;
    fx.exec(&format!(
        "UPDATE entries SET access_count = 50 WHERE id = {alpha}"
    ))
    .await;
    let refresh = fx.partial(&resident).await;
    assert!(
        refresh.bank.is_some(),
        "an over-capacity telemetry change was patched"
    );
    install(&mut resident, refresh);
    fx.assert_matches_rebuild(&resident).await;
    fx.close().await;
}

#[tokio::test]
async fn db_meta_commit_between_the_reads_is_refreshed() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let fx = Fixture::open(&dsn, flat()).await;
    fx.insert("flat", "alpha", 1).await;
    // A meta commit after the full rebuild's meta read, before it hydrates.
    let (resident, startup) = fx
        .resident_with(fx.exec(
            "INSERT INTO meta (key, value) VALUES ('cortex_dream_cursor', '12.5'), \
             ('active_session_pointer', '{\"session_id\": \"s2\", \"ts\": 6}')",
        ))
        .await;
    // The stores were built from the meta read before the commit...
    assert_eq!(
        startup.metadata["cortex_dream_cursor"],
        serde_json::json!(0.0)
    );
    // ...so the next refresh must see the commit, cortex included.
    let refresh = fx.partial(&resident).await;
    let metadata = refresh
        .metadata
        .as_ref()
        .expect("the meta commit was missed");
    assert_eq!(metadata["cortex_dream_cursor"], serde_json::json!(12.5));
    assert_eq!(metadata["active_session"], serde_json::json!(["s2", 6.0]));
    assert!(refresh.cortex_meta, "the cortex reconfigure was missed");
    fx.close().await;
}

#[tokio::test]
async fn db_pending_flat_migration_refuses_the_refresh() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let fx = Fixture::open(&dsn, flat()).await;
    let alpha = fx.insert("flat", "alpha", 1).await;
    let resident = fx.resident().await;
    // migrate-flat --apply on the writer: its marker, and rows rewritten.
    fx.exec(&format!(
        "INSERT INTO meta (key, value) VALUES ('flat_band_migration_v1', '{{\"capacity\": 5250}}'); \
         UPDATE entries SET access_count = 1 WHERE id = {alpha}"
    ))
    .await;
    match fx.refresh(&resident).await {
        Outcome::Full(reason) => assert!(
            reason.contains("flat migration config finalization is pending"),
            "{reason}"
        ),
        Outcome::Partial(_) => panic!("a pending flat migration was served incrementally"),
    }
    fx.close().await;
}
