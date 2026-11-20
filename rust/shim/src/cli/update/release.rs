use super::*;
use sha2::{Digest, Sha256};
use std::{
    cell::{Cell, RefCell},
    collections::BTreeMap,
    io::Read,
};

pub(super) const PYTHON_IMAGE: &str = "ghcr.io/pseudogiant-xr/pseudolife-daemon";
pub(crate) const RUST_IMAGE: &str = "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust";
const IMPORT_VERSION: &str = "0.23.1";
const IMPORT_SCHEMA: i64 = 59;
const IMPORT_RECOVERY_LIMIT: usize = 256 * 1024;
// OCI index for the archived v0.23.1 Python daemon (Linux amd64).
const IMPORT_IMAGE: &str = concat!(
    "ghcr.io/pseudogiant-xr/pseudolife-daemon@sha256:",
    "1613e1bd0cb563e9119f0444ca97dea659fa143a16931f4c512f4959243fdcc8"
);
const COMPOSE_FILES: [&str; 6] = [
    "docker-compose.yml",
    "docker-compose.ghcr.yml",
    "docker-compose.rust-daemon.yml",
    "docker-compose.rust-shadow.yml",
    "docker-compose.rust-ghcr.yml",
    "Dockerfile.extractor",
];

pub(super) fn health_endpoint_hint(ports: &str) -> String {
    let mut urls = std::collections::BTreeSet::new();
    for line in ports.lines() {
        let Some((host, port)) = line.trim().rsplit_once(':') else {
            continue;
        };
        if port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let Ok(port) = port.parse::<u16>() else {
            continue;
        };
        if port == 0 {
            continue;
        }
        match host {
            "127.0.0.1" | "0.0.0.0" => {
                urls.insert(format!("http://127.0.0.1:{port}"));
            }
            "[::1]" | "[::]" => {
                urls.insert(format!("http://[::1]:{port}"));
            }
            _ => {}
        }
    }
    if let Some(url) = urls.first() {
        format!(
            "Docker reports a daemon publication at {url} on the Docker host. If Docker runs on this machine and this is the intended daemon, retry with --daemon-url {url}; otherwise check your proxy/custom endpoint."
        )
    } else {
        "Check --daemon-url http(s)://<host>:<port> for the intended daemon or proxy endpoint."
            .into()
    }
}

pub(super) fn verified_compose(
    version: &str,
    sums: &[u8],
    archive: &[u8],
) -> Result<BTreeMap<String, Vec<u8>>> {
    let name = format!("pseudolife-compose-{version}.tar.gz");
    let sums =
        std::str::from_utf8(sums).map_err(|_| UpdateError::new("invalid release checksums"))?;
    let matches: Vec<_> = sums
        .lines()
        .filter_map(|line| line.split_once("  "))
        .filter(|(_, file)| *file == name)
        .collect();
    if matches.is_empty() {
        return Err(UpdateError::usage(
            "this release does not publish checksums for its native Compose bundle; use the Python updater, or --tag a release that ships pseudolife-compose-<version>.tar.gz; nothing was deployed",
        ));
    }
    if matches.len() != 1
        || matches[0].0.len() != 64
        || !matches[0]
            .0
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        || matches[0].0 != format!("{:x}", Sha256::digest(archive))
    {
        return Err(UpdateError::new(
            "the Compose release archive checksum is missing or does not match; nothing was deployed",
        ));
    }
    let mut decoder = flate2::read::MultiGzDecoder::new(archive);
    let mut result = BTreeMap::new();
    {
        let mut tar = tar::Archive::new(&mut decoder);
        for entry in tar
            .entries()
            .map_err(|_| UpdateError::new("invalid Compose release archive"))?
        {
            let mut entry =
                entry.map_err(|_| UpdateError::new("invalid Compose release archive"))?;
            let path = entry
                .path()
                .map_err(|_| UpdateError::new("invalid Compose archive path"))?;
            let name = path
                .to_str()
                .ok_or_else(|| UpdateError::new("invalid Compose archive path"))?
                .to_owned();
            if !COMPOSE_FILES.contains(&name.as_str())
                || !entry.header().entry_type().is_file()
                || entry.size() > 1024 * 1024
                || result.contains_key(&name)
            {
                return Err(UpdateError::new(
                    "unexpected or duplicate file in Compose release archive; nothing was deployed",
                ));
            }
            let mut bytes = Vec::new();
            entry.read_to_end(&mut bytes)?;
            result.insert(name, bytes);
        }
    }
    std::io::copy(&mut decoder, &mut std::io::sink())?;
    if COMPOSE_FILES.iter().any(|n| !result.contains_key(*n)) {
        return Err(UpdateError::new(
            "the release Compose archive is incomplete; nothing was deployed",
        ));
    }
    Ok(result)
}
struct Context {
    args: Vec<String>,
    env_files: Vec<PathBuf>,
    checkout: Option<PathBuf>,
    running: Value,
    /// The Rust overlay the running daemon was created with, if still present.
    running_rust: Option<PathBuf>,
    /// Compose arguments the mount identity check resolves when a Rust
    /// daemon leaves Rust: the target project plus the Rust overlay, whose
    /// models volume the switch drops on purpose.
    identity_args: Option<Vec<String>>,
}

struct PreparedRelease {
    context: Context,
    env: Environment,
    config: Value,
    token: Option<String>,
}

impl PreparedRelease {
    fn daemon(&self) -> &str {
        self.context.running["Id"]
            .as_str()
            .unwrap_or(DAEMON_CONTAINER)
    }

    fn postgres(&self, update: &Update) -> Result<String> {
        engine::container(update, &self.context.args, Some(&self.env), "pseudolife-pg")
    }

    fn import_status(&self, update: &Update) -> Result<engine::ImportStatus> {
        let pg = self.postgres(update)?;
        let env = &self.config["services"]["pseudolife-pg"]["environment"];
        let user = env["POSTGRES_USER"]
            .as_str()
            .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL user"))?;
        let db = env["POSTGRES_DB"]
            .as_str()
            .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL database"))?;
        engine::import_status(update, &pg, user, db, self.daemon())
    }
}

#[derive(Clone, Copy)]
enum ReleaseStage<'a> {
    Ordinary,
    Import(&'a ImportRun),
    AfterImport(&'a ImportRun),
}

#[derive(Clone, Deserialize, Serialize)]
struct RecoveryPoint {
    rollback: Value,
    commands: Vec<String>,
}

#[derive(Clone, Deserialize, Serialize)]
pub(super) struct ImportRecovery {
    #[serde(skip)]
    path: PathBuf,
    #[serde(skip)]
    on_bridge: bool,
    #[serde(skip)]
    replacement_interrupted: bool,
    #[serde(skip)]
    original_running: bool,
    bank: (String, String, String),
    image_id: String,
    source_id: String,
    source_image_id: String,
    original_activation: Option<String>,
    source_activation: Option<String>,
    original: RecoveryPoint,
    bridge: Option<RecoveryPoint>,
}

impl ImportRecovery {
    fn path(activation: &str) -> PathBuf {
        data_dir()
            .join("update-engine")
            .join(format!("import-{activation}.json"))
    }

    fn pending_path() -> PathBuf {
        data_dir().join("update-engine/pending-import")
    }

    fn valid_activation(activation: &str) -> bool {
        activation.len() == 32 && activation.bytes().all(|b| b.is_ascii_hexdigit())
    }

    fn invalid(path: &Path) -> UpdateError {
        UpdateError::usage(format!(
            "cannot verify the legacy import recovery record at {} against the running daemon and its bank; restore the original pre-import recovery record or use the first run's rollback commands before retrying",
            path.display()
        ))
    }

    fn read(path: PathBuf) -> Result<Self> {
        let bytes = crate::board::state::read(&path, IMPORT_RECOVERY_LIMIT)
            .map_err(|_| Self::invalid(&path))?;
        let mut record: Self = serde_json::from_slice(&bytes).map_err(|_| Self::invalid(&path))?;
        if record.original.commands.is_empty() {
            return Err(Self::invalid(&path));
        }
        record.path = path;
        Ok(record)
    }

    fn pending() -> Result<Option<(String, Self)>> {
        let path = Self::pending_path();
        if let Err(error) = fs::symlink_metadata(&path)
            && error.kind() == std::io::ErrorKind::NotFound
        {
            return Ok(None);
        }
        let bytes = crate::board::state::read(&path, 32).map_err(|_| Self::invalid(&path))?;
        let activation = std::str::from_utf8(&bytes)
            .ok()
            .filter(|value| Self::valid_activation(value))
            .ok_or_else(|| Self::invalid(&path))?;
        Ok(Some((
            activation.to_owned(),
            Self::read(Self::path(activation))?,
        )))
    }

    fn bank(running: &Value) -> Option<(String, String, String)> {
        let project = running["Config"]["Labels"]["com.docker.compose.project"].as_str()?;
        let state = running["Mounts"]
            .as_array()?
            .iter()
            .find(|m| m["Destination"] == "/data")?["Name"]
            .as_str()?;
        let dsn = running["Config"]["Env"]
            .as_array()?
            .iter()
            .filter_map(Value::as_str)
            .find_map(|entry| entry.strip_prefix("PSEUDOLIFE_MCP_DATABASE_URL="))?;
        Some((
            project.to_owned(),
            state.to_owned(),
            connection_fingerprint(dsn)?,
        ))
    }

    fn save(&self) -> Result<()> {
        let bytes = serde_json::to_vec(self).expect("import recovery record serializes");
        if bytes.len() > IMPORT_RECOVERY_LIMIT {
            return Err(UpdateError::new(format!(
                "the import recovery record at {} is too large to save and read back; simplify the Compose file paths before retrying",
                self.path.display()
            )));
        }
        seams::write_atomic(&self.path, &bytes).map_err(|error| {
            UpdateError::new(format!(
                "{} (recovery record {})",
                error.message,
                self.path.display()
            ))
        })
    }

    fn error(&self, error: UpdateError, progress: ImportProgress) -> UpdateError {
        if self.original_running {
            let commands = self
                .original
                .commands
                .iter()
                .take_while(|line| {
                    !line
                        .trim_start()
                        .starts_with("After restoring the original image")
                })
                .cloned()
                .collect::<Vec<_>>()
                .join("\n");
            return UpdateError::new(format!(
                "The original daemon is still running. Its bank was not migrated. The pending import is recorded at {}. To roll back the daemon image, recreate it from the original tag with the command below, then rerun the update.\n{commands}",
                Self::pending_path().display()
            ));
        }
        let mut error = error;
        error.message = format!(
            "Saved recovery record: {}\n{}",
            self.path.display(),
            error.message
        );
        import_stage_error(
            error,
            progress,
            &self.original.commands,
            self.bridge.as_ref().map(|point| point.commands.as_slice()),
        )
    }

    fn unverified_progress(&self) -> ImportProgress {
        if self.replacement_interrupted {
            ImportProgress::ReplacementUnverified
        } else if !self.on_bridge && self.bridge.is_some() {
            ImportProgress::ApplyingRust
        } else {
            ImportProgress::ApplyingBridge
        }
    }

    fn pending_disposition(
        &self,
        activation: &str,
        running: &Value,
        accepted: bool,
    ) -> PendingDisposition {
        let Some(image) = running["Image"].as_str().filter(|image| !image.is_empty()) else {
            return PendingDisposition::Unverified;
        };
        let labels = &running["Config"]["Labels"];
        let same_bank = Self::bank(running).as_ref() == Some(&self.bank);
        let same_source = running["Id"] == self.source_id && image == self.source_image_id;
        if same_bank
            && accepted
            && labels[engine::ACTIVATION_LABEL] == activation
            && image == self.image_id
            && labels[engine::ENGINE_LABEL] == "rust"
        {
            return PendingDisposition::Finished;
        }
        if same_source {
            if !same_bank {
                return PendingDisposition::Unverified;
            }
            return if self.bridge.is_none() && running["State"]["Running"] == true {
                PendingDisposition::OriginalRunning
            } else {
                PendingDisposition::Interrupted
            };
        }
        if same_bank
            && self.original.rollback["image_id"] == image
            && running["Config"]["Image"] == self.original.rollback["tag"]
            && running["Id"] != self.source_id
        {
            return PendingDisposition::Finished;
        }
        let later_activation = labels[engine::ACTIVATION_LABEL]
            .as_str()
            .is_some_and(|current| {
                current != activation
                    && self.original_activation.as_deref() != Some(current)
                    && self.source_activation.as_deref() != Some(current)
            });
        // Recreating an already accepted source can preserve its activation
        // while changing its container ID. Only a later activation supersedes it.
        if (accepted && later_activation)
            || (image != self.image_id
                && image != self.source_image_id
                && self.original.rollback["image_id"] != image)
        {
            return PendingDisposition::Finished;
        }
        PendingDisposition::Unverified
    }
}

fn connection_fingerprint(dsn: &str) -> Option<String> {
    let fields = backup::connection_fields(dsn)?;
    let scheme = if dsn.starts_with("postgres://") {
        "postgres"
    } else {
        "postgresql"
    };
    let hostname = fields.get("host");
    let host = match fields.get("hostaddr").filter(|addr| !addr.is_empty()) {
        Some(address) => json!([hostname, address]),
        None => json!(hostname),
    };
    let identity = (
        scheme,
        host,
        fields.get("port"),
        fields.get("user"),
        fields.get("dbname"),
    );
    let encoded = serde_json::to_vec(&identity).expect("connection identity serializes");
    Some(format!("{:x}", Sha256::digest(encoded)))
}

#[derive(Debug, PartialEq, Eq)]
enum PendingDisposition {
    OriginalRunning,
    Interrupted,
    Finished,
    Unverified,
}

struct ImportRun {
    progress: Cell<ImportProgress>,
    recovery: RefCell<Option<ImportRecovery>>,
}

impl ImportRun {
    fn new(progress: ImportProgress, recovery: Option<ImportRecovery>) -> Self {
        Self {
            progress: Cell::new(progress),
            recovery: RefCell::new(recovery),
        }
    }

    fn finish(&self, update: &mut Update, result: Result<()>) -> Result<()> {
        let recovery = self.recovery.borrow();
        let Some(record) = recovery.as_ref() else {
            return result;
        };
        if let Some(bridge) = &record.bridge {
            if update.report.rollback["tag"] != bridge.rollback["tag"] {
                update.report.rollback = bridge.rollback.clone();
            }
            update.report.rollback["before_import"] = record.original.rollback.clone();
        }
        result.map_err(|error| record.error(error, self.progress.get()))
    }
}

#[derive(Clone, Copy)]
enum ImportProgress {
    BeforeBridge,
    ApplyingBridge,
    ReplacementUnverified,
    BridgeHealthy,
    Imported,
    ApplyingRust,
    RustHealthy,
}

fn import_stage_error(
    error: UpdateError,
    progress: ImportProgress,
    original_commands: &[String],
    second_commands: Option<&[String]>,
) -> UpdateError {
    let state = match progress {
        ImportProgress::BeforeBridge => return error,
        ImportProgress::ApplyingBridge => {
            format!(
                "The Python {IMPORT_VERSION} bridge replacement was attempted. Its health and bank import could not be verified."
            )
        }
        ImportProgress::ReplacementUnverified => {
            "The update saved its recovery point before replacing the daemon. The replacement has not been verified.".into()
        }
        ImportProgress::BridgeHealthy => {
            format!(
                "The Python {IMPORT_VERSION} bridge reached schema v{IMPORT_SCHEMA}. The bank import could not be verified."
            )
        }
        ImportProgress::Imported => {
            format!(
                "The last verified daemon is the Python {IMPORT_VERSION} bridge at schema v{IMPORT_SCHEMA}. The bank import completed."
            )
        }
        ImportProgress::ApplyingRust => {
            format!(
                "The bank import completed on the Python {IMPORT_VERSION} bridge at schema v{IMPORT_SCHEMA}. The Rust replacement was attempted and its health could not be verified."
            )
        }
        ImportProgress::RustHealthy => {
            format!(
                "The bank import completed on the Python {IMPORT_VERSION} bridge at schema v{IMPORT_SCHEMA}. The Rust daemon passed its health gate, but its activation record could not be saved."
            )
        }
    };
    // Inner errors describe a single deployment and may already carry rollback
    // commands. Render recovery once using the import transaction's full state.
    let reason = error
        .message
        .split("\nto roll back:\n")
        .next()
        .unwrap_or_default()
        .replace("; nothing was changed", "")
        .replace(". Nothing was changed", ".")
        .replace("; nothing was deployed", "")
        .replace(". Nothing was deployed", ".")
        .replace("; no rollback tag was moved", "");
    let recovery = if let Some(commands) = second_commands {
        format!(
            "to roll back to the imported bank:\n{}\nto restore the host and bank from before the import:\n{}",
            commands.join("\n"),
            original_commands.join("\n")
        )
    } else {
        format!("to roll back:\n{}", original_commands.join("\n"))
    };
    let next = if matches!(progress, ImportProgress::Imported) {
        "Fix the reported error and rerun the same update to finish the Rust step. To return to the old state, use the rollback commands below."
    } else {
        "Use the rollback commands below to restore the image and bank from before the import."
    };
    let mut result = UpdateError::new(format!(
        "{state} The pre-import backups and rollback point are available.\n{reason}\n{next}\n{recovery}"
    ));
    if error.exit_code == 4 {
        result.exit_code = 4;
    }
    result
}

fn confirm_import(bank: &engine::ImportStatus) -> Result<()> {
    if bank.schema != Some(IMPORT_SCHEMA) || bank.counters_pending {
        return Err(UpdateError::new(
            "the archived Python daemon did not finish importing the bank's schema and CMS counters into cms_counters; use the pre-import rollback and recover the original state before retrying",
        ));
    }
    if bank.chroma_pending {
        return Err(UpdateError::new(
            "the archived Python daemon did not finish importing the Chroma documents; check its logs and use the pre-import rollback before retrying",
        ));
    }
    Ok(())
}

fn model_provision_diagnostic(stderr_tail: &str) -> Option<&'static str> {
    // The helper and Docker can include URLs, proxy credentials and paths in
    // their diagnostics. Only retain fixed, non-sensitive excerpts.
    const EXCERPTS: &[&str] = &[
        "No space left on device",
        "Disk quota exceeded",
        "Read-only file system",
        "Permission denied",
        "DNS error",
        "Temporary failure in name resolution",
        "Network is unreachable",
        "Connection refused",
        "Connection reset",
        "certificate verify failed",
        "command timed out",
        "command interrupted",
        "model checksum mismatch",
        "does not match its pinned sha256",
        "model artifact is missing or unsafe",
        "does not match this build",
        "download exceeds the pinned artifact byte size",
        "download is shorter than the pinned",
        "could not download",
        "cannot start the requested command",
        "missing executable",
    ];
    let lower = stderr_tail.to_ascii_lowercase();
    EXCERPTS
        .iter()
        .copied()
        .find(|excerpt| lower.contains(&excerpt.to_ascii_lowercase()))
}

/// A copy of the shipped Compose files the release archive replaces: one
/// of this updater's release archives, the Python updater's bundled copies,
/// or a checkout's `ops/` (the installers' layout).
/// Recognised by shape as well as by today's roots, so an archive or copy
/// written under another home or data directory still counts.
fn packaged_compose(path: &Path) -> bool {
    let Some(parent) = path.parent() else {
        return false;
    };
    let name = |p: Option<&Path>| {
        p.and_then(Path::file_name)
            .and_then(|n| n.to_str())
            .map(str::to_owned)
    };
    let grandparent = parent.parent();
    // <...>/pseudolife-mcp/compose/<x.y.z>/: a release archive.
    let archive = grandparent == Some(compose_root().as_path())
        || (name(Some(parent)).is_some_and(|v| plain_release(&v))
            && name(grandparent).as_deref() == Some("compose")
            && name(grandparent.and_then(Path::parent)).as_deref() == Some("pseudolife-mcp"));
    // <data dir>/compose/: the Python updater's bundled copies.
    let bundled = parent == data_dir().join("compose")
        || (name(Some(parent)).as_deref() == Some("compose")
            && name(grandparent).as_deref() == Some(".pseudolife-mcp"));
    // <checkout>/ops/: the installers' layout.
    let checkout = name(Some(parent)).as_deref() == Some("ops")
        && grandparent.is_some_and(|c| c.join("pyproject.toml").is_file());
    archive || bundled || checkout
}

/// The updater's own overlays (by name, in their own `update-engine`
/// directory: a user's override elsewhere is kept whatever it is called)
/// and the release's engine overlays: never a project's settings, so never
/// kept from the labels.
fn engine_overlay(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
        return false;
    };
    let own_dir = path
        .parent()
        .and_then(Path::file_name)
        .is_some_and(|d| d == "update-engine");
    (own_dir
        && (name.starts_with("docker-compose.engine")
            || name.starts_with("docker-compose.rollback")))
        || matches!(
            name,
            "docker-compose.rust-ghcr.yml"
                | "docker-compose.rust-daemon.yml"
                | "docker-compose.rust-shadow.yml"
        )
}

pub(super) fn compose_root() -> PathBuf {
    if cfg!(windows) {
        std::env::var_os("LOCALAPPDATA")
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join("AppData/Local"))
            .join("pseudolife-mcp/compose")
    } else {
        home().join(".local/share/pseudolife-mcp/compose")
    }
}
pub(super) fn publish_compose(
    root: &Path,
    target: &str,
    files: &BTreeMap<String, Vec<u8>>,
) -> Result<PathBuf> {
    if !plain_release(target) {
        return Err(UpdateError::usage("invalid Compose release version"));
    }
    let directory = root.join(target);
    crate::credentials::reject_ancestor_redirects(&directory).map_err(|_| {
        UpdateError::usage(
            "the Compose bundle path contains a redirect; restore its original directory and retry",
        )
    })?;
    let agrees = || -> bool {
        files.iter().all(|(name, bytes)| {
            fs::symlink_metadata(directory.join(name)).is_ok_and(|m| m.is_file())
                && fs::read(directory.join(name)).ok().as_ref() == Some(bytes)
        }) && fs::read_dir(&directory).is_ok_and(|d| d.count() == files.len())
    };
    if directory.exists() {
        return if agrees() {
            Ok(directory)
        } else {
            Err(UpdateError::usage(
                "the immutable Compose bundle differs from its verified release; preserve it for rollback and restore the original bundle before retrying",
            ))
        };
    }
    fs::create_dir_all(root)?;
    let staging = root.join(format!(
        ".compose-{target}-{}",
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir(&staging)?;
    let result = (|| -> Result<()> {
        for (name, bytes) in files {
            seams::write_atomic(&staging.join(name), bytes)?;
        }
        fs::rename(&staging, &directory)?;
        Ok(())
    })();
    if staging.exists() {
        let _ = fs::remove_dir_all(&staging);
    }
    if result.is_err() && directory.exists() && agrees() {
        return Ok(directory);
    }
    result?;
    Ok(directory)
}

impl Update {
    fn missing_release_asset(&self, target: &str) -> UpdateError {
        // A missing asset can mean either an older release or a nonexistent
        // tag. Only a confirmed published release justifies the Python remedy.
        let verify = || -> Result<()> {
            let base = std::env::var("PSEUDOLIFE_RELEASE_API_BASE_URL").unwrap_or_else(|_| {
                "https://api.github.com/repos/Pseudogiant-xr/Pseudolife-MCP/releases/tags".into()
            });
            let parsed = reqwest::Url::parse(&base)
                .map_err(|_| UpdateError::usage("invalid release metadata URL"))?;
            if parsed.scheme() != "https"
                && !(parsed.scheme() == "http"
                    && matches!(parsed.host_str(), Some("127.0.0.1" | "localhost" | "::1")))
            {
                return Err(UpdateError::usage(
                    "release metadata requires HTTPS (or a loopback fixture endpoint)",
                ));
            }
            let tag = format!("v{target}");
            let Some(bytes) = seams::download(&format!("{}/{tag}", base.trim_end_matches('/')))?
            else {
                return Err(UpdateError::usage(format!(
                    "release {tag} is not published; choose a published version with --tag <version>; nothing was deployed"
                )));
            };
            let release: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
            if release["tag_name"] != tag || release["draft"] != false {
                return Err(UpdateError::usage(
                    "cannot verify the published release; check --tag and retry; nothing was deployed",
                ));
            }
            Ok(())
        };
        verify().err().unwrap_or_else(|| UpdateError::usage(
            "this release does not publish the native Compose bundle and checksums; use the Python updater, or --tag a release that ships pseudolife-compose-<version>.tar.gz; nothing was deployed",
        ))
    }
    fn release_compose(&self, target: &str) -> Result<PathBuf> {
        let base = std::env::var("PSEUDOLIFE_RELEASE_BASE_URL").unwrap_or_else(|_| {
            "https://github.com/Pseudogiant-xr/Pseudolife-MCP/releases/download".into()
        });
        let parsed = reqwest::Url::parse(&base)
            .map_err(|_| UpdateError::usage("invalid release asset URL"))?;
        if parsed.scheme() != "https"
            && !(parsed.scheme() == "http"
                && matches!(parsed.host_str(), Some("127.0.0.1" | "localhost" | "::1")))
        {
            return Err(UpdateError::usage(
                "release assets require HTTPS (or a loopback fixture endpoint)",
            ));
        }
        let base = format!("{}/v{target}", base.trim_end_matches('/'));
        let sums = seams::download(&format!("{base}/SHA256SUMS"))?
            .ok_or_else(|| self.missing_release_asset(target))?;
        let archive = seams::download(&format!("{base}/pseudolife-compose-{target}.tar.gz"))?
            .ok_or_else(|| self.missing_release_asset(target))?;
        let files = verified_compose(target, &sums, &archive)?;
        publish_compose(&compose_root(), target, &files)
    }
    fn release_context(&self, target: &str, engine: &str) -> Result<Context> {
        // The Python updater's first refusal, in its order: with no project
        // file on this host and no env file to recreate from, the release's
        // files would reset the Postgres password, the volume names and the
        // bearer. It reads only the labels, as Python does.
        {
            let labels = engine::host_labels(self, DAEMON_CONTAINER);
            let list = |key: &str| -> Vec<PathBuf> {
                labels[key]
                    .as_str()
                    .unwrap_or("")
                    .split(',')
                    .filter(|s| !s.is_empty())
                    .map(PathBuf::from)
                    .collect()
            };
            let no_files = !list("com.docker.compose.project.config_files")
                .iter()
                .any(|p| p.is_file());
            let labelled_env = list("com.docker.compose.project.environment_file");
            let working_env = labels["com.docker.compose.project.working_dir"]
                .as_str()
                .map(|d| PathBuf::from(d).join(".env"))
                .is_some_and(|p| p.is_file());
            if no_files && self.o.env_file.is_none() && labelled_env.is_empty() && !working_env {
                return Err(UpdateError::usage(
                    "the daemon's compose files are gone and no env file was found beside them; recreating the container from the bundled compose files without one would reset the Postgres password, the volume names and the bearer. Name the file with --env-file. Nothing was changed",
                ));
            }
        }
        let (code, out) = self.docker_call(&["inspect", DAEMON_CONTAINER]);
        let running=serde_json::from_str::<Value>(&out).ok().and_then(|v|v.as_array().and_then(|a|a.first()).cloned()).filter(|_|code==0).ok_or_else(||UpdateError::usage("cannot inspect the daemon's Compose project; start it and retry; nothing was changed"))?;
        let labels = &running["Config"]["Labels"];
        let list = |key: &str| -> Vec<PathBuf> {
            labels[key]
                .as_str()
                .unwrap_or("")
                .split(',')
                .filter(|s| !s.is_empty())
                .map(PathBuf::from)
                .collect()
        };
        let labelled = list("com.docker.compose.project.environment_file");
        let working = labels["com.docker.compose.project.working_dir"]
            .as_str()
            .map(PathBuf::from);
        let env_files = if let Some(file) = &self.o.env_file {
            vec![file.clone()]
        } else if !labelled.is_empty() {
            labelled.clone()
        } else {
            working
                .as_ref()
                .map(|d| d.join(".env"))
                .filter(|p| p.is_file())
                .into_iter()
                .collect()
        };
        if env_files.iter().any(|p| !p.is_file()) {
            return Err(UpdateError::usage(
                "the daemon's env file no longer exists; recreating without it would reset credentials and volume names. Name the original file with --env-file. Nothing was changed",
            ));
        }
        let existing = list("com.docker.compose.project.config_files");
        let running_rust = existing
            .iter()
            .find(|p| {
                p.file_name().and_then(|n| n.to_str()) == Some("docker-compose.rust-ghcr.yml")
                    && p.is_file()
            })
            .cloned();
        let project_dir = working.or_else(|| {
            existing.first().and_then(|p| p.parent()).map(Path::to_owned)
        }).ok_or_else(|| UpdateError::usage(
            "cannot resolve the original Compose project directory; restore its project labels before updating; nothing was changed"
        ))?;
        if existing.iter().any(|p| !p.is_file()) && env_files.is_empty() {
            return Err(UpdateError::usage(
                "the daemon's compose files are gone and no env file was found; name the original file with --env-file. Nothing was changed",
            ));
        }
        // A project file that is neither packaged nor one of the updater's
        // own overlays holds this stack's settings: if it is missing here (a
        // stack manager's path inside its own container), the release's files
        // would replace that configuration, so refuse, as the Python updater does.
        let unavailable: Vec<String> = existing
            .iter()
            .filter(|p| !p.is_file())
            // Only a packaged copy of a shipped file is the archive's to replace;
            // any other missing file, in a packaged directory or not, held settings.
            .filter(|p| {
                let shipped = p
                    .file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| COMPOSE_FILES.contains(&n));
                !(engine_overlay(p) || (shipped && packaged_compose(p)))
            })
            .map(|p| p.display().to_string())
            .collect();
        if !unavailable.is_empty() {
            let project = labels["com.docker.compose.project"]
                .as_str()
                .unwrap_or("pseudolife-mcp");
            return Err(UpdateError::usage(format!(
                "compose project {project:?} has files unavailable on this host: {}. Their settings cannot be recovered from the env file, and the release's Compose files would replace this stack's configuration. Restore those files at those paths (Portainer: Stacks > {project} > Editor), then retry. Nothing was changed",
                unavailable.join(", ")
            )));
        }
        // Release Compose is always taken from the checksum-verified archive.
        // Keep project-specific overrides, but never stale engine/candidate
        // overlays from a prior activation, which could override the new tag.
        let directory = self.release_compose(target)?;
        // Only a packaged copy of a shipped file is replaced by the archive's.
        // A stack manager's own file that shares a shipped name (a Portainer
        // stack's docker-compose.yml) holds that stack's settings and stays.
        let mut kept_base = false;
        let mut kept_ghcr = false;
        let mut kept = Vec::new();
        for path in existing.into_iter().filter(|p| p.is_file()) {
            let Some(name) = path.file_name().and_then(|n| n.to_str()).map(str::to_owned) else {
                continue;
            };
            if engine_overlay(&path) {
                continue;
            }
            if COMPOSE_FILES.contains(&name.as_str()) {
                if packaged_compose(&path) {
                    continue;
                }
                kept_base |= name == "docker-compose.yml";
                kept_ghcr |= name == "docker-compose.ghcr.yml";
            }
            kept.push(path);
        }
        // Kept files stay in label order; the archive supplies a base and
        // the ghcr overlay only where the project kept none of its own.
        let mut files = Vec::new();
        if !kept_base {
            files.push(directory.join("docker-compose.yml"));
        }
        files.extend(kept);
        if !kept_ghcr {
            files.push(directory.join("docker-compose.ghcr.yml"));
        }
        let leaving_rust = engine != "rust" && labels[engine::ENGINE_LABEL] == "rust";
        let identity_overlay = leaving_rust.then(|| {
            running_rust
                .clone()
                .unwrap_or_else(|| directory.join("docker-compose.rust-ghcr.yml"))
        });
        if engine == "rust" {
            files.push(directory.join("docker-compose.rust-ghcr.yml"));
        }
        let mut args = vec![
            "-p".into(),
            labels["com.docker.compose.project"]
                .as_str()
                .unwrap_or("pseudolife-mcp")
                .into(),
        ];
        args.extend([
            "--project-directory".into(),
            project_dir.to_string_lossy().into_owned(),
        ]);
        args.extend(compose_args(&files, &env_files));
        let identity_args =
            identity_overlay.map(|overlay| engine::with_rust_overlay(&args, &overlay));
        let checkout = [
            project_dir.clone(),
            project_dir.parent().unwrap_or(&project_dir).to_owned(),
        ]
        .into_iter()
        .find(|p| p.join("pyproject.toml").is_file() && p.join("ops").is_dir());
        Ok(Context {
            args,
            env_files,
            checkout,
            running,
            running_rust,
            identity_args,
        })
    }
    pub(super) fn active_import(&self) -> Result<Option<ImportRecovery>> {
        self.inspect_active_import(true)
    }

    fn inspect_active_import(&self, retire: bool) -> Result<Option<ImportRecovery>> {
        let labels = engine::host_labels(self, DAEMON_CONTAINER);
        let on_bridge = labels[engine::ENGINE_LABEL] == "python"
            && labels[engine::RELEASE_LABEL] == IMPORT_VERSION;
        let unfinished_rust = labels[engine::ENGINE_LABEL] == "rust"
            && labels[engine::RELEASE_LABEL].is_string()
            && engine::validated(&labels).is_err();
        if !on_bridge && !unfinished_rust {
            return self.unfinished_replacement(retire);
        }
        let Some(activation) = labels[engine::ACTIVATION_LABEL].as_str() else {
            return self.unfinished_replacement(retire);
        };
        let missing = || {
            let path = if ImportRecovery::valid_activation(activation) {
                ImportRecovery::path(activation)
            } else {
                ImportRecovery::pending_path()
            };
            UpdateError::usage(format!(
                "the archived Python bridge is running without a verified pre-import recovery record at {}; the cms_counters or Chroma import may still be pending. Use the original pre-import rollback and backups from the first run before retrying",
                path.display()
            ))
        };
        let legacy_bridge = || {
            on_bridge
                && (self.running_image().1.as_deref() == Some(IMPORT_IMAGE)
                    || engine::validated(&labels)
                        .is_ok_and(|service| service["image"] == IMPORT_IMAGE))
        };
        if !ImportRecovery::valid_activation(activation) {
            return if legacy_bridge() && !ImportRecovery::pending_path().exists() {
                Err(missing())
            } else {
                self.unfinished_replacement(retire)
            };
        }
        let path = ImportRecovery::path(activation);
        if let Err(error) = fs::symlink_metadata(&path)
            && error.kind() == std::io::ErrorKind::NotFound
        {
            return if legacy_bridge() && !ImportRecovery::pending_path().exists() {
                Err(missing())
            } else {
                self.unfinished_replacement(retire)
            };
        }
        let mut recovery = ImportRecovery::read(path)?;
        let running = self.inspect_import_daemon(&recovery.path)?;
        if running["Config"]["Labels"][engine::ACTIVATION_LABEL] != activation
            || running["Image"] != recovery.image_id
            || ImportRecovery::bank(&running).as_ref() != Some(&recovery.bank)
        {
            if running["Image"] != recovery.image_id && ImportRecovery::pending_path().exists() {
                return self.unfinished_replacement(retire);
            }
            return Err(ImportRecovery::invalid(&recovery.path));
        }
        recovery.on_bridge = on_bridge;
        Ok(Some(recovery))
    }

    fn inspect_import_daemon(&self, path: &Path) -> Result<Value> {
        let (code, out) = self.docker_call(&["inspect", DAEMON_CONTAINER]);
        let data: Vec<Value> =
            serde_json::from_str(&out).map_err(|_| ImportRecovery::invalid(path))?;
        data.into_iter()
            .next()
            .filter(|_| code == 0)
            .ok_or_else(|| ImportRecovery::invalid(path))
    }

    fn unfinished_replacement(&self, retire: bool) -> Result<Option<ImportRecovery>> {
        let Some((activation, mut recovery)) = ImportRecovery::pending()? else {
            return Ok(None);
        };
        let running = self.inspect_import_daemon(&ImportRecovery::pending_path())?;
        let labels = &running["Config"]["Labels"];
        let disposition =
            recovery.pending_disposition(&activation, &running, engine::validated(labels).is_ok());
        match disposition {
            PendingDisposition::Finished => {
                let path = ImportRecovery::pending_path();
                if retire
                    && let Err(error) = fs::remove_file(&path)
                    && error.kind() != std::io::ErrorKind::NotFound
                {
                    return Err(UpdateError::usage(format!(
                        "cannot remove the completed import pointer at {}; check its directory permissions and retry",
                        path.display()
                    )));
                }
                Ok(None)
            }
            PendingDisposition::OriginalRunning | PendingDisposition::Interrupted => {
                recovery.replacement_interrupted = true;
                recovery.original_running = disposition == PendingDisposition::OriginalRunning;
                Ok(Some(recovery))
            }
            PendingDisposition::Unverified => {
                Err(ImportRecovery::invalid(&ImportRecovery::pending_path()))
            }
        }
    }

    pub(super) fn refuse_missing_import_daemon(&self) -> Result<()> {
        let Some((_, mut recovery)) = ImportRecovery::pending()? else {
            return Ok(());
        };
        recovery.replacement_interrupted = true;
        Err(recovery.error(UpdateError::new(
                format!("Docker Compose no longer exposes the daemon under its normal name; restore a verified recovery point from {} before retrying", ImportRecovery::pending_path().display()),
        ), recovery.unverified_progress()))
    }

    pub(super) fn refuse_import_noop(&self, locked: bool) -> Result<()> {
        if let Err(error) = fs::symlink_metadata(ImportRecovery::pending_path())
            && error.kind() == std::io::ErrorKind::NotFound
        {
            return Ok(());
        }
        let _lock = if locked {
            None
        } else {
            Some(UpdateLock::acquire()?)
        };
        let Some(recovery) = self.active_import()? else {
            return Ok(());
        };
        Err(recovery.error(UpdateError::new(
            "the running version does not establish that the legacy import finished; check the daemon and use the saved recovery instructions before retrying",
        ), recovery.unverified_progress()))
    }

    pub(super) fn import_health_error(&self, error: UpdateError) -> UpdateError {
        match self.inspect_active_import(false) {
            Ok(Some(record)) => record.error(error, record.unverified_progress()),
            Ok(None) => error,
            Err(error) => error,
        }
    }

    pub fn deploy_release(
        &mut self,
        current: Option<String>,
        before_recreate: Option<&mut dyn FnMut() -> Result<()>>,
        previous_digest: Option<String>,
    ) -> Result<()> {
        if !self.o.clients_only
            && let Some(recovery) = self.active_import()?
        {
            return self.resume_import(recovery, previous_digest, before_recreate);
        }
        if !self.o.clients_only && engine::selected_checkout(self)?.is_some() {
            return Err(UpdateError::usage(
                "this host selected a checkout engine; run update --checkout <repo>; release updates leave it unchanged",
            ));
        }
        let Some(target) = self.target_version()? else {
            self.step(NO_PUBLISHED_RELEASE);
            return Ok(());
        };
        self.report.target = Some(target.clone());
        let before = self.daemon_health();
        let current = current
            .or_else(|| {
                before
                    .as_ref()
                    .and_then(|v| v["version"].as_str().map(str::to_owned))
            })
            .or_else(|| {
                self.running_image()
                    .1
                    .and_then(|v| v.rsplit_once(':').map(|(_, v)| v.to_owned()))
                    .filter(|v| plain_release(v))
            });
        let previous = previous_digest.or_else(|| {
            before
                .as_ref()
                .and_then(|v| v["hooks_digest"].as_str().map(str::to_owned))
        });
        self.schema_before = before.as_ref().and_then(|v| v["schema"].as_i64());
        self.report.current = current.clone();
        self.refuse_downgrade(current.as_deref(), &target)?;
        if current.as_deref().is_some_and(|v| !safe_tag(v)) {
            return Err(UpdateError::usage(
                "the running daemon reported an invalid version; restore its previous image before retrying; nothing was deployed",
            ));
        }
        if self.o.clients_only {
            self.refuse_checkout_shadow(&target)?;
            self.refuse_tunnel_move(None)?;
            self.clients(None, &format!("pseudolife-mcp=={target}"), before.as_ref());
            return Ok(());
        }
        let labels = engine::host_labels(self, DAEMON_CONTAINER);
        let selected = if labels[engine::ENGINE_LABEL].as_str() == Some("rust") {
            "rust"
        } else {
            "python"
        };
        let (choice, defaulted, probe_legacy) = match self.o.engine.clone() {
            Some(engine) => (engine, false, false),
            None => self.default_choice(selected, &labels, &target),
        };
        // Engine selection, and every update of a Rust host, records an
        // accepted activation so the next update can verify it.
        let activating = self.o.engine.is_some() || choice == "rust";
        if current.as_deref() == Some(&target) && !self.o.reinstall && choice == selected {
            if self.o.engine.as_deref() == Some("python")
                && let Err(error) = engine::record_choice("python")
            {
                // An explicit choice is kept even when there is nothing to deploy.
                self.warn(format!(
                    "the Python choice could not be recorded ({}); a later update may move this host to the default engine",
                    error.message
                ));
            }
            self.step(format!("the daemon already runs {target}; nothing to do (--reinstall to recreate it anyway, --clients-only for the shim and plugin)"));
            return Ok(());
        }
        if !self.o.daemon_only {
            // Before the backup and the daemon, since the client steps run
            // last; and outside the default-engine attempt, whose early
            // refusals fall back to the host's own engine. An unattended
            // run updates the daemon alone instead of refusing.
            self.refuse_tunnel_move_before_daemon()?;
        }
        if defaulted {
            // The default never blocks an ordinary update: a move that refuses
            // before the backup falls back to the host's own engine.
            self.defaulted_engine = true;
            self.release_started = false;
            let attempt = self.deploy_release_as(
                &choice,
                activating,
                &target,
                current.clone(),
                previous.clone(),
                before.clone(),
                None,
                probe_legacy,
            );
            match attempt {
                Err(error) if !self.release_started => {
                    self.defaulted_engine = false;
                    self.discard_default_attempt();
                    if self.legacy_attempt {
                        return Err(error);
                    }
                    // The refusal's whole first line, its fix included.
                    let reason = error.message.lines().next().unwrap_or("").trim();
                    self.warn(format!(
                        "not moving to the {choice} daemon this time: {reason}"
                    ));
                    self.step(format!("staying on the {selected} daemon for this update; nothing was recorded, so the next attended update tries again (--engine {selected} keeps it)"));
                    if current.as_deref() == Some(&target) && !self.o.reinstall {
                        self.step(format!("the daemon already runs {target}; nothing to do"));
                        return Ok(());
                    }
                    return self.deploy_release_as(
                        selected,
                        false,
                        &target,
                        current,
                        previous,
                        before,
                        before_recreate,
                        false,
                    );
                }
                other => return other,
            }
        }
        let result = self.deploy_release_as(
            &choice,
            activating,
            &target,
            current,
            previous,
            before,
            before_recreate,
            probe_legacy,
        );
        if result.is_ok()
            && self.o.engine.as_deref() == Some("python")
            && engine::record_choice("python").is_err()
        {
            self.warn("the switch succeeded, but the Python choice could not be recorded; a later update may move this host to the default engine");
        }
        result
    }
    fn prepare_release(
        &mut self,
        choice: &str,
        activating: bool,
        target: &str,
        before: Option<&Value>,
    ) -> Result<PreparedRelease> {
        let context = self.release_context(target, choice)?;
        let env = compose_environment(
            &context.env_files,
            &[("PSEUDOLIFE_IMAGE_TAG".into(), target.to_owned())]
                .into_iter()
                .collect(),
        );
        // Resolve before any mutation, retaining the running bank and state.
        let mut config_args = vec!["compose".into()];
        config_args.extend(context.args.clone());
        config_args.extend(["config".into(), "--format".into(), "json".into()]);
        let (code, out) = self.command(&config_args, Some(&env), 60, false);
        let config:Value=serde_json::from_str(&out).ok().filter(|_:&Value|code==0).ok_or_else(||UpdateError::usage("cannot resolve the Compose project; check its files and env file, then retry; nothing was changed"))?;
        // Leaving Rust drops the overlay's models volume on purpose: check the
        // running mounts against the project with that overlay, so every other
        // mount is still held to the running daemon's.
        let identity = match &context.identity_args {
            Some(args) => engine::compose_config(
                self,
                args,
                &env,
                "cannot resolve the Compose project with the running Rust overlay; check its files and env file, then retry; nothing was changed",
            )?,
            None => config.clone(),
        };
        engine::verify_bank_identity(self, &identity, &context.running)?;
        if self.o.allow_auth_change {
            self.step("using the explicitly requested daemon bearer settings from --env-file");
        }
        self.set_rollback_scope(&config, &context.running)?;
        self.maintainer_context = Some((context.args.clone(), env.clone()));
        let token = if activating {
            engine::verify_identity(self, &identity, &context.running, &self.o.daemon_url)?;
            let token = engine::daemon_token(&config)?;
            // A daemon running without a bearer (--allow-auth-change adding
            // one) answers any bearer: read it with the configured one.
            let running = engine::daemon_token(&running_config(&context.running))
                .unwrap_or_else(|_| token.clone());
            if before.is_none_or(|v| v["status"] != "ok")
                || !seams::authenticated_read(&self.o.daemon_url, &running)
            {
                return Err(UpdateError::usage(
                    "the running daemon did not pass an authenticated bank read; check the configured daemon endpoint and bearer before retrying",
                ));
            }
            Some(token)
        } else {
            None
        };
        Ok(PreparedRelease {
            context,
            env,
            config,
            token,
        })
    }
    #[allow(clippy::too_many_arguments)]
    fn deploy_release_as(
        &mut self,
        choice: &str,
        activating: bool,
        target: &str,
        current: Option<String>,
        previous: Option<String>,
        before: Option<Value>,
        before_recreate: Option<&mut dyn FnMut() -> Result<()>>,
        probe_legacy: bool,
    ) -> Result<()> {
        let prepared = self.prepare_release(choice, activating, target, before.as_ref())?;
        if choice == "rust" || probe_legacy {
            let bank = prepared
                .import_status(self)
                .inspect_err(|_| self.legacy_attempt = true)?;
            if bank.pending() {
                self.legacy_attempt = true;
                let schema = bank.schema.ok_or_else(|| UpdateError::usage(
                    "the bank's schema version is unreadable; restore a verified backup of this bank before retrying; nothing was deployed",
                ))?;
                if schema > IMPORT_SCHEMA {
                    return Err(UpdateError::usage(format!(
                        "the bank is at schema v{schema}, newer than the archived Python daemon's v{IMPORT_SCHEMA}; restore a verified pre-migration backup at v{IMPORT_SCHEMA} or earlier before retrying; nothing was deployed"
                    )));
                }
                if self.o.no_backup {
                    return Err(UpdateError::usage(
                        "legacy import requires a bank and state backup; retry without --no-backup; nothing was deployed",
                    ));
                }
                let rust = if choice == "rust" {
                    prepared
                } else {
                    self.prepare_release("rust", true, target, before.as_ref())?
                };
                return self.import_release(
                    rust,
                    target,
                    current.as_deref(),
                    previous.as_deref(),
                    before.as_ref(),
                    before_recreate,
                );
            }
        }
        if choice == "rust" {
            model_volumes(&prepared.config)?;
        }
        let repository = if choice == "rust" {
            RUST_IMAGE
        } else {
            PYTHON_IMAGE
        };
        let image = format!("{repository}:{target}");
        let pg = self.release_disk(&prepared)?;
        self.pull_release_image(&image, choice, target)?;
        let schema = activating
            .then(|| self.release_image_schema(&image, choice))
            .transpose()?;
        if let (Some(image_schema), Some(bank)) = (schema, self.schema_before)
            && bank > image_schema
        {
            return Err(UpdateError::usage(format!(
                "the bank is at schema v{bank}, newer than {image}'s v{image_schema}, which would refuse it; update to a release at schema v{bank} or later; nothing was changed"
            )));
        }
        self.apply_release(
            prepared,
            ReleaseStage::Ordinary,
            choice,
            target,
            target,
            current.as_deref(),
            previous.as_deref(),
            &image,
            schema,
            pg,
            before_recreate,
        )?;
        Ok(())
    }

    fn release_disk(&mut self, prepared: &PreparedRelease) -> Result<Option<String>> {
        disk::host(&data_dir())?;
        let pg = storage_pg(self.o.no_backup, || prepared.postgres(self))?;
        self.disk_storage(
            prepared.daemon(),
            pg.as_deref(),
            prepared.context.running["Image"].as_str(),
        )?;
        Ok(pg)
    }

    fn pull_release_image(&mut self, image: &str, choice: &str, target: &str) -> Result<()> {
        let fresh = self.defaulted_engine && image != IMPORT_IMAGE && {
            let (code, out) = self.docker_call(&["image", "inspect", image]);
            code != 0 && out.to_lowercase().contains("no such image")
        };
        self.step(format!("pulling {image}..."));
        if self
            .command(&["pull".into(), image.into()], None, 7200, true)
            .0
            != 0
        {
            let fix = if image == IMPORT_IMAGE {
                format!(
                    "the archived Python daemon {IMPORT_VERSION} is available only for Linux amd64. On that platform, restore registry access and retry. On another platform, restore a verified backup on a Linux amd64 Docker host and complete the update there before moving the migrated bank back"
                )
            } else if choice == "rust" && !self.legacy_attempt && self.defaulted_engine {
                format!(
                    "this host keeps its Python daemon. Retry when the Rust daemon image for {target} can be pulled"
                )
            } else if choice == "rust" && !self.legacy_attempt && self.o.engine.is_none() {
                format!(
                    "this host keeps its current Rust daemon. Restore registry access or select a published Rust daemon with --tag <version> (requested {target})"
                )
            } else if choice == "rust" {
                format!(
                    "restore registry access or select a published Rust daemon with --tag <version> (requested {target})"
                )
            } else {
                format!("verify that {target} publishes a Python daemon image and retry")
            };
            return Err(UpdateError::new(format!(
                "docker pull {image} failed; {fix}; nothing was changed"
            )));
        }
        if fresh {
            self.default_pulled = Some(image.into());
        }
        Ok(())
    }

    fn release_image_schema(&self, image: &str, choice: &str) -> Result<i64> {
        let mut probe = vec!["run", "--rm", "--network", "none", "--entrypoint"];
        if choice == "rust" {
            probe.extend([
                "/usr/local/bin/pseudolife-mcp",
                image,
                "update",
                "--schema-version",
            ]);
        } else {
            probe.extend([
                "sh",
                image,
                "-c",
                "cat /app/pseudolife_memory/storage/schema.py",
            ]);
        }
        let (code, metadata) = self.docker_call(&probe);
        let schema = if choice == "rust" {
            metadata.trim().parse::<i64>().ok()
        } else {
            regex::Regex::new(r"(?m)^SCHEMA_META_VERSION\s*=\s*([0-9]+)")
                .expect("schema")
                .captures(&metadata)
                .and_then(|c| c[1].parse::<i64>().ok())
        };
        schema.filter(|_| code == 0).ok_or_else(|| UpdateError::usage(
            "cannot read the pulled image's schema version; verify its published CLI/image contract and retry; nothing was deployed",
        ))
    }

    fn import_release(
        &mut self,
        rust: PreparedRelease,
        target: &str,
        current: Option<&str>,
        previous: Option<&str>,
        before: Option<&Value>,
        before_recreate: Option<&mut dyn FnMut() -> Result<()>>,
    ) -> Result<()> {
        let models = model_volumes(&rust.config)?;
        let python = self.prepare_release("python", true, target, before)?;
        let pg = self.release_disk(&rust)?;
        let rust_image = format!("{RUST_IMAGE}:{target}");
        self.pull_release_image(&rust_image, "rust", target)?;
        self.check_config(&rust_image, &rust.config, true)?;
        let rust_schema = self.release_image_schema(&rust_image, "rust")?;
        if rust_schema < IMPORT_SCHEMA {
            return Err(UpdateError::usage(format!(
                "Rust release {target} opens schema v{rust_schema}, older than the import result at v{IMPORT_SCHEMA}; select a newer release with --tag <version>; nothing was deployed"
            )));
        }
        self.pull_release_image(IMPORT_IMAGE, "python", IMPORT_VERSION)?;
        if self.release_image_schema(IMPORT_IMAGE, "python")? != IMPORT_SCHEMA {
            return Err(UpdateError::usage(format!(
                "the archived Python daemon did not report schema v{IMPORT_SCHEMA}; verify the pinned image and retry; nothing was deployed"
            )));
        }
        self.provision_models(&rust_image, &rust.config)?;
        self.step(format!(
            "importing the legacy bank with the archived Python daemon {IMPORT_VERSION} before deploying Rust {target}"
        ));

        let import = ImportRun::new(ImportProgress::BeforeBridge, None);
        let health = self
            .apply_release(
                python,
                ReleaseStage::Import(&import),
                "python",
                target,
                IMPORT_VERSION,
                current,
                None,
                IMPORT_IMAGE,
                Some(IMPORT_SCHEMA),
                pg,
                before_recreate,
            )
            .map_err(|error| {
                import_stage_error(error, import.progress.get(), &self.rollback_lines, None)
            })?;
        self.schema_before = health["schema"].as_i64();
        let result = (|| -> Result<()> {
            let rust = self.prepare_release("rust", true, target, Some(&health))?;
            confirm_import(&rust.import_status(self)?)?;
            import.progress.set(ImportProgress::Imported);
            if models != model_volumes(&rust.config)? {
                return Err(UpdateError::new(
                    "the Rust model volume or cache directory changed since preflight; restore the original Compose settings or retry to provision the new cache",
                ));
            }
            let pg = self.release_disk(&rust)?;
            self.apply_release(
                rust,
                ReleaseStage::AfterImport(&import),
                "rust",
                target,
                target,
                Some(IMPORT_VERSION),
                previous,
                &rust_image,
                Some(rust_schema),
                pg,
                None,
            )?;
            Ok(())
        })();
        import.finish(self, result)
    }

    pub(super) fn resume_import(
        &mut self,
        recovery: ImportRecovery,
        previous: Option<String>,
        before_recreate: Option<&mut dyn FnMut() -> Result<()>>,
    ) -> Result<()> {
        self.report.rollback = recovery.original.rollback.clone();
        if recovery.original_running {
            self.report.rollback["restore_required"] = json!(false);
        }
        self.rollback_lines = recovery.original.commands.clone();
        let original_commands = self.rollback_lines.join("\n");
        let on_bridge = recovery.on_bridge;
        let interrupted = recovery.replacement_interrupted;
        let import = ImportRun::new(recovery.unverified_progress(), Some(recovery));
        let result = (|| {
            if interrupted {
                return Err(UpdateError::new(
                    "Docker Compose did not finish replacing the daemon; restore a verified recovery point before retrying",
                ));
            }
            if !on_bridge {
                return Err(UpdateError::new(
                    "the Rust replacement has no accepted activation; restore a verified recovery point before retrying",
                ));
            }
            if self.o.checkout.is_some()
                || self.o.engine.as_deref() == Some("python")
                || self.o.no_backup
            {
                return Err(UpdateError::usage(
                    "finish the existing legacy import with a backed-up release update to Rust, or use the original pre-import rollback commands",
                ));
            }
            self.report.current = Some(IMPORT_VERSION.into());
            let target = self
                .target_version()?
                .ok_or_else(|| UpdateError::new(NO_PUBLISHED_RELEASE))?;
            self.report.target = Some(target.clone());
            self.refuse_downgrade(Some(IMPORT_VERSION), &target)?;
            let before = self.daemon_health();
            let previous = previous.or_else(|| {
                before
                    .as_ref()?
                    .get("hooks_digest")?
                    .as_str()
                    .map(str::to_owned)
            });
            let rust = self.prepare_release("rust", true, &target, before.as_ref())?;
            if let Some(reason) = gate_failure(
                before.as_ref(),
                Some(IMPORT_VERSION),
                Some(IMPORT_SCHEMA),
                true,
                false,
                || true,
            ) {
                return Err(UpdateError::new(format!(
                    "the archived bridge has not passed its health gate ({reason}); allow the import to finish, then rerun the same update"
                )));
            }
            import.progress.set(ImportProgress::BridgeHealthy);
            confirm_import(&rust.import_status(self)?)?;
            import.progress.set(ImportProgress::Imported);
            if !self.o.daemon_only {
                self.refuse_tunnel_move_before_daemon()?;
            }
            self.schema_before = Some(IMPORT_SCHEMA);
            let pg = self.release_disk(&rust)?;
            let image = format!("{RUST_IMAGE}:{target}");
            self.pull_release_image(&image, "rust", &target)?;
            let schema = self.release_image_schema(&image, "rust")?;
            if schema < IMPORT_SCHEMA {
                return Err(UpdateError::usage(format!(
                    "Rust release {target} opens schema v{schema}, older than the import result at v{IMPORT_SCHEMA}; select a newer release with --tag <version>"
                )));
            }
            self.check_config(&image, &rust.config, true)?;
            self.provision_models(&image, &rust.config)?;
            self.apply_release(
                rust,
                ReleaseStage::AfterImport(&import),
                "rust",
                &target,
                &target,
                Some(IMPORT_VERSION),
                previous.as_deref(),
                &image,
                Some(schema),
                pg,
                before_recreate,
            )?;
            Ok(())
        })();
        import.finish(self, result)?;
        self.step(format!(
            "Original pre-import rollback:\n{original_commands}"
        ));
        Ok(())
    }

    fn save_import_activation(
        &self,
        stage: ReleaseStage<'_>,
        running: &Value,
        activation: &str,
        image: &str,
    ) -> Result<()> {
        let (import, on_bridge) = match stage {
            ReleaseStage::Ordinary => return Ok(()),
            ReleaseStage::Import(import) => (import, true),
            ReleaseStage::AfterImport(import) => (import, false),
        };
        let unavailable = || {
            UpdateError::new(
                "cannot identify the image or bank for the legacy import recovery record; check the pulled image and Compose project before retrying",
            )
        };
        let (code, out) = self.docker_call(&["image", "inspect", "-f", "{{.Id}}", image]);
        let image_id = out.trim();
        if code != 0 || !image_id.starts_with("sha256:") {
            return Err(unavailable());
        }
        let bank = ImportRecovery::bank(running).ok_or_else(unavailable)?;
        let source_id = running["Id"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(unavailable)?;
        let source_image_id = running["Image"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(unavailable)?;
        let source_activation = running["Config"]["Labels"][engine::ACTIVATION_LABEL]
            .as_str()
            .map(str::to_owned);
        let point = RecoveryPoint {
            rollback: self.report.rollback.clone(),
            commands: self.rollback_lines.clone(),
        };
        let path = ImportRecovery::path(activation);
        let mut recovery = import.recovery.borrow_mut();
        if on_bridge {
            let record = ImportRecovery {
                path,
                on_bridge,
                replacement_interrupted: false,
                original_running: false,
                bank,
                image_id: image_id.into(),
                source_id: source_id.into(),
                source_image_id: source_image_id.into(),
                original_activation: source_activation.clone(),
                source_activation,
                original: point,
                bridge: None,
            };
            record.save()?;
            *recovery = Some(record);
        } else {
            let record = recovery
                .as_mut()
                .filter(|r| r.bank == bank)
                .ok_or_else(unavailable)?;
            record.bridge = Some(point);
            record.save()?;
            let mut replacement = record.clone();
            replacement.path = path;
            replacement.image_id = image_id.into();
            replacement.source_id = source_id.into();
            replacement.source_image_id = source_image_id.into();
            replacement.source_activation = source_activation;
            replacement.save()?;
        }
        // Publish only after the corresponding original/bridge recovery
        // data is durable, and before Compose can remove the original name.
        seams::write_atomic(&ImportRecovery::pending_path(), activation.as_bytes())
    }

    #[allow(clippy::too_many_arguments)]
    fn apply_release(
        &mut self,
        prepared: PreparedRelease,
        stage: ReleaseStage<'_>,
        choice: &str,
        target: &str,
        version: &str,
        current: Option<&str>,
        previous: Option<&str>,
        image: &str,
        schema: Option<i64>,
        pg: Option<String>,
        before_recreate: Option<&mut dyn FnMut() -> Result<()>>,
    ) -> Result<Value> {
        let PreparedRelease {
            context,
            env,
            config,
            token,
        } = prepared;
        self.maintainer_context = Some((context.args.clone(), env.clone()));
        // Once the pull and the schema check passed (they change nothing):
        // can the daemon account use its volumes? A model cache it cannot use
        // is repaired here; the state waits for its backup.
        let ownership = self.ownership_preflight(&context.running)?;
        let daemon = context.running["Id"].as_str().unwrap_or(DAEMON_CONTAINER);
        let checkout = context.checkout.clone();
        let release_backup = |u: &mut Self| -> Result<Value> {
            if u.o.no_backup {
                u.warn("skipping backup (--no-backup)");
                return Ok(json!({}));
            }
            let pg = pg.as_deref().expect("backup discovery returns PostgreSQL");
            if matches!(stage, ReleaseStage::Ordinary)
                && let Some(repo) = checkout
                    .as_deref()
                    .filter(|repo| u.checkout_script(repo, "backup").is_some())
            {
                let pg_env = &config["services"]["pseudolife-pg"]["environment"];
                let user = pg_env["POSTGRES_USER"]
                    .as_str()
                    .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL user"))?;
                let db = pg_env["POSTGRES_DB"]
                    .as_str()
                    .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL database"))?;
                u.project_backup(repo, pg, daemon, user, db)
            } else {
                let mut directory = data_dir().join("backups");
                if matches!(stage, ReleaseStage::Import(_)) {
                    // Reserve the original dump and state independently of the
                    // post-import backup and its ordinary rotation.
                    directory.push("pre-import");
                }
                let backup = u.builtin_backup(pg, daemon, &directory)?;
                if !matches!(stage, ReleaseStage::Ordinary) && backup["state_archive"].is_null() {
                    return Err(UpdateError::new(
                        "the legacy import needs a complete state archive; fix the state backup and retry; no daemon was replaced in this step",
                    ));
                }
                Ok(backup)
            }
        };
        // A state volume the daemon account cannot use is backed up and
        // repaired before anything reads it as that account.
        let early = match ownership.as_ref().filter(|c| !c.state_usable()) {
            Some(checked) => Some(self.backup_around_ownership(
                Some(checked),
                &context.running,
                release_backup,
            )?),
            None => None,
        };
        if choice == "rust" {
            // A first start would otherwise download the models inside the
            // health gate. The running daemon keeps serving meanwhile.
            // Before provisioning: a configuration Rust refuses downloads no models.
            self.check_config(
                image,
                &config,
                matches!(stage, ReleaseStage::AfterImport(_)),
            )?;
            if matches!(stage, ReleaseStage::Ordinary) {
                self.provision_models(image, &config)?;
            }
        }
        let backups = match early {
            Some(backups) => backups,
            None => release_backup(self)?,
        };
        // A backup changes nothing on the host: a refused one still lets a
        // defaulted move fall back to the ordinary update.
        self.release_started = true;
        if let Some(callback) = before_recreate {
            callback()?;
        }
        let (running_id, _) = self.running_image();
        let running_id = running_id.ok_or_else(|| {
            UpdateError::new(
                "the daemon container vanished during the backup; nothing was deployed",
            )
        })?;
        if context.running["Image"].as_str() != Some(&running_id) {
            return Err(UpdateError::new(
                "the running daemon changed during backup; retry; nothing was deployed",
            ));
        }
        let old_engine = context.running["Config"]["Labels"][engine::ENGINE_LABEL]
            .as_str()
            .unwrap_or("python");
        let mut suffix = self.rollback_suffix();
        if matches!(stage, ReleaseStage::Import(_)) {
            // Reserve the recovery image beside the separately retained
            // pre-import backup. The suffix also survives a new invocation
            // that resumes on Python after the first hop.
            suffix.push_str("-pre-import");
        }
        let rollback = format!(
            "{}:{}-{suffix}",
            if old_engine == "rust" {
                RUST_IMAGE
            } else {
                PYTHON_IMAGE
            },
            current.unwrap_or("previous")
        );
        if let ReleaseStage::AfterImport(import) = stage
            && import
                .recovery
                .borrow()
                .as_ref()
                .is_some_and(|record| record.original.rollback["tag"] == rollback)
        {
            return Err(UpdateError::usage(
                "that tag is reserved for the original pre-import rollback; choose another --rollback-tag suffix before retrying",
            ));
        }
        engine::tag_validated(self, &running_id, &rollback)?;
        let mut old_service = engine::rollback_service(&context.running, &rollback, None);
        if matches!(stage, ReleaseStage::AfterImport(_)) {
            old_service["user"] = json!("10001:10001");
        }
        let rollback_file = data_dir().join("update-engine").join(format!(
            "docker-compose.rollback-{}-{}.json",
            stamp(),
            uuid::Uuid::new_v4().simple()
        ));
        engine::write_overlay(&rollback_file, &old_service)?;
        let mut restore_args = context.args.clone();
        if old_engine == "rust" && choice != "rust" {
            // The restored Rust daemon needs its overlay's model volume again.
            // Prefer the overlay the running daemon was created with: an older
            // target release's overlay may lack the model volume.
            let overlay = context.running_rust.clone().unwrap_or_else(|| {
                compose_root()
                    .join(target)
                    .join("docker-compose.rust-ghcr.yml")
            });
            restore_args.extend(["-f".into(), overlay.to_string_lossy().into_owned()]);
        }
        self.rollback_lines = engine::rollback_commands(
            &restore_args,
            &context.env_files,
            &rollback_file,
            &engine::release_accepted(),
            None,
            Some(target),
        );
        if matches!(stage, ReleaseStage::Import(_)) {
            let dump = seams::quote(backups["dump"].as_str().expect("validated bank dump"));
            let state = seams::quote(
                backups["state_archive"]
                    .as_str()
                    .expect("validated state archive"),
            );
            let pg = seams::quote(
                pg.as_deref()
                    .expect("the import required a PostgreSQL backup"),
            );
            self.rollback_lines.push("      After restoring the original image, restore the bank and state from before the import:".into());
            self.rollback_lines.push(format!(
                "      pseudolife-mcp restore --container {pg} --backup-file {dump} --state-archive {state} --yes"
            ));
        } else {
            self.rollback_lines.extend(self.schema_rollback_note());
        }
        self.report.rollback = json!({"tag":rollback,"image_id":running_id,"state":"tagged","overlay":rollback_file,"backup":backups});
        self.prune_rollbacks(PYTHON_IMAGE);
        self.prune_rollbacks(RUST_IMAGE);
        let activation = uuid::Uuid::new_v4().simple().to_string();
        let mut service = json!({"image":image});
        if token.is_some() {
            service["labels"] = json!({(engine::ENGINE_LABEL):choice,(engine::ACTIVATION_LABEL):activation,(engine::RELEASE_LABEL):version});
        }
        if matches!(stage, ReleaseStage::Import(_)) {
            // The archived entrypoint can migrate ownership as root. The
            // updater already backed up and checked the volume above.
            service["user"] = json!("10001:10001");
            self.report.rollback["restore_required"] = json!(true);
        }
        let candidate = data_dir()
            .join("update-engine")
            .join(format!("docker-compose.engine-candidate-{activation}.json"));
        engine::write_overlay(&candidate, &service).map_err(|e| self.rollback_error(e.message))?;
        let result = (|| -> Result<Value> {
            self.save_import_activation(stage, &context.running, &activation, image)?;
            let mut args = vec!["compose".into()];
            args.extend(context.args.clone());
            args.extend([
                "-f".into(),
                candidate.to_string_lossy().into_owned(),
                "up".into(),
                "-d".into(),
                "--no-deps".into(),
                "--no-build".into(),
            ]);
            if !matches!(stage, ReleaseStage::Ordinary) {
                args.extend(["--pull".into(), "never".into()]);
            }
            args.push(DAEMON_SERVICE.into());
            self.step(format!(
                "recreating the daemon container on {image} (Postgres + extractor untouched)..."
            ));
            match stage {
                ReleaseStage::Import(import) => import.progress.set(ImportProgress::ApplyingBridge),
                ReleaseStage::AfterImport(import) => {
                    import.progress.set(ImportProgress::ApplyingRust)
                }
                ReleaseStage::Ordinary => {}
            }
            if self.command(&args, Some(&env), 3600, true).0 != 0 {
                return Err(UpdateError::new("daemon recreate failed"));
            }
            let health = if let Some(token) = token {
                self.wait_health(Some(version), schema, &token, false)?
            } else {
                self.wait_plain_health(Some(version))?
            };
            match stage {
                ReleaseStage::Import(import) => import.progress.set(ImportProgress::BridgeHealthy),
                ReleaseStage::AfterImport(import) => {
                    import.progress.set(ImportProgress::RustHealthy)
                }
                ReleaseStage::Ordinary => {}
            }
            if service.get("labels").is_some() {
                engine::write_overlay(&engine::release_accepted(), &service)?;
            }
            if matches!(stage, ReleaseStage::AfterImport(_))
                && let Err(error) = fs::remove_file(ImportRecovery::pending_path())
                && error.kind() != std::io::ErrorKind::NotFound
            {
                self.warn(format!("could not clear the completed import pointer at {}; the accepted Rust activation and recovery records remain valid", ImportRecovery::pending_path().display()));
            }
            if !matches!(stage, ReleaseStage::Import(_)) {
                if self.o.daemon_only {
                    self.hooks_changed_note(previous, Some(&health));
                } else {
                    self.clients(None, &format!("pseudolife-mcp=={target}"), Some(&health));
                }
            }
            Ok(health)
        })();
        if fs::remove_file(&candidate).is_err() {
            self.warn("could not remove the temporary engine overlay; the rollback instructions remain usable");
        }
        result.map_err(|e| self.rollback_error(e.message))
    }
    pub fn wait_health(
        &mut self,
        version: Option<&str>,
        schema: Option<i64>,
        token: &str,
        shadow: bool,
    ) -> Result<Value> {
        self.wait_health_with(version, schema, Some(token), shadow)
    }
    fn wait_plain_health(&mut self, version: Option<&str>) -> Result<Value> {
        self.wait_health_with(version, None, None, false)
    }
    fn wait_health_with(
        &mut self,
        version: Option<&str>,
        schema: Option<i64>,
        token: Option<&str>,
        shadow: bool,
    ) -> Result<Value> {
        self.step("waiting for the daemon to report healthy...");
        let mut reason = "/health did not answer".to_owned();
        for attempt in 0..self.o.health_retries {
            #[cfg(unix)]
            if super::signals::checkpoint() != 0 {
                return Err(UpdateError::new("health check interrupted"));
            }
            let health = seams::fetch_health_json(
                &format!("{}/health", self.o.daemon_url.trim_end_matches('/')),
                3,
            );
            let failed = gate_failure(
                health.as_ref(),
                version,
                schema,
                token.is_some(),
                shadow,
                || token.is_none_or(|token| seams::authenticated_read(&self.o.daemon_url, token)),
            );
            if let Some(failed) = failed {
                reason = failed;
            } else if let Some(health) = health {
                if self
                    .schema_before
                    .zip(health["schema"].as_i64())
                    .is_some_and(|(a, b)| b > a)
                {
                    self.report.rollback["restore_required"] = json!(true);
                }
                self.step(format!(
                    "healthy. version={} schema={} persist_errors={}",
                    health["version"].as_str().unwrap_or("unknown"),
                    health["schema"],
                    health["persist_errors"]
                ));
                if !self.o.as_json && !self.rollback_lines.is_empty() {
                    let _ = output("    Rolled-back deploy if ever needed:\n", false);
                    for line in &self.rollback_lines {
                        let _ = output(&format!("{line}\n"), false);
                    }
                    let _ = std::io::stdout().flush();
                }
                return Ok(health);
            }
            #[cfg(unix)]
            if super::signals::checkpoint() != 0 {
                return Err(UpdateError::new("health check interrupted"));
            }
            if attempt + 1 < self.o.health_retries {
                let delay = std::time::Duration::from_millis(self.o.health_delay_ms);
                #[cfg(unix)]
                super::signals::sleep(delay);
                #[cfg(not(unix))]
                std::thread::sleep(delay);
            }
        }
        let failure = if token.is_some() {
            format!(
                "the daemon failed its expected version, schema or authenticated-read health gate at the configured endpoint ({reason}, last of {} checks); the client side was not moved",
                self.o.health_retries
            )
        } else {
            "the daemon did not report healthy at the expected version at the configured endpoint; inspect docker logs and use the rollback commands below; the client side was not moved".to_owned()
        };
        // Shadow verification uses its temporary endpoint, not the main daemon's port.
        let ports = if !shadow {
            self.maintainer_context.as_ref().and_then(|(compose, env)| {
                let daemon = engine::container(self, compose, Some(env), DAEMON_SERVICE).ok()?;
                let (code, out) = self.docker_call(&["port", &daemon, "8765/tcp"]);
                (code == 0).then_some(out)
            })
        } else {
            None
        };
        Err(UpdateError::new(format!(
            "{failure}\n{}",
            health_endpoint_hint(ports.as_deref().unwrap_or(""))
        )))
    }
    pub fn deploy_checkout(&mut self, repo: &Path) -> Result<()> {
        let repo = fs::canonicalize(repo)
            .map(seams::command_path)
            .map_err(|_| UpdateError::usage("--checkout must name a Pseudolife-MCP checkout"))?;
        let file = repo.join("ops/docker-compose.yml");
        if !file.is_file() {
            return Err(UpdateError::usage(
                "docker-compose.yml is missing: --checkout must name a Pseudolife-MCP checkout",
            ));
        }
        let tree = tree_state(&repo);
        if tree.dirty != "false" && !self.o.allow_dirty {
            for line in tree.lines.iter().take(20) {
                self.warn(line);
            }
            return Err(UpdateError::new(
                "commit, stash or remove the listed paths, or re-run with --allow-dirty to deploy this tree as it is (stamped dirty, or unknown when git cannot describe it)",
            ));
        }
        disk::host(&repo)?;
        let env_file = repo.join("ops/.env");
        let compose = compose_args(
            &[file.clone(), repo.join("ops/docker-compose.override.yml")],
            std::slice::from_ref(&env_file),
        );
        let env = compose_environment(&[env_file], &engine::build_environment(&tree));
        let before = self.daemon_health();
        self.schema_before = before.as_ref().and_then(|v| v["schema"].as_i64());
        // Resolve this project's container and bank before backup. Plain
        // deployments may reach health through a proxy or a non-loopback bind.
        let mut discovery = vec!["compose".into()];
        discovery.extend(compose.clone());
        discovery.extend(["config".into(), "--format".into(), "json".into()]);
        let (code, out) = self.command(&discovery, Some(&env), 60, false);
        let config: Value = serde_json::from_str(&out)
            .ok()
            .filter(|_: &Value| code == 0)
            .ok_or_else(|| {
                UpdateError::usage(
                    "cannot resolve the project's Compose configuration; restore its files and retry; nothing was deployed",
                )
            })?;
        // A checkout can name a different Compose project. Never read a global
        // daemon or back up a different bank while recreating this project.
        let daemon = engine::container(self, &compose, Some(&env), DAEMON_SERVICE)?;
        let running = engine::inspect(self, &daemon)?;
        engine::verify_checkout_identity(self, &config, &running, &daemon)?;
        let pg = storage_pg(self.o.no_backup, || {
            engine::container(self, &compose, Some(&env), "pseudolife-pg")
        })?;
        self.disk_storage(&daemon, pg.as_deref(), running["Image"].as_str())?;
        self.set_rollback_scope(&config, &running)?;
        self.maintainer_context = Some((compose.clone(), env.clone()));
        let ownership = self.ownership_preflight(&running)?;
        self.backup_around_ownership(ownership.as_ref(), &running, |u| {
            if u.o.no_backup {
                return u.backup(None);
            }
            let pg = pg.as_deref().expect("backup discovery returns PostgreSQL");
            if u.checkout_script(&repo, "backup").is_some() {
                let pg_env = &config["services"]["pseudolife-pg"]["environment"];
                let user = pg_env["POSTGRES_USER"].as_str().ok_or_else(|| {
                    UpdateError::usage("cannot resolve the project's PostgreSQL user; set POSTGRES_USER in its Compose environment and retry; nothing was deployed")
                })?;
                let db = pg_env["POSTGRES_DB"].as_str().ok_or_else(|| {
                    UpdateError::usage("cannot resolve the project's PostgreSQL database; set POSTGRES_DB in its Compose environment and retry; nothing was deployed")
                })?;
                u.project_backup(&repo, pg, &daemon, user, db)
            } else {
                u.builtin_backup(pg, &daemon, &data_dir().join("backups"))
            }
        })?;
        let text = fs::read_to_string(&file)?;
        disk::host(&repo)?;
        self.disk_storage(&daemon, pg.as_deref(), running["Image"].as_str())?;
        let image = text
            .lines()
            .find_map(|l| {
                l.trim()
                    .strip_prefix("image:")
                    .map(str::trim)
                    .filter(|s| s.starts_with("pseudolife-daemon:"))
                    .map(str::to_owned)
            })
            .ok_or_else(|| {
                UpdateError::new("could not find the daemon image tag in docker-compose.yml")
            })?;
        let (code, out) = self.docker_call(&["image", "inspect", "-f", "{{.Id}}", &image]);
        let tag_id = (code == 0).then(|| out.trim().to_owned());
        let (running, reference) = self.running_image_for(&daemon);
        let foreign = reference
            .as_deref()
            .is_some_and(|r| r.split(':').next() != image.split(':').next());
        let suffix = self.rollback_suffix();
        let rollback = format!("{image}-{suffix}");
        if let Some(source) = if foreign {
            running.as_ref()
        } else {
            tag_id.as_ref()
        } {
            if !foreign
                && running.as_ref().is_some_and(|id| id != source)
                && !self.o.force_rollback_tag
            {
                self.warn("REFUSING to move the rollback tag: the version tag is NOT the image the running daemon deployed; existing rollback tags are untouched");
                self.report.rollback = json!({"tag":rollback,"state":"kept","image_tag":image});
                self.rollback_lines = vec![
                    "(the rollback tag was NOT moved this run - see the warning above)".into(),
                    "Pick the newest surviving rollback tag and redeploy it:".into(),
                    format!(
                        "docker image ls {}",
                        seams::quote(image.split(':').next().unwrap_or("pseudolife-daemon"))
                    ),
                    format!(
                        "docker tag <surviving-rollback-tag> {}",
                        seams::quote(&image)
                    ),
                    format!(
                        "docker compose {} up -d --no-deps --no-build {DAEMON_SERVICE}",
                        display(&compose)
                    ),
                ];
            } else {
                engine::tag_validated(self, source, &rollback)?;
                self.report.rollback = json!({"tag":rollback,"state":"tagged","image_tag":image});
                self.rollback_lines = vec![
                    format!(
                        "docker tag {} {}",
                        seams::quote(&rollback),
                        seams::quote(&image)
                    ),
                    format!(
                        "docker compose {} up -d --no-deps --no-build {DAEMON_SERVICE}",
                        display(&compose)
                    ),
                ];
            }
        } else {
            self.warn("this deploy has NO rollback image. Rolling back means rebuilding the previous code");
            self.report.rollback = json!({"tag":rollback,"state":"none","image_tag":image});
            self.rollback_lines = vec![
                "(no rollback image exists for this deploy - nothing was tagged)".into(),
                "Restore the previous checkout and rebuild the daemon to roll back.".into(),
                format!(
                    "docker compose {} up -d --no-deps --build {DAEMON_SERVICE}",
                    display(&compose)
                ),
            ];
        }
        if !self.rollback_lines.is_empty() {
            self.rollback_lines.extend(self.schema_rollback_note());
        }
        self.prune_rollbacks(image.split(':').next().unwrap_or("pseudolife-daemon"));
        if tree_state(&repo) != tree {
            return Err(UpdateError::new(
                "the checkout changed during this deploy; nothing was built. Re-run the deploy",
            ));
        }
        let mut args = vec!["compose".into()];
        args.extend(compose.clone());
        args.extend([
            "up".into(),
            "-d".into(),
            "--no-deps".into(),
            "--build".into(),
            DAEMON_SERVICE.into(),
        ]);
        self.step("rebuilding the daemon only (Postgres + extractor untouched)...");
        self.step(format!(
            "build stamp: commit {}, dirty={}",
            tree.sha, tree.dirty
        ));
        if self.command(&args, Some(&env), 7200, true).0 != 0 {
            return Err(self.rollback_error("daemon rebuild failed"));
        }
        let health = self
            .wait_plain_health(None)
            .map_err(|e| self.rollback_error(e.message))?;
        // A forwarded health URL may name another healthy daemon. Verify the
        // recreated service itself before reporting this project as healthy.
        let recreated = engine::container(self, &compose, Some(&env), DAEMON_SERVICE)
            .map_err(|_| self.rollback_error("cannot identify exactly one recreated checkout daemon; inspect this project's Compose services and use the rollback commands below before retrying"))?;
        let (code, state) = self.docker_call(&["inspect", "-f", "{{.State.Status}}", &recreated]);
        if code != 0 || state.trim() != "running" {
            return Err(self.rollback_error("the recreated checkout daemon is not running; inspect this project's container logs before retrying"));
        }
        self.checkout_post_deploy(
            &repo,
            before.as_ref().and_then(|v| v["hooks_digest"].as_str()),
            &health,
        )?;
        self.prune_cache(Some(&repo));
        Ok(())
    }
}

/// The first health-gate check this /health answer fails, or None. A None
/// `version` or `schema` skips that check; `counters` checks
/// counters_import_required; `read` (the authenticated bank read) runs only
/// once every other check passed. Values print as JSON, as Python prints them.
pub(super) fn gate_failure(
    health: Option<&Value>,
    version: Option<&str>,
    schema: Option<i64>,
    counters: bool,
    shadow: bool,
    read: impl FnOnce() -> bool,
) -> Option<String> {
    let Some(health) = health.filter(|h| h.is_object()) else {
        return Some("/health did not answer".into());
    };
    if health["status"] != "ok" {
        return Some(format!(
            "/health reported status {} instead of \"ok\"",
            health["status"]
        ));
    }
    if let Some(v) = version.filter(|v| health["version"] != *v) {
        return Some(format!(
            "version {} instead of {}",
            health["version"],
            json!(v)
        ));
    }
    if let Some(v) = schema.filter(|v| health["schema"] != *v) {
        return Some(format!("schema {} instead of {v}", health["schema"]));
    }
    if counters && truthy(&health["counters_import_required"]) {
        return Some("counters_import_required: the daemon has not imported its counters".into());
    }
    if shadow && !(health["read_only"] == true && health["ready"] == true && health["db"] == "ok") {
        return Some(format!(
            "the shadow is not ready read-only (read_only={}, ready={}, db={})",
            health["read_only"], health["ready"], health["db"]
        ));
    }
    if !read() {
        return Some("the authenticated bank read was refused".into());
    }
    None
}

/// The running daemon's environment shaped as a Compose config, so its own
/// bearer is read the same way as the configured one.
fn running_config(running: &Value) -> Value {
    let mut env = serde_json::Map::new();
    for item in running["Config"]["Env"].as_array().into_iter().flatten() {
        if let Some((key, value)) = item.as_str().and_then(|s| s.split_once('=')) {
            env.insert(key.to_owned(), json!(value));
        }
    }
    json!({"services": {DAEMON_SERVICE: {"environment": env}}})
}

/// `(docker run flag, value, Compose volume key)`: one mount holding the
/// models. A named volume uses `--mount` (it keeps a subpath); a bind uses
/// `-v`, which creates a missing host path as Compose does.
type ModelMount = (String, String, Option<String>);

/// A `docker run --mount` value. Docker parses it as CSV, so a field holding
/// a comma or quote is quoted, with its quotes doubled.
fn mount_spec(fields: &[(&str, &str)]) -> String {
    fields
        .iter()
        .map(|(key, value)| {
            let field = format!("{key}={value}");
            if field.contains([',', '"', '\n', '\r']) {
                format!("\"{}\"", field.replace('"', "\"\""))
            } else {
                field
            }
        })
        .collect::<Vec<_>>()
        .join(",")
}

/// The models directory and the mounts covering it, or None when the daemon
/// names its own ONNX model directory.
fn model_volumes(config: &Value) -> Result<Option<(String, Vec<ModelMount>)>> {
    let service = &config["services"][DAEMON_SERVICE];
    let env = &service["environment"];
    if env["PSEUDOLIFE_DAEMON_ONNX_DIR"]
        .as_str()
        .is_some_and(|s| !s.is_empty())
    {
        return Ok(None);
    }
    let models = env["PSEUDOLIFE_DAEMON_MODELS_DIR"]
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or("/data/models")
        .trim_end_matches('/')
        .to_owned();
    let mut mounts = Vec::new();
    for volume in service["volumes"].as_array().into_iter().flatten() {
        let target = volume["target"]
            .as_str()
            .unwrap_or("")
            .trim_end_matches('/');
        if target.is_empty() || !(models == target || models.starts_with(&format!("{target}/"))) {
            continue;
        }
        let Some(source) = volume["source"].as_str().filter(|s| !s.is_empty()) else {
            continue;
        };
        match volume["type"].as_str() {
            Some("volume") => {
                let name = config["volumes"][source]["name"].as_str().unwrap_or(source);
                let mut fields = vec![("type", "volume"), ("src", name), ("dst", target)];
                if let Some(subpath) = volume["volume"]["subpath"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                {
                    fields.push(("volume-subpath", subpath));
                }
                mounts.push((
                    "--mount".to_owned(),
                    mount_spec(&fields),
                    Some(source.to_owned()),
                ));
            }
            // `-v` creates a missing host path, as Compose does for a bind.
            Some("bind") => mounts.push(("-v".to_owned(), format!("{source}:{target}"), None)),
            _ => {}
        }
    }
    if mounts.is_empty() {
        return Err(UpdateError::usage(format!(
            "the Rust daemon's model directory {models} is on no persistent volume of this project; restore the release Compose files and retry; nothing was changed"
        )));
    }
    Ok(Some((models, mounts)))
}

/// `docker volume create` arguments matching the project's definition, or
/// an error for a missing external volume.
fn volume_create_args(config: &Value, key: &str) -> Result<Vec<String>> {
    let definition = &config["volumes"][key];
    let name = definition["name"].as_str().unwrap_or(key);
    if !definition["external"].is_null() && definition["external"] != false {
        return Err(UpdateError::usage(format!(
            "the external model volume {name} does not exist; create it as the project expects, then retry; nothing was deployed"
        )));
    }
    let project = config["name"].as_str().unwrap_or("pseudolife-mcp");
    let mut args: Vec<String> = vec![
        "volume".into(),
        "create".into(),
        "--label".into(),
        format!("com.docker.compose.project={project}"),
        "--label".into(),
        format!("com.docker.compose.volume={key}"),
    ];
    if let Some(driver) = definition["driver"].as_str() {
        args.extend(["--driver".into(), driver.to_owned()]);
    }
    for (flag, field) in [("--opt", "driver_opts"), ("--label", "labels")] {
        if let Some(map) = definition[field].as_object() {
            for (option, value) in map {
                let value = value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| value.to_string());
                args.extend([flag.into(), format!("{option}={value}")]);
            }
        }
    }
    args.push(name.to_owned());
    Ok(args)
}

/// `pseudolife-daemon check-config`: the image refuses this configuration. An
/// image without the command falls through to startup and exits 2 instead.
const CHECK_UNSUPPORTED: i32 = 3;

/// `docker run` arguments giving `check-config` the daemon's own view: its
/// working directory, configuration environment and every persistent mount,
/// read-only. Never a bearer, a credential or the database URL: the check
/// opens no bank. A named volume that does not exist yet holds nothing to
/// check, and mounting it would create it outside Compose.
fn config_check_args(config: &Value, volume_exists: impl Fn(&str) -> bool) -> Vec<String> {
    let service = &config["services"][DAEMON_SERVICE];
    let mut args: Vec<String> = ["run", "--rm", "--network", "none"]
        .into_iter()
        .map(str::to_owned)
        .collect();
    if let Some(dir) = service["working_dir"].as_str().filter(|s| !s.is_empty()) {
        args.extend(["-w".to_owned(), dir.to_owned()]);
    }
    if let Some(env) = service["environment"].as_object() {
        let mut keys: Vec<&String> = env.keys().collect();
        keys.sort();
        for key in keys {
            let wanted = [
                "PSEUDOLIFE_DAEMON_",
                "PSEUDOLIFE_EMBEDDING_",
                "HF_",
                "HUGGINGFACE_",
                "TRANSFORMERS_",
            ]
            .iter()
            .any(|prefix| key.starts_with(prefix))
                || [
                    "PSEUDOLIFE_MCP_CONFIG",
                    "PSEUDOLIFE_MCP_DATA_DIR",
                    "HOME",
                    "XDG_CACHE_HOME",
                ]
                .contains(&key.as_str());
            // A credential under an allowed prefix (HF_TOKEN) stays out of argv.
            let secret = ["TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL"]
                .iter()
                .any(|word| key.to_uppercase().contains(word));
            let Some(value) = env[key].as_str().filter(|_| wanted && !secret) else {
                continue;
            };
            args.extend(["-e".to_owned(), format!("{key}={value}")]);
        }
    }
    for volume in service["volumes"].as_array().into_iter().flatten() {
        let (Some(source), Some(target)) = (
            volume["source"].as_str().filter(|s| !s.is_empty()),
            volume["target"].as_str().filter(|s| !s.is_empty()),
        ) else {
            continue;
        };
        let mut fields = match volume["type"].as_str() {
            Some("volume") => {
                let name = config["volumes"][source]["name"].as_str().unwrap_or(source);
                if !volume_exists(name) {
                    continue;
                }
                let mut fields = vec![("type", "volume"), ("src", name), ("dst", target)];
                if let Some(subpath) = volume["volume"]["subpath"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                {
                    fields.push(("volume-subpath", subpath));
                }
                fields
            }
            Some("bind") => vec![("type", "bind"), ("src", source), ("dst", target)],
            _ => continue,
        };
        fields.push(("readonly", "true"));
        args.extend(["--mount".to_owned(), mount_spec(&fields)]);
    }
    args
}

/// The engine a Docker host that never chose one gets (G2). A host chose when
/// it runs Rust or recorded an accepted selection (`--engine python` leaves one).
pub(super) const DEFAULT_ENGINE: &str = "rust";

impl Update {
    /// An unchosen host moves to the default engine on an attended update when
    /// the target release publishes it; unattended runs never change engines.
    /// `(choice, defaulted)`. A host chose when it runs Rust, carries an
    /// activation label, or recorded an accepted selection or an explicit
    /// choice (an unreadable record counts as chosen). The default applies
    /// only on an attended update with a backup.
    // The final value permits checking a Python host for legacy import even
    // when the ordinary engine policy would keep it on Python.
    fn default_choice(
        &mut self,
        selected: &str,
        labels: &Value,
        target: &str,
    ) -> (String, bool, bool) {
        // This engine preference governs new work. Recovery of a previously
        // started import is handled before choosing the next engine.
        if self.o.unattended
            || self.o.no_backup
            || self.o.allow_auth_change
            || DEFAULT_ENGINE == selected
        {
            return (
                selected.to_owned(),
                false,
                self.o.unattended && selected == "python",
            );
        }
        let chosen = labels[engine::ACTIVATION_LABEL]
            .as_str()
            .is_some_and(|s| !s.is_empty())
            || !matches!(engine::selection(&engine::release_accepted()), Ok(None))
            || match engine::recorded_choice() {
                Ok(choice) => choice.is_some(),
                Err(reason) => {
                    self.warn(format!(
                        "cannot read the engine choice in {} ({reason}); this host keeps its engine. Fix or remove that file to let the default engine apply",
                        engine::choice_path().display()
                    ));
                    true
                }
            };
        if chosen {
            return (selected.to_owned(), false, false);
        }
        let image = format!("{RUST_IMAGE}:{target}");
        if self.docker_call(&["manifest", "inspect", &image]).0 != 0 {
            self.step(format!(
                "could not confirm a published {DEFAULT_ENGINE} daemon image for {target} (docker manifest inspect); checking this bank before choosing the update path"
            ));
            return (selected.to_owned(), false, true);
        }
        self.step(format!(
            "moving this host to the {DEFAULT_ENGINE} daemon, the default since G2 (keep {selected} with --engine {selected})"
        ));
        (DEFAULT_ENGINE.to_owned(), true, false)
    }
    /// Remove what a defaulted attempt pulled or created, so falling back
    /// leaves the host as it found it.
    fn discard_default_attempt(&mut self) {
        let mut removals: Vec<(&str, String)> = Vec::new();
        if let Some(image) = self.default_pulled.take() {
            removals.push(("image", image));
        }
        for volume in std::mem::take(&mut self.default_volumes) {
            removals.push(("volume", volume));
        }
        for (kind, name) in removals {
            if self.docker_call(&[kind, "rm", &name]).0 != 0 {
                self.warn(format!(
                    "could not remove {name} after falling back (docker {kind} rm); remove it by hand if the disk is short"
                ));
            }
        }
    }
    /// Refuse before the backup a configuration the Rust daemon would refuse
    /// at startup (a non-default reranker, an embedding model it cannot
    /// load): the Python daemon keeps serving.
    fn check_config(&mut self, image: &str, config: &Value, strict: bool) -> Result<()> {
        self.step(format!(
            "checking this bank's configuration against {image}..."
        ));
        let mut args = config_check_args(config, |name| {
            let (code, out) = self.docker_call(&["volume", "inspect", name]);
            code == 0
                && serde_json::from_str::<Value>(&out)
                    .ok()
                    .and_then(|v| v.as_array().map(|a| !a.is_empty()))
                    .unwrap_or(false)
        });
        args.extend(
            ["--entrypoint", "pseudolife-daemon", image, "check-config"]
                .into_iter()
                .map(str::to_owned),
        );
        let (code, out) = self.command(&args, None, 600, false);
        if code == 0 {
            return Ok(());
        }
        let detail = out.trim().lines().last().unwrap_or("").trim().to_owned();
        if code == CHECK_UNSUPPORTED {
            return Err(UpdateError::usage(format!(
                "the Rust daemon does not support this bank's configuration: {detail}; nothing was deployed"
            )));
        }
        if strict || self.defaulted_engine {
            return Err(UpdateError::new(format!(
                "could not check this bank's configuration against {image} (exit {code}{}); nothing was deployed",
                if detail.is_empty() {
                    String::new()
                } else {
                    format!(": {detail}")
                }
            )));
        }
        self.warn(format!(
            "could not check this bank's configuration against {image} (exit {code}); the health gate checks the new daemon instead"
        ));
        Ok(())
    }
    /// Install the pinned models into the daemon's model volume before backup.
    pub(super) fn provision_models(&mut self, image: &str, config: &Value) -> Result<()> {
        let Some((models, mounts)) = model_volumes(config)? else {
            self.step(
                "the daemon names its own ONNX model directory; leaving model provisioning to it",
            );
            return Ok(());
        };
        for (_, _, key) in &mounts {
            let Some(key) = key else { continue };
            let name = config["volumes"][key.as_str()]["name"]
                .as_str()
                .unwrap_or(key)
                .to_owned();
            let (code, out) = self.docker_call(&["volume", "inspect", &name]);
            let exists = code == 0
                && serde_json::from_str::<Value>(&out)
                    .ok()
                    .and_then(|v| v.as_array().map(|a| !a.is_empty()))
                    .unwrap_or(false);
            if !exists {
                // Created the way Compose would, so `up` adopts it quietly.
                let args = volume_create_args(config, key)?;
                let borrowed: Vec<&str> = args.iter().map(String::as_str).collect();
                if self.docker_call(&borrowed).0 != 0 {
                    return Err(UpdateError::new(format!(
                        "could not create the model volume {name}; check Docker and retry; nothing was deployed"
                    )));
                }
                if self.defaulted_engine {
                    self.default_volumes.push(name.clone());
                }
            }
        }
        self.step(format!(
            "provisioning the Rust daemon's pinned models into {models} (a first activation downloads them)..."
        ));
        let mut args: Vec<String> = vec![
            "run".into(),
            "--rm".into(),
            "-e".into(),
            format!("PSEUDOLIFE_DAEMON_MODELS_DIR={models}"),
        ];
        for (flag, value, _) in &mounts {
            args.extend([flag.clone(), value.clone()]);
        }
        args.extend(
            [
                "--entrypoint",
                "pseudolife-daemon",
                image,
                "provision",
                "models",
                "--if-missing",
            ]
            .into_iter()
            .map(str::to_owned),
        );
        args.insert(0, self.docker.clone());
        let (code, stderr_tail) = seams::run_cli_stderr(&args, 3600, !self.o.as_json);
        if code != 0 {
            let detail = model_provision_diagnostic(&stderr_tail)
                .map_or_else(String::new, |excerpt| format!(": {excerpt}"));
            return Err(UpdateError::new(format!(
                "could not provision the Rust daemon's models (exit {code}){detail}; check the model cache, network access and disk space, then retry; nothing was deployed"
            )));
        }
        Ok(())
    }
}

fn storage_pg(no_backup: bool, resolve: impl FnOnce() -> Result<String>) -> Result<Option<String>> {
    if no_backup {
        Ok(None)
    } else {
        resolve().map(Some)
    }
}

#[cfg(test)]
mod disk_review_tests {
    use super::*;

    fn pending_fixture() -> (ImportRecovery, Value) {
        let dsn = "postgresql://fixture:first-fixture@postgres:5432/bank";
        let running = json!({
            "Id": "old-container", "Image": "sha256:old", "State": {"Running": true},
            "Mounts": [{"Destination": "/data", "Name": "state"}],
            "Config": {
                "Labels": {"com.docker.compose.project": "fixture"},
                "Env": [format!("PSEUDOLIFE_MCP_DATABASE_URL={dsn}")],
                "Image": "daemon:old",
            },
        });
        let record = ImportRecovery {
            path: PathBuf::new(),
            on_bridge: false,
            replacement_interrupted: false,
            original_running: false,
            bank: ImportRecovery::bank(&running).unwrap(),
            image_id: "sha256:bridge".into(),
            source_id: "old-container".into(),
            source_image_id: "sha256:old".into(),
            original_activation: Some("original-activation".into()),
            source_activation: Some("original-activation".into()),
            original: RecoveryPoint {
                rollback: json!({"image_id": "sha256:old", "tag": "daemon:original"}),
                commands: vec!["recreate original".into()],
            },
            bridge: None,
        };
        (record, running)
    }

    #[test]
    fn recovery_record_connection_fingerprint_excludes_the_password() {
        let (original, mut running) = pending_fixture();
        let mut rotated = original.clone();
        running["Config"]["Env"] = json!([
            "PSEUDOLIFE_MCP_DATABASE_URL=postgresql://fixture:second-fixture@postgres:5432/bank?password=override-fixture"
        ]);
        rotated.bank = ImportRecovery::bank(&running).unwrap();
        let allowed_parts = serde_json::to_vec(&json!([
            "postgresql",
            "postgres",
            "5432",
            "fixture",
            "bank"
        ]))
        .unwrap();
        assert_eq!(
            rotated.bank.2,
            format!("{:x}", Sha256::digest(allowed_parts))
        );
        assert_eq!(
            serde_json::to_vec(&rotated).unwrap(),
            serde_json::to_vec(&original).unwrap()
        );
        for (one, two) in [
            (
                "postgresql://fixture@postgres/bank?hostaddr=192.0.2.10",
                "postgresql://fixture@postgres/bank?hostaddr=192.0.2.11",
            ),
            (
                "host=postgres hostaddr=192.0.2.10 user=fixture dbname=bank",
                "host=postgres hostaddr=192.0.2.11 user=fixture dbname=bank",
            ),
        ] {
            assert_ne!(
                connection_fingerprint(one).unwrap(),
                connection_fingerprint(two).unwrap()
            );
        }
    }

    #[test]
    fn pending_import_decision_distinguishes_finished_unverified_and_live_source() {
        use PendingDisposition::{Finished, Interrupted, OriginalRunning, Unverified};
        let (mut record, original) = pending_fixture();
        let check = |record: &ImportRecovery, running: &Value, accepted| {
            record.pending_disposition("pending-activation", running, accepted)
        };
        assert_eq!(check(&record, &original, true), OriginalRunning);
        let mut running = original.clone();
        running["State"]["Running"] = json!(false);
        assert_eq!(check(&record, &running, false), Interrupted);
        running["Mounts"][0]["Name"] = json!("another-state");
        assert_eq!(check(&record, &running, true), Unverified);

        running = original.clone();
        running["Id"] = json!("restored-container");
        running["Config"]["Image"] = json!("daemon:original");
        assert_eq!(check(&record, &running, false), Finished);
        running["Config"]["Image"] = json!("daemon:old");
        assert_eq!(check(&record, &running, false), Unverified);
        running["Config"]["Labels"][engine::ACTIVATION_LABEL] = json!("original-activation");
        assert_eq!(check(&record, &running, true), Unverified);
        running["Mounts"][0]["Name"] = json!("another-state");
        assert_eq!(check(&record, &running, true), Unverified);
        running["Mounts"][0]["Name"] = json!("state");
        running["Config"]["Labels"][engine::ACTIVATION_LABEL] = json!("later-activation");
        assert_eq!(check(&record, &running, true), Finished);
        running["Image"] = json!("sha256:unrelated");
        assert_eq!(check(&record, &running, false), Finished);
        running["Image"] = Value::Null;
        assert_eq!(check(&record, &running, false), Unverified);

        record.source_image_id = "sha256:bridge".into();
        record.image_id = "sha256:rust".into();
        record.bridge = Some(record.original.clone());
        running["Image"] = json!("sha256:bridge");
        running["Id"] = json!("old-container");
        assert_eq!(check(&record, &running, false), Interrupted);
        running["Id"] = json!("rust-container");
        running["Image"] = json!("sha256:rust");
        running["Config"]["Labels"][engine::ACTIVATION_LABEL] = json!("pending-activation");
        running["Config"]["Labels"][engine::ENGINE_LABEL] = json!("rust");
        assert_eq!(check(&record, &running, true), Finished);
        assert_eq!(check(&record, &running, false), Unverified);
    }

    #[test]
    fn confirm_import_matches_the_archived_schema_and_pending_files() {
        let bank = engine::ImportStatus {
            schema: Some(IMPORT_SCHEMA),
            counters_pending: false,
            chroma_pending: false,
        };
        assert!(confirm_import(&bank).is_ok());
        for schema in [None, Some(56), Some(57), Some(58), Some(60)] {
            assert!(confirm_import(&engine::ImportStatus { schema, ..bank }).is_err());
        }
        assert!(
            confirm_import(&engine::ImportStatus {
                counters_pending: true,
                ..bank
            })
            .unwrap_err()
            .message
            .contains("cms_counters")
        );
        assert!(
            confirm_import(&engine::ImportStatus {
                chroma_pending: true,
                ..bank
            })
            .unwrap_err()
            .message
            .contains("Chroma")
        );
    }

    #[test]
    fn post_import_errors_keep_both_recovery_points_without_false_noop_claims() {
        let original = ["original restore".to_owned()];
        let bridge = ["bridge restore".to_owned()];
        let error = import_stage_error(
            UpdateError::usage(
                "the running daemon did not pass an authenticated bank read; no rollback tag was moved",
            ),
            ImportProgress::Imported,
            &original,
            Some(&bridge),
        );
        assert!(error.message.contains("authenticated bank read"));
        assert!(!error.message.contains("no rollback tag was moved"));
        assert!(
            error
                .message
                .contains("to roll back to the imported bank:\nbridge restore")
        );
        assert!(
            error
                .message
                .contains("to restore the host and bank from before the import:\noriginal restore")
        );
    }
    fn daemon(env: Value, volumes: Value) -> Value {
        json!({"name": "fixture", "services": {DAEMON_SERVICE: {"environment": env, "volumes": volumes}},
               "volumes": {"pseudolife_data": {"name": "fixture-state"},
                           "pseudolife_rust_models": {"name": "fixture_pseudolife_rust_models"}}})
    }
    #[test]
    fn models_are_provisioned_into_the_volume_the_service_maps_there() {
        let data = json!({"type": "volume", "source": "pseudolife_data", "target": "/data"});
        let models =
            json!({"type": "volume", "source": "pseudolife_rust_models", "target": "/models"});
        let (dir, mounts) = model_volumes(&daemon(
            json!({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}),
            json!([data.clone(), models]),
        ))
        .unwrap()
        .unwrap();
        assert_eq!(dir, "/models");
        assert_eq!(
            mounts,
            vec![(
                "--mount".to_owned(),
                "type=volume,src=fixture_pseudolife_rust_models,dst=/models".to_owned(),
                Some("pseudolife_rust_models".to_owned())
            )]
        );
        let (dir, mounts) = model_volumes(&daemon(json!({}), json!([data.clone()])))
            .unwrap()
            .unwrap();
        assert_eq!(
            (dir.as_str(), mounts[0].1.as_str()),
            ("/data/models", "type=volume,src=fixture-state,dst=/data")
        );
        let nested = json!({"type": "volume", "source": "pseudolife_rust_models", "target": "/models",
                            "volume": {"subpath": "cache"}});
        let (_, mounts) = model_volumes(&daemon(
            json!({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}),
            json!([nested]),
        ))
        .unwrap()
        .unwrap();
        assert!(mounts[0].1.ends_with(",volume-subpath=cache"));
        assert!(
            model_volumes(&daemon(
                json!({"PSEUDOLIFE_DAEMON_ONNX_DIR": "/opt/models"}),
                json!([data])
            ))
            .unwrap()
            .is_none()
        );
        assert!(
            model_volumes(&daemon(
                json!({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}),
                json!([])
            ))
            .is_err()
        );
    }
    #[test]
    fn a_comma_in_a_model_subpath_is_quoted_and_a_bind_keeps_v() {
        let nested = json!({"type": "volume", "source": "pseudolife_rust_models", "target": "/models",
                            "volume": {"subpath": "a,\"b\""}});
        let (_, mounts) = model_volumes(&daemon(
            json!({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}),
            json!([nested]),
        ))
        .unwrap()
        .unwrap();
        assert_eq!(
            mounts[0].1,
            "type=volume,src=fixture_pseudolife_rust_models,dst=/models,\"volume-subpath=a,\"\"b\"\"\""
        );
        let bind = json!({"type": "bind", "source": "/srv/models,cache", "target": "/models"});
        let (_, mounts) = model_volumes(&daemon(
            json!({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}),
            json!([bind]),
        ))
        .unwrap()
        .unwrap();
        assert_eq!(
            mounts,
            vec![(
                "-v".to_owned(),
                "/srv/models,cache:/models".to_owned(),
                None
            )]
        );
    }
    #[test]
    fn a_model_volume_is_created_with_its_project_driver_and_options() {
        let mut config = daemon(json!({}), json!([]));
        config["volumes"]["pseudolife_rust_models"] = json!({"name": "fixture_models", "driver": "local",
            "driver_opts": {"type": "none", "o": "bind", "device": "/srv/models"}});
        let args = volume_create_args(&config, "pseudolife_rust_models").unwrap();
        assert_eq!(args.last().map(String::as_str), Some("fixture_models"));
        for expected in [
            "com.docker.compose.project=fixture",
            "com.docker.compose.volume=pseudolife_rust_models",
            "local",
            "device=/srv/models",
            "o=bind",
            "type=none",
        ] {
            assert!(
                args.iter().any(|a| a == expected),
                "{expected} missing from {args:?}"
            );
        }
        config["volumes"]["pseudolife_rust_models"] =
            json!({"name": "fixture_models", "external": true});
        assert!(volume_create_args(&config, "pseudolife_rust_models").is_err());
    }
    #[test]
    fn the_running_bearer_is_read_from_the_container_environment() {
        let running = json!({"Config": {"Env": ["PATH=/bin", "PSEUDOLIFE_MCP_TOKEN=old=bearer"]}});
        assert_eq!(
            engine::daemon_token(&running_config(&running)).unwrap(),
            "old=bearer"
        );
    }
    #[test]
    fn no_backup_storage_probe_does_not_require_project_postgres() {
        let absent = storage_pg(true, || Err(UpdateError::usage("no project PostgreSQL")));
        assert!(matches!(absent, Ok(None)));
        assert!(storage_pg(false, || Err(UpdateError::usage("no project PostgreSQL"))).is_err());
        assert_eq!(
            storage_pg(false, || Ok("selected-pg".into())).unwrap(),
            Some("selected-pg".into())
        );
    }
}
