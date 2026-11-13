//! The bank's selection and restore's checks before it changes anything:
//! which containers, database, role and port; the daemon's image, `/data`
//! mount and health URL; whether the database exists; the safety directory;
//! and the free-space floor everywhere restore writes. Every Docker call
//! here only reads, except the short-lived helper containers that measure
//! free space where no running container can (removed before returning).
//! The outcome is a [`Plan`] for `execute`, or a refusal naming its cause
//! and the one fix.
use std::{path::Path, time::Duration};

use serde_json::Value;

use super::{
    args::{Args, python_int},
    conn::{self, Resolved, truthy},
    docker::{Docker, Ran, ascii_text},
    dump::python_space,
    pathlib,
};

pub(super) const PG_CONTAINER: &str = "pseudolife-mcp-postgres";
pub(super) const DAEMON_CONTAINER: &str = "pseudolife-mcp-daemon";
const MIN_FREE: u128 = 8 * 1024 * 1024 * 1024;
pub(super) const MINUTE: Duration = Duration::from_secs(60);
pub(super) const CLEANUP: Duration = Duration::from_secs(120);
/// A step that reads or writes the whole bank (dump, copy, replay, tar):
/// a large bank on a slow disk can pass the hour Python allowed.
pub(super) const BULK: Duration = Duration::from_secs(6 * 3600);

/// `restore failed: <message>`, exit `code`.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct Stop(pub String, pub u8);

pub(super) type Step<T> = Result<T, Stop>;

pub(super) fn fail<T>(message: impl Into<String>, code: u8) -> Step<T> {
    Err(Stop(message.into(), code))
}

/// Everything `execute` needs, resolved and checked.
#[derive(Debug, Clone)]
pub(super) struct Plan {
    pub pg: String,
    pub daemon: String,
    pub user: String,
    pub db: String,
    pub port: u16,
    pub image: String,
    /// The health endpoint; empty only with `--leave-stopped`.
    pub daemon_url: String,
    pub was_running: bool,
    /// Whether the destination database exists (and goes into the safety
    /// backup).
    pub exists: bool,
    /// The resolved safety directory.
    pub safety_dir: String,
    /// The daemon's `/data` mount and any mounts below it.
    pub state_mounts: Vec<String>,
    /// PostgreSQL's writable mounts (and `/`) whose free space is checked.
    pub pg_paths: Vec<String>,
    /// Helper containers an earlier, killed restore left behind.
    pub stale_helpers: Vec<String>,
}

pub(super) struct Bank<'a, D: Docker> {
    pub docker: &'a mut D,
    pub pg: String,
    pub daemon: String,
}

/// `dict.get(key)` on a JSON object.
pub(super) fn get<'v>(value: &'v Value, key: &str) -> Option<&'v Value> {
    value.as_object().and_then(|map| map.get(key))
}

/// The output of `docker inspect` is not the shape Docker writes.
fn malformed(container: &str) -> Stop {
    Stop(
        format!("cannot inspect {container}; restore its Docker container configuration and retry"),
        2,
    )
}

/// `value[key]`, which must be an object when present.
fn sub_dict<'v>(value: &'v Value, key: &str, container: &str) -> Step<Option<&'v Value>> {
    match get(value, key) {
        None | Some(Value::Null) => Ok(None),
        Some(inner @ Value::Object(_)) => Ok(Some(inner)),
        Some(_) => Err(malformed(container)),
    }
}

/// A list field (`Mounts`): absent or null is empty.
fn list<'v>(value: &'v Value, key: &str, container: &str) -> Step<&'v [Value]> {
    match get(value, key) {
        None | Some(Value::Null) => Ok(&[]),
        Some(Value::Array(items)) => Ok(items),
        Some(_) => Err(malformed(container)),
    }
}

/// A string field of a mount, absent as "".
fn text_field<'v>(mount: &'v Value, key: &str, container: &str) -> Step<&'v str> {
    match get(mount, key) {
        None | Some(Value::Null) => Ok(""),
        Some(Value::String(text)) => Ok(text),
        Some(_) => Err(malformed(container)),
    }
}

/// `str.split()` on ASCII text.
fn split_ascii(text: &str) -> Vec<&str> {
    text.split(python_space)
        .filter(|word| !word.is_empty())
        .collect()
}

/// The last line's fourth field of `df -Pk` output, in bytes.
pub(super) fn available(output: &[u8], location: &str) -> Step<u128> {
    let refuse = || {
        Stop(
            format!(
                "cannot read free space on {location}; make `df -Pk` work in the container and retry. Nothing was changed"
            ),
            2,
        )
    };
    let text = ascii_text(output).ok_or_else(refuse)?;
    let last = text
        .split('\n')
        .rfind(|line| !line.is_empty())
        .ok_or_else(refuse)?;
    let field = *split_ascii(last).get(3).ok_or_else(refuse)?;
    let free = python_int(field).map_err(|()| refuse())?;
    if free < 0 || free > i128::from(u64::MAX / 1024) {
        return Err(refuse());
    }
    Ok(free as u128 * 1024)
}

/// At least 8 GiB free at `location`.
pub(super) fn require(free: u128, location: &str) -> Step<()> {
    if free < MIN_FREE {
        return fail(
            format!(
                "less than 8 GiB is free on {location}; free at least 8 GiB there, then retry. Nothing was changed"
            ),
            2,
        );
    }
    Ok(())
}

/// What Docker's own error text says about a failed `inspect`.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum Absence {
    /// The program could not be started.
    NoProgram,
    /// The engine does not answer.
    EngineDown,
    /// The engine answers and has no such container.
    NoContainer,
    Other,
}

pub(super) fn absence(ran: &Ran) -> Absence {
    if ran.failed && !ran.interrupted {
        return Absence::NoProgram;
    }
    let text = String::from_utf8_lossy(&ran.stderr).to_lowercase();
    if text.contains("no such object") || text.contains("no such container") {
        Absence::NoContainer
    } else if text.contains("cannot connect to the docker daemon")
        || text.contains("error during connect")
        || text.contains("is the docker daemon running")
    {
        Absence::EngineDown
    } else {
        Absence::Other
    }
}

impl<D: Docker> Bank<'_, D> {
    pub fn run(&mut self, args: &[&str], timeout: Duration) -> Ran {
        let args: Vec<String> = args.iter().map(|s| (*s).to_owned()).collect();
        self.docker.run(&args, timeout)
    }

    /// A cleanup command (removing a helper): it runs to its end even
    /// after an interrupt.
    pub fn quiet(&mut self, args: &[&str]) -> Ran {
        let interruptible = self.docker.is_interruptible();
        self.docker.set_interruptible(false);
        let ran = self.run(args, CLEANUP);
        self.docker.set_interruptible(interruptible);
        ran
    }

    /// `docker inspect <container>`: its one object.
    pub fn inspect(&mut self, container: &str) -> Step<Value> {
        let ran = self.run(&["inspect", container], MINUTE);
        if !ran.ok() {
            return Err(match absence(&ran) {
                Absence::NoProgram => Stop(
                    "the docker program could not be started; check that Docker is installed and PSEUDOLIFE_DOCKER (if set) names it, then retry. Nothing was restored".into(),
                    2,
                ),
                Absence::EngineDown => Stop(
                    "the Docker engine is not answering; start Docker (Docker Desktop on Windows and macOS), then retry. Nothing was restored".into(),
                    2,
                ),
                Absence::NoContainer => Stop(
                    format!(
                        "the container {container} does not exist; start the Docker bank (docker compose up -d in its install directory) or name its containers with --container <postgres> --daemon-container <daemon>, then retry. Nothing was restored"
                    ),
                    2,
                ),
                Absence::Other => Stop(
                    format!(
                        "Docker inspect failed for the backup/restore; check the containers with docker logs {} and docker logs {}, then retry",
                        self.pg, self.daemon
                    ),
                    1,
                ),
            });
        }
        let value: Value = serde_json::from_slice(&ran.stdout).map_err(|_| malformed(container))?;
        match value {
            Value::Array(items) => match items.into_iter().next() {
                Some(item @ Value::Object(_)) => Ok(item),
                _ => Err(malformed(container)),
            },
            _ => Err(malformed(container)),
        }
    }

    /// At least 8 GiB free at `path` inside PostgreSQL's container.
    pub fn container_space(&mut self, path: &str) -> Step<()> {
        let pg = self.pg.clone();
        let location = format!("{pg}:{path}");
        let ran = self.run(&["exec", &pg, "df", "-Pk", path], MINUTE);
        if !ran.ok() {
            return fail(
                format!(
                    "cannot check free space on {location}; run `docker exec {pg} df -Pk {path}` successfully, then retry. Nothing was changed"
                ),
                2,
            );
        }
        require(available(&ran.stdout, &location)?, &location)
    }

    /// Docker's storage: `df /` in a running bank container, else in a
    /// helper created from the daemon image.
    pub fn storage_space(&mut self, image: &str, nonce: &str) -> Step<()> {
        for name in [self.pg.clone(), self.daemon.clone()] {
            let ran = self.run(&["exec", &name, "df", "-Pk", "/"], MINUTE);
            if ran.ok() {
                let free = available(&ran.stdout, &format!("Docker storage ({name})"))?;
                return require(free, "Docker storage");
            }
        }
        let helper = format!("pseudolife-diskcheck-{nonce}");
        let created = self.run(
            &[
                "create",
                "--name",
                &helper,
                "--read-only",
                "--network",
                "none",
                "--log-driver",
                "none",
                "--entrypoint",
                "df",
                image,
                "-Pk",
                "/",
            ],
            MINUTE,
        );
        let result = if !created.ok() {
            fail(
                "cannot check Docker storage space; restore Docker's ability to create a container from the current daemon image and retry. Nothing was changed",
                2,
            )
        } else {
            let ran = self.run(&["start", "-a", &helper], MINUTE);
            if ran.ok() {
                available(&ran.stdout, "Docker storage")
                    .and_then(|free| require(free, "Docker storage"))
            } else {
                fail(
                    "cannot check Docker storage space; make `df -Pk /` work in the current daemon image and retry. Nothing was changed",
                    2,
                )
            }
        };
        let removed = self.quiet(&["rm", "-f", "-v", &helper]);
        if created.ok() && !removed.ok() && result.is_ok() {
            return fail(
                format!(
                    "the disk-check helper could not be removed; run `docker rm -f -v {helper}` and retry. Nothing was changed"
                ),
                2,
            );
        }
        result
    }

    /// A helper container over the daemon's volumes (read-only unless
    /// `writable`), running `script` as root with no network: its id.
    pub fn helper(&mut self, image: &str, script: &str, writable: bool) -> Step<String> {
        let label = helper_label(&self.daemon);
        let volumes = if writable {
            self.daemon.clone()
        } else {
            format!("{}:ro", self.daemon)
        };
        let ran = self.run(
            &[
                "create",
                "--network",
                "none",
                "--user",
                "0",
                "--volumes-from",
                &volumes,
                "--log-driver",
                "none",
                "--label",
                &label,
                "--entrypoint",
                "sh",
                image,
                "-c",
                script,
            ],
            MINUTE,
        );
        let id = String::from_utf8_lossy(&ran.stdout).trim().to_owned();
        if !ran.ok() || id.is_empty() || id.contains(char::is_whitespace) {
            if !id.is_empty() && !id.contains(char::is_whitespace) {
                self.quiet(&["rm", "-f", &id]);
            }
            return fail(
                format!(
                    "Docker could not create a helper container from the daemon image {image}; check `docker images` and Docker's free space, then retry"
                ),
                1,
            );
        }
        Ok(id)
    }

    /// The psql argv for one statement or file, as root of the container's
    /// socket: inherited libpq address or service settings never redirect it.
    pub fn psql_argv(&self, user: &str, port: u16, database: &str, args: &[&str]) -> Vec<String> {
        let mut argv: Vec<String> = [
            "exec",
            &self.pg,
            "env",
            "-u",
            "PGHOSTADDR",
            "-u",
            "PGSERVICE",
            "-u",
            "PGSERVICEFILE",
            "psql",
            "-X",
            "-h",
            "/var/run/postgresql",
            "-p",
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
        argv.push(port.to_string());
        argv.extend(["-U".to_owned(), user.to_owned(), "-d".to_owned()]);
        argv.push(conn::database_arg(database));
        argv.extend(args.iter().map(|s| (*s).to_owned()));
        argv
    }

    /// A failed psql: a missing or refused socket names `--db-port`.
    pub fn psql_failure(&self, ran: &Ran, port: u16) -> Stop {
        if ran.interrupted {
            return Stop("interrupted".into(), 130);
        }
        if ran.code == 2 {
            let reason =
                String::from_utf8_lossy(&[ran.stdout.clone(), ran.stderr.clone()].concat())
                    .to_lowercase();
            if reason.contains("socket")
                && (reason.contains("no such file") || reason.contains("connection refused"))
            {
                return Stop(
                    format!(
                        "PostgreSQL's local socket is unavailable on port {port}; name the selected container's server port with --db-port <port> and retry"
                    ),
                    2,
                );
            }
        }
        Stop(
            format!(
                "PostgreSQL failed inside {pg}; check docker logs {pg} before retrying",
                pg = self.pg
            ),
            1,
        )
    }

    pub fn psql(&mut self, user: &str, port: u16, database: &str, args: &[&str]) -> Step<String> {
        let argv = self.psql_argv(user, port, database, args);
        let ran = self.docker.run(&argv, BULK);
        if !ran.ok() {
            return Err(self.psql_failure(&ran, port));
        }
        Ok(String::from_utf8_lossy(&ran.stdout).replace("\r\n", "\n"))
    }

    /// Free space in PostgreSQL's container: `/tmp`, then `/` and every
    /// writable mount (a socket mount only when it holds more than its
    /// socket), sorted.
    pub fn pg_space(&mut self, paths: &[String]) -> Step<()> {
        self.container_space("/tmp")?;
        for path in paths {
            self.container_space(path)?;
        }
        Ok(())
    }

    /// The daemon's state mounts, measured in a read-only helper.
    pub fn state_space(&mut self, image: &str, mounts: &[String]) -> Step<()> {
        let script = mounts
            .iter()
            .map(|path| format!("LC_ALL=C df -Pk -- {}", sh_quote(path)))
            .collect::<Vec<_>>()
            .join(" && ");
        let helper = self.helper(image, &script, false)?;
        let ran = self.run(&["start", "-a", &helper], MINUTE);
        self.quiet(&["rm", "-f", &helper]);
        let text = ascii_text(&ran.stdout).unwrap_or_default();
        let rows: Vec<&str> = text
            .split('\n')
            .filter(|line| !line.is_empty() && !line.starts_with("Filesystem"))
            .collect();
        if !ran.ok() || rows.len() != mounts.len() {
            return fail(
                "cannot read free space on the state mounts; make df -Pk work in the daemon image and retry. Nothing was restored",
                2,
            );
        }
        for (path, row) in mounts.iter().zip(rows) {
            let location = format!("{}:{path}", self.daemon);
            require(available(row.as_bytes(), &location)?, &location)?;
        }
        Ok(())
    }
}

/// The label on every helper restore creates for `daemon`'s volumes, so a
/// later run can remove one a killed run left behind.
pub(super) fn helper_label(daemon: &str) -> String {
    format!("pseudolife.restore-helper={daemon}")
}

/// A POSIX shell word.
pub(super) fn sh_quote(text: &str) -> String {
    if !text.is_empty()
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"@%+=:,./-_".contains(&b))
    {
        return text.to_owned();
    }
    format!("'{}'", text.replace('\'', r"'\''"))
}

const RESOLVE: &str = "the daemon's database connection cannot be resolved safely; set a direct PSEUDOLIFE_MCP_DATABASE_URL on a network shared with the selected PostgreSQL container, or name the actual bank with --container <postgres> --db <database> --user <role> --db-port <port>. Nothing was restored";
const PORT: &str = "the database port is ambiguous; name the selected container's server port with --db-port <port>. Nothing was restored";
const NO_MOUNT: &str = "the daemon has no reusable /data mount or local image; restore its Docker container configuration before backing up or restoring";
const NO_HEALTH: &str = "the daemon has no published health port; name its endpoint with --daemon-url or use --leave-stopped. Nothing was restored";
const EXISTS: &str = "cannot determine whether the destination database exists; verify PostgreSQL and retry. Nothing was restored";
const IN_MOUNT: &str = "the safety backup would be inside the state mount being replaced; choose --safety-dir <directory outside the state mount>. Nothing was restored";
const SOCKET_MOUNT: &str = "cannot inspect the PostgreSQL socket mount; make find work in the selected PostgreSQL container and retry. Nothing was restored";
const SELECTOR: &str = "the database selector is invalid; name its literal database with --db and retry. Nothing was restored";
/// A socket mount holding anything but sockets and their lock files.
const FIND: &str = r#"test -d "$1" && find -H "$1" -mindepth 1 -maxdepth 1 ! -type s ! \( -type f -name ".s.PGSQL.*.lock" \) -print -quit"#;

fn nonempty(value: &Option<String>) -> Option<&str> {
    value.as_deref().filter(|v| !v.is_empty())
}

/// The home directory for the default safety directory.
fn home() -> Step<String> {
    for key in ["USERPROFILE", "HOME"] {
        if let Ok(value) = std::env::var(key)
            && !value.is_empty()
        {
            return Ok(value);
        }
    }
    fail(
        "cannot find your home directory for the safety backup; set HOME (USERPROFILE on Windows) or pass --safety-dir <directory outside a checkout>, then retry. Nothing was restored",
        2,
    )
}

/// The Windows Docker Desktop host-path forms of a mount source.
fn desktop_source(source: &str) -> String {
    if !cfg!(windows) {
        return source.to_owned();
    }
    for prefix in ["/run/desktop/mnt/host/", "/host_mnt/"] {
        if let Some(rest) = source.strip_prefix(prefix) {
            let mut chars = rest.chars();
            if let (Some(drive), Some('/')) = (chars.next(), chars.next())
                && drive.is_ascii_alphabetic()
            {
                let tail = chars.as_str();
                if !tail.contains('\n') {
                    return format!("{drive}:/{tail}");
                }
            }
        }
    }
    source.to_owned()
}

/// The free bytes on the filesystem holding `path`.
fn free_space(path: &str) -> Option<u128> {
    #[cfg(unix)]
    {
        let stats = rustix::fs::statvfs(path).ok()?;
        Some(u128::from(stats.f_bavail) * u128::from(stats.f_frsize))
    }
    #[cfg(windows)]
    #[allow(unsafe_code)]
    {
        use std::os::windows::ffi::OsStrExt;
        let wide: Vec<u16> = std::ffi::OsStr::new(path)
            .encode_wide()
            .chain([0])
            .collect();
        let (mut caller, mut total, mut free) = (0u64, 0u64, 0u64);
        // SAFETY: a NUL-terminated path and three valid output pointers.
        let ok = unsafe {
            windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW(
                wide.as_ptr(),
                &mut caller,
                &mut total,
                &mut free,
            )
        };
        (ok != 0).then_some(u128::from(caller))
    }
}

fn unresolvable(path: &str) -> Stop {
    Stop(
        format!(
            "cannot resolve the path {path}; make it and its parents readable (no symlink loop), or choose another --safety-dir, then retry. Nothing was restored"
        ),
        2,
    )
}

/// At least 8 GiB free where the safety backup is written (its nearest
/// existing parent).
pub(super) fn host_space(directory: &str) -> Step<()> {
    let mut target =
        pathlib::resolve(Path::new(directory)).ok_or_else(|| unresolvable(directory))?;
    loop {
        if pathlib::exists(&target).ok_or_else(|| unresolvable(&target))? {
            break;
        }
        match Path::new(&target).parent() {
            Some(parent) if !parent.as_os_str().is_empty() => {
                target = parent.to_string_lossy().into_owned();
            }
            _ => break,
        }
    }
    let Some(free) = free_space(&target) else {
        return fail(
            format!(
                "cannot check free space for {directory}; restore access to that filesystem and retry before updating or backing up. Nothing was changed"
            ),
            2,
        );
    };
    require(free, directory)
}

/// The bank's selection and restore's checks, in order.
pub(super) fn probe<D: Docker>(
    docker: &mut D,
    args: &Args,
    nonce: &str,
    host_space: &mut dyn FnMut(&str) -> Step<()>,
) -> Step<Plan> {
    let mut bank = Bank {
        docker,
        pg: nonempty(&args.container).unwrap_or(PG_CONTAINER).to_owned(),
        daemon: nonempty(&args.daemon_container)
            .unwrap_or(DAEMON_CONTAINER)
            .to_owned(),
    };
    let daemon_name = bank.daemon.clone();
    let pg_name = bank.pg.clone();
    let running = bank.inspect(&daemon_name)?;
    let pg = bank.inspect(&pg_name)?;
    let explicit = nonempty(&args.container).is_some()
        && nonempty(&args.db).is_some()
        && nonempty(&args.user).is_some()
        && args.db_port.is_some();
    let (connection_user, connection_db, connection_port) = if explicit {
        (String::new(), String::new(), 0)
    } else {
        match conn::resolve(&running, &pg) {
            Resolved::Ok { user, db, port } => (user, db, port),
            Resolved::Fail => return fail(RESOLVE, 2),
        }
    };
    let user = nonempty(&args.user).map_or(connection_user, str::to_owned);
    let db = nonempty(&args.db).map_or(connection_db, str::to_owned);
    let port = args.db_port.unwrap_or(connection_port);
    if !(1..=65535).contains(&port) {
        return fail(PORT, 2);
    }
    let port = port as u16;
    if db.contains('\0') || user.contains('\0') || db.is_empty() || user.is_empty() {
        return fail(SELECTOR, 2);
    }
    let image = match get(&running, "Image") {
        Some(Value::String(image)) if !image.is_empty() => image.clone(),
        _ => return fail(NO_MOUNT, 2),
    };
    let mounts = list(&running, "Mounts", &daemon_name)?;
    let mut state_mounts = Vec::new();
    for mount in mounts {
        if !mount.is_object() {
            return Err(malformed(&daemon_name));
        }
        let destination = text_field(mount, "Destination", &daemon_name)?;
        if destination == "/data" || destination.starts_with("/data/") {
            state_mounts.push(destination.to_owned());
        }
    }
    if !state_mounts.iter().any(|m| m == "/data") {
        return fail(NO_MOUNT, 2);
    }
    state_mounts.sort();
    state_mounts.dedup();
    let pg_running = match sub_dict(&pg, "State", &pg_name)? {
        Some(state) => get(state, "Running").is_some_and(truthy),
        None => false,
    };
    if !pg_running {
        return fail(
            format!(
                "PostgreSQL is stopped; run docker start {pg_name} and retry. Nothing was restored"
            ),
            2,
        );
    }
    let daemon_url = daemon_url(&running, args.daemon_url.as_deref(), &daemon_name)?;
    if !args.leave_stopped && daemon_url.is_empty() {
        return fail(NO_HEALTH, 2);
    }
    if !args.leave_stopped && !daemon_url.starts_with("http://") {
        return fail(
            format!(
                "restore checks the daemon's readiness over plain HTTP, and {daemon_url} is not an http:// URL; name its http:// endpoint with --daemon-url or use --leave-stopped. Nothing was restored"
            ),
            2,
        );
    }
    let literal = format!("'{}'", db.replace('\'', "''"));
    let query = format!("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = {literal})");
    let answer = bank.psql(&user, port, "postgres", &["-Atc", &query])?;
    let exists = match answer.trim_matches(python_space) {
        "t" => true,
        "f" => false,
        _ => return fail(EXISTS, 2),
    };
    let safety = match &args.safety_dir {
        Some(path) => path.clone(),
        None => super::super::backup::args::pathlib(
            &Path::new(&home()?).join(".pseudolife-mcp").join("backups"),
        ),
    };
    let shown = safety.to_string_lossy().into_owned();
    let safety = pathlib::resolve(&safety).ok_or_else(|| unresolvable(&shown))?;
    for parent in pathlib::self_and_parents(&safety) {
        let git = pathlib::join(&parent, ".git");
        if pathlib::exists(&git).ok_or_else(|| unresolvable(&git))? {
            return fail(
                format!(
                    "the safety backup directory {safety} is inside a checkout; choose --safety-dir <directory outside a checkout>. Nothing was restored"
                ),
                2,
            );
        }
    }
    for mount in mounts {
        let destination = text_field(mount, "Destination", &daemon_name)?;
        if destination != "/data" && !destination.starts_with("/data/") {
            continue;
        }
        let source = match get(mount, "Source") {
            Some(Value::String(source)) if !source.is_empty() => desktop_source(source),
            None | Some(Value::Null) | Some(Value::String(_)) => continue,
            Some(_) => return Err(malformed(&daemon_name)),
        };
        let source = super::super::backup::args::pathlib(Path::new(&source));
        if source.is_absolute() {
            let shown = source.to_string_lossy().into_owned();
            let resolved = pathlib::resolve(&source).ok_or_else(|| unresolvable(&shown))?;
            if pathlib::is_relative_to(&safety, &resolved) {
                return fail(IN_MOUNT, 2);
            }
        }
    }
    host_space(&safety)?;
    bank.storage_space(&image, nonce)?;
    bank.container_space("/tmp")?;
    let postgres = bank.inspect(&pg_name)?;
    let mut pg_paths = vec!["/".to_owned()];
    for mount in list(&postgres, "Mounts", &pg_name)? {
        let Some(map) = mount.as_object() else {
            return Err(malformed(&pg_name));
        };
        if !map.get("RW").is_none_or(truthy) {
            continue;
        }
        let path = text_field(mount, "Destination", &pg_name)?;
        if !path.starts_with('/') {
            continue;
        }
        if path == "/run/postgresql" || path == "/var/run/postgresql" {
            let ran = bank.run(&["exec", &pg_name, "sh", "-c", FIND, "_", path], MINUTE);
            if !ran.ok() {
                return fail(SOCKET_MOUNT, 2);
            }
            if String::from_utf8_lossy(&ran.stdout).trim().is_empty() {
                continue;
            }
        }
        if !pg_paths.iter().any(|known| known == path) {
            pg_paths.push(path.to_owned());
        }
    }
    pg_paths.sort();
    for path in &pg_paths {
        bank.container_space(path)?;
    }
    if args.state_archive.is_some() {
        bank.state_space(&image, &state_mounts)?;
    }
    let was_running = match sub_dict(&running, "State", &daemon_name)? {
        Some(state) => get(state, "Running").is_some_and(truthy),
        None => false,
    };
    let filter = format!("label={}", helper_label(&daemon_name));
    let listed = bank.run(&["ps", "-aq", "--filter", &filter], MINUTE);
    if !listed.ok() {
        return fail(
            format!(
                "cannot list earlier restore helpers; run `docker ps -aq --filter {filter}` successfully, then retry. Nothing was restored"
            ),
            2,
        );
    }
    let stale_helpers = String::from_utf8_lossy(&listed.stdout)
        .split_whitespace()
        .map(str::to_owned)
        .collect();
    Ok(Plan {
        pg: pg_name,
        daemon: daemon_name,
        user,
        db,
        port,
        image,
        daemon_url,
        was_running,
        exists,
        safety_dir: safety,
        state_mounts,
        pg_paths,
        stale_helpers,
    })
}

/// The health URL: the option, else the daemon's published 8765/tcp
/// binding; "" when neither names one (or the binding is unreadable).
fn daemon_url(running: &Value, option: Option<&str>, container: &str) -> Step<String> {
    if let Some(option) = option.filter(|o| !o.is_empty()) {
        return Ok(option.to_owned());
    }
    let ports = |outer: &str, inner: &str| -> Step<Option<Value>> {
        let Some(section) = sub_dict(running, outer, container)? else {
            return Ok(None);
        };
        match get(section, inner) {
            Some(value @ Value::Object(_)) => Ok(Some(value.clone())),
            _ => Ok(None),
        }
    };
    let host_ports = ports("HostConfig", "PortBindings")?;
    let network_ports = ports("NetworkSettings", "Ports")?;
    let pick = |section: &Option<Value>| {
        section
            .as_ref()
            .and_then(|s| get(s, "8765/tcp"))
            .filter(|v| truthy(v))
            .cloned()
    };
    let Some(bindings) = pick(&host_ports).or_else(|| pick(&network_ports)) else {
        return Ok(String::new());
    };
    let Some(first) = bindings.as_array().and_then(|b| b.first()) else {
        return Ok(String::new());
    };
    let host = match get(first, "HostIp") {
        Some(Value::String(host)) if !host.is_empty() => host.clone(),
        _ => "127.0.0.1".to_owned(),
    };
    let host = match host.as_str() {
        "0.0.0.0" => "127.0.0.1".to_owned(),
        "::" => "::1".to_owned(),
        _ => host,
    };
    let host = if host.contains(':') {
        format!("[{host}]")
    } else {
        host
    };
    let Some(Value::String(port)) = get(first, "HostPort") else {
        return Ok(String::new());
    };
    if port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit()) {
        return Ok(String::new());
    }
    Ok(format!("http://{host}:{port}"))
}

#[cfg(test)]
mod tests {
    use super::super::docker::script::{Script, fail as failed, ok};
    use super::*;
    use serde_json::json;

    #[test]
    fn available_reads_the_fourth_field_of_the_last_line() {
        let df =
            "Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 9 1 8388608 1% /\n";
        assert_eq!(available(df.as_bytes(), "x").unwrap(), MIN_FREE);
        assert_eq!(available(b"a b c 8_388_608 e\n", "x").unwrap(), MIN_FREE);
        for bad in [
            &b""[..],
            b"a b c",
            b"a b c -1",
            b"a b c 1_",
            b"a b c 18014398509481984",
            "a b c \u{661}".as_bytes(),
        ] {
            assert!(matches!(available(bad, "x"), Err(Stop(_, 2))), "{bad:?}");
        }
    }

    #[test]
    fn daemon_url_reads_the_published_binding() {
        let running = json!({"HostConfig": {"PortBindings": {"8765/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9"}]}}});
        assert_eq!(
            daemon_url(&running, None, "d").unwrap(),
            "http://127.0.0.1:9"
        );
        let v6 = json!({"NetworkSettings": {"Ports": {"8765/tcp": [{"HostIp": "::", "HostPort": "9"}]}}});
        assert_eq!(daemon_url(&v6, None, "d").unwrap(), "http://[::1]:9");
        assert_eq!(daemon_url(&v6, Some("http://x"), "d").unwrap(), "http://x");
        assert_eq!(daemon_url(&json!({}), None, "d").unwrap(), "");
        // An unreadable binding is no published port (the refusal names
        // --daemon-url), never a guess.
        let odd = json!({"HostConfig": {"PortBindings": {"8765/tcp": [{"HostIp": "h"}]}}});
        assert_eq!(daemon_url(&odd, None, "d").unwrap(), "");
        assert_eq!(daemon_url(&odd, Some("http://x"), "d").unwrap(), "http://x");
        assert_eq!(
            daemon_url(&json!({"HostConfig": 5}), None, "d"),
            Err(malformed("d"))
        );
    }

    #[test]
    fn inspect_names_the_cause_of_a_failure() {
        let mut script = Script::new(vec![
            (&["inspect", "d"], failed(1, "Error: No such object: d")),
            (
                &["inspect", "d"],
                failed(
                    1,
                    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
                ),
            ),
            (&["inspect", "d"], failed(1, "some other trouble")),
            (&["inspect", "d"], ok("[]")),
            (&["inspect", "d"], ok("[5]")),
            (&["inspect", "d"], ok("{}")),
            (&["inspect", "d"], ok("[NaN]")),
            (&["inspect", "d"], ok("")),
            (&["inspect", "d"], ok("[{\"Name\": \"d\u{e9}\"}]")),
        ]);
        let mut bank = Bank {
            docker: &mut script,
            pg: "p".into(),
            daemon: "d".into(),
        };
        let missing = bank.inspect("d").unwrap_err();
        assert!(
            missing.0.starts_with("the container d does not exist;"),
            "{missing:?}"
        );
        let down = bank.inspect("d").unwrap_err();
        assert!(
            down.0.starts_with("the Docker engine is not answering;"),
            "{down:?}"
        );
        assert_eq!(bank.inspect("d").unwrap_err(), Stop(
            "Docker inspect failed for the backup/restore; check the containers with docker logs p and docker logs d, then retry".into(), 1));
        for _ in 0..5 {
            assert_eq!(bank.inspect("d").unwrap_err(), malformed("d"));
        }
        assert!(bank.inspect("d").is_ok());
    }

    #[test]
    fn a_wrong_typed_inspect_field_is_a_refusal() {
        // Issue #1507 item 1: `"Mounts": "not-a-list"` and friends.
        let running = json!({"Mounts": "not-a-list"});
        assert_eq!(list(&running, "Mounts", "d"), Err(malformed("d")));
        let state = json!({"State": []});
        assert_eq!(sub_dict(&state, "State", "d"), Err(malformed("d")));
        let mount = json!({"Destination": 5});
        assert_eq!(text_field(&mount, "Destination", "d"), Err(malformed("d")));
    }

    fn psql_exists(db: &str) -> Vec<String> {
        let query = format!("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = '{db}')");
        [
            "exec",
            "p",
            "env",
            "-u",
            "PGHOSTADDR",
            "-u",
            "PGSERVICE",
            "-u",
            "PGSERVICEFILE",
            "psql",
            "-X",
            "-h",
            "/var/run/postgresql",
            "-p",
            "5432",
            "-U",
            "u",
            "-d",
            "dbname=postgres",
            "-Atc",
            &query,
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect()
    }

    fn df(kb: u64) -> String {
        format!(
            "Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 1 1 {kb} 1% /\n"
        )
    }

    fn safety_dir() -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("pl-probe-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn args(safety: &std::path::Path) -> Args {
        Args {
            backup_file: "dump.sql.gz".into(),
            state_archive: Some("state.tgz".into()),
            yes: true,
            leave_stopped: false,
            container: Some("p".into()),
            daemon_container: Some("d".into()),
            db: Some("db".into()),
            user: Some("u".into()),
            db_port: Some(5432),
            daemon_url: None,
            safety_dir: Some(safety.to_path_buf()),
        }
    }

    const FLOOR_KB: u64 = 8 * 1024 * 1024;

    /// Every check passes: explicit selectors, a published port, a socket
    /// mount holding only its socket, a read-only mount skipped, free space
    /// exactly at the floor, and a stale helper listed.
    #[test]
    fn probe_checks_every_writable_mount_and_ends_with_a_plan() {
        let safety = safety_dir();
        let daemon = json!([{
            "Image": "img", "State": {"Running": true},
            "Mounts": [{"Destination": "/data"}, {"Destination": "/models"}],
            "HostConfig": {"PortBindings": {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18765"}]}},
        }])
        .to_string();
        let pg_mounts = json!([{
            "State": {"Running": true},
            "Mounts": [
                {"Destination": "/var/lib/postgresql", "RW": true},
                {"Destination": "/srv/ro", "RW": false},
                {"Destination": "/var/run/postgresql", "RW": true},
            ],
        }])
        .to_string();
        let exists = psql_exists("db");
        let exists: Vec<&str> = exists.iter().map(String::as_str).collect();
        let floor = df(FLOOR_KB);
        let mut script = Script::new(vec![
            (&["inspect", "d"], ok(&daemon)),
            (&["inspect", "p"], ok(&pg_mounts)),
            (&exists, ok("t\n")),
            (&["exec", "p", "df", "-Pk", "/"], ok(&floor)),
            (&["exec", "p", "df", "-Pk", "/tmp"], ok(&floor)),
            (&["inspect", "p"], ok(&pg_mounts)),
            (
                &["exec", "p", "sh", "-c", FIND, "_", "/var/run/postgresql"],
                ok(""),
            ),
            (&["exec", "p", "df", "-Pk", "/"], ok(&floor)),
            (
                &["exec", "p", "df", "-Pk", "/var/lib/postgresql"],
                ok(&floor),
            ),
            (
                &[
                    "create",
                    "--network",
                    "none",
                    "--user",
                    "0",
                    "--volumes-from",
                    "d:ro",
                    "--log-driver",
                    "none",
                    "--label",
                    "pseudolife.restore-helper=d",
                    "--entrypoint",
                    "sh",
                    "img",
                    "-c",
                    "LC_ALL=C df -Pk -- /data",
                ],
                ok("h1\n"),
            ),
            (&["start", "-a", "h1"], ok(&floor)),
            (&["rm", "-f", "h1"], ok("")),
            (
                &["ps", "-aq", "--filter", "label=pseudolife.restore-helper=d"],
                ok("old1\nold2\n"),
            ),
        ]);
        let mut measured = Vec::new();
        let plan = probe(&mut script, &args(&safety), "n", &mut |dir| {
            measured.push(dir.to_owned());
            Ok(())
        })
        .unwrap();
        assert_eq!(script.calls.len(), script.answers.len());
        assert_eq!(measured.len(), 1);
        assert_eq!(plan.daemon_url, "http://127.0.0.1:18765");
        assert!(plan.was_running && plan.exists);
        assert_eq!(plan.pg_paths, ["/", "/var/lib/postgresql"]);
        assert_eq!(plan.state_mounts, ["/data"]);
        assert_eq!(plan.stale_helpers, ["old1", "old2"]);
        assert_eq!(
            (plan.user.as_str(), plan.db.as_str(), plan.port),
            ("u", "db", 5432)
        );
        let _ = std::fs::remove_dir_all(&safety);
    }

    /// `--leave-stopped` needs no health port; an absent database is no
    /// refusal; a mount one KiB under the floor is.
    #[test]
    fn probe_refuses_one_kib_under_the_floor() {
        let safety = safety_dir();
        let mut no_state = args(&safety);
        no_state.state_archive = None;
        no_state.leave_stopped = true;
        let daemon = json!([{"Image": "img", "Mounts": [{"Destination": "/data"}]}]).to_string();
        let pg = json!([{"State": {"Running": true}, "Mounts": [{"Destination": "/var/lib/postgresql"}]}])
            .to_string();
        let exists = psql_exists("db");
        let exists: Vec<&str> = exists.iter().map(String::as_str).collect();
        let floor = df(FLOOR_KB);
        let under = df(FLOOR_KB - 1);
        let calls = |last: &str| -> Vec<(Vec<String>, Ran)> {
            let answers: Vec<(&[&str], Ran)> = vec![
                (&["inspect", "d"], ok(&daemon)),
                (&["inspect", "p"], ok(&pg)),
                (&exists, ok("f\n")),
                (&["exec", "p", "df", "-Pk", "/"], ok(&floor)),
                (&["exec", "p", "df", "-Pk", "/tmp"], ok(&floor)),
                (&["inspect", "p"], ok(&pg)),
                (&["exec", "p", "df", "-Pk", "/"], ok(&floor)),
                (&["exec", "p", "df", "-Pk", "/var/lib/postgresql"], ok(last)),
                (
                    &["ps", "-aq", "--filter", "label=pseudolife.restore-helper=d"],
                    ok(""),
                ),
            ];
            answers
                .into_iter()
                .map(|(a, r)| (a.iter().map(|s| (*s).to_owned()).collect(), r))
                .collect()
        };
        let script_of = |answers: Vec<(Vec<String>, Ran)>| Script {
            calls: Vec::new(),
            inputs: Vec::new(),
            flag: false,
            answers: answers
                .into_iter()
                .map(|(args, ran)| super::super::docker::script::Answer {
                    args,
                    ran,
                    writes: None,
                    interrupt: false,
                })
                .collect(),
        };
        let mut script = script_of(calls(&floor));
        let plan = probe(&mut script, &no_state, "n", &mut |_| Ok(())).unwrap();
        assert_eq!(plan.daemon_url, "");
        assert!(!plan.exists && !plan.was_running && plan.stale_helpers.is_empty());
        let mut script = script_of(calls(&under));
        assert_eq!(
            probe(&mut script, &no_state, "n", &mut |_| Ok(())).unwrap_err(),
            Stop(
                "less than 8 GiB is free on p:/var/lib/postgresql; free at least 8 GiB there, then retry. Nothing was changed".into(),
                2
            )
        );
        let _ = std::fs::remove_dir_all(&safety);
    }

    #[test]
    fn sh_quote_keeps_plain_words() {
        assert_eq!(sh_quote("/data/x-1"), "/data/x-1");
        assert_eq!(sh_quote("a b"), "'a b'");
        assert_eq!(sh_quote("it's"), r"'it'\''s'");
        assert_eq!(sh_quote(""), "''");
    }
}
