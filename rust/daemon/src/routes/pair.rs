//! `POST /api/pair`, routed before the bearer gate: redeem a pairing code
//! for the SHA-256 of a token the client minted (`web/api.py` `_pair`).

use crate::auth::{PrincipalStore, StoredPrincipal};
use crate::http::{
    App, RequestBody, json_response, media_type, method_not_allowed, parse_body_json, py_strip,
    read_body,
};
use axum::http::HeaderMap;
use axum::response::Response;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::VecDeque;
use std::future::Future;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const PAIR_BODY_LIMIT: usize = 1024;
/// `principal_store.FAILED_REDEMPTIONS_PER_MINUTE`, `REDEMPTION_SLOTS`.
const PAIR_FAILURES_PER_WINDOW: usize = 20;
const PAIR_WINDOW: Duration = Duration::from_secs(60);
const PAIRING_CODE_ALPHABET: &str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// `principal_store.REDEMPTION_SLOTS`.
const REDEMPTION_SLOTS: usize = 2;

/// The daemon-wide pairing budget (`web/api.py` `pairing_failures` and
/// `pairing_gate`): the failed-attempt window (`FailureLimiter`) and the
/// two-slot redemption gate (`RedemptionGate`). Cloning shares both, so the
/// redemption task that outlives a disconnected request keeps them.
#[derive(Clone, Default)]
pub(crate) struct PairLimiter {
    failures: Arc<Mutex<Failures>>,
    in_flight: Arc<AtomicUsize>,
}

/// The window's attempts, each with its own id, so a refund gives back that
/// attempt even when two were counted in the same clock tick.
#[derive(Default)]
struct Failures {
    next: u64,
    attempts: VecDeque<Attempt>,
}

/// One counted attempt (`FailureLimiter.reserve`'s stamp).
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Attempt {
    id: u64,
    at: Instant,
}

/// One redemption's place in the gate, given back when dropped
/// (`RedemptionGate.leave`).
pub(crate) struct Slot(Arc<AtomicUsize>);

impl Drop for Slot {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

impl PairLimiter {
    /// `FailureLimiter.reserve`: count one attempt, or None when spent.
    fn reserve(&self) -> Option<Attempt> {
        let mut f = self.failures.lock().expect("pair limiter");
        let now = Instant::now();
        while f
            .attempts
            .front()
            .is_some_and(|a| now.duration_since(a.at) >= PAIR_WINDOW)
        {
            f.attempts.pop_front();
        }
        if f.attempts.len() >= PAIR_FAILURES_PER_WINDOW {
            return None;
        }
        f.next += 1;
        let attempt = Attempt {
            id: f.next,
            at: now,
        };
        f.attempts.push_back(attempt);
        Some(attempt)
    }

    /// `FailureLimiter.refund`: give back an attempt that succeeded or never
    /// reached the store (one already aged out is gone).
    fn refund(&self, attempt: Attempt) {
        let mut f = self.failures.lock().expect("pair limiter");
        if let Some(i) = f.attempts.iter().position(|a| a.id == attempt.id) {
            f.attempts.remove(i);
        }
    }

    /// `RedemptionGate.try_enter`: a slot, or None when both are taken. A
    /// full gate is a refusal, never a queue.
    fn enter(&self) -> Option<Slot> {
        self.in_flight
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
                (n < REDEMPTION_SLOTS).then_some(n + 1)
            })
            .ok()
            .map(|_| Slot(self.in_flight.clone()))
    }

    #[cfg(test)]
    fn attempts(&self) -> usize {
        self.failures.lock().expect("pair limiter").attempts.len()
    }

    #[cfg(test)]
    fn in_flight(&self) -> usize {
        self.in_flight.load(Ordering::SeqCst)
    }
}

/// The redemption, in a task of its own: a client that disconnects drops
/// only its wait, as Python's redemption runs on in the gate's executor
/// thread. The slot is given back when the store call ends; a success then
/// refunds its attempt and is usable at once (`PrincipalStore::add`, which
/// keeps the entry until a refresh that started after it).
async fn redeem_owned<F>(
    limiter: PairLimiter,
    store: Arc<PrincipalStore>,
    attempt: Attempt,
    slot: Slot,
    redemption: F,
) -> Result<Option<StoredPrincipal>, String>
where
    F: Future<Output = Result<Option<StoredPrincipal>, String>> + Send + 'static,
{
    tokio::spawn(async move {
        let outcome = redemption.await;
        drop(slot);
        if let Ok(Some(row)) = &outcome {
            limiter.refund(attempt);
            store.add(row.clone());
        }
        outcome
    })
    .await
    .map_err(|_| "the redemption task failed".to_string())?
}

/// `principal_store.redeem` on a connection of its own (`_connect`, a
/// 5-second connect timeout), closed when it ends.
async fn redeem_in_bank(
    dsn: String,
    code_hash: String,
    token_hash: String,
    excluded: Vec<String>,
) -> Result<Option<StoredPrincipal>, String> {
    let config = crate::storage::parse_dsn(&dsn)?;
    let (mut client, task) = crate::storage::connect(&config, Duration::from_secs(5)).await?;
    let outcome = crate::principals::redeem(&mut client, &code_hash, &token_hash, &excluded)
        .await
        .map_err(|e| crate::storage::error_text(&e));
    drop(client);
    let _ = task.await;
    outcome
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
    // Counted before anything else can happen to it, so concurrent bad codes
    // cannot all pass a check made before any of them failed.
    let Some(attempt) = app.pair_limiter.reserve() else {
        return json_response(429, &json!({"error": "rate_limited"}));
    };
    let refused = || json_response(400, &json!({"error": "pairing_refused"}));
    let Some(raw) = read_body(body, PAIR_BODY_LIMIT).await else {
        return json_response(413, &json!({"error": "request_too_large"}));
    };
    let parsed = std::str::from_utf8(&raw)
        .ok()
        .and_then(|t| parse_body_json(t).ok());
    let request = parsed.as_ref().and_then(Value::as_object).and_then(|o| {
        let code = o.get("code").and_then(normalize_pairing_code)?;
        let token_hash = o.get("token_sha256").filter(|v| is_sha256_hex(v))?;
        (o.len() == 2).then(|| (code, token_hash.as_str().unwrap_or("").to_string()))
    });
    let Some((code, token_hash)) = request else {
        return refused();
    };
    if !app.auth_configured() {
        return refused();
    }
    let Some(slot) = app.pair_limiter.enter() else {
        // Never reached the store: not a failed redemption.
        app.pair_limiter.refund(attempt);
        return json_response(429, &json!({"error": "rate_limited"}));
    };
    let code_hash = hex::encode(Sha256::digest(code.as_bytes()));
    let redemption = redeem_in_bank(
        app.board.dsn.clone(),
        code_hash,
        token_hash,
        app.store.excluded_names(),
    );
    match redeem_owned(
        app.pair_limiter.clone(),
        app.store.clone(),
        attempt,
        slot,
        redemption,
    )
    .await
    {
        Ok(Some(paired)) => json_response(
            200,
            &json!({"principal": paired.principal, "tier": paired.tier, "bank": app.store.bank()}),
        ),
        Ok(None) => refused(),
        Err(_) => {
            // The outcome is unknown; its text may hold row data, so the
            // log names the class only, as Python logs the type.
            eprintln!("pairing redemption failed (store error)");
            json_response(503, &json!({"error": "pairing_unavailable"}))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::http::handle;
    use crate::http::tests::{authed_request, mcp_refusal_app};
    use axum::extract::State;

    fn pairing() -> String {
        json!({"code": "ABCD-EFGH-JKMN", "token_sha256": "b".repeat(64)}).to_string()
    }

    async fn post(app: &Arc<App>) -> (u16, Value) {
        let response = handle(
            State(app.clone()),
            authed_request("POST", "/api/pair", Some(&pairing())),
        )
        .await;
        let status = response.status().as_u16();
        let bytes = axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap();
        (status, serde_json::from_slice(&bytes).unwrap())
    }

    fn laptop() -> StoredPrincipal {
        StoredPrincipal {
            principal: "laptop".into(),
            token_hash: Some(hex::encode(Sha256::digest(b"laptop-token"))),
            tier: Some("core".into()),
            board: true,
            revoked: false,
        }
    }

    #[test]
    fn two_redemptions_at_once_fill_the_gate_and_a_third_is_refused_not_queued() {
        let limiter = PairLimiter::default();
        let first = limiter.enter().expect("first slot");
        let second = limiter.enter().expect("second slot");
        assert!(limiter.enter().is_none(), "REDEMPTION_SLOTS is 2");
        drop(first);
        assert!(
            limiter.enter().is_some(),
            "a finished redemption frees its slot"
        );
        drop(second);
    }

    #[test]
    fn a_refund_gives_back_exactly_its_own_attempt() {
        let limiter = PairLimiter::default();
        let stamps: Vec<_> = (0..20).map(|_| limiter.reserve().unwrap()).collect();
        assert!(limiter.reserve().is_none(), "20 attempts a minute");
        limiter.refund(stamps[7]);
        limiter.refund(stamps[7]); // already given back: nothing more
        assert!(limiter.reserve().is_some());
        assert!(limiter.reserve().is_none());
    }

    /// An authenticated app whose board opens connections to `dsn`.
    fn app_with_bank(dsn: &str) -> Arc<App> {
        let base = mcp_refusal_app();
        let board = Arc::new(crate::board::Board::new(
            base.service.config.coordination.clone(),
            dsn.into(),
            base.service.clone(),
            base.store.clone(),
        ));
        Arc::new(App::new(
            base.service.clone(),
            crate::auth::EnvTokens {
                single: Some("fixture-token".into()),
                map: vec![],
            },
            base.store.clone(),
            None,
            board,
        ))
    }

    /// The store cannot be reached (here its DSN does not even parse, so no
    /// socket is opened): a valid pairing reaches the store, answers `503
    /// pairing_unavailable`, and keeps its attempt (only a success or a full
    /// gate gives it back).
    #[tokio::test]
    async fn a_store_failure_is_pairing_unavailable_and_keeps_its_attempt() {
        let app = app_with_bank("not a connection string");
        let (status, body) = post(&app).await;
        assert_eq!(
            (status, body),
            (503, json!({"error": "pairing_unavailable"}))
        );
        assert_eq!(app.pair_limiter.attempts(), 1);
        assert_eq!(app.pair_limiter.in_flight(), 0);
    }

    #[tokio::test]
    async fn a_full_gate_answers_429_and_refunds_the_attempt() {
        let app = mcp_refusal_app();
        let _a = app.pair_limiter.enter().unwrap();
        let _b = app.pair_limiter.enter().unwrap();
        let (status, body) = post(&app).await;
        assert_eq!((status, body), (429, json!({"error": "rate_limited"})));
        assert_eq!(app.pair_limiter.attempts(), 0, "never reached the store");
    }

    /// A client that disconnects mid-redemption drops only its wait: the
    /// redemption finishes, frees its slot, refunds its attempt and is usable
    /// at once, as Python's executor thread runs on.
    #[tokio::test]
    async fn a_disconnected_request_still_completes_its_redemption() {
        let limiter = PairLimiter::default();
        let store = Arc::new(PrincipalStore::new());
        let attempt = limiter.reserve().unwrap();
        let slot = limiter.enter().unwrap();
        let (release, released) = tokio::sync::oneshot::channel::<()>();
        let row = laptop();
        // Owned (boxed), so dropping it really drops the request's future.
        let mut wait = Box::pin(redeem_owned(
            limiter.clone(),
            store.clone(),
            attempt,
            slot,
            async move {
                released.await.unwrap();
                Ok(Some(row))
            },
        ));
        // One poll starts the redemption's task; it then waits on `released`.
        assert!(futures::FutureExt::now_or_never(wait.as_mut()).is_none());
        drop(wait); // the client went away
        release.send(()).unwrap();
        let hash = laptop().token_hash.unwrap();
        // The test runtime has one thread: the task runs while this yields.
        for _ in 0..1000 {
            if store.lookup(&hash).is_some() {
                break;
            }
            tokio::task::yield_now().await;
        }
        assert_eq!(store.lookup(&hash).as_deref(), Some("laptop"));
        assert_eq!((limiter.attempts(), limiter.in_flight()), (0, 0));
    }

    #[tokio::test]
    async fn a_refusal_keeps_its_attempt_and_frees_its_slot() {
        let limiter = PairLimiter::default();
        let store = Arc::new(PrincipalStore::new());
        let attempt = limiter.reserve().unwrap();
        let slot = limiter.enter().unwrap();
        let out = redeem_owned(limiter.clone(), store, attempt, slot, async { Ok(None) }).await;
        assert_eq!(out, Ok(None));
        assert_eq!((limiter.attempts(), limiter.in_flight()), (1, 0));
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
