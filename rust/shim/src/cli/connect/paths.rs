//! Where `runtimes.py` looks for client files, the shim layout, and
//! `_path_forms` (`normcase` of `abspath` and of a non-strict `realpath`).
//!
//! Directory settings must name unambiguous absolute paths. Harmless separator
//! and current-directory spellings are normalized without resolving links.
use super::Refusal;
use std::path::{Path, PathBuf};

pub(in crate::cli) const PACKAGE: &str = "pseudolife-mcp";

/// Read a setting without ever including its value in a decoding error.
pub(in crate::cli) fn env(key: &str) -> Result<Option<String>, Refusal> {
    match std::env::var(key) {
        Ok(value) => Ok(Some(value)),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(std::env::VarError::NotUnicode(_)) => Err(Refusal(format!(
            "{key} is not Unicode; set {key} to a Unicode value"
        ))),
    }
}

/// `env.get(key)` when truthy (set and non-empty).
pub(in crate::cli) fn env_truthy(key: &str) -> Result<Option<String>, Refusal> {
    Ok(env(key)?.filter(|value| !value.is_empty()))
}

/// Keep the absolute-path boundary; normalize only equivalent components.
pub(in crate::cli) fn base(value: &str) -> Result<PathBuf, Refusal> {
    setting("path", value)
}

fn setting(key: &str, value: &str) -> Result<PathBuf, Refusal> {
    let path = Path::new(value);
    if !path.is_absolute() {
        return Err(Refusal(format!(
            "{key} must be an absolute path; set {key} to the full directory path"
        )));
    }
    if value.contains('\0')
        || path
            .components()
            .any(|part| matches!(part, std::path::Component::ParentDir))
    {
        return Err(Refusal(format!(
            "{key} contains a NUL or parent-directory component; set {key} to an absolute path without those components"
        )));
    }
    #[cfg(windows)]
    if !matches!(path.components().next(), Some(std::path::Component::Prefix(prefix)) if matches!(prefix.kind(), std::path::Prefix::Disk(_)))
    {
        return Err(Refusal(format!(
            "{key} is not a local drive-absolute path; set {key} to a path such as C:\\data\\client"
        )));
    }
    Ok(path.components().collect())
}

pub(in crate::cli) fn show(path: &Path) -> Result<String, Refusal> {
    path.to_str().map(str::to_owned).ok_or_else(|| {
        Refusal("a client configuration path is not Unicode; use a Unicode directory name".into())
    })
}

/// `runtimes.home(env)`.
pub(in crate::cli) fn home() -> Result<PathBuf, Refusal> {
    for key in ["USERPROFILE", "HOME"] {
        if let Some(value) = env_truthy(key)? {
            return setting(key, &value);
        }
    }
    Err(Refusal(
        "USERPROFILE and HOME are unset or empty; set HOME to your absolute home directory".into(),
    ))
}

pub(in crate::cli) struct Layout {
    pub root: PathBuf,
    pub launcher: PathBuf,
}

/// `runtimes.default_layout(env)`; `Ok(None)` is its ValueError.
pub(in crate::cli) fn layout() -> Result<Option<Layout>, Refusal> {
    let root = env_truthy("PSEUDOLIFE_SHIM_RUNTIMES")?;
    let launcher = env_truthy("PSEUDOLIFE_SHIM_LAUNCHER")?;
    if let (Some(root), Some(launcher)) = (root, launcher) {
        let launcher = setting("PSEUDOLIFE_SHIM_LAUNCHER", &launcher)?;
        let exe = launcher
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case("exe"));
        if cfg!(windows) != exe {
            return Ok(None);
        }
        return Ok(Some(Layout {
            root: setting("PSEUDOLIFE_SHIM_RUNTIMES", &root)?,
            launcher,
        }));
    }
    let user = home()?;
    if cfg!(windows) {
        let local = match env_truthy("LOCALAPPDATA")? {
            Some(value) => setting("LOCALAPPDATA", &value)?,
            None => user.join("AppData").join("Local"),
        }
        .join(PACKAGE);
        return Ok(Some(Layout {
            root: local.join("runtimes"),
            launcher: local.join("bin").join("pseudolife-mcp.exe"),
        }));
    }
    let data = match env_truthy("XDG_DATA_HOME")? {
        Some(value) => setting("XDG_DATA_HOME", &value)?,
        None => user.join(".local").join("share"),
    }
    .join(PACKAGE);
    Ok(Some(Layout {
        root: data.join("runtimes"),
        launcher: data.join("bin").join("pseudolife-mcp"),
    }))
}

/// `runtimes._claude_config_file(env)`.
pub(in crate::cli) fn claude_config() -> Result<PathBuf, Refusal> {
    Ok(match env_truthy("CLAUDE_CONFIG_DIR")? {
        Some(value) => setting("CLAUDE_CONFIG_DIR", &value)?,
        None => home()?,
    }
    .join(".claude.json"))
}

/// `_settings_row`'s `settings.json`.
pub(in crate::cli) fn claude_settings() -> Result<PathBuf, Refusal> {
    Ok(match env_truthy("CLAUDE_CONFIG_DIR")? {
        Some(value) => setting("CLAUDE_CONFIG_DIR", &value)?,
        None => home()?.join(".claude"),
    }
    .join("settings.json"))
}

/// `runtimes._codex_config_file(env)`; only its existence is consulted.
pub(in crate::cli) fn codex_config() -> Result<PathBuf, Refusal> {
    Ok(match env_truthy("CODEX_HOME")? {
        Some(value) => PathBuf::from(value),
        None => home()?.join(".codex"),
    }
    .join("config.toml"))
}

pub(in crate::cli) fn gemini_settings() -> Result<PathBuf, Refusal> {
    Ok(home()?.join(".gemini").join("settings.json"))
}

/// `runtimes.desktop_config_files(env)`.
pub(in crate::cli) fn desktop_configs() -> Result<Vec<PathBuf>, Refusal> {
    let user = home()?;
    let mut found = Vec::new();
    if cfg!(windows) {
        if let Some(local) = env_truthy("LOCALAPPDATA")? {
            let packages = setting("LOCALAPPDATA", &local)?.join("Packages");
            let mut names = Vec::new();
            if let Ok(entries) = std::fs::read_dir(&packages) {
                for entry in entries.flatten() {
                    let name = entry.file_name();
                    if name
                        .to_string_lossy()
                        .get(..7)
                        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("claude_"))
                    {
                        names.push(name.to_str().ok_or_else(|| Refusal("a Claude package directory under LOCALAPPDATA/Packages is not Unicode; repair that Claude Desktop installation".into()))?.to_owned());
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
            Some(value) => setting("APPDATA", &value)?,
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
        Some(value) => setting("XDG_CONFIG_HOME", &value)?,
        None => user.join(".config"),
    };
    Ok(vec![
        config.join("Claude").join("claude_desktop_config.json"),
    ])
}

/// `os.path.normcase`: Windows lowercases (LCMapStringEx's simple mapping)
/// and turns `/` into `\`.
pub(in crate::cli) fn normcase(text: &str) -> String {
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
pub(in crate::cli) fn abspath(path: &Path) -> Option<PathBuf> {
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

/// Unix paths have no verbatim prefix.
#[cfg(unix)]
pub(super) fn strip_verbatim(path: PathBuf) -> PathBuf {
    path
}

#[cfg(not(unix))]
pub(super) fn strip_verbatim(path: PathBuf) -> PathBuf {
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

/// `os.path.realpath(path)` (non-strict). Windows (`ntpath`) normalizes
/// first and resolves the longest existing prefix; POSIX walks the path
/// one component at a time, so a `..` after a symlink leaves the link's
/// target (`posixpath._joinrealpath`). `None` is an OSError.
pub(in crate::cli) fn realpath(path: &Path) -> Option<PathBuf> {
    #[cfg(unix)]
    {
        posix::realpath(path.to_str()?).map(PathBuf::from)
    }
    #[cfg(not(unix))]
    {
        prefix_realpath(path)
    }
}

#[cfg(unix)]
mod posix {
    use std::collections::HashMap;

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

    /// `posixpath._joinrealpath(path, rest, strict=False, seen)`.
    fn walk(mut path: String, rest: &str, seen: &mut Seen) -> Option<(String, bool)> {
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
            if let Some(known) = seen.get(&next) {
                match known {
                    Some(resolved) => {
                        path = resolved.clone();
                        continue;
                    }
                    None => return Some((join(&next, &rest), false)),
                }
            }
            seen.insert(next.clone(), None);
            let target = std::fs::read_link(&next).ok()?;
            let (resolved, ok) = walk(path, target.to_str()?, seen)?;
            path = resolved;
            if !ok {
                return Some((join(&path, &rest), false));
            }
            seen.insert(next, Some(path.clone()));
        }
        Some((path, true))
    }

    /// `posixpath.realpath(path)`: the walk, then `abspath`.
    pub(in crate::cli) fn realpath(path: &str) -> Option<String> {
        let (resolved, _) = walk(String::new(), path, &mut Seen::new())?;
        let absolute = super::abspath(std::path::Path::new(&resolved))?;
        absolute.to_str().map(str::to_owned)
    }

    #[cfg(test)]
    mod tests {
        #[test]
        fn a_parent_step_after_a_symlink_leaves_the_link_target() {
            let base = std::env::temp_dir().join(format!("connect-real-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir_all(base.join("real").join("sub")).unwrap();
            std::os::unix::fs::symlink(base.join("real").join("sub"), base.join("link")).unwrap();
            let base = std::fs::canonicalize(&base).unwrap();
            let asked = format!("{}/link/../x", base.display());
            assert_eq!(
                super::realpath(&asked).unwrap(),
                format!("{}/real/x", base.display())
            );
            let _ = std::fs::remove_dir_all(&base);
        }
    }
}

#[cfg(not(unix))]
fn prefix_realpath(path: &Path) -> Option<PathBuf> {
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
pub(in crate::cli) fn forms(path: &Path) -> Vec<String> {
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

pub(in crate::cli) fn same_path(a: &Path, b: &Path) -> bool {
    let a = forms(a);
    forms(b).iter().any(|form| a.contains(form))
}

/// `processes_inside`'s test: the image is the root or lies under it.
pub(in crate::cli) fn inside(image: &Path, held: &[String]) -> bool {
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
    fn equivalent_absolute_spellings_keep_the_same_path() {
        let root = if cfg!(windows) { r"C:\data" } else { "/data" };
        assert_eq!(
            base(&format!("{root}/./client//")).unwrap(),
            Path::new(root).join("client")
        );
        assert!(base("relative").is_err());
        assert!(base(&format!("{root}/../client")).is_err());
        if cfg!(windows) {
            assert!(base(r"\\server\share").is_err());
            assert_eq!(normcase(r"C:/A/İb"), r"c:\a\ib");
        }
    }
}
