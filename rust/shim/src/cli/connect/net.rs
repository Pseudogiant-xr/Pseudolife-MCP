//! Connect's daemon traffic: the unauthenticated health probe, the
//! installers' credential check, the board check-in probe and the MCP
//! handshake through this candidate's own shim.
use super::pyjson::{self, J};
use super::{Cred, Defer};
use std::time::Duration;

/// `shim.probe_health(url, 2.0)`: any HTTP answer's body as JSON, else None.
/// Redirects are followed, as urllib's default opener follows them.
pub(super) async fn probe_health(url: &str) -> Result<Option<J>, Defer> {
    let Ok(client) = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
    else {
        return Ok(None);
    };
    let Ok(response) = client.get(format!("{url}/health")).send().await else {
        return Ok(None);
    };
    let Ok(body) = response.bytes().await else {
        return Ok(None);
    };
    let Ok(text) = std::str::from_utf8(&body) else {
        return Ok(None);
    };
    pyjson::loads(text)
}

fn no_redirect(timeout: u64) -> Option<reqwest::Client> {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(timeout))
        .build()
        .ok()
}

/// The `Authorization` value urllib would send: `http.client` encodes a
/// header as latin-1 and refuses CR or LF not followed by folding
/// whitespace; either refusal means no request at all (`None`).
pub(super) fn bearer(token: &str) -> Option<reqwest::header::HeaderValue> {
    let mut value = b"Bearer ".to_vec();
    for c in token.chars() {
        value.push(u8::try_from(u32::from(c)).ok()?);
    }
    let illegal = value.iter().enumerate().any(|(index, byte)| {
        let next = value.get(index + 1);
        match byte {
            b'\n' => !matches!(next, Some(b' ' | b'\t')),
            b'\r' => !matches!(next, Some(b' ' | b'\t' | b'\n')),
            _ => false,
        }
    });
    if illegal {
        return None;
    }
    reqwest::header::HeaderValue::from_bytes(&value).ok()
}

/// `codex_connection.installer_credential_valid`.
pub(super) async fn credential_valid(url: &str, token: &str) -> bool {
    let (Some(client), Some(header)) = (no_redirect(3), bearer(token)) else {
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
    if !response.status().is_success() {
        return false;
    }
    let ok = response.status().as_u16() == 200;
    response.chunk().await.is_ok() && ok
}

const REASONS: [(&str, &str); 6] = [
    (
        "disabled",
        "coordination is disabled in the daemon's config.yaml (coordination.enabled)",
    ),
    (
        "authentication_required",
        "the daemon has no bearer token configured; set PSEUDOLIFE_MCP_TOKEN in ops/.env and redeploy",
    ),
    (
        "unauthorized",
        "the daemon rejected this token; re-run the installer so the client token file matches ops/.env",
    ),
    (
        "principal_not_allowed",
        "this token's principal is not in coordination.allowed_principals; list it there, or invite this machine with `pseudolife-mcp invite <machine>` on the daemon host",
    ),
    (
        "coordination_requires_postgres",
        "the board needs the PostgreSQL bank",
    ),
    (
        "principals_unavailable",
        "the daemon cannot check invited machines' tokens right now (its database is not answering); try again shortly",
    ),
];

fn latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|b| char::from(*b)).collect()
}

/// `board_status.board_status(url, token)[1]`.
pub(super) async fn board_line(url: &str, token: Option<&str>) -> String {
    let Some(token) = token.filter(|t| !t.is_empty()) else {
        return "off - no bearer token, so the daemon has no principal to admit (open-loopback install)"
            .to_owned();
    };
    let unreachable = || "off - daemon unreachable".to_owned();
    // urllib's refusal to put the header is one more exception: unreachable.
    let (Some(client), Some(header)) = (no_redirect(2), bearer(token)) else {
        return unreachable();
    };
    let Ok(mut response) = client
        .get(format!(
            "{}/api/hook/coordination-start",
            url.trim_end_matches('/')
        ))
        .header(reqwest::header::AUTHORIZATION, header)
        .send()
        .await
    else {
        return unreachable();
    };
    let status = response.status().as_u16();
    if !(200..300).contains(&status) {
        return format!("off - board probe refused (HTTP {status})");
    }
    let header = response
        .headers()
        .get("X-PL-Board")
        .map(|value| latin1(value.as_bytes()))
        .unwrap_or_default();
    let header = pyjson::py_strip(&header).to_owned();
    let mut body = Vec::new();
    loop {
        match response.chunk().await {
            Ok(Some(chunk)) => {
                body.extend_from_slice(&chunk);
                if body.len() >= 65536 {
                    body.truncate(65536);
                    break;
                }
            }
            Ok(None) => break,
            Err(_) => return unreachable(),
        }
    }
    // bytes.strip(): ASCII whitespace including \x0b, which trim_ascii keeps.
    let served = body
        .iter()
        .any(|byte| !matches!(byte, b' ' | b'\t' | b'\n' | b'\r' | b'\x0b' | b'\x0c'));
    if header == "on" || (header.is_empty() && served) {
        return "on - token present, principal allowed".to_owned();
    }
    let reason = header.split_once("reason=").map_or("", |(_, r)| r);
    let reason = pyjson::py_strip(reason);
    if let Some((_, line)) = REASONS.iter().find(|(name, _)| *name == reason) {
        return format!("off - {line}");
    }
    if !reason.is_empty() {
        return "off - the daemon refused the board (unrecognized reason)".to_owned();
    }
    "off - the daemon does not serve the board to this token; update the daemon to see why"
        .to_owned()
}

const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(20);

/// `connect_cli.subprocess_handshake`: initialize + tools/list through this
/// candidate's own shim (as the oracle runs its own interpreter's shim) in a
/// child whose environment names only the target.
pub(super) async fn handshake(url: &str, credential: &Cred) -> Result<usize, String> {
    use rmcp::{ServiceExt, transport::async_rw::AsyncRwTransport};
    let exited = || "the handshake exited 1".to_owned();
    let Ok(exe) = std::env::current_exe() else {
        return Err("OSError".to_owned());
    };
    let mut command = tokio::process::Command::new(exe);
    for key in [
        super::URL_KEY,
        super::FILE_KEY,
        super::TOKEN_KEY,
        "PSEUDOLIFE_MCP_TOKENS",
        super::STATE_KEY,
    ] {
        command.env_remove(key);
    }
    command
        .env(super::URL_KEY, url)
        .env(super::NO_SPAWN_KEY, "1")
        .env("PSEUDOLIFE_AGENT_COORDINATION", "0")
        .env("PYTHONIOENCODING", "utf-8");
    match credential {
        Cred::File(path) => {
            let resolved = crate::credentials::absolute_expanded(std::path::Path::new(path))
                .map_err(|_| exited())?;
            command.env(super::FILE_KEY, resolved);
        }
        Cred::Literal(token) => {
            command.env(super::TOKEN_KEY, token);
        }
        Cred::None => {}
    }
    command
        .current_dir(std::env::temp_dir())
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(0x0800_0000);
    let Ok(mut child) = command.spawn() else {
        return Err("OSError".to_owned());
    };
    let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
        return Err(exited());
    };
    let session = async {
        let client = ().serve(AsyncRwTransport::new_client(stdout, stdin)).await.ok()?;
        let instructions = client
            .peer_info()
            .and_then(|info| info.instructions.clone())
            .is_some_and(|text| !text.is_empty());
        let tools = client.list_tools(None).await.ok().map(|r| r.tools.len());
        let _ = client.cancel().await;
        Some((instructions, tools?))
    };
    let answer = tokio::time::timeout(HANDSHAKE_TIMEOUT, session).await;
    // The shim sees its stdin close; give it the time the MCP client's own
    // shutdown gives a server before it is stopped.
    if tokio::time::timeout(Duration::from_secs(2), child.wait())
        .await
        .is_err()
    {
        let _ = child.kill().await;
    }
    match answer {
        Ok(Some((true, count))) if count > 0 => Ok(count),
        Ok(Some(_)) => Err("the daemon listed no tools".to_owned()),
        _ => Err(exited()),
    }
}
