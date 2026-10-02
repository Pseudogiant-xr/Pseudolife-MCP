//! HTTP-only helper. The Python shim remains the MCP and credential authority.
use base64::{Engine, engine::general_purpose::STANDARD};
use reqwest::{
    Certificate, Client, Method, Url,
    header::{HeaderMap, HeaderName, HeaderValue},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{collections::HashMap, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncWriteExt, BufReader},
    sync::{Semaphore, mpsc},
    task::{AbortHandle, JoinSet},
};

// Protocol design bounds, not performance tuning: one request can carry a
// 32 MiB body; four 16 KiB chunks per stream bound outstanding pipe data.
const MAX_FRAME: usize = 64 * 1024 * 1024;
const MAX_BODY: usize = 32 * 1024 * 1024;
const CHUNK: usize = 16 * 1024;
const WINDOW: usize = 4;
const MAX_ACTIVE: usize = 100; // httpx's default concurrent connection limit.

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum Command {
    Configure {
        version: u32,
        origin: String,
        certificates: Option<Vec<String>>,
    },
    Request {
        id: u64,
        url: String,
        method: String,
        body: String,
        headers: Vec<(String, String)>,
        proxy: Option<String>,
        timeout: HashMap<String, Option<f64>>,
    },
    Cancel {
        id: u64,
    },
    Credit {
        id: u64,
    },
}

type Output = mpsc::Sender<Value>;
type ClientKey = (Option<u64>, Option<u64>, Option<String>);

struct Configuration {
    origin: Url,
    certificates: Option<Vec<Certificate>>,
    clients: HashMap<ClientKey, Client>,
}

impl Configuration {
    fn new(version: u32, origin: &str, certificates: Option<Vec<String>>) -> Result<Self, ()> {
        let origin = Url::parse(origin).map_err(|_| ())?;
        if version != 1 || !valid_url(&origin) || origin.path() != "/" || origin.query().is_some() {
            return Err(());
        }
        let certificates = certificates
            .map(|values| {
                values
                    .into_iter()
                    .map(|value| {
                        let bytes = STANDARD.decode(value).map_err(|_| ())?;
                        Certificate::from_der(&bytes).map_err(|_| ())
                    })
                    .collect::<Result<Vec<_>, ()>>()
            })
            .transpose()?;
        if certificates.as_ref().is_some_and(Vec::is_empty) {
            return Err(());
        }
        Ok(Self {
            origin,
            certificates,
            clients: HashMap::new(),
        })
    }

    fn client(
        &mut self,
        timeout: &HashMap<String, Option<f64>>,
        proxy: Option<String>,
    ) -> Result<Client, ()> {
        for (name, value) in timeout {
            if !matches!(name.as_str(), "connect" | "read" | "write" | "pool")
                || value.is_some_and(|seconds| !seconds.is_finite() || seconds <= 0.0)
            {
                return Err(());
            }
        }
        let connect = timeout.get("connect").copied().flatten();
        let read = timeout.get("read").copied().flatten();
        let key = (
            connect.map(f64::to_bits),
            read.map(f64::to_bits),
            proxy.clone(),
        );
        if let Some(client) = self.clients.get(&key) {
            return Ok(client.clone());
        }
        let mut builder = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            // Python resolves trust_env proxies with the same URLPattern logic
            // as its default transport; do not also apply Rust/system proxies.
            .no_proxy()
            .tls_backend_native()
            // Fresh HTTP exchanges preserve the shim's idle/restart behavior.
            .pool_max_idle_per_host(0);
        if let Some(seconds) = connect {
            builder =
                builder.connect_timeout(Duration::try_from_secs_f64(seconds).map_err(|_| ())?);
        }
        if let Some(seconds) = read {
            builder = builder.read_timeout(Duration::try_from_secs_f64(seconds).map_err(|_| ())?);
        }
        if let Some(certificates) = &self.certificates {
            builder = builder.tls_certs_only(certificates.clone());
        }
        if let Some(proxy) = proxy {
            builder = builder.proxy(reqwest::Proxy::all(proxy).map_err(|_| ())?);
        }
        let client = builder.build().map_err(|_| ())?;
        if self.clients.len() >= 16 {
            self.clients.clear();
        }
        self.clients.insert(key, client.clone());
        Ok(client)
    }
}

fn valid_url(url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https")
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none()
}

fn prepare(
    url: &str,
    method: &str,
    body: &str,
    headers: Vec<(String, String)>,
    config: &Configuration,
) -> Result<(Url, Method, Vec<u8>, HeaderMap), ()> {
    let url = Url::parse(url).map_err(|_| ())?;
    if !valid_url(&url) || url.origin() != config.origin.origin() {
        return Err(());
    }
    let method = Method::from_bytes(method.as_bytes()).map_err(|_| ())?;
    let body = STANDARD.decode(body).map_err(|_| ())?;
    if body.len() > MAX_BODY {
        return Err(());
    }
    let mut result = HeaderMap::new();
    for (name, value) in headers {
        let name =
            HeaderName::from_bytes(&STANDARD.decode(name).map_err(|_| ())?).map_err(|_| ())?;
        let mut value =
            HeaderValue::from_bytes(&STANDARD.decode(value).map_err(|_| ())?).map_err(|_| ())?;
        value.set_sensitive(true);
        result.append(name, value);
    }
    Ok((url, method, body, result))
}

fn error_kind(error: &reqwest::Error) -> &'static str {
    if error.is_timeout() {
        if error.is_connect() {
            "connect_timeout"
        } else {
            "timeout"
        }
    } else if error.is_builder() {
        "protocol"
    } else {
        "connection"
    }
}

async fn error(output: &Output, id: u64, kind: &str) {
    // Never serialize reqwest error Display/Debug, URL, headers or body.
    let _ = output
        .send(json!({"type": "error", "id": id, "kind": kind}))
        .await;
}

async fn exchange(
    client: Client,
    id: u64,
    request: (Url, Method, Vec<u8>, HeaderMap),
    output: Output,
    credits: Arc<Semaphore>,
) {
    let (url, method, body, headers) = request;
    let mut response = match client
        .request(method, url)
        .headers(headers)
        .body(body)
        .send()
        .await
    {
        Ok(response) => response,
        Err(failure) => {
            error(&output, id, error_kind(&failure)).await;
            return;
        }
    };
    let headers: Vec<_> = response
        .headers()
        .iter()
        .map(|(name, value)| {
            (
                STANDARD.encode(name.as_str().as_bytes()),
                STANDARD.encode(value.as_bytes()),
            )
        })
        .collect();
    if output
        .send(
            json!({"type": "head", "id": id, "status": response.status().as_u16(),
                          "headers": headers}),
        )
        .await
        .is_err()
    {
        return;
    }
    loop {
        match response.chunk().await {
            Ok(Some(bytes)) => {
                for chunk in bytes.chunks(CHUNK) {
                    let Ok(permit) = credits.acquire().await else {
                        return;
                    };
                    permit.forget();
                    if output
                        .send(json!({"type": "chunk", "id": id,
                                          "data": STANDARD.encode(chunk)}))
                        .await
                        .is_err()
                    {
                        return;
                    }
                }
            }
            Ok(None) => {
                let _ = output.send(json!({"type": "end", "id": id})).await;
                return;
            }
            Err(failure) => {
                error(&output, id, error_kind(&failure)).await;
                return;
            }
        }
    }
}

async fn line<R: AsyncBufRead + Unpin>(
    input: &mut R,
    partial: &mut Vec<u8>,
) -> Result<Option<Vec<u8>>, ()> {
    loop {
        let bytes = input.fill_buf().await.map_err(|_| ())?;
        if bytes.is_empty() {
            return if partial.is_empty() {
                Ok(None)
            } else {
                Err(())
            };
        }
        let length = bytes
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(bytes.len(), |offset| offset + 1);
        if partial.len() + length > MAX_FRAME {
            return Err(());
        }
        let complete = bytes[length - 1] == b'\n';
        partial.extend_from_slice(&bytes[..length]);
        input.consume(length);
        if complete {
            return Ok(Some(std::mem::take(partial)));
        }
    }
}

async fn run() -> Result<(), ()> {
    let mut input = BufReader::new(tokio::io::stdin());
    // This buffer outlives a select! branch: another completed exchange may
    // cancel the read future after it has consumed only a command prefix.
    let mut partial = Vec::new();
    let Some(first) = line(&mut input, &mut partial).await? else {
        return Ok(());
    };
    let Command::Configure {
        version,
        origin,
        certificates,
    } = serde_json::from_slice(&first).map_err(|_| ())?
    else {
        return Err(());
    };
    let mut config = Configuration::new(version, &origin, certificates)?;
    let (output, mut events) = mpsc::channel::<Value>(16);
    let writer = tokio::spawn(async move {
        let mut stdout = tokio::io::stdout();
        while let Some(event) = events.recv().await {
            let mut raw = serde_json::to_vec(&event).map_err(|_| ())?;
            raw.push(b'\n');
            stdout.write_all(&raw).await.map_err(|_| ())?;
            stdout.flush().await.map_err(|_| ())?;
        }
        Ok::<_, ()>(())
    });
    output
        .send(json!({"type": "ready", "version": 1}))
        .await
        .map_err(|_| ())?;
    let mut tasks = JoinSet::new();
    let mut active: HashMap<u64, (AbortHandle, Arc<Semaphore>)> = HashMap::new();
    loop {
        tokio::select! {
            result = tasks.join_next(), if !tasks.is_empty() => {
                if let Some(Ok(id)) = result { active.remove(&id); }
            }
            raw = line(&mut input, &mut partial) => {
                let Some(raw) = raw? else { break; };
                match serde_json::from_slice::<Command>(&raw).map_err(|_| ())? {
                    Command::Cancel { id } => {
                        if let Some((task, _)) = active.remove(&id) { task.abort(); }
                    }
                    Command::Credit { id } => {
                        if let Some((_, credit)) = active.get(&id) {
                            if credit.available_permits() < WINDOW { credit.add_permits(1); }
                        }
                    }
                    Command::Request { id, url, method, body, headers, proxy, timeout } => {
                        if active.len() >= MAX_ACTIVE || active.contains_key(&id) {
                            error(&output, id, "protocol").await; continue;
                        }
                        let prepared = prepare(&url, &method, &body, headers, &config);
                        let client = config.client(&timeout, proxy);
                        let (Ok(request), Ok(client)) = (prepared, client) else {
                            error(&output, id, "protocol").await; continue;
                        };
                        let credits = Arc::new(Semaphore::new(WINDOW));
                        let sender = output.clone();
                        let credit = credits.clone();
                        let task = tasks.spawn(async move {
                            exchange(client, id, request, sender, credit).await;
                            id
                        });
                        active.insert(id, (task, credits));
                    }
                    Command::Configure { .. } => return Err(()),
                }
            }
            _ = output.closed() => return Err(()),
        }
    }
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
    drop(output);
    writer.await.map_err(|_| ())??;
    Ok(())
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    if run().await.is_err() {
        eprintln!("pseudolife-http: private transport protocol failed.");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origin_and_protocol_are_pinned_before_requests() {
        for origin in [
            "http://fixture:secret@example.com",
            "file:///tmp/bank",
            "https://example.com/path",
            "https://example.com?secret=value",
        ] {
            assert!(Configuration::new(1, origin, None).is_err());
        }
        assert!(Configuration::new(2, "https://example.com", None).is_err());
        let config = Configuration::new(1, "https://example.com", None).unwrap();
        assert!(prepare("https://other.example.com/mcp", "GET", "", vec![], &config).is_err());
        assert!(prepare("https://example.com/mcp", "POST", "", vec![], &config).is_ok());
    }

    #[tokio::test]
    async fn framing_rejects_truncated_commands() {
        let mut input = BufReader::new(&b"{\"type\":\"cancel\",\"id\":1}"[..]);
        let mut partial = Vec::new();
        assert!(line(&mut input, &mut partial).await.is_err());
        let mut input = BufReader::new(&b"one\ntwo\n"[..]);
        partial.clear();
        assert_eq!(
            line(&mut input, &mut partial).await.unwrap().unwrap(),
            b"one\n"
        );
        assert_eq!(
            line(&mut input, &mut partial).await.unwrap().unwrap(),
            b"two\n"
        );
        assert!(line(&mut input, &mut partial).await.unwrap().is_none());
    }
}
