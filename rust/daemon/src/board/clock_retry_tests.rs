//! A send whose clock epoch changed (W2F-CLOCK-RETRY): the one retry reprobes
//! the writer session even inside its probe interval and runs `_ensure_init`
//! (`Service::refresh_writer_session`), as `coordination.dispatch` does, so
//! it either recovers once or refuses with nothing written. Run through
//! `python -m board.boundary --clock-retry` (a disposable bank on the test
//! PostgreSQL and the offline embedding fixture).
//!
//! The recover-once case reaches the retry (a stale ready service is fenced,
//! then the retry sends); so does the refusal. The half-open case is a
//! delivery check only: through this proxy the client still learns of the
//! killed session within milliseconds, so the send's first check reconnects
//! and the retry never runs. The half-open window the forced reprobe exists
//! for (a session dead but not yet seen closed) is not reproduced here, so no
//! case shows the reprobe changing an outcome; it follows Python's retry.

use crate::board::Board;
use crate::config::DaemonEnv;
use crate::service::Service;
use crate::storage::Storage;
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

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
    let config = PathBuf::from(std::env::var("PL_W2F_BOARD_CONFIG").expect("fixture config"));
    (dsn, config)
}

/// The service's writer session on `service_dsn`; the board's mailbox
/// connection on `board_dsn`.
fn service(service_dsn: &str, board_dsn: &str, config: &Path) -> (Arc<Service>, Arc<Board>) {
    let service = Arc::new(Service::new(
        crate::config::load(config).unwrap(),
        DaemonEnv::from_env(&|_| None).unwrap(),
        config.parent().unwrap().to_path_buf(),
        service_dsn.to_string(),
        true,
    ));
    let board = Arc::new(Board::new(
        service.config.coordination.clone(),
        board_dsn.to_string(),
        service.clone(),
        Arc::new(crate::auth::PrincipalStore::new()),
    ));
    service.install(&board);
    (service, board)
}

/// Two registered agents and the sender's instance headers.
async fn agents(board: &Board) -> (HashMap<String, String>, String) {
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
    (headers, recipient["agent_id"].as_str().unwrap().to_string())
}

fn send(to: &str, request: &str) -> Map<String, Value> {
    params(json!({"to": to, "text": "after the epoch moved", "request_id": request}))
}

async fn direct(dsn: &str) -> tokio_postgres::Client {
    let (client, task) = crate::storage::connect(
        &crate::storage::parse_dsn(dsn).unwrap(),
        Duration::from_secs(10),
    )
    .await
    .unwrap();
    drop(task);
    client
}

async fn count(client: &tokio_postgres::Client, table: &str) -> i64 {
    client
        .query_one(&format!("SELECT count(*) FROM {table}"), &[])
        .await
        .unwrap()
        .get(0)
}

async fn backend_pid(storage: &Storage) -> i32 {
    storage
        .client()
        .query_one("SELECT pg_backend_pid()", &[])
        .await
        .unwrap()
        .get(0)
}

/// End the session `pid` server-side and wait until PostgreSQL has gone
/// past it (its writer lease is free).
async fn terminate(client: &tokio_postgres::Client, pid: i32) {
    client
        .execute("SELECT pg_terminate_backend($1)", &[&pid])
        .await
        .unwrap();
    for _ in 0..500 {
        let alive: i64 = client
            .query_one(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = $1",
                &[&pid],
            )
            .await
            .unwrap()
            .get(0);
        if alive == 0 {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("the terminated session never ended");
}

/// The connections a [`half_open_proxy`] has accepted, each with its own
/// cut flag.
#[derive(Clone, Default)]
struct Cuts(Arc<std::sync::Mutex<Vec<Arc<AtomicBool>>>>);

impl Cuts {
    /// Cut every connection open now; later ones forward normally, so a
    /// reconnect through the proxy works.
    fn cut_open(&self) {
        for flag in self.0.lock().unwrap().iter() {
            flag.store(true, Ordering::SeqCst);
        }
    }
}

/// A loopback TCP proxy to `upstream`. A cut connection forwards nothing
/// more either way and closes the client's socket at the client's next
/// write: while idle it looks open to its client (a half-open TCP session,
/// as after a server restart with no FIN), and only a probe's write reveals
/// it.
async fn half_open_proxy(upstream: std::net::SocketAddr) -> (u16, Cuts) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let cuts = Cuts::default();
    let registry = cuts.clone();
    tokio::spawn(async move {
        loop {
            let Ok((client, _)) = listener.accept().await else {
                return;
            };
            let Ok(server) = TcpStream::connect(upstream).await else {
                return;
            };
            let (mut cr, mut cw) = client.into_split();
            let (mut sr, mut sw) = server.into_split();
            let (closed, mut close) = tokio::sync::watch::channel(false);
            let switch = Arc::new(AtomicBool::new(false));
            registry.0.lock().unwrap().push(switch.clone());
            let up = switch.clone();
            tokio::spawn(async move {
                let mut buf = vec![0u8; 16384];
                loop {
                    let Ok(n) = cr.read(&mut buf).await else {
                        break;
                    };
                    if n == 0 || up.load(Ordering::SeqCst) || sw.write_all(&buf[..n]).await.is_err()
                    {
                        break;
                    }
                }
                let _ = closed.send(true);
            });
            let down = switch.clone();
            tokio::spawn(async move {
                let mut buf = vec![0u8; 16384];
                loop {
                    tokio::select! {
                        _ = close.changed() => break,
                        read = sr.read(&mut buf) => {
                            let n = read.unwrap_or(0);
                            if n == 0 {
                                // The server side ended. A cut connection
                                // stays open to its client until the
                                // client writes (the half-open session).
                                if down.load(Ordering::SeqCst) {
                                    let _ = close.changed().await;
                                }
                                break;
                            }
                            if !down.load(Ordering::SeqCst) && cw.write_all(&buf[..n]).await.is_err() {
                                break;
                            }
                        }
                    }
                }
                // Dropping both halves closes the client's socket.
                drop(cw);
            });
        }
    });
    (port, cuts)
}

/// Another writer takes the bank's lease, moves its epoch and leaves, as
/// `Storage::open` and `close` do (the advisory lock and the epoch bump), in
/// a few milliseconds so the send that follows stays inside the probe
/// interval.
async fn other_writer(client: &tokio_postgres::Client) {
    client
        .execute(
            "SELECT pg_advisory_lock(hashtextextended($1, 0))",
            &[&crate::storage::WRITER_LEASE_KEY],
        )
        .await
        .unwrap();
    client
        .execute(
            "UPDATE meta SET value = to_jsonb((value #>> '{}')::bigint + 1) \
             WHERE key = 'writer_lease_epoch'",
            &[],
        )
        .await
        .unwrap();
    client
        .execute(
            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
            &[&crate::storage::WRITER_LEASE_KEY],
        )
        .await
        .unwrap();
}

#[tokio::test]
#[ignore = "requires a disposable board database through board.boundary --clock-retry"]
async fn a_send_after_a_half_open_writer_session_is_delivered() {
    let (dsn, config) = fixture();
    let upstream = "127.0.0.1:5434".parse().unwrap();
    let (port, cuts) = half_open_proxy(upstream).await;
    let proxied = dsn.replacen(":5434", &format!(":{port}"), 1);
    assert_ne!(proxied, dsn, "the fixture DSN names its port");
    let (service, board) = service(&proxied, &dsn, &config);
    let (headers, to) = agents(&board).await;
    let side = direct(&dsn).await;
    // A real probe now (the probe timestamp forgotten first), so the session
    // check stays fresh for the next second and only the retry's forced
    // reprobe can find the dead session.
    let ready = service.ensure_init().await.unwrap();
    ready.storage.forget_probe();
    drop(ready);
    let ready = service.ensure_init().await.unwrap();
    let pid = backend_pid(&ready.storage).await;
    let before = ready.storage.lease_epoch();
    drop(ready);
    // Inside the probe interval: the session dies unseen and another writer
    // holds the bank for a moment, so the epoch the board captured is stale.
    cuts.cut_open();
    terminate(&side, pid).await;
    other_writer(&side).await;
    let messages = count(&side, "coordination_messages").await;
    let sent = board
        .dispatch("send", send(&to, "half-open"), &headers, "default")
        .await;
    let sent = sent.expect("the dead session is replaced and the mail sent");
    assert_eq!(sent["state"], "queued");
    assert_eq!(count(&side, "coordination_messages").await, messages + 1);
    let now = service.ensure_init().await.unwrap().storage.lease_epoch();
    assert!(
        now > before + 1,
        "reconnected after the other writer: {before} -> {now}"
    );
}

#[tokio::test]
#[ignore = "requires a disposable board database through board.boundary --clock-retry"]
async fn a_send_refuses_with_nothing_written_while_another_writer_holds_the_bank() {
    let (dsn, config) = fixture();
    let (service, board) = service(&dsn, &dsn, &config);
    let (headers, to) = agents(&board).await;
    let side = direct(&dsn).await;
    let ready = service.ensure_init().await.unwrap();
    let pid = backend_pid(&ready.storage).await;
    drop(ready);
    terminate(&side, pid).await;
    let holder = Storage::open(&dsn)
        .await
        .unwrap_or_else(|_| panic!("the second writer takes the lease"));
    let (messages, events) = (
        count(&side, "coordination_messages").await,
        count(&side, "coordination_events").await,
    );
    let refused = board
        .dispatch("send", send(&to, "held"), &headers, "default")
        .await;
    assert!(refused.is_err(), "{refused:?}");
    assert_eq!(count(&side, "coordination_messages").await, messages);
    assert_eq!(count(&side, "coordination_events").await, events);
    holder.close().await;
}

#[tokio::test]
#[ignore = "requires a disposable board database through board.boundary --clock-retry"]
async fn a_send_recovers_once_from_a_reconnect_after_it_took_its_stamp() {
    let (dsn, config) = fixture();
    let (service, board) = service(&dsn, &dsn, &config);
    let (headers, to) = agents(&board).await;
    let side = direct(&dsn).await;
    let ready = service.ensure_init().await.unwrap();
    let pid = backend_pid(&ready.storage).await;
    let before = ready.storage.lease_epoch();
    drop(ready);
    // The send takes its ready service, then waits at the gate before it
    // stamps.
    let (release, gate) = tokio::sync::oneshot::channel();
    *board.stamp_gate.lock().unwrap() = Some(gate);
    let mut sending =
        Box::pin(board.dispatch("send", send(&to, "reconnected"), &headers, "default"));
    for _ in 0..10_000 {
        if board.stamp_gate.lock().unwrap().is_none() {
            break;
        }
        assert!(futures::FutureExt::now_or_never(sending.as_mut()).is_none());
        tokio::task::yield_now().await;
    }
    assert!(
        board.stamp_gate.lock().unwrap().is_none(),
        "the send never reached its stamp"
    );
    assert!(futures::FutureExt::now_or_never(sending.as_mut()).is_none());
    // Meanwhile another call reconnects (every reconnect moves the epoch), so
    // the ready service the send holds is stale.
    terminate(&side, pid).await;
    let after = service.ensure_init().await.unwrap().storage.lease_epoch();
    assert!(after > before);
    release.send(()).unwrap();
    let sent = sending
        .await
        .expect("the first attempt is fenced, the retry sends");
    assert_eq!(sent["state"], "queued");
    assert_eq!(
        board.clock_retries.load(Ordering::SeqCst),
        1,
        "the first attempt was fenced"
    );
}
