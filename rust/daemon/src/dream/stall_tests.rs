//! `StallTracker` and the stall notice text against Python's
//! `DreamStallTracker` and `dream_stall_notice_text`
//! (`harness/gen_stall_tracker_goldens.py`): every scripted sequence
//! replayed on the same clock, the answer and the snapshot compared after
//! each step.

use serde_json::{Map, Value};

use super::{PrimaryProbe, StallTracker, minute_at_offset, stall_notice_text_with};

const GOLDEN: &str = include_str!("../../harness/goldens/stall-tracker.json");

fn golden() -> Value {
    serde_json::from_str(GOLDEN).expect("golden parses")
}

/// Numbers compared by value (`arbitrary_precision` keeps their text).
fn norm(v: &Value) -> Value {
    match v {
        Value::Number(n) => {
            let f: f64 = n.to_string().parse().unwrap();
            serde_json::json!(f)
        }
        Value::Array(a) => Value::Array(a.iter().map(norm).collect()),
        Value::Object(m) => Value::Object(m.iter().map(|(k, v)| (k.clone(), norm(v))).collect()),
        other => other.clone(),
    }
}

fn obj(v: &Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

#[test]
fn every_sequence_matches_python_step_by_step() {
    let mut failures = Vec::new();
    for (name, steps) in golden()["sequences"].as_object().unwrap() {
        let mut t = StallTracker::default();
        for (i, step) in steps.as_array().unwrap().iter().enumerate() {
            let now = step["now"].as_f64().unwrap();
            let args = step["args"].as_array().unwrap();
            let got = match step["op"].as_str().unwrap() {
                "record" => {
                    let probe = args.get(2).filter(|p| !p.is_null()).map(|p| PrimaryProbe {
                        reason: p["reason"].as_str().unwrap().to_string(),
                        error: p["error"].as_str().unwrap().to_string(),
                        fallback_resolves: p["fallback_resolves"].as_bool().unwrap(),
                    });
                    t.record_at(
                        &obj(&args[0]),
                        args[1].as_bool().unwrap(),
                        probe.as_ref(),
                        now,
                    );
                    Value::Null
                }
                "overdue" => {
                    t.check_overdue_at(
                        args[0].as_i64().unwrap(),
                        args[1].as_i64().unwrap(),
                        args[2].as_bool().unwrap(),
                        args[3].as_f64().unwrap(),
                        now,
                    );
                    Value::Null
                }
                "take" => match t.take_notice_at(args[0].as_f64().unwrap(), now) {
                    Some((kind, record)) => serde_json::json!([kind, record]),
                    None => Value::Null,
                },
                "mark" => {
                    t.mark_noticed_at(args[0].as_str().unwrap(), now);
                    Value::Null
                }
                other => panic!("unknown op {other}"),
            };
            let (stall, last_stall) = t.snapshot();
            let snap = serde_json::json!({"stall": stall, "last_stall": last_stall});
            if norm(&got) != norm(&step["got"]) {
                failures.push(format!("{name}[{i}] answer: {got} != {}", step["got"]));
            }
            if norm(&snap) != norm(&step["snapshot"]) {
                failures.push(format!(
                    "{name}[{i}] snapshot: {snap} != {}",
                    step["snapshot"]
                ));
            }
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn every_notice_text_matches_python_at_fixed_offsets() {
    for case in golden()["texts"].as_array().unwrap() {
        let offset = i32::try_from(case["offset"].as_i64().unwrap()).unwrap();
        let text = stall_notice_text_with(
            case["kind"].as_str().unwrap(),
            &obj(&case["record"]),
            |ts| minute_at_offset(ts, offset),
        );
        assert_eq!(text, case["text"].as_str().unwrap(), "{case}");
    }
}
