//! Remove only the file published by the transaction being compensated.
use std::{fs, io::Read, path::Path};

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
    // Move away from the public name before rechecking ownership. A competing
    // replacement at that name must never be unlinked after a stale check.
    let tombstone = path.with_file_name(format!(
        ".pseudolife-rollback-{}",
        uuid::Uuid::new_v4().simple()
    ));
    before_remove();
    fs::rename(path, &tombstone).map_err(|_| {
        format!(
            "Setup file {path:?} could not be moved for rollback; review the current file and its directory permissions before retrying recovery."
        )
    })?;
    after_move();
    let owned = (|| {
        let opened = crate::board::state::open(&tombstone).ok()?;
        if opened.identity != identity {
            return None;
        }
        let mut current = Vec::new();
        (&opened.file)
            .take(bytes.len() as u64 + 1)
            .read_to_end(&mut current)
            .ok()?;
        (current == bytes).then_some(())
    })()
    .is_some();
    if owned && fs::remove_file(&tombstone).is_ok() {
        return Ok(());
    }
    let cause = if owned {
        "the filesystem refused removal"
    } else {
        "the moved file no longer matches this transaction's private file"
    };
    // Never overwrite a file created at the original name during verification.
    match restore_exclusive(&tombstone, path) {
        Ok(()) => Err(format!(
            "Setup rollback refused file {path:?}: {cause}; the file was returned to its original path. Review it before retrying recovery."
        )),
        Err(_) => Err(format!(
            "Setup rollback refused file {path:?}: {cause}; the retained file is at {tombstone:?} because its original path could not be restored exclusively. Review both paths and restore the retained file only after resolving the conflict, then retry recovery."
        )),
    }
}

#[cfg(unix)]
fn restore_exclusive(source: &Path, target: &Path) -> std::io::Result<()> {
    fs::hard_link(source, target)?;
    fs::remove_file(source)
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
