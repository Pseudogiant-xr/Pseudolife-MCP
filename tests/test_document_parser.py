"""Document parser extraction paths — pypdfium2 preferred, pypdf fallback.

The PDF fixture is handcrafted (offsets computed, no writer dependency) so
the tests run offline; the default install reads PDFs with pypdfium2.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from pseudolife_memory.memory.document_parser import extract_text

ROOT = Path(__file__).resolve().parents[1]


def _make_minimal_pdf(path, text: str) -> None:
    """Write a minimal single-page PDF with `text` in Helvetica."""
    content = f"BT /F1 24 Tf 72 712 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode()
        + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))


def test_extract_text_plain(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello memory", encoding="utf-8")
    assert extract_text(p) == "hello memory"


def test_extract_pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    _make_minimal_pdf(p, "Pseudolife pdfium test")
    assert "Pseudolife pdfium test" in extract_text(p)


def test_extract_pdf_pypdf_fallback(tmp_path, monkeypatch):
    """With pypdfium2 unavailable, the pypdf path must still extract."""
    p = tmp_path / "doc.pdf"
    _make_minimal_pdf(p, "fallback path test")

    real_import = builtins.__import__

    def no_pdfium(name, *args, **kwargs):
        if name == "pypdfium2":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pdfium)
    assert "fallback path test" in extract_text(p)


def test_a_pdf_pdfium_cannot_open_is_refused_as_a_read_failure(tmp_path):
    """PDFium's own error reaches the caller as the ValueError every other
    unreadable document gives, not as an unexpected exception."""
    pytest.importorskip("pypdfium2")
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"not a pdf at all")
    with pytest.raises(ValueError) as caught:
        extract_text(p)
    assert str(caught.value) == "Failed to read PDF: Failed to load document (PDFium: Data format error)."


def test_a_pdfium_whose_library_cannot_load_falls_back_to_pypdf(tmp_path, monkeypatch):
    """An installed pypdfium2 whose bundled library fails to load (ctypes
    raises OSError on import) leaves pypdf to read the file."""
    p = tmp_path / "doc.pdf"
    _make_minimal_pdf(p, "library load fallback")
    real_import = builtins.__import__

    def broken_pdfium(name, *args, **kwargs):
        if name == "pypdfium2":
            raise OSError("cannot load library 'pdfium.dll'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_pdfium)
    assert "library load fallback" in extract_text(p)


def _name(requirement: str) -> str:
    head = requirement.split(">")[0].split("<")[0].split("=")[0].split("[")[0]
    return head.strip().lower().replace("_", "-")


def test_pypdfium2_is_a_core_dependency_pinned_exactly():
    """Every install extracts PDF text with the same PDFium (2026-10-26):
    exactly pypdfium2 5.14.0, in pyproject and in the image lock, whose
    wheels bundle the build the Rust daemon pins; ``pdf`` stays as an
    accepted no-op extra."""
    tomllib = pytest.importorskip("tomllib", reason="needs Python 3.11")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = [r for r in project["dependencies"] if _name(r) == "pypdfium2"]
    assert core == ["pypdfium2==5.14.0"]
    assert project["optional-dependencies"]["pdf"] == []
    lock = {}
    for line in (ROOT / "ops" / "requirements.lock.txt").read_text(encoding="utf-8").splitlines():
        if "==" in line and not line.startswith("#"):
            name, version = line.split("==", 1)
            lock[_name(name)] = version.strip()
    assert lock.get("pypdfium2") == "5.14.0"


def test_the_default_install_extracts_pdfs_with_pdfium(tmp_path, monkeypatch):
    """With the core dependencies alone, PDF text comes from pypdfium2:
    pypdf is blocked and extraction still succeeds, through PdfDocument."""
    import pypdfium2  # a core dependency: no importorskip

    p = tmp_path / "doc.pdf"
    _make_minimal_pdf(p, "default path test")
    opened = []
    real_document = pypdfium2.PdfDocument

    def spy(*args, **kwargs):
        opened.append(args)
        return real_document(*args, **kwargs)

    monkeypatch.setattr(pypdfium2, "PdfDocument", spy)
    real_import = builtins.__import__

    def no_pypdf(name, *args, **kwargs):
        if name == "pypdf" or name.startswith("pypdf."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)
    assert extract_text(p) == "default path test"
    assert len(opened) == 1
