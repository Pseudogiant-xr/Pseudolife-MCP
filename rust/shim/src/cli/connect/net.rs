//! Connect's daemon traffic: the unauthenticated health probe, the
//! installers' credential check, the board check-in probe and the MCP
//! handshake through this candidate's own shim. The first three are GETs as
//! `urllib.request` sends them (`cli::hook_http`): urllib's request fields
//! (Host as written, `User-Agent`, `Accept-Encoding: identity`,
//! `Connection: close`), per-receive socket timeouts and
//! `HTTPRedirectHandler`'s limits.
use super::pyjson::{self, J};
use super::{Cred, Refusal};
use crate::cli::hook_http;
use crate::setup::codex::{CredentialCheck, ProbeFailure, wait_for_credential};
use reqwest::StatusCode;
use std::time::Duration;

/// `shim.probe_health(url, 2.0)`: `urlopen(url + "/health")`, any HTTP
/// answer's body as JSON, else None. Redirects are followed as the default
/// opener follows them; a 3xx reply here is where its redirect handler
/// raised `HTTPError` (no target, a refused scheme, the repeat limits), whose
/// body `e.read()` parses like any other.
pub(super) async fn probe_health(url: &str) -> Result<Option<J>, Refusal> {
    let Some(reply) = hook_http::get_bounded(
        url,
        "/health",
        None,
        Duration::from_secs(2),
        true,
        (None, None),
    )
    .await
    else {
        return Ok(None);
    };
    if reply.unsupported {
        return Err(Refusal("the daemon /health response redirects to an unsupported URL; use its direct http(s) daemon origin".into()));
    }
    if reply.body_failed {
        return Ok(None);
    }
    let Ok(text) = std::str::from_utf8(&reply.body) else {
        return Ok(None);
    };
    pyjson::loads(text).map_err(|cause| Refusal(format!("the daemon /health response is unsupported: {}; repair the daemon or reverse proxy's JSON response", cause.0)))
}

/// The `Authorization` value urllib would send: `http.client` encodes a
/// header as latin-1 and refuses CR or LF not followed by folding
/// whitespace; either refusal means no request at all (`None`).
pub(super) fn bearer(token: &str) -> Option<Vec<u8>> {
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
    Some(value)
}

fn credential_refused(url: &str, shown: &str) -> String {
    format!(
        "the daemon at {url} refused {shown} (an authenticated request that follows no redirects did not succeed)"
    )
}

/// The authenticated read uses the installer's startup budget. Preserve
/// connect's request headers and the one-byte read on a successful response.
pub(super) async fn verify_credential(url: &str, shown: &str, token: &str) -> Result<(), String> {
    let header = bearer(token).ok_or_else(|| credential_refused(url, shown))?;
    verify_credential_with(url, shown, async |timeout| {
        let reply = hook_http::get_bounded_no_redirect(
            url,
            "/api/episodes?limit=1",
            &header,
            timeout,
            (Some(1), Some(0)),
        )
        .await
        .map_err(|error| ProbeFailure::from_io(error.io_kind()))?;
        credential_status(reply)
    })
    .await
}

fn credential_status(reply: hook_http::Reply) -> Result<StatusCode, ProbeFailure> {
    if reply.body_failed {
        // Once response headers arrived, only a known transport error
        // should describe this as a connection problem.
        return Err(match ProbeFailure::from_io(reply.body_io_error) {
            ProbeFailure::ConnectionFailed => ProbeFailure::UnreadableReply,
            failure => failure,
        });
    }
    StatusCode::from_u16(reply.status).map_err(|_| ProbeFailure::UnreadableReply)
}

async fn verify_credential_with(
    url: &str,
    shown: &str,
    probe: impl AsyncFnMut(Duration) -> Result<StatusCode, ProbeFailure>,
) -> Result<(), String> {
    let reason = match wait_for_credential(probe).await {
        CredentialCheck::Accepted => return Ok(()),
        CredentialCheck::Refused => credential_refused(url, shown),
        CredentialCheck::NotReady(failure) => format!(
            "{} Check that the daemon is reachable, then retry",
            CredentialCheck::not_ready_reason(url, failure)
        ),
        CredentialCheck::UnexpectedStatus(status) => format!(
            "the daemon at {url} returned HTTP {} to the authenticated check with {shown}. Check that the address serves the Pseudolife daemon and retry",
            status.as_u16()
        ),
    };
    Err(reason)
}

#[cfg(test)]
mod credential_tests {
    use super::*;
    use tokio::time::Instant;

    const URL: &str = "https://daemon.example.com";

    #[tokio::test(start_paused = true)]
    async fn credential_verification_reports_exhausted_http_retry_and_says_to_retry() {
        let started = Instant::now();
        let error = verify_credential_with(URL, "test credential file", async |_| {
            Ok(StatusCode::SERVICE_UNAVAILABLE)
        })
        .await
        .unwrap_err();
        assert_eq!(started.elapsed(), Duration::from_secs(180));
        assert_eq!(
            error,
            "The authenticated check at https://daemon.example.com did not succeed within 180 seconds. The last attempt returned HTTP 503. Check that the daemon is reachable, then retry"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn credential_verification_returns_redirect_and_404_without_retry() {
        for status in [StatusCode::FOUND, StatusCode::NOT_FOUND] {
            let mut attempts = 0;
            let started = Instant::now();
            let error = verify_credential_with(URL, "test credential file", async |_| {
                attempts += 1;
                Ok(status)
            })
            .await
            .unwrap_err();
            assert_eq!(attempts, 1);
            assert_eq!(started.elapsed(), Duration::ZERO);
            assert!(error.contains(&format!("returned HTTP {}", status.as_u16())));
            assert!(error.contains("Check that the address serves the Pseudolife daemon"));
        }
    }

    #[tokio::test(start_paused = true)]
    async fn credential_verification_names_an_unreadable_response() {
        use std::io::ErrorKind;

        for (body_io_error, message) in [
            (
                None,
                "The last attempt received an unreadable HTTP response.",
            ),
            (Some(ErrorKind::TimedOut), "The last attempt timed out."),
            (
                Some(ErrorKind::ConnectionReset),
                "The last connection was refused or reset.",
            ),
        ] {
            let error = verify_credential_with(URL, "test credential file", async |_| {
                credential_status(hook_http::Reply {
                    status: 200,
                    body: vec![],
                    headers: http::HeaderMap::new(),
                    body_failed: true,
                    body_io_error,
                    unsupported: false,
                })
            })
            .await
            .unwrap_err();
            assert!(error.contains(message), "{error}");
            assert!(!error.contains("could not establish a connection"));
        }
    }
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
        "the daemon rejected this token; on the daemon's own host, re-run the installer so the client token file matches ops/.env; on another machine, run `pseudolife-mcp invite <name> --replace` on the daemon host, then `pseudolife-mcp connect <url> --read-code` here",
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

/// `board_status.board_status(url, token)[1]`: a no-redirect opener's GET,
/// `read(65536)` of a 2xx body; any other status is an `HTTPError` whose
/// body is never read.
pub(in crate::cli) async fn board_line(url: &str, token: Option<&str>) -> String {
    let Some(token) = token.filter(|t| !t.is_empty()) else {
        return "off - no bearer token, so the daemon has no principal to admit (open-loopback install)"
            .to_owned();
    };
    let unreachable = || "off - daemon unreachable".to_owned();
    // urllib's refusal to put the header is one more exception: unreachable.
    let Some(header) = bearer(token) else {
        return unreachable();
    };
    let Some(reply) = hook_http::get_bounded(
        url.trim_end_matches('/'),
        "/api/hook/coordination-start",
        Some(&header),
        Duration::from_secs(2),
        false,
        (Some(65536), Some(0)),
    )
    .await
    else {
        return unreachable();
    };
    let status = reply.status;
    if !(200..300).contains(&status) {
        return format!("off - board probe refused (HTTP {status})");
    }
    // read(65536) failing inside the `with`: the probe's `except Exception`.
    if reply.body_failed {
        return unreachable();
    }
    let header = reply
        .headers
        .get("X-PL-Board")
        .map(|value| latin1(value.as_bytes()))
        .unwrap_or_default();
    let header = pyjson::py_strip(&header).to_owned();
    // bytes.strip(): ASCII whitespace including \x0b, which trim_ascii keeps.
    let served = reply
        .body
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
