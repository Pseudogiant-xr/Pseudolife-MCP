//! Console sign-in with the maintainer passkey over HTTP, after
//! tests/test_console_passkey.py and tests/test_console_passkey_pg.py:
//! `/health`'s capability, every refusal the challenge route, the passkey
//! body of `/api/console/session` and `/api/console/passkey-bind` give
//! before the bank, and, on the test bank (`PL_W1A_TEST_DSN`), the whole
//! flow with a software Ed25519 authenticator: the challenge binds the
//! browser's token, the assertion signs it in at once, a replay is refused,
//! a key with no sign-in principal or one that no longer resolves is
//! refused with its recovery, a bind completes only for the principal it
//! was issued to, and a revoked key signs its browsers out at once.
//!
//! These tests use only the HTTP surface and the session view's read API,
//! so they run against a daemon without the passkey sign-in (watched RED).

use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, Request};
use base64::Engine;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::auth::{EnvTokens, PrincipalStore, StoredPrincipal};
use crate::console_sessions::{ConsoleSession, METHOD_PASSKEY, SESSION_MAX_S, SessionSnapshot};
use crate::http::{App, handle};

const ENV_TOKEN: &str = "fixture-token";
const ALICE_TOKEN: &str = "fixture-alice-token";
const FRIEND_TOKEN: &str = "fixture-friend-token";
const RP_ID: &str = "console.example.com";
const ORIGIN: &str = "https://console.example.com:8443";
const SECRET_HEX: &str = "4242424242424242424242424242424242424242424242424242424242424242";
const UNREACHABLE: &str = "postgresql://unused@127.0.0.1:1/pl_cf_g2e_unused";

fn sha(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

fn b64(bytes: &[u8]) -> String {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

/// A daemon with the singular token (`default`), `alice` (an owner) and
/// `friend` (someone else's, `coordination.not_owner_principals`) in the
/// token map, all admitted to the board; passkeys set up at
/// [`ORIGIN`] when `passkeys`; a loaded session view holding `rows` when
/// `view`; the bank at `dsn`.
fn app(dsn: &str, passkeys: bool, rows: Vec<ConsoleSession>, view: bool) -> Arc<App> {
    let mut config = crate::config::Config::default();
    let c = &mut config.coordination;
    c.enabled = true;
    c.allowed_principals = vec!["default".into(), "alice".into(), "friend".into()];
    c.not_owner_principals = vec!["friend".into()];
    if passkeys {
        c.maintainer_rp_id = RP_ID.into();
        c.maintainer_origin = ORIGIN.into();
    }
    let env = crate::config::DaemonEnv::from_env(&|_| None).unwrap();
    let service = Arc::new(crate::service::Service::new(
        config,
        env,
        std::path::PathBuf::from("unused"),
        dsn.into(),
        true,
    ));
    let store = Arc::new(PrincipalStore::new());
    store.refresh(
        vec![StoredPrincipal {
            principal: "laptop".into(),
            token_hash: Some(sha("fixture-stored-token")),
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
        service.config.coordination.clone(),
        dsn.into(),
        service.clone(),
        store.clone(),
    ));
    let sessions = view.then(|| {
        let s = Arc::new(SessionSnapshot::new());
        s.finish_refresh(s.begin_refresh(), rows, vec![]);
        s
    });
    Arc::new(
        App::new(
            service,
            EnvTokens {
                single: Some(ENV_TOKEN.into()),
                map: vec![
                    (ALICE_TOKEN.into(), "alice".into()),
                    (FRIEND_TOKEN.into(), "friend".into()),
                ],
            },
            store,
            None,
            board,
        )
        .with_sessions(sessions),
    )
}

async fn call(app: &Arc<App>, request: Request<Body>) -> (u16, HeaderMap, Value) {
    // A task of its own, as the server runs each request: the maintainer
    // routes' debug-build futures would overflow a test thread's stack.
    let response = tokio::time::timeout(
        Duration::from_secs(60),
        tokio::spawn(handle(State(app.clone()), request)),
    )
    .await
    .expect("answered")
    .expect("the request task");
    let status = response.status().as_u16();
    let headers = response.headers().clone();
    let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
        .await
        .unwrap();
    (
        status,
        headers,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// A browser's pre-auth POST: `origin` (or none), `content_type`, `body`.
fn browser(path: &str, origin: Option<&str>, content_type: &str, body: &str) -> Request<Body> {
    let mut r = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", content_type);
    if let Some(o) = origin {
        r = r.header("origin", o);
    }
    r.body(Body::from(body.to_string())).unwrap()
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

fn challenge_body(token: &str) -> String {
    json!({"token_sha256": sha(token), "label": "Firefox  on\tLinux"}).to_string()
}

const CHALLENGE: &str = "/api/console/passkey-challenge";
const SESSION: &str = "/api/console/session";
const BIND: &str = "/api/console/passkey-bind";

fn unavailable_hint() -> Value {
    json!(
        "this passkey has no Console sign-in, or the principal it signs in as no longer \
         exists. On the daemon's host run `pseudolife-mcp console`, open Settings, Your \
         passkeys, and choose \"Use this passkey to sign in\"; after that this passkey signs \
         the Console in"
    )
}

/// The `db_` tests share the bank's key table, so they run one at a time.
static DB_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

const NO_KEYS_HINT: &str = "no passkey can sign yet: none is set up (`pseudolife-mcp maintainer \
     setup` on the daemon's host), the first one still waits for the host's `pseudolife-mcp \
     maintainer confirm <prefix>`, or a newly added one is still in its 24-hour wait. Meanwhile \
     sign in with `pseudolife-mcp console`";

// -- /health ---------------------------------------------------------------------

#[tokio::test]
async fn health_offers_the_passkey_where_passkeys_are_set_up() {
    let (_, _, out) = call(
        &app(UNREACHABLE, true, vec![], true),
        Request::get("/health").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(
        out["console_signin"],
        json!({"link": true, "passkey": true})
    );
    // The near misses: no passkey origin, or no link sign-in at all.
    let (_, _, out) = call(
        &app(UNREACHABLE, false, vec![], true),
        Request::get("/health").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(
        out["console_signin"],
        json!({"link": true, "passkey": false})
    );
    let (_, _, out) = call(
        &app(UNREACHABLE, true, vec![], false),
        Request::get("/health").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(
        out["console_signin"],
        json!({"link": false, "passkey": false})
    );
}

// -- POST /api/console/passkey-challenge, before the bank ---------------------------

#[tokio::test]
async fn the_challenge_is_pre_auth_and_refuses_what_a_browser_would_not_send() {
    let app = app(UNREACHABLE, true, vec![], true);
    let good = challenge_body("browser-a");
    let get = Request::get(CHALLENGE)
        .header("origin", ORIGIN)
        .body(Body::empty())
        .unwrap();
    let cases: Vec<(&str, Request<Body>, u16, Value)> = vec![
        ("GET", get, 405, json!({"error": "method_not_allowed"})),
        (
            "no Origin",
            browser(CHALLENGE, None, "application/json", &good),
            403,
            json!({"error": "forbidden_origin"}),
        ),
        (
            "an Origin with a path",
            browser(
                CHALLENGE,
                Some("https://console.example.com:8443/ui"),
                "application/json",
                &good,
            ),
            403,
            json!({"error": "forbidden_origin"}),
        ),
        (
            "text/plain",
            browser(CHALLENGE, Some(ORIGIN), "text/plain", &good),
            415,
            json!({"error": "content_type_must_be_application_json"}),
        ),
        (
            "a key short",
            browser(
                CHALLENGE,
                Some(ORIGIN),
                "application/json",
                &json!({"token_sha256": sha("x")}).to_string(),
            ),
            400,
            json!({"error": "invalid_request"}),
        ),
        (
            "an extra key",
            browser(
                CHALLENGE,
                Some(ORIGIN),
                "application/json",
                &json!({"token_sha256": sha("x"), "label": "l", "principal": "default"})
                    .to_string(),
            ),
            400,
            json!({"error": "invalid_request"}),
        ),
        (
            "an upper-case hash",
            browser(
                CHALLENGE,
                Some(ORIGIN),
                "application/json",
                &json!({"token_sha256": sha("x").to_uppercase(), "label": "l"}).to_string(),
            ),
            400,
            json!({"error": "invalid_request"}),
        ),
        (
            "over 1 KiB",
            browser(
                CHALLENGE,
                Some(ORIGIN),
                "application/json",
                &json!({"token_sha256": sha("x"), "label": "l".repeat(1100)}).to_string(),
            ),
            413,
            json!({"error": "request_too_large"}),
        ),
        (
            "a cross-origin request",
            browser(
                CHALLENGE,
                Some("https://evil.example.com"),
                "application/json",
                &good,
            ),
            403,
            json!({"error": "forbidden_origin"}),
        ),
        (
            "the passkey host on another port",
            browser(
                CHALLENGE,
                Some("https://console.example.com"),
                "application/json",
                &good,
            ),
            403,
            json!({"error": "forbidden_origin"}),
        ),
    ];
    for (name, request, status, body) in cases {
        let (got, headers, out) = call(&app, request).await;
        assert_eq!((got, out), (status, body), "{name}");
        // No CORS answer, ever.
        assert!(
            headers.get("access-control-allow-origin").is_none(),
            "{name}"
        );
    }
    // At the passkey origin the request reaches the bank: an unreachable one
    // is 503, never a 401 (the Console would read that as "sign in again").
    let (got, _, out) = call(
        &app,
        browser(CHALLENGE, Some(ORIGIN), "application/json", &good),
    )
    .await;
    assert_eq!((got, out), (503, json!({"error": "signin_unavailable"})));
}

#[tokio::test]
async fn no_challenge_without_passkeys_or_a_session_table() {
    for (passkeys, view) in [(false, true), (true, false)] {
        let app = app(UNREACHABLE, passkeys, vec![], view);
        let (status, _, out) = call(
            &app,
            browser(
                CHALLENGE,
                Some(ORIGIN),
                "application/json",
                &challenge_body("b"),
            ),
        )
        .await;
        assert_eq!(
            (status, out),
            (
                409,
                json!({"error": "passkey_signin_unavailable", "hint": NO_KEYS_HINT})
            ),
            "passkeys={passkeys} view={view}"
        );
    }
}

/// Issued challenges are capped at 30 a minute even when every one is
/// refunded from the failure budget.
#[tokio::test]
async fn issued_challenges_are_capped_per_minute() {
    let app = app(UNREACHABLE, false, vec![], true);
    for i in 0..30 {
        let (status, _, _) = call(
            &app,
            browser(
                CHALLENGE,
                Some(ORIGIN),
                "application/json",
                &challenge_body("b"),
            ),
        )
        .await;
        assert_eq!(status, 409, "request {i}");
    }
    let (status, _, out) = call(
        &app,
        browser(
            CHALLENGE,
            Some(ORIGIN),
            "application/json",
            &challenge_body("b"),
        ),
    )
    .await;
    assert_eq!((status, out), (429, json!({"error": "rate_limited"})));
}

// -- the passkey body of POST /api/console/session, before the bank ------------------

fn passkey_body(pad: usize) -> String {
    json!({"payload": format!("{{\"x\":\"{}\"}}", "p".repeat(pad)), "mac": "0".repeat(64),
        "assertion": {"id": "AAAA", "response": {}}})
    .to_string()
}

#[tokio::test]
async fn a_passkey_body_elsewhere_than_the_passkey_origin_is_refused() {
    let refused = json!({"error": "signin_refused"});
    for (passkeys, origin) in [
        (true, "https://evil.example.com"),
        (true, "http://127.0.0.1:8765"),
        (false, ORIGIN),
    ] {
        let app = app(UNREACHABLE, passkeys, vec![], true);
        let (status, _, out) = call(
            &app,
            browser(SESSION, Some(origin), "application/json", &passkey_body(10)),
        )
        .await;
        assert_eq!((status, out), (400, refused.clone()), "{passkeys} {origin}");
    }
}

/// A code body keeps its 1 KiB limit; a passkey body gets 8 KiB, so an
/// RS256 assertion fits.
#[tokio::test]
async fn a_code_body_keeps_its_one_kib_limit_and_a_passkey_body_gets_eight() {
    let app = app(UNREACHABLE, true, vec![], true);
    let code = json!({"code": "ABCD-EFGH-JKMN", "token_sha256": sha("b"),
        "label": "l".repeat(1100)})
    .to_string();
    let (status, _, _) = call(
        &app,
        browser(SESSION, Some(ORIGIN), "application/json", &code),
    )
    .await;
    assert_eq!(status, 413, "a code body over 1 KiB");
    // About 4 KiB: past the code limit, inside the passkey one, so it reaches
    // the bank (unreachable here: 503, refunded).
    let (status, _, out) = call(
        &app,
        browser(
            SESSION,
            Some(ORIGIN),
            "application/json",
            &passkey_body(4000),
        ),
    )
    .await;
    assert_eq!((status, out), (503, json!({"error": "signin_unavailable"})));
    let (status, _, _) = call(
        &app,
        browser(
            SESSION,
            Some(ORIGIN),
            "application/json",
            &passkey_body(9000),
        ),
    )
    .await;
    assert_eq!(status, 413, "a passkey body over 8 KiB");
}

// -- POST /api/console/passkey-bind, before the bank ---------------------------------

#[tokio::test]
async fn binding_is_for_owner_principals_through_the_bearer_gate() {
    let app = app(UNREACHABLE, true, vec![], true);
    let (status, _, out) = call(&app, bearer("POST", BIND, FRIEND_TOKEN, Some("{}"))).await;
    assert_eq!(
        (status, out),
        (
            403,
            json!({"reason": "not_owner", "error": "principal_not_allowed"})
        ),
        "someone else's principal"
    );
    let (status, _, out) = call(&app, bearer("POST", BIND, ENV_TOKEN, Some("{\"x\": 1}"))).await;
    assert_eq!((status, out), (400, json!({"error": "invalid_request"})));
    let (status, _, out) = call(
        &app,
        bearer(
            "POST",
            BIND,
            ENV_TOKEN,
            Some(r#"{"payload": "p", "mac": "m"}"#),
        ),
    )
    .await;
    assert_eq!((status, out), (400, json!({"error": "invalid_request"})));
    let (status, _, out) = call(&app, bearer("GET", BIND, ENV_TOKEN, None)).await;
    assert_eq!(
        (status, out),
        (405, json!({"error": "method_not_allowed", "path": BIND}))
    );
    // Unauthenticated: the bearer gate, not the route.
    let (status, _, _) = call(&app, browser(BIND, Some(ORIGIN), "application/json", "{}")).await;
    assert_eq!(status, 401);
    // No session table: sign-in is unavailable before any gate.
    let bare = self::app(UNREACHABLE, true, vec![], false);
    let (status, _, out) = call(&bare, bearer("POST", BIND, ENV_TOKEN, Some("{}"))).await;
    assert_eq!(
        (status, out["error"].clone()),
        (409, json!("signin_unavailable"))
    );
}

// -- the session view ------------------------------------------------------------------

fn passkey_row(token: &str, principal: &str, issuer: &str, sid: &str) -> ConsoleSession {
    let created = crate::console_sessions::wall_now() - 60.0;
    ConsoleSession {
        session_id: sid.into(),
        token_hash: sha(token),
        principal: principal.into(),
        source: "environment".into(),
        issuer: issuer.into(),
        method: METHOD_PASSKEY.into(),
        label: "Firefox on Linux".into(),
        created_at: created,
        last_used_at: None,
        expires_at: created + SESSION_MAX_S,
    }
}

/// A passkey sign-in in the 10 s after its key's confirm must not bounce:
/// the sign-in itself counts the key active until a refresh that started
/// after it.
#[tokio::test]
async fn a_passkey_sign_in_counts_its_key_active_at_once() {
    let view = SessionSnapshot::new();
    let stale = view.begin_refresh();
    view.finish_refresh(view.begin_refresh(), vec![], vec![]);
    view.add(passkey_row("t", "default", "KEY-A", "p1"));
    assert!(view.has_passkey("KEY-A"));
    // A refresh whose read began before the sign-in keeps it.
    view.finish_refresh(stale, vec![], vec![]);
    assert!(view.has_passkey("KEY-A"));
    // One that began after reads the table, which is the truth from then.
    view.finish_refresh(view.begin_refresh(), vec![], vec![]);
    assert!(!view.has_passkey("KEY-A"));
}

// -- the whole flow on the test bank ----------------------------------------------

/// A software Ed25519 authenticator (`tests/maintainer_authenticator.py`'s
/// `SoftAuthenticator(-8)`), never used outside tests.
struct Soft {
    key: aws_lc_rs::signature::Ed25519KeyPair,
    credential_id: Vec<u8>,
    sign_count: u32,
}

impl Soft {
    fn new(name: &str, sign_count: u32) -> Self {
        let seed = Sha256::digest(format!("rust-passkey-fixture:{name}").as_bytes());
        Soft {
            key: aws_lc_rs::signature::Ed25519KeyPair::from_seed_unchecked(&seed).unwrap(),
            credential_id: seed[..16].to_vec(),
            sign_count,
        }
    }

    fn id(&self) -> String {
        b64(&self.credential_id)
    }

    /// The COSE OKP key `{1: 1, 3: -8, -1: 6, -2: x}`.
    fn cose_key(&self) -> Vec<u8> {
        use aws_lc_rs::signature::KeyPair;
        let mut out = vec![0xa4, 0x01, 0x01, 0x03, 0x27, 0x20, 0x06, 0x21, 0x58, 0x20];
        out.extend_from_slice(self.key.public_key().as_ref());
        out
    }

    /// An assertion over `payload`'s challenge, its counter one past the
    /// last, from `origin`.
    fn assertion(&mut self, payload: &str, origin: &str) -> Value {
        self.sign_count += 1;
        let challenge = Sha256::digest(payload.as_bytes());
        let client = json!({"type": "webauthn.get", "challenge": b64(&challenge),
            "origin": origin, "crossOrigin": false})
        .to_string();
        let mut auth = Sha256::digest(RP_ID.as_bytes()).to_vec();
        auth.push(0x05);
        auth.extend_from_slice(&self.sign_count.to_be_bytes());
        let mut signed = auth.clone();
        signed.extend_from_slice(&Sha256::digest(client.as_bytes()));
        let signature = self.key.sign(&signed);
        json!({"id": self.id(), "rawId": self.id(), "type": "public-key",
            "response": {"clientDataJSON": b64(client.as_bytes()),
                "authenticatorData": b64(&auth), "signature": b64(signature.as_ref())}})
    }
}

async fn test_bank() -> Option<(String, tokio_postgres::Client, tokio::task::JoinHandle<()>)> {
    let dsn = std::env::var("PL_W1A_TEST_DSN")
        .ok()
        .filter(|d| !d.is_empty());
    let Some(dsn) = dsn else {
        eprintln!("PL_W1A_TEST_DSN unset: skipping database test");
        return None;
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
        return None;
    }
    Some((dsn, c, t))
}

/// Seed `soft` as the only active key, past quarantine, signing in as
/// `signs_in_as`, and the challenge secret.
async fn seed_key(c: &tokio_postgres::Client, soft: &Soft, signs_in_as: Option<&str>) {
    c.execute(
        "UPDATE maintainer_passkeys SET state = 'revoked' WHERE state <> 'revoked'",
        &[],
    )
    .await
    .unwrap();
    c.execute(
        "INSERT INTO meta (key, value) VALUES ('maintainer_secret_v1', $1) \
         ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        &[&json!(SECRET_HEX)],
    )
    .await
    .unwrap();
    c.execute(
        "DELETE FROM console_sessions WHERE issuer = $1",
        &[&soft.id()],
    )
    .await
    .unwrap();
    c.execute(
        "DELETE FROM maintainer_passkeys WHERE credential_id = $1",
        &[&soft.id()],
    )
    .await
    .unwrap();
    let now = crate::console_sessions::wall_now();
    c.execute(
        "INSERT INTO maintainer_passkeys (credential_id, public_key, alg, sign_count, label, \
         enrolled_by, state, active_from, created_at, signs_in_as) \
         VALUES ($1, $2, -8, $3, 'Fixture key', 'bootstrap', 'active', $4, $5, $6)",
        &[
            &soft.id(),
            &soft.cose_key(),
            &i64::from(soft.sign_count),
            &(now - 10.0),
            &(now - 100.0),
            &signs_in_as,
        ],
    )
    .await
    .unwrap();
}

async fn challenge(app: &Arc<App>, token: &str) -> (u16, Value) {
    let (status, _, out) = call(
        app,
        browser(
            CHALLENGE,
            Some(ORIGIN),
            "application/json",
            &challenge_body(token),
        ),
    )
    .await;
    (status, out)
}

async fn sign_in(app: &Arc<App>, body: &Value) -> (u16, Value) {
    let (status, _, out) = call(
        app,
        browser(SESSION, Some(ORIGIN), "application/json", &body.to_string()),
    )
    .await;
    (status, out)
}

fn signed(issued: &Value, soft: &mut Soft, origin: &str) -> Value {
    let payload = issued["payload"].as_str().unwrap();
    json!({"payload": payload, "mac": issued["mac"], "assertion": soft.assertion(payload, origin)})
}

#[tokio::test]
async fn db_a_passkey_signs_the_browser_in_and_a_revoked_key_signs_it_out() {
    let _serial = DB_LOCK.lock().await;
    let Some((dsn, c, _t)) = test_bank().await else {
        return;
    };
    let mut soft = Soft::new("flow", 1);
    seed_key(&c, &soft, Some("default")).await;
    let browser_token = "rs59-passkey-browser-flow";
    c.execute(
        "DELETE FROM console_sessions WHERE token_hash = $1",
        &[&sha(browser_token)],
    )
    .await
    .unwrap();
    let app = app(&dsn, true, vec![], true);

    // The challenge binds the browser's token and lists the key.
    let (status, issued) = challenge(&app, browser_token).await;
    assert_eq!(status, 200, "{issued}");
    let keys: Vec<&String> = issued.as_object().unwrap().keys().collect();
    assert_eq!(keys, ["payload", "mac", "publicKey"]);
    let fields: Value = serde_json::from_str(issued["payload"].as_str().unwrap()).unwrap();
    assert_eq!(fields["purpose"], "sign-in");
    assert_eq!(fields["token_sha256"], json!(sha(browser_token)));
    assert_eq!(fields["label"], "Firefox on Linux");
    let mac = {
        use hmac::Mac;
        let mut m =
            hmac::Hmac::<Sha256>::new_from_slice(&hex::decode(SECRET_HEX).unwrap()).unwrap();
        m.update(issued["payload"].as_str().unwrap().as_bytes());
        hex::encode(m.finalize().into_bytes())
    };
    assert_eq!(issued["mac"], json!(mac));
    assert_eq!(issued["publicKey"]["rpId"], RP_ID);
    assert_eq!(
        issued["publicKey"]["allowCredentials"],
        json!([{"type": "public-key", "id": soft.id()}])
    );

    // A wrong-origin assertion is refused and spends nothing.
    let wrong = signed(&issued, &mut soft, "https://evil.example.com");
    let (status, out) = sign_in(&app, &wrong).await;
    assert_eq!((status, out), (400, json!({"error": "signin_refused"})));

    let body = signed(&issued, &mut soft, ORIGIN);
    let (status, out) = sign_in(&app, &body).await;
    assert_eq!(status, 200, "{out}");
    assert_eq!(out["principal"], "default");
    let keys: Vec<&String> = out.as_object().unwrap().keys().collect();
    assert_eq!(keys, ["principal", "session_id", "expires_at"]);
    let row = c
        .query_one(
            "SELECT principal, source, issuer, method, label FROM console_sessions \
             WHERE token_hash = $1",
            &[&sha(browser_token)],
        )
        .await
        .unwrap();
    assert_eq!(
        (
            row.get::<_, String>(0),
            row.get::<_, String>(1),
            row.get::<_, String>(2),
            row.get::<_, String>(3),
            row.get::<_, String>(4)
        ),
        (
            "default".into(),
            "environment".into(),
            soft.id(),
            "passkey".into(),
            "Firefox on Linux".into()
        )
    );
    // Usable at once, without waiting for a refresh.
    let (status, _, _) = call(&app, bearer("GET", "/api/nope", browser_token, None)).await;
    assert_eq!(status, 404);

    // A replay of the same assertion: the nonce is spent.
    let (status, out) = sign_in(&app, &body).await;
    assert_eq!((status, out), (400, json!({"error": "signin_refused"})));
    // A second challenge for a token already in use: refused, and that
    // refusal spends nothing (its own assertion still fails the same way).
    let (_, again) = challenge(&app, browser_token).await;
    let reuse = signed(&again, &mut soft, ORIGIN);
    let (status, out) = sign_in(&app, &reuse).await;
    assert_eq!((status, out), (400, json!({"error": "signin_refused"})));

    // The key revokes itself in the Console: its browser is signed out at
    // once, and its row is revoked in the same transaction.
    let (status, _, ch) = call(
        &app,
        bearer(
            "POST",
            "/api/maintainer/challenge",
            ENV_TOKEN,
            Some(&json!({"purpose": "revoke-self", "credential_id": soft.id()}).to_string()),
        ),
    )
    .await;
    assert_eq!(status, 200, "{ch}");
    let revoke = signed(&ch, &mut soft, ORIGIN);
    let (status, _, out) = call(
        &app,
        bearer(
            "POST",
            "/api/maintainer/revoke",
            ENV_TOKEN,
            Some(&revoke.to_string()),
        ),
    )
    .await;
    assert_eq!((status, out), (200, json!({"state": "revoked"})));
    let (status, _, _) = call(&app, bearer("GET", "/api/nope", browser_token, None)).await;
    assert_eq!(status, 401, "a revoked key's browser is signed out at once");
    let revoked: Option<f64> = c
        .query_one(
            "SELECT revoked_at FROM console_sessions WHERE token_hash = $1",
            &[&sha(browser_token)],
        )
        .await
        .unwrap()
        .get(0);
    assert!(revoked.is_some());
    // A revoked key gets no challenge.
    let (status, out) = challenge(&app, "rs59-passkey-browser-after").await;
    assert_eq!(status, 409, "{out}");
}

#[tokio::test]
async fn db_a_key_with_no_sign_in_principal_names_the_recovery() {
    let _serial = DB_LOCK.lock().await;
    let Some((dsn, c, _t)) = test_bank().await else {
        return;
    };
    let mut soft = Soft::new("unbound", 1);
    seed_key(&c, &soft, None).await;
    let app = app(&dsn, true, vec![], true);
    let (status, out) = challenge(&app, "rs59-passkey-browser-unbound").await;
    assert_eq!(
        (status, out),
        (
            409,
            json!({"error": "passkey_signin_unavailable", "hint": unavailable_hint()})
        )
    );
    // Bound to a principal that no longer resolves: the challenge is
    // issued, the completion refused with the same recovery, the browser not
    // signed in.
    c.execute(
        "UPDATE maintainer_passkeys SET signs_in_as = 'ghost' WHERE credential_id = $1",
        &[&soft.id()],
    )
    .await
    .unwrap();
    let (status, issued) = challenge(&app, "rs59-passkey-browser-ghost").await;
    assert_eq!(status, 200, "{issued}");
    let body = signed(&issued, &mut soft, ORIGIN);
    let (status, out) = sign_in(&app, &body).await;
    assert_eq!(
        (status, out),
        (
            409,
            json!({"error": "passkey_signin_unavailable", "hint": unavailable_hint()})
        )
    );
    let (status, _, _) = call(
        &app,
        bearer("GET", "/api/nope", "rs59-passkey-browser-ghost", None),
    )
    .await;
    assert_eq!(status, 401);
}

#[tokio::test]
async fn db_only_the_principal_a_bind_was_issued_for_can_complete_it() {
    let _serial = DB_LOCK.lock().await;
    let Some((dsn, c, _t)) = test_bank().await else {
        return;
    };
    let mut soft = Soft::new("bind", 1);
    seed_key(&c, &soft, Some("alice")).await;
    // A passkey session the key gave alice before the rebind.
    let alice_browser = "rs59-passkey-browser-alice";
    c.execute(
        "DELETE FROM console_sessions WHERE token_hash = $1",
        &[&sha(alice_browser)],
    )
    .await
    .unwrap();
    let app = app(&dsn, true, vec![], true);
    let (_, issued) = challenge(&app, alice_browser).await;
    let (status, out) = sign_in(&app, &signed(&issued, &mut soft, ORIGIN)).await;
    assert_eq!((status, out["principal"].clone()), (200, json!("alice")));

    // `default` asks to bind the key; alice cannot complete that challenge.
    let (status, _, bind) = call(&app, bearer("POST", BIND, ENV_TOKEN, Some("{}"))).await;
    assert_eq!(status, 200, "{bind}");
    let fields: Value = serde_json::from_str(bind["payload"].as_str().unwrap()).unwrap();
    assert_eq!(
        (fields["purpose"].clone(), fields["principal"].clone()),
        (json!("bind-sign-in"), json!("default"))
    );
    let theirs = signed(&bind, &mut soft, ORIGIN);
    let (status, _, out) = call(
        &app,
        bearer("POST", BIND, ALICE_TOKEN, Some(&theirs.to_string())),
    )
    .await;
    assert_eq!((status, out), (403, json!({"error": "assertion_invalid"})));
    // The refusal rolled back: the same assertion completes for `default`.
    let (status, _, out) = call(
        &app,
        bearer("POST", BIND, ENV_TOKEN, Some(&theirs.to_string())),
    )
    .await;
    assert_eq!(
        (status, out),
        (
            200,
            json!({"credential_id": soft.id(), "signs_in_as": "default"})
        )
    );
    // Rebinding signed the key's browser under alice out at once.
    let (status, _, _) = call(&app, bearer("GET", "/api/nope", alice_browser, None)).await;
    assert_eq!(status, 401);
    let principal: Option<String> = c
        .query_one(
            "SELECT signs_in_as FROM maintainer_passkeys WHERE credential_id = $1",
            &[&soft.id()],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(principal.as_deref(), Some("default"));
    let change: Value = c
        .query_one(
            "SELECT payload::jsonb FROM coordination_events WHERE event = 'maintainer_key' \
             ORDER BY seq DESC LIMIT 1",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(change["change"], "bind-sign-in");
    assert_eq!(change["credential_id"], json!(soft.id()));
}
