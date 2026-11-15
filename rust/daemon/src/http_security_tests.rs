//! Request admission of a daemon configured off loopback, in process: the
//! open (`PSEUDOLIFE_MCP_TRUST_BIND`) and closed (bearer) configurations of
//! the harness's `security-remote-open` and `security-remote-auth`, built
//! the way `main` builds them from the same environment, answering every
//! case of `tests/security_admission/cases.rs` through `handle`.
//!
//! The configured host reaches the daemon only through `DaemonEnv`, which
//! the startup bind guard and the listener read and the request gates do
//! not, so each table also runs with the host unset (the loopback goldens'
//! configuration) and must answer the same.

use super::*;

#[path = "../tests/security_admission/cases.rs"]
mod cases;

use cases::{CLOSED, CLOSED_ENV, Case, OPEN, OPEN_ENV};

/// The `App` `main` serves for this environment, with an unreachable bank
/// (as the harness scenarios run: the principal store never loads).
fn daemon_app(vars: &[(&str, &str)]) -> Arc<App> {
    let lookup = |name: &str| {
        vars.iter()
            .find(|(n, _)| *n == name)
            .map(|(_, v)| (*v).to_string())
    };
    let env = crate::config::DaemonEnv::from_env(&lookup).unwrap();
    // As `main`: either the config or the environment makes it read-only.
    let service_env_read_only = crate::config::Config::default().read_only || env.read_only;
    let tokens = auth::EnvTokens::from_lookup(&lookup);
    let auth_configured = tokens.configured();
    // What the startup guard decides for this environment: both
    // configurations start off loopback.
    assert!(
        auth_configured
            || env.trust_bind
            || ["127.0.0.1", "::1", "localhost"].contains(&env.host.as_str()),
        "this configuration would not start: {vars:?}"
    );
    let dsn = "postgresql://nobody:nothing@127.0.0.1:9/pl_security_admission_none".to_string();
    let env_principals: Vec<String> = tokens.map.iter().map(|(_, p)| p.clone()).collect();
    let store = Arc::new(auth::PrincipalStore::new_with_shadowed(env_principals));
    let activity = (auth_configured && !service_env_read_only)
        .then(|| Arc::new(crate::principal_activity::Recorder::new()));
    let sessions =
        auth_configured.then(|| Arc::new(crate::console_sessions::SessionSnapshot::new()));
    let service = Arc::new(Service::new(
        crate::config::Config::default(),
        env,
        PathBuf::from("unused"),
        dsn.clone(),
        auth_configured,
    ));
    let board = Arc::new(crate::board::Board::new(
        service.config.coordination.clone(),
        dsn,
        service.clone(),
        store.clone(),
    ));
    service.install(&board);
    Arc::new(
        App::new(service, tokens, store, None, board)
            .with_activity(activity)
            .with_sessions(sessions),
    )
}

fn without_host(vars: &[(&'static str, &'static str)]) -> Vec<(&'static str, &'static str)> {
    vars.iter()
        .copied()
        .filter(|(name, _)| *name != "PSEUDOLIFE_MCP_HOST")
        .collect()
}

/// One case through `handle`, as the harness's client sends it: its own
/// `Host: 127.0.0.1:8765` unless the case names a Host, and a
/// Content-Length with any body.
async fn answer(app: &Arc<App>, case: &Case) -> (u16, HeaderMap, Vec<u8>) {
    let mut request = Request::builder().method(case.method).uri(case.path);
    if !case
        .headers
        .iter()
        .any(|(name, _)| name.eq_ignore_ascii_case("host"))
    {
        request = request.header("host", "127.0.0.1:8765");
    }
    for (name, value) in case.headers {
        request = request.header(*name, HeaderValue::from_bytes(value).unwrap());
    }
    let body = case.body.bytes();
    if let Some(bytes) = &body {
        request = request.header("content-length", bytes.len().to_string());
    }
    let request = request
        .body(body.map_or_else(Body::empty, Body::from))
        .unwrap();
    let response = handle(State(app.clone()), request).await;
    let status = response.status().as_u16();
    let headers = response.headers().clone();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();
    (status, headers, bytes)
}

async fn run_table(vars: &[(&str, &str)], table: &[Case]) {
    let app = daemon_app(vars);
    let mut failures = Vec::new();
    for case in table {
        let (status, headers, body) = answer(&app, case).await;
        let headers: Vec<(String, String)> = headers
            .iter()
            .map(|(k, v)| {
                (
                    k.as_str().to_string(),
                    String::from_utf8_lossy(v.as_bytes()).into_owned(),
                )
            })
            .collect();
        failures.extend(cases::mismatches(case, status, &headers, &body));
    }
    assert!(failures.is_empty(), "{vars:?}:\n{}", failures.join("\n"));
}

/// `security-remote-open`: tokenless on 0.0.0.0 by the trust opt-in, /api
/// still serves loopback browsers only (Origin first, then Host, both
/// before method and body), pairing refuses any foreign Origin after its
/// method check, and an unparsable Origin is a 500, never an admission.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_trusted_wildcard_bind_still_admits_loopback_browsers_only() {
    run_table(OPEN_ENV, OPEN).await;
    run_table(&without_host(OPEN_ENV), OPEN).await;
}

/// `security-remote-auth`: a configured bearer on 0.0.0.0 gates /api, /mcp
/// and every other path before route, method and body; an unknown bearer
/// against an unavailable store is 503 (an always-200 hook answers empty);
/// the last Authorization counts; a skipped map entry inherits nothing; a
/// bearer lets a remote browser through.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_configured_bearer_still_gates_a_wildcard_bind() {
    run_table(CLOSED_ENV, CLOSED).await;
    run_table(&without_host(CLOSED_ENV), CLOSED).await;
}

/// The table's hand-written answers are the loopback goldens' recorded
/// answers, row for row, so neither can drift from the other unseen.
#[test]
fn the_table_matches_the_loopback_security_goldens() {
    for (golden, table) in [
        (include_str!("../harness/goldens/security-open.json"), OPEN),
        (
            include_str!("../harness/goldens/security-closed.json"),
            CLOSED,
        ),
    ] {
        let golden: serde_json::Value = serde_json::from_str(golden).unwrap();
        let responses = golden["responses"].as_array().unwrap();
        assert_eq!(responses.len(), table.len());
        for (recorded, case) in responses.iter().zip(table) {
            let e = &case.expect;
            assert_eq!(recorded["status"], e.status, "{}", case.name);
            let mut headers = serde_json::Map::new();
            if let Some(cache) = e.cache_control {
                headers.insert("cache-control".into(), cache.into());
            }
            headers.insert("content-type".into(), e.content_type.into());
            if e.nosniff {
                headers.insert("x-content-type-options".into(), "nosniff".into());
            }
            assert_eq!(
                recorded["headers"],
                serde_json::Value::Object(headers),
                "{}",
                case.name
            );
            if e.content_type.starts_with("application/json") {
                let body: serde_json::Value = serde_json::from_slice(e.body).unwrap();
                assert_eq!(recorded["json"], body, "{}", case.name);
            } else {
                assert_eq!(
                    recorded["bytes"],
                    String::from_utf8(e.body.to_vec()).unwrap(),
                    "{}",
                    case.name
                );
            }
        }
    }
}
