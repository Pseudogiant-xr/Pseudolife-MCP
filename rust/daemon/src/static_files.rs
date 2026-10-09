//! The Console's static shell under `/ui` (spec R2-R3, `web/api.py:204-228`).

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

/// Python's built-in `mimetypes` table for the extensions a Console build
/// ships, plus the four `add_type` calls in `web/api.py:96-99`. Platform
/// tables (the Windows registry, `/etc/mime.types`) are not consulted.
fn guess_type(path: &Path) -> Option<&'static str> {
    let ext = path.extension()?.to_str()?.to_ascii_lowercase();
    Some(match ext.as_str() {
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
    })
}

/// `(root / rel).resolve()` for a path that need not exist: lexical, with
/// `..` able to climb out (which the caller then refuses).
fn resolve_under(root: &Path, rel: &str) -> PathBuf {
    let mut out = PathBuf::new();
    for comp in root.join(rel).components() {
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
    let root = resolve_under(&std::env::current_dir()?.join(root), "");
    let mut target = resolve_under(&root, rel);
    if !target.starts_with(&root) {
        return Ok(Served {
            status: 403,
            body: b"forbidden".to_vec(),
            content_type: "text/plain".into(),
            cache: "no-store",
        });
    }
    // `Path.resolve()` follows symlinks: a link out of the root is refused
    // like a `..` escape.
    if let (Ok(real), Ok(real_root)) =
        (std::fs::canonicalize(&target), std::fs::canonicalize(&root))
        && !real.starts_with(&real_root)
    {
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
    let mut ctype = guess_type(&target)
        .unwrap_or("application/octet-stream")
        .to_string();
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
    Ok(Served {
        status: 200,
        body: std::fs::read(&target)?,
        content_type: ctype,
        cache,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tree() -> PathBuf {
        let root = std::env::temp_dir().join(format!("pl-static-{}", std::process::id()));
        std::fs::create_dir_all(root.join("assets/sub")).unwrap();
        std::fs::write(root.join("index.html"), b"<html>").unwrap();
        std::fs::write(root.join("assets/a.js"), b"js").unwrap();
        std::fs::write(root.join("assets/f.woff2"), b"font").unwrap();
        std::fs::write(root.join("assets/sub/index.html"), b"sub").unwrap();
        root
    }

    #[test]
    fn index_fallback_traversal_and_types() {
        let root = tree();
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
        let root = tree();
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
}
