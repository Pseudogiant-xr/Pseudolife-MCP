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

/// A PDF with these objects (object 1 the catalog) and a correct xref.
fn well_formed(objects: &[&[u8]]) -> Vec<u8> {
    let mut out = b"%PDF-1.7\n".to_vec();
    let mut offsets = Vec::new();
    for (i, body) in objects.iter().enumerate() {
        offsets.push(out.len());
        out.extend_from_slice(format!("{} 0 obj\n", i + 1).as_bytes());
        out.extend_from_slice(body);
        out.extend_from_slice(b"\nendobj\n");
    }
    let xref = out.len();
    out.extend_from_slice(
        format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
    );
    for off in offsets {
        out.extend_from_slice(format!("{off:010} 00000 n \n").as_bytes());
    }
    out.extend_from_slice(
        format!(
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
            objects.len() + 1
        )
        .as_bytes(),
    );
    out
}

#[test]
fn a_zero_page_document_that_loads_still_fails_as_pypdfium2_fails_it() {
    // `_open_pdf` raises on fewer than one page even when the load
    // succeeded. The reason is whatever FPDF_GetLastError then holds, which
    // depends on the process's (on Windows, the thread's) earlier PDFium
    // calls on both daemons, so only the prefix is fixed.
    let pdf = well_formed(&[
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [] /Count 0 >>",
    ]);
    match read(&pdf) {
        Err(PdfError::Unreadable(why)) => {
            assert!(
                why.starts_with("Failed to load document (PDFium: "),
                "{why}"
            )
        }
        other => panic!("{other:?}"),
    }
    // The branch is the loaded-but-empty one: PDFium opens the document.
    let bound = BOUND.lock().unwrap_or_else(|p| p.into_inner());
    let b = bound.as_deref().expect("bound by read");
    // SAFETY: the bound library, under its lock; the handle is closed
    // before the page count is asserted, so a failure leaks nothing.
    let pages = unsafe {
        let doc = b.FPDF_LoadMemDocument64(&pdf, None);
        assert!(!doc.is_null());
        let pages = b.FPDF_GetPageCount(doc);
        b.FPDF_CloseDocument(doc);
        pages
    };
    assert_eq!(pages, 0);
}

#[test]
fn the_goldens_cover_exactly_the_corpus() {
    let golden: Value = serde_json::from_str(GOLDEN).expect("golden json");
    let mut in_golden: Vec<String> = golden["files"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["file"].as_str().unwrap().to_string())
        .collect();
    in_golden.sort();
    let dir = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("harness")
        .join("pdf_corpus");
    let mut on_disk: Vec<String> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.ends_with(".pdf"))
        .collect();
    on_disk.sort();
    assert_eq!(
        on_disk, in_golden,
        "regenerate goldens/pdf_text.json with harness/gen_pdf_goldens.py"
    );
}
