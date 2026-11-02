//! The pairing commands' daemon traffic, as urllib makes it: no proxy and
//! no redirect followed for `/health` and `POST /api/pair`; the token check
//! (`codex_connection.installer_credential_valid`) uses a no-redirect opener
//! that honours environment proxies. HTTPS uses system trust.
use super::pyjson::{self, J};
use std::time::Duration;

/// `pair_cli.MAX_RESPONSE_BYTES`.
const MAX_RESPONSE_BYTES: usize = 65536;

fn client(timeout: Duration, use_proxies: bool) -> Option<reqwest::Client> {
    let mut builder = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(timeout)
        .pool_max_idle_per_host(0);
    if !use_proxies {
        builder = builder.no_proxy();
    }
    // Python's default HTTPS context uses the system trust store, including
    // SSL_CERT_FILE / SSL_CERT_DIR. The native loader honours those overrides.
    let roots = rustls_native_certs::load_native_certs();
    if !roots.certs.is_empty() {
        let certs = roots
            .certs
            .iter()
            .map(|cert| reqwest::Certificate::from_der(cert.as_ref()))
            .collect::<Result<Vec<_>, _>>()
            .ok()?;
        builder = builder.tls_certs_only(certs);
    } else if std::env::var_os("SSL_CERT_FILE").is_some()
        || std::env::var_os("SSL_CERT_DIR").is_some()
    {
        return None;
    }
    builder.build().ok()
}

/// `response.read(limit)` (or `read()` with no limit): `Err` when the body
/// could not be read.
async fn read(response: &mut reqwest::Response, limit: Option<usize>) -> Result<Vec<u8>, ()> {
    let mut body = Vec::new();
    loop {
        if limit.is_some_and(|limit| body.len() >= limit) {
            break;
        }
        match response.chunk().await {
            Ok(Some(chunk)) => body.extend_from_slice(&chunk),
            Ok(None) => break,
            Err(_) => return Err(()),
        }
    }
    if let Some(limit) = limit {
        body.truncate(limit);
    }
    Ok(body)
}

/// The oracle raised `RecursionError` reading the answer: past the read
/// path's measured nesting limit.
#[derive(Debug)]
pub(super) struct TooDeep;

/// `json.loads(body.decode("utf-8"))` as a dict, else `None` (a decode or
/// JSON error, the digit-limit `ValueError`, or another type).
fn dict(body: &[u8], limit: usize) -> Result<Option<pyjson::Dict>, TooDeep> {
    let Ok(text) = std::str::from_utf8(body) else {
        return Ok(None);
    };
    match pyjson::loads(text, limit) {
        Ok(J::Dict(dict)) => Ok(Some(dict)),
        Ok(_) | Err(pyjson::Refused::NotJson) => Ok(None),
        Err(pyjson::Refused::TooDeep) => Err(TooDeep),
    }
}

/// `pair_cli.probe_health(url)`: the body of any HTTP answer (a redirect's
/// too: `_NoRedirectHandler` turns it into an `HTTPError`) as a dict.
pub(super) async fn pair_health(url: &str) -> Result<Option<pyjson::Dict>, TooDeep> {
    let Some(client) = client(Duration::from_secs(5), false) else {
        return Ok(None);
    };
    let Ok(mut response) = client.get(format!("{url}/health")).send().await else {
        return Ok(None);
    };
    let Ok(body) = read(&mut response, Some(MAX_RESPONSE_BYTES)).await else {
        return Ok(None);
    };
    dict(&body, pyjson::PAIR_DEPTH)
}

/// `expose_cli.probe_health(url, 3.0)`: a JSON object from any non-redirect
/// answer, else `None`.
pub(super) async fn invite_health(url: &str) -> Result<Option<pyjson::Dict>, TooDeep> {
    let Some(client) = client(Duration::from_secs(3), false) else {
        return Ok(None);
    };
    let Ok(mut response) = client.get(format!("{url}/health")).send().await else {
        return Ok(None);
    };
    if response.status().is_redirection() {
        return Ok(None);
    }
    let Ok(body) = read(&mut response, None).await else {
        return Ok(None);
    };
    dict(&body, pyjson::INVITE_DEPTH)
}

/// One `pair_cli.post_pair`: `Err` when no HTTP answer arrived (Python's
/// exception), else the status and, for a body that is a JSON object, that
/// object. A 2xx body that cannot be read is no answer; a refusal's
/// unreadable body is no payload.
pub(super) async fn post_pair(url: &str, body: &str) -> Result<(u16, Option<pyjson::Dict>), ()> {
    let client = client(Duration::from_secs(10), false).ok_or(())?;
    let mut response = client
        .post(format!("{url}/api/pair"))
        .header(reqwest::header::CONTENT_TYPE, "application/json")
        .body(body.to_owned())
        .send()
        .await
        .map_err(|_| ())?;
    let status = response.status().as_u16();
    let success = response.status().is_success();
    let text = match read(&mut response, Some(MAX_RESPONSE_BYTES)).await {
        Ok(text) => text,
        Err(()) if success => return Err(()),
        Err(()) => return Ok((status, None)),
    };
    match dict(&text, pyjson::PAIR_DEPTH) {
        Ok(payload) => Ok((status, payload)),
        // `_json` lets RecursionError through: out of a 2xx's `with` block it
        // is no answer; an HTTPError's body read catches it (no payload).
        Err(TooDeep) if success => Err(()),
        Err(TooDeep) => Ok((status, None)),
    }
}

/// `codex_connection.installer_credential_valid(url, token)`: a 200 to an
/// authenticated `GET /api/episodes?limit=1`, without following redirects.
pub(super) async fn credential_valid(url: &str, token: &str) -> bool {
    let Some(client) = client(Duration::from_secs(3), true) else {
        return false;
    };
    let Ok(header) = reqwest::header::HeaderValue::from_str(&format!("Bearer {token}")) else {
        return false;
    };
    let Ok(mut response) = client
        .get(format!("{url}/api/episodes?limit=1"))
        .header(reqwest::header::AUTHORIZATION, header)
        .send()
        .await
    else {
        return false;
    };
    if response.status().as_u16() != 200 {
        return false;
    }
    response.chunk().await.is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[tokio::test]
    async fn credential_verification_does_not_follow_redirects() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (finished, mut finish) = tokio::sync::oneshot::channel();
        let server = tokio::spawn(async move {
            let mut requests = 0;
            loop {
                let (mut stream, _) = tokio::select! {
                    accepted = listener.accept() => accepted.unwrap(),
                    _ = &mut finish => break,
                };
                let mut request = [0; 4096];
                let read = stream.read(&mut request).await.unwrap();
                assert!(read > 0, "credential probe must send a request");
                requests += 1;
                let response = if requests == 1 {
                    "HTTP/1.1 302 Found\r\nLocation: /accepted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                } else {
                    "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
                };
                stream.write_all(response.as_bytes()).await.unwrap();
            }
            requests
        });
        let valid = credential_valid(&format!("http://{address}"), "fixture-token").await;
        finished.send(()).unwrap();
        let requests = server.await.unwrap();
        assert!(!valid, "a redirect must not validate a credential");
        assert_eq!(
            requests, 1,
            "credential verification must not request the redirect target"
        );
    }
}
