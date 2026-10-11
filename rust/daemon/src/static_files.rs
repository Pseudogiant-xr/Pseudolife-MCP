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
/// Python obtains optional mappings from the platform MIME database.
fn guess_type(path: &Path) -> Option<String> {
    #[cfg(windows)]
    let platform = registry_type;
    #[cfg(target_os = "linux")]
    let platform = |ext: &str| linux_type(ext, Path::new("/etc/mime.types"));
    #[cfg(not(any(windows, target_os = "linux")))]
    let platform = |_: &str| None;
    guess_type_with(path, platform)
}

#[cfg(target_os = "linux")]
fn linux_type(ext: &str, table: &Path) -> Option<String> {
    table_type(table, ext)
}

fn guess_type_with(path: &Path, platform: impl Fn(&str) -> Option<String>) -> Option<String> {
    let ext = path.extension()?.to_str()?.to_ascii_lowercase();
    if matches!(ext.as_str(), "md" | "webp") {
        return platform(&ext);
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

/// The Windows oracle reads REG_SZ Content Type values under HKCR.
/// WebP and Markdown have no built-in mapping in the supported Python.
#[cfg(windows)]
fn registry_type(ext: &str) -> Option<String> {
    use windows_sys::Win32::System::Registry::{HKEY_CLASSES_ROOT, RRF_RT_REG_SZ, RegGetValueW};
    let key: Vec<u16> = format!(".{ext}\0").encode_utf16().collect();
    let value: Vec<u16> = "Content Type\0".encode_utf16().collect();
    let mut size = 0;
    // SAFETY: terminated key/value strings and a valid output length;
    // null data queries the required size without copying a value.
    let result = unsafe {
        RegGetValueW(
            HKEY_CLASSES_ROOT,
            key.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut size,
        )
    };
    if result != 0 || size == 0 || size % 2 != 0 {
        return None;
    }
    let mut data = vec![0u16; size as usize / 2];
    // SAFETY: the u16 buffer has the queried byte capacity, and size is
    // passed back to the API. A changed or missing value returns an error.
    let result = unsafe {
        RegGetValueW(
            HKEY_CLASSES_ROOT,
            key.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            data.as_mut_ptr().cast(),
            &mut size,
        )
    };
    if result != 0 {
        return None;
    }
    data.truncate(size as usize / 2);
    let end = data.iter().position(|c| *c == 0).unwrap_or(data.len());
    String::from_utf16(&data[..end])
        .ok()
        .filter(|s| !s.is_empty())
}

/// Optional shipped types use the system table's last mapping, or absence.
#[cfg(target_os = "linux")]
fn table_type(path: &Path, extension: &str) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut found = None;
    for line in text.lines() {
        let mut fields = line.split('#').next().unwrap_or("").split_whitespace();
        if let Some(kind) = fields.next()
            && fields.any(|ext| ext == extension)
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

fn lexical_path(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            Component::Normal(name) => {
                #[cfg(windows)]
                use std::os::windows::ffi::{OsStrExt, OsStringExt};
                #[cfg(windows)]
                let mut units: Vec<u16> = name.encode_wide().collect();
                #[cfg(windows)]
                while units.last().is_some_and(|u| matches!(*u, 32 | 46)) {
                    units.pop();
                }
                #[cfg(windows)]
                let name = std::ffi::OsString::from_wide(&units);
                out.push(name);
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

fn contained(path: &Path, root: &Path) -> bool {
    if crate::mutants::active("static-string-prefix") {
        path.to_string_lossy()
            .starts_with(root.to_string_lossy().as_ref())
    } else {
        path.starts_with(root)
    }
}

fn forbidden() -> Served {
    Served {
        status: 403,
        body: b"forbidden".to_vec(),
        content_type: "text/plain".into(),
        cache: "no-store",
    }
}

fn metadata(path: &Path) -> std::io::Result<Option<std::fs::Metadata>> {
    match std::fs::metadata(path) {
        Ok(meta) => Ok(Some(meta)),
        Err(e) if missing_metadata_error(&e) => Ok(None),
        Err(e) => Err(e),
    }
}

fn missing_metadata_error(error: &std::io::Error) -> bool {
    #[cfg(windows)]
    let ignored = matches!(error.raw_os_error(), Some(21 | 123 | 1921));
    #[cfg(unix)]
    let ignored = matches!(error.raw_os_error(), Some(libc::EBADF | libc::ELOOP));
    #[cfg(not(any(windows, unix)))]
    let ignored = false;
    // pathlib's is_file/is_dir ignore these OS errors, but not EACCES.
    ignored
        || matches!(
            error.kind(),
            std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
        )
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
    #[cfg(windows)]
    if rel.split(['/', '\\']).any(|part| {
        !part.is_empty()
            && !matches!(part, "." | "..")
            && part.bytes().all(|b| matches!(b, b'.' | b' '))
    }) && !crate::mutants::active("static-traversal-open")
    {
        return Ok(forbidden());
    }
    let original_root = lexical_path(&std::env::current_dir()?.join(root));
    let joined = original_root.join(rel);
    // Refuse out-of-root paths before filesystem access.
    if !contained(&lexical_path(&joined), &original_root)
        && !crate::mutants::active("static-traversal-open")
    {
        return Ok(forbidden());
    }
    let root = resolve_path(&original_root, &mut HashSet::new())?;
    let mut target = resolve_path(&joined, &mut HashSet::new())?;
    if !contained(&target, &root) && !crate::mutants::active("static-traversal-open") {
        return Ok(forbidden());
    }
    if metadata(&target)?.is_some_and(|m| m.is_dir()) {
        target = target.join("index.html");
        let child = resolve_path(&target, &mut HashSet::new())?;
        if !contained(&child, &root) && !crate::mutants::active("static-traversal-open") {
            return Ok(forbidden());
        }
    }
    if !metadata(&target)?.is_some_and(|m| m.is_file()) {
        let index = resolve_path(&root.join("index.html"), &mut HashSet::new())?;
        if !contained(&index, &root) && !crate::mutants::active("static-traversal-open") {
            return Ok(forbidden());
        }
        if metadata(&index)?.is_some_and(|m| m.is_file()) {
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

    #[test]
    fn metadata_absence_preserves_pathlib_error_classes() {
        #[cfg(windows)]
        let codes = [21, 123, 1921];
        #[cfg(unix)]
        let codes = [libc::EBADF, libc::ELOOP];
        for code in codes {
            assert!(
                missing_metadata_error(&std::io::Error::from_raw_os_error(code)),
                "{code}"
            );
        }
        assert!(!missing_metadata_error(&std::io::Error::from(
            std::io::ErrorKind::PermissionDenied
        )));
        assert!(!missing_metadata_error(&std::io::Error::other(
            "not absence"
        )));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_webp_uses_missing_unmapped_and_last_mapping() {
        let root = tree("webp-mime");
        let table = root.join("mime.types");
        assert_eq!(linux_type("webp", &table), None);
        std::fs::write(&table, "image/png png # webp\n").unwrap();
        assert_eq!(linux_type("webp", &table), None);
        std::fs::write(
            &table,
            "image/webp webp\napplication/octet-stream webp # last\n",
        )
        .unwrap();
        assert_eq!(
            linux_type("webp", &table).as_deref(),
            Some("application/octet-stream")
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn webp_mapping_can_be_absent() {
        let path = Path::new("logo.webp");
        assert_eq!(guess_type_with(path, |_| None), None);
        assert_eq!(
            guess_type_with(path, |_| Some("image/webp".into())),
            Some("image/webp".into())
        );
    }

    #[test]
    fn lexical_escape_is_refused_before_an_invalid_root_is_resolved() {
        let root = std::env::temp_dir().join("pl-static-invalid\0root");
        assert_eq!(serve(&root, "/ui/../outside").unwrap().status, 403);
    }

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
    fn spa_fallback_refuses_index_resolving_outside_root() {
        let root = tree("fallback-outside");
        let outside = root.with_extension("outside");
        std::fs::write(&outside, b"marker").unwrap();
        std::fs::remove_file(root.join("index.html")).unwrap();
        std::os::unix::fs::symlink(&outside, root.join("index.html")).unwrap();
        let direct = serve(&root, "/ui/").unwrap();
        let fallback = serve(&root, "/ui/unknown/route").unwrap();
        std::fs::remove_dir_all(&root).unwrap();
        std::fs::remove_file(outside).unwrap();
        assert_eq!(
            (direct.status, direct.body.as_slice()),
            (403, b"forbidden".as_slice())
        );
        assert_eq!(
            (
                fallback.status,
                fallback.body,
                fallback.content_type,
                fallback.cache
            ),
            (
                direct.status,
                direct.body,
                direct.content_type,
                direct.cache
            )
        );
    }

    #[cfg(unix)]
    #[test]
    fn spa_fallback_serves_index_link_within_root() {
        let root = tree("fallback-inside");
        std::fs::rename(root.join("index.html"), root.join("shell.html")).unwrap();
        std::os::unix::fs::symlink("shell.html", root.join("index.html")).unwrap();
        let s = serve(&root, "/ui/unknown/route").unwrap();
        std::fs::remove_dir_all(root).unwrap();
        assert_eq!((s.status, s.body.as_slice()), (200, b"<html>".as_slice()));
        assert_eq!(s.content_type, "text/html; charset=utf-8");
        assert_eq!(s.cache, "no-store");
    }

    #[test]
    fn spa_fallback_without_index_is_not_found() {
        let root = tree("fallback-missing");
        std::fs::remove_file(root.join("index.html")).unwrap();
        let s = serve(&root, "/ui/unknown/route").unwrap();
        std::fs::remove_dir_all(root).unwrap();
        assert_eq!(
            (s.status, s.body.as_slice()),
            (404, b"not found".as_slice())
        );
        assert_eq!(s.content_type, "text/plain");
        assert_eq!(s.cache, "no-store");
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
        assert_eq!(table_type(&table, "md"), None);
        std::fs::write(&table, "text/plain txt # md\n").unwrap();
        assert_eq!(table_type(&table, "md"), None);
        std::fs::write(
            &table,
            "text/markdown md markdown\ntext/x-markdown md # last\n",
        )
        .unwrap();
        assert_eq!(table_type(&table, "md").as_deref(), Some("text/x-markdown"));
        std::fs::remove_dir_all(root).unwrap();
    }
}
