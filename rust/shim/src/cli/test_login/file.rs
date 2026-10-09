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

/// Shapes whose write would fail with an OS error text this port does not
/// reproduce: an ancestor that is not a directory, or a target that is not a
/// regular file. Checked before any effect, so they defer.
pub(super) fn writable_shape(path: &Path) -> bool {
    if path.file_name().is_none() {
        return false;
    }
    match fs::symlink_metadata(path) {
        Ok(meta) if !meta.file_type().is_file() => return false,
        Ok(_) => {}
        Err(error) if error.kind() != io::ErrorKind::NotFound => return false,
        Err(_) => {}
    }
    let mut ancestor = path.parent();
    while let Some(dir) = ancestor.filter(|dir| !dir.as_os_str().is_empty()) {
        match fs::symlink_metadata(dir) {
            Ok(meta) => return meta.is_dir(),
            Err(error) if error.kind() == io::ErrorKind::NotFound => ancestor = dir.parent(),
            Err(_) => return false,
        }
    }
    true
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
    let file = options.open(staged).map_err(|error| error.to_string())?;
    let meta = file.metadata().map_err(|error| error.to_string())?;
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
        let meta = file.metadata().map_err(|error| error.to_string())?;
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
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let name = path.file_name().ok_or("no file name")?.to_string_lossy();
    let staged = path.with_file_name(format!(".{name}.{}.new", std::process::id()));
    match fs::remove_file(&staged) {
        Err(error) if error.kind() != io::ErrorKind::NotFound => return Err(error.to_string()),
        _ => {}
    }
    let mut file = open_private(&staged)?;
    if let Err(error) = file.write_all(text.as_bytes()) {
        drop(file);
        return Err(error.to_string());
    }
    Ok(staged)
}

#[cfg(test)]
mod tests {
    use super::*;

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
