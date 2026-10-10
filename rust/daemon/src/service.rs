//! The lazy initialization lifecycle (spec section L, `service.py:1223-1467,
//! 6192-6224`): storage first, then the embedder, then hydration, with the
//! exact failure recording and retry backoff of `MemoryService._ensure_init`.

use crate::bank::Bank;
use crate::config::{Config, DaemonEnv};
use crate::embed::Embedder;
use crate::startup::StartupState;
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
    /// Complete durable startup inputs for the read/write slices.
    #[allow(dead_code)] // W2-D/E adopt these inputs when their stores land.
    pub startup: Arc<StartupState>,
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
            if crate::mutants::active("startup-clock-reloads")
                && r.startup.clock_state().hlc_reseed_pending
            {
                inner.ready = None;
            } else {
                if r.startup.clock_state().hlc_reseed_pending {
                    r.startup
                        .reseed(r.storage.client())
                        .await
                        .map_err(|error| format!("{error:#}"))?;
                }
                return Ok(r.clone());
            }
        }
        self.refuse_while_backing_off(&inner)?;
        let storage = self.ensure_storage(&mut inner).await?;
        let metadata = crate::startup::metadata(storage.client())
            .await
            .map_err(|e| format!("startup metadata failed: {e}"))?;
        if inner.embedder.is_none() {
            // Built once and kept across failed attempts; a failure here
            // records nothing, as in Python (it is outside the abandon path).
            let dir = self.model_dir.clone();
            let threads = self.ort_threads;
            let config = self.config.embedding.clone();
            let embedder = tokio::task::spawn_blocking(move || {
                Embedder::load_config(&config, dir.as_deref(), threads)
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
        let candidate = crate::startup::candidate(
            storage.client(),
            &self.config.memory,
            metadata,
            embedder.embedding_dim(),
        )
        .await;
        match candidate {
            Ok((bank, startup)) => {
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
                    startup: Arc::new(startup),
                });
                inner.ready = Some(ready.clone());
                // Python publishes every loaded store before the late reseed.
                // Failure here keeps them and retries only the clock next time.
                ready
                    .startup
                    .reseed(ready.storage.client())
                    .await
                    .map_err(|error| format!("{error:#}"))?;
                Ok(ready)
            }
            Err(e) => {
                if let Some(stale) = e.downcast_ref::<crate::bank::StaleDims>() {
                    let msg = self.stale_dims_message(stale, embedder.embedding_dim());
                    self.update(|s| s.init_refusal = Some(msg.clone()));
                    let mut inner2 = inner;
                    self.update(|s| s.not_ready = None);
                    Self::arm_backoff(&mut inner2);
                    return Err(msg);
                }
                // `_abandon_partial_init`: retryable unless it is the refusal
                // already recorded.
                // `hydrate_cms` failures are re-raised as this RuntimeError (service.py:1543).
                let reason = format!("{e:#}");
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
    fn stale_dims_message(&self, s: &crate::bank::StaleDims, dim: usize) -> String {
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
            dim,
        )
    }

    /// The session reaper's init retry (`mcp_server._session_reaper_loop`
    /// calls `reap_idle_sessions`, which runs `_ensure_init` first): a
    /// daemon whose warmup gave up still recovers with no client traffic.
    /// Reaping sessions themselves belongs to the episode slice.
    pub async fn reaper(self: Arc<Self>, every_s: f64) {
        if !(every_s.is_finite() && every_s >= 0.0) {
            return; // time.sleep raises in Python's thread: the reaper dies
        }
        loop {
            tokio::time::sleep(Duration::from_secs_f64(every_s.max(0.001))).await;
            if let Err(e) = self.ensure_init().await {
                eprintln!("session reaper error: {e}");
            }
        }
    }

    /// `MemoryService.warmup`: retry only a retryable failure whose window
    /// is still below the cap; anything else waits for the next caller.
    pub async fn warmup(self: Arc<Self>) {
        loop {
            match self.ensure_init().await {
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

    // A real Service with the existing tiny CPU ONNX fixture exercises the
    // clock-only fast path without loading or encoding a real model.
    #[tokio::test]
    async fn db_clock_reseed_retry() {
        let Ok(dsn) = std::env::var("PL_PGS_CLOCK_RETRY_DSN") else {
            return;
        };
        let parsed = crate::storage::parse_dsn(&dsn).unwrap();
        assert!(
            parsed
                .get_dbname()
                .is_some_and(|name| name.starts_with("pl_cf_pgs_"))
        );
        let config_path = PathBuf::from(std::env::var("PL_PGS_CLOCK_CONFIG").unwrap());
        let config = crate::config::load(&config_path).unwrap();
        let env = DaemonEnv::from_env(&|_| None).unwrap();
        let service = Arc::new(Service::new(
            config,
            env,
            config_path.parent().unwrap().to_path_buf(),
            dsn,
            false,
        ));
        fn dump_db(phase: &str) {
            let result = std::process::Command::new(std::env::var("PL_PGS_CLOCK_PYTHON").unwrap())
                .args([
                    std::env::var("PL_PGS_CLOCK_DB_DUMP_SCRIPT").unwrap(),
                    "--dump-db".into(),
                    phase.into(),
                ])
                .output()
                .unwrap();
            assert!(
                result.status.success(),
                "DB snapshot helper failed: {}",
                String::from_utf8_lossy(&result.stderr)
            );
        }
        fn retained(ready: &Ready) -> Value {
            json!({"startup": ready.startup.dump(), "entries": ready.bank.entries_dump()})
        }
        let first = service.ensure_init().await;
        let first_ready = service.inner.lock().await.ready.clone();
        let mut attempts = vec![json!({
            "ok": first.is_ok(), "error": first.err(),
            "retained": first_ready.as_deref().map(retained), "same_as_first": null,
        })];
        let storage = service
            .snapshot()
            .storage
            .expect("storage opened before clock reseed");
        let actual =
            crate::storage::schema::simple_value(storage.client(), "SELECT current_database()")
                .await
                .unwrap()
                .unwrap()
                .unwrap();
        assert!(actual.starts_with("pl_cf_pgs_"));
        dump_db("initial");
        storage
            .client()
            .execute(
                "UPDATE meta SET value = $1 WHERE key = 'coordination_hlc_highwater'",
                &[&json!([20000, 2])],
            )
            .await
            .unwrap();
        dump_db("clock-corrected");
        // Losing this required column makes a second loader pass observable.
        storage
            .client()
            .batch_execute("ALTER TABLE entries DROP COLUMN text")
            .await
            .unwrap();
        dump_db("column-dropped");
        for (index, phase) in [(1, "retry"), (2, "healthy")] {
            if index == 2 {
                storage
                    .client()
                    .execute(
                        "UPDATE meta SET value = $1 WHERE key = 'coordination_hlc_highwater'",
                        &[&json!("malformed")],
                    )
                    .await
                    .unwrap();
                dump_db("healthy-clock-broken");
            }
            let result = service.ensure_init().await;
            attempts.push(match result {
                Ok(ready) => {
                    let same = first_ready.as_ref().map(|original| {
                        json!({
                            "bank": Arc::ptr_eq(&original.bank, &ready.bank),
                            "startup": Arc::ptr_eq(&original.startup, &ready.startup),
                            "storage": Arc::ptr_eq(&original.storage, &ready.storage),
                            "embedder": Arc::ptr_eq(&original.embedder, &ready.embedder),
                        })
                    });
                    json!({"ok": true, "startup": ready.startup.dump(),
                           "entries": ready.bank.entries_dump(), "same_as_first": same})
                }
                Err(error) => json!({"ok": false, "error": error,
                    "retained": service.inner.lock().await.ready.as_deref().map(retained),
                    "same_as_first": null}),
            });
            dump_db(phase);
        }
        std::fs::write(
            std::env::var("PL_PGS_CLOCK_OUT").unwrap(),
            serde_json::to_vec(&json!({"attempts": attempts})).unwrap(),
        )
        .unwrap();
        drop(first_ready);
        {
            let mut inner = service.inner.lock().await;
            inner.ready = None;
            inner.storage = None;
        }
        service.update(|snapshot| snapshot.storage = None);
        drop(service);
        Arc::try_unwrap(storage)
            .ok()
            .expect("fixture owns the writer session")
            .close()
            .await;
    }
}
