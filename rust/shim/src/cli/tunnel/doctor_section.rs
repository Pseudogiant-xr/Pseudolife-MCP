//! `doctor`'s saved-tunnel section. Every step it names is one this client
//! runs, and only where that step answers: where native `tunnel status`
//! cannot read a file, the section names that file instead.
use std::{io, path::Path};

use chrono::Utc;
use serde_json::{Map, Value, json};

use super::{
    ChallengeIssue, Fail, Refresh, challenge_issue, challenge_path, occupied, refresh_status, store,
};

/// Where a new restricted key is created (the Python client's `KEYS_URL`).
const KEYS_URL: &str = "https://platform.openai.com/settings/organization/api-keys";

/// Why native `tunnel status --profile <name>` would not answer, with the
/// one fix, given the key's state it read first; `None` when it answers.
/// It follows `status`'s reads after the profile loaded: the key, then the
/// process record (never a refusal), then the challenge record (the same
/// [`challenge_issue`] `status` uses).
fn status_blocker(
    root: &Path,
    name: &str,
    key_state: &Result<Option<bool>, Fail>,
) -> Option<String> {
    if *key_state == Err(Fail::Defer) {
        return Some(format!(
            "pseudolife-mcp tunnel status --profile {name} cannot read the key file {}; check that it is a plain owner-only file, not a pipe or a link",
            store::key_path(root, name).display()
        ));
    }
    let path = challenge_path(root, name);
    match challenge_issue(root, name)? {
        ChallengeIssue::Form => Some(format!(
            "pseudolife-mcp tunnel status --profile {name} cannot read the verification challenge {}; check that it is a plain owner-only file, not a pipe or a link",
            path.display()
        )),
        ChallengeIssue::Deep => Some(format!(
            "pseudolife-mcp tunnel status --profile {name} does not read the verification challenge {}, which is nested too deeply to be one this client wrote; move that file aside",
            path.display()
        )),
    }
}

/// Read-only: `None` when `root` holds no `*.profile.json` (absent, empty or
/// unrelated files say nothing), else one entry per profile, where a profile
/// or a store this client cannot read is a named `unavailable` entry, never
/// a reason to stop. Key presence only: no key, token or credential path is
/// reported.
pub(in crate::cli) fn doctor_section(root: &Path) -> Option<Vec<Value>> {
    let unavailable = |profile: Option<&str>, reason: &str, recovery: String| {
        let mut entry = Map::new();
        if let Some(profile) = profile {
            entry.insert("profile".into(), json!(profile));
        }
        entry.insert("state".into(), json!("unavailable"));
        entry.insert("reason".into(), json!(reason));
        entry.insert("recovery".into(), json!(recovery));
        Value::Object(entry)
    };
    let store_recovery = format!(
        "Make {} a directory only your account can access, holding only tunnel profile files, then retry.",
        root.display()
    );
    match std::fs::symlink_metadata(root) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return None,
        Err(_) => {
            return Some(vec![unavailable(
                None,
                "the tunnel directory cannot be inspected",
                store_recovery,
            )]);
        }
        Ok(_) => {}
    }
    // Profiles first, then the store's own checks: an empty or unrelated
    // directory is not a store anyone set up.
    let any_profile = std::fs::read_dir(root).ok().map(|entries| {
        entries.flatten().any(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .to_ascii_lowercase()
                .ends_with(".profile.json")
        })
    });
    if any_profile == Some(false) {
        return None;
    }
    let names = match store::checked_path(root).and_then(|root| store::list(&root)) {
        Ok(names) if names.is_empty() => return None,
        Ok(names) => names,
        Err(Fail::Tunnel(reason)) => return Some(vec![unavailable(None, reason, store_recovery)]),
        Err(Fail::Detail(reason)) => return Some(vec![unavailable(None, &reason, store_recovery)]),
        Err(Fail::Defer) => {
            return Some(vec![unavailable(
                None,
                "the tunnel directory holds entries this client does not read",
                store_recovery,
            )]);
        }
    };
    let entries = names
        .iter()
        .map(|name| {
            let recovery = format!(
                "Check that {} is an owner-only file holding a tunnel profile; if it is left from an abandoned setup, move it aside and save it again with tunnel setup --daemon-url and --token-file.",
                store::profile_file(root, name).display()
            );
            let profile = match store::load(root, name) {
                Ok(profile) => profile,
                Err(Fail::Tunnel(reason)) => return unavailable(Some(name), reason, recovery),
                Err(Fail::Detail(reason)) => return unavailable(Some(name), &reason, recovery),
                Err(Fail::Defer) => {
                    return unavailable(
                        Some(name),
                        "the profile is in a form this client does not read",
                        recovery,
                    );
                }
            };
            let now = Utc::now();
            let (key_expiry, expired) = match &profile.expires_at {
                None => (json!({"known": false, "expired": null}), None),
                Some((text, instant)) => (
                    json!({"known": true, "expires_at": text, "expired": *instant <= now}),
                    (*instant <= now).then_some(text),
                ),
            };
            let key_state = store::key_state(root, name);
            let key = match key_state {
                Ok(Some(present)) => json!(present),
                // A Windows key this session cannot unseal, or a key file
                // this client does not open: not a claim either way.
                Ok(None) | Err(_) => json!("not_verified"),
            };
            // `None` when the path cannot be inspected: not a claim either way.
            let process_record = occupied(&root.join(format!("{name}.process.json"))).ok();
            let mut next = Vec::new();
            let update = match refresh_status(root, name) {
                Ok(refresh) => {
                    if refresh.needs_attention() {
                        let inspect = status_blocker(root, name, &key_state).unwrap_or_else(|| {
                            format!(
                                "pseudolife-mcp tunnel status --profile {name} --json shows the outcome and the key's state"
                            )
                        });
                        next.push(format!(
                            "Last tunnel refresh: {}; needs attention: {inspect}.",
                            refresh.state()
                        ));
                    }
                    let mut update = json!({"state": refresh.state(),
                        "needs_attention": refresh.needs_attention()});
                    if let Refresh::Done { rolled_back, .. } = refresh {
                        update["rolled_back"] = json!(rolled_back);
                    }
                    update
                }
                Err(_) => json!("not_checked"),
            };
            if key_state == Ok(None) {
                next.push(format!(
                    "The saved tunnel key {} cannot be unlocked in this session: a Windows-protected key opens only for the account that saved it, in a logon signed in with its password (not a key-based SSH logon), so run this check from such a logon.",
                    store::key_path(root, name).display()
                ));
            }
            if let Some(date) = expired {
                next.push(format!(
                    "The tunnel key expired on {date}; create a new restricted key at {KEYS_URL}; this client cannot save it yet."
                ));
            }
            let mut entry = json!({
                "profile": profile.name,
                "state": profile.state,
                "catalog": profile.catalog,
                "private_key_present": key,
                "key_expiry": key_expiry,
                "update": update,
                "runtime": {"checked": false, "process_record": process_record},
                "cloud": "not_checked",
            });
            if !next.is_empty() {
                entry["next"] = json!(next);
            }
            entry
        })
        .collect();
    Some(entries)
}
