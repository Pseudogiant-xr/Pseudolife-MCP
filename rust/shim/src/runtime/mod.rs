//! Opt-in native, side-by-side client runtimes. No interpreter is used here.
mod cli;
mod config_file;
mod install;
mod launch;
mod prune;
pub mod registrations;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{self, Read},
    path::{Path, PathBuf},
};

pub use cli::run;
pub use install::{install_archive, install_binary, install_source, target_triple};
pub use launch::maybe_launch;
pub use prune::{Evidence, Process, collect_evidence, remove_unused};

pub const MARKER: &str = "runtime.json";
pub const BINARY: &str = if cfg!(windows) {
    "pseudolife-mcp.exe"
} else {
    "pseudolife-mcp"
};
pub type Result<T> = std::result::Result<T, String>;

#[derive(Clone, Debug)]
pub struct Layout {
    pub root: PathBuf,
    pub launcher: PathBuf,
    pub user_bin: Option<PathBuf>,
    pub user_path: bool,
}

pub fn home() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .or_else(dirs::home_dir)
        .unwrap_or_default()
}

pub fn default_layout() -> Result<Layout> {
    layout_from(|key| {
        std::env::var_os(key)
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
    })
}

pub fn layout_from(mut env: impl FnMut(&str) -> Option<PathBuf>) -> Result<Layout> {
    let root = env("PSEUDOLIFE_SHIM_RUNTIMES");
    let launcher = env("PSEUDOLIFE_SHIM_LAUNCHER");
    let user_bin = env("PSEUDOLIFE_SHIM_USER_BIN");
    if root.is_some() != launcher.is_some() {
        return Err(
            "set both PSEUDOLIFE_SHIM_RUNTIMES and PSEUDOLIFE_SHIM_LAUNCHER, or neither".into(),
        );
    }
    if let (Some(root), Some(launcher)) = (root, launcher) {
        if !root.is_absolute() || !launcher.is_absolute() {
            return Err("shim runtime and launcher overrides must be absolute paths".into());
        }
        if launcher
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case("exe"))
            != cfg!(windows)
        {
            return Err(
                "PSEUDOLIFE_SHIM_LAUNCHER must use the platform's executable suffix".into(),
            );
        }
        return Ok(Layout {
            root,
            launcher,
            user_bin: if cfg!(windows) { None } else { user_bin },
            user_path: false,
        });
    }
    let user = env("USERPROFILE")
        .or_else(|| env("HOME"))
        .or_else(dirs::home_dir)
        .ok_or("could not determine the client home")?;
    let base = if cfg!(windows) {
        env("LOCALAPPDATA").unwrap_or_else(|| user.join("AppData/Local"))
    } else {
        env("XDG_DATA_HOME").unwrap_or_else(|| user.join(".local/share"))
    }
    .join("pseudolife-mcp");
    Ok(Layout {
        root: base.join("runtimes"),
        launcher: base.join("bin").join(BINARY),
        user_bin: if cfg!(windows) {
            None
        } else {
            Some(user_bin.unwrap_or_else(|| user.join(".local/bin")))
        },
        user_path: cfg!(windows),
    })
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Marker {
    pub format: u32,
    pub kind: String,
    pub version: String,
    pub installed_at: String,
    pub source: String,
    pub source_commit: Option<String>,
    pub target: String,
    pub binary: String,
    pub binary_sha256: String,
    pub archive_sha256: Option<String>,
}

#[derive(Clone, Debug)]
pub struct Runtime {
    pub path: PathBuf,
    pub sequence: u32,
    pub version: String,
    pub source: String,
    pub source_commit: Option<String>,
    pub marker: Marker,
}
impl Runtime {
    pub fn executable(&self) -> PathBuf {
        self.path.join(BINARY)
    }
    /// The pin lives beside the runtime so Windows can quarantine its directory.
    pub fn usage_lock_path(&self) -> PathBuf {
        self.path
            .with_file_name(format!(".in-use-{}.lock", self.name()))
    }
    pub fn name(&self) -> String {
        format!("{:06}", self.sequence)
    }
}

pub fn sequence(name: &str) -> Option<u32> {
    (name.len() == 6 && name.bytes().all(|b| b.is_ascii_digit()))
        .then(|| name.parse().ok())
        .flatten()
}
pub fn valid_version(version: &str) -> bool {
    !version.is_empty()
        && version.len() <= 64
        && version.as_bytes()[0].is_ascii_digit()
        && version
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".+-".contains(&b))
}
pub fn valid_digest(digest: &str) -> bool {
    digest.len() == 64
        && digest
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn hash_file(path: &Path) -> Result<String> {
    let mut file = fs::File::open(path).map_err(|e| e.to_string())?;
    let mut digest = Sha256::new();
    let mut buf = [0; 65536];
    loop {
        let n = file.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        digest.update(&buf[..n]);
    }
    Ok(format!("{:x}", digest.finalize()))
}
pub fn plain_file(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|m| m.is_file() && !redirect(&m))
}
pub fn redirect(meta: &fs::Metadata) -> bool {
    if meta.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if meta.file_attributes() & 0x400 != 0 {
            return true;
        }
    }
    false
}
pub fn read_runtime(path: &Path) -> Result<Runtime> {
    read_runtime_with_digest(path, true)
}

fn read_runtime_with_digest(path: &Path, verify_digest: bool) -> Result<Runtime> {
    let sequence = path
        .file_name()
        .and_then(|n| n.to_str())
        .and_then(sequence)
        .ok_or("invalid runtime name")?;
    crate::credentials::reject_ancestor_redirects(path)
        .map_err(|_| "runtime path is redirected")?;
    let bytes = crate::board::state::read(&path.join(MARKER), 16384)
        .map_err(|_| "runtime marker is unavailable or not owner-only")?;
    let marker: Marker = serde_json::from_slice(&bytes).map_err(|_| "invalid runtime marker")?;
    if marker.format != 1
        || marker.kind != "native"
        || !valid_version(&marker.version)
        || marker.binary != BINARY
        || marker.target != target_triple()?
        || !valid_digest(&marker.binary_sha256)
        || marker
            .archive_sha256
            .as_ref()
            .is_some_and(|d| !valid_digest(d))
        || marker
            .source_commit
            .as_ref()
            .is_some_and(|s| s.len() != 40 || !s.bytes().all(|b| b.is_ascii_hexdigit()))
        || !plain_file(&path.join(BINARY))
        || (verify_digest && hash_file(&path.join(BINARY))? != marker.binary_sha256)
    {
        return Err("runtime marker or executable verification failed".into());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if fs::metadata(path.join(BINARY))
            .map_err(|e| e.to_string())?
            .permissions()
            .mode()
            & 0o111
            == 0
        {
            return Err("runtime executable has no execute permission".into());
        }
    }
    Ok(Runtime {
        path: path.to_owned(),
        sequence,
        version: marker.version.clone(),
        source: marker.source.clone(),
        source_commit: marker.source_commit.clone(),
        marker,
    })
}
pub fn list_runtimes(layout: &Layout) -> Vec<Runtime> {
    let mut found: Vec<_> = fs::read_dir(&layout.root)
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .filter_map(|e| read_runtime(&e.path()).ok())
        .collect();
    found.sort_by_key(|r| r.sequence);
    found
}
pub fn current_runtime(layout: &Layout) -> Option<Runtime> {
    newest_runtime(layout, true)
}

fn newest_runtime(layout: &Layout, verify_digest: bool) -> Option<Runtime> {
    // Verify candidates newest first. Old sessions can retain many large
    // binaries; starting a new session only needs the first valid candidate.
    let mut paths: Vec<_> = fs::read_dir(&layout.root)
        .ok()?
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let number = e.file_name().to_str().and_then(sequence)?;
            Some((number, e.path()))
        })
        .collect();
    paths.sort_by_key(|(number, _)| std::cmp::Reverse(*number));
    paths
        .into_iter()
        .find_map(|(_, path)| read_runtime_with_digest(&path, verify_digest).ok())
}
pub fn running_runtime(layout: &Layout) -> Option<Runtime> {
    let executable = std::env::current_exe().ok()?;
    let path = executable.parent()?;
    if !same_path(path.parent()?, &layout.root) {
        return None;
    }
    let runtime = read_runtime(path).ok()?;
    same_path(&executable, &runtime.executable()).then_some(runtime)
}
pub fn same_path(a: &Path, b: &Path) -> bool {
    fn norm(path: &Path) -> PathBuf {
        let path = fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
        #[cfg(windows)]
        {
            PathBuf::from(path.to_string_lossy().to_lowercase())
        }
        #[cfg(not(windows))]
        {
            path
        }
    }
    norm(a) == norm(b)
}

/// One native update owns selection/activation/pruning at a time; never stale PID locks.
pub struct UpdateLock(fs::File);
impl UpdateLock {
    pub fn acquire(layout: &Layout) -> Result<Self> {
        crate::board::state::private_dir(&layout.root).map_err(str::to_owned)?;
        let path = layout.root.join(".native-update.lock");
        let file = match crate::board::state::open_with_write(&path, true) {
            Ok(f) => f.file,
            Err(_) if !path.exists() => {
                crate::board::state::create(&path)
                    .map_err(str::to_owned)?
                    .file
            }
            Err(_) => {
                return Err("native runtime update lock is unavailable or not owner-only".into());
            }
        };
        file.try_lock().map_err(|_| {
            "another native runtime update is running; retry after it finishes".to_owned()
        })?;
        Ok(Self(file))
    }
}
impl Drop for UpdateLock {
    fn drop(&mut self) {
        let _ = self.0.unlock();
    }
}

pub fn update_shim(source: &str, repo: Option<&Path>, reinstall: bool) -> Value {
    let result = (|| -> Result<Value> {
        let layout = default_layout()?;
        let registrations = registrations::find_registrations()?;
        if registrations.is_empty() {
            return Ok(
                json!({"state":"not-registered", "detail":"no stdio registration of pseudolife-memory in Claude Code, Codex, Claude Desktop or Gemini CLI; rerun the installer with the intended client"}),
            );
        }
        let managed: Vec<_> = registrations
            .iter()
            .filter(|r| matches!(r.install_kind(&layout, repo), "managed" | "legacy"))
            .collect();
        let left: Vec<_> = registrations.iter().filter(|r| !matches!(r.install_kind(&layout,repo),"managed"|"legacy"))
            .map(|r|json!({"client":r.client,"state":r.install_kind(&layout,repo),"detail":"independent, editable, spawning or unverified installation preserved; no Python probe or package modification was attempted"})).collect();
        if managed.is_empty() {
            let state = left
                .iter()
                .map(|r| r["state"].as_str().unwrap_or("unmanaged"))
                .min_by_key(|s| match *s {
                    "unknown" => 0,
                    "spawning" => 1,
                    "editable" => 2,
                    _ => 3,
                })
                .unwrap_or("unmanaged");
            return Ok(
                json!({"state":state, "detail":"registrations use independent, editable or unverified environments; left as they are; rerun native setup to opt into the stable launcher", "registrations":left}),
            );
        }
        let _lock = UpdateLock::acquire(&layout)?;
        let version = source
            .strip_prefix("pseudolife-mcp==")
            .or_else(|| valid_version(source).then_some(source));
        let current = current_runtime(&layout).filter(|r| {
            !reinstall
                && repo.is_none()
                && r.source_commit.is_none()
                && r.source == source
                && version == Some(r.version.as_str())
        });
        let installed = current.is_none();
        let runtime = if let Some(runtime) = current {
            let activation = install::activate_launcher(&layout, &runtime.executable())?;
            activation.commit();
            runtime
        } else {
            install_source(source, repo, &layout)?
        };
        let mut migrations = Vec::new();
        for registration in managed {
            migrations.push(registrations::migrate(registration, &layout));
        }
        let failed = migrations.iter().any(|r| r["state"] == "failed");
        let exposed = registrations::expose_launcher(&layout);
        let evidence = if failed {
            Err("client registration migration is incomplete; no runtime was removed".into())
        } else {
            collect_evidence(&layout)
        };
        let pruned = remove_unused(&layout, evidence);
        Ok(
            json!({"state": if failed {"failed".into()} else {format!("{}:{}", if installed {"installed"} else {"current"}, runtime.version)},
            "detail":format!("runtime {} ({}) {} behind {}; sessions already running keep their runtime; new sessions use the newest complete native runtime",runtime.name(),runtime.version,if installed {"installed"} else {"already installed"},layout.launcher.display()),
            "runtime":runtime.path,"launcher":layout.launcher,"registrations":migrations,"left":left,"exposure":exposed,"pruned":pruned}),
        )
    })();
    result.unwrap_or_else(|detail| json!({"state":"failed", "detail":format!("native shim update failed ({detail}); existing sessions and registrations are preserved; retry pseudolife-mcp update --clients-only")}))
}

pub(crate) fn io_error(error: io::Error) -> String {
    error.to_string()
}

#[cfg(test)]
mod tests;
