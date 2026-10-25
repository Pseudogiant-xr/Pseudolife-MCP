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
    // A text suffix and HTML read outside any `try`: the OSError itself.
    assert!(matches!(
        extract_text(&at("folder.txt"), "folder.txt"),
        Err(ToolError::Internal(_))
    ));
    assert!(matches!(
        extract_text(&at("folder.html"), "folder.html"),
        Err(ToolError::Internal(_))
    ));
    // Any other suffix wraps it: `Cannot read file {name}: {e}`.
    match extract_text(&at("folder.data"), "folder.data") {
        Err(ToolError::Invalid(m)) => {
            assert!(m.starts_with("Cannot read file folder.data: "), "{m}");
            #[cfg(unix)]
            assert_eq!(
                m,
                format!(
                    "Cannot read file folder.data: [Errno 21] Is a directory: '{}'",
                    at("folder.data")
                )
            );
        }
        other => panic!("{other:?}"),
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

#[test]
fn similarity_clamps_like_python_max() {
    assert_eq!(similarity(0.25), 0.75);
    assert_eq!(similarity(1.0), 0.0);
    assert_eq!(similarity(1.5), 0.0);
    assert_eq!(similarity(f64::NAN), 0.0);
    assert!(similarity(1.0).is_sign_positive());
}
