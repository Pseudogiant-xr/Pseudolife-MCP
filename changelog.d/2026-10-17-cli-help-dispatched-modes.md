### Fixed (2026-10-17 — `pseudolife-mcp --help` lists delegate-watch)

- `pseudolife-mcp --help` now lists `delegate-watch`, which shipped in v0.18.2 without a help line. The native shim names it as deferred instead of answering "unknown mode". Two guards keep this from recurring: every mode the CLI dispatches is listed in `--help` or named as deliberately unlisted (`shim`, the no-argument mode by name, and `doorbell-prompt-seen`, the hook the Codex doorbell installs), and every mode the CLI dispatches is served or deferred by the native shim.
