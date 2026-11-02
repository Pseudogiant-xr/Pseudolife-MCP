//! The gate's session call sites and the four sign-in routes over HTTP,
//! after tests/test_console_signin.py: where a session resolves and where it
//! must not (the near misses), the operator allowlist, staleness, and every
//! refusal the routes give before the bank. The bank-backed flow is the
//! `db_` test at the end (`PL_W1A_TEST_DSN`).

use super::*;
use crate::auth::{EnvTokens, StoredPrincipal};
use crate::console_sessions::{ConsoleSession, METHOD_LINK, SESSION_MAX_S, wall_now};
use crate::http::handle;
use axum::body::Body;
use axum::extract::State;
use axum::http::Request;
use std::time::{Duration, Instant};

const ENV_TOKEN: &str = "fixture-token";
const STORED_TOKEN: &str = "fixture-stored-token";
const SESSION_TOKEN: &str = "fixture-browser-session-token";
const STORE_SESSION_TOKEN: &str = "fixture-browser-session-from-paired";
const ORIGIN: &str = "http://127.0.0.1:8765";

fn sha(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

fn row(token: &str, principal: &str, source: &str, issuer: &str, sid: &str) -> ConsoleSession {
    let created = wall_now() - 60.0;
    ConsoleSession {
        session_id: sid.into(),
        token_hash: sha(token),
        principal: principal.into(),
        source: source.into(),
        issuer: issuer.into(),
        method: METHOD_LINK.into(),
        label: "Firefox on Linux".into(),
        created_at: created,
        last_used_at: None,
        expires_at: created + SESSION_MAX_S,
    }
}

fn env_row() -> ConsoleSession {
    row(
        SESSION_TOKEN,
        "default",
        "environment",
        &sha(ENV_TOKEN),
        "s1",
    )
}

fn store_row() -> ConsoleSession {
    row(
        STORE_SESSION_TOKEN,
        "laptop",
        "store",
        &sha(STORED_TOKEN),
        "s2",
    )
}

/// A fixture daemon: the singular token `fixture-token`, a loaded stored
/// view holding `laptop` (paired with `fixture-stored-token`), a loaded
/// session view holding `rows`, and the bank at `dsn`.
fn app_with(dsn: &str, token: Option<&str>, rows: Vec<ConsoleSession>, view: bool) -> Arc<App> {
    let base = crate::http::tests::mcp_refusal_app();
    let store = Arc::new(PrincipalStore::new());
    store.refresh(
        vec![StoredPrincipal {
            principal: "laptop".into(),
            token_hash: Some(sha(STORED_TOKEN)),
            tier: None,
            board: true,
            revoked: false,
            owned: true,
        }],
        None,
        &[],
        Instant::now(),
    );
    let board = Arc::new(crate::board::Board::new(
        base.service.config.coordination.clone(),
        dsn.into(),
        base.service.clone(),
        store.clone(),
    ));
    let sessions = view.then(|| {
        let s = Arc::new(SessionSnapshot::new());
        s.finish_refresh(s.begin_refresh(), rows, vec![]);
        s
    });
    Arc::new(
        App::new(
            base.service.clone(),
            EnvTokens {
                single: token.map(String::from),
                map: vec![],
            },
            store,
            None,
            board,
        )
        .with_sessions(sessions),
    )
}

/// No reachable bank (the DSN does not parse, so no socket is opened).
fn app(rows: Vec<ConsoleSession>) -> Arc<App> {
    app_with("not a connection string", Some(ENV_TOKEN), rows, true)
}

async fn call(app: &Arc<App>, request: Request<Body>) -> (u16, HeaderMap, Vec<u8>) {
    let response =
        tokio::time::timeout(Duration::from_secs(30), handle(State(app.clone()), request))
            .await
            .expect("answered");
    let status = response.status().as_u16();
    let headers = response.headers().clone();
    let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
        .await
        .unwrap();
    (status, headers, bytes.to_vec())
}

fn bearer(method: &str, path: &str, token: &str, body: Option<&str>) -> Request<Body> {
    let r = Request::builder()
        .method(method)
        .uri(path)
        .header("authorization", format!("Bearer {token}"));
    match body {
        Some(b) => r
            .header("content-type", "application/json")
            .body(Body::from(b.to_string())),
        None => r.body(Body::empty()),
    }
    .unwrap()
}

fn json_of(bytes: &[u8]) -> Value {
    serde_json::from_slice(bytes).unwrap_or(Value::Null)
}

// -- where a session resolves, and where it must not ------------------------------

#[tokio::test]
async fn a_session_reads_the_api() {
    let app = app(vec![env_row()]);
    let (status, _, out) = call(&app, bearer("GET", "/api/nope", SESSION_TOKEN, None)).await;
    assert_eq!(
        (status, json_of(&out)),
        (404, json!({"error": "not_found", "path": "/api/nope"}))
    );
}

/// The near misses: every agent path answers a session bearer as an
/// unknown bearer, so a session is never an agent credential.
#[tokio::test]
async fn a_session_is_not_an_agent_credential() {
    let app = app(vec![env_row()]);
    for (method, path) in [
        ("POST", "/mcp"),
        ("POST", "/mcp/"),
        ("POST", "/mcp/messages"),
        ("GET", "/mcp"),
        ("POST", "/api/coordination/receive"),
        ("POST", "/api/coordination/register"),
        ("POST", "/api/coordination/send"),
        ("POST", "/api/daemon-notice"),
    ] {
        let (status, _, out) = call(&app, bearer(method, path, SESSION_TOKEN, Some("{}"))).await;
        // The transport gate's own refusal: the session lookup never ran,
        // not a later handler turning the session away.
        assert_eq!(
            (status, json_of(&out)),
            (
                401,
                json!({"error": "unauthorized", "hint": crate::http::UNAUTHORIZED_HINT})
            ),
            "{method} {path}"
        );
    }
}

#[tokio::test]
async fn the_hooks_treat_a_session_as_unauthorized() {
    let app = app(vec![env_row()]);
    let (status, headers, out) = call(
        &app,
        bearer("GET", "/api/hook/coordination-start", SESSION_TOKEN, None),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(headers["x-pl-board"], "off; reason=unauthorized");
    assert!(out.is_empty());
    for (method, path) in [
        ("GET", "/api/hook/park-gate?agent=a1"),
        ("POST", "/api/hook/woke?agent=a1"),
    ] {
        let (status, _, out) = call(&app, bearer(method, path, SESSION_TOKEN, None)).await;
        assert_eq!(status, 200, "{path}");
        assert!(out.is_empty() || out == b"allow\n", "{path}: {out:?}");
    }
    // The environment token is not refused there.
    let (_, headers, _) = call(
        &app,
        bearer("GET", "/api/hook/coordination-start", ENV_TOKEN, None),
    )
    .await;
    assert_ne!(headers["x-pl-board"], "off; reason=unauthorized");
}

#[tokio::test]
async fn a_session_cannot_issue_a_session() {
    let app = app(vec![env_row(), store_row()]);
    for token in [SESSION_TOKEN, STORE_SESSION_TOKEN] {
        let body = json!({"origin": ORIGIN}).to_string();
        let (status, _, out) = call(
            &app,
            bearer("POST", "/api/console/link", token, Some(&body)),
        )
        .await;
        assert_eq!(status, 403);
        assert_eq!(
            json_of(&out),
            json!({"error": "session_cannot_issue", "hint": "a sign-in link needs a client's own \
                token: run `pseudolife-mcp console` on a machine that uses Pseudolife"})
        );
        // Ahead of the verb check, as Python's.
        let (status, _, _) = call(&app, bearer("GET", "/api/console/link", token, None)).await;
        assert_eq!(status, 403);
    }
}

// -- the operator allowlist --------------------------------------------------------

#[tokio::test]
async fn only_a_session_an_environment_token_issued_may_change_settings() {
    let app = app(vec![env_row(), store_row()]);
    let (status, _, out) = call(
        &app,
        bearer("POST", "/api/config", STORE_SESSION_TOKEN, None),
    )
    .await;
    assert_eq!(
        (status, out.as_slice()),
        (403, &br#"{"error": "operator_principal_required"}"#[..])
    );
    // The daemon notice never resolves a session at all: an unknown bearer.
    for token in [SESSION_TOKEN, STORE_SESSION_TOKEN] {
        let (status, _, _) = call(&app, bearer("POST", "/api/daemon-notice", token, None)).await;
        assert_eq!(status, 401);
    }
    let (_, _, out) = call(&app, bearer("POST", "/api/config", SESSION_TOKEN, None)).await;
    assert!(!String::from_utf8_lossy(&out).contains("operator_principal_required"));
    // A paired machine's session reads them, as its machine does.
    let (status, _, _) = call(
        &app,
        bearer("GET", "/api/config", STORE_SESSION_TOKEN, None),
    )
    .await;
    assert_eq!(status, 200);
}

// -- staleness -----------------------------------------------------------------

#[tokio::test]
async fn a_stale_session_view_is_503_through_the_gate() {
    let app = app_with("not a connection string", Some(ENV_TOKEN), vec![], true);
    let view = app.sessions.clone().unwrap();
    let long_ago = Instant::now()
        .checked_sub(crate::console_sessions::STALE_AFTER + Duration::from_secs(1))
        .unwrap();
    view.finish_refresh(view.begin_refresh_at(long_ago), vec![env_row()], vec![]);
    let (status, _, out) = call(&app, bearer("GET", "/api/nope", SESSION_TOKEN, None)).await;
    assert_eq!(
        (status, json_of(&out)),
        (503, json!({"error": "principals_unavailable"}))
    );
    // Not on the paths that never resolve a session: an unknown bearer there.
    let (status, _, _) = call(
        &app,
        bearer("POST", "/api/daemon-notice", SESSION_TOKEN, None),
    )
    .await;
    assert_eq!(status, 401);
    // The environment token does not depend on the session view.
    let (status, _, _) = call(&app, bearer("GET", "/api/nope", ENV_TOKEN, None)).await;
    assert_eq!(status, 404);
}

#[tokio::test]
async fn an_environment_session_resolves_while_the_stored_view_is_stale() {
    let app = app(vec![env_row(), store_row()]);
    let long_ago = Instant::now()
        .checked_sub(crate::auth::STALE_AFTER + Duration::from_secs(1))
        .unwrap();
    app.store.refresh(vec![], None, &[], long_ago);
    let (status, _, _) = call(&app, bearer("GET", "/api/nope", SESSION_TOKEN, None)).await;
    assert_eq!(status, 404);
    // A session a stored principal issued cannot be checked: 503.
    let (status, _, _) = call(&app, bearer("GET", "/api/nope", STORE_SESSION_TOKEN, None)).await;
    assert_eq!(status, 503);
}

#[tokio::test]
async fn a_session_reads_the_board_view_and_the_maintainer_status() {
    let app = app(vec![env_row()]);
    for path in ["/api/agents?view=coordination", "/api/maintainer"] {
        let (status, _, out) = call(&app, bearer("GET", path, SESSION_TOKEN, None)).await;
        let text = String::from_utf8_lossy(&out);
        assert_ne!(status, 401, "{path}: {text}");
        assert!(
            !text.contains("\"unauthorized\"") && !text.contains("authentication_required"),
            "{path}: {text}"
        );
    }
}

#[tokio::test]
async fn a_sessions_use_is_noted_under_its_principal() {
    let activity = Arc::new(crate::principal_activity::Recorder::new());
    let base = app(vec![env_row()]);
    let app = Arc::new(
        App::new(
            base.service.clone(),
            EnvTokens {
                single: Some(ENV_TOKEN.into()),
                map: vec![],
            },
            base.store.clone(),
            None,
            base.board.clone(),
        )
        .with_sessions(base.sessions.clone())
        .with_activity(Some(activity.clone())),
    );
    call(&app, bearer("GET", "/api/nope", SESSION_TOKEN, None)).await;
    let batch = activity.take().unwrap();
    assert_eq!(batch.seen.keys().collect::<Vec<_>>(), vec!["default"]);
    assert!(batch.rejected.is_empty());
}

// -- the bearer-gated routes before the bank ---------------------------------------

#[tokio::test]
async fn an_unknown_console_route_is_not_found_and_a_wrong_verb_names_its_path() {
    let app = app(vec![]);
    let (status, _, out) = call(&app, bearer("GET", "/api/console/nope", ENV_TOKEN, None)).await;
    assert_eq!(
        (status, json_of(&out)),
        (
            404,
            json!({"error": "not_found", "path": "/api/console/nope"})
        )
    );
    let (status, _, out) = call(&app, bearer("GET", "/api/console/link", ENV_TOKEN, None)).await;
    assert_eq!(
        (status, json_of(&out)),
        (
            405,
            json!({"error": "method_not_allowed", "path": "/api/console/link"})
        )
    );
    // Unauthenticated: the gate's 401 first.
    let request = Request::builder()
        .method("GET")
        .uri("/api/console/sessions")
        .body(Body::empty())
        .unwrap();
    assert_eq!(call(&app, request).await.0, 401);
}

#[tokio::test]
async fn link_refusals_before_the_bank() {
    let app = app(vec![]);
    let link = |body: Value| {
        bearer(
            "POST",
            "/api/console/link",
            ENV_TOKEN,
            Some(&body.to_string()),
        )
    };
    for body in [
        json!({"origin": "http://127.0.0.1:8765/ui"}),
        json!({"origin": null}),
        json!({"origin": "javascript:alert(1)"}),
        json!({}),
        json!({"origin": 8765}),
    ] {
        let (status, _, out) = call(&app, link(body.clone())).await;
        assert_eq!(
            (status, json_of(&out)["error"].clone()),
            (400, json!("invalid_origin")),
            "{body}"
        );
    }
    let (status, _, out) = call(&app, link(json!({"origin": ORIGIN, "extra": 1}))).await;
    assert_eq!(
        (status, json_of(&out)["error"].clone()),
        (400, json!("invalid_request"))
    );
    for to in [json!("../evil"), json!(7), json!("")] {
        let (status, _, out) = call(&app, link(json!({"origin": ORIGIN, "to": to}))).await;
        assert_eq!(
            (status, json_of(&out)["error"].clone()),
            (400, json!("invalid_route")),
            "{to}"
        );
    }
    let (status, _, out) = call(&app, link(json!({"origin": "http://100.64.0.1:8765"}))).await;
    let body = json_of(&out);
    assert_eq!(
        (status, body["error"].clone()),
        (409, json!("signin_origin_unsupported"))
    );
    let hint = body["hint"].as_str().unwrap();
    assert!(
        hint.contains("http://100.64.0.1:8765 is not a local address")
            && hint.contains("maintainer setup")
    );
    // A valid loopback request reaches the bank, which is unreachable here.
    let (status, _, out) = call(&app, link(json!({"origin": ORIGIN, "to": null}))).await;
    assert_eq!(
        (status, json_of(&out)),
        (
            503,
            json!({"error": "signin_unavailable", "hint": BANK_UNREACHABLE})
        )
    );
}

#[tokio::test]
async fn an_open_daemon_or_one_without_sessions_offers_no_sign_in() {
    let open = app_with("not a connection string", None, vec![], false);
    let request = Request::builder()
        .method("POST")
        .uri("/api/console/link")
        .header("content-type", "application/json")
        .body(Body::from(json!({"origin": ORIGIN}).to_string()))
        .unwrap();
    let (status, _, out) = call(&open, request).await;
    assert_eq!(
        (status, json_of(&out)),
        (
            409,
            json!({"error": "signin_unavailable", "hint": NO_TOKEN})
        )
    );
    let request = Request::builder()
        .method("GET")
        .uri("/api/console/sessions")
        .body(Body::empty())
        .unwrap();
    let (status, _, out) = call(&open, request).await;
    assert_eq!(
        (status, json_of(&out)),
        (
            200,
            json!({"sessions": [], "operator": true, "available": false})
        )
    );
    let bankless = app_with("not a connection string", Some(ENV_TOKEN), vec![], false);
    let body = json!({"origin": ORIGIN}).to_string();
    let (status, _, out) = call(
        &bankless,
        bearer("POST", "/api/console/link", ENV_TOKEN, Some(&body)),
    )
    .await;
    assert_eq!(
        (status, json_of(&out)),
        (409, json!({"error": "signin_unavailable", "hint": NO_BANK}))
    );
    let (status, _, out) = call(
        &bankless,
        bearer("GET", "/api/console/sessions", STORED_TOKEN, None),
    )
    .await;
    assert_eq!(
        (status, json_of(&out)),
        (
            200,
            json!({"sessions": [], "operator": false, "available": false})
        )
    );
}

#[tokio::test]
async fn revoke_takes_exactly_one_session_id() {
    let app = app(vec![]);
    for body in [
        json!({}),
        json!({"session_id": ""}),
        json!({"session_id": 7}),
        json!({"session_id": "s1", "x": 1}),
        json!({"session_id": "x".repeat(65)}),
    ] {
        let (status, _, out) = call(
            &app,
            bearer(
                "POST",
                "/api/console/sessions/revoke",
                ENV_TOKEN,
                Some(&body.to_string()),
            ),
        )
        .await;
        assert_eq!(
            (status, json_of(&out)),
            (
                400,
                json!({"error": "invalid_request", "hint": "send {\"session_id\": \"<id>\"}"})
            ),
            "{body}"
        );
    }
}

// -- POST /api/console/session before the bank -------------------------------------

fn signin_body() -> Value {
    json!({"code": "ABCD-EFGH-JKMN", "token_sha256": sha(SESSION_TOKEN), "label": "Firefox"})
}

fn signin(origin: Option<&str>, ctype: Option<&str>, body: &[u8]) -> Request<Body> {
    let mut r = Request::builder()
        .method("POST")
        .uri("/api/console/session");
    if let Some(o) = origin {
        r = r.header("origin", o);
    }
    if let Some(c) = ctype {
        r = r.header("content-type", c);
    }
    r.body(Body::from(body.to_vec())).unwrap()
}

const JS: Option<&str> = Some("application/json");

#[tokio::test]
async fn the_sign_in_checks_its_request_before_the_bank() {
    let app = app(vec![]);
    let good = signin_body().to_string();
    let get = Request::builder()
        .method("GET")
        .uri("/api/console/session")
        .body(Body::empty())
        .unwrap();
    assert_eq!(call(&app, get).await.0, 405);
    for origin in [None, Some("null"), Some("http://host/ui")] {
        let (status, _, out) = call(&app, signin(origin, JS, good.as_bytes())).await;
        assert_eq!(
            (status, json_of(&out)),
            (403, json!({"error": "forbidden_origin"})),
            "{origin:?}"
        );
    }
    assert_eq!(
        call(
            &app,
            signin(Some(ORIGIN), Some("text/plain"), good.as_bytes())
        )
        .await
        .0,
        415
    );
    assert_eq!(
        app.signin_limiter.attempts(),
        0,
        "nothing counted before the reservation"
    );
    let mut bad = vec![];
    for (k, v) in [
        ("code", json!("not-a-code")),
        ("token_sha256", json!("AB".repeat(32))),
        ("label", json!(7)),
        ("label", json!("x".repeat(65))),
    ] {
        let mut b = signin_body();
        b[k] = v;
        bad.push(b.to_string().into_bytes());
    }
    let mut extra = signin_body();
    extra["extra"] = json!(1);
    bad.push(extra.to_string().into_bytes());
    bad.push(
        json!({"code": "ABCD-EFGH-JKMN", "token_sha256": sha(SESSION_TOKEN)})
            .to_string()
            .into_bytes(),
    );
    for b in [&b"[]"[..], b"not json", b"", b"{\"code\": \"\xff\"}"] {
        bad.push(b.to_vec());
    }
    for body in &bad {
        let (status, _, out) = call(&app, signin(Some(ORIGIN), JS, body)).await;
        assert_eq!(
            (status, out.as_slice()),
            (400, &br#"{"error": "signin_refused"}"#[..]),
            "{:?}",
            String::from_utf8_lossy(body)
        );
    }
    let large =
        json!({"code": "ABCD-EFGH-JKMN", "token_sha256": sha("x"), "label": "x".repeat(2000)})
            .to_string();
    assert_eq!(
        call(&app, signin(Some(ORIGIN), JS, large.as_bytes()))
            .await
            .0,
        413
    );
    // Every one of those counted against the budget: 20 a minute, then 429.
    let spent = app.signin_limiter.attempts();
    for _ in spent..20 {
        assert_eq!(call(&app, signin(Some(ORIGIN), JS, b"[]")).await.0, 400);
    }
    let (status, _, out) = call(&app, signin(Some(ORIGIN), JS, good.as_bytes())).await;
    assert_eq!(
        (status, out.as_slice()),
        (429, &br#"{"error": "rate_limited"}"#[..])
    );
}

#[tokio::test]
async fn sign_in_has_its_own_budget_and_a_database_failure_is_unavailable() {
    let app = app(vec![]);
    for _ in 0..25 {
        let r = Request::builder()
            .method("POST")
            .uri("/api/pair")
            .header("content-type", "application/json")
            .body(Body::from("{}"))
            .unwrap();
        call(&app, r).await;
    }
    let good = signin_body().to_string();
    let (status, _, out) = call(&app, signin(Some("http://[::1]:8765"), JS, good.as_bytes())).await;
    assert_eq!(
        (status, json_of(&out)),
        (503, json!({"error": "signin_unavailable"}))
    );
    assert_eq!(
        (
            app.signin_limiter.attempts(),
            app.signin_limiter.in_flight()
        ),
        (1, 0),
        "the outcome is unknown: kept"
    );
}

#[tokio::test]
async fn an_open_daemon_or_a_full_gate_refuses_sign_in() {
    let open = app_with("not a connection string", None, vec![], false);
    let good = signin_body().to_string();
    let (status, _, out) = call(&open, signin(Some(ORIGIN), JS, good.as_bytes())).await;
    assert_eq!(
        (status, out.as_slice()),
        (400, &br#"{"error": "signin_refused"}"#[..])
    );
    let app = app(vec![]);
    let _a = app.signin_limiter.enter().unwrap();
    let _b = app.signin_limiter.enter().unwrap();
    let (status, _, out) = call(&app, signin(Some(ORIGIN), JS, good.as_bytes())).await;
    assert_eq!(
        (status, out.as_slice()),
        (429, &br#"{"error": "rate_limited"}"#[..])
    );
    assert_eq!(
        app.signin_limiter.attempts(),
        0,
        "never reached the store: refunded"
    );
}

#[test]
fn the_issuer_check_at_redemption() {
    let env = crate::console_sessions::environment_issuers(&EnvTokens {
        single: Some(ENV_TOKEN.into()),
        map: vec![],
    });
    let store = PrincipalStore::new();
    assert_eq!(
        issuer_still_valid(&env, &store, "default", "environment", &sha(ENV_TOKEN)),
        Ok(true)
    );
    assert_eq!(
        issuer_still_valid(&env, &store, "alice", "environment", &sha(ENV_TOKEN)),
        Ok(false)
    );
    assert_eq!(
        issuer_still_valid(&env, &store, "laptop", "store", &sha(STORED_TOKEN)),
        Err(Unavailable),
        "never loaded: stale"
    );
    store.refresh(vec![], None, &[], Instant::now());
    assert_eq!(
        issuer_still_valid(&env, &store, "laptop", "store", &sha(STORED_TOKEN)),
        Ok(false)
    );
    assert_eq!(
        issuer_still_valid(&env, &store, "laptop", "passkey", "x"),
        Ok(false)
    );
}

#[tokio::test]
async fn health_says_which_sign_ins_the_daemon_offers() {
    for (token, view, link) in [
        (Some(ENV_TOKEN), true, true),
        (None, false, false),
        (Some(ENV_TOKEN), false, false),
    ] {
        let app = app_with("not a connection string", token, vec![], view);
        let request = Request::builder()
            .method("GET")
            .uri("/health")
            .body(Body::empty())
            .unwrap();
        let (_, _, out) = call(&app, request).await;
        assert_eq!(
            json_of(&out)["console_signin"],
            json!({"link": link, "passkey": false})
        );
    }
}

// -- the whole flow on the test bank ---------------------------------------------

/// Issue, redeem at another loopback alias, use at once, list (with
/// `current`), revoke, and the re-pair that signs a paired machine's
/// session out: on the test bank.
#[tokio::test]
async fn db_link_sign_in_list_and_revoke() {
    let Some(dsn) = std::env::var("PL_W1A_TEST_DSN")
        .ok()
        .filter(|d| !d.is_empty())
    else {
        eprintln!("PL_W1A_TEST_DSN unset: skipping database test");
        return;
    };
    let (c, t) = crate::storage::connect(
        &crate::storage::parse_dsn(&dsn).unwrap(),
        Duration::from_secs(10),
    )
    .await
    .unwrap();
    let present: bool = c
        .query_one(
            "SELECT to_regclass('public.console_sessions') IS NOT NULL",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    if !present {
        eprintln!("console_sessions absent (bank below v59): skipping");
        return;
    }
    let browser = "rs59-browser-token";
    c.execute(
        "DELETE FROM console_sessions WHERE token_hash = $1",
        &[&sha(browser)],
    )
    .await
    .unwrap();
    let app = app_with(&dsn, Some(ENV_TOKEN), vec![], true);
    let body = json!({"origin": "http://127.0.0.1:8765", "to": "board"}).to_string();
    let (status, _, out) = call(
        &app,
        bearer("POST", "/api/console/link", ENV_TOKEN, Some(&body)),
    )
    .await;
    let issued = json_of(&out);
    assert_eq!(status, 200, "{issued}");
    let keys: Vec<&String> = issued.as_object().unwrap().keys().collect();
    assert_eq!(
        keys,
        [
            "code",
            "url",
            "origin",
            "principal",
            "expires_at",
            "expires_in"
        ]
    );
    let code = issued["code"].as_str().unwrap().to_string();
    assert_eq!(
        issued["url"],
        json!(format!(
            "http://127.0.0.1:8765/ui/#/signin?code={code}&to=board"
        ))
    );
    assert_eq!(
        (
            issued["origin"].clone(),
            issued["principal"].clone(),
            issued["expires_in"].clone()
        ),
        (json!("http://127.0.0.1:8765"), json!("default"), json!(300))
    );
    let stored_origin: String = c
        .query_one(
            "SELECT origin FROM console_signin_codes WHERE code_hash = $1",
            &[&sha(&code.replace('-', ""))],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(stored_origin, "http://localhost:8765");
    let redeem = json!({"code": code.to_lowercase(), "token_sha256": sha(browser), "label": " Firefox  on Linux "}).to_string();
    // Another origin: refused, and the code is left unspent.
    let (status, _, _) = call(
        &app,
        signin(Some("http://127.0.0.1:9999"), JS, redeem.as_bytes()),
    )
    .await;
    assert_eq!(status, 400);
    // The [::1] alias of the same loopback origin redeems it.
    let (status, _, out) = call(
        &app,
        signin(Some("http://[::1]:8765"), JS, redeem.as_bytes()),
    )
    .await;
    let signed = json_of(&out);
    assert_eq!(status, 200, "{signed}");
    assert_eq!(signed["principal"], json!("default"));
    let sid = signed["session_id"].as_str().unwrap().to_string();
    assert_eq!(
        app.signin_limiter.attempts(),
        1,
        "the success refunded its own attempt only"
    );
    // Spent.
    assert_eq!(
        call(
            &app,
            signin(Some("http://[::1]:8765"), JS, redeem.as_bytes())
        )
        .await
        .0,
        400
    );
    // The new token works at once, with no refresh.
    assert_eq!(
        call(&app, bearer("GET", "/api/nope", browser, None))
            .await
            .0,
        404
    );
    let (status, _, out) = call(&app, bearer("GET", "/api/console/sessions", browser, None)).await;
    let listed = json_of(&out);
    assert_eq!(
        (
            status,
            listed["operator"].clone(),
            listed["available"].clone()
        ),
        (200, json!(true), json!(true))
    );
    let mine = listed["sessions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["session_id"] == json!(sid))
        .unwrap()
        .clone();
    assert_eq!(
        (
            mine["current"].clone(),
            mine["label"].clone(),
            mine["method"].clone()
        ),
        (json!(true), json!("Firefox on Linux"), json!("link"))
    );
    assert!(mine.get("token_hash").is_none() && mine.get("issuer").is_none());
    let rbody = json!({"session_id": sid}).to_string();
    let (status, _, out) = call(
        &app,
        bearer(
            "POST",
            "/api/console/sessions/revoke",
            ENV_TOKEN,
            Some(&rbody),
        ),
    )
    .await;
    assert_eq!((status, json_of(&out)), (200, json!({"revoked": true})));
    assert_eq!(
        call(&app, bearer("GET", "/api/nope", browser, None))
            .await
            .0,
        401,
        "signed out at once"
    );
    c.execute(
        "DELETE FROM console_sessions WHERE token_hash = $1",
        &[&sha(browser)],
    )
    .await
    .unwrap();
    drop(c);
    let _ = t.await;
}
