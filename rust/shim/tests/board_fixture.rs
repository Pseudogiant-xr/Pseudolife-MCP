#![allow(dead_code)]
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};
pub const BANK: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
pub struct Home(pub std::path::PathBuf);
impl Default for Home {
    fn default() -> Self {
        Self::new()
    }
}
impl Home {
    pub fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "pseudolife-board-{}",
            uuid::Uuid::new_v4().simple()
        ));
        pseudolife_stdio::board::state::private_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        assert!(self.0.parent() == Some(std::env::temp_dir().as_path()));
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
#[derive(Clone)]
pub struct Request {
    pub path: String,
    pub headers: String,
    pub body: Value,
}
#[derive(Clone)]
pub struct Answer {
    pub status: u16,
    pub bytes: Vec<u8>,
    pub delay: Duration,
}
impl Answer {
    pub fn json(status: u16, value: Value) -> Self {
        Self {
            status,
            bytes: value.to_string().into_bytes(),
            delay: Duration::ZERO,
        }
    }
    pub fn text(status: u16, value: &[u8]) -> Self {
        Self {
            status,
            bytes: value.to_vec(),
            delay: Duration::ZERO,
        }
    }
    pub fn delayed(mut self, delay: Duration) -> Self {
        self.delay = delay;
        self
    }
}
pub struct Fixture {
    pub url: String,
    pub requests: Arc<Mutex<Vec<Request>>>,
    pub bank: Arc<Mutex<String>>,
    pub heartbeat: Arc<Mutex<std::collections::VecDeque<(u16, Value)>>>,
    pub answers: Arc<Mutex<std::collections::HashMap<String, std::collections::VecDeque<Answer>>>>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Fixture {
    pub fn new(register_refusals: usize) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let requests = Arc::new(Mutex::new(vec![]));
        let bank = Arc::new(Mutex::new(BANK.to_owned()));
        let stop = Arc::new(AtomicBool::new(false));
        let heartbeat = Arc::new(Mutex::new(std::collections::VecDeque::<(u16, Value)>::new()));
        let beats = heartbeat.clone();
        let answers = Arc::new(Mutex::new(std::collections::HashMap::<
            String,
            std::collections::VecDeque<Answer>,
        >::new()));
        let replies = answers.clone();
        let (log, identity, stopping) = (requests.clone(), bank.clone(), stop.clone());
        let thread = thread::spawn(move || {
            let mut attempts = 0;
            for stream in listener.incoming() {
                let mut stream = stream.unwrap();
                if stopping.load(Ordering::Acquire) {
                    break;
                }
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut bytes = vec![];
                let mut buffer = [0u8; 4096];
                let mut wanted = None;
                loop {
                    let length = match stream.read(&mut buffer) {
                        Ok(length) if length > 0 => length,
                        _ => break,
                    };
                    bytes.extend_from_slice(&buffer[..length]);
                    if let Some(split) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
                        let headers = String::from_utf8_lossy(&bytes[..split]);
                        let length = headers
                            .lines()
                            .find_map(|line| {
                                line.to_lowercase()
                                    .strip_prefix("content-length:")
                                    .and_then(|v| v.trim().parse::<usize>().ok())
                            })
                            .unwrap_or(0);
                        wanted = Some(split + 4 + length);
                    }
                    if wanted.is_some_and(|wanted| bytes.len() >= wanted) {
                        break;
                    }
                }
                let Some(split) = bytes.windows(4).position(|window| window == b"\r\n\r\n") else {
                    continue;
                };
                let headers = String::from_utf8_lossy(&bytes[..split]).to_string();
                let path = headers
                    .lines()
                    .next()
                    .unwrap_or("")
                    .split_whitespace()
                    .nth(1)
                    .unwrap_or("")
                    .to_owned();
                let body = serde_json::from_slice(&bytes[split + 4..]).unwrap_or(json!({}));
                log.lock().unwrap().push(Request {
                    path: path.clone(),
                    headers: headers.to_lowercase(),
                    body,
                });
                let (status, value) = if path.ends_with("coordination-start") {
                    (200, json!("ready"))
                } else if path.ends_with("/context") {
                    (
                        200,
                        json!({"bank_id":identity.lock().unwrap().clone(),"principal":"fixture"}),
                    )
                } else if path.ends_with("/register") {
                    attempts += 1;
                    if attempts <= register_refusals {
                        (503, json!({"error":"principals_unavailable"}))
                    } else {
                        (
                            200,
                            json!({"agent_id":"fixture-agent","credential":"fixture-agent-key"}),
                        )
                    }
                } else if path.ends_with("/attach") || path.ends_with("/heartbeat") {
                    let ordinary = (
                        200,
                        json!({"generation":1,"pending_count":1,"pending_preview":[]}),
                    );
                    if path.ends_with("/heartbeat") {
                        beats.lock().unwrap().pop_front().unwrap_or(ordinary)
                    } else {
                        ordinary
                    }
                } else {
                    (200, json!({}))
                };
                let body = if path.ends_with("coordination-start") {
                    "ready".to_owned()
                } else {
                    value.to_string()
                };
                let ordinary = Answer::text(status, body.as_bytes());
                let answer = replies
                    .lock()
                    .unwrap()
                    .get_mut(path.rsplit('/').next().unwrap())
                    .and_then(|answers| answers.pop_front())
                    .unwrap_or(ordinary);
                let until = std::time::Instant::now() + answer.delay;
                while std::time::Instant::now() < until && !stopping.load(Ordering::Acquire) {
                    thread::sleep(Duration::from_millis(2));
                }
                if stopping.load(Ordering::Acquire) {
                    break;
                }
                if answer.status == 0 {
                    continue;
                }
                let response = format!(
                    "HTTP/1.1 {} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    answer.status,
                    answer.bytes.len()
                );
                let _ = stream.write_all(response.as_bytes());
                let _ = stream.write_all(&answer.bytes);
            }
        });
        Self {
            url: format!("http://{address}"),
            requests,
            bank,
            heartbeat,
            answers,
            stop,
            thread: Some(thread),
        }
    }
    pub fn count(&self, action: &str) -> usize {
        self.requests
            .lock()
            .unwrap()
            .iter()
            .filter(|request| request.path.ends_with(action))
            .count()
    }
    pub fn answer(&self, action: &str, answer: Answer) {
        self.answers
            .lock()
            .unwrap()
            .entry(action.into())
            .or_default()
            .push_back(answer);
    }
    pub async fn wait(&self, action: &str, count: usize) {
        tokio::time::timeout(Duration::from_secs(2), async {
            while self.count(action) < count {
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
        })
        .await
        .unwrap();
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let _ = TcpStream::connect(self.url.trim_start_matches("http://"));
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}
pub fn runtime(fixture: &Fixture, codex: bool) -> Arc<pseudolife_stdio::lifecycle::Runtime> {
    Arc::new(pseudolife_stdio::lifecycle::Runtime {
        url: fixture.url.clone(),
        provider: pseudolife_stdio::credentials::CredentialProvider::new(
            Some("fixture-bank-key".into()),
            None,
        )
        .unwrap(),
        session: pseudolife_stdio::lifecycle::SessionIdentity::new(codex.then_some("codex"), None),
        health: Some(json!({"status":"ok"})),
        instructions_note: String::new(),
        cache: None,
    })
}
pub fn options(
    runtime: &pseudolife_stdio::lifecycle::Runtime,
    home: &Home,
    setting: &str,
) -> pseudolife_stdio::board::Options {
    pseudolife_stdio::board::Options::from_lookup(runtime, false, |key| match key {
        "PSEUDOLIFE_AGENT_COORDINATION" => Some(setting.into()),
        "PSEUDOLIFE_AGENT_STATE_DIR" => Some(home.0.join("agents").to_string_lossy().into()),
        "PSEUDOLIFE_DIGEST_DIR" => Some(home.0.join("digests").to_string_lossy().into()),
        "CLAUDE_CODE_SESSION_ID" => Some(BANK.into()),
        _ => None,
    })
}
pub fn adapter_config(fixture: &Fixture, home: &Home) -> pseudolife_stdio::board::adapter::Config {
    pseudolife_stdio::board::adapter::Config {
        url: fixture.url.clone(),
        provider: runtime(fixture, false).provider.clone(),
        state: Some(home.0.join("agent.json")),
        digest: Some(home.0.join("digest.txt")),
        label: "fixture".into(),
        project: String::new(),
        task: String::new(),
        episode: BANK.into(),
        codex: true,
        wake: false,
        ring: false,
        parent: None,
        initial_snapshot: None,
        legacy_state: None,
    }
}
