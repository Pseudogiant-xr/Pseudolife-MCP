//! Executable store boundary for the differential harness, never installed.
#![allow(dead_code)]

#[path = "../src/mutants.rs"]
mod mutants;
#[path = "../src/storage/mod.rs"]
mod storage;

use std::io::{BufRead, Write};

#[tokio::main(flavor = "current_thread")]
async fn main() -> anyhow::Result<()> {
    let dsn = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL")?;
    let mut store = storage::Storage::open(&dsn)
        .await
        .map_err(|e| anyhow::anyhow!(e.to_string()))?;
    for line in std::io::stdin().lock().lines() {
        let request: serde_json::Value = serde_json::from_str(&line?)?;
        let result = match storage::graph::dispatch(store.client_mut(), &request).await {
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
