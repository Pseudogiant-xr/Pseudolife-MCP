# Cortex Console v3 (frontend)

The next Cortex Console: a Svelte 5 + TypeScript single-page app built with
Vite. It is served by the daemon at `/ui/next/`, beside the classic console at
`/ui/`. In this phase the Observatory and the Board are native; every other
view links to the classic console.

## Develop

Requires Node 22.12 or newer.

```bash
# terminal 1, from the repository root: fixture-backed daemon on :8770
python -m pseudolife_memory.web.devserver

# terminal 2
cd frontend
npm ci
npm run dev          # http://localhost:5173/ui/next/
```

The dev server proxies `/api` and `/health` to `http://127.0.0.1:8770`. The
fixture server has no coordination board, so the Board shows its "needs a
bearer token" state there; point the proxy at a real daemon to see a live
board.

## Check and build

```bash
npm run check        # svelte-check, warnings fail
npm run build        # writes ../pseudolife_memory/web/static/next/
```

`npm run build` also runs `scripts/check-dist.mjs`, which fails if the output
contains an absolute local path or this machine's user or host name.

**The build output is committed.** The Python wheel ships
`pseudolife_memory/web/static/**` and is built without Node, so a source
change here is not live until `npm run build` has run and its output is
committed with it.

## Notes

- Same browser storage keys as the classic console: `pl_token` (bearer
  token) and `pl_theme` (`dark` or `light`), so one token works in both.
- No runtime requests to third-party hosts: Geist and Geist Mono (variable
  woff2 from the `geist` npm package, version 1.7.2) are vendored under
  `src/fonts/` with their SIL Open Font License (`src/fonts/LICENSE.txt`).
  The package itself is not a dependency: it declares Next.js as a peer, which
  `npm ci` would install for nothing. To update the fonts, copy the two files
  from a newer release and note the version here.
- The Board is read-only. It reads `GET /api/agents?view=coordination`;
  message bodies are never served to the Console and there is no compose.
