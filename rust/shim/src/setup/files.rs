//! Private installer writes, preflighted before any configuration mutation.
use super::codex::SetupError;
use crate::board::state;
use std::{
    fs,
    io::Write,
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
#[allow(unsafe_code)] // One bounded Win32 call publishes an already-private lock exclusively.
fn create_private_lock(path: &Path) -> Result<state::PrivateFile, &'static str> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::MoveFileW;
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
        let source: Vec<u16> = temp.as_os_str().encode_wide().chain(Some(0)).collect();
        let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
        // MoveFileW fails if the destination exists; unlike a check followed by
        // fs::rename, it cannot replace a competing installer's lock inode.
        // Both buffers are NUL-terminated paths and remain alive for the call.
        if unsafe { MoveFileW(source.as_ptr(), target.as_ptr()) } == 0 {
            return Err("state_unavailable");
        }
        state::open_with_write(path, true)
    })();
    let _ = fs::remove_file(&temp);
    result
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
            state::atomic_write(&self.path, &self.after, self.identity.as_deref())
                .map_err(|cause| file_error(&self.path, cause))?;
        } else {
            state::atomic_create(&self.path, &self.after)
                .map_err(|cause| file_error(&self.path, cause))?;
        }
        self.committed = true;
        Ok(())
    }

    pub fn rollback(&mut self) -> Result<(), SetupError> {
        if !self.committed {
            return Ok(());
        }
        let opened = state::open(&self.path).map_err(|cause| file_error(&self.path, cause))?;
        if state::read(&self.path, LIMIT).map_err(|cause| file_error(&self.path, cause))?
            != self.after
        {
            return Err(SetupError(
                "Setup rollback refused a file changed by another writer; restore the saved backup after reviewing it.",
            ));
        }
        let identity = opened.identity;
        drop(opened.file);
        if let Some(bytes) = &self.before {
            state::atomic_write(&self.path, bytes, Some(&identity))
                .map_err(|cause| file_error(&self.path, cause))?;
        } else {
            fs::remove_file(&self.path).map_err(|_| file_error(&self.path, "remove"))?;
        }
        self.committed = false;
        Ok(())
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
