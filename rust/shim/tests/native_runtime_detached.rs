//! Native fixture processes exercise ownership through an installed launcher.
use pseudolife_stdio::{
    lifecycle::DaemonLaunch,
    runtime::{self, BINARY, Evidence, Layout, UpdateLock},
};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{BufRead, BufReader, Write},
    net::{TcpListener, TcpStream},
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

const PARENT: &str = "native_runtime_parent_fixture";
const DETACHED: &str = "native_detached_child_fixture";
#[cfg(windows)]
const ORDINARY: &str = "native_ordinary_child_fixture";
const TIMEOUT: Duration = Duration::from_secs(20);

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("native-detach-{}", uuid::Uuid::new_v4()));
        pseudolife_stdio::board::state::private_dir(&root).unwrap();
        Self(root)
    }
    fn layout(&self) -> Layout {
        Layout {
            root: self.0.join("runtimes"),
            launcher: self.0.join("bin").join(BINARY),
            user_bin: None,
            user_path: false,
        }
    }
    fn command(&self, executable: &std::path::Path) -> Command {
        let mut command = Command::new(executable);
        command.env_clear();
        for name in ["SystemRoot", "WINDIR", "TEMP", "TMP"] {
            if let Some(value) = std::env::var_os(name) {
                command.env(name, value);
            }
        }
        let layout = self.layout();
        command
            .env("HOME", &self.0)
            .env("USERPROFILE", &self.0)
            .env("LOCALAPPDATA", self.0.join("Local"))
            .env("APPDATA", self.0.join("Roaming"))
            .env("CODEX_HOME", self.0.join("codex"))
            .env("CLAUDE_CONFIG_DIR", self.0.join("claude"))
            .env("PATH", self.0.join("empty-path"))
            .env("PSEUDOLIFE_SHIM_RUNTIMES", layout.root)
            .env("PSEUDOLIFE_SHIM_LAUNCHER", layout.launcher)
            .env("NATIVE_DETACH_EXECUTABLE", std::env::current_exe().unwrap())
            .current_dir(&self.0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        command
    }
    fn install(&self, version: &str) -> runtime::Runtime {
        let layout = self.layout();
        let _guard = UpdateLock::acquire(&layout).unwrap();
        runtime::install_binary(
            &layout,
            version,
            "fixture",
            None,
            &std::env::current_exe().unwrap(),
        )
        .unwrap()
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn fixture_connection(role: &str) -> Option<BufReader<TcpStream>> {
    let endpoint = std::env::var("NATIVE_DETACH_ENDPOINT").ok()?;
    let stream = TcpStream::connect(endpoint).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(60)))
        .unwrap();
    stream.set_write_timeout(Some(TIMEOUT)).unwrap();
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
        let deadline = Instant::now() + TIMEOUT;
        let expected = if cfg!(windows) { 3 } else { 2 };
        while session.connections.len() < expected {
            match listener.accept() {
                Ok((stream, _)) => {
                    // Windows inherits the listener's nonblocking mode; the
                    // framed fixture protocol uses bounded blocking reads.
                    stream.set_nonblocking(false).unwrap();
                    stream.set_read_timeout(Some(TIMEOUT)).unwrap();
                    stream.set_write_timeout(Some(TIMEOUT)).unwrap();
                    let mut connection = BufReader::new(stream);
                    let mut role = String::new();
                    connection.read_line(&mut role).unwrap();
                    assert!(["runtime", "detached", "ordinary"].contains(&role.trim()));
                    assert!(
                        session
                            .connections
                            .insert(role.trim().to_owned(), connection)
                            .is_none()
                    );
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(Instant::now() < deadline, "fixture readiness timed out");
                    assert!(
                        session.child.try_wait().unwrap().is_none(),
                        "launcher exited before readiness"
                    );
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(error) => panic!("fixture accept failed: {error}"),
            }
        }
        session
    }
    fn stop_launcher(&mut self, terminate: bool) {
        if terminate {
            self.child.kill().unwrap();
        } else {
            writeln!(
                self.connections.get_mut("runtime").unwrap().get_mut(),
                "exit"
            )
            .unwrap();
        }
        let deadline = Instant::now() + TIMEOUT;
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                if !terminate {
                    assert_eq!(status.code(), Some(37));
                }
                return;
            }
            assert!(Instant::now() < deadline, "launcher did not exit");
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    fn ping_detached(&mut self) {
        let connection = self.connections.get_mut("detached").unwrap();
        writeln!(connection.get_mut(), "ping").unwrap();
        let mut reply = String::new();
        connection.read_line(&mut reply).unwrap();
        assert_eq!(
            reply.trim(),
            "pong:detached",
            "detached child died with launcher"
        );
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        for connection in self.connections.values_mut() {
            let _ = writeln!(connection.get_mut(), "quit");
        }
        for connection in self.connections.values_mut() {
            // Each fixture closes on cancellation; wait for its socket to
            // close, including a detached child no longer in the owned tree.
            loop {
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
    let home = Home::new();
    let old = home.install("1.0.0");
    let mut session = Session::start(&home);
    session.ping_detached();
    let pin =
        pseudolife_stdio::board::state::open_with_write(&old.usage_lock_path(), true).unwrap();
    assert!(pin.file.try_lock().is_err(), "running runtime lost its pin");
    home.install("1.0.1");
    session.stop_launcher(terminate);
    session.ping_detached();
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
    let deadline = Instant::now() + TIMEOUT;
    loop {
        if pin.file.try_lock().is_ok() {
            pin.file.unlock().unwrap();
            break;
        }
        assert!(
            Instant::now() < deadline,
            "detached grandchild retained old runtime pin"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    drop(pin);
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
    assert_eq!(report["removed"].as_array().unwrap().len(), 1);
    assert!(!old.path.exists());
    session.ping_detached();
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
    let unrelated = pseudolife_stdio::board::state::create(&home.0.join("unrelated")).unwrap();
    let flags = rustix::io::fcntl_getfd(&unrelated.file).unwrap();
    rustix::io::fcntl_setfd(&unrelated.file, flags & !rustix::io::FdFlags::CLOEXEC).unwrap();
    let output = home
        .command(&installed.executable())
        .args(["--exact", "native_unrelated_fd_fixture", "--nocapture"])
        .env(
            "NATIVE_UNRELATED_FD",
            unrelated.file.as_raw_fd().to_string(),
        )
        .env(
            "PSEUDOLIFE_NATIVE_USAGE_FD",
            unrelated.file.as_raw_fd().to_string(),
        )
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .unwrap();
    rustix::io::fcntl_setfd(&unrelated.file, flags).unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
