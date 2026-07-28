"""Guard: every tracked ``ops/*.sh`` file must carry the executable bit.

Why this matters: git tracks the executable bit as part of a file's mode in
the index (100644 vs 100755). A shell script checked in as 100644 clones
onto Linux/macOS as non-executable. Anything that invokes it by bare path
(``"$(dirname "$0")/foo.sh"``, or a doc telling a user to run ``ops/foo.sh``
directly) gets ``bash: ops/foo.sh: Permission denied`` (exit 126) on a fresh
clone — `chmod +x` was never run, because the bit was never set in git.

This bit rotted silently once already: ``ops/prune-rollbacks.sh`` has been
100644 since commit b47430de (2026-07-14, "port rollback-tag retention to
update.sh"), which means rollback-tag image retention has been a silent
no-op on every Linux/macOS deploy since that commit — ``ops/update.sh``
invokes it as ``"$(dirname "$0")/prune-rollbacks.sh"``, that call fails with
Permission denied, and the failure is swallowed by
``if ! ...; then echo WARNING; fi`` (see ``ops/update.sh``), so nothing
ever surfaced it. ``ops/install-shim-autostart.sh``, ``ops/preflight.sh``
and ``ops/prune-build-cache.sh`` carried the same 100644 defect; and
``ops/install.sh`` being 100644 breaks the documented quickstart
(README.md and every translated ``docs/i18n/README.*.md`` tell a fresh
Linux/macOS clone to run ``ops/install.sh`` directly as the first step).

The fix is a one-line ``git update-index --chmod=+x`` per file; this test
exists so the bit cannot silently regress back to 100644 on a future edit
(e.g. a Windows checkout + re-add, or a naive ``git apply``, both of which
can drop the bit).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _tracked_ops_sh_modes() -> dict[str, str]:
    """Read the git index directly (``git ls-files -s``) rather than
    ``os.stat`` — the executable bit is a property of the git index entry,
    not (reliably) of the working-tree file: Windows checkouts and some
    editors don't preserve POSIX mode bits on disk, but git still records
    and ships whatever mode is committed. The index is the thing that
    actually reaches a Linux/macOS clone."""
    out = subprocess.run(
        ["git", "ls-files", "-s", "ops/*.sh"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    modes: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        # "<mode> <blob-sha> <stage>\t<path>"
        meta, path = line.split("\t", 1)
        mode = meta.split()[0]
        modes[path] = mode
    return modes


def test_every_tracked_ops_shell_script_is_executable_in_the_index():
    """A 100644 ``ops/*.sh`` file is Permission denied on a fresh
    Linux/macOS clone the moment anything invokes it by bare path — which
    ``ops/update.sh`` does for both ``prune-rollbacks.sh`` and
    ``prune-build-cache.sh``, and which the README quickstart does for
    ``ops/install.sh``. ``.ps1`` files have no executable-bit concept on
    Windows and are intentionally out of scope."""
    modes = _tracked_ops_sh_modes()
    assert modes, "expected to find tracked ops/*.sh files — did the glob break?"
    non_executable = {path: mode for path, mode in modes.items() if mode != "100755"}
    assert not non_executable, (
        "these ops/*.sh files are not executable (100755) in the git index "
        "and will fail with 'Permission denied' when invoked by bare path "
        f"on a fresh Linux/macOS clone: {non_executable}. Fix with: "
        "git update-index --chmod=+x <path>"
    )
