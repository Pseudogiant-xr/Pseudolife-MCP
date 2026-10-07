//! Admit only PostgreSQL's S/N SSLRequest responses before startup/authentication.
use super::SslMode;
use std::{
    convert::Infallible,
    future::Future,
    io,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    task::{Context, Poll},
};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf};
use tokio_postgres::{
    Socket,
    tls::{ChannelBinding, MakeTlsConnect, TlsConnect, TlsStream},
};
use tokio_postgres_rustls::MakeRustlsConnect;

type Secure = <MakeRustlsConnect as MakeTlsConnect<Socket>>::Stream;
type SecureConnect = <MakeRustlsConnect as MakeTlsConnect<Socket>>::TlsConnect;
#[derive(Clone)]
pub(super) struct Negotiator {
    tls: MakeRustlsConnect,
    mode: SslMode,
    established: Arc<AtomicBool>,
}
impl Negotiator {
    pub(super) fn new(tls: MakeRustlsConnect, mode: SslMode) -> Self {
        Self {
            tls,
            mode,
            established: Arc::new(AtomicBool::new(false)),
        }
    }
    pub(super) fn established(&self) -> bool {
        self.established.load(Ordering::Acquire)
    }
}

#[derive(Debug)]
struct TlsAttemptFailed(io::Error);
impl std::fmt::Display for TlsAttemptFailed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("PostgreSQL TLS attempt failed")
    }
}
impl std::error::Error for TlsAttemptFailed {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.0)
    }
}
pub(super) fn handshake_failed(error: &tokio_postgres::Error) -> bool {
    let mut cause: Option<&(dyn std::error::Error + 'static)> = Some(error);
    while let Some(current) = cause {
        if current.is::<TlsAttemptFailed>()
            || current
                .downcast_ref::<io::Error>()
                .and_then(io::Error::get_ref)
                .is_some_and(|inner| inner.is::<TlsAttemptFailed>())
        {
            return true;
        }
        cause = current.source();
    }
    false
}

pub(super) struct Connect {
    tls: SecureConnect,
    mode: SslMode,
    established: Arc<AtomicBool>,
}
pub(super) enum Stream {
    Plain(Socket),
    Secure(Box<Secure>),
}
impl MakeTlsConnect<Socket> for Negotiator {
    type Stream = Stream;
    type TlsConnect = Connect;
    type Error = Infallible;
    fn make_tls_connect(&mut self, hostname: &str) -> Result<Connect, Infallible> {
        self.established.store(false, Ordering::Release);
        Ok(Connect {
            tls: <MakeRustlsConnect as MakeTlsConnect<Socket>>::make_tls_connect(
                &mut self.tls,
                hostname,
            )?,
            mode: self.mode,
            established: self.established.clone(),
        })
    }
}
impl TlsConnect<Socket> for Connect {
    type Stream = Stream;
    type Error = io::Error;
    type Future = Pin<Box<dyn Future<Output = io::Result<Stream>> + Send>>;
    fn connect(self, mut socket: Socket) -> Self::Future {
        Box::pin(async move {
            socket.write_all(&[0, 0, 0, 8, 4, 210, 22, 47]).await?;
            match socket.read_u8().await? {
                b'S' => {
                    let stream = self
                        .tls
                        .connect(socket)
                        .await
                        .map_err(|error| io::Error::other(TlsAttemptFailed(error)))?;
                    self.established.store(true, Ordering::Release);
                    Ok(Stream::Secure(Box::new(stream)))
                }
                b'N' if self.mode == SslMode::Prefer => Ok(Stream::Plain(socket)),
                b'N' => Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "server does not support TLS",
                )),
                _ => Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "invalid PostgreSQL SSL response",
                )),
            }
        })
    }
}
impl TlsStream for Stream {
    fn channel_binding(&self) -> ChannelBinding {
        match self {
            Self::Plain(_) => ChannelBinding::none(),
            Self::Secure(stream) => stream.channel_binding(),
        }
    }
}
impl AsyncRead for Stream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        match &mut *self {
            Self::Plain(stream) => Pin::new(stream).poll_read(cx, buf),
            Self::Secure(stream) => Pin::new(stream).poll_read(cx, buf),
        }
    }
}
impl AsyncWrite for Stream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        match &mut *self {
            Self::Plain(stream) => Pin::new(stream).poll_write(cx, buf),
            Self::Secure(stream) => Pin::new(stream).poll_write(cx, buf),
        }
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match &mut *self {
            Self::Plain(stream) => Pin::new(stream).poll_flush(cx),
            Self::Secure(stream) => Pin::new(stream).poll_flush(cx),
        }
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match &mut *self {
            Self::Plain(stream) => Pin::new(stream).poll_shutdown(cx),
            Self::Secure(stream) => Pin::new(stream).poll_shutdown(cx),
        }
    }
}
