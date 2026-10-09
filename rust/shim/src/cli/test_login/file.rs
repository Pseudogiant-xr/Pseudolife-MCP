//! The login file: where it lives, how it is shown, read and written
//! (`read_file`, `_write_private`, `_shown` and `Path.home()`).
use std::{
    collections::HashMap,
    fs,
    io::{self, Write},
    path::{Component, Path, PathBuf},
};

/// Python's `str.isspace()`, which `str.strip()` and `re`'s `\s` use.
pub(super) fn py_space(c: char) -> bool {
    matches!(
        c,
        '\t' | '\n'
            | '\u{b}'
            | '\u{c}'
            | '\r'
            | '\u{1c}'..='\u{1f}'
                | ' '
                | '\u{85}'
                | '\u{a0}'
                | '\u{1680}'
                | '\u{2000}'..='\u{200a}'
                | '\u{2028}'
                | '\u{2029}'
                | '\u{202f}'
                | '\u{205f}'
                | '\u{3000}'
    )
}

pub(super) fn py_strip(text: &str) -> &str {
    text.trim_matches(py_space)
}

/// Python's `str.splitlines()`.
pub(super) fn py_splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((index, c)) = chars.next() {
        let width = match c {
            '\r' if chars.peek().is_some_and(|(_, next)| *next == '\n') => {
                chars.next();
                2
            }
            '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}'
            | '\u{2028}' | '\u{2029}' => c.len_utf8(),
            _ => continue,
        };
        lines.push(&text[start..index]);
        start = index + width;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// `str(Path(value)) == value` with no `..`: such a path prints and joins as
/// written. Every other spelling defers.
#[cfg(not(windows))]
pub(super) fn canonical(value: &str) -> bool {
    let rest = value.strip_prefix('/').unwrap_or(value);
    value == "/"
        || (!rest.is_empty()
            && !rest.starts_with('-')
            && rest
                .split('/')
                .all(|part| !part.is_empty() && part != "." && part != ".."))
}

#[cfg(windows)]
pub(super) fn canonical(value: &str) -> bool {
    let bytes = value.as_bytes();
    let rest = if bytes.len() >= 3 && bytes[0].is_ascii_alphabetic() && &bytes[1..3] == b":\\" {
        if bytes.len() == 3 {
            return true;
        }
        &value[3..]
    } else {
        value
    };
    !rest.is_empty()
        && !rest.starts_with('-')
        && !rest.contains(['/', ':'])
        && rest
            .split('\\')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

/// `Path.home()` as Python 3.11 resolves it, when it prints as written.
pub(super) fn home() -> Option<PathBuf> {
    let home = if cfg!(windows) {
        match std::env::var_os("USERPROFILE") {
            Some(profile) => profile,
            None => {
                let path = std::env::var_os("HOMEPATH")?;
                let mut drive = std::env::var_os("HOMEDRIVE").unwrap_or_default();
                drive.push(path);
                drive
            }
        }
    } else {
        std::env::var_os("HOME").filter(|home| !home.is_empty())?
    };
    let text = home.into_string().ok()?;
    canonical(&text).then(|| PathBuf::from(text))
}

/// `_shown`: `~/` and the POSIX form of the part under the home, else the path.
pub(super) fn shown(path: &Path, home: &Path) -> Option<String> {
    let parts = |path: &Path| -> Option<Vec<String>> {
        path.components()
            .map(|part| part.as_os_str().to_str().map(str::to_owned))
            .collect()
    };
    let (mine, base) = (parts(path)?, parts(home)?);
    let fold = |part: &String| {
        if cfg!(windows) {
            part.to_lowercase()
        } else {
            part.clone()
        }
    };
    let absolute = |path: &Path| path.components().any(|c| matches!(c, Component::RootDir));
    if absolute(path) == absolute(home)
        && mine.len() >= base.len()
        && mine.iter().zip(&base).all(|(a, b)| fold(a) == fold(b))
    {
        let rest = &mine[base.len()..];
        return Some(if rest.is_empty() {
            "~/.".into()
        } else {
            format!("~/{}", rest.join("/"))
        });
    }
    Some(path.to_str()?.to_owned())
}

/// `read_file`: the file's `KEY=value` lines; nothing when it cannot be read.
pub(super) fn read_file(path: &Path) -> HashMap<String, String> {
    let mut values = HashMap::new();
    let Ok(data) = fs::read(path) else {
        return values;
    };
    let data = data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&data);
    let Ok(text) = std::str::from_utf8(data) else {
        return values;
    };
    for line in py_splitlines(text) {
        let line = py_strip(line);
        if line.starts_with('#') || !line.contains('=') {
            continue;
        }
        let (key, value) = line.split_once('=').expect("contains '='");
        values.insert(
            py_strip(key).to_owned(),
            py_strip(value).trim_matches(['\'', '"']).to_owned(),
        );
    }
    values
}

/// Shapes whose write the oracle would report as a failure after it has
/// already connected: an ancestor that is not a directory, a target that is
/// not a regular file (or, on Windows, is read-only), a directory this
/// process cannot add entries to, or names past the platform's limits.
/// Checked before any effect, so they defer.
pub(super) fn writable_shape(path: &Path) -> bool {
    let Some(name) = path.file_name() else {
        return false;
    };
    let name_units = if cfg!(windows) {
        name.to_string_lossy().encode_utf16().count()
    } else {
        name.len()
    };
    // `.<name>.<pid>.new`: ten digits is the widest pid either arm can have.
    if name_units + 16 > 255 {
        return false;
    }
    match fs::symlink_metadata(path) {
        Ok(meta) if !meta.file_type().is_file() => return false,
        Ok(meta) if cfg!(windows) && meta.permissions().readonly() => return false,
        Ok(_) => {}
        Err(error) if error.kind() != io::ErrorKind::NotFound => return false,
        Err(_) => {}
    }
    if cfg!(windows) && !within_max_path(path) {
        return false;
    }
    let mut ancestor = path.parent();
    let mut missing = false;
    while let Some(dir) = ancestor.filter(|dir| !dir.as_os_str().is_empty()) {
        match fs::symlink_metadata(dir) {
            Ok(meta) => return meta.is_dir() && can_add_entries(dir, missing),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                missing = true;
                ancestor = dir.parent();
            }
            Err(_) => return false,
        }
    }
    can_add_entries(Path::new("."), missing)
}

/// Python 3.11 on Windows without long-path support: under MAX_PATH (260)
/// for the staged file, under 248 for a directory CreateDirectoryW makes.
fn within_max_path(path: &Path) -> bool {
    let Ok(absolute) = std::path::absolute(path) else {
        return false;
    };
    let units = |path: &Path| path.as_os_str().to_string_lossy().encode_utf16().count();
    units(&absolute) + 16 < 260 && absolute.parent().is_none_or(|parent| units(parent) < 248)
}

/// Whether this process can create the file (and any missing directory) in
/// `dir`. POSIX asks `access`; Windows creates a probe file the system deletes
/// on close (or a probe directory, removed at once), leaving nothing behind.
fn can_add_entries(dir: &Path, directory: bool) -> bool {
    #[cfg(unix)]
    {
        let _ = directory;
        rustix::fs::access(
            dir,
            rustix::fs::Access::WRITE_OK | rustix::fs::Access::EXEC_OK,
        )
        .is_ok()
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        let probe = dir.join(format!(
            ".pl-test-login-probe-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4().simple()
        ));
        if directory {
            return fs::create_dir(&probe).is_ok() && fs::remove_dir(&probe).is_ok();
        }
        // GENERIC_WRITE | DELETE, FILE_FLAG_DELETE_ON_CLOSE, no sharing.
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .access_mode(0x4000_0000 | 0x0001_0000)
            .custom_flags(0x0400_0000)
            .share_mode(0)
            .open(&probe)
            .is_ok()
    }
}

/// Which Python call raised, which decides the Windows form of `str(OSError)`:
/// `os.open` and file writes go through the C runtime (`[Errno N]`),
/// `os.mkdir`, `os.unlink` and `os.replace` through Win32 (`[WinError N]`).
#[derive(Clone, Copy)]
pub(super) enum Call {
    Crt,
    Win32,
}

/// `str(OSError)` as CPython 3.11 builds it: `[Errno N] strerror`, or for a
/// Win32 call on Windows `[WinError N] message` (trailing dots and spaces
/// dropped), then `: 'file'` and ` -> 'file2'` in `repr` form.
pub(super) fn py_oserror(call: Call, error: &io::Error, files: &[&Path]) -> String {
    let names: Vec<String> = files
        .iter()
        .map(|file| super::super::mode_repr(&file.to_string_lossy()))
        .collect();
    let suffix = match names.as_slice() {
        [] => String::new(),
        [one] => format!(": {one}"),
        [one, two, ..] => format!(": {one} -> {two}"),
    };
    let Some(code) = error.raw_os_error() else {
        return format!("{error}{suffix}");
    };
    let text = error.to_string();
    let message = text
        .strip_suffix(&format!(" (os error {code})"))
        .unwrap_or(&text);
    if cfg!(windows) {
        return match call {
            Call::Crt => {
                let (errno, text) = crt_errno(code);
                format!("[Errno {errno}] {text}{suffix}")
            }
            Call::Win32 => {
                let message = message.trim_end_matches(|c: char| c <= ' ' || c == '.');
                format!("[WinError {code}] {message}{suffix}")
            }
        };
    }
    format!("[Errno {code}] {message}{suffix}")
}

/// The Windows C runtime's `_dosmaperr` (Win32 error to errno) and its
/// `strerror` texts.
fn crt_errno(code: i32) -> (i32, &'static str) {
    let errno = match code {
        2 | 3 | 15 | 18 | 53 | 67 | 161 | 206 => 2,
        4 => 24,
        5 | 16 | 19..=36 | 65 | 82 | 83 | 108 | 132 | 158 => 13,
        6 | 114 | 130 => 9,
        7..=9 | 1816 => 12,
        10 => 7,
        11 | 188..=202 => 8,
        17 => 18,
        80 | 183 => 17,
        89 | 164 | 215 => 11,
        109 => 32,
        112 => 28,
        128 | 129 => 10,
        145 => 41,
        _ => 22,
    };
    let text = match errno {
        2 => "No such file or directory",
        7 => "Arg list too long",
        8 => "Exec format error",
        9 => "Bad file descriptor",
        10 => "No child processes",
        11 => "Resource temporarily unavailable",
        12 => "Not enough space",
        13 => "Permission denied",
        17 => "File exists",
        18 => "Cross-device link",
        24 => "Too many open files",
        28 => "No space left on device",
        32 => "Broken pipe",
        41 => "Directory not empty",
        _ => "Invalid argument",
    };
    (errno, text)
}

/// `Path.mkdir(parents=True, exist_ok=True)`: the missing ancestors first; an
/// existing directory is fine; the error names the level that failed.
fn py_mkdir(dir: &Path) -> Result<(), String> {
    match fs::create_dir(dir) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let parent = dir
                .parent()
                .filter(|parent| !parent.as_os_str().is_empty() && *parent != dir);
            let Some(parent) = parent else {
                return Err(py_oserror(Call::Win32, &error, &[dir]));
            };
            py_mkdir(parent)?;
            match fs::create_dir(dir) {
                Err(error) if !dir.is_dir() => Err(py_oserror(Call::Win32, &error, &[dir])),
                _ => Ok(()),
            }
        }
        Err(_) if dir.is_dir() => Ok(()),
        Err(error) => Err(py_oserror(Call::Win32, &error, &[dir])),
    }
}

/// `PrivateStateError` texts, as `open_private` raises them.
const NOT_REGULAR: &str = "the private state file must be a private regular file";
const NOT_SECURED: &str = "cannot secure the private state file";
const NOT_PRIVATE: &str = "the private state file must be private and owned by the current user";

fn open_private(staged: &Path) -> Result<fs::File, String> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // Read/write sharing only, plus WRITE_DAC | WRITE_OWNER to secure it.
        options.share_mode(3).access_mode(0x0012019f | 0x000c0000);
    }
    let file = options
        .open(staged)
        .map_err(|error| py_oserror(Call::Crt, &error, &[staged]))?;
    let meta = file
        .metadata()
        .map_err(|error| py_oserror(Call::Crt, &error, &[]))?;
    if !meta.is_file() {
        return Err(NOT_REGULAR.into());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        if meta.nlink() != 1 {
            return Err(NOT_REGULAR.into());
        }
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|_| NOT_SECURED.to_owned())?;
        let meta = file
            .metadata()
            .map_err(|error| py_oserror(Call::Crt, &error, &[]))?;
        if meta.uid() != rustix::process::geteuid().as_raw() || meta.mode() & 0o077 != 0 {
            return Err(NOT_PRIVATE.into());
        }
    }
    #[cfg(windows)]
    {
        crate::credentials::windows_security::make_private(&file)
            .map_err(|_| NOT_SECURED.to_owned())?;
        crate::credentials::windows_security::validate(&file)
            .map_err(|_| NOT_PRIVATE.to_owned())?;
    }
    Ok(file)
}

/// `_write_private`: `text` in a new owner-only file beside `path`.
pub(super) fn write_private(path: &Path, text: &str) -> Result<PathBuf, String> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        py_mkdir(parent)?;
    }
    let name = path.file_name().ok_or("no file name")?.to_string_lossy();
    let staged = path.with_file_name(format!(".{name}.{}.new", std::process::id()));
    match fs::remove_file(&staged) {
        Err(error) if error.kind() != io::ErrorKind::NotFound => {
            return Err(py_oserror(Call::Win32, &error, &[&staged]));
        }
        _ => {}
    }
    let mut file = open_private(&staged)?;
    if let Err(error) = file.write_all(text.as_bytes()) {
        drop(file);
        return Err(py_oserror(Call::Crt, &error, &[]));
    }
    Ok(staged)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Each expected text is CPython 3.11's `str(OSError)` for the same call
    /// on the same missing path (captured from the oracle's interpreter).
    #[test]
    fn os_errors_read_as_python_prints_them() {
        let sep = std::path::MAIN_SEPARATOR;
        let (a, b) = (
            PathBuf::from(format!("pl-tl-missing{sep}a")),
            PathBuf::from(format!("pl-tl-missing{sep}b")),
        );
        let rename = fs::rename(&a, &b).unwrap_err();
        let mkdir = fs::create_dir(&a).unwrap_err();
        let open = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&a)
            .unwrap_err();
        let replaced = py_oserror(Call::Win32, &rename, &[&a, &b]);
        let made = py_oserror(Call::Win32, &mkdir, &[&a]);
        let opened = py_oserror(Call::Crt, &open, &[&a]);
        if cfg!(windows) {
            assert_eq!(
                replaced,
                r"[WinError 3] The system cannot find the path specified: 'pl-tl-missing\\a' -> 'pl-tl-missing\\b'"
            );
            assert_eq!(
                made,
                r"[WinError 3] The system cannot find the path specified: 'pl-tl-missing\\a'"
            );
            assert_eq!(
                opened,
                r"[Errno 2] No such file or directory: 'pl-tl-missing\\a'"
            );
            assert_eq!(crt_errno(5), (13, "Permission denied"));
            assert_eq!(crt_errno(80), (17, "File exists"));
            assert_eq!(crt_errno(112), (28, "No space left on device"));
        } else {
            assert_eq!(
                replaced,
                "[Errno 2] No such file or directory: 'pl-tl-missing/a' -> 'pl-tl-missing/b'"
            );
            assert_eq!(
                made,
                "[Errno 2] No such file or directory: 'pl-tl-missing/a'"
            );
            assert_eq!(
                opened,
                "[Errno 2] No such file or directory: 'pl-tl-missing/a'"
            );
        }
        let plain = io::Error::other("not an OS error");
        assert_eq!(py_oserror(Call::Crt, &plain, &[]), "not an OS error");
    }

    #[test]
    fn splitlines_and_strip_follow_python() {
        assert_eq!(
            py_splitlines("a\r\nb\rc\nd\u{2028}e"),
            ["a", "b", "c", "d", "e"]
        );
        assert_eq!(py_splitlines("a\n"), ["a"]);
        assert_eq!(py_splitlines(""), Vec::<&str>::new());
        assert_eq!(py_strip("\u{1c} x \u{3000}"), "x");
    }

    #[test]
    fn the_login_file_is_read_like_the_oracle() {
        let dir = std::env::temp_dir().join(format!("pl-tl-read-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("f.env");
        fs::write(
            &path,
            "\u{feff}# c\r\n A = 'x' \r\nB=\"y\"=z\nnoequals\nA=last\n",
        )
        .unwrap();
        let values = read_file(&path);
        assert_eq!(values.get("A").map(String::as_str), Some("last"));
        assert_eq!(values.get("B").map(String::as_str), Some("y\"=z"));
        assert_eq!(values.len(), 2);
        fs::write(&path, b"A=\xff\n").unwrap();
        assert!(read_file(&path).is_empty());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn shown_paths_are_home_relative_when_under_it() {
        let home = std::env::temp_dir().join("pl-home");
        assert_eq!(
            shown(&home.join(".pseudolife-mcp").join("test-pg.env"), &home).as_deref(),
            Some("~/.pseudolife-mcp/test-pg.env")
        );
        let elsewhere = std::env::temp_dir().join("other").join("x.env");
        assert_eq!(shown(&elsewhere, &home).as_deref(), elsewhere.to_str());
    }
}
