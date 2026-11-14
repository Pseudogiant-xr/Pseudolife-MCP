### Added (2026-11-14 — the native client plans a bank move)

- `pseudolife-mcp move --dry-run` in the native (Rust) client now checks
  both hosts of a Docker bank move and prints the plan, where it used to stop
  with "mode 'move' is deferred in this candidate". It changes nothing on
  either host: it takes no update lock, writes no file of its own (the native
  launcher keeps only its usual runtime usage lock) and runs only reads
  (Docker `version` and `inspect`, read-only PostgreSQL queries, `du` of
  `/data`, `/health`). The
  target's half runs in the native client on the target, reached over
  key-based ssh; a target without the native client, with an older one, or
  with a different move protocol is refused with the fix. A target must be an
  installed Docker bank (one compose stack, not a source checkout) that is
  healthy, empty (a fresh bank's own warm-up search aside), at least the
  source's schema and PostgreSQL major version, with room for the move's
  files and the restore's 8 GiB margin; `--target-url` must reach the
  target's own daemon, and for a fresh target, which has no identity yet,
  the plan says the URL is matched only by release and schema.
- ssh never prompts and never weakens host-key checking: an unknown or
  changed host key fails with ssh's refusal and the fix. `ssh` and `docker`
  are found only in absolute `PATH` directories.
- For `--expose-target`, carried tokens (pass `--no-keep-tokens`),
  `--target-env-file`, `--target-compose-file` and `--target-checkout`, the
  native client refuses before any effect, naming what to do instead.
- A source daemon on the host network is read where it binds
  (`PSEUDOLIFE_MCP_HOST` / `PSEUDOLIFE_MCP_PORT`, default
  `127.0.0.1:8765`), as the Python move's fixed source address did.
