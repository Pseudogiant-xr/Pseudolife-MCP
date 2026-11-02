//! The SDK dispatcher's per-session in-flight table and cancellation scope.
//! Synchronous tool bodies finish their shielded worker call; async handlers
//! stop at cancellation. Neither sends its normal result after cancellation.

use super::*;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
enum Id {
    Integer(String),
    Text(String),
}

/// `coerce_request_id`: Python int(text), not Pydantic's lax int parser.
fn integer(text: &str) -> Option<String> {
    let text = text.trim();
    let (negative, digits) = match text.as_bytes().first() {
        Some(b'-') => (true, &text[1..]),
        Some(b'+') => (false, &text[1..]),
        _ => (false, text),
    };
    let mut out = String::new();
    let mut digit_before = false;
    for c in digits.chars() {
        if c == '_' && digit_before {
            digit_before = false;
        } else if let Some(value) = crate::graph_review::decimal_value(c) {
            out.push(char::from_digit(value, 10).unwrap());
            digit_before = true;
        } else {
            return None;
        }
    }
    if !digit_before || out.len() > 4300 {
        return None;
    }
    let out = out.trim_start_matches('0');
    Some(if out.is_empty() {
        "0".into()
    } else if negative {
        format!("-{out}")
    } else {
        out.into()
    })
}

fn key(value: &Value) -> Option<Id> {
    match request_id(value)? {
        Value::String(s) => Some(integer(&s).map_or_else(|| Id::Text(s), Id::Integer)),
        Value::Number(n) => Some(Id::Integer(if n.to_string() == "-0" {
            "0".into()
        } else {
            n.to_string()
        })),
        _ => unreachable!(),
    }
}

type Requests = HashMap<(String, Id), Arc<watch::Sender<bool>>>;

#[derive(Default)]
pub(super) struct Registry(Mutex<Requests>);

impl Registry {
    pub(super) fn start(self: &Arc<Self>, sid: &str, id: &Value) -> Pending {
        let key = (sid.to_string(), key(id).expect("validated request id"));
        let (signal, _) = watch::channel(false);
        let signal = Arc::new(signal);
        self.0
            .lock()
            .expect("request lock")
            .insert(key.clone(), signal.clone());
        Pending {
            registry: self.clone(),
            key,
            signal,
        }
    }

    pub(super) fn cancel(&self, sid: &str, id: &Value) {
        let Some(id) = key(id) else { return };
        if let Some(signal) = self.0.lock().expect("request lock").get(&(sid.into(), id)) {
            signal.send_replace(true);
        }
    }

    pub(super) fn forget(&self, sid: &str) {
        self.0
            .lock()
            .expect("request lock")
            .retain(|(session, _), signal| {
                if session == sid {
                    signal.send_replace(true);
                    false
                } else {
                    true
                }
            });
    }
}

pub(super) struct Pending {
    registry: Arc<Registry>,
    key: (String, Id),
    signal: Arc<watch::Sender<bool>>,
}

impl Pending {
    fn finish(&self) -> bool {
        let mut requests = self.registry.0.lock().expect("request lock");
        // A duplicate id replaces the table entry. Its older handler must
        // not remove the newer handler's cancellation scope on completion.
        if requests
            .get(&self.key)
            .is_some_and(|s| Arc::ptr_eq(s, &self.signal))
        {
            requests.remove(&self.key);
        }
        *self.signal.borrow()
    }

    async fn cancelled(&self) {
        let mut signal = self.signal.subscribe();
        let _ = signal.wait_for(|cancelled| *cancelled).await;
    }

    pub(super) async fn run(
        self,
        id: Value,
        interruptible: bool,
        work: impl std::future::Future<Output = String>,
    ) -> String {
        let result = if *self.signal.borrow() {
            None
        } else if interruptible {
            tokio::select! {
                biased;
                _ = self.cancelled() => None,
                result = work => Some(result),
            }
        } else {
            Some(work.await)
        };
        match (self.finish(), result) {
            // The legacy HTTP transport settles an unanswered request with
            // this error; the cancelled handler's result is never delivered.
            (true, _) | (_, None) => json!({"jsonrpc":"2.0", "id":id, "error":{"code":-32800,"message":"Request cancelled"}})
                .to_string(),
            (false, Some(result)) => result,
        }
    }
}

impl Drop for Pending {
    fn drop(&mut self) {
        self.finish();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_follow_python_int_without_lax_float_or_boolean_coercion() {
        for s in ["7", " +0007 ", "٠٧", "0_7"] {
            assert_eq!(key(&json!(s)), key(&json!(7)), "{s}");
        }
        for s in ["7.0", "7e0", "0-7", "_7", "7_", "7__0"] {
            assert_eq!(key(&json!(s)), Some(Id::Text(s.into())), "{s}");
        }
        assert_eq!(key(&json!("-000")), key(&json!(0)));
        assert_eq!(key(&json!(true)), None);
        assert_eq!(key(&json!(7.0)), None);
        assert_eq!(integer(&"1".repeat(4301)), None);
    }

    #[test]
    fn cancellation_is_scoped_and_old_completion_keeps_a_reused_id() {
        let registry = Arc::new(Registry::default());
        let old = registry.start("a", &json!(7));
        let current = registry.start("a", &json!("7"));
        let other = registry.start("b", &json!(7));
        drop(old);
        registry.cancel("a", &json!(" +7 "));
        assert!(*current.signal.borrow());
        assert!(!*other.signal.borrow());
        drop(current);
        assert_eq!(registry.0.lock().unwrap().len(), 1);
        registry.forget("b");
        assert!(*other.signal.borrow());
        assert!(registry.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn shielded_work_finishes_but_its_result_is_suppressed() {
        let registry = Arc::new(Registry::default());
        let pending = registry.start("s", &json!(1));
        let (started, running) = tokio::sync::oneshot::channel();
        let (release, blocked) = tokio::sync::oneshot::channel();
        let (committed, saved) = tokio::sync::oneshot::channel();
        let worker = tokio::spawn(pending.run(json!(1), false, async move {
            started.send(()).unwrap();
            blocked.await.unwrap();
            committed.send(()).unwrap();
            "normal result".into()
        }));
        running.await.unwrap();
        registry.cancel("s", &json!(1));
        assert!(!worker.is_finished());
        release.send(()).unwrap();
        let result: Value = serde_json::from_str(&worker.await.unwrap()).unwrap();
        saved.await.unwrap();
        assert_eq!(result["error"]["code"], -32800);
        assert!(result.get("result").is_none());
    }

    #[tokio::test]
    async fn async_handler_stops_without_releasing_its_blocker() {
        let registry = Arc::new(Registry::default());
        let pending = registry.start("s", &json!(1));
        let (started, running) = tokio::sync::oneshot::channel();
        let worker = tokio::spawn(pending.run(json!(1), true, async move {
            started.send(()).unwrap();
            std::future::pending::<String>().await
        }));
        running.await.unwrap();
        registry.cancel("s", &json!(1));
        let result: Value = serde_json::from_str(&worker.await.unwrap()).unwrap();
        assert_eq!(result["error"]["code"], -32800);
        assert!(registry.0.lock().unwrap().is_empty());
    }
}
