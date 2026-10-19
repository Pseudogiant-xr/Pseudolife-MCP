"""Run one named selection many times on fresh hosted runners (rust.yml mode=stress).

A new live parity row, or a test whose verdict depends on the timing of a
spawned process or socket, joins a required lane only after 20 clean hosted
windows-latest runs (CLAUDE.md), because the Windows dev VM reproduces the
hosted timing classes at a fraction of their hosted rate: #835 lost 0 of 525
calls on the VM, then failed 3 of 5 hosted runs. Each iteration is its own
job on a fresh runner, so first-hit cold-start stalls count.

A selection is one of:
  row:<row>[/<case>]     one live arm of a CLI harness row against the oracle
  scenario:<name>        one live daemon comparison (run.py live --only)
  <node id> [<node id>]  pytest node ids, run with -ra and a junit report

Subcommands: ``plan`` checks the inputs and lists the iterations, ``run`` runs
one iteration and writes its row, ``report`` joins the rows into
stress-report.json and fails unless every iteration passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MAX_ITERATIONS = 50
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# A pytest node id: a test path under the tree, optionally ::test[param].
NODE = re.compile(r"[A-Za-z0-9_./-]+\.py(::[A-Za-z0-9_\[\]=.,:/-]+)?")


def parse(selection: str) -> tuple[str, list[str]]:
    """The selection's kind and its names, or ValueError naming the fix."""
    selection = selection.strip()
    if selection.startswith("row:"):
        row, slash, case = selection[4:].partition("/")
        if not NAME.fullmatch(row) or (slash and not NAME.fullmatch(case)):
            raise ValueError(f"row selection {selection!r}: expected row:<row> or row:<row>/<case>")
        return "row", [row] + ([case] if case else [])
    if selection.startswith("scenario:"):
        name = selection[9:]
        if not NAME.fullmatch(name):
            raise ValueError(f"scenario selection {selection!r}: expected scenario:<run.py scenario>")
        return "scenario", [name]
    nodes = selection.split()
    if not nodes or not all(NODE.fullmatch(node) and ".." not in node for node in nodes):
        raise ValueError(f"selection {selection!r}: expected row:<row>[/<case>], scenario:<name>, "
                         "or pytest node ids such as tests/test_x.py::test_y")
    return "pytest", nodes


def iterations(value: str) -> int:
    count = int(value)
    if not 1 <= count <= MAX_ITERATIONS:
        raise ValueError(f"iterations={count}: choose 1 to {MAX_ITERATIONS}")
    return count


def command(kind: str, names: list[str], candidates: Path, scratch: Path) -> list[str]:
    """The argv one iteration runs, from the repository root."""
    suffix = ".exe" if os.name == "nt" else ""
    if kind == "row":
        argv = [sys.executable, "rust/cli_harness", "--row", names[0]]
        if len(names) > 1:
            argv += ["--case", names[1]]
        return argv + ["--candidate", str(candidates / f"pseudolife-stdio{suffix}"), "-v",
                       "--out", str(scratch / "harness.json")]
    if kind == "scenario":
        return [sys.executable, "rust/daemon/harness/run.py", "live",
                "--rust-bin", str(candidates / f"pseudolife-daemon-static{suffix}"),
                "--only", names[0], "--out", str(scratch / "harness.json")]
    return [sys.executable, "-m", "pytest", *names, "-ra", "--durations=30",
            "--junitxml", str(scratch / "junit.xml")]


def runner() -> dict:
    return {"os": os.environ.get("RUNNER_OS", ""), "image": " ".join(
        filter(None, (os.environ.get("ImageOS"), os.environ.get("ImageVersion")))) or "unknown",
            "cpus": os.cpu_count()}


def plan(options) -> int:
    try:
        parse(options.selection)
        count = iterations(options.iterations)
    except ValueError as error:
        print(f"stress: {error}; nothing was started", file=sys.stderr)
        return 2
    with open(options.github_output, "a", encoding="utf-8") as output:
        output.write(f"iterations={json.dumps(list(range(1, count + 1)))}\n")
    print(f"stress: {count} iterations of {options.selection.strip()}")
    return 0


def run(options) -> int:
    kind, names = parse(options.selection)
    scratch = Path(options.out).parent
    scratch.mkdir(parents=True, exist_ok=True)
    argv = command(kind, names, Path(options.candidates), scratch)
    # The harness self-tests' stdio candidate, as the parity shards set it.
    stdio = Path(options.candidates) / ("pseudolife-stdio.exe" if os.name == "nt" else "pseudolife-stdio")
    env = dict(os.environ, PSEUDOLIFE_RUST_BINARY=str(stdio))
    started, code = time.monotonic(), None
    try:
        print("stress: " + " ".join(argv), flush=True)
        code = subprocess.run(argv, env=env).returncode
    finally:
        row = {"iteration": options.iteration, "selection": options.selection.strip(),
               "runner": runner(), "exit_code": code,
               "outcome": "pass" if code == 0 else "fail",
               "seconds": round(time.monotonic() - started, 1)}
        Path(options.out).write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    return code


def report(options) -> int:
    count = iterations(options.iterations)
    rows = {}
    for path in sorted(Path(options.rows).glob("**/stress-row-*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        rows[int(row["iteration"])] = row
    table = [rows.get(i, {"iteration": i, "outcome": "no result", "exit_code": None, "seconds": None,
                          "runner": {}}) for i in range(1, count + 1)]
    passed = sum(row["outcome"] == "pass" for row in table)
    summary = {"selection": options.selection.strip(), "os": options.os, "iterations": count,
               "passed": passed, "failed": sum(row["outcome"] == "fail" for row in table),
               "no_result": sum(row["outcome"] == "no result" for row in table), "rows": table}
    Path(options.out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    lines = [f"### Stress: {summary['selection']} on {options.os}",
             f"{passed} of {count} iterations passed.", "",
             "| Iteration | Outcome | Seconds | Image | CPUs |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {row['iteration']} | {row['outcome']} | {row['seconds'] if row['seconds'] is not None else ''} "
              f"| {row['runner'].get('image', '')} | {row['runner'].get('cpus', '')} |" for row in table]
    text = "\n".join(lines) + "\n"
    print(text)
    if options.summary:
        with open(options.summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 0 if passed == count else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--selection", required=True)
    p.add_argument("--iterations", required=True)
    p.add_argument("--github-output", required=True)
    r = sub.add_parser("run")
    r.add_argument("--selection", required=True)
    r.add_argument("--iteration", required=True, type=int)
    r.add_argument("--candidates", required=True)
    r.add_argument("--out", required=True)
    s = sub.add_parser("report")
    s.add_argument("--selection", required=True)
    s.add_argument("--iterations", required=True)
    s.add_argument("--os", required=True)
    s.add_argument("--rows", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--summary")
    options = parser.parse_args(argv)
    return {"plan": plan, "run": run, "report": report}[options.action](options)


if __name__ == "__main__":
    sys.exit(main())
