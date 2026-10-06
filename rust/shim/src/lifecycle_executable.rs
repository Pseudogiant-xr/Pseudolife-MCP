use std::{
    ffi::{OsStr, OsString},
    fs, io,
    path::{Component, Path, PathBuf},
};

/// Resolve a command before a launcher or disposable child changes its environment.
pub fn find_executable(program: impl AsRef<OsStr>) -> io::Result<PathBuf> {
    let cwd = std::env::current_dir().ok();
    let path = std::env::var_os("PATH");
    let paths = search_paths(program.as_ref(), path.as_deref(), cwd.as_deref())?;
    #[cfg(windows)]
    let extensions = &parse_extensions(std::env::var_os("PATHEXT"));
    #[cfg(not(windows))]
    let extensions = &[];
    find_in_paths(paths, extensions)
}

fn not_found() -> io::Error {
    io::Error::new(io::ErrorKind::NotFound, "executable path not found")
}

fn absolute_from(path: PathBuf, cwd: &Path) -> PathBuf {
    if path.is_absolute() {
        path
    } else {
        let mut absolute = cwd.to_path_buf();
        absolute.extend(path.components().skip_while(|c| *c == Component::CurDir));
        absolute
    }
}

fn search_paths(
    program: &OsStr,
    path: Option<&OsStr>,
    cwd: Option<&Path>,
) -> io::Result<Vec<PathBuf>> {
    let program = PathBuf::from(program);
    if program.is_absolute() {
        return Ok(vec![program]);
    }
    if program.components().count() > 1
        && let Some(cwd) = cwd
    {
        return Ok(vec![absolute_from(program, cwd)]);
    }
    let paths = std::env::split_paths(path.ok_or_else(not_found)?).collect::<Vec<_>>();
    if paths.is_empty() {
        return Err(not_found());
    }
    Ok(paths
        .into_iter()
        .filter(|entry| !cfg!(windows) || !entry.as_os_str().is_empty())
        .map(|entry| {
            let mut components = entry.components();
            let entry = if components.next() == Some(Component::Normal(OsStr::new("~"))) {
                std::env::home_dir().map_or(entry.clone(), |mut home| {
                    home.extend(components);
                    home
                })
            } else {
                entry
            };
            let native = entry.has_root() || entry.starts_with("~");
            #[cfg(windows)]
            let native = native || {
                let text = entry.as_os_str().to_string_lossy();
                text.starts_with('\\') || text.as_bytes().get(1) == Some(&b':')
            };
            let entry = match cwd {
                Some(cwd) if !native => absolute_from(entry, cwd),
                _ => entry,
            };
            entry.join(&program)
        })
        .collect())
}

#[cfg(windows)]
fn parse_extensions(value: Option<OsString>) -> Vec<String> {
    value
        .and_then(|value| value.into_string().ok())
        .map(|value| {
            value
                .split(';')
                .filter(|extension| extension.starts_with('.'))
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn find_in_paths(paths: Vec<PathBuf>, extensions: &[String]) -> io::Result<PathBuf> {
    for path in paths {
        if is_executable(&path) {
            return Ok(actual_casing(path));
        }
        let known_extension = path.extension().and_then(OsStr::to_str).is_some_and(|ext| {
            extensions
                .iter()
                .any(|extension| ext.eq_ignore_ascii_case(&extension[1..]))
        });
        if !known_extension {
            for extension in extensions {
                let mut candidate = OsString::from(&path);
                candidate.push(extension);
                let candidate = PathBuf::from(candidate);
                if is_executable(&candidate) {
                    return Ok(actual_casing(candidate));
                }
            }
        }
    }
    Err(not_found())
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    // access uses real IDs and the filesystem's ACLs, rather than guessing from mode bits.
    fs::metadata(path).is_ok_and(|metadata| metadata.is_file())
        && rustix::fs::access(path, rustix::fs::Access::EXEC_OK).is_ok()
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn is_executable(path: &Path) -> bool {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::GetBinaryTypeW;
    if !fs::symlink_metadata(path)
        .is_ok_and(|metadata| metadata.is_file() || metadata.file_type().is_symlink())
    {
        return false;
    }
    if path.extension().is_some() {
        return true;
    }
    let wide = path
        .as_os_str()
        .encode_wide()
        .chain([0])
        .collect::<Vec<_>>();
    let mut binary_type = 0;
    // SAFETY: Both the NUL-terminated input and output buffers live through this call.
    unsafe { GetBinaryTypeW(wide.as_ptr(), &mut binary_type) != 0 }
}

fn actual_casing(path: PathBuf) -> PathBuf {
    #[cfg(windows)]
    if let (Some(parent), Some(name)) = (path.parent(), path.file_name())
        && let Ok(entries) = fs::read_dir(parent)
    {
        for entry in entries.flatten() {
            if entry.file_name().eq_ignore_ascii_case(name) {
                return parent.join(entry.file_name());
            }
        }
    }
    path
}
