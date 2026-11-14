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
- The same rule now finds the other programs Pseudolife locates and then
  runs by the path it found. In the Python commands: `pg_dump` for
  `backup`, `codex` for Codex setup and the Codex doorbell (a desktop Codex
  install only under an absolute `LOCALAPPDATA`), `tailscale` for
  `maintainer setup` and the address `invite` prints, `git`, `pipx`, `pwsh`
  and `bash` for `update`, `claude`, `pipx` and Python for the client
  updates, the `pseudolife-mcp` launcher a tunnel service is registered
  with, and the interpreter new shim runtimes are built from (a name that
  is not found stays a bare name, never a file or `pyvenv.cfg` in the
  current directory). In the native (Rust) commands: `pg_dump` for
  `backup`, `codex` for Codex setup (the same `LOCALAPPDATA` rule), and
  `tailscale` for `expose`, `maintainer setup` and `invite`; the client
  updates' `git` and `claude`, already looked for in absolute directories
  only, now follow `PATHEXT`'s order. The checks of whether `docker` is
  installed (`update`, `maintainer setup`) follow it in both.
- Not covered yet: a program started by its bare name, such as `docker`
  (and `git`, `pwsh` and `bash` in the native `update`), is still found by
  the operating system's or runtime's own search, which on Windows
  includes the current directory unless `NoDefaultCurrentDirectoryInExePath`
  is set.
