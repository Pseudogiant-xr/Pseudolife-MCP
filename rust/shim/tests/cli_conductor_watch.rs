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
const OTHER: &str = "fedcba9876543210fedcba9876543210";

/// A board on a loopback port this test owns, answering the calls a
/// watcher makes; the owner is always listed.
struct Board {
    url: String,
    requests: Arc<Mutex<Vec<(String, Value)>>>,
    /// The agents the board lists: both owners unless a test says otherwise.
    listed: Arc<Mutex<Vec<String>>>,
    stop: Arc<AtomicBool>,
}

impl Board {
    fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let listed = Arc::new(Mutex::new(vec![OWNER.to_owned(), OTHER.to_owned()]));
        let stop = Arc::new(AtomicBool::new(false));
        let (log, stopping, roster) = (requests.clone(), stop.clone(), listed.clone());
        thread::spawn(move || {
            while !stopping.load(Ordering::Acquire) {
                let Ok((mut stream, _)) = listener.accept() else {
                    thread::sleep(Duration::from_millis(5));
                    continue;
                };
                let log = log.clone();
                let roster = roster.clone();
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
                        "agents" => {
                            let rows: Vec<Value> = roster
                                .lock()
                                .unwrap()
                                .iter()
                                .map(|id| json!({"agent_id": id, "park_reason": ""}))
                                .collect();
                            json!({"agents": rows, "truncated": false})
                        }
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
        Self { url, requests, listed, stop }
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
        // gh answering `repo view` with a repository and every other call
        // with an empty list, each call's arguments appended to gh-calls.
        let calls = root.join("gh-calls.txt");
        if cfg!(windows) {
            fs::write(
                root.join("bin").join("gh.cmd"),
                format!(
                    "@echo %*>> \"{}\"\r\n@if \"%1 %2\"==\"repo view\" (echo {{\"nameWithOwner\":\"example/demo\",\"name\":\"demo\"}}) else (echo [])\r\n",
                    calls.display()
                ),
            )
            .unwrap();
        } else {
            let gh = root.join("bin").join("gh");
            fs::write(
                &gh,
                format!(
                    "#!/bin/sh\necho \"$*\" >> '{}'\nif [ \"$1 $2\" = \"repo view\" ]; then echo '{{\"nameWithOwner\":\"example/demo\",\"name\":\"demo\"}}'; else echo '[]'; fi\n",
                    calls.display()
                ),
            )
            .unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(&gh, fs::Permissions::from_mode(0o755)).unwrap();
            }
        }
        Self { root, board: Board::new(), started: Mutex::new(Vec::new()) }
    }

    /// gh made slow: two seconds per call, so a first poll takes several.
    fn slow_gh(&self) {
        if cfg!(windows) {
            let ping = Path::new(&std::env::var_os("SYSTEMROOT").expect("SYSTEMROOT"))
                .join("System32")
                .join("PING.EXE");
            fs::write(
                self.root.join("bin").join("gh.cmd"),
                format!("@\"{}\" -n 3 127.0.0.1 >nul
@echo []
", ping.display()),
            )
            .unwrap();
        } else {
            let sleep = ["/bin/sleep", "/usr/bin/sleep"]
                .into_iter()
                .find(|path| Path::new(path).exists())
                .expect("a sleep program");
            fs::write(self.root.join("bin").join("gh"), format!("#!/bin/sh
{sleep} 2
echo '[]'
")).unwrap();
        }
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
            if ours() && process::end(*pid, start.unwrap_or_default()) {
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

    /// End `pid` only if it is still the process that started at `start`,
    /// checked and ended through one handle (Windows) or pidfd (Linux), so a
    /// pid reused in between is never touched. Whether it was ended.
    pub fn end(pid: u32, token: u64) -> bool {
        #[cfg(windows)]
        {
            use windows_sys::Win32::{
                Foundation::{CloseHandle, FILETIME},
                System::Threading::{
                    GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
                    TerminateProcess,
                },
            };
            let empty = || FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
            // SAFETY: one handle, closed on every path; the creation time is
            // read from the same handle the process is ended through.
            unsafe {
                let handle = OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
                if handle.is_null() {
                    return false;
                }
                let (mut a, mut b, mut c, mut d) = (empty(), empty(), empty(), empty());
                let same = GetProcessTimes(handle, &mut a, &mut b, &mut c, &mut d) != 0
                    && ((u64::from(a.dwHighDateTime) << 32) | u64::from(a.dwLowDateTime)) == token;
                let ended = same && TerminateProcess(handle, 1) != 0;
                CloseHandle(handle);
                ended
            }
        }
        #[cfg(target_os = "linux")]
        {
            // SAFETY: a pidfd for `pid`, checked against the start token
            // after it is open (it then names that process for good), used
            // for the signal and closed.
            unsafe {
                let fd = libc::syscall(libc::SYS_pidfd_open, pid as libc::c_int, 0) as libc::c_int;
                if fd < 0 {
                    return false;
                }
                let same = start(pid) == Some(token);
                let ended = same
                    && libc::syscall(libc::SYS_pidfd_send_signal, fd, libc::SIGKILL, std::ptr::null::<u8>(), 0) == 0;
                libc::close(fd);
                ended
            }
        }
        #[cfg(all(unix, not(target_os = "linux")))]
        {
            let _ = (pid, token);
            false
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
    let began = Instant::now();
    let mut parent = command.spawn().unwrap();
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
    thread::sleep(Duration::from_secs(2));
    assert_eq!(process::start(pid), start, "the detached watcher did not outlive its parent");
    assert!(fs::read_to_string(state.with_extension("log")).unwrap().contains("watching for"));
    // Ready before "watching" was printed: registered, first poll done.
    assert_eq!(held["ready"], true);

    let stop = sandbox.run(&["--stop", "--state", &state.display().to_string()]);
    assert_eq!(stop.status.code(), Some(0));
    wait_for("the detached watcher to exit", Duration::from_secs(10), || process::start(pid) != start);
    assert!(record(&state).is_none());
    assert_eq!(sandbox.board.count("detach"), 1);
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

/// A shell that runs `--detach` and then waits: the session the watcher
/// must outlive. It stays alive (and is this test's unreaped child, so its
/// pid and group cannot be reused) until the test ends its group.
fn session(sandbox: &Sandbox, args: &[String]) -> Child {
    let path = std::env::join_paths([sandbox.root.join("bin")]).unwrap();
    let env = sandbox.env(&path);
    let work = sandbox.root.join("work");
    sandbox.require(&env, &work);
    let binary = env!("CARGO_BIN_EXE_pseudolife-stdio");
    #[cfg(windows)]
    let mut command = {
        use std::os::windows::process::CommandExt;
        let quoted: Vec<String> = args.iter().map(|arg| format!("\"{arg}\"")).collect();
        let system = std::env::var_os("SYSTEMROOT").expect("SYSTEMROOT");
        let mut command = common::cli::cleared_command(&Path::new(&system).join("System32").join("cmd.exe"));
        command.raw_arg(format!(
            "/d /c \"\"{binary}\" conductor-watch {} --detach & \"{}\" -n 61 127.0.0.1 >nul\"",
            quoted.join(" "),
            Path::new(&system).join("System32").join("PING.EXE").display()
        ));
        // Its own group, sharing this console: the group this test ends.
        command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NEW_PROCESS_GROUP);
        command
    };
    #[cfg(unix)]
    let mut command = {
        use std::os::unix::process::CommandExt;
        let sleep = ["/bin/sleep", "/usr/bin/sleep"]
            .into_iter()
            .find(|path| Path::new(path).exists())
            .expect("a sleep program");
        let mut command = common::cli::cleared_command(Path::new("/bin/sh"));
        command
            .arg("-c")
            .arg(format!("\"$0\" conductor-watch \"$@\" --detach; exec {sleep} 60"))
            .arg(binary)
            .args(args);
        // It leads its own group, as a terminal's foreground job does.
        command.process_group(0);
        command
    };
    command
        .envs(env)
        .current_dir(&work)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap()
}

/// End the session's group: SIGHUP (a closing terminal) or Ctrl-Break (a
/// closing console). The leader is this test's live, unreaped child.
#[allow(unsafe_code)]
fn end_session(leader: &mut Child) {
    assert!(leader.try_wait().unwrap().is_none(), "the session ended by itself");
    #[cfg(unix)]
    // SAFETY: the group of a live child this test holds, so not reused.
    unsafe {
        libc::kill(-(leader.id() as i32), libc::SIGHUP);
    }
    #[cfg(windows)]
    interrupt(leader.id());
    // The wrapper may outlast the signal (its wait is the point); it is
    // this test's own child, ended through its handle.
    let deadline = Instant::now() + Duration::from_secs(5);
    while leader.try_wait().unwrap().is_none() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(50));
    }
    if leader.try_wait().unwrap().is_none() {
        leader.kill().unwrap();
        leader.wait().unwrap();
    }
}

#[test]
fn a_detached_watcher_survives_the_end_of_the_session_that_started_it() {
    let sandbox = Sandbox::new();
    let state = sandbox.state("session");
    let args = sandbox.watch_args(&state);
    let mut leader = session(&sandbox, &args);
    wait_for("the detached watcher to be ready", Duration::from_secs(45), || {
        record(&state).is_some_and(|held| held["ready"] == true)
    });
    let held = record(&state).unwrap();
    let pid = held["pid"].as_u64().unwrap() as u32;
    sandbox.track(pid, &state);
    let start = process::start(pid);
    assert!(start.is_some());
    end_session(&mut leader);
    thread::sleep(Duration::from_secs(2));
    assert_eq!(process::start(pid), start, "the detached watcher did not outlive its session");
    let stop = sandbox.run(&["--stop", "--state", &state.display().to_string()]);
    assert_eq!(stop.status.code(), Some(0));
    wait_for("the detached watcher to exit", Duration::from_secs(10), || process::start(pid) != start);
}

#[test]
fn a_second_watcher_for_the_same_owner_is_refused_with_the_first_ones_record() {
    let sandbox = Sandbox::new();
    let first_state = sandbox.state("first");
    let mut first = sandbox.spawn(&first_state);
    wait_for("the first watcher's pid record", Duration::from_secs(20), || record(&first_state).is_some());
    // Another state file for the same owner: the first watcher answers.
    let second_state = sandbox.root.join("elsewhere").join("second.json");
    let args = sandbox.watch_args(&second_state);
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let second = sandbox.run(&args);
    assert_eq!(second.status.code(), Some(0), "{}", text(&second.stderr));
    assert_eq!(
        text(&second.stdout).trim_end(),
        format!("conductor-watch {AGENT} already running for {OWNER}; park with park_needs=\"decision events\" park_clear_by={AGENT}")
    );
    assert_eq!(sandbox.board.count("register"), 1);
    // --stop for that owner, given the other state file, stops the first.
    let stop = sandbox.run(&["--stop", "--owner", OWNER, "--state", &second_state.display().to_string()]);
    assert_eq!(
        text(&stop.stdout).trim_end(),
        format!("conductor-watch {AGENT} asked to stop; it exits at its next poll")
    );
    assert_eq!(exit_within(&mut first, Duration::from_secs(10)).code(), Some(0));
    // Near miss: two different owners on one machine both run.
    let mine = sandbox.state("mine");
    let mut one = sandbox.spawn(&mine);
    wait_for("the owner's watcher", Duration::from_secs(20), || record(&mine).is_some());
    let theirs = sandbox.state("theirs");
    let mut other_args = sandbox.watch_args(&theirs);
    other_args[1] = OTHER.into();
    let other_args: Vec<&str> = other_args.iter().map(String::as_str).collect();
    let mut two = sandbox
        .command(&other_args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    sandbox.track(two.id(), &theirs);
    wait_for("the other owner's watcher", Duration::from_secs(20), || record(&theirs).is_some());
    assert_eq!(sandbox.board.count("register"), 3);
    for (child, state) in [(&mut one, &mine), (&mut two, &theirs)] {
        fs::write(state.with_extension("stop"), b"").unwrap();
        assert_eq!(exit_within(child, Duration::from_secs(10)).code(), Some(0));
    }
}

/// Whether `folder` is this user's alone: mode 0700 on POSIX; on Windows a
/// protected DACL (no inherited entries).
#[allow(unsafe_code)]
fn private(folder: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::metadata(folder).unwrap().permissions().mode() & 0o777 == 0o700
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::{
            Foundation::LocalFree,
            Security::{
                Authorization::{GetNamedSecurityInfoW, SE_FILE_OBJECT},
                DACL_SECURITY_INFORMATION, GetSecurityDescriptorControl, SE_DACL_PROTECTED,
            },
        };
        let wide: Vec<u16> = folder.as_os_str().encode_wide().chain([0]).collect();
        // SAFETY: the descriptor the call allocates is read, then freed.
        unsafe {
            let mut descriptor = std::ptr::null_mut();
            let read = GetNamedSecurityInfoW(
                wide.as_ptr(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                &mut descriptor,
            );
            assert_eq!(read, 0, "the folder's DACL could not be read");
            let (mut control, mut revision) = (0u16, 0u32);
            GetSecurityDescriptorControl(descriptor, &mut control, &mut revision);
            LocalFree(descriptor.cast());
            control & SE_DACL_PROTECTED != 0
        }
    }
}

#[test]
fn the_watchers_files_live_in_private_folders() {
    let sandbox = Sandbox::new();
    let state = sandbox.root.join("fresh").join("watch.json");
    let mut watcher = sandbox.spawn(&state);
    wait_for("the watcher's pid record", Duration::from_secs(20), || record(&state).is_some());
    assert!(private(state.parent().unwrap()), "the state folder is not private");
    let owners = sandbox.root.join("home").join(".pseudolife-mcp").join("watch");
    assert!(owners.join(format!("{OWNER}.owner.json")).exists());
    assert!(private(&owners), "the owner folder is not private");
    fs::write(state.with_extension("stop"), b"").unwrap();
    assert_eq!(exit_within(&mut watcher, Duration::from_secs(10)).code(), Some(0));
}

#[test]
fn detach_reports_a_watcher_that_stops_before_its_first_poll() {
    let sandbox = Sandbox::new();
    // The owner is not on the board: the watcher registers, then leaves at
    // its first poll's owner check. A slow gh keeps that poll, and so its
    // not-yet-ready record, around for several seconds.
    sandbox.board.listed.lock().unwrap().clear();
    sandbox.slow_gh();
    let state = sandbox.state("gone");
    let mut args = sandbox.watch_args(&state);
    args.push("--detach".into());
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let out = sandbox.run(&args);
    assert_eq!(out.status.code(), Some(2), "{}", text(&out.stdout));
    assert!(!text(&out.stdout).contains("watching for"), "{}", text(&out.stdout));
    assert_eq!(
        text(&out.stderr).trim_end(),
        format!(
            "conductor-watch: the watcher did not start (conductor-watch: exiting: owner gone); its log is {}",
            state.with_extension("log").display()
        )
    );
    assert!(record(&state).is_none());
    assert_eq!(sandbox.board.count("detach"), 1);
}

#[test]
fn a_detached_watcher_runs_from_its_own_folder_with_the_callers_repository() {
    let sandbox = Sandbox::new();
    let state = sandbox.state("folder");
    let caller = sandbox.root.join("work").join("checkout");
    fs::create_dir_all(&caller).unwrap();
    // No --repo: the caller's folder names it (gh repo view), once.
    let args: Vec<String> = sandbox
        .watch_args(&state)
        .into_iter()
        .filter(|arg| arg != "--repo" && arg != "example/demo")
        .chain(["--detach".to_owned()])
        .collect();
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let path = std::env::join_paths([sandbox.root.join("bin")]).unwrap();
    let out = sandbox.command_in(&args, path, &caller).stdin(Stdio::null()).output().unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", text(&out.stderr));
    let held = record(&state).expect("the detached watcher's pid record");
    let pid = held["pid"].as_u64().unwrap() as u32;
    sandbox.track(pid, &state);
    let calls = fs::read_to_string(sandbox.root.join("gh-calls.txt")).unwrap();
    assert!(calls.lines().next().unwrap().starts_with("repo view --json nameWithOwner"), "{calls}");
    assert!(calls.lines().any(|line| line.starts_with("pr list -R example/demo ")), "{calls}");
    // It does not keep the caller's folder: that can go while it runs.
    #[cfg(target_os = "linux")]
    assert_eq!(
        fs::read_link(format!("/proc/{pid}/cwd")).unwrap(),
        fs::canonicalize(state.parent().unwrap()).unwrap()
    );
    fs::remove_dir(&caller).expect("the caller's folder is still in use");
    let stop = sandbox.run(&["--stop", "--state", &state.display().to_string()]);
    assert_eq!(stop.status.code(), Some(0));
    let start = process::start(pid);
    wait_for("the detached watcher to exit", Duration::from_secs(10), || process::start(pid) != start);
}
