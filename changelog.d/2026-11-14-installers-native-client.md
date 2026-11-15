### Added (2026-11-14 — the installers install the native client when asked)
- `ops/install.sh` and `ops/install.ps1` still install the Python client by
  default, exactly as before. With `PSEUDOLIFE_SHIM_ENGINE=native` they
  install the native `pseudolife-mcp` client instead (the stdio shim every
  agent registers and the CLI), through `ops/install-native.sh` /
  `ops/install-native.ps1` with the same arguments.
  `PSEUDOLIFE_SHIM_ENGINE=python` is the default spelled out, and brings a
  machine on the native client back to the Python client; any other value
  is refused before anything changes, naming the two it accepts.
- Asked for native, the installer downloads the release archive for the host
  (Linux x86_64 and aarch64, macOS Intel and Apple silicon, Windows x86_64)
  and `SHA256SUMS` from the release the checkout names, verifies the
  archive, runs it once from a private staging directory, and checks that
  it answers `installer --capability` with this installer's
  `installer-protocol` level, before anything changes. A host with no
  published archive, a release without the native assets, a checksum
  mismatch, a binary that will not run on the host (for example an older
  glibc), or a release older than the installer (0.24.0 is one) refuses
  with its cause and fix. Only then is it installed with
  `pseudolife-mcp runtime install`.
- The native install moves Claude Code, Codex, Gemini CLI and Claude
  Desktop registrations made by pipx, `pip --user` or the Python runtime to
  the native launcher, each changed file backed up first, and says where
  the old Python client stays on disk; it is never deleted. Editable
  checkout installs and self-spawning registrations are left as they are. A
  rerun on a native host says nothing changed.
- On Windows the native launcher's directory is put first on the user PATH.
  `PSEUDOLIFE_NATIVE_ARCHIVE_URL` (a mirror or a local directory) and
  `PSEUDOLIFE_NATIVE_TARGET` (one of the five release targets) override the
  download and the detected target.
- `pseudolife-mcp installer --capability` prints
  `pseudolife-mcp installer-protocol <N>`, the level of installer steps the
  binary carries.
