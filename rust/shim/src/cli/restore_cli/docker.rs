//! The Docker commands restore runs, as `update_cli.run_cli` runs them:
//! stdin closed, stdout and stderr captured, a timeout that kills the child
//! (exit 1), and a spawn failure reported as exit 1. Which streams make up the
//! returned text depends on the caller (`stdout_only`, `stderr_on_failure`).
use std::{
    io::Read,
    process::{Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

/// One finished command: Python's `returncode` and the raw streams.
#[derive(Debug, Clone, Default)]
pub(super) struct Ran {
    pub code: i64,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    /// The command never ran or was killed at its timeout: `run_cli`
    /// returns exit 1 and the exception's text, which no caller here shows.
    pub failed: bool,
}

impl Ran {
    /// `run_cli(..., stdout_only=True, stderr_on_failure=...)`'s text, as
    /// bytes before decoding.
    pub fn output(&self, stderr_on_failure: bool) -> Vec<u8> {
        if self.failed {
            return Vec::new();
        }
        let mut out = self.stdout.clone();
        if stderr_on_failure && self.code != 0 {
            out.extend_from_slice(&self.stderr);
        }
        out
    }
}

/// What runs the `docker` program: the real one, or a script in tests.
pub(super) trait Docker {
    fn run(&mut self, args: &[String], timeout: Duration) -> Ran;
}

/// `up.docker_cmd()`: `PSEUDOLIFE_DOCKER`, else `docker`.
pub(super) fn program() -> String {
    std::env::var("PSEUDOLIFE_DOCKER")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "docker".to_owned())
}

pub(super) struct Real {
    pub program: String,
}

fn exit_code(status: std::process::ExitStatus) -> i64 {
    #[cfg(windows)]
    {
        // Python reports the DWORD exit code unsigned.
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

impl Docker for Real {
    fn run(&mut self, args: &[String], timeout: Duration) -> Ran {
        let failed = Ran {
            code: 1,
            failed: true,
            ..Ran::default()
        };
        let mut child = match Command::new(&self.program)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
        {
            Ok(child) => child,
            Err(_) => return failed,
        };
        let deadline = Instant::now() + timeout;
        let mut stdout = child.stdout.take().expect("piped stdout");
        let mut stderr = child.stderr.take().expect("piped stderr");
        // The readers report over a channel, never through a join: a
        // descendant that inherited a pipe keeps it open after the child has
        // gone, and a join would then wait on that descendant, not on the
        // timeout. A reader left behind ends when its pipe closes.
        let (sender, receiver) = mpsc::channel();
        let out = sender.clone();
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stdout.read_to_end(&mut bytes);
            let _ = out.send((true, bytes));
        });
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stderr.read_to_end(&mut bytes);
            let _ = sender.send((false, bytes));
        });
        // subprocess.run kills the direct child at its timeout and reaps it.
        let stop = |child: &mut std::process::Child| {
            let _ = child.kill();
            let _ = child.wait();
        };
        // `communicate(timeout=...)`: both streams to their end, then the
        // child, all inside the one deadline.
        let (mut stdout, mut stderr) = (None, None);
        while stdout.is_none() || stderr.is_none() {
            let left = deadline.saturating_duration_since(Instant::now());
            match receiver.recv_timeout(left) {
                Ok((true, bytes)) => stdout = Some(bytes),
                Ok((false, bytes)) => stderr = Some(bytes),
                Err(_) => {
                    stop(&mut child);
                    return failed;
                }
            }
        }
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) if Instant::now() >= deadline => {
                    stop(&mut child);
                    return failed;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(5)),
                Err(_) => return failed,
            }
        };
        Ran {
            code: exit_code(status),
            stdout: stdout.unwrap_or_default(),
            stderr: stderr.unwrap_or_default(),
            failed: false,
        }
    }
}

/// `text=True, errors="replace"` decoding with universal newlines, for
/// ASCII output only: under any locale encoding Python reads ASCII the same
/// way, so other bytes are `None` (this leaf does not know the encoding).
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

    pub struct Script {
        pub calls: Vec<Vec<String>>,
        pub answers: Vec<(Vec<String>, Ran)>,
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
                answers: answers
                    .into_iter()
                    .map(|(args, ran)| (args.iter().map(|s| (*s).to_owned()).collect(), ran))
                    .collect(),
            }
        }
    }

    impl Docker for Script {
        fn run(&mut self, args: &[String], _timeout: Duration) -> Ran {
            self.calls.push(args.to_vec());
            let at = self.calls.len() - 1;
            match self.answers.get(at) {
                Some((expected, ran)) => {
                    assert_eq!(&expected[..], args, "call {at}");
                    ran.clone()
                }
                None => panic!("unscripted docker call {at}: {args:?}"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn output_keeps_stderr_only_on_failure_when_asked() {
        let ran = Ran {
            code: 0,
            stdout: b"out".to_vec(),
            stderr: b"err".to_vec(),
            failed: false,
        };
        assert_eq!(ran.output(true), b"out");
        let failed = Ran {
            code: 2,
            ..ran.clone()
        };
        assert_eq!(failed.output(true), b"outerr");
        assert_eq!(failed.output(false), b"out");
    }

    #[cfg(unix)]
    #[test]
    fn a_finished_command_returns_its_code_and_both_streams() {
        let mut runner = Real {
            program: "sh".to_owned(),
        };
        let ran = runner.run(
            &[
                "-c".to_owned(),
                "printf out; printf err >&2; exit 3".to_owned(),
            ],
            Duration::from_secs(30),
        );
        assert!(!ran.failed);
        assert_eq!(
            (ran.code, &ran.stdout[..], &ran.stderr[..]),
            (3, &b"out"[..], &b"err"[..])
        );
    }

    /// A descendant that inherited the pipes keeps them open after the
    /// direct child has gone. `subprocess.run` still raises at the timeout
    /// (it kills and reaps the child, then stops reading), so the call is
    /// bounded by the timeout, not by the descendant.
    #[cfg(unix)]
    #[test]
    fn a_timeout_is_not_held_open_by_a_descendant_with_the_pipes() {
        let mut runner = Real {
            program: "sh".to_owned(),
        };
        let started = Instant::now();
        let ran = runner.run(
            &["-c".to_owned(), "sleep 3 & exit 0".to_owned()],
            Duration::from_millis(200),
        );
        assert!(ran.failed, "{ran:?}");
        assert_eq!(ran.code, 1);
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
