//! Bounded native HTTP serving for maintainer sent on an initialized SQL bank.
use crate::{
    maintainer_sent, pg,
    sent_json::{self, Json},
};
use bytes::Bytes;
use hmac::{Hmac, Mac};
use http_body_util::{BodyExt, Full};
use hyper::{Request, Response, body::Incoming, service::service_fn};
use hyper_util::rt::TokioIo;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    convert::Infallible,
    env,
    io::Write,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::{net::TcpListener, sync::Mutex, task::JoinSet};

struct Config {
    token: Option<String>,
    tokens: Vec<(String, String)>,
    enabled: bool,
    allowed: Vec<String>,
    rp_id: String,
    origin: String,
    dsn: Option<pg::Dsn>,
}
impl Config {
    fn load() -> Result<Self, String> {
        let token = env::var("PSEUDOLIFE_MCP_TOKEN")
            .ok()
            .filter(|s| !s.is_empty());
        let raw_tokens = env::var("PSEUDOLIFE_MCP_TOKENS").ok();
        let mut tokens = Vec::new();
        for part in raw_tokens.as_deref().unwrap_or("").split(',') {
            if let Some((token, principal)) = part.trim().rsplit_once(':') {
                let token = token.trim();
                let principal = principal.trim().to_lowercase();
                if !token.is_empty()
                    && !principal.is_empty()
                    && principal != "default"
                    && principal != "maintainer"
                    && !tokens.iter().any(|(known, _)| known == token)
                {
                    tokens.push((token.to_owned(), principal));
                }
            }
        }
        if raw_tokens.as_ref().is_some_and(|s| !s.trim().is_empty())
            && tokens.is_empty()
            && token.is_none()
        {
            return Err("PSEUDOLIFE_MCP_TOKENS has no valid entries".into());
        }
        let path = env::var("PSEUDOLIFE_MCP_CONFIG").unwrap_or_else(|_| "config.yaml".into());
        let fields = match std::fs::read_to_string(path) {
            Ok(text) => crate::sent_config::parse(&text)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                crate::sent_config::SentConfig::default()
            }
            Err(_) => return Err("config-yaml-typed: configuration read failed".into()),
        };
        let dsn = env::var("PSEUDOLIFE_MCP_DATABASE_URL")
            .ok()
            .filter(|s| !s.is_empty())
            .map(|s| pg::Dsn::parse(&s).map_err(|e| e.to_string()))
            .transpose()?;
        Ok(Self {
            token,
            tokens,
            enabled: fields.enabled,
            allowed: fields.allowed,
            rp_id: fields.rp_id,
            origin: fields.origin,
            dsn,
        })
    }
    fn auth(&self) -> bool {
        self.token.is_some() || !self.tokens.is_empty()
    }
}

#[derive(Default)]
struct Snapshot {
    rows: Vec<(String, Option<String>, bool, bool)>,
    loaded: Option<Instant>,
}
struct State {
    config: Config,
    session: Mutex<Option<pg::Session>>,
    snapshot_session: Mutex<Option<pg::Session>>,
    snapshot: Mutex<Snapshot>,
}

fn response(status: u16, body: Vec<u8>) -> Response<Full<Bytes>> {
    Response::builder()
        .status(status)
        .header("content-type", "application/json; charset=utf-8")
        .header("content-length", body.len().to_string())
        .header("cache-control", "no-store")
        .header("x-content-type-options", "nosniff")
        .body(Full::new(Bytes::from(body)))
        .expect("fixed response")
}
fn json(status: u16, fields: Vec<(&str, Json)>) -> Response<Full<Bytes>> {
    let value = Json::Object(fields.into_iter().map(|(k, v)| (k.into(), v)).collect());
    response(
        status,
        sent_json::encode(&value).expect("fixed JSON fields"),
    )
}
fn error(status: u16, name: &str) -> Response<Full<Bytes>> {
    json(status, vec![("error", Json::String(name.into()))])
}
fn header<'a>(request: &'a Request<Incoming>, name: &str) -> Option<&'a [u8]> {
    if name == "authorization" {
        request
            .headers()
            .get_all(name)
            .iter()
            .next_back()
            .map(|v| v.as_bytes())
    } else {
        request.headers().get(name).map(|v| v.as_bytes())
    }
}
fn latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|b| char::from(*b)).collect()
}
fn host(value: &[u8]) -> String {
    let value = latin1(value).trim().to_lowercase();
    let value = value
        .split_once("://")
        .map_or(value.as_str(), |(_, after)| {
            after.split(['/', '?', '#']).next().unwrap_or(after)
        });
    if value.starts_with('[') {
        value
            .split(']')
            .next()
            .unwrap_or(value)
            .trim_start_matches('[')
            .into()
    } else if value.matches(':').count() == 1 {
        value.rsplit_once(':').unwrap().0.into()
    } else {
        value.into()
    }
}
fn same_secret(a: &[u8], b: &[u8]) -> bool {
    let mut first = Hmac::<Sha256>::new_from_slice(b"sent-auth-comparison").expect("HMAC key");
    first.update(a);
    let mut second = Hmac::<Sha256>::new_from_slice(b"sent-auth-comparison").expect("HMAC key");
    second.update(b);
    second.verify_slice(&first.finalize().into_bytes()).is_ok()
}
fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name.as_bytes()[0].is_ascii_alphanumeric()
        && name
            .bytes()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"._-".contains(&c))
}
async fn refresh(state: &State) {
    let started = Instant::now();
    let mut slot = state.snapshot_session.lock().await;
    if slot.is_none()
        && let Some(dsn) = &state.config.dsn
    {
        *slot = pg::Session::open(dsn).await.ok();
        if let Some(session) = slot.as_ref()
            && session
                .client()
                .batch_execute("SET statement_timeout = '5s'")
                .await
                .is_err()
            && let Some(session) = slot.take()
        {
            let _ = session.close().await;
        }
    }
    let Some(session) = slot.as_ref() else {
        return;
    };
    let rows = match session
        .client()
        .query(
            "SELECT principal, token_hash, board, revoked_at IS NOT NULL FROM public.principals",
            &[],
        )
        .await
    {
        Ok(rows) => rows,
        Err(e) if e.code().is_some_and(|c| c.code() == "42P01") => vec![],
        Err(_) => {
            if let Some(session) = slot.take() {
                let _ = session.close().await;
            }
            return;
        }
    };
    let mut result = BTreeMap::new();
    for row in rows {
        let Ok(name) = row.try_get::<_, String>(0) else {
            continue;
        };
        let name = name.trim().to_lowercase();
        if !valid_name(&name)
            || ["default", "daemon", "maintainer"].contains(&name.as_str())
            || state.config.tokens.iter().any(|(_, n)| n == &name)
        {
            continue;
        }
        if let (Ok(hash), Ok(board), Ok(revoked)) = (row.try_get(1), row.try_get(2), row.try_get(3))
        {
            result.insert(name.clone(), (name, hash, board, revoked));
        }
    }
    *state.snapshot.lock().await = Snapshot {
        rows: result.into_values().collect(),
        loaded: Some(started),
    };
}
async fn principal(state: &State, request: &Request<Incoming>) -> Result<Option<String>, ()> {
    let c = &state.config;
    if !c.auth() {
        return Ok(Some("default".into()));
    }
    let Some(auth) = header(request, "authorization") else {
        return Ok(None);
    };
    let Some(space) = auth.iter().position(|b| *b == b' ') else {
        return Ok(None);
    };
    let mut token = &auth[space + 1..];
    while token.first().is_some_and(|b| [b' ', b'\t'].contains(b)) {
        token = &token[1..];
    }
    while token.last().is_some_and(|b| [b' ', b'\t'].contains(b)) {
        token = &token[..token.len() - 1];
    }
    if !auth[..space].eq_ignore_ascii_case(b"bearer") || token.is_empty() {
        return Ok(None);
    }
    let decoded = latin1(token).into_bytes();
    let candidates = [decoded.as_slice(), token];
    for (known, name) in &c.tokens {
        if candidates.iter().any(|v| same_secret(v, known.as_bytes())) {
            return Ok(Some(name.clone()));
        }
    }
    if c.token
        .as_ref()
        .is_some_and(|known| candidates.iter().any(|v| same_secret(v, known.as_bytes())))
    {
        return Ok(Some("default".into()));
    }
    if c.dsn.is_none() {
        return Ok(None);
    }
    let snapshot = state.snapshot.lock().await;
    if snapshot
        .loaded
        .is_none_or(|loaded| loaded.elapsed() > Duration::from_secs(60))
    {
        return Err(());
    }
    for token in candidates {
        let hash = format!("{:x}", Sha256::digest(token));
        if let Some((name, _, _, _)) = snapshot
            .rows
            .iter()
            .find(|(_, known, _, revoked)| !revoked && known.as_ref() == Some(&hash))
        {
            return Ok(Some(name.clone()));
        }
    }
    Ok(None)
}
fn config_problem(c: &Config) -> Result<Option<String>, ()> {
    if c.rp_id.is_empty() || c.origin.is_empty() {
        return Ok(Some("unset".into()));
    }
    if c.rp_id.len() > 253
        || !c.rp_id.split('.').all(|s| {
            !s.is_empty()
                && s.len() <= 63
                && s.as_bytes()[0].is_ascii_alphanumeric()
                && s.as_bytes()[s.len() - 1].is_ascii_alphanumeric()
                && s.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        })
    {
        return Ok(Some("rp_id must be a lower-case hostname".into()));
    }
    let parsed = c
        .origin
        .trim_start_matches(|c: char| c as u32 <= 0x20)
        .replace(['\t', '\r', '\n'], "");
    let (scheme, rest) = parsed.split_once(':').unwrap_or(("", ""));
    let scheme = scheme.to_ascii_lowercase();
    let authority = rest
        .strip_prefix("//")
        .unwrap_or("")
        .split(['/', '?', '#'])
        .next()
        .unwrap_or("");
    let authority = authority.rsplit('@').next().unwrap_or(authority);
    let port_source = if authority.contains('[') || authority.contains(']') {
        let (before, rest) = authority.split_once('[').ok_or(())?;
        let (inside, after) = rest.split_once(']').ok_or(())?;
        if !before.is_empty() {
            return Err(());
        }
        let future = inside
            .strip_prefix('v')
            .and_then(|s| s.split_once('.'))
            .is_some_and(|(version, host)| {
                !version.is_empty()
                    && version.bytes().all(|b| b.is_ascii_hexdigit())
                    && !host.is_empty()
            });
        if !future && inside.parse::<std::net::Ipv6Addr>().is_err() {
            return Err(());
        }
        after
    } else {
        authority
    };
    let port = port_source
        .rsplit_once(':')
        .and_then(|(_, p)| (!p.is_empty()).then(|| p.parse::<u16>()));
    if port.as_ref().is_some_and(Result::is_err) {
        return Ok(Some("origin has an invalid port".into()));
    }
    if scheme == "http" && c.rp_id != "localhost" {
        return Ok(Some(
            "plain http is allowed for rp_id localhost only".into(),
        ));
    }
    if !["http", "https"].contains(&scheme.as_str()) {
        return Ok(Some("origin must be https".into()));
    }
    let port = port.and_then(Result::ok);
    if port == Some(if scheme == "http" { 80 } else { 443 }) {
        return Ok(Some(format!(
            "origin must leave out the default port :{}",
            port.unwrap()
        )));
    }
    let expected = format!(
        "{scheme}://{}{}",
        c.rp_id,
        port.map_or(String::new(), |p| format!(":{p}"))
    );
    Ok((c.origin != expected).then(|| "origin must be exactly <scheme>://<rp_id>[:<port>]".into()))
}
fn value(raw: Value) -> Result<Json, ()> {
    Ok(match raw {
        Value::Null => Json::Null,
        Value::Bool(v) => Json::Bool(v),
        Value::String(v) => Json::String(v),
        Value::Number(v) => {
            let text = v.to_string();
            if text.contains(['.', 'e', 'E']) {
                Json::Float(text.parse().map_err(|_| ())?)
            } else {
                Json::Integer(text)
            }
        }
        Value::Array(v) => Json::Array(v.into_iter().map(value).collect::<Result<_, _>>()?),
        Value::Object(v) => Json::Object(
            v.into_iter()
                .map(|(k, v)| value(v).map(|v| (k, v)))
                .collect::<Result<_, _>>()?,
        ),
    })
}
fn row(r: pg::Row) -> Result<maintainer_sent::Row, ()> {
    macro_rules! get {
        ($name:literal) => {
            r.try_get($name).map_err(|_| ())?
        };
    }
    let proof: Option<crate::sent_jsonb::Jsonb> = get!("maintainer_proof");
    let wake: Option<crate::sent_jsonb::Jsonb> = get!("wake");
    Ok(maintainer_sent::Row {
        message_id: get!("message_id"),
        recipient_agent_id: get!("recipient_agent_id"),
        recipient_label: get!("recipient_label"),
        created_at: get!("created_at"),
        maintainer_proof: value(proof.map_or(Value::Null, |v| v.0))?,
        text: get!("text"),
        wake: value(wake.map_or(Value::Null, |v| v.0))?,
        first_read_at: get!("first_read_at"),
        acknowledged_at: get!("acknowledged_at"),
        repudiated_at: get!("repudiated_at"),
    })
}
// Decimal zero code points from the pinned Python 3.11 Unicode database.
fn decimal(c: char) -> Option<u8> {
    const ZEROES: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x16a60, 0x16ac0,
        0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e950, 0x1fbf0,
    ];
    ZEROES.iter().find_map(|zero| {
        (c as u32)
            .checked_sub(*zero)
            .filter(|n| *n < 10)
            .map(|n| n as u8)
    })
}
fn limit(query: Option<&str>) -> i64 {
    let decode = |s: &str| {
        percent_encoding::percent_decode_str(&s.replace('+', " "))
            .decode_utf8_lossy()
            .into_owned()
    };
    let mut selected = None;
    for pair in query.unwrap_or("").split('&').filter(|p| !p.is_empty()) {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        if decode(key) == "limit" {
            selected = Some(decode(value));
        }
    }
    let Some(text) = selected else {
        return 50;
    };
    let text = text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c));
    let (negative, digits) = if let Some(s) = text.strip_prefix('-') {
        (true, s)
    } else {
        (false, text.strip_prefix('+').unwrap_or(text))
    };
    let mut count = 0;
    let mut value: i64 = 0;
    let mut previous_digit = false;
    for c in digits.chars() {
        if c == '_' && previous_digit {
            previous_digit = false;
            continue;
        }
        let Some(n) = decimal(c) else {
            return 50;
        };
        count += 1;
        value = (value * 10 + i64::from(n)).min(201);
        previous_digit = true;
    }
    // The pinned Python interpreter's decimal conversion limit is 4300 digits.
    if !previous_digit || count > 4300 {
        return 50;
    }
    if negative { 1 } else { value.clamp(1, 200) }
}
async fn handle(
    state: Arc<State>,
    mut request: Request<Incoming>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    if percent_encoding::percent_decode_str(request.uri().path()).decode_utf8_lossy()
        != "/api/maintainer/sent"
    {
        return Ok(response(501, maintainer_sent::DEFERRED_BODY.to_vec()));
    }
    let c = &state.config;
    if !c.auth() {
        for (key, code) in [("origin", "forbidden_origin"), ("host", "forbidden_host")] {
            if header(&request, key)
                .is_some_and(|h| !["localhost", "127.0.0.1", "::1"].contains(&host(h).as_str()))
            {
                return Ok(json(403,vec![("error",Json::String(code.into())),("hint",Json::String("tokenless /api serves loopback browsers only; set PSEUDOLIFE_MCP_TOKEN for remote access".into()))]));
            }
        }
    }
    let principal = match principal(&state, &request).await {
        Err(()) => return Ok(error(503, "principals_unavailable")),
        Ok(None) => {
            return Ok(json(
                401,
                vec![
                    ("error", Json::String("unauthorized".into())),
                    (
                        "hint",
                        Json::String("Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>".into()),
                    ),
                ],
            ));
        }
        Ok(Some(p)) => p,
    };
    let method = request.method().as_str().to_ascii_uppercase();
    if !["GET", "POST"].contains(&method.as_str()) {
        return Ok(error(405, "method_not_allowed"));
    }
    let count = limit(request.uri().query());
    if method == "POST" {
        let ctype = header(&request, "content-type")
            .map(latin1)
            .unwrap_or_default();
        let mut raw = vec![];
        while let Some(frame) = request.body_mut().frame().await {
            let Ok(frame) = frame else {
                return Ok(error(400, "invalid_request"));
            };
            if let Ok(data) = frame.into_data() {
                raw.extend_from_slice(&data);
            }
            if raw.len() > 256 * 1024 {
                return Ok(error(413, "request_too_large"));
            }
        }
        if !raw.is_empty() {
            if !ctype
                .split(';')
                .next()
                .unwrap_or("")
                .trim()
                .eq_ignore_ascii_case("application/json")
            {
                return Ok(error(415, "content_type_must_be_application_json"));
            }
            match serde_json::from_slice::<Value>(&raw) {
                Err(_) => return Ok(error(400, "invalid_json")),
                Ok(value) if !value.is_object() => return Ok(error(400, "body_must_be_object")),
                _ => (),
            }
        }
    }
    if !c.auth() {
        return Ok(error(401, "authentication_required"));
    }
    if method != "GET" {
        // The known maintainer route catches dispatch's wrong-verb KeyError.
        return Ok(error(400, "invalid_request"));
    }
    let snapshot = state.snapshot.lock().await;
    let admitted = c.allowed.contains(&principal)
        || snapshot
            .rows
            .iter()
            .any(|(name, _, board, revoked)| name == &principal && *board && !revoked);
    drop(snapshot);
    if ["maintainer", "daemon"].contains(&principal.as_str()) || !admitted {
        return Ok(error(403, "principal_not_allowed"));
    }
    if !c.enabled || c.dsn.is_none() {
        return Ok(error(503, "coordination_unavailable"));
    }
    let problem = match config_problem(c) {
        Ok(problem) => problem,
        Err(()) => return Ok(error(503, "coordination_unavailable")),
    };
    if let Some(problem) = problem {
        return Ok(json(
            409,
            vec![
                ("config_problem", Json::String(problem)),
                ("error", Json::String("maintainer_https_required".into())),
            ],
        ));
    }
    let mut connection = state.session.lock().await;
    if connection.is_none()
        && let Some(dsn) = &c.dsn
    {
        *connection = pg::Session::open(dsn).await.ok();
    }
    let Some(session) = connection.as_ref() else {
        return Ok(error(503, "coordination_unavailable"));
    };
    let result = session
        .client()
        .query(
            maintainer_sent::SENT_SQL,
            &[&"maintainer", &"maintainer", &count],
        )
        .await;
    if result.is_err()
        && let Some(session) = connection.take()
    {
        let _ = session.close().await;
    }
    // All recursive Values/Jsons are created and dropped on the protected
    // stack, including error paths; raw JSONB is bounded before construction.
    let result = stacker::grow(crate::sent_jsonb::STACK_BYTES, || {
        result
            .map_err(|_| ())
            .and_then(|rows| rows.into_iter().map(row).collect::<Result<Vec<_>, _>>())
            .and_then(|rows| maintainer_sent::page(rows).map_err(|_| ()))
            .and_then(|v| sent_json::encode(&v).map_err(|_| ()))
    });
    Ok(match result {
        Ok(body) => response(200, body),
        Err(()) => error(503, "coordination_unavailable"),
    })
}

pub async fn run() -> std::process::ExitCode {
    match serve().await {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            crate::stderrln!("pseudolife-stdio serve: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
async fn serve() -> Result<(), String> {
    let config = Config::load()?;
    let host = env::var("PSEUDOLIFE_MCP_HOST").unwrap_or_else(|_| "127.0.0.1".into());
    let port = env::var("PSEUDOLIFE_MCP_PORT")
        .unwrap_or_else(|_| "8765".into())
        .parse::<u16>()
        .map_err(|_| "invalid port")?;
    let trust = env::var("PSEUDOLIFE_MCP_TRUST_BIND")
        .unwrap_or_default()
        .to_lowercase();
    if !["127.0.0.1", "::1", "localhost"].contains(&host.as_str())
        && !config.auth()
        && !["1", "true", "yes", "on"].contains(&trust.as_str())
    {
        return Err("refusing a non-loopback bind without configured authentication".into());
    }
    let listener = TcpListener::bind((host.as_str(), port))
        .await
        .map_err(|_| "HTTP bind failed")?;
    let state = Arc::new(State {
        config,
        session: Mutex::new(None),
        snapshot_session: Mutex::new(None),
        snapshot: Mutex::new(Snapshot::default()),
    });
    if state.config.auth() && state.config.dsn.is_some() {
        refresh(&state).await;
    }
    let notice = serde_json::json!({"candidate":"rust-maintainer-sent","ready":true,"pid":std::process::id(),
        "nonce":env::var("PSEUDOLIFE_BASELINE_NONCE").unwrap_or_default(),"port":listener.local_addr().map_err(|_|"HTTP address failed")?.port()});
    writeln!(std::io::stdout().lock(), "{notice}").map_err(|_| "readiness write failed")?;
    std::io::stdout()
        .flush()
        .map_err(|_| "readiness flush failed")?;
    let mut connections = JoinSet::new();
    let refresh_state = state.clone();
    let refresher = tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(10));
        tick.tick().await;
        loop {
            tick.tick().await;
            if refresh_state.config.auth() && refresh_state.config.dsn.is_some() {
                refresh(&refresh_state).await;
            }
        }
    });
    loop {
        tokio::select! {
            result=listener.accept()=>{
                let (stream,_)=result.map_err(|_|"HTTP accept failed")?; let state=state.clone();
                connections.spawn(async move {let service=service_fn(move |r|handle(state.clone(),r));
                    let _=hyper::server::conn::http1::Builder::new().serve_connection(TokioIo::new(stream),service).await;});
            }
            _=connections.join_next(), if !connections.is_empty()=>(),
            _=tokio::signal::ctrl_c()=>break,
        }
    }
    refresher.abort();
    let _ = refresher.await;
    connections.abort_all();
    while connections.join_next().await.is_some() {}
    if let Some(session) = state.snapshot_session.lock().await.take() {
        session
            .close()
            .await
            .map_err(|_| "PostgreSQL snapshot shutdown failed")?;
    }
    if let Some(session) = state.session.lock().await.take() {
        session
            .close()
            .await
            .map_err(|_| "PostgreSQL shutdown failed")?;
    }
    Ok(())
}
