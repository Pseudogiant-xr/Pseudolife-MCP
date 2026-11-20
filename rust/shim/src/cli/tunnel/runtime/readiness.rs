//! Local startup contract for supervisors and their attached bridges.
//!
//! Refresh and readiness protocol v1 first ship together. A v1 supervisor
//! requires exactly v1 from a verified candidate before stopping its child.
//! Higher, lower and missing versions are refusals. Every rollback image
//! already held v1 readiness on this supervisor. A bridge can still serve a
//! pre-protocol supervisor without publishing an acknowledgment.
use std::{
    fs::File,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

use hmac::Mac;
use serde::{Deserialize, Serialize};
use tokio::io::AsyncReadExt;
use uuid::Uuid;

use super::{identity, launch_id, state};

const VERSION: u32 = 1;
const QUERY_LIMIT: usize = 64;
const QUERY_TIMEOUT: Duration = Duration::from_secs(3);
const PREFIX: &str = "bridge-ready-";
const DOMAIN: &[u8] = b"pseudolife/tunnel-bridge-ready/1\0";
pub(in crate::cli::tunnel) const QUERY: &str = "--readiness-protocol";
const INVALID: &str = "the bridge readiness attempt does not match the signed launch";

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Protocol {
    version: u32,
}

pub(in crate::cli::tunnel) fn protocol_response() -> String {
    format!("{{\"version\":{VERSION}}}\n")
}

fn compatible(bytes: &[u8]) -> bool {
    bytes.len() <= QUERY_LIMIT
        && bytes.iter().find(|byte| !byte.is_ascii_whitespace()) == Some(&b'{')
        && serde_json::from_slice::<Protocol>(bytes).is_ok_and(|answer| answer.version == VERSION)
}

/// `executable` is the supervisor's verified installed image held by its
/// usage pin. This fixed command opens no store or network connection.
pub(super) async fn query(executable: &Path) -> bool {
    let mut command = tokio::process::Command::new(executable);
    command
        .args(["tunnel", "shim", QUERY])
        .env_clear()
        .kill_on_drop(true)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(Stdio::piped());
    #[cfg(windows)]
    command.creation_flags(crate::cli::restore_cli::child_window::child_window_flag());
    let Ok(mut child) = command.spawn() else {
        return false;
    };
    let Some(stdout) = child.stdout.take() else {
        return false;
    };
    let result = tokio::time::timeout(QUERY_TIMEOUT, async {
        let mut answer = Vec::new();
        stdout
            .take(QUERY_LIMIT as u64 + 1)
            .read_to_end(&mut answer)
            .await?;
        if !compatible(&answer) {
            return Ok::<_, std::io::Error>(false);
        }
        Ok(child.wait().await?.success())
    })
    .await;
    if matches!(result, Ok(Ok(true))) {
        return true;
    }
    let _ = child.start_kill();
    let _ = tokio::time::timeout(QUERY_TIMEOUT, child.wait()).await;
    false
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Attempt {
    version: u32,
    pub(super) id: String,
    tag: [u8; 32],
}

fn signer(
    root: &Path,
    generation: &str,
    identity: &str,
    id: &str,
) -> Result<hmac::Hmac<sha2::Sha256>, &'static str> {
    let mut signer = identity::mac(root)?;
    signer.update(DOMAIN);
    signer.update(&VERSION.to_be_bytes());
    signer.update(generation.as_bytes());
    signer.update(identity.as_bytes());
    signer.update(id.as_bytes());
    Ok(signer)
}

impl Attempt {
    pub(super) fn new(root: &Path, generation: &str, identity: &str) -> Result<Self, &'static str> {
        let id = Uuid::new_v4().simple().to_string();
        let tag = signer(root, generation, identity, &id)?
            .finalize()
            .into_bytes()
            .into();
        Ok(Self {
            version: VERSION,
            id,
            tag,
        })
    }

    pub(super) fn validate(
        &self,
        root: &Path,
        generation: &str,
        identity: &str,
        requested: &str,
    ) -> Result<(), &'static str> {
        if self.version != VERSION
            || self.id != requested
            || launch_id(requested).is_none()
            || signer(root, generation, identity, requested)?
                .verify_slice(&self.tag)
                .is_err()
        {
            return Err(INVALID);
        }
        Ok(())
    }

    pub(super) fn path(&self, directory: &Path) -> PathBuf {
        lock_path(directory, &self.id)
    }

    pub(super) fn held(&self, directory: &Path) -> bool {
        state::open_with_write(&self.path(directory), true).is_ok_and(|lock| {
            matches!(
                lock.file.try_lock_shared(),
                Err(std::fs::TryLockError::WouldBlock)
            )
        })
    }
}

pub(super) fn lock_path(directory: &Path, id: &str) -> PathBuf {
    directory.join(format!("{PREFIX}{id}.lock"))
}

pub(super) fn lock_name(name: &str) -> bool {
    name.strip_prefix(PREFIX)
        .and_then(|id| id.strip_suffix(".lock"))
        .and_then(launch_id)
        .is_some()
}

pub(in crate::cli::tunnel) fn hold(path: &Path) -> Result<File, &'static str> {
    let failed = "the bridge could not acknowledge its launch readiness";
    let lock = state::create(path).map_err(|_| failed)?.file;
    lock.try_lock().map_err(|_| failed)?;
    Ok(lock)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Root(PathBuf);

    impl Root {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!("bridge-ready-{}", Uuid::new_v4()));
            state::private_dir(&root).unwrap();
            identity::ensure_signing_key(&root).unwrap();
            Self(root)
        }
    }

    impl Drop for Root {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn readiness_protocol_requires_an_exact_integer_answer() {
        assert!(compatible(protocol_response().as_bytes()));
        for bytes in [
            b"{\"version\":0}".as_slice(),
            b"{\"version\":2}",
            b"{\"version\":1.0}",
            b"{\"version\":\"1\"}",
            b"{\"version\":1,\"version\":1}",
            b"{\"version\":1,\"unused\":true}",
            b"[1]",
            b"{}",
            b"{\"version\":18446744073709551616}",
        ] {
            assert!(!compatible(bytes), "{bytes:?}");
        }
        let oversized = format!("{}{}", " ".repeat(QUERY_LIMIT), protocol_response());
        assert!(!compatible(oversized.as_bytes()));
    }

    #[test]
    fn signed_readiness_attempt_cannot_cross_launch_generations() {
        let root = Root::new();
        let generation = Uuid::new_v4().simple().to_string();
        let other = Uuid::new_v4().simple().to_string();
        let image = "a".repeat(64);
        let attempt = Attempt::new(&root.0, &generation, &image).unwrap();
        assert!(
            attempt
                .validate(&root.0, &generation, &image, &attempt.id)
                .is_ok()
        );
        assert!(
            attempt
                .validate(&root.0, &other, &image, &attempt.id)
                .is_err()
        );
    }

    #[test]
    fn prior_attempt_lock_cannot_acknowledge_the_current_attempt() {
        let root = Root::new();
        let generation = Uuid::new_v4().simple().to_string();
        let image = "a".repeat(64);
        let first = Attempt::new(&root.0, &generation, &image).unwrap();
        let next = Attempt::new(&root.0, &generation, &image).unwrap();
        let _previous_bridge = hold(&first.path(&root.0)).unwrap();
        assert!(first.held(&root.0));
        assert!(
            next.validate(&root.0, &generation, &image, &first.id)
                .is_err()
        );
        assert!(!next.held(&root.0));
    }

    #[test]
    fn unlocked_readiness_file_does_not_acknowledge_a_launch() {
        let root = Root::new();
        let attempt = Attempt::new(
            &root.0,
            &Uuid::new_v4().simple().to_string(),
            &"a".repeat(64),
        )
        .unwrap();
        let path = attempt.path(&root.0);
        let created = state::create(&path).unwrap();
        assert_eq!(created.file.metadata().unwrap().len(), 0);
        assert!(path.is_file());
        assert!(!attempt.held(&root.0));
    }
}
