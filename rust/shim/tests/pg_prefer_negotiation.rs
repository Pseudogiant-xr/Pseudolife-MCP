//! Protocol retry boundaries; these controls do not prove PostgreSQL TLS parity.
use pseudolife_stdio::pg::{Dsn, Error, Session, TlsEnvironment};
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
