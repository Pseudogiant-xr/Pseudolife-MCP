### Security (2026-11-13 — Programs are never found in the current directory)

- `pseudolife-mcp expose` looked for `tailscale` the way Python's
  `shutil.which` does, which on Windows searches the current directory
  before `PATH`, and on every system follows a relative or empty `PATH`
  entry into it. A `tailscale.cmd`, `tailscale.exe` or `tailscale.bat` left
  in the folder you ran `expose` from (a downloaded or cloned folder, say)
  was run instead of Tailscale, and after `expose tailscale --yes` it was
  the program asked to change this machine's Tailscale Serve settings.
  `expose` now finds programs only in absolute `PATH` directories (and the
  usual Tailscale install location), never in the current directory.
- The same lookup now finds every other program Pseudolife runs by the path
  it found: `git`, `pipx`, `pwsh` and `bash` for `update`, `claude`, `pipx`
  and Python for the client updates, `codex` for Codex setup and the Codex
  doorbell, `pg_dump` for `backup`, the `pseudolife-mcp` launcher a tunnel
  service is registered with, and the interpreter new shim runtimes are
  built from.
