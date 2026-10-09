//! The read-only daemon probes: `GET /api/hook/coordination-start` (board),
//! `GET /api/maintainer` and `GET /health`, answered as `board_status`,
//! `doctor_cli.maintainer_probe` and `shim.probe_health` answer them.
use super::pyenv::{Defer, Res, json_loads, strip};
use serde_json::{Map, Value, json};
use std::time::Duration;

pub(super) struct Answer {
    status: u16,
    board: Vec<Vec<u8>>,
    body: Vec<u8>,
}

/// One GET the way urllib sends it: per-operation socket timeouts, no
/// proxy, redirects followed only for the health probe.
async fn get(
    url: &str,
    path: &str,
    token: Option<&str>,
    timeout: Duration,
    follow: bool,
) -> Option<Answer> {
    let client = reqwest::Client::builder()
        .redirect(if follow {
            reqwest::redirect::Policy::limited(10)
        } else {
            reqwest::redirect::Policy::none()
        })
        .no_proxy()
        .referer(false)
        .connect_timeout(timeout)
        .read_timeout(timeout)
        .user_agent("Python-urllib/3.11")
        .build()
        .ok()?;
    let mut request = client.get(format!("{}{path}", url.trim_end_matches('/')));
    if let Some(token) = token {
        // Admitted by the preflight: Latin-1, no control characters.
        let bytes: Vec<u8> = format!("Bearer {token}")
            .chars()
            .map(|c| c as u32 as u8)
            .collect();
        request = request.header(
            "Authorization",
            reqwest::header::HeaderValue::from_bytes(&bytes).ok()?,
        );
    }
    let response = request.send().await.ok()?;
    let status = response.status().as_u16();
    let board = response
        .headers()
        .get_all("X-PL-Board")
        .iter()
        .map(|value| value.as_bytes().to_vec())
        .collect();
    let body = response.bytes().await.ok()?.to_vec();
    Some(Answer {
        status,
        board,
        body,
    })
}

fn success(status: u16) -> bool {
    (200..300).contains(&status)
}

/// `board_status.board_probe`: `{"state", "line"}`.
pub(super) async fn board(
    url: &str,
    token: Option<&str>,
    timeout: Duration,
) -> Res<Map<String, Value>> {
    let line = |state: &str, line: String| -> Map<String, Value> {
        let mut map = Map::new();
        map.insert("state".into(), json!(state));
        map.insert("line".into(), json!(line));
        map
    };
    let Some(token) = token else {
        return Ok(line("missing_bearer",
            "off - no bearer token, so the daemon has no principal to admit (open-loopback install)".into()));
    };
    let Some(answer) = get(
        url,
        "/api/hook/coordination-start",
        Some(token),
        timeout,
        false,
    )
    .await
    else {
        return Ok(line("unreachable", "off - daemon unreachable".into()));
    };
    if !success(answer.status) {
        let state = match answer.status {
            401 => "unauthorized",
            404 | 405 => "unsupported_capability",
            _ => "transport_refused",
        };
        return Ok(line(
            state,
            format!("off - board probe refused (HTTP {})", answer.status),
        ));
    }
    let header = match answer.board.as_slice() {
        [] => String::new(),
        [value] if value.is_ascii() => {
            strip(std::str::from_utf8(value).map_err(|_| Defer)?).to_owned()
        }
        _ => return Err(Defer),
    };
    let head = &answer.body[..answer.body.len().min(65536)];
    let served = head
        .iter()
        .any(|byte| !matches!(byte, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c));
    if header == "on" || (header.is_empty() && served) {
        return Ok(line("on", "on - token present, principal allowed".into()));
    }
    let reason = header
        .split_once("reason=")
        .map(|(_, rest)| strip(rest))
        .unwrap_or("");
    if let Some(text) = reason_text(reason) {
        return Ok(line(reason, format!("off - {text}")));
    }
    if !reason.is_empty() {
        return Ok(line(
            "refused",
            "off - the daemon refused the board (unrecognized reason)".into(),
        ));
    }
    Ok(line(
        "unsupported_capability",
        "off - the daemon does not serve the board to this token; update the daemon to see why"
            .into(),
    ))
}

fn reason_text(reason: &str) -> Option<&'static str> {
    Some(match reason {
        "disabled" => "coordination is disabled in the daemon's config.yaml (coordination.enabled)",
        "authentication_required" => {
            "the daemon has no bearer token configured; set PSEUDOLIFE_MCP_TOKEN in ops/.env and redeploy"
        }
        "unauthorized" => {
            "the daemon rejected this token; re-run the installer so the client token file matches ops/.env"
        }
        "principal_not_allowed" => {
            "this token's principal is not in coordination.allowed_principals; list it there, or invite this machine with `pseudolife-mcp invite <machine>` on the daemon host"
        }
        "coordination_requires_postgres" => "the board needs the PostgreSQL bank",
        "principals_unavailable" => {
            "the daemon cannot check invited machines' tokens right now (its database is not answering); try again shortly"
        }
        _ => return None,
    })
}

const MAINTAINER_FIX: &str = "Run `pseudolife-mcp maintainer setup` on the daemon host. By hand: in the daemon's config.yaml (file-only), set coordination.maintainer.rp_id to the lower-case host name the Console is served at and coordination.maintainer.origin to exactly https://<rp_id> or https://<rp_id>:<port> (for local use only, rp_id localhost with origin http://localhost:<port>). Restart the daemon, then open the Console at that origin.";
const MAINTAINER_NOTE: &str = "Maintainer authority on this host also rests on the database password and on a shell on the daemon host: either can reset the passkeys and enrol another. See the guide's \"Maintainer messages and roles from the Console\".";

/// `json.loads(body)` on bytes: BOM-aware UTF-8, `None` where Python
/// raises too.
fn loads_bytes(body: &[u8]) -> Res<Option<Value>> {
    if body.starts_with(&[0xef, 0xbb, 0xbf]) {
        return loads_bytes(&body[3..]);
    }
    if body.iter().take(4).any(|byte| *byte == 0)
        || body.starts_with(&[0xff, 0xfe])
        || body.starts_with(&[0xfe, 0xff])
    {
        return Err(Defer);
    }
    match std::str::from_utf8(body) {
        Ok(text) => json_loads(text),
        // surrogatepass would admit encoded surrogates (ED A0..BF xx).
        Err(_) if body.contains(&0xed) => Err(Defer),
        Err(_) => Ok(None),
    }
}

fn python_str(value: Option<&Value>) -> Res<String> {
    match value {
        None | Some(Value::Null) => Ok("None".into()),
        Some(Value::String(text)) => Ok(text.clone()),
        Some(_) => Err(Defer),
    }
}

/// `maintainer_probe(url, token)`.
pub(super) async fn maintainer(
    url: &str,
    token: &str,
    timeout: Duration,
) -> Res<Map<String, Value>> {
    let mut out = Map::new();
    let Some(answer) = get(url, "/api/maintainer", Some(token), timeout, false).await else {
        out.insert("state".into(), json!("not_checked"));
        out.insert("line".into(), json!("not checked - daemon unreachable"));
        return Ok(out);
    };
    let limit = if success(answer.status) {
        1 << 20
    } else {
        65536
    };
    let parsed = loads_bytes(&answer.body[..answer.body.len().min(limit)])?;
    let body = match parsed {
        Some(Value::Object(map)) => map,
        _ => Map::new(),
    };
    let status = answer.status;
    if status == 200 {
        let active = match body.get("passkeys") {
            Some(Value::Array(keys)) => keys
                .iter()
                .filter(|key| {
                    key.as_object().and_then(|k| k.get("state")) == Some(&json!("active"))
                })
                .count(),
            _ => 0,
        };
        let rp_id = body.get("rp_id").cloned().unwrap_or(Value::Null);
        let origin = body.get("origin").cloned().unwrap_or(Value::Null);
        let line = format!(
            "on - rp_id {}, origin {}, {active} active key(s)",
            python_str(Some(&rp_id))?,
            python_str(Some(&origin))?
        );
        out.insert("state".into(), json!("on"));
        out.insert("rp_id".into(), rp_id);
        out.insert("origin".into(), origin);
        out.insert("active_keys".into(), json!(active));
        out.insert("line".into(), json!(line));
        out.insert("note".into(), json!(MAINTAINER_NOTE));
        return Ok(out);
    }
    let (state, line) = if status == 404 || status == 405 {
        (
            "unsupported",
            "not available - this daemon predates maintainer passkeys".to_owned(),
        )
    } else if status == 409 && body.get("error") == Some(&json!("maintainer_https_required")) {
        match body.get("config_problem") {
            Some(Value::String(problem)) if problem == "unset" => (
                "off",
                "off - coordination.maintainer is not configured (pseudolife-mcp maintainer setup)".to_owned(),
            ),
            Some(Value::String(problem)) if !problem.is_empty() => {
                out.insert("state".into(), json!("invalid"));
                out.insert("line".into(), json!(format!("invalid - {problem}")));
                out.insert("recovery".into(), json!(MAINTAINER_FIX));
                return Ok(out);
            }
            _ => ("off_or_invalid", "off - not configured, or configured in a way the daemon refuses (this daemon does not say which; its log names the rule)".to_owned()),
        }
    } else {
        let code = match body.get("error") {
            Some(Value::String(code)) if !code.is_empty() => format!(" {code}"),
            _ => String::new(),
        };
        ("not_checked", format!("not checked - HTTP {status}{code}"))
    };
    out.insert("state".into(), json!(state));
    out.insert("line".into(), json!(line));
    Ok(out)
}

/// `shim.probe_health(url)`: the parsed body of any answer, `None` when
/// there is none or it is not JSON.
pub(super) async fn health(url: &str, timeout: Duration) -> Res<Option<Value>> {
    let Some(answer) = get(url, "/health", None, timeout, true).await else {
        return Ok(None);
    };
    if (300..400).contains(&answer.status) {
        // A redirect urllib would not follow (limit, scheme, status).
        return Err(Defer);
    }
    match std::str::from_utf8(&answer.body) {
        Ok(text) => json_loads(text),
        Err(_) => Ok(None),
    }
}
