//! The lite tier at daemon startup (Python's
//! `storage.embedded_pg.resolve_daemon_storage`): with no DSN, the bank is
//! the embedded PostgreSQL in the per-user data dir, started or attached
//! through `embedded::attach_or_start` on the pinned runtime, which the
//! first start provisions when the installer has not
//! (`rust/daemon/spec-lite-storage.md`, maintainer decisions D1 and 1-8).
//!
//! The daemon's PostgreSQL client answers a server's cleartext password
//! request, so every connection to the lite server is preceded by a probe
//! that requires SCRAM (`verify_before_connect`, maintainer decision B,
//! 2026-10-26); the short gap between probe and connect is a declared
//! divergence.

use super::embedded::{self, Embedded, Runtime, SecretStore};
use super::native_provision::{self as native, Platform};
use super::{pg_runtime, secret_file};
use std::{
    ffi::OsString,
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
};

/// Where storage resolution put the bank.
#[derive(Debug, PartialEq, Eq)]
pub enum Storage {
    /// `PSEUDOLIFE_MCP_DATABASE_URL`, trusted as given (as Python does).
    Explicit(String),
    /// The lite tier's embedded PostgreSQL under this data dir.
    Lite(PathBuf),
}

const FILES_REFUSAL: &str = "PSEUDOLIFE_MCP_STORAGE=files selects file mode, which the Rust \
     daemon does not have (file mode retires with the Rust cutover). Unset \
     PSEUDOLIFE_MCP_STORAGE to keep the bank in the lite tier's embedded PostgreSQL, or set \
     PSEUDOLIFE_MCP_DATABASE_URL to a PostgreSQL server.";

/// `resolve_daemon_storage`'s choice, before anything starts: a nonempty
/// DSN wins; otherwise `PSEUDOLIFE_MCP_STORAGE` (stripped, lowercased,
/// default `auto`) must be `auto`, and the data dir is
/// `PSEUDOLIFE_MCP_DATA_DIR` or the per-user default. Python's `files`
/// mode is refused, naming what replaces it.
pub fn resolve(
    env: &dyn Fn(&str) -> Option<String>,
    home: Option<&Path>,
) -> Result<Storage, String> {
    let set = |name: &str| env(name).filter(|value| !value.is_empty());
    if let Some(dsn) = set("PSEUDOLIFE_MCP_DATABASE_URL") {
        return Ok(Storage::Explicit(dsn));
    }
    let mode = set("PSEUDOLIFE_MCP_STORAGE")
        .unwrap_or_else(|| "auto".to_owned())
        .trim()
        .to_lowercase();
    match mode.as_str() {
        "auto" => {}
        "files" => return Err(FILES_REFUSAL.to_owned()),
        _ => {
            return Err(format!(
                "PSEUDOLIFE_MCP_STORAGE={} is not recognized; expected 'auto' (the default). \
                 File mode ('files') retires with the Rust cutover.",
                crate::storage::py_repr(&mode)
            ));
        }
    }
    let data_dir = match set("PSEUDOLIFE_MCP_DATA_DIR") {
        Some(dir) => PathBuf::from(dir),
        None => native::default_data_dir(
            Platform::current(),
            &|name| env(name).map(OsString::from),
            home,
        )
        .ok_or(
            "no home directory is known for the lite tier's data dir; set \
                PSEUDOLIFE_MCP_DATA_DIR to the directory to keep the bank in",
        )?,
    };
    Ok(Storage::Lite(data_dir))
}

/// The bank's password file, through the shared owner-only writer and
/// reader (`shared/secret_file.rs`).
struct DaemonSecrets;
impl SecretStore for DaemonSecrets {
    fn read(&self, path: &Path) -> Result<Option<String>, String> {
        secret_file::read_secret(path)
    }
    fn create(&self, path: &Path) -> Result<String, String> {
        secret_file::create(path)
    }
}

/// The running lite bank: the server handle (stopped at exit only if this
/// process started it) and the runtime it runs on.
pub struct Lite {
    embedded: Embedded,
    runtime: Runtime,
    pub data_dir: PathBuf,
}
impl Lite {
    /// The DSN for the daemon's client; every connection it makes is
    /// probed first (`verify_before_connect`).
    pub fn client_dsn(&self) -> String {
        self.embedded.client_dsn()
    }
    pub fn owned(&self) -> bool {
        self.embedded.owned()
    }
}

/// The installed pinned runtime, or the first-start install of it
/// (decision 2: the installers provision it; a start without it does).
fn runtime(home: Option<&Path>, log: &mut dyn FnMut(&str)) -> Result<Runtime, String> {
    let artifacts = pg_runtime::current_artifacts().map_err(|e| e.to_string())?;
    let env = |name: &str| std::env::var_os(name);
    let root = pg_runtime::default_root(Platform::current(), &env, home).ok_or(
        "no home directory is known for the lite tier's PostgreSQL runtime; set \
         PSEUDOLIFE_MCP_DATABASE_URL to a PostgreSQL server instead",
    )?;
    match pg_runtime::locate(&root, artifacts).map_err(|e| e.to_string())? {
        Some(runtime) => Ok(runtime),
        None => {
            log(&format!(
                "storage: installing the lite tier's PostgreSQL {} runtime (one time) in {}",
                pg_runtime::PG_VERSION,
                root.display()
            ));
            pg_runtime::provision(&root, artifacts, &native::HttpFetch, log)
                .map(|(runtime, _)| runtime)
                .map_err(|e| {
                    format!(
                        "{e} (`pseudolife-daemon provision postgresql` installs it ahead of \
                         a start)"
                    )
                })
        }
    }
}

/// Start or attach the lite bank for `data_dir`, refuse it if it holds
/// file-mode state only Python can import (decision 6), and register it
/// for the connection probe and for the stop at exit.
pub fn start(
    data_dir: &Path,
    home: Option<&Path>,
    log: &mut dyn FnMut(&str),
) -> Result<String, String> {
    let runtime = runtime(home, log)?;
    let embedded =
        embedded::attach_or_start(data_dir, &runtime, &DaemonSecrets).map_err(|e| e.to_string())?;
    let lite = Lite {
        embedded,
        runtime,
        data_dir: data_dir.to_path_buf(),
    };
    if let Err(refusal) = legacy_state(&lite) {
        stop_lite(lite);
        return Err(refusal);
    }
    let (pgdata, port, password) = lite.embedded.identity();
    let _ = PROBE.set(Probe {
        runtime: lite.runtime.clone(),
        pgdata,
        port,
        password,
    });
    let dsn = lite.client_dsn();
    *RUNNING.lock().unwrap_or_else(|e| e.into_inner()) = Some(lite);
    Ok(dsn)
}

/// Decision 6: the Rust daemon does not import `.pt` state. A data dir
/// whose legacy files the oracle would still import into this bank
/// (`storage/migrate.py` `migrate_legacy`: the files are there, the
/// `legacy_migration` meta record is not `done`, and either an import is in
/// progress or the bank holds no entries or facts) is refused, naming the
/// command that imports them. A bank with entries and no import under way
/// is served, as Python serves it (it skips the files).
fn legacy_state(lite: &Lite) -> Result<(), String> {
    let sources: Vec<PathBuf> = [
        lite.data_dir.join("memory_state").join("cms_state.pt"),
        lite.data_dir.join("cortex_state.pt"),
    ]
    .into_iter()
    .filter(|path| path.exists())
    .collect();
    if sources.is_empty() {
        return Ok(());
    }
    let query = |sql: &str| {
        lite.embedded
            .query(&lite.runtime, sql)
            .map(|rows| rows.trim().to_owned())
            .map_err(|e| format!("could not read the lite bank's import state: {e}"))
    };
    let tables = query(
        "SELECT to_regclass('public.meta') IS NOT NULL, to_regclass('public.entries') IS NOT \
         NULL, to_regclass('public.facts') IS NOT NULL",
    )?;
    let present: Vec<bool> = tables.split('|').map(|cell| cell == "t").collect();
    let [meta, entries, facts] = present[..] else {
        return Err(format!(
            "could not read the lite bank's tables ({tables:?})"
        ));
    };
    let status = if meta {
        query(
            "SELECT coalesce((SELECT value->>'status' FROM meta WHERE key = 'legacy_migration'), '')",
        )?
    } else {
        String::new()
    };
    let has_rows = |table: &str, present: bool| -> Result<bool, String> {
        Ok(present && query(&format!("SELECT EXISTS (SELECT 1 FROM {table})"))? == "t")
    };
    let pending = match status.as_str() {
        "done" => false,
        "in_progress" => true,
        _ => !(has_rows("entries", entries)? || has_rows("facts", facts)?),
    };
    if !pending {
        return Ok(());
    }
    let files: Vec<String> = sources.iter().map(|p| p.display().to_string()).collect();
    Err(format!(
        "the lite data dir {} holds file-mode state that was never imported into its bank ({}). \
         The Rust daemon does not import .pt files: start the last Python release once \
         (`pseudolife-mcp serve` with the same data dir), which imports them and renames them \
         to .pre-v8.bak, then start this daemon again.",
        lite.data_dir.display(),
        files.join(", ")
    ))
}

/// What the probe before each connection needs.
struct Probe {
    runtime: Runtime,
    pgdata: PathBuf,
    port: u16,
    password: String,
}
static PROBE: OnceLock<Probe> = OnceLock::new();
/// The lite bank, held until exit so the server this process started is
/// stopped (`shutdown`).
static RUNNING: Mutex<Option<Lite>> = Mutex::new(None);

/// Before any connection to the lite server: prove, with SCRAM required,
/// that the server on its port is still this bank's. Connections to any
/// other host or port (an explicit DSN) are not probed.
pub async fn verify_before_connect(
    config: &tokio_postgres::Config,
    timeout: std::time::Duration,
) -> Result<(), String> {
    let Some(probe) = PROBE.get() else {
        return Ok(());
    };
    let ours = config.get_ports().contains(&probe.port)
        && config.get_hosts().iter().any(
            |host| matches!(host, tokio_postgres::config::Host::Tcp(name) if name == "127.0.0.1"),
        );
    if !ours {
        return Ok(());
    }
    // Within the connection's own deadline: psql's connect timeout is set
    // from it, and the wait is bounded by it too (a probe that outlives the
    // deadline ends at that connect timeout on its own).
    let check = tokio::task::spawn_blocking(move || {
        embedded::serves_within(
            &probe.runtime,
            probe.port,
            &probe.password,
            &probe.pgdata,
            timeout,
        )
    });
    let verified = matches!(
        tokio::time::timeout(timeout + std::time::Duration::from_secs(1), check).await,
        Ok(Ok(true))
    );
    if verified {
        Ok(())
    } else {
        Err(format!(
            "the server on 127.0.0.1:{} did not prove it is the lite bank's own (SCRAM with \
             the bank's secret, serving {}); not connecting",
            probe.port,
            probe.pgdata.display()
        ))
    }
}

/// Make every daemon connection pass `verify_before_connect` (called once,
/// after a lite start).
pub fn register_connect_guard() {
    let _ = crate::storage::CONNECT_GUARD.set(Box::new(|config, timeout| {
        Box::pin(async move { verify_before_connect(&config, timeout).await })
    }));
}

fn stop_lite(lite: Lite) {
    if lite.owned()
        && let Err(e) = lite.embedded.stop()
    {
        eprintln!("storage: could not stop the lite bank's server: {e}");
    }
}

/// At exit: stop the lite server if this process started it (an attached
/// server is another process's and keeps running, as in Python).
pub fn shutdown() {
    let lite = RUNNING.lock().unwrap_or_else(|e| e.into_inner()).take();
    if let Some(lite) = lite {
        stop_lite(lite);
    }
}

/// `pseudolife-daemon provision postgresql`: install the pinned runtime
/// ahead of a start (what the installers run).
pub fn provision_command() -> i32 {
    let home = std::env::home_dir();
    let artifacts = match pg_runtime::current_artifacts() {
        Ok(artifacts) => artifacts,
        Err(e) => {
            eprintln!("pseudolife-daemon provision postgresql: {e}");
            return 1;
        }
    };
    let env = |name: &str| std::env::var_os(name);
    let Some(root) = pg_runtime::default_root(Platform::current(), &env, home.as_deref()) else {
        eprintln!(
            "pseudolife-daemon provision postgresql: no home directory is known for the \
             runtime; nothing was installed."
        );
        return 1;
    };
    match pg_runtime::provision(&root, artifacts, &native::HttpFetch, &mut |line| {
        println!("{line}")
    }) {
        Ok(_) => 0,
        Err(e) => {
            eprintln!("pseudolife-daemon provision postgresql: {e}");
            1
        }
    }
}

#[cfg(test)]
#[path = "lite_tests.rs"]
mod tests;
