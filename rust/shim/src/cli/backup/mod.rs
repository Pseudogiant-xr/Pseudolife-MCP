//! `backup`: a Docker bank (restore's verified backup under the backup
//! names, `restore_cli::docker_backup`), or pg_dump of an explicit DSN plus
//! the state archive, with rotation.
//!
//! Oracle: `pseudolife_memory/backup_cli.py`. A lite bank (an embedded
//! instance) is refused before any effect, naming the Docker install.
mod archive;
pub(super) mod args;
mod dump;

use super::restore_cli::docker_backup;

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

/// Reuse the physical backup format, retaining this pair outside rotation.
pub(super) fn migration_backup(data_dir: &Path, dsn: &str) -> Result<serde_json::Value, String> {
    use std::io::Read;
    let bdir = data_dir
        .join("backups")
        .join(format!("migrate-flat-{}", uuid::Uuid::new_v4().simple()));
    let fail = || {
        "the bank backup could not finish; nothing changed; check pg_dump and the state directory, then retry migrate-flat --apply".to_owned()
    };
    let home = home().ok_or_else(fail)?;
    let pg_dump = dump::find(&home).map_err(|_| fail())?.ok_or_else(fail)?;
    archive::admit(data_dir, &bdir).map_err(|_| fail())?;
    let stamp = timestamp().ok_or_else(fail)?;
    fs::create_dir_all(&bdir).map_err(|_| fail())?;
    let dump = dump::write(dsn, &pg_dump, &bdir, &stamp).map_err(|_| fail())?;
    let state = archive::write(data_dir, &bdir, &stamp).map_err(|_| fail())?;
    let mut source = flate2::read::GzDecoder::new(fs::File::open(&dump).map_err(|_| fail())?);
    let mut tail = Vec::new();
    let mut buffer = [0u8; 65536];
    loop {
        let n = source.read(&mut buffer).map_err(|_| fail())?;
        if n == 0 {
            break;
        }
        tail.extend_from_slice(&buffer[..n]);
        if tail.len() > 65536 {
            tail.drain(..tail.len() - 65536);
        }
    }
    if !tail
        .windows(b"-- PostgreSQL database dump complete".len())
        .any(|w| w == b"-- PostgreSQL database dump complete")
    {
        return Err("the bank backup is incomplete; nothing changed; repair backup and retry migrate-flat --apply".into());
    }
    let original = fs::read(data_dir.join("config.yaml")).map_err(|_| fail())?;
    let gzip = flate2::read::GzDecoder::new(fs::File::open(&state).map_err(|_| fail())?);
    let mut archive = tar::Archive::new(gzip);
    let mut matched = false;
    for member in archive.entries().map_err(|_| fail())? {
        let mut member = member.map_err(|_| fail())?;
        if member.path().map_err(|_| fail())?.as_ref() == Path::new("config.yaml")
            && member.header().entry_type().is_file()
        {
            let mut bytes = Vec::new();
            member.read_to_end(&mut bytes).map_err(|_| fail())?;
            matched = bytes == original;
        }
    }
    if !matched {
        return Err("state backup does not contain the unchanged config.yaml; nothing changed; retry migrate-flat --apply".into());
    }
    let flush_failed = |_| {
        "the bank backup could not be flushed to disk; nothing changed; repair the backup filesystem and retry migrate-flat --apply".to_owned()
    };
    for path in [&dump, &state] {
        // Windows FlushFileBuffers requires a writable handle and includes metadata.
        fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .and_then(|file| file.sync_all())
            .map_err(flush_failed)?;
    }
    #[cfg(unix)]
    for path in [bdir.as_path(), bdir.parent().unwrap(), data_dir] {
        fs::File::open(path)
            .and_then(|directory| directory.sync_all())
            .map_err(flush_failed)?;
    }
    Ok(serde_json::json!({"dump": dump, "state": state}))
}

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

/// The data directory when `--data-dir` is not given:
/// `PSEUDOLIFE_MCP_DATA_DIR`, else `./data`. A lite bank in the per-user
/// lite directory is refused (the native client backs up a Docker bank, or
/// a bank its `PSEUDOLIFE_MCP_DATABASE_URL` names); that directory alone
/// (it also holds the installed client) is no bank.
fn default_data_dir() -> Result<PathBuf, u8> {
    if let Some(raw) = std::env::var_os("PSEUDOLIFE_MCP_DATA_DIR").filter(|raw| !raw.is_empty()) {
        return Ok(args::pathlib(Path::new(&raw)));
    }
    if let Some(lite) = lite_default()
        && crate::cli::retired::is_lite_bank(&lite)
        && nonempty_env("PSEUDOLIFE_MCP_DATABASE_URL")
            .flatten()
            .is_none()
    {
        return Err(lite_refusal(&lite));
    }
    match std::env::current_dir() {
        Ok(cwd) => Ok(cwd.join("data")),
        Err(error) => Err(refused(&format!(
            "cannot read the current directory ({error}); pass --data-dir <directory>, then retry. No backup was written"
        ))),
    }
}

/// `backup failed: <message>`, exit 2: a refusal before anything is written.
fn refused(message: &str) -> u8 {
    let _ = write_stream(false, &format!("backup failed: {message}\n"));
    2
}

/// The shared lite-tier refusal (cli/retired.rs), as backup's refusals read.
fn lite_refusal(dir: &Path) -> u8 {
    refused(&format!(
        "{} No backup was written",
        crate::cli::retired::lite_bank_refusal(dir)
    ))
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

fn missing_data_dir_message(data_dir: &str) -> String {
    format!(
        "data dir {data_dir} does not exist — nothing to back up. \
         If this is a Docker install, Docker or its bank containers may be unavailable; \
         start Docker and retry, or pass --container <postgres>.\n"
    )
}

/// Every argv is answered here: the dispatcher never defers `backup`.
pub(super) fn run(arguments: Vec<OsString>) -> Option<u8> {
    Some(main(arguments))
}

fn main(arguments: Vec<OsString>) -> u8 {
    let parsed = args::parse(&arguments).expect("backup's parser answers every argv");
    let args = match parsed {
        args::Parsed::Args(args) => args,
        args::Parsed::Help => {
            return if write_stream(true, &args::help(args::width())).is_ok() {
                0
            } else {
                120
            };
        }
        args::Parsed::Error(message) => {
            let _ = write_stream(
                false,
                &format!(
                    "{}pseudolife-mcp backup: error: {message}\n",
                    args::usage(args::width())
                ),
            );
            return 2;
        }
    };
    // The Docker tier: a Docker selector, or no --data-dir and a bank
    // container `docker ps -a` lists.
    let tier = if args.docker() {
        docker_backup::Configured::Yes
    } else if args.data_dir.is_none() {
        docker_backup::configured()
    } else {
        docker_backup::Configured::No
    };
    // Docker that could not be asked never turns into a file-tier backup
    // without a database dump that exits 0.
    if let docker_backup::Configured::Unknown(cause) = &tier
        && nonempty_env("PSEUDOLIFE_MCP_DATABASE_URL")
            .flatten()
            .is_none()
    {
        return refused(&format!(
            "Docker is installed but could not be asked whether this host runs a Docker bank ({cause}), and without that this backup would archive a data directory without its database; start Docker and retry, or pass --data-dir <directory> to back up a data directory without a database. No backup was written"
        ));
    }
    if tier == docker_backup::Configured::Yes {
        return docker_backup::run(&docker_backup::Request {
            container: args.container.clone(),
            daemon_container: args.daemon_container.clone(),
            db: args.db.clone(),
            user: args.user.clone(),
            db_port: args.db_port,
            out: args.out.clone(),
            keep_days: args.keep_days,
        });
    }
    let data_dir = match &args.data_dir {
        Some(value) => value.clone(),
        None => match default_data_dir() {
            Ok(dir) => dir,
            Err(code) => return code,
        },
    };
    match data_dir.try_exists() {
        Ok(true) => {}
        Ok(false) => {
            let _ = write_stream(
                false,
                &missing_data_dir_message(&data_dir.to_string_lossy()),
            );
            return 1;
        }
        Err(error) => {
            return refused(&format!(
                "cannot read the data directory {} ({error}); restore access to it or pass another --data-dir, then retry. No backup was written",
                data_dir.display()
            ));
        }
    }
    let Some(dsn) = nonempty_env("PSEUDOLIFE_MCP_DATABASE_URL") else {
        return refused(
            "PSEUDOLIFE_MCP_DATABASE_URL is not valid Unicode text; set it to the bank's PostgreSQL URL, then retry. No backup was written",
        );
    };
    if dsn.is_none()
        && !matches!(
            data_dir.join("embedded_pg").join("PG_VERSION").try_exists(),
            Ok(false)
        )
    {
        return lite_refusal(&data_dir);
    }
    let bdir = match &args.out {
        Some(out) => out.clone(),
        None => args::join(&data_dir, "backups"),
    };
    match fs::metadata(&bdir) {
        Ok(meta) if !meta.is_dir() => {
            return refused(&format!(
                "{} is not a directory; name a directory with --out <directory>, then retry. No backup was written",
                bdir.display()
            ));
        }
        Err(error) if error.kind() != io::ErrorKind::NotFound => {
            return refused(&format!(
                "cannot read the backup directory {} ({error}); restore access to it or choose another --out, then retry. No backup was written",
                bdir.display()
            ));
        }
        _ => {}
    }
    match archive::admit(&data_dir, &bdir) {
        Ok(()) => {}
        Err(archive::Unadmitted::Inside) => {
            return refused(&format!(
                "the backup directory {} is inside the data directory {} (outside its own backups directory), which the archive includes; choose --out <a directory outside the data directory>, then retry. No backup was written",
                bdir.display(),
                data_dir.display()
            ));
        }
        Err(archive::Unadmitted::Unresolved(path)) => {
            return refused(&format!(
                "cannot resolve {} the way the archive would (a symlink loop, a link that cannot be followed, or an entry that cannot be read); repair or remove it, then retry. No backup was written",
                path.display()
            ));
        }
        Err(archive::Unadmitted::Unsupported(path)) => {
            return refused(&format!(
                "{} in the data directory is a kind of entry this backup cannot archive the way restore reads it back (a link it cannot follow or a special file); remove it or move it outside the data directory, then retry. No backup was written",
                path.display()
            ));
        }
    }
    let pg_dump = match &dsn {
        Some(_) => {
            let Some(home) = home() else {
                return refused(
                    "cannot find your home directory to look for pg_dump; set HOME (USERPROFILE on Windows), then retry. No backup was written",
                );
            };
            if dump::discovery_paths_supported(&home).is_none() {
                return refused(
                    "PATH or the home directory is not valid Unicode text, so pg_dump cannot be looked up; fix PATH, then retry. No backup was written",
                );
            }
            Some(dump::find(&home))
        }
        None => None,
    };
    let Some(ts) = timestamp() else {
        return refused(
            "cannot read the local time for the backup's name; check the system clock and TZ, then retry. No backup was written",
        );
    };
    // Python's Windows rename refuses an existing target after writing the
    // partial; elsewhere it replaces it, as this backup does.
    if cfg!(windows) {
        for name in [
            format!("{STATE_PREFIX}{ts}.tar.gz"),
            format!("{DUMP_PREFIX}{ts}.sql.gz"),
        ] {
            let path = bdir.join(&name);
            match path.try_exists() {
                Ok(false) => {}
                Ok(true) => {
                    return refused(&format!(
                        "a backup named for {ts} already exists in {} (two backups in one second); wait a second, then retry. No backup was written",
                        bdir.display()
                    ));
                }
                Err(error) => {
                    return refused(&format!(
                        "cannot check whether {} exists ({error}); restore access to the backup directory or choose another --out, then retry. No backup was written",
                        path.display()
                    ));
                }
            }
        }
    }

    // Effects start here, in Python's order.
    if let Err(error) = fs::create_dir_all(&bdir) {
        return failed(&error.to_string());
    }
    if let Some(Err(error)) = &pg_dump {
        let _ = write_stream(false, &error.diagnostic());
        return 1;
    }
    let dump = match (&dsn, pg_dump) {
        (Some(dsn), Some(Ok(Some(pg_dump)))) => match dump::write(dsn, &pg_dump, &bdir, &ts) {
            Ok(path) => Some(path),
            Err(message) => return failed(&message),
        },
        (Some(_), _) => {
            return failed("pg_dump not found — install the PostgreSQL client tools.");
        }
        (None, _) => None,
    };
    let state = match archive::write(&data_dir, &bdir, &ts) {
        Ok(path) => path,
        Err(error) => return failed(&error.to_string()),
    };
    let pruned = match rotate(&bdir, args.keep_days, dump.is_some()) {
        Ok(pruned) => pruned,
        Err(error) => return failed(&error.to_string()),
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
    if write_stream(true, &text).is_ok() {
        0
    } else {
        120
    }
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
    fn missing_bank_refusal_names_the_docker_recovery() {
        let message = missing_data_dir_message("missing-bank");
        assert!(message.starts_with("data dir missing-bank does not exist — nothing to back up."));
        assert!(message.contains("start Docker and retry"));
        assert!(message.contains("--container <postgres>"));
    }

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
