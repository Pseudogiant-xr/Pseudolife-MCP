//! Goldens from `memory/graph_consolidation.py` on CPython 3.11 with numpy
//! (`harness/gen_w2e_deep_dream_goldens.py`).

use super::*;
use crate::openblas_sdot::Kernel;
use sha2::{Digest, Sha256};

const GOLDEN: &str = include_str!("../harness/goldens/w2e-deep-dream.json");

fn golden() -> Value {
    serde_json::from_str(GOLDEN).unwrap()
}

fn f32s(v: &Value) -> Vec<f32> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|h| {
            let b = hex::decode(h.as_str().unwrap()).unwrap();
            f32::from_le_bytes([b[0], b[1], b[2], b[3]])
        })
        .collect()
}

fn digest(v: &[f32]) -> String {
    let mut h = Sha256::new();
    for x in v {
        h.update(x.to_le_bytes());
    }
    hex::encode(h.finalize())
}

/// The generator's `synthetic(i, dim)`.
fn synthetic(i: u64, dim: u64) -> Vec<f32> {
    (0..dim)
        .map(|j| {
            let h = (i * 2654435761 + j * 40503 + 12345) % (1 << 32);
            ((h % 20011) as i64 - 10005) as f32 / 4096.0
        })
        .collect()
}

fn entity(v: &Value) -> Entity {
    Entity {
        id: v["id"].as_i64().unwrap(),
        canonical: v["canonical"].as_str().unwrap_or_default().to_string(),
        display: v["display"].as_str().unwrap().to_string(),
    }
}

fn edge(v: &Value) -> GEdge {
    GEdge {
        id: 0,
        src_id: v["src_id"].as_i64().unwrap(),
        relation: v["relation"].as_str().unwrap_or("related-to").to_string(),
        dst_id: v["dst_id"].as_i64().unwrap(),
        confidence: 0.8,
        origin: None,
    }
}

fn deg(edges: &[GEdge]) -> BTreeMap<i64, usize> {
    let e: Vec<_> = edges.iter().map(GEdge::review_edge).collect();
    crate::graph_review::degree_counts(&e)
}

fn candidate_json(c: &Candidate) -> Value {
    Value::Object(c.to_map())
}

#[test]
fn set_order_matches_cpython() {
    for case in golden()["set_order"].as_array().unwrap() {
        let items: Vec<i64> = serde_json::from_value(case[0].clone()).unwrap();
        let want: Vec<i64> = serde_json::from_value(case[1].clone()).unwrap();
        assert_eq!(py_int_set_order(items.clone()), want, "{items:?}");
    }
}

/// Run `f` once per sdot kernel with that kernel forced and its goldens.
fn per_kernel(f: impl Fn(&Value)) {
    let g = golden();
    for k in Kernel::ALL {
        crate::openblas_sdot::with_kernel(k, || f(&g["kernels"][k.name()]));
    }
}

#[test]
fn context_vectors_and_cosines_are_numpy_bit_exact() {
    per_kernel(context_vectors_case);
}

fn context_vectors_case(g: &Value) {
    let kernel = crate::openblas_sdot::active();
    for case in g["numeric"].as_array().unwrap() {
        let dim = case["dim"].as_u64().unwrap();
        let entries: Vec<DEntry> = case["entry_ids"]
            .as_array()
            .unwrap()
            .iter()
            .enumerate()
            .map(|(n, id)| DEntry {
                id: id.as_i64().unwrap(),
                text: format!("note {}", n + 1),
                source: None,
                embedding: Some(synthetic(n as u64 + 1, dim)),
            })
            .collect();
        let entities: Vec<Entity> = (1..=8)
            .map(|k| Entity {
                id: k,
                canonical: format!("e{k}"),
                display: format!("Entity {k}"),
            })
            .collect();
        let traces: HashMap<String, Vec<i64>> =
            serde_json::from_value(case["traces"].clone()).unwrap();
        let tokens: Vec<(i64, BTreeSet<String>)> =
            entries.iter().map(|e| (e.id, token_set(&e.text))).collect();
        let ctx = entity_context_vectors(&entities, &entries, &tokens, &traces, 2, None).unwrap();
        let want: BTreeMap<String, String> =
            serde_json::from_value(case["vectors"].clone()).unwrap();
        let got: BTreeMap<String, String> = ctx
            .vectors
            .iter()
            .map(|(k, v)| (k.to_string(), digest(v)))
            .collect();
        assert_eq!(got, want, "{kernel:?} dim {dim} vectors");
        let mentions: BTreeMap<String, Vec<i64>> =
            serde_json::from_value(case["mentions"].clone()).unwrap();
        let got: BTreeMap<String, Vec<i64>> = ctx
            .mentions
            .iter()
            .map(|(k, v)| (k.to_string(), v.iter().copied().collect()))
            .collect();
        assert_eq!(got, mentions);
        for (pair, bits) in case["cosines"].as_object().unwrap() {
            let (a, b) = pair.split_once('-').unwrap();
            let c = sdot(
                &ctx.vectors[&a.parse::<i64>().unwrap()],
                &ctx.vectors[&b.parse::<i64>().unwrap()],
            );
            assert_eq!(
                hex::encode(c.to_le_bytes()),
                bits.as_str().unwrap(),
                "{kernel:?} dim {dim} {pair}"
            );
        }
        let none = HashSet::new();
        let pairs = HashSet::new();
        let dismissed = HashSet::new();
        let near = candidate_pairs(
            &ctx,
            &[],
            &entities,
            &BTreeMap::new(),
            &[],
            &CandidateOpts {
                min_similarity: -1.0,
                top_k: 12,
                dismissed: &dismissed,
                max_support_overlap: 2.0,
                pending_pairs: &pairs,
                excluded_ids: &none,
            },
        );
        let got: Vec<Value> = near.iter().map(candidate_json).collect();
        assert_eq!(
            Value::Array(got),
            case["candidates"],
            "{kernel:?} dim {dim} candidates"
        );
    }
}

#[test]
fn candidate_filters_and_partition_match_python() {
    per_kernel(partition_case);
}

fn partition_case(g: &Value) {
    let p = &g["partition"];
    let entities: Vec<Entity> = p["entities"]
        .as_array()
        .unwrap()
        .iter()
        .map(entity)
        .collect();
    let edges: Vec<GEdge> = p["edges"].as_array().unwrap().iter().map(edge).collect();
    let vectors: HashMap<i64, Vec<f32>> = p["vectors"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.parse().unwrap(), f32s(v)))
        .collect();
    let mentions: HashMap<i64, BTreeSet<i64>> =
        serde_json::from_value::<HashMap<String, Vec<i64>>>(p["mentions"].clone())
            .unwrap()
            .into_iter()
            .map(|(k, v)| (k.parse().unwrap(), v.into_iter().collect()))
            .collect();
    let scope: BTreeMap<i64, Vec<String>> =
        serde_json::from_value::<HashMap<String, Vec<String>>>(p["scope"].clone())
            .unwrap()
            .into_iter()
            .map(|(k, v)| (k.parse().unwrap(), v))
            .collect();
    let ctx = Contexts { vectors, mentions };
    let dismissed = HashSet::from([("harbor".to_string(), "kelp-farm".to_string())]);
    let (none, pending) = (HashSet::new(), HashSet::new());
    let d = deg(&edges);
    let dups = exact_duplicate_pairs(&entities, &d);
    let pairs = candidate_pairs(
        &ctx,
        &edges,
        &entities,
        &scope,
        &dups,
        &CandidateOpts {
            min_similarity: 0.0,
            top_k: 50,
            dismissed: &dismissed,
            max_support_overlap: 0.8,
            pending_pairs: &pending,
            excluded_ids: &none,
        },
    );
    let got: Vec<Value> = pairs.iter().map(candidate_json).collect();
    assert_eq!(Value::Array(got), p["pairs"]);
    let facts = HashMap::from([(2, 3)]);
    let (merges, links) = partition_candidates(pairs, &entities, &d, &facts, 0.0);
    let merges: Vec<Value> = merges
        .iter()
        .map(|m| {
            json!({"from_id": m.from_id, "into_id": m.into_id, "from": m.from, "into": m.into,
                   "similarity": m.similarity, "reason": m.reason})
        })
        .collect();
    assert_eq!(Value::Array(merges), p["merges"]);
    let links: Vec<Value> = links.iter().map(candidate_json).collect();
    assert_eq!(Value::Array(links), p["links"]);
    // Excluded ids (junk-owned, lesson-only) and pending pairs drop before
    // the top-k cut.
    let excluded = p["excluded"].as_array().unwrap();
    assert!(excluded.len() >= 3);
    for case in excluded {
        let excl: HashSet<i64> = serde_json::from_value(case["excluded_ids"].clone()).unwrap();
        let pending: HashSet<(i64, i64)> =
            serde_json::from_value::<Vec<(i64, i64)>>(case["pending_pairs"].clone())
                .unwrap()
                .into_iter()
                .collect();
        let got = candidate_pairs(
            &ctx,
            &edges,
            &entities,
            &scope,
            &dups,
            &CandidateOpts {
                min_similarity: 0.0,
                top_k: case["top_k"].as_i64().unwrap(),
                dismissed: &dismissed,
                max_support_overlap: 0.8,
                pending_pairs: &pending,
                excluded_ids: &excl,
            },
        );
        let got: Vec<Value> = got.iter().map(candidate_json).collect();
        assert_eq!(Value::Array(got), case["pairs"], "excluded {excl:?}");
    }
}

#[test]
fn slot_duplicates_match_python() {
    per_kernel(slot_duplicates_case);
}

fn slot_duplicates_case(g: &Value) {
    let s = &g["slot_duplicates"];
    let records: Vec<SlotRecord> = s["records"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            let mut label = Map::new();
            for k in ["entity", "attribute", "value"] {
                label.insert(k.into(), r[k].clone());
            }
            SlotRecord {
                key: r["key"].as_str().unwrap().to_string(),
                label,
                embedding: (!r["embedding"].is_null()).then(|| f32s(&r["embedding"])),
            }
        })
        .collect();
    let dismissed = HashSet::from([("host-a|os".to_string(), "host-b|os".to_string())]);
    let got = slot_duplicate_candidates(&records, 0.0, 20, &dismissed);
    assert_eq!(Value::Array(got), s["expected"]);
}

#[test]
fn name_shapes_match_python() {
    let g = golden();
    for case in g["variant_tokens"].as_array().unwrap() {
        let name = case[0].as_str().unwrap();
        let want: Vec<String> = serde_json::from_value(case[1].clone()).unwrap();
        let got: Vec<String> = variant_tokens(name).into_iter().collect();
        assert_eq!(got, want, "variant_tokens({name:?})");
    }
    for case in g["name_contains"].as_array().unwrap() {
        let (a, b) = (case[0].as_str().unwrap(), case[1].as_str().unwrap());
        assert_eq!(name_contains(a, b), case[2].as_str(), "{a:?} {b:?}");
    }
    for case in g["compound_halves"].as_array().unwrap() {
        let name = case[0].as_str().unwrap();
        let want: Option<(String, String)> = serde_json::from_value(case[1].clone()).unwrap();
        assert_eq!(compound_halves(name), want, "{name:?}");
    }
}

#[test]
fn junk_and_exact_duplicates_match_python() {
    let g = golden();
    let j = &g["junk"];
    let entities: Vec<Entity> = j["entities"]
        .as_array()
        .unwrap()
        .iter()
        .map(entity)
        .collect();
    let edges: Vec<GEdge> = j["edges"].as_array().unwrap().iter().map(edge).collect();
    let d = deg(&edges);
    for case in j["cases"].as_array().unwrap() {
        let known: HashSet<String> = serde_json::from_value(case["known_norms"].clone()).unwrap();
        let got: Vec<Value> =
            junk_entities(&entities, &d, case["max_degree"].as_i64().unwrap(), &known)
                .iter()
                .map(
                    |x| json!({"entity_id": x.entity_id, "display": x.display, "reason": x.reason}),
                )
                .collect();
        assert_eq!(
            Value::Array(got),
            case["expected"],
            "{}",
            case["max_degree"]
        );
    }
    let x = &g["exact_dups"];
    let entities: Vec<Entity> = x["entities"]
        .as_array()
        .unwrap()
        .iter()
        .map(entity)
        .collect();
    let edges: Vec<GEdge> = x["edges"].as_array().unwrap().iter().map(edge).collect();
    let got: Vec<Value> = exact_duplicate_pairs(&entities, &deg(&edges))
        .iter()
        .map(|&(a, b)| json!([a, b]))
        .collect();
    assert_eq!(Value::Array(got), x["expected"]);
}

#[test]
fn python_slices_and_rounding() {
    assert_eq!(py_head(5, 3), 3);
    assert_eq!(py_head(5, -2), 3);
    assert_eq!(py_head(2, -5), 0);
    assert_eq!(py_slice_chars("abcdé", -1), "abcd");
    assert_eq!(py_slice_chars("abcdé", 10), "abcdé");
    assert_eq!(py_round(0.98285, 4), 0.9829);
    assert_eq!(py_round(0.98275, 4), 0.9828);
    assert_eq!(py_round(-0.04, 1), -0.0);
    // Python's sort keys tie -0.0 with 0.0; total_cmp would not.
    assert_eq!(py_float_cmp(-0.0, 0.0), std::cmp::Ordering::Equal);
    assert_eq!(py_float_cmp(0.5, -0.0), std::cmp::Ordering::Greater);
}

#[test]
fn signed_zero_similarities_tie_and_ids_decide() {
    // Two orthogonal pairs, one rounding to -0.0 and one to 0.0: Python's
    // `(-similarity, src_id, dst_id)` key orders them by id alone.
    let entities: Vec<Entity> = (1..=4)
        .map(|k| Entity {
            id: k,
            canonical: format!("c{k}"),
            display: format!("Name {k}"),
        })
        .collect();
    let v = |a: f32, b: f32| vec![a, b];
    let vectors = HashMap::from([
        (1, v(1.0, 0.0)),
        (2, v(-0.00001, 1.0)),
        (3, v(0.0, -1.0)),
        (4, v(0.00001, 1.0)),
    ]);
    let ctx = Contexts {
        vectors,
        mentions: HashMap::new(),
    };
    let (none, pending, dismissed) = (HashSet::new(), HashSet::new(), HashSet::new());
    let got = candidate_pairs(
        &ctx,
        &[],
        &entities,
        &BTreeMap::new(),
        &[],
        &CandidateOpts {
            min_similarity: -0.5,
            top_k: 10,
            dismissed: &dismissed,
            max_support_overlap: 1.0,
            pending_pairs: &pending,
            excluded_ids: &none,
        },
    );
    let zeros: Vec<(i64, i64, u64)> = got
        .iter()
        .filter(|c| c.similarity == 0.0)
        .map(|c| (c.src_id, c.dst_id, c.similarity.to_bits()))
        .collect();
    assert!(
        zeros.iter().any(|z| z.2 == (-0.0f64).to_bits()),
        "{zeros:?}"
    );
    assert!(zeros.iter().any(|z| z.2 == 0f64.to_bits()), "{zeros:?}");
    let ids: Vec<(i64, i64)> = zeros.iter().map(|z| (z.0, z.1)).collect();
    let mut sorted = ids.clone();
    sorted.sort_unstable();
    assert_eq!(ids, sorted);
}
