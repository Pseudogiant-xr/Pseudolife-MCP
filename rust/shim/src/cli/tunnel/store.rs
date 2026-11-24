//! Read-only port of `tunnel_profiles.ProfileStore`: checked paths, private
//! reads, profile validation and the private key's presence.
use std::{
    fs::{self, File, Metadata, OpenOptions},
    io::{self, Read},
    path::{Path, PathBuf},
};

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};

use super::Fail;
use super::syntax::{Admit, json_loads, parse_expiry, valid_name, validate_url};

const REDIRECT: &str = "tunnel paths must not contain redirects";
const UNSAFE_PATH: &str = "tunnel path cannot be inspected safely";
pub(super) const UNAVAILABLE: &str = "private tunnel file is unavailable or not owner-only";
const TOO_LARGE: &str = "private tunnel file is too large";
const INVALID_PROFILE: &str = "tunnel profile is invalid";
pub(super) const INVALID_NAME: &str = "invalid tunnel profile name";
pub(super) const NOT_OWNER_ONLY: &str = "tunnel profile directory must be owner-only";
const EXPIRY: &str = "key expiry requires an ISO date or timestamp with timezone";
const FIELDS: [&str; 12] = [
    "name",
    "daemon_url",
    "token_file",
    "tunnel_id",
    "organization_id",
    "state",
    "consent",
    "autostart_consent",
    "runtime_version",
    "minimum_catalog",
    "runtime_key_expires_at",
    "cloud_verification_file",
];

/// The validated fields a ported command reads.
#[derive(Debug)]
pub(super) struct Profile {
    pub name: String,
    /// The daemon the bridge forwards to, and the token file it names (a
    /// path; the token itself is never read here).
    pub daemon_url: String,
    pub token_file: String,
    pub tunnel_id: Option<String>,
    pub organization_id: Option<String>,
    pub state: String,
    pub catalog: String,
    pub runtime_version: Option<String>,
    pub expires_at: Option<(String, DateTime<Utc>)>,
}

fn is_redirect(info: &Metadata) -> bool {
    if info.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if info.file_attributes() & 0x400 != 0 {
            return true;
        }
    }
    false
}

/// `os.path.exists`-style probe: absent is `false`; any other failure is not
/// decided here (Python's `Path.exists` raises for some of them).
pub(super) fn exists(path: &Path) -> Result<bool, Fail> {
    match fs::metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(Fail::Defer),
    }
}

/// Whether anything (a link included) occupies `path`.
pub(super) fn occupied(path: &Path) -> Result<bool, Fail> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(Fail::Defer),
    }
}

/// An absolute path a ported command may inspect: a drive-letter path on
/// Windows (no UNC or device namespace), a rooted one elsewhere.
fn plain_absolute(path: &Path) -> bool {
    let Some(text) = path.to_str() else {
        return false;
    };
    if text.contains('\0') {
        return false;
    }
    if cfg!(windows) {
        let b = text.as_bytes();
        b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && matches!(b[2], b'\\' | b'/')
    } else {
        text.starts_with('/')
    }
}

/// `tunnel_profiles.checked_path`: `abspath(expanduser(path))`, no redirect
/// among its ancestors, and no redirect at the leaf.
pub(super) fn checked_path(path: &Path) -> Result<PathBuf, Fail> {
    if path
        .to_str()
        .is_none_or(|text| text.starts_with('~') || text.contains('\0'))
    {
        return Err(Fail::Defer);
    }
    let target = crate::credentials::absolute_expanded(path).map_err(|_| Fail::Defer)?;
    if !plain_absolute(&target) {
        return Err(Fail::Defer);
    }
    crate::credentials::reject_ancestor_redirects(&target)
        .map_err(|_| Fail::Tunnel(UNSAFE_PATH))?;
    match fs::symlink_metadata(&target) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(target),
        Err(_) => Err(Fail::Defer),
        Ok(info) if !is_redirect(&info) => Ok(target),
        // A POSIX symlink, or a Windows redirect that resolves (exists()).
        Ok(_) if !cfg!(windows) || fs::metadata(&target).is_ok() => Err(Fail::Tunnel(REDIRECT)),
        // A dangling Windows junction: Path.is_symlink() is not decided here.
        Ok(_) => Err(Fail::Defer),
    }
}

fn open_private(path: &Path) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        // NONBLOCK: a FIFO swapped in after the type check below cannot
        // block the open (regular files ignore the flag).
        use rustix::fs::OFlags;
        options.custom_flags((OFlags::NOFOLLOW | OFlags::NONBLOCK).bits() as i32);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{FILE_SHARE_READ, FILE_SHARE_WRITE};
        // The CRT's os.open shares read and write, never delete.
        options.share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE);
    }
    sharing_retry(|| options.open(path))
}

pub(super) fn sharing_retry<T>(mut operation: impl FnMut() -> io::Result<T>) -> io::Result<T> {
    let mut attempt = 0;
    loop {
        match operation() {
            // _sharing_retry: a transient Windows sharing violation clears.
            Err(error)
                if cfg!(windows)
                    && (error.kind() == io::ErrorKind::PermissionDenied
                        || error.raw_os_error() == Some(32))
                    && attempt < 19 =>
            {
                attempt += 1;
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            other => return other,
        }
    }
}

/// `credentials._validate_file` on an open handle.
fn owner_only_file(file: &File) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let Ok(info) = file.metadata() else {
            return false;
        };
        info.is_file()
            && info.nlink() == 1
            && info.uid() == rustix::process::geteuid().as_raw()
            && info.mode() & 0o077 == 0
    }
    #[cfg(windows)]
    {
        crate::credentials::windows_security::validate(file).is_ok()
    }
}

/// `tunnel_profiles.private_read`.
pub(super) fn private_read(path: &Path, limit: usize) -> Result<Vec<u8>, Fail> {
    let target = checked_path(path)?;
    // Python's blocking open of a FIFO waits for a writer: not decided here.
    #[cfg(unix)]
    {
        use std::os::unix::fs::FileTypeExt;
        if fs::symlink_metadata(&target).is_ok_and(|info| info.file_type().is_fifo()) {
            return Err(Fail::Defer);
        }
    }
    let file = open_private(&target).map_err(|_| Fail::Tunnel(UNAVAILABLE))?;
    if !owner_only_file(&file) {
        return Err(Fail::Tunnel(UNAVAILABLE));
    }
    let mut data = Vec::new();
    file.take(limit as u64 + 1)
        .read_to_end(&mut data)
        .map_err(|_| Fail::Tunnel(UNAVAILABLE))?;
    if data.len() > limit {
        return Err(Fail::Tunnel(TOO_LARGE));
    }
    Ok(data)
}

pub(super) fn profile_path(root: &Path, name: &str) -> Result<PathBuf, Fail> {
    if !valid_name(name) {
        return Err(Fail::Tunnel(INVALID_NAME));
    }
    checked_path(&root.join(format!("{name}.profile.json")))
}

/// `ProfileStore.load(name)`.
pub(super) fn load(root: &Path, name: &str) -> Result<Profile, Fail> {
    let data = private_read(&profile_path(root, name)?, 65536)?;
    decode_profile(&data, name).map(|(_, profile)| profile)
}

pub(super) fn decode_profile(
    data: &[u8],
    name: &str,
) -> Result<(Map<String, Value>, Profile), Fail> {
    let payload = match json_loads(data) {
        Admit::Yes(Value::Object(payload)) => payload,
        Admit::Yes(_) | Admit::No => return Err(Fail::Tunnel(INVALID_PROFILE)),
        Admit::Defer => return Err(Fail::Defer),
    };
    // Unknown fields, then Profile(**payload)'s missing required arguments.
    if payload.keys().any(|key| !FIELDS.contains(&key.as_str()))
        || FIELDS[..3].iter().any(|key| !payload.contains_key(*key))
    {
        return Err(Fail::Tunnel(INVALID_PROFILE));
    }
    let profile = validate(&payload)?;
    if profile.name != name {
        return Err(Fail::Tunnel(INVALID_PROFILE));
    }
    Ok((payload, profile))
}

/// The profile file a person would look for, even when its path cannot be
/// checked.
pub(super) fn profile_file(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.profile.json"))
}

/// `run` could not open the store root: an empty `--profile-dir`, no home
/// directory to resolve, or a link or junction on its path.
pub(super) const UNSAFE_ROOT: &str = "the tunnel directory cannot be opened safely (an empty --profile-dir, no home directory, or a link or junction on its path); point it at a plain directory your account owns, then retry";

/// Why `load(root, name)` deferred, in plain words naming the file or field
/// this client does not read (for a person; no field value is repeated).
pub(super) fn unread_reason(root: &Path, name: &str) -> &'static str {
    const FILE: &str = "the profile file cannot be opened safely here (a pipe, or a link or junction this client does not follow)";
    let Ok(path) = profile_path(root, name) else {
        return FILE;
    };
    let payload = match private_read(&path, 65536) {
        Err(_) => return FILE,
        Ok(data) => match json_loads(&data) {
            Admit::Yes(Value::Object(payload)) => payload,
            _ => {
                return "the profile file uses JSON this client does not read (a byte-order mark, a UTF-16 or UTF-32 encoding, NaN or Infinity, or very deep nesting)";
            }
        },
    };
    let path_field = |key: &str| {
        text(&payload, key)
            .flatten()
            .is_some_and(|value| match absolute_reference(value) {
                Err(Fail::Defer) => true,
                Ok(true) => checked_path(Path::new(value)) == Err(Fail::Defer),
                _ => false,
            })
    };
    if text(&payload, "daemon_url")
        .flatten()
        .is_some_and(|url| validate_url(url) == Err(Fail::Defer))
    {
        return "its daemon_url is a URL form this client does not read";
    }
    if path_field("token_file") {
        return "its token_file is a path form this client does not read (a UNC or device path)";
    }
    if text(&payload, "runtime_version")
        .flatten()
        .is_some_and(|version| !version.is_ascii())
    {
        return "its runtime_version is not plain ASCII digits";
    }
    if let Some(Value::String(value)) = field(&payload, "runtime_key_expires_at")
        && matches!(parse_expiry(value), Admit::Defer)
    {
        return "its runtime_key_expires_at uses a timestamp spelling this client does not read (a fraction of other than 3 or 6 digits, hour 24, or an unusual offset)";
    }
    if path_field("cloud_verification_file") {
        return "its cloud_verification_file is a path form this client does not read (a UNC or device path)";
    }
    "it holds a value this client does not read"
}

fn field<'a>(payload: &'a Map<String, Value>, key: &str) -> Option<&'a Value> {
    payload.get(key).filter(|value| !value.is_null())
}

fn text<'a>(payload: &'a Map<String, Value>, key: &str) -> Option<Option<&'a str>> {
    field(payload, key).map(Value::as_str)
}

/// `Path(value).is_absolute()` for a reference the oracle stores as text.
fn absolute_reference(value: &str) -> Result<bool, Fail> {
    if value.contains('\0') {
        return Err(Fail::Defer);
    }
    if cfg!(windows) {
        let b = value.as_bytes();
        if b.len() >= 2 && matches!(b[0], b'\\' | b'/') && matches!(b[1], b'\\' | b'/') {
            return Err(Fail::Defer); // UNC and device namespaces
        }
        Ok(b.len() >= 3
            && b[0].is_ascii_alphabetic()
            && b[1] == b':'
            && matches!(b[2], b'\\' | b'/'))
    } else {
        Ok(value.starts_with('/'))
    }
}

/// `Profile.validate`, check by check in the oracle's order.
pub(super) fn validate(payload: &Map<String, Value>) -> Result<Profile, Fail> {
    let name = match text(payload, "name") {
        Some(Some(name)) if valid_name(name) => name.to_owned(),
        _ => return Err(Fail::Tunnel(INVALID_NAME)),
    };
    let daemon_url = match text(payload, "daemon_url") {
        Some(Some(url)) => {
            validate_url(url)?;
            url.to_owned()
        }
        _ => return Err(Fail::Tunnel("invalid tunnel endpoint")),
    };
    let token_file = match text(payload, "token_file") {
        Some(Some(token)) if !token.is_empty() && absolute_reference(token)? => {
            checked_path(Path::new(token))?;
            token.to_owned()
        }
        _ => {
            return Err(Fail::Tunnel(
                "daemon credential reference requires an absolute path",
            ));
        }
    };
    // re.fullmatch(prefix + '[class]+', value) for an optional identifier.
    let identifier =
        |key: &str, prefix: &str, class: fn(&u8) -> bool, message: &'static str| match text(
            payload, key,
        ) {
            None => Ok(()),
            Some(Some(value))
                if value
                    .strip_prefix(prefix)
                    .is_some_and(|rest| !rest.is_empty() && rest.as_bytes().iter().all(class)) =>
            {
                Ok(())
            }
            Some(_) => Err(Fail::Tunnel(message)),
        };
    identifier(
        "tunnel_id",
        "tunnel_",
        u8::is_ascii_hexdigit,
        "invalid tunnel identifier",
    )?;
    identifier(
        "organization_id",
        "org-",
        |b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'),
        "invalid organization identifier",
    )?;
    let state = match payload.get("state") {
        None => "pending",
        Some(Value::String(state)) if state == "pending" || state == "ready" => state,
        Some(_) => return Err(Fail::Tunnel("invalid tunnel profile state")),
    };
    for key in ["consent", "autostart_consent"] {
        if payload.get(key).is_some_and(|value| !value.is_boolean()) {
            return Err(Fail::Tunnel("invalid tunnel profile state"));
        }
    }
    let consent = payload.get("consent").and_then(Value::as_bool) == Some(true);
    if state == "ready" && (!consent || field(payload, "tunnel_id").is_none()) {
        return Err(Fail::Tunnel(
            "ready tunnel requires explicit consent and an identifier",
        ));
    }
    match text(payload, "runtime_version") {
        None => {}
        Some(Some(version)) if !version.is_ascii() => return Err(Fail::Defer),
        Some(Some(version))
            if version.split('.').count() == 3
                && version
                    .split('.')
                    .all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit())) => {}
        Some(_) => return Err(Fail::Tunnel("invalid runtime version")),
    }
    let catalog = match payload.get("minimum_catalog") {
        None => "current",
        Some(Value::String(catalog)) if catalog == "current" || catalog == "full" => catalog,
        Some(_) => return Err(Fail::Tunnel("invalid tunnel catalog selection")),
    };
    let expires_at = match field(payload, "runtime_key_expires_at") {
        None => None,
        Some(Value::String(value)) => match parse_expiry(value) {
            Admit::Yes(instant) => Some((value.clone(), instant)),
            Admit::No => return Err(Fail::Tunnel(EXPIRY)),
            Admit::Defer => return Err(Fail::Defer),
        },
        // value.replace(...) on a non-string: AttributeError, then refused.
        Some(_) => return Err(Fail::Tunnel(EXPIRY)),
    };
    match text(payload, "cloud_verification_file") {
        None => {}
        Some(Some(path)) if absolute_reference(path)? => {
            checked_path(Path::new(path))?;
        }
        Some(_) => {
            return Err(Fail::Tunnel(
                "cloud verification reference requires an absolute path",
            ));
        }
    }
    let id = |key: &str| text(payload, key).flatten().map(str::to_owned);
    Ok(Profile {
        name,
        daemon_url,
        token_file,
        tunnel_id: id("tunnel_id"),
        organization_id: id("organization_id"),
        state: state.to_owned(),
        catalog: catalog.to_owned(),
        runtime_version: id("runtime_version"),
        expires_at,
    })
}

/// A Windows key this session cannot unseal, which is never called absent.
pub(super) const SEALED: &str = "the saved runtime key cannot be unsealed in this session: it was sealed by another account, or this logon has no access to the account's credentials (such as a key-based SSH logon). Run the command from a desktop session of the account that saved the key, or import the key again with tunnel setup --read-key or --key-file";

/// `_key_present`: `ProfileStore.read_key` succeeds. A Windows key this
/// session cannot unseal is refused rather than called absent.
pub(super) fn key_present(root: &Path, name: &str) -> Result<bool, Fail> {
    key_state(root, name)?.ok_or(Fail::Tunnel(SEALED))
}

/// The private key's presence: `Some(true)` when it reads as a valid key
/// (on Windows, after DPAPI unseals it), `Some(false)` when it is absent,
/// unreadable as a private file, protected for another platform or not a
/// key, and `None` for a Windows key that cannot be unsealed in this
/// session: another account's key, or the account's own over a logon
/// without its credentials (a key-based SSH logon). `Err(Defer)` when the
/// key file itself is a form
/// this client does not open (a pipe, a link).
pub(super) fn key_state(root: &Path, name: &str) -> Result<Option<bool>, Fail> {
    let path = match checked_path(&root.join(format!("{name}.key"))) {
        Ok(path) => path,
        Err(Fail::Tunnel(_) | Fail::Detail(_)) => return Ok(Some(false)),
        Err(Fail::Defer) => return Err(Fail::Defer),
    };
    let data = match private_read(&path, 16384) {
        Ok(data) => data,
        Err(Fail::Tunnel(_) | Fail::Detail(_)) => return Ok(Some(false)),
        Err(Fail::Defer) => return Err(Fail::Defer),
    };
    let (prefix, encoded) = match data.iter().position(|b| *b == 0) {
        Some(index) => (&data[..index], &data[index + 1..]),
        None => (&data[..], &[][..]),
    };
    #[cfg(windows)]
    {
        if prefix != b"DPAPI" {
            return Ok(Some(false));
        }
        Ok(super::dpapi::holds_key(encoded))
    }
    #[cfg(not(windows))]
    Ok(Some(
        prefix == b"PLAIN" && crate::credentials::decode_token(encoded).is_ok(),
    ))
}

/// The key file `key_state` reads, for a person.
pub(super) fn key_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.key"))
}

/// The first `*.profile.json` in `root` whose stem `list` refuses as a
/// profile name, matched as `list` matches it.
pub(super) fn invalid_profile_entry(root: &Path) -> Option<PathBuf> {
    fs::read_dir(root).ok()?.flatten().find_map(|entry| {
        let file_name = entry.file_name();
        let text = file_name.to_str()?;
        let folded = if cfg!(windows) {
            text.to_ascii_lowercase()
        } else {
            text.to_owned()
        };
        folded.strip_suffix(".profile.json")?;
        // The stem exactly as `list` takes it: case-sensitively, so a
        // Windows `dot.PROFILE.JSON` keeps its whole name and is refused.
        let stem = text.strip_suffix(".profile.json").unwrap_or(text);
        (!valid_name(stem)).then(|| entry.path())
    })
}

/// Why listing `root` deferred, naming the root or the one entry it stopped
/// on, with its fix: a root that cannot be inspected, is not a directory or
/// cannot be listed; a file name that is not UTF-8 (or, on Windows, not
/// ASCII); a `*.profile.json` that is not a regular file. Each entry named
/// blocks `list`; with several, fixing one may reveal the next. The
/// ownership check runs first, as in `list`, so the closing sentence is
/// reached only if the directory changed between the two reads.
pub(super) fn list_refusal(root: &Path) -> String {
    let root_shown = root.display();
    let keep = "keeping every .key and .process.json file, then retry";
    match fs::metadata(root) {
        Err(_) => {
            return format!(
                "the tunnel directory {root_shown} cannot be inspected; make it a directory your account can read, then retry"
            );
        }
        Ok(info) if !info.is_dir() => {
            return format!(
                "the tunnel directory {root_shown} is not a directory; move it aside, then retry"
            );
        }
        // `list` checks the directory's ownership before any entry, so a
        // check that cannot be decided is the cause, whatever it holds.
        Ok(info) => match owner_only_directory(root, &info) {
            Ok(()) => {}
            Err(Fail::Defer) => {
                return format!(
                    "the permissions of the tunnel directory {root_shown} cannot be checked; make it a directory only your account can access, then retry"
                );
            }
            Err(_) => {
                return format!(
                    "the tunnel directory {root_shown} must be owner-only; make it a directory only your account can access, then retry"
                );
            }
        },
    }
    let unlisted = || {
        format!(
            "the tunnel directory {root_shown} cannot be listed; make it a directory your account can read, then retry"
        )
    };
    let Ok(entries) = fs::read_dir(root) else {
        return unlisted();
    };
    for entry in entries {
        let Ok(entry) = entry else {
            return unlisted();
        };
        let file_name = entry.file_name();
        let Some(text) = file_name.to_str() else {
            return format!(
                "the tunnel directory {root_shown} holds an entry whose name is not UTF-8 ({}); move that one entry out, {keep}",
                file_name.to_string_lossy()
            );
        };
        if cfg!(windows) && !text.is_ascii() {
            return format!(
                "the tunnel directory {root_shown} holds {text}, whose name is not plain ASCII; move that one entry out, {keep}"
            );
        }
        let folded = if cfg!(windows) {
            text.to_ascii_lowercase()
        } else {
            text.to_owned()
        };
        if folded.ends_with(".profile.json") && !entry.file_type().is_ok_and(|kind| kind.is_file())
        {
            return format!(
                "{} is not a regular file; move it out of the tunnel directory, {keep}",
                entry.path().display()
            );
        }
    }
    format!(
        "the permissions of the tunnel directory {root_shown} cannot be checked; make it a directory only your account can access, then retry"
    )
}

/// The root a command's `ProfileStore(args.profile_dir)` opens. Its checks
/// run outside the oracle's error handler (a traceback): every failure defers.
pub(super) fn store_root(profile_dir: Option<&str>) -> Result<PathBuf, Fail> {
    let root = match profile_dir {
        Some("") => return Err(Fail::Defer),
        Some(directory) => PathBuf::from(directory),
        None => crate::credentials::expand_user(Path::new("~"))
            .ok()
            .filter(|home| plain_absolute(home))
            .ok_or(Fail::Defer)?
            .join(".pseudolife-mcp")
            .join("tunnel"),
    };
    checked_path(&root).map_err(|_| Fail::Defer)
}

/// `ProfileStore._prepare` on an existing root, then `list()`'s glob.
pub(super) fn list(root: &Path) -> Result<Vec<String>, Fail> {
    let info = fs::metadata(root).map_err(|_| Fail::Defer)?;
    if !info.is_dir() {
        // mkdir(exist_ok=True) raises FileExistsError: not decided here.
        return Err(Fail::Defer);
    }
    owner_only_directory(root, &info)?;
    let mut names = Vec::new();
    let mut irregular = false;
    for entry in fs::read_dir(root).map_err(|_| Fail::Defer)? {
        let entry = entry.map_err(|_| Fail::Defer)?;
        let file_name = entry.file_name();
        let Some(file_name) = file_name.to_str() else {
            return Err(Fail::Defer);
        };
        // Windows globbing folds case (Unicode-wide): only ASCII is decided.
        let matched = if cfg!(windows) {
            if !file_name.is_ascii() {
                return Err(Fail::Defer);
            }
            file_name.to_ascii_lowercase().ends_with(".profile.json")
        } else {
            file_name.ends_with(".profile.json")
        };
        if !matched {
            continue;
        }
        let stem = file_name.strip_suffix(".profile.json").unwrap_or(file_name);
        if !valid_name(stem) {
            return Err(Fail::Tunnel(INVALID_NAME));
        }
        irregular |= !entry.file_type().is_ok_and(|kind| kind.is_file());
        names.push(stem.to_owned());
    }
    if irregular {
        return Err(Fail::Defer);
    }
    names.sort();
    Ok(names)
}

#[cfg(unix)]
pub(super) fn owner_only_directory(_root: &Path, info: &Metadata) -> Result<(), Fail> {
    use std::os::unix::fs::MetadataExt;
    if info.uid() != rustix::process::geteuid().as_raw() || info.mode() & 0o077 != 0 {
        return Err(Fail::Tunnel(NOT_OWNER_ONLY));
    }
    Ok(())
}

#[cfg(windows)]
pub(super) fn owner_only_directory(root: &Path, _info: &Metadata) -> Result<(), Fail> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES,
        FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, READ_CONTROL,
    };
    // _windows_private_directory opens READ_CONTROL with full sharing.
    let directory = OpenOptions::new()
        .access_mode(READ_CONTROL | FILE_READ_ATTRIBUTES)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(root)
        .map_err(|_| Fail::Defer)?;
    match crate::credentials::windows_security::validate(&directory) {
        Ok(_) => Ok(()),
        Err(error) if error.0 == "credential file must be owner-only" => {
            Err(Fail::Tunnel(NOT_OWNER_ONLY))
        }
        // Link-count or handle failures are outside the oracle's ACL check.
        Err(_) => Err(Fail::Defer),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(windows)]
    #[test]
    fn sharing_retry_accepts_a_move_after_transient_contention() {
        let mut attempts = 0;
        let result = sharing_retry(|| {
            attempts += 1;
            if attempts == 1 {
                Err(io::Error::from_raw_os_error(32))
            } else {
                Ok(())
            }
        });
        assert!(result.is_ok());
        assert_eq!(attempts, 2);
    }

    fn payload(text: &str) -> Map<String, Value> {
        serde_json::from_str(text).unwrap()
    }

    fn token() -> String {
        std::env::temp_dir()
            .join("tunnel-validate-token")
            .to_str()
            .unwrap()
            .replace('\\', "\\\\")
    }

    fn check(extra: &str) -> Result<Profile, Fail> {
        let text = format!(
            "{{\"name\": \"dot\", \"daemon_url\": \"http://127.0.0.1:8765\", \"token_file\": \"{}\"{extra}}}",
            token()
        );
        validate(&payload(&text))
    }

    #[test]
    fn validate_checks_in_the_oracle_order() {
        assert!(check("").is_ok());
        let refused = |extra: &str, message: &'static str| {
            assert_eq!(check(extra).unwrap_err(), Fail::Tunnel(message), "{extra}");
        };
        refused(
            ", \"tunnel_id\": \"tunnel_xyz\"",
            "invalid tunnel identifier",
        );
        refused(", \"tunnel_id\": \"\"", "invalid tunnel identifier");
        refused(", \"tunnel_id\": 5", "invalid tunnel identifier");
        refused(
            ", \"tunnel_id\": \"bad\", \"organization_id\": \"bad\"",
            "invalid tunnel identifier",
        );
        refused(
            ", \"organization_id\": \"org-a.b\"",
            "invalid organization identifier",
        );
        refused(", \"state\": \"live\"", "invalid tunnel profile state");
        refused(", \"consent\": 1", "invalid tunnel profile state");
        refused(", \"state\": null", "invalid tunnel profile state");
        refused(
            ", \"state\": \"ready\", \"consent\": true",
            "ready tunnel requires explicit consent and an identifier",
        );
        refused(", \"runtime_version\": \"1.2\"", "invalid runtime version");
        refused(
            ", \"minimum_catalog\": \"all\"",
            "invalid tunnel catalog selection",
        );
        refused(
            ", \"runtime_key_expires_at\": \"2026-01-01T00:00:00\"",
            EXPIRY,
        );
        refused(", \"runtime_key_expires_at\": 5", EXPIRY);
        refused(
            ", \"cloud_verification_file\": \"relative\"",
            "cloud verification reference requires an absolute path",
        );
        assert!(
            check(", \"organization_id\": \"org-A_b-9\", \"tunnel_id\": \"tunnel_0aF\"").is_ok()
        );
        assert_eq!(
            check(", \"runtime_version\": \"\u{661}.2.3\"").unwrap_err(),
            Fail::Defer
        );
    }

    #[test]
    fn missing_or_relative_references_are_refused() {
        let refused = |text: &str, message: &'static str| {
            assert_eq!(validate(&payload(text)).unwrap_err(), Fail::Tunnel(message));
        };
        refused("{\"name\": \"a.b\"}", INVALID_NAME);
        refused(
            "{\"name\": \"dot\", \"daemon_url\": 1}",
            "invalid tunnel endpoint",
        );
        refused(
            "{\"name\": \"dot\", \"daemon_url\": \"http://127.0.0.1\", \"token_file\": \"token\"}",
            "daemon credential reference requires an absolute path",
        );
    }
}
