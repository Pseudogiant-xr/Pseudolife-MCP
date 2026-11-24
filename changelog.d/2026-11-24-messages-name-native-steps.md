### Fixed (2026-11-24 — messages name steps an installed client has) [#1609]
- The native client's version-mismatch notes (shim stderr, served instructions, `doctor`) no longer add a checkout-only `python ops/update_clients.py` step; `pseudolife-mcp update --clients-only --tag <version>` is the step.
- `doctor` no longer tells a native client to run `-m pip check` on a Python interpreter when its own stdio shim fails the MCP handshake; it points at daemon health, the shim's stderr and an installer rerun.
- `connect` names the installer (`--client claude-desktop`) instead of `python ops/register_claude_desktop.py`, and `pseudolife-mcp update --clients-only` instead of `python ops/shim_runtime.py migrate`.
- `import` of an export with other embedding dimensions no longer names `ops/migrate_embeddings.py`; it says which bank the export loads into and that this client does not re-embed.
- `pseudolife-mcp --help`, `ops/install-hook-native.sh --client codex --remove-legacy` and the plugin README name the installer and `update` instead of checkout Python scripts.
