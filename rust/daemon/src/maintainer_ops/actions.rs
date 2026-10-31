//! Signed completions and passkey changes (`storage/maintainer.py`).

use std::collections::HashSet;
use std::future::Future;
use std::pin::Pin;

use tokio_postgres::{Client, GenericClient, Row, Transaction};

use super::{
    Outcome, Refusal, check_label, create_options, issue, live_keys, refuse, secret, unrevoked_keys,
};
use crate::board::store::{Event, Store, append, now};
use crate::maintainer_webauthn::{
    self as webauthn, AssertionInput, Registration, RegistrationInput,
};
use hmac::{Hmac, Mac};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

// These are the Python store's protocol values: seven days protects spent
// challenges against a backward wall-clock step; five guesses burn a code.
const SPENT_NONCE_RETENTION: f64 = 7.0 * 86400.0;
const BOOTSTRAP_MAX_FAILURES: i32 = 5;
const QUARANTINE_SECONDS: f64 = 24.0 * 3600.0;
const MAX_PAYLOAD_CHARS: usize = 16384;

pub(super) type ActionFuture<'a> = Pin<Box<dyn Future<Output = Outcome<Value>> + Send + 'a>>;

/// The signer and the proof passed to the signed action. Counter and age
/// fields remain private to the completion and cancellation checks.
pub(super) struct VerifiedKey {
    pub(super) credential_id: String,
    pub(super) label: String,
    pub(super) proof: Value,
    sign_count: i64,
    new_count: i64,
    created_at: f64,
}

struct VerifyFailure {
    refusal: Refusal,
    flag: Option<String>,
}

impl From<Refusal> for VerifyFailure {
    fn from(refusal: Refusal) -> Self {
        Self {
            refusal,
            flag: None,
        }
    }
}

impl From<tokio_postgres::Error> for VerifyFailure {
    fn from(error: tokio_postgres::Error) -> Self {
        Refusal::from(error).into()
    }
}

fn invalid(check: &'static str) -> Refusal {
    refuse("assertion_invalid", check)
}

fn webauthn_error(error: webauthn::AssertionInvalid) -> Refusal {
    invalid(error.check)
}

/// Reject duplicate object keys at every nesting level before using the
/// HTTP parser (which also protects real keys from serde's number transport).
fn payload_fields(payload: &str) -> Outcome<Map<String, Value>> {
    let bytes = payload.as_bytes();
    let mut containers: Vec<Option<HashSet<String>>> = Vec::new();
    let mut pos = 0;
    while pos < bytes.len() {
        match bytes[pos] {
            b'{' => containers.push(Some(HashSet::new())),
            b'[' => containers.push(None),
            b'}' | b']' => {
                containers.pop();
            }
            b'"' => {
                let start = pos;
                pos += 1;
                while pos < bytes.len() {
                    match bytes[pos] {
                        b'\\' => pos += 2,
                        b'"' => break,
                        _ => pos += 1,
                    }
                }
                if pos >= bytes.len() {
                    return Err(invalid("payload"));
                }
                let mut next = pos + 1;
                while bytes.get(next).is_some_and(u8::is_ascii_whitespace) {
                    next += 1;
                }
                if bytes.get(next) == Some(&b':') {
                    let key: String = serde_json::from_str(&payload[start..=pos])
                        .map_err(|_| invalid("payload"))?;
                    if let Some(Some(keys)) = containers.last_mut()
                        && !keys.insert(key)
                    {
                        return Err(invalid("payload"));
                    }
                }
            }
            _ => (),
        }
        pos += 1;
    }
    match crate::http::parse_body_json(payload).map_err(|_| invalid("payload"))? {
        Value::Object(fields) => Ok(fields),
        _ => Err(invalid("purpose")),
    }
}

/// `open`: shape, constant-time MAC, duplicate-free payload, purpose,
/// finite expiry and nonce. Only the authenticated payload supplies inputs.
pub(super) async fn open(
    c: &impl GenericClient,
    body: &Map<String, Value>,
    purposes: &[&str],
) -> Outcome<(String, Map<String, Value>)> {
    let (Some(Value::String(payload)), Some(Value::String(mac))) =
        (body.get("payload"), body.get("mac"))
    else {
        return Err(invalid("mac_shape"));
    };
    if payload.chars().count() > MAX_PAYLOAD_CHARS
        || mac.len() != 64
        || !mac.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    {
        return Err(invalid("mac_shape"));
    }
    let mut expected = Hmac::<Sha256>::new_from_slice(&secret(c).await?).expect("any key length");
    expected.update(payload.as_bytes());
    expected
        .verify_slice(&hex::decode(mac).expect("checked hex"))
        .map_err(|_| invalid("mac"))?;
    let fields = payload_fields(payload)?;
    if !fields
        .get("purpose")
        .and_then(Value::as_str)
        .is_some_and(|purpose| purposes.contains(&purpose))
    {
        return Err(invalid("purpose"));
    }
    if !fields
        .get("expires_at")
        .and_then(Value::as_f64)
        .is_some_and(|expires| expires.is_finite() && now() <= expires)
    {
        return Err(refuse("challenge_expired", "expiry"));
    }
    if !matches!(fields.get("nonce"), Some(Value::String(_))) {
        return Err(invalid("nonce"));
    }
    Ok((payload.clone(), fields))
}

async fn spend(c: &impl GenericClient, fields: &Map<String, Value>) -> Outcome<()> {
    let at = now();
    let expires = fields["expires_at"].as_f64().expect("opened expiry");
    if at > expires {
        return Err(refuse("challenge_expired", "expiry"));
    }
    c.execute(
        "DELETE FROM maintainer_nonces WHERE expires_at<$1",
        &[&(at - SPENT_NONCE_RETENTION)],
    )
    .await?;
    let nonce = fields["nonce"].as_str().expect("opened nonce");
    if c.execute(
        "INSERT INTO maintainer_nonces (nonce,expires_at) VALUES ($1,$2) \
         ON CONFLICT (nonce) DO NOTHING",
        &[&nonce, &expires],
    )
    .await?
        != 1
    {
        return Err(refuse("challenge_spent", "nonce"));
    }
    Ok(())
}

fn decode(object: Option<&Value>, field: &str, check: &'static str) -> Outcome<Vec<u8>> {
    let Some(Value::Object(object)) = object else {
        return Err(invalid(check));
    };
    let Some(Value::String(value)) = object.get(field) else {
        return Err(invalid(check));
    };
    webauthn::b64url_decode(value, check).map_err(webauthn_error)
}

fn credential_id(credential: Option<&Value>) -> Outcome<Vec<u8>> {
    let Some(Value::Object(object)) = credential else {
        return Err(invalid("credential_shape"));
    };
    let raw = decode(credential, "id", "credential_id")?;
    if object.contains_key("rawId") && decode(credential, "rawId", "raw_id")? != raw {
        return Err(invalid("raw_id"));
    }
    Ok(raw)
}

async fn verify(
    c: &impl GenericClient,
    rp_id: &str,
    origin: &str,
    payload: &str,
    assertion: Option<&Value>,
) -> Result<VerifiedKey, VerifyFailure> {
    let raw = credential_id(assertion)?;
    let response = assertion.and_then(|a| a.get("response"));
    let client_data = decode(response, "clientDataJSON", "client_data")?;
    let auth_data = decode(response, "authenticatorData", "auth_data")?;
    let signature = decode(response, "signature", "signature")?;
    let id = webauthn::b64url_encode(&raw);
    let row = c
        .query_opt(
            "SELECT * FROM maintainer_passkeys WHERE credential_id=$1 FOR UPDATE",
            &[&id],
        )
        .await?
        .ok_or_else(|| invalid("unknown_credential"))?;
    let at = now();
    if row.get::<_, String>("state") != "active" {
        return Err(invalid("state").into());
    }
    if !row
        .get::<_, Option<f64>>("active_from")
        .is_some_and(|from| at >= from)
    {
        return Err(invalid("quarantined").into());
    }
    let alg = i64::from(row.get::<_, i32>("alg"));
    if !webauthn::SUPPORTED_ALGS.contains(&alg) {
        return Err(invalid("stored_alg").into());
    }
    let public_key: Vec<u8> = row.get("public_key");
    let challenge = Sha256::digest(payload.as_bytes());
    let parsed = webauthn::verify_assertion(&AssertionInput {
        public_key: &public_key,
        alg,
        client_data_json: &client_data,
        authenticator_data: &auth_data,
        signature: &signature,
        challenge: &challenge,
        origin,
        rp_id,
    })
    .map_err(webauthn_error)?;
    let sign_count: i64 = row.get("sign_count");
    let new_count = i64::from(parsed.sign_count);
    if (sign_count != 0 || new_count != 0) && new_count <= sign_count {
        return Err(VerifyFailure {
            refusal: invalid("sign_count"),
            flag: Some(id),
        });
    }
    let label: String = row.get("label");
    let proof = json!({
        "credential_id": id, "label": label,
        "authenticator_data": webauthn::b64url_encode(&auth_data),
        "client_data_json": webauthn::b64url_encode(&client_data),
        "signature": webauthn::b64url_encode(&signature),
        "payload": payload, "signed_at": at,
    });
    Ok(VerifiedKey {
        credential_id: id,
        label,
        proof,
        sign_count,
        new_count,
        created_at: row.get("created_at"),
    })
}

async fn touch(c: &impl GenericClient, key: &VerifiedKey) -> Outcome<()> {
    if c.execute(
        "UPDATE maintainer_passkeys SET sign_count=$1,last_used_at=$2 \
         WHERE credential_id=$3 AND sign_count=$4",
        &[&key.new_count, &now(), &key.credential_id, &key.sign_count],
    )
    .await?
        != 1
    {
        return Err(invalid("sign_count_race"));
    }
    Ok(())
}

async fn finish(tx: Transaction<'_>, result: Outcome<Value>) -> Outcome<Value> {
    match result {
        Ok(value) => {
            crate::board::db::commit(tx).await?;
            Ok(value)
        }
        Err(error) => {
            tx.rollback().await?;
            Err(error)
        }
    }
}

/// Verify, spend, advance the counter, and perform the action in one
/// transaction. A refused action rolls every mutation back. A counter
/// regression flags the key only after that rollback, in its own transaction.
pub(super) async fn complete<F>(
    store: &Store,
    client: &mut Client,
    rp_id: &str,
    origin: &str,
    body: &Map<String, Value>,
    purposes: &[&str],
    action: F,
) -> Outcome<Value>
where
    F: for<'a, 't> FnOnce(
        &'a Store,
        &'a Transaction<'t>,
        &'a Map<String, Value>,
        &'a VerifiedKey,
    ) -> ActionFuture<'a>,
{
    let (payload, fields) = open(client, body, purposes).await?;
    let tx = client.transaction().await?;
    let key = match verify(&tx, rp_id, origin, &payload, body.get("assertion")).await {
        Ok(key) => key,
        Err(failure) => {
            tx.rollback().await?;
            if let Some(id) = failure.flag {
                let flag_tx = client.transaction().await?;
                flag_tx
                    .execute(
                        "UPDATE maintainer_passkeys SET flagged_at=$1 WHERE credential_id=$2",
                        &[&now(), &id],
                    )
                    .await?;
                crate::board::db::commit(flag_tx).await?;
            }
            return Err(failure.refusal);
        }
    };
    let result = async {
        spend(&tx, &fields).await?;
        touch(&tx, &key).await?;
        action(store, &tx, &fields, &key).await
    }
    .await;
    finish(tx, result).await
}

fn register(
    rp_id: &str,
    origin: &str,
    payload: &str,
    attestation: Option<&Value>,
) -> Outcome<Registration> {
    let raw = credential_id(attestation)?;
    let response = attestation.and_then(|a| a.get("response"));
    let client_data = decode(response, "clientDataJSON", "client_data")?;
    let attestation_object = decode(response, "attestationObject", "attestation")?;
    let challenge = Sha256::digest(payload.as_bytes());
    webauthn::verify_registration(&RegistrationInput {
        raw_id: &raw,
        client_data_json: &client_data,
        attestation_object: &attestation_object,
        challenge: &challenge,
        origin,
        rp_id,
    })
    .map_err(webauthn_error)
}

async fn insert_key(
    c: &impl GenericClient,
    registration: &Registration,
    label: &str,
    enrolled_by: &str,
    state: &str,
    active_from: Option<f64>,
) -> Outcome<Value> {
    let id = webauthn::b64url_encode(&registration.credential_id);
    let alg = registration.alg as i32;
    let count = i64::from(registration.sign_count);
    if c.execute(
        "INSERT INTO maintainer_passkeys (credential_id,public_key,alg,sign_count,label,\
         enrolled_by,state,active_from,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) \
         ON CONFLICT (credential_id) DO NOTHING",
        &[
            &id,
            &registration.public_key,
            &alg,
            &count,
            &label,
            &enrolled_by,
            &state,
            &active_from,
            &now(),
        ],
    )
    .await?
        != 1
    {
        return Err(invalid("credential_exists"));
    }
    Ok(json!({"credential_id": id, "label": label, "state": state, "active_from": active_from}))
}

async fn key_change(
    c: &impl GenericClient,
    principal: &str,
    change: &str,
    id: &str,
    label: &str,
    by: &str,
) -> Outcome<()> {
    let actor = if by == "bootstrap" {
        "daemon"
    } else {
        "maintainer"
    };
    append(
        c,
        vec![
            Event::new(
                "maintainer_key",
                &json!({
                    "change": change, "credential_id": id, "label": label,
                    "by": by, "path": "console", "revoked": null,
                }),
            )
            .actor(actor)
            .principal(principal),
        ],
        now(),
        None,
    )
    .await?;
    Ok(())
}

/// Python's uppercasing, removal of ASCII space/hyphen, then ASCII-ignore
/// encoding. Non-string codes hash as the empty string.
fn code_hash(code: Option<&Value>) -> String {
    let code = code.and_then(Value::as_str).unwrap_or("");
    let normalized: Vec<u8> = code
        .chars()
        .flat_map(char::to_uppercase)
        .filter(|c| c.is_ascii() && !matches!(c, ' ' | '-'))
        .map(|c| c as u8)
        .collect();
    hex::encode(Sha256::digest(&normalized))
}

async fn enrol_bootstrap(
    client: &mut Client,
    rp_id: &str,
    origin: &str,
    principal: &str,
    body: &Map<String, Value>,
) -> Outcome<Value> {
    let (payload, fields) = open(client, body, &["enrol-bootstrap"]).await?;
    let label = check_label(fields.get("label"))?;
    let registration = register(rp_id, origin, &payload, body.get("attestation"))?;
    let tx = client.transaction().await?;
    let result = async {
        spend(&tx, &fields).await?;
        let at = now();
        // Lock the key table before the code row, as the host's code issuer does.
        tx.batch_execute("LOCK TABLE maintainer_passkeys IN SHARE ROW EXCLUSIVE MODE")
            .await?;
        let hash = code_hash(body.get("code"));
        let row = tx
            .query_opt(
                "SELECT * FROM maintainer_bootstrap WHERE code_hash=$1 FOR UPDATE",
                &[&hash],
            )
            .await?;
        let valid = row.as_ref().is_some_and(|row| {
            row.get::<_, Option<f64>>("used_at").is_none()
                && at <= row.get::<_, f64>("expires_at")
                && row.get::<_, i32>("failed_attempts") < BOOTSTRAP_MAX_FAILURES
        });
        if !valid {
            return Err(refuse("bootstrap_code_invalid", "bootstrap_code"));
        }
        if live_keys(&tx).await? != 0 {
            return Err(refuse("enrolment_closed", "live_keys"));
        }
        let out = insert_key(&tx, &registration, &label, "bootstrap", "pending", None).await?;
        let id = out["credential_id"].as_str().expect("inserted id");
        tx.execute(
            "UPDATE maintainer_bootstrap SET used_at=$1,credential_id=$2 WHERE code_hash=$3",
            &[&at, &id, &hash],
        )
        .await?;
        key_change(&tx, principal, "enrol", id, &label, "bootstrap").await?;
        Ok(out)
    }
    .await;
    let result = finish(tx, result).await;
    if matches!(&result, Err(Refusal::Code("bootstrap_code_invalid", _))) {
        // The failed redemption and nonce have rolled back; guesses persist.
        let failure_tx = client.transaction().await?;
        failure_tx
            .execute(
                "UPDATE maintainer_bootstrap SET failed_attempts=failed_attempts+1 \
             WHERE used_at IS NULL AND expires_at>=$1 AND failed_attempts<$2",
                &[&now(), &BOOTSTRAP_MAX_FAILURES],
            )
            .await?;
        crate::board::db::commit(failure_tx).await?;
    }
    result
}

async fn enrol_approved(
    client: &mut Client,
    rp_id: &str,
    origin: &str,
    principal: &str,
    body: &Map<String, Value>,
) -> Outcome<Value> {
    let (payload, fields) = open(client, body, &["enrol"]).await?;
    let label = check_label(fields.get("label"))?;
    let registration = register(rp_id, origin, &payload, body.get("attestation"))?;
    let tx = client.transaction().await?;
    let result = async {
        spend(&tx, &fields).await?;
        let approved_by = fields.get("approved_by").and_then(Value::as_str);
        let approver = tx
            .query_opt(
                "SELECT * FROM maintainer_passkeys WHERE credential_id=$1 FOR UPDATE",
                &[&approved_by],
            )
            .await?;
        let at = now();
        let Some(approver) = approver.filter(|row| {
            row.get::<_, String>("state") == "active"
                && row
                    .get::<_, Option<f64>>("active_from")
                    .is_some_and(|from| at >= from)
        }) else {
            return Err(invalid("approver"));
        };
        let id: String = approver.get("credential_id");
        let out = insert_key(
            &tx,
            &registration,
            &label,
            &id,
            "active",
            Some(at + QUARANTINE_SECONDS),
        )
        .await?;
        key_change(
            &tx,
            principal,
            "add",
            out["credential_id"].as_str().expect("inserted id"),
            &label,
            &id,
        )
        .await?;
        Ok(out)
    }
    .await;
    finish(tx, result).await
}

/// `_enrol`: approval takes precedence over code, as it does in Python.
pub(super) async fn enrol(
    store: &Store,
    client: &mut Client,
    rp_id: &str,
    origin: &str,
    principal: &str,
    body: &Map<String, Value>,
) -> Outcome<Value> {
    if body.contains_key("assertion") {
        let bound = complete(
            store,
            client,
            rp_id,
            origin,
            body,
            &["enrol-approve"],
            |_store, _tx, fields, key| {
                Box::pin(async move {
                    let label = check_label(fields.get("label"))?;
                    Ok(json!({"label": label, "approved_by": key.credential_id,
                    "approval_nonce": fields["nonce"]}))
                })
            },
        )
        .await?;
        let label = bound["label"].clone();
        let (payload, mac) = issue(
            client,
            "enrol",
            bound.as_object().expect("bound").clone(),
            now(),
        )
        .await?;
        let options = create_options(rp_id, &payload, &unrevoked_keys(client).await?);
        return Ok(json!({"payload": payload, "mac": mac, "label": label, "publicKey": options}));
    }
    let bootstrap = body.contains_key("code");
    let out = if bootstrap {
        enrol_bootstrap(client, rp_id, origin, principal, body).await?
    } else {
        enrol_approved(client, rp_id, origin, principal, body).await?
    };
    let id = out["credential_id"].as_str().expect("inserted id");
    let prefix: String = id.chars().take(12).collect();
    let notice = if bootstrap {
        format!(
            "key {prefix} was enrolled with a one-time code from the daemon host and waits for the host confirm"
        )
    } else {
        format!(
            "key {prefix} was added; it cannot sign for 24 hours and an older key can cancel it meanwhile"
        )
    };
    Ok(
        json!({"credential_id": id, "label": out["label"], "state": out["state"], "_notice": notice}),
    )
}

fn older(key: &VerifiedKey, target: &Row) -> bool {
    let created: f64 = target.get("created_at");
    let id: String = target.get("credential_id");
    key.created_at < created || (key.created_at == created && key.credential_id < id)
}

pub(super) async fn cancel(
    store: &Store,
    client: &mut Client,
    rp_id: &str,
    origin: &str,
    principal: &str,
    body: &Map<String, Value>,
) -> Outcome<Value> {
    let principal = principal.to_owned();
    complete(store, client, rp_id, origin, body, &["cancel"],
        move |_store, tx, fields, key| Box::pin(async move {
            let id = fields.get("credential_id").and_then(Value::as_str);
            let target = tx.query_opt("SELECT * FROM maintainer_passkeys WHERE credential_id=$1 FOR UPDATE", &[&id])
                .await?.ok_or_else(|| refuse("credential_not_found", "target"))?;
            let at = now();
            if target.get::<_, String>("state") != "active"
                || !target.get::<_, Option<f64>>("active_from").is_some_and(|from| at < from)
                || !older(key, &target)
            {
                return Err(invalid("cancel_target"));
            }
            let id: String = target.get("credential_id");
            let label: String = target.get("label");
            tx.execute("UPDATE maintainer_passkeys SET state='revoked',revoked_at=$1,revoked_by=$2 WHERE credential_id=$3",
                &[&at, &key.credential_id, &id]).await?;
            key_change(tx, &principal, "cancel", &id, &label, &key.credential_id).await?;
            let prefix: String = id.chars().take(12).collect();
            Ok(json!({"state": "revoked", "_notice": format!("key {prefix} was cancelled by an older key")}))
        })).await
}

pub(super) async fn revoke(
    store: &Store,
    client: &mut Client,
    rp_id: &str,
    origin: &str,
    principal: &str,
    body: &Map<String, Value>,
) -> Outcome<Value> {
    let principal = principal.to_owned();
    complete(store, client, rp_id, origin, body, &["revoke-self"],
        move |_store, tx, fields, key| Box::pin(async move {
            if fields.get("credential_id").and_then(Value::as_str) != Some(key.credential_id.as_str()) {
                return Err(invalid("revoke_other"));
            }
            tx.execute("UPDATE maintainer_passkeys SET state='revoked',revoked_at=$1,revoked_by=$2 WHERE credential_id=$2",
                &[&now(), &key.credential_id]).await?;
            key_change(tx, &principal, "revoke", &key.credential_id, &key.label, &key.credential_id).await?;
            let prefix: String = key.credential_id.chars().take(12).collect();
            Ok(json!({"state": "revoked", "_notice": format!("key {prefix} revoked itself")}))
        })).await
}
