//! PostgreSQL certificate verification shared by the shim and daemon.

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
use std::sync::Arc;

#[derive(Debug)]
pub struct RootCertificateError;

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
pub fn roots(path: &std::path::Path) -> Result<rustls::RootCertStore, RootCertificateError> {
    let bytes = std::fs::read(path).map_err(|_| RootCertificateError)?;
    let mut roots = rustls::RootCertStore::empty();
    let certs = rustls_pemfile::certs(&mut bytes.as_slice())
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| RootCertificateError)?;
    if certs.is_empty() {
        return Err(RootCertificateError);
    }
    for cert in certs {
        roots.add(cert).map_err(|_| RootCertificateError)?;
    }
    Ok(roots)
}
/// Verification policy is selected by the caller's libpq option handling.
pub enum Verification {
    EncryptionOnly,
    CertificateAuthority(rustls::RootCertStore),
    Hostname(rustls::RootCertStore),
}

pub fn connector(
    verification: Verification,
) -> Result<tokio_postgres_rustls::MakeRustlsConnect, rustls::Error> {
    let provider = Arc::new(rustls::crypto::aws_lc_rs::default_provider());
    let builder = rustls::ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()?;
    let config = match verification {
        Verification::Hostname(roots) => {
            builder.with_root_certificates(roots).with_no_client_auth()
        }
        other => {
            let roots = match other {
                Verification::CertificateAuthority(roots) => Some(roots),
                Verification::EncryptionOnly => None,
                Verification::Hostname(_) => unreachable!(),
            };
            builder
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(Verifier { roots, provider }))
                .with_no_client_auth()
        }
    };
    Ok(tokio_postgres_rustls::MakeRustlsConnect::new(config))
}
