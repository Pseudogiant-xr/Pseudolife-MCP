//! `backup`: pg_dump of an explicit DSN plus the state archive, with rotation.
//!
//! Oracle: `pseudolife_memory/backup_cli.py`. Only canonical argv and path
//! spellings are admitted, and every case that needs the lite tier's embedded
//! instance defers before any effect.
mod archive;
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

/// True when Python's `str(Path(value)) == value`, so a path can be printed
/// verbatim and joined like pathlib joins it. Every other spelling defers.
#[cfg(not(windows))]
fn canonical(value: &str) -> bool {
    let rest = value.strip_prefix('/').unwrap_or(value);
    value == "/"
        || (!rest.is_empty()
            && !rest.starts_with('-')
            && rest.split('/').all(|part| !part.is_empty() && part != "."))
}

#[cfg(windows)]
fn canonical(value: &str) -> bool {
    let bytes = value.as_bytes();
    let rest = if bytes.len() >= 3 && bytes[0].is_ascii_alphabetic() && &bytes[1..3] == b":\\" {
        if bytes.len() == 3 {
            return true;
        }
        &value[3..]
    } else {
        value
    };
    !rest.is_empty()
        && !rest.starts_with('-')
        && !rest.contains(['/', ':'])
        && rest.split('\\').all(|part| !part.is_empty() && part != ".")
}

fn has_parent_component(path: &Path) -> bool {
    path.components()
        .any(|part| matches!(part, std::path::Component::ParentDir))
}

struct Args {
    data_dir: Option<String>,
    out: Option<String>,
    keep_days: f64,
}

fn parse(arguments: &[OsString]) -> Option<Args> {
    let mut args = Args {
        data_dir: None,
        out: None,
        keep_days: 7.0,
    };
    let mut keep_days_seen = false;
    let mut values = arguments.iter();
    while let Some(option) = values.next() {
        let value = values.next()?.to_str()?;
        match option.to_str()? {
            "--data-dir" if args.data_dir.is_none() && canonical(value) => {
                args.data_dir = Some(value.to_owned());
            }
            "--out" if args.out.is_none() && canonical(value) => args.out = Some(value.to_owned()),
            "--keep-days" if !keep_days_seen => {
                let (whole, fraction) = value.split_once('.').unwrap_or((value, "0"));
                if whole.is_empty()
                    || fraction.is_empty()
                    || !whole
                        .bytes()
                        .chain(fraction.bytes())
                        .all(|b| b.is_ascii_digit())
                {
                    return None;
                }
                // Under a minute, rotation races this run's own new files against
                // the platform clock (Python 3.11 reads the coarse clock on Windows).
                args.keep_days = value
                    .parse()
                    .ok()
                    .filter(|days: &f64| days.is_finite() && days * 86400.0 >= 60.0)?;
                keep_days_seen = true;
            }
            _ => return None,
        }
    }
    Some(args)
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

/// `_default_data_dir`, deferring wherever Python could pick the lite tier.
fn default_data_dir() -> Option<PathBuf> {
    if let Some(raw) = nonempty_env("PSEUDOLIFE_MCP_DATA_DIR")? {
        return canonical(&raw).then(|| PathBuf::from(raw));
    }
    if !matches!(lite_default()?.try_exists(), Ok(false)) {
        return None;
    }
    let cwd = std::env::current_dir().ok()?;
    canonical(cwd.to_str()?).then(|| cwd.join("data"))
}

fn absolute(path: &Path) -> Option<PathBuf> {
    if path.is_absolute() {
        Some(path.to_path_buf())
    } else {
        Some(std::env::current_dir().ok()?.join(path))
    }
}

fn same_name(left: &str, right: &str) -> bool {
    if cfg!(windows) {
        left.to_lowercase() == right.to_lowercase()
    } else {
        left == right
    }
}

/// Defer when `--out` would sit inside an archived child of the data dir,
/// or be the data dir itself: Python would archive its own partial file.
fn nested_out(data_dir: &Path, bdir: &Path) -> Option<bool> {
    let data = absolute(data_dir)?;
    let out = absolute(bdir)?;
    let data: Vec<_> = data.components().collect();
    let out: Vec<_> = out.components().collect();
    if out.len() < data.len() {
        return Some(false);
    }
    let inside = data.iter().zip(&out).all(|(left, right)| {
        same_name(
            &left.as_os_str().to_string_lossy(),
            &right.as_os_str().to_string_lossy(),
        )
    });
    Some(inside && out.len() != data.len() + 1)
}

fn write_stream(stdout: bool, text: &str) {
    let bytes = super::text_bytes(text);
    let _ = if stdout {
        io::stdout().lock().write_all(&bytes)
    } else {
        io::stderr().lock().write_all(&bytes)
    };
}

fn help() -> u8 {
    if io::stdout()
        .lock()
        .write_all(&super::text_bytes(HELP))
        .is_ok()
    {
        0
    } else {
        1
    }
}

/// `None` defers to the dispatcher before any effect.
pub(super) fn run(arguments: Vec<OsString>) -> Option<u8> {
    if arguments == [OsString::from("--help")] {
        return (std::env::var_os("COLUMNS") == Some(OsString::from("80"))).then(help);
    }
    let args = parse(&arguments)?;
    let data_dir = match &args.data_dir {
        Some(value) => PathBuf::from(value),
        None => default_data_dir()?,
    };
    match data_dir.try_exists() {
        Ok(true) => {}
        Ok(false) => {
            write_stream(
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
        Some(out) => PathBuf::from(out),
        None => data_dir.join("backups"),
    };
    if has_parent_component(&data_dir) || has_parent_component(&bdir) {
        return None;
    }
    match fs::metadata(&bdir) {
        Ok(meta) if !meta.is_dir() => return None,
        Err(error) if error.kind() != io::ErrorKind::NotFound => return None,
        _ => {}
    }
    if args.out.is_some() && nested_out(&data_dir, &bdir)? {
        return None;
    }
    archive::admit(&data_dir, &bdir)?;
    let pg_dump = match &dsn {
        Some(_) => Some(dump::find(&home()?)),
        None => None,
    };

    // Effects start here, in Python's order.
    if fs::create_dir_all(&bdir).is_err() {
        return None;
    }
    let ts = chrono::Local::now().format("%Y%m%d-%H%M%S").to_string();
    let dump = match (&dsn, pg_dump) {
        (Some(dsn), Some(Some(pg_dump))) => match dump::write(dsn, &pg_dump, &bdir, &ts) {
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
    write_stream(true, &text);
    Some(0)
}

fn failed(message: &str) -> u8 {
    write_stream(false, &format!("backup failed: {message}\n"));
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
    if name.len() < prefix.len() + suffix.len() {
        return false;
    }
    let (Some(head), Some(tail)) = (
        name.get(..prefix.len()),
        name.get(name.len() - suffix.len()..),
    ) else {
        return false;
    };
    if cfg!(windows) {
        head.eq_ignore_ascii_case(prefix) && tail.eq_ignore_ascii_case(suffix)
    } else {
        head == prefix && tail == suffix
    }
}

fn seconds(time: SystemTime) -> f64 {
    match time.duration_since(SystemTime::UNIX_EPOCH) {
        Ok(since) => since.as_secs() as f64 + f64::from(since.subsec_nanos()) * 1e-9,
        Err(before) => {
            let before = before.duration();
            -(before.as_secs() as f64 + f64::from(before.subsec_nanos()) * 1e-9)
        }
    }
}

/// `_rotate`: this tool's own files older than `keep_days`, dumps only when
/// this run produced one.
fn rotate(bdir: &Path, keep_days: f64, include_dumps: bool) -> io::Result<Vec<PathBuf>> {
    let cutoff = seconds(SystemTime::now()) - keep_days * 86400.0;
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
            let path = bdir.join(&name);
            if seconds(fs::metadata(&path)?.modified()?) < cutoff {
                fs::remove_file(&path)?;
                pruned.push(path);
            }
        }
    }
    Ok(pruned)
}
