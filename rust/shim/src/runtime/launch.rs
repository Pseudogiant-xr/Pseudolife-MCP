use super::*;
use std::sync::OnceLock;

// Kept for the entire native child lifetime, including normal CLI dispatch.
// This complements process/tunnel/registration evidence; it is never a PID lock.
static RUNNING_LOCK: OnceLock<fs::File> = OnceLock::new();

pub(super) fn usage_file(path: &Path) -> Result<fs::File> {
    usage_file_with(path, || crate::board::state::create(path))
}

pub(super) fn usage_file_with(
    path: &Path,
    create: impl FnOnce() -> std::result::Result<crate::board::state::PrivateFile, &'static str>,
) -> Result<fs::File> {
    let opened = crate::board::state::open_with_write(path, true)
        .or_else(|_| create().or_else(|_| crate::board::state::open_with_write(path, true)))
        .map_err(|_| "native runtime usage lock is unavailable".to_owned())?;
    Ok(opened.file)
}

pub(super) fn pin(runtime: &Runtime) -> Result<fs::File> {
    let file = usage_file(&runtime.usage_lock_path())?;
    file.lock_shared().map_err(io_error)?;
    // Selection can race with pruning while waiting for the shared lock.
    read_runtime(&runtime.path)?;
    Ok(file)
}

pub(super) fn select_pin_with(
    layout: &Layout,
    mut acquire: impl FnMut(&Runtime) -> Result<fs::File>,
) -> Result<(Runtime, fs::File)> {
    for _ in 0..4 {
        let runtime = current_runtime(layout).ok_or_else(|| format!(
            "no complete native runtime under {}; rerun native setup from a separate installed CLI", layout.root.display()))?;
        match acquire(&runtime) {
            Ok(lock) => return Ok((runtime, lock)),
            Err(error) => {
                if current_runtime(layout).is_some_and(|new| new.sequence != runtime.sequence) {
                    continue;
                }
                return Err(error);
            }
        }
    }
    Err("runtime selection changed repeatedly; retry after the update finishes".into())
}

/// Call before dispatching *any* CLI mode. A runtime executable pins itself
/// and returns None; only the stable launcher selects and runs another binary.
pub fn maybe_launch() -> Option<u8> {
    let executable = std::env::current_exe().ok()?;
    let layout = match default_layout() {
        Ok(layout) => layout,
        Err(error) => {
            // Half-configured overrides must not silently bypass selection.
            if executable.file_name().is_some_and(|name| name == BINARY)
                || std::env::var_os("PSEUDOLIFE_SHIM_LAUNCHER")
                    .is_some_and(|p| same_path(&executable, Path::new(&p)))
            {
                crate::stderrln!("pseudolife-mcp: {error}");
                return Some(1);
            }
            return None;
        }
    };
    if !same_path(&executable, &layout.launcher) {
        if executable.file_name().is_some_and(|name| name == BINARY)
            && executable
                .parent()
                .and_then(|p| p.parent())
                .is_some_and(|root| same_path(root, &layout.root))
        {
            let runtime = match read_runtime(executable.parent()?) {
                Ok(runtime) => runtime,
                Err(error) => {
                    crate::stderrln!("pseudolife-mcp: {error}; rerun native setup");
                    return Some(1);
                }
            };
            match pin(&runtime) {
                Ok(lock) => {
                    let _ = RUNNING_LOCK.set(lock);
                }
                Err(error) => {
                    crate::stderrln!("pseudolife-mcp: {error}; retry after the update finishes");
                    return Some(1);
                }
            }
        }
        return None;
    }
    let (runtime, lock) = match select_pin_with(&layout, pin) {
        Ok(selected) => selected,
        Err(error) => {
            crate::stderrln!("pseudolife-mcp: {error}");
            return Some(1);
        }
    };
    // The child lives at the root of a numbered directory, never at the stable
    // launcher path. No inherited environment flag can bypass that check.
    if same_path(&runtime.executable(), &executable) {
        crate::stderrln!(
            "pseudolife-mcp: runtime resolves to the stable launcher; rerun native setup"
        );
        return Some(1);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // exec must retain the pin until the child has entered its own startup
        // path. Otherwise pruning can win between exec and the child's pin.
        if let Err(error) = rustix::io::fcntl_getfd(&lock)
            .and_then(|flags| rustix::io::fcntl_setfd(&lock, flags & !rustix::io::FdFlags::CLOEXEC))
        {
            crate::stderrln!("pseudolife-mcp: could not retain the runtime usage lock ({error})");
            return Some(1);
        }
        let error = std::process::Command::new(runtime.executable())
            .args(std::env::args_os().skip(1))
            .exec();
        drop(lock);
        crate::stderrln!(
            "pseudolife-mcp: could not start the selected native runtime ({error}); rerun native setup"
        );
        Some(1)
    }
    #[cfg(windows)]
    {
        let result = windows_child(&runtime.executable());
        drop(lock);
        match result {
            // ExitCode/u8 would truncate Windows process exit status.
            Ok(status) => std::process::exit(status.code().unwrap_or(1)),
            Err(error) => {
                crate::stderrln!(
                    "pseudolife-mcp: could not start the selected native runtime ({error}); rerun native setup"
                );
                Some(1)
            }
        }
    }
}

#[cfg(windows)]
fn windows_child(binary: &Path) -> io::Result<std::process::ExitStatus> {
    use process_wrap::tokio::{CommandWrap, JobObject, KillOnDrop};
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(async {
            let mut command = CommandWrap::with_new(binary, |command| {
                command.args(std::env::args_os().skip(1));
                // Inherit console, environment, directory and all three streams.
            });
            command.wrap(KillOnDrop).wrap(JobObject);
            // JobObject assigns the suspended process before it executes, and
            // closes with kill-on-close even when the launcher is terminated.
            let mut child = command.spawn()?;
            loop {
                if let Some(status) = child.try_wait()? {
                    return Ok(status);
                }
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {}, // Child receives the same console event.
                    _ = tokio::time::sleep(std::time::Duration::from_millis(20)) => {},
                }
            }
        })
}
