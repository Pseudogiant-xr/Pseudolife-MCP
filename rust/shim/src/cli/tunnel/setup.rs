//! Save a fresh profile for an explicitly selected daemon connection.
use std::{
    fs,
    io::{self, Write},
    path::Path,
    sync::{Arc, Mutex},
    time::Duration,
};

use reqwest::header::{AUTHORIZATION, HeaderMap, HeaderValue};
use rmcp::{ServiceExt, transport::StreamableHttpClientTransport};
use serde_json::{Map, Value};

use super::{Fail, Options, Output, record, store};
use crate::{board::state, credentials::CredentialProvider, raw_http::ObservedHttp};

const CONSENT: &str = "This tunnel makes the daemon available to the selected OpenAI organization and associated ChatGPT workspace. Rerun with --accept-access to consent to that access.";
const CREDENTIAL: &str =
    "daemon credential file is unavailable or unsafe; repair its owner-only access and retry setup";
const HANDSHAKE: &str = "existing daemon MCP handshake failed; check the URL and credential with pseudolife-mcp doctor, then retry setup";
const CONFLICT: &str = "a different or unreadable tunnel profile already occupies this name; check tunnel status or choose another --profile";
const ORPHAN: &str = "this profile name has a saved key or process record without a profile; check tunnel status or choose another --profile";
const DIRECTORY: &str = "cannot use the tunnel directory; pass --profile-dir pointing to a plain directory your account owns, then retry";
const WRITE: &str =
    "could not save the tunnel profile; check the tunnel directory permissions and retry setup";
#[cfg(unix)]
const SYNC: &str = "the profile was saved but the tunnel directory could not be synced; check the filesystem and rerun setup to confirm the profile";
const DEADLINE: Duration = Duration::from_secs(20);
const PROFILE_LIMIT: usize = 65536;

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
    let profile = store::validate(&payload)?;
    let bytes = serde_json::to_vec(&payload).map_err(|_| Fail::Tunnel(WRITE))?;
    if bytes.len() > PROFILE_LIMIT {
        return Err(Fail::Tunnel(
            "the tunnel profile would exceed 64 KiB; shorten the setup values and retry",
        ));
    }
    check_directory(root)?;
    if store::occupied(path)? {
        let saved = store::private_read(path, PROFILE_LIMIT).map_err(|_| Fail::Tunnel(CONFLICT))?;
        if serde_json::from_slice::<Map<String, Value>>(&saved).is_ok_and(|saved| saved == payload)
        {
            return Ok((format!("Profile {name} is already configured.\n"), 0));
        }
        return Err(Fail::Tunnel(CONFLICT));
    }
    if !options.setup.accept_access {
        return Err(Fail::Tunnel(CONSENT));
    }
    check_unused_name(root, name)?;

    let provider = CredentialProvider::new(None, Some(profile.token_file.into()))
        .map_err(|_| Fail::Tunnel(CREDENTIAL))?;
    let snapshot = provider.snapshot().map_err(|_| Fail::Tunnel(CREDENTIAL))?;
    let token = snapshot.token().ok_or(Fail::Tunnel(CREDENTIAL))?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| Fail::Tunnel(HANDSHAKE))?;
    runtime.block_on(async {
        tokio::time::timeout(DEADLINE, handshake(&profile.daemon_url, token))
            .await
            .ok()
            .filter(|verified| *verified)
            .ok_or(Fail::Tunnel(HANDSHAKE))
    })?;
    provider.require_current(&snapshot).map_err(|_| {
        Fail::Tunnel(
            "daemon credential changed during setup; retry with the current credential file",
        )
    })?;

    prepare_directory(root)?;
    check_unused_name(root, name)?;
    publish_profile(path, &bytes)?;
    let next = if profile.tunnel_id.is_some() {
        "Add the private runtime key for the same organization before starting the tunnel."
    } else {
        "Choose a tunnel in the intended organization at https://platform.openai.com/settings/organization/tunnels."
    };
    Ok((format!("Profile {name} saved. {next}\n"), 0))
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
        ObservedHttp {
            client,
            observation: Arc::new(Mutex::new(Default::default())),
        },
        rmcp::transport::streamable_http_client::StreamableHttpClientTransportConfig::with_uri(
            endpoint,
        )
        .reinit_on_expired_session(false),
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

fn check_unused_name(root: &Path, name: &str) -> Result<(), Fail> {
    if store::occupied(&store::key_path(root, name))? || store::occupied(&record::path(root, name))?
    {
        return Err(Fail::Tunnel(ORPHAN));
    }
    Ok(())
}

fn check_directory(root: &Path) -> Result<(), Fail> {
    store::checked_path(root).map_err(|_| Fail::Tunnel(DIRECTORY))?;
    match fs::symlink_metadata(root) {
        Ok(info) if info.is_dir() => {
            store::owner_only_directory(root, &info).map_err(|_| Fail::Tunnel(DIRECTORY))
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        _ => Err(Fail::Tunnel(DIRECTORY)),
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

fn publish_profile(path: &Path, bytes: &[u8]) -> Result<(), Fail> {
    let parent = path.parent().ok_or(Fail::Tunnel(DIRECTORY))?;
    let stage = parent.join(format!(".tunnel-setup-{}", uuid::Uuid::new_v4().simple()));
    let result = (|| {
        let mut opened = state::create(&stage).map_err(|_| Fail::Tunnel(WRITE))?;
        opened
            .file
            .write_all(bytes)
            .and_then(|()| opened.file.sync_all())
            .map_err(|_| Fail::Tunnel(WRITE))?;
        drop(opened);
        rename_new(&stage, path).map_err(|error| {
            if error.kind() == io::ErrorKind::AlreadyExists {
                Fail::Tunnel(CONFLICT)
            } else {
                Fail::Tunnel(WRITE)
            }
        })?;
        #[cfg(unix)]
        fs::File::open(parent)
            .and_then(|dir| dir.sync_all())
            .map_err(|_| Fail::Tunnel(SYNC))?;
        Ok(())
    })();
    let _ = fs::remove_file(stage);
    result
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
    if unsafe { MoveFileW(from.as_ptr(), to.as_ptr()) } == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
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
        let result = publish_profile(&path, b"new profile");
        let contents = state::read(&path, 65536).unwrap();
        let entries = fs::read_dir(&root).unwrap().count();
        fs::remove_dir_all(root).unwrap();
        assert_eq!(result, Err(Fail::Tunnel(CONFLICT)));
        assert_eq!(contents, b"competing profile");
        assert_eq!(entries, 1);
    }
}
