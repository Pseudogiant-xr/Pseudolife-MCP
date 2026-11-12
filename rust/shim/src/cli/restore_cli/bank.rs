//! `DockerBank(...)` and the read-only start of `DockerBank.restore()`, up to
//! the first command that changes anything (stopping the daemon, a helper
//! container, the safety backup). Every Docker call here only reads
//! (`inspect`, `exec ... psql -Atc SELECT`, `exec ... df`, `exec ... find`),
//! in the oracle's order. The outcome is a refusal this leaf is sure of (the
//! caller takes the update lock, as the oracle holds it, then prints it),
//! `Defer` where it is not sure of Python's answer, or `Reached`: the oracle
//! would now make its first change.
use std::{path::Path, time::Duration};

use serde_json::Value;

use super::{
    args::{Args, python_int},
    conn::{self, Resolved, truthy},
    docker::{Docker, ascii_text},
    dump::python_space,
    pathlib,
};

const PG_CONTAINER: &str = "pseudolife-mcp-postgres";
const DAEMON_CONTAINER: &str = "pseudolife-mcp-daemon";
const MIN_FREE: u128 = 8 * 1024 * 1024 * 1024;
/// `command`'s default timeout.
const LONG: Duration = Duration::from_secs(3600);
const MINUTE: Duration = Duration::from_secs(60);

#[derive(Debug, PartialEq, Eq)]
pub(super) enum Stop {
    /// `UpdateError(message, exit_code)`: `restore failed: <message>`.
    Fail(String, u8),
    /// Python's answer is not certain here: defer (nothing has changed).
    Defer,
}

type Step<T> = Result<T, Stop>;

fn fail<T>(message: impl Into<String>, code: u8) -> Step<T> {
    Err(Stop::Fail(message.into(), code))
}

/// Where the read-only part ended without a refusal: what the oracle's
/// first change depends on (read once its destructive path is ported).
#[derive(Debug)]
#[allow(dead_code)]
pub(super) struct Reached {
    pub was_running: bool,
    pub exists: bool,
}

struct Bank<'a, D: Docker> {
    docker: &'a mut D,
    pg: String,
    daemon: String,
}

/// `dict.get(key, default)` on a value known to be a dict.
fn get<'v>(value: &'v Value, key: &str) -> Option<&'v Value> {
    value.as_object().and_then(|map| map.get(key))
}

/// `value.get(key, {})` that must itself be a dict for the next `.get`.
fn sub_dict<'v>(value: &'v Value, key: &str) -> Step<Option<&'v Value>> {
    match get(value, key) {
        None => Ok(None),
        Some(inner @ Value::Object(_)) => Ok(Some(inner)),
        // `.get` on None or a list escapes as AttributeError.
        Some(_) => Err(Stop::Defer),
    }
}

/// `str.split()` on ASCII text (`str.isspace`).
fn split_ascii(text: &str) -> Vec<&str> {
    text.split(|c: char| python_space(c))
        .filter(|word| !word.is_empty())
        .collect()
}

/// `str.splitlines()` on ASCII text whose `\r` is already translated.
fn splitlines_ascii(text: &str) -> Vec<&str> {
    let mut lines: Vec<&str> = text
        .split(['\n', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e'])
        .collect();
    if lines.last() == Some(&"") {
        lines.pop();
    }
    lines
}

/// `disk_space._available(output, location)` for ASCII output.
fn available(output: &[u8], location: &str) -> Step<u128> {
    let text = ascii_text(output).ok_or(Stop::Defer)?;
    let refuse = || {
        Stop::Fail(
            format!(
                "cannot read free space on {location}; make `df -Pk` work in the container and retry. Nothing was changed"
            ),
            2,
        )
    };
    let lines = splitlines_ascii(&text);
    let last = lines.last().ok_or_else(refuse)?;
    let field = *split_ascii(last).get(3).ok_or_else(refuse)?;
    let free = match python_int(field) {
        Some(Ok(free)) => free,
        Some(Err(())) => return Err(refuse()),
        None => return Err(Stop::Defer),
    };
    if free < 0 || free > i128::from(u64::MAX / 1024) {
        return Err(refuse());
    }
    Ok(free as u128 * 1024)
}

/// `disk_space.require(free, location)`.
fn require(free: u128, location: &str) -> Step<()> {
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

impl<D: Docker> Bank<'_, D> {
    fn run(&mut self, args: &[&str], timeout: Duration) -> super::docker::Ran {
        let args: Vec<String> = args.iter().map(|s| (*s).to_owned()).collect();
        self.docker.run(&args, timeout)
    }

    /// `DockerBank.inspect(container)`.
    fn inspect(&mut self, container: &str) -> Step<Value> {
        let ran = self.run(&["inspect", container], MINUTE);
        if ran.code != 0 {
            return fail(
                format!(
                    "Docker inspect failed for the backup/restore; check the containers with docker logs {} and docker logs {}, then retry",
                    self.pg, self.daemon
                ),
                1,
            );
        }
        let refuse = || {
            Stop::Fail(
                format!(
                    "cannot inspect {container}; restore its Docker container configuration and retry"
                ),
                2,
            )
        };
        let text = ascii_text(&ran.output(true)).ok_or(Stop::Defer)?;
        if text.trim_matches([' ', '\t', '\n', '\r']).is_empty() {
            return Err(refuse());
        }
        // json.loads also reads NaN, Infinity, huge exponents and lone
        // surrogates, which serde rejects: a parse error is unsure.
        let value: Value = serde_json::from_str(&text).map_err(|_| Stop::Defer)?;
        match value {
            Value::Array(items) => match items.into_iter().next() {
                Some(item @ Value::Object(_)) => Ok(item),
                _ => Err(refuse()),
            },
            // `{...}[0]` raises KeyError, which escapes.
            Value::Object(_) => Err(Stop::Defer),
            _ => Err(refuse()),
        }
    }

    /// `disk_space.container(docker, name, path)`.
    fn container_space(&mut self, path: &str) -> Step<()> {
        let pg = self.pg.clone();
        let location = format!("{pg}:{path}");
        let ran = self.run(&["exec", &pg, "df", "-Pk", path], MINUTE);
        if ran.code != 0 {
            return fail(
                format!(
                    "cannot check free space on {location}; run `docker exec {pg} df -Pk {path}` successfully, then retry. Nothing was changed"
                ),
                2,
            );
        }
        require(available(&ran.output(false), &location)?, &location)
    }

    /// `disk_space.docker_storage(docker, [pg, daemon], image)` while one
    /// of the containers answers; its helper container is a change.
    fn storage_space(&mut self) -> Step<()> {
        for name in [self.pg.clone(), self.daemon.clone()] {
            let ran = self.run(&["exec", &name, "df", "-Pk", "/"], MINUTE);
            if ran.code == 0 {
                let free = available(&ran.output(false), &format!("Docker storage ({name})"))?;
                return require(free, "Docker storage");
            }
        }
        Err(Stop::Defer)
    }

    /// `DockerBank.psql(database, *args)`.
    fn psql(&mut self, user: &str, port: i128, database: &str, args: &[&str]) -> Step<String> {
        let port = port.to_string();
        let target = conn::database_arg(database);
        let pg = self.pg.clone();
        let mut argv = vec![
            "exec",
            &pg,
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
            &port,
            "-U",
            user,
            "-d",
            &target,
        ];
        argv.extend_from_slice(args);
        let ran = self.run(&argv, LONG);
        if ran.code != 0 {
            if ran.code == 2 {
                let text = ascii_text(&ran.output(true)).ok_or(Stop::Defer)?;
                let reason = text.to_lowercase();
                if reason.contains("socket")
                    && (reason.contains("no such file") || reason.contains("connection refused"))
                {
                    return fail(
                        format!(
                            "PostgreSQL's local socket is unavailable on port {port}; name the selected container's server port with --db-port <port> and retry"
                        ),
                        2,
                    );
                }
            }
            return fail(
                format!("PostgreSQL failed inside {pg}; check docker logs {pg} before retrying"),
                1,
            );
        }
        ascii_text(&ran.output(true)).ok_or(Stop::Defer)
    }
}

const RESOLVE: &str = "the daemon's database connection cannot be resolved safely; set a direct PSEUDOLIFE_MCP_DATABASE_URL on a network shared with the selected PostgreSQL container, or name the actual bank with --container <postgres> --db <database> --user <role> --db-port <port>. Nothing was restored";
const PORT: &str = "the database port is ambiguous; name the selected container's server port with --db-port <port>. Nothing was restored";
const NO_MOUNT: &str = "the daemon has no reusable /data mount or local image; restore its Docker container configuration before backing up or restoring";
const NO_HEALTH: &str = "the daemon has no published health port; name its endpoint with --daemon-url or use --leave-stopped. Nothing was restored";
const EXISTS: &str = "cannot determine whether the destination database exists; verify PostgreSQL and retry. Nothing was restored";
const IN_MOUNT: &str = "the safety backup would be inside the state mount being replaced; choose --safety-dir <directory outside the state mount>. Nothing was restored";
const SOCKET_MOUNT: &str = "cannot inspect the PostgreSQL socket mount; make find work in the selected PostgreSQL container and retry. Nothing was restored";
/// `_restore_space`'s probe for payload in a socket mount.
const FIND: &str = r#"test -d "$1" && find -H "$1" -mindepth 1 -maxdepth 1 ! -type s ! \( -type f -name ".s.PGSQL.*.lock" \) -print -quit"#;

fn nonempty(value: &Option<String>) -> Option<&str> {
    value.as_deref().filter(|v| !v.is_empty())
}

/// `up.home()`: `USERPROFILE`, else `HOME` (an unset or empty pair, where
/// Python asks `Path.home()`, defers).
fn home() -> Step<String> {
    for key in ["USERPROFILE", "HOME"] {
        if let Ok(value) = std::env::var(key)
            && !value.is_empty()
        {
            return Ok(value);
        }
    }
    Err(Stop::Defer)
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
                // `.` does not match a newline: no full match, no mapping.
                if !tail.contains('\n') {
                    return format!("{drive}:/{tail}");
                }
            }
        }
    }
    source.to_owned()
}

/// `Path(source).is_absolute()` on this platform (a drive and a root, or a
/// UNC share, on Windows; a leading `/` elsewhere).
fn absolute(source: &Path) -> bool {
    source.is_absolute()
}

/// The free bytes `shutil.disk_usage(path).free` reports.
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
        // nt._getdiskusage reports the volume's total free bytes (the
        // fourth argument), not the caller's quota-limited share.
        let ok = unsafe {
            windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW(
                wide.as_ptr(),
                &mut caller,
                &mut total,
                &mut free,
            )
        };
        (ok != 0).then_some(u128::from(free))
    }
}

/// `disk_space.host(directory)`; `directory` is the resolved safety dir.
fn host_space(directory: &str) -> Step<()> {
    let mut target = pathlib::resolve(Path::new(directory)).ok_or(Stop::Defer)?;
    loop {
        if pathlib::exists(&target).ok_or(Stop::Defer)? {
            break;
        }
        match Path::new(&target).parent() {
            Some(parent) if !parent.as_os_str().is_empty() => {
                target = parent.to_str().ok_or(Stop::Defer)?.to_owned();
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

/// The bank's selection and the restore's read-only checks.
pub(super) fn probe<D: Docker>(docker: &mut D, args: &Args) -> Step<Reached> {
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
            Resolved::Unsure => return Err(Stop::Defer),
        }
    };
    let user = nonempty(&args.user).map_or(connection_user, str::to_owned);
    let db = nonempty(&args.db).map_or(connection_db, str::to_owned);
    let port = args.db_port.unwrap_or(connection_port);
    if !(1..=65535).contains(&port) {
        return fail(PORT, 2);
    }
    // make_conninfo(dbname=...) cannot fail for a NUL-free string.
    if db.contains('\0') || user.contains('\0') {
        return Err(Stop::Defer);
    }
    let image = match get(&running, "Image") {
        Some(Value::String(image)) if !image.is_empty() => image.clone(),
        Some(value) if truthy(value) => return Err(Stop::Defer),
        _ => return fail(NO_MOUNT, 2),
    };
    let mounts: &[Value] = match get(&running, "Mounts") {
        None => &[],
        Some(Value::Array(mounts)) => mounts,
        Some(_) => return Err(Stop::Defer),
    };
    let mut has_data = false;
    for mount in mounts {
        if !mount.is_object() {
            return Err(Stop::Defer);
        }
        if get(mount, "Destination") == Some(&Value::String("/data".into())) {
            has_data = true;
            break;
        }
    }
    let _ = image;
    if !has_data {
        return fail(NO_MOUNT, 2);
    }
    let pg_running = match sub_dict(&pg, "State")? {
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
    let daemon_url = daemon_url(&running, args.daemon_url.as_deref())?;

    // DockerBank.restore(): the files were checked by the gates.
    pathlib::resolve(&args.backup_file).ok_or(Stop::Defer)?;
    if let Some(state) = &args.state_archive {
        pathlib::resolve(state).ok_or(Stop::Defer)?;
    }
    if !args.leave_stopped && daemon_url.is_empty() {
        return fail(NO_HEALTH, 2);
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
    let safety = pathlib::resolve(&safety).ok_or(Stop::Defer)?;
    for parent in pathlib::self_and_parents(&safety) {
        if pathlib::exists(&pathlib::join(&parent, ".git")).ok_or(Stop::Defer)? {
            return fail(
                format!(
                    "the safety backup directory {safety} is inside a checkout; choose --safety-dir <directory outside a checkout>. Nothing was restored"
                ),
                2,
            );
        }
    }
    for mount in mounts {
        let Some(map) = mount.as_object() else {
            return Err(Stop::Defer);
        };
        let destination = match map.get("Destination") {
            None => "",
            Some(Value::String(destination)) => destination,
            Some(_) => return Err(Stop::Defer),
        };
        if destination != "/data" && !destination.starts_with("/data/") {
            continue;
        }
        let source = match map.get("Source") {
            Some(value) if !truthy(value) => continue,
            None => continue,
            Some(Value::String(source)) => desktop_source(source),
            Some(_) => return Err(Stop::Defer),
        };
        let source = super::super::backup::args::pathlib(Path::new(&source));
        if absolute(&source) {
            let resolved = pathlib::resolve(&source).ok_or(Stop::Defer)?;
            if pathlib::is_relative_to(&safety, &resolved) {
                return fail(IN_MOUNT, 2);
            }
        }
    }
    // _restore_space(safety_dir, state=...): _backup_space first.
    host_space(&safety)?;
    bank.storage_space()?;
    bank.container_space("/tmp")?;
    let postgres = bank.inspect(&pg_name)?;
    let pg_mounts: &[Value] = match get(&postgres, "Mounts") {
        None => &[],
        Some(Value::Array(mounts)) => mounts,
        Some(_) => return Err(Stop::Defer),
    };
    let mut paths = vec!["/".to_owned()];
    for mount in pg_mounts {
        let Some(map) = mount.as_object() else {
            return Err(Stop::Defer);
        };
        let rw = map.get("RW").is_none_or(truthy);
        if !rw {
            continue;
        }
        let path = match map.get("Destination") {
            None => "",
            Some(Value::String(path)) => path.as_str(),
            Some(_) => return Err(Stop::Defer),
        };
        if !path.starts_with('/') {
            continue;
        }
        if path == "/run/postgresql" || path == "/var/run/postgresql" {
            let ran = bank.run(&["exec", &pg_name, "sh", "-c", FIND, "_", path], MINUTE);
            if ran.code != 0 {
                return fail(SOCKET_MOUNT, 2);
            }
            let found = ascii_text(&ran.output(true)).ok_or(Stop::Defer)?;
            if found.trim_matches(python_space).is_empty() {
                continue;
            }
        }
        if !paths.iter().any(|known| known == path) {
            paths.push(path.to_owned());
        }
    }
    paths.sort();
    for path in &paths {
        bank.container_space(path)?;
    }
    if args.state_archive.is_some() {
        // The state mounts' df runs in a helper container: a change.
        return Err(Stop::Defer);
    }
    let was_running = match sub_dict(&running, "State")? {
        Some(state) => get(state, "Running").is_some_and(truthy),
        None => false,
    };
    Ok(Reached {
        was_running,
        exists,
    })
}

/// `DockerBank.__init__`'s daemon URL: the option, else the daemon's
/// published 8765/tcp binding; "" when neither names one.
fn daemon_url(running: &Value, option: Option<&str>) -> Step<String> {
    let option = option.unwrap_or("");
    let ports = |outer: &str, inner: &str| -> Step<Option<Value>> {
        let Some(section) = sub_dict(running, outer)? else {
            return Ok(None);
        };
        match get(section, inner) {
            Some(value) if !truthy(value) => Ok(None),
            None => Ok(None),
            Some(value @ Value::Object(_)) => Ok(Some(value.clone())),
            Some(_) => Err(Stop::Defer),
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
    let bindings = pick(&host_ports).or_else(|| pick(&network_ports));
    if !option.is_empty() {
        return Ok(option.to_owned());
    }
    let Some(bindings) = bindings else {
        return Ok(String::new());
    };
    let Some(first) = bindings.as_array().and_then(|b| b.first()) else {
        return Err(Stop::Defer);
    };
    if !first.is_object() {
        return Err(Stop::Defer);
    }
    let host = match get(first, "HostIp") {
        Some(value) if !truthy(value) => "127.0.0.1".to_owned(),
        None => "127.0.0.1".to_owned(),
        Some(Value::String(host)) => host.clone(),
        Some(_) => return Err(Stop::Defer),
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
    let port = match get(first, "HostPort") {
        Some(Value::String(port)) => port.clone(),
        _ => return Err(Stop::Defer),
    };
    Ok(format!("http://{host}:{port}"))
}

#[cfg(test)]
mod tests {
    use super::super::docker::script::{Script, fail as failed, ok};
    use super::*;
    use serde_json::json;

    #[test]
    fn available_follows_python_split_and_int() {
        let df =
            "Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 9 1 8388608 1% /\n";
        assert_eq!(available(df.as_bytes(), "x").unwrap(), MIN_FREE);
        assert_eq!(available(b"a b c 8_388_608 e\n", "x").unwrap(), MIN_FREE);
        assert_eq!(available(b"a b c +1\x0bnext\n", "x").unwrap_err(), Stop::Fail(
            "cannot read free space on x; make `df -Pk` work in the container and retry. Nothing was changed".into(), 2));
        for bad in [
            &b""[..],
            b"a b c",
            b"a b c -1",
            b"a b c 1_",
            b"a b c 18014398509481984",
        ] {
            assert!(
                matches!(available(bad, "x"), Err(Stop::Fail(_, 2))),
                "{bad:?}"
            );
        }
        assert_eq!(available("a b c \u{661}".as_bytes(), "x"), Err(Stop::Defer));
    }

    #[test]
    fn daemon_url_reads_the_published_binding() {
        let running = json!({"HostConfig": {"PortBindings": {"8765/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9"}]}}});
        assert_eq!(daemon_url(&running, None).unwrap(), "http://127.0.0.1:9");
        let v6 = json!({"NetworkSettings": {"Ports": {"8765/tcp": [{"HostIp": "::", "HostPort": "9"}]}}});
        assert_eq!(daemon_url(&v6, None).unwrap(), "http://[::1]:9");
        assert_eq!(daemon_url(&v6, Some("http://x")).unwrap(), "http://x");
        assert_eq!(daemon_url(&json!({}), None).unwrap(), "");
        let odd = json!({"HostConfig": {"PortBindings": {"8765/tcp": [{"HostIp": "h"}]}}});
        assert_eq!(daemon_url(&odd, None), Err(Stop::Defer));
        assert_eq!(daemon_url(&odd, Some("http://x")).unwrap(), "http://x");
        assert_eq!(
            daemon_url(&json!({"HostConfig": null}), Some("u")),
            Err(Stop::Defer)
        );
    }

    #[test]
    fn inspect_refuses_what_json_loads_refuses() {
        let mut script = Script::new(vec![
            (&["inspect", "d"], failed(1, "no such container")),
            (&["inspect", "d"], ok("[]")),
            (&["inspect", "d"], ok("[5]")),
            (&["inspect", "d"], ok("{}")),
            (&["inspect", "d"], ok("[NaN]")),
            (&["inspect", "d"], ok("")),
        ]);
        let mut bank = Bank {
            docker: &mut script,
            pg: "p".into(),
            daemon: "d".into(),
        };
        assert_eq!(bank.inspect("d").unwrap_err(), Stop::Fail(
            "Docker inspect failed for the backup/restore; check the containers with docker logs p and docker logs d, then retry".into(), 1));
        let refused = Stop::Fail(
            "cannot inspect d; restore its Docker container configuration and retry".into(),
            2,
        );
        assert_eq!(bank.inspect("d").unwrap_err(), refused);
        assert_eq!(bank.inspect("d").unwrap_err(), refused);
        assert_eq!(bank.inspect("d").unwrap_err(), Stop::Defer);
        assert_eq!(bank.inspect("d").unwrap_err(), Stop::Defer);
        assert_eq!(bank.inspect("d").unwrap_err(), refused);
    }
}
