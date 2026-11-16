//! Bounded waits for a test's own child processes. A child that never exits,
//! or a descendant that keeps its output pipe open, fails the test with a
//! message naming what it waited for and for how long, instead of running
//! into nextest's timeout with no message; the child is killed and reaped on
//! every path, so a failure leaves no process behind.
use std::{
    io::Read,
    process::{Child, Command, ExitStatus, Output, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

/// Waits until `child` exits. Past `budget` it kills and reaps the child, then
/// panics naming `what`.
pub fn wait_within(child: &mut Child, budget: Duration, what: &str) -> ExitStatus {
    let deadline = Instant::now() + budget;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                panic!("{what} did not exit within {budget:?}; it was killed");
            }
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                panic!("could not wait for {what}: {error}");
            }
        }
    }
}

/// `Command::output` with a deadline: stdin is closed and stdout and stderr
/// are captured, as `output` does. The child must exit, and its pipes close,
/// within `budget`.
pub fn output_within(command: &mut Command, budget: Duration, what: &str) -> Output {
    let started = Instant::now();
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap_or_else(|error| panic!("could not start {what}: {error}"));
    let stdout = drain(child.stdout.take().expect("piped stdout"));
    let stderr = drain(child.stderr.take().expect("piped stderr"));
    let status = wait_within(&mut child, budget, what);
    let collect = |pipe: mpsc::Receiver<Vec<u8>>, name: &str| {
        let remaining = budget.saturating_sub(started.elapsed());
        pipe.recv_timeout(remaining.max(Duration::from_millis(100)))
            .unwrap_or_else(|_| {
                panic!("{what} exited, but a descendant kept its {name} open past {budget:?}")
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
