//! The document store's text path against the Python oracle's own values
//! (`harness/gen_document_goldens.py` writes `goldens/documents.json`).

use super::*;
use serde_json::Value;

const GOLDEN: &str = include_str!("../harness/goldens/documents.json");

fn golden(kind: &str) -> Vec<Value> {
    let all: Value = serde_json::from_str(GOLDEN).expect("golden json");
    all[kind].as_array().expect("golden kind").clone()
}

fn s(v: &Value) -> &str {
    v.as_str().expect("string")
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .expect("array")
        .iter()
        .map(|x| s(x).to_string())
        .collect()
}

#[test]
fn decode_matches_read_text() {
    for case in golden("decode") {
        let bytes = hex::decode(s(&case["hex"])).unwrap();
        assert_eq!(
            decode_text(&bytes),
            s(&case["text"]),
            "bytes {}",
            case["hex"]
        );
    }
}

#[test]
fn html_matches_the_regex_fallback() {
    for case in golden("html") {
        assert_eq!(
            strip_html(s(&case["html"])),
            s(&case["text"]),
            "{}",
            case["html"]
        );
    }
}

#[test]
fn strip_matches_str_strip() {
    for case in golden("strip") {
        assert_eq!(py_strip(s(&case["s"])), s(&case["out"]), "{:?}", case["s"]);
    }
}

#[test]
fn chunks_match_chunk_text() {
    let cases = golden("chunk");
    assert!(cases.len() > 50);
    for case in cases {
        let cs = case["chunk_size"].as_i64().unwrap();
        let co = case["chunk_overlap"].as_i64().unwrap();
        assert_eq!(
            chunk_text(s(&case["text"]), cs, co),
            strings(&case["chunks"]),
            "chunk_size {cs} overlap {co} text {:.60}",
            case["text"]
        );
    }
}

#[test]
fn chunk_ids_match() {
    for case in golden("chunk_id") {
        let index = case["index"].as_u64().unwrap() as usize;
        assert_eq!(
            chunk_id(s(&case["source"]), index, s(&case["chunk"])),
            s(&case["id"]),
            "{case}"
        );
    }
}

type PathParser = fn(&str) -> PyPath;

#[test]
fn paths_match_pathlib() {
    // The Windows parse is checked on every OS; the native one on its own.
    let mut parsers: Vec<(&str, PathParser)> = vec![("path_windows", py_path_windows)];
    if !cfg!(windows) {
        parsers.push(("path_posix", py_path));
    }
    for (kind, parse) in parsers {
        check_paths(kind, parse);
    }
}

fn check_paths(kind: &str, parse: PathParser) {
    for case in golden(kind) {
        let p = parse(s(&case["raw"]));
        assert_eq!(p.shown, s(&case["shown"]), "raw {:?}", case["raw"]);
        assert_eq!(p.name, s(&case["name"]), "raw {:?}", case["raw"]);
        assert_eq!(
            py_suffix(&p.name),
            s(&case["suffix"]),
            "raw {:?}",
            case["raw"]
        );
    }
}

#[test]
fn files_extract_chunk_and_name_as_ingest_does() {
    let dir = std::env::temp_dir().join(format!("pl-documents-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    for case in golden("file") {
        let name = s(&case["name"]);
        let path = dir.join(name);
        std::fs::write(&path, hex::decode(s(&case["hex"])).unwrap()).unwrap();
        let text = match extract_text(path.to_str().unwrap(), name) {
            Ok(t) => t,
            Err(e) => panic!("{name}: {e:?}"),
        };
        assert_eq!(text, s(&case["text"]), "{name}");
        let chunks = chunk_text(&text, 512, 64);
        let want = case["chunks"].as_array().unwrap();
        assert_eq!(chunks.len(), want.len(), "{name}");
        for (i, (got, w)) in chunks.iter().zip(want).enumerate() {
            assert_eq!(got, s(&w["text"]), "{name} chunk {i}");
            assert_eq!(
                chunk_id(&format!("src-{name}"), i, got),
                s(&w["id"]),
                "{name} id {i}"
            );
        }
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn errors_take_python_classes() {
    let dir = std::env::temp_dir().join(format!("pl-documents-err-{}", std::process::id()));
    std::fs::create_dir_all(dir.join("folder.txt")).unwrap();
    std::fs::create_dir_all(dir.join("folder.data")).unwrap();
    std::fs::create_dir_all(dir.join("folder.html")).unwrap();
    std::fs::write(dir.join("paper.PDF"), b"%PDF-1.4").unwrap();
    let at = |n: &str| dir.join(n).to_str().unwrap().to_string();
    // A directory reached past the service's check: on POSIX it opens, and
    // the handle's check refuses it, whatever the suffix (`read_document_bytes`,
    // as `document_parser.read_document_bytes`).
    #[cfg(unix)]
    for name in ["folder.txt", "folder.html", "folder.data"] {
        match extract_text(&at(name), name) {
            Err(ToolError::Invalid(m)) => assert_eq!(
                m,
                format!(
                    "not_a_regular_file: {} is a directory; document_ingest reads one regular \
                     file, not a directory, FIFO, socket or device",
                    at(name)
                )
            ),
            other => panic!("{name}: {other:?}"),
        }
    }
    // On Windows the open fails: a text suffix and HTML raise the OSError
    // itself, any other suffix wraps it (`Cannot read file {name}: {e}`).
    #[cfg(windows)]
    {
        assert!(matches!(
            extract_text(&at("folder.txt"), "folder.txt"),
            Err(ToolError::Internal(_))
        ));
        assert!(matches!(
            extract_text(&at("folder.html"), "folder.html"),
            Err(ToolError::Internal(_))
        ));
        match extract_text(&at("folder.data"), "folder.data") {
            Err(ToolError::Invalid(m)) => {
                assert!(m.starts_with("Cannot read file folder.data: "), "{m}")
            }
            other => panic!("{other:?}"),
        }
    }
    // A text-suffix file gone since `exists()`: FileNotFoundError's text.
    match extract_text(&at("gone.md"), "gone.md") {
        Err(ToolError::FileNotFound(m)) => {
            #[cfg(unix)]
            assert_eq!(
                m,
                format!("[Errno 2] No such file or directory: '{}'", at("gone.md"))
            );
            assert!(!m.is_empty());
        }
        other => panic!("{other:?}"),
    }
    // PDF is the declared refusal, whatever the suffix's case.
    match extract_text(&at("paper.PDF"), "paper.PDF") {
        Err(ToolError::Refused { code, .. }) => assert_eq!(code, "not_implemented"),
        other => panic!("{other:?}"),
    }
    assert!(!py_exists(&at("missing.txt")).unwrap());
    assert!(!py_exists(&at("folder.data/inside")).unwrap() || cfg!(windows));
    assert!(!py_exists("nul\0byte").unwrap());
    assert!(py_exists(&at("folder.txt")).unwrap());
    let _ = std::fs::remove_dir_all(&dir);
}

// `document_file_problem` (`reference_bank.py`): only a regular file of at
// most the cap is read, with Python's `code: detail` text.
#[test]
fn only_a_regular_file_within_the_cap_is_read() {
    let dir = std::env::temp_dir().join(format!("pl-documents-kind-{}", std::process::id()));
    std::fs::create_dir_all(dir.join("notes.md")).unwrap();
    std::fs::write(dir.join("at-cap.txt"), vec![b'x'; 1024]).unwrap();
    std::fs::write(dir.join("over-cap.txt"), vec![b'x'; 1025]).unwrap();
    let at = |n: &str| dir.join(n).to_str().unwrap().to_string();
    let invalid = |r: Result<(), ToolError>| match r {
        Err(ToolError::Invalid(m)) => m,
        other => panic!("{other:?}"),
    };
    let m = invalid(document_file_problem(&at("notes.md"), 1024));
    assert_eq!(
        m,
        format!(
            "not_a_regular_file: {} is a directory; document_ingest reads one regular file, \
             not a directory, FIFO, socket or device",
            at("notes.md")
        )
    );
    // The near-miss legitimate call: a normal file of exactly the cap.
    assert!(document_file_problem(&at("at-cap.txt"), 1024).is_ok());
    let m = invalid(document_file_problem(&at("over-cap.txt"), 1024));
    assert_eq!(
        m,
        format!(
            "document_too_large: {} is 1025 bytes; document_ingest reads files of at most \
             1024 bytes: split the document or ingest the part you need",
            at("over-cap.txt")
        )
    );
    // The read itself: within the cap it returns the bytes; the open
    // handle's metadata is checked again.
    assert_eq!(
        read_document_bytes(&at("at-cap.txt"), 1024).unwrap().len(),
        1024
    );
    assert!(matches!(
        read_document_bytes(&at("over-cap.txt"), 1024),
        Err(ReadError::Refused(m)) if m.contains(" is 1025 bytes; ")
    ));
    #[cfg(unix)]
    {
        // A FIFO is refused from its metadata, never opened (opening one
        // for reading blocks until a writer appears).
        let fifo = dir.join("pipe.txt");
        let made = std::process::Command::new("mkfifo")
            .arg(&fifo)
            .status()
            .unwrap();
        assert!(made.success());
        let m = invalid(document_file_problem(fifo.to_str().unwrap(), 1024));
        assert!(m.contains(" is a FIFO; "), "{m}");
        // A FIFO swapped in after that check: the read opens it without
        // blocking and refuses it from the handle.
        assert!(matches!(
            read_document_bytes(fifo.to_str().unwrap(), 1024),
            Err(ReadError::Refused(m)) if m.contains(" is a FIFO; ")
        ));
        std::os::unix::fs::symlink(dir.join("at-cap.txt"), dir.join("link.txt")).unwrap();
        assert!(document_file_problem(&at("link.txt"), 1024).is_ok());
    }
    // A `/proc` entry stats as an empty regular file and reads on: the
    // read stops one byte past the cap.
    if std::path::Path::new("/proc/self/status").exists() {
        assert!(matches!(
            read_document_bytes("/proc/self/status", 100),
            Err(ReadError::Refused(m))
                if m.starts_with("document_too_large: /proc/self/status holds more than 100 bytes")
        ));
        // pagemap reports no size and holds the whole address space (it
        // ran a plain read out of memory): cut at the cap, as in Python.
        assert!(matches!(
            read_document_bytes("/proc/self/pagemap", 1 << 20),
            Err(ReadError::Refused(m))
                if m.starts_with("document_too_large: /proc/self/pagemap holds more than")
        ));
    }
    assert_eq!(MAX_DOCUMENT_BYTES, 64 * 1024 * 1024);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn similarity_clamps_like_python_max() {
    assert_eq!(similarity(0.25), 0.75);
    assert_eq!(similarity(1.0), 0.0);
    assert_eq!(similarity(1.5), 0.0);
    assert_eq!(similarity(f64::NAN), 0.0);
    assert!(similarity(1.0).is_sign_positive());
}
