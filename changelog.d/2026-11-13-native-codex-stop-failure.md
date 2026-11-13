### Fixed (2026-11-13 — native updater no longer asks Codex to approve the Claude-only StopFailure hook)

- The native `pseudolife-mcp update` and `doctor` now leave the plugin's
  Claude-only `StopFailure` hook out of their Codex checks, as the Python
  updater has since 7 November 2026. Codex never runs that handler, so the
  native path reported `codex_hooks` as needing approval on every update,
  read Codex's runtime as not loaded, and treated a plugin update that only
  added the event as a changed definition to re-approve. The shared Codex
  consent cases now pin the skip for both implementations, and the Python
  half of that comparison runs in CI again.
