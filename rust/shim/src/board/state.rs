//! Private state reservations are bound to opened file identity.
use serde_json::Value;
use std::{
    collections::HashMap,
    fs::{self, File, OpenOptions},
    future::Future,
    io::{Read, Write},
    panic::AssertUnwindSafe,
    path::{Path, PathBuf},
    sync::{LazyLock, Mutex as StdMutex},
    time::{Duration, SystemTime},
};
use tokio::sync::{mpsc, oneshot};

type Result<T> = std::result::Result<T, &'static str>;
type Job = Box<dyn FnOnce() + Send>;

/// Queue `job` behind every job already queued for `path`. Each path has one
/// worker task that runs its jobs one at a time on the blocking pool; a new
/// worker replaces one whose runtime has shut down.
fn enqueue(path: &Path, job: Job) {
    static WORKERS: LazyLock<StdMutex<HashMap<PathBuf, mpsc::UnboundedSender<Job>>>> =
        LazyLock::new(Default::default);
    let mut workers = WORKERS.lock().unwrap_or_else(|poison| poison.into_inner());
    let job = match workers.get(path) {
        Some(jobs) => match jobs.send(job) {
            Ok(()) => return,
            Err(mpsc::error::SendError(job)) => job,
        },
        None => job,
    };
    let (jobs, mut queued) = mpsc::unbounded_channel::<Job>();
    tokio::spawn(async move {
        while let Some(job) = queued.recv().await {
            let _ = tokio::task::spawn_blocking(job).await;
        }
    });
    let _ = jobs.send(job);
    workers.insert(path.to_owned(), jobs);
}
/// Run blocking state-file work for `path` off the async runtime.
///
/// The shim runs a current-thread runtime, so a durable write there stalls
/// every tool call: on the Windows dev VM on 2026-10-19 one `sync_all` took
/// up to 3.1 s beside other tests. The job is queued when this is called,
/// not when the future is first polled, and jobs for one path run in that
/// order. Dropping the future does not cancel the job: a caller abandoned
/// mid-write cannot let a later step on the same file overtake the write.
/// A result whose caller was already gone when the job finished is dropped
/// before the next job starts; one whose caller leaves after that is dropped
/// with the caller's future.
pub fn offload<T, J>(path: &Path, job: J) -> impl Future<Output = Result<T>> + use<T, J>
where
    T: Send + 'static,
    J: FnOnce() -> Result<T> + Send + 'static,
{
    let (reply, answer) = oneshot::channel();
    enqueue(
        path,
        Box::new(move || {
            // A refused send hands the result back; it is dropped here, on
            // the blocking thread, before the next job for this path starts.
            let _ = reply.send(std::panic::catch_unwind(AssertUnwindSafe(job)));
        }),
    );
    async move {
        match answer.await {
            Ok(Ok(result)) => result,
            Ok(Err(panic)) => std::panic::resume_unwind(panic),
            Err(_) => Err("state_unavailable"),
        }
    }
}
/// Wait until every job already queued for `path` has finished.
pub async fn drain(path: &Path) {
    let _ = offload(path, || Ok(())).await;
}
pub struct PrivateFile {
    pub file: File,
    pub identity: Vec<u128>,
}
pub struct Reservation {
    path: PathBuf,
    identity: Vec<u128>,
    _lock: Option<File>,
    saved: bool,
}

fn identity(file: &File) -> Result<Vec<u128>> {
    identity_detailed(file).map_err(|error| error.code())
}

/// Preserve the failing operation's I/O error across handle cleanup.
#[derive(Debug)]
pub enum OpenError {
    Io(std::io::Error),
    InvalidState,
}
impl OpenError {
    pub(crate) fn code(&self) -> &'static str {
        match self {
            Self::Io(_) => "state_unavailable",
            Self::InvalidState => "invalid_state",
        }
    }
}
impl std::fmt::Display for OpenError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "state_unavailable; {:?}: {error}", error.kind()),
            Self::InvalidState => formatter.write_str("invalid_state"),
        }
    }
}

fn identity_detailed(file: &File) -> std::result::Result<Vec<u128>, OpenError> {
    let meta = file.metadata().map_err(OpenError::Io)?;
    if !meta.is_file() {
        return Err(OpenError::InvalidState);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.uid() != rustix::process::getuid().as_raw()
            || meta.mode() & 0o077 != 0
            || meta.nlink() != 1
        {
            return Err(OpenError::InvalidState);
        }
        Ok(vec![meta.dev() as u128, meta.ino() as u128])
    }
    #[cfg(windows)]
    {
        crate::credentials::windows_security::validate(file)
            .map(|parts| parts[..2].to_vec())
            .map_err(|_| OpenError::InvalidState)
    }
}
fn options(create: bool, write: bool) -> OpenOptions {
    let mut options = OpenOptions::new();
    options.read(true).write(write).create_new(create);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x00200000).share_mode(0x1 | 0x2);
        if create {
            options.access_mode(0x0012019f | 0x000c0000);
        }
    }
    options
}
pub fn open(path: &Path) -> Result<PrivateFile> {
    open_with_write(path, false)
}
pub fn open_with_write(path: &Path, write: bool) -> Result<PrivateFile> {
    open_detailed_with_write(path, write).map_err(|error| error.code())
}
pub fn open_detailed(path: &Path) -> std::result::Result<PrivateFile, OpenError> {
    open_detailed_with_write(path, false)
}
pub(crate) fn open_detailed_with_write(
    path: &Path,
    write: bool,
) -> std::result::Result<PrivateFile, OpenError> {
    crate::credentials::reject_ancestor_redirects(path).map_err(|_| OpenError::InvalidState)?;
    let file = options(false, write).open(path).map_err(OpenError::Io)?;
    let identity = identity_detailed(&file)?;
    Ok(PrivateFile { file, identity })
}
pub fn create(path: &Path) -> Result<PrivateFile> {
    crate::credentials::reject_ancestor_redirects(path).map_err(|_| "invalid_state")?;
    let file = options(true, true)
        .open(path)
        .map_err(|_| "state_unavailable")?;
    #[cfg(windows)]
    {
        // This helper is supplied by the credential module's existing security boundary.
        crate::credentials::windows_security::make_private(&file).map_err(|_| "invalid_state")?;
    }
    let identity = identity(&file)?;
    Ok(PrivateFile { file, identity })
}
pub fn read(path: &Path, limit: usize) -> Result<Vec<u8>> {
    read_opened(open(path)?, limit)
}
/// Open a digest marker the plugin hooks share with the shim: `.seen`,
/// `.wake`, `.wake-armed`, `.wait-armed` and `.reprint`. Hooks released
/// before 0600 markers wrote them under the user's umask (0644 under the
/// usual 022), and readable-by-others is harmless for a marker that holds a
/// timestamp or token. What [`open`]'s owner-only rule guards against still
/// holds: a marker another user owns or can write could forge a wake or
/// suppress delivery, so a POSIX marker must be owned by this user and not
/// group- or world-writable, and is refused with one stderr line naming the
/// path and the fix otherwise. The other checks of [`open`] hold too: no
/// redirected ancestor, no symlink, one regular file with a single link.
/// On Windows the hooks write markers through Git Bash or PowerShell, whose
/// files carry inherited, unprotected DACLs that [`open`]'s protected
/// owner-only rule refuses; the same guard is applied instead: the owner is
/// this user, SYSTEM or Administrators, and no other principal can write it
/// (`windows_security::validate_marker`).
pub fn open_marker(path: &Path, write: bool) -> Result<PrivateFile> {
    #[cfg(windows)]
    {
        crate::credentials::reject_ancestor_redirects(path).map_err(|_| "invalid_state")?;
        let file = options(false, write)
            .open(path)
            .map_err(|_| "state_unavailable")?;
        let (identity, distrust) = crate::credentials::windows_security::validate_marker(&file)
            .map_err(|_| "invalid_state")?;
        if let Some(distrust) = distrust {
            report_marker_once(path, &marker_refusal_windows(path, &distrust));
            return Err("invalid_state");
        }
        Ok(PrivateFile { file, identity })
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        crate::credentials::reject_ancestor_redirects(path).map_err(|_| "invalid_state")?;
        let file = options(false, write)
            .open(path)
            .map_err(|_| "state_unavailable")?;
        let meta = file.metadata().map_err(|_| "state_unavailable")?;
        if !meta.is_file() || meta.nlink() != 1 {
            return Err("invalid_state");
        }
        let own = rustix::process::getuid().as_raw();
        if let Some(refusal) = marker_refusal(path, own, meta.uid(), meta.mode()) {
            report_marker_once(path, &refusal);
            return Err("invalid_state");
        }
        let identity = vec![meta.dev() as u128, meta.ino() as u128];
        Ok(PrivateFile { file, identity })
    }
}
/// Why a POSIX marker may not be trusted, as the line [`open_marker`]
/// prints: the path, the cause and the one fix. `None` accepts it.
#[cfg(unix)]
fn marker_refusal(path: &Path, own: u32, owner: u32, mode: u32) -> Option<String> {
    let shown = path.display();
    if owner != own {
        return Some(format!(
            "pseudolife-mcp: ignoring {shown}: it is owned by another user, so it \
             could forge or suppress board wakes for this session. Fix: remove it \
             (rm '{shown}'); the plugin hooks write it again."
        ));
    }
    if mode & 0o022 != 0 {
        return Some(format!(
            "pseudolife-mcp: ignoring {shown}: other users can write it (mode {:04o}), so it \
             could forge or suppress board wakes for this session. Fix: update the plugin \
             (its hooks write these files 0600), or chmod 600 '{shown}'.",
            mode & 0o7777
        ));
    }
    None
}
/// Why a Windows marker may not be trusted, as the line [`open_marker`]
/// prints: the path, the cause and the one fix.
#[cfg(windows)]
fn marker_refusal_windows(
    path: &Path,
    distrust: &crate::credentials::windows_security::MarkerDistrust,
) -> String {
    use crate::credentials::windows_security::MarkerDistrust;
    let shown = path.display();
    let folder = path
        .parent()
        .map(|parent| parent.display().to_string())
        .unwrap_or_default();
    match distrust {
        MarkerDistrust::Owner(owner) => format!(
            "pseudolife-mcp: ignoring {shown}: it is owned by {owner}, not this user, so it could forge or suppress board wakes for this session. Fix: delete that file; the plugin hooks write it again."
        ),
        MarkerDistrust::Writer(writer) => {
            // Named, not %USERNAME%: Git Bash and PowerShell do not expand it.
            let me = crate::credentials::windows_security::current_user_name()
                .unwrap_or_else(|| "<this account>".into());
            format!(
                "pseudolife-mcp: ignoring {shown}: {writer} can change or delete it, so it could forge or suppress board wakes for this session. Fix: make {folder} private to this account (icacls \"{folder}\" /inheritance:r /grant:r \"{me}:(OI)(CI)F\"), then delete the file; the plugin hooks write it again."
            )
        }
        MarkerDistrust::NoDacl => format!(
            "pseudolife-mcp: ignoring {shown}: it has no access list, so every user can write it and it could forge or suppress board wakes for this session. Fix: delete that file; the plugin hooks write it again."
        ),
    }
}
/// One line per directory and marker kind per process: each Stop-hook run
/// arms a fresh `<key>.<token>.wake-armed`, so a per-path set would repeat
/// the same refusal at every turn end.
fn report_marker_once(path: &Path, line: &str) {
    static REPORTED: LazyLock<StdMutex<std::collections::HashSet<(PathBuf, String)>>> =
        LazyLock::new(Default::default);
    let kind = (
        path.parent().map(Path::to_path_buf).unwrap_or_default(),
        path.extension()
            .map(|e| e.to_string_lossy().into_owned())
            .unwrap_or_default(),
    );
    if REPORTED
        .lock()
        .map(|mut seen| seen.insert(kind))
        .unwrap_or(false)
    {
        eprintln!("{line}");
    }
}
/// [`read`] for a marker a hook may have written (see [`open_marker`]).
pub fn read_marker(path: &Path, limit: usize) -> Result<Vec<u8>> {
    read_opened(open_marker(path, false)?, limit)
}
fn read_opened(opened: PrivateFile, limit: usize) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    opened
        .file
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "state_unavailable")?;
    if bytes.len() > limit {
        return Err("invalid_state");
    }
    Ok(bytes)
}
fn same(path: &Path, expected: &[u128]) -> bool {
    open(path).is_ok_and(|file| file.identity == expected)
}
pub fn atomic_write(path: &Path, bytes: &[u8], expected: Option<&[u128]>) -> Result<()> {
    atomic_replace(path, bytes, expected, false, false)
}
/// [`atomic_write`] over a marker a hook may have written (see
/// [`open_marker`]); the replacement itself is owner-only.
pub fn atomic_write_marker(path: &Path, bytes: &[u8]) -> Result<()> {
    atomic_replace(path, bytes, None, false, true)
}
pub fn atomic_create(path: &Path, bytes: &[u8]) -> Result<()> {
    atomic_replace(path, bytes, None, true, false)
}
pub fn exists(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}
fn atomic_replace(
    path: &Path,
    bytes: &[u8],
    expected: Option<&[u128]>,
    exclusive: bool,
    marker: bool,
) -> Result<()> {
    if exists(path) {
        if exclusive {
            return Err("reservation_changed");
        }
        if marker {
            open_marker(path, false)?;
        } else {
            open(path)?;
        }
    }
    let parent = path.parent().ok_or("invalid_state")?;
    let temp = parent.join(format!(".agent-state-{}", uuid::Uuid::new_v4().simple()));
    let result = (|| {
        let mut opened = create(&temp)?;
        #[cfg(test)]
        if path
            .file_name()
            .is_some_and(|name| name.to_string_lossy().starts_with("slow-write"))
        {
            std::thread::sleep(tests::WRITE_DELAY);
        }
        opened
            .file
            .write_all(bytes)
            .and_then(|_| opened.file.sync_all())
            .map_err(|_| "state_unavailable")?;
        if expected.is_some_and(|identity| !same(path, identity)) {
            return Err("reservation_changed");
        }
        if exclusive && exists(path) {
            return Err("reservation_changed");
        }
        drop(opened);
        fs::rename(&temp, path).map_err(|_| "state_unavailable")?;
        #[cfg(unix)]
        {
            File::open(parent)
                .and_then(|file| file.sync_all())
                .map_err(|_| "state_unavailable")?;
        }
        Ok(())
    })();
    let _ = fs::remove_file(&temp);
    result
}
/// Create `path` (and missing ancestors) as a folder only this user can
/// use, or make an existing one so.
///
/// Another task may create the same folder between the check and the
/// create: the shim's handshake cache creates `<state dir>/handshake-cache`,
/// and with it the state directory, while the board reserves its state file
/// (#1324: on the Windows dev VM a refused `create_dir` here left a session
/// without a board address on a first start). A folder that appeared
/// meanwhile is treated like one that existed before the call: `path`
/// itself is protected, and an ancestor is left as its creator made it.
/// Protecting that ancestor would cut off what its creator is putting in
/// it: on Windows the protected DACL has no inheritable entries, so the
/// cache's `handshake-cache` inside it would be left with none.
pub fn private_dir(path: &Path) -> Result<()> {
    private_dir_at(path, true)
}
fn private_dir_at(path: &Path, leaf: bool) -> Result<()> {
    crate::credentials::reject_ancestor_redirects(path).map_err(|_| "invalid_state")?;
    if !path.is_dir() {
        if let Some(parent) = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty() && !p.is_dir())
        {
            private_dir_at(parent, false)?;
        }
        #[cfg(test)]
        tests::create_meanwhile(path);
        #[cfg(unix)]
        let created = {
            use std::os::unix::fs::DirBuilderExt;
            fs::DirBuilder::new().mode(0o700).create(path)
        };
        #[cfg(windows)]
        let created = fs::create_dir(path);
        match created {
            Ok(()) => {}
            // Never a symlink: the protection below must not follow one.
            Err(error)
                if error.kind() == std::io::ErrorKind::AlreadyExists
                    && fs::symlink_metadata(path).is_ok_and(|meta| meta.is_dir()) =>
            {
                if !leaf {
                    return Ok(());
                }
            }
            Err(_) => return Err("state_unavailable"),
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|_| "state_unavailable")?;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        let directory = OpenOptions::new()
            .access_mode(0x00120089 | 0x000c0000)
            .share_mode(0x1 | 0x2)
            .custom_flags(0x02000000 | 0x00200000)
            .open(path)
            .map_err(|_| "state_unavailable")?;
        crate::credentials::windows_security::make_private(&directory)
            .map_err(|_| "invalid_state")?;
    }
    Ok(())
}
/// Why the private state file at `path` or its folder is refused, when a
/// fresh look finds a reason: for the one line a stopped registration
/// prints. `None` when nothing is wrong now, or the cause is not a
/// permission or redirect one.
pub fn diagnose(path: &Path) -> Option<String> {
    if crate::credentials::reject_ancestor_redirects(path).is_err() {
        return Some(
            "a folder on its path is a symlink or junction, which private state never follows"
                .into(),
        );
    }
    let folder = path.parent()?;
    let meta = fs::symlink_metadata(folder).ok()?;
    if !meta.is_dir() {
        return Some(format!("{} is not a plain folder", folder.display()));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let own = rustix::process::getuid().as_raw();
        if meta.uid() != own {
            return Some(format!("{} is owned by another user", folder.display()));
        }
        let file = fs::symlink_metadata(path).ok()?;
        if file.uid() != own {
            return Some("the file is owned by another user".into());
        }
        if file.mode() & 0o077 != 0 {
            return Some(format!(
                "other users can use the file (mode {:04o})",
                file.mode() & 0o7777
            ));
        }
        if file.nlink() != 1 || !file.is_file() {
            return Some("the file is not one plain file".into());
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // READ_CONTROL | FILE_READ_ATTRIBUTES, not following a reparse point.
        let opened = |target: &Path, flags: u32| {
            OpenOptions::new()
                .access_mode(0x0002_0000 | 0x80)
                .share_mode(0x1 | 0x2)
                .custom_flags(flags | 0x0020_0000)
                .open(target)
        };
        let Ok(directory) = opened(folder, 0x0200_0000) else {
            return Some(format!(
                "this user cannot read the permissions of {}",
                folder.display()
            ));
        };
        if crate::credentials::windows_security::validate(&directory).is_err() {
            return Some(format!(
                "the access list of {} is not private to this user (it lets other accounts in, or another account owns it)",
                folder.display()
            ));
        }
        if exists(path) {
            let Ok(file) = opened(path, 0) else {
                return Some("this user cannot read the file's permissions".into());
            };
            if crate::credentials::windows_security::validate(&file).is_err() {
                return Some(
                    "the file's access list is not private to this user (it lets other accounts in, or another account owns it)"
                        .into(),
                );
            }
        }
    }
    None
}
impl Reservation {
    pub fn load_or_reserve(path: &Path, url: &str) -> Result<(Option<Value>, Option<Self>)> {
        if let Some(parent) = path.parent() {
            private_dir(parent)?;
        }
        if !exists(path) {
            // Another start of this session may create it between the
            // check and the create: that start holds the reservation now.
            #[cfg(test)]
            tests::create_file_meanwhile(path);
            let opened = create(path).map_err(|code| {
                if exists(path) {
                    "registration_in_progress"
                } else {
                    code
                }
            })?;
            return Ok((
                None,
                Some(Self {
                    path: path.to_owned(),
                    identity: opened.identity,
                    _lock: None,
                    saved: false,
                }),
            ));
        }
        let opened = open(path)?;
        if opened
            .file
            .metadata()
            .map_err(|_| "state_unavailable")?
            .len()
            != 0
        {
            let mut bytes = Vec::new();
            opened
                .file
                .take(16385)
                .read_to_end(&mut bytes)
                .map_err(|_| "state_unavailable")?;
            let value: Value = serde_json::from_slice(&bytes).map_err(|_| "invalid_state")?;
            if bytes.len() > 16384
                || value.get("bank_url").and_then(Value::as_str) != Some(url)
                || ["agent_id", "credential"].iter().any(|key| {
                    value
                        .get(key)
                        .and_then(Value::as_str)
                        .is_none_or(str::is_empty)
                })
            {
                return Err("invalid_state");
            }
            return Ok((Some(value), None));
        }
        let stale = |file: &File| {
            file.metadata()
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|time| SystemTime::now().duration_since(time).ok())
                .is_some_and(|age| age >= Duration::from_secs(60))
        };
        if !stale(&opened.file) {
            return Err("registration_in_progress");
        }
        let lock_path = path.with_file_name(format!(
            "{}.lock",
            path.file_name().ok_or("invalid_state")?.to_string_lossy()
        ));
        let lock = if lock_path.exists() {
            options(false, true)
                .open(&lock_path)
                .map_err(|_| "invalid_state")?
        } else {
            create(&lock_path)?.file
        };
        identity(&lock)?;
        lock.try_lock().map_err(|_| "registration_in_progress")?;
        let fresh = open(path)?;
        if fresh.identity != opened.identity
            || fresh
                .file
                .metadata()
                .map_err(|_| "state_unavailable")?
                .len()
                != 0
            || !stale(&fresh.file)
        {
            return Err("reservation_changed");
        }
        Ok((
            None,
            Some(Self {
                path: path.to_owned(),
                identity: opened.identity,
                _lock: Some(lock),
                saved: false,
            }),
        ))
    }
    pub fn save(&mut self, value: &Value) -> Result<()> {
        let bytes = serde_json::to_vec(value).map_err(|_| "invalid_state")?;
        atomic_write(&self.path, &bytes, Some(&self.identity))?;
        self.saved = true;
        Ok(())
    }
}
impl Drop for Reservation {
    fn drop(&mut self) {
        if !self.saved
            && let Ok(opened) = open(&self.path)
            && opened.identity == self.identity
            && opened.file.metadata().is_ok_and(|meta| meta.len() == 0)
        {
            drop(opened);
            let _ = fs::remove_file(&self.path);
        }
        if let Some(lock) = &self._lock {
            let _ = lock.unlock();
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };

    /// Writes to files named `slow-write*` take this long under test.
    pub(super) const WRITE_DELAY: Duration = Duration::from_millis(300);

    thread_local! {
        /// Folders another task creates while [`private_dir`] on this
        /// thread is between its check and its create.
        static MEANWHILE: std::cell::RefCell<Vec<PathBuf>> =
            const { std::cell::RefCell::new(Vec::new()) };
        /// A symlink (path, target) another process plants at that moment.
        static LINKED_MEANWHILE: std::cell::RefCell<Option<(PathBuf, PathBuf)>> =
            const { std::cell::RefCell::new(None) };
    }
    thread_local! {
        /// A state file another start creates at that moment.
        static FILE_MEANWHILE: std::cell::RefCell<Option<PathBuf>> =
            const { std::cell::RefCell::new(None) };
    }
    pub(super) fn create_file_meanwhile(path: &Path) {
        if FILE_MEANWHILE.with(|file| file.borrow().as_deref() == Some(path)) {
            drop(create(path).unwrap());
        }
    }

    #[test]
    fn a_state_file_another_start_creates_meanwhile_is_its_reservation() {
        let root = Dir::new();
        let path = root.0.join("key.json");
        FILE_MEANWHILE.with(|file| *file.borrow_mut() = Some(path.clone()));
        let reserved = Reservation::load_or_reserve(&path, "http://x");
        FILE_MEANWHILE.with(|file| *file.borrow_mut() = None);
        // Retried in the background, not a stop (state_unavailable).
        assert!(matches!(reserved, Err("registration_in_progress")));
        assert!(exists(&path));
    }

    pub(super) fn create_meanwhile(path: &Path) {
        if MEANWHILE.with(|folders| folders.borrow().iter().any(|folder| folder == path)) {
            // As the handshake cache's `create_dir_all` does: the folder and
            // its `handshake-cache` child, which it then writes into.
            fs::create_dir_all(path.join("handshake-cache")).unwrap();
        }
        #[cfg(unix)]
        if let Some((link, target)) = LINKED_MEANWHILE.with(|linked| linked.borrow().clone())
            && link == path
        {
            std::os::unix::fs::symlink(target, link).unwrap();
        }
    }

    #[test]
    fn a_state_folder_another_task_creates_meanwhile_is_still_made_private() {
        // A home that inherits its access, as a user's profile does (a
        // private root would give everything under it explicit entries).
        let home =
            std::env::temp_dir().join(format!("pseudolife-race-{}", uuid::Uuid::new_v4().simple()));
        let root = Dir(home.clone());
        let state_dir = root.0.join(".pseudolife-mcp").join("agent-state");
        let bound = state_dir.join("0c33202fbddfe6ee6eebb386fea0df74");
        // Each level the reservation creates can be raced; on the Windows
        // dev VM (#1324) the cache made `agent-state` meanwhile, with its
        // `handshake-cache` child, under a `.pseudolife-mcp` it had made.
        for raced in [&state_dir, &bound] {
            let _ = fs::remove_dir_all(&home);
            fs::create_dir_all(root.0.join(".pseudolife-mcp")).unwrap();
            MEANWHILE.with(|folders| *folders.borrow_mut() = vec![raced.clone()]);
            let reserved = Reservation::load_or_reserve(&bound.join("key.json"), "http://x");
            MEANWHILE.with(|folders| folders.borrow_mut().clear());
            let (saved, reservation) =
                reserved.unwrap_or_else(|code| panic!("{}: {code}", raced.display()));
            assert!(saved.is_none() && reservation.is_some());
            // The state file's own folder is private, whoever created it.
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                assert_eq!(
                    fs::metadata(&bound).unwrap().permissions().mode() & 0o777,
                    0o700
                );
            }
            #[cfg(windows)]
            {
                use std::os::windows::fs::OpenOptionsExt;
                let directory = OpenOptions::new()
                    .access_mode(0x0002_0000 | 0x80)
                    .custom_flags(0x0200_0000 | 0x0020_0000)
                    .open(&bound)
                    .unwrap();
                crate::credentials::windows_security::validate(&directory).unwrap();
            }
            // What the cache made in the state folder still works for it:
            // protecting that folder on Windows emptied the child's DACL.
            // (The cache never creates inside the state file's own folder.)
            if raced == &state_dir {
                fs::write(raced.join("handshake-cache").join("cache.json"), b"{}")
                    .unwrap_or_else(|error| panic!("{}: {error}", raced.display()));
            }
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_that_appears_meanwhile_is_not_adopted() {
        let root = Dir::new();
        let target = root.0.join("elsewhere");
        fs::create_dir(&target).unwrap();
        let link = root.0.join("state");
        // Planted between private_dir's check and its create, where an
        // existing folder is now accepted: a symlink there is not.
        LINKED_MEANWHILE.with(|linked| *linked.borrow_mut() = Some((link.clone(), target.clone())));
        let result = private_dir(&link);
        LINKED_MEANWHILE.with(|linked| *linked.borrow_mut() = None);
        assert_eq!(result, Err("state_unavailable"));
        assert!(
            fs::symlink_metadata(&link)
                .unwrap()
                .file_type()
                .is_symlink()
        );
    }

    #[test]
    fn diagnose_names_a_state_folder_others_can_use() {
        let root = Dir::new();
        let file = root.0.join("key.json");
        assert_eq!(diagnose(&file), None);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::write(&file, b"{}").unwrap();
            fs::set_permissions(&file, fs::Permissions::from_mode(0o644)).unwrap();
            assert_eq!(
                diagnose(&file).as_deref(),
                Some("other users can use the file (mode 0644)")
            );
        }
        #[cfg(windows)]
        {
            set_dacl(&root.0, "D:(A;;FA;;;OW)(A;OICI;FA;;;WD)");
            let reason = diagnose(&file).unwrap();
            assert!(
                reason.contains("is not private to this user")
                    && reason.contains(&root.0.display().to_string()),
                "{reason}"
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_marker_others_can_write_or_another_user_owns_is_refused_with_its_fix() {
        let path = Path::new("/home/u/.pseudolife-mcp/digests/k.wake");
        for mode in [0o100600, 0o100640, 0o100644, 0o100444] {
            assert_eq!(marker_refusal(path, 1000, 1000, mode), None, "{mode:o}");
        }
        for (mode, shown) in [
            (0o100664, "0664"),
            (0o100666, "0666"),
            (0o100646, "0646"),
            (0o100620, "0620"),
        ] {
            let line = marker_refusal(path, 1000, 1000, mode).expect("refused");
            assert!(line.contains(&path.display().to_string()), "{line}");
            assert!(line.contains(&format!("mode {shown}")), "{line}");
            assert!(
                line.contains("Fix: update the plugin") && line.contains("chmod 600"),
                "{line}"
            );
        }
        let line = marker_refusal(path, 1000, 0, 0o100600).expect("refused");
        assert!(
            line.contains("owned by another user,")
                && !line.contains("uid")
                && line.contains("Fix: remove it"),
            "{line}"
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_marker_rule_trusts_this_user_system_and_administrators_only() {
        use crate::credentials::windows_security::{MarkerDistrust, marker_distrust_sddl};
        const ME: &str = "S-1-5-21-100-200-300-1001";
        // Power Users: a non-admin group, standing in for another account.
        const OTHER: &str = "S-1-5-32-547";
        let rule = |sddl: &str| {
            marker_distrust_sddl(&sddl.replace("ME", ME).replace("OTHER", OTHER), Some(ME))
        };
        // What Git Bash and PowerShell leave: inherited, unprotected entries
        // for this user, SYSTEM and Administrators, a read-only logon SID,
        // and (Cygwin) a read-control-only Everyone entry.
        for sddl in [
            "O:MED:AI(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;ME)",
            "O:MED:(A;;FA;;;ME)(A;;FA;;;SY)(A;;0x1200a9;;;S-1-5-5-0-123456)",
            "O:MED:(A;;0x1f019f;;;ME)(A;;0x120080;;;WD)(A;;FR;;;BU)",
            "O:MED:P(A;;FA;;;OW)",
            // An elevated token's files are owned by Administrators.
            "O:BAD:(A;;FA;;;BA)(A;;FA;;;SY)(A;;FA;;;ME)",
            "O:SYD:(A;;FA;;;SY)",
            // Entries that grant nothing here: inherit-only, or a denial.
            "O:MED:(A;;FA;;;ME)(A;OICIIO;FA;;;WD)(D;;FA;;;OTHER)",
        ] {
            assert_eq!(rule(sddl), None, "{sddl}");
        }
        assert!(
            matches!(
                rule("O:OTHERD:(A;;FA;;;OTHER)(A;;FA;;;ME)"),
                Some(MarkerDistrust::Owner(_))
            ),
            "another user's marker"
        );
        for (sddl, who) in [
            ("O:MED:(A;;FA;;;ME)(A;;FW;;;WD)", "Everyone"),
            (
                "O:MED:(A;;FA;;;ME)(A;;0x1301bf;;;AU)",
                "Authenticated Users",
            ),
            ("O:MED:(A;;FA;;;ME)(A;;FA;;;BU)", "Users"),
            ("O:MED:(A;;FA;;;ME)(A;;0x4;;;OTHER)", "Power Users"),
            ("O:MED:(A;;FA;;;ME)(A;;WD;;;OTHER)", "Power Users"),
            ("O:MED:(A;;FA;;;ME)(A;;GW;;;OTHER)", "Power Users"),
            // Deleting or backdating a marker suppresses a wake too.
            ("O:MED:(A;;FA;;;ME)(A;;SD;;;BU)", "Users"),
            ("O:MED:(A;;FA;;;ME)(A;;0x100;;;BU)", "Users"),
        ] {
            match rule(sddl) {
                Some(MarkerDistrust::Writer(name)) => assert!(name.contains(who), "{sddl}: {name}"),
                other => panic!("{sddl}: {other:?}"),
            }
        }
        assert_eq!(
            rule("O:MED:NO_ACCESS_CONTROL"),
            Some(MarkerDistrust::NoDacl)
        );
        // The real token user is trusted too, not only the synthetic one.
        assert_eq!(marker_distrust_sddl("O:SYD:(A;;FA;;;SY)", None), None);
    }

    #[cfg(windows)]
    #[test]
    fn windows_marker_rule_refuses_a_malformed_access_list() {
        use crate::credentials::windows_security::{MarkerDistrust, marker_distrust_raw};
        // ACL (revision 2, size 28, one entry) holding ACCESS_ALLOWED_ACE
        // (size 20, FILE_ALL_ACCESS) for Everyone (S-1-1-0), little-endian.
        let well_formed = || {
            vec![
                0x001C_0002u32,
                0x0000_0001,
                0x0014_0000,
                0x001F_01FF,
                0x0000_0101,
                0x0100_0000,
                0x0000_0000,
            ]
        };
        match marker_distrust_raw(&mut well_formed()) {
            Some(MarkerDistrust::Writer(name)) => assert!(name.contains("Everyone"), "{name}"),
            other => panic!("{other:?}"),
        }
        let unreadable = Some(MarkerDistrust::Writer(
            "an access rule that cannot be read".into(),
        ));
        // More entries counted than the list holds.
        let mut counted = well_formed();
        counted[1] = 0x0000_0002;
        assert_eq!(marker_distrust_raw(&mut counted), unreadable);
        // An entry shorter than its own header.
        let mut short = well_formed();
        short[2] = 0x0002_0000;
        assert_eq!(marker_distrust_raw(&mut short), unreadable);
    }

    /// A folder whose children inherit their access, as in a user profile:
    /// the profile's own entries (SYSTEM, Administrators and this account,
    /// inherited by everything below). The temporary folder itself can carry
    /// more: on a host with the Codex sandbox its sandbox accounts may modify
    /// everything under %TEMP%, which the marker rule rightly refuses.
    #[cfg(windows)]
    fn inheriting_folder(tag: &str) -> Dir {
        let path = std::env::temp_dir().join(format!(
            "pseudolife-{tag}-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir(&path).unwrap();
        let me = crate::credentials::windows_security::current_user_name().unwrap();
        let status = std::process::Command::new("icacls")
            .arg(&path)
            .args([
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
            ])
            .arg(format!("{me}:(OI)(CI)F"))
            .arg("/Q")
            .stdout(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(status.success(), "icacls on {}", path.display());
        Dir(path)
    }

    /// Git Bash, as Claude Code runs the `.sh` hooks on Windows.
    #[cfg(windows)]
    fn git_bash() -> PathBuf {
        let output = std::process::Command::new("git")
            .arg("--exec-path")
            .output()
            .expect("git is on PATH");
        let exec = PathBuf::from(String::from_utf8_lossy(&output.stdout).trim());
        exec.ancestors()
            .map(|dir| dir.join("bin").join("bash.exe"))
            .find(|bash| bash.is_file())
            .unwrap_or_else(|| panic!("no Git Bash beside {}", exec.display()))
    }

    #[cfg(windows)]
    fn bash(script: &str, args: &[&Path]) {
        let status = std::process::Command::new(git_bash())
            .arg("-c")
            .arg(script)
            .arg("hook")
            .args(
                args.iter()
                    .map(|path| path.to_string_lossy().replace('\\', "/")),
            )
            .status()
            .unwrap();
        assert!(status.success(), "{script}");
    }

    #[cfg(windows)]
    fn powershell_write(path: &Path, text: &str) {
        // `-Command` joins trailing arguments into the script text, so the
        // values travel in the environment instead.
        let status = std::process::Command::new("powershell")
            .args(["-NoProfile", "-NonInteractive", "-Command"])
            .arg("[IO.File]::WriteAllText($env:MARKER_PATH, $env:MARKER_TEXT)")
            .env("MARKER_PATH", path)
            .env("MARKER_TEXT", text)
            .status()
            .unwrap();
        assert!(status.success());
    }

    /// Set a file's or folder's DACL from SDDL, as its owner may.
    #[cfg(windows)]
    fn set_dacl(path: &Path, sddl: &str) {
        crate::credentials::windows_security::set_dacl_sddl(path, sddl).unwrap();
    }

    #[cfg(windows)]
    const MARKERS: [&str; 4] = ["k.wake", "k.0123abcd.wake-armed", "k.seen", "k.reprint"];

    #[cfg(windows)]
    #[test]
    fn windows_markers_written_by_git_bash_and_powershell_are_read() {
        let root = Dir::new();
        // The digest folder as the shim makes it (owner-only), as a hook's
        // `mkdir -p` under umask 077 makes it, and as plain inheritance from
        // the user's temporary folder leaves it.
        let private = root.0.join("private");
        private_dir(&private).unwrap();
        let hooked = root.0.join("hooked");
        bash(r#"umask 077; mkdir -p "$1""#, &[&hooked]);
        let _inherited = inheriting_folder("markers");
        let inherited = _inherited.0.clone();
        for folder in [&private, &hooked, &inherited] {
            for marker in MARKERS {
                let bashed = folder.join(format!("bash-{marker}"));
                // stop-wake.sh and coordination-start.sh: umask 077, a
                // temporary file, then mv -f over the marker.
                bash(
                    r#"umask 077; printf '%s\n' 1789 > "$1.$$" && mv -f "$1.$$" "$1""#,
                    &[&bashed],
                );
                // lifecycle.ps1 (Codex on Windows).
                let shelled = folder.join(format!("ps-{marker}"));
                powershell_write(&shelled, "1789\n");
                for path in [&bashed, &shelled] {
                    assert_eq!(
                        read_marker(path, 64).as_deref(),
                        Ok(&b"1789\n"[..]),
                        "{}",
                        path.display()
                    );
                    // A hook-written marker is also replaced in place.
                    atomic_write_marker(path, b"1790\n").unwrap();
                    assert_eq!(read_marker(path, 64).unwrap(), b"1790\n");
                }
            }
        }
    }

    #[cfg(windows)]
    #[test]
    fn windows_marker_another_principal_can_write_is_refused() {
        let root = Dir::new();
        for (index, grant) in ["(A;;FW;;;WD)", "(A;;FA;;;S-1-5-32-547)"]
            .into_iter()
            .enumerate()
        {
            let path = root.0.join(format!("k{index}.wake"));
            powershell_write(&path, "1\n");
            assert!(read_marker(&path, 64).is_ok());
            set_dacl(&path, &format!("D:(A;;FA;;;OW)(A;;FA;;;SY){grant}"));
            assert_eq!(read_marker(&path, 64), Err("invalid_state"), "{grant}");
            // The guard also keeps a forged marker from being adopted on write.
            assert_eq!(atomic_write_marker(&path, b"2\n"), Err("invalid_state"));
        }
    }

    #[cfg(windows)]
    #[test]
    fn windows_marker_refusal_names_the_path_the_writer_and_the_fix() {
        use crate::credentials::windows_security::MarkerDistrust;
        let path = Path::new(r"C:\Users\<user>\.pseudolife-mcp\digests\k.wake");
        let line = marker_refusal_windows(path, &MarkerDistrust::Writer("Everyone".into()));
        assert!(line.contains(&path.display().to_string()), "{line}");
        assert!(line.contains("Everyone can change or delete it"), "{line}");
        assert!(
            line.contains(r#"icacls "C:\Users\<user>\.pseudolife-mcp\digests" /inheritance:r"#),
            "{line}"
        );
        // The fix names this account: %USERNAME% expands only in cmd, and
        // agents run the line in Git Bash or PowerShell.
        let me = crate::credentials::windows_security::current_user_name().unwrap();
        assert!(
            line.contains(&format!("/grant:r \"{me}:(OI)(CI)F\"")),
            "{line}"
        );
        assert!(!line.contains('%') && !line.contains("del \""), "{line}");
        let line = marker_refusal_windows(path, &MarkerDistrust::Owner(r"HOST\other".into()));
        assert!(
            line.contains(r"owned by HOST\other, not this user")
                && line.contains("Fix: delete that file"),
            "{line}"
        );
        let line = marker_refusal_windows(path, &MarkerDistrust::NoDacl);
        assert!(
            line.contains("no access list") && line.contains("Fix: delete that file"),
            "{line}"
        );
        for distrust in [
            MarkerDistrust::Writer("Everyone".into()),
            MarkerDistrust::Owner("other".into()),
            MarkerDistrust::NoDacl,
        ] {
            let line = marker_refusal_windows(path, &distrust);
            assert!(!line.contains("  ") && !line.contains('\n'), "{line}");
        }
    }

    struct Dir(PathBuf);
    impl Dir {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "pseudolife-state-{}",
                uuid::Uuid::new_v4().simple()
            ));
            private_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[tokio::test]
    async fn offloaded_write_leaves_the_runtime_responsive() {
        let dir = Dir::new();
        let path = dir.0.join("slow-write-state");
        let ticks = Arc::new(AtomicUsize::new(0));
        let ticker = tokio::spawn({
            let ticks = ticks.clone();
            async move {
                loop {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                    ticks.fetch_add(1, Ordering::Relaxed);
                }
            }
        });
        tokio::task::yield_now().await;
        let target = path.clone();
        offload(&path, move || atomic_write(&target, b"{}", None))
            .await
            .unwrap();
        ticker.abort();
        // A write that blocked this current-thread runtime would leave the
        // ticker unpolled for the whole delay.
        assert!(
            ticks.load(Ordering::Relaxed) >= 5,
            "runtime stalled during the write: {} ticks",
            ticks.load(Ordering::Relaxed)
        );
        assert_eq!(read(&path, 16).unwrap(), b"{}");
    }

    #[tokio::test]
    async fn a_dropped_write_finishes_before_the_next_job_on_its_path() {
        let dir = Dir::new();
        let path = dir.0.join("slow-write-state");
        let (target, key) = (path.clone(), path.clone());
        let writer = tokio::spawn(async move {
            offload(&key, move || atomic_write(&target, b"saved", None)).await
        });
        // Let the write start, then drop its caller as a retry timeout would.
        tokio::time::sleep(Duration::from_millis(50)).await;
        writer.abort();
        let target = path.clone();
        assert_eq!(
            offload(&path, move || read(&target, 16)).await.unwrap(),
            b"saved"
        );
    }

    #[tokio::test]
    async fn a_write_is_queued_when_offload_is_called_not_when_polled() {
        let dir = Dir::new();
        let path = dir.0.join("state");
        let target = path.clone();
        // A caller may queue a write under a lock and never await it.
        drop(offload(&path, move || {
            atomic_write(&target, b"queued", None)
        }));
        let target = path.clone();
        assert_eq!(
            offload(&path, move || read(&target, 16)).await.unwrap(),
            b"queued"
        );
    }

    #[tokio::test]
    async fn drain_waits_for_a_dropped_write() {
        let dir = Dir::new();
        let path = dir.0.join("slow-write-state");
        let (target, key) = (path.clone(), path.clone());
        let writer = tokio::spawn(async move {
            offload(&key, move || atomic_write(&target, b"saved", None)).await
        });
        tokio::time::sleep(Duration::from_millis(50)).await;
        writer.abort();
        // Close paths drain before cleaning up, so no write lands after them.
        drain(&path).await;
        assert_eq!(fs::read(&path).unwrap(), b"saved");
    }

    #[tokio::test]
    async fn a_panicking_job_reaches_its_caller_and_frees_the_path() {
        let dir = Dir::new();
        let path = dir.0.join("state");
        let key = path.clone();
        let panicked =
            tokio::spawn(async move { offload::<(), _>(&key, || panic!("job failed")).await })
                .await
                .unwrap_err();
        assert!(panicked.is_panic());
        let target = path.clone();
        offload(&path, move || atomic_write(&target, b"{}", None))
            .await
            .unwrap();
    }
}
