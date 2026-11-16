//! Starting the browser [`browser::choose`] picked, and the machine lookups
//! that choice reads.
//!
//! - A background launch: `[program] + [arg.replace("%s", url) ...]` (the
//!   URL is always one argument, never read by a shell). POSIX: fds 0-2
//!   are `/dev/null`, every other descriptor closed, `setsid`, then `execve`
//!   over each `PATH` candidate (never execvp's `/bin/sh` fallback),
//!   reporting the first error that is not ENOENT/ENOTDIR. The launcher is
//!   then given [`SETTLE`] to exit: a zero exit or a program still running
//!   (a browser in the foreground of its own session) is opened, a non-zero
//!   exit is a failure with its status. Windows: `CreateProcess` with
//!   nothing redirected, so the browser gets this command's own standard
//!   handles, as CPython's child does (measured 2026-11-13, a program
//!   CPython 3.11.9 started this way wrote into its parent's stdout pipe),
//!   and opened when still running or successfully exited after the start.
//! - Windows `os.startfile(url)`: `ShellExecuteW(NULL, NULL, url, NULL,
//!   NULL, SW_SHOWNORMAL)` (the default verb), opened unless it returns 32
//!   or less.
//!
//! Test builds start nothing: every launch passes [`intercept`] first,
//! which panics unless the program is an absolute path inside the stub
//! directory the calling test registered with [`stub_root`].
use super::browser::{Host, Plan};
use std::ffi::OsString;

/// The outcome of one launch: opened, or not, with the cause to print
/// beside the link.
pub(super) type Launched = Result<(), Option<String>>;

/// How long a POSIX launcher has to report failure by exiting non-zero.
/// Chosen, not measured: `open`, `xdg-open` and `gio open` hand the URL to
/// a running desktop and exit well inside it; a launcher still running
/// after it is taken as the browser itself, started in the foreground.
#[cfg(unix)]
const SETTLE: std::time::Duration = std::time::Duration::from_secs(2);

/// The program's argv after `program`, each `%s` replaced by the URL.
pub(super) fn arguments(args: &[String], url: &str) -> Vec<String> {
    args.iter().map(|arg| arg.replace("%s", url)).collect()
}

/// Start what `plan` names for `url`, at most one program.
pub(super) fn open(plan: &Plan, url: &str) -> Launched {
    match plan {
        Plan::Refused(cause) => Err(cause.clone()),
        Plan::StartFile => {
            intercept(plan);
            start_file(url)
        }
        Plan::Background { program, args } => {
            intercept(plan);
            background(program, &arguments(args, url))
        }
    }
}

#[cfg(test)]
thread_local! {
    static STUB_ROOT: std::cell::RefCell<Option<std::path::PathBuf>> =
        const { std::cell::RefCell::new(None) };
}

/// A test's stub directory: programs inside it may start on this thread.
#[cfg(test)]
pub(super) fn stub_root(root: Option<std::path::PathBuf>) {
    STUB_ROOT.with(|cell| *cell.borrow_mut() = root);
}

/// In a test build, refuse (panic) before any launch but a registered stub:
/// a test that reaches a real launcher fails instead of opening a browser
/// (#1509). `ShellExecuteW` is never a stub.
#[cfg(test)]
fn intercept(plan: &Plan) {
    let allowed = match plan {
        Plan::Background { program, .. } => STUB_ROOT.with(|cell| {
            let path = std::path::Path::new(program);
            cell.borrow().as_deref().is_some_and(|root| {
                path.is_absolute()
                    && path.starts_with(root)
                    && !path
                        .components()
                        .any(|part| part == std::path::Component::ParentDir)
            })
        }),
        _ => false,
    };
    assert!(
        allowed,
        "a test tried to start a real browser launcher ({plan:?}); tests start only stubs inside launch::stub_root"
    );
}

#[cfg(not(test))]
fn intercept(_plan: &Plan) {}

/// The machine as the choice reads it.
pub(super) struct Machine;

impl Host for Machine {
    fn var(&self, key: &str) -> Option<OsString> {
        std::env::var_os(key)
    }

    fn which(&self, name: &str) -> bool {
        posix::which(name)
    }
}

/// Open the default Windows browser, preserving the system launch cause.
#[cfg(windows)]
fn start_file(url: &str) -> Launched {
    if url.encode_utf16().count() > 32767 {
        return Err(Some("the sign-in URL is too long for the Windows default browser launcher; open the printed link yourself".to_owned()));
    }
    shell_result(shell_execute(url))
}

#[cfg(any(windows, test))]
fn shell_result(code: isize) -> Launched {
    if code > 32 {
        Ok(())
    } else {
        // ShellExecuteW returns these compatibility codes, not a Win32
        // GetLastError code (notably 31 means no association).
        let cause = match code {
            0 | 8 => "Windows is out of memory or resources",
            2 => "the browser executable was not found",
            3 => "the browser path was not found",
            5 => "Windows denied access to the browser",
            11 => "the browser executable is invalid",
            26 => "a sharing violation prevented the launch",
            27 => "the browser association is incomplete",
            28 => "the browser hand-off timed out",
            29 => "the browser hand-off failed",
            30 => "the browser is busy",
            31 => "no browser is associated with this URL",
            32 => "a browser library was not found",
            _ => "Windows returned an unknown launch error",
        };
        Err(Some(format!(
            "could not open the default browser: {cause} (ShellExecuteW {code}); open the printed link yourself"
        )))
    }
}

#[cfg(not(windows))]
fn start_file(_url: &str) -> Launched {
    // `os.startfile` exists only on Windows; the plan never names it here.
    Err(None)
}

/// A Windows background start, with an injectable result for offline tests.
#[cfg(windows)]
fn background(program: &str, args: &[String]) -> Launched {
    spawn_result(program, || {
        let mut child = std::process::Command::new(program)
            .args(args)
            .spawn()
            .map_err(|error| format!("could not start {program} ({error})"))?;
        child
            .try_wait()
            .map_err(|error| format!("could not check {program} ({error})"))
    })
}

#[cfg(any(windows, test))]
fn spawn_result(
    program: &str,
    start: impl FnOnce() -> Result<Option<std::process::ExitStatus>, String>,
) -> Launched {
    let result = match start() {
        Ok(None) => Ok(()),
        Ok(Some(status)) if status.success() => Ok(()),
        Ok(Some(status)) => Err(format!("{program} failed ({status})")),
        Err(cause) => Err(cause),
    };
    result.map_err(|cause| Some(format!("{cause}; {}", super::browser::SET_WINDOWS_BROWSER)))
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn shell_execute(url: &str) -> isize {
    use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};
    let mut file: Vec<u16> = url.encode_utf16().collect();
    file.push(0);
    // SAFETY: `file` is a live NUL-terminated UTF-16 string for the whole
    // call; every other pointer argument is NULL, which ShellExecuteW
    // accepts (no window, the default verb, no parameters, no directory).
    let instance = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            std::ptr::null(),
            file.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            SW_SHOWNORMAL,
        )
    };
    instance as isize
}

#[cfg(not(windows))]
fn background(program: &str, args: &[String]) -> Launched {
    posix::background(program, args)
        .map_err(|cause| Some(format!("{cause}; {}", super::browser::SET_BROWSER)))
}

#[cfg(not(unix))]
mod posix {
    pub(super) fn which(_name: &str) -> bool {
        false
    }
}

#[cfg(unix)]
mod posix {
    use std::{
        ffi::{CString, OsStr, OsString},
        io,
        os::unix::{ffi::OsStrExt, process::CommandExt},
        path::Path,
        process::{Command, Stdio},
        ptr,
    };

    /// `os.confstr("CS_PATH")`, `shutil.which`'s search path when `PATH` is unset.
    const CS_PATH: &str = if cfg!(target_os = "macos") {
        "/usr/bin:/bin:/usr/sbin:/sbin"
    } else {
        "/bin:/usr/bin"
    };
    /// `os.defpath`, `os.get_exec_path`'s when `PATH` is unset.
    const DEFPATH: &str = "/bin:/usr/bin";

    /// `posixpath.join(directory, name)` for a name with no slash.
    fn join(directory: &[u8], name: &[u8]) -> Vec<u8> {
        let mut path = directory.to_vec();
        if !path.is_empty() && !path.ends_with(b"/") {
            path.push(b'/');
        }
        path.extend_from_slice(name);
        path
    }

    /// `shutil.which(name) is not None` for a bare name.
    pub(super) fn which(name: &str) -> bool {
        which_in(name, std::env::var_os("PATH"))
    }

    /// [`which`] with `PATH` given.
    pub(crate) fn which_in(name: &str, path: Option<OsString>) -> bool {
        let path = path.unwrap_or_else(|| CS_PATH.into());
        if path.is_empty() {
            return false;
        }
        let mut seen: Vec<&[u8]> = Vec::new();
        for directory in path.as_bytes().split(|byte| *byte == b':') {
            if seen.contains(&directory) {
                continue;
            }
            seen.push(directory);
            let candidate = join(directory, name.as_bytes());
            let candidate = Path::new(OsStr::from_bytes(&candidate));
            // `_access_check`: exists, `os.access(X_OK)`, not a directory.
            if candidate.metadata().is_ok_and(|meta| !meta.is_dir())
                && rustix::fs::access(candidate, rustix::fs::Access::EXEC_OK).is_ok()
            {
                return true;
            }
        }
        false
    }

    /// The paths `Popen` tries for `program`: itself when it holds a slash,
    /// else joined to each `os.get_exec_path()` entry (`PATH`, or
    /// `os.defpath` when unset; an empty `PATH` is one empty entry).
    pub(crate) fn candidates(program: &[u8], path: Option<OsString>) -> Vec<Vec<u8>> {
        if program.contains(&b'/') {
            return vec![program.to_vec()];
        }
        let path = path.unwrap_or_else(|| DEFPATH.into());
        path.as_bytes()
            .split(|byte| *byte == b':')
            .map(|directory| join(directory, program))
            .collect()
    }

    /// The descriptors above stderr that are open before the fork, which
    /// `close_fds=True` closes in the child. Read from `/dev/fd`; where
    /// that cannot be listed, the first 1024 numbers.
    fn open_above_stderr() -> Vec<libc::c_int> {
        match std::fs::read_dir("/dev/fd") {
            Ok(entries) => entries
                .filter_map(|entry| entry.ok()?.file_name().to_str()?.parse().ok())
                .filter(|fd| *fd > 2)
                .collect(),
            Err(_) => (3..1024).collect(),
        }
    }

    /// What `_posixsubprocess` would `execve`, with the argv and the
    /// environment, built before the fork.
    struct Exec {
        _strings: Vec<CString>,
        close: Vec<libc::c_int>,
        candidates: Vec<*const libc::c_char>,
        argv: Vec<*const libc::c_char>,
        envp: Vec<*const libc::c_char>,
        new_session: bool,
    }

    // SAFETY: The pointer tables point only into `_strings`, which this value
    // owns and never mutates; moving the value does not move those buffers.
    #[allow(unsafe_code)]
    unsafe impl Send for Exec {}
    // SAFETY: Shared access only reads the immutable tables.
    #[allow(unsafe_code)]
    unsafe impl Sync for Exec {}

    impl Exec {
        /// `Popen(argv)`: the program as given when it holds a slash, else
        /// joined to each `os.get_exec_path()` entry.
        fn new(argv: &[&[u8]], new_session: bool) -> io::Result<Self> {
            let paths = candidates(argv[0], std::env::var_os("PATH"));
            let mut strings = Vec::new();
            let mut lengths = [0usize; 3];
            for (index, group) in [
                paths,
                argv.iter().map(|item| item.to_vec()).collect(),
                std::env::vars_os()
                    .map(|(key, value)| {
                        let mut entry = key.into_encoded_bytes();
                        entry.push(b'=');
                        entry.extend_from_slice(value.as_bytes());
                        entry
                    })
                    .collect(),
            ]
            .into_iter()
            .enumerate()
            {
                lengths[index] = group.len();
                for item in group {
                    // CPython raises ValueError for an embedded NUL.
                    strings.push(CString::new(item)?);
                }
            }
            let pointers = |from: usize, count: usize| -> Vec<*const libc::c_char> {
                strings[from..from + count]
                    .iter()
                    .map(|item| item.as_ptr())
                    .chain([ptr::null()])
                    .collect()
            };
            let candidates = pointers(0, lengths[0]);
            let argv = pointers(lengths[0], lengths[1]);
            let envp = pointers(lengths[0] + lengths[1], lengths[2]);
            Ok(Self {
                _strings: strings,
                close: open_above_stderr(),
                candidates,
                argv,
                envp,
                new_session,
            })
        }

        /// The child's last step: `setsid` if asked, the descriptors
        /// above stderr left to close, then each candidate.
        #[allow(unsafe_code)]
        fn exec(&self) -> io::Result<()> {
            if self.new_session {
                rustix::process::setsid().map_err(io::Error::from)?;
            }
            for fd in &self.close {
                // Close-on-exec, not close: the program starts without the
                // descriptor either way, and the pipe std reports a failed
                // exec through (one of these numbers) stays usable.
                // SAFETY: fcntl on a descriptor number is async-signal-safe
                // and touches no memory; a number no longer open fails with
                // EBADF, which is ignored.
                unsafe { libc::fcntl(*fd, libc::F_SETFD, libc::FD_CLOEXEC) };
            }
            let mut saved = 0;
            let mut last = 0;
            for candidate in self.candidates.iter().take_while(|item| !item.is_null()) {
                // SAFETY: Every pointer is a live NUL-terminated string owned
                // by `self`, and both tables end in NULL. On success execve
                // replaces the process; on failure it returns and errno is
                // read at once, without allocating.
                unsafe { libc::execve(*candidate, self.argv.as_ptr(), self.envp.as_ptr()) };
                last = io::Error::last_os_error().raw_os_error().unwrap_or(0);
                if last != libc::ENOENT && last != libc::ENOTDIR && saved == 0 {
                    saved = last;
                }
            }
            Err(io::Error::from_raw_os_error(if saved != 0 {
                saved
            } else {
                last
            }))
        }
    }

    /// A command whose child runs [`Exec::exec`] as its final step.
    #[allow(unsafe_code)]
    fn command(argv: &[&[u8]], new_session: bool) -> io::Result<Command> {
        let exec = Exec::new(argv, new_session)?;
        let mut command = Command::new(OsStr::from_bytes(argv[0]));
        // SAFETY: Everything the closure touches was allocated before the
        // fork and is only read; setsid, fcntl and execve are
        // async-signal-safe and the closure neither allocates, locks nor
        // panics.
        unsafe { command.pre_exec(move || exec.exec()) };
        Ok(command)
    }

    /// A background launch on POSIX: started in a session of its own with
    /// `/dev/null` for its standard streams (a browser left running must
    /// not hold a caller's pipe open, nor write into this terminal), then
    /// given [`super::SETTLE`] to exit. `Err` is the cause, worded for the
    /// user.
    pub(super) fn background(program: &str, args: &[String]) -> Result<(), String> {
        let argv: Vec<&[u8]> = std::iter::once(program.as_bytes())
            .chain(args.iter().map(|arg| arg.as_bytes()))
            .collect();
        let mut command = command(&argv, true)
            .map_err(|_| format!("could not start {program}: a NUL byte in its command line"))?;
        let mut child = command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| format!("could not start {program} ({error})"))?;
        let deadline = std::time::Instant::now() + super::SETTLE;
        loop {
            match child.try_wait() {
                // WSL's Explorer hands the URL to Windows and exits 1.
                // Only this exact executable name and code are a hand-off.
                Ok(Some(status))
                    if status.success()
                        || (cfg!(target_os = "linux")
                            && Path::new(program).file_name().is_some_and(|name| {
                                name.as_encoded_bytes()
                                    .eq_ignore_ascii_case(b"explorer.exe")
                            })
                            && status.code() == Some(1)) =>
                {
                    return Ok(());
                }
                Ok(Some(status)) => return Err(format!("{program} failed ({status})")),
                // Still running at the deadline: the browser itself.
                Ok(None) if std::time::Instant::now() >= deadline => return Ok(()),
                Ok(None) => std::thread::sleep(std::time::Duration::from_millis(20)),
                // A caller that ignores SIGCHLD has the kernel reap the
                // launcher as it exits: it started and is gone, its status
                // unknowable, which is a hand-off, not a failure.
                Err(error) if error.raw_os_error() == Some(libc::ECHILD) => return Ok(()),
                Err(error) => return Err(format!("could not wait for {program} ({error})")),
            }
        }
    }

    #[cfg(test)]
    mod tests {
        use std::os::fd::AsRawFd;

        /// `close_fds=True`: a descriptor above stderr that this process
        /// holds open and inheritable is not open in the program it starts.
        #[test]
        #[allow(unsafe_code)]
        fn a_started_program_inherits_no_descriptor_above_stderr() {
            let file = std::fs::File::open("/dev/null").unwrap();
            let fd = file.as_raw_fd();
            // SAFETY: `fd` is this test's own open descriptor; clearing its
            // close-on-exec flag only makes it inheritable.
            assert_eq!(unsafe { libc::fcntl(fd, libc::F_SETFD, 0) }, 0);
            let script = format!("[ -e /dev/fd/{fd} ]");
            let argv: [&[u8]; 3] = [b"/bin/sh", b"-c", script.as_bytes()];
            let status = super::command(&argv, false).unwrap().status().unwrap();
            assert_eq!(
                status.code(),
                Some(1),
                "descriptor {fd} reached the program"
            );
            // The same program sees a descriptor it is meant to keep.
            let argv: [&[u8]; 3] = [b"/bin/sh", b"-c", b"[ -e /dev/fd/2 ]"];
            let status = super::command(&argv, false).unwrap().status().unwrap();
            assert_eq!(status.code(), Some(0));
        }
    }
}

#[cfg(all(test, unix))]
pub(super) use posix::{candidates, which_in};

#[cfg(test)]
#[path = "launch_tests.rs"]
mod tests;
