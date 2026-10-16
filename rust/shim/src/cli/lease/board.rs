use super::json::{Text, Value, json};
use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::sync::Mutex;

#[derive(Debug)]
pub struct Failure {
    pub text: String,
    pub transient: bool,
    pub fatal_input: bool,
    pub code: Option<String>,
}
impl Failure {
    fn refused(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            transient: false,
            fatal_input: false,
            code: None,
        }
    }
    fn forbidden(field: &str) -> Self {
        Self {
            text: format!("HTTP_FORBIDDEN_INPUT_REFUSED: invalid {field} header"),
            fatal_input: true,
            ..Self::refused("")
        }
    }
    pub fn report(&self) -> i32 {
        super::say(&format!("lease: {}", self.text));
        1
    }
}
fn forbidden(value: &Text) -> bool {
    value.chars().any(|c| c < '\u{20}' || c == '\u{7f}')
}
fn instance_header(field: &str, value: &Text) -> Result<reqwest::header::HeaderValue, Failure> {
    if forbidden(value) {
        return Err(Failure::forbidden(field));
    }
    reqwest::header::HeaderValue::from_str(value)
        .map_err(|_| Failure::refused("the board header is not understood"))
}
struct Session {
    agent: Option<Text>,
    credential: Option<Text>,
    answered: bool,
}
pub struct Board {
    pub url: String,
    client: reqwest::Client,
    session: Mutex<Session>,
}
impl Board {
    async fn post(
        &self,
        action: &str,
        body: Value,
        instance: bool,
        timeout: u64,
    ) -> Result<Value, Failure> {
        let mut session = self.session.lock().await;
        let mut request = self
            .client
            .post(format!("{}/api/coordination/{action}", self.url))
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .body(body.to_string())
            .timeout(Duration::from_secs(timeout));
        if instance && let (Some(agent), Some(key)) = (&session.agent, &session.credential) {
            // Board instance headers must be ASCII before sending.
            // Registration retains the address, so cleanup fails the same way.
            if agent.chars().chain(key.chars()).any(|c| !c.is_ascii()) {
                return Err(Failure::refused("registration headers are not understood"));
            }
            request = request
                .header("X-PL-Agent", instance_header("agent_id", agent)?)
                .header("X-PL-Agent-Key", instance_header("credential", key)?);
        }
        let failure = |e: reqwest::Error| Failure {
            text: format!(
                "the daemon at {} is unreachable ({})",
                self.url,
                if e.is_timeout() && e.is_connect() {
                    "connection timed out"
                } else if e.is_timeout() {
                    "response timed out"
                } else if e.is_connect() {
                    "connection failed"
                } else {
                    "HTTP exchange failed"
                }
            ),
            transient: session.answered,
            fatal_input: false,
            code: None,
        };
        let response = request.send().await.map_err(&failure)?;
        let status = response.status().as_u16();
        let bytes = response.bytes().await.map_err(failure)?;
        session.answered = true;
        let payload = super::json::from_slice(&bytes);
        if status == 200 && payload.is_err() {
            return Err(Failure {
                text: "HTTP_REPLY_NOT_UNDERSTOOD".into(),
                fatal_input: true,
                ..Failure::refused("")
            });
        }
        decode(status, payload.ok())
    }
    pub async fn register(&self, task: &str, project: &str) -> Result<(), Failure> {
        self.register_as(task, project, "lease-run", "").await
    }
    pub async fn register_as(
        &self,
        task: &str,
        project: &str,
        label: &str,
        status: &str,
    ) -> Result<(), Failure> {
        if self.session.lock().await.agent.is_some() {
            return Ok(());
        }
        let reply=self.post("register",json!({"label":label,"project":project,"task":task,"status":status,"capabilities":json!({"resumable":false}),"wake_enabled":false}),false,10).await?;
        for field in ["agent_id", "credential"] {
            if let Value::String(value) = &reply[field]
                && forbidden(value)
            {
                return Err(Failure::forbidden(field));
            }
        }
        let (agent, key) = match (&reply["agent_id"], &reply["credential"]) {
            (Value::String(agent), Value::String(key)) if !agent.is_empty() && !key.is_empty() => {
                (agent, key)
            }
            _ => {
                return Err(Failure::refused(
                    "the daemon's registration reply carried no address",
                ));
            }
        };
        let mut session = self.session.lock().await;
        session.agent = Some(agent.to_owned());
        session.credential = Some(key.to_owned());
        Ok(())
    }
    pub async fn lease(&self, args: &super::args::Args) -> Result<Value, Failure> {
        let mut body = json!({"name":args.name.as_ref(),"ttl":args.ttl});
        if let Some(expect) = args.expect {
            body["expect"] = json!(expect);
        }
        if let Some(purpose) = args.purpose.as_ref().filter(|p| !p.is_empty()) {
            body["purpose"] = json!(purpose);
        }
        let reply = self.post("lease", body, true, 10).await?;
        if !matches!(reply["state"].as_str(), Some("held" | "queued")) {
            return Err(Failure::refused(
                "the daemon's lease reply was not understood",
            ));
        }
        Ok(reply)
    }
    pub async fn release(&self, name: &str) {
        if self.session.lock().await.agent.is_some() {
            let _ = self.post("release", json!({"name":name}), true, 5).await;
        }
    }
    pub async fn forget(&self) {
        let mut session = self.session.lock().await;
        session.agent = None;
        session.credential = None;
    }
    pub async fn agent_id(&self) -> Option<String> {
        self.session.lock().await.agent.clone()
    }
    pub async fn agents(&self) -> Result<Vec<Value>, Failure> {
        let reply = self.post("agents", json!({"limit":50}), true, 5).await?;
        let Some(agents) = reply["agents"].as_array() else {
            return Err(Failure::refused(
                "the daemon's agents reply was not understood",
            ));
        };
        Ok(agents
            .iter()
            .filter(|agent| agent.is_object() && agent["agent_id"].is_string())
            .cloned()
            .collect())
    }
    pub async fn send(&self, to: &str, text: &str, request_id: &str) -> Result<(), Failure> {
        self.post(
            "send",
            json!({"to":to,"text":text,"request_id":request_id}),
            true,
            5,
        )
        .await?;
        Ok(())
    }
    pub async fn leases(&self, name: Option<&str>) -> Result<(Vec<Value>, bool), Failure> {
        let reply = self
            .post(
                "leases",
                name.filter(|v| !v.is_empty())
                    .map_or_else(|| json!({"limit":50}), |name| json!({"name":name})),
                false,
                10,
            )
            .await?;
        let Some(leases) = reply["leases"].as_array() else {
            return Err(Failure::refused(
                "the daemon's leases reply was not understood",
            ));
        };
        Ok((
            leases
                .iter()
                .filter(|v| v.is_object() && v["name"].is_string())
                .cloned()
                .collect(),
            reply["truncated"] == true,
        ))
    }
}

fn decode(status: u16, payload: Option<Value>) -> Result<Value, Failure> {
    let code = payload
        .as_ref()
        .and_then(|p| p.get("error"))
        .and_then(Value::as_str)
        .filter(|s| {
            !s.is_empty()
                && s.len() <= 64
                && s.bytes()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_')
        });
    if status == 200 {
        let Some(payload) = payload.filter(Value::is_object) else {
            return Err(Failure::refused("the daemon's reply was not a JSON object"));
        };
        if payload["enabled"] == false {
            return Err(Failure::refused("coordination is disabled on the daemon"));
        }
        return Ok(payload);
    }
    let detail = format!(
        "HTTP {status}{}",
        code.map_or_else(String::new, |c| format!(" {c}"))
    );
    let transient = status == 429 || status >= 500 || code == Some("lease_queue_full");
    let reason = match code {
        Some("unauthorized") => "the daemon did not accept the bearer token",
        Some("authentication_required") => {
            "the daemon has no bearer authentication, which the board needs"
        }
        Some("principal_not_allowed") => "this bearer's principal is not allowed on the board",
        Some("coordination_requires_postgres") => "the daemon's board needs PostgreSQL",
        Some("unknown_coordination_action") => {
            "the daemon does not support leases (it predates them)"
        }
        Some("invalid_credential") => "the daemon did not accept this run's board address",
        Some("instance_not_found") => "the daemon no longer knows this run's board address",
        _ if status == 401 || status == 403 => "the daemon refused the bearer token",
        _ if status == 404 || status == 405 => "the daemon has no coordination API",
        _ => "the daemon did not accept the request",
    };
    Err(Failure {
        text: if transient {
            detail
        } else {
            format!("{reason} ({detail})")
        },
        transient,
        fatal_input: false,
        code: code.map(str::to_owned),
    })
}
pub fn connect(no_board: bool) -> Result<Arc<Board>, Failure> {
    if no_board {
        return Err(Failure::refused("--no-board was given"));
    }
    let url=crate::daemon_url::from_environment().map_err(|_|Failure::refused("PSEUDOLIFE_MCP_DAEMON_URL is not an http(s) origin (scheme, host and optional port only)"));
    // Environment tokens are header values, while private files may carry a
    // trailing line terminator that the credential decoder strips.
    if std::env::var_os("PSEUDOLIFE_MCP_TOKEN_FILE").is_none()
        && std::env::var("PSEUDOLIFE_MCP_TOKEN")
            .is_ok_and(|token| token.bytes().any(|c| c < 32 || c == 127))
    {
        return Err(Failure::forbidden("bearer"));
    }
    const FORBIDDEN_BEARER: &str = "HTTP_FORBIDDEN_INPUT_REFUSED: invalid bearer header";
    let unusable = |e: crate::credentials::CredentialError| {
        if e.0 == FORBIDDEN_BEARER {
            Failure::forbidden("bearer")
        } else {
            Failure::refused(format!("the bearer credential is unusable ({e})"))
        }
    };
    let snapshot = crate::credentials::CredentialProvider::from_environment()
        .and_then(|provider| {
            provider.snapshot_checked(|raw| {
                let Ok(text) = std::str::from_utf8(raw) else {
                    return Ok(());
                };
                // Strip only the decoder's CR/LF file terminator, preserving TAB
                // and every other C0 byte for forbidden-header admission.
                let token = text.trim_end_matches(['\r', '\n']);
                if token.bytes().any(|c| c < 32 || c == 127) {
                    Err(crate::credentials::CredentialError(FORBIDDEN_BEARER))
                } else {
                    Ok(())
                }
            })
        })
        .map_err(unusable);
    let snapshot = match snapshot {
        Err(failure) if failure.fatal_input => return Err(failure),
        snapshot => snapshot,
    };
    // Only forbidden admitted headers preempt URL errors. Ordinary credential
    // failures retain URL-first diagnostics, including failed file security.
    let url = url?;
    let snapshot = snapshot?;
    let Some(token) = snapshot.token() else {
        return Err(Failure::refused(
            "no bearer token (set PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKEN_FILE)",
        ));
    };
    let token = crate::credentials::decode_token(token.as_bytes()).map_err(unusable)?;
    connected(url, &token)
}
/// Hold connects only after local acquisition, matching BoardMirror._connect.
/// Its URL-first credential failures degrade mirroring and never release hold.
pub fn connect_hold(no_board: bool) -> Result<Arc<Board>, Failure> {
    if no_board {
        return Err(Failure::refused("--no-board was given"));
    }
    let url = crate::daemon_url::from_environment().map_err(|_| {
        Failure::refused("PSEUDOLIFE_MCP_DAEMON_URL is not an http(s) origin (scheme, host and optional port only)")
    })?;
    let snapshot = crate::credentials::CredentialProvider::from_environment()
        .and_then(|provider| provider.snapshot())
        .map_err(|error| {
            Failure::refused(format!("the bearer credential is unusable ({error})"))
        })?;
    let Some(token) = snapshot.token() else {
        return Err(Failure::refused(
            "no bearer token (set PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKEN_FILE)",
        ));
    };
    connected(url, token)
}
fn connected(url: String, token: &str) -> Result<Arc<Board>, Failure> {
    if !token.is_ascii() {
        return Err(Failure::refused("registration headers are not understood"));
    }
    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(
        reqwest::header::AUTHORIZATION,
        reqwest::header::HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| Failure::refused("the board header is not understood"))?,
    );
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .default_headers(headers)
        .build()
        .map_err(|_| Failure::refused("the HTTP client could not be initialized"))?;
    Ok(Arc::new(Board {
        url,
        client,
        session: Mutex::new(Session {
            agent: None,
            credential: None,
            answered: false,
        }),
    }))
}

/// The renewer owns no OS lock. Stopping it is awaited after that lock is closed.
pub struct Renewer {
    stop: tokio::sync::watch::Sender<bool>,
    task: tokio::task::JoinHandle<()>,
}
impl Renewer {
    pub fn start(board: Arc<Board>, args: Arc<super::args::Args>) -> Self {
        let (stop, mut stopped) = tokio::sync::watch::channel(false);
        let task = tokio::spawn(async move {
            let interval = Duration::from_secs_f64(args.ttl as f64 / 3.0);
            let mut delay = interval;
            let warned = AtomicBool::new(false);
            loop {
                tokio::select! { _=stopped.changed()=>break,_=tokio::time::sleep(delay)=>() }
                delay = interval;
                let why = match board.lease(&args).await {
                    Ok(reply) if reply["state"] == "held" => continue,
                    Ok(_) => "it has this run queued behind another holder".to_owned(),
                    Err(f) if f.transient => {
                        delay = interval.min(Duration::from_secs(5));
                        continue;
                    }
                    Err(f) => {
                        if !warned.swap(true, Ordering::Relaxed) {
                            super::say(&format!(
                                "lease: warning: the board no longer shows this run holding {} ({}); the command keeps running under the local lock, which is what excludes other runs",
                                super::repr(args.name.as_deref().unwrap_or("")),
                                f.text
                            ));
                        }
                        break;
                    }
                };
                if !warned.swap(true, Ordering::Relaxed) {
                    super::say(&format!(
                        "lease: warning: the board no longer shows this run holding {} ({why}); the command keeps running under the local lock, which is what excludes other runs",
                        super::repr(args.name.as_deref().unwrap_or(""))
                    ));
                }
            }
        });
        Self { stop, task }
    }
    pub async fn stop(self) {
        let _ = self.stop.send(true);
        let _ = self.task.await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn refused_and_transient_never_echo_arbitrary_error_text() {
        let failure = decode(403, Some(json!({"error":"secret\ncredential"}))).unwrap_err();
        assert_eq!(
            failure.text,
            "the daemon refused the bearer token (HTTP 403)"
        );
        assert!(
            decode(429, Some(json!({"error":"lease_queue_full"})))
                .unwrap_err()
                .transient
        );
        assert!(
            !decode(200, Some(json!({"enabled":false})))
                .unwrap_err()
                .transient
        );
    }
}
