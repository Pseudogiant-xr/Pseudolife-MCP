//! Dedicated PostgreSQL connections shared by native SQL clients and operators.
mod dsn;
mod password;
pub mod resolution;
mod tls;
mod transport;

pub use dsn::{Dsn, SslMode};
pub use password::PasswordEnvironment;
use std::{fmt, time::Duration};
pub use tls::TlsEnvironment;
use tokio::task::JoinHandle;
pub use tokio_postgres::{Client, Row, Transaction, types};

/// Diagnostics deliberately contain no DSN, credential, path or driver message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    UnsupportedOption(String),
    AmbientControl(String),
    InvalidOption(&'static str),
    InvalidDsn,
    RootCertificate,
    Connection,
    HostnameRules,
    SessionSetup,
    Shutdown,
}
impl Error {
    pub fn exit_code(&self) -> i32 {
        1
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedOption(option) => {
                write!(f, "unsupported PostgreSQL DSN option: {option}")
            }
            Self::AmbientControl(control) => {
                write!(f, "unsupported PostgreSQL ambient control: {control}")
            }
            Self::InvalidOption(option) => write!(f, "invalid PostgreSQL DSN option: {option}"),
            Self::InvalidDsn => f.write_str("PostgreSQL DSN is not understood"),
            Self::RootCertificate => {
                f.write_str("PostgreSQL sslrootcert is not understood or unavailable")
            }
            Self::Connection => f.write_str("PostgreSQL connection failed"),
            Self::HostnameRules => f.write_str("PostgreSQL pg-tls-webpki-hostnames refused: requires matching SAN dNSName/iPAddress; libpq accepts matching legacy CN-only/IP-in-dNSName certificates"),
            Self::SessionSetup => f.write_str("PostgreSQL session setup failed"),
            Self::Shutdown => f.write_str("PostgreSQL connection shutdown failed"),
        }
    }
}
impl std::error::Error for Error {}

fn connection_error(error: tokio_postgres::Error) -> Error {
    let mut cause: Option<&(dyn std::error::Error + 'static)> = Some(&error);
    while let Some(current) = cause {
        let tls = current.downcast_ref::<rustls::Error>().or_else(|| {
            current
                .downcast_ref::<std::io::Error>()
                .and_then(std::io::Error::get_ref)
                .and_then(|inner| inner.downcast_ref::<rustls::Error>())
        });
        if matches!(
            tls,
            Some(rustls::Error::InvalidCertificate(
                rustls::CertificateError::NotValidForName
                    | rustls::CertificateError::NotValidForNameContext { .. }
            ))
        ) {
            return Error::HostnameRules;
        }
        cause = current.source();
    }
    Error::Connection
}

/// One dedicated connection. No pooling, schema changes or post-startup reconnect.
/// A borrowed transaction pins this client; its caller must observe commit.
pub struct Session {
    client: Option<Client>,
    driver: JoinHandle<Result<(), tokio_postgres::Error>>,
    notices: Notices,
    ended: Ended,
}
/// The SQLSTATE of a server error that ended the connection with no request
/// pending (a FATAL such as `admin_shutdown`), kept for the caller.
type Ended = std::sync::Arc<std::sync::Mutex<Option<tokio_postgres::error::SqlState>>>;
type Notices =
    std::sync::Arc<std::sync::Mutex<std::collections::VecDeque<tokio_postgres::error::DbError>>>;
/// Notices kept until a caller takes them; beyond this the oldest are
/// dropped, so a long-lived session that never takes them stays bounded.
pub const NOTICE_LIMIT: usize = 4096;
impl Session {
    pub async fn open(dsn: &Dsn) -> Result<Self, Error> {
        Self::open_in(dsn, &TlsEnvironment::from_environment()).await
    }
    /// Explicit environment binding also allows private fixture roots without
    /// changing process-global environment or touching the operator's files.
    pub async fn open_in(dsn: &Dsn, environment: &TlsEnvironment) -> Result<Self, Error> {
        let tls = transport::Negotiator::new(tls::connector(dsn, environment)?, dsn.mode);
        let config = password::configure(dsn, &environment.password)?;
        // libpq18 prefer tries plaintext once after a failed TLS attempt. Keep
        // both attempts inside the existing connect deadline; never retry a
        // configuration refusal, invalid SSLRequest reply or session setup.
        let connect = async {
            match config.connect(tls.clone()).await {
                Err(error)
                    if dsn.mode == SslMode::Prefer
                        && (transport::handshake_failed(&error)
                            || (tls.established()
                                && error.as_db_error().is_some_and(|error| {
                                    error.code()
                                        != &tokio_postgres::error::SqlState::CANNOT_CONNECT_NOW
                                }))) =>
                {
                    let mut plaintext = config.clone();
                    plaintext.ssl_mode(tokio_postgres::config::SslMode::Disable);
                    plaintext.ssl_negotiation(tokio_postgres::config::SslNegotiation::Postgres);
                    plaintext.connect(tls).await
                }
                result => result,
            }
        };
        let (client, connection) = tokio::time::timeout(Duration::from_secs(10), connect)
            .await
            .map_err(|_| Error::Connection)?
            .map_err(connection_error)?;
        // The driver is the Connection future's own poll loop, except that
        // server notices are kept for the caller instead of being logged.
        let notices = Notices::default();
        let sink = std::sync::Arc::clone(&notices);
        let ended = Ended::default();
        let fatal = std::sync::Arc::clone(&ended);
        let mut connection = connection;
        let driver = tokio::spawn(async move {
            while let Some(message) =
                std::future::poll_fn(|context| connection.poll_message(context)).await
            {
                let message = message.inspect_err(|error| {
                    if let Some(error) = error.as_db_error() {
                        *fatal
                            .lock()
                            .unwrap_or_else(std::sync::PoisonError::into_inner) =
                            Some(error.code().clone());
                    }
                })?;
                if let tokio_postgres::AsyncMessage::Notice(notice) = message {
                    let mut kept = sink
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner);
                    if kept.len() == NOTICE_LIMIT {
                        kept.pop_front();
                    }
                    kept.push_back(notice);
                }
            }
            Ok(())
        });
        let session = Self {
            client: Some(client),
            driver,
            notices,
            ended,
        };
        session
            .client()
            .batch_execute("SET lock_timeout = '5s'; SET search_path TO public")
            .await
            .map_err(|_| Error::SessionSetup)?;
        Ok(session)
    }
    pub fn client(&self) -> &Client {
        self.client.as_ref().expect("open PostgreSQL session")
    }
    pub fn client_mut(&mut self) -> &mut Client {
        self.client.as_mut().expect("open PostgreSQL session")
    }
    /// Server notices received so far (in order), removed from the session.
    /// At most the newest [`NOTICE_LIMIT`] are kept between takes.
    pub fn take_notices(&self) -> Vec<tokio_postgres::error::DbError> {
        std::mem::take(
            &mut *self
                .notices
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner),
        )
        .into()
    }
    /// The SQLSTATE of the server error that ended this connection while no
    /// request was pending, once the connection has ended that way.
    pub fn ended_with(&self) -> Option<tokio_postgres::error::SqlState> {
        self.ended
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
    /// Close gracefully after all borrowed operations have ended.
    pub async fn close(mut self) -> Result<(), Error> {
        drop(self.client.take());
        (&mut self.driver)
            .await
            .map_err(|_| Error::Shutdown)?
            .map_err(|_| Error::Shutdown)
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        // Cancellation/error closes exactly this connection; never a bank/server.
        self.driver.abort();
    }
}
