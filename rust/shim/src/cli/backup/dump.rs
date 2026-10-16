//! `_find_pg_dump` and `_dump_bank`.
use flate2::{Compression, write::GzEncoder};
use std::{
    fs,
    io::{self, Read},
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

const EXE: &str = if cfg!(windows) {
    "pg_dump.exe"
} else {
    "pg_dump"
};

/// A stat error Python 3.11's `Path.is_dir()`/`exists()` treats as "no";
/// any other error raises there and is reported here.
fn ignorable(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::NotFound || ignorable_code(error.raw_os_error())
}

#[cfg(unix)]
fn ignorable_code(code: Option<i32>) -> bool {
    matches!(code, Some(libc::ENOTDIR | libc::EBADF | libc::ELOOP))
}

#[cfg(windows)]
fn ignorable_code(code: Option<i32>) -> bool {
    windows_ignorable(code)
}

#[cfg(any(windows, test))]
fn windows_ignorable(code: Option<i32>) -> bool {
    // pathlib's ignored winerrors plus CPython's ENOENT/ENOTDIR/EBADF map.
    matches!(
        code,
        Some(2 | 3 | 6 | 15 | 18 | 21 | 53 | 67 | 114 | 123 | 130 | 161 | 206 | 267 | 1921)
    )
}

#[cfg(any(windows, test))]
fn windows_class(code: i32) -> &'static str {
    match code {
        89 | 164 | 215 | 10035..=10037 => "BlockingIOError",
        128 | 129 => "ChildProcessError",
        109 | 232 | 10058 => "BrokenPipeError",
        10053 => "ConnectionAbortedError",
        10061 => "ConnectionRefusedError",
        10054 => "ConnectionResetError",
        10004 => "InterruptedError",
        10060 => "TimeoutError",
        2 | 3 | 15 | 18 | 53 | 67 | 161 | 206 => "FileNotFoundError",
        5 | 16 | 19..=34 | 36 | 65 | 82 | 83 | 108 | 132 | 158 | 167 | 10013 => "PermissionError",
        80 | 183 => "FileExistsError",
        267 => "NotADirectoryError",
        _ => "OSError",
    }
}

#[cfg(unix)]
fn errno_class(code: i32) -> &'static str {
    #[cfg(any(target_vendor = "apple", target_os = "freebsd"))]
    if code == libc::ENOTCAPABLE {
        return "PermissionError";
    }
    // CPython Objects/exceptions.c: its fixed errno-to-OSError subclass table.
    for (number, class) in [
        (libc::EAGAIN, "BlockingIOError"),
        (libc::EALREADY, "BlockingIOError"),
        (libc::EINPROGRESS, "BlockingIOError"),
        (libc::EWOULDBLOCK, "BlockingIOError"),
        (libc::ECHILD, "ChildProcessError"),
        (libc::EPIPE, "BrokenPipeError"),
        (libc::ESHUTDOWN, "BrokenPipeError"),
        (libc::ECONNABORTED, "ConnectionAbortedError"),
        (libc::ECONNREFUSED, "ConnectionRefusedError"),
        (libc::ECONNRESET, "ConnectionResetError"),
        (libc::EEXIST, "FileExistsError"),
        (libc::ENOENT, "FileNotFoundError"),
        (libc::EINTR, "InterruptedError"),
        (libc::EISDIR, "IsADirectoryError"),
        (libc::ENOTDIR, "NotADirectoryError"),
        (libc::EACCES, "PermissionError"),
        (libc::EPERM, "PermissionError"),
        (libc::ESRCH, "ProcessLookupError"),
        (libc::ETIMEDOUT, "TimeoutError"),
    ] {
        if number == code {
            return class;
        }
    }
    "OSError"
}

pub(super) fn discovery_paths_supported(home: &Path) -> Option<()> {
    home.to_str()?;
    if let Some(path) = std::env::var_os("PATH") {
        path.to_str()?;
    }
    if let Ok(entries) = fs::read_dir(home.join(".pg0").join("installation")) {
        for entry in entries.flatten() {
            entry.path().to_str()?;
        }
    }
    Some(())
}

pub(super) struct DiscoveryError {
    path: PathBuf,
    error: io::Error,
}
impl DiscoveryError {
    fn new(path: &Path, error: io::Error) -> Self {
        Self {
            path: path.to_owned(),
            error,
        }
    }
    pub(super) fn diagnostic(&self) -> String {
        let code = self.error.raw_os_error().unwrap_or(0);
        #[cfg(unix)]
        let class = errno_class(code);
        #[cfg(windows)]
        let class = windows_class(code);
        let message = self.error.to_string();
        let message = message
            .strip_suffix(&format!(" (os error {code})"))
            .unwrap_or(&message);
        let message = if cfg!(windows) {
            message.trim_end_matches(|c: char| c <= ' ' || c == '.')
        } else {
            message
        };
        let label = if cfg!(windows) { "WinError" } else { "Errno" };
        format!(
            "{class}: [{label} {code}] {message}: {}\n",
            super::super::mode_repr(&self.path.to_string_lossy())
        )
    }
}

fn is_dir(path: &Path) -> Result<bool, DiscoveryError> {
    match fs::metadata(path) {
        Ok(meta) => Ok(meta.is_dir()),
        Err(error) if ignorable(&error) => Ok(false),
        Err(error) => Err(DiscoveryError::new(path, error)),
    }
}

fn exists(path: &Path) -> Result<bool, DiscoveryError> {
    match fs::metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if ignorable(&error) => Ok(false),
        Err(error) => Err(DiscoveryError::new(path, error)),
    }
}

/// The lite tier's bundled pg_dump (newest version directory first), then PATH.
/// Unexpected filesystem errors propagate with the path Python reports.
pub(super) fn find(home: &Path) -> Result<Option<PathBuf>, DiscoveryError> {
    let install_root = home.join(".pg0").join("installation");
    if is_dir(&install_root)? {
        let mut versions = Vec::new();
        let entries = fs::read_dir(&install_root)
            .map_err(|error| DiscoveryError::new(&install_root, error))?;
        for entry in entries {
            versions.push(
                entry
                    .map_err(|error| DiscoveryError::new(&install_root, error))?
                    .path(),
            );
        }
        versions.sort_by_key(|path| {
            super::path_sort_key(&path.file_name().unwrap_or_default().to_string_lossy())
        });
        for version in versions.into_iter().rev() {
            let candidate = version.join("bin").join(EXE);
            if exists(&candidate)? {
                return Ok(Some(candidate));
            }
        }
    }
    Ok(which())
}

/// Python 3.11 `shutil.which("pg_dump")`: PATH split on the separator as
/// text (quotes and empty entries kept), `None` for an empty PATH.
fn which() -> Option<PathBuf> {
    let path = match std::env::var_os("PATH") {
        Some(path) => path.into_string().ok()?,
        None if cfg!(windows) => ".;C:\\bin".into(),
        None if cfg!(target_os = "macos") => "/usr/bin:/bin:/usr/sbin:/sbin".into(),
        None => "/bin:/usr/bin".into(),
    };
    if path.is_empty() {
        return None;
    }
    let separator = if cfg!(windows) { ';' } else { ':' };
    let mut directories: Vec<&str> = path.split(separator).collect();
    let files: Vec<String> = if cfg!(windows) {
        if !directories.contains(&".") {
            directories.insert(0, ".");
        }
        let pathext = std::env::var("PATHEXT")
            .ok()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS;.MSC".into());
        pathext
            .split(';')
            .filter(|ext| !ext.is_empty())
            .map(|ext| format!("pg_dump{ext}"))
            .collect()
    } else {
        vec!["pg_dump".into()]
    };
    let mut seen = Vec::new();
    for directory in directories {
        let key = if cfg!(windows) {
            directory.replace('/', "\\").to_lowercase()
        } else {
            directory.to_owned()
        };
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        for file in &files {
            let candidate = Path::new(directory).join(file);
            if executable(&candidate) {
                return Some(candidate);
            }
        }
    }
    None
}

#[cfg(windows)]
fn executable(path: &Path) -> bool {
    fs::metadata(path).is_ok_and(|meta| !meta.is_dir())
}

#[cfg(not(windows))]
fn executable(path: &Path) -> bool {
    fs::metadata(path).is_ok_and(|meta| !meta.is_dir())
        && rustix::fs::access(path, rustix::fs::Access::EXEC_OK).is_ok()
}

/// Python `str.strip()`: Unicode whitespace plus the ASCII separators U+001C..U+001F.
fn python_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// Stream `pg_dump --no-owner --no-acl` through gzip into `<ts>.sql.gz`.
pub(super) fn write(dsn: &str, pg_dump: &Path, bdir: &Path, ts: &str) -> Result<PathBuf, String> {
    let target = super::args::join(bdir, &format!("{}{ts}.sql.gz", super::DUMP_PREFIX));
    let partial = super::args::join(bdir, &format!("{}{ts}.sql.gz.part", super::DUMP_PREFIX));
    // Python starts pg_dump before its cleanup scope: a spawn failure leaves
    // any existing partial file alone.
    let mut child = Command::new(pg_dump)
        .args(["--no-owner", "--no-acl", "--dbname", dsn])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| error.to_string())?;
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stderr.read_to_end(&mut bytes);
        bytes
    });
    let copied = (|| -> io::Result<()> {
        let mut gz = GzEncoder::new(fs::File::create(&partial)?, Compression::best());
        io::copy(&mut stdout, &mut gz)?;
        gz.finish()?;
        Ok(())
    })();
    drop(stdout);
    if let Err(error) = copied {
        // A pg_dump still writing would block on the full pipe forever.
        let _ = child.kill();
        let _ = child.wait();
        let _ = fs::remove_file(&partial);
        return Err(error.to_string());
    }
    let stderr = reader.join().unwrap_or_default();
    let status = match child.wait() {
        Ok(status) => status,
        Err(error) => {
            let _ = fs::remove_file(&partial);
            return Err(error.to_string());
        }
    };
    if !status.success() {
        let _ = fs::remove_file(&partial);
        let text = String::from_utf8_lossy(&stderr);
        return Err(format!("pg_dump failed: {}", python_strip(&text)));
    }
    fs::rename(&partial, &target).map_err(|error| error.to_string())?;
    Ok(target)
}

#[cfg(all(test, windows))]
mod windows_tests {
    use super::*;

    #[test]
    fn discovery_errors_use_python_winerror_formatting() {
        let error = DiscoveryError::new(
            Path::new(r"C:\fixture\installation"),
            io::Error::from_raw_os_error(5),
        );
        assert_eq!(
            error.diagnostic(),
            "PermissionError: [WinError 5] Access is denied: 'C:\\\\fixture\\\\installation'\n"
        );
        let sharing = DiscoveryError::new(Path::new("fixture"), io::Error::from_raw_os_error(32));
        assert!(
            sharing
                .diagnostic()
                .starts_with("PermissionError: [WinError 32] ")
        );
        assert!(!sharing.diagnostic().contains(".: "));
        for code in [6, 21, 123, 267, 1921] {
            assert!(ignorable(&io::Error::from_raw_os_error(code)));
        }
    }
}

#[cfg(test)]
mod error_map_tests {
    #[cfg(unix)]
    #[test]
    fn fixed_errno_table_keeps_python_exception_classes() {
        for (code, class) in [
            (libc::EAGAIN, "BlockingIOError"),
            (libc::EALREADY, "BlockingIOError"),
            (libc::EINPROGRESS, "BlockingIOError"),
            (libc::EWOULDBLOCK, "BlockingIOError"),
            (libc::ECHILD, "ChildProcessError"),
            (libc::EPIPE, "BrokenPipeError"),
            (libc::ESHUTDOWN, "BrokenPipeError"),
            (libc::ECONNABORTED, "ConnectionAbortedError"),
            (libc::ECONNREFUSED, "ConnectionRefusedError"),
            (libc::ECONNRESET, "ConnectionResetError"),
            (libc::EEXIST, "FileExistsError"),
            (libc::ENOENT, "FileNotFoundError"),
            (libc::EISDIR, "IsADirectoryError"),
            (libc::ENOTDIR, "NotADirectoryError"),
            (libc::EACCES, "PermissionError"),
            (libc::EPERM, "PermissionError"),
            (libc::ESRCH, "ProcessLookupError"),
            (libc::ETIMEDOUT, "TimeoutError"),
            (libc::EINTR, "InterruptedError"),
        ] {
            let message = super::DiscoveryError::new(
                std::path::Path::new("fixture"),
                std::io::Error::from_raw_os_error(code),
            )
            .diagnostic();
            assert!(
                message.starts_with(&format!("{class}: [Errno {code}] ")),
                "{message}"
            );
        }
    }
    use super::*;

    #[test]
    fn win32_codes_follow_cpython_311_errno_classes() {
        for (code, class) in [
            (89, "BlockingIOError"),
            (164, "BlockingIOError"),
            (215, "BlockingIOError"),
            (128, "ChildProcessError"),
            (129, "ChildProcessError"),
            (109, "BrokenPipeError"),
            (232, "BrokenPipeError"),
            (10035, "BlockingIOError"),
            (10036, "BlockingIOError"),
            (10037, "BlockingIOError"),
            (10004, "InterruptedError"),
            (10013, "PermissionError"),
            (10058, "BrokenPipeError"),
            (10053, "ConnectionAbortedError"),
            (10061, "ConnectionRefusedError"),
            (10054, "ConnectionResetError"),
            (10060, "TimeoutError"),
        ] {
            assert_eq!(windows_class(code), class, "{code}");
        }
        // CPython PC/errmap.h: EACCES includes device read failures; EBADF
        // includes both invalid-target and direct-access handles.
        for code in [
            5, 16, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 36, 65, 82, 83,
            108, 132, 158, 167,
        ] {
            assert_eq!(windows_class(code), "PermissionError", "{code}");
        }
        for code in [35, 123, 4390] {
            assert_eq!(windows_class(code), "OSError", "{code}");
        }
        for code in [6, 114, 130, 267, 1921] {
            assert!(windows_ignorable(Some(code)), "{code}");
        }
    }
}
