//! Native fixture processes exercise ownership through an installed launcher.
#[cfg(unix)]
mod bounded_child;
mod native_binary_fixture;
use pseudolife_stdio::{
    lifecycle::DaemonLaunch,
    runtime::{self, BINARY, Evidence, Layout, UpdateLock},
};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{BufRead, BufReader, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::OnceLock,
    time::{Duration, Instant},
};

const PARENT: &str = "native_runtime_parent_fixture";
const DETACHED: &str = "native_detached_child_fixture";
#[cfg(windows)]
const ORDINARY: &str = "native_ordinary_child_fixture";

// Runtime selection and pruning SHA-256-verify the installed binary about
// eleven times per run, in an unoptimized build. Measured 16 November 2026 in
// WSL on an 8-core host: installing the 196 MB debug test binary itself cost
// one quiet run 36 s of CPU. Pinned to one core beside three busy loops, every
// run failed "fixture readiness timed out" at 43-45 s (hosted #1605: 37 s);
// on two cores beside six busy loops a run took 133 s, past nextest's 120 s
// cap (hosted #1605: killed at 120 s), while every wait took under 25 ms.
// The runtime is therefore a stripped copy (6 MB), and the budgets below were
// measured with it: a whole run took 1.3 s quiet and 4.4-5.1 s on one core
// beside three busy loops.

/// Launcher start until every fixture has connected: the launcher and the
/// runtime each pin and hash the runtime, then the runtime starts the
/// detached child. Measured with the stripped copy (16 November 2026, WSL):
/// 0.2 s quiet, 0.7 s on one core beside three busy loops.
const READINESS: Duration = Duration::from_secs(20);
/// One round trip to a running fixture, the launcher's exit after "exit",
/// and the release of the old runtime's pin: no hashing, under 25 ms when
/// measured under load in WSL. Kept at the 20 s these waits always had,
/// since on Windows the pin's release waits for the launcher's job to end.
const REPLY: Duration = Duration::from_secs(20);
/// Draining every fixture connection when a session ends, in all.
const DRAIN: Duration = Duration::from_secs(10);
/// A one-shot runtime child: it pins and hashes the runtime, then exits.
#[cfg(unix)]
const CHILD: Duration = Duration::from_secs(60);
/// A fixture's idle limit. A fixture whose test process is gone sees end of
/// file at once; this bounds only a test that is alive but stuck, so it
/// exceeds nextest's 120 s cap and never fires while the test can still ping.
const FIXTURE_IDLE: Duration = Duration::from_secs(150);

/// Names the step a run has reached, so a run that overruns nextest's cap
/// says where it was: nextest prints a timed-out test's captured output.
fn phase(started: Instant, step: &str) {
    eprintln!(
        "native_runtime_detached: {step} at {:.1} s",
        started.elapsed().as_secs_f64()
    );
}

struct Home {
    root: PathBuf,
    binary: OnceLock<PathBuf>,
}
impl Home {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("native-detach-{}", uuid::Uuid::new_v4()));
        pseudolife_stdio::board::state::private_dir(&root).unwrap();
        Self {
            root,
            binary: OnceLock::new(),
        }
    }
    fn layout(&self) -> Layout {
        Layout {
            root: self.root.join("runtimes"),
            launcher: self.root.join("bin").join(BINARY),
            user_bin: None,
            user_path: false,
        }
    }
    /// This test executable without debug symbols, made once per home: the
    /// runtime runs the same code, and verifying it no longer dominates the
    /// budgets (see above).
    fn runtime_binary(&self) -> &Path {
        self.binary.get_or_init(|| {
            native_binary_fixture::installable(&std::env::current_exe().unwrap(), &self.root)
        })
    }
    fn command(&self, executable: &Path) -> Command {
        let mut command = Command::new(executable);
        command.env_clear();
        for name in ["SystemRoot", "WINDIR", "TEMP", "TMP"] {
            if let Some(value) = std::env::var_os(name) {
                command.env(name, value);
            }
        }
        let layout = self.layout();
        command
            .env("HOME", &self.root)
            .env("USERPROFILE", &self.root)
            .env("LOCALAPPDATA", self.root.join("Local"))
            .env("APPDATA", self.root.join("Roaming"))
            .env("CODEX_HOME", self.root.join("codex"))
            .env("CLAUDE_CONFIG_DIR", self.root.join("claude"))
            .env("PATH", self.root.join("empty-path"))
            .env("PSEUDOLIFE_SHIM_RUNTIMES", layout.root)
            .env("PSEUDOLIFE_SHIM_LAUNCHER", layout.launcher)
            // The detached child runs Cargo's own executable, outside the
            // runtime tree.
            .env("NATIVE_DETACH_EXECUTABLE", std::env::current_exe().unwrap())
            .current_dir(&self.root)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        command
    }
    fn install(&self, version: &str) -> runtime::Runtime {
        let layout = self.layout();
        let _guard = UpdateLock::acquire(&layout).unwrap();
        runtime::install_binary(&layout, version, "fixture", None, self.runtime_binary())
            .unwrap_or_else(|error| panic!("install runtime {version}: {error}"))
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn fixture_connection(role: &str) -> Option<BufReader<TcpStream>> {
    let endpoint = std::env::var("NATIVE_DETACH_ENDPOINT").ok()?;
    let stream = TcpStream::connect(endpoint).unwrap();
    stream.set_read_timeout(Some(FIXTURE_IDLE)).unwrap();
    stream.set_write_timeout(Some(REPLY)).unwrap();
    let mut connection = BufReader::new(stream);
    writeln!(connection.get_mut(), "{role}").unwrap();
    Some(connection)
}

fn fixture_serve(role: &str) {
    let Some(mut connection) = fixture_connection(role) else {
        return;
    };
    loop {
        let mut request = String::new();
        match connection.read_line(&mut request) {
            Ok(0) | Err(_) => return, // Parent cancellation or bounded orphan cleanup.
            Ok(_) if request.trim() == "quit" => {
                let _ = writeln!(connection.get_mut(), "bye:{role}");
                return;
            }
            Ok(_) => writeln!(connection.get_mut(), "pong:{role}").unwrap(),
        }
    }
}

#[test]
fn native_detached_child_fixture() {
    fixture_serve("detached");
}

#[test]
fn native_ordinary_child_fixture() {
    fixture_serve("ordinary");
}

#[test]
fn native_runtime_parent_fixture() {
    if std::env::var_os("NATIVE_DETACH_ENDPOINT").is_none() {
        return;
    }
    if let Some(code) = runtime::maybe_launch() {
        std::process::exit(i32::from(code));
    }
    let executable = std::env::var_os("NATIVE_DETACH_EXECUTABLE").unwrap();
    // The actual daemon spawn seam applies the production detach policy. Its
    // executable is outside the runtime tree, so only an inherited fd could
    // pin the parent's old runtime.
    let _detached = DaemonLaunch {
        program: executable.clone(),
        args: ["--exact", DETACHED, "--nocapture"]
            .map(Into::into)
            .to_vec(),
    }
    .spawn()
    .unwrap();
    #[cfg(windows)]
    let _ordinary = Command::new(executable)
        .args(["--exact", ORDINARY, "--nocapture"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut connection = fixture_connection("runtime").unwrap();
    let mut request = String::new();
    let _ = connection.read_line(&mut request);
    std::process::exit(37);
}

struct Session {
    child: Child,
    connections: BTreeMap<String, BufReader<TcpStream>>,
}
impl Session {
    fn start(home: &Home) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let child = home
            .command(&home.layout().launcher)
            .env(
                "NATIVE_DETACH_ENDPOINT",
                listener.local_addr().unwrap().to_string(),
            )
            .args(["--exact", PARENT, "--nocapture"])
            .spawn()
            .unwrap();
        // Own cleanup before awaiting any child readiness.
        let mut session = Self {
            child,
            connections: BTreeMap::new(),
        };
        let deadline = Instant::now() + READINESS;
        let expected: &[&str] = if cfg!(windows) {
            &["detached", "ordinary", "runtime"]
        } else {
            &["detached", "runtime"]
        };
        while session.connections.len() < expected.len() {
            match listener.accept() {
                Ok((stream, _)) => {
                    // Windows inherits the listener's nonblocking mode; the
                    // framed fixture protocol uses bounded blocking reads.
                    stream.set_nonblocking(false).unwrap();
                    // A fixture that connected in time gets a full reply
                    // budget for its role line, even near the deadline.
                    stream.set_read_timeout(Some(REPLY)).unwrap();
                    stream.set_write_timeout(Some(REPLY)).unwrap();
                    let mut connection = BufReader::new(stream);
                    let mut role = String::new();
                    match connection.read_line(&mut role) {
                        Ok(0) => session.not_ready(
                            expected,
                            "a fixture connected and closed without a role line",
                        ),
                        Ok(_) => {}
                        Err(error) => session.not_ready(
                            expected,
                            &format!(
                                "a connected fixture sent no role line within {REPLY:?} ({error})"
                            ),
                        ),
                    }
                    let role = role.trim().to_owned();
                    if !expected.contains(&role.as_str()) {
                        session.not_ready(expected, &format!("unexpected fixture role {role:?}"));
                    }
                    assert!(
                        session
                            .connections
                            .insert(role.clone(), connection)
                            .is_none(),
                        "fixture {role:?} connected twice"
                    );
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    if Instant::now() >= deadline {
                        session.not_ready(expected, &format!("timed out after {READINESS:?}"));
                    }
                    if let Some(status) = session.child.try_wait().unwrap() {
                        session.not_ready(expected, &format!("the launcher exited ({status})"));
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(error) => panic!("fixture accept failed: {error}"),
            }
        }
        session
    }
    fn not_ready(&self, expected: &[&str], reason: &str) -> ! {
        panic!(
            "fixture readiness failed: {reason}; connected {:?} of {expected:?}",
            self.connections.keys().collect::<Vec<_>>()
        )
    }
    fn stop_launcher(&mut self, terminate: bool) {
        if terminate {
            self.child.kill().unwrap();
        } else {
            writeln!(
                self.connections.get_mut("runtime").unwrap().get_mut(),
                "exit"
            )
            .unwrap_or_else(|error| panic!("could not ask the runtime fixture to exit: {error}"));
        }
        let deadline = Instant::now() + REPLY;
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                if !terminate {
                    assert_eq!(status.code(), Some(37), "runtime fixture exit status");
                }
                return;
            }
            assert!(
                Instant::now() < deadline,
                "launcher did not exit within {REPLY:?} of being stopped"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    fn ping_detached(&mut self, when: &str) {
        let connection = self.connections.get_mut("detached").unwrap();
        writeln!(connection.get_mut(), "ping")
            .unwrap_or_else(|error| panic!("could not ping the detached child {when}: {error}"));
        let mut reply = String::new();
        match connection.read_line(&mut reply) {
            Ok(0) => {
                panic!("detached child closed its connection {when}: it died with the launcher")
            }
            Ok(_) => assert_eq!(
                reply.trim(),
                "pong:detached",
                "detached child answered a ping {when} with something else"
            ),
            Err(error) => {
                panic!("detached child did not answer a ping {when} within {REPLY:?}: {error}")
            }
        }
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        for connection in self.connections.values_mut() {
            let _ = writeln!(connection.get_mut(), "quit");
        }
        // Each fixture closes on cancellation; wait for its socket to close,
        // including a detached child no longer in the owned tree, but for no
        // more than DRAIN in all, since this also runs while a test panics.
        let deadline = Instant::now() + DRAIN;
        for connection in self.connections.values_mut() {
            loop {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero()
                    || connection
                        .get_mut()
                        .set_read_timeout(Some(remaining))
                        .is_err()
                {
                    break;
                }
                match connection.read_line(&mut String::new()) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {}
                }
            }
            let _ = connection.get_mut().shutdown(std::net::Shutdown::Both);
        }
        // Child handles identify only this test's launcher, never shared PIDs.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn detached_survives_launcher(terminate: bool) {
    let started = Instant::now();
    let home = Home::new();
    let old = home.install("1.0.0");
    phase(started, "installed 1.0.0");
    let mut session = Session::start(&home);
    phase(started, "fixtures ready");
    session.ping_detached("before the launcher stopped");
    let pin =
        pseudolife_stdio::board::state::open_with_write(&old.usage_lock_path(), true).unwrap();
    assert!(pin.file.try_lock().is_err(), "running runtime lost its pin");
    home.install("1.0.1");
    phase(started, "installed 1.0.1");
    session.stop_launcher(terminate);
    phase(started, "launcher stopped");
    session.ping_detached("after the launcher stopped");
    #[cfg(windows)]
    {
        let mut reply = String::new();
        let ended = session
            .connections
            .get_mut("ordinary")
            .unwrap()
            .read_line(&mut reply);
        assert!(
            matches!(ended, Ok(0))
                || ended.as_ref().is_err_and(|error| matches!(
                    error.kind(),
                    std::io::ErrorKind::ConnectionReset | std::io::ErrorKind::ConnectionAborted
                )),
            "ordinary child escaped the launcher's job: {ended:?}"
        );
    }
    let deadline = Instant::now() + REPLY;
    loop {
        if pin.file.try_lock().is_ok() {
            pin.file.unlock().unwrap();
            break;
        }
        assert!(
            Instant::now() < deadline,
            "detached grandchild retained old runtime pin for {REPLY:?} after the launcher exited"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    drop(pin);
    phase(started, "old runtime unpinned");
    let layout = home.layout();
    let _guard = UpdateLock::acquire(&layout).unwrap();
    let report = runtime::remove_unused(
        &layout,
        Ok(Evidence {
            processes: Vec::new(),
            registrations: BTreeSet::new(),
            tunnels: BTreeSet::new(),
        }),
    );
    phase(started, "pruned");
    assert_eq!(
        report["removed"].as_array().unwrap().len(),
        1,
        "prune report: {report}"
    );
    assert!(!old.path.exists());
    session.ping_detached("after the old runtime was removed");
}

#[test]
fn detached_child_survives_installed_launcher_exit_without_pinning_old_runtime() {
    detached_survives_launcher(false);
}

#[cfg(windows)]
#[test]
fn detached_child_survives_terminated_installed_launcher_while_owned_child_dies() {
    detached_survives_launcher(true);
}

#[cfg(unix)]
#[test]
fn native_unrelated_fd_fixture() {
    let Ok(fd) = std::env::var("NATIVE_UNRELATED_FD") else {
        return;
    };
    let fd = fd.parse::<i32>().unwrap();
    if let Some(code) = runtime::maybe_launch() {
        std::process::exit(i32::from(code));
    }
    // SAFETY: fcntl checks the inherited descriptor without taking ownership.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    assert!(flags >= 0, "untrusted usage marker closed an unrelated fd");
    assert_eq!(
        flags & libc::FD_CLOEXEC,
        0,
        "untrusted usage marker changed an unrelated fd"
    );
}

#[cfg(unix)]
#[test]
fn untrusted_usage_fd_does_not_change_unrelated_descriptor() {
    use std::os::fd::AsRawFd;
    let home = Home::new();
    let installed = home.install("1.0.0");
    let unrelated = pseudolife_stdio::board::state::create(&home.root.join("unrelated")).unwrap();
    let flags = rustix::io::fcntl_getfd(&unrelated.file).unwrap();
    rustix::io::fcntl_setfd(&unrelated.file, flags & !rustix::io::FdFlags::CLOEXEC).unwrap();
    let output = bounded_child::output_within(
        home.command(&installed.executable())
            .args(["--exact", "native_unrelated_fd_fixture", "--nocapture"])
            .env(
                "NATIVE_UNRELATED_FD",
                unrelated.file.as_raw_fd().to_string(),
            )
            .env(
                "PSEUDOLIFE_NATIVE_USAGE_FD",
                unrelated.file.as_raw_fd().to_string(),
            ),
        CHILD,
        "the installed runtime's unrelated-descriptor fixture",
    );
    rustix::io::fcntl_setfd(&unrelated.file, flags).unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
