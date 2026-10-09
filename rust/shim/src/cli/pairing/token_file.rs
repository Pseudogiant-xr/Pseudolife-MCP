//! `client_config.write_token_file` for a token pair minted itself, and
//! `pair_cli._move_no_replace`.
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::Path;

/// Why the file was not written, as `pair_cli.redeem` reports it.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum Failure {
    /// `HelperError`: the target already exists.
    Exists,
    /// `CredentialError` (an ancestor redirect, protection or validation).
    Credential,
    /// An `OSError`, by the class name Python gives it.
    Os(&'static str),
}

/// `type(exc).__name__` of the `OSError` CPython raises for this error.
pub(super) fn os_class(error: &io::Error) -> &'static str {
    match error.kind() {
        io::ErrorKind::PermissionDenied => "PermissionError",
        io::ErrorKind::NotFound => "FileNotFoundError",
        io::ErrorKind::AlreadyExists => "FileExistsError",
        io::ErrorKind::NotADirectory => "NotADirectoryError",
        io::ErrorKind::IsADirectory => "IsADirectoryError",
        _ => "OSError",
    }
}

/// Whether `Path.exists() or Path.is_symlink()` holds; `Err` for an error
/// pathlib would raise rather than answer (only "missing" answers false).
pub(super) fn present(path: &Path) -> Result<bool, ()> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::NotFound | io::ErrorKind::NotADirectory
            ) =>
        {
            Ok(false)
        }
        Err(_) => Err(()),
    }
}

fn open_new(path: &Path) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // The CRT's sharing (read and write, never delete) plus the rights
        // the owner-only protection is applied through.
        options.share_mode(3).access_mode(0x0012_019f | 0x000c_0000);
    }
    options.open(path)
}

/// Owner-only before a byte is written: the protected DACL on Windows, the
/// mode bits elsewhere; then the oracle's `_validate_file` checks.
fn protect(file: &File) -> Result<(), Failure> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|error| Failure::Os(os_class(&error)))?;
        let info = file
            .metadata()
            .map_err(|error| Failure::Os(os_class(&error)))?;
        if !info.is_file()
            || info.nlink() != 1
            || info.uid() != rustix::process::geteuid().as_raw()
            || info.mode() & 0o077 != 0
        {
            return Err(Failure::Credential);
        }
    }
    #[cfg(windows)]
    {
        crate::credentials::windows_security::make_private(file)
            .map_err(|_| Failure::Credential)?;
        crate::credentials::windows_security::validate(file).map_err(|_| Failure::Credential)?;
    }
    Ok(())
}

/// Create `target` owner-only holding `token`; never replace a file. The
/// final name is created exclusively; a file this run created is removed
/// again on any later failure.
pub(super) fn write(target: &Path, token: &str) -> Result<(), Failure> {
    if present(target).map_err(|()| Failure::Os("OSError"))? {
        return Err(Failure::Exists);
    }
    crate::credentials::reject_ancestor_redirects(target).map_err(|_| Failure::Credential)?;
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|error| Failure::Os(os_class(&error)))?;
    }
    let mut file = match open_new(target) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            return Err(Failure::Exists);
        }
        Err(error) => return Err(Failure::Os(os_class(&error))),
    };
    let written = (|| {
        protect(&file)?;
        file.write_all(token.as_bytes())
            .and_then(|()| file.sync_all())
            .map_err(|error| Failure::Os(os_class(&error)))?;
        Ok(())
    })();
    drop(file);
    let checked = written.and_then(|()| {
        let provider =
            crate::credentials::CredentialProvider::new(None, Some(target.to_path_buf()))
                .map_err(|_| Failure::Credential)?;
        let snapshot = provider.snapshot().map_err(|_| Failure::Credential)?;
        if snapshot.token() == Some(token) {
            Ok(())
        } else {
            Err(Failure::Credential)
        }
    });
    if checked.is_err() {
        let _ = fs::remove_file(target);
    }
    checked
}

/// How `_move_no_replace` ended.
pub(super) enum Moved {
    Done,
    /// The new name was linked but the old one could not be removed.
    LinkedTwice,
    /// The new name exists or could not be created; nothing moved.
    Refused,
}

/// Move `source` to `target` without ever replacing `target`: a hard link
/// (which fails on an existing name), then the old name goes. POSIX is the
/// oracle's own way; on Windows the oracle renames (`MoveFileExW` without
/// replace), which this reaches without native calls; see the declared
/// divergence for volumes without hard links.
pub(super) fn move_no_replace(source: &Path, target: &Path) -> Moved {
    if fs::hard_link(source, target).is_err() {
        return Moved::Refused;
    }
    match fs::remove_file(source) {
        Ok(()) => Moved::Done,
        Err(_) => Moved::LinkedTwice,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_owner_only_and_never_replaces() {
        let root = std::env::temp_dir().join(format!("pair-token-{}", uuid::Uuid::new_v4()));
        let target = root.join("nested").join("a.token");
        write(&target, "tok-en_123").unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"tok-en_123");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&target).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
        assert_eq!(write(&target, "other"), Err(Failure::Exists));
        assert_eq!(fs::read(&target).unwrap(), b"tok-en_123");
        let moved = root.join("b.token");
        assert!(matches!(move_no_replace(&target, &moved), Moved::Done));
        assert!(!target.exists());
        write(&target, "again").unwrap();
        assert!(matches!(move_no_replace(&target, &moved), Moved::Refused));
        assert_eq!(fs::read(&moved).unwrap(), b"tok-en_123");
        assert_eq!(fs::read(&target).unwrap(), b"again");
        fs::remove_dir_all(&root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn a_symlinked_target_is_present_and_refused() {
        let root = std::env::temp_dir().join(format!("pair-link-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        let link = root.join("dangling.token");
        std::os::unix::fs::symlink(root.join("nowhere"), &link).unwrap();
        assert_eq!(present(&link), Ok(true));
        assert_eq!(write(&link, "x"), Err(Failure::Exists));
        assert!(!root.join("nowhere").exists());
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn os_errors_take_cpython_class_names() {
        let named = |kind| os_class(&io::Error::from(kind));
        assert_eq!(named(io::ErrorKind::PermissionDenied), "PermissionError");
        assert_eq!(named(io::ErrorKind::NotFound), "FileNotFoundError");
        assert_eq!(named(io::ErrorKind::Other), "OSError");
    }
}
