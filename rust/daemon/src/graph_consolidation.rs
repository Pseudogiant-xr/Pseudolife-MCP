//! The deep dream's pure graph-consolidation logic; oracle:
//! `memory/graph_consolidation.py` (the self-clean classifiers, candidate
//! generation, the slot-duplicate listing and the junk detector). DB-free,
//! like the Python: `dream/deep.rs` loads the rows and persists decisions.
//!
//! Numerics follow numpy exactly where the Python computes in float32:
//! * a context vector is `np.mean(np.stack(embs), axis=0)`: a float32 sum in
//!   row order (the iteration order of the Python `set` of entry ids, which
//!   [`py_int_set_order`] reproduces), divided by the count;
//! * `np.dot` and `np.linalg.norm` on float32 vectors call OpenBLAS `sdot`;
//!   [`sdot`] is its AVX-512 (SkylakeX) kernel, fused multiply-adds into
//!   four 16-lane accumulators, folded as the kernel folds them. An
//!   AVX2-only host runs OpenBLAS's Haswell kernel instead and can differ
//!   from this in the last float32 bit (declared).
//!
//! Python string semantics carried over: `str.lower()` is
//! `storage::graph::py_lower`, `str.strip()`/`\s` is `str.isspace`,
//! `str.isdigit()` is `dream::gates::py_isdigit`, `re.I` on ASCII literals is
//! `graph_review::ci_eq`.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::LazyLock;

use serde_json::{Map, Value, json};

use crate::graph_review::{Entity, ci_eq, round3, token_set};
use crate::graph_review_tools::actions::{edge_confidence, is_hard_type_violation};
use crate::junk_names::{is_concat_artifact, is_list_artifact, is_metric_reading};
use crate::storage::graph::{norm_name, py_lower};
use crate::stores::cortex::{py_casefold, py_isspace};

/// A live `edges` row as `load_graph` returns it.
#[derive(Debug, Clone)]
pub(crate) struct GEdge {
    pub id: i64,
    pub src_id: i64,
    pub relation: String,
    pub dst_id: i64,
    pub confidence: f64,
    pub origin: Option<String>,
}

impl GEdge {
    pub(crate) fn review_edge(&self) -> crate::graph_review::Edge {
        crate::graph_review::Edge {
            src_id: self.src_id,
            relation: self.relation.clone(),
            dst_id: self.dst_id,
            confidence: self.confidence,
            origin: self.origin.clone(),
        }
    }
}

fn py_strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// Python `round(x, n)`: correctly rounded on the exact binary value.
pub(crate) fn py_round(x: f64, n: usize) -> f64 {
    format!("{x:.n$}").parse().unwrap_or(x)
}

/// `seq[:n]` for a Python int `n` (a negative `n` counts from the end).
pub(crate) fn py_head(len: usize, n: i64) -> usize {
    if n >= 0 {
        (n as usize).min(len)
    } else {
        len.saturating_sub(n.unsigned_abs() as usize)
    }
}

/// `s[:n]` on code points, Python slice semantics.
pub(crate) fn py_slice_chars(s: &str, n: i64) -> String {
    let count = s.chars().count();
    s.chars().take(py_head(count, n)).collect()
}

/// `_full_token_set`: every `[a-z0-9]` run of `str(name).lower()`.
pub(crate) fn full_token_set(name: &str) -> BTreeSet<String> {
    py_lower(name)
        .split(|c: char| !(c.is_ascii_lowercase() || c.is_ascii_digit()))
        .filter(|t| !t.is_empty())
        .map(str::to_string)
        .collect()
}

fn disp_of(disp: &HashMap<i64, String>, id: i64) -> &str {
    disp.get(&id).map_or("", String::as_str)
}

// ── Step A: self-clean classifiers ───────────────────────────────────────

/// `rescore_edges`: `(edge_id, new_conf)` for every agent edge whose
/// recomputed confidence differs from the stored one rounded to 3 places.
pub(crate) fn rescore_edges(edges: &[GEdge], disp: &HashMap<i64, String>) -> Vec<(i64, f64)> {
    edges
        .iter()
        .filter(|e| e.origin.as_deref() == Some("agent"))
        .filter_map(|e| {
            let new = edge_confidence(
                disp_of(disp, e.src_id),
                &e.relation,
                disp_of(disp, e.dst_id),
            );
            (round3(e.confidence) != new).then_some((e.id, new))
        })
        .collect()
}

/// `hard_violation_edges`: agent edges that are hard type violations.
pub(crate) fn hard_violation_edges<'a>(
    edges: &'a [GEdge],
    disp: &HashMap<i64, String>,
) -> Vec<&'a GEdge> {
    edges
        .iter()
        .filter(|e| {
            e.origin.as_deref() == Some("agent")
                && is_hard_type_violation(
                    disp_of(disp, e.src_id),
                    &e.relation,
                    disp_of(disp, e.dst_id),
                )
        })
        .collect()
}

/// `exact_duplicate_pairs`: `(from_id, into_id)` for token-set-identical
/// displays (no concat artifacts), the lower-degree side folded into the
/// higher (a tie folds the higher id into the lower), sorted. Grouping by
/// token set visits exactly the pairs the all-pairs loop accepts.
pub(crate) fn exact_duplicate_pairs(
    entities: &[Entity],
    deg: &BTreeMap<i64, usize>,
) -> Vec<(i64, i64)> {
    let mut groups: HashMap<BTreeSet<String>, Vec<usize>> = HashMap::new();
    for (i, e) in entities.iter().enumerate() {
        let toks = full_token_set(&e.display);
        if !toks.is_empty() {
            groups.entry(toks).or_default().push(i);
        }
    }
    let mut pairs = Vec::new();
    for idx in groups.values() {
        for (n, &i) in idx.iter().enumerate() {
            for &j in &idx[n + 1..] {
                let (a, b) = (&entities[i], &entities[j]);
                if is_concat_artifact(&a.display) || is_concat_artifact(&b.display) {
                    continue;
                }
                let da = deg.get(&a.id).copied().unwrap_or(0);
                let db = deg.get(&b.id).copied().unwrap_or(0);
                if da > db || (da == db && a.id < b.id) {
                    pairs.push((b.id, a.id));
                } else {
                    pairs.push((a.id, b.id));
                }
            }
        }
    }
    pairs.sort_unstable();
    pairs
}

// ── numpy / CPython numerics ─────────────────────────────────────────────

/// CPython's `hash(int)` as the `size_t` the set table masks.
fn py_hash_int(v: i64) -> u64 {
    const MODULUS: u64 = (1 << 61) - 1;
    let a = (v.unsigned_abs() % MODULUS) as i64;
    let h = if v < 0 { -a } else { a };
    (if h == -1 { -2 } else { h }) as u64
}

/// The iteration order of `{i for i in items}` in CPython 3.11
/// (`Objects/setobject.c`: an 8-slot table, nine linear probes then
/// perturbed probing, a resize to the next power of two above
/// `used * 4` once `fill * 5 >= mask * 3`). Duplicates are dropped.
pub(crate) fn py_int_set_order(items: impl IntoIterator<Item = i64>) -> Vec<i64> {
    const LINEAR_PROBES: usize = 9;
    fn insert_clean(table: &mut [Option<i64>], key: i64) {
        let mask = table.len() - 1;
        let mut perturb = py_hash_int(key);
        let mut i = (perturb as usize) & mask;
        loop {
            if table[i].is_none() {
                table[i] = Some(key);
                return;
            }
            if i + LINEAR_PROBES <= mask {
                for j in 1..=LINEAR_PROBES {
                    if table[i + j].is_none() {
                        table[i + j] = Some(key);
                        return;
                    }
                }
            }
            perturb >>= 5;
            i = (i
                .wrapping_mul(5)
                .wrapping_add(1)
                .wrapping_add(perturb as usize))
                & mask;
        }
    }
    let mut table: Vec<Option<i64>> = vec![None; 8];
    let mut fill = 0usize;
    for key in items {
        let mask = table.len() - 1;
        let mut perturb = py_hash_int(key);
        let mut i = (perturb as usize) & mask;
        let inserted = 'probe: loop {
            let probes = if i + LINEAR_PROBES <= mask {
                LINEAR_PROBES
            } else {
                0
            };
            for slot in table.iter_mut().skip(i).take(probes + 1) {
                match *slot {
                    None => {
                        *slot = Some(key);
                        break 'probe true;
                    }
                    Some(k) if k == key => break 'probe false,
                    Some(_) => {}
                }
            }
            perturb >>= 5;
            i = (i
                .wrapping_mul(5)
                .wrapping_add(1)
                .wrapping_add(perturb as usize))
                & mask;
        };
        if !inserted {
            continue;
        }
        fill += 1;
        if fill * 5 >= mask * 3 {
            let minused = if fill > 50000 { fill * 2 } else { fill * 4 };
            let mut size = 8usize;
            while size <= minused {
                size <<= 1;
            }
            let old = std::mem::replace(&mut table, vec![None; size]);
            for k in old.into_iter().flatten() {
                insert_clean(&mut table, k);
            }
        }
    }
    table.into_iter().flatten().collect()
}

/// OpenBLAS `sdot` (x86_64, SkylakeX kernel), unit strides.
pub(crate) fn sdot(x: &[f32], y: &[f32]) -> f32 {
    let n = x.len().min(y.len());
    let n1 = n & !31;
    let mut dot = if n1 > 0 {
        sdot_kernel(&x[..n1], &y[..n1])
    } else {
        0.0
    };
    for i in n1..n {
        dot = y[i].mul_add(x[i], dot);
    }
    dot
}

/// `sdot_kernel_16` (`sdot_microk_skylakex-2.c`) for `n` a multiple of 32.
fn sdot_kernel(x: &[f32], y: &[f32]) -> f32 {
    #[cfg(target_arch = "x86_64")]
    {
        if std::arch::is_x86_feature_detected!("fma") {
            // SAFETY: the FMA feature was detected at runtime.
            return unsafe { sdot_kernel_fma(x, y) };
        }
    }
    sdot_kernel_body(x, y)
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "fma,avx2")]
unsafe fn sdot_kernel_fma(x: &[f32], y: &[f32]) -> f32 {
    sdot_kernel_body(x, y)
}

#[inline(always)]
fn sdot_kernel_body(x: &[f32], y: &[f32]) -> f32 {
    let n = x.len();
    let n64 = n & !63;
    let mut wide = [[0f32; 16]; 4];
    let mut i = 0;
    while i < n64 {
        for (k, acc) in wide.iter_mut().enumerate() {
            let base = i + 16 * k;
            for (l, a) in acc.iter_mut().enumerate() {
                *a = x[base + l].mul_add(y[base + l], *a);
            }
        }
        i += 64;
    }
    // The 512-bit accumulators fold into the 256-bit ones (low + high).
    let mut acc = [[0f32; 8]; 4];
    for k in 0..4 {
        for l in 0..8 {
            acc[k][l] = wide[k][l] + wide[k][l + 8];
        }
    }
    while i < n {
        for (k, a8) in acc.iter_mut().enumerate() {
            let base = i + 8 * k;
            for (l, a) in a8.iter_mut().enumerate() {
                *a = x[base + l].mul_add(y[base + l], *a);
            }
        }
        i += 32;
    }
    let mut s = [0f32; 8];
    for l in 0..8 {
        s[l] = ((acc[0][l] + acc[1][l]) + acc[2][l]) + acc[3][l];
    }
    let h = [s[0] + s[4], s[1] + s[5], s[2] + s[6], s[3] + s[7]];
    (h[0] + h[1]) + (h[2] + h[3])
}

/// `_l2`: `v / float(np.linalg.norm(v))` in float32, unchanged at norm 0.
pub(crate) fn l2(mut v: Vec<f32>) -> Vec<f32> {
    let n = sdot(&v, &v).sqrt();
    if n != 0.0 {
        for x in &mut v {
            *x /= n;
        }
    }
    v
}

/// `np.mean(np.stack(rows), axis=0)` on float32 rows: a float32 sum in row
/// order, divided by the count (numpy 2 divides in float64 by the `intp`
/// count and casts back, which equals the float32 quotient).
pub(crate) fn mean_rows(rows: &[&[f32]]) -> Vec<f32> {
    let Some(first) = rows.first() else {
        return Vec::new();
    };
    let mut acc = first.to_vec();
    for r in &rows[1..] {
        for (a, x) in acc.iter_mut().zip(r.iter()) {
            *a += *x;
        }
    }
    let count = rows.len() as f64;
    acc.iter().map(|a| (f64::from(*a) / count) as f32).collect()
}

// ── Step B: candidate generation ─────────────────────────────────────────

/// One `load_entries` row as the deep dream reads it.
#[derive(Debug, Clone)]
pub(crate) struct DEntry {
    pub id: i64,
    pub text: String,
    pub source: Option<String>,
    pub embedding: Option<Vec<f32>>,
}

/// The vectors and mentions `entity_context_vectors` returns.
pub(crate) struct Contexts {
    pub vectors: HashMap<i64, Vec<f32>>,
    pub mentions: HashMap<i64, BTreeSet<i64>>,
}

/// `entity_context_vectors`: per entity, its trace entries (else the
/// entries whose token set contains its display's, excluded outright over
/// `max_fallback`), kept at `min_mentions` distinct live entries; the vector
/// is the float32 mean of their embeddings in set order, L2-normalized.
/// `Err` is numpy's refusal when there is nothing to stack.
pub(crate) fn entity_context_vectors(
    entities: &[Entity],
    entries: &[DEntry],
    entry_tokens: &[(i64, BTreeSet<String>)],
    traces: &HashMap<String, Vec<i64>>,
    min_mentions: i64,
    max_fallback: Option<i64>,
) -> Result<Contexts, String> {
    let by_id: HashMap<i64, &DEntry> = entries.iter().map(|e| (e.id, e)).collect();
    let mut out = Contexts {
        vectors: HashMap::new(),
        mentions: HashMap::new(),
    };
    for ent in entities {
        let mut ids: Vec<i64> = traces.get(&ent.canonical).cloned().unwrap_or_default();
        if ids.is_empty() {
            let want = token_set(&ent.display);
            if !want.is_empty() {
                ids = entry_tokens
                    .iter()
                    .filter(|(_, toks)| want.is_subset(toks))
                    .map(|(id, _)| *id)
                    .collect();
                if max_fallback.is_some_and(|cap| (ids.len() as i64) > cap) {
                    continue;
                }
            }
        }
        let valid = py_int_set_order(ids.into_iter().filter(|i| by_id.contains_key(i)));
        if (valid.len() as i64) < min_mentions {
            continue;
        }
        let mut rows: Vec<&[f32]> = Vec::with_capacity(valid.len());
        for i in &valid {
            match by_id[i].embedding.as_deref() {
                Some(e) => rows.push(e),
                // `np.stack` over a None embedding raises.
                None => return Err("ValueError: entry without an embedding".into()),
            }
        }
        if rows.is_empty() {
            return Err("ValueError: need at least one array to stack".into());
        }
        if rows.iter().any(|r| r.len() != rows[0].len()) {
            return Err("ValueError: all input arrays must have the same shape".into());
        }
        out.vectors.insert(ent.id, l2(mean_rows(&rows)));
        out.mentions.insert(ent.id, valid.into_iter().collect());
    }
    Ok(out)
}

/// One candidate pair (`candidate_pairs`' dict).
#[derive(Debug, Clone)]
pub(crate) struct Candidate {
    pub src_id: i64,
    pub dst_id: i64,
    pub src: String,
    pub dst: String,
    pub similarity: f64,
}

impl Candidate {
    pub(crate) fn to_map(&self) -> Map<String, Value> {
        let mut m = Map::new();
        m.insert("src_id".into(), json!(self.src_id));
        m.insert("dst_id".into(), json!(self.dst_id));
        m.insert("src".into(), json!(self.src));
        m.insert("dst".into(), json!(self.dst));
        m.insert("similarity".into(), json!(self.similarity));
        m
    }
}

/// `candidate_pairs`' keyword arguments.
pub(crate) struct CandidateOpts<'a> {
    pub min_similarity: f64,
    pub top_k: i64,
    pub dismissed: &'a HashSet<(String, String)>,
    pub max_support_overlap: f64,
    pub pending_pairs: &'a HashSet<(i64, i64)>,
    pub excluded_ids: &'a HashSet<i64>,
}

/// `candidate_pairs`: unlinked, non-duplicate, undismissed, scope-coherent
/// pairs whose supporting entries are not co-mentions (name-contained pairs
/// exempt), at or above `min_similarity`, best first, cut to `top_k`. The
/// Python ranks a matmul prefilter and stops past the k-th survivor's tie
/// band; its output is pinned equal to this full scan (the per-pair `np.dot`
/// value decides and is reported).
pub(crate) fn candidate_pairs(
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
        b.similarity
            .total_cmp(&a.similarity)
            .then(a.src_id.cmp(&b.src_id))
            .then(a.dst_id.cmp(&b.dst_id))
    });
    let keep = py_head(scored.len(), o.top_k);
    scored.truncate(keep);
    scored
}

// ── store curation: cross-key near-duplicate slot pairs ──────────────────

/// `_SAME_ENTITY_MIN_SIMILARITY`.
pub(crate) const SAME_ENTITY_MIN_SIMILARITY: f64 = 0.95;

/// One `_curation_records` record: the slot key, the label fields (every
/// key but `key` and `embedding`, in Python's order) and the vector.
#[derive(Debug, Clone)]
pub(crate) struct SlotRecord {
    pub key: String,
    pub label: Map<String, Value>,
    pub embedding: Option<Vec<f32>>,
}

/// `_ID_KEYED_ENTITY`: `^([A-Za-z][\w.-]*):(\S+)$`.
static ID_KEYED: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(r"^([A-Za-z][\p{L}\p{N}_.\-]*):([^\s\x1c-\x1f]+)$").expect("id-keyed regex")
});

fn id_keyed_siblings(ea: &str, eb: &str) -> bool {
    match (ID_KEYED.captures(ea), ID_KEYED.captures(eb)) {
        (Some(a), Some(b)) => {
            py_casefold(&a[1]) == py_casefold(&b[1]) && py_casefold(&a[2]) != py_casefold(&b[2])
        }
        _ => false,
    }
}

fn slot_entity(rec: &SlotRecord) -> String {
    match rec.label.get("entity") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => rec.key.split('|').next().unwrap_or("").to_string(),
    }
}

/// `slot_duplicate_candidates`: cross-key pairs over the records' own
/// normalized embeddings at or above the floor (`same_entity_min` for one
/// entity's aspect siblings), identifier-keyed siblings and dismissed pairs
/// skipped, best first, cut to `top_k`.
pub(crate) fn slot_duplicate_candidates(
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
        y.0.total_cmp(&x.0)
            .then_with(|| x.1.cmp(&y.1))
            .then_with(|| x.2.cmp(&y.2))
    });
    let keep = py_head(scored.len(), top_k);
    scored.into_iter().take(keep).map(|s| s.3).collect()
}

// ── SP-1: entity consolidation (merge + junk surfacing) ──────────────────

/// `_JUNK_STOPWORDS`.
const JUNK_STOPWORDS: [&str; 12] = [
    "live", "merged", "done", "fixed", "current", "ok", "pending", "wip", "todo", "n/a", "none",
    "null",
];

/// `_VARIANT_PATTERNS`, in order.
static VARIANT_PATTERNS: LazyLock<Vec<fancy_regex::Regex>> = LazyLock::new(|| {
    const VB: &str = r"(?<![A-Za-z0-9])";
    const VE: &str = r"(?![A-Za-z0-9])";
    [
        format!(r"(?i){VB}E\d+B{VE}"),
        format!(r"(?i){VB}\d+(?:\.\d+)?[MK]?B{VE}"),
        format!(r"(?i){VB}Q\d[_-]K(?:[_-](?:XS|S|M|L|XL))?{VE}"),
        format!(r"(?i){VB}q\d[_-]\d{VE}"),
        format!(r"(?i){VB}UD[_-]Q[A-Za-z0-9_-]*{VE}"),
        r"\d+\.\d+(?:\.\d+)*".to_string(),
    ]
    .iter()
    .map(|p| fancy_regex::Regex::new(p).expect("variant pattern"))
    .collect()
});

/// `variant_tokens`: size / quant / dotted-version markers, casefolded,
/// `_` folded to `-`.
pub(crate) fn variant_tokens(name: &str) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for pat in VARIANT_PATTERNS.iter() {
        for m in pat.find_iter(name).flatten() {
            out.insert(py_casefold(m.as_str()).replace('_', "-"));
        }
    }
    out
}

/// `variant_conflict`: both names carry variant tokens and the sets differ.
pub(crate) fn variant_conflict(a: &str, b: &str) -> bool {
    let (ta, tb) = (variant_tokens(a), variant_tokens(b));
    !ta.is_empty() && !tb.is_empty() && ta != tb
}

/// `_name_contains`: `"token-subset"` / `"substring"` when one display
/// asserts identity with the other (no concat artifacts, at least two
/// tokens on the smaller side), else None.
pub(crate) fn name_contains(a: &str, b: &str) -> Option<&'static str> {
    if is_concat_artifact(a) || is_concat_artifact(b) {
        return None;
    }
    let (ta, tb) = (full_token_set(a), full_token_set(b));
    if ta.len().min(tb.len()) < 2 {
        return None;
    }
    if ta.is_subset(&tb) || tb.is_subset(&ta) {
        return Some("token-subset");
    }
    let (na, nb) = (norm_name(a), norm_name(b));
    if !na.is_empty() && !nb.is_empty() && (nb.contains(&na) || na.contains(&nb)) {
        return Some("substring");
    }
    None
}

/// One merge candidate (`partition_candidates`' dict).
#[derive(Debug, Clone)]
pub(crate) struct MergeCand {
    pub from_id: i64,
    pub into_id: i64,
    pub from: String,
    pub into: String,
    pub similarity: f64,
    pub reason: &'static str,
}

/// `partition_candidates`: high-similarity name-contained pairs without a
/// variant conflict become merges, folded into the side with more
/// `(degree + facts, degree)` (a tie folds the higher id into the lower);
/// the rest stay link candidates.
pub(crate) fn partition_candidates(
    pairs: Vec<Candidate>,
    entities: &[Entity],
    deg: &BTreeMap<i64, usize>,
    facts: &HashMap<i64, i64>,
    merge_min_similarity: f64,
) -> (Vec<MergeCand>, Vec<Candidate>) {
    let disp: HashMap<i64, &str> = entities
        .iter()
        .map(|e| (e.id, e.display.as_str()))
        .collect();
    let mut merges = Vec::new();
    let mut links = Vec::new();
    for p in pairs {
        let reason = if p.similarity >= merge_min_similarity && !variant_conflict(&p.src, &p.dst) {
            name_contains(&p.src, &p.dst)
        } else {
            None
        };
        let Some(reason) = reason else {
            links.push(p);
            continue;
        };
        let rank = |id: i64| {
            let d = deg.get(&id).copied().unwrap_or(0) as i64;
            (d + facts.get(&id).copied().unwrap_or(0), d)
        };
        let (u, v) = (p.src_id, p.dst_id);
        let (into, frm) = if rank(u) > rank(v) || (rank(u) == rank(v) && u < v) {
            (u, v)
        } else {
            (v, u)
        };
        let label = |id: i64| {
            disp.get(&id)
                .map_or_else(|| id.to_string(), |d| d.to_string())
        };
        merges.push(MergeCand {
            from_id: frm,
            into_id: into,
            from: label(frm),
            into: label(into),
            similarity: p.similarity,
            reason,
        });
    }
    (merges, links)
}

/// `_CODE_OR_DATA_EXT` alternatives (`ya?ml` and `html?` spelled out).
const CODE_OR_DATA_EXT: &[&str] = &[
    "py",
    "js",
    "mjs",
    "ts",
    "tsx",
    "jsx",
    "ps1",
    "sh",
    "bat",
    "md",
    "json",
    "jsonl",
    "yml",
    "yaml",
    "toml",
    "cfg",
    "ini",
    "txt",
    "csv",
    "htm",
    "html",
    "sql",
    "gz",
    "tgz",
    "zip",
    "exe",
    "dll",
    "gguf",
    "pt",
    "bin",
    "vhdx",
    "fx",
    "cpp",
    "cc",
    "h",
    "hpp",
    "rs",
    "go",
    "rb",
    "java",
    "log",
    "com",
    "io",
    "org",
    "net",
    "dev",
    "internal",
    "local",
    "localdomain",
];

/// `_CODE_OR_DATA_EXT.match(tail)` (`re.I`, anchored both ends).
fn code_or_data_ext(tail: &str) -> bool {
    CODE_OR_DATA_EXT.iter().any(|ext| {
        tail.chars().count() == ext.len() && tail.chars().zip(ext.chars()).all(|(c, p)| ci_eq(c, p))
    })
}

/// `_is_code_dotted(head, tail)`.
fn is_code_dotted(head: &str, tail: &str) -> bool {
    if tail.starts_with('_') || tail.chars().any(|c| c.is_ascii_uppercase() || c == '_') {
        return true;
    }
    head.chars()
        .last()
        .is_some_and(crate::dream::gates::py_isdigit)
        && tail
            .chars()
            .next()
            .is_some_and(crate::dream::gates::py_isdigit)
}

/// `_FILE_EXTENSION.search(s)`: `\.[A-Za-z0-9]{1,4}$` (`s` is stripped).
fn file_extension(s: &str) -> bool {
    let run = s
        .chars()
        .rev()
        .take_while(|c| c.is_ascii_alphanumeric())
        .count();
    (1..=4).contains(&run) && s[..s.len() - run].ends_with('.')
}

/// `_compound_halves`: split at the first spaced `/` or any `+`; both halves
/// non-empty with ASCII alphanumerics, neither ending in a dot-extension.
pub(crate) fn compound_halves(name: &str) -> Option<(String, String)> {
    let chars: Vec<(usize, char)> = name.char_indices().collect();
    let mut found = None;
    for (n, &(i, c)) in chars.iter().enumerate() {
        if c == '+' {
            found = Some((i, i + 1));
            break;
        }
        if py_isspace(c)
            && let (Some(&(_, '/')), Some(&(k, w))) = (chars.get(n + 1), chars.get(n + 2))
            && py_isspace(w)
        {
            found = Some((i, k + w.len_utf8()));
            break;
        }
    }
    let (start, end) = found?;
    let (a, b) = (py_strip(&name[..start]), py_strip(&name[end..]));
    if a.is_empty() || b.is_empty() {
        return None;
    }
    let alnum = |s: &str| s.chars().any(|c| c.is_ascii_alphanumeric());
    if !alnum(a) || !alnum(b) || file_extension(a) || file_extension(b) {
        return None;
    }
    Some((a.to_string(), b.to_string()))
}

/// One `junk_entities` finding.
#[derive(Debug, Clone)]
pub(crate) struct Junk {
    pub entity_id: i64,
    pub display: String,
    pub reason: &'static str,
}

/// `^\d+$` on a stripped display (`\d` is the Unicode decimal digit).
fn bare_number(d: &str) -> bool {
    !d.is_empty() && d.chars().all(crate::graph_review::is_decimal)
}

/// `junk_entities`: concat / list artifacts (any degree), compound and
/// slot-key artifacts over known names (any degree), then, at most
/// `max_degree`, bare numbers, displays of two characters or fewer, status
/// words and metric readings.
pub(crate) fn junk_entities(
    entities: &[Entity],
    deg: &BTreeMap<i64, usize>,
    max_degree: i64,
    known_norms: &HashSet<String>,
) -> Vec<Junk> {
    let mut out = Vec::new();
    for e in entities {
        let d = py_strip(&e.display);
        let mut push = |reason: &'static str| {
            out.push(Junk {
                entity_id: e.id,
                display: e.display.clone(),
                reason,
            });
        };
        if is_concat_artifact(d) {
            push("concat-artifact");
            continue;
        }
        if is_list_artifact(d) {
            push("list-artifact");
            continue;
        }
        if !known_norms.is_empty() {
            if let Some((ha, hb)) = compound_halves(d) {
                let (na, nb, nd) = (norm_name(&ha), norm_name(&hb), norm_name(d));
                if !na.is_empty()
                    && !nb.is_empty()
                    && na != nb
                    && na != nd
                    && nb != nd
                    && known_norms.contains(&na)
                    && known_norms.contains(&nb)
                {
                    push("compound-artifact");
                    continue;
                }
            }
            if let Some((head, tail)) = d.rsplit_once('.')
                && !head.is_empty()
                && !tail.is_empty()
                && !code_or_data_ext(tail)
                && !is_code_dotted(head, tail)
            {
                let nh = norm_name(head);
                if !nh.is_empty() && known_norms.contains(&nh) && nh != norm_name(d) {
                    push("slot-key-artifact");
                    continue;
                }
            }
        }
        if deg.get(&e.id).copied().unwrap_or(0) as i64 > max_degree {
            continue;
        }
        let reason = if bare_number(d) {
            "bare-number"
        } else if d.chars().count() <= 2 {
            "too-short"
        } else if JUNK_STOPWORDS.contains(&py_lower(d).as_str()) {
            "status-word"
        } else if is_metric_reading(d) {
            "metric-reading"
        } else {
            continue;
        };
        push(reason);
    }
    out
}

#[cfg(test)]
#[path = "graph_consolidation_tests.rs"]
mod tests;
