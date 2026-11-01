"""Publish full live body differentials, including failed and missing evidence.

Both daemons use this checkout: the harness starts the Python oracle from
its own repository and CI builds the Rust binary here. A branch dispatch
exercises the lane; only a scheduled master run supplies D6/D11 evidence.
Raw harness responses and daemon logs stay private to the job. The artifact
contains the comparators' differences and counts, not credentials or homes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "rust/daemon/harness"
SUITES = {"w2e": "w2e.py", "w2d_search": "w2d_search.py", "board": "board/run.py"}


def summarize(family: str, report: dict, exit_code: int) -> dict:
    rows = report.get("scenarios" if family == "w2e" else "results", [])
    planned = report.get("planned_scenarios", [])
    scenarios = []
    for row in rows:
        cases = row.get("cases" if family == "w2d_search" else "steps", [])
        differences = [{"case": str(case.get("case", case.get("step", index))), "detail": diff}
                       for index, case in enumerate(cases) for diff in case["diffs"]]
        for key in ("db_diffs", "state_diffs", "chain"):
            differences.extend({"case": key, "detail": diff} for diff in row.get(key, []))
        mixed = family == "board" and row["scenario"] in (
            "mixed-chain (python first)", "mixed-chain (rust first)")
        valid = bool(cases) or (mixed and "chain" in row and "state_diffs" in row)
        scenarios.append({"name": row["scenario"], "cases": len(cases),
                          "different_cases": sum(bool(case["diffs"]) for case in cases),
                          "differences": differences,
                          "status": "pass" if valid and not differences and not row.get("skipped") else "fail"})
    complete = report.get("complete") is True and planned == [row["name"] for row in scenarios]
    return {"name": family, "exit_code": exit_code,
            "not_run": [name for name in planned if name not in {r["name"] for r in scenarios}],
            "status": "pass" if complete and exit_code == 0 and scenarios and all(
                row["status"] == "pass" for row in scenarios) else "fail",
            "counts": {"scenarios": len(scenarios), "cases": sum(r["cases"] for r in scenarios),
                       "different_cases": sum(r["different_cases"] for r in scenarios),
                       "differences": sum(len(r["differences"]) for r in scenarios)},
            "scenarios": scenarios}


def save(out: Path, report: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "evidence.json").write_text(json.dumps(public_value(report, out), indent=2) + "\n", encoding="utf-8")


def public_value(value, out: Path):
    """Keep comparator details without publishing machine paths or logins."""
    if isinstance(value, dict):
        return {key: public_value(item, out) for key, item in value.items()}
    if isinstance(value, list):
        return [public_value(item, out) for item in value]
    if not isinstance(value, str):
        return value
    for path, label in ((ROOT, "<checkout>"), (out.parent, "<job-temp>"), (Path.home(), "<home>")):
        value = value.replace(str(path), label).replace(path.as_posix(), label)
    value = re.sub(r"postgres(?:ql)?://[^\s\"']+", "<database-url>", value)
    value = re.sub(r'(?i)("(?:password|token|api_key|authorization)"\s*:\s*)"[^"]*"',
                   r'\1"<redacted>"', value)
    return value


def plan(out: Path) -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"],
                                    cwd=ROOT, text=True).strip()
    if dirty:
        raise ValueError("body evidence requires a committed checkout")
    expected = os.environ.get("GITHUB_SHA")
    if expected and expected != head:
        raise ValueError("checkout HEAD does not match this run's GITHUB_SHA")
    event, ref = os.environ.get("GITHUB_EVENT_NAME", "local"), os.environ.get("GITHUB_REF", "")
    if event == "schedule" and ref != "refs/heads/master":
        raise ValueError("scheduled evidence must compare master")
    report = {"schema_version": 1, "rust_head": head, "python_head": head,
              "event": event, "ref": ref, "run_id": os.environ.get("GITHUB_RUN_ID"),
              "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
              "scheduled_master": event == "schedule" and ref == "refs/heads/master",
              "status": "incomplete", "suites": []}
    save(out, report)
    print(f"Body evidence plan: {', '.join(SUITES)}; Rust {head}; Python {head}", flush=True)
    return report


def run(binary: Path, out: Path, manifest: dict) -> dict:
    with binary.open("rb") as stream:
        binary_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {**manifest, "status": "incomplete", "suites": [], "binary_sha256": binary_digest}
    save(out, result)
    # Scratch is outside the published directory. Never upload a whole
    # harness home: it contains disposable login material and raw responses.
    scratch = out.parent / "body-evidence-private"
    scratch.mkdir(parents=True, exist_ok=True)
    for family, script in SUITES.items():
        raw = scratch / f"{family}.json"
        raw.unlink(missing_ok=True)
        command = [sys.executable, str(HARNESS / script), "live", "--rust-bin", str(binary),
                   "--out", str(raw)]
        env = dict(os.environ, PYTHONUNBUFFERED="1", PL_HARNESS_SCRATCH=str(scratch / family))
        print(f"Starting full {family} live differential", flush=True)
        with (scratch / f"{family}.log").open("w", encoding="utf-8") as log:
            completed = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            row = summarize(family, json.loads(raw.read_text(encoding="utf-8")), completed.returncode)
        except (OSError, ValueError, KeyError, TypeError):
            row = summarize(family, {}, completed.returncode)
            row["error"] = "Harness did not produce a complete readable report."
        result["suites"].append(row)
        save(out, result)
        print(f"{family}: {row['status']}, exit {completed.returncode}, {row['counts']}", flush=True)
    result["status"] = "pass" if all(r["status"] == "pass" for r in result["suites"]) else "fail"
    save(out, result)
    return result


def summary(report: dict) -> str:
    lines = ["## Rust daemon body differentials", "",
             f"Rust: `{report['rust_head']}`; Python oracle: `{report['python_head']}`.",
             f"Result: **{report['status']}**. Scheduled master: {report['scheduled_master']}.", "",
             "| Harness | Scenarios | Cases | Differences | Result |",
             "|---|---:|---:|---:|---|"]
    rows = {row["name"]: row for row in report["suites"]}
    for name in SUITES:
        row = rows.get(name)
        if row:
            counts = row["counts"]
            lines.append(f"| {name} | {counts['scenarios']} | {counts['cases']} | "
                         f"{counts['differences']} | {row['status']} (exit {row['exit_code']}) |")
        else:
            lines.append(f"| {name} | — | — | — | not run |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "run", "summary"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rust-bin", type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    if args.mode == "plan":
        plan(out)
        return 0
    report = json.loads((out / "evidence.json").read_text(encoding="utf-8"))
    if args.mode == "run":
        if not args.rust_bin:
            parser.error("run requires --rust-bin")
        report = run(args.rust_bin.resolve(), out, report)
        return 0 if report["status"] == "pass" else 1
    text = summary(report)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
