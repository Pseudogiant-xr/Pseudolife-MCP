"""Write the hand-made files of the PDF corpus (``pdf_corpus/``), whose text
the Rust daemon must read as the Python daemon does (``gen_pdf_goldens.py``).

Every file is small and built here, so the corpus can be committed: PDF
syntax written by hand (one font, text placed line by line), plus pypdf's
writer for the two encrypted cases (their file ids are random, so a rerun
rewrites those two files with new bytes and the same content).

Cases: one page; three pages of several lines; a page with no text; a
long document that makes several chunks; Latin-1 text in a WinAnsi font;
text placed out of reading order on the page; a document encrypted with a
user password (both extractors refuse it); one encrypted with an empty user
password (both read it); a truncated file; a file that is not a PDF.

usage: python gen_pdf_corpus.py
"""
from __future__ import annotations

import io
from pathlib import Path

OUT = Path(__file__).resolve().parent / "pdf_corpus"

LOREM = ("The nightly backup writes a compressed dump of the memory bank to the "
         "backups volume, and a restore replays it into an empty database. ")


def _escape(text: str) -> bytes:
    """A PDF literal string body: latin-1 bytes, with ``\\``, ``(``, ``)``
    escaped and anything outside printable ASCII as an octal escape."""
    out = bytearray()
    for b in text.encode("latin-1"):
        if b in b"\\()":
            out += b"\\" + bytes([b])
        elif 32 <= b < 127:
            out.append(b)
        else:
            out += f"\\{b:03o}".encode()
    return bytes(out)


def _content(lines: list[tuple[float, float, str]]) -> bytes:
    """One text object per line, each placed absolutely."""
    ops = []
    for x, y, text in lines:
        ops.append(b"BT /F1 11 Tf 1 0 0 1 %d %d Tm (" % (x, y) + _escape(text) + b") Tj ET")
    return b"\n".join(ops)


def build(pages: list[list[tuple[float, float, str]]], *, winansi: bool = False) -> bytes:
    """A PDF 1.4 file with one Helvetica font and one content stream per
    page (an empty page gets no content stream)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in once the page tree's number is known
    pages_obj = add(b"")
    encoding = b" /Encoding /WinAnsiEncoding" if winansi else b""
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica" + encoding + b" >>")
    kids = []
    for lines in pages:
        content_ref = b""
        if lines:
            stream = _content(lines)
            c = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
            content_ref = b" /Contents %d 0 R" % c
        p = add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792]"
                b" /Resources << /Font << /F1 %d 0 R >> >>%s >>" % (pages_obj, font, content_ref))
        kids.append(p)
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    objects[pages_obj - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                              + b"] /Count %d >>" % len(kids))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, catalog, xref))
    return bytes(out)


def page_of(paragraph: str, top: int = 740, width: int = 90) -> list[tuple[float, float, str]]:
    """A paragraph wrapped at ``width`` characters, a line every 14 points."""
    words, lines, cur = paragraph.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return [(72, top - 14 * i, line) for i, line in enumerate(lines)]


def encrypted(data: bytes, user: str, owner: str, algorithm: str) -> bytes:
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(user_password=user, owner_password=owner, algorithm=algorithm)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def main() -> int:
    OUT.mkdir(exist_ok=True)
    one = build([page_of("A single page about restoring the bank from a backup.")])
    long_pages = [page_of(LOREM * 6) for _ in range(5)]
    files = {
        "one-page.pdf": one,
        "three-pages.pdf": build([
            page_of("First page. The bank lives in PostgreSQL and the daemon owns every write."),
            page_of("Second page. A dream consolidates the stream into canonical facts."),
            page_of("Third page. Lessons come from outcomes recorded at the end of a task."),
        ]),
        "empty-page.pdf": build([[]]),
        "long.pdf": build(long_pages),
        "latin1.pdf": build([page_of("Café crème, naïve résumé, "
                                     "über groß © 2026.")], winansi=True),
        "out-of-order.pdf": build([[(72, 600, "Third line placed lowest."),
                                    (72, 740, "First line placed highest."),
                                    (300, 670, "Second line, to the right."),
                                    (72, 670, "Second line, at the margin.")]]),
        "encrypted-user-password.pdf": encrypted(one, "secret", "owner", "AES-128"),
        "encrypted-empty-user.pdf": encrypted(one, "", "owner", "RC4-128"),
        "truncated.pdf": build(long_pages)[:2000],
        "not-a-pdf.pdf": b"This is a plain text file with a .pdf suffix.\n",
    }
    for name, data in files.items():
        (OUT / name).write_bytes(data)
        print(f"wrote {name} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
