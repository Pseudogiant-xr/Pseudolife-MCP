//! Native wait-mail over the coordination adapter's LF-framed records.
//! These markers have no lock or transaction in the oracle; preserve its races.
#[path = "wait_mail_args.rs"]
mod args;
use std::{
    cmp::Ordering,
    ffi::OsString,
    fs::{File, Metadata, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering as AtomicOrdering},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[cfg(not(test))]
use std::fs;
#[cfg(test)]
mod fs {
    pub use std::fs::*;
    use std::{
        cell::RefCell,
        io,
        path::{Path, PathBuf},
    };
    std::thread_local! {
        static DENY: RefCell<Option<(PathBuf, usize, usize)>> = const { RefCell::new(None) };
    }
    pub fn deny_once(path: &Path) {
        DENY.with(|slot| *slot.borrow_mut() = Some((path.to_path_buf(), 1, 0)));
    }
    pub fn clear_denial() -> usize {
        DENY.with(|slot| slot.borrow_mut().take().map_or(0, |(_, _, calls)| calls))
    }
    pub fn metadata(path: impl AsRef<Path>) -> io::Result<std::fs::Metadata> {
        let denied = DENY.with(|slot| {
            let mut slot = slot.borrow_mut();
            let Some((target, left, calls)) = slot.as_mut() else {
                return false;
            };
            if target.as_path() != path.as_ref() {
                return false;
            }
            *calls += 1;
            let deny = *left > 0;
            *left = left.saturating_sub(1);
            deny
        });
        if denied {
            Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "test metadata denial",
            ))
        } else {
            std::fs::metadata(path)
        }
    }
}

const HELP: &str = include_str!("wait_mail_help.txt");
static INTERRUPTED: AtomicBool = AtomicBool::new(false);
const INTERRUPT_SLICE: Duration = Duration::from_millis(20);

#[derive(Clone, Debug, Eq, PartialEq)]
struct Integer(String);
impl Integer {
    fn parse(raw: &[u8]) -> Option<Self> {
        if raw.is_empty() || !raw.iter().all(u8::is_ascii_digit) {
            return None;
        }
        let raw = &raw[raw
            .iter()
            .position(|byte| *byte != b'0')
            .unwrap_or(raw.len() - 1)..];
        Some(Self(String::from_utf8(raw.to_vec()).unwrap()))
    }
    fn zero() -> Self {
        Self("0".into())
    }
    fn text(&self) -> &str {
        &self.0
    }
}
impl Ord for Integer {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0
            .len()
            .cmp(&other.0.len())
            .then_with(|| self.0.cmp(&other.0))
    }
}
impl PartialOrd for Integer {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

#[cfg(unix)]
struct DescriptorWriter<'a>(rustix::fd::BorrowedFd<'a>);
#[cfg(unix)]
impl Write for DescriptorWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        rustix::io::write(self.0, bytes).map_err(Into::into)
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
fn stderr(text: &str) -> io::Result<()> {
    #[cfg(unix)]
    let owner = io::stderr();
    #[cfg(unix)]
    let mut stream = {
        use std::os::fd::AsFd;
        DescriptorWriter(owner.as_fd())
    };
    #[cfg(windows)]
    let mut stream = io::stderr().lock();
    stream
        .write_all(&super::text_bytes(text))
        .and_then(|_| stream.flush())
}
fn write_output(writer: &mut impl Write, raw: &[u8]) -> io::Result<()> {
    writer.write_all(raw).and_then(|_| writer.flush())
}
fn write_then_mark<W: Write, T>(
    mut writer: W,
    raw: &[u8],
    mark: impl FnOnce() -> T,
) -> Result<T, (io::Error, W)> {
    if let Err(error) = write_output(&mut writer, raw) {
        // Keep the failed stdout lock alive through the caller's diagnostic.
        return Err((error, writer));
    }
    drop(writer);
    Ok(mark())
}
fn corrupt_record(kind: &str) -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        format!(
            "corrupt coordination {kind} record; expected canonical unsigned ASCII decimal and LF framing"
        ),
    )
}
fn key_name(bytes: &[u8]) -> bool {
    bytes.len() == 64
        && bytes
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
}
fn digest_path(session: &OsString) -> Option<PathBuf> {
    if session.is_empty() {
        return None;
    }
    let root = std::env::var_os("PSEUDOLIFE_DIGEST_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let variable = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
            std::env::var_os(variable)
                .map(PathBuf::from)
                .or_else(dirs::home_dir)
                .unwrap_or_default()
                .join(".pseudolife-mcp")
                .join("digests")
        });
    let root = args::pathlib(&root);
    let root = crate::credentials::expand_user(&root).ok()?;
    let key = crate::board::identity::hex_hash(session.to_str()?.as_bytes());
    let direct = root.join(format!("{key}.txt"));
    let pid = std::env::var("CLAUDE_PID").unwrap_or_default();
    if pid.is_empty()
        || !pid.bytes().all(|b| b.is_ascii_digit())
        || std::env::var_os("CLAUDE_CODE_SESSION_ID").as_ref() != Some(session)
    {
        return Some(direct);
    }
    let record = root.join(format!("claude-{pid}.host"));
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let Ok(file) = options.open(&record) else {
        return Some(direct);
    };
    if fs::symlink_metadata(&record).is_ok_and(|m| m.file_type().is_symlink())
        || !file.metadata().is_ok_and(|m| m.is_file())
    {
        return Some(direct);
    }
    let mut raw = Vec::new();
    if file.take(256).read_to_end(&mut raw).is_err() {
        return Some(direct);
    }
    let mut lines = raw.split(|b| *b == b'\n');
    if let (Some(mapped), Some(confirmed)) = (lines.next(), lines.next())
        && key_name(mapped)
        && confirmed == key.as_bytes()
    {
        return Some(root.join(format!("{}.txt", String::from_utf8_lossy(mapped))));
    }
    Some(direct)
}

fn read_digest(path: &Path) -> io::Result<(Integer, Vec<u8>)> {
    let raw = interruptible_read(path)?;
    let split = raw
        .iter()
        .position(|b| *b == b'\n')
        .ok_or_else(|| corrupt_record("digest"))?;
    let watermark = Integer::parse(&raw[..split]).ok_or_else(|| corrupt_record("digest"))?;
    let body = raw[split + 1..].to_vec();
    if !body.is_empty() && !body.ends_with(b"\n") {
        return Err(corrupt_record("digest"));
    }
    Ok((watermark, body))
}
#[cfg(unix)]
fn interruptible_read(path: &Path) -> io::Result<Vec<u8>> {
    interruptible_read_with(path, || INTERRUPTED.load(AtomicOrdering::Relaxed))
}
#[cfg(unix)]
fn interruptible_read_with(
    path: &Path,
    mut interrupted: impl FnMut() -> bool,
) -> io::Result<Vec<u8>> {
    use rustix::{
        event::{PollFd, PollFlags, Timespec, poll},
        fs::{Mode, OFlags, open},
        io::{Errno, read},
    };
    let file = loop {
        if interrupted() {
            return Err(Errno::INTR.into());
        }
        // SIGINT can arrive after a flag check but before open/read starts.
        // Nonblocking IO closes that lost-interrupt window for replaced FIFOs.
        match open(
            path,
            OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NONBLOCK,
            Mode::empty(),
        ) {
            Err(Errno::INTR) if !interrupted() => continue,
            result => break result.map_err(io::Error::from)?,
        }
    };
    let mut result = Vec::new();
    let mut buffer = [0u8; 8192];
    // Reuse the wait loop's interruptible-sleep cadence, including when a
    // signal has already run its handler before readiness polling starts.
    let timeout = Timespec::try_from(INTERRUPT_SLICE).unwrap();
    loop {
        if interrupted() {
            return Err(Errno::INTR.into());
        }
        let mut ready = [PollFd::new(&file, PollFlags::IN)];
        match poll(&mut ready, Some(&timeout)) {
            Ok(0) => continue,
            Ok(_) => (),
            Err(Errno::INTR) if !interrupted() => continue,
            Err(error) => return Err(error.into()),
        }
        match read(&file, &mut buffer) {
            Ok(0) => return Ok(result),
            Ok(size) => result.extend_from_slice(&buffer[..size]),
            Err(Errno::AGAIN) => (),
            Err(Errno::INTR) if !interrupted() => (),
            Err(error) => return Err(error.into()),
        }
    }
}
#[cfg(windows)]
fn interruptible_read(path: &Path) -> io::Result<Vec<u8>> {
    fs::read(path)
}
fn read_ring(path: &Path) -> io::Result<Option<(Integer, String)>> {
    let raw = match fs::symlink_metadata(path) {
        Ok(info) if info.is_file() => match interruptible_read(path) {
            Ok(raw) => raw,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        },
        Ok(_) => return Ok(None),
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e),
    };
    // Match wait_mail_cli._read_ring and the Stop hook's ring_past_seen:
    // only the first two lines matter; malformed markers mean no ring.
    let mut lines = raw.split(|byte| *byte == b'\n');
    let head: Vec<u8> = lines
        .next()
        .unwrap_or_default()
        .iter()
        .copied()
        .filter(|byte| !matches!(byte, b'\r' | b' '))
        .collect();
    if head.len() > 12 {
        return Ok(None);
    }
    let Some(watermark) = Integer::parse(&head) else {
        return Ok(None);
    };
    let reason = lines.next().unwrap_or_default();
    let reason = reason.strip_suffix(b"\r").unwrap_or(reason);
    if !reason.starts_with(b"rung ")
        || !reason[5..]
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || b"-_ ".contains(b))
    {
        return Ok(None);
    }
    Ok(Some((
        watermark,
        String::from_utf8(reason.to_vec()).unwrap(),
    )))
}
fn read_seen(path: &Path) -> io::Result<Integer> {
    match interruptible_read(path) {
        // An absent/unreadable marker has never established shown mail; in
        // particular a directory still reaches the existing rename diagnostic.
        Err(_) => Ok(Integer::zero()),
        Ok(raw) => {
            // Python bytes.strip() also includes vertical tab (unlike Rust's trim_ascii).
            let start = raw
                .iter()
                .position(|byte| !byte_space(*byte))
                .unwrap_or(raw.len());
            let end = raw
                .iter()
                .rposition(|byte| !byte_space(*byte))
                .map_or(start, |index| index + 1);
            let raw = &raw[start..end];
            if raw.is_empty() {
                Ok(Integer::zero())
            } else {
                Integer::parse(raw).ok_or_else(|| corrupt_record("seen"))
            }
        }
    }
}

fn temporary_suffix() -> String {
    const ALPHABET: &[u8] = b"abcdefghijklmnopqrstuvwxyz0123456789_";
    let mut suffix = String::with_capacity(8);
    while suffix.len() < 8 {
        for (i, byte) in uuid::Uuid::new_v4().as_bytes().iter().copied().enumerate() {
            // UUID version/variant bits are fixed; rejection avoids modulo bias.
            if i != 6 && i != 8 && byte < 222 {
                suffix.push(char::from(ALPHABET[usize::from(byte) % ALPHABET.len()]));
                if suffix.len() == 8 {
                    break;
                }
            }
        }
    }
    suffix
}

fn temporary(path: &Path, private: bool) -> io::Result<(PathBuf, File)> {
    temporary_with(path, private, temporary_suffix)
}

fn temporary_with(
    path: &Path,
    private: bool,
    mut suffix: impl FnMut() -> String,
) -> io::Result<(PathBuf, File)> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    #[cfg(windows)]
    if private {
        use std::os::windows::fs::OpenOptionsExt;
        options.access_mode(0x0012019f | 0x000c0000);
    }
    // Native collision budget; preserve exclusive creation and owned cleanup.
    let attempts = 16;
    for _ in 0..attempts {
        let name = format!(
            ".tmp-{}.{}",
            suffix(),
            path.extension().unwrap_or_default().to_string_lossy()
        );
        let temp = path.with_file_name(name);
        let file = match options.open(&temp) {
            Ok(file) => file,
            Err(error)
                if error.kind() == io::ErrorKind::AlreadyExists
                    || cfg!(windows)
                        && error.kind() == io::ErrorKind::PermissionDenied
                        && temp.is_dir() =>
            {
                continue;
            }
            Err(error) => return Err(error),
        };
        #[cfg(unix)]
        if private {
            use std::os::unix::fs::PermissionsExt;
            if let Err(error) = file.set_permissions(fs::Permissions::from_mode(0o600)) {
                drop(file);
                let _ = fs::remove_file(&temp);
                return Err(error);
            }
        }
        #[cfg(windows)]
        if private && crate::credentials::windows_security::make_private(&file).is_err() {
            drop(file);
            let _ = fs::remove_file(&temp);
            return Err(io::Error::other("cannot secure the private state file"));
        }
        #[cfg(not(windows))]
        let _ = private;
        return Ok((temp, file));
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "temporary file creation exhausted after 16 collisions",
    ))
}

fn os_points(session: &OsString) -> Vec<u32> {
    if let Some(text) = session.to_str() {
        return text.chars().map(u32::from).collect();
    }
    let mut points = Vec::<u32>::new();
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let mut bytes = session.as_bytes();
        while !bytes.is_empty() {
            match std::str::from_utf8(bytes) {
                Ok(text) => {
                    points.extend(text.chars().map(u32::from));
                    break;
                }
                Err(error) => {
                    let valid = error.valid_up_to();
                    points.extend(
                        std::str::from_utf8(&bytes[..valid])
                            .unwrap()
                            .chars()
                            .map(u32::from),
                    );
                    let invalid = error.error_len().unwrap_or(bytes.len() - valid);
                    points.extend(
                        bytes[valid..valid + invalid]
                            .iter()
                            .map(|byte| 0xdc00 + u32::from(*byte)),
                    );
                    bytes = &bytes[valid + invalid..];
                }
            }
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        points.extend(char::decode_utf16(session.encode_wide()).map(|point| {
            point.map_or_else(|error| u32::from(error.unpaired_surrogate()), u32::from)
        }));
    }
    points
}
fn os_display(value: &OsString) -> String {
    os_points(value)
        .into_iter()
        .map(|point| {
            char::from_u32(point).map_or_else(
                || format!("\\u{point:04x}"),
                |character| character.to_string(),
            )
        })
        .collect()
}
fn os_repr(value: &OsString) -> String {
    let points = os_points(value);
    let quote = if points.contains(&39) && !points.contains(&34) {
        '"'
    } else {
        '\''
    };
    let mut result = String::from(quote);
    for point in points {
        if let Some(character) = char::from_u32(point) {
            if character == quote {
                result.push('\\');
                result.push(character);
            } else {
                let repr = super::mode_repr(&character.to_string());
                result.push_str(&repr[1..repr.len() - 1]);
            }
        } else {
            result.push_str(&format!("\\u{point:04x}"));
        }
    }
    result.push(quote);
    result
}
fn session_encoding_error(session: &OsString) -> Option<String> {
    session.to_str().is_none().then_some(())?;
    let points = os_points(session);
    let start = points
        .iter()
        .position(|point| (0xd800..=0xdfff).contains(point))?;
    let length = points[start..]
        .iter()
        .take_while(|point| (0xd800..=0xdfff).contains(*point))
        .count();
    let detail = if length == 1 {
        format!("character '\\u{:04x}' in position {start}", points[start])
    } else {
        format!("characters in position {start}-{}", start + length - 1)
    };
    Some(format!(
        "UnicodeEncodeError: 'utf-8' codec can't encode {detail}: surrogates not allowed\n"
    ))
}
fn atomic_write(path: &Path, raw: &[u8], private: bool) -> io::Result<()> {
    let (temp, mut file) = temporary(path, private)?;
    let result = file.write_all(raw);
    drop(file);
    let result = result.and_then(|_| fs::rename(&temp, path));
    let _ = fs::remove_file(temp);
    result
}
#[derive(Debug)]
struct MarkerError {
    error: io::Error,
    from: Option<PathBuf>,
    to: Option<PathBuf>,
}
fn write_seen(path: &Path, watermark: &Integer) -> Result<(), MarkerError> {
    let (temp, mut file) = temporary(path, false).map_err(|error| MarkerError {
        error,
        from: None,
        to: None,
    })?;
    let write = file.write_all(format!("{}\n", watermark.text()).as_bytes());
    drop(file);
    let result = match write {
        Err(error) => Err(MarkerError {
            error,
            from: None,
            to: None,
        }),
        Ok(()) => fs::rename(&temp, path).map_err(|error| MarkerError {
            error,
            from: Some(temp.clone()),
            to: Some(path.to_path_buf()),
        }),
    };
    let _ = fs::remove_file(&temp);
    result
}
fn mark_seen(path: &Path, watermark: &Integer) -> Result<(), MarkerError> {
    for attempt in 0..3 {
        let seen = read_seen(path).map_err(|error| MarkerError {
            error,
            from: None,
            to: None,
        })?;
        if seen >= *watermark {
            return Ok(());
        }
        match write_seen(path, watermark) {
            Ok(()) => return Ok(()),
            Err(error) if attempt == 2 => return Err(error),
            Err(_) => std::thread::sleep(Duration::from_millis(50)),
        }
    }
    unreachable!()
}
fn wall_time() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |t| t.as_secs_f64())
}
struct Listener {
    path: PathBuf,
    token: String,
    deadline: Instant,
}
impl Listener {
    fn new(digest: &Path, timeout: f64) -> Self {
        let token = uuid::Uuid::new_v4().simple().to_string();
        Self {
            path: digest.with_file_name(format!(
                "{}.{}.wait-armed",
                digest.file_stem().unwrap_or_default().to_string_lossy(),
                token
            )),
            token,
            deadline: Instant::now() + Duration::from_secs_f64(timeout),
        }
    }
    fn renew(&self) {
        let expiry = wall_time()
            + self
                .deadline
                .saturating_duration_since(Instant::now())
                .as_secs_f64()
                .min(60.0);
        let _ = atomic_write(
            &self.path,
            format!("{}\n{expiry:?}\n", self.token).as_bytes(),
            true,
        );
    }
}
impl Drop for Listener {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

#[derive(Eq, PartialEq)]
struct Signature {
    inode: u128,
    modified: u128,
    length: u64,
}
fn signature(path: &Path, info: &Metadata, follow: bool) -> io::Result<Signature> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let _ = (path, follow);
        Ok(Signature {
            inode: info.ino() as u128,
            modified: (info.mtime() as u128)
                .wrapping_mul(1_000_000_000)
                .wrapping_add(info.mtime_nsec() as u128),
            length: info.len(),
        })
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
        let file = OpenOptions::new()
            .access_mode(0)
            .share_mode(7)
            .custom_flags(0x02000000 | if follow { 0 } else { 0x00200000 })
            .open(path)?;
        Ok(Signature {
            inode: windows_inode(&file)?,
            modified: info.last_write_time() as u128,
            length: info.len(),
        })
    }
}
#[cfg(windows)]
#[allow(unsafe_code)]
fn windows_inode(file: &File) -> io::Result<u128> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, GetFileInformationByHandle,
    };
    // SAFETY: file owns the handle and info is initialized, correctly sized storage.
    unsafe {
        let mut info: BY_HANDLE_FILE_INFORMATION = std::mem::zeroed();
        if GetFileInformationByHandle(file.as_raw_handle() as _, &mut info) == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(((info.nFileIndexHigh as u128) << 32) | info.nFileIndexLow as u128)
    }
}
enum WaitResult {
    Mail(Integer, Vec<u8>, String),
    Timeout,
    Gone,
    Interrupted,
    Corrupt(String),
}
fn byte_space(byte: u8) -> bool {
    byte.is_ascii_whitespace() || byte == 11
}
fn wait(digest: &Path, timeout: f64, interval: f64) -> WaitResult {
    let deadline = Instant::now() + Duration::from_secs_f64(timeout);
    let listener = Listener::new(digest, timeout);
    let _interrupt = InterruptGuard::new();
    let ring = digest.with_extension("ring");
    let seen = digest.with_extension("seen");
    let mut last = None;
    loop {
        if INTERRUPTED.load(AtomicOrdering::Relaxed) {
            return WaitResult::Interrupted;
        }
        listener.renew();
        match fs::metadata(digest) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => return WaitResult::Gone,
            Err(_) => (),
            Ok(info) => {
                let sig = signature(digest, &info, true);
                let ring_sig = match fs::symlink_metadata(&ring) {
                    Ok(info) => signature(&ring, &info, false).map(Some),
                    Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
                    Err(e) => Err(e),
                };
                let sig = sig.and_then(|digest| ring_sig.map(|ring| (digest, ring)));
                if sig.as_ref().ok() != last.as_ref() || sig.is_err() {
                    last = sig.ok();
                    match read_digest(digest).and_then(|(watermark, body)| {
                        read_ring(&ring).map(|ring| (watermark, body, ring))
                    }) {
                        Err(error) if error.kind() == io::ErrorKind::NotFound => {
                            return WaitResult::Gone;
                        }
                        Err(error) if error.kind() == io::ErrorKind::InvalidData => {
                            return WaitResult::Corrupt(error.to_string());
                        }
                        Err(_) => last = None,
                        Ok((watermark, body, Some((ring_watermark, reason))))
                            if body.iter().any(|b| !byte_space(*b)) =>
                        {
                            let shown = match read_seen(&seen) {
                                Ok(value) => value,
                                Err(error) => return WaitResult::Corrupt(error.to_string()),
                            };
                            if INTERRUPTED.load(AtomicOrdering::Relaxed) {
                                return WaitResult::Interrupted;
                            }
                            if watermark > shown && ring_watermark > shown {
                                return WaitResult::Mail(watermark, body, reason);
                            }
                        }
                        Ok(_) => (),
                    }
                }
            }
        }
        if INTERRUPTED.load(AtomicOrdering::Relaxed) {
            return WaitResult::Interrupted;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return WaitResult::Timeout;
        }
        // Interruptible sleep preserves the poll interval without renewing early.
        let sleep_deadline = Instant::now() + remaining.min(Duration::from_secs_f64(interval));
        while Instant::now() < sleep_deadline {
            if INTERRUPTED.load(AtomicOrdering::Relaxed) {
                return WaitResult::Interrupted;
            }
            std::thread::sleep(
                sleep_deadline
                    .saturating_duration_since(Instant::now())
                    .min(INTERRUPT_SLICE),
            );
        }
    }
}

#[cfg(unix)]
extern "C" fn interrupt_handler(_: libc::c_int) {
    INTERRUPTED.store(true, AtomicOrdering::Relaxed);
}
#[cfg(windows)]
extern "system" fn interrupt_handler(kind: u32) -> i32 {
    if kind == 0 {
        INTERRUPTED.store(true, AtomicOrdering::Relaxed);
        1
    } else {
        0
    }
}
struct InterruptGuard {
    #[cfg(unix)]
    previous: Option<libc::sigaction>,
}
impl InterruptGuard {
    #[allow(unsafe_code)]
    fn new() -> Self {
        INTERRUPTED.store(false, AtomicOrdering::Relaxed);
        #[cfg(unix)]
        {
            // SAFETY: initialized sigaction values, static atomic-only handler,
            // and restoration of this process's prior disposition. No SA_RESTART:
            // readiness polling returns EINTR promptly for owned cleanup.
            let previous = unsafe {
                let mut action: libc::sigaction = std::mem::zeroed();
                let mut previous: libc::sigaction = std::mem::zeroed();
                action.sa_sigaction = interrupt_handler as *const () as libc::sighandler_t;
                libc::sigemptyset(&mut action.sa_mask);
                (libc::sigaction(libc::SIGINT, &action, &mut previous) == 0).then_some(previous)
            };
            Self { previous }
        }
        #[cfg(windows)]
        {
            #[link(name = "Kernel32")]
            unsafe extern "system" {
                fn SetConsoleCtrlHandler(
                    handler: Option<extern "system" fn(u32) -> i32>,
                    add: i32,
                ) -> i32;
            }
            // SAFETY: callback has the Windows ABI and static lifetime.
            unsafe {
                SetConsoleCtrlHandler(Some(interrupt_handler), 1);
            }
            Self {}
        }
    }
}
impl Drop for InterruptGuard {
    #[allow(unsafe_code)]
    fn drop(&mut self) {
        #[cfg(unix)]
        {
            if let Some(previous) = &self.previous {
                // SAFETY: restores the exact prior disposition for this process.
                unsafe {
                    libc::sigaction(libc::SIGINT, previous, std::ptr::null_mut());
                }
            }
        }
        #[cfg(windows)]
        {
            #[link(name = "Kernel32")]
            unsafe extern "system" {
                fn SetConsoleCtrlHandler(
                    handler: Option<extern "system" fn(u32) -> i32>,
                    add: i32,
                ) -> i32;
            }
            // SAFETY: removes this exact static callback registration.
            unsafe {
                SetConsoleCtrlHandler(Some(interrupt_handler), 0);
            }
        }
    }
}

fn error_text(error: &io::Error, path: Option<&Path>, stat: bool) -> String {
    if error.kind() == io::ErrorKind::AlreadyExists && error.raw_os_error().is_none() {
        return error.to_string();
    }
    #[cfg(unix)]
    {
        let _ = stat;
        let code = error.raw_os_error().unwrap_or(5);
        let message = match code {
            1 => "Operation not permitted",
            2 => "No such file or directory",
            5 => "Input/output error",
            9 => "Bad file descriptor",
            13 => "Permission denied",
            20 => "Not a directory",
            21 => "Is a directory",
            28 => "No space left on device",
            32 => "Broken pipe",
            36 => "File name too long",
            40 => "Too many levels of symbolic links",
            _ => return error.to_string(),
        };
        format!(
            "[Errno {code}] {message}{}",
            path.map_or(String::new(), |p| format!(
                ": {}",
                os_repr(&p.as_os_str().to_os_string())
            ))
        )
    }
    #[cfg(windows)]
    {
        let code = error.raw_os_error().unwrap_or(5);
        let (prefix, message) = if stat {
            (
                format!("WinError {code}"),
                match code {
                    5 => "Access is denied",
                    32 => {
                        "The process cannot access the file because it is being used by another process"
                    }
                    267 => "The directory name is invalid",
                    _ => "The system cannot find the path specified",
                },
            )
        } else {
            (
                format!(
                    "Errno {}",
                    match code {
                        2 | 3 => 2,
                        5 | 32 | 33 => 13,
                        112 => 28,
                        109 => 22,
                        _ => 22,
                    }
                ),
                match code {
                    2 | 3 => "No such file or directory",
                    5 | 32 | 33 => "Permission denied",
                    112 => "No space left on device",
                    _ => "Invalid argument",
                },
            )
        };
        format!(
            "[{prefix}] {message}{}",
            path.map_or(String::new(), |p| format!(
                ": {}",
                os_repr(&p.as_os_str().to_os_string())
            ))
        )
    }
}

pub fn run(argv: Vec<OsString>) -> u8 {
    macro_rules! diagnostic {
        ($text:expr $(,)?) => {
            if stderr($text).is_err() {
                return 120;
            }
        };
    }
    let args = match args::parse(argv) {
        Ok(args::Parsed::Help) => {
            #[cfg(unix)]
            let owner = io::stdout();
            #[cfg(unix)]
            let mut stdout = {
                use std::os::fd::AsFd;
                DescriptorWriter(owner.as_fd())
            };
            #[cfg(windows)]
            let mut stdout = io::stdout().lock();
            if let Err(error) = write_output(
                &mut stdout,
                &super::text_bytes(&args::help(args::columns())),
            ) {
                let _ = stderr(&format!(
                    "wait-mail: could not write help to stdout ({}).\n",
                    error_text(&error, None, false)
                ));
                return 2;
            }
            return 0;
        }
        Ok(args::Parsed::Args(args)) => args,
        Err(error) => {
            diagnostic!(&format!(
                "{}pseudolife-mcp wait-mail: error: {error}\n",
                args::usage(args::columns())
            ));
            return 2;
        }
    };
    let digest = if let Some(path) = args.digest {
        crate::credentials::expand_user(&path).ok()
    } else {
        let session = args
            .session
            .unwrap_or_else(|| std::env::var_os("CLAUDE_CODE_SESSION_ID").unwrap_or_default());
        if let Some(error) = session_encoding_error(&session) {
            diagnostic!(&error);
            return 1;
        }
        digest_path(&session)
    };
    let Some(digest) = digest else {
        diagnostic!(
            "wait-mail: no session id to key the digest. Run it from a Claude Code session (which sets CLAUDE_CODE_SESSION_ID), or pass --session-id (a Codex thread id) or --digest PATH.\n",
        );
        return 2;
    };
    let present = match fs::metadata(&digest) {
        Ok(info) => info.is_file(),
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(error) => {
            diagnostic!(&format!(
                "wait-mail: cannot read the digest file at {} ({}).\n",
                os_display(&digest.as_os_str().to_os_string()),
                error_text(&error, Some(&digest), true)
            ));
            return 2;
        }
    };
    if !present {
        diagnostic!(&format!(
            "wait-mail: no digest file at {}. The shim writes it when its coordination adapter attaches: at shim start in Claude Code (if it is missing there, coordination did not attach for this session; the shim's stderr says why), on a thread's first memory_* call in Codex. Then re-arm. Also check that PSEUDOLIFE_DIGEST_DIR is the same for the MCP server and this shell.\n",
            os_display(&digest.as_os_str().to_os_string())
        ));
        return 2;
    }
    let started = Instant::now();
    let (watermark, body, ring) = match wait(&digest, args.timeout, args.interval) {
        WaitResult::Gone => {
            diagnostic!(
                "wait-mail: the digest file disappeared (the shim exited or restarted). Re-arm once the shim is back.\n",
            );
            return 2;
        }
        WaitResult::Interrupted => return 130,
        WaitResult::Corrupt(error) => {
            diagnostic!(&format!("wait-mail: {error}; left the mail unshown.\n"));
            return 2;
        }
        WaitResult::Timeout => {
            diagnostic!(&format!(
                "wait-mail: no ring in {} s (plain mail does not end the wait); re-arm to keep waiting.\n",
                args::general(args.timeout)
            ));
            return 3;
        }
        WaitResult::Mail(watermark, body, ring) => (watermark, body, ring),
    };
    let local = chrono::Local::now().format("%H:%M:%S");
    diagnostic!(&format!(
        "wait-mail: the daemon rang for addressed mail at {local} ({ring}, watermark {}, {:.0} s after arming):\n",
        watermark.text(),
        started.elapsed().as_secs_f64()
    ));
    #[cfg(unix)]
    let owner = io::stdout();
    #[cfg(unix)]
    let stdout = {
        use std::os::fd::AsFd;
        DescriptorWriter(owner.as_fd())
    };
    #[cfg(windows)]
    let stdout = io::stdout().lock();
    let marker = match write_then_mark(stdout, &body, || {
        mark_seen(&digest.with_extension("seen"), &watermark)
    }) {
        Ok(marker) => marker,
        Err((error, _stdout)) => {
            let _ = stderr(&format!(
                "wait-mail: could not write the mail to stdout ({}); left it unshown.\n",
                error_text(&error, None, false)
            ));
            return 2;
        }
    };
    if let Err(error) = marker {
        let details = if let (Some(from), Some(to)) = (&error.from, &error.to) {
            format!(
                "{} -> {}",
                error_text(&error.error, Some(from), true),
                os_repr(&to.as_os_str().to_os_string())
            )
        } else {
            error_text(&error.error, None, false)
        };
        diagnostic!(&format!(
            "wait-mail: could not advance the .seen marker ({}); the prompt hook or tool-result hint may show this mail again.\n",
            details
        ));
    }
    let size = String::from_utf8_lossy(&body).chars().count();
    let key = digest.file_stem().unwrap_or_default().to_string_lossy();
    let line = super::text_bytes(&format!(
        "{}\twait\t{}\t{}\t{size}\t{ring}\n",
        wall_time() as i64,
        &key[..8.min(key.len())],
        watermark.text()
    ));
    if let Ok(mut ledger) = OpenOptions::new()
        .create(true)
        .append(true)
        .open(digest.with_file_name("ledger.log"))
    {
        let _ = ledger.write_all(&line);
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, rc::Rc};

    #[cfg(unix)]
    #[test]
    fn fifo_read_observes_interrupt_before_open_or_read() {
        use std::process::{Command, Stdio};
        const CHILD: &str = "PSEUDOLIFE_TEST_FIFO_INTERRUPT";
        const AFTER: &str = "PSEUDOLIFE_TEST_FIFO_INTERRUPT_AFTER";
        if let Some(path) = std::env::var_os(CHILD) {
            let after: usize = std::env::var(AFTER).unwrap().parse().unwrap();
            // Deliver the interrupt after the caller's first flag check.
            let mut checks = 0;
            let error = interruptible_read_with(Path::new(&path), || {
                checks += 1;
                checks > after
            })
            .unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::Interrupted);
            return;
        }
        let home =
            std::env::temp_dir().join(format!("wait-mail-interrupt-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let fifo = home.join("digest");
        rustix::fs::mkfifoat(
            rustix::fs::CWD,
            &fifo,
            rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
        )
        .unwrap();
        let mut results = Vec::new();
        for after in [1, 2] {
            // No writer blocks open; a connected, silent writer blocks read.
            let writer = (after == 2).then(|| {
                rustix::fs::open(
                    &fifo,
                    rustix::fs::OFlags::RDWR | rustix::fs::OFlags::NONBLOCK,
                    rustix::fs::Mode::empty(),
                )
                .unwrap()
            });
            let mut child = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "cli::wait_mail::tests::fifo_read_observes_interrupt_before_open_or_read",
                ])
                .env_clear()
                .env(CHILD, &fifo)
                .env(AFTER, after.to_string())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap();
            let deadline = Instant::now() + Duration::from_secs(2);
            let mut forced = false;
            while child.try_wait().unwrap().is_none() {
                if Instant::now() >= deadline {
                    child.kill().unwrap();
                    forced = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            let output = child.wait_with_output().unwrap();
            drop(writer);
            results.push((after, forced, output));
        }
        fs::remove_dir_all(home).unwrap();
        for (after, forced, output) in results {
            assert!(
                !forced,
                "an interrupt before a blocking FIFO syscall was lost (after={after})"
            );
            assert!(output.status.success(), "{output:?}");
            assert!(String::from_utf8_lossy(&output.stdout).contains("running 1 test"));
        }
    }

    #[cfg(unix)]
    #[test]
    fn fifo_read_waits_for_a_writer_and_keeps_all_bytes_until_eof() {
        use std::process::{Command, Stdio};
        const CHILD: &str = "PSEUDOLIFE_TEST_FIFO_EOF";
        const DELAYED: &str = "PSEUDOLIFE_TEST_FIFO_EOF_DELAYED";
        const RELEASED: &str = "released delayed writer after the reader returned";
        if let Some(path) = std::env::var_os(CHILD) {
            let delayed = std::env::var(DELAYED).as_deref() == Ok("1");
            let path = PathBuf::from(path);
            let writer_path = path.clone();
            let bytes = vec![b'p'; 10000];
            let sent = bytes.clone();
            let (release, startup) = std::sync::mpsc::channel();
            let writer = std::thread::spawn(move || {
                if delayed {
                    startup.recv().unwrap();
                }
                OpenOptions::new()
                    .write(true)
                    .open(writer_path)?
                    .write_all(&sent)
            });
            let deadline = Instant::now() + Duration::from_secs(2);
            let mut checks = 0;
            let result = interruptible_read_with(&path, || {
                checks += 1;
                (delayed && checks > 1) || Instant::now() >= deadline
            });
            if delayed {
                assert_eq!(
                    result.as_ref().unwrap_err().kind(),
                    io::ErrorKind::Interrupted
                );
                release.send(()).unwrap();
                println!("{RELEASED}");
                io::stdout().flush().unwrap();
            }
            let written = writer.join().unwrap();
            written.unwrap();
            assert_eq!(result.unwrap(), bytes);
            return;
        }
        let home = std::env::temp_dir().join(format!("wait-mail-read-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let fifo = home.join("digest");
        rustix::fs::mkfifoat(
            rustix::fs::CWD,
            &fifo,
            rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
        )
        .unwrap();
        let mut results = Vec::new();
        for delayed in [false, true] {
            // Contain a writer that starts only after the reader has closed.
            let mut child = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "cli::wait_mail::tests::fifo_read_waits_for_a_writer_and_keeps_all_bytes_until_eof",
                    "--nocapture",
                ])
                .env_clear()
                .env(CHILD, &fifo)
                .env(DELAYED, if delayed { "1" } else { "0" })
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap();
            let deadline = Instant::now() + Duration::from_secs(2);
            let mut forced = false;
            while child.try_wait().unwrap().is_none() {
                if Instant::now() >= deadline {
                    child.kill().unwrap();
                    forced = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            let output = child.wait_with_output().unwrap();
            results.push((delayed, forced, output));
        }
        fs::remove_dir_all(home).unwrap();
        for (delayed, forced, output) in results {
            assert_eq!(forced, delayed, "{output:?}");
            let text = String::from_utf8_lossy(&output.stdout);
            assert!(text.contains("running 1 test"), "{output:?}");
            if delayed {
                assert!(text.contains(RELEASED), "{output:?}");
            } else {
                assert!(output.status.success(), "{output:?}");
            }
        }
    }

    #[test]
    fn metadata_error_refuses_arming_and_retries_during_wait() {
        let home = std::env::temp_dir().join(format!("wait-mail-stat-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let digest = home.join(format!("{}.txt", "a".repeat(64)));
        fs::write(&digest, b"1\npeer\n").unwrap();
        fs::write(digest.with_extension("ring"), b"1\nrung anyone\n").unwrap();
        fs::deny_once(&digest);
        assert_eq!(
            run(vec![
                OsString::from("--digest"),
                digest.clone().into_os_string(),
                OsString::from("--timeout"),
                OsString::from("1"),
                OsString::from("--interval"),
                OsString::from("0.01")
            ]),
            2
        );
        assert_eq!(fs::clear_denial(), 1);
        assert_eq!(fs::read_dir(&home).unwrap().count(), 2);
        fs::deny_once(&digest);
        let result = wait(&digest, 1.0, 0.01);
        assert!(fs::clear_denial() >= 2);
        match result {
            WaitResult::Mail(watermark, body, _) => {
                assert_eq!(watermark.text(), "1");
                assert_eq!(body, b"peer\n");
            }
            _ => panic!("metadata retry did not reach mail"),
        }
        assert_eq!(fs::read_dir(&home).unwrap().count(), 2);
        fs::remove_dir_all(home).unwrap();
    }

    #[derive(Debug, Default)]
    struct RecordedOutput {
        written: Vec<u8>,
        flushed: Vec<u8>,
        events: Vec<&'static str>,
    }
    #[derive(Clone, Copy, Debug)]
    enum OutputFailure {
        Write,
        Flush,
    }
    #[derive(Debug)]
    struct RecordingWriter {
        output: Rc<RefCell<RecordedOutput>>,
        failure: Option<OutputFailure>,
    }
    impl Write for RecordingWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            let mut output = self.output.borrow_mut();
            output.events.push("write");
            if matches!(self.failure, Some(OutputFailure::Write)) {
                return Err(io::Error::new(io::ErrorKind::BrokenPipe, "write failed"));
            }
            let count = bytes.len().min(2);
            output.written.extend_from_slice(&bytes[..count]);
            Ok(count)
        }
        fn flush(&mut self) -> io::Result<()> {
            let mut output = self.output.borrow_mut();
            output.events.push("flush");
            if matches!(self.failure, Some(OutputFailure::Flush)) {
                return Err(io::Error::other("flush failed"));
            }
            output.flushed = output.written.clone();
            Ok(())
        }
    }
    impl Drop for RecordingWriter {
        fn drop(&mut self) {
            self.output.borrow_mut().events.push("drop");
        }
    }
    fn recording_writer(failure: Option<OutputFailure>) -> RecordingWriter {
        RecordingWriter {
            output: Rc::new(RefCell::new(RecordedOutput::default())),
            failure,
        }
    }

    #[test]
    fn write_then_mark_flushes_body_and_releases_writer_before_marker_rename() {
        let home =
            std::env::temp_dir().join(format!("wait-mail-ordering-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let path = home.join("marker.seen");
        let body = "peer — café 🧠\n".as_bytes();
        let writer = recording_writer(None);
        let output = Rc::clone(&writer.output);
        write_then_mark(writer, body, || {
            assert_eq!(output.borrow().flushed, body);
            assert!(output.borrow().events.ends_with(&["flush", "drop"]));
            assert!(!path.exists());
            mark_seen(&path, &Integer::parse(b"3").unwrap()).unwrap();
            output.borrow_mut().events.push("mark");
        })
        .unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"3\n");
        assert!(output.borrow().events.ends_with(&["flush", "drop", "mark"]));
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn write_then_mark_interrupted_marker_keeps_flushed_body_and_absent_seen() {
        #[derive(Debug)]
        struct Killed;
        let home = std::env::temp_dir().join(format!("wait-mail-killed-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let path = home.join("marker.seen");
        let body = "peer — café 🧠\n".as_bytes();
        let writer = recording_writer(None);
        let output = Rc::clone(&writer.output);
        let killed = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = write_then_mark::<_, ()>(writer, body, || {
                assert_eq!(output.borrow().flushed, body);
                assert!(output.borrow().events.ends_with(&["flush", "drop"]));
                std::panic::panic_any(Killed);
            });
        }))
        .unwrap_err();
        assert!(killed.is::<Killed>());
        assert_eq!(output.borrow().flushed, body);
        assert!(!path.exists());
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn write_then_mark_write_failure_retains_writer_and_never_calls_marker() {
        let writer = recording_writer(Some(OutputFailure::Write));
        let output = Rc::clone(&writer.output);
        let (error, writer) =
            write_then_mark::<_, ()>(writer, b"peer\n", || panic!("marker called")).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::BrokenPipe);
        assert!(output.borrow().written.is_empty());
        assert_eq!(output.borrow().events, ["write"]);
        output.borrow_mut().events.push("diagnostic");
        drop(writer);
        assert_eq!(output.borrow().events, ["write", "diagnostic", "drop"]);
    }

    #[test]
    fn write_then_mark_flush_failure_retains_writer_and_never_calls_marker() {
        let writer = recording_writer(Some(OutputFailure::Flush));
        let output = Rc::clone(&writer.output);
        let (error, writer) =
            write_then_mark::<_, ()>(writer, b"peer\n", || panic!("marker called")).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Other);
        assert_eq!(output.borrow().written, b"peer\n");
        assert!(output.borrow().flushed.is_empty());
        assert!(output.borrow().events.ends_with(&["flush"]));
        output.borrow_mut().events.push("diagnostic");
        drop(writer);
        assert!(
            output
                .borrow()
                .events
                .ends_with(&["flush", "diagnostic", "drop"])
        );
    }

    #[test]
    fn stdout_bytes_are_written_without_interpreter_buffer_flags() {
        let mut output = Vec::new();
        write_output(&mut output, b"peer\n").unwrap();
        assert_eq!(output, b"peer\n");
    }
    #[test]
    fn temporary_collisions_keep_existing_files_and_directories() {
        let home =
            std::env::temp_dir().join(format!("wait-mail-collision-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let existing = home.join(".tmp-occupied.seen");
        fs::write(&existing, b"keep").unwrap();
        let directory = home.join(".tmp-director.seen");
        fs::create_dir(&directory).unwrap();
        let mut names = ["occupied", "director", "freename"].into_iter();
        let (path, mut file) = temporary_with(&home.join("marker.seen"), false, || {
            names.next().unwrap().into()
        })
        .unwrap();
        file.write_all(b"new").unwrap();
        drop(file);
        assert_eq!(fs::read(&existing).unwrap(), b"keep");
        assert!(directory.is_dir());
        assert_eq!(fs::read(&path).unwrap(), b"new");
        fs::remove_dir_all(home).unwrap();
    }
    #[cfg(windows)]
    #[test]
    fn unpaired_windows_session_surrogates_are_refused() {
        use std::os::windows::ffi::OsStringExt;
        assert_eq!(
            session_encoding_error(&OsString::from_wide(&[0xd83e, 0xdde0, 0xdcff])).unwrap(),
            "UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcff' in position 1: surrogates not allowed\n"
        );
    }
    #[test]
    fn integer_bytes_match_decimal_grammar_without_machine_integer_truncation() {
        assert!(Integer::parse(b" \t+0_012\r\n").is_none());
        assert_eq!(Integer::parse(b"12").unwrap().text(), "12");
        assert_eq!(
            Integer::parse(b"00042").unwrap(),
            Integer::parse(b"42").unwrap()
        );
        assert_eq!(Integer::parse(b"00000").unwrap(), Integer::zero());
        assert!(Integer::parse(b"00042").unwrap() > Integer::parse(b"9").unwrap());
        assert!(Integer::parse(b"-000").is_none());
        assert!(Integer::parse(b"1__2").is_none());
        assert!(Integer::parse(b"\xc2\xa012").is_none());
        assert!(Integer::parse("1".repeat(4301).as_bytes()).is_some());
        assert!(
            Integer::parse(b"123456789012345678901234567890").unwrap()
                > Integer::parse(b"9999999999999999999").unwrap()
        );
    }
    #[test]
    fn seen_reader_matches_hook_truncation_and_unsigned_decimal_values() {
        let home =
            std::env::temp_dir().join(format!("wait-mail-hook-seen-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let path = home.join("marker.seen");
        for raw in [b"".as_slice(), b" \t\r\n\x0b\x0c"] {
            fs::write(&path, raw).unwrap();
            assert_eq!(read_seen(&path).unwrap(), Integer::zero());
        }
        for raw in [b"00042".as_slice(), b" \t00042\r\n"] {
            fs::write(&path, raw).unwrap();
            assert_eq!(read_seen(&path).unwrap(), Integer::parse(b"42").unwrap());
        }
        // The prompt hook's redirection truncates before printf writes its counter.
        let mut writer = File::create(&path).unwrap();
        assert_eq!(read_seen(&path).unwrap(), Integer::zero());
        writer.write_all(b"00042\n").unwrap();
        writer.flush().unwrap();
        assert_eq!(read_seen(&path).unwrap(), Integer::parse(b"42").unwrap());
        drop(writer);
        for raw in [b"+42".as_slice(), b"-42", b"4_2", b"\xd9\xa4\xd9\xa2"] {
            fs::write(&path, raw).unwrap();
            assert_eq!(
                read_seen(&path).unwrap_err().kind(),
                io::ErrorKind::InvalidData
            );
        }
        fs::remove_dir_all(home).unwrap();
    }
    #[test]
    fn seen_writer_keeps_a_higher_existing_marker() {
        let home = std::env::temp_dir().join(format!("wait-mail-seen-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let path = home.join("marker.seen");
        fs::write(&path, b"22\n").unwrap();
        mark_seen(&path, &Integer::parse(b"12").unwrap()).unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"22\n");
        fs::remove_dir_all(home).unwrap();
    }
    #[test]
    fn overlapping_listener_cleanup_is_owned_and_private() {
        let home =
            std::env::temp_dir().join(format!("wait-mail-listeners-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let digest = home.join(format!("{}.txt", "a".repeat(64)));
        let first = Listener::new(&digest, 150.0);
        let second = Listener::new(&digest, 150.0);
        assert_ne!(first.path, second.path);
        first.renew();
        second.renew();
        let raw = fs::read(&second.path).unwrap();
        let text = std::str::from_utf8(&raw).unwrap();
        let lines = text.lines().collect::<Vec<_>>();
        assert_eq!(lines[0], second.token);
        let expiry: f64 = lines[1].parse().unwrap();
        assert!(expiry <= wall_time() + 60.0 && expiry > wall_time());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&second.path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
        #[cfg(windows)]
        {
            let file = File::open(&second.path).unwrap();
            assert!(crate::credentials::windows_security::validate(&file).is_ok());
        }
        drop(first);
        assert!(second.path.is_file());
        drop(second);
        assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
        fs::remove_dir_all(home).unwrap();
    }
    #[test]
    fn ring_reader_accepts_only_the_hook_marker_grammar() {
        let home = std::env::temp_dir().join(format!("wait-mail-ring-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let path = home.join("marker.ring");
        for raw in [
            b"12\nrung anyone!\n".as_slice(),
            b"12\nrung caf\xc3\xa9\n",
            b"12\nplain anyone\n",
        ] {
            fs::write(&path, raw).unwrap();
            assert!(read_ring(&path).unwrap().is_none());
        }
        fs::write(&path, b"1234567890123\nrung anyone\n").unwrap();
        assert!(read_ring(&path).unwrap().is_none());
        fs::write(&path, b" 1 2 \r\nrung \r\nextra").unwrap();
        assert_eq!(
            read_ring(&path).unwrap(),
            Some((Integer::parse(b"12").unwrap(), "rung ".into()))
        );
        fs::write(&path, b"12\nrung anyone\n").unwrap();
        #[cfg(unix)]
        {
            let link = home.join("link.ring");
            std::os::unix::fs::symlink(&path, &link).unwrap();
            assert!(read_ring(&link).unwrap().is_none());
        }
        fs::remove_dir_all(home).unwrap();
    }
}

#[cfg(test)]
#[path = "wait_mail_reduction_tests.rs"]
mod reduction_tests;
