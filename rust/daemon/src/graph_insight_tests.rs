//! `graph_insight.rs` against Python (`harness/gen_graph_insight_goldens.py`):
//! byte equality with NetworkX's Louvain run without its shuffle (the
//! algorithm this port implements), and the Option B structural comparison
//! with NetworkX as the daemon runs it (`seed=42`).

use std::collections::{BTreeMap, HashMap, HashSet};

use serde_json::Value;

use super::*;

const GOLDEN: &str = include_str!("../harness/goldens/graph-insight.json");
const COMPUTED_AT: f64 = 1792000000.25;

fn cases() -> Vec<Value> {
    let v: Value = serde_json::from_str(GOLDEN).expect("golden parses");
    v["cases"].as_array().expect("cases").clone()
}

fn cfg(case: &Value) -> GraphInsightConfig {
    let mut c = GraphInsightConfig::default();
    let o = &case["cfg"];
    if let Some(x) = o["resolution"].as_f64() {
        c.resolution = x;
    }
    if let Some(x) = o["max_community_fraction"].as_f64() {
        c.max_community_fraction = x;
    }
    if let Some(x) = o["god_nodes_top_n"].as_i64() {
        c.god_nodes_top_n = x;
    }
    if let Some(x) = o["surprises_top_n"].as_i64() {
        c.surprises_top_n = x;
    }
    if let Some(x) = o["questions_top_n"].as_i64() {
        c.questions_top_n = x;
    }
    c
}

fn inputs(
    case: &Value,
) -> (
    Vec<InsightEntity>,
    Vec<InsightEdge>,
    BTreeMap<i64, i64>,
    Vec<Contested>,
) {
    let entities = case["entities"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| InsightEntity {
            id: e["id"].as_i64().unwrap(),
            display: e["display"].as_str().unwrap().to_string(),
        })
        .collect();
    let edges = case["edges"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| InsightEdge {
            src: e["src_id"].as_i64().unwrap(),
            dst: e["dst_id"].as_i64().unwrap(),
            relation: e["relation"].as_str().unwrap().to_string(),
            confidence: e["confidence"].as_f64().unwrap(),
            origin: e["origin"].as_str().map(str::to_string),
        })
        .collect();
    let prior = case["prior"]
        .as_object()
        .map(|m| {
            m.iter()
                .map(|(k, v)| (k.parse().unwrap(), v.as_i64().unwrap()))
                .collect()
        })
        .unwrap_or_default();
    let contested = case["contested"]
        .as_array()
        .map(|a| {
            a.iter()
                .map(|c| Contested {
                    entity: c["entity"].as_str().unwrap().into(),
                    attribute: c["attribute"].as_str().unwrap().into(),
                    value: c["value"].as_str().unwrap().into(),
                    contender_value: c["contender_value"].as_str().unwrap().into(),
                    contender_origin: c["contender_origin"].as_str().unwrap().into(),
                })
                .collect()
        })
        .unwrap_or_default();
    (entities, edges, prior, contested)
}

fn run(case: &Value) -> Insight {
    let (entities, edges, prior, contested) = inputs(case);
    compute(
        &cfg(case),
        &entities,
        &edges,
        &prior,
        &contested,
        COMPUTED_AT,
    )
}

/// Equal values, numbers compared by their value (the crate's
/// `arbitrary_precision` keeps each number's text, which differs between
/// Python's `repr` and Rust's shortest form only in spelling).
fn same(path: &str, a: &Value, b: &Value, out: &mut Vec<String>) {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            let (x, y): (f64, f64) = (
                x.to_string().parse().unwrap(),
                y.to_string().parse().unwrap(),
            );
            if x != y {
                out.push(format!("{path}: {x} != {y}"));
            }
        }
        (Value::Array(x), Value::Array(y)) => {
            if x.len() != y.len() {
                out.push(format!("{path}: {} items != {}", x.len(), y.len()));
            }
            for (i, (p, q)) in x.iter().zip(y).enumerate() {
                same(&format!("{path}[{i}]"), p, q, out);
            }
        }
        (Value::Object(x), Value::Object(y)) => {
            let kx: HashSet<&String> = x.keys().collect();
            let ky: HashSet<&String> = y.keys().collect();
            if kx != ky {
                out.push(format!("{path}: keys {kx:?} != {ky:?}"));
            }
            for (k, p) in x {
                if let Some(q) = y.get(k) {
                    same(&format!("{path}.{k}"), p, q, out);
                }
            }
        }
        _ => {
            if a != b {
                out.push(format!("{path}: {a} != {b}"));
            }
        }
    }
}

#[test]
fn every_case_matches_networkx_without_its_shuffle_byte_for_byte() {
    let mut failures = Vec::new();
    for case in cases() {
        let name = case["name"].as_str().unwrap();
        let got = run(&case);
        let want = &case["exact"];
        let mut diffs = Vec::new();
        same("digest", &got.digest, &want["digest"], &mut diffs);
        let summaries: Vec<Value> = got
            .summaries
            .iter()
            .map(|s| serde_json::json!({"id": s.id, "label": s.label, "size": s.size, "cohesion": s.cohesion}))
            .collect();
        same(
            "summaries",
            &Value::Array(summaries),
            &want["summaries"],
            &mut diffs,
        );
        let mut assignment = got.assignment.clone();
        assignment.sort_unstable();
        let assignment: Vec<Value> = assignment
            .iter()
            .map(|(e, c)| serde_json::json!([e, c]))
            .collect();
        same(
            "assignment",
            &Value::Array(assignment),
            &want["assignment"],
            &mut diffs,
        );
        if !diffs.is_empty() {
            failures.push(format!("{name}:\n  {}", diffs.join("\n  ")));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

// -- Option B: the structural comparison with Python's seeded run ----------

fn comb2(x: f64) -> f64 {
    x * (x - 1.0) / 2.0
}

/// Adjusted Rand index over the entities both assignments hold (the
/// generator's `ari`).
fn ari(a: &HashMap<i64, i64>, b: &HashMap<i64, i64>) -> f64 {
    let mut keys: Vec<i64> = a.keys().filter(|k| b.contains_key(k)).copied().collect();
    keys.sort_unstable();
    let n = keys.len() as f64;
    if keys.len() < 2 {
        return 1.0;
    }
    let mut table: HashMap<(i64, i64), f64> = HashMap::new();
    let mut rows: HashMap<i64, f64> = HashMap::new();
    let mut cols: HashMap<i64, f64> = HashMap::new();
    for k in &keys {
        *table.entry((a[k], b[k])).or_default() += 1.0;
        *rows.entry(a[k]).or_default() += 1.0;
        *cols.entry(b[k]).or_default() += 1.0;
    }
    let index: f64 = table.values().map(|&v| comb2(v)).sum();
    let sr: f64 = rows.values().map(|&v| comb2(v)).sum();
    let sc: f64 = cols.values().map(|&v| comb2(v)).sum();
    let expected = sr * sc / comb2(n);
    let maximum = (sr + sc) / 2.0;
    if maximum == expected {
        return 1.0;
    }
    (index - expected) / (maximum - expected)
}

fn jaccard<T: Eq + std::hash::Hash>(a: &HashSet<T>, b: &HashSet<T>) -> f64 {
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    a.intersection(b).count() as f64 / a.union(b).count() as f64
}

fn sizes_distance(a: &[i64], b: &[i64]) -> f64 {
    let (mut a, mut b) = (a.to_vec(), b.to_vec());
    a.sort_unstable_by(|x, y| y.cmp(x));
    b.sort_unstable_by(|x, y| y.cmp(x));
    let width = a.len().max(b.len());
    a.resize(width, 0);
    b.resize(width, 0);
    let total = a.iter().sum::<i64>().max(1) as f64;
    a.iter().zip(&b).map(|(x, y)| (x - y).abs()).sum::<i64>() as f64 / total
}

/// The partition-agreement measure, community count, size distribution,
/// god-node and surprise sets against Python's `seed=42` run must each be
/// inside the envelope Python spans across seeds 1..20 against seed 42.
/// The floor below is the agreement threshold the P2b PR states: on every
/// graph with planted structure the port reproduces the seeded partition
/// almost exactly; the envelope governs the structureless graphs, where
/// Louvain's answer depends on visiting order in Python too.
#[test]
fn every_case_agrees_with_seeded_networkx_within_its_own_seed_envelope() {
    const PLANTED_ARI_FLOOR: f64 = 0.95;
    let mut failures = Vec::new();
    for case in cases() {
        let name = case["name"].as_str().unwrap().to_string();
        let got = run(&case);
        let real = &case["real"];
        let env = &case["envelope"];
        let a: HashMap<i64, i64> = got.assignment.iter().copied().collect();
        let b: HashMap<i64, i64> = real["assignment"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| (p[0].as_i64().unwrap(), p[1].as_i64().unwrap()))
            .collect();
        let sa: Vec<i64> = got.summaries.iter().map(|s| s.size as i64).collect();
        let sb: Vec<i64> = real["sizes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_i64().unwrap())
            .collect();
        let gods: HashSet<i64> = got.digest["god_nodes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|g| g["entity_id"].as_i64().unwrap())
            .collect();
        let real_gods: HashSet<i64> = real["gods"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_i64().unwrap())
            .collect();
        let triple = |s: &Value, k: [&str; 3]| -> (String, String, String) {
            (
                s[k[0]].as_str().unwrap().into(),
                s[k[1]].as_str().unwrap().into(),
                s[k[2]].as_str().unwrap().into(),
            )
        };
        let surprises: HashSet<_> = got.digest["surprises"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| triple(s, ["src", "dst", "relation"]))
            .collect();
        let real_surprises: HashSet<_> = real["surprises"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| {
                (
                    s[0].as_str().unwrap().to_string(),
                    s[1].as_str().unwrap().to_string(),
                    s[2].as_str().unwrap().to_string(),
                )
            })
            .collect();
        let m_ari = ari(&a, &b);
        let checks = [
            ("ari", m_ari >= env["ari_min"].as_f64().unwrap() - 1e-12),
            (
                "count",
                (sa.len() as i64 - sb.len() as i64).abs()
                    <= env["count_delta_max"].as_i64().unwrap(),
            ),
            (
                "sizes",
                sizes_distance(&sa, &sb) <= env["sizes_distance_max"].as_f64().unwrap() + 1e-12,
            ),
            (
                "gods",
                jaccard(&gods, &real_gods) >= env["god_jaccard_min"].as_f64().unwrap() - 1e-12,
            ),
            (
                "surprises",
                jaccard(&surprises, &real_surprises)
                    >= env["surprise_jaccard_min"].as_f64().unwrap() - 1e-12,
            ),
            (
                "planted floor",
                !name.starts_with("planted-")
                    || name.ends_with("-split")
                    || m_ari >= PLANTED_ARI_FLOOR,
            ),
        ];
        for (what, ok) in checks {
            if !ok {
                failures.push(format!("{name}: {what} outside the envelope {env}"));
            }
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

// -- unit edges --------------------------------------------------------------

fn e(src: i64, dst: i64) -> InsightEdge {
    InsightEdge {
        src,
        dst,
        relation: "related_to".into(),
        confidence: 0.8,
        origin: None,
    }
}

#[test]
fn no_edges_means_no_communities() {
    assert!(detect_communities(&[], 1.0, 0.25).is_empty());
    assert!(betweenness(&WGraph::with_nodes(3)).is_empty());
}

#[test]
fn python_slices_and_the_one_surprise_a_zero_top_n_keeps() {
    assert_eq!(py_head(5, 3), 3);
    assert_eq!(py_head(5, 9), 5);
    assert_eq!(py_head(5, 0), 0);
    assert_eq!(py_head(5, -2), 3);
    assert_eq!(py_head(2, -7), 0);
    let edges = vec![InsightEdge {
        origin: Some("agent".into()),
        ..e(1, 2)
    }];
    let disp = HashMap::from([(1, "a".to_string()), (2, "b".to_string())]);
    assert_eq!(
        surprising_connections(&edges, &disp, &HashMap::new(), 0).len(),
        1
    );
}

#[test]
fn remap_keeps_old_ids_by_overlap_and_fills_gaps_for_new_communities() {
    let communities = vec![vec![1, 2, 3], vec![4, 5], vec![6]];
    let prior = BTreeMap::from([(1, 7), (2, 7), (4, 0), (9, 1)]);
    let got = remap_to_previous(&communities, &prior);
    assert_eq!(
        got,
        BTreeMap::from([(7, vec![1, 2, 3]), (0, vec![4, 5]), (1, vec![6])])
    );
}
