//! A PDF's text as the Python daemon reads it: `document_parser._extract_pdf`
//! through pypdfium2 5.14.0, whose wheels bundle the PDFium build this
//! daemon pins (`pdfium.rs`). The calls are pypdfium2's own, made through
//! pdfium-render's raw bindings: `PdfDocument(data)`, then per page
//! `get_textpage().get_text_range()`, the pages joined by a blank line.
//! pdfium-render's own text API differs (it reads a line-end hyphen as
//! `\x02` where `FPDFText_GetText` gives U+FFFE, and drops some breaks).

// Exception to the crate's safe-code rule: pdfium-render's raw bindings are
// unsafe FFI into PDFium; every call runs under BOUND's lock, and each
// document, page and text-page handle is closed exactly once.
#![allow(unsafe_code)]

use std::path::Path;
use std::sync::Mutex;

use pdfium_render::prelude::{
    FPDF_DOCUMENT, FPDF_LIBRARY_CONFIG, FPDF_TEXTPAGE, Pdfium, PdfiumLibraryBindings,
};

/// Why a PDF yields no text.
#[derive(Debug, PartialEq)]
pub enum PdfError {
    /// No usable library: the `pdf_engine_unavailable` refusal.
    Unavailable(String),
    /// pypdfium2's `PdfiumError` text, which Python raises as
    /// `ValueError("Failed to read PDF: …")`.
    Unreadable(String),
    /// An internal failure (pypdfium2's own assertion), not the file's.
    Internal(String),
}

/// pypdfium2's `ErrorToStr`, a `_fallback_dict` over `FPDF_GetLastError`.
pub fn error_to_str(code: u64) -> String {
    let code = if crate::mutants::active("document-pdf-error-names-shifted") {
        code + 1
    } else {
        code
    };
    match code {
        0 => "Success".into(),
        1 => "Unknown error".into(),
        2 => "File access error".into(),
        3 => "Data format error".into(),
        4 => "Incorrect password error".into(),
        5 => "Unsupported security scheme error".into(),
        6 => "Page not found or content error".into(),
        other => format!("Unhandled constant {other}"),
    }
}

/// What one read takes from a PDF. Pages can share one content stream and
/// text can be compressed, so a file under the 64 MiB input cap can still
/// expand far past it; a PDF past either bound is refused, not truncated.
#[derive(Clone, Copy, Debug)]
pub struct Budget {
    pub pages: i32,
    /// UTF-8 bytes of the joined text.
    pub text_bytes: usize,
}

/// Ten times the corpus's 1,000-page hostile file, and the input cap's
/// 64 MiB (`documents::MAX_DOCUMENT_BYTES`) for the text.
pub const BUDGET: Budget = Budget {
    pages: 10_000,
    text_bytes: 64 * 1024 * 1024,
};

/// The bound library, bound once per process from a verified install; the
/// lock also serializes every call (PDFium is not thread-safe).
static BOUND: Mutex<Option<Box<dyn PdfiumLibraryBindings>>> = Mutex::new(None);

/// The text of a PDF held in `data`, or why there is none.
pub fn extract(data: &[u8]) -> Result<String, PdfError> {
    extract_within(data, BUDGET)
}

/// [`extract`] under `budget`.
pub fn extract_within(data: &[u8], budget: Budget) -> Result<String, PdfError> {
    let mut bound = BOUND.lock().unwrap_or_else(|p| p.into_inner());
    if bound.is_none() {
        let path = super::pdfium::library().map_err(PdfError::Unavailable)?;
        *bound = Some(bind(&path).map_err(PdfError::Unavailable)?);
    }
    let bindings = bound.as_deref().expect("bound above");
    // SAFETY: the bindings come from the pinned, hash-checked library and
    // are used only under BOUND's lock; every handle is opened and closed
    // within this call.
    unsafe { text_of(bindings, data, budget) }
}

fn bind(path: &Path) -> Result<Box<dyn PdfiumLibraryBindings>, String> {
    let bindings = Pdfium::bind_to_library(path).map_err(|e| {
        super::pdfium::unavailable(
            &format!("the library at {} cannot be loaded ({e:?})", path.display()),
            None,
        )
    })?;
    // pypdfium2's `init_lib`: a version 2 config, nothing else set.
    let config = FPDF_LIBRARY_CONFIG {
        version: 2,
        m_pUserFontPaths: std::ptr::null_mut(),
        m_pIsolate: std::ptr::null_mut(),
        m_v8EmbedderSlot: 0,
        m_pPlatform: std::ptr::null_mut(),
        m_RendererType: 0,
        m_FontLibraryType: 0,
    };
    // SAFETY: the config outlives the call, as PDFium requires.
    unsafe { bindings.FPDF_InitLibraryWithConfig(&config) };
    Ok(bindings)
}

/// `PdfDocument(data)` and its pages' text, closing every handle.
///
/// # Safety
/// `b` must be initialized bindings used by one thread at a time.
unsafe fn text_of(
    b: &dyn PdfiumLibraryBindings,
    data: &[u8],
    budget: Budget,
) -> Result<String, PdfError> {
    let unreadable = |m: &str| PdfError::Unreadable(m.to_string());
    unsafe {
        let doc = b.FPDF_LoadMemDocument64(data, None);
        // `_open_pdf`: fewer than one page fails the load, whatever the
        // handle (a null one counts no pages).
        if b.FPDF_GetPageCount(doc) < 1 {
            // c_ulong: u32 on Windows, u64 elsewhere, where the conversion
            // is the identity.
            #[allow(clippy::useless_conversion)]
            let code = u64::from(b.FPDF_GetLastError());
            if !doc.is_null() {
                b.FPDF_CloseDocument(doc);
            }
            return Err(unreadable(&format!(
                "Failed to load document (PDFium: {}).",
                error_to_str(code)
            )));
        }
        let result = pages(b, doc, budget);
        b.FPDF_CloseDocument(doc);
        result
    }
}

/// `"\n\n".join(page text for page in pdf)`.
unsafe fn pages(
    b: &dyn PdfiumLibraryBindings,
    doc: FPDF_DOCUMENT,
    budget: Budget,
) -> Result<String, PdfError> {
    let mut texts = Vec::new();
    let mut joined = 0usize;
    unsafe {
        let mut n_pages = b.FPDF_GetPageCount(doc);
        if n_pages > budget.pages {
            return Err(PdfError::Unreadable(format!(
                "the PDF has {n_pages} pages, more than the {} one ingest reads; split it into smaller files",
                budget.pages
            )));
        }
        if crate::mutants::active("document-pdf-first-page-only") {
            n_pages = n_pages.min(1);
        }
        for i in 0..n_pages {
            let page = b.FPDF_LoadPage(doc, i);
            if page.is_null() {
                return Err(PdfError::Unreadable("Failed to load page.".into()));
            }
            let text_page = b.FPDFText_LoadPage(page);
            if text_page.is_null() {
                b.FPDF_ClosePage(page);
                return Err(PdfError::Unreadable("Failed to load text page.".into()));
            }
            let separator = if texts.is_empty() { 0 } else { 2 };
            let remaining = budget.text_bytes.saturating_sub(joined + separator);
            let text = text_range(b, text_page, remaining, budget.text_bytes);
            b.FPDFText_ClosePage(text_page);
            b.FPDF_ClosePage(page);
            let text = text?;
            joined += text.len() + separator;
            if joined > budget.text_bytes {
                return Err(text_budget_passed(budget.text_bytes));
            }
            texts.push(text);
        }
    }
    if crate::mutants::active("document-pdf-pages-one-newline") {
        return Ok(texts.join("\n"));
    }
    Ok(texts.join("\n\n"))
}

fn text_budget_passed(text_bytes: usize) -> PdfError {
    PdfError::Unreadable(format!(
        "the PDF's text passes {text_bytes} bytes, more than one ingest reads; split it into smaller files"
    ))
}

/// `PdfTextPage.get_text_range()` with its defaults (`index=0, count=-1,
/// errors="ignore"`). A page whose text could not fit in the `remaining`
/// bytes of the budget is refused before its buffer is allocated: every
/// UTF-16 unit kept decodes to at least one UTF-8 byte.
unsafe fn text_range(
    b: &dyn PdfiumLibraryBindings,
    tp: FPDF_TEXTPAGE,
    remaining: usize,
    text_bytes: usize,
) -> Result<String, PdfError> {
    unsafe {
        let n_chars = b.FPDFText_CountChars(tp);
        if n_chars == -1 {
            return Err(PdfError::Unreadable(
                "Failed to get character count.".into(),
            ));
        }
        // `_get_active_text_range`: drop excluded characters at either end.
        // On a whole page this changes no text (removing it was an
        // equivalent mutant over the corpus); it keeps the calls
        // pypdfium2's, which matters on its partial ranges (crbug 2079).
        let (mut c_start, mut c_end, mut l_passive, mut r_passive) = (0, n_chars - 1, 0, 0);
        let (t_start, t_end) = loop {
            if c_start > c_end {
                return Ok(String::new());
            }
            let t_start = b.FPDFText_GetTextIndexFromCharIndex(tp, c_start);
            if t_start == -1 {
                c_start += 1;
                l_passive += 1;
                continue;
            }
            let t_end = b.FPDFText_GetTextIndexFromCharIndex(tp, c_end);
            if t_end == -1 {
                c_end -= 1;
                r_passive += 1;
                continue;
            }
            break (t_start, t_end);
        };
        let index = l_passive;
        let count = n_chars - l_passive - r_passive;
        // pypdfium2's size estimate, which its assertion below checks; the
        // buffer itself is at least PDFium's contract for FPDFText_GetText
        // (`count + 1` UTF-16 units), so no PDFium build can write past it.
        let in_count = usize::try_from(t_end + 2 - t_start).unwrap_or(0);
        let contract = usize::try_from(count).unwrap_or(0) + 1;
        if contract - 1 > remaining {
            return Err(text_budget_passed(text_bytes));
        }
        let mut buffer = vec![0u16; in_count.max(contract)];
        let out_count =
            usize::try_from(b.FPDFText_GetText(tp, index, count, buffer.as_mut_ptr())).unwrap_or(0);
        // pypdfium2 asserts the buffer held it.
        if out_count > in_count {
            return Err(PdfError::Internal(format!(
                "Buffer too small: {in_count} vs {out_count}"
            )));
        }
        // `decode(buffer[:out_count-1], "utf-16-le", errors="ignore")`.
        let units = &buffer[..out_count.saturating_sub(1)];
        Ok(char::decode_utf16(units.iter().copied())
            .filter_map(Result::ok)
            .collect())
    }
}

#[cfg(test)]
#[path = "pdf_text_tests.rs"]
mod tests;
