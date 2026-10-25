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
``require_oracle_without_bs4`` refuses a run whose interpreter has it. PDF
ingest is a declared divergence (``divergences.md``, DOC-PDF) and has no
case here.
"""
from __future__ import annotations

import functools
import tempfile
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


def answer_tokens(answer, ranks: dict[float, str]):
    """A ``document_search`` answer with each entry's ``timestamp`` as the
    rank token of the ``ingested_at`` it equals (``w2e.clock_ranks``)."""
    if not isinstance(answer, dict):
        return answer
    value = answer.get("value")
    if isinstance(value, dict) and isinstance(value.get("entries"), list):
        for e in value["entries"]:
            if isinstance(e, dict) and isinstance(e.get("timestamp"), float) \
                    and e["timestamp"] in ranks:
                e["timestamp"] = ranks[e["timestamp"]]
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


def require_oracle_without_bs4() -> None:
    """The oracle daemon runs on this interpreter (``daemons.python_daemon``)."""
    import importlib.util
    if importlib.util.find_spec("bs4") is not None:
        raise SystemExit("documents scenarios: this interpreter has BeautifulSoup, which the "
                         "daemon image lacks; run from an environment without bs4")


# The document breaks (``documents.rs``), each caught by the documents-*
# scenarios.
MUTANTS = ["document-raw-newlines", "document-strip-rust-whitespace", "document-no-overlap",
           "document-search-no-id-order", "document-upsert-keeps-old",
           "document-upsert-clock-only"]


SCENARIOS: dict[str, dict] = {
    # Ingest and search on an empty bank: every text branch the parser
    # takes, the refusals, then exact-order search over three topics.
    "documents-ingest-search": {
        "config": None,
        "seed": [],
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
            search("how do I restore a pg_dump backup", top_k=2),
            search("watering tomato seedlings", top_k=1),
            search("sourdough starter feeding schedule"),
            search("comet orbital period", top_k=50),
            search(""),
        ],
    },
    # A re-ingest: the same file moves ingested_at and keeps created_at; a
    # shorter version under the same source replaces its first chunks and
    # leaves the old tail in place.
    "documents-reingest": {
        "config": None,
        "seed": [],
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
        "ops": [
            ingest("@DOCS@/garden.txt", source="twin-a"),
            ingest("@DOCS@/garden.txt", source="twin-b"),
            search("watering tomato seedlings", top_k=2),
            ingest("@DOCS@/separators.txt"),
            search("information separators", top_k=1),
        ],
    },
    # Configured chunking: the memory.reference sizes.
    "documents-config": {
        "config": "memory:\n  reference:\n    chunk_size: 40\n    chunk_overlap: 8\n",
        "seed": [],
        "ops": [
            ingest("@DOCS@/garden.txt"),
            search("transplanting seedlings outside"),
        ],
    },
}
