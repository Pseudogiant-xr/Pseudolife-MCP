use super::{Dsn, Error, PasswordEnvironment, SslMode};
#[path = "../../../shared/pg_tls.rs"]
mod crypto;
use crypto::Verification;
use std::path::PathBuf;

/// Unsupported controls retain only their names; passwords have redacted Debug.
#[derive(Debug, Clone, Default)]
pub struct TlsEnvironment {
    pub default_directory: Option<PathBuf>,
    pub ambient_controls: Vec<String>,
    pub password: PasswordEnvironment,
}
impl TlsEnvironment {
    pub fn from_environment() -> Self {
        #[cfg(windows)]
        let default_directory =
            std::env::var_os("APPDATA").map(|path| PathBuf::from(path).join("postgresql"));
        #[cfg(not(windows))]
        let default_directory = dirs::home_dir().map(|path| path.join(".postgresql"));
        let mut ambient_controls: Vec<_> = std::env::vars_os()
            .filter_map(|(key, _)| {
                let key = key.to_string_lossy().into_owned();
                let folded = if cfg!(windows) {
                    key.to_uppercase()
                } else {
                    key.clone()
                };
                (folded.starts_with("PGSSL")
                    || folded.starts_with("PGTLS")
                    || matches!(
                        folded.as_str(),
                        "PGHOST"
                            | "PGHOSTADDR"
                            | "PGPORT"
                            | "PGDATABASE"
                            | "PGUSER"
                            | "PGPASSFILE"
                            | "PGSERVICE"
                            | "PGSERVICEFILE"
                            | "PGSYSCONFDIR"
                            | "PGOPTIONS"
                            | "PGAPPNAME"
                            | "PGCONNECT_TIMEOUT"
                            | "PGCLIENTENCODING"
                            | "PGCHANNELBINDING"
                            | "PGTARGETSESSIONATTRS"
                            | "PGLOADBALANCEHOSTS"
                            | "PGREQUIREAUTH"
                            | "PGGSSENCMODE"
                            | "PGKRBSRVNAME"
                            | "PGGSSLIB"
                            | "PGGSSDELEGATION"
                            | "PGREQUIREPEER"
                            | "PGMINPROTOCOLVERSION"
                            | "PGMAXPROTOCOLVERSION"
                            | "PGREQUIRESSL"
                            | "PGREALM"
                            | "PGDATESTYLE"
                            | "PGTZ"
                            | "PGGEQO"
                    ))
                .then_some(key)
            })
            .collect();
        ambient_controls.sort();
        Self {
            default_directory,
            ambient_controls,
            password: PasswordEnvironment::from_environment(),
        }
    }
    pub fn validate(&self) -> Result<(), Error> {
        if let Some(control) = self.ambient_controls.first() {
            return Err(Error::AmbientControl(control.clone()));
        }
        if let Some(directory) = &self.default_directory {
            for name in ["postgresql.crt", "postgresql.key", "root.crl"] {
                match std::fs::symlink_metadata(directory.join(name)) {
                    Ok(_) => return Err(Error::AmbientControl(name.into())),
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
                    Err(_) => return Err(Error::AmbientControl(name.into())),
                }
            }
        }
        Ok(())
    }
}

pub(super) fn connector(
    dsn: &Dsn,
    environment: &TlsEnvironment,
) -> Result<tokio_postgres_rustls::MakeRustlsConnect, Error> {
    environment.validate()?;
    let verification = if dsn.mode == SslMode::Disable {
        // The driver sends no SSLRequest in disable mode; never a default path.
        Verification::Hostname(rustls::RootCertStore::empty())
    } else {
        let explicit = dsn.rootcert.as_ref();
        let default = environment
            .default_directory
            .as_ref()
            .map(|directory| directory.join("root.crt"));
        let default_exists = match default
            .as_ref()
            .filter(|_| explicit.is_none())
            .map(std::fs::symlink_metadata)
        {
            Some(Ok(_)) => true,
            None => false,
            Some(Err(error)) if error.kind() == std::io::ErrorKind::NotFound => false,
            Some(Err(_)) => return Err(Error::RootCertificate),
        };
        let path = explicit.or_else(|| default.as_ref().filter(|_| default_exists));
        let roots = path
            .map(|path| crypto::roots(path).map_err(|_| Error::RootCertificate))
            .transpose()?;
        if matches!(dsn.mode, SslMode::VerifyCa | SslMode::VerifyFull) && roots.is_none() {
            return Err(Error::RootCertificate);
        }
        if dsn.mode == SslMode::VerifyFull {
            Verification::Hostname(roots.expect("required CA roots"))
        } else {
            match roots {
                Some(roots) => Verification::CertificateAuthority(roots),
                None => Verification::EncryptionOnly,
            }
        }
    };
    crypto::connector(verification).map_err(|_| Error::InvalidOption("sslmode"))
}
