//! `export` / `import`: the logical bank transfer (`transfer_cli.py`) on an
//! explicit `PSEUDOLIFE_MCP_DATABASE_URL`.
//!
//! Inputs are handled natively or refused with a cause and repair step.
use std::{
    ffi::OsString,
    io::{self, Write},
    path::{Path, PathBuf},
    process::ExitCode,
};

mod export;
mod import;
mod json;
mod schema;
mod sql;

const EXPORT_HELP: &str = include_str!("transfer/export_help.txt");
const IMPORT_HELP: &str = include_str!("transfer/import_help.txt");
const NO_DATABASE: &str = "no database configured — set PSEUDOLIFE_MCP_DATABASE_URL (Docker tier: postgresql://pseudolife:<POSTGRES_PASSWORD from ops/.env>@127.0.0.1:5433/pseudolife_memory).\n";

/// `transfer_cli.py` table roster and meta policy, in its order.
pub(super) const EXPORTED_TABLES: [&str; 27] = [
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
    "reference_chunks",
];
pub(super) const EXCLUDED_TABLES: [&str; 22] = [
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
    "cms_counters",
    "principal_activity",
    "rejected_bearers",
    "console_sessions",
    "console_signin_codes",
];
const META_SKIP_KEYS: [&str; 9] = [
    "schema_version",
    "active_session_pointer",
    "flat_band_migration_v1",
    "dream_ack_secret_v1",
    "coordination_hlc_highwater",
    "coordination_bank_id",
    "writer_lease_epoch",
    "maintainer_secret_v1",
    "reference_chroma_import",
];

pub(super) fn skip_meta_key(key: &str) -> bool {
    META_SKIP_KEYS.contains(&key) || key.ends_with("_schema_version")
}

/// How one transfer ended; printing and exit codes live in [`run`].
pub(super) enum Outcome {
    Done(String),
    /// `TransferError`: `"{mode} refused: {message}"` on stderr, exit 1.
    Refused(String),
    /// The native schema guard refused before making changes.
    SchemaRefused(String),
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

fn parse(mode: &str, arguments: &[OsString]) -> Result<Request, String> {
    let mut out = None;
    let mut data_dir = None;
    let mut archive = None;
    let mut force = false;
    let mut positional = false;
    let mut values = arguments.iter();
    while let Some(argument) = values.next() {
        let text = argument.to_str().ok_or_else(|| {
            "argument is not valid Unicode — use a Unicode path or option.".to_owned()
        })?;
        if !positional && matches!(text, "--help" | "-h") {
            return Ok(Request::Help);
        }
        if !positional && text == "--" {
            positional = true;
            continue;
        }
        let (key, inline) = text
            .split_once('=')
            .map_or((text, None), |(k, v)| (k, Some(v)));
        if !positional && (key == "--data-dir" || (key == "--out" && mode == "export")) {
            let value = match inline {
                Some(value) => value.to_owned(),
                None => values
                    .next()
                    .and_then(|v| v.to_str())
                    .filter(|v| !v.starts_with('-'))
                    .ok_or_else(|| format!("{key} needs a value — pass {key}=<path>."))?
                    .to_owned(),
            };
            if value.is_empty() || value.contains('\0') {
                return Err(format!(
                    "{key} has an empty path or U+0000 — pass a nonempty filesystem path."
                ));
            }
            if key == "--out" {
                out = Some(value);
            } else {
                data_dir = Some(OsString::from(value));
            }
        } else if !positional && text == "--force" && mode == "import" {
            force = true;
        } else if mode == "import"
            && archive.is_none()
            && (positional || !text.starts_with('-'))
            && !text.is_empty()
            && !text.contains('\0')
        {
            archive = Some(text.to_owned());
        } else {
            return Err(format!(
                "unexpected argument {} — run pseudolife-mcp {mode} --help for supported arguments.",
                super::mode_repr(text)
            ));
        }
    }
    Ok(if mode == "export" {
        Request::Export { out, data_dir }
    } else {
        Request::Import {
            archive: archive.ok_or_else(|| {
                "archive path is missing — pass pseudolife-mcp import <archive.zip>.".to_owned()
            })?,
            force,
            data_dir,
        }
    })
}

/// Connection diagnostics omit DSNs and credentials.
fn database_refusal(error: crate::pg::Error) -> String {
    use crate::pg::Error;
    let fix = match &error {
        Error::UnsupportedOption(_) | Error::AmbientControl(_) => {
            "remove the named unsupported setting"
        }
        Error::RootCertificate | Error::HostnameRules => {
            "configure sslrootcert and a server certificate matching the DSN host"
        }
        Error::Connection | Error::SessionSetup | Error::Shutdown => {
            "restore access to the database configured by PSEUDOLIFE_MCP_DATABASE_URL and retry"
        }
        Error::InvalidOption(_) | Error::InvalidDsn => {
            "correct PSEUDOLIFE_MCP_DATABASE_URL and retry"
        }
    };
    format!("PSEUDOLIFE_MCP_DATABASE_URL: {error} — {fix}.")
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
        Outcome::SchemaRefused(message) => (
            emit(
                &mut io::stderr().lock(),
                &format!("pseudolife-stdio: import schema initialization refused: {message}\n"),
            ),
            1,
        ),
        Outcome::Failed => (
            emit(
                &mut io::stderr().lock(),
                &format!(
                    "pseudolife-stdio: {mode} failed (native-pg-diagnostics); {}\n",
                    if mode == "import" {
                        "archive rows were not committed"
                    } else {
                        "nothing was committed"
                    }
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
    let request = match parse(mode, &arguments) {
        Ok(request) => request,
        Err(message) => return finish(mode, Outcome::Refused(message)),
    };
    let data_dir = match &request {
        Request::Help => {
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
        Some(dsn) => match dsn.into_string() {
            Ok(dsn) => dsn,
            Err(_) => return finish(mode, Outcome::Refused("PSEUDOLIFE_MCP_DATABASE_URL is not valid Unicode — set a Unicode PostgreSQL DSN.".into())),
        },
        None => {
            let explicit = data_dir.map(PathBuf::from);
            if let Some(bank) = crate::cli::retired::find_lite_bank(explicit.as_deref()) {
                let message = format!(
                    "{mode} refused: {}\n",
                    crate::cli::retired::lite_bank_refusal(&bank)
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
    let dsn = match crate::pg::Dsn::parse_with_connection_options(&dsn) {
        Ok(dsn) => dsn,
        Err(error) => return finish(mode, Outcome::Refused(database_refusal(error))),
    };
    let runtime = match runtime() {
        Ok(runtime) => runtime,
        Err(error) => {
            return finish(
                mode,
                Outcome::Refused(format!(
                    "cannot initialize the transfer runtime: {error} — free operating-system resources and retry."
                )),
            );
        }
    };
    let outcome = match request {
        Request::Help => unreachable!("help returned above"),
        Request::Export { out, .. } => {
            let out = match export::destination(out) {
                Ok(out) => out,
                Err(message) => return finish(mode, Outcome::Refused(message)),
            };
            runtime.block_on(export::run(&dsn, &out))
        }
        Request::Import { archive, force, .. } => {
            runtime.block_on(import::run(&dsn, Path::new(&archive), &archive, force))
        }
    };
    finish(mode, outcome)
}

fn runtime() -> io::Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
}

#[cfg(all(test, unix))]
mod tests {
    #[test]
    fn runtime_resource_failure_is_refused() {
        const CHILD: &str = "PSEUDOLIFE_TRANSFER_RESOURCE_TEST";
        if std::env::var_os(CHILD).is_some() {
            assert_eq!(
                super::run("export", Vec::new()),
                Some(std::process::ExitCode::from(1))
            );
            return;
        }
        // Enter the transfer directly: the binary's unrelated startup runtime
        // otherwise consumes descriptors before dispatch can reach this code.
        let output = std::process::Command::new("/bin/sh")
            .args([
                "-c",
                "ulimit -n 4; exec \"$1\" --exact cli::transfer::tests::runtime_resource_failure_is_refused --nocapture",
                "transfer-resource-test",
            ])
            .arg(std::env::current_exe().unwrap())
            .env_clear()
            .env(CHILD, "1")
            .env("HOME", std::env::temp_dir())
            .env("PSEUDOLIFE_MCP_DATABASE_URL", "postgresql://nobody@127.0.0.1:9/pl_cf_absent")
            .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9")
            .env("PSEUDOLIFE_RELEASE_CHECK", "0")
            .output()
            .unwrap();
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(output.status.success(), "{stderr}");
        assert!(
            stderr.contains("cannot initialize the transfer runtime"),
            "{stderr}"
        );
        assert!(
            stderr.contains("free operating-system resources and retry"),
            "{stderr}"
        );
    }
}
