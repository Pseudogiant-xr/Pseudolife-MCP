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
    let s = Storage::open(&dsn).await.expect("open");
    s.check_search_path().await.expect("search_path");
    s.close().await;
}
