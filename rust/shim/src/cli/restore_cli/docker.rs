//! The Docker commands restore runs, as `update_cli.run_cli` runs them:
//! stdin closed, stdout and stderr captured, a timeout that kills the child
//! (exit 1), and a spawn failure reported as exit 1. Which streams make up the
//! returned text depends on the caller (`stdout_only`, `stderr_on_failure`).
use std::{
    io::Read,
    process::{Command, Stdio},
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
        let mut stdout = child.stdout.take().expect("piped stdout");
        let mut stderr = child.stderr.take().expect("piped stderr");
        let out = std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stdout.read_to_end(&mut bytes);
            bytes
        });
        let err = std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stderr.read_to_end(&mut bytes);
            bytes
        });
        let deadline = Instant::now() + timeout;
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break Some(status),
                Ok(None) if Instant::now() >= deadline => {
                    // subprocess.run kills the direct child at its timeout.
                    let _ = child.kill();
                    let _ = child.wait();
                    break None;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(5)),
                Err(_) => break None,
            }
        };
        let stdout = out.join().unwrap_or_default();
        let stderr = err.join().unwrap_or_default();
        match status {
            Some(status) => Ran {
                code: exit_code(status),
                stdout,
                stderr,
                failed: false,
            },
            None => failed,
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

    #[test]
    fn text_translates_newlines_and_rejects_non_ascii() {
        assert_eq!(ascii_text(b"a\r\nb\rc\n").unwrap(), "a\nb\nc\n");
        assert_eq!(ascii_text("é".as_bytes()), None);
    }
}
