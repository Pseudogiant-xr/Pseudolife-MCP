### Fixed (2026-11-01 — a stray `mcp.py` in the working directory no longer makes `doctor` blame the MCP SDK)
- `pseudolife-mcp doctor` checks the shim by starting it as `python -m
  pseudolife_memory.cli`, which puts the working directory first on
  `sys.path`. Run from a directory holding a file named `mcp.py` (one turned
  up in the temp directory), the shim it started imported that file instead
  of the MCP SDK, so doctor failed with "this environment's MCP SDK predates
  v2" and advised a pip install that could not help. The check now starts
  the shim from a fresh, empty directory of its own, as the installed
  launcher already does. A relative `PSEUDOLIFE_MCP_TOKEN_FILE` is resolved
  first, and a doctor run from a source checkout still checks that
  checkout's code.
- When a file `mcp.py` or a package folder `mcp` (one holding
  `__init__.py`) does hide the SDK (any `python -m` or `python -c` run from
  its directory), the shim's SDK check names that path and says to move or
  rename it, or start from another directory, instead of blaming the SDK
  version; doctor's recovery advice says the same when the SDK check or an
  import failed. A genuinely old or missing SDK still gets the pip command.
