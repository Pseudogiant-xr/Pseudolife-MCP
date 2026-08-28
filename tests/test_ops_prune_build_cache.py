"""Build-cache retention: ``ops/prune-build-cache.ps1|.sh`` + update wiring.

Why this exists: ``update.ps1`` rebuilds the daemon image on every deploy and
nothing ever removed the BuildKit cache those rebuilds create — 51.87GB across
169 entries by 2026-07-28, and a single deploy measured at 12.45GB/17 entries.

Build cache is pure derived data, so over-pruning only costs rebuild time. The
load-bearing assertion here is therefore NEGATIVE: the scripts must be
incapable of touching images, containers, or volumes. The stub fails the test
on any such call.

Same harness as ``test_ops_prune_rollbacks.py``: drive the REAL script with
``docker`` (and ``wsl``) stubbed as shell functions, so each script's exact CLI
contract is pinned without a Docker daemon.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PS1_SCRIPT = REPO / "ops" / "prune-build-cache.ps1"
SH_SCRIPT = REPO / "ops" / "prune-build-cache.sh"
UPDATE_PS1 = REPO / "ops" / "update.ps1"
UPDATE_SH = REPO / "ops" / "update.sh"
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _find_bash() -> str | None:
    # Prefer Git Bash on Windows — System32 bash.exe launches WSL, where the
    # C:-style script paths don't resolve.
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(cand).exists():
            return cand
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


BASH = _find_bash()

# Forbidden verbs. Any of these reaching the stub fails the run outright.
#
# Matched as a PREFIX of the logged call, which is the argv joined by spaces
# with no leading "docker" — every entry here is therefore the first token(s)
# of a docker invocation. Substring matching would be wrong twice over: it
# would never fire on a "docker rm" written with the binary name, and bare
# "rm" is a substring of "--format", so `docker system df --format ...`
# would fail the guard.
#
# The container verbs were added 2026-07-29. This module's own docstring and
# docs/runbooks/docker-disk-retention.md both claimed containers were
# protected, but no container verb was ever on this list, so the claim was
# decoration. Build-cache retention has no business stopping or removing a
# container — the one it would kill is the daemon serving the deploy that
# just invoked it.
FORBIDDEN = ("system prune", "rmi", "image rm", "volume",
             "container", "rm ", "stop ", "kill ")


def _docker_timestamp(when: datetime) -> str:
    """Format like Docker/Go's ``time.Time`` string output:
    ``2026-07-28 05:31:09.884805417 +0000 UTC`` (nanosecond fraction)."""
    nanos = when.microsecond * 1000
    return when.strftime("%Y-%m-%d %H:%M:%S.") + f"{nanos:09d} +0000 UTC"


def _fixture(size="12.45GB", entries=None, distro=True, sizes=None):
    """Default state: a fresh post-deploy cache, every entry minutes old.

    ``sizes`` sequences the Build Cache figure returned by successive
    ``docker system df`` calls within a single script run (the script calls
    it up to three times: before the age pass, after the age pass, and
    after the optional ceiling pass). The last value repeats once the
    sequence is exhausted, so a single-element list (the default, built
    from ``size``) reproduces the old static-fixture behaviour exactly.
    Consumed by both ``_run_ps1``'s and ``_run_sh``'s stubs.
    """
    if entries is None:
        now = datetime.now(timezone.utc)
        entries = [
            {"created": _docker_timestamp(now - timedelta(minutes=3)),
             "size": "250B"},
            {"created": _docker_timestamp(now - timedelta(minutes=3, seconds=2)),
             "size": "8.192kB"},
        ]
    if sizes is None:
        sizes = [size]
    return {
        "df": [("Images", "44.22GB"), ("Containers", "1.532MB"),
               ("Local Volumes", "283.6MB")],
        "buildCacheSizes": list(sizes),
        "du": entries,
        "distro": distro,
    }


def _run_ps1(tmp_path: Path, fixture: dict, *args: str):
    fx_path = tmp_path / "fixture.json"
    fx_path.write_text(json.dumps(fixture), encoding="utf-8")
    calls_log = tmp_path / "calls.log"
    calls_log.write_text("", encoding="utf-8")
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        f'''
$fx = Get-Content -Raw "{fx_path}" | ConvertFrom-Json
$global:DfCallIndex = 0
function global:docker {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    Add-Content "{calls_log}" ($a -join ' ')
    if ($a[0] -eq "system" -and $a[1] -eq "df") {{
        # Successive calls walk $fx.buildCacheSizes (last value repeats once
        # exhausted) so tests can pin the before/after-age/after-ceiling
        # measurements independently instead of one static figure.
        $sizes = @($fx.buildCacheSizes)
        $idx = [Math]::Min($global:DfCallIndex, $sizes.Count - 1)
        $global:DfCallIndex++
        $rows = @($fx.df | ForEach-Object {{ "$($_[0])|$($_[1])" }})
        $rows += "Build Cache|$($sizes[$idx])"
        return $rows
    }}
    if ($a[0] -eq "builder" -and $a[1] -eq "du") {{
        return @($fx.du | ForEach-Object {{ "$($_.created)|$($_.size)" }})
    }}
    if ($a[0] -eq "builder" -and $a[1] -eq "prune") {{ return "Total: 0B" }}
    # Forbidden verbs are deliberately PERMISSIVE here: the stub must not die
    # on them (that would fail the run via proc.returncode before the
    # FORBIDDEN loop in the test ever inspects calls.log). Logged above via
    # Add-Content like every other call; the test's own loop is what must
    # catch these, not this stub's strictness.
    if ($a[0] -eq "rmi") {{ return }}
    if ($a[0] -eq "image" -and $a[1] -eq "rm") {{ return }}
    if ($a[0] -eq "system" -and $a[1] -eq "prune") {{ return }}
    if ($a[0] -eq "volume") {{ return }}
    throw "unexpected docker call: $($a -join ' ')"
}}
function global:wsl {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    Add-Content "{calls_log}" ("wsl " + ($a -join ' '))
    if (-not $fx.distro) {{ $global:LASTEXITCODE = 255 }}
    return
}}
& "{PS1_SCRIPT}" {" ".join(args)}
''',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=120,
    )
    calls = [ln for ln in calls_log.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return proc, calls


def _run_sh(tmp_path: Path, fixture: dict, *args: str):
    fx_dir = tmp_path / "fx"
    fx_dir.mkdir(exist_ok=True)
    (fx_dir / "df_base.txt").write_text(
        "".join(f"{t}|{s}\n" for t, s in fixture["df"]),
        encoding="utf-8", newline="\n")
    sizes = fixture["buildCacheSizes"]
    (fx_dir / "sizes.txt").write_text(
        "".join(f"{s}\n" for s in sizes),
        encoding="utf-8", newline="\n")
    (fx_dir / "df_idx").write_text("0", encoding="utf-8", newline="\n")
    (fx_dir / "du.txt").write_text(
        "".join(f"{e['created']}|{e['size']}\n" for e in fixture["du"]),
        encoding="utf-8", newline="\n")
    calls_log = tmp_path / "calls.log"
    calls_log.write_text("", encoding="utf-8")
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f'''#!/usr/bin/env bash
set -u
export FX="{fx_dir.as_posix()}"
export CALLS="{calls_log.as_posix()}"
export DISTRO_OK={"1" if fixture["distro"] else "0"}
export SIZES_COUNT={len(sizes)}
docker() {{
    echo "$*" >> "$CALLS"
    if [ "$1" = "system" ] && [ "$2" = "df" ]; then
        # Successive calls walk $FX/sizes.txt (last value repeats once
        # exhausted) so tests can pin the before/after-age/after-ceiling
        # measurements independently instead of one static figure. State
        # lives in a file because each call to `docker system df` here runs
        # inside its own process-substitution subshell, so a plain shell
        # variable would not survive between calls.
        idx=$(cat "$FX/df_idx")
        useidx=$idx
        if [ "$useidx" -ge "$SIZES_COUNT" ]; then
            useidx=$((SIZES_COUNT - 1))
        fi
        echo $((idx + 1)) > "$FX/df_idx"
        cat "$FX/df_base.txt"
        line=$(sed -n "$((useidx + 1))p" "$FX/sizes.txt")
        echo "Build Cache|$line"
    elif [ "$1" = "builder" ] && [ "$2" = "du" ]; then
        cat "$FX/du.txt"
    elif [ "$1" = "builder" ] && [ "$2" = "prune" ]; then
        echo "Total: 0B"
    elif [ "$1" = "rmi" ]; then
        # Forbidden verbs are deliberately PERMISSIVE here: the stub must
        # not die on them (that would fail the run via proc.returncode
        # before the FORBIDDEN loop in the test ever inspects calls.log).
        # Logged above like every other call; the test's own loop is what
        # must catch these, not this stub's strictness.
        return 0
    elif [ "$1" = "image" ] && [ "$2" = "rm" ]; then
        return 0
    elif [ "$1" = "system" ] && [ "$2" = "prune" ]; then
        return 0
    elif [ "$1" = "volume" ]; then
        return 0
    else
        echo "unexpected docker call: $*" >&2
        return 1
    fi
}}
wsl() {{
    echo "wsl $*" >> "$CALLS"
    [ "$DISTRO_OK" = "1" ] || return 255
    return 0
}}
export -f docker
export -f wsl
bash "{SH_SCRIPT.as_posix()}" "$@"
''',
        encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [BASH, str(driver), *args],
        capture_output=True, text=True, timeout=120,
    )
    calls = [ln for ln in calls_log.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return proc, calls


# PowerShell flag -> bash flag. One test body, two CLIs.
_SH_FLAGS = {
    "-MaxAgeHours": "--max-age-hours",
    "-MaxUsedSpaceGB": "--max-used-space-gb",
    "-DryRun": "--dry-run",
    "-NoTrim": "--no-trim",
}


@pytest.fixture(params=["ps1", "sh"])
def prune(request, tmp_path):
    """Run the retention script variant under test.
    Call as ``prune(fixture, "-DryRun")``; returns (proc, docker_calls)."""
    if request.param == "ps1":
        if PWSH is None:
            pytest.skip("PowerShell not on PATH")

        def run(fixture, *args):
            return _run_ps1(tmp_path, fixture, *args)
    else:
        if BASH is None:
            pytest.skip("bash not available")

        def run(fixture, *args):
            translated = [_SH_FLAGS.get(a, a) for a in args]
            return _run_sh(tmp_path, fixture, *translated)
    return run


def _prunes(calls):
    return [c for c in calls if c.startswith("builder prune")]


def test_never_issues_a_forbidden_docker_verb(prune):
    """THE guard. Build-cache retention must be incapable of removing images,
    containers or volumes — those are owned by prune-rollbacks.ps1 and by
    nothing at all, respectively."""
    proc, calls = prune(_fixture())
    assert proc.returncode == 0, proc.stderr
    for call in calls:
        for verb in FORBIDDEN:
            assert not call.startswith(verb), (
                f"forbidden docker verb in: {call}")


def test_age_pass_always_runs_with_the_default_window(prune):
    proc, calls = prune(_fixture())
    assert proc.returncode == 0, proc.stderr
    assert any("builder prune --all --force --filter until=168h" in c
               for c in _prunes(calls)), calls


def test_age_pass_honours_the_max_age_parameter(prune):
    proc, calls = prune(_fixture(), "-MaxAgeHours", "24")
    assert proc.returncode == 0, proc.stderr
    assert any("until=24h" in c for c in _prunes(calls)), calls


def test_ceiling_pass_is_skipped_when_under_the_cap(prune):
    # 12.45GB of cache, 20GB ceiling -> the backstop must not fire.
    proc, calls = prune(_fixture())
    assert proc.returncode == 0, proc.stderr
    assert not any("--max-used-space" in c for c in _prunes(calls)), calls


def test_ceiling_pass_fires_when_still_over_the_cap_after_the_age_pass(prune):
    fx = _fixture(size="30GB")
    proc, calls = prune(fx, "-MaxUsedSpaceGB", "8")
    assert proc.returncode == 0, proc.stderr
    assert any("builder prune --all --force --max-used-space 8000000000" in c
               for c in _prunes(calls)), calls


def test_every_prune_carries_all_for_the_containerd_image_store(prune):
    """2026-08-06 live finding (Docker Desktop, engine 29.6.2, buildx 0.35,
    containerd image store): ``docker builder prune`` WITHOUT ``--all``
    removes nothing at all — exit 0, ``Total: 0B`` — for both the age pass
    and the ceiling pass, so the cache grew unbounded (38.95GB against a
    20GB ceiling) while every retention run reported success. With
    ``--all`` the identical commands reclaimed ~20GB with the live images
    untouched. ``--all`` is load-bearing, not an optimization: every prune
    this script issues must carry it."""
    fx = _fixture(size="30GB")
    proc, calls = prune(fx, "-MaxUsedSpaceGB", "8")
    assert proc.returncode == 0, proc.stderr
    prunes = _prunes(calls)
    assert prunes, "expected at least one builder prune call"
    for c in prunes:
        assert c.startswith("builder prune --all "), (
            f"builder prune without --all is a no-op under the containerd "
            f"image store: {c}")


def test_ceiling_pass_repeats_until_the_measurement_is_under_the_cap(prune):
    """2026-08-06 live finding: one ``--max-used-space`` pass can stop well
    above the target — cache-record parent chains unwind one pass at a
    time, and containerd's GC frees space asynchronously, so the measured
    size keeps dropping across passes (38.95GB -> 35.61GB -> 20.37GB ->
    18.22GB live). The ceiling branch must therefore re-measure and repeat
    while it is still over the cap and making progress. Sequence: before
    25GB, post-age 25GB (fires), post-pass-1 22GB (still over, progress),
    post-pass-2 18GB (under the 20GB default cap — stop)."""
    fx = _fixture(sizes=["25GB", "25GB", "22GB", "18GB"])
    proc, calls = prune(fx)
    assert proc.returncode == 0, proc.stderr
    ceiling = [c for c in _prunes(calls) if "--max-used-space" in c]
    assert len(ceiling) == 2, (f"expected exactly two ceiling passes "
                               f"(22GB made progress, 18GB is under the cap): {ceiling}")
    assert "reclaimed 7.00GB" in proc.stdout, proc.stdout


def test_ceiling_pass_stops_after_a_pass_with_no_progress(prune):
    """A pass that moves the measured size not at all means the remaining
    cache is pinned (live images, running build) — repeating is futile.
    The branch must stop after one fruitless pass, warn, and still exit 0:
    retention is best-effort and must never fail an otherwise-healthy
    deploy over an unreachable ceiling."""
    fx = _fixture(sizes=["25GB", "25GB", "25GB"])
    proc, calls = prune(fx)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    ceiling = [c for c in _prunes(calls) if "--max-used-space" in c]
    assert len(ceiling) == 1, (f"expected exactly one ceiling pass before "
                               f"the no-progress stop: {ceiling}")
    assert "stalled" in (proc.stdout + proc.stderr).lower(), (
        f"a stalled ceiling must be reported, not silent\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}")


def test_ceiling_pass_uses_the_post_age_measurement_not_the_stale_before(prune):
    """The ceiling comparison must re-measure the cache AFTER the age pass,
    not reuse the pre-age ``$before`` figure. Sequence the cache at 25GB
    before the age pass and 15GB after it (both against the default 20GB
    ceiling): the age pass alone already brought the cache under the cap,
    so the ceiling pass must not fire. An implementation that compared the
    stale $before (25GB, over the cap) instead of $afterAge (15GB, under
    it) would wrongly fire the ceiling pass here."""
    fx = _fixture(sizes=["25GB", "15GB", "15GB"])
    proc, calls = prune(fx)
    assert proc.returncode == 0, proc.stderr
    assert not any("--max-used-space" in c for c in _prunes(calls)), calls


def test_ceiling_pass_warns_when_the_pass_budget_is_exhausted(prune):
    """Review finding on the 2026-08-06 fix: the loop has THREE exits, and
    the third — all 5 passes spent while still over the cap and still
    making progress — must not be the silent one. It reads as unqualified
    success ('reclaimed 36GB') while the ceiling was never met, and the
    weekly Scheduled Task's LastTaskResult stays 0. Also pins the bound
    itself: an unbounded (or differently-bounded) loop fails the exact
    count here. Sequence: before 60GB, post-age 60GB, then five passes of
    monotone progress that never reach the 20GB default cap."""
    fx = _fixture(sizes=["60GB", "60GB", "55GB", "50GB", "45GB", "40GB",
                         "35GB", "30GB"])
    proc, calls = prune(fx)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    ceiling = [c for c in _prunes(calls) if "--max-used-space" in c]
    assert len(ceiling) == 5, (f"expected exactly five ceiling passes "
                               f"(the budget), got: {ceiling}")
    assert "not reached" in (proc.stdout + proc.stderr).lower(), (
        f"an exhausted pass budget must be reported, not silent\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}")


def test_dry_run_mutates_nothing(prune):
    proc, calls = prune(_fixture(), "-DryRun")
    assert proc.returncode == 0, proc.stderr
    assert _prunes(calls) == [], "dry run must issue no prune"
    assert not any(c.startswith("wsl") and "fstrim" in c for c in calls), \
        "dry run must not fstrim — it discards blocks"
    # Must still have produced a report, else this passes vacuously against
    # a missing script.
    assert "12.45GB" in proc.stdout
    assert "until=168h" in proc.stdout


def test_no_trim_switch_skips_fstrim(prune):
    proc, calls = prune(_fixture(), "-NoTrim")
    assert proc.returncode == 0, proc.stderr
    assert not any("fstrim" in c for c in calls)


def test_fstrim_runs_by_default_on_the_docker_desktop_distro(prune):
    proc, calls = prune(_fixture())
    assert proc.returncode == 0, proc.stderr
    assert any("fstrim -v /mnt/docker-desktop-disk" in c for c in calls), calls


def test_absent_distro_skips_fstrim_without_failing(prune):
    proc, calls = prune(_fixture(distro=False))
    assert proc.returncode == 0, proc.stderr
    assert not any("fstrim" in c for c in calls)


def test_fstrim_is_invoked_through_sh_c_not_as_the_bare_e_target(prune):
    """2026-07-28 live-verification finding: ``wsl -d <distro> -e <cmd>``
    fails to resolve ``fstrim`` when passed as the bare ``-e`` target, even
    though ``/sbin`` (where ``fstrim`` lives) is on the child's ``PATH`` —
    it always failed (``execvpe(fstrim) failed: No such file or
    directory``), silently, because a failed fstrim only warns. An absolute
    path (``/sbin/fstrim``) works, and so does ``wsl -d docker-desktop -e sh
    -c "fstrim -v /mnt/docker-desktop-disk"`` — the verified-working form
    (it reclaimed 207.2MiB live) — because ``sh -c`` performs its own
    ``PATH`` lookup rather than relying on wsl's relay to do it. Pins the
    working invocation shape; must NOT regress to the bare form."""
    proc, calls = prune(_fixture())
    assert proc.returncode == 0, proc.stderr
    fstrim_calls = [c for c in calls if "fstrim" in c]
    assert fstrim_calls, "expected a wsl fstrim call"
    for c in fstrim_calls:
        assert "-e sh -c" in c, (
            f"fstrim must be invoked as `-e sh -c \"fstrim ...\"`, which "
            f"performs its own PATH lookup, not as the bare -e target: {c}"
        )
        assert "-e fstrim" not in c, (
            f"fstrim passed as the bare -e target, which wsl's relay fails "
            f"to resolve even though /sbin is on the child's PATH: {c}"
        )


def test_unparseable_build_cache_size_fails_loudly(prune):
    """2026-07-28 review finding: a malformed ``docker system df`` Build
    Cache size must fail the run, not get silently swallowed into a
    fabricated zero-byte report. The ``size=`` fixture field is passed
    through verbatim to both stub drivers, so an unparseable value here
    reproduces the same 'garbage-not-a-size' repro from the review without
    needing any fixture/driver changes. Pins parity: the ps1 throws
    'unparseable docker size' and exits non-zero; the bash port must match,
    not print '0B' and exit 0."""
    fx = _fixture(size="garbage-not-a-size")
    proc, calls = prune(fx)
    assert proc.returncode != 0, (
        "must fail loudly on an unparseable docker size, not exit 0\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert "reclaimed" not in proc.stdout, (
        "must not report a fabricated reclaim figure after an unparseable size\n"
        f"stdout: {proc.stdout!r}"
    )


def test_update_ps1_wires_cache_retention_in():
    """update.ps1 must expose -KeepCacheHours / -NoCachePrune and call the
    primitive. Retention must not abort a deploy that otherwise succeeded."""
    text = UPDATE_PS1.read_text(encoding="utf-8")
    assert "KeepCacheHours" in text
    assert "NoCachePrune" in text
    assert "prune-build-cache.ps1" in text
    assert "$KeepCacheHours = 168" in text


def test_update_sh_wires_cache_retention_in():
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "--keep-cache-hours" in text
    assert "--no-cache-prune" in text
    assert "prune-build-cache.sh" in text
    assert "KEEP_CACHE_HOURS=168" in text


def test_rollback_retention_still_runs_before_the_build():
    """Rollback retention is not cache retention and keeps its old slot: it
    reaps old image tags, which the build about to run does not reuse.

    The mirror-image placement rules for prune-build-cache (after the build,
    and unreachable on the unhealthy path) are proven by execution in
    tests/test_ops_update_rollback.py, which drives the real scripts and
    watches for the primitive's own `docker system df` — see the comment
    block there for why a `.index()` assertion could not prove it.
    """
    ps1 = UPDATE_PS1.read_text(encoding="utf-8")
    assert ps1.index("prune-rollbacks.ps1") < ps1.index("--build pseudolife-daemon"), \
        "rollback retention still belongs before the build"
