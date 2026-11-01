//! The maintainer's Console actions (`maintainer.py` `MaintainerOps`,
//! `storage/maintainer.py` `MaintainerStore`). Served so far:
//! `POST /api/maintainer/challenge` (`_challenge`), which issues the signed
//! payload a passkey prompt covers.
//!
//! The gate is the reads' (`read::maintainer::gate`). The handler runs on
//! the board connection under its lock, as `_run` runs it under the
//! coordination lock. A refusal is `{"error": code}` with
//! `MaintainerError.STATUS`; anything unexpected is
//! `coordination_unavailable`, never the database's text.

use axum::http::HeaderMap;
use axum::response::Response;
use hmac::{Hmac, Mac};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tokio_postgres::GenericClient;

use crate::board::error::BoardError;
use crate::board::lease::{
    CONDUCTOR_PREFIX, CONDUCTOR_PREFIXES, COORDINATOR_PREFIX, conductor_project,
};
use crate::board::mail::{MAINTAINER_ORIGIN, MAX_TEXT_BYTES};
use crate::board::store::{ATTACHED_IDLE_WINDOW, MAINTAINER_PRINCIPAL, RESERVED_SENDERS};
use crate::http::{App, json_response};
use crate::read::maintainer::{gate, refusal};

/// The POST registrations this module answers.
pub(crate) const PATHS: &[&str] = &[
    "/api/maintainer/challenge",
    "/api/maintainer/enrol",
    "/api/maintainer/role",
    "/api/maintainer/send",
    "/api/maintainer/cancel",
    "/api/maintainer/repudiate",
    "/api/maintainer/revoke",
];
mod actions;
mod mail;
mod roles;

/// `CHALLENGE_TTL` (seconds).
const CHALLENGE_TTL: i64 = 120;
/// `SECRET_META_KEY`.
const SECRET_META_KEY: &str = "maintainer_secret_v1";
/// `MAX_PASSKEY_LABEL`.
const MAX_PASSKEY_LABEL: usize = 64;
/// `DELEGATION_MIN`, `DELEGATION_MAX` (seconds).
const DELEGATION_MIN: i64 = 60;
const DELEGATION_MAX: i64 = 7 * 86400;
/// `SUPPORTED_ALGS`: ES256, EdDSA, RS256.
const SUPPORTED_ALGS: [i64; 3] = [-7, -8, -257];
/// `ROLE_PURPOSES`.
const ROLE_PURPOSES: [&str; 4] = [
    "grant-conductor",
    "revoke-conductor",
    "assign-coordinator",
    "revoke-coordinator",
];
/// `LEGACY_ROLE_PURPOSES`: the conductor's purposes until 2026-10-31. A
/// Console tab loaded before the upgrade asks for one and refuses a payload
/// whose purpose differs from its request, so the challenge is issued under
/// the requested name (and previews the role as `delegate`); everything
/// else reads it as the new one.
const LEGACY_ROLE_PURPOSES: [(&str, &str); 2] = [
    ("grant-delegate", "grant-conductor"),
    ("revoke-delegate", "revoke-conductor"),
];

/// `_CHALLENGE_FIELDS`: each purpose's fields besides `purpose`, in
/// `CHALLENGE_PURPOSES` order.
const CHALLENGE_FIELDS: [(&str, &[&str]); 10] = [
    ("send", &["to", "text", "urgent"]),
    ("enrol-approve", &["label"]),
    ("enrol-bootstrap", &["label"]),
    ("cancel", &["credential_id"]),
    ("revoke-self", &["credential_id"]),
    ("repudiate", &["message_id"]),
    ("grant-conductor", &["project", "agent_id", "hold"]),
    ("assign-coordinator", &["project", "agent_id", "hold"]),
    ("revoke-conductor", &["project"]),
    ("revoke-coordinator", &["project"]),
];

/// A `MaintainerError`: its public code (`check` goes to the log only).
#[derive(Debug)]
enum Refusal {
    Code(&'static str, &'static str),
    Board(BoardError),
}

impl From<BoardError> for Refusal {
    fn from(e: BoardError) -> Self {
        Refusal::Board(e)
    }
}

impl From<tokio_postgres::Error> for Refusal {
    fn from(e: tokio_postgres::Error) -> Self {
        Refusal::Board(BoardError::Internal(e.to_string()))
    }
}

fn refuse(code: &'static str, check: &'static str) -> Refusal {
    Refusal::Code(code, check)
}

type Outcome<T> = Result<T, Refusal>;

pub(crate) async fn route(
    app: &App,
    path: &str,
    h: &HeaderMap,
    body: Map<String, Value>,
) -> Response {
    if let Err(refused) = gate(app, h) {
        return *refused;
    }
    if path != "/api/maintainer/challenge" {
        return action_route(app, path, h, body).await;
    }
    let cfg = &app.board.config;
    let rp_id = cfg.maintainer_rp_id.clone();
    let answer = app
        .board
        .with_store(move |_store, client| {
            Box::pin(async move {
                match challenge(client, &rp_id, &body, crate::board::store::now()).await {
                    Ok(v) => Ok(v),
                    Err(Refusal::Code(code, check)) => {
                        eprintln!("maintainer route refused: {code} ({check})");
                        Ok(json!({"_refused": code}))
                    }
                    Err(Refusal::Board(e)) => Err(e),
                }
            })
        })
        .await;
    match answer {
        Ok(v) => match v.get("_refused").and_then(Value::as_str) {
            Some(code) => refusal(code, None),
            None => json_response(200, &v),
        },
        Err(e) => {
            if std::env::var_os("PSEUDOLIFE_DAEMON_DEBUG_BOARD").is_some() {
                eprintln!("maintainer route failed ({e:?})");
            } else {
                eprintln!("maintainer route failed (internal)");
            }
            refusal("coordination_unavailable", None)
        }
    }
}

// ── field checks ───────────────────────────────────────────────────────────

/// `unicodedata.category(c)[0] == "Z"`: the space separators and the line
/// and paragraph separators, which are the Unicode white space outside
/// category C.
fn category_z(c: char) -> bool {
    c.is_whitespace() && !crate::board::text::category_c(c)
}

/// `check_label`: NFC, stripped, 1..64 code points, nothing in categories C
/// or Z but the plain space.
fn check_label(label: Option<&Value>) -> Outcome<String> {
    use unicode_normalization::UnicodeNormalization;
    let Some(Value::String(label)) = label else {
        return Err(refuse("invalid_request", "label"));
    };
    let nfc: String = label.nfc().collect();
    let label = crate::board::text::py_strip(&nfc);
    let n = label.chars().count();
    if !(1..=MAX_PASSKEY_LABEL).contains(&n)
        || label
            .chars()
            .any(|c| c != ' ' && (crate::board::text::category_c(c) || category_z(c)))
    {
        return Err(refuse("invalid_request", "label"));
    }
    Ok(label.to_string())
}

/// `_send_text`.
fn send_text(text: Option<&Value>) -> Outcome<String> {
    let Some(Value::String(text)) = text else {
        return Err(refuse("invalid_request", "text"));
    };
    let ok = !crate::board::text::py_strip(text).is_empty()
        && !text.contains('\0')
        && text.len() <= MAX_TEXT_BYTES;
    // `looks_like_secret`, the board's credential-shape guard.
    if !ok || crate::board::store::refuse_secret(text, "text").is_err() {
        return Err(refuse("invalid_request", "text"));
    }
    Ok(text.clone())
}

/// `_project`.
fn project(value: Option<&Value>) -> Outcome<String> {
    match value {
        Some(Value::String(p))
            if !p.is_empty()
                && p == crate::board::text::py_strip(p)
                && p.chars().count() <= 108 =>
        {
            Ok(p.clone())
        }
        _ => Err(refuse("invalid_request", "project")),
    }
}

/// `_hold`: an int (never a bool or float) within the delegation bounds.
fn hold(value: Option<&Value>) -> Outcome<i64> {
    match value {
        Some(Value::Number(n)) if !n.to_string().contains(['.', 'e', 'E']) => {
            match n.to_string().parse::<i64>() {
                Ok(h) if (DELEGATION_MIN..=DELEGATION_MAX).contains(&h) => Ok(h),
                _ => Err(refuse("invalid_request", "hold")),
            }
        }
        _ => Err(refuse("invalid_request", "hold")),
    }
}

// ── store reads ────────────────────────────────────────────────────────────

/// `live_keys`: pending or active passkeys.
async fn live_keys(c: &impl GenericClient) -> Outcome<i64> {
    let row = c
        .query_one(
            "SELECT count(*) FROM maintainer_passkeys WHERE state IN ('pending','active')",
            &[],
        )
        .await?;
    Ok(row.get(0))
}

/// `signing_keys`' credential ids: active and past their quarantine.
async fn signing_keys(c: &impl GenericClient, now: f64) -> Outcome<Vec<String>> {
    let rows = c
        .query(
            "SELECT credential_id, active_from FROM maintainer_passkeys WHERE state='active' \
             ORDER BY created_at, credential_id",
            &[],
        )
        .await?;
    Ok(rows
        .iter()
        .filter(|r| r.get::<_, Option<f64>>(1).is_some_and(|a| now >= a))
        .map(|r| r.get(0))
        .collect())
}

/// Every passkey not revoked, for `excludeCredentials`.
async fn unrevoked_keys(c: &impl GenericClient) -> Outcome<Vec<String>> {
    let rows = c
        .query(
            "SELECT credential_id, state FROM maintainer_passkeys \
             ORDER BY created_at, credential_id",
            &[],
        )
        .await?;
    Ok(rows
        .iter()
        .filter(|r| r.get::<_, String>(1) != "revoked")
        .map(|r| r.get(0))
        .collect())
}

/// `recipient_row`'s fields a challenge reads.
struct Recipient {
    principal: String,
    label: String,
    project: String,
    task: String,
    last_activity: f64,
    capabilities: Value,
}

/// `recipient_row`: a full agent id, registered and not revoked, and a
/// session's own address (never a reserved row or a subagent's).
async fn recipient_row(
    c: &impl GenericClient,
    agent_id: Option<&Value>,
) -> Outcome<(String, Recipient)> {
    let Some(Value::String(id)) = agent_id else {
        return Err(refuse("recipient_unknown", "agent_id"));
    };
    if id.len() != 32 || !id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
        return Err(refuse("recipient_unknown", "agent_id"));
    }
    let row = c
        .query_opt(
            "SELECT principal, parent_thread, label, project, task, last_activity, capabilities \
             FROM coordination_agents WHERE agent_id=$1 AND credential_hash IS NOT NULL",
            &[id],
        )
        .await?;
    let Some(row) = row else {
        return Err(refuse("recipient_unknown", "agent_id"));
    };
    let principal: String = row.get(0);
    if RESERVED_SENDERS.contains(&principal.as_str()) || row.get::<_, Option<String>>(1).is_some() {
        return Err(refuse("recipient_reserved", "agent_id"));
    }
    Ok((
        id.clone(),
        Recipient {
            principal,
            label: row.get(2),
            project: row.get(3),
            task: row.get(4),
            last_activity: row.get(5),
            capabilities: row.get(6),
        },
    ))
}

/// `recipient`: the preview a challenge carries.
async fn recipient(
    c: &impl GenericClient,
    agent_id: Option<&Value>,
    now: f64,
) -> Outcome<Map<String, Value>> {
    let (id, row) = recipient_row(c, agent_id).await?;
    let duplicate = !row.label.is_empty()
        && c.query_opt(
            "SELECT 1 FROM coordination_agents WHERE agent_id<>$1 \
             AND credential_hash IS NOT NULL AND last_activity>$2 AND label=$3 LIMIT 1",
            &[&id, &(now - ATTACHED_IDLE_WINDOW), &row.label],
        )
        .await?
        .is_some();
    let caps = row.capabilities.as_object();
    let host = caps
        .and_then(|m| m.get("host"))
        .and_then(Value::as_str)
        .map_or(Value::Null, |h| json!(h));
    let client = if caps.is_some_and(|m| m.contains_key("codex")) {
        "codex"
    } else {
        "claude"
    };
    let mut out = Map::new();
    out.insert("agent_id_prefix".into(), json!(&id[..12]));
    out.insert("name".into(), json!(row.label));
    out.insert("label".into(), json!(row.label));
    out.insert("principal".into(), json!(row.principal));
    out.insert("host".into(), host);
    out.insert("client".into(), json!(client));
    out.insert("project".into(), json!(row.project));
    out.insert("task".into(), json!(row.task));
    out.insert("last_activity".into(), json!(row.last_activity));
    out.insert("duplicate_name".into(), json!(duplicate));
    Ok(out)
}

/// `role_holder`: the live holder of a role lease; for
/// `conductor:<project>` a hold under the role's old name
/// (`delegate:<project>`) counts too, the new name first.
async fn role_holder(c: &impl GenericClient, name: &str, now: f64) -> Outcome<Value> {
    let names: Vec<String> = match conductor_project(name) {
        Some(project) if name.starts_with(CONDUCTOR_PREFIX) => CONDUCTOR_PREFIXES
            .iter()
            .map(|prefix| format!("{prefix}{project}"))
            .collect(),
        _ => vec![name.to_string()],
    };
    let row = c
        .query_opt(
            "SELECT holder_agent_id FROM coordination_leases WHERE name=ANY($1) \
             AND holder_agent_id IS NOT NULL AND expires_at>$2 ORDER BY name LIMIT 1",
            &[&names, &now],
        )
        .await?;
    Ok(row.map_or(Value::Null, |r| json!(r.get::<_, String>(0))))
}

/// `is_sent`: a maintainer message still on the board.
async fn is_sent(c: &impl GenericClient, message_id: Option<&Value>) -> Outcome<bool> {
    let Some(Value::String(id)) = message_id else {
        return Ok(false);
    };
    Ok(c.query_opt(
        "SELECT 1 FROM coordination_messages WHERE message_id=$1 AND origin=$2 \
             AND sender_principal=$3",
        &[id, &MAINTAINER_ORIGIN, &MAINTAINER_PRINCIPAL],
    )
    .await?
    .is_some())
}

// ── payloads ───────────────────────────────────────────────────────────────

/// `_secret`: the MAC key, inserted the first time it is needed.
async fn secret(c: &impl GenericClient) -> Outcome<Vec<u8>> {
    let mut fresh = [0u8; 32];
    getrandom::fill(&mut fresh).map_err(|_| refuse("coordination_unavailable", "randomness"))?;
    c.execute(
        "INSERT INTO meta (key,value) VALUES ($1,$2) ON CONFLICT (key) DO NOTHING",
        &[&SECRET_META_KEY, &json!(hex::encode(fresh))],
    )
    .await?;
    let row = c
        .query_opt("SELECT value FROM meta WHERE key=$1", &[&SECRET_META_KEY])
        .await?;
    match row.map(|r| r.get::<_, Value>(0)) {
        Some(Value::String(v))
            if v.len() == 64 && v.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) =>
        {
            Ok(hex::decode(v).expect("hex"))
        }
        _ => Err(refuse("coordination_unavailable", "secret")),
    }
}

/// `issue`: the canonical payload for `purpose` carrying `fields`, and its
/// MAC (`HMAC-SHA256` with the bank's secret, lower-case hex).
async fn issue(
    c: &impl GenericClient,
    purpose: &str,
    mut fields: Map<String, Value>,
    now: f64,
) -> Outcome<(String, String)> {
    let mut nonce = [0u8; 16];
    getrandom::fill(&mut nonce).map_err(|_| refuse("coordination_unavailable", "randomness"))?;
    fields.insert("purpose".into(), json!(purpose));
    fields.insert("nonce".into(), json!(hex::encode(nonce)));
    fields.insert(
        "expires_at".into(),
        json!(crate::read::stats::py_round(now + CHALLENGE_TTL as f64, 3)),
    );
    let payload = crate::pyjson::dumps_sorted_compact(&Value::Object(fields));
    let mut mac = Hmac::<Sha256>::new_from_slice(&secret(c).await?).expect("any key length");
    mac.update(payload.as_bytes());
    Ok((payload, hex::encode(mac.finalize().into_bytes())))
}

/// `b64url_encode(challenge_bytes(payload))`.
fn challenge_b64(payload: &str) -> String {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(Sha256::digest(payload.as_bytes()))
}

fn credentials(ids: &[String]) -> Value {
    Value::Array(
        ids.iter()
            .map(|id| json!({"type": "public-key", "id": id}))
            .collect(),
    )
}

/// `_get_options`.
fn get_options(rp_id: &str, payload: &str, signing: &[String]) -> Value {
    json!({
        "challenge": challenge_b64(payload),
        "rpId": rp_id,
        "userVerification": "required",
        "timeout": CHALLENGE_TTL * 1000,
        "allowCredentials": credentials(signing),
    })
}

/// `_create_options`.
fn create_options(rp_id: &str, payload: &str, unrevoked: &[String]) -> Value {
    use base64::Engine;
    let user_id = Sha256::digest(format!("pseudolife-maintainer:{rp_id}").as_bytes());
    json!({
        "challenge": challenge_b64(payload),
        "rp": {"id": rp_id, "name": "Pseudolife maintainer"},
        "user": {
            "id": base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(&user_id[..16]),
            "name": "maintainer",
            "displayName": "Pseudolife maintainer",
        },
        "pubKeyCredParams": SUPPORTED_ALGS
            .iter()
            .map(|alg| json!({"type": "public-key", "alg": alg}))
            .collect::<Vec<_>>(),
        "authenticatorSelection": {"userVerification": "required", "residentKey": "preferred"},
        "attestation": "none",
        "timeout": CHALLENGE_TTL * 1000,
        "excludeCredentials": credentials(unrevoked),
    })
}

// ── the handler ────────────────────────────────────────────────────────────

/// `_role_preview`.
async fn role_preview(
    c: &impl GenericClient,
    purpose: &str,
    fields: &Map<String, Value>,
    now: f64,
) -> Outcome<Map<String, Value>> {
    let conductor = purpose.ends_with("conductor");
    let (role, prefix, other) = if conductor {
        ("conductor", CONDUCTOR_PREFIX, COORDINATOR_PREFIX)
    } else {
        ("coordinator", COORDINATOR_PREFIX, CONDUCTOR_PREFIX)
    };
    let project = fields["project"].as_str().expect("checked project");
    let holder = role_holder(c, &format!("{prefix}{project}"), now).await?;
    let action = if purpose.starts_with("revoke") {
        "revoke"
    } else if conductor {
        "grant"
    } else {
        "assign"
    };
    let mut preview = Map::new();
    preview.insert("role".into(), json!(role));
    preview.insert("project".into(), json!(project));
    preview.insert("action".into(), json!(action));
    preview.insert("current_holder".into(), holder.clone());
    if let Some(agent_id) = fields.get("agent_id") {
        // `preview.update(store.recipient(...))`: `project` keeps its place.
        for (k, v) in recipient(c, Some(agent_id), now).await? {
            preview.insert(k, v);
        }
        let replaces = if holder != *agent_id {
            holder
        } else {
            Value::Null
        };
        preview.insert("replaces".into(), replaces);
        let other_holder = role_holder(c, &format!("{other}{project}"), now).await?;
        let breaks = if other_holder == *agent_id {
            json!(format!("{other}{project}"))
        } else {
            Value::Null
        };
        preview.insert("also_breaks".into(), breaks);
    }
    Ok(preview)
}

/// `_challenge`.
async fn challenge(
    c: &impl GenericClient,
    rp_id: &str,
    body: &Map<String, Value>,
    now: f64,
) -> Outcome<Value> {
    let legacy = match body.get("purpose") {
        Some(Value::String(p)) => LEGACY_ROLE_PURPOSES.iter().find(|(old, _)| old == p),
        _ => None,
    };
    let purpose = match body.get("purpose") {
        Some(Value::String(p)) => {
            let p = legacy.map_or(p.as_str(), |(_, new)| new);
            CHALLENGE_FIELDS.iter().find(|(name, _)| *name == p)
        }
        _ => None,
    };
    let Some(&(purpose, allowed)) = purpose else {
        return Err(refuse("invalid_request", "purpose"));
    };
    if body
        .keys()
        .any(|k| k != "purpose" && !allowed.contains(&k.as_str()))
        && !crate::mutants::active("maintainer-challenge-any-field")
    {
        return Err(refuse("invalid_request", "parameters"));
    }
    if purpose == "enrol-bootstrap" {
        if live_keys(c).await? > 0 {
            return Err(refuse("enrolment_closed", "live_keys"));
        }
        let mut fields = Map::new();
        fields.insert("label".into(), json!(check_label(body.get("label"))?));
        let (payload, mac) = issue(c, purpose, fields, now).await?;
        let options = create_options(rp_id, &payload, &unrevoked_keys(c).await?);
        return Ok(json!({"payload": payload, "mac": mac, "publicKey": options}));
    }
    if signing_keys(c, now).await?.is_empty() {
        return Err(refuse("maintainer_not_enrolled", "signing_keys"));
    }
    let mut preview: Option<Map<String, Value>> = None;
    let mut fields = Map::new();
    match purpose {
        "send" => {
            let urgent = match body.get("urgent") {
                None => false,
                Some(Value::Bool(b)) => *b,
                Some(_) => return Err(refuse("invalid_request", "urgent")),
            };
            preview = Some(recipient(c, body.get("to"), now).await?);
            fields.insert("to".into(), body["to"].clone());
            fields.insert("text".into(), json!(send_text(body.get("text"))?));
            fields.insert("urgent".into(), json!(urgent));
        }
        "enrol-approve" => {
            fields.insert("label".into(), json!(check_label(body.get("label"))?));
        }
        "cancel" | "revoke-self" => match body.get("credential_id") {
            Some(Value::String(cid)) if !cid.is_empty() && cid.chars().count() <= 1400 => {
                fields.insert("credential_id".into(), json!(cid));
            }
            _ => return Err(refuse("invalid_request", "credential_id")),
        },
        "repudiate" => {
            if !is_sent(c, body.get("message_id")).await? {
                return Err(refuse("message_not_found", "message_id"));
            }
            fields.insert("message_id".into(), body["message_id"].clone());
        }
        "grant-conductor" | "assign-coordinator" => {
            fields.insert("project".into(), json!(project(body.get("project"))?));
            fields.insert(
                "agent_id".into(),
                body.get("agent_id").cloned().unwrap_or(Value::Null),
            );
            fields.insert("hold".into(), json!(hold(body.get("hold"))?));
            // `_role_recipient`: the session's own address, on exactly the
            // payload's project.
            let (_, row) = recipient_row(c, fields.get("agent_id")).await?;
            if Some(row.project.as_str()) != fields["project"].as_str() {
                return Err(refuse("invalid_request", "project_mismatch"));
            }
            preview = Some(role_preview(c, purpose, &fields, now).await?);
        }
        _ => {
            fields.insert("project".into(), json!(project(body.get("project"))?));
            preview = Some(role_preview(c, purpose, &fields, now).await?);
        }
    }
    if let (Some(_), Some(preview)) = (legacy, preview.as_mut()) {
        // The role as that tab names it.
        preview.insert("role".into(), json!("delegate"));
    }
    let project_name = fields
        .get("project")
        .and_then(Value::as_str)
        .map(str::to_string);
    if ROLE_PURPOSES.contains(&purpose) {
        // Who the change replaces or evicts decides its outcome, so it is
        // signed too.
        let prefix = if purpose.ends_with("conductor") {
            CONDUCTOR_PREFIX
        } else {
            COORDINATOR_PREFIX
        };
        let name = format!("{prefix}{}", project_name.as_deref().unwrap_or(""));
        fields.insert("holder".into(), role_holder(c, &name, now).await?);
    }
    if matches!(purpose, "grant-conductor" | "assign-coordinator") {
        // So is the other role's holder, which the change breaks when it
        // is the recipient.
        let other = if purpose == "grant-conductor" {
            COORDINATOR_PREFIX
        } else {
            CONDUCTOR_PREFIX
        };
        let name = format!("{other}{}", project_name.as_deref().unwrap_or(""));
        fields.insert("other_holder".into(), role_holder(c, &name, now).await?);
    }
    let issued = legacy.map_or(purpose, |(old, _)| old);
    let (payload, mac) = issue(c, issued, fields, now).await?;
    let options = get_options(rp_id, &payload, &signing_keys(c, now).await?);
    let mut out = json!({"payload": payload, "mac": mac, "publicKey": options});
    if let Some(preview) = preview {
        out["preview"] = Value::Object(preview);
    }
    Ok(out)
}

#[cfg(test)]
#[path = "maintainer_ops_tests.rs"]
mod tests;

async fn action_route(app: &App, path: &str, h: &HeaderMap, body: Map<String, Value>) -> Response {
    let principal = match gate(app, h) {
        Ok(p) => p,
        Err(r) => return *r,
    };
    let board = app.board.clone();
    let mail_action = matches!(path, "/api/maintainer/send" | "/api/maintainer/repudiate");
    let ready = if mail_action {
        match board.full_tier().await {
            Ok(r) => Some(r),
            Err(_) => return refusal("coordination_unavailable", None),
        }
    } else {
        None
    };
    let path = path.to_string();
    let owner = board.clone();
    let answer = board
        .with_store(move |store, client| {
            Box::pin(async move {
                let rp = &owner.config.maintainer_rp_id;
                let origin = &owner.config.maintainer_origin;
                let result: Outcome<Value> = async {
                    match path.as_str() {
                        "/api/maintainer/enrol" => {
                            actions::enrol(store, client, rp, origin, &principal, &body).await
                        }
                        "/api/maintainer/cancel" => {
                            actions::cancel(store, client, rp, origin, &principal, &body).await
                        }
                        "/api/maintainer/revoke" => {
                            actions::revoke(store, client, rp, origin, &principal, &body).await
                        }
                        "/api/maintainer/role" => {
                            actions::complete(
                                store,
                                client,
                                rp,
                                origin,
                                &body,
                                &[
                                    "grant-conductor",
                                    "revoke-conductor",
                                    "assign-coordinator",
                                    "revoke-coordinator",
                                    "grant-delegate",
                                    "revoke-delegate",
                                ],
                                |store, tx, fields, _key| {
                                    Box::pin(async move { roles::role(store, tx, fields).await })
                                },
                            )
                            .await
                        }
                        _ => {
                            let purpose = if path.ends_with("/send") {
                                "send"
                            } else {
                                "repudiate"
                            };
                            for attempt in 0..2 {
                                let cached = owner
                                    .maintainer_identity
                                    .lock()
                                    .expect("identity lock")
                                    .clone();
                                let identity = match cached {
                                    Some(i) => i,
                                    None => {
                                        let params =
                                            json!({"label":"maintainer","status":"maintainer"});
                                        let row = store
                                            .register(
                                                client,
                                                MAINTAINER_PRINCIPAL,
                                                params.as_object().expect("object"),
                                            )
                                            .await?;
                                        let i = (
                                            row["agent_id"].as_str().expect("id").to_string(),
                                            row["credential"]
                                                .as_str()
                                                .expect("credential")
                                                .to_string(),
                                        );
                                        *owner.maintainer_identity.lock().expect("identity lock") =
                                            Some(i.clone());
                                        i
                                    }
                                };
                                let stamp = crate::board::dispatch::stamp(
                                    ready.as_ref().expect("mail full tier"),
                                );
                                let actor = principal.clone();
                                let result = actions::complete(
                                    store,
                                    client,
                                    rp,
                                    origin,
                                    &body,
                                    &[purpose],
                                    move |store, tx, fields, key| {
                                        Box::pin(async move {
                                            if purpose == "send" {
                                                mail::send(
                                                    store, tx, fields, &key.proof, &identity, stamp,
                                                )
                                                .await
                                            } else {
                                                mail::repudiate(
                                                    store, tx, fields, &key.proof, &identity,
                                                    stamp, &actor,
                                                )
                                                .await
                                            }
                                        })
                                    },
                                )
                                .await;
                                match result {
                                    Err(Refusal::Code("_identity", _)) => {
                                        *owner.maintainer_identity.lock().expect("identity lock") =
                                            None;
                                        if attempt == 1 {
                                            return Err(refuse(
                                                "coordination_unavailable",
                                                "identity",
                                            ));
                                        }
                                    }
                                    other => return other,
                                }
                            }
                            unreachable!()
                        }
                    }
                }
                .await;
                match result {
                    Ok(v) => Ok(v),
                    Err(Refusal::Code(code, check)) => {
                        eprintln!("maintainer route refused: {code} ({check})");
                        Ok(json!({"_refused":code}))
                    }
                    Err(Refusal::Board(e)) => Err(e),
                }
            })
        })
        .await;
    match answer {
        Ok(mut v) => {
            if let Some(code) = v.get("_refused").and_then(Value::as_str) {
                return refusal(code, None);
            }
            if let Some(out) = v.as_object_mut() {
                if let Some(Value::Array(ids)) = out.remove("_notify") {
                    for id in ids {
                        if let Some(id) = id.as_str() {
                            board.hub.notify(id);
                        }
                    }
                }
                if let Some(Value::String(notice)) = out.remove("_notice") {
                    board.daemon_notice(&format!("Maintainer passkey change in the Console: {notice}. A courtesy notice only; the Console's Settings, Your passkeys, is the record. Nothing for you to do.")).await;
                }
            }
            json_response(200, &v)
        }
        Err(_) => refusal("coordination_unavailable", None),
    }
}
