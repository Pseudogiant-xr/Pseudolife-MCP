"""Document parser extraction paths — pypdfium2 preferred, pypdf fallback.

The PDF fixture is handcrafted (offsets computed, no writer dependency) so
the test runs offline and exercises whichever extractor is installed.
"""

from __future__ import annotations

import builtins

import pytest

from pseudolife_memory.memory.document_parser import extract_text


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
    _make_minimal_pdf(p, "PseudoLife pdfium test")
    assert "PseudoLife pdfium test" in extract_text(p)


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
