| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-CONNECT-CODEX | 2 | Codex registration (`config.toml`) and `connection.json` through Codex's app-server. `connect_cli.py`, `codex_connection.py` | The native command locates the registration in TOML and writes through the Codex app-server. #1677 finished native handling of unreadable settings and invalid inputs. See the `connect` harness row. | ported-with-substitution |
