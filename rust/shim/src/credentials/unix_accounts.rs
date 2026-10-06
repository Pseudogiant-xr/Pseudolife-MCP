//! Reentrant account-database lookup with validated libc results.
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

/// When status is zero and result identifies `entry`, each non-null field must
/// be NUL-terminated and alive until the buffer is dropped. Other results and
/// null home fields are rejected without reading them. The caller cannot retain
/// borrowed storage.
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
    let status = loop {
        let status = lookup(&name, &mut entry, &mut buffer, &mut result);
        if status != libc::ERANGE {
            break status;
        }
        let size = buffer.len().checked_mul(2)?;
        buffer.resize(size, 0);
    };
    if status != 0 || !std::ptr::eq(result, &entry) {
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
        if entry.pw_dir.is_null() {
            return None;
        }
        // SAFETY: status is zero, result identifies the initialized entry, and
        // pw_dir is non-null. The lookup contract guarantees its live NUL-
        // terminated storage. Copy before buffer drop.
        let home = unsafe { CStr::from_ptr(entry.pw_dir) };
        Some(PathBuf::from(std::ffi::OsStr::from_bytes(home.to_bytes())))
    }
}

#[cfg(test)]
mod tests {
    include!("../../tests/unix_accounts_lookup/mod.rs");
}
