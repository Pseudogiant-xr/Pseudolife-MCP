### Changed (2026-11-14 — every release carries the native client archives)
- The release workflow now attaches the five native `pseudolife-mcp`
  archives (Linux x86_64 and aarch64, macOS Intel and Apple silicon,
  Windows x86_64), the compose bundle and `SHA256SUMS` to every approved
  release. They no longer wait for the `PSEUDOLIFE_PUBLISH_RUST_ASSETS`
  repository variable, which is now unused. The build-only workflow
  dispatch is unchanged.
- The native CLI gains an installer-only step, `pseudolife-mcp installer
  migrate-registrations`, that moves Claude Code, Claude Desktop, Codex
  and Gemini CLI registrations made by a pipx, `pip --user` or Python
  runtime install to the native launcher, with a private backup of each
  changed file. Editable checkout installs and self-spawning
  registrations stay as they are, and the old Python client stays on
  disk, unregistered. The installers start using it in a later change.
- `PSEUDOLIFE_NATIVE_ARCHIVE_URL` points the native client's release
  download at another directory of archives and `SHA256SUMS` (a mirror or
  a test server). The archive is checked against that directory's
  `SHA256SUMS`, which proves it arrived intact, not who published it.
