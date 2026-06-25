# Vendored third-party assets

These files are vendored (committed in-tree) so the Cortex Console stays fully
**offline** — no CDN at runtime, matching the daemon's baked `HF_HUB_OFFLINE`
image. They are served verbatim at `/ui/vendor/` and lazy-loaded on demand.

## `3d-force-graph.bundle.js`

- **What:** [`3d-force-graph`](https://github.com/vasturiano/3d-force-graph) — a
  WebGL 3D force-directed graph. Powers the Graph tab's **galaxy** view.
- **Version:** `3d-force-graph@1.73.6`.
- **Source:** `https://esm.sh/3d-force-graph@1.73.6?bundle&target=es2020`
  (resolved artifact: `.../es2020/3d-force-graph.bundle.mjs`). The `?bundle`
  flag inlines **all** dependencies — including **three.js** — into a single
  self-contained ES module (verified: no external `import`/`from` references;
  `WebGLRenderer` present). ≈1.35 MB.
- **Loaded by:** `static/js/views/graph.js` via dynamic
  `import('/ui/vendor/3d-force-graph.bundle.js')`, only when the galaxy view is
  opened (keeps the base bundle light).
- **Licenses:** MIT — `3d-force-graph` © Vasco Asturiano; bundled
  [three.js](https://github.com/mrdoob/three.js) © three.js authors.
- **Vendored:** 2026-06-26.

To update: re-download from the pinned esm.sh URL (bump the version), confirm it
is still self-contained (`grep -c WebGLRenderer` > 0; no `from "https`/`from "/`
references), and re-run the galaxy QA.
