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
) -> Result<(), String> {
    restore_after_check(source, target, identity, bytes, published, || {}, || {})
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
    // The public path is restored now. An unlink failure retains the old owned
    // file at its private name; it must not turn this into an uncommitted write.
    fs::remove_file(&moved).map_err(|error| format!(
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
    restore_exclusive(path, &moved).map_err(|error| format!(
        "Setup file {path:?} could not be moved for rollback ({:?}: {error}); resolve the reported filesystem error, then retry recovery.", error.kind()
    ))?;
    Ok(moved)
}

fn verify_file(path: &Path, identity: &[u128], bytes: &[u8]) -> Result<(), String> {
    let opened = crate::board::state::open(path).map_err(|cause| {
        // state::open intentionally exposes only a small error vocabulary.
        // On its OS-I/O error branch, capture the OS error immediately; on a
        // security-validation refusal no OS error is implied.
        let detail = if cause == "state_unavailable" {
            let error = std::io::Error::last_os_error();
            format!("{cause}; {:?}: {error}", error.kind())
        } else {
            cause.to_owned()
        };
        format!("could not open or validate private file {path:?}: {detail}")
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
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::MoveFileW;
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: the NUL-terminated path buffers live through this call.
    if unsafe { MoveFileW(source.as_ptr(), target.as_ptr()) } == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
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
        restore_existing(&source, &target, &identity, b"owned-value", &mut published).unwrap();
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
