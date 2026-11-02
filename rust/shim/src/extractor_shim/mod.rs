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
        if let Some(requested) = requested {
            config::validate_cli_token("model", requested)?;
        }
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
                let result = match req.into_body().collect().await {
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
        Ok(Response::builder()
            .status(status)
            .header("content-type", "application/json")
            .header("content-length", body.len())
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

pub fn run(args: Vec<OsString>) -> u8 {
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
    let config = match config::parse(args) {
        Ok(Some(c)) => c,
        Ok(None) => return 0,
        Err(e) => {
            eprintln!("extractor-shim: {e}");
            return 2;
        }
    };
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
    loop {
        match accept().await {
            Ok(value) => return value,
            Err(error) => {
                report(&error);
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    #[tokio::test]
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
        assert_eq!((answer, attempts, reports), (42, 3, 2));
    }
}
