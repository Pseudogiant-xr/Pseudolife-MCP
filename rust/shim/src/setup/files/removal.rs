//! Compensate only the private file published by this transaction.
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
};

pub(crate) fn restore_existing(
    source: &Path,
    target: &Path,
    identity: &[u128],
    bytes: &[u8],
    published: &mut bool,
    backup: Option<&Path>,
) -> Result<(), String> {
    restore_after_check(source, target, identity, bytes, published, || {}, || {}).map_err(|error| {
        match backup {
            Some(path) => format!("{error} Backup retained at {path:?}."),
            None => error,
        }
    })
}

fn restore_after_check(
    source: &Path,
    target: &Path,
    identity: &[u128],
    bytes: &[u8],
    published: &mut bool,
    before_move: impl FnOnce(),
    before_publish: impl FnOnce(),
) -> Result<(), String> {
    verify_file(target, identity, bytes).map_err(|cause| {
        format!(
            "Setup restoration refused: {cause}; review the current file before retrying recovery."
        )
    })?;
    before_move();
    let moved = move_aside(target)?;
    if let Err(cause) = verify_file(&moved, identity, bytes) {
        return Err(return_file(&moved, target, &cause));
    }
    before_publish();
    if let Err(error) = restore_exclusive(source, target) {
        return Err(return_file(
            &moved,
            target,
            &format!(
                "restoration publication failed ({:?}: {error})",
                error.kind()
            ),
        ));
    }
    *published = true;
    cleanup_restored(
        target,
        &moved,
        || sync_parent(target),
        || fs::remove_file(&moved),
    )
}

fn sync_parent(path: &Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        let parent = path.parent().ok_or(std::io::ErrorKind::InvalidInput)?;
        fs::File::open(parent)?.sync_all()
    }
    #[cfg(windows)]
    {
        let _ = path;
        Ok(())
    }
}

fn cleanup_restored(
    target: &Path,
    moved: &Path,
    sync: impl FnOnce() -> std::io::Result<()>,
    remove: impl FnOnce() -> std::io::Result<()>,
) -> Result<(), String> {
    // The restored directory entry must reach disk even if cleanup fails.
    sync().map_err(|error| format!(
        "Setup file {target:?} was restored but its directory could not be synced ({:?}: {error}); previous owned file retained at {moved:?}. Review both paths before retrying recovery.", error.kind()
    ))?;
    // The public path is restored now. An unlink failure retains the old owned
    // file at its private name; it must not turn this into an uncommitted write.
    remove().map_err(|error| format!(
        "Setup file {target:?} was restored, but the previous owned file remains at {moved:?}: {:?}: {error}. Review and remove that retained file before retrying recovery.", error.kind()
    ))
}

pub(crate) fn remove_created(path: &Path, identity: &[u128], bytes: &[u8]) -> Result<(), String> {
    remove_after_check(path, identity, bytes, || {}, || {})
}

fn remove_after_check(
    path: &Path,
    identity: &[u128],
    bytes: &[u8],
    before_remove: impl FnOnce(),
    after_move: impl FnOnce(),
) -> Result<(), String> {
    before_remove();
    let moved = move_aside(path)?;
    after_move();
    let result = verify_file(&moved, identity, bytes).and_then(|()| {
        fs::remove_file(&moved)
            .map_err(|error| format!("removal failed ({:?}: {error})", error.kind()))
    });
    result.map_err(|cause| return_file(&moved, path, &cause))
}

fn move_aside(path: &Path) -> Result<PathBuf, String> {
    let moved = path.with_file_name(format!(
        ".pseudolife-rollback-{}",
        uuid::Uuid::new_v4().simple()
    ));
    restore_exclusive(path, &moved).map_err(|error| move_error(path, &error))?;
    Ok(moved)
}

fn move_error(path: &Path, error: &std::io::Error) -> String {
    if matches!(
        error.kind(),
        std::io::ErrorKind::InvalidInput | std::io::ErrorKind::Unsupported
    ) {
        return format!(
            "Setup file {path:?} could not be moved for rollback: this filesystem does not support exclusive rename ({:?}: {error}). The file remains at {path:?}; review the retained file and recover manually on a filesystem supporting exclusive rename.",
            error.kind()
        );
    }
    format!(
        "Setup file {path:?} could not be moved for rollback ({:?}: {error}); resolve the reported filesystem error, then retry recovery.",
        error.kind()
    )
}

fn verify_file(path: &Path, identity: &[u128], bytes: &[u8]) -> Result<(), String> {
    verify_after_open_error(path, identity, bytes, || {})
}

fn verify_after_open_error(
    path: &Path,
    identity: &[u128],
    bytes: &[u8],
    after_error: impl FnOnce(),
) -> Result<(), String> {
    let opened = crate::board::state::open_detailed(path).map_err(|cause| {
        after_error();
        format!("could not open or validate private file {path:?}: {cause}")
    })?;
    if opened.identity != identity {
        return Err(format!(
            "file {path:?} no longer matches this transaction's identity"
        ));
    }
    let mut current = Vec::new();
    (&opened.file)
        .take(bytes.len() as u64 + 1)
        .read_to_end(&mut current)
        .map_err(|error| {
            format!(
                "could not read private file {path:?}: {:?}: {error}",
                error.kind()
            )
        })?;
    if current != bytes {
        return Err(format!(
            "file {path:?} no longer matches this transaction's bytes"
        ));
    }
    Ok(())
}

fn return_file(moved: &Path, target: &Path, cause: &str) -> String {
    match restore_exclusive(moved, target) {
        Ok(()) => format!(
            "Setup rollback refused file {target:?}: {cause}; the file was returned to its original path. Review it before retrying recovery."
        ),
        Err(error) if fs::symlink_metadata(moved).is_ok() => format!(
            "Setup rollback refused file {target:?}: {cause}; the retained file is at {moved:?} because its original path could not be restored exclusively ({:?}: {error}). Review both paths and restore the retained file only after resolving the conflict, then retry recovery.",
            error.kind()
        ),
        Err(error) => format!(
            "Setup rollback refused file {target:?}: {cause}; the moved file is no longer available at {moved:?} ({:?}: {error}). Review both paths before retrying recovery.",
            error.kind()
        ),
    }
}

#[cfg(any(
    target_os = "linux",
    target_vendor = "apple",
    target_os = "android",
    target_os = "redox"
))]
fn restore_exclusive(source: &Path, target: &Path) -> std::io::Result<()> {
    // One exclusive rename has no partial hard-link/unlink state. Unsupported
    // filesystems refuse before publication rather than weakening that contract.
    rustix::fs::renameat_with(
        rustix::fs::CWD,
        source,
        rustix::fs::CWD,
        target,
        rustix::fs::RenameFlags::NOREPLACE,
    )
    .map_err(Into::into)
}

#[cfg(all(
    unix,
    not(any(
        target_os = "linux",
        target_vendor = "apple",
        target_os = "android",
        target_os = "redox"
    ))
))]
fn restore_exclusive(_source: &Path, _target: &Path) -> std::io::Result<()> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "exclusive rename is unavailable on this platform",
    ))
}

#[cfg(windows)]
#[allow(unsafe_code)] // Exclusive rename preserves a competing destination.
fn restore_exclusive(source: &Path, target: &Path) -> std::io::Result<()> {
    use windows_sys::Win32::Storage::FileSystem::MoveFileW;
    let source = move_path(source)?;
    let target = move_path(target)?;
    // SAFETY: the NUL-terminated path buffers live through this call.
    if unsafe { MoveFileW(source.as_ptr(), target.as_ptr()) } == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(windows)]
pub(super) fn move_path(path: &Path) -> std::io::Result<Vec<u16>> {
    use std::os::windows::ffi::OsStrExt;
    use std::path::{Component, Prefix};
    // GetFullPathNameW, used by absolute(), resolves relative paths and dot
    // components before the verbatim prefix disables Win32 normalization.
    let absolute = std::path::absolute(path)?;
    let wide: Vec<u16> = absolute.as_os_str().encode_wide().collect();
    if wide.contains(&0) {
        return Err(std::io::ErrorKind::InvalidInput.into());
    }
    let mut result: Vec<u16> = match absolute.components().next() {
        Some(Component::Prefix(prefix)) => match prefix.kind() {
            Prefix::Verbatim(_) | Prefix::VerbatimDisk(_) | Prefix::VerbatimUNC(_, _) => wide,
            Prefix::Disk(_) => r"\\?\".encode_utf16().chain(wide).collect(),
            Prefix::UNC(_, _) => r"\\?\UNC\"
                .encode_utf16()
                .chain(wide.into_iter().skip(2))
                .collect(),
            Prefix::DeviceNS(_) => return Err(std::io::ErrorKind::InvalidInput.into()),
        },
        _ => return Err(std::io::ErrorKind::InvalidInput.into()),
    };
    result.push(0);
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::board::state;
    use std::path::PathBuf;

    struct Home(PathBuf);
    impl Home {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("setup-remove-{}", uuid::Uuid::new_v4()));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Home {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[cfg(windows)]
    #[test]
    fn review_long_rollback_sibling_preserves_short_public_path_support() {
        use std::os::windows::ffi::OsStrExt;
        let home = Home::new();
        let length = home.0.as_os_str().encode_wide().count();
        let parent = home.0.join("p".repeat(224 - length - 1));
        fs::create_dir(&parent).unwrap();
        let target = parent.join("f");
        assert_eq!(target.as_os_str().encode_wide().count(), 226);
        assert!(
            parent
                .join(format!(".pseudolife-rollback-{}", "0".repeat(32)))
                .as_os_str()
                .encode_wide()
                .count()
                > 260
        );
        state::atomic_create(&target, b"created-value").unwrap();
        let identity = state::open(&target).unwrap().identity;
        remove_created(&target, &identity, b"created-value").unwrap();
        assert!(!target.exists());
        state::atomic_create(&target, b"owned-value").unwrap();
        let source = parent.join("before");
        state::atomic_create(&source, b"before-value").unwrap();
        let identity = state::open(&target).unwrap().identity;
        let mut published = false;
        restore_existing(
            &source,
            &target,
            &identity,
            b"owned-value",
            &mut published,
            None,
        )
        .unwrap();
        assert!(published);
        assert_eq!(state::read(&target, 4096).unwrap(), b"before-value");
        assert_eq!(fs::read_dir(&parent).unwrap().count(), 1);
    }

    #[cfg(windows)]
    #[test]
    fn review_move_paths_support_unc_and_preserve_verbatim_paths() {
        for (input, expected) in [
            (
                r"\\server\share\folder\file",
                r"\\?\UNC\server\share\folder\file",
            ),
            (r"\\?\C:\folder\file", r"\\?\C:\folder\file"),
            (r"C:\folder\..\file", r"\\?\C:\file"),
        ] {
            let path = move_path(Path::new(input)).unwrap();
            assert_eq!(
                String::from_utf16(&path[..path.len() - 1]).unwrap(),
                expected
            );
        }
    }

    #[cfg(windows)]
    #[test]
    #[allow(unsafe_code)] // Alter this test thread's last error after opening failed.
    fn review_open_error_survives_an_intervening_os_call() {
        let home = Home::new();
        let error = verify_after_open_error(&home.0.join("absent"), &[], b"", || {
            // SAFETY: SetLastError only changes the calling thread's error slot.
            unsafe { windows_sys::Win32::Foundation::SetLastError(5) };
        })
        .unwrap_err();
        assert!(error.contains("NotFound"), "{error}");
        assert!(!error.contains("PermissionDenied"), "{error}");
    }

    #[test]
    fn review_unsupported_exclusive_rename_gives_actionable_recovery() {
        for kind in [
            std::io::ErrorKind::InvalidInput,
            std::io::ErrorKind::Unsupported,
        ] {
            let error = move_error(Path::new("retained-config"), &std::io::Error::from(kind));
            assert!(
                error.contains("this filesystem does not support exclusive rename"),
                "{error}"
            );
            assert!(error.contains("retained-config"), "{error}");
            assert!(!error.contains("then retry"), "{error}");
        }
    }

    #[test]
    fn review_restore_refusal_names_the_actual_backup() {
        let home = Home::new();
        let backup = home.0.join("config.bak-pseudolife-fixture");
        state::atomic_create(&backup, b"before").unwrap();
        let error = restore_existing(
            &home.0.join("source"),
            &home.0.join("absent"),
            &[],
            b"",
            &mut false,
            Some(&backup),
        )
        .unwrap_err();
        assert!(error.contains(&format!("{backup:?}")), "{error}");
        assert_eq!(state::read(&backup, 4096).unwrap(), b"before");
    }

    #[test]
    fn review_restoration_sync_precedes_failed_cleanup() {
        let calls = std::cell::RefCell::new(Vec::new());
        let error = cleanup_restored(
            Path::new("restored"),
            Path::new("retained"),
            || {
                calls.borrow_mut().push("sync");
                Ok(())
            },
            || {
                calls.borrow_mut().push("remove");
                Err(std::io::Error::from(std::io::ErrorKind::PermissionDenied))
            },
        )
        .unwrap_err();
        assert_eq!(*calls.borrow(), ["sync", "remove"]);
        assert!(
            error.contains("was restored") && error.contains("retained"),
            "{error}"
        );
    }

    #[test]
    fn review_restoration_race_preserves_the_competing_replacement() {
        let home = Home::new();
        let target = home.0.join("credential");
        let source = home.0.join("restoration");
        state::atomic_create(&target, b"owned-value").unwrap();
        state::atomic_create(&source, b"before-value").unwrap();
        let identity = state::open(&target).unwrap().identity;
        let mut contender = None;
        let mut published = false;
        let result = restore_after_check(
            &source,
            &target,
            &identity,
            b"owned-value",
            &mut published,
            || {
                state::atomic_write(&target, b"competing-value", None).unwrap();
                contender = Some(state::open(&target).unwrap().identity);
            },
            || {},
        );
        assert!(result.is_err(), "restoration must refuse the replacement");
        assert!(!published);
        assert_eq!(Some(state::open(&target).unwrap().identity), contender);
        assert_eq!(state::read(&target, 4096).unwrap(), b"competing-value");
        assert_eq!(state::read(&source, 4096).unwrap(), b"before-value");
    }

    #[test]
    fn restoration_removes_only_the_owned_previous_file() {
        let home = Home::new();
        let target = home.0.join("credential");
        let source = home.0.join("restoration");
        state::atomic_create(&target, b"owned-value").unwrap();
        state::atomic_create(&source, b"before-value").unwrap();
        let identity = state::open(&target).unwrap().identity;
        let mut published = false;
        restore_existing(
            &source,
            &target,
            &identity,
            b"owned-value",
            &mut published,
            None,
        )
        .unwrap();
        assert!(published);
        assert_eq!(state::read(&target, 4096).unwrap(), b"before-value");
        assert_eq!(fs::read_dir(&home.0).unwrap().count(), 1);
    }

    #[test]
    fn restoration_does_not_replace_a_creator_after_the_move() {
        let home = Home::new();
        let target = home.0.join("credential");
        let source = home.0.join("restoration");
        state::atomic_create(&target, b"owned-value").unwrap();
        state::atomic_create(&source, b"before-value").unwrap();
        let identity = state::open(&target).unwrap().identity;
        let mut published = false;
        let error = restore_after_check(
            &source,
            &target,
            &identity,
            b"owned-value",
            &mut published,
            || {},
            || {
                state::atomic_create(&target, b"new-contender").unwrap();
            },
        )
        .unwrap_err();
        assert!(!published);
        assert!(error.contains("retained file is at"));
        assert_eq!(state::read(&target, 4096).unwrap(), b"new-contender");
        assert_eq!(state::read(&source, 4096).unwrap(), b"before-value");
        let retained = fs::read_dir(&home.0)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .find(|entry| entry != &target && entry != &source)
            .unwrap();
        assert_eq!(state::open(&retained).unwrap().identity, identity);
        assert_eq!(state::read(&retained, 4096).unwrap(), b"owned-value");
    }

    #[cfg(unix)]
    #[test]
    fn review_exclusive_restore_failure_never_leaves_an_extra_link() {
        use std::os::unix::fs::PermissionsExt;
        if rustix::process::getuid().is_root() {
            eprintln!("read-only directory refusal requires an unprivileged test process");
            return;
        }
        let home = Home::new();
        let parent = home.0.join("read-only");
        fs::create_dir(&parent).unwrap();
        let source = parent.join("retained");
        let target = home.0.join("original");
        state::atomic_create(&source, b"fixture-value").unwrap();
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o500)).unwrap();
        let result = restore_exclusive(&source, &target);
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(result.is_err());
        assert!(
            !target.exists(),
            "failed restore must not leave a second link"
        );
        assert!(state::open(&source).is_ok());
        restore_exclusive(&source, &target).unwrap();
        assert!(state::open(&target).is_ok());
    }

    #[cfg(windows)]
    #[test]
    fn review_sharing_violation_keeps_the_operating_system_cause() {
        let home = Home::new();
        let path = home.0.join("credential");
        state::atomic_create(&path, b"fixture-value").unwrap();
        let opened = state::open(&path).unwrap();
        let error = remove_created(&path, &opened.identity, b"fixture-value").unwrap_err();
        assert!(
            error.contains("os error 32"),
            "sharing violation cause must survive: {error}"
        );
        drop(opened);
        assert!(state::open(&path).is_ok());
    }

    #[test]
    fn review_move_failure_reports_the_filesystem_cause() {
        let home = Home::new();
        let path = home.0.join("absent");
        let error = remove_created(&path, &[], b"fixture-value").unwrap_err();
        assert!(
            error.contains("NotFound"),
            "filesystem error kind must survive: {error}"
        );
    }

    #[test]
    fn review_open_failure_is_not_reported_as_an_identity_conflict() {
        let home = Home::new();
        let path = home.0.join("credential");
        state::atomic_create(&path, b"fixture-value").unwrap();
        let identity = state::open(&path).unwrap().identity;
        let error = remove_after_check(
            &path,
            &identity,
            b"fixture-value",
            || {},
            || {
                let moved = fs::read_dir(&home.0)
                    .unwrap()
                    .next()
                    .unwrap()
                    .unwrap()
                    .path();
                fs::remove_file(moved).unwrap();
            },
        )
        .unwrap_err();
        assert!(
            error.contains("could not open"),
            "open failure must remain distinct: {error}"
        );
        assert!(!error.contains("no longer matches"));
        assert!(error.contains("NotFound"), "{error}");
    }

    #[test]
    fn replacement_between_validation_and_removal_survives() {
        let home = Home::new();
        let path = home.0.join("credential");
        state::atomic_create(&path, b"fixture-value").unwrap();
        let owned = state::open(&path).unwrap().identity;
        let mut contender = None;
        let result = remove_after_check(
            &path,
            &owned,
            b"fixture-value",
            || {
                state::atomic_write(&path, b"fixture-value", None).unwrap();
                contender = Some(state::open(&path).unwrap().identity);
            },
            || {},
        );
        assert!(result.is_err(), "a replacement must not be removed");
        assert_eq!(Some(state::open(&path).unwrap().identity), contender);
        assert_eq!(state::read(&path, 4096).unwrap(), b"fixture-value");
    }

    #[test]
    fn owned_file_is_removed_without_leaving_a_private_tombstone() {
        let home = Home::new();
        let path = home.0.join("credential");
        state::atomic_create(&path, b"fixture-value").unwrap();
        let owned = state::open(&path).unwrap().identity;
        remove_created(&path, &owned, b"fixture-value").unwrap();
        assert!(!path.exists());
        assert_eq!(fs::read_dir(&home.0).unwrap().count(), 0);
    }

    #[test]
    fn changed_bytes_are_returned_without_removal() {
        let home = Home::new();
        let path = home.0.join("credential");
        state::atomic_create(&path, b"fixture-value").unwrap();
        let owned = state::open(&path).unwrap().identity;
        let result = remove_after_check(
            &path,
            &owned,
            b"fixture-value",
            || {
                fs::write(&path, b"competing-value").unwrap();
            },
            || {},
        );
        assert!(result.is_err());
        assert_eq!(state::read(&path, 4096).unwrap(), b"competing-value");
        assert_eq!(fs::read_dir(&home.0).unwrap().count(), 1);
    }

    #[test]
    fn failed_restore_preserves_both_competing_files() {
        let home = Home::new();
        let path = home.0.join("credential");
        state::atomic_create(&path, b"fixture-value").unwrap();
        let owned = state::open(&path).unwrap().identity;
        let error = remove_after_check(
            &path,
            &owned,
            b"fixture-value",
            || {
                state::atomic_write(&path, b"moved-contender", None).unwrap();
            },
            || {
                state::atomic_create(&path, b"new-contender").unwrap();
            },
        )
        .unwrap_err();
        assert!(error.contains("retained file is at") && error.contains("Review both paths"));
        assert_eq!(state::read(&path, 4096).unwrap(), b"new-contender");
        let retained = fs::read_dir(&home.0)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .find(|entry| entry != &path)
            .unwrap();
        assert_eq!(state::read(&retained, 4096).unwrap(), b"moved-contender");
    }
}
