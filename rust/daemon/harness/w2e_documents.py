"""Document-store scenarios for ``w2e.py``: ``document_ingest`` and
``document_search`` over the bank's ``reference_chunks`` table (schema v57),
each daemon reading the same fixture files.

The fixtures are written once per run to ``docs_dir()``; ``@DOCS@`` in an
argument names that directory, and in every answer the directory is
replaced by ``@DOCS@`` again, so a recorded golden carries no machine path.

Normalizers (declared, added to ``w2e.py``'s list):

* ``reference_chunks.embedding`` compares numerically per chunk id against
  the embedding tolerances, as ``facts.embedding`` does;
* ``reference_chunks`` ``ingested_at`` and ``created_at``, and the
  ``timestamp`` in each chunk's ``metadata``, keep their equality and order
  (``ORDERED_CLOCK`` rank tokens): every chunk of one ingest shares one
  clock, and a re-ingest moves ``ingested_at`` but keeps ``created_at``;
* in a ``document_search`` answer, an entry's ``timestamp`` equal to a
  chunk's ``ingested_at`` on the same side becomes that clock's rank token.

The oracle must run without BeautifulSoup, as the daemon image does (the
HTML fixture then takes ``_extract_html``'s regex fallback);
``require_document_oracles`` refuses a run whose interpreter has it. PDFs
are read by PDFium on both sides: the oracle needs the pypdfium2 the image
locks, and the Rust daemon the pinned library under
``PSEUDOLIFE_DAEMON_PDFIUM_DIR`` (``pseudolife-daemon provision pdfium``);
the same guard refuses a run without either.
"""
from __future__ import annotations

import functools
import os
import shutil
import tempfile
import time
from pathlib import Path

GUIDE = (
    "# Restoring the bank from a backup\n\n"
    "The nightly backup writes a compressed pg_dump of the memory bank to the "
    "backups volume. To restore it, stop the daemon, create an empty database, "
    "run pg_restore with the dump file, then start the daemon again so the "
    "schema check runs before any request is served. "
) * 12

# A shorter revision sharing GUIDE's first 160 characters: its one chunk has
# the id of GUIDE's first (the id hashes the first 100), so the re-ingest
# overwrites that row with new text, and GUIDE's other chunks stay.
GUIDE_V2 = GUIDE[:160] + (
    " Restores now go through the update command, which takes a fresh backup "
    "first and rolls back on a failed health check. "
) * 3

GARDEN = (
    "Tomato seedlings want sixteen hours of light and a soil temperature near "
    "twenty-four degrees.\r\nHarden them off outside for a week before "
    "transplanting.\r\rWater at the base in the morning, never the leaves.\r\n"
) * 4

PAGE = (
    "<html><head><title>Sourdough</title><style>p {color: red}</style></head>"
    "<body><h1>Feeding a sourdough starter</h1>\r\n<p>Feed it equal weights of "
    "flour and water every twelve hours; it is ready when it doubles.</p>"
    "<script>var unused = 1 < 2;</script></body></html>"
)

FIXTURES: dict[str, bytes] = {
    "guide.md": GUIDE.encode("utf-8"),
    "v2/guide.md": GUIDE_V2.encode("utf-8"),
    "garden.txt": GARDEN.encode("utf-8"),
    "page.html": PAGE.encode("utf-8"),
    "notes.data": "Comet orbits are ellipses; the period of Halley's comet is "
                  "about seventy-six years. Café — \U0001f680\n".encode("utf-8")
                  + b"\xff invalid byte\n",
    "blank.txt": b"   \r\n\t\r\n",
    "separators.txt": b"\x1c\x1d Information separators pad this lone chunk. \x1f\x1e\n",
    # PostgreSQL refuses NUL: it is stored as U+FFFD, the id hashes the original.
    "nul.txt": b"A PDF's extracted text can carry NUL\x00characters\x00like these.\n",
}

MISSING = "/nonexistent-pl-w2e-documents/missing.txt"

# PDFs from the corpus the extraction goldens cover (``pdf_corpus/``):
# multi-page, CSS-hyphenated browser output, several scripts, and the
# refusals PDFium gives (not a PDF, a wrong password, a page-tree cycle).
PDF_CORPUS = Path(__file__).with_name("pdf_corpus")
PDF_FIXTURES = ("three-pages.pdf", "chrome-prose.pdf", "chrome-scripts.pdf",
                "fpdf-long.pdf", "not-a-pdf.pdf", "encrypted-user-password.pdf",
                "hostile-self-parent.pdf", "empty-page.pdf")


@functools.lru_cache(maxsize=1)
def docs_dir() -> str:
    """The fixture directory, written once per run (``folder.data`` is a
    directory with a suffix the parser reads without a text-mode branch)."""
    root = Path(tempfile.mkdtemp(prefix="pl-w2e-docs-"))
    for rel, data in FIXTURES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / "folder.data").mkdir()
    (root / "folder.pdf").mkdir()
    (root / "pdf").mkdir()
    for name in PDF_FIXTURES:
        shutil.copyfile(PDF_CORPUS / name, root / "pdf" / name)
    # A FIFO (POSIX) and a file one byte over the 64 MiB cap, sparse, so it
    # costs no disk: both daemons refuse each from its metadata.
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "pipe.txt")
    with open(root / "huge.txt", "wb") as f:
        f.truncate(64 * 1024 * 1024 + 1)
    return str(root)


def resolve(value):
    """``@DOCS@`` in an argument -> the fixture directory."""
    if isinstance(value, str) and "@DOCS@" in value:
        return value.replace("@DOCS@", docs_dir())
    return value


def scrub(value):
    """The fixture directory in an answer -> ``@DOCS@``."""
    if isinstance(value, str):
        return value.replace(docs_dir(), "@DOCS@")
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {scrub(k): scrub(v) for k, v in value.items()}
    return value


def ingest(path: str, **kw) -> dict:
    return {"op": "tool", "name": "document_ingest", "args": {"path": path, **kw},
            "headers": []}


def search(query: str, **kw) -> dict:
    return {"op": "tool", "name": "document_search", "args": {"query": query, **kw},
            "headers": []}


def memory_search(query: str, **kw) -> dict:
    return {"op": "tool", "name": "memory_search", "args": {"query": query, **kw},
            "headers": []}


def rest_search(query: str, **params) -> dict:
    """``GET /api/search`` (the Console route over the same search)."""
    from urllib.parse import urlencode
    return {"op": "http", "method": "GET",
            "path": "/api/search?" + urlencode({"q": query, **params})}


def remember(text: str, source: str = "agent") -> dict:
    return {"op": "tool", "name": "memory_store", "args": {"text": text, "source": source},
            "headers": []}


def stats() -> dict:
    return {"op": "tool", "name": "memory_stats", "args": {}, "headers": []}


def is_search(op: dict) -> bool:
    """An operation whose answer serves entries that may be reference chunks."""
    return (op.get("name") in ("document_search", "memory_search")
            or (op.get("op") == "http" and str(op.get("path", "")).startswith("/api/search")))


def answer_tokens(answer, ranks: dict[float, str]):
    """An answer with each served entry's ``timestamp`` (in any ``entries``
    list: ``document_search``, ``memory_search``, ``GET /api/search``) as
    the rank token of the clock it equals (``w2e.clock_ranks``): a
    reference chunk's is its ``ingested_at``. A reference hit's timestamp
    matching no ingest is the clock at serving (a stored 0.0 becomes
    ``time.time()``): within a day of now it is ``<served-now>``, each
    side's own moment."""
    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "entries" and isinstance(x, list):
                    for e in x:
                        if not (isinstance(e, dict) and isinstance(e.get("timestamp"), float)):
                            continue
                        if e["timestamp"] in ranks:
                            e["timestamp"] = ranks[e["timestamp"]]
                        elif (e.get("bank") == "reference"
                              and abs(time.time() - e["timestamp"]) < 86400):
                            e["timestamp"] = "<served-now>"
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(answer)
    return answer


def metadata_clocks(t: dict, ranks: dict[float, str], ordered) -> None:
    """``reference_chunks.metadata.timestamp`` as its rank token."""
    if "metadata" not in t["columns"]:
        return
    mi = t["columns"].index("metadata")
    for row in t["rows"]:
        m = row[mi]
        if isinstance(m, dict) and "timestamp" in m:
            row[mi] = {**m, "timestamp": ordered(m["timestamp"], ranks, False)}


def require_document_oracles() -> None:
    """The oracle daemon runs on this interpreter (``daemons.python_daemon``):
    without BeautifulSoup and with the locked pypdfium2, as in the image;
    the Rust daemon reads PDFs from the provisioned PDFium root."""
    import importlib.util
    if importlib.util.find_spec("bs4") is not None:
        raise SystemExit("documents scenarios: this interpreter has BeautifulSoup, which the "
                         "daemon image lacks; run from an environment without bs4")
    lock = Path(__file__).resolve().parents[3] / "ops" / "requirements.lock.txt"
    locked = next((line.split("==", 1)[1].strip() for line in
                   lock.read_text(encoding="utf-8").splitlines()
                   if line.lower().startswith("pypdfium2==")), None)
    try:
        import pypdfium2.version as v
        found = v.PYPDFIUM_INFO.version
    except ImportError:
        found = None
    if found != locked:
        raise SystemExit(f"documents scenarios: the oracle needs pypdfium2 {locked} (the "
                         f"image lock), found {found}")
    if not os.environ.get("PSEUDOLIFE_DAEMON_PDFIUM_DIR"):
        raise SystemExit("documents scenarios: set PSEUDOLIFE_DAEMON_PDFIUM_DIR to a root "
                         "`pseudolife-daemon provision pdfium` installed into")


# Golden replay skips these scenarios (``live_only``): a replay boots one
# Rust bank and compares its whole state with the recorded oracle bank, and
# every w2e recording on master already differs there in boot-time rows (the
# schema version, Python's startup meta keys, the warmup's retrieval event),
# so a recording would add no check. Live mode compares both daemons.
LIVE_ONLY = ("live mode only: golden replay compares boot-time bank rows that "
             "differ for every w2e recording on master")

# The document breaks (``documents.rs``), each caught by the documents-*
# scenarios.
MUTANTS = ["document-raw-newlines", "document-strip-rust-whitespace", "document-no-overlap",
           "document-search-no-id-order", "document-upsert-keeps-old",
           "document-upsert-clock-only", "document-zero-clock-kept",
           "document-directory-allowed",
           # The reference pool and counts (search.rs, documents.rs, stats.rs):
           # caught by documents-search-pool and documents-search-seeded.
           "search-no-reference-pool", "search-reference-no-dedup",
           "search-reference-no-floor", "stats-no-reference-counts",
           "search-reference-no-contiguity",
           # PDF text (native/pdf_text.rs): caught by documents-pdf.
           "document-pdf-first-page-only", "document-pdf-pages-one-newline",
           "document-pdf-error-names-shifted"]


SCENARIOS: dict[str, dict] = {
    # Ingest and search on an empty bank: every text branch the parser
    # takes, the refusals, then exact-order search over three topics.
    "documents-ingest-search": {
        "config": None,
        "seed": [],
        "live_only": LIVE_ONLY,
        "ops": [
            search("restoring the bank from a backup", top_k=3),
            ingest("@DOCS@/guide.md"),
            ingest("@DOCS@/garden.txt", source="ops-garden"),
            ingest("@DOCS@/page.html", source=""),
            ingest("@DOCS@/notes.data", source="astronomy"),
            ingest("@DOCS@/blank.txt"),
            ingest("@DOCS@/nul.txt", source="nul\x00source"),
            ingest("@DOCS@//./page.html/"),
            ingest(MISSING),
            ingest("@DOCS@/folder.data"),
            ingest("@DOCS@/pipe.txt"),
            ingest("@DOCS@/huge.txt"),
            search("how do I restore a pg_dump backup", top_k=2),
            search("watering tomato seedlings", top_k=1),
            search("sourdough starter feeding schedule"),
            search("comet orbital period", top_k=50),
            search(""),
        ],
    },
    # PDFs, read by PDFium on both sides: multi-page and browser-printed
    # text chunked and stored identically, a page with no text, the
    # refusals PDFium gives, and a directory named .pdf refused before
    # PDFium is reached; then search over what was stored.
    "documents-pdf": {
        "config": None,
        "seed": [],
        "live_only": LIVE_ONLY,
        "ops": [
            ingest("@DOCS@/pdf/three-pages.pdf"),
            ingest("@DOCS@/pdf/chrome-prose.pdf", source="prose"),
            ingest("@DOCS@/pdf/chrome-scripts.pdf"),
            ingest("@DOCS@/pdf/fpdf-long.pdf"),
            ingest("@DOCS@/pdf/empty-page.pdf"),
            ingest("@DOCS@/pdf/not-a-pdf.pdf"),
            ingest("@DOCS@/pdf/encrypted-user-password.pdf"),
            ingest("@DOCS@/pdf/hostile-self-parent.pdf"),
            ingest("@DOCS@/folder.pdf"),
            search("ligatures and kerning pairs", top_k=3),
            search("memory keeps every fact with its history", top_k=5),
            search("page two of three", top_k=2),
        ],
    },
    # A re-ingest: the same file moves ingested_at and keeps created_at; a
    # shorter version under the same source replaces its first chunks and
    # leaves the old tail in place.
    "documents-reingest": {
        "config": None,
        "seed": [],
        "live_only": LIVE_ONLY,
        "ops": [
            ingest("@DOCS@/guide.md"),
            ingest("@DOCS@/guide.md"),
            ingest("@DOCS@/v2/guide.md"),
            search("restoring the bank from a backup", top_k=10),
        ],
    },
    # Equal distances break by chunk id: the same text under two sources
    # embeds identically, and the source whose id sorts last
    # (twin-a: d250…, twin-b: 0f16…) is written first, so a scan in
    # physical order serves the wrong one first. Python's str.strip also
    # removes the information separators U+001C..U+001F that Rust's trim
    # keeps.
    "documents-ties": {
        "config": None,
        "seed": [],
        "live_only": LIVE_ONLY,
        "ops": [
            ingest("@DOCS@/garden.txt", source="twin-a"),
            ingest("@DOCS@/garden.txt", source="twin-b"),
            search("watering tomato seedlings", top_k=2),
            ingest("@DOCS@/separators.txt"),
            search("information separators", top_k=1),
        ],
    },
    # A chunk whose stored ingested_at is 0.0 (Python's chromadb import writes
    # that for a chunk without a timestamp) serves the clock at serving, as
    # `MemoryEntry.__post_init__` sets it.
    "documents-zero-clock": {
        "config": None,
        "seed": [
            ingest("@DOCS@/garden.txt"),
            {"op": "sql", "sql": "UPDATE reference_chunks SET ingested_at = 0",
             "after_stop": True},
        ],
        "ops": [
            search("watering tomato seedlings", top_k=1),
        ],
    },
    # Configured chunking: the memory.reference sizes.
    "documents-config": {
        "config": "memory:\n  reference:\n    chunk_size: 40\n    chunk_overlap: 8\n",
        "seed": [],
        "live_only": LIVE_ONLY,
        "ops": [
            ingest("@DOCS@/garden.txt"),
            search("transplanting seedlings outside"),
        ],
    },
    # The reference pool in memory search (cms.py Pool 2) and the reference
    # counts in memory_stats: each daemon ingests its own copy, the memories
    # come from the shared seed. One memory has a chunk's exact text, so the
    # pool skips that chunk once the memory is served.
    "documents-search-pool": {
        "config": None,
        "seed": [
            remember("The nightly backup job runs at two in the morning."),
            remember("Tomatoes in the north bed get watered every second day."),
            remember("Information separators pad this lone chunk."),
        ],
        "live_only": LIVE_ONLY,
        "ops": [
            memory_search("how do I restore a pg_dump backup", top_k=2, verbose=True),
            stats(),
            ingest("@DOCS@/guide.md"),
            ingest("@DOCS@/garden.txt"),
            ingest("@DOCS@/separators.txt"),
            stats(),
            memory_search("how do I restore a pg_dump backup", top_k=2, verbose=True),
            memory_search("watering tomato seedlings", top_k=3),
            memory_search("information separators", top_k=2, verbose=True),
            memory_search("how do I restore a pg_dump backup", min_score=0.75, verbose=True),
            rest_search("how do I restore a pg_dump backup", top_k=3),
            rest_search("watering tomato seedlings"),
        ],
    },
    # Rows the Python daemon wrote, read by both (the older side's rows, as
    # after a rollback): the pool under ref_top_k 1, contiguity and the
    # timeline channel, then ref_top_k 0 (the read takes
    # reference.max_results rows and `ref_pool[:0]` serves none), RRF fusion,
    # and a negative ref_top_k (the LIMIT fails; the lenient read serves no
    # chunk).
    "documents-search-seeded": {
        "config": "memory:\n  ref_top_k: 1\n  search:\n    contiguity_neighbors: 1\n"
                  "    timeline_channel: true\n",
        "seed": [
            ingest("@DOCS@/guide.md"),
            ingest("@DOCS@/garden.txt", source="garden.txt"),
            remember("Seedlings were moved to the greenhouse.", source="nursery"),
            remember("The restore drill passed on the staging bank."),
            # The only episode-less memory of the document's source, and one
            # no query here matches: only the reference hit anchors it as a
            # contiguity neighbour.
            remember("Quarterly payroll forms are due at the end of April.", source="garden.txt"),
            {"op": "sql", "sql": "UPDATE entries SET episode_id = NULL WHERE source = 'garden.txt'",
             "after_stop": True},
        ],
        "live_only": LIVE_ONLY,
        "ops": [
            memory_search("how do I restore a pg_dump backup", verbose=True),
            rest_search("watering tomato seedlings", top_k=2),
            memory_search("what did we restore before the drill", verbose=True),
            stats(),
            {"op": "restart", "config": "memory:\n  ref_top_k: 0\n  reference:\n"
                                        "    max_results: 2\n"},
            memory_search("how do I restore a pg_dump backup", top_k=1, verbose=True),
            {"op": "restart", "config": "memory:\n  search:\n    fusion: rrf\n"},
            memory_search("watering tomato seedlings", top_k=2, verbose=True),
            {"op": "restart", "config": "memory:\n  ref_top_k: -1\n"},
            memory_search("how do I restore a pg_dump backup", verbose=True),
        ],
    },
}
