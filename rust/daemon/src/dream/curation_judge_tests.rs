//! `curation_judge.rs` against Python (`harness/gen_queue_judge_goldens.py`):
//! the evidence identities, the safe-fold rule, the memo state, the policy
//! fingerprint, the prompt rows and the reply validation; `interleave`
//! against the schedule Python's closure produces.

use std::collections::HashSet;

use serde_json::{Map, Value, json};

use super::*;

const GOLDEN: &str = include_str!("../../harness/goldens/queue-judges.json");

fn golden() -> Value {
    serde_json::from_str(GOLDEN).expect("golden parses")
}

fn obj(v: &Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

fn norm(v: &Value) -> Value {
    match v {
        Value::Number(n) => json!(n.to_string().parse::<f64>().unwrap()),
        Value::Array(a) => Value::Array(a.iter().map(norm).collect()),
        Value::Object(m) => Value::Object(m.iter().map(|(k, v)| (k.clone(), norm(v))).collect()),
        other => other.clone(),
    }
}

fn evidence(case: &Value) -> PairEvidence {
    PairEvidence {
        store: if case["store"] == "world" {
            Store::World
        } else {
            Store::Lesson
        },
        a: obj(&case["a"]),
        b: obj(&case["b"]),
        a_fingerprint: case["a_fingerprint"].as_str().unwrap().into(),
        b_fingerprint: case["b_fingerprint"].as_str().unwrap().into(),
    }
}

#[test]
fn evidence_identities_are_pythons() {
    for case in golden()["curation_cases"].as_array().unwrap() {
        let ev = evidence(case);
        assert_eq!(evidence_fingerprint(&ev.a), ev.a_fingerprint, "{case}");
        assert_eq!(evidence_fingerprint(&ev.b), ev.b_fingerprint, "{case}");
        assert_eq!(pair_id(&ev), case["pair_id"].as_str().unwrap());
        assert_eq!(
            judgment_signature(&ev, case["policy"].as_str().unwrap()),
            case["signature"].as_str().unwrap()
        );
        let (a, b) = pair_keys(&ev);
        assert_eq!(json!([a, b]), case["pair_keys"]);
    }
}

#[test]
fn the_safe_fold_rule_is_pythons() {
    for case in golden()["curation_cases"].as_array().unwrap() {
        let ev = evidence(case);
        for f in case["folds"].as_array().unwrap() {
            let (allowed, reason) = can_auto_fold(&ev, &obj(&f["verdict"]));
            assert_eq!(allowed, f["allowed"].as_bool().unwrap(), "{f}");
            assert_eq!(reason, f["reason"].as_str().unwrap(), "{f}");
        }
    }
}

#[test]
fn the_memo_state_is_pythons() {
    for case in golden()["curation_cases"].as_array().unwrap() {
        let ev = evidence(case);
        let policy = case["policy"].as_str().unwrap();
        for s in case["states"].as_array().unwrap() {
            let judgment = s["judgment"].as_object();
            let bindings = obj(&s["bindings"]);
            let at = s["at"].as_f64().unwrap();
            assert_eq!(
                judgment_state(judgment, &bindings, &ev, policy, at, 500.0),
                s["state"].as_str().unwrap(),
                "{s}"
            );
            assert_eq!(
                bound_observed_model(&bindings, &ev).map(Value::String),
                s["observed"].as_str().map(|x| json!(x)),
                "{s}"
            );
            assert_eq!(
                bound_action(&bindings, &ev).as_deref(),
                s["action"].as_str(),
                "{s}"
            );
        }
    }
}

#[test]
fn the_policy_fingerprint_is_pythons() {
    let config = crate::config::Config::default();
    let mut extra = Map::new();
    extra.insert("z".into(), json!(1));
    extra.insert("a".into(), json!([1, 2]));
    let ex = super::super::extractor::Compat::new(
        "http://127.0.0.1:9/v1",
        "judge-one",
        None,
        4096,
        240.0,
        Some(extra),
    );
    for case in golden()["curation_policy"].as_array().unwrap() {
        assert_eq!(
            policy_fingerprint(&config, &ex, case["observed"].as_str()),
            case["fingerprint"].as_str().unwrap(),
            "{case}"
        );
    }
}

#[test]
fn slot_pairs_render_as_pythons_prompt_text() {
    for case in golden()["slot_format"].as_array().unwrap() {
        assert_eq!(
            format_slot_pair(&obj(&case["row"])),
            case["text"].as_str().unwrap(),
            "{case}"
        );
    }
}

#[test]
fn slot_verdicts_validate_as_pythons_judge_slot_pairs() {
    for case in golden()["slot_parse"].as_array().unwrap() {
        let known: HashSet<usize> = case["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["n"].as_u64().unwrap() as usize)
            .collect();
        let got = parse_slot_verdicts(case["raw"].as_array().unwrap(), &known).unwrap();
        let got = Value::Array(got.into_iter().map(Value::Object).collect());
        assert_eq!(norm(&got), norm(&case["verdicts"]), "{case}");
    }
}

#[test]
fn interleave_alternates_stores_with_pending_first() {
    use Store::{Lesson as L, World as W};
    let pending = [(L, "pl1"), (W, "pw1")];
    let stale = [(L, "sl1"), (L, "sl2"), (W, "sw1"), (L, "sl3")];
    let got: Vec<(bool, &str)> = interleave(&pending, &stale, [L, W])
        .into_iter()
        .map(|(p, _, t)| (p, t))
        .collect();
    assert_eq!(
        got,
        vec![
            (true, "pl1"),
            (true, "pw1"),
            (false, "sl1"),
            (false, "sw1"),
            (false, "sl2"),
            (false, "sl3"),
        ]
    );
    let got: Vec<&str> = interleave(&pending, &stale, [W, L])
        .into_iter()
        .map(|(_, _, t)| t)
        .collect();
    assert_eq!(got, vec!["pw1", "pl1", "sw1", "sl1", "sl2", "sl3"]);
    assert!(interleave::<&str>(&[], &[], [L, W]).is_empty());
}

#[test]
fn the_retire_audit_is_pythons() {
    for case in golden()["curation_audit"].as_array().unwrap() {
        let ev = evidence(case);
        let embedding: Vec<f32> = case["embedding"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_f64().unwrap() as f32)
            .collect();
        let got = audit_snapshot(
            ev.store,
            obj(&case["retired_snapshot"]),
            Some(embedding),
            &ev.b_fingerprint,
            &ev,
            "a",
        );
        assert_eq!(norm(&got), norm(&case["audit"]), "{}", case["store"]);
    }
}
