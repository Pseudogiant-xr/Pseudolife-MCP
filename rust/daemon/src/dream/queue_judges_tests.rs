//! `queue_judges.rs` against Python (`harness/gen_queue_judge_goldens.py`):
//! the junk prompt row, the reply validation and the evidence pack.

use std::collections::{BTreeMap, HashMap, HashSet};

use serde_json::{Map, Value, json};

use super::*;

const GOLDEN: &str = include_str!("../../harness/goldens/queue-judges.json");

fn golden() -> Value {
    serde_json::from_str(GOLDEN).expect("golden parses")
}

fn row(v: &Value) -> Row {
    v.as_object().cloned().unwrap_or_default()
}

/// Numbers compared by value (`arbitrary_precision` keeps their text).
fn norm(v: &Value) -> Value {
    match v {
        Value::Number(n) => json!(n.to_string().parse::<f64>().unwrap()),
        Value::Array(a) => Value::Array(a.iter().map(norm).collect()),
        Value::Object(m) => Value::Object(m.iter().map(|(k, v)| (k.clone(), norm(v))).collect()),
        other => other.clone(),
    }
}

#[test]
fn junk_rows_render_as_pythons_prompt_text() {
    for case in golden()["format"].as_array().unwrap() {
        assert_eq!(
            format_junk_proposal(&row(&case["row"])),
            case["text"].as_str().unwrap(),
            "{case}"
        );
    }
}

#[test]
fn junk_verdicts_validate_as_pythons_judge_junk() {
    for case in golden()["parse"].as_array().unwrap() {
        let known: HashSet<usize> = case["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["n"].as_u64().unwrap() as usize)
            .collect();
        let got: Vec<Value> = parse_junk_verdicts(case["raw"].as_array().unwrap(), &known)
            .unwrap()
            .into_iter()
            .map(|v| {
                json!({"n": v.n, "verdict": v.verdict, "confidence": v.confidence, "note": v.note})
            })
            .collect();
        assert_eq!(norm(&Value::Array(got)), norm(&case["verdicts"]), "{case}");
    }
}

fn evidence(v: &Value) -> JunkEvidence {
    let g = &v["graph"];
    let s = |x: &Value| x.as_str().unwrap_or_default().to_string();
    let int_map = |m: &Value| -> HashMap<i64, i64> {
        m.as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.parse().unwrap(), v.as_i64().unwrap()))
            .collect()
    };
    JunkEvidence {
        entities: g["entities"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| {
                (
                    e["id"].as_i64().unwrap(),
                    s(&e["canonical"]),
                    s(&e["display"]),
                )
            })
            .collect(),
        edges: g["edges"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| JEdge {
                src: e["src_id"].as_i64().unwrap(),
                dst: e["dst_id"].as_i64().unwrap(),
                relation: s(&e["relation"]),
                origin: e["origin"].as_str().map(str::to_string),
            })
            .collect(),
        scopes: v["scopes"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| {
                (
                    k.parse().unwrap(),
                    v.as_array().unwrap().iter().map(s).collect(),
                )
            })
            .collect::<BTreeMap<_, _>>(),
        entries: v["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| (e["source"].as_str().map(str::to_string), s(&e["text"])))
            .collect(),
        facts: int_map(&v["facts"]),
        fact_texts: v["fact_texts"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, n)| (k.clone(), n.as_i64().unwrap()))
            .collect(),
        fact_rows: v["fact_rows"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, rows)| {
                (
                    k.parse().unwrap(),
                    rows.as_array()
                        .unwrap()
                        .iter()
                        .map(|p| (s(&p[0]), s(&p[1])))
                        .collect(),
                )
            })
            .collect(),
    }
}

#[test]
fn junk_packs_match_pythons_enrichment() {
    for case in golden()["pack"].as_array().unwrap() {
        let cfg = crate::config::DeepDreamConfig {
            snippet_max_chars: case["cap"].as_i64().unwrap(),
            ..Default::default()
        };
        let pending: Vec<Row> = case["pending"]
            .as_array()
            .unwrap()
            .iter()
            .map(row)
            .collect();
        let got = enrich_junk_from(&pending, &evidence(&case["evidence"]), &cfg);
        assert_eq!(
            norm(&Value::Array(got)),
            norm(&case["rows"]),
            "cap {}",
            case["cap"]
        );
    }
}

#[test]
fn the_sweep_logs_a_queue_judge_only_when_it_did_something() {
    let quiet: Map<String, Value> = json!({"judged": 0, "reconsideration": {"reopened": 0}})
        .as_object()
        .cloned()
        .unwrap();
    assert!(!worth_logging(&quiet));
    for key in ["judged", "applied", "error", "second_opinion_error"] {
        let mut m = quiet.clone();
        m.insert(key.into(), json!(1));
        assert!(worth_logging(&m), "{key}");
    }
    let mut m = quiet.clone();
    m.insert("reconsideration".into(), json!({"reopened": 2}));
    assert!(worth_logging(&m));
}
