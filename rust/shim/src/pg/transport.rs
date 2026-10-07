//! Admit only PostgreSQL's S/N SSLRequest responses before startup/authentication.
use super::SslMode;
use std::{
    convert::Infallible,
    future::Future,
    io,
    pin::Pin,
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
pub(super) struct Negotiator {
    tls: MakeRustlsConnect,
    mode: SslMode,
}
impl Negotiator {
    pub(super) fn new(tls: MakeRustlsConnect, mode: SslMode) -> Self {
        Self { tls, mode }
    }
}
pub(super) struct Connect {
    tls: SecureConnect,
    mode: SslMode,
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
        Ok(Connect {
            tls: <MakeRustlsConnect as MakeTlsConnect<Socket>>::make_tls_connect(
                &mut self.tls,
                hostname,
            )?,
            mode: self.mode,
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
                b'S' => self
                    .tls
                    .connect(socket)
                    .await
                    .map(Box::new)
                    .map(Stream::Secure),
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
