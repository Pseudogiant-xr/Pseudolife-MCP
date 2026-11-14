//! `pseudolife-mcp installer preflight-check --repo R --env-file F`
//! (formerly `ops/preflight.py --env-file F`): the read-only Docker
//! ownership checks both preflights run before an installer writes anything.
//!
//! Names reserve a namespace; they do not prove ownership. Only the Compose
//! project label plus the recorded local environment permits an installer
//! rerun. No Docker output (which may contain credentials) reaches an error.
//! Each refusal is ops/preflight.py's, printed as `FAIL <message>` on stderr
//! with exit 1; success prints its one `OK` line.
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

const PROJECT: &str = "pseudolife-mcp";
const BEGIN: &str =
    "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>";
const END: &str = "# <<< pseudolife-mcp install <<<";
const FIELDS: [&str; 5] = [
    "PSEUDOLIFE_BANK_VOLUME",
    "PSEUDOLIFE_STATE_VOLUME",
    "POSTGRES_PASSWORD",
    "PSEUDOLIFE_MCP_TOKEN",
    "PSEUDOLIFE_MCP_TOKENS",
];
const DOCKER_UNSAFE: &str =
    "Cannot inspect Docker safely. Run docker info, fix Docker access, then rerun.";
const UNREADABLE: &str = "Cannot read installer ownership records. Restore the original ops/.env and check docker info, then rerun. Nothing changed.";
const OK_LINE: &str =
    "  OK   Docker stack, recorded volumes and ports belong to this install (or are free)";

#[derive(Debug, PartialEq)]
pub(super) struct Refusal(pub(super) String);
type Result<T> = std::result::Result<T, Refusal>;
fn refuse<T>(message: impl Into<String>) -> Result<T> {
    Err(Refusal(message.into()))
}

/// What the check reads from the host; the tests supply their own.
pub(super) trait Host {
    /// `docker(*args, input=..., failure=...)`: stdout, or the refusal.
    fn docker(&self, args: &[&str], input: Option<&str>, failure: Option<&str>) -> Result<String>;
    fn port_busy(&self, port: u16) -> bool;
    fn file_text(&self, path: &Path) -> Option<String>;
    fn is_file(&self, path: &Path) -> bool;
    fn is_dir(&self, path: &Path) -> bool;
    fn same_path(&self, first: &Path, second: &Path) -> bool;
    fn var(&self, name: &str) -> Option<String>;
    fn home(&self) -> PathBuf;
    fn warn(&self, line: &str);
}

/// `port_busy`: whether 127.0.0.1:`port` cannot be bound. On Unix the socket
/// is plain, as Python's was: std's bind sets SO_REUSEADDR, which on macOS
/// lets a loopback bind succeed beside a same-user listener on every
/// address. On Windows std's plain bind answers as Python's exclusive one
/// does for every listener shape (probed 14 November 2026).
fn port_taken(port: u16) -> bool {
    #[cfg(unix)]
    {
        // SAFETY: a fresh socket, bound once and closed on every path.
        unsafe {
            let fd = libc::socket(libc::AF_INET, libc::SOCK_STREAM, 0);
            if fd < 0 {
                return true;
            }
            let mut address: libc::sockaddr_in = std::mem::zeroed();
            #[cfg(any(target_os = "macos", target_os = "ios", target_os = "freebsd"))]
            {
                address.sin_len = std::mem::size_of::<libc::sockaddr_in>() as u8;
            }
            address.sin_family = libc::AF_INET as libc::sa_family_t;
            address.sin_port = port.to_be();
            address.sin_addr = libc::in_addr {
                s_addr: u32::from(std::net::Ipv4Addr::LOCALHOST).to_be(),
            };
            let bound = libc::bind(
                fd,
                std::ptr::addr_of!(address).cast::<libc::sockaddr>(),
                std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t,
            );
            libc::close(fd);
            bound != 0
        }
    }
    #[cfg(not(unix))]
    {
        std::net::TcpListener::bind(("127.0.0.1", port)).is_err()
    }
}

struct RealHost;
impl Host for RealHost {
    fn docker(&self, args: &[&str], input: Option<&str>, failure: Option<&str>) -> Result<String> {
        let unreached = || Refusal(DOCKER_UNSAFE.into());
        let mut child = Command::new("docker")
            .args(args)
            .stdin(if input.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| unreached())?;
        if let (Some(text), Some(mut stdin)) = (input, child.stdin.take()) {
            // stdin keeps the credential out of argv; a closed pipe is the
            // command's own failure, reported by its exit.
            let _ = stdin.write_all(text.as_bytes());
        }
        let mut stdout = child.stdout.take();
        let mut stderr = child.stderr.take();
        let reader = std::thread::spawn(move || {
            let mut bytes = Vec::new();
            if let Some(out) = stdout.as_mut() {
                let _ = out.read_to_end(&mut bytes);
            }
            bytes
        });
        let drain = std::thread::spawn(move || {
            if let Some(err) = stderr.as_mut() {
                let _ = io::copy(err, &mut io::sink());
            }
        });
        let started = Instant::now();
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) if started.elapsed() <= Duration::from_secs(20) => {
                    std::thread::sleep(Duration::from_millis(20));
                }
                _ => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(unreached());
                }
            }
        };
        let bytes = reader.join().unwrap_or_default();
        let _ = drain.join();
        if !status.success() {
            return refuse(failure.unwrap_or(DOCKER_UNSAFE));
        }
        Ok(String::from_utf8_lossy(&bytes)
            .replace("\r\n", "\n")
            .replace('\r', "\n"))
    }
    /// Both shipped publications bind IPv4 loopback only; an IPv6-only
    /// listener on ::1 can coexist and is not an installer collision.
    fn port_busy(&self, port: u16) -> bool {
        port_taken(port)
    }
    fn file_text(&self, path: &Path) -> Option<String> {
        let bytes = std::fs::read(path).ok()?;
        String::from_utf8(bytes).ok()
    }
    fn is_file(&self, path: &Path) -> bool {
        path.is_file()
    }
    fn is_dir(&self, path: &Path) -> bool {
        path.is_dir()
    }
    /// `samefile` (one file: device and inode on Unix), else the
    /// case-normalized resolved paths.
    fn same_path(&self, first: &Path, second: &Path) -> bool {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if let (Ok(a), Ok(b)) = (std::fs::metadata(first), std::fs::metadata(second)) {
                return (a.dev(), a.ino()) == (b.dev(), b.ino());
            }
        }
        match (std::fs::canonicalize(first), std::fs::canonicalize(second)) {
            (Ok(a), Ok(b)) => a == b,
            _ => {
                let norm = |p: &Path| {
                    let text = std::path::absolute(p)
                        .unwrap_or_else(|_| p.to_path_buf())
                        .display()
                        .to_string();
                    if cfg!(windows) {
                        text.to_lowercase().replace('/', "\\")
                    } else {
                        text
                    }
                };
                norm(first) == norm(second)
            }
        }
    }
    fn var(&self, name: &str) -> Option<String> {
        std::env::var(name).ok()
    }
    /// `Path.home()`: HOME on POSIX, USERPROFILE on Windows.
    fn home(&self) -> PathBuf {
        let name = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
        std::env::var_os(name)
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .or_else(dirs::home_dir)
            .unwrap_or_default()
    }
    fn warn(&self, line: &str) {
        let _ = writeln!(io::stderr().lock(), "{line}");
    }
}

/// `environment(path)`: the FIELDS assignments in the file (an unquoted
/// value loses a " #" comment) and whether it holds a managed block.
fn environment(host: &dyn Host, path: &Path) -> Result<(BTreeMap<String, String>, bool)> {
    // read_text(encoding="utf-8-sig"): a file that is not UTF-8 refuses.
    let text = if host.is_file(path) {
        host.file_text(path)
            .ok_or_else(|| Refusal(UNREADABLE.into()))?
    } else {
        String::new()
    };
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    // Universal newlines: a lone CR ends a line too.
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    let lines: Vec<&str> = text.lines().collect();
    let mut values = BTreeMap::new();
    for line in &lines {
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        if !FIELDS.contains(&key) {
            continue;
        }
        let value = value.trim();
        let value = match value.chars().next() {
            // value[:1] in quotes and value[-1:] == value[:1]: a lone quote
            // is both ends, and value[1:-1] is empty.
            Some(q @ ('\'' | '"')) if value.ends_with(q) => value
                .get(1..value.len().saturating_sub(1))
                .unwrap_or("")
                .to_owned(),
            _ => value.split(" #").next().unwrap_or("").trim_end().to_owned(),
        };
        values.insert(key.to_owned(), value);
    }
    Ok((values, lines.contains(&BEGIN) && lines.contains(&END)))
}

/// `resolved_environment(path)`: Compose's view of the FIELDS (it owns dotenv
/// syntax, interpolation and exported-value precedence).
fn resolved_environment(
    host: &dyn Host,
    repo: &Path,
    path: &Path,
) -> Result<BTreeMap<String, String>> {
    let ops = repo.join("ops");
    let source = if host.is_file(path) {
        path.to_path_buf()
    } else {
        ops.join(".env.example")
    };
    let (source, compose) = (
        source.display().to_string(),
        ops.join("docker-compose.yml").display().to_string(),
    );
    let output = host.docker(
        &["compose", "--env-file", &source, "-f", &compose, "config", "--environment"],
        None,
        Some("Cannot resolve ops/.env with Docker Compose. Run docker compose --env-file ops/.env -f ops/docker-compose.yml config --quiet, fix the reported configuration, then rerun. Nothing changed."),
    )?;
    Ok(output
        .lines()
        .filter_map(|line| line.split_once('='))
        .filter(|(key, _)| FIELDS.contains(key))
        .map(|(key, value)| (key.to_owned(), value.to_owned()))
        .collect())
}

fn valid_volume(name: &str) -> bool {
    let mut chars = name.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && name.chars().count() >= 2
        && chars.all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c))
}

fn volume_names(values: &BTreeMap<String, String>) -> Result<(String, String)> {
    let pick = |key: &str, default: &str| {
        values
            .get(key)
            .filter(|v| !v.is_empty())
            .cloned()
            .unwrap_or_else(|| default.to_owned())
    };
    let names = (
        pick("PSEUDOLIFE_BANK_VOLUME", "pseudolife-mcp-bank"),
        pick("PSEUDOLIFE_STATE_VOLUME", "pseudolife-mcp-state"),
    );
    if !(valid_volume(&names.0) && valid_volume(&names.1)) {
        return refuse(
            "Invalid bank/state volume name. Set literal Docker volume names in ops/.env, then rerun. Nothing changed.",
        );
    }
    Ok(names)
}

/// `host_path`: a checkout used through WSL and native Windows is still one
/// checkout.
fn host_path(host: &dyn Host, value: &str) -> PathBuf {
    let mut value = value.to_owned();
    if cfg!(windows) {
        let rest = value
            .strip_prefix("/mnt/")
            .or_else(|| value.strip_prefix('/'));
        if let Some(rest) = rest
            && let Some((drive, tail)) = rest.split_once('/')
            && drive.len() == 1
            && drive.chars().all(|c| c.is_ascii_alphabetic())
        {
            value = format!("{drive}:/{tail}");
        }
    } else if host.var("WSL_DISTRO_NAME").is_some_and(|v| !v.is_empty()) {
        let bytes = value.as_bytes();
        if bytes.len() >= 3
            && bytes[0].is_ascii_alphabetic()
            && bytes[1] == b':'
            && (bytes[2] == b'\\' || bytes[2] == b'/')
        {
            value = format!(
                "/mnt/{}/{}",
                (bytes[0] as char).to_ascii_lowercase(),
                value[3..].replace('\\', "/")
            );
        }
    }
    // Path(value).expanduser()
    if value == "~" {
        return py_path(&host.home().display().to_string());
    }
    if value.starts_with("~/") || (cfg!(windows) && value.starts_with("~\\")) {
        return py_path(&host.home().join(&value[2..]).display().to_string());
    }
    py_path(&value)
}

/// `Path(text)` as Python prints it: the platform's separator, no repeated
/// or trailing separator, no "." part.
fn py_path(text: &str) -> PathBuf {
    Path::new(text).components().collect()
}

/// `env_auth` plus whether a nonempty map lost every entry: the token map as
/// a set of (token, principal) pairs, the singular token, malformed.
type Auth = (BTreeSet<(String, String)>, Option<String>, bool);
fn authentication(values: &BTreeMap<String, String>) -> Auth {
    let raw = values
        .get("PSEUDOLIFE_MCP_TOKENS")
        .cloned()
        .unwrap_or_default();
    let mapping: BTreeSet<(String, String)> = super::credentials::parse_token_map(&raw)
        .into_iter()
        .collect();
    let singular = values
        .get("PSEUDOLIFE_MCP_TOKEN")
        .filter(|v| !v.is_empty())
        .cloned();
    // A malformed nonempty map stays distinct from an open configuration:
    // the daemon fails closed when no entry survives parsing.
    let malformed = !raw.is_empty() && mapping.is_empty();
    (mapping, singular, malformed)
}

fn env_map(container: &Value) -> BTreeMap<String, String> {
    container["Config"]["Env"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter_map(|item| item.split_once('='))
        .map(|(k, v)| (k.to_owned(), v.to_owned()))
        .collect()
}

fn labels(container: &Value) -> &serde_json::Map<String, Value> {
    static EMPTY: std::sync::OnceLock<serde_json::Map<String, Value>> = std::sync::OnceLock::new();
    container["Config"]["Labels"]
        .as_object()
        .unwrap_or_else(|| EMPTY.get_or_init(serde_json::Map::new))
}

fn label<'a>(container: &'a Value, key: &str) -> Option<&'a str> {
    labels(container).get(key).and_then(Value::as_str)
}

fn check_daemon_authority(
    host: &dyn Host,
    container: &Value,
    env_file: &Path,
    values: &BTreeMap<String, String>,
) -> Result<()> {
    let working =
        label(container, "com.docker.compose.project.working_dir").filter(|v| !v.is_empty());
    let labelled_env =
        label(container, "com.docker.compose.project.environment_file").filter(|v| !v.is_empty());
    let mut origin_matches = false;
    if let Some(labelled_env) = labelled_env {
        let original_env = host_path(host, labelled_env);
        if !host.is_file(&original_env) && labelled_env.contains(',') {
            let originals: Vec<PathBuf> = labelled_env
                .split(',')
                .filter(|v| !v.is_empty())
                .map(|v| host_path(host, v))
                .collect();
            if originals.len() > 1 && originals.iter().all(|p| host.is_file(p)) {
                return refuse(
                    "This stack uses multiple environment files; maintain it with pseudolife-mcp update, which retains that Compose context, instead of this single-env-file installer. Nothing changed.",
                );
            }
        }
        if !host.is_file(&original_env) {
            return refuse(format!(
                "The recorded install configuration ({}) is missing; restore it with its original checkout before rerunning. Nothing changed.",
                original_env.display()
            ));
        }
        if !host.same_path(&original_env, env_file) {
            let parent = original_env
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_default();
            return refuse(format!(
                "This stack uses a different install directory ({}); rerun the installer from that directory with its original ops/.env. Nothing changed.",
                parent.display()
            ));
        }
        origin_matches = true;
        if let Some(working) = working
            && !host.is_dir(&host_path(host, working))
        {
            host.warn("WARN The recorded Compose working directory is unavailable; keep the original ops/.env and use this checkout's installer to refresh the configuration paths.");
        }
    } else if let Some(working) = working {
        let original = host_path(host, working);
        if !host.is_dir(&original) {
            return refuse(format!(
                "The recorded install directory ({}) is missing; restore that checkout and its original ops/.env before rerunning. Nothing changed.",
                original.display()
            ));
        }
        let parent = env_file.parent().map(Path::to_path_buf).unwrap_or_default();
        if !host.same_path(&original, &parent) {
            return refuse(format!(
                "This stack uses a different install directory ({}); rerun the installer from that checkout with its original ops/.env. Nothing changed.",
                original.display()
            ));
        }
        origin_matches = true;
    }
    let live = authentication(&env_map(container));
    let requested = authentication(values);
    let any_requested = !requested.0.is_empty() || requested.1.is_some() || requested.2;
    if live != requested && (!origin_matches || !any_requested) {
        return refuse(
            "This configuration does not retain the existing daemon authentication; restore the original ops/.env instead of minting replacement credentials, then rerun. Nothing changed.",
        );
    }
    Ok(())
}

fn running(container: &Value) -> bool {
    container["State"]["Running"].as_bool().unwrap_or(false)
}

/// A running container publishing `port`/tcp on loopback or every address.
fn publishes(container: &Value, port: &str) -> bool {
    running(container)
        && container["NetworkSettings"]["Ports"]
            .as_object()
            .is_some_and(|ports| {
                ports
                    .iter()
                    .filter(|(publication, _)| publication.ends_with("/tcp"))
                    .any(|(_, bindings)| {
                        bindings.as_array().into_iter().flatten().any(|binding| {
                            // .get("HostIp", ""): absent is every address,
                            // a null is not.
                            let ip = match binding.get("HostIp") {
                                None => Some(""),
                                Some(value) => value.as_str(),
                            };
                            binding["HostPort"].as_str() == Some(port)
                                && matches!(ip, Some("" | "127.0.0.1" | "0.0.0.0"))
                        })
                    })
            })
}

/// `preflight_lite.data_dirs`: where a lite bank could live.
fn lite_data_dirs(host: &dyn Host) -> Vec<PathBuf> {
    let home = host.home();
    let base = if cfg!(windows) {
        host.var("LOCALAPPDATA")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .map(|v| py_path(&v.display().to_string()))
            .unwrap_or_else(|| home.join("AppData").join("Local"))
    } else if cfg!(target_os = "macos") {
        home.join("Library").join("Application Support")
    } else {
        host.var("XDG_DATA_HOME")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .map(|v| py_path(&v.display().to_string()))
            .unwrap_or_else(|| home.join(".local").join("share"))
    };
    let mut candidates = vec![base.join("pseudolife-mcp"), home.join(".pseudolife-mcp")];
    if let Some(custom) = host
        .var("PSEUDOLIFE_MCP_DATA_DIR")
        .filter(|v| !v.is_empty())
    {
        candidates.insert(0, py_path(&custom));
    }
    let mut unique = Vec::new();
    for candidate in candidates {
        if !unique.contains(&candidate) {
            unique.push(candidate);
        }
    }
    unique
}

/// `preflight_lite.refusal`: an initialized lite bank the Docker install
/// would not migrate.
fn lite_refusal(
    host: &dyn Host,
    containers: &[Value],
    configured: bool,
    recorded_volumes: bool,
) -> Option<String> {
    let bank = lite_data_dirs(host)
        .into_iter()
        .find(|p| host.is_file(&p.join("embedded_pg/PG_VERSION")))?;
    let own_daemons: Vec<&Value> = containers
        .iter()
        .filter(|c| {
            c["Name"].as_str().unwrap_or("").trim_start_matches('/') == "pseudolife-mcp-daemon"
                && label(c, "com.docker.compose.project") == Some(PROJECT)
        })
        .collect();
    let own_listener = own_daemons.iter().any(|c| publishes(c, "8765"));
    // A completed Docker migration can leave a stopped lite cluster behind:
    // it must not prevent an ordinary rerun.
    if configured
        && (!own_daemons.is_empty() || recorded_volumes)
        && (own_listener || !host.port_busy(8765))
    {
        return None;
    }
    let bank = bank.display();
    Some(format!(
        "An initialized lite bank exists at {bank}. Docker installation would not migrate it. Migrate it first: stop the lite daemon and clients, run pseudolife-mcp backup --data-dir \"{bank}\" --out <new-backup-dir-outside-lite-data>, verify its Postgres is stopped, move only embedded_pg to a preserved sibling, install Docker, then explicitly restore both artifacts; the complete sequence is docs/guide/configuration.md#move-a-lite-bank-to-docker (https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/master/docs/guide/configuration.md#move-a-lite-bank-to-docker). Keep the lite bank and its backups until the Docker bank is verified. Nothing changed."
    ))
}

fn mounted_volumes(container: &Value) -> BTreeSet<String> {
    container["Mounts"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|m| m["Type"].as_str() == Some("volume"))
        .filter_map(|m| m["Name"].as_str().map(str::to_owned))
        .collect()
}

/// `check(env_file)`.
pub(super) fn check(host: &dyn Host, repo: &Path, env_file: &Path) -> Result<()> {
    let ids_text = host.docker(&["ps", "-a", "--format", "{{.ID}}"], None, None)?;
    let ids: Vec<&str> = ids_text.split_whitespace().collect();
    let containers: Vec<Value> = if ids.is_empty() {
        Vec::new()
    } else {
        let mut args = vec!["inspect"];
        args.extend(&ids);
        let text = host.docker(&args, None, None)?;
        match serde_json::from_str::<Value>(&text) {
            Ok(Value::Array(items)) => items,
            _ => return refuse(UNREADABLE),
        }
    };
    let (recorded, managed) = environment(host, env_file)?;
    let values = resolved_environment(host, repo, env_file)?;
    let (bank, state) = volume_names(&values)?;
    for c in &containers {
        let name = c["Name"]
            .as_str()
            .ok_or_else(|| Refusal(UNREADABLE.into()))?
            .trim_start_matches('/');
        let project = label(c, "com.docker.compose.project").unwrap_or("");
        let ours = project == PROJECT;
        if name.starts_with("pseudolife-mcp-") && !ours {
            return refuse(format!(
                "Container {name} belongs to another stack (Compose project: {}). Use that stack's own compose file to update it; for clients only, rerun with --client-only --daemon-url <url> (PowerShell: -ClientOnly -DaemonUrl <url>). Nothing changed.",
                if project.is_empty() {
                    "unlabelled"
                } else {
                    project
                }
            ));
        }
        if name == "pseudolife-mcp-daemon" && ours {
            check_daemon_authority(host, c, env_file, &values)?;
        }
        let mounted = mounted_volumes(c);
        if (mounted.contains(&bank) || mounted.contains(&state)) && !ours {
            return refuse(
                "The selected bank/state volume is attached to another stack. Use that stack's own compose file; connect clients with --client-only --daemon-url <url> (PowerShell: -ClientOnly -DaemonUrl <url>). Nothing changed.",
            );
        }
        if name == "pseudolife-mcp-postgres" && ours {
            let pg_env = env_map(c);
            let expected = values
                .get("POSTGRES_PASSWORD")
                .filter(|v| !v.is_empty())
                .cloned()
                .unwrap_or_else(|| "pseudolife".into());
            if running(c) {
                // Postgres initdb can trust localhost, including TCP loopback;
                // its bridge address uses the same host rule as the daemon.
                let address = c["NetworkSettings"]["Networks"]
                    .as_object()
                    .into_iter()
                    .flat_map(|networks| networks.values())
                    .filter_map(|n| n["IPAddress"].as_str())
                    .find(|a| !a.is_empty());
                let Some(address) = address else {
                    return refuse(
                        "Cannot verify Postgres authentication without its Docker bridge address. Run docker info and start the original stack with its own compose file, then rerun. Nothing changed.",
                    );
                };
                let id = c["Id"].as_str().ok_or_else(|| Refusal(UNREADABLE.into()))?;
                // stdin keeps the credential out of argv and preserves dollars;
                // Config.Env records initialization, not a later ALTER USER.
                host.docker(
                    &[
                        "exec", "-i", id, "sh", "-c",
                        "PGPASSWORD=$(cat; printf x); PGPASSWORD=${PGPASSWORD%x}; export PGPASSWORD; exec psql -h \"$1\" -U pseudolife -d pseudolife_memory -Atqc \"SELECT 1\"",
                        "preflight", address,
                    ],
                    Some(&expected),
                    Some("Postgres did not accept the configured POSTGRES_PASSWORD. Restore POSTGRES_PASSWORD from the original ops/.env, then rerun. Nothing changed."),
                )?;
            } else if pg_env.get("POSTGRES_PASSWORD") != Some(&expected) {
                return refuse(
                    "The stopped Postgres container records a different initialization password; a later password rotation cannot be checked while stopped. Start the original stack with its own compose file, then rerun this preflight. Nothing changed.",
                );
            }
            if !mounted.is_empty() && !mounted.contains(&bank) {
                return refuse(
                    "PSEUDOLIFE_BANK_VOLUME does not name the existing Postgres bank. Restore PSEUDOLIFE_BANK_VOLUME from the original ops/.env, then rerun. Nothing changed.",
                );
            }
        }
    }
    let volumes: BTreeSet<String> = host
        .docker(&["volume", "ls", "--format", "{{.Name}}"], None, None)?
        .lines()
        .map(str::to_owned)
        .collect();
    let selected = [
        ("PSEUDOLIFE_BANK_VOLUME", bank.clone()),
        ("PSEUDOLIFE_STATE_VOLUME", state.clone()),
    ];
    for ((key, volume), legacy) in selected
        .iter()
        .zip(["ops_pseudolife_pgdata", "ops_pseudolife_data"])
    {
        if volumes.contains(legacy)
            && !volumes.contains(volume)
            && recorded.get(*key).is_none_or(String::is_empty)
        {
            return refuse(format!(
                "Pre-rename volume {legacy} exists. Set {key}={legacy} in the original ops/.env, keep its POSTGRES_PASSWORD, then rerun. Nothing changed."
            ));
        }
    }
    for (key, volume) in &selected {
        let default = if *key == FIELDS[0] {
            "pseudolife-mcp-bank"
        } else {
            "pseudolife-mcp-state"
        };
        if volumes.contains(volume)
            && !(recorded.get(*key).is_some_and(|v| !v.is_empty())
                || (managed && volume == default))
        {
            return refuse(format!(
                "Existing volume {volume} was not recorded by this install; its bank and password may belong to another install. Restore the original ops/.env (including POSTGRES_PASSWORD and PSEUDOLIFE_BANK_VOLUME/STATE_VOLUME), then rerun. Nothing changed; do not delete the volume."
            ));
        }
    }
    if let Some(message) = lite_refusal(
        host,
        &containers,
        host.is_file(env_file),
        managed && volumes.contains(&bank) && volumes.contains(&state),
    ) {
        return refuse(message);
    }
    for port in [8765u16, 5433] {
        let text = port.to_string();
        let bound_by_us = containers.iter().any(|c| {
            label(c, "com.docker.compose.project") == Some(PROJECT) && publishes(c, &text)
        });
        if host.port_busy(port) && !bound_by_us {
            return refuse(format!(
                "Port {port} is already in use by another service. Stop or reconfigure the service listening on {port}, then rerun. Nothing changed."
            ));
        }
    }
    Ok(())
}

pub(super) fn run(args: &[String]) -> u8 {
    let (mut repo, mut env_file) = (None, None);
    let mut items = args.iter();
    while let Some(item) = items.next() {
        match (item.as_str(), items.next()) {
            ("--repo", Some(value)) => repo = Some(PathBuf::from(value)),
            ("--env-file", Some(value)) => env_file = Some(PathBuf::from(value)),
            _ => {
                let _ = writeln!(
                    io::stderr().lock(),
                    "installer preflight-check: usage: --repo R --env-file F"
                );
                return 2;
            }
        }
    }
    let (Some(repo), Some(env_file)) = (repo, env_file) else {
        let _ = writeln!(
            io::stderr().lock(),
            "installer preflight-check: usage: --repo R --env-file F"
        );
        return 2;
    };
    match check(&RealHost, &repo, &env_file) {
        Ok(()) => u8::from(writeln!(io::stdout().lock(), "{OK_LINE}").is_err()),
        Err(Refusal(message)) => {
            let _ = writeln!(io::stderr().lock(), "FAIL {message}");
            1
        }
    }
}

#[cfg(test)]
#[path = "preflight_tests.rs"]
mod tests;
