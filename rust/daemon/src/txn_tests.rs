use super::*;
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;
use tokio_postgres::NoTls;

// OPEN belongs to the process's one leased writer. Keep isolated peers from
// deliberately exercising that marker concurrently with each other.
static TESTS: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

#[derive(Debug)]
enum Failure {
    Operational(tokio_postgres::Error),
    Refusal,
}

impl From<tokio_postgres::Error> for Failure {
    fn from(error: tokio_postgres::Error) -> Self {
        Self::Operational(error)
    }
}

async fn message(stream: &mut TcpStream, tag: u8, data: &[u8]) {
    stream.write_u8(tag).await.unwrap();
    stream.write_u32((data.len() + 4) as u32).await.unwrap();
    stream.write_all(data).await.unwrap();
}

async fn completion(stream: &mut TcpStream, tag: &[u8], state: u8) {
    message(stream, b'C', tag).await;
    message(stream, b'Z', &[state]).await;
}

async fn startup(stream: &mut TcpStream) {
    let length = stream.read_u32().await.unwrap();
    let mut data = vec![0; (length - 4) as usize];
    stream.read_exact(&mut data).await.unwrap();
    message(stream, b'R', &0_i32.to_be_bytes()).await;
    message(stream, b'S', b"server_version\x0017.0\0").await;
    message(stream, b'Z', b"I").await;
}

async fn query(stream: &mut TcpStream) -> String {
    assert_eq!(stream.read_u8().await.unwrap(), b'Q');
    let length = stream.read_u32().await.unwrap();
    let mut data = vec![0; (length - 4) as usize];
    stream.read_exact(&mut data).await.unwrap();
    String::from_utf8(data[..data.len() - 1].to_vec()).unwrap()
}

async fn terminal_error(stream: &mut TcpStream, code: &str) {
    let data = format!("SERROR\0VERROR\0C{code}\0Mfixture terminal-command refusal\0\0");
    message(stream, b'E', data.as_bytes()).await;
    message(stream, b'Z', b"E").await;
}

async fn failed_exit_case(mode: &'static str) {
    let _test = TESTS.lock().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        let mut trace = vec![query(&mut stream).await];
        assert_eq!(trace[0], "BEGIN");
        completion(&mut stream, b"BEGIN\0", b'T').await;
        if mode != "recovery" {
            trace.push(query(&mut stream).await);
            assert_eq!(trace.last().unwrap(), "INSERT INTO fixture VALUES (1)");
            completion(&mut stream, b"INSERT 0 1\0", b'T').await;
        }
        trace.push(query(&mut stream).await);
        assert_eq!(
            trace.last().unwrap(),
            if mode.starts_with("commit") {
                "COMMIT"
            } else {
                "ROLLBACK"
            }
        );
        terminal_error(&mut stream, "57014").await;
        trace.push(query(&mut stream).await);
        let mut recovered = trace.last().unwrap() == "ROLLBACK";
        if recovered && mode == "commit-cleanup" {
            terminal_error(&mut stream, "57014").await;
            trace.push(query(&mut stream).await);
            recovered = trace.last().unwrap() == "ROLLBACK";
        }
        if recovered {
            completion(&mut stream, b"ROLLBACK\0", b'I').await;
            trace.push(query(&mut stream).await);
        }
        assert_eq!(trace.last().unwrap(), "INSERT INTO fixture VALUES (2)");
        if recovered {
            completion(&mut stream, b"INSERT 0 1\0", b'I').await;
        } else {
            terminal_error(&mut stream, "25P02").await;
        }
        (recovered, trace)
    });
    let dsn = format!(
        "host=127.0.0.1 port={} user=fixture dbname=fixture sslmode=disable",
        address.port()
    );
    let (client, connection) = tokio_postgres::connect(&dsn, NoTls).await.unwrap();
    let connection_task = tokio::spawn(connection);
    let client = Arc::new(client);
    let first = if mode == "recovery" {
        let (body_tx, body_rx) = oneshot::channel();
        let writer = client.clone();
        let task = tokio::spawn(async move {
            run(&writer, |_| async move {
                body_tx.send(()).unwrap();
                std::future::pending::<Result<(), tokio_postgres::Error>>().await
            })
            .await
        });
        body_rx.await.unwrap();
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        with_client(
            &client,
            |c| async move { c.batch_execute("NEVER RUN").await },
        )
        .await
        .map_err(Failure::from)
    } else {
        run(&client, |c| async move {
            c.batch_execute("INSERT INTO fixture VALUES (1)").await?;
            if mode == "body" {
                Err(Failure::Refusal)
            } else {
                Ok(())
            }
        })
        .await
    };
    if mode == "body" {
        assert!(matches!(first, Err(Failure::Refusal)));
    } else {
        assert!(
            matches!(&first, Err(Failure::Operational(error)) if error.code().unwrap().code() == "57014")
        );
    }
    let next = with_client(&client, |c| async move {
        c.batch_execute("INSERT INTO fixture VALUES (2)").await
    })
    .await;
    let (recovered, trace) = server.await.unwrap();
    drop(client);
    connection_task.abort();
    assert!(
        recovered && next.is_ok(),
        "{mode}: next statement received an unrecovered transaction; trace={trace:?}"
    );
}

#[tokio::test]
async fn failed_recovery_rollback_remains_open() {
    failed_exit_case("recovery").await;
}

#[tokio::test]
async fn failed_body_rollback_remains_open() {
    failed_exit_case("body").await;
}

#[tokio::test]
async fn failed_commit_is_cleaned_up() {
    failed_exit_case("commit").await;
}

#[tokio::test]
async fn failed_commit_cleanup_remains_open() {
    failed_exit_case("commit-cleanup").await;
}

#[tokio::test]
async fn cancelled_begin_is_recovered_before_single_statement() {
    let _test = TESTS.lock().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let (seen_tx, seen_rx) = oneshot::channel();
    let (release_tx, release_rx) = oneshot::channel();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        assert_eq!(query(&mut stream).await, "BEGIN");
        seen_tx.send(()).unwrap();
        release_rx.await.unwrap();
        completion(&mut stream, b"BEGIN\0", b'T').await;
        let mut in_transaction = true;
        let mut trace = vec!["BEGIN".to_string()];
        loop {
            let command = query(&mut stream).await;
            trace.push(command.clone());
            if command == "ROLLBACK" {
                in_transaction = false;
                completion(&mut stream, b"ROLLBACK\0", b'I').await;
            } else {
                assert_eq!(command, "INSERT INTO fixture VALUES (1)");
                completion(
                    &mut stream,
                    b"INSERT 0 1\0",
                    if in_transaction { b'T' } else { b'I' },
                )
                .await;
                return (!in_transaction, trace);
            }
        }
    });
    let dsn = format!(
        "host=127.0.0.1 port={} user=fixture dbname=fixture sslmode=disable",
        address.port()
    );
    let (client, connection) = tokio_postgres::connect(&dsn, NoTls).await.unwrap();
    let connection_task = tokio::spawn(connection);
    let client = Arc::new(client);
    let body_ran = Arc::new(AtomicBool::new(false));
    let writer = client.clone();
    let observed = body_ran.clone();
    let task = tokio::spawn(async move {
        run(&writer, |_c| async move {
            observed.store(true, Ordering::SeqCst);
            Ok::<_, tokio_postgres::Error>(())
        })
        .await
    });
    seen_rx.await.unwrap();
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert!(!body_ran.load(Ordering::SeqCst));
    release_tx.send(()).unwrap();
    with_client(&client, |c| async move {
        c.batch_execute("INSERT INTO fixture VALUES (1)").await
    })
    .await
    .unwrap();
    let (committed, trace) = server.await.unwrap();
    drop(client);
    connection_task.abort();
    assert!(
        committed,
        "single statement inherited the abandoned transaction"
    );
    assert_eq!(
        trace,
        ["BEGIN", "ROLLBACK", "INSERT INTO fixture VALUES (1)"]
    );
}

#[tokio::test]
async fn recovery_warning_is_not_an_error_when_begin_never_arrived() {
    let _test = TESTS.lock().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        assert_eq!(query(&mut stream).await, "ROLLBACK");
        message(
            &mut stream,
            b'N',
            b"SWARNING\0VWARNING\0C25P01\0Mthere is no transaction in progress\0\0",
        )
        .await;
        completion(&mut stream, b"ROLLBACK\0", b'I').await;
        assert_eq!(query(&mut stream).await, "INSERT INTO fixture VALUES (1)");
        completion(&mut stream, b"INSERT 0 1\0", b'I').await;
    });
    let dsn = format!(
        "host=127.0.0.1 port={} user=fixture dbname=fixture sslmode=disable",
        address.port()
    );
    let (client, connection) = tokio_postgres::connect(&dsn, NoTls).await.unwrap();
    let connection_task = tokio::spawn(connection);
    // The eager marker can remain set even if cancellation occurred before
    // BEGIN was sent. PostgreSQL's idle ROLLBACK is a warning, not failure.
    OPEN.store(true, Ordering::SeqCst);
    with_client(&client, |c| async move {
        c.batch_execute("INSERT INTO fixture VALUES (1)").await
    })
    .await
    .unwrap();
    server.await.unwrap();
    assert!(!OPEN.load(Ordering::SeqCst));
    drop(client);
    connection_task.abort();
}

#[tokio::test]
async fn typed_refusal_rolls_back_prior_write() {
    let _test = TESTS.lock().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        assert_eq!(query(&mut stream).await, "BEGIN");
        completion(&mut stream, b"BEGIN\0", b'T').await;
        assert_eq!(query(&mut stream).await, "INSERT INTO fixture VALUES (1)");
        completion(&mut stream, b"INSERT 0 1\0", b'T').await;
        assert_eq!(query(&mut stream).await, "ROLLBACK");
        completion(&mut stream, b"ROLLBACK\0", b'I').await;
        assert_eq!(query(&mut stream).await, "INSERT INTO fixture VALUES (2)");
        completion(&mut stream, b"INSERT 0 1\0", b'I').await;
    });
    let dsn = format!(
        "host=127.0.0.1 port={} user=fixture dbname=fixture sslmode=disable",
        address.port()
    );
    let (client, connection) = tokio_postgres::connect(&dsn, NoTls).await.unwrap();
    let connection_task = tokio::spawn(connection);
    let result = run(&client, |c| async move {
        c.batch_execute("INSERT INTO fixture VALUES (1)").await?;
        Err::<(), Failure>(Failure::Refusal)
    })
    .await;
    assert!(matches!(result, Err(Failure::Refusal)));
    assert!(!OPEN.load(Ordering::SeqCst));
    with_client(&client, |c| async move {
        c.batch_execute("INSERT INTO fixture VALUES (2)").await
    })
    .await
    .unwrap();
    server.await.unwrap();
    drop(client);
    connection_task.abort();
}
