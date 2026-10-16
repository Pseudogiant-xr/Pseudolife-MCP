pub use super::claim_name::{claim_name, fold, forbidden, normalize};
use super::identity;
use serde_json::{Map, Value, json};
use std::{path::Path, process::Stdio, time::Duration};
use unicode_normalization::UnicodeNormalization;

async fn git(
    worktree: &Path,
    args: &[&std::ffi::OsStr],
    missing_ok: bool,
) -> Result<String, &'static str> {
    let mut command = tokio::process::Command::new("git");
    command
        .arg("-C")
        .arg(worktree)
        .args(args)
        // Git queries must not inherit the downstream JSON-RPC input pipe.
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().to_uppercase().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    #[cfg(windows)]
    {
        command.creation_flags(0x08000000);
    }
    let output = tokio::time::timeout(Duration::from_secs(5), command.output())
        .await
        .map_err(|_| "invalid_repository")?
        .map_err(|_| "invalid_repository")?;
    if !(output.status.success() || missing_ok && output.status.code() == Some(1)) {
        return Err("invalid_repository");
    }
    String::from_utf8(output.stdout)
        .map(|text| text.trim_end_matches(['\r', '\n']).to_owned())
        .map_err(|_| "invalid_repository")
}
fn redirected(meta: &std::fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        meta.file_attributes() & 0x400 != 0 || meta.file_type().is_symlink()
    }
    #[cfg(not(windows))]
    {
        meta.file_type().is_symlink()
    }
}
fn check_file(root: &Path, path: &str, sensitive: bool) -> Result<(), &'static str> {
    let parts = path.split('/').collect::<Vec<_>>();
    let mut current = root.to_owned();
    for (index, part) in parts.iter().enumerate() {
        let spelling = |value: &str| {
            let nfc = value.nfc().collect::<String>();
            if sensitive { nfc } else { fold(&nfc) }
        };
        let mut selected = part.to_string();
        if current.is_dir() {
            let matches = std::fs::read_dir(&current)
                .map_err(|_| "invalid_claim_path")?
                .filter_map(Result::ok)
                .filter_map(|entry| entry.file_name().into_string().ok())
                .filter(|name| spelling(name) == spelling(part))
                .collect::<Vec<_>>();
            if matches.len() > 1 {
                return Err("invalid_claim_path");
            }
            if let Some(name) = matches.first() {
                selected = name.clone();
            }
        }
        current.push(selected);
        let meta = match std::fs::symlink_metadata(&current) {
            Ok(meta) => meta,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => return Err("invalid_claim_path"),
        };
        if redirected(&meta)
            || (index + 1 < parts.len() && !meta.is_dir())
            || (index + 1 == parts.len() && !meta.is_file())
        {
            return Err("invalid_claim_path");
        }
    }
    if current.is_dir() {
        return Err("invalid_claim_path");
    }
    Ok(())
}
pub async fn prepare(worktree: &str, path: &str) -> Result<(String, String), &'static str> {
    if !Path::new(worktree).is_absolute()
        || worktree.chars().count() > 4096
        || worktree.chars().any(forbidden)
    {
        return Err("invalid_repository");
    }
    let normalized = normalize(path, true)?;
    if cfg!(windows)
        && normalized
            .as_bytes()
            .windows(2)
            .any(|pair| pair[0] == b'~' && pair[1].is_ascii_digit())
    {
        return Err("invalid_claim_path");
    }
    let output = git(
        Path::new(worktree),
        &[
            "rev-parse".as_ref(),
            "--path-format=absolute".as_ref(),
            "--show-toplevel".as_ref(),
            "--git-common-dir".as_ref(),
            "--git-dir".as_ref(),
        ],
        false,
    )
    .await?;
    let paths = output
        .lines()
        .map(|path| std::fs::canonicalize(path).map_err(|_| "invalid_repository"))
        .collect::<Result<Vec<_>, _>>()?;
    if paths.len() != 3 {
        return Err("invalid_repository");
    }
    let (root, common, gitdir) = (&paths[0], &paths[1], &paths[2]);
    let config = common.join("config");
    let ignorecase = git(
        Path::new(worktree),
        &[
            "config".as_ref(),
            "--no-includes".as_ref(),
            "--file".as_ref(),
            config.as_os_str(),
            "--type=bool".as_ref(),
            "--get".as_ref(),
            "core.ignorecase".as_ref(),
        ],
        true,
    )
    .await?;
    let sensitive = !(cfg!(windows) || ignorecase == "true");
    let canonical = normalize(&normalized, sensitive)?;
    for metadata in [common, gitdir] {
        if let Ok(relative) = metadata.strip_prefix(root) {
            let relative = relative.to_string_lossy().replace('\\', "/");
            let relative = if sensitive {
                relative.nfc().collect()
            } else {
                fold(&relative)
            };
            if relative.is_empty()
                || canonical == relative
                || canonical.starts_with(&format!("{relative}/"))
            {
                return Err("invalid_claim_path");
            }
        }
    }
    check_file(root, &normalized, sensitive)?;
    let meta = std::fs::metadata(common).map_err(|_| "invalid_repository")?;
    if !meta.is_dir() {
        return Err("invalid_repository");
    }
    #[cfg(unix)]
    let (dev, ino) = {
        use std::os::unix::fs::MetadataExt;
        (meta.dev(), meta.ino())
    };
    #[cfg(windows)]
    let (dev, ino) = crate::credentials::windows_security::directory_identity(common)
        .map_err(|_| "invalid_repository")?;
    if ino == 0 {
        return Err("invalid_repository");
    }
    let common = common.to_string_lossy().to_string();
    #[cfg(windows)]
    let common = common
        .strip_prefix("\\\\?\\")
        .unwrap_or(&common)
        .replace('/', "\\")
        .to_lowercase();
    let repository = identity::hex_hash(identity::ascii_json(&json!([common, dev, ino])));
    Ok((repository, canonical))
}
pub async fn prepare_arguments(
    name: &str,
    mut arguments: Map<String, Value>,
) -> Result<Map<String, Value>, &'static str> {
    if name != "memory_agents" || arguments.get("worktree").is_none_or(Value::is_null) {
        return Ok(arguments);
    }
    if !matches!(
        arguments.get("action").and_then(Value::as_str),
        Some("claim" | "release")
    ) || ["lease", "repository_id"]
        .iter()
        .any(|key| arguments.get(*key).is_some_and(|value| !value.is_null()))
    {
        return Err("unexpected_parameter");
    }
    let worktree = arguments
        .get("worktree")
        .and_then(Value::as_str)
        .ok_or("invalid_repository")?;
    let path = arguments
        .get("path")
        .and_then(Value::as_str)
        .ok_or("invalid_claim_path")?;
    let (repository, path) = prepare(worktree, path).await?;
    arguments.remove("worktree");
    arguments.insert("repository_id".into(), json!(repository));
    arguments.insert("path".into(), json!(path));
    Ok(arguments)
}
