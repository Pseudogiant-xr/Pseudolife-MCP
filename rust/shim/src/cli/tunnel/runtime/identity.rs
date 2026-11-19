//! Verification is bound to stable launch inputs and to the bridge's own
//! live generation. Relaunching the same inputs preserves the identity.
use std::{
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
};

use serde_json::json;
use sha2::{Digest, Sha256};

use super::{
    key::Key, launch_id, lock_path, read_record, record, setup, stage_path, store, vendor,
};

pub(in crate::cli::tunnel) const CHANGED: &str = "the saved tunnel identity differs from the active launch. Stop and run the tunnel again before verification";
const UNAVAILABLE: &str =
    "the native tunnel launch identity is unavailable. Check tunnel status before verification";

pub(super) fn compute(
    profile: &store::Profile,
    key: &Key,
    executable: &Path,
) -> Result<String, super::Fail> {
    compute_identity(profile, key, executable).map_err(super::Fail::Tunnel)
}

fn compute_identity(
    profile: &store::Profile,
    key: &Key,
    executable: &Path,
) -> Result<String, &'static str> {
    let mut file = File::open(executable).map_err(|_| UNAVAILABLE)?;
    let mut buffer = [0; 8192];
    let mut hasher = Sha256::new();
    loop {
        let count = file.read(&mut buffer).map_err(|_| UNAVAILABLE)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    let provider =
        crate::credentials::CredentialProvider::new(None, Some((&profile.token_file).into()))
            .map_err(|_| UNAVAILABLE)?;
    let snapshot = provider.snapshot().map_err(|_| UNAVAILABLE)?;
    let token = snapshot.token().ok_or(UNAVAILABLE)?;
    let inputs = json!({
        "schema": 1,
        "runtime_digest": format!("{:x}", hasher.finalize()),
        "runtime_version": env!("CARGO_PKG_VERSION"),
        "vendor_version": vendor::VERSION,
        "profile": {
            "name": profile.name,
            "daemon_url": profile.daemon_url,
            "token_file": profile.token_file,
            "tunnel_id": profile.tunnel_id,
            "organization_id": profile.organization_id,
            "minimum_catalog": profile.catalog,
            "runtime_version": profile.runtime_version.as_deref().unwrap_or(vendor::VERSION),
        },
        "daemon_credential": format!("{:x}", Sha256::digest(token.as_bytes())),
        "runtime_key": format!("{:x}", key.digest()),
    });
    provider.require_current(&snapshot).map_err(|_| CHANGED)?;
    Ok(format!(
        "{:x}",
        Sha256::digest(inputs.to_string().as_bytes())
    ))
}

fn held(root: &Path, name: &str) -> bool {
    setup::check_directory(root).is_ok()
        && crate::board::state::open_with_write(&lock_path(root, name), true)
            .is_ok_and(|lock| matches!(lock.file.try_lock(), Err(fs::TryLockError::WouldBlock)))
}

fn expected(
    root: &Path,
    profile: &store::Profile,
    executable: &Path,
) -> Result<String, &'static str> {
    if profile.state != "ready" {
        return Err(CHANGED);
    }
    let key = Key::load(root, &profile.name).map_err(|_| UNAVAILABLE)?;
    compute_identity(profile, &key, executable)
}

pub(in crate::cli::tunnel) fn active(
    root: &Path,
    name: &str,
    executable: &Path,
    generation: Option<&str>,
) -> Result<String, &'static str> {
    let path = record::path(root, name);
    let record = read_record(&path).map_err(|_| UNAVAILABLE)?;
    if generation.is_some_and(|id| record.native_launch != id) {
        return Err(CHANGED);
    }
    if !record.ready || !held(root, name) {
        return Err(UNAVAILABLE);
    }
    let profile = store::load(root, name).map_err(|_| UNAVAILABLE)?;
    let identity = expected(root, &profile, executable)?;
    let current = read_record(&path).map_err(|_| UNAVAILABLE)?;
    if current.native_launch != record.native_launch
        || !current.ready
        || record.launch_identity.as_deref() != Some(&identity)
        || current.launch_identity != record.launch_identity
        || !held(root, name)
    {
        return Err(CHANGED);
    }
    Ok(identity)
}

#[cfg(all(test, unix))]
pub(in crate::cli::tunnel) fn fixture_identity(
    root: &Path,
    profile: &store::Profile,
    executable: &Path,
) -> String {
    expected(root, profile, executable).unwrap()
}

pub(in crate::cli::tunnel) fn current(root: &Path, name: &str) -> Result<String, &'static str> {
    let executable = std::env::current_exe().map_err(|_| UNAVAILABLE)?;
    active(root, name, &executable, None)
}

pub(in crate::cli::tunnel) struct Bound {
    pub(in crate::cli::tunnel) root: PathBuf,
    pub(in crate::cli::tunnel) identity: String,
    generation: String,
    name: String,
    executable: PathBuf,
}

impl Bound {
    pub(in crate::cli::tunnel) fn capture(
        stage: &Path,
        generation: &str,
        profile: &store::Profile,
    ) -> Result<Self, &'static str> {
        let id = launch_id(generation).ok_or(UNAVAILABLE)?;
        let root = stage.parent().ok_or(UNAVAILABLE)?;
        if stage_path(root, id) != stage || !held(root, &profile.name) {
            return Err(UNAVAILABLE);
        }
        setup::check_directory(stage).map_err(|_| UNAVAILABLE)?;
        let executable = std::env::current_exe().map_err(|_| UNAVAILABLE)?;
        let identity = expected(root, profile, &executable)?;
        // The bridge is what gets the vendor ready. Check the record even
        // while starting, but require readiness before accepting a receipt.
        let record = read_record(&record::path(root, &profile.name)).map_err(|_| UNAVAILABLE)?;
        if record.native_launch != generation
            || record.launch_identity.as_deref() != Some(&identity)
        {
            return Err(CHANGED);
        }
        Ok(Self {
            root: root.to_owned(),
            identity,
            generation: generation.to_owned(),
            name: profile.name.clone(),
            executable,
        })
    }

    pub(in crate::cli::tunnel) fn expected(&self) -> Result<String, &'static str> {
        active(
            &self.root,
            &self.name,
            &self.executable,
            Some(&self.generation),
        )
    }
}
