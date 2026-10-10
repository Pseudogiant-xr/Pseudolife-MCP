### Added (2026-10-09 — native connect for JSON-file clients)

- The Rust candidate runs `pseudolife-mcp connect` natively for Claude Code
  (with its `settings.json` copy), Claude Desktop and Gemini CLI
  registrations: the health probe, plan and dry run; verification of every
  credential (the token-file check, an authenticated request sent as urllib
  sends it, an MCP handshake through the candidate's own shim, the board
  line); all-or-nothing writes with backups, a read-back compared as Python
  compares dicts, and rollback; `--read-token`; `--json`; and the restart
  and post-apply checks. Configs are read and written as CPython's `json`
  does, including `NaN`/`Infinity`, number respelling and repeated keys.
- Pairing (`--code`, `--read-code`), a Codex `config.toml`, an interactive
  prompt, a Windows User-scope daemon URL and an unattended-update schedule
  defer before any effect, as does any input outside the reproduced Python
  domain. The CLI differential harness row `connect` compares both
  implementations on disposable homes against a stand-in daemon, including
  file permissions on Windows.
- The candidate shim's handshake cache is now written byte for byte as the
  Python shim writes it (`url` first, Python's default separators and ASCII
  escapes); it was compact JSON with `url` last.
