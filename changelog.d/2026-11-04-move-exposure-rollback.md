### Fixed (2026-11-04 — Require consent and undo new move target exposure)
- `pseudolife-mcp move --yes` confirms the bank move; a new persistent target Tailscale serve requires `--expose-target` or a separate interactive confirmation. Existing connections can use `--target-url`.
- Move records exposure intent before running the remote command, handles interruption from that point, and removes a serve it created when rolling back. Existing serves remain untouched; failed cleanup records the exact manual recovery command.
