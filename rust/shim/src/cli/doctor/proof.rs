//! A disposable coordination proof through the installed native daemon and adapter.
//! The selected server is used only to create and remove this invocation's bank.
use crate::board::{
    adapter::{Adapter, Config},
    state,
};
use crate::credentials::{CredentialProvider, CredentialSnapshot};
use crate::pg::{Dsn, Session};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::Duration,
};
use tokio::process::Child;
use unicode_casefold::UnicodeCaseFold;

const RECOVERY: &str = "Check the disposable fixture server and CREATE/DROP DATABASE permission; no host delivery has been verified.";
const TOKEN: &str = "synthetic-proof-bearer";

pub(super) fn daemon() -> Option<PathBuf> {
    let name = if cfg!(windows) {
        "pseudolife-daemon.exe"
    } else {
        "pseudolife-daemon"
    };
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.join(name)))
        .filter(|p| p.is_file())
        .or_else(|| crate::lifecycle::find_executable(name).ok())
}

pub(super) fn missing_daemon() -> Value {
    json!({"ok": false, "error": "NativeDaemonRequired", "recovery":
        "The native disposable proof requires pseudolife-daemon. Run doctor where the daemon is installed (for example, docker exec <daemon container> pseudolife-mcp doctor --disposable-proof with an explicit disposable PSEUDOLIFE_TEST_DATABASE_URL), or install the native daemon; nothing was changed."})
}

pub(super) fn missing_models() -> Value {
    json!({"ok": false, "error": "ProvisionedModelsRequired", "recovery":
        "The native disposable proof needs the daemon's provisioned embedding model. Run `pseudolife-daemon provision models` or run inside the daemon container; keep the same PSEUDOLIFE_DAEMON_MODELS_DIR (or PSEUDOLIFE_DAEMON_ONNX_DIR) setting. Nothing was changed."})
}

/// Resolve the host's read-only model inputs before replacing HOME and data dir.
/// Presence is an admission check; the daemon verifies all pinned checksums.
pub(super) fn models() -> Option<PathBuf> {
    let set = |key: &str| {
        std::env::var_os(key)
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
    };
    let complete = |root: &Path| {
        [
            "onnx/model.onnx",
            "onnx/model.onnx_data",
            "tokenizer.json",
            "config.json",
            "modules.json",
            "1_Pooling/config.json",
            "tokenizer_config.json",
            "config_sentence_transformers.json",
        ]
        .iter()
        .all(|p| root.join(p).is_file())
    };
    if let Some(root) = set("PSEUDOLIFE_DAEMON_ONNX_DIR") {
        return complete(&root)
            .then(|| std::fs::canonicalize(root).ok())
            .flatten();
    }
    let cache = set("PSEUDOLIFE_DAEMON_MODELS_DIR").or_else(|| {
        let data = set("PSEUDOLIFE_MCP_DATA_DIR").or_else(|| {
            let home = set(if cfg!(windows) { "USERPROFILE" } else { "HOME" });
            crate::pg::native_provision::default_data_dir(
                crate::pg::native_provision::Platform::current(),
                &|k| std::env::var_os(k),
                home.as_deref(),
            )
        });
        data.map(|p| p.join("models"))
    })?;
    let mut candidates: Vec<_> = std::fs::read_dir(cache)
        .ok()?
        .filter_map(Result::ok)
        .filter(|e| {
            e.file_name()
                .to_string_lossy()
                .starts_with("qwen3-msmarco-fp32-v1-")
        })
        .map(|e| e.path().join("embedding"))
        .filter(|p| complete(p))
        .collect();
    candidates.sort();
    // Do not guess between several provisioned generations.
    if candidates.len() != 1 {
        return None;
    }
    std::fs::canonicalize(&candidates[0]).ok()
}

fn failed(error: &'static str) -> Value {
    json!({"ok": false, "error": error, "recovery": RECOVERY})
}

fn fold(name: &str) -> String {
    name.trim_end_matches('/').case_fold().collect()
}

fn fixture_name(dsn: &str) -> Result<String, &'static str> {
    let selected = Dsn::parse(dsn).map_err(|_| "ProgrammingError")?;
    let name = selected
        .config()
        .get_dbname()
        .filter(|s| !s.is_empty())
        .ok_or("ValueError")?;
    let mut refused = vec!["pseudolife_memory".to_owned()];
    match std::env::var("_PSEUDOLIFE_PRODUCTION_DB")
        .ok()
        .filter(|s| !s.is_empty())
    {
        Some(recorded) if recorded == "<unresolved>" => return Err("ProductionDatabaseError"),
        Some(recorded) => refused.push(recorded),
        None => {
            if let Ok(live) = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL")
                && !live.is_empty()
            {
                let live = Dsn::parse(&live).map_err(|_| "ProductionDatabaseError")?;
                refused.push(
                    live.config()
                        .get_dbname()
                        .ok_or("ProductionDatabaseError")?
                        .to_owned(),
                );
            }
        }
    }
    if refused.iter().any(|v| fold(v) == fold(name)) {
        return Err("ProductionDatabaseError");
    }
    Ok(name.to_owned())
}

/// Preserve the connection's authority while replacing every database selector.
fn database(dsn: &str, name: &str) -> Result<String, &'static str> {
    if dsn.starts_with("postgres://") || dsn.starts_with("postgresql://") {
        let mut url = reqwest::Url::parse(dsn).map_err(|_| "ProgrammingError")?;
        let query: Vec<_> = url
            .query_pairs()
            .filter(|(key, _)| key != "dbname")
            .map(|(k, v)| (k.into_owned(), v.into_owned()))
            .collect();
        url.set_query(None);
        if !query.is_empty() {
            url.query_pairs_mut().extend_pairs(query);
        }
        url.set_path(&format!("/{name}"));
        Ok(url.into())
    } else {
        // Generated database names contain only ASCII letters, digits and '_'.
        Ok(format!("{dsn} dbname='{name}'"))
    }
}

async fn connect(dsn: &str) -> Result<Session, &'static str> {
    let dsn = Dsn::parse(dsn).map_err(|_| "ProgrammingError")?;
    tokio::time::timeout(Duration::from_secs(5), Session::open(&dsn))
        .await
        .map_err(|_| "OperationalError")?
        .map_err(|_| "OperationalError")
}

async fn drop_bank(admin: &str, name: &str) -> bool {
    for _ in 0..2 {
        if let Ok(session) = connect(admin).await {
            let result = async {
                session
                    .client()
                    .batch_execute("SET statement_timeout = '300s'")
                    .await?;
                session
                    .client()
                    .batch_execute(&format!("DROP DATABASE IF EXISTS \"{name}\" WITH (FORCE)"))
                    .await
            }
            .await;
            let _ = session.close().await;
            if result.is_ok() {
                return true;
            }
        }
    }
    false
}

/// Install handlers before CREATE so a signal during fixture creation is
/// remembered and handled after ownership has been established.
fn interrupted() -> std::io::Result<std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>>>
{
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let mut interrupt = signal(SignalKind::interrupt())?;
        let mut terminate = signal(SignalKind::terminate())?;
        Ok(Box::pin(async move {
            tokio::select! { _ = interrupt.recv() => {}, _ = terminate.recv() => {} }
        }))
    }
    #[cfg(windows)]
    {
        let mut interrupt = tokio::signal::windows::ctrl_c()?;
        let mut terminate = tokio::signal::windows::ctrl_break()?;
        Ok(Box::pin(async move {
            tokio::select! { _ = interrupt.recv() => {}, _ = terminate.recv() => {} }
        }))
    }
}

struct Fixture {
    root: PathBuf,
    child: Option<Child>,
    adapters: Vec<Arc<Adapter>>,
}

impl Fixture {
    async fn close(&mut self) -> Result<(), &'static str> {
        for adapter in self.adapters.drain(..).rev() {
            adapter.close().await;
        }
        if let Some(mut child) = self.child.take() {
            // A Child handle owns the unreaped process; no name or shared PID search.
            let _ = child.kill().await;
            let _ = child.wait().await;
        }
        match std::fs::remove_dir_all(&self.root) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err("FixtureDirectoryCleanupError"),
        }
    }
}

async fn start(
    daemon: &Path,
    models: &Path,
    dsn: &str,
    fixture: &mut Fixture,
) -> Result<String, &'static str> {
    state::private_dir(&fixture.root).map_err(|_| "PermissionError")?;
    let config = fixture.root.join("config.yaml");
    state::atomic_create(&config, b"coordination:\n  enabled: true\n  allowed_principals: [default]\ndream:\n  enabled: false\nmemory:\n  retrieval_log_enabled: false\nupdates:\n  check_enabled: false\n").map_err(|_| "PermissionError")?;
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).map_err(|_| "OSError")?;
    let port = listener.local_addr().map_err(|_| "OSError")?.port();
    drop(listener);
    let mut command = tokio::process::Command::new(daemon);
    command.env_clear();
    for key in [
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "TEMP",
        "TMP",
        "LD_LIBRARY_PATH",
        "ORT_DYLIB_PATH",
    ] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    command
        .current_dir(&fixture.root)
        .env("HOME", &fixture.root)
        .env("USERPROFILE", &fixture.root)
        .env("PSEUDOLIFE_MCP_DATABASE_URL", dsn)
        .env("PSEUDOLIFE_MCP_CONFIG", config)
        .env("PSEUDOLIFE_MCP_DATA_DIR", fixture.root.join("data"))
        .env("PSEUDOLIFE_DAEMON_ONNX_DIR", models)
        .env(
            "PSEUDOLIFE_DAEMON_RERANK_DIR",
            models.parent().unwrap_or(models).join("reranker"),
        )
        .env("PSEUDOLIFE_DAEMON_ORT_THREADS", "2")
        .env("PSEUDOLIFE_MCP_HOST", "127.0.0.1")
        .env("PSEUDOLIFE_MCP_PORT", port.to_string())
        .env("PSEUDOLIFE_MCP_TOKEN", TOKEN)
        .env("PSEUDOLIFE_RELEASE_CHECK", "0")
        .env("HF_HUB_OFFLINE", "1")
        .env("TRANSFORMERS_OFFLINE", "1")
        .env("CUDA_VISIBLE_DEVICES", "-1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    fixture.child = Some(command.spawn().map_err(|_| "OSError")?);
    let url = format!("http://127.0.0.1:{port}");
    let client = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(1))
        .build()
        .map_err(|_| "RuntimeError")?;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
    loop {
        if fixture
            .child
            .as_mut()
            .unwrap()
            .try_wait()
            .map_err(|_| "OSError")?
            .is_some()
        {
            return Err("NativeDaemonStartupError");
        }
        // An authenticated request loads the principal view; public /health
        // deliberately cannot do that initialization itself.
        let _ = client
            .post(format!("{url}/api/coordination/context"))
            .bearer_auth(TOKEN)
            .json(&json!({}))
            .send()
            .await;
        if let Ok(response) = client.get(format!("{url}/health")).send().await
            && response.status().is_success()
            && let Ok(health) = response.json::<Value>().await
            && health["ready"] == true
        {
            return Ok(url);
        }
        if tokio::time::Instant::now() >= deadline {
            return Err("TimeoutError");
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

async fn adapter(
    fixture: &mut Fixture,
    url: &str,
    tag: &str,
    digest: Option<PathBuf>,
    label: &str,
) -> Result<(Arc<Adapter>, CredentialSnapshot), &'static str> {
    let provider = CredentialProvider::new(Some(TOKEN.into()), None).map_err(|_| "RuntimeError")?;
    let snapshot = provider.snapshot().map_err(|_| "RuntimeError")?;
    let adapter = Adapter::enter(Config {
        url: url.into(),
        provider,
        state: None,
        digest,
        label: label.into(),
        project: tag.into(),
        task: String::new(),
        episode: String::new(),
        codex: false,
        wake: false,
        ring: true,
        parent: None,
        initial_snapshot: Some(snapshot.clone()),
        legacy_state: None,
    })
    .await
    .map_err(|_| "RuntimeError")?;
    fixture.adapters.push(adapter.clone());
    Ok((adapter, snapshot))
}

async fn post(
    client: &reqwest::Client,
    url: &str,
    adapter: &Adapter,
    snapshot: &CredentialSnapshot,
    action: &str,
    body: Value,
) -> Result<Value, &'static str> {
    let headers = adapter
        .instance_headers(snapshot)
        .await
        .map_err(|_| "RuntimeError")?;
    let response = client
        .post(format!("{url}/api/coordination/{action}"))
        .headers(headers)
        .json(&body)
        .send()
        .await
        .map_err(|_| "RuntimeError")?;
    if !response.status().is_success() {
        return Err("RuntimeError");
    }
    let value = response.json().await.map_err(|_| "RuntimeError")?;
    adapter.update_mailbox(&value).await;
    Ok(value)
}

async fn exercise(fixture: &mut Fixture, url: &str, tag: &str) -> Result<Value, &'static str> {
    let client = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|_| "RuntimeError")?;
    let (sender, sender_snapshot) = adapter(fixture, url, tag, None, "proof-sender").await?;
    let mut stages = Vec::new();
    for path in ["hint", "ring"] {
        let digest = fixture.root.join(format!(
            "{}{}.txt",
            uuid::Uuid::new_v4().simple(),
            uuid::Uuid::new_v4().simple()
        ));
        let (recipient, snapshot) =
            adapter(fixture, url, tag, Some(digest.clone()), "proof-recipient").await?;
        if path == "ring" {
            recipient.set_ring_listener(Arc::new(|| true));
            post(&client,url,&recipient,&snapshot,"update",json!({
                "park_reason":"waiting_peer","park_needs":"fixture ready","park_clear_by":"anyone"
            })).await?;
        }
        recipient.heartbeat().await.map_err(|_| "RuntimeError")?;
        let headers = recipient
            .instance_headers(&snapshot)
            .await
            .map_err(|_| "RuntimeError")?;
        let to = headers
            .get("x-pl-agent")
            .and_then(|h| h.to_str().ok())
            .ok_or("RuntimeError")?;
        let sent = post(&client,url,&sender,&sender_snapshot,"send",json!({
            "to":to,"text":"Synthetic fixture ready","clears":"fixture ready","request_id":format!("{tag}-{path}")
        })).await?;
        let expected = if path == "ring" { "rung" } else { "hinted" };
        if sent["wake"]["decision"] != expected {
            return Err("RuntimeError");
        }
        recipient.heartbeat().await.map_err(|_| "RuntimeError")?;
        let signal = if path == "ring" {
            // The actual adapter writes its ring marker; no host or model is launched.
            loop {
                if state::read(&digest.with_extension("ring"), 1024).is_ok() {
                    break true;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        } else {
            recipient.deliver_hint().await.is_some()
        };
        if !signal {
            return Err("RuntimeError");
        }
        let received = post(&client, url, &recipient, &snapshot, "receive", json!({})).await?;
        let messages = received["messages"].as_array().ok_or("RuntimeError")?;
        if messages.len() != 1 || messages[0]["message_id"] != sent["message_id"] {
            return Err("RuntimeError");
        }
        post(
            &client,
            url,
            &recipient,
            &snapshot,
            "ack",
            json!({"message_id":sent["message_id"]}),
        )
        .await?;
        let received = post(&client, url, &recipient, &snapshot, "receive", json!({})).await?;
        if received["messages"] != json!([]) {
            return Err("RuntimeError");
        }
        stages.push(
            json!({"path":path,"message_id":sent["message_id"],"enqueued":true,
            "wake_decision":expected,"signal_observed":true,"received":true,"harness_ack":true}),
        );
        recipient.close().await;
    }
    Ok(
        json!({"ok":true,"tag":tag,"instrument":"disposable_http_actual_adapter",
        "stages":stages,"host_delivery":"unverified","model_ack":"unverified"}),
    )
}

pub(super) async fn run(dsn: &str, daemon: &Path, models: &Path, timeout: f64) -> Value {
    let selected = match fixture_name(dsn) {
        Ok(name) => name,
        Err(e) => return failed(e),
    };
    let interrupted = match interrupted() {
        Ok(signal) => signal,
        Err(_) => return failed("OSError"),
    };
    // Keep disposable-server fixture namespaces disjoint across concurrent callers.
    let prefix: String = selected
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '_')
        .take(20)
        .collect();
    let name = format!("{prefix}_proof_{}", uuid::Uuid::new_v4().simple());
    let admin = match database(dsn, "postgres") {
        Ok(dsn) => dsn,
        Err(e) => return failed(e),
    };
    let fixture_dsn = match database(dsn, &name) {
        Ok(dsn) => dsn,
        Err(e) => return failed(e),
    };
    let session = match connect(&admin).await {
        Ok(s) => s,
        Err(e) => return failed(e),
    };
    let create = async {
        session
            .client()
            .batch_execute("SET statement_timeout = '5s'")
            .await?;
        session
            .client()
            .batch_execute(&format!("CREATE DATABASE \"{name}\""))
            .await
    }
    .await;
    let _ = session.close().await;
    if let Err(error) = create {
        // A server rejection proves CREATE did not take effect. A lost
        // connection can hide a successful CREATE, so resolve that uncertainty
        // by removing only this invocation's randomly named fixture.
        if error.as_db_error().is_none() && !drop_bank(&admin, &name).await {
            return json!({"ok":false,"error":"FixtureCleanupError","fixture_removed":false,
                "fixture_bank":name,"recovery":format!("On the explicitly selected fixture server, run DROP DATABASE \"{name}\" WITH (FORCE); no host delivery has been verified.")});
        }
        return failed("OperationalError");
    }
    let tag = format!("coordination-proof-{}", uuid::Uuid::new_v4().simple());
    let mut fixture = Fixture {
        root: std::env::temp_dir().join(format!(
            "pseudolife-proof-{}",
            uuid::Uuid::new_v4().simple()
        )),
        child: None,
        adapters: Vec::new(),
    };
    let proof = async {
        match start(daemon, models, &fixture_dsn, &mut fixture).await {
            Ok(url) => match tokio::time::timeout(
                Duration::from_secs_f64(timeout),
                exercise(&mut fixture, &url, &tag),
            )
            .await
            {
                Ok(Ok(proof)) => proof,
                Ok(Err(e)) => failed(e),
                Err(_) => failed("TimeoutError"),
            },
            Err(e) => failed(e),
        }
    };
    let mut result = tokio::select! {
        result = proof => result,
        _ = interrupted => failed("KeyboardInterrupt"),
    };
    let files_removed = fixture.close().await;
    if !drop_bank(&admin, &name).await {
        return json!({"ok":false,"error":"FixtureCleanupError","fixture_removed":false,
            "fixture_bank":name,"recovery":format!("On the explicitly selected fixture server, run DROP DATABASE \"{name}\" WITH (FORCE); no host delivery has been verified.")});
    }
    if let Err(error) = files_removed {
        return json!({"ok":false,"error":error,"fixture_removed":true,
            "fixture_directory":fixture.root.to_string_lossy(),
            "recovery":"The disposable bank was removed, but its local fixture directory could not be removed. Remove that directory after releasing any open handles; no host delivery has been verified."});
    }
    if result["ok"] == true {
        result["fixture_removed"] = json!(true);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn directory_cleanup_failure_is_not_reported_as_success() {
        let path =
            std::env::temp_dir().join(format!("pseudolife-proof-test-{}", uuid::Uuid::new_v4()));
        // A replacement regular file cannot be removed by directory cleanup.
        std::fs::write(&path, b"fixture replacement").unwrap();
        let mut fixture = Fixture {
            root: path.clone(),
            child: None,
            adapters: Vec::new(),
        };
        assert_eq!(fixture.close().await, Err("FixtureDirectoryCleanupError"));
        assert_eq!(std::fs::read(&path).unwrap(), b"fixture replacement");
        std::fs::remove_file(path).unwrap();
    }
}
