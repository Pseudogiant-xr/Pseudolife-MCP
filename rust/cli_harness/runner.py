"""Command line: run rows live, record goldens, compare against goldens, or
run the mutant control. Exit 0 only when every selected case matches (or,
for ``--mutants``, when every mutant is caught)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import compare, core, normalize, producers, rows

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
GOLDENS = HERE / "goldens"


def _default_candidate() -> Path:
    name = "pseudolife-stdio.exe" if core.WINDOWS else "pseudolife-stdio"
    import os
    target = Path(os.environ.get("CARGO_TARGET_DIR", REPO / "rust" / "target"))
    return target / "debug" / name


def _golden_path(row: str) -> Path:
    return GOLDENS / f"{row}.{core.PLATFORM}.json"


def _select(row: str, wanted: list[str], bank: bool = True) -> list[core.Case]:
    selected = [c for c in rows.load(row) if c.runs_here() and (bank or not c.bank)]
    if wanted:
        selected = [c for c in selected if c.id in wanted]
        missing = set(wanted) - {c.id for c in selected}
        if missing:
            raise SystemExit(f"unknown or skipped case(s): {sorted(missing)}")
    return selected


def _label(case: core.Case) -> str:
    """A case's id, naming any real host program it is allowed to run."""
    if not case.real_programs:
        return case.id
    return f"{case.id}  [real programs: {', '.join(case.real_programs)}]"


def run_row(row: str, cases: list[core.Case], oracle: core.Target | None,
            candidate: core.Target, golden: dict | None, verbose: bool) -> dict:
    results = {}
    for case in cases:
        started = time.monotonic()
        if golden is not None:
            want = golden["cases"].get(case.id)
            if want is None:
                results[case.id] = {"status": "error", "detail": ["no golden recorded"]}
                continue
            got = core.run_arm(case, candidate, core._home_root() / "h")
            diffs = compare.diff(want, got, case.rules)
        else:
            want, got = core.run_case(case, oracle, candidate)
            diffs = compare.diff(want, got, case.rules)
        status = "match" if not diffs else "DIFF"
        results[case.id] = {"status": status, "detail": diffs,
                            "seconds": round(time.monotonic() - started, 2)}
        if case.real_programs:
            results[case.id]["real_programs"] = list(case.real_programs)
        print(f"  {status:5} {_label(case)}", flush=True)
        if diffs and verbose:
            for line in diffs:
                print(f"        {line}")
    return results


def record(row: str, cases: list[core.Case], oracle: core.Target, source: Path,
           commit: str | None) -> Path:
    import subprocess
    if not commit:
        commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip() or "unknown"
    golden = {"row": rows.ROWS[row], "platform": core.PLATFORM, "oracle_commit": commit,
              "cases": {}}
    for case in cases:
        obs = core.run_arm(case, oracle, core._home_root() / "h")
        normal = normalize.apply(obs, (), obs["home"])
        normal["home"] = None
        normal.pop("daemon_url", None)
        golden["cases"][case.id] = normal
        print(f"  recorded {_label(case)} (exit {obs['exit']})", flush=True)
    path = _golden_path(row)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(golden, indent=1, ensure_ascii=True) + "\n", encoding="utf-8",
                    newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli_harness",
                                     description="Python CLI vs Rust CLI differential harness")
    parser.add_argument("--row", action="append", choices=sorted(rows.ROWS), required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--oracle-python", default=sys.executable)
    parser.add_argument("--oracle-source", type=Path, default=REPO)
    parser.add_argument("--candidate", type=Path, default=None)
    parser.add_argument("--oracle-commit", help="recorded in goldens when the source has no .git")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="write goldens from the oracle")
    mode.add_argument("--golden", action="store_true", help="compare against goldens")
    mode.add_argument("--mutants", action="store_true", help="run the mutant control")
    parser.add_argument("--mutant", action="append", default=[], help="only these mutants")
    parser.add_argument("--skip-bank", action="store_true",
                        help="skip cases that need real daemons on disposable banks")
    parser.add_argument("--out", type=Path, help="write a JSON summary")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    producers.use_oracle(args.oracle_source.resolve())
    from .rows import _daemon  # noqa: PLC0415
    _daemon.ORACLE.update(python=args.oracle_python, source=args.oracle_source.resolve())
    oracle = core.python_target(args.oracle_python, args.oracle_source.resolve())
    candidate_path = args.candidate or _default_candidate()

    if args.mutants:
        from . import mutants  # noqa: PLC0415
        mutants.SKIP_BANK = args.skip_bank
        return mutants.main(args.row, args.mutant, oracle, args.verbose)

    summary: dict = {"platform": core.PLATFORM, "rows": {}}
    failed = 0
    for row in args.row:
        cases = _select(row, args.case, bank=not (args.skip_bank or args.golden or args.record))
        print(f"{rows.ROWS[row]} ({len(cases)} cases, {core.PLATFORM})", flush=True)
        if args.record:
            print(f"  wrote {record(row, cases, oracle, args.oracle_source.resolve(), args.oracle_commit)}")
            continue
        if not candidate_path.is_file():
            raise SystemExit(f"candidate binary not found: {candidate_path}")
        golden = (json.loads(_golden_path(row).read_text(encoding="utf-8"))
                  if args.golden else None)
        results = run_row(row, cases, oracle, core.rust_target(candidate_path), golden,
                          args.verbose)
        bad = [k for k, v in results.items() if v["status"] != "match"]
        failed += len(bad)
        summary["rows"][rows.ROWS[row]] = {"cases": len(results), "diffs": bad,
                                          "results": results}
        print(f"  {len(results) - len(bad)}/{len(results)} match", flush=True)
    if args.out:
        args.out.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    return 1 if failed else 0
