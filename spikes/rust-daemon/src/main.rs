//! Contract-first spike of the Pseudolife daemon read path: GET /health, the
//! bearer gate, read-only hydration of a schema-55 bank, ONNX query embedding
//! and GET /api/search. See spec.md for the contract this answers to.

mod auth;
mod bank;
mod embed;
mod search;

use anyhow::{Context, Result};
use axum::body::Body;
use axum::extract::State;
use axum::http::{header, HeaderMap, Method, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio_postgres::NoTls;

const VERSION: &str = concat!(env!("CARGO_PKG_VERSION"), "+spike");
const UNAUTHORIZED_HINT: &str = "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>";
const BROWSER_HINT: &str =
    "tokenless /api serves loopback browsers only; set PSEUDOLIFE_MCP_TOKEN for remote access";

/// Python's route table (web/routes.py), so a wrong verb on a known path is a
/// 405 there and here. Only /api/search is served by the spike.
const GET_PATHS: &[&str] = &[
    "/api/stats", "/api/overview", "/api/facts", "/api/facts/history", "/api/world", "/api/lessons",
    "/api/briefing", "/api/agents", "/api/episodes", "/api/episodes/summary", "/api/recent",
    "/api/search", "/api/trace", "/api/recall", "/api/chain", "/api/entry", "/api/sources",
    "/api/graph", "/api/graph/projects", "/api/graph/digest", "/api/graph/communities",
    "/api/graph/path", "/api/graph/review", "/api/graph/proposal-evidence", "/api/wiki",
    "/api/graph/entity-provenance", "/api/curation/duplicates", "/api/curation/retired",
    "/api/dream/status", "/api/consolidation", "/api/maintainer", "/api/maintainer/sent",
    "/api/maintainer/inbox", "/api/config",
];
const POST_PATHS: &[&str] = &[
    "/api/facts/resolve", "/api/facts/set", "/api/facts/forget", "/api/episode/start",
    "/api/episode/end", "/api/episodes/prune", "/api/episodes/rename", "/api/episodes/merge",
    "/api/reinforce", "/api/graph/rejudge", "/api/graph/assign-scope", "/api/graph/unrelate",
    "/api/graph/relate", "/api/graph/bless-edge", "/api/graph/dismiss-duplicate",
    "/api/curation/dismiss-duplicate", "/api/lessons/restore", "/api/world/restore",
    "/api/graph/delete-entity", "/api/graph/merge", "/api/graph/accept-proposal",
    "/api/graph/reject-proposal", "/api/graph/accept-entity-merge", "/api/graph/accept-entity-junk",
    "/api/graph/reject-entity-proposal", "/api/dream/run", "/api/consolidate", "/api/delete",
    "/api/supersede", "/api/daemon-notice", "/api/maintainer/challenge", "/api/maintainer/enrol",
    "/api/maintainer/send", "/api/maintainer/role", "/api/maintainer/cancel",
    "/api/maintainer/revoke", "/api/maintainer/repudiate", "/api/config",
];

struct App {
    db_url: String,
    env: auth::EnvTokens,
    store: auth::PrincipalStore,
    bank: bank::Bank,
    embedder: embed::Embedder,
}

fn json_response(status: StatusCode, body: &Value) -> Response {
    let bytes = serde_json::to_vec(body).expect("serializable");
    (
        status,
        [
            (header::CONTENT_TYPE, "application/json; charset=utf-8"),
            (header::CACHE_CONTROL, "no-store"),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        ],
        bytes,
    )
        .into_response()
}

async fn connect(url: &str) -> Result<tokio_postgres::Client> {
    let (client, conn) = tokio_postgres::connect(url, NoTls).await?;
    tokio::spawn(async move {
        let _ = conn.await;
    });
    bank::read_only(&client).await?;
    Ok(client)
}

async fn health(app: &App) -> Response {
    let mut body = json!({
        "status": "ok",
        "version": VERSION,
        "schema": bank::SCHEMA,
        "storage": "postgres",
        "auth": app.env.configured(),
        "bank": Value::Null,
        "persist_errors": 0,
        "memory": {"source": "unavailable"},
        "embedder": {"backend": "onnx", "device": "cpu", "dtype": "fp32"},
    });
    let probe = tokio::time::timeout(Duration::from_secs(2), async {
        let c = connect(&app.db_url).await?;
        c.simple_query("SELECT 1").await?;
        let row = c.query_opt("SELECT value::text FROM meta WHERE key = 'coordination_bank_id'", &[]).await?;
        Ok::<_, anyhow::Error>(row.map(|r| r.get::<_, String>(0)))
    })
    .await;
    match probe {
        Ok(Ok(bank_id)) => {
            body["db"] = json!("ok");
            if let Some(id) = bank_id {
                // meta values are JSON-encoded text; the hash is over the id string.
                let id = serde_json::from_str::<String>(&id).unwrap_or(id);
                body["bank"] = json!(&hex::encode(Sha256::digest(id.as_bytes()))[..16]);
            }
        }
        Ok(Err(e)) => {
            body["db"] = json!(format!("error: {e}"));
            body["status"] = json!("degraded");
        }
        Err(_) => {
            body["db"] = json!("error: timeout");
            body["status"] = json!("degraded");
        }
    }
    let status = if body["status"] == "ok" { StatusCode::OK } else { StatusCode::SERVICE_UNAVAILABLE };
    json_response(status, &body)
}

fn latin1_header(h: &HeaderMap, name: header::HeaderName) -> Option<String> {
    h.get(name).map(|v| v.as_bytes().iter().map(|&b| b as char).collect())
}

fn host_part(value: &str) -> String {
    let mut v = value;
    if let Some((_, rest)) = v.split_once("://") {
        v = rest.split(['/', '?', '#']).next().unwrap_or(rest);
        if let Some((_, h)) = v.rsplit_once('@') {
            v = h;
        }
    }
    let v = v.trim().to_lowercase();
    if let Some(rest) = v.strip_prefix('[') {
        return rest.split(']').next().unwrap_or("").to_string();
    }
    if v.matches(':').count() == 1 {
        return v.rsplit_once(':').map(|(h, _)| h.to_string()).unwrap_or(v);
    }
    v
}

fn browser_gate(app: &App, h: &HeaderMap) -> Option<&'static str> {
    if app.env.configured() {
        return None;
    }
    let loopback = |v: String| matches!(host_part(&v).as_str(), "127.0.0.1" | "::1" | "localhost");
    if let Some(o) = latin1_header(h, header::ORIGIN) {
        if !loopback(o) {
            return Some("forbidden_origin");
        }
    }
    if let Some(host) = latin1_header(h, header::HOST) {
        if !loopback(host) {
            return Some("forbidden_host");
        }
    }
    None
}

/// `parse_qsl(keep_blank_values=True)`, last value per key wins.
fn parse_query(raw: Option<&str>) -> std::collections::HashMap<String, String> {
    form_urlencoded::parse(raw.unwrap_or("").as_bytes()).into_owned().collect()
}

fn py_int(s: &str) -> Option<i64> {
    let t = s.trim().replace('_', "");
    t.parse::<i64>().ok()
}

fn py_float(s: &str) -> Option<f64> {
    let t = s.trim().replace('_', "");
    t.parse::<f64>().ok()
}

/// `_tribool` (web/routes.py:55): None follows config.
fn tribool(q: &std::collections::HashMap<String, String>, key: &str) -> Option<bool> {
    let v = q.get(key)?;
    if matches!(v.as_str(), "" | "null" | "auto") {
        return None;
    }
    Some(matches!(v.trim().to_lowercase().as_str(), "1" | "true" | "yes" | "on"))
}

fn list(q: &std::collections::HashMap<String, String>, key: &str) -> Option<Vec<String>> {
    let items: Vec<String> = q
        .get(key)?
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();
    (!items.is_empty()).then_some(items)
}

fn search_route(app: &App, raw_query: Option<&str>) -> Response {
    let q = parse_query(raw_query);
    let query = q.get("q").map(|s| s.trim().to_string()).unwrap_or_default();
    if query.is_empty() {
        return json_response(
            StatusCode::OK,
            &json!({"entries": [], "query": "", "count": 0, "low_confidence": true}),
        );
    }
    if let Some(bands) = list(&q, "band") {
        let unknown: Vec<&String> = bands.iter().filter(|b| !search::BANDS.contains(&b.as_str())).collect();
        if !unknown.is_empty() {
            let msg = format!("unknown band name(s) {unknown:?} — this preset has {:?}", search::BANDS);
            return json_response(StatusCode::BAD_REQUEST, &json!({"error": msg}));
        }
    }
    let top_k = q.get("top_k").filter(|s| !s.is_empty()).and_then(|s| py_int(s)).unwrap_or(search::ROUTE_TOP_K);
    let k = match top_k {
        0 => search::DEFAULT_TOP_K,
        n if n < 0 => {
            return json_response(StatusCode::INTERNAL_SERVER_ERROR, &json!({"error": "top_k must not be negative"}))
        }
        n => n as usize,
    };
    let params = search::Params {
        query: query.clone(),
        k,
        sources: list(&q, "source").map(|v| v.into_iter().collect::<HashSet<_>>()),
        tags: list(&q, "tag").map(|v| v.into_iter().map(|t| t.to_lowercase()).collect::<HashSet<_>>()),
        min_score: q.get("min_score").filter(|s| !s.is_empty()).and_then(|s| py_float(s)),
        bm25: tribool(&q, "bm25").unwrap_or(true),
    };
    let qv = match app.embedder.embed_query(&query) {
        Ok(v) => v,
        Err(e) => return json_response(StatusCode::INTERNAL_SERVER_ERROR, &json!({"error": e.to_string()})),
    };
    let hits = search::rank(&app.bank, &qv, &params);
    let entries: Vec<Value> =
        hits.iter().map(|h| search::entry_json(&app.bank, &app.bank.entries[h.idx], h.score)).collect();
    json_response(
        StatusCode::OK,
        &json!({"query": query, "count": entries.len(), "low_confidence": entries.is_empty(), "entries": entries}),
    )
}

async fn handle(State(app): State<Arc<App>>, req: Request<Body>) -> Response {
    let path = req.uri().path().to_string();
    let method = req.method().clone();
    let headers = req.headers().clone();
    if path == "/health" {
        return health(&app).await;
    }
    let is_api = path.starts_with("/api/") || path == "/api";
    if is_api {
        if let Some(denied) = browser_gate(&app, &headers) {
            return json_response(StatusCode::FORBIDDEN, &json!({"error": denied, "hint": BROWSER_HINT}));
        }
    }
    let authz = latin1_header(&headers, header::AUTHORIZATION);
    match auth::resolve(authz.as_deref(), &app.env, &app.store) {
        auth::Resolved::Unavailable => {
            return json_response(StatusCode::SERVICE_UNAVAILABLE, &json!({"error": "principals_unavailable"}))
        }
        auth::Resolved::None => {
            return json_response(StatusCode::UNAUTHORIZED, &json!({"error": "unauthorized", "hint": UNAUTHORIZED_HINT}))
        }
        auth::Resolved::Principal(_) => {}
    }
    if !is_api {
        // MCP, the Console and the other non-/api surfaces are outside the spike.
        return json_response(StatusCode::NOT_FOUND, &json!({"error": "not_found", "path": path}));
    }
    if method != Method::GET && method != Method::POST {
        return json_response(StatusCode::METHOD_NOT_ALLOWED, &json!({"error": "method_not_allowed"}));
    }
    if method == Method::GET && path == "/api/search" {
        let raw = req.uri().query().map(String::from);
        let app2 = app.clone();
        return tokio::task::spawn_blocking(move || search_route(&app2, raw.as_deref()))
            .await
            .unwrap_or_else(|e| json_response(StatusCode::INTERNAL_SERVER_ERROR, &json!({"error": e.to_string()})));
    }
    let table = if method == Method::GET { GET_PATHS } else { POST_PATHS };
    let known = GET_PATHS.contains(&path.as_str()) || POST_PATHS.contains(&path.as_str());
    if known && !table.contains(&path.as_str()) {
        return json_response(StatusCode::METHOD_NOT_ALLOWED, &json!({"error": "method_not_allowed", "path": path}));
    }
    // Known routes the spike does not serve answer 404 too (a listed divergence).
    json_response(StatusCode::NOT_FOUND, &json!({"error": "not_found", "path": path}))
}

async fn refresh_principals(app: Arc<App>) {
    loop {
        let rows = async {
            let c = connect(&app.db_url).await?;
            let rows = c
                .query(
                    "SELECT principal, token_hash, revoked_at IS NOT NULL FROM public.principals",
                    &[],
                )
                .await?;
            Ok::<_, anyhow::Error>(
                rows.into_iter().map(|r| (r.get::<_, String>(0), r.get(1), r.get::<_, bool>(2))).collect(),
            )
        }
        .await;
        match rows {
            Ok(rows) => app.store.load(rows, &app.env),
            Err(e) => eprintln!("principals refresh failed: {e}"),
        }
        tokio::time::sleep(auth::REFRESH_EVERY).await;
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let db_url = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL").context("PSEUDOLIFE_MCP_DATABASE_URL is required")?;
    let model_dir = PathBuf::from(std::env::var("PSEUDOLIFE_SPIKE_MODEL_DIR").context("PSEUDOLIFE_SPIKE_MODEL_DIR is required")?);
    let host = std::env::var("PSEUDOLIFE_MCP_HOST").unwrap_or_else(|_| "127.0.0.1".into());
    let port: u16 = std::env::var("PSEUDOLIFE_MCP_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(8765);
    let threads: usize = std::env::var("PSEUDOLIFE_SPIKE_THREADS").ok().and_then(|p| p.parse().ok()).unwrap_or(4);
    let env = auth::EnvTokens::from_env();
    if !env.configured() && !matches!(host.as_str(), "127.0.0.1" | "::1" | "localhost") {
        anyhow::bail!("refusing a non-loopback bind without PSEUDOLIFE_MCP_TOKEN");
    }

    let t0 = std::time::Instant::now();
    let client = connect(&db_url).await.context("connecting to the bank")?;
    bank::check_schema(&client).await?;
    let bank = bank::hydrate(&client).await?;
    eprintln!("hydrated {} entries in {:.2?}", bank.entries.len(), t0.elapsed());
    let t1 = std::time::Instant::now();
    let embedder = embed::Embedder::load(&model_dir, threads)?;
    embedder.embed_query("warmup")?;
    eprintln!("embedder ready in {:.2?}", t1.elapsed());

    let app = Arc::new(App { db_url, env, store: auth::PrincipalStore::new(), bank, embedder });
    tokio::spawn(refresh_principals(app.clone()));
    let router = axum::Router::new().fallback(handle).with_state(app);
    let listener = tokio::net::TcpListener::bind((host.as_str(), port)).await?;
    eprintln!("listening on {host}:{port}");
    axum::serve(listener, router).await?;
    Ok(())
}
