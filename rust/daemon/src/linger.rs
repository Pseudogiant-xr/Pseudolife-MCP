//! Orderly close for accepted connections.
//!
//! Closing a TCP socket with request bytes still unread makes the stack send
//! RST, and Windows clients then discard a response they have received but
//! not yet read. A refusal sent before the upload ends was lost that way
//! (`divergences.md`, MCP-REFUSAL-CLEANUP). uvicorn never closes there: it
//! keeps the connection and discards the rest of the body.
//!
//! Hyper ends a connection through `poll_shutdown` when keep-alive is off,
//! after a parse error, at client EOF and on graceful shutdown (an I/O error
//! skips it). This wrapper turns that into a lingering close: send FIN after
//! the response, then read and discard until the client closes, the line
//! stays quiet for [`LINGER_QUIET`], or [`LINGER_DEADLINE`] passes. Only then
//! is the socket dropped; an upload still arriving at the deadline is the one
//! case closed with bytes unread.

use std::future::Future;
use std::io;
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::time::{Instant, Sleep};

/// Longest a closing connection keeps discarding a client's upload.
pub(crate) const LINGER_DEADLINE: Duration = Duration::from_secs(2);
/// A closing connection with no bytes arriving for this long is closed.
/// Matches the refusal cleanup deadline (`http::REFUSAL_DRAIN_DEADLINE`).
pub(crate) const LINGER_QUIET: Duration = Duration::from_millis(250);

/// Wraps every accepted stream in a [`LingerStream`].
pub(crate) struct LingerListener<L>(L);

impl<L> LingerListener<L> {
    pub(crate) fn new(inner: L) -> Self {
        Self(inner)
    }
}

impl<L> axum::serve::Listener for LingerListener<L>
where
    L: axum::serve::Listener,
{
    type Io = LingerStream<L::Io>;
    type Addr = L::Addr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        let (io, addr) = self.0.accept().await;
        (LingerStream::new(io), addr)
    }

    fn local_addr(&self) -> io::Result<Self::Addr> {
        self.0.local_addr()
    }
}

enum State {
    Open,
    Lingering {
        deadline: Instant,
        quiet_until: Instant,
        wake: Pin<Box<Sleep>>,
    },
    Closed,
}

pub(crate) struct LingerStream<S> {
    inner: S,
    state: State,
}

impl<S> LingerStream<S> {
    pub(crate) fn new(inner: S) -> Self {
        Self {
            inner,
            state: State::Open,
        }
    }
}

impl<S: AsyncRead + Unpin> AsyncRead for LingerStream<S> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_read(cx, buf)
    }
}

impl<S: AsyncRead + AsyncWrite + Unpin> AsyncWrite for LingerStream<S> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.inner).poll_write(cx, buf)
    }

    fn poll_write_vectored(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bufs: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.inner).poll_write_vectored(cx, bufs)
    }

    fn is_write_vectored(&self) -> bool {
        self.inner.is_write_vectored()
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = &mut *self;
        loop {
            match &mut this.state {
                State::Open => {
                    std::task::ready!(Pin::new(&mut this.inner).poll_shutdown(cx))?;
                    let now = Instant::now();
                    this.state = State::Lingering {
                        deadline: now + LINGER_DEADLINE,
                        quiet_until: now + LINGER_QUIET,
                        wake: Box::pin(tokio::time::sleep_until(now + LINGER_QUIET)),
                    };
                }
                State::Lingering {
                    deadline,
                    quiet_until,
                    wake,
                } => {
                    // Both limits are plain instants checked on every pass: a
                    // Sleep registers its timer only once polled, and a client
                    // that keeps the socket readable can keep it unpolled
                    // (each read spends task budget), so it serves as the
                    // wakeup only.
                    let mut scratch = [0u8; 16 * 1024];
                    loop {
                        let now = Instant::now();
                        if now >= *deadline || now >= *quiet_until {
                            break;
                        }
                        let mut buf = ReadBuf::new(&mut scratch);
                        match Pin::new(&mut this.inner).poll_read(cx, &mut buf) {
                            // EOF or a failed read: nothing more can arrive unread.
                            Poll::Ready(Ok(())) if buf.filled().is_empty() => break,
                            Poll::Ready(Err(_)) => break,
                            Poll::Ready(Ok(())) => *quiet_until = now + LINGER_QUIET,
                            Poll::Pending => {
                                let next = (*deadline).min(*quiet_until);
                                if wake.deadline() != next {
                                    wake.as_mut().reset(next);
                                }
                                if wake.as_mut().poll(cx).is_ready() {
                                    continue;
                                }
                                return Poll::Pending;
                            }
                        }
                    }
                    this.state = State::Closed;
                }
                State::Closed => return Poll::Ready(Ok(())),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    async fn pair() -> (LingerStream<tokio::net::TcpStream>, tokio::net::TcpStream) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let client = tokio::net::TcpStream::connect(listener.local_addr().unwrap())
            .await
            .unwrap();
        let (server, _) = listener.accept().await.unwrap();
        (LingerStream::new(server), client)
    }

    #[tokio::test]
    async fn shutdown_sends_fin_then_discards_until_client_eof() {
        let (mut server, mut client) = pair().await;
        server.write_all(b"answer").await.unwrap();
        let close = tokio::spawn(async move {
            let started = Instant::now();
            server.shutdown().await.unwrap();
            started.elapsed()
        });
        // The answer and then EOF arrive, and the upload that follows is
        // read, not reset: without the linger these writes fail.
        let mut answer = Vec::new();
        client.read_to_end(&mut answer).await.unwrap();
        assert_eq!(answer, b"answer");
        for _ in 0..8 {
            client.write_all(&[b' '; 64 * 1024]).await.unwrap();
        }
        client.flush().await.unwrap();
        drop(client);
        let lingered = close.await.unwrap();
        assert!(lingered < LINGER_DEADLINE, "{lingered:?}");
    }

    #[tokio::test]
    async fn quiet_client_is_closed_after_the_quiet_window() {
        let (mut server, _client) = pair().await;
        let started = Instant::now();
        server.shutdown().await.unwrap();
        let lingered = started.elapsed();
        assert!(lingered >= LINGER_QUIET, "{lingered:?}");
        assert!(lingered < LINGER_DEADLINE, "{lingered:?}");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_streaming_client_is_cut_at_the_deadline() {
        let (mut server, mut client) = pair().await;
        // As fast as the socket takes it: the reads stay ready, so only the
        // deadline itself can end the linger.
        let writer =
            tokio::spawn(
                async move { while client.write_all(&[b' '; 64 * 1024]).await.is_ok() {} },
            );
        let started = Instant::now();
        tokio::time::timeout(LINGER_DEADLINE * 3, server.shutdown())
            .await
            .expect("the linger outlived its deadline")
            .unwrap();
        let lingered = started.elapsed();
        assert!(lingered < LINGER_DEADLINE * 2, "{lingered:?}");
        drop(server);
        writer.await.unwrap();
    }

    /// Always readable, and charging tokio's task budget per read as a
    /// socket does: a client that keeps the receive queue full.
    struct Flood;

    impl AsyncRead for Flood {
        fn poll_read(
            self: Pin<&mut Self>,
            cx: &mut Context<'_>,
            buf: &mut ReadBuf<'_>,
        ) -> Poll<io::Result<()>> {
            let coop = std::task::ready!(tokio::task::coop::poll_proceed(cx));
            buf.put_slice(&[b' '; 1024]);
            coop.made_progress();
            Poll::Ready(Ok(()))
        }
    }

    impl AsyncWrite for Flood {
        fn poll_write(
            self: Pin<&mut Self>,
            _: &mut Context<'_>,
            buf: &[u8],
        ) -> Poll<io::Result<usize>> {
            Poll::Ready(Ok(buf.len()))
        }
        fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }
        fn poll_shutdown(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn a_budget_starved_linger_still_ends_at_its_deadline() {
        let mut stream = LingerStream::new(Flood);
        let started = Instant::now();
        tokio::time::timeout(LINGER_DEADLINE * 3, stream.shutdown())
            .await
            .expect("the linger outlived its deadline")
            .unwrap();
        let lingered = started.elapsed();
        assert!(lingered >= LINGER_DEADLINE, "{lingered:?}");
        assert!(lingered < LINGER_DEADLINE * 2, "{lingered:?}");
    }

    #[tokio::test]
    async fn closed_client_ends_the_linger_at_once() {
        let (mut server, client) = pair().await;
        drop(client);
        let started = Instant::now();
        server.shutdown().await.unwrap();
        assert!(started.elapsed() < LINGER_QUIET, "{:?}", started.elapsed());
        // Idempotent: a second shutdown does not linger again.
        server.shutdown().await.unwrap();
    }
}
