"""Run strict MCP parity and controls, retaining the named refusal divergence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from mcp_compare import UNSERVED
from mcp_run import mutant_caught
from mcp_validation_sweep import EXPECTED_COERCION_CASES

HERE = Path(__file__).resolve().parent
MUTANTS = ("mcp-tier-flip", "mcp-toolset-floor", "mcp-sse-no-session", "mcp-no-hint")


def check_baseline(report):
    """Accept exactly MCP-REFUSAL-CLEANUP; the strict report stays red."""
    summary = report["summary"]
    assert (summary["responses"], summary["cases"], summary["diffs"]) == (331, 17, 5)
    assert set(summary["unserved_checked"]) == set(UNSERVED)
    expected = {f"body_limit[{index}]" for index in (0, 2, 3, 4, 5)}
    seen = set()
    for diff in report["diffs"]:
        name, pair = diff.split(": python=", 1)
        py, rs = pair.split("\n          rust  =", 1)
        py, rs = json.loads(py), json.loads(rs)
        assert name in expected and name not in seen
        seen.add(name)
        assert py == {"status": 413, "headers": {"mcp-session-id": None},
                      "body": {"raw": "Request body too large"}}
        assert rs["headers"].pop("connection") == "close"
        assert rs == py
    assert seen == expected


def check_coercion_report(report):
    assert report['mode'] == 'coercions'
    assert report['generated_cases'] == EXPECTED_COERCION_CASES
    assert report['expected_observations'] == report['completed_observations'] == 2 * EXPECTED_COERCION_CASES
    assert not report['diffs']
    assert report['source']['pydantic-core'] == '2.50.0'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--default-bin", type=Path, required=True)
    parser.add_argument("--mutant-bin", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    results = {}

    def run(label, binary, flags, expected):
        target = out / label
        report_name = (f"mutant-{flags[flags.index('--mutant') + 1]}.json"
                       if "--mutant" in flags else "compare.json")
        (target / report_name).unlink(missing_ok=True)
        if "--validation-coercions" in flags:
            (target / "validation-sweep.json").unlink(missing_ok=True)
        if "--mount-probe" in flags:
            (target / "foundation-mount.json").unlink(missing_ok=True)
        command = [sys.executable, str(HERE / "mcp_run.py"), str(binary.resolve()), str(target), *flags]
        with (out / f"{label}.log").open("w", encoding="utf-8") as log:
            code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
        results[label] = code
        print(f"{label}: exit {code}", flush=True)
        assert code == expected, f"{label}: expected exit {expected}, got {code}; see {out / (label + '.log')}"
        return target

    baseline_dir = run("baseline", args.default_bin, ["--mount-probe", "--validation-coercions"], 1)
    baseline_file = baseline_dir / "compare.json"
    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    check_baseline(baseline)
    check_coercion_report(json.loads((baseline_dir / 'validation-sweep.json').read_text(encoding='utf-8')))
    print(f'SDK coercion gate: {2 * EXPECTED_COERCION_CASES} observations, zero differences', flush=True)
    mount = json.loads((baseline_dir / "foundation-mount.json").read_text(encoding="utf-8"))
    assert mount["case"] == "mcp path, auth" and not mount["diffs"]
    print("strict baseline: five declared 413 close-header differences; foundation mount matches", flush=True)
    for mutant in MUTANTS:
        target = run(mutant, args.mutant_bin, ["--mutant", mutant, "--baseline", str(baseline_file)], 0)
        report = json.loads((target / f"mutant-{mutant}.json").read_text(encoding="utf-8"))
        assert mutant_caught(1, report, baseline), mutant
    # A production binary must not be credited for the baseline's five diffs.
    target = run("without-mutants", args.default_bin,
                 ["--mutant", "mcp-no-hint", "--baseline", str(baseline_file)], 1)
    report = json.loads((target / "mutant-mcp-no-hint.json").read_text(encoding="utf-8"))
    check_baseline(report)
    assert not mutant_caught(1, report, baseline)
    results["seconds"] = round(time.monotonic() - started, 1)
    (out / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
