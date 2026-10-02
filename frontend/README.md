# Cortex Console (frontend)

The Cortex Console: a Svelte 5 + TypeScript single-page app built with Vite,
served by the daemon at `/ui/`. It replaced the vanilla-JS console on
2026-10-02; bookmarks into the old one (`#/console`, `#/atlas`,
`#/coordination`) land on the matching view.

## Develop

Requires Node 22.12 or newer.

```bash
# terminal 1, from the repository root: fixture-backed daemon on :8770
python -m pseudolife_memory.web.devserver

# terminal 2
cd frontend
npm ci
npm run dev          # http://localhost:5173/ui/
```

The dev server proxies `/api` and `/health` to `http://127.0.0.1:8770`. The
fixture server has no coordination board, so the Board shows its "needs a
bearer token" state there; point the proxy at a real daemon to see a live
board.

## Check, test and build

```bash
npm run check        # svelte-check, warnings fail
npm test             # vitest: the pure logic under src/lib/*.test.ts
npm run build        # writes ../pseudolife_memory/web/static/
```

`npm run build` empties `pseudolife_memory/web/static/` and writes the build
there, then runs `scripts/check-dist.mjs`, which fails if the output contains
an absolute local path or this machine's user or host name.

**The build output is committed.** The Python wheel ships
`pseudolife_memory/web/static/**` and is built without Node, so a source
change here is not live until `npm run build` has run and its output is
committed with it. CI's `frontend` job runs the check, the tests and the
build, and fails if the rebuilt output differs from what is committed.
`.gitattributes` checks the sources out with LF line endings on every OS so
that a Windows build and the Linux CI build produce the same bytes.

## Layout

- `src/views/`: one component per route; `src/lib/nav.ts` lists the routes.
- `src/lib/api/client.ts`: `get`, `post` and `softError` (several write
  routes answer HTTP 200 with `{"error": ...}`; a view must check before it
  reports success).
- `src/lib/overlay.svelte.ts`: `toast()` and `confirm()`;
  `src/lib/reviewActions.ts` and `reviewRunner.svelte.ts`: every graph
  review decision, shared by the Graph and Review views.
- `src/lib/dreamer.ts`: the Settings view's dreamer model list, kept in step
  with the installers and shims by `tests/test_extractor_model_lists.py`.
- `public/vendor/`: the vendored 3D engine (see its README for provenance
  and the licence audit), copied into the build unchanged.

## Notes

- Browser storage keys: `pl_token` (bearer token) and `pl_theme` (`dark` or
  `light`), the keys the console has always used.
- No runtime requests to third-party hosts: Geist and Geist Mono (variable
  woff2 from the `geist` npm package, version 1.7.2) are vendored under
  `src/fonts/` with their SIL Open Font License (`src/fonts/LICENSE.txt`).
  The package itself is not a dependency: it declares Next.js as a peer, which
  `npm ci` would install for nothing. To update the fonts, copy the two files
  from a newer release and note the version here.
- Text from the bank is rendered as text: no `{@html}` anywhere, and a URL
  written by an agent is linked only when it is plain http(s)
  (`src/lib/safe.ts`). The 3D galaxy's tooltips are DOM nodes for the same
  reason (`tests/test_console_source_guards.py`).
- The Board is read-only. It reads `GET /api/agents?view=coordination`;
  message bodies are never served to the Console and there is no compose.
