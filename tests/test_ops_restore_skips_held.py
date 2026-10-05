"""``ops/restore.ps1|.sh`` never auto-picks a dump the row-count gate held.

``ops/backup.*`` marks a dump whose entries, facts or lessons fell sharply
as ``"rotation": "held"`` in its manifest
(``pseudolife_manifest-<stamp>.json``, beside the dump). With no file named,
the restore scripts used to take the NEWEST dump, which after a wipe is
exactly the held one. The rehearsal then passed, because live and restored
were both wiped (two independent reviews of PR #339, 2026-09-23). Now a
restore with no file named skips held dumps, says so, and takes the newest
one that was not held; if every dump is held it refuses. Naming the file is
the override, and a dump without a manifest (written before the gate) is
treated as before.

The auto-pick reads ``<the script's repo>/data/backups``, so each scenario
runs a COPY of the real script inside its own temp repo, with ``docker``
stubbed (every row count answers 100, so the rehearsal passes whenever it
gets as far as restoring).
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from tests.ops_harness import (
    BASH, PWSH, Scenario, run_ps1_batch, run_sh_batch, scenario_dir)

REPO = Path(__file__).resolve().parents[1]
RESTORE_PS1 = REPO / "ops" / "restore.ps1"
RESTORE_SH = REPO / "ops" / "restore.sh"

GOOD = "20260101-000000"   # older, not held
HELD = "20260102-000000"   # newer, held by the row-count gate


def _dump(stamp: str) -> str:
    return f"pseudolife_memory-{stamp}.sql.gz"


# name -> (dumps: {stamp: manifest rotation or None}, explicit stamp or None)
SCENARIOS: dict[str, tuple[dict[str, str | None], str | None]] = {
    "skips_held": ({GOOD: "ok", HELD: "held"}, None),
    "all_held": ({HELD: "held"}, None),
    "explicit_held": ({GOOD: "ok", HELD: "held"}, HELD),
    "no_manifests": ({GOOD: None, HELD: None}, None),
}


def _stage(root: Path, name: str, script: Path) -> tuple[Path, Path]:
    """A temp repo holding a copy of ``script`` and the scenario's dumps.
    Returns (script copy, backups dir). mtimes follow the stamps, since the
    restore scripts pick by modification time."""
    sdir = scenario_dir(root, name)
    repo = sdir / "repo"
    (repo / "ops").mkdir(parents=True, exist_ok=True)
    copy = repo / "ops" / script.name
    shutil.copy(script, copy)
    backups = repo / "data" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    dumps, _ = SCENARIOS[name]
    for i, (stamp, rotation) in enumerate(sorted(dumps.items())):
        path = backups / _dump(stamp)
        path.write_bytes(gzip.compress(b"-- dump\n"))
        when = time.time() - 3600 * (len(dumps) - i)
        os.utime(path, (when, when))
        if rotation is not None:
            (backups / f"pseudolife_manifest-{stamp}.json").write_text(json.dumps({
                "dump": _dump(stamp), "rotation": rotation,
                "note": "fell by more than 25% since X: public.entries 100 -> 3 (-97%)",
            }, indent=2), encoding="utf-8")
    return copy, backups


_PS1_STUB = """
function global:docker {
    $a = @($args | ForEach-Object { "$_" })
    $global:LASTEXITCODE = 0
    if (($a -join " ") -match "SELECT count") { return "100" }
    return
}
"""

_SH_STUB = """
docker() {
    case "$*" in *"SELECT count"*) echo 100 ;; esac
    return 0
}
export -f docker
"""


@pytest.fixture(scope="module")
def ps1_runs(tmp_path_factory):
    if PWSH is None:
        pytest.skip("pwsh not available")
    root = tmp_path_factory.mktemp("restore_held_ps1")
    scenarios = []
    for name, (_, explicit) in SCENARIOS.items():
        copy, backups = _stage(root, name, RESTORE_PS1)
        arg = f' -BackupFile "{(backups / _dump(explicit)).as_posix()}"' if explicit else ""
        scenarios.append(Scenario(name, _PS1_STUB, f'& "{copy}"{arg}'))
    return run_ps1_batch(root, scenarios)


@pytest.fixture(scope="module")
def sh_runs(tmp_path_factory):
    if BASH is None:
        pytest.skip("bash not available")
    root = tmp_path_factory.mktemp("restore_held_sh")
    scenarios = []
    for name, (_, explicit) in SCENARIOS.items():
        copy, backups = _stage(root, name, RESTORE_SH)
        arg = f' --backup-file "{(backups / _dump(explicit)).as_posix()}"' if explicit else ""
        scenarios.append(Scenario(name, _SH_STUB, f'bash "{copy.as_posix()}"{arg}'))
    return run_sh_batch(root, scenarios)


@pytest.fixture(params=["ps1", "sh"])
def restore(request):
    batch = request.getfixturevalue(f"{request.param}_runs")
    return lambda name: batch[name]


def _chosen(res) -> str:
    """The dump the script reported restoring (its '==> Backup:' line)."""
    for line in (res.stdout + res.stderr).splitlines():
        if line.startswith("==> Backup:"):
            return line
    return ""


def test_a_held_dump_is_skipped_for_the_newest_good_one(restore):
    """THE guard: after a wipe the held dump is the newest, and restoring
    it would put the wipe back."""
    res = restore("skips_held")
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert _dump(GOOD) in _chosen(res), out
    assert _dump(HELD) in out and "held" in out.lower(), (
        "the skip must be said out loud:\n" + out)


def test_every_dump_held_refuses_instead_of_restoring_one(restore):
    """And says why, with the way out: without the explicit refusal the run
    still fails, but on a baffling "backup artifact missing or empty"."""
    res = restore("all_held")
    out = res.stdout + res.stderr
    assert res.returncode != 0, out
    assert _chosen(res) == "", out
    assert "every backup under data" in out and "held by the row-count gate" in out, out


def test_naming_the_file_overrides_the_skip(restore):
    res = restore("explicit_held")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _dump(HELD) in _chosen(res), res.stdout + res.stderr


def test_dumps_without_manifests_are_picked_as_before(restore):
    res = restore("no_manifests")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _dump(HELD) in _chosen(res), res.stdout + res.stderr   # the newest
