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
    // Owner-only from creation on Windows: the folder may be one this
    // client uses without protecting it (see private_dir).
    #[cfg(windows)]
    let file = crate::credentials::windows_security::create_owner_only(path)
        .map_err(|_| "state_unavailable")?;
    #[cfg(unix)]
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
        // An account that may delete entries in the marker's folder can
        // suppress a wake whatever the marker's own access list says, so the
        // folder is held to the folder rule too.
        // The checked folder stays open, without delete sharing, until the
        // marker is: it cannot be renamed or replaced in between, so the
        // marker is opened in the folder that was checked.
        let _folder = match path.parent() {
            Some(folder) => Some(marker_folder_trusted(folder)?),
            None => None,
        };
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
        MarkerDistrust::Writer { name, sid } => format!(
            "pseudolife-mcp: ignoring {shown}: {name} can change or delete it, so it could forge or suppress board wakes for this session. Fix: make {folder} private to this account ({}), then delete the file; the plugin hooks write it again.",
            private_folder_command(&folder, Some(sid.as_str()))
        ),
        MarkerDistrust::Unreadable => format!(
            "pseudolife-mcp: ignoring {shown}: its access list holds an entry this client cannot read (a callback or object entry), which could let another account change it. Fix: delete that file; the plugin hooks write it again."
        ),
        MarkerDistrust::NoDacl => format!(
            "pseudolife-mcp: ignoring {shown}: it has no access list, so every user can write it and it could forge or suppress board wakes for this session. Fix: delete that file; the plugin hooks write it again."
        ),
    }
}
/// Marker folders that passed the folder rule, and when.
#[cfg(windows)]
static MARKER_FOLDERS: LazyLock<StdMutex<HashMap<PathBuf, FolderPass>>> =
    LazyLock::new(Default::default);
/// A marker folder's (volume, file index) and when it passed the rule.
#[cfg(windows)]
type FolderPass = ((u64, u64), std::time::Instant);
/// Check the folder a marker is read from with the folder rule
/// (`windows_security::validate_folder`), as `private_dir` does before
/// anything is written into one: no other account may add, delete or
/// replace entries in it. A folder that passes is not checked again for 30
/// seconds, because the Stop hook and the ring read markers often.
#[cfg(windows)]
fn marker_folder_trusted(folder: &Path) -> Result<File> {
    use std::os::windows::fs::OpenOptionsExt;
    const FRESH: Duration = Duration::from_secs(30);
    // READ_CONTROL | FILE_READ_ATTRIBUTES, not following a reparse point,
    // sharing read and write but not delete: while the caller holds this
    // handle the folder cannot be renamed or removed.
    let directory = OpenOptions::new()
        .access_mode(0x0002_0000 | 0x80)
        .share_mode(0x1 | 0x2)
        .custom_flags(0x02000000 | 0x00200000)
        .open(folder)
        .map_err(|_| "state_unavailable")?;
    // The pass is cached for the folder object it was made on (volume and
    // file index, read through this same handle), not its name: a folder
    // swapped in under the same name is checked afresh.
    let Ok(identity) = crate::credentials::windows_security::folder_identity(&directory) else {
        report_folder_once(
            folder,
            &format!(
                "pseudolife-mcp: not reading board markers in {}: it is not a plain folder (a symlink or junction, which private state never follows). Fix: point the digest folder at a plain folder, then retry.",
                folder.display()
            ),
        );
        return Err("invalid_state");
    };
    let fresh = MARKER_FOLDERS
        .lock()
        .map(|trusted| {
            trusted
                .get(folder)
                .is_some_and(|(seen, at)| *seen == identity && at.elapsed() < FRESH)
        })
        .unwrap_or(false);
    if fresh {
        return Ok(directory);
    }
    match crate::credentials::windows_security::validate_folder(&directory) {
        Ok(None) => {
            if let Ok(mut trusted) = MARKER_FOLDERS.lock() {
                trusted.insert(folder.to_path_buf(), (identity, std::time::Instant::now()));
            }
            Ok(directory)
        }
        Ok(Some(distrust)) => {
            report_folder_once(folder, &folder_refusal_windows(folder, &distrust));
            Err("invalid_state")
        }
        Err(error) => {
            report_folder_once(
                folder,
                &format!(
                    "pseudolife-mcp: not reading board markers in {}: its permissions could not be checked ({}). Fix: check that this account can read that folder's permissions, then retry.",
                    folder.display(),
                    error.0
                ),
            );
            Err("invalid_state")
        }
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
    // Whether this call made the folder, or another task did meanwhile.
    let mut created = false;
    let mut appeared = false;
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
        let made = {
            use std::os::unix::fs::DirBuilderExt;
            fs::DirBuilder::new().mode(0o700).create(path)
        };
        // Owner-only from the moment it exists: a child another task creates
        // before make_private would otherwise inherit entries the
        // protection then strips, leaving it with none (#1538 review).
        #[cfg(windows)]
        let made = crate::credentials::windows_security::create_dir_owner_only(path);
        #[cfg(test)]
        if made.is_ok() {
            tests::create_child_meanwhile(path);
        }
        match made {
            Ok(()) => created = true,
            // Never a symlink: the protection below must not follow one.
            Err(error)
                if error.kind() == std::io::ErrorKind::AlreadyExists
                    && fs::symlink_metadata(path).is_ok_and(|meta| meta.is_dir()) =>
            {
                if !leaf {
                    return Ok(());
                }
                appeared = true;
            }
            Err(_) => return Err("state_unavailable"),
        }
    }
    #[cfg(unix)]
    {
        // A mode change never takes access away from what is in the folder.
        let _ = (created, appeared);
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|_| "state_unavailable")?;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // Protect only a folder this call made, or one still empty. Any
        // other already holds what another client or task put there, whose
        // access may be only inherited: the protected DACL has no
        // inheritable entry, so protecting it would leave those files with
        // none (#1538). Such a folder must already be safe instead. (An
        // empty folder can be filled between this check and the change;
        // what lands in it then is as exposed as it was before this fix.)
        let empty = !created
            && !appeared
            && fs::read_dir(path).is_ok_and(|mut entries| entries.next().is_none());
        if created || empty {
            let directory = OpenOptions::new()
                .access_mode(0x00120089 | 0x000c0000)
                .share_mode(0x1 | 0x2)
                .custom_flags(0x02000000 | 0x00200000)
                .open(path)
                .map_err(|_| "state_unavailable")?;
            // An existing empty folder another account owns is refused, not
            // taken over: its owner may hold a handle that can re-grant
            // itself access after the change.
            if empty
                && let Ok(Some(
                    distrust @ crate::credentials::windows_security::MarkerDistrust::Owner(_),
                )) = crate::credentials::windows_security::validate_folder(&directory)
            {
                report_folder_once(path, &folder_refusal_windows(path, &distrust));
                return Err("invalid_state");
            }
            if crate::credentials::windows_security::make_private(&directory).is_err() {
                drop(directory);
                // Nothing is left half-done: a folder this call made goes
                // again; an empty one keeps the access it had.
                if created {
                    let _ = fs::remove_dir(path);
                }
                return Err("invalid_state");
            }
        } else {
            // READ_CONTROL | FILE_READ_ATTRIBUTES, not following a reparse
            // point: enough to read the owner and DACL, which the owner can
            // always do.
            let directory = OpenOptions::new()
                .access_mode(0x0002_0000 | 0x80)
                .share_mode(0x1 | 0x2 | 0x4)
                .custom_flags(0x02000000 | 0x00200000)
                .open(path)
                .map_err(|_| "state_unavailable")?;
            match crate::credentials::windows_security::validate_folder(&directory) {
                Ok(None) => {}
                Ok(Some(distrust)) => {
                    report_folder_once(path, &folder_refusal_windows(path, &distrust));
                    return Err("invalid_state");
                }
                Err(error) => {
                    report_folder_once(
                        path,
                        &format!(
                            "pseudolife-mcp: not using {}: its permissions could not be checked ({}). Fix: check that this account can read that folder's permissions, then retry.",
                            path.display(),
                            error.0
                        ),
                    );
                    return Err("invalid_state");
                }
            }
        }
    }
    Ok(())
}
/// The icacls command that makes `folder` private to this account while
/// keeping access for what is in it, and removes `writer`'s own grants
/// (`writer` as `S-1-...` text), which `/inheritance:r` (inherited entries
/// only) and `/grant:r` (only the named account) leave in place.
///
/// Accounts are named by SID (`*S-1-...`): no localisation, and no
/// characters a shell expands. The path is the one part a shell could
/// change: double quotes keep it safe to paste in cmd, PowerShell 5.1 and
/// PowerShell 7 unless it holds a character one of them expands (`$` or a
/// backtick in PowerShell, `%` or `!` in cmd, or a quote); then the command
/// is given for PowerShell, in single quotes, and says so.
#[cfg(windows)]
fn private_folder_command(folder: &str, writer: Option<&str>) -> String {
    let me = this_account_sid();
    let grant = format!("*{me}:(OI)(CI)F");
    let remove = writer
        .filter(|writer| writer.starts_with("S-1-"))
        .map(|writer| format!("*{writer}"));
    let mut parts = vec![
        ("/inheritance:r", false),
        ("/grant:r", false),
        (grant.as_str(), true),
    ];
    if let Some(remove) = &remove {
        parts.extend([("/remove:g", false), (remove.as_str(), true)]);
    }
    icacls_line(folder, &[parts])
}
#[cfg(windows)]
fn this_account_sid() -> String {
    crate::credentials::windows_security::current_user_sid()
        .unwrap_or_else(|| "<this account's SID>".into())
}
/// One or more icacls calls on `folder` as a line safe to paste: the path is
/// the one part a shell could change. Double quotes keep it safe in cmd,
/// PowerShell 5.1 and PowerShell 7 unless it holds a character one of them
/// expands or treats as a quote (`$` or a backtick in PowerShell, `%` or `!`
/// in cmd, straight or curly quotes, `^`); then, and for more than one call
/// (cmd and PowerShell join commands differently), the line is given for
/// PowerShell, single-quoted, and says so. Each call is a list of arguments,
/// `true` for a value to quote. A path ending in a backslash gets a `.`, so
/// the backslash cannot escape the closing quote (`"D:\"`).
#[cfg(windows)]
fn icacls_line(folder: &str, calls: &[Vec<(&str, bool)>]) -> String {
    let folder = if folder.ends_with('\\') {
        format!("{folder}.")
    } else {
        folder.to_owned()
    };
    let special = [
        '$', '`', '%', '!', '^', '\'', '"', '\u{2018}', '\u{2019}', '\u{201C}', '\u{201D}',
    ];
    let plain = calls.len() == 1 && !folder.contains(special);
    let quote = |text: &str| {
        if plain {
            format!("\"{text}\"")
        } else {
            format!("'{}'", text.replace('\'', "''"))
        }
    };
    let line = calls
        .iter()
        .map(|parts| {
            let rest: Vec<String> = parts
                .iter()
                .map(|(text, quoted)| {
                    if *quoted {
                        quote(text)
                    } else {
                        (*text).to_owned()
                    }
                })
                .collect();
            format!("icacls {} {}", quote(&folder), rest.join(" "))
        })
        .collect::<Vec<_>>()
        .join("; ");
    if plain {
        line
    } else {
        format!("in PowerShell: {line}")
    }
}
/// The fix for a folder whose access list holds an entry the rule cannot
/// read. icacls cannot name such an entry to remove it, so `/reset` drops
/// the folder's own entries; then the folder stops inheriting and grants this
/// account (by SID, inheritable) full control, because an entry like that can
/// also come from a parent, and a folder under one that passes nothing on is
/// left with no entries by `/reset` alone. The owner keeps WRITE_DAC however
/// empty the list is, so the second call always runs (from an elevated
/// prompt when Administrators owns the folder).
#[cfg(windows)]
fn unreadable_folder_command(folder: &str) -> String {
    let grant = format!("*{}:(OI)(CI)F", this_account_sid());
    icacls_line(
        folder,
        &[
            vec![("/reset", false)],
            vec![
                ("/inheritance:r", false),
                ("/grant:r", false),
                (grant.as_str(), true),
            ],
        ],
    )
}
/// The fix for a handshake cache folder this account cannot use: give this
/// account full control of that folder and what is in it, and nothing else.
/// Resetting an ancestor instead could unprotect the private state files
/// beside it, which are refused once not protected.
#[cfg(windows)]
pub(crate) fn cache_folder_fix(folder: &Path) -> String {
    let grant = format!("*{}:(OI)(CI)F", this_account_sid());
    icacls_line(
        &folder.display().to_string(),
        &[vec![
            ("/grant:r", false),
            (grant.as_str(), true),
            ("/T", false),
        ]],
    )
}
/// Why an existing Windows folder is not used for private state, as the
/// line [`private_dir`] prints: the path, the cause and the fix.
#[cfg(windows)]
fn folder_refusal_windows(
    path: &Path,
    distrust: &crate::credentials::windows_security::MarkerDistrust,
) -> String {
    use crate::credentials::windows_security::MarkerDistrust;
    let shown = path.display();
    let fix = |writer: Option<&str>| {
        format!(
            "Fix: make it private to this account, keeping access for what is in it ({}), then retry.",
            private_folder_command(&shown.to_string(), writer)
        )
    };
    match distrust {
        MarkerDistrust::Owner(owner) => format!(
            "pseudolife-mcp: not using {shown}: it is owned by {owner}, not this account, so private state there could be read or replaced. Fix: move it aside and retry; this client creates it again."
        ),
        MarkerDistrust::Writer { name, sid } => format!(
            "pseudolife-mcp: not using {shown}: {name} can add, delete or replace what is in it, so private state there could be forged. {}",
            fix(Some(sid.as_str()))
        ),
        // icacls cannot name an entry it does not read; /reset replaces the
        // folder's explicit entries, that one included, with inherited ones.
        MarkerDistrust::Unreadable => format!(
            "pseudolife-mcp: not using {shown}: its access list holds an entry this client cannot read (a callback or object entry), which could let another account change what is in it. Fix: drop its own entries and give this account full control of it and what is in it ({}; from an elevated prompt if Administrators owns it), then retry.",
            unreadable_folder_command(&shown.to_string())
        ),
        MarkerDistrust::NoDacl => format!(
            "pseudolife-mcp: not using {shown}: it has no access list, so every account can change what is in it. {}",
            fix(None)
        ),
    }
}
/// One line per folder per process.
#[cfg(windows)]
fn report_folder_once(path: &Path, line: &str) {
    static REPORTED: LazyLock<StdMutex<std::collections::HashSet<PathBuf>>> =
        LazyLock::new(Default::default);
    if REPORTED
        .lock()
        .map(|mut seen| seen.insert(path.to_path_buf()))
        .unwrap_or(false)
    {
        eprintln!("{line}");
    }
}
/// `error` from opening, reading or moving `path`, with its cause when that
/// is an earlier native client's folder protection: on Windows a file this
/// account owns whose access list has no entries (#1538), which only its
/// owner can repair. The client never rewrites access it did not set.
pub fn denied_cause(path: &Path, error: &std::io::Error) -> String {
    #[cfg(windows)]
    if error.kind() == std::io::ErrorKind::PermissionDenied
        && crate::credentials::windows_security::empty_dacl_owned(path)
    {
        // The folder whose protection cut it off: the nearest ancestor with
        // a protected DACL that passes nothing on. Resetting a folder below
        // that one would re-inherit nothing.
        let folder = path
            .ancestors()
            .skip(1)
            .find(|folder| {
                crate::credentials::windows_security::protected_without_inheritance(folder)
            })
            .or(path.parent())
            .unwrap_or(path)
            .display();
        // Git Bash rewrites the /reset and /T switches, so the command is
        // given for PowerShell or cmd.
        return format!(
            "{error}: {} has an empty access list (an earlier native client's folder protection removed the access it inherited); restore it as its owner (from an elevated prompt if Administrators owns it), in PowerShell or cmd, with: icacls \"{folder}\" /reset /T",
            path.display()
        );
    }
    let _ = path;
    error.to_string()
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
        // A folder this client did not create is used when no other account
        // can change what is in it (see private_dir), not only when owner-only.
        if !matches!(
            crate::credentials::windows_security::validate_folder(&directory),
            Ok(None)
        ) {
            return Some(format!(
                "the access list of {} lets other accounts change what is in it, or another account owns it",
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

    thread_local! {
        /// A folder in which another task makes a child the moment
        /// private_dir has created it, before it is protected.
        static CHILD_MEANWHILE: std::cell::RefCell<Option<PathBuf>> =
            const { std::cell::RefCell::new(None) };
    }
    pub(super) fn create_child_meanwhile(path: &Path) {
        if CHILD_MEANWHILE.with(|folder| folder.borrow().as_deref() == Some(path)) {
            // As the handshake cache's `create_dir_all` does.
            fs::create_dir(path.join("handshake-cache")).unwrap();
        }
    }

    #[cfg(windows)]
    #[test]
    fn windows_a_child_made_while_its_folder_is_created_keeps_its_access() {
        // #1538 review finding 4: a folder made plainly and protected after
        // left a child created in between with an empty access list.
        let home = inheriting_folder("child-window");
        let folder = home.0.join("agent-state");
        CHILD_MEANWHILE.with(|meanwhile| *meanwhile.borrow_mut() = Some(folder.clone()));
        let made = private_dir(&folder);
        CHILD_MEANWHILE.with(|meanwhile| *meanwhile.borrow_mut() = None);
        made.unwrap();
        fs::write(folder.join("handshake-cache").join("cache.json"), b"{}")
            .expect("the child made in the creation window lost its access");
        use std::os::windows::fs::OpenOptionsExt;
        let directory = OpenOptions::new()
            .access_mode(0x0002_0000 | 0x80)
            .custom_flags(0x0200_0000 | 0x0020_0000)
            .open(&folder)
            .unwrap();
        crate::credentials::windows_security::validate(&directory).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn windows_a_marker_in_a_folder_others_can_empty_is_refused() {
        // #1538 review finding 3: whatever a marker's own access list says,
        // an account that may delete entries in its folder can suppress a
        // wake; the folder is held to the folder rule when reading too.
        let home = inheriting_folder("marker-folder");
        let digests = home.0.join("digests");
        fs::create_dir(&digests).unwrap();
        let marker = digests.join("k.wake");
        powershell_write(&marker, "w\n");
        assert_eq!(read_marker(&marker, 64).unwrap(), b"w\n");
        // Users may now delete what is in the folder (FILE_DELETE_CHILD),
        // not write the marker itself.
        crate::credentials::windows_security::set_dacl_sddl(
            &digests,
            "D:(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;;0x40;;;BU)",
        )
        .unwrap();
        // Freshly checked: the earlier pass is no longer cached.
        if let Ok(mut trusted) = MARKER_FOLDERS.lock() {
            trusted.clear();
        }
        assert_eq!(read_marker(&marker, 64), Err("invalid_state"));
    }

    #[cfg(windows)]
    #[test]
    fn windows_a_marker_folder_swapped_under_its_name_is_checked_again() {
        // The 30-second pass belongs to the folder it was made on, not to
        // its name.
        let home = inheriting_folder("marker-swap");
        let digests = home.0.join("digests");
        fs::create_dir(&digests).unwrap();
        powershell_write(&digests.join("k.wake"), "w\n");
        assert!(read_marker(&digests.join("k.wake"), 64).is_ok());
        fs::rename(&digests, home.0.join("digests-old")).unwrap();
        fs::create_dir(&digests).unwrap();
        crate::credentials::windows_security::set_dacl_sddl(
            &digests,
            "D:(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;;0x40;;;BU)",
        )
        .unwrap();
        powershell_write(&digests.join("k.wake"), "w\n");
        assert_eq!(
            read_marker(&digests.join("k.wake"), 64),
            Err("invalid_state")
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_ace_at_keeps_to_the_list_without_is_valid_acl() {
        use crate::credentials::windows_security::ace_at_raw;
        // One ACCESS_ALLOWED_ACE (20 bytes) for S-1-1-0 in a 28-byte list.
        let list = |acl_size: u32, ace_size: u32| {
            vec![
                0x0000_0002u32 | (acl_size << 16),
                0x0000_0001,
                ace_size << 16,
                0x001F_01FF,
                0x0000_0101,
                0x0100_0000,
                0x0000_0000,
            ]
        };
        assert_eq!(ace_at_raw(&list(28, 20), 0), Some((0, 20)));
        // An entry shorter than a header.
        assert_eq!(ace_at_raw(&list(28, 0), 0), None);
        assert_eq!(ace_at_raw(&list(28, 2), 0), None);
        // An entry running past the end of the list.
        assert_eq!(ace_at_raw(&list(28, 24), 0), None);
        // A header running past the end of the list.
        assert_eq!(ace_at_raw(&list(10, 20), 0), None);
        // No entry at an index past the count.
        assert_eq!(ace_at_raw(&list(28, 20), 1), None);
    }

    #[cfg(windows)]
    #[test]
    fn windows_an_unreadable_entry_on_a_folder_gets_a_fix_that_works() {
        // Under a folder that passes nothing on, as the shim's own are:
        // `/reset` alone would leave it with an empty access list.
        let root = Dir::new();
        let folder = root.0.join("state$x");
        fs::create_dir(&folder).unwrap();
        // A callback (conditional) allow entry, which the rule does not read.
        crate::credentials::windows_security::set_dacl_sddl(
            &folder,
            "D:(A;OICI;FA;;;OW)(XA;;FA;;;WD;(Member_of {SID(BA)}))",
        )
        .unwrap();
        // A file whose access is only inherited from the folder.
        fs::write(folder.join("kept"), b"x").unwrap();
        assert_eq!(private_dir(&folder), Err("invalid_state"));
        let line = folder_refusal_windows(
            &folder,
            &crate::credentials::windows_security::MarkerDistrust::Unreadable,
        );
        let printed = line
            .split(" (in PowerShell: ")
            .nth(1)
            .and_then(|rest| rest.strip_suffix("), then retry."))
            .unwrap_or_else(|| panic!("{line}"));
        // Each call of the printed line must succeed, not only the last.
        for call in printed.split("; ") {
            let status = std::process::Command::new("powershell")
                .args(["-NoProfile", "-NonInteractive", "-Command"])
                .arg("Invoke-Expression $env:PRINTED_FIX; exit $LASTEXITCODE")
                .env("PRINTED_FIX", call)
                .stdout(std::process::Stdio::null())
                .status()
                .unwrap();
            assert!(status.success(), "{call}");
        }
        private_dir(&folder).unwrap();
        fs::read(folder.join("kept")).unwrap();
        fs::write(folder.join("kept"), b"y").unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn windows_an_emptied_cache_folder_gets_a_fix_for_that_folder_only() {
        // A host an earlier native client hit: the state root was protected
        // with the cache's folder already in it, which emptied that folder.
        let home = inheriting_folder("emptied-cache");
        let root = home.0.join("agent-state");
        fs::create_dir(&root).unwrap();
        let cache = root.join("handshake-cache");
        fs::create_dir(&cache).unwrap();
        {
            use std::os::windows::fs::OpenOptionsExt;
            let directory = OpenOptions::new()
                .access_mode(0x00120089 | 0x000c0000)
                .share_mode(0x1 | 0x2)
                .custom_flags(0x02000000 | 0x00200000)
                .open(&root)
                .unwrap();
            crate::credentials::windows_security::make_private(&directory).unwrap();
        }
        let error = fs::write(cache.join("c.json"), b"{}")
            .expect_err("the emptied folder is still writable");
        let line = crate::cache::store_failure_line(&cache, &error);
        let command = cache_folder_fix(&cache);
        assert!(
            line.contains(&command) && line.contains("empty access list"),
            "{line}"
        );
        // Never a reset of the state root, which would unprotect its files.
        assert!(
            !line.contains("/reset") && !line.contains(&format!("\"{}\"", root.display())),
            "{line}"
        );
        let status = std::process::Command::new("powershell")
            .args(["-NoProfile", "-NonInteractive", "-Command"])
            .arg("Invoke-Expression $env:PRINTED_FIX; exit $LASTEXITCODE")
            .env("PRINTED_FIX", &command)
            .stdout(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(status.success(), "{command}");
        fs::write(cache.join("c.json"), b"{}").unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn windows_the_powershell_form_of_the_fix_works_as_printed() {
        // A folder name PowerShell would expand in double quotes.
        let home = inheriting_folder("ps-fix");
        let folder = home.0.join("state$x");
        fs::create_dir(&folder).unwrap();
        fs::write(folder.join("kept"), b"x").unwrap();
        crate::credentials::windows_security::set_dacl_sddl(
            &folder,
            "D:(A;OICI;FA;;;OW)(A;OICI;FA;;;WD)",
        )
        .unwrap();
        assert_eq!(private_dir(&folder), Err("invalid_state"));
        let printed = private_folder_command(&folder.display().to_string(), Some("S-1-1-0"));
        let command = printed
            .strip_prefix("in PowerShell: ")
            .unwrap_or_else(|| panic!("{printed}"));
        let status = std::process::Command::new("powershell")
            .args(["-NoProfile", "-NonInteractive", "-Command"])
            .arg("Invoke-Expression $env:PRINTED_FIX; exit $LASTEXITCODE")
            .env("PRINTED_FIX", command)
            .stdout(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(status.success(), "{command}");
        private_dir(&folder).unwrap();
        fs::write(folder.join("kept"), b"y").unwrap();
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
        let root = inheriting_folder("race");
        let state_dir = root.0.join(".pseudolife-mcp").join("agent-state");
        let bound = state_dir.join("0c33202fbddfe6ee6eebb386fea0df74");
        // Each level the reservation creates can be raced; on the Windows
        // dev VM (#1324) the cache made `agent-state` meanwhile, with its
        // `handshake-cache` child, under a `.pseudolife-mcp` it had made.
        for raced in [&state_dir, &bound] {
            let _ = fs::remove_dir_all(root.0.join(".pseudolife-mcp"));
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
                // Protected when this call made it; trusted as it is
                // when the other task did.
                assert_eq!(
                    crate::credentials::windows_security::validate_folder(&directory).unwrap(),
                    None
                );
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

    /// A folder whose children inherit their access, as in a user profile.
    /// On Windows it gets the profile's own entries (SYSTEM, Administrators
    /// and this account, inherited by everything below), because the
    /// temporary folder can carry more: on a host with the Codex sandbox,
    /// its sandbox accounts may modify everything under %TEMP%.
    fn inheriting_folder(tag: &str) -> Dir {
        let path = std::env::temp_dir().join(format!(
            "pseudolife-{tag}-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir(&path).unwrap();
        #[cfg(windows)]
        profile_like(&path);
        Dir(path)
    }
    #[cfg(windows)]
    fn profile_like(path: &Path) {
        let me = crate::credentials::windows_security::current_user_name().unwrap();
        let status = std::process::Command::new("icacls")
            .arg(path)
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
    }

    /// The rule named `sid` as the writer, by SID and by the name Windows
    /// gives it here (account names are localised).
    #[cfg(windows)]
    fn expect_writer(
        found: Option<crate::credentials::windows_security::MarkerDistrust>,
        sid: &str,
        case: &str,
    ) {
        use crate::credentials::windows_security::{MarkerDistrust, sid_name_of};
        match found {
            Some(MarkerDistrust::Writer { name, sid: found }) => {
                assert_eq!(found, sid, "{case}");
                assert_eq!(name, sid_name_of(sid), "{case}");
            }
            other => panic!("{case}: {other:?}"),
        }
    }

    #[cfg(windows)]
    #[test]
    fn windows_folder_rule_refuses_what_lets_others_change_its_contents() {
        use crate::credentials::windows_security::{MarkerDistrust, folder_distrust_sddl};
        const ME: &str = "S-1-5-21-100-200-300-1001";
        let rule = |sddl: &str| folder_distrust_sddl(&sddl.replace("ME", ME), Some(ME));
        // A profile folder as Windows makes it, with CREATOR OWNER for new
        // children, and the shim's own protected folder.
        for sddl in [
            "O:MED:AI(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;ME)",
            "O:MED:(A;;FA;;;ME)(A;OICIIO;FA;;;CO)(A;;0x1200a9;;;BU)",
            "O:MED:P(A;;FA;;;OW)",
            "O:BAD:(A;OICI;FA;;;BA)(A;OICI;FA;;;ME)",
        ] {
            assert_eq!(rule(sddl), None, "{sddl}");
        }
        for (sddl, who) in [
            // Inherit-only entries decide what new files there grant.
            ("O:MED:(A;;FA;;;ME)(A;OICIIO;FA;;;WD)", "S-1-1-0"),
            // Deleting a child replaces it as surely as writing it.
            ("O:MED:(A;;FA;;;ME)(A;;0x40;;;BU)", "S-1-5-32-545"),
            ("O:MED:(A;;FA;;;ME)(A;OICI;0x1301bf;;;AU)", "S-1-5-11"),
        ] {
            expect_writer(rule(sddl), who, sddl);
        }
        assert!(matches!(
            rule("O:S-1-5-32-547D:(A;;FA;;;ME)"),
            Some(MarkerDistrust::Owner(_))
        ));
        assert_eq!(
            rule("O:MED:NO_ACCESS_CONTROL"),
            Some(MarkerDistrust::NoDacl)
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_a_filled_folder_others_can_change_is_refused_and_left_as_it_was() {
        let home = inheriting_folder("exposed");
        let folder = home.0.join("state");
        fs::create_dir(&folder).unwrap();
        fs::write(folder.join("kept"), b"x").unwrap();
        crate::credentials::windows_security::set_dacl_sddl(
            &folder,
            "D:(A;OICI;FA;;;OW)(A;OICI;FA;;;WD)",
        )
        .unwrap();
        assert_eq!(private_dir(&folder), Err("invalid_state"));
        // Refused, not rewritten: what is in it is still usable.
        fs::read(folder.join("kept")).unwrap();
        let everyone = crate::credentials::windows_security::sid_name_of("S-1-1-0");
        let line = folder_refusal_windows(
            &folder,
            &crate::credentials::windows_security::MarkerDistrust::Writer {
                name: everyone.clone(),
                sid: "S-1-1-0".into(),
            },
        );
        assert!(line.contains(&folder.display().to_string()), "{line}");
        assert!(
            line.contains(&format!(
                "{everyone} can add, delete or replace what is in it"
            )),
            "{line}"
        );
        assert!(
            line.contains("/inheritance:r /grant:r") && line.contains("(OI)(CI)F"),
            "{line}"
        );
        // The printed fix, run as printed, makes the folder acceptable and
        // keeps what is in it usable.
        let command = private_folder_command(&folder.display().to_string(), Some("S-1-1-0"));
        assert!(line.contains(&command), "{line}");
        assert!(command.contains("/remove:g \"*S-1-1-0\""), "{command}");
        // Run in PowerShell exactly as printed; the text travels in the
        // environment so no quoting layer sits in between.
        let status = std::process::Command::new("powershell")
            .args(["-NoProfile", "-NonInteractive", "-Command"])
            .arg("Invoke-Expression $env:PRINTED_FIX; exit $LASTEXITCODE")
            .env("PRINTED_FIX", &command)
            .stdout(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(status.success(), "{command}");
        private_dir(&folder).unwrap();
        fs::read(folder.join("kept")).unwrap();
        fs::write(folder.join("kept"), b"y").unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn windows_a_state_file_is_owner_only_from_its_creation() {
        // In a folder used as it is (not re-protected) that lets everyone
        // read what is created in it, a new state file must not inherit
        // that access even before make_private: a handle opened then keeps
        // it.
        let home = inheriting_folder("born-private");
        let folder = home.0.join("state");
        fs::create_dir(&folder).unwrap();
        crate::credentials::windows_security::set_dacl_sddl(
            &folder,
            "D:(A;OICI;FA;;;OW)(A;OICI;FR;;;WD)",
        )
        .unwrap();
        let born = crate::credentials::windows_security::create_owner_only(&folder.join("k.json"))
            .unwrap();
        assert_eq!(
            crate::credentials::windows_security::dacl_sddl(&born),
            "D:P(A;;FA;;;OW)"
        );
        // And the full create still ends owner-only, owned by this account.
        drop(born);
        let created = create(&folder.join("k2.json")).unwrap();
        crate::credentials::windows_security::validate(&created.file).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn windows_an_empty_existing_folder_others_could_change_is_made_private() {
        let home = inheriting_folder("empty");
        let folder = home.0.join("state");
        fs::create_dir(&folder).unwrap();
        crate::credentials::windows_security::set_dacl_sddl(
            &folder,
            "D:(A;OICI;FA;;;OW)(A;OICI;FA;;;WD)",
        )
        .unwrap();
        private_dir(&folder).unwrap();
        use std::os::windows::fs::OpenOptionsExt;
        let directory = OpenOptions::new()
            .access_mode(0x0002_0000 | 0x80)
            .custom_flags(0x0200_0000 | 0x0020_0000)
            .open(&folder)
            .unwrap();
        crate::credentials::windows_security::validate(&directory).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn windows_hook_markers_survive_their_folder_being_taken_for_digests() {
        // The digest folder the hooks made and wrote markers into before
        // the shim's first digest write (finding 10 of the #1536 review).
        let home = inheriting_folder("digests");
        let digests = home.0.join("digests");
        fs::create_dir(&digests).unwrap();
        let marker = digests.join("k.seen");
        powershell_write(
            &marker, "7
",
        );
        private_dir(&digests).unwrap();
        assert_eq!(
            read_marker(&marker, 64).unwrap(),
            b"7
"
        );
        atomic_write(
            &digests.join("k.txt"),
            b"7
",
            None,
        )
        .unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn windows_a_file_an_earlier_protection_emptied_names_its_repair() {
        // A machine an earlier native client already hit: the folder was
        // protected with the file in it, which emptied the file's DACL.
        let home = inheriting_folder("emptied");
        let folder = home.0.join("runtimes");
        fs::create_dir(&folder).unwrap();
        let file = folder.join("runtime.json");
        fs::write(&file, b"{}").unwrap();
        // A Python venv's file one level down, as 000001\pyvenv.cfg is.
        let nested = folder.join("000001").join("pyvenv.cfg");
        fs::create_dir(nested.parent().unwrap()).unwrap();
        fs::write(&nested, b"home").unwrap();
        {
            use std::os::windows::fs::OpenOptionsExt;
            let directory = OpenOptions::new()
                .access_mode(0x00120089 | 0x000c0000)
                .share_mode(0x1 | 0x2)
                .custom_flags(0x02000000 | 0x00200000)
                .open(&folder)
                .unwrap();
            crate::credentials::windows_security::make_private(&directory).unwrap();
        }
        let error = fs::read(&file).expect_err("the emptied file is still readable");
        let line = denied_cause(&file, &error);
        assert!(line.contains("has an empty access list"), "{line}");
        assert!(
            line.contains(&format!("icacls \"{}\" /reset /T", folder.display())),
            "{line}"
        );
        // The command is given for PowerShell or cmd (Git Bash rewrites /reset).
        assert!(line.contains("in PowerShell or cmd"), "{line}");
        // For a file further down, it names the folder that was protected,
        // not the file's own: resetting that one would re-inherit nothing.
        let error = fs::read(&nested).expect_err("the emptied file is still readable");
        let line = denied_cause(&nested, &error);
        assert!(
            line.contains(&format!("icacls \"{}\" /reset /T", folder.display())),
            "{line}"
        );
        // The repair it names works, run as the owner.
        let status = std::process::Command::new("icacls")
            .arg(&folder)
            .args(["/reset", "/T", "/Q"])
            .status()
            .unwrap();
        assert!(status.success());
        fs::read(&file).unwrap();
        fs::read(&nested).unwrap();
        // An ordinary refusal is passed through unchanged.
        let other = std::io::Error::from(std::io::ErrorKind::PermissionDenied);
        assert_eq!(
            denied_cause(&home.0.join("absent"), &other),
            other.to_string()
        );
    }

    #[test]
    fn a_folder_another_client_filled_keeps_its_files_usable() {
        // The Python client's runtimes root: folders and files it wrote with
        // inherited access only, which this client then reuses.
        let home = inheriting_folder("filled");
        let root = home.0.join("runtimes");
        let scripts = root.join("000001").join("Scripts");
        fs::create_dir_all(&scripts).unwrap();
        let files = [
            root.join("000001").join("runtime.json"),
            scripts.join("pseudolife-mcp.exe"),
        ];
        for file in &files {
            fs::write(file, b"python client").unwrap();
        }
        private_dir(&root).unwrap();
        for file in &files {
            fs::read(file).unwrap_or_else(|error| panic!("{}: {error}", file.display()));
        }
        fs::write(root.join("000001").join("runtime.json"), b"rewritten").unwrap();
        fs::write(scripts.join("added"), b"x").unwrap();
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
                reason.contains("lets other accounts change what is in it")
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
            ("O:MED:(A;;FA;;;ME)(A;;FW;;;WD)", "S-1-1-0"),
            ("O:MED:(A;;FA;;;ME)(A;;0x1301bf;;;AU)", "S-1-5-11"),
            ("O:MED:(A;;FA;;;ME)(A;;FA;;;BU)", "S-1-5-32-545"),
            ("O:MED:(A;;FA;;;ME)(A;;0x4;;;OTHER)", OTHER),
            ("O:MED:(A;;FA;;;ME)(A;;WD;;;OTHER)", OTHER),
            ("O:MED:(A;;FA;;;ME)(A;;GW;;;OTHER)", OTHER),
            // FILE_WRITE_EA, part of FILE_GENERIC_WRITE.
            ("O:MED:(A;;FA;;;ME)(A;;0x10;;;BU)", "S-1-5-32-545"),
            // Deleting or backdating a marker suppresses a wake too.
            ("O:MED:(A;;FA;;;ME)(A;;SD;;;BU)", "S-1-5-32-545"),
            ("O:MED:(A;;FA;;;ME)(A;;0x100;;;BU)", "S-1-5-32-545"),
        ] {
            expect_writer(rule(sddl), who, sddl);
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
        expect_writer(
            marker_distrust_raw(&mut well_formed()),
            "S-1-1-0",
            "well-formed list",
        );
        let unreadable = Some(MarkerDistrust::Unreadable);
        // More entries counted than the list holds.
        let mut counted = well_formed();
        counted[1] = 0x0000_0002;
        assert_eq!(marker_distrust_raw(&mut counted), unreadable);
        // An entry shorter than its own header.
        let mut short = well_formed();
        short[2] = 0x0002_0000;
        assert_eq!(marker_distrust_raw(&mut short), unreadable);
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
        let everyone = crate::credentials::windows_security::sid_name_of("S-1-1-0");
        let writer = MarkerDistrust::Writer {
            name: everyone.clone(),
            sid: "S-1-1-0".into(),
        };
        let line = marker_refusal_windows(path, &writer);
        assert!(line.contains(&path.display().to_string()), "{line}");
        assert!(
            line.contains(&format!("{everyone} can change or delete it")),
            "{line}"
        );
        assert!(
            line.contains(r#"icacls "C:\Users\<user>\.pseudolife-mcp\digests" /inheritance:r"#),
            "{line}"
        );
        // Accounts are named by SID: no shell variable, no localised or
        // special-character name to quote.
        let me = crate::credentials::windows_security::current_user_sid().unwrap();
        assert!(
            line.contains(&format!(
                "/grant:r \"*{me}:(OI)(CI)F\" /remove:g \"*S-1-1-0\""
            )),
            "{line}"
        );
        assert!(!line.contains('%') && !line.contains("del \""), "{line}");
        // An entry the rule cannot read gets no command that cannot remove it.
        let line = marker_refusal_windows(path, &MarkerDistrust::Unreadable);
        assert!(
            line.contains("cannot read")
                && line.contains("Fix: delete that file")
                && !line.contains("icacls"),
            "{line}"
        );
        let line = folder_refusal_windows(Path::new(r"C:\d"), &MarkerDistrust::Unreadable);
        assert!(
            line.contains(
                r"in PowerShell: icacls 'C:\d' /reset; icacls 'C:\d' /inheritance:r /grant:r '*"
            ),
            "{line}"
        );
        // A path a shell would expand gets the PowerShell form, single-quoted.
        let plain = private_folder_command(r"D:\data\a b\digests", None);
        assert!(
            plain.starts_with(r#"icacls "D:\data\a b\digests" "#),
            "{plain}"
        );
        let dollar = private_folder_command(r"D:\data\x$y\digests", Some("S-1-1-0"));
        assert!(
            dollar.starts_with(
                r"in PowerShell: icacls 'D:\data\x$y\digests' /inheritance:r /grant:r '*"
            ) && dollar.ends_with("/remove:g '*S-1-1-0'"),
            "{dollar}"
        );
        // A drive root's trailing backslash cannot escape the closing quote.
        let drive = private_folder_command(r"D:\", None);
        assert!(drive.starts_with(r#"icacls "D:\." "#), "{drive}");
        // PowerShell treats curly quotes as quotes too.
        let curly = private_folder_command("D:\\data\\o\u{2019}n\\d", None);
        assert!(curly.starts_with("in PowerShell: "), "{curly}");
        let quoted = private_folder_command(r"D:\data\o'n\digests", None);
        assert!(quoted.contains(r"'D:\data\o''n\digests'"), "{quoted}");
        for percent in [r"D:\data\a%b%\d", r"D:\data\a!b\d"] {
            assert!(
                private_folder_command(percent, None).starts_with("in PowerShell: "),
                "{percent}"
            );
        }
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
            writer,
            MarkerDistrust::Owner("other".into()),
            MarkerDistrust::Unreadable,
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
