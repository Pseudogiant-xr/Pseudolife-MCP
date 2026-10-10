//! Feature-gated process adapter for the dream differential harness.
#[path = "../dream/mod.rs"]
mod dream;
#[path = "../graph_read.rs"]
mod graph_read;
#[allow(dead_code)]
#[path = "../mutants.rs"]
mod mutants;
#[allow(dead_code)]
#[path = "../pyjson.rs"]
mod pyjson;
#[path = "../storage/mod.rs"]
mod storage;
#[path = "../txn.rs"]
mod txn;

use serde_json::{Value, json};
use std::io::{BufRead, Write};
use std::sync::Arc;

fn fixture_database(config: &tokio_postgres::Config) -> bool {
    let name = config.get_dbname().unwrap_or("");
    name.starts_with("pl_cf_w3d_")
        && name.len() > 10
        && name[10..]
            .bytes()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_')
        && matches!(config.get_hosts(), [tokio_postgres::config::Host::Tcp(host)] if host == "127.0.0.1")
        && matches!(config.get_ports(), [port] if *port > 0)
}

fn main() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .unwrap();
    runtime.block_on(async {
        let dsn = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL").expect("fixture DSN required");
        let config = storage::parse_dsn(&dsn).expect("fixture DSN parse");
        assert!(
            fixture_database(&config),
            "refusing non-disposable or non-loopback dream fixture bank"
        );
        let storage = Arc::new(
            storage::Storage::open(&dsn)
                .await
                .expect("fixture storage open"),
        );
        let engine = dream::cursors::Cursors::new(storage.clone());
        for line in std::io::stdin().lock().lines() {
            let input: Value = serde_json::from_str(&line.unwrap()).expect("fixture JSON");
            if input["op"] == "exit" {
                break;
            }
            let output = if input["op"] == "panic" {
                panic_writer(&storage).await
            } else if input["op"] == "writer-state" {
                let row = storage
                    .client()
                    .query_one("SELECT pg_current_xact_id_if_assigned() IS NULL", &[])
                    .await
                    .unwrap();
                json!({"idle":row.get::<_,bool>(0)})
            } else if input["op"] == "cancel" {
                cancel_waiter(&engine, &storage, &dsn, &input).await
            } else if input["op"] == "issue" || input["op"] == "verify" {
                token_action(&input)
            } else {
                engine.execute(input).await
            };
            println!("{}", pyjson::dumps(&output));
            std::io::stdout().flush().unwrap();
        }
        drop(engine);
        Arc::try_unwrap(storage)
            .ok()
            .expect("fixture ownership")
            .close()
            .await;
    });
}

/// A harness-only panic abandons a real write; the next ordinary cursor read
/// must recover it through the shared seam before loading resident entries.
async fn panic_writer(writer: &Arc<storage::Storage>) -> Value {
    let writer = writer.clone();
    let task = tokio::spawn(async move {
        txn::run(writer.client(), |client| async move {
            client
                .batch_execute(
                    "UPDATE entries SET dream_state='acknowledged' WHERE dream_state='pending'",
                )
                .await?;
            panic!("injected cursor writer panic");
            #[allow(unreachable_code)]
            Ok::<(), tokio_postgres::Error>(())
        })
        .await
    });
    json!({"panicked": task.await.is_err_and(|error| error.is_panic())})
}

/// Test cancellation only after the real writer has entered its UPDATE and
/// is blocked in a fixture trigger; no clock guess stands in for admission.
async fn cancel_waiter(
    engine: &Arc<dream::cursors::Cursors>,
    writer: &storage::Storage,
    dsn: &str,
    input: &Value,
) -> Value {
    let pid: i32 = writer
        .client()
        .query_one("SELECT pg_backend_pid()", &[])
        .await
        .unwrap()
        .get(0);
    let config = storage::parse_dsn(dsn).unwrap();
    let (observer, connection) = storage::connect(&config, std::time::Duration::from_secs(10))
        .await
        .unwrap();
    observer
        .batch_execute(
            "SELECT pg_advisory_lock(63338); \
         CREATE FUNCTION dream_cancel_fixture() RETURNS trigger LANGUAGE plpgsql AS \
         $$ BEGIN PERFORM pg_advisory_xact_lock(63338); RETURN NEW; END $$; \
         CREATE TRIGGER dream_cancel_fixture BEFORE UPDATE ON entries \
         FOR EACH ROW EXECUTE FUNCTION dream_cancel_fixture()",
        )
        .await
        .unwrap();
    let owned = engine.clone();
    let action = input["action"].clone();
    let waiter = tokio::spawn(async move { owned.execute(action).await });
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(10);
    let mut admitted = false;
    while tokio::time::Instant::now() < deadline {
        admitted = observer.query_one(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND locktype='advisory' AND NOT granted AND objid=63338)",
            &[&pid],
        ).await.unwrap().get(0);
        if admitted {
            break;
        }
        if waiter.is_finished() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    }
    if admitted {
        waiter.abort();
    }
    observer
        .batch_execute("SELECT pg_advisory_unlock(63338)")
        .await
        .unwrap();
    let cancelled = waiter.await.is_err_and(|e| e.is_cancelled());
    // This next operation must wait for the owned transaction to finish and
    // then succeed; dropped ownership must not undo the admitted commit.
    let next = engine.execute(json!({"op":"pull"})).await;
    observer
        .batch_execute(
            "DROP TRIGGER dream_cancel_fixture ON entries; DROP FUNCTION dream_cancel_fixture()",
        )
        .await
        .unwrap();
    drop(observer);
    let _ = connection.await;
    json!({"admitted":admitted,"cancelled":cancelled,"next":next})
}

fn token_action(input: &Value) -> Value {
    let issuing = input["op"] == "issue";
    let bad = if issuing {
        "invalid_dream_commit_payload"
    } else {
        "invalid_dream_commit_token"
    };
    let result = (|| -> Result<Value, String> {
        if input["backend"] != "postgres" {
            return Err(bad.into());
        }
        let secret = input["secret"].as_str().ok_or(bad)?;
        let generation = input["generation"].as_str().ok_or(bad)?;
        if issuing {
            let ids: Vec<i64> = input["entry_ids"]
                .as_array()
                .ok_or(bad)?
                .iter()
                .map(|i| i.as_i64().ok_or(bad))
                .collect::<Result<_, _>>()?;
            let timestamp = input["display_timestamp"].as_f64().ok_or(bad)?;
            Ok(json!({"token":dream::token::issue(secret,generation,&ids,timestamp)?}))
        } else {
            let token = input["token"].as_str().ok_or(bad)?;
            let payload = dream::token::verify(token, secret, generation)?;
            Ok(json!({"entry_ids":payload.entry_ids,"display_timestamp":payload.display_timestamp}))
        }
    })();
    result.unwrap_or_else(|e| json!({"error":e}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixture_refuses_remote_or_unnamed_banks_before_connect() {
        for (dsn, allowed) in [
            ("postgresql://u:p@127.0.0.1:5433/pl_cf_w3d_test", true),
            ("postgresql://u:p@127.0.0.1:5544/pl_cf_w3d_ci", true),
            ("postgresql://u:p@127.0.0.1:5433/bank", false),
            ("postgresql://u:p@example.com:5433/pl_cf_w3d_test", false),
        ] {
            assert_eq!(fixture_database(&storage::parse_dsn(dsn).unwrap()), allowed);
        }
    }
}
