### Changed (2026-11-14 — the native installer runs its own steps through the native client, not Python)
- Asked for the native client (`PSEUDOLIFE_SHIM_ENGINE=native`),
  `ops/install.sh` and `ops/install.ps1` no longer look for or start Python
  for their own steps. The recorded installer choices and the deployment
  fingerprints, the bearer token and the per-client token files, the
  Claude Code settings and registration environment, the Docker volume
  names, the Docker daemon engine (detect, prepare, accept, choose), the
  Codex credential, hooks and runtime-default setup, the Claude Desktop
  registration, the Claude Code plugin marketplace move and the small JSON
  and pairing-code reads are then steps of the verified native client
  (`pseudolife-mcp installer ...`), with the same answers, files and
  refusals as the Python helpers they replace. The default installer, which
  installs the Python client, keeps its Python helpers.
- These steps run `docker` and `git` only from an absolute `PATH`
  directory, never from the current directory, a relative `PATH` entry or
  the folders Windows also searches by itself (its system directory among
  them), so a `docker.exe` left in a checkout is never started.
- An unattended first native install checks its flags before the native
  client is downloaded, so a mistyped flag is refused at once. At a
  terminal the client is downloaded and checked before the first question,
  so a release that cannot be used is refused before anything is asked; a
  flag checked after the questions (`--engine`, the client-only flags) is
  then refused after the download. Unknown flags are refused at once either
  way. A rerun downloads it first, because it reads its recorded choices
  through it.
- The native installer runs these steps only from a staged release that
  carries them: one whose `installer --capability` answers the installer's
  protocol level.
- The installer preflight (`ops/preflight.sh`, `ops/preflight.ps1`) and the
  Claude Code session-hook writer `ops/install-hook.sh` still need Python
  3.10 or newer until a following change replaces them.
