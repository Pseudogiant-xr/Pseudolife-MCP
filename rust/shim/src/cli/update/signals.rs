//! Temporary signal ownership while updater commands own process groups.
//! Nested commands defer re-raising until the enclosing cleanup scope finishes.
#![allow(unsafe_code)]
use std::{
    cell::Cell,
    sync::{
        Mutex, MutexGuard,
        atomic::{AtomicBool, AtomicI32, AtomicU64, AtomicUsize, Ordering},
    },
    time::Duration,
};

static COMMAND: Mutex<()> = Mutex::new(());
static RECEIVED: AtomicI32 = AtomicI32::new(0);
static LATEST: AtomicI32 = AtomicI32::new(0);
static COUNT: AtomicUsize = AtomicUsize::new(0);
static SUSPEND: AtomicBool = AtomicBool::new(false);
static STOPPED_NANOS: AtomicU64 = AtomicU64::new(0);
thread_local! { static DEPTH: Cell<usize> = const { Cell::new(0) }; }

extern "C" fn receive(signal: libc::c_int) {
    if signal == libc::SIGTSTP {
        SUSPEND.store(true, Ordering::Relaxed);
    } else {
        let _ = RECEIVED.compare_exchange(0, signal, Ordering::Relaxed, Ordering::Relaxed);
        LATEST.store(signal, Ordering::Relaxed);
        COUNT.fetch_add(1, Ordering::Release);
    }
}

pub(super) struct Guard {
    previous: Vec<(i32, libc::sigaction)>,
    _command: Option<MutexGuard<'static, ()>>,
}

impl Guard {
    pub(super) fn new() -> std::io::Result<Self> {
        if DEPTH.get() != 0 {
            DEPTH.set(DEPTH.get() + 1);
            return Ok(Self {
                previous: Vec::new(),
                _command: None,
            });
        }
        let command = COMMAND.lock().unwrap_or_else(|e| e.into_inner());
        RECEIVED.store(0, Ordering::Relaxed);
        LATEST.store(0, Ordering::Relaxed);
        COUNT.store(0, Ordering::Relaxed);
        SUSPEND.store(false, Ordering::Relaxed);
        STOPPED_NANOS.store(0, Ordering::Relaxed);
        DEPTH.set(1);
        let mut guard = Self {
            previous: Vec::new(),
            _command: Some(command),
        };
        for signal in [
            libc::SIGINT,
            libc::SIGTERM,
            libc::SIGHUP,
            libc::SIGQUIT,
            libc::SIGTSTP,
        ] {
            // SAFETY: initialized buffers, a static atomic-only handler, and
            // process-wide serialization of updater signal ownership.
            unsafe {
                let mut previous: libc::sigaction = std::mem::zeroed();
                if libc::sigaction(signal, std::ptr::null(), &mut previous) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                // Background shells may deliberately ignore interrupts.
                if previous.sa_sigaction == libc::SIG_IGN {
                    continue;
                }
                let mut action: libc::sigaction = std::mem::zeroed();
                action.sa_sigaction = receive as *const () as libc::sighandler_t;
                libc::sigemptyset(&mut action.sa_mask);
                if libc::sigaction(signal, &action, std::ptr::null_mut()) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                guard.previous.push((signal, previous));
            }
        }
        Ok(guard)
    }
}

pub(super) fn received() -> i32 {
    RECEIVED.load(Ordering::Relaxed)
}

pub(super) fn owns_scope() -> bool {
    DEPTH.get() != 0
}

/// Allow only an explicitly owned recovery action to run after cancellation.
/// A further signal still cancels that action; the first signal remains pending
/// for the outer guard when recovery finishes.
pub(super) struct CleanupSignal(i32);
impl CleanupSignal {
    pub(super) fn new() -> Self {
        Self(RECEIVED.swap(0, Ordering::Relaxed))
    }
}
impl Drop for CleanupSignal {
    fn drop(&mut self) {
        if self.0 != 0 {
            let _ = RECEIVED.compare_exchange(0, self.0, Ordering::Relaxed, Ordering::Relaxed);
        }
    }
}

fn suspend(group: Option<u32>) {
    if SUSPEND.swap(false, Ordering::Relaxed) {
        let stopped = std::time::Instant::now();
        // SAFETY: group is the owned child's process group, captured at spawn.
        // SIGSTOP stops this process even in an orphaned background group. After
        // the shell continues us, continue the isolated child group as well.
        unsafe {
            if let Some(group) = group {
                libc::kill(-(group as i32), libc::SIGTSTP);
            }
            libc::raise(libc::SIGSTOP);
            if let Some(group) = group {
                libc::kill(-(group as i32), libc::SIGCONT);
            }
        }
        STOPPED_NANOS.fetch_add(
            stopped.elapsed().as_nanos().min(u64::MAX as u128) as u64,
            Ordering::Relaxed,
        );
    }
}

/// Preserve the remaining command budget across terminal suspension. The
/// signal future records stopped time before this future is polled on resume.
pub(super) async fn timeout<F: std::future::Future>(
    duration: Duration,
    future: F,
) -> Result<F::Output, tokio::time::error::Elapsed> {
    let mut stopped = STOPPED_NANOS.load(Ordering::Relaxed);
    let mut deadline = tokio::time::Instant::now() + duration;
    tokio::pin!(future);
    loop {
        // Borrow the same pinned future: extending an expired wall-clock timer
        // must not cancel or restart the command and its output readers.
        let result = tokio::time::timeout_at(deadline, &mut future).await;
        let total = STOPPED_NANOS.load(Ordering::Relaxed);
        if result.is_ok() || total == stopped {
            return result;
        }
        deadline += Duration::from_nanos(total.saturating_sub(stopped));
        stopped = total;
    }
}

pub(super) fn checkpoint() -> i32 {
    suspend(None);
    received()
}

/// A health retry delay remains interruptible while a shadow owns cleanup.
pub(super) fn sleep(duration: Duration) {
    let start = std::time::Instant::now();
    while start.elapsed() < duration && checkpoint() == 0 {
        std::thread::sleep(
            duration
                .saturating_sub(start.elapsed())
                .min(Duration::from_millis(20)),
        );
    }
}

pub(super) async fn wait(group: Option<u32>) -> i32 {
    loop {
        suspend(group);
        let signal = received();
        if signal != 0 {
            return signal;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

/// Forward repeated interrupts without extending the original cleanup deadline.
pub(super) async fn terminate(child: &mut dyn process_wrap::tokio::ChildWrapper, signal: i32) {
    let _ = child.signal(signal);
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    let mut seen = 1;
    let group = child.id();
    loop {
        tokio::select! {
            _ = tokio::time::timeout_at(deadline, child.wait()) => break,
            _ = async {
                while COUNT.load(Ordering::Acquire) <= seen {
                    suspend(group);
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
            } => {
                seen = COUNT.load(Ordering::Acquire);
                let _ = child.signal(LATEST.load(Ordering::Relaxed));
            }
        }
    }
    let _ = child.start_kill();
    let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;
}

impl Drop for Guard {
    fn drop(&mut self) {
        DEPTH.set(DEPTH.get() - 1);
        if self._command.is_none() {
            return;
        }
        for (signal, previous) in &self.previous {
            // SAFETY: restore the exact disposition saved by this outer guard.
            unsafe { libc::sigaction(*signal, previous, std::ptr::null_mut()) };
        }
        let signal = received();
        if signal != 0 {
            // SAFETY: owned commands and enclosing recovery actions have now
            // finished. Restore normal signal exit semantics. A returning custom
            // handler must not allow the update to begin another mutation.
            unsafe { libc::raise(signal) };
            std::process::exit(128 + signal);
        }
    }
}
