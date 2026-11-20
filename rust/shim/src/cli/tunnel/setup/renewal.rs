//! A private write-ahead record lets setup finish replacing a key and its
//! expiry after interruption. The record holds only the replacement pair.
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};

use sha2::{Digest, Sha256};

use super::resume::{KeyBytes, commit_lock};
use super::{PROFILE_LIMIT, check_directory, publish_new, stage_and_publish, sync_directory};
use crate::{
    board::state,
    cli::tunnel::{Fail, Options, runtime, store, syntax},
};

pub(super) const KEY_LIMIT: usize = 16 * 1024;
const MAGIC: &[u8; 4] = b"PKR1";
const HEADER: usize = 4 + 32 * 2 + 4;
const CHECKSUM_SIZE: usize = 32;

fn path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.key-renewal"))
}

pub(in crate::cli::tunnel) fn check_pending(root: &Path, name: &str) -> Result<(), Fail> {
    if !syntax::valid_name(name) {
        return Err(Fail::Tunnel(store::INVALID_NAME));
    }
    let marker = path(root, name);
    if store::occupied(&marker)? {
        return Err(interrupted(&marker));
    }
    Ok(())
}

fn interrupted(marker: &Path) -> Fail {
    Fail::Detail(format!(
        "runtime key renewal is incomplete at {}. Retry tunnel setup for the same profile and directory to recover",
        marker.display()
    ))
}

fn changed(marker: &Path) -> Fail {
    Fail::Detail(format!(
        "cannot recover runtime key renewal at {} because its saved files or record differ. Restore the expected owner-only profile and key from backup before retrying",
        marker.display()
    ))
}

fn digest(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}

struct Saved {
    bytes: KeyBytes,
    identity: Vec<u128>,
}

impl Saved {
    fn read(path: &Path, limit: usize) -> Result<Self, Fail> {
        let identity = state::open(path).map_err(|_| Fail::Defer)?.identity;
        let bytes = KeyBytes(store::private_read(path, limit)?);
        if !state::open(path).is_ok_and(|file| file.identity == identity) {
            return Err(Fail::Defer);
        }
        Ok(Self { bytes, identity })
    }

    fn replace(self, path: &Path, bytes: &[u8]) -> Result<(), Fail> {
        if self.bytes.0 == bytes {
            return Ok(());
        }
        stage_and_publish(
            path,
            |file| file.write_all(bytes),
            |stage| {
                if !state::open(path).is_ok_and(|file| file.identity == self.identity)
                    || !store::private_read(path, self.bytes.0.len())
                        .is_ok_and(|bytes| KeyBytes(bytes).0 == self.bytes.0)
                {
                    return Err(Fail::Defer);
                }
                store::sharing_retry(|| fs::rename(stage, path)).map_err(|_| Fail::Defer)
            },
        )
    }
}

pub(super) fn publish(
    root: &Path,
    name: &str,
    old: (&[u8], &[u8]),
    replacement: (&[u8], &[u8]),
) -> Result<(), Fail> {
    let marker = path(root, name);
    check_pending(root, name)?;
    write_journal(&marker, old, replacement)?;
    settle(root, name, &marker)
}

fn write_journal(
    marker: &Path,
    old: (&[u8], &[u8]),
    replacement: (&[u8], &[u8]),
) -> Result<(), Fail> {
    let (profile, key) = replacement;
    if key.len() > KEY_LIMIT {
        return Err(Fail::Tunnel(
            "the protected runtime key is too large; supply a shorter runtime key",
        ));
    }
    let size = u32::try_from(profile.len()).expect("profile size is bounded");
    let mut journal = KeyBytes(Vec::with_capacity(
        HEADER + profile.len() + key.len() + CHECKSUM_SIZE,
    ));
    journal.0.extend_from_slice(MAGIC);
    journal.0.extend_from_slice(&digest(old.0));
    journal.0.extend_from_slice(&digest(old.1));
    journal.0.extend_from_slice(&size.to_be_bytes());
    journal.0.extend_from_slice(profile);
    journal.0.extend_from_slice(key);
    let checksum = digest(&journal.0);
    journal.0.extend_from_slice(&checksum);
    publish_new(marker, |file| file.write_all(&journal.0))
}

fn settle(root: &Path, name: &str, marker: &Path) -> Result<(), Fail> {
    let invalid = || changed(marker);
    let journal = KeyBytes(
        store::private_read(marker, HEADER + PROFILE_LIMIT + KEY_LIMIT + CHECKSUM_SIZE)
            .map_err(|_| invalid())?,
    );
    let checksum_at = journal
        .0
        .len()
        .checked_sub(CHECKSUM_SIZE)
        .ok_or_else(invalid)?;
    let (contents, checksum) = journal.0.split_at(checksum_at);
    if digest(contents).as_slice() != checksum {
        return Err(invalid());
    }
    let (head, payload) = contents.split_at_checked(HEADER).ok_or_else(invalid)?;
    let length = u32::from_be_bytes(head[68..HEADER].try_into().unwrap()) as usize;
    let (profile, key) = payload.split_at_checked(length).ok_or_else(invalid)?;
    let key_prefix = if cfg!(windows) {
        b"DPAPI\0"
    } else {
        b"PLAIN\0"
    };
    if &head[..4] != MAGIC
        || profile.len() > PROFILE_LIMIT
        || key.len() > KEY_LIMIT
        || key.len() <= key_prefix.len()
        || !key.starts_with(key_prefix)
        || !store::decode_profile(profile, name).is_ok_and(|(_, profile)| profile.state == "ready")
    {
        return Err(invalid());
    }
    let profile_path = store::profile_path(root, name)?;
    let key_path = store::key_path(root, name);
    let saved_profile = Saved::read(&profile_path, PROFILE_LIMIT).map_err(|_| invalid())?;
    let saved_key = Saved::read(&key_path, KEY_LIMIT).map_err(|_| invalid())?;
    for (saved, next, prior) in [
        (&saved_profile, profile, &head[4..36]),
        (&saved_key, key, &head[36..68]),
    ] {
        if saved.bytes.0 != next && digest(&saved.bytes.0).as_slice() != prior {
            return Err(invalid());
        }
    }
    saved_key
        .replace(&key_path, key)
        .map_err(|_| interrupted(marker))?;
    saved_profile
        .replace(&profile_path, profile)
        .map_err(|_| interrupted(marker))?;
    store::sharing_retry(|| fs::remove_file(marker)).map_err(|_| interrupted(marker))?;
    sync_directory(root)
}

pub(in crate::cli::tunnel) fn recover_pending(
    options: &Options,
    root: &Path,
    profile_path: &Path,
) -> Result<Option<(String, u8)>, Fail> {
    let name = &options.profile;
    let marker = path(root, name);
    if !store::occupied(&marker)? {
        return Ok(None);
    }
    check_directory(root)?;
    let _runtime = runtime::prepare_locks(root, name)?;
    let original =
        store::private_read(profile_path, PROFILE_LIMIT).map_err(|_| changed(&marker))?;
    let (_setup, _) = commit_lock(root, profile_path, &original)?;
    if !store::occupied(&marker)? {
        return Ok(None);
    }
    settle(root, name, &marker)?;
    let status = super::super::bridge::status_command(name, options.profile_dir.as_deref());
    Ok(Some((
        format!("Recovered runtime key renewal for profile {name}. Check it with `{status}`.\n"),
        0,
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn committed_renewal_recovers_at_each_publication_boundary() {
        for published in 0..=2 {
            let root = std::env::temp_dir().join(format!("tunnel-renew-{}", uuid::Uuid::new_v4()));
            state::private_dir(&root).unwrap();
            let name = "dot";
            let profile = store::profile_path(&root, name).unwrap();
            let key = store::key_path(&root, name);
            let marker = path(&root, name);
            let token = root.join("token");
            let mut old = serde_json::json!({
                "name": name, "daemon_url": "http://127.0.0.1:9", "token_file": token,
                "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
                "runtime_key_expires_at": "2020-01-01T00:00:00Z",
            });
            let old_profile = old.to_string();
            old["runtime_key_expires_at"] = serde_json::Value::Null;
            let new_profile = old.to_string();
            let prefix = if cfg!(windows) {
                b"DPAPI\0"
            } else {
                b"PLAIN\0"
            };
            // Opaque stored envelopes exercise publication. CLI contracts
            // separately exercise the real platform key codec.
            let old_key = [prefix.as_slice(), b"old fixture"].concat();
            let new_key = [prefix.as_slice(), b"new fixture"].concat();
            state::atomic_create(&profile, old_profile.as_bytes()).unwrap();
            state::atomic_create(&key, &old_key).unwrap();
            write_journal(
                &marker,
                (old_profile.as_bytes(), &old_key),
                (new_profile.as_bytes(), &new_key),
            )
            .unwrap();
            assert!(check_pending(&root, name).is_err());
            if published >= 1 {
                state::atomic_write(&key, &new_key, None).unwrap();
            }
            if published == 2 {
                state::atomic_write(&profile, new_profile.as_bytes(), None).unwrap();
            }
            let options = super::super::super::parse(&["setup".into()]).unwrap();
            if published == 0 {
                state::atomic_write(&key, b"unrelated fixture", None).unwrap();
                assert!(recover_pending(&options, &root, &profile).is_err());
                assert!(marker.exists() && fs::read(&profile).unwrap() == old_profile.as_bytes());
                assert!(fs::read(&key).unwrap() == b"unrelated fixture");
                state::atomic_write(&key, &old_key, None).unwrap();
            }
            let recovered = recover_pending(&options, &root, &profile).unwrap().unwrap();
            assert!(recovered.0.contains("Recovered runtime key renewal"));
            assert!(fs::read(&profile).unwrap() == new_profile.as_bytes());
            assert!(store::private_read(&key, KEY_LIMIT).unwrap() == new_key);
            assert!(check_pending(&root, name).is_ok());
            fs::remove_dir_all(root).unwrap();
        }
    }
}
