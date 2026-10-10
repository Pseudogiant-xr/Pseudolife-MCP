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
/// any other error raises there, so it defers here.
fn ignorable(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::NotFound || ignorable_code(error.raw_os_error())
}

#[cfg(unix)]
fn ignorable_code(code: Option<i32>) -> bool {
    matches!(code, Some(libc::ENOTDIR | libc::EBADF | libc::ELOOP))
}

#[cfg(windows)]
fn ignorable_code(code: Option<i32>) -> bool {
    // ERROR_NOT_READY, ERROR_INVALID_NAME, ERROR_CANT_RESOLVE_FILENAME.
    matches!(code, Some(21 | 123 | 1921))
}

fn is_dir(path: &Path) -> Option<bool> {
    match fs::metadata(path) {
        Ok(meta) => Some(meta.is_dir()),
        Err(error) if ignorable(&error) => Some(false),
        Err(_) => None,
    }
}

fn exists(path: &Path) -> Option<bool> {
    match fs::metadata(path) {
        Ok(_) => Some(true),
        Err(error) if ignorable(&error) => Some(false),
        Err(_) => None,
    }
}

/// The lite tier's bundled pg_dump (newest version directory first), then
/// PATH. `None` defers: Python would raise while looking.
pub(super) fn find(home: &Path) -> Option<Option<PathBuf>> {
    let install_root = home.join(".pg0").join("installation");
    if is_dir(&install_root)? {
        let mut versions = Vec::new();
        for entry in fs::read_dir(&install_root).ok()? {
            versions.push(entry.ok()?.path());
        }
        versions.sort_by_key(|path| {
            super::path_sort_key(&path.file_name().unwrap_or_default().to_string_lossy())
        });
        for version in versions.into_iter().rev() {
            let candidate = version.join("bin").join(EXE);
            if exists(&candidate)? {
                return Some(Some(candidate));
            }
        }
    }
    Some(which())
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
    let target = bdir.join(format!("{}{ts}.sql.gz", super::DUMP_PREFIX));
    let partial = bdir.join(format!("{}{ts}.sql.gz.part", super::DUMP_PREFIX));
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
