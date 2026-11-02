//! Private installer writes, preflighted before any configuration mutation.
use super::codex::SetupError;
use crate::board::state;
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};

const LIMIT: usize = 4 * 1024 * 1024;

/// Stable private advisory lock shared by native setup transactions. Refuse a
/// concurrent installer rather than waiting while its user may be responding.
pub(crate) struct TransactionLock(std::fs::File);
impl TransactionLock {
    pub fn acquire(config: &Path) -> Result<Self, SetupError> {
        let parent = config
            .parent()
            .ok_or_else(|| file_error(config, "parent"))?;
        let path = parent.join(".pseudolife-setup.lock");
        crate::credentials::reject_ancestor_redirects(&path)
            .map_err(|_| file_error(&path, "path"))?;
        fs::create_dir_all(parent).map_err(|_| file_error(&path, "parent"))?;
        // Windows create() hardens the ACL after opening. Publish an already
        // private file so a racing installer never sees the inherited ACL.
        // POSIX create_new with mode 0600 is private and exclusive at creation.
        #[cfg(windows)]
        let created = create_private_lock(&path);
        #[cfg(unix)]
        let created = state::create(&path);
        let opened = created
            .or_else(|_| state::open_with_write(&path, true))
            .map_err(|cause| file_error(&path, cause))?;
        opened.file.try_lock().map_err(|_| {
            SetupError("Another Codex setup is in progress; retry after it finishes.")
        })?;
        if state::open(&path)
            .map_err(|cause| file_error(&path, cause))?
            .identity
            != opened.identity
        {
            return Err(file_error(&path, "changed"));
        }
        Ok(Self(opened.file))
    }
}

#[cfg(windows)]
fn create_private_lock(path: &Path) -> Result<state::PrivateFile, &'static str> {
    let parent =
        fs::canonicalize(path.parent().ok_or("invalid_state")?).map_err(|_| "state_unavailable")?;
    let temp = parent.join(format!(
        ".pseudolife-lock-{}",
        uuid::Uuid::new_v4().simple()
    ));
    let target = parent.join(path.file_name().ok_or("invalid_state")?);
    let result = (|| {
        // state::create finishes ACL hardening before the stable name appears.
        let opened = state::create(&temp)?;
        drop(opened);
        publish_exclusive(&temp, &target, &mut false)?;
        state::open_with_write(path, true)
    })();
    let _ = fs::remove_file(&temp);
    result
}

#[cfg(windows)]
#[allow(unsafe_code)] // One bounded Win32 call publishes an already-private file exclusively.
fn publish_exclusive(
    source: &Path,
    target: &Path,
    published: &mut bool,
) -> Result<(), &'static str> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::MoveFileW;
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: both buffers are NUL-terminated paths that remain live for the
    // call. MoveFileW refuses an existing destination, preserving its inode.
    if unsafe { MoveFileW(source.as_ptr(), target.as_ptr()) } == 0 {
        return Err("state_unavailable");
    }
    *published = true;
    Ok(())
}

#[cfg(unix)]
fn publish_exclusive(
    source: &Path,
    target: &Path,
    published: &mut bool,
) -> Result<(), &'static str> {
    // link is exclusive at the destination even if another creator races us.
    fs::hard_link(source, target).map_err(|_| "reservation_changed")?;
    *published = true;
    // The temporary second link must be removed before strict state validation
    // (which requires nlink == 1). The receipt uses the staged inode identity,
    // never a target reopen during this two-link interval.
    fs::remove_file(source).map_err(|_| "state_unavailable")
}

/// A receipt for the inode we created and filled before publishing its name.
/// Native transaction locks serialize cooperating writers; the final pathname
/// check and rename are not an atomic CAS against uncooperative file editors.
struct StagedPrivate {
    path: PathBuf,
    identity: Vec<u128>,
    published: bool,
}
impl StagedPrivate {
    fn new(target: &Path, bytes: &[u8]) -> Result<Self, SetupError> {
        let parent = target
            .parent()
            .ok_or_else(|| file_error(target, "parent"))?;
        let path = parent.join(format!(
            ".pseudolife-publish-{}",
            uuid::Uuid::new_v4().simple()
        ));
        let staged = (|| {
            let mut opened = state::create(&path).map_err(|cause| file_error(target, cause))?;
            opened
                .file
                .write_all(bytes)
                .and_then(|()| opened.file.sync_all())
                .map_err(|_| file_error(target, "write"))?;
            // This is the actual staged file's identity, captured before its
            // handle closes for Windows rename's no-delete-sharing contract.
            Ok(Self {
                path: path.clone(),
                identity: opened.identity,
                published: false,
            })
        })();
        if staged.is_err() {
            let _ = fs::remove_file(&path);
        }
        staged
    }

    fn publish(&mut self, target: &Path, expected: Option<&[u128]>) -> Result<(), SetupError> {
        if let Some(expected) = expected {
            if state::open(target)
                .map_err(|cause| file_error(target, cause))?
                .identity
                != expected
            {
                return Err(file_error(target, "changed"));
            }
            fs::rename(&self.path, target).map_err(|_| file_error(target, "write"))?;
            self.published = true;
            Ok(())
        } else {
            publish_exclusive(&self.path, target, &mut self.published)
                .map_err(|cause| file_error(target, cause))
        }
    }
}
impl Drop for StagedPrivate {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

fn sync_parent(path: &Path) -> Result<(), SetupError> {
    #[cfg(unix)]
    fs::File::open(path.parent().ok_or_else(|| file_error(path, "parent"))?)
        .and_then(|file| file.sync_all())
        .map_err(|_| file_error(path, "write"))?;
    let _ = path;
    Ok(())
}
impl Drop for TransactionLock {
    fn drop(&mut self) {
        let _ = self.0.unlock();
    }
}

pub(crate) fn backup(path: &Path, bytes: &[u8]) -> Result<PathBuf, SetupError> {
    let name = path
        .file_name()
        .ok_or(SetupError("Invalid setup file path."))?;
    let mut name = name.to_os_string();
    name.push(format!(".bak-pseudolife-{}", uuid::Uuid::new_v4().simple()));
    let target = path.with_file_name(name);
    let mut opened = state::create(&target).map_err(|cause| file_error(path, cause))?;
    let result = opened
        .file
        .write_all(bytes)
        .and_then(|()| opened.file.sync_all());
    drop(opened);
    if result.is_err() {
        let _ = fs::remove_file(&target);
        return Err(file_error(path, "write"));
    }
    Ok(target)
}

fn file_error(path: &Path, reason: &str) -> SetupError {
    let detail = match reason {
        "invalid_state" => "is not a private regular file owned by this account",
        "path" => "has a redirected parent path",
        "changed" | "reservation_changed" => "changed during setup",
        "parent" => "has an unavailable parent directory",
        "write" => "could not be written durably",
        "read" => "could not be read",
        "remove" => "could not be removed during rollback",
        "metadata" => "is not an accessible regular file within the size limit",
        _ => "could not be opened",
    };
    SetupError::detail(format!(
        "Setup file {path:?} {detail}; repair that file and retry."
    ))
}

/// Contents are intentionally absent from Debug and all reports.
pub(crate) struct PrivateEdit {
    path: PathBuf,
    before: Option<Vec<u8>>,
    after: Vec<u8>,
    identity: Option<Vec<u128>>,
    published_identity: Option<Vec<u128>>,
    pub backup: Option<PathBuf>,
    committed: bool,
}

impl PrivateEdit {
    pub fn prepare(path: &Path, after: Vec<u8>) -> Result<Self, SetupError> {
        crate::credentials::reject_ancestor_redirects(path)
            .map_err(|_| file_error(path, "path"))?;
        let (before, identity) = match fs::symlink_metadata(path) {
            Ok(_) => {
                let identity = state::open(path)
                    .map_err(|cause| file_error(path, cause))?
                    .identity;
                let bytes = state::read(path, LIMIT).map_err(|cause| file_error(path, cause))?;
                (Some(bytes), Some(identity))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (None, None),
            Err(_) => return Err(file_error(path, "metadata")),
        };
        Ok(Self {
            path: path.to_owned(),
            before,
            after,
            identity,
            published_identity: None,
            backup: None,
            committed: false,
        })
    }

    pub fn changed(&self) -> bool {
        self.before.as_deref() != Some(self.after.as_slice())
    }
    pub fn created(&self) -> bool {
        self.committed && self.before.is_none()
    }
    pub fn committed(&self) -> bool {
        self.committed
    }
    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn still_before(&self) -> bool {
        match &self.before {
            None => fs::symlink_metadata(&self.path)
                .is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound),
            Some(before) => {
                state::read(&self.path, LIMIT).is_ok_and(|bytes| bytes == *before)
                    && state::open(&self.path)
                        .is_ok_and(|opened| Some(&opened.identity) == self.identity.as_ref())
            }
        }
    }

    pub fn commit(&mut self) -> Result<(), SetupError> {
        if !self.changed() {
            return Ok(());
        }
        let parent = self
            .path
            .parent()
            .ok_or_else(|| file_error(&self.path, "parent"))?;
        crate::credentials::reject_ancestor_redirects(&self.path)
            .map_err(|_| file_error(&self.path, "path"))?;
        fs::create_dir_all(parent).map_err(|_| file_error(&self.path, "parent"))?;
        if let Some(before) = &self.before {
            if state::read(&self.path, LIMIT).map_err(|cause| file_error(&self.path, cause))?
                != *before
            {
                return Err(file_error(&self.path, "changed"));
            }
            self.backup = Some(backup(&self.path, before)?);
        }
        let mut staged = StagedPrivate::new(&self.path, &self.after)?;
        let result = staged.publish(&self.path, self.identity.as_deref());
        if staged.published {
            // Retain ownership even when unlinking the staging alias or the
            // directory durability check fails after successful publication.
            self.published_identity = Some(staged.identity.clone());
            self.committed = true;
        }
        result?;
        sync_parent(&self.path)
    }

    pub fn rollback(&mut self) -> Result<(), SetupError> {
        if !self.committed {
            return Ok(());
        }
        let opened = state::open(&self.path).map_err(|cause| file_error(&self.path, cause))?;
        if Some(&opened.identity) != self.published_identity.as_ref() {
            return Err(SetupError(
                "Setup rollback refused a file replaced by another writer; restore the saved backup after reviewing it.",
            ));
        }
        let mut bytes = Vec::new();
        (&opened.file)
            .take(LIMIT as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| file_error(&self.path, "read"))?;
        if bytes != self.after {
            return Err(SetupError(
                "Setup rollback refused a file changed by another writer; restore the saved backup after reviewing it.",
            ));
        }
        let identity = opened.identity;
        drop(opened.file);
        if let Some(bytes) = &self.before {
            let mut staged = StagedPrivate::new(&self.path, bytes)?;
            let result = staged.publish(&self.path, Some(&identity));
            if staged.published {
                // This restoration is another inode we own, not the original
                // inode from prepare. A legitimate rerun may replace it again.
                self.identity = Some(staged.identity.clone());
                self.published_identity = None;
                self.committed = false;
            }
            result?;
        } else {
            fs::remove_file(&self.path).map_err(|_| file_error(&self.path, "remove"))?;
            self.published_identity = None;
            self.committed = false;
        }
        sync_parent(&self.path)
    }
}

pub(crate) fn read_config(path: &Path) -> Result<Option<Vec<u8>>, SetupError> {
    crate::credentials::reject_ancestor_redirects(path).map_err(|_| file_error(path, "path"))?;
    match fs::symlink_metadata(path) {
        Ok(info)
            if info.is_file() && !info.file_type().is_symlink() && info.len() <= LIMIT as u64 =>
        {
            fs::read(path)
                .map(Some)
                .map_err(|_| file_error(path, "read"))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        _ => Err(file_error(path, "metadata")),
    }
}
