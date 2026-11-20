//! Select the shim for the next stdio session of an owned vendor process.
//! The vendor stays the sole poller. A bridge holds the selection lock for
//! its whole session, so refresh cannot replace an initialized transport.
use std::{
    fs::{self, File},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};

use super::{
    LaunchRecord, identity, key::Key, launch_id, read_record, record, setup, stage_path, store,
    vendor,
};
use crate::{board::state, credentials::CredentialProvider, runtime};

pub(super) const BRIDGE_LOCK: &str = "bridge.lock";
const BUSY: &str =
    "the bridge has an active stdio session. Stop and run the same profile to restart it";
const CHANGED: &str =
    "the saved settings or launch ownership changed. Check tunnel status before retrying";
const UPGRADE: &str =
    "the bridge cannot use this vendor version. Stop and run the same profile to restart it";
const UNSAFE: &str = "the bridge selection cannot be verified. Check tunnel status before retrying";
const INSTALL: &str = "the candidate is not a verified installed client. Run tunnel update through the installed launcher";
const RECOVERY: &str =
    "key renewal or refresh needs attention. Check tunnel status before retrying";
const HANDSHAKE: &str =
    "the daemon did not complete the refresh handshake. Run pseudolife-mcp doctor before retrying";
const PUBLISH: &str =
    "the bridge selection could not be saved. Check access to the tunnel store before retrying";
const CUSTOM_STORE: &str = "the installer can retain selected runtimes only for the default tunnel store. Stop and run the same profile to restart it";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct Protocol {
    schema: u8,
    pub(super) version: String,
    vendor: String,
}

impl Protocol {
    pub(super) fn current() -> Self {
        Self {
            schema: 1,
            version: env!("CARGO_PKG_VERSION").into(),
            vendor: vendor::VERSION.into(),
        }
    }
}

pub(super) fn selection(launch: &LaunchRecord) -> Result<(&Path, &Protocol), &'static str> {
    let protocol = launch.bridge.as_ref().ok_or(UNSAFE)?;
    if protocol.schema != 1 || protocol.vendor != vendor::VERSION {
        return Err(UPGRADE);
    }
    let path = launch.executable.as_deref().ok_or(UNSAFE)?;
    if !path.is_absolute()
        || !runtime::valid_version(&protocol.version)
        || store::checked_path(path).is_err()
        || !runtime::plain_file(path)
    {
        return Err(UNSAFE);
    }
    Ok((path, protocol))
}

fn try_gate(directory: &Path) -> Result<Option<File>, &'static str> {
    let gate = state::open_with_write(&directory.join(BRIDGE_LOCK), true)
        .map_err(|_| UNSAFE)?
        .file;
    match gate.try_lock() {
        Ok(()) => Ok(Some(gate)),
        Err(fs::TryLockError::WouldBlock) => Ok(None),
        Err(_) => Err(UNSAFE),
    }
}

pub(super) fn wait_for_gate(directory: &Path) -> Result<File, &'static str> {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(gate) = try_gate(directory)? {
            return Ok(gate);
        }
        if Instant::now() >= deadline {
            return Err(BUSY);
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(in crate::cli::tunnel) enum Selection {
    Refreshed,
    Unconfirmed,
}

/// Older records have no session fence. Leave their established update
/// reporting and explicit stop/run route in place.
pub(in crate::cli::tunnel) fn attempt(
    root: &Path,
    name: &str,
) -> Option<Result<Selection, &'static str>> {
    let launch = read_record(&record::path(root, name)).ok()?;
    launch.bridge.as_ref()?;
    if !runtime::same_path(root, &runtime::home().join(".pseudolife-mcp/tunnel")) {
        return Some(Err(CUSTOM_STORE));
    }
    let installed = || {
        let layout = runtime::default_layout().ok()?;
        let running = runtime::running_runtime(&layout)?;
        let current = runtime::current_runtime(&layout)?;
        let executable = running.executable();
        #[cfg(unix)]
        if rustix::process::geteuid().is_root() {
            super::trusted_root_paths([root, &executable]).ok()?;
        }
        (running.sequence == current.sequence).then_some(executable)
    };
    Some(activate(root, name, launch, installed, |profile| {
        #[cfg(unix)]
        if rustix::process::geteuid().is_root() {
            super::trusted_root_paths([Path::new(&profile.token_file)]).map_err(|_| CHANGED)?;
        }
        setup::verify_connection(profile)
            .map(|_| ())
            .map_err(|_| HANDSHAKE)
    }))
}

pub(super) fn activate(
    root: &Path,
    name: &str,
    launch: LaunchRecord,
    installed: impl Fn() -> Option<PathBuf>,
    probe: impl FnOnce(&store::Profile) -> Result<(), &'static str>,
) -> Result<Selection, &'static str> {
    let record_path = record::path(root, name);
    let generation = launch_id(&launch.native_launch).ok_or(UNSAFE)?;
    let stage = stage_path(root, generation);
    let owned = || launch.ready && identity::owns_generation(root, name, &stage);
    if !owned() {
        return Err(CHANGED);
    }
    // Do not wait for an established session to finish. A probe is run only
    // after the first gate check, with the gate open to incoming sessions.
    drop(try_gate(&stage)?.ok_or(BUSY)?);
    let (prior, protocol) = selection(&launch)?;
    let check_recovery = || {
        if setup::check_pending(root, name).is_err()
            || !matches!(setup::validate_previous_key(root, name), Ok(false))
            || !super::super::refresh_idle(root, name)
        {
            return Err(RECOVERY);
        }
        Ok(())
    };
    let verify_prior = |profile: &store::Profile| {
        let expected = identity::expected(root, profile, prior, &protocol.version)?;
        if launch.launch_identity.as_deref() != Some(&expected) {
            return Err(CHANGED);
        }
        Ok(())
    };
    check_recovery()?;
    let profile_path = store::profile_path(root, name).map_err(|_| CHANGED)?;
    let profile_bytes =
        store::private_read(&profile_path, super::RECORD_LIMIT).map_err(|_| CHANGED)?;
    let (_, profile) = store::decode_profile(&profile_bytes, name).map_err(|_| CHANGED)?;
    if profile
        .expires_at
        .as_ref()
        .is_some_and(|(_, expires)| *expires <= chrono::Utc::now())
    {
        return Err(super::KEY_RENEWAL);
    }
    let provider =
        CredentialProvider::new(None, Some((&profile.token_file).into())).map_err(|_| CHANGED)?;
    let credential = provider.snapshot().map_err(|_| CHANGED)?;
    let key_digest = Key::load(root, name).map_err(|_| CHANGED)?.digest();
    verify_prior(&profile)?;
    let candidate = installed().ok_or(INSTALL)?;
    probe(&profile)?;
    verify_prior(&profile)?;
    if installed().as_ref() != Some(&candidate) {
        return Err(INSTALL);
    }
    let identity = identity::expected(root, &profile, &candidate, env!("CARGO_PKG_VERSION"))?;

    let _gate = try_gate(&stage)?.ok_or(BUSY)?;
    let original = state::open(&record_path).map_err(|_| CHANGED)?.identity;
    check_recovery()?;
    if !owned()
        || read_record(&record_path).ok().as_ref() != Some(&launch)
        || !store::private_read(&profile_path, super::RECORD_LIMIT)
            .is_ok_and(|bytes| bytes == profile_bytes)
        || provider.require_current(&credential).is_err()
        || !Key::load(root, name).is_ok_and(|key| key.digest() == key_digest)
    {
        return Err(CHANGED);
    }
    let next = LaunchRecord {
        executable: Some(candidate),
        bridge: Some(Protocol::current()),
        launch_identity: Some(identity),
        ..launch
    };
    let bytes = serde_json::to_vec(&next).map_err(|_| PUBLISH)?;
    // The one record replacement selects the executable and its receipt
    // identity together. No pending file can block a later command.
    match state::atomic_write(&record_path, &bytes, Some(&original)) {
        Ok(()) => Ok(Selection::Refreshed),
        // A directory sync may fail after rename. Report the visible
        // selection without calling the commit durable.
        Err(_) if read_record(&record_path).ok().as_ref() == Some(&next) => {
            Ok(Selection::Unconfirmed)
        }
        Err(_) => Err(PUBLISH),
    }
}

#[cfg(test)]
mod tests {
    use super::super::tests::{Home, launch_fixture, save_fixture_key};
    use super::super::{Identity, Prepared, Stage};
    use super::*;
    use uuid::Uuid;

    fn launch_for_refresh<'a>(
        home: &'a Home,
        executable: &Path,
    ) -> (Prepared<'a>, Stage, LaunchRecord) {
        let prepared = launch_fixture(&home.0);
        state::atomic_create(
            &store::profile_path(&home.0, "dot").unwrap(),
            &prepared.profile_bytes,
        )
        .unwrap();
        let id = Uuid::new_v4();
        let mut stage = Stage::new(&home.0, "dot", id).unwrap();
        let launch = LaunchRecord {
            native_launch: id.simple().to_string(),
            launch_identity: Some(
                identity::compute(&home.0, &prepared.profile, &prepared.key, executable).unwrap(),
            ),
            executable: Some(executable.to_owned()),
            bridge: Some(Protocol::current()),
            spawn_started: Some(true),
            supervisor: Identity {
                pid: std::process::id(),
                exe: executable.to_owned(),
            },
            target: Some(Identity {
                pid: std::process::id(),
                exe: executable.to_owned(),
            }),
            ready: true,
        };
        stage.announce(&launch).unwrap();
        (prepared, stage, launch)
    }

    #[test]
    fn current_stdio_session_excludes_a_second_bridge_or_refresh() {
        let home = Home::new();
        let stage = Stage::new(&home.0, "dot", Uuid::new_v4()).unwrap();
        let session = try_gate(&stage.directory).unwrap().unwrap();
        assert!(try_gate(&stage.directory).unwrap().is_none());
        drop(session);
        assert!(try_gate(&stage.directory).unwrap().is_some());
    }

    #[test]
    fn refresh_preserves_old_selection_across_failure_and_interruption() {
        let home = Home::new();
        if !save_fixture_key(&home.0, "dot") {
            return;
        }
        let current = std::env::current_exe().unwrap();
        let (_prepared, stage, initial) = launch_for_refresh(&home, &current);
        let candidate = || Some(current.clone());
        let run = |probe: fn(&store::Profile) -> Result<(), &'static str>| {
            activate(
                &home.0,
                "dot",
                read_record(&stage.record).unwrap(),
                candidate,
                probe,
            )
        };
        let guarded = try_gate(&stage.directory).unwrap().unwrap();
        assert_eq!(
            run(|_| panic!("a live stdio session must skip the probe")),
            Err(BUSY)
        );
        drop(guarded);
        assert_eq!(run(|_| Err(HANDSHAKE)), Err(HANDSHAKE));
        // Cancellation before commit has no request or credential files to
        // recover. Catching the unwind models a vanished requesting process.
        assert!(std::panic::catch_unwind(|| run(|_| panic!("interrupt before commit"))).is_err());
        assert_eq!(read_record(&stage.record).unwrap(), initial);
        let token = home.0.join("daemon-token");
        state::atomic_write(&token, b"moved-fixture-credential", None).unwrap();
        assert_eq!(
            run(|_| panic!("a different connection cannot refresh")),
            Err(CHANGED)
        );
        state::atomic_write(&token, b"fixture-daemon-token", None).unwrap();

        let mut racing_session = None;
        let raced = activate(&home.0, "dot", initial, candidate, |_| {
            racing_session = try_gate(&stage.directory).unwrap();
            Ok(())
        });
        assert_eq!(raced, Err(BUSY));
        assert_eq!(
            read_record(&stage.record).unwrap().executable.as_deref(),
            Some(current.as_path())
        );
        drop(racing_session);
        assert!(identity::current(&home.0, "dot").is_ok());
    }

    #[test]
    fn refresh_selects_new_identity_for_next_session_without_replacing_poller() {
        let home = Home::new();
        if !save_fixture_key(&home.0, "dot") {
            return;
        }
        let prior = std::env::current_exe().unwrap();
        let (prepared, mut stage, mut initial) = launch_for_refresh(&home, &prior);
        // Use an older recorded shim version to model an installed upgrade
        // without starting a second vendor process.
        initial.bridge.as_mut().unwrap().version = "0.0.1".into();
        initial.launch_identity =
            Some(identity::expected(&home.0, &prepared.profile, &prior, "0.0.1").unwrap());
        stage.announce(&initial).unwrap();
        let prior_id = initial.launch_identity.clone();
        let generation = initial.native_launch.clone();
        let outcome = activate(&home.0, "dot", initial, || Some(prior.clone()), |_| Ok(()));
        assert_eq!(outcome, Ok(Selection::Refreshed));
        let selected = read_record(&stage.record).unwrap();
        assert_ne!(selected.launch_identity, prior_id);
        assert_eq!(selected.native_launch, generation);
        assert_eq!(selected.target.as_ref().unwrap().pid, std::process::id());
        let bound =
            identity::Bound::capture(&stage.directory, &generation, &prepared.profile).unwrap();
        assert_eq!(Some(bound.expected().unwrap()), selected.launch_identity);
        assert!(identity::current(&home.0, "dot").is_ok());
        assert!(try_gate(&stage.directory).unwrap().is_none());
    }
}
