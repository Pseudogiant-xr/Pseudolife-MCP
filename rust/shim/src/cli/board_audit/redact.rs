//! `board-audit redact`: `CoordinationStore.redact` in one transaction, then
//! `_vacuum`, as `board_audit_cli._redact` runs them.
use super::{bank, codec};
use crate::{pg, sent_json::Json};
use serde_json::{Value, json};
use tokio_postgres::{GenericClient, error::SqlState};

const ID_CHARS: &str = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_";
const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";

/// How a step of the redaction ended before its commit.
enum Stop {
    /// A `CoordinationError` with its code and optional detail.
    Refused(&'static str, Option<String>),
    /// `psycopg.errors.LockNotAvailable` under the five-second lock timeout.
    Busy,
    /// Outside the reproduced domain; the transaction is rolled back.
    Deferred,
    /// The commit was not confirmed: the redaction may have landed.
    Unconfirmed,
}

impl From<tokio_postgres::Error> for Stop {
    fn from(error: tokio_postgres::Error) -> Self {
        if error.code() == Some(&SqlState::LOCK_NOT_AVAILABLE) {
            Stop::Busy
        } else {
            Stop::Deferred
        }
    }
}

struct Redacted {
    message_id: String,
    seq: Option<i64>,
    redact_seq: i64,
    redact_hash: String,
    cleared: bool,
    audit_copy: &'static str,
    other_copies: Option<Vec<String>>,
}

fn python_whitespace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// The reason refusals, in the producer's order; `Err` defers a reason whose
/// verdict needs the Cf/Co/Cn split the pinned category-C table cannot make.
fn reason_refusal(reason: &str) -> Result<Option<&'static str>, ()> {
    let certain = |c: char| {
        c <= '\u{1f}' || ('\u{7f}'..='\u{9f}').contains(&c) || c == '\u{2028}' || c == '\u{2029}'
    };
    if reason.chars().all(python_whitespace)
        || reason.chars().count() > 240
        || reason.chars().any(certain)
    {
        return Ok(Some("invalid_reason"));
    }
    if reason.chars().any(crate::board::claims::forbidden) {
        return Err(());
    }
    if crate::cli::lease::operator::secrets::refused(reason) {
        return Ok(Some("secret_like_body"));
    }
    Ok(None)
}

fn is_id_prefix(value: &str) -> bool {
    (8..32).contains(&value.len())
        && value
            .bytes()
            .all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'))
}

fn clock() -> Result<f64, Stop> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| Stop::Deferred)?;
    Ok(now.as_secs_f64())
}

/// `_append` of one redact event at the chain head the caller locked.
async fn append_redact(
    tx: &impl GenericClient,
    head: (i64, String),
    now: f64,
    fields: [Option<String>; 4],
    message_id: &str,
    payload: &Value,
) -> Result<(i64, String), Stop> {
    let [agent_id, recipient, project, task] = fields;
    let seq = head.0.checked_add(1).ok_or(Stop::Deferred)?;
    let payload = codec::canonical(payload).map_err(|_| Stop::Deferred)?;
    let row = json!({
        "seq": seq,
        "event": "redact",
        "actor": "operator",
        "principal": "",
        "agent_id": agent_id.clone().unwrap_or_default(),
        "recipient_agent_id": recipient,
        "project": project.clone().unwrap_or_default(),
        "task": task.clone().unwrap_or_default(),
        "message_id": message_id,
        "hlc": "",
        "payload": payload,
        "prev_hash": head.1,
    });
    let hash = codec::hash(&row, now).map_err(|_| Stop::Deferred)?;
    tx.execute(
        "INSERT INTO coordination_events (seq,event,actor,principal,agent_id,recipient_agent_id,project,task,message_id,payload,created_at,hlc,prev_hash,hash) VALUES ($1,'redact','operator','',$2,$3,$4,$5,$6,$7,$8,'',$9,$10)",
        &[
            &seq,
            &agent_id.unwrap_or_default(),
            &row["recipient_agent_id"].as_str(),
            &project.unwrap_or_default(),
            &task.unwrap_or_default(),
            &message_id,
            &payload,
            &now,
            &head.1,
            &hash,
        ],
    )
    .await?;
    Ok((seq, hash))
}

async fn resolve_message(client: &impl GenericClient, value: &str) -> Result<String, Stop> {
    if !is_id_prefix(value) {
        return Ok(value.to_owned());
    }
    let like = format!("{value}%");
    let rows = client
        .query(
            "SELECT message_id FROM (SELECT message_id FROM coordination_messages WHERE message_id LIKE $1 UNION SELECT message_id FROM coordination_events WHERE event='send' AND message_id LIKE $1) AS ids ORDER BY message_id LIMIT 9",
            &[&like],
        )
        .await?;
    let ids: Vec<String> = rows
        .iter()
        .map(|row| row.try_get::<_, String>(0))
        .collect::<Result<_, _>>()?;
    match ids.len() {
        0 => Err(Stop::Refused("message_not_found", None)),
        1 => Ok(ids[0].clone()),
        _ => Err(Stop::Refused(
            "ambiguous_message_id",
            Some(bank::distinguishing_prefixes(&ids[..ids.len().min(8)]).join(", ")),
        )),
    }
}

async fn redact_in(
    session: &mut pg::Session,
    message_id: &str,
    reason: &str,
) -> Result<Redacted, Stop> {
    if message_id.is_empty()
        || message_id.chars().count() > 120
        || !message_id.chars().all(|c| ID_CHARS.contains(c))
    {
        return Err(Stop::Refused("invalid_message_id", None));
    }
    match reason_refusal(reason) {
        Err(()) => return Err(Stop::Deferred),
        Ok(Some(code)) => return Err(Stop::Refused(code, None)),
        Ok(None) => {}
    }
    let message_id = resolve_message(session.client(), message_id).await?;
    let tx = session.client_mut().transaction().await?;
    let now = clock()?;
    let live = tx
        .query_opt(
            "SELECT text FROM coordination_messages WHERE message_id=$1 FOR UPDATE",
            &[&message_id],
        )
        .await?
        .map(|row| row.try_get::<_, Option<String>>("text"))
        .transpose()?;
    tx.query_one(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        &[&"coordination-audit-chain"],
    )
    .await?;
    let head = match tx
        .query_opt(
            "SELECT seq,hash FROM coordination_events ORDER BY seq DESC LIMIT 1",
            &[],
        )
        .await?
    {
        Some(row) => (row.try_get::<_, i64>("seq")?, row.try_get("hash")?),
        None => (0, GENESIS.to_owned()),
    };
    let sent = tx
        .query_opt(
            "SELECT seq,payload,agent_id,recipient_agent_id,project,task FROM coordination_events WHERE event='send' AND message_id=$1 ORDER BY seq LIMIT 1",
            &[&message_id],
        )
        .await?;
    let Some(sent) = sent else {
        let Some(live_row) = tx
            .query_opt(
                "SELECT text,fingerprint,sender_agent_id,recipient_agent_id,project,task FROM coordination_messages WHERE message_id=$1",
                &[&message_id],
            )
            .await?
        else {
            return Err(Stop::Refused("message_not_found", None));
        };
        let text: Option<String> = live_row.try_get("text")?;
        let fingerprint: String = live_row.try_get("fingerprint")?;
        if text.is_none() && fingerprint == "redacted" {
            return Err(Stop::Refused("already_redacted", None));
        }
        tx.execute(
            "UPDATE coordination_messages SET text=NULL,fingerprint='redacted',expires_at=LEAST(expires_at,$1) WHERE message_id=$2",
            &[&now, &message_id],
        )
        .await?;
        let payload =
            json!({"message_id": message_id, "seq": null, "reason": reason, "audit_copy": "gone"});
        let fields = [
            live_row.try_get("sender_agent_id")?,
            live_row.try_get("recipient_agent_id")?,
            live_row.try_get("project")?,
            live_row.try_get("task")?,
        ];
        let (redact_seq, redact_hash) =
            append_redact(&tx, head, now, fields, &message_id, &payload).await?;
        commit(tx).await?;
        return Ok(Redacted {
            message_id,
            seq: None,
            redact_seq,
            redact_hash,
            cleared: text.is_some(),
            audit_copy: "gone",
            other_copies: None,
        });
    };
    let sent_seq: i64 = sent.try_get("seq")?;
    let stored: String = sent.try_get("payload")?;
    let payload = match codec::python_loads(&stored).ok_or(Stop::Deferred)? {
        Value::Object(map) => Some(map),
        _ => None,
    };
    let audit_copy = if payload
        .as_ref()
        .is_none_or(|p| !p.contains_key("text_commitment"))
    {
        if !matches!(live, Some(Some(_))) {
            return Err(Stop::Refused("body_in_hashed_payload", None));
        }
        "kept"
    } else if tx
        .query_opt(
            "UPDATE coordination_events SET body=NULL,body_salt=NULL WHERE seq=$1 AND (body IS NOT NULL OR body_salt IS NOT NULL) RETURNING seq",
            &[&sent_seq],
        )
        .await?
        .is_none()
    {
        return Err(Stop::Refused("already_redacted", None));
    } else {
        "removed"
    };
    let mut cleared = false;
    if let Some(text) = &live {
        cleared = text.is_some();
        tx.execute(
            "UPDATE coordination_messages SET text=NULL,fingerprint='redacted',expires_at=LEAST(expires_at,$1) WHERE message_id=$2",
            &[&now, &message_id],
        )
        .await?;
    }
    let event = json!({"message_id": message_id, "seq": sent_seq, "reason": reason, "audit_copy": audit_copy});
    let sender: Option<String> = sent.try_get("agent_id")?;
    let fields = [
        sender.clone(),
        sent.try_get("recipient_agent_id")?,
        sent.try_get("project")?,
        sent.try_get("task")?,
    ];
    let (redact_seq, redact_hash) =
        append_redact(&tx, head, now, fields, &message_id, &event).await?;
    // `_burst_siblings`: the other messages of a fan-out send's burst.
    let mut siblings = Vec::new();
    if let Some(payload) = payload.as_ref().filter(|p| p.contains_key("fanout")) {
        let request: Option<&str> = match payload.get("request_id") {
            None | Some(Value::Null) => None,
            Some(Value::String(text)) => Some(text),
            Some(_) => return Err(Stop::Deferred),
        };
        for row in tx
            .query(
                "SELECT message_id FROM coordination_events WHERE event='send' AND agent_id=$1 AND message_id<>$2 AND payload::jsonb->>'request_id'=$3 ORDER BY seq",
                &[&sender, &message_id, &request],
            )
            .await?
        {
            siblings.push(row.try_get::<_, String>(0)?);
        }
    }
    commit(tx).await?;
    Ok(Redacted {
        message_id,
        seq: Some(sent_seq),
        redact_seq,
        redact_hash,
        cleared,
        audit_copy,
        other_copies: Some(siblings),
    })
}

/// A commit the server did not confirm is reported, never deferred: the
/// redaction may have landed.
async fn commit(tx: pg::Transaction<'_>) -> Result<(), Stop> {
    tx.commit().await.map_err(|_| Stop::Unconfirmed)
}

/// `_vacuum`: the warnings Postgres raised while skipping a step, or `Err`
/// when a statement failed.
async fn vacuum(session: &pg::Session) -> Result<Vec<String>, ()> {
    let client = session.client();
    let previous: String = client
        .query_one("SHOW client_min_messages", &[])
        .await
        .and_then(|row| row.try_get(0))
        .map_err(|_| ())?;
    let _ = session.take_notices();
    let steps = async {
        client
            .batch_execute("SET client_min_messages TO warning")
            .await?;
        client
            .batch_execute("VACUUM (ANALYZE) coordination_events, coordination_messages")
            .await?;
        client.batch_execute("VACUUM pg_catalog.pg_statistic").await
    }
    .await;
    let warnings: Vec<String> = session
        .take_notices()
        .into_iter()
        .filter(|notice| notice.parsed_severity() == Some(tokio_postgres::error::Severity::Warning))
        .map(|notice| notice.message().to_owned())
        .collect();
    let restored = client
        .execute(
            "SELECT set_config('client_min_messages', $1, false)",
            &[&previous],
        )
        .await;
    steps.map_err(|_| ())?;
    restored.map_err(|_| ())?;
    Ok(warnings)
}

pub(super) async fn run(dsn: &pg::Dsn, message_id: &str, reason: &str) -> bank::Ending {
    let Ok(mut session) = pg::Session::open(dsn).await else {
        return bank::Ending::Deferred;
    };
    let ending = run_in(&mut session, message_id, reason).await;
    let _ = session.close().await;
    ending
}

async fn run_in(session: &mut pg::Session, message_id: &str, reason: &str) -> bank::Ending {
    match bank::log_present(session.client()).await {
        Ok(true) => {}
        Ok(false) => return bank::no_log(),
        Err(()) => return bank::Ending::Deferred,
    }
    match bank::has_body_column(session.client()).await {
        Ok(true) => {}
        Ok(false) => {
            crate::stderrln!(
                "board-audit: this bank's audit log predates schema v46, so no body in it can be redacted: every send body is part of the hashed payload. Start a v46 daemon once to add the body column; bodies sent before it stay unredactable"
            );
            return bank::Ending::Exit(2);
        }
        Err(()) => return bank::Ending::Deferred,
    }
    let redacted = match redact_in(session, message_id, reason).await {
        Ok(redacted) => redacted,
        Err(Stop::Deferred) => return bank::Ending::Deferred,
        Err(Stop::Busy) => {
            crate::stderrln!(
                "board-audit: the board is busy: a row or the audit chain stayed locked for more than 5 s. Nothing was changed; retry"
            );
            return bank::Ending::Exit(2);
        }
        Err(Stop::Unconfirmed) => {
            crate::stderrln!(
                "board-audit failed; the database error is not shown because it can include the connection string. Check PSEUDOLIFE_MCP_DATABASE_URL."
            );
            return bank::Ending::Exit(2);
        }
        Err(Stop::Refused(code, detail)) => {
            let line = Json::Object(vec![
                ("ok".into(), Json::Bool(false)),
                ("message_id".into(), Json::String(message_id.into())),
                ("reason".into(), Json::String(code.into())),
            ]);
            let explained = match code {
                "invalid_message_id" => {
                    "--message-id must be one message id as the audit log shows it"
                }
                "invalid_reason" => {
                    "--reason must be 1-240 characters on one line, with no control, format or separator characters"
                }
                "secret_like_body" => {
                    "the reason looks like it holds a credential, and the reason is kept in the log for good; describe the mistake without repeating it"
                }
                "message_not_found" => {
                    "no record of this message id: an unknown id, or one whose send event audit retention removed and whose live copy is gone too"
                }
                "body_in_hashed_payload" => {
                    "this message was sent before schema v46, when the body was part of the hashed payload; removing it would break the chain, so it stays until audit retention removes the event, and its live copy is already gone"
                }
                "already_redacted" => "this message's body is already redacted",
                other => other,
            };
            // Nothing was changed (rolled back), so a stdout that refuses
            // the refusal line defers.
            if super::print_line(&line).is_err() {
                return bank::Ending::Deferred;
            }
            match detail {
                Some(detail) => crate::stderrln!("board-audit: {explained}: {detail}"),
                None => crate::stderrln!("board-audit: {explained}"),
            }
            return bank::Ending::Exit(1);
        }
    };
    let (skipped, vacuumed) = match vacuum(session).await {
        Ok(skipped) => {
            let vacuumed = skipped.is_empty();
            (skipped, vacuumed)
        }
        Err(()) => (Vec::new(), false),
    };
    let expect_head = format!("{}:{}", redacted.redact_seq, redacted.redact_hash);
    let mut fields = vec![
        ("ok".into(), Json::Bool(true)),
        (
            "message_id".into(),
            Json::String(redacted.message_id.clone()),
        ),
        (
            "seq".into(),
            redacted
                .seq
                .map_or(Json::Null, |seq| Json::Integer(seq.to_string())),
        ),
        (
            "redact_seq".into(),
            Json::Integer(redacted.redact_seq.to_string()),
        ),
        (
            "redact_hash".into(),
            Json::String(redacted.redact_hash.clone()),
        ),
        ("expect_head".into(), Json::String(expect_head.clone())),
        ("live_body_cleared".into(), Json::Bool(redacted.cleared)),
        (
            "audit_copy".into(),
            Json::String(redacted.audit_copy.into()),
        ),
    ];
    if let Some(copies) = &redacted.other_copies {
        fields.push((
            "other_copies".into(),
            Json::Array(copies.iter().cloned().map(Json::String).collect()),
        ));
    }
    fields.push(("vacuumed".into(), Json::Bool(vacuumed)));
    // The redaction is committed. CPython's print is buffered, so a stdout
    // that refuses it fails only in the interpreter's shutdown flush, after
    // every note below: exit 120 (its ignored-exception trailer is the
    // declared substitution `audit-stdout-closed-trailer`). Never exit 1,
    // which would read as a refusal.
    let code = if super::print_line(&Json::Object(fields)).is_ok() {
        0
    } else {
        120
    };
    if redacted.audit_copy == "kept" {
        crate::stderrln!(
            "board-audit: the live copy is blanked and out of delivery, but this message was sent before schema v46: the audit log keeps its body until audit retention removes the send event"
        );
    }
    if let Some(copies) = redacted.other_copies.as_ref().filter(|c| !c.is_empty()) {
        crate::stderrln!(
            "board-audit: this message was one of {} copies of one send to a project or the whole board; each keeps its own copy of the body until it is redacted too: {}",
            copies.len() + 1,
            copies.join(" ")
        );
    }
    if redacted.audit_copy == "gone" {
        crate::stderrln!(
            "board-audit: audit retention had already removed this message's send event, so the audit log held no copy of it; its live request fingerprint (and any live text) is blanked"
        );
    }
    if !skipped.is_empty() {
        crate::stderrln!(
            "board-audit: the redaction is committed, but Postgres skipped part of the clean-up ({}), so the old row versions or the statistics may still hold the body; run `VACUUM (ANALYZE) coordination_events, coordination_messages` and `VACUUM pg_statistic` as the tables' owner or a superuser",
            skipped.join("; ")
        );
    } else if !vacuumed {
        crate::stderrln!(
            "board-audit: the redaction is committed, but the VACUUM that frees the old row versions and rebuilds the statistics failed (the board may be busy); run `VACUUM (ANALYZE) coordination_events, coordination_messages` and `VACUUM pg_statistic` later"
        );
    }
    crate::stderrln!(
        "board-audit: record this head outside the bank; `pseudolife-mcp board-audit verify --expect-head {expect_head}` later shows the redaction record is still there"
    );
    bank::Ending::Exit(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reason_refusals_and_category_deferrals() {
        assert_eq!(reason_refusal("wrong paste"), Ok(None));
        assert_eq!(reason_refusal(" \u{1c} "), Ok(Some("invalid_reason")));
        assert_eq!(reason_refusal("bell\u{7}"), Ok(Some("invalid_reason")));
        assert_eq!(reason_refusal("line\u{2028}"), Ok(Some("invalid_reason")));
        assert_eq!(reason_refusal(&"x".repeat(241)), Ok(Some("invalid_reason")));
        assert_eq!(reason_refusal(&"é".repeat(240)), Ok(None));
        // Cf (refused) and Co (accepted) share the pinned category-C table.
        assert_eq!(reason_refusal("zero\u{200b}width"), Err(()));
        assert_eq!(reason_refusal("private\u{e000}"), Err(()));
        assert_eq!(
            reason_refusal("token=q7Hd2kLm9Pz4Rt6Wv8Xy1Bc3"),
            Ok(Some("secret_like_body"))
        );
    }
}
