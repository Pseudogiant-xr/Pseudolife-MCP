//! `pseudolife-mcp restore`: replace a Docker bank (its PostgreSQL database
//! and the daemon's `/data` state volume) from a backup.
//!
//! In order: the command line; the backup's completeness and the state
//! archive's safety, read to their ends before anything is touched; the
//! confirmation (`--yes`, or a yes at a real terminal); the update lock,
//! held to the end; the bank's selection and every read-only check
//! (`bank`); then the restore's steps (`execute`), which stop at the first
//! failure or interrupt and say what state the bank is in and the command
//! that recovers it. A lite (embedded PostgreSQL) bank is refused: restore
//! replaces a Docker bank only.
use std::{
    ffi::OsString,
    io::{self, BufRead, Write},
    path::{Path, PathBuf},
    process::{self, ExitCode},
    time::Duration,
};

use super::lease::lock as os_lock;

mod args;
mod bank;
mod conn;
mod docker;
mod dump;
mod execute;
mod gzip;
mod interrupt;
mod pathlib;
mod safety;
mod state;

#[cfg(test)]
mod execute_tests;

/// A file check's answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Verdict {
    Pass,
    Refuse,
}

const INCOMPLETE_DUMP: &str =
    "the backup is unreadable or incomplete; use a complete .sql.gz dump. Nothing was restored";
const UNSAFE_STATE: &str = "the state archive is unreadable or contains unsafe paths; use a complete, unmodified state backup. Nothing was restored";
const NEEDS_YES: &str =
    "restore replaces the existing bank; rerun with --yes to confirm. Nothing was restored";
const PROMPT: &str =
    "Restore replaces the existing bank after taking a safety backup. Continue? [y/N] ";

fn write_stream(stdout: bool, text: &str) -> io::Result<()> {
    let bytes = super::text_bytes(text);
    if stdout {
        let mut out = io::stdout().lock();
        out.write_all(&bytes)?;
        out.flush()
    } else {
        let mut err = io::stderr().lock();
        err.write_all(&bytes)?;
        err.flush()
    }
}

/// `restore failed: <message>`, exit 2.
fn refuse(message: &str) -> ExitCode {
    let _ = write_stream(false, &format!("restore failed: {message}\n"));
    ExitCode::from(2)
}

/// Every run is answered here: the dispatcher never defers `restore`.
pub(super) fn run(arguments: Vec<OsString>) -> Option<ExitCode> {
    Some(main(arguments))
}

fn main(arguments: Vec<OsString>) -> ExitCode {
    let args = match args::parse(&arguments) {
        args::Parsed::Args(args) => args,
        args::Parsed::Help => {
            return if write_stream(true, args::HELP).is_ok() {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(120)
            };
        }
        args::Parsed::Error(message) => {
            let _ = write_stream(
                false,
                &format!(
                    "{}pseudolife-mcp restore: error: {message}\n",
                    args::usage()
                ),
            );
            return ExitCode::from(2);
        }
    };
    if dump::complete(&args.backup_file) != Verdict::Pass {
        return refuse(INCOMPLETE_DUMP);
    }
    let mut state_size = 0;
    if let Some(archive) = &args.state_archive {
        match state::validate(archive) {
            Ok(size) => state_size = size,
            Err(state::Refusal::Unsafe) => return refuse(UNSAFE_STATE),
            Err(state::Refusal::Unsupported(what)) => {
                return refuse(&format!(
                    "the state archive contains {what}, which restore cannot check safely; re-create it as a plain GNU tar archive (tar --format=gnu -czf <archive> -C <state directory> .) and retry. Nothing was restored"
                ));
            }
        }
    }
    if !args.yes {
        if !super::connect::interactive() {
            return refuse(NEEDS_YES);
        }
        // A Ctrl-C here ends the process with nothing changed.
        if write_stream(true, PROMPT).is_err() {
            return refuse(NEEDS_YES);
        }
        let mut answer = String::new();
        let read = io::stdin().lock().read_line(&mut answer);
        let answer = answer.trim().to_ascii_lowercase();
        if read.is_err() || !matches!(answer.as_str(), "y" | "yes") {
            let _ = write_stream(true, "Restore cancelled; nothing was changed.\n");
            return ExitCode::SUCCESS;
        }
    }
    let Some(directory) = data_dir() else {
        return refuse(
            "cannot find your home directory for the update lock; set HOME (USERPROFILE on Windows) or PSEUDOLIFE_MCP_DATA_DIR, then retry. Nothing was restored",
        );
    };
    let mut lock = os_lock::Lock::new(directory.join("update.lock"));
    match lock.acquire() {
        Ok(true) => {}
        Ok(false) => return busy(&directory),
        Err(error) => {
            return refuse(&format!(
                "cannot take the update lock in {} ({error}); make that directory writable, then retry. Nothing was restored",
                directory.display()
            ));
        }
    }
    let holder = directory.join("update.lock.pid");
    let newline = if cfg!(windows) { "\r\n" } else { "\n" };
    let _ = std::fs::write(&holder, format!("{}{newline}", process::id()));
    let code = locked(&args, &directory, state_size);
    let _ = std::fs::remove_file(&holder);
    lock.release();
    code
}

/// The data directory: `PSEUDOLIFE_MCP_DATA_DIR`, else
/// `<home>/.pseudolife-mcp` (the update lock's home).
fn data_dir() -> Option<PathBuf> {
    let set = |key: &str| std::env::var_os(key).filter(|value| !value.is_empty());
    if let Some(dir) = set("PSEUDOLIFE_MCP_DATA_DIR") {
        return Some(PathBuf::from(dir));
    }
    let home = set("USERPROFILE").or_else(|| set("HOME"))?;
    Some(PathBuf::from(home).join(".pseudolife-mcp"))
}

/// Another update or restore holds the lock.
fn busy(directory: &Path) -> ExitCode {
    let text = std::fs::read_to_string(directory.join("update.lock.pid")).unwrap_or_default();
    let pid = text.trim();
    let pid = if !pid.is_empty() && pid.bytes().all(|b| b.is_ascii_digit()) {
        pid
    } else {
        "unknown"
    };
    refuse(&format!(
        "another pseudolife-mcp update is running (pid {pid}); nothing was changed. Run this again when it has finished"
    ))
}

/// The embedded PostgreSQL of a lite bank on this host, if there is one.
fn lite_bank() -> Option<PathBuf> {
    let set = |key: &str| std::env::var_os(key).filter(|value| !value.is_empty());
    let home = set("USERPROFILE")
        .or_else(|| set("HOME"))
        .map(PathBuf::from);
    let mut candidates = Vec::new();
    if let Some(dir) = set("PSEUDOLIFE_MCP_DATA_DIR") {
        candidates.push(PathBuf::from(dir));
    }
    let base = if cfg!(windows) {
        set("LOCALAPPDATA")
            .map(PathBuf::from)
            .or_else(|| home.as_ref().map(|h| h.join("AppData").join("Local")))
    } else if cfg!(target_os = "macos") {
        home.as_ref()
            .map(|h| h.join("Library").join("Application Support"))
    } else {
        set("XDG_DATA_HOME")
            .map(PathBuf::from)
            .or_else(|| home.as_ref().map(|h| h.join(".local").join("share")))
    };
    if let Some(base) = base {
        candidates.push(base.join("pseudolife-mcp"));
    }
    candidates
        .into_iter()
        .find(|dir| dir.join("embedded_pg").join("PG_VERSION").is_file())
}

fn lite_refusal(dir: &Path) -> ExitCode {
    refuse(&format!(
        "no Docker bank was found, and this host has a lite bank (embedded PostgreSQL in {}); restore replaces a Docker bank only. Install the Docker bank first (ops/install.sh, or ops\\install.ps1 on Windows; see \"Move a lite bank to Docker\" in docs/guide/configuration.md), then restore into it. Nothing was restored",
        dir.display()
    ))
}

/// The rest of the run, under the update lock.
fn locked(args: &args::Args, directory: &Path, state_size: u128) -> ExitCode {
    let program = match docker::program() {
        Ok(program) => program,
        Err(message) => {
            return match lite_bank() {
                Some(lite) => lite_refusal(&lite),
                None => refuse(message),
            };
        }
    };
    let _guard = interrupt::Guard::install();
    let mut runner = docker::Real {
        program,
        interruptible: true,
    };
    let mut host = RealHost;
    let run_tag = execute::Host::run_tag(&mut host);
    let plan = match bank::probe(&mut runner, args, &run_tag, &mut |dir| {
        bank::host_space(dir)
    }) {
        Ok(plan) => plan,
        Err(_) if interrupt::requested() => {
            let _ = write_stream(
                false,
                "restore failed: restore was interrupted before anything was changed. Nothing was restored\n",
            );
            return ExitCode::from(130);
        }
        Err(bank::Stop(message, code)) => {
            let daemon = args
                .daemon_container
                .as_deref()
                .filter(|d| !d.is_empty())
                .unwrap_or(bank::DAEMON_CONTAINER);
            let no_docker_bank = message
                .starts_with(&format!("the container {daemon} does not exist"))
                || message.starts_with("the Docker engine is not answering")
                || message.starts_with("the docker program could not be started");
            if no_docker_bank && let Some(lite) = lite_bank() {
                return lite_refusal(&lite);
            }
            let _ = write_stream(false, &format!("restore failed: {message}\n"));
            return ExitCode::from(code);
        }
    };
    let absolute = |path: &Path| std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf());
    let cx = execute::Context {
        args,
        plan: &plan,
        backup_file: absolute(&args.backup_file),
        state_archive: args.state_archive.as_deref().map(absolute),
        state_size,
        data_dir: directory.to_path_buf(),
    };
    ExitCode::from(execute::execute(&mut runner, &mut host, &cx))
}

struct RealHost;

impl execute::Host for RealHost {
    fn info(&mut self, line: &str) {
        let _ = write_stream(true, &format!("{line}\n"));
    }

    fn error(&mut self, line: &str) {
        let _ = write_stream(false, &format!("{line}\n"));
    }

    fn health(&mut self, url: &str) -> Option<serde_json::Value> {
        http_json(url, Duration::from_secs(3))
    }

    fn sleep(&mut self, duration: Duration) {
        let end = std::time::Instant::now() + duration;
        while std::time::Instant::now() < end && !interrupt::requested() {
            std::thread::sleep(Duration::from_millis(100));
        }
    }

    fn stamp(&mut self) -> String {
        let token = uuid::Uuid::new_v4().simple().to_string();
        format!(
            "{}-{}",
            chrono::Local::now().format("%Y%m%d-%H%M%S"),
            &token[..8]
        )
    }

    fn run_tag(&mut self) -> String {
        uuid::Uuid::new_v4().simple().to_string()[..16].to_owned()
    }

    fn now(&mut self) -> String {
        chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, false)
    }

    fn host_space(&mut self, directory: &str) -> bank::Step<()> {
        bank::host_space(directory)
    }
}

/// GET `url` (plain HTTP) and read a JSON object from a 200 answer. The
/// daemon's health endpoint needs no proxy or credentials.
fn http_json(url: &str, timeout: Duration) -> Option<serde_json::Value> {
    use std::io::Read;
    use std::net::{TcpStream, ToSocketAddrs};
    let rest = url.strip_prefix("http://")?;
    let (authority, path) = match rest.find('/') {
        Some(at) => (&rest[..at], &rest[at..]),
        None => (rest, "/"),
    };
    let address = if authority.starts_with('[')
        || authority
            .rsplit_once(':')
            .is_some_and(|(_, p)| p.bytes().all(|b| b.is_ascii_digit()))
    {
        authority.to_owned()
    } else {
        format!("{authority}:80")
    };
    // Every address the name resolves to (`localhost` is ::1 and 127.0.0.1;
    // a daemon may publish only one of them).
    let mut stream = address
        .to_socket_addrs()
        .ok()?
        .find_map(|target| TcpStream::connect_timeout(&target, timeout).ok())?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    let request = format!(
        "GET {path} HTTP/1.0\r\nHost: {authority}\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).ok()?;
    let mut reply = Vec::new();
    stream.take(1 << 20).read_to_end(&mut reply).ok()?;
    let split = reply.windows(4).position(|w| w == b"\r\n\r\n")?;
    let head = std::str::from_utf8(&reply[..split]).ok()?;
    let status = head.lines().next()?.split_whitespace().nth(1)?;
    if status != "200"
        || head
            .to_ascii_lowercase()
            .contains("transfer-encoding: chunked")
    {
        return None;
    }
    let value: serde_json::Value = serde_json::from_slice(&reply[split + 4..]).ok()?;
    value.is_object().then_some(value)
}

#[cfg(test)]
pub(super) mod tests {
    use std::path::PathBuf;

    /// A file in the temporary directory, removed on drop.
    pub(in crate::cli::restore_cli) struct Scratch(pub PathBuf);

    impl Scratch {
        pub fn new(bytes: &[u8]) -> Self {
            let path =
                std::env::temp_dir().join(format!("pl-restore-{}", uuid::Uuid::new_v4().simple()));
            std::fs::write(&path, bytes).unwrap();
            Self(path)
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }

    #[test]
    fn http_json_reads_a_health_answer() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut buffer = [0u8; 1024];
            let _ = socket.read(&mut buffer);
            socket
                .write_all(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"ok\",\"ready\":true}")
                .unwrap();
        });
        let value = super::http_json(
            &format!("http://127.0.0.1:{port}/health"),
            std::time::Duration::from_secs(5),
        )
        .unwrap();
        server.join().unwrap();
        assert_eq!(value["ready"], serde_json::json!(true));
        assert_eq!(
            super::http_json(
                "https://127.0.0.1:1/health",
                std::time::Duration::from_secs(1)
            ),
            None
        );
    }

    #[test]
    fn http_json_tries_every_address_a_name_resolves_to() {
        use std::io::{Read, Write};
        // `localhost` resolves to ::1 as well on most hosts; the daemon here
        // listens on IPv4 only, as one publishing 127.0.0.1:<port> does.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut buffer = [0u8; 1024];
            let _ = socket.read(&mut buffer);
            socket
                .write_all(b"HTTP/1.0 200 OK\r\n\r\n{\"status\":\"ok\",\"ready\":true}")
                .unwrap();
        });
        let value = super::http_json(
            &format!("http://localhost:{port}/health"),
            std::time::Duration::from_secs(5),
        );
        assert_eq!(
            value.map(|v| v["ready"].clone()),
            Some(serde_json::json!(true))
        );
        server.join().unwrap();
    }
}
