//! Mail writes inside the nonce and signature-counter transaction.
use super::*;
use crate::board::mail::SendArgs;
use crate::board::store::{Event, Store, append};
use tokio_postgres::Transaction;

fn board_error(e: BoardError) -> Refusal {
    match e {
        BoardError::Refused(r) => refuse(
            match r.code.as_str() {
                "instance_not_found" | "invalid_credential" => "_identity",
                "recipient_not_found" | "agent_revoked" => "recipient_unknown",
                "recipient_reserved" => "recipient_reserved",
                "rate_limited" | "queue_full" => "rate_capped",
                _ => "invalid_request",
            },
            "board",
        ),
        e => Refusal::Board(e),
    }
}

pub(super) async fn send(
    store: &Store,
    tx: &Transaction<'_>,
    fields: &Map<String, Value>,
    proof: &Value,
    identity: &(String, String),
    stamp: (String, i64),
) -> Outcome<Value> {
    if !fields.get("to").is_some_and(Value::is_string)
        || !fields.get("urgent").is_some_and(Value::is_boolean)
    {
        return Err(refuse("assertion_invalid", "payload_fields"));
    }
    recipient_row(tx, fields.get("to")).await?;
    board_send(
        store,
        tx,
        &fields["to"],
        fields.get("text").unwrap_or(&Value::Null),
        &fields["urgent"],
        &fields["nonce"],
        proof,
        identity,
        stamp,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn board_send(
    store: &Store,
    tx: &Transaction<'_>,
    to: &Value,
    text: &Value,
    urgent: &Value,
    nonce: &Value,
    proof: &Value,
    identity: &(String, String),
    stamp: (String, i64),
) -> Outcome<Value> {
    let result = store
        .send_in(
            tx,
            MAINTAINER_PRINCIPAL,
            &identity.0,
            &identity.1,
            SendArgs {
                to: Some(to),
                text,
                urgent: Some(urgent),
                request_id: nonce,
                reply_to: None,
                clears: None,
                hlc: stamp.0,
                expected_writer_epoch: Some(stamp.1),
                enforce_writer_epoch: true,
                notice: false,
                maintainer_proof: Some(proof.clone()),
            },
        )
        .await
        .map_err(board_error)?;
    Ok(
        json!({"message_id":result["message_id"],"wake":result.get("wake"),
        "_notify":[result["recipient_agent_id"]]}),
    )
}

#[allow(clippy::too_many_arguments)]
pub(super) async fn repudiate(
    store: &Store,
    tx: &Transaction<'_>,
    fields: &Map<String, Value>,
    proof: &Value,
    identity: &(String, String),
    stamp: (String, i64),
    principal: &str,
) -> Outcome<Value> {
    let id = fields
        .get("message_id")
        .and_then(Value::as_str)
        .ok_or_else(|| refuse("message_not_found", "message_id"))?;
    let row = tx.query_opt("SELECT recipient_agent_id FROM coordination_messages WHERE message_id=$1 AND origin=$2 AND sender_principal=$3",
        &[&id,&MAINTAINER_ORIGIN,&MAINTAINER_PRINCIPAL]).await?
        .ok_or_else(|| refuse("message_not_found", "message_id"))?;
    let recipient: String = row.get(0);
    let mut agents = vec![recipient.clone(), identity.0.clone()];
    agents.sort();
    agents.dedup();
    tx.query("SELECT agent_id FROM coordination_agents WHERE agent_id=ANY($1) ORDER BY agent_id FOR UPDATE", &[&agents]).await?;
    let row = tx.query_one("SELECT acknowledged_at,repudiated_at FROM coordination_messages WHERE message_id=$1 FOR UPDATE", &[&id]).await?;
    let acknowledged: Option<f64> = row.get(0);
    let old: Option<f64> = row.get(1);
    let at = old.unwrap_or_else(crate::board::store::now);
    if old.is_none() {
        tx.execute(
            "UPDATE coordination_messages SET repudiated_at=$1 WHERE message_id=$2",
            &[&at, &id],
        )
        .await?;
    }
    let follow = if acknowledged.is_some() {
        Some(
            board_send(
                store,
                tx,
                &json!(recipient),
                &json!(format!(
                    "The maintainer withdrew message {id}; do not act on it."
                )),
                &json!(false),
                &fields["nonce"],
                proof,
                identity,
                stamp,
            )
            .await?,
        )
    } else {
        None
    };
    if old.is_none() {
        append(
            tx,
            vec![
                Event::new("maintainer_repudiate", &json!({"message_id":id}))
                    .actor("maintainer")
                    .principal(principal)
                    .recipient(&recipient)
                    .message(id),
            ],
            at,
            None,
        )
        .await?;
    }
    Ok(json!({"message_id":id,"repudiated_at":at,
        "follow_up":follow.as_ref().map(|f| &f["message_id"]),
        "_notify":if follow.is_some(){vec![recipient]}else{vec![]}}))
}
