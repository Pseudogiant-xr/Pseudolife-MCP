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
        let Ok(metadata) = fs::symlink_metadata(&self.path) else {
            return false;
        };
        if !metadata.is_file() {
            return false;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            // A restrictive umask may remove read permission. Identity and
            // cleanup must not require reopening a file we created for writing.
            (metadata.dev(), metadata.ino()) == self.identity
        }
        #[cfg(not(unix))]
        {
            File::open(&self.path)
                .and_then(|file| identity(&file))
                .is_ok_and(|id| id == self.identity)
        }
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
    let (cause, fix) = match error.kind() {
        io::ErrorKind::NotFound if operation == "staging" => (
            "parent directory does not exist",
            "create the parent directory before exporting",
        ),
        io::ErrorKind::NotFound => (
            "the staged archive or destination directory is missing",
            "keep the destination directory available and retry the export",
        ),
        io::ErrorKind::PermissionDenied
            if cfg!(windows) && matches!(error.raw_os_error(), Some(32 | 33)) =>
        {
            (
                "an archive path is locked by another process",
                "close the program using that path before retrying",
            )
        }
        io::ErrorKind::PermissionDenied => (
            "permission denied",
            "choose a destination writable by your user",
        ),
        io::ErrorKind::AlreadyExists => {
            ("destination already exists", "choose an unused --out path")
        }
        io::ErrorKind::NotADirectory => (
            "a parent path component is not a directory",
            "choose an --out path inside an existing directory",
        ),
        io::ErrorKind::StorageFull => (
            "the destination filesystem is full",
            "free space on that filesystem before retrying",
        ),
        io::ErrorKind::ReadOnlyFilesystem => (
            "the destination filesystem is read-only",
            "choose a destination on a writable filesystem",
        ),
        io::ErrorKind::InvalidInput => (
            "the destination path is unsupported",
            "choose a valid --out path for this filesystem",
        ),
        _ => (
            "the filesystem refused the archive operation",
            "choose a writable destination on a filesystem supporting exclusive rename",
        ),
    };
    let code = error
        .raw_os_error()
        .map(|code| format!(" (OS error {code})"))
        .unwrap_or_default();
    super::stats::error(format!(
        "--out {operation} failed: {cause}{code}; {fix}; no archive was published"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::Write, process::Command};

    const SECRET: &str = "synthetic-staging-content-must-not-escape";
    const CASE: &str = "PSEUDOLIFE_AUDIT_TEST_STAGING_CASE";

    struct Directory(PathBuf);
    impl Directory {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("audit-staging-{}", uuid::Uuid::new_v4()));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Directory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn staging_failures_have_distinct_causes() {
        if let Ok(case) = std::env::var(CASE) {
            let directory = Directory::new();
            #[cfg(windows)]
            if case == "privacy" {
                let path = directory.0.join("read-only-handle");
                fs::write(&path, SECRET).unwrap();
                let file = File::open(path).unwrap();
                // This helper requires WRITE_OWNER and WRITE_DAC;
                // a read-only handle provides neither.
                let error = crate::credentials::windows_security::make_private(&file).unwrap_err();
                let error = io::Error::new(io::ErrorKind::PermissionDenied, error);
                assert_eq!(failure("staging", &error), 2);
                return;
            }
            assert!(matches!(case.as_str(), "removed" | "replaced"));
            let destination = directory.0.join("archive.jsonl");
            let staged = Staged::create(&destination).unwrap();
            staged.file().write_all(SECRET.as_bytes()).unwrap();
            #[cfg(windows)]
            let mut staged = staged;
            #[cfg(windows)]
            {
                use std::os::windows::fs::OpenOptionsExt;
                use windows_sys::Win32::Storage::FileSystem::{
                    FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
                };
                // The command excludes delete sharing. This constructed
                // Staged value lets the test remove its path before publish.
                drop(staged.file.take());
                staged.file = Some(
                    fs::OpenOptions::new()
                        .read(true)
                        .write(true)
                        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
                        .open(&staged.path)
                        .unwrap(),
                );
            }
            let path = staged.path.clone();
            if case == "removed" {
                fs::remove_file(&path).unwrap();
            } else {
                fs::rename(&path, directory.0.join("moved-staging-file")).unwrap();
                fs::write(&path, b"replacement must survive").unwrap();
            }
            let error = staged.publish().unwrap_err();
            assert_eq!(failure("publication", &error), 2);
            assert!(!destination.exists());
            if case == "replaced" {
                assert_eq!(fs::read(path).unwrap(), b"replacement must survive");
            }
            return;
        }
        let cases = [
            (
                "removed",
                "staged archive identity",
                "private destination directory",
            ),
            (
                "replaced",
                "staged archive identity",
                "private destination directory",
            ),
            #[cfg(windows)]
            (
                "privacy",
                "owner-only staging permissions",
                "owner-only permissions",
            ),
        ];
        let mut failures = Vec::new();
        for (case, cause, fix) in cases {
            let output = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "cli::board_audit::output::tests::staging_failures_have_distinct_causes",
                    "--nocapture",
                ])
                .env(CASE, case)
                .output()
                .unwrap();
            assert!(output.status.success(), "{case}: {:?}", output.stderr);
            let stderr = String::from_utf8(output.stderr).unwrap();
            if !stderr.contains(cause) || !stderr.contains(fix) {
                failures.push(format!("{case}: {stderr}"));
            }
            assert!(!stderr.contains(SECRET));
            assert!(!String::from_utf8_lossy(&output.stdout).contains(SECRET));
        }
        assert!(failures.is_empty(), "{}", failures.join("\n"));
        let directory = Directory::new();
        let destination = directory.0.join("complete.jsonl");
        let staged = Staged::create(&destination).unwrap();
        staged.file().write_all(b"complete archive\n").unwrap();
        staged.publish().unwrap();
        assert_eq!(fs::read(destination).unwrap(), b"complete archive\n");
    }

    #[cfg(windows)]
    #[test]
    fn reparse_identity_has_its_own_cause() {
        use std::os::windows::{fs::OpenOptionsExt, process::CommandExt};
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
        };
        use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
        let directory = Directory::new();
        let ordinary = directory.0.join("ordinary");
        let redirect = directory.0.join("redirect");
        let target_directory = directory.0.join("target");
        fs::write(&ordinary, SECRET).unwrap();
        fs::create_dir(&target_directory).unwrap();
        // Directory junctions require no symlink privilege on the test VM.
        let command = PathBuf::from(std::env::var_os("SystemRoot").unwrap())
            .join("System32")
            .join("cmd.exe");
        let output = Command::new(command)
            .args(["/d", "/c", "mklink", "/J"])
            .arg(&redirect)
            .arg(&target_directory)
            .creation_flags(CREATE_NO_WINDOW)
            .output()
            .unwrap();
        assert!(output.status.success(), "{:?}", output.stderr);
        let file = fs::OpenOptions::new()
            .read(true)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS)
            .open(redirect)
            .unwrap();
        let message = identity(&file).unwrap_err().to_string();
        assert!(
            message.contains("staged archive is a reparse point"),
            "{message}"
        );
        assert!(message.contains("without redirected paths"));
        assert!(!message.contains(SECRET));
        assert!(identity(&File::open(ordinary).unwrap()).is_ok());
    }
}
