use super::*;
use crate::txn::tests::{completion, query, startup, terminal_error};
use tokio::net::TcpListener;

#[tokio::test]
async fn aborted_board_transaction_cannot_report_success() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        assert_eq!(query(&mut stream).await, "START TRANSACTION");
        completion(&mut stream, b"BEGIN\0", b'T').await;
        assert_eq!(query(&mut stream).await, "BAD STATEMENT");
        terminal_error(&mut stream, "22012").await;
        // PostgreSQL answers COMMIT in an aborted block with ROLLBACK,
        // without an ErrorResponse. A successful pre-commit probe is needed.
        let next = query(&mut stream).await;
        if next == "SELECT 1" {
            terminal_error(&mut stream, "25P02").await;
            assert_eq!(query(&mut stream).await, "ROLLBACK");
        } else {
            assert_eq!(next, "COMMIT");
        }
        completion(&mut stream, b"ROLLBACK\0", b'I').await;
        assert_eq!(query(&mut stream).await, "NEXT CALL");
        completion(&mut stream, b"SELECT 0\0", b'I').await;
    });
    let dsn = format!(
        "host=127.0.0.1 port={} user=fixture sslmode=disable",
        address.port()
    );
    let (mut client, connection) = tokio_postgres::connect(&dsn, NoTls).await.unwrap();
    let driver = tokio::spawn(connection);
    let tx = client.transaction().await.unwrap();
    assert!(tx.batch_execute("BAD STATEMENT").await.is_err());
    let result = commit(tx).await;
    client.batch_execute("NEXT CALL").await.unwrap();
    server.await.unwrap();
    drop(client);
    driver.abort();
    assert!(
        result.is_err(),
        "an aborted board transaction was reported committed"
    );
}

#[tokio::test]
async fn cancelled_board_call_rolls_back_before_next_use() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let (entered, entry) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        assert_eq!(query(&mut stream).await, "START TRANSACTION");
        completion(&mut stream, b"BEGIN\0", b'T').await;
        entered.send(()).unwrap();
        assert_eq!(query(&mut stream).await, "ROLLBACK");
        completion(&mut stream, b"ROLLBACK\0", b'I').await;
        assert_eq!(query(&mut stream).await, "NEXT CALL");
        completion(&mut stream, b"SELECT 0\0", b'I').await;
    });
    let dsn = format!(
        "host=127.0.0.1 port={} user=fixture sslmode=disable",
        address.port()
    );
    let (mut client, connection) = tokio_postgres::connect(&dsn, NoTls).await.unwrap();
    let driver = tokio::spawn(connection);
    let call = async {
        let _tx = client.transaction().await.unwrap();
        std::future::pending::<()>().await;
    };
    // Dropping the request future must retain the driver's rollback ordering.
    {
        tokio::pin!(call);
        tokio::select! {
            _ = &mut call => panic!("call should wait"),
            _ = entry => (),
        }
    }
    client.batch_execute("NEXT CALL").await.unwrap();
    server.await.unwrap();
    driver.abort();
}

#[tokio::test]
async fn lost_board_commit_fails_and_next_call_reconnects() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        assert_eq!(
            query(&mut stream).await,
            "SET lock_timeout = '5s'; SET search_path TO public"
        );
        completion(&mut stream, b"SET\0", b'I').await;
        assert_eq!(query(&mut stream).await, "START TRANSACTION");
        completion(&mut stream, b"BEGIN\0", b'T').await;
        assert_eq!(query(&mut stream).await, "SELECT 1");
        completion(&mut stream, b"SELECT 1\0", b'T').await;
        assert_eq!(query(&mut stream).await, "COMMIT");
        // No acknowledgement: never retry this potentially committed write.
        drop(stream);
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        assert_eq!(
            query(&mut stream).await,
            "SET lock_timeout = '5s'; SET search_path TO public"
        );
        completion(&mut stream, b"SET\0", b'I').await;
        assert_eq!(query(&mut stream).await, "NEXT CALL");
        completion(&mut stream, b"SELECT 0\0", b'I').await;
    });
    let dsn = format!(
        "host=127.0.0.1 port={} user=fixture sslmode=disable",
        address.port()
    );
    let mut conn = Conn::new(dsn);
    let client = conn.client().await.unwrap();
    let tx = client.transaction().await.unwrap();
    assert!(commit(tx).await.is_err());
    conn.client()
        .await
        .unwrap()
        .batch_execute("NEXT CALL")
        .await
        .unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn board_commit_is_independent_of_the_writer_adapter() {
    let _test = crate::txn::tests::TESTS.lock().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        assert_eq!(query(&mut stream).await, "START TRANSACTION");
        completion(&mut stream, b"BEGIN\0", b'T').await;
        assert_eq!(query(&mut stream).await, "SELECT 1");
        completion(&mut stream, b"SELECT 1\0", b'T').await;
        assert_eq!(query(&mut stream).await, "COMMIT");
        completion(&mut stream, b"COMMIT\0", b'I').await;
    });
    let dsn = format!(
        "host=127.0.0.1 port={} user=fixture sslmode=disable",
        address.port()
    );
    let (mut client, connection) = tokio_postgres::connect(&dsn, NoTls).await.unwrap();
    let driver = tokio::spawn(connection);
    // Enter the actual writer task-local boundary using a separate idle client.
    // The board must neither wait for its lock nor trip its reentrancy guard.
    let writer_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let writer_address = writer_listener.local_addr().unwrap();
    let writer_server = tokio::spawn(async move {
        let (mut stream, _) = writer_listener.accept().await.unwrap();
        startup(&mut stream).await;
        std::future::pending::<()>().await;
    });
    let writer_dsn = format!(
        "host=127.0.0.1 port={} user=fixture sslmode=disable",
        writer_address.port()
    );
    let (writer, connection) = tokio_postgres::connect(&writer_dsn, NoTls).await.unwrap();
    let writer_driver = tokio::spawn(connection);
    crate::txn::with_client(&writer, |_| async {
        let tx = client.transaction().await?;
        commit(tx).await
    })
    .await
    .unwrap();
    server.await.unwrap();
    driver.abort();
    writer_driver.abort();
    writer_server.abort();
}
