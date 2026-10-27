//! Owned, idempotent background scheduling (`mcp_server.py:3165-3301`).

use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::watch;
use tokio::task::JoinHandle;

pub type Duty<'a> = Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>>;

/// Parse the background settings at their Python lifecycle boundary.
pub fn seconds(raw: Option<&str>, default: f64) -> Result<f64, String> {
    match raw {
        None => Ok(default),
        Some(raw) => {
            let text = crate::storage::py_strip(raw);
            let bytes = text.as_bytes();
            for (index, byte) in bytes.iter().enumerate() {
                if *byte == b'_'
                    && !(index > 0
                        && index + 1 < bytes.len()
                        && bytes[index - 1].is_ascii_digit()
                        && bytes[index + 1].is_ascii_digit())
                {
                    return Err("background interval is not a number".into());
                }
            }
            text.replace('_', "")
                .parse::<f64>()
                .map_err(|_| "background interval is not a number".into())
        }
    }
}

/// The writer and dream slices provide the work; this module owns when
/// it runs. Callbacks finish before shutdown joins their tasks, so a
/// durable write is never abandoned half way through an await.
pub trait Duties: Send + Sync + 'static {
    fn autosave(&self) -> Duty<'_>;
    fn warmup(&self, stop: watch::Receiver<bool>) -> Duty<'_>;
    fn reap(&self, idle_seconds: f64) -> Duty<'_>;
    /// One sweep tick; `stop` fires when shutdown begins, so a long
    /// lock-free phase (a model call) can give way to it.
    fn sweep(&self, stop: watch::Receiver<bool>) -> Duty<'_>;
}

pub struct Background {
    durability_started: AtomicBool,
    reaper_started: AtomicBool,
    sweep_started: AtomicBool,
    trim_started: AtomicBool,
    stop: watch::Sender<bool>,
    tasks: Mutex<Vec<JoinHandle<()>>>,
}

impl Default for Background {
    fn default() -> Self {
        Self {
            durability_started: AtomicBool::new(false),
            reaper_started: AtomicBool::new(false),
            sweep_started: AtomicBool::new(false),
            trim_started: AtomicBool::new(false),
            stop: watch::channel(false).0,
            tasks: Mutex::new(Vec::new()),
        }
    }
}

impl Background {
    pub fn start_dream_sweep(
        &self,
        duties: Arc<dyn Duties>,
        enabled: bool,
        retrieval_log: bool,
        seconds: f64,
    ) -> bool {
        if !(enabled || retrieval_log) || self.sweep_started.swap(true, Ordering::SeqCst) {
            return false;
        }
        let stop = self.stop.subscribe();
        let sweep_stop = self.stop.subscribe();
        self.push(tokio::spawn(async move {
            periodic(stop, seconds, "dream sweep", move || {
                let duties = duties.clone();
                let stop = sweep_stop.clone();
                Box::pin(async move { duties.sweep(stop).await }) as Duty<'static>
            })
            .await;
        }));
        true
    }
    pub fn start_release_check(
        &self,
        check: Arc<crate::release_check::ReleaseCheck>,
        config: &crate::config::UpdatesConfig,
    ) {
        if let Some(handle) = check.start(
            config.check_releases,
            std::env::var("PSEUDOLIFE_RELEASE_CHECK").ok().as_deref(),
            config.check_interval_seconds as f64,
            self.stop.subscribe(),
        ) {
            self.push(handle);
        }
    }

    pub fn start_heap_trim(&self, seconds: f64) {
        if seconds <= 0.0
            || !crate::heap_trim::available()
            || self.trim_started.swap(true, Ordering::SeqCst)
        {
            return;
        }
        let stop = self.stop.subscribe();
        self.push(tokio::spawn(async move {
            periodic(stop, seconds, "heap trim", || {
                Box::pin(async {
                    tokio::task::spawn_blocking(crate::heap_trim::trim_once)
                        .await
                        .map_err(|e| e.to_string())?;
                    Ok(())
                })
            })
            .await;
        }));
    }
    fn push(&self, handle: JoinHandle<()>) {
        self.tasks.lock().expect("background tasks").push(handle);
    }

    pub fn start_background_durability(&self, duties: Arc<dyn Duties>, seconds: f64) -> bool {
        if self.durability_started.swap(true, Ordering::SeqCst) {
            return false;
        }
        let save = duties.clone();
        let stop = self.stop.subscribe();
        self.push(tokio::spawn(async move {
            periodic(stop, seconds, "autosave loop", move || {
                let save = save.clone();
                Box::pin(async move { save.autosave().await }) as Duty<'static>
            })
            .await;
        }));
        let warmup_stop = self.stop.subscribe();
        self.push(tokio::spawn(async move {
            if let Err(e) = duties.warmup(warmup_stop).await {
                eprintln!("warmup error: {e}");
            }
        }));
        true
    }

    pub fn start_session_reaper(&self, duties: Arc<dyn Duties>, seconds: f64, idle: f64) -> bool {
        if self.reaper_started.swap(true, Ordering::SeqCst) {
            return false;
        }
        let stop = self.stop.subscribe();
        self.push(tokio::spawn(async move {
            periodic(stop, seconds, "session reaper", move || {
                let duties = duties.clone();
                Box::pin(async move { duties.reap(idle).await }) as Duty<'static>
            })
            .await;
        }));
        true
    }

    /// Stop the background tasks, then run the final save, each within its
    /// share of the exit budget ([`JOIN_DEADLINE`], [`SAVE_DEADLINE`]).
    pub async fn shutdown(&self, duties: &dyn Duties) {
        self.shutdown_within(duties, JOIN_DEADLINE, SAVE_DEADLINE)
            .await
    }

    /// [`Background::shutdown`] with deadlines relative to now.
    pub async fn shutdown_within(&self, duties: &dyn Duties, join: Duration, save: Duration) {
        let now = tokio::time::Instant::now();
        self.shutdown_until(duties, now + join, now + join + save)
            .await
    }

    /// Stop the background tasks, waiting for them until `join_until`, then
    /// run the final save until `save_until`. A task still running at its
    /// deadline is no longer waited for (it ends with the process); a final
    /// save still running at its deadline is abandoned, and its open
    /// transaction rolls back when the session closes at exit.
    pub async fn shutdown_until(
        &self,
        duties: &dyn Duties,
        join_until: tokio::time::Instant,
        save_until: tokio::time::Instant,
    ) {
        self.stop.send_replace(true);
        let handles = std::mem::take(&mut *self.tasks.lock().expect("background tasks"));
        let mut left = 0;
        for handle in handles {
            match tokio::time::timeout_at(join_until, handle).await {
                Ok(Ok(())) => {}
                Ok(Err(e)) => eprintln!("background task failed: {e}"),
                Err(_) => left += 1,
            }
        }
        if left > 0 {
            eprintln!(
                "shutdown: {left} background task(s) still running; leaving them to the exit"
            );
        }
        if self.durability_started.load(Ordering::SeqCst) {
            match tokio::time::timeout_at(save_until, duties.autosave()).await {
                Ok(Ok(())) => {}
                Ok(Err(e)) => eprintln!("exit flush failed: {e}"),
                Err(_) => eprintln!(
                    "exit flush failed: not finished by its deadline (the database may be unresponsive)"
                ),
            }
        }
    }
}

/// The exit budget. Python sets none (uvicorn waits for open requests, then
/// the atexit save runs, however long each takes); in the container both
/// are cut by Docker's kill, 10 s after its SIGTERM by default
/// (`ops/docker-compose.yml` sets no `stop_grace_period`). Here every stage
/// ends at a fixed point after the signal, so the final save gets its turn
/// before that kill and time a stage does not use passes to the next: open
/// HTTP requests are waited for until 3 s (a board wait can park for 30 s),
/// admitted writes until 6 s, background tasks until 7.5 s, the final save
/// until 9 s, and writes still running after the save until 9 s too. Not
/// measured: a store over a healthy database takes a fraction of a second.
/// Work still running at the end is left to the exit, never cancelled; a
/// store's eviction and insert (two transactions in both daemons) can then
/// be torn apart, as the kill would tear them a second later.
pub const HTTP_DRAIN: Duration = Duration::from_secs(3);
pub const WRITES_DRAIN: Duration = Duration::from_secs(3);
pub const JOIN_DEADLINE: Duration = Duration::from_millis(1500);
pub const SAVE_DEADLINE: Duration = Duration::from_millis(1500);

async fn periodic<F>(mut stop: watch::Receiver<bool>, seconds: f64, name: &str, mut callback: F)
where
    F: FnMut() -> Duty<'static>,
{
    if !seconds.is_finite() || seconds < 0.0 {
        return;
    }
    let Ok(interval) = Duration::try_from_secs_f64(seconds) else {
        return;
    };
    loop {
        if *stop.borrow() {
            return;
        }
        tokio::select! {
            _ = stop.changed() => return,
            _ = tokio::time::sleep(interval) => {}
        }
        if let Err(e) = callback().await {
            eprintln!("{name} error: {e}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    #[test]
    fn seconds_underscores_match_python_float() {
        // CPython float(raw), checked 2026-10-10: separators sit between digits.
        for raw in ["1__0", "_10", "10_", "1_.0", "1._0", "1_e2", "1e_2", "+_10"] {
            assert!(seconds(Some(raw), 30.0).is_err(), "{raw:?}");
        }
        for (raw, expected) in [
            ("1_0", 10.0),
            ("  +1_0.0_5e+1_0\t", 100_500_000_000.0),
            (".1_0", 0.1),
            ("-0_0", -0.0),
        ] {
            assert_eq!(seconds(Some(raw), 30.0), Ok(expected), "{raw:?}");
        }
        assert_eq!(seconds(None, 30.0), Ok(30.0));
    }

    /// A save stuck on an unresponsive database, and a task that ignores
    /// the stop signal: shutdown still ends within its deadlines.
    struct Stuck {
        saves: AtomicUsize,
    }
    impl Duties for Stuck {
        fn sweep(&self, _stop: watch::Receiver<bool>) -> Duty<'_> {
            Box::pin(async { Ok(()) })
        }
        fn autosave(&self) -> Duty<'_> {
            Box::pin(async {
                self.saves.fetch_add(1, Ordering::SeqCst);
                std::future::pending::<()>().await;
                Ok(())
            })
        }
        fn warmup(&self, _stop: watch::Receiver<bool>) -> Duty<'_> {
            Box::pin(async { Ok(()) })
        }
        fn reap(&self, _: f64) -> Duty<'_> {
            Box::pin(async { Ok(()) })
        }
    }

    #[tokio::test]
    async fn shutdown_ends_within_its_deadlines_when_work_is_stuck() {
        let background = Background::default();
        background.durability_started.store(true, Ordering::SeqCst);
        background.push(tokio::spawn(std::future::pending::<()>()));
        let stuck = Stuck {
            saves: AtomicUsize::new(0),
        };
        let (join, save) = (Duration::from_millis(100), Duration::from_millis(100));
        let started = tokio::time::Instant::now();
        tokio::time::timeout(
            Duration::from_secs(10),
            background.shutdown_within(&stuck, join, save),
        )
        .await
        .expect("shutdown must not wait forever on stuck work");
        // The final save was attempted, and the whole shutdown fit in the
        // two deadlines (with scheduling slack).
        assert_eq!(stuck.saves.load(Ordering::SeqCst), 1);
        assert!(started.elapsed() < join + save + Duration::from_secs(2));
    }

    #[derive(Default)]
    struct Fake {
        warm: AtomicUsize,
        save: AtomicUsize,
        reap: AtomicUsize,
    }
    impl Duties for Fake {
        fn sweep(&self, _stop: watch::Receiver<bool>) -> Duty<'_> {
            Box::pin(async { Ok(()) })
        }
        fn autosave(&self) -> Duty<'_> {
            Box::pin(async {
                self.save.fetch_add(1, Ordering::SeqCst);
                Ok(())
            })
        }
        fn warmup(&self, _stop: watch::Receiver<bool>) -> Duty<'_> {
            Box::pin(async {
                self.warm.fetch_add(1, Ordering::SeqCst);
                Ok(())
            })
        }
        fn reap(&self, _: f64) -> Duty<'_> {
            Box::pin(async {
                let n = self.reap.fetch_add(1, Ordering::SeqCst);
                if n == 0 {
                    Err("injected first tick failure".into())
                } else {
                    Ok(())
                }
            })
        }
    }

    #[tokio::test]
    async fn starts_once_survives_errors_and_joins_before_exit_save() {
        let bg = Background::default();
        let fake = Arc::new(Fake::default());
        assert!(bg.start_background_durability(fake.clone(), 3600.0));
        assert!(!bg.start_background_durability(fake.clone(), 3600.0));
        assert!(bg.start_session_reaper(fake.clone(), 0.001, 30.0));
        assert!(!bg.start_session_reaper(fake.clone(), 0.001, 30.0));
        tokio::time::timeout(Duration::from_secs(2), async {
            while fake.reap.load(Ordering::SeqCst) < 2 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("second tick after error");
        bg.shutdown(fake.as_ref()).await;
        assert_eq!(fake.warm.load(Ordering::SeqCst), 1);
        assert_eq!(fake.save.load(Ordering::SeqCst), 1);
        let stopped = fake.reap.load(Ordering::SeqCst);
        tokio::time::sleep(Duration::from_millis(10)).await;
        assert_eq!(fake.reap.load(Ordering::SeqCst), stopped);
    }

    #[tokio::test]
    async fn sweep_enablement_and_trim_are_registered_once() {
        let bg = Background::default();
        let fake = Arc::new(Fake::default());
        assert!(!bg.start_dream_sweep(fake.clone(), false, false, 3600.0));
        assert!(bg.start_dream_sweep(fake.clone(), false, true, 3600.0));
        assert!(!bg.start_dream_sweep(fake.clone(), true, true, 3600.0));
        let before = bg.tasks.lock().unwrap().len();
        bg.start_heap_trim(3600.0);
        bg.start_heap_trim(3600.0);
        let expected = before + usize::from(crate::heap_trim::available());
        assert_eq!(bg.tasks.lock().unwrap().len(), expected);
        bg.shutdown(fake.as_ref()).await;
    }
}
