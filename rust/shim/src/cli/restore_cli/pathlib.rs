//! The pathlib operations `DockerBank.restore` applies to the safety
//! directory and the state mounts, as CPython 3.11 performs them:
//! `Path.resolve()` (non-strict `os.path.realpath`, then a `stat` that turns a
//! symlink loop into `RuntimeError`), `exists()`, `parents` and
//! `is_relative_to`. `None` means the path cannot be resolved or tested (an
//! I/O error, a symlink loop, a path that is not Unicode text), and the
//! caller refuses before the first write.
use std::path::{Path, PathBuf};

/// `Path(text).resolve()` as a string; `text` is already pathlib's spelling.
pub(super) fn resolve(path: &Path) -> Option<String> {
    let text = path.to_str()?;
    if text.contains('\0') {
        return None;
    }
    let resolved = platform::realpath(text)?;
    // Path.resolve: a loop surfaces from stat() as RuntimeError.
    if let Err(error) = std::fs::metadata(&resolved)
        && platform::loop_error(&error)
    {
        return None;
    }
    Some(resolved)
}

/// `Path(text).exists()`: `None` where Python's stat error would escape.
pub(super) fn exists(path: &str) -> Option<bool> {
    match std::fs::metadata(path) {
        Ok(_) => Some(true),
        Err(error) if platform::ignored(&error) => Some(false),
        Err(_) => None,
    }
}

/// `(path, *path.parents)` for an absolute, resolved path.
pub(super) fn self_and_parents(path: &str) -> Vec<String> {
    let mut out = vec![path.to_owned()];
    let mut current = PathBuf::from(path);
    while let Some(parent) = current.parent() {
        if parent.as_os_str().is_empty() {
            break;
        }
        out.push(parent.to_str().expect("a parent of a str path").to_owned());
        current = parent.to_path_buf();
    }
    out
}

/// `os.path.join(path, name)` for a resolved directory and a plain name.
pub(super) fn join(path: &str, name: &str) -> String {
    let sep = std::path::MAIN_SEPARATOR;
    if path.ends_with(sep) || (cfg!(windows) && path.ends_with('/')) {
        format!("{path}{name}")
    } else {
        format!("{path}{sep}{name}")
    }
}

/// `PurePath(path).is_relative_to(other)` for two resolved absolute paths:
/// component-wise, case-insensitively on Windows (`str.lower`).
pub(super) fn is_relative_to(path: &str, other: &str) -> bool {
    let parts = |text: &str| -> Vec<String> {
        Path::new(text)
            .components()
            .map(|component| {
                let part = component.as_os_str().to_string_lossy().into_owned();
                if cfg!(windows) {
                    part.to_lowercase()
                } else {
                    part
                }
            })
            .collect()
    };
    let (path, other) = (parts(path), parts(other));
    path.len() >= other.len() && path[..other.len()] == other[..]
}

#[cfg(unix)]
mod platform {
    use std::collections::HashMap;

    pub(super) fn loop_error(error: &std::io::Error) -> bool {
        error.raw_os_error() == Some(libc_eloop())
    }

    fn libc_eloop() -> i32 {
        rustix::io::Errno::LOOP.raw_os_error()
    }

    /// pathlib's `_IGNORED_ERRNOS`: ENOENT, ENOTDIR, EBADF, ELOOP.
    pub(super) fn ignored(error: &std::io::Error) -> bool {
        use rustix::io::Errno;
        error.raw_os_error().is_some_and(|raw| {
            [Errno::NOENT, Errno::NOTDIR, Errno::BADF, Errno::LOOP]
                .iter()
                .any(|errno| errno.raw_os_error() == raw)
        })
    }

    /// `posixpath.join(a, b)`.
    fn join(a: &str, b: &str) -> String {
        if b.starts_with('/') {
            b.to_owned()
        } else if a.is_empty() || a.ends_with('/') {
            format!("{a}{b}")
        } else {
            format!("{a}/{b}")
        }
    }

    /// `posixpath.split(p)`.
    fn split(p: &str) -> (String, String) {
        let cut = p.rfind('/').map_or(0, |i| i + 1);
        let (head, tail) = p.split_at(cut);
        let head = if !head.is_empty() && !head.bytes().all(|b| b == b'/') {
            head.trim_end_matches('/')
        } else {
            head
        };
        (head.to_owned(), tail.to_owned())
    }

    type Seen = HashMap<String, Option<String>>;

    /// `posixpath._joinrealpath(path, rest, strict=False, seen)`; `None`
    /// is an `OSError` from `readlink` or a symlink loop (`ok` False, which
    /// `Path.resolve` then raises as `RuntimeError`).
    fn walk(mut path: String, rest: &str, seen: &mut Seen, depth: usize) -> Option<String> {
        if depth > 200 {
            return None;
        }
        let mut rest = rest.to_owned();
        if let Some(stripped) = rest.strip_prefix('/') {
            rest = stripped.to_owned();
            path = "/".to_owned();
        }
        while !rest.is_empty() {
            let (name, tail) = match rest.find('/') {
                Some(i) => (rest[..i].to_owned(), rest[i + 1..].to_owned()),
                None => (rest.clone(), String::new()),
            };
            rest = tail;
            if name.is_empty() || name == "." {
                continue;
            }
            if name == ".." {
                if path.is_empty() {
                    path = "..".to_owned();
                } else {
                    let (head, last) = split(&path);
                    path = if last == ".." {
                        join(&join(&head, ".."), "..")
                    } else {
                        head
                    };
                }
                continue;
            }
            let next = join(&path, &name);
            let link =
                std::fs::symlink_metadata(&next).is_ok_and(|info| info.file_type().is_symlink());
            if !link {
                path = next;
                continue;
            }
            match seen.get(&next) {
                Some(Some(resolved)) => {
                    path = resolved.clone();
                    continue;
                }
                Some(None) => return None,
                None => {}
            }
            seen.insert(next.clone(), None);
            let target = std::fs::read_link(&next).ok()?;
            path = walk(path, target.to_str()?, seen, depth + 1)?;
            seen.insert(next, Some(path.clone()));
        }
        Some(path)
    }

    /// `os.path.abspath`: join the cwd, then `normpath`.
    fn abspath(path: &str) -> Option<String> {
        let joined = if path.starts_with('/') {
            path.to_owned()
        } else {
            join(std::env::current_dir().ok()?.to_str()?, path)
        };
        let initial = if joined.starts_with("//") && !joined.starts_with("///") {
            2
        } else {
            1
        };
        let mut parts: Vec<&str> = Vec::new();
        for part in joined.split('/') {
            match part {
                "" | "." => {}
                ".." => {
                    parts.pop();
                }
                other => parts.push(other),
            }
        }
        Some(format!("{}{}", "/".repeat(initial), parts.join("/")))
    }

    pub(super) fn realpath(text: &str) -> Option<String> {
        let walked = walk(String::new(), text, &mut Seen::new(), 0)?;
        abspath(&walked)
    }
}

#[cfg(windows)]
mod platform {
    /// ERROR_CANT_RESOLVE_FILENAME, pathlib's loop signal on Windows.
    const CANT_RESOLVE: i32 = 1921;

    pub(super) fn loop_error(error: &std::io::Error) -> bool {
        error.raw_os_error() == Some(CANT_RESOLVE)
    }

    /// pathlib's ignored errors: ENOENT (ERROR_FILE_NOT_FOUND,
    /// ERROR_PATH_NOT_FOUND) and the winerrors 21, 123 and 1921.
    pub(super) fn ignored(error: &std::io::Error) -> bool {
        matches!(error.raw_os_error(), Some(2 | 3 | 21 | 123 | 1921))
    }

    /// `_getfinalpathname_nonstrict`'s errors that stop at a parent.
    const ALLOWED: [i32; 15] = [1, 2, 3, 5, 21, 32, 50, 53, 65, 67, 87, 123, 161, 1920, 1921];

    const RESERVED: [&str; 4] = ["CON", "PRN", "AUX", "NUL"];

    /// A component Win32 would rewrite or treat as a device: this leaf
    /// does not follow those spellings.
    fn unusual(component: &str) -> bool {
        if component.ends_with('.') || component.ends_with(' ') || component.contains(':') {
            return true;
        }
        let stem = component
            .split('.')
            .next()
            .unwrap_or("")
            .trim_end()
            .to_ascii_uppercase();
        RESERVED.contains(&stem.as_str())
            || ((stem.starts_with("COM") || stem.starts_with("LPT"))
                && stem.chars().count() == 4
                && !stem[3..].starts_with('0'))
    }

    /// `ntpath.normpath` for a drive-absolute path or a relative one.
    fn normpath(text: &str) -> Option<(Option<String>, Vec<String>)> {
        let text = text.replace('/', "\\");
        let bytes = text.as_bytes();
        let (drive, rest) =
            if bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic() {
                if bytes.get(2) != Some(&b'\\') {
                    return None; // drive-relative ("C:x")
                }
                (Some(text[..2].to_owned()), &text[3..])
            } else if text.starts_with('\\') {
                return None; // rooted without a drive, UNC or a device path
            } else {
                (None, text.as_str())
            };
        let mut parts: Vec<String> = Vec::new();
        for part in rest.split('\\') {
            match part {
                "" | "." => {}
                ".." => {
                    if drive.is_some() {
                        parts.pop();
                    } else if parts.pop().is_none() {
                        return None; // above the cwd: join then normalizes again
                    }
                }
                other => {
                    if unusual(other) {
                        return None;
                    }
                    parts.push(other.to_owned());
                }
            }
        }
        Some((drive, parts))
    }

    fn canonical(path: &str) -> Result<String, i32> {
        match std::fs::canonicalize(path) {
            Ok(found) => found.to_str().map(str::to_owned).ok_or(-1),
            Err(error) => Err(error.raw_os_error().unwrap_or(-1)),
        }
    }

    pub(super) fn realpath(text: &str) -> Option<String> {
        let (drive, parts) = normpath(text)?;
        let full = match drive {
            Some(drive) => format!("{drive}\\{}", parts.join("\\")),
            None => {
                let cwd = std::env::current_dir().ok()?;
                let cwd = cwd.to_str()?;
                if cwd.starts_with('\\') || normpath(cwd).is_none() {
                    return None;
                }
                if parts.is_empty() {
                    cwd.to_owned()
                } else {
                    super::join(cwd, &parts.join("\\"))
                }
            }
        };
        if full.eq_ignore_ascii_case("nul") {
            return None;
        }
        let (prefixed, initial) = match canonical(&full) {
            Ok(found) => (found, 0),
            Err(code) if ALLOWED.contains(&code) => {
                // Find the longest existing prefix; a reparse point the OS
                // could not open is followed by `_readlink_deep`: defer.
                let mut head = full.clone();
                let mut tail: Vec<String> = Vec::new();
                loop {
                    match canonical(&head) {
                        Ok(found) => {
                            let mut joined = found;
                            for name in tail.iter().rev() {
                                joined = super::join(&joined, name);
                            }
                            break (joined, code);
                        }
                        Err(step) if ALLOWED.contains(&step) => {}
                        Err(_) => return None,
                    }
                    if std::fs::symlink_metadata(&head).is_ok() {
                        return None;
                    }
                    let cut = head.rfind('\\')?;
                    let name = head[cut + 1..].to_owned();
                    if name.is_empty() {
                        return None;
                    }
                    tail.push(name);
                    head.truncate(cut);
                    if head.len() == 2 {
                        head.push('\\'); // the drive's root
                    }
                }
            }
            Err(_) => return None,
        };
        let stripped = prefixed.strip_prefix(r"\\?\")?;
        if stripped.starts_with("UNC\\") {
            return None;
        }
        // realpath keeps the prefix unless the plain spelling resolves the
        // same way (or fails as the original did).
        match canonical(stripped) {
            Ok(found) if found == prefixed => Some(stripped.to_owned()),
            Err(code) if code == initial && initial != 0 => Some(stripped.to_owned()),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch() -> PathBuf {
        let base =
            std::env::temp_dir().join(format!("pl-restore-path-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&base).unwrap();
        base
    }

    #[test]
    fn resolve_keeps_a_missing_tail_under_the_canonical_prefix() {
        let base = scratch();
        let canonical = resolve(&base).unwrap();
        let missing = base.join("absent").join("deeper");
        assert_eq!(
            resolve(&missing).unwrap(),
            join(&join(&canonical, "absent"), "deeper")
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn parents_and_containment_follow_pathlib() {
        let root = if cfg!(windows) { r"C:\a\b" } else { "/a/b" };
        let parents = self_and_parents(root);
        assert_eq!(parents.len(), 3);
        assert!(is_relative_to(root, &parents[1]));
        assert!(is_relative_to(root, root));
        assert!(!is_relative_to(&parents[1], root));
        let sibling = if cfg!(windows) { r"C:\a\bc" } else { "/a/bc" };
        assert!(!is_relative_to(sibling, root));
        if cfg!(windows) {
            assert!(is_relative_to(r"C:\A\B\c", r"c:\a\b"));
        } else {
            assert!(!is_relative_to("/A/B/c", "/a/b"));
        }
    }

    #[test]
    fn exists_ignores_only_pathlibs_errors() {
        let base = scratch();
        assert_eq!(exists(base.to_str().unwrap()), Some(true));
        assert_eq!(exists(base.join("absent").to_str().unwrap()), Some(false));
        std::fs::write(base.join("file"), b"x").unwrap();
        // A component that is a file: ENOTDIR (POSIX) or ERROR_PATH_NOT_FOUND.
        assert_eq!(
            exists(base.join("file").join(".git").to_str().unwrap()),
            Some(false)
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_loop_is_not_resolved() {
        let base = scratch();
        std::os::unix::fs::symlink(base.join("b"), base.join("a")).unwrap();
        std::os::unix::fs::symlink(base.join("a"), base.join("b")).unwrap();
        assert_eq!(resolve(&base.join("a").join("x")), None);
        let _ = std::fs::remove_dir_all(&base);
    }
}
