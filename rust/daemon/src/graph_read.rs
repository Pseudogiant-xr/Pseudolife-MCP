//! Read-model helpers; oracle: graph.py and memory/graph_store.py.
#![allow(dead_code)] // Shared read APIs; service wiring follows.
use std::collections::{BTreeMap, BTreeSet, VecDeque};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

#[derive(Deserialize)]
pub struct Entity {
    pub id: i64,
    pub canonical: String,
}

/// Canonicals win name collisions; aliases for missing entities are ignored.
pub fn alias_canonical_map(
    entities: &[Entity],
    aliases: &BTreeMap<i64, Vec<String>>,
) -> BTreeMap<String, String> {
    let by_id: BTreeMap<_, _> = entities.iter().map(|e| (e.id, &e.canonical)).collect();
    let canonicals: BTreeSet<_> = by_id.values().copied().collect();
    let mut result = BTreeMap::new();
    for (id, names) in aliases {
        if let Some(canonical) = by_id.get(id) {
            for name in names {
                if !canonicals.contains(name) || crate::mutants::active("graph-read-alias") {
                    result.insert(name.clone(), (*canonical).clone());
                }
            }
        }
    }
    result
}

#[derive(Deserialize)]
pub struct StoredEdge {
    pub src_id: i64,
    pub dst_id: i64,
}

#[derive(Deserialize)]
pub struct NamedEntity {
    pub id: i64,
    pub display: String,
}

pub fn degree_counts(edges: &[StoredEdge]) -> BTreeMap<i64, usize> {
    let mut result = BTreeMap::new();
    for edge in edges {
        let endpoints = [edge.src_id, edge.dst_id];
        let count = if edge.src_id == edge.dst_id && crate::mutants::active("graph-read-degree") {
            1
        } else {
            2
        };
        for &id in &endpoints[..count] {
            *result.entry(id).or_default() += 1;
        }
    }
    result
}

pub fn degrees_by_name(edges: &[StoredEdge], entities: &[NamedEntity]) -> BTreeMap<String, usize> {
    let names: BTreeMap<_, _> = entities.iter().map(|e| (e.id, &e.display)).collect();
    let counts = degree_counts(edges);
    let mut seen = BTreeSet::new();
    let mut result = BTreeMap::new();
    // Python's degree dict retains the first endpoint encounter order;
    // duplicate display names are overwritten in that order.
    for edge in edges {
        for id in [edge.src_id, edge.dst_id] {
            if seen.insert(id)
                && let Some(name) = names.get(&id)
            {
                result.insert((*name).clone(), counts[&id]);
            }
        }
    }
    result
}

type OrderedNeighbors = BTreeMap<i64, Vec<i64>>;

fn undirected(pairs: impl Iterator<Item = (i64, i64)>) -> OrderedNeighbors {
    let mut neighbors = OrderedNeighbors::new();
    for (src, dst) in pairs {
        for (node, next) in [(src, dst), (dst, src)] {
            let row = neighbors.entry(node).or_default();
            if !row.contains(&next) {
                row.push(next);
            }
        }
    }
    neighbors
}

fn expand(
    neighbors: &OrderedNeighbors,
    fringe: &mut Vec<i64>,
    visited: &mut BTreeMap<i64, Option<i64>>,
    other: &BTreeMap<i64, Option<i64>>,
) -> Option<i64> {
    for node in std::mem::take(fringe) {
        for &next in &neighbors[&node] {
            if let std::collections::btree_map::Entry::Vacant(entry) = visited.entry(next) {
                entry.insert(Some(node));
                fringe.push(next);
            }
            if other.contains_key(&next) {
                return Some(next);
            }
        }
    }
    None
}

// NetworkX expands the smaller fringe, forward on ties, retaining each
// adjacency list's insertion order. Subgraph path choice selects view nodes.
fn path(neighbors: &OrderedNeighbors, src: i64, dst: i64) -> Option<Vec<i64>> {
    if !neighbors.contains_key(&src) || !neighbors.contains_key(&dst) {
        return None;
    }
    if src == dst {
        return Some(vec![src]);
    }
    let mut forward = vec![src];
    let mut reverse = vec![dst];
    let mut pred = BTreeMap::from([(src, None)]);
    let mut succ = BTreeMap::from([(dst, None)]);
    while !forward.is_empty() && !reverse.is_empty() {
        let meeting = if forward.len() <= reverse.len() {
            expand(neighbors, &mut forward, &mut pred, &succ)
        } else {
            expand(neighbors, &mut reverse, &mut succ, &pred)
        };
        if let Some(meeting) = meeting {
            let mut result = vec![meeting];
            let mut node = meeting;
            while let Some(prior) = pred[&node] {
                result.push(prior);
                node = prior;
            }
            result.reverse();
            node = meeting;
            while let Some(next) = succ[&node] {
                result.push(next);
                node = next;
            }
            return Some(result);
        }
    }
    None
}

pub fn shortest_path(edges: &[StoredEdge], src: i64, dst: i64, max_hops: i64) -> Option<Vec<i64>> {
    if src == dst {
        return Some(vec![src]);
    }
    let neighbors = undirected(edges.iter().map(|e| (e.src_id, e.dst_id)));
    let result = path(&neighbors, src, dst)?;
    (max_hops >= 0 && result.len() - 1 <= max_hops as usize).then_some(result)
}

#[derive(Clone, Deserialize, Serialize)]
pub struct Edge {
    pub src: i64,
    pub relation: String,
    pub dst: i64,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Deserialize)]
pub struct Relation {
    #[serde(default)]
    pub transitive: bool,
    #[serde(default)]
    pub inverse_of: Option<String>,
}

pub type Relations = Vec<(String, Relation)>;

/// Keep the producer's registry order; SQL text ordering follows collation.
pub fn registry_from_value(value: &Value) -> Result<Relations, serde_json::Error> {
    let rows: Map<String, Value> = serde_json::from_value(value.clone())?;
    rows.into_iter()
        .map(|(name, meta)| serde_json::from_value(meta).map(|meta| (name, meta)))
        .collect()
}

type Triple = (i64, String, i64);

pub fn derive_edges(edges: &[Edge], relations: &Relations) -> Vec<Edge> {
    let base: BTreeSet<Triple> = edges
        .iter()
        .map(|e| (e.src, e.relation.clone(), e.dst))
        .collect();
    let mut derived = BTreeMap::<Triple, String>::new();
    let mut order = Vec::new();
    for (name, meta) in relations {
        if !meta.transitive || crate::mutants::active("graph-read-transitive") {
            continue;
        }
        let mut successors = OrderedNeighbors::new();
        let mut node_order = Vec::new();
        for edge in edges.iter().filter(|e| &e.relation == name) {
            for id in [edge.src, edge.dst] {
                if let std::collections::btree_map::Entry::Vacant(entry) = successors.entry(id) {
                    entry.insert(Vec::new());
                    node_order.push(id);
                }
            }
            let adjacent = successors.get_mut(&edge.src).unwrap();
            if !adjacent.contains(&edge.dst) {
                adjacent.push(edge.dst);
            }
        }
        // reflexive=False uses edge_bfs, not a sorted descendant set.
        // Its source and new-destination order can select subgraph path nodes.
        for src in node_order {
            let mut visited = BTreeSet::from([src]);
            let mut queue = VecDeque::from([src]);
            while let Some(node) = queue.pop_front() {
                for &dst in &successors[&node] {
                    if visited.insert(dst) {
                        queue.push_back(dst);
                    }
                    let key = (src, name.clone(), dst);
                    if src != dst && !base.contains(&key) && !derived.contains_key(&key) {
                        order.push(key.clone());
                        derived.insert(key, format!("transitive:{name}"));
                    }
                }
            }
        }
    }
    let mut inverse = BTreeMap::new();
    for (name, meta) in relations {
        if let Some(other) = &meta.inverse_of {
            inverse.insert(name.clone(), other.clone());
            inverse.entry(other.clone()).or_insert_with(|| name.clone());
        }
    }
    // Asserted sources precede transitive sources. Only asserted collisions
    // use the declared lexical rule; transitive sources retain producer order.
    let phases = [base.iter().cloned().collect::<Vec<_>>(), order.clone()];
    for (index, mut phase) in phases.into_iter().enumerate() {
        if index == 1 && crate::mutants::active("graph-read-inverse-closure") {
            continue;
        }
        if index == 0 {
            phase.sort_by(|a, b| (&a.1, a.0, a.2).cmp(&(&b.1, b.0, b.2)));
            if crate::mutants::active("graph-read-provenance") {
                phase.reverse();
            }
        }
        for (src, relation, dst) in phase {
            if let Some(mirror) = inverse.get(&relation) {
                let key = (dst, mirror.clone(), src);
                if !base.contains(&key)
                    && let std::collections::btree_map::Entry::Vacant(entry) = derived.entry(key)
                {
                    order.push(entry.key().clone());
                    entry.insert(format!("inverse:{relation}"));
                }
            }
        }
    }
    order
        .into_iter()
        .map(|key| {
            let via = derived.remove(&key).unwrap();
            let (src, relation, dst) = key;
            Edge {
                src,
                relation,
                dst,
                extra: Map::from_iter([
                    ("derived".to_owned(), json!(true)),
                    ("via".to_owned(), json!([via])),
                ]),
            }
        })
        .collect()
}

#[derive(Serialize)]
pub struct Subgraph {
    pub nodes: BTreeSet<i64>,
    pub edges: Vec<Edge>,
    pub paths: Vec<Vec<i64>>,
}

pub fn build_subgraph(
    edges: &[Edge],
    relations: &Relations,
    root: i64,
    depth: i64,
    to: Option<i64>,
) -> Subgraph {
    let mut all = edges.to_vec();
    for edge in &mut all {
        edge.extra.insert("derived".to_owned(), json!(false));
        edge.extra.insert("via".to_owned(), json!([]));
    }
    all.extend(derive_edges(edges, relations));
    let mut neighbors = undirected(all.iter().map(|e| (e.src, e.dst)));
    neighbors.entry(root).or_default();
    if crate::mutants::active("graph-read-path-order") {
        for adjacent in neighbors.values_mut() {
            adjacent.sort_unstable();
        }
    }
    let mut nodes = BTreeSet::from([root]);
    let mut queue = VecDeque::from([(root, 0)]);
    let depth = if crate::mutants::active("graph-read-depth") {
        1
    } else {
        depth.clamp(1, 3)
    };
    while let Some((node, distance)) = queue.pop_front() {
        if distance < depth {
            for &next in &neighbors[&node] {
                if nodes.insert(next) {
                    queue.push_back((next, distance + 1));
                }
            }
        }
    }
    let mut paths = Vec::new();
    if let Some(to) = to
        && let Some(found) = path(&neighbors, root, to)
    {
        nodes.extend(found.iter().copied());
        paths.push(found);
    }
    let edges = all
        .into_iter()
        .filter(|e| nodes.contains(&e.src) && nodes.contains(&e.dst))
        .collect();
    Subgraph {
        nodes,
        edges,
        paths,
    }
}

// SequenceMatcher's default matching-block score, with no user junk predicate.
// The query is sequence B, as in difflib.get_close_matches. Popular symbols
// are omitted from the search index at length 200, but can extend a match.
fn similarity(candidate: &str, query: &str) -> f64 {
    let a: Vec<_> = candidate.chars().collect();
    let b: Vec<_> = query.chars().collect();
    let length = a.len() + b.len();
    if length == 0 {
        return 1.0;
    }
    if 2.0 * a.len().min(b.len()) as f64 / (length as f64) < 0.5 {
        return 0.0;
    }
    let mut positions = BTreeMap::<char, Vec<usize>>::new();
    for (index, &c) in b.iter().enumerate() {
        positions.entry(c).or_default().push(index);
    }
    if b.len() >= 200 {
        let threshold = b.len() / 100 + 1;
        positions.retain(|_, indices| indices.len() <= threshold);
    }
    let mut regions = vec![(0, a.len(), 0, b.len())];
    let mut matched = 0;
    while let Some((alo, ahi, blo, bhi)) = regions.pop() {
        let (mut first, mut second, mut size) = (alo, blo, 0);
        let mut previous = BTreeMap::new();
        for (i, c) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut current = BTreeMap::new();
            if let Some(indices) = positions.get(c) {
                for &j in indices
                    .iter()
                    .filter(|&&j| j >= blo)
                    .take_while(|&&j| j < bhi)
                {
                    let count = j
                        .checked_sub(1)
                        .and_then(|k| previous.get(&k))
                        .copied()
                        .unwrap_or(0)
                        + 1;
                    current.insert(j, count);
                    if count > size {
                        (first, second, size) = (i + 1 - count, j + 1 - count, count);
                    }
                }
            }
            previous = current;
        }
        while first > alo && second > blo && a[first - 1] == b[second - 1] {
            first -= 1;
            second -= 1;
            size += 1;
        }
        while first + size < ahi && second + size < bhi && a[first + size] == b[second + size] {
            size += 1;
        }
        if size > 0 {
            matched += size;
            if alo < first && blo < second {
                regions.push((alo, first, blo, second));
            }
            if first + size < ahi && second + size < bhi {
                regions.push((first + size, ahi, second + size, bhi));
            }
        }
    }
    2.0 * matched as f64 / length as f64
}

pub fn resolve_relation(known: &[String], raw: &str) -> (Option<String>, Vec<String>) {
    let name = crate::storage::graph::norm_name(raw);
    if known.contains(&name) {
        return (Some(name), Vec::new());
    }
    let mut suggestions: Vec<_> = known
        .iter()
        .filter_map(|candidate| {
            let score = similarity(candidate, &name);
            (score >= 0.5).then_some((score, candidate))
        })
        .collect();
    suggestions.sort_by(|a, b| {
        b.0.total_cmp(&a.0).then_with(|| {
            if crate::mutants::active("graph-read-suggestions") {
                a.1.cmp(b.1)
            } else {
                b.1.cmp(a.1)
            }
        })
    });
    (
        None,
        suggestions
            .into_iter()
            .take(3)
            .map(|(_, name)| name.clone())
            .collect(),
    )
}
