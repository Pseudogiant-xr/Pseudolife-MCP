"""Rollback-tag retention: ``ops/prune-rollbacks.ps1|.sh`` + update.ps1|.sh wiring.

Why this exists: update.ps1 tags a ``pre-*`` rollback image on every deploy and
never garbage-collected them — by 2026-07-14 that was ~60 stale tags inside a
177GB docker_data.vhdx, pruned by hand. The retention scripts keep the newest N
rollback tags and remove the rest, never touching the deployed tag, any image
in use by a running container, or volumes.

The tests drive the REAL scripts with a stubbed ``docker``: for PowerShell a
function (functions shadow docker.exe on command lookup), for bash an
``export -f``-ed function (inherited by the child bash running the script), so
each script's exact docker CLI contract is pinned without a Docker daemon. All
five scenarios run in one interpreter per shell (``tests/ops_harness.py``),
each in its own sandbox with its own ``rmi.log`` and exit code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ops_harness import (
    BASH,
    PWSH,
    Scenario,
    run_ps1_batch,
    run_sh_batch,
    scenario_dir,
)

REPO = Path(__file__).resolve().parents[1]
PS1_SCRIPT = REPO / "ops" / "prune-rollbacks.ps1"
SH_SCRIPT = REPO / "ops" / "prune-rollbacks.sh"
UPDATE_PS1 = REPO / "ops" / "update.ps1"
UPDATE_SH = REPO / "ops" / "update.sh"

LIVE = "sha256:" + "a" * 16   # image the running daemon container uses
ID_B = "sha256:" + "b" * 16
ID_C = "sha256:" + "c" * 16
ID_D = "sha256:" + "d" * 16
ID_E = "sha256:" + "e" * 16

DAEMON = "pseudolife-daemon"


def _fixture():
    """A realistic mid-deploy state: rollback for the new build just tagged
    (shares the live image id), three older rollbacks, plus refs that must
    never be candidates (the deployed tag, a dangling <none>)."""
    return {
        "running_containers": ["cont-daemon", "cont-pg"],
        "container_images": {
            "cont-daemon": LIVE,
            "cont-pg": "sha256:" + "f" * 16,
        },
        "tags": {
            f"{DAEMON}:0.7.0":
                {"id": LIVE, "created": "2026-07-14T12:00:00.5Z"},
            f"{DAEMON}:0.7.0-pre-update-20260714-120000":
                {"id": LIVE, "created": "2026-07-14T12:00:00.5Z"},
            f"{DAEMON}:0.7.0-pre-update-20260712-225543":
                {"id": ID_B, "created": "2026-07-12T22:55:43Z"},
            f"{DAEMON}:0.7.0-pre-linux-parity":
                {"id": ID_C, "created": "2026-07-05T09:00:00Z"},
            f"{DAEMON}:0.7.0-pre-update-20260701-000000":
                {"id": ID_D, "created": "2026-07-01T00:00:00Z"},
            f"{DAEMON}:<none>":
                {"id": ID_E, "created": "2026-06-01T00:00:00Z"},
        },
    }


def _in_use_fixture():
    fx = _fixture()
    # A (stopped-deploy recovery, say) container still runs the oldest
    # rollback image: that tag must survive even though it is beyond N.
    fx["running_containers"].append("cont-old")
    fx["container_images"]["cont-old"] = ID_D
    return fx


def _nothing_to_prune_fixture():
    fx = _fixture()
    fx["tags"] = {
        f"{DAEMON}:0.7.0": {"id": LIVE, "created": "2026-07-14T12:00:00.5Z"},
        f"{DAEMON}:0.7.0-pre-update-20260714-120000":
            {"id": LIVE, "created": "2026-07-14T12:00:00.5Z"},
    }
    return fx


# One entry per execution test: (fixture, -Keep value or None).
SCENARIOS: dict[str, tuple[dict, int | None]] = {
    "default_keeps_two": (_fixture(), None),
    "keep_three": (_fixture(), 3),
    "keep_zero": (_fixture(), 0),
    "image_in_use": (_in_use_fixture(), None),
    "nothing_to_prune": (_nothing_to_prune_fixture(), None),
}


def _ps1_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (fixture, keep) in SCENARIOS.items():
        sdir = scenario_dir(root, name)
        fx_path = sdir / "fixture.json"
        fx_path.write_text(json.dumps(fixture), encoding="utf-8")
        rmi_log = sdir / "rmi.log"
        rmi_log.write_text("", encoding="utf-8")
        # Both re-bound per scenario — a stale $global:fx would answer the
        # next scenario's docker calls from the previous fixture.
        setup = f'''
$global:fx = Get-Content -Raw "{fx_path}" | ConvertFrom-Json
$global:RmiLog = "{rmi_log}"
function global:docker {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "ps" -and $a[1] -eq "-q") {{ return @($global:fx.running_containers) }}
    if ($a[0] -eq "inspect" -and $a[1] -eq "--format" -and $a[2] -eq "{{{{.Image}}}}") {{
        return @($a[3..($a.Count - 1)] | ForEach-Object {{ $global:fx.container_images.$_ }})
    }}
    if ($a[0] -eq "image" -and $a[1] -eq "ls" -and $a[3] -eq "--format") {{
        if ($a[2] -ne "{DAEMON}") {{ return @() }}
        return @($global:fx.tags.PSObject.Properties.Name)
    }}
    if ($a[0] -eq "image" -and $a[1] -eq "inspect" -and $a[2] -eq "--format") {{
        $t = $global:fx.tags.($a[4])
        if (-not $t) {{ throw "image inspect on unknown ref: $($a[4])" }}
        return "$($t.id)|$($t.created)"
    }}
    if ($a[0] -eq "rmi") {{ Add-Content $global:RmiLog $a[1]; return }}
    throw "unexpected docker call: $($a -join ' ')"
}}
'''
        args = f"-Keep {keep}" if keep is not None else ""
        scenarios.append(Scenario(name, setup, f'& "{PS1_SCRIPT}" {args}'))
    return scenarios


def _sh_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (fixture, keep) in SCENARIOS.items():
        sdir = scenario_dir(root, name)
        fx_dir = sdir / "fx"
        fx_dir.mkdir(exist_ok=True)
        (fx_dir / "containers.txt").write_text(
            "".join(c + "\n" for c in fixture["running_containers"]),
            encoding="utf-8", newline="\n")
        (fx_dir / "container_images.txt").write_text(
            "".join(f"{c} {i}\n" for c, i in fixture["container_images"].items()),
            encoding="utf-8", newline="\n")
        (fx_dir / "tags.tsv").write_text(
            "".join(f"{ref}\t{t['id']}\t{t['created']}\n"
                    for ref, t in fixture["tags"].items()),
            encoding="utf-8", newline="\n")
        rmi_log = sdir / "rmi.log"
        rmi_log.write_text("", encoding="utf-8")
        setup = f'''
export FX="{fx_dir.as_posix()}"
export RMI_LOG="{rmi_log.as_posix()}"
docker() {{
    if [ "$1" = "ps" ] && [ "$2" = "-q" ]; then
        cat "$FX/containers.txt"
    elif [ "$1" = "inspect" ] && [ "$2" = "--format" ] && [ "$3" = "{{{{.Image}}}}" ]; then
        shift 3
        for c in "$@"; do awk -v c="$c" '$1==c{{print $2}}' "$FX/container_images.txt"; done
    elif [ "$1" = "image" ] && [ "$2" = "ls" ] && [ "$4" = "--format" ]; then
        if [ "$3" = "{DAEMON}" ]; then cut -f1 "$FX/tags.tsv"; fi
    elif [ "$1" = "image" ] && [ "$2" = "inspect" ] && [ "$3" = "--format" ]; then
        line="$(awk -F'\\t' -v r="$5" '$1==r{{print $2 "|" $3}}' "$FX/tags.tsv")"
        if [ -z "$line" ]; then echo "image inspect on unknown ref: $5" >&2; return 1; fi
        id="${{line%%|*}}"; created="${{line##*|}}"
        case "$4" in
            "{{{{.Id}}}}|{{{{.Created}}}}") echo "$id|$created" ;;
            "{{{{.Created}}}}|{{{{.Id}}}}") echo "$created|$id" ;;
            *) echo "unexpected inspect format: $4" >&2; return 1 ;;
        esac
    elif [ "$1" = "rmi" ]; then
        echo "$2" >> "$RMI_LOG"
    else
        echo "unexpected docker call: $*" >&2
        return 1
    fi
}}
export -f docker
'''
        args = f"--keep {keep}" if keep is not None else ""
        invoke = f'bash "{SH_SCRIPT.as_posix()}" {args}'
        scenarios.append(Scenario(name, setup, invoke))
    return scenarios


@pytest.fixture(scope="module")
def ps1_batch(tmp_path_factory):
    if PWSH is None:
        pytest.skip("PowerShell not on PATH")
    root = tmp_path_factory.mktemp("prune_rollbacks_ps1")
    return run_ps1_batch(root, _ps1_scenarios(root))


@pytest.fixture(scope="module")
def sh_batch(tmp_path_factory):
    if BASH is None:
        pytest.skip("bash not available")
    root = tmp_path_factory.mktemp("prune_rollbacks_sh")
    return run_sh_batch(root, _sh_scenarios(root))


@pytest.fixture(params=["ps1", "sh"])
def prune(request):
    """Look up one scenario's result for the script variant under test. Call
    as ``prune("keep_three")``; the removed refs are ``res.lines('rmi.log')``."""
    batch = request.getfixturevalue(f"{request.param}_batch")
    return batch.__getitem__


def test_default_keeps_newest_two_rollbacks(prune):
    res = prune("default_keeps_two")
    assert res.returncode == 0, res.detail("rmi.log")
    assert sorted(res.lines("rmi.log")) == [
        f"{DAEMON}:0.7.0-pre-linux-parity",
        f"{DAEMON}:0.7.0-pre-update-20260701-000000",
    ], res.detail("rmi.log")


def test_keep_parameter_overrides_retention_count(prune):
    res = prune("keep_three")
    assert res.returncode == 0, res.detail("rmi.log")
    assert res.lines("rmi.log") == [
        f"{DAEMON}:0.7.0-pre-update-20260701-000000"], res.detail("rmi.log")


def test_deployed_tag_and_dangling_ref_are_never_candidates(prune):
    # Even keep=0 must only ever remove pre-* rollback tags that no running
    # container uses: 0.7.0 and <none> stay, and the just-tagged rollback is
    # protected because the running daemon still uses its image.
    res = prune("keep_zero")
    removed = res.lines("rmi.log")
    assert res.returncode == 0, res.detail("rmi.log")
    assert f"{DAEMON}:0.7.0" not in removed, res.detail("rmi.log")
    assert f"{DAEMON}:<none>" not in removed, res.detail("rmi.log")
    assert f"{DAEMON}:0.7.0-pre-update-20260714-120000" not in removed, \
        res.detail("rmi.log")
    assert f"{DAEMON}:0.7.0-pre-update-20260712-225543" in removed, \
        res.detail("rmi.log")


def test_image_in_use_by_running_container_is_kept(prune):
    res = prune("image_in_use")
    assert res.returncode == 0, res.detail("rmi.log")
    assert res.lines("rmi.log") == [
        f"{DAEMON}:0.7.0-pre-linux-parity"], res.detail("rmi.log")


def test_nothing_to_prune_is_a_quiet_success(prune):
    res = prune("nothing_to_prune")
    assert res.returncode == 0, res.detail("rmi.log")
    assert res.lines("rmi.log") == [], res.detail("rmi.log")
    # The script must still have run and said so (otherwise this test would
    # pass vacuously against a missing script).
    assert "rollback" in res.stdout.lower(), res.detail("rmi.log")


def test_update_ps1_wires_retention_in():
    """update.ps1 must expose -KeepRollbacks and call the retention script;
    retention failures must not abort a deploy (wrapped, not bare)."""
    text = UPDATE_PS1.read_text(encoding="utf-8")
    assert "KeepRollbacks" in text
    assert "prune-rollbacks.ps1" in text
    assert "$KeepRollbacks = 2" in text


def test_update_sh_wires_retention_in():
    """update.sh (the Linux/macOS port) must mirror the wiring: a
    --keep-rollbacks flag defaulting to 2 and a non-fatal retention call."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "--keep-rollbacks" in text
    assert "prune-rollbacks.sh" in text
    assert "KEEP_ROLLBACKS=2" in text
