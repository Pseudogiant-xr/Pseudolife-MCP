use super::*;

#[test]
fn repr_matches_python() {
    assert_eq!(py_repr("pl_bank"), "'pl_bank'");
    assert_eq!(py_repr(""), "''");
    assert_eq!(py_repr("it's"), "\"it's\"");
    assert_eq!(py_repr("both ' and \""), "'both \\' and \"'");
    assert_eq!(
        py_repr("a\\b\tc\nd\re\x01\x7f"),
        "'a\\\\b\\tc\\nd\\re\\x01\\x7f'"
    );
    assert_eq!(
        py_repr("caf\u{e9}\u{a0}\u{2028}"),
        "'caf\u{e9}\\xa0\\u2028'"
    );
    // Python 3.11 escapes unassigned (U+0378, U+1FFFF) and private-use
    // (U+E000) code points, and keeps a Unicode 14 addition (U+1FAE8 is
    // unassigned there, U+1F979 is assigned).
    assert_eq!(
        py_repr("\u{378}\u{e000}\u{1ffff}\u{1f979}\u{1fae8}"),
        "'\\u0378\\ue000\\U0001ffff\u{1f979}\\U0001fae8'"
    );
}

#[test]
fn printable_table_matches_cpython() {
    use sha2::{Digest, Sha256};
    let mut bits = Vec::with_capacity(0x110000);
    for n in 0u32..0x110000 {
        let nonprintable = match char::from_u32(n) {
            Some(c) => !py_printable(c),
            None => true, // the surrogates: category Cs
        };
        bits.push(u8::from(nonprintable));
    }
    assert_eq!(
        hex::encode(Sha256::digest(&bits)),
        crate::py_repr_unicode14::NONPRINTABLE_SHA256
    );
}

#[test]
fn lease_message_matches_python() {
    // The text postgres.py:_describe_lease_holder builds for these inputs.
    let tail = ". A bank has exactly one writer, because two would each save slots \
from their own resident copy and erase each other's history. Stop that process first \
(for the Docker daemon: `docker compose -f ops/docker-compose.yml stop pseudolife-daemon`), \
or point this one at a different database.";
    assert_eq!(
        lease_holder_message("pl_x", &[]),
        format!(
            "another process holds the writer lease on bank 'pl_x': a session that \
released it a moment ago (retry){tail}"
        )
    );
    assert_eq!(
        lease_holder_message(
            "pl_x",
            &[(41, Some("pseudolife-mcp pid=7 serve".into())), (42, None)]
        ),
        format!(
            "another process holds the writer lease on bank 'pl_x': backend pid 41 \
(application_name 'pseudolife-mcp pid=7 serve'), backend pid 42 (application_name ''){tail}"
        )
    );
}

#[test]
fn search_path_rule() {
    assert_eq!(search_path_refusal("public"), None);
    assert_eq!(search_path_refusal("public, \"$user\""), None);
    assert_eq!(search_path_refusal(" \"public\" "), None);
    let shadow = search_path_refusal("\"$user\", public").unwrap();
    assert_eq!(
        shadow,
        "search_path resolves $user (role 'pseudolife', which is also a schema name) \
ahead of public (got '\"$user\", public') — this shadows the real bank. \
Pin search_path to public first."
    );
    let missing = search_path_refusal("pseudolife").unwrap();
    assert_eq!(
        missing,
        "search_path must include 'public' (got 'pseudolife'); the real bank lives in \
public — refusing to run against a shadow schema."
    );
    assert_eq!(py_strip("\u{1c} public\u{a0}"), "public");
}

#[test]
fn application_name_is_bounded() {
    let name = application_name();
    assert!(name.starts_with("pseudolife-mcp pid="));
    assert!(name.len() <= 63);
    assert_eq!(truncate_bytes(&"\u{e9}".repeat(40), 63).len(), 62);
}

#[test]
fn dsn_forms_parse() {
    let url = parse_dsn("postgresql://u:p@127.0.0.1:5433/pl_cf_w1a_x").unwrap();
    assert_eq!(url.get_dbname(), Some("pl_cf_w1a_x"));
    assert_eq!(url.get_application_name(), None);
    let kv = parse_dsn("host=127.0.0.1 port=5433 dbname=pl_cf_w1a_x application_name=me").unwrap();
    assert_eq!(kv.get_application_name(), Some("me"));
    assert!(parse_dsn("postgresql://u@h/db?bogus_option=1").is_err());
}

// ---- database tests: a disposable bank named by PL_W1A_TEST_DSN ----------

/// One test at a time on the shared disposable bank: they take its lease.
static DB: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn test_dsn() -> Option<String> {
    let dsn = std::env::var("PL_W1A_TEST_DSN")
        .ok()
        .filter(|d| !d.is_empty());
    if dsn.is_none() {
        eprintln!("PL_W1A_TEST_DSN unset: skipping database test");
    }
    dsn
}

async fn side_client(dsn: &str) -> (Client, JoinHandle<()>) {
    connect(&parse_dsn(dsn).unwrap(), Duration::from_secs(10))
        .await
        .unwrap()
}

#[tokio::test]
async fn db_second_writer_is_refused_and_epoch_counts() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let first = Storage::open(&dsn).await.expect("first open");
    let e1 = first.lease_epoch();
    assert!(e1 >= 1);
    let dbname = parse_dsn(&dsn).unwrap().get_dbname().unwrap().to_string();
    match Storage::open(&dsn).await {
        Err(OpenError::LeaseHeld(msg)) => {
            let head =
                format!("another process holds the writer lease on bank '{dbname}': backend pid ");
            assert!(msg.starts_with(&head), "{msg}");
            assert!(
                msg.contains("(application_name 'pseudolife-mcp pid="),
                "{msg}"
            );
        }
        Err(e) => panic!("expected LeaseHeld, got {e:?}"),
        Ok(_) => panic!("a second writer opened the bank"),
    }
    first.check_search_path().await.expect("search_path");
    first.ping().await.expect("ping");
    first.close().await;
    let second = Storage::open(&dsn).await.expect("reopen after close");
    assert_eq!(second.lease_epoch(), e1 + 1);
    second.close().await;
}

#[tokio::test]
async fn db_non_number_epoch_restarts_at_one() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let s = Storage::open(&dsn).await.expect("open");
    s.client()
        .batch_execute("UPDATE meta SET value = '\"x\"'::jsonb WHERE key = 'writer_lease_epoch'")
        .await
        .unwrap();
    s.close().await;
    let s = Storage::open(&dsn).await.expect("reopen");
    assert_eq!(s.lease_epoch(), 1);
    s.close().await;
}

#[tokio::test]
async fn db_dimension_mismatch_is_refused() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    Storage::open(&dsn).await.expect("open").close().await;
    let (c, t) = side_client(&dsn).await;
    c.batch_execute("ALTER TABLE public.entries ALTER COLUMN embedding TYPE vector(384)")
        .await
        .unwrap();
    let refused = Storage::open(&dsn).await;
    c.batch_execute("ALTER TABLE public.entries ALTER COLUMN embedding TYPE vector(1024)")
        .await
        .unwrap();
    drop(c);
    let _ = t.await;
    match refused {
        Err(OpenError::Refused(msg)) => assert_eq!(msg, schema::dim_mismatch_message(384)),
        Err(e) => panic!("expected Refused, got {e:?}"),
        Ok(_) => panic!("opened a 384-d bank"),
    }
    // The refusal released the lease: the next open succeeds at once.
    Storage::open(&dsn)
        .await
        .expect("open after refusal")
        .close()
        .await;
}

#[tokio::test]
async fn db_bank_id_retry_gate_and_cache() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let (c, t) = side_client(&dsn).await;
    let s = Storage::open(&dsn).await.expect("open");
    c.batch_execute("DELETE FROM meta WHERE key = 'coordination_bank_id'")
        .await
        .unwrap();
    assert_eq!(s.cached_bank_id().await, None);
    c.batch_execute(
        "INSERT INTO meta (key, value) VALUES ('coordination_bank_id', '\"bank-1\"'::jsonb)",
    )
    .await
    .unwrap();
    // Inside the 60 s window armed by the first attempt: not read again.
    assert_eq!(s.cached_bank_id().await, None);
    s.close().await;
    let s = Storage::open(&dsn).await.expect("reopen");
    assert_eq!(s.cached_bank_id().await.as_deref(), Some("bank-1"));
    c.batch_execute("DELETE FROM meta WHERE key = 'coordination_bank_id'")
        .await
        .unwrap();
    // Found once, never read again.
    assert_eq!(s.cached_bank_id().await.as_deref(), Some("bank-1"));
    s.close().await;
    drop(c);
    let _ = t.await;
}

#[tokio::test]
async fn db_connect_failure_is_failed() {
    // Port 1 on loopback refuses at once; no database is touched.
    match Storage::open("postgresql://nobody@127.0.0.1:1/pl_cf_w1a_none").await {
        Err(OpenError::Failed(_)) => {}
        Err(e) => panic!("expected Failed, got {e:?}"),
        Ok(_) => panic!("opened a closed port"),
    }
}

/// Opens the bank named by PL_W1A_OPEN_DSN once and closes it: the Rust half
/// of the database-state comparison against Python's constructor.
#[tokio::test]
async fn db_open_once_for_state_compare() {
    let Some(dsn) = std::env::var("PL_W1A_OPEN_DSN")
        .ok()
        .filter(|d| !d.is_empty())
    else {
        eprintln!("PL_W1A_OPEN_DSN unset: skipping");
        return;
    };
    // This hook is harness-only: independently verify the connected server
    // before dispatching the constructor which executes DDL.
    let (check, task) = side_client(&dsn).await;
    let name = simple_value(&check, "SELECT current_database()")
        .await
        .unwrap()
        .flatten()
        .unwrap();
    assert!(
        name.starts_with("pl_cf_"),
        "state comparison requires a disposable harness bank"
    );
    assert_eq!(Some(name.as_str()), parse_dsn(&dsn).unwrap().get_dbname());
    drop(check);
    let _ = task.await;
    let result = Storage::open(&dsn).await;
    if let Ok(expected) = std::env::var("PL_SCHEMA_EXPECT_REFUSAL") {
        match result {
            Err(OpenError::Refused(message)) => assert!(message.contains(&expected), "{message}"),
            Err(error) => panic!("expected schema refusal, got {error:?}"),
            Ok(s) => {
                s.close().await;
                panic!("opened a refused schema");
            }
        }
        return;
    }
    let s = result.expect("open");
    s.check_search_path().await.expect("search_path");
    s.close().await;
}

// ---- writer-session probe and reconnect (postgres.py `conn`, verify) ----

/// Ends `s`'s writer session from a side connection, as a PostgreSQL restart
/// or an idle-session kill would, and waits until the client has seen it.
async fn kill_writer(s: &Storage, dsn: &str) {
    let pid: i32 = s
        .client()
        .query_one("SELECT pg_backend_pid()", &[])
        .await
        .unwrap()
        .get(0);
    let (c, t) = side_client(dsn).await;
    c.execute("SELECT pg_terminate_backend($1)", &[&pid])
        .await
        .unwrap();
    // Until the backend has gone, lease and all, and the client knows.
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let alive: i64 = c
            .query_one(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = $1",
                &[&pid],
            )
            .await
            .unwrap()
            .get(0);
        if alive == 0 && s.client().is_closed() {
            break;
        }
        assert!(Instant::now() < deadline, "the killed session never ended");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    drop(c);
    let _ = t.await;
}

fn handover_message(from: i64, to: i64) -> String {
    format!(
        "another writer held this bank while this process was disconnected \
         (lease epoch {from} -> {to}); its resident copy may be stale, so \
         nothing is served or saved until it re-reads the bank"
    )
}

#[tokio::test]
async fn db_live_writer_session_is_kept() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let s = Storage::open(&dsn).await.expect("open");
    s.forget_probe();
    assert!(s.verify_writer_session().await.expect("verify").is_none());
    s.close().await;
}

#[tokio::test]
async fn db_dead_writer_session_reconnects_without_a_handover() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let s = Storage::open(&dsn).await.expect("open");
    let e1 = s.lease_epoch();
    kill_writer(&s, &dsn).await;
    let fresh = s
        .verify_writer_session()
        .await
        .expect("verify")
        .expect("a dead session is replaced");
    assert_eq!(fresh.lease_epoch(), e1 + 1, "the reconnect bumps the epoch");
    assert_eq!(fresh.resident_invalidated(), None, "no other writer ran");
    fresh
        .client()
        .batch_execute("SELECT 1")
        .await
        .expect("live");
    fresh.ping().await.expect("ping");
    // The replacement holds the lease: a second writer is still refused.
    assert!(matches!(
        Storage::open(&dsn).await,
        Err(OpenError::LeaseHeld(_))
    ));
    drop(s);
    fresh.close().await;
}

#[tokio::test]
async fn db_reconnect_after_another_writer_flags_the_handover() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let s = Storage::open(&dsn).await.expect("open");
    let e1 = s.lease_epoch();
    kill_writer(&s, &dsn).await;
    let other = Storage::open(&dsn)
        .await
        .expect("another writer takes the bank");
    assert_eq!(other.lease_epoch(), e1 + 1);
    other.close().await;
    let fresh = s
        .verify_writer_session()
        .await
        .expect("verify")
        .expect("replaced");
    assert_eq!(fresh.lease_epoch(), e1 + 2);
    let message = handover_message(e1, e1 + 1);
    assert_eq!(
        fresh.resident_invalidated().as_deref(),
        Some(message.as_str())
    );
    // /health reports it until the owner has re-read the bank.
    assert_eq!(fresh.ping().await, Err(message));
    fresh.acknowledge_rehydration();
    assert_eq!(fresh.resident_invalidated(), None);
    fresh.ping().await.expect("ping after the re-read");
    drop(s);
    fresh.close().await;
}

#[tokio::test]
async fn db_reconnect_refused_while_another_writer_holds_the_lease() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let s = Storage::open(&dsn).await.expect("open");
    let e1 = s.lease_epoch();
    kill_writer(&s, &dsn).await;
    let other = Storage::open(&dsn)
        .await
        .expect("another writer takes the bank");
    let refused = s.verify_writer_session().await.err().expect("refused");
    let dbname = parse_dsn(&dsn).unwrap().get_dbname().unwrap().to_string();
    assert!(
        refused.starts_with(&format!(
            "another process holds the writer lease on bank '{dbname}': backend pid "
        )),
        "{refused}"
    );
    // ping reports the lost lease while its holder is there.
    assert_eq!(s.ping().await, Err(refused.clone()));
    // Inside the retry window the cached refusal answers at once, without
    // another lease wait (_LEASE_WAIT is 2 s).
    let started = Instant::now();
    assert_eq!(s.verify_writer_session().await.err(), Some(refused));
    assert!(started.elapsed() < Duration::from_millis(1500));
    other.close().await;
    // The holder left: ping no longer reports the old refusal.
    s.ping().await.expect("ping once the holder is gone");
    tokio::time::sleep(LEASE_RETRY).await;
    let fresh = s
        .verify_writer_session()
        .await
        .expect("verify after the window")
        .expect("replaced");
    assert_eq!(fresh.lease_epoch(), e1 + 2);
    assert_eq!(
        fresh.resident_invalidated(),
        Some(handover_message(e1, e1 + 1))
    );
    drop(s);
    fresh.close().await;
}

#[tokio::test]
async fn db_unreachable_reconnect_is_tolerated_and_retried_after_its_window() {
    let Some(dsn) = test_dsn() else { return };
    let _guard = DB.lock().await;
    let mut s = Storage::open(&dsn).await.expect("open");
    let e1 = s.lease_epoch();
    kill_writer(&s, &dsn).await;
    // The server is unreachable: port 1 on loopback refuses at once.
    let reachable = std::mem::replace(
        &mut s.base,
        parse_dsn("postgresql://nobody@127.0.0.1:1/pl_cf_w1a_none").unwrap(),
    );
    assert!(
        s.verify_writer_session()
            .await
            .expect("tolerated")
            .is_none()
    );
    let failure = s
        .session
        .lock()
        .unwrap()
        .reconnect_error
        .clone()
        .expect("recorded");
    assert!(failure.starts_with("reconnect failed: "), "{failure}");
    // Reachable again, but inside the window no attempt is made.
    s.base = reachable;
    assert!(
        s.verify_writer_session()
            .await
            .expect("tolerated")
            .is_none()
    );
    tokio::time::sleep(RECONNECT_RETRY).await;
    let fresh = s
        .verify_writer_session()
        .await
        .expect("verify after the window")
        .expect("replaced");
    assert_eq!(fresh.lease_epoch(), e1 + 1);
    assert_eq!(fresh.resident_invalidated(), None);
    drop(s);
    fresh.close().await;
}
