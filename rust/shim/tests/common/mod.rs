#![allow(dead_code)]
pub mod cli;
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

#[derive(Clone)]
pub enum Reply {
    Hang,
    Gated(Arc<ResponseGate>, Box<Reply>),
    Result(Value),
    Http(u16, String),
    HttpType(u16, &'static str, String),
    RpcError(i32, String, Option<Value>),
    Sse(Value),
    Cut,
    Redirect(String),
    AfterCommit(Box<Reply>),
}
#[derive(Default)]
pub struct ResponseGate {
    released: Mutex<bool>,
    changed: Condvar,
}
impl ResponseGate {
    pub fn release(&self) {
        *self.released.lock().unwrap() = true;
        self.changed.notify_all();
    }
    fn wait(&self, stopped: &AtomicBool) -> bool {
        let deadline = Instant::now() + Duration::from_secs(12);
        let mut released = self.released.lock().unwrap();
        while !*released && !stopped.load(Ordering::SeqCst) {
            assert!(Instant::now() < deadline, "held response was not released");
            // This bounded wake observes fixture shutdown after a test panic;
            // response ordering is controlled only by the explicit release.
            released = self
                .changed
                .wait_timeout(released, Duration::from_millis(20))
                .unwrap()
                .0;
        }
        *released
    }
}
#[derive(Default)]
pub struct Setup {
    pub health: Vec<Reply>,
    pub calls: Vec<Reply>,
    pub lists: Vec<Reply>,
    pub initialize: Vec<Reply>,
}
#[derive(Clone, Debug)]
pub struct Record {
    pub verb: String,
    pub path: String,
    pub headers: HashMap<String, String>,
    pub message: Value,
}
pub struct Fixture {
    pub url: String,
    stopped: Arc<AtomicBool>,
    calls: Arc<(Mutex<usize>, Condvar)>,
    deleted: Arc<Mutex<HashSet<String>>>,
    sessions: Arc<AtomicUsize>,
    pub records: Arc<Mutex<Vec<Record>>>,
    pub health_enabled: Arc<AtomicBool>,
    pub mcp_enabled: Arc<AtomicBool>,
    pub writes: Arc<AtomicUsize>,
    listener: Option<thread::JoinHandle<()>>,
}
impl Fixture {
    pub fn start() -> Self {
        Self::start_with(Setup::default())
    }
    pub fn start_with(setup: Setup) -> Self {
        let socket = TcpListener::bind("127.0.0.1:0").unwrap();
        socket.set_nonblocking(true).unwrap();
        let url = format!("http://{}", socket.local_addr().unwrap());
        let stopped = Arc::new(AtomicBool::new(false));
        let calls = Arc::new((Mutex::new(0), Condvar::new()));
        let deleted = Arc::new(Mutex::new(HashSet::new()));
        let sessions = Arc::new(AtomicUsize::new(0));
        let records = Arc::new(Mutex::new(Vec::new()));
        let health_enabled = Arc::new(AtomicBool::new(true));
        let mcp_enabled = Arc::new(AtomicBool::new(true));
        let writes = Arc::new(AtomicUsize::new(0));
        let replies = Arc::new(Mutex::new(HashMap::from([
            ("health", VecDeque::from(setup.health)),
            ("initialize", VecDeque::from(setup.initialize)),
            ("tools/call", VecDeque::from(setup.calls)),
            ("tools/list", VecDeque::from(setup.lists)),
        ])));
        let (trace, health, mcp) = (records.clone(), health_enabled.clone(), mcp_enabled.clone());
        let committed = writes.clone();
        let (stop, seen, gone, sequence) = (
            stopped.clone(),
            calls.clone(),
            deleted.clone(),
            sessions.clone(),
        );
        let listener = thread::spawn(move || {
            let mut handlers = Vec::new();
            while !stop.load(Ordering::SeqCst) {
                match socket.accept() {
                    Ok((stream, _)) => {
                        let (stop, seen, gone, sequence) =
                            (stop.clone(), seen.clone(), gone.clone(), sequence.clone());
                        let (trace, health, mcp, replies) =
                            (trace.clone(), health.clone(), mcp.clone(), replies.clone());
                        let committed = committed.clone();
                        handlers.push(thread::spawn(move || {
                            respond(
                                stream, stop, seen, gone, sequence, trace, health, mcp, replies,
                                committed,
                            )
                        }));
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2))
                    }
                    Err(error) => panic!("fixture accept: {error}"),
                }
            }
            for handler in handlers {
                handler.join().unwrap();
            }
        });
        Self {
            url,
            stopped,
            calls,
            deleted,
            sessions,
            records,
            health_enabled,
            mcp_enabled,
            writes,
            listener: Some(listener),
        }
    }
    pub fn wait_calls(&self, count: usize) -> bool {
        let (lock, changed) = &*self.calls;
        let ready = changed
            .wait_timeout_while(lock.lock().unwrap(), Duration::from_secs(10), |seen| {
                *seen < count
            })
            .unwrap();
        *ready.0 >= count
    }
    pub fn assert_deleted(&self) {
        assert_eq!(
            self.deleted.lock().unwrap().len(),
            self.sessions.load(Ordering::SeqCst),
            "all initialized sessions must be deleted before shim exit"
        );
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::SeqCst);
        if let Some(listener) = self.listener.take() {
            listener.join().unwrap();
        }
    }
}
#[allow(clippy::too_many_arguments)]
fn respond(
    mut stream: TcpStream,
    stopped: Arc<AtomicBool>,
    calls: Arc<(Mutex<usize>, Condvar)>,
    deleted: Arc<Mutex<HashSet<String>>>,
    sessions: Arc<AtomicUsize>,
    records: Arc<Mutex<Vec<Record>>>,
    health_enabled: Arc<AtomicBool>,
    mcp_enabled: Arc<AtomicBool>,
    replies: Arc<Mutex<HashMap<&'static str, VecDeque<Reply>>>>,
    writes: Arc<AtomicUsize>,
) {
    stream.set_nonblocking(false).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut line = String::new();
    if reader.read_line(&mut line).unwrap_or(0) == 0 {
        return;
    }
    let mut words = line.split_whitespace();
    let verb = words.next().unwrap().to_owned();
    let path = words.next().unwrap().to_owned();
    let mut headers = HashMap::new();
    loop {
        line.clear();
        if reader.read_line(&mut line).unwrap_or(0) == 0 {
            return;
        }
        if line == "\r\n" {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            headers.insert(name.to_lowercase(), value.trim().to_owned());
        }
    }
    let length = headers
        .get("content-length")
        .map_or(0, |value| value.parse().unwrap());
    let mut body = vec![0; length];
    reader.read_exact(&mut body).unwrap();
    let message: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    records.lock().unwrap().push(Record {
        verb: verb.clone(),
        path: path.clone(),
        headers: headers.clone(),
        message: message.clone(),
    });
    let mut session = headers.get("mcp-session-id").cloned().unwrap_or_default();
    let mut session_header = String::new();
    let method = message.get("method").and_then(Value::as_str).unwrap_or("");
    let reply = if verb == "GET" && path == "/health" {
        if !health_enabled.load(Ordering::SeqCst) {
            return;
        }
        replies.lock().unwrap().get_mut("health").and_then(VecDeque::pop_front).unwrap_or_else(|| Reply::Http(
            200,
            json!({"status":"ok","auth_required":false,"version":pseudolife_stdio::lifecycle::PACKAGE_VERSION}).to_string(),
        ))
    } else if verb == "DELETE" {
        deleted.lock().unwrap().insert(session.clone());
        Reply::Http(200, "{}".to_owned())
    } else if verb == "POST" && path == "/mcp" {
        if !mcp_enabled.load(Ordering::SeqCst) {
            return;
        }
        let planned = replies
            .lock()
            .unwrap()
            .get_mut(method)
            .and_then(VecDeque::pop_front);
        match method {
            "initialize" => {
                let reply = planned.unwrap_or_else(||Reply::Result(json!({"protocolVersion":"2025-11-25","capabilities":{"tools":{"listChanged":true}},"serverInfo":{"name":"fixture","version":"1"},"instructions":"Disposable hanging-call fixture."})));
                if matches!(reply, Reply::Result(_) | Reply::Sse(_) | Reply::Hang) {
                    let id = sessions.fetch_add(1, Ordering::SeqCst);
                    session = format!("fixture-{id}");
                    session_header = format!("Mcp-Session-Id: {session}\r\n");
                }
                reply
            }
            "tools/call" => {
                assert!(!session.is_empty());
                let (seen, changed) = &*calls;
                *seen.lock().unwrap() += 1;
                changed.notify_all();
                let reply = planned.unwrap_or_else(|| {
                    if message["params"]["name"] == "read" {
                        Reply::Result(json!({"content":[{"type":"text","text":json!({"writes":writes.load(Ordering::SeqCst)}).to_string()}],"isError":false}))
                    } else { Reply::Hang }
                });
                if let Reply::AfterCommit(reply) = reply {
                    assert_eq!(message["params"]["name"], "write");
                    writes.fetch_add(1, Ordering::SeqCst);
                    *reply
                } else {
                    if message["params"]["name"] == "write"
                        && matches!(reply, Reply::Result(_) | Reply::Sse(_))
                    {
                        writes.fetch_add(1, Ordering::SeqCst);
                    }
                    reply
                }
            }
            "tools/list" => planned.unwrap_or_else(|| {
                Reply::Result(json!({"tools":[{"name":"fixture","inputSchema":{"type":"object"}}]}))
            }),
            _ => Reply::Http(202, String::new()),
        }
    } else if verb == "POST" && path == "/api/episode/end" {
        Reply::Http(200, "{}".to_owned())
    } else {
        Reply::Http(405, "{}".to_owned())
    };
    let reply = if let Reply::Gated(gate, reply) = reply {
        if !gate.wait(&stopped) {
            return;
        }
        *reply
    } else {
        reply
    };
    let (status,content_type,body,extra) = match reply {
        Reply::Gated(_, _) => unreachable!("gate wrapper is handled before response writing"),
        Reply::AfterCommit(_) => unreachable!("commit wrapper is handled during tool dispatch"),
        Reply::HttpType(status,content_type,body) => (status,content_type,body,String::new()),
        Reply::Hang => {
            let _ = write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n{session_header}Connection: close\r\n\r\n"); let _ = stream.flush();
            while !stopped.load(Ordering::SeqCst) && !deleted.lock().unwrap().contains(&session) { thread::sleep(Duration::from_millis(2)); }
            return;
        }
        Reply::Cut => { let _ = write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n{session_header}Connection: close\r\n\r\n"); return; }
        Reply::Result(result) => (200,"application/json",json!({"jsonrpc":"2.0","id":message["id"],"result":result}).to_string(),String::new()),
        Reply::RpcError(code,message_text,data) => (200,"application/json",json!({"jsonrpc":"2.0","id":message["id"],"error":{"code":code,"message":message_text,"data":data}}).to_string(),String::new()),
        Reply::Http(status,body) => (status,"application/json",body,String::new()),
        Reply::Redirect(url) => (307,"application/json","{}".to_owned(),format!("Location: {url}\r\n")),
        Reply::Sse(result) => (200,"text/event-stream",format!("data: {}\n\n",json!({"jsonrpc":"2.0","id":message["id"],"result":result})),String::new()),
    };
    let _ = write!(
        stream,
        "HTTP/1.1 {status} Fixture\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\n{session_header}{extra}Connection: close\r\n\r\n{body}",
        body.len()
    );
}
pub struct Shim {
    process: Child,
    input: Option<ChildStdin>,
    pub output: mpsc::Receiver<Value>,
    reader: Option<thread::JoinHandle<()>>,
    home: std::path::PathBuf,
    raw: Arc<Mutex<Vec<Vec<u8>>>>,
    stderr: Arc<Mutex<Vec<u8>>>,
    stderr_reader: Option<thread::JoinHandle<()>>,
}
impl Shim {
    pub fn start(url: &str) -> Self {
        Self::start_with_env(url, &[])
    }
    pub fn start_with_env(url: &str, env: &[(&str, &str)]) -> Self {
        let home = std::env::temp_dir().join(format!(
            "pseudolife-stdio-contract-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4()
        ));
        std::fs::create_dir_all(&home).unwrap();
        let mut process = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .env("HOME", &home)
            .env("USERPROFILE", &home)
            .env("PSEUDOLIFE_AGENT_STATE_DIR", &home)
            .env("PSEUDOLIFE_MCP_DAEMON_URL", url)
            .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
            .env("PSEUDOLIFE_AGENT_COORDINATION", "0")
            .env("PSEUDOLIFE_CODEX_DOORBELL", "0")
            .env("PSEUDOLIFE_RELEASE_CHECK", "0")
            .env_remove("PSEUDOLIFE_MCP_TOKEN_FILE")
            .env_remove("PSEUDOLIFE_MCP_TOKEN")
            .envs(env.iter().copied())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let input = process.stdin.take();
        let stdout = process.stdout.take().unwrap();
        let mut errors = process.stderr.take().unwrap();
        let stderr = Arc::new(Mutex::new(Vec::new()));
        let captured_stderr = stderr.clone();
        let stderr_reader = thread::spawn(move || {
            let mut bytes = Vec::new();
            errors.read_to_end(&mut bytes).unwrap();
            *captured_stderr.lock().unwrap() = bytes;
        });
        let (sender, output) = mpsc::channel();
        let raw = Arc::new(Mutex::new(Vec::new()));
        let captured = raw.clone();
        let reader = thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                let mut line = Vec::new();
                if reader.read_until(b'\n', &mut line).unwrap() == 0 {
                    break;
                }
                let value = serde_json::from_slice(&line).unwrap();
                captured.lock().unwrap().push(line);
                if sender.send(value).is_err() {
                    break;
                }
            }
        });
        Self {
            process,
            input,
            output,
            reader: Some(reader),
            home,
            raw,
            stderr,
            stderr_reader: Some(stderr_reader),
        }
    }
    pub fn state_dir(&self) -> &std::path::Path {
        &self.home
    }
    pub fn raw(&self) -> Vec<Vec<u8>> {
        self.raw.lock().unwrap().clone()
    }
    pub fn stderr(&self) -> String {
        String::from_utf8_lossy(&self.stderr.lock().unwrap()).into_owned()
    }
    pub fn send(&mut self, frame: Value) {
        let input = self.input.as_mut().unwrap();
        writeln!(input, "{frame}").unwrap();
        input.flush().unwrap();
    }
    pub fn receive(&self) -> Value {
        self.output
            .recv_timeout(Duration::from_secs(12))
            .expect("required stdio response")
    }
    pub fn finish(&mut self) -> Vec<Value> {
        self.input.take();
        let deadline = Instant::now() + Duration::from_secs(12);
        let status = loop {
            if let Some(status) = self.process.try_wait().unwrap() {
                break status;
            }
            assert!(Instant::now() < deadline, "EOF shutdown timed out");
            thread::sleep(Duration::from_millis(5));
        };
        assert!(status.success(), "candidate exit: {status}");
        self.reader.take().unwrap().join().unwrap();
        self.stderr_reader.take().unwrap().join().unwrap();
        self.output.try_iter().collect()
    }
}
impl Drop for Shim {
    fn drop(&mut self) {
        let _ = self.process.kill();
        let _ = self.process.wait();
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
        if let Some(reader) = self.stderr_reader.take() {
            let _ = reader.join();
        }
        let _ = std::fs::remove_dir_all(&self.home);
    }
}
pub fn meta() -> Value {
    json!({"io.modelcontextprotocol/protocolVersion":"2026-07-28", "io.modelcontextprotocol/clientCapabilities":{}, "io.modelcontextprotocol/clientInfo":{"name":"eof-contract","version":"1"}})
}

/// Windows reserves the exited child's PID with its retained process handle.
/// Other platforms retain the existing reaped-PID fixture and its reuse risk.
pub struct CompletedPid {
    pid: u32,
    #[cfg(windows)]
    _child: Child,
}
impl CompletedPid {
    pub fn id(&self) -> u32 {
        self.pid
    }
}

/// Disposable lease homes retain the local and board tests' original profiles.
pub struct LeaseHome(pub std::path::PathBuf);
impl LeaseHome {
    fn create(prefix: &str) -> Self {
        let path = std::env::temp_dir().join(format!("{prefix}-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&path).unwrap();
        Self(path)
    }
    pub fn new() -> Self {
        Self::create("native-lease-test")
    }
    pub fn board() -> Self {
        let home = Self::create("lease-board-test");
        std::fs::write(home.0.join("instance.id"), b"0123456789ab\n").unwrap();
        home
    }
    fn base_command(&self, url: &str) -> Command {
        let mut command =
            cli::cleared_command(std::path::Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
        command
            .env("HOME", &self.0)
            .env("USERPROFILE", &self.0)
            .env("CUDA_VISIBLE_DEVICES", "-1")
            .env("OMP_NUM_THREADS", "1")
            .env("MKL_NUM_THREADS", "1")
            .env("PSEUDOLIFE_LEASE_LOCK_DIR", &self.0)
            .env("PSEUDOLIFE_SUITE_LOCK_DIR", &self.0)
            .env("PSEUDOLIFE_MCP_DAEMON_URL", url)
            .env("PSEUDOLIFE_DAEMON_EXEC", "1")
            .env("PSEUDOLIFE_MCP_NO_SPAWN", "1");
        command
    }
    pub fn command(&self) -> Command {
        let mut command = self.base_command("http://127.0.0.1:1");
        command.env("LOCALAPPDATA", self.0.join("local"));
        command
    }
    pub fn board_command(&self, url: &str) -> Command {
        let mut command = self.base_command(url);
        command.env("PSEUDOLIFE_MCP_TOKEN", "fixture-bearer");
        command
    }
    pub fn call(&self, args: &[&str]) -> std::process::Output {
        self.command().args(args).output().unwrap()
    }
    pub fn completed_pid(&self) -> CompletedPid {
        let mut child = self
            .command()
            .args(["lease", "--help"])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let pid = child.id();
        assert!(child.wait().unwrap().success());
        CompletedPid {
            pid,
            #[cfg(windows)]
            _child: child,
        }
    }
}
impl Drop for LeaseHome {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
