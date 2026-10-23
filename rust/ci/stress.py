"""Run one named selection many times on fresh hosted runners (rust.yml mode=stress).

A new live parity row, or a test whose verdict depends on the timing of a
spawned process or socket, joins a required lane only after 20 clean hosted
windows-latest runs (CLAUDE.md), because the Windows dev VM reproduces the
hosted timing classes at a fraction of their hosted rate: #835 lost 0 of 525
calls on the VM, then failed 3 of 5 hosted runs. Each iteration is its own
job on a fresh runner, so first-hit cold-start stalls count.

A selection is one of:
  row:<row>[/<case>]     one live arm of a CLI harness row against this
                         checkout's Python, run as its required lane runs it
  scenario:<name>        one live daemon comparison (run.py live --only), on
                         the binary and settings the scenario's lane uses
  <node id> [<node id>]  pytest node ids, run with -ra and a junit report, with
                         the parity shards' candidate binaries in the env

An iteration passes only with evidence: a pytest run where every selected
test ran (a skip is not evidence), or a row or scenario that compared at
least one case. Subcommands: ``plan`` checks the inputs and lists the
iterations, ``run`` runs one iteration and writes its row, ``report`` joins
the rows into stress-report.json and fails unless every iteration passed in
the same run attempt.
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
# The rows whose required live arm is cli-live's "CLI differential harness
# live arms" step, which skips the bank cases; every other row's live arm
# (the cli lease steps, W1-C, H5 audit and backup) runs them, without the
# embedder fixture.
SKIP_BANK_ROWS = {"mail", "hook", "episode"}
# Scenarios the cli-live steps run on the default (static) build, under the
# http slice, without the embedder fixture; every other scenario runs as
# the H5 shards run it (rust/ci/run_coverage.py), on the mutants-feature
# build with --fixture.
STATIC_PREFIXES = ("body-", "security-", "static-")
# These need the debug build's loopback-bind override, which only the rust
# job builds, and a stress loop runs no rust job.
REMOTE_BIND = {"trust-bind", "security-remote-open", "security-remote-auth"}


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
        if name in REMOTE_BIND:
            raise ValueError(f"scenario:{name} needs the debug loopback-bind build, which a stress loop "
                             "does not build; dispatch manual CI (mode=ci) for it, or pick another scenario:")
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


def binary(candidates: Path, name: str) -> Path:
    return candidates / (name + (".exe" if os.name == "nt" else ""))


def daemon(name: str, candidates: Path) -> tuple[Path, bool]:
    """The daemon binary a scenario's required lane uses, and whether it
    passes the offline embedder fixture."""
    if name.startswith(STATIC_PREFIXES):
        return binary(candidates, "pseudolife-daemon-static"), False
    return binary(candidates, "pseudolife-daemon"), True


def command(kind: str, names: list[str], candidates: Path, scratch: Path) -> list[str]:
    """The argv one iteration runs, from the repository root."""
    if kind == "row":
        argv = [sys.executable, "rust/cli_harness", "--row", names[0]]
        if len(names) > 1:
            argv += ["--case", names[1]]
        if names[0] in SKIP_BANK_ROWS:
            argv += ["--skip-bank"]
        return argv + ["--candidate", str(binary(candidates, "pseudolife-stdio")), "-v",
                       "--out", str(scratch / "harness.json")]
    if kind == "scenario":
        path, fixture = daemon(names[0], candidates)
        return [sys.executable, "rust/daemon/harness/run.py", "live", "--rust-bin", str(path),
                *(["--fixture"] if fixture else []), "--only", names[0],
                "--out", str(scratch / "harness.json")]
    return [sys.executable, "-m", "pytest", *names, "-ra", "--durations=30",
            "--junitxml", str(scratch / "junit.xml")]


def environment(kind: str, names: list[str], candidates: Path, scratch: Path) -> dict:
    """The env the selection's lane step sets, on top of the job's."""
    env = {"PSEUDOLIFE_RUST_BINARY": str(binary(candidates, "pseudolife-stdio"))}
    if kind == "scenario":
        env["PL_HARNESS_SCRATCH"] = str(scratch / "harness-scratch")
        if names[0].startswith(STATIC_PREFIXES):
            env["PL_HARNESS_SLICE"] = "http"
    if kind == "pytest":
        # The variables the parity steps give the harness self-tests, so a
        # test that needs a candidate runs instead of skipping.
        env.update({
            "PSEUDOLIFE_BACKUP_NATIVE_REQUIRED": "1",
            "PSEUDOLIFE_SENT_YAML_BIN": str(binary(candidates, "pseudolife-stdio")),
            "PL_STATIC_DEFAULT_BIN": str(binary(candidates, "pseudolife-daemon-static")),
            "PL_SECURITY_DEFAULT_BIN": str(binary(candidates, "pseudolife-daemon-static")),
            "PL_SECURITY_RELEASE_MUTANTS_BIN": str(binary(candidates, "pseudolife-daemon")),
            "PL_DREAM_CONTRACT_BINARY": str(binary(candidates, "dream-contract")),
            # The eval shard's episode executable nodes: a JSON list of the
            # absolute stdio candidate, as its "Run every eval harness test"
            # step exports it.
            "PSEUDOLIFE_PORT_EPISODE_JSON": json.dumps([str(binary(candidates, "pseudolife-stdio").resolve())]),
        })
    return env


def junit_counts(report: Path) -> tuple[int, int]:
    """The (tests, skipped) totals of pytest's junit report; (0, 0) if absent."""
    try:
        text = report.read_text(encoding="utf-8")
    except OSError:
        return 0, 0
    total = skipped = 0
    for tag in re.findall(r"<testsuite\b[^>]*>", text):
        total += int((re.search(r'\btests="(\d+)"', tag) or [0, 0])[1])
        skipped += int((re.search(r'\bskipped="(\d+)"', tag) or [0, 0])[1])
    return total, skipped


def compared(kind: str, names: list[str], scratch: Path) -> int:
    """How many cases a row or scenario run compared, from its summary."""
    try:
        summary = json.loads((scratch / "harness.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if kind == "row":
        return int(summary.get("rows", {}).get(names[0], {}).get("cases", 0)) if summary.get("complete") else 0
    return sum(len(item.get("cases", [])) for item in summary.get("scenarios", [])) \
        + len(summary.get("refusals", []))


def evidence(kind: str, names: list[str], scratch: Path) -> str:
    """Why a run that exited 0 is still no evidence, or "" when it is."""
    if kind == "pytest":
        total, skipped = junit_counts(scratch / "junit.xml")
        if total == 0:
            return "no selected test was collected"
        if skipped:
            return f"{skipped} of {total} selected tests skipped; a skip is not evidence"
        return ""
    if compared(kind, names, scratch) == 0:
        return "no case was compared (all filtered out on this platform, or no summary)"
    return ""


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
    candidates = Path(options.candidates)
    argv = command(kind, names, candidates, scratch)
    env = dict(os.environ, **environment(kind, names, candidates, scratch))
    started, code, note = time.monotonic(), None, ""
    try:
        print("stress: " + " ".join(argv), flush=True)
        code = subprocess.run(argv, env=env).returncode
        if code == 0:
            note = evidence(kind, names, scratch)
            if note:
                code = 1
                print(f"stress: {note}", flush=True)
    finally:
        row = {"iteration": options.iteration, "attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT") or 1),
               "selection": options.selection.strip(), "runner": runner(), "exit_code": code,
               "outcome": "pass" if code == 0 else "fail",
               "seconds": round(time.monotonic() - started, 1),
               **({"note": note} if note else {})}
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
    # "Re-run failed jobs" replaces a failed iteration's row with the retry's:
    # rows from different attempts hide a failure, so they never count.
    attempts = sorted({row["attempt"] for row in table if "attempt" in row})
    mixed = len(attempts) > 1
    summary = {"selection": options.selection.strip(), "os": options.os, "iterations": count,
               "passed": passed, "failed": sum(row["outcome"] == "fail" for row in table),
               "no_result": sum(row["outcome"] == "no result" for row in table),
               "attempts": attempts, "clean": passed == count and not mixed, "rows": table}
    Path(options.out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    lines = [f"### Stress: {summary['selection']} on {options.os}",
             f"{passed} of {count} iterations passed."]
    if mixed:
        lines.append(f"Not clean: rows come from run attempts {attempts}; a re-run of failed jobs "
                     "replaced at least one failure. Dispatch a new loop.")
    lines += ["", "| Iteration | Attempt | Outcome | Seconds | Image | CPUs | Note |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {row['iteration']} | {row.get('attempt', '')} | {row['outcome']} "
              f"| {row['seconds'] if row['seconds'] is not None else ''} "
              f"| {row['runner'].get('image', '')} | {row['runner'].get('cpus', '')} | {row.get('note', '')} |"
              for row in table]
    text = "\n".join(lines) + "\n"
    print(text)
    if options.summary:
        with open(options.summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 0 if summary["clean"] else 1


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
