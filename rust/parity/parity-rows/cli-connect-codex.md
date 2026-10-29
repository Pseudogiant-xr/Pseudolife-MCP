| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-CONNECT-CODEX | 2 | Codex registration (`config.toml`) and `connection.json` through Codex's app-server. `connect_cli.py`, `codex_connection.py` | Native defers by name before any effect. Blocker: no TOML writer or Codex app-server JSON-RPC client in the candidate | deferred |
