//! `document_ingest` through the bearer gate and the MCP transport, for the
//! operator's environment token and for an invited (stored) principal: a
//! file outside the documents directories is refused for both, the same
//! way whether or not it exists, before the bank is needed (this daemon has
//! none reachable). The resolution rules themselves are
//! `document_roots_tests.rs`'s.

use crate::http::{App, handle};
use axum::body::Body;
use axum::extract::State;
use axum::http::{Request, StatusCode};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

const OPERATOR_TOKEN: &str = "operator-fixture-token";
const INVITED_TOKEN: &str = "invited-fixture-token";
const ACCEPT: &str = "application/json, text/event-stream";
const INIT: &str = r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"mcp","version":"0.1.0"}}}"#;
const INITIALIZED: &str = r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#;

/// A scratch directory holding `root/` and `outside/secret.txt`, and a
/// config.yaml naming `root` (or nothing) as the documents directory.
struct Scratch(PathBuf);

impl Scratch {
    fn new(tag: &str) -> Scratch {
        let base = std::env::temp_dir().join(format!(
            "pl-ingest-route-{tag}-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(base.join("root")).unwrap();
        std::fs::create_dir_all(base.join("outside")).unwrap();
        std::fs::write(base.join("outside").join("secret.txt"), b"not a document").unwrap();
        Scratch(base)
    }

    fn path(&self, rel: &str) -> String {
        let mut p = self.0.clone();
        for part in rel.split('/') {
            p.push(part);
        }
        p.to_str().unwrap().to_string()
    }

    /// The daemon configuration with `roots` under `memory.reference`.
    fn config(&self, roots: &[String]) -> crate::config::Config {
        let file = self.0.join("config.yaml");
        let mut text = String::from("memory:\n  reference:\n    chunk_size: 512\n");
        if !roots.is_empty() {
            text.push_str("    ingest_roots:\n");
            for root in roots {
                // A JSON string is a YAML double-quoted scalar.
                text.push_str(&format!(
                    "      - {}\n",
                    serde_json::to_string(root).unwrap()
                ));
            }
        }
        std::fs::write(&file, text).unwrap();
        crate::config::load(&file).unwrap()
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn sha(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

/// A daemon with the operator's environment token, one invited principal
/// in its store, and no reachable bank.
fn app(config: crate::config::Config) -> Arc<App> {
    let dsn = "postgresql://unused@127.0.0.1:1/pl_cf_w128_unused";
    let service = Arc::new(crate::service::Service::new(
        config,
        crate::config::DaemonEnv::from_env(&|_| None).unwrap(),
        PathBuf::from("unused"),
        dsn.into(),
        true,
    ));
    let store = Arc::new(crate::auth::PrincipalStore::new());
    store.refresh(vec![], None, &[], Instant::now());
    store.add(crate::auth::StoredPrincipal {
        principal: "invited-laptop".into(),
        token_hash: Some(sha(INVITED_TOKEN)),
        tier: None,
        board: true,
        revoked: false,
        owned: false,
    });
    let board = Arc::new(crate::board::Board::new(
        service.config.coordination.clone(),
        dsn.into(),
        service.clone(),
        store.clone(),
    ));
    Arc::new(App::new(
        service,
        crate::auth::EnvTokens {
            single: Some(OPERATOR_TOKEN.into()),
            map: vec![],
        },
        store,
        None,
        board,
    ))
}

async fn send(
    app: &Arc<App>,
    headers: &[(&str, &str)],
    body: &str,
) -> (StatusCode, Option<String>, String) {
    let mut request = Request::builder().method("POST").uri("/mcp");
    for (name, value) in headers {
        request = request.header(*name, *value);
    }
    let request = request.body(Body::from(body.to_string())).unwrap();
    let response = handle(State(app.clone()), request).await;
    let status = response.status();
    let sid = response
        .headers()
        .get("mcp-session-id")
        .map(|v| v.to_str().unwrap().to_string());
    let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
        .await
        .unwrap();
    (status, sid, String::from_utf8_lossy(&bytes).into_owned())
}

/// The JSON-RPC message in a response body (plain JSON or one SSE event).
fn message(body: &str) -> serde_json::Value {
    let text = body
        .lines()
        .find_map(|l| l.strip_prefix("data: "))
        .unwrap_or(body);
    serde_json::from_str(text).unwrap_or_else(|e| panic!("{e}: {body}"))
}

/// `document_ingest(path)` as the holder of `token`: the tool's error
/// payload (its result's structured content).
async fn ingest(app: &Arc<App>, token: &str, path: &str) -> serde_json::Value {
    let bearer = format!("Bearer {token}");
    let base = [
        ("host", "127.0.0.1:8765"),
        ("content-type", "application/json"),
        ("accept", ACCEPT),
        ("authorization", bearer.as_str()),
    ];
    let (status, sid, body) = send(app, &base, INIT).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let sid = sid.expect("a session");
    let mut headers = base.to_vec();
    headers.push(("mcp-session-id", sid.as_str()));
    headers.push(("mcp-protocol-version", "2025-11-25"));
    let (status, _, body) = send(app, &headers, INITIALIZED).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let call = serde_json::json!({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "document_ingest", "arguments": {"path": path}}});
    let (status, _, body) = send(app, &headers, &call.to_string()).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let reply = message(&body);
    let result = &reply["result"];
    assert_eq!(result["isError"], true, "{reply}");
    result["structuredContent"].clone()
}

fn without(text: &str, path: &str) -> String {
    text.replace(path, "<path>")
}

#[tokio::test]
async fn a_file_outside_the_documents_directories_is_refused_for_operator_and_invited() {
    let s = Scratch::new("outside");
    let app = app(s.config(&[s.path("root")]));
    let secret = s.path("outside/secret.txt");
    let missing = s.path("outside/missing.txt");
    for token in [OPERATOR_TOKEN, INVITED_TOKEN] {
        let existing = ingest(&app, token, &secret).await;
        assert_eq!(existing["error"], "document_outside_roots", "{existing}");
        let message = existing["message"].as_str().unwrap();
        assert!(
            message.contains("memory.reference.ingest_roots"),
            "{message}"
        );
        assert!(
            message.contains(&format!("copy the file into {}", s.path("root"))),
            "{message}"
        );
        assert!(!message.contains("not a document"), "{message}");
        // A missing path outside: the same refusal, so no existence oracle.
        let absent = ingest(&app, token, &missing).await;
        // Compared decoded, so a Windows path's escaped backslashes match.
        let text = |v: &serde_json::Value| v["message"].as_str().unwrap().to_string();
        assert_eq!(absent["error"], existing["error"]);
        assert_eq!(absent["param"], existing["param"]);
        assert_eq!(
            without(&text(&absent), &missing),
            without(&text(&existing), &secret)
        );
    }
}

#[tokio::test]
async fn no_documents_directory_refuses_every_ingest_for_operator_and_invited() {
    let s = Scratch::new("none");
    let app = app(s.config(&[]));
    for token in [OPERATOR_TOKEN, INVITED_TOKEN] {
        for path in [s.path("outside/secret.txt"), s.path("outside/missing.txt")] {
            let refused = ingest(&app, token, &path).await;
            assert_eq!(refused["error"], "no_document_roots", "{refused}");
            let message = refused["message"].as_str().unwrap();
            assert!(
                message.contains("memory.reference.ingest_roots"),
                "{message}"
            );
            assert!(
                message.contains("PSEUDOLIFE_DAEMON_DOCUMENT_ROOTS"),
                "{message}"
            );
            // The none-configured answer, with the Docker fix, not the one
            // for configured directories that are missing.
            assert!(message.contains("none is configured"), "{message}");
            assert!(message.contains("pseudolife-mcp update"), "{message}");
        }
    }
}

#[tokio::test]
async fn a_file_inside_the_root_passes_the_check_and_reaches_the_bank() {
    // The near miss: inside the root the check admits the file, and the
    // call goes on to the bank, which this daemon cannot reach.
    let s = Scratch::new("inside");
    std::fs::write(Path::new(&s.path("root")).join("guide.md"), b"inside").unwrap();
    let app = app(s.config(&[s.path("root")]));
    for token in [OPERATOR_TOKEN, INVITED_TOKEN] {
        let answer = ingest(&app, token, &s.path("root/guide.md")).await;
        assert_eq!(answer["error"], "internal_error", "{answer}");
    }
}
