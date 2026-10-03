use super::{
    adapter::{Adapter, View},
    notice::PendingNotice,
    state,
};
use process_wrap::tokio::*;
use serde_json::Value;
use std::{
    collections::{HashMap, HashSet},
    io::Write,
    path::{Path, PathBuf},
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::sync::Mutex;
use tokio_util::{sync::CancellationToken, task::TaskTracker};

#[cfg(windows)]
#[path = "doorbell_windows.rs"]
pub mod windows_process;

#[cfg(unix)]
#[path = "doorbell_posix.rs"]
mod posix_process;

pub fn resolve_command(mut env: impl FnMut(&str) -> Option<String>) -> Option<PathBuf> {
    if let Some(explicit) = env("PSEUDOLIFE_CODEX_BIN").filter(|value| !value.trim().is_empty()) {
        let path = crate::credentials::expand_user(Path::new(explicit.trim())).ok()?;
        return (path.is_absolute() && path.is_file()).then_some(path);
    }
    let separator = if cfg!(windows) { ';' } else { ':' };
    let names = if cfg!(windows) {
        env("PATHEXT")
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD".into())
            .split(';')
            .filter(|extension| {
                matches!(
                    extension.to_lowercase().as_str(),
                    ".com" | ".exe" | ".bat" | ".cmd"
                )
            })
            .map(|extension| format!("codex{extension}"))
            .collect::<Vec<_>>()
    } else {
        vec!["codex".into()]
    };
    for directory in env("PATH").unwrap_or_default().split(separator) {
        let directory = Path::new(directory.trim().trim_matches('"'));
        if !directory.is_absolute() {
            continue;
        }
        for name in &names {
            let path = directory.join(name);
            #[cfg(unix)]
            let executable = {
                use std::os::unix::fs::PermissionsExt;
                path.metadata()
                    .is_ok_and(|meta| meta.permissions().mode() & 0o111 != 0)
            };
            #[cfg(windows)]
            let executable = true;
            if path.is_file() && executable {
                return Some(path);
            }
        }
    }
    #[cfg(windows)]
    {
        if let Some(local) = env("LOCALAPPDATA")
            .map(|value| value.trim().trim_matches('"').to_owned())
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
        {
            let root = local.join("OpenAI/Codex/bin");
            let mut builds = std::fs::read_dir(root)
                .ok()?
                .filter_map(Result::ok)
                .map(|entry| entry.path().join("codex.exe"))
                .filter(|path| path.is_file())
                .collect::<Vec<_>>();
            builds.sort_by_key(|path| {
                std::cmp::Reverse(path.metadata().and_then(|meta| meta.modified()).ok())
            });
            return builds.into_iter().next();
        }
    }
    None
}
pub struct BellState {
    known: HashSet<String>,
    newest: f64,
    last_count: u64,
    complete: bool,
    last_call: tokio::time::Instant,
    arrival: u64,
    covered: u64,
    outstanding: bool,
    last_ring: Option<(String, String, f64)>,
    pending: PendingNotice,
    watch_cancel: CancellationToken,
    adapter: Arc<Adapter>,
}
pub struct Doorbell {
    command: PathBuf,
    directory: PathBuf,
    quiet: Duration,
    timeout: Duration,
    bells: Mutex<HashMap<String, BellState>>,
    disabled: AtomicBool,
    cancel: CancellationToken,
    tasks: TaskTracker,
}
fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
impl Doorbell {
    pub fn new(command: PathBuf, directory: PathBuf) -> Arc<Self> {
        Self::with_timing(
            command,
            directory,
            Duration::from_secs(30),
            Duration::from_secs(20),
        )
    }
    pub fn with_timing(
        command: PathBuf,
        directory: PathBuf,
        quiet: Duration,
        timeout: Duration,
    ) -> Arc<Self> {
        Arc::new(Self {
            command,
            directory,
            quiet,
            timeout,
            bells: Mutex::new(HashMap::new()),
            disabled: AtomicBool::new(false),
            cancel: CancellationToken::new(),
            tasks: TaskTracker::new(),
        })
    }
    pub async fn watch(self: &Arc<Self>, thread: &str, adapter: Arc<Adapter>, shown: bool) {
        let _ = self.watch_checked(thread, adapter, shown).await;
    }
    /// Attach a watch, refusing noncanonical thread identifiers before subscribing.
    pub async fn watch_checked(
        self: &Arc<Self>,
        thread: &str,
        adapter: Arc<Adapter>,
        shown: bool,
    ) -> std::io::Result<()> {
        let pending = PendingNotice::new(&self.directory, thread).map_err(|_| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "doorbell needs a canonical Codex thread id",
            )
        })?;
        if self.cancel.is_cancelled() {
            return Ok(());
        }
        // Subscribe before taking the baseline so an intervening update remains observable.
        let mut changes = adapter.subscribe();
        let view = adapter.view().await;
        let mut bells = self.bells.lock().await;
        if self.cancel.is_cancelled() {
            return Ok(());
        }
        if let Some(previous) = bells.remove(thread) {
            previous.watch_cancel.cancel();
        }
        let arrival = if view.count.is_some_and(|count| count > 0) {
            view.watermark
        } else {
            0
        };
        let covered = if shown {
            arrival
        } else {
            arrival.saturating_sub(1)
        };
        let outstanding = state::exists(&pending.path);
        let watch_cancel = self.cancel.child_token();
        let listener_cancel = watch_cancel.clone();
        let listener = Arc::downgrade(self);
        adapter.set_ring_listener(Arc::new(move || {
            !listener_cancel.is_cancelled()
                && listener.upgrade().is_some_and(|doorbell| {
                    !doorbell.disabled.load(Ordering::Acquire) && !doorbell.cancel.is_cancelled()
                })
        }));
        bells.insert(
            thread.into(),
            BellState {
                known: view
                    .preview
                    .iter()
                    .filter_map(|entry| entry["message_id"].as_str().map(str::to_owned))
                    .collect(),
                newest: view
                    .preview
                    .iter()
                    .filter_map(|entry| entry["created_at"].as_f64())
                    .fold(0.0, f64::max),
                last_count: view.count.unwrap_or(0),
                complete: view.count.unwrap_or(0) <= view.preview.len() as u64,
                last_call: tokio::time::Instant::now(),
                arrival,
                covered,
                outstanding,
                last_ring: None,
                pending,
                watch_cancel: watch_cancel.clone(),
                adapter: adapter.clone(),
            },
        );
        drop(bells);
        let thread = thread.to_owned();
        let doorbell = self.clone();
        self.tasks.spawn(async move {
            doorbell.observe_watch(&thread,&view,Some(&watch_cancel)).await;
            loop {
                tokio::select! {_=watch_cancel.cancelled()=>break,result=changes.changed()=>if result.is_err() {break;}}
                let view=adapter.view().await;doorbell.observe_watch(&thread,&view,Some(&watch_cancel)).await;
            }
        });
        Ok(())
    }
    pub async fn note_call(&self, thread: &str, name: &str, arguments: &Value, succeeded: bool) {
        if let Some(bell) = self.bells.lock().await.get_mut(thread) {
            bell.last_call = tokio::time::Instant::now();
            if succeeded
                && name == "memory_message"
                && arguments.get("action").and_then(Value::as_str) == Some("receive")
            {
                bell.covered = bell.covered.max(bell.arrival);
            }
        }
    }
    pub async fn observe(self: &Arc<Self>, thread: &str, view: &View) {
        self.observe_watch(thread, view, None).await;
    }
    async fn observe_watch(
        self: &Arc<Self>,
        thread: &str,
        view: &View,
        watch: Option<&CancellationToken>,
    ) {
        if self.cancel.is_cancelled() {
            return;
        }
        let mut bells = self.bells.lock().await;
        if watch.is_some_and(CancellationToken::is_cancelled) {
            return;
        }
        let Some(bell) = bells.get_mut(thread) else {
            return;
        };
        if bell.outstanding
            && let Some(resolution) = bell.pending.resolution(now())
        {
            bell.outstanding = false;
            if resolution == "unresolved_expired"
                && let Some(basis) = expired_basis(&bell.pending)
            {
                self.note_expiry(&bell.adapter, thread, &basis).await;
            }
        }
        if self.disabled.load(Ordering::Acquire) {
            return;
        }
        let Some(count) = view.count else { return };
        let unknown = view
            .preview
            .iter()
            .filter(|entry| {
                entry
                    .get("message_id")
                    .and_then(Value::as_str)
                    .is_some_and(|id| !bell.known.contains(id))
            })
            .collect::<Vec<_>>();
        let fresh = unknown.iter().any(|entry| {
            entry["created_at"]
                .as_f64()
                .is_some_and(|time| time > bell.newest)
        });
        let arrived = count > bell.last_count
            || (!unknown.is_empty() && bell.complete)
            || (fresh && count >= bell.last_count);
        bell.known = view
            .preview
            .iter()
            .filter_map(|entry| entry["message_id"].as_str().map(str::to_owned))
            .collect();
        for entry in &view.preview {
            if let Some(time) = entry["created_at"].as_f64() {
                bell.newest = bell.newest.max(time);
            }
        }
        bell.last_count = count;
        bell.complete = count <= view.preview.len() as u64;
        if arrived {
            bell.arrival = view.watermark;
        }
        if count == 0 {
            bell.covered = bell.arrival;
            return;
        }
        if bell.arrival <= bell.covered || bell.outstanding || bell.last_call.elapsed() < self.quiet
        {
            return;
        }
        if view.delivered >= bell.arrival {
            bell.covered = bell.arrival;
            return;
        }
        let Some(wake) = &view.wake else { return };
        if view
            .wake_watermark
            .is_none_or(|watermark| view.delivered >= watermark)
        {
            return;
        }
        let decision = wake.get("decision").and_then(Value::as_str);
        let reason = wake.get("reason").and_then(Value::as_str);
        let ring_at = wake
            .get("ring_at")
            .and_then(Value::as_f64)
            .filter(|time| time.is_finite());
        let (Some(decision), Some(reason), Some(ring_at)) = (decision, reason, ring_at) else {
            return;
        };
        if !matches!(decision, "rung" | "attention")
            || (decision == "attention"
                && (wake.get("queue_allowed") != Some(&Value::Bool(true))
                    || wake.get("recipient_state").and_then(Value::as_str) != Some("unknown")))
        {
            return;
        }
        let ring = (
            decision.to_owned(),
            super::liveness::reason(reason),
            ring_at,
        );
        if now() < ring_at || bell.last_ring.as_ref() == Some(&ring) {
            return;
        }
        let expiry = wake
            .get("message_expires_at")
            .filter(|value| !value.is_null())
            .map(|value| value.as_f64().unwrap_or(f64::NAN));
        let previous_expiry = state::read(
            &bell.pending.path.with_extension("bell-unresolved-expired"),
            8192,
        )
        .ok();
        let notice = bell
            .pending
            .reserve(count, expiry, now(), decision == "attention");
        let current_expiry = state::read(
            &bell.pending.path.with_extension("bell-unresolved-expired"),
            8192,
        )
        .ok();
        if current_expiry != previous_expiry
            && let Some(basis) = expired_basis(&bell.pending)
        {
            self.note_expiry(&bell.adapter, thread, &basis).await;
        }
        let Some(notice) = notice else {
            bell.outstanding = state::exists(&bell.pending.path);
            self.note_delivery(
                &bell.adapter,
                thread,
                if bell.outstanding {
                    "bell_pending"
                } else {
                    "bell_deferred"
                },
                0,
                if bell.outstanding {
                    "unresolved queue transport"
                } else {
                    "reservation_unavailable no_queue_attempt"
                },
            )
            .await;
            return;
        };
        bell.last_ring = Some(ring.clone());
        bell.covered = bell.arrival;
        bell.outstanding = true;
        let thread = thread.to_owned();
        let doorbell = self.clone();
        let adapter = bell.adapter.clone();
        self.tasks.spawn(async move {
            doorbell.queue(&thread, &notice, &adapter, &ring).await;
        });
    }
    async fn note_expiry(&self, adapter: &Adapter, thread: &str, basis: &str) {
        self.note_delivery(adapter,thread,"unresolved_expired",0,
            if basis=="legacy_upper_bound" {"availability_fallback native_cancellation_unknown origin_expiry_unknown legacy_upper_bound"}
            else {"availability_fallback native_cancellation_unknown origin_message_expiry"}).await;
    }
    async fn note_delivery(
        &self,
        adapter: &Adapter,
        thread: &str,
        kind: &str,
        size: usize,
        reason: &str,
    ) {
        // Board constructs both paths from Options::digest_root and the thread digest key.
        let watermark = adapter.view().await.watermark;
        let key = super::identity::hex_hash(thread);
        let row = format!(
            "{}\t{kind}\t{}\t{watermark}\t{size}\t{reason}\n",
            now() as u64,
            &key[..8]
        );
        if let Ok(mut ledger) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.directory.join("ledger.log"))
        {
            let _ = ledger.write_all(row.as_bytes());
        }
    }
    fn disable(&self, reason: &str) {
        if !self.disabled.swap(true, Ordering::AcqRel) {
            crate::stderrln!(
                "pseudolife-mcp: Codex board doorbell off ({reason}); pull delivery and tool-result hints continue."
            );
        }
    }
    async fn queue(
        &self,
        thread: &str,
        notice: &Value,
        adapter: &Adapter,
        ring: &(String, String, f64),
    ) {
        if self.cancel.is_cancelled() {
            return;
        }
        let mut command = CommandWrap::with_new(&self.command, |command| {
            command
                .args([
                    "queue",
                    "--thread",
                    thread,
                    "--message",
                    notice["text"].as_str().unwrap_or(""),
                ])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            for (key, _) in std::env::vars_os() {
                if key
                    .to_string_lossy()
                    .to_uppercase()
                    .starts_with("PSEUDOLIFE_")
                {
                    command.env_remove(key);
                }
            }
        });
        #[cfg(unix)]
        {
            command.wrap(ProcessSession);
            command.wrap(posix_process::ExecveOnly);
        }
        #[cfg(unix)]
        let spawned = command.spawn().map_err(|error| {
            let unstarted = definitely_unstarted(&error);
            (error, unstarted)
        });
        #[cfg(windows)]
        let spawned = windows_process::QueueProcess::spawn(command.command_mut())
            .await
            .map_err(|failure| (failure.error, failure.unstarted));
        let mut child = match spawned {
            Ok(child) => child,
            Err((_error, unstarted)) => {
                if unstarted
                    && let Some(bell) = self.bells.lock().await.get_mut(thread)
                    && bell.pending.rollback_unstarted(notice, now())
                {
                    bell.outstanding = false;
                }
                self.disable("could not start the Codex CLI: OSError");
                return;
            }
        };
        // Wait only for the queue leader; a successful launcher may leave its worker alive.
        #[cfg(unix)]
        let result = tokio::select! {_=self.cancel.cancelled()=>None,result=tokio::time::timeout(self.timeout,child.inner_mut().wait())=>Some(result)};
        #[cfg(windows)]
        let result = tokio::select! {_=self.cancel.cancelled()=>None,result=tokio::time::timeout(self.timeout,child.wait())=>Some(result)};
        match result {
            Some(Ok(Ok(status))) if status.success() => {
                let prompt_seen = self
                    .bells
                    .lock()
                    .await
                    .get(thread)
                    .is_some_and(|bell| bell.pending.accept(notice, now()));
                self.note_delivery(
                    adapter,
                    thread,
                    "bell",
                    notice["text"].as_str().unwrap_or("").chars().count() + 1,
                    &format!(
                        "{} {} queue_accepted {} recipient_state_unknown",
                        ring.0,
                        ring.1,
                        if prompt_seen {
                            "prompt_seen"
                        } else {
                            "pending"
                        }
                    ),
                )
                .await;
            }
            Some(Ok(Ok(status))) => self.disable(&format!(
                "codex queue exited with status {}",
                status.code().unwrap_or(-1)
            )),
            _ => {
                #[cfg(unix)]
                let _ =
                    tokio::time::timeout(Duration::from_secs(5), Box::into_pin(child.kill())).await;
                #[cfg(windows)]
                child.kill().await;
                if !self.cancel.is_cancelled() {
                    self.disable(&format!(
                        "codex queue did not finish within {} s",
                        self.timeout.as_secs_f64()
                    ));
                }
            }
        }
    }
    pub async fn close(&self) {
        self.disabled.store(true, Ordering::Release);
        self.cancel.cancel();
        self.tasks.close();
        self.tasks.wait().await;
        self.bells.lock().await.clear();
    }
}

fn definitely_unstarted(error: &std::io::Error) -> bool {
    #[cfg(windows)]
    if let Some(code) = error.raw_os_error() {
        // CPython's Windows errno conversion, rather than Rust's broader kinds.
        return matches!(code, 2 | 3 | 5 | 11 | 15 | 16 | 18..=36 | 53 | 65 | 67 | 82 | 83 | 108 | 132 | 158 | 161 | 167 | 188..=202 | 206 | 267 | 10013);
    }
    matches!(
        error.kind(),
        std::io::ErrorKind::NotFound
            | std::io::ErrorKind::PermissionDenied
            | std::io::ErrorKind::NotADirectory
    ) || cfg!(unix) && error.raw_os_error() == Some(8)
}

fn expired_basis(pending: &PendingNotice) -> Option<String> {
    let bytes = state::read(
        &pending.path.with_extension("bell-unresolved-expired"),
        8192,
    )
    .ok()?;
    let receipt: Value = serde_json::from_slice(&bytes).ok()?;
    receipt["expiry_basis"]
        .as_str()
        .filter(|basis| matches!(*basis, "message" | "legacy_upper_bound"))
        .map(str::to_owned)
}
