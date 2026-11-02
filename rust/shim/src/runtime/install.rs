use super::*;
use futures::StreamExt;
use std::{
    io::{Cursor, Write},
    process::{Command, Stdio},
    time::Duration,
};

// A release contains exactly one CLI executable. Limits apply before allocation
// and during decompression, including when an archive lies about its size.
const ARCHIVE_LIMIT: usize = 128 * 1024 * 1024;
const BINARY_LIMIT: u64 = 256 * 1024 * 1024;

pub fn target_triple() -> Result<&'static str> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => Ok("x86_64-unknown-linux-gnu"),
        ("linux", "aarch64") => Ok("aarch64-unknown-linux-gnu"),
        ("macos", "x86_64") => Ok("x86_64-apple-darwin"),
        ("macos", "aarch64") => Ok("aarch64-apple-darwin"),
        ("windows", "x86_64") => Ok("x86_64-pc-windows-msvc"),
        _ => Err(
            "no native release binary for this platform; build the Rust CLI from a checkout".into(),
        ),
    }
}
pub fn archive_name(version: &str) -> Result<String> {
    if !valid_version(version) {
        return Err("invalid release version (omit the v prefix)".into());
    }
    Ok(format!(
        "pseudolife-mcp-{version}-{}.{}",
        target_triple()?,
        if cfg!(windows) { "zip" } else { "tar.gz" }
    ))
}
pub fn checksum(sums: &str, name: &str) -> Result<String> {
    let mut matched = None;
    for line in sums.lines() {
        let Some((digest, file)) = line.split_once("  ") else {
            return Err("invalid SHA256SUMS record".into());
        };
        if !valid_digest(digest) || file.is_empty() || file.contains(['/', '\\']) {
            return Err("invalid SHA256SUMS record".into());
        }
        if file == name {
            if matched.is_some() {
                return Err("duplicate archive checksum".into());
            }
            matched = Some(digest.to_owned());
        }
    }
    matched.ok_or_else(|| "release checksum does not name the platform archive".into())
}

fn unpack(bytes: &[u8]) -> Result<Vec<u8>> {
    if cfg!(windows) {
        unpack_zip(bytes)
    } else {
        unpack_tar(bytes)
    }
}
fn unpack_zip(bytes: &[u8]) -> Result<Vec<u8>> {
    let mut archive =
        zip::ZipArchive::new(Cursor::new(bytes)).map_err(|_| "invalid release zip archive")?;
    if archive.len() != 1 {
        return Err("release archive must contain exactly one root binary".into());
    }
    let mut entry = archive
        .by_index(0)
        .map_err(|_| "unreadable release zip entry")?;
    if entry.name().map_err(|_| "invalid release zip entry name")? != BINARY
        || !entry.is_file()
        || entry.size() > BINARY_LIMIT
        || entry
            .unix_mode()
            .is_some_and(|m| m & 0o170000 != 0 && m & 0o170000 != 0o100000)
    {
        return Err("release zip contains a link, non-root binary, or oversized entry".into());
    }
    let mut data = Vec::new();
    (&mut entry)
        .take(BINARY_LIMIT + 1)
        .read_to_end(&mut data)
        .map_err(io_error)?;
    if data.is_empty() || data.len() as u64 > BINARY_LIMIT {
        return Err("invalid release binary size".into());
    }
    Ok(data)
}
fn unpack_tar(bytes: &[u8]) -> Result<Vec<u8>> {
    let decoder = flate2::read::GzDecoder::new(Cursor::new(bytes));
    // The tar reader itself is bounded: padding, metadata and skipped entries
    // cannot inflate indefinitely before it reaches the only permitted file.
    let mut archive = tar::Archive::new(decoder.take(BINARY_LIMIT + 1024 * 1024));
    let mut data = None;
    for entry in archive.entries().map_err(io_error)? {
        let mut entry = entry.map_err(io_error)?;
        if data.is_some()
            || entry.path_bytes().as_ref() != BINARY.as_bytes()
            || !entry.header().entry_type().is_file()
            || entry.size() > BINARY_LIMIT
            || entry.header().mode().map_err(io_error)? & 0o7000 != 0
        {
            return Err(
                "release tar contains a link, additional file, non-root binary, or oversized entry"
                    .into(),
            );
        }
        let mut body = Vec::new();
        (&mut entry)
            .take(BINARY_LIMIT + 1)
            .read_to_end(&mut body)
            .map_err(io_error)?;
        if body.is_empty() || body.len() as u64 > BINARY_LIMIT {
            return Err("invalid release binary size".into());
        }
        data = Some(body);
    }
    data.ok_or_else(|| "release archive contains no binary".into())
}
fn native_header(bytes: &[u8]) -> bool {
    #[cfg(windows)]
    {
        bytes.starts_with(b"MZ")
    }
    #[cfg(target_os = "linux")]
    {
        bytes.starts_with(b"\x7fELF")
    }
    #[cfg(target_os = "macos")]
    {
        bytes.get(..4).is_some_and(|m| {
            [
                b"\xcf\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf",
                b"\xca\xfe\xba\xbe",
                b"\xbe\xba\xfe\xca",
            ]
            .iter()
            .any(|magic| magic.as_slice() == m)
        })
    }
    #[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
    {
        false
    }
}

/// Install an R3 archive. The caller holds `UpdateLock` through registration
/// migration and pruning; R5 can use the same guard for its setup transaction.
pub fn install_archive(
    layout: &Layout,
    version: &str,
    source: &str,
    archive: &[u8],
    sums: &str,
) -> Result<Runtime> {
    if archive.len() > ARCHIVE_LIMIT {
        return Err("release archive exceeds the download limit".into());
    }
    let digest = checksum(sums, &archive_name(version)?)?;
    if format!("{:x}", Sha256::digest(archive)) != digest {
        return Err("release archive checksum mismatch; nothing activated".into());
    }
    let data = unpack(archive)?;
    install_bytes(layout, version, source, None, Some(digest), &data)
}

/// Checkout artifact installation never invokes Python or modifies that checkout.
pub fn install_binary(
    layout: &Layout,
    version: &str,
    source: &str,
    source_commit: Option<String>,
    binary: &Path,
) -> Result<Runtime> {
    if !plain_file(binary) {
        return Err("native checkout executable is missing or redirected".into());
    }
    let mut data = Vec::new();
    fs::File::open(binary)
        .map_err(io_error)?
        .take(BINARY_LIMIT + 1)
        .read_to_end(&mut data)
        .map_err(io_error)?;
    install_bytes(layout, version, source, source_commit, None, &data)
}
fn install_bytes(
    layout: &Layout,
    version: &str,
    source: &str,
    source_commit: Option<String>,
    archive_sha256: Option<String>,
    data: &[u8],
) -> Result<Runtime> {
    if !valid_version(version) || data.len() as u64 > BINARY_LIMIT || !native_header(data) {
        return Err("invalid native executable or version; no runtime was activated".into());
    }
    crate::board::state::private_dir(&layout.root).map_err(str::to_owned)?;
    let highest = fs::read_dir(&layout.root)
        .map_err(io_error)?
        .filter_map(|e| e.ok())
        .filter_map(|e| e.file_name().to_str().and_then(sequence))
        .max()
        .unwrap_or(0);
    let mut next = highest + 1;
    let path = loop {
        if next > 999999 {
            return Err("runtime sequence exhausted; no runtime was activated".into());
        }
        let path = layout.root.join(format!("{next:06}"));
        match fs::create_dir(&path) {
            Ok(()) => break path,
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => next += 1,
            Err(e) => return Err(e.to_string()),
        }
    };
    let result = (|| {
        crate::board::state::private_dir(&path).map_err(str::to_owned)?;
        let binary = path.join(BINARY);
        let mut opened = crate::board::state::create(&binary).map_err(str::to_owned)?;
        opened
            .file
            .write_all(data)
            .and_then(|_| opened.file.sync_all())
            .map_err(io_error)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            opened
                .file
                .set_permissions(fs::Permissions::from_mode(0o755))
                .map_err(io_error)?;
        }
        drop(opened);
        let marker = Marker {
            format: 1,
            kind: "native".into(),
            version: version.into(),
            source: source.into(),
            source_commit,
            target: target_triple()?.into(),
            binary: BINARY.into(),
            installed_at: chrono::Utc::now().to_rfc3339(),
            binary_sha256: hash_file(&binary)?,
            archive_sha256,
        };
        // The selector's only completion signal is published after activation.
        // Dropping an uncommitted activation restores the old stable launcher.
        let activation = activate_launcher(layout, &binary)?;
        crate::board::state::atomic_create(
            &path.join(MARKER),
            &serde_json::to_vec_pretty(&marker).map_err(|e| e.to_string())?,
        )
        .map_err(str::to_owned)?;
        let runtime = read_runtime(&path)?;
        activation.commit();
        Ok(runtime)
    })();
    if result.is_err() {
        let _ = fs::remove_dir_all(&path);
    }
    result
}

pub(crate) struct Activation {
    launcher: PathBuf,
    backup: Option<PathBuf>,
    python_record: PathBuf,
    record_backup: Option<PathBuf>,
    committed: bool,
    changed: bool,
}
impl Activation {
    pub fn commit(mut self) {
        self.committed = true;
        // Windows can retain an open legacy image. Deletion then fails safely;
        // never stop a running session merely to remove its displaced launcher.
        for backup in [&self.backup, &self.record_backup].into_iter().flatten() {
            let _ = fs::remove_file(backup);
        }
    }
}
impl Drop for Activation {
    fn drop(&mut self) {
        if self.committed {
            return;
        }
        let restored = if !self.changed {
            true
        } else if let Some(backup) = &self.backup {
            // Replace atomically: a failed restore leaves the current launcher
            // and recovery copy intact, without an unlink-then-rename gap.
            rename_retry(backup, &self.launcher).is_ok()
        } else {
            fs::remove_file(&self.launcher).is_ok()
        };
        if restored && let Some(backup) = &self.record_backup {
            // Restore the Python cache only alongside its original launcher.
            let _ = rename_retry(backup, &self.python_record);
        }
    }
}
fn rename_retry(source: &Path, target: &Path) -> Result<()> {
    for attempt in 0..5 {
        match fs::rename(source, target) {
            Ok(()) => return Ok(()),
            Err(e) if e.kind() == io::ErrorKind::PermissionDenied && attempt < 4 => {
                std::thread::sleep(Duration::from_millis(100 * (attempt + 1)))
            }
            Err(e) => return Err(e.to_string()),
        }
    }
    unreachable!()
}
pub(crate) fn activate_launcher(layout: &Layout, binary: &Path) -> Result<Activation> {
    let parent = layout
        .launcher
        .parent()
        .ok_or("launcher has no parent directory")?;
    crate::board::state::private_dir(parent).map_err(str::to_owned)?;
    crate::credentials::reject_ancestor_redirects(&layout.launcher)
        .map_err(|_| "launcher path is redirected")?;
    let exists = fs::symlink_metadata(&layout.launcher).is_ok();
    if exists && !plain_file(&layout.launcher) {
        return Err("stable launcher is not a regular file; nothing replaced".into());
    }
    let unchanged = exists && hash_file(&layout.launcher)? == hash_file(binary)?;
    let id = uuid::Uuid::new_v4();
    let mut record_name = layout.launcher.as_os_str().to_os_string();
    record_name.push(".json");
    let python_record = PathBuf::from(record_name);
    let record_backup = match fs::symlink_metadata(&python_record) {
        Ok(meta) if meta.is_file() && !redirect(&meta) => {
            let backup = parent.join(format!(".python-launcher-record.old-native-{id}"));
            rename_retry(&python_record, &backup)?;
            Some(backup)
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        _ => return Err("Python launcher record is unavailable or redirected; repair the sibling launcher .json file before retrying".into()),
    };
    let mut activation = Activation {
        launcher: layout.launcher.clone(),
        backup: None,
        python_record,
        record_backup,
        committed: false,
        changed: false,
    };
    if unchanged {
        return Ok(activation);
    }
    let temp = parent.join(format!(".native-launcher-{id}"));
    let backup = exists.then(|| {
        parent.join(format!(
            "{}.old-native-{id}",
            layout.launcher.file_name().unwrap().to_string_lossy()
        ))
    });
    let result = (|| {
        fs::copy(binary, &temp).map_err(io_error)?;
        fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&temp)
            .and_then(|f| f.sync_all())
            .map_err(io_error)?;
        if let Some(backup) = &backup {
            #[cfg(windows)]
            rename_retry(&layout.launcher, backup)?;
            #[cfg(not(windows))]
            {
                fs::copy(&layout.launcher, backup).map_err(io_error)?;
            }
        }
        activation.backup = backup.clone();
        activation.changed = true;
        #[cfg(windows)]
        rename_retry(&temp, &layout.launcher)?;
        #[cfg(not(windows))]
        if let Err(error) = rename_retry(&temp, &layout.launcher) {
            // On POSIX rename failure leaves the original in place.
            activation.changed = false;
            if let Some(backup) = &activation.backup {
                let _ = fs::remove_file(backup);
            }
            return Err(error);
        }
        Ok(activation)
    })();
    let _ = fs::remove_file(&temp);
    result
}

pub fn install_source(source: &str, repo: Option<&Path>, layout: &Layout) -> Result<Runtime> {
    if let Some(repo) = repo.or_else(|| Path::new(source).is_dir().then(|| Path::new(source))) {
        let manifest = repo.join("rust/shim/Cargo.toml");
        let text = fs::read_to_string(&manifest).map_err(io_error)?;
        let parsed: toml::Value =
            toml::from_str(&text).map_err(|_| "checkout Rust manifest is invalid")?;
        let version = parsed
            .get("package")
            .and_then(|p| p.get("version"))
            .and_then(toml::Value::as_str)
            .ok_or("checkout has no Rust CLI version")?;
        let cargo = super::registrations::absolute_command("cargo").ok_or("cargo is not on an absolute PATH entry; install the Rust toolchain for checkout updates")?;
        // The native checkout path builds its own committed source, never a
        // stale shared target artifact and never a pip environment.
        let scratch =
            std::env::temp_dir().join(format!("pseudolife-native-build-{}", uuid::Uuid::new_v4()));
        ensure_build_space(repo)?;
        ensure_build_space(scratch.parent().ok_or("build directory has no parent")?)?;
        let result = (|| {
            let output = Command::new(cargo)
                .args([
                    "build",
                    "--release",
                    "--package",
                    "pseudolife-stdio",
                    "--manifest-path",
                ])
                .arg(repo.join("rust/Cargo.toml"))
                .env("CARGO_TARGET_DIR", &scratch)
                .stdin(Stdio::null())
                .stdout(Stdio::inherit())
                .stderr(Stdio::inherit())
                .status()
                .map_err(io_error)?;
            if !output.success() {
                return Err(format!(
                    "native checkout build failed with exit {}; nothing activated",
                    output.code().unwrap_or(1)
                ));
            }
            let git = super::registrations::absolute_command("git");
            let commit = git
                .and_then(|git| {
                    Command::new(git)
                        .arg("-C")
                        .arg(repo)
                        .args(["rev-parse", "HEAD"])
                        .output()
                        .ok()
                })
                .filter(|o| o.status.success())
                .and_then(|o| String::from_utf8(o.stdout).ok())
                .map(|s| s.trim().to_owned())
                .filter(|s| s.len() == 40 && s.bytes().all(|b| b.is_ascii_hexdigit()));
            install_binary(
                layout,
                version,
                source,
                commit,
                &scratch.join("release").join(if cfg!(windows) {
                    "pseudolife-stdio.exe"
                } else {
                    "pseudolife-stdio"
                }),
            )
        })();
        let _ = fs::remove_dir_all(scratch);
        return result;
    }
    let version = source.strip_prefix("pseudolife-mcp==").unwrap_or(source);
    let name = archive_name(version)?;
    let base =
        format!("https://github.com/Pseudogiant-xr/Pseudolife-MCP/releases/download/v{version}");
    // A private runtime avoids nested Tokio runtimes in synchronous CLI callers.
    let base_clone = base.clone();
    let name_clone = name.clone();
    let (archive, sums) = std::thread::spawn(move || -> Result<(Vec<u8>, String)> {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(io_error)?
            .block_on(async {
                let client = reqwest::Client::builder()
                    .timeout(Duration::from_secs(300))
                    .build()
                    .map_err(|e| e.to_string())?;
                let sums = fetch(&client, &format!("{base_clone}/SHA256SUMS"), 1024 * 1024).await?;
                let sums = String::from_utf8(sums).map_err(|_| "SHA256SUMS is not UTF-8")?;
                checksum(&sums, &name_clone)?;
                let archive = fetch(
                    &client,
                    &format!("{base_clone}/{name_clone}"),
                    ARCHIVE_LIMIT,
                )
                .await?;
                Ok((archive, sums))
            })
    })
    .join()
    .map_err(|_| "release download worker failed")??;
    install_archive(layout, version, source, &archive, &sums)
}

fn ensure_build_space(path: &Path) -> Result<()> {
    let available = available_space(path)?;
    if available < 8 * 1024 * 1024 * 1024 {
        return Err("less than 8 GiB is available for the native checkout build; no build or activation started".into());
    }
    Ok(())
}
#[cfg(unix)]
fn available_space(path: &Path) -> Result<u64> {
    let stats = rustix::fs::statvfs(path)
        .map_err(|_| "could not check available disk space; no build started")?;
    Ok((stats.f_bavail as u64).saturating_mul(stats.f_frsize as u64))
}
#[cfg(windows)]
#[allow(unsafe_code)]
fn available_space(path: &Path) -> Result<u64> {
    use std::os::windows::ffi::OsStrExt;
    let path: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    let mut available = 0u64;
    // Only the caller-available count is used (including user quotas), and
    // the UTF-16 directory buffer and u64 output remain live for the call.
    let result = unsafe {
        windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW(
            path.as_ptr(),
            &mut available,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if result == 0 {
        return Err("could not check available disk space; no build started".into());
    }
    Ok(available)
}
async fn fetch(client: &reqwest::Client, url: &str, limit: usize) -> Result<Vec<u8>> {
    let response = client
        .get(url)
        .send()
        .await
        .map_err(|_| "could not download the native release; existing runtime preserved")?
        .error_for_status()
        .map_err(|e| {
            format!(
                "native release download returned {}",
                e.status().map(|s| s.as_u16()).unwrap_or(0)
            )
        })?;
    if response.content_length().is_some_and(|n| n > limit as u64) {
        return Err("release download exceeds size limit".into());
    }
    let mut stream = response.bytes_stream();
    let mut bytes = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| "native release download interrupted")?;
        if bytes.len().saturating_add(chunk.len()) > limit {
            return Err("release download exceeds size limit".into());
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}
