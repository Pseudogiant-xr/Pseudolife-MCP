//! The lazy initialization lifecycle (spec section L, `service.py:1223-1467,
//! 6192-6224`): storage first, then the embedder, then hydration, with the
//! exact failure recording and retry backoff of `MemoryService._ensure_init`.

use crate::bank::Bank;
use crate::config::{Config, DaemonEnv};
use crate::embed::Embedder;
use crate::storage::{OpenError, Storage};
use serde_json::{Value, json};
use std::path::PathBuf;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

/// `INIT_RETRY_BASE_SECONDS`, `INIT_RETRY_MAX_SECONDS` (`service.py:89-90`).
const RETRY_BASE_S: f64 = 5.0;
const RETRY_MAX_S: f64 = 60.0;

/// What a request needs once init has completed.
pub struct Ready {
    /// The writer session, for the write-path slices.
    #[allow(dead_code)]
    pub storage: Arc<Storage>,
    pub embedder: Arc<Embedder>,
    pub bank: Arc<Bank>,
    pub sessions: tokio::sync::Mutex<crate::background_sessions::Sessions>,
}

/// Lock-free view for `/health` (it never waits on init, spec L7).
#[derive(Clone, Default)]
pub struct Snapshot {
    pub init_refusal: Option<String>,
    pub not_ready: Option<String>,
    pub storage: Option<Arc<Storage>>,
    pub embedder: Option<Value>,
}

#[derive(Default)]
struct Inner {
    storage: Option<Arc<Storage>>,
    embedder: Option<Arc<Embedder>>,
    ready: Option<Arc<Ready>>,
    failures: u32,
    backoff_s: f64,
    retry_at: Option<Instant>,
}

pub struct Service {
    pub session_dream: Arc<dyn crate::background_sessions::SessionDream>,
    pub release_check: Arc<crate::release_check::ReleaseCheck>,
    pub config: Config,
    pub env: DaemonEnv,
    pub data_dir: PathBuf,
    pub auth_configured: bool,
    dsn: String,
    model_dir: Option<PathBuf>,
    ort_threads: usize,
    /// The service lock: one init at a time; callers wait on it.
    inner: tokio::sync::Mutex<Inner>,
    snap: RwLock<Snapshot>,
}

impl Service {
    pub fn new(
        config: Config,
        env: DaemonEnv,
        data_dir: PathBuf,
        dsn: String,
        auth_configured: bool,
    ) -> Service {
        let model_dir = std::env::var_os("PSEUDOLIFE_DAEMON_ONNX_DIR").map(PathBuf::from);
        let ort_threads = std::env::var("PSEUDOLIFE_DAEMON_ORT_THREADS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(4);
        Service {
            session_dream: Arc::new(crate::background_sessions::DeferredSessionDream),
            release_check: Arc::new(crate::release_check::ReleaseCheck::default()),
            config,
            env,
            data_dir,
            auth_configured,
            dsn,
            model_dir,
            ort_threads,
            inner: tokio::sync::Mutex::new(Inner::default()),
            snap: RwLock::new(Snapshot::default()),
        }
    }

    pub fn snapshot(&self) -> Snapshot {
        self.snap.read().expect("snapshot lock").clone()
    }

    fn update(&self, f: impl FnOnce(&mut Snapshot)) {
        f(&mut self.snap.write().expect("snapshot lock"));
    }

    /// `_arm_retry_backoff`.
    fn arm_backoff(inner: &mut Inner) {
        if crate::mutants::active("no-backoff") {
            return;
        }
        inner.failures += 1;
        let exp = (inner.failures - 1).min(16);
        inner.backoff_s = RETRY_MAX_S.min(RETRY_BASE_S * 2f64.powi(exp as i32));
        inner.retry_at = Some(Instant::now() + Duration::from_secs_f64(inner.backoff_s));
    }

    /// `_refuse_while_backing_off`.
    fn refuse_while_backing_off(&self, inner: &Inner) -> Result<(), String> {
        let Some(at) = inner.retry_at else {
            return Ok(());
        };
        let now = Instant::now();
        if at <= now {
            return Ok(());
        }
        let snap = self.snapshot();
        let reason = snap
            .not_ready
            .or(snap.init_refusal)
            .unwrap_or_else(|| "None".into());
        let wait = (at - now).as_secs_f64();
        Err(format!(
            "memory bank not ready: {reason} (next retry in {wait:.0}s)"
        ))
    }

    /// `_ensure_postgres_storage`.
    async fn ensure_storage(&self, inner: &mut Inner) -> Result<Arc<Storage>, String> {
        if let Some(s) = &inner.storage {
            return Ok(s.clone());
        }
        self.refuse_while_backing_off(inner)?;
        let storage = match Storage::open(&self.dsn).await {
            Ok(s) => Arc::new(s),
            Err(OpenError::LeaseHeld(msg)) => {
                // Retryable: the holder may leave.
                self.update(|s| s.not_ready = Some(msg.clone()));
                Self::arm_backoff(inner);
                return Err(msg);
            }
            Err(OpenError::Refused(msg)) => {
                // The embedding-dimension refusal: recorded, no backoff.
                self.update(|s| s.init_refusal = Some(msg.clone()));
                return Err(msg);
            }
            // A connection failure records nothing (spec L3).
            Err(OpenError::Failed(msg)) => return Err(msg),
        };
        self.update(|s| {
            s.init_refusal = None;
            s.not_ready = None;
        });
        // A failed search_path check closes the session and records nothing.
        storage.check_search_path().await?;
        inner.storage = Some(storage.clone());
        self.update(|s| s.storage = Some(storage.clone()));
        Ok(storage)
    }

    /// `_ensure_init`, run in its own task: a request whose client goes
    /// away mid-init drops only its wait, never the init itself (which
    /// writes to the shared writer session).
    pub async fn ensure_init(self: &Arc<Self>) -> Result<Arc<Ready>, String> {
        let me = self.clone();
        tokio::spawn(async move { me.ensure_init_inner().await })
            .await
            .map_err(|e| format!("initialization task failed: {e}"))?
    }

    /// `_ensure_init`, cold and fast paths.
    async fn ensure_init_inner(&self) -> Result<Arc<Ready>, String> {
        let mut inner = self.inner.lock().await;
        if let Some(r) = &inner.ready {
            return Ok(r.clone());
        }
        self.refuse_while_backing_off(&inner)?;
        let storage = self.ensure_storage(&mut inner).await?;
        if inner.embedder.is_none() {
            // Built once and kept across failed attempts; a failure here
            // records nothing, as in Python (it is outside the abandon path).
            let dir = self
                .model_dir
                .clone()
                .ok_or_else(|| "PSEUDOLIFE_DAEMON_ONNX_DIR is not set".to_string())?;
            let threads = self.ort_threads;
            let prefix = self.config.embedding.query_prefix.clone();
            let max_tokens = self.config.embedding.max_seq_length.max(1) as usize;
            let embedder = tokio::task::spawn_blocking(move || {
                Embedder::load(&dir, threads, &prefix, max_tokens)
            })
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;
            let embedder = Arc::new(embedder);
            inner.embedder = Some(embedder.clone());
            self.update(|s| {
                s.embedder = Some(json!({"backend": "onnx", "device": "cpu", "dtype": null}))
            });
        }
        let embedder = inner.embedder.clone().expect("embedder built above");
        let hydrated = async {
            let bank = crate::bank::hydrate(storage.client(), &self.config.memory.bands).await?;
            let sessions = crate::background_sessions::Sessions::hydrate(storage.client())
                .await
                .map_err(anyhow::Error::msg)?;
            Ok::<_, anyhow::Error>((bank, sessions))
        }
        .await;
        match hydrated {
            Ok((bank, sessions)) => {
                inner.failures = 0;
                inner.backoff_s = 0.0;
                inner.retry_at = None;
                self.update(|s| {
                    s.init_refusal = None;
                    s.not_ready = None;
                });
                let ready = Arc::new(Ready {
                    storage,
                    embedder,
                    bank: Arc::new(bank),
                    sessions: tokio::sync::Mutex::new(sessions),
                });
                inner.ready = Some(ready.clone());
                Ok(ready)
            }
            Err(e) => {
                if let Some(stale) = e.downcast_ref::<crate::bank::StaleDims>() {
                    let msg = self.stale_dims_message(stale);
                    self.update(|s| s.init_refusal = Some(msg.clone()));
                    let mut inner2 = inner;
                    self.update(|s| s.not_ready = None);
                    Self::arm_backoff(&mut inner2);
                    return Err(msg);
                }
                // `_abandon_partial_init`: retryable unless it is the refusal
                // already recorded.
                // `hydrate_cms` failures are re-raised as this RuntimeError (service.py:1543).
                let reason = format!("entry hydration failed: {e}");
                let refusal = self.snapshot().init_refusal;
                self.update(|s| {
                    s.not_ready = if refusal.as_deref() == Some(reason.as_str()) {
                        None
                    } else {
                        Some(reason.clone())
                    }
                });
                Self::arm_backoff(&mut inner);
                Err(reason)
            }
        }
    }

    /// `_refuse_on_stale_hydrated_dims`'s message.
    fn stale_dims_message(&self, s: &crate::bank::StaleDims) -> String {
        let dims: Vec<String> = s.dims.iter().map(|d| d.to_string()).collect();
        format!(
            "Refusing to serve: {} hydrated row(s) in the bank at {} are embedded at {} dims, but the live \
             embedder ({}) produces {}-d vectors — every search/store would crash with a torch shape error. \
             If this daemon was started by accident against an old or retired bank (e.g. a shim-spawned \
             fallback while the real Docker daemon was still booting), stop it and point the client at the \
             intended daemon. Otherwise migrate deliberately: a file-mode bank is re-embedded on import when \
             the daemon is given Postgres storage (set PSEUDOLIFE_MCP_DATABASE_URL, or install \
             pseudolife-mcp[lite]); a Postgres bank migrates with `python ops/migrate_embeddings.py`. Or \
             configure the embedding model that produced these vectors.",
            s.count,
            self.data_dir.display(),
            dims.join(", "),
            self.config.embedding.model_name,
            crate::bank::DIM,
        )
    }

    /// `MemoryService.warmup`: retry only a retryable failure whose window
    /// is still below the cap; anything else waits for the next caller.
    pub async fn warmup(&self) {
        loop {
            match self.ensure_init_inner().await {
                Ok(_) => return,
                Err(e) => {
                    eprintln!("warmup init failed: {e}");
                    let (wait, backoff) = {
                        let inner = self.inner.lock().await;
                        let wait = inner
                            .retry_at
                            .map(|at| at.saturating_duration_since(Instant::now()))
                            .unwrap_or_default();
                        (wait, inner.backoff_s)
                    };
                    if self.snapshot().not_ready.is_none()
                        || wait.is_zero()
                        || backoff >= RETRY_MAX_S
                    {
                        return;
                    }
                    tokio::time::sleep(wait).await;
                }
            }
        }
    }
}

impl crate::background::Duties for Service {
    fn sweep(&self) -> crate::background::Duty<'_> {
        Box::pin(async {
            if self.config.memory.compaction_enabled {
                self.ensure_init_inner().await?;
                // The current reader has no canonical resident stores.
                // W2-E connects compaction_victims and its slot-rewrite API.
            }
            let inner = self.inner.lock().await;
            let Some(storage) = &inner.storage else {
                return Ok(());
            };
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(|e| e.to_string())?
                .as_secs_f64();
            let runs =
                crate::maintenance::prune_runs(storage.client(), self.config.dream.runs_keep, now)
                    .await?;
            let retrieval = crate::maintenance::prune_retrieval(
                storage.client(),
                now - self.config.memory.retrieval_retention_days * 86400.0,
            )
            .await?;
            drop(inner);
            if runs > 0 || retrieval > 0 {
                eprintln!("sweep maintenance: runs_pruned={runs} retrieval_pruned={retrieval}");
            }
            // The W3-H automatic-dream adapter runs after all maintenance.
            Ok(())
        })
    }
    fn autosave(&self) -> crate::background::Duty<'_> {
        Box::pin(async {
            // The current read-only bank has no unsaved mutable state.
            // W2-E supplies the changed-state persistence operation when
            // its writer lands; scheduling and exit ownership live here.
            Ok(())
        })
    }

    fn warmup(&self) -> crate::background::Duty<'_> {
        Box::pin(async {
            Service::warmup(self).await;
            Ok(())
        })
    }

    fn reap(&self, idle_seconds: f64) -> crate::background::Duty<'_> {
        Box::pin(async move {
            let ready = self.ensure_init_inner().await?;
            // All session mutation is serialized with service operations.
            let _service_lock = self.inner.lock().await;
            let mut sessions = ready.sessions.lock().await;
            let entries: Vec<_> = ready
                .bank
                .entries
                .iter()
                .filter_map(|e| {
                    e.episode_id
                        .as_ref()
                        .map(|id| (id.clone(), e.ts, e.source.clone()))
                })
                .collect();
            let seconds = |key: &str, default: f64| {
                std::env::var(key)
                    .ok()
                    .and_then(|v| v.trim().parse::<f64>().ok())
                    .unwrap_or(default)
            };
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(|e| e.to_string())?
                .as_secs_f64();
            let result = sessions.reap_with_clock(
                idle_seconds,
                now,
                seconds("PSEUDOLIFE_SESSION_RESUME_SECONDS", 21600.0),
                seconds("PSEUDOLIFE_HANDLE_RESUME_SECONDS", 2592000.0),
                &entries,
                || {
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|t| t.as_secs_f64())
                        .unwrap_or(now)
                },
            );
            sessions.persist_reap(ready.storage.client(), &result).await;
            drop(sessions);
            drop(_service_lock);
            if result.fire_dream {
                self.session_dream.fire();
            }
            if !result.session_keys.is_empty() || result.swept > 0 {
                eprintln!(
                    "session reaper: closed={} swept={}",
                    result.session_keys.len(),
                    result.swept
                );
            }
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_ladder_matches_python() {
        let mut inner = Inner::default();
        let mut seen = Vec::new();
        for _ in 0..7 {
            Service::arm_backoff(&mut inner);
            seen.push(inner.backoff_s);
        }
        assert_eq!(seen, vec![5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0]);
    }
}
