### Changed (2026-11-14 — the installers run their own steps through the native client, not Python)
- `ops/install.sh` and `ops/install.ps1` no longer look for or start Python
  for their own steps. The recorded installer choices and the deployment
  fingerprints, the bearer token and the per-client token files, the
  Claude Code settings and registration environment, the Docker volume
  names, the Docker daemon engine (detect, prepare, accept, choose), the
  Codex credential, hooks and runtime-default setup, the Claude Desktop
  registration, the Claude Code plugin marketplace move and the small JSON
  and pairing-code reads are now steps of the verified native client
  (`pseudolife-mcp installer ...`), with the same answers, files and
  refusals as the Python helpers they replace.
- These steps run `docker` and `git` only from an absolute `PATH`
  directory, never from the current directory, a relative `PATH` entry or
  the folders Windows also searches by itself (its system directory among
  them), so a `docker.exe` left in a checkout is never started.
- A first install checks its flags before the native client is downloaded,
  so a mistyped flag is refused at once; a rerun downloads it first, because
  it reads its recorded choices through it.
