//! Resume an existing tunnel profile and manage its private runtime key.
use std::{
    fs::{self, File},
    io::{IsTerminal, Write},
    path::{Path, PathBuf},
};

use serde_json::Value;

use super::{
    PROFILE_LIMIT, check_directory, consent_required, disclose, fill_profile_defaults,
    profile_bytes, publish_new, renewal, saved_message, stage_and_publish, verify_connection,
};
use crate::{
    board::state,
    cli::tunnel::{
        Fail, KEY_RENEWAL, Options, Output, absolute_token, bridge::status_command, record,
        runtime, setup_command, store,
    },
    credentials::{CredentialProvider, MAX_TOKEN_BYTES, decode_token},
};

const CHANGED: &str =
    "the saved tunnel profile changed during setup; check tunnel status before retrying";
const LOCK: &str = "cannot lock the tunnel setup directory; check its owner-only access and retry after any other setup finishes";
const KEY_INPUT: &str = "could not read a private runtime key; supply --key-file with an owner-only file or use --read-key at a terminal";

pub(in crate::cli::tunnel) fn resume(options: &Options, root: &Path, path: &Path) -> Output {
    check_directory(root)?;
    // A file in a form this client does not open stays `Defer`, which names
    // it; a saved profile it can read but not use names the field at fault.
    let broken = |fail: Fail| match fail {
        Fail::Defer => Fail::Defer,
        Fail::Tunnel(reason) => Fail::Detail(format!(
            "{reason}: {}; fix that file or move it aside and save the profile again with `pseudolife-mcp tunnel setup --daemon-url <url> --token-file <owner-only file> --accept-access`",
            path.display()
        )),
        Fail::Detail(reason) => Fail::Detail(format!(
            "{reason} ({}); fix that file or move it aside and save the profile again with `pseudolife-mcp tunnel setup --daemon-url <url> --token-file <owner-only file> --accept-access`",
            path.display()
        )),
    };
    let original = store::private_read(path, PROFILE_LIMIT).map_err(broken)?;
    let (mut saved, profile) =
        store::decode_profile(&original, &options.profile).map_err(broken)?;
    let setup = &options.setup;
    let directory = root.display().to_string();
    let status = status_command(
        &profile.name,
        options.profile_dir.as_ref().map(|_| directory.as_str()),
    );

    if setup.has_key_input() {
        if profile.state != "ready" {
            return Err(if profile.tunnel_id.is_none() {
                Fail::Tunnel(
                    "save a tunnel ID with tunnel setup --tunnel-id before importing its runtime key",
                )
            } else {
                Fail::Detail(format!(
                    "profile {} is not ready to take a runtime key (its saved state is {}); `{status}` shows the step it still needs. Finish that step with tunnel setup, then import the key",
                    profile.name, profile.state
                ))
            });
        }
        let key_path = store::key_path(root, &profile.name);
        let old_key = if store::occupied(&key_path)? {
            require_stopped_record(root, &profile.name, "renew the key")?;
            Some(KeyBytes(store::private_read(
                &key_path,
                renewal::KEY_LIMIT,
            )?))
        } else {
            require_inactive(root, &profile.name, true, &status)?;
            None
        };
        let replacement_profile = if old_key.is_some() {
            saved.insert(
                "runtime_key_expires_at".into(),
                Value::from(setup.key_expires_at.clone()),
            );
            let updated = store::validate(&saved)?;
            if updated
                .expires_at
                .as_ref()
                .is_some_and(|(_, expires)| *expires <= chrono::Utc::now())
            {
                return Err(Fail::Tunnel(
                    "supply a future --key-expires-at value for the replacement runtime key",
                ));
            }
            Some(profile_bytes(&saved)?)
        } else {
            None
        };
        let key = read_key(options)?;
        let daemon = verify_connection(&profile)?;
        if daemon
            .token()
            .is_some_and(|token| token.as_bytes() == key.0.as_slice())
        {
            return Err(Fail::Tunnel(
                "the runtime key matches the daemon credential; supply the separate tunnel runtime key",
            ));
        }
        if let (Some(old_key), Some(replacement_profile)) = (old_key, replacement_profile) {
            let _runtime = runtime::key_change_locks(root, &profile.name)?;
            let (_lock, _) = commit_lock(root, path, &original)?;
            if !store::private_read(&key_path, renewal::KEY_LIMIT)
                .is_ok_and(|bytes| KeyBytes(bytes).0 == old_key.0)
            {
                return Err(Fail::Tunnel(CHANGED));
            }
            CredentialProvider::new(None, Some((&profile.token_file).into()))
                .and_then(|provider| provider.require_current(&daemon))
                .map_err(|_| Fail::Tunnel(
                    "daemon credential changed during runtime key renewal; retry with the current token file"
                ))?;
            if profile.expires_at.as_ref().map(|(text, _)| text) == setup.key_expires_at.as_ref()
                && runtime::saved_key_matches(root, &profile.name, &key.0)
            {
                return Ok((
                    format!(
                        "Runtime key for profile {} is already configured. Check it with `{status}`.\n",
                        profile.name
                    ),
                    0,
                ));
            }
            require_no_previous_key(options, root)?;
            if store::key_state(root, &profile.name)? != Some(true) {
                return Err(Fail::Tunnel(
                    "cannot retain the current runtime key. Restore its owner-only file or use the account sign-in that can unlock it, then retry",
                ));
            }
            let encoded = encode_key(&key.0)?;
            renewal::publish(
                root,
                &profile.name,
                (&original, &old_key.0),
                (&replacement_profile, &encoded.0),
            )?;
            return Ok((
                format!(
                    "Runtime key renewed for profile {}. The previous key is retained until a successful control-plane poll. It can still be valid at the provider. Revoke it there after accepting the replacement. Check it with `{status}`.\n",
                    profile.name
                ),
                0,
            ));
        }
        let encoded = encode_key(&key.0);
        let (_lock, _) = commit_lock(root, path, &original)?;
        renewal::check_pending(root, &profile.name)?;
        recheck_inactive(root, &profile.name, &status, true)?;
        let encoded = encoded?;
        publish_new(&store::key_path(root, &profile.name), |file| {
            file.write_all(&encoded.0)
        })?;
        return Ok((
            format!(
                "Runtime key saved for profile {}. Check it with `{status}`.\n",
                profile.name
            ),
            0,
        ));
    }

    fill_profile_defaults(&mut saved);
    let mut desired = saved.clone();
    match (&setup.daemon_url, &setup.token_file) {
        (None, None) => {}
        (Some(url), Some(token)) if !url.is_empty() && !token.is_empty() => {
            crate::cli::tunnel::checked_url(url, "--daemon-url")?;
            desired.insert("daemon_url".into(), Value::from(url.clone()));
            desired.insert("token_file".into(), Value::from(absolute_token(token)?));
        }
        _ => {
            return Err(Fail::Tunnel(
                "pass both --daemon-url and --token-file to change the saved daemon connection",
            ));
        }
    }
    setup.edit(&mut desired);
    let needs_consent = saved["consent"] != true
        || ["daemon_url", "token_file"]
            .iter()
            .any(|field| saved[*field] != desired[*field])
        || (saved["minimum_catalog"] != "full" && desired["minimum_catalog"] == "full")
        || ["tunnel_id", "organization_id"]
            .iter()
            .any(|field| !saved[*field].is_null() && saved[*field] != desired[*field]);
    let new_state = if desired["tunnel_id"].is_null() {
        "pending"
    } else {
        "ready"
    };
    desired.insert("state".into(), Value::from(new_state));
    desired.insert("consent".into(), Value::Bool(true));
    if desired == saved {
        // This is a no-op. Status is the user's next step for checking the
        // saved configuration without silently making a network request.
        return Ok((
            format!(
                "Profile {} is already configured. Check it with `{status}`.\n",
                profile.name
            ),
            0,
        ));
    }
    // A key saved before setup started stays; one that appears during the
    // handshake belongs to another setup, so the commit refuses it.
    let had_key = store::occupied(&store::key_path(root, &profile.name))?;
    // The saved key's expiry changes only with a renewal of that key, alone
    // or beside other settings.
    if had_key && saved.get("runtime_key_expires_at") != desired.get("runtime_key_expires_at") {
        return Err(Fail::Tunnel(KEY_RENEWAL));
    }
    require_inactive(root, &profile.name, false, &status)?;
    let updated = store::validate(&desired)?;
    if needs_consent && !setup.accept_access {
        return Err(consent_required(&updated));
    }
    let bytes = profile_bytes(&desired)?;
    verify_connection(&updated)?;
    if needs_consent && disclose(&updated).is_err() {
        return Ok((String::new(), crate::cli::tunnel::STDOUT_REFUSED));
    }
    // With a saved key a launch or a renewal may be starting: hold their
    // locks, then check again that neither left anything behind.
    let _runtime = if had_key {
        Some(runtime::key_change_locks(root, &updated.name)?)
    } else {
        None
    };
    let (_lock, expected) = commit_lock(root, path, &original)?;
    recheck_inactive(root, &updated.name, &status, !had_key)?;
    if had_key {
        renewal::check_pending(root, &updated.name)?;
        if renewal::validate_previous_key(root, &updated.name)? {
            return Err(Fail::Detail(format!(
                "a key renewal of profile {} began during setup and still retains its previous key; finish it with tunnel setup --restore-key or --discard-previous-key, then change the settings",
                updated.name
            )));
        }
    }
    stage_and_publish(
        path,
        |file| file.write_all(&bytes),
        |stage| {
            if !state::open(path).is_ok_and(|current| current.identity == expected)
                || !store::private_read(path, PROFILE_LIMIT)
                    .is_ok_and(|current| current == original)
            {
                return Err(Fail::Tunnel(CHANGED));
            }
            store::sharing_retry(|| fs::rename(stage, path)).map_err(|_| {
            Fail::Detail(format!(
                "could not replace {}; check write access to the tunnel directory before retrying",
                path.display(),
            ))
        })
        },
    )?;
    Ok(saved_message(options, root, &updated))
}

fn require_no_previous_key(options: &Options, root: &Path) -> Result<(), Fail> {
    if renewal::validate_previous_key(root, &options.profile)? {
        let command = setup_command(&options.profile, options.profile_dir.as_deref());
        return Err(Fail::Detail(format!(
            "The previous key is still retained. Stop the tunnel and run `{command} --restore-key` before trying a different replacement."
        )));
    }
    Ok(())
}

fn require_stopped_record(root: &Path, name: &str, action: &str) -> Result<(), Fail> {
    let path = record::path(root, name);
    if store::occupied(&path)? {
        let table = record::table();
        if !record::read(path.clone(), table.as_deref())
            .is_some_and(|record| record.native && record.liveness == record::Liveness::Gone)
        {
            return Err(Fail::Detail(format!(
                "cannot {action} while a launch may own {}. Stop the tunnel first, then retry setup",
                path.display()
            )));
        }
    }
    Ok(())
}

/// Before a first key import (`key`), or a settings change: no process
/// record, and for a settings change no renewal still retaining its
/// previous key. A saved key does not block a settings change: the tunnel
/// reads the profile again at its next launch.
fn require_inactive(root: &Path, name: &str, key: bool, status: &str) -> Result<(), Fail> {
    if let Some((path, kind)) = runtime_conflict(root, name, key)? {
        return Err(Fail::Detail(if kind == "runtime key" {
            format!(
                "a runtime key is already saved at {}; renew it with tunnel setup --read-key or --key-file and --key-expires-at",
                path.display()
            )
        } else {
            format!(
                "cannot change profile {name} while a process record exists at {}. Check `{status}`; once it reports the tunnel stopped, move that record aside, then retry setup",
                path.display()
            )
        }));
    }
    if !key && renewal::validate_previous_key(root, name)? {
        return Err(Fail::Detail(format!(
            "a key renewal of profile {name} still retains its previous key; finish it with tunnel setup --restore-key or --discard-previous-key, then change the settings"
        )));
    }
    Ok(())
}

fn recheck_inactive(root: &Path, name: &str, status: &str, key: bool) -> Result<(), Fail> {
    if let Some((path, kind)) = runtime_conflict(root, name, key)? {
        return Err(Fail::Detail(format!(
            "a {kind} appeared during setup at {}. Check `{status}` before retrying",
            path.display(),
        )));
    }
    Ok(())
}

/// The runtime key (only when `key`) or process record that blocks setup.
fn runtime_conflict(
    root: &Path,
    name: &str,
    key: bool,
) -> Result<Option<(PathBuf, &'static str)>, Fail> {
    let key_path = key.then(|| (store::key_path(root, name), "runtime key"));
    for (path, kind) in key_path
        .into_iter()
        .chain([(record::path(root, name), "process record")])
    {
        if store::occupied(&path)? {
            return Ok(Some((path, kind)));
        }
    }
    Ok(None)
}

pub(super) fn commit_lock(
    root: &Path,
    profile: &Path,
    expected: &[u8],
) -> Result<(File, Vec<u128>), Fail> {
    check_directory(root)?;
    let path = store::checked_path(&root.join(".tunnel-setup.lock"))?;
    // Keep a single lock file for this store. Never unlink a lock other
    // processes might already have open.
    if !store::occupied(&path)? {
        drop(state::create(&path));
    }
    let opened = state::open_with_write(&path, true).map_err(|_| Fail::Tunnel(LOCK))?;
    opened.file.try_lock().map_err(|_| Fail::Tunnel(LOCK))?;
    let profile_identity = state::open(profile)
        .map_err(|_| Fail::Tunnel(CHANGED))?
        .identity;
    if !state::open(&path).is_ok_and(|current| current.identity == opened.identity)
        || !store::private_read(profile, PROFILE_LIMIT).is_ok_and(|bytes| bytes == expected)
    {
        return Err(Fail::Tunnel(CHANGED));
    }
    Ok((opened.file, profile_identity))
}

pub(super) struct KeyBytes(pub(super) Vec<u8>);

impl Drop for KeyBytes {
    fn drop(&mut self) {
        self.0.fill(0);
        std::hint::black_box(&self.0);
    }
}

fn read_key(options: &Options) -> Result<KeyBytes, Fail> {
    let input = if let Some(path) = &options.setup.key_file {
        store::private_read(Path::new(path), MAX_TOKEN_BYTES)
            .map_err(|_| Fail::Tunnel(KEY_INPUT))?
    } else {
        if !std::io::stdin().is_terminal() {
            return Err(Fail::Tunnel(
                "--read-key needs a terminal; use --key-file with an owner-only file for unattended setup",
            ));
        }
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| Fail::Tunnel(KEY_INPUT))?
            .block_on(crate::cli::pairing::prompt::password(
                "Tunnel API key (private, no echo): ",
            ))
            .map_err(|_| Fail::Tunnel(KEY_INPUT))?
            .into_bytes()
    };
    let input = KeyBytes(input);
    let key = decode_token(&input.0)
        .map_err(|_| Fail::Tunnel("invalid private tunnel runtime key; check the key and retry"))?;
    Ok(KeyBytes(key.into_bytes()))
}

fn encode_key(key: &[u8]) -> Result<KeyBytes, Fail> {
    #[cfg(unix)]
    let encoded = [b"PLAIN\0".as_slice(), key].concat();
    #[cfg(windows)]
    let encoded = {
        let sealed = crate::cli::tunnel::dpapi::protect(key).map_err(|_| Fail::Tunnel(
            "Windows could not protect the runtime key with DPAPI; rerun setup from an interactive sign-in under the account that will run the tunnel",
        ))?;
        [b"DPAPI\0".as_slice(), &sealed].concat()
    };
    Ok(KeyBytes(encoded))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commit_refuses_a_busy_store_and_a_changed_profile() {
        let root = std::env::temp_dir().join(format!("tunnel-resume-{}", uuid::Uuid::new_v4()));
        state::private_dir(&root).unwrap();
        let profile = root.join("dot.profile.json");
        state::atomic_create(&profile, b"old fixture").unwrap();
        let (lock, _) = commit_lock(&root, &profile, b"old fixture").unwrap();
        let busy = commit_lock(&root, &profile, b"old fixture");
        drop(lock);
        state::atomic_write(&profile, b"new fixture", None).unwrap();
        let stale = commit_lock(&root, &profile, b"old fixture");
        let current = commit_lock(&root, &profile, b"new fixture");
        let observed = (busy.err(), stale.err(), current.is_ok());
        drop(current);
        std::fs::remove_dir_all(root).unwrap();
        assert_eq!(
            observed,
            (Some(Fail::Tunnel(LOCK)), Some(Fail::Tunnel(CHANGED)), true)
        );
    }
}
