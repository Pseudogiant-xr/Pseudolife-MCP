//! Temporary signal ownership while an updater command owns a process group.
//! Restore the prior dispositions before re-raising, after child cleanup.
#![allow(unsafe_code)]
use std::sync::{
    Mutex, MutexGuard,
    atomic::{AtomicI32, Ordering},
};

static COMMAND: Mutex<()> = Mutex::new(());
static RECEIVED: AtomicI32 = AtomicI32::new(0);

extern "C" fn receive(signal: libc::c_int) {
    let _ = RECEIVED.compare_exchange(0, signal, Ordering::Relaxed, Ordering::Relaxed);
}

pub(super) struct Guard {
    previous: Vec<(i32, libc::sigaction)>,
    _command: MutexGuard<'static, ()>,
}

impl Guard {
    pub(super) fn new() -> std::io::Result<Self> {
        let command = COMMAND.lock().unwrap_or_else(|e| e.into_inner());
        RECEIVED.store(0, Ordering::Relaxed);
        let mut guard = Self {
            previous: Vec::new(),
            _command: command,
        };
        for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
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

pub(super) async fn wait() -> i32 {
    loop {
        let signal = received();
        if signal != 0 {
            return signal;
        }
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
}

impl Drop for Guard {
    fn drop(&mut self) {
        for (signal, previous) in &self.previous {
            // SAFETY: restore the exact disposition saved by this guard.
            unsafe { libc::sigaction(*signal, previous, std::ptr::null_mut()) };
        }
        let signal = received();
        if signal != 0 {
            // SAFETY: re-deliver only a signal received while owning a child,
            // after its group was terminated and reaped. If a previous custom
            // handler returns, still stop the update rather than start another
            // mutation. The OS releases the update lock on exit.
            unsafe { libc::raise(signal) };
            std::process::exit(128 + signal);
        }
    }
}
