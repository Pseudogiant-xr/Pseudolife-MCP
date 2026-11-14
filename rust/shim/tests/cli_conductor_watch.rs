//! `conductor-watch` as a process: the lock, `--stop`, `--detach`, an
//! interrupt, and where it looks for gh.
//!
//! Every start goes through `Sandbox::command`, which refuses unless the
//! homes, every `XDG_*` and `PSEUDOLIFE_*` path, the working directory and
//! every `PATH` entry are inside this test's own temporary directory and
//! the daemon address is this test's own loopback board. Every watcher a
//! test starts is stopped by its recorded pid before the test ends, and
//! checked gone.
mod common;

use serde_json::{Value, json};
use std::{
    ffi::OsString,
    fs,
    io::{Read, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

const OWNER: &str = "0123456789abcdef0123456789abcdef";
const AGENT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

/// A board on a loopback port this test owns, answering the calls a
/// watcher makes; the owner is always listed.
struct Board {
    url: String,
    requests: Arc<Mutex<Vec<(String, Value)>>>,
    stop: Arc<AtomicBool>,
}

impl Board {
    fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let (log, stopping) = (requests.clone(), stop.clone());
        thread::spawn(move || {
            while !stopping.load(Ordering::Acquire) {
                let Ok((mut stream, _)) = listener.accept() else {
                    thread::sleep(Duration::from_millis(5));
                    continue;
                };
                let log = log.clone();
                thread::spawn(move || {
                    stream.set_nonblocking(false).unwrap();
                    stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
                    let mut bytes = Vec::new();
                    let mut buffer = [0u8; 8192];
                    let body = loop {
                        let Ok(read) = stream.read(&mut buffer) else { return };
                        if read == 0 {
                            return;
                        }
                        bytes.extend_from_slice(&buffer[..read]);
                        let Some(split) = bytes.windows(4).position(|w| w == b"\r\n\r\n") else {
                            continue;
                        };
                        let head = String::from_utf8_lossy(&bytes[..split]).to_lowercase();
                        let length = head
                            .lines()
                            .find_map(|line| line.strip_prefix("content-length:"))
                            .and_then(|value| value.trim().parse::<usize>().ok())
                            .unwrap_or(0);
                        if bytes.len() >= split + 4 + length {
                            let path = head.split_whitespace().nth(1).unwrap_or("").to_owned();
                            let body: Value = serde_json::from_slice(&bytes[split + 4..split + 4 + length])
                                .unwrap_or(json!({}));
                            break (path, body);
                        }
                    };
                    let action = body.0.rsplit('/').next().unwrap_or("").to_owned();
                    log.lock().unwrap().push((action.clone(), body.1));
                    let reply = match action.as_str() {
                        "register" => json!({"agent_id": AGENT, "credential": "fixture-key"}),
                        "attach" | "heartbeat" => json!({"generation": 1}),
                        "agents" => json!({"agents": [{"agent_id": OWNER, "park_reason": ""}], "truncated": false}),
                        "leases" => json!({"leases": [], "truncated": false}),
                        _ => json!({}),
                    };
                    let text = reply.to_string();
                    let _ = write!(
                        stream,
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}",
                        text.len()
                    );
                });
            }
        });
        Self { url, requests, stop }
    }
    fn count(&self, action: &str) -> usize {
        self.requests.lock().unwrap().iter().filter(|(a, _)| a == action).count()
    }
    fn bodies(&self, action: &str) -> Vec<Value> {
        self.requests
            .lock()
            .unwrap()
            .iter()
            .filter(|(a, _)| a == action)
            .map(|(_, body)| body.clone())
            .collect()
    }
}

impl Drop for Board {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
    }
}

/// The test's own temporary directory: the homes, the state, a fake gh.
struct Sandbox {
    root: PathBuf,
    board: Board,
    /// Every watcher a test started: its pid, its start token read when
    /// the test learned the pid, and its state.
    started: Mutex<Vec<(u32, Option<u64>, PathBuf)>>,
}

/// Whether `path` is inside `root`, both made absolute first.
fn inside(path: &Path, root: &Path) -> bool {
    let absolute = |p: &Path| fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
    absolute(path).starts_with(absolute(root))
}

impl Sandbox {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("pl-cw-{}", uuid::Uuid::new_v4().simple()));
        fs::create_dir_all(root.join("home")).unwrap();
        fs::create_dir_all(root.join("bin")).unwrap();
        fs::create_dir_all(root.join("work")).unwrap();
        // gh answering every call with an empty list.
        if cfg!(windows) {
            fs::write(root.join("bin").join("gh.cmd"), "@echo []\r\n").unwrap();
        } else {
            let gh = root.join("bin").join("gh");
            fs::write(&gh, "#!/bin/sh\necho '[]'\n").unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(&gh, fs::Permissions::from_mode(0o755)).unwrap();
            }
        }
        Self { root, board: Board::new(), started: Mutex::new(Vec::new()) }
    }

    fn state(&self, name: &str) -> PathBuf {
        self.root.join("home").join("watch").join(format!("{name}.json"))
    }

    fn env(&self, path: &OsString) -> Vec<(String, OsString)> {
        let home = self.root.join("home");
        let mut env: Vec<(String, OsString)> = vec![
            ("HOME".into(), home.clone().into()),
            ("USERPROFILE".into(), home.clone().into()),
            ("LOCALAPPDATA".into(), home.join("AppData").join("Local").into()),
            ("APPDATA".into(), home.join("AppData").join("Roaming").into()),
            ("XDG_CONFIG_HOME".into(), home.join(".config").into()),
            ("XDG_STATE_HOME".into(), home.join(".state").into()),
            ("PSEUDOLIFE_MCP_DAEMON_URL".into(), self.board.url.clone().into()),
            ("PSEUDOLIFE_MCP_TOKEN".into(), "fake-test-token".into()),
            ("PSEUDOLIFE_AGENT_PROJECT".into(), "demo".into()),
            ("PSEUDOLIFE_LEASE_LOCK_DIR".into(), home.join("locks").into()),
            ("PSEUDOLIFE_SUITE_LOCK_DIR".into(), home.join("locks").into()),
            ("PATH".into(), path.clone()),
        ];
        if let Some(comspec) = std::env::var_os("ComSpec") {
            env.push(("ComSpec".into(), comspec));
        }
        for (key, value) in &env {
            if key != "PATH" && !key.starts_with("PSEUDOLIFE_MCP") && key != "PSEUDOLIFE_AGENT_PROJECT" && key != "ComSpec" {
                fs::create_dir_all(value).unwrap();
            }
        }
        env
    }

    /// Refuse unless `env` and `cwd` keep everything this test's own.
    fn require(&self, env: &[(String, OsString)], cwd: &Path) {
        let temp = std::env::temp_dir();
        assert!(inside(&self.root, &temp), "the sandbox {} is not in {}", self.root.display(), temp.display());
        assert!(inside(cwd, &self.root), "working directory {} is outside the sandbox", cwd.display());
        let get = |name: &str| env.iter().find(|(key, _)| key == name).map(|(_, value)| value.clone());
        for name in ["HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA"] {
            let value = get(name).unwrap_or_else(|| panic!("refusing to start: {name} is unset"));
            assert!(inside(Path::new(&value), &self.root), "refusing to start: {name} is outside the sandbox");
        }
        for (key, value) in env {
            match key.as_str() {
                "PSEUDOLIFE_MCP_DAEMON_URL" => assert_eq!(
                    value.to_str(),
                    Some(self.board.url.as_str()),
                    "refusing to start: the daemon address is not this test's board"
                ),
                "PSEUDOLIFE_MCP_TOKEN" => assert_eq!(value.to_str(), Some("fake-test-token")),
                "PSEUDOLIFE_AGENT_PROJECT" => {}
                "PATH" => {
                    for entry in std::env::split_paths(value) {
                        let entry = if entry.is_absolute() { entry } else { cwd.join(entry) };
                        assert!(inside(&entry, &self.root), "refusing to start: PATH entry {} is outside", entry.display());
                    }
                }
                key if key.starts_with("XDG_") || key.starts_with("PSEUDOLIFE_") => {
                    assert!(inside(Path::new(value), &self.root), "refusing to start: {key} is outside the sandbox");
                }
                _ => {}
            }
        }
    }

    fn command_in(&self, args: &[&str], path: OsString, cwd: &Path) -> Command {
        let env = self.env(&path);
        self.require(&env, cwd);
        let mut command = common::cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
        command.envs(env).current_dir(cwd).arg("conductor-watch").args(args);
        command
    }

    fn command(&self, args: &[&str]) -> Command {
        let path = std::env::join_paths([self.root.join("bin")]).unwrap();
        self.command_in(args, path, &self.root.join("work"))
    }

    fn watch_args(&self, state: &Path) -> Vec<String> {
        ["--owner", OWNER, "--state"]
            .iter()
            .map(|s| (*s).to_owned())
            .chain([state.display().to_string()])
            .chain(["--interval", "1", "--settle", "0", "--repo", "example/demo", "--max-age", "120"].iter().map(|s| (*s).to_owned()))
            .collect()
    }

    /// A foreground watcher on `state`, recorded for the final stop.
    fn spawn(&self, state: &Path) -> Child {
        let args = self.watch_args(state);
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        let child = self
            .command(&args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        self.track(child.id(), state);
        child
    }

    /// Record a watcher for the final stop, with its start token now.
    fn track(&self, pid: u32, state: &Path) {
        let start = process::start(pid);
        self.started.lock().unwrap().push((pid, start, state.to_path_buf()));
    }

    fn run(&self, args: &[&str]) -> std::process::Output {
        self.command(args).stdin(Stdio::null()).output().unwrap()
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        // Every watcher this test started: asked to stop, then, if the very
        // process recorded (pid and start token) is still there after the
        // wait, ended by that pid. Without a token nothing is ended; its
        // `--max-age` ends it.
        let mut left = Vec::new();
        for (pid, start, state) in self.started.lock().unwrap().iter() {
            let ours = || start.is_some() && process::start(*pid) == *start;
            if !ours() {
                continue;
            }
            let _ = fs::write(state.with_extension("stop"), b"");
            let deadline = Instant::now() + Duration::from_secs(10);
            while ours() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(100));
            }
            if ours() {
                process::kill(*pid);
                left.push(*pid);
            }
        }
        let _ = fs::remove_dir_all(&self.root);
        if !std::thread::panicking() {
            assert!(left.is_empty(), "watchers {left:?} did not stop when asked");
        }
    }
}

/// Process facts by pid, through the OS: its start token, and ending it.
#[allow(unsafe_code)]
mod process {
    /// When `pid` started, `None` once it is gone.
    pub fn start(pid: u32) -> Option<u64> {
        #[cfg(windows)]
        {
            use windows_sys::Win32::{
                Foundation::{CloseHandle, FILETIME, STILL_ACTIVE},
                System::Threading::{GetExitCodeProcess, GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION},
            };
            let empty = || FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
            // SAFETY: a query-only handle, closed on every path.
            unsafe {
                let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
                if handle.is_null() {
                    return None;
                }
                let mut code = 0u32;
                let alive = GetExitCodeProcess(handle, &mut code) != 0 && code == STILL_ACTIVE as u32;
                let (mut a, mut b, mut c, mut d) = (empty(), empty(), empty(), empty());
                let read = GetProcessTimes(handle, &mut a, &mut b, &mut c, &mut d);
                CloseHandle(handle);
                (alive && read != 0).then(|| (u64::from(a.dwHighDateTime) << 32) | u64::from(a.dwLowDateTime))
            }
        }
        #[cfg(target_os = "linux")]
        {
            let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
            let tail = stat.rsplit_once(')')?.1;
            if tail.split_whitespace().next() == Some("Z") {
                return None;
            }
            tail.split_whitespace().nth(19)?.parse().ok()
        }
        #[cfg(all(unix, not(target_os = "linux")))]
        {
            // No start token here: the cleanup never ends a process by pid.
            let _ = pid;
            None
        }
    }

    /// End `pid` (one this test started, checked by its start token).
    pub fn kill(pid: u32) {
        #[cfg(windows)]
        {
            use windows_sys::Win32::{
                Foundation::CloseHandle,
                System::Threading::{OpenProcess, PROCESS_TERMINATE, TerminateProcess},
            };
            // SAFETY: a terminate-only handle, closed after use.
            unsafe {
                let handle = OpenProcess(PROCESS_TERMINATE, 0, pid);
                if !handle.is_null() {
                    TerminateProcess(handle, 1);
                    CloseHandle(handle);
                }
            }
        }
        #[cfg(unix)]
        // SAFETY: a signal to one pid this test started.
        unsafe {
            libc::kill(pid as i32, libc::SIGKILL);
        }
    }
}

fn wait_for(what: &str, budget: Duration, mut done: impl FnMut() -> bool) {
    let deadline = Instant::now() + budget;
    while !done() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        thread::sleep(Duration::from_millis(50));
    }
}

fn record(state: &Path) -> Option<Value> {
    serde_json::from_slice(&fs::read(state.with_extension("pid")).ok()?).ok()
}

fn exit_within(child: &mut Child, budget: Duration) -> std::process::ExitStatus {
    let deadline = Instant::now() + budget;
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status;
        }
        assert!(Instant::now() < deadline, "the watcher did not exit");
        thread::sleep(Duration::from_millis(50));
    }
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

#[test]
fn a_second_watcher_on_the_same_state_is_refused_and_stop_ends_the_first() {
    let sandbox = Sandbox::new();
    let state = sandbox.state("one");
    let mut first = sandbox.spawn(&state);
    wait_for("the first watcher's pid record", Duration::from_secs(20), || record(&state).is_some());
    let held = record(&state).unwrap();
    assert_eq!(held["pid"], first.id());
    assert_eq!(held["agent_id"], AGENT);

    // The pid record answers a second start.
    let args = sandbox.watch_args(&state);
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let second = sandbox.run(&args);
    assert_eq!(second.status.code(), Some(0), "{}", text(&second.stderr));
    assert_eq!(
        text(&second.stdout).trim_end(),
        format!("conductor-watch {AGENT} already running for {OWNER}; park with park_needs=\"decision events\" park_clear_by={AGENT}")
    );
    // Without its record the first still holds the state's lock.
    let saved = fs::read(state.with_extension("pid")).unwrap();
    fs::remove_file(state.with_extension("pid")).unwrap();
    let third = sandbox.run(&args);
    assert_eq!(third.status.code(), Some(0));
    assert_eq!(
        text(&third.stderr).trim_end(),
        format!("conductor-watch: another watcher holds {}; this one exits", state.display())
    );
    assert_eq!(sandbox.board.count("register"), 1);
    fs::write(state.with_extension("pid"), saved).unwrap();

    // --stop asks it; it exits cleanly within seconds and leaves the row released.
    let stop = sandbox.run(&["--stop", "--state", &state.display().to_string()]);
    assert_eq!(text(&stop.stdout).trim_end(), format!("conductor-watch {AGENT} asked to stop; it exits at its next poll"));
    let status = exit_within(&mut first, Duration::from_secs(10));
    assert_eq!(status.code(), Some(0));
    assert!(record(&state).is_none());
    assert_eq!(sandbox.board.count("detach"), 1);
    let statuses = sandbox.board.bodies("update");
    assert_eq!(statuses.last().unwrap()["status"], "stopped: stopped");
    // The board calls the row's liveness rests on, as the daemon takes them.
    let register = &sandbox.board.bodies("register")[0];
    assert_eq!(register["label"], "conductor-watch");
    assert_eq!(register["wake_enabled"], false);
    let attach = &sandbox.board.bodies("attach")[0];
    let attachment = attach["attachment_id"].as_str().expect("an attachment id");
    assert_eq!(attach["wake_enabled"], false);
    let detach = &sandbox.board.bodies("detach")[0];
    assert_eq!(detach["attachment_id"], attachment);
    assert_eq!(detach["generation"], 1);
    assert!(statuses.iter().any(|body| body["expect"] == 180), "{statuses:?}");
    assert!(statuses.last().unwrap().get("expect").is_none());
    assert_eq!(sandbox.board.bodies("agents")[0]["limit"], 50);
}

#[test]
fn stop_leaves_a_process_alone_whose_start_time_is_not_the_recorded_one() {
    let sandbox = Sandbox::new();
    let state = sandbox.state("real");
    let mut watcher = sandbox.spawn(&state);
    wait_for("the watcher's pid record", Duration::from_secs(20), || record(&state).is_some());
    let real = record(&state).unwrap();
    let pid = real["pid"].as_u64().unwrap() as u32;
    let start = process::start(pid);
    // A record naming that live process with another start time.
    let near = sandbox.state("near-miss");
    fs::create_dir_all(near.parent().unwrap()).unwrap();
    fs::write(
        near.with_extension("pid"),
        json!({"pid": pid, "process_start": "1", "agent_id": "x", "owner": OWNER}).to_string(),
    )
    .unwrap();
    let out = sandbox.run(&["--stop", "--state", &near.display().to_string()]);
    assert_eq!(out.status.code(), Some(0));
    assert_eq!(
        text(&out.stdout).trim_end(),
        format!("conductor-watch: no watcher running for {}; nothing changed", near.display())
    );
    assert!(!near.with_extension("stop").exists());
    thread::sleep(Duration::from_secs(3));
    assert_eq!(process::start(pid), start, "the process with that pid was disturbed");
    assert_eq!(sandbox.board.count("detach"), 0);
    // Its own stop still ends it.
    fs::write(state.with_extension("stop"), b"").unwrap();
    assert_eq!(exit_within(&mut watcher, Duration::from_secs(10)).code(), Some(0));
}

#[test]
fn a_detached_watcher_outlives_the_command_that_started_it() {
    let sandbox = Sandbox::new();
    let state = sandbox.state("detached");
    let mut args = sandbox.watch_args(&state);
    args.push("--detach".into());
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let mut command = sandbox.command(&args);
    command.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    // The parent leads its own group, as a terminal's foreground job does.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NEW_PROCESS_GROUP);
    }
    let began = Instant::now();
    let mut parent = command.spawn().unwrap();
    let parent_pid = parent.id();
    // Its output is a pipe: it must end when the parent exits, not when
    // the watcher does.
    let (sender, outputs) = std::sync::mpsc::channel();
    for (stream, name) in [
        (Box::new(parent.stdout.take().unwrap()) as Box<dyn Read + Send>, "stdout"),
        (Box::new(parent.stderr.take().unwrap()), "stderr"),
    ] {
        let sender = sender.clone();
        thread::spawn(move || {
            let mut stream = stream;
            let mut text = String::new();
            let _ = stream.read_to_string(&mut text);
            let _ = sender.send((name, text));
        });
    }
    let status = exit_within(&mut parent, Duration::from_secs(20));
    let mut out = (String::new(), String::new());
    for _ in 0..2 {
        match outputs.recv_timeout(Duration::from_secs(10)) {
            Ok(("stdout", text)) => out.0 = text,
            Ok((_, text)) => out.1 = text,
            Err(_) => {
                if let Some(held) = record(&state) {
                    sandbox.track(held["pid"].as_u64().unwrap() as u32, &state);
                }
                panic!("--detach's output stayed open after it exited ({:?})", began.elapsed());
            }
        }
    }
    assert_eq!(status.code(), Some(0), "{}", out.1);
    assert_eq!(
        out.0.trim_end(),
        format!("conductor-watch {AGENT} watching for {OWNER}; park with park_needs=\"decision events\" park_clear_by={AGENT}")
    );
    let held = record(&state).expect("the detached watcher's pid record");
    let pid = held["pid"].as_u64().unwrap() as u32;
    sandbox.track(pid, &state);
    let start = process::start(pid);
    assert!(start.is_some(), "the detached watcher is not running");
    // The session that started it ends: its group gets what a closing
    // terminal or console sends.
    end_session(parent_pid);
    thread::sleep(Duration::from_secs(2));
    assert_eq!(process::start(pid), start, "the detached watcher did not outlive its session");
    assert!(fs::read_to_string(state.with_extension("log")).unwrap().contains("watching for"));

    let stop = sandbox.run(&["--stop", "--state", &state.display().to_string()]);
    assert_eq!(stop.status.code(), Some(0));
    wait_for("the detached watcher to exit", Duration::from_secs(10), || process::start(pid) != start);
    assert!(record(&state).is_none());
    assert_eq!(sandbox.board.count("detach"), 1);
}

#[cfg(unix)]
#[allow(unsafe_code)]
fn end_session(group: u32) {
    // SAFETY: SIGHUP to the process group the test's own parent led.
    unsafe {
        libc::kill(-(group as i32), libc::SIGHUP);
    }
}

#[cfg(windows)]
fn end_session(group: u32) {
    interrupt(group);
}

#[test]
fn an_interrupt_ends_the_watcher_cleanly_and_releases_what_it_held() {
    let sandbox = Sandbox::new();
    let state = sandbox.state("interrupted");
    let args = sandbox.watch_args(&state);
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let mut command = sandbox.command(&args);
    command.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // Its own process group, sharing this console, so a Ctrl-Break
        // reaches it and not the test runner.
        command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NEW_PROCESS_GROUP);
    }
    let mut child = command.spawn().unwrap();
    sandbox.track(child.id(), &state);
    wait_for("the watcher's pid record", Duration::from_secs(20), || record(&state).is_some());
    interrupt(child.id());
    let status = exit_within(&mut child, Duration::from_secs(10));
    let mut stderr = String::new();
    child.stderr.take().unwrap().read_to_string(&mut stderr).unwrap();
    assert_eq!(status.code(), Some(0), "{stderr}");
    assert!(stderr.contains("conductor-watch: exiting: interrupted"), "{stderr}");
    assert!(record(&state).is_none());
    assert_eq!(sandbox.board.count("detach"), 1);
    // The lock went with it: a new watcher starts on the same state.
    let mut next = sandbox.spawn(&state);
    wait_for("a new watcher on the state", Duration::from_secs(20), || record(&state).is_some());
    let _ = fs::write(state.with_extension("stop"), b"");
    assert_eq!(exit_within(&mut next, Duration::from_secs(10)).code(), Some(0));
}

#[cfg(unix)]
#[allow(unsafe_code)]
fn interrupt(pid: u32) {
    // SAFETY: a signal to the child this test started.
    unsafe {
        libc::kill(pid as i32, libc::SIGTERM);
    }
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn interrupt(pid: u32) {
    use windows_sys::Win32::System::Console::{CTRL_BREAK_EVENT, GenerateConsoleCtrlEvent};
    // SAFETY: an event to the process group of the child this test started.
    let sent = unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid) };
    assert_ne!(
        sent,
        0,
        "Ctrl-Break could not be sent ({}): this test needs a console",
        std::io::Error::last_os_error()
    );
}

#[test]
fn a_gh_in_the_current_directory_is_never_run() {
    let sandbox = Sandbox::new();
    let work = sandbox.root.join("work");
    let marker = work.join("planted-ran");
    if cfg!(windows) {
        fs::write(work.join("gh.cmd"), format!("@echo ran> \"{}\"\r\n@echo []\r\n", marker.display())).unwrap();
        fs::write(work.join("gh.exe"), b"MZ").unwrap();
    } else {
        let gh = work.join("gh");
        fs::write(&gh, format!("#!/bin/sh\ntouch '{}'\necho '[]'\n", marker.display())).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&gh, fs::Permissions::from_mode(0o755)).unwrap();
        }
    }
    let state = sandbox.state("planted");
    let args = sandbox.watch_args(&state);
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    // PATH names only the current directory, relatively, and an empty entry.
    let path = OsString::from(if cfg!(windows) { ".;" } else { ".:" });
    let out = sandbox
        .command_in(&args, path, &work)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2), "{}", text(&out.stderr));
    assert!(text(&out.stderr).contains("conductor-watch: gh is not installed or not on PATH"), "{}", text(&out.stderr));
    assert!(!marker.exists(), "the planted gh ran");
    // The owner was told, the message rings its park, and the row is released.
    let sent = sandbox.board.bodies("send");
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0]["to"], OWNER);
    assert_eq!(sent[0]["clears"], "decision events");
    assert!(sent[0]["text"].as_str().unwrap().starts_with("conductor-watch stopped: \"gh is not installed or not on PATH\"."));
    assert_eq!(sandbox.board.count("detach"), 1);
    assert!(record(&state).is_none());
}
