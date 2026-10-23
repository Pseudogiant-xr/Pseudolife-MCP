//! `memory_dream(action="deep")` (`mcp_server.py:2172-2290`): the deep
//! dream, a deterministic full-corpus graph consolidation (no model calls).
//! Python: `service_dream.py` `deep_dream` (3355-3675) and its helpers
//! `_file_analyzer_duplicates` (6095-6156), `_unreachable_orphans` /
//! `_sweep_unreachable_orphans` (6158-6203), `_write_graph_snapshot[_locked]`
//! (6205-6228), `_edge_label`, `_merge_labels`, `_attach_candidate_snippets`
//! (6253-6340) and `_enrich_merge_proposals` (6342-6413); `service.py`
//! `_kept_junk_norms` (499-503), `_slot_duplicate_listings` (7989-8001) and
//! `_curation_records` (8766-8785); the pure half in `graph_consolidation.rs`.
//!
//! The tool runs detached. Python takes its service lock in windows (the
//! read snapshot; a dry run's orphan census; an apply's snapshot file, then
//! its writes, then the stamp and the re-read) and computes between them.
//! Here the writer lock is held for the read snapshot and the computation
//! runs on a blocking thread; a dry run releases the lock for it and takes
//! it again for the census, as Python does, while an apply keeps it from
//! the read to the end, so nothing interleaves with its writes (declared).
//! Every apply write is its own statement or transaction, as each
//! `storage/postgres.py` call is.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};
use tokio_postgres::Client;
use tokio_postgres::types::Type;

use crate::config::DeepDreamConfig;
use crate::graph_consolidation::{
    self as gc, Candidate, CandidateOpts, Contexts, DEntry, GEdge, MergeCand, SlotRecord, py_round,
    py_slice_chars,
};
use crate::graph_review::{self as gr, Entity, EntityProposal, Key, token_set};
use crate::graph_review_tools::actions;
use crate::mcp::dispatch::ToolError;
use crate::service::{Ready, Service};
use crate::storage::graph::{self, norm_name, py_lower};

/// `_DEEP_LIST_HEAD`, `_DEEP_RESPONSE_BUDGET` (`mcp_server.py:2249-2250`).
const DEEP_LIST_HEAD: usize = 40;
const DEEP_RESPONSE_BUDGET: usize = 250_000;
/// `_LOW_DIFFERENTIAL_SHARE`, `_SNIPPET_SCAN_CAP` (`service_dream.py`).
const LOW_DIFFERENTIAL_SHARE: f64 = 0.5;
const SNIPPET_SCAN_CAP: usize = 12;
/// `service._JUNK_KEEP_PREFIX`.
const JUNK_KEEP_PREFIX: &str = "junk:";
/// The five graph tables the apply mutates (`dump_graph_tables`).
const SNAPSHOT_TABLES: [&str; 5] = [
    "entities",
    "edges",
    "entity_aliases",
    "edge_proposals",
    "entity_proposals",
];

fn internal(e: impl std::fmt::Display) -> ToolError {
    ToolError::Internal(anyhow::anyhow!(e.to_string()))
}

fn now() -> f64 {
    crate::episodes::now()
}

fn py_strip(s: &str) -> &str {
    s.trim_matches(crate::stores::cortex::py_isspace)
}

/// An `entity_proposal_keys` key: `("merge", lo, hi)` or `(kind, id)`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum PropKey {
    Merge(i64, i64),
    Kind(String, i64),
}

/// The deep dream's read snapshot (the first lock window).
struct Loaded {
    entities: Vec<Entity>,
    etype: HashMap<i64, Option<String>>,
    aliases: Vec<String>,
    edges: Vec<GEdge>,
    scope_map: BTreeMap<i64, Vec<String>>,
    traces: HashMap<String, Vec<i64>>,
    entries: Vec<DEntry>,
    dismissed: HashSet<(String, String)>,
    prop_keys: HashSet<PropKey>,
    pending_props: Vec<EntityProposal>,
    pending_links: HashSet<(i64, i64)>,
    lesson_refs: HashSet<i64>,
    fact_counts: HashMap<i64, i64>,
    fact_texts: Vec<(String, i64)>,
}

async fn pending_entity_proposals(c: &Client) -> anyhow::Result<Vec<EntityProposal>> {
    Ok(
        c.query(crate::graph_review_tools::PENDING_ENTITY_PROPOSALS_SQL, &[])
            .await?
            .iter()
            .map(crate::graph_review_tools::entity_proposal_row)
            .collect(),
    )
}

async fn load(c: &Client) -> anyhow::Result<Loaded> {
    let g = graph::load_graph_body(c).await?;
    let s = |v: &Value| v.as_str().unwrap_or_default().to_string();
    let mut entities = Vec::new();
    let mut etype = HashMap::new();
    for e in g["entities"].as_array().into_iter().flatten() {
        let id = e["id"].as_i64().unwrap_or(0);
        entities.push(Entity {
            id,
            canonical: s(&e["canonical"]),
            display: s(&e["display"]),
        });
        etype.insert(id, e["etype"].as_str().map(str::to_string));
    }
    let aliases = g["aliases"]
        .as_object()
        .into_iter()
        .flat_map(|m| m.values())
        .flat_map(|v| v.as_array().into_iter().flatten())
        .filter_map(|a| a.as_str().map(str::to_string))
        .collect();
    let edges = g["edges"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| GEdge {
            id: e["id"].as_i64().unwrap_or(0),
            src_id: e["src_id"].as_i64().unwrap_or(0),
            relation: s(&e["relation"]),
            dst_id: e["dst_id"].as_i64().unwrap_or(0),
            confidence: e["confidence"].as_f64().unwrap_or(0.0),
            origin: e["origin"].as_str().map(str::to_string),
        })
        .collect();
    // `entity_sources_map` (4262-4268).
    let mut scope_map: BTreeMap<i64, Vec<String>> = BTreeMap::new();
    for r in c
        .query(
            "SELECT entity_id, source FROM entity_sources ORDER BY entity_id, source",
            &[],
        )
        .await?
    {
        scope_map.entry(r.get(0)).or_default().push(r.get(1));
    }
    // `traces_by_entity_norm` (3392-3398).
    let mut traces: HashMap<String, Vec<i64>> = HashMap::new();
    for r in c
        .query(
            "SELECT entity_norm, entry_id FROM memory_traces ORDER BY entity_norm, entry_id",
            &[],
        )
        .await?
    {
        traces.entry(r.get(0)).or_default().push(r.get(1));
    }
    // `load_entries` (1435-1445): the columns the deep dream reads.
    let entries = c
        .query(
            "SELECT id, text, source, embedding::real[] FROM entries ORDER BY id",
            &[],
        )
        .await?
        .iter()
        .map(|r| DEntry {
            id: r.get(0),
            text: r.get(1),
            source: r.get(2),
            embedding: r.get(3),
        })
        .collect();
    // `dismissed_pairs` (3427-3429).
    let dismissed = c
        .query("SELECT a_norm, b_norm FROM dismissed_pairs", &[])
        .await?
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect();
    // `entity_proposal_keys` (3731-3744).
    let mut prop_keys = HashSet::new();
    for r in c
        .query("SELECT kind, entity_id, into_id FROM entity_proposals", &[])
        .await?
    {
        let (kind, eid, into): (String, i64, Option<i64>) = (r.get(0), r.get(1), r.get(2));
        prop_keys.insert(match (kind.as_str(), into) {
            ("merge", Some(into)) => PropKey::Merge(eid.min(into), eid.max(into)),
            _ => PropKey::Kind(kind, eid),
        });
    }
    let pending_props = pending_entity_proposals(c).await?;
    // `pending_proposals` (3431-3455): the pairs.
    let pending_links = c
        .query(
            "SELECT p.src_id, p.dst_id FROM edge_proposals p \
             JOIN entities s ON s.id = p.src_id JOIN entities d ON d.id = p.dst_id \
             WHERE p.status = 'pending'",
            &[],
        )
        .await?
        .iter()
        .map(|r| {
            let (a, b): (i64, i64) = (r.get(0), r.get(1));
            (a.min(b), a.max(b))
        })
        .collect();
    // `lesson_entity_ids` (4228-4238).
    let lesson_refs = c
        .query(
            "SELECT entity_id FROM lessons WHERE entity_id IS NOT NULL \
             UNION \
             SELECT object_entity_id FROM lessons WHERE object_entity_id IS NOT NULL",
            &[],
        )
        .await?
        .iter()
        .map(|r| r.get(0))
        .collect();
    // `entity_fact_counts` (4240-4247).
    let fact_counts = c
        .query(
            "SELECT entity_id, COUNT(*) FROM facts \
             WHERE status = 'current' AND entity_id IS NOT NULL GROUP BY entity_id",
            &[],
        )
        .await?
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect();
    // `current_fact_counts_by_entity_text` (4249-4260): `str(entity)`.
    let fact_texts = c
        .query(
            "SELECT entity, COUNT(*) FROM facts WHERE status = 'current' GROUP BY entity",
            &[],
        )
        .await?
        .iter()
        .map(|r| {
            let ent: Option<String> = r.get(0);
            (ent.unwrap_or_else(|| "None".into()), r.get(1))
        })
        .collect();
    Ok(Loaded {
        entities,
        etype,
        aliases,
        edges,
        scope_map,
        traces,
        entries,
        dismissed,
        prop_keys,
        pending_props,
        pending_links,
        lesson_refs,
        fact_counts,
        fact_texts,
    })
}

/// `_curation_records(store, value_cap)` for both stores, from the resident
/// lesson and world stores' current records.
fn curation_records(ready: &Ready, value_cap: i64) -> (Vec<SlotRecord>, Vec<SlotRecord>) {
    let cap = |v: &str| {
        if value_cap != 0 {
            py_slice_chars(v, value_cap)
        } else {
            v.to_string()
        }
    };
    let key = |k: &(String, String)| crate::graph_review_tools::slots::slot_key_text(&k.0, &k.1);
    let lessons = ready.lessons.snapshot();
    let lesson_recs = lessons
        .current_records()
        .map(|r| {
            let mut label = Map::new();
            label.insert("entity".into(), json!(r.entity));
            label.insert("attribute".into(), json!(r.attribute));
            label.insert("value".into(), json!(cap(&r.value)));
            label.insert("polarity".into(), json!(r.polarity));
            label.insert("outcome".into(), json!(r.outcome));
            label.insert("about".into(), json!(r.about));
            SlotRecord {
                key: key(&r.key),
                label,
                embedding: r.embedding.as_deref().map(<[f32]>::to_vec),
            }
        })
        .collect();
    let world = ready.world.snapshot();
    let world_recs = world
        .records
        .iter()
        .filter(|r| r.status == "current")
        .map(|r| {
            let mut label = Map::new();
            label.insert("entity".into(), json!(r.entity));
            label.insert("attribute".into(), json!(r.attribute));
            label.insert("value".into(), json!(cap(&r.value)));
            label.insert("source_url".into(), json!(r.source_url));
            SlotRecord {
                key: key(&r.key),
                label,
                embedding: r.embedding.as_deref().map(<[f32]>::to_vec),
            }
        })
        .collect();
    (lesson_recs, world_recs)
}

/// `_store_dismissed(dismissed, store)`: the store's namespaced pairs, bare.
fn store_dismissed(
    dismissed: &HashSet<(String, String)>,
    store: &str,
) -> HashSet<(String, String)> {
    let pre = format!("{store}:");
    dismissed
        .iter()
        .filter_map(|(a, b)| {
            Some((
                a.strip_prefix(&pre)?.to_string(),
                b.strip_prefix(&pre)?.to_string(),
            ))
        })
        .collect()
}

/// `_kept_junk_norms(dismissed)`: canonicals a junk proposal was kept for.
fn kept_junk_norms(dismissed: &HashSet<(String, String)>) -> HashSet<String> {
    dismissed
        .iter()
        .filter(|(a, b)| a == b)
        .filter_map(|(a, _)| a.strip_prefix(JUNK_KEEP_PREFIX).map(str::to_string))
        .collect()
}

// ── evidence snippets (`_attach_candidate_snippets`) ─────────────────────

/// What `_attach_candidate_snippets` reads, shared by every call.
struct Snippets<'a> {
    by_id: HashMap<i64, &'a DEntry>,
    canon: HashMap<i64, &'a str>,
    disp: HashMap<i64, &'a str>,
    traces: &'a HashMap<String, Vec<i64>>,
    mentions: &'a HashMap<i64, BTreeSet<i64>>,
    entry_tokens: &'a [(i64, BTreeSet<String>)],
    k: i64,
    max_chars: i64,
}

/// One pair's snippets and, on the merge path, its evidence stamps.
struct Attached {
    src: Vec<String>,
    dst: Vec<String>,
    overlap: f64,
    low_differential: bool,
}

impl Snippets<'_> {
    /// `pool(eid)`: usable traces, then the sorted mentions not yet seen,
    /// else a bounded token scan.
    fn pool(&self, eid: Option<i64>) -> Vec<i64> {
        let canon = eid.and_then(|e| self.canon.get(&e).copied()).unwrap_or("");
        let mut ids: Vec<i64> = self
            .traces
            .get(canon)
            .into_iter()
            .flatten()
            .copied()
            .filter(|i| self.by_id.contains_key(i))
            .collect();
        let mut seen: HashSet<i64> = ids.iter().copied().collect();
        if let Some(m) = eid.and_then(|e| self.mentions.get(&e)) {
            for &i in m {
                if self.by_id.contains_key(&i) && seen.insert(i) {
                    ids.push(i);
                }
            }
        }
        if ids.is_empty() {
            let want = token_set(eid.and_then(|e| self.disp.get(&e).copied()).unwrap_or(""));
            if !want.is_empty() {
                for (i, toks) in self.entry_tokens {
                    if want.is_subset(toks) {
                        ids.push(*i);
                        if ids.len() >= SNIPPET_SCAN_CAP {
                            break;
                        }
                    }
                }
            }
        }
        ids
    }

    /// `pick(own, other)`: up to `k` distinct (truncated) texts, exclusive
    /// entries first on the merge path.
    fn pick(&self, own: &[i64], other: &HashSet<i64>, differential: bool) -> Vec<String> {
        let ranked: Vec<i64> = if differential {
            own.iter()
                .filter(|i| !other.contains(i))
                .chain(own.iter().filter(|i| other.contains(i)))
                .copied()
                .collect()
        } else {
            own.to_vec()
        };
        let mut texts: Vec<String> = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        for i in ranked {
            if texts.len() as i64 >= self.k {
                break;
            }
            let mut t = self.by_id[&i].text.clone();
            if self.max_chars != 0 {
                t = py_slice_chars(&t, self.max_chars);
            }
            if seen.contains(&t) {
                continue;
            }
            seen.insert(t.clone());
            texts.push(t);
        }
        texts
    }

    fn attach(&self, src_id: Option<i64>, dst_id: Option<i64>, differential: bool) -> Attached {
        let (src_pool, dst_pool) = (self.pool(src_id), self.pool(dst_id));
        let src_set: HashSet<i64> = src_pool.iter().copied().collect();
        let dst_set: HashSet<i64> = dst_pool.iter().copied().collect();
        let src = self.pick(&src_pool, &dst_set, differential);
        let dst = self.pick(&dst_pool, &src_set, differential);
        let (mut overlap, mut low) = (0.0, false);
        if differential {
            if !src.is_empty() && !dst.is_empty() {
                let a: HashSet<&String> = src.iter().collect();
                let b: HashSet<&String> = dst.iter().collect();
                overlap = a.intersection(&b).count() as f64 / src.len().min(dst.len()) as f64;
            }
            let one_sided = !src_pool.is_empty()
                && !dst_pool.is_empty()
                && (src_set.is_subset(&dst_set) || dst_set.is_subset(&src_set));
            low =
                src.is_empty() || dst.is_empty() || one_sided || overlap >= LOW_DIFFERENTIAL_SHARE;
        }
        Attached {
            src,
            dst,
            overlap,
            low_differential: low,
        }
    }
}

// ── merge proposal enrichment (`_enrich_merge_proposals`) ─────────────────

struct Enrich<'a> {
    deg: &'a BTreeMap<i64, usize>,
    facts: &'a HashMap<i64, i64>,
    display: HashMap<i64, &'a str>,
    etype: &'a HashMap<i64, Option<String>>,
    scope_map: &'a BTreeMap<i64, Vec<String>>,
    snippets: &'a Snippets<'a>,
}

impl Enrich<'_> {
    fn ev(&self, id: Option<i64>) -> i64 {
        id.map_or(0, |i| {
            self.deg.get(&i).copied().unwrap_or(0) as i64 + self.facts.get(&i).copied().unwrap_or(0)
        })
    }

    fn side(&self, eid: Option<i64>, snips: Vec<String>, include: bool) -> Value {
        let display = eid.and_then(|e| self.display.get(&e).copied());
        let etype = eid.and_then(|e| self.etype.get(&e).cloned().flatten());
        let degree = eid.map_or(0, |e| self.deg.get(&e).copied().unwrap_or(0));
        let mut scopes: Vec<String> = eid
            .and_then(|e| self.scope_map.get(&e).cloned())
            .unwrap_or_default();
        scopes.sort();
        json!({"display": display, "etype": etype, "degree": degree, "scopes": scopes,
               "snippets": if include { snips } else { Vec::new() }})
    }

    /// The pending merge rows, each oriented by `_fold_direction` over current
    /// evidence, grouped on the stored ids, with per-side evidence.
    #[allow(clippy::result_large_err)]
    fn rows(&self, pending: &[EntityProposal], include: bool) -> Result<Vec<Value>, ToolError> {
        let rows: Vec<&EntityProposal> = pending.iter().filter(|p| p.kind == "merge").collect();
        let key = |id: Option<i64>| id.map_or(Key::Name(None), Key::Id);
        let pairs: Vec<(Key, Key)> = rows
            .iter()
            .map(|p| (key(p.entity_id), key(p.into_id)))
            .collect();
        let groups = gr::shared_pair_groups(&pairs).map_err(ToolError::Invalid)?;
        let mut out = Vec::new();
        for (p, g) in rows.iter().zip(groups) {
            let (frm, into) = if self.ev(p.entity_id) > self.ev(p.into_id) {
                (p.into_id, p.entity_id)
            } else {
                (p.entity_id, p.into_id)
            };
            let attached = include.then(|| self.snippets.attach(frm, into, true));
            let (src, dst) = attached
                .as_ref()
                .map_or((Vec::new(), Vec::new()), |a| (a.src.clone(), a.dst.clone()));
            let group = match g {
                Some(Key::Id(id)) => self
                    .display
                    .get(&id)
                    .map(|d| json!(d))
                    .unwrap_or(Value::Null),
                _ => Value::Null,
            };
            let mut row = Map::new();
            row.insert("id".into(), json!(p.id));
            row.insert("score".into(), json!(p.score));
            row.insert("reason".into(), json!(p.reason));
            row.insert("group".into(), group);
            row.insert("from".into(), self.side(frm, src, include));
            row.insert("into".into(), self.side(into, dst, include));
            if let Some(a) = &attached {
                row.insert("evidence_overlap".into(), json!(py_round(a.overlap, 2)));
                row.insert("low_differential".into(), json!(a.low_differential));
            }
            for (k, v) in gr::judge_blocks(p) {
                row.insert(k.into(), v);
            }
            out.push(Value::Object(row));
        }
        Ok(out)
    }
}

// ── storage (`storage/postgres.py`) ──────────────────────────────────────

/// `zero_evidence_entities(min_age_seconds)` (3862-3893).
async fn zero_evidence_entities(
    client: &Client,
    min_age_seconds: f64,
) -> Result<Vec<Orphan>, ToolError> {
    let cutoff = now() - min_age_seconds;
    let rows = crate::txn::with_client(client, |c| async move {
        c.query(
            "SELECT e.id, e.display, e.canonical, e.created_at FROM entities e \
             WHERE e.created_at < $1 \
               AND NOT EXISTS (SELECT 1 FROM edges x \
                               WHERE x.src_id = e.id OR x.dst_id = e.id) \
               AND NOT EXISTS (SELECT 1 FROM facts f \
                               WHERE (f.entity_id = e.id OR f.object_entity_id = e.id \
                                      OR f.entity_norm = e.canonical) \
                                 AND f.superseded_at IS NULL) \
               AND NOT EXISTS (SELECT 1 FROM lessons l \
                               WHERE l.entity_id = e.id OR l.object_entity_id = e.id \
                                  OR l.entity_norm = e.canonical) \
               AND NOT EXISTS (SELECT 1 FROM world_facts w \
                               WHERE w.entity_norm = e.canonical AND w.status = 'current') \
               AND NOT EXISTS (SELECT 1 FROM entity_aliases a WHERE a.entity_id = e.id) \
               AND NOT EXISTS (SELECT 1 FROM entity_sources s WHERE s.entity_id = e.id) \
               AND NOT EXISTS (SELECT 1 FROM edge_proposals p \
                               WHERE p.src_id = e.id OR p.dst_id = e.id) \
               AND NOT EXISTS (SELECT 1 FROM entity_proposals q \
                               WHERE q.entity_id = e.id OR q.into_id = e.id) \
             ORDER BY e.id",
            &[&cutoff],
        )
        .await
    })
    .await
    .map_err(internal)?;
    Ok(rows
        .iter()
        .map(|r| Orphan {
            id: r.get(0),
            display: r.get(1),
            canonical: r.get(2),
            created_at: r.get(3),
        })
        .collect())
}

/// One orphan-census candidate.
struct Orphan {
    id: i64,
    display: String,
    canonical: String,
    created_at: f64,
}

/// `delete_entity_audited` (2918-2937): the delete and its audit row in one
/// transaction.
async fn delete_entity_audited(
    client: &Client,
    id: i64,
    display: &str,
    now: f64,
) -> Result<bool, ToolError> {
    actions::in_txn(client, |c| async move {
        for sql in [
            "UPDATE facts SET entity_id = NULL WHERE entity_id = $1",
            "UPDATE facts SET object_entity_id = NULL WHERE object_entity_id = $1",
            "UPDATE lessons SET entity_id = NULL WHERE entity_id = $1",
            "UPDATE lessons SET object_entity_id = NULL WHERE object_entity_id = $1",
        ] {
            c.execute(sql, &[&id]).await.map_err(internal)?;
        }
        let gone = c
            .query_opt("DELETE FROM entities WHERE id = $1 RETURNING id", &[&id])
            .await
            .map_err(internal)?;
        if gone.is_none() {
            return Ok(false);
        }
        c.execute(
            "INSERT INTO merge_decisions \
             (proposal_id, entity_display, into_display, status, score, \
              reason, decided_by, decided_at) \
             VALUES (NULL, $1, NULL, 'deleted', NULL, \
                     'orphan-unreachable auto-delete', 'dream-auto', $2)",
            &[&display, &now],
        )
        .await
        .map_err(internal)?;
        Ok(true)
    })
    .await
}

/// `set_edge_confidence` (3387-3390).
async fn set_edge_confidence(client: &Client, id: i64, confidence: f64) -> Result<(), ToolError> {
    crate::txn::run(client, |c| async move {
        c.execute(
            "UPDATE edges SET confidence = $1::float8 WHERE id = $2",
            &[&confidence, &id],
        )
        .await
    })
    .await
    .map(drop)
    .map_err(internal)
}

/// One column value as psycopg reads it and `json.dumps(default=str)`
/// writes it: a REAL through its shortest text (psycopg parses the text
/// form), JSON as decoded, anything else unusual through its text.
fn column_value(
    row: &tokio_postgres::Row,
    i: usize,
    ty: &Type,
) -> Result<Value, tokio_postgres::Error> {
    Ok(if *ty == Type::INT8 {
        json!(row.try_get::<_, Option<i64>>(i)?)
    } else if *ty == Type::INT4 {
        json!(row.try_get::<_, Option<i32>>(i)?)
    } else if *ty == Type::INT2 {
        json!(row.try_get::<_, Option<i16>>(i)?)
    } else if *ty == Type::FLOAT8 {
        json!(row.try_get::<_, Option<f64>>(i)?)
    } else if *ty == Type::FLOAT4 {
        match row.try_get::<_, Option<f32>>(i)? {
            None => Value::Null,
            Some(x) if x.is_finite() => {
                json!(format!("{x}").parse::<f64>().unwrap_or(f64::from(x)))
            }
            Some(x) => json!(f64::from(x)),
        }
    } else if *ty == Type::BOOL {
        json!(row.try_get::<_, Option<bool>>(i)?)
    } else if *ty == Type::JSON || *ty == Type::JSONB {
        row.try_get::<_, Option<Value>>(i)?.unwrap_or(Value::Null)
    } else {
        json!(row.try_get::<_, Option<String>>(i)?)
    })
}

/// `dump_graph_tables` (3766-3775): every row of the five tables, `ORDER BY 1`,
/// as column-ordered dicts.
async fn dump_graph_tables(c: &Client) -> Result<Value, tokio_postgres::Error> {
    let mut out = Map::new();
    for t in SNAPSHOT_TABLES {
        let stmt = c.prepare(&format!("SELECT * FROM {t} ORDER BY 1")).await?;
        let cols: Vec<(String, Type)> = stmt
            .columns()
            .iter()
            .map(|col| (col.name().to_string(), col.type_().clone()))
            .collect();
        let mut rows = Vec::new();
        for r in c.query(&stmt, &[]).await? {
            let mut m = Map::new();
            for (i, (name, ty)) in cols.iter().enumerate() {
                m.insert(name.clone(), column_value(&r, i, ty)?);
            }
            rows.push(Value::Object(m));
        }
        out.insert(t.into(), Value::Array(rows));
    }
    Ok(Value::Object(out))
}

/// `_write_graph_snapshot_locked` (6210-6229): the tables dumped to
/// `data_dir/graph_snapshots/graph-%Y%m%d-%H%M%S.json` (UTC) as
/// `json.dumps(tables, default=str)`, the newest `snapshot_keep` (at least
/// one) kept. `None` on an OS error: the caller refuses to apply.
async fn write_graph_snapshot(
    data_dir: &Path,
    client: &Client,
    snapshot_keep: i64,
) -> Result<Option<String>, ToolError> {
    let tables = crate::txn::with_client(client, |c| async move { dump_graph_tables(c).await })
        .await
        .map_err(internal)?;
    Ok(write_snapshot_file(data_dir, &tables, snapshot_keep))
}

/// The snapshot file write and rotation; `None` on an OS error.
fn write_snapshot_file(data_dir: &Path, tables: &Value, snapshot_keep: i64) -> Option<String> {
    let keep = snapshot_keep.max(1) as usize;
    let text = crate::pyjson::dumps(tables);
    let dir = data_dir.join("graph_snapshots");
    let name = format!("{}.json", chrono::Utc::now().format("graph-%Y%m%d-%H%M%S"));
    let written = (|| -> std::io::Result<()> {
        std::fs::create_dir_all(&dir)?;
        std::fs::write(dir.join(&name), text.as_bytes())?;
        // `sorted(d.glob("graph-*.json"))[:-keep]`: pathlib matches and
        // orders case-insensitively on Windows.
        let fold = |s: &str| {
            if cfg!(windows) {
                s.to_lowercase()
            } else {
                s.to_string()
            }
        };
        let mut found: Vec<(String, PathBuf)> = Vec::new();
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            let file_name = entry.file_name().to_string_lossy().into_owned();
            let key = fold(&file_name);
            if key.starts_with("graph-") && key.ends_with(".json") {
                found.push((key, entry.path()));
            }
        }
        found.sort();
        let stale = found.len().saturating_sub(keep);
        for (_, path) in &found[..stale] {
            std::fs::remove_file(path)?;
        }
        Ok(())
    })();
    written.ok().map(|()| name)
}

/// The orphan sweep's census (`_unreachable_orphans`): zero-evidence
/// entities no current entry mentions and no fact names.
async fn unreachable_orphans(
    client: &Client,
    cfg: &DeepDreamConfig,
    entry_tokens: &[(i64, BTreeSet<String>)],
    mentions: &HashMap<i64, BTreeSet<i64>>,
    facts_by_norm: &HashMap<String, i64>,
    dropped: &HashSet<i64>,
) -> Result<Vec<Orphan>, ToolError> {
    let cands = zero_evidence_entities(client, cfg.orphan_min_age_days * 86400.0).await?;
    let mut out = Vec::new();
    for c in cands {
        if dropped.contains(&c.id) || mentions.get(&c.id).is_some_and(|m| !m.is_empty()) {
            continue;
        }
        let named = |k: &str| facts_by_norm.get(k).copied().unwrap_or(0) != 0;
        if named(&norm_name(&c.display)) || named(&c.canonical) {
            continue;
        }
        let want = token_set(&c.display);
        if want.is_empty() || entry_tokens.iter().any(|(_, toks)| want.is_subset(toks)) {
            continue;
        }
        out.push(c);
    }
    Ok(out)
}

/// `_file_analyzer_duplicates` (6095-6156): the analyzer's live duplicate
/// findings filed into the merge queue (variant conflicts skipped) or, for
/// a file and its stem, the link queue; pairs the merge queue already holds
/// in any status are skipped.
#[allow(clippy::too_many_arguments)]
async fn file_analyzer_duplicates(
    client: &Client,
    entities: &[Entity],
    edges: &[gr::Edge],
    dismissed: &HashSet<(String, String)>,
    lesson_refs: &HashSet<i64>,
    fact_counts: &HashMap<i64, i64>,
    excluded: &HashSet<i64>,
    prop_keys: &HashSet<PropKey>,
    now: f64,
) -> Result<i64, ToolError> {
    let alive: Vec<Entity> = entities
        .iter()
        .filter(|e| !excluded.contains(&e.id))
        .cloned()
        .collect();
    let findings =
        gr::duplicate_candidates(&alive, dismissed, &gr::lesson_only_ids(edges, lesson_refs));
    if findings.is_empty() {
        return Ok(0);
    }
    let mut by_display: HashMap<&str, i64> = HashMap::new();
    for e in &alive {
        by_display.insert(&e.display, e.id);
    }
    let deg = gr::degree_counts(edges);
    let registry: HashSet<String> = graph::load_relations(client)
        .await
        .map_err(internal)?
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|r| r.get("name").and_then(Value::as_str).map(str::to_string))
        .collect();
    let mut filed = 0;
    for f in &findings {
        let names: Vec<&str> = f
            .get("entities")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect();
        let [a, b] = names[..] else {
            continue;
        };
        let (Some(&ia), Some(&ib)) = (by_display.get(a), by_display.get(b)) else {
            continue;
        };
        if ia == ib || prop_keys.contains(&PropKey::Merge(ia.min(ib), ia.max(ib))) {
            continue;
        }
        let score = f.get("score").and_then(Value::as_f64);
        let score_text = score.map_or_else(|| "None".to_string(), crate::pyjson::float_repr);
        let pid = if f.get("action").and_then(Value::as_str) == Some("relate") {
            let mut rel = f
                .get("suggested_relation")
                .and_then(Value::as_str)
                .filter(|r| !r.is_empty())
                .unwrap_or("related-to")
                .to_string();
            if !registry.contains(&rel) {
                rel = "related-to".into();
            }
            actions::insert_proposal(
                client,
                ia,
                &rel,
                ib,
                actions::edge_confidence(a, &rel, b) as f32,
                None,
                Some(format!("analyzer file/concept pair (jaccard {score_text})")),
                "analyzer",
                now,
            )
            .await?
        } else {
            if gc::variant_conflict(a, b) {
                continue;
            }
            let ev = |i: i64| {
                deg.get(&i).copied().unwrap_or(0) as i64 + fact_counts.get(&i).copied().unwrap_or(0)
            };
            let (into, frm) = if ev(ia) > ev(ib) || (ev(ia) == ev(ib) && ia < ib) {
                (ia, ib)
            } else {
                (ib, ia)
            };
            super::relations::insert_entity_proposal_row(
                client,
                "merge",
                frm,
                Some(into),
                score,
                Some(&format!("analyzer-duplicate: jaccard {score_text}")),
                now,
            )
            .await?
        };
        filed += i64::from(pid.is_some());
    }
    Ok(filed)
}

// ── the response bound (`_bound_deep_response`) ──────────────────────────

/// `_bound_deep_response`: a response whose `json.dumps` text fits the
/// budget is returned untouched; otherwise every top-level list is cut to
/// its leading `head` items (40, halved until the text fits or the head is
/// 1), with `truncated` carrying each cut list's full length and a `hint`.
pub(crate) fn bound(result: Map<String, Value>) -> Map<String, Value> {
    let size = |m: &Map<String, Value>| crate::pyjson::dumps(&Value::Object(m.clone())).len();
    let lists: Vec<(String, usize)> = result
        .iter()
        .filter_map(|(k, v)| v.as_array().map(|a| (k.clone(), a.len())))
        .collect();
    if lists.is_empty() || size(&result) <= DEEP_RESPONSE_BUDGET {
        return result;
    }
    let mut head = DEEP_LIST_HEAD;
    loop {
        let mut out = result.clone();
        let mut truncated = Map::new();
        for (key, len) in &lists {
            if *len > head {
                if let Some(Value::Array(items)) = out.get_mut(key) {
                    items.truncate(head);
                }
                truncated.insert(key.clone(), json!(len));
            }
        }
        if !truncated.is_empty() {
            out.insert("truncated".into(), Value::Object(truncated));
            out.insert(
                "hint".into(),
                json!(format!(
                    "the response exceeded {DEEP_RESPONSE_BUDGET} bytes, so lists are cut to \
                     their leading {head} items; truncated carries each full count. Cut \
                     candidates and duplicate listings resurface on the next deep pass; \
                     memory_graph_review(action='list') lists the pending merge proposals in \
                     full; snippets=false gives a smaller listing."
                )),
            );
        }
        if head <= 1 || size(&out) <= DEEP_RESPONSE_BUDGET {
            return out;
        }
        head /= 2;
    }
}

// ── deep_dream ───────────────────────────────────────────────────────────

/// `memory_dream(action="deep", apply, snippets)`: the deep dream, bounded.
pub(crate) async fn deep(
    service: &Service,
    ready: &Ready,
    apply: bool,
    include_snippets: bool,
) -> Result<Value, ToolError> {
    let out = deep_dream(service, ready, apply, include_snippets).await?;
    if crate::mutants::active("mcp-dream-deep-unbounded") {
        return Ok(Value::Object(out));
    }
    Ok(Value::Object(bound(out)))
}

fn edge_label(e: &GEdge, disp: &HashMap<i64, String>) -> Value {
    let name = |id: i64| disp.get(&id).cloned().unwrap_or_else(|| id.to_string());
    json!({"src": name(e.src_id), "relation": e.relation, "dst": name(e.dst_id),
           "confidence": e.confidence})
}

/// What the lock-free half of `deep_dream` computes from the read snapshot.
struct Plan {
    ld: Loaded,
    disp: HashMap<i64, String>,
    deg: BTreeMap<i64, usize>,
    review_edges: Vec<gr::Edge>,
    rescore: Vec<(i64, f64)>,
    violations: Vec<GEdge>,
    dups: Vec<(i64, i64)>,
    junk: Vec<gc::Junk>,
    junk_owned: HashSet<i64>,
    entry_tokens: Vec<(i64, BTreeSet<String>)>,
    contexts: Contexts,
    merge_cands: Vec<MergeCand>,
    candidates: Vec<Value>,
    merge_proposals: Vec<Value>,
    lesson_dups: Vec<Value>,
    world_dups: Vec<Value>,
    totals: Value,
    facts_by_norm: HashMap<String, i64>,
}

/// `_enrich_merge_proposals` over `pending`, with the snapshot's evidence.
#[allow(clippy::result_large_err)]
fn merge_rows(
    ld: &Loaded,
    contexts: &Contexts,
    entry_tokens: &[(i64, BTreeSet<String>)],
    deg: &BTreeMap<i64, usize>,
    cfg: &DeepDreamConfig,
    pending: &[EntityProposal],
    include_snippets: bool,
) -> Result<Vec<Value>, ToolError> {
    let snippets = snippets_for(ld, contexts, entry_tokens, cfg);
    let enrich = Enrich {
        deg,
        facts: &ld.fact_counts,
        display: snippets.disp.clone(),
        etype: &ld.etype,
        scope_map: &ld.scope_map,
        snippets: &snippets,
    };
    enrich.rows(pending, include_snippets)
}

fn snippets_for<'a>(
    ld: &'a Loaded,
    contexts: &'a Contexts,
    entry_tokens: &'a [(i64, BTreeSet<String>)],
    cfg: &DeepDreamConfig,
) -> Snippets<'a> {
    Snippets {
        by_id: ld.entries.iter().map(|e| (e.id, e)).collect(),
        canon: ld
            .entities
            .iter()
            .map(|e| (e.id, e.canonical.as_str()))
            .collect(),
        disp: ld
            .entities
            .iter()
            .map(|e| (e.id, e.display.as_str()))
            .collect(),
        traces: &ld.traces,
        mentions: &contexts.mentions,
        entry_tokens,
        k: cfg.max_context_snippets,
        max_chars: cfg.snippet_max_chars,
    }
}

/// Steps A and B, the candidate evidence, the merge-queue enrichment and
/// the curation listings: pure computation over the read snapshot, which
/// Python runs outside its service lock (`service_dream.py:3385-3466`).
#[allow(clippy::result_large_err)]
fn plan(
    ld: Loaded,
    lesson_recs: Vec<SlotRecord>,
    world_recs: Vec<SlotRecord>,
    cfg: &DeepDreamConfig,
    include_snippets: bool,
) -> Result<Plan, ToolError> {
    let entities = &ld.entities;
    let edges = &ld.edges;
    let disp: HashMap<i64, String> = entities.iter().map(|e| (e.id, e.display.clone())).collect();
    let canon: HashMap<i64, &str> = entities
        .iter()
        .map(|e| (e.id, e.canonical.as_str()))
        .collect();
    let review_edges: Vec<gr::Edge> = edges.iter().map(GEdge::review_edge).collect();
    let deg = gr::degree_counts(&review_edges);

    let rescore = gc::rescore_edges(edges, &disp);
    let violations: Vec<GEdge> = gc::hard_violation_edges(edges, &disp)
        .into_iter()
        .cloned()
        .collect();
    let dups = gc::exact_duplicate_pairs(entities, &deg);
    let mut known_norms: HashSet<String> = entities.iter().map(|e| e.canonical.clone()).collect();
    known_norms.extend(ld.aliases.iter().map(|a| norm_name(a)));
    let mut junk = gc::junk_entities(entities, &deg, cfg.junk_max_degree, &known_norms);
    let kept = kept_junk_norms(&ld.dismissed);
    if !kept.is_empty() && !crate::mutants::active("mcp-dream-deep-kept-refiled") {
        junk.retain(|j| !canon.get(&j.entity_id).is_some_and(|c| kept.contains(*c)));
    }
    let junk_owned: HashSet<i64> = junk
        .iter()
        .map(|j| j.entity_id)
        .chain(
            ld.pending_props
                .iter()
                .filter(|p| p.kind == "junk")
                .filter_map(|p| p.entity_id),
        )
        .collect();
    let entry_tokens: Vec<(i64, BTreeSet<String>)> = ld
        .entries
        .iter()
        .map(|e| (e.id, token_set(&e.text)))
        .collect();
    let contexts: Contexts = gc::entity_context_vectors(
        entities,
        &ld.entries,
        &entry_tokens,
        &ld.traces,
        cfg.min_entity_mentions,
        (cfg.max_fallback_mentions != 0).then_some(cfg.max_fallback_mentions),
    )
    .map_err(internal)?;
    let mut excluded: HashSet<i64> = junk_owned.clone();
    excluded.extend(gr::lesson_only_ids(&review_edges, &ld.lesson_refs));
    if crate::mutants::active("mcp-dream-deep-no-exclusion") {
        excluded.clear();
    }
    let near = gc::candidate_pairs(
        &contexts,
        edges,
        entities,
        &ld.scope_map,
        &dups,
        &CandidateOpts {
            min_similarity: cfg.min_similarity,
            top_k: cfg.top_k_candidates,
            dismissed: &ld.dismissed,
            max_support_overlap: cfg.max_support_overlap,
            pending_pairs: &ld.pending_links,
            excluded_ids: &excluded,
        },
    );
    let (mut merge_cands, link_cands): (Vec<MergeCand>, Vec<Candidate>) = gc::partition_candidates(
        near,
        entities,
        &deg,
        &ld.fact_counts,
        cfg.merge_min_similarity,
    );
    merge_cands.retain(|m| gr::merge_veto(&m.from, &m.into).is_none());
    merge_cands.retain(|m| !junk_owned.contains(&m.from_id) && !junk_owned.contains(&m.into_id));

    let snippets = snippets_for(&ld, &contexts, &entry_tokens, cfg);
    let candidates: Vec<Value> = link_cands
        .iter()
        .map(|c| {
            let mut m = c.to_map();
            if include_snippets {
                let a = snippets.attach(Some(c.src_id), Some(c.dst_id), false);
                m.insert("src_snippets".into(), json!(a.src));
                m.insert("dst_snippets".into(), json!(a.dst));
            }
            Value::Object(m)
        })
        .collect();
    let merge_proposals = merge_rows(
        &ld,
        &contexts,
        &entry_tokens,
        &deg,
        cfg,
        &ld.pending_props,
        include_snippets,
    )?;
    let lesson_dups = gc::slot_duplicate_candidates(
        &lesson_recs,
        cfg.curation_min_similarity,
        cfg.curation_top_k,
        &store_dismissed(&ld.dismissed, "lesson"),
    );
    let world_dups = gc::slot_duplicate_candidates(
        &world_recs,
        cfg.curation_min_similarity,
        cfg.curation_top_k,
        &store_dismissed(&ld.dismissed, "world"),
    );
    let totals = json!({"entities": entities.len(), "edges": edges.len(),
                        "candidates": candidates.len()});
    let mut facts_by_norm: HashMap<String, i64> = HashMap::new();
    for (text, n) in &ld.fact_texts {
        let k = norm_name(text);
        if !k.is_empty() {
            *facts_by_norm.entry(k).or_default() += n;
        }
    }

    Ok(Plan {
        ld,
        disp,
        deg,
        review_edges,
        rescore,
        violations,
        dups,
        junk,
        junk_owned,
        entry_tokens,
        contexts,
        merge_cands,
        candidates,
        merge_proposals,
        lesson_dups,
        world_dups,
        totals,
        facts_by_norm,
    })
}

/// `service_dream.deep_dream` (3355-3675), unbounded. The writer lock is
/// held for the read snapshot; the computation then runs on a blocking
/// thread. The dry run computes with the lock released and takes it again
/// for the orphan census alone, as Python does; the apply keeps it from the
/// snapshot read to the end (Python releases it between its windows;
/// declared).
#[allow(clippy::result_large_err)]
async fn deep_dream(
    service: &Service,
    ready: &Ready,
    apply: bool,
    include_snippets: bool,
) -> Result<Map<String, Value>, ToolError> {
    let cfg = service.config.memory.deep_dream.clone();
    let client = ready.storage.client();
    let read_lock = ready.write.lock().await;
    let ld = crate::txn::with_client(client, |c| async move { load(c).await })
        .await
        .map_err(internal)?;
    let (lesson_recs, world_recs) = curation_records(ready, cfg.snippet_max_chars);
    let _apply_lock = if apply {
        Some(read_lock)
    } else {
        drop(read_lock);
        None
    };
    let Plan {
        ld,
        disp,
        deg,
        review_edges,
        rescore,
        violations,
        dups,
        junk,
        junk_owned,
        entry_tokens,
        contexts,
        merge_cands,
        candidates,
        merge_proposals,
        lesson_dups,
        world_dups,
        totals,
        facts_by_norm,
    } = {
        let cfg = cfg.clone();
        tokio::task::spawn_blocking(move || {
            plan(ld, lesson_recs, world_recs, &cfg, include_snippets)
        })
        .await
        .map_err(internal)??
    };
    let entities = &ld.entities;
    let canon: HashMap<i64, &str> = entities
        .iter()
        .map(|e| (e.id, e.canonical.as_str()))
        .collect();

    if !apply {
        // The census reads the bank: Python takes its lock again for it.
        let would_orphan = {
            let _census = ready.write.lock().await;
            unreachable_orphans(
                client,
                &cfg,
                &entry_tokens,
                &contexts.mentions,
                &facts_by_norm,
                &HashSet::new(),
            )
            .await?
        };
        let census: Vec<Value> = would_orphan
            .iter()
            .take(200)
            .map(|c| {
                // `round((time() - float(created_at or time())) / 86400, 1)`.
                let t = now();
                let created = if c.created_at != 0.0 {
                    c.created_at
                } else {
                    now()
                };
                json!({"entity": c.display, "age_days": py_round((t - created) / 86400.0, 1)})
            })
            .collect();
        let would_merge: Vec<Value> = dups
            .iter()
            .map(|&(f, t)| {
                let name = |id: i64| disp.get(&id).cloned().unwrap_or_else(|| id.to_string());
                json!({"from": name(f), "into": name(t)})
            })
            .collect();
        let would_merge_propose: Vec<Value> = merge_cands
            .iter()
            .map(|m| {
                let key = PropKey::Merge(m.from_id.min(m.into_id), m.from_id.max(m.into_id));
                json!({"from": m.from, "into": m.into, "similarity": m.similarity,
                       "reason": m.reason, "already_proposed": ld.prop_keys.contains(&key)})
            })
            .collect();
        let would_junk: Vec<Value> = junk
            .iter()
            .map(|j| {
                let key = PropKey::Kind("junk".into(), j.entity_id);
                json!({"entity": j.display, "reason": j.reason,
                       "already_proposed": ld.prop_keys.contains(&key)})
            })
            .collect();
        let mut out = Map::new();
        out.insert("dry_run".into(), json!(true));
        out.insert("rescored".into(), json!(rescore.len()));
        out.insert("would_orphan_count".into(), json!(would_orphan.len()));
        out.insert("would_orphan".into(), json!(census));
        out.insert(
            "would_supersede".into(),
            json!(
                violations
                    .iter()
                    .map(|e| edge_label(e, &disp))
                    .collect::<Vec<_>>()
            ),
        );
        out.insert("would_merge".into(), json!(would_merge));
        out.insert("would_merge_propose".into(), json!(would_merge_propose));
        out.insert("would_junk".into(), json!(would_junk));
        out.insert("merge_proposals".into(), json!(merge_proposals));
        out.insert("lesson_duplicates".into(), json!(lesson_dups));
        out.insert("world_duplicates".into(), json!(world_dups));
        out.insert("candidates".into(), json!(candidates));
        out.insert("totals".into(), totals);
        return Ok(out);
    }

    let Some(snapshot) = write_graph_snapshot(&service.data_dir, client, cfg.snapshot_keep).await?
    else {
        let mut out = Map::new();
        out.insert("error".into(), json!("snapshot_failed"));
        out.insert("applied".into(), json!(false));
        out.insert(
            "detail".into(),
            json!("pre-apply graph snapshot could not be written; nothing changed"),
        );
        return Ok(out);
    };
    let (mut superseded, mut merged, mut merge_proposed, mut junk_proposed) = (0, 0, 0, 0);
    let (mut junk_deleted, mut scoped, mut analyzer_filed, mut orphans_deleted) = (0, 0, 0, 0);
    // Entities removed by this apply: later steps must not reference them.
    let mut dropped: HashSet<i64> = HashSet::new();
    if !crate::mutants::active("mcp-dream-deep-no-rescore") {
        for &(eid, conf) in &rescore {
            set_edge_confidence(client, eid, conf).await?;
        }
    }
    if cfg.auto_apply_safe || crate::mutants::active("mcp-dream-deep-supersede-unsafe") {
        for e in &violations {
            if graph::supersede_edge(client, e.src_id, &e.relation, e.dst_id)
                .await
                .map_err(internal)?
            {
                superseded += 1;
            }
        }
    }
    if cfg.auto_apply_safe {
        for &(frm, into) in &dups {
            if graph::merge_entity(client, frm, into)
                .await
                .map_err(internal)?
            {
                merged += 1;
                dropped.insert(frm);
            }
        }
    }
    // Junk proposals, then the unattended junk deletions, BEFORE the merge
    // filing so a proposal never lands on an id this pass deletes.
    let now = now();
    for j in &junk {
        if super::relations::insert_entity_proposal_row(
            client,
            "junk",
            j.entity_id,
            None,
            None,
            Some(j.reason),
            crate::episodes::now(),
        )
        .await?
        .is_some()
        {
            junk_proposed += 1;
        }
    }
    let canon_by_id = &canon;
    let tombstones: HashSet<String> = crate::txn::with_client(client, |c| async move {
        c.query(
            "SELECT DISTINCT entity_display FROM merge_decisions \
             WHERE status = 'accepted' AND into_display IS NULL",
            &[],
        )
        .await
    })
    .await
    .map_err(internal)?
    .iter()
    // `entity_display` is nullable; `norm_name(None)` is "".
    .map(|r| norm_name(r.get::<_, Option<String>>(0).as_deref().unwrap_or("")))
    .collect();
    let pending =
        crate::txn::with_client(client, |c| async move { pending_entity_proposals(c).await })
            .await
            .map_err(internal)?;
    for p in pending.iter().filter(|p| p.kind == "junk") {
        let Some(eid) = p.entity_id else {
            continue;
        };
        let display = disp
            .get(&eid)
            .cloned()
            .or_else(|| p.entity.clone().filter(|e| !e.is_empty()))
            .unwrap_or_else(|| "?".into());
        // A tombstone relaxes the degree bar only; the fact bar holds.
        let nd = norm_name(&display);
        let facts_at = |k: &str| facts_by_norm.get(k).copied().unwrap_or(0);
        let contentless = ld
            .fact_counts
            .get(&eid)
            .copied()
            .unwrap_or(0)
            .max(facts_at(&nd))
            .max(facts_at(canon_by_id.get(&eid).copied().unwrap_or("")))
            <= 1;
        let degree = deg.get(&eid).copied().unwrap_or(0) as i64;
        let zero_structure = degree == 0 && contentless;
        let tombstoned = contentless
            && tombstones.contains(&nd)
            && (degree <= cfg.junk_max_degree
                || crate::mutants::active("mcp-dream-deep-tombstone-any-degree"));
        if !(zero_structure || tombstoned) {
            continue;
        }
        let pid = p.id;
        let votes = actions::reads(client, |c| async move {
            actions::entity_proposal_votes_body(c, pid).await
        })
        .await?;
        if graph::delete_entity(client, eid).await.map_err(internal)? {
            let suffix = if zero_structure { "" } else { " (tombstoned)" };
            let reason = format!(
                "junk auto-delete: {}{suffix}",
                p.reason.as_deref().unwrap_or("None")
            );
            let score = p.score.map(|s| s as f32);
            let display2 = display.clone();
            actions::in_txn(client, |c| async move {
                actions::record_merge_decision_by(
                    c,
                    pid,
                    &display2,
                    None,
                    "accepted",
                    score,
                    Some(&reason),
                    "dream-auto",
                    now,
                    votes,
                )
                .await
            })
            .await?;
            junk_deleted += 1;
            dropped.insert(eid);
        }
    }
    // Non-destructive: populate the review queue regardless of auto_apply_safe.
    for m in &merge_cands {
        if (dropped.contains(&m.from_id) || dropped.contains(&m.into_id))
            && !crate::mutants::active("mcp-dream-deep-dropped-merge-filed")
        {
            continue;
        }
        if super::relations::insert_entity_proposal_row(
            client,
            "merge",
            m.from_id,
            Some(m.into_id),
            Some(m.similarity),
            Some(m.reason),
            crate::episodes::now(),
        )
        .await?
        .is_some()
        {
            merge_proposed += 1;
        }
    }
    // Scope stamping from the mentioning entries' sources.
    let scopes = &service.config.memory;
    let fold = |s: &str| py_lower(py_strip(s));
    let excl: HashSet<String> = scopes.scopes_exclude.iter().map(|s| fold(s)).collect();
    let mut roll: HashMap<String, String> = HashMap::new();
    for (k, v) in &scopes.scopes_rollup {
        roll.insert(fold(k), fold(v));
    }
    let entry_source: HashMap<i64, &str> = ld
        .entries
        .iter()
        .map(|e| (e.id, e.source.as_deref().unwrap_or("")))
        .collect();
    for e in entities {
        let eid = e.id;
        if dropped.contains(&eid) {
            continue;
        }
        let Some(mentioned) = contexts.mentions.get(&eid).filter(|m| !m.is_empty()) else {
            continue;
        };
        if ld.scope_map.get(&eid).is_some_and(|s| !s.is_empty()) {
            continue;
        }
        let mut keys: BTreeSet<String> = BTreeSet::new();
        for entry_id in mentioned {
            let key = fold(entry_source.get(entry_id).copied().unwrap_or(""));
            if key.is_empty() || excl.contains(&key) {
                continue;
            }
            if let Some(umb) = roll.get(&key)
                && !umb.is_empty()
                && *umb != key
                && !excl.contains(umb)
                && !crate::mutants::active("mcp-dream-deep-no-rollup")
            {
                keys.insert(umb.clone());
            }
            keys.insert(key);
        }
        for key in &keys {
            super::relations::upsert_entity_source_at(client, eid, key, now).await?;
        }
        if !keys.is_empty() {
            scoped += 1;
        }
    }
    if cfg.analyzer_file_duplicates {
        let excluded: HashSet<i64> = dropped.union(&junk_owned).copied().collect();
        analyzer_filed = file_analyzer_duplicates(
            client,
            entities,
            &review_edges,
            &ld.dismissed,
            &ld.lesson_refs,
            &ld.fact_counts,
            &excluded,
            &ld.prop_keys,
            now,
        )
        .await?;
    }
    if cfg.orphan_sweep {
        let cap = cfg.orphan_max_per_apply;
        let census = unreachable_orphans(
            client,
            &cfg,
            &entry_tokens,
            &contexts.mentions,
            &facts_by_norm,
            &dropped,
        )
        .await?;
        for c in census {
            if cap > 0 && orphans_deleted >= cap {
                break;
            }
            if delete_entity_audited(client, c.id, &c.display, now).await? {
                dropped.insert(c.id);
                orphans_deleted += 1;
            }
        }
    }
    // The stamp resets the deep-dream need signal; the re-read shows the
    // proposals this apply filed.
    if !crate::mutants::active("mcp-dream-deep-no-stamp") {
        let ts = crate::episodes::now();
        let max_id: i64 = crate::txn::with_client(client, |c| async move {
            c.query_one("SELECT coalesce(max(id), 0)::bigint FROM entities", &[])
                .await
                .map(|r| r.get(0))
        })
        .await
        .map_err(internal)?;
        crate::txn::set_meta(
            client,
            "deep_last_apply",
            &json!({"ts": ts, "max_entity_id": max_id}),
        )
        .await
        .map_err(internal)?;
    }
    let pending =
        crate::txn::with_client(client, |c| async move { pending_entity_proposals(c).await })
            .await
            .map_err(internal)?;
    let merge_proposals = {
        let cfg = cfg.clone();
        tokio::task::spawn_blocking(move || {
            merge_rows(
                &ld,
                &contexts,
                &entry_tokens,
                &deg,
                &cfg,
                &pending,
                include_snippets,
            )
        })
        .await
        .map_err(internal)??
    };
    let mut out = Map::new();
    out.insert("applied".into(), json!(true));
    out.insert("rescored".into(), json!(rescore.len()));
    out.insert("superseded".into(), json!(superseded));
    out.insert("merged".into(), json!(merged));
    out.insert("merge_proposed".into(), json!(merge_proposed));
    out.insert("junk_proposed".into(), json!(junk_proposed));
    out.insert("junk_deleted".into(), json!(junk_deleted));
    out.insert("scoped".into(), json!(scoped));
    out.insert("analyzer_filed".into(), json!(analyzer_filed));
    out.insert("orphans_deleted".into(), json!(orphans_deleted));
    out.insert("snapshot".into(), json!(snapshot));
    out.insert("merge_proposals".into(), json!(merge_proposals));
    out.insert("lesson_duplicates".into(), json!(lesson_dups));
    out.insert("world_duplicates".into(), json!(world_dups));
    out.insert("candidates".into(), json!(candidates));
    out.insert("totals".into(), totals);
    Ok(out)
}

// ── the merge judge's evidence pack (`service_dream.py:4396-4428`) ────────

/// The storage reads behind the merge-judge evidence pack
/// (`_judge_evidence_locked`): the graph, scopes, traces, entry TEXTS and
/// fact counts. No embedding is read.
pub(crate) struct JudgeEvidence {
    entities: Vec<Entity>,
    etype: HashMap<i64, Option<String>>,
    edges: Vec<GEdge>,
    scope_map: BTreeMap<i64, Vec<String>>,
    traces: HashMap<String, Vec<i64>>,
    entries: Vec<DEntry>,
    fact_counts: HashMap<i64, i64>,
}

/// `_judge_evidence_locked` on a borrowed client (the caller holds the
/// writer lock).
pub(crate) async fn judge_evidence_body(c: &Client) -> anyhow::Result<JudgeEvidence> {
    let g = graph::load_graph_body(c).await?;
    let s = |v: &Value| v.as_str().unwrap_or_default().to_string();
    let mut entities = Vec::new();
    let mut etype = HashMap::new();
    for e in g["entities"].as_array().into_iter().flatten() {
        let id = e["id"].as_i64().unwrap_or(0);
        entities.push(Entity {
            id,
            canonical: s(&e["canonical"]),
            display: s(&e["display"]),
        });
        etype.insert(id, e["etype"].as_str().map(str::to_string));
    }
    let edges = g["edges"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| GEdge {
            id: e["id"].as_i64().unwrap_or(0),
            src_id: e["src_id"].as_i64().unwrap_or(0),
            relation: s(&e["relation"]),
            dst_id: e["dst_id"].as_i64().unwrap_or(0),
            confidence: e["confidence"].as_f64().unwrap_or(0.0),
            origin: e["origin"].as_str().map(str::to_string),
        })
        .collect();
    let mut scope_map: BTreeMap<i64, Vec<String>> = BTreeMap::new();
    for r in c
        .query(
            "SELECT entity_id, source FROM entity_sources ORDER BY entity_id, source",
            &[],
        )
        .await?
    {
        scope_map.entry(r.get(0)).or_default().push(r.get(1));
    }
    let mut traces: HashMap<String, Vec<i64>> = HashMap::new();
    for r in c
        .query(
            "SELECT entity_norm, entry_id FROM memory_traces ORDER BY entity_norm, entry_id",
            &[],
        )
        .await?
    {
        traces.entry(r.get(0)).or_default().push(r.get(1));
    }
    // `load_entry_texts` (1447-1455).
    let entries = c
        .query("SELECT id, text, source FROM entries ORDER BY id", &[])
        .await?
        .iter()
        .map(|r| DEntry {
            id: r.get(0),
            text: r.get(1),
            source: r.get(2),
            embedding: None,
        })
        .collect();
    let fact_counts = c
        .query(
            "SELECT entity_id, COUNT(*) FROM facts \
             WHERE status = 'current' AND entity_id IS NOT NULL GROUP BY entity_id",
            &[],
        )
        .await?
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect();
    Ok(JudgeEvidence {
        entities,
        etype,
        edges,
        scope_map,
        traces,
        entries,
        fact_counts,
    })
}

/// `entity_context_vectors(..., with_vectors=False)`: the mentions alone,
/// over entries that need no embedding (every listed entry counts).
fn context_mentions(
    entities: &[&Entity],
    entries: &[DEntry],
    entry_tokens: &[(i64, BTreeSet<String>)],
    traces: &HashMap<String, Vec<i64>>,
    min_mentions: i64,
    max_fallback: Option<i64>,
) -> HashMap<i64, BTreeSet<i64>> {
    let ids_known: HashSet<i64> = entries.iter().map(|e| e.id).collect();
    let mut out = HashMap::new();
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
        let valid: BTreeSet<i64> = ids.into_iter().filter(|i| ids_known.contains(i)).collect();
        if (valid.len() as i64) < min_mentions {
            continue;
        }
        out.insert(ent.id, valid);
    }
    out
}

/// `_judge_enrich_from` (4407-4428): the merge-judge evidence pack for
/// `pending`, built at the JUDGE's snippet cap. Pure over `ev`.
#[allow(clippy::result_large_err)]
pub(crate) fn judge_enrich_from(
    pending: &[EntityProposal],
    ev: &JudgeEvidence,
    cfg: &DeepDreamConfig,
) -> Result<Vec<Value>, ToolError> {
    let ids: HashSet<Option<i64>> = pending
        .iter()
        .flat_map(|p| [p.entity_id, p.into_id])
        .collect();
    let own: Vec<&Entity> = ev
        .entities
        .iter()
        .filter(|e| ids.contains(&Some(e.id)))
        .collect();
    let entry_tokens: Vec<(i64, BTreeSet<String>)> = ev
        .entries
        .iter()
        .map(|e| (e.id, token_set(&e.text)))
        .collect();
    let mentions = context_mentions(
        &own,
        &ev.entries,
        &entry_tokens,
        &ev.traces,
        cfg.min_entity_mentions,
        (cfg.max_fallback_mentions != 0).then_some(cfg.max_fallback_mentions),
    );
    let review_edges: Vec<gr::Edge> = ev.edges.iter().map(GEdge::review_edge).collect();
    let deg = gr::degree_counts(&review_edges);
    let snippets = Snippets {
        by_id: ev.entries.iter().map(|e| (e.id, e)).collect(),
        canon: ev
            .entities
            .iter()
            .map(|e| (e.id, e.canonical.as_str()))
            .collect(),
        disp: ev
            .entities
            .iter()
            .map(|e| (e.id, e.display.as_str()))
            .collect(),
        traces: &ev.traces,
        mentions: &mentions,
        entry_tokens: &entry_tokens,
        k: cfg.max_context_snippets,
        max_chars: cfg.judge_snippet_max_chars,
    };
    let enrich = Enrich {
        deg: &deg,
        facts: &ev.fact_counts,
        display: snippets.disp.clone(),
        etype: &ev.etype,
        scope_map: &ev.scope_map,
        snippets: &snippets,
    };
    enrich.rows(pending, true)
}

/// `_write_graph_snapshot_locked` on a borrowed client, inside the
/// caller's transaction (the merge judge's snapshot-first accept).
pub(crate) async fn write_graph_snapshot_body(
    c: &Client,
    data_dir: &Path,
    snapshot_keep: i64,
) -> Result<Option<String>, ToolError> {
    let tables = dump_graph_tables(c).await.map_err(internal)?;
    Ok(write_snapshot_file(data_dir, &tables, snapshot_keep))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bound_keeps_small_answers_and_halves_the_head() {
        let mut small = Map::new();
        small.insert("candidates".into(), json!([1, 2, 3]));
        assert_eq!(bound(small.clone()), small);
        // Each item ~10 KB: 60 items overflow; 40, then 20 still overflow.
        let item = "x".repeat(10_000);
        let mut big = Map::new();
        big.insert("dry_run".into(), json!(true));
        big.insert("candidates".into(), json!(vec![item.clone(); 60]));
        big.insert("would_junk".into(), json!(vec![item.clone(); 3]));
        big.insert("totals".into(), json!({"candidates": 60}));
        let out = bound(big);
        let keys: Vec<&String> = out.keys().collect();
        assert_eq!(
            keys,
            [
                "dry_run",
                "candidates",
                "would_junk",
                "totals",
                "truncated",
                "hint"
            ]
        );
        // 20 items + 3 = 230 KB fits.
        assert_eq!(out["candidates"].as_array().unwrap().len(), 20);
        assert_eq!(out["truncated"], json!({"candidates": 60}));
        assert!(out["hint"].as_str().unwrap().contains("leading 20 items"));
    }

    #[test]
    fn kept_and_store_dismissals_follow_their_prefixes() {
        let d: HashSet<(String, String)> = [
            ("junk:kk", "junk:kk"),
            ("junk:a", "junk:b"),
            ("lesson:x|y", "lesson:z|w"),
            ("lesson:x|y", "world:z|w"),
            ("a", "b"),
        ]
        .iter()
        .map(|(a, b)| (a.to_string(), b.to_string()))
        .collect();
        assert_eq!(kept_junk_norms(&d), HashSet::from(["kk".to_string()]));
        assert_eq!(
            store_dismissed(&d, "lesson"),
            HashSet::from([("x|y".to_string(), "z|w".to_string())])
        );
    }
}
