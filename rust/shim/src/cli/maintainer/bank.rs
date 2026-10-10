//! The bank side of `maintainer_cli.main`: `_bank()`'s table check, then
//! `MaintainerStore`'s host operations (`bootstrap_code`,
//! `bootstrap_redeemed`, `bootstrap_burned`, `confirm`, `revoke`, `reset`,
//! `passkeys`) with their statements, clock reads and transaction
//! boundaries, and `_key_change`'s audit append.
use super::{
    Action, STDOUT_REFUSED, Written, display, print, refused, sqlstate, stdout_error_class,
    write_report,
};
use crate::{
    board::identity::hex_hash,
    cli::lease::operator::store::audit_timestamp,
    pg::{self, Transaction},
};
use serde_json::{Value, json};
use std::time::{Duration, Instant};
use tokio_postgres::error::SqlState;

const NO_TABLES: &str = "this bank has no maintainer tables yet (schema older than v54); start a v54 daemon once to create them";
/// `BOOTSTRAP_TTL`: the one-time code's life, and how long the host waits.
const BOOTSTRAP_TTL: u64 = 600;
const BOOTSTRAP_MAX_FAILURES: i32 = 5;
/// `--poll`'s default; other intervals are deferred argv.
const POLL: Duration = Duration::from_secs(2);
const BASE32: &[u8; 32] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const SECRET_META_KEY: &str = "maintainer_secret_v1";
const LIVE_KEYS: &str =
    "SELECT count(*) AS n FROM maintainer_passkeys WHERE state IN ('pending','active')";

/// The most a listing may hold and still reach CPython's stdout in one piece
/// at exit: a pipe's text stream buffers up to `io.DEFAULT_BUFFER_SIZE`
/// (8192) bytes before its first write, so a refused stdout fails only in
/// the shutdown flush (exit 120). A longer listing would be written, and
/// could be refused, part way through the prints: deferred.
const LISTING_BUFFER: usize = 8192;

/// Where an action stops: deferred (before any effect), a refusal code, or
/// a failed COMMIT, whose change may have been made durable and so takes
/// Python's generic error path instead of a deferral.
enum Stop {
    Deferred,
    Refused(&'static str),
    /// The SQLSTATE the failure carried, if any.
    Failed(Option<SqlState>),
}

impl From<tokio_postgres::Error> for Stop {
    fn from(_: tokio_postgres::Error) -> Self {
        Self::Deferred
    }
}

fn sqlstate_of(error: &tokio_postgres::Error) -> Option<SqlState> {
    error.as_db_error().map(|error| error.code().clone())
}

/// `print(f"error: {type(exc).__name__}; check PSEUDOLIFE_MCP_DATABASE_URL")`
/// for the psycopg exception: by the failure's SQLSTATE, else by the server
/// error that ended the connection, else `OperationalError` (a lost
/// connection with no server error).
fn failed(session: &pg::Session, code: Option<SqlState>) -> u8 {
    let class = code
        .or_else(|| session.ended_with())
        .map_or("OperationalError", |code| sqlstate::class_name(code.code()));
    crate::stderrln!("error: {class}; check PSEUDOLIFE_MCP_DATABASE_URL");
    2
}

/// COMMIT, whose failure is never a deferral.
async fn commit(tx: Transaction<'_>) -> Result<(), Stop> {
    tx.commit()
        .await
        .map_err(|error| Stop::Failed(sqlstate_of(&error)))
}

/// The exit code, or `None` to defer. Nothing is printed before an outcome
/// is known, except the waiting form's progress after its code committed.
pub(super) async fn run(dsn: &pg::Dsn, action: Action) -> Option<u8> {
    let mut session = pg::Session::open(dsn).await.ok()?;
    let ending = act(&mut session, action).await;
    let _ = session.close().await;
    ending
}

async fn act(session: &mut pg::Session, action: Action) -> Option<u8> {
    let present: bool = session
        .client()
        .query_one(
            "SELECT to_regclass('public.maintainer_passkeys') IS NOT NULL",
            &[],
        )
        .await
        .ok()?
        .try_get(0)
        .ok()?;
    if !present {
        crate::stderrln!("error: {NO_TABLES}");
        return Some(2);
    }
    let outcome = match action {
        Action::EnrolCode { wait } => return enrol_code(session, wait).await,
        Action::Confirm(prefix) => confirm(session, &prefix).await.map(|(id, label)| {
            format!("Active: {}  label: {label}\n", display::key_prefix(&id))
        }),
        Action::Revoke(prefix) => revoke(session, &prefix).await.map(|(id, label)| {
            format!("Revoked: {}  label: {label}\n", display::key_prefix(&id))
        }),
        Action::Reset { yes: false } => {
            crate::stderrln!("reset revokes every passkey and rotates the secret; pass --yes");
            return Some(1);
        }
        Action::Reset { yes: true } => reset(session).await.map(|revoked| {
            format!(
                "Revoked {revoked} passkey(s); secret rotated; run `pseudolife-mcp maintainer enrol-code` to enrol again.\n"
            )
        }),
        Action::List => return list(session).await,
    };
    match outcome {
        Ok(report) => Some(if print(&report).is_ok() {
            0
        } else {
            STDOUT_REFUSED
        }),
        Err(Stop::Refused(code)) => Some(refused(code)),
        Err(Stop::Failed(code)) => Some(failed(session, code)),
        Err(Stop::Deferred) => None,
    }
}

fn clock() -> Result<f64, Stop> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|now| now.as_secs_f64())
        .map_err(|_| Stop::Deferred)
}

/// `secrets.choice(_BASE32)` ten times: a random byte's low five bits are
/// uniform over the 32 symbols.
fn bootstrap_code() -> Option<String> {
    let mut bytes = [0u8; 10];
    rustls::crypto::aws_lc_rs::default_provider()
        .secure_random
        .fill(&mut bytes)
        .ok()?;
    Some(
        bytes
            .iter()
            .map(|byte| char::from(BASE32[usize::from(byte & 31)]))
            .collect(),
    )
}

/// `secrets.token_hex(32)`.
fn secret_hex() -> Result<String, Stop> {
    let mut bytes = [0u8; 32];
    rustls::crypto::aws_lc_rs::default_provider()
        .secure_random
        .fill(&mut bytes)
        .map_err(|_| Stop::Deferred)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// `code_hash` of a code this process generated (already normalized).
fn code_hash(code: &str) -> String {
    hex_hash(code)
}

async fn rollback(tx: Transaction<'_>, stop: Stop) -> Stop {
    // A failed rollback leaves nothing committed; the connection closes next.
    let _ = tx.rollback().await;
    stop
}

async fn enrol_code(session: &mut pg::Session, wait: bool) -> Option<u8> {
    let code = bootstrap_code()?;
    let hash = code_hash(&code);
    match store_code(session, &hash).await {
        Ok(()) => {}
        Err(Stop::Refused(code)) => return Some(refused(code)),
        Err(Stop::Failed(code)) => return Some(failed(session, code)),
        Err(Stop::Deferred) => return None,
    }
    let minutes = BOOTSTRAP_TTL / 60;
    let shown = format!(
        "One-time enrolment code: {code}\nValid for {minutes} minutes. Enter it in the Console with a label, then register your passkey.\n"
    );
    if !wait {
        // Buffered by CPython until exit, where a refused stdout exits 120.
        return Some(if print(&shown).is_ok() {
            0
        } else {
            STDOUT_REFUSED
        });
    }
    let deadline = Instant::now() + Duration::from_secs(BOOTSTRAP_TTL);
    // `print(..., flush=True)` sends the code lines with this one; a refused
    // flush raises into `main`'s generic handler (its class, exit 2), and
    // the still-buffered lines then fail CPython's shutdown flush: 120.
    if let Err(error) = print(&format!("{shown}Waiting for the Console to redeem it...\n")) {
        crate::stderrln!(
            "error: {}; check PSEUDOLIFE_MCP_DATABASE_URL",
            stdout_error_class(&error)
        );
        return Some(STDOUT_REFUSED);
    }
    let client = session.client();
    let (report, code) = loop {
        if Instant::now() >= deadline {
            break ("The code expired unredeemed.\n".to_owned(), 1);
        }
        let redeemed = match client
            .query(
                "SELECT b.credential_id,p.label,p.state FROM maintainer_bootstrap b JOIN maintainer_passkeys p ON p.credential_id=b.credential_id WHERE b.code_hash=$1",
                &[&hash],
            )
            .await
        {
            Ok(rows) => rows,
            Err(error) => return Some(failed(session, sqlstate_of(&error))),
        };
        if let Some(row) = redeemed.first() {
            let (Ok(id), Ok(label)) = (
                row.try_get::<_, String>("credential_id"),
                row.try_get::<_, String>("label"),
            ) else {
                return Some(failed(session, None));
            };
            let prefix = display::key_prefix(&id);
            break (
                format!(
                    "Enrolled (pending): {prefix}  label: {label}\nCheck that the Console shows the same prefix and label, then run:\n  pseudolife-mcp maintainer confirm {prefix}\nIf it does not match, someone else redeemed the code: run\n  pseudolife-mcp maintainer revoke {prefix}\n"
                ),
                0,
            );
        }
        let burned = match client
            .query(
                "SELECT failed_attempts FROM maintainer_bootstrap WHERE code_hash=$1 AND used_at IS NULL",
                &[&hash],
            )
            .await
        {
            Ok(rows) => rows,
            Err(error) => return Some(failed(session, sqlstate_of(&error))),
        };
        if let Some(row) = burned.first() {
            let Ok(attempts) = row.try_get::<_, i32>("failed_attempts") else {
                return Some(failed(session, None));
            };
            if attempts >= BOOTSTRAP_MAX_FAILURES {
                break (
                    "The code was burned after too many wrong guesses in the Console. If they were not yours, someone else is trying to enrol: check `pseudolife-mcp maintainer list`, then run enrol-code again.\n".to_owned(),
                    1,
                );
            }
        }
        tokio::time::sleep(POLL).await;
    };
    Some(if print(&report).is_ok() {
        code
    } else {
        STDOUT_REFUSED
    })
}

/// `bootstrap_code`'s transaction: refused while any key is pending or
/// active; earlier unused codes are dropped.
async fn store_code(session: &mut pg::Session, hash: &str) -> Result<(), Stop> {
    let tx = session.client_mut().transaction().await?;
    let result = async {
        tx.batch_execute("LOCK TABLE maintainer_passkeys IN SHARE ROW EXCLUSIVE MODE")
            .await?;
        let live: i64 = tx.query_one(LIVE_KEYS, &[]).await?.try_get("n")?;
        if live != 0 {
            return Err(Stop::Refused("enrolment_closed"));
        }
        tx.execute(
            "DELETE FROM maintainer_bootstrap WHERE used_at IS NULL",
            &[],
        )
        .await?;
        let expires_at = clock()? + BOOTSTRAP_TTL as f64;
        tx.execute(
            "INSERT INTO maintainer_bootstrap (code_hash,expires_at) VALUES ($1,$2)",
            &[&hash, &expires_at],
        )
        .await?;
        Ok(())
    }
    .await;
    match result {
        Ok(()) => commit(tx).await,
        Err(stop) => Err(rollback(tx, stop).await),
    }
}

/// `_by_prefix`'s argument check, made inside the transaction as Python
/// makes it, before the transaction's first statement (it reads nothing).
fn checked_prefix(prefix: &str) -> Result<String, Stop> {
    if prefix.chars().count() < 6
        || !prefix
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    {
        return Err(Stop::Refused("invalid_request"));
    }
    // `_` is a base64url character and a LIKE wildcard: escaped.
    Ok(prefix.replace('_', "\\_") + "%")
}

/// `_by_prefix`: exactly one key whose id starts with the prefix (in
/// `state` when given), locked.
async fn by_prefix(
    tx: &Transaction<'_>,
    pattern: &str,
    state: Option<&str>,
) -> Result<(String, String), Stop> {
    let rows = match state {
        Some(state) => {
            tx.query(
                "SELECT credential_id,label FROM maintainer_passkeys WHERE credential_id LIKE $1 AND state=$2 FOR UPDATE",
                &[&pattern, &state],
            )
            .await?
        }
        None => {
            tx.query(
                "SELECT credential_id,label FROM maintainer_passkeys WHERE credential_id LIKE $1 FOR UPDATE",
                &[&pattern],
            )
            .await?
        }
    };
    if rows.len() != 1 {
        return Err(Stop::Refused("credential_not_found"));
    }
    Ok((rows[0].try_get(0)?, rows[0].try_get(1)?))
}

async fn confirm(session: &mut pg::Session, prefix: &str) -> Result<(String, String), Stop> {
    let tx = session.client_mut().transaction().await?;
    let result = async {
        let pattern = checked_prefix(prefix)?;
        let (id, label) = by_prefix(&tx, &pattern, Some("pending")).await?;
        tx.execute(
            "UPDATE maintainer_passkeys SET state='active',active_from=$1 WHERE credential_id=$2",
            &[&clock()?, &id],
        )
        .await?;
        key_change(&tx, "confirm", Some(&id), Some(&label), None).await?;
        Ok((id, label))
    }
    .await;
    finish(tx, result).await
}

async fn revoke(session: &mut pg::Session, prefix: &str) -> Result<(String, String), Stop> {
    let tx = session.client_mut().transaction().await?;
    let result = async {
        let pattern = checked_prefix(prefix)?;
        let (id, label) = by_prefix(&tx, &pattern, None).await?;
        let revoked = tx
            .execute(
                "UPDATE maintainer_passkeys SET state='revoked',revoked_at=$1,revoked_by='host' WHERE credential_id=$2 AND state<>'revoked'",
                &[&clock()?, &id],
            )
            .await?;
        if revoked != 0 {
            key_change(&tx, "revoke", Some(&id), Some(&label), None).await?;
        }
        Ok((id, label))
    }
    .await;
    finish(tx, result).await
}

async fn reset(session: &mut pg::Session) -> Result<u64, Stop> {
    let tx = session.client_mut().transaction().await?;
    let result = async {
        let revoked = tx
            .execute(
                "UPDATE maintainer_passkeys SET state='revoked',revoked_at=$1,revoked_by='host' WHERE state<>'revoked'",
                &[&clock()?],
            )
            .await?;
        tx.execute("DELETE FROM maintainer_bootstrap", &[]).await?;
        tx.execute("DELETE FROM meta WHERE key=$1", &[&SECRET_META_KEY])
            .await?;
        // `_secret()`: insert a fresh secret, then read it back and check it.
        tx.execute(
            "INSERT INTO meta (key,value) VALUES ($1,to_jsonb($2::text)) ON CONFLICT (key) DO NOTHING",
            &[&SECRET_META_KEY, &secret_hex()?],
        )
        .await?;
        let stored = tx
            .query_opt(
                "SELECT CASE WHEN jsonb_typeof(value)='string' THEN value #>> '{}' END AS secret FROM meta WHERE key=$1",
                &[&SECRET_META_KEY],
            )
            .await?
            .map(|row| row.try_get::<_, Option<String>>("secret"))
            .transpose()?
            .flatten();
        if !stored.is_some_and(|value| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        }) {
            return Err(Stop::Refused("coordination_unavailable"));
        }
        key_change(&tx, "reset", None, None, Some(revoked)).await?;
        Ok(revoked)
    }
    .await;
    finish(tx, result).await
}

async fn finish<T>(tx: Transaction<'_>, result: Result<T, Stop>) -> Result<T, Stop> {
    match result {
        Ok(value) => commit(tx).await.map(|()| value),
        Err(stop) => Err(rollback(tx, stop).await),
    }
}

/// `_key_change(change, ..., by="host")`: one `maintainer_key` audit event
/// from the operator, appended inside the change's transaction at the
/// chain head under the chain lock, stamped with its own clock read.
async fn key_change(
    tx: &Transaction<'_>,
    change: &str,
    credential_id: Option<&str>,
    label: Option<&str>,
    revoked: Option<u64>,
) -> Result<(), Stop> {
    // `_canonical`: sorted keys, compact, UTF-8 (serde_json's escapes are
    // json.dumps's with ensure_ascii=False).
    let payload = json!({
        "by": "host",
        "change": change,
        "credential_id": credential_id,
        "label": label,
        "path": "host",
        "revoked": revoked,
    })
    .to_string();
    let now = clock()?;
    let stamp = audit_timestamp(now).map_err(|_| Stop::Deferred)?;
    tx.query_one(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        &[&"coordination-audit-chain"],
    )
    .await?;
    let (seq, prev) = match tx
        .query_opt(
            "SELECT seq,hash FROM coordination_events ORDER BY seq DESC LIMIT 1",
            &[],
        )
        .await?
    {
        Some(row) => (
            row.try_get::<_, i64>("seq")?,
            row.try_get::<_, String>("hash")?,
        ),
        None => (0, "0".repeat(64)),
    };
    let seq = seq.checked_add(1).ok_or(Stop::Deferred)?;
    let hash = hex_hash(format!("{prev}{}", material(seq, &payload, &stamp)));
    tx.execute(
        "INSERT INTO coordination_events (seq,event,actor,principal,agent_id,recipient_agent_id,project,task,message_id,payload,created_at,hlc,prev_hash,hash) VALUES ($1,'maintainer_key','operator','','',NULL,'','',NULL,$2,$3,'',$4,$5)",
        &[&seq, &payload, &now, &prev, &hash],
    )
    .await?;
    Ok(())
}

/// `audit_hash`'s material for a host key change: `json.dumps([AUDIT_FORMAT,
/// seq, event, actor, principal, agent_id, recipient_agent_id, project,
/// task, message_id, float(created_at), hlc, payload])`, compact, with the
/// timestamp in CPython's float repr.
fn material(seq: i64, payload: &str, stamp: &str) -> String {
    let before = json!([
        "pseudolife-coordination-audit-v1",
        seq,
        "maintainer_key",
        "operator",
        "",
        "",
        Value::Null,
        "",
        "",
        Value::Null
    ])
    .to_string();
    let after = json!(["", payload]).to_string();
    format!("{},{stamp},{}", &before[..before.len() - 1], &after[1..])
}

async fn list(session: &mut pg::Session) -> Option<u8> {
    let rows = session
        .client()
        .query(
            "SELECT credential_id,label,state,enrolled_by,active_from,last_used_at,flagged_at FROM maintainer_passkeys ORDER BY created_at, credential_id",
            &[],
        )
        .await
        .ok()?;
    let clock = display::local_clock();
    let mut text = String::new();
    if rows.is_empty() {
        text.push_str("No passkeys.\n");
    }
    for row in &rows {
        let key = display::Key {
            credential_id: row.try_get("credential_id").ok()?,
            label: row.try_get("label").ok()?,
            state: row.try_get("state").ok()?,
            enrolled_by: row.try_get("enrolled_by").ok()?,
            active_from: row.try_get("active_from").ok()?,
            last_used_at: row.try_get("last_used_at").ok()?,
            flagged_at: row.try_get("flagged_at").ok()?,
        };
        text.push_str(&display::line(&key, &clock)?);
        text.push('\n');
    }
    listing_exit(write_report, &super::super::text_bytes(&text))
}

/// The listing's exit once written: CPython holds it all until its exit
/// flush, which a refused stdout fails (exit 120). Deferred, before any
/// byte is written, whenever it is too long for that buffer (8192 encoded
/// bytes or more, whatever stdout is: a healthy one too), and when stdout
/// refused it before any byte left.
fn listing_exit(write: impl FnOnce(&[u8]) -> Written, bytes: &[u8]) -> Option<u8> {
    if bytes.len() >= LISTING_BUFFER {
        return None;
    }
    match write(bytes) {
        Written::All => Some(0),
        Written::Nothing => None,
        Written::Part => Some(STDOUT_REFUSED),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_listing_defers_unless_python_would_hold_it_whole() {
        let short = vec![b'x'; LISTING_BUFFER - 1];
        assert_eq!(listing_exit(|_| Written::All, &short), Some(0));
        assert_eq!(listing_exit(|_| Written::Part, &short), Some(120));
        assert_eq!(listing_exit(|_| Written::Nothing, &short), None);
        let long = vec![b'x'; LISTING_BUFFER];
        assert_eq!(
            listing_exit(|_| panic!("a long listing is never written"), &long),
            None
        );
    }

    #[test]
    fn audit_material_matches_python_audit_hash() {
        // Recorded from the oracle: coordination.audit_hash(prev, row) for a
        // host confirm of a non-ASCII label at created_at=1791000000.25.
        let payload = json!({
            "by": "host",
            "change": "confirm",
            "credential_id": "-dashKeyAAAAAAAAAAAAAA",
            "label": "Tom's \"phone\" \u{260e}",
            "path": "host",
            "revoked": Value::Null,
        })
        .to_string();
        assert_eq!(
            payload,
            "{\"by\":\"host\",\"change\":\"confirm\",\"credential_id\":\"-dashKeyAAAAAAAAAAAAAA\",\"label\":\"Tom's \\\"phone\\\" \u{260e}\",\"path\":\"host\",\"revoked\":null}"
        );
        let stamp = audit_timestamp(1_791_000_000.25).unwrap();
        assert_eq!(
            material(7, &payload, &stamp),
            "[\"pseudolife-coordination-audit-v1\",7,\"maintainer_key\",\"operator\",\"\",\"\",null,\"\",\"\",null,1791000000.25,\"\",\"{\\\"by\\\":\\\"host\\\",\\\"change\\\":\\\"confirm\\\",\\\"credential_id\\\":\\\"-dashKeyAAAAAAAAAAAAAA\\\",\\\"label\\\":\\\"Tom's \\\\\\\"phone\\\\\\\" \u{260e}\\\",\\\"path\\\":\\\"host\\\",\\\"revoked\\\":null}\"]"
        );
        assert_eq!(
            hex_hash(format!(
                "{}{}",
                "0".repeat(64),
                material(7, &payload, &stamp)
            )),
            ORACLE_HASH
        );
    }

    /// `audit_hash("0"*64, row)` from the oracle for the row above.
    const ORACLE_HASH: &str = "cf4d0f1cbddab7b070293096c9d698c083fb84ff204b20da3dbcba755d263124";

    #[test]
    fn prefixes_are_checked_and_escaped_like_by_prefix() {
        assert_eq!(checked_prefix("ab_cd-e").ok().unwrap(), "ab\\_cd-e%");
        for refused in ["abcde", "", "a%b_cd", "abc def", "abcdé1", "abcdef\n"] {
            assert!(
                matches!(
                    checked_prefix(refused),
                    Err(Stop::Refused("invalid_request"))
                ),
                "{refused:?}"
            );
        }
    }

    #[test]
    fn a_bootstrap_code_is_ten_base32_symbols() {
        let code = bootstrap_code().unwrap();
        assert_eq!(code.len(), 10);
        assert!(code.bytes().all(|c| BASE32.contains(&c)));
        assert_eq!(secret_hex().ok().unwrap().len(), 64);
    }
}
