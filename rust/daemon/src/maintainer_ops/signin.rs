//! Console sign-in with the maintainer passkey (schema v59; spec 2026-11-02,
//! section 5), from `pseudolife_memory/console_passkey.py`.
//!
//! At the passkey origin the browser mints a token first, then asks `POST
//! /api/console/passkey-challenge` ([`issue_signin_challenge`]) for a
//! challenge whose MAC'd payload (purpose `sign-in`) carries that token's
//! SHA-256 and the browser's label, so the assertion signs exactly which
//! token the session will accept. It signs, and posts `{payload, mac,
//! assertion}` to `POST /api/console/session`, which verifies through the
//! signed-action completion (past quarantine, sign count forward, nonce
//! spent once) and creates the session in the same transaction
//! ([`complete_signin`]). The session acts as the key's **sign-in
//! principal** (`maintainer_passkeys.signs_in_as`), never as `maintainer`.
//!
//! A key's sign-in principal is set when it is enrolled (the enrolling
//! request's principal, `actions::insert_key`) and changed only by the
//! signed action "Use this passkey to sign in" (purpose `bind-sign-in`,
//! `POST /api/console/passkey-bind`, [`bind_route`]), which binds it to the
//! caller's principal, an owner principal only.
//!
//! The challenge route is pre-auth, so it reads the signing keys and the
//! challenge secret on its own short-lived connection, never the board's,
//! and never writes: no secret yet means no challenge.

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Map, Value, json};
use tokio_postgres::GenericClient;

use super::{
    Outcome, Refusal, SECRET_META_KEY, actions, challenge_payload, get_options, issue, key_notice,
    mac_hex, refuse, signing_keys,
};
use crate::auth::PrincipalStore;
use crate::board::Board;
use crate::board::store::{MAINTAINER_PRINCIPAL, now};
use crate::console_sessions::{
    self as sessions, ConsoleSession, METHOD_PASSKEY, SOURCE_ENVIRONMENT, SOURCE_STORE,
    SessionSnapshot,
};
use crate::http::{App, json_response};
use crate::read::maintainer::{config_problem, gate, refusal};

pub(crate) const SIGN_IN: &str = "sign-in";
pub(crate) const BIND_SIGN_IN: &str = "bind-sign-in";

/// `UNAVAILABLE_HINT`: a key that cannot sign anyone in, and the way out.
pub(crate) const UNAVAILABLE_HINT: &str = "this passkey has no Console sign-in, or the principal \
     it signs in as no longer exists. On the daemon's host run `pseudolife-mcp console`, open \
     Settings, Your passkeys, and choose \"Use this passkey to sign in\"; after that this passkey \
     signs the Console in";
/// `NO_KEYS_HINT`: no key can sign yet.
pub(crate) const NO_KEYS_HINT: &str = "no passkey can sign yet: none is set up (`pseudolife-mcp \
     maintainer setup` on the daemon's host), the first one still waits for the host's \
     `pseudolife-mcp maintainer confirm <prefix>`, or a newly added one is still in its 24-hour \
     wait. Meanwhile sign in with `pseudolife-mcp console`";

const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// `_secret`: the challenge secret, read only. `None` when it does not
/// exist yet (an unauthenticated caller must never create it) or is not 64
/// lower-case hex characters.
async fn read_secret(c: &impl GenericClient) -> Result<Option<Vec<u8>>, tokio_postgres::Error> {
    let row = c
        .query_opt(
            "SELECT value FROM public.meta WHERE key = $1",
            &[&SECRET_META_KEY],
        )
        .await?;
    let value = row.and_then(|r| r.try_get::<_, Value>(0).ok());
    Ok(match value {
        Some(Value::String(v))
            if v.len() == 64 && v.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) =>
        {
            hex::decode(v).ok()
        }
        _ => None,
    })
}

/// What `issue_signin_challenge` came to.
#[derive(Debug, PartialEq)]
pub(crate) enum Challenge {
    /// `{payload, mac, publicKey}`.
    Issued(Value),
    /// No secret yet, or no key can sign.
    NoKeys,
    /// Keys can sign, but none has a sign-in principal.
    Unbound,
}

/// `issue_signin_challenge`: a `sign-in` challenge for the browser's
/// `token_hash` and its WebAuthn request options, listing only the keys that
/// can sign in (active, past quarantine, with a sign-in principal), so the
/// person is told before the passkey prompt when none can. Read-only, on its
/// own connection. `Err` is a database failure.
pub(crate) async fn issue_signin_challenge(
    dsn: &str,
    token_hash: &str,
    label: &str,
    rp_id: &str,
) -> Result<Challenge, String> {
    let at = now();
    let config = crate::storage::parse_dsn(dsn)?;
    let (client, task) = crate::storage::connect(&config, CONNECT_TIMEOUT).await?;
    let read = async {
        client.batch_execute("SET statement_timeout = '5s'").await?;
        let secret = read_secret(&client).await?;
        let rows = client
            .query(
                "SELECT credential_id, signs_in_as FROM public.maintainer_passkeys \
                 WHERE state = 'active' AND active_from IS NOT NULL AND active_from <= $1 \
                 ORDER BY created_at, credential_id",
                &[&at],
            )
            .await?;
        let signing = rows
            .iter()
            .map(|r| {
                Ok((
                    r.try_get::<_, String>(0)?,
                    r.try_get::<_, Option<String>>(1)?,
                ))
            })
            .collect::<Result<Vec<_>, tokio_postgres::Error>>()?;
        Ok::<_, tokio_postgres::Error>((secret, signing))
    }
    .await;
    drop(client);
    let _ = task.await;
    let (secret, signing) = read.map_err(|e| crate::storage::error_text(&e))?;
    let Some(secret) = secret.filter(|_| !signing.is_empty()) else {
        return Ok(Challenge::NoKeys);
    };
    let keys: Vec<String> = signing
        .into_iter()
        .filter(|(_, principal)| principal.as_deref().is_some_and(|p| !p.is_empty()))
        .map(|(id, _)| id)
        .collect();
    if keys.is_empty() {
        return Ok(Challenge::Unbound);
    }
    let mut fields = Map::new();
    fields.insert("token_sha256".into(), json!(token_hash));
    fields.insert("label".into(), json!(label));
    let payload = challenge_payload(SIGN_IN, fields, at).map_err(|_| "randomness".to_string())?;
    let mac = mac_hex(&secret, &payload);
    let options = get_options(rp_id, &payload, &keys);
    Ok(Challenge::Issued(
        json!({"payload": payload, "mac": mac, "publicKey": options}),
    ))
}

/// What a passkey sign-in came to, as the route answers it.
#[derive(Debug)]
pub(crate) enum Signin {
    /// Created and already published to the session view.
    Session(ConsoleSession),
    /// Any bad assertion or challenge: `400 signin_refused`, counted.
    Refused,
    /// The key has no sign-in principal, or it no longer resolves: `409
    /// passkey_signin_unavailable`, refunded.
    KeyUnavailable,
    /// The board or the stored principals could not be read: `503
    /// signin_unavailable`, refunded.
    Unavailable,
    /// A database failure, the outcome unknown: `503 signin_unavailable`.
    Failed,
}

/// `principal_source`: where `name` resolves on its own now, the
/// environment or the store, or nowhere (`None`); `Err` when the stored view
/// is stale.
fn principal_source(
    name: &str,
    env_principals: &HashSet<String>,
    store: &PrincipalStore,
) -> Outcome<Option<&'static str>> {
    if env_principals.contains(name) {
        return Ok(Some(SOURCE_ENVIRONMENT));
    }
    if !store.available() {
        return Err(refuse("principals_unavailable", "store"));
    }
    Ok(store.resolves(name).then_some(SOURCE_STORE))
}

/// `complete_signin`: verify a `sign-in` assertion and create its session,
/// in one transaction on the board's connection under its lock, as every
/// maintainer completion runs. The session is published to `view` before
/// the lock is released, so a rebind of the same key, which drops its
/// sessions under that lock, cannot run between the commit and the
/// publication and be undone by it.
pub(crate) async fn complete_signin(
    board: Arc<Board>,
    env_principals: HashSet<String>,
    principals: Arc<PrincipalStore>,
    view: Arc<SessionSnapshot>,
    body: Map<String, Value>,
) -> Signin {
    let cfg = &board.config;
    if !cfg.enabled || config_problem(&cfg.maintainer_rp_id, &cfg.maintainer_origin) != Ok(None) {
        // `maintainer_https_required`: a refusal like any other.
        return Signin::Refused;
    }
    if board.service.storage_tier().await.is_err() {
        return Signin::Unavailable;
    }
    let created: Arc<Mutex<Option<ConsoleSession>>> = Arc::default();
    let slot = created.clone();
    let owner = board.clone();
    let answer = board
        .with_store(move |store, client| {
            Box::pin(async move {
                let rp = owner.config.maintainer_rp_id.clone();
                let origin = owner.config.maintainer_origin.clone();
                let result: Outcome<Value> = async {
                    // A pre-auth route never creates the challenge secret (the
                    // completion's MAC check would): no secret, no challenge
                    // was ever issued.
                    if read_secret(client).await?.is_none() {
                        return Err(refuse("assertion_invalid", "no_secret"));
                    }
                    let fill = slot.clone();
                    actions::complete(
                        store,
                        client,
                        &rp,
                        &origin,
                        &body,
                        &[SIGN_IN],
                        move |_store, tx, fields, key| {
                            Box::pin(async move {
                                let token_hash = fields
                                    .get("token_sha256")
                                    .filter(|v| crate::routes::pair::is_sha256_hex(v))
                                    .and_then(Value::as_str);
                                let label = fields
                                    .get("label")
                                    .and_then(Value::as_str)
                                    .and_then(sessions::clean_label);
                                let (Some(token_hash), Some(label)) = (token_hash, label) else {
                                    return Err(refuse("assertion_invalid", "signin_fields"));
                                };
                                let Some(principal) = key.signs_in_as.as_deref() else {
                                    return Err(refuse(
                                        "passkey_signin_unavailable",
                                        "signs_in_as",
                                    ));
                                };
                                let Some(source) =
                                    principal_source(principal, &env_principals, &principals)?
                                else {
                                    return Err(refuse(
                                        "passkey_signin_unavailable",
                                        "signs_in_as",
                                    ));
                                };
                                let row = match sessions::insert_session(
                                    tx,
                                    token_hash,
                                    principal,
                                    source,
                                    &key.credential_id,
                                    METHOD_PASSKEY,
                                    &label,
                                )
                                .await
                                {
                                    Ok(row) => row,
                                    Err(e)
                                        if e.code()
                                            == Some(
                                                &tokio_postgres::error::SqlState::UNIQUE_VIOLATION,
                                            ) =>
                                    {
                                        return Err(refuse("assertion_invalid", "token_in_use"));
                                    }
                                    Err(e) => return Err(e.into()),
                                };
                                *fill.lock().expect("sign-in slot") = Some(row);
                                Ok(Value::Null)
                            })
                        },
                    )
                    .await
                }
                .await;
                match result {
                    Ok(_) => {
                        if let Some(row) = slot.lock().expect("sign-in slot").clone() {
                            view.add(row);
                        }
                        Ok(Value::Null)
                    }
                    Err(Refusal::Code(code, check)) => {
                        eprintln!("maintainer {SIGN_IN} refused: {code} ({check})");
                        Ok(json!({"_refused": code}))
                    }
                    Err(Refusal::Board(e)) => Err(e),
                }
            })
        })
        .await;
    match answer {
        Ok(Value::Object(o)) => match o.get("_refused").and_then(Value::as_str) {
            Some("passkey_signin_unavailable") => Signin::KeyUnavailable,
            Some("coordination_unavailable" | "principals_unavailable") => Signin::Unavailable,
            _ => Signin::Refused,
        },
        Ok(_) => match created.lock().expect("sign-in slot").take() {
            Some(row) => Signin::Session(row),
            None => Signin::Failed,
        },
        Err(_) => Signin::Failed,
    }
}

/// `POST /api/console/passkey-bind` ("Use this passkey to sign in", purpose
/// `bind-sign-in`), behind the bearer gate: `{}` asks for a challenge naming
/// the caller's principal, `{payload, mac, assertion}` completes it. Through
/// the maintainer routes' gate and the board lock, for an owner principal
/// only. On success the key signs the Console in as the caller and its
/// passkey sessions under any other principal end, in the same transaction
/// and in the daemon's view before the lock is released.
pub(crate) async fn bind_route(
    app: &App,
    h: &HeaderMap,
    session: Option<&str>,
    principal: &str,
    body: Map<String, Value>,
) -> Response {
    let owners =
        crate::board::console::owner_principals(&app.board.config, &app.env_tokens, &app.store);
    if principal == MAINTAINER_PRINCIPAL || !owners.iter().any(|p| p == principal) {
        eprintln!("maintainer route refused: principal_not_allowed (bind_not_owner)");
        return refusal("principal_not_allowed", Some(("reason", "not_owner")));
    }
    let completing = !body.is_empty();
    if completing {
        let keys: HashSet<&str> = body.keys().map(String::as_str).collect();
        if keys != HashSet::from(["payload", "mac", "assertion"]) {
            eprintln!("maintainer route refused: invalid_request (bind_body)");
            return refusal("invalid_request", None);
        }
    }
    let principal = match gate(app, h, session) {
        Ok(p) => p,
        Err(r) => return *r,
    };
    // A task of its own, as Python's executor thread is: a browser that goes
    // away after the COMMIT was sent must not stop the key's other sessions
    // leaving the view, nor the notice.
    let (board, view) = (app.board.clone(), app.sessions.clone());
    tokio::spawn(bind_complete(board, view, principal, completing, body))
        .await
        .unwrap_or_else(|_| {
            eprintln!("maintainer route failed (internal)");
            refusal("coordination_unavailable", None)
        })
}

/// `bind_route`'s board work, the view's update and the notice.
async fn bind_complete(
    board: Arc<Board>,
    view: Option<Arc<SessionSnapshot>>,
    caller: String,
    completing: bool,
    body: Map<String, Value>,
) -> Response {
    let owner = board.clone();
    let answer = board
        .with_store(move |store, client| {
            Box::pin(async move {
                let rp = owner.config.maintainer_rp_id.clone();
                let origin = owner.config.maintainer_origin.clone();
                let result: Outcome<Value> = async {
                    if !completing {
                        let at = now();
                        let mut fields = Map::new();
                        fields.insert("principal".into(), json!(caller));
                        let (payload, mac) = issue(client, BIND_SIGN_IN, fields, at).await?;
                        let options = get_options(&rp, &payload, &signing_keys(client, at).await?);
                        return Ok(json!({"payload": payload, "mac": mac, "publicKey": options}));
                    }
                    let who = caller.clone();
                    let out = actions::complete(
                        store,
                        client,
                        &rp,
                        &origin,
                        &body,
                        &[BIND_SIGN_IN],
                        move |_store, tx, fields, key| {
                            Box::pin(async move {
                                if fields.get("principal").and_then(Value::as_str)
                                    != Some(who.as_str())
                                {
                                    return Err(refuse("assertion_invalid", "bind_principal"));
                                }
                                tx.execute(
                                    "UPDATE maintainer_passkeys SET signs_in_as=$1 \
                                     WHERE credential_id=$2",
                                    &[&who, &key.credential_id],
                                )
                                .await?;
                                sessions::revoke_issuer_sessions(
                                    tx,
                                    Some(&key.credential_id),
                                    Some(&who),
                                )
                                .await?;
                                actions::key_change(
                                    tx,
                                    &who,
                                    BIND_SIGN_IN,
                                    &key.credential_id,
                                    &key.label,
                                    &key.credential_id,
                                )
                                .await?;
                                Ok(json!({"credential_id": key.credential_id, "signs_in_as": who}))
                            })
                        },
                    )
                    .await?;
                    // Still under the board lock, as a passkey sign-in's
                    // publication is: the key's other sessions leave the view
                    // at once and no sign-in committed before can put one back.
                    if let (Some(view), Some(id)) = (&view, out["credential_id"].as_str()) {
                        view.drop_issuer(id, Some(&caller));
                    }
                    Ok(out)
                }
                .await;
                match result {
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
        Ok(v) => {
            if let Some(code) = v.get("_refused").and_then(Value::as_str) {
                return refusal(code, None);
            }
            if let (Some(id), Some(who)) = (
                v.get("credential_id").and_then(Value::as_str),
                v.get("signs_in_as").and_then(Value::as_str),
            ) {
                let prefix: String = id.chars().take(12).collect();
                board
                    .daemon_notice(&key_notice(&format!(
                        "key {prefix} now signs the Console in as {who}"
                    )))
                    .await;
            }
            json_response(200, &v)
        }
        Err(_) => {
            eprintln!("maintainer route failed (internal)");
            refusal("coordination_unavailable", None)
        }
    }
}
