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

/// The lite tier's bundled pg_dump (newest version directory first), then PATH.
pub(super) fn find(home: &Path) -> Option<PathBuf> {
    let install_root = home.join(".pg0").join("installation");
    if install_root.is_dir()
        && let Ok(entries) = fs::read_dir(&install_root)
    {
        let mut versions: Vec<PathBuf> = entries.flatten().map(|entry| entry.path()).collect();
        versions.sort_by_key(|path| {
            super::path_sort_key(&path.file_name().unwrap_or_default().to_string_lossy())
        });
        for version in versions.into_iter().rev() {
            let candidate = version.join("bin").join(EXE);
            if candidate.exists() {
                return Some(candidate);
            }
        }
    }
    which()
}

/// Python 3.11 `shutil.which("pg_dump")`.
fn which() -> Option<PathBuf> {
    let path = match std::env::var_os("PATH") {
        Some(path) => path,
        None if cfg!(windows) => ".;C:\\bin".into(),
        None => "/bin:/usr/bin".into(),
    };
    let mut directories: Vec<PathBuf> = std::env::split_paths(&path).collect();
    let files: Vec<String> = if cfg!(windows) {
        if !directories.iter().any(|dir| dir.as_os_str() == ".") {
            directories.insert(0, PathBuf::from("."));
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
            directory
                .to_string_lossy()
                .replace('/', "\\")
                .to_lowercase()
        } else {
            directory.to_string_lossy().into_owned()
        };
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        for file in &files {
            let candidate = directory.join(file);
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
    let outcome = (|| -> io::Result<Result<(), String>> {
        let mut child = Command::new(pg_dump)
            .args(["--no-owner", "--no-acl", "--dbname", dsn])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;
        let mut stderr = child.stderr.take().expect("piped stderr");
        let reader = std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stderr.read_to_end(&mut bytes);
            bytes
        });
        let mut gz = GzEncoder::new(fs::File::create(&partial)?, Compression::best());
        let copied = io::copy(child.stdout.as_mut().expect("piped stdout"), &mut gz);
        let finished = copied.and_then(|_| gz.finish().map(drop));
        let status = child.wait()?;
        let stderr = reader.join().unwrap_or_default();
        finished?;
        if !status.success() {
            let text = String::from_utf8_lossy(&stderr);
            return Ok(Err(format!("pg_dump failed: {}", python_strip(&text))));
        }
        Ok(Ok(()))
    })();
    match outcome {
        Ok(Ok(())) => {}
        Ok(Err(message)) => {
            let _ = fs::remove_file(&partial);
            return Err(message);
        }
        Err(error) => {
            let _ = fs::remove_file(&partial);
            return Err(error.to_string());
        }
    }
    fs::rename(&partial, &target).map_err(|error| error.to_string())?;
    Ok(target)
}
