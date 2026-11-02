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
        let parent = config.parent().ok_or_else(|| file_error("parent"))?;
        let path = parent.join(".pseudolife-setup.lock");
        crate::credentials::reject_ancestor_redirects(&path).map_err(|_| file_error("path"))?;
        fs::create_dir_all(parent).map_err(|_| file_error("parent"))?;
        let opened = state::create(&path)
            .or_else(|_| state::open_with_write(&path, true))
            .map_err(file_error)?;
        opened.file.try_lock().map_err(|_| {
            SetupError("Another Codex setup is in progress; retry after it finishes.")
        })?;
        if state::open(&path).map_err(file_error)?.identity != opened.identity {
            return Err(file_error("changed"));
        }
        Ok(Self(opened.file))
    }
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
    let mut opened = state::create(&target).map_err(file_error)?;
    let result = opened
        .file
        .write_all(bytes)
        .and_then(|()| opened.file.sync_all());
    drop(opened);
    if result.is_err() {
        let _ = fs::remove_file(&target);
        return Err(file_error("write"));
    }
    Ok(target)
}

fn file_error(_: &str) -> SetupError {
    SetupError("Setup file is unsafe, changed, or unavailable; repair its permissions and retry.")
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
        crate::credentials::reject_ancestor_redirects(path).map_err(|_| file_error("path"))?;
        let (before, identity) = match fs::symlink_metadata(path) {
            Ok(_) => {
                let identity = state::open(path).map_err(file_error)?.identity;
                let bytes = state::read(path, LIMIT).map_err(file_error)?;
                (Some(bytes), Some(identity))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (None, None),
            Err(_) => return Err(file_error("metadata")),
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
        let parent = self.path.parent().ok_or_else(|| file_error("parent"))?;
        crate::credentials::reject_ancestor_redirects(&self.path)
            .map_err(|_| file_error("path"))?;
        fs::create_dir_all(parent).map_err(|_| file_error("parent"))?;
        if let Some(before) = &self.before {
            if state::read(&self.path, LIMIT).map_err(file_error)? != *before {
                return Err(file_error("changed"));
            }
            self.backup = Some(backup(&self.path, before)?);
            state::atomic_write(&self.path, &self.after, self.identity.as_deref())
                .map_err(file_error)?;
        } else {
            state::atomic_create(&self.path, &self.after).map_err(file_error)?;
        }
        self.committed = true;
        Ok(())
    }

    pub fn rollback(&mut self) -> Result<(), SetupError> {
        if !self.committed {
            return Ok(());
        }
        let opened = state::open(&self.path).map_err(file_error)?;
        if state::read(&self.path, LIMIT).map_err(file_error)? != self.after {
            return Err(SetupError(
                "Setup rollback refused a file changed by another writer; restore the saved backup after reviewing it.",
            ));
        }
        let identity = opened.identity;
        drop(opened.file);
        if let Some(bytes) = &self.before {
            state::atomic_write(&self.path, bytes, Some(&identity)).map_err(file_error)?;
        } else {
            fs::remove_file(&self.path).map_err(|_| file_error("remove"))?;
        }
        self.committed = false;
        Ok(())
    }
}

pub(crate) fn read_config(path: &Path) -> Result<Option<Vec<u8>>, SetupError> {
    crate::credentials::reject_ancestor_redirects(path).map_err(|_| file_error("path"))?;
    match fs::symlink_metadata(path) {
        Ok(info)
            if info.is_file() && !info.file_type().is_symlink() && info.len() <= LIMIT as u64 =>
        {
            fs::read(path).map(Some).map_err(|_| file_error("read"))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        _ => Err(file_error("metadata")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn setup_transaction_lock_refuses_a_competitor_and_releases_on_drop() {
        let home =
            std::env::temp_dir().join(format!("pseudolife-setup-lock-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let config = home.join("config.toml");
        let lock = TransactionLock::acquire(&config).unwrap();
        assert!(TransactionLock::acquire(&config).is_err());
        assert!(state::open(&home.join(".pseudolife-setup.lock")).is_ok());
        drop(lock);
        drop(TransactionLock::acquire(&config).unwrap());
        fs::remove_dir_all(home).unwrap();
    }
}
