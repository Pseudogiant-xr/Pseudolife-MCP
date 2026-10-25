//! The document store (`document_ingest`, `document_search`) over the bank's
//! `reference_chunks` table (schema v57). Python: `mcp_server.py`
//! `document_ingest` / `document_search` (2988-3019); `service.py`
//! `ingest_document` / `search_documents` (3587-3637);
//! `memory/reference_bank.py` (chunking, chunk ids, `ingest_text`,
//! `retrieve`); `memory/document_parser.py` `extract_text`;
//! `storage/postgres.py` `upsert_reference_chunks`,
//! `search_reference_chunks` (2024-2091).
//!
//! The Rust daemon has no chromadb reader: the one-time import of a pre-v57
//! store (`storage/migrate_chroma.py`) stays with the Python daemon (D5).
//! PDF text extraction (pypdf) has no byte-equal Rust port and is refused
//! with the declared stub refusal (`divergences.md`, DOC-PDF).

use md5::{Digest, Md5};
use serde_json::{Map, Value, json};

use crate::fact_set::internal;
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::stores::cortex::py_isspace;

/// `INGEST_ENCODE_BATCH`: chunks per encode call while ingesting.
const INGEST_ENCODE_BATCH: usize = 8;

/// `extract_text`'s plain-text suffixes, read without a wrapping `try`.
const TEXT_SUFFIXES: &[&str] = &[
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
];

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![
        ("document_ingest", mcp_document_ingest),
        ("document_search", mcp_document_search),
    ]
}

// ── pathlib ──────────────────────────────────────────────────────────────

/// What `Path(path)` makes of the argument: `str(path)` and `path.name`.
#[derive(Debug, PartialEq)]
pub(crate) struct PyPath {
    pub shown: String,
    pub name: String,
}

/// `PurePosixPath`'s parse: one leading `//` is kept (two, not three),
/// any other run of slashes is one, `.` components drop, a trailing slash
/// drops, and nothing at all is `.`.
#[cfg(not(windows))]
pub(crate) fn py_path(raw: &str) -> PyPath {
    let root = if raw.starts_with("//") && !raw.starts_with("///") {
        "//"
    } else if raw.starts_with('/') {
        "/"
    } else {
        ""
    };
    let parts: Vec<&str> = raw
        .split('/')
        .filter(|p| !p.is_empty() && *p != ".")
        .collect();
    let mut shown = format!("{root}{}", parts.join("/"));
    if shown.is_empty() {
        shown = ".".into();
    }
    PyPath {
        shown,
        name: parts.last().map_or(String::new(), |p| p.to_string()),
    }
}

/// CPython 3.12's `ntpath.splitroot` over a path whose `/` are already
/// `\`: `(drive, root, rest)`, sliced by code point as Python slices.
#[cfg(any(windows, test))]
fn nt_splitroot(p: &[char]) -> (String, String, String) {
    const SEP: char = '\\';
    let s = |a: usize, b: usize| -> String { p[a.min(p.len())..b.min(p.len())].iter().collect() };
    let at = |i: usize| p.get(i).copied();
    let find = |from: usize| (from..p.len()).find(|&i| p[i] == SEP);
    let n = p.len();
    if at(0) == Some(SEP) {
        if at(1) == Some(SEP) {
            // UNC drives (`\\server\share`, `\\?\UNC\server\share`) and
            // device drives (`\\.\device`, `\\?\device`).
            let head: String = p.iter().take(8).collect::<String>().to_uppercase();
            let start = if head == r"\\?\UNC\" { 8 } else { 2 };
            let Some(index) = find(start) else {
                return (s(0, n), String::new(), String::new());
            };
            let Some(index2) = find(index + 1) else {
                return (s(0, n), String::new(), String::new());
            };
            return (s(0, index2), s(index2, index2 + 1), s(index2 + 1, n));
        }
        return (String::new(), s(0, 1), s(1, n));
    }
    if at(1) == Some(':') {
        if at(2) == Some(SEP) {
            return (s(0, 2), s(2, 3), s(3, n));
        }
        return (s(0, 2), String::new(), s(2, n));
    }
    (String::new(), String::new(), s(0, n))
}

/// `PureWindowsPath` as CPython 3.12 parses and formats it (`_parse_path`,
/// `_format_parsed_parts`): both slashes separate, the shown form uses `\`,
/// a `\\server\share` drive always has a root, and a first part that reads
/// as a drive is shown behind `.\`. CPython 3.11 parsed paths that start
/// with two separators differently (`divergences.md`, DOC-PATH-WINDOWS).
#[cfg(any(windows, test))]
pub(crate) fn py_path_windows(raw: &str) -> PyPath {
    const SEP: char = '\\';
    if raw.is_empty() {
        return PyPath {
            shown: ".".into(),
            name: String::new(),
        };
    }
    let chars: Vec<char> = raw
        .chars()
        .map(|c| if c == '/' { SEP } else { c })
        .collect();
    let (drive, mut root, rest) = nt_splitroot(&chars);
    if root.is_empty() && drive.starts_with(SEP) && !drive.ends_with(SEP) {
        let drive_parts: Vec<&str> = drive.split(SEP).collect();
        // `drv_parts[2] not in '?.'` is a substring test: "", "?", "." and
        // "?." all fail it.
        if (drive_parts.len() == 4 && !matches!(drive_parts[2], "" | "?" | "." | "?."))
            || drive_parts.len() == 6
        {
            root = SEP.to_string();
        }
    }
    let tail: Vec<&str> = rest
        .split(SEP)
        .filter(|p| !p.is_empty() && *p != ".")
        .collect();
    let shown = if !drive.is_empty() || !root.is_empty() {
        format!("{drive}{root}{}", tail.join(r"\"))
    } else if tail
        .first()
        .is_some_and(|t| !nt_splitroot(&t.chars().collect::<Vec<_>>()).0.is_empty())
    {
        format!(r".\{}", tail.join(r"\"))
    } else {
        tail.join(r"\")
    };
    PyPath {
        shown: if shown.is_empty() { ".".into() } else { shown },
        name: tail.last().map_or(String::new(), |p| p.to_string()),
    }
}

#[cfg(windows)]
pub(crate) fn py_path(raw: &str) -> PyPath {
    py_path_windows(raw)
}

/// `PurePath.suffix`: from the name's last dot, unless the dot leads or
/// ends the name.
pub(crate) fn py_suffix(name: &str) -> &str {
    let chars: Vec<(usize, char)> = name.char_indices().collect();
    match chars.iter().rposition(|&(_, c)| c == '.') {
        Some(i) if i > 0 && i + 1 < chars.len() => &name[chars[i].0..],
        _ => "",
    }
}

/// `Path.exists()` (CPython 3.11/3.12): a stat failing with ENOENT,
/// ENOTDIR, EBADF or ELOOP (on Windows, path-not-found and the ignored
/// winerrors), or a path the OS cannot encode (an embedded NUL), is
/// `False`; any other failure raises.
fn py_exists(path: &str) -> Result<bool, std::io::Error> {
    if path.contains('\0') {
        return Ok(false);
    }
    match std::fs::metadata(path) {
        Ok(_) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        #[cfg(unix)]
        Err(e)
            if matches!(
                e.raw_os_error(),
                Some(libc::ENOTDIR | libc::EBADF | libc::ELOOP)
            ) =>
        {
            Ok(false)
        }
        // `_IGNORED_WINERRORS`: not ready, invalid name, symlink loop.
        #[cfg(windows)]
        Err(e) if matches!(e.raw_os_error(), Some(3 | 21 | 123 | 1921)) => Ok(false),
        Err(e) => Err(e),
    }
}

/// `str(OSError)` for a failed `open(path)`: `[Errno N] strerror: 'path'`.
/// Windows renders the OS message instead (`divergences.md`,
/// DOC-OSERROR-TEXT).
fn py_oserror(e: &std::io::Error, filename: &str) -> String {
    match e.raw_os_error() {
        Some(n) => {
            let text = e.to_string();
            let strerror = text
                .strip_suffix(&format!(" (os error {n})"))
                .unwrap_or(&text)
                .to_string();
            format!(
                "[Errno {n}] {strerror}: {}",
                crate::storage::py_repr(filename)
            )
        }
        None => e.to_string(),
    }
}

// ── text ─────────────────────────────────────────────────────────────────

/// `Path.read_text(encoding="utf-8", errors="replace")`: each invalid
/// sequence becomes U+FFFD, then text mode's universal newlines turn
/// `\r\n` and a lone `\r` into `\n`. A byte-order mark stays.
pub(crate) fn decode_text(bytes: &[u8]) -> String {
    let text = String::from_utf8_lossy(bytes);
    if crate::mutants::active("document-raw-newlines") {
        return text.into_owned();
    }
    text.replace("\r\n", "\n").replace('\r', "\n")
}

/// `str.strip()`.
pub(crate) fn py_strip(s: &str) -> &str {
    if crate::mutants::active("document-strip-rust-whitespace") {
        return s.trim();
    }
    s.trim_matches(py_isspace)
}

/// `_extract_html`'s fallback, the one the daemon image serves (it ships no
/// BeautifulSoup): every tag becomes a space, every run of whitespace one
/// space, then strip.
pub(crate) fn strip_html(html: &str) -> String {
    static TAG: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let tag = TAG.get_or_init(|| regex::Regex::new(r"<[^>]+>").expect("static pattern"));
    let clean = tag.replace_all(html, " ");
    let mut out = String::with_capacity(clean.len());
    let mut in_space = false;
    for c in clean.chars() {
        if py_isspace(c) {
            if !in_space {
                out.push(' ');
            }
            in_space = true;
        } else {
            out.push(c);
            in_space = false;
        }
    }
    py_strip(&out).to_string()
}

/// Python's `s[start:stop]` over code points, for `start` in `0..len`.
fn py_slice(chars: &[char], start: usize, stop: i128) -> String {
    let n = chars.len() as i128;
    let stop = if stop < 0 {
        (stop + n).max(0)
    } else {
        stop.min(n)
    };
    if stop <= start as i128 {
        return String::new();
    }
    chars[start..stop as usize].iter().collect()
}

/// `_chunk_text`: windows of `chunk_size * 4` code points every
/// `max(chunk_size * 4 - chunk_overlap * 4, 1)`, each stripped, the empty
/// ones dropped.
pub(crate) fn chunk_text(text: &str, chunk_size: i64, chunk_overlap: i64) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let char_size = i128::from(chunk_size) * 4;
    let overlap = if crate::mutants::active("document-no-overlap") {
        0
    } else {
        i128::from(chunk_overlap) * 4
    };
    let step = (char_size - overlap).max(1);
    let step = usize::try_from(step).unwrap_or(usize::MAX);
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < chars.len() {
        let window = py_slice(&chars, i, i as i128 + char_size);
        let chunk = py_strip(&window);
        if !chunk.is_empty() {
            out.push(chunk.to_string());
        }
        i = match i.checked_add(step) {
            Some(next) => next,
            None => break,
        };
    }
    out
}

/// `chunk_id`: the md5 of `source:index:` and the chunk's first 100 code
/// points, as the chromadb store named its rows.
pub(crate) fn chunk_id(source: &str, index: usize, chunk: &str) -> String {
    let head: String = chunk.chars().take(100).collect();
    let key = format!("{source}:{index}:{head}");
    hex::encode(Md5::digest(key.as_bytes()))
}

/// `_without_nul`: PostgreSQL text and jsonb refuse NUL.
fn without_nul(s: &str) -> String {
    s.replace('\0', "\u{fffd}")
}

/// `extract_text(path)` as `_read_file` runs it.
// The body-slice error type (`dispatch::ToolError`) is the large one.
#[allow(clippy::result_large_err)]
fn extract_text(path: &str, name: &str) -> Result<String, ToolError> {
    let suffix = py_suffix(name).to_lowercase();
    if suffix == ".pdf" {
        return Err(ToolError::refused(
            "not_implemented",
            Some(
                "PDF text extraction is not yet served by the Rust daemon; \
                 ingest .txt, .md or .html, or a PDF through the Python daemon",
            ),
        ));
    }
    // Unwrapped reads raise the OSError itself: a file removed since the
    // `exists()` check is FileNotFoundError, anything else internal.
    let read = || {
        std::fs::read(path).map_err(|e| match e.kind() {
            std::io::ErrorKind::NotFound => ToolError::FileNotFound(py_oserror(&e, path)),
            _ => internal(e),
        })
    };
    if suffix == ".html" || suffix == ".htm" {
        // The fallback's read sits outside the `try` that wraps the parser.
        return Ok(strip_html(&decode_text(&read()?)));
    }
    if TEXT_SUFFIXES.contains(&suffix.as_str()) {
        return Ok(decode_text(&read()?));
    }
    match std::fs::read(path) {
        Ok(bytes) => Ok(decode_text(&bytes)),
        Err(e) => Err(ToolError::Invalid(format!(
            "Cannot read file {name}: {}",
            py_oserror(&e, path)
        ))),
    }
}

// ── storage ──────────────────────────────────────────────────────────────

/// One `search_reference_chunks` row.
pub struct Nearest {
    pub text: String,
    pub source: String,
    pub ingested_at: f64,
    pub distance: f64,
}

/// `search_reference_chunks`: the `limit` nearest chunks by exact cosine
/// distance, ties broken by chunk id in byte order.
pub(crate) async fn nearest(
    db: &tokio_postgres::Client,
    query: &[f32],
    limit: i64,
) -> Result<Vec<Nearest>, tokio_postgres::Error> {
    let order = if crate::mutants::active("document-search-no-id-order") {
        "ORDER BY distance LIMIT $2"
    } else {
        "ORDER BY distance, id COLLATE \"C\" LIMIT $2"
    };
    let sql = format!(
        "SELECT text, source, ingested_at, embedding <=> $1::real[]::vector AS distance \
         FROM reference_chunks {order}"
    );
    let rows = db.query(&sql, &[&query, &limit]).await?;
    Ok(rows
        .iter()
        .map(|r| Nearest {
            text: r.get(0),
            source: r.get(1),
            // `MemoryEntry.__post_init__`: a 0.0 timestamp (the chromadb
            // import's default for a chunk without one) is the clock now.
            ingested_at: match r.get::<_, f64>(2) {
                t if t == 0.0 && !crate::mutants::active("document-zero-clock-kept") => now(),
                t => t,
            },
            distance: r.get(3),
        })
        .collect())
}

/// The memory search's reference pool: `reference.retrieve(q,
/// top_k=ref_k)` with `strict=False`, so `top_k or max_results` chunks and
/// a failed read (a negative LIMIT included) serves none, logged.
pub(crate) async fn reference_pool(
    db: &tokio_postgres::Client,
    query: &[f32],
    cfg: &crate::config::MemoryConfig,
) -> Vec<crate::search::RefHit> {
    if crate::mutants::active("search-no-reference-pool") {
        return Vec::new();
    }
    // `top_k or self.config.max_results`: 0 and null fall through, and a
    // null `max_results` fails `int(limit)` inside the lenient read.
    let limit = match cfg.ref_top_k {
        Some(k) if k != 0 => Some(k),
        _ => cfg.reference.max_results,
    };
    let Some(limit) = limit else {
        eprintln!("reference pool skipped: memory.reference.max_results is null");
        return Vec::new();
    };
    match nearest(db, query, limit).await {
        Ok(rows) => rows
            .into_iter()
            .map(|n| {
                let score = similarity(n.distance);
                crate::search::RefHit { nearest: n, score }
            })
            .collect(),
        Err(e) => {
            eprintln!("reference pool skipped: document query failed: {e}");
            Vec::new()
        }
    }
}

/// `cosine_similarity_from_distance`: `max(0.0, 1 - dist)`, whose tie (and
/// a NaN distance) keeps the first argument.
pub(crate) fn similarity(distance: f64) -> f64 {
    let s = 1.0 - distance;
    if s > 0.0 { s } else { 0.0 }
}

/// `_entry_to_dict` of a reference hit: no row id, the reference band, the
/// chunk's latest ingest as its timestamp, nothing else set. `accesses` is
/// the throwaway entry's count when served: 1 from a memory search that
/// accrues access (`retrieve` bumps every served entry), else 0.
pub(crate) fn hit_json(n: &Nearest, score: f64, accesses: i64) -> Value {
    let mut m = Map::new();
    m.insert("id".into(), Value::Null);
    m.insert("text".into(), json!(n.text));
    m.insert("source".into(), json!(n.source));
    m.insert("bank".into(), json!("reference"));
    m.insert("timestamp".into(), json!(n.ingested_at));
    m.insert("access_count".into(), json!(accesses));
    m.insert("surprise_score".into(), json!(0.0));
    m.insert("superseded".into(), json!(false));
    m.insert("superseded_at".into(), Value::Null);
    m.insert("superseded_by_text".into(), Value::Null);
    m.insert("episode_id".into(), Value::Null);
    m.insert("episode_title".into(), Value::Null);
    m.insert("tags".into(), json!([]));
    m.insert("score".into(), json!(crate::search::round4(score)));
    Value::Object(m)
}

/// One chunk row: id, source, chunk_index, text, embedding, metadata.
type ChunkRow = (String, String, i32, String, Vec<f32>, Value);

/// `upsert_reference_chunks(rows)` with `replace=True`: a chunk with the
/// same id is overwritten except its first `created_at`.
async fn upsert(
    db: &tokio_postgres::Client,
    rows: &[ChunkRow],
    now: f64,
) -> Result<(), tokio_postgres::Error> {
    let conflict = if crate::mutants::active("document-upsert-keeps-old") {
        "DO NOTHING"
    } else if crate::mutants::active("document-upsert-clock-only") {
        "DO UPDATE SET ingested_at = EXCLUDED.ingested_at"
    } else {
        "DO UPDATE SET source = EXCLUDED.source, chunk_index = EXCLUDED.chunk_index, \
         text = EXCLUDED.text, embedding = EXCLUDED.embedding, \
         metadata = EXCLUDED.metadata, ingested_at = EXCLUDED.ingested_at"
    };
    let sql = format!(
        "INSERT INTO reference_chunks (id, source, chunk_index, text, embedding, metadata, \
         ingested_at, created_at) VALUES ($1, $2, $3, $4, $5::real[]::vector, $6, $7, $7) \
         ON CONFLICT (id) {conflict}"
    );
    let stmt = db.prepare(&sql).await?;
    for (id, source, index, text, embedding, metadata) in rows {
        db.execute(&stmt, &[id, source, index, text, embedding, metadata, &now])
            .await?;
    }
    Ok(())
}

// ── tools ────────────────────────────────────────────────────────────────

/// Orders ingests among themselves; tokio's mutex is fair (FIFO).
fn ingest_lock() -> &'static tokio::sync::Mutex<()> {
    static LOCK: std::sync::OnceLock<tokio::sync::Mutex<()>> = std::sync::OnceLock::new();
    LOCK.get_or_init(|| tokio::sync::Mutex::new(()))
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn mcp_document_ingest<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let raw = a.get("path").and_then(Value::as_str).unwrap_or_default();
        let source = a.get("source").and_then(Value::as_str).map(str::to_string);
        let svc = app.service.clone();
        let ready = svc.ensure_init().await.map_err(internal)?;
        let path = py_path(raw);
        let reference = svc.config.memory.reference.clone();
        let r2 = ready.clone();
        // Admitted ingests outlive their callers, as every write does.
        ready
            .writes
            .spawn(async move {
                // One ingest at a time, in arrival order (a fair lock), as
                // Python's service lock orders them; other calls are not
                // held (`divergences.md`, DOC-INGEST-LOCK).
                let _ingest = ingest_lock().lock().await;
                let shown = path.shown.clone();
                let exists = tokio::task::spawn_blocking(move || py_exists(&shown))
                    .await
                    .map_err(internal)?
                    .map_err(internal)?;
                if !exists {
                    return Err(ToolError::FileNotFound(format!(
                        "Not found on the server's filesystem: {}. The path resolves where \
                         the daemon runs (with Docker, inside the container, e.g. a mounted \
                         volume), not on the client.",
                        path.shown
                    )));
                }
                // `source or file_path.name`: an empty source names the file too.
                let src = source
                    .filter(|s| !s.is_empty())
                    .unwrap_or(path.name.clone());
                let shown = path.shown.clone();
                let name = path.name.clone();
                let embedder = r2.embedder.clone();
                #[allow(clippy::result_large_err)]
                let read_and_embed = move || {
                    let text = extract_text(&shown, &name)?;
                    let chunks = chunk_text(&text, reference.chunk_size, reference.chunk_overlap);
                    let mut embeddings = Vec::with_capacity(chunks.len());
                    for slice in chunks.chunks(INGEST_ENCODE_BATCH) {
                        embeddings.extend(embedder.encode(slice, true).map_err(internal)?);
                    }
                    Ok::<_, ToolError>((chunks, embeddings))
                };
                let (chunks, embeddings) = tokio::task::spawn_blocking(read_and_embed)
                    .await
                    .map_err(internal)??;
                let total = chunks.len();
                if total > 0 {
                    let now = now();
                    let stored_source = without_nul(&src);
                    let mut rows = Vec::with_capacity(total);
                    for (i, (chunk, embedding)) in chunks.iter().zip(embeddings).enumerate() {
                        // `int(chunk_index)` into INTEGER: past it the write fails.
                        let index = i32::try_from(i).map_err(internal)?;
                        rows.push((
                            chunk_id(&src, i, chunk),
                            stored_source.clone(),
                            index,
                            without_nul(chunk),
                            embedding,
                            json!({"source": stored_source, "chunk_index": i, "timestamp": now}),
                        ));
                    }
                    let _ws = r2.write.lock().await;
                    crate::txn::run(r2.storage.client(), |db| async move {
                        upsert(db, &rows, now).await
                    })
                    .await
                    .map_err(internal)?;
                }
                Ok(json!({"source": src, "chunks_stored": total, "chunks_total": total}))
            })
            .await
            .map_err(internal)?
    })
}

fn mcp_document_search<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let a = call.args;
        let query = a
            .get("query")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        // Bound `ge=1`; past i64 PostgreSQL refuses the LIMIT, as for Python.
        let top_k = a
            .get("top_k")
            .and_then(Value::as_i64)
            .ok_or_else(|| internal("top_k is out of range for a LIMIT"))?;
        let svc = app.service.clone();
        let ready = svc.ensure_init().await.map_err(internal)?;
        let embedder = ready.embedder.clone();
        let cache_size = svc.config.embedding.cache_size;
        let q = tokio::task::spawn_blocking(move || {
            crate::read::embed_cache::encode_query(&embedder, &query, cache_size)
        })
        .await
        .map_err(internal)?
        .map_err(ToolError::Internal)?;
        let hits = crate::txn::with_client(ready.storage.client(), |db| async move {
            nearest(db, &q, top_k).await
        })
        .await
        .map_err(internal)?;
        let entries: Vec<Value> = hits
            .iter()
            .map(|n| hit_json(n, similarity(n.distance), 0))
            .collect();
        Ok(json!({"count": entries.len(), "entries": entries}))
    })
}

#[cfg(test)]
#[path = "documents_tests.rs"]
mod tests;
