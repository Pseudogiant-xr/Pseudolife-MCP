//! Pure pieces of the review actions. Every expected value was produced by
//! the Python oracle (`python -c` from the repo root, CPython 3.11).

use super::*;

fn dumps(v: &Value) -> String {
    serde_json::to_string(v).unwrap()
}

#[test]
fn edge_confidence_and_violations_match_relation_quality() {
    // `is_hard_type_violation(*c), edge_confidence(*c)`.
    for (src, rel, dst, violation, conf) in [
        ("llama-server", "runs-on", "windows", false, 0.7),
        ("llama-server", "runs-on", "postgres", true, 0.175),
        ("foo", "related-to", "bar", false, 0.45),
        ("foo", "uses", "bar", false, 0.7),
        ("postgres", "runs-on", "docker", false, 0.7),
        ("config.yaml", "part-of", "memory_store", true, 0.175),
        ("api-service", "part-of", "gateway", false, 0.7),
        ("11", "hosts", "x-service", true, 0.175),
        ("docker", "hosts", "redis", false, 0.7),
        ("feat/x", "runs-on", "linux", true, 0.175),
        ("a.py", "stores-data-in", "b.json", true, 0.175),
        ("pg", "related-to", "4090", false, 0.45),
        ("  POSTGRES ", "stores-data-in", "WINDOWS 11", true, 0.175),
        ("memory_store", "stores-data-in", "user", true, 0.175),
    ] {
        assert_eq!(
            is_hard_type_violation(src, rel, dst),
            violation,
            "{src} {rel} {dst}"
        );
        assert_eq!(edge_confidence(src, rel, dst), conf, "{src} {rel} {dst}");
    }
}

#[test]
fn infer_type_matches_relation_quality() {
    for (name, want) in [
        ("docker compose up", Some("concept")),
        ("v8", Some("concept")),
        ("0.2.0", Some("concept")),
        ("\u{663}", Some("concept")),
        ("schema.sql", Some("concept")),
        ("main", Some("concept")),
        ("docs/guide.md", Some("file")),
        ("feat/lme", Some("concept")),
        ("user", Some("person")),
        ("The User", Some("person")),
        ("docker-desktop", Some("runtime")),
        ("windows box", Some("runtime")),
        ("redis-db", Some("datastore")),
        ("my database", Some("datastore")),
        ("mem-daemon", Some("service")),
        ("x-worker", Some("service")),
        ("gateway", Some("service")),
        ("do_thing", Some("tool")),
        ("f()", Some("tool")),
        ("Foo", None),
        ("", None),
        ("  ", None),
        ("\u{3a3}\u{391}\u{3a3}_x", None),
        ("abc", None),
        ("hosts", None),
        ("sidecar-x", Some("service")),
        ("fix/", None),
        ("fix/a\nb", Some("concept")),
    ] {
        assert_eq!(infer_type(name), want, "{name:?}");
    }
}

#[test]
fn coerce_id_list_matches_mcp_server() {
    let ints = |v: Option<Vec<Value>>| v.map(|v| dumps(&Value::Array(v)));
    for (input, want) in [
        (json!(null), None),
        (json!([1, 2]), Some("[1,2]")),
        (json!("[3, 4]"), Some("[3,4]")),
        (json!("[]"), Some("[]")),
        (json!("x"), None),
        (json!("5"), None),
        (json!("[\"6\", 7.9]"), Some("[6,7]")),
        (json!([true]), Some("[1]")),
        (json!("{\"a\":1}"), None),
        (json!([[1]]), None),
        (json!("[1.5e0]"), Some("[1]")),
        (json!("[null]"), None),
        (json!([" -0_1 "]), Some("[-1]")),
        (json!(["1__0"]), None),
    ] {
        assert_eq!(
            ints(coerce_id_list(Some(&input))),
            want.map(str::to_string),
            "{input}"
        );
    }
    assert_eq!(coerce_id_list(None), None);
}

#[test]
fn decision_index_normalizes_like_review_decisions() {
    // `json.dumps(review_decisions._index(storage))` per stored value.
    let fresh = r#"{"version":1,"next_seq":1,"cursors":{},"active":{},"recent_ids":[]}"#;
    for (stored, want) in [
        (None, fresh),
        (Some(json!([1])), fresh),
        (Some(json!({"version": 2, "next_seq": 9})), fresh),
        (Some(json!({"version": "1"})), fresh),
        (Some(json!({"version": 1})), fresh),
        (
            Some(json!({"version": 1.0, "next_seq": 0, "cursors": null,
                "active": {"merge:1": {"decision_id": "1"}}, "recent_ids": ["a", "b"],
                "extra": 5})),
            r#"{"version":1,"next_seq":1,"cursors":{},"active":{"merge:1":{"decision_id":"1"}},"recent_ids":["a","b"]}"#,
        ),
        (
            Some(
                json!({"version": true, "next_seq": 7.9, "cursors": [["c", 3]],
                "active": [], "recent_ids": "xy"}),
            ),
            r#"{"version":1,"next_seq":7,"cursors":{"c":3},"active":{},"recent_ids":["x","y"]}"#,
        ),
        (
            Some(json!({"version": 1, "next_seq": "12"})),
            r#"{"version":1,"next_seq":12,"cursors":{},"active":{},"recent_ids":[]}"#,
        ),
        (Some(json!({"version": 1, "next_seq": -4})), fresh),
        (
            Some(json!({"version": 1, "active": [[1.5, 2], [true, 3], [null, 4]]})),
            r#"{"version":1,"next_seq":1,"cursors":{},"active":{"1.5":2,"true":3,"null":4},"recent_ids":[]}"#,
        ),
    ] {
        let got = Index::from_stored(stored.clone()).unwrap().to_value();
        assert_eq!(dumps(&got), want, "{stored:?}");
    }
    for (stored, message) in [
        (
            json!({"version": 1, "next_seq": null}),
            "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'",
        ),
        (
            json!({"version": 1, "cursors": "ab"}),
            "dictionary update sequence element #0 has length 1; 2 is required",
        ),
        (
            json!({"version": 1, "active": 5}),
            "'int' object is not iterable",
        ),
    ] {
        assert_eq!(
            Index::from_stored(Some(stored)).unwrap_err(),
            message.to_string()
        );
    }
    let many: Vec<String> = (0..105).map(|i| i.to_string()).collect();
    let index = Index::from_stored(Some(json!({"version": 1, "recent_ids": many}))).unwrap();
    assert_eq!(index.recent_ids.len(), 100);
    assert_eq!(index.recent_ids[0], json!("5"));
}

#[test]
fn touch_recent_moves_the_id_last_and_caps() {
    let mut index = Index::fresh();
    index.recent_ids = vec![json!("1"), json!("2"), json!("3")];
    index.touch_recent(&json!("2"));
    assert_eq!(index.recent_ids, vec![json!("1"), json!("3"), json!("2")]);
    index.recent_ids = (0..100).map(|i| json!(i.to_string())).collect();
    index.touch_recent(&json!("x"));
    assert_eq!(index.recent_ids.len(), 100);
    assert_eq!(index.recent_ids[0], json!("1"));
    assert_eq!(index.recent_ids[99], json!("x"));
}

#[test]
fn proposal_votes_match_judge_agreement() {
    let s = |v: &str| Some(v.to_string());
    let got = proposal_votes(
        Some("relate:uses x | 2nd (m2): relate:depends-on y"),
        [
            Opinion {
                verdict: s("relate"),
                confidence: Some(0.85),
                model: s("m1"),
                served: s("s1"),
            },
            Opinion {
                verdict: s("accept"),
                confidence: None,
                model: None,
                served: s(""),
            },
            Opinion {
                verdict: s(""),
                confidence: Some(1.0),
                model: s("m3"),
                served: None,
            },
        ],
    );
    assert_eq!(
        dumps(&got.unwrap()),
        r#"{"judge":{"verdict":"relate","confidence":0.85,"model":"m1","served_model":"s1","relation":"uses"},"judge2":{"verdict":"accept","confidence":0.0,"model":null,"relation":null}}"#
    );
    let got = proposal_votes(
        Some("accept | 2nd (m2): relate:depends-on y"),
        [
            Opinion::default(),
            Opinion {
                verdict: s("relate"),
                confidence: Some(1.0),
                model: s("m2"),
                served: None,
            },
            Opinion::default(),
        ],
    );
    assert_eq!(
        dumps(&got.unwrap()),
        r#"{"judge2":{"verdict":"relate","confidence":1.0,"model":"m2","relation":"depends-on"}}"#
    );
    assert_eq!(proposal_votes(None, Default::default()), None);
}

#[test]
fn noted_adds_only_listed_reasons_and_keeps_a_note() {
    let pid = Pid::new(json!(3));
    assert_eq!(
        dumps(&noted(not_pending("accepted", &pid))),
        r#"{"accepted":false,"reason":"not_pending","id":3,"note":"Nothing settled: no pending proposal has this id (already decided or unknown); list again for current ids."}"#
    );
    let stale = json!({"rejected": false, "id": 3});
    assert_eq!(noted(stale.clone()), stale);
    let kept =
        json!({"dismissed": false, "reason": "bad_pair", "a": "x", "b": "x", "note": "keep"});
    assert_eq!(noted(kept.clone()), kept);
}

#[test]
fn batch_counts_truthy_accepted_or_rejected() {
    let results = vec![
        json!({"accepted": true, "from": "a", "into": "b"}),
        json!({"accepted": false, "reason": "not_pending", "id": 2}),
        json!({"rejected": false, "id": 3}),
        json!({"rejected": true, "id": 4}),
    ];
    assert_eq!(
        dumps(&batch_answer("accept_merge", results)),
        r#"{"action":"accept_merge","settled":2,"results":[{"accepted":true,"from":"a","into":"b"},{"accepted":false,"reason":"not_pending","id":2},{"rejected":false,"id":3},{"rejected":true,"id":4}]}"#
    );
}

#[test]
fn plan_refuses_like_mcp_server() {
    let args = |v: Value| v.as_object().unwrap().clone();
    let refusal = |action: &str, v: Value| dumps(&plan(action, &args(v)).unwrap_err());
    assert_eq!(
        refusal("propose", json!({"proposals": []})),
        r#"{"error":"proposals_required"}"#
    );
    assert_eq!(
        refusal("relate", json!({"src": "a", "dst": "b", "relation": ""})),
        r#"{"error":"src_relation_dst_required"}"#
    );
    assert_eq!(
        refusal("dismiss_pair", json!({"src": "a", "dst": null})),
        r#"{"error":"src_dst_required"}"#
    );
    // An empty batch falls through to `proposal_id`.
    assert_eq!(
        refusal(
            "reject_entity",
            json!({"proposal_ids": [], "proposal_id": null})
        ),
        r#"{"error":"proposal_id_required","action":"reject_entity"}"#
    );
    let Ok(Plan::Ids { pids, batch, .. }) = plan(
        "accept_link",
        &args(json!({"proposal_ids": "[2, 3]", "proposal_id": 9})),
    ) else {
        panic!("not an id plan");
    };
    assert!(batch);
    assert_eq!(pids.iter().map(Pid::text).collect::<Vec<_>>(), ["2", "3"]);
}

#[test]
fn bound_parameters_follow_postgres_casts() {
    // PostgreSQL `float8out` (shortest digits, exponent below 1e-4 and from 1e15).
    for (x, want) in [
        (0.5, "0.5"),
        (100.0, "100"),
        (1e15, "1e+15"),
        (123456789012345.0, "123456789012345"),
        (1e-5, "1e-05"),
        (0.0001, "0.0001"),
        (-2.5e-7, "-2.5e-07"),
        (1.5e300, "1.5e+300"),
    ] {
        assert_eq!(pg_float8_text(x), want);
    }
    assert_eq!(real_param(Some(&json!(0.25))).unwrap(), Some(0.25));
    assert_eq!(real_param(Some(&json!(" 0.5 "))).unwrap(), Some(0.5));
    assert!(real_param(Some(&json!(1e39))).is_err());
    assert!(real_param(Some(&json!(true))).is_err());
    assert_eq!(text_param(Some(&json!(7))).unwrap().as_deref(), Some("7"));
    assert_eq!(
        text_param(Some(&json!(false))).unwrap().as_deref(),
        Some("false")
    );
}
