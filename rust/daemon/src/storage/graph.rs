//! Graph storage APIs; oracle: storage/postgres.py:2732–3139,4292–4317.
use serde_json::{Value, json};
use tokio_postgres::{Client, Row};

#[path = "graph_lower.rs"]
mod unicode14;

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock before Unix epoch")
        .as_secs_f64()
}

/// Graph names differ from cortex slot keys.
pub fn norm_name(raw: &str) -> String {
    if crate::mutants::active("graph-normalize") {
        return unicode14::lower(super::py_strip(raw));
    }
    let mut out = String::new();
    for c in unicode14::lower(super::py_strip(raw)).chars() {
        let c = if c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c) || "_/\\.:".contains(c) {
            '-'
        } else {
            c
        };
        if c != '-' || !out.ends_with('-') {
            out.push(c);
        }
    }
    out.trim_matches('-').to_owned()
}

fn entity(row: &Row) -> Value {
    json!({"id": row.get::<_, i64>(0), "canonical": row.get::<_, String>(1),
        "display": row.get::<_, String>(2), "etype": row.get::<_, Option<String>>(3),
        "created_at": row.get::<_, f64>(4)})
}

async fn ensure_entity_body(
    client: &Client,
    canonical: &str,
    display: Option<&str>,
    etype: Option<&str>,
) -> anyhow::Result<i64> {
    let tx = client;
    let display = display.filter(|s| !s.is_empty()).unwrap_or(canonical);
    let row = tx
        .query_one(
            "INSERT INTO entities (canonical,display,etype,created_at) \
        VALUES ($1,$2,$3,$4) ON CONFLICT (canonical) DO UPDATE \
        SET etype=COALESCE(entities.etype,EXCLUDED.etype) RETURNING id,(xmax=0)",
            &[&canonical, &display, &etype, &now()],
        )
        .await?;
    let id: i64 = row.get(0);
    if row.get::<_, bool>(1) && !canonical.is_empty() && !crate::mutants::active("graph-relink") {
        // Python's necessary substring prefilter; exact graph norm remains
        // authoritative. Mint-only repair is part of this transaction.
        let segment = canonical
            .split('-')
            .reduce(|best, next| {
                if next.chars().count() > best.chars().count() {
                    next
                } else {
                    best
                }
            })
            .unwrap();
        let like = format!(
            "%{}%",
            segment
                .replace('\\', "\\\\")
                .replace('%', "\\%")
                .replace('_', "\\_")
        );
        for (table, id_col, text_col) in [
            ("facts", "entity_id", "entity"),
            ("facts", "object_entity_id", "value"),
            ("lessons", "entity_id", "entity"),
            ("lessons", "object_entity_id", "about"),
        ] {
            let rows = tx
                .query(
                    &format!(
                        "SELECT DISTINCT {text_col} FROM {table} \
                WHERE {id_col} IS NULL AND {text_col} IS NOT NULL AND {text_col} ILIKE $1"
                    ),
                    &[&like],
                )
                .await?;
            for row in rows {
                let text: String = row.get(0);
                if norm_name(&text) == canonical {
                    tx.execute(
                        &format!(
                            "UPDATE {table} SET {id_col}=$1 \
                        WHERE {id_col} IS NULL AND {text_col}=$2"
                        ),
                        &[&id, &text],
                    )
                    .await?;
                }
            }
        }
    }
    Ok(id)
}

async fn find_entity_body(client: &Client, name: &str) -> anyhow::Result<Value> {
    let canonical = client
        .query_opt(
            "SELECT id,canonical,display,etype,created_at FROM entities \
        WHERE canonical=$1",
            &[&name],
        )
        .await?;
    let row = match canonical {
        Some(row) if !crate::mutants::active("graph-alias") => Some(row),
        canonical => client
            .query_opt(
                "SELECT e.id,e.canonical,e.display,e.etype,e.created_at \
            FROM entity_aliases a JOIN entities e ON e.id=a.entity_id WHERE a.alias=$1",
                &[&name],
            )
            .await?
            .or(canonical),
    };
    let Some(row) = row else {
        return Ok(Value::Null);
    };
    let mut result = entity(&row);
    let aliases: Vec<String> = client
        .query(
            "SELECT alias FROM entity_aliases WHERE entity_id=$1 \
        ORDER BY alias",
            &[&row.get::<_, i64>(0)],
        )
        .await?
        .iter()
        .map(|r| r.get(0))
        .collect();
    result["aliases"] = json!(aliases);
    Ok(result)
}

async fn add_alias_body(client: &Client, alias: &str, id: i64) -> anyhow::Result<()> {
    client
        .execute(
            "INSERT INTO entity_aliases(alias,entity_id) VALUES($1,$2) \
        ON CONFLICT(alias) DO UPDATE SET entity_id=EXCLUDED.entity_id",
            &[&alias, &id],
        )
        .await?;
    Ok(())
}

async fn entity_id_map_body(client: &Client) -> anyhow::Result<Value> {
    let mut map = serde_json::Map::new();
    for row in client
        .query("SELECT alias,entity_id FROM entity_aliases", &[])
        .await?
    {
        map.insert(row.get(0), json!(row.get::<_, i64>(1)));
    }
    for row in client
        .query("SELECT canonical,id FROM entities", &[])
        .await?
    {
        map.insert(row.get(0), json!(row.get::<_, i64>(1)));
    }
    Ok(Value::Object(map))
}

async fn load_relations_body(client: &Client) -> anyhow::Result<Value> {
    let rows = client
        .query(
            "SELECT name,description,src_type,dst_type,transitive,inverse_of,builtin \
        FROM relations ORDER BY name",
            &[],
        )
        .await?;
    Ok(json!(
        rows.iter()
            .map(|r| json!({"name": r.get::<_, String>(0),
        "description": r.get::<_, String>(1), "src_type": r.get::<_, Option<String>>(2),
        "dst_type": r.get::<_, Option<String>>(3), "transitive": r.get::<_, bool>(4),
        "inverse_of": r.get::<_, Option<String>>(5), "builtin": r.get::<_, bool>(6)}))
            .collect::<Vec<_>>()
    ))
}

pub struct Relation<'a> {
    pub name: &'a str,
    pub description: &'a str,
    pub src_type: Option<&'a str>,
    pub dst_type: Option<&'a str>,
    pub transitive: bool,
    pub inverse_of: Option<&'a str>,
}

async fn upsert_relation_body(client: &Client, r: Relation<'_>) -> anyhow::Result<()> {
    client.execute("INSERT INTO relations(name,description,src_type,dst_type,transitive,inverse_of,builtin,created_at) \
        VALUES($1,$2,$3,$4,$5,$6,FALSE,$7) ON CONFLICT(name) DO UPDATE SET \
        description=EXCLUDED.description,src_type=EXCLUDED.src_type,dst_type=EXCLUDED.dst_type,\
        transitive=EXCLUDED.transitive,inverse_of=EXCLUDED.inverse_of",
        &[&r.name, &r.description, &r.src_type, &r.dst_type, &r.transitive, &r.inverse_of, &now()]).await?;
    Ok(())
}

pub struct Edge<'a> {
    pub src_id: i64,
    pub relation: &'a str,
    pub dst_id: i64,
    pub confidence: f64,
    pub origin: Option<&'a str>,
    pub revive: bool,
    pub source_entry_ids: &'a [i64],
}

async fn upsert_edge_body(client: &Client, edge: Edge<'_>) -> anyhow::Result<Value> {
    let tx = client;
    let mut ids = edge.source_entry_ids.to_vec();
    ids.sort_unstable();
    ids.dedup();
    if !ids.is_empty() {
        ids = tx
            .query(
                "SELECT id FROM entries WHERE id=ANY($1) AND superseded_at IS NULL \
            ORDER BY id FOR UPDATE",
                &[&ids],
            )
            .await?
            .iter()
            .map(|r| r.get(0))
            .collect();
        if ids.is_empty() {
            return Ok(Value::Null);
        }
    }
    let revive = edge.revive || crate::mutants::active("graph-revive");
    let bump = if crate::mutants::active("graph-confidence") {
        "0.0"
    } else {
        "0.05"
    };
    let origin = if crate::mutants::active("graph-origin") {
        "EXCLUDED.origin"
    } else {
        "CASE WHEN CASE edges.origin WHEN 'user' THEN 3 WHEN 'action' THEN 2 WHEN 'agent' THEN 1 ELSE 0 END \
         > CASE EXCLUDED.origin WHEN 'user' THEN 3 WHEN 'action' THEN 2 WHEN 'agent' THEN 1 ELSE 0 END \
         THEN edges.origin ELSE EXCLUDED.origin END"
    };
    let row = tx.query_one(&format!("INSERT INTO edges(src_id,relation,dst_id,confidence,origin,asserted_at) \
        VALUES($1,$2,$3,$4::double precision,$5,$6) ON CONFLICT(src_id,relation,dst_id) DO UPDATE SET \
        confidence=LEAST(0.99,GREATEST(EXCLUDED.confidence,edges.confidence+{bump})), \
        origin={origin},superseded_at=CASE WHEN $7 THEN NULL ELSE edges.superseded_at END,\
        asserted_at=EXCLUDED.asserted_at RETURNING id,confidence::text::double precision"),
        &[&edge.src_id, &edge.relation, &edge.dst_id, &edge.confidence, &edge.origin, &now(), &revive]).await?;
    let id: i64 = row.get(0);
    if !ids.is_empty() {
        tx.execute(
            "INSERT INTO edge_evidence(edge_id,entry_id) SELECT $1,e.id FROM entries e \
            WHERE e.id=ANY($2) AND e.superseded_at IS NULL ON CONFLICT DO NOTHING",
            &[&id, &ids],
        )
        .await?;
    }
    // Psycopg's text-format REAL decoder parses PostgreSQL's shortest
    // spelling, rather than widening its binary float32 value.
    Ok(json!({"id": id, "confidence": row.get::<_, f64>(1)}))
}

async fn bless_edge_body(
    client: &Client,
    src: i64,
    relation: &str,
    dst: i64,
    confidence: f64,
) -> anyhow::Result<bool> {
    let live = if crate::mutants::active("graph-bless") {
        ""
    } else {
        " AND superseded_at IS NULL"
    };
    let count = client
        .execute(
            &format!(
                "UPDATE edges SET confidence=GREATEST(confidence,$1::double precision),\
        origin='user',asserted_at=$2 WHERE src_id=$3 AND relation=$4 AND dst_id=$5{live}"
            ),
            &[&confidence, &now(), &src, &relation, &dst],
        )
        .await?;
    Ok(count > 0)
}

async fn supersede_edge_body(
    client: &Client,
    src: i64,
    relation: &str,
    dst: i64,
) -> anyhow::Result<bool> {
    Ok(client
        .execute(
            "UPDATE edges SET superseded_at=$1 WHERE src_id=$2 AND relation=$3 AND dst_id=$4 \
        AND superseded_at IS NULL",
            &[&now(), &src, &relation, &dst],
        )
        .await?
        > 0)
}

async fn unlink(tx: &Client, id: i64) -> anyhow::Result<()> {
    for table in ["facts", "lessons"] {
        for col in ["entity_id", "object_entity_id"] {
            tx.execute(
                &format!("UPDATE {table} SET {col}=NULL WHERE {col}=$1"),
                &[&id],
            )
            .await?;
        }
    }
    Ok(())
}

async fn delete_entity_body(client: &Client, id: i64) -> anyhow::Result<bool> {
    let tx = client;
    unlink(tx, id).await?;
    let deleted = tx
        .query_opt("DELETE FROM entities WHERE id=$1 RETURNING id", &[&id])
        .await?
        .is_some();
    Ok(deleted)
}

async fn merge_entity_body(client: &Client, from: i64, into: i64) -> anyhow::Result<bool> {
    if from == into {
        return Ok(false);
    }
    let tx = client;
    if tx
        .query(
            "SELECT id FROM entities WHERE id IN ($1,$2)",
            &[&from, &into],
        )
        .await?
        .len()
        != 2
    {
        return Ok(false);
    }
    for sql in [
        "DELETE FROM edges f WHERE f.src_id=$1 AND EXISTS(SELECT 1 FROM edges t WHERE t.src_id=$2 AND t.relation=f.relation AND t.dst_id=f.dst_id)",
        "DELETE FROM edges f WHERE f.dst_id=$1 AND EXISTS(SELECT 1 FROM edges t WHERE t.dst_id=$2 AND t.relation=f.relation AND t.src_id=f.src_id)",
        "DELETE FROM edges WHERE (src_id=$1 AND dst_id=$2) OR (src_id=$2 AND dst_id=$1)",
    ] {
        tx.execute(sql, &[&from, &into]).await?;
    }
    tx.execute("DELETE FROM edges WHERE src_id=$1 AND dst_id=$1", &[&from])
        .await?;
    tx.execute(
        "UPDATE edges SET src_id=$1 WHERE src_id=$2",
        &[&into, &from],
    )
    .await?;
    tx.execute(
        "UPDATE edges SET dst_id=$1 WHERE dst_id=$2",
        &[&into, &from],
    )
    .await?;
    if !crate::mutants::active("graph-merge") {
        for table in ["facts", "lessons"] {
            for col in ["entity_id", "object_entity_id"] {
                tx.execute(
                    &format!("UPDATE {table} SET {col}=$1 WHERE {col}=$2"),
                    &[&into, &from],
                )
                .await?;
            }
        }
    }
    tx.execute(
        "INSERT INTO entity_aliases(alias,entity_id) SELECT canonical,$2 FROM entities WHERE id=$1 \
        ON CONFLICT(alias) DO NOTHING",
        &[&from, &into],
    )
    .await?;
    tx.execute(
        "UPDATE entity_aliases SET entity_id=$1 WHERE entity_id=$2 AND alias NOT IN \
        (SELECT alias FROM entity_aliases WHERE entity_id=$1)",
        &[&into, &from],
    )
    .await?;
    tx.execute(
        "INSERT INTO entity_sources(entity_id,source,count,origin,updated_at) \
        SELECT $1,source,count,origin,updated_at FROM entity_sources WHERE entity_id=$2 \
        ON CONFLICT(entity_id,source) DO NOTHING",
        &[&into, &from],
    )
    .await?;
    tx.execute("DELETE FROM entities WHERE id=$1", &[&from])
        .await?;
    Ok(true)
}

async fn load_graph_body(client: &Client) -> anyhow::Result<Value> {
    let entities: Vec<Value> = client
        .query(
            "SELECT id,canonical,display,etype,created_at FROM entities \
        ORDER BY id",
            &[],
        )
        .await?
        .iter()
        .map(entity)
        .collect();
    let mut aliases = serde_json::Map::new();
    for row in client
        .query(
            "SELECT alias,entity_id FROM entity_aliases ORDER BY alias",
            &[],
        )
        .await?
    {
        aliases
            .entry(row.get::<_, i64>(1).to_string())
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .unwrap()
            .push(json!(row.get::<_, String>(0)));
    }
    let edges: Vec<Value> = client.query("SELECT id,src_id,relation,dst_id,confidence::text::double precision,origin,asserted_at \
        FROM edges WHERE superseded_at IS NULL ORDER BY id", &[]).await?.iter().map(|r| json!({
        "id": r.get::<_, i64>(0), "src_id": r.get::<_, i64>(1), "relation": r.get::<_, String>(2),
        "dst_id": r.get::<_, i64>(3), "confidence": r.get::<_, f64>(4),
        "origin": r.get::<_, Option<String>>(5), "asserted_at": r.get::<_, f64>(6)})).collect();
    Ok(json!({"entities": entities, "aliases": aliases, "edges": edges}))
}

/// Fixture transport only; ordinary callers use the APIs above.
#[cfg(feature = "graph-harness")]
pub async fn dispatch(client: &Client, r: &Value) -> anyhow::Result<Value> {
    let text = |k: &str| r.get(k).and_then(Value::as_str);
    let required = |k: &str| text(k).ok_or_else(|| anyhow::anyhow!("missing string {k}"));
    let id = |k: &str| {
        r.get(k)
            .and_then(Value::as_i64)
            .ok_or_else(|| anyhow::anyhow!("missing id {k}"))
    };
    let confidence = r.get("confidence").and_then(Value::as_f64).unwrap_or(0.8);
    match required("op")? {
        "subgraph" => {
            subgraph(
                client,
                id("root")?,
                r["depth"].as_i64().unwrap_or(1),
                r["to"].as_i64(),
            )
            .await
        }
        "norm_name" => Ok(json!(norm_name(required("raw")?))),
        "ensure_entity" => Ok(json!(
            ensure_entity(
                client,
                required("canonical")?,
                text("display"),
                text("etype")
            )
            .await?
        )),
        "find_entity" => find_entity(client, required("name_norm")?).await,
        "add_alias" => {
            add_alias(client, required("alias_norm")?, id("entity_id")?).await?;
            Ok(Value::Null)
        }
        "entity_id_map" => entity_id_map(client).await,
        "load_relations" => load_relations(client).await,
        "upsert_relation" => {
            upsert_relation(
                client,
                Relation {
                    name: required("name")?,
                    description: required("description")?,
                    src_type: text("src_type"),
                    dst_type: text("dst_type"),
                    transitive: r["transitive"].as_bool().unwrap_or(false),
                    inverse_of: text("inverse_of"),
                },
            )
            .await?;
            Ok(Value::Null)
        }
        "upsert_edge" => {
            let ids = r
                .get("source_entry_ids")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .map(|v| {
                            v.as_i64()
                                .ok_or_else(|| anyhow::anyhow!("invalid source id"))
                        })
                        .collect::<anyhow::Result<Vec<_>>>()
                })
                .transpose()?
                .unwrap_or_default();
            upsert_edge(
                client,
                Edge {
                    src_id: id("src_id")?,
                    relation: required("relation")?,
                    dst_id: id("dst_id")?,
                    confidence,
                    origin: text("origin"),
                    revive: r["revive"].as_bool().unwrap_or(true),
                    source_entry_ids: &ids,
                },
            )
            .await
        }
        "bless_edge" => Ok(json!(
            bless_edge(
                client,
                id("src_id")?,
                required("relation")?,
                id("dst_id")?,
                confidence
            )
            .await?
        )),
        "supersede_edge" => Ok(json!(
            supersede_edge(client, id("src_id")?, required("relation")?, id("dst_id")?).await?
        )),
        "delete_entity" => Ok(json!(delete_entity(client, id("entity_id")?).await?)),
        "merge_entity" => Ok(json!(
            merge_entity(client, id("from_id")?, id("into_id")?).await?
        )),
        "load_graph" => load_graph(client).await,
        _ => anyhow::bail!("unknown graph operation"),
    }
}

// Every graph statement uses the shared writer guard; bodies never nest it.
pub async fn ensure_entity(
    client: &Client,
    canonical: &str,
    display: Option<&str>,
    etype: Option<&str>,
) -> anyhow::Result<i64> {
    crate::txn::run(client, |client| async move {
        ensure_entity_body(client, canonical, display, etype).await
    })
    .await
}

pub async fn find_entity(client: &Client, name: &str) -> anyhow::Result<Value> {
    crate::txn::with_client(client, |client| async move {
        find_entity_body(client, name).await
    })
    .await
}

pub async fn add_alias(client: &Client, alias: &str, id: i64) -> anyhow::Result<()> {
    crate::txn::with_client(client, |client| async move {
        add_alias_body(client, alias, id).await
    })
    .await
}

pub async fn entity_id_map(client: &Client) -> anyhow::Result<Value> {
    crate::txn::with_client(
        client,
        |client| async move { entity_id_map_body(client).await },
    )
    .await
}

pub async fn load_relations(client: &Client) -> anyhow::Result<Value> {
    crate::txn::with_client(
        client,
        |client| async move { load_relations_body(client).await },
    )
    .await
}

pub async fn upsert_relation(client: &Client, r: Relation<'_>) -> anyhow::Result<()> {
    crate::txn::with_client(client, |client| async move {
        upsert_relation_body(client, r).await
    })
    .await
}

pub async fn upsert_edge(client: &Client, edge: Edge<'_>) -> anyhow::Result<Value> {
    crate::txn::run(client, |client| async move {
        upsert_edge_body(client, edge).await
    })
    .await
}

pub async fn bless_edge(
    client: &Client,
    src: i64,
    relation: &str,
    dst: i64,
    confidence: f64,
) -> anyhow::Result<bool> {
    crate::txn::with_client(client, |client| async move {
        bless_edge_body(client, src, relation, dst, confidence).await
    })
    .await
}

pub async fn supersede_edge(
    client: &Client,
    src: i64,
    relation: &str,
    dst: i64,
) -> anyhow::Result<bool> {
    crate::txn::with_client(client, |client| async move {
        supersede_edge_body(client, src, relation, dst).await
    })
    .await
}

pub async fn delete_entity(client: &Client, id: i64) -> anyhow::Result<bool> {
    crate::txn::run(client, |client| async move {
        delete_entity_body(client, id).await
    })
    .await
}

pub async fn merge_entity(client: &Client, from: i64, into: i64) -> anyhow::Result<bool> {
    crate::txn::run(client, |client| async move {
        merge_entity_body(client, from, into).await
    })
    .await
}

pub async fn load_graph(client: &Client) -> anyhow::Result<Value> {
    crate::txn::with_client(
        client,
        |client| async move { load_graph_body(client).await },
    )
    .await
}

/// The GraphStore read boundary, with all queries under one writer guard.
pub async fn subgraph(
    client: &Client,
    root: i64,
    depth: i64,
    to: Option<i64>,
) -> anyhow::Result<Value> {
    crate::txn::with_client(client, |client| async move {
        let graph = load_graph_body(client).await?;
        let registry = load_relations_body(client).await?;
        let edges: Vec<crate::graph_read::Edge> = serde_json::from_value(json!(
            graph["edges"]
                .as_array()
                .unwrap()
                .iter()
                .map(|e| json!({
                    "src": e["src_id"], "relation": e["relation"], "dst": e["dst_id"],
                    "confidence": e["confidence"], "origin": e["origin"],
                }))
                .collect::<Vec<_>>()
        ))?;
        let relations: std::collections::BTreeMap<String, crate::graph_read::Relation> =
            serde_json::from_value(Value::Object(
                registry
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|r| {
                        (
                            r["name"].as_str().unwrap().to_owned(),
                            json!({
                                "transitive": r["transitive"], "inverse_of": r["inverse_of"],
                            }),
                        )
                    })
                    .collect(),
            ))?;
        let mut result = serde_json::to_value(crate::graph_read::build_subgraph(
            &edges, &relations, root, depth, to,
        ))?;
        result["entities"] = Value::Object(
            graph["entities"]
                .as_array()
                .unwrap()
                .iter()
                .map(|e| (e["id"].as_i64().unwrap().to_string(), e.clone()))
                .collect(),
        );
        result["aliases"] = graph["aliases"].clone();
        Ok(result)
    })
    .await
}
