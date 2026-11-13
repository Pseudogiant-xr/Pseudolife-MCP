//! `pseudolife_memory/program_lookup.py`'s `find_program` for the native
//! CLI, shared by every native command that finds a program the Python
//! command finds with it: a program is found only in an absolute `PATH`
//! directory (never the current directory, never a relative or empty
//! entry), trying `PATHEXT`'s extensions that `CreateProcess` starts on
//! Windows; plus `expose_cli`'s default install location for `tailscale`
//! and `expose_cli._is_ipv4`.
use super::pyjson;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

/// `os.path.join(dir, name)` for a relative `name` with no drive.
fn join(dir: &str, name: &str) -> String {
    let seps: &[char] = if cfg!(windows) { &['\\', '/'] } else { &['/'] };
    if dir.is_empty() || dir.ends_with(seps) {
        format!("{dir}{name}")
    } else {
        format!("{dir}{}{name}", if cfg!(windows) { '\\' } else { '/' })
    }
}

/// `program_lookup.is_program(path)`: a regular file (following links),
/// executable for this user outside Windows.
fn usable(path: &Path) -> bool {
    let Ok(meta) = std::fs::metadata(path) else {
        return false;
    };
    if !meta.is_file() {
        return false;
    }
    #[cfg(unix)]
    if rustix::fs::access(path, rustix::fs::Access::EXEC_OK).is_err() {
        return false;
    }
    true
}

/// `program_lookup.DEFAULT_PATHEXT`.
const DEFAULT_PATHEXT: &str = ".COM;.EXE;.BAT;.CMD";
/// `program_lookup.WINDOWS_LAUNCHABLE`: what `CreateProcess` starts.
const LAUNCHABLE: [&str; 4] = [".com", ".exe", ".bat", ".cmd"];

/// CPython 3.11's `_WindowsFlavour.splitroot` (after `/` became `\`):
/// whether the drive and the root are both present.
fn windows_drive_and_root(entry: &str) -> (String, bool) {
    let part = entry.replace('/', "\\");
    let mut prefix = String::new();
    let mut part = part.as_str().to_owned();
    // `_split_extended_path`.
    if let Some(rest) = part.strip_prefix("\\\\?\\") {
        prefix.push_str("\\\\?\\");
        part = rest.to_owned();
        if let Some(rest) = part.strip_prefix("UNC\\") {
            prefix.push_str("UNC");
            part = format!("\\{rest}");
        }
    }
    let bytes = part.as_bytes();
    let at = |i: usize| bytes.get(i).copied();
    if at(0) == Some(b'\\') && at(1) == Some(b'\\') && at(2) != Some(b'\\') {
        // `\\machine\mountpoint\...`: the drive runs to the separator after
        // the mount point; two separators in a row end the UNC reading.
        if let Some(index) = part[2..].find('\\').map(|i| i + 2) {
            let index2 = part[index + 1..].find('\\').map(|i| i + index + 1);
            if index2 != Some(index + 1) {
                let end = index2.unwrap_or(part.len());
                let drive = if prefix.is_empty() {
                    part[..end].to_owned()
                } else {
                    format!("{prefix}{}", &part[1..end])
                };
                return (drive, true);
            }
        }
    }
    let mut drive = prefix;
    let mut rest = part.as_str();
    if at(1) == Some(b':') && at(0).is_some_and(|b| b.is_ascii_alphabetic()) {
        drive.push_str(&part[..2]);
        rest = &part[2..];
    }
    (drive, rest.starts_with('\\'))
}

/// `Path(entry).is_absolute()`: `Some(answer)`, or `None` for the one
/// spelling whose `os.path.join` this port does not reproduce (a UNC drive
/// that ends in a separator, `\\server\`).
fn absolute(entry: &str) -> Option<bool> {
    if !cfg!(windows) {
        return Some(entry.starts_with('/'));
    }
    let (drive, root) = windows_drive_and_root(entry);
    if drive.is_empty() || !root {
        return Some(false);
    }
    if drive.ends_with('\\') {
        return None;
    }
    Some(true)
}

/// `Path(value).is_absolute()` for a directory setting, `False` where this
/// port does not answer (see [`absolute`]).
#[cfg(windows)]
pub(crate) fn absolute_dir(value: &str) -> bool {
    !value.is_empty() && absolute(value) == Some(true)
}

/// `program_lookup.program_names(name)`: a name that already ends in a
/// launchable extension is that one file.
fn names(name: &str, pathext: Option<OsString>) -> Result<Vec<String>, ()> {
    if !cfg!(windows) {
        return Ok(vec![name.to_owned()]);
    }
    let lower = name.to_lowercase();
    if LAUNCHABLE.iter().any(|ext| lower.ends_with(ext)) {
        return Ok(vec![name.to_owned()]);
    }
    let pathext = pathext
        .map(|value| value.into_string().map_err(|_| ()))
        .transpose()?
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_PATHEXT.to_owned());
    Ok(pathext
        .split(';')
        .filter(|ext| LAUNCHABLE.contains(&ext.to_lowercase().as_str()))
        .map(|ext| format!("{name}{ext}"))
        .collect())
}

/// One `PATH` entry: its text, or (not UTF-8) the raw entry.
enum Entry {
    Text(String),
    Raw(OsString),
}

/// `PATH` split on its separator, entry by entry, so one entry that is not
/// UTF-8 does not hide the others.
fn entries(path: &OsString) -> Vec<Entry> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::{OsStrExt, OsStringExt};
        path.as_bytes()
            .split(|byte| *byte == b':')
            .map(|part| match std::str::from_utf8(part) {
                Ok(text) => Entry::Text(text.to_owned()),
                Err(_) => Entry::Raw(OsString::from_vec(part.to_vec())),
            })
            .collect()
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::{OsStrExt, OsStringExt};
        let wide: Vec<u16> = path.encode_wide().collect();
        wide.split(|unit| *unit == u16::from(b';'))
            .map(|part| match String::from_utf16(part) {
                Ok(text) => Entry::Text(text),
                Err(_) => Entry::Raw(OsString::from_wide(part)),
            })
            .collect()
    }
    #[cfg(not(any(unix, windows)))]
    {
        match path.clone().into_string() {
            Ok(text) => text
                .split(':')
                .map(|part| Entry::Text(part.to_owned()))
                .collect(),
            Err(raw) => vec![Entry::Raw(raw)],
        }
    }
}

/// Where a search found the program: `text` when the oracle's string for it
/// is reproduced (an entry this port classifies), and the path to run.
struct Found {
    text: Option<String>,
    path: PathBuf,
}

/// The search behind [`which`] and [`find_program_path`]. `Err` only for a
/// `PATHEXT` that is not UTF-8.
fn search(
    name: &str,
    path: Option<OsString>,
    pathext: Option<OsString>,
) -> Result<Option<Found>, ()> {
    if name.is_empty() {
        return Ok(None);
    }
    let separators: &[char] = if cfg!(windows) { &['\\', '/'] } else { &['/'] };
    if name.contains(separators) {
        return Ok(usable(Path::new(name)).then(|| Found {
            text: Some(name.to_owned()),
            path: PathBuf::from(name),
        }));
    }
    let Some(path) = path else {
        return Ok(None);
    };
    let names = names(name, pathext)?;
    for entry in entries(&path) {
        match entry {
            Entry::Text(entry) => {
                let dir = pyjson::py_strip(&entry).trim_matches('"');
                if dir.is_empty() {
                    continue;
                }
                let known = absolute(dir);
                if known == Some(false) {
                    continue;
                }
                for file in &names {
                    let candidate = join(dir, file);
                    if usable(Path::new(&candidate)) {
                        let path = PathBuf::from(&candidate);
                        return Ok(Some(Found {
                            text: known.map(|_| candidate),
                            path,
                        }));
                    }
                }
            }
            Entry::Raw(entry) => {
                // Not text: searched only when it is an absolute directory;
                // the string the oracle would report is not reproduced.
                let dir = PathBuf::from(entry);
                if !dir.is_absolute() {
                    continue;
                }
                for file in &names {
                    let candidate = dir.join(file);
                    if usable(&candidate) {
                        return Ok(Some(Found {
                            text: None,
                            path: candidate,
                        }));
                    }
                }
            }
        }
    }
    Ok(None)
}

/// `program_lookup.find_program(name, environ)` with `PATH` and `PATHEXT`
/// given. `Err` defers: a `PATHEXT` that is not UTF-8, or a program found
/// under an entry whose text this port does not reproduce (one that is not
/// UTF-8, or a bare share root such as `\\server\`).
fn which(
    name: &str,
    path: Option<OsString>,
    pathext: Option<OsString>,
) -> Result<Option<String>, ()> {
    match search(name, path, pathext)? {
        None => Ok(None),
        Some(Found {
            text: Some(text), ..
        }) => Ok(Some(text)),
        Some(Found { text: None, .. }) => Err(()),
    }
}

/// The program `find_program` would find, as a path to run, for a caller
/// that does not print it: never defers on an entry whose text the port
/// does not reproduce; `None` when nothing is found (or `PATHEXT` is not
/// UTF-8).
pub(crate) fn find_program_path(name: &str) -> Option<PathBuf> {
    search(name, std::env::var_os("PATH"), std::env::var_os("PATHEXT"))
        .ok()
        .flatten()
        .map(|found| found.path)
}

/// `program_lookup.find_program(name)` over this process's `PATH` and
/// `PATHEXT`: `Ok(None)` when no absolute `PATH` directory holds it, `Err`
/// defers (see [`which`]).
pub(crate) fn find_program(name: &str) -> Result<Option<String>, ()> {
    which(name, std::env::var_os("PATH"), std::env::var_os("PATHEXT"))
}

/// `str(Path(base) / "Tailscale" / "tailscale.exe")` for a base pathlib
/// leaves as written; any other spelling defers.
fn program_files_exe(base: &str) -> Result<String, ()> {
    let plain = base.len() >= 3
        && base.as_bytes()[0].is_ascii_alphabetic()
        && &base[1..3] == ":\\"
        && !base.contains('/')
        && !base[2..].contains("\\\\")
        && !base[3..]
            .split('\\')
            .any(|part| part == "." || part.is_empty() && base.len() > 3);
    if !plain {
        return Err(());
    }
    Ok(join(base, "Tailscale\\tailscale.exe"))
}

/// `default_locations()`'s Windows base: the first of `ProgramW6432` and
/// `ProgramFiles` that is set and absolute (a relative one would resolve
/// against the current directory), else `C:\Program Files`. `Err` defers:
/// a value that is not UTF-8, or one [`absolute`] cannot answer for.
fn program_files_base(values: [Option<OsString>; 2]) -> Result<String, ()> {
    for value in values.into_iter().flatten() {
        let value = value.into_string().map_err(|_| ())?;
        if !value.is_empty() && absolute(&value).ok_or(())? {
            return Ok(value);
        }
    }
    Ok(r"C:\Program Files".to_owned())
}

/// `expose_cli.default_locations()` as a path (joined by `Path`, for a
/// caller that does not print it), when that file exists. `Err`: a
/// Program Files value that is not UTF-8 or [`absolute`] cannot answer for.
pub(crate) fn tailscale_install_location() -> Result<Option<std::path::PathBuf>, ()> {
    let candidate = if cfg!(windows) {
        std::path::PathBuf::from(program_files_base([
            std::env::var_os("ProgramW6432"),
            std::env::var_os("ProgramFiles"),
        ])?)
        .join("Tailscale")
        .join("tailscale.exe")
    } else if cfg!(target_os = "macos") {
        std::path::PathBuf::from("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    } else {
        return Ok(None);
    };
    Ok(candidate.is_file().then_some(candidate))
}

/// `expose_cli.find_tailscale()`: `find_program("tailscale")`, then the
/// default install location. `Ok(None)` when it is nowhere, `Err` defers.
pub(crate) fn find_tailscale() -> Result<Option<String>, ()> {
    if let Some(found) = find_program("tailscale")? {
        return Ok(Some(found));
    }
    let candidate = if cfg!(windows) {
        program_files_exe(&program_files_base([
            std::env::var_os("ProgramW6432"),
            std::env::var_os("ProgramFiles"),
        ])?)?
    } else if cfg!(target_os = "macos") {
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale".to_owned()
    } else {
        return Ok(None);
    };
    Ok(std::fs::metadata(&candidate)
        .is_ok_and(|meta| meta.is_file())
        .then_some(candidate))
}

/// `_is_ipv4(word)`: four dot-separated octets, each `0` or 1 to 3 ASCII
/// digits without a leading zero, at most 255.
pub(crate) fn is_ipv4(word: &str) -> bool {
    let parts: Vec<&str> = word.split('.').collect();
    parts.len() == 4
        && parts.iter().all(|part| {
            (1..=3).contains(&part.len())
                && part.bytes().all(|b| b.is_ascii_digit())
                && (*part == "0" || !part.starts_with('0'))
                && part.parse::<u16>().is_ok_and(|octet| octet <= 255)
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_lookup_joins_as_os_path_join() {
        if cfg!(windows) {
            assert_eq!(join(r"C:\x", "t.CMD"), r"C:\x\t.CMD");
            assert_eq!(join("C:/x/", "t"), "C:/x/t");
            assert_eq!(join(r"C:\x/bin", "t"), r"C:\x/bin\t");
            assert_eq!(
                program_files_exe(r"C:\Program Files"),
                Ok(r"C:\Program Files\Tailscale\tailscale.exe".into())
            );
            assert_eq!(
                program_files_exe(r"C:\"),
                Ok(r"C:\Tailscale\tailscale.exe".into())
            );
            for odd in [
                "C:/Program Files",
                r"C:\a\\b",
                r"C:\a\",
                r"C:\.\a",
                "relative",
                r"\\server\share",
            ] {
                assert_eq!(program_files_exe(odd), Err(()), "{odd}");
            }
        } else {
            assert_eq!(join("/x", "t"), "/x/t");
            assert_eq!(join("/x/", "t"), "/x/t");
        }
        // An empty or unset PATH finds nothing without touching the disk.
        assert_eq!(which("tailscale", Some(OsString::new()), None), Ok(None));
        assert_eq!(which("tailscale", None, None), Ok(None));
    }

    /// A directory of its own under the system temporary directory.
    fn scratch(tag: &str) -> std::path::PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.as_nanos());
        let dir = std::env::temp_dir().join(format!(
            "expose-lookup-{tag}-{}-{nanos}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A stand-in program file (never run: the lookup only finds it).
    fn plant(dir: &std::path::Path) -> String {
        let path = dir.join(if cfg!(windows) {
            "tailscale.cmd"
        } else {
            "tailscale"
        });
        std::fs::write(&path, "exit 99\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        path.to_str().unwrap().to_owned()
    }

    fn path_of(entries: &[&str]) -> Option<OsString> {
        Some(OsString::from(entries.join(if cfg!(windows) {
            ";"
        } else {
            ":"
        })))
    }

    #[test]
    fn relative_and_empty_path_entries_are_never_searched() {
        let dir = scratch("relative");
        let real = dir.join("real");
        std::fs::create_dir_all(&real).unwrap();
        let found = plant(&real);
        let real = real.to_str().unwrap();
        let pathext = Some(OsString::from(".EXE;.CMD"));
        // Relative entries name a directory under the test's current one,
        // which holds no stand-in: skipping them is what is checked.
        for entry in ["", ".", "bin", "\"\"", "  "] {
            assert_eq!(
                which("tailscale", path_of(&[entry]), pathext.clone()),
                Ok(None),
                "{entry:?}"
            );
            let got = which("tailscale", path_of(&[entry, real]), pathext.clone())
                .unwrap()
                .unwrap();
            assert!(got.eq_ignore_ascii_case(&found), "{entry:?}: {got}");
        }
        if cfg!(windows) {
            for entry in ["C:bin", r"\bin", "/bin"] {
                assert_eq!(absolute(entry), Some(false), "{entry}");
            }
            assert_eq!(absolute("C:/x"), Some(true));
            // A relative Program Files base is passed over.
            let base = |a: &str, b: &str| program_files_base([Some(a.into()), Some(b.into())]);
            assert_eq!(base("Program Files", r"D:\pf"), Ok(r"D:\pf".into()));
            assert_eq!(base("", "C:pf"), Ok(r"C:\Program Files".into()));
            assert_eq!(base(r"E:\pf64", r"D:\pf"), Ok(r"E:\pf64".into()));
            assert_eq!(base(r"\\server\pf", r"D:\pf"), Ok(r"\\server\pf".into()));
            assert_eq!(base(r"\\server\", r"D:\pf"), Err(()));
            // CPython 3.11 `PureWindowsPath(entry).is_absolute()`, probed on
            // 3.11.9 (2026-11-14); `\\server\` is the one deferred spelling.
            for (entry, expected) in [
                (r"\\server\share", Some(true)),
                (r"\\server\share\", Some(true)),
                (r"\\server\share\bin", Some(true)),
                (r"\\server", Some(false)),
                (r"\\server\", None),
                ("//server/share/bin", Some(true)),
                (r"\\?\C:\bin", Some(true)),
                (r"\\?\C:", Some(false)),
                (r"\\.\C:\bin", Some(true)),
                (r"\\?\UNC\s\sh\b", Some(true)),
                (r"\\\x", Some(false)),
                (r"\\\x\y", Some(false)),
                ("//", Some(false)),
                (r"\\\\", Some(false)),
                (r"\\s\\sh", Some(false)),
                (r"\\s//sh/x", Some(false)),
                (r"\\s\sh\\x", Some(true)),
                ("C:/x", Some(true)),
                (r"C:\", Some(true)),
                ("c:x", Some(false)),
                (r"\x", Some(false)),
                (r"C:\a\..\b", Some(true)),
            ] {
                assert_eq!(absolute(entry), expected, "{entry}");
            }
            // Quotes and space around an entry are removed; the extension
            // is spelled as PATHEXT spells it.
            let quoted = format!(" \"{real}\" ");
            assert_eq!(
                which("tailscale", path_of(&[&quoted]), pathext.clone()),
                Ok(Some(format!(r"{real}\tailscale.CMD")))
            );
            // Only what CreateProcess starts.
            std::fs::write(dir.join("real").join("tailscale.js"), "").unwrap();
            assert_eq!(
                which("tailscale", path_of(&[real]), Some(".JS".into())),
                Ok(None)
            );
        } else {
            assert_eq!(absolute("bin"), Some(false));
            assert_eq!(absolute("/bin"), Some(true));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// An entry that is not UTF-8 does not hide the others: the program in a
    /// later absolute entry is still found, and one found under the
    /// unreadable entry itself defers (`which`) or is returned as a path
    /// (`find_program_path`).
    #[test]
    fn an_entry_that_is_not_text_does_not_hide_the_others() {
        let dir = scratch("raw-entry");
        let real = dir.join("real");
        std::fs::create_dir_all(&real).unwrap();
        let found = plant(&real);
        let pathext = Some(OsString::from(".EXE;.CMD"));
        #[cfg(unix)]
        let raw = {
            use std::os::unix::ffi::OsStringExt;
            let mut bytes = b"/nonexistent-\xff:".to_vec();
            bytes.extend_from_slice(real.to_str().unwrap().as_bytes());
            OsString::from_vec(bytes)
        };
        #[cfg(windows)]
        let raw = {
            use std::os::windows::ffi::OsStringExt;
            let mut wide: Vec<u16> = r"C:\nonexistent-".encode_utf16().collect();
            wide.push(0xD800);
            wide.push(u16::from(b';'));
            wide.extend(real.to_str().unwrap().encode_utf16());
            OsString::from_wide(&wide)
        };
        let got = which("tailscale", Some(raw.clone()), pathext.clone())
            .unwrap()
            .unwrap();
        assert!(got.eq_ignore_ascii_case(&found), "{got}");
        let path = search("tailscale", Some(raw), pathext)
            .unwrap()
            .unwrap()
            .path;
        assert!(path.to_str().unwrap().eq_ignore_ascii_case(&found));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// `find_program`'s name rules: any program name, a name that already
    /// carries a launchable extension tried as it is, a name with a
    /// separator taken as a path and never searched for.
    #[test]
    fn any_name_is_found_by_the_same_rules() {
        let dir = scratch("names");
        let real = dir.join("real");
        std::fs::create_dir_all(&real).unwrap();
        let file = if cfg!(windows) {
            "pg_dump.exe"
        } else {
            "pg_dump"
        };
        let planted = real.join(file);
        std::fs::write(&planted, "not run\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&planted, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let planted = planted.to_str().unwrap().to_owned();
        let real = real.to_str().unwrap();
        for entry in ["", ".", "bin"] {
            assert_eq!(
                which("pg_dump", path_of(&[entry]), None),
                Ok(None),
                "{entry:?}"
            );
        }
        let got = which("pg_dump", path_of(&["", ".", real]), None)
            .unwrap()
            .unwrap();
        assert!(got.eq_ignore_ascii_case(&planted), "{got}");
        if cfg!(windows) {
            // Named with its extension: that file, nothing added.
            assert_eq!(
                which("pg_dump.exe", path_of(&[real]), None),
                Ok(Some(format!(r"{real}\pg_dump.exe")))
            );
            assert_eq!(which("pg_dump.exe", path_of(&["."]), None), Ok(None));
            // A script type PATHEXT lists is not tried as it is.
            std::fs::write(dir.join("real").join("pg_dump.js"), "").unwrap();
            assert_eq!(which("pg_dump.js", path_of(&[real]), None), Ok(None));
        }
        // A path is the caller's choice: returned when it is a program.
        assert_eq!(which(&planted, None, None), Ok(Some(planted.clone())));
        assert_eq!(which(&format!("{planted}-missing"), None, None), Ok(None));
        assert_eq!(which("", path_of(&[real]), None), Ok(None));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
