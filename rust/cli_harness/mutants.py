"""Mutant control: break the Rust leaf on purpose and require a red harness.

Each mutant is one exact source substitution in a copy of ``rust/``. The copy
lives beside the build output (``$CARGO_TARGET_DIR/mutant-src``) and builds
into ``$CARGO_TARGET_DIR/mutant``, so the worktree and the ordinary build are
never touched. A mutant the row's cases do not catch fails the control: a
harness that cannot see a deliberate break proves nothing.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import compare, core, rows

HERE = Path(__file__).resolve().parent
RUST = HERE.parent


@dataclasses.dataclass(frozen=True)
class Mutant:
    id: str
    row: str
    file: str      # relative to rust/
    old: str
    new: str
    cases: tuple[str, ...] = ()  # run only these (default: the whole row)


# Each row module owns its list as ``MUTANTS``; see rows.mutants().
SKIP_BANK = False


def _target() -> Path:
    return Path(os.environ.get("CARGO_TARGET_DIR", RUST / "target"))


def _sync_source(mutants: list[Mutant]) -> Path:
    copy = _target() / "mutant-src" / "rust"
    ignore = shutil.ignore_patterns("target", "cli_harness", "__pycache__")
    shutil.copytree(RUST, copy, dirs_exist_ok=True, ignore=ignore)
    now = time.time()
    # Copies keep the original mtime; a file a previous mutant changed must
    # look newer than the last build or Cargo would reuse the mutant binary.
    for mutant in mutants:
        os.utime(copy / mutant.file, (now, now))
    return copy


def _build(copy: Path) -> Path:
    env = dict(os.environ, CARGO_TARGET_DIR=str(_target() / "mutant"))
    result = subprocess.run(["cargo", "build", "--locked", "--bin", "pseudolife-stdio", "-j", "4"],
                            cwd=copy, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("mutant build failed:\n" + result.stderr[-3000:])
    name = "pseudolife-stdio.exe" if core.WINDOWS else "pseudolife-stdio"
    return _target() / "mutant" / "debug" / name


def run_mutant(mutant: Mutant, oracle: core.Target, verbose: bool,
               all_mutants: list[Mutant]) -> tuple[bool, list[str]]:
    copy = _sync_source(all_mutants)
    path = copy / mutant.file
    data = path.read_bytes()
    # Bytes, not text: the checkout's line endings stay as they are.
    for eol in ("\n", "\r\n"):
        old, new = (s.replace("\n", eol).encode() for s in (mutant.old, mutant.new))
        if data.count(old) == 1:
            break
    else:
        raise RuntimeError(f"{mutant.id}: expected exactly one match in {mutant.file}")
    path.write_bytes(data.replace(old, new))
    binary = _build(copy)
    candidate = core.rust_target(binary)
    caught: list[str] = []
    for case in rows.load(mutant.row):
        if (not case.runs_here() or (mutant.cases and case.id not in mutant.cases)
                or (case.bank and SKIP_BANK)):
            continue
        want, got = core.run_case(case, oracle, candidate)
        if compare.diff(want, got, case.rules):
            caught.append(case.id)
    return bool(caught), caught


def main(row_names: list[str], only: list[str], oracle: core.Target, verbose: bool) -> int:
    pool = [m for row in rows.ROWS for m in rows.mutants(row)]
    selected = [m for m in pool if m.row in row_names and (not only or m.id in only)]
    if not selected:
        raise SystemExit("no mutants selected")
    escaped = []
    for mutant in selected:
        killed, cases = run_mutant(mutant, oracle, verbose, pool)
        print(f"  {'caught ' if killed else 'ESCAPED'} {mutant.id}"
              + (f" by {', '.join(cases)}" if cases else ""), flush=True)
        if not killed:
            escaped.append(mutant.id)
    # Leave the copy unmutated so a later ordinary build of it is clean.
    _sync_source(pool)
    print(f"  {len(selected) - len(escaped)}/{len(selected)} mutants caught")
    return 1 if escaped else 0
