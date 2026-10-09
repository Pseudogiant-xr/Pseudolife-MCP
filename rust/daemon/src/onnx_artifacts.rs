//! Offline model-file admission, matching `pseudolife_memory/onnx_artifacts.py`.

use anyhow::{Result, bail};
use serde_json::Value;
use std::path::{Path, PathBuf};

pub fn relative_parts(value: &str) -> Result<Vec<&str>> {
    if value.is_empty() {
        return Ok(vec![]);
    }
    let parts: Vec<_> = value.split('/').collect();
    if value.contains('\\')
        || parts
            .iter()
            .any(|p| p.is_empty() || *p == "." || *p == ".." || p.contains(':'))
    {
        bail!("module path leaves its model directory");
    }
    Ok(parts)
}

pub fn existing_file(root: &Path, parts: &[&str], hub: bool) -> bool {
    let candidate: PathBuf = parts.iter().fold(root.to_path_buf(), |p, c| p.join(c));
    if !candidate.is_file() {
        return false;
    }
    let Ok(resolved_root) = root.canonicalize() else {
        return false;
    };
    let mut parent = root.to_path_buf();
    for part in parts.iter().take(parts.len().saturating_sub(1)) {
        parent.push(part);
        if std::fs::symlink_metadata(&parent).is_ok_and(|m| m.file_type().is_symlink()) {
            return false;
        }
    }
    let Ok(resolved_parent) = candidate.parent().unwrap_or(root).canonicalize() else {
        return false;
    };
    if !resolved_parent.starts_with(&resolved_root) {
        return false;
    }
    let Ok(resolved) = candidate.canonicalize() else {
        return false;
    };
    if !std::fs::symlink_metadata(&candidate).is_ok_and(|m| m.file_type().is_symlink()) {
        return resolved.starts_with(resolved_root);
    }
    if !hub
        || root
            .parent()
            .and_then(Path::file_name)
            .is_none_or(|p| p != "snapshots")
    {
        return false;
    }
    root.parent()
        .and_then(Path::parent)
        .and_then(|p| p.join("blobs").canonicalize().ok())
        .is_some_and(|blobs| resolved.starts_with(blobs))
}

pub fn transformer(kind: &str) -> bool {
    matches!(
        kind,
        "sentence_transformers.models.Transformer"
            | "sentence_transformers.base.modules.Transformer"
            | "sentence_transformers.base.modules.transformer.Transformer"
    )
}

pub fn postprocessor(kind: &str) -> bool {
    ["Pooling", "Normalize", "Dense"].iter().any(|name| {
        kind == format!("sentence_transformers.models.{name}")
            || kind == format!("sentence_transformers.base.modules.{name}")
    }) || matches!(
        kind,
        "sentence_transformers.sentence_transformer.modules.pooling.Pooling"
            | "sentence_transformers.sentence_transformer.modules.normalize.Normalize"
            | "sentence_transformers.base.modules.dense.Dense"
    )
}

pub fn layout_available(root: &Path, file_name: &str, hub: bool) -> bool {
    let Ok(parts) = relative_parts(file_name) else {
        return false;
    };
    if parts.is_empty() || !file_name.ends_with(".onnx") {
        return false;
    }
    if !root.join("modules.json").exists() {
        return existing_file(root, &parts, hub);
    }
    if !existing_file(root, &["modules.json"], hub) {
        return false;
    }
    let Ok(raw) = std::fs::read(root.join("modules.json")) else {
        return false;
    };
    let Ok(Value::Array(modules)) = serde_json::from_slice(&raw) else {
        return false;
    };
    let mut count = 0;
    for module in modules {
        let (Some(path), Some(kind)) = (module["path"].as_str(), module["type"].as_str()) else {
            return false;
        };
        let Ok(mut subfolder) = relative_parts(path) else {
            return false;
        };
        if transformer(kind) {
            count += 1;
            subfolder.extend(parts.iter().copied());
            if !existing_file(root, &subfolder, hub) {
                return false;
            }
        } else if !postprocessor(kind) {
            return false;
        }
    }
    count > 0
}

/// Resolve only an existing local directory or the cached Hub `refs/main` snapshot.
/// No network client or exporter participates in this path.
pub fn resolve(model: &str, file_name: &str, cache: &Path) -> Result<(PathBuf, bool)> {
    let normalized = file_name.replace('\\', "/");
    relative_parts(&normalized)?;
    if !normalized.ends_with(".onnx") {
        bail!("embedding.onnx_file_name must be a relative .onnx path");
    }
    let local = Path::new(model);
    if local.is_dir() {
        if layout_available(local, &normalized, false) {
            return Ok((local.to_path_buf(), false));
        }
    } else {
        let candidates = if model.contains('/') {
            vec![model.to_string()]
        } else {
            vec![format!("sentence-transformers/{model}"), model.to_string()]
        };
        for repo in candidates {
            let repo = cache.join(format!("models--{}", repo.replace('/', "--")));
            if let Ok(revision) = std::fs::read_to_string(repo.join("refs/main")) {
                // A Hub ref is a single revision, never an arbitrary filesystem path.
                if revision.is_empty() || !revision.chars().all(|c| c.is_ascii_hexdigit()) {
                    continue;
                }
                let root = repo.join("snapshots").join(revision);
                if layout_available(&root, &normalized, true) {
                    return Ok((root, true));
                }
            }
        }
    }
    bail!("configured ONNX artifact is not available in the local model or Hub cache")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flat_artifact_is_admitted_but_unknown_module_is_not() {
        let root = std::env::temp_dir().join(format!("pl_cf_emb_artifact_{}", std::process::id()));
        std::fs::create_dir_all(root.join("onnx")).unwrap();
        std::fs::write(root.join("onnx/model.onnx"), b"fixture").unwrap();
        assert!(layout_available(&root, "onnx/model.onnx", false));
        std::fs::write(
            root.join("modules.json"),
            r#"[{"path":"","type":"custom.Encoder"}]"#,
        )
        .unwrap();
        assert!(!layout_available(&root, "onnx/model.onnx", false));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn module_paths_and_cached_refs_cannot_escape() {
        for bad in ["/onnx", "../onnx", "a//b", "a\\b", "C:/onnx", "a/./b"] {
            assert!(relative_parts(bad).is_err(), "{bad}");
        }
        assert_eq!(relative_parts("").unwrap(), Vec::<&str>::new());
        let root = std::env::temp_dir().join(format!("pl_cf_emb_nested_{}", std::process::id()));
        std::fs::create_dir_all(root.join("onnx")).unwrap();
        std::fs::write(root.join("onnx/model.onnx"), b"fixture").unwrap();
        std::fs::write(
            root.join("modules.json"),
            r#"[{"path":"0_Transformer","type":"sentence_transformers.models.Transformer"}]"#,
        )
        .unwrap();
        assert!(!layout_available(&root, "onnx/model.onnx", false));
        std::fs::create_dir_all(root.join("0_Transformer/onnx")).unwrap();
        std::fs::write(root.join("0_Transformer/onnx/model.onnx"), b"fixture").unwrap();
        assert!(layout_available(&root, "onnx/model.onnx", false));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn only_same_repository_hub_blob_leaf_links_are_admitted() {
        use std::os::unix::fs::symlink;
        let base = std::env::temp_dir().join(format!("pl_cf_emb_links_{}", std::process::id()));
        let repo = base.join("models--fixture--model");
        let root = repo.join("snapshots/revision");
        std::fs::create_dir_all(root.join("onnx")).unwrap();
        std::fs::create_dir_all(repo.join("blobs")).unwrap();
        std::fs::write(repo.join("blobs/digest"), b"fixture").unwrap();
        symlink(repo.join("blobs/digest"), root.join("onnx/model.onnx")).unwrap();
        assert!(!layout_available(&root, "onnx/model.onnx", false));
        assert!(layout_available(&root, "onnx/model.onnx", true));
        std::fs::remove_file(root.join("onnx/model.onnx")).unwrap();
        std::fs::write(base.join("outside"), b"fixture").unwrap();
        symlink(base.join("outside"), root.join("onnx/model.onnx")).unwrap();
        assert!(!layout_available(&root, "onnx/model.onnx", true));
        std::fs::remove_file(root.join("onnx/model.onnx")).unwrap();
        std::fs::remove_dir(root.join("onnx")).unwrap();
        std::fs::write(repo.join("blobs/digest.onnx"), b"fixture").unwrap();
        symlink(repo.join("blobs"), root.join("onnx")).unwrap();
        assert!(!layout_available(&root, "onnx/digest.onnx", true));
        std::fs::remove_dir_all(base).unwrap();
    }
}
