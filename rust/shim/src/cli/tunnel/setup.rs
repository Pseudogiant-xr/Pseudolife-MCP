//! Save a fresh profile for an explicitly selected daemon connection.
use std::{
    collections::HashMap,
    fs,
    io::{self, Write},
    path::Path,
    sync::Arc,
    time::Duration,
};

use futures::{StreamExt, TryStreamExt, stream::BoxStream};
use reqwest::header::{ACCEPT, AUTHORIZATION, CONTENT_TYPE, HeaderMap, HeaderName, HeaderValue};
use rmcp::{
    ServiceExt,
    model::ClientJsonRpcMessage,
    transport::{
        StreamableHttpClientTransport,
        streamable_http_client::{
            SseError, StreamableHttpClient, StreamableHttpClientTransportConfig,
            StreamableHttpError, StreamableHttpPostResponse,
        },
    },
};
use serde_json::{Map, Value};
use sse_stream::{Sse, SseStream};

use super::{Fail, Options, Output, record, store};
use crate::{
    board::state,
    credentials::{CredentialProvider, CredentialSnapshot},
};

mod resume;
pub(super) use resume::resume;
mod renewal;
pub(super) use renewal::{
    check_pending, finish_previous_key, qualify_previous_key, recover_pending,
    validate_previous_key,
};

#[cfg(test)]
pub(super) fn seed_trial(
    root: &Path,
    name: &str,
    old: (&[u8], &[u8]),
    replacement: (&[u8], &[u8]),
) -> Result<(), Fail> {
    renewal::publish(root, name, old, replacement)
}

const ACCESS: &str = "This tunnel makes the daemon available to the selected OpenAI organization and associated ChatGPT workspace. The host and daemon must stay running for tunnel access.";
const FULL_CATALOG: &str = "The full catalog also expands tool discovery for the daemon principal's other sessions. Authentication permissions stay the same.";
const CONSENT: &str = "This tunnel makes the daemon available to the selected OpenAI organization and associated ChatGPT workspace. The host and daemon must stay running for tunnel access. Rerun with --accept-access to consent to that access.";
const FULL_CONSENT: &str = "This tunnel makes the daemon available to the selected OpenAI organization and associated ChatGPT workspace. The host and daemon must stay running for tunnel access. The full catalog also expands tool discovery for the daemon principal's other sessions. Authentication permissions stay the same. Rerun with --accept-access to consent to that access.";
const CREDENTIAL: &str =
    "daemon credential file is unavailable or unsafe; repair its owner-only access and retry setup";
const HANDSHAKE: &str = "existing daemon MCP handshake failed; check the URL and credential with pseudolife-mcp doctor, then retry setup";
const CONFLICT: &str = "a tunnel file appeared at this name during setup; check tunnel status and rerun setup with the intended profile";
const DIRECTORY: &str = "cannot use the tunnel directory; pass --profile-dir pointing to a plain directory your account owns, then retry";
const WRITE: &str = "could not save the private tunnel file; check the tunnel directory permissions and retry setup";
const CLEANUP: &str = "could not remove the private tunnel staging file";
#[cfg(unix)]
const UNSUPPORTED_MOVE: &str = "the filesystem refused an atomic no-replace move for the tunnel file; use --profile-dir on a filesystem that supports it";
#[cfg(unix)]
const SYNC: &str = "the tunnel file was saved but its directory could not be synced; check the filesystem and run tunnel status";
const DEADLINE: Duration = Duration::from_secs(20);
const PROFILE_LIMIT: usize = 65536;
const RESPONSE_LIMIT: usize = 4 * 1024 * 1024;
const STAGE_PREFIX: &str = ".tunnel-setup-";

pub(super) fn save(
    options: &Options,
    root: &Path,
    path: &Path,
    mut payload: Map<String, Value>,
) -> Output {
    let name = &options.profile;
    let state = if payload.contains_key("tunnel_id") {
        "ready"
    } else {
        "pending"
    };
    payload.insert("state".into(), Value::String(state.to_owned()));
    payload.insert("consent".into(), Value::Bool(true));
    if store::occupied(path)? {
        return Err(Fail::Tunnel(CONFLICT));
    }
    let profile = store::validate(&payload)?;
    let bytes = profile_bytes(&payload)?;
    check_directory(root)?;
    if !options.setup.accept_access {
        return Err(consent_required(&profile));
    }
    check_unused_name(root, name)?;
    verify_connection(&profile)?;
    if disclose(&profile).is_err() {
        return Ok((String::new(), super::STDOUT_REFUSED));
    }
    prepare_directory(root)?;
    check_unused_name(root, name)?;
    publish_new(path, |file| file.write_all(&bytes))?;
    Ok(saved_message(options, root, &profile))
}

fn verify_connection(profile: &store::Profile) -> Result<CredentialSnapshot, Fail> {
    let provider = CredentialProvider::new(None, Some((&profile.token_file).into()))
        .map_err(|_| Fail::Tunnel(CREDENTIAL))?;
    let snapshot = provider.snapshot().map_err(|_| Fail::Tunnel(CREDENTIAL))?;
    let token = snapshot.token().ok_or(Fail::Tunnel(CREDENTIAL))?;
    run_handshake(handshake(&profile.daemon_url, token), DEADLINE)?;
    provider.require_current(&snapshot).map_err(|_| {
        Fail::Tunnel(
            "daemon credential changed during setup; retry with the current credential file",
        )
    })?;
    Ok(snapshot)
}

fn profile_bytes(payload: &Map<String, Value>) -> Result<Vec<u8>, Fail> {
    let bytes = serde_json::to_vec(payload).map_err(|_| Fail::Tunnel(WRITE))?;
    if bytes.len() > PROFILE_LIMIT {
        return Err(Fail::Tunnel(
            "the tunnel profile would exceed 64 KiB; shorten the setup values and retry",
        ));
    }
    Ok(bytes)
}

fn consent_required(profile: &store::Profile) -> Fail {
    Fail::Tunnel(if profile.catalog == "full" {
        FULL_CONSENT
    } else {
        CONSENT
    })
}

fn disclose(profile: &store::Profile) -> io::Result<()> {
    let notice = if profile.catalog == "full" {
        format!("{ACCESS}\n{FULL_CATALOG}\n")
    } else {
        format!("{ACCESS}\n")
    };
    let mut stdout = io::stdout().lock();
    stdout.write_all(&super::super::text_bytes(&notice))?;
    stdout.flush()
}

fn saved_message(options: &Options, root: &Path, profile: &store::Profile) -> (String, u8) {
    let name = &options.profile;
    let directory = options.profile_dir.as_ref().map_or_else(String::new, |_| {
        format!(
            " --profile-dir {}",
            crate::cli::update::seams::quote(&root.display().to_string())
        )
    });
    let (next, flag) = if profile.tunnel_id.is_some() {
        (
            "Add the private runtime key for the same organization before starting the tunnel.",
            "--read-key",
        )
    } else {
        (
            "Choose a tunnel in the intended organization at https://platform.openai.com/settings/organization/tunnels.",
            "--tunnel-id <tunnel_id>",
        )
    };
    (
        format!(
            "Profile {name} saved. {next}\nResume: pseudolife-mcp tunnel setup --profile {name}{directory} {flag}\n"
        ),
        0,
    )
}

fn fill_profile_defaults(payload: &mut Map<String, Value>) {
    for (key, default) in [
        ("tunnel_id", Value::Null),
        ("organization_id", Value::Null),
        ("state", Value::from("pending")),
        ("consent", Value::Bool(false)),
        ("autostart_consent", Value::Bool(false)),
        ("runtime_version", Value::Null),
        ("minimum_catalog", Value::from("current")),
        ("runtime_key_expires_at", Value::Null),
        ("cloud_verification_file", Value::Null),
    ] {
        payload.entry(key).or_insert(default);
    }
}

fn run_handshake(probe: impl Future<Output = bool>, deadline: Duration) -> Result<(), Fail> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| Fail::Tunnel(HANDSHAKE))?;
    if matches!(
        runtime.block_on(async { tokio::time::timeout(deadline, probe).await }),
        Ok(true)
    ) {
        return Ok(());
    }
    // DNS resolution can still be in a blocking worker when the async
    // deadline expires. Dropping the runtime would wait for that worker.
    runtime.shutdown_background();
    Err(Fail::Tunnel(HANDSHAKE))
}

async fn handshake(url: &str, token: &str) -> bool {
    let Ok(mut auth) = HeaderValue::from_str(&format!("Bearer {token}")) else {
        return false;
    };
    auth.set_sensitive(true);
    let Ok(client) = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .no_proxy()
        .default_headers(HeaderMap::from_iter([(AUTHORIZATION, auth)]))
        .connect_timeout(Duration::from_secs(5))
        .timeout(DEADLINE)
        .build()
    else {
        return false;
    };
    let endpoint = format!("{}/mcp", url.trim_end_matches('/'));
    let transport = StreamableHttpClientTransport::with_client(
        ProbeHttp(client),
        StreamableHttpClientTransportConfig::with_uri(endpoint).reinit_on_expired_session(false),
    );
    let Ok(client) = ().serve(transport).await else {
        return false;
    };
    let has_instructions = client.peer_info().is_some_and(|info| {
        info.instructions
            .as_deref()
            .is_some_and(|instructions| !instructions.is_empty())
    });
    let has_tools = client
        .list_tools(None)
        .await
        .is_ok_and(|list| !list.tools.is_empty());
    let _ = client.cancel().await;
    has_instructions && has_tools
}

/// Setup needs two finite MCP answers. Cap the HTTP body before the SDK
/// parses either JSON or SSE, including chunked responses without a length.
#[derive(Clone)]
struct ProbeHttp(reqwest::Client);

type TransportError = StreamableHttpError<reqwest::Error>;

impl StreamableHttpClient for ProbeHttp {
    type Error = reqwest::Error;

    async fn post_message(
        &self,
        uri: Arc<str>,
        message: ClientJsonRpcMessage,
        session: Option<Arc<str>>,
        auth: Option<String>,
        headers: HashMap<HeaderName, HeaderValue>,
    ) -> Result<StreamableHttpPostResponse, TransportError> {
        let mut request = self
            .0
            .post(uri.as_ref())
            .header(ACCEPT, "application/json, text/event-stream")
            .headers(headers.into_iter().collect())
            .json(&message);
        if let Some(session) = session {
            request = request.header("Mcp-Session-Id", session.as_ref());
        }
        if let Some(auth) = auth {
            request = request.bearer_auth(auth);
        }
        let response = request.send().await.map_err(TransportError::Client)?;
        if !response.status().is_success() {
            return Err(TransportError::UnexpectedServerResponse(
                "MCP request refused".into(),
            ));
        }
        if !matches!(message, ClientJsonRpcMessage::Request(_))
            || matches!(response.status().as_u16(), 202 | 204)
        {
            return Ok(StreamableHttpPostResponse::Accepted);
        }
        let session_id = response
            .headers()
            .get("Mcp-Session-Id")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        let content_type = response
            .headers()
            .get(CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .to_owned();
        let mut remaining = RESPONSE_LIMIT;
        let chunks = response.bytes_stream().map(move |chunk| {
            let chunk = chunk.map_err(io::Error::other)?;
            remaining = remaining
                .checked_sub(chunk.len())
                .ok_or_else(|| io::Error::other("MCP response is too large"))?;
            Ok(chunk)
        });
        match content_type.split(';').next().unwrap_or_default().trim() {
            "application/json" => {
                let body = chunks
                    .try_fold(Vec::new(), |mut body, chunk| async move {
                        body.extend_from_slice(&chunk);
                        Ok(body)
                    })
                    .await
                    .map_err(|_: io::Error| {
                        TransportError::UnexpectedServerResponse(
                            "could not read the bounded MCP response".into(),
                        )
                    })?;
                Ok(StreamableHttpPostResponse::Json(
                    serde_json::from_slice(&body)?,
                    session_id,
                ))
            }
            "text/event-stream" => Ok(StreamableHttpPostResponse::Sse(
                SseStream::from_bytes_stream(chunks).boxed(),
                session_id,
            )),
            _ => Err(TransportError::UnexpectedContentType(None)),
        }
    }

    async fn delete_session(
        &self,
        uri: Arc<str>,
        session: Arc<str>,
        auth: Option<String>,
        headers: HashMap<HeaderName, HeaderValue>,
    ) -> Result<(), TransportError> {
        self.0.delete_session(uri, session, auth, headers).await
    }

    async fn get_stream(
        &self,
        _uri: Arc<str>,
        _session: Option<Arc<str>>,
        _last_event: Option<String>,
        _auth: Option<String>,
        _headers: HashMap<HeaderName, HeaderValue>,
    ) -> Result<BoxStream<'static, Result<Sse, SseError>>, TransportError> {
        // POST carries the answers. Setup does not subscribe to notifications.
        Err(TransportError::ServerDoesNotSupportSse)
    }
}

fn check_unused_name(root: &Path, name: &str) -> Result<(), Fail> {
    for path in [store::key_path(root, name), record::path(root, name)] {
        if store::occupied(&path)? {
            return Err(Fail::Detail(format!(
                "{} exists without a profile; restore its matching profile at {} from a backup before retrying",
                path.display(),
                store::profile_file(root, name).display()
            )));
        }
    }
    Ok(())
}

pub(super) fn check_directory(root: &Path) -> Result<(), Fail> {
    store::checked_path(root).map_err(|_| Fail::Tunnel(DIRECTORY))?;
    match fs::symlink_metadata(root) {
        Ok(info) if info.is_dir() => {
            store::owner_only_directory(root, &info).map_err(|_| Fail::Tunnel(DIRECTORY))
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        _ => Err(Fail::Tunnel(DIRECTORY)),
    }
}

/// An interrupted writer can leave a stage behind. List only the names of
/// regular files that match the publisher's naming scheme. A second setup
/// could still be writing one, so this advisory never removes a stage.
pub(super) fn warn_for_stages(root: &Path) {
    if check_directory(root).is_err() {
        return;
    }
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    let mut stages: Vec<_> = entries
        .filter_map(Result::ok)
        .filter(|entry| {
            entry
                .file_name()
                .to_str()
                .and_then(|name| name.strip_prefix(STAGE_PREFIX))
                .is_some_and(|id| id.len() == 32 && id.bytes().all(|byte| byte.is_ascii_hexdigit()))
                && entry.file_type().is_ok_and(|kind| kind.is_file())
        })
        .map(|entry| entry.path())
        .collect();
    stages.sort();
    for stage in stages {
        crate::stderrln!(
            "Unpublished tunnel stage {}. If no tunnel setup is running, remove this file before retrying.",
            stage.display()
        );
    }
}

fn prepare_directory(root: &Path) -> Result<(), Fail> {
    check_directory(root)?;
    if !store::occupied(root)? {
        let parent = root.parent().ok_or(Fail::Tunnel(DIRECTORY))?;
        if !store::occupied(parent)? {
            state::private_dir(parent).map_err(|_| Fail::Tunnel(DIRECTORY))?;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            if fs::DirBuilder::new()
                .mode(0o700)
                .create(root)
                .is_err_and(|error| error.kind() != io::ErrorKind::AlreadyExists)
            {
                return Err(Fail::Tunnel(DIRECTORY));
            }
        }
        // The Windows helper also sets ownership to the current user. The
        // token's default owner can otherwise be the Administrators group.
        #[cfg(windows)]
        state::private_dir(root).map_err(|_| Fail::Tunnel(DIRECTORY))?;
    }
    check_directory(root)
}

pub(super) fn publish_new(
    path: &Path,
    write: impl FnOnce(&mut fs::File) -> io::Result<()>,
) -> Result<(), Fail> {
    stage_and_publish(path, write, |stage| {
        rename_new(stage, path).map_err(publication_error)
    })
}

fn stage_and_publish(
    path: &Path,
    write: impl FnOnce(&mut fs::File) -> io::Result<()>,
    publish: impl FnOnce(&Path) -> Result<(), Fail>,
) -> Result<(), Fail> {
    let parent = path.parent().ok_or(Fail::Tunnel(DIRECTORY))?;
    let stage = parent.join(format!("{STAGE_PREFIX}{}", uuid::Uuid::new_v4().simple()));
    let result = (|| {
        let mut opened = state::create(&stage).map_err(|_| Fail::Tunnel(WRITE))?;
        write(&mut opened.file)
            .and_then(|()| opened.file.sync_all())
            .map_err(|_| Fail::Tunnel(WRITE))?;
        drop(opened);
        publish(&stage)?;
        sync_directory(parent)
    })();
    finish_publication(result, &stage, || fs::remove_file(&stage))
}

fn sync_directory(path: &Path) -> Result<(), Fail> {
    #[cfg(unix)]
    fs::File::open(path)
        .and_then(|dir| dir.sync_all())
        .map_err(|_| Fail::Tunnel(SYNC))?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

fn finish_publication(
    result: Result<(), Fail>,
    stage: &Path,
    remove: impl FnMut() -> io::Result<()>,
) -> Result<(), Fail> {
    match store::sharing_retry(remove) {
        Ok(()) => result,
        Err(error) if error.kind() == io::ErrorKind::NotFound => result,
        Err(_) => {
            let remedy = if cfg!(windows) {
                "close programs using that file, then remove it before retrying"
            } else {
                "check write access to its parent directory, then remove it before retrying"
            };
            Err(Fail::Detail(format!(
                "{CLEANUP} {}; {remedy}",
                stage.display()
            )))
        }
    }
}

fn publication_error(error: io::Error) -> Fail {
    Fail::Tunnel(match error.kind() {
        io::ErrorKind::AlreadyExists => CONFLICT,
        #[cfg(unix)]
        io::ErrorKind::InvalidInput | io::ErrorKind::Unsupported => UNSUPPORTED_MOVE,
        _ => WRITE,
    })
}

#[cfg(unix)]
fn rename_new(from: &Path, to: &Path) -> io::Result<()> {
    use rustix::fs::{CWD, RenameFlags, renameat_with};
    renameat_with(CWD, from, CWD, to, RenameFlags::NOREPLACE).map_err(Into::into)
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn rename_new(from: &Path, to: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::MoveFileW;

    let parent = fs::canonicalize(to.parent().ok_or(io::ErrorKind::InvalidInput)?)?;
    let wide = |path: &Path| -> io::Result<Vec<u16>> {
        let name = path.file_name().ok_or(io::ErrorKind::InvalidInput)?;
        Ok(parent
            .join(name)
            .as_os_str()
            .encode_wide()
            .chain([0])
            .collect())
    };
    let from = wide(from)?;
    let to = wide(to)?;
    // SAFETY: both NUL-terminated buffers live through the call. The source
    // and destination are siblings, and MoveFileW refuses replacement.
    store::sharing_retry(|| {
        if unsafe { MoveFileW(from.as_ptr(), to.as_ptr()) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_publication_preserves_a_competing_writer() {
        let root = std::env::temp_dir().join(format!("tunnel-publish-{}", uuid::Uuid::new_v4()));
        state::private_dir(&root).unwrap();
        let path = root.join("dot.profile.json");
        state::atomic_create(&path, b"competing profile").unwrap();
        let result = publish_new(&path, |file| file.write_all(b"new profile"));
        let contents = state::read(&path, 65536).unwrap();
        let entries = fs::read_dir(&root).unwrap().count();
        fs::remove_dir_all(root).unwrap();
        assert_eq!(result, Err(Fail::Tunnel(CONFLICT)));
        assert_eq!(contents, b"competing profile");
        assert_eq!(entries, 1);
    }

    #[test]
    fn profile_publication_cleans_up_after_a_partial_write_fails() {
        let root = std::env::temp_dir().join(format!("tunnel-publish-{}", uuid::Uuid::new_v4()));
        state::private_dir(&root).unwrap();
        let result = publish_new(&root.join("dot.profile.json"), |file| {
            file.write_all(b"partial fixture")?;
            Err(io::Error::other("fixture write failure"))
        });
        let entries = fs::read_dir(&root).unwrap().count();
        fs::remove_dir_all(root).unwrap();
        assert_eq!(result, Err(Fail::Tunnel(WRITE)));
        assert_eq!(entries, 0);
    }

    #[test]
    fn profile_publication_reports_if_staging_cleanup_fails() {
        let stage = Path::new("fixture/.tunnel-setup-dangling");
        let result = finish_publication(Err(Fail::Tunnel(WRITE)), stage, || {
            Err(io::Error::other("fixture cleanup failure"))
        });
        assert!(
            matches!(result, Err(Fail::Detail(message)) if message.contains(CLEANUP) && message.contains(&stage.display().to_string()))
        );
    }

    #[test]
    fn failed_replacement_names_the_leftover_stage_and_preserves_the_profile() {
        let root = std::env::temp_dir().join(format!("tunnel-publish-{}", uuid::Uuid::new_v4()));
        state::private_dir(&root).unwrap();
        let profile = root.join("dot.profile.json");
        state::atomic_create(&profile, b"saved fixture").unwrap();
        let mut residue = std::path::PathBuf::new();
        let result = stage_and_publish(
            &profile,
            |file| file.write_all(b"updated fixture"),
            |stage| {
                // Inject promotion failure and an entry that remove_file cannot unlink.
                fs::remove_file(stage).unwrap();
                fs::create_dir(stage).unwrap();
                residue = stage.to_owned();
                Err(Fail::Tunnel(WRITE))
            },
        );
        let saved = state::read(&profile, 65536).unwrap();
        fs::remove_dir_all(root).unwrap();
        assert_eq!(saved, b"saved fixture");
        assert!(
            matches!(result, Err(Fail::Detail(message)) if message.contains(CLEANUP) && message.contains(&residue.display().to_string()))
        );
    }

    #[cfg(windows)]
    #[test]
    fn profile_publication_retries_a_sharing_violation_during_cleanup() {
        let mut attempts = 0;
        let result =
            finish_publication(Err(Fail::Tunnel(WRITE)), Path::new("fixture-stage"), || {
                attempts += 1;
                if attempts == 1 {
                    Err(io::Error::from_raw_os_error(32))
                } else {
                    Ok(())
                }
            });
        assert_eq!(result, Err(Fail::Tunnel(WRITE)));
        assert_eq!(attempts, 2);
    }

    #[test]
    fn handshake_deadline_does_not_wait_for_a_blocking_worker() {
        use std::sync::mpsc;
        let (started, ready) = mpsc::channel();
        let (release, blocked) = mpsc::channel::<()>();
        let (done, result) = mpsc::channel();
        let worker = std::thread::spawn(move || {
            let output = run_handshake(
                async {
                    tokio::task::spawn_blocking(move || {
                        let _ = started.send(());
                        let _ = blocked.recv_timeout(Duration::from_secs(10));
                    });
                    std::future::pending().await
                },
                Duration::from_millis(25),
            );
            let _ = done.send(output);
        });
        let started = ready.recv_timeout(Duration::from_secs(3));
        let observed = result.recv_timeout(Duration::from_secs(2));
        let _ = release.send(());
        worker.join().unwrap();
        assert!(started.is_ok());
        assert_eq!(observed.unwrap(), Err(Fail::Tunnel(HANDSHAKE)));
    }

    #[cfg(unix)]
    #[test]
    fn setup_rejects_a_shared_directory_or_a_redirect() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let root = std::env::temp_dir().join(format!("tunnel-dir-{}", uuid::Uuid::new_v4()));
        let shared = root.join("shared");
        let private = root.join("private");
        let redirect = root.join("redirect");
        state::private_dir(&shared).unwrap();
        state::private_dir(&private).unwrap();
        fs::set_permissions(&shared, fs::Permissions::from_mode(0o755)).unwrap();
        symlink(&private, &redirect).unwrap();
        let shared_result = check_directory(&shared);
        let private_result = check_directory(&private);
        let redirect_result = check_directory(&redirect);
        fs::remove_dir_all(root).unwrap();
        assert_eq!(private_result, Ok(()));
        assert_eq!(shared_result, Err(Fail::Tunnel(DIRECTORY)));
        assert_eq!(redirect_result, Err(Fail::Tunnel(DIRECTORY)));
    }

    #[cfg(unix)]
    #[test]
    fn publication_names_the_atomic_move_refusal() {
        use rustix::io::Errno;
        for error in [Errno::INVAL, Errno::NOTSUP] {
            assert_eq!(
                publication_error(error.into()),
                Fail::Tunnel(UNSUPPORTED_MOVE)
            );
        }
        assert_eq!(
            publication_error(io::ErrorKind::PermissionDenied.into()),
            Fail::Tunnel(WRITE)
        );
    }
}
