#![allow(unsafe_code)]

use std::io;

/// Keep CPython's inherited SIG_IGN contract before registering with Tokio.
pub(super) fn ignored() -> io::Result<bool> {
    // SAFETY: sigaction with a null action only reads the disposition into
    // our initialized, writable storage. It installs no handler and retains
    // no pointer after returning. Only a successful query is inspected.
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        if libc::sigaction(libc::SIGINT, std::ptr::null(), &mut action) != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(action.sa_sigaction == libc::SIG_IGN)
    }
}
