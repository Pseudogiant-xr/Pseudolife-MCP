use serde_json::Value;

pub const CHECKIN: &str = "Board: memory_agents update, list; memory_message receive, ack. Need what a peer holds? Message them you're next. Free? Use it, update status. Subagents only read the board.";
pub const PENDING: &str = "This session's board registration did not complete at startup (the memory daemon was unreachable or slow) and is being retried in the background; memory tools work meanwhile. Retry this call in a minute.";
pub const REGISTERED: &str = "Coordination: board registration completed after a startup delay; memory_agents update and memory_message now work here. Check in again if an earlier board call was refused.";
pub const STOPPED: &str = "Coordination: board registration stopped retrying (the daemon refused it; check bearer access and the private adapter state). Board writes here are refused until the session restarts; memory tools work.";
pub const SHARED_REFUSAL: &str = "This Pseudolife server is one process shared by every conversation in the host, and this shared shim has no authenticated per-conversation board binding: posting, status updates and mail here would act as every conversation at once, and are refused. Use a per-session Pseudolife server with a supported host session binding; in Claude Desktop its entry needs a separate name. memory_agents list here shows open sessions only, not the board.";
pub const SHARED_NOTE: &str = "Agent board: this server is shared by every conversation in its host and this shared shim has no authenticated per-conversation binding, so skip memory_agents update and memory_message here; memory tools work as usual.";
/// The refusal for a board call whose identity check still failed after its
/// retry: the cause, that nothing ran, and that the identity is not lost.
/// A proxy in front of the daemon can be the one answering, so the status is
/// the check's, not necessarily the daemon's.
pub fn identity_check_failed(status: Option<u16>) -> String {
    let cause = match status {
        Some(status) => format!("the check was answered with HTTP {status}"),
        None => "the check got no answer".to_owned(),
    };
    format!(
        "This session's board identity could not be checked with the memory daemon ({cause}), so this board call did not run. A failed check does not by itself mean the identity is lost: retry this call in a few seconds. If it keeps failing, check the daemon with `pseudolife-mcp doctor`."
    )
}
pub const RETRY_DELAYS: [u64; 6] = [1, 2, 5, 10, 30, 60];

pub fn yes(value: &str) -> bool {
    matches!(
        value.trim().to_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}
pub fn shared_host(writer: Option<&str>, configured: Option<&str>) -> bool {
    matches!(
        writer.unwrap_or("").trim().to_lowercase().as_str(),
        "claude-desktop" | "tunnel"
    ) || !matches!(
        configured.unwrap_or("").trim().to_lowercase().as_str(),
        "" | "0" | "false" | "no" | "off"
    )
}
pub fn requires_identity(name: &str, arguments: &Value) -> bool {
    name == "memory_message"
        || (name == "memory_agents"
            && matches!(
                arguments.get("action").and_then(Value::as_str),
                Some("update" | "claim" | "release")
            ))
}
pub fn canonical_uuid(candidate: &str) -> Option<String> {
    uuid::Uuid::parse_str(candidate)
        .ok()
        .map(|id| id.to_string())
        .filter(|id| id == candidate)
}
/// The hint a Codex thread's failed coordination step leaves (Python
/// `CodexCoordinationRegistry._note_failure`, #637). `validation` is an
/// existing thread's authority check; otherwise it is the thread's first
/// identity attachment. `code` is "timeout" when its deadline ran out.
pub fn registry_failure_hint(code: &str, status: Option<u16>, validation: bool) -> String {
    let origin = if validation {
        "authority validation"
    } else {
        "identity attachment"
    };
    match code {
        "bank_identity_mismatch" => "Coordination: bank or principal differs from the saved mailbox; state was preserved. Check the configured bank and credential.".to_owned(),
        "credential_unavailable" | "credential_changed" => "Coordination: credential source changed or is unavailable; saved identity was preserved. Retry after credential setup completes.".to_owned(),
        _ if matches!(code, "unauthorized" | "authentication_required" | "principal_not_allowed")
            || matches!(status, Some(401 | 403)) =>
        {
            format!("Coordination: {origin} was refused by authentication or access policy; saved state was preserved. Check the configured credential and access.")
        }
        "timeout" => format!("Coordination: {origin} timed out (timeout); saved state was preserved. Retry when the bank responds."),
        "transport_unavailable" | "context_unavailable" => format!("Coordination: {origin} failed ({code}); saved state was preserved. Retry when the bank responds."),
        "attachment_busy" if !validation => "Coordination: identity attachment is busy (attachment_busy); saved state was preserved. Wait for the prior attachment lease to expire.".to_owned(),
        _ => format!("Coordination: {origin} unavailable; saved state was preserved. Check bank access and private adapter state."),
    }
}
pub fn thread_id(meta: &Value) -> Option<String> {
    canonical_uuid(meta.get("threadId")?.as_str()?)
}
pub fn parent_thread(meta: &Value, thread: &str) -> Option<String> {
    let turn = meta.get("x-codex-turn-metadata")?;
    let decoded;
    let turn = if let Some(text) = turn.as_str() {
        if text.chars().count() > 8192 {
            return None;
        }
        decoded = serde_json::from_str::<Value>(text).ok()?;
        &decoded
    } else {
        turn
    };
    if turn.get("thread_source")?.as_str()? != "subagent"
        || canonical_uuid(turn.get("thread_id")?.as_str()?)?.as_str() != thread
    {
        return None;
    }
    canonical_uuid(turn.get("parent_thread_id")?.as_str()?).filter(|parent| parent != thread)
}

pub fn append_hint(result: &mut rmcp::model::CallToolResult, hint: &str) {
    result.content.push(rmcp::model::ContentBlock::text(hint));
    if let Some(object) = result
        .structured_content
        .as_mut()
        .and_then(Value::as_object_mut)
    {
        object
            .entry("coordination_hint")
            .or_insert_with(|| Value::String(hint.to_owned()));
    }
}

pub fn public_error(value: &str) -> Option<&'static str> {
    const CODES: &[&str] = &[
        "ambiguous_message_id",
        "ambiguous_recipient",
        "ambiguous_reply",
        "attachment_already_waiting",
        "attachment_busy",
        "attachment_required",
        "attempts_exhausted",
        "authentication_required",
        "bank_identity_mismatch",
        "child_send_refused",
        "coordination_requires_postgres",
        "coordination_unavailable",
        "fanout_too_large",
        "file_claim_requires_local_client",
        "instance_authentication_required",
        "instance_not_found",
        "invalid_active",
        "invalid_agent_id",
        "invalid_attachment_id",
        "invalid_bank_identity",
        "invalid_capabilities",
        "invalid_children",
        "invalid_claim_path",
        "invalid_clears",
        "invalid_credential",
        "invalid_cursor",
        "invalid_episode",
        "invalid_expect",
        "invalid_expiry",
        "invalid_generation",
        "invalid_hlc",
        "invalid_label",
        "invalid_lease",
        "invalid_limit",
        "invalid_message_id",
        "invalid_nonce",
        "invalid_parent",
        "invalid_park",
        "invalid_principal",
        "invalid_project",
        "invalid_purpose",
        "invalid_rebind",
        "invalid_recipient",
        "invalid_reply",
        "invalid_repository",
        "invalid_repository_id",
        "invalid_request",
        "invalid_request_id",
        "invalid_ring_armed_until",
        "invalid_status",
        "invalid_task",
        "invalid_text",
        "invalid_ttl",
        "invalid_update",
        "invalid_urgent",
        "invalid_wait_seconds",
        "invalid_wake_enabled",
        "lease_not_held",
        "lease_queue_full",
        "message_not_found",
        "message_not_pending",
        "missing_parameter",
        "no_recipients",
        "principal_not_allowed",
        "principals_unavailable",
        "queue_full",
        "rate_limited",
        "recipient_not_found",
        "request_conflict",
        "reserved_lease",
        "secret_like_body",
        "stale_attachment",
        "unauthorized",
        "unexpected_parameter",
        "unknown_coordination_action",
        "wait_capacity_exceeded",
        "wake_disabled",
    ];
    CODES.iter().copied().find(|code| *code == value)
}
