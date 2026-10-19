//! Expected values come from the Python oracle (CPython 3.11, the repo
//! root): `service._parse_slot_key` / `_slot_key`,
//! `graph_consolidation._compound_halves`, and
//! `curation_safety.snapshot_record` / `evidence_fingerprint`.

use std::sync::Arc;

use serde_json::{Value, json};

use super::*;
use crate::stores::cortex::slot_key;

#[test]
fn slot_keys_parse_and_build_like_python() {
    let cases: [(&str, Option<(&str, &str)>); 8] = [
        ("a|b", Some(("a", "b"))),
        ("a%7Cx|b", Some(("a|x", "b"))),
        ("a|b|c", None),
        ("nopipe", None),
        ("|", Some(("", ""))),
        ("a|", Some(("a", ""))),
        ("%7C|%7C%7C", Some(("|", "||"))),
        ("A B|C", Some(("A B", "C"))),
    ];
    for (key, want) in cases {
        let got = parse_slot_key(key);
        let want = want.map(|(e, a)| (e.to_string(), a.to_string()));
        assert_eq!(got, want, "{key}");
    }
    assert_eq!(slot_key_text("ci|cd", "x"), "ci%7Ccd|x");
    assert_eq!(slot_key_text("a", "b|c%7C"), "a|b%7Cc%7C");
}

fn args(v: Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap()
}

#[test]
fn plan_refuses_what_mcp_server_refuses() {
    let dismiss = |v: Value| plan("dismiss_slot_pair", &args(v));
    let refused = json!({"error": "store_src_dst_required", "detail": DISMISS_DETAIL});
    assert_eq!(
        dismiss(json!({"store": "lesson", "src": "a|b"})).unwrap_err(),
        refused
    );
    assert_eq!(
        dismiss(json!({"store": "", "src": "a|b", "dst": "c|d"})).unwrap_err(),
        refused
    );
    assert_eq!(
        dismiss(json!({"store": "lesson", "src": "a", "dst": "c|d"})).unwrap_err(),
        refused
    );
    // A bad store reaches the service (bad_store), as in Python.
    assert!(matches!(
        dismiss(json!({"store": "bogus", "src": "a|b", "dst": "c|d"})),
        Ok(SlotPlan::Dismiss { .. })
    ));

    let restore = |v: Value| plan("restore_slot", &args(v));
    let refused = json!({"error": "store_src_required", "detail": RESTORE_DETAIL});
    assert_eq!(
        restore(json!({"store": "bogus", "src": "a"})).unwrap_err(),
        refused
    );
    assert_eq!(
        restore(json!({"store": "world", "src": ""})).unwrap_err(),
        refused
    );
    assert_eq!(
        restore(json!({"store": "world", "src": "a|b|c"})).unwrap_err(),
        refused
    );
    let Ok(SlotPlan::Restore {
        entity, attribute, ..
    }) = restore(json!({"store": "lesson", "src": "Deploy%7CEngine"}))
    else {
        panic!("bare entity");
    };
    assert_eq!((entity.as_str(), attribute), ("Deploy|Engine", None));
    // `attr or None`: an empty aspect restores the whole entity.
    let Ok(SlotPlan::Restore {
        entity, attribute, ..
    }) = restore(json!({"store": "lesson", "src": "deploy|"}))
    else {
        panic!("empty aspect");
    };
    assert_eq!((entity.as_str(), attribute), ("deploy", None));
    let Ok(SlotPlan::Restore { attribute, .. }) =
        restore(json!({"store": "world", "src": "rust|latest version"}))
    else {
        panic!("slot");
    };
    assert_eq!(attribute.as_deref(), Some("latest version"));
}

#[test]
fn compound_halves_match_python() {
    let cases: [(&str, Option<(&str, &str)>); 13] = [
        ("pg+extractor", Some(("pg", "extractor"))),
        ("codex-cli / installer", Some(("codex-cli", "installer"))),
        ("origin/master", None),
        ("a + ", None),
        ("ops/backup.ps1 + x", None),
        ("a.py / b", None),
        ("foo\u{a0}/\u{a0}bar", Some(("foo", "bar"))),
        ("x +y+z", Some(("x", "y+z"))),
        ("+", None),
        (" / ", None),
        ("C++", None),
        ("tool.json+x", None),
        ("ab.cdefg+x", Some(("ab.cdefg", "x"))),
    ];
    for (name, want) in cases {
        let want = want.map(|(a, b)| (a.to_string(), b.to_string()));
        assert_eq!(compound_halves(name), want, "{name:?}");
    }
}

fn lesson() -> LessonRecord {
    let emb: Arc<[f32]> = Arc::from(vec![0.5f32, -1.25, 3.0]);
    LessonRecord {
        entity: "Deploy Engine".into(),
        attribute: "Approach".into(),
        value: "use blue/green \u{e9}\"\n".into(),
        about: Some("kubectl".into()),
        polarity: "+".into(),
        outcome: "success".into(),
        confidence: 0.8999999761581421,
        status: "current".into(),
        origin: Some("agent".into()),
        support: vec!["b".into(), "a".into()],
        provenance: vec!["signal:2".into(), "signal:1".into()],
        asserted_at: 1700000000.0,
        last_confirmed: 1700000000.5,
        supersedes_value: None,
        superseded_by_value: None,
        superseded_at: None,
        unit: None,
        embedding: Some(emb),
        tx_time: None,
        valid_time: Some(1e16),
        hlc_phys: Some(1760000000000),
        hlc_logical: Some(3),
        writer_id: Some("w".into()),
        session_id: None,
        version: 1,
        key: slot_key("Deploy Engine", "Approach"),
    }
}

/// `json.dumps(snapshot_record("lesson", r))` from the oracle.
const LESSON_SNAPSHOT: &str = r#"{"store": "lesson", "key": "deploy-engine|approach", "entity_norm": "deploy-engine", "attribute_norm": "approach", "entity": "Deploy Engine", "attribute": "Approach", "value": "use blue/green é\"\n", "polarity": "+", "status": "current", "confidence": 0.9, "origin": "agent", "support": ["a", "b"], "provenance": ["signal:1", "signal:2"], "asserted_at": 1700000000.0, "last_confirmed": 1700000000.5, "supersedes_value": null, "superseded_by_value": null, "superseded_at": null, "tx_time": null, "valid_time": 1e+16, "hlc_phys": 1760000000000, "hlc_logical": 3, "writer_id": "w", "session_id": null, "version": 1, "about": "kubectl", "outcome": "success", "embedding": {"dtype": "float32", "shape": [3], "sha256": "ed21e3285b6d7a8d2f34ae3a076ccb7583f2a8c5d2b6d619cbaba5cfce970f0c"}}"#;
const LESSON_FINGERPRINT: &str = "0de88235651bcfba26a581874ff7ba37aee764f78b3bfd3f5e67fed113a21c48";

#[test]
fn lesson_snapshot_and_fingerprint_match_python() {
    let r = lesson();
    let snap = snapshot_record(Store::Lesson, &lesson_dict(&r), r.embedding.as_deref()).unwrap();
    let want: Value = serde_json::from_str(LESSON_SNAPSHOT).unwrap();
    assert_eq!(py_json(Value::Object(snap.clone())), py_json(want.clone()));
    assert_eq!(fingerprint(&snap), LESSON_FINGERPRINT);
    // The stored evidence (jsonb, read back as `json.loads` holds it)
    // fingerprints the same.
    let Value::Object(stored) = py_json(want) else {
        panic!("object");
    };
    assert_eq!(fingerprint(&stored), LESSON_FINGERPRINT);
}

#[test]
fn world_snapshot_takes_the_dataclass_defaults() {
    let r = WorldRecord {
        entity: "Rust".into(),
        attribute: "latest version".into(),
        value: "1.90".into(),
        polarity: "+".into(),
        confidence: 0.85,
        status: "current".into(),
        source_url: "https://a".into(),
        source_quote: String::new(),
        freshness_class: "volatile".into(),
        retrieved_at: 1e-05,
        content_hash: None,
        source_doc_id: None,
        asserted_at: 10.0,
        last_confirmed: 11.0,
        supersedes_value: None,
        superseded_by_value: None,
        superseded_at: None,
        embedding: None,
        tx_time: None,
        valid_time: None,
        hlc_phys: Some(5),
        hlc_logical: Some(0),
        writer_id: Some("w".into()),
        session_id: None,
        version: 1,
        key: slot_key("Rust", "latest version"),
        unit: None,
    };
    let snap = snapshot_record(Store::World, &world_dict(&r), None).unwrap();
    assert_eq!(snap["support"], json!(["source"]));
    assert_eq!(snap["provenance"], json!([]));
    assert_eq!(snap["origin"], json!("source"));
    assert_eq!(
        fingerprint(&snap),
        "d54046cfa598925411243b99dac48cd41b94aeec17ca620e193de8684ff87697"
    );
}

#[test]
fn restore_decision_snapshot_renames_lesson_fields_last() {
    let r = lesson();
    let snap = snapshot_record(Store::Lesson, &lesson_dict(&r), r.embedding.as_deref()).unwrap();
    let Value::Object(undo) = restore_decision_snapshot(Store::Lesson, snap, 42) else {
        panic!("object");
    };
    let keys: Vec<&str> = undo.keys().map(String::as_str).collect();
    assert_eq!(
        &keys[keys.len() - 6..],
        [
            "about",
            "outcome",
            "_curation_undo_of",
            "task",
            "aspect",
            "lesson"
        ]
    );
    assert!(!undo.contains_key("embedding") && !undo.contains_key("entity"));
    assert_eq!(undo["_curation_undo_of"], json!(42));
}

#[test]
fn retire_audit_without_curation_is_the_legacy_path() {
    assert!(pair_from_retire_audit(None).unwrap().is_none());
    assert!(pair_from_retire_audit(Some(&json!({}))).unwrap().is_none());
    assert!(
        pair_from_retire_audit(Some(&json!({"task": "t", "_curation_duplicate": null})))
            .unwrap()
            .is_none()
    );
    let bad_keep = json!({"_curation_duplicate": {"a": {}, "b": {}, "a_fingerprint": "x",
                                                  "b_fingerprint": "y", "keep": "c"}});
    assert!(pair_from_retire_audit(Some(&bad_keep)).unwrap().is_none());
    let no_side = json!({"_curation_duplicate": {"a": null, "b": {}, "a_fingerprint": "x",
                                                 "b_fingerprint": "y", "keep": "a"}});
    assert!(pair_from_retire_audit(Some(&no_side)).unwrap().is_none());
    let ok = json!({"_curation_duplicate": {"a": {"entity_norm": "x"}, "b": [], "a_fingerprint": 1,
                                            "b_fingerprint": "y", "keep": "b"}});
    let cur = pair_from_retire_audit(Some(&ok)).unwrap().unwrap();
    assert!(!cur.keep_a);
    assert_eq!(cur.sides().1["entity_norm"], json!("x"));
    // `(record or {}).get(...)` on a list raises.
    assert!(pair_from_retire_audit(Some(&json!([1]))).is_err());
}

#[test]
fn restore_result_names_its_source() {
    let r = restore_result(Store::Lesson, "t", None, 0, vec![], vec![], true);
    assert_eq!(
        serde_json::to_string(&r).unwrap(),
        r#"{"restored":0,"task":"t","aspect":null,"reason":"slot_live"}"#
    );
    let r = restore_result(
        Store::World,
        "e",
        Some("a"),
        1,
        vec![json!(1)],
        vec![json!(2)],
        false,
    );
    assert_eq!(
        serde_json::to_string(&r).unwrap(),
        r#"{"restored":2,"source":"mixed","entity":"e","attribute":"a","entries":[1,2]}"#
    );
    let r = restore_result(Store::World, "e", None, 0, vec![], vec![json!(2)], false);
    assert_eq!(r["source"], json!("audit_snapshot"));
}

#[test]
fn retired_slots_keep_the_newest_hundred_retires() {
    let d = |id: i64, action: &str, at: f64| Decision {
        id,
        entity_norm: "e".into(),
        attribute_norm: format!("a{id}"),
        action: action.into(),
        record: None,
        decided_at: at,
    };
    let mut rows = vec![
        d(1, "retire", 5.0),
        d(2, "restore", 9.0),
        d(3, "retire", 5.0),
    ];
    rows.extend((10..120).map(|i| d(i, "retire", 1.0)));
    let out = retired_only(rows);
    assert_eq!(out.len(), 100);
    assert_eq!((out[0].id, out[1].id, out[2].id), (3, 1, 119));
}

// ── the lesson snapshot-fallback prediction ──────────────────────────────

/// A lesson at `t|aspect`, as the oracle's `LessonRecord(entity="t",
/// attribute=aspect, status=..., superseded_at=..., asserted_at=1.0)`.
fn bare(aspect: &str, status: &str, superseded_at: Option<f64>) -> LessonRecord {
    LessonRecord {
        entity: "t".into(),
        attribute: aspect.into(),
        value: format!("t/{aspect}"),
        status: status.into(),
        superseded_at,
        asserted_at: 1.0,
        last_confirmed: 0.0,
        embedding: None,
        key: slot_key("t", aspect),
        ..lesson()
    }
}

fn store_of(records: Vec<LessonRecord>) -> LessonStore {
    LessonStore {
        records,
        dirty_slots: Default::default(),
    }
}

/// The latest decision at `t|aspect`, a retire; `record` false is NULL.
fn decided(id: i64, aspect: &str, at: f64, record: bool) -> Decision {
    Decision {
        id,
        entity_norm: "t".into(),
        attribute_norm: aspect.into(),
        action: "retire".into(),
        record: record.then(|| json!({"task": "t", "aspect": aspect})),
        decided_at: at,
    }
}

const NOW: f64 = 1000.0;

/// 99 retires without a record (never a fallback slot), the newest at 400.
fn recordless_retires() -> Vec<Decision> {
    (0..99)
        .map(|i| decided(10 + i, &format!("n{i:02}"), 400.0 - i as f64, false))
        .collect()
}

/// Expected values: `o1c_fallback_oracle.py`, which runs Python's own
/// `LessonStore.restore`, appends the restore rows `lesson_restore` writes
/// (`decided_at=now`, fresh ids), takes the latest decision per slot (the
/// `DISTINCT ON`), then `PostgresStorage.retired_slots`'s filter, sort and
/// cut and `lesson_restore`'s `snaps` comprehension; refusing is
/// `bool(snaps)`.
#[test]
fn lesson_fallback_prediction_matches_python() {
    let predict = |records: Vec<LessonRecord>, latest: Vec<Decision>, aspect: Option<&str>| {
        lesson_fallback_predicted(&store_of(records), latest, "t", aspect, NOW)
    };
    // A whole-task restore whose one retire row's slot was purged.
    assert!(predict(vec![], vec![decided(1, "a", 10.0, true)], None));
    // A future-dated retire row on the slot being restored stays its
    // latest decision, but the slot is live again: no fallback.
    assert!(!predict(
        vec![bare("x", "retired", Some(5.0))],
        vec![decided(1, "x", NOW + 500.0, true)],
        Some("x"),
    ));
    // ... and it keeps its place in the hundred, pushing a purged slot out.
    let mut rows = vec![decided(1, "x", NOW + 500.0, true)];
    rows.extend(recordless_retires());
    rows.push(decided(200, "p", 1.0, true));
    assert!(!predict(vec![bare("x", "retired", Some(5.0))], rows, None));
    // Large: the restored slot's (past) retire row turns into a restore row
    // before the cut, which lets the purged slot into the hundred.
    let mut rows = vec![decided(1, "x", 500.0, true)];
    rows.extend(recordless_retires());
    rows.push(decided(200, "p", 1.0, true));
    assert_eq!(rows.len(), 101);
    assert!(predict(vec![bare("x", "retired", Some(5.0))], rows, None));
    // Every retire row covered by a retired record: no refusal.
    assert!(!predict(
        vec![
            bare("x", "retired", Some(5.0)),
            bare("y", "retired", Some(6.0))
        ],
        vec![decided(1, "x", 10.0, true), decided(2, "y", 11.0, true)],
        None,
    ));
    // An aspect restore never sees another slot's purged row.
    assert!(!predict(
        vec![bare("x", "retired", Some(5.0))],
        vec![decided(1, "x", 10.0, true), decided(2, "y", 11.0, true)],
        Some("x"),
    ));
    // A decision dated exactly `now` loses to the restore row's newer id.
    assert!(!predict(
        vec![bare("x", "retired", Some(5.0))],
        vec![decided(1, "x", NOW, true)],
        Some("x"),
    ));
}

/// Mixed: one retired record and one purged slot. Python restores the first
/// and rewrites the second through `lesson_write`; the daemon refuses the
/// whole call, and the prediction only borrows the store, so nothing has
/// changed when it answers.
#[test]
fn mixed_lesson_restore_refuses_before_any_change() {
    let store = store_of(vec![bare("x", "retired", Some(5.0))]);
    let before = store.clone();
    let latest = vec![decided(1, "x", 10.0, true), decided(2, "y", 11.0, true)];
    assert!(lesson_fallback_predicted(&store, latest, "t", None, NOW));
    assert_eq!(store.records, before.records);
    assert_eq!(store.dirty_slots, before.dirty_slots);
    assert_eq!(store.records[0].status, "retired");
}

// ── the reconciliation of a failed curated restore ───────────────────────

/// The oracle's `rec(aspect, value, ...)` (`o1c_recovery_oracle.py`).
fn curated(aspect: &str, value: &str, status: &str, superseded_at: Option<f64>) -> LessonRecord {
    LessonRecord {
        entity: "Curate Keys".into(),
        attribute: aspect.into(),
        value: value.into(),
        about: Some("vault".into()),
        polarity: "+".into(),
        outcome: "success".into(),
        confidence: 0.8,
        status: status.into(),
        origin: Some("agent".into()),
        support: vec![],
        provenance: vec!["signal:1".into()],
        asserted_at: 100.0,
        last_confirmed: 100.0,
        supersedes_value: None,
        superseded_by_value: None,
        superseded_at,
        unit: None,
        embedding: None,
        tx_time: Some(100.0),
        valid_time: Some(100.0),
        hlc_phys: Some(100000),
        hlc_logical: Some(0),
        writer_id: Some("seed".into()),
        session_id: None,
        version: 1,
        key: slot_key("Curate Keys", aspect),
    }
}

const RETIRE_ID: i64 = 7;

fn survivor() -> LessonRecord {
    curated("Approach", "Rotate keys quarterly.", "current", None)
}

fn loser() -> LessonRecord {
    curated("Method", "Rotate keys every quarter.", "current", None)
}

fn snap_of(r: &LessonRecord) -> Map<String, Value> {
    snapshot_record(Store::Lesson, &lesson_dict(r), None).unwrap()
}

fn pair() -> Curated {
    Curated {
        a: snap_of(&survivor()),
        b: snap_of(&loser()),
        keep_a: true,
        curation: Map::new(),
    }
}

fn loser_decision(id: i64, action: &str, record: Option<Value>) -> Decision {
    Decision {
        id,
        entity_norm: "curate-keys".into(),
        attribute_norm: "method".into(),
        action: action.into(),
        record,
        decided_at: 600.0,
    }
}

fn restore_row(undo_of: Value) -> Decision {
    loser_decision(9, "restore", Some(json!({"_curation_undo_of": undo_of})))
}

fn retire_row(id: i64) -> Decision {
    loser_decision(id, "retire", Some(json!({"_curation_duplicate": {}})))
}

/// Expected values: `curation_safety._restore_recovery_state` itself
/// (`o1c_recovery_oracle.py`), over the same records and decision rows.
#[test]
fn restore_recovery_state_matches_python() {
    // The records are the oracle's: their evidence fingerprints agree.
    assert_eq!(
        fingerprint(&snap_of(&survivor())),
        "cfd60354527ff1950e8ef529b9d607fcd3200217c6faa14297f51a01605364d6"
    );
    assert_eq!(
        fingerprint(&snap_of(&loser())),
        "ffbdb3413159e83409abc23f5c2184526b78939d760deb3a4e19482bed983e20"
    );
    let classify =
        |records: Vec<LessonRecord>, latest: Option<Decision>| match restore_recovery_state(
            &Staged::Lesson(store_of(records)),
            &pair(),
            RETIRE_ID,
            latest.as_ref(),
        ) {
            Ok(Recovery::Committed) => "committed".to_string(),
            Ok(Recovery::RolledBack) => "rolled_back".to_string(),
            Err(e) => format!("error: {}", error_text(&e)),
        };
    let changed = "error: durable curation restore changed the survivor";
    let neither = "error: durable curation restore is neither before nor after the decision";
    let retired_loser = curated(
        "Method",
        "Rotate keys every quarter.",
        "retired",
        Some(500.0),
    );
    let other_survivor = curated("Approach", "Rotate keys yearly.", "current", None);
    let other_loser = curated("Method", "other", "current", None);
    let both = || vec![survivor(), loser()];
    let undo = |id: Value| Some(restore_row(id));
    let cases: Vec<(&str, Vec<LessonRecord>, Option<Decision>, &str)> = vec![
        ("committed", both(), undo(json!(RETIRE_ID)), "committed"),
        (
            "committed_float_undo_of",
            both(),
            undo(json!(7.0)),
            "committed",
        ),
        (
            "rolled_back",
            vec![survivor(), retired_loser],
            Some(retire_row(RETIRE_ID)),
            "rolled_back",
        ),
        (
            "rolled_back_purged_loser",
            vec![survivor()],
            Some(retire_row(RETIRE_ID)),
            "rolled_back",
        ),
        (
            "survivor_changed",
            vec![other_survivor, loser()],
            undo(json!(RETIRE_ID)),
            changed,
        ),
        (
            "survivor_missing",
            vec![loser()],
            undo(json!(RETIRE_ID)),
            changed,
        ),
        (
            "loser_back_latest_retire",
            both(),
            Some(retire_row(RETIRE_ID)),
            neither,
        ),
        ("loser_back_other_undo", both(), undo(json!(8)), neither),
        (
            "loser_back_changed",
            vec![survivor(), other_loser],
            undo(json!(RETIRE_ID)),
            neither,
        ),
        (
            "loser_gone_latest_restore",
            vec![survivor()],
            undo(json!(RETIRE_ID)),
            neither,
        ),
        (
            "loser_gone_other_retire",
            vec![survivor()],
            Some(retire_row(8)),
            neither,
        ),
        ("loser_gone_no_decision", vec![survivor()], None, neither),
        (
            "loser_back_null_record",
            both(),
            Some(loser_decision(9, "restore", None)),
            neither,
        ),
    ];
    for (name, records, latest, want) in cases {
        assert_eq!(classify(records, latest), want, "{name}");
    }
}
