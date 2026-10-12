//! `backup`: pg_dump of an explicit DSN plus the state archive, with rotation.
//!
//! Oracle: `pseudolife_memory/backup_cli.py`. Cases requiring the lite
//! tier's embedded instance defer before any effect.
mod archive;
mod args;
mod dump;

use std::{
    ffi::OsString,
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
    time::SystemTime,
};

const HELP: &str = include_str!("../backup_help.txt");
const DUMP_PREFIX: &str = "pseudolife_lite_memory-";
const STATE_PREFIX: &str = "pseudolife_lite_state-";

/// `Path.home()` as Python 3.11 resolves it; unusual environments defer.
fn home() -> Option<PathBuf> {
    if cfg!(windows) {
        if let Some(profile) = std::env::var_os("USERPROFILE") {
            return Some(PathBuf::from(profile));
        }
        let path = std::env::var_os("HOMEPATH")?;
        let mut drive = std::env::var_os("HOMEDRIVE").unwrap_or_default();
        drive.push(path);
        Some(PathBuf::from(drive))
    } else {
        std::env::var_os("HOME")
            .filter(|home| !home.is_empty())
            .map(PathBuf::from)
    }
}

fn nonempty_env(name: &str) -> Option<Option<String>> {
    match std::env::var_os(name) {
        None => Some(None),
        Some(value) if value.is_empty() => Some(None),
        Some(value) => Some(Some(value.into_string().ok()?)),
    }
}

/// `embedded_pg.default_lite_data_dir()`.
fn lite_default() -> Option<PathBuf> {
    let base = if cfg!(windows) {
        match nonempty_env("LOCALAPPDATA")? {
            Some(local) => PathBuf::from(local),
            None => home()?.join("AppData").join("Local"),
        }
    } else if cfg!(target_os = "macos") {
        home()?.join("Library").join("Application Support")
    } else {
        match nonempty_env("XDG_DATA_HOME")? {
            Some(data) => PathBuf::from(data),
            None => home()?.join(".local").join("share"),
        }
    };
    Some(base.join("pseudolife-mcp"))
}

/// `_default_data_dir`, deferring wherever Python could pick the lite tier.
fn default_data_dir() -> Option<PathBuf> {
    if let Some(raw) = nonempty_env("PSEUDOLIFE_MCP_DATA_DIR")? {
        return Some(args::pathlib(Path::new(&raw)));
    }
    if !matches!(lite_default()?.try_exists(), Ok(false)) {
        return None;
    }
    let cwd = std::env::current_dir().ok()?;
    Some(cwd.join("data"))
}

fn write_stream(stdout: bool, text: &str) -> io::Result<()> {
    let bytes = super::text_bytes(text);
    if stdout {
        let mut out = io::stdout().lock();
        out.write_all(&bytes)?;
        out.flush()
    } else {
        io::stderr().lock().write_all(&bytes)
    }
}

/// Raw unrelated names are harmless; a rotated raw name cannot be printed
/// through this leaf's UTF-8 stdout contract.
fn rotation_names_supported(bdir: &Path, include_dumps: bool) -> Option<()> {
    let entries = match fs::read_dir(bdir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Some(()),
        Err(_) => return None,
    };
    for entry in entries {
        let name = entry.ok()?.file_name();
        if name.to_str().is_none() {
            let text = name.to_string_lossy();
            if glob_matches(&text, STATE_PREFIX, ".tar.gz")
                || include_dumps && glob_matches(&text, DUMP_PREFIX, ".sql.gz")
            {
                return None;
            }
        }
    }
    Some(())
}

/// `None` defers to the dispatcher before any effect.
pub(super) fn run(arguments: Vec<OsString>) -> Option<u8> {
    if !args::columns_supported() {
        return None;
    }
    let args = match args::parse(&arguments)? {
        args::Parsed::Args(args) => args,
        args::Parsed::Help => {
            return Some(if write_stream(true, &args::help(args::width())).is_ok() {
                0
            } else {
                120
            });
        }
        args::Parsed::Error(message) => {
            let _ = write_stream(
                false,
                &format!(
                    "{}pseudolife-mcp backup: error: {message}\n",
                    args::usage(args::width())
                ),
            );
            return Some(2);
        }
    };
    let data_dir = match &args.data_dir {
        Some(value) => value.clone(),
        None => default_data_dir()?,
    };
    match data_dir.try_exists() {
        Ok(true) => {}
        Ok(false) => {
            let _ = write_stream(
                false,
                &format!(
                    "data dir {} does not exist — nothing to back up.\n",
                    data_dir.to_str()?
                ),
            );
            return Some(1);
        }
        Err(_) => return None,
    }
    let dsn = nonempty_env("PSEUDOLIFE_MCP_DATABASE_URL")?;
    if dsn.is_none()
        && !matches!(
            data_dir.join("embedded_pg").join("PG_VERSION").try_exists(),
            Ok(false)
        )
    {
        return None;
    }
    let bdir = match &args.out {
        Some(out) => out.clone(),
        None => args::join(&data_dir, "backups"),
    };
    match fs::metadata(&bdir) {
        Ok(meta) if !meta.is_dir() => return None,
        Err(error) if error.kind() != io::ErrorKind::NotFound => return None,
        _ => {}
    }
    archive::admit(&data_dir, &bdir)?;
    rotation_names_supported(&bdir, dsn.is_some())?;
    let pg_dump = match &dsn {
        Some(_) => {
            let home = home()?;
            dump::discovery_paths_supported(&home)?;
            Some(dump::find(&home))
        }
        None => None,
    };
    let ts = timestamp()?;
    // Python's Windows rename refuses an existing target after writing the partial.
    if cfg!(windows)
        && [
            format!("{STATE_PREFIX}{ts}.tar.gz"),
            format!("{DUMP_PREFIX}{ts}.sql.gz"),
        ]
        .iter()
        .any(|name| !matches!(bdir.join(name).try_exists(), Ok(false)))
    {
        return None;
    }

    // Effects start here, in Python's order.
    if let Err(error) = fs::create_dir_all(&bdir) {
        return Some(failed(&error.to_string()));
    }
    if let Some(Err(error)) = &pg_dump {
        let _ = write_stream(false, &error.diagnostic());
        return Some(1);
    }
    let dump = match (&dsn, pg_dump) {
        (Some(dsn), Some(Ok(Some(pg_dump)))) => match dump::write(dsn, &pg_dump, &bdir, &ts) {
            Ok(path) => Some(path),
            Err(message) => return Some(failed(&message)),
        },
        (Some(_), _) => {
            return Some(failed(
                "pg_dump not found — install PostgreSQL client tools or the \
                 pseudolife-mcp[lite] extra (whose embedded runtime bundles it).",
            ));
        }
        (None, _) => None,
    };
    let state = match archive::write(&data_dir, &bdir, &ts) {
        Ok(path) => path,
        Err(error) => return Some(failed(&error.to_string())),
    };
    let pruned = match rotate(&bdir, args.keep_days, dump.is_some()) {
        Ok(pruned) => pruned,
        Err(error) => return Some(failed(&error.to_string())),
    };
    let mut text = match &dump {
        Some(path) => format!("bank dump:     {}\n", path.display()),
        None => String::from(
            "bank dump:     skipped (no database configured — file-mode state archived only)\n",
        ),
    };
    text.push_str(&format!("state archive: {}\n", state.display()));
    for path in pruned {
        text.push_str(&format!("rotated out:   {}\n", path.display()));
    }
    // Python flushes stdout at interpreter shutdown; a closed stdout makes that exit 120.
    Some(if write_stream(true, &text).is_ok() {
        0
    } else {
        120
    })
}

fn failed(message: &str) -> u8 {
    let _ = write_stream(false, &format!("backup failed: {message}\n"));
    1
}

/// Python sorts `Path` objects: case-folded parts on Windows, plain strings elsewhere.
fn path_sort_key(name: &str) -> String {
    if cfg!(windows) {
        name.to_lowercase()
    } else {
        name.to_owned()
    }
}

fn glob_matches(name: &str, prefix: &str, suffix: &str) -> bool {
    if cfg!(windows) {
        return windows_glob_matches(name, prefix, suffix);
    }
    name.len() >= prefix.len() + suffix.len() && name.starts_with(prefix) && name.ends_with(suffix)
}

fn windows_glob_matches(name: &str, prefix: &str, suffix: &str) -> bool {
    fn matches(character: char, ascii: char) -> bool {
        character.eq_ignore_ascii_case(&ascii)
            || matches!(
                (ascii.to_ascii_lowercase(), character),
                ('i', 'İ' | 'ı') | ('s', 'ſ') | ('k', 'K')
            )
    }
    let mut characters = name.chars();
    for expected in prefix.chars() {
        if !characters
            .next()
            .is_some_and(|actual| matches(actual, expected))
        {
            return false;
        }
    }
    for expected in suffix.chars().rev() {
        if !characters
            .next_back()
            .is_some_and(|actual| matches(actual, expected))
        {
            return false;
        }
    }
    true
}

#[cfg(not(windows))]
fn timestamp() -> Option<String> {
    Some(chrono::Local::now().format("%Y%m%d-%H%M%S").to_string())
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn timestamp() -> Option<String> {
    let now = seconds(SystemTime::now()).floor() as libc::time_t;
    // SAFETY: Windows CRT tm contains integer fields, whose zero values are valid.
    let mut local: libc::tm = unsafe { std::mem::zeroed() };
    // SAFETY: CRT reads this child's inherited TZ; both pointers are valid for the call.
    unsafe {
        libc::tzset();
        if libc::localtime_s(&mut local, &now) != 0 {
            return None;
        }
    }
    Some(format!(
        "{:04}{:02}{:02}-{:02}{:02}{:02}",
        local.tm_year + 1900,
        local.tm_mon + 1,
        local.tm_mday,
        local.tm_hour,
        local.tm_min,
        local.tm_sec
    ))
}

/// CPython's float `st_mtime`: floored seconds plus non-negative nanoseconds * 1e-9.
fn seconds(time: SystemTime) -> f64 {
    let nanos = match time.duration_since(SystemTime::UNIX_EPOCH) {
        Ok(since) => since.as_nanos() as i128,
        Err(before) => -(before.duration().as_nanos() as i128),
    };
    let whole = nanos.div_euclid(1_000_000_000);
    let fraction = nanos.rem_euclid(1_000_000_000);
    whole as f64 + fraction as f64 * 1e-9
}

#[cfg(not(windows))]
fn now_seconds() -> f64 {
    seconds(SystemTime::now())
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn now_seconds() -> f64 {
    use windows_sys::Win32::{
        Foundation::FILETIME, System::SystemInformation::GetSystemTimeAsFileTime,
    };
    let mut time = FILETIME {
        dwLowDateTime: 0,
        dwHighDateTime: 0,
    };
    // SAFETY: the API writes one FILETIME into the valid output pointer.
    // CPython 3.11 uses this coarse clock, rather than Rust's precise clock.
    unsafe { GetSystemTimeAsFileTime(&mut time) };
    let ticks = (u64::from(time.dwHighDateTime) << 32) | u64::from(time.dwLowDateTime);
    let nanos = i128::from(ticks) * 100 - 11_644_473_600_000_000_000;
    clock_seconds(nanos)
}

/// `_PyTime_AsSecondsDouble` converts the whole nanosecond count at once;
/// `st_mtime` instead adds the fractional part after converting whole seconds.
#[cfg(any(windows, test))]
fn clock_seconds(nanos: i128) -> f64 {
    if nanos % 1_000_000_000 == 0 {
        (nanos / 1_000_000_000) as f64
    } else {
        nanos as f64 / 1e9
    }
}

/// `_rotate`: this tool's own files older than `keep_days`, dumps only when
/// this run produced one.
fn rotate(bdir: &Path, keep_days: f64, include_dumps: bool) -> io::Result<Vec<PathBuf>> {
    let cutoff = now_seconds() - keep_days * 86400.0;
    rotate_before(bdir, cutoff, include_dumps)
}

fn rotate_before(bdir: &Path, cutoff: f64, include_dumps: bool) -> io::Result<Vec<PathBuf>> {
    let mut patterns = vec![(STATE_PREFIX, ".tar.gz")];
    if include_dumps {
        patterns.insert(0, (DUMP_PREFIX, ".sql.gz"));
    }
    let mut pruned = Vec::new();
    for (prefix, suffix) in patterns {
        let mut names = Vec::new();
        for entry in fs::read_dir(bdir)? {
            let name = entry?.file_name();
            if let Some(name) = name.to_str()
                && glob_matches(name, prefix, suffix)
            {
                names.push(name.to_owned());
            }
        }
        names.sort_by_key(|name| path_sort_key(name));
        for name in names {
            let path = args::join(bdir, &name);
            if seconds(fs::metadata(&path)?.modified()?) < cutoff {
                fs::remove_file(&path)?;
                pruned.push(path);
            }
        }
    }
    Ok(pruned)
}

#[cfg(test)]
mod glob_tests {
    use super::*;

    #[test]
    fn clock_float_rounding_matches_python_time() {
        let nanos = 1_790_000_000_123_450_200_i128;
        assert_eq!(clock_seconds(nanos), 1_790_000_000.123_45);
        assert_eq!(clock_seconds(-1), -1e-9);
        assert_eq!(clock_seconds(-1_000_000_000), -1.0);
        assert_eq!(clock_seconds(1_790_000_000_000_000_000), 1_790_000_000.0);
    }

    #[test]
    fn rotation_keeps_files_equal_to_the_python_clock_cutoff() {
        use std::time::Duration;
        let root = std::env::temp_dir().join(format!(
            "pseudolife-backup-clock-boundary-{}",
            uuid::Uuid::new_v4()
        ));
        fs::create_dir(&root).unwrap();
        let clock = 1_790_000_000_123_450_200_u64;
        let mut paths = Vec::new();
        for (label, nanos) in [
            ("older", clock - 1_000_000_000),
            ("equal", clock - 200),
            ("newer", clock + 1_000_000_000),
        ] {
            let path = root.join(format!("{STATE_PREFIX}{label}.tar.gz"));
            fs::write(&path, b"state").unwrap();
            fs::OpenOptions::new()
                .write(true)
                .open(&path)
                .unwrap()
                .set_times(
                    fs::FileTimes::new()
                        .set_modified(SystemTime::UNIX_EPOCH + Duration::from_nanos(nanos)),
                )
                .unwrap();
            paths.push(path);
        }
        // CPython's clock float equals this file's independently rounded mtime.
        let cutoff = clock_seconds(i128::from(clock));
        let equal_mtime = seconds(fs::metadata(&paths[1]).unwrap().modified().unwrap());
        let removed = rotate_before(&root, cutoff, false).unwrap();
        let retained = paths[1..].iter().all(|path| path.exists());
        fs::remove_dir_all(root).unwrap();
        assert_eq!(equal_mtime, cutoff);
        assert_eq!(removed, paths[..1]);
        assert!(retained, "equal or newer archive was deleted");
    }

    #[cfg(windows)]
    #[test]
    fn windows_clock_matches_python_within_the_same_clock_tick() {
        use std::{
            io::{BufRead, BufReader},
            process::{Command, Stdio},
        };
        let python = std::env::var_os("PSEUDOLIFE_MCP_PYTHON").unwrap_or_else(|| "python".into());
        let mut child = Command::new(python)
            .args([
                "-u",
                "-c",
                "import sys,time\nprint(time.get_clock_info('time').implementation,flush=True)\nfor line in sys.stdin: print(repr(time.time()),flush=True)",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut output = BufReader::new(child.stdout.take().unwrap());
        let mut implementation = String::new();
        output.read_line(&mut implementation).unwrap();
        let mut input = child.stdin.take().unwrap();
        let mut stable = 0;
        let mut mismatch = None;
        {
            let mut sample = || {
                writeln!(input, "now").unwrap();
                let mut value = String::new();
                output.read_line(&mut value).unwrap();
                value.trim().parse::<f64>().unwrap()
            };
            for _ in 0..100 {
                let before = sample();
                let native = now_seconds();
                let after = sample();
                if before == after {
                    stable += 1;
                    if native != before {
                        mismatch = Some((before, native));
                    }
                    if stable == 5 {
                        break;
                    }
                }
            }
        }
        drop(input);
        assert!(child.wait().unwrap().success());
        assert_eq!(implementation.trim(), "GetSystemTimeAsFileTime()");
        assert_eq!(stable, 5, "no sufficient same-tick clock observations");
        assert_eq!(
            mismatch, None,
            "native clock differs within a Python clock tick"
        );
    }

    #[test]
    fn windows_rotation_glob_uses_python_regex_unicode_case_matching() {
        for name in [
            "pseudolıfe_lite_state-old.tar.gz",
            "pseudolİfe_lite_state-old.tar.gz",
            "pſeudolife_lite_state-old.tar.gz",
            "PSEUDOLIFE_LITE_STATE-old.TAR.GZ",
        ] {
            assert!(
                windows_glob_matches(name, STATE_PREFIX, ".tar.gz"),
                "{name}"
            );
        }
        for name in [
            "注意.txt",
            "pseudolife_lite_state-old.tar.gż",
            "foreign-state.tar.gz",
        ] {
            assert!(
                !windows_glob_matches(name, STATE_PREFIX, ".tar.gz"),
                "{name}"
            );
        }
    }
}
