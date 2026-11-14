//! Ctrl-C, SIGTERM and SIGHUP (a console close or Ctrl-Break on Windows)
//! while restore owns the bank: the signal only raises a flag. The running
//! Docker command is in its own process group, so it is not signalled; the
//! runner stops it when its step allows, and restore then reports the
//! bank's state and stops at that step boundary.
#![allow(unsafe_code)]
use std::sync::atomic::{AtomicBool, Ordering};

static INTERRUPTED: AtomicBool = AtomicBool::new(false);

pub(super) fn requested() -> bool {
    INTERRUPTED.load(Ordering::SeqCst)
}

#[cfg(unix)]
extern "C" fn receive(_signal: libc::c_int) {
    INTERRUPTED.store(true, Ordering::SeqCst);
}

#[cfg(windows)]
extern "system" fn receive(_kind: u32) -> windows_sys::core::BOOL {
    INTERRUPTED.store(true, Ordering::SeqCst);
    // Handled: Windows does not end the process for Ctrl-C or Ctrl-Break.
    // A console close still ends it after the handler returns, which the
    // recovery note beside the safety backup covers.
    1
}

/// The handlers, installed for the guard's life.
pub(super) struct Guard {
    #[cfg(unix)]
    previous: Vec<(libc::c_int, libc::sigaction)>,
}

impl Guard {
    pub(super) fn install() -> Self {
        #[cfg(unix)]
        {
            let mut previous = Vec::new();
            for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
                // SAFETY: zeroed sigaction values are valid; the handler only
                // stores to a static atomic; the prior action is restored on
                // drop.
                unsafe {
                    let mut action: libc::sigaction = std::mem::zeroed();
                    let mut old: libc::sigaction = std::mem::zeroed();
                    action.sa_sigaction = receive as *const () as libc::sighandler_t;
                    action.sa_flags = libc::SA_RESTART;
                    libc::sigemptyset(&mut action.sa_mask);
                    if libc::sigaction(signal, &action, &mut old) == 0 {
                        previous.push((signal, old));
                    }
                }
            }
            Self { previous }
        }
        #[cfg(windows)]
        {
            // SAFETY: the callback has the system ABI and static lifetime.
            unsafe {
                windows_sys::Win32::System::Console::SetConsoleCtrlHandler(Some(receive), 1);
            }
            Self {}
        }
    }
}

impl Drop for Guard {
    fn drop(&mut self) {
        #[cfg(unix)]
        for (signal, old) in &self.previous {
            // SAFETY: restores the exact prior disposition.
            unsafe {
                libc::sigaction(*signal, old, std::ptr::null_mut());
            }
        }
        #[cfg(windows)]
        // SAFETY: removes this exact static callback registration.
        unsafe {
            windows_sys::Win32::System::Console::SetConsoleCtrlHandler(Some(receive), 0);
        }
    }
}
