"""Write the golden cases for the Rust port of the document store's text
path (``src/documents.rs``).

Every expected value comes from calling the real Python code in this
checkout: ``Path.read_text`` and ``memory/document_parser.extract_text`` on
files written from the case bytes, ``_extract_html`` with BeautifulSoup made
unimportable (the daemon image ships none, so it serves the regex fallback),
``memory/reference_bank._chunk_text`` and ``chunk_id``, ``str.strip`` and
``pathlib``'s parse. The Rust unit tests (``src/documents_tests.rs``) assert
against these values, not against hand-written expectations.

Output: ``goldens/documents.json``, ``{kind: [case, ...]}``.

usage: python gen_document_goldens.py [--check]
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "documents.json"
sys.path.insert(0, str(REPO))

from pseudolife_memory.memory import document_parser  # noqa: E402
from pseudolife_memory.memory.reference_bank import _chunk_text, chunk_id  # noqa: E402

# Byte sequences around UTF-8 decoding and text-mode newlines: valid
# multi-byte text, every class of invalid sequence the decoder replaces
# (truncated, overlong, surrogate, out of range, stray continuation), a BOM,
# and CR/LF in each arrangement.
DECODE = [
    b"plain ascii\n",
    "caf\u00e9 \u2014 \u65e5\u672c \U0001f600".encode(),
    b"\xef\xbb\xbfbom kept\n",
    b"crlf\r\nline\r\n",
    b"lone\rcr\r",
    b"mixed\r\r\n\n\r\n\r",
    b"\r\n\r\n",
    b"trunc \xe6\x97",
    b"trunc mid \xe6\x97 x",
    b"overlong \xc0\xaf \xe0\x80\xaf",
    b"surrogate \xed\xa0\x80 \xed\xbf\xbf",
    b"range \xf4\x90\x80\x80 \xf5\x80",
    b"stray \x80\x81 \xbf",
    b"four \xf0\x9f\x98 \xf0\x9f",
    b"\xff\xfe\x00a",
    b"nul \x00 inside",
    b"tail cr \xe6\r\n",
]

HTML = [
    "<html><head><title>T</title></head><body><p>Hello <b>world</b></p></body></html>",
    "<script>var x = 1 < 2;</script>text after",
    "a<br/>b<br>c",
    "  <p>\n\n  spaced \t\t out  </p>\n",
    "no tags at all",
    "<unclosed tag text",
    "x > y and <z",
    "<a\nhref='multi\nline'>link</a>",
    "\u00a0nbsp\u00a0<i>\u3000ideographic</i>\u2028sep\x1cinfo\x1f",
    "<p>caf\u00e9</p>\r\n<p>na\u00efve</p>",
    "",
    "<>empty<>tag",
]

STRIP = [
    "  both  ", "\t\n\x0b\x0c\r x \r\n", "\x1c\x1d\x1e\x1f inner \x1f",
    "\u0085\u00a0\u1680x\u2000\u200a\u2028\u2029\u202f\u205f\u3000",
    "\u200b zero width stays \u200b", "\ufeff bom \ufeff", "", "   ",
    "\u180e mongolian \u180e",
]

CHUNK_SIZES = [(512, 64), (2, 1), (3, 0), (1, 1), (2, 3), (0, 0), (-1, 0), (-2, 1),
               (1, 0), (4, 2)]

LOREM = (
    "The reference bank keeps verbatim source text.\n\n"
    "Chunks overlap so a passage cut at a boundary is still whole somewhere. "
    "\u65e5\u672c\u8a9e\u306e\u6587\u7ae0\u3082\u5165\u308a\u307e\u3059\u3002 "
    "Emoji \U0001f600\U0001f680 count as one code point each.\n"
) * 3

CHUNK_TEXTS = [
    "",
    "   \n\t  ",
    "short",
    "abcdefghijklmnopqrstuvwxyz",
    "  pad  pad  pad  pad  pad  ",
    "\U0001f600" * 30,
    "x\x1cy\x1d z" * 5,
    LOREM,
    LOREM * 40,
]

CHUNK_IDS = [
    ("notes.md", 0, "short"),
    ("notes.md", 7, "x" * 250),
    ("", 0, ""),
    ("caf\u00e9.txt", 3, "\U0001f600" * 150),
    ("a:b", 1, "c:d"),
    ("nul\x00src", 2, "nul\x00text"),
]

PATHS = [
    "", ".", "./", "/", "//", "///", "////x", "//srv/share/f.txt", "a", "a/", "a//b",
    "a/./b", "./a/b/", "a/../b", "/tmp/x.txt", "/tmp/dir/", "x.tar.gz", ".bashrc",
    "file.", "..", "a/..", "notes.MD", "trailing.txt/", "sp ace/na me.md",
    "C:\\Users\\<user>\\doc.txt", "C:/Users/<user>/doc.txt", "C:doc.txt", "\\rooted\\a.md",
    "rel\\win\\b.html", "mix/ed\\sep.TXT",
    # Two leading separators, drive-like first parts and device paths, where
    # CPython 3.12's ntpath.splitroot differs from 3.11's.
    "//srv", "//srv/", "//srv//x", "//srv/share", "\\\\srv\\share\\", "1:foo", "\u00e9:x",
    "./c:x", "a/b:c", "\\\\?\\C:\\x.txt", "\\\\.\\pipe\\name", "//?/UNC/srv/share/f.md",
    "//?/unc/srv", "\\\\server", "c:", "c:/", "C:..\\up.txt",
]

# Whole files through extract_text, then the ingest's chunking and ids.
FILES = [
    ("doc.txt", b"First paragraph.\r\n\r\nSecond paragraph with caf\xc3\xa9.\r\n" * 20),
    ("page.html", b"<html><body><h1>Title</h1>\r\n<p>Body \xe2\x80\x94 text</p></body></html>"),
    ("PAGE.HTM", b"<div>upper</div>"),
    ("data.unknownext", b"bytes \xff that are \xfe mostly text\r"),
    ("noext", b"no suffix at all\n"),
    ("notes.markdown", b"# Heading\n\n" + b"word " * 900),
    ("blank.md", b"   \r\n\t\r\n"),
]


@contextlib.contextmanager
def without_bs4():
    """BeautifulSoup unimportable, as in the daemon image."""
    saved = sys.modules.get("bs4", ...)
    sys.modules["bs4"] = None  # `import bs4` raises ImportError
    try:
        yield
    finally:
        if saved is ...:
            del sys.modules["bs4"]
        else:
            sys.modules["bs4"] = saved


def written(data: bytes, name: str, read):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / name
        p.write_bytes(data)
        return read(p)


def build() -> dict:
    out: dict = {"decode": [], "html": [], "strip": [], "chunk": [], "chunk_id": [],
                 "path_posix": [], "path_windows": [], "file": []}
    for b in DECODE:
        text = written(b, "case.txt", lambda p: p.read_text(encoding="utf-8", errors="replace"))
        out["decode"].append({"hex": b.hex(), "text": text})
    with without_bs4():
        for h in HTML:
            text = written(h.encode("utf-8"), "page.html", document_parser._extract_html)
            out["html"].append({"html": h, "text": text})
        for name, data in FILES:
            text = written(data, name, document_parser.extract_text)
            chunks = _chunk_text(text, 512, 64)
            out["file"].append({
                "name": name, "hex": data.hex(), "text": text,
                "chunks": [{"id": chunk_id("src-" + name, i, c), "text": c}
                           for i, c in enumerate(chunks)],
            })
    for s in STRIP:
        out["strip"].append({"s": s, "out": s.strip()})
    for text in CHUNK_TEXTS:
        for cs, co in CHUNK_SIZES:
            if len(text) > 2000 and (cs * 4 - co * 4) < 64:
                continue  # a step of one over a long text adds nothing
            out["chunk"].append({"text": text, "chunk_size": cs, "chunk_overlap": co,
                                 "chunks": _chunk_text(text, cs, co)})
    for source, index, chunk in CHUNK_IDS:
        out["chunk_id"].append({"source": source, "index": index, "chunk": chunk,
                                "id": chunk_id(source, index, chunk)})
    for raw in PATHS:
        for kind, cls in (("path_posix", PurePosixPath), ("path_windows", PureWindowsPath)):
            p = cls(raw)
            out[kind].append({"raw": raw, "shown": str(p), "name": p.name,
                              "suffix": p.suffix})
    return out


# pathlib's parse changed between CPython 3.11 and 3.12 (ntpath.splitroot);
# the daemon image runs 3.12.
PYTHON = "3.12"


def main() -> int:
    if not sys.version.startswith(PYTHON + "."):
        print(f"gen_document_goldens.py needs CPython {PYTHON} (the daemon image's), "
              f"not {sys.version.split()[0]}", file=sys.stderr)
        return 2
    out = build()
    out["python"] = PYTHON  # the minor version, so any 3.12.x checks clean
    text = json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if "--check" in sys.argv[1:]:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET} is stale: rerun gen_document_goldens.py", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
