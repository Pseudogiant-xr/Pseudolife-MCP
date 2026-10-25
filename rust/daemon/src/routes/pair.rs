//! `POST /api/pair`, routed before the bearer gate: its refusals are
//! served; redemption is not yet.

use crate::http::{
    App, RequestBody, json_response, media_type, method_not_allowed, not_implemented,
    parse_body_json, py_strip, read_body,
};
use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::{Duration, Instant};

const PAIR_BODY_LIMIT: usize = 1024;
/// `principal_store.FAILED_REDEMPTIONS_PER_MINUTE`, `REDEMPTION_SLOTS`.
const PAIR_FAILURES_PER_WINDOW: usize = 20;
const PAIR_WINDOW: Duration = Duration::from_secs(60);
const PAIRING_CODE_ALPHABET: &str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// The daemon-wide redemption attempt window.
#[derive(Default)]
pub(crate) struct PairLimiter(Mutex<VecDeque<Instant>>);

impl PairLimiter {
    /// `FailureLimiter.reserve`: count one attempt, or None when spent.
    fn reserve(&self) -> Option<Instant> {
        let mut q = self.0.lock().expect("pair limiter");
        let now = Instant::now();
        while q
            .front()
            .is_some_and(|t| now.duration_since(*t) >= PAIR_WINDOW)
        {
            q.pop_front();
        }
        if q.len() >= PAIR_FAILURES_PER_WINDOW {
            return None;
        }
        q.push_back(now);
        Some(now)
    }
}

/// `normalize_pairing_code`.
fn normalize_pairing_code(v: &Value) -> Option<String> {
    let text = v.as_str()?;
    let code: String = py_strip(text)
        .to_uppercase()
        .replace('-', "")
        .chars()
        .map(|c| match c {
            'O' => '0',
            'I' | 'L' => '1',
            c => c,
        })
        .collect();
    (code.chars().count() == 12 && code.chars().all(|c| PAIRING_CODE_ALPHABET.contains(c)))
        .then_some(code)
}

fn is_sha256_hex(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == 64
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

pub(crate) async fn route(
    app: &App,
    method: &str,
    h: &HeaderMap,
    body: &mut RequestBody,
) -> Response {
    if method != "POST" {
        return method_not_allowed();
    }
    if h.contains_key("origin") && !crate::mutants::active("pair-ignores-origin") {
        return json_response(403, &json!({"error": "forbidden_origin"}));
    }
    if media_type(h) != "application/json" {
        return json_response(
            415,
            &json!({"error": "content_type_must_be_application_json"}),
        );
    }
    if app.pair_limiter.reserve().is_none() {
        return json_response(429, &json!({"error": "rate_limited"}));
    }
    let refused = || json_response(400, &json!({"error": "pairing_refused"}));
    let Some(raw) = read_body(body, PAIR_BODY_LIMIT).await else {
        return json_response(413, &json!({"error": "request_too_large"}));
    };
    let parsed = std::str::from_utf8(&raw)
        .ok()
        .and_then(|t| parse_body_json(t).ok());
    let valid = parsed.as_ref().and_then(Value::as_object).is_some_and(|o| {
        o.len() == 2
            && o.get("code").and_then(normalize_pairing_code).is_some()
            && o.get("token_sha256").is_some_and(is_sha256_hex)
    });
    if !valid || !app.auth_configured() {
        return refused();
    }
    // Redemption writes the principal store: owned by a later slice. The
    // attempt keeps its reservation, as a failed redemption would.
    not_implemented("/api/pair")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::http::tests::{assert_declared_501, authed_request, mcp_refusal_app};

    /// A well-formed request on an authenticated daemon reaches redemption,
    /// which a later slice owns.
    #[tokio::test]
    async fn redemption_answers_the_declared_501() {
        let pairing = json!({"code": "ABCD-EFGH-JKMN", "token_sha256": "b".repeat(64)});
        let request = authed_request("POST", "/api/pair", Some(&pairing.to_string()));
        assert_declared_501(&mcp_refusal_app(), request).await;
    }

    #[test]
    fn pairing_code_normalization() {
        assert_eq!(
            normalize_pairing_code(&json!(" abcd-efgh-jkmn ")),
            Some("ABCDEFGHJKMN".into())
        );
        assert_eq!(
            normalize_pairing_code(&json!("oooo-iiii-llll")),
            Some("000011111111".into())
        );
        assert_eq!(normalize_pairing_code(&json!("uuuu-uuuu-uuuu")), None);
        assert_eq!(normalize_pairing_code(&json!("abc")), None);
        assert_eq!(normalize_pairing_code(&json!(12)), None);
        assert!(is_sha256_hex(&json!("a".repeat(64))));
        assert!(!is_sha256_hex(&json!("A".repeat(64))));
    }
}
