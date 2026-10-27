//! `candidates.rs` against Python (`harness/gen_queue_judge_goldens.py`):
//! the prompt row, the reply validation, and the scan generation and
//! pair-local evidence fingerprints over one synthetic bank snapshot.

use serde_json::{Value, json};

use super::*;

const GOLDEN: &str = include_str!("../../harness/goldens/queue-judges.json");

fn golden() -> Value {
    serde_json::from_str(GOLDEN).expect("golden parses")
}

fn row(v: &Value) -> Row {
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

#[test]
fn candidates_render_as_pythons_prompt_text() {
    for case in golden()["candidate_format"].as_array().unwrap() {
        assert_eq!(
            format_candidate(&row(&case["row"])),
            case["text"].as_str().unwrap(),
            "{case}"
        );
    }
}

#[test]
fn candidate_verdicts_validate_as_pythons_judge_candidates() {
    for case in golden()["candidate_parse"].as_array().unwrap() {
        let rows: Vec<Row> = case["rows"].as_array().unwrap().iter().map(row).collect();
        let got = parse_candidate_verdicts(case["raw"].as_array().unwrap(), &rows).unwrap();
        let got = Value::Array(got.into_iter().map(Value::Object).collect());
        assert_eq!(norm(&got), norm(&case["verdicts"]), "{case}");
    }
}

fn pairs_of<K, T>(v: &Value, key: impl Fn(&Value) -> K, val: impl Fn(&Value) -> T) -> Vec<(K, T)> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|p| (key(&p[0]), val(&p[1])))
        .collect()
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap().to_string())
        .collect()
}

fn inputs(v: &Value) -> CandInputs {
    let rows = |k: &str| -> Vec<Row> { v[k].as_array().unwrap().iter().map(row).collect() };
    CandInputs {
        entities: rows("entities"),
        aliases: pairs_of(&v["aliases"], |k| k.as_i64().unwrap(), strings),
        edges: rows("edges"),
        entries: v["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| CEntry {
                id: e["id"].as_i64().unwrap(),
                text: e["text"].clone(),
                embedding: e["embedding"]
                    .as_array()
                    .map(|xs| xs.iter().map(|x| x.as_f64().unwrap() as f32).collect()),
            })
            .collect(),
        scopes: pairs_of(&v["scopes"], |k| k.as_i64().unwrap(), strings),
        traces: pairs_of(
            &v["traces"],
            |k| k.as_str().unwrap().to_string(),
            |ids| {
                ids.as_array()
                    .unwrap()
                    .iter()
                    .map(|i| i.as_i64().unwrap())
                    .collect()
            },
        ),
        facts: pairs_of(
            &v["facts"],
            |k| k.as_i64().unwrap(),
            |n| n.as_i64().unwrap(),
        ),
        fact_texts: pairs_of(
            &v["fact_texts"],
            |k| k.as_str().unwrap().to_string(),
            |n| n.as_i64().unwrap(),
        ),
        pending_entities: rows("pending_entities"),
        lesson_refs: v["lesson_refs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|i| i.as_i64().unwrap())
            .collect(),
        pending_links: rows("pending_links"),
        dismissed: v["dismissed"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| {
                (
                    p[0].as_str().unwrap().to_string(),
                    p[1].as_str().unwrap().to_string(),
                )
            })
            .collect(),
    }
}

#[test]
fn the_scan_generation_is_pythons_fingerprint() {
    let g = golden();
    let case = &g["candidate_fingerprints"];
    let inputs = inputs(&case["inputs"]);
    let cfg = crate::config::DeepDreamConfig::default();
    assert_eq!(
        generation(&inputs, &cfg, true),
        case["generation"].as_str().unwrap()
    );
    assert_eq!(
        generation(&inputs, &cfg, false),
        case["decision_generation"].as_str().unwrap()
    );
}

#[test]
fn pair_local_evidence_is_pythons_fingerprint() {
    let g = golden();
    let case = &g["candidate_fingerprints"];
    let inputs = inputs(&case["inputs"]);
    let candidates: Vec<Row> = case["candidates"]
        .as_array()
        .unwrap()
        .iter()
        .map(row)
        .collect();
    let want: Vec<String> = strings(&case["evidence"]);
    assert_eq!(evidence_fingerprints(&inputs, &candidates), want);
}

#[test]
fn an_int_keyed_dict_sorts_by_its_integers() {
    let fp = Fp::IntObj(vec![(10, Fp::V(json!(1))), (2, Fp::V(json!(2)))]);
    let mut text = String::new();
    write_fp(&mut text, &fp);
    assert_eq!(text, r#"{"2":2,"10":1}"#);
}
