//! Where `runtimes.py` looks for client files, the shim layout, and
//! `_path_forms` (`normcase` of `abspath` and of a non-strict `realpath`).
//!
//! Every base directory taken from the environment must already be in the
//! form `str(pathlib.Path(value))` prints (absolute, native separators, no
//! empty, `.` or `..` parts, no trailing separator); anything else defers, so
//! displayed paths never depend on pathlib's normalization rules.
use super::Defer;
use std::path::{Path, PathBuf};

pub(super) const PACKAGE: &str = "pseudolife-mcp";

/// `os.environ.get(key)`, deferring on a value that is not valid Unicode.
pub(super) fn env(key: &str) -> Result<Option<String>, Defer> {
    match std::env::var(key) {
        Ok(value) => Ok(Some(value)),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(std::env::VarError::NotUnicode(_)) => Err(Defer),
    }
}

/// `env.get(key)` when truthy (set and non-empty).
pub(super) fn env_truthy(key: &str) -> Result<Option<String>, Defer> {
    Ok(env(key)?.filter(|value| !value.is_empty()))
}

pub(super) fn canonical(text: &str) -> bool {
    if cfg!(windows) {
        let bytes = text.as_bytes();
        if bytes.len() < 3
            || !bytes[0].is_ascii_alphabetic()
            || bytes[1] != b':'
            || bytes[2] != b'\\'
            || text.contains('/')
        {
            return false;
        }
        let rest = &text[3..];
        rest.is_empty()
            || rest
                .split('\\')
                .all(|part| !part.is_empty() && part != "." && part != "..")
    } else {
        let Some(rest) = text.strip_prefix('/') else {
            return false;
        };
        rest.is_empty()
            || rest
                .split('/')
                .all(|part| !part.is_empty() && part != "." && part != "..")
    }
}

/// A base directory from the environment, admitted only in canonical form.
pub(super) fn base(value: &str) -> Result<PathBuf, Defer> {
    if canonical(value) {
        Ok(PathBuf::from(value))
    } else {
        Err(Defer)
    }
}

pub(super) fn show(path: &Path) -> Result<String, Defer> {
    path.to_str().map(str::to_owned).ok_or(Defer)
}

/// `runtimes.home(env)`.
pub(super) fn home() -> Result<PathBuf, Defer> {
    match env_truthy("USERPROFILE")?.or(env_truthy("HOME")?) {
        Some(value) => base(&value),
        // Path.home() would consult the platform's own records.
        None => Err(Defer),
    }
}

pub(super) struct Layout {
    pub root: PathBuf,
    pub launcher: PathBuf,
}

/// `runtimes.default_layout(env)`; `Ok(None)` is its ValueError.
pub(super) fn layout() -> Result<Option<Layout>, Defer> {
    let root = env_truthy("PSEUDOLIFE_SHIM_RUNTIMES")?;
    let launcher = env_truthy("PSEUDOLIFE_SHIM_LAUNCHER")?;
    if let (Some(root), Some(launcher)) = (root, launcher) {
        let launcher = base(&launcher)?;
        let exe = launcher
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case("exe"));
        if cfg!(windows) != exe {
            return Ok(None);
        }
        return Ok(Some(Layout {
            root: base(&root)?,
            launcher,
        }));
    }
    let user = home()?;
    if cfg!(windows) {
        let local = match env_truthy("LOCALAPPDATA")? {
            Some(value) => base(&value)?,
            None => user.join("AppData").join("Local"),
        }
        .join(PACKAGE);
        return Ok(Some(Layout {
            root: local.join("runtimes"),
            launcher: local.join("bin").join("pseudolife-mcp.exe"),
        }));
    }
    let data = match env_truthy("XDG_DATA_HOME")? {
        Some(value) => base(&value)?,
        None => user.join(".local").join("share"),
    }
    .join(PACKAGE);
    Ok(Some(Layout {
        root: data.join("runtimes"),
        launcher: data.join("bin").join("pseudolife-mcp"),
    }))
}

/// `runtimes._claude_config_file(env)`.
pub(super) fn claude_config() -> Result<PathBuf, Defer> {
    Ok(match env_truthy("CLAUDE_CONFIG_DIR")? {
        Some(value) => base(&value)?,
        None => home()?,
    }
    .join(".claude.json"))
}

/// `_settings_row`'s `settings.json`.
pub(super) fn claude_settings() -> Result<PathBuf, Defer> {
    Ok(match env_truthy("CLAUDE_CONFIG_DIR")? {
        Some(value) => base(&value)?,
        None => home()?.join(".claude"),
    }
    .join("settings.json"))
}

/// `runtimes._codex_config_file(env)`; only its existence is consulted.
pub(super) fn codex_config() -> Result<PathBuf, Defer> {
    Ok(match env_truthy("CODEX_HOME")? {
        Some(value) => PathBuf::from(value),
        None => home()?.join(".codex"),
    }
    .join("config.toml"))
}

pub(super) fn gemini_settings() -> Result<PathBuf, Defer> {
    Ok(home()?.join(".gemini").join("settings.json"))
}

/// `runtimes.desktop_config_files(env)`.
pub(super) fn desktop_configs() -> Result<Vec<PathBuf>, Defer> {
    let user = home()?;
    let mut found = Vec::new();
    if cfg!(windows) {
        if let Some(local) = env_truthy("LOCALAPPDATA")? {
            let packages = base(&local)?.join("Packages");
            let mut names = Vec::new();
            if let Ok(entries) = std::fs::read_dir(&packages) {
                for entry in entries.flatten() {
                    let name = entry.file_name();
                    let name = name.to_str().ok_or(Defer)?.to_owned();
                    if name.len() >= 7
                        && name.is_char_boundary(7)
                        && name[..7].eq_ignore_ascii_case("claude_")
                    {
                        names.push(name);
                    }
                }
            }
            names.sort_by_key(|name| name.to_lowercase());
            for name in names {
                found.push(
                    packages
                        .join(name)
                        .join("LocalCache")
                        .join("Roaming")
                        .join("Claude")
                        .join("claude_desktop_config.json"),
                );
            }
        }
        let appdata = match env_truthy("APPDATA")? {
            Some(value) => base(&value)?,
            None => user.join("AppData").join("Roaming"),
        };
        found.push(appdata.join("Claude").join("claude_desktop_config.json"));
        return Ok(found);
    }
    if cfg!(target_os = "macos") {
        return Ok(vec![
            user.join("Library")
                .join("Application Support")
                .join("Claude")
                .join("claude_desktop_config.json"),
        ]);
    }
    let config = match env_truthy("XDG_CONFIG_HOME")? {
        Some(value) => base(&value)?,
        None => user.join(".config"),
    };
    Ok(vec![
        config.join("Claude").join("claude_desktop_config.json"),
    ])
}

/// `os.path.normcase`: Windows lowercases (LCMapStringEx's simple mapping)
/// and turns `/` into `\`.
pub(super) fn normcase(text: &str) -> String {
    if !cfg!(windows) {
        return text.to_owned();
    }
    text.chars()
        .map(|c| match c {
            '/' => '\\',
            '\u{130}' => 'i',
            c => {
                let mut lower = c.to_lowercase();
                match (lower.next(), lower.next()) {
                    (Some(single), None) => single,
                    _ => c,
                }
            }
        })
        .collect()
}

/// `os.path.abspath`.
pub(super) fn abspath(path: &Path) -> Option<PathBuf> {
    let absolute = std::path::absolute(path).ok()?;
    let mut clean = PathBuf::new();
    for component in absolute.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                clean.pop();
            }
            other => clean.push(other.as_os_str()),
        }
    }
    Some(clean)
}

fn strip_verbatim(path: PathBuf) -> PathBuf {
    let Some(text) = path.to_str() else {
        return path;
    };
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        return PathBuf::from(rest);
    }
    path
}

/// `os.path.realpath(path)` (non-strict): the longest existing prefix
/// resolved, the rest appended as written.
pub(super) fn realpath(path: &Path) -> Option<PathBuf> {
    let absolute = abspath(path)?;
    let mut head = absolute.as_path();
    let mut tail: Vec<&std::ffi::OsStr> = Vec::new();
    loop {
        if let Ok(resolved) = std::fs::canonicalize(head) {
            let mut resolved = strip_verbatim(resolved);
            for part in tail.iter().rev() {
                resolved.push(part);
            }
            return Some(resolved);
        }
        let name = head.file_name()?;
        tail.push(name);
        head = head.parent()?;
    }
}

/// `runtimes._path_forms`.
pub(super) fn forms(path: &Path) -> Vec<String> {
    let mut found = Vec::new();
    for form in [abspath(path), realpath(path)].into_iter().flatten() {
        if let Some(text) = form.to_str() {
            let text = normcase(text);
            if !found.contains(&text) {
                found.push(text);
            }
        }
    }
    found
}

pub(super) fn same_path(a: &Path, b: &Path) -> bool {
    let a = forms(a);
    forms(b).iter().any(|form| a.contains(form))
}

/// `processes_inside`'s test: the image is the root or lies under it.
pub(super) fn inside(image: &Path, held: &[String]) -> bool {
    let sep = std::path::MAIN_SEPARATOR;
    forms(image).iter().any(|form| {
        held.iter().any(|root| {
            let prefix = format!("{}{sep}", root.trim_end_matches(sep));
            form == root || form.starts_with(&prefix)
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_forms_only() {
        if cfg!(windows) {
            assert!(canonical(r"C:\Users\x"));
            assert!(canonical(r"C:\"));
            assert!(!canonical(r"C:\Users\x\"));
            assert!(!canonical("C:/Users/x"));
            assert!(!canonical(r"C:\Users\.\x"));
            assert!(!canonical(r"\\server\share"));
            assert_eq!(normcase(r"C:/A/İb"), r"c:\a\ib");
        } else {
            assert!(canonical("/home/x"));
            assert!(!canonical("/home//x"));
            assert!(!canonical("home/x"));
        }
    }
}
