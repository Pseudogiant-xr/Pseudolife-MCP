//! Opt-in OpenAI-compatible completion endpoints backed by isolated CLI calls.
mod autostart;
mod cli;
mod config;
mod json;
mod tools;

use bytes::Bytes;
use config::{Config, Kind};
use http_body_util::{BodyExt, Full};
use hyper::{Request, Response, body::Incoming, server::conn::http1, service::service_fn};
use hyper_util::rt::TokioIo;
use serde_json::{Value, json};
use std::{
    convert::Infallible,
    ffi::OsString,
    sync::{Arc, Mutex},
    time::Instant,
};

struct Health {
    body: Value,
    ok: bool,
    at: Instant,
    refreshing: bool,
}
struct Shim {
    config: Config,
    calls: tokio::sync::Mutex<Option<cli::Instructions>>,
    health: Mutex<Option<Health>>,
    override_misses: Mutex<std::collections::HashSet<String>>,
    unlisted_models: Mutex<std::collections::HashSet<String>>,
}

impl Shim {
    async fn chat(
        &self,
        system: &str,
        user: &str,
        model: &str,
        effort: Option<&str>,
    ) -> Result<String, cli::Failure> {
        config::validate_cli_token("model", model)?;
        if let Some(effort) = effort.or(self.config.effort.as_deref()) {
            config::validate_cli_token("reasoning-effort", effort)?;
        }
        if !self.config.kind.models().contains(&model)
            && self
                .unlisted_models
                .lock()
                .expect("model warnings")
                .insert(model.to_owned())
        {
            cli::log(
                &self.config,
                &format!(
                    "{} shim: unlisted model {model}; passing it through to the CLI",
                    self.config.kind.name()
                ),
            );
        }
        if self.config.kind == Kind::Claude
            && self.config.system.as_ref().is_some_and(|s| !s.is_empty())
            && !system.is_empty()
            && !system.starts_with(
                &include_str!("../../../../evals/prompts/assistant_facts_provenance.txt")
                    .replace("\r\n", "\n"),
            )
        {
            let head = json::head(system, 60);
            if self
                .override_misses
                .lock()
                .expect("override warnings")
                .insert(head.clone())
            {
                cli::log(
                    &self.config,
                    &format!(
                        "--system-prompt-file did not apply: this prompt does not start with dream._SYSTEM_PROMPT, so the shipped text was sent unchanged (prompt begins {}). Expected for every non-claims prompt (events, relations, lessons, digest, outcome inference, the review-queue judges); for claims extraction it means the variant never reached the model.",
                        json::repr(&head)
                    ),
                );
            }
        }
        let mut instructions = self.calls.lock().await;
        cli::call(&self.config, &mut instructions, system, user, model, effort).await
    }
    async fn refresh(&self) -> (bool, Value) {
        let c = &self.config;
        let (system, user) = if c.kind == Kind::Claude {
            ("", "Reply with exactly: OK")
        } else {
            ("Reply with exactly: OK", "ping")
        };
        let (ok, body) = match self.chat(system, user, &c.model, None).await {
            Ok(_) => (true, json!({"status":"ok"})),
            Err(e) => {
                let detail = json::head(&e.message, 600);
                let body = if e.sign_in {
                    let user = ["LOGNAME", "USER", "LNAME", "USERNAME"]
                        .into_iter()
                        .find_map(|k| std::env::var(k).ok().filter(|s| !s.is_empty()));
                    let who = user
                        .as_ref()
                        .map(|u| format!("as {u}"))
                        .unwrap_or_else(|| "as the user the shim runs as".into());
                    let fix = if c.kind == Kind::Claude {
                        format!(
                            "sign in to the Claude CLI on the shim host {who}: run `claude`, then `/login`; /health recovers within 30 seconds, no restart needed"
                        )
                    } else {
                        format!(
                            "sign in to the Codex CLI on the shim host {who}: run `codex login` (or `codex login --with-api-key` for an API key); /health recovers within 30 seconds, no restart needed"
                        )
                    };
                    let mut v = json!({"status":"login_required","detail":detail,"fix":fix,"provider":c.kind.name()});
                    if let Some(user) = user {
                        v["user"] = json!(user);
                    }
                    v
                } else {
                    json!({"status":"cli_error","detail":detail})
                };
                (false, body)
            }
        };
        *self.health.lock().expect("health mutex") = Some(Health {
            ok,
            body: body.clone(),
            at: Instant::now(),
            refreshing: false,
        });
        (ok, body)
    }
    async fn health(self: &Arc<Self>) -> (bool, Value) {
        let cached = {
            let mut guard = self.health.lock().expect("health mutex");
            guard.as_mut().map(|h| {
                let ttl = if h.body["status"] == "login_required" {
                    if self.config.kind == Kind::Claude {
                        std::time::Duration::from_secs(30)
                    } else {
                        self.config.ttl.min(std::time::Duration::from_secs(30))
                    }
                } else {
                    self.config.ttl
                };
                if h.at.elapsed() >= ttl && !h.refreshing {
                    h.refreshing = true;
                    let this = self.clone();
                    tokio::spawn(async move {
                        this.refresh().await;
                    });
                }
                (h.ok, h.body.clone())
            })
        };
        if let Some(v) = cached {
            v
        } else {
            self.refresh().await
        }
    }
    async fn completion(&self, req: Value) -> Result<Value, String> {
        if !req.is_object() {
            return Err(format!(
                "'{}' object has no attribute 'get'",
                json::typename(&req)
            ));
        }
        let empty = vec![];
        let msgs = match req.get("messages") {
            None => &empty,
            Some(Value::Array(a)) => a,
            Some(v) => return Err(format!("'{}' object is not iterable", json::typename(v))),
        };
        let c = &self.config;
        let (mut system, user) = if c.emulate {
            tools::fold(msgs)
        } else {
            plain_messages(msgs)?
        };
        if c.emulate && tools::enabled(&req["tool_choice"]) {
            let rendered = tools::render(&req["tools"]);
            if !rendered.is_empty() {
                if !system.is_empty() {
                    system.push_str("\n\n");
                }
                system.push_str(&rendered);
            }
        }
        let requested = match req.get("model").filter(|v| json::truth(v)) {
            None => None,
            Some(Value::String(s)) => Some(s.as_str()),
            Some(v) => {
                return Err(format!(
                    "'{}' object has no attribute 'startswith'",
                    json::typename(v)
                ));
            }
        };
        let model = c.kind.resolve_model(requested, &c.model);
        let effort = req["reasoning_effort"]
            .as_str()
            .map(|s| s.trim_matches(' '))
            .filter(|s| !s.is_empty());
        let reply = self
            .chat(&system, &user, &model, effort)
            .await
            .map_err(|e| e.message)?;
        let mut calls = if c.emulate {
            tools::parse(&reply)
        } else {
            None
        };
        if c.emulate && calls.is_none() && tools::looks_like(&reply) {
            calls = tools::parse(
                &self
                    .chat(
                        &system,
                        &format!("{user}\n\n{}", tools::RETRY),
                        &model,
                        effort,
                    )
                    .await
                    .map_err(|e| e.message)?,
            );
        }
        let (message, finish) = if let Some(calls) = calls {
            (
                json!({"role":"assistant","content":null,"tool_calls":calls}),
                "tool_calls",
            )
        } else {
            (json!({"role":"assistant","content":reply}), "stop")
        };
        let ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis();
        Ok(
            json!({"id":format!("{}-shim-{ms}",c.kind.name()),"object":"chat.completion","model":model,"choices":[{"index":0,"message":message,"finish_reason":finish}],"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}),
        )
    }
    async fn request(
        self: Arc<Self>,
        req: Request<Incoming>,
    ) -> Result<Response<Full<Bytes>>, Infallible> {
        let (parts, body) = req.into_parts();
        let req = Request::from_parts(parts, ());
        let mut completion_body = Some(body);
        if req.method() != hyper::Method::POST
            || !matches!(
                req.uri().path_and_query().map(|path| path.as_str()),
                Some("/chat/completions" | "/v1/chat/completions")
            )
        {
            // Keep Incoming alive while separately sent bytes arrive; dropping
            // it makes Hyper close early. Preserve Python's immediate response
            // for ignored bodies rather than waiting for the sender to finish.
            let mut ignored = completion_body.take().expect("request body");
            tokio::spawn(async move {
                while let Some(frame) = ignored.frame().await {
                    if frame.is_err() {
                        break;
                    }
                }
            });
        }
        if !matches!(req.method().as_str(), "GET" | "POST") {
            let method = req
                .method()
                .as_str()
                .replace('&', "&amp;")
                .replace('<', "&lt;")
                .replace('>', "&gt;");
            let body = format!(
                "<!DOCTYPE HTML>\n<html lang=\"en\">\n    <head>\n        <meta charset=\"utf-8\">\n        <title>Error response</title>\n    </head>\n    <body>\n        <h1>Error response</h1>\n        <p>Error code: 501</p>\n        <p>Message: Unsupported method ('{method}').</p>\n        <p>Error code explanation: 501 - Server does not support this operation.</p>\n    </body>\n</html>\n"
            );
            let length = body.len();
            return Ok(Response::builder()
                .status(501)
                .header("content-type", "text/html;charset=utf-8")
                .header("content-length", length)
                .header("connection", "close")
                .body(Full::new(Bytes::from(if method == "HEAD" {
                    String::new()
                } else {
                    body
                })))
                .expect("method error"));
        }
        let path = req
            .uri()
            .path_and_query()
            .map(|s| s.as_str())
            .unwrap_or("/");
        let (status, body) = match (req.method().as_str(), path) {
            ("GET", "/health") => {
                let (ok, body) = self.health().await;
                (if ok { 200 } else { 503 }, body)
            }
            ("GET", "/models" | "/v1/models") => {
                let mut models = vec![self.config.model.as_str()];
                for m in self
                    .config
                    .kind
                    .models()
                    .iter()
                    .copied()
                    .chain(["extractor", "bench"])
                {
                    if !models.contains(&m) {
                        models.push(m);
                    }
                }
                (
                    200,
                    json!({"object":"list","data":models.into_iter().map(|m|json!({"id":m,"object":"model"})).collect::<Vec<_>>()}),
                )
            }
            ("POST", "/chat/completions" | "/v1/chat/completions") => {
                let result = match completion_body
                    .take()
                    .expect("completion body")
                    .collect()
                    .await
                {
                    Ok(body) => {
                        let raw = body.to_bytes();
                        let text = String::from_utf8_lossy(&raw);
                        match serde_json::from_slice(&raw) {
                            Ok(v) => self.completion(v).await,
                            Err(e) => Err(decode_error(&text, e)),
                        }
                    }
                    Err(e) => Err(e.to_string()),
                };
                match result {
                    Ok(v) => (200, v),
                    Err(e) => {
                        cli::log(
                            &self.config,
                            &format!("{}_shim: request failed: {e}", self.config.kind.name()),
                        );
                        (500, json!({"error":e}))
                    }
                }
            }
            _ => (404, json!({"error":"not found"})),
        };
        let body = json::dumps(&body, true);
        let response = Response::builder()
            .status(status)
            .header("content-type", "application/json")
            .header("content-length", body.len());
        Ok(response
            .body(Full::new(Bytes::from(body)))
            .expect("HTTP response"))
    }
}

fn plain_messages(msgs: &[Value]) -> Result<(String, String), String> {
    let mut system = vec![];
    let mut user = vec![];
    for msg in msgs {
        if !msg.is_object() {
            return Err(format!(
                "'{}' object has no attribute 'get'",
                json::typename(msg)
            ));
        }
        let content = &msg["content"];
        if !json::truth(content) {
            continue;
        }
        let target = if msg["role"] == "system" {
            &mut system
        } else {
            &mut user
        };
        match content.as_str() {
            Some(s) => target.push(s),
            None => {
                return Err(format!(
                    "sequence item {}: expected str instance, {} found",
                    target.len(),
                    json::typename(content)
                ));
            }
        }
    }
    Ok((system.join("\n\n"), user.join("\n\n")))
}

fn decode_error(raw: &str, error: serde_json::Error) -> String {
    let reason = error.to_string();
    let mut pos = raw
        .lines()
        .take(error.line().saturating_sub(1))
        .map(|s| s.chars().count() + 1)
        .sum::<usize>()
        + error.column().saturating_sub(1);
    let message = if reason.starts_with("key must be a string") {
        "Expecting property name enclosed in double quotes"
    } else if reason.starts_with("expected `,` or `}`") || reason.starts_with("expected `,` or `]`")
    {
        "Expecting ',' delimiter"
    } else if reason.starts_with("expected `:`") {
        "Expecting ':' delimiter"
    } else if reason.starts_with("trailing characters") {
        "Extra data"
    } else if reason.starts_with("EOF while parsing an object") {
        pos = raw.chars().count();
        "Expecting property name enclosed in double quotes"
    } else if reason.starts_with("EOF while parsing a string") {
        pos = raw.rfind('"').unwrap_or(0);
        "Unterminated string starting at"
    } else {
        if raw.trim().is_empty() {
            pos = raw.chars().count();
        }
        "Expecting value"
    };
    let prefix = raw.chars().take(pos).collect::<String>();
    let line = prefix.chars().filter(|c| *c == '\n').count() + 1;
    let col = prefix.rsplit('\n').next().unwrap_or("").chars().count() + 1;
    format!("{message}: line {line} column {col} (char {pos})")
}

/// Synchronous dispatch keeps this mode outside the MCP runtime and its bank.
pub fn dispatch_if_requested(args: &[OsString]) {
    if args.first().is_some_and(|a| a == "extractor-shim") {
        std::process::exit(i32::from(run(args[1..].to_vec())));
    }
}

pub fn run(mut args: Vec<OsString>) -> u8 {
    if args
        .first()
        .is_some_and(|a| a == "install-autostart" || a == "uninstall-autostart")
    {
        return match autostart::install(&args[1..], args[0] == "uninstall-autostart") {
            Ok(()) => 0,
            Err(e) => {
                eprintln!("extractor-shim: {e}");
                1
            }
        };
    }
    // Local-only check, used after the registration helper proves socket
    // ownership. Neither loaded settings nor their hash cross the HTTP API.
    let verify_pid = if args.first().is_some_and(|a| a == "verify-settings") {
        let pid = args
            .get(1)
            .and_then(|p| p.to_str())
            .and_then(|p| p.parse::<u32>().ok());
        let Some(pid) = pid.filter(|p| *p != 0) else {
            eprintln!("extractor-shim: verify-settings needs a process ID and provider");
            return 2;
        };
        args.drain(..2);
        Some(pid)
    } else {
        None
    };
    let config = match config::parse(args) {
        Ok(Some(c)) => c,
        Ok(None) => return 0,
        Err(e) => {
            eprintln!("extractor-shim: {e}");
            return 2;
        }
    };
    if let Some(pid) = verify_pid {
        return u8::from(config::verify_settings(&config, pid).is_err());
    }
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(r) => r,
        Err(e) => {
            eprintln!("{e}");
            return 1;
        }
    };
    match runtime.block_on(serve(config)) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("extractor-shim: {e}");
            1
        }
    }
}

async fn serve(config: Config) -> Result<(), Box<dyn std::error::Error>> {
    let _settings_receipt = if config.managed {
        Some(config::SettingsReceipt::create(&config)?)
    } else {
        None
    };
    let shim = Arc::new(Shim {
        config,
        calls: tokio::sync::Mutex::new(None),
        health: Mutex::new(None),
        override_misses: Mutex::new(std::collections::HashSet::new()),
        unlisted_models: Mutex::new(std::collections::HashSet::new()),
    });
    let (ok, body) = shim.refresh().await;
    cli::log(
        &shim.config,
        &format!(
            "{}_shim: health warm -> {}",
            shim.config.kind.name(),
            if ok {
                "ok"
            } else {
                body["detail"].as_str().unwrap_or("CLI error")
            }
        ),
    );
    // Tokio does not enable SO_REUSEADDR on Windows; an existing listener
    // must fail this bind, as ShimHTTPServer does in the Python oracle.
    let listener =
        tokio::net::TcpListener::bind((shim.config.host.as_str(), shim.config.port)).await?;
    cli::log(
        &shim.config,
        &format!(
            "{}_shim: serving {} on http://{}:{}/v1",
            shim.config.kind.name(),
            shim.config.model,
            shim.config.host,
            listener.local_addr()?.port()
        ),
    );
    loop {
        let (stream, _) = retry_accept(
            || listener.accept(),
            |e| {
                cli::log(
                    &shim.config,
                    &format!("extractor-shim: accept failed: {e}; retrying"),
                )
            },
        )
        .await;
        let shim = shim.clone();
        tokio::spawn(async move {
            let service = service_fn(move |req| {
                let shim = shim.clone();
                async move {
                    // A disconnected HTTP caller does not cancel the queued
                    // CLI call; Python's request thread has the same lifetime.
                    tokio::spawn(shim.request(req)).await.expect("request task")
                }
            });
            let _ = http1::Builder::new()
                .serve_connection(TokioIo::new(stream), service)
                .await;
        });
    }
}

async fn retry_accept<T, F, Fut>(mut accept: F, mut report: impl FnMut(&std::io::Error)) -> T
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = std::io::Result<T>>,
{
    let mut delay = std::time::Duration::from_millis(100);
    let mut report_at = tokio::time::Instant::now();
    loop {
        match accept().await {
            Ok(value) => return value,
            Err(error) => {
                let now = tokio::time::Instant::now();
                if now >= report_at {
                    report(&error);
                    report_at = now + std::time::Duration::from_secs(30);
                }
                tokio::time::sleep(delay).await;
                delay = (delay * 2).min(std::time::Duration::from_secs(5));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    #[tokio::test]
    async fn ignored_request_body_keeps_early_response_and_connection() {
        use super::*;
        use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};

        async fn response(
            reader: &mut BufReader<tokio::io::ReadHalf<tokio::io::DuplexStream>>,
            status: u16,
        ) -> Vec<u8> {
            let mut headers = String::new();
            loop {
                let mut line = String::new();
                assert_ne!(
                    reader.read_line(&mut line).await.unwrap(),
                    0,
                    "response closed early"
                );
                if line == "\r\n" {
                    break;
                }
                headers.push_str(&line);
            }
            assert!(headers.starts_with(&format!("HTTP/1.1 {status} ")));
            let length: usize = headers
                .lines()
                .find_map(|line| {
                    let (key, value) = line.split_once(':')?;
                    key.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse().unwrap())
                })
                .unwrap();
            let mut body = vec![0; length];
            reader.read_exact(&mut body).await.unwrap();
            body
        }

        let config = config::parse(vec![
            "claude".into(),
            "--cli".into(),
            std::env::current_exe().unwrap().into_os_string(),
            "--model".into(),
            "fixture".into(),
        ])
        .unwrap()
        .unwrap();
        let shim = Arc::new(Shim {
            config,
            calls: tokio::sync::Mutex::new(None),
            health: Mutex::new(None),
            override_misses: Mutex::new(Default::default()),
            unlisted_models: Mutex::new(Default::default()),
        });
        for (method, path, status) in [
            ("POST", "/missing", 404),
            ("GET", "/models", 200),
            ("PUT", "/models", 501),
        ] {
            let (client, server) = tokio::io::duplex(4096);
            let shim = shim.clone();
            let service = service_fn(move |req| {
                let shim = shim.clone();
                async move { tokio::spawn(shim.request(req)).await.unwrap() }
            });
            let connection = tokio::spawn(async move {
                http1::Builder::new()
                    .serve_connection(TokioIo::new(server), service)
                    .await
            });
            let (reader, mut writer) = tokio::io::split(client);
            let mut reader = BufReader::new(reader);
            writer
                .write_all(
                    format!(
                        "{method} {path} HTTP/1.1\r\nHost: fixture\r\nContent-Length: 2\r\n\r\n"
                    )
                    .as_bytes(),
                )
                .await
                .unwrap();
            // The completed response is the barrier: Python answers ignored
            // bodies immediately, including when their bytes are withheld.
            let body = tokio::time::timeout(
                std::time::Duration::from_secs(5),
                response(&mut reader, status),
            )
            .await
            .unwrap();
            match status {
                404 => assert_eq!(body.as_slice(), br#"{"error": "not found"}"#),
                501 => assert!(
                    String::from_utf8(body)
                        .unwrap()
                        .contains("Unsupported method ('PUT')")
                ),
                _ => assert_eq!(
                    serde_json::from_slice::<Value>(&body).unwrap()["data"][0]["id"],
                    "fixture"
                ),
            }
            if status != 501 {
                // Python's unsupported-method reply explicitly closes.
                // Only now release the ignored body (invalid JSON on purpose),
                // followed by another request. Dropping Incoming closes this
                // connection before the second response; retaining it does not.
                writer
                    .write_all(b"!?GET /models HTTP/1.1\r\nHost: fixture\r\n\r\n")
                    .await
                    .unwrap();
                let body = tokio::time::timeout(
                    std::time::Duration::from_secs(5),
                    response(&mut reader, 200),
                )
                .await
                .unwrap();
                assert_eq!(
                    serde_json::from_slice::<Value>(&body).unwrap()["data"][0]["id"],
                    "fixture"
                );
            }
            drop(reader);
            drop(writer);
            connection.await.unwrap().unwrap();
        }
    }

    #[tokio::test(start_paused = true)]
    async fn transient_accept_errors_do_not_stop_the_server() {
        let mut attempts = 0;
        let mut reports = 0;
        let answer = super::retry_accept(
            || {
                attempts += 1;
                std::future::ready(if attempts < 3 {
                    Err(std::io::Error::other("temporary resource exhaustion"))
                } else {
                    Ok(42)
                })
            },
            |_| reports += 1,
        )
        .await;
        assert_eq!((answer, attempts, reports), (42, 3, 1));
    }

    #[tokio::test(start_paused = true)]
    async fn persistent_accept_errors_back_off_and_limit_logs() {
        let mut attempts = vec![];
        let mut reports = vec![];
        let answer = super::retry_accept(
            || {
                attempts.push(tokio::time::Instant::now());
                std::future::ready(if attempts.len() <= 20 {
                    Err(std::io::Error::other("persistent resource exhaustion"))
                } else {
                    Ok(42)
                })
            },
            |_| reports.push(tokio::time::Instant::now()),
        )
        .await;
        assert_eq!(answer, 42);
        let delays: Vec<_> = attempts
            .windows(2)
            .map(|a| (a[1] - a[0]).as_millis())
            .collect();
        assert_eq!(&delays[..7], &[100, 200, 400, 800, 1600, 3200, 5000]);
        assert!(delays[7..].iter().all(|&delay| delay == 5000));
        assert_eq!(reports.len(), 3);
        assert!(
            reports
                .windows(2)
                .all(|a| a[1] - a[0] >= std::time::Duration::from_secs(30))
        );
    }
}
