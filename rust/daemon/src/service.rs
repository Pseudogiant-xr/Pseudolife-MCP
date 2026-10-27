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
///
/// A reconnect that keeps the bank (no other writer in the gap) republishes
/// the same resident state over the new session ([`Ready::over`]), so every
/// field but `storage` is shared between the two.
pub struct Ready {
    /// The writer session, for the write-path slices.
    pub storage: Arc<Storage>,
    pub embedder: Arc<Embedder>,
    pub bank: Arc<std::sync::RwLock<Bank>>,
    /// Lock order: Service.inner, then write, then the storage writer.
    /// Stores and background reaping share this episode state and lock.
    pub write: Arc<tokio::sync::Mutex<crate::episodes::WriteState>>,
    pub writes: crate::store::WriteTasks,
    /// Complete durable startup inputs for the read/write slices.
    #[allow(dead_code)] // W2-D/E adopt these inputs when their stores land.
    pub startup: Arc<StartupState>,
    /// The resident cortex; `fact_set.rs` writes it under `write`.
    pub cortex: Arc<crate::stores::cortex::SharedCortex>,
    /// The resident world cortex (`world_tools.rs` writes it under `write`).
    pub world: Arc<crate::stores::world::SharedWorld>,
    /// The resident lesson store (`memory_forget` retires lessons under
    /// `write`; the dream's synthesis is not ported).
    pub lessons: Arc<crate::stores::lessons::SharedLessons>,
    /// The service hybrid logical clock (`MemoryService._hlc`).
    pub hlc: Arc<std::sync::Mutex<crate::hlc::Clock>>,
    /// The dream's acknowledgement state (`dream::service`). Lock order:
    /// `write`, then this.
    pub dream: Arc<tokio::sync::Mutex<crate::dream::service::Tracking>>,
    /// The dream pass's process-local state (`dream::run::RunState`): the
    /// single-flight guard, the per-batch failure counter, the alias-name
    /// memo, the stall tracker and the last extractor record. Never held
    /// across an await that takes `write`.
    pub dream_run: Arc<crate::dream::run::RunState>,
}

impl Ready {
    /// The same resident state over a replacement writer session: what
    /// `PostgresStorage.conn` gives its owner after a reconnect that found
    /// no other writer (the resident copy is still the bank's).
    fn over(&self, storage: Arc<Storage>) -> Ready {
        Ready {
            storage,
            embedder: self.embedder.clone(),
            bank: self.bank.clone(),
            write: self.write.clone(),
            writes: self.writes.clone(),
            startup: self.startup.clone(),
            cortex: self.cortex.clone(),
            world: self.world.clone(),
            lessons: self.lessons.clone(),
            hlc: self.hlc.clone(),
            dream: self.dream.clone(),
            dream_run: self.dream_run.clone(),
        }
    }

    /// `self._hlc.tick()`, with the clock re-seeded first from the stored
    /// high-water mark as `_reseed_hlc` seeds it (`service.py:1642-1669`).
    /// Observing the same mark again is a no-op, and the mark changes only
    /// when a reseed runs, so this equals observing once per reseed.
    pub fn tick_hlc(&self) -> (i64, i64) {
        let high = self.startup.clock_state().hlc_highwater;
        let mut clock = self.hlc.lock().expect("hlc lock");
        if high > (0, 0) {
            clock.observe(high.0, high.1);
        }
        clock.tick()
    }
}

/// Lock-free view for `/health` (it never waits on init, spec L7).
#[derive(Clone, Default)]
pub struct Snapshot {
    pub init_refusal: Option<String>,
    pub not_ready: Option<String>,
    pub storage: Option<Arc<Storage>>,
    pub embedder: Option<Value>,
    /// The published stores' startup state, once an init has completed its
    /// clock reseed: `/health` `ready` reads its live reseed flag
    /// (`MemoryService.coordination_tier_ready`).
    pub startup: Option<Arc<crate::startup::StartupState>>,
    /// The ready service, published with `startup`: `/health` reads the
    /// resident bank and the dream's tracking state from it, as Python
    /// reads `svc._cms` and `svc._dream_tracking_error`, without the lock.
    pub ready: Option<Arc<Ready>>,
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
    pub writes: crate::store::WriteTasks,
    /// `MemoryService._persist_errors`: durable saves that failed (served
    /// by `/health`).
    pub persist_errors: std::sync::atomic::AtomicU64,
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
    /// This service's own handle and the board's, installed once at
    /// startup (`install`): the background dream work needs an owned
    /// service (the dream pass spawns), and the stall notice the board.
    handle: std::sync::OnceLock<std::sync::Weak<Service>>,
    board: std::sync::OnceLock<std::sync::Weak<crate::board::Board>>,
    /// `MemoryService._hlc`: one clock for the process, which a rehydrate
    /// reseeds but never replaces, so it never runs backwards.
    hlc: Arc<std::sync::Mutex<crate::hlc::Clock>>,
    /// The dream's process state (`_dream_run_guard`, the stall tracker, the
    /// lesson cursors, the alias memo, the last extractor): service
    /// attributes in Python, kept across a rehydrate, so a pass still
    /// running over a superseded ready service keeps its single flight.
    dream_run: Arc<crate::dream::run::RunState>,
    /// `/health`'s last `dream_tracking_error` code and the tracking state
    /// it was read from (its address), for a read that finds it busy.
    pub(crate) health_tracking_code: std::sync::Mutex<Option<(usize, Option<String>)>>,
    /// The counters the row carries that this daemon does not maintain.
    counters_carry: std::sync::Mutex<crate::counters::Carry>,
    /// `_last_saved_fingerprint`; `None` until a save (or after a re-read).
    last_saved: std::sync::Mutex<Option<crate::counters::Fingerprint>>,
    /// `_counters_save_pending`: the last counters write failed.
    counters_pending: std::sync::atomic::AtomicBool,
    /// A `weights.pt` the bank has no counters row for (`/health`).
    pub(crate) counters_import: std::sync::Mutex<Option<String>>,
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
            writes: crate::store::WriteTasks::default(),
            persist_errors: std::sync::atomic::AtomicU64::new(0),
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
            handle: std::sync::OnceLock::new(),
            board: std::sync::OnceLock::new(),
            hlc: Arc::default(),
            dream_run: Arc::default(),
            health_tracking_code: std::sync::Mutex::new(None),
            counters_carry: Default::default(),
            last_saved: std::sync::Mutex::new(None),
            counters_pending: std::sync::atomic::AtomicBool::new(false),
            counters_import: std::sync::Mutex::new(None),
        }
    }

    /// Install the handles the background dream work needs (once, at
    /// startup, after the board exists). Weak, so neither keeps the other.
    pub fn install(self: &Arc<Self>, board: &Arc<crate::board::Board>) {
        let _ = self.handle.set(Arc::downgrade(self));
        let _ = self.board.set(Arc::downgrade(board));
    }

    /// The dream's process state (`/health` reads the stall tracker here,
    /// without the service lock).
    pub fn dream_run(&self) -> &Arc<crate::dream::run::RunState> {
        &self.dream_run
    }

    /// The owned service, once installed.
    pub fn handle(&self) -> Option<Arc<Service>> {
        self.handle.get().and_then(std::sync::Weak::upgrade)
    }

    /// The board, once installed.
    pub fn board(&self) -> Option<Arc<crate::board::Board>> {
        self.board.get().and_then(std::sync::Weak::upgrade)
    }

    /// `_fire_and_forget_dream` for a closed session (the reaper's idle
    /// close here; the SessionEnd hook's close calls the same): one
    /// automatic dream in the background.
    pub fn fire_session_dream(&self) {
        match self.handle() {
            Some(me) => {
                eprintln!("session-end dream trigger: fired");
                crate::dream::ticks::fire_and_forget(&me);
            }
            None => self.session_dream.fire(),
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

    /// `_ensure_tier(full=False)` for the board, without embedding or hydration.
    pub async fn storage_tier(&self) -> Result<Arc<Storage>, String> {
        // Embedding and hydration keep inner locked after storage is usable.
        // Heartbeats and mailbox calls must not wait for that unrelated tier.
        if let Some(storage) = self.snapshot().storage {
            return Ok(storage);
        }
        let mut inner = self.inner.lock().await;
        self.ensure_storage(&mut inner).await
    }

    /// `_ensure_init`, run in its own task: a request whose client goes
    /// away mid-init drops only its wait, never the init itself (which
    /// writes to the shared writer session).
    /// The initialized service state, without initializing it (Python's
    /// `self._cms is None` check under the service lock).
    pub async fn ready_if_initialized(&self) -> Option<Arc<Ready>> {
        self.inner.lock().await.ready.clone()
    }

    pub async fn ensure_init(self: &Arc<Self>) -> Result<Arc<Ready>, String> {
        let me = self.clone();
        tokio::spawn(async move { me.ensure_init_inner().await })
            .await
            .map_err(|e| format!("initialization task failed: {e}"))?
    }

    /// The board's clock retry (`coordination.dispatch`, its
    /// `CoordinationClockChanged` arm): an epoch mismatch proves the cached
    /// session check stale, so reprobe the writer session even inside its
    /// probe interval (`_session_ok_at = 0.0`) and run `_ensure_init`, both
    /// under the service lock, before the caller's one fresh attempt.
    #[allow(dead_code)] // B5's W2F-CLOCK-RETRY send retry is its caller.
    pub async fn refresh_writer_session(self: &Arc<Self>) -> Result<Arc<Ready>, String> {
        let me = self.clone();
        tokio::spawn(async move { me.init(true).await })
            .await
            .map_err(|e| format!("initialization task failed: {e}"))?
    }

    /// `_ensure_init`, cold and fast paths.
    pub(crate) async fn ensure_init_inner(&self) -> Result<Arc<Ready>, String> {
        self.init(false).await
    }

    async fn init(&self, reprobe: bool) -> Result<Arc<Ready>, String> {
        let mut inner = self.inner.lock().await;
        if reprobe && let Some(storage) = &inner.storage {
            storage.forget_probe();
        }
        self.verify_writer_session(&mut inner).await?;
        self.rehydrate_if_bank_changed_hands(&mut inner);
        if let Some(r) = &inner.ready {
            if crate::mutants::active("startup-clock-reloads")
                && r.startup.clock_state().hlc_reseed_pending
            {
                inner.ready = None;
                self.update(|s| s.startup = None);
            } else {
                if r.startup.clock_state().hlc_reseed_pending {
                    r.startup
                        .reseed(r.storage.client())
                        .await
                        .map_err(|error| format!("{error:#}"))?;
                    let startup = r.startup.clone();
                    let published = r.clone();
                    self.update(|s| {
                        s.startup = Some(startup);
                        s.ready = Some(published);
                    });
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
        let candidate = async {
            let (bank, startup) = crate::startup::candidate(
                storage.client(),
                &self.config.memory,
                metadata,
                embedder.embedding_dim(),
            )
            .await?;
            let write = crate::episodes::from_startup(&startup)?;
            let mut cortex =
                crate::stores::cortex::hydrate(storage.client(), embedder.embedding_dim()).await?;
            cortex.configure(&self.config.memory.cortex, &startup.metadata);
            let world = crate::stores::world::hydrate(storage.client()).await?;
            let lessons = crate::stores::lessons::hydrate(storage.client()).await?;
            // `_load_counters`: a failed read fails the build, since serving
            // on and autosaving fresh counters would overwrite the real ones.
            let counters = crate::counters::get(storage.client())
                .await
                .map_err(|e| anyhow::anyhow!("counters row read failed: {e}"))?;
            let mut write = write;
            self.load_counters(&mut write, counters.as_ref());
            Ok::<_, anyhow::Error>((bank, startup, write, cortex, world, lessons))
        }
        .await;
        match candidate {
            Ok((bank, startup, write, cortex, world, lessons)) => {
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
                    bank: Arc::new(std::sync::RwLock::new(bank)),
                    write: Arc::new(tokio::sync::Mutex::new(write)),
                    writes: self.writes.clone(),
                    startup: Arc::new(startup),
                    cortex: Arc::new(crate::stores::cortex::SharedCortex::new(cortex)),
                    world: Arc::new(crate::stores::world::SharedWorld::new(world)),
                    lessons: Arc::new(crate::stores::lessons::SharedLessons::new(lessons)),
                    hlc: self.hlc.clone(),
                    dream: Arc::new(tokio::sync::Mutex::new(Default::default())),
                    dream_run: self.dream_run.clone(),
                });
                inner.ready = Some(ready.clone());
                // Python publishes every loaded store before the late reseed.
                // Failure here keeps them and retries only the clock next time.
                ready
                    .startup
                    .reseed(ready.storage.client())
                    .await
                    .map_err(|error| format!("{error:#}"))?;
                // Published only now, as Python's flag stays pending until
                // the reseed: `ready` never leads the clock.
                let startup = ready.startup.clone();
                let published = ready.clone();
                self.update(|s| {
                    s.startup = Some(startup);
                    s.ready = Some(published);
                });
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

    /// `_ensure_init`'s first step, `storage.verify_writer_session()`: a
    /// replaced session is published everywhere the old one was (the
    /// service, `/health`'s snapshot and, with the same resident state, the
    /// ready service). Only a lease another writer holds, or an error
    /// Python does not catch there, fails the call.
    async fn verify_writer_session(&self, inner: &mut Inner) -> Result<(), String> {
        let Some(storage) = inner.storage.clone() else {
            return Ok(());
        };
        let Some(fresh) = storage.verify_writer_session().await? else {
            return Ok(());
        };
        let fresh = Arc::new(fresh);
        if fresh.resident_invalidated().is_some()
            && !crate::mutants::active("writer-board-clock-kept")
            && let Some(board) = self.board()
        {
            // `_coordination_hlc_epoch = None` (service.py:1420): the board's
            // own clock never saw the other writer's high-water mark, and the
            // replacement's epoch would let a send past the epoch fence. Mark
            // it unseeded before any caller can see that epoch, so the next
            // send reseeds from the bank.
            board.hlc.lock().expect("hlc lock").seeded = false;
        }
        inner.storage = Some(fresh.clone());
        let republished = inner
            .ready
            .take()
            .map(|ready| Arc::new(ready.over(fresh.clone())));
        inner.ready = republished.clone();
        self.update(|s| {
            s.storage = Some(fresh);
            // Only a ready service `/health` already sees is republished.
            if s.ready.is_some() {
                s.ready = republished;
            }
        });
        Ok(())
    }

    /// `_rehydrate_if_bank_changed_hands` (service.py:1381-1438): another
    /// writer held the bank while this process was disconnected, so its
    /// resident stores may predate that writer's changes. Drop them (the
    /// ready service holds the bank, the canonical stores, the episode write
    /// state with the active-session pointer, tombstones, deferred roots and
    /// entity-kind cache, and the dream's acknowledgement tracking) so the
    /// rest of `_ensure_init` hydrates afresh, reset the dream's batch
    /// failures, then lift the storage's refusal. The process clock and the
    /// rest of the dream's process state stay, as in Python. A reconnect
    /// with no other writer flags nothing.
    fn rehydrate_if_bank_changed_hands(&self, inner: &mut Inner) {
        let Some(storage) = inner.storage.clone() else {
            return;
        };
        let Some(reason) = storage.resident_invalidated() else {
            return;
        };
        eprintln!("{reason}: dropping the resident stores and re-reading the bank before serving");
        inner.ready = None;
        self.dream_run.forget_batch_failures();
        self.update(|s| {
            s.startup = None;
            s.ready = None;
        });
        storage.acknowledge_rehydration();
    }

    /// `_load_counters` (service.py:1487-1545) for a bank this daemon reads:
    /// the row's counters merged over the fresh ones. A pre-v57 `weights.pt`
    /// beside a bank with no row is reported (logged once, and `/health`
    /// names the import step) and never read: the Python daemon imports it.
    fn load_counters(
        &self,
        ws: &mut crate::episodes::WriteState,
        row: Option<&crate::counters::Row>,
    ) {
        *self.last_saved.lock().expect("last saved lock") = None;
        let mut carry = crate::counters::Carry::default();
        if let Some(row) = row {
            let applied = crate::counters::apply(row, std::mem::take(&mut ws.surprise_history));
            ws.interaction_count = applied.interaction_count;
            ws.surprise_history = applied.surprise_history;
            crate::read::stats::restore_tier(applied.tier_queries, applied.tier_hits);
            carry = applied.carry;
        }
        *self.counters_carry.lock().expect("counters carry lock") = carry;
        let pending =
            crate::counters::pending_import(&self.data_dir.join("memory_state"), row.is_some());
        let mut reported = self.counters_import.lock().expect("counters import lock");
        if pending.is_some() && *reported != pending {
            eprintln!("{}", pending.as_deref().unwrap_or_default());
        }
        *reported = pending;
    }

    /// `_persist_all`'s counters arm for autosave (`always` false: only when
    /// the bank or cortex changed since the last save, or the last counters
    /// write failed) and the clean-exit flush (`always`). Entries, access
    /// counts and the canonical stores write through here, so the counters
    /// row is the one thing these saves add. A failed write is logged and
    /// retried by the next autosave, as in Python.
    async fn save_counters(&self, always: bool) -> Result<(), String> {
        let inner = self.inner.lock().await;
        let Some(ready) = inner.ready.clone() else {
            return Ok(());
        };
        if crate::mutants::active("counters-save-off") {
            return Ok(());
        }
        let fp = crate::counters::fingerprint(&ready);
        let pending = self
            .counters_pending
            .load(std::sync::atomic::Ordering::SeqCst);
        if !always && !pending && *self.last_saved.lock().expect("last saved lock") == Some(fp) {
            return Ok(());
        }
        let row = {
            let ws = ready.write.lock().await;
            let bands: Vec<String> = self
                .config
                .memory
                .band_specs
                .iter()
                .map(|b| b.name.clone())
                .collect();
            crate::counters::state(
                &self.config.memory.preset,
                &bands,
                ws.interaction_count,
                &ws.surprise_history,
                crate::read::stats::tier_state(),
                &self.counters_carry.lock().expect("counters carry lock"),
            )
        };
        match crate::counters::put(ready.storage.client(), &row).await {
            Ok(()) => self
                .counters_pending
                .store(false, std::sync::atomic::Ordering::SeqCst),
            Err(e) => {
                self.counters_pending
                    .store(true, std::sync::atomic::Ordering::SeqCst);
                eprintln!("counters save failed: {}", crate::storage::error_text(&e));
            }
        }
        *self.last_saved.lock().expect("last saved lock") = Some(fp);
        drop(inner);
        Ok(())
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

    /// `MemoryService.warmup`: retry only a retryable failure whose window
    /// is still below the cap; anything else waits for the next caller.
    pub async fn warmup(&self, mut stop: tokio::sync::watch::Receiver<bool>) {
        loop {
            if *stop.borrow() {
                return;
            }
            match self.ensure_init_inner().await {
                Ok(ready) => {
                    crate::read::search_route::warmup_probe(self, &ready).await;
                    return;
                }
                Err(e) => {
                    if *stop.borrow() {
                        return;
                    }
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
                    tokio::select! {
                        _ = stop.changed() => return,
                        _ = tokio::time::sleep(wait) => {}
                    }
                }
            }
        }
    }
}

impl crate::background::Duties for Service {
    fn sweep(&self, stop: tokio::sync::watch::Receiver<bool>) -> crate::background::Duty<'_> {
        Box::pin(async move {
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
            // `run_sweep_once` (`memory/dream.py:2398-2547`): the steps after
            // the reapers run only while dreaming is enabled: the analyzer
            // and deep ticks, the merge judge, `dream_status`, the stall tick
            // and the automatic dream (`dream/ticks.rs`). The review-queue
            // judges are not run yet (divergences.md).
            if !self.config.dream.enabled {
                return Ok(());
            }
            let ready = self.ensure_init_inner().await?;
            crate::dream::ticks::sweep(self, &ready, &stop).await
        })
    }
    /// `autosave_if_changed` (service.py:3687-3700).
    fn autosave(&self) -> crate::background::Duty<'_> {
        Box::pin(self.save_counters(false))
    }

    /// `flush` (service.py:3702-3711): unconditional, at a clean exit.
    fn flush(&self) -> crate::background::Duty<'_> {
        Box::pin(self.save_counters(true))
    }

    fn warmup(&self, stop: tokio::sync::watch::Receiver<bool>) -> crate::background::Duty<'_> {
        Box::pin(async {
            Service::warmup(self, stop).await;
            Ok(())
        })
    }

    fn reap(&self, idle_seconds: f64) -> crate::background::Duty<'_> {
        Box::pin(async move {
            self.ensure_init_inner().await?;
            // All session mutation is serialized with service operations.
            let _service_lock = self.inner.lock().await;
            // The ready service current under the lock: a rehydrate between
            // the two locks replaced the one init returned.
            let Some(ready) = _service_lock.ready.clone() else {
                return Ok(());
            };
            let mut write = ready.write.lock().await;
            let sessions = &mut write.log;
            let entries: Vec<_> = ready
                .bank
                .read()
                .unwrap()
                .entries
                .iter()
                .filter_map(|e| {
                    e.episode_id
                        .as_ref()
                        .map(|id| (id.clone(), e.ts, e.source.clone()))
                })
                .collect();
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(|e| e.to_string())?
                .as_secs_f64();
            let result = sessions.reap_with_settings(
                idle_seconds,
                now,
                &entries,
                || {
                    let resume = crate::background::seconds(
                        std::env::var("PSEUDOLIFE_SESSION_RESUME_SECONDS").ok().as_deref(),
                        21600.0,
                    )?;
                    let handles = crate::background::seconds(
                        std::env::var("PSEUDOLIFE_HANDLE_RESUME_SECONDS").ok().as_deref(),
                        2592000.0,
                    ).unwrap_or_else(|_| {
                        eprintln!("PSEUDOLIFE_HANDLE_RESUME_SECONDS is not a number; using the 30-day default");
                        2592000.0
                    });
                    Ok((resume, handles))
                },
                || {
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|t| t.as_secs_f64())
                        .unwrap_or(now)
                },
            );
            sessions.persist_reap(ready.storage.client(), &result).await;
            drop(write);
            drop(_service_lock);
            if let Some(error) = result.failure {
                return Err(error);
            }
            if result.fire_dream {
                self.fire_session_dream();
            }
            if !result.session_keys.is_empty() || result.swept > 0 {
                eprintln!("session reaper: idle sessions closed or swept");
            }
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn shutdown_interrupts_warmup_retry_wait_without_another_init() {
        let service = Arc::new(Service::new(
            Config::default(),
            DaemonEnv::from_env(&|_| None).unwrap(),
            PathBuf::new(),
            "postgresql://nobody@127.0.0.1:9/pl_cf_w3i_unused".into(),
            false,
        ));
        {
            let mut inner = service.inner.lock().await;
            inner.retry_at = Some(Instant::now() + Duration::from_secs(3600));
            inner.failures = 1;
            inner.backoff_s = 5.0;
        }
        service.update(|s| s.not_ready = Some("controlled retry refusal".into()));
        let background = crate::background::Background::default();
        background.start_background_durability(service.clone(), 3600.0);
        tokio::task::yield_now().await;
        tokio::time::timeout(
            Duration::from_millis(500),
            background.shutdown(service.as_ref()),
        )
        .await
        .expect("stop must interrupt the retry wait");
        let inner = service.inner.lock().await;
        assert_eq!(inner.failures, 1);
        assert!(inner.storage.is_none());
    }

    #[tokio::test]
    #[ignore = "requires a disposable board database through board.boundary"]
    async fn board_heartbeat_during_paused_initialization() {
        let dsn =
            std::env::var("PL_W2F_BOARD_DSN").expect("board.boundary supplies the disposable DSN");
        let parsed = crate::storage::parse_dsn(&dsn).unwrap();
        assert!(
            parsed
                .get_dbname()
                .is_some_and(|n| n.starts_with("pl_cf_w2f_"))
        );
        assert_eq!(parsed.get_ports(), &[5434]);
        assert!(
            matches!(parsed.get_hosts(), [tokio_postgres::config::Host::Tcp(h)] if h == "127.0.0.1")
        );
        let service = Arc::new(Service::new(
            Config::default(),
            DaemonEnv::from_env(&|_| None).unwrap(),
            PathBuf::new(),
            dsn.clone(),
            true,
        ));
        let board = crate::board::Board::new(
            service.config.coordination.clone(),
            dsn,
            service.clone(),
            Arc::new(crate::auth::PrincipalStore::new()),
        );
        let params = |v: Value| v.as_object().unwrap().clone();
        let mut headers = std::collections::HashMap::new();
        let registered = board
            .dispatch(
                "register",
                params(json!({"label": "codex"})),
                &headers,
                "default",
            )
            .await
            .unwrap();
        headers.insert(
            "x-pl-agent".into(),
            registered["agent_id"].as_str().unwrap().into(),
        );
        headers.insert(
            "x-pl-agent-key".into(),
            registered["credential"].as_str().unwrap().into(),
        );
        let attached = board
            .dispatch(
                "attach",
                params(json!({"attachment_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})),
                &headers,
                "default",
            )
            .await
            .unwrap();
        // Initialization owns this same mutex throughout embedding and hydration,
        // after ensure_storage has published the usable storage snapshot.
        let initializing = service.inner.lock().await;
        let heartbeat = tokio::time::timeout(
            Duration::from_secs(2),
            board.dispatch(
                "heartbeat",
                params(json!({"attachment_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "generation": attached["generation"], "active": true})),
                &headers,
                "default",
            ),
        )
        .await;
        drop(initializing);
        let result = heartbeat
            .expect("heartbeat waited behind full service initialization")
            .unwrap();
        assert_eq!(result["agent_id"], registered["agent_id"]);
        assert!(
            result["lease_until"].as_f64().unwrap() >= attached["lease_until"].as_f64().unwrap()
        );
        drop(board);
        let storage = service.inner.lock().await.storage.take().unwrap();
        service.update(|s| s.storage = None);
        Arc::try_unwrap(storage)
            .ok()
            .expect("storage released")
            .close()
            .await;
    }

    #[tokio::test]
    #[ignore = "requires a disposable board database through board.boundary"]
    async fn board_send_refuses_an_incomplete_clock_seed() {
        let dsn = std::env::var("PL_W2F_BOARD_DSN").expect("owned board fixture DSN");
        let parsed = crate::storage::parse_dsn(&dsn).unwrap();
        assert!(
            parsed
                .get_dbname()
                .is_some_and(|n| n.starts_with("pl_cf_w2f_"))
        );
        assert_eq!(parsed.get_ports(), &[5434]);
        assert!(
            matches!(parsed.get_hosts(), [tokio_postgres::config::Host::Tcp(h)] if h == "127.0.0.1")
        );
        let service = Arc::new(Service::new(
            Config::default(),
            DaemonEnv::from_env(&|_| None).unwrap(),
            PathBuf::new(),
            dsn.clone(),
            true,
        ));
        let board = crate::board::Board::new(
            service.config.coordination.clone(),
            dsn.clone(),
            service.clone(),
            Arc::new(crate::auth::PrincipalStore::new()),
        );
        let params = |v: Value| v.as_object().unwrap().clone();
        let mut headers = std::collections::HashMap::new();
        let sender = board
            .dispatch(
                "register",
                params(json!({"label": "sender"})),
                &headers,
                "default",
            )
            .await
            .unwrap();
        let recipient = board
            .dispatch(
                "register",
                params(json!({"label": "recipient"})),
                &headers,
                "default",
            )
            .await
            .unwrap();
        headers.insert(
            "x-pl-agent".into(),
            sender["agent_id"].as_str().unwrap().into(),
        );
        headers.insert(
            "x-pl-agent-key".into(),
            sender["credential"].as_str().unwrap().into(),
        );
        let storage = service.storage_tier().await.unwrap();
        let future = (crate::board::store::now() * 1000.0) as i64 + 3_600_000;
        storage.client().execute(
            "INSERT INTO facts(entity,attribute,entity_norm,attribute_norm,value,status,confidence,asserted_at,last_confirmed,hlc_phys,hlc_logical) VALUES ('clock','fixture','clock','fixture','future','current',1,0,0,$1,7)",
            &[&future],
        ).await.unwrap();
        // A real query timeout hides a persisted high-water mark from the
        // first seed attempt. No schema mutation or production bank is used.
        let (mut locker, connection) = tokio_postgres::connect(&dsn, tokio_postgres::NoTls)
            .await
            .unwrap();
        let driver = tokio::spawn(connection);
        let lock = locker.transaction().await.unwrap();
        lock.batch_execute("LOCK TABLE facts IN ACCESS EXCLUSIVE MODE")
            .await
            .unwrap();
        storage
            .client()
            .batch_execute("SET statement_timeout = '100ms'")
            .await
            .unwrap();
        let send = params(
            json!({"to": recipient["agent_id"], "text": "clock proof", "request_id": "clock-proof"}),
        );
        let refused = board
            .dispatch("send", send.clone(), &headers, "default")
            .await;
        lock.rollback().await.unwrap();
        storage
            .client()
            .batch_execute("RESET statement_timeout")
            .await
            .unwrap();
        let committed: i64 = storage
            .client()
            .query_one("SELECT count(*) FROM coordination_messages", &[])
            .await
            .unwrap()
            .get(0);
        assert_eq!(
            committed, 0,
            "incomplete clock seed committed mail below bank history"
        );
        assert!(refused.is_err());
        assert!(!board.hlc.lock().unwrap().seeded);
        board
            .dispatch("send", send, &headers, "default")
            .await
            .unwrap();
        assert!(board.hlc.lock().unwrap().seeded);
        let stamp: String = storage
            .client()
            .query_one("SELECT hlc FROM coordination_messages", &[])
            .await
            .unwrap()
            .get(0);
        let (phys, logical) = stamp.split_once(':').unwrap();
        assert!(
            (
                phys.parse::<i64>().unwrap(),
                logical.parse::<i64>().unwrap()
            ) > (future, 7)
        );
        drop(storage);
        drop(board);
        drop(locker);
        driver.abort();
        let storage = service.inner.lock().await.storage.take().unwrap();
        service.update(|s| s.storage = None);
        Arc::try_unwrap(storage)
            .ok()
            .expect("storage released")
            .close()
            .await;
    }

    #[tokio::test]
    #[ignore = "requires a disposable board database through board.boundary"]
    async fn board_json_capabilities_roundtrip() {
        let dsn = std::env::var("PL_W2F_BOARD_DSN").expect("owned board fixture DSN");
        let parsed = crate::storage::parse_dsn(&dsn).unwrap();
        assert!(
            parsed
                .get_dbname()
                .is_some_and(|n| n.starts_with("pl_cf_w2f_"))
        );
        assert_eq!(parsed.get_ports(), &[5434]);
        assert!(
            matches!(parsed.get_hosts(), [tokio_postgres::config::Host::Tcp(h)] if h == "127.0.0.1")
        );
        let service = Arc::new(Service::new(
            Config::default(),
            DaemonEnv::from_env(&|_| None).unwrap(),
            PathBuf::new(),
            dsn.clone(),
            true,
        ));
        let board = crate::board::Board::new(
            service.config.coordination.clone(),
            dsn,
            service.clone(),
            Arc::new(crate::auth::PrincipalStore::new()),
        );
        let params = |v: Value| v.as_object().unwrap().clone();
        let mut headers = std::collections::HashMap::new();
        let capabilities = json!({"$serde_json::private::Number": true});
        let registered = board
            .dispatch(
                "register",
                params(json!({"label":"json fixture", "capabilities": capabilities})),
                &headers,
                "default",
            )
            .await
            .unwrap();
        assert_eq!(
            serde_json::to_vec(&registered["capabilities"]).unwrap(),
            serde_json::to_vec(&capabilities).unwrap()
        );
        headers.insert(
            "x-pl-agent".into(),
            registered["agent_id"].as_str().unwrap().into(),
        );
        headers.insert(
            "x-pl-agent-key".into(),
            registered["credential"].as_str().unwrap().into(),
        );
        let changed = capabilities.clone();
        let updated = board
            .dispatch(
                "update",
                params(json!({"status": "updated"})),
                &headers,
                "default",
            )
            .await
            .unwrap();
        assert_eq!(
            serde_json::to_vec(&updated["capabilities"]).unwrap(),
            serde_json::to_vec(&changed).unwrap()
        );
        // The roster excludes its caller, so read from a separate observer.
        let observer = board
            .dispatch(
                "register",
                params(json!({"label": "observer"})),
                &std::collections::HashMap::new(),
                "default",
            )
            .await
            .unwrap();
        let observer_headers = std::collections::HashMap::from([
            (
                "x-pl-agent".into(),
                observer["agent_id"].as_str().unwrap().into(),
            ),
            (
                "x-pl-agent-key".into(),
                observer["credential"].as_str().unwrap().into(),
            ),
        ]);
        let roster = board
            .dispatch("agents", params(json!({})), &observer_headers, "default")
            .await
            .unwrap();
        let console = board
            .console("default", &std::collections::HashMap::new())
            .await
            .unwrap();
        for result in [roster, console] {
            let row = result["agents"]
                .as_array()
                .unwrap()
                .iter()
                .find(|row| row["agent_id"] == registered["agent_id"])
                .unwrap();
            assert_eq!(
                serde_json::to_vec(&row["capabilities"]).unwrap(),
                serde_json::to_vec(&changed).unwrap()
            );
        }
        drop(board);
        let storage = service.inner.lock().await.storage.take().unwrap();
        service.update(|s| s.storage = None);
        Arc::try_unwrap(storage)
            .ok()
            .expect("storage released")
            .close()
            .await;
    }

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
            json!({"startup": ready.startup.dump(), "entries": ready.bank.read().expect("bank lock").entries_dump()})
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
                           "entries": ready.bank.read().expect("bank lock").entries_dump(), "same_as_first": same})
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
        service.update(|snapshot| {
            snapshot.storage = None;
            snapshot.startup = None;
        });
        drop(service);
        Arc::try_unwrap(storage)
            .ok()
            .expect("fixture owns the writer session")
            .close()
            .await;
    }

    /// The Rust arm of `harness/writer_session_cases.py`: the writer
    /// session dies, is replaced, changes hands and is refused, through the
    /// real service's `_ensure_init` path. The harness supplies the bank,
    /// the second writer (`--other-writer`, `--hold-lease`) and the
    /// per-phase database dumps, identically for the Python arm.
    #[tokio::test]
    async fn db_writer_session_phases() {
        let Ok(dsn) = std::env::var("PL_PGS_WRITER_DSN") else {
            return;
        };
        let parsed = crate::storage::parse_dsn(&dsn).unwrap();
        assert!(
            parsed
                .get_dbname()
                .is_some_and(|name| name.starts_with("pl_cf_pgs_"))
        );
        let config_path = PathBuf::from(std::env::var("PL_PGS_WRITER_CONFIG").unwrap());
        let config = crate::config::load(&config_path).unwrap();
        let env = DaemonEnv::from_env(&|_| None).unwrap();
        let service = Arc::new(Service::new(
            config,
            env,
            config_path.parent().unwrap().to_path_buf(),
            dsn.clone(),
            false,
        ));
        // The board's own clock, which a handover must unseed.
        let board = Arc::new(crate::board::Board::new(
            service.config.coordination.clone(),
            dsn.clone(),
            service.clone(),
            Arc::new(crate::auth::PrincipalStore::new()),
        ));
        service.install(&board);
        let helper = |args: &[&str]| {
            let mut command =
                std::process::Command::new(std::env::var("PL_PGS_WRITER_PYTHON").unwrap());
            command.arg(std::env::var("PL_PGS_WRITER_SCRIPT").unwrap());
            command.args(args);
            command
        };
        let run_helper = |args: &[&str]| {
            let out = helper(args).output().unwrap();
            assert!(
                out.status.success(),
                "writer helper {args:?} failed: {}",
                String::from_utf8_lossy(&out.stderr)
            );
        };
        // `None` once the session is dead: a call served over it.
        async fn backend_pid(storage: &Storage) -> Option<i32> {
            storage
                .client()
                .query_one("SELECT pg_backend_pid()", &[])
                .await
                .ok()
                .map(|row| row.get(0))
        }
        // Ends the writer's backend and waits until it has gone, lease and
        // all, so a second writer never waits on a dying session.
        async fn kill(storage: &Storage, dsn: &str) {
            let pid = backend_pid(storage).await.expect("a live session to kill");
            let (side, task) = crate::storage::connect(
                &crate::storage::parse_dsn(dsn).unwrap(),
                Duration::from_secs(10),
            )
            .await
            .unwrap();
            side.execute("SELECT pg_terminate_backend($1)", &[&pid])
                .await
                .unwrap();
            let deadline = Instant::now() + Duration::from_secs(10);
            loop {
                let alive: i64 = side
                    .query_one(
                        "SELECT count(*) FROM pg_stat_activity WHERE pid = $1",
                        &[&pid],
                    )
                    .await
                    .unwrap()
                    .get(0);
                if alive == 0 && storage.client().is_closed() {
                    break;
                }
                assert!(Instant::now() < deadline, "the killed session never ended");
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
            drop(side);
            let _ = task.await;
        }
        // Past `_SESSION_PROBE_INTERVAL`, so Python's arm probes too.
        let past_probe = || tokio::time::sleep(Duration::from_millis(1100));
        // One phase's answer, compared with the resident stores the previous
        // successful phase served.
        let observe =
            |result: Result<Arc<Ready>, String>, pid_before: Option<i32>, before: Arc<Ready>| {
                let service = service.clone();
                async move {
                    let storage = service.snapshot().storage.expect("storage");
                    let ping = match storage.ping().await {
                        Ok(()) => "ok".to_string(),
                        Err(e) => format!("error: {e}"),
                    };
                    let (ok, error, same, replaced, served) = match result {
                        Ok(ready) => {
                            let pid = backend_pid(&ready.storage).await;
                            let same = json!({
                                "bank": Arc::ptr_eq(&before.bank, &ready.bank),
                                "world": Arc::ptr_eq(&before.world, &ready.world),
                                "cortex": Arc::ptr_eq(&before.cortex, &ready.cortex),
                            });
                            // The process clock and the dream's process state
                            // outlive every rehydrate, as Python's service
                            // attributes do.
                            assert!(Arc::ptr_eq(&before.hlc, &ready.hlc));
                            assert!(Arc::ptr_eq(&before.dream_run, &ready.dream_run));
                            let replaced = json!(pid.is_some() && pid != pid_before);
                            (true, Value::Null, same, replaced, ready)
                        }
                        Err(e) => (false, json!(e), Value::Null, Value::Null, before),
                    };
                    let observed = json!({
                        "ok": ok, "error": error, "same_as_before": same,
                        "session_replaced": replaced,
                        "epoch": storage.lease_epoch(), "health_db": ping,
                        "resident_invalidated": storage.resident_invalidated(),
                    });
                    (observed, served)
                }
            };
        let seeded = || board.hlc.lock().unwrap().seeded;
        let first = service.ensure_init().await.expect("initial init");
        let pid = backend_pid(&first.storage).await;
        let (observed, mut current) = observe(Ok(first.clone()), pid, first.clone()).await;
        drop(first);
        let mut phases = vec![json!({"phase": "initial", "observed": observed})];
        run_helper(&["--dump-db", "initial"]);

        // Death with no other writer: the next call reconnects and keeps
        // the resident copy, and the board keeps its seeded clock.
        board.hlc.lock().unwrap().seeded = true;
        let pid = backend_pid(&current.storage).await;
        kill(&current.storage, &dsn).await;
        past_probe().await;
        let result = service.ensure_init().await;
        let (observed, next) = observe(result, pid, current).await;
        current = next;
        assert!(
            seeded(),
            "a reconnect with no other writer keeps the board clock"
        );
        phases.push(json!({"phase": "death", "observed": observed}));
        run_helper(&["--dump-db", "death"]);

        // Another writer held the bank in the gap: the reconnect flags the
        // handover, the board clock must reseed before its next send, and
        // the service re-reads the bank before serving.
        let pid = backend_pid(&current.storage).await;
        kill(&current.storage, &dsn).await;
        run_helper(&["--other-writer"]);
        past_probe().await;
        let result = service.ensure_init().await;
        let (observed, next) = observe(result, pid, current).await;
        current = next;
        assert!(!seeded(), "a handover must unseed the board clock");
        phases.push(json!({"phase": "handover", "observed": observed}));
        run_helper(&["--dump-db", "handover"]);

        // Another writer still holds the lease: refused, and /health says so
        // while it is there; once it leaves and the retry window passes the
        // next call reconnects and re-reads the bank.
        let pid = backend_pid(&current.storage).await;
        kill(&current.storage, &dsn).await;
        let gate = PathBuf::from(std::env::var("PL_PGS_WRITER_GATE").unwrap());
        let ready_file = gate.with_extension("ready");
        std::fs::write(&gate, b"hold").unwrap();
        let mut holder = helper(&["--hold-lease"]).spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(60);
        while !ready_file.exists() {
            assert!(
                holder.try_wait().unwrap().is_none(),
                "the lease holder exited before taking the lease"
            );
            assert!(
                Instant::now() < deadline,
                "the lease holder never took the lease"
            );
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        past_probe().await;
        let result = service.ensure_init().await;
        let (observed, next) = observe(result, pid, current).await;
        current = next;
        phases.push(json!({"phase": "held", "observed": observed}));
        std::fs::remove_file(&gate).unwrap();
        assert!(holder.wait().unwrap().success(), "lease holder failed");
        run_helper(&["--dump-db", "held"]);
        tokio::time::sleep(Duration::from_millis(5100)).await;
        let result = service.ensure_init().await;
        let (observed, next) = observe(result, pid, current).await;
        current = next;
        phases.push(json!({"phase": "released", "observed": observed}));
        run_helper(&["--dump-db", "released"]);

        std::fs::write(
            std::env::var("PL_PGS_WRITER_OUT").unwrap(),
            serde_json::to_vec(&json!({"phases": phases})).unwrap(),
        )
        .unwrap();
        drop(current);
        drop(board);
        let storage = {
            let mut inner = service.inner.lock().await;
            inner.ready = None;
            inner.storage.take().unwrap()
        };
        service.update(|snapshot| {
            snapshot.storage = None;
            snapshot.startup = None;
        });
        drop(service);
        Arc::try_unwrap(storage)
            .ok()
            .expect("fixture owns the writer session")
            .close()
            .await;
    }
}
