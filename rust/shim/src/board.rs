//! Session-scoped coordination. Mailbox credentials never enter diagnostics.
pub mod adapter;
pub mod claims;
#[cfg(feature = "codex-delivery")]
pub mod delivery;
pub mod doorbell;
pub mod identity;
pub mod liveness;
pub mod notice;
pub mod policy;
pub mod state;
mod unicode14;

use crate::{lifecycle::Runtime, upstream::OperationContext};
use adapter::{Adapter, Config};
use rmcp::{ErrorData, model::CallToolResult};
use serde_json::{Map, Value, json};
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::sync::Mutex;
use tokio_util::{sync::CancellationToken, task::TaskTracker};

#[derive(Clone)]
pub struct Timing {
    pub startup: Duration,
    pub probe: Duration,
    pub retry_attempt: Duration,
    pub retry_delays: [Duration; 6],
    pub registry_retry: Duration,
}
impl Default for Timing {
    fn default() -> Self {
        Self {
            startup: Duration::from_secs(3),
            probe: Duration::from_secs(2),
            retry_attempt: Duration::from_secs(30),
            retry_delays: policy::RETRY_DELAYS.map(Duration::from_secs),
            registry_retry: Duration::from_secs(5),
        }
    }
}
#[derive(Clone)]
pub struct Options {
    pub shared: bool,
    pub codex: bool,
    pub channel: bool,
    pub setting: String,
    pub state: Option<PathBuf>,
    pub state_root: PathBuf,
    pub digest_root: PathBuf,
    pub label: String,
    pub project: String,
    pub task: String,
    pub wake: bool,
    pub delivery_url: Option<String>,
    pub delivery_token: Option<String>,
    pub doorbell_setting: Option<String>,
    pub command_environment: HashMap<String, String>,
    pub session_state_root: bool,
    pub host_session: Option<String>,
    pub timing: Timing,
}
impl Options {
    pub fn from_lookup(
        runtime: &Runtime,
        channel: bool,
        mut env: impl FnMut(&str) -> Option<String>,
    ) -> Self {
        let writer = runtime.session.writer.as_deref();
        let shared = policy::shared_host(writer, env("PSEUDOLIFE_MCP_SHARED_HOST").as_deref());
        let codex = !shared
            && !channel
            && writer.is_some_and(|writer| writer.trim().eq_ignore_ascii_case("codex"));
        let expanded = |path: String| {
            crate::credentials::absolute_expanded(std::path::Path::new(&path))
                .unwrap_or_else(|_| PathBuf::from(path))
        };
        let home = dirs::home_dir().unwrap_or_default();
        let configured_root = env("PSEUDOLIFE_AGENT_STATE_DIR").filter(|value| !value.is_empty());
        let session_state_root = configured_root.is_some();
        let state_root = configured_root.map(expanded).unwrap_or_else(|| {
            env("CODEX_HOME")
                .map(expanded)
                .unwrap_or_else(|| home.join(".codex"))
                .join("pseudolife/agents")
        });
        let digest_root = env("PSEUDOLIFE_DIGEST_DIR")
            .filter(|value| !value.is_empty())
            .map(expanded)
            .unwrap_or_else(|| home.join(".pseudolife-mcp/digests"));
        let state = env("PSEUDOLIFE_AGENT_STATE")
            .filter(|value| !value.is_empty())
            .map(expanded);
        Self {
            shared,
            codex,
            channel,
            setting: env("PSEUDOLIFE_AGENT_COORDINATION")
                .unwrap_or_default()
                .trim()
                .to_lowercase(),
            state,
            state_root,
            digest_root,
            label: env("PSEUDOLIFE_AGENT_LABEL")
                .unwrap_or_else(|| if codex { "codex" } else { "agent" }.into()),
            project: env("PSEUDOLIFE_AGENT_PROJECT").unwrap_or_default(),
            task: env("PSEUDOLIFE_AGENT_TASK").unwrap_or_default(),
            wake: env("PSEUDOLIFE_AGENT_WAKE").is_some_and(|value| policy::yes(&value)),
            delivery_url: env("PSEUDOLIFE_CODEX_SERVER_URL"),
            delivery_token: env("PSEUDOLIFE_CODEX_SERVER_TOKEN"),
            doorbell_setting: env("PSEUDOLIFE_CODEX_DOORBELL"),
            command_environment: ["PSEUDOLIFE_CODEX_BIN", "PATH", "PATHEXT", "LOCALAPPDATA"]
                .into_iter()
                .filter_map(|key| env(key).map(|value| (key.to_owned(), value)))
                .collect(),
            session_state_root,
            host_session: env("CLAUDE_CODE_SESSION_ID")
                .and_then(|value| policy::canonical_uuid(&value)),
            timing: Timing::default(),
        }
    }
}
enum Mode {
    Off,
    Pending,
    Adapter(Arc<Adapter>),
    Registry,
    Stopped,
}
struct Status {
    mode: Mode,
    checkin: bool,
    note: Option<&'static str>,
    validated: bool,
}
struct ThreadEntry {
    adapter: Option<Arc<Adapter>>,
    #[cfg(feature = "codex-delivery")]
    delivery: Option<Arc<delivery::Delivery>>,
    retry: Option<(crate::credentials::Generation, tokio::time::Instant)>,
    late_note: bool,
    delivery_failed: Arc<AtomicBool>,
    failure_hint: String,
}
pub struct Board {
    runtime: Arc<Runtime>,
    options: Options,
    status: Mutex<Status>,
    threads: Mutex<HashMap<String, Arc<Mutex<ThreadEntry>>>>,
    closing: AtomicBool,
    cancel: CancellationToken,
    tasks: TaskTracker,
    doorbell: Mutex<Option<Arc<doorbell::Doorbell>>>,
    #[cfg(feature = "codex-delivery")]
    bridge_enabled: AtomicBool,
    #[cfg(feature = "codex-delivery")]
    bridge_setup_reported: AtomicBool,
    registry_failure_reported: AtomicBool,
    capacity_reported: AtomicBool,
}
pub enum Preparation {
    Forward(PreparedCall),
    Result(CallToolResult),
}
pub struct PreparedCall {
    pub arguments: Map<String, Value>,
    pub operation: OperationContext,
    adapter: Option<Arc<Adapter>>,
    thread: Option<String>,
    name: String,
    original: Map<String, Value>,
    registry_hint: Option<String>,
}

fn coordination_error(message: Option<&str>, hint: Option<String>) -> ErrorData {
    let mut data = json!({"classification":"coordination_unavailable","phase":"initialize","operation_outcome":"not_dispatched"});
    if let Some(hint) = hint {
        data["hint"] = json!(hint);
    }
    ErrorData::internal_error(
        message
            .unwrap_or("Coordination identity is unavailable; reattach coordination and retry.")
            .to_owned(),
        Some(data),
    )
}
fn credential_error() -> ErrorData {
    ErrorData::internal_error(
        "The memory credential is unavailable; restore the configured credential and retry.",
        Some(
            json!({"classification":"credential_unavailable","phase":"call","operation_outcome":"not_dispatched"}),
        ),
    )
}
async fn probe_before_registry<P, F, T>(closing: &AtomicBool, probe: P, build: F) -> Option<T>
where
    P: std::future::Future<Output = Option<bool>>,
    F: FnOnce(Option<bool>) -> T,
{
    let answer = probe.await;
    if closing.load(Ordering::Acquire) {
        return None;
    }
    Some(build(answer))
}

impl Board {
    pub async fn attach(runtime: Arc<Runtime>, channel: bool) -> Arc<Self> {
        let options = Options::from_lookup(&runtime, channel, |key| std::env::var(key).ok());
        Self::attach_options(runtime, options).await
    }
    pub async fn attach_options(runtime: Arc<Runtime>, options: Options) -> Arc<Self> {
        let board = Arc::new(Self {
            runtime,
            options,
            status: Mutex::new(Status {
                mode: Mode::Off,
                checkin: false,
                note: None,
                validated: false,
            }),
            threads: Mutex::new(HashMap::new()),
            closing: AtomicBool::new(false),
            cancel: CancellationToken::new(),
            tasks: TaskTracker::new(),
            doorbell: Mutex::new(None),
            #[cfg(feature = "codex-delivery")]
            bridge_enabled: AtomicBool::new(false),
            #[cfg(feature = "codex-delivery")]
            bridge_setup_reported: AtomicBool::new(false),
            registry_failure_reported: AtomicBool::new(false),
            capacity_reported: AtomicBool::new(false),
        });
        if board.options.shared {
            if policy::yes(&board.options.setting) {
                crate::stderrln!(
                    "pseudolife-mcp: this process serves every conversation in the host without a supported per-conversation binding, so it registers no coordination address; board writes are refused here."
                );
            }
            return board;
        }
        let explicit = policy::yes(&board.options.setting);
        let token = board
            .runtime
            .provider
            .snapshot()
            .ok()
            .is_some_and(|snapshot| snapshot.token().is_some());
        let answer = if explicit {
            Some(true)
        } else if !board.options.setting.is_empty() || !token {
            Some(false)
        } else if board.runtime.daemon_unreachable() {
            None
        } else {
            board.probe().await
        };
        if board.options.codex && board.options.state.is_some() && answer != Some(false) {
            crate::stderrln!(
                "pseudolife-mcp: Codex automatic coordination is disabled by the fixed PSEUDOLIFE_AGENT_STATE setting because threads must not share credentials; remove it and use PSEUDOLIFE_AGENT_STATE_DIR instead."
            );
            return board;
        }
        match answer {
            Some(false) => {
                board.report_doorbell_off(false);
            }
            Some(true) if board.options.codex => {
                board.configure_registry().await;
                let checkin = if explicit {
                    board.probe().await == Some(true)
                } else {
                    true
                };
                let mut status = board.status.lock().await;
                status.mode = Mode::Registry;
                status.checkin = checkin;
            }
            Some(true) => {
                let entered = tokio::time::timeout(
                    board.options.timing.startup,
                    Adapter::enter(board.config(None, None)),
                )
                .await;
                match entered {
                    Ok(Ok(adapter)) => {
                        let mut status = board.status.lock().await;
                        status.mode = Mode::Adapter(adapter);
                        status.checkin = true;
                    }
                    Ok(Err(failure)) if !failure.transient() => {
                        board.report_unavailable();
                    }
                    _ => {
                        board.begin_retry(false).await;
                    }
                }
            }
            None => {
                board.begin_retry(true).await;
            }
        }
        board
    }
    fn report_unavailable(&self) {
        let where_state = self
            .options
            .state
            .as_ref()
            .map(|path| format!(" ({})", path.display()))
            .unwrap_or_default();
        crate::stderrln!(
            "pseudolife-mcp: coordination unavailable; memory proxy remains active. Check the daemon's coordination setting, authentication and private adapter state{where_state}."
        );
    }
    async fn configure_registry(&self) {
        #[cfg(feature = "codex-delivery")]
        if self.options.wake {
            let bank = self.runtime.provider.snapshot().ok();
            let enabled = self.options.delivery_url.is_some()
                && self.options.delivery_token.as_ref().is_some_and(|token| {
                    bank.as_ref()
                        .and_then(|snapshot| snapshot.token())
                        .is_some_and(|bank| token != bank)
                });
            self.bridge_enabled.store(enabled, Ordering::Release);
            if !enabled {
                crate::stderrln!(
                    "pseudolife-mcp: Codex live delivery requires an authenticated bridge with a separate host credential; using pull coordination."
                );
            }
        }
        #[cfg(not(feature = "codex-delivery"))]
        if self.options.wake {
            crate::stderrln!(
                "pseudolife-mcp: Codex live delivery unavailable; using pull coordination."
            );
        }
        let setting = self
            .options
            .doorbell_setting
            .as_deref()
            .unwrap_or("")
            .trim();
        let explicit = policy::yes(setting);
        if setting.is_empty() || explicit {
            let command =
                doorbell::resolve_command(|key| self.options.command_environment.get(key).cloned());
            if let Some(command) = command {
                *self.doorbell.lock().await = Some(doorbell::Doorbell::new(
                    command,
                    self.options.digest_root.clone(),
                ));
            } else if explicit {
                let reason = if self
                    .options
                    .command_environment
                    .get("PSEUDOLIFE_CODEX_BIN")
                    .is_some_and(|value| !value.trim().is_empty())
                {
                    "PSEUDOLIFE_CODEX_BIN is not an absolute path to an existing file"
                } else {
                    "no codex CLI found on PATH"
                };
                crate::stderrln!(
                    "pseudolife-mcp: Codex board doorbell off ({reason}); using pull coordination."
                );
            }
        }
    }
    fn report_doorbell_off(&self, late: bool) {
        if self.options.codex
            && self
                .options
                .doorbell_setting
                .as_deref()
                .is_some_and(policy::yes)
        {
            if late {
                crate::stderrln!(
                    "pseudolife-mcp: PSEUDOLIFE_CODEX_DOORBELL needs agent coordination, which the daemon does not serve this bearer; doorbell off."
                );
            } else {
                let needs = if self.options.setting.is_empty() {
                    "agent coordination, which is off here (no bearer token, or the daemon does not serve the board to it)"
                } else {
                    "PSEUDOLIFE_AGENT_COORDINATION=1"
                };
                crate::stderrln!(
                    "pseudolife-mcp: PSEUDOLIFE_CODEX_DOORBELL needs {needs}; doorbell off."
                );
            }
        }
    }
    fn config(&self, thread: Option<&str>, parent: Option<String>) -> Config {
        let session = thread.unwrap_or(&self.runtime.session.uid);
        let state = if self.options.codex {
            Some(identity::bound_state_path(
                &self.options.state_root,
                &self.runtime.url,
                session,
            ))
        } else {
            self.options.state.clone().or_else(|| {
                self.options
                    .session_state_root
                    .then_some(self.options.host_session.as_deref())
                    .flatten()
                    .map(|session| {
                        identity::bound_state_path(
                            &self.options.state_root,
                            &self.runtime.url,
                            session,
                        )
                    })
            })
        };
        Config {
            url: self.runtime.url.clone(),
            provider: self.runtime.provider.clone(),
            state,
            digest: (if self.options.codex {
                Some(session)
            } else {
                self.options.host_session.as_deref()
            })
            .map(|session| {
                self.options
                    .digest_root
                    .join(format!("{}.txt", identity::hex_hash(session)))
            }),
            label: self.options.label.clone(),
            project: self.options.project.clone(),
            task: self.options.task.clone(),
            episode: session.to_owned(),
            codex: self.options.codex,
            wake: self.options.channel && self.options.wake,
            ring: !self.options.codex && self.options.host_session.is_some(),
            parent,
            initial_snapshot: None,
            legacy_state: None,
        }
    }
    pub async fn probe(&self) -> Option<bool> {
        tokio::time::timeout(self.options.timing.probe, self.probe_unbounded())
            .await
            .ok()
            .flatten()
    }
    async fn probe_unbounded(&self) -> Option<bool> {
        let snapshot = self.runtime.provider.snapshot().ok()?;
        let token = snapshot.token()?;
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .ok()?;
        let response = tokio::time::timeout(
            self.options.timing.probe,
            client
                .get(format!("{}/api/hook/coordination-start", self.runtime.url))
                .bearer_auth(token)
                .send(),
        )
        .await
        .ok()?
        .ok()?;
        if self.runtime.provider.require_current(&snapshot).is_err() {
            return None;
        }
        let status = response.status().as_u16();
        if status >= 500 || matches!(status, 408 | 429) {
            return None;
        }
        if status >= 300 {
            return Some(false);
        }
        let body = tokio::time::timeout(self.options.timing.probe, response.bytes())
            .await
            .ok()?
            .ok()?;
        Some(body.iter().any(|byte| !byte.is_ascii_whitespace()))
    }
    async fn begin_retry(self: &Arc<Self>, probe: bool) {
        self.status.lock().await.mode = Mode::Pending;
        if probe {
            if self.options.codex {
                crate::stderrln!(
                    "pseudolife-mcp: the daemon did not answer the board check at startup; memory proxy remains active and the check is retried in the background."
                );
            } else {
                crate::stderrln!(
                    "pseudolife-mcp: the daemon did not answer the board check at startup; memory proxy remains active and the check and registration are retried in the background."
                );
            }
        } else {
            self.status.lock().await.checkin = true;
            crate::stderrln!(
                "pseudolife-mcp: coordination registration did not complete at startup (daemon unreachable or slow); memory proxy remains active and registration is retried in the background."
            );
        }
        let board = self.clone();
        self.tasks.spawn(async move {
            let mut ask_board = probe;
            for attempt in 0usize.. {
                tokio::select! { _ = board.cancel.cancelled() => return, _ = tokio::time::sleep(board.options.timing.retry_delays[attempt.min(5)]) => {} }
                if board.closing.load(Ordering::Acquire) { return; }
                if ask_board {
                    let probed = tokio::select! {
                        _ = board.cancel.cancelled() => return,
                        value = probe_before_registry(&board.closing, board.probe(), |answer| {
                            let registry = (answer == Some(true) && board.options.codex)
                                .then(|| board.configure_registry());
                            (answer, registry)
                        }) => value,
                    };
                    let Some((answer, registry)) = probed else { return; };
                    match answer {
                        None => continue,
                        Some(false) => { board.status.lock().await.mode = Mode::Off;board.report_doorbell_off(true); return; }
                        Some(true) if board.options.codex => {
                            if let Some(registry) = registry {
                                registry.await;
                            }
                            let mut status = board.status.lock().await;
                            status.mode = Mode::Registry;
                            status.note = Some(policy::REGISTERED);
                            crate::stderrln!("pseudolife-mcp: the daemon serves the board after a startup delay; Codex threads register on their next call.");
                            return;
                        }
                        Some(true) => {ask_board = false;}
                    }
                }
                let result = tokio::select! { _ = board.cancel.cancelled() => return, value = tokio::time::timeout(board.options.timing.retry_attempt,Adapter::enter(board.config(None,None))) => value };
                match result {
                    Ok(Ok(adapter)) => {
                        if board.closing.load(Ordering::Acquire) { adapter.close().await; return; }
                        let mut status = board.status.lock().await; status.mode = Mode::Adapter(adapter); status.note = Some(policy::REGISTERED);crate::stderrln!("pseudolife-mcp: coordination registered after a startup delay; board tools are available."); return;
                    }
                    Ok(Err(failure)) if !failure.transient() => { let mut status = board.status.lock().await; status.mode = Mode::Stopped; status.note = Some(policy::STOPPED);board.report_unavailable(); return; }
                    _ => {}
                }
            }
        });
    }
    pub async fn board_checkin(&self) -> bool {
        self.status.lock().await.checkin
    }
    pub fn instructions_note(&self) -> &str {
        if self.options.shared {
            policy::SHARED_NOTE
        } else {
            ""
        }
    }
    pub async fn operation_context(&self) -> Result<OperationContext, ErrorData> {
        let snapshot = self
            .runtime
            .provider
            .snapshot()
            .map_err(|_| credential_error())?;
        let mut headers = self
            .runtime
            .session
            .headers(&snapshot, None)
            .map_err(|_| credential_error())?;
        let adapter = {
            let status = self.status.lock().await;
            if let Mode::Adapter(adapter) = &status.mode {
                Some(adapter.clone())
            } else {
                None
            }
        };
        if let Some(adapter) = adapter
            && let Ok(identity) = adapter.validated_headers(&snapshot).await
        {
            for (name, value) in identity {
                if let Some(name) = name
                    && name.as_str().starts_with("x-pl-")
                {
                    headers.insert(name, value);
                }
            }
        }
        self.runtime
            .provider
            .require_current(&snapshot)
            .map_err(|_| credential_error())?;
        Ok(OperationContext { snapshot, headers })
    }
    async fn thread_adapter(
        &self,
        thread: &str,
        parent: Option<String>,
        snapshot: &crate::credentials::CredentialSnapshot,
    ) -> Result<Arc<Adapter>, String> {
        let entry = {
            let mut threads = self.threads.lock().await;
            if self.closing.load(Ordering::Acquire) {
                return Err("Coordination: session is closing.".into());
            }
            if !threads.contains_key(thread) && threads.len() >= 128 {
                if !self.capacity_reported.swap(true, Ordering::AcqRel) {
                    crate::stderrln!(
                        "pseudolife-mcp: coordination registry capacity reached; memory proxy remains active."
                    );
                }
                return Err(
                    "Coordination: registry capacity reached; ordinary memory remains available."
                        .into(),
                );
            }
            threads
                .entry(thread.to_owned())
                .or_insert_with(|| {
                    Arc::new(Mutex::new(ThreadEntry {
                        adapter: None,
                        #[cfg(feature = "codex-delivery")]
                        delivery: None,
                        retry: None,
                        late_note: false,
                        delivery_failed: Arc::new(AtomicBool::new(false)),
                        failure_hint: String::new(),
                    }))
                })
                .clone()
        };
        let mut entry = entry.lock().await;
        if let Some(adapter) = entry.adapter.clone() {
            let validation = tokio::select! {_ = self.cancel.cancelled() => return Err("Coordination: session is closing.".into()), value = tokio::time::timeout(self.options.timing.startup, adapter.validated_headers(snapshot)) => value};
            return match validation {
                Ok(Ok(_)) => {
                    entry.failure_hint.clear();
                    Ok(adapter)
                }
                Ok(Err(failure)) => {
                    entry.failure_hint = policy::registry_failure_hint(failure.code).into();
                    Err(entry.failure_hint.clone())
                }
                Err(_) => {
                    entry.failure_hint =
                        policy::registry_failure_hint("attachment_unavailable").into();
                    Err(entry.failure_hint.clone())
                }
            };
        }
        if entry.retry.as_ref().is_some_and(|(generation, time)| {
            generation == snapshot.generation()
                && time.elapsed() < self.options.timing.registry_retry
        }) {
            return Err(entry.failure_hint.clone());
        }
        #[cfg(feature = "codex-delivery")]
        if let Some(delivery) = entry.delivery.take() {
            delivery.close().await;
        }
        let mut config = self.config(Some(thread), parent);
        config.initial_snapshot = Some(snapshot.clone());
        config.legacy_state = snapshot.token().map(|token| {
            identity::legacy_state_path(&self.options.state_root, &self.runtime.url, token, thread)
        });
        #[cfg(feature = "codex-delivery")]
        if self.bridge_enabled.load(Ordering::Acquire) {
            let connected = tokio::select! {_=self.cancel.cancelled()=>return Err("Coordination: session is closing.".into()),result=tokio::time::timeout(self.options.timing.startup,delivery::Delivery::connect(self.options.delivery_url.as_deref().unwrap_or(""),self.options.delivery_token.as_deref().unwrap_or(""),thread))=>result};
            match connected {
                Ok(Ok(delivery)) => {
                    entry.delivery = Some(delivery.clone());
                    let verified = tokio::select! {_=self.cancel.cancelled()=>false,result=tokio::time::timeout(self.options.timing.startup,delivery.verify())=>matches!(result,Ok(Ok(())))};
                    if verified {
                        entry.delivery = Some(delivery);
                        config.wake = true;
                    } else {
                        entry.delivery = None;
                        delivery.close().await;
                        if !self.bridge_setup_reported.swap(true, Ordering::AcqRel) {
                            crate::stderrln!(
                                "pseudolife-mcp: Codex live delivery unavailable; using pull coordination."
                            );
                        }
                    }
                }
                _ => {
                    if !self.bridge_setup_reported.swap(true, Ordering::AcqRel) {
                        crate::stderrln!(
                            "pseudolife-mcp: Codex live delivery unavailable; using pull coordination."
                        );
                    }
                }
            }
        }
        let doorbell = self.doorbell.lock().await.clone();
        config.ring = doorbell.is_some();
        let result = tokio::select! {_=self.cancel.cancelled()=>return Err("Coordination: session is closing.".into()),result=tokio::time::timeout(self.options.timing.startup,Adapter::enter(config))=>result};
        let failure_code = match &result {
            Ok(Err(failure)) => failure.code,
            _ => "attachment_unavailable",
        };
        if let Ok(Ok(adapter)) = result {
            if self.closing.load(Ordering::Acquire) {
                adapter.close().await;
                return Err("Coordination: session is closing.".into());
            }
            entry.adapter = Some(adapter.clone());
            entry.retry = None;
            entry.failure_hint.clear();
            self.registry_failure_reported
                .store(false, Ordering::Release);
            entry.late_note = self.status.lock().await.note == Some(policy::REGISTERED);
            #[cfg(feature = "codex-delivery")]
            if let Some(delivery) = &entry.delivery {
                let (owned, delivery, cancel) =
                    (adapter.clone(), delivery.clone(), self.cancel.clone());
                let failed = entry.delivery_failed.clone();
                let thread = thread.to_owned();
                let retained = adapter.clone();
                self.tasks.spawn(async move {
                    let stopped=tokio::select! {_=cancel.cancelled()=>false,stopped=owned.pump(delivery.clone())=>stopped};
                    if stopped && !cancel.is_cancelled() {failed.store(true,Ordering::Release);if let Some(doorbell)=doorbell {doorbell.watch(&thread,retained,true).await;}}
                    delivery.close().await;
                });
            } else if let Some(doorbell) = doorbell {
                doorbell.watch(thread, adapter.clone(), false).await;
            }
            #[cfg(not(feature = "codex-delivery"))]
            if let Some(doorbell) = doorbell {
                doorbell.watch(thread, adapter.clone(), false).await;
            }
            Ok(adapter)
        } else {
            #[cfg(feature = "codex-delivery")]
            if let Some(delivery) = entry.delivery.take() {
                delivery.close().await;
            }
            entry.retry = Some((snapshot.generation().clone(), tokio::time::Instant::now()));
            entry.failure_hint = policy::registry_failure_hint(failure_code).into();
            if !self.registry_failure_reported.swap(true, Ordering::AcqRel) {
                crate::stderrln!(
                    "pseudolife-mcp: coordination unavailable; memory proxy remains active. Check the daemon's coordination setting, authentication and private adapter state."
                );
            }
            Err(entry.failure_hint.clone())
        }
    }
    pub async fn prepare_call(
        &self,
        name: &str,
        arguments: Option<Map<String, Value>>,
        meta: &Value,
    ) -> Result<Preparation, ErrorData> {
        if self.closing.load(Ordering::Acquire) {
            return Err(coordination_error(None, None));
        }
        let arguments = arguments.unwrap_or_default();
        let required = policy::requires_identity(name, &Value::Object(arguments.clone()));
        if self.options.shared && required {
            return Err(coordination_error(Some(policy::SHARED_REFUSAL), None));
        }
        let original = arguments.clone();
        let arguments = match claims::prepare_arguments(name, arguments).await {
            Ok(arguments) => arguments,
            Err(code) => {
                return Ok(Preparation::Result(CallToolResult::error(vec![
                    rmcp::model::ContentBlock::text(code),
                ])));
            }
        };
        let snapshot = self
            .runtime
            .provider
            .snapshot()
            .map_err(|_| credential_error())?;
        let thread = self
            .options
            .codex
            .then(|| policy::thread_id(meta))
            .flatten();
        let mut headers = self
            .runtime
            .session
            .headers(&snapshot, thread.as_deref())
            .map_err(|_| credential_error())?;
        let (adapter, registry, pending) = {
            let status = self.status.lock().await;
            (
                if let Mode::Adapter(adapter) = &status.mode {
                    Some(adapter.clone())
                } else {
                    None
                },
                matches!(status.mode, Mode::Registry),
                matches!(status.mode, Mode::Pending),
            )
        };
        if pending && required {
            return Err(coordination_error(Some(policy::PENDING), None));
        }
        let mut registry_hint = None;
        let mut adapter = if registry {
            if let Some(thread) = &thread {
                match self
                    .thread_adapter(thread, policy::parent_thread(meta, thread), &snapshot)
                    .await
                {
                    Ok(adapter) => Some(adapter),
                    Err(hint) if required => return Err(coordination_error(None, Some(hint))),
                    Err(hint) => {
                        registry_hint = Some(hint);
                        None
                    }
                }
            } else if required {
                return Err(coordination_error(None, None));
            } else {
                None
            }
        } else {
            adapter
        };
        if let Some(bound) = adapter.clone() {
            match bound.validated_headers(&snapshot).await {
                Ok(identity) => {
                    for (name, value) in identity {
                        if let Some(name) = name
                            && name.as_str().starts_with("x-pl-")
                        {
                            headers.insert(name, value);
                        }
                    }
                    bound.note_turn().await;
                    self.status.lock().await.validated = true;
                }
                Err(_) if required => {
                    return Err(coordination_error(None, bound.deliver_hint().await));
                }
                Err(failure) => {
                    if registry {
                        registry_hint = Some(policy::registry_failure_hint(failure.code).into());
                    }
                    adapter = None;
                }
            }
        }
        self.runtime
            .provider
            .require_current(&snapshot)
            .map_err(|_| credential_error())?;
        if let (Some(thread), Some(doorbell)) = (&thread, self.doorbell.lock().await.clone()) {
            doorbell
                .note_call(thread, name, &Value::Object(original.clone()), false)
                .await;
        }
        Ok(Preparation::Forward(PreparedCall {
            arguments,
            operation: OperationContext { snapshot, headers },
            adapter,
            thread,
            name: name.to_owned(),
            original,
            registry_hint,
        }))
    }
    pub async fn finish_call(
        &self,
        call: &PreparedCall,
        result: &mut CallToolResult,
    ) -> Option<String> {
        let mut hints = vec![];
        if let Some(hint) = &call.registry_hint {
            hints.push(hint.clone());
        }
        {
            let mut status = self.status.lock().await;
            if (status.note == Some(policy::STOPPED) || (status.validated && !self.options.codex))
                && let Some(note) = status.note.take()
            {
                hints.push(note.to_owned());
            }
        }
        if let Some(thread) = &call.thread {
            let entry = self.threads.lock().await.get(thread).cloned();
            if let Some(entry) = entry {
                let mut entry = entry.lock().await;
                if call.adapter.is_some() && std::mem::take(&mut entry.late_note) {
                    hints.push(policy::REGISTERED.to_owned());
                }
            }
        }
        let failed = if let Some(thread) = &call.thread {
            let entry = self.threads.lock().await.get(thread).cloned();
            if let Some(entry) = entry {
                entry.lock().await.delivery_failed.load(Ordering::Acquire)
            } else {
                false
            }
        } else {
            false
        };
        if failed {
            hints.push("Coordination: live Codex delivery stopped; use memory_message receive for pull delivery.".into());
        } else if let Some(adapter) = &call.adapter
            && let Some(hint) = adapter.deliver_hint().await
        {
            hints.push(hint);
        }
        // Doorbell receive coverage is recorded only after a successful upstream call.
        if result.is_error != Some(true)
            && let (Some(thread), Some(doorbell)) =
                (&call.thread, self.doorbell.lock().await.clone())
        {
            doorbell
                .note_call(
                    thread,
                    &call.name,
                    &Value::Object(call.original.clone()),
                    true,
                )
                .await;
        }
        if hints.is_empty() {
            None
        } else {
            let hint = hints.join("\n");
            policy::append_hint(result, &hint);
            Some(hint)
        }
    }
    pub async fn close(&self) {
        if self.closing.swap(true, Ordering::AcqRel) {
            return;
        }
        self.cancel.cancel();
        self.tasks.close();
        self.tasks.wait().await;
        if let Some(doorbell) = self.doorbell.lock().await.take() {
            doorbell.close().await;
        }
        let adapter = {
            let mut status = self.status.lock().await;
            let mode = std::mem::replace(&mut status.mode, Mode::Off);
            if let Mode::Adapter(adapter) = mode {
                Some(adapter)
            } else {
                None
            }
        };
        if let Some(adapter) = adapter {
            adapter.close().await;
        }
        let entries = self
            .threads
            .lock()
            .await
            .drain()
            .map(|(_, entry)| entry)
            .collect::<Vec<_>>();
        for entry in entries.into_iter().rev() {
            let mut entry = entry.lock().await;
            #[cfg(feature = "codex-delivery")]
            if let Some(delivery) = entry.delivery.take() {
                delivery.close().await;
            }
            if let Some(adapter) = entry.adapter.take() {
                adapter.close().await;
            }
        }
    }
}

#[cfg(test)]
mod registry_probe_tests {
    use super::*;

    #[tokio::test]
    async fn probe_answered_as_close_begins_builds_no_registry() {
        let closing = AtomicBool::new(false);
        let mut built = Vec::new();
        let result = probe_before_registry(
            &closing,
            async {
                closing.store(true, Ordering::Release);
                Some(true)
            },
            |answer| {
                assert_eq!(answer, Some(true));
                built.push(1);
            },
        )
        .await;
        assert_eq!(built, Vec::<i32>::new());
        assert!(result.is_none());
    }

    #[tokio::test]
    async fn open_probe_answer_constructs_the_registry() {
        let closing = AtomicBool::new(false);
        let mut built = Vec::new();
        let result = probe_before_registry(&closing, async { Some(true) }, |answer| {
            assert_eq!(answer, Some(true));
            built.push(1);
        })
        .await;
        assert!(result.is_some());
        assert_eq!(built, vec![1]);
    }
}
