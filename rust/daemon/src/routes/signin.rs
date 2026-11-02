//! Console sign-in with a one-time link (spec 2026-11-02, section 4;
//! `web/api.py` `_signin` and `_console_route`), outside `ConsoleRoutes`:
//!
//! * `POST /api/console/session` ([`redeem`]), pre-auth and browser-called,
//!   dispatched beside `/api/pair` before the bearer gate;
//! * `POST /api/console/link`, `GET /api/console/sessions` and
//!   `POST /api/console/sessions/revoke` ([`route`]), behind the bearer gate,
//!   which has already resolved the caller (a Console session included).

use crate::auth::{self, PrincipalSource, PrincipalStore};
use crate::console_sessions::{self as sessions, Redeemed, SessionSnapshot, Unavailable};
use crate::http::{
    ApiCaller, App, RequestBody, authorization, first_header, json_response, media_type,
    method_not_allowed, parse_body_json, read_body, uvicorn_500,
};
use crate::routes::pair::{Attempt, PairLimiter, Slot, is_sha256_hex, normalize_pairing_code};
use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

/// A sign-in body is `{"code", "token_sha256", "label"}` (64 characters):
/// under 300 bytes.
const SIGNIN_BODY_LIMIT: usize = 1024;
const BANK_UNREACHABLE: &str = "the daemon could not reach its bank just now; try again in a \
     minute, and if it keeps failing, check the daemon log";
const NO_TOKEN: &str = "this daemon has no token configured, so the Console needs no \
     sign-in: open it on this machine";
const NO_BANK: &str = "Console sign-in needs the daemon's Postgres bank";
const ROUTES: [(&str, &str); 3] = [
    ("/api/console/link", "POST"),
    ("/api/console/sessions", "GET"),
    ("/api/console/sessions/revoke", "POST"),
];

fn refused() -> Response {
    json_response(400, &json!({"error": "signin_refused"}))
}

fn signin_unavailable() -> Response {
    json_response(503, &json!({"error": "signin_unavailable"}))
}

/// `_issuer_still_valid`: at redemption, the credential that asked for the
/// code still resolves to the same principal. A stale stored view is
/// unknown, not refused (the caller answers 503).
fn issuer_still_valid(
    env_issuers: &HashMap<String, String>,
    store: &PrincipalStore,
    principal: &str,
    source: &str,
    issuer: &str,
) -> Result<bool, Unavailable> {
    match source {
        sessions::SOURCE_ENVIRONMENT => {
            Ok(env_issuers.get(issuer).map(String::as_str) == Some(principal))
        }
        sessions::SOURCE_STORE => {
            if !store.available() {
                return Err(Unavailable);
            }
            Ok(store.lookup(issuer).as_deref() == Some(principal))
        }
        _ => Ok(false),
    }
}

/// The redemption, in a task of its own, as `/api/pair`'s: a client that
/// disconnects drops only its wait. The slot is given back when the store
/// call ends; a success, or an issuer that could not be checked, refunds
/// its attempt, and a success is usable at once (`SessionSnapshot::add`).
async fn redeem_owned<F>(
    limiter: PairLimiter,
    view: Arc<SessionSnapshot>,
    attempt: Attempt,
    slot: Slot,
    redemption: F,
) -> Result<Redeemed, String>
where
    F: std::future::Future<Output = Result<Redeemed, String>> + Send + 'static,
{
    tokio::spawn(async move {
        let outcome = redemption.await;
        drop(slot);
        match &outcome {
            Ok(Redeemed::Session(row)) => {
                limiter.refund(attempt);
                view.add(row.clone());
            }
            // The issuing token could not be checked: nothing was spent.
            Ok(Redeemed::Unavailable) => limiter.refund(attempt),
            Ok(Redeemed::Refused) | Err(_) => {}
        }
        outcome
    })
    .await
    .map_err(|_| "the redemption task failed".to_string())?
}

/// `POST /api/console/session`: redeem a sign-in code for the SHA-256 of a
/// token the browser minted. Browser-called, so the request's `Origin` must
/// equal the origin the code was issued for; a JSON body only (a
/// preflight, and no CORS headers are served); exactly `{code,
/// token_sha256, label}` in 1 KiB. Every bad code is the same `400
/// signin_refused` and counts against sign-in's own failure budget; nothing
/// from the body or an error is logged or returned.
pub(crate) async fn redeem(
    app: &App,
    method: &str,
    h: &HeaderMap,
    body: &mut RequestBody,
) -> Response {
    if method != "POST" {
        return method_not_allowed();
    }
    let Some(origin) = first_header(h, "origin").and_then(|o| sessions::normalize_origin(&o))
    else {
        return json_response(403, &json!({"error": "forbidden_origin"}));
    };
    if media_type(h) != "application/json" {
        return json_response(
            415,
            &json!({"error": "content_type_must_be_application_json"}),
        );
    }
    let Some(attempt) = app.signin_limiter.reserve() else {
        return json_response(429, &json!({"error": "rate_limited"}));
    };
    let Some(raw) = read_body(body, SIGNIN_BODY_LIMIT).await else {
        return json_response(413, &json!({"error": "request_too_large"}));
    };
    let parsed = std::str::from_utf8(&raw)
        .ok()
        .and_then(|t| parse_body_json(t).ok());
    let request = parsed.as_ref().and_then(Value::as_object).and_then(|o| {
        let keys: HashSet<&str> = o.keys().map(String::as_str).collect();
        if keys != HashSet::from(["code", "token_sha256", "label"]) {
            return None;
        }
        let code = normalize_pairing_code(&o["code"])?;
        let token_hash = o["token_sha256"]
            .as_str()
            .filter(|_| is_sha256_hex(&o["token_sha256"]))?;
        let label = sessions::clean_label(o["label"].as_str()?)?;
        Some((code, token_hash.to_string(), label))
    });
    let Some((code, token_hash, label)) = request else {
        return refused();
    };
    let Some(view) = app.sessions.clone().filter(|_| app.auth_configured()) else {
        return refused();
    };
    let Some(slot) = app.signin_limiter.enter() else {
        // Never reached the store: not a failed redemption.
        app.signin_limiter.refund(attempt);
        return json_response(429, &json!({"error": "rate_limited"}));
    };
    let (dsn, store, env_issuers) = (
        app.board.dsn.clone(),
        app.store.clone(),
        app.env_issuers.clone(),
    );
    let code_hash = hex::encode(Sha256::digest(code.as_bytes()));
    let key = sessions::origin_key(&origin);
    let redemption = async move {
        sessions::redeem_code(&dsn, &code_hash, &token_hash, &key, &label, |p, s, i| {
            issuer_still_valid(&env_issuers, &store, p, s, i)
        })
        .await
    };
    match redeem_owned(app.signin_limiter.clone(), view, attempt, slot, redemption).await {
        Ok(Redeemed::Session(row)) => json_response(
            200,
            &json!({"principal": row.principal, "session_id": row.session_id,
                "expires_at": row.expires_at}),
        ),
        Ok(Redeemed::Refused) => refused(),
        Ok(Redeemed::Unavailable) => signin_unavailable(),
        Err(_) => {
            // The outcome is unknown; its text may hold row data, so the
            // log names the class only, as Python logs the type.
            eprintln!("console sign-in failed (store error)");
            signin_unavailable()
        }
    }
}

/// `_issuing_hash`: the SHA-256 of the bearer that resolved to `principal`
/// from `source`, a link session's issuer, which must keep naming the same
/// principal for the session to resolve.
fn issuing_hash(
    app: &App,
    h: &HeaderMap,
    principal: &str,
    source: Option<PrincipalSource>,
) -> Option<String> {
    let header = authorization(h);
    sessions::bearer_hashes(header.as_deref())
        .into_iter()
        .find(|hash| match source {
            Some(PrincipalSource::Environment) => {
                app.env_issuers.get(hash).map(String::as_str) == Some(principal)
            }
            Some(PrincipalSource::Store) => app.store.lookup(hash).as_deref() == Some(principal),
            _ => false,
        })
}

/// `_dead_session_ids`: the sessions in the daemon's view that no longer
/// resolve; none when that cannot be told now (a stale view, all or
/// nothing, as Python's comprehension raising part-way is).
fn dead_session_ids(app: &App, view: &SessionSnapshot) -> HashSet<String> {
    if !view.available() {
        return HashSet::new();
    }
    let now = sessions::wall_now();
    let views = sessions::Views {
        sessions: view,
        env_issuers: &app.env_issuers,
        env_principals: &app.env_principals,
        store: &app.store,
    };
    let mut dead = HashSet::new();
    for row in view.rows() {
        match sessions::still_resolves(&row, &views, now) {
            Err(Unavailable) => return HashSet::new(),
            Ok(false) => {
                dead.insert(row.session_id);
            }
            Ok(true) => {}
        }
    }
    dead
}

fn source_name(source: Option<PrincipalSource>) -> &'static str {
    match source {
        Some(PrincipalSource::Store) => sessions::SOURCE_STORE,
        _ => sessions::SOURCE_ENVIRONMENT,
    }
}

/// A database call that runs to its end even when the client goes away, as
/// Python's executor thread does.
async fn detached<T, F>(work: F) -> Result<T, String>
where
    T: Send + 'static,
    F: std::future::Future<Output = Result<T, String>> + Send + 'static,
{
    tokio::spawn(work)
        .await
        .map_err(|_| "the task failed".to_string())?
}

/// The bearer-gated Console sign-in routes (`_console_route`): `POST
/// /api/console/link` asks for a sign-in code, `GET /api/console/sessions`
/// lists signed-in browsers and `POST /api/console/sessions/revoke` ends
/// one. An operator (`auth::operator`) sees and revokes every session;
/// anyone else only their own principal's.
pub(crate) async fn route(
    app: &App,
    method: &str,
    path: &str,
    h: &HeaderMap,
    body: Map<String, Value>,
    caller: &ApiCaller,
) -> Response {
    let Some(&(_, want)) = ROUTES.iter().find(|(p, _)| *p == path) else {
        return json_response(404, &json!({"error": "not_found", "path": path}));
    };
    if method != want {
        return json_response(405, &json!({"error": "method_not_allowed", "path": path}));
    }
    let operator = auth::operator(caller.source, caller.issued_by);
    let principal = caller.principal.as_str();
    let scope = (!operator).then_some(principal.to_string());
    let dsn = app.board.dsn.clone();
    let view = match (&app.sessions, app.auth_configured()) {
        (_, false) => Err(NO_TOKEN),
        (None, true) => Err(NO_BANK),
        (Some(view), true) => Ok(view.clone()),
    };
    if path == "/api/console/sessions" {
        let Ok(view) = view else {
            return json_response(
                200,
                &json!({"sessions": [], "operator": operator, "available": false}),
            );
        };
        let current = (caller.source == Some(PrincipalSource::Session))
            .then(|| caller.session_id.clone())
            .flatten();
        let rows =
            match detached(async move { sessions::list_sessions(&dsn, scope.as_deref()).await })
                .await
            {
                Ok(rows) => rows,
                Err(_) => {
                    eprintln!("console sessions: list failed (store error)");
                    return json_response(
                        503,
                        &json!({"error": "signin_unavailable", "hint": BANK_UNREACHABLE}),
                    );
                }
            };
        let rows = sessions::with_last_use(rows, &view);
        // A session whose issuer or principal is gone no longer signs
        // anything in, though its row has not been revoked yet.
        let dead = dead_session_ids(app, &view);
        let listed: Vec<Value> = rows
            .into_iter()
            .filter(|r| !dead.contains(r["session_id"].as_str().unwrap_or("")))
            .map(|mut r| {
                let mine = current.is_some() && r["session_id"].as_str() == current.as_deref();
                r["current"] = json!(mine);
                r
            })
            .collect();
        return json_response(
            200,
            &json!({"sessions": listed, "operator": operator, "available": true}),
        );
    }
    let view = match view {
        Ok(view) => view,
        Err(hint) => {
            return json_response(409, &json!({"error": "signin_unavailable", "hint": hint}));
        }
    };
    if path == "/api/console/sessions/revoke" {
        let session_id = body
            .get("session_id")
            .and_then(Value::as_str)
            .map(String::from);
        let Some(session_id) =
            session_id.filter(|sid| body.len() == 1 && (1..=64).contains(&sid.chars().count()))
        else {
            return json_response(
                400,
                &json!({"error": "invalid_request", "hint": "send {\"session_id\": \"<id>\"}"}),
            );
        };
        let sid = session_id.clone();
        let revoked =
            match detached(
                async move { sessions::revoke_session(&dsn, &sid, scope.as_deref()).await },
            )
            .await
            {
                Ok(revoked) => revoked,
                Err(_) => {
                    eprintln!("console sessions: revoke failed (store error)");
                    return json_response(
                        503,
                        &json!({"error": "signin_unavailable", "hint": BANK_UNREACHABLE}),
                    );
                }
            };
        if revoked {
            view.drop_ids(std::slice::from_ref(&session_id));
        }
        return json_response(200, &json!({"revoked": revoked}));
    }
    // POST /api/console/link
    if !body.keys().all(|k| k == "origin" || k == "to") {
        return json_response(
            400,
            &json!({"error": "invalid_request",
                "hint": "send {\"origin\": \"<daemon origin>\"} and optionally \"to\": \"<Console page>\""}),
        );
    }
    let Some(origin) = body
        .get("origin")
        .and_then(Value::as_str)
        .and_then(sessions::normalize_origin)
    else {
        return json_response(
            400,
            &json!({"error": "invalid_origin",
                "hint": "origin is the daemon address the client reached: http(s)://host[:port], with no path"}),
        );
    };
    let to = match body.get("to") {
        None | Some(Value::Null) => None,
        Some(Value::String(to)) if sessions::valid_route_id(to) => Some(to.clone()),
        Some(_) => {
            return json_response(
                400,
                &json!({"error": "invalid_route", "hint": "to is a Console page id, such as board"}),
            );
        }
    };
    let cfg = &app.board.config;
    let stored = match crate::read::maintainer::config_problem(
        &cfg.maintainer_rp_id,
        &cfg.maintainer_origin,
    ) {
        // Python's `problem()` raises out of the route: uvicorn's 500.
        Err(()) => return uvicorn_500(),
        // Passkeys set up: the Console's one HTTPS address, wherever the
        // client reached the daemon.
        Ok(None) => sessions::normalize_origin(&cfg.maintainer_origin)
            .unwrap_or_else(|| cfg.maintainer_origin.clone()),
        Ok(Some(_)) if sessions::is_loopback_origin(&origin) => origin.clone(),
        Ok(Some(_)) => {
            return json_response(
                409,
                &json!({"error": "signin_origin_unsupported", "hint": format!(
                    "without passkeys, a sign-in link opens the Console only on the daemon's own \
                     machine, and {origin} is not a local address: on the daemon's host run \
                     `pseudolife-mcp console` with PSEUDOLIFE_MCP_DAEMON_URL set to its local \
                     address (such as http://127.0.0.1:8765), or give the Console an HTTPS \
                     address with `pseudolife-mcp maintainer setup` there")}),
            );
        }
    };
    let Some(issuer) = issuing_hash(app, h, principal, caller.source) else {
        return json_response(
            403,
            &json!({"error": "session_cannot_issue",
                "hint": "a sign-in link needs a client's own token"}),
        );
    };
    let (who, source, key) = (
        principal.to_string(),
        source_name(caller.source),
        sessions::origin_key(&stored),
    );
    let issued =
        detached(async move { sessions::issue_code(&dsn, &who, source, &issuer, &key).await })
            .await;
    let (code, expires_at) = match issued {
        Ok(issued) => issued,
        Err(_) => {
            eprintln!("console sign-in link failed (store error)");
            return json_response(
                503,
                &json!({"error": "signin_unavailable", "hint": BANK_UNREACHABLE}),
            );
        }
    };
    let shown = sessions::format_code(&code);
    let mut url = format!("{stored}/ui/#/signin?code={shown}");
    if let Some(to) = &to {
        url.push_str(&format!("&to={to}"));
    }
    json_response(
        200,
        &json!({"code": shown, "url": url, "origin": stored, "principal": principal,
            "expires_at": expires_at, "expires_in": sessions::CODE_TTL_S as i64}),
    )
}

#[cfg(test)]
#[path = "signin_tests.rs"]
mod tests;
