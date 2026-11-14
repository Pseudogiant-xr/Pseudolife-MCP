//! What a Docker bank on this host is, read without changing anything.
//!
//! The same code reads the source (this host) and the target (inside the
//! helper the source runs there over ssh). Every Docker command it runs is
//! `inspect`, a psql query under `default_transaction_read_only=on` that is
//! a `SELECT` or a `SHOW`, or `du -sk /data` in the daemon container; the
//! tests hold it to that list. Nothing secret leaves it: no environment
//! value, no connection string, only names, counts and verdicts.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::super::{
    bank::{self, Bank, Selectors, Stop},
    conn::truthy,
    docker::{Docker, Ran},
};

/// Every psql query runs read-only.
pub(super) const READ_ONLY: &str = "PGOPTIONS=-c default_transaction_read_only=on";
const QUERY_TIME: std::time::Duration = std::time::Duration::from_secs(120);

/// The meta keys a fresh bank seeds itself; any other key is user state.
pub(super) const EMPTY_TARGET_META_KEYS: [&str; 8] = [
    "schema_version",
    "coordination_bank_id",
    "maintainer_secret_v1",
    "dream_ack_secret_v1",
    "flat_band_migration_v1",
    "curation_listing_spelling_v2",
    "writer_lease_epoch",
    "coordination_hlc_highwater",
];

/// The bank on one host, as move needs it.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(in crate::cli) struct Facts {
    pub daemon: String,
    pub pg: String,
    pub image: String,
    pub running: bool,
    pub restart_policy: String,
    /// `com.docker.compose.project` of the daemon and of PostgreSQL.
    pub compose_project: String,
    pub pg_compose_project: String,
    /// `com.docker.compose.project.working_dir` and `.config_files`.
    pub compose_dir: String,
    pub compose_files: String,
    /// The daemon's published health endpoint (`""` when none).
    pub health_url: String,
    pub status: String,
    pub ready: Option<bool>,
    pub auth: Option<bool>,
    pub schema: Option<i64>,
    /// The health answer's bank fingerprint.
    pub bank: Option<String>,
    pub version: String,
    /// The fingerprint of `meta.coordination_bank_id`.
    pub meta_fingerprint: Option<String>,
    pub pg_version: Option<i64>,
    pub db: String,
    pub user: String,
    pub port: u16,
    pub db_bytes: Option<u64>,
    pub data_kb: Option<u64>,
    /// Host sources of the daemon's `/data` mounts and of PostgreSQL's mounts.
    pub state_sources: Vec<String>,
    pub pg_sources: Vec<String>,
}

/// A shared check's refusal in move's words: without restore's ending,
/// and without restore's selector options, which move does not take (it
/// reads the bank the installer's containers run).
pub(super) fn text(Stop(message, _): Stop) -> String {
    let message = message
        .trim_end_matches(". Nothing was restored")
        .trim_end_matches(" Nothing was restored")
        .replace(
            " or name its containers with --container <postgres> --daemon-container <daemon>,",
            ",",
        )
        .replace(
            ", or name the actual bank with --container <postgres> --db <database> --user <role> --db-port <port>",
            ", then retry",
        )
        .replace("Docker inspect failed for the backup/restore", "docker inspect failed")
        .replace(" before backing up or restoring", ", then retry");
    if message.contains("--db-port") || message.contains("name its literal database with --db") {
        return "the daemon's database connection (PSEUDOLIFE_MCP_DATABASE_URL in its configuration) names no single database, role and port; set it to the bank's direct PostgreSQL URL, then retry".to_owned();
    }
    message
}

/// A failed command's reason: its fault, else its last stderr line.
pub(super) fn why(ran: &Ran) -> String {
    if let Some(fault) = &ran.fault {
        return fault.clone();
    }
    String::from_utf8_lossy(&ran.stderr)
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map_or_else(|| format!("it exited {}", ran.code), str::to_owned)
}

/// The argv of a read-only psql query in `pg`.
pub(super) fn query_argv<D: Docker>(
    bank: &Bank<'_, D>,
    user: &str,
    port: u16,
    database: &str,
    sql: &str,
) -> Vec<String> {
    let mut argv = bank.psql_argv(user, port, database, &["-At", "-c", sql]);
    argv.insert(1, "-e".to_owned());
    argv.insert(2, READ_ONLY.to_owned());
    argv
}

/// One read-only query's output, trimmed.
pub(super) fn query<D: Docker>(
    bank: &mut Bank<'_, D>,
    user: &str,
    port: u16,
    database: &str,
    sql: &str,
) -> Result<String, String> {
    let argv = query_argv(bank, user, port, database, sql);
    let ran = bank.docker.run(&argv, QUERY_TIME);
    if !ran.ok() {
        return Err(why(&ran));
    }
    Ok(String::from_utf8_lossy(&ran.stdout).trim().to_owned())
}

/// `bank_fingerprint`: the first 16 hex of the SHA-256 of a non-empty
/// bank id.
pub(super) fn fingerprint(bank_id: &str) -> Option<String> {
    if bank_id.is_empty() {
        return None;
    }
    let digest = Sha256::digest(bank_id.as_bytes());
    Some(digest.iter().take(8).map(|b| format!("{b:02x}")).collect())
}

fn label(doc: &Value, key: &str) -> String {
    bank::get(doc, "Config")
        .and_then(|c| bank::get(c, "Labels"))
        .and_then(|l| bank::get(l, key))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn sources(doc: &Value, keep: impl Fn(&str) -> bool) -> Vec<String> {
    bank::get(doc, "Mounts")
        .and_then(Value::as_array)
        .map(|mounts| {
            mounts
                .iter()
                .filter(|m| {
                    bank::get(m, "Destination")
                        .and_then(Value::as_str)
                        .is_some_and(&keep)
                })
                .filter_map(|m| bank::get(m, "Source").and_then(Value::as_str))
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

/// Fill the health fields from the daemon's `/health` answer.
pub(super) fn apply_health(facts: &mut Facts, answer: &Value) {
    let text = |key: &str| answer.get(key).and_then(Value::as_str).map(str::to_owned);
    facts.status = text("status").unwrap_or_default();
    facts.ready = answer.get("ready").and_then(Value::as_bool);
    facts.auth = answer.get("auth").and_then(Value::as_bool);
    facts.schema = answer.get("schema").and_then(Value::as_i64);
    facts.bank = text("bank");
    facts.version = text("version").unwrap_or_default();
}

/// Read the bank under the installer's container names on this host; the
/// caller reads `/health` at `health_url` and applies it.
pub(super) fn gather<D: Docker>(docker: &mut D) -> Result<Facts, String> {
    let mut bank = Bank {
        docker,
        pg: bank::PG_CONTAINER.to_owned(),
        daemon: bank::DAEMON_CONTAINER.to_owned(),
    };
    // Whether this user can use Docker at all, with Docker's own reason when
    // not (a socket it may not open, an engine that is down): the shared
    // inspect check names only its own causes.
    let engine = bank.run(&["version", "--format", "{{.Server.Version}}"], QUERY_TIME);
    if !engine.ok() {
        return Err(format!(
            "Docker cannot be used here ({}); run move as a user who can run docker (or set PSEUDOLIFE_DOCKER to a docker that works), then retry",
            why(&engine)
        ));
    }
    let chosen = bank::select(&mut bank, &Selectors::default()).map_err(text)?;
    let pg_doc = bank.inspect(&chosen.pg).map_err(text)?;
    let state = bank::get(&chosen.running, "State");
    let running = state
        .and_then(|s| bank::get(s, "Running"))
        .is_some_and(truthy);
    let restart_policy = bank::get(&chosen.running, "HostConfig")
        .and_then(|h| bank::get(h, "RestartPolicy"))
        .and_then(|r| bank::get(r, "Name"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    let published = bank::daemon_url(&chosen.running, None, &chosen.daemon).map_err(text)?;
    let health_url = if published.is_empty() {
        host_network_url(&chosen.running)
    } else {
        published
    };
    let mut facts = Facts {
        daemon: chosen.daemon.clone(),
        pg: chosen.pg.clone(),
        // The image as it was named (`Config.Image`); the inspect `Image`
        // restore uses is its id.
        image: bank::get(&chosen.running, "Config")
            .and_then(|c| bank::get(c, "Image"))
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
            .map_or_else(|| chosen.image.clone(), str::to_owned),
        running,
        restart_policy,
        compose_project: label(&chosen.running, "com.docker.compose.project"),
        pg_compose_project: label(&pg_doc, "com.docker.compose.project"),
        compose_dir: label(&chosen.running, "com.docker.compose.project.working_dir"),
        compose_files: label(&chosen.running, "com.docker.compose.project.config_files"),
        health_url: health_url.clone(),
        db: chosen.db.clone(),
        user: chosen.user.clone(),
        port: chosen.port,
        state_sources: sources(&chosen.running, |d| d == "/data" || d.starts_with("/data/")),
        pg_sources: sources(&pg_doc, |_| true),
        ..Facts::default()
    };
    let (user, port, db) = (chosen.user.clone(), chosen.port, chosen.db.clone());
    let meta = query(
        &mut bank,
        &user,
        port,
        &db,
        "SELECT value FROM meta WHERE key = 'coordination_bank_id'",
    )
    .map_err(|why| {
        format!(
            "cannot read the bank's identity from {} ({why}); check docker logs {}, then retry",
            chosen.pg, chosen.pg
        )
    })?;
    facts.meta_fingerprint = serde_json::from_str::<Value>(&meta)
        .ok()
        .and_then(|v| v.as_str().and_then(fingerprint));
    facts.pg_version = query(
        &mut bank,
        &user,
        port,
        "postgres",
        "SHOW server_version_num",
    )
    .ok()
    .and_then(|v| v.parse().ok());
    facts.db_bytes = query(
        &mut bank,
        &user,
        port,
        &db,
        "SELECT pg_database_size(current_database())",
    )
    .ok()
    .and_then(|v| v.parse().ok());
    if running {
        let ran = bank.run(&["exec", &chosen.daemon, "du", "-sk", "/data"], QUERY_TIME);
        if ran.ok() {
            facts.data_kb = String::from_utf8_lossy(&ran.stdout)
                .split_whitespace()
                .next()
                .and_then(|kb| kb.parse().ok());
        }
    }
    Ok(facts)
}

/// A daemon on the host network publishes no port: its endpoint is the
/// address it binds, `PSEUDOLIFE_MCP_HOST` and `PSEUDOLIFE_MCP_PORT` from its
/// environment (the daemon's defaults 127.0.0.1 and 8765), a wildcard read
/// as loopback. `""` for any other network mode or a value it cannot read.
pub(super) fn host_network_url(running: &Value) -> String {
    let mode = bank::get(running, "HostConfig")
        .and_then(|h| bank::get(h, "NetworkMode"))
        .and_then(Value::as_str);
    if mode != Some("host") {
        return String::new();
    }
    let env: Vec<&str> = bank::get(running, "Config")
        .and_then(|c| bank::get(c, "Env"))
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    let setting = |key: &str| {
        env.iter()
            .rev()
            .find_map(|item| item.strip_prefix(key)?.strip_prefix('='))
            .map(str::trim)
            .filter(|v| !v.is_empty())
    };
    let port = match setting("PSEUDOLIFE_MCP_PORT") {
        None => 8765,
        Some(raw) => match raw.parse::<u16>() {
            Ok(port) if port > 0 && raw.bytes().all(|b| b.is_ascii_digit()) => port,
            _ => return String::new(),
        },
    };
    let host = match setting("PSEUDOLIFE_MCP_HOST").unwrap_or("127.0.0.1") {
        "0.0.0.0" | "localhost" => "127.0.0.1".to_owned(),
        "::" | "::1" => "[::1]".to_owned(),
        host if host.parse::<std::net::Ipv4Addr>().is_ok() => host.to_owned(),
        host if host.parse::<std::net::Ipv6Addr>().is_ok() => format!("[{host}]"),
        _ => return String::new(),
    };
    format!("http://{host}:{port}")
}

fn literal(text: &str) -> String {
    format!("'{}'", text.replace('\'', "''"))
}

/// The tables of the bank `facts` names that hold user data, with their
/// counts: Python's move rule (fresh-bank seed rows excluded), plus the
/// daemon's own warm-up search row, which Python's rule counts; an
/// unreadable count is -1 and counts as used.
pub(super) fn used_tables<D: Docker>(
    docker: &mut D,
    facts: &Facts,
) -> Result<Vec<(String, i64)>, String> {
    let mut bank = Bank {
        docker,
        pg: facts.pg.clone(),
        daemon: facts.daemon.clone(),
    };
    let listed = query(
        &mut bank,
        &facts.user,
        facts.port,
        &facts.db,
        "SELECT coalesce(json_agg(relname ORDER BY relname), '[]'::json) FROM pg_class WHERE relkind IN ('r', 'p') AND relnamespace = 'public'::regnamespace",
    )
    .map_err(|why| format!("cannot read the target bank's tables ({why}); check the target's PostgreSQL logs, then retry"))?;
    let tables: Vec<String> = serde_json::from_str::<Vec<String>>(&listed)
        .ok()
        .filter(|t| t.iter().all(|name| !name.is_empty()))
        .ok_or("cannot enumerate the target bank's tables; check the target's PostgreSQL logs, then retry")?;
    if tables.is_empty() {
        return Ok(Vec::new());
    }
    let meta_filter = format!(
        " WHERE key NOT IN ({}) AND right(key, 15) <> '_schema_version'",
        EMPTY_TARGET_META_KEYS
            .iter()
            .map(|k| literal(k))
            .collect::<Vec<_>>()
            .join(", ")
    );
    let filter = |table: &str| -> String {
        match table {
            "meta" => meta_filter.clone(),
            "relations" => " WHERE NOT builtin".to_owned(),
            "maintainer_bootstrap" => " WHERE used_at IS NOT NULL OR credential_id IS NOT NULL".to_owned(),
            // Every daemon start runs its own warm-up search,
            // search("warmup probe", top_k=1), outside any session: one row
            // per start that no user made (service.py warm-up, Rust
            // search_route S17). A user's search, even with those words, has
            // a session or served something.
            "retrieval_events" => " WHERE NOT (query_text = 'warmup probe' AND session_id IS NULL AND episode_id IS NULL AND served = '[]'::jsonb)".to_owned(),
            "cms_counters" => " WHERE interaction_count <> 0 OR logical_turn_count <> 0 OR tier_queries <> 0 OR EXISTS (SELECT 1 FROM jsonb_each(tier_hits) WHERE value <> '0'::jsonb) OR EXISTS (SELECT 1 FROM jsonb_each(surprise_history) WHERE value <> '[]'::jsonb) OR consolidation_events <> '[]'::jsonb".to_owned(),
            _ => String::new(),
        }
    };
    let sql = format!(
        "SELECT {}",
        tables
            .iter()
            .map(|t| format!(
                "(SELECT count(*) FROM public.\"{}\"{})",
                t.replace('"', "\"\""),
                filter(t)
            ))
            .collect::<Vec<_>>()
            .join(", ")
    );
    let row = query(&mut bank, &facts.user, facts.port, &facts.db, &sql)
        .map_err(|why| format!("cannot count the target bank's rows ({why}); check the target's PostgreSQL logs, then retry"))?;
    let counts: Vec<&str> = row.split('|').collect();
    if counts.len() != tables.len() {
        return Err("cannot count the target bank's rows (the answer has the wrong shape); check the target's PostgreSQL logs, then retry".to_owned());
    }
    Ok(tables
        .into_iter()
        .zip(counts)
        .map(|(table, n)| (table, n.trim().parse::<i64>().unwrap_or(-1)))
        .filter(|(_, n)| *n != 0)
        .collect())
}
