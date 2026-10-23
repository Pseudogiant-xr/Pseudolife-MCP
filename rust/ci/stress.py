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
  cargo:<package>/<test binary>[::<test>] [cargo:...]
                         Rust integration test binaries, or single tests in
                         them, under nextest with the Rust job's toolchain,
                         lockfile, test profile, default features and 4 test
                         threads: the stress-build job compiles them once
                         into a nextest archive (``archive``), and each
                         iteration runs that archive without compiling. Like
                         every kind, the selection runs alone, not beside
                         the rest of its lane, and only the selected
                         packages are built (the Rust job builds the whole
                         workspace, and also runs --no-default-features)
  <node id> [<node id>]  pytest node ids, run with -ra and a junit report, with
                         the parity shards' candidate binaries in the env

An iteration passes only with evidence: a pytest or cargo run where every
selected test ran and passed (a skip is not evidence, so a binary holding
#[ignore] tests is selected test by test; a Rust test that returns early
still counts as run, so a cargo row lists each test's seconds), or a row or scenario that
compared at least one case. Subcommands: ``plan`` checks the inputs and
lists the iterations, ``archive`` builds a cargo selection's archive,
``run`` runs one iteration and writes its row, ``report`` joins the rows
into stress-report.json and fails unless every iteration passed in the same
run attempt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
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
# A cargo item: package and integration test binary, optionally one test by
# its libtest path (module::test).
CARGO = re.compile(r"cargo:([a-z0-9][a-z0-9_-]{0,63})/([a-z0-9][a-z0-9_-]{0,63})"
                   r"(?:::([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*))?")
CARGO_FORM = "cargo:<package>/<test binary>[::<test>]"
# The Rust workspace; nextest reads rust/.config/nextest.toml from it.
WORKSPACE = Path("rust")


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
    if any(node.startswith("cargo:") for node in nodes):
        if not all(CARGO.fullmatch(node) for node in nodes):
            raise ValueError(f"cargo selection {selection!r}: expected {CARGO_FORM} items, "
                             "separated by spaces and not mixed with other kinds")
        return "cargo", [node[6:] for node in nodes]
    if not nodes or not all(NODE.fullmatch(node) and ".." not in node for node in nodes):
        raise ValueError(f"selection {selection!r}: expected row:<row>[/<case>], scenario:<name>, "
                         f"{CARGO_FORM}, or pytest node ids such as tests/test_x.py::test_y")
    return "pytest", nodes


def cargo_item(name: str) -> tuple[str, str, str | None]:
    """A parsed cargo item's (package, test binary, test or None)."""
    package, _, rest = name.partition("/")
    binary, _, test = rest.partition("::")
    return package, binary, test or None


def filterset(names: list[str], binaries_only: bool = False) -> str:
    """The nextest filterset selecting a cargo selection's tests; with
    binaries_only, the binaries holding them (an archive holds whole binaries)."""
    items = [cargo_item(name) for name in names]
    if binaries_only:
        ids = dict.fromkeys(f"{package}::{binary}" for package, binary, _ in items)
        return " | ".join(f"binary_id(={binary_id})" for binary_id in ids)
    return " | ".join(f"(binary_id(={package}::{binary})" + (f" & test(={test}))" if test else ")")
                      for package, binary, test in items)


def archive_command(names: list[str], archive: Path) -> list[str]:
    """The build of a cargo selection's archive, run from the Rust workspace
    with the Rust job's pinned toolchain, --locked, test profile and default
    features, limited to the selected packages and binaries (so dependency
    features unify over those packages, not the whole workspace)."""
    items = [cargo_item(name) for name in names]
    argv = ["cargo", "nextest", "archive", "--locked"]
    for package in dict.fromkeys(package for package, _, _ in items):
        argv += ["-p", package]
    for binary in dict.fromkeys(binary for _, binary, _ in items):
        argv += ["--test", binary]
    return argv + ["-E", filterset(names, binaries_only=True), "--archive-file", str(archive)]


def workspace_record(archive: Path) -> Path:
    """The file beside an archive naming the checkout path it was built in."""
    return archive.with_name(archive.name + ".workspace")


def cargo_junit(workspace: Path) -> Path:
    """Where the stress nextest profile writes its JUnit report: under the
    (remapped) workspace's target dir, not the archive's extraction dir."""
    return workspace / "target" / "nextest" / "stress" / "junit.xml"


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


def command(kind: str, names: list[str], candidates: Path, scratch: Path,
            archive: Path | None = None) -> list[str]:
    """The argv one iteration runs, from the repository root."""
    if kind == "cargo":
        if archive is None:
            raise ValueError("a cargo selection runs stress-build's archive: pass --archive-file")
        # Extracted over the workspace, the archive's target dir lands where
        # the build job's was. A hosted runner's checkout path is the same in
        # every job of one OS, so the paths the tests compiled in with env!()
        # (CARGO_BIN_EXE_pseudolife-stdio) resolve. --extract-overwrite keeps
        # the candidate binaries already in rust/target/release.
        workspace = str(WORKSPACE.resolve())
        # --test-threads 4 is the Rust job's `cargo nextest run -j 4`.
        return ["cargo-nextest", "nextest", "run", "--archive-file", str(archive),
                "--workspace-remap", workspace, "--extract-to", workspace, "--extract-overwrite",
                "--profile", "stress", "--retries", "0", "--no-fail-fast", "--no-tests", "fail",
                "--test-threads", "4", "-E", filterset(names)]
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
    if kind == "cargo":
        # The Rust job binds the selected interpreter for its Python fixtures
        # and gives nextest no parity candidate.
        return {"PSEUDOLIFE_MCP_PYTHON": sys.executable}
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


def selected_cases(names: list[str], report: Path) -> list | None:
    """The (suite, testcase, matching items) a nextest JUnit report holds for
    a cargo selection, or None without a readable report. Tests outside the
    selection (the rest of a binary when one test is named) are left out."""
    try:
        root = ElementTree.parse(report).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    items = [cargo_item(name) for name in names]
    selected = []
    for suite in root.iter("testsuite"):
        for case in suite.iter("testcase"):
            matched = [name for name, (package, binary, test) in zip(names, items)
                       if suite.get("name") == f"{package}::{binary}" and test in (None, case.get("name"))]
            if matched:
                selected.append((suite.get("name"), case, matched))
    return selected


def cargo_times(names: list[str], report: Path) -> dict:
    """Each selected test's seconds, so a test that returned at once (an
    env-gated child helper) is visible as 0 s rather than hidden in "pass"."""
    return {f"{suite}::{case.get('name')}": float(case.get("time") or 0)
            for suite, case, _ in selected_cases(names, report) or []}


def cargo_evidence(names: list[str], report: Path) -> str:
    """Why a nextest JUnit report shows the cargo selection did not all run
    and pass, or "" when it did."""
    found = selected_cases(names, report)
    if found is None:
        return "no JUnit report from nextest"
    selected, ran = [(case, matched) for _, case, matched in found], dict.fromkeys(names, 0)
    skipped = sum(case.find("skipped") is not None for case, _ in selected)
    if skipped:
        return f"{skipped} of {len(selected)} selected tests skipped; a skip is not evidence"
    # A failure, an error, or a flaky attempt that a retry would have hidden.
    failed = sum(any(child.tag not in ("system-out", "system-err") for child in case) for case, _ in selected)
    if failed:
        return f"{failed} of {len(selected)} selected tests did not pass"
    for _, matched in selected:
        for name in matched:
            ran[name] += 1
    missing = [name for name, count in ran.items() if not count]
    return "no test ran for " + ", ".join(missing) if missing else ""


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
    if kind == "cargo":
        return cargo_evidence(names, scratch / "junit.xml")
    if compared(kind, names, scratch) == 0:
        return "no case was compared (all filtered out on this platform, or no summary)"
    return ""


def runner() -> dict:
    return {"os": os.environ.get("RUNNER_OS", ""), "image": " ".join(
        filter(None, (os.environ.get("ImageOS"), os.environ.get("ImageVersion")))) or "unknown",
            "cpus": os.cpu_count()}


def plan(options) -> int:
    try:
        kind, _ = parse(options.selection)
        count = iterations(options.iterations)
    except ValueError as error:
        print(f"stress: {error}; nothing was started", file=sys.stderr)
        return 2
    with open(options.github_output, "a", encoding="utf-8") as output:
        output.write(f"iterations={json.dumps(list(range(1, count + 1)))}\n")
        output.write(f"kind={kind}\n")
    print(f"stress: {count} iterations of {options.selection.strip()}")
    return 0


def archive(options) -> int:
    kind, names = parse(options.selection)
    if kind != "cargo":
        print(f"stress: archive builds only cargo: selections, not {kind}; nothing was built", file=sys.stderr)
        return 2
    archive_file = Path(options.archive_file).resolve()
    argv = archive_command(names, archive_file)
    print(f"stress: (in {WORKSPACE}) " + " ".join(argv), flush=True)
    code = subprocess.run(argv, cwd=str(WORKSPACE)).returncode
    if code == 0:
        workspace_record(archive_file).write_text(str(WORKSPACE.resolve()), encoding="utf-8")
    return code


def checkout_mismatch(archive_file: Path) -> str:
    """Why an archive cannot run in this checkout, or "" when it can: its
    tests compiled in env!() paths under the build job's checkout."""
    try:
        built = workspace_record(archive_file).read_text(encoding="utf-8").strip()
    except OSError:
        return "the archive has no record of the checkout path it was built in"
    here = str(WORKSPACE.resolve())
    if built != here:
        return (f"the archive was built in checkout path {built}, this runner's is {here}; "
                "its tests' compiled-in paths would not resolve")
    return ""


def run(options) -> int:
    kind, names = parse(options.selection)
    scratch = Path(options.out).parent
    scratch.mkdir(parents=True, exist_ok=True)
    candidates = Path(options.candidates)
    archive_file = Path(options.archive_file).resolve() if options.archive_file else None
    argv = command(kind, names, candidates, scratch, archive_file)
    env = dict(os.environ, **environment(kind, names, candidates, scratch))
    started, code, note, tests = time.monotonic(), None, "", None
    try:
        if kind == "cargo":
            note = checkout_mismatch(archive_file)
            if note:
                code = 1
                print(f"stress: {note}", flush=True)
                return code
            # Only this iteration's report may count.
            cargo_junit(WORKSPACE).unlink(missing_ok=True)
        print("stress: " + " ".join(argv), flush=True)
        code = subprocess.run(argv, env=env).returncode
        if kind == "cargo" and cargo_junit(WORKSPACE).is_file():
            # The row's artifact keeps the report; evidence reads this copy.
            (scratch / "junit.xml").write_bytes(cargo_junit(WORKSPACE).read_bytes())
            tests = cargo_times(names, scratch / "junit.xml")
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
               **({"note": note} if note else {}), **({"tests": tests} if tests is not None else {})}
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
    r.add_argument("--archive-file", help="a cargo selection's nextest archive, from stress-build")
    r.add_argument("--out", required=True)
    a = sub.add_parser("archive")
    a.add_argument("--selection", required=True)
    a.add_argument("--archive-file", required=True)
    s = sub.add_parser("report")
    s.add_argument("--selection", required=True)
    s.add_argument("--iterations", required=True)
    s.add_argument("--os", required=True)
    s.add_argument("--rows", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--summary")
    options = parser.parse_args(argv)
    return {"plan": plan, "archive": archive, "run": run, "report": report}[options.action](options)


if __name__ == "__main__":
    sys.exit(main())
