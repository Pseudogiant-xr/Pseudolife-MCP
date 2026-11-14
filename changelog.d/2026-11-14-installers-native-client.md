### Changed (2026-11-14 — the installers install the native client, not a Python venv)
- `ops/install.sh` and `ops/install.ps1` now install the native
  `pseudolife-mcp` client (the stdio shim every agent registers and the
  CLI) from the release this checkout names: they download the archive
  for the host (Linux x86_64 and aarch64, macOS Intel and Apple silicon,
  Windows x86_64) and `SHA256SUMS`, verify the archive, run it once from
  a private staging directory, and only then install it with
  `pseudolife-mcp runtime install`. No `pipx`, `pip --user` or
  side-by-side Python venv is built for the client any more, and there is
  no fallback to one.
- A host with no published archive, a release without the native assets,
  a checksum mismatch, or a binary that will not run on the host (for
  example an older glibc) refuses before anything changes, naming the
  cause and the fix.
- A rerun on a host with the Python client moves Claude Code, Codex,
  Gemini CLI and Claude Desktop registrations made by pipx, `pip --user`
  or the Python runtime to the native launcher, each changed file backed
  up first, and says in one line where the old Python client stays on
  disk; it is never deleted. Editable checkout installs and self-spawning
  registrations are left as they are. A rerun on a native host says
  nothing changed.
- On Windows the launcher's directory is put first on the user PATH, as
  before. `PSEUDOLIFE_NATIVE_ARCHIVE_URL` (a mirror or a local directory)
  and `PSEUDOLIFE_NATIVE_TARGET` (one of the five release targets) override
  the download and the detected target.
- The installers' other helpers (the bearer token, recorded choices,
  preflight, Codex and Claude Desktop setup) still run on Python 3.10 or
  newer until a later change replaces them.
