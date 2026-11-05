"""Run one named selection many times on fresh hosted runners (rust.yml mode=stress).

A new live parity row, or a test whose verdict depends on the timing of a
spawned process or socket, joins a required lane only after 20 clean hosted
windows-latest runs (CLAUDE.md), because the Windows dev VM reproduces the
hosted timing classes at a fraction of their hosted rate: #835 lost 0 of 525
calls on the VM, then failed 3 of 5 hosted runs. Each iteration is its own
job on a fresh runner, so first-hit cold-start stalls count.

A selection is one of:
  row:<row>[/<case>] [row:...]
                         live arms of CLI harness rows against this
                         checkout's Python, each run as its required lane runs it
  scenario:<name> [scenario:...]
                         live daemon comparisons (run.py live --only), each on
                         the binary and settings the scenario's lane uses
  cargo:<package>/<test binary>[::<test>] [cargo:...]
                         Rust integration test binaries, lib for library unit
                         tests, or bin/<name> for binary unit tests, optionally
                         one exact test, under nextest with the Rust job's toolchain,
                         lockfile, test profile, default features and 4 test
                         threads: the stress-build job compiles them once
                         into a nextest archive (``archive``), and each
                         iteration runs that archive without compiling. Like
                         every kind, the selection runs alone, not beside
                         the rest of its lane, and only the selected
                         packages are built (the Rust job builds the whole
                         workspace, and also runs --no-default-features).
                         lib is reserved: an integration test target named lib
                         uses test/lib; a binary target named lib is unsupported
                         (bin/lib is refused). Exact tests that need a
                         disposable PostgreSQL match a CARGO_DATABASE_PROFILES
                         entry: the iteration gets the checkout install and the
                         job's PostgreSQL (plan's postgres=true), a fresh
                         database per iteration (dropped afterwards, also on
                         failure), the profile's flags and fixtures, and runs
                         them with 1 test thread, since they share that database
                         and nextest's process per test defeats their in-process
                         lock. plan refuses an exact db_ test no profile covers
                         and a selection mixing profiles (no profile counts as
                         one). A whole-binary item names no exact test, so it
                         gets no database: its db_ tests return early and count
                         as run, which is why such tests are selected by name.
  <node id> [<node id>]  pytest node ids, run with -ra and a junit report, with
                         the parity shards' candidate binaries in the env

An iteration passes only with evidence: a pytest or cargo run where every
selected test ran and passed (a runtime skip is not evidence; nextest's
default-excluded #[ignore] tests are not selected; a Rust test that returns early
still counts as run, so a cargo row lists each test's seconds, and a database
profile sets its tests' REQUIRED flag so a missing input panics instead), or a
row or scenario that compared at least one case.

Several rows, or several scenarios, share one loop: every iteration runs
each item in turn as its own harness process, with the argv, env and
scratch directory it would get alone, so items never share a setup and N
items cost one set of runner jobs instead of N. The iteration passes only
if every item passed and compared a case; its row lists each item's
result (an item that raises is that item's failure, and the rest still
run) and is rewritten before each item, so a timed-out job names the item
it was running. One iteration provides every row and every scenario (the
job downloads the stdio, default static and mutants daemon builds and
prepares PostgreSQL for both kinds) except REMOTE_BIND's, so ``plan``
refuses a selection naming one of those, a mix of kinds, overlapping
items (a row twice, or a whole row beside one of its cases), or more than
MAX_ITEMS items, which would not fit one job's time budget.

Subcommands: ``plan`` checks the inputs and
lists the iterations, ``archive`` builds a cargo selection's archive,
``run`` runs one iteration and writes its row, ``report`` joins the rows
into stress-report.json and fails unless every iteration passed, allowing
one verified setup-only retry per iteration when job history is supplied.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
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
# The kinds whose items each run as their own harness process.
ITEM_KINDS = ("row", "scenario")
# The items of one row or scenario selection run one after another inside one
# stress job (rust.yml's timeout-minutes, ITERATION_MINUTES). Review of the
# multi-item change (2026-11-02): a row job spends about 4-5 minutes on setup,
# then 2-3 minutes per row, so 6 items leave room inside the budget.
MAX_ITEMS = 6
ITERATION_MINUTES = 45
# A cargo item: package and test binary, optionally one test by its libtest
# path (module::test). lib selects library unit tests; test/lib escapes the
# reserved name for an integration target. bin/<name> selects binary unit
# tests, except the reserved bin/lib spelling.
CARGO = re.compile(r"cargo:([a-z0-9][a-z0-9_-]{0,63})/(test/lib|(?:bin/)?[a-z0-9][a-z0-9_-]{0,63})"
                   r"(?:::([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*))?")
CARGO_FORM = "cargo:<package>/[bin/]<test binary>[::<test>]"
# The Rust workspace; nextest reads rust/.config/nextest.toml from it.
WORKSPACE = Path("rust")
HARNESS = Path(__file__).resolve().parents[1] / "daemon" / "harness"
# Cargo tests that need a disposable PostgreSQL, by the exact test's libtest
# path: an item whose test is in `target` and starts with one of `prefixes`
# gets, every iteration, a fresh database of the pgdisposable `slice`
# (pl_cf_<slice>_..., on the job's lease_ci.py cluster) in `dsn_env`, the
# `env` flags, the embedding fixture model at `dimension` in the fixture's
# `env` variable when `fixture` is set, and the onnxruntime wheel's library
# in `ort_env` when set. Adding one is one entry here.
CARGO_DATABASE_PROFILES = {
    # PR #1332's read-only shadow refresh tests, as startup_ci.py's
    # shadow_refresh runs them; REQUIRED makes a missing input panic.
    "shadow-refresh": {
        "target": "pseudolife-daemon/bin/pseudolife-daemon",
        "prefixes": ("shadow::tests::db_", "service::shadow_refresh_tests::db_"),
        "slice": "pgs",
        "dsn_env": "PL_SHADOW_REFRESH_DSN",
        "env": {"PL_SHADOW_REFRESH_REQUIRED": "1"},
        "fixture": {"env": "PL_SHADOW_EMBED_FIXTURE", "dimension": 1024},
        "ort_env": "ORT_DYLIB_PATH",
    },
}


def token_kind(token: str) -> str:
    """The kind one space-separated selection item names."""
    prefix, colon, _ = token.partition(":")
    return prefix if colon and prefix in ("row", "scenario", "cargo") else "pytest"


def parse(selection: str) -> tuple[str, list[str]]:
    """The selection's kind and its names, or ValueError naming the fix.
    Rows and scenarios are named per item ("hook/<case>", "tokenless");
    item_names splits one into the harness's names."""
    selection = selection.strip()
    tokens = selection.split()
    if any(token_kind(token) in ITEM_KINDS for token in tokens):
        kind = token_kind(tokens[0])
        stray = next((token for token in tokens if token_kind(token) != kind), None)
        if stray is not None:
            first = kind if kind in ITEM_KINDS else token_kind(stray)
            raise ValueError(f"{first} selection {selection!r}: item {stray!r} is a {token_kind(stray)} item; "
                             f"a selection holds one kind, so dispatch the {token_kind(stray)} item separately")
        seen = {}
        for token in tokens:
            where = "" if len(tokens) == 1 else f": item {token!r}"
            name = token[len(kind) + 1:]
            if kind == "row":
                row, slash, case = name.partition("/")
                if not NAME.fullmatch(row) or (slash and not NAME.fullmatch(case)):
                    raise ValueError(f"row selection {selection!r}{where}: expected row:<row> or row:<row>/<case>"
                                     + (", separated by spaces" if where else ""))
            else:
                if not NAME.fullmatch(name):
                    raise ValueError(f"scenario selection {selection!r}{where}: expected scenario:<run.py scenario>"
                                     + (", separated by spaces" if where else ""))
                if name in REMOTE_BIND:
                    raise ValueError(f"scenario:{name} needs the debug loopback-bind build, which a stress loop "
                                     "does not build; dispatch manual CI (mode=ci) for it, or pick another scenario:")
            # Identity is (row, case) or (scenario,): a whole row already runs
            # every case of it, so a row and one of its cases overlap.
            identity = tuple(item_names(kind, name))
            for other, earlier in seen.items():
                if other == identity:
                    raise ValueError(f"{kind} selection {selection!r}: {token!r} is named twice; "
                                     f"name each {kind} once")
                if other[0] == identity[0] and len(other) != len(identity):
                    raise ValueError(f"{kind} selection {selection!r}: item {token!r} overlaps {earlier!r}, "
                                     "since a whole row runs every case of it; name the row or its cases, not both")
            seen[identity] = token
        if len(tokens) > MAX_ITEMS:
            raise ValueError(f"{kind} selection {selection!r} names {len(tokens)} items; a loop runs at most "
                             f"{MAX_ITEMS}, one after another in one {ITERATION_MINUTES}-minute job, so split "
                             "the dispatch into several of at most that many")
        return kind, [token[len(kind) + 1:] for token in tokens]
    nodes = tokens
    if any(node.startswith("cargo:") for node in nodes):
        if not all(CARGO.fullmatch(node) for node in nodes):
            raise ValueError(f"cargo selection {selection!r}: expected {CARGO_FORM} items, "
                             "or cargo:<package>/lib[::<test>] (test/lib for an integration target named lib), "
                             "separated by spaces and not mixed with other kinds")
        if any(cargo_item(node[6:])[1] == "bin/lib" for node in nodes):
            raise ValueError("lib is reserved for library unit tests: use cargo:<package>/lib[::<test>]; "
                             "a binary target named lib cannot be selected with bin/lib")
        database_profile([node[6:] for node in nodes])
        return "cargo", [node[6:] for node in nodes]
    if not nodes or not all(NODE.fullmatch(node) and ".." not in node for node in nodes):
        raise ValueError(f"selection {selection!r}: expected row:<row>[/<case>], scenario:<name>, "
                         f"{CARGO_FORM}, or pytest node ids such as tests/test_x.py::test_y")
    return "pytest", nodes


def item_names(kind: str, item: str) -> list[str]:
    """One row or scenario item's harness names: [row] or [row, case], or [scenario]."""
    if kind == "row":
        row, _, case = item.partition("/")
        return [row] + ([case] if case else [])
    return [item]


def cargo_item(name: str) -> tuple[str, str, str | None]:
    """A parsed cargo item's (package, target selector, exact test or None)."""
    package, _, rest = name.partition("/")
    binary, _, test = rest.partition("::")
    return package, binary, test or None


def database_profile(names: list[str]) -> str | None:
    """The CARGO_DATABASE_PROFILES entry a cargo selection's tests need, or
    None; ValueError naming the fix for a db_ test no profile covers (it
    would run without its database, return early and count as run) or for
    items of different profiles, since one loop prepares one profile's
    database and runs it serially."""
    found = {}
    for name in names:
        package, binary, test = cargo_item(name)
        profile = next((key for key, entry in CARGO_DATABASE_PROFILES.items()
                        if entry["target"] == f"{package}/{binary}" and test
                        and test.startswith(entry["prefixes"])), None)
        if profile is None and test and test.rpartition("::")[2].startswith("db_"):
            raise ValueError(f"cargo item {name!r} is a database test (db_) that no entry of "
                             "CARGO_DATABASE_PROFILES in rust/ci/stress.py covers, so it would run without "
                             "its database and its early return would count as run; add a profile naming its "
                             "target, test prefix and DSN variable, or select another test")
        found[name] = profile
    profiles = dict.fromkeys(found.values())
    if len(profiles) > 1:
        listed = ", ".join(f"{name!r} ({profile or 'no database'})" for name, profile in found.items())
        raise ValueError(f"cargo selection mixes database profiles: {listed}; one loop prepares one profile's "
                         "database and runs its tests serially, so dispatch the items of each profile separately")
    return next(iter(profiles), None)


def cargo_binary_id(package: str, binary: str) -> str:
    """Nextest names library test binaries by package alone."""
    return package if binary == "lib" else f"{package}::{binary.removeprefix('test/')}"


def filterset(names: list[str], binaries_only: bool = False) -> str:
    """The nextest filterset selecting a cargo selection's tests; with
    binaries_only, the binaries holding them (an archive holds whole binaries)."""
    items = [cargo_item(name) for name in names]
    if binaries_only:
        ids = dict.fromkeys(cargo_binary_id(package, binary) for package, binary, _ in items)
        return " | ".join(f"binary_id(={binary_id})" for binary_id in ids)
    return " | ".join(f"(binary_id(={cargo_binary_id(package, binary)})" + (f" & test(={test}))" if test else ")")
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
        if binary == "lib":
            argv += ["--lib"]
        else:
            target, _, name = binary.rpartition("/")
            argv += ["--bin" if target == "bin" else "--test", name]
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
        # --test-threads 4 is the Rust job's `cargo nextest run -j 4`. A
        # database profile's tests share one bank and serialize on an
        # in-process mutex, which nextest's process per test cannot hold, so
        # they run one at a time (as startup_ci.py runs them, --test-threads=1).
        threads = "1" if database_profile(names) else "4"
        return ["cargo-nextest", "nextest", "run", "--archive-file", str(archive),
                "--workspace-remap", workspace, "--extract-to", workspace, "--extract-overwrite",
                "--profile", "stress", "--retries", "0", "--no-fail-fast", "--no-tests", "fail",
                "--test-threads", threads, "-E", filterset(names)]
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
        # and gives nextest no parity candidate. A database profile's fixed
        # flags join it; run() adds its per-iteration database and fixture.
        profile = database_profile(names)
        return {"PSEUDOLIFE_MCP_PYTHON": sys.executable,
                **(CARGO_DATABASE_PROFILES[profile]["env"] if profile else {})}
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
    selection (the rest of a binary when one test is named, or tests excluded
    by nextest's default run-ignored policy) are left out."""
    try:
        root = ElementTree.parse(report).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    items = [cargo_item(name) for name in names]
    selected = []
    for suite in root.iter("testsuite"):
        for case in suite.iter("testcase"):
            # Only nextest's canonical default-exclusion record is not selected.
            # Unknown skips or records carrying additional failure evidence fail closed.
            if (len(case) == 1 and case[0].tag == "skipped"
                    and case[0].get("message") == "Skipped: test does not match the run-ignored option"):
                continue
            matched = [name for name, (package, binary, test) in zip(names, items)
                       if suite.get("name") == cargo_binary_id(package, binary) and test in (None, case.get("name"))]
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


def write_row(out: str | Path, row: dict) -> None:
    """Write an iteration's row whole or not at all: a temporary file beside
    it, then a rename over it, so a job killed mid-write leaves the previous
    row, never a torn one. The report's glob does not match the .tmp name,
    and a write or rename that fails removes it."""
    out = Path(out)
    temporary = out.with_name(out.name + ".tmp")
    try:
        temporary.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, out)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def runner() -> dict:
    return {"os": os.environ.get("RUNNER_OS", ""), "image": " ".join(
        filter(None, (os.environ.get("ImageOS"), os.environ.get("ImageVersion")))) or "unknown",
            "cpus": os.cpu_count()}


def plan(options) -> int:
    try:
        kind, names = parse(options.selection)
        count = iterations(options.iterations)
    except ValueError as error:
        print(f"stress: {error}; nothing was started", file=sys.stderr)
        return 2
    # postgres=true only for a cargo selection with a database profile: the
    # other kinds always prepare PostgreSQL (rust.yml's step condition).
    profile = database_profile(names) if kind == "cargo" else None
    with open(options.github_output, "a", encoding="utf-8") as output:
        output.write(f"iterations={json.dumps(list(range(1, count + 1)))}\n")
        output.write(f"kind={kind}\n")
        output.write(f"postgres={'true' if profile else 'false'}\n")
    each = f" ({len(names)} items each)" if kind in ITEM_KINDS and len(names) > 1 else ""
    database = (f"; database profile {profile}: a fresh disposable PostgreSQL each iteration, "
                "tests run one at a time") if profile else ""
    print(f"stress: {count} iterations of {options.selection.strip()}{each}{database}")
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


def harness_import_path() -> None:
    """Make the daemon harness's modules (pgdisposable, embedding_fixture) importable."""
    if str(HARNESS) not in sys.path:
        sys.path.insert(0, str(HARNESS))


def database_slice(profile: dict):
    """The pgdisposable namespace a profile's databases are made in, on the
    cluster and test login lease_ci.py prepared (PSEUDOLIFE_TEST_PG_HOST_PORT
    and PSEUDOLIFE_TEST_PG_LOGIN_FILE, from the job's GITHUB_ENV)."""
    harness_import_path()
    import pgdisposable
    return pgdisposable.Slice(profile["slice"])


@contextlib.contextmanager
def disposable_database(profile: dict):
    """A fresh database for one iteration, yielding its DSN, dropped
    afterwards whatever happened (a failed create included). A drop that
    fails is reported and leaves the verdict alone: the runner's cluster
    ends with the job, and the tests' evidence is already judged."""
    pg = database_slice(profile)
    name = pg.name(pg.PREFIX + "stress_" + str(time.time_ns()))
    try:
        yield pg.create(name)
    finally:
        try:
            pg.drop(name)
        except Exception as error:  # cleanup only; never the iteration's verdict
            print(f"stress: warning: could not drop disposable database {name}: {redacted(error)}",
                  file=sys.stderr, flush=True)


def onnx_runtime_library(capi: Path, windows: bool) -> Path | None:
    """The onnxruntime wheel's shared library in its capi directory, or None."""
    if windows:
        library = capi / "onnxruntime.dll"
        return library if library.is_file() else None
    return next(iter(sorted(capi.glob("libonnxruntime.so.*"))), None)


def fixture_environment(profile: dict, root: Path) -> dict:
    """A profile's embedder inputs under root: the embedding fixture model
    (harness/embedding_fixture.py, numpy/onnx/tokenizers) and the onnxruntime
    wheel's library for the ort crate's ORT_DYLIB_PATH, found as the startup
    lane's shadow fixture finds it."""
    env = {}
    fixture = profile.get("fixture")
    if fixture:
        harness_import_path()
        from embedding_fixture import create
        model = root / "model"
        create(model, dimension=fixture["dimension"])
        env[fixture["env"]] = str(model)
    if profile.get("ort_env"):
        import onnxruntime
        capi = Path(onnxruntime.__file__).parent / "capi"
        library = onnx_runtime_library(capi, os.name == "nt")
        if library is None:
            raise FileNotFoundError(f"no ONNX Runtime shared library in {capi}; a database iteration "
                                    "installs onnxruntime==1.27.0 in rust.yml's checkout install step")
        env[profile["ort_env"]] = str(library)
    return env


def cargo_database(profile_name: str, stack: contextlib.ExitStack) -> dict:
    """Enter a profile's database and fixture on stack; their env for nextest.
    The fixture lives outside the scratch directory the job uploads."""
    profile = CARGO_DATABASE_PROFILES[profile_name]
    env = {profile["dsn_env"]: stack.enter_context(disposable_database(profile))}
    if profile.get("fixture") or profile.get("ort_env"):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="pl-stress-fixture-",
                                                                    ignore_cleanup_errors=True)))
        env.update(fixture_environment(profile, root))
    return env


def redacted(error: BaseException) -> str:
    """An error for a row or log, without any connection string's credentials."""
    text = f"{type(error).__name__}: {error}"
    return re.sub(r"(postgres(?:ql)?://)[^\s/@]+@", r"\1***@", text, flags=re.I)


def run_items(options, kind: str, items: list[str]) -> int:
    """Several rows or scenarios, one after another: each runs as its own
    harness process with the binary, flags, env and summary a one-item
    selection would give it, in its own scratch directory, and is judged
    by the same evidence rule. Every item runs even after one fails or
    raises, so the row names each item's own result. The row is written
    before each item starts and once at the end, so a job that times out
    keeps the finished items' results and names the item still running."""
    scratch = Path(options.out).parent
    scratch.mkdir(parents=True, exist_ok=True)
    candidates = Path(options.candidates)
    started, results = time.monotonic(), []

    def record(running: dict | None = None) -> int:
        """Write the row as it stands and return its exit code."""
        failed = [result for result in results if result["outcome"] != "pass" and result is not running]
        if running is None and len(results) == len(items) and not failed:
            code = 0
        else:
            code = next((result["exit_code"] for result in failed if result["exit_code"]), 1)
        unrun = [f"{kind}:{item}" for item in items[len(results):]]
        note = "; ".join(filter(None, (
            f"{len(failed)} of {len(items)} items failed: " + ", ".join(result["item"] for result in failed)
            if failed else "", f"not finished: {running['item']}" if running else "",
            "not run: " + ", ".join(unrun) if unrun else "")))
        row = {"iteration": options.iteration, "attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT") or 1),
               "selection": options.selection.strip(), "runner": runner(), "exit_code": code,
               "outcome": "pass" if code == 0 else "fail",
               "seconds": round(time.monotonic() - started, 1),
               **({"note": note} if note else {}), "items": results}
        write_row(options.out, row)
        return code

    for index, item in enumerate(items, 1):
        label = f"{kind}:{item}"
        result = {"item": label, "exit_code": None, "outcome": "fail", "note": "not finished"}
        results.append(result)
        record(running=result)
        begun, exit_code = time.monotonic(), None
        try:
            names, here = item_names(kind, item), scratch / f"item-{index}"
            here.mkdir(parents=True, exist_ok=True)
            argv = command(kind, names, candidates, here)
            env = dict(os.environ, **environment(kind, names, candidates, here))
            print("stress: " + " ".join(argv), flush=True)
            exit_code = subprocess.run(argv, env=env).returncode
            note = f"exit code {exit_code}" if exit_code else evidence(kind, names, here)
            # Built whole before it replaces the placeholder, so a raise
            # anywhere above leaves a complete failed result, never an empty one.
            done = {"item": label, "exit_code": exit_code, "outcome": "fail" if note else "pass",
                    "compared": compared(kind, names, here), **({"note": note} if note else {})}
        except Exception as error:  # one item that cannot run is that item's failure, not the loop's
            done = {"item": label, "exit_code": exit_code, "outcome": "fail",
                    "note": f"{type(error).__name__}: {error}"}
        result.clear()
        result.update(done, seconds=round(time.monotonic() - begun, 1))
        print(f"stress: {label} {result['outcome']}" + (f": {result['note']}" if "note" in result else ""),
              flush=True)
    return record()


def run(options) -> int:
    kind, names = parse(options.selection)
    if kind in ITEM_KINDS:
        if len(names) > 1:
            return run_items(options, kind, names)
        names = item_names(kind, names[0])
    scratch = Path(options.out).parent
    scratch.mkdir(parents=True, exist_ok=True)
    candidates = Path(options.candidates)
    archive_file = Path(options.archive_file).resolve() if options.archive_file else None
    argv = command(kind, names, candidates, scratch, archive_file)
    env = dict(os.environ, **environment(kind, names, candidates, scratch))
    # A database profile's bank and fixture: entered below, released after
    # the row is written, whatever the outcome.
    with contextlib.ExitStack() as stack:
        return run_once(options, kind, names, scratch, archive_file, argv, env, stack)


def run_once(options, kind: str, names: list[str], scratch: Path, archive_file: Path | None,
             argv: list[str], env: dict, stack: contextlib.ExitStack) -> int:
    """One run of a one-item selection, judged and written as its row."""
    started, code, note, tests, judged = time.monotonic(), None, "", None, False
    try:
        if kind == "cargo":
            note = checkout_mismatch(archive_file)
            if note:
                code = 1
                print(f"stress: {note}", flush=True)
                return code
            profile = database_profile(names)
            if profile:
                try:
                    env.update(cargo_database(profile, stack))
                except Exception as error:  # a setup that fails is this iteration's failure, in its row
                    note, code = f"database profile {profile} setup failed: {redacted(error)}", 1
                    print(f"stress: {note}", flush=True)
                    return code
            # Only this iteration's report may count.
            cargo_junit(WORKSPACE).unlink(missing_ok=True)
        print("stress: " + " ".join(argv), flush=True)
        code = subprocess.run(argv, env=env).returncode
        try:
            if kind == "cargo" and cargo_junit(WORKSPACE).is_file():
                # The row's artifact keeps the report; evidence reads this copy.
                (scratch / "junit.xml").write_bytes(cargo_junit(WORKSPACE).read_bytes())
                tests = cargo_times(names, scratch / "junit.xml")
            if code == 0:
                note = evidence(kind, names, scratch)
                if note:
                    code = 1
                    print(f"stress: {note}", flush=True)
            judged = True
        except Exception as error:  # a run whose evidence cannot be judged is no evidence, whatever it exited
            note, code = f"{type(error).__name__}: {error}", code or 1
            print(f"stress: {note}", flush=True)
    finally:
        # Only a judged run may pass: an interrupt (a job timeout or cancel)
        # during the evidence check leaves code 0 with nothing judged.
        if code == 0 and not judged:
            note = note or "interrupted before evidence was judged"
        row = {"iteration": options.iteration, "attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT") or 1),
               "selection": options.selection.strip(), "runner": runner(), "exit_code": code,
               "outcome": "pass" if code == 0 and judged else "fail",
               "seconds": round(time.monotonic() - started, 1),
               **({"note": note} if note else {}), **({"tests": tests} if tests is not None else {})}
        write_row(options.out, row)
    return code


def complete_row(row) -> bool:
    """Whether a row holds every field the report reads."""
    def whole(item):
        return isinstance(item, dict) and isinstance(item.get("item"), str) and isinstance(item.get("outcome"), str)
    return (isinstance(row, dict) and type(row.get("iteration")) is int and row.get("outcome") in ("pass", "fail")
            and "seconds" in row and isinstance(row.get("runner"), dict)
            and type(row.get("attempt", 1)) is int
            and isinstance(row.get("items", []), list) and all(whole(item) for item in row.get("items", [])))


def read_row(path: Path) -> tuple[int | None, dict]:
    """An iteration's number and row. A row that cannot be read whole (a
    kill mid-write, a partial upload) is that iteration's failure, numbered
    by its file name, so one bad file never costs the other iterations'
    report; a file whose name carries no number is not an iteration's row."""
    numbered = re.fullmatch(r"stress-row-(\d+)\.json", path.name)
    number = int(numbered[1]) if numbered else None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        note = f"unreadable row {path.name}: {type(error).__name__}: {error}"
    else:
        if complete_row(row):
            return row["iteration"], row
        note = f"incomplete row {path.name}: a field the report needs is missing or malformed"
    print(f"stress: {note}", file=sys.stderr)
    return number, {"iteration": number, "outcome": "fail", "exit_code": None, "seconds": None,
                    "runner": {}, "note": note}


def report(options) -> int:
    count = iterations(options.iterations)
    rows = {}
    for path in sorted(Path(options.rows).glob("**/stress-row-*.json")):
        iteration, row = read_row(path)
        if iteration is not None:
            rows[iteration] = row
    table = [rows.get(i, {"iteration": i, "outcome": "no result", "exit_code": None, "seconds": None,
                          "runner": {}}) for i in range(1, count + 1)]
    passed = sum(row["outcome"] == "pass" for row in table)
    # Artifact rows alone cannot explain what a rerun replaced. Mixed
    # attempts require API evidence that the replaced job never ran a test.
    attempts = sorted({row["attempt"] for row in table if "attempt" in row})
    mixed = len(attempts) > 1
    history = None
    if options.job_history:
        from stress_history import assess
        try:
            document = json.loads(Path(options.job_history).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = None
        history = assess(document, table, options.os)
    summary = {"selection": options.selection.strip(), "os": options.os, "iterations": count,
               "passed": passed, "failed": sum(row["outcome"] == "fail" for row in table),
               "no_result": sum(row["outcome"] == "no result" for row in table),
               "attempts": attempts, "clean": passed == count and (history["clean"] if history else not mixed),
               **({"job_history": history} if history else {}), "rows": table}
    # Several rows or scenarios: each item's own tally, so a failing one is named.
    kind, names = parse(options.selection)
    labels = [f"{kind}:{name}" for name in names] if kind in ITEM_KINDS and len(names) > 1 else []
    results = [{item["item"]: item["outcome"] for item in row.get("items", [])} for row in table]
    items = {label: {"passed": sum(result.get(label) == "pass" for result in results),
                     "failed": sum(result.get(label) == "fail" for result in results),
                     "no_result": sum(label not in result for result in results)} for label in labels}
    if items:
        summary = {**{key: value for key, value in summary.items() if key != "rows"}, "items": items, "rows": table}
    Path(options.out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    lines = [f"### Stress: {summary['selection']} on {options.os}",
             f"{passed} of {count} iterations passed."]
    lines += [f"- {label}: {tally['passed']} of {count} iterations passed" for label, tally in items.items()]
    if history:
        lines += [history["reason"], "", "| Iteration | Attempt | Job | Outcome | Cause |",
                  "| --- | --- | --- | --- | --- |"]
        lines += [f"| {job['iteration']} | {job['attempt']} | {job['job_id']} | {job['outcome']} | {job['cause']} |"
                  for job in history["jobs"]]
    elif mixed:
        lines.append(f"Not clean: rows come from run attempts {attempts}; a re-run of failed jobs "
                     "replaced at least one failure. Dispatch a new loop.")

    def note(row: dict) -> str:
        if not labels or "items" not in row:
            return row.get("note", "")
        found = {item["item"]: item for item in row["items"]}
        return "; ".join(f"{label} {found[label]['outcome']}" + (f": {found[label]['note']}"
                                                                 if found[label].get("note") else "")
                         if label in found else f"{label} no result" for label in labels)
    lines += ["", "| Iteration | Attempt | Outcome | Seconds | Image | CPUs | Note |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {row['iteration']} | {row.get('attempt', '')} | {row['outcome']} "
              f"| {row['seconds'] if row['seconds'] is not None else ''} "
              f"| {row['runner'].get('image', '')} | {row['runner'].get('cpus', '')} | {note(row)} |"
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
    s.add_argument("--job-history", help="jobs API history for every attempt; required to accept a setup-only retry")
    options = parser.parse_args(argv)
    return {"plan": plan, "archive": archive, "run": run, "report": report}[options.action](options)


if __name__ == "__main__":
    sys.exit(main())
