//! The maintainer's Console reads: `GET /api/maintainer`,
//! `/api/maintainer/sent` and `/api/maintainer/inbox` (`web/routes.py`;
//! `maintainer.py` `maintainer_status`, `maintainer_sent`,
//! `maintainer_inbox`, `_gate`, `_run`; `storage/maintainer.py`
//! `MaintainerStore` `passkeys`, `signing_keys`, `roles`, `key_changes`,
//! `sent`, `inbox`).
//!
//! The gate runs as `_gate` does, after `http.rs` has refused a tokenless
//! install (401 `authentication_required`): the caller's bearer must be an
//! admitted principal and never the reserved `maintainer`, the board must
//! be on, and `coordination.maintainer` must name a usable passkey origin
//! (`MaintainerConfig.problem`). Then the reads run under the coordination
//! lock once the storage tier is up. A refusal is `_maintainer_error`'s
//! `{"error": code}` with its status; a failure inside is
//! `coordination_unavailable`, never the database's text.

use std::collections::HashMap;

use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use crate::board::lease::{
    CONDUCTOR_EVENTS, CONDUCTOR_PREFIXES, COORDINATOR_PREFIX, conductor_project,
};
use crate::board::mail::MAINTAINER_ORIGIN;
use crate::board::store::{MAINTAINER_PRINCIPAL, listener_path};
use crate::http::{App, json_response};

/// The GET registrations this module answers.
pub(crate) const PATHS: &[&str] = &[
    "/api/maintainer",
    "/api/maintainer/sent",
    "/api/maintainer/inbox",
];

/// `MaintainerError.STATUS`.
pub(crate) fn status(code: &str) -> u16 {
    match code {
        "maintainer_https_required"
        | "maintainer_not_enrolled"
        | "enrolment_closed"
        | "role_changed" => 409,
        "challenge_expired" | "challenge_spent" => 410,
        "assertion_invalid" | "bootstrap_code_invalid" | "principal_not_allowed" => 403,
        "recipient_unknown" | "message_not_found" | "credential_not_found" | "not_found" => 404,
        "rate_capped" => 429,
        "authentication_required" | "unauthorized" => 401,
        "principals_unavailable" | "coordination_unavailable" => 503,
        _ => 400,
    }
}

pub(crate) fn refusal(code: &str, public: Option<(&str, &str)>) -> Response {
    let mut body = Map::new();
    if let Some((k, v)) = public {
        body.insert(k.into(), json!(v));
    }
    body.insert("error".into(), json!(code));
    json_response(status(code), &Value::Object(body))
}

/// `KEY_CHANGE_EVENT`, `KEY_CHANGE_LIMIT` and `KEY_CHANGE_FIELDS`.
const KEY_CHANGE_EVENT: &str = "maintainer_key";
const KEY_CHANGE_LIMIT: i64 = 30;
const KEY_CHANGE_FIELDS: [&str; 6] = ["change", "credential_id", "label", "by", "path", "revoked"];
/// `LIST_DEFAULT`, `LIST_MAX`.
const LIST_DEFAULT: i64 = 50;
const LIST_MAX: i64 = 200;
/// `MESSAGE_ORIGIN`: an inbox row is agent mail.
const MESSAGE_ORIGIN: &str = "agent";

/// `MaintainerConfig.problem`: why the maintainer routes cannot run on this
/// configuration, or `None` when they can. `rp_id` must be a lower-case
/// hostname and `origin` exactly its `https://` origin (or
/// `http://localhost[:port]`), without the scheme's default port. `Err`
/// where `urlsplit` itself raises (a malformed bracketed host), which
/// escapes `problem` and answers `coordination_unavailable`.
pub(crate) fn config_problem(rp_id: &str, origin: &str) -> Result<Option<String>, ()> {
    if rp_id.is_empty() || origin.is_empty() {
        return Ok(Some("unset".into()));
    }
    if rp_id.len() > 253 || !lower_hostname(rp_id) {
        return Ok(Some("rp_id must be a lower-case hostname".into()));
    }
    let parts = split_origin(origin)?;
    let Ok(port) = parts.port else {
        return Ok(Some("origin has an invalid port".into()));
    };
    let scheme = parts.scheme.as_str();
    if scheme == "http" && rp_id != "localhost" {
        return Ok(Some(
            "plain http is allowed for rp_id localhost only".into(),
        ));
    }
    if scheme != "https" && scheme != "http" {
        return Ok(Some("origin must be https".into()));
    }
    let default = if scheme == "https" { 443 } else { 80 };
    if port == Some(default) {
        return Ok(Some(format!(
            "origin must leave out the default port :{default}"
        )));
    }
    let expected = match port {
        Some(p) => format!("{scheme}://{rp_id}:{p}"),
        None => format!("{scheme}://{rp_id}"),
    };
    if parts.hostname.as_deref() != Some(rp_id) || origin != expected {
        return Ok(Some(
            "origin must be exactly <scheme>://<rp_id>[:<port>]".into(),
        ));
    }
    Ok(None)
}

/// `[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*`
/// as a full match.
fn lower_hostname(s: &str) -> bool {
    s.split('.').all(|label| {
        let b = label.as_bytes();
        let alnum = |c: &u8| c.is_ascii_lowercase() || c.is_ascii_digit();
        !b.is_empty()
            && b.len() <= 63
            && alnum(&b[0])
            && alnum(&b[b.len() - 1])
            && b.iter().all(|c| alnum(c) || *c == b'-')
    })
}

/// What `problem` reads of `urllib.parse.urlsplit(origin)`.
struct OriginParts {
    scheme: String,
    hostname: Option<String>,
    /// `parts.port`: `Err` where Python raises `ValueError`.
    port: Result<Option<u16>, ()>,
}

/// `urlsplit`'s scheme, netloc, `hostname` (lower-cased) and `port` for
/// the shapes an origin takes. A scheme is letters first, then letters,
/// digits, `+`, `-` or `.`, before the first `:`; a netloc follows `//`
/// up to `/`, `?` or `#`. The host and port split as `_hostinfo` splits
/// them: after the last `@`, at the first `[` (then `]`, then `:`), else at
/// the first `:`. `Err` where `urlsplit` raises: unbalanced brackets, or a
/// bracketed host that is not an IPv6 address or a `v<hex>.<text>` form.
fn split_origin(url: &str) -> Result<OriginParts, ()> {
    // urlsplit strips leading C0 controls and spaces, and drops tab, CR, LF.
    let cleaned: String = url
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut rest = cleaned.as_str();
    let mut scheme = String::new();
    if let Some(i) = rest.find(':') {
        let candidate = &rest[..i];
        let valid = candidate
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic())
            && candidate
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'));
        if valid {
            scheme = candidate.to_ascii_lowercase();
            rest = &rest[i + 1..];
        }
    }
    let mut netloc = "";
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = &after[..end];
    }
    if netloc.contains('[') != netloc.contains(']') {
        return Err(()); // "Invalid IPv6 URL"
    }
    let hostinfo = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let (host, port_text) = match hostinfo.split_once('[') {
        Some((_, bracketed)) => {
            let (h, tail) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            (h, tail.split_once(':').map(|(_, p)| p))
        }
        None => match hostinfo.split_once(':') {
            Some((h, p)) => (h, Some(p)),
            None => (hostinfo, None),
        },
    };
    if let Some((_, bracketed)) = netloc.split_once('[') {
        let inner = bracketed.split_once(']').map_or(bracketed, |(h, _)| h);
        if !bracketed_host_ok(inner) {
            return Err(());
        }
    }
    let hostname = (!host.is_empty()).then(|| host.to_lowercase());
    let port = match port_text {
        None | Some("") => Ok(None),
        Some(p) if p.bytes().all(|b| b.is_ascii_digit()) => p
            .parse::<u32>()
            .ok()
            .and_then(|n| u16::try_from(n).ok())
            .map(Some)
            .ok_or(()),
        Some(_) => Err(()),
    };
    Ok(OriginParts {
        scheme,
        hostname,
        port,
    })
}

/// `_check_bracketed_host`: `v<hex>.<anything>`, else an IPv6 address
/// (an IPv4 one in brackets is refused).
fn bracketed_host_ok(host: &str) -> bool {
    if let Some(rest) = host.strip_prefix('v') {
        return rest.split_once('.').is_some_and(|(hex, tail)| {
            !hex.is_empty() && hex.bytes().all(|b| b.is_ascii_hexdigit()) && !tail.is_empty()
        });
    }
    let address = host.split_once('%').map_or(host, |(a, _)| a);
    address.parse::<std::net::Ipv6Addr>().is_ok()
}

/// `_limit`: `int(limit)` clamped to 1..200 (the route has already taken
/// `_i(limit, 50)`).
fn list_limit(q: &HashMap<String, String>) -> i64 {
    crate::read::console::i_param(q, "limit", LIST_DEFAULT).clamp(1, LIST_MAX)
}

/// `_gate`: the caller's principal, or the refusal to answer.
pub(crate) fn gate(app: &App, h: &HeaderMap) -> Result<String, Box<Response>> {
    let headers = crate::board::http::header_dict(h);
    let principal = match crate::board::http::authenticated_principal(app, &headers) {
        Ok(p) => p,
        Err(crate::board::error::BoardError::Refused(r)) => {
            return Err(Box::new(refusal(&r.code, None)));
        }
        Err(_) => return Err(Box::new(refusal("coordination_unavailable", None))),
    };
    let cfg = &app.board.config;
    if principal == MAINTAINER_PRINCIPAL
        || (!app.board.admitted(&principal) && !crate::mutants::active("maintainer-gate-open"))
    {
        return Err(Box::new(refusal("principal_not_allowed", None)));
    }
    if !cfg.enabled {
        return Err(Box::new(refusal("coordination_unavailable", None)));
    }
    match config_problem(&cfg.maintainer_rp_id, &cfg.maintainer_origin) {
        _ if crate::mutants::active("maintainer-config-unchecked") => {}
        Ok(None) => {}
        Ok(Some(problem)) => {
            return Err(Box::new(refusal(
                "maintainer_https_required",
                Some(("config_problem", &problem)),
            )));
        }
        Err(()) => return Err(Box::new(refusal("coordination_unavailable", None))),
    }
    Ok(principal)
}

pub(crate) async fn route(
    app: &App,
    path: &str,
    q: &HashMap<String, String>,
    h: &HeaderMap,
) -> Response {
    if let Err(refused) = gate(app, h) {
        return *refused;
    }
    let cfg = &app.board.config;
    let (path, limit) = (path.to_string(), list_limit(q));
    let (rp_id, origin) = (cfg.maintainer_rp_id.clone(), cfg.maintainer_origin.clone());
    let read = app
        .board
        .with_store(move |_store, client| {
            Box::pin(async move {
                let now = crate::board::store::now();
                let out = match path.as_str() {
                    "/api/maintainer/sent" => json!({"messages": sent(client, limit).await?}),
                    "/api/maintainer/inbox" => json!({"messages": inbox(client, limit).await?}),
                    _ => maintainer_status(client, &rp_id, &origin, now).await?,
                };
                Ok(out)
            })
        })
        .await;
    match read {
        Ok(v) => json_response(200, &v),
        Err(e) => {
            // Python logs the exception type only; the text can carry row
            // data, so it is printed only when a harness asks for it.
            if std::env::var_os("PSEUDOLIFE_DAEMON_DEBUG_BOARD").is_some() {
                eprintln!("maintainer route failed ({e:?})");
            } else {
                eprintln!("maintainer route failed (internal)");
            }
            refusal("coordination_unavailable", None)
        }
    }
}

type ReadResult<T> = Result<T, crate::board::error::BoardError>;

fn db(e: tokio_postgres::Error) -> crate::board::error::BoardError {
    crate::board::error::BoardError::Internal(e.to_string())
}

/// `_status`: whether a key can sign, the configured origin, every
/// passkey, the live roles and the recent key changes.
async fn maintainer_status(c: &Client, rp_id: &str, origin: &str, now: f64) -> ReadResult<Value> {
    let rows = c
        .query(
            "SELECT credential_id, label, state, enrolled_by, active_from, created_at, \
             last_used_at, revoked_at, revoked_by, flagged_at FROM maintainer_passkeys \
             ORDER BY created_at, credential_id",
            &[],
        )
        .await
        .map_err(db)?;
    let mut passkeys = Vec::new();
    let mut available = false;
    for r in &rows {
        let state: String = r.get(2);
        let active_from: Option<f64> = r.get(4);
        // `signing_keys`: active, and past its quarantine.
        if state == "active" && active_from.is_some_and(|a| now >= a) {
            available = true;
        }
        passkeys.push(json!({
            "credential_id": r.get::<_, String>(0),
            "label": r.get::<_, String>(1),
            "state": state,
            "enrolled_by": r.get::<_, String>(3),
            "active_from": active_from,
            "created_at": r.get::<_, f64>(5),
            "last_used_at": r.get::<_, Option<f64>>(6),
            "revoked_at": r.get::<_, Option<f64>>(7),
            "revoked_by": r.get::<_, Option<String>>(8),
            "flagged_at": r.get::<_, Option<f64>>(9),
        }));
    }
    if crate::mutants::active("maintainer-available-any-key") {
        available = !rows.is_empty();
    }
    let mut out = Map::new();
    out.insert("available".into(), json!(available));
    if !available {
        out.insert("reason".into(), json!("maintainer_not_enrolled"));
    }
    out.insert("rp_id".into(), json!(rp_id));
    out.insert("origin".into(), json!(origin));
    out.insert("passkeys".into(), Value::Array(passkeys));
    out.insert("roles".into(), roles(c, now).await?);
    out.insert("key_changes".into(), key_changes(c).await?);
    Ok(Value::Object(out))
}

/// `key_changes`: the newest key-set changes the audit log keeps, skipping
/// a payload that is not a JSON object.
async fn key_changes(c: &Client) -> ReadResult<Value> {
    let rows = c
        .query(
            "SELECT created_at, principal, payload FROM coordination_events \
             WHERE event = $1 ORDER BY seq DESC LIMIT $2",
            &[&KEY_CHANGE_EVENT, &KEY_CHANGE_LIMIT],
        )
        .await
        .map_err(db)?;
    let mut out = Vec::new();
    for r in &rows {
        let payload: String = r.get(2);
        let Ok(Value::Object(p)) = serde_json::from_str::<Value>(&payload) else {
            continue;
        };
        let principal: String = r.get(1);
        let mut m = Map::new();
        m.insert("at".into(), json!(r.get::<_, f64>(0)));
        m.insert(
            "principal".into(),
            if principal.is_empty() {
                Value::Null
            } else {
                json!(principal)
            },
        );
        for k in KEY_CHANGE_FIELDS {
            m.insert(k.into(), p.get(k).cloned().unwrap_or(Value::Null));
        }
        out.push(Value::Object(m));
    }
    Ok(Value::Array(out))
}

/// `roles`: every project with a live role lease, its conductor (granted by
/// the operator or the maintainer, read from the grant's audit record for
/// the current fence) and its coordinator, each with whether maintainer
/// mail rings it now (`CoordinationStore.reachability`). A conductor hold
/// under the role's old name (`delegate:<project>`) fills the same slot; a
/// project's new-named hold wins over it.
async fn roles(c: &Client, now: f64) -> ReadResult<Value> {
    let rows = c
        .query(
            "SELECT l.name, l.holder_agent_id, l.expires_at, (SELECT e.actor FROM \
             coordination_events e WHERE e.event=ANY($5) \
             AND e.agent_id=l.holder_agent_id AND CASE WHEN e.event=ANY($5) \
             THEN e.payload::jsonb->>'name'=l.name \
             AND (e.payload::jsonb->>'fence')::bigint=l.fence ELSE false END \
             ORDER BY e.seq DESC LIMIT 1) AS granted_by \
             FROM coordination_leases l WHERE l.holder_agent_id IS NOT NULL \
             AND l.expires_at > $1 AND (l.name LIKE $2 OR l.name LIKE $3 OR l.name LIKE $4) \
             ORDER BY l.name",
            &[
                &now,
                &format!("{}%", CONDUCTOR_PREFIXES[0]),
                &format!("{}%", CONDUCTOR_PREFIXES[1]),
                &format!("{COORDINATOR_PREFIX}%"),
                &CONDUCTOR_EVENTS
                    .iter()
                    .map(|e| e.to_string())
                    .collect::<Vec<_>>(),
            ],
        )
        .await
        .map_err(db)?;
    let mut out = Map::new();
    for r in &rows {
        let name: String = r.get(0);
        let holder: String = r.get(1);
        let expires_at: Option<f64> = r.get(2);
        let granted_by: Option<String> = r.get(3);
        let agent = c
            .query_opt(
                "SELECT * FROM coordination_agents WHERE agent_id = $1",
                &[&holder],
            )
            .await
            .map_err(db)?
            .map(|row| crate::board::db::AgentRow::from_row(&row));
        // `reachability(row or None, now)`: a holder with no board row has
        // no wake path.
        let reason = match &agent {
            None => Some("wake_disabled"),
            Some(a) => listener_path(a, now),
        };
        let mut entry = Map::new();
        entry.insert("agent_id".into(), json!(holder));
        entry.insert("expires_at".into(), json!(expires_at));
        let (slot, project) = if let Some(project) = conductor_project(&name) {
            if !matches!(granted_by.as_deref(), Some("operator" | "maintainer"))
                && !crate::mutants::active("maintainer-roles-any-grant")
            {
                continue;
            }
            // `conductor:` sorts before `delegate:`, so a project's
            // old-named hold never replaces its new-named one.
            if out
                .get(project)
                .is_some_and(|p: &Value| !p["conductor"].is_null())
            {
                continue;
            }
            entry.insert("granted_by".into(), json!(granted_by));
            ("conductor", project)
        } else {
            (
                "coordinator",
                name.strip_prefix(COORDINATOR_PREFIX).unwrap_or(&name),
            )
        };
        entry.insert("reachable".into(), json!(reason.is_none()));
        entry.insert("reason".into(), json!(reason));
        let project_entry = out
            .entry(project.to_string())
            .or_insert_with(|| json!({"conductor": null, "coordinator": null}));
        project_entry[slot] = Value::Object(entry);
    }
    Ok(Value::Object(out))
}

/// `sent`: every maintainer message still on the board, newest first, with
/// its wake receipt.
async fn sent(c: &Client, limit: i64) -> ReadResult<Vec<Value>> {
    let rows = c
        .query(
            "SELECT m.message_id, m.recipient_agent_id, a.label AS recipient_label, \
             m.created_at, m.maintainer_proof, m.text, m.wake, m.first_read_at, \
             m.acknowledged_at, m.repudiated_at FROM coordination_messages m \
             LEFT JOIN coordination_agents a ON a.agent_id = m.recipient_agent_id \
             WHERE m.origin = $1 AND m.sender_principal = $2 \
             ORDER BY m.created_at DESC, m.message_id LIMIT $3",
            &[&MAINTAINER_ORIGIN, &MAINTAINER_PRINCIPAL, &limit],
        )
        .await
        .map_err(db)?;
    rows.iter()
        .map(|r| {
            let proof: Option<Value> = r.get(4);
            // `(maintainer_proof or {}).get("label")`: a truthy proof that
            // is not an object raises (coordination_unavailable).
            let label = match proof {
                Some(Value::Object(p)) => p.get("label").cloned().unwrap_or(Value::Null),
                Some(p) if crate::board::store::py_truthy(&p) => {
                    return Err(crate::board::error::BoardError::Internal(
                        "maintainer_proof is not an object".into(),
                    ));
                }
                _ => Value::Null,
            };
            Ok(json!({
                "message_id": r.get::<_, String>(0),
                "recipient_agent_id": r.get::<_, String>(1),
                "recipient_label": r.get::<_, Option<String>>(2),
                "created_at": r.get::<_, f64>(3),
                "label": label,
                "text": r.get::<_, Option<String>>(5),
                "wake": r.get::<_, Option<Value>>(6),
                "first_read_at": r.get::<_, Option<f64>>(7),
                "acknowledged_at": r.get::<_, Option<f64>>(8),
                "repudiated_at": r.get::<_, Option<f64>>(9),
            }))
        })
        .collect()
}

/// `inbox`: replies to the maintainer (mail to its board rows), newest
/// first.
async fn inbox(c: &Client, limit: i64) -> ReadResult<Vec<Value>> {
    let rows = c
        .query(
            "SELECT m.message_id, m.sender_agent_id, s.label AS sender_label, \
             m.sender_principal, m.reply_to, m.text, m.created_at, m.acknowledged_at \
             FROM coordination_messages m \
             JOIN coordination_agents r ON r.agent_id = m.recipient_agent_id \
             LEFT JOIN coordination_agents s ON s.agent_id = m.sender_agent_id \
             WHERE r.principal = $1 ORDER BY m.created_at DESC, m.message_id LIMIT $2",
            &[&MAINTAINER_PRINCIPAL, &limit],
        )
        .await
        .map_err(db)?;
    Ok(rows
        .iter()
        .map(|r| {
            json!({
                "message_id": r.get::<_, String>(0),
                "sender_agent_id": r.get::<_, String>(1),
                "sender_label": r.get::<_, Option<String>>(2),
                "sender_principal": r.get::<_, String>(3),
                "reply_to": r.get::<_, Option<String>>(4),
                "text": r.get::<_, Option<String>>(5),
                "created_at": r.get::<_, f64>(6),
                "acknowledged_at": r.get::<_, Option<f64>>(7),
                "origin": MESSAGE_ORIGIN,
            })
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_problem_follows_python() {
        let cases: [(&str, &str, Option<&str>); 13] = [
            ("", "", Some("unset")),
            ("console.example.com", "", Some("unset")),
            (
                "Console.example.com",
                "https://Console.example.com",
                Some("rp_id must be a lower-case hostname"),
            ),
            (
                "-bad.example.com",
                "https://-bad.example.com",
                Some("rp_id must be a lower-case hostname"),
            ),
            (
                "console.example.com",
                "https://console.example.com:x",
                Some("origin has an invalid port"),
            ),
            (
                "console.example.com",
                "https://console.example.com:70000",
                Some("origin has an invalid port"),
            ),
            (
                "console.example.com",
                "http://console.example.com",
                Some("plain http is allowed for rp_id localhost only"),
            ),
            (
                "console.example.com",
                "ftp://console.example.com",
                Some("origin must be https"),
            ),
            (
                "console.example.com",
                "https://console.example.com:443",
                Some("origin must leave out the default port :443"),
            ),
            (
                "console.example.com",
                "https://console.example.com/",
                Some("origin must be exactly <scheme>://<rp_id>[:<port>]"),
            ),
            (
                "console.example.com",
                "https://other.example.com",
                Some("origin must be exactly <scheme>://<rp_id>[:<port>]"),
            ),
            // The near-miss legitimate configurations.
            (
                "console.example.com",
                "https://console.example.com:8443",
                None,
            ),
            ("localhost", "http://localhost:8765", None),
        ];
        for (rp, origin, want) in cases {
            assert_eq!(
                config_problem(rp, origin).unwrap().as_deref(),
                want,
                "{rp} {origin}"
            );
        }
        assert_eq!(
            config_problem("console.example.com", "https://console.example.com"),
            Ok(None)
        );
        // `_hostinfo` splits the port at the first `:`.
        for origin in [
            "https://console.example.com:8443:",
            "https://console.example.com:1:2",
            "https://console.example.com::8443",
        ] {
            assert_eq!(
                config_problem("console.example.com", origin),
                Ok(Some("origin has an invalid port".into())),
                "{origin}"
            );
        }
        // Where urlsplit raises: unbalanced or invalid brackets.
        for origin in [
            "https://[::1",
            "https://x]",
            "https://[notip]",
            "https://[10.0.0.1]",
        ] {
            assert_eq!(
                config_problem("console.example.com", origin),
                Err(()),
                "{origin}"
            );
        }
        assert_eq!(
            config_problem("console.example.com", "https://[::1]:8443"),
            Ok(Some(
                "origin must be exactly <scheme>://<rp_id>[:<port>]".into()
            ))
        );
    }

    #[test]
    fn list_limit_clamps_like_python() {
        let q = |v: &str| -> HashMap<String, String> {
            [("limit".to_string(), v.to_string())].into_iter().collect()
        };
        assert_eq!(list_limit(&HashMap::new()), 50);
        assert_eq!(list_limit(&q("0")), 1);
        assert_eq!(list_limit(&q("-5")), 1);
        assert_eq!(list_limit(&q("999")), 200);
        assert_eq!(list_limit(&q("x")), 50);
        assert_eq!(list_limit(&q("7")), 7);
        assert_eq!(list_limit(&q("99999999999999999999")), 200);
        assert_eq!(list_limit(&q("-99999999999999999999")), 1);
    }
}
