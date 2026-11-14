//! `ssh` to the move's target: key-based and non-interactive.
//!
//! - `BatchMode=yes`: ssh never prompts (no password, no passphrase, no
//!   host-key question). Host-key checking is left exactly as the user's
//!   ssh configuration sets it; move passes no option that touches it, so
//!   an unknown or changed key fails with ssh's own refusal, which
//!   [`lost`] turns into its cause and the fix.
//! - Exit 255 is ssh's own failure (the link, the login, the host key) and
//!   never a remote command's answer: the helper on the target never exits
//!   255.
//! - Anything secret travels on stdin, never in a command line.
//! - Remote commands are quoted for a POSIX shell whatever this host is.
use std::{
    io::{Read, Write},
    path::PathBuf,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

/// What one ssh run returned.
#[derive(Debug, Clone, Default)]
pub(in crate::cli) struct Ran {
    pub code: i32,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    /// ssh could not be started or did not finish: the reason.
    pub fault: Option<String>,
}

impl Ran {
    /// ssh's own failure: never a remote answer.
    pub fn lost(&self) -> bool {
        self.fault.is_some() || self.code == 255
    }
}

/// Runs a command on the target.
pub(in crate::cli) trait Remote {
    fn run(&mut self, target: &str, command: &str, stdin: &[u8], timeout: Duration) -> Ran;
}

/// The options every move ssh carries; none of them touches host-key
/// checking.
pub(super) const OPTIONS: [&str; 8] = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=20",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
];

/// The real ssh, found only on absolute PATH entries.
pub(in crate::cli) struct Ssh {
    pub program: PathBuf,
}

impl Ssh {
    /// `ssh` on an absolute PATH entry, or the refusal naming the fix.
    pub fn find() -> Result<Self, String> {
        crate::cli::program_lookup::find_program_path("ssh")
            .map(|program| Self { program })
            .ok_or_else(|| {
                "no ssh program is on PATH (only absolute PATH entries are searched); install the OpenSSH client, then retry".to_owned()
            })
    }
}

/// The argv after the program: the options, `--`, the target and the one
/// remote command string.
pub(super) fn argv(target: &str, command: &str) -> Vec<String> {
    let mut argv: Vec<String> = OPTIONS.iter().map(|s| (*s).to_owned()).collect();
    argv.push("--".to_owned());
    argv.push(target.to_owned());
    argv.push(command.to_owned());
    argv
}

impl Remote for Ssh {
    fn run(&mut self, target: &str, command: &str, stdin: &[u8], timeout: Duration) -> Ran {
        let mut child = match Command::new(&self.program)
            .args(argv(target, command))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
        {
            Ok(child) => child,
            Err(error) => {
                return Ran {
                    code: 255,
                    fault: Some(format!(
                        "{} cannot be started ({error})",
                        self.program.display()
                    )),
                    ..Ran::default()
                };
            }
        };
        let input = stdin.to_vec();
        if let Some(mut pipe) = child.stdin.take() {
            std::thread::spawn(move || {
                let _ = pipe.write_all(&input);
            });
        }
        // The readers report over a channel, never through a join: a
        // ProxyCommand or ProxyJump child that inherited a pipe keeps it open
        // after ssh is gone, and a join would wait on it, not on the timeout
        // (as `docker::Real` does).
        let (sender, receiver) = std::sync::mpsc::channel::<(bool, Vec<u8>)>();
        if let Some(mut out) = child.stdout.take() {
            let sender = sender.clone();
            std::thread::spawn(move || {
                let mut bytes = Vec::new();
                let _ = out.read_to_end(&mut bytes);
                let _ = sender.send((true, bytes));
            });
        }
        if let Some(mut err) = child.stderr.take() {
            let sender = sender.clone();
            std::thread::spawn(move || {
                let mut bytes = Vec::new();
                let _ = err.read_to_end(&mut bytes);
                let _ = sender.send((false, bytes));
            });
        }
        drop(sender);
        let end = Instant::now() + timeout;
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break Ok(status),
                Ok(None) if Instant::now() >= end => {
                    let _ = child.kill();
                    let _ = child.wait();
                    break Err(format!(
                        "it did not finish within {} seconds",
                        timeout.as_secs()
                    ));
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(20)),
                Err(error) => break Err(format!("its exit could not be read ({error})")),
            }
        };
        let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
        let collect = Instant::now() + Duration::from_secs(2);
        while let Some(left) = collect.checked_duration_since(Instant::now()) {
            match receiver.recv_timeout(left) {
                Ok((true, bytes)) => stdout = bytes,
                Ok((false, bytes)) => stderr = bytes,
                Err(_) => break,
            }
        }
        match status {
            Ok(status) => Ran {
                code: status.code().unwrap_or(255),
                stdout,
                stderr,
                fault: None,
            },
            Err(fault) => Ran {
                code: 255,
                stdout,
                stderr,
                fault: Some(fault),
            },
        }
    }
}

/// The host part of an ssh target (`user@host` or a config alias).
fn host(target: &str) -> &str {
    target.rsplit_once('@').map_or(target, |(_, host)| host)
}

/// ssh's last non-empty stderr line.
fn said(ran: &Ran) -> String {
    String::from_utf8_lossy(&ran.stderr)
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or("nothing")
        .to_owned()
}

/// Why ssh to `target` failed, and the fix. ssh states its own host-key
/// refusals in fixed words; any other failure is named by ssh's last line.
pub(super) fn lost(target: &str, ran: &Ran) -> String {
    if let Some(fault) = &ran.fault {
        return format!(
            "ssh to {target} failed: {fault}; check the network and `ssh {target} true`, then retry"
        );
    }
    let text = String::from_utf8_lossy(&ran.stderr);
    let host = host(target);
    if text.contains("REMOTE HOST IDENTIFICATION HAS CHANGED") {
        // ssh names the known_hosts file and entry itself ("remove with:").
        let remove = text
            .lines()
            .map(str::trim)
            .find(|line| line.starts_with("ssh-keygen -f "))
            .map_or_else(|| format!("ssh-keygen -R {host}"), str::to_owned);
        return format!(
            "the ssh host key of {target} has changed since this machine recorded it (ssh refused: {}). A reinstalled host does this, and so does an attack: confirm the new key with whoever runs that host, then run {remove} and connect once with ssh {target} to record it; move never accepts a host key itself",
            said(ran)
        );
    }
    if text.contains("Host key verification failed") {
        return format!(
            "the ssh host key of {target} is not known on this machine (ssh refused: {}). Connect once with ssh {target}, check the fingerprint it shows against the target's own, and accept it; move never accepts a host key itself, then retry",
            said(ran)
        );
    }
    // ssh's login refusal names its methods ("Permission denied (publickey)");
    // a local "connect to host ... Permission denied" is a network refusal.
    if text.contains("Permission denied (") {
        return format!(
            "ssh could not log in to {target} with a key (ssh said: {}); move needs key-based login without prompts, so set it up (for example ssh-copy-id {target}) and check that ssh {target} true works, then retry",
            said(ran)
        );
    }
    format!(
        "ssh could not reach {target} (ssh said: {}); check the name, the network and that ssh {target} true works, then retry",
        said(ran)
    )
}

/// The POSIX `sh -c <script>` command string: the remote login shell only
/// ever sees one single-quoted word after `sh -c`.
pub(super) fn sh(script: &str) -> String {
    format!("sh -c {}", super::super::bank::sh_quote(script))
}
