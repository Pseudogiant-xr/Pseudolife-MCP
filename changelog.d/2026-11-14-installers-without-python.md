### Changed (2026-11-14 — an install needs no Python on the host at all)
- `ops/preflight.sh` and `ops/preflight.ps1` no longer refuse without Python
  3.10 or check for Python, venv or pipx. Their Docker ownership check (a
  foreign stack, an unrecorded bank or state volume, a changed Postgres
  password, a different checkout's daemon or its bearer, a lite bank, a busy
  port) is now the native client's `pseudolife-mcp installer
  preflight-check`, with the same refusals as before. Like the other
  installer steps, it runs `docker` only from an absolute `PATH` directory.
- `ops/install-hook.sh` no longer needs python3: its settings.json and
  hooks.json edits are the native client's `pseudolife-mcp installer
  hook-settings`, with the same hooks, backups and exit codes.
- Both scripts run the client the installer just verified, else
  `PSEUDOLIFE_NATIVE_CLI`, `PSEUDOLIFE_SHIM_LAUNCHER` or the installed
  launcher, as `ops/update.sh` does, and name how to get it when none is
  there.
