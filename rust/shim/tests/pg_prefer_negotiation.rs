//! Protocol retry boundaries; these controls do not prove PostgreSQL TLS parity.
use pseudolife_stdio::pg::{Dsn, Error, Session, TlsEnvironment};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
use std::time::Duration;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

const SSL_REQUEST: [u8; 8] = [0, 0, 0, 8, 4, 210, 22, 47];
async fn frame(stream: &mut TcpStream) -> (u8, Vec<u8>) {
    let kind = stream.read_u8().await.unwrap();
    let len = stream.read_u32().await.unwrap();
    assert!((4..4096).contains(&len));
    let mut body = vec![0; (len - 4) as usize];
    stream.read_exact(&mut body).await.unwrap();
    (kind, body)
}
async fn send(stream: &mut TcpStream, kind: u8, body: &[u8]) {
    stream.write_u8(kind).await.unwrap();
    stream.write_u32((body.len() + 4) as u32).await.unwrap();
    stream.write_all(body).await.unwrap();
}
async fn startup(stream: &mut TcpStream) {
    let len = stream.read_u32().await.unwrap();
    assert!((8..4096).contains(&len));
    let mut body = vec![0; (len - 4) as usize];
    stream.read_exact(&mut body).await.unwrap();
    assert_eq!(&body[..4], &196608u32.to_be_bytes());
}
async fn ready(stream: &mut TcpStream) {
    send(stream, b'R', &0u32.to_be_bytes()).await;
    send(stream, b'Z', b"I").await;
    let (kind, _) = frame(stream).await;
    assert_eq!(kind, b'Q');
    send(stream, b'C', b"SET\0").await;
    send(stream, b'C', b"SET\0").await;
    send(stream, b'Z', b"I").await;
    assert_eq!(frame(stream).await.0, b'X');
}
fn dsn(listener: &TcpListener, mode: &str) -> Dsn {
    Dsn::parse(&format!(
        "host=127.0.0.1 port={} user=fixture dbname=plbench_protocol sslmode={mode}",
        listener.local_addr().unwrap().port()
    ))
    .unwrap()
}
#[tokio::test]
async fn prefer_failed_tls_gets_one_fresh_plaintext_connection() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let config = dsn(&listener, "prefer");
    let server = tokio::spawn(async move {
        let (mut first, _) = listener.accept().await.unwrap();
        let mut request = [0; 8];
        first.read_exact(&mut request).await.unwrap();
        assert_eq!(request, SSL_REQUEST);
        first.write_all(b"Sinvalid TLS record").await.unwrap();
        drop(first);
        let (mut second, _) = listener.accept().await.unwrap();
        startup(&mut second).await;
        ready(&mut second).await;
        assert!(
            tokio::time::timeout(Duration::from_millis(100), listener.accept())
                .await
                .is_err()
        );
    });
    let session = tokio::time::timeout(
        Duration::from_secs(3),
        Session::open_in(&config, &TlsEnvironment::default()),
    )
    .await
    .unwrap()
    .unwrap();
    session.close().await.unwrap();
    tokio::time::timeout(Duration::from_secs(3), server)
        .await
        .unwrap()
        .unwrap();
}
async fn no_retry(mode: &str, reply: &[u8], plaintext_auth_error: bool) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let config = dsn(&listener, mode);
    let reply = reply.to_vec();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = [0; 8];
        stream.read_exact(&mut request).await.unwrap();
        assert_eq!(request, SSL_REQUEST);
        stream.write_all(&reply).await.unwrap();
        if plaintext_auth_error {
            startup(&mut stream).await;
            send(
                &mut stream,
                b'E',
                b"SFATAL\0C28000\0Mfixture authentication refusal\0\0",
            )
            .await;
        }
        drop(stream);
        assert!(
            tokio::time::timeout(Duration::from_millis(150), listener.accept())
                .await
                .is_err()
        );
    });
    assert!(matches!(
        tokio::time::timeout(
            Duration::from_secs(3),
            Session::open_in(&config, &TlsEnvironment::default())
        )
        .await
        .unwrap(),
        Err(Error::Connection)
    ));
    server.await.unwrap();
}
#[tokio::test]
async fn require_tls_failure_never_retries_plaintext() {
    no_retry("require", b"Sinvalid TLS record", false).await;
}
#[tokio::test]
async fn prefer_invalid_sslrequest_reply_never_retries_plaintext() {
    no_retry("prefer", b"X", false).await;
}
#[tokio::test]
async fn prefer_sslrequest_error_never_retries_plaintext() {
    no_retry("prefer", b"E", false).await;
}
#[tokio::test]
async fn prefer_plaintext_auth_error_has_no_second_plaintext_attempt() {
    no_retry("prefer", b"N", true).await;
}

// Each row exercises the actual Session::open_in guard and SSLRequest arm.
// The fixture certificate only enables strict-mode connector construction;
// malformed handshakes prove no certificate acceptance or real PG behavior.
#[tokio::test]
async fn each_sslmode_pins_negotiation_errors_and_attempt_counts() {
    let directory =
        std::env::temp_dir().join(format!("plbench-pg-protocol-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&directory).unwrap();
    let root = directory.join("root.crt");
    std::fs::write(&root, include_bytes!("fixtures/pg_protocol_root.pem")).unwrap();
    for mode in ["disable", "prefer", "require", "verify-ca", "verify-full"] {
        for reply in [
            b"N".as_slice(),
            b"X".as_slice(),
            b"Sinvalid TLS record".as_slice(),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let config = dsn(&listener, mode);
            let environment = TlsEnvironment {
                default_directory: Some(directory.clone()),
                ambient_controls: vec![],
            };
            let count = Arc::new(AtomicUsize::new(0));
            let recorded = count.clone();
            let expected = if mode == "prefer" && reply[0] == b'S' {
                2
            } else {
                1
            };
            let disabled = mode == "disable";
            let prefer = mode == "prefer";
            let name = reply_name(reply[0]);
            let reply = reply.to_vec();
            let server = tokio::spawn(async move {
                for attempt in 0..expected {
                    let (mut stream, _) = listener.accept().await.unwrap();
                    recorded.fetch_add(1, Ordering::SeqCst);
                    if disabled || attempt == 1 {
                        startup(&mut stream).await;
                        send(
                            &mut stream,
                            b'E',
                            b"SFATAL\0C28000\0Mfixture authentication refusal\0\0",
                        )
                        .await;
                    } else {
                        let mut request = [0; 8];
                        stream.read_exact(&mut request).await.unwrap();
                        assert_eq!(request, SSL_REQUEST);
                        stream.write_all(&reply).await.unwrap();
                        if prefer && reply[0] == b'N' {
                            startup(&mut stream).await;
                            send(
                                &mut stream,
                                b'E',
                                b"SFATAL\0C28000\0Mfixture authentication refusal\0\0",
                            )
                            .await;
                        }
                    }
                    drop(stream);
                }
                assert!(
                    tokio::time::timeout(Duration::from_millis(100), listener.accept())
                        .await
                        .is_err()
                );
            });
            assert!(
                matches!(
                    tokio::time::timeout(
                        Duration::from_secs(3),
                        Session::open_in(&config, &environment)
                    )
                    .await
                    .unwrap(),
                    Err(Error::Connection)
                ),
                "{mode} {name}"
            );
            tokio::time::timeout(Duration::from_secs(3), server)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(count.load(Ordering::SeqCst), expected, "{mode}");
        }
    }
    std::fs::remove_dir_all(directory).unwrap();
}

fn reply_name(reply: u8) -> &'static str {
    match reply {
        b'N' => "server N",
        b'X' => "garbage",
        _ => "handshake failure",
    }
}
