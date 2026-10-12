//! Executable store boundary for the differential harness, never installed.
#![allow(dead_code)]

#[path = "../src/graph_read.rs"]
mod graph_read;

#[path = "../src/mutants.rs"]
mod mutants;
#[path = "../src/storage/mod.rs"]
mod storage;
#[path = "../src/txn.rs"]
mod txn;

use std::io::{BufRead, Write};

#[tokio::main(flavor = "current_thread")]
async fn main() -> anyhow::Result<()> {
    let dsn = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL")?;
    let store = storage::Storage::open(&dsn)
        .await
        .map_err(|e| anyhow::anyhow!(e.to_string()))?;
    for line in std::io::stdin().lock().lines() {
        let request: serde_json::Value = serde_json::from_str(&line?)?;
        let value = if request["op"] == "resolve_relation" {
            let known: Vec<String> = serde_json::from_value(request["known"].clone())?;
            Ok(serde_json::to_value(graph_read::resolve_relation(
                &known,
                request["raw"].as_str().unwrap(),
            ))?)
        } else if request["op"] == "degree_counts" {
            let edges: Vec<graph_read::StoredEdge> =
                serde_json::from_value(request["edges"].clone())?;
            Ok(serde_json::to_value(graph_read::degree_counts(&edges))?)
        } else if request["op"] == "degrees_by_name" {
            let edges: Vec<graph_read::StoredEdge> =
                serde_json::from_value(request["edges"].clone())?;
            let entities: Vec<graph_read::NamedEntity> =
                serde_json::from_value(request["entities"].clone())?;
            Ok(serde_json::to_value(graph_read::degrees_by_name(
                &edges, &entities,
            ))?)
        } else if request["op"] == "shortest_path" {
            let edges: Vec<graph_read::StoredEdge> =
                serde_json::from_value(request["edges"].clone())?;
            Ok(serde_json::to_value(graph_read::shortest_path(
                &edges,
                request["src_id"].as_i64().unwrap(),
                request["dst_id"].as_i64().unwrap(),
                request["max_hops"].as_i64().unwrap_or(8),
            ))?)
        } else if request["op"] == "derive_edges" || request["op"] == "build_subgraph" {
            let edges: Vec<graph_read::Edge> = serde_json::from_value(request["edges"].clone())?;
            let relations = graph_read::registry_from_value(&request["relations"])?;
            if request["op"] == "derive_edges" {
                Ok(serde_json::to_value(graph_read::derive_edges(
                    &edges, &relations,
                ))?)
            } else {
                Ok(serde_json::to_value(graph_read::build_subgraph(
                    &edges,
                    &relations,
                    request["root"].as_i64().unwrap(),
                    request["depth"].as_i64().unwrap_or(1),
                    request["to"].as_i64(),
                ))?)
            }
        } else if request["op"] == "alias_canonical_map" {
            let entities: Vec<graph_read::Entity> =
                serde_json::from_value(request["entities"].clone())?;
            let aliases = serde_json::from_value(request["aliases"].clone())?;
            Ok(serde_json::to_value(graph_read::alias_canonical_map(
                &entities, &aliases,
            ))?)
        } else {
            storage::graph::dispatch(store.client(), &request).await
        };
        let result = match value {
            Ok(value) => serde_json::json!({"value": value}),
            Err(error) => match error
                .downcast_ref::<tokio_postgres::Error>()
                .and_then(tokio_postgres::Error::code)
            {
                Some(code) => serde_json::json!({"sqlstate": code.code()}),
                None => return Err(error),
            },
        };
        println!("{result}");
        std::io::stdout().flush()?;
    }
    store.close().await;
    Ok(())
}
