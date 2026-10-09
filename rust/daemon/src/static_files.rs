//! The Console's static shell under `/ui` (spec R2-R3, `web/api.py:204-228`).

use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};

/// Every `/` and `/ui` answer carries these (`web/api.py:60-71`).
pub const SECURITY_HEADERS: [(&str, &str); 4] = [
    (
        "content-security-policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; \
         img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; \
         object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    ),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("x-content-type-options", "nosniff"),
];

pub struct Served {
    pub status: u16,
    pub body: Vec<u8>,
    pub content_type: String,
    pub cache: &'static str,
}

/// Types of the committed Console build, including its vendor notice.
/// Python obtains Markdown's Linux type from `/etc/mime.types`; without
/// that file (including the supported Windows oracle), it is octet-stream.
fn guess_type(path: &Path) -> Option<String> {
    let ext = path.extension()?.to_str()?.to_ascii_lowercase();
    #[cfg(target_os = "linux")]
    if ext == "md" {
        return markdown_type(Path::new("/etc/mime.types"));
    }
    Some(
        match ext.as_str() {
            "html" | "htm" => "text/html",
            "css" => "text/css",
            "js" | "mjs" => "application/javascript",
            "json" => "application/json",
            "svg" => "image/svg+xml",
            "woff2" => "font/woff2",
            "png" => "image/png",
            "jpg" | "jpeg" => "image/jpeg",
            "gif" => "image/gif",
            "webp" => "image/webp",
            "avif" => "image/avif",
            "ico" => "image/vnd.microsoft.icon",
            "txt" => "text/plain",
            "xml" => "text/xml",
            "wasm" => "application/wasm",
            "pdf" => "application/pdf",
            _ => return None,
        }
        .to_string(),
    )
}

/// Only the shipped vendor notice needs the system MIME table. Preserve
/// its absence and last mapping; unshipped extensions remain deferred.
#[cfg(target_os = "linux")]
fn markdown_type(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut found = None;
    for line in text.lines() {
        let mut fields = line.split('#').next().unwrap_or("").split_whitespace();
        if let Some(kind) = fields.next()
            && fields.any(|ext| ext == "md")
        {
            found = Some(kind.to_string());
        }
    }
    found
}

/// Python's non-strict `Path.resolve()`: resolve existing ancestors even
/// when the requested file is missing. This keeps an escaping directory
/// link from turning a refused request into the SPA fallback.
fn resolve_path(path: &Path, links: &mut HashSet<PathBuf>) -> std::io::Result<PathBuf> {
    // ntpath.realpath normalizes parents before resolving Windows links.
    #[cfg(windows)]
    let normalized = lexical_path(path);
    #[cfg(windows)]
    let path = normalized.as_path();
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => {
                out.push(other.as_os_str());
                if let Ok(real) = std::fs::canonicalize(&out) {
                    out = real;
                } else if let Ok(dest) = std::fs::read_link(&out) {
                    if !links.insert(out.clone()) {
                        return Err(std::io::Error::other("symlink loop"));
                    }
                    let link = out.clone();
                    let target = out.parent().unwrap_or(Path::new("")).join(dest);
                    out = resolve_path(&target, links)?;
                    links.remove(&link);
                }
            }
        }
    }
    Ok(out)
}

#[cfg(windows)]
fn lexical_path(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// `_serve_static(path)`: `path` is the full request path (`/ui/...`).
pub fn serve(root: &Path, path: &str) -> std::io::Result<Served> {
    let trimmed = path.trim_matches('/');
    let rel = if matches!(trimmed, "" | "ui" | "ui/") {
        "index.html"
    } else if let Some(rest) = trimmed.strip_prefix("ui/") {
        rest
    } else {
        trimmed
    };
    if rel.contains('\0') {
        // `Path.resolve()` raises on an embedded NUL: the 500 path.
        return Err(std::io::Error::other("embedded NUL in path"));
    }
    let original_root = std::env::current_dir()?.join(root);
    let root = resolve_path(&original_root, &mut HashSet::new())?;
    let mut target = resolve_path(&original_root.join(rel), &mut HashSet::new())?;
    if !target.starts_with(&root) && !crate::mutants::active("static-traversal-open") {
        return Ok(Served {
            status: 403,
            body: b"forbidden".to_vec(),
            content_type: "text/plain".into(),
            cache: "no-store",
        });
    }
    if target.is_dir() {
        target = target.join("index.html");
    }
    if !target.is_file() {
        let index = root.join("index.html");
        if index.is_file() {
            return Ok(Served {
                status: 200,
                body: std::fs::read(index)?,
                content_type: "text/html; charset=utf-8".into(),
                cache: "no-store",
            });
        }
        return Ok(Served {
            status: 404,
            body: b"not found".to_vec(),
            content_type: "text/plain".into(),
            cache: "no-store",
        });
    }
    let mut ctype = guess_type(&target).unwrap_or_else(|| "application/octet-stream".into());
    if crate::mutants::active("static-wrong-type") {
        ctype = "application/octet-stream".into();
    }
    if ctype.starts_with("text/")
        || matches!(
            ctype.as_str(),
            "application/javascript" | "image/svg+xml" | "application/json"
        )
    {
        ctype.push_str("; charset=utf-8");
    }
    let cache = if ctype.starts_with("font/") || ctype.starts_with("image/") {
        "max-age=86400"
    } else {
        "no-store"
    };
    let mut body = std::fs::read(&target)?;
    if crate::mutants::active("static-json-whitespace") && body == b"{\"a\": 1}" {
        body = b"{\"a\":1 }".to_vec();
    }
    Ok(Served {
        status: 200,
        body,
        content_type: ctype,
        cache,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tree(name: &str) -> PathBuf {
        // One directory per test: tests run in parallel in one process.
        let root = std::env::temp_dir().join(format!("pl-static-{}-{name}", std::process::id()));
        std::fs::create_dir_all(root.join("assets/sub")).unwrap();
        std::fs::write(root.join("index.html"), b"<html>").unwrap();
        std::fs::write(root.join("assets/a.js"), b"js").unwrap();
        std::fs::write(root.join("assets/f.woff2"), b"font").unwrap();
        std::fs::write(root.join("assets/sub/index.html"), b"sub").unwrap();
        root
    }

    #[test]
    fn index_fallback_traversal_and_types() {
        let root = tree("types");
        let s = serve(&root, "/ui").unwrap();
        assert_eq!((s.status, s.body.as_slice()), (200, b"<html>".as_slice()));
        let s = serve(&root, "/ui/assets/a.js").unwrap();
        assert_eq!(s.content_type, "application/javascript; charset=utf-8");
        assert_eq!(s.cache, "no-store");
        let s = serve(&root, "/ui/assets/f.woff2").unwrap();
        assert_eq!(
            (s.content_type.as_str(), s.cache),
            ("font/woff2", "max-age=86400")
        );
        let s = serve(&root, "/ui/assets/sub").unwrap();
        assert_eq!(s.body, b"sub");
        #[cfg(windows)]
        {
            std::fs::write(root.join("README.md"), b"vendor notice").unwrap();
            assert_eq!(
                serve(&root, "/ui/README.md").unwrap().content_type,
                "application/octet-stream"
            );
        }
        let s = serve(&root, "/ui/nope/route").unwrap();
        assert_eq!((s.status, s.body.as_slice()), (200, b"<html>".as_slice()));
        let s = serve(&root, "/ui/../../etc/passwd").unwrap();
        assert_eq!(
            (s.status, s.body.as_slice()),
            (403, b"forbidden".as_slice())
        );
        assert!(serve(&root, "/ui/a\0b").is_err());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_out_of_the_root_is_forbidden() {
        let root = tree("symlink");
        let outside = root.with_extension("outside");
        std::fs::write(&outside, b"outside\n").unwrap();
        std::os::unix::fs::symlink(&outside, root.join("escape.txt")).unwrap();
        let s = serve(&root, "/ui/escape.txt").unwrap();
        assert_eq!(
            (s.status, s.body.as_slice()),
            (403, b"forbidden".as_slice())
        );
        std::fs::remove_dir_all(&root).unwrap();
        std::fs::remove_file(outside).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn links_are_resolved_before_parent_components_and_mime_selection() {
        let root = tree("link-resolution");
        let outside = root.with_extension("outside-dir");
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(root.join("notice.txt"), b"notice").unwrap();
        std::os::unix::fs::symlink(root.join("notice.txt"), root.join("notice.js")).unwrap();
        assert_eq!(
            serve(&root, "/ui/notice.js").unwrap().content_type,
            "text/plain; charset=utf-8"
        );
        std::os::unix::fs::symlink(&outside, root.join("escape")).unwrap();
        for path in ["/ui/escape/missing", "/ui/escape/../index.html"] {
            assert_eq!(serve(&root, path).unwrap().status, 403, "{path}");
        }
        std::fs::remove_dir_all(&root).unwrap();
        std::fs::remove_dir_all(outside).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn markdown_table_missing_unmapped_and_last_mapping() {
        let root = tree("mime");
        let table = root.join("mime.types");
        assert_eq!(markdown_type(&table), None);
        std::fs::write(&table, "text/plain txt # md\n").unwrap();
        assert_eq!(markdown_type(&table), None);
        std::fs::write(
            &table,
            "text/markdown md markdown\ntext/x-markdown md # last\n",
        )
        .unwrap();
        assert_eq!(markdown_type(&table).as_deref(), Some("text/x-markdown"));
        std::fs::remove_dir_all(root).unwrap();
    }
}
