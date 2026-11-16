//! The bridge over in-memory streams: a scripted fake shim stands in for
//! the stdio shim, so nothing is spawned and no socket or daemon is used.
use super::*;

use std::collections::HashMap;
use std::sync::Mutex;
use tokio::io::{AsyncReadExt, DuplexStream, duplex};

/// A nonce made when the tests run, in the shape a saved one has (32
/// lowercase hex digits), so no test carries a fixed nonce.
struct RunNonce(std::sync::LazyLock<String>);
impl std::ops::Deref for RunNonce {
    type Target = str;
    fn deref(&self) -> &str {
        &self.0
    }
}
impl std::fmt::Display for RunNonce {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self)
    }
}
static NONCE: RunNonce = RunNonce(std::sync::LazyLock::new(|| {
    uuid::Uuid::new_v4().simple().to_string()
}));
static OTHER_NONCE: RunNonce = RunNonce(std::sync::LazyLock::new(|| {
    uuid::Uuid::new_v4().simple().to_string()
}));
const IDENTITY: &str = "aa00aa00aa00aa00aa00aa00aa00aa00aa00aa00aa00aa00aa00aa00aa00aa00";

/// A disposable owner-only store, written as the native client writes
/// private files.
struct Store(PathBuf);
impl Store {
    fn new() -> Self {
        let base = std::env::temp_dir().join(format!("tunnel-bridge-{}", uuid::Uuid::new_v4()));
        let root = base.join("tunnel");
        crate::board::state::private_dir(&root).unwrap();
        Self(root)
    }
    fn put(&self, rel: &str, data: &[u8]) {
        crate::board::state::atomic_create(&self.0.join(rel), data).unwrap();
    }
    fn profile(&self, name: &str) {
        let token = serde_json::to_string(&self.0.join("token")).unwrap();
        self.put(
            &format!("{name}.profile.json"),
            format!("{{\"name\": \"{name}\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {token}, \"tunnel_id\": \"tunnel_0123\", \"state\": \"ready\", \"consent\": true}}").as_bytes(),
        );
    }
    fn challenge(&self, name: &str, nonce: &str, created: f64) {
        crate::board::state::private_dir(&self.0.join(format!("{name}-verification"))).unwrap();
        let challenge = json!({"nonce": nonce, "identity": IDENTITY, "created": created});
        self.put(
            &format!("{name}-verification/challenge.json"),
            challenge.to_string().as_bytes(),
        );
    }
    fn load(&self, name: &str) -> store::Profile {
        store::load(&self.0, name).unwrap()
    }
    /// The files in a profile's verification folder besides the challenge.
    fn receipts(&self, name: &str) -> Vec<String> {
        let mut names: Vec<String> = std::fs::read_dir(self.0.join(format!("{name}-verification")))
            .map(|entries| {
                entries
                    .flatten()
                    .map(|entry| entry.file_name().to_string_lossy().into_owned())
                    .filter(|name| name != "challenge.json")
                    .collect()
            })
            .unwrap_or_default();
        names.sort();
        names
    }
    fn receipt(&self, name: &str, tool: &str) -> Value {
        let path = self
            .0
            .join(format!("{name}-verification"))
            .join(format!("{NONCE}-{tool}.json"));
        serde_json::from_slice(&store::private_read(&path, 65536).unwrap()).unwrap()
    }
}
impl Drop for Store {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(self.0.parent().unwrap());
    }
}

/// A reporter that keeps its lines instead of printing them.
fn counting_reporter() -> Reporter {
    recording_reporter().0
}
fn recording_reporter() -> (Reporter, Arc<Mutex<Vec<String>>>) {
    let said = Arc::new(Mutex::new(Vec::new()));
    let kept = said.clone();
    (
        Reporter::new(Arc::new(move |line: &str| {
            kept.lock().unwrap().push(line.to_owned())
        })),
        said,
    )
}

/// A sink that records each receipt instead of writing it.
#[derive(Clone, Default)]
struct Recorder(Arc<Mutex<Vec<(PathBuf, Value)>>>);
impl Recorder {
    fn sink(&self) -> Sink {
        let written = self.0.clone();
        Arc::new(move |path, bytes| {
            written
                .lock()
                .unwrap()
                .push((path.to_owned(), serde_json::from_slice(bytes).unwrap()));
            Ok(())
        })
    }
    fn written(&self) -> Vec<(PathBuf, Value)> {
        self.0.lock().unwrap().clone()
    }
}

fn observer(store: &Store, name: &str, sink: Sink) -> Observer {
    Observer::new(
        store.0.clone(),
        &store.load(name),
        counting_reporter(),
        sink,
    )
}

/// What a fake shim answers to one request it reads.
type Script = Box<dyn FnMut(&Map<String, Value>) -> Vec<String> + Send>;

/// Run one session: the client's bytes in, then its input closes; returns
/// what the client received and what the fake shim received.
async fn session(
    client_bytes: &[u8],
    observer: Option<Observer>,
    gate: Gate,
    mut script: Script,
) -> (Vec<u8>, Vec<u8>) {
    // One pipe per direction, as stdin and stdout are: dropping a writer
    // ends its reader's stream (a split half's drop would not).
    let (mut client_writer, client_in) = duplex(1 << 20);
    let (client_out, mut client_reader) = duplex(1 << 20);
    let (shim_in, fake_reader) = duplex(1 << 20);
    let (mut fake_writer, shim_out) = duplex(1 << 20);
    let fake = tokio::spawn(async move {
        let mut reader = BufReader::new(fake_reader);
        let writer = &mut fake_writer;
        let mut received = Vec::new();
        let mut line = Vec::new();
        while reader.read_until(b'\n', &mut line).await.unwrap() > 0 {
            received.extend_from_slice(&line);
            let unmarked = line.strip_prefix(b"\xEF\xBB\xBF").unwrap_or(&line);
            if let Ok(Value::Object(message)) = serde_json::from_slice(unmarked) {
                for answer in script(&message) {
                    writer.write_all(answer.as_bytes()).await.unwrap();
                }
            }
            line.clear();
        }
        drop(fake_writer);
        received
    });
    let relayed = tokio::spawn(relay(
        client_in, client_out, shim_in, shim_out, observer, gate,
    ));
    // Written while the relay runs: input larger than a pipe's buffer must
    // not wait for a relay that has not started.
    let client_bytes = client_bytes.to_vec();
    tokio::spawn(async move {
        client_writer.write_all(&client_bytes).await.unwrap();
    });
    // A relay that never ends fails the test instead of hanging it.
    tokio::time::timeout(Duration::from_secs(30), async move {
        let mut out = Vec::new();
        client_reader.read_to_end(&mut out).await.unwrap();
        relayed.await.unwrap().unwrap();
        (out, fake.await.unwrap())
    })
    .await
    .expect("the relay ended once both sides closed")
}

fn call(id: Value, tool: &str, arguments: Value) -> String {
    format!(
        "{}\n",
        json!({"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    )
}

fn search(id: u64, query: &str) -> String {
    call(json!(id), "memory_search", json!({"query": query}))
}

fn success(id: &Value) -> String {
    format!(
        "{}\n",
        json!({"jsonrpc": "2.0", "id": id, "result": {"content": [{"type": "text", "text": "{\"count\": 0, \"entries\": []}"}], "structuredContent": {"count": 0, "entries": []}}})
    )
}

/// A shim that answers every request with a successful empty result.
fn succeeding() -> Script {
    Box::new(|message| {
        if message.contains_key("id") {
            vec![success(&message["id"])]
        } else {
            Vec::new()
        }
    })
}

fn answering(result: Value) -> Script {
    Box::new(move |message| {
        vec![format!(
            "{}\n",
            json!({"jsonrpc": "2.0", "id": message["id"], "result": result})
        )]
    })
}

#[tokio::test]
async fn a_successful_nonce_search_writes_a_receipt() {
    let store = Store::new();
    store.profile("dot");
    let created = now() - 5.0;
    store.challenge("dot", &*NONCE, created);
    let request = search(7, &format!("query=\"{NONCE}\""));
    // The store's own owner-only write, read back through its owner-only
    // read: the one relay test that writes a real file.
    let (out, received) = session(
        request.as_bytes(),
        Some(observer(&store, "dot", private_sink())),
        Gate::new(false),
        succeeding(),
    )
    .await;
    assert_eq!(received, request.as_bytes());
    assert_eq!(out, success(&json!(7)).as_bytes());
    assert_eq!(
        store.receipts("dot"),
        [format!("{NONCE}-memory_search.json")]
    );
    let receipt = store.receipt("dot", "memory_search");
    assert_eq!(receipt["identity"], IDENTITY);
    assert_eq!(receipt["nonce"], &*NONCE);
    assert_eq!(receipt["tool"], "memory_search");
    assert_eq!(receipt["success"], true);
    assert!(receipt["observed"].as_f64().unwrap() >= created);
}

#[tokio::test]
async fn a_successful_board_list_with_the_nonce_writes_its_own_receipt() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let request = call(
        json!("a"),
        "memory_agents",
        json!({"action": "list", "project": NONCE}),
    );
    let recorder = Recorder::default();
    session(
        request.as_bytes(),
        Some(observer(&store, "dot", recorder.sink())),
        Gate::new(false),
        succeeding(),
    )
    .await;
    let written = recorder.written();
    assert_eq!(written.len(), 1);
    assert_eq!(
        written[0].0,
        store
            .0
            .join("dot-verification")
            .join(format!("{NONCE}-memory_agents.json"))
    );
    assert_eq!(written[0].1["tool"], "memory_agents");
    assert_eq!(written[0].1["nonce"], &*NONCE);
    assert_eq!(written[0].1["identity"], IDENTITY);
}

/// Each case reaches the shim and gets its answer, and writes nothing.
#[tokio::test]
async fn calls_that_prove_nothing_write_nothing() {
    let failed = json!({"content": [{"type": "text", "text": "boom"}], "isError": true});
    let refused = json!({"content": [{"type": "text", "text": "{\"ok\": false}"}]});
    let unavailable = json!({"content": [{"type": "text", "text": "x"}], "structuredContent": {"available": false}});
    let malformed = json!({"content": "not a list"});
    let cases: Vec<(&str, String, Script)> = vec![
        ("isError", search(1, &*NONCE), answering(failed)),
        ("ok false", search(1, &*NONCE), answering(refused)),
        (
            "available false",
            search(1, &*NONCE),
            answering(unavailable),
        ),
        ("malformed result", search(1, &*NONCE), answering(malformed)),
        (
            "JSON-RPC error",
            search(1, &*NONCE),
            Box::new(|m: &Map<String, Value>| {
                vec![format!(
                    "{}\n",
                    json!({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603, "message": "x"}})
                )]
            }),
        ),
        ("no nonce", search(1, "what did we decide"), succeeding()),
        ("wrong nonce", search(1, &*OTHER_NONCE), succeeding()),
        (
            "nonce in another argument",
            call(
                json!(1),
                "memory_search",
                json!({"query": "x", "tags": [&*NONCE]}),
            ),
            succeeding(),
        ),
        (
            "another tool",
            call(json!(1), "memory_store", json!({"text": NONCE})),
            succeeding(),
        ),
        (
            "board call that is not a list",
            call(
                json!(1),
                "memory_agents",
                json!({"action": "update", "project": NONCE}),
            ),
            succeeding(),
        ),
        (
            "answer under another id",
            search(1, &*NONCE),
            Box::new(|_: &Map<String, Value>| vec![success(&json!(2))]),
        ),
        (
            "id reused before the answer",
            format!("{}{}", search(1, &*NONCE), search(1, "plain")),
            Box::new(|m: &Map<String, Value>| {
                // Both answers come after the second request is read.
                if m["params"]["arguments"]["query"] == "plain" {
                    vec![success(&m["id"]), success(&m["id"])]
                } else {
                    Vec::new()
                }
            }),
        ),
        (
            // The earlier call's success must not vouch for the nonce call.
            "nonce call under an id already in flight",
            format!("{}{}", search(1, "plain"), search(1, &*NONCE)),
            Box::new(|m: &Map<String, Value>| {
                if m["params"]["arguments"]["query"] == &*NONCE {
                    vec![
                        success(&m["id"]),
                        format!(
                            "{}\n",
                            json!({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603, "message": "x"}})
                        ),
                    ]
                } else {
                    Vec::new()
                }
            }),
        ),
    ];
    for (case, request, script) in cases {
        let store = Store::new();
        store.profile("dot");
        store.challenge("dot", &*NONCE, now() - 5.0);
        let recorder = Recorder::default();
        let (_, received) = session(
            request.as_bytes(),
            Some(observer(&store, "dot", recorder.sink())),
            Gate::new(false),
            script,
        )
        .await;
        assert_eq!(received, request.as_bytes(), "{case}");
        assert!(recorder.written().is_empty(), "{case} wrote a receipt");
    }
}

#[tokio::test]
async fn another_profiles_nonce_writes_nothing_anywhere() {
    let store = Store::new();
    store.profile("dot");
    store.profile("lab");
    store.challenge("dot", &*OTHER_NONCE, now() - 5.0);
    store.challenge("lab", &*NONCE, now() - 5.0);
    for request in [
        search(1, &*NONCE),
        call(
            json!(1),
            "memory_agents",
            json!({"action": "list", "project": NONCE}),
        ),
    ] {
        let recorder = Recorder::default();
        session(
            request.as_bytes(),
            Some(observer(&store, "dot", recorder.sink())),
            Gate::new(false),
            succeeding(),
        )
        .await;
        assert!(recorder.written().is_empty(), "{request}");
    }
}

#[tokio::test]
async fn a_challenge_newer_than_the_call_or_a_rebound_profile_writes_nothing() {
    // The challenge was saved after the call was made.
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() + 3600.0);
    let recorder = Recorder::default();
    session(
        search(1, &*NONCE).as_bytes(),
        Some(observer(&store, "dot", recorder.sink())),
        Gate::new(false),
        succeeding(),
    )
    .await;
    assert!(recorder.written().is_empty());

    // The bridge was started for another daemon than the saved profile
    // names now.
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let mut started = store.load("dot");
    started.daemon_url = "http://127.0.0.1:10".into();
    let recorder = Recorder::default();
    session(
        search(1, &*NONCE).as_bytes(),
        Some(Observer::new(
            store.0.clone(),
            &started,
            counting_reporter(),
            recorder.sink(),
        )),
        Gate::new(false),
        succeeding(),
    )
    .await;
    assert!(recorder.written().is_empty());
}

#[tokio::test]
async fn a_receipt_that_cannot_be_saved_is_reported_once_and_the_session_goes_on() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let failing: Sink = Arc::new(|_, _| Err(()));
    let request = format!(
        "{}{}{}",
        search(1, &*NONCE),
        call(
            json!(2),
            "memory_agents",
            json!({"action": "list", "project": NONCE})
        ),
        search(3, "after")
    );
    let (reporter, said) = recording_reporter();
    let (out, received) = session(
        request.as_bytes(),
        Some(Observer::new(
            store.0.clone(),
            &store.load("dot"),
            reporter,
            failing,
        )),
        Gate::new(false),
        succeeding(),
    )
    .await;
    assert_eq!(received, request.as_bytes());
    let expected = format!(
        "{}{}{}",
        success(&json!(1)),
        success(&json!(2)),
        success(&json!(3))
    );
    assert_eq!(out, expected.as_bytes());
    assert_eq!(*said.lock().unwrap(), [RECEIPT_FAILED]);
}

/// Every line, whatever it is, crosses unchanged, with or without the
/// observer, while the observer writes its receipt.
#[tokio::test]
async fn traffic_is_byte_identical_with_and_without_the_observer() {
    let request = format!(
        "{}{}{}{}{}{}",
        "{\"jsonrpc\":\"2.0\",\"id\":0,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"t\",\"version\":\"1\"}}}\r\n",
        "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n",
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}\n",
        search(2, &*NONCE),
        "\r\n",
        // A last line without its newline.
        "{\"jsonrpc\": \"2.0\", \"id\": \"tail\", \"method\": \"ping\"}",
    );
    let shim_lines = |m: &Map<String, Value>| -> Vec<String> {
        match m.get("id") {
            Some(id) => vec![
                format!(
                    "{{\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\",\"params\":{{\"level\":\"info\",\"data\":{id}}}}}\n"
                ),
                success(id),
            ],
            None => Vec::new(),
        }
    };
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let recorder = Recorder::default();
    let observed = session(
        request.as_bytes(),
        Some(observer(&store, "dot", recorder.sink())),
        Gate::new(false),
        Box::new(shim_lines),
    )
    .await;
    let plain = session(
        request.as_bytes(),
        None,
        Gate::new(false),
        Box::new(shim_lines),
    )
    .await;
    assert_eq!(observed, plain);
    assert_eq!(observed.1, request.as_bytes(), "the shim saw other bytes");
    assert_eq!(recorder.written().len(), 1, "the observer wrote no receipt");
}

/// A last line without its newline still crosses when an earlier read of
/// it was cut short by the shim's side of the relay.
#[tokio::test]
async fn an_unended_last_line_crosses_after_an_interrupted_read() {
    let (mut client_writer, client_in) = duplex(1 << 16);
    let read = Arc::new(tokio::sync::Notify::new());
    let client_in = Telling {
        inner: client_in,
        read: read.clone(),
    };
    let (client_out, mut client_reader) = duplex(1 << 16);
    let (shim_in, mut fake_reader) = duplex(1 << 16);
    let (mut fake_writer, shim_out) = duplex(1 << 16);
    let relayed = tokio::spawn(relay(
        client_in,
        client_out,
        shim_in,
        shim_out,
        None,
        Gate::new(false),
    ));
    let tail = b"{\"jsonrpc\":\"2.0\",\"id\":\"tail\",\"method\":\"ping\"}";
    client_writer.write_all(tail).await.unwrap();
    // The relay has read the tail and waits for its newline (this task
    // runs again only once the relay's task is waiting)...
    read.notified().await;
    // ...until a shim line wins the round, cutting that read short.
    let note = b"{\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\"}\n";
    fake_writer.write_all(note).await.unwrap();
    let mut relayed_note = vec![0; note.len()];
    client_reader.read_exact(&mut relayed_note).await.unwrap();
    assert_eq!(relayed_note, note);
    drop(client_writer);
    let mut received = Vec::new();
    tokio::time::timeout(
        Duration::from_secs(30),
        fake_reader.read_to_end(&mut received),
    )
    .await
    .expect("the shim's input closed")
    .unwrap();
    assert_eq!(received, tail);
    drop(fake_writer);
    relayed.await.unwrap().unwrap();
}

/// A reader that says when it has handed out bytes.
struct Telling<R> {
    inner: R,
    read: Arc<tokio::sync::Notify>,
}
impl<R: AsyncRead + Unpin> AsyncRead for Telling<R> {
    fn poll_read(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
        buffer: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        let before = buffer.filled().len();
        let polled = std::pin::Pin::new(&mut self.inner).poll_read(context, buffer);
        if matches!(polled, std::task::Poll::Ready(Ok(()))) && buffer.filled().len() > before {
            self.read.notify_one();
        }
        polled
    }
}

/// A shim that writes before it reads, through pipes far smaller than the
/// traffic, still gets every line both ways: reading never waits for a
/// write in the other direction.
#[tokio::test]
async fn a_shim_that_is_not_reading_never_stalls_the_relay() {
    const LINES: usize = 200;
    let (mut client_writer, client_in) = duplex(256);
    let (client_out, mut client_reader) = duplex(256);
    let (shim_in, fake_reader) = duplex(256);
    let (mut fake_writer, shim_out) = duplex(256);
    let request = |n: usize| {
        format!(
            "{{\"jsonrpc\":\"2.0\",\"id\":{n},\"method\":\"ping\",\"params\":{{\"pad\":\"{}\"}}}}\n",
            "x".repeat(80)
        )
    };
    let note = |n: usize| {
        format!(
            "{{\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\",\"params\":{{\"n\":{n},\"pad\":\"{}\"}}}}\n",
            "y".repeat(80)
        )
    };
    let sent: String = (0..LINES).map(request).collect();
    let noted: String = (0..LINES).map(note).collect();
    let relayed = tokio::spawn(relay(
        client_in,
        client_out,
        shim_in,
        shim_out,
        None,
        Gate::new(false),
    ));
    let client = {
        let sent = sent.clone();
        tokio::spawn(async move {
            client_writer.write_all(sent.as_bytes()).await.unwrap();
        })
    };
    let fake = {
        let noted = noted.clone();
        tokio::spawn(async move {
            // Everything it has to say, before it reads a byte.
            fake_writer.write_all(noted.as_bytes()).await.unwrap();
            let mut received = Vec::new();
            let mut reader = fake_reader;
            reader.read_to_end(&mut received).await.unwrap();
            received
        })
    };
    let reading = tokio::spawn(async move {
        let mut out = Vec::new();
        client_reader.read_to_end(&mut out).await.unwrap();
        out
    });
    let outcome = tokio::time::timeout(Duration::from_secs(30), async {
        client.await.unwrap();
        let received = fake.await.unwrap();
        relayed.await.unwrap().unwrap();
        (reading.await.unwrap(), received)
    })
    .await
    .expect("the relay stalled");
    assert_eq!(outcome.0, noted.as_bytes());
    assert_eq!(outcome.1, sent.as_bytes());
}

/// Pipes for a hand-driven session: the client's two ends, the fake
/// shim's two ends, and the running relay.
struct Wired {
    client_writer: DuplexStream,
    client_reader: DuplexStream,
    fake_reader: DuplexStream,
    fake_writer: DuplexStream,
    relayed: tokio::task::JoinHandle<std::io::Result<bool>>,
}
fn wired(size: usize, observer: Option<Observer>, gate: Gate) -> Wired {
    let (client_writer, client_in) = duplex(size);
    let (client_out, client_reader) = duplex(size);
    let (shim_in, fake_reader) = duplex(size);
    let (fake_writer, shim_out) = duplex(size);
    Wired {
        client_writer,
        client_reader,
        fake_reader,
        fake_writer,
        relayed: tokio::spawn(relay(
            client_in, client_out, shim_in, shim_out, observer, gate,
        )),
    }
}

async fn line_of(reader: &mut DuplexStream) -> Value {
    let mut line = Vec::new();
    let mut byte = [0u8; 1];
    while reader.read_exact(&mut byte).await.is_ok() && byte[0] != b'\n' {
        line.push(byte[0]);
    }
    serde_json::from_slice(&line).unwrap()
}

/// A listing the gate refused is answered: its id may be used again, and
/// a verification call under it is credited.
#[tokio::test]
async fn an_id_the_gate_answered_may_carry_a_verification_call() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let recorder = Recorder::default();
    let mut w = wired(
        1 << 16,
        Some(observer(&store, "dot", recorder.sink())),
        Gate::new(true),
    );
    w.client_writer
        .write_all(b"{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"tools/list\"}\n")
        .await
        .unwrap();
    // The gate's catalog check fails: the listing is refused.
    let check = line_of(&mut w.fake_reader).await;
    assert_eq!(check["params"]["name"], "memory_toolset");
    let failed =
        json!({"jsonrpc": "2.0", "id": check["id"], "error": {"code": -32601, "message": "no"}});
    w.fake_writer
        .write_all(format!("{failed}\n").as_bytes())
        .await
        .unwrap();
    let refusal = line_of(&mut w.client_reader).await;
    assert_eq!(refusal["id"], 5);
    assert_eq!(refusal["error"]["message"], DISCOVERY_FAILED);
    // The client uses id 5 again, for the verification search.
    w.client_writer
        .write_all(search(5, &*NONCE).as_bytes())
        .await
        .unwrap();
    let forwarded = line_of(&mut w.fake_reader).await;
    assert_eq!(forwarded["params"]["arguments"]["query"], &*NONCE);
    w.fake_writer
        .write_all(success(&json!(5)).as_bytes())
        .await
        .unwrap();
    assert_eq!(line_of(&mut w.client_reader).await["id"], 5);
    drop(w.client_writer);
    drop(w.fake_writer);
    tokio::time::timeout(Duration::from_secs(30), w.relayed)
        .await
        .expect("the relay ended")
        .unwrap()
        .unwrap();
    assert_eq!(recorder.written().len(), 1, "no receipt for the reused id");
}

/// A client that stops reading while its input stays open ends the
/// session: the shim's input closes and the relay returns.
#[tokio::test]
async fn a_client_that_stops_reading_ends_the_session() {
    let mut w = wired(1 << 16, None, Gate::new(false));
    drop(w.client_reader);
    w.client_writer
        .write_all(search(1, "x").as_bytes())
        .await
        .unwrap();
    line_of(&mut w.fake_reader).await;
    w.fake_writer
        .write_all(success(&json!(1)).as_bytes())
        .await
        .unwrap();
    // The answer cannot be written: the shim's input closes.
    let mut rest = Vec::new();
    tokio::time::timeout(
        Duration::from_secs(30),
        w.fake_reader.read_to_end(&mut rest),
    )
    .await
    .expect("the shim's input closed")
    .unwrap();
    drop(w.fake_writer);
    tokio::time::timeout(Duration::from_secs(30), w.relayed)
        .await
        .expect("the relay ended")
        .unwrap()
        .unwrap();
    drop(w.client_writer);
}

/// A shim slow to take its input, once the client has finished sending,
/// still answers every request: its time to exit starts only once its
/// input has been written and closed.
#[tokio::test(start_paused = true)]
async fn a_slow_shim_answers_everything_queued_before_the_client_left() {
    const LINES: usize = 40;
    let mut w = wired(256, None, Gate::new(false));
    let sent: String = (0..LINES).map(|n| search(n as u64, "x")).collect();
    let writing = {
        let mut writer = w.client_writer;
        let sent = sent.clone();
        tokio::spawn(async move { writer.write_all(sent.as_bytes()).await.unwrap() })
    };
    let fake = tokio::spawn(async move {
        // Busy for longer than the exit allowance before it reads.
        tokio::time::sleep(CHILD_EXIT * 3).await;
        let mut reader = BufReader::new(w.fake_reader);
        let mut line = Vec::new();
        while reader.read_until(b'\n', &mut line).await.unwrap() > 0 {
            let message = message(&line).unwrap();
            w.fake_writer
                .write_all(success(&message["id"]).as_bytes())
                .await
                .unwrap();
            line.clear();
        }
    });
    let mut out = Vec::new();
    w.client_reader.read_to_end(&mut out).await.unwrap();
    writing.await.unwrap();
    fake.await.unwrap();
    w.relayed.await.unwrap().unwrap();
    let expected: String = (0..LINES).map(|n| success(&json!(n))).collect();
    assert_eq!(out, expected.as_bytes());
}

/// A client that closes its input but never reads its output again does
/// not keep the bridge alive: the relay ends within CLIENT_GONE.
#[tokio::test(start_paused = true)]
async fn a_client_that_left_without_reading_is_given_up_on() {
    let Wired {
        client_writer,
        client_reader,
        fake_reader,
        mut fake_writer,
        relayed,
    } = wired(256, None, Gate::new(false));
    let fake = tokio::spawn(async move {
        let note = "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\",\"params\":{\"pad\":\"zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz\"}}\n";
        for _ in 0..1000 {
            if fake_writer.write_all(note.as_bytes()).await.is_err() {
                break;
            }
        }
        drop(fake_reader);
    });
    drop(client_writer);
    let ended = tokio::time::timeout(CLIENT_GONE * 3, relayed).await;
    assert!(ended.is_ok(), "the relay outlived a client that left");
    fake.abort();
    drop(client_reader);
}

/// A shim that never takes its input, with requests waiting for it, is
/// treated as hung: the relay ends within SHIM_STALL.
#[tokio::test(start_paused = true)]
async fn a_shim_that_takes_no_input_is_given_up_on() {
    let Wired {
        mut client_writer,
        client_reader,
        fake_reader,
        fake_writer,
        relayed,
    } = wired(256, None, Gate::new(false));
    let client = tokio::spawn(async move {
        for n in 0..1000 {
            if client_writer
                .write_all(search(n, "x").as_bytes())
                .await
                .is_err()
            {
                break;
            }
        }
    });
    let ended = tokio::time::timeout(SHIM_STALL * 3, relayed).await;
    assert!(
        ended.is_ok(),
        "the relay waited for a hung shim without end"
    );
    client.abort();
    drop((client_reader, fake_reader, fake_writer));
}

/// A success answer the remote side writes on stdin is relayed to the shim
/// like any line; it never reaches the observer, so it proves nothing.
#[tokio::test]
async fn a_success_forged_on_stdin_proves_nothing() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let recorder = Recorder::default();
    let request = format!("{}{}", search(1, &*NONCE), success(&json!(1)));
    let (_, received) = session(
        request.as_bytes(),
        Some(observer(&store, "dot", recorder.sink())),
        Gate::new(false),
        Box::new(|m: &Map<String, Value>| {
            // The shim refuses the search itself.
            if m.contains_key("method") {
                vec![format!("{}\n", json!({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603, "message": "x"}}))]
            } else {
                Vec::new()
            }
        }),
    )
    .await;
    assert_eq!(received, request.as_bytes());
    assert!(recorder.written().is_empty());
}

/// An answer that cannot be written to the client writes no receipt.
#[tokio::test]
async fn an_answer_the_client_never_got_proves_nothing() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let recorder = Recorder::default();
    let mut w = wired(
        1 << 16,
        Some(observer(&store, "dot", recorder.sink())),
        Gate::new(false),
    );
    drop(w.client_reader);
    w.client_writer
        .write_all(search(1, &*NONCE).as_bytes())
        .await
        .unwrap();
    line_of(&mut w.fake_reader).await;
    w.fake_writer
        .write_all(success(&json!(1)).as_bytes())
        .await
        .unwrap();
    let mut rest = Vec::new();
    tokio::time::timeout(
        Duration::from_secs(30),
        w.fake_reader.read_to_end(&mut rest),
    )
    .await
    .expect("the shim's input closed")
    .unwrap();
    drop(w.fake_writer);
    tokio::time::timeout(Duration::from_secs(30), w.relayed)
        .await
        .expect("the relay ended")
        .unwrap()
        .unwrap();
    drop(w.client_writer);
    assert!(recorder.written().is_empty());
}

/// A request padded past PARSE_LIMIT, which the shim would compact and
/// answer, ends the session before it is relayed: it can never vouch for a
/// nonce call under its id.
#[tokio::test]
async fn a_padded_request_past_the_parse_limit_ends_the_session() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let recorder = Recorder::default();
    let padded = format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":7,{}\"method\":\"tools/call\",\"params\":{{\"name\":\"memory_search\",\"arguments\":{{\"query\":\"plain\"}}}}}}\n",
        " ".repeat(PARSE_LIMIT + 1024)
    );
    let mut w = wired(
        1 << 20,
        Some(observer(&store, "dot", recorder.sink())),
        Gate::new(false),
    );
    let writing = tokio::spawn(async move {
        w.client_writer
            .write_all(search(7, &*NONCE).as_bytes())
            .await
            .unwrap();
        let _ = w.client_writer.write_all(padded.as_bytes()).await;
        w.client_writer
    });
    let fake = tokio::spawn(async move {
        // The shim answers whatever reaches it, as a success under id 7.
        let mut reader = BufReader::new(w.fake_reader);
        let mut line = Vec::new();
        while reader.read_until(b'\n', &mut line).await.unwrap_or(0) > 0 {
            if line.windows(5).any(|window| window == b"plain") {
                let _ = w.fake_writer.write_all(success(&json!(7)).as_bytes()).await;
            }
            line.clear();
        }
    });
    let ended = tokio::time::timeout(Duration::from_secs(60), w.relayed)
        .await
        .expect("the relay ended")
        .unwrap();
    assert_eq!(ended.unwrap_err().kind(), std::io::ErrorKind::InvalidData);
    drop(writing.await.unwrap());
    let _ = fake.await;
    drop(w.client_reader);
    assert!(recorder.written().is_empty());
}

/// A request this bridge cannot parse but the shim can (a byte order mark
/// before it, which the SDK strips) switches the observer off for the
/// session: its answer can never vouch for a nonce call under its id.
#[tokio::test]
async fn a_request_the_bridge_cannot_read_switches_receipts_off() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let recorder = Recorder::default();
    let (reporter, said) = recording_reporter();
    let request = format!("{}\u{feff}{}", search(7, &*NONCE), search(7, "plain"));
    let (_, received) = session(
        request.as_bytes(),
        Some(Observer::new(
            store.0.clone(),
            &store.load("dot"),
            reporter,
            recorder.sink(),
        )),
        Gate::new(false),
        Box::new(|m: &Map<String, Value>| {
            // Only the marked plain search is answered, with a success.
            if m["params"]["arguments"]["query"] == "plain" {
                vec![success(&m["id"])]
            } else {
                Vec::new()
            }
        }),
    )
    .await;
    assert_eq!(received, request.as_bytes());
    assert!(recorder.written().is_empty());
    assert_eq!(*said.lock().unwrap(), [UNPARSED]);
}

/// Two requests under one id, one of them answered: the other is still
/// outstanding, so a nonce call sent under that id now is never credited
/// by the late answer.
#[tokio::test]
async fn a_duplicate_id_answered_once_still_blocks_its_reuse() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let recorder = Recorder::default();
    let mut w = wired(
        1 << 16,
        Some(observer(&store, "dot", recorder.sink())),
        Gate::new(false),
    );
    // A slow plain search, then a ping, both under id 7.
    let ping = "{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"ping\"}\n";
    w.client_writer
        .write_all(format!("{}{ping}", search(7, "plain")).as_bytes())
        .await
        .unwrap();
    line_of(&mut w.fake_reader).await;
    line_of(&mut w.fake_reader).await;
    // The ping is answered first; the client has its answer.
    w.fake_writer
        .write_all(success(&json!(7)).as_bytes())
        .await
        .unwrap();
    assert_eq!(line_of(&mut w.client_reader).await["id"], 7);
    // The nonce call under id 7, then the slow search's success.
    w.client_writer
        .write_all(search(7, &*NONCE).as_bytes())
        .await
        .unwrap();
    line_of(&mut w.fake_reader).await;
    w.fake_writer
        .write_all(success(&json!(7)).as_bytes())
        .await
        .unwrap();
    assert_eq!(line_of(&mut w.client_reader).await["id"], 7);
    drop(w.client_writer);
    drop(w.fake_writer);
    tokio::time::timeout(Duration::from_secs(30), w.relayed)
        .await
        .expect("the relay ended")
        .unwrap()
        .unwrap();
    assert!(
        recorder.written().is_empty(),
        "a late answer vouched for the nonce call"
    );
}

/// A line past LINE_LIMIT ends the session, from either side, before it is
/// relayed or held whole.
#[tokio::test]
async fn a_line_past_the_limit_ends_the_session() {
    // From the client, with no newline ever.
    let mut w = wired(1 << 16, None, Gate::new(false));
    let writing = tokio::spawn(async move {
        let chunk = vec![b'x'; 1 << 16];
        for _ in 0..(LINE_LIMIT / chunk.len() + 2) {
            if w.client_writer.write_all(&chunk).await.is_err() {
                break;
            }
        }
        w.client_writer
    });
    let ended = tokio::time::timeout(Duration::from_secs(60), w.relayed)
        .await
        .expect("the relay ended")
        .unwrap();
    assert_eq!(ended.unwrap_err().kind(), std::io::ErrorKind::InvalidData);
    let mut received = Vec::new();
    w.fake_reader.read_to_end(&mut received).await.unwrap();
    assert!(received.is_empty(), "part of the line was relayed");
    drop(writing.await.unwrap());
    // From the shim.
    let mut w = wired(1 << 16, None, Gate::new(false));
    let writing = tokio::spawn(async move {
        let chunk = vec![b'y'; 1 << 16];
        for _ in 0..(LINE_LIMIT / chunk.len() + 2) {
            if w.fake_writer.write_all(&chunk).await.is_err() {
                break;
            }
        }
        w.fake_writer
    });
    let ended = tokio::time::timeout(Duration::from_secs(60), w.relayed)
        .await
        .expect("the relay ended")
        .unwrap();
    assert_eq!(ended.unwrap_err().kind(), std::io::ErrorKind::InvalidData);
    let mut out = Vec::new();
    w.client_reader.read_to_end(&mut out).await.unwrap();
    assert!(out.is_empty(), "part of the line was relayed");
    drop(writing.await.unwrap());
    drop(w.client_writer);
}

fn as_map(line: &str) -> Map<String, Value> {
    message(line.as_bytes()).unwrap()
}

/// Past IN_FLIGHT_LIMIT unanswered requests the observer stops crediting
/// for good: an id still unanswered from before can then never vouch for a
/// nonce call sent under it.
#[test]
fn crossing_the_in_flight_limit_fails_closed() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let (reporter, said) = recording_reporter();
    let mut observer = Observer::new(
        store.0.clone(),
        &store.load("dot"),
        reporter,
        Recorder::default().sink(),
    );
    observer.request(&as_map(&search(7, "plain")));
    for n in 0..IN_FLIGHT_LIMIT as u64 {
        observer.request(&as_map(&search(1000 + n, "plain")));
    }
    observer.request(&as_map(&search(7, &*NONCE)));
    assert!(observer.response(&as_map(&success(&json!(7)))).is_none());
    // Nor any later call, however fresh its id.
    observer.request(&as_map(&search(99_999, &*NONCE)));
    assert!(
        observer
            .response(&as_map(&success(&json!(99_999))))
            .is_none()
    );
    assert_eq!(*said.lock().unwrap(), [SATURATED]);
}

/// What the observer keeps is bounded: a long id is never tracked and a
/// long argument is never recorded.
#[test]
fn long_ids_and_long_arguments_are_never_credited() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let mut observer = observer(&store, "dot", Recorder::default().sink());
    let long_id = json!("i".repeat(ID_LIMIT + 1));
    observer.request(&as_map(&call(
        long_id.clone(),
        "memory_search",
        json!({"query": NONCE}),
    )));
    assert!(observer.response(&as_map(&success(&long_id))).is_none());
    let long_query = format!("{NONCE}{}", " ".repeat(SUPPLIED_LIMIT));
    observer.request(&as_map(&search(1, &long_query)));
    assert!(observer.response(&as_map(&success(&json!(1)))).is_none());
    // The same calls within the bounds are credited.
    observer.request(&as_map(&call(
        json!("i"),
        "memory_search",
        json!({"query": NONCE}),
    )));
    assert!(observer.response(&as_map(&success(&json!("i")))).is_some());
}

/// A profile whose tunnel or organization changed since the bridge started,
/// or that is no longer ready, writes nothing.
#[tokio::test]
async fn a_profile_changed_in_any_bound_field_writes_nothing() {
    for change in ["tunnel_id", "organization_id", "token_file", "not ready"] {
        let store = Store::new();
        store.profile("dot");
        store.challenge("dot", &*NONCE, now() - 5.0);
        let mut started = store.load("dot");
        match change {
            "tunnel_id" => started.tunnel_id = Some("tunnel_ffff".into()),
            "organization_id" => started.organization_id = Some("org-other".into()),
            "token_file" => started.token_file = "/elsewhere/token".into(),
            _ => {
                // The saved profile is pending now, every bound field the
                // same: only its readiness changed.
                std::fs::remove_file(store.0.join("dot.profile.json")).unwrap();
                let token = serde_json::to_string(&store.0.join("token")).unwrap();
                store.put(
                    "dot.profile.json",
                    format!("{{\"name\": \"dot\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {token}, \"tunnel_id\": \"tunnel_0123\", \"state\": \"pending\", \"consent\": true}}").as_bytes(),
                );
            }
        }
        let recorder = Recorder::default();
        session(
            search(1, &*NONCE).as_bytes(),
            Some(Observer::new(
                store.0.clone(),
                &started,
                counting_reporter(),
                recorder.sink(),
            )),
            Gate::new(false),
            succeeding(),
        )
        .await;
        assert!(recorder.written().is_empty(), "{change}");
    }
}

/// With the gate on, everything but the held listing crosses unchanged and
/// in order, both ways; the gate's own call and answer never reach the
/// client.
#[tokio::test]
async fn traffic_is_byte_identical_with_the_gate_on() {
    let log = Arc::new(Mutex::new(Vec::new()));
    let lines = [
        "{\"jsonrpc\":\"2.0\",\"id\":0,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"t\",\"version\":\"1\"}}}\r\n",
        "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n",
        "not json at all\n",
        "{\"jsonrpc\": \"2.0\", \"id\": \"p\", \"method\": \"ping\"}\n",
    ];
    let list = "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}\n";
    let sent: String = lines.iter().copied().chain([list]).collect();
    let mut script = toolset("full", log.clone());
    let (out, received) = session(
        sent.as_bytes(),
        None,
        Gate::new(true),
        Box::new(move |m: &Map<String, Value>| {
            let mut answers = script(m);
            if m.get("method").and_then(Value::as_str) == Some("notifications/initialized") {
                answers =
                    vec!["{\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\"}\r\n".into()];
            }
            answers
        }),
    )
    .await;
    // The shim got the client's lines in order, then the gate's check, then
    // the listing it held.
    let received = String::from_utf8(received).unwrap();
    let before: String = lines.concat();
    assert!(received.starts_with(&before), "{received}");
    let rest = &received[before.len()..];
    let (check, held) = rest.split_once('\n').unwrap();
    assert_eq!(
        message(check.as_bytes()).unwrap()["params"]["name"],
        "memory_toolset"
    );
    assert_eq!(held, list);
    // The client got the shim's answers unchanged, the gate's never.
    let answer = |id: Value| {
        format!(
            "{}\n",
            json!({"jsonrpc": "2.0", "id": id, "result": {"tools": [], "tier": "full"}})
        )
    };
    let expected = format!(
        "{}{}{}{}",
        answer(json!(0)),
        "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\"}\r\n",
        answer(json!("p")),
        answer(json!(1)),
    );
    assert_eq!(String::from_utf8(out).unwrap(), expected);
}

/// A shim that stops reading part-way through one request larger than its
/// pipe, while the client waits with its input open, is given up on within
/// SHIM_STALL: the stall bound covers the write itself, not only the queue.
#[tokio::test(start_paused = true)]
async fn a_shim_that_stops_reading_mid_request_is_given_up_on() {
    let Wired {
        mut client_writer,
        client_reader,
        fake_reader,
        fake_writer,
        relayed,
    } = wired(256, None, Gate::new(false));
    client_writer
        .write_all(search(1, &"x".repeat(64 * 1024)).as_bytes())
        .await
        .unwrap();
    let ended = tokio::time::timeout(SHIM_STALL * 3, relayed).await;
    assert!(
        ended.is_ok(),
        "the relay waited on a hung shim's input without end"
    );
    drop((client_writer, client_reader, fake_reader, fake_writer));
}

/// An integer id beyond 64 bits, which the shim preserves, carries a
/// verification call like any other; `-0` and `0` are one id.
#[test]
fn integer_ids_of_any_size_are_tracked() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", &*NONCE, now() - 5.0);
    let mut observer = observer(&store, "dot", Recorder::default().sink());
    let big = "184467440737095516160";
    let request = format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":{big},\"method\":\"tools/call\",\"params\":{{\"name\":\"memory_search\",\"arguments\":{{\"query\":\"{NONCE}\"}}}}}}"
    );
    observer.request(&as_map(&request));
    let answer = format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":{big},\"result\":{{\"content\":[{{\"type\":\"text\",\"text\":\"ok\"}}]}}}}"
    );
    assert!(observer.response(&as_map(&answer)).is_some());
    assert_eq!(
        identifier(&as_map("{\"id\":-0}")),
        identifier(&as_map("{\"id\":0}"))
    );
    assert_eq!(identifier(&as_map("{\"id\":1.0}")), None);
    assert_eq!(identifier(&as_map("{\"id\":1e2}")), None);
}

/// A fake daemon's toolset: answers `memory_toolset` status and expand from
/// `tier`, records the order of what it is asked, and answers the rest.
fn toolset(start: &'static str, log: Arc<Mutex<Vec<String>>>) -> Script {
    let mut tier = start;
    Box::new(move |message| {
        // Value indexing answers null for a missing key (Map indexing panics).
        let message = &Value::Object(message.clone());
        let method = message["method"].as_str().unwrap_or_default().to_owned();
        let id = message["id"].clone();
        if message["params"]["name"] == "memory_toolset" {
            let action = message["params"]["arguments"]["action"]
                .as_str()
                .unwrap()
                .to_owned();
            if action == "expand" {
                tier = match tier {
                    "minimal" => "core",
                    _ => "full",
                };
            }
            log.lock()
                .unwrap()
                .push(format!("toolset {action} -> {tier}"));
            let payload = json!({"current": tier});
            return vec![format!(
                "{}\n",
                json!({"jsonrpc": "2.0", "id": id, "result": {"content": [{"type": "text", "text": payload.to_string()}], "structuredContent": payload}})
            )];
        }
        log.lock().unwrap().push(format!("{method} at {tier}"));
        vec![format!(
            "{}\n",
            json!({"jsonrpc": "2.0", "id": id, "result": {"tools": [], "tier": tier}})
        )]
    })
}

#[tokio::test]
async fn a_full_catalog_is_expanded_before_the_first_tools_list_answer() {
    let log = Arc::new(Mutex::new(Vec::new()));
    let list = "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{\"_meta\":{\"progressToken\":3,\"k\":\"v\"}}}\n";
    let (out, received) = session(
        list.as_bytes(),
        None,
        Gate::new(true),
        toolset("minimal", log.clone()),
    )
    .await;
    assert_eq!(
        *log.lock().unwrap(),
        [
            "toolset status -> minimal",
            "toolset expand -> core",
            "toolset expand -> full",
            "tools/list at full"
        ]
    );
    // The client sees only its own answer, never the gate's calls.
    let answers: Vec<Value> = out
        .split(|b| *b == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice(line).unwrap())
        .collect();
    assert_eq!(answers.len(), 1);
    assert_eq!(answers[0]["id"], 1);
    assert_eq!(answers[0]["result"]["tier"], "full");
    // The held request reaches the shim unchanged; the gate's calls carry
    // its metadata, less the progress token.
    assert!(received.ends_with(list.as_bytes()));
    let first: Value =
        serde_json::from_slice(received.split(|b| *b == b'\n').next().unwrap()).unwrap();
    assert_eq!(first["params"]["_meta"], json!({"k": "v"}));
}

#[tokio::test]
async fn the_current_catalog_is_served_unchanged() {
    let log = Arc::new(Mutex::new(Vec::new()));
    let list = "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}\n";
    let (_, received) = session(
        list.as_bytes(),
        None,
        Gate::new(false),
        toolset("minimal", log.clone()),
    )
    .await;
    assert_eq!(received, list.as_bytes());
    assert_eq!(*log.lock().unwrap(), ["tools/list at minimal"]);
}

#[tokio::test]
async fn a_catalog_that_stays_short_refuses_the_list() {
    let list = "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"tools/list\"}\n";
    let (out, received) = session(
        list.as_bytes(),
        None,
        Gate::new(true),
        Box::new(|m: &Map<String, Value>| {
            vec![format!(
                "{}\n",
                json!({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "no"}})
            )]
        }),
    )
    .await;
    let answer: Value = serde_json::from_slice(&out).unwrap();
    assert_eq!(answer["id"], 9);
    assert_eq!(answer["error"]["message"], DISCOVERY_FAILED);
    assert!(!String::from_utf8_lossy(&received).contains("tools/list"));
}

#[tokio::test(start_paused = true)]
async fn a_catalog_check_that_never_answers_times_out() {
    let list = "{\"jsonrpc\":\"2.0\",\"id\":\"l\",\"method\":\"tools/list\"}\n";
    let (out, _) = session(
        list.as_bytes(),
        None,
        Gate::new(true),
        Box::new(|_: &Map<String, Value>| Vec::new()),
    )
    .await;
    let answer: Value = serde_json::from_slice(&out).unwrap();
    assert_eq!(answer["id"], "l");
    assert_eq!(answer["error"]["message"], DISCOVERY_FAILED);
}

#[test]
fn the_gates_own_ids_are_refused_from_the_client() {
    let mut gate = Gate::new(true);
    let line = format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":\"{}1\",\"method\":\"ping\"}}\n",
        gate.prefix
    );
    let message = message(line.as_bytes());
    let actions = gate.inbound(line.into_bytes(), message.as_ref());
    assert!(matches!(actions.as_slice(), [Action::ToClient(_)]));
}

#[test]
fn the_child_gets_only_its_allowlist_and_the_profiles_connection() {
    let store = Store::new();
    store.profile("dot");
    // What a coding session's shell might carry when it starts the vendor.
    let parent = [
        ("PSEUDOLIFE_MCP_TOKEN", "secret"),
        ("PSEUDOLIFE_MCP_TOKEN_FILE", "/elsewhere/token"),
        ("PSEUDOLIFE_MCP_TOKENS", "a:b"),
        ("pseudolife_mcp_token_file", "/elsewhere/token"),
        ("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1"),
        ("OPENAI_API_KEY", "sk-fixture"),
        ("PSEUDOLIFE_CODEX_SERVER_TOKEN", "codex-secret"),
        ("PSEUDOLIFE_CODEX_SERVER_URL", "http://127.0.0.1:2"),
        ("PSEUDOLIFE_AGENT_STATE", "/elsewhere/state"),
        ("PSEUDOLIFE_AGENT_STATE_DIR", "/elsewhere/agent"),
        ("PSEUDOLIFE_AGENT_LABEL", "someone-else"),
        ("PSEUDOLIFE_AGENT_PROJECT", "elsewhere"),
        ("PSEUDOLIFE_AGENT_TASK", "theirs"),
        ("CLAUDE_CODE_SESSION_ID", "their-session"),
        ("PSEUDOLIFE_WRITER_ID", "their-writer"),
        ("PSEUDOLIFE_MCP_SERVE_COMMAND", "[\"x\"]"),
        ("PSEUDOLIFE_HOOK_PROBE_MS", "9"),
        ("HTTP_PROXY", "http://proxy.example.com"),
        ("ALL_PROXY", "http://proxy.example.com"),
        // Kept: what the shim needs to find its folders and reach its daemon.
        ("HOME", "/home/fixture"),
        ("PATH", "/usr/bin"),
        ("XDG_DATA_HOME", "/home/fixture/.local/share"),
        ("PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS", "120"),
        ("HTTPS_PROXY", "http://proxy.example.com"),
        ("SSL_CERT_FILE", "/etc/ssl/cert.pem"),
    ];
    let executable = Path::new("/opt/runtime/pseudolife-stdio");
    let command = child_command(
        executable,
        &store.load("dot"),
        parent
            .iter()
            .map(|(name, value)| (OsString::from(name), OsString::from(value))),
    );
    assert_eq!(command.get_program(), executable);
    assert_eq!(command.get_args().collect::<Vec<_>>(), ["shim"]);
    let child: HashMap<String, String> = command
        .get_envs()
        .filter_map(|(name, value)| Some((name.to_str()?.to_owned(), value?.to_str()?.to_owned())))
        .collect();
    let token = store.0.join("token");
    let mut expected: HashMap<String, String> = [
        ("HOME", "/home/fixture"),
        ("PATH", "/usr/bin"),
        ("XDG_DATA_HOME", "/home/fixture/.local/share"),
        ("PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS", "120"),
        ("HTTPS_PROXY", "http://proxy.example.com"),
        ("SSL_CERT_FILE", "/etc/ssl/cert.pem"),
        ("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9"),
        ("PSEUDOLIFE_MCP_NO_SPAWN", "1"),
        ("PSEUDOLIFE_WRITER_ID", "tunnel"),
    ]
    .iter()
    .map(|(name, value)| (name.to_string(), value.to_string()))
    .collect();
    expected.insert(
        "PSEUDOLIFE_MCP_TOKEN_FILE".into(),
        token.to_str().unwrap().into(),
    );
    // On Windows names compare without case: one entry per name.
    let child: HashMap<String, String> = child
        .into_iter()
        .map(|(name, value)| {
            (
                if cfg!(windows) {
                    name.to_uppercase()
                } else {
                    name
                },
                value,
            )
        })
        .collect();
    let expected: HashMap<String, String> = expected
        .into_iter()
        .map(|(name, value)| {
            (
                if cfg!(windows) {
                    name.to_uppercase()
                } else {
                    name
                },
                value,
            )
        })
        .collect();
    assert_eq!(child, expected);
}

/// The child starts from a cleared environment, not the bridge's own: a
/// shell in the shim's place prints what it got. The test's process carries
/// cargo's variables, which an uncleared child would inherit.
#[cfg(unix)]
#[test]
fn the_child_starts_from_a_cleared_environment() {
    let store = Store::new();
    store.profile("dot");
    let folder = store.0.parent().unwrap().join("fake-shim");
    std::fs::create_dir_all(&folder).unwrap();
    std::fs::write(folder.join("shim"), "env\n").unwrap();
    assert!(std::env::var_os("CARGO_MANIFEST_DIR").is_some());
    let mut command = child_command(
        Path::new("/bin/sh"),
        &store.load("dot"),
        [
            ("PATH", "/usr/bin:/bin"),
            ("CLAUDE_CODE_SESSION_ID", "sentinel-session"),
        ]
        .into_iter()
        .map(|(name, value)| (OsString::from(name), OsString::from(value))),
    );
    command.current_dir(&folder).stdin(Stdio::null());
    let output = command.output().unwrap();
    assert!(output.status.success());
    let environment = String::from_utf8(output.stdout).unwrap();
    let names: Vec<&str> = environment
        .lines()
        .filter_map(|line| line.split_once('=').map(|(name, _)| name))
        .collect();
    assert!(!names.contains(&"CARGO_MANIFEST_DIR"), "{environment}");
    assert!(!names.contains(&"CLAUDE_CODE_SESSION_ID"), "{environment}");
    assert!(
        environment
            .lines()
            .any(|line| line == "PSEUDOLIFE_WRITER_ID=tunnel")
    );
    assert!(environment.lines().any(|line| line == "PATH=/usr/bin:/bin"));
}

#[test]
fn a_shim_that_fails_before_answering_is_named_once() {
    assert_eq!(silent_exit(1, false, false), Some(SHIM_SILENT));
    assert_eq!(silent_exit(0, false, false), None);
    assert_eq!(silent_exit(1, true, false), None);
    // Stopped by the bridge, not exited: named as such.
    assert_eq!(silent_exit(1, false, true), Some(SHIM_STOPPED));
    assert_eq!(silent_exit(1, true, true), None);
}

fn argv(values: &[&str]) -> Vec<OsString> {
    values.iter().map(OsString::from).collect()
}

#[test]
fn the_bridge_needs_its_profile_and_a_ready_one() {
    for values in [
        &[][..],
        &["--profile"],
        &["--profile-dir", "x"],
        &["--profile", "dot", "--profile", "dot"],
        &["--profile", "-x"],
        &["--profile", ""],
        &["--profile", "dot", "--json"],
    ] {
        assert_eq!(
            prepare(&argv(values)).err().as_deref(),
            Some(USAGE),
            "{values:?}"
        );
    }
    let store = Store::new();
    let dir = store.0.to_str().unwrap();
    assert_eq!(
        prepare(&argv(&["--profile", "dot", "--profile-dir", dir])).err(),
        Some(format!(
            "{}; `pseudolife-mcp tunnel status --profile dot` names the cause",
            store::UNAVAILABLE
        ))
    );
    let token = serde_json::to_string(&store.0.join("token")).unwrap();
    store.put(
        "draft.profile.json",
        format!(
            "{{\"name\": \"draft\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {token}}}"
        )
        .as_bytes(),
    );
    assert_eq!(
        prepare(&argv(&["--profile-dir", dir, "--profile", "draft"])).err(),
        Some("tunnel profile is not ready: its setup is unfinished, and this client cannot set a tunnel up yet; `pseudolife-mcp tunnel status --profile draft` shows what is missing".to_owned())
    );
    // The store admits an https address with a path; the shim would not.
    store.put(
        "web.profile.json",
        format!(
            "{{\"name\": \"web\", \"daemon_url\": \"https://memory.example.com/mcp\", \"token_file\": {token}, \"tunnel_id\": \"tunnel_0123\", \"state\": \"ready\", \"consent\": true}}"
        )
        .as_bytes(),
    );
    let refused = prepare(&argv(&["--profile-dir", dir, "--profile", "web"]))
        .err()
        .unwrap();
    assert!(refused.contains("not an http(s) origin"), "{refused}");
    assert!(refused.contains("tunnel status --profile web"), "{refused}");
    store.profile("dot");
    let start = prepare(&argv(&["--profile-dir", dir, "--profile", "dot"])).unwrap();
    assert_eq!(start.root, Path::new(dir));
    assert_eq!(start.profile.daemon_url, "http://127.0.0.1:9");
}

#[test]
fn only_saved_nonce_shapes_are_compared() {
    assert!(nonce_shaped(&*NONCE));
    assert!(nonce_shaped(&format!("query=\"{NONCE}\"")));
    assert!(!nonce_shaped(&NONCE[..31]));
    assert!(!nonce_shaped(&NONCE.to_uppercase()));
}
