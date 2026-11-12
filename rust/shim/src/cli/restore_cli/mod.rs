//! `pseudolife-mcp restore` (`restore_cli.py`). The gates before any Docker
//! command: argparse's help and errors, the dump-completeness check, the
//! state-archive check (`docker_backup_restore.validate_state`) and the
//! refusal without `--yes`. Then, confirmed, `DockerBank(...)` and the
//! read-only start of `DockerBank.restore()` (`bank`): a refusal there is
//! printed under the update lock, as the oracle holds it. The interactive
//! prompt, every shape this leaf does not decide and every run that would
//! reach the oracle's first change defer to the dispatcher before the
//! update lock is taken or anything is written.
use std::{
    ffi::OsString,
    io::{self, Write},
    path::PathBuf,
    process::{self, ExitCode},
};

use super::lease::lock as os_lock;

mod args;
mod bank;
mod conn;
mod docker;
mod dump;
mod gzip;
mod pathlib;
mod state;

/// A check's answer: Python's result is certain (`Pass`, `Refuse`), or this
/// leaf cannot be sure of it (`Unsure`, which defers).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Verdict {
    Pass,
    Refuse,
    Unsure,
}

const INCOMPLETE_DUMP: &str =
    "the backup is unreadable or incomplete; use a complete .sql.gz dump. Nothing was restored";
const UNSAFE_STATE: &str = "the state archive is unreadable or contains unsafe paths; use a complete, unmodified state backup. Nothing was restored";
const NEEDS_YES: &str =
    "restore replaces the existing bank; rerun with --yes to confirm. Nothing was restored";

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

/// `print(f"restore failed: {exc}", file=sys.stderr)`, exit 2 (`UpdateError(..., 2)`).
fn refuse(message: &str) -> Option<ExitCode> {
    let _ = write_stream(false, &format!("restore failed: {message}\n"));
    Some(ExitCode::from(2))
}

/// `None` defers to the dispatcher before any effect.
pub(super) fn run(arguments: Vec<OsString>) -> Option<ExitCode> {
    let args = match args::parse(&arguments)? {
        args::Parsed::Args(args) => args,
        args::Parsed::Help => {
            return Some(if write_stream(true, args::HELP).is_ok() {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(120)
            });
        }
        args::Parsed::Error(message) => {
            let _ = write_stream(
                false,
                &format!(
                    "{}pseudolife-mcp restore: error: {message}\n",
                    args::usage()
                ),
            );
            return Some(ExitCode::from(2));
        }
    };
    match dump::complete(&args.backup_file) {
        Verdict::Pass => {}
        Verdict::Refuse => return refuse(INCOMPLETE_DUMP),
        Verdict::Unsure => return None,
    }
    if let Some(archive) = &args.state_archive {
        match state::validate(archive) {
            Verdict::Pass => {}
            Verdict::Refuse => return refuse(UNSAFE_STATE),
            Verdict::Unsure => return None,
        }
    }
    if !args.yes {
        // A person at a terminal is asked; the prompt path defers.
        if super::connect::interactive() {
            return None;
        }
        return refuse(NEEDS_YES);
    }
    confirmed(&args)
}

/// `up.data_dir()`: `PSEUDOLIFE_MCP_DATA_DIR`, else `<home>/.pseudolife-mcp`
/// (`None` where Python would ask `Path.home()`).
fn data_dir() -> Option<PathBuf> {
    let set = |key: &str| std::env::var_os(key).filter(|value| !value.is_empty());
    if let Some(dir) = set("PSEUDOLIFE_MCP_DATA_DIR") {
        return Some(PathBuf::from(dir));
    }
    let home = set("USERPROFILE").or_else(|| set("HOME"))?;
    Some(PathBuf::from(home).join(".pseudolife-mcp"))
}

/// `UpdateBusy`: the holder's pid as `UpdateLock.acquire` reads it.
fn busy(directory: &std::path::Path) -> Option<ExitCode> {
    let pid = match std::fs::read(directory.join("update.lock.pid")) {
        Ok(bytes) => {
            // read_text(encoding="utf-8") then str.strip/isdigit, which
            // read Unicode; a decode error escapes the oracle.
            if !bytes.is_ascii() {
                return None;
            }
            let text = String::from_utf8(bytes).expect("ASCII");
            text.trim_matches(dump::python_space).to_owned()
        }
        Err(_) => String::new(),
    };
    let pid = if !pid.is_empty() && pid.bytes().all(|b| b.is_ascii_digit()) {
        pid
    } else {
        "unknown".to_owned()
    };
    refuse(&format!(
        "another pseudolife-mcp update is running (pid {pid}); nothing was changed. Run this again when it has finished"
    ))
}

/// The oracle's `with up.UpdateLock():` around `DockerBank` and `restore`.
/// A lock this run cannot take (held, or an OS error) defers: nothing was
/// written, and the oracle then answers for itself.
fn under_lock(directory: &std::path::Path, body: impl FnOnce() -> ExitCode) -> Option<ExitCode> {
    let mut lock = os_lock::Lock::new(directory.join("update.lock"));
    if !matches!(lock.acquire(), Ok(true)) {
        return None;
    }
    let holder = directory.join("update.lock.pid");
    // write_text in text mode: the newline is CRLF on Windows.
    let newline = if cfg!(windows) { "\r\n" } else { "\n" };
    let _ = std::fs::write(&holder, format!("{}{newline}", process::id()));
    let code = body();
    let _ = std::fs::remove_file(&holder);
    lock.release();
    Some(code)
}

/// Confirmed: the update lock, `DockerBank(...)` and `restore(...)`.
fn confirmed(args: &args::Args) -> Option<ExitCode> {
    let directory = data_dir()?;
    // Held now: the oracle refuses before any Docker command.
    match os_lock::probe(&directory.join("update.lock")) {
        Ok(Some(true)) => return busy(&directory),
        Ok(_) => {}
        Err(_) => return None,
    }
    let mut runner = docker::Real {
        program: docker::program(),
    };
    match bank::probe(&mut runner, args) {
        Err(bank::Stop::Fail(message, code)) => under_lock(&directory, || {
            let _ = write_stream(false, &format!("restore failed: {message}\n"));
            ExitCode::from(code)
        }),
        // Unsure, or the oracle's first change comes next: not ported yet.
        Err(bank::Stop::Defer) | Ok(_) => None,
    }
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
}
