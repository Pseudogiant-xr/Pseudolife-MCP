//! The directories `document_ingest` reads from.
//!
//! An ingest reads a file on the daemon's own filesystem, so it is limited
//! to the documents directories its operator configured: the config key
//! `memory.reference.ingest_roots` (a list) together with the environment
//! variable `PSEUDOLIFE_DAEMON_DOCUMENT_ROOTS` (a path list, as `PATH` is
//! written), which the shipped Compose overlays set to the read-only mount
//! `/documents`. With neither set, every ingest is refused.
//!
//! [`admit`] decides one request: a path the roots do not contain, read
//! lexically, is refused before anything on disk is looked at, then the
//! path is resolved (symbolic links followed, `..` applied) and must still
//! lie inside a root, itself resolved. A path outside answers the same
//! refusal whether or not it exists. `/proc`, `/sys` and `/dev` are never
//! read: a root there (or `/`, which holds them) is refused when the
//! configuration loads, and a root that resolves there (a link) is inert.

use std::path::{Component, Path, PathBuf};

use serde_json::Value;

use crate::mcp::dispatch::ToolError;

/// The config key naming the documents directories.
pub(crate) const ROOTS_KEY: &str = "memory.reference.ingest_roots";
/// The environment variable naming more of them (the Docker mount).
pub(crate) const ROOTS_ENV: &str = "PSEUDOLIFE_DAEMON_DOCUMENT_ROOTS";

/// The refusal codes, for callers and tests.
pub(crate) const OUTSIDE: &str = "document_outside_roots";
pub(crate) const NONE_CONFIGURED: &str = "no_document_roots";
pub(crate) const NOT_ABSOLUTE: &str = "document_path_not_absolute";
pub(crate) const UNREADABLE: &str = "document_unreadable";
pub(crate) const DOTDOT: &str = "document_path_dotdot";

/// The kernel's pseudo-filesystems: their files describe the running
/// process and the machine (its environment, memory, devices), never a
/// document.
#[cfg(unix)]
const PSEUDO: &[&str] = &["/proc", "/sys", "/dev"];

/// Whether `path` (absolute, normalized) is a pseudo-filesystem or below
/// one, or `/`, which holds them all.
#[cfg(unix)]
fn pseudo_or_above(path: &Path) -> bool {
    path == Path::new("/") || PSEUDO.iter().any(|p| path.starts_with(p))
}

#[cfg(not(unix))]
fn pseudo_or_above(_: &Path) -> bool {
    false
}

/// `path` with `.` dropped and each `..` removing the component before it,
/// without looking at the disk (a `..` above the root stays at the root).
fn lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if !matches!(
                    out.components().next_back(),
                    None | Some(Component::RootDir | Component::Prefix(_))
                ) {
                    out.pop();
                }
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// A path in a form two spellings of one location compare equal in: on
/// Windows without the `\\?\` verbatim prefix `canonicalize` adds, and
/// lower-cased, since its file names compare without case.
#[cfg(windows)]
fn comparable(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    let plain = if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        format!(r"\\{rest}")
    } else if let Some(rest) = text.strip_prefix(r"\\?\") {
        rest.to_string()
    } else {
        text.into_owned()
    };
    PathBuf::from(plain.to_lowercase())
}

#[cfg(not(windows))]
fn comparable(path: &Path) -> PathBuf {
    path.to_path_buf()
}

/// Whether `path` lies in `root` (or is it), component by component.
/// For the lexical pre-check only; resolved paths use [`resolved_within`].
fn within(path: &Path, root: &Path) -> bool {
    comparable(path).starts_with(comparable(root))
}

/// Whether the resolved `path` lies in the resolved `root`, compared
/// exactly: `canonicalize` spells both as they are on disk, so no case is
/// folded (a case-sensitive Windows directory keeps `DOCS` apart from
/// `docs`).
fn resolved_within(path: &Path, root: &Path) -> bool {
    path.starts_with(root)
}

/// Why `root` cannot be a documents directory, or `None`: it must be an
/// absolute path, never the root of a filesystem (`/`, a whole drive) and
/// never a pseudo-filesystem.
pub(crate) fn root_problem(root: &str) -> Option<String> {
    let path = Path::new(root);
    if root.is_empty() || !path.is_absolute() {
        return Some(format!(
            "{root:?} is not an absolute path; name each documents directory in full"
        ));
    }
    if root.contains('\0') {
        return Some(format!("{root:?} contains a NUL character"));
    }
    if pseudo_or_above(&lexical(path)) {
        return Some(format!(
            "{root:?} is or contains /proc, /sys or /dev, which document_ingest never reads; \
             name a directory that holds only documents"
        ));
    }
    if lexical(path).parent().is_none() {
        return Some(format!(
            "{root:?} is the root of a filesystem; name a directory that holds only documents"
        ));
    }
    None
}

/// The roots `PSEUDOLIFE_DAEMON_DOCUMENT_ROOTS` names (empty entries
/// skipped), or why one cannot be a root.
pub(crate) fn env_roots(value: Option<&str>) -> Result<Vec<String>, String> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let mut roots = Vec::new();
    for entry in std::env::split_paths(value) {
        let text = entry.to_string_lossy().into_owned();
        if text.is_empty() {
            continue;
        }
        if let Some(problem) = root_problem(&text) {
            return Err(format!("{ROOTS_ENV}: {problem}"));
        }
        roots.push(text);
    }
    Ok(roots)
}

/// The effective roots: the environment's, then the config key's, each
/// once.
pub(crate) fn configured(
    config_roots: &[String],
    env_value: Option<&str>,
) -> Result<Vec<String>, String> {
    let mut roots = env_roots(env_value)?;
    for root in config_roots {
        if let Some(problem) = root_problem(root) {
            return Err(format!("{ROOTS_KEY}: {problem}"));
        }
        if !roots.contains(root) {
            roots.push(root.clone());
        }
    }
    Ok(roots)
}

/// The startup check: a root the configuration or the environment names
/// that can never be one refuses the start.
pub(crate) fn startup_refusal(config_roots: &[String]) -> Option<String> {
    env_value()
        .and_then(|env| configured(config_roots, env.as_deref()))
        .err()
}

/// The environment variable as the daemon reads it at startup: unset, its
/// value, or why it cannot be read (it must be UTF-8).
pub(crate) fn env_value() -> Result<Option<String>, String> {
    match std::env::var(ROOTS_ENV) {
        Ok(value) => Ok(Some(value)),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(std::env::VarError::NotUnicode(_)) => Err(format!(
            "{ROOTS_ENV} is not valid UTF-8; name each documents directory in UTF-8"
        )),
    }
}

/// One configured root that is usable now: as written (normalized) and
/// resolved.
struct Root {
    written: PathBuf,
    resolved: PathBuf,
}

/// The roots that resolve to a real directory, and the ones that do not.
/// A root that is missing, not a directory, or resolves into a
/// pseudo-filesystem or a filesystem's root (a link to `/proc` or `/`) is
/// inert, not an error: a stack without the documents mount still starts.
fn usable(roots: &[String]) -> (Vec<Root>, Vec<String>) {
    let mut ok = Vec::new();
    let mut inert = Vec::new();
    for root in roots {
        match std::fs::canonicalize(root) {
            Ok(resolved)
                if resolved.is_dir()
                    && !pseudo_or_above(&resolved)
                    && resolved.parent().is_some() =>
            {
                ok.push(Root {
                    written: lexical(Path::new(root)),
                    resolved,
                })
            }
            _ => inert.push(root.clone()),
        }
    }
    (ok, inert)
}

/// The roots, listed for a message.
fn listed(roots: &[String]) -> String {
    roots.join(", ")
}

fn refusal(code: &str, detail: String) -> ToolError {
    ToolError::Refused {
        code: code.to_string(),
        detail: Some(detail),
        param: Some(Value::from("path")),
        accepted: None,
        action: None,
    }
}

fn none_configured() -> ToolError {
    refusal(
        NONE_CONFIGURED,
        format!(
            "document_ingest reads only from documents directories, and none is configured; \
             with the Docker install, run `pseudolife-mcp update` with the native client, which \
             mounts the documents folder ~/.pseudolife-mcp/documents at /documents; otherwise \
             add a directory to {ROOTS_KEY} in config.yaml (or {ROOTS_ENV}) and restart the \
             daemon, then copy the file there"
        ),
    )
}

fn none_usable(inert: &[String]) -> ToolError {
    refusal(
        NONE_CONFIGURED,
        format!(
            "document_ingest reads only from documents directories, and none of the \
             configured ones is a directory on the daemon's filesystem: {} ({ROOTS_KEY}, \
             {ROOTS_ENV}); create one of them (with Docker, mount it into the container), \
             or name an existing directory in {ROOTS_KEY}",
            listed(inert)
        ),
    )
}

fn outside(shown: &str, roots: &[String]) -> ToolError {
    let first = roots.first().map(String::as_str).unwrap_or("");
    refusal(
        OUTSIDE,
        format!(
            "{shown} is not inside a documents directory; document_ingest reads only from {} \
             ({ROOTS_KEY}, {ROOTS_ENV}): copy the file into {first} or add its directory to \
             {ROOTS_KEY}",
            listed(roots)
        ),
    )
}

fn not_absolute(shown: &str, roots: &[String]) -> ToolError {
    let first = roots.first().map(String::as_str).unwrap_or("");
    let name = Path::new(shown)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let example = Path::new(first).join(name);
    refusal(
        NOT_ABSOLUTE,
        format!(
            "{shown} is a relative path; document_ingest needs the file's absolute path inside \
             a documents directory ({}), for example {}",
            listed(roots),
            example.display()
        ),
    )
}

/// The refusal text for a path inside a documents directory that the
/// daemon cannot read (`error` names why), with the fix.
pub(crate) fn unreadable(shown: &str, error: &str) -> String {
    format!(
        "{UNREADABLE}: {shown} is inside a documents directory, but the daemon cannot read it \
         ({error}); make the file readable by the daemon's account (with Docker on Linux: \
         chmod a+r on the file and a+rx on the folders above it), then ingest it again"
    )
}

/// What an admitted request reads: the resolved path, that it does not
/// exist, or that the daemon cannot reach it (each inside a root, so saying
/// so tells nothing about the rest of the filesystem).
#[derive(Debug, PartialEq)]
pub(crate) enum Admitted {
    /// The file, and the resolved roots its open is checked against.
    File {
        path: PathBuf,
        roots: Vec<PathBuf>,
    },
    Missing,
    /// A folder on the way cannot be searched: the OS error, for the message.
    Unreadable(String),
}

/// Whether the file `file` has open lies inside one of `roots` (resolved),
/// read from the open handle itself, so a component replaced by a link
/// after [`admit`] resolved the path cannot redirect the read. Linux reads
/// it from `/proc/self/fd` and refuses when it cannot; elsewhere the check
/// passes and the read rests on `admit` alone (no shipped daemon runs
/// there).
pub(crate) fn opened_inside(file: &std::fs::File, roots: &[PathBuf]) -> bool {
    #[cfg(target_os = "linux")]
    {
        use std::os::fd::AsRawFd;
        match std::fs::read_link(format!("/proc/self/fd/{}", file.as_raw_fd())) {
            Ok(opened) => roots.iter().any(|root| resolved_within(&opened, root)),
            Err(_) => false,
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (file, roots);
        true
    }
}

/// The resolved form of a path `canonicalize` refused: its deepest
/// resolvable ancestor, with the remaining names appended, and the first
/// refusal on the way that was not a missing name (a folder the daemon
/// cannot search). `None` when a component that exists is a link that
/// cannot be resolved (a dangling or looping link). The path is lexical
/// (no `.` or `..`).
fn resolve_missing(path: &Path, look: &dyn Fn(&Path)) -> Option<(PathBuf, Option<String>)> {
    let mut missing = Vec::new();
    let mut current = path.to_path_buf();
    let mut problem = None;
    loop {
        look(&current);
        match std::fs::canonicalize(&current) {
            Ok(resolved) => {
                let mut out = resolved;
                for name in missing.iter().rev() {
                    out.push(name);
                }
                return Some((out, problem));
            }
            Err(e) => {
                // It exists but cannot be resolved: a link to nowhere, or
                // a loop. Where it points is not inside any root we can
                // vouch for.
                if std::fs::symlink_metadata(&current).is_ok_and(|m| m.file_type().is_symlink()) {
                    return None;
                }
                if problem.is_none()
                    && !matches!(
                        e.kind(),
                        std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                    )
                {
                    problem = Some(e.to_string());
                }
                missing.push(current.file_name()?.to_os_string());
                current = current.parent()?.to_path_buf();
            }
        }
    }
}

/// Decide one `document_ingest` path against `roots` (the effective list,
/// already validated by [`configured`]). `shown` is the path as pathlib
/// normalizes it.
#[allow(clippy::result_large_err)]
pub(crate) fn admit(shown: &str, roots: &[String]) -> Result<Admitted, ToolError> {
    admit_observed(shown, roots, &|_| {})
}

/// [`admit`], calling `look` with each path of the request before the disk
/// is asked about it (the tests' view of what a refusal looked at).
#[allow(clippy::result_large_err)]
pub(crate) fn admit_observed(
    shown: &str,
    roots: &[String],
    look: &dyn Fn(&Path),
) -> Result<Admitted, ToolError> {
    if roots.is_empty() {
        return Err(none_configured());
    }
    let (usable, inert) = usable(roots);
    if usable.is_empty() {
        return Err(none_usable(&inert));
    }
    let requested = Path::new(shown);
    if !requested.is_absolute() {
        return Err(not_absolute(shown, roots));
    }
    if shown.contains('\0') {
        return Err(outside(shown, roots));
    }
    // Lexically first: a path no root can contain is refused before the
    // disk is asked anything about it, so the answer cannot say whether
    // it exists. A root counts as written and as resolved (a root that is
    // a link, or a short or differently cased spelling on Windows).
    let lex = lexical(requested);
    let lexically_inside = usable
        .iter()
        .any(|r| within(&lex, &r.written) || within(&lex, &r.resolved));
    if !lexically_inside {
        return Err(outside(shown, roots));
    }
    // A `..` after a link means the link target's parent to the filesystem
    // but the link's parent to the check above, so such a spelling is
    // refused rather than read with a meaning the caller may not intend
    // (and the disk is never asked about a folder it only passes through).
    if requested
        .components()
        .any(|c| matches!(c, Component::ParentDir))
    {
        return Err(refusal(
            DOTDOT,
            format!(
                "{shown} contains `..`; pass the file's path inside the documents directory                  without it, for example {}",
                lex.display()
            ),
        ));
    }
    // Then resolved (no `..` is left, so the lexical form is the path):
    // links are followed.
    look(&lex);
    let (resolved, state) = match std::fs::canonicalize(&lex) {
        Ok(resolved) => (resolved, Ok(())),
        Err(_) => match resolve_missing(&lex, look) {
            Some((resolved, problem)) => (resolved, Err(problem)),
            None => return Err(outside(shown, roots)),
        },
    };
    // A resolved root is never a pseudo-filesystem or above one (`usable`),
    // so a path inside it is not in one either.
    let inside = usable
        .iter()
        .any(|r| resolved_within(&resolved, &r.resolved));
    if !inside {
        return Err(outside(shown, roots));
    }
    Ok(match state {
        Ok(()) => Admitted::File {
            path: resolved,
            roots: usable.into_iter().map(|r| r.resolved).collect(),
        },
        Err(None) => Admitted::Missing,
        Err(Some(problem)) => Admitted::Unreadable(problem),
    })
}

#[cfg(test)]
#[path = "document_roots_tests.rs"]
mod tests;
