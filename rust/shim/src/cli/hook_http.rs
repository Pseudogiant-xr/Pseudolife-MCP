//! urllib-shaped GETs for the hook leaves: connection order, per-receive
//! timeouts, request fields and redirect limits as Python's `urlopen` has them.
use std::{
    collections::HashMap,
    future::Future,
    io,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};

use bytes::Bytes;
use http_body_util::{BodyExt, Empty};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::time::{Instant, Sleep};

/// A reply from [`get`].
pub(super) struct Reply {
    pub(super) status: u16,
    pub(super) body: Vec<u8>,
    /// The final response's header fields.
    pub(super) headers: http::HeaderMap,
    /// [`get_bounded`] only: the status arrived but reading the body failed
    /// (a timeout, a reset, a cut-short body read to its end). [`get`]
    /// answers `None` there instead.
    pub(super) body_failed: bool,
    /// [`get_bounded`] only: a redirect this port does not follow where
    /// urllib would (an `ftp:` target, a target that is not text).
    pub(super) unsupported: bool,
}

/// How much body a caller's `read(n)` takes: on a 2xx reply, and on any
/// other reply; `None` reads to the end.
pub(super) type Limits = (Option<usize>, Option<usize>);

/// One GET as `urllib.request.urlopen` performs it:
/// - each resolved address in resolver order, each with its own connect
///   timeout (`socket.create_connection`), so `localhost` reaches an
///   IPv4-only daemon after `::1` fails;
/// - `timeout` bounds each send and receive, not the whole reply
///   (`socket.settimeout`): a daemon answering slowly but steadily completes,
///   one that goes quiet for `timeout` fails. Over https the handshake has
///   one deadline and each read must yield plaintext within `timeout`, as
///   CPython's SSL socket bounds each SSL operation;
/// - urllib's request fields: Host as written in the URL, User-Agent,
///   `Accept-Encoding: identity`, `Connection: close` and an optional
///   Authorization value already encoded as Latin-1;
/// - with `follow`, 301/302/303/307/308 are followed under
///   `HTTPRedirectHandler`'s limits (a URL at most four times, at most ten
///   distinct targets); otherwise a 3xx comes back as the reply.
///
/// https verifies against the system trust store, as CPython's default
/// context does; proxy settings are not consulted (declared deferral). `None` is a
/// transport failure, a malformed or short reply, or a redirect past the
/// limits.
pub(super) async fn get(
    origin: &str,
    target: &str,
    authorization: Option<&[u8]>,
    timeout: Duration,
    follow: bool,
) -> Option<Reply> {
    get_with(origin, target, authorization, timeout, follow, None).await
}

/// [`get`] with the caller's body bounds and urllib's error replies, for
/// `doctor`:
/// - `response.read(n)` stops after `n` bytes, and an HTTP error status's
///   body is read only as far as the caller's `exc.read(n)`; a bounded read
///   whose length-delimited body ends early returns what arrived, as
///   `HTTPResponse.read(amt)` does (http/client.py 3.11), where reading to
///   the end fails as `_safe_read`'s `IncompleteRead` does;
/// - a body that fails to read comes back as `body_failed`, not `None`;
/// - redirects follow `Location`, else `URI`; where `HTTPRedirectHandler`
///   raises `HTTPError` instead (neither header, a scheme it refuses, the
///   repeat limits) the 3xx reply itself comes back, its body read, as the
///   error's `fp` is.
///
/// [`get`] keeps its own behaviour: the whole body, none for an unfollowed
/// non-2xx reply, `Location` only, and `None` for every failure.
pub(super) async fn get_bounded(
    origin: &str,
    target: &str,
    authorization: Option<&[u8]>,
    timeout: Duration,
    follow: bool,
    limits: Limits,
) -> Option<Reply> {
    request(
        origin,
        target,
        authorization,
        timeout,
        follow,
        limits,
        None,
        true,
    )
    .await
}

async fn get_with(
    origin: &str,
    target: &str,
    authorization: Option<&[u8]>,
    timeout: Duration,
    follow: bool,
    tls: Option<Arc<rustls::ClientConfig>>,
) -> Option<Reply> {
    let limits = (None, if follow { None } else { Some(0) });
    request(
        origin,
        target,
        authorization,
        timeout,
        follow,
        limits,
        tls,
        false,
    )
    .await
    .filter(|reply| !reply.body_failed)
}

#[allow(clippy::too_many_arguments)]
async fn request(
    origin: &str,
    target: &str,
    authorization: Option<&[u8]>,
    timeout: Duration,
    follow: bool,
    limits: Limits,
    tls: Option<Arc<rustls::ClientConfig>>,
    urllib_errors: bool,
) -> Option<Reply> {
    let mut url = format!("{origin}{target}");
    let mut visited: HashMap<String, u32> = HashMap::new();
    loop {
        let mut reply = once(
            &url,
            authorization,
            timeout,
            limits,
            urllib_errors,
            tls.clone(),
        )
        .await?;
        if !(follow && matches!(reply.status, 301 | 302 | 303 | 307 | 308)) || reply.body_failed {
            return Some(reply);
        }
        // urllib raises HTTPError carrying this reply where it stops; `get`
        // has always answered None there.
        let stop = |mut reply: Reply, unsupported: bool| {
            reply.unsupported = unsupported;
            urllib_errors.then_some(reply)
        };
        let named = reply
            .headers
            .get(http::header::LOCATION)
            .or(if urllib_errors {
                reply.headers.get("URI")
            } else {
                None
            });
        let Some(named) = named else {
            return stop(reply, false);
        };
        let Ok(named) = named.to_str().map(str::to_owned) else {
            return stop(reply, true);
        };
        let Some(next) = reqwest::Url::parse(&url)
            .ok()
            .and_then(|base| base.join(&named).ok())
        else {
            return stop(reply, true);
        };
        match next.scheme() {
            "http" | "https" => {}
            "ftp" => return stop(reply, true),
            _ => return stop(reply, false),
        }
        let next: String = next.into();
        if visited.get(&next).copied().unwrap_or(0) >= 4 || visited.len() >= 10 {
            return stop(reply, false);
        }
        *visited.entry(next.clone()).or_insert(0) += 1;
        reply.body.clear();
        url = next;
    }
}

/// A body cut short of its length: hyper's length decoder fails with an
/// `UnexpectedEof` I/O error, which `HTTPResponse.read(amt)` treats as the
/// end of the body.
fn ended_early(error: &hyper::Error) -> bool {
    let mut source: Option<&(dyn std::error::Error + 'static)> = Some(error);
    while let Some(current) = source {
        if let Some(io) = current.downcast_ref::<io::Error>() {
            return io.kind() == io::ErrorKind::UnexpectedEof;
        }
        source = current.source();
    }
    false
}

trait Stream: AsyncRead + AsyncWrite + Send + Unpin {}
impl<T: AsyncRead + AsyncWrite + Send + Unpin> Stream for T {}

async fn once(
    url: &str,
    authorization: Option<&[u8]>,
    timeout: Duration,
    limits: Limits,
    short_reads: bool,
    tls: Option<Arc<rustls::ClientConfig>>,
) -> Option<Reply> {
    let (secure, rest) = if let Some(rest) = url.strip_prefix("https://") {
        (true, rest)
    } else {
        (false, url.strip_prefix("http://")?)
    };
    let split = rest.find(['/', '?']).unwrap_or(rest.len());
    let (authority, path) = rest.split_at(split);
    let target = match path.chars().next() {
        Some('/') => path.to_owned(),
        _ => format!("/{path}"),
    };
    let parsed = reqwest::Url::parse(url).ok()?;
    let host = parsed.host_str()?.trim_matches(['[', ']']).to_owned();
    let port = parsed.port_or_known_default()?;
    let mut connected = None;
    for address in tokio::net::lookup_host((host.as_str(), port)).await.ok()? {
        if let Ok(Ok(stream)) =
            tokio::time::timeout(timeout, tokio::net::TcpStream::connect(address)).await
        {
            connected = Some(stream);
            break;
        }
    }
    let socket = connected?;
    let stream: Box<dyn Stream> = if secure {
        let config = match tls {
            Some(config) => config,
            None => system_tls()?,
        };
        let name = rustls::pki_types::ServerName::try_from(host).ok()?;
        // CPython bounds the whole handshake by the socket timeout, then each
        // SSL read by its own deadline until plaintext arrives: the timer goes
        // above TLS, so encrypted fragments alone do not count as progress.
        let tls = tokio::time::timeout(
            timeout,
            tokio_rustls::TlsConnector::from(config).connect(name, socket),
        )
        .await
        .ok()?
        .ok()?;
        Box::new(Inactivity::new(RaggedEof(tls), timeout))
    } else {
        Box::new(Inactivity::new(socket, timeout))
    };
    let (mut sender, connection) =
        // Conventional Title-Case names (urllib writes `User-agent`;
        // field-name case carries no meaning in HTTP).
        hyper::client::conn::http1::Builder::new()
            .title_case_headers(true)
            .handshake(hyper_util::rt::TokioIo::new(stream))
            .await
            .ok()?;
    let driver = tokio::spawn(connection);
    let mut request = http::Request::get(target)
        .header("Accept-Encoding", "identity")
        .header("Host", authority)
        .header("User-Agent", "Python-urllib/3.11");
    if let Some(value) = authorization {
        request = request.header("Authorization", http::HeaderValue::from_bytes(value).ok()?);
    }
    let request = request
        .header("Connection", "close")
        .body(Empty::<Bytes>::new())
        .ok()?;
    let reply = async {
        let response = sender.send_request(request).await.ok()?;
        let status = response.status().as_u16();
        let headers = response.headers().clone();
        let limit = if (200..300).contains(&status) {
            limits.0
        } else {
            limits.1
        };
        // read(amt) on a chunked body raises IncompleteRead when cut short.
        let chunked = headers.contains_key(http::header::TRANSFER_ENCODING);
        let mut body = Vec::new();
        let mut body_failed = false;
        let mut incoming = response.into_body();
        while limit.is_none_or(|limit| body.len() < limit) {
            match incoming.frame().await {
                None => break,
                Some(Ok(frame)) => {
                    if let Ok(data) = frame.into_data() {
                        body.extend_from_slice(&data);
                    }
                }
                Some(Err(error)) => {
                    body_failed =
                        !(short_reads && limit.is_some() && !chunked && ended_early(&error));
                    break;
                }
            }
        }
        if let Some(limit) = limit {
            body.truncate(limit);
        }
        Some(Reply {
            status,
            body,
            headers,
            body_failed,
            unsupported: false,
        })
    }
    .await;
    driver.abort();
    reply
}

/// CPython's default context: the system trust store (or `SSL_CERT_FILE` /
/// `SSL_CERT_DIR`), certificate and hostname verification, no revocation
/// lookups. Declared gap: on Windows a set `SSL_CERT_FILE` replaces the
/// system store here, where CPython unions the two.
fn system_tls() -> Option<Arc<rustls::ClientConfig>> {
    let mut roots = rustls::RootCertStore::empty();
    roots.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
    let mut config = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::aws_lc_rs::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .ok()?
    .with_root_certificates(roots)
    .with_no_client_auth();
    // http.client offers ALPN http/1.1 on its default context.
    config.alpn_protocols = vec![b"http/1.1".to_vec()];
    Some(Arc::new(config))
}

/// A TLS stream whose peer closing without close_notify reads as end of
/// stream, as CPython's default `suppress_ragged_eofs=True` makes it; a body
/// still short of its declared length then fails as `IncompleteRead` does.
struct RaggedEof<S>(S);

impl<S: AsyncRead + Unpin> AsyncRead for RaggedEof<S> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        match Pin::new(&mut self.0).poll_read(cx, buf) {
            Poll::Ready(Err(error)) if error.kind() == io::ErrorKind::UnexpectedEof => {
                Poll::Ready(Ok(()))
            }
            other => other,
        }
    }
}

impl<S: AsyncWrite + Unpin> AsyncWrite for RaggedEof<S> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.0).poll_write(cx, buf)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.0).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.0).poll_shutdown(cx)
    }
}

/// A socket whose every read and write must make progress within `timeout`
/// of starting to wait, as `socket.settimeout` bounds each recv and send.
struct Inactivity<S> {
    inner: S,
    timeout: Duration,
    read: Option<Pin<Box<Sleep>>>,
    write: Option<Pin<Box<Sleep>>>,
}

impl<S> Inactivity<S> {
    fn new(inner: S, timeout: Duration) -> Self {
        Self {
            inner,
            timeout,
            read: None,
            write: None,
        }
    }
}

fn wait(
    deadline: &mut Option<Pin<Box<Sleep>>>,
    timeout: Duration,
    cx: &mut Context<'_>,
) -> Poll<io::Error> {
    let sleep = deadline
        .get_or_insert_with(|| Box::pin(tokio::time::sleep_until(Instant::now() + timeout)));
    match sleep.as_mut().poll(cx) {
        Poll::Ready(()) => Poll::Ready(io::Error::new(io::ErrorKind::TimedOut, "timed out")),
        Poll::Pending => Poll::Pending,
    }
}

impl<S: AsyncRead + Unpin> AsyncRead for Inactivity<S> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = &mut *self;
        match Pin::new(&mut this.inner).poll_read(cx, buf) {
            Poll::Ready(result) => {
                this.read = None;
                Poll::Ready(result)
            }
            Poll::Pending => wait(&mut this.read, this.timeout, cx).map(Err),
        }
    }
}

impl<S: AsyncWrite + Unpin> AsyncWrite for Inactivity<S> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = &mut *self;
        match Pin::new(&mut this.inner).poll_write(cx, buf) {
            Poll::Ready(result) => {
                this.write = None;
                Poll::Ready(result)
            }
            Poll::Pending => wait(&mut this.write, this.timeout, cx).map(Err),
        }
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// Serve `reply` to each connection, `chunk` bytes every `gap`.
    async fn server(
        reply: Vec<u8>,
        chunk: usize,
        gap: Duration,
        tls: Option<Arc<rustls::ServerConfig>>,
    ) -> (u16, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let task = tokio::spawn(async move {
            loop {
                let Ok((socket, _)) = listener.accept().await else {
                    return;
                };
                let (reply, tls) = (reply.clone(), tls.clone());
                tokio::spawn(async move {
                    let mut stream: Box<dyn Stream> = match tls {
                        Some(config) => {
                            match tokio_rustls::TlsAcceptor::from(config).accept(socket).await {
                                Ok(stream) => Box::new(stream),
                                Err(_) => return,
                            }
                        }
                        None => Box::new(socket),
                    };
                    let mut request = Vec::new();
                    let mut buffer = [0u8; 1024];
                    while !request.windows(4).any(|w| w == b"\r\n\r\n") {
                        match stream.read(&mut buffer).await {
                            Ok(0) | Err(_) => return,
                            Ok(read) => request.extend_from_slice(&buffer[..read]),
                        }
                    }
                    for piece in reply.chunks(chunk) {
                        if stream.write_all(piece).await.is_err() {
                            return;
                        }
                        let _ = stream.flush().await;
                        tokio::time::sleep(gap).await;
                    }
                    let _ = stream.shutdown().await;
                });
            }
        });
        (port, task)
    }

    fn fixture(name: &str) -> Vec<u8> {
        std::fs::read(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("tests/fixtures/pg_tls")
                .join(name),
        )
        .unwrap()
    }

    fn tls_pair() -> (Arc<rustls::ServerConfig>, Arc<rustls::ClientConfig>) {
        let provider = Arc::new(rustls::crypto::aws_lc_rs::default_provider());
        let certs = rustls_pemfile::certs(&mut fixture("server.pem").as_slice())
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        let key = rustls_pemfile::private_key(&mut fixture("server-key.pem").as_slice())
            .unwrap()
            .unwrap();
        let server = rustls::ServerConfig::builder_with_provider(provider.clone())
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(certs, key)
            .unwrap();
        let mut roots = rustls::RootCertStore::empty();
        for ca in rustls_pemfile::certs(&mut fixture("ca.pem").as_slice()) {
            roots.add(ca.unwrap()).unwrap();
        }
        let client = rustls::ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_root_certificates(roots)
            .with_no_client_auth();
        (Arc::new(server), Arc::new(client))
    }

    const OK: &[u8] = b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\ntrickl";

    #[tokio::test(flavor = "current_thread")]
    async fn a_trickled_reply_completes_over_http_and_https() {
        let (server_tls, client_tls) = tls_pair();
        for (scheme, tls) in [("http", None), ("https", Some(server_tls))] {
            let (port, task) = server(OK.to_vec(), 8, Duration::from_millis(60), tls).await;
            let host = if scheme == "https" {
                "localhost"
            } else {
                "127.0.0.1"
            };
            let reply = get_with(
                &format!("{scheme}://{host}:{port}"),
                "/health",
                None,
                Duration::from_millis(250),
                true,
                Some(client_tls.clone()),
            )
            .await
            .unwrap_or_else(|| panic!("{scheme} trickle failed"));
            assert_eq!(
                (reply.status, reply.body.as_slice()),
                (200, b"trickl".as_slice())
            );
            task.abort();
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn a_reply_that_stalls_past_the_timeout_fails() {
        let (port, task) = server(OK.to_vec(), 20, Duration::from_millis(500), None).await;
        let reply = get(
            &format!("http://127.0.0.1:{port}"),
            "/health",
            None,
            Duration::from_millis(150),
            true,
        )
        .await;
        assert!(reply.is_none());
        task.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn a_tls_reply_that_stalls_past_the_timeout_fails() {
        let (server_tls, client_tls) = tls_pair();
        let (port, task) = server(
            OK.to_vec(),
            20,
            Duration::from_millis(500),
            Some(server_tls),
        )
        .await;
        let reply = get_with(
            &format!("https://localhost:{port}"),
            "/health",
            None,
            Duration::from_millis(150),
            true,
            Some(client_tls),
        )
        .await;
        assert!(reply.is_none());
        task.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn a_handshake_slower_than_the_timeout_fails_even_while_bytes_flow() {
        // A relay forwards the server's first 768 bytes (its handshake flight)
        // 16 bytes every 20 ms, then everything else at once: each TCP read
        // progresses, but the handshake takes far longer than 250 ms, which
        // CPython's do_handshake deadline refuses.
        let (server_tls, client_tls) = tls_pair();
        let (port, task) = server(OK.to_vec(), OK.len(), Duration::ZERO, Some(server_tls)).await;
        let relay = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let relay_port = relay.local_addr().unwrap().port();
        let relay_task = tokio::spawn(async move {
            let (client, _) = relay.accept().await.unwrap();
            let upstream = tokio::net::TcpStream::connect(("127.0.0.1", port))
                .await
                .unwrap();
            let (mut client_read, mut client_write) = client.into_split();
            let (mut up_read, mut up_write) = upstream.into_split();
            tokio::spawn(async move {
                let _ = tokio::io::copy(&mut client_read, &mut up_write).await;
            });
            let mut buffer = [0u8; 16];
            let mut forwarded = 0;
            while let Ok(read) = up_read.read(&mut buffer).await {
                if read == 0 || client_write.write_all(&buffer[..read]).await.is_err() {
                    return;
                }
                forwarded += read;
                if forwarded < 768 {
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
            }
        });
        let reply = get_with(
            &format!("https://localhost:{relay_port}"),
            "/health",
            None,
            Duration::from_millis(250),
            true,
            Some(client_tls),
        )
        .await;
        assert!(reply.is_none());
        relay_task.abort();
        task.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn a_rejected_payload_returns_without_reading_its_body() {
        // A 500 whose body trickles for seconds: the payload request is
        // rejected at its head, as urllib's HTTPErrorProcessor raises.
        let error = b"HTTP/1.1 500 Boom\r\nContent-Length: 400\r\nConnection: close\r\n\r\n";
        let mut reply = error.to_vec();
        reply.extend(std::iter::repeat_n(b'x', 400));
        let (port, task) = server(reply, error.len(), Duration::from_millis(200), None).await;
        let started = std::time::Instant::now();
        let rejected = get(
            &format!("http://127.0.0.1:{port}"),
            "/api/briefing",
            None,
            Duration::from_secs(5),
            false,
        )
        .await
        .unwrap();
        assert_eq!((rejected.status, rejected.body.len()), (500, 0));
        assert!(started.elapsed() < Duration::from_secs(2));
        task.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn a_bounded_read_stops_at_its_limit_and_reads_error_bodies_to_theirs() {
        // A body cut short of its declared length: read(20) returns its first
        // 20 bytes; reading to the end fails as IncompleteRead does.
        let short = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n";
        let mut reply = short.to_vec();
        reply.extend(std::iter::repeat_n(b'x', 50));
        let (port, task) = server(reply, 1024, Duration::ZERO, None).await;
        let origin = format!("http://127.0.0.1:{port}");
        let timeout = Duration::from_millis(500);
        let bounded = get_bounded(&origin, "/a", None, timeout, false, (Some(20), Some(0)))
            .await
            .unwrap();
        assert_eq!((bounded.status, bounded.body.len()), (200, 20));
        assert!(
            get_bounded(&origin, "/a", None, timeout, false, (None, None))
                .await
                .unwrap()
                .body_failed
        );
        task.abort();
        // An unfollowed error status's body is read as far as exc.read(n).
        let refused = b"HTTP/1.1 409 No\r\nContent-Length: 9\r\nX-PL-Board: off\r\nConnection: close\r\n\r\nrefused!!";
        let (port, task) = server(refused.to_vec(), 1024, Duration::ZERO, None).await;
        let origin = format!("http://127.0.0.1:{port}");
        let error = get_bounded(&origin, "/a", None, timeout, false, (None, Some(7)))
            .await
            .unwrap();
        assert_eq!(
            (error.status, error.body.as_slice()),
            (409, b"refused".as_slice())
        );
        assert_eq!(error.headers.get("X-PL-Board").unwrap(), "off");
        let plain = get(&origin, "/a", None, timeout, false).await.unwrap();
        assert_eq!((plain.status, plain.body.len()), (409, 0));
        task.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn a_bounded_read_of_a_body_cut_short_returns_what_arrived() {
        // Content-Length 100, then 50 bytes (or none) and a clean close:
        // HTTPResponse.read(amt) returns what arrived; reading to the end
        // fails as _safe_read's IncompleteRead does, and `get` stays None.
        let timeout = Duration::from_millis(500);
        for sent in [50usize, 0] {
            let head = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n";
            let mut reply = head.to_vec();
            reply.extend(std::iter::repeat_n(b'y', sent));
            let (port, task) = server(reply, 4096, Duration::ZERO, None).await;
            let origin = format!("http://127.0.0.1:{port}");
            let bounded = get_bounded(&origin, "/a", None, timeout, false, (Some(1024), Some(0)))
                .await
                .unwrap();
            assert_eq!((bounded.body.len(), bounded.body_failed), (sent, false));
            let whole = get_bounded(&origin, "/a", None, timeout, false, (None, None))
                .await
                .unwrap();
            assert!(whole.body_failed);
            assert!(get(&origin, "/a", None, timeout, true).await.is_none());
            task.abort();
        }
        // A chunked body cut short raises IncompleteRead even under read(amt).
        let chunked =
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n10\r\nshort";
        let (port, task) = server(chunked.to_vec(), 4096, Duration::ZERO, None).await;
        let reply = get_bounded(
            &format!("http://127.0.0.1:{port}"),
            "/a",
            None,
            timeout,
            false,
            (Some(1024), Some(0)),
        )
        .await
        .unwrap();
        assert!(reply.body_failed);
        task.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn bounded_redirects_follow_uri_and_return_where_urllib_raises() {
        let timeout = Duration::from_millis(500);
        let ok = |status: &str, extra: &str, body: &str| {
            format!(
                "HTTP/1.1 {status}\r\n{extra}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .into_bytes()
        };
        // URI only: get_bounded follows it to the 200; get keeps None.
        let target = ok("200 OK", "", "moved");
        let (target_port, target_task) = server(target, 4096, Duration::ZERO, None).await;
        let uri = ok(
            "302 Found",
            &format!("URI: http://127.0.0.1:{target_port}/b\r\n"),
            "",
        );
        let (port, task) = server(uri, 4096, Duration::ZERO, None).await;
        let origin = format!("http://127.0.0.1:{port}");
        let followed = get_bounded(&origin, "/a", None, timeout, true, (None, None))
            .await
            .unwrap();
        assert_eq!(
            (followed.status, followed.body.as_slice()),
            (200, b"moved".as_slice())
        );
        assert!(get(&origin, "/a", None, timeout, true).await.is_none());
        task.abort();
        target_task.abort();
        // No target, a refused scheme, the repeat limit: the 3xx itself.
        for (extra, unsupported) in [
            ("", false),
            ("Location: gopher://x/y\r\n", false),
            ("Location: /a\r\n", false),
            ("Location: ftp://x/y\r\n", true),
        ] {
            let (port, task) =
                server(ok("307 Again", extra, "kept"), 4096, Duration::ZERO, None).await;
            let origin = format!("http://127.0.0.1:{port}");
            let stopped = get_bounded(&origin, "/a", None, timeout, true, (None, None))
                .await
                .unwrap();
            assert_eq!(
                (stopped.status, stopped.body.as_slice(), stopped.unsupported),
                (307, b"kept".as_slice(), unsupported)
            );
            assert!(get(&origin, "/a", None, timeout, true).await.is_none());
            task.abort();
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn localhost_reaches_an_ipv4_only_listener_within_the_budget() {
        let (port, task) = server(OK.to_vec(), OK.len(), Duration::ZERO, None).await;
        let reply = get(
            &format!("http://localhost:{port}"),
            "/health",
            None,
            Duration::from_millis(250),
            true,
        )
        .await
        .unwrap();
        assert_eq!(reply.status, 200);
        task.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn redirects_stop_at_urllibs_repeat_limit() {
        let redirect =
            b"HTTP/1.1 307 Again\r\nLocation: /health\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
        let (port, task) = server(redirect.to_vec(), redirect.len(), Duration::ZERO, None).await;
        let origin = format!("http://127.0.0.1:{port}");
        assert!(
            get(&origin, "/health", None, Duration::from_millis(500), true)
                .await
                .is_none()
        );
        let unfollowed = get(&origin, "/health", None, Duration::from_millis(500), false)
            .await
            .unwrap();
        assert_eq!(unfollowed.status, 307);
        task.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn an_unknown_certificate_authority_is_refused() {
        let (server_tls, _) = tls_pair();
        let (port, task) = server(OK.to_vec(), OK.len(), Duration::ZERO, Some(server_tls)).await;
        let reply = get(
            &format!("https://localhost:{port}"),
            "/health",
            None,
            Duration::from_millis(500),
            true,
        )
        .await;
        assert!(
            reply.is_none(),
            "the fixture CA is in no platform trust store"
        );
        task.abort();
    }
}
