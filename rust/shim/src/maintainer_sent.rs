//! SQL/projection for the initialized-bank native sent HTTP boundary.
use crate::sent_json::Json;

pub const SENT_SQL: &str = "SELECT m.*,a.label AS recipient_label FROM coordination_messages m \
LEFT JOIN coordination_agents a ON a.agent_id=m.recipient_agent_id \
WHERE m.origin=$1 AND m.sender_principal=$2 \
ORDER BY m.created_at DESC, m.message_id LIMIT $3";

// All deferred paths share this constant body; no path or private data is echoed.
pub const DEFERRED_BODY: &[u8] = br#"{"error": "route_deferred", "candidate": "rust-maintainer-sent", "deferred_route": "all paths other than /api/maintainer/sent"}"#;

pub struct Row {
    pub message_id: String,
    pub recipient_agent_id: String,
    // Null is only a fixture/schema-compatibility control in the current schema.
    pub recipient_label: Option<String>,
    pub created_at: f64,
    pub maintainer_proof: Json,
    pub text: Option<String>,
    pub wake: Json,
    pub first_read_at: Option<f64>,
    pub acknowledged_at: Option<f64>,
    pub repudiated_at: Option<f64>,
}

#[derive(Debug)]
pub struct InvalidProof;

fn proof_label(proof: &Json) -> Result<Json, InvalidProof> {
    match proof {
        Json::Object(members) => Ok(members
            .iter()
            .find(|(key, _)| key == "label")
            .map_or(Json::Null, |(_, value)| value.clone())),
        Json::Null | Json::Bool(false) => Ok(Json::Null),
        Json::Integer(value) if value == "0" => Ok(Json::Null),
        Json::Float(value) if *value == 0.0 => Ok(Json::Null),
        Json::String(value) if value.is_empty() => Ok(Json::Null),
        Json::Array(value) if value.is_empty() => Ok(Json::Null),
        _ => Err(InvalidProof),
    }
}

fn timestamp(value: Option<f64>) -> Json {
    value.map_or(Json::Null, Json::Float)
}

pub fn project(row: Row) -> Result<Json, InvalidProof> {
    let label = proof_label(&row.maintainer_proof)?;
    Ok(Json::Object(vec![
        ("message_id".into(), Json::String(row.message_id)),
        (
            "recipient_agent_id".into(),
            Json::String(row.recipient_agent_id),
        ),
        (
            "recipient_label".into(),
            row.recipient_label.map_or(Json::Null, Json::String),
        ),
        ("created_at".into(), Json::Float(row.created_at)),
        ("label".into(), label),
        ("text".into(), row.text.map_or(Json::Null, Json::String)),
        ("wake".into(), row.wake),
        ("first_read_at".into(), timestamp(row.first_read_at)),
        ("acknowledged_at".into(), timestamp(row.acknowledged_at)),
        ("repudiated_at".into(), timestamp(row.repudiated_at)),
    ]))
}

pub fn page(rows: Vec<Row>) -> Result<Json, InvalidProof> {
    let messages = rows
        .into_iter()
        .map(project)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(Json::Object(vec![(
        "messages".into(),
        Json::Array(messages),
    )]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proof_truthiness_matches_python_projection() {
        for value in [
            Json::Null,
            Json::Bool(false),
            Json::Integer("0".into()),
            Json::Float(-0.0),
            Json::String(String::new()),
            Json::Array(vec![]),
            Json::Object(vec![]),
        ] {
            assert!(matches!(proof_label(&value), Ok(Json::Null)));
        }
        for value in [
            Json::Bool(true),
            Json::Integer("1".into()),
            Json::String("x".into()),
            Json::Array(vec![Json::Null]),
        ] {
            assert!(proof_label(&value).is_err());
        }
        assert!(matches!(
            proof_label(&Json::Object(vec![("label".into(), Json::Bool(false))])),
            Ok(Json::Bool(false))
        ));
    }

    #[test]
    fn projection_retains_ten_fields_nullable_text_and_order() {
        let row = Row {
            message_id: "m".into(),
            recipient_agent_id: "a".into(),
            recipient_label: None,
            created_at: 1.0,
            maintainer_proof: Json::Bool(false),
            text: None,
            wake: Json::Null,
            first_read_at: None,
            acknowledged_at: None,
            repudiated_at: None,
        };
        assert_eq!(crate::sent_json::encode(&project(row).unwrap()).unwrap(),
            br#"{"message_id": "m", "recipient_agent_id": "a", "recipient_label": null, "created_at": 1.0, "label": null, "text": null, "wake": null, "first_read_at": null, "acknowledged_at": null, "repudiated_at": null}"#);
    }
}
