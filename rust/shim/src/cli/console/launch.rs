//! Starting the browser [`browser::choose`] picked, as CPython 3.11 starts
//! it, and the machine lookups that choice reads.
//!
//! - `BackgroundBrowser.open`: `subprocess.Popen(cmdline)` and `p.poll() is
//!   None`. POSIX: fds 0-2 inherited, `setsid` (`start_new_session=True`),
//!   then `_posixsubprocess`'s `execve` over each `PATH` candidate (never
//!   execvp's `/bin/sh` fallback), reporting the first error that is not
//!   ENOENT/ENOTDIR. Windows: `CreateProcess` with no handle inherited by
//!   CPython; here std's, with the three standard handles on `NUL` so the
//!   browser holds none of this command's pipes either.
//! - `WindowsDefault.open`: `os.startfile(url)`, which is
//!   `ShellExecuteW(NULL, NULL, url, NULL, NULL, SW_SHOWNORMAL)` (the
//!   default verb) and True unless it returns 32 or less.
//!
//! The URL is always one argument (or the one `ShellExecuteW` file): no
//! shell ever reads it.
use super::browser::{Host, Plan, Probe};
use std::ffi::OsString;

/// `open_browser(url)` once the plan is known.
pub(super) fn open(plan: &Plan, url: &str) -> bool {
    match plan {
        Plan::Refused => false,
        Plan::StartFile => start_file(url),
        Plan::Background { program, args } => {
            let args: Vec<String> = args.iter().map(|arg| arg.replace("%s", url)).collect();
            background(program, &args)
        }
    }
}

/// The machine as the oracle reads it.
pub(super) struct Machine;

impl Host for Machine {
    fn var(&self, key: &str) -> Option<OsString> {
        std::env::var_os(key)
    }

    fn which(&self, name: &str) -> bool {
        posix::which(name)
    }

    fn probe(&self) -> Probe {
        posix::probe()
    }
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn start_file(url: &str) -> bool {
    use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};
    let mut file: Vec<u16> = url.encode_utf16().collect();
    // `path_converter` raises ValueError ("too long for Windows") past this.
    if file.len() > 32767 {
        return false;
    }
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
    instance as usize > 32
}

#[cfg(not(windows))]
fn start_file(_url: &str) -> bool {
    // `WindowsDefault` exists only on Windows; the plan never names it here.
    false
}

#[cfg(windows)]
fn background(program: &str, args: &[String]) -> bool {
    use std::process::{Command, Stdio};
    // `browser::launchable` admits only a drive-absolute `.exe`.
    match Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(child) => running(child),
        Err(_) => false,
    }
}

/// `p.poll() is None` right after the start.
fn running(mut child: std::process::Child) -> bool {
    matches!(child.try_wait(), Ok(None))
}

#[cfg(not(windows))]
fn background(program: &str, args: &[String]) -> bool {
    posix::background(program, args)
}

#[cfg(not(unix))]
mod posix {
    use super::Probe;

    pub(super) fn which(_name: &str) -> bool {
        false
    }

    pub(super) fn probe() -> Probe {
        Probe::Missing
    }
}

#[cfg(unix)]
mod posix {
    use super::Probe;
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

    /// What `_posixsubprocess` would `execve`, with the argv and the
    /// environment, built before the fork.
    struct Exec {
        _strings: Vec<CString>,
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
                candidates,
                argv,
                envp,
                new_session,
            })
        }

        /// The child's last step: `setsid` if asked, then each candidate.
        #[allow(unsafe_code)]
        fn exec(&self) -> io::Result<()> {
            if self.new_session {
                rustix::process::setsid().map_err(io::Error::from)?;
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
        // fork and is only read; setsid and execve are async-signal-safe and
        // the closure neither allocates, locks nor panics.
        unsafe { command.pre_exec(move || exec.exec()) };
        Ok(command)
    }

    /// `BackgroundBrowser.open` on POSIX.
    pub(super) fn background(program: &str, args: &[String]) -> bool {
        let argv: Vec<&[u8]> = std::iter::once(program.as_bytes())
            .chain(args.iter().map(|arg| arg.as_bytes()))
            .collect();
        let Ok(mut command) = command(&argv, true) else {
            return false;
        };
        match command.spawn() {
            Ok(child) => super::running(child),
            Err(_) => false,
        }
    }

    /// `subprocess.check_output(["xdg-settings", "get",
    /// "default-web-browser"], stderr=DEVNULL)`, as
    /// `register_standard_browsers` reads it.
    pub(super) fn probe() -> Probe {
        let argv: [&[u8]; 3] = [b"xdg-settings", b"get", b"default-web-browser"];
        let Ok(mut command) = command(&argv, false) else {
            return Probe::Raised;
        };
        let output = command
            .stdin(Stdio::inherit())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .output();
        let output = match output {
            Ok(output) => output,
            Err(error) => {
                // FileNotFoundError, PermissionError, NotADirectoryError.
                return match error.raw_os_error() {
                    Some(libc::ENOENT | libc::ENOTDIR | libc::EACCES | libc::EPERM) => {
                        Probe::Missing
                    }
                    _ => Probe::Raised,
                };
            }
        };
        if !output.status.success() {
            return Probe::Missing;
        }
        match String::from_utf8(output.stdout) {
            Ok(text) => Probe::Answer(super::super::pyjson::py_strip(&text).to_owned()),
            Err(_) => Probe::Raised,
        }
    }
}

#[cfg(all(test, unix))]
pub(super) use posix::{candidates, which_in};
