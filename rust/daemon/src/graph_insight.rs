//! The graph-insight refresh every `memory_dream run` ends with
//! (`service_dream.py` `_refresh_graph_insight` / `_safe_refresh_graph_insight`
//! 3313-3352 and `_contested_facts` 3285-3311) over the pure analytics of
//! `memory/graph_insight.py`: communities over the asserted edges, their
//! summaries, and the `graph_digest` meta snapshot (god nodes, surprising
//! connections, suggested questions).
//!
//! Option B (maintainer decision, 2026-10-19): the communities and the
//! betweenness behind the god nodes and the bridges come from a
//! deterministic algorithm, not from NetworkX's seeded RNG.
//!
//! * Communities: NetworkX 3.6.1's Louvain (`louvain_partitions`,
//!   `_one_level`, `_gen_graph`, `modularity`), operation for operation,
//!   except that each level visits its nodes in graph order instead of
//!   `seed.shuffle`'s order. Ties keep the first community in adjacency
//!   order, as `gain > best_mod` does. The oversized-community split runs
//!   on the subgraph in its parent's node and adjacency order (NetworkX's
//!   subgraph view iterates a member set's hash order when the community is
//!   under half the graph).
//! * Betweenness: exact Brandes in NetworkX's order (BFS over insertion
//!   ordered adjacency, `_accumulate_basic`, normalized `_rescale`), so it
//!   is bit-identical to Python's wherever Python is exact (at most
//!   `betweenness_sample` nodes, or a sample of 0); above that Python
//!   estimates from 200 seeded sources and this side stays exact.
//!
//! Everything downstream of those two (re-indexing, the remap onto the
//! previous ids, summaries, cohesion, god nodes, surprises, questions, the
//! digest) is a direct port. `graph_insight_tests.rs` checks both halves
//! against Python: byte-exact against NetworkX with the shuffle disabled,
//! structurally against NetworkX with its seed.
//!
//! Derived state: the `communities` and `entity_communities` tables and
//! `meta.graph_digest` are a snapshot rebuilt wholesale on every dream run;
//! nothing invalidates them on a graph write (a relate, a merge, a deep
//! apply, the dream's own relations), exactly as in Python, where readers
//! (`memory_graph`'s community, `memory_stats`) show the last dream's view.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::sync::Arc;

use indexmap::IndexMap;
use serde_json::{Value, json};

use crate::config::GraphInsightConfig;
use crate::graph_consolidation::{py_float_cmp, py_round};
use crate::service::{Ready, Service};

/// `louvain_communities`' default `threshold`.
const LOUVAIN_THRESHOLD: f64 = 0.000_000_1;
/// `_MIN_SPLIT_SIZE`.
const MIN_SPLIT_SIZE: i64 = 10;
/// `_LOW_CONFIDENCE`, `_PERIPHERAL_DEGREE`.
const LOW_CONFIDENCE: f64 = 0.6;
const PERIPHERAL_DEGREE: i64 = 2;
/// `_LOW_COHESION`, `_MIN_COHESION_SIZE`, `_AGENT_EDGE_MIN`.
const LOW_COHESION: f64 = 0.15;
const MIN_COHESION_SIZE: usize = 5;
const AGENT_EDGE_MIN: usize = 2;

/// An entity row (`load_graph()["entities"]`); `display` is NOT NULL.
#[derive(Debug, Clone, PartialEq)]
pub struct InsightEntity {
    pub id: i64,
    pub display: String,
}

/// An asserted, live edge (`load_graph()["edges"]`, in id order).
#[derive(Debug, Clone, PartialEq)]
pub struct InsightEdge {
    pub src: i64,
    pub dst: i64,
    pub relation: String,
    pub confidence: f64,
    pub origin: Option<String>,
}

/// A contested slot (`_contested_facts`).
#[derive(Debug, Clone, PartialEq)]
pub struct Contested {
    pub entity: String,
    pub attribute: String,
    pub value: String,
    pub contender_value: String,
    pub contender_origin: String,
}

/// A community summary (`summarize_communities`).
#[derive(Debug, Clone, PartialEq)]
pub struct Summary {
    pub id: i64,
    pub label: String,
    pub size: usize,
    pub cohesion: f64,
}

// ---------------------------------------------------------------------------
// Weighted undirected graph with NetworkX's iteration order
// ---------------------------------------------------------------------------

/// An `nx.Graph` over nodes `0..n` (their order is the graph's node order),
/// each adjacency kept in insertion order, as a dict keeps it.
#[derive(Debug, Clone, Default)]
struct WGraph {
    adj: Vec<IndexMap<usize, f64>>,
}

impl WGraph {
    fn with_nodes(n: usize) -> Self {
        WGraph {
            adj: vec![IndexMap::new(); n],
        }
    }

    fn len(&self) -> usize {
        self.adj.len()
    }

    /// `add_edge(u, v, weight=w)`: a new neighbour is appended, an existing
    /// one keeps its place and takes the new weight; a self-loop is one
    /// entry.
    fn add_edge(&mut self, u: usize, v: usize, w: f64) {
        self.adj[u].insert(v, w);
        self.adj[v].insert(u, w);
    }

    fn weight(&self, u: usize, v: usize) -> Option<f64> {
        self.adj[u].get(&v).copied()
    }

    /// `G.edges(data="weight")`: each node in order, each neighbour in
    /// adjacency order, every edge once (from the endpoint seen first).
    fn edges(&self) -> Vec<(usize, usize, f64)> {
        let mut seen = vec![false; self.len()];
        let mut out = Vec::new();
        for (n, nbrs) in self.adj.iter().enumerate() {
            for (&nbr, &w) in nbrs {
                if !seen[nbr] {
                    out.push((n, nbr, w));
                }
            }
            seen[n] = true;
        }
        out
    }

    fn number_of_edges(&self) -> usize {
        self.edges().len()
    }

    /// `G.degree(u, weight="weight")`: a self-loop counts twice.
    fn degree(&self, u: usize) -> f64 {
        let mut d: f64 = self.adj[u].values().sum();
        if let Some(w) = self.adj[u].get(&u) {
            d += w;
        }
        d
    }
}

/// `_undirected(edges)`: nodes in order of first appearance, every edge
/// weighted 1 (NetworkX leaves the attribute unset; every reader defaults
/// it to 1).
struct Undirected {
    ids: Vec<i64>,
    g: WGraph,
}

fn undirected(edges: &[InsightEdge]) -> Undirected {
    let mut ids = Vec::new();
    let mut index = HashMap::new();
    let mut g = WGraph::default();
    let mut node = |id: i64, g: &mut WGraph| -> usize {
        *index.entry(id).or_insert_with(|| {
            ids.push(id);
            g.adj.push(IndexMap::new());
            g.adj.len() - 1
        })
    };
    for e in edges {
        let u = node(e.src, &mut g);
        let v = node(e.dst, &mut g);
        g.add_edge(u, v, 1.0);
    }
    Undirected { ids, g }
}

// ---------------------------------------------------------------------------
// Louvain (NetworkX 3.6.1, no shuffle)
// ---------------------------------------------------------------------------

/// `nx.community.modularity(G, communities, resolution=...)` (undirected).
/// The sum runs left to right from 0, as CPython 3.11's `sum()` does
/// (3.12 compensates; the value only meets the 1e-7 stopping test).
fn modularity(g: &WGraph, communities: &[BTreeSet<usize>], resolution: f64) -> f64 {
    let degree: Vec<f64> = (0..g.len()).map(|u| g.degree(u)).collect();
    let deg_sum: f64 = degree.iter().sum();
    let m = deg_sum / 2.0;
    let norm = 1.0 / (deg_sum * deg_sum);
    let mut total = 0.0;
    for comm in communities {
        let mut l_c = 0.0;
        let mut seen: HashSet<usize> = HashSet::new();
        for &n in comm {
            for (&nbr, &w) in &g.adj[n] {
                if !seen.contains(&nbr) && comm.contains(&nbr) {
                    l_c += w;
                }
            }
            seen.insert(n);
        }
        let out: f64 = comm.iter().map(|&u| degree[u]).sum();
        total += l_c / m - resolution * out * out * norm;
    }
    total
}

/// One `_one_level` pass. `members[u]` is `G.nodes[u].get("nodes", {u})`:
/// the original nodes an aggregated node stands for.
fn one_level(
    g: &WGraph,
    members: &[BTreeSet<usize>],
    m: f64,
    mut partition: Vec<BTreeSet<usize>>,
    resolution: f64,
) -> (Vec<BTreeSet<usize>>, Vec<BTreeSet<usize>>, bool) {
    let n = g.len();
    let mut node2com: Vec<usize> = (0..n).collect();
    let mut inner: Vec<BTreeSet<usize>> = (0..n).map(|u| BTreeSet::from([u])).collect();
    let degrees: Vec<f64> = (0..n).map(|u| g.degree(u)).collect();
    let mut stot = degrees.clone();
    let nbrs: Vec<Vec<(usize, f64)>> = (0..n)
        .map(|u| {
            g.adj[u]
                .iter()
                .filter(|&(&v, _)| v != u)
                .map(|(&v, &w)| (v, w))
                .collect()
        })
        .collect();
    let two_m2 = 2.0 * (m * m);
    let mut improvement = false;
    let mut nb_moves = 1;
    while nb_moves > 0 {
        nb_moves = 0;
        // `seed.shuffle(rand_nodes)` in NetworkX; graph order here.
        for u in 0..n {
            let mut best_mod = 0.0;
            let mut best_com = node2com[u];
            // `_neighbor_weights`: a defaultdict in first-seen order.
            let mut weights2com: IndexMap<usize, f64> = IndexMap::new();
            for &(nbr, wt) in &nbrs[u] {
                *weights2com.entry(node2com[nbr]).or_insert(0.0) += wt;
            }
            let degree = degrees[u];
            stot[best_com] -= degree;
            // Reading the defaultdict inserts the node's own community last
            // when no neighbour shares it, so the loop below also scores
            // staying put.
            let own = *weights2com.entry(best_com).or_insert(0.0);
            let remove_cost = -own / m + resolution * (stot[best_com] * degree) / two_m2;
            for (&nbr_com, &wt) in &weights2com {
                let gain = remove_cost + wt / m - resolution * (stot[nbr_com] * degree) / two_m2;
                if gain > best_mod {
                    best_mod = gain;
                    best_com = nbr_com;
                }
            }
            stot[best_com] += degree;
            if best_com != node2com[u] {
                let from = node2com[u];
                for x in &members[u] {
                    partition[from].remove(x);
                }
                inner[from].remove(&u);
                partition[best_com].extend(members[u].iter().copied());
                inner[best_com].insert(u);
                improvement = true;
                nb_moves += 1;
                node2com[u] = best_com;
            }
        }
    }
    partition.retain(|s| !s.is_empty());
    inner.retain(|s| !s.is_empty());
    (partition, inner, improvement)
}

/// `_gen_graph`: one node per community (in partition order), edge weights
/// summed, intra-community weight on a self-loop.
fn gen_graph(
    g: &WGraph,
    members: &[BTreeSet<usize>],
    partition: &[BTreeSet<usize>],
) -> (WGraph, Vec<BTreeSet<usize>>) {
    let mut node2com = vec![0usize; g.len()];
    let mut nodes = Vec::with_capacity(partition.len());
    for (i, part) in partition.iter().enumerate() {
        let mut all = BTreeSet::new();
        for &node in part {
            node2com[node] = i;
            all.extend(members[node].iter().copied());
        }
        nodes.push(all);
    }
    let mut h = WGraph::with_nodes(partition.len());
    for (n1, n2, wt) in g.edges() {
        let (c1, c2) = (node2com[n1], node2com[n2]);
        let temp = h.weight(c1, c2).unwrap_or(0.0);
        h.add_edge(c1, c2, wt + temp);
    }
    (h, nodes)
}

/// `louvain_communities(G, resolution=..., threshold=1e-7)`: the last
/// level's partition, as sets of `G`'s nodes.
fn louvain(g: &WGraph, resolution: f64) -> Vec<BTreeSet<usize>> {
    let n = g.len();
    let mut partition: Vec<BTreeSet<usize>> = (0..n).map(|u| BTreeSet::from([u])).collect();
    if g.number_of_edges() == 0 {
        return partition;
    }
    let mut modv = modularity(g, &partition, resolution);
    // `graph.add_weighted_edges_from(G.edges(data=weight, default=1))`.
    let mut graph = WGraph::with_nodes(n);
    for (u, v, _) in g.edges() {
        graph.add_edge(u, v, 1.0);
    }
    let m: f64 = (0..n).map(|u| graph.degree(u)).sum::<f64>() / 2.0;
    let mut members: Vec<BTreeSet<usize>> = (0..n).map(|u| BTreeSet::from([u])).collect();
    let (p, mut inner, _) = one_level(&graph, &members, m, partition, resolution);
    partition = p;
    loop {
        // `yield`: the caller keeps the last partition yielded.
        let last = partition.clone();
        let new_mod = modularity(&graph, &inner, resolution);
        if new_mod - modv <= LOUVAIN_THRESHOLD {
            return last;
        }
        modv = new_mod;
        let (h, hm) = gen_graph(&graph, &members, &inner);
        graph = h;
        members = hm;
        let (p, i, improvement) = one_level(&graph, &members, m, partition, resolution);
        if !improvement {
            // `while improvement` ends without yielding this level, whose
            // partition equals the one yielded before it.
            return last;
        }
        partition = p;
        inner = i;
    }
}

/// `g.subgraph(members)` in its parent's order (see the module comment).
fn subgraph(g: &WGraph, members: &BTreeSet<usize>) -> (WGraph, Vec<usize>) {
    let order: Vec<usize> = (0..g.len()).filter(|u| members.contains(u)).collect();
    let local: HashMap<usize, usize> = order.iter().enumerate().map(|(i, &u)| (u, i)).collect();
    let mut sub = WGraph::with_nodes(order.len());
    for (i, &u) in order.iter().enumerate() {
        for (&v, &w) in &g.adj[u] {
            if let Some(&j) = local.get(&v) {
                sub.adj[i].insert(j, w);
            }
        }
    }
    (sub, order)
}

/// `detect_communities`: community id (the index) -> sorted entity ids.
pub fn detect_communities(
    edges: &[InsightEdge],
    resolution: f64,
    max_community_fraction: f64,
) -> Vec<Vec<i64>> {
    if edges.is_empty() {
        return Vec::new();
    }
    let u = undirected(edges);
    let n = u.ids.len();
    let groups = louvain(&u.g, resolution);
    // `int(n * max_community_fraction)` truncates toward zero.
    let max_size = MIN_SPLIT_SIZE.max((n as f64 * max_community_fraction) as i64);
    let mut out: Vec<Vec<usize>> = Vec::new();
    for members in groups {
        if members.len() as i64 > max_size {
            let (sub, order) = subgraph(&u.g, &members);
            if sub.number_of_edges() > 0 {
                let parts = louvain(&sub, resolution);
                if parts.len() > 1 {
                    out.extend(
                        parts
                            .into_iter()
                            .map(|p| p.into_iter().map(|i| order[i]).collect()),
                    );
                    continue;
                }
            }
        }
        out.push(members.into_iter().collect());
    }
    reindex(
        out.into_iter()
            .map(|g| g.into_iter().map(|i| u.ids[i]).collect())
            .collect(),
    )
}

/// `_reindex`: size descending, then the sorted ids.
fn reindex(groups: Vec<Vec<i64>>) -> Vec<Vec<i64>> {
    let mut groups: Vec<Vec<i64>> = groups
        .into_iter()
        .map(|mut g| {
            g.sort_unstable();
            g
        })
        .collect();
    groups.sort_by(|a, b| b.len().cmp(&a.len()).then_with(|| a.cmp(b)));
    groups
}

// ---------------------------------------------------------------------------
// Betweenness (exact, NetworkX's order)
// ---------------------------------------------------------------------------

/// `betweenness_centrality(g)` with `k=None`, `normalized=True`,
/// `endpoints=False`, by node index; empty for an edgeless graph.
fn betweenness(g: &WGraph) -> Vec<f64> {
    let n = g.len();
    if g.number_of_edges() == 0 {
        return Vec::new();
    }
    // Adjacency as plain lists (same order), and per-source buffers reused
    // across sources: allocation, not arithmetic, dominated (measured
    // 2026-10-25, 5,000 entities / 10,000 edges: 4.7 s fresh, see
    // `graph_insight_bank_scale_timing`). Reuse keeps every sum's order.
    let adj: Vec<Vec<usize>> = g.adj.iter().map(|a| a.keys().copied().collect()).collect();
    let mut bc = vec![0.0f64; n];
    let mut stack: Vec<usize> = Vec::with_capacity(n);
    let mut pred: Vec<Vec<usize>> = vec![Vec::new(); n];
    let mut sigma = vec![0.0f64; n];
    let mut dist = vec![-1i64; n];
    let mut delta = vec![0.0f64; n];
    let mut queue: VecDeque<usize> = VecDeque::with_capacity(n);
    for s in 0..n {
        // `_single_source_shortest_path_basic`.
        for &v in &stack {
            pred[v].clear();
            sigma[v] = 0.0;
            dist[v] = -1;
            delta[v] = 0.0;
        }
        stack.clear();
        sigma[s] = 1.0;
        dist[s] = 0;
        queue.push_back(s);
        while let Some(v) = queue.pop_front() {
            stack.push(v);
            let dv = dist[v];
            let sigmav = sigma[v];
            for &w in &adj[v] {
                if dist[w] < 0 {
                    queue.push_back(w);
                    dist[w] = dv + 1;
                }
                if dist[w] == dv + 1 {
                    sigma[w] += sigmav;
                    pred[w].push(v);
                }
            }
        }
        // `_accumulate_basic`, walking the stack from its top; the reset at
        // the next source reads the same stack.
        for &w in stack.iter().rev() {
            let coeff = (1.0 + delta[w]) / sigma[w];
            for &v in &pred[w] {
                delta[v] += sigma[v] * coeff;
            }
            if w != s {
                bc[w] += delta[w];
            }
        }
    }
    // `_rescale`: N = n - 1 (no endpoints), every source taken.
    let big_n = n as i64 - 1;
    if big_n >= 2 {
        let scale = 1.0 / ((big_n * (big_n - 1)) as f64);
        if scale != 1.0 {
            for b in &mut bc {
                *b *= scale;
            }
        }
    }
    bc
}

// ---------------------------------------------------------------------------
// The direct ports
// ---------------------------------------------------------------------------

/// `graph.degree_counts`: undirected degree over asserted edges (a
/// self-loop adds two).
fn degree_counts(edges: &[InsightEdge]) -> HashMap<i64, i64> {
    let mut deg = HashMap::new();
    for e in edges {
        *deg.entry(e.src).or_insert(0) += 1;
        *deg.entry(e.dst).or_insert(0) += 1;
    }
    deg
}

/// Python's `seq[:n]` length for a list of `len` items.
fn py_head(len: usize, n: i64) -> usize {
    if n >= 0 {
        (n as usize).min(len)
    } else {
        len.saturating_sub(n.unsigned_abs() as usize)
    }
}

/// `cohesion_score`.
fn cohesion_score(edges: &[InsightEdge], ids: &[i64]) -> f64 {
    let n = ids.len();
    if n <= 1 {
        return 1.0;
    }
    let members: HashSet<i64> = ids.iter().copied().collect();
    let actual: HashSet<(i64, i64)> = edges
        .iter()
        .filter(|e| members.contains(&e.src) && members.contains(&e.dst) && e.src != e.dst)
        .map(|e| (e.src.min(e.dst), e.src.max(e.dst)))
        .collect();
    let possible = (n * (n - 1)) as f64 / 2.0;
    if possible != 0.0 {
        actual.len() as f64 / possible
    } else {
        0.0
    }
}

/// `remap_to_previous`: final community id -> sorted ids.
pub fn remap_to_previous(
    communities: &[Vec<i64>],
    prior: &BTreeMap<i64, i64>,
) -> BTreeMap<i64, Vec<i64>> {
    if prior.is_empty() {
        return communities
            .iter()
            .enumerate()
            .map(|(c, ids)| (c as i64, ids.clone()))
            .collect();
    }
    let mut old_sets: BTreeMap<i64, HashSet<i64>> = BTreeMap::new();
    for (&node, &oid) in prior {
        old_sets.entry(oid).or_default().insert(node);
    }
    let mut overlaps: Vec<(usize, i64, usize)> = Vec::new();
    for (new_cid, ids) in communities.iter().enumerate() {
        for (&old_cid, old) in &old_sets {
            let inter = ids.iter().filter(|i| old.contains(i)).count();
            if inter > 0 {
                overlaps.push((inter, old_cid, new_cid));
            }
        }
    }
    overlaps.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));
    let mut new_to_final: HashMap<usize, i64> = HashMap::new();
    let mut used_old: HashSet<i64> = HashSet::new();
    let mut matched_new: HashSet<usize> = HashSet::new();
    for &(_, old_cid, new_cid) in &overlaps {
        if used_old.contains(&old_cid) || matched_new.contains(&new_cid) {
            continue;
        }
        new_to_final.insert(new_cid, old_cid);
        used_old.insert(old_cid);
        matched_new.insert(new_cid);
    }
    let mut unmatched: Vec<usize> = (0..communities.len())
        .filter(|c| !matched_new.contains(c))
        .collect();
    unmatched.sort_by(|&a, &b| {
        communities[b]
            .len()
            .cmp(&communities[a].len())
            .then_with(|| communities[a].cmp(&communities[b]))
    });
    let mut nxt: i64 = 0;
    for new_cid in unmatched {
        while used_old.contains(&nxt) {
            nxt += 1;
        }
        new_to_final.insert(new_cid, nxt);
        used_old.insert(nxt);
        nxt += 1;
    }
    communities
        .iter()
        .enumerate()
        .map(|(c, ids)| (new_to_final[&c], ids.clone()))
        .collect()
}

/// `summarize_communities`, by community id.
pub fn summarize_communities(
    communities: &BTreeMap<i64, Vec<i64>>,
    edges: &[InsightEdge],
    disp: &HashMap<i64, String>,
) -> Vec<Summary> {
    let deg = degree_counts(edges);
    communities
        .iter()
        .map(|(&cid, ids)| {
            // `max(ids, key=(deg, id))`: ids are distinct, so a strict max.
            let label = match ids
                .iter()
                .max_by_key(|&&i| (deg.get(&i).copied().unwrap_or(0), i))
            {
                Some(top) => disp.get(top).cloned().unwrap_or_else(|| top.to_string()),
                None => format!("community-{cid}"),
            };
            Summary {
                id: cid,
                label,
                size: ids.len(),
                cohesion: py_round(cohesion_score(edges, ids), 4),
            }
        })
        .collect()
}

fn display(disp: &HashMap<i64, String>, id: i64) -> String {
    disp.get(&id).cloned().unwrap_or_else(|| id.to_string())
}

/// `god_nodes(edges, entities, top_n=...)` over the betweenness by entity.
fn god_nodes(
    edges: &[InsightEdge],
    disp: &HashMap<i64, String>,
    bc: &HashMap<i64, f64>,
    top_n: i64,
) -> Vec<(i64, String, i64, f64)> {
    let deg = degree_counts(edges);
    let mut ranked: Vec<(i64, i64)> = deg.into_iter().collect();
    let b = |id: i64| bc.get(&id).copied().unwrap_or(0.0);
    ranked.sort_by(|x, y| {
        py_float_cmp(b(y.0), b(x.0))
            .then(y.1.cmp(&x.1))
            .then(x.0.cmp(&y.0))
    });
    let take = py_head(ranked.len(), top_n);
    ranked
        .into_iter()
        .take(take)
        .map(|(id, d)| (id, display(disp, id), d, py_round(b(id), 4)))
        .collect()
}

/// `_god_degree_cutoff`.
fn god_degree_cutoff(deg: &HashMap<i64, i64>) -> i64 {
    if deg.is_empty() {
        return 5;
    }
    let mut top: Vec<i64> = deg.values().copied().collect();
    top.sort_unstable_by(|a, b| b.cmp(a));
    5.max(top[(top.len() - 1).min(9)])
}

/// `surprising_connections`.
fn surprising_connections(
    edges: &[InsightEdge],
    disp: &HashMap<i64, String>,
    node_community: &HashMap<i64, i64>,
    top_n: i64,
) -> Vec<Value> {
    let deg = degree_counts(edges);
    let cutoff = god_degree_cutoff(&deg);
    struct Scored<'a> {
        src: String,
        dst: String,
        edge: &'a InsightEdge,
        score: i64,
        why: String,
        pair: Option<(i64, i64)>,
    }
    let mut scored: Vec<Scored> = Vec::new();
    for e in edges {
        let (u, v) = (e.src, e.dst);
        if u == v {
            continue;
        }
        let mut score = 0;
        let mut reasons: Vec<String> = Vec::new();
        if e.confidence < LOW_CONFIDENCE || e.origin.as_deref() == Some("agent") {
            score += 2;
            reasons.push("agent-inferred or low-confidence".into());
        }
        let (cu, cv) = (node_community.get(&u), node_community.get(&v));
        let cross = matches!((cu, cv), (Some(a), Some(b)) if a != b);
        if cross {
            score += 3;
            reasons.push(format!(
                "bridge between community {} and {}",
                cu.unwrap(),
                cv.unwrap()
            ));
        }
        let du = deg.get(&u).copied().unwrap_or(0);
        let dv = deg.get(&v).copied().unwrap_or(0);
        if du.min(dv) <= PERIPHERAL_DEGREE && du.max(dv) >= cutoff {
            score += 1;
            reasons.push("peripheral node reaches a hub".into());
        }
        if score <= 0 {
            continue;
        }
        let pair = cross.then(|| {
            let (a, b) = (*cu.unwrap(), *cv.unwrap());
            (a.min(b), a.max(b))
        });
        scored.push(Scored {
            src: display(disp, u),
            dst: display(disp, v),
            edge: e,
            score,
            why: reasons.join("; "),
            pair,
        });
    }
    scored.sort_by(|a, b| {
        b.score
            .cmp(&a.score)
            .then_with(|| a.src.cmp(&b.src))
            .then_with(|| a.dst.cmp(&b.dst))
    });
    let mut seen: HashSet<(i64, i64)> = HashSet::new();
    let mut out = Vec::new();
    for s in scored {
        if let Some(pair) = s.pair
            && !seen.insert(pair)
        {
            continue;
        }
        out.push(json!({
            "src": s.src, "dst": s.dst, "relation": s.edge.relation,
            "confidence": s.edge.confidence, "origin": s.edge.origin,
            "score": s.score, "why": s.why,
        }));
        // Python appends, then stops once `len(out) >= top_n`: a top_n of
        // zero or less still answers one.
        if out.len() as i64 >= top_n {
            break;
        }
    }
    out
}

/// `suggest_questions`.
#[allow(clippy::too_many_arguments)]
fn suggest_questions(
    edges: &[InsightEdge],
    entities: &[InsightEntity],
    disp: &HashMap<i64, String>,
    node_community: &HashMap<i64, i64>,
    contested: &[Contested],
    summaries: &[Summary],
    graph: &Undirected,
    bc_by_index: &[f64],
    bc: &HashMap<i64, f64>,
    top_n: i64,
) -> Vec<Value> {
    let deg = degree_counts(edges);
    let label: HashMap<i64, &str> = summaries.iter().map(|s| (s.id, s.label.as_str())).collect();
    let mut questions: Vec<Value> = Vec::new();

    // 1. contested facts
    for c in contested {
        questions.push(json!({
            "type": "contested_fact",
            "question": format!(
                "Which value of `{}` for `{}` is correct \u{2014} `{}` or `{}`?",
                c.attribute, c.entity, c.value, c.contender_value
            ),
            "why": format!("Contested fact; rival from origin={}.", c.contender_origin),
        }));
    }

    // 2. bridge entities (positive betweenness, spanning other communities)
    if !bc_by_index.is_empty() {
        let mut bridges: Vec<(usize, f64)> = bc_by_index
            .iter()
            .enumerate()
            .filter(|&(_, &s)| s > 0.0)
            .map(|(i, &s)| (i, s))
            .collect();
        bridges.sort_by(|a, b| {
            py_float_cmp(b.1, a.1).then_with(|| graph.ids[a.0].cmp(&graph.ids[b.0]))
        });
        for &(i, _) in bridges.iter().take(3) {
            let n = graph.ids[i];
            let cid = node_community.get(&n).copied();
            let other: BTreeSet<i64> = graph.g.adj[i]
                .keys()
                .filter_map(|&nb| node_community.get(&graph.ids[nb]).copied())
                .filter(|&c| Some(c) != cid)
                .collect();
            let nd = display(disp, n);
            let mut names: Vec<String> = Vec::new();
            for c in other {
                let lab = label
                    .get(&c)
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| format!("community {c}"));
                if lab != nd && !names.contains(&lab) {
                    names.push(lab);
                }
            }
            if !names.is_empty() {
                let targets: Vec<String> = names.iter().map(|x| format!("`{x}`")).collect();
                questions.push(json!({
                    "type": "bridge_entity",
                    "question": format!("Why is `{nd}` a bridge to {}?", targets.join(", ")),
                    "why": "High betweenness \u{2014} a cross-community bridge.",
                }));
            }
        }
    }

    // 3. a god node with several agent-origin edges
    for (eid, gdisp, _, _) in god_nodes(edges, disp, bc, 5) {
        let agent_n = edges
            .iter()
            .filter(|e| (e.src == eid || e.dst == eid) && e.origin.as_deref() == Some("agent"))
            .count();
        if agent_n >= AGENT_EDGE_MIN {
            questions.push(json!({
                "type": "verify_inferred",
                "question": format!(
                    "Are the {agent_n} inferred relationships involving `{gdisp}` correct?"
                ),
                "why": format!(
                    "{agent_n} agent-origin (dream-inferred) edges need verification."
                ),
            }));
            break;
        }
    }

    // 4. isolated entities (degree <= 1)
    let isolated: Vec<&str> = entities
        .iter()
        .filter(|e| deg.get(&e.id).copied().unwrap_or(0) <= 1)
        .map(|e| e.display.as_str())
        .collect();
    if !isolated.is_empty() {
        let shown: Vec<String> = isolated.iter().take(3).map(|n| format!("`{n}`")).collect();
        questions.push(json!({
            "type": "isolated_entity",
            "question": format!("What connects {} to the rest of the graph?", shown.join(", ")),
            "why": format!("{} weakly-connected entities \u{2014} possible gaps.", isolated.len()),
        }));
    }

    // 5. low-cohesion communities
    for s in summaries {
        if s.cohesion < LOW_COHESION && s.size >= MIN_COHESION_SIZE {
            questions.push(json!({
                "type": "low_cohesion",
                "question": format!(
                    "Should community `{}` be split into tighter groups?",
                    s.label
                ),
                "why": format!(
                    "Cohesion {} over {} entities.",
                    crate::pyjson::float_repr(s.cohesion),
                    s.size
                ),
            }));
        }
    }

    let take = py_head(questions.len(), top_n);
    questions.truncate(take);
    questions
}

/// The computed refresh: the summaries, the entity assignment and the
/// `graph_digest` value.
pub struct Insight {
    pub summaries: Vec<Summary>,
    pub assignment: Vec<(i64, i64)>,
    pub digest: Value,
}

/// Everything `_refresh_graph_insight` computes between its two locked
/// windows: `detect_communities`, `remap_to_previous`,
/// `summarize_communities` and `build_digest`.
pub fn compute(
    cfg: &GraphInsightConfig,
    entities: &[InsightEntity],
    edges: &[InsightEdge],
    prior: &BTreeMap<i64, i64>,
    contested: &[Contested],
    computed_at: f64,
) -> Insight {
    let detected = detect_communities(edges, cfg.resolution, cfg.max_community_fraction);
    let no_prior = BTreeMap::new();
    let prior = if crate::mutants::active("mcp-dream-insight-no-remap") {
        &no_prior
    } else {
        prior
    };
    let communities = remap_to_previous(&detected, prior);
    let disp: HashMap<i64, String> = entities.iter().map(|e| (e.id, e.display.clone())).collect();
    let summaries = summarize_communities(&communities, edges, &disp);
    let assignment: Vec<(i64, i64)> = communities
        .iter()
        .flat_map(|(&cid, ids)| ids.iter().map(move |&eid| (eid, cid)))
        .collect();
    let node_community: HashMap<i64, i64> = assignment.iter().copied().collect();
    let graph = undirected(edges);
    let bc_by_index = betweenness(&graph.g);
    let bc: HashMap<i64, f64> = bc_by_index
        .iter()
        .enumerate()
        .map(|(i, &b)| (graph.ids[i], b))
        .collect();
    let gods: Vec<Value> = god_nodes(edges, &disp, &bc, cfg.god_nodes_top_n)
        .into_iter()
        .map(|(id, d, degree, b)| {
            json!({"entity_id": id, "display": d, "degree": degree, "betweenness": b})
        })
        .collect();
    let surprises = surprising_connections(edges, &disp, &node_community, cfg.surprises_top_n);
    let questions = suggest_questions(
        edges,
        entities,
        &disp,
        &node_community,
        contested,
        &summaries,
        &graph,
        &bc_by_index,
        &bc,
        cfg.questions_top_n,
    );
    let community_values: Vec<Value> = summaries
        .iter()
        .map(|s| json!({"id": s.id, "label": s.label, "size": s.size, "cohesion": s.cohesion}))
        .collect();
    let digest = json!({
        "computed_at": computed_at,
        "communities": community_values,
        "god_nodes": gods,
        "surprises": surprises,
        "questions": questions,
        "totals": {"entities": entities.len(), "edges": edges.len(),
                   "communities": communities.len()},
    });
    Insight {
        summaries,
        assignment,
        digest,
    }
}

// ---------------------------------------------------------------------------
// The service refresh
// ---------------------------------------------------------------------------

/// `_contested_facts`: each current record against the first contested
/// record parked on its slot, in record order.
fn contested_facts(store: &crate::stores::cortex::CortexStore) -> Vec<Contested> {
    let mut parked: HashMap<&crate::stores::cortex::SlotKey, &crate::stores::cortex::CortexRecord> =
        HashMap::new();
    for c in store.records() {
        if c.status == "contested" {
            parked.entry(c.key()).or_insert(c);
        }
    }
    if parked.is_empty() {
        return Vec::new();
    }
    store
        .current_records()
        .filter_map(|r| {
            parked.get(r.key()).map(|c| Contested {
                entity: r.entity.clone(),
                attribute: r.attribute.clone(),
                value: r.value.clone(),
                contender_value: c.value.clone(),
                contender_origin: c.origin().to_string(),
            })
        })
        .collect()
}

fn now() -> f64 {
    crate::episodes::now()
}

/// `_refresh_graph_insight`: read the graph and the prior assignment under
/// the writer lock, compute without it, then replace the communities (one
/// transaction) and write the digest (another), as Python's two storage
/// calls do.
async fn refresh(svc: &Arc<Service>, ready: &Arc<Ready>) -> anyhow::Result<Value> {
    let cfg = svc.config.memory.graph_insight.clone();
    if !cfg.enabled {
        return Ok(json!({"refreshed": false, "reason": "disabled"}));
    }
    let client = ready.storage.client();
    let (graph, prior) = {
        let _ws = ready.write.lock().await;
        let graph = crate::storage::graph::load_graph(client).await?;
        let prior: BTreeMap<i64, i64> = crate::txn::with_client(client, |c| async move {
            Ok::<_, anyhow::Error>(
                c.query(
                    "SELECT entity_id, community_id FROM entity_communities",
                    &[],
                )
                .await?
                .iter()
                .map(|r| (r.get::<_, i64>(0), r.get::<_, i64>(1)))
                .collect(),
            )
        })
        .await?;
        (graph, prior)
    };
    let edges: Vec<InsightEdge> = graph["edges"]
        .as_array()
        .map(|a| a.as_slice())
        .unwrap_or_default()
        .iter()
        .map(|e| InsightEdge {
            src: e["src_id"].as_i64().unwrap_or_default(),
            dst: e["dst_id"].as_i64().unwrap_or_default(),
            relation: e["relation"].as_str().unwrap_or_default().to_string(),
            confidence: e["confidence"].as_f64().unwrap_or(1.0),
            origin: e["origin"].as_str().map(str::to_string),
        })
        .collect();
    if edges.is_empty() {
        return Ok(json!({"refreshed": false, "reason": "empty_graph"}));
    }
    let entities: Vec<InsightEntity> = graph["entities"]
        .as_array()
        .map(|a| a.as_slice())
        .unwrap_or_default()
        .iter()
        .map(|e| InsightEntity {
            id: e["id"].as_i64().unwrap_or_default(),
            display: e["display"].as_str().unwrap_or_default().to_string(),
        })
        .collect();
    let contested = {
        let _ws = ready.write.lock().await;
        contested_facts(&ready.cortex.snapshot())
    };
    let computed_at = now();
    let insight = tokio::task::spawn_blocking(move || {
        compute(&cfg, &entities, &edges, &prior, &contested, computed_at)
    })
    .await?;
    let n = insight.summaries.len();
    {
        let _ws = ready.write.lock().await;
        replace_communities(client, &insight, computed_at).await?;
        if !crate::mutants::active("mcp-dream-insight-no-digest") {
            crate::txn::set_meta(client, "graph_digest", &insight.digest).await?;
        }
    }
    Ok(json!({"refreshed": true, "communities": n}))
}

/// `replace_communities` (`storage/postgres.py:4590-4609`): truncate and
/// bulk insert, one transaction.
async fn replace_communities(
    client: &tokio_postgres::Client,
    insight: &Insight,
    computed_at: f64,
) -> anyhow::Result<()> {
    let ids: Vec<i64> = insight.summaries.iter().map(|s| s.id).collect();
    let labels: Vec<String> = insight.summaries.iter().map(|s| s.label.clone()).collect();
    let sizes: Vec<i32> = insight
        .summaries
        .iter()
        .map(|s| i32::try_from(s.size).unwrap_or(i32::MAX))
        .collect();
    let cohesion: Vec<f64> = insight.summaries.iter().map(|s| s.cohesion).collect();
    let eids: Vec<i64> = insight.assignment.iter().map(|a| a.0).collect();
    let cids: Vec<i64> = insight.assignment.iter().map(|a| a.1).collect();
    let (ids, labels, sizes, cohesion, eids, cids) =
        (&ids, &labels, &sizes, &cohesion, &eids, &cids);
    crate::txn::run(client, |c| async move {
        c.execute("DELETE FROM entity_communities", &[]).await?;
        c.execute("DELETE FROM communities", &[]).await?;
        if !ids.is_empty() {
            c.execute(
                "INSERT INTO communities (id, label, size, cohesion, computed_at) \
                 SELECT u.id, u.label, u.size, u.cohesion, $5 \
                 FROM UNNEST($1::bigint[], $2::text[], $3::integer[], $4::double precision[]) \
                 AS u(id, label, size, cohesion)",
                &[ids, labels, sizes, cohesion, &computed_at],
            )
            .await?;
        }
        if !eids.is_empty() {
            c.execute(
                "INSERT INTO entity_communities (entity_id, community_id, computed_at) \
                 SELECT u.e, u.c, $3 FROM UNNEST($1::bigint[], $2::bigint[]) AS u(e, c)",
                &[eids, cids, &computed_at],
            )
            .await?;
        }
        Ok::<_, anyhow::Error>(())
    })
    .await
}

/// `_safe_refresh_graph_insight`: any failure becomes an `error` answer and
/// a log line, never a failed dream.
pub async fn safe_refresh(svc: &Arc<Service>, ready: &Arc<Ready>) -> Value {
    match refresh(svc, ready).await {
        Ok(v) => v,
        Err(e) => {
            eprintln!("graph-insight refresh failed ({e}); dream unaffected");
            json!({"refreshed": false, "error": e.to_string()})
        }
    }
}

#[cfg(test)]
#[path = "graph_insight_tests.rs"]
mod tests;
