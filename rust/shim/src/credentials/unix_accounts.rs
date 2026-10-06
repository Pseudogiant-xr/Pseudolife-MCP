//! Reentrant account-database lookup, retaining the former users 0.11 behavior.
use std::{ffi::CString, path::PathBuf};

#[allow(unsafe_code)]
pub(super) fn home_by_name(name: &str) -> Option<PathBuf> {
    // SAFETY: libc's reentrant lookup obeys the buffer/result contract documented
    // below. No returned pointer or borrowed account field escapes the call.
    unsafe {
        home_with_lookup(name, |name, entry, buffer, result| {
            libc::getpwnam_r(
                name.as_ptr(),
                entry,
                buffer.as_mut_ptr(),
                buffer.len(),
                result,
            )
        })
    }
}

/// The lookup must obey getpwnam_r's contract: a successful result points to
/// `entry`, with valid NUL-terminated fields alive until the buffer is dropped.
/// Error results must be null or a different pointer, except ERANGE, which is
/// retried without reading the entry. The caller cannot retain borrowed storage.
#[allow(unsafe_code)]
unsafe fn home_with_lookup(
    name: &str,
    mut lookup: impl FnMut(
        &CString,
        &mut libc::passwd,
        &mut [libc::c_char],
        &mut *mut libc::passwd,
    ) -> libc::c_int,
) -> Option<PathBuf> {
    let name = CString::new(name).ok()?;
    // SAFETY: all passwd fields admit zero initialization; libc fills them
    // before a successful result is inspected.
    let mut entry: libc::passwd = unsafe { std::mem::zeroed() };
    let mut buffer = vec![0; 2048];
    let mut result = std::ptr::null_mut();
    while lookup(&name, &mut entry, &mut buffer, &mut result) == libc::ERANGE {
        let size = buffer.len().checked_mul(2)?;
        buffer.resize(size, 0);
    }
    if !std::ptr::eq(result, &entry) {
        return None;
    }
    #[cfg(target_os = "android")]
    {
        // users 0.11 supplies this default instead of Android's passwd fields.
        Some(PathBuf::from("/var/empty"))
    }
    #[cfg(not(target_os = "android"))]
    {
        use std::{ffi::CStr, os::unix::ffi::OsStrExt};
        // SAFETY: result identifies the initialized entry; the lookup contract
        // guarantees a live NUL-terminated home field. Copy before buffer drop.
        let home = unsafe { CStr::from_ptr(entry.pw_dir) };
        Some(PathBuf::from(std::ffi::OsStr::from_bytes(home.to_bytes())))
    }
}

#[cfg(test)]
mod tests {
    include!("../../tests/unix_accounts_lookup/mod.rs");
}
