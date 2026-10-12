use super::{Dsn, Error, PasswordEnvironment, SslMode};
use rustls::{
    DigitallySignedStruct, SignatureScheme,
    client::{
        danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
        verify_server_cert_signed_by_trust_anchor,
    },
    crypto::{CryptoProvider, verify_tls12_signature, verify_tls13_signature},
    pki_types::{CertificateDer, ServerName, UnixTime},
    server::ParsedCertificate,
};
use std::{path::PathBuf, sync::Arc};

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

/// `verify-ca` checks the server chain, validity and server-auth usage, but
/// deliberately omits only the hostname check. TLS handshake signatures are
/// verified in every mode, including libpq's encryption-only prefer/require.
#[derive(Debug)]
struct Verifier {
    roots: Option<rustls::RootCertStore>,
    provider: Arc<CryptoProvider>,
}
impl ServerCertVerifier for Verifier {
    fn verify_server_cert(
        &self,
        leaf: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        _name: &ServerName<'_>,
        _ocsp: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        if let Some(roots) = &self.roots {
            let leaf = ParsedCertificate::try_from(leaf)?;
            verify_server_cert_signed_by_trust_anchor(
                &leaf,
                roots,
                intermediates,
                now,
                self.provider.signature_verification_algorithms.all,
            )?;
        }
        Ok(ServerCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(
            message,
            cert,
            signature,
            &self.provider.signature_verification_algorithms,
        )
    }
    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(
            message,
            cert,
            signature,
            &self.provider.signature_verification_algorithms,
        )
    }
    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider
            .signature_verification_algorithms
            .supported_schemes()
    }
}
fn roots(path: &std::path::Path) -> Result<rustls::RootCertStore, Error> {
    let bytes = std::fs::read(path).map_err(|_| Error::RootCertificate)?;
    let mut roots = rustls::RootCertStore::empty();
    let certs = rustls_pemfile::certs(&mut bytes.as_slice())
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| Error::RootCertificate)?;
    if certs.is_empty() {
        return Err(Error::RootCertificate);
    }
    for cert in certs {
        roots.add(cert).map_err(|_| Error::RootCertificate)?;
    }
    Ok(roots)
}
pub(super) fn connector(
    dsn: &Dsn,
    environment: &TlsEnvironment,
) -> Result<tokio_postgres_rustls::MakeRustlsConnect, Error> {
    environment.validate()?;
    let provider = Arc::new(rustls::crypto::aws_lc_rs::default_provider());
    let builder = rustls::ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .map_err(|_| Error::InvalidOption("sslmode"))?;
    let config = if dsn.mode == SslMode::Disable {
        // The driver sends no SSLRequest in disable mode; never a default path.
        builder
            .with_root_certificates(rustls::RootCertStore::empty())
            .with_no_client_auth()
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
        let roots = path.map(|path| roots(path)).transpose()?;
        if matches!(dsn.mode, SslMode::VerifyCa | SslMode::VerifyFull) && roots.is_none() {
            return Err(Error::RootCertificate);
        }
        if dsn.mode == SslMode::VerifyFull {
            builder
                .with_root_certificates(roots.expect("required CA roots"))
                .with_no_client_auth()
        } else {
            builder
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(Verifier { roots, provider }))
                .with_no_client_auth()
        }
    };
    Ok(tokio_postgres_rustls::MakeRustlsConnect::new(config))
}
