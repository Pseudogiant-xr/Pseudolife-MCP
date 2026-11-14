//! The Docker commands restore runs: stdin closed or read from a file,
//! stdout captured or written to a new file, stderr captured, a timeout that
//! kills the child, and a spawn failure reported as a failed command.
//!
//! Each command runs in its own process group (a new console process group
//! on Windows), so a Ctrl-C at the terminal reaches only this process. The
//! interrupt flag (`interrupt`) then stops the running command when the
//! caller allows it, and restore decides what happens next at a step
//! boundary instead of a child dying mid-step on its own.
use std::{
    ffi::OsString,
    fs::File,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

use super::interrupt;

/// One finished command: its exit code and the captured streams.
#[derive(Debug, Clone, Default)]
pub(super) struct Ran {
    pub code: i64,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    /// The command never ran, or was killed at its timeout or interrupt.
    pub failed: bool,
    /// Killed because the user interrupted restore.
    pub interrupted: bool,
}

impl Ran {
    pub fn ok(&self) -> bool {
        !self.failed && self.code == 0
    }
}

/// Where a command's stdin comes from.
#[derive(Debug, Clone, Copy)]
pub(super) enum Input<'a> {
    Null,
    File(&'a Path),
}

/// Where a command's stdout goes: captured, or a new private file.
#[derive(Debug, Clone, Copy)]
pub(super) enum Output<'a> {
    Capture,
    File(&'a Path),
}

/// What runs the `docker` program: the real one, or a script in tests.
pub(super) trait Docker {
    fn run_io(
        &mut self,
        args: &[String],
        timeout: Duration,
        input: Input<'_>,
        output: Output<'_>,
    ) -> Ran;

    fn run(&mut self, args: &[String], timeout: Duration) -> Ran {
        self.run_io(args, timeout, Input::Null, Output::Capture)
    }

    /// Whether an interrupt stops the running command (cleanup and the
    /// recovery path run to their end).
    fn set_interruptible(&mut self, _on: bool) {}

    fn is_interruptible(&self) -> bool {
        false
    }

    /// Whether the user asked restore to stop.
    fn interrupted(&self) -> bool {
        interrupt::requested()
    }
}

/// Why the docker program cannot be used.
pub(super) const NOT_FOUND: &str = "Docker was not found: no `docker` program is on PATH (only absolute PATH entries are searched); install Docker, or set PSEUDOLIFE_DOCKER to the docker program's full path, then retry. Nothing was restored";
pub(super) const RELATIVE: &str = "PSEUDOLIFE_DOCKER names a relative path; set it to the docker program's full path (or unset it to use docker from PATH), then retry. Nothing was restored";

/// The docker program: `PSEUDOLIFE_DOCKER` as a full path or a program
/// name, else `docker`, found only on absolute PATH entries (never the
/// current directory).
pub(super) fn program() -> Result<PathBuf, &'static str> {
    let named = std::env::var_os("PSEUDOLIFE_DOCKER").filter(|value| !value.is_empty());
    let name = named.unwrap_or_else(|| OsString::from("docker"));
    let path = Path::new(&name);
    if path.is_absolute() {
        return if path.is_file() {
            Ok(path.to_path_buf())
        } else {
            Err(NOT_FOUND)
        };
    }
    if path.components().count() != 1 || name.to_string_lossy().contains(['/', '\\']) {
        return Err(RELATIVE);
    }
    lookup(&name, std::env::var_os("PATH")).ok_or(NOT_FOUND)
}

/// `name` on the absolute entries of `path`, with Windows' executable
/// extensions.
pub(super) fn lookup(name: &std::ffi::OsStr, path: Option<OsString>) -> Option<PathBuf> {
    let path = path?;
    let extensions: Vec<OsString> = if cfg!(windows) {
        let has_extension = Path::new(name).extension().is_some();
        let mut list: Vec<OsString> = Vec::new();
        if has_extension {
            list.push(OsString::new());
        }
        let pathext = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into());
        list.extend(
            pathext
                .split(';')
                .filter(|ext| !ext.is_empty())
                .map(OsString::from),
        );
        list
    } else {
        vec![OsString::new()]
    };
    for directory in std::env::split_paths(&path) {
        if !directory.is_absolute() {
            continue;
        }
        for extension in &extensions {
            let mut file = name.to_os_string();
            file.push(extension);
            let candidate = directory.join(&file);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

pub(super) struct Real {
    pub program: PathBuf,
    pub interruptible: bool,
}

fn exit_code(status: std::process::ExitStatus) -> i64 {
    #[cfg(windows)]
    {
        i64::from(status.code().unwrap_or(1) as u32)
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        match (status.code(), status.signal()) {
            (Some(code), _) => i64::from(code),
            (None, Some(signal)) => -i64::from(signal),
            (None, None) => 1,
        }
    }
}

/// A new file only this user can read, for a command's stdout.
pub(super) fn private_file(path: &Path) -> std::io::Result<File> {
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)
}

/// The command in its own process group, so a terminal's Ctrl-C reaches
/// only restore, which decides at a step boundary.
fn own_group(command: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // A new process group stops Ctrl-C; no console of its own keeps
        // Ctrl-Break and a console close from reaching the child too. Every
        // stream is a pipe or a file, so the child needs no console.
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
    }
}

/// The longest one wait for output lasts before the deadline and the
/// interrupt flag are checked again.
const POLL: Duration = Duration::from_millis(50);

impl Docker for Real {
    fn set_interruptible(&mut self, on: bool) {
        self.interruptible = on;
    }

    fn is_interruptible(&self) -> bool {
        self.interruptible
    }

    fn run_io(
        &mut self,
        args: &[String],
        timeout: Duration,
        input: Input<'_>,
        output: Output<'_>,
    ) -> Ran {
        let failed = Ran {
            code: 1,
            failed: true,
            ..Ran::default()
        };
        let mut command = Command::new(&self.program);
        command.args(args).stderr(Stdio::piped());
        match input {
            Input::Null => {
                command.stdin(Stdio::null());
            }
            Input::File(path) => match File::open(path) {
                Ok(file) => {
                    command.stdin(file);
                }
                Err(_) => return failed,
            },
        }
        match output {
            Output::Capture => {
                command.stdout(Stdio::piped());
            }
            Output::File(path) => match private_file(path) {
                Ok(file) => {
                    command.stdout(file);
                }
                Err(_) => return failed,
            },
        }
        own_group(&mut command);
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(_) => return failed,
        };
        let deadline = Instant::now() + timeout;
        // The readers report over a channel, never through a join: a
        // descendant that inherited a pipe keeps it open after the child has
        // gone, and a join would then wait on that descendant, not on the
        // timeout. A reader left behind ends when its pipe closes.
        let (sender, receiver) = mpsc::channel();
        let mut streams = 1;
        if let Some(mut stdout) = child.stdout.take() {
            streams += 1;
            let out = sender.clone();
            std::thread::spawn(move || {
                let mut bytes = Vec::new();
                let _ = stdout.read_to_end(&mut bytes);
                let _ = out.send((true, bytes));
            });
        }
        let mut stderr = child.stderr.take().expect("piped stderr");
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stderr.read_to_end(&mut bytes);
            let _ = sender.send((false, bytes));
        });
        let stop = |child: &mut std::process::Child, interrupted: bool| {
            let _ = child.kill();
            let _ = child.wait();
            Ran {
                code: 1,
                failed: true,
                interrupted,
                ..Ran::default()
            }
        };
        let (mut stdout, mut stderr) = (None, None);
        let mut received = 0;
        while received < streams {
            if self.interruptible && interrupt::requested() {
                return stop(&mut child, true);
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return stop(&mut child, false);
            }
            match receiver.recv_timeout(left.min(POLL)) {
                Ok((true, bytes)) => {
                    stdout = Some(bytes);
                    received += 1;
                }
                Ok((false, bytes)) => {
                    stderr = Some(bytes);
                    received += 1;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
        let status = loop {
            if self.interruptible && interrupt::requested() {
                return stop(&mut child, true);
            }
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) if Instant::now() >= deadline => return stop(&mut child, false),
                Ok(None) => std::thread::sleep(Duration::from_millis(5)),
                Err(_) => return failed,
            }
        };
        Ran {
            code: exit_code(status),
            stdout: stdout.unwrap_or_default(),
            stderr: stderr.unwrap_or_default(),
            failed: false,
            interrupted: false,
        }
    }
}

/// Command output as text for messages and parsing: ASCII only, newlines
/// normalized; other bytes are `None` (no locale is assumed).
pub(super) fn ascii_text(bytes: &[u8]) -> Option<String> {
    if !bytes.is_ascii() {
        return None;
    }
    let text = std::str::from_utf8(bytes).expect("ASCII is UTF-8");
    Some(text.replace("\r\n", "\n").replace('\r', "\n"))
}

#[cfg(test)]
pub(super) mod script {
    //! A scripted `docker` for unit tests: each call is matched in order.
    use super::*;

    /// One scripted answer: the expected argv and what the call returns.
    /// `writes` is copied into an `Output::File` target, as `docker`
    /// would stream it.
    pub struct Answer {
        pub args: Vec<String>,
        pub ran: Ran,
        pub writes: Option<Vec<u8>>,
        /// Raise this script's interrupt flag when this call runs.
        pub interrupt: bool,
    }

    pub struct Script {
        pub calls: Vec<Vec<String>>,
        pub inputs: Vec<Option<PathBuf>>,
        pub answers: Vec<Answer>,
        /// This script's own interrupt flag (never the process-wide one,
        /// which parallel tests would share).
        pub flag: bool,
    }

    pub fn ok(stdout: &str) -> Ran {
        Ran {
            code: 0,
            stdout: stdout.as_bytes().to_vec(),
            ..Ran::default()
        }
    }

    pub fn fail(code: i64, stderr: &str) -> Ran {
        Ran {
            code,
            stderr: stderr.as_bytes().to_vec(),
            ..Ran::default()
        }
    }

    impl Script {
        pub fn new(answers: Vec<(&[&str], Ran)>) -> Self {
            Self {
                calls: Vec::new(),
                inputs: Vec::new(),
                flag: false,
                answers: answers
                    .into_iter()
                    .map(|(args, ran)| Answer {
                        args: args.iter().map(|s| (*s).to_owned()).collect(),
                        ran,
                        writes: None,
                        interrupt: false,
                    })
                    .collect(),
            }
        }
    }

    impl Docker for Script {
        fn run_io(
            &mut self,
            args: &[String],
            _timeout: Duration,
            input: Input<'_>,
            output: Output<'_>,
        ) -> Ran {
            self.calls.push(args.to_vec());
            self.inputs.push(match input {
                Input::Null => None,
                Input::File(path) => Some(path.to_path_buf()),
            });
            let at = self.calls.len() - 1;
            let Some(answer) = self.answers.get(at) else {
                panic!("unscripted docker call {at}: {args:?}");
            };
            assert_eq!(&answer.args[..], args, "call {at}");
            if answer.interrupt {
                self.flag = true;
            }
            if let (Output::File(path), Some(bytes)) = (output, &answer.writes) {
                std::fs::write(path, bytes).unwrap();
            } else if let Output::File(path) = output {
                std::fs::write(path, b"").unwrap();
            }
            answer.ran.clone()
        }

        fn interrupted(&self) -> bool {
            self.flag
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lookup_skips_relative_and_empty_path_entries() {
        // cargo runs unit tests in the crate directory, which holds
        // `src/main.rs`: a relative or empty entry reaching it is skipped.
        let name = std::ffi::OsStr::new("main.rs");
        assert!(Path::new("src").join(name).is_file());
        let relative =
            std::env::join_paths([Path::new("src"), Path::new(""), Path::new(".")]).unwrap();
        assert_eq!(lookup(name, Some(relative)), None);
        let absolute = std::env::current_dir().unwrap().join("src");
        let path = std::env::join_paths([Path::new("src"), absolute.as_path()]).unwrap();
        assert_eq!(lookup(name, Some(path)), Some(absolute.join(name)));
        assert_eq!(lookup(name, None), None);
    }

    #[cfg(unix)]
    fn sh() -> Real {
        Real {
            program: PathBuf::from("/bin/sh"),
            interruptible: false,
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_finished_command_returns_its_code_and_both_streams() {
        let ran = sh().run(
            &["-c".into(), "printf out; printf err >&2; exit 3".into()],
            Duration::from_secs(30),
        );
        assert!(!ran.failed);
        assert_eq!(
            (ran.code, &ran.stdout[..], &ran.stderr[..]),
            (3, &b"out"[..], &b"err"[..])
        );
    }

    #[cfg(unix)]
    #[test]
    fn stdin_and_stdout_stream_through_files() {
        let dir =
            std::env::temp_dir().join(format!("pl-restore-io-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&dir).unwrap();
        let source = dir.join("in");
        std::fs::write(&source, b"payload").unwrap();
        let target = dir.join("out");
        let ran = sh().run_io(
            &["-c".into(), "cat".into()],
            Duration::from_secs(30),
            Input::File(&source),
            Output::File(&target),
        );
        assert!(ran.ok(), "{ran:?}");
        assert_eq!(std::fs::read(&target).unwrap(), b"payload");
        // The target is never overwritten.
        let again = sh().run_io(
            &["-c".into(), "cat".into()],
            Duration::from_secs(30),
            Input::File(&source),
            Output::File(&target),
        );
        assert!(again.failed);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A descendant that inherited the pipes keeps them open after the
    /// direct child has gone; the call is still bounded by its timeout.
    #[cfg(unix)]
    #[test]
    fn a_timeout_is_not_held_open_by_a_descendant_with_the_pipes() {
        let started = Instant::now();
        let ran = sh().run(
            &["-c".into(), "sleep 3 & exit 0".into()],
            Duration::from_millis(200),
        );
        assert!(ran.failed, "{ran:?}");
        assert!(!ran.interrupted);
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "{:?}",
            started.elapsed()
        );
    }

    #[test]
    fn text_translates_newlines_and_rejects_non_ascii() {
        assert_eq!(ascii_text(b"a\r\nb\rc\n").unwrap(), "a\nb\nc\n");
        assert_eq!(ascii_text("é".as_bytes()), None);
    }
}
