use crate::raw_http::Observation;
use rmcp::{ErrorData, model::ErrorCode, service::ServiceError};

fn nested_io_failure(error: Option<&ServiceError>) -> Option<&'static str> {
    let Some(ServiceError::TransportSend(error)) = error else {
        return None;
    };
    let mut current: Option<&(dyn std::error::Error + 'static)> = Some(error.error.as_ref());
    let mut connection = false;
    while let Some(error) = current {
        if let Some(io) = error.downcast_ref::<std::io::Error>() {
            use std::io::ErrorKind;
            match io.kind() {
                ErrorKind::TimedOut => return Some("timeout"),
                ErrorKind::ConnectionReset
                | ErrorKind::ConnectionAborted
                | ErrorKind::ConnectionRefused
                | ErrorKind::BrokenPipe
                | ErrorKind::NotConnected
                | ErrorKind::UnexpectedEof => connection = true,
                _ => {}
            }
        }
        current = error.source();
    }
    connection.then_some("connection_failure")
}

fn exact_refusal(error: Option<&ServiceError>) -> Option<i32> {
    let Some(ServiceError::McpError(error)) = error else {
        return None;
    };
    let (name, status) = match error.code.0 {
        -32003 => ("principals_unavailable", 503),
        -32004 => ("unauthorized", 401),
        _ => return None,
    };
    (error.message == name
        && error.data.as_ref() == Some(&serde_json::json!({"status": status,"error":name})))
    .then_some(error.code.0)
}
pub(crate) fn map(
    observation: &Observation,
    error: Option<ServiceError>,
    requested: &str,
    timed_out: bool,
    credential_failed: bool,
) -> ErrorData {
    let refusal = exact_refusal(error.as_ref());
    let nested_failure = nested_io_failure(error.as_ref());
    let classification = if credential_failed {
        "credential_unavailable"
    } else if refusal == Some(-32004) {
        "revoked"
    } else if refusal == Some(-32003)
        || observation.status == Some(503)
            && observation.refused_phase.is_some()
            && observation.refused_phase == Some(observation.phase)
    {
        "principals_unavailable"
    } else {
        match observation.status {
            Some(401 | 403) => "authentication_required",
            Some(429 | 502 | 503 | 504) => "service_unavailable",
            _ if observation.failure == Some("connection_failure") => "connection_failure",
            _ if timed_out
                || observation.failure == Some("timeout")
                || nested_failure == Some("timeout") =>
            {
                "timeout"
            }
            _ if nested_failure == Some("connection_failure") => "connection_failure",
            _ if matches!(&error, Some(ServiceError::TransportClosed))
                || matches!(&error, Some(ServiceError::McpError(error)) if error.code.0 == -32000) =>
            {
                "response_lost"
            }
            _ => "protocol",
        }
    };
    let code = match error {
        Some(ServiceError::McpError(error)) if classification == "protocol" => error.code,
        _ if classification == "protocol" && observation.status == Some(404) => {
            if observation.session.is_some() {
                ErrorCode::INVALID_REQUEST
            } else {
                ErrorCode::METHOD_NOT_FOUND
            }
        }
        _ => ErrorCode::INTERNAL_ERROR,
    };
    failure(
        classification,
        observation.response_phase.unwrap_or(observation.phase),
        requested == "call" && observation.dispatched,
        code,
    )
}
pub(crate) fn failure(
    classification: &str,
    phase: &str,
    mut unknown: bool,
    mut code: ErrorCode,
) -> ErrorData {
    let lost = "The memory daemon's response stream closed before a result arrived (a dropped connection, or a result larger than the client's event limit).";
    let mut message = match classification {
        "credential_unavailable" => "The memory credential is unavailable; restore the configured credential and retry.",
        "revoked" => { unknown = false; code = ErrorCode(-32004); "The memory daemon no longer accepts this token, so it refused this request before running it. No tool ran; pair this machine again or refresh the configured credential." },
        "principals_unavailable" => { unknown = false; code = ErrorCode(-32003); "The memory daemon refused this request before running it: it cannot check invited machines' tokens right now. No tool ran; retry shortly." },
        "authentication_required" => "Memory daemon authentication is required; refresh the configured credential and retry.",
        "service_unavailable" => "The memory daemon is temporarily unavailable; retry this operation.",
        "connection_failure" => "The memory daemon connection failed; check the daemon and retry.",
        "timeout" => "The memory daemon did not respond before the operation timeout.",
        "response_lost" => lost,
        _ => "The memory daemon returned an invalid MCP response.",
    }.to_owned();
    if unknown {
        message = "The memory operation may have completed before the response failed. Check its result before retrying; reuse the same request_id when available.".to_owned();
        if classification == "response_lost" {
            message = format!("{lost} {message}");
        }
    }
    let mut data = serde_json::json!({"classification":if classification=="revoked" {"authentication_required"} else {classification}, "phase":phase, "operation_outcome":if unknown {"unknown"} else {"not_dispatched"}});
    if classification == "principals_unavailable" {
        data["status"] = serde_json::json!(503);
        data["error"] = serde_json::json!("principals_unavailable");
        data["hint"] = serde_json::json!(
            "the daemon cannot check invited machines' tokens right now (its database is not answering); try again shortly"
        );
    } else if classification == "revoked" {
        data["status"] = serde_json::json!(401);
        data["error"] = serde_json::json!("unauthorized");
    }
    ErrorData::new(code, message, Some(data))
}
pub(crate) fn never_reached(error: &ErrorData) -> bool {
    error.data.as_ref().is_some_and(|data| {
        data["classification"] == "connection_failure"
            && data["operation_outcome"] == "not_dispatched"
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn test_sanitizer_flattens_nested_errors_without_echoing_details() {
        #[derive(Debug)]
        struct Wrapped(Box<dyn std::error::Error + Send + Sync>);
        impl std::fmt::Display for Wrapped {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                write!(f, "private-body private-token http://private.invalid")
            }
        }
        impl std::error::Error for Wrapped {
            fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
                Some(self.0.as_ref())
            }
        }
        let nested = Wrapped(Box::new(Wrapped(Box::new(std::io::Error::new(
            std::io::ErrorKind::ConnectionReset,
            "private-token",
        )))));
        let error = map(
            &observation(),
            Some(ServiceError::TransportSend(
                rmcp::transport::DynamicTransportError::from_parts(
                    "fixture",
                    std::any::TypeId::of::<()>(),
                    Box::new(nested),
                ),
            )),
            "call",
            false,
            false,
        );
        assert_eq!(
            error.data,
            Some(
                json!({"classification":"connection_failure", "phase":"call", "operation_outcome":"unknown"})
            )
        );
        let rendered = format!("{error:?}{error}");
        for private in ["private-body", "private-token", "private.invalid"] {
            assert!(!rendered.contains(private));
        }
    }
    fn observation() -> Observation {
        Observation {
            phase: "call",
            dispatched: true,
            ..Default::default()
        }
    }
    fn refusal(code: i32, name: &str, status: u16) -> ServiceError {
        ServiceError::McpError(ErrorData::new(
            ErrorCode(code),
            name.to_owned(),
            Some(json!({"status":status,"error":name})),
        ))
    }
    #[test]
    fn test_principals_unavailable_refusal_is_definitely_not_executed() {
        let error = map(
            &observation(),
            Some(refusal(-32003, "principals_unavailable", 503)),
            "call",
            false,
            false,
        );
        assert_eq!(error.code, ErrorCode(-32003));
        assert_eq!(
            error.data.as_ref().unwrap()["classification"],
            "principals_unavailable"
        );
        assert_eq!(
            error.data.as_ref().unwrap()["operation_outcome"],
            "not_dispatched"
        );
        assert!(error.message.contains("No tool ran"));
    }
    #[test]
    fn test_revoked_token_refusal_is_definitely_not_executed() {
        let error = map(
            &observation(),
            Some(refusal(-32004, "unauthorized", 401)),
            "call",
            false,
            false,
        );
        assert_eq!(error.code, ErrorCode(-32004));
        assert_eq!(
            error.data.as_ref().unwrap()["operation_outcome"],
            "not_dispatched"
        );
        assert!(error.message.contains("no longer accepts this token"));
    }
    #[test]
    fn test_a_stale_credential_outranks_a_daemon_refusal() {
        for (code, name, status) in [
            (-32003, "principals_unavailable", 503),
            (-32004, "unauthorized", 401),
        ] {
            let error = map(
                &observation(),
                Some(refusal(code, name, status)),
                "call",
                false,
                true,
            );
            assert_eq!(error.code, ErrorCode::INTERNAL_ERROR);
            assert_eq!(
                error.data.as_ref().unwrap()["classification"],
                "credential_unavailable"
            );
            assert_eq!(error.data.as_ref().unwrap()["operation_outcome"], "unknown");
            assert_eq!(
                error.data,
                Some(
                    json!({"classification":"credential_unavailable","phase":"call","operation_outcome":"unknown"})
                )
            );
        }
    }
    #[test]
    fn test_inexact_principals_refusal_stays_unknown() {
        let error = map(
            &observation(),
            Some(ServiceError::McpError(ErrorData::new(
                ErrorCode(-32003),
                "secret.invalid",
                Some(json!({"status":503,"error":"principals_unavailable"})),
            ))),
            "call",
            false,
            false,
        );
        assert_eq!(error.data.as_ref().unwrap()["operation_outcome"], "unknown");
        assert!(!error.message.contains("secret.invalid"));
    }
    #[test]
    fn test_closed_connection_code_maps_to_response_lost() {
        let error = map(
            &observation(),
            Some(ServiceError::McpError(ErrorData::new(
                ErrorCode(-32000),
                "secret.invalid",
                None,
            ))),
            "call",
            false,
            false,
        );
        assert_eq!(
            error.data.as_ref().unwrap()["classification"],
            "response_lost"
        );
        assert!(
            error
                .message
                .starts_with("The memory daemon's response stream closed")
        );
        assert_eq!(error.code, ErrorCode::INTERNAL_ERROR);
        assert_eq!(
            error.data,
            Some(
                json!({"classification":"response_lost","phase":"call","operation_outcome":"unknown"})
            )
        );
        assert!(error.message.contains("may have completed"));
        let early = map(
            &Observation {
                phase: "initialize",
                ..Default::default()
            },
            Some(refusal(-32000, "secret.invalid", 0)),
            "call",
            false,
            false,
        );
        assert_eq!(
            early.data,
            Some(
                json!({"classification":"response_lost","phase":"initialize","operation_outcome":"not_dispatched"})
            )
        );
        assert!(!early.message.contains("may have completed"));
    }
    #[test]
    fn test_protocol_code_is_preserved_only_from_the_sdk_error_type() {
        let observed = Observation {
            phase: "list",
            ..Default::default()
        };
        let error = map(
            &observed,
            Some(ServiceError::McpError(ErrorData::new(
                ErrorCode(-32601),
                "secret.invalid",
                Some(json!({"credential":"secret.invalid"})),
            ))),
            "list",
            false,
            false,
        );
        assert_eq!(error.code, ErrorCode(-32601));
        assert!(!error.message.contains("secret.invalid"));
        assert_eq!(
            error.message,
            "The memory daemon returned an invalid MCP response."
        );
        assert_eq!(
            error.data,
            Some(
                json!({"classification":"protocol","phase":"list","operation_outcome":"not_dispatched"})
            )
        );
        let forged = map(
            &observed,
            Some(ServiceError::UnexpectedResponse),
            "list",
            false,
            false,
        );
        assert_eq!(forged.code, ErrorCode::INTERNAL_ERROR);
        assert!(!format!("{forged:?}").contains("secret.invalid"));
    }
    #[test]
    fn test_principals_refusal_counts_only_for_the_request_that_failed() {
        let mut observed = Observation {
            phase: "initialize",
            status: Some(503),
            refused_phase: Some("initialize"),
            ..Default::default()
        };
        let initial = map(&observed, None, "call", false, false);
        assert_eq!(
            initial.data.as_ref().unwrap()["classification"],
            "principals_unavailable"
        );
        assert_eq!(
            initial.data.as_ref().unwrap()["operation_outcome"],
            "not_dispatched"
        );
        observed.phase = "call";
        observed.dispatched = true;
        let later = map(
            &observed,
            Some(ServiceError::TransportClosed),
            "call",
            false,
            false,
        );
        assert_eq!(later.data.as_ref().unwrap()["operation_outcome"], "unknown");
        assert!(!later.message.contains("No tool ran"));
    }
}
