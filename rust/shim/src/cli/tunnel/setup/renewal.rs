//! A private write-ahead record lets setup finish replacing a key and its
//! expiry after interruption. A separate record holds the previous pair
//! until a successful poll or an explicit decision.
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};

use sha2::{Digest, Sha256};

use super::resume::{KeyBytes, commit_lock, require_no_autostart};
use super::{PROFILE_LIMIT, check_directory, publish_new, stage_and_publish, sync_directory};
use crate::cli::tunnel::{Fail, Options, Output, runtime, store, syntax};

pub(super) const KEY_LIMIT: usize = 16 * 1024;
const MAGIC: &[u8; 4] = b"PKR1";
const HEADER: usize = 4 + 32 * 2 + 4;
const CHECKSUM_SIZE: usize = 32;
const JOURNAL_LIMIT: usize = HEADER + PROFILE_LIMIT + KEY_LIMIT + CHECKSUM_SIZE;

fn path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.key-renewal"))
}

fn previous_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.key-previous"))
}

fn has_previous_key(root: &Path, name: &str) -> Result<bool, Fail> {
    store::occupied(&previous_path(root, name))
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
        "cannot recover runtime key renewal at {} because its saved files or record differ. Stop the tunnel and check that the saved key and profile expiry belong to the same credential. After confirming the pair or restoring it from backup, remove {} and rerun tunnel setup",
        marker.display(),
        marker.display()
    ))
}

fn digest(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}

fn replace(path: &Path, saved: &[u8], next: &[u8]) -> Result<(), Fail> {
    if saved == next {
        return Ok(());
    }
    stage_and_publish(
        path,
        |file| file.write_all(next),
        |stage| {
            if !store::private_read(path, saved.len()).is_ok_and(|bytes| KeyBytes(bytes).0 == saved)
            {
                return Err(Fail::Defer);
            }
            store::sharing_retry(|| fs::rename(stage, path)).map_err(|_| Fail::Defer)
        },
    )
}

pub(super) fn publish(
    root: &Path,
    name: &str,
    old: (&[u8], &[u8]),
    replacement: (&[u8], &[u8]),
) -> Result<(), Fail> {
    let marker = path(root, name);
    check_pending(root, name)?;
    check_key_size(replacement.1)?;
    write_journal(&previous_path(root, name), replacement, old)?;
    write_journal(&marker, old, replacement)?;
    settle(root, name, &marker)
}

fn write_journal(
    marker: &Path,
    old: (&[u8], &[u8]),
    replacement: (&[u8], &[u8]),
) -> Result<(), Fail> {
    let (profile, key) = replacement;
    check_key_size(key)?;
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

fn check_key_size(key: &[u8]) -> Result<(), Fail> {
    if key.len() > KEY_LIMIT {
        return Err(Fail::Tunnel(
            "the protected runtime key is too large; supply a shorter runtime key",
        ));
    }
    Ok(())
}

struct Journal {
    bytes: KeyBytes,
    profile_end: usize,
    key_end: usize,
}

impl Journal {
    fn pair(&self) -> (&[u8], &[u8]) {
        (
            &self.bytes.0[HEADER..self.profile_end],
            &self.bytes.0[self.profile_end..self.key_end],
        )
    }

    fn prior(&self) -> (&[u8], &[u8]) {
        (&self.bytes.0[4..36], &self.bytes.0[36..68])
    }
}

fn read_journal(name: &str, marker: &Path) -> Result<Journal, Fail> {
    let invalid = || changed(marker);
    let journal = KeyBytes(store::private_read(marker, JOURNAL_LIMIT).map_err(|_| invalid())?);
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
    Ok(Journal {
        bytes: journal,
        profile_end: HEADER + length,
        key_end: checksum_at,
    })
}

fn saved_pair(root: &Path, name: &str, marker: &Path) -> Result<(KeyBytes, KeyBytes), Fail> {
    let profile = store::profile_path(root, name)?;
    let key = store::key_path(root, name);
    Ok((
        KeyBytes(store::private_read(&profile, PROFILE_LIMIT).map_err(|_| changed(marker))?),
        KeyBytes(store::private_read(&key, KEY_LIMIT).map_err(|_| changed(marker))?),
    ))
}

fn settle(root: &Path, name: &str, marker: &Path) -> Result<(), Fail> {
    let journal = read_journal(name, marker)?;
    apply_journal(root, name, marker, &journal)
}

fn apply_journal(root: &Path, name: &str, marker: &Path, journal: &Journal) -> Result<(), Fail> {
    let (profile, key) = journal.pair();
    let (prior_profile, prior_key) = journal.prior();
    let (saved_profile, saved_key) = saved_pair(root, name, marker)?;
    let profile_path = store::profile_path(root, name)?;
    let key_path = store::key_path(root, name);
    for (saved, next, prior) in [
        (&saved_profile, profile, prior_profile),
        (&saved_key, key, prior_key),
    ] {
        if saved.0 != next && digest(&saved.0).as_slice() != prior {
            return Err(changed(marker));
        }
    }
    replace(&key_path, &saved_key.0, key).map_err(|_| interrupted(marker))?;
    replace(&profile_path, &saved_profile.0, profile).map_err(|_| interrupted(marker))?;
    store::sharing_retry(|| fs::remove_file(marker)).map_err(|_| interrupted(marker))?;
    sync_directory(root)
}

fn read_previous(root: &Path, name: &str) -> Result<Journal, Fail> {
    let marker = previous_path(root, name);
    let journal = read_journal(name, &marker).map_err(|_| {
        Fail::Detail(format!(
            "cannot read the retained previous key at {}. Preserve this file and restore an owner-only copy of it from backup",
            marker.display()
        ))
    })?;
    let mismatch = || {
        Fail::Detail(format!(
            "cannot use the retained previous key at {} because the saved key or profile changed since the replacement was started. Keep the retained file and restore the saved profile and key together to the replacement pair before retrying",
            marker.display()
        ))
    };
    let (profile, key) = saved_pair(root, name, &marker).map_err(|_| mismatch())?;
    let (previous_profile, previous_key) = journal.pair();
    let (candidate_profile, candidate_key) = journal.prior();
    if !(profile.0 == previous_profile && key.0 == previous_key
        || digest(&profile.0).as_slice() == candidate_profile
            && digest(&key.0).as_slice() == candidate_key)
    {
        return Err(mismatch());
    }
    Ok(journal)
}

pub(in crate::cli::tunnel) fn validate_previous_key(root: &Path, name: &str) -> Result<bool, Fail> {
    if !has_previous_key(root, name)? {
        return Ok(false);
    }
    match read_previous(root, name) {
        Ok(_) => Ok(true),
        Err(_) if !has_previous_key(root, name)? => Ok(false),
        Err(error) => Err(error),
    }
}

fn remove_previous(root: &Path, name: &str) -> Result<(), Fail> {
    let marker = previous_path(root, name);
    let failure = || {
        Fail::Detail(format!(
            "cannot remove the previous key at {}. Check owner-only access to that file, then retry --discard-previous-key",
            marker.display()
        ))
    };
    #[cfg(windows)]
    {
        // Flush an empty file before unlinking. NTFS can recover the name
        // after power loss without bringing back any retained key bytes.
        let opened = crate::board::state::open_with_write(&marker, true).map_err(|_| failure())?;
        opened
            .file
            .set_len(0)
            .and_then(|()| opened.file.sync_all())
            .map_err(|_| failure())?;
    }
    store::sharing_retry(|| fs::remove_file(&marker)).map_err(|_| failure())?;
    sync_directory(root)
}

pub(in crate::cli::tunnel) fn qualify_previous_key(root: &Path, name: &str) -> Result<(), Fail> {
    read_previous(root, name)?;
    remove_previous(root, name).map_err(|_| {
        Fail::Detail(format!(
            "the replacement key completed a control-plane poll, but cleanup could not durably remove {}. Check owner-only access, then retry `pseudolife-mcp tunnel setup --discard-previous-key` for the same profile and directory",
            previous_path(root, name).display()
        ))
    })
}

pub(in crate::cli::tunnel) fn finish_previous_key(
    options: &Options,
    root: &Path,
    profile: &Path,
) -> Output {
    let name = &options.profile;
    check_directory(root)?;
    if !has_previous_key(root, name)? {
        return Ok((format!("Profile {name} has no retained previous key.\n"), 0));
    }
    let _runtime = runtime::key_change_locks(root, name)?;
    let original = store::private_read(profile, PROFILE_LIMIT)?;
    let (saved, _) = store::decode_profile(&original, name)?;
    require_no_autostart(&saved)?;
    let (_setup, _) = commit_lock(root, profile, &original)?;
    check_pending(root, name)?;
    if !has_previous_key(root, name)? {
        return Ok((format!("Profile {name} has no retained previous key.\n"), 0));
    }
    let action = if options.setup.restore_key {
        let journal = read_previous(root, name)?;
        let marker = path(root, name);
        publish_new(&marker, |file| file.write_all(&journal.bytes.0))?;
        settle(root, name, &marker)?;
        "restored"
    } else {
        // A damaged but owner-only retained file can still be discarded.
        let _private = KeyBytes(store::private_read(
            &previous_path(root, name),
            JOURNAL_LIMIT,
        )?);
        "discarded"
    };
    remove_previous(root, name)?;
    Ok((format!("Previous key {action} for profile {name}.\n"), 0))
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
    let _runtime = runtime::key_change_locks(root, name)?;
    let original =
        store::private_read(profile_path, PROFILE_LIMIT).map_err(|_| changed(&marker))?;
    let (_setup, _) = commit_lock(root, profile_path, &original)?;
    if !store::occupied(&marker)? {
        return Ok(None);
    }
    let pending = read_journal(name, &marker)?;
    let restoring = store::private_read(&previous_path(root, name), JOURNAL_LIMIT)
        .is_ok_and(|bytes| KeyBytes(bytes).0 == pending.bytes.0);
    apply_journal(root, name, &marker, &pending)?;
    let operation = if restoring {
        remove_previous(root, name)?;
        "the explicit restore of the previous key"
    } else {
        "runtime key renewal"
    };
    let status = super::super::bridge::status_command(name, options.profile_dir.as_deref());
    Ok(Some((
        format!(
            "Recovered {operation} for profile {name}. Check it with `{status}`, then rerun your command.\n"
        ),
        0,
    )))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::board::state;

    #[test]
    fn mismatched_previous_pair_preserves_recovery_file_without_advertising_restore() {
        let root = std::env::temp_dir().join(format!("tunnel-previous-{}", uuid::Uuid::new_v4()));
        state::private_dir(&root).unwrap();
        let name = "dot";
        let profile = store::profile_path(&root, name).unwrap();
        let key = store::key_path(&root, name);
        let previous = previous_path(&root, name);
        let mut saved = serde_json::json!({
            "name": name, "daemon_url": "http://127.0.0.1:9", "token_file": root.join("token"),
            "tunnel_id": "tunnel_abcd", "state": "ready", "consent": true,
            "runtime_key_expires_at": "2020-01-01T00:00:00Z",
        });
        let original = saved.to_string();
        saved["runtime_key_expires_at"] = serde_json::Value::Null;
        let replacement = saved.to_string();
        let prefix = if cfg!(windows) {
            b"DPAPI\0"
        } else {
            b"PLAIN\0"
        };
        let old_key = [prefix.as_slice(), b"old fixture"].concat();
        let new_key = [prefix.as_slice(), b"new fixture"].concat();
        state::atomic_create(&profile, replacement.as_bytes()).unwrap();
        state::atomic_create(&key, &new_key).unwrap();
        write_journal(
            &previous,
            (replacement.as_bytes(), &new_key),
            (original.as_bytes(), &old_key),
        )
        .unwrap();
        let retained = store::private_read(&previous, JOURNAL_LIMIT).unwrap();
        let cli = super::super::super::parse;
        let status = super::super::super::status;
        let text = cli(&["status".into()]).unwrap();
        let json = cli(&["status".into(), "--json".into()]).unwrap();
        assert!(status(&text, &root).unwrap().0.contains("--restore-key"));

        saved["tunnel_id"] = serde_json::json!("tunnel_dcba");
        let edited = saved.to_string();
        state::atomic_write(&profile, edited.as_bytes(), None).unwrap();
        for options in [&text, &json] {
            let error =
                status(options, &root).expect_err("status must refuse a mismatched retained key");
            let Fail::Detail(message) = error else {
                panic!("unexpected status refusal: {error:?}");
            };
            assert!(message.contains(&previous.display().to_string()));
            assert!(message.contains("saved key or profile changed"));
            assert!(message.contains("restore the saved profile and key together"));
            assert!(
                !message.contains("--restore-key") && !message.contains("--discard-previous-key")
            );
            assert_eq!(
                validate_previous_key(&root, name),
                Err(Fail::Detail(message))
            );
        }
        assert!(fs::read(&profile).unwrap() == edited.as_bytes());
        assert!(fs::read(&key).unwrap() == new_key);
        assert!(store::private_read(&previous, JOURNAL_LIMIT).unwrap() == retained);
        state::atomic_write(&profile, replacement.as_bytes(), None).unwrap();
        assert!(status(&text, &root).unwrap().0.contains("--restore-key"));
        fs::remove_dir_all(root).unwrap();
    }

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
                let journal = store::private_read(&marker, 131072).unwrap();
                let mut damaged = journal.clone();
                damaged[HEADER + new_profile.len() + prefix.len()] ^= 1;
                state::atomic_write(&marker, &damaged, None).unwrap();
                assert!(recover_pending(&options, &root, &profile).is_err());
                assert!(marker.exists() && fs::read(&key).unwrap() == old_key);
                state::atomic_write(&marker, &journal, None).unwrap();
                state::atomic_write(&key, b"unrelated fixture", None).unwrap();
                assert!(recover_pending(&options, &root, &profile).is_err());
                assert!(marker.exists() && fs::read(&profile).unwrap() == old_profile.as_bytes());
                assert!(fs::read(&key).unwrap() == b"unrelated fixture");
                state::atomic_write(&key, &old_key, None).unwrap();
            }
            let recovered = recover_pending(&options, &root, &profile).unwrap().unwrap();
            assert!(recovered.0.contains("Recovered runtime key renewal"));
            assert!(recovered.0.contains("rerun your command"));
            assert!(fs::read(&profile).unwrap() == new_profile.as_bytes());
            assert!(store::private_read(&key, KEY_LIMIT).unwrap() == new_key);
            assert!(check_pending(&root, name).is_ok());
            if published == 2 {
                let previous = previous_path(&root, name);
                write_journal(
                    &previous,
                    (new_profile.as_bytes(), &new_key),
                    (old_profile.as_bytes(), &old_key),
                )
                .unwrap();
                let restore = read_journal(name, &previous).unwrap();
                publish_new(&marker, |file| file.write_all(&restore.bytes.0)).unwrap();
                state::atomic_write(&key, &old_key, None).unwrap();
                let recovered = recover_pending(&options, &root, &profile).unwrap().unwrap();
                assert!(recovered.0.contains("Recovered the explicit restore"));
                assert!(fs::read(&profile).unwrap() == old_profile.as_bytes());
                assert!(!previous.exists() && !marker.exists());
            }
            fs::remove_dir_all(root).unwrap();
        }
    }
}
