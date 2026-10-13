//! Board refusals and their public boundary (spec BT2): `CoordinationError`
//! and `CoordinationRefused` become one stable public code, with a detail
//! only beside a public code, never the text of an unexpected failure.

use serde_json::{Value, json};

/// `PUBLIC_ERROR_CODES` (`coordination.py:67-114`).
pub const PUBLIC_ERROR_CODES: &[&str] = &[
    "authentication_required",
    "unauthorized",
    "principal_not_allowed",
    "principals_unavailable",
    "instance_not_found",
    "invalid_credential",
    "unknown_coordination_action",
    "unexpected_parameter",
    "missing_parameter",
    "instance_authentication_required",
    "coordination_requires_postgres",
    "attachment_required",
    "attachment_busy",
    "stale_attachment",
    "attachment_already_waiting",
    "wait_capacity_exceeded",
    "invalid_wait_seconds",
    "invalid_capabilities",
    "invalid_children",
    "invalid_cursor",
    "invalid_expiry",
    "invalid_limit",
    "invalid_rebind",
    "invalid_reply",
    "invalid_text",
    "invalid_update",
    "invalid_wake_enabled",
    "invalid_active",
    "invalid_ring_armed_until",
    "invalid_message_id",
    "message_not_found",
    "message_not_pending",
    "queue_full",
    "rate_limited",
    "recipient_not_found",
    "request_conflict",
    "attempts_exhausted",
    "wake_disabled",
    "invalid_principal",
    "invalid_label",
    "invalid_project",
    "invalid_task",
    "invalid_status",
    "invalid_episode",
    "invalid_attachment_id",
    "invalid_recipient",
    "invalid_request_id",
    "invalid_hlc",
    "invalid_generation",
    "invalid_agent_id",
    "invalid_nonce",
    "invalid_bank_identity",
    "bank_identity_mismatch",
    "coordination_unavailable",
    "invalid_request",
    "invalid_lease",
    "invalid_ttl",
    "invalid_expect",
    "invalid_purpose",
    "lease_not_held",
    "lease_queue_full",
    "reserved_lease",
    "already_delegate",
    "invalid_repository",
    "invalid_repository_id",
    "invalid_claim_path",
    "file_claim_requires_local_client",
    "secret_like_body",
    "ambiguous_recipient",
    "ambiguous_message_id",
    "ambiguous_reply",
    "fanout_too_large",
    "no_recipients",
    "invalid_park",
    "invalid_clears",
    "invalid_urgent",
    "invalid_parent",
    "child_send_refused",
    "recipient_reserved",
    "invalid_name",
];

pub fn is_public(code: &str) -> bool {
    PUBLIC_ERROR_CODES.contains(&code)
}

/// A refusal with its optional public fields.
#[derive(Debug, Clone, PartialEq)]
pub struct Refusal {
    pub code: String,
    pub detail: Option<String>,
    pub param: Option<String>,
    pub accepted: Option<Vec<String>>,
    pub action: Option<String>,
}

#[derive(Debug)]
pub enum BoardError {
    /// `CoordinationError` / `CoordinationRefused` (both ValueErrors).
    Refused(Refusal),
    /// `CoordinationClockChanged`: a send retries once after a reseed.
    ClockChanged,
    /// Anything else: a database error, a broken invariant. Public
    /// `coordination_unavailable`, never its text.
    Internal(#[allow(dead_code)] String),
}

pub type Result<T> = std::result::Result<T, BoardError>;

impl BoardError {
    pub fn code(code: impl Into<String>) -> BoardError {
        BoardError::Refused(Refusal {
            code: code.into(),
            detail: None,
            param: None,
            accepted: None,
            action: None,
        })
    }

    pub fn detail(code: impl Into<String>, detail: impl Into<String>) -> BoardError {
        BoardError::Refused(Refusal {
            code: code.into(),
            detail: Some(detail.into()),
            param: None,
            accepted: None,
            action: None,
        })
    }

    /// A refusal with every optional field.
    pub fn full(
        code: &str,
        detail: Option<String>,
        param: Option<&str>,
        accepted: Option<Vec<String>>,
        action: Option<&str>,
    ) -> BoardError {
        BoardError::Refused(Refusal {
            code: code.to_string(),
            detail,
            param: param.map(str::to_string),
            accepted,
            action: action.map(str::to_string),
        })
    }

    pub fn with_param(mut self, param: &str) -> BoardError {
        if let BoardError::Refused(r) = &mut self {
            r.param = Some(param.to_string());
        }
        self
    }

    /// `public_refusal(exc)`: the public code and, beside a public code
    /// only, its detail, parameter, accepted values and action.
    pub fn public(&self) -> Refusal {
        match self {
            BoardError::Refused(r) if is_public(&r.code) => r.clone(),
            BoardError::Refused(_) => Refusal {
                code: "invalid_request".into(),
                detail: None,
                param: None,
                accepted: None,
                action: None,
            },
            BoardError::ClockChanged | BoardError::Internal(_) => Refusal {
                code: "coordination_unavailable".into(),
                detail: None,
                param: None,
                accepted: None,
                action: None,
            },
        }
    }
}

impl From<tokio_postgres::Error> for BoardError {
    fn from(e: tokio_postgres::Error) -> Self {
        BoardError::Internal(e.to_string())
    }
}

/// `_send_coordination_error`'s status for a public code.
pub fn status_for(code: &str) -> u16 {
    match code {
        "unauthorized" | "authentication_required" | "instance_authentication_required" => 401,
        "principal_not_allowed" | "invalid_credential" | "child_send_refused" => 403,
        "instance_not_found" => 404,
        "bank_identity_mismatch" => 409,
        "wait_capacity_exceeded" | "rate_limited" | "queue_full" | "lease_queue_full" => 429,
        "coordination_unavailable" | "invalid_bank_identity" => 500,
        "principals_unavailable" => 503,
        _ => 400,
    }
}

/// The REST body: `{"error": code}`, plus `"detail"` when present.
pub fn rest_body(refusal: &Refusal) -> Value {
    match &refusal.detail {
        Some(d) => json!({"error": refusal.code, "detail": d}),
        None => json!({"error": refusal.code}),
    }
}
