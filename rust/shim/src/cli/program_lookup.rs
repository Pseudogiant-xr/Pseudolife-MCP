//! `pseudolife_memory/program_lookup.py`'s `find_program` for the native
//! CLI, and the reads `expose` and `invite` share through it: a program is
//! found only in an absolute `PATH` directory (never the current directory,
//! never a relative or empty entry), trying `PATHEXT`'s extensions that
//! `CreateProcess` starts on Windows; then `expose_cli`'s default install
//! location for `tailscale`. Also `expose_cli._is_ipv4`.
use super::pyjson;
use std::ffi::OsString;

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
fn usable(path: &str) -> bool {
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

/// `Path(entry).is_absolute()`: `Some(answer)`, or `None` for a spelling
/// this port does not classify as pathlib does (Windows UNC and device
/// paths, which start with two separators).
fn absolute(entry: &str) -> Option<bool> {
    if !cfg!(windows) {
        return Some(entry.starts_with('/'));
    }
    let sep = |b: u8| b == b'\\' || b == b'/';
    let bytes = entry.as_bytes();
    if bytes.len() >= 2 && sep(bytes[0]) && sep(bytes[1]) {
        return None;
    }
    // A drive is an ASCII letter and a colon; it is absolute with a root.
    Some(bytes.len() >= 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && sep(bytes[2]))
}

/// `program_lookup.find_program("tailscale")`: only absolute `PATH`
/// directories, never the current directory. `Err` defers: a `PATH` or
/// `PATHEXT` that is not UTF-8, or a program found under an entry
/// [`absolute`] cannot classify.
fn which(path: Option<OsString>, pathext: Option<OsString>) -> Result<Option<String>, ()> {
    let Some(path) = path else {
        return Ok(None);
    };
    let path = path.into_string().map_err(|_| ())?;
    let separator = if cfg!(windows) { ';' } else { ':' };
    let names: Vec<String> = if cfg!(windows) {
        let pathext = pathext
            .map(|value| value.into_string().map_err(|_| ()))
            .transpose()?
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| DEFAULT_PATHEXT.to_owned());
        pathext
            .split(';')
            .filter(|ext| LAUNCHABLE.contains(&ext.to_lowercase().as_str()))
            .map(|ext| format!("tailscale{ext}"))
            .collect()
    } else {
        vec!["tailscale".to_owned()]
    };
    for entry in path.split(separator) {
        let dir = pyjson::py_strip(entry).trim_matches('"');
        if dir.is_empty() {
            continue;
        }
        let known = absolute(dir);
        if known == Some(false) {
            continue;
        }
        for name in &names {
            let candidate = join(dir, name);
            if usable(&candidate) {
                return known.map(|_| Some(candidate)).ok_or(());
            }
        }
    }
    Ok(None)
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
/// a value that is not UTF-8, or one [`absolute`] cannot classify.
fn program_files_base(values: [Option<OsString>; 2]) -> Result<String, ()> {
    for value in values.into_iter().flatten() {
        let value = value.into_string().map_err(|_| ())?;
        if !value.is_empty() && absolute(&value).ok_or(())? {
            return Ok(value);
        }
    }
    Ok(r"C:\Program Files".to_owned())
}

/// `expose_cli.find_tailscale()`: `program_lookup.find_program("tailscale")`,
/// then the default install location. `Ok(None)` when it is nowhere, `Err`
/// defers.
pub(super) fn find_tailscale() -> Result<Option<String>, ()> {
    if let Some(found) = which(std::env::var_os("PATH"), std::env::var_os("PATHEXT"))? {
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
pub(super) fn is_ipv4(word: &str) -> bool {
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
        assert_eq!(which(Some(OsString::new()), None), Ok(None));
        assert_eq!(which(None, None), Ok(None));
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
                which(path_of(&[entry]), pathext.clone()),
                Ok(None),
                "{entry:?}"
            );
            let got = which(path_of(&[entry, real]), pathext.clone())
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
            assert_eq!(base(r"\\server\pf", r"D:\pf"), Err(()));
            assert_eq!(absolute(r"\\server\share"), None);
            assert_eq!(absolute("//server/share"), None);
            // Quotes and space around an entry are removed; the extension
            // is spelled as PATHEXT spells it.
            let quoted = format!(" \"{real}\" ");
            assert_eq!(
                which(path_of(&[&quoted]), pathext.clone()),
                Ok(Some(format!(r"{real}\tailscale.CMD")))
            );
            // Only what CreateProcess starts.
            std::fs::write(dir.join("real").join("tailscale.js"), "").unwrap();
            assert_eq!(which(path_of(&[real]), Some(".JS".into())), Ok(None));
        } else {
            assert_eq!(absolute("bin"), Some(false));
            assert_eq!(absolute("/bin"), Some(true));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
