//! Bounded top-k in `candidate_pairs` and `slot_duplicate_candidates`.
//!
//! On a real bank's copy with every floor at -1.0 (w2e's deep-dream config),
//! the full collect-then-sort grew the Rust daemon past 18 GiB in one
//! `dream deep` (13,241 entities; 3,748 lessons, about 7.0M pairs); Python
//! finished at 9.2 GiB (daemon box, 2026-11-08). The reference functions
//! below are the passes as they stood before the bounded rewrite, kept
//! verbatim so the parity tests compare old and new output exactly.

use super::*;
use crate::test_alloc::peak_during;

// ── reference copies (pre-change, verbatim) ──────────────────────────────

fn reference_candidate_pairs(
    contexts: &Contexts,
    edges: &[GEdge],
    entities: &[Entity],
    scope_map: &BTreeMap<i64, Vec<String>>,
    dups: &[(i64, i64)],
    o: &CandidateOpts<'_>,
) -> Vec<Candidate> {
    let disp: HashMap<i64, &str> = entities
        .iter()
        .map(|e| (e.id, e.display.as_str()))
        .collect();
    let canon: HashMap<i64, &str> = entities
        .iter()
        .map(|e| (e.id, e.canonical.as_str()))
        .collect();
    let key = |a: i64, b: i64| (a.min(b), a.max(b));
    let mut linked: HashSet<(i64, i64)> = edges.iter().map(|e| key(e.src_id, e.dst_id)).collect();
    linked.extend(o.pending_pairs.iter().copied());
    let dup: HashSet<(i64, i64)> = dups.iter().map(|&(a, b)| key(a, b)).collect();
    let mut ids: Vec<i64> = contexts
        .vectors
        .keys()
        .copied()
        .filter(|i| !o.excluded_ids.contains(i))
        .collect();
    ids.sort_unstable();
    let mut scored = Vec::new();
    if ids.len() < 2 {
        return scored;
    }
    let empty = BTreeSet::new();
    for (n, &u) in ids.iter().enumerate() {
        let vu = &contexts.vectors[&u];
        for &v in &ids[n + 1..] {
            let k = key(u, v);
            if linked.contains(&k) || dup.contains(&k) {
                continue;
            }
            let sim = f64::from(sdot(vu, &contexts.vectors[&v]));
            if sim < o.min_similarity {
                continue;
            }
            if !o.dismissed.is_empty() {
                let (cu, cv) = (
                    canon.get(&u).copied().unwrap_or(""),
                    canon.get(&v).copied().unwrap_or(""),
                );
                let pair = if cu <= cv { (cu, cv) } else { (cv, cu) };
                if o.dismissed
                    .contains(&(pair.0.to_string(), pair.1.to_string()))
                {
                    continue;
                }
            }
            let mu = contexts.mentions.get(&u).unwrap_or(&empty);
            let mv = contexts.mentions.get(&v).unwrap_or(&empty);
            if !mu.is_empty() && !mv.is_empty() {
                let shared = mu.intersection(mv).count();
                let ratio = shared as f64 / mu.len().min(mv.len()) as f64;
                if ratio >= o.max_support_overlap
                    && name_contains(
                        disp.get(&u).copied().unwrap_or(""),
                        disp.get(&v).copied().unwrap_or(""),
                    )
                    .is_none()
                {
                    continue;
                }
            }
            let su: HashSet<&String> = scope_map.get(&u).into_iter().flatten().collect();
            let sv: HashSet<&String> = scope_map.get(&v).into_iter().flatten().collect();
            if !su.is_empty() && !sv.is_empty() && su.is_disjoint(&sv) {
                continue;
            }
            let label = |id: i64| {
                disp.get(&id)
                    .map_or_else(|| id.to_string(), |d| d.to_string())
            };
            scored.push(Candidate {
                src_id: u,
                dst_id: v,
                src: label(u),
                dst: label(v),
                similarity: py_round(sim, 4),
            });
        }
    }
    scored.sort_by(|a, b| {
        py_float_cmp(b.similarity, a.similarity)
            .then(a.src_id.cmp(&b.src_id))
            .then(a.dst_id.cmp(&b.dst_id))
    });
    let keep = py_head(scored.len(), o.top_k);
    scored.truncate(keep);
    scored
}

fn reference_slot_duplicate_candidates(
    records: &[SlotRecord],
    min_similarity: f64,
    top_k: i64,
    dismissed: &HashSet<(String, String)>,
) -> Vec<Value> {
    let recs: Vec<&SlotRecord> = records.iter().filter(|r| r.embedding.is_some()).collect();
    let vecs: Vec<Vec<f32>> = recs
        .iter()
        .map(|r| l2(r.embedding.clone().unwrap_or_default()))
        .collect();
    let mut scored: Vec<(f64, String, String, Value)> = Vec::new();
    for i in 0..recs.len() {
        for j in i + 1..recs.len() {
            let (ki, kj) = (&recs[i].key, &recs[j].key);
            let (ka, kb) = if ki <= kj { (ki, kj) } else { (kj, ki) };
            if ka == kb {
                continue;
            }
            if dismissed.contains(&(ka.clone(), kb.clone())) {
                continue;
            }
            let ea = py_casefold(py_strip(&slot_entity(recs[i])));
            let eb = py_casefold(py_strip(&slot_entity(recs[j])));
            if id_keyed_siblings(&ea, &eb) {
                continue;
            }
            let sim = f64::from(sdot(&vecs[i], &vecs[j]));
            let floor = if ea == eb {
                SAME_ENTITY_MIN_SIMILARITY
            } else {
                min_similarity
            };
            if sim < floor {
                continue;
            }
            let (a, b) = if recs[i].key == *ka {
                (recs[i], recs[j])
            } else {
                (recs[j], recs[i])
            };
            let rounded = py_round(sim, 4);
            let mut m = Map::new();
            m.insert("a_key".into(), json!(ka));
            m.insert("b_key".into(), json!(kb));
            m.insert("a".into(), Value::Object(a.label.clone()));
            m.insert("b".into(), Value::Object(b.label.clone()));
            m.insert("similarity".into(), json!(rounded));
            scored.push((rounded, ka.clone(), kb.clone(), Value::Object(m)));
        }
    }
    scored.sort_by(|x, y| {
        py_float_cmp(y.0, x.0)
            .then_with(|| x.1.cmp(&y.1))
            .then_with(|| x.2.cmp(&y.2))
    });
    let keep = py_head(scored.len(), top_k);
    scored.into_iter().take(keep).map(|s| s.3).collect()
}

// ── fixtures ─────────────────────────────────────────────────────────────

/// A deterministic unit vector per seed (an LCG; no rand dependency).
fn unit(seed: u64, dim: usize) -> Vec<f32> {
    let mut s = seed
        .wrapping_mul(6_364_136_223_846_793_005)
        .wrapping_add(1_442_695_040_888_963_407);
    let mut v: Vec<f32> = (0..dim)
        .map(|_| {
            s = s
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            ((s >> 33) as f32 / (1u64 << 31) as f32) - 0.5
        })
        .collect();
    let n = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    v.iter_mut().for_each(|x| *x /= n);
    v
}

fn entities(n: i64) -> Vec<Entity> {
    (1..=n)
        .map(|k| Entity {
            id: k,
            canonical: format!("entity-{k}"),
            display: format!("Entity number {k}"),
        })
        .collect()
}

/// Contexts where many entities share one of a few vectors, so rounded
/// similarities tie heavily and ids alone decide the order.
fn tied_contexts(n: i64, distinct: u64) -> Contexts {
    Contexts {
        vectors: (1..=n)
            .map(|k| (k, unit((k as u64 * 7919) % distinct, 6)))
            .collect(),
        mentions: HashMap::new(),
    }
}

fn opts<'a>(
    min_similarity: f64,
    top_k: i64,
    empty_pairs: &'a HashSet<(String, String)>,
    pending: &'a HashSet<(i64, i64)>,
    excluded: &'a HashSet<i64>,
) -> CandidateOpts<'a> {
    CandidateOpts {
        min_similarity,
        top_k,
        dismissed: empty_pairs,
        max_support_overlap: 1.0,
        pending_pairs: pending,
        excluded_ids: excluded,
    }
}

fn cand_rows(v: &[Candidate]) -> Vec<String> {
    v.iter()
        .map(|c| format!("{c:?} bits={}", c.similarity.to_bits()))
        .collect()
}

/// Slot records with repeated keys and tied embeddings: the full sort is
/// stable, so equal `(similarity, a_key, b_key)` keep their scan order.
fn tied_records(n: usize, distinct: u64) -> Vec<SlotRecord> {
    (0..n)
        .map(|i| {
            let mut label = Map::new();
            label.insert("entity".into(), json!(format!("Subject {}", i % 13)));
            label.insert("attribute".into(), json!(format!("aspect-{}", i % 5)));
            label.insert(
                "value".into(),
                json!(format!("value {i} {}", "x".repeat(i % 7))),
            );
            SlotRecord {
                // Every third record reuses an earlier key.
                key: format!("subject-{}|aspect-{}", (i / 3) % 17, i % 5),
                label,
                embedding: (i % 11 != 0).then(|| unit(i as u64 % distinct, 6)),
            }
        })
        .collect()
}

const TOP_KS: [i64; 10] = [-1_000_000, -7, -1, 0, 1, 2, 5, 25, 300, 1_000_000];

// ── parity: new output equals the full sort, ties included ───────────────

#[test]
fn candidate_pairs_equal_the_full_sort_with_ties() {
    let ents = entities(40);
    let ctx = tied_contexts(40, 4);
    let (none, pending, empty) = (HashSet::new(), HashSet::new(), HashSet::new());
    let dups = [(3, 9), (12, 5)];
    let edges = [GEdge {
        id: 1,
        src_id: 1,
        relation: "relates_to".into(),
        dst_id: 2,
        confidence: 0.5,
        origin: None,
    }];
    for floor in [-1.0, 0.0, 0.5] {
        for k in TOP_KS {
            let o = opts(floor, k, &empty, &pending, &none);
            let old = reference_candidate_pairs(&ctx, &edges, &ents, &BTreeMap::new(), &dups, &o);
            let new = candidate_pairs(&ctx, &edges, &ents, &BTreeMap::new(), &dups, &o);
            assert_eq!(cand_rows(&new), cand_rows(&old), "floor {floor} top_k {k}");
        }
    }
}

#[test]
fn slot_duplicates_equal_the_full_sort_with_ties_and_repeated_keys() {
    let records = tied_records(90, 3);
    let dismissed = HashSet::from([(
        "subject-1|aspect-1".to_string(),
        "subject-2|aspect-2".to_string(),
    )]);
    for floor in [-1.0, 0.0, 0.9] {
        for k in TOP_KS {
            let old = reference_slot_duplicate_candidates(&records, floor, k, &dismissed);
            let new = slot_duplicate_candidates(&records, floor, k, &dismissed);
            assert_eq!(
                Value::Array(new),
                Value::Array(old),
                "floor {floor} top_k {k}"
            );
        }
    }
}

// ── memory: the passes hold about top_k pairs, not every survivor ────────

/// Far above what a bounded pass holds for these inputs, and far below what
/// the full collect held (measured on the reference copies: see each test).
const BOUND: usize = 16 << 20;

#[test]
fn candidate_pairs_peak_memory_does_not_grow_with_survivors() {
    // 1,500 entities, every pair above the floor: 1,124,250 survivors.
    let ents = entities(1_500);
    let ctx = Contexts {
        vectors: (1..=1_500).map(|k| (k, unit(k as u64, 8))).collect(),
        mentions: HashMap::new(),
    };
    let (none, pending, empty) = (HashSet::new(), HashSet::new(), HashSet::new());
    let o = opts(-1.0, 25, &empty, &pending, &none);
    let (got, peak) = peak_during(|| candidate_pairs(&ctx, &[], &ents, &BTreeMap::new(), &[], &o));
    assert_eq!(got.len(), 25);
    assert!(
        peak < BOUND,
        "candidate_pairs held {peak} bytes at its peak"
    );
}

#[test]
fn slot_duplicates_peak_memory_does_not_grow_with_survivors() {
    // 700 distinct-entity records, every pair above the floor: 244,650 pairs.
    let records: Vec<SlotRecord> = (0..700)
        .map(|i| {
            let mut label = Map::new();
            label.insert(
                "entity".into(),
                json!(format!("Distinct subject number {i}")),
            );
            label.insert("attribute".into(), json!("an attribute name"));
            label.insert(
                "value".into(),
                json!(format!(
                    "{i} {}",
                    "a reasonably long stored value ".repeat(3)
                )),
            );
            SlotRecord {
                key: format!("subject-{i}|attribute"),
                label,
                embedding: Some(unit(i as u64, 8)),
            }
        })
        .collect();
    let (got, peak) = peak_during(|| slot_duplicate_candidates(&records, -1.0, 8, &HashSet::new()));
    assert_eq!(got.len(), 8);
    assert!(
        peak < BOUND,
        "slot_duplicate_candidates held {peak} bytes at its peak"
    );
}

#[test]
fn the_meter_sees_a_full_collect() {
    // Near miss for the bound itself: the reference full collect over the
    // same slot records is far above it, so the bound can fail.
    let records: Vec<SlotRecord> = (0..700)
        .map(|i| {
            let mut label = Map::new();
            label.insert(
                "entity".into(),
                json!(format!("Distinct subject number {i}")),
            );
            label.insert("attribute".into(), json!("an attribute name"));
            label.insert(
                "value".into(),
                json!(format!(
                    "{i} {}",
                    "a reasonably long stored value ".repeat(3)
                )),
            );
            SlotRecord {
                key: format!("subject-{i}|attribute"),
                label,
                embedding: Some(unit(i as u64, 8)),
            }
        })
        .collect();
    let (got, peak) =
        peak_during(|| reference_slot_duplicate_candidates(&records, -1.0, 8, &HashSet::new()));
    assert_eq!(got.len(), 8);
    assert!(peak > 8 * BOUND, "the full collect held only {peak} bytes");
}
