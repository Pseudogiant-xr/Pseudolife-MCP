#![forbid(unsafe_code)]
use pseudolife_stdio::pg::{Dsn, Error, Session, SslMode, TlsEnvironment, resolution};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

fn cert_path(name: &str) -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/pg_tls")
        .join(name)
}
struct Directory(std::path::PathBuf);
impl Directory {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("pseudolife-pg-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Directory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn configured_producer_dsns_and_quoting_are_preserved() {
    for text in [
        "postgresql://fixture:fixture@127.0.0.1:5433/pseudolife_memory",
        "postgresql://fixture:fixture@pseudolife-pg:5432/pseudolife_memory",
        "postgres://postgres@localhost:5432/pseudolife_memory",
        "host=127.0.0.1 port=5433 user=fixture password='fixture' dbname=pseudolife_memory",
    ] {
        let dsn = Dsn::parse(text).unwrap();
        assert_eq!(dsn.ssl_mode(), SslMode::Prefer);
        assert_eq!(dsn.config().get_dbname(), Some("pseudolife_memory"));
    }
    let dsn = Dsn::parse("postgresql://fixture:p%40ss+word@localhost/db?password=query%2Bpassword&port=6543&host=127.0.0.1&sslmode=require").unwrap();
    assert_eq!(
        dsn.config().get_password(),
        Some(b"query+password".as_slice())
    );
    assert_eq!(dsn.config().get_ports(), &[6543]);
    assert_eq!(
        dsn.config().get_hosts(),
        &[tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
    );
    let dsn = Dsn::parse(
        "host=localhost user=fixture password='one\\'two\\\\three' dbname=db sslmode=disable",
    )
    .unwrap();
    assert_eq!(
        dsn.config().get_password(),
        Some(b"one'two\\three".as_slice())
    );
    assert_eq!(dsn.ssl_mode(), SslMode::Disable);
    assert!(!format!("{dsn:?}").contains("one"));
}
#[test]
fn unsupported_dsn_controls_are_named_and_never_ignored() {
    for option in [
        "sslcert",
        "sslkey",
        "sslcrl",
        "sslcrldir",
        "sslpassword",
        "sslnegotiation",
        "sslsni",
        "connect_timeout",
        "options",
        "application_name",
        "hostaddr",
        "service",
        "passfile",
        "channel_binding",
        "gssencmode",
        "target_session_attrs",
        "replication",
    ] {
        for dsn in [
            format!("host=localhost user=fixture {option}=fixture-value"),
            format!("postgresql://fixture@localhost/db?{option}=fixture-value"),
        ] {
            let error = Dsn::parse(&dsn).unwrap_err();
            assert_eq!(error, Error::UnsupportedOption(option.into()));
            assert_eq!(error.exit_code(), 1);
            assert_eq!(
                error.to_string(),
                format!("unsupported PostgreSQL DSN option: {option}")
            );
            assert!(!error.to_string().contains("fixture-value"));
        }
    }
    for mode in ["allow", "unknown", "Require", ""] {
        assert_eq!(
            Dsn::parse(&format!("host=localhost sslmode='{mode}'")).unwrap_err(),
            Error::UnsupportedOption("sslmode".into())
        );
    }
    for dsn in [
        "host='unterminated",
        "host localhost",
        "postgresql://fixture@localhost/db?sslmode=%GG",
        "postgresql://fixture@localhost/db#ignored",
        "host=localhost port=0",
    ] {
        assert!(Dsn::parse(dsn).is_err());
    }
}
#[test]
fn own_resolution_precedes_embedded_then_preserves_container_argv() {
    let directory = Directory::new();
    let bank = directory.0.join("embedded_pg");
    std::fs::create_dir(&bank).unwrap();
    std::fs::write(bank.join("PG_VERSION"), b"18\n").unwrap();
    assert!(matches!(
        resolution::select(
            Some("postgresql://fixture@localhost/db"),
            &directory.0,
            true
        )
        .unwrap(),
        resolution::Target::Direct(_)
    ));
    assert!(
        matches!(resolution::select(None, &directory.0, true).unwrap(), resolution::Target::ExistingEmbedded(path) if path == bank)
    );
    assert!(matches!(
        resolution::select(None, &directory.0, false).unwrap(),
        resolution::Target::Container
    ));
    assert!(resolution::select(Some("host=localhost options=unsafe"), &directory.0, true).is_err());
    let args = vec![
        "delegate".into(),
        "project".into(),
        "agent".into(),
        "--for".into(),
        "1d".into(),
    ];
    for terminal in [false, true] {
        let actual =
            resolution::container_exec(std::ffi::OsStr::new("docker"), terminal, "lease", &args);
        let mut expected: Vec<std::ffi::OsString> = [
            "docker",
            "exec",
            if terminal { "-it" } else { "-i" },
            "-e",
            "PSEUDOLIFE_DAEMON_EXEC=1",
            "pseudolife-mcp-daemon",
            "python",
            "-m",
            "pseudolife_memory.cli",
            "lease",
        ]
        .iter()
        .map(Into::into)
        .collect();
        expected.extend_from_slice(&args);
        assert_eq!(actual, expected);
    }
    assert_eq!(
        resolution::container_inspect(std::ffi::OsStr::new("docker")),
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            "pseudolife-mcp-daemon"
        ]
        .map(std::ffi::OsString::from)
    );
}

#[derive(Debug, Default)]
struct Receipt {
    ssl_requested: bool,
    tls: bool,
    startup: bool,
    setup: bool,
    terminated: bool,
}
async fn message<S: tokio::io::AsyncRead + Unpin>(
    stream: &mut S,
) -> std::io::Result<(u8, Vec<u8>)> {
    let tag = stream.read_u8().await?;
    let length = stream.read_u32().await?;
    assert!((4..=65536).contains(&length));
    let mut data = vec![0; length as usize - 4];
    stream.read_exact(&mut data).await?;
    Ok((tag, data))
}
async fn reply<S: tokio::io::AsyncWrite + Unpin>(stream: &mut S, tag: u8, data: &[u8]) {
    stream.write_u8(tag).await.unwrap();
    stream.write_u32(data.len() as u32 + 4).await.unwrap();
    stream.write_all(data).await.unwrap();
}
async fn protocol<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(
    mut stream: S,
    initial: Option<Vec<u8>>,
    mut receipt: Receipt,
) -> Receipt {
    let startup = if let Some(initial) = initial {
        initial
    } else {
        let length = stream.read_u32().await.unwrap();
        assert!((8..=65536).contains(&length));
        let mut data = vec![0; length as usize - 4];
        stream.read_exact(&mut data).await.unwrap();
        data
    };
    assert!(startup.starts_with(&[0, 3, 0, 0]));
    receipt.startup = true;
    reply(&mut stream, b'R', &[0, 0, 0, 0]).await;
    reply(&mut stream, b'S', b"client_encoding\0UTF8\0").await;
    reply(&mut stream, b'S', b"server_version\x0018.0\0").await;
    reply(&mut stream, b'Z', b"I").await;
    loop {
        match message(&mut stream).await {
            Ok((b'Q', data)) => {
                if data == b"SET lock_timeout = '5s'; SET search_path TO public\0" {
                    receipt.setup = true;
                }
                reply(&mut stream, b'C', b"SET\0").await;
                reply(&mut stream, b'C', b"SET\0").await;
                reply(&mut stream, b'Z', b"I").await;
            }
            Ok((b'X', data)) => {
                assert!(data.is_empty());
                receipt.terminated = true;
                break;
            }
            Err(_) => break,
            Ok((tag, _)) => panic!("unexpected PG transport tag {tag}"),
        }
    }
    receipt
}
async fn peer(tls: bool, certificate: &str) -> (u16, tokio::task::JoinHandle<Receipt>) {
    let socket = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
        .await
        .unwrap();
    let port = socket.local_addr().unwrap().port();
    let certificate = certificate.to_owned();
    let task = tokio::spawn(async move {
        let (mut stream, _) = socket.accept().await.unwrap();
        let length = stream.read_u32().await.unwrap();
        assert!((8..=65536).contains(&length));
        let mut request = vec![0; length as usize - 4];
        stream.read_exact(&mut request).await.unwrap();
        let mut receipt = Receipt::default();
        if request == [4, 210, 22, 47] {
            receipt.ssl_requested = true;
            stream
                .write_all(if tls { b"S" } else { b"N" })
                .await
                .unwrap();
            if tls {
                let bytes = std::fs::read(cert_path(&format!("{certificate}.pem"))).unwrap();
                let certs = rustls_pemfile::certs(&mut bytes.as_slice())
                    .collect::<Result<Vec<_>, _>>()
                    .unwrap();
                let bytes = std::fs::read(cert_path(&format!("{certificate}-key.pem"))).unwrap();
                let key = rustls_pemfile::private_key(&mut bytes.as_slice())
                    .unwrap()
                    .unwrap();
                let config = rustls::ServerConfig::builder_with_provider(std::sync::Arc::new(
                    rustls::crypto::aws_lc_rs::default_provider(),
                ))
                .with_safe_default_protocol_versions()
                .unwrap()
                .with_no_client_auth()
                .with_single_cert(certs, key)
                .unwrap();
                match tokio_rustls::TlsAcceptor::from(std::sync::Arc::new(config))
                    .accept(stream)
                    .await
                {
                    Ok(stream) => {
                        receipt.tls = true;
                        protocol(stream, None, receipt).await
                    }
                    Err(_) => receipt,
                }
            } else {
                let mut length = [0; 4];
                if stream.read_exact(&mut length).await.is_err() {
                    return receipt;
                }
                let length = u32::from_be_bytes(length);
                let mut startup = vec![0; length as usize - 4];
                stream.read_exact(&mut startup).await.unwrap();
                protocol(stream, Some(startup), receipt).await
            }
        } else {
            protocol(stream, Some(request), receipt).await
        }
    });
    (port, task)
}
async fn cell(
    mode: &str,
    tls: bool,
    host: &str,
    root: Option<&str>,
    default_ca: Option<&str>,
    certificate: &str,
    accepted: bool,
) {
    let directory = Directory::new();
    if let Some(ca) = default_ca {
        std::fs::copy(cert_path(ca), directory.0.join("root.crt")).unwrap();
    }
    let environment = TlsEnvironment {
        default_directory: Some(directory.0.clone()),
        ambient_controls: vec![],
    };
    let (port, peer) = peer(tls, certificate).await;
    let mut text = format!("host={host} port={port} user=fixture dbname=fixture sslmode={mode}");
    if let Some(ca) = root {
        let path = cert_path(ca)
            .to_string_lossy()
            .replace('\\', "\\\\")
            .replace('\'', "\\'");
        text.push_str(&format!(" sslrootcert='{path}'"));
    }
    let result = Session::open_in(&Dsn::parse(&text).unwrap(), &environment).await;
    assert_eq!(
        result.is_ok(),
        accepted,
        "mode={mode} TLS={tls} host={host} root={root:?} default={default_ca:?}"
    );
    if let Ok(session) = result {
        session.close().await.unwrap();
    }
    let receipt = tokio::time::timeout(Duration::from_secs(10), peer)
        .await
        .unwrap()
        .unwrap();
    println!(
        "cell mode={mode} TLS={tls} host={host} root={root:?} default={default_ca:?} accepted={accepted}: {receipt:?}"
    );
    assert_eq!(receipt.startup, accepted);
    assert_eq!(receipt.setup, accepted);
    assert_eq!(receipt.terminated, accepted);
    assert_eq!(receipt.ssl_requested, mode != "disable");
}
#[tokio::test]
async fn all_tls_modes_enforce_their_transport_and_root_contracts() {
    cell("disable", false, "127.0.0.1", None, None, "server", true).await;
    cell("prefer", false, "127.0.0.1", None, None, "server", true).await;
    for mode in ["require", "verify-ca", "verify-full"] {
        cell(
            mode,
            false,
            "127.0.0.1",
            Some("ca.pem"),
            None,
            "server",
            false,
        )
        .await;
    }
    for mode in ["prefer", "require"] {
        cell(mode, true, "127.0.0.1", None, None, "server", true).await;
        cell(
            mode,
            true,
            "127.0.0.1",
            None,
            Some("ca.pem"),
            "server",
            true,
        )
        .await;
        cell(
            mode,
            true,
            "127.0.0.1",
            None,
            Some("other-ca.pem"),
            "server",
            false,
        )
        .await;
        cell(
            mode,
            true,
            "127.0.0.1",
            Some("other-ca.pem"),
            None,
            "server",
            false,
        )
        .await;
    }
    cell(
        "verify-ca",
        true,
        "127.0.0.1",
        Some("ca.pem"),
        None,
        "server",
        true,
    )
    .await;
    cell(
        "verify-ca",
        true,
        "127.0.0.1",
        Some("other-ca.pem"),
        None,
        "server",
        false,
    )
    .await;
    cell(
        "verify-full",
        true,
        "localhost",
        Some("ca.pem"),
        None,
        "server",
        true,
    )
    .await;
    cell(
        "verify-full",
        true,
        "127.0.0.1",
        Some("ca.pem"),
        None,
        "server",
        false,
    )
    .await;
    cell(
        "verify-full",
        true,
        "localhost",
        Some("other-ca.pem"),
        None,
        "server",
        false,
    )
    .await;
}
#[tokio::test]
async fn ambient_and_unavailable_ca_controls_refuse_before_network() {
    let dsn = Dsn::parse("host=127.0.0.1 port=1 user=fixture sslmode=require").unwrap();
    for control in [
        "PGSSLROOTCERT",
        "PGTLS_TEST",
        "PGHOST",
        "PGPASSWORD",
        "PGSERVICE",
        "PGOPTIONS",
    ] {
        let environment = TlsEnvironment {
            default_directory: None,
            ambient_controls: vec![control.into()],
        };
        let error = match Session::open_in(&dsn, &environment).await {
            Ok(_) => panic!("ambient control admitted"),
            Err(error) => error,
        };
        assert_eq!(error, Error::AmbientControl(control.into()));
        assert_eq!(error.exit_code(), 1);
    }
    for name in ["postgresql.crt", "postgresql.key", "root.crl"] {
        let directory = Directory::new();
        std::fs::write(directory.0.join(name), b"fixture").unwrap();
        let environment = TlsEnvironment {
            default_directory: Some(directory.0.clone()),
            ambient_controls: vec![],
        };
        assert_eq!(
            environment.validate(),
            Err(Error::AmbientControl(name.into()))
        );
    }
    let environment = TlsEnvironment::default();
    for mode in ["verify-ca", "verify-full"] {
        let dsn = Dsn::parse(&format!("host=127.0.0.1 port=1 sslmode={mode}")).unwrap();
        assert!(matches!(
            Session::open_in(&dsn, &environment).await,
            Err(Error::RootCertificate)
        ));
    }
}

#[tokio::test]
async fn required_tls_refuses_plain_server_before_startup_or_authentication() {
    let socket = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
        .await
        .unwrap();
    let port = socket.local_addr().unwrap().port();
    let peer = tokio::spawn(async move {
        let (mut stream, _) = socket.accept().await.unwrap();
        let mut request = [0; 8];
        stream.read_exact(&mut request).await.unwrap();
        assert_eq!(request, [0, 0, 0, 8, 4, 210, 22, 47]);
        stream.write_all(b"N").await.unwrap();
        let mut extra = [0; 1];
        assert_eq!(stream.read(&mut extra).await.unwrap(), 0);
    });
    let mut config = tokio_postgres::Config::new();
    config
        .host("127.0.0.1")
        .port(port)
        .user("fixture")
        .dbname("fixture")
        .ssl_mode(tokio_postgres::config::SslMode::Require);
    let tls = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(
        rustls::crypto::aws_lc_rs::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_root_certificates(rustls::RootCertStore::empty())
    .with_no_client_auth();
    let result = tokio::time::timeout(
        Duration::from_secs(10),
        config.connect(tokio_postgres_rustls::MakeRustlsConnect::new(tls)),
    )
    .await
    .unwrap();
    assert!(result.is_err());
    tokio::time::timeout(Duration::from_secs(10), peer)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn prefer_never_sends_startup_after_an_invalid_ssl_response() {
    for response in [b'X', 0, 0xff, b'E'] {
        let socket = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = socket.local_addr().unwrap().port();
        let peer = tokio::spawn(async move {
            let (mut stream, _) = socket.accept().await.unwrap();
            let mut request = [0; 8];
            stream.read_exact(&mut request).await.unwrap();
            assert_eq!(request, [0, 0, 0, 8, 4, 210, 22, 47]);
            stream.write_u8(response).await.unwrap();
            let mut extra = [0; 1];
            assert_eq!(stream.read(&mut extra).await.unwrap(), 0);
        });
        let dsn = Dsn::parse(&format!(
            "host=127.0.0.1 port={port} user=fixture dbname=fixture sslmode=prefer"
        ))
        .unwrap();
        let error = match Session::open_in(&dsn, &TlsEnvironment::default()).await {
            Ok(_) => panic!("invalid SSL response admitted plaintext"),
            Err(error) => error,
        };
        assert_eq!(error, Error::Connection);
        tokio::time::timeout(Duration::from_secs(10), peer)
            .await
            .unwrap()
            .unwrap();
    }
}

#[tokio::test]
async fn webpki_hostname_policy_names_legacy_refusals_without_changing_verify_ca() {
    for (certificate, host) in [("legacy-cn", "localhost"), ("legacy-ip-dns", "127.0.0.1")] {
        let (port, task) = peer(true, certificate).await;
        let root = cert_path("ca.pem").to_string_lossy().replace('\\', "\\\\");
        let dsn = Dsn::parse(&format!("host={host} port={port} user=fixture dbname=fixture sslmode=verify-full sslrootcert='{root}'")).unwrap();
        let error = match Session::open_in(&dsn, &TlsEnvironment::default()).await {
            Ok(_) => panic!("legacy hostname admitted"),
            Err(error) => error,
        };
        assert_eq!(error, Error::HostnameRules);
        assert_eq!(error.exit_code(), 1);
        assert_eq!(
            error.to_string(),
            "PostgreSQL pg-tls-webpki-hostnames refused: requires matching SAN dNSName/iPAddress; libpq accepts matching legacy CN-only/IP-in-dNSName certificates"
        );
        let receipt = tokio::time::timeout(Duration::from_secs(10), task)
            .await
            .unwrap()
            .unwrap();
        assert!(!receipt.startup && !receipt.setup && !receipt.terminated);
        cell(
            "verify-ca",
            true,
            host,
            Some("ca.pem"),
            None,
            certificate,
            true,
        )
        .await;
    }
}
