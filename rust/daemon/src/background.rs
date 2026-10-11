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
    fn sweep(&self) -> Duty<'_>;
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
        self.push(tokio::spawn(async move {
            periodic(stop, seconds, "dream sweep", move || {
                let duties = duties.clone();
                Box::pin(async move { duties.sweep().await }) as Duty<'static>
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

    pub async fn shutdown(&self, duties: &dyn Duties) {
        self.stop.send_replace(true);
        let handles = std::mem::take(&mut *self.tasks.lock().expect("background tasks"));
        for handle in handles {
            if let Err(e) = handle.await {
                eprintln!("background task failed: {e}");
            }
        }
        if self.durability_started.load(Ordering::SeqCst)
            && let Err(e) = duties.autosave().await
        {
            eprintln!("exit flush failed: {e}");
        }
    }
}

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

    #[derive(Default)]
    struct Fake {
        warm: AtomicUsize,
        save: AtomicUsize,
        reap: AtomicUsize,
    }
    impl Duties for Fake {
        fn sweep(&self) -> Duty<'_> {
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
