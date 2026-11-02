//! Preserve the actual displaced configuration, including concurrent writes.
use super::*;
use registrations::read_optional;
use std::io::Write;

pub fn write_config(path: &Path, before: &[u8], after: &[u8]) -> Result<PathBuf> {
    write_config_with(path, before, after, || {})
}

pub(super) fn write_config_with(
    path: &Path,
    before: &[u8],
    after: &[u8],
    before_replace: impl FnOnce(),
) -> Result<PathBuf> {
    let target = if path.exists() {
        fs::canonicalize(path).map_err(io_error)?
    } else {
        path.to_owned()
    };
    crate::credentials::reject_ancestor_redirects(&target)
        .map_err(|_| "client config target is redirected")?;
    let parent = target.parent().ok_or("client config has no parent")?;
    if !parent.is_dir() {
        crate::board::state::private_dir(parent).map_err(str::to_owned)?;
    }
    let meta = match fs::metadata(&target) {
        Ok(meta) => Some(meta),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(io_error(error)),
    };
    #[cfg(unix)]
    if let Some(meta) = &meta {
        use std::os::unix::fs::MetadataExt;
        if meta.uid() != rustix::process::getuid().as_raw() || meta.mode() & 0o022 != 0 {
            return Err(
                "client configuration is not owned by this user or is writable by others".into(),
            );
        }
    }
    #[cfg(windows)]
    if meta.is_some() {
        let opened = fs::File::open(&target).map_err(io_error)?;
        crate::credentials::windows_security::validate(&opened)
            .map_err(|_| "client configuration is not owner-only")?;
    }
    let recovery = parent.join(format!(".client-native-backup-{}", uuid::Uuid::new_v4()));
    crate::board::state::private_dir(&recovery).map_err(str::to_owned)?;
    let observed = recovery.join("observed");
    crate::board::state::atomic_create(&observed, before).map_err(str::to_owned)?;
    let pending = recovery.join("pending");
    let previous = recovery.join("previous");
    let mut opened = crate::board::state::create(&pending).map_err(str::to_owned)?;
    opened
        .file
        .write_all(after)
        .and_then(|_| opened.file.sync_all())
        .map_err(io_error)?;
    #[cfg(unix)]
    if let Some(meta) = &meta {
        opened
            .file
            .set_permissions(meta.permissions())
            .map_err(io_error)?;
    }
    drop(opened);
    let result = (|| {
        if read_optional(&target)?.as_deref().unwrap_or_default() != before {
            return Err(
                "client configuration changed during the edit; newer content was preserved".into(),
            );
        }
        before_replace();
        let backup = if meta.is_some() {
            replace_preserving(&target, &pending, &previous).map_err(io_error)?;
            if read_optional(&previous)?.as_deref() != Some(before) {
                return Err("client configuration changed during replacement; the actual displaced file was preserved; review the recovery copy before retrying".into());
            }
            previous
        } else {
            // Atomic create-if-absent: a concurrent new file is never replaced.
            fs::hard_link(&pending, &target).map_err(io_error)?;
            let _ = fs::remove_file(&pending);
            observed
        };
        if read_optional(&target)?.as_deref() != Some(after) {
            return Err(
                "client configuration changed after the edit; newer content was preserved".into(),
            );
        }
        Ok(backup)
    })();
    result.map_err(|error: String| format!("{error}; recovery files: {}", recovery.display()))
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn replace_preserving(target: &Path, pending: &Path, previous: &Path) -> io::Result<()> {
    rustix::fs::renameat_with(
        rustix::fs::CWD,
        pending,
        rustix::fs::CWD,
        target,
        rustix::fs::RenameFlags::EXCHANGE,
    )?;
    // If this rename fails, the displaced file remains in the private pending
    // path. Never delete it or roll back over a newer writer's target.
    fs::rename(pending, previous)
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn replace_preserving(target: &Path, pending: &Path, previous: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    let wide = |path: &Path| -> io::Result<Vec<u16>> {
        let units: Vec<_> = path.as_os_str().encode_wide().collect();
        if units.contains(&0) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "path contains NUL",
            ));
        }
        Ok(units.into_iter().chain([0]).collect())
    };
    let target_w = wide(target)?;
    let pending_w = wide(pending)?;
    let previous_w = wide(previous)?;
    // ReplaceFileW retains the actual displaced file and its attributes. All
    // buffers live for the call; no ACL-merge errors are ignored.
    let replaced = unsafe {
        windows_sys::Win32::Storage::FileSystem::ReplaceFileW(
            target_w.as_ptr(),
            pending_w.as_ptr(),
            previous_w.as_ptr(),
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if replaced != 0 {
        return Ok(());
    }
    let error = io::Error::last_os_error();
    // ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 can leave the original at the backup
    // path. Restore only into an absent target; never overwrite another writer.
    if error.raw_os_error() == Some(1177) {
        let _ = fs::hard_link(previous, target);
    }
    Err(error)
}

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
fn replace_preserving(_: &Path, _: &Path, _: &Path) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "atomic configuration exchange is unavailable",
    ))
}
