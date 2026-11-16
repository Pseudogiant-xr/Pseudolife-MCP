//! `tunnel shim` (hidden): the MCP server OpenAI's `tunnel-client` starts.
//!
//! The bridge runs this runtime's own stdio shim (`<this executable> shim`)
//! as its child, pointed at the profile's daemon with the profile's token
//! file, and relays the newline-framed JSON-RPC both ways unchanged. It adds
//! two duties on top of the relay:
//!
//! - **Receipts.** A successful `memory_search` (query) or `memory_agents`
//!   `list` (project) call whose text carries this profile's pending
//!   verification nonce writes a payload-free receipt beside the challenge,
//!   owner-only, after its answer has been relayed. Nothing else does.
//! - **Catalog gate.** With `minimum_catalog: full`, each `tools/list` is
//!   held until `memory_toolset` reports the full tier (expanding at most
//!   twice); otherwise the catalog is served as the daemon sets it.
//!
//! What the bridge trusts: the profile named on its command line, as the
//! owner-only store holds it, and the store's owner-only files (the saved
//! challenge). What it does not: anything in the MCP stream. A nonce there is
//! only compared with the saved challenge, never run or used to build a
//! path; a receipt's name and fields come from the saved challenge. The
//! bridge prints no token, key or nonce, and its child never inherits a
//! token or daemon address from the bridge's own environment.
//!
//! A receipt's `identity` is the challenge's, copied: this bridge checks the
//! profile it serves is unchanged since it started, but there is no native
//! launch identity yet to compare with the challenge's, so a reader must not
//! take the field as proof of which launch answered until one exists.
use std::{
    collections::{HashMap, HashSet},
    ffi::{OsStr, OsString},
    io::Write,
    path::{Path, PathBuf},
    process::{ExitCode, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use serde_json::{Map, Value, json};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

use super::{Fail, store, syntax};

const USAGE: &str = "tunnel shim is started by the tunnel runtime, not by hand: it needs --profile <name>, and --profile-dir <directory> for a store other than the default";
const NOT_READY: &str =
    "tunnel profile is not ready; finish `pseudolife-mcp tunnel setup` before its bridge can start";
const UNREADABLE: &str =
    "tunnel profile cannot be read safely; `pseudolife-mcp tunnel status` names the cause";
const NO_SHIM: &str =
    "tunnel bridge could not start the stdio shim; check this install with `pseudolife-mcp doctor`";
const RECEIPT_FAILED: &str = "tunnel bridge: a verification receipt could not be saved; the session continues, and `pseudolife-mcp tunnel verify` can be repeated";
const DISCOVERY_FAILED: &str =
    "Full tool discovery unavailable; run pseudolife-mcp doctor on the tunnel host.";

/// What the child shim must not inherit from the bridge's environment: any
/// `PSEUDOLIFE_MCP_TOKEN*` (a token, a token file, a daemon's token list),
/// any daemon address, and the vendor's own API key. Everything else is
/// inherited: the shim also reads its home, state folders, TLS and proxy
/// settings and timeouts from the environment, and an allowlist would drop
/// one a user relies on without a word. Names compare without case, as
/// Windows compares them.
fn dropped(name: &OsStr) -> bool {
    let name = name.to_string_lossy().to_ascii_uppercase();
    name.starts_with("PSEUDOLIFE_MCP_TOKEN")
        || name == "PSEUDOLIFE_MCP_DAEMON_URL"
        || name == "OPENAI_API_KEY"
}

/// A JSON-RPC message the observer and the gate parse; larger lines are
/// relayed but never parsed.
const PARSE_LIMIT: usize = 4 * 1024 * 1024;
/// Calls awaiting their answer, client requests in flight, and `tools/list`
/// requests held by the gate.
const PENDING_LIMIT: usize = 256;
const IN_FLIGHT_LIMIT: usize = 1024;
const HELD_LIMIT: usize = 64;
/// How long a held `tools/list` waits for the catalog before it is refused.
const GATE_TIMEOUT: Duration = Duration::from_secs(15);
/// How long the child shim gets to exit once its input closed.
const CHILD_EXIT: Duration = Duration::from_secs(5);
/// How long the relay outlives the client's leaving: past a held listing's
/// wait and the shim's exit above. It bounds a shim that never takes its
/// input and a client that stopped reading without closing.
const CLIENT_GONE: Duration = Duration::from_secs(30);
/// How long the shim may take none of its waiting input before it is
/// treated as hung and the session ends. A working shim reads its input
/// continuously; this only ends one that stopped.
const SHIM_STALL: Duration = Duration::from_secs(60);
/// Lines a direction queues before its reader waits for its writer.
const QUEUE: usize = 64;

/// The verification tools whose successful calls write a receipt.
const TOOLS: [&str; 2] = ["memory_search", "memory_agents"];

/// `--profile <name>` (required) and `--profile-dir <directory>`, each once,
/// in any order.
fn parse(values: &[OsString]) -> Option<(String, Option<String>)> {
    let values: Vec<&str> = values.iter().map(|v| v.to_str()).collect::<Option<_>>()?;
    let (mut profile, mut directory) = (None, None);
    let mut rest = values.iter();
    while let Some(option) = rest.next() {
        let value = rest
            .next()
            .filter(|value| !value.is_empty() && !value.starts_with('-'))?;
        let slot = match *option {
            "--profile" => &mut profile,
            "--profile-dir" => &mut directory,
            _ => return None,
        };
        if slot.replace((*value).to_owned()).is_some() {
            return None;
        }
    }
    Some((profile?, directory))
}

/// What the bridge was started with: the store and its validated profile.
struct Start {
    root: PathBuf,
    profile: store::Profile,
}

fn prepare(values: &[OsString]) -> Result<Start, &'static str> {
    let (name, directory) = parse(values).ok_or(USAGE)?;
    let root = store::store_root(directory.as_deref()).map_err(|_| store::UNSAFE_ROOT)?;
    let profile = match store::load(&root, &name) {
        Ok(profile) => profile,
        Err(Fail::Tunnel(message)) => return Err(message),
        Err(Fail::Defer) => return Err(UNREADABLE),
    };
    // A ready profile carries the user's consent and a tunnel identifier
    // (store::validate refuses one without them).
    if profile.state != "ready" {
        return Err(NOT_READY);
    }
    Ok(Start { root, profile })
}

/// The child's environment: the names of `inherited` it must not see, then
/// what is set from the profile.
fn child_environment(
    profile: &store::Profile,
    inherited: impl Iterator<Item = OsString>,
) -> (Vec<OsString>, [(&'static str, String); 3]) {
    (
        inherited.filter(|name| dropped(name)).collect(),
        [
            ("PSEUDOLIFE_MCP_DAEMON_URL", profile.daemon_url.clone()),
            ("PSEUDOLIFE_MCP_TOKEN_FILE", profile.token_file.clone()),
            // The bridge serves a daemon that already runs; it starts none.
            ("PSEUDOLIFE_MCP_NO_SPAWN", "1".to_owned()),
        ],
    )
}

/// `CREATE_NO_WINDOW` for the child when this process has no console (the
/// vendor starts the bridge with pipes only), so the console child never
/// opens a window; with a console the child shares it. The pattern of
/// `restore_cli/child_window.rs`, kept here so tunnel does not import
/// restore_cli's private module.
#[cfg(windows)]
#[allow(unsafe_code)]
fn window_flag() -> u32 {
    use windows_sys::Win32::System::Console::GetConsoleProcessList;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let mut members = [0_u32; 1];
    // SAFETY: `members` is a writable buffer of the length passed; the call
    // writes at most that many process ids and returns the console's
    // process count (0 when no console is attached, or on failure).
    let count = unsafe { GetConsoleProcessList(members.as_mut_ptr(), members.len() as u32) };
    if count == 0 { CREATE_NO_WINDOW } else { 0 }
}

pub(super) fn run(values: &[OsString]) -> ExitCode {
    let start = match prepare(values) {
        Ok(start) => start,
        Err(message) => {
            let text = super::super::text_bytes(&format!("{message}\n"));
            let _ = std::io::stderr().lock().write_all(&text);
            return ExitCode::from(2);
        }
    };
    let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    else {
        crate::stderrln!("{NO_SHIM}");
        return ExitCode::FAILURE;
    };
    let code = runtime.block_on(serve(start));
    // A read of the bridge's stdin may still be pending once the shim has
    // gone; it must not keep the process alive.
    runtime.shutdown_timeout(Duration::from_millis(100));
    code
}

async fn serve(start: Start) -> ExitCode {
    // The runtime the vendor started this bridge from: the same binary, so
    // the bridge and its shim never straddle a runtime update.
    let Ok(executable) = std::env::current_exe() else {
        crate::stderrln!("{NO_SHIM}");
        return ExitCode::FAILURE;
    };
    let mut command = tokio::process::Command::new(executable);
    command.arg("shim");
    let (dropped, set) =
        child_environment(&start.profile, std::env::vars_os().map(|(name, _)| name));
    for key in dropped {
        command.env_remove(key);
    }
    #[cfg(windows)]
    command.creation_flags(window_flag());
    for (key, value) in set {
        command.env(key, value);
    }
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        // The vendor may not drain a second stream; the shim's diagnostics
        // are for local clients, not the cloud session.
        .stderr(Stdio::null())
        // The child never outlives the bridge: on a normal exit it is
        // waited for or killed here, and if the bridge itself is killed the
        // OS closes the bridge's end of the child's input, the only other
        // handle to it, and the shim ends on that EOF.
        .kill_on_drop(true);
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(_) => {
            crate::stderrln!("{NO_SHIM}");
            return ExitCode::FAILURE;
        }
    };
    let (Some(shim_in), Some(shim_out)) = (child.stdin.take(), child.stdout.take()) else {
        crate::stderrln!("{NO_SHIM}");
        return ExitCode::FAILURE;
    };
    let observer = Observer::new(
        start.root,
        &start.profile,
        Reporter::stderr(),
        private_sink(),
    );
    let gate = Gate::new(start.profile.catalog == "full");
    let _ = relay(
        tokio::io::stdin(),
        tokio::io::stdout(),
        shim_in,
        shim_out,
        Some(observer),
        gate,
    )
    .await;
    match tokio::time::timeout(CHILD_EXIT, child.wait()).await {
        Ok(Ok(status)) => ExitCode::from(
            status
                .code()
                .and_then(|c| u8::try_from(c).ok())
                .unwrap_or(1),
        ),
        _ => {
            let _ = child.kill().await;
            ExitCode::FAILURE
        }
    }
}

/// What the client's writer is handed: a line to write, or a verification
/// proof to settle once every line queued before it has been written.
enum Out {
    Line(Vec<u8>),
    Settle(Box<Proof>),
}

/// What both directions of one relay share. The lock is never held across
/// an await.
struct Relay {
    state: std::sync::Mutex<(Option<Observer>, Gate)>,
    /// The gate's deadline or waiting state changed.
    gate_changed: tokio::sync::Notify,
    /// The client's input ended, or its output can no longer be written.
    client_ended: CancellationToken,
    /// The shim's input has been closed.
    shim_closed: CancellationToken,
    /// The shim's output ended: everything else stops.
    stopping: CancellationToken,
}

impl Relay {
    fn lock(&self) -> std::sync::MutexGuard<'_, (Option<Observer>, Gate)> {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Queues a line for the client. The observer sees it as the client
    /// will, whether the shim or the gate wrote it, and a proof it yields is
    /// queued behind it.
    async fn to_client(&self, queue: &mpsc::Sender<Out>, line: Vec<u8>) {
        let proof = match (self.lock().0.as_mut(), message(&line)) {
            (Some(observer), Some(message)) => observer.response(&message),
            _ => None,
        };
        let proof = proof.map(|proof| Out::Settle(Box::new(proof)));
        for item in std::iter::once(Out::Line(line)).chain(proof) {
            // A client that stopped reading never holds the relay past its
            // shutdown.
            tokio::select! {
                _ = queue.send(item) => {}
                () = self.stopping.cancelled() => return,
            }
        }
    }

    /// Waits for a change to the gate, from the moment this is called.
    fn gate_change(&self) -> impl Future<Output = ()> + '_ {
        let mut changed = Box::pin(self.gate_changed.notified());
        changed.as_mut().enable();
        changed
    }
}

/// Relay the client's lines to the shim and the shim's lines to the client,
/// byte for byte, until the shim's output ends. The observer only reads; the
/// gate holds or answers `tools/list` when it is enabled, and otherwise
/// passes every line through.
///
/// Each direction has its own reader, writer and bounded queue, so each
/// pushes back only on its own source: a shim that is not reading its input
/// (while it attaches, say) never stops the bridge draining its output. The
/// gate's own few lines to the shim take a lane of their own. Once the
/// shim's input has closed it has `CHILD_EXIT` to end its output. Receipts
/// settle after their answer is written, and have settled when this
/// returns.
pub(super) async fn relay<CI, CO, SI, SO>(
    client_in: CI,
    client_out: CO,
    shim_in: SI,
    shim_out: SO,
    observer: Option<Observer>,
    gate: Gate,
) -> std::io::Result<()>
where
    CI: AsyncRead + Unpin,
    CO: AsyncWrite + Unpin,
    SI: AsyncWrite + Unpin,
    SO: AsyncRead + Unpin,
{
    let relay = Relay {
        state: std::sync::Mutex::new((observer, gate)),
        gate_changed: tokio::sync::Notify::new(),
        client_ended: CancellationToken::new(),
        shim_closed: CancellationToken::new(),
        stopping: CancellationToken::new(),
    };
    let (to_client, client_queue) = mpsc::channel(QUEUE);
    let (to_shim, shim_queue) = mpsc::channel(QUEUE);
    let (gate_to_shim, gate_queue) = mpsc::unbounded_channel();
    let outbound = outbound(&relay, shim_out, gate_to_shim, to_client.clone());
    let rest = async {
        tokio::join!(
            inbound(&relay, client_in, to_shim, to_client),
            write_client(&relay, client_out, client_queue),
            write_shim(&relay, shim_in, gate_queue, shim_queue),
        )
    };
    // Once the client has left, the relay ends within CLIENT_GONE however
    // either side behaves.
    let gone = async {
        relay.client_ended.cancelled().await;
        tokio::time::sleep(CLIENT_GONE).await;
    };
    tokio::pin!(outbound, rest, gone);
    // `rest` ends only after the client's queue closes, which needs
    // `outbound` to have ended first.
    let (result, finished) = tokio::select! {
        result = &mut outbound => (result, false),
        _ = &mut rest => (Ok(()), true),
        () = &mut gone => (Ok(()), false),
        () = relay.stopping.cancelled() => (Ok(()), false),
    };
    relay.stopping.cancel();
    if !finished {
        // What is queued is written, unless a side stopped reading for good.
        let _ = tokio::time::timeout(CHILD_EXIT, &mut rest).await;
    }
    result
}

/// The client's lines, to the shim (or answered by the gate).
async fn inbound<CI: AsyncRead + Unpin>(
    relay: &Relay,
    client_in: CI,
    to_shim: mpsc::Sender<Vec<u8>>,
    to_client: mpsc::Sender<Out>,
) {
    let mut reader = BufReader::new(client_in);
    // Kept across select! rounds: a partly read line stays in its buffer.
    let mut buffer = Vec::new();
    'read: loop {
        let read = tokio::select! {
            read = reader.read_until(b'\n', &mut buffer) => read,
            () = relay.client_ended.cancelled() => break,
            () = relay.stopping.cancelled() => break,
        };
        // A read that adds nothing is the end of the stream, even when an
        // earlier, cancelled read left an unended last line in the buffer.
        let ended = !matches!(read, Ok(n) if n > 0);
        let line = std::mem::take(&mut buffer);
        if !line.is_empty() {
            let message = message(&line);
            let actions = {
                let mut state = relay.lock();
                let (observer, gate) = &mut *state;
                if let (Some(observer), Some(message)) = (observer.as_mut(), message.as_ref()) {
                    observer.request(message);
                }
                gate.inbound(line, message.as_ref())
            };
            relay.gate_changed.notify_waiters();
            for action in actions {
                match action {
                    Action::ToShim(bytes) => tokio::select! {
                        _ = to_shim.send(bytes) => {}
                        () = relay.stopping.cancelled() => break 'read,
                        () = tokio::time::sleep(SHIM_STALL) => {
                            // The shim has taken nothing for a minute while
                            // its input waits: it is hung.
                            relay.stopping.cancel();
                            break 'read;
                        }
                    },
                    Action::ToClient(bytes) => relay.to_client(&to_client, bytes).await,
                }
            }
        }
        if ended {
            break;
        }
    }
    relay.client_ended.cancel();
    // Held listings still need the shim: its input stays open until the
    // gate has settled them.
    loop {
        let changed = relay.gate_change();
        if !relay.lock().1.waiting() || relay.stopping.is_cancelled() {
            break;
        }
        tokio::select! {
            () = changed => {}
            () = relay.stopping.cancelled() => break,
        }
    }
    // Dropping the last strong sender closes the shim's input.
    drop(to_shim);
}

/// The shim's lines, to the client (or to the gate), until the shim's
/// output ends.
async fn outbound<SO: AsyncRead + Unpin>(
    relay: &Relay,
    shim_out: SO,
    gate_to_shim: mpsc::UnboundedSender<Vec<u8>>,
    to_client: mpsc::Sender<Out>,
) -> std::io::Result<()> {
    let mut reader = BufReader::new(shim_out);
    let mut buffer = Vec::new();
    // Once the shim's input has closed, it must end its output in time.
    let mut closing: Option<tokio::time::Instant> = None;
    loop {
        if closing.is_none() && relay.shim_closed.is_cancelled() {
            closing = Some(tokio::time::Instant::now() + CHILD_EXIT);
        }
        let changed = relay.gate_change();
        let deadline = relay.lock().1.deadline;
        let mut done = None;
        let actions = tokio::select! {
            read = reader.read_until(b'\n', &mut buffer) => {
                match read {
                    Ok(n) if n > 0 => {}
                    Ok(_) => done = Some(Ok(())),
                    Err(error) => done = Some(Err(error)),
                }
                let line = std::mem::take(&mut buffer);
                if line.is_empty() {
                    Vec::new()
                } else {
                    let message = message(&line);
                    let actions = relay.lock().1.outbound(line, message.as_ref());
                    relay.gate_changed.notify_waiters();
                    actions
                }
            },
            () = sleep_until(deadline), if deadline.is_some() => {
                let actions = relay.lock().1.expire();
                relay.gate_changed.notify_waiters();
                actions
            },
            () = sleep_until(closing), if closing.is_some() => return Ok(()),
            () = relay.shim_closed.cancelled(), if closing.is_none() => Vec::new(),
            () = relay.stopping.cancelled() => return Ok(()),
            () = changed => Vec::new(),
        };
        for action in actions {
            match action {
                // Held listings and the gate's own calls; a handful at most.
                Action::ToShim(bytes) => {
                    let _ = gate_to_shim.send(bytes);
                }
                Action::ToClient(bytes) => relay.to_client(&to_client, bytes).await,
            }
        }
        if let Some(done) = done {
            return done;
        }
    }
}

/// Writes the client's lines in order; settles each proof, on the blocking
/// pool, once the answer queued before it has been written.
async fn write_client<CO: AsyncWrite + Unpin>(
    relay: &Relay,
    mut writer: CO,
    mut queue: mpsc::Receiver<Out>,
) {
    let mut open = true;
    let mut receipts = tokio::task::JoinSet::new();
    while let Some(item) = queue.recv().await {
        match item {
            Out::Line(bytes) => {
                if open
                    && (writer.write_all(&bytes).await.is_err() || writer.flush().await.is_err())
                {
                    // The client is gone: the session ends as if its input
                    // had.
                    open = false;
                    relay.client_ended.cancel();
                }
            }
            // An answer the client never received proves nothing.
            Out::Settle(proof) if open => {
                receipts.spawn_blocking(move || proof.settle());
            }
            Out::Settle(_) => {}
        }
        while receipts.try_join_next().is_some() {}
    }
    while receipts.join_next().await.is_some() {}
}

/// Writes the shim's lines in order, the gate's first; the shim's input
/// closes once the client's queue has closed and been written.
async fn write_shim<SI: AsyncWrite + Unpin>(
    relay: &Relay,
    mut writer: SI,
    mut gate_queue: mpsc::UnboundedReceiver<Vec<u8>>,
    mut queue: mpsc::Receiver<Vec<u8>>,
) {
    let mut open = true;
    let mut gate_open = true;
    loop {
        let bytes = tokio::select! {
            biased;
            bytes = gate_queue.recv(), if gate_open => match bytes {
                Some(bytes) => bytes,
                None => {
                    gate_open = false;
                    continue;
                }
            },
            bytes = queue.recv() => match bytes {
                Some(bytes) => bytes,
                None => break,
            },
            () = relay.stopping.cancelled() => break,
        };
        if open && (writer.write_all(&bytes).await.is_err() || writer.flush().await.is_err()) {
            // The shim stopped reading: its output ends next.
            open = false;
        }
    }
    let _ = writer.shutdown().await;
    drop(writer);
    relay.shim_closed.cancel();
}

async fn sleep_until(deadline: Option<tokio::time::Instant>) {
    match deadline {
        Some(deadline) => tokio::time::sleep_until(deadline).await,
        None => std::future::pending().await,
    }
}

/// A line as a JSON-RPC object, when it is one the bridge reads.
fn message(line: &[u8]) -> Option<Map<String, Value>> {
    if line.len() > PARSE_LIMIT {
        return None;
    }
    match serde_json::from_slice(line) {
        Ok(Value::Object(map)) => Some(map),
        _ => None,
    }
}

/// A JSON-RPC id the bridge correlates: a string or an integer.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum Id {
    Text(String),
    Integer(i128),
}

fn identifier(message: &Map<String, Value>) -> Option<Id> {
    match message.get("id")? {
        Value::String(text) => Some(Id::Text(text.clone())),
        Value::Number(number) => number
            .as_i64()
            .map(i128::from)
            .or_else(|| number.as_u64().map(i128::from))
            .map(Id::Integer),
        _ => None,
    }
}

/// Python's truthiness, for the payload checks below.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// A `tools/call` result that is a well-formed success: content blocks
/// with a type, no `isError: true`, and no payload that reports a failure
/// (an error, `ok`/`success`/`available` false) in its structured content
/// or a JSON text block.
fn succeeded(message: &Map<String, Value>) -> bool {
    if message.contains_key("error") {
        return false;
    }
    let Some(Value::Object(result)) = message.get("result") else {
        return false;
    };
    let Some(Value::Array(content)) = result.get("content") else {
        return false;
    };
    if !content
        .iter()
        .all(|block| block.get("type").is_some_and(Value::is_string))
    {
        return false;
    }
    if !matches!(result.get("isError"), None | Some(Value::Bool(false))) {
        return false;
    }
    let structured = match result.get("structuredContent") {
        None | Some(Value::Null) => None,
        Some(Value::Object(map)) => Some(map.clone()),
        Some(_) => return false,
    };
    let texts = content.iter().filter_map(|block| {
        block
            .get("text")
            .and_then(Value::as_str)
            .and_then(message_text)
    });
    !structured.into_iter().chain(texts).any(|payload| {
        payload.get("error").is_some_and(truthy)
            || payload.get("errors").is_some_and(truthy)
            || ["ok", "success", "available"]
                .iter()
                .any(|key| payload.get(*key) == Some(&Value::Bool(false)))
    })
}

fn message_text(text: &str) -> Option<Map<String, Value>> {
    message(text.as_bytes())
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0)
}

/// Whether `text` holds a run of 32 lowercase hex digits, the only shape a
/// saved nonce has; any other call is never compared with the challenge.
fn nonce_shaped(text: &str) -> bool {
    let mut run = 0;
    for byte in text.bytes() {
        if matches!(byte, b'0'..=b'9' | b'a'..=b'f') {
            run += 1;
            if run >= 32 {
                return true;
            }
        } else {
            run = 0;
        }
    }
    false
}

fn lowercase_hex(text: &str, length: usize) -> bool {
    text.len() == length && text.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// The pending verification challenge `tunnel verify` saved for a profile.
#[derive(Clone, Debug, PartialEq)]
struct Challenge {
    nonce: String,
    identity: String,
    created: f64,
}

/// The profile's challenge, if one is saved, owner-only and well formed.
fn read_challenge(root: &Path, name: &str) -> Option<Challenge> {
    let path = root
        .join(format!("{name}-verification"))
        .join("challenge.json");
    let data = store::private_read(&path, 65536).ok()?;
    let syntax::Admit::Yes(Value::Object(map)) = syntax::json_loads(&data) else {
        return None;
    };
    let nonce = map.get("nonce")?.as_str()?;
    let identity = map.get("identity")?.as_str()?;
    let created = match map.get("created")? {
        Value::Number(number) => number.as_f64()?,
        _ => return None,
    };
    (lowercase_hex(nonce, 32) && lowercase_hex(identity, 64) && created.is_finite()).then(|| {
        Challenge {
            nonce: nonce.to_owned(),
            identity: identity.to_owned(),
            created,
        }
    })
}

/// The profile fields that decide where the bridge's calls go; a receipt is
/// written only while the saved profile still names them.
#[derive(Clone, Debug, PartialEq)]
struct Binding {
    name: String,
    daemon_url: String,
    token_file: String,
    tunnel_id: Option<String>,
    organization_id: Option<String>,
}

impl Binding {
    fn of(profile: &store::Profile) -> Self {
        Self {
            name: profile.name.clone(),
            daemon_url: profile.daemon_url.clone(),
            token_file: profile.token_file.clone(),
            tunnel_id: profile.tunnel_id.clone(),
            organization_id: profile.organization_id.clone(),
        }
    }
}

/// Writes one receipt; the relay tests record instead.
pub(super) type Sink = Arc<dyn Fn(&Path, &[u8]) -> Result<(), ()> + Send + Sync>;

/// The owner-only atomic write the native client uses for private files.
fn private_sink() -> Sink {
    Arc::new(|path, bytes| crate::board::state::atomic_write(path, bytes, None).map_err(|_| ()))
}

/// Says once per bridge that a receipt could not be saved.
#[derive(Clone)]
pub(super) struct Reporter {
    said: Arc<AtomicBool>,
    say: fn(&str),
}

impl Reporter {
    fn stderr() -> Self {
        Self {
            said: Arc::default(),
            say: |line| crate::stderrln!("{line}"),
        }
    }
    fn failed(&self) {
        if !self.said.swap(true, Ordering::SeqCst) {
            (self.say)(RECEIPT_FAILED);
        }
    }
}

/// A verification call observed on its way to the shim.
struct Call {
    tool: &'static str,
    supplied: String,
    asked: f64,
}

/// Watches the relayed stream for verification calls; never changes it.
pub(super) struct Observer {
    root: PathBuf,
    binding: Binding,
    pending: HashMap<Id, Call>,
    /// Every client request id not yet answered: a verification call
    /// sent under an id already in flight is never credited.
    in_flight: HashSet<Id>,
    reporter: Reporter,
    sink: Sink,
}

impl Observer {
    fn new(root: PathBuf, profile: &store::Profile, reporter: Reporter, sink: Sink) -> Self {
        Self {
            root,
            binding: Binding::of(profile),
            pending: HashMap::new(),
            in_flight: HashSet::new(),
            reporter,
            sink,
        }
    }

    /// A client request: remember a verification-shaped call by its id.
    /// Reads nothing from disk.
    fn request(&mut self, message: &Map<String, Value>) {
        if !message.contains_key("method") {
            return;
        }
        let Some(id) = identifier(message) else {
            return;
        };
        if self.in_flight.len() >= IN_FLIGHT_LIMIT {
            // Requests a client cancelled are never answered; forget them.
            self.in_flight.clear();
        }
        // A reused id makes either answer ambiguous: neither call is
        // credited.
        let reused = !self.in_flight.insert(id.clone());
        self.pending.remove(&id);
        if reused
            || self.pending.len() >= PENDING_LIMIT
            || message.get("method").and_then(Value::as_str) != Some("tools/call")
        {
            return;
        }
        let Some(params) = message.get("params").and_then(Value::as_object) else {
            return;
        };
        let Some(arguments) = params.get("arguments").and_then(Value::as_object) else {
            return;
        };
        let (tool, supplied) = match params.get("name").and_then(Value::as_str) {
            Some("memory_search") => (TOOLS[0], arguments.get("query")),
            Some("memory_agents") if arguments.get("action") == Some(&json!("list")) => {
                (TOOLS[1], arguments.get("project"))
            }
            _ => return,
        };
        let Some(Value::String(supplied)) = supplied else {
            return;
        };
        if nonce_shaped(supplied) {
            self.pending.insert(
                id,
                Call {
                    tool,
                    supplied: supplied.clone(),
                    asked: now(),
                },
            );
        }
    }

    /// A shim answer: the proof to settle, when it answers a remembered
    /// call with a success.
    fn response(&mut self, message: &Map<String, Value>) -> Option<Proof> {
        if message.contains_key("method") {
            return None;
        }
        let id = identifier(message)?;
        self.in_flight.remove(&id);
        let call = self.pending.remove(&id)?;
        succeeded(message).then(|| Proof {
            root: self.root.clone(),
            binding: self.binding.clone(),
            call,
            reporter: self.reporter.clone(),
            sink: self.sink.clone(),
        })
    }
}

/// A successful verification-shaped call, checked against the saved
/// challenge and profile before its receipt is written.
struct Proof {
    root: PathBuf,
    binding: Binding,
    call: Call,
    reporter: Reporter,
    sink: Sink,
}

impl Proof {
    /// Writes the receipt when the call carried this profile's pending
    /// nonce; the path written, if any. A failed write is reported once.
    fn settle(self) -> Option<PathBuf> {
        let name = &self.binding.name;
        let challenge = read_challenge(&self.root, name)?;
        // Compared only; the receipt's name and fields come from the saved
        // challenge, never from the stream.
        if !self.call.supplied.contains(&challenge.nonce) || challenge.created > self.call.asked {
            return None;
        }
        // The profile still names the daemon and tunnel this bridge serves.
        match store::load(&self.root, name) {
            Ok(profile) if profile.state == "ready" && Binding::of(&profile) == self.binding => {}
            _ => return None,
        }
        let written = (|| {
            let directory = store::checked_path(&self.root.join(format!("{name}-verification")))
                .map_err(|_| ())?;
            let path = directory.join(format!("{}-{}.json", challenge.nonce, self.call.tool));
            let receipt = json!({
                "identity": challenge.identity,
                "nonce": challenge.nonce,
                "tool": self.call.tool,
                "success": true,
                "observed": now(),
            });
            (self.sink)(&path, receipt.to_string().as_bytes())?;
            Ok::<_, ()>(path)
        })();
        match written {
            Ok(path) => Some(path),
            Err(()) => {
                self.reporter.failed();
                None
            }
        }
    }
}

/// Where the relay sends a line.
#[derive(Debug, PartialEq)]
pub(super) enum Action {
    ToShim(Vec<u8>),
    ToClient(Vec<u8>),
}

/// Holds `tools/list` until the full catalog is visible, when the profile
/// asks for it. Each `tools/list` checks the tier afresh: another session of
/// the same principal may have collapsed it since.
pub(super) struct Gate {
    enabled: bool,
    prefix: String,
    count: u64,
    current: Option<String>,
    held: Vec<(Vec<u8>, Value)>,
    expansions: u8,
    deadline: Option<tokio::time::Instant>,
    meta: Option<Value>,
}

impl Gate {
    pub(super) fn new(enabled: bool) -> Self {
        Self {
            enabled,
            prefix: format!("pseudolife-tunnel-{}-", uuid::Uuid::new_v4().simple()),
            count: 0,
            current: None,
            held: Vec::new(),
            expansions: 0,
            deadline: None,
            meta: None,
        }
    }

    fn waiting(&self) -> bool {
        self.deadline.is_some()
    }

    fn internal(&self, id: &Value) -> bool {
        let Some(suffix) = id.as_str().and_then(|id| id.strip_prefix(&self.prefix)) else {
            return false;
        };
        !suffix.is_empty()
            && suffix.len() <= 16
            && suffix.bytes().all(|b| b.is_ascii_digit())
            && suffix
                .parse::<u64>()
                .is_ok_and(|n| n > 0 && n <= self.count)
    }

    fn call(&mut self, action: &str) -> Action {
        self.count += 1;
        let id = format!("{}{}", self.prefix, self.count);
        self.current = Some(id.clone());
        let mut params = json!({"name": "memory_toolset", "arguments": {"action": action}});
        // The held request's metadata carries what a 2026-07-28 session
        // needs on every request (its protocol version and client).
        if let Some(meta) = &self.meta {
            params["_meta"] = meta.clone();
        }
        let request = json!({"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": params});
        Action::ToShim(format!("{request}\n").into_bytes())
    }

    fn refusal(id: Value) -> Action {
        let error = json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32603, "message": DISCOVERY_FAILED}});
        Action::ToClient(format!("{error}\n").into_bytes())
    }

    /// A line from the client.
    fn inbound(&mut self, line: Vec<u8>, message: Option<&Map<String, Value>>) -> Vec<Action> {
        if !self.enabled {
            return vec![Action::ToShim(line)];
        }
        let id = message
            .and_then(|message| message.get("id"))
            .cloned()
            .unwrap_or(Value::Null);
        // The gate's own ids are not the client's to use.
        if id.as_str().is_some_and(|id| id.starts_with(&self.prefix)) {
            return vec![Self::refusal(id)];
        }
        if message
            .and_then(|message| message.get("method"))
            .and_then(Value::as_str)
            != Some("tools/list")
        {
            return vec![Action::ToShim(line)];
        }
        if self.held.len() >= HELD_LIMIT {
            return vec![Self::refusal(id)];
        }
        self.held.push((line, id));
        if self.waiting() {
            return Vec::new();
        }
        self.deadline = Some(tokio::time::Instant::now() + GATE_TIMEOUT);
        self.expansions = 0;
        self.meta = message
            .and_then(|message| message.get("params"))
            .and_then(|params| params.get("_meta"))
            .and_then(Value::as_object)
            .map(|meta| {
                let mut meta = meta.clone();
                meta.remove("progressToken");
                Value::Object(meta)
            });
        vec![self.call("status")]
    }

    /// A line from the shim.
    fn outbound(&mut self, line: Vec<u8>, message: Option<&Map<String, Value>>) -> Vec<Action> {
        if !self.enabled {
            return vec![Action::ToClient(line)];
        }
        let Some(message) = message else {
            return vec![Action::ToClient(line)];
        };
        let id = message.get("id").cloned().unwrap_or(Value::Null);
        if message.contains_key("method") || !self.internal(&id) {
            return vec![Action::ToClient(line)];
        }
        // An answer to an earlier internal call, or after a timeout.
        if self.current.as_deref() != id.as_str() || !self.waiting() {
            return Vec::new();
        }
        match tier(message).as_deref() {
            Some("full") => {
                self.finish();
                self.held
                    .drain(..)
                    .map(|(line, _)| Action::ToShim(line))
                    .collect()
            }
            Some("minimal" | "core") if self.expansions < 2 => {
                self.expansions += 1;
                vec![self.call("expand")]
            }
            _ => self.fail(),
        }
    }

    /// The held requests waited too long.
    fn expire(&mut self) -> Vec<Action> {
        if self.waiting() {
            self.fail()
        } else {
            Vec::new()
        }
    }

    fn finish(&mut self) {
        self.deadline = None;
        self.current = None;
    }

    fn fail(&mut self) -> Vec<Action> {
        self.finish();
        self.held
            .drain(..)
            .map(|(_, id)| Self::refusal(id))
            .collect()
    }
}

/// The tier a successful `memory_toolset` answer reports.
fn tier(message: &Map<String, Value>) -> Option<String> {
    if !succeeded(message) {
        return None;
    }
    let result = message.get("result")?;
    let structured = result.get("structuredContent").and_then(Value::as_object);
    let payload = structured
        .map(|map| {
            map.get("result")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_else(|| map.clone())
        })
        .or_else(|| {
            result
                .get("content")?
                .as_array()?
                .iter()
                .filter(|block| block.get("type").and_then(Value::as_str) == Some("text"))
                .filter_map(|block| {
                    block
                        .get("text")
                        .and_then(Value::as_str)
                        .and_then(message_text)
                })
                .find(|payload| payload.contains_key("current"))
        })?;
    payload.get("current")?.as_str().map(str::to_owned)
}

#[cfg(test)]
#[path = "bridge_tests.rs"]
mod tests;
