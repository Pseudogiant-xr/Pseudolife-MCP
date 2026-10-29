//! Pinned native runtimes (the lite tier's PostgreSQL, and later others)
//! downloaded into a Pseudolife-owned directory: fetch over HTTPS, check
//! each artifact's sha256 against its pin, unpack into a staging tree,
//! validate it, and publish it with one rename. The consumer supplies the
//! pins, how each artifact is laid into the tree, and what a complete tree
//! is (`pg_runtime` for PostgreSQL).
//!
//! An install is all or nothing. It runs under an OS lock on the root (two
//! daemons may provision at once), clears staging trees left by runs that
//! died, syncs every file before the rename, and records the pins it was
//! built from in the tree; a later start trusts a directory only while that
//! record matches the build's pins. Each pin set has its own directory, so
//! a changed pin installs beside a runtime a running process may be using.

use sha2::{Digest, Sha256};
use std::{
    ffi::OsString,
    fmt, fs,
    io::{self, BufReader, BufWriter, Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Platform {
    Windows,
    MacOs,
    Other,
}
impl Platform {
    pub fn current() -> Self {
        if cfg!(windows) {
            Self::Windows
        } else if cfg!(target_os = "macos") {
            Self::MacOs
        } else {
            Self::Other
        }
    }
}

/// `embedded_pg.default_lite_data_dir()`: the stable per-user data dir.
/// `env` answers nonempty values only as Python's `os.environ.get(..) or`
/// does; `home` is `Path.home()`. `None` when no home is known.
pub fn default_data_dir(
    platform: Platform,
    env: &dyn Fn(&str) -> Option<OsString>,
    home: Option<&Path>,
) -> Option<PathBuf> {
    let set = |name: &str| {
        env(name)
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
    };
    let base = match platform {
        Platform::Windows => match set("LOCALAPPDATA") {
            Some(local) => local,
            None => home?.join("AppData").join("Local"),
        },
        Platform::MacOs => home?.join("Library").join("Application Support"),
        Platform::Other => match set("XDG_DATA_HOME") {
            Some(data) => data,
            None => home?.join(".local").join("share"),
        },
    };
    Some(base.join("pseudolife-mcp"))
}

/// The record an install carries of the artifacts it was built from.
pub const PROVENANCE: &str = "pseudolife-runtime.json";
const INSTALL_LOCK: &str = ".install.lock";
const STAGING_PREFIX: &str = ".staging-";
const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// A download that stalls this long between chunks fails rather than hangs.
const READ_TIMEOUT: Duration = Duration::from_secs(60);

/// One downloadable artifact and its sha256 (lowercase hex).
pub trait Pinned {
    fn url(&self) -> &str;
    fn sha256(&self) -> &str;
}

/// What to install: the pins and how the install is named and described.
pub struct Manifest<'a, A: Pinned> {
    /// What logs and errors call it, e.g. "PostgreSQL 18.1.0 runtime".
    pub label: &'a str,
    /// The install directory's name prefix, e.g. "18.1.0".
    pub version: &'a str,
    pub artifacts: &'a [A],
    /// Fields the provenance record carries beside the artifact list.
    pub record: serde_json::Map<String, serde_json::Value>,
}

impl<A: Pinned> Manifest<'_, A> {
    /// `<root>/<version>-<12 hex of the provenance record>`: the pins and
    /// the consumer's record fields (a layout version, say), so any change
    /// to either installs beside the old tree rather than failing on it.
    pub fn install_dir(&self, root: &Path) -> PathBuf {
        let record = serde_json::to_vec(&self.provenance()).expect("provenance serializes");
        let digest = Sha256::digest(&record);
        root.join(format!("{}-{}", self.version, hex(&digest[..6])))
    }

    fn provenance(&self) -> serde_json::Value {
        let mut record = self.record.clone();
        record.insert(
            "artifacts".into(),
            self.artifacts
                .iter()
                .map(|a| serde_json::json!({"url": a.url(), "sha256": a.sha256()}))
                .collect(),
        );
        serde_json::Value::Object(record)
    }
}

#[derive(Debug)]
pub enum Error {
    Download {
        label: String,
        url: String,
        detail: String,
    },
    Checksum {
        url: String,
        expected: String,
        actual: String,
    },
    Archive {
        url: String,
        detail: String,
    },
    /// The unpacked tree fails the consumer's check.
    Incomplete {
        label: String,
        detail: String,
    },
    Io {
        action: &'static str,
        path: PathBuf,
        source: io::Error,
    },
    /// An existing install that does not match the build's pins.
    Installed {
        dir: PathBuf,
        detail: String,
    },
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Download { label, url, detail } => write!(
                f,
                "could not download {url} ({detail}); nothing was installed. The {label} is \
                 downloaded once: check the network connection (behind a proxy, set \
                 HTTPS_PROXY) and start again."
            ),
            Self::Checksum {
                url,
                expected,
                actual,
            } => write!(
                f,
                "the download of {url} does not match its pinned sha256 (expected {expected}, \
                 got {actual}); nothing was installed. Start again; if this repeats, the file \
                 changed upstream, so report it rather than work around the pin."
            ),
            Self::Archive { url, detail } => {
                write!(
                    f,
                    "{url} cannot be installed: {detail}; nothing was installed."
                )
            }
            Self::Incomplete { label, detail } => write!(
                f,
                "the downloaded {label} is incomplete ({detail}); nothing was installed."
            ),
            Self::Io {
                action,
                path,
                source,
            } => write!(f, "could not {action} {}: {source}", path.display()),
            Self::Installed { dir, detail } => write!(
                f,
                "the runtime at {} does not match this build ({detail}). Move that directory \
                 aside and start again to reinstall it.",
                dir.display()
            ),
        }
    }
}
impl std::error::Error for Error {}

fn io_error<'a>(action: &'static str, path: &'a Path) -> impl FnOnce(io::Error) -> Error + 'a {
    move |source| Error::Io {
        action,
        path: path.to_path_buf(),
        source,
    }
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Where the artifact bytes come from; `HttpFetch` in production.
pub trait Fetch {
    /// Writes the body of `url` into `sink`; `Err` carries the reason.
    fn fetch(&self, url: &str, sink: &mut dyn Write) -> Result<(), String>;
}

/// HTTPS download with redirects (GitHub and Launchpad both redirect to
/// their file stores) and the environment's proxy settings. It runs its own
/// single-thread Tokio runtime on a separate thread, so it works whether or
/// not the caller is already inside a runtime.
pub struct HttpFetch;

impl Fetch for HttpFetch {
    fn fetch(&self, url: &str, sink: &mut dyn Write) -> Result<(), String> {
        let (sender, chunks) = std::sync::mpsc::sync_channel::<Result<Vec<u8>, String>>(8);
        std::thread::scope(|scope| {
            scope.spawn(move || download(url, sender));
            for chunk in chunks {
                sink.write_all(&chunk?).map_err(|e| e.to_string())?;
            }
            Ok(())
        })
    }
}

fn download(url: &str, sender: std::sync::mpsc::SyncSender<Result<Vec<u8>, String>>) {
    let result = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| e.to_string())
        .and_then(|runtime| {
            runtime.block_on(async {
                let client = reqwest::Client::builder()
                    .https_only(true)
                    .connect_timeout(CONNECT_TIMEOUT)
                    .read_timeout(READ_TIMEOUT)
                    .user_agent(concat!("pseudolife-mcp/", env!("CARGO_PKG_VERSION")))
                    .build()
                    .map_err(|e| describe(&e))?;
                let mut response = client
                    .get(url)
                    .send()
                    .await
                    .and_then(reqwest::Response::error_for_status)
                    .map_err(|e| describe(&e))?;
                while let Some(chunk) = response.chunk().await.map_err(|e| describe(&e))? {
                    if sender.send(Ok(chunk.to_vec())).is_err() {
                        break;
                    }
                }
                Ok(())
            })
        });
    if let Err(detail) = result {
        let _ = sender.send(Err(detail));
    }
}

/// An error with its causes, since reqwest's own text omits them.
fn describe(error: &dyn std::error::Error) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        text.push_str(": ");
        text.push_str(&cause.to_string());
        source = cause.source();
    }
    text
}

/// The install for this manifest: `None` when there is none, `Installed`
/// when the directory exists but its record or `validate` disagrees.
pub fn locate<A: Pinned, T>(
    root: &Path,
    manifest: &Manifest<'_, A>,
    validate: &dyn Fn(&Path) -> Result<T, String>,
) -> Result<Option<T>, Error> {
    let dir = manifest.install_dir(root);
    match fs::symlink_metadata(&dir) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(io_error("inspect", &dir)(e)),
        Ok(_) => {}
    }
    let installed = |detail: String| Error::Installed {
        dir: dir.clone(),
        detail,
    };
    let text = fs::read(dir.join(PROVENANCE))
        .map_err(|e| installed(format!("its {PROVENANCE} cannot be read: {e}")))?;
    let recorded: serde_json::Value = serde_json::from_slice(&text)
        .map_err(|e| installed(format!("its {PROVENANCE} is not JSON: {e}")))?;
    if recorded != manifest.provenance() {
        return Err(installed(format!(
            "its {PROVENANCE} records other artifacts or layout than this build's"
        )));
    }
    validate(&dir).map(Some).map_err(installed)
}

/// Installs the manifest under `root` unless it is already there; `true`
/// when this call installed it. `unpack` lays one downloaded artifact into
/// the staging tree, `validate` checks the finished tree (and, on success,
/// the published one), and `log` receives one line per step.
pub fn provision<A: Pinned, T>(
    root: &Path,
    manifest: &Manifest<'_, A>,
    fetch: &dyn Fetch,
    log: &mut dyn FnMut(&str),
    unpack: &mut dyn FnMut(&A, &Path, &Path) -> Result<(), String>,
    validate: &dyn Fn(&Path) -> Result<T, String>,
) -> Result<(T, bool), Error> {
    let already = |dir: &Path| format!("{} already installed at {}", manifest.label, dir.display());
    let dir = manifest.install_dir(root);
    if let Some(found) = locate(root, manifest, validate)? {
        log(&already(&dir));
        return Ok((found, false));
    }
    fs::create_dir_all(root).map_err(io_error("create", root))?;
    let _lock = InstallLock::acquire(root, &mut || {
        log(&format!(
            "waiting for another process installing the {} in {}",
            manifest.label,
            root.display()
        ))
    })?;
    // Another process may have installed it while this one waited.
    if let Some(found) = locate(root, manifest, validate)? {
        log(&already(&dir));
        return Ok((found, false));
    }
    clear_staging(root, log);
    let mut name = [0u8; 8];
    getrandom::fill(&mut name).map_err(|e| Error::Io {
        action: "name a staging directory in",
        path: root.to_path_buf(),
        source: io::Error::other(e.to_string()),
    })?;
    let staging = root.join(format!("{STAGING_PREFIX}{}", hex(&name)));
    let result = install(root, &staging, manifest, fetch, log, unpack, validate);
    // Whatever is left: the downloads, or the whole tree after a failure.
    let _ = fs::remove_dir_all(&staging);
    result.map(|found| (found, true))
}

/// Staging trees of runs that died; the install lock proves no live run is
/// using them. Best effort: each run stages under a fresh name, so one that
/// cannot be removed yet (a scanner holding a file) only costs disk space.
fn clear_staging(root: &Path, log: &mut dyn FnMut(&str)) {
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        if !entry
            .file_name()
            .to_string_lossy()
            .starts_with(STAGING_PREFIX)
        {
            continue;
        }
        let path = entry.path();
        let removed = if entry.file_type().is_ok_and(|t| t.is_dir()) {
            fs::remove_dir_all(&path)
        } else {
            fs::remove_file(&path)
        };
        if let Err(e) = removed {
            log(&format!(
                "left an abandoned staging directory {} in place ({e})",
                path.display()
            ));
        }
    }
}

fn install<A: Pinned, T>(
    root: &Path,
    staging: &Path,
    manifest: &Manifest<'_, A>,
    fetch: &dyn Fetch,
    log: &mut dyn FnMut(&str),
    unpack: &mut dyn FnMut(&A, &Path, &Path) -> Result<(), String>,
    validate: &dyn Fn(&Path) -> Result<T, String>,
) -> Result<T, Error> {
    // Owner-only until published, so no other account can add or swap a
    // file between the unpack and the rename; the install keeps that mode.
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    let tree = staging.join("tree");
    builder.create(&tree).map_err(io_error("create", &tree))?;
    let download = staging.join("download");
    let total = manifest.artifacts.len();
    for (index, artifact) in manifest.artifacts.iter().enumerate() {
        let url = artifact.url();
        let name = url.rsplit('/').next().unwrap_or(url);
        log(&format!("downloading {name} ({} of {total})", index + 1));
        fetch_verified(manifest.label, artifact, &download, fetch)?;
        unpack(artifact, &download, &tree).map_err(|detail| Error::Archive {
            url: url.to_owned(),
            detail,
        })?;
    }
    validate(&tree).map_err(|detail| Error::Incomplete {
        label: manifest.label.to_owned(),
        detail,
    })?;
    let record = tree.join(PROVENANCE);
    let text = serde_json::to_vec_pretty(&manifest.provenance()).expect("provenance serializes");
    write_new(&record, &mut &text[..], 0o644).map_err(io_error("write", &record))?;
    #[cfg(unix)]
    sync_dirs(&tree).map_err(io_error("sync", &tree))?;
    let dir = manifest.install_dir(root);
    rename_dir(&tree, &dir).map_err(io_error("move the runtime into", &dir))?;
    #[cfg(unix)]
    fs::File::open(root)
        .and_then(|d| d.sync_all())
        .map_err(io_error("sync", root))?;
    log(&format!(
        "installed the {} at {}",
        manifest.label,
        dir.display()
    ));
    validate(&dir).map_err(|detail| Error::Installed {
        dir,
        detail: format!(
            "{detail} right after it was installed, so something else changed it, such as \
             an antivirus quarantine"
        ),
    })
}

/// Every directory of the tree, deepest first, so its entries (links
/// included, which `write_new`'s file syncs do not cover) are on disk
/// before the rename publishes them; a power loss after the rename then
/// leaves the whole tree or none of it. NTFS journals directory entries.
#[cfg(unix)]
fn sync_dirs(dir: &Path) -> io::Result<()> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            sync_dirs(&entry.path())?;
        }
    }
    fs::File::open(dir)?.sync_all()
}

/// Antivirus scanners briefly hold newly written files open on Windows,
/// which fails a directory rename with access denied; it clears in moments.
fn rename_dir(from: &Path, to: &Path) -> io::Result<()> {
    let mut attempt = 0;
    loop {
        match fs::rename(from, to) {
            Err(e)
                if cfg!(windows) && e.kind() == io::ErrorKind::PermissionDenied && attempt < 50 =>
            {
                attempt += 1;
                std::thread::sleep(Duration::from_millis(100));
            }
            other => return other,
        }
    }
}

/// Downloads into `path` and checks the bytes against the pin.
fn fetch_verified(
    label: &str,
    artifact: &dyn Pinned,
    path: &Path,
    fetch: &dyn Fetch,
) -> Result<(), Error> {
    let file = fs::File::create(path).map_err(io_error("create", path))?;
    let mut sink = Hashing {
        inner: BufWriter::new(file),
        hash: Sha256::new(),
        failed: None,
    };
    let fetched = fetch.fetch(artifact.url(), &mut sink);
    if let Some(source) = sink.failed.take() {
        // The disk, not the network, stopped it.
        return Err(io_error("write", path)(source));
    }
    fetched.map_err(|detail| Error::Download {
        label: label.to_owned(),
        url: artifact.url().to_owned(),
        detail,
    })?;
    sink.inner.flush().map_err(io_error("write", path))?;
    let actual = hex(&sink.hash.finalize());
    if actual != artifact.sha256() {
        return Err(Error::Checksum {
            url: artifact.url().to_owned(),
            expected: artifact.sha256().to_owned(),
            actual,
        });
    }
    Ok(())
}

struct Hashing<W: Write> {
    inner: W,
    hash: Sha256,
    failed: Option<io::Error>,
}
impl<W: Write> Write for Hashing<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self.inner.write(buf) {
            Ok(n) => {
                self.hash.update(&buf[..n]);
                Ok(n)
            }
            Err(e) => {
                let copy = io::Error::new(e.kind(), e.to_string());
                self.failed = Some(e);
                Err(copy)
            }
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

/// Serializes installs into one root across processes; the OS releases it
/// when the holder dies.
struct InstallLock {
    file: fs::File,
}
impl InstallLock {
    /// Blocks until held, calling `waiting` once first if another process
    /// holds it (a download can take minutes; the caller should not look
    /// hung meanwhile).
    fn acquire(root: &Path, waiting: &mut dyn FnMut()) -> Result<Self, Error> {
        let path = root.join(INSTALL_LOCK);
        let file = fs::OpenOptions::new()
            .read(true)
            .append(true)
            .create(true)
            .open(&path)
            .map_err(io_error("open", &path))?;
        match file.try_lock() {
            Ok(()) => {}
            Err(fs::TryLockError::WouldBlock) => {
                waiting();
                file.lock().map_err(io_error("lock", &path))?;
            }
            Err(fs::TryLockError::Error(e)) => return Err(io_error("lock", &path)(e)),
        }
        Ok(Self { file })
    }
}
impl Drop for InstallLock {
    fn drop(&mut self) {
        let _ = self.file.unlock();
    }
}

// ── unpacking helpers for consumers ─────────────────────────────────────

/// A downloaded `.tar.gz` as a reader.
pub fn gzip(path: &Path) -> Result<impl Read, String> {
    let file = fs::File::open(path).map_err(|e| e.to_string())?;
    Ok(flate2::read::GzDecoder::new(BufReader::new(file)))
}

/// The components of an archive entry's path, or why it is refused:
/// anything absolute, with a parent step, a backslash or a drive or stream
/// colon.
fn components(raw: &str) -> Result<Vec<&str>, String> {
    if raw.starts_with('/') || raw.contains('\\') || raw.contains(':') {
        return Err(format!("entry {raw:?} is not a plain relative path"));
    }
    let parts: Vec<&str> = raw
        .split('/')
        .filter(|p| !p.is_empty() && *p != ".")
        .collect();
    if parts.contains(&"..") {
        return Err(format!("entry {raw:?} would leave the install directory"));
    }
    Ok(parts)
}

/// How an archive's paths map into the install tree.
#[derive(Default)]
pub struct Layout {
    /// The archive holds one top directory, which becomes the tree; an
    /// entry outside it, or a file beside it, is refused.
    strip_top: bool,
    top: Option<String>,
}
impl Layout {
    /// Paths are kept as they are (an archive with `bin/`, `lib/`, ... at
    /// its root).
    pub fn as_is() -> Self {
        Self::default()
    }
    /// One top directory (`postgresql-18.1.0-<target>/`) becomes the tree.
    pub fn strip_top() -> Self {
        Self {
            strip_top: true,
            top: None,
        }
    }
    /// The entry's path in the tree (`None` for the top directory itself),
    /// or why it is refused.
    pub fn place(&mut self, raw: &str, is_dir: bool) -> Result<Option<PathBuf>, String> {
        let parts = components(raw)?;
        if !self.strip_top {
            return Ok((!parts.is_empty()).then(|| parts.iter().collect()));
        }
        let Some(first) = parts.first() else {
            return Ok(None);
        };
        match &self.top {
            Some(top) if top != first => {
                return Err(format!(
                    "entry {raw:?} is outside the archive's top directory {top:?}"
                ));
            }
            Some(_) => {}
            None => self.top = Some((*first).to_owned()),
        }
        if parts.len() == 1 && !is_dir {
            return Err(format!(
                "entry {raw:?} is a file beside the archive's top directory"
            ));
        }
        Ok((parts.len() > 1).then(|| parts[1..].iter().collect()))
    }
}

/// The last component of an archive entry's path, for helpers that place
/// files by name; refused unless it is a plain file name.
pub fn file_name(raw: &str) -> Result<&str, String> {
    let name = components(raw)?.last().copied().unwrap_or("");
    if name.is_empty() {
        return Err(format!("entry {raw:?} has no file name"));
    }
    Ok(name)
}

/// A new file with the archive's mode less group and other write (owner
/// read and write always), synced so the data is on disk before the rename
/// publishes the tree.
pub fn write_new(path: &Path, body: &mut dyn Read, mode: u32) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        // Never wider than intended while it is written; the umask only
        // narrows it, so the exact mode is set again below.
        options.mode((mode & 0o755) | 0o600);
    }
    let mut file = options.open(path)?;
    io::copy(body, &mut file)?;
    file.sync_all()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode((mode & 0o755) | 0o600))?;
    }
    #[cfg(not(unix))]
    let _ = mode;
    Ok(())
}

pub fn create_dir(path: &Path) -> io::Result<()> {
    fs::create_dir_all(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o755))?;
    }
    Ok(())
}

/// A link may only name a file beside it, as library links do
/// (`libpq.so.5 -> libpq.so.5.18`).
pub fn check_link(raw: &str, target: &str) -> Result<(), String> {
    if target.is_empty()
        || target == "."
        || target == ".."
        || target.contains('/')
        || target.contains('\\')
        || target.contains(':')
    {
        return Err(format!(
            "entry {raw:?} links to {target:?}, outside its own directory"
        ));
    }
    Ok(())
}

/// A tar archive laid into `tree`: directories, files and same-directory
/// links; anything else (hard links, devices) is refused.
pub fn unpack_tar_tree(reader: impl Read, tree: &Path, mut layout: Layout) -> Result<(), String> {
    let mut archive = tar::Archive::new(reader);
    for entry in archive.entries().map_err(|e| e.to_string())? {
        let mut entry = entry.map_err(|e| e.to_string())?;
        let raw = String::from_utf8_lossy(&entry.path_bytes()).into_owned();
        let is_dir = entry.header().entry_type() == tar::EntryType::Directory;
        let Some(relative) = layout.place(&raw, is_dir)? else {
            continue;
        };
        let dest = tree.join(&relative);
        match entry.header().entry_type() {
            tar::EntryType::Directory => create_dir(&dest).map_err(|e| e.to_string())?,
            tar::EntryType::Regular | tar::EntryType::Continuous => {
                let mode = entry.header().mode().map_err(|e| e.to_string())?;
                write_new(&dest, &mut entry, mode).map_err(|e| format!("{raw}: {e}"))?
            }
            tar::EntryType::Symlink => {
                let target = entry
                    .link_name_bytes()
                    .map(|t| String::from_utf8_lossy(&t).into_owned())
                    .unwrap_or_default();
                check_link(&raw, &target)?;
                // Only Unix archives carry links: a Windows tree skips them.
                #[cfg(unix)]
                {
                    if let Some(parent) = dest.parent() {
                        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
                    }
                    std::os::unix::fs::symlink(&target, &dest)
                        .map_err(|e| format!("{raw}: {e}"))?;
                }
            }
            other => {
                return Err(format!(
                    "entry {raw:?} is a {other:?}, which a runtime archive should not hold"
                ));
            }
        }
    }
    Ok(())
}

/// A zip archive laid into `tree`; links are refused.
pub fn unpack_zip_tree(path: &Path, tree: &Path, mut layout: Layout) -> Result<(), String> {
    let file = fs::File::open(path).map_err(|e| e.to_string())?;
    let mut archive = zip::ZipArchive::new(BufReader::new(file)).map_err(|e| e.to_string())?;
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(|e| e.to_string())?;
        // zip decodes a name on demand and can refuse it; refuse the archive
        // rather than skip the entry or guess at its name.
        let raw = entry
            .name()
            .map_err(|e| format!("entry {index} has an unreadable name: {e}"))?
            .into_owned();
        let Some(relative) = layout.place(&raw, entry.is_dir())? else {
            continue;
        };
        let dest = tree.join(&relative);
        if entry.is_dir() {
            create_dir(&dest).map_err(|e| e.to_string())?;
        } else if entry.is_symlink() {
            return Err(format!(
                "entry {raw:?} is a link, which a runtime zip should not hold"
            ));
        } else {
            let mode = entry.unix_mode().unwrap_or(0o644);
            write_new(&dest, &mut entry, mode).map_err(|e| format!("{raw}: {e}"))?;
        }
    }
    Ok(())
}
