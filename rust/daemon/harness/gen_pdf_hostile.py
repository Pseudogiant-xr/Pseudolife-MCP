"""Write the hostile PDFs of the PDF corpus (``pdf_corpus/hostile-*.pdf``).

The cases the pdf-extract review found (2026-10-25), which PDFium has to
survive: a Form XObject that draws itself; a Pages node that is its own
kid; a two-node page-tree cycle; image bytes that read like text
operators; many pages in one flat Pages node; a long chain of nested Form
XObjects. Plus a page whose text starts with 1,100 glyph-0 characters,
which PDFium excludes and pypdfium2's recursive edge trim fails on
(``divergences.md``, DOC-PDF-RECURSION). Sizes are cut to keep the corpus
small; each case still trips what it was built for.

    python rust/daemon/harness/gen_pdf_hostile.py
"""
import sys
from pathlib import Path

FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def stream(dict_body: bytes, data: bytes) -> bytes:
    return b"<< " + dict_body + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def write(path, objs):
    """objs: list of bytes, object i+1. Object 1 is the catalog."""
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offs = []
    for i, body in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    Path(path).write_bytes(bytes(out))


def page(parent, content_ref, resources):
    return (b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R /Resources "
            % (parent, content_ref) + resources + b" >>")


def main(d):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    text = b"BT /F1 12 Tf 72 720 Td (Visible page text) Tj ET"
    # 1. A Form XObject that draws itself.
    write(d / "hostile-recursive-xobject.pdf", [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page(2, 4, b"<< /Font << /F1 6 0 R >> /XObject << /X0 5 0 R >> >>"),
        stream(b"", text + b" /X0 Do"),
        stream(b"/Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /Font << /F1 6 0 R >> /XObject << /X0 5 0 R >> >>",
               b"BT /F1 12 Tf 72 700 Td (Form text) Tj ET /X0 Do"),
        FONT,
    ])
    # 2. A Pages node listing itself among its kids.
    write(d / "hostile-self-parent.pdf", [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [2 0 R 3 0 R] /Count 2 >>",
        page(2, 4, b"<< /Font << /F1 5 0 R >> >>"),
        stream(b"", text),
        FONT,
    ])
    # 3. A two-node cycle in the page tree.
    write(d / "hostile-tree-cycle.pdf", [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Pages /Parent 2 0 R /Kids [2 0 R] /Count 1 >>",
        page(2, 5, b"<< /Font << /F1 6 0 R >> >>"),
        stream(b"", text),
        FONT,
    ])
    # 4. An image whose bytes read like text operators: none of it is text.
    write(d / "hostile-image-as-content.pdf", [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page(2, 4, b"<< /Font << /F1 6 0 R >> /XObject << /Im0 5 0 R >> >>"),
        stream(b"", text + b" q 100 0 0 100 72 500 cm /Im0 Do Q"),
        stream(b"/Type /XObject /Subtype /Image /Width 10 /Height 5 /ColorSpace /DeviceGray /BitsPerComponent 8",
               b"BT /F1 12 Tf (IMAGEBYTES) Tj ET" + b" " * 18),
        FONT,
    ])
    # 5. Many pages in one flat Pages node (enumeration cost).
    n = 1000
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % (5 + i) for i in range(n)) + b"] /Count %d >>" % n,
            stream(b"", text), FONT]
    objs += [page(2, 3, b"<< /Font << /F1 4 0 R >> >>") for _ in range(n)]
    write(d / "hostile-many-pages.pdf", objs)
    # 6. A chain of 20,000 nested Form XObjects (stack depth).
    depth = 2000
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            page(2, 4, b"<< /Font << /F1 5 0 R >> /XObject << /X 6 0 R >> >>"),
            stream(b"", text + b" /X Do"),
            FONT]
    for i in range(depth):
        me = 6 + i
        nxt = me + 1 if i + 1 < depth else None
        res = b"<< /XObject << /X %d 0 R >> >>" % nxt if nxt else b"<< >>"
        objs.append(stream(b"/Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources " + res,
                           b"/X Do" if nxt else b""))
    write(d / "hostile-deep-forms.pdf", objs)
    # 7. 1,100 glyph-0 characters before the visible text.
    write(d / "hostile-excluded-chars.pdf", [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page(2, 4, b"<< /Font << /F1 5 0 R >> >>"),
        stream(b"", b"BT /F1 12 Tf 72 720 Td <" + b"00" * 1100 + b"> Tj (visible) Tj ET"),
        FONT,
    ])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("pdf_corpus"))
