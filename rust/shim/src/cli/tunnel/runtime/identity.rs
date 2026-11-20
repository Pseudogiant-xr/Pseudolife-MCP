//! Verification is bound to stable launch inputs and to the bridge's own
//! live generation. Relaunching the same inputs preserves the identity.
use std::{
    fs::{self, File},
    io::{Read, Write},
    path::{Path, PathBuf},
};

use hmac::{Hmac, Mac};
use serde_json::json;
use sha2::{Digest, Sha256};

use super::{
    OWNER_LOCK, key::Key, launch_id, lock_path, read_record, record, setup, stage_path, store,
    vendor,
};

pub(in crate::cli::tunnel) const CHANGED: &str = "the saved tunnel identity differs from the active launch. Stop and run the tunnel again before verification";
const UNAVAILABLE: &str =
    "the native tunnel launch identity is unavailable. Check tunnel status before verification";

pub(super) fn compute(
    root: &Path,
    profile: &store::Profile,
    key: &Key,
    executable: &Path,
) -> Result<String, super::Fail> {
    ensure_signing_key(root).map_err(super::Fail::Tunnel)?;
    compute_identity(root, profile, key, executable).map_err(super::Fail::Tunnel)
}

const SIGNING_KEY: &str = ".tunnel-identity-key";

fn ensure_signing_key(root: &Path) -> Result<(), &'static str> {
    let path = root.join(SIGNING_KEY);
    if !store::occupied(&path).map_err(|_| UNAVAILABLE)? {
        let mut key = [0; 32];
        getrandom::fill(&mut key).map_err(|_| UNAVAILABLE)?;
        let result = setup::publish_new(&path, |file| file.write_all(&key));
        key.fill(0);
        std::hint::black_box(&key);
        if result.is_err() && !store::occupied(&path).map_err(|_| UNAVAILABLE)? {
            return Err(UNAVAILABLE);
        }
    }
    Ok(())
}

fn mac(root: &Path) -> Result<Hmac<Sha256>, &'static str> {
    let mut key = store::private_read(&root.join(SIGNING_KEY), 32).map_err(|_| UNAVAILABLE)?;
    let signer = if key.len() == 32 {
        Hmac::new_from_slice(&key).map_err(|_| UNAVAILABLE)
    } else {
        Err(UNAVAILABLE)
    };
    key.fill(0);
    std::hint::black_box(&key);
    signer
}

#[cfg(test)]
#[test]
fn signing_requires_an_existing_owner_only_store_key() {
    let first = std::env::temp_dir().join(format!("tunnel-mac-{}", uuid::Uuid::new_v4()));
    let second = first.join("second");
    let roots = [&first, &second];
    for root in roots {
        crate::board::state::private_dir(root).unwrap();
        assert!(mac(root).is_err());
        ensure_signing_key(root).unwrap();
    }
    let sign = |root: &Path| {
        let mut signer = mac(root).unwrap();
        signer.update(b"same launch fields");
        signer.finalize().into_bytes()
    };
    let original = sign(&first);
    ensure_signing_key(&first).unwrap();
    assert!(original == sign(&first) && original != sign(&second));
    fs::remove_dir_all(first).unwrap();
}

fn compute_identity(
    root: &Path,
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
        "schema": 2,
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
    let mut signer = mac(root)?;
    signer.update(inputs.to_string().as_bytes());
    Ok(format!("{:x}", signer.finalize().into_bytes()))
}

fn held(path: &Path) -> bool {
    crate::board::state::open_with_write(path, true).is_ok_and(|lock| {
        matches!(
            lock.file.try_lock_shared(),
            Err(fs::TryLockError::WouldBlock)
        )
    })
}

fn owns_generation(root: &Path, name: &str, stage: &Path) -> bool {
    // Startup and recovery both acquire the profile lock. Only the
    // supervisor holds the second lock for this particular generation.
    setup::check_directory(stage).is_ok()
        && held(&stage.join(OWNER_LOCK))
        && held(&lock_path(root, name))
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
    compute_identity(root, profile, &key, executable)
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
    let id = launch_id(&record.native_launch).ok_or(UNAVAILABLE)?;
    let stage = stage_path(root, id);
    if !record.ready || !owns_generation(root, name, &stage) {
        return Err(UNAVAILABLE);
    }
    let profile = store::load(root, name).map_err(|_| UNAVAILABLE)?;
    let identity = expected(root, &profile, executable)?;
    let current = read_record(&path).map_err(|_| UNAVAILABLE)?;
    if current.native_launch != record.native_launch
        || !current.ready
        || record.launch_identity.as_deref() != Some(&identity)
        || current.launch_identity != record.launch_identity
        || !owns_generation(root, name, &stage)
    {
        return Err(CHANGED);
    }
    Ok(identity)
}

#[cfg(test)]
pub(in crate::cli::tunnel) fn fixture_identity(
    root: &Path,
    profile: &store::Profile,
    executable: &Path,
) -> String {
    ensure_signing_key(root).unwrap();
    expected(root, profile, executable).unwrap()
}

#[cfg(test)]
pub(in crate::cli::tunnel) fn fixture_owner(root: &Path, generation: &str) -> File {
    let stage = stage_path(root, launch_id(generation).unwrap());
    crate::board::state::private_dir(&stage).unwrap();
    let owner = crate::board::state::create(&stage.join(OWNER_LOCK))
        .unwrap()
        .file;
    owner.try_lock().unwrap();
    owner
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
        if stage_path(root, id) != stage || !owns_generation(root, &profile.name, stage) {
            return Err(UNAVAILABLE);
        }
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
