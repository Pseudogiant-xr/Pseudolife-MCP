use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io,
    path::{Path, PathBuf},
};

pub fn directory() -> PathBuf {
    let path = std::env::var_os("PSEUDOLIFE_LEASE_LOCK_DIR")
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            dirs::home_dir()
                .unwrap_or_default()
                .join(".pseudolife-mcp/locks")
        });
    path.components().collect()
}
pub fn file_name(name: &str) -> String {
    let safe: String = name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || "._-".contains(c) {
                c
            } else {
                '_'
            }
        })
        .collect();
    let suffix = if safe == name {
        String::new()
    } else {
        format!("-{:x}", Sha256::digest(name.as_bytes()))[..9].to_owned()
    };
    format!("lease-{safe}{suffix}.lock")
}

// Lock exactly Python's first byte on Windows. File::try_lock takes a larger
// range; that would also exclude independent locks beyond byte zero.
#[cfg(windows)]
#[allow(unsafe_code)]
fn try_lock(file: &File) -> io::Result<bool> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::{
        Foundation::{ERROR_IO_PENDING, ERROR_LOCK_VIOLATION},
        Storage::FileSystem::{LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY, LockFileEx},
        System::IO::OVERLAPPED,
    };
    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    if unsafe {
        LockFileEx(
            file.as_raw_handle() as _,
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            &mut overlapped,
        )
    } != 0
    {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if matches!(
        error.raw_os_error().map(|v| v as u32),
        Some(ERROR_LOCK_VIOLATION | ERROR_IO_PENDING)
    ) {
        Ok(false)
    } else {
        Err(error)
    }
}
#[cfg(windows)]
#[allow(unsafe_code)]
fn unlock(file: &File) {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::{Storage::FileSystem::UnlockFileEx, System::IO::OVERLAPPED};
    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    // Unlock exactly the byte-zero range acquired by try_lock.
    // Like Python, ignore unlock errors; closing the handle still releases it.
    let _ = unsafe { UnlockFileEx(file.as_raw_handle() as _, 0, 1, 0, &mut overlapped) };
}
#[cfg(unix)]
fn try_lock(file: &File) -> io::Result<bool> {
    match rustix::fs::flock(file, rustix::fs::FlockOperation::NonBlockingLockExclusive) {
        Ok(()) => Ok(true),
        Err(e) if e == rustix::io::Errno::WOULDBLOCK || e == rustix::io::Errno::ACCESS => Ok(false),
        Err(e) => Err(e.into()),
    }
}
pub struct Lock {
    pub path: PathBuf,
    file: Option<File>,
}

pub struct AcquisitionError {
    error: io::Error,
    path: Option<PathBuf>,
    mkdir: bool,
}
impl std::fmt::Display for AcquisitionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let raw = self.error.raw_os_error().unwrap_or(0);
        let text = self.error.to_string();
        let suffix = format!(" (os error {raw})");
        let message = text
            .strip_suffix(&suffix)
            .unwrap_or(&text)
            .trim_end_matches('.');
        let (label, message) = if cfg!(windows) && self.mkdir {
            (format!("WinError {raw}"), message.to_owned())
        } else if cfg!(windows) {
            match self.error.kind() {
                io::ErrorKind::PermissionDenied => ("Errno 13".into(), "Permission denied".into()),
                io::ErrorKind::NotFound => ("Errno 2".into(), "No such file or directory".into()),
                _ => (format!("WinError {raw}"), message.to_owned()),
            }
        } else {
            (format!("Errno {raw}"), message.to_owned())
        };
        write!(f, "[{label}] {message}")?;
        if let Some(path) = &self.path {
            write!(f, ": {}", super::repr(&path.to_string_lossy()))?;
        }
        Ok(())
    }
}
fn mkdir(path: &Path) -> Result<(), AcquisitionError> {
    let path = if path.as_os_str().is_empty() {
        Path::new(".")
    } else {
        path
    };
    match fs::create_dir(path) {
        Ok(()) => Ok(()),
        Err(e)
            if e.kind() == io::ErrorKind::NotFound && path.parent().is_some_and(|p| p != path) =>
        {
            mkdir(path.parent().expect("parent"))?;
            fs::create_dir(path)
                .or_else(|e| if path.is_dir() { Ok(()) } else { Err(e) })
                .map_err(|error| AcquisitionError {
                    error,
                    path: Some(path.into()),
                    mkdir: true,
                })
        }
        Err(_) if path.is_dir() => Ok(()),
        Err(error) => Err(AcquisitionError {
            error,
            path: Some(path.into()),
            mkdir: true,
        }),
    }
}
impl Lock {
    pub fn new(path: PathBuf) -> Self {
        Self { path, file: None }
    }
    pub fn acquire(&mut self) -> Result<bool, AcquisitionError> {
        if self.file.is_some() {
            return Ok(true);
        }
        if let Some(parent) = self.path.parent() {
            mkdir(parent)?;
        }
        let file = OpenOptions::new()
            .read(true)
            .append(true)
            .create(true)
            .open(&self.path)
            .map_err(|error| AcquisitionError {
                error,
                path: Some(self.path.clone()),
                mkdir: false,
            })?;
        if !try_lock(&file).map_err(|error| AcquisitionError {
            error,
            path: None,
            mkdir: false,
        })? {
            return Ok(false);
        }
        self.file = Some(file);
        Ok(true)
    }
    pub fn release(&mut self) {
        #[cfg(windows)]
        if let Some(file) = &self.file {
            unlock(file);
        }
        self.file = None;
    }
}
pub fn probe(path: &Path) -> io::Result<Option<bool>> {
    let file = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e),
    };
    Ok(Some(!try_lock(&file)?))
}

pub fn instance_id(directory: &Path) -> Option<String> {
    let path = directory.join("instance.id");
    for attempt in 0..2 {
        match fs::read(&path) {
            Ok(bytes) => {
                if !bytes.is_ascii() {
                    return None;
                }
                let text = std::str::from_utf8(&bytes)
                    .ok()?
                    .trim_matches(super::whitespace);
                return (text.len() == 12
                    && text
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
                .then(|| text.to_owned());
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound && attempt == 0 => (),
            _ => return None,
        }
        fs::create_dir_all(directory).ok()?;
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut f) => {
                use std::io::Write;
                let value = uuid::Uuid::new_v4().simple().to_string()[..12].to_owned();
                // Python's text-mode create translates the newline on Windows.
                f.write_all(&super::super::text_bytes(&format!("{value}\n")))
                    .ok()?;
                return Some(value);
            }
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => (),
            _ => return None,
        }
    }
    None
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

    #[test]
    fn unlock_releases_first_byte_while_original_handle_remains_open() {
        let path = std::env::temp_dir().join(format!("lease-unlock-{}.lock", uuid::Uuid::new_v4()));
        let mut original = Lock::new(path.clone());
        let mut contender = Lock::new(path.clone());
        assert!(matches!(original.acquire(), Ok(true)));
        assert!(matches!(contender.acquire(), Ok(false)));

        // Keep the actual owning handle open: dropping it cannot satisfy this test.
        let file = original.file.as_ref().expect("acquired handle");
        unlock(file);
        assert!(matches!(contender.acquire(), Ok(true)));
        assert!(file.metadata().is_ok());

        original.release();
        contender.release();
        fs::remove_file(path).unwrap();
    }
}

#[cfg(all(test, windows))]
#[path = "lock_release_tests.rs"]
mod release_tests;
