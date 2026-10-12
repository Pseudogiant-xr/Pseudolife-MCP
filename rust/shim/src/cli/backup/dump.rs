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

fn windows_class(code: i32) -> &'static str {
    match code {
        2 | 3 | 15 | 18 | 53 | 67 | 161 | 206 => "FileNotFoundError",
        5 | 16 | 19..=36 | 65 | 82 | 83 | 108 | 132 | 158 | 167 => "PermissionError",
        80 | 183 => "FileExistsError",
        267 => "NotADirectoryError",
        _ => "OSError",
    }
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
        let mut class = match self.error.kind() {
            io::ErrorKind::PermissionDenied => "PermissionError",
            io::ErrorKind::NotFound => "FileNotFoundError",
            io::ErrorKind::NotADirectory => "NotADirectoryError",
            io::ErrorKind::TimedOut => "TimeoutError",
            io::ErrorKind::Interrupted => "InterruptedError",
            _ => "OSError",
        };
        let code = self.error.raw_os_error().unwrap_or(0);
        if cfg!(windows) {
            // CPython maps the Win32 code to errno before selecting a subclass;
            // sharing violations, for example, are PermissionError there.
            class = windows_class(code);
        }
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
    fn timeout_and_interrupt_keep_python_exception_classes() {
        for (code, class) in [
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
        // CPython PC/errmap.h: EACCES includes device read failures; EBADF
        // includes both invalid-target and direct-access handles.
        for code in [
            5, 16, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 65, 82,
            83, 108, 132, 158, 167,
        ] {
            assert_eq!(windows_class(code), "PermissionError", "{code}");
        }
        for code in [123, 4390] {
            assert_eq!(windows_class(code), "OSError", "{code}");
        }
        for code in [6, 114, 130, 267, 1921] {
            assert!(windows_ignorable(Some(code)), "{code}");
        }
    }
}
