# System Atlas

A hand-curated architecture map of Pseudolife-MCP: every significant
component, the typed edges between them, and the six end-to-end flows
(store, search/recall, dream, briefing, console, deploy). It complements —
and is distinct from — the Cortex Console's Atlas view, which visualizes the
*memory bank's* knowledge graph; this atlas describes the *codebase*.

- **`atlas.json`** — the canonical data: `meta`, `groups`, `nodes`, `edges`,
  `flows`. Machine-readable; agents can consume it directly.
- **`atlas.html`** — a dependency-free viewer (pan/zoom SVG, inspector,
  flow tracing, in-browser editing with localStorage persistence and
  JSON export/import). It fetches `atlas.json` at load.

## Viewing

Browsers block `fetch()` from `file://`, so serve the folder:

```bash
python -m http.server 8899 --bind 127.0.0.1 --directory docs/atlas
```

then open <http://127.0.0.1:8899/atlas.html>. In-browser edits stay in
localStorage; use **export** to save an annotated copy, **reset** to return
to the committed atlas.

## Keeping it honest

`tests/test_atlas_currency.py` pins the map to the tree:

- every node `path` must exist (globs, URLs, and parenthesized annotations
  are descriptive and skipped);
- `meta.version` must equal the `pyproject.toml` version and `meta.schema`
  must equal `SCHEMA_META_VERSION` — version cuts and schema bumps must
  re-verify the map and update `meta` (including `verified`) in the same
  change;
- edges and flow sequences must reference nodes/edges that exist.

When the guard goes red, fix the map, not the test: update the affected
cards, re-verify the claims against the code, and bump `meta.verified`.

## Editing the committed atlas

Edit `atlas.json` directly (it is the single source of truth — the viewer
embeds no data). For larger reworks, annotate in the viewer, export the
JSON, and diff it against the committed file.
