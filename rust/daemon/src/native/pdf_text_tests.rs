//! PDF text against the Python daemon's own output for every file of
//! `harness/pdf_corpus/` (`harness/gen_pdf_goldens.py` writes
//! `goldens/pdf_text.json` from `document_parser.extract_text` under the
//! pypdfium2 the image locks). These tests need the pinned PDFium library:
//! CI provisions it, and locally `pseudolife-daemon provision pdfium` (or
//! `PSEUDOLIFE_DAEMON_PDFIUM_DIR` naming a provisioned root) does.

use super::*;
use serde_json::Value;

const GOLDEN: &str = include_str!("../../harness/goldens/pdf_text.json");

/// Declared divergences (`divergences.md`): the oracle's outcome differs
/// and this is what the Rust daemon reads instead.
const DECLARED: &[(&str, &str)] = &[
    // DOC-PDF-RECURSION: pypdfium2's recursive edge trim exhausts Python's
    // stack on 1,100 excluded characters; the loop here reads the page.
    ("hostile-excluded-chars.pdf", "visible"),
];

fn corpus(name: &str) -> Vec<u8> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("harness")
        .join("pdf_corpus")
        .join(name);
    std::fs::read(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

fn sha256(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(bytes))
}

/// `extract`, failing loudly when the library is not provisioned: these
/// tests never pass by skipping.
fn read(data: &[u8]) -> Result<String, PdfError> {
    let out = extract(data);
    if let Err(PdfError::Unavailable(why)) = &out {
        panic!(
            "the PDF tests need the pinned PDFium library: run `cargo run -p pseudolife-daemon \
             -- provision pdfium` (or set PSEUDOLIFE_DAEMON_PDFIUM_DIR to a provisioned root): {why}"
        );
    }
    out
}

#[test]
fn error_codes_read_as_pypdfium2_names_them() {
    let names: Vec<String> = (0..=7).map(error_to_str).collect();
    assert_eq!(
        names,
        [
            "Success",
            "Unknown error",
            "File access error",
            "Data format error",
            "Incorrect password error",
            "Unsupported security scheme error",
            "Page not found or content error",
            "Unhandled constant 7",
        ]
    );
}

#[test]
fn every_corpus_pdf_reads_as_the_python_daemon_reads_it() {
    let golden: Value = serde_json::from_str(GOLDEN).expect("golden json");
    let files = golden["files"].as_array().expect("files");
    assert!(files.len() >= 28, "{} files", files.len());
    for entry in files {
        let name = entry["file"].as_str().unwrap();
        let data = corpus(name);
        assert_eq!(
            sha256(&data),
            entry["sha256"].as_str().unwrap(),
            "{name} bytes"
        );
        let got = read(&data);
        if let Some((_, text)) = DECLARED.iter().find(|(f, _)| *f == name) {
            assert_eq!(got, Ok(text.to_string()), "{name} (declared)");
            assert!(
                entry.get("error").is_some(),
                "{name}: no longer a divergence"
            );
            continue;
        }
        match (entry.get("text"), entry["error"].as_str()) {
            (Some(text), _) => assert_eq!(got, Ok(text.as_str().unwrap().to_string()), "{name}"),
            (None, Some("ValueError")) => {
                let message = entry["message"].as_str().unwrap();
                let why = message
                    .strip_prefix("Failed to read PDF: ")
                    .unwrap_or_else(|| panic!("{name}: {message}"));
                assert_eq!(got, Err(PdfError::Unreadable(why.to_string())), "{name}");
            }
            other => panic!("{name}: an oracle outcome this test does not map: {other:?}"),
        }
    }
}

#[test]
fn a_zero_page_document_fails_as_pypdfium2_fails_it() {
    // `_open_pdf` raises on fewer than one page even when the load
    // succeeded, with whatever FPDF_GetLastError then says.
    let pdf = b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n\
2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\n\
trailer << /Root 1 0 R >>\n%%EOF\n";
    match read(pdf) {
        Err(PdfError::Unreadable(why)) => {
            assert!(
                why.starts_with("Failed to load document (PDFium: "),
                "{why}"
            )
        }
        other => panic!("{other:?}"),
    }
}
