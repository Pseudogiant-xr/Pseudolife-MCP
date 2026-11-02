//! A disposable coordination proof through the installed native daemon and adapter.
//! The selected server is used only to create and remove this invocation's bank.
use crate::board::{
    adapter::{Adapter, Config},
    state,
};
use crate::credentials::{CredentialProvider, CredentialSnapshot};
use crate::pg::{Dsn, PasswordEnvironment, Session, TlsEnvironment};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::Duration,
};
use unicode_casefold::UnicodeCaseFold;

#[path = "proof_diagnostics.rs"]
mod diagnostics;
#[path = "proof_process.rs"]
mod process;

const RECOVERY: &str = "Check the disposable fixture server and CREATE/DROP DATABASE permission; no host delivery has been verified.";
const TOKEN: &str = "synthetic-proof-bearer";
const DATABASE_IO_TIMEOUT: Duration = Duration::from_secs(10);
// Python measured DROP checkpoint waits of 150–172 s on large fixture servers
// (coordination_proof.py, 2026-10-01). Leave a client margin beyond its 300 s
// statement timeout so a lost response is bounded without defeating that fix.
const DROP_IO_TIMEOUT: Duration = Duration::from_secs(315);

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
        "The native disposable proof needs the daemon's provisioned embedding model. Run `pseudolife-daemon provision models` or run inside the daemon container. If several cached generations exist, set PSEUDOLIFE_DAEMON_ONNX_DIR to one complete embedding directory. Nothing was changed."})
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
    let recovery = match error {
        "ProvisionedModelIntegrityError" => {
            "The native daemon rejected the embedding model's integrity or metadata. Move the damaged model directory aside and run `pseudolife-daemon provision models`, then set PSEUDOLIFE_DAEMON_ONNX_DIR to the repaired embedding directory."
        }
        "OnnxRuntimeUnavailable" => {
            "The native daemon could not load ONNX Runtime. Install the matching native runtime library and set ORT_DYLIB_PATH to it before retrying."
        }
        "NativeProofTlsUnsupported" => {
            "The installed native daemon cannot use this fixture's TLS options. Use an explicitly disposable plaintext fixture server with sslmode=disable and no sslrootcert; nothing was changed."
        }
        "PostgresEnvironmentUnsupported" => {
            "A PG* environment override is unsupported by the native proof. Pass the fixture host, port, database and authentication explicitly in PSEUDOLIFE_TEST_DATABASE_URL and unset the other PG* connection controls; nothing was changed."
        }
        "FixtureListenerUnavailable" => {
            "The loopback fixture port could not be bound. Retry the proof after releasing local listener resources."
        }
        "FixtureDirectoryPermissionError" => {
            "The private fixture directory could not be created or written. Check permissions and free space in the configured temporary directory, then retry."
        }
        "NativeDaemonSpawnError" => {
            "The installed native daemon could not be started. Check its executable permissions and required runtime libraries, then retry."
        }
        "NativeDaemonStartupError" => {
            "The installed native daemon exited before readiness. Check its installation and native runtime libraries, then retry with the provisioned model directory."
        }
        "NativeDaemonStartupTimeout" => {
            "The native fixture daemon did not become ready within 60 seconds. Check the disposable server's availability and the provisioned model/runtime installation before retrying."
        }
        "KeyboardInterrupt" => {
            "The disposable proof was interrupted; owned fixture cleanup was attempted. No host delivery has been verified."
        }
        "TimeoutError" => {
            "The disposable proof exceeded its operation timeout; owned fixture cleanup was attempted. Check fixture responsiveness or increase --timeout before retrying."
        }
        _ => RECOVERY,
    };
    json!({"ok": false, "error": error, "recovery": recovery})
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
                        .filter(|name| !name.is_empty())
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
        // libpq URI queries percent-decode; '+' is literal, not form whitespace.
        // Keep every other option's original bytes, including encoded spaces.
        let query = url.query().map(|query| {
            query
                .split('&')
                .filter(|pair| {
                    let key = pair.split_once('=').map_or(*pair, |(key, _)| key);
                    percent_encoding::percent_decode_str(key).decode_utf8_lossy() != "dbname"
                })
                .collect::<Vec<_>>()
                .join("&")
        });
        url.set_query(query.as_deref());
        url.set_path(&format!("/{name}"));
        Ok(url.into())
    } else {
        // Generated database names contain only ASCII letters, digits and '_'.
        Ok(format!("{dsn} dbname='{name}'"))
    }
}

/// Bind the selected fixture's authentication before changing database or HOME.
/// No credential is placed in argv, output, or a persistent configuration file.
fn authenticated(dsn: &str, environment: &PasswordEnvironment) -> Result<String, &'static str> {
    let parsed = Dsn::parse(dsn).map_err(|_| "ProgrammingError")?;
    let config = parsed
        .resolved_config(environment)
        .map_err(|_| "OperationalError")?;
    let user = config.get_user().ok_or("OperationalError")?;
    let password = std::str::from_utf8(config.get_password().unwrap_or_default())
        .map_err(|_| "ProgrammingError")?;
    if dsn.starts_with("postgres://") || dsn.starts_with("postgresql://") {
        let encode = |value: &str| {
            percent_encoding::utf8_percent_encode(value, percent_encoding::NON_ALPHANUMERIC)
                .to_string()
        };
        let separator = if dsn.contains('?') { '&' } else { '?' };
        Ok(format!(
            "{dsn}{separator}user={}&password={}",
            encode(user),
            encode(password)
        ))
    } else {
        let quote = |value: &str| value.replace('\\', "\\\\").replace('\'', "\\'");
        Ok(format!(
            "{dsn} user='{}' password='{}'",
            quote(user),
            quote(password)
        ))
    }
}

async fn connect(dsn: &str) -> Result<Session, &'static str> {
    let dsn = Dsn::parse(dsn).map_err(|_| "ProgrammingError")?;
    let mut environment = TlsEnvironment::from_environment();
    // Authentication was resolved against the original fixture database; an
    // empty result must not read a different pgpass entry for the admin bank.
    environment.password = PasswordEnvironment::default();
    tokio::time::timeout(Duration::from_secs(5), Session::open_in(&dsn, &environment))
        .await
        .map_err(|_| "OperationalError")?
        .map_err(|error| match error {
            crate::pg::Error::AmbientControl(_) => "PostgresEnvironmentUnsupported",
            _ => "OperationalError",
        })
}

fn daemon_connection_admission(dsn: &str) -> Result<(), &'static str> {
    let parsed = Dsn::parse(dsn).map_err(|_| "ProgrammingError")?;
    if !matches!(
        parsed.ssl_mode(),
        crate::pg::SslMode::Disable | crate::pg::SslMode::Prefer
    ) || dsn.parse::<tokio_postgres::Config>().is_err()
    {
        // The daemon's native pool uses NoTls and cannot read sslrootcert.
        // Refuse before CREATE rather than accepting TLS only in the parent.
        return Err("NativeProofTlsUnsupported");
    }
    Ok(())
}

async fn drop_bank(admin: &str, name: &str) -> bool {
    for _ in 0..2 {
        if let Ok(session) = connect(admin).await {
            let result = tokio::time::timeout(DROP_IO_TIMEOUT, async {
                session
                    .client()
                    .batch_execute("SET statement_timeout = '300s'")
                    .await?;
                session
                    .client()
                    .batch_execute(&format!("DROP DATABASE IF EXISTS \"{name}\" WITH (FORCE)"))
                    .await
            })
            .await;
            // Closing this owned socket must not wait for a lost server reply.
            drop(session);
            if matches!(result, Ok(Ok(()))) {
                return true;
            }
        }
    }
    false
}

struct Fixture {
    root: PathBuf,
    child: Option<Box<dyn process_wrap::tokio::ChildWrapper>>,
    adapters: Vec<Arc<Adapter>>,
    digest_paths: Vec<PathBuf>,
    diagnostics: diagnostics::Diagnostics,
}

impl Fixture {
    async fn close(&mut self) -> Result<(), &'static str> {
        for adapter in self.adapters.drain(..).rev() {
            adapter.close().await;
        }
        // Entry can be cancelled before an Adapter is returned, while its
        // queued blocking writes still own these paths.
        for path in self.digest_paths.drain(..) {
            state::drain(&path).await;
        }
        if let Some(mut child) = self.child.take() {
            // A Child handle owns the unreaped process; no name or shared PID search.
            let _ = child.start_kill();
            let _ = child.wait().await;
        }
        self.diagnostics.close().await;
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
    state::private_dir(&fixture.root).map_err(|_| "FixtureDirectoryPermissionError")?;
    let config = fixture.root.join("config.yaml");
    state::atomic_create(&config, b"coordination:\n  enabled: true\n  allowed_principals: [default]\ndream:\n  enabled: false\nmemory:\n  retrieval_log_enabled: false\nupdates:\n  check_enabled: false\n").map_err(|_| "FixtureDirectoryPermissionError")?;
    let listener =
        std::net::TcpListener::bind(("127.0.0.1", 0)).map_err(|_| "FixtureListenerUnavailable")?;
    let port = listener
        .local_addr()
        .map_err(|_| "FixtureListenerUnavailable")?
        .port();
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
        // Only this owned child's bounded, private classifier reads debug text.
        .env("PSEUDOLIFE_DAEMON_DEBUG_BOARD", "1")
        .env("PSEUDOLIFE_MCP_HOST", "127.0.0.1")
        .env("PSEUDOLIFE_MCP_PORT", port.to_string())
        .env("PSEUDOLIFE_MCP_TOKEN", TOKEN)
        .env("PSEUDOLIFE_RELEASE_CHECK", "0")
        .env("HF_HUB_OFFLINE", "1")
        .env("TRANSFORMERS_OFFLINE", "1")
        .env("CUDA_VISIBLE_DEVICES", "-1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    let mut child = process::spawn(command).map_err(|_| "NativeDaemonSpawnError")?;
    fixture
        .diagnostics
        .watch(child.stderr().take().ok_or("NativeDaemonSpawnError")?);
    fixture.child = Some(child);
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
            // A descendant can still hold stderr after the leader exits.
            // Stop the owned tree before waiting for the diagnostic reader.
            if let Some(mut child) = fixture.child.take() {
                let _ = child.start_kill();
                let _ = child.wait().await;
            }
            fixture.diagnostics.close().await;
            return Err(fixture
                .diagnostics
                .error()
                .unwrap_or("NativeDaemonStartupError"));
        }
        if let Some(error) = fixture.diagnostics.error() {
            return Err(error);
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
            return Err("NativeDaemonStartupTimeout");
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
    if let Some(path) = &digest {
        fixture.digest_paths.push(path.clone());
    }
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
    if let Err(error) = daemon_connection_admission(dsn) {
        return failed(error);
    }
    let dsn = match authenticated(dsn, &PasswordEnvironment::from_environment()) {
        Ok(dsn) => dsn,
        Err(e) => return failed(e),
    };
    let mut signals = match process::Signals::new() {
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
    let admin = match database(&dsn, "postgres") {
        Ok(dsn) => dsn,
        Err(e) => return failed(e),
    };
    let fixture_dsn = match database(&dsn, &name) {
        Ok(dsn) => dsn,
        Err(e) => return failed(e),
    };
    let session = match connect(&admin).await {
        Ok(s) => s,
        Err(e) => return failed(e),
    };
    let create = {
        let create = tokio::time::timeout(DATABASE_IO_TIMEOUT, async {
            session
                .client()
                .batch_execute("SET statement_timeout = '5s'")
                .await?;
            session
                .client()
                .batch_execute(&format!("CREATE DATABASE \"{name}\""))
                .await
        });
        tokio::pin!(create);
        let (create, cancelled) = tokio::select! {
            result = &mut create => (result, false),
            // Let the bounded CREATE settle before DROP, so a pending CREATE
            // cannot finish after cleanup has already observed an absent bank.
            _ = signals.wait() => (create.await, true),
        };
        match create {
            Ok(Ok(())) if cancelled => Err(("KeyboardInterrupt", true)),
            Ok(Ok(())) => Ok(()),
            Ok(Err(error)) => Err((
                if cancelled {
                    "KeyboardInterrupt"
                } else {
                    "OperationalError"
                },
                error.as_db_error().is_none(),
            )),
            Err(_) => Err((
                if cancelled {
                    "KeyboardInterrupt"
                } else {
                    "TimeoutError"
                },
                true,
            )),
        }
    };
    drop(session);
    if let Err((error, uncertain)) = create {
        // A server rejection proves CREATE did not take effect. A lost
        // connection can hide a successful CREATE, so resolve that uncertainty
        // by removing only this invocation's randomly named fixture.
        if uncertain && !drop_bank(&admin, &name).await {
            return json!({"ok":false,"error":"FixtureCleanupError","fixture_removed":false,
                "fixture_bank":name,"recovery":format!("On the explicitly selected fixture server, run DROP DATABASE \"{name}\" WITH (FORCE); no host delivery has been verified.")});
        }
        return failed(error);
    }
    let tag = format!("coordination-proof-{}", uuid::Uuid::new_v4().simple());
    let mut fixture = Fixture {
        root: std::env::temp_dir().join(format!(
            "pseudolife-proof-{}",
            uuid::Uuid::new_v4().simple()
        )),
        child: None,
        adapters: Vec::new(),
        digest_paths: Vec::new(),
        diagnostics: diagnostics::Diagnostics::default(),
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
        _ = signals.wait() => failed("KeyboardInterrupt"),
    };
    let files_removed = fixture.close().await;
    let bank_removed = drop_bank(&admin, &name).await;
    // Windows defaults resume when the last receiver drops. Retain every
    // receiver through owned process, file and database cleanup.
    drop(signals);
    if let Some(error) = cleanup_failure(&name, &fixture.root, bank_removed, files_removed) {
        return error;
    }
    if result["ok"] == true {
        result["fixture_removed"] = json!(true);
    }
    result
}

fn cleanup_failure(
    name: &str,
    root: &Path,
    bank_removed: bool,
    files_removed: Result<(), &'static str>,
) -> Option<Value> {
    if bank_removed && files_removed.is_ok() {
        return None;
    }
    let mut report = json!({"ok":false,"error":if bank_removed {"FixtureDirectoryCleanupError"} else {"FixtureCleanupError"},"fixture_removed":bank_removed});
    let mut recovery = Vec::new();
    if !bank_removed {
        report["fixture_bank"] = json!(name);
        recovery.push(format!(
            "On the explicitly selected fixture server, run DROP DATABASE \"{name}\" WITH (FORCE)."
        ));
    }
    if files_removed.is_err() {
        report["fixture_directory"] = json!(root.to_string_lossy());
        recovery.push(
            "Remove the remaining local fixture directory after releasing any open handles.".into(),
        );
    }
    recovery.push("No host delivery has been verified.".into());
    report["recovery"] = json!(recovery.join(" "));
    Some(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixture_connection_options_reach_the_native_driver() {
        for dsn in [
            "host=localhost user=fixture dbname=r2_options connect_timeout=5 application_name='proof fixture'",
            "postgresql://fixture@localhost/r2_options?connect_timeout=5&application_name=proof%20fixture",
        ] {
            let parsed = Dsn::parse(dsn).unwrap();
            assert_eq!(
                parsed.config().get_connect_timeout(),
                Some(&Duration::from_secs(5))
            );
            assert_eq!(
                parsed.config().get_application_name(),
                Some("proof fixture")
            );
            assert_eq!(daemon_connection_admission(dsn), Ok(()));
            assert_eq!(fixture_name(dsn), Ok("r2_options".into()));
            let child = database(
                &authenticated(dsn, &PasswordEnvironment::default()).unwrap(),
                "r2_child",
            )
            .unwrap();
            let config: tokio_postgres::Config = child.parse().unwrap();
            assert_eq!(config.get_dbname(), Some("r2_child"));
            assert_eq!(config.get_application_name(), Some("proof fixture"));
        }
    }

    #[test]
    fn daemon_tls_admission_refuses_before_any_database_work() {
        for option in [
            "sslmode=require",
            "sslmode=verify-full",
            "sslrootcert=relative.pem",
        ] {
            let dsn = format!("postgresql://fixture@localhost/r2_options?{option}");
            assert_eq!(
                daemon_connection_admission(&dsn),
                Err("NativeProofTlsUnsupported")
            );
        }
        assert_eq!(
            daemon_connection_admission("host=localhost dbname=r2_options sslmode=disable"),
            Ok(())
        );
    }

    #[test]
    fn simultaneous_cleanup_failures_report_both_owned_resources() {
        let report = cleanup_failure(
            "r2_owned",
            Path::new("fixture-directory"),
            false,
            Err("FixtureDirectoryCleanupError"),
        )
        .unwrap();
        assert_eq!(report["fixture_removed"], false);
        assert_eq!(report["fixture_bank"], "r2_owned");
        assert_eq!(report["fixture_directory"], "fixture-directory");
        let recovery = report["recovery"].as_str().unwrap();
        assert!(recovery.contains("DROP DATABASE"));
        assert!(recovery.contains("remaining local fixture directory"));
    }

    #[tokio::test]
    async fn cancelled_entry_file_jobs_finish_before_directory_cleanup() {
        let root =
            std::env::temp_dir().join(format!("pseudolife-proof-test-{}", uuid::Uuid::new_v4()));
        let digest = root.join("entry.digest");
        let (started, ready) = tokio::sync::oneshot::channel();
        let (release, wait) = std::sync::mpsc::channel();
        let write_path = digest.clone();
        let abandoned = state::offload(&digest, move || {
            let _ = started.send(());
            let _ = wait.recv();
            state::private_dir(write_path.parent().unwrap())?;
            state::atomic_create(&write_path, b"late entry write")
        });
        drop(abandoned);
        ready.await.unwrap();
        let mut fixture = Fixture {
            root: root.clone(),
            child: None,
            adapters: Vec::new(),
            digest_paths: vec![digest.clone()],
            diagnostics: diagnostics::Diagnostics::default(),
        };
        let mut close = tokio::spawn(async move { fixture.close().await });
        let early = tokio::time::timeout(Duration::from_millis(100), &mut close).await;
        let closed_early = early.is_ok();
        release.send(()).unwrap();
        let result = match early {
            Ok(result) => result.unwrap(),
            Err(_) => close.await.unwrap(),
        };
        state::drain(&digest).await;
        let leftover = root.exists();
        if leftover {
            std::fs::remove_dir_all(root).unwrap();
        }
        assert!(
            !closed_early,
            "cleanup skipped the cancelled entry's queued write"
        );
        assert_eq!(result, Ok(()));
        assert!(!leftover, "entry recreated fixture files after cleanup");
    }

    #[test]
    fn isolated_child_dsn_binds_resolved_credentials_without_reinterpreting_them() {
        let environment = PasswordEnvironment {
            password: Some(b"synthetic space+'\\password".to_vec()),
            default_user: Some("fixture_user".into()),
            ..Default::default()
        };
        for original in [
            "host=localhost dbname=r2_fixture",
            "postgresql://localhost/r2_fixture",
        ] {
            let bound = authenticated(original, &environment).unwrap();
            let changed = database(&bound, "r2_created").unwrap();
            // The child daemon uses the driver's parser directly, without the
            // CLI's environment or password-file resolution.
            let child: tokio_postgres::Config = changed.parse().unwrap();
            assert_eq!(child.get_user(), Some("fixture_user"));
            assert!(child.get_password() == environment.password.as_deref());
            assert_eq!(child.get_dbname(), Some("r2_created"));
        }
    }

    #[test]
    fn explicit_dsn_password_wins_over_environment_for_the_child() {
        let environment = PasswordEnvironment {
            password: Some(b"other synthetic password".to_vec()),
            ..Default::default()
        };
        let bound = authenticated(
            "host=localhost user=fixture dbname=r2_fixture password='dsn value'",
            &environment,
        )
        .unwrap();
        let child: tokio_postgres::Config = bound.parse().unwrap();
        assert!(child.get_password() == Some(b"dsn value".as_slice()));
    }

    #[test]
    fn database_replacement_preserves_postgres_query_encoding() {
        let original = "postgresql://fixture@localhost/source?password=a%20b%2Bc&sslrootcert=cert%20dir/root.crt&db%6eame=old&dbname=last";
        let changed = database(original, "r2_fixture").unwrap();
        let before = Dsn::parse(original).unwrap();
        let after = Dsn::parse(&changed).unwrap();
        assert_eq!(after.config().get_dbname(), Some("r2_fixture"));
        assert_eq!(
            after.config().get_password(),
            before.config().get_password()
        );
        assert!(changed.contains("sslrootcert=cert%20dir/root.crt"));
        assert!(!changed.contains("dbname="));
        assert!(!changed.contains("db%6eame="));
    }

    #[test]
    fn database_replacement_preserves_literal_plus() {
        let changed = database(
            "postgres://fixture@localhost/source?password=a+b",
            "r2_fixture",
        )
        .unwrap();
        let parsed = Dsn::parse(&changed).unwrap();
        assert_eq!(parsed.config().get_password(), Some(b"a+b".as_slice()));
    }

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
            digest_paths: Vec::new(),
            diagnostics: diagnostics::Diagnostics::default(),
        };
        assert_eq!(fixture.close().await, Err("FixtureDirectoryCleanupError"));
        assert_eq!(std::fs::read(&path).unwrap(), b"fixture replacement");
        std::fs::remove_file(path).unwrap();
    }
}
