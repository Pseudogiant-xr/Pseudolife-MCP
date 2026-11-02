//! Real PostgreSQL matrix supplied by the disposable TLS harness.

use std::time::Duration;
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    net::TcpListener,
};

const SSL_REQUEST: [u8; 8] = [0, 0, 0, 8, 4, 210, 22, 47];
async fn startup<S: AsyncRead + Unpin>(stream: &mut S) {
    let length = stream.read_u32().await.unwrap();
    assert!((8..4096).contains(&length));
    let mut body = vec![0; (length - 4) as usize];
    stream.read_exact(&mut body).await.unwrap();
    assert_eq!(&body[..4], &196608u32.to_be_bytes());
}
async fn frame<S: AsyncWrite + Unpin>(stream: &mut S, kind: u8, body: &[u8]) {
    stream.write_u8(kind).await.unwrap();
    stream.write_u32((body.len() + 4) as u32).await.unwrap();
    stream.write_all(body).await.unwrap();
}
async fn ready<S: AsyncRead + AsyncWrite + Unpin>(stream: &mut S) {
    frame(stream, b'R', &0u32.to_be_bytes()).await;
    frame(stream, b'Z', b"I").await;
    assert_eq!(stream.read_u8().await.unwrap(), b'X');
}
fn protocol_config(listener: &TcpListener, mode: &str) -> super::Config {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../shim/tests/fixtures/pg_tls/ca.pem");
    let root = root
        .to_string_lossy()
        .replace('\\', "\\\\")
        .replace('\'', "\\'");
    super::tls::parse_in(
        &format!(
            "host=127.0.0.1 port={} user=fixture dbname=pl_cf_tls sslmode={mode} sslrootcert='{}'",
            listener.local_addr().unwrap().port(),
            root
        ),
        super::tls::Environment::default(),
    )
    .unwrap()
}

#[tokio::test]
async fn review_hostaddr_only_negotiates_without_a_hostname() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let config = super::tls::parse_in(
        &format!(
            "hostaddr=127.0.0.1 port={} user=fixture dbname=pl_cf_tls",
            listener.local_addr().unwrap().port()
        ),
        super::tls::Environment::default(),
    )
    .unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = [0; 8];
        stream.read_exact(&mut request).await.unwrap();
        assert_eq!(request, SSL_REQUEST);
        stream.write_all(b"N").await.unwrap();
        startup(&mut stream).await;
        ready(&mut stream).await;
    });
    let (client, task) = super::connect(&config, Duration::from_secs(3))
        .await
        .unwrap();
    drop(client);
    task.await.unwrap();
    tokio::time::timeout(Duration::from_secs(3), server)
        .await
        .unwrap()
        .unwrap();
}

#[cfg(unix)]
#[tokio::test]
async fn review_unix_sockets_ignore_all_tls_modes() {
    let directory = std::env::temp_dir().join(format!("pl-tls-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&directory).unwrap();
    let path = directory.join(".s.PGSQL.54823");
    for mode in [
        "disable",
        "allow",
        "prefer",
        "require",
        "verify-ca",
        "verify-full",
    ] {
        let listener = tokio::net::UnixListener::bind(&path).unwrap();
        let config = super::tls::parse_in(
            &format!(
                "host='{}' port=54823 user=fixture dbname=pl_cf_tls sslmode={mode}",
                directory.display()
            ),
            super::tls::Environment::default(),
        )
        .unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            startup(&mut stream).await;
            ready(&mut stream).await;
        });
        let (client, task) = super::connect(&config, Duration::from_secs(3))
            .await
            .unwrap();
        drop(client);
        task.await.unwrap();
        tokio::time::timeout(Duration::from_secs(3), server)
            .await
            .unwrap()
            .unwrap();
        std::fs::remove_file(&path).unwrap();
    }
    std::fs::remove_dir(&directory).unwrap();
}

#[tokio::test]
async fn prefer_failed_tls_retries_plaintext_once() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let config = protocol_config(&listener, "prefer");
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = [0; 8];
        stream.read_exact(&mut request).await.unwrap();
        assert_eq!(request, SSL_REQUEST);
        stream.write_all(b"Sinvalid TLS record").await.unwrap();
        drop(stream);
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        ready(&mut stream).await;
        assert!(
            tokio::time::timeout(Duration::from_millis(100), listener.accept())
                .await
                .is_err()
        );
    });
    let (client, task) = super::connect(&config, Duration::from_secs(3))
        .await
        .unwrap();
    drop(client);
    task.await.unwrap();
    tokio::time::timeout(Duration::from_secs(3), server)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn ssl_response_and_auth_errors_never_authorize_an_extra_plaintext_attempt() {
    for (mode, reply, auth_error) in [
        ("prefer", b"X".as_slice(), false),
        ("prefer", b"E".as_slice(), false),
        ("prefer", b"N".as_slice(), true),
        ("require", b"N".as_slice(), false),
        ("require", b"Sinvalid TLS record".as_slice(), false),
        ("verify-ca", b"Sinvalid TLS record".as_slice(), false),
        ("verify-full", b"Sinvalid TLS record".as_slice(), false),
    ] {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let config = protocol_config(&listener, mode);
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = [0; 8];
            stream.read_exact(&mut request).await.unwrap();
            assert_eq!(request, SSL_REQUEST);
            stream.write_all(reply).await.unwrap();
            if auth_error {
                startup(&mut stream).await;
                frame(
                    &mut stream,
                    b'E',
                    b"SFATAL\0C28000\0Mfixture authentication refusal\0\0",
                )
                .await;
            }
            drop(stream);
            assert!(
                tokio::time::timeout(Duration::from_millis(100), listener.accept())
                    .await
                    .is_err()
            );
        });
        assert!(
            super::connect(&config, Duration::from_secs(3))
                .await
                .is_err(),
            "{mode}"
        );
        tokio::time::timeout(Duration::from_secs(3), server)
            .await
            .unwrap()
            .unwrap();
    }
}

#[tokio::test]
async fn allow_retries_tls_after_plaintext_auth_refusal() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let config = protocol_config(&listener, "allow");
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        startup(&mut stream).await;
        frame(
            &mut stream,
            b'E',
            b"SFATAL\0C28000\0Mfixture requires TLS\0\0",
        )
        .await;
        drop(stream);
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = [0; 8];
        stream.read_exact(&mut request).await.unwrap();
        assert_eq!(request, SSL_REQUEST);
        stream.write_all(b"N").await.unwrap();
        drop(stream);
        assert!(
            tokio::time::timeout(Duration::from_millis(100), listener.accept())
                .await
                .is_err()
        );
    });
    assert!(
        super::connect(&config, Duration::from_secs(3))
            .await
            .is_err()
    );
    tokio::time::timeout(Duration::from_secs(3), server)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn connection_timeout_bounds_negotiation() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let config = protocol_config(&listener, "prefer");
    let (accepted, arrived) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = [0; 8];
        stream.read_exact(&mut request).await.unwrap();
        assert_eq!(request, SSL_REQUEST);
        accepted.send(()).unwrap();
        // The peer must close when the whole startup deadline expires.
        let mut byte = [0];
        assert_eq!(stream.read(&mut byte).await.unwrap(), 0);
    });
    let connect =
        tokio::spawn(async move { super::connect(&config, Duration::from_secs(1)).await });
    tokio::time::timeout(Duration::from_secs(3), arrived)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        connect.await.unwrap().unwrap_err(),
        "connection timeout expired"
    );
    tokio::time::timeout(Duration::from_secs(3), server)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn required_modes_name_invalid_root_without_exposing_its_path() {
    for mode in ["require", "verify-ca", "verify-full"] {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        // A known non-PEM file tests the real configuration error chain.
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml");
        let encoded = path
            .to_string_lossy()
            .replace('\\', "\\\\")
            .replace('\'', "\\'");
        let config = super::tls::parse_in(&format!(
            "host=127.0.0.1 port={} user=fixture dbname=pl_cf_tls sslmode={mode} sslrootcert='{encoded}'",
            listener.local_addr().unwrap().port()
        ), super::tls::Environment::default()).unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = [0; 8];
            stream.read_exact(&mut request).await.unwrap();
            assert_eq!(request, SSL_REQUEST);
            stream.write_all(b"S").await.unwrap();
            assert_eq!(stream.read(&mut [0]).await.unwrap(), 0);
        });
        let error = super::connect(&config, Duration::from_secs(3))
            .await
            .unwrap_err();
        assert!(
            error.contains("sslrootcert") && error.contains("PEM"),
            "{error}"
        );
        assert!(!error.contains("Cargo.toml"));
        tokio::time::timeout(Duration::from_secs(3), server)
            .await
            .unwrap()
            .unwrap();
    }
}

#[tokio::test]
async fn disposable_postgres_tls_matrix() {
    let Ok(path) = std::env::var("PSEUDOLIFE_TEST_TLS_CASES") else {
        return;
    };
    let cases: serde_json::Value =
        serde_json::from_slice(&std::fs::read(path).expect("TLS case file")).unwrap();
    let mut failures = Vec::new();
    for case in cases.as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let dsn = case["dsn"].as_str().unwrap();
        // This hook is for a newly created, loopback-only disposable server.
        // Validate through the plain driver's parser without TLS-only fields.
        let guard: tokio_postgres::Config = case["guard_dsn"].as_str().unwrap().parse().unwrap();
        assert_eq!(guard.get_dbname(), Some("pl_cf_tls"));
        assert!(guard.get_hosts().iter().all(|host| matches!(host,
            tokio_postgres::config::Host::Tcp(value) if value == "localhost" || value == "127.0.0.1")));
        assert!(
            guard
                .get_hostaddrs()
                .iter()
                .all(|address| *address == std::net::IpAddr::from([127, 0, 0, 1]))
        );
        assert!(!guard.get_hosts().is_empty() || !guard.get_hostaddrs().is_empty());
        assert!(
            guard
                .get_ports()
                .iter()
                .all(|port| *port > 1024 && ![5432, 5433, 5434].contains(port))
        );
        // Require the same target in the tested DSN; fixture paths remain private.
        assert!(dsn.starts_with(case["guard_dsn"].as_str().unwrap()));
        // Fixture DSNs carry every option under test. Never read the test
        // user's ambient libpq files or environment into this connection.
        let mut environment = super::tls::Environment::default();
        if let Some(values) = case["environment"].as_object() {
            for (name, value) in values {
                assert_eq!(name, "PGCHANNELBINDING");
                environment
                    .values
                    .insert(name.clone(), value.as_str().unwrap().into());
            }
        }
        let result = match super::tls::parse_in(dsn, environment) {
            Ok(config) => {
                assert!(
                    config.get_dbname() == guard.get_dbname()
                        && config.get_hosts() == guard.get_hosts()
                        && config.get_hostaddrs() == guard.get_hostaddrs()
                        && config.get_ports() == guard.get_ports(),
                    "fixture DSN cannot override its disposable target"
                );
                super::connect(&config, std::time::Duration::from_secs(5)).await
            }
            Err(error) => Err(error),
        };
        let expected = case["accepted"].as_bool().unwrap();
        match result {
            Ok((client, task)) => {
                let tls: bool = client
                    .query_one(
                        "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()",
                        &[],
                    )
                    .await
                    .unwrap()
                    .get(0);
                if !expected || Some(tls) != case["tls"].as_bool() {
                    failures.push(name.to_owned());
                }
                drop(client);
                task.await.unwrap();
            }
            Err(_) if expected => failures.push(name.to_owned()),
            Err(_) => (),
        }
    }
    assert!(failures.is_empty(), "TLS cases failed: {failures:?}");
}
