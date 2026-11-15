### Changed (2026-11-14 — a native install needs no Python on the host at all)
- With `PSEUDOLIFE_SHIM_ENGINE=native`, the installer's preflight and
  Claude Code session-hook steps need no Python either:
  `ops/preflight-native.sh` and `ops/preflight-native.ps1` no longer refuse
  without Python 3.10 or check for Python, venv or pipx. Their Docker
  ownership check (a foreign stack, an unrecorded bank or state volume, a
  changed Postgres password, a different checkout's daemon or its bearer, a
  lite bank, a busy port) is the native client's `pseudolife-mcp installer
  preflight-check`, with the same refusals as before. Like the other
  installer steps, it runs `docker` only from an absolute `PATH` directory.
- `ops/install-hook-native.sh` edits settings.json and hooks.json through
  the native client's `pseudolife-mcp installer hook-settings`, with the
  same hooks, backups and exit codes as `ops/install-hook.sh`, which the
  default installer keeps using with python3.
- Both native scripts run the client the installer just verified, else
  `PSEUDOLIFE_NATIVE_CLI`, `PSEUDOLIFE_SHIM_LAUNCHER` or the installed
  launcher, as `ops/update.sh` does, and name how to get it when none is
  there. `pseudolife-mcp installer --capability` now answers
  `installer-protocol 3`, which the native installer requires.
