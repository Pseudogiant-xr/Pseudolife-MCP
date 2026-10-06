//! Private state reservations are bound to opened file identity.
use fs2::FileExt;
use serde_json::Value;
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

type Result<T> = std::result::Result<T, &'static str>;
pub struct PrivateFile {
    pub file: File,
    pub identity: Vec<u128>,
}
pub struct Reservation {
    path: PathBuf,
    identity: Vec<u128>,
    _lock: Option<File>,
    saved: bool,
}

fn identity(file: &File) -> Result<Vec<u128>> {
    let meta = file.metadata().map_err(|_| "state_unavailable")?;
    if !meta.is_file() {
        return Err("invalid_state");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.uid() != rustix::process::getuid().as_raw()
            || meta.mode() & 0o077 != 0
            || meta.nlink() != 1
        {
            return Err("invalid_state");
        }
        Ok(vec![meta.dev() as u128, meta.ino() as u128])
    }
    #[cfg(windows)]
    {
        crate::credentials::windows_security::validate(file)
            .map(|parts| parts[..2].to_vec())
            .map_err(|_| "invalid_state")
    }
}
fn options(create: bool, write: bool) -> OpenOptions {
    let mut options = OpenOptions::new();
    options.read(true).write(write).create_new(create);
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
        options.custom_flags(0x00200000).share_mode(0x1 | 0x2);
        if create {
            options.access_mode(0x0012019f | 0x000c0000);
        }
    }
    options
}
pub fn open(path: &Path) -> Result<PrivateFile> {
    open_with_write(path, false)
}
pub fn open_with_write(path: &Path, write: bool) -> Result<PrivateFile> {
    crate::credentials::reject_ancestor_redirects(path).map_err(|_| "invalid_state")?;
    let file = options(false, write)
        .open(path)
        .map_err(|_| "state_unavailable")?;
    let identity = identity(&file)?;
    Ok(PrivateFile { file, identity })
}
pub fn create(path: &Path) -> Result<PrivateFile> {
    crate::credentials::reject_ancestor_redirects(path).map_err(|_| "invalid_state")?;
    let file = options(true, true)
        .open(path)
        .map_err(|_| "state_unavailable")?;
    #[cfg(windows)]
    {
        // This helper is supplied by the credential module's existing security boundary.
        crate::credentials::windows_security::make_private(&file).map_err(|_| "invalid_state")?;
    }
    let identity = identity(&file)?;
    Ok(PrivateFile { file, identity })
}
pub fn read(path: &Path, limit: usize) -> Result<Vec<u8>> {
    let opened = open(path)?;
    let mut bytes = Vec::new();
    opened
        .file
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "state_unavailable")?;
    if bytes.len() > limit {
        return Err("invalid_state");
    }
    Ok(bytes)
}
fn same(path: &Path, expected: &[u128]) -> bool {
    open(path).is_ok_and(|file| file.identity == expected)
}
pub fn atomic_write(path: &Path, bytes: &[u8], expected: Option<&[u128]>) -> Result<()> {
    atomic_replace(path, bytes, expected, false)
}
pub fn atomic_create(path: &Path, bytes: &[u8]) -> Result<()> {
    atomic_replace(path, bytes, None, true)
}
pub fn exists(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}
fn atomic_replace(
    path: &Path,
    bytes: &[u8],
    expected: Option<&[u128]>,
    exclusive: bool,
) -> Result<()> {
    if exists(path) {
        if exclusive {
            return Err("reservation_changed");
        }
        open(path)?;
    }
    let parent = path.parent().ok_or("invalid_state")?;
    let temp = parent.join(format!(".agent-state-{}", uuid::Uuid::new_v4().simple()));
    let result = (|| {
        let mut opened = create(&temp)?;
        opened
            .file
            .write_all(bytes)
            .and_then(|_| opened.file.sync_all())
            .map_err(|_| "state_unavailable")?;
        if expected.is_some_and(|identity| !same(path, identity)) {
            return Err("reservation_changed");
        }
        if exclusive && exists(path) {
            return Err("reservation_changed");
        }
        drop(opened);
        fs::rename(&temp, path).map_err(|_| "state_unavailable")?;
        #[cfg(unix)]
        {
            File::open(parent)
                .and_then(|file| file.sync_all())
                .map_err(|_| "state_unavailable")?;
        }
        Ok(())
    })();
    let _ = fs::remove_file(&temp);
    result
}
pub fn private_dir(path: &Path) -> Result<()> {
    crate::credentials::reject_ancestor_redirects(path).map_err(|_| "invalid_state")?;
    if !path.is_dir() {
        if let Some(parent) = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty() && !p.is_dir())
        {
            private_dir(parent)?;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            fs::DirBuilder::new()
                .mode(0o700)
                .create(path)
                .map_err(|_| "state_unavailable")?;
        }
        #[cfg(windows)]
        {
            fs::create_dir(path).map_err(|_| "state_unavailable")?;
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|_| "state_unavailable")?;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        let directory = OpenOptions::new()
            .access_mode(0x00120089 | 0x000c0000)
            .share_mode(0x1 | 0x2)
            .custom_flags(0x02000000 | 0x00200000)
            .open(path)
            .map_err(|_| "state_unavailable")?;
        crate::credentials::windows_security::make_private(&directory)
            .map_err(|_| "invalid_state")?;
    }
    Ok(())
}
impl Reservation {
    pub fn load_or_reserve(path: &Path, url: &str) -> Result<(Option<Value>, Option<Self>)> {
        if let Some(parent) = path.parent() {
            private_dir(parent)?;
        }
        if !exists(path) {
            let opened = create(path)?;
            return Ok((
                None,
                Some(Self {
                    path: path.to_owned(),
                    identity: opened.identity,
                    _lock: None,
                    saved: false,
                }),
            ));
        }
        let opened = open(path)?;
        if opened
            .file
            .metadata()
            .map_err(|_| "state_unavailable")?
            .len()
            != 0
        {
            let mut bytes = Vec::new();
            opened
                .file
                .take(16385)
                .read_to_end(&mut bytes)
                .map_err(|_| "state_unavailable")?;
            let value: Value = serde_json::from_slice(&bytes).map_err(|_| "invalid_state")?;
            if bytes.len() > 16384
                || value.get("bank_url").and_then(Value::as_str) != Some(url)
                || ["agent_id", "credential"].iter().any(|key| {
                    value
                        .get(key)
                        .and_then(Value::as_str)
                        .is_none_or(str::is_empty)
                })
            {
                return Err("invalid_state");
            }
            return Ok((Some(value), None));
        }
        let stale = |file: &File| {
            file.metadata()
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|time| SystemTime::now().duration_since(time).ok())
                .is_some_and(|age| age >= Duration::from_secs(60))
        };
        if !stale(&opened.file) {
            return Err("registration_in_progress");
        }
        let lock_path = path.with_file_name(format!(
            "{}.lock",
            path.file_name().ok_or("invalid_state")?.to_string_lossy()
        ));
        let lock = if lock_path.exists() {
            options(false, true)
                .open(&lock_path)
                .map_err(|_| "invalid_state")?
        } else {
            create(&lock_path)?.file
        };
        identity(&lock)?;
        lock.try_lock_exclusive()
            .map_err(|_| "registration_in_progress")?;
        let fresh = open(path)?;
        if fresh.identity != opened.identity
            || fresh
                .file
                .metadata()
                .map_err(|_| "state_unavailable")?
                .len()
                != 0
            || !stale(&fresh.file)
        {
            return Err("reservation_changed");
        }
        Ok((
            None,
            Some(Self {
                path: path.to_owned(),
                identity: opened.identity,
                _lock: Some(lock),
                saved: false,
            }),
        ))
    }
    pub fn save(&mut self, value: &Value) -> Result<()> {
        let bytes = serde_json::to_vec(value).map_err(|_| "invalid_state")?;
        atomic_write(&self.path, &bytes, Some(&self.identity))?;
        self.saved = true;
        Ok(())
    }
}
impl Drop for Reservation {
    fn drop(&mut self) {
        if !self.saved
            && let Ok(opened) = open(&self.path)
            && opened.identity == self.identity
            && opened.file.metadata().is_ok_and(|meta| meta.len() == 0)
        {
            drop(opened);
            let _ = fs::remove_file(&self.path);
        }
        if let Some(lock) = &self._lock {
            let _ = FileExt::unlock(lock);
        }
    }
}
