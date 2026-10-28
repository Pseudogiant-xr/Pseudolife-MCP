//! The board on the service clock (W2F-SHARED-HLC): a send's stamp is the
//! next tick of `Ready::tick_hlc`, interleaved with memory writes, and a
//! restart's reseed reads the board's stored high-water mark. Run through
//! `python -m board.boundary --shared-clock` (a disposable bank on the test
//! PostgreSQL and the offline embedding fixture).

use crate::board::Board;
use crate::config::DaemonEnv;
use crate::service::Service;
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

fn params(v: Value) -> Map<String, Value> {
    v.as_object().unwrap().clone()
}

fn fixture() -> (String, PathBuf) {
    let dsn = std::env::var("PL_W2F_BOARD_DSN").expect("owned board fixture DSN");
    let parsed = crate::storage::parse_dsn(&dsn).unwrap();
    assert!(
        parsed
            .get_dbname()
            .is_some_and(|n| n.starts_with("pl_cf_w2f_"))
    );
    assert_eq!(parsed.get_ports(), &[5434]);
    assert!(
        matches!(parsed.get_hosts(), [tokio_postgres::config::Host::Tcp(h)] if h == "127.0.0.1")
    );
    let config = PathBuf::from(std::env::var("PL_W2F_BOARD_CONFIG").expect("fixture config"));
    (dsn, config)
}

fn service(dsn: &str, config_path: &Path) -> (Arc<Service>, Arc<Board>) {
    let service = Arc::new(Service::new(
        crate::config::load(config_path).unwrap(),
        DaemonEnv::from_env(&|_| None).unwrap(),
        config_path.parent().unwrap().to_path_buf(),
        dsn.to_string(),
        true,
    ));
    let board = Arc::new(Board::new(
        service.config.coordination.clone(),
        dsn.to_string(),
        service.clone(),
        Arc::new(crate::auth::PrincipalStore::new()),
    ));
    service.install(&board);
    (service, board)
}

fn stamp(text: &str) -> (i64, i64) {
    let (phys, logical) = text.split_once(':').unwrap();
    (phys.parse().unwrap(), logical.parse().unwrap())
}

#[tokio::test]
#[ignore = "requires a disposable board database through board.boundary --shared-clock"]
async fn board_sends_tick_the_service_clock() {
    let (dsn, config) = fixture();
    let (service, board) = service(&dsn, &config);
    let mut headers = HashMap::new();
    let sender = board
        .dispatch(
            "register",
            params(json!({"label": "sender"})),
            &headers,
            "default",
        )
        .await
        .unwrap();
    let recipient = board
        .dispatch(
            "register",
            params(json!({"label": "recipient"})),
            &headers,
            "default",
        )
        .await
        .unwrap();
    headers.insert(
        "x-pl-agent".into(),
        sender["agent_id"].as_str().unwrap().into(),
    );
    headers.insert(
        "x-pl-agent-key".into(),
        sender["credential"].as_str().unwrap().into(),
    );
    let send = |request: &str| {
        params(json!({"to": recipient["agent_id"], "text": "clock proof", "request_id": request}))
    };
    let stored = |request: &'static str| {
        let service = service.clone();
        async move {
            let storage = service.snapshot().storage.unwrap();
            let row = storage
                .client()
                .query_one(
                    "SELECT hlc FROM coordination_messages WHERE request_id = $1",
                    &[&request],
                )
                .await
                .unwrap();
            stamp(&row.get::<_, String>(0))
        }
    };

    // A stamp far ahead of the wall clock that only the service clock holds:
    // the board's send must take the clock's next tick, not its own.
    let ready = service.ensure_init().await.unwrap();
    let future = (crate::board::store::now() * 1000.0) as i64 + 3_600_000;
    ready.hlc.lock().unwrap().observe(future, 7);
    board
        .dispatch("send", send("first"), &headers, "default")
        .await
        .unwrap();
    assert_eq!(stored("first").await, (future, 8));

    // Interleaved with memory writes, one strictly increasing order.
    let before = ready.tick_hlc();
    board
        .dispatch("send", send("second"), &headers, "default")
        .await
        .unwrap();
    let sent = stored("second").await;
    let after = ready.tick_hlc();
    assert_eq!(
        (before, sent, after),
        ((future, 9), (future, 10), (future, 11))
    );
    // A restart reseeds from the bank (`StartupState::reseed`, which a new
    // service runs before its first tick) past the board's stored high-water
    // mark, which no memory write carries: the next tick outranks the last send.
    ready.startup.reseed(ready.storage.client()).await.unwrap();
    let high = ready.startup.clock_state().hlc_highwater;
    assert!(high >= (future, 10), "{high:?}");
    let mut restarted = crate::hlc::Clock::default();
    restarted.observe(high.0, high.1);
    assert!(restarted.tick() > (future, 10));

    // On a served daemon a send never queues behind the service mutex
    // (`coordination_tier_ready`, W2F-SEND-SERVICE-LOCK): it takes the
    // published ready service, whose clock is reseeded.
    drop(ready);
    let held = service.hold_service_lock().await;
    let sent = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        board.dispatch("send", send("unlocked"), &headers, "default"),
    )
    .await;
    drop(held);
    let sent = sent.expect("a send waited behind the service mutex");
    assert_eq!(sent.unwrap()["state"], "queued");
    drop(board);
}
