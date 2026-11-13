//! Proof that the approved hooks run: a fresh Codex runtime with every
//! other MCP server, plugin and hook switched off and a model provider that
//! points nowhere, one ephemeral thread and one turn, then the daemon must
//! have seen the session's episode open and, once Codex exits, close.
use super::definitions::PLUGIN_ID;
use super::{Fail, Hook, inventory};
use crate::setup::codex::{Rpc, SetupError};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{path::PathBuf, time::Duration};

#[allow(async_fn_in_trait)]
pub trait Session: Rpc {
    fn events(&self) -> &[Value];
    async fn receive(&mut self, timeout: Duration) -> Result<Value, SetupError>;
    async fn close(&mut self);
}

/// Opens Codex app-server sessions on the setup's home and working folder.
#[allow(async_fn_in_trait)]
pub trait Codex {
    type Session: Session;
    /// `overrides` are `-c key=value` switches (never credentials);
    /// `environment` sets (`Some`) or removes (`None`) child variables.
    async fn open(
        &self,
        overrides: &Value,
        environment: &[(String, Option<String>)],
    ) -> Result<Self::Session, SetupError>;
}

/// The memory daemon the hooks talk to.
#[allow(async_fn_in_trait)]
pub trait Daemon {
    async fn healthy(&self) -> bool;
    /// Whether the daemon serves the board check-in to this credential.
    async fn serves_checkin(&self) -> bool;
    async fn episode_open(&self, thread: &str) -> Result<bool, SetupError>;
    /// Seconds to wait for `/health` before refusing.
    fn health_wait(&self) -> Duration {
        Duration::from_secs(30)
    }
    /// Seconds to wait for the hooks of the first turn.
    fn hook_wait(&self) -> Duration {
        Duration::from_secs(25)
    }
}

/// Where the prompt hook saves its cursor; the hooks compute the same
/// default from their own environment.
pub fn digest_dir(lookup: &dyn Fn(&str) -> Option<String>) -> PathBuf {
    if let Some(configured) = lookup("PSEUDOLIFE_DIGEST_DIR").filter(|v| !v.is_empty()) {
        if let Some(rest) = configured
            .strip_prefix("~/")
            .or_else(|| configured.strip_prefix("~\\"))
        {
            return dirs::home_dir().unwrap_or_default().join(rest);
        }
        return PathBuf::from(configured);
    }
    dirs::home_dir()
        .unwrap_or_default()
        .join(".pseudolife-mcp")
        .join("digests")
}

/// Whether the board check-in should print here: only where the daemon
/// serves it, and not when the client opted out.
pub async fn checkin_expected(daemon: &impl Daemon, setting: Option<String>) -> bool {
    let setting = setting.unwrap_or_default().trim().to_lowercase();
    if !setting.is_empty() && !matches!(setting.as_str(), "1" | "true" | "yes" | "on") {
        return false;
    }
    daemon.serves_checkin().await
}

fn overrides(effective: &Value, all: &[Hook], selected: &[&Hook]) -> Result<Value, Fail> {
    let mut overrides = Map::new();
    for (key, value) in [
        ("model_provider", json!("pseudolife_hook_fixture")),
        ("model", json!("fixture-model")),
        (
            "model_providers.pseudolife_hook_fixture.name",
            json!("Local hook verification"),
        ),
        (
            "model_providers.pseudolife_hook_fixture.base_url",
            json!("http://127.0.0.1:1/v1"),
        ),
        (
            "model_providers.pseudolife_hook_fixture.wire_api",
            json!("responses"),
        ),
        (
            "model_providers.pseudolife_hook_fixture.requires_openai_auth",
            json!(false),
        ),
        (
            "model_providers.pseudolife_hook_fixture.request_max_retries",
            json!(0),
        ),
        (
            "model_providers.pseudolife_hook_fixture.stream_max_retries",
            json!(0),
        ),
        ("analytics.enabled", json!(false)),
        ("notify", json!([])),
    ] {
        overrides.insert(key.into(), value);
    }
    let disabled = |names: Option<&Map<String, Value>>, skip: Option<&str>| -> Value {
        Value::Object(
            names
                .into_iter()
                .flatten()
                .filter(|(name, _)| Some(name.as_str()) != skip)
                .map(|(name, _)| (name.clone(), json!({"enabled": false})))
                .collect(),
        )
    };
    overrides.insert(
        "mcp_servers".into(),
        disabled(effective["mcp_servers"].as_object(), None),
    );
    overrides.insert(
        "plugins".into(),
        disabled(effective["plugins"].as_object(), Some(PLUGIN_ID)),
    );
    let own: Vec<&str> = selected.iter().map(|h| h.key.as_str()).collect();
    let mut states = Map::new();
    for hook in all.iter().filter(|h| !own.contains(&h.key.as_str())) {
        if hook.is_managed {
            return Err(Fail::new(
                "Managed hooks also apply here, so the automatic check is unavailable. Review Codex /hooks and check the hooks in a normal session.",
            ));
        }
        states.insert(hook.key.clone(), json!({"enabled": false}));
    }
    overrides.insert("hooks.state".into(), Value::Object(states));
    Ok(Value::Object(overrides))
}

fn completed(events: &[Value]) -> Vec<Value> {
    events
        .iter()
        .filter(|e| e["method"] == "hook/completed")
        .map(|e| e["params"]["run"].clone())
        .collect()
}

pub struct Verification<'a> {
    pub cwd: &'a std::path::Path,
    pub effective: &'a Value,
    pub all: &'a [Hook],
    pub selected: &'a [&'a Hook],
    pub environment: &'a [(String, Option<String>)],
    pub digest_dir: PathBuf,
    pub coordination_setting: Option<String>,
}

pub async fn verify(
    codex: &impl Codex,
    daemon: &impl Daemon,
    check: Verification<'_>,
) -> Result<Value, Fail> {
    let deadline = tokio::time::Instant::now() + daemon.health_wait();
    while !daemon.healthy().await {
        if tokio::time::Instant::now() >= deadline {
            return Err(Fail::new(
                "The memory daemon is not ready. Start it, check its URL and authentication, then rerun the installer.",
            ));
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    let overrides = overrides(&check.effective["config"], check.all, check.selected)?;
    let own: Vec<&str> = check.selected.iter().map(|h| h.key.as_str()).collect();
    let mut client = codex.open(&overrides, check.environment).await?;
    let result = async {
        let (isolated, active) = inventory(&mut client, check.cwd).await?;
        if isolated["config"]["mcp_servers"]
            .as_object()
            .into_iter()
            .flatten()
            .any(|(_, server)| server.get("enabled").is_none_or(|e| e != false))
        {
            return Err(Fail::new("Cannot isolate the hook check from configured MCP servers."));
        }
        let mine: Vec<&Hook> = active.iter().filter(|h| own.contains(&h.key.as_str())).collect();
        if mine.len() != check.selected.len() || mine.iter().any(|h| !h.enabled || h.trust_status != "trusted") {
            return Err(Fail::new(
                "PseudoLife hooks are not all enabled and trusted in a fresh Codex runtime.",
            ));
        }
        if active
            .iter()
            .any(|h| !own.contains(&h.key.as_str()) && h.enabled && h.trust_status == "trusted")
        {
            return Err(Fail::new("Cannot isolate PseudoLife's hook check from other trusted hooks."));
        }
        let thread = client
            .rpc(
                "thread/start",
                json!({"cwd": check.cwd, "ephemeral": true, "approvalPolicy": "never",
                       "sandbox": "read-only", "baseInstructions": "Local hook verification only."}),
            )
            .await?["thread"]["id"]
            .as_str()
            .ok_or_else(|| Fail::new("Codex started no verification thread; update Codex and retry."))?
            .to_owned();
        client
            .rpc(
                "turn/start",
                json!({"threadId": thread, "input": [{"type": "text",
                       "text": "Local hook verification.", "text_elements": []}]}),
            )
            .await?;
        let expected = |event: &str| check.selected.iter().filter(|h| h.event == event).count();
        let count = |runs: &[Value], event: &str| runs.iter().filter(|r| r["eventName"] == event).count();
        let deadline = tokio::time::Instant::now() + daemon.hook_wait();
        loop {
            let runs = completed(client.events());
            if ["sessionStart", "userPromptSubmit"]
                .iter()
                .all(|event| count(&runs, event) >= expected(event))
            {
                break;
            }
            let left = deadline.saturating_duration_since(tokio::time::Instant::now());
            if left.is_zero() || client.receive(left).await.is_err() {
                break;
            }
        }
        let runs = completed(client.events());
        // The prompt hook prints only when memory changed, and a session's
        // first turn is a silent baseline, so its proof is the cursor it
        // saves after an authorized answer from the daemon.
        let mark = check
            .digest_dir
            .join(format!("{:x}.mark", Sha256::digest(thread.as_bytes())));
        for (event, name) in [("sessionStart", "SessionStart"), ("userPromptSubmit", "UserPromptSubmit")] {
            let mine: Vec<&Value> = runs.iter().filter(|r| r["eventName"] == event).collect();
            let memory = if event == "sessionStart" {
                mine.iter().any(|r| {
                    r["entries"].as_array().into_iter().flatten().any(|e| {
                        e["text"].as_str().is_some_and(|t| t.contains("Session episode:"))
                    })
                })
            } else {
                mark.is_file()
            };
            if mine.len() != expected(event) || mine.iter().any(|r| r["status"] != "completed") || !memory {
                return Err(Fail::detail(format!(
                    "{name} did not return the expected memory context ({} completed events, memory={}). Check daemon access and Codex /hooks.",
                    mine.len(),
                    if memory { "True" } else { "False" }
                )));
            }
        }
        let _ = std::fs::remove_file(&mark);
        if checkin_expected(daemon, check.coordination_setting.clone()).await
            && !runs.iter().filter(|r| r["eventName"] == "sessionStart").any(|r| {
                r["entries"].as_array().into_iter().flatten().any(|e| {
                    e["text"].as_str().is_some_and(|t| t.contains("memory_agents(action=list)"))
                })
            })
        {
            return Err(Fail::new(
                "The agent-board check-in hook (CoordinationStart) returned no guidance. Check Codex /hooks.",
            ));
        }
        if !daemon.episode_open(&thread).await? {
            return Err(Fail::new(
                "SessionStart did not open a verifiable memory episode. Check daemon access.",
            ));
        }
        Ok(thread)
    }
    .await;
    client.close().await;
    let thread = result?;
    if daemon.episode_open(&thread).await? {
        return Err(Fail::new(
            "SessionEnd did not close the verification episode. Check daemon access and Codex /hooks.",
        ));
    }
    Ok(json!({"session_start": true, "user_prompt_submit": true, "session_end": true}))
}

// -- the real Codex and daemon -------------------------------------------------

pub struct RealCodex {
    pub executable: PathBuf,
    pub home: PathBuf,
    pub cwd: PathBuf,
}

impl Session for crate::setup::codex::Client {
    fn events(&self) -> &[Value] {
        crate::setup::codex::Client::events(self)
    }
    async fn receive(&mut self, timeout: Duration) -> Result<Value, SetupError> {
        crate::setup::codex::Client::receive(self, timeout).await
    }
    async fn close(&mut self) {
        crate::setup::codex::Client::close(self).await;
    }
}

impl Codex for RealCodex {
    type Session = crate::setup::codex::Client;
    async fn open(
        &self,
        overrides: &Value,
        environment: &[(String, Option<String>)],
    ) -> Result<Self::Session, SetupError> {
        crate::setup::codex::Client::open_with_environment(
            &self.executable,
            &self.home,
            &self.cwd,
            overrides,
            environment,
        )
        .await
    }
}

pub struct RealDaemon {
    pub url: String,
    pub token: Option<String>,
}

impl RealDaemon {
    async fn get(&self, path: &str) -> Option<reqwest::Response> {
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(3))
            .build()
            .ok()?;
        let mut request = client.get(format!("{}{path}", self.url.trim_end_matches('/')));
        if let Some(token) = &self.token {
            request = request.bearer_auth(token);
        }
        request
            .send()
            .await
            .ok()
            .filter(|r| r.status().is_success())
    }
}

impl Daemon for RealDaemon {
    async fn healthy(&self) -> bool {
        match self.get("/health").await {
            Some(response) => response
                .json::<Value>()
                .await
                .is_ok_and(|v| v["status"] == "ok"),
            None => false,
        }
    }
    async fn serves_checkin(&self) -> bool {
        match self.get("/api/hook/coordination-start").await {
            Some(response) => response.text().await.is_ok_and(|t| !t.trim().is_empty()),
            None => false,
        }
    }
    async fn episode_open(&self, thread: &str) -> Result<bool, SetupError> {
        // Exact lookup is bounded by the episode's place in the newest 100.
        let unavailable =
            SetupError("The memory daemon did not list its episodes; check daemon access.");
        let response = self
            .get("/api/episodes?limit=100")
            .await
            .ok_or(unavailable.clone())?;
        let value: Value = response.json().await.map_err(|_| unavailable.clone())?;
        let episodes = value["episodes"].as_array().ok_or(unavailable)?;
        Ok(episodes.iter().any(|e| e["session_key"] == thread))
    }
}
