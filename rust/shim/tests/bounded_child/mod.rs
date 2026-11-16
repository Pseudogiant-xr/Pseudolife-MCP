//! Bounded waits for a test's own child processes. A child that never exits,
//! or a descendant that keeps its output pipe open, fails the test with a
//! message naming what it waited for and for how long, instead of running
//! into nextest's timeout with no message. The direct child is killed and
//! reaped on every path; a descendant that outlives it is only named.
use std::{
    io::Read,
    process::{Child, Command, ExitStatus, Output, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

/// How long each output pipe may stay open once the child has exited or been
/// killed. An exiting child's own ends of its pipes close with it, so a
/// healthy child's last bytes arrive at once; this bounds only a descendant
/// that inherited the pipes and still holds them. It is a fixed budget of its
/// own, not whatever is left of the child's, so a loaded machine that delivers
/// the last bytes late does not trip it: only a stall of this long between
/// the exit and the read, or a pipe still held that long, can.
const PIPE_DRAIN: Duration = Duration::from_secs(10);

/// Waits until `child` exits, closing its stdin first as `Child::wait` does.
/// Past `budget` it kills and reaps the child, then panics naming `what`.
#[allow(dead_code)] // Not every importer waits on a child it holds itself.
pub fn wait_within(child: &mut Child, budget: Duration, what: &str) -> ExitStatus {
    wait_until(child, budget, what).unwrap_or_else(|error| panic!("{error}"))
}

fn wait_until(child: &mut Child, budget: Duration, what: &str) -> Result<ExitStatus, String> {
    drop(child.stdin.take());
    let deadline = Instant::now() + budget;
    loop {
        let error = match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(10));
                continue;
            }
            Ok(None) => format!("{what} did not exit within {budget:?}; it was killed"),
            Err(error) => format!("could not wait for {what}: {error}"),
        };
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
}

/// `Command::output` with a deadline: stdin is closed and stdout and stderr
/// are captured, as `output` does. The child must exit within `budget`; each
/// pipe is then waited on for up to PIPE_DRAIN.
pub fn output_within(command: &mut Command, budget: Duration, what: &str) -> Output {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap_or_else(|error| panic!("could not start {what}: {error}"));
    let stdout = drain(child.stdout.take().expect("piped stdout"));
    let stderr = drain(child.stderr.take().expect("piped stderr"));
    let status = match wait_until(&mut child, budget, what) {
        Ok(status) => status,
        Err(error) => {
            // The killed child's stderr says where it was, unless a
            // descendant still holds the pipe.
            let printed = stderr
                .recv_timeout(PIPE_DRAIN)
                .map(|bytes| String::from_utf8_lossy(&bytes).into_owned())
                .unwrap_or_else(|_| "(stderr still held open by a descendant)".into());
            panic!("{error}; its stderr:\n{printed}");
        }
    };
    let collect = |pipe: mpsc::Receiver<Vec<u8>>, name: &str| {
        pipe.recv_timeout(PIPE_DRAIN).unwrap_or_else(|_| {
            panic!("{what} exited, but a descendant kept its {name} open {PIPE_DRAIN:?} after it")
        })
    };
    Output {
        status,
        stdout: collect(stdout, "stdout"),
        stderr: collect(stderr, "stderr"),
    }
}

fn drain(mut pipe: impl Read + Send + 'static) -> mpsc::Receiver<Vec<u8>> {
    let (send, receive) = mpsc::channel();
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = pipe.read_to_end(&mut bytes);
        let _ = send.send(bytes);
    });
    receive
}
