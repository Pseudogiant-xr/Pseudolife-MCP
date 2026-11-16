//! The bridge over in-memory streams: a scripted fake shim stands in for
//! the stdio shim, so nothing is spawned and no socket or daemon is used.
use super::*;

use std::sync::Mutex;
use tokio::io::{AsyncReadExt, duplex};

const NONCE: &str = "0123456789abcdef0123456789abcdef";
const OTHER_NONCE: &str = "fedcba9876543210fedcba9876543210";
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

/// Counts the reporter's lines instead of printing them.
static REPORTED: Mutex<usize> = Mutex::new(0);
fn counting_reporter() -> Reporter {
    Reporter {
        said: Arc::default(),
        say: |_| *REPORTED.lock().unwrap() += 1,
    }
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
            if let Some(message) = message(&line) {
                for answer in script(&message) {
                    writer.write_all(answer.as_bytes()).await.unwrap();
                }
            }
            line.clear();
        }
        drop(fake_writer);
        received
    });
    client_writer.write_all(client_bytes).await.unwrap();
    let relayed = tokio::spawn(relay(
        client_in, client_out, shim_in, shim_out, observer, gate,
    ));
    drop(client_writer);
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
    store.challenge("dot", NONCE, created);
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
    assert_eq!(receipt["nonce"], NONCE);
    assert_eq!(receipt["tool"], "memory_search");
    assert_eq!(receipt["success"], true);
    assert!(receipt["observed"].as_f64().unwrap() >= created);
}

#[tokio::test]
async fn a_successful_board_list_with_the_nonce_writes_its_own_receipt() {
    let store = Store::new();
    store.profile("dot");
    store.challenge("dot", NONCE, now() - 5.0);
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
    assert_eq!(written[0].1["nonce"], NONCE);
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
        ("isError", search(1, NONCE), answering(failed)),
        ("ok false", search(1, NONCE), answering(refused)),
        ("available false", search(1, NONCE), answering(unavailable)),
        ("malformed result", search(1, NONCE), answering(malformed)),
        (
            "JSON-RPC error",
            search(1, NONCE),
            Box::new(|m: &Map<String, Value>| {
                vec![format!(
                    "{}\n",
                    json!({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603, "message": "x"}})
                )]
            }),
        ),
        ("no nonce", search(1, "what did we decide"), succeeding()),
        ("wrong nonce", search(1, OTHER_NONCE), succeeding()),
        (
            "nonce in another argument",
            call(
                json!(1),
                "memory_search",
                json!({"query": "x", "tags": [NONCE]}),
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
            search(1, NONCE),
            Box::new(|_: &Map<String, Value>| vec![success(&json!(2))]),
        ),
        (
            "id reused before the answer",
            format!("{}{}", search(1, NONCE), search(1, "plain")),
            Box::new(|m: &Map<String, Value>| {
                // Both answers come after the second request is read.
                if m["params"]["arguments"]["query"] == "plain" {
                    vec![success(&m["id"]), success(&m["id"])]
                } else {
                    Vec::new()
                }
            }),
        ),
    ];
    for (case, request, script) in cases {
        let store = Store::new();
        store.profile("dot");
        store.challenge("dot", NONCE, now() - 5.0);
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
    store.challenge("dot", OTHER_NONCE, now() - 5.0);
    store.challenge("lab", NONCE, now() - 5.0);
    for request in [
        search(1, NONCE),
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
    store.challenge("dot", NONCE, now() + 3600.0);
    let recorder = Recorder::default();
    session(
        search(1, NONCE).as_bytes(),
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
    store.challenge("dot", NONCE, now() - 5.0);
    let mut started = store.load("dot");
    started.daemon_url = "http://127.0.0.1:10".into();
    let recorder = Recorder::default();
    session(
        search(1, NONCE).as_bytes(),
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
    store.challenge("dot", NONCE, now() - 5.0);
    let failing: Sink = Arc::new(|_, _| Err(()));
    let request = format!(
        "{}{}{}",
        search(1, NONCE),
        call(
            json!(2),
            "memory_agents",
            json!({"action": "list", "project": NONCE})
        ),
        search(3, "after")
    );
    let before = *REPORTED.lock().unwrap();
    let (out, received) = session(
        request.as_bytes(),
        Some(observer(&store, "dot", failing)),
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
    // Other tests may report too; this session reported exactly once.
    assert_eq!(*REPORTED.lock().unwrap() - before, 1);
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
        search(2, NONCE),
        "not json at all\n",
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
    store.challenge("dot", NONCE, now() - 5.0);
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
fn the_child_never_inherits_a_token_or_daemon_address() {
    let store = Store::new();
    store.profile("dot");
    let inherited = [
        "PSEUDOLIFE_MCP_TOKEN",
        "PSEUDOLIFE_MCP_TOKEN_FILE",
        "PSEUDOLIFE_MCP_TOKENS",
        "pseudolife_mcp_token_file",
        "PSEUDOLIFE_MCP_TOKEN_SOMETHING_NEW",
        "PSEUDOLIFE_MCP_DAEMON_URL",
        "OPENAI_API_KEY",
        // Kept: what the shim reads for its home, state and timeouts.
        "HOME",
        "PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS",
        "PSEUDOLIFE_WRITER_ID",
        "SSL_CERT_FILE",
    ];
    let (dropped, set) =
        child_environment(&store.load("dot"), inherited.iter().map(OsString::from));
    let expected: Vec<OsString> = inherited[..7].iter().map(OsString::from).collect();
    assert_eq!(dropped, expected);
    let token = store.0.join("token");
    assert_eq!(
        set.to_vec(),
        [
            ("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9".to_owned()),
            (
                "PSEUDOLIFE_MCP_TOKEN_FILE",
                token.to_str().unwrap().to_owned()
            ),
            ("PSEUDOLIFE_MCP_NO_SPAWN", "1".to_owned()),
        ]
    );
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
        assert_eq!(prepare(&argv(values)).err(), Some(USAGE), "{values:?}");
    }
    let store = Store::new();
    let dir = store.0.to_str().unwrap();
    assert_eq!(
        prepare(&argv(&["--profile", "dot", "--profile-dir", dir])).err(),
        Some(store::UNAVAILABLE)
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
        Some(NOT_READY)
    );
    store.profile("dot");
    let start = prepare(&argv(&["--profile-dir", dir, "--profile", "dot"])).unwrap();
    assert_eq!(start.root, Path::new(dir));
    assert_eq!(start.profile.daemon_url, "http://127.0.0.1:9");
}

#[test]
fn only_saved_nonce_shapes_are_compared() {
    assert!(nonce_shaped(NONCE));
    assert!(nonce_shaped(&format!("query=\"{NONCE}\"")));
    assert!(!nonce_shaped(&NONCE[..31]));
    assert!(!nonce_shaped(&NONCE.to_uppercase()));
}
