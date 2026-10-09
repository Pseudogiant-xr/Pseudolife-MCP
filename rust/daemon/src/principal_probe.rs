//! NDJSON driver for the principal-store differential harness. Compiled only
//! with `principal-harness`; credentials and pairing codes never reach output.

use std::io::{self, BufRead, Write};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::auth::{self, EnvTokens, PrincipalSource, PrincipalStore, RefreshRead, StoredPrincipal};
use crate::{principals, storage};

fn strings(v: &Value, key: &str) -> Vec<String> {
    v[key]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|s| s.as_str().map(str::to_owned))
        .collect()
}

fn text(v: &Value, key: &str) -> Option<String> {
    v[key].as_str().map(str::to_owned)
}

fn row(v: &Value) -> Result<StoredPrincipal, String> {
    Ok(StoredPrincipal {
        principal: text(v, "principal").ok_or("invalid row")?,
        token_hash: text(v, "token_hash"),
        tier: text(v, "tier"),
        board: v["board"].as_bool().unwrap_or(true),
        revoked: v["revoked"].as_bool().unwrap_or(false),
    })
}

fn rows(v: &Value) -> Result<Vec<StoredPrincipal>, String> {
    v["rows"]
        .as_array()
        .ok_or("invalid rows")?
        .iter()
        .map(row)
        .collect()
}

fn safe_row(row: &StoredPrincipal) -> Value {
    json!({"principal": row.principal, "token_hash": row.token_hash,
        "tier": row.tier, "board": row.board, "revoked": row.revoked})
}

fn started(v: &Value) -> Instant {
    Instant::now()
        .checked_sub(Duration::from_secs_f64(
            v["age_s"].as_f64().unwrap_or(0.0).clamp(0.0, 3600.0),
        ))
        .unwrap_or_else(Instant::now)
}

fn inspect(v: &Value, env: &EnvTokens, store: &PrincipalStore) -> Value {
    let allowed = strings(v, "allowed");
    let names: Vec<_> = strings(v, "names").iter().map(|name| json!({
        "principal": name, "has": store.has(name), "admitted": store.admitted(name),
        "tier": store.tier_of(name), "board_allowed": auth::principal_admitted(&allowed, name, store),
    })).collect();
    let resolved: Vec<_> = v["headers"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|header| {
            let (resolved, source) = auth::resolve_detailed(header.as_str(), env, store);
            let (status, principal) = match resolved {
                auth::Resolved::Principal(p) => ("principal", Some(p)),
                auth::Resolved::None => ("unauthorized", None),
                auth::Resolved::Unavailable => ("unavailable", None),
            };
            let source_name = match source {
                Some(PrincipalSource::Open) => Some("open"),
                Some(PrincipalSource::Environment) => Some("environment"),
                Some(PrincipalSource::Store) => Some("store"),
                None => None,
            };
            json!({"status": status, "principal": principal, "source": source_name,
            "config_allowed": auth::operator_allowed(source, "/api/config"),
            "notice_allowed": auth::operator_allowed(source, "/api/daemon-notice"),
            "stats_allowed": auth::operator_allowed(source, "/api/stats")})
        })
        .collect();
    let (shadowed, invalid) = store.skipped_rows();
    let mut excluded = store.excluded_names();
    excluded.sort();
    json!({"available": store.available(), "len": store.len(), "bank": store.bank(),
        "shadowed": shadowed, "invalid": invalid, "excluded": excluded,
        "names": names, "resolved": resolved})
}

async fn redeem_race(
    client: &mut tokio_postgres::Client,
    dsn: &str,
    v: &Value,
) -> Result<Value, String> {
    let name = text(v, "name").ok_or("name required")?;
    let code = text(v, "code_hash").ok_or("hash required")?;
    let hashes = strings(v, "token_hashes");
    if hashes.len() != 2 {
        return Err("two hashes required".into());
    }
    let config = storage::parse_dsn(dsn).map_err(|_| "invalid database")?;
    let mut clients = Vec::new();
    let mut pids = Vec::new();
    for _ in 0..2 {
        let (c, task) = storage::connect(&config, Duration::from_secs(10))
            .await
            .map_err(|_| "race connection failed")?;
        pids.push(
            c.query_one("SELECT pg_backend_pid()", &[])
                .await
                .map_err(|_| "race query failed")?
                .get::<_, i32>(0),
        );
        clients.push((c, task));
    }
    let holder = client.transaction().await.map_err(|_| "race lock failed")?;
    holder
        .query(
            "SELECT principal FROM principals WHERE principal=$1 FOR UPDATE",
            &[&name],
        )
        .await
        .map_err(|_| "race lock failed")?;
    let mut tasks = Vec::new();
    for ((mut c, connection), hash) in clients.into_iter().zip(hashes.clone()) {
        let code = code.clone();
        tasks.push(tokio::spawn(async move {
            let result = principals::redeem(&mut c, &code, &hash, &[])
                .await
                .map_err(|_| "race redemption failed".to_string());
            drop(c);
            let _ = connection.await;
            result
        }));
    }
    let deadline = Instant::now() + Duration::from_secs(4);
    let queued = loop {
        let count: i64 = holder.query_one(
            "SELECT count(*) FROM pg_stat_activity WHERE pid=ANY($1) AND wait_event_type='Lock'", &[&pids])
            .await.map_err(|_| "race observation failed")?.get(0);
        if count == 2 {
            break true;
        }
        if Instant::now() >= deadline {
            break false;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    };
    holder.commit().await.map_err(|_| "race unlock failed")?;
    let mut winners = Vec::new();
    for task in tasks {
        if let Some(row) = task.await.map_err(|_| "race task failed")?? {
            winners.push(row);
        }
    }
    let durable: String = client
        .query_one(
            "SELECT token_hash FROM principals WHERE principal=$1",
            &[&name],
        )
        .await
        .map_err(|_| "race durable query failed")?
        .get(0);
    let valid = winners.len() == 1
        && winners[0].principal == name
        && winners[0].token_hash.as_deref() == Some(&durable)
        && hashes.contains(&durable);
    Ok(json!({"both_queued": queued, "winners": winners.len(), "durable_winner_matches": valid}))
}

pub async fn run() -> Result<(), String> {
    let dsn = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL").map_err(|_| "database required")?;
    let (mut client, connection) = storage::connect(
        &storage::parse_dsn(&dsn).map_err(|_| "invalid database")?,
        Duration::from_secs(10),
    )
    .await
    .map_err(|_| "database connection failed")?;
    let mut env = EnvTokens {
        map: vec![],
        single: None,
    };
    let mut store = PrincipalStore::new_with_shadowed(vec![]);
    let mut pending: Option<RefreshRead> = None;
    let mut stdout = io::stdout().lock();
    for line in io::stdin().lock().lines() {
        let line = line.map_err(|_| "input read failed")?;
        let v: Value = serde_json::from_str(&line).map_err(|_| "invalid JSON input")?;
        let answer = match v["op"].as_str().unwrap_or("") {
            "reset" => {
                env = EnvTokens {
                    map: auth::parse_token_map(v["env_map"].as_str().unwrap_or("")),
                    single: text(&v, "single"),
                };
                let mut shadowed = strings(&v, "shadowed");
                shadowed.extend(env.map.iter().map(|(_, name)| name.clone()));
                store = PrincipalStore::new_with_shadowed(shadowed);
                pending = None;
                json!({"ok": true})
            }
            "refresh" => {
                let read = store.begin_refresh_at(started(&v));
                store.finish_refresh(read, rows(&v)?, text(&v, "bank"));
                json!({"ok": true})
            }
            "begin_refresh" => {
                pending = Some(store.begin_refresh_at(started(&v)));
                json!({"ok": true})
            }
            "finish_refresh" => {
                store.finish_refresh(
                    pending.take().ok_or("no pending refresh")?,
                    rows(&v)?,
                    text(&v, "bank"),
                );
                json!({"ok": true})
            }
            "add" => {
                store.add(row(&v["row"])?);
                json!({"ok": true})
            }
            "inspect" => inspect(&v, &env, &store),
            "load" => {
                let read = store.begin_refresh();
                match principals::load_rows(&client).await {
                    Ok((rows, bank)) => {
                        store.finish_refresh(read, rows, bank);
                        json!({"ok": true})
                    }
                    Err(_) => json!({"ok": false, "error": "database"}),
                }
            }
            "list" => match principals::list_principals(&client).await {
                Ok(rows) => json!({"rows": rows}),
                Err(_) => json!({"ok": false, "error": "database"}),
            },
            "revoke" => {
                match principals::revoke(&mut client, v["name"].as_str().ok_or("name required")?)
                    .await
                {
                    Ok(found) => json!({"found": found}),
                    Err(_) => json!({"ok": false, "error": "database"}),
                }
            }
            "redeem" => {
                let excluded = if v["excluded"].is_array() {
                    strings(&v, "excluded")
                } else {
                    store.excluded_names()
                };
                match principals::redeem(
                    &mut client,
                    v["code_hash"].as_str().ok_or("hash required")?,
                    v["token_hash"].as_str().ok_or("hash required")?,
                    &excluded,
                )
                .await
                {
                    Ok(row) => {
                        if v["add"].as_bool().unwrap_or(false)
                            && let Some(ref row) = row
                        {
                            store.add(row.clone());
                        }
                        json!({"row": row.as_ref().map(safe_row)})
                    }
                    Err(_) => json!({"ok": false, "error": "database"}),
                }
            }
            "redeem_race" => redeem_race(&mut client, &dsn, &v).await?,
            _ => return Err("unknown operation".into()),
        };
        serde_json::to_writer(&mut stdout, &answer).map_err(|_| "output failed")?;
        writeln!(&mut stdout).map_err(|_| "output failed")?;
        stdout.flush().map_err(|_| "output failed")?;
    }
    drop(client);
    connection.await.map_err(|_| "database task failed")?;
    Ok(())
}
