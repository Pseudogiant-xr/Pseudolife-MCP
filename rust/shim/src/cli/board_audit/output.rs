//! Private same-directory staging; publication never replaces another file.
use std::{
    fs::{self, File},
    io,
    path::{Path, PathBuf},
};

pub(super) struct Staged {
    path: PathBuf,
    destination: PathBuf,
    file: Option<File>,
    identity: (u64, u64),
}

impl Staged {
    pub(super) fn create(destination: &Path) -> io::Result<Self> {
        // Include dangling links and directories in the no-replacement rule.
        match fs::symlink_metadata(destination) {
            Ok(_) => return Err(io::ErrorKind::AlreadyExists.into()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e),
        }
        let parent = destination
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        let path = parent.join(format!(".board-audit-{}.tmp", uuid::Uuid::new_v4()));
        #[cfg(windows)]
        let file = crate::credentials::windows_security::create_owner_only(&path)?;
        #[cfg(not(windows))]
        let file = {
            let mut options = fs::OpenOptions::new();
            options.read(true).write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            options.open(&path)?
        };
        let identity = identity(&file)?;
        let staged = Self {
            path,
            destination: destination.to_owned(),
            file: Some(file),
            identity,
        };
        #[cfg(windows)]
        crate::credentials::windows_security::make_private(staged.file())
            .map_err(|_| io::Error::from(io::ErrorKind::PermissionDenied))?;
        Ok(staged)
    }

    pub(super) fn file(&self) -> &File {
        self.file
            .as_ref()
            .expect("staging file is open until publication")
    }

    pub(super) fn publish(mut self) -> io::Result<()> {
        self.file().sync_all()?;
        if !self.owns_path() {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        // Windows refuses a pathname rename while our non-delete-sharing
        // private handle is open. Nothing is written after this close.
        drop(self.file.take());
        rename_exclusive(&self.path, &self.destination)
    }

    fn owns_path(&self) -> bool {
        fs::symlink_metadata(&self.path).is_ok_and(|m| m.is_file())
            && File::open(&self.path)
                .and_then(|file| identity(&file))
                .is_ok_and(|id| id == self.identity)
    }
}

impl Drop for Staged {
    fn drop(&mut self) {
        drop(self.file.take());
        if self.owns_path() {
            let _ = fs::remove_file(&self.path);
        }
    }
}

#[cfg(unix)]
fn identity(file: &File) -> io::Result<(u64, u64)> {
    use std::os::unix::fs::MetadataExt;
    let metadata = file.metadata()?;
    Ok((metadata.dev(), metadata.ino()))
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn identity(file: &File) -> io::Result<(u64, u64)> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::{
        Foundation::HANDLE,
        Storage::FileSystem::{
            BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_REPARSE_POINT, GetFileInformationByHandle,
        },
    };
    // SAFETY: the owned file handle and initialized output remain valid.
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as HANDLE, &mut info) } == 0 {
        return Err(io::Error::last_os_error());
    }
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok((
        u64::from(info.dwVolumeSerialNumber),
        u64::from(info.nFileIndexHigh) << 32 | u64::from(info.nFileIndexLow),
    ))
}

#[cfg(not(any(unix, windows)))]
fn identity(_: &File) -> io::Result<(u64, u64)> {
    Err(io::ErrorKind::Unsupported.into())
}

#[cfg(any(
    target_os = "linux",
    target_vendor = "apple",
    target_os = "android",
    target_os = "redox"
))]
fn rename_exclusive(source: &Path, destination: &Path) -> io::Result<()> {
    rustix::fs::renameat_with(
        rustix::fs::CWD,
        source,
        rustix::fs::CWD,
        destination,
        rustix::fs::RenameFlags::NOREPLACE,
    )
    .map_err(Into::into)
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn rename_exclusive(source: &Path, destination: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use std::path::{Component, Prefix};
    fn wide(path: &Path) -> io::Result<Vec<u16>> {
        let path = std::path::absolute(path)?;
        let units: Vec<_> = path.as_os_str().encode_wide().collect();
        if units.contains(&0) {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        let mut out: Vec<_> = match path.components().next() {
            Some(Component::Prefix(p)) => match p.kind() {
                Prefix::Verbatim(_) | Prefix::VerbatimDisk(_) | Prefix::VerbatimUNC(_, _) => units,
                Prefix::Disk(_) => r"\\?\".encode_utf16().chain(units).collect(),
                Prefix::UNC(_, _) => r"\\?\UNC\"
                    .encode_utf16()
                    .chain(units.into_iter().skip(2))
                    .collect(),
                _ => return Err(io::ErrorKind::InvalidInput.into()),
            },
            _ => return Err(io::ErrorKind::InvalidInput.into()),
        };
        out.push(0);
        Ok(out)
    }
    let source = wide(source)?;
    let destination = wide(destination)?;
    // SAFETY: both NUL-terminated buffers live throughout the call. MoveFileW
    // does not replace an existing destination, including a racing creator.
    if unsafe {
        windows_sys::Win32::Storage::FileSystem::MoveFileW(source.as_ptr(), destination.as_ptr())
    } == 0
    {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(any(
    windows,
    target_os = "linux",
    target_vendor = "apple",
    target_os = "android",
    target_os = "redox"
)))]
fn rename_exclusive(_: &Path, _: &Path) -> io::Result<()> {
    Err(io::ErrorKind::Unsupported.into())
}

pub(super) fn failure(operation: &str, error: &io::Error) -> u8 {
    super::stats::error(format!(
        "--out {operation} failed ({:?}, OS code {:?}); choose a writable destination on a filesystem supporting exclusive rename; no archive was published",
        error.kind(),
        error.raw_os_error()
    ))
}
