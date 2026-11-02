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
    // Hash only after pinning: selection can race with pruning while waiting
    // for this lock. Both launcher and child need this verification because
    // runtime binaries can also be invoked directly, without a launcher pin.
    read_runtime(&runtime.path)?;
    Ok(file)
}

pub(super) fn select_pin_with(
    layout: &Layout,
    mut acquire: impl FnMut(&Runtime) -> Result<fs::File>,
) -> Result<(Runtime, fs::File)> {
    let mut retry = None;
    for _ in 0..4 {
        let runtime = retry.take().or_else(|| newest_runtime(layout, false)).ok_or_else(|| format!(
            "no complete native runtime under {}; rerun native setup from a separate installed CLI", layout.root.display()))?;
        match acquire(&runtime) {
            Ok(lock) => return Ok((runtime, lock)),
            Err(error) => {
                if let Some(new) = current_runtime(layout)
                    && new.sequence != runtime.sequence
                {
                    retry = Some(new);
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
            let runtime = match read_runtime_with_digest(executable.parent()?, false) {
                Ok(runtime) => runtime,
                Err(error) => {
                    crate::stderrln!("pseudolife-mcp: {error}; rerun native setup");
                    return Some(1);
                }
            };
            match pin(&runtime) {
                Ok(lock) => {
                    #[cfg(unix)]
                    if let Err(error) = inherited_pin::cloexec(&lock) {
                        crate::stderrln!(
                            "pseudolife-mcp: could not secure the runtime usage lock ({error})"
                        );
                        return Some(1);
                    }
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
        use std::os::fd::AsRawFd;
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
            .env(inherited_pin::ENV, lock.as_raw_fd().to_string())
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
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(async {
            let job = windows_job::create()?;
            let mut command = tokio::process::Command::new(binary);
            command
                .args(std::env::args_os().skip(1))
                .creation_flags(windows_sys::Win32::System::Threading::CREATE_SUSPENDED)
                .kill_on_drop(true);
            // Inherit console, environment, directory and all three streams.
            let mut child = command.spawn()?;
            if let Err(error) = windows_job::assign_and_resume(&job, &child) {
                let _ = child.kill().await;
                return Err(error);
            }
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

#[cfg(unix)]
#[allow(unsafe_code)]
mod inherited_pin {
    use std::{fs::File, io, os::unix::fs::MetadataExt};

    pub(super) const ENV: &str = "PSEUDOLIFE_NATIVE_USAGE_FD";

    // libc's dev_t/ino_t widths differ across Linux and macOS.
    #[allow(clippy::unnecessary_cast)]
    pub(super) fn cloexec(own: &File) -> io::Result<()> {
        use rustix::io::{FdFlags, fcntl_getfd, fcntl_setfd};
        fcntl_setfd(own, fcntl_getfd(own)? | FdFlags::CLOEXEC)?;
        let Some(fd) = std::env::var(ENV).ok().and_then(|s| s.parse::<i32>().ok()) else {
            return Ok(());
        };
        if fd <= 2 {
            return Ok(());
        }
        let meta = own.metadata()?;
        let mut inherited = std::mem::MaybeUninit::<libc::stat>::uninit();
        // SAFETY: fstat accepts any integer descriptor and writes only to this
        // stat buffer on success. An environment value never owns a descriptor.
        if unsafe { libc::fstat(fd, inherited.as_mut_ptr()) } != 0 {
            return Ok(());
        }
        // SAFETY: successful fstat initialized the buffer.
        let inherited = unsafe { inherited.assume_init() };
        if inherited.st_dev as u64 != meta.dev() || inherited.st_ino as u64 != meta.ino() {
            return Ok(());
        }
        // The runtime has acquired its own verified pin before reaching here.
        // Keep the exec-gap pin until exit, but prevent any grandchildren from
        // inheriting it. Only the verified usage file may be changed; never
        // close or take ownership of an untrusted environment-supplied fd.
        // SAFETY: fcntl validates the raw fd; neither call transfers ownership.
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}

#[cfg(windows)]
#[allow(unsafe_code)]
mod windows_job {
    use std::{
        io,
        os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    };
    use windows_sys::Win32::{
        Foundation::{ERROR_NO_MORE_FILES, INVALID_HANDLE_VALUE},
        System::{
            Diagnostics::ToolHelp::{
                CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First,
                Thread32Next,
            },
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_BREAKAWAY_OK,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                JobObjectExtendedLimitInformation, SetInformationJobObject,
            },
            Threading::{
                GetProcessIdOfThread, OpenThread, ResumeThread, THREAD_QUERY_LIMITED_INFORMATION,
                THREAD_SUSPEND_RESUME,
            },
        },
    };

    pub(super) fn create() -> io::Result<OwnedHandle> {
        // SAFETY: null attributes/name create an unnamed, non-inheritable job.
        let raw = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if raw.is_null() {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: CreateJobObjectW returned a new owned handle.
        let job = unsafe { OwnedHandle::from_raw_handle(raw) };
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        // Explicit detached spawns may leave; ordinary descendants stay owned.
        // SILENT_BREAKAWAY_OK would incorrectly release ordinary children too.
        info.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;
        // SAFETY: the owned job and correctly sized information stay live.
        if unsafe {
            SetInformationJobObject(
                job.as_raw_handle(),
                JobObjectExtendedLimitInformation,
                (&info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                std::mem::size_of_val(&info) as u32,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(job)
    }

    pub(super) fn assign_and_resume(
        job: &OwnedHandle,
        child: &tokio::process::Child,
    ) -> io::Result<()> {
        let raw = child
            .raw_handle()
            .ok_or_else(|| io::Error::other("no child handle"))?;
        // SAFETY: both handles remain owned and live. Assignment precedes any
        // execution, so a runtime cannot spawn outside the configured policy.
        if unsafe { AssignProcessToJobObject(job.as_raw_handle(), raw) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let pid = child.id().ok_or_else(|| io::Error::other("no child pid"))?;
        // SAFETY: a non-inheritable snapshot is returned or INVALID_HANDLE_VALUE.
        let raw = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
        if raw == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: the valid snapshot is newly owned.
        let snapshot = unsafe { OwnedHandle::from_raw_handle(raw) };
        let mut entry = THREADENTRY32 {
            dwSize: std::mem::size_of::<THREADENTRY32>() as u32,
            ..Default::default()
        };
        // SAFETY: snapshot and initialized output buffer remain live.
        if unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut threads = Vec::new();
        loop {
            if entry.dwSize < std::mem::size_of::<THREADENTRY32>() as u32 {
                return Err(io::Error::other("short thread snapshot entry"));
            }
            if entry.th32OwnerProcessID == pid {
                // SAFETY: OpenThread validates the snapshot's thread id.
                let raw = unsafe {
                    OpenThread(
                        THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION,
                        0,
                        entry.th32ThreadID,
                    )
                };
                if raw.is_null() {
                    return Err(io::Error::last_os_error());
                }
                // SAFETY: OpenThread returned a new owned handle.
                let thread = unsafe { OwnedHandle::from_raw_handle(raw) };
                // SAFETY: the handle pins the thread; verify its owner in case
                // the snapshot's TID was reused before OpenThread.
                if unsafe { GetProcessIdOfThread(thread.as_raw_handle()) } != pid {
                    return Err(io::Error::other("child thread owner changed"));
                }
                threads.push(thread);
            }
            entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
            // SAFETY: snapshot and correctly sized output buffer remain live.
            if unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) } == 0 {
                let error = io::Error::last_os_error();
                if error.raw_os_error() != Some(ERROR_NO_MORE_FILES as i32) {
                    return Err(error);
                }
                break;
            }
        }
        if threads.is_empty() {
            return Err(io::Error::other("no suspended child thread"));
        }
        for thread in threads {
            // SAFETY: only pinned, verified threads of this owned child resume.
            if unsafe { ResumeThread(thread.as_raw_handle()) } == u32::MAX {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(())
    }
}
