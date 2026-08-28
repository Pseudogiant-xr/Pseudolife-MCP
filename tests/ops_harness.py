"""Shared plumbing for the ``ops/*.ps1|.sh`` script tests.

Six modules drive the REAL deploy/backup/retention scripts with ``docker``
(and friends) stubbed as shell functions, so each script's own branching is
what is under test without a Docker daemon. Two costs were duplicated across
all of them and are owned here instead:

* the Git-Bash lookup (``_find_bash``) — five verbatim copies — and the
  hermetic-env scrub of the real backup-mirror user settings;
* one interpreter launch per *scenario*. That was 81 launches across the
  family at ~0.3s of pure process startup each. ``run_ps1_batch`` /
  ``run_sh_batch`` walk every scenario of a module inside ONE interpreter,
  each in its own sandbox directory, recording that scenario's exit code and
  its stdout/stderr separately — so the per-test assertions stay exactly what
  they were.

Isolation rules the batch runners enforce, because a shared interpreter is
where scenario leakage lives:

* every scenario gets its own directory (fixtures, call logs, captured
  output), named after the test that reads it;
* every scenario re-runs its own setup, so ``$global:``-scoped stub state
  (a call counter, say) is re-initialized rather than carried over;
* the exit code is captured PER SCENARIO right after the invocation, never
  read off the batch process at the end — several of these scripts' exit
  codes are the assertion.

A scenario that throws is recorded as exit 1 with the exception appended to
its stderr, which is what a dedicated interpreter would have produced.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PWSH = shutil.which("pwsh") or shutil.which("powershell")


def find_bash() -> str | None:
    """Git Bash, if this machine has one.

    Prefer Git Bash on Windows — System32 bash.exe launches WSL, where the
    C:-style script paths don't resolve.
    """
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(cand).exists():
            return cand
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


BASH = find_bash()

# Real user settings the backup scripts read. A maintainer's machine config
# must not change what these tests exercise.
_MIRROR_KNOBS = ("PSEUDOLIFE_BACKUP_MIRROR", "PSEUDOLIFE_BACKUP_MIRROR_KEEP")


def hermetic_env(**overrides: object) -> dict[str, str]:
    """``os.environ`` with the backup-mirror knobs scrubbed.

    Pass ``NAME=value`` to set one back deliberately (``None`` leaves it
    scrubbed) — that opt-in is the only way a test sees these.
    """
    env = os.environ.copy()
    for name in _MIRROR_KNOBS:
        env.pop(name, None)
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = str(value)
    return env


@dataclass(frozen=True)
class Scenario:
    """One batched run of a script.

    ``setup`` is shell text that prepares this scenario (stub definitions,
    ``$global:`` counter resets); ``invoke`` is the single command that runs
    the script under test, WITHOUT redirection — the runner adds the
    per-scenario stream capture. Both may reference the scenario's own
    directory through ``$ScenarioDir`` (PowerShell) / ``$SCENARIO_DIR``
    (bash).
    """

    name: str
    setup: str
    invoke: str
    # PowerShell only: override the batch's exit-code model for this scenario
    # (see EXIT_ON_COMPLETION / EXIT_FROM_LASTEXITCODE). Needed where one
    # module's old drivers disagreed — update.ps1's are split between the two.
    exit_code: str | None = None


@dataclass(frozen=True)
class ScenarioResult:
    """What a dedicated ``subprocess.run`` would have returned, per scenario."""

    name: str
    returncode: int
    stdout: str
    stderr: str
    dir: Path

    def lines(self, filename: str) -> list[str]:
        """Non-blank lines of one of this scenario's log files."""
        path = self.dir / filename
        if not path.exists():
            return []
        return [ln for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip()]

    def detail(self, log: str = "calls.log", n: int = 40) -> str:
        """Scenario context for an assertion message.

        A batched failure is worthless if it only says which file mismatched,
        so assertion messages carry this: which scenario, its exit code, the
        tail of its call log, and its captured output.
        """
        tail = self.lines(log)[-n:]
        return (f"\n--- scenario {self.name!r} (exit {self.returncode}) ---\n"
                f"{log} tail:\n" + "\n".join(f"  {c}" for c in tail) +
                f"\nstdout:\n{self.stdout}\nstderr:\n{self.stderr}\n")


class BatchRun:
    """Scenario results keyed by name, with the driver output on hand.

    A driver that aborts early leaves scenarios unrecorded; looking one up
    then fails loudly WITH the driver's output rather than handing the test a
    missing-file error several assertions later.
    """

    def __init__(self, proc: subprocess.CompletedProcess,
                 results: dict[str, ScenarioResult]):
        self.proc = proc
        self.results = results

    def __getitem__(self, name: str) -> ScenarioResult:
        if name not in self.results:
            raise AssertionError(
                f"scenario {name!r} recorded no result — the batch driver "
                f"aborted before reaching it.\n"
                f"--- driver stdout ---\n{self.proc.stdout[-4000:]}\n"
                f"--- driver stderr ---\n{self.proc.stderr[-4000:]}")
        return self.results[name]


def scenario_dir(root: Path, name: str) -> Path:
    """The sandbox for one scenario; created so fixtures can be staged."""
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# Templates are filled by literal substitution, not str.format: the setup and
# invoke text is shell source full of braces.
#
# PowerShell splits its output across four streams that a real
# ``subprocess.run`` folded into two: Write-Host lands on the information
# stream (6), Write-Output on success (1), Write-Error on error (2),
# Write-Warning on warning (3). Capturing them separately and rejoining 6+1
# as stdout and 2+3 as stderr reproduces what the caller used to see.
_PS_SCENARIO = r"""
# --- scenario @NAME@ ---
$ScenarioDir = '@DIR@'
@SETUP@
$global:LASTEXITCODE = 0
$__scenarioError = $null
$__rc = 0
try {
    @INVOKE@ > "$ScenarioDir\s1.txt" 2> "$ScenarioDir\s2.txt" 3> "$ScenarioDir\s3.txt" 6> "$ScenarioDir\s6.txt"
    $__rc = @RC_EXPR@
} catch {
    # The raw message first, THEN the formatted record: PowerShell's error
    # view hard-wraps at the console width, which would break an assertion
    # that regexes over the message (the rehearsal alarms do).
    $__scenarioError = ($_.Exception.Message + "`n" + ($_ | Out-String))
    $__rc = 1
}
if ($__scenarioError) {
    Add-Content -LiteralPath "$ScenarioDir\s2.txt" -Value $__scenarioError
}
Set-Content -LiteralPath "$ScenarioDir\rc.txt" -Value ([string]$__rc) -Encoding utf8
"""

# How a scenario's exit code is derived, per `pwsh -File`'s own rules — which
# are not "whatever $LASTEXITCODE happens to be". A script that runs to
# completion exits 0 even when the last stubbed `docker`/`wsl` call left
# $LASTEXITCODE non-zero (`absent_distro` in the build-cache tests leaves 255
# and was always a passing run); an uncaught terminating error exits 1; only
# an explicit `exit N` yields N. The ops scripts signal failure with `throw`,
# so COMPLETION is the right model for all of them EXCEPT update.ps1, whose
# unhealthy path ends in `exit 1` — those drivers propagated $LASTEXITCODE by
# hand and keep doing so here.
EXIT_ON_COMPLETION = "0"
EXIT_FROM_LASTEXITCODE = "$global:LASTEXITCODE"

# The subshell is bash's isolation: exported stub functions and per-scenario
# variables die with it, so nothing a scenario defines can reach the next.
_SH_SCENARIO = """
# --- scenario @NAME@ ---
SCENARIO_DIR='@DIR@'
(
@SETUP@
@INVOKE@
) > "$SCENARIO_DIR/s1.txt" 2> "$SCENARIO_DIR/s2.txt"
__rc=$?
echo "$__rc" > "$SCENARIO_DIR/rc.txt"
"""


def _fill(template: str, sc: Scenario, sdir: str, rc_expr: str = "") -> str:
    return (template
            .replace("@NAME@", sc.name)
            .replace("@DIR@", sdir)
            .replace("@RC_EXPR@", rc_expr)
            .replace("@SETUP@", sc.setup)
            .replace("@INVOKE@", sc.invoke))


def _collect(root: Path, scenarios: list[Scenario],
             proc: subprocess.CompletedProcess,
             stdout_parts: tuple[str, ...],
             stderr_parts: tuple[str, ...]) -> BatchRun:
    results: dict[str, ScenarioResult] = {}
    for sc in scenarios:
        sdir = root / sc.name
        rc_file = sdir / "rc.txt"
        if not rc_file.exists():
            continue

        def _read(parts: tuple[str, ...]) -> str:
            text = ""
            for part in parts:
                path = sdir / part
                if path.exists():
                    text += path.read_text(encoding="utf-8", errors="replace")
            return text

        results[sc.name] = ScenarioResult(
            name=sc.name,
            returncode=int(rc_file.read_text(encoding="utf-8").strip() or 0),
            stdout=_read(stdout_parts),
            stderr=_read(stderr_parts),
            dir=sdir,
        )
    return BatchRun(proc, results)


def run_ps1_batch(root: Path, scenarios: list[Scenario], *,
                  prelude: str = "", env: dict[str, str] | None = None,
                  exit_code: str = EXIT_ON_COMPLETION,
                  timeout: int = 600) -> BatchRun:
    """Run every scenario in one ``pwsh``, each in its own sandbox.

    ``exit_code`` picks the model the module's old per-scenario driver used —
    see ``EXIT_ON_COMPLETION`` / ``EXIT_FROM_LASTEXITCODE``.
    """
    root.mkdir(parents=True, exist_ok=True)
    blocks = [prelude]
    for sc in scenarios:
        sdir = scenario_dir(root, sc.name)
        blocks.append(_fill(_PS_SCENARIO, sc, str(sdir),
                            sc.exit_code or exit_code))
    driver = root / "batch_driver.ps1"
    driver.write_text("\n".join(blocks), encoding="utf-8")
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return _collect(root, scenarios, proc,
                    stdout_parts=("s6.txt", "s1.txt"),
                    stderr_parts=("s2.txt", "s3.txt"))


def run_sh_batch(root: Path, scenarios: list[Scenario], *,
                 prelude: str = "", env: dict[str, str] | None = None,
                 timeout: int = 600) -> BatchRun:
    """Run every scenario in one ``bash``, each in its own subshell + sandbox."""
    root.mkdir(parents=True, exist_ok=True)
    blocks = ["#!/usr/bin/env bash", "set -u", prelude]
    for sc in scenarios:
        sdir = scenario_dir(root, sc.name)
        blocks.append(_fill(_SH_SCENARIO, sc, sdir.as_posix()))
    driver = root / "batch_driver.sh"
    driver.write_text("\n".join(blocks), encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [BASH, str(driver)],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return _collect(root, scenarios, proc,
                    stdout_parts=("s1.txt",), stderr_parts=("s2.txt",))
