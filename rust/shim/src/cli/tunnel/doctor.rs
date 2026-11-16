//! `doctor`'s saved-tunnel section. Every step it names is one this client
//! runs, and only where that step answers: where native `tunnel status`
//! cannot read a file, the section names that file instead.
use std::{io, path::Path};

use chrono::Utc;
use serde_json::{Map, Value, json};

use super::{Fail, Refresh, occupied, refresh_status, shallow_challenge, store};

/// Where a new restricted key is created (the Python client's `KEYS_URL`).
const KEYS_URL: &str = "https://platform.openai.com/settings/organization/api-keys";

/// The file native `tunnel status --profile <name>` cannot read, in the
/// order it reads them after the profile, or `None` when it answers.
fn status_blocker(root: &Path, name: &str) -> Option<(&'static str, std::path::PathBuf)> {
    if store::key_state(root, name) == Err(Fail::Defer) {
        return Some(("the key file", store::key_path(root, name)));
    }
    if shallow_challenge(root, name) == Err(Fail::Defer) {
        let path = root
            .join(format!("{name}-verification"))
            .join("challenge.json");
        return Some(("the verification challenge", path));
    }
    None
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
                "Check that {} is an owner-only file holding a tunnel profile; if it is left from an abandoned setup, move it aside; this client cannot set a tunnel up yet.",
                store::profile_file(root, name).display()
            );
            let profile = match store::load(root, name) {
                Ok(profile) => profile,
                Err(Fail::Tunnel(reason)) => return unavailable(Some(name), reason, recovery),
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
                        let inspect = match status_blocker(root, name) {
                            Some((what, path)) => format!(
                                "pseudolife-mcp tunnel status --profile {name} cannot read {what} {}; check that it is an owner-only file",
                                path.display()
                            ),
                            None => format!(
                                "pseudolife-mcp tunnel status --profile {name} shows the outcome and the key's state"
                            ),
                        };
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
                    "The saved tunnel key {} cannot be unlocked in this session: a Windows-protected key opens only for the account that saved it, signed in with its password (not over a key-based SSH logon), so run this check from such a session.",
                    store::key_path(root, name).display()
                ));
            }
            if let Some(date) = expired {
                next.push(format!(
                    "The tunnel key expired on {date}; create a new restricted key at {KEYS_URL}."
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
