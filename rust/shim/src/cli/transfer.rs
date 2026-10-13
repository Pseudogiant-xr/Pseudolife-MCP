//! `export` / `import`: the logical bank transfer (`transfer_cli.py`) on an
//! explicit `PSEUDOLIFE_MCP_DATABASE_URL`.
//!
//! The embedded lite tier and imports into a bank below this build's schema
//! defer by name before any effect; every non-canonical argv or input shape
//! defers through the dispatcher's generic line.
use std::{
    ffi::OsString,
    io::{self, Write},
    path::{Path, PathBuf},
    process::ExitCode,
};

mod export;
mod import;
mod json;
mod sql;

const EXPORT_HELP: &str = include_str!("transfer/export_help.txt");
const IMPORT_HELP: &str = include_str!("transfer/import_help.txt");
const NO_DATABASE: &str = "no database configured — set PSEUDOLIFE_MCP_DATABASE_URL (Docker tier: postgresql://pseudolife:<POSTGRES_PASSWORD from ops/.env>@127.0.0.1:5433/pseudolife_memory) or use a lite data dir that holds a bank.\n";

/// `transfer_cli.py` table roster and meta policy, in its order.
pub(super) const EXPORTED_TABLES: [&str; 26] = [
    "meta",
    "episodes",
    "entries",
    "entry_reinstatement_decisions",
    "entities",
    "entity_aliases",
    "relations",
    "edges",
    "edge_evidence",
    "edge_proposals",
    "entity_proposals",
    "entity_kinds",
    "dismissed_pairs",
    "facts",
    "world_facts",
    "lessons",
    "outcome_signals",
    "communities",
    "entity_communities",
    "memory_traces",
    "memory_trace_invalidations",
    "entity_sources",
    "merge_decisions",
    "chronicle_events",
    "curation_judgments",
    "store_decisions",
];
pub(super) const EXCLUDED_TABLES: [&str; 17] = [
    "dream_runs",
    "dream_run_slots",
    "retrieval_events",
    "retrieval_uses",
    "slot_reads",
    "lesson_search_events",
    "client_sessions",
    "coordination_agents",
    "coordination_messages",
    "coordination_events",
    "coordination_leases",
    "coordination_lease_waiters",
    "coordination_wakes",
    "principals",
    "maintainer_passkeys",
    "maintainer_bootstrap",
    "maintainer_nonces",
];
const META_SKIP_KEYS: [&str; 7] = [
    "schema_version",
    "active_session_pointer",
    "dream_ack_secret_v1",
    "coordination_hlc_highwater",
    "coordination_bank_id",
    "writer_lease_epoch",
    "maintainer_secret_v1",
];

pub(super) fn skip_meta_key(key: &str) -> bool {
    META_SKIP_KEYS.contains(&key) || key.ends_with("_schema_version")
}

/// How one transfer ended; printing and exit codes live in [`run`].
pub(super) enum Outcome {
    Done(String),
    /// `TransferError`: `"{mode} refused: {message}"` on stderr, exit 1.
    Refused(String),
    /// A shape outside this candidate: the dispatcher's generic deferral.
    Deferred,
    /// The named deferral for a bank not at this build's schema.
    SchemaDeferred,
    /// A PostgreSQL failure after the work began (`native-pg-diagnostics`).
    Failed,
    /// Import only: the COMMIT's answer was FATAL or lost, so the import may
    /// have become durable. The oracle raises out of `conn.transaction()`
    /// (a traceback, exit 1) and claims nothing either way.
    CommitUnknown,
    /// A filesystem failure after export began writing (`native-io-diagnostics`).
    FailedIo,
}

enum Request {
    Help,
    Export {
        out: Option<String>,
        data_dir: Option<OsString>,
    },
    Import {
        archive: String,
        force: bool,
        data_dir: Option<OsString>,
    },
}

/// An option value argparse would take as given: never empty or dash-led.
fn option_value(value: Option<&OsString>) -> Option<OsString> {
    let value = value?;
    let text = value.to_str()?;
    (!text.is_empty() && !text.starts_with('-')).then(|| value.clone())
}

fn parse(mode: &str, arguments: &[OsString]) -> Option<Request> {
    if arguments.len() == 1 && arguments[0] == "--help" {
        return Some(Request::Help);
    }
    let (mut out, mut data_dir, mut archive, mut force) = (None, None, None, false);
    let mut values = arguments.iter();
    while let Some(argument) = values.next() {
        match argument.to_str()? {
            "--out" if mode == "export" && out.is_none() => {
                out = Some(option_value(values.next())?.into_string().ok()?);
            }
            "--data-dir" if data_dir.is_none() => data_dir = Some(option_value(values.next())?),
            "--force" if mode == "import" && !force => force = true,
            value
                if mode == "import"
                    && archive.is_none()
                    && !value.is_empty()
                    && !value.starts_with('-') =>
            {
                archive = Some(value.to_owned());
            }
            _ => return None,
        }
    }
    Some(if mode == "export" {
        Request::Export { out, data_dir }
    } else {
        Request::Import {
            archive: archive?,
            force,
            data_dir,
        }
    })
}

/// True when Python's `str(Path(value)) == value`, so printed paths and the
/// `.part` sibling are spelled exactly as given. Other spellings defer.
#[cfg(windows)]
pub(super) fn canonical_path(value: &str) -> bool {
    const RESERVED: [&str; 22] = [
        "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8",
        "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    ];
    let rest = match value.as_bytes() {
        [drive, b':', b'\\', ..] if drive.is_ascii_alphabetic() => &value[3..],
        _ => value,
    };
    !rest.is_empty()
        && rest.split('\\').all(|part| {
            let stem = part.split('.').next().unwrap_or("").to_ascii_lowercase();
            !part.is_empty()
                && part != "."
                && !part.ends_with(['.', ' '])
                && !RESERVED.contains(&stem.as_str())
                && !part
                    .chars()
                    .any(|c| c < ' ' || matches!(c, '<' | '>' | ':' | '"' | '/' | '|' | '?' | '*'))
        })
}

#[cfg(not(windows))]
pub(super) fn canonical_path(value: &str) -> bool {
    let rest = value.strip_prefix('/').unwrap_or(value);
    !rest.is_empty()
        && !value.contains('\0')
        && rest.split('/').all(|part| !part.is_empty() && part != ".")
}

/// `embedded_pg.default_lite_data_dir()`; `None` when the oracle would reach
/// an account database lookup this leaf does not reproduce.
fn lite_default_dir() -> Option<PathBuf> {
    let set = |name: &str| std::env::var_os(name).filter(|value| !value.is_empty());
    let base = if cfg!(windows) {
        PathBuf::from(set("LOCALAPPDATA")?)
    } else if cfg!(target_os = "macos") {
        PathBuf::from(set("HOME")?)
            .join("Library")
            .join("Application Support")
    } else {
        match set("XDG_DATA_HOME") {
            Some(value) => PathBuf::from(value),
            None => PathBuf::from(set("HOME")?).join(".local").join("share"),
        }
    };
    Some(base.join("pseudolife-mcp"))
}

/// Without a DSN: `Some(true)` for a lite bank the oracle may attach to (a
/// named deferral), `Some(false)` for the "no database configured" refusal.
fn lite_bank(data_dir: Option<&OsString>) -> Option<bool> {
    let data_dir = match data_dir {
        Some(value) => PathBuf::from(value),
        None => match std::env::var_os("PSEUDOLIFE_MCP_DATA_DIR").filter(|v| !v.is_empty()) {
            Some(value) => PathBuf::from(value),
            None => {
                // With pg0 installed the oracle uses an existing lite default;
                // without it no lite bank is ever attached, so checking the
                // default's marker is exact for both installs.
                let lite = lite_default_dir()?;
                if lite.try_exists().ok()? {
                    lite
                } else {
                    std::env::current_dir().ok()?.join("data")
                }
            }
        },
    };
    data_dir
        .join("embedded_pg")
        .join("PG_VERSION")
        .try_exists()
        .ok()
}

fn emit(stream: &mut dyn Write, text: &str) -> bool {
    stream
        .write_all(&super::text_bytes(text))
        .and_then(|()| stream.flush())
        .is_ok()
}

/// CPython's exit when its interpreter-shutdown flush of a refused stdout
/// fails: the oracle's few report lines (and argparse's help) sit in its
/// 8192-byte stdout buffer until then, so the run itself completed.
const EXIT_STDOUT_REFUSED: u8 = 120;

fn finish(mode: &str, outcome: Outcome) -> Option<ExitCode> {
    let (ok, code) = match outcome {
        Outcome::Deferred => return None,
        Outcome::Done(text) => {
            return Some(ExitCode::from(if emit(&mut io::stdout().lock(), &text) {
                0
            } else {
                EXIT_STDOUT_REFUSED
            }));
        }
        Outcome::Refused(message) => (
            emit(
                &mut io::stderr().lock(),
                &format!("{mode} refused: {message}\n"),
            ),
            1,
        ),
        Outcome::SchemaDeferred => (
            emit(
                &mut io::stderr().lock(),
                "pseudolife-stdio: import into a bank not at this build's schema (55) is deferred in this candidate (needs native ensure_schema)\n",
            ),
            1,
        ),
        Outcome::Failed => (
            emit(
                &mut io::stderr().lock(),
                &format!(
                    "pseudolife-stdio: {mode} failed (native-pg-diagnostics); nothing was committed\n"
                ),
            ),
            1,
        ),
        Outcome::CommitUnknown => (
            emit(
                &mut io::stderr().lock(),
                &format!(
                    "pseudolife-stdio: {mode} lost the answer to its COMMIT (native-pg-diagnostics); it may have been committed: inspect the target bank before retrying\n"
                ),
            ),
            1,
        ),
        Outcome::FailedIo => (
            emit(
                &mut io::stderr().lock(),
                &format!(
                    "pseudolife-stdio: {mode} failed (native-io-diagnostics); no archive was written\n"
                ),
            ),
            1,
        ),
    };
    Some(if ok {
        ExitCode::from(code)
    } else {
        ExitCode::FAILURE
    })
}

pub(super) fn run(mode: &str, arguments: Vec<OsString>) -> Option<ExitCode> {
    let request = parse(mode, &arguments)?;
    let data_dir = match &request {
        Request::Help => {
            if std::env::var_os("COLUMNS") != Some(OsString::from("80")) {
                return None;
            }
            let help = if mode == "export" {
                EXPORT_HELP
            } else {
                IMPORT_HELP
            };
            return finish(mode, Outcome::Done(help.into()));
        }
        Request::Export { data_dir, .. } | Request::Import { data_dir, .. } => data_dir.as_ref(),
    };
    let dsn = match std::env::var_os("PSEUDOLIFE_MCP_DATABASE_URL").filter(|v| !v.is_empty()) {
        Some(dsn) => dsn.into_string().ok()?,
        None => {
            if lite_bank(data_dir)? {
                let message = format!(
                    "pseudolife-stdio: mode '{mode}' on the embedded lite tier is deferred in this candidate (needs native embedded_pg)\n"
                );
                let ok = emit(&mut io::stderr().lock(), &message);
                return Some(if ok {
                    ExitCode::from(1)
                } else {
                    ExitCode::FAILURE
                });
            }
            let ok = emit(&mut io::stderr().lock(), NO_DATABASE);
            return Some(if ok {
                ExitCode::from(1)
            } else {
                ExitCode::FAILURE
            });
        }
    };
    // The oracle still resolves `_default_data_dir` with a DSN: the lite
    // default (home) and, unless pg0 finds that dir, `Path.cwd()`. Either
    // failing is a Python traceback, so it defers.
    if data_dir.is_none()
        && std::env::var_os("PSEUDOLIFE_MCP_DATA_DIR").is_none_or(|v| v.is_empty())
    {
        lite_default_dir()?;
        std::env::current_dir().ok()?;
    }
    let dsn = crate::pg::Dsn::parse(&dsn).ok()?;
    let outcome = match request {
        Request::Help => unreachable!("help returned above"),
        Request::Export { out, .. } => {
            let out = export::destination(out)?;
            runtime()?.block_on(export::run(&dsn, &out))
        }
        Request::Import { archive, force, .. } => {
            if !canonical_path(&archive) {
                return None;
            }
            runtime()?.block_on(import::run(&dsn, Path::new(&archive), &archive, force))
        }
    };
    finish(mode, outcome)
}

fn runtime() -> Option<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .ok()
}
