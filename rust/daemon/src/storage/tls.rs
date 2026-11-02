//! libpq TLS options around tokio-postgres's connection-string parser.
//! Non-TLS keywords retain the driver's syntax and support boundaries.

#[path = "../../../shared/pg_tls.rs"]
mod crypto;

use super::tls_transport as transport;
use std::{
    collections::BTreeMap,
    fmt,
    ops::{Deref, DerefMut},
    path::{Path, PathBuf},
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    task::JoinHandle,
};
use tokio_postgres::{
    Client, NoTls,
    config::{SslMode as DriverMode, SslNegotiation},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum SslMode {
    Disable,
    Allow,
    Prefer,
    Require,
    VerifyCa,
    VerifyFull,
}

#[derive(Clone)]
pub struct Config {
    driver: tokio_postgres::Config,
    mode: SslMode,
    root: Option<PathBuf>,
    system: bool,
    cert: Option<PathBuf>,
    key: Option<PathBuf>,
    directory: Option<PathBuf>,
}
impl fmt::Debug for Config {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Config")
            .field("sslmode", &self.mode)
            .finish_non_exhaustive()
    }
}
impl Deref for Config {
    type Target = tokio_postgres::Config;
    fn deref(&self) -> &Self::Target {
        &self.driver
    }
}
impl DerefMut for Config {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.driver
    }
}

const INVALID: &str =
    "invalid connection string; check PostgreSQL DSN keyword quoting and URI escaping";
fn unsupported(name: &str) -> String {
    format!(
        "PostgreSQL option {name} is unsupported by the native daemon; use the Python engine for this libpq setting"
    )
}
fn decode(value: &str) -> Result<String, String> {
    if value.as_bytes().iter().enumerate().any(|(i, b)| {
        *b == b'%'
            && (value
                .as_bytes()
                .get(i + 1)
                .is_none_or(|v| !v.is_ascii_hexdigit())
                || value
                    .as_bytes()
                    .get(i + 2)
                    .is_none_or(|v| !v.is_ascii_hexdigit()))
    }) {
        return Err(INVALID.into());
    }
    percent_encoding::percent_decode_str(value)
        .decode_utf8()
        .map(|v| v.into_owned())
        .map_err(|_| INVALID.into())
}
fn valid_key(key: &str) -> bool {
    !key.is_empty() && key.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
}
fn keywords(text: &str) -> Result<BTreeMap<String, String>, String> {
    let mut input = text.chars().peekable();
    let mut fields = BTreeMap::new();
    loop {
        while input.peek().is_some_and(|c| c.is_ascii_whitespace()) {
            input.next();
        }
        if input.peek().is_none() {
            return Ok(fields);
        }
        let mut key = String::new();
        while input
            .peek()
            .is_some_and(|c| *c != '=' && !c.is_ascii_whitespace())
        {
            key.push(input.next().unwrap());
        }
        if !valid_key(&key) {
            return Err(INVALID.into());
        }
        while input.peek().is_some_and(|c| c.is_ascii_whitespace()) {
            input.next();
        }
        if input.next() != Some('=') {
            return Err(INVALID.into());
        }
        while input.peek().is_some_and(|c| c.is_ascii_whitespace()) {
            input.next();
        }
        let quoted = input.peek() == Some(&'\'');
        if quoted {
            input.next();
        }
        let mut value = String::new();
        let mut closed = !quoted;
        while let Some(c) = input.next() {
            if c == '\\' {
                value.push(input.next().ok_or(INVALID)?);
            } else if quoted && c == '\'' {
                closed = true;
                break;
            } else if !quoted && c.is_ascii_whitespace() {
                break;
            } else {
                value.push(c);
            }
        }
        if !closed || (quoted && input.peek().is_some_and(|c| !c.is_ascii_whitespace())) {
            return Err(INVALID.into());
        }
        fields.insert(key, value);
    }
}

#[derive(Default)]
pub(super) struct Environment {
    values: BTreeMap<String, String>,
    directory: Option<PathBuf>,
}
impl Environment {
    fn current() -> Self {
        #[cfg(windows)]
        let directory = std::env::var_os("APPDATA").map(|p| PathBuf::from(p).join("postgresql"));
        #[cfg(not(windows))]
        let directory = dirs::home_dir().map(|p| p.join(".postgresql"));
        let values = std::env::vars_os()
            .filter_map(|(key, value)| {
                let key = key.to_string_lossy().into_owned();
                let key = if cfg!(windows) {
                    key.to_uppercase()
                } else {
                    key
                };
                (key.starts_with("PGSSL")
                    || key.starts_with("PGTLS")
                    || matches!(
                        key.as_str(),
                        "PGGSSENCMODE"
                            | "PGKRBSRVNAME"
                            | "PGGSSLIB"
                            | "PGGSSDELEGATION"
                            | "PGREQUIRESSL"
                    ))
                .then(|| (key, value.to_string_lossy().into_owned()))
            })
            .collect();
        Self { values, directory }
    }
}

pub fn parse(dsn: &str) -> Result<Config, String> {
    parse_in(dsn, Environment::current())
}
pub(super) fn parse_in(dsn: &str, environment: Environment) -> Result<Config, String> {
    if dsn.contains('\0') {
        return Err(INVALID.into());
    }
    let uri = dsn.starts_with("postgresql://") || dsn.starts_with("postgres://");
    let (base, mut fields) = if uri {
        if dsn.contains('#') {
            return Err(INVALID.into());
        }
        let (base, query) = dsn.split_once('?').unwrap_or((dsn, ""));
        let mut fields = BTreeMap::new();
        for item in query.split('&').filter(|v| !v.is_empty()) {
            let (key, value) = item.split_once('=').ok_or(INVALID)?;
            let key = decode(key)?;
            if !valid_key(&key) {
                return Err(INVALID.into());
            }
            let value = decode(value)?;
            if value.contains('\0') {
                return Err(INVALID.into());
            }
            fields.insert(key, value);
        }
        (base, fields)
    } else {
        ("", keywords(dsn)?)
    };
    // Explicit DSN fields override the corresponding environment field, even
    // when empty. Do not mutate process-global environment during reconnects.
    for (name, value) in environment.values {
        // libpq ignores empty environment defaults; an explicitly empty
        // DSN sslmode still fails validation below.
        if value.is_empty() {
            continue;
        }
        let key = match name.as_str() {
            "PGSSLMODE" => "sslmode",
            "PGSSLROOTCERT" => "sslrootcert",
            "PGSSLCERT" => "sslcert",
            "PGSSLKEY" => "sslkey",
            "PGGSSENCMODE" => "gssencmode",
            _ => return Err(unsupported(&name)),
        };
        fields.entry(key.into()).or_insert(value);
    }
    if fields.remove("gssencmode").is_some_and(|v| v != "disable") {
        return Err(unsupported("gssencmode"));
    }
    let root = fields.remove("sslrootcert").filter(|v| !v.is_empty());
    let system = root.as_deref() == Some("system");
    let mode =
        match fields.remove("sslmode").as_deref() {
            None if system => SslMode::VerifyFull,
            None | Some("prefer") => SslMode::Prefer,
            Some("disable") => SslMode::Disable,
            Some("allow") => SslMode::Allow,
            Some("require") => SslMode::Require,
            Some("verify-ca") => SslMode::VerifyCa,
            Some("verify-full") => SslMode::VerifyFull,
            _ => return Err(
                "invalid sslmode; use disable, allow, prefer, require, verify-ca or verify-full"
                    .into(),
            ),
        };
    if system && mode != SslMode::VerifyFull {
        return Err("sslrootcert=system requires sslmode=verify-full; remove sslmode or set it to verify-full".into());
    }
    let cert = fields
        .remove("sslcert")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from);
    let key = fields
        .remove("sslkey")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from);
    if fields
        .get("sslnegotiation")
        .is_some_and(|v| v == "postgres")
    {
        fields.remove("sslnegotiation");
    }
    for name in fields.keys() {
        if name.starts_with("ssl")
            || name.starts_with("gss")
            || matches!(name.as_str(), "krbsrvname" | "requiressl")
        {
            return Err(unsupported(name));
        }
    }
    let driver_text = if uri {
        let query = fields
            .iter()
            .map(|(k, v)| {
                format!(
                    "{}={}",
                    percent_encoding::utf8_percent_encode(k, percent_encoding::NON_ALPHANUMERIC),
                    percent_encoding::utf8_percent_encode(v, percent_encoding::NON_ALPHANUMERIC)
                )
            })
            .collect::<Vec<_>>()
            .join("&");
        if query.is_empty() {
            base.to_owned()
        } else {
            format!("{base}?{query}")
        }
    } else {
        fields
            .iter()
            .map(|(k, v)| format!("{k}='{}'", v.replace('\\', "\\\\").replace('\'', "\\'")))
            .collect::<Vec<_>>()
            .join(" ")
    };
    let driver = driver_text.parse().map_err(|_| INVALID.to_owned())?;
    Ok(Config {
        driver,
        mode,
        root: root.filter(|_| !system).map(PathBuf::from),
        system,
        cert,
        key,
        directory: environment.directory,
    })
}

fn exists(path: &Path, option: &str) -> Result<bool, String> {
    match std::fs::metadata(path) {
        Ok(_) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(format!(
            "cannot read {option}; fix permissions or use the Python engine"
        )),
    }
}
fn file_error(option: &str) -> String {
    format!("cannot load {option}; configure a readable PEM file or use the Python engine")
}
impl Config {
    fn path(&self, explicit: &Option<PathBuf>, default: &str) -> Option<PathBuf> {
        explicit
            .clone()
            .or_else(|| self.directory.as_ref().map(|p| p.join(default)))
    }
    fn supported_files(&self) -> Result<(), String> {
        if let Some(directory) = &self.directory
            && exists(&directory.join("root.crl"), "root.crl")?
        {
            return Err(unsupported("root.crl"));
        }
        if let Some(cert) = self.path(&self.cert, "postgresql.crt")
            && exists(&cert, "sslcert")?
            && let Some(key) = self.path(&self.key, "postgresql.key")
            && let Ok(bytes) = std::fs::read(key)
            && bytes.windows(9).any(|v| v == b"ENCRYPTED")
        {
            return Err("encrypted sslkey is unsupported; use the Python engine or an appropriately protected unencrypted PEM key".into());
        }
        Ok(())
    }
    pub(super) fn connector(&self) -> Result<tokio_postgres_rustls::MakeRustlsConnect, String> {
        let roots = if self.system {
            let loaded = rustls_native_certs::load_native_certs();
            let mut roots = rustls::RootCertStore::empty();
            for cert in loaded.certs {
                roots
                    .add(cert)
                    .map_err(|_| file_error("sslrootcert=system"))?;
            }
            if roots.is_empty() {
                return Err(file_error("sslrootcert=system"));
            }
            Some(roots)
        } else if let Some(path) = self.path(&self.root, "root.crt") {
            if exists(&path, "sslrootcert")? {
                Some(crypto::roots(&path).map_err(|_| file_error("sslrootcert"))?)
            } else {
                None
            }
        } else {
            None
        };
        let verification = match (self.mode, roots) {
            (SslMode::VerifyFull, Some(roots)) => crypto::Verification::Hostname(roots),
            (SslMode::VerifyCa | SslMode::VerifyFull, None) => {
                return Err(file_error("sslrootcert"));
            }
            (_, Some(roots)) => crypto::Verification::CertificateAuthority(roots),
            (_, None) => crypto::Verification::EncryptionOnly,
        };
        crypto::connector_with_identity(verification, self.identity()?)
            .map_err(|_| "invalid sslcert/sslkey pair; configure matching PEM certificate and unencrypted private key".into())
    }
    fn identity(&self) -> Result<Option<crypto::Identity>, String> {
        let Some(cert) = self.path(&self.cert, "postgresql.crt") else {
            return Ok(None);
        };
        if !exists(&cert, "sslcert")? {
            return if self.cert.is_some() {
                Err(file_error("sslcert"))
            } else {
                Ok(None)
            };
        }
        let bytes = std::fs::read(cert).map_err(|_| file_error("sslcert"))?;
        let certificates = rustls_pemfile::certs(&mut bytes.as_slice())
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| file_error("sslcert"))?;
        if certificates.is_empty() {
            return Err(file_error("sslcert"));
        }
        let key = self
            .path(&self.key, "postgresql.key")
            .ok_or_else(|| file_error("sslkey"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let metadata = std::fs::metadata(&key).map_err(|_| file_error("sslkey"))?;
            let forbidden = if metadata.uid() == 0 { 0o037 } else { 0o077 };
            if metadata.mode() & forbidden != 0 {
                return Err(
                    "sslkey permissions are too open; set mode 0600 (or 0640 for a root-owned key)"
                        .into(),
                );
            }
        }
        let bytes = std::fs::read(key).map_err(|_| file_error("sslkey"))?;
        if bytes.windows(9).any(|v| v == b"ENCRYPTED") {
            return Err("encrypted sslkey is unsupported; use the Python engine or an appropriately protected unencrypted PEM key".into());
        }
        let key = rustls_pemfile::private_key(&mut bytes.as_slice())
            .map_err(|_| file_error("sslkey"))?
            .ok_or_else(|| file_error("sslkey"))?;
        Ok(Some((certificates, key)))
    }
}

pub(super) enum ConnectError {
    Config(String),
    Driver(tokio_postgres::Error),
}
impl ConnectError {
    pub(super) fn local_port_error(&self) -> bool {
        matches!(self, Self::Driver(e) if super::local_port_error(e))
    }
    pub(super) fn text(&self) -> String {
        match self {
            Self::Config(e) => e.clone(),
            Self::Driver(e) => super::error_text(e),
        }
    }
}
impl From<tokio_postgres::Error> for ConnectError {
    fn from(e: tokio_postgres::Error) -> Self {
        Self::Driver(e)
    }
}
fn spawn<S, T>(
    client: Client,
    connection: tokio_postgres::Connection<S, T>,
) -> (Client, JoinHandle<()>)
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    T: tokio_postgres::tls::TlsStream + Unpin + Send + 'static,
{
    (
        client,
        tokio::spawn(async move {
            let _ = connection.await;
        }),
    )
}
async fn plain(
    config: &tokio_postgres::Config,
) -> Result<(Client, JoinHandle<()>), tokio_postgres::Error> {
    let mut config = config.clone();
    config
        .ssl_mode(DriverMode::Disable)
        .ssl_negotiation(SslNegotiation::Postgres);
    let (client, connection) = config.connect(NoTls).await?;
    Ok(spawn(client, connection))
}
pub(super) async fn connect(config: &Config) -> Result<(Client, JoinHandle<()>), ConnectError> {
    if config.mode == SslMode::Disable {
        return plain(&config.driver).await.map_err(Into::into);
    }
    if config.mode == SslMode::Allow {
        match plain(&config.driver).await {
            Ok(result) => return Ok(result),
            // Match libpq's alternate-transport attempt after authentication
            // refusal, but never reinterpret a busy/recovering server as TLS.
            Err(e)
                if e.as_db_error().is_some_and(|e| {
                    e.code() != &tokio_postgres::error::SqlState::CANNOT_CONNECT_NOW
                }) => {}
            Err(e) => return Err(e.into()),
        }
    }
    config.supported_files().map_err(ConnectError::Config)?;
    let tls = transport::Negotiator::new(config.clone(), config.mode);
    let mut driver = config.driver.clone();
    driver
        .ssl_mode(DriverMode::Require)
        .ssl_negotiation(SslNegotiation::Direct);
    match driver.connect(tls.clone()).await {
        Ok((client,connection)) => Ok(spawn(client,connection)),
        Err(e) if config.mode == SslMode::Prefer && (transport::handshake_failed(&e) ||
            (tls.established() && e.as_db_error().is_some_and(|e| e.code() != &tokio_postgres::error::SqlState::CANNOT_CONNECT_NOW))) =>
            plain(&config.driver).await.map_err(Into::into),
        Err(e) if transport::configuration_error(&e).is_some() =>
            Err(ConnectError::Config(transport::configuration_error(&e).unwrap())),
        Err(e) if config.mode == SslMode::VerifyFull && transport::handshake_failed(&e) =>
            Err(ConnectError::Config("sslmode=verify-full TLS handshake failed; check sslrootcert and a certificate with a matching DNS/IP subject alternative name (legacy CN-only and IP-in-dNSName certificates require the Python engine)".into())),
        Err(e) => Err(e.into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Directory(PathBuf);
    impl Directory {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("plbench-tls-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn path(&self) -> &Path {
            &self.0
        }
    }
    impl Drop for Directory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    fn parse(text: &str) -> Config {
        parse_in(text, Environment::default()).unwrap()
    }
    #[test]
    fn empty_sslmode_is_invalid_like_libpq() {
        for dsn in ["sslmode=''", "postgresql://localhost/pl_cf_tls?sslmode="] {
            assert!(parse_in(dsn, Environment::default()).is_err());
        }
        assert_eq!(
            parse_in(
                "",
                Environment {
                    values: BTreeMap::from([("PGSSLMODE".into(), "".into())]),
                    ..Default::default()
                }
            )
            .unwrap()
            .mode,
            SslMode::Prefer
        );
    }
    #[test]
    fn libpq_modes_and_environment_precedence() {
        assert_eq!(parse("host=localhost").mode, SslMode::Prefer);
        for (text, mode) in [
            ("disable", SslMode::Disable),
            ("allow", SslMode::Allow),
            ("prefer", SslMode::Prefer),
            ("require", SslMode::Require),
            ("verify-ca", SslMode::VerifyCa),
            ("verify-full", SslMode::VerifyFull),
        ] {
            assert_eq!(
                parse(&format!("host=localhost sslmode='{text}'")).mode,
                mode
            );
        }
        let env = Environment {
            values: BTreeMap::from([
                ("PGSSLMODE".into(), "verify-full".into()),
                ("PGSSLROOTCERT".into(), "root.pem".into()),
            ]),
            directory: None,
        };
        let config = parse_in("host=localhost sslmode=disable", env).unwrap();
        assert_eq!(config.mode, SslMode::Disable);
        assert_eq!(config.root, Some(PathBuf::from("root.pem")));
        assert_eq!(parse("sslrootcert=system").mode, SslMode::VerifyFull);
        assert!(
            parse_in("sslrootcert=system sslmode=require", Environment::default())
                .unwrap_err()
                .contains("verify-full")
        );
    }
    #[test]
    fn quoted_and_uri_options_preserve_driver_fields_and_redact_secrets() {
        for text in [
            "host=localhost user=fixture password='redacted+value' application_name='has spaces' sslmode=verify-full sslrootcert='root path.pem'",
            "postgresql://fixture:redacted%2Bvalue@localhost/db?application_name=has%20spaces&sslmode=verify-full&sslrootcert=root%20path.pem",
        ] {
            let config = parse(text);
            assert_eq!(config.get_password(), Some(b"redacted+value".as_slice()));
            assert_eq!(config.get_application_name(), Some("has spaces"));
            assert_eq!(config.root, Some(PathBuf::from("root path.pem")));
            assert!(!format!("{config:?}").contains("redacted"));
        }
        for name in [
            "sslcrl",
            "sslpassword",
            "sslnegotiation",
            "ssl_min_protocol_version",
            "gssencmode",
        ] {
            let error = parse_in(
                &format!("host=localhost {name}=redacted-secret"),
                Environment::default(),
            )
            .unwrap_err();
            assert!(error.contains(name) && error.contains("Python"));
            assert!(!error.contains("redacted-secret"));
        }
        assert!(
            parse_in(
                "postgresql://fixture:redacted-secret@localhost/?sslrootcert=%XX",
                Environment::default()
            )
            .unwrap_err()
            .contains("escaping")
        );
    }
    #[test]
    fn missing_root_is_optional_only_outside_verification_modes() {
        let directory = std::env::temp_dir().join(format!("plbench-tls-{}", uuid::Uuid::new_v4()));
        for mode in [
            "disable",
            "allow",
            "prefer",
            "require",
            "verify-ca",
            "verify-full",
        ] {
            let config = parse_in(
                &format!(
                    "sslmode={mode} sslrootcert='{}'",
                    directory
                        .display()
                        .to_string()
                        .replace('\\', "\\\\")
                        .replace('\'', "\\'")
                ),
                Environment::default(),
            )
            .unwrap();
            assert_eq!(
                config.connector().is_ok(),
                !mode.starts_with("verify"),
                "{mode}"
            );
        }
    }

    #[test]
    fn default_files_and_client_environment_are_resolved_without_global_mutation() {
        let directory = Directory::new();
        let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("../shim/tests/fixtures/pg_tls");
        for (source, target) in [
            ("ca.pem", "root.crt"),
            ("server.pem", "postgresql.crt"),
            ("server-key.pem", "postgresql.key"),
        ] {
            std::fs::copy(fixtures.join(source), directory.path().join(target)).unwrap();
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(
                directory.path().join("postgresql.key"),
                std::fs::Permissions::from_mode(0o600),
            )
            .unwrap();
        }
        let config = parse_in(
            "sslmode=verify-full",
            Environment {
                directory: Some(directory.path().into()),
                ..Default::default()
            },
        )
        .unwrap();
        assert!(config.connector().is_ok());
        assert!(config.identity().unwrap().is_some());
        let config = parse_in(
            "sslcert=explicit.pem",
            Environment {
                values: BTreeMap::from([
                    ("PGSSLCERT".into(), "environment.pem".into()),
                    ("PGSSLKEY".into(), "key.pem".into()),
                ]),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(config.cert, Some(PathBuf::from("explicit.pem")));
        assert_eq!(config.key, Some(PathBuf::from("key.pem")));
    }

    #[test]
    fn unsupported_security_controls_are_named_and_redacted() {
        for name in [
            "PGSSLCRL",
            "PGSSLCRLDIR",
            "PGSSLMINPROTOCOLVERSION",
            "PGREQUIRESSL",
            "PGGSSDELEGATION",
        ] {
            let error = parse_in(
                "",
                Environment {
                    values: BTreeMap::from([(name.into(), "private-value".into())]),
                    ..Default::default()
                },
            )
            .unwrap_err();
            assert!(error.contains(name) && error.contains("Python"));
            assert!(!error.contains("private-value"));
        }
        let directory = Directory::new();
        let config = parse_in(
            "",
            Environment {
                directory: Some(directory.path().into()),
                ..Default::default()
            },
        )
        .unwrap();
        std::fs::write(directory.path().join("root.crl"), "fixture").unwrap();
        assert!(config.supported_files().unwrap_err().contains("root.crl"));
        std::fs::remove_file(directory.path().join("root.crl")).unwrap();
        std::fs::write(directory.path().join("postgresql.crt"), "fixture").unwrap();
        std::fs::write(
            directory.path().join("postgresql.key"),
            "-----BEGIN ENCRYPTED PRIVATE KEY-----",
        )
        .unwrap();
        assert!(
            config
                .supported_files()
                .unwrap_err()
                .contains("encrypted sslkey")
        );
    }
}
