//! `pseudolife-mcp restore` (`restore_cli.py`): the gates before any Docker
//! command. Argparse's help and errors, the dump-completeness check, the
//! state-archive check (`docker_backup_restore.validate_state`) and the
//! refusal without `--yes` are answered here. Every argv that passes them,
//! the interactive prompt and every shape this leaf does not decide defer to
//! the dispatcher before the update lock, a Docker command or any write.
use std::{
    ffi::OsString,
    io::{self, Write},
    process::ExitCode,
};

mod args;
mod dump;
mod gzip;
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

/// `update_cli.interactive()`: `sys.stdin.isatty() and sys.stdout.isatty()`.
/// On Windows the CRT's `isatty` is true for any character device (`NUL`
/// included), so a character device counts as a terminal here too.
fn interactive() -> bool {
    #[cfg(windows)]
    {
        windows::is_character_device(true) && windows::is_character_device(false)
    }
    #[cfg(not(windows))]
    {
        use std::io::IsTerminal;
        io::stdin().is_terminal() && io::stdout().is_terminal()
    }
}

#[cfg(windows)]
#[allow(unsafe_code)]
mod windows {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{FILE_TYPE_CHAR, GetFileType};

    pub(super) fn is_character_device(stdin: bool) -> bool {
        let handle = if stdin {
            std::io::stdin().as_raw_handle()
        } else {
            std::io::stdout().as_raw_handle()
        };
        if handle.is_null() {
            return false;
        }
        // SAFETY: GetFileType only inspects the borrowed standard handle.
        unsafe { GetFileType(handle) == FILE_TYPE_CHAR }
    }
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
        if interactive() {
            return None;
        }
        return refuse(NEEDS_YES);
    }
    // Confirmed: the oracle takes the update lock and drives Docker next.
    None
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
