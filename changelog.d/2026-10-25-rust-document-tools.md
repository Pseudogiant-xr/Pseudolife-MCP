### Added (2026-10-25 — the Rust daemon ingests and searches documents)
- `document_ingest` and `document_search` were the last two of the 38 MCP
  tools the Rust daemon answered with its `not_implemented` stub. It now
  serves both over the bank's `reference_chunks` table (schema v57), as the
  Python daemon does: the same path handling and refusals, text decoding,
  chunk windows and chunk ids, so a document ingested by either daemon is
  found and replaced by the other. Search is the same exact cosine scan,
  ordered by distance and then chunk id.
- Declared differences (`rust/daemon/divergences.md`, "Document store
  (B7)"): a `.pdf` is refused with `not_implemented` until PDF text
  extraction is decided; `.html` always takes the tag-stripping path the
  daemon image serves (no BeautifulSoup); the one-time import of a pre-v57
  chromadb store stays with the Python daemon. Memory search does not yet
  include document hits on the Rust daemon, and `memory_stats` still counts
  no documents there; both follow in the next change.
- Nothing installs the Rust daemon yet, so no user setup changes.
