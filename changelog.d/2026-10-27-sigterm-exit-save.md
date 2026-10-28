### Fixed (2026-10-27 — the daemon saves its state when stopped with SIGTERM)

- Stopping the HTTP daemon with SIGTERM (`kill`, a service manager, a stop
  that is not Ctrl-C) no longer loses what changed since the last autosave,
  up to `PSEUDOLIFE_MCP_AUTOSAVE_SECONDS` (30 s) of it: the interaction
  counters, access counts and unsaved cortex, world-fact and lesson slots.
  uvicorn re-raises SIGTERM after its graceful shutdown with the default
  handler, which exits without running the `atexit` exit save; a daemon
  running as PID 1 (the Docker image) drops that signal and was not
  affected. The daemon now saves from the app's lifespan shutdown, which
  runs first, and keeps `atexit` as the fallback; the save runs once even
  when both paths reach it. Ctrl-C (SIGINT) and the embedded stdio server
  already saved and still do.
