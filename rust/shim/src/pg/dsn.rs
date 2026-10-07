use super::Error;
use std::{collections::BTreeMap, fmt, path::PathBuf, time::Duration};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SslMode {
    Disable,
    Prefer,
    Require,
    VerifyCa,
    VerifyFull,
}
impl SslMode {
    fn parse(value: &str) -> Result<Self, Error> {
        match value {
            "disable" => Ok(Self::Disable),
            "prefer" => Ok(Self::Prefer),
            "require" => Ok(Self::Require),
            "verify-ca" => Ok(Self::VerifyCa),
            "verify-full" => Ok(Self::VerifyFull),
            _ => Err(Error::UnsupportedOption("sslmode".into())),
        }
    }
}
pub struct Dsn {
    config: tokio_postgres::Config,
    pub(super) mode: SslMode,
    pub(super) rootcert: Option<PathBuf>,
}
impl fmt::Debug for Dsn {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Dsn")
            .field("sslmode", &self.mode)
            .field("explicit_rootcert", &self.rootcert.is_some())
            .finish_non_exhaustive()
    }
}
fn decode(text: &str) -> Result<String, Error> {
    if text.as_bytes().iter().enumerate().any(|(i, b)| {
        *b == b'%'
            && (text
                .as_bytes()
                .get(i + 1)
                .is_none_or(|b| !b.is_ascii_hexdigit())
                || text
                    .as_bytes()
                    .get(i + 2)
                    .is_none_or(|b| !b.is_ascii_hexdigit()))
    }) {
        return Err(Error::InvalidDsn);
    }
    percent_encoding::percent_decode_str(text)
        .decode_utf8()
        .map(|s| s.into_owned())
        .map_err(|_| Error::InvalidDsn)
}
fn option(key: &str) -> Result<(), Error> {
    if key.is_empty() || !key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_') {
        return Err(Error::InvalidDsn);
    }
    match key {
        "host" | "port" | "user" | "password" | "dbname" | "sslmode" | "sslrootcert" => Ok(()),
        _ => Err(Error::UnsupportedOption(key.into())),
    }
}
// libpq keyword quoting/backslash rules; URI authority parsing remains in the driver.
fn keywords(text: &str) -> Result<BTreeMap<String, String>, Error> {
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
        option(&key)?;
        while input.peek().is_some_and(|c| c.is_ascii_whitespace()) {
            input.next();
        }
        if input.next() != Some('=') {
            return Err(Error::InvalidDsn);
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
                value.push(input.next().ok_or(Error::InvalidDsn)?);
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
            return Err(Error::InvalidDsn);
        }
        fields.insert(key, value);
    }
}
impl Dsn {
    pub fn parse(text: &str) -> Result<Self, Error> {
        if text.contains('\0') {
            return Err(Error::InvalidDsn);
        }
        let (base, mut fields) =
            if text.starts_with("postgresql://") || text.starts_with("postgres://") {
                if text.contains('#') {
                    return Err(Error::InvalidDsn);
                }
                let (base, query) = text.split_once('?').unwrap_or((text, ""));
                let mut fields = BTreeMap::new();
                for item in query.split('&').filter(|item| !item.is_empty()) {
                    let (key, value) = item.split_once('=').ok_or(Error::InvalidDsn)?;
                    let key = decode(key)?;
                    option(&key)?;
                    fields.insert(key, decode(value)?);
                }
                (Some(base), fields)
            } else {
                (None, keywords(text)?)
            };
        let mode = fields
            .remove("sslmode")
            .map_or(Ok(SslMode::Prefer), |s| SslMode::parse(&s))?;
        let rootcert = fields
            .remove("sslrootcert")
            .filter(|s| !s.is_empty())
            .map(PathBuf::from);
        let origin = base
            .map_or(Ok(tokio_postgres::Config::new()), |s| s.parse())
            .map_err(|_| Error::InvalidDsn)?;
        let mut config = tokio_postgres::Config::new();
        if let Some(value) = origin.get_user() {
            config.user(value);
        }
        if let Some(value) = origin.get_password() {
            config.password(value);
        }
        if let Some(value) = origin.get_dbname() {
            config.dbname(value);
        }
        // Last configured value wins without host-list append or URL '+' rewriting.
        let host = fields
            .remove("host")
            .or_else(|| match origin.get_hosts() {
                [tokio_postgres::config::Host::Tcp(host)] => Some(host.clone()),
                _ => None,
            })
            .ok_or(Error::InvalidOption("host"))?;
        // The installed/documented producer domain has one TCP host.
        if host.is_empty() || host.contains(',') || host.starts_with('/') {
            return Err(Error::InvalidOption("host"));
        }
        config.host(host);
        let port = fields
            .remove("port")
            .map(|value| {
                value
                    .parse::<u16>()
                    .map_err(|_| Error::InvalidOption("port"))
            })
            .transpose()?
            .or_else(|| origin.get_ports().first().copied())
            .unwrap_or(5432);
        if port == 0 {
            return Err(Error::InvalidOption("port"));
        }
        config.port(port);
        for (key, value) in fields {
            match key.as_str() {
                "user" => {
                    config.user(value);
                }
                "password" => {
                    config.password(value);
                }
                "dbname" => {
                    config.dbname(value);
                }
                _ => unreachable!("admitted DSN option"),
            }
        }
        if config.get_hosts().len() != 1 {
            return Err(Error::InvalidOption("host"));
        }
        config.ssl_mode(match mode {
            SslMode::Disable => tokio_postgres::config::SslMode::Disable,
            SslMode::Prefer => tokio_postgres::config::SslMode::Prefer,
            _ => tokio_postgres::config::SslMode::Require,
        });
        // The connector owns PostgreSQL SSLRequest admission; the driver must not
        // interpret an invalid server reply as permission for plaintext.
        if mode != SslMode::Disable {
            config.ssl_mode(tokio_postgres::config::SslMode::Require);
            config.ssl_negotiation(tokio_postgres::config::SslNegotiation::Direct);
        }
        config.connect_timeout(Duration::from_secs(10));
        Ok(Self {
            config,
            mode,
            rootcert,
        })
    }
    pub fn config(&self) -> &tokio_postgres::Config {
        &self.config
    }
    pub fn ssl_mode(&self) -> SslMode {
        self.mode
    }
}
