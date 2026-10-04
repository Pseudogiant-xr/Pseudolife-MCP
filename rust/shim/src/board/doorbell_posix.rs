use process_wrap::tokio::{CommandWrap, CommandWrapper};
use std::{collections::BTreeMap, ffi::CString, io, os::unix::ffi::OsStrExt, ptr};
use tokio::process::Command;

/// Runs the resolved doorbell CLI after ProcessSession's setsid hook, without execvp's shell fallback.
#[derive(Debug)]
pub(super) struct ExecveOnly;

impl CommandWrapper for ExecveOnly {
    #[allow(unsafe_code)]
    fn pre_spawn(&mut self, command: &mut Command, _core: &CommandWrap) -> io::Result<()> {
        // Doorbell inherits the environment and applies removals; it never uses env_clear or arg0.
        let native = command.as_std();
        let program = CString::new(native.get_program().as_bytes())?;
        let arguments = std::iter::once(native.get_program())
            .chain(native.get_args())
            .map(|argument| CString::new(argument.as_bytes()))
            .collect::<Result<Vec<_>, _>>()?;
        let mut environment = std::env::vars_os().collect::<BTreeMap<_, _>>();
        for (key, value) in native.get_envs() {
            if let Some(value) = value {
                environment.insert(key.to_owned(), value.to_owned());
            } else {
                environment.remove(key);
            }
        }
        let environment = environment
            .into_iter()
            .map(|(key, value)| {
                let mut entry = key.into_encoded_bytes();
                entry.push(b'=');
                entry.extend_from_slice(value.as_bytes());
                CString::new(entry)
            })
            .collect::<Result<Vec<_>, _>>()?;
        let prepared = PreparedExec::new(program, arguments, environment);
        // SAFETY: All storage is owned by this closure before fork. The child only calls execve
        // and captures its errno; no allocation, locking, mutation, panic or destructor occurs.
        // Registration follows ProcessSession, so std runs setsid before this closure.
        unsafe { command.pre_exec(move || prepared.exec()) };
        Ok(())
    }
}

struct PreparedExec {
    program: CString,
    _arguments: Vec<CString>,
    _environment: Vec<CString>,
    argv: Vec<*const libc::c_char>,
    envp: Vec<*const libc::c_char>,
}

// SAFETY: Pointer tables refer only to this value's owned CString allocations. Moving the value
// never moves those allocations. No member is mutated or exposed after construction.
#[allow(unsafe_code)]
unsafe impl Send for PreparedExec {}
// SAFETY: Shared access only reads immutable tables/strings; execve never writes to this storage.
#[allow(unsafe_code)]
unsafe impl Sync for PreparedExec {}

impl PreparedExec {
    fn new(program: CString, arguments: Vec<CString>, environment: Vec<CString>) -> Self {
        let argv = arguments
            .iter()
            .map(|item| item.as_ptr())
            .chain([ptr::null()])
            .collect();
        let envp = environment
            .iter()
            .map(|item| item.as_ptr())
            .chain([ptr::null()])
            .collect();
        Self {
            program,
            _arguments: arguments,
            _environment: environment,
            argv,
            envp,
        }
    }

    #[allow(unsafe_code)]
    fn exec(&self) -> io::Result<()> {
        // SAFETY: Program and entries are live NUL-terminated strings, both pointer tables end
        // in NULL, and all allocations outlive this synchronous read-only call. On success
        // execve replaces the child; on failure errno is captured immediately, without allocating.
        unsafe {
            libc::execve(
                self.program.as_ptr(),
                self.argv.as_ptr(),
                self.envp.as_ptr(),
            )
        };
        Err(io::Error::last_os_error())
    }
}
