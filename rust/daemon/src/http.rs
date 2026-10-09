//! The HTTP surface: route order and gates of `web/api.py:404-886` (spec
//! section R), `/health` (section H) and the one Console route this crate
//! serves so far, `GET /api/search` (`spec.md`).

use crate::auth::{self, Resolved};
use crate::routes::RouteTable;
use crate::service::Service;
use crate::{health, search, static_files};
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue, Request, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet, VecDeque};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const UNAUTHORIZED_HINT: &str = "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>";
const BROWSER_HINT: &str =
    "tokenless /api serves loopback browsers only; set PSEUDOLIFE_MCP_TOKEN for remote access";
const CONTROL_BODY_LIMIT: usize = 256 * 1024;
const TEXT_BODY_LIMIT: usize = 4 * 1024 * 1024;
const COORDINATION_BODY_LIMIT: usize = 32 * 1024;
const SESSION_END_BODY_LIMIT: usize = 16 * 1024;
const PAIR_BODY_LIMIT: usize = 1024;
const TEXT_BODY_PATHS: [&str; 3] = ["/api/facts/set", "/api/consolidate", "/api/supersede"];
const OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", "/api/daemon-notice"];
/// `principal_store.FAILED_REDEMPTIONS_PER_MINUTE`, `REDEMPTION_SLOTS`.
const PAIR_FAILURES_PER_WINDOW: usize = 20;
const PAIR_WINDOW: Duration = Duration::from_secs(60);
const PAIRING_CODE_ALPHABET: &str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

pub struct App {
    pub service: Arc<Service>,
    pub env_tokens: auth::EnvTokens,
    pub store: Arc<auth::PrincipalStore>,
    pub static_dir: Option<PathBuf>,
    pair_failures: Mutex<VecDeque<Instant>>,
}

impl App {
    pub fn new(
        service: Arc<Service>,
        env_tokens: auth::EnvTokens,
        store: Arc<auth::PrincipalStore>,
        static_dir: Option<PathBuf>,
    ) -> App {
        App {
            service,
            env_tokens,
            store,
            static_dir,
            pair_failures: Mutex::new(VecDeque::new()),
        }
    }

    fn auth_configured(&self) -> bool {
        self.env_tokens.configured()
    }

    /// `FailureLimiter.reserve`: count one attempt, or None when spent.
    fn pair_reserve(&self) -> Option<Instant> {
        let mut q = self.pair_failures.lock().expect("pair limiter");
        let now = Instant::now();
        while q
            .front()
            .is_some_and(|t| now.duration_since(*t) >= PAIR_WINDOW)
        {
            q.pop_front();
        }
        if q.len() >= PAIR_FAILURES_PER_WINDOW {
            return None;
        }
        q.push_back(now);
        Some(now)
    }
}

pub fn json_response(status: u16, body: &Value) -> Response {
    // `json.dumps(payload, default=str)`, byte for byte (pyjson.rs).
    let bytes = if crate::mutants::active("serde-json-writer") {
        serde_json::to_vec(body).expect("serializable")
    } else {
        crate::pyjson::dumps(body).into_bytes()
    };
    (
        StatusCode::from_u16(status).expect("valid status"),
        [
            (header::CONTENT_TYPE, "application/json; charset=utf-8"),
            (header::CACHE_CONTROL, "no-store"),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        ],
        bytes,
    )
        .into_response()
}

fn text_response(status: u16, body: Vec<u8>, ctype: &str, cache: Option<&str>) -> Response {
    let mut r = Response::new(Body::from(body));
    *r.status_mut() = StatusCode::from_u16(status).expect("valid status");
    let h = r.headers_mut();
    h.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_str(ctype).expect("ascii"),
    );
    if let Some(cache) = cache {
        h.insert(
            header::CACHE_CONTROL,
            HeaderValue::from_str(cache).expect("ascii"),
        );
    }
    r
}

fn hook_text(body: &str) -> Response {
    text_response(
        200,
        body.as_bytes().to_vec(),
        "text/plain; charset=utf-8",
        Some("no-store"),
    )
}

/// A route whose handler belongs to a later slice (declared divergence).
fn not_implemented(path: &str) -> Response {
    json_response(501, &json!({"error": "not_implemented", "path": path}))
}

/// uvicorn's answer when an exception escapes the ASGI app.
fn uvicorn_500() -> Response {
    text_response(
        500,
        b"Internal Server Error".to_vec(),
        "text/plain; charset=utf-8",
        None,
    )
}

fn latin1(v: &HeaderValue) -> String {
    v.as_bytes().iter().map(|&b| b as char).collect()
}

/// `_hdr`: the first value.
fn first_header(h: &HeaderMap, name: &str) -> Option<String> {
    h.get(name).map(latin1)
}

/// The resolver reads headers through a dict: the last value wins.
fn last_header(h: &HeaderMap, name: &str) -> Option<String> {
    h.get_all(name).iter().next_back().map(latin1)
}

/// `urllib.parse.urlsplit(value).netloc` (Python 3.11): leading C0 and
/// space stripped, tab/CR/LF removed anywhere, a scheme only when it is
/// `[A-Za-z][A-Za-z0-9+.-]*` before the first colon, a netloc only after
/// `//`. Bracket errors raise `ValueError` (Err), which escapes the ASGI
/// app as uvicorn's 500.
fn urlsplit_netloc(value: &str) -> Result<String, ()> {
    let url: String = value
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut rest = url.as_str();
    if let Some(i) = rest.find(':')
        && i > 0
        && rest.as_bytes()[0].is_ascii_alphabetic()
        && rest[..i]
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
    {
        rest = &rest[i + 1..];
    }
    let Some(after) = rest.strip_prefix("//") else {
        return Ok(String::new());
    };
    let netloc = after.split(['/', '?', '#']).next().unwrap_or(after);
    let (open, close) = (netloc.contains('['), netloc.contains(']'));
    if open != close {
        return Err(()); // "Invalid IPv6 URL"
    }
    if open {
        let host = netloc.split_once('[').map_or("", |x| x.1);
        let host = host.split_once(']').map_or(host, |x| x.0);
        let valid = if let Some(v) = host.strip_prefix('v') {
            // `\Av[a-fA-F0-9]+\..+\Z`
            v.split_once('.').is_some_and(|(hex, tail)| {
                !hex.is_empty() && hex.chars().all(|c| c.is_ascii_hexdigit()) && !tail.is_empty()
            })
        } else {
            // ipaddress.ip_address: IPv6 (scope id allowed); IPv4 in brackets refuses.
            host.split('%')
                .next()
                .unwrap_or("")
                .parse::<std::net::Ipv6Addr>()
                .is_ok()
        };
        if !valid {
            return Err(());
        }
    }
    Ok(netloc.to_string())
}

/// `_host_part`: lower-cased host of a Host header or an Origin URL.
fn host_part(value: &str) -> Result<String, ()> {
    let mut v = value.to_string();
    if value.contains("://") {
        let netloc = urlsplit_netloc(value)?;
        if !netloc.is_empty() {
            v = netloc; // urlsplit().netloc keeps any userinfo
        }
    }
    let v = crate::storage::py_strip(&v).to_lowercase();
    if let Some(rest) = v.strip_prefix('[') {
        return Ok(rest.split(']').next().unwrap_or("").to_string());
    }
    if v.matches(':').count() == 1 {
        return Ok(v.rsplit_once(':').map(|(h, _)| h.to_string()).unwrap_or(v));
    }
    Ok(v)
}

/// `_browser_gate`: tokenless installs serve loopback browsers only.
/// Err: the header made `urlsplit` raise (answered as uvicorn's 500).
fn browser_gate(app: &App, h: &HeaderMap) -> Result<Option<&'static str>, ()> {
    if app.auth_configured() {
        return Ok(None);
    }
    let loopback = |v: &str| -> Result<bool, ()> {
        Ok(matches!(
            host_part(v)?.as_str(),
            "127.0.0.1" | "::1" | "localhost"
        ))
    };
    if let Some(o) = first_header(h, "origin")
        && !loopback(&o)?
    {
        return Ok(Some("forbidden_origin"));
    }
    if let Some(host) = first_header(h, "host")
        && !loopback(&host)?
    {
        return Ok(Some("forbidden_host"));
    }
    Ok(None)
}

fn resolve(app: &App, h: &HeaderMap) -> Resolved {
    auth::resolve(
        last_header(h, "authorization").as_deref(),
        &app.env_tokens,
        &app.store,
    )
}

/// `_principal` / `_authorized`: an unavailable store counts as unauthorized.
fn authorized(app: &App, h: &HeaderMap) -> bool {
    matches!(resolve(app, h), Resolved::Principal(..))
}

fn unauthorized() -> Response {
    json_response(
        401,
        &json!({"error": "unauthorized", "hint": UNAUTHORIZED_HINT}),
    )
}

fn principals_unavailable() -> Response {
    json_response(503, &json!({"error": "principals_unavailable"}))
}

fn method_not_allowed() -> Response {
    json_response(405, &json!({"error": "method_not_allowed"}))
}

/// `_read_body(receive, max_bytes)`: None when over the limit.
async fn read_body(body: Body, limit: usize) -> Option<Vec<u8>> {
    axum::body::to_bytes(body, limit)
        .await
        .ok()
        .map(|b| b.to_vec())
}

fn media_type(h: &HeaderMap) -> String {
    first_header(h, "content-type")
        .unwrap_or_default()
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase()
}

/// `parse_qsl(keep_blank_values=True)` into a dict: the last value wins.
pub fn parse_query(raw: Option<&str>) -> HashMap<String, String> {
    form_urlencoded::parse(raw.unwrap_or("").as_bytes())
        .into_owned()
        .collect()
}

/// `normalize_pairing_code`.
fn normalize_pairing_code(v: &Value) -> Option<String> {
    let text = v.as_str()?;
    let code: String = py_strip(text)
        .to_uppercase()
        .replace('-', "")
        .chars()
        .map(|c| match c {
            'O' => '0',
            'I' | 'L' => '1',
            c => c,
        })
        .collect();
    (code.chars().count() == 12 && code.chars().all(|c| PAIRING_CODE_ALPHABET.contains(c)))
        .then_some(code)
}

fn is_sha256_hex(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == 64
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

async fn pair(app: &App, method: &str, h: &HeaderMap, body: Body) -> Response {
    if method != "POST" {
        return method_not_allowed();
    }
    if h.contains_key("origin") && !crate::mutants::active("pair-ignores-origin") {
        return json_response(403, &json!({"error": "forbidden_origin"}));
    }
    if media_type(h) != "application/json" {
        return json_response(
            415,
            &json!({"error": "content_type_must_be_application_json"}),
        );
    }
    if app.pair_reserve().is_none() {
        return json_response(429, &json!({"error": "rate_limited"}));
    }
    let refused = || json_response(400, &json!({"error": "pairing_refused"}));
    let Some(raw) = read_body(body, PAIR_BODY_LIMIT).await else {
        return json_response(413, &json!({"error": "request_too_large"}));
    };
    let parsed = std::str::from_utf8(&raw)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(t).ok());
    let valid = parsed.as_ref().and_then(Value::as_object).is_some_and(|o| {
        o.len() == 2
            && o.get("code").and_then(normalize_pairing_code).is_some()
            && o.get("token_sha256").is_some_and(is_sha256_hex)
    });
    if !valid || !app.auth_configured() {
        return refused();
    }
    // Redemption writes the principal store: owned by a later slice. The
    // attempt keeps its reservation, as a failed redemption would.
    not_implemented("/api/pair")
}

/// `/api/hook/*`, routed before the bearer gate (`web/api.py:464-697`).
async fn hook(app: &App, path: &str, method: &str, h: &HeaderMap, body: Body) -> Response {
    match browser_gate(app, h) {
        Err(()) => return uvicorn_500(),
        Ok(Some(denied)) => return json_response(403, &json!({"error": denied})),
        Ok(None) => {}
    }
    let want = match path {
        "/api/hook/session-end" | "/api/hook/woke" | "/api/hook/subagent" => "POST",
        _ => "GET",
    };
    if method != want {
        return method_not_allowed();
    }
    match path {
        "/api/hook/memory-changes"
        | "/api/hook/park-gate"
        | "/api/hook/woke"
        | "/api/hook/subagent" => {
            if authorized(app, h) {
                not_implemented(path)
            } else {
                hook_text("")
            }
        }
        "/api/hook/session-end" => {
            match resolve(app, h) {
                Resolved::Unavailable => return principals_unavailable(),
                Resolved::None => return unauthorized(),
                Resolved::Principal(..) => {}
            }
            if read_body(body, SESSION_END_BODY_LIMIT).await.is_none() {
                return json_response(413, &json!({"error": "request_too_large"}));
            }
            not_implemented(path)
        }
        // session-start, memory-policy, coordination-start: their bodies
        // (briefing, policy and board text) belong to later slices.
        _ => not_implemented(path),
    }
}

fn body_limit(path: &str) -> usize {
    if crate::mutants::active("body-limit-off") {
        return usize::MAX;
    }
    if path.starts_with("/api/coordination/") {
        COORDINATION_BODY_LIMIT
    } else if TEXT_BODY_PATHS.contains(&path) {
        TEXT_BODY_LIMIT
    } else {
        CONTROL_BODY_LIMIT
    }
}

fn is_maintainer_path(path: &str) -> bool {
    path == "/api/maintainer" || path.starts_with("/api/maintainer/")
}

async fn api(
    app: &App,
    path: &str,
    method: &str,
    h: &HeaderMap,
    raw_query: Option<&str>,
    body: Body,
) -> Response {
    match browser_gate(app, h) {
        Err(()) => return uvicorn_500(),
        Ok(Some(denied)) => {
            return json_response(403, &json!({"error": denied, "hint": BROWSER_HINT}));
        }
        Ok(None) => {}
    }
    let stored = match resolve(app, h) {
        Resolved::Unavailable => return principals_unavailable(),
        Resolved::None => return unauthorized(),
        // Stored rows never carry an env principal's name or `default`
        // (they are dropped from the snapshot), so the name tells the source.
        Resolved::Principal(name) => {
            name != "default" && !app.env_tokens.map.iter().any(|(_, p)| *p == name)
        }
    };
    if method != "GET" && method != "POST" {
        return method_not_allowed();
    }
    if method == "POST" && OPERATOR_POST_PATHS.contains(&path) && stored {
        return json_response(403, &json!({"error": "operator_principal_required"}));
    }
    let params = parse_query(raw_query);
    let coordination_path = path.starts_with("/api/coordination/");
    let coordination_errors = coordination_path
        || (path == "/api/agents"
            && params.get("view").map(String::as_str) == Some("coordination"));
    let maintainer_path = is_maintainer_path(path);
    if coordination_path && method != "POST" {
        return method_not_allowed();
    }
    if method == "POST" {
        let Some(raw) = read_body(body, body_limit(path)).await else {
            return json_response(413, &json!({"error": "request_too_large"}));
        };
        if !raw.is_empty() {
            if media_type(h) != "application/json" {
                return json_response(
                    415,
                    &json!({"error": "content_type_must_be_application_json"}),
                );
            }
            let Ok(text) = std::str::from_utf8(&raw) else {
                // UnicodeDecodeError escapes the handler except on the board.
                return if coordination_path {
                    json_response(400, &json!({"error": "invalid_json"}))
                } else {
                    uvicorn_500()
                };
            };
            match serde_json::from_str::<Value>(text) {
                Err(_) => return json_response(400, &json!({"error": "invalid_json"})),
                Ok(v) if !v.is_object() => {
                    return json_response(400, &json!({"error": "body_must_be_object"}));
                }
                Ok(_) => {}
            }
        }
    }
    if coordination_path {
        return not_implemented(path);
    }
    if (coordination_errors || maintainer_path)
        && !app.auth_configured()
        && !crate::mutants::active("tokenless-maintainer-open")
    {
        // `authenticated_principal` refuses an open install outright.
        return json_response(401, &json!({"error": "authentication_required"}));
    }
    let table = RouteTable::get();
    if !table.handles(method, path) {
        if maintainer_path && table.has(path) {
            return json_response(400, &json!({"error": "invalid_request"}));
        }
        return if table.has(path) && !crate::mutants::active("route-405-as-404") {
            json_response(405, &json!({"error": "method_not_allowed", "path": path}))
        } else {
            json_response(404, &json!({"error": "not_found", "path": path}))
        };
    }
    if method == "GET" && path == "/api/search" {
        return search_route(app, raw_query).await;
    }
    not_implemented(path)
}

pub async fn handle(State(app): State<Arc<App>>, req: Request<Body>) -> Response {
    // uvicorn percent-decodes the path; an empty one is "/".
    let decoded = percent_encoding::percent_decode_str(req.uri().path())
        .decode_utf8_lossy()
        .into_owned();
    let path = if decoded.is_empty() {
        "/".to_string()
    } else {
        decoded
    };
    let method = req.method().as_str().to_ascii_uppercase();
    let raw_query = req.uri().query().map(String::from);
    let (parts, body) = req.into_parts();
    let h = parts.headers;

    if path == "/health" {
        let payload = health::payload(&app.service).await;
        let status = if payload["status"] == "ok" { 200 } else { 503 };
        return json_response(status, &payload);
    }
    if path == "/" {
        let mut r = Response::new(Body::empty());
        *r.status_mut() = StatusCode::TEMPORARY_REDIRECT;
        r.headers_mut()
            .insert(header::LOCATION, HeaderValue::from_static("/ui/"));
        for (k, v) in static_files::SECURITY_HEADERS {
            r.headers_mut().insert(k, HeaderValue::from_static(v));
        }
        return r;
    }
    if path == "/ui" || path.starts_with("/ui/") {
        let served = match &app.static_dir {
            Some(dir) => {
                let (dir, p) = (dir.clone(), path.clone());
                tokio::task::spawn_blocking(move || static_files::serve(&dir, &p))
                    .await
                    .ok()
                    .and_then(Result::ok)
            }
            // No Console build: what Python answers with no index.html.
            None => Some(static_files::Served {
                status: 404,
                body: b"not found".to_vec(),
                content_type: "text/plain".into(),
                cache: "no-store",
            }),
        };
        let mut r = match served {
            Some(s) => text_response(s.status, s.body, &s.content_type, Some(s.cache)),
            None => text_response(
                500,
                b"static error".to_vec(),
                "text/plain",
                Some("no-cache"),
            ),
        };
        for (k, v) in static_files::SECURITY_HEADERS {
            r.headers_mut().insert(k, HeaderValue::from_static(v));
        }
        return r;
    }
    if path.starts_with("/api/hook/")
        && matches!(
            path.as_str(),
            "/api/hook/session-start"
                | "/api/hook/memory-policy"
                | "/api/hook/memory-changes"
                | "/api/hook/session-end"
                | "/api/hook/coordination-start"
                | "/api/hook/park-gate"
                | "/api/hook/woke"
                | "/api/hook/subagent"
        )
    {
        return hook(&app, &path, &method, &h, body).await;
    }
    if path == "/api/pair" {
        return pair(&app, &method, &h, body).await;
    }
    if path.starts_with("/api/") || path == "/api" {
        return api(&app, &path, &method, &h, raw_query.as_deref(), body).await;
    }
    match resolve(&app, &h) {
        Resolved::Unavailable => return principals_unavailable(),
        Resolved::None => return unauthorized(),
        Resolved::Principal(..) => {}
    }
    if path == "/mcp" || path.starts_with("/mcp/") {
        return not_implemented(&path);
    }
    // The MCP app's own router: `/mcp` is its only route.
    text_response(
        404,
        b"Not Found".to_vec(),
        "text/plain; charset=utf-8",
        None,
    )
}

// ---- GET /api/search (spec.md S1-S7) -------------------------------------

/// Python `str.strip()`: Unicode whitespace plus the U+001C..U+001F separators.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// Python numeric literals allow single underscores between digits only.
fn py_underscores(t: &str) -> Option<String> {
    let b: Vec<char> = t.chars().collect();
    for (i, c) in b.iter().enumerate() {
        if *c == '_'
            && !(i > 0 && i + 1 < b.len() && b[i - 1].is_ascii_digit() && b[i + 1].is_ascii_digit())
        {
            return None;
        }
    }
    Some(t.replace('_', ""))
}

fn py_int(s: &str) -> Option<i64> {
    py_underscores(py_strip(s))?.parse::<i64>().ok()
}

fn py_float(s: &str) -> Option<f64> {
    py_underscores(py_strip(s))?.parse::<f64>().ok()
}

/// `_tribool` (web/routes.py:55): None follows config.
fn tribool(q: &HashMap<String, String>, key: &str) -> Option<bool> {
    let v = q.get(key)?;
    if matches!(v.as_str(), "" | "null" | "auto") {
        return None;
    }
    Some(matches!(
        v.trim().to_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    ))
}

fn list(q: &HashMap<String, String>, key: &str) -> Option<Vec<String>> {
    let items: Vec<String> = q
        .get(key)?
        .split(',')
        .map(|s| py_strip(s).to_string())
        .filter(|s| !s.is_empty())
        .collect();
    (!items.is_empty()).then_some(items)
}

async fn search_route(app: &App, raw_query: Option<&str>) -> Response {
    let q = parse_query(raw_query);
    let cfg = &app.service.config.memory;
    let ready = match app.service.ensure_init().await {
        Ok(r) => r,
        Err(e) => return json_response(500, &json!({"error": e})),
    };
    let bands = list(&q, "band");
    if let Some(bands) = &bands {
        let unknown: Vec<&String> = bands.iter().filter(|b| !cfg.bands.contains(b)).collect();
        if !unknown.is_empty() {
            let msg = format!(
                "unknown band name(s) {unknown:?} — this preset has {:?}",
                cfg.bands
            );
            return json_response(400, &json!({"error": msg}));
        }
    }
    let query = q
        .get("q")
        .map(|s| py_strip(s).to_string())
        .unwrap_or_default();
    if query.is_empty() {
        return json_response(
            200,
            &json!({"entries": [], "query": "", "count": 0, "low_confidence": true}),
        );
    }
    let top_k = q
        .get("top_k")
        .filter(|s| !s.is_empty())
        .and_then(|s| py_int(s))
        .unwrap_or(search::ROUTE_TOP_K);
    let k = match top_k {
        0 => cfg.top_k.max(0) as usize,
        n if n < 0 => return json_response(500, &json!({"error": "top_k must not be negative"})),
        n => n as usize,
    };
    let b = &cfg.bm25;
    let bm25_on = tribool(&q, "bm25").unwrap_or(b.enabled);
    let params = search::Params {
        query: query.clone(),
        k,
        sources: list(&q, "source").map(|v| v.into_iter().collect::<HashSet<_>>()),
        tags: list(&q, "tag").map(|v| {
            v.into_iter()
                .map(|t| t.to_lowercase())
                .collect::<HashSet<_>>()
        }),
        min_score: q
            .get("min_score")
            .filter(|s| !s.is_empty())
            .and_then(|s| py_float(s)),
        default_floor: cfg.search.min_score,
        bm25: bm25_on.then_some(search::Bm25Knobs {
            k1: b.k1,
            b: b.b,
            weight: b.weight,
            top_n: b.top_n.max(0) as usize,
            min_norm: b.min_score,
        }),
        hide_superseded: cfg.hide_superseded,
        bands: bands.map(|v| v.into_iter().collect()),
    };
    let ready2 = ready.clone();
    let floor = cfg.search_confidence_floor;
    let result = tokio::task::spawn_blocking(move || {
        let qv = ready2.embedder.embed_query(&query)?;
        let hits = search::rank(&ready2.bank, &qv, &params);
        let entries: Vec<Value> = hits
            .iter()
            .map(|h| search::entry_json(&ready2.bank, &ready2.bank.entries[h.idx], h.score))
            .collect();
        // `abstain.low_confidence` over the direct hits' raw scores.
        let best = hits
            .iter()
            .map(|h| h.score)
            .fold(f64::NEG_INFINITY, f64::max);
        let low = hits.is_empty() || (floor > 0.0 && best < floor);
        Ok::<_, anyhow::Error>((params.query, entries, low))
    })
    .await;
    match result {
        Ok(Ok((query, entries, low))) => json_response(
            200,
            &json!({"query": query, "count": entries.len(), "low_confidence": low, "entries": entries}),
        ),
        Ok(Err(e)) => json_response(500, &json!({"error": e.to_string()})),
        Err(e) => json_response(500, &json!({"error": e.to_string()})),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_part_matches_python() {
        let ok = |v: &str| host_part(v).unwrap();
        assert_eq!(ok("http://localhost:5173"), "localhost");
        assert_eq!(ok("http://user@localhost"), "user@localhost");
        assert_eq!(ok("[::1]:8765"), "::1");
        assert_eq!(ok("127.0.0.1:8765"), "127.0.0.1");
        assert_eq!(ok("::1"), "::1");
        assert_eq!(ok(" LocalHost "), "localhost");
        // Python 3.11 urlsplit, probed 2026-10-09.
        assert_eq!(ok("x y://localhost"), "x y");
        assert_eq!(ok("http://local\thost"), "localhost");
        assert_eq!(ok("  http://localhost"), "localhost");
        assert_eq!(ok("1http://localhost"), "1http");
        assert_eq!(ok("http://[::1%eth0]:5"), "::1%eth0");
        assert_eq!(ok("http://[v1.x]"), "v1.x");
        assert!(host_part("http://[::1").is_err());
        assert!(host_part("http://[localhost]").is_err());
        assert!(host_part("http://[127.0.0.1]").is_err());
    }

    #[test]
    fn pairing_code_normalization() {
        assert_eq!(
            normalize_pairing_code(&json!(" abcd-efgh-jkmn ")),
            Some("ABCDEFGHJKMN".into())
        );
        assert_eq!(
            normalize_pairing_code(&json!("oooo-iiii-llll")),
            Some("000011111111".into())
        );
        assert_eq!(normalize_pairing_code(&json!("uuuu-uuuu-uuuu")), None);
        assert_eq!(normalize_pairing_code(&json!("abc")), None);
        assert_eq!(normalize_pairing_code(&json!(12)), None);
        assert!(is_sha256_hex(&json!("a".repeat(64))));
        assert!(!is_sha256_hex(&json!("A".repeat(64))));
    }
}
