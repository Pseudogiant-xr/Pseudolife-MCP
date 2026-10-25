//! The lite tier's embedded PostgreSQL: start or attach, guards and the
//! first-boot lock (`rust/daemon/spec-lite-storage.md`, option C).
//!
//! Oracle: `pseudolife_memory/storage/embedded_pg.py`, which drives the pg0
//! CLI. This module runs the same PostgreSQL binaries itself, so a bank
//! either side created opens on the other: the same pgdata
//! (`<data_dir>/embedded_pg`), role, password, database, initdb arguments
//! and lock file. A running server is found through its own
//! `postmaster.pid`, whichever program started it.
//!
//! Invariants carried over from the oracle:
//! * nothing here deletes or re-initializes a pgdata that holds a cluster;
//! * a pgdata initialized by another PostgreSQL major is refused;
//! * only a server this process started is ever stopped;
//! * each bank's superuser has its own secret (`SecretStore`) and SCRAM
//!   authentication, and a bank made with pg0's fixed `postgres` password
//!   moves to its secret on its next owned start (`move_under_trust`, `move_over_scram`).

use std::{
    collections::BTreeSet,
    ffi::OsString,
    fmt, fs, io,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

/// The PostgreSQL major Python lite's pinned pg0 provides (18.1.0).
pub const EXPECTED_PG_MAJOR: u32 = 18;
/// The bank's database and superuser, as Python lite names them.
pub const DATABASE: &str = "pseudolife_memory";
pub const SUPERUSER: &str = "postgres";
/// pg0's fixed default password, which banks made before 2026-10-25 still
/// carry until their next owned start moves them to their secret.
pub const LEGACY_PASSWORD: &str = "postgres";
/// pg0's default port when none is given; it moves up while one is taken.
const DEFAULT_PORT: u16 = 5432;
/// The oracle's Windows wait for the first-boot lock (`_start_lock`).
pub const START_LOCK_TIMEOUT: Duration = Duration::from_secs(120);
/// pg0 waits up to ten minutes for `pg_ctl start`.
const PG_CTL_TIMEOUT_SECS: &str = "600";

/// The server settings pg0 0.15 passes on every start (`src/main.rs`,
/// `start`), so a bank behaves the same whichever side started it. pg0
/// also passes `-F` (fsync off); this module does not (maintainer
/// decision 7, 2026-10-25: fsync on).
const SERVER_SETTINGS: &[(&str, &str)] = &[
    ("shared_buffers", "256MB"),
    ("maintenance_work_mem", "512MB"),
    ("effective_cache_size", "1GB"),
    ("max_parallel_maintenance_workers", "4"),
    ("work_mem", "64MB"),
    ("logging_collector", "on"),
    ("log_directory", "log"),
    ("log_filename", "postgresql-%Y-%m-%d.log"),
    ("log_rotation_age", "1d"),
    ("log_rotation_size", "100MB"),
    ("timezone", "UTC"),
    ("log_timezone", "UTC"),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Platform {
    Windows,
    MacOs,
    Other,
}
impl Platform {
    pub fn current() -> Self {
        if cfg!(windows) {
            Self::Windows
        } else if cfg!(target_os = "macos") {
            Self::MacOs
        } else {
            Self::Other
        }
    }
}

#[derive(Debug)]
pub enum Error {
    NonAsciiPath(PathBuf),
    UnreadablePgVersion {
        pgdata: PathBuf,
        found: String,
    },
    MajorMismatch {
        pgdata: PathBuf,
        found: String,
    },
    LockTimeout(PathBuf),
    Runtime {
        root: PathBuf,
        missing: String,
    },
    Io {
        action: &'static str,
        path: PathBuf,
        source: io::Error,
    },
    Secret {
        path: PathBuf,
        detail: String,
    },
    Command {
        program: &'static str,
        detail: String,
    },
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NonAsciiPath(pgdata) => write!(
                f,
                "embedded postgres data dir {} contains non-ASCII characters, which the \
                 embedded PostgreSQL runtime cannot handle on Windows. Set \
                 PSEUDOLIFE_MCP_DATA_DIR to an ASCII-only path (for example \
                 C:\\pseudolife-data) and restart the daemon.",
                py_repr(&pgdata.to_string_lossy())
            ),
            Self::UnreadablePgVersion { pgdata, found } => write!(
                f,
                "embedded pgdata at {} has an unreadable PG_VERSION ({}); refusing to touch it.",
                pgdata.display(),
                py_repr(found)
            ),
            Self::MajorMismatch { pgdata, found } => write!(
                f,
                "embedded pgdata at {} was initialized by PostgreSQL {found}, but the installed \
                 embedded runtime provides PostgreSQL {EXPECTED_PG_MAJOR}. Refusing to touch it \
                 — a cross-major pgdata cannot start and must never be re-initialized in place. \
                 Restore a `pseudolife-mcp backup` dump into a fresh data dir, or install the \
                 embedded runtime matching this bank.",
                pgdata.display()
            ),
            Self::LockTimeout(path) => write!(
                f,
                "timed out waiting for the embedded-postgres start lock ({}) — another process \
                 has been starting this bank for over {}s.",
                path.display(),
                START_LOCK_TIMEOUT.as_secs()
            ),
            Self::Runtime { root, missing } => write!(
                f,
                "the embedded PostgreSQL runtime at {} is incomplete: {missing} is missing. \
                 Reinstall Pseudolife to restore it.",
                root.display()
            ),
            Self::Io {
                action,
                path,
                source,
            } => {
                write!(
                    f,
                    "embedded postgres: could not {action} {}: {source}",
                    path.display()
                )
            }
            Self::Command { program, detail } => {
                write!(f, "embedded postgres: {program} failed: {detail}")
            }
            Self::Secret { path, detail } => write!(
                f,
                "embedded postgres password file {} cannot be used ({detail}). Move it aside \
                 and start again: the next start gives the bank's superuser a new password.",
                path.display()
            ),
        }
    }
}
impl std::error::Error for Error {}

fn io_error<'a>(action: &'static str, path: &'a Path) -> impl FnOnce(io::Error) -> Error + 'a {
    move |source| Error::Io {
        action,
        path: path.to_path_buf(),
        source,
    }
}

/// Python's `repr()` of a str for the refusal texts above: single quotes
/// unless the text holds one and no double quote; backslashes doubled.
fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `embedded_pg.default_lite_data_dir()`: the stable per-user data dir.
/// `env` answers nonempty values only as Python's `os.environ.get(..) or`
/// does; `home` is `Path.home()`. `None` when no home is known.
pub fn default_data_dir(
    platform: Platform,
    env: &dyn Fn(&str) -> Option<OsString>,
    home: Option<&Path>,
) -> Option<PathBuf> {
    let set = |name: &str| {
        env(name)
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
    };
    let base = match platform {
        Platform::Windows => match set("LOCALAPPDATA") {
            Some(local) => local,
            None => home?.join("AppData").join("Local"),
        },
        Platform::MacOs => home?.join("Library").join("Application Support"),
        Platform::Other => match set("XDG_DATA_HOME") {
            Some(data) => data,
            None => home?.join(".local").join("share"),
        },
    };
    Some(base.join("pseudolife-mcp"))
}

/// The pgdata of a lite data dir.
pub fn pgdata(data_dir: &Path) -> PathBuf {
    data_dir.join("embedded_pg")
}

/// `_preflight_path`: refuse a non-ASCII pgdata on Windows.
pub fn preflight_path(pgdata: &Path, platform: Platform) -> Result<(), Error> {
    if platform == Platform::Windows && !pgdata.as_os_str().to_string_lossy().is_ascii() {
        return Err(Error::NonAsciiPath(pgdata.to_path_buf()));
    }
    Ok(())
}

/// `_guard_pg_major`: refuse a pgdata another PostgreSQL major initialized.
pub fn guard_pg_major(pgdata: &Path) -> Result<(), Error> {
    let path = pgdata.join("PG_VERSION");
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(io_error("read", &path)(e)),
    };
    // Python decodes as ASCII with replacement, then strips whitespace.
    let found: String = bytes
        .iter()
        .map(|&b| if b.is_ascii() { b as char } else { '\u{FFFD}' })
        .collect::<String>()
        .trim()
        .to_owned();
    let major = found.split('.').next().unwrap_or("");
    match major.parse::<u32>() {
        Ok(EXPECTED_PG_MAJOR) => Ok(()),
        Ok(_) => Err(Error::MajorMismatch {
            pgdata: pgdata.to_path_buf(),
            found,
        }),
        Err(_) => Err(Error::UnreadablePgVersion {
            pgdata: pgdata.to_path_buf(),
            found,
        }),
    }
}

/// Where a bank's superuser password lives (`<data_dir>/embedded_pg.secret`)
/// and how it is read and created owner-only. The oracle writes it through
/// its bearer-token writer; the shim implements this with its credential
/// code.
pub trait SecretStore {
    /// The secret in `path`, or `None` when no such file exists.
    fn read(&self, path: &Path) -> Result<Option<String>, String>;
    /// Create `path` owner-only holding a fresh secret (64 lowercase hex
    /// characters from a CSPRNG) and return it.
    fn create(&self, path: &Path) -> Result<String, String>;
}

/// `_secret_path`: beside the pgdata, like the lock (initdb needs the
/// pgdata empty, and a copy of the pgdata must not carry it).
pub fn secret_path(pgdata: &Path) -> PathBuf {
    let mut name = pgdata.file_name().map(OsString::from).unwrap_or_default();
    name.push(".secret");
    pgdata.with_file_name(name)
}

/// `_cluster_secret`: this bank's superuser password, read or created, and
/// whether this call created it.
fn cluster_secret(store: &dyn SecretStore, pgdata: &Path) -> Result<(String, bool), Error> {
    let path = secret_path(pgdata);
    let fail = |detail: String| Error::Secret {
        path: path.clone(),
        detail,
    };
    let (secret, created) = match store.read(&path).map_err(fail)? {
        Some(secret) => (secret, false),
        None => (store.create(&path).map_err(fail)?, true),
    };
    // It goes into a SQL string literal and a URI: only the shape the
    // oracle writes is accepted.
    let hex = |b: u8| b.is_ascii_digit() || (b'a'..=b'f').contains(&b);
    if secret.len() != 64 || !secret.bytes().all(hex) {
        return Err(fail("it holds an unexpected value".into()));
    }
    Ok((secret, created))
}

/// pg_hba.conf methods this module manages, on its rule lines. A rule with
/// any other method (reject, peer, cert, ...) and every other line (include
/// directives, comments) are someone's deliberate edits and stay as found.
const MANAGED_METHODS: [&str; 4] = ["trust", "password", "md5", "scram-sha-256"];
const RULE_TYPES: [&str; 6] = [
    "local",
    "host",
    "hostssl",
    "hostnossl",
    "hostgssenc",
    "hostnogssenc",
];

/// `_rule_method`: index of the managed method in a rule line's fields.
fn rule_method(fields: &[&str]) -> Option<usize> {
    if !fields.first().is_some_and(|kind| RULE_TYPES.contains(kind)) {
        return None;
    }
    (3..fields.len()).find(|&i| MANAGED_METHODS.contains(&fields[i]))
}

/// `_is_loopback_rule`: a `local` rule, or a `host*` rule for the loopback
/// address only.
fn is_loopback_rule(fields: &[&str]) -> bool {
    if fields.first() == Some(&"local") {
        return true;
    }
    let Some(address) = fields.get(3) else {
        return false;
    };
    let (host, prefix) = address.split_once('/').unwrap_or((address, ""));
    if host == "localhost" && prefix.is_empty() {
        return true;
    }
    let full_mask = match host {
        "127.0.0.1" => ("32", "255.255.255.255"),
        "::1" => ("128", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"),
        _ => return false,
    };
    if !prefix.is_empty() {
        return prefix == full_mask.0;
    }
    fields
        .get(4)
        .is_some_and(|mask| mask.eq_ignore_ascii_case(full_mask.1))
}

/// `_is_marked`: this module's progress marker, a local or loopback rule on
/// trust.
pub fn is_marked(text: &str) -> bool {
    text.lines().any(|line| {
        let fields: Vec<&str> = line
            .split('#')
            .next()
            .unwrap_or("")
            .split_whitespace()
            .collect();
        rule_method(&fields).is_some_and(|i| fields[i] == "trust" && is_loopback_rule(&fields))
    })
}

/// `_managed_methods`: the managed authentication methods on pg_hba.conf's
/// rule lines.
pub fn managed_methods(text: &str) -> BTreeSet<String> {
    let mut methods = BTreeSet::new();
    for line in text.lines() {
        let fields: Vec<&str> = line
            .split('#')
            .next()
            .unwrap_or("")
            .split_whitespace()
            .collect();
        if let Some(index) = rule_method(&fields) {
            methods.insert(fields[index].to_owned());
        }
    }
    methods
}

/// `_rewrite_hba`: set every managed rule line's method (`loopback_only`:
/// only on local and loopback rules, so the trust marker never opens a rule
/// a user pointed at the network), keeping comments, columns and every
/// other byte.
pub fn rewrite_hba(text: &str, method: &str, loopback_only: bool) -> String {
    let mut out = String::with_capacity(text.len());
    for line in text.split_inclusive('\n') {
        let (rule, comment) = match line.find('#') {
            Some(at) => line.split_at(at),
            None => (line, ""),
        };
        // Tokens and the whitespace between them, in order.
        let mut pieces: Vec<(bool, &str)> = Vec::new();
        let mut rest = rule;
        while !rest.is_empty() {
            let space = rest.len() - rest.trim_start().len();
            if space > 0 {
                pieces.push((false, &rest[..space]));
                rest = &rest[space..];
            }
            let end = rest.find(char::is_whitespace).unwrap_or(rest.len());
            if end > 0 {
                pieces.push((true, &rest[..end]));
                rest = &rest[end..];
            }
        }
        let tokens: Vec<&str> = pieces.iter().filter(|(t, _)| *t).map(|(_, p)| *p).collect();
        let target = rule_method(&tokens).filter(|_| !loopback_only || is_loopback_rule(&tokens));
        let mut seen = 0;
        for (is_token, piece) in pieces {
            if is_token {
                out.push_str(if Some(seen) == target { method } else { piece });
                seen += 1;
            } else {
                out.push_str(piece);
            }
        }
        out.push_str(comment);
    }
    out
}

/// `_write_hba`: replace pg_hba.conf atomically and durably, so a crash or
/// power loss leaves the old file or the new one, never an empty one.
fn write_hba(pgdata: &Path, text: &str) -> Result<(), Error> {
    use std::io::Write;
    let target = pgdata.join("pg_hba.conf");
    let temporary = pgdata.join("pg_hba.conf.tmp");
    let mut file = fs::File::create(&temporary).map_err(io_error("write", &temporary))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        // Keep the mode initdb gave the file (0600), not the umask's.
        let mode = fs::metadata(&target)
            .map(|m| m.permissions().mode() & 0o777)
            .unwrap_or(0o600);
        file.set_permissions(fs::Permissions::from_mode(mode))
            .map_err(io_error("write", &temporary))?;
    }
    file.write_all(text.as_bytes())
        .and_then(|()| file.sync_all())
        .map_err(io_error("write", &temporary))?;
    drop(file);
    // A scanner or indexer can hold the file for a moment on Windows; the
    // oracle retries the same way (20 x 5 ms).
    let mut attempt = 0;
    loop {
        match fs::rename(&temporary, &target) {
            Ok(()) => return Ok(()),
            Err(e)
                if cfg!(windows) && e.kind() == io::ErrorKind::PermissionDenied && attempt < 19 =>
            {
                attempt += 1;
                std::thread::sleep(Duration::from_millis(5));
            }
            Err(e) => return Err(io_error("replace", &target)(e)),
        }
    }
}

#[cfg(test)]
fn scram_only(methods: &BTreeSet<String>) -> bool {
    methods.len() == 1 && methods.contains("scram-sha-256")
}

/// The cross-process first-boot lock: `<pgdata>.lock` beside the pgdata
/// (initdb needs the pgdata itself empty). The oracle locks byte 0 with
/// `msvcrt.locking` on Windows and `flock` elsewhere; `File::lock` takes
/// the whole file on Windows (which overlaps byte 0, so each side excludes
/// the other) and `flock` elsewhere. The OS drops it when the holder dies.
pub struct StartLock {
    file: fs::File,
}
impl StartLock {
    pub fn path(pgdata: &Path) -> PathBuf {
        let mut name = pgdata.file_name().map(OsString::from).unwrap_or_default();
        name.push(".lock");
        pgdata.with_file_name(name)
    }

    /// Blocks until held. Windows gives up after `timeout`, as the oracle
    /// does; elsewhere the oracle's `flock` waits without a bound.
    pub fn acquire(pgdata: &Path, timeout: Duration) -> Result<Self, Error> {
        let path = Self::path(pgdata);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(io_error("create", parent))?;
        }
        let file = fs::OpenOptions::new()
            .read(true)
            .append(true)
            .create(true)
            .open(&path)
            .map_err(io_error("open", &path))?;
        if cfg!(windows) {
            let deadline = Instant::now() + timeout;
            loop {
                match file.try_lock() {
                    Ok(()) => break,
                    Err(fs::TryLockError::WouldBlock) if Instant::now() < deadline => {
                        std::thread::sleep(Duration::from_millis(100));
                    }
                    Err(fs::TryLockError::WouldBlock) => return Err(Error::LockTimeout(path)),
                    Err(fs::TryLockError::Error(e)) => return Err(io_error("lock", &path)(e)),
                }
            }
        } else {
            file.lock().map_err(io_error("lock", &path))?;
        }
        Ok(Self { file })
    }
}
impl Drop for StartLock {
    fn drop(&mut self) {
        let _ = self.file.unlock();
    }
}

/// The PostgreSQL installation the lifecycle runs: `bin/` with initdb,
/// pg_ctl, postgres and psql, and pgvector's control file.
#[derive(Clone, Debug)]
pub struct Runtime {
    root: PathBuf,
}
impl Runtime {
    pub fn new(root: PathBuf) -> Result<Self, Error> {
        let runtime = Self { root };
        for program in ["initdb", "pg_ctl", "postgres", "psql"] {
            let path = runtime.binary(program);
            if !path.is_file() {
                return Err(Error::Runtime {
                    root: runtime.root.clone(),
                    missing: format!("bin/{}", path.file_name().unwrap().to_string_lossy()),
                });
            }
        }
        let control = runtime
            .root
            .join("share")
            .join("extension")
            .join("vector.control");
        if !control.is_file() {
            return Err(Error::Runtime {
                root: runtime.root.clone(),
                missing: "share/extension/vector.control (pgvector)".into(),
            });
        }
        Ok(runtime)
    }
    pub fn root(&self) -> &Path {
        &self.root
    }
    fn binary(&self, program: &str) -> PathBuf {
        let name = if cfg!(windows) {
            format!("{program}.exe")
        } else {
            program.to_owned()
        };
        self.root.join("bin").join(name)
    }
    /// A command for one of the runtime's programs, with no inherited
    /// libpq or server environment (a stray `PGHOST`, `PGDATA` or
    /// `PGPASSWORD` must not redirect it) and no console window on Windows.
    fn command(&self, program: &str) -> Command {
        let mut command = Command::new(self.binary(program));
        for (key, _) in std::env::vars_os() {
            if key.to_string_lossy().to_ascii_uppercase().starts_with("PG") {
                command.env_remove(&key);
            }
        }
        command.stdin(Stdio::null());
        // pg0's Linux runtime carries libxml2 and ICU 70 in `lib/` (the
        // theseus build needs them and newer hosts lack ICU 70); pg0 puts
        // that directory first on the loader path, and so does this.
        #[cfg(target_os = "linux")]
        {
            let mut paths = vec![self.root.join("lib")];
            if let Some(existing) = std::env::var_os("LD_LIBRARY_PATH") {
                paths.extend(std::env::split_paths(&existing));
            }
            if let Ok(joined) = std::env::join_paths(paths) {
                command.env("LD_LIBRARY_PATH", joined);
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        command
    }
}

/// The fields of `postmaster.pid` the lifecycle reads: the pid (line 1)
/// and the port (line 4). PostgreSQL writes it on start and removes it on
/// a clean stop.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PidFile {
    pub pid: u32,
    pub port: u16,
}
impl PidFile {
    pub fn parse(text: &str) -> Option<Self> {
        let mut lines = text.lines();
        let pid = lines.next()?.trim().parse().ok()?;
        let port = lines.nth(2)?.trim().parse().ok()?;
        Some(Self { pid, port })
    }
    fn read(pgdata: &Path) -> Result<Option<(PathBuf, Option<Self>)>, Error> {
        let path = pgdata.join("postmaster.pid");
        match fs::read(&path) {
            Ok(bytes) => Ok(Some((path, Self::parse(&String::from_utf8_lossy(&bytes))))),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(io_error("read", &path)(e)),
        }
    }
}

/// Whether `pid` is a live PostgreSQL server process. Liveness alone is not
/// enough: after a reboot the OS can hand a recorded pid to an unrelated
/// process (pg0 #37), which must be neither attached to nor stopped. A
/// process this account may not inspect counts as possibly postgres (fail
/// closed): the caller then refuses rather than treat its pgdata as free.
pub fn is_postgres_process(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    match process_image(pid) {
        Image::Name(name) => is_postgres_image(&name),
        Image::Denied => true,
        Image::Gone => false,
    }
}
fn is_postgres_image(image: &str) -> bool {
    Path::new(image.trim())
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.to_ascii_lowercase().starts_with("postgres"))
}

enum Image {
    Name(String),
    /// Alive, but this account may not read its image.
    Denied,
    Gone,
}

#[cfg(target_os = "linux")]
fn process_image(pid: u32) -> Image {
    match fs::read_to_string(format!("/proc/{pid}/comm")) {
        Ok(name) => Image::Name(name),
        Err(e) if e.kind() == io::ErrorKind::PermissionDenied => Image::Denied,
        Err(_) => Image::Gone,
    }
}

#[cfg(all(unix, not(target_os = "linux")))]
fn process_image(pid: u32) -> Image {
    // `ps` lists every account's processes.
    match Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "comm="])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
    {
        Ok(output) if output.status.success() => {
            Image::Name(String::from_utf8_lossy(&output.stdout).into_owned())
        }
        Ok(_) => Image::Gone,
        Err(_) => Image::Denied,
    }
}

/// Windows exception to the crates' safe-code rule: opens a query-only
/// handle to `pid`, reads its exit code and image path into an owned
/// buffer whose length the call bounds, and closes the handle exactly once.
#[cfg(windows)]
#[allow(unsafe_code)]
fn process_image(pid: u32) -> Image {
    use windows_sys::Win32::{
        Foundation::{CloseHandle, ERROR_ACCESS_DENIED, GetLastError, STILL_ACTIVE},
        System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
            QueryFullProcessImageNameW,
        },
    };
    // SAFETY: OpenProcess has no pointer arguments; a null result is checked.
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        // SAFETY: reads this thread's last-error value; no arguments.
        return if unsafe { GetLastError() } == ERROR_ACCESS_DENIED {
            Image::Denied
        } else {
            Image::Gone
        };
    }
    let mut exit_code = 0u32;
    let mut buffer = [0u16; 1024];
    let mut length = buffer.len() as u32;
    // SAFETY: `handle` is the live handle opened above; `exit_code`,
    // `buffer` and `length` are owned locals that outlive both calls, and
    // `length` holds the buffer's capacity in UTF-16 units on entry.
    let image = unsafe {
        if GetExitCodeProcess(handle, &mut exit_code) == 0 {
            Image::Denied
        } else if exit_code != STILL_ACTIVE as u32 {
            Image::Gone
        } else if QueryFullProcessImageNameW(handle, 0, buffer.as_mut_ptr(), &mut length) != 0 {
            Image::Name(String::from_utf16_lossy(
                &buffer[..length.min(buffer.len() as u32) as usize],
            ))
        } else {
            Image::Denied
        }
    };
    // SAFETY: closes the handle opened above, once; it is not used after.
    let _ = unsafe { CloseHandle(handle) };
    image
}

/// A running lite server and whether this process owns it.
pub struct Embedded {
    pub pgdata: PathBuf,
    pub port: u16,
    password: String,
    /// The runtime and the pid of the server this process launched.
    owned: Option<(Runtime, Option<u32>)>,
}
impl fmt::Debug for Embedded {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Embedded")
            .field("pgdata", &self.pgdata)
            .field("port", &self.port)
            .field("password", &"<redacted>")
            .field("owned", &self.owned.as_ref().map(|(_, pid)| pid))
            .finish()
    }
}
impl Embedded {
    /// The DSN Python lite exports for the same server, for a libpq or
    /// psycopg child: it requires SCRAM, so the password is never sent as
    /// text. A Rust client connecting with `password()` must refuse every
    /// authentication but SCRAM itself (tokio-postgres rejects the
    /// `require_auth` option).
    pub fn dsn(&self) -> String {
        format!(
            "postgresql://{SUPERUSER}:{}@127.0.0.1:{}/{DATABASE}?require_auth=scram-sha-256",
            self.password, self.port
        )
    }
    /// The superuser password the server accepts (for a child process's
    /// `PGPASSWORD`, which keeps it off command lines, with SCRAM required).
    pub fn password(&self) -> &str {
        &self.password
    }
    /// True when this process started the server (and so may stop it).
    pub fn owned(&self) -> bool {
        self.owned.is_some()
    }
    /// Stop the server if, and only if, this process started it and it is
    /// still that server: one that exited and was started again by another
    /// caller is theirs.
    pub fn stop(self) -> Result<(), Error> {
        let Some((runtime, launched)) = &self.owned else {
            return Ok(());
        };
        if let Some(launched) = launched {
            let current = PidFile::read(&self.pgdata)?.and_then(|(_, record)| record);
            if current.map(|record| record.pid) != Some(*launched) {
                return Ok(());
            }
        }
        let mut command = runtime.command("pg_ctl");
        command
            .arg("stop")
            .arg("-D")
            .arg(&self.pgdata)
            .args(["-m", "fast", "-w", "-t", "60"]);
        run_logged(command, "pg_ctl stop", &self.pgdata)
    }
}

/// `attach_or_start`: start, or attach to, the lite server for `data_dir`.
pub fn attach_or_start(
    data_dir: &Path,
    runtime: &Runtime,
    secrets: &dyn SecretStore,
) -> Result<Embedded, Error> {
    let pgdata = pgdata(data_dir);
    preflight_path(&pgdata, Platform::current())?;
    guard_pg_major(&pgdata)?;
    fs::create_dir_all(&pgdata).map_err(io_error("create", &pgdata))?;
    // Two daemons racing at first boot must not run initdb on one pgdata;
    // the loser waits here, then finds the winner's server and attaches.
    // Holding it also proves no other process is mid-migration.
    let _lock = StartLock::acquire(&pgdata, START_LOCK_TIMEOUT)?;
    let (secret, created) = cluster_secret(secrets, &pgdata)?;
    let mut altered = false;
    let result = attach_or_start_locked(runtime, pgdata.clone(), secret, created, &mut altered);
    if result.is_err() && created && !altered {
        // A secret this call created is unused until a role change may have
        // given it to the role; before that a failed attempt drops it (the
        // trust marker lets the next start re-key regardless), after that it
        // is kept, since the server may already require it.
        let _ = fs::remove_file(secret_path(&pgdata));
    }
    result
}

fn read_hba(pgdata: &Path) -> Result<Option<String>, Error> {
    let path = pgdata.join("pg_hba.conf");
    match fs::read_to_string(&path) {
        Ok(text) => Ok(Some(text)),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(io_error("read", &path)(e)),
    }
}

fn attach_or_start_locked(
    runtime: &Runtime,
    pgdata: PathBuf,
    secret: String,
    created: bool,
    altered: &mut bool,
) -> Result<Embedded, Error> {
    if let Some((path, record)) = PidFile::read(&pgdata)? {
        if let Some(record) = record.filter(|record| is_postgres_process(record.pid)) {
            // A server started before the move still takes the old password.
            let password = [secret.as_str(), LEGACY_PASSWORD]
                .into_iter()
                .find(|password| serves(runtime, record.port, password, &pgdata))
                .ok_or_else(|| Error::Command {
                    program: "attach",
                    detail: format!(
                        "the server running for {} (port {}, pid {}) answers to neither this \
                         bank's password nor the old fixed one (it may be busy or still \
                         starting: try again in a moment). If it persists, stop that process \
                         and start again; if no server for this bank runs, the pid now belongs \
                         to another program: remove {} and start again",
                        pgdata.display(),
                        record.port,
                        record.pid,
                        pgdata.join("postmaster.pid").display()
                    ),
                })?
                .to_owned();
            settle_running(runtime, record.port, &pgdata, &secret, &password, altered)?;
            // A starter killed after the move but before creating the
            // bank's database leaves it missing; create it here too.
            ensure_database(runtime, record.port, &secret)?;
            return Ok(Embedded {
                pgdata,
                port: record.port,
                password: secret,
                owned: None,
            });
        }
        // A pid file whose process is gone (or is no postgres) is stale;
        // PostgreSQL would refuse to start past it.
        fs::remove_file(&path).map_err(io_error("remove", &path))?;
    }
    let found_hba = read_hba(&pgdata)?;
    // A bank not yet on SCRAM (or whose secret file was just recreated)
    // starts with loopback trust, which is also the migration's progress
    // marker: a crash anywhere before the SCRAM rewrite leaves trust behind,
    // and the next start under the lock finishes the move.
    let marked = found_hba
        .as_deref()
        .is_some_and(|text| managed_methods(text).iter().any(|m| m != "scram-sha-256") || created);
    if let Some(text) = found_hba.as_deref().filter(|_| marked) {
        write_hba(&pgdata, &rewrite_hba(text, "trust", true))?;
    }
    let started = (|| {
        if !pgdata.join("PG_VERSION").exists() {
            initdb(runtime, &pgdata)?;
            *altered = true;
        }
        let port = free_port(DEFAULT_PORT);
        start(runtime, &pgdata, port)?;
        Ok(port)
    })();
    let port = match started {
        Ok(port) => port,
        Err(error) => {
            if let Some(text) = found_hba.as_deref().filter(|_| marked) {
                let _ = write_hba(&pgdata, text);
            }
            // pg_ctl can fail after postgres launched (a readiness timeout);
            // never leave that server running unowned.
            let mut stop = runtime.command("pg_ctl");
            stop.arg("stop")
                .arg("-D")
                .arg(&pgdata)
                .args(["-m", "fast", "-w", "-t", "60"]);
            let _ = run_logged(stop, "pg_ctl stop", &pgdata);
            return Err(error);
        }
    };
    let launched = PidFile::read(&pgdata)?
        .and_then(|(_, record)| record)
        .map(|record| record.pid);
    let embedded = Embedded {
        pgdata,
        port,
        password: secret,
        owned: Some((runtime.clone(), launched)),
    };
    // A fresh bank's role has the secret from initdb, and its rules are
    // SCRAM already; an existing one on the trust marker moves trust-first;
    // an unmarked existing one is asked which password the role takes (a
    // move interrupted between its SCRAM rewrite and its role change, then
    // stopped, leaves SCRAM rules with the role on the public password).
    let fresh = found_hba.is_none();
    let finished = if marked {
        move_under_trust(runtime, port, &embedded.pgdata, &embedded.password, altered)
    } else if fresh {
        Ok(())
    } else {
        match [embedded.password.as_str(), LEGACY_PASSWORD]
            .into_iter()
            .find(|password| serves(runtime, port, password, &embedded.pgdata))
        {
            Some(matched) => {
                let matched = matched.to_owned();
                settle_running(
                    runtime,
                    port,
                    &embedded.pgdata,
                    &embedded.password,
                    &matched,
                    altered,
                )
            }
            None => Err(Error::Secret {
                path: secret_path(&embedded.pgdata),
                detail: "the bank's server does not accept it".into(),
            }),
        }
    }
    .and_then(|()| ensure_database(runtime, port, &embedded.password));
    if let Err(error) = finished {
        // Never leave a server this call started running unowned (and
        // possibly on trust): stop it; the next start finishes the move.
        let _ = embedded.stop();
        return Err(error);
    }
    Ok(embedded)
}

/// `_settle_running`: finish what a dead starter left on a running server.
/// Under the start lock nobody else is migrating. A trust file on a server
/// that took the secret over SCRAM goes back to SCRAM; this module's own
/// trust marker means a move was interrupted with the server on trust:
/// finish it the trust way; a server that still takes the public old
/// password moves the SCRAM way; one that took the secret is moved.
fn settle_running(
    runtime: &Runtime,
    port: u16,
    pgdata: &Path,
    secret: &str,
    matched: &str,
    altered: &mut bool,
) -> Result<(), Error> {
    let marked = is_marked(&read_hba(pgdata)?.unwrap_or_default());
    if marked && matched == secret {
        switch_to_scram(runtime, port, pgdata, secret)?;
        wait_for_scram(runtime, port, secret)
    } else if marked {
        move_under_trust(runtime, port, pgdata, secret, altered)
    } else if matched == LEGACY_PASSWORD {
        move_over_scram(runtime, port, pgdata, secret, altered)
    } else {
        Ok(())
    }
}

/// Whether the server on `port` accepts `password` and is this pgdata's
/// own: a recorded port another PostgreSQL has since taken must not be
/// mistaken for the bank.
fn serves(runtime: &Runtime, port: u16, password: &str, pgdata: &Path) -> bool {
    let Ok(reported) = psql(runtime, port, password, "SHOW data_directory") else {
        return false;
    };
    let canonical = |path: &Path| fs::canonicalize(path).ok();
    let reported = PathBuf::from(reported.trim());
    canonical(&reported).is_some() && canonical(&reported) == canonical(pgdata)
}

/// `_move_under_trust`: the role gets the secret (idempotent; trust accepts
/// the connection whatever the role's password was, so a lost secret
/// re-keys too), then the rules move to SCRAM, the server reloads, and the
/// move waits until the server takes the secret over SCRAM.
fn move_under_trust(
    runtime: &Runtime,
    port: u16,
    pgdata: &Path,
    secret: &str,
    altered: &mut bool,
) -> Result<(), Error> {
    *altered = true;
    alter_role(runtime, port, secret)?;
    switch_to_scram(runtime, port, pgdata, secret)?;
    wait_for_scram(runtime, port, secret)
}

/// `_move_over_scram`: for a role still on the public old password, the
/// rules move to SCRAM and the server reloads first, then (unless the role
/// already takes the secret) the role gets it over SCRAM, so no state has
/// the role on the secret while the server asks for a cleartext password.
fn move_over_scram(
    runtime: &Runtime,
    port: u16,
    pgdata: &Path,
    secret: &str,
    altered: &mut bool,
) -> Result<(), Error> {
    switch_to_scram(runtime, port, pgdata, secret)?;
    if psql(runtime, port, secret, "SELECT 1").is_err() {
        *altered = true;
        // Off Windows the reload applies asynchronously, so a failure here
        // may only mean the role already holds the secret; the wait below
        // is the real check.
        let _ = alter_role(runtime, port, secret);
        wait_for_scram(runtime, port, secret)?;
    }
    Ok(())
}

/// `_alter_role`: give the role the secret as a SCRAM verifier computed
/// here, connecting with the public old password (accepted under the trust
/// marker, or by a role still on it), so the secret never reaches the server.
fn alter_role(runtime: &Runtime, port: u16, secret: &str) -> Result<(), Error> {
    let mut salt = [0u8; 16];
    getrandom::fill(&mut salt).map_err(|e| Error::Command {
        program: "getrandom",
        detail: e.to_string(),
    })?;
    let verifier = scram_verifier(secret, salt, 4096);
    // Over stdin, not psql's command line, which other accounts can read.
    let alter = format!("ALTER ROLE \"{SUPERUSER}\" PASSWORD '{verifier}'");
    psql_stdin(runtime, port, LEGACY_PASSWORD, &alter).map(drop)
}

/// `_switch_to_scram`: rewrite the managed rules to SCRAM, then reload. On
/// Windows a changed pg_hba.conf applies to new connections at once (SCRAM
/// already in force); elsewhere the loaded rules still apply. So the reload
/// tries the secret (SCRAM required), then the public password.
fn switch_to_scram(runtime: &Runtime, port: u16, pgdata: &Path, secret: &str) -> Result<(), Error> {
    let hba = read_hba(pgdata)?.unwrap_or_default();
    write_hba(pgdata, &rewrite_hba(&hba, "scram-sha-256", false))?;
    psql(runtime, port, secret, "SELECT pg_reload_conf()")
        .or_else(|_| psql(runtime, port, LEGACY_PASSWORD, "SELECT pg_reload_conf()"))
        .map(drop)
}

/// `_wait_for_scram`: off Windows, pg_reload_conf() returns before the
/// server applies the new rules.
fn wait_for_scram(runtime: &Runtime, port: u16, secret: &str) -> Result<(), Error> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match psql(runtime, port, secret, "SELECT 1") {
            Ok(_) => return Ok(()),
            Err(_) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(100)),
            Err(error) => return Err(error),
        }
    }
}

/// `_scram_verifier`: PostgreSQL's stored SCRAM-SHA-256 verifier for
/// `secret` (RFC 5802 and 7677; the secret is ASCII hex, so SASLprep leaves
/// it unchanged): `SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>`.
pub fn scram_verifier(secret: &str, salt: [u8; 16], iterations: u32) -> String {
    use hmac::{Hmac, Mac};
    use sha2::{Digest, Sha256};
    let mac = |key: &[u8], data: &[u8]| -> [u8; 32] {
        let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC takes any key length");
        mac.update(data);
        mac.finalize().into_bytes().into()
    };
    // PBKDF2-HMAC-SHA256, one 32-byte block.
    let mut block = salt.to_vec();
    block.extend_from_slice(&1u32.to_be_bytes());
    let mut u = mac(secret.as_bytes(), &block);
    let mut salted = u;
    for _ in 1..iterations {
        u = mac(secret.as_bytes(), &u);
        for (s, b) in salted.iter_mut().zip(u) {
            *s ^= b;
        }
    }
    let client_key = mac(&salted, b"Client Key");
    let stored_key: [u8; 32] = Sha256::digest(client_key).into();
    let server_key = mac(&salted, b"Server Key");
    format!(
        "SCRAM-SHA-256${iterations}:{}${}:{}",
        base64(&salt),
        base64(&stored_key),
        base64(&server_key)
    )
}

/// Standard base64 with padding.
fn base64(data: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let n = (chunk[0] as u32) << 16
            | (*chunk.get(1).unwrap_or(&0) as u32) << 8
            | *chunk.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(TABLE[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// pg0's initdb, with the bank's secret and SCRAM from the start: superuser
/// `postgres`, UTF8, and pg0's locale per OS (the C locale on Windows since
/// pg0 0.15.2, which keeps localized locale names from failing initdb; the
/// system locale elsewhere). The password file is the bank's owner-only
/// secret file itself, so no other copy of the secret is ever written. The
/// oracle reaches the same pg_hba.conf through password-then-SCRAM.
fn initdb(runtime: &Runtime, pgdata: &Path) -> Result<(), Error> {
    let mut command = runtime.command("initdb");
    command
        .arg("-D")
        .arg(pgdata)
        .args(["-U", SUPERUSER, "--auth=scram-sha-256", "--encoding=UTF8"])
        .arg(format!("--pwfile={}", secret_path(pgdata).display()));
    if cfg!(windows) {
        command.arg("--locale=C");
    }
    run_logged(command, "initdb", pgdata)
}

fn start(runtime: &Runtime, pgdata: &Path, port: u16) -> Result<(), Error> {
    let mut options = format!("-p {port}");
    for (key, value) in SERVER_SETTINGS {
        options.push_str(&format!(" -c {key}={value}"));
    }
    let log = pgdata.with_file_name("embedded_pg.start.log");
    let mut command = runtime.command("pg_ctl");
    command
        .arg("start")
        .arg("-D")
        .arg(pgdata)
        .arg("-l")
        .arg(&log)
        .args(["-w", "-t", PG_CTL_TIMEOUT_SECS, "-o", &options]);
    run_logged(command, "pg_ctl start", pgdata)
}

/// Create the bank's database when it is missing (pg0 checks first rather
/// than parsing the localized duplicate-database error, pg0 #13).
fn ensure_database(runtime: &Runtime, port: u16, password: &str) -> Result<(), Error> {
    let exists = psql(
        runtime,
        port,
        password,
        &format!("SELECT 1 FROM pg_database WHERE datname = '{DATABASE}'"),
    )?;
    if exists.trim() != "1" {
        psql(
            runtime,
            port,
            password,
            &format!("CREATE DATABASE \"{DATABASE}\""),
        )?;
    }
    Ok(())
}

/// psql against the server's `postgres` database. The password goes in
/// `PGPASSWORD`, never on the command line.
fn psql(runtime: &Runtime, port: u16, password: &str, sql: &str) -> Result<String, Error> {
    let output = psql_command(runtime, port, password)
        .args(["-c", sql])
        .output()
        .map_err(|e| Error::Command {
            program: "psql",
            detail: e.to_string(),
        })?;
    psql_result(output)
}

/// `psql`, reading the statement from stdin (for one that carries a
/// credential, which must stay off the command line).
fn psql_stdin(runtime: &Runtime, port: u16, password: &str, sql: &str) -> Result<String, Error> {
    use std::io::Write;
    let fail = |e: io::Error| Error::Command {
        program: "psql",
        detail: e.to_string(),
    };
    let mut child = psql_command(runtime, port, password)
        .args(["-f", "-"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(fail)?;
    let written = child
        .stdin
        .take()
        .map(|mut stdin| stdin.write_all(sql.as_bytes()))
        .unwrap_or(Ok(()));
    let output = child.wait_with_output().map_err(fail)?;
    written.map_err(fail)?;
    psql_result(output)
}

fn psql_command(runtime: &Runtime, port: u16, password: &str) -> Command {
    let mut command = runtime.command("psql");
    command
        .args(["-X", "-w", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1"])
        .args([
            "-h",
            "127.0.0.1",
            "-p",
            &port.to_string(),
            "-U",
            SUPERUSER,
            "-d",
            "postgres",
        ])
        .env("PGPASSWORD", password)
        // Bounded: a hung port or a blocked statement must not hang a start.
        .env("PGCONNECT_TIMEOUT", "10")
        .env("PGOPTIONS", "-c statement_timeout=60000")
        .env(
            // The public old password may go as text; anything else is
            // offered over SCRAM only, so a server (or an impostor on the
            // port) asking for a cleartext password gets nothing.
            "PGREQUIREAUTH",
            if password == LEGACY_PASSWORD {
                "password,md5,scram-sha-256,none"
            } else {
                "scram-sha-256"
            },
        );
    command
}

fn psql_result(output: std::process::Output) -> Result<String, Error> {
    if !output.status.success() {
        return Err(Error::Command {
            program: "psql",
            detail: String::from_utf8_lossy(&output.stderr).trim().to_owned(),
        });
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Runs a runtime program with its output in files, not pipes: on Windows
/// the server pg_ctl leaves running inherits pg_ctl's handles, so a pipe
/// would never reach end-of-file (the pg0 wrapper routes through files for
/// the same reason).
fn run_logged(mut command: Command, program: &'static str, pgdata: &Path) -> Result<(), Error> {
    let out = pgdata.with_file_name(format!(
        "embedded_pg.{}-{}.out",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let file = fs::File::create(&out).map_err(io_error("create", &out))?;
    let err = file.try_clone().map_err(io_error("open", &out))?;
    let status = command
        .stdout(file)
        .stderr(err)
        .status()
        .map_err(|e| Error::Command {
            program,
            detail: e.to_string(),
        });
    let text = fs::read(&out).map(|b| String::from_utf8_lossy(&b).trim().to_owned());
    let _ = fs::remove_file(&out);
    match status? {
        status if status.success() => Ok(()),
        status => Err(Error::Command {
            program,
            detail: format!("{status}: {}", text.unwrap_or_default()),
        }),
    }
}

/// pg0's port choice: the default when it is free on loopback, otherwise
/// the next free one up (wrapping into the dynamic range near the top).
fn free_port(start: u16) -> u16 {
    let available = |port: u16| std::net::TcpListener::bind(("127.0.0.1", port)).is_ok();
    let mut port = start;
    while !available(port) {
        port = if port >= 65535 - 100 { 49152 } else { port + 1 };
    }
    port
}

#[cfg(test)]
#[path = "embedded_pg_tests.rs"]
pub(crate) mod tests;
