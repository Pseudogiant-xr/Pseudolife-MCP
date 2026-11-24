"""Probe a trusted commit with the Python package omitted from its Git archive.

On Linux, use a Python 3.11.4+ tooling environment and run
    python rust/ci/package_absent.py HEAD
Install the tooling dependencies you want to test with (including PyYAML for
the budget check). A Python environment that can import the package is refused.
The temporary checkout and home are made under your home directory. Probe
failures produce exit 1. Setup errors produce exit 2.

The checks import live test modules and shared fixtures, collect the suite with
pytest (which loads tests/conftest.py), check the CI budget and producer census,
and request harness help. A successful import, collection or --help does not
exercise a test body, a build, an installation or a daemon.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from package_consumers import ACTIVE, CATEGORIES, PACKAGE, ROOT, TESTING


PER_PROBE_SECONDS = 12
# A ceiling, not a measurement: room for the two collection probes beside the
# few hundred imports the 240 s it replaced was sized for.
TOTAL_PROBE_SECONDS = 600
BOOTSTRAP = """
from importlib.util import find_spec
from pathlib import Path
import re
import runpy
import sys

package, mode, target, *arguments = sys.argv[1:]
if find_spec(package) is not None:
    raise SystemExit(f"Tooling Python can import {package}. Use a clean tooling environment.")

def restrict_probe(event, args):
    if event in {"socket.connect", "socket.bind", "socket.sendto", "socket.getaddrinfo",
                 "os.system", "os.exec", "os.posix_spawn", "os.fork", "os.forkpty"}:
        raise RuntimeError(f"Probe blocked {event}")
    if event == "subprocess.Popen" and (
        target != "rust/producer_census.py" or args[0] != "git"
        or not (args[1][1] == "ls-files" or args[1][1] == "show" and len(args[1]) == 3
                and re.fullmatch(r"[0-9a-f]{40}:pseudolife_memory/[\\w/.]+", args[1][2]))
    ):
        raise RuntimeError("Probe blocked an unexpected subprocess")

sys.addaudithook(restrict_probe)
root = Path.cwd()
if mode == "check":
    if (root / package).exists():
        raise SystemExit(f"Package survived archive exclusion: {package}")
elif mode == "module":
    sys.path.insert(0, str(root))
    sys.argv = [target, *arguments]
    runpy.run_module(target, run_name="__main__", alter_sys=True)
elif mode == "collect":
    # Collection loads tests/conftest.py and its fixtures, which importing a
    # test module alone does not. Nothing is run.
    sys.path.insert(0, str(root))
    import pytest
    raise SystemExit(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", target]))
else:
    script = root / target
    sys.path[:0] = [str(root), str(script.parent)]
    sys.argv = [str(script), *arguments]
    name = "__main__" if mode == "script" else "tests." + script.stem
    runpy.run_path(str(script), run_name=name)
"""


@dataclass(frozen=True)
class Probe:
    category: str
    mode: str
    target: str
    arguments: tuple[str, ...] = ()
    seconds: float = PER_PROBE_SECONDS

    @property
    def label(self) -> str:
        return " ".join((self.mode, self.target, *self.arguments))


def plan(checkout: Path) -> list[Probe]:
    live = []
    for line in (checkout / "tests/suite_classes.txt").read_text(encoding="utf-8").splitlines():
        if not line.startswith("live "):
            continue
        _, filename = line.split()
        if not re.fullmatch(r"test_\w+\.py", filename):
            raise ValueError(f"Invalid live test filename: {filename}")
        live.append(Probe(TESTING, "import", f"tests/{filename}"))
    fixtures = (
        "conftest.py", "fake_embedder.py", "helpers.py", "pg_defaults.py",
        "pg_fixtures.py", "suite_lock.py",
    )
    return [
        Probe(ACTIVE, "script", "rust/ci/ci_budget.py", ("check",)),
        Probe(ACTIVE, "script", "rust/producer_census.py", ("--check",)),
        Probe(ACTIVE, "script", "rust/ci/pr_precheck_run.py", ("--help",)),
        Probe(TESTING, "script", "rust/cli_harness/__main__.py", ("--help",)),
        Probe(TESTING, "script", "rust/daemon/harness/run.py", ("--help",)),
        Probe(TESTING, "module", "evals.rust_port.cli_dispatch", ("--help",)),
        *(Probe(TESTING, "import", f"tests/{filename}") for filename in fixtures),
        # pytest itself: the suite's conftest and fixtures load (one file whose
        # own imports are package-free), then the whole default collection.
        # The limits are ceilings, not measurements: pytest's start-up and a
        # collection that imports every live test module outlast the 12 s
        # an import gets.
        Probe(TESTING, "collect", "tests/test_fixture_guards_package_absent.py", seconds=60),
        Probe(TESTING, "collect", "tests", seconds=180),
        *live,
    ]


def temp_parent(repo: Path) -> Path:
    home = Path.home().resolve(strict=True)
    worktrees = subprocess.check_output(["git", "worktree", "list", "--porcelain", "-z"], cwd=repo)
    if any(home.is_relative_to(Path(os.fsdecode(entry[9:])).resolve())
           for entry in worktrees.split(b"\0") if entry.startswith(b"worktree ")):
        raise ValueError("Set HOME outside the repository worktrees before running this probe.")
    enclosing = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=home,
                               capture_output=True, check=False, text=True)
    if enclosing.stdout.strip() == "true":
        raise ValueError("Set HOME outside the enclosing Git checkout before running this probe.")
    return home


def archive(repo: Path, commit: str, checkout: Path) -> None:
    command = ["git", "archive", "--format=tar", commit, "--", ".", f":(exclude,top){PACKAGE}"]
    with subprocess.Popen(command, cwd=repo, stdout=subprocess.PIPE) as git:
        try:
            with tarfile.open(fileobj=git.stdout, mode="r|") as stream:
                stream.extractall(checkout, filter="data")
        finally:
            git.stdout.close()
        if git.wait():
            raise RuntimeError("git archive failed")


def probe_env(directory: Path, git: str) -> dict[str, str]:
    home, bin_dir = directory / "home", directory / "bin"
    home.mkdir()
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(Path(git).resolve())
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home),
        "LOCALAPPDATA": str(home),
        "XDG_CONFIG_HOME": str(home),
        "XDG_CACHE_HOME": str(home),
        "TMPDIR": str(home),
        "PATH": str(bin_dir),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:1",
        "PSEUDOLIFE_MCP_NO_SPAWN": "1",
        # libpq connects outside the audit hook below, so every PostgreSQL
        # setting conftest reads points nowhere: an explicit test URL (which
        # also ends the suite's password preflight before it opens a socket),
        # and a bench name and admin URL that keep its exit-time DROP inert.
        "PSEUDOLIFE_TEST_DATABASE_URL": "postgresql://probe@127.0.0.1:1/pl_cf_prb_absent",
        "PSEUDOLIFE_BENCH_DB": "pl_cf_prb_absent_bench",
        "PSEUDOLIFE_BENCH_ADMIN_URL": "postgresql://probe@127.0.0.1:1/postgres",
        "CUDA_VISIBLE_DEVICES": "-1",
        "HF_HUB_OFFLINE": "1",
        "BROWSER": str(bin_dir / "no-browser"),
        "CLI_HARNESS_PYCACHE_PREFIX": str(home / "empty-pycache"),
    }


def run_probe(probe: Probe, checkout: Path, env: dict[str, str], seconds: float) -> tuple[int, str]:
    command = [sys.executable, "-I", "-B"]
    if probe.target == "rust/cli_harness/__main__.py":
        command.extend(("-X", f"pycache_prefix={env['CLI_HARNESS_PYCACHE_PREFIX']}"))
    command.extend(("-c", BOOTSTRAP, PACKAGE, probe.mode, probe.target, *probe.arguments))
    try:
        result = subprocess.run(command, cwd=checkout, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                timeout=seconds, check=False)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    output = result.stdout.replace(str(checkout), "<checkout>").replace(env["HOME"], "<home>")
    lines = output.splitlines()
    reason = next((line.strip() for line in reversed(lines) if line.strip()), "no output")
    if probe.mode == "collect" and result.returncode:
        # pytest's last line is a count; name the first modules it could not
        # collect, or its internal error, so the cause is visible.
        causes = [line.strip() for line in lines
                  if line.startswith(("ERROR ", "INTERNALERROR> E ", "E   "))][:3]
        reason = "; ".join([*causes, reason])
    return result.returncode, reason if result.returncode else "completed"


def run(commit: str) -> int:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is required on PATH.")
    sha = subprocess.check_output(
        [git, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
        cwd=ROOT, text=True,
    ).strip()
    with tempfile.TemporaryDirectory(prefix=".package-absent-", dir=temp_parent(ROOT)) as name:
        directory = Path(name)
        checkout = directory / "checkout"
        checkout.mkdir()
        env = probe_env(directory, git)
        archive(ROOT, sha, checkout)
        code, reason = run_probe(Probe(ACTIVE, "check", ""), checkout, env, PER_PROBE_SECONDS)
        if code:
            raise RuntimeError(reason)
        # The census reads git ls-files. Give it an index of the archive files
        # without exposing history or the original checkout.
        subprocess.run([git, "-c", "init.templateDir=", "init", "-q"],
                       cwd=checkout, env=env, check=True)
        subprocess.run([git, "add", "-N", "-f", "--", "."], cwd=checkout, env=env, check=True)
        # It reads the frozen package's contract sources from Git at its pinned
        # commit, as it does in any clone once the package is deleted: fetch
        # that one commit's objects. Nothing lands in the working tree, so no
        # import can reach the package.
        census = (checkout / "rust/producer_census.py").read_text(encoding="utf-8")
        pin = re.search(r'^FROZEN_SOURCES_COMMIT = "([0-9a-f]{40})"$', census, re.M)
        if pin:
            subprocess.run([git, "fetch", "--quiet", "--no-tags", "--depth=1", ROOT.as_uri(), pin[1]],
                           cwd=checkout, env=env, check=True)
            code, reason = run_probe(Probe(ACTIVE, "check", ""), checkout, env, PER_PROBE_SECONDS)
            if code:
                raise RuntimeError(reason)

        print(f"Package-absent probe at {sha}", flush=True)
        probes = plan(checkout)
        failed = False
        deadline = time.monotonic() + TOTAL_PROBE_SECONDS
        for category in CATEGORIES:
            print(f"\n[{category}]", flush=True)
            group = [probe for probe in probes if probe.category == category]
            if not group:
                print("No runtime probes selected.")
            for probe in group:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    code, reason = run_probe(probe, checkout, env, min(probe.seconds, remaining))
                else:
                    code, reason = 124, "not run: overall time limit reached"
                failed |= code != 0
                outcome = f"FAIL ({code})" if code else "PASS"
                print(f"{outcome} {probe.label}: {reason}", flush=True)
    print(f"Package-absent probe {'found failures' if failed else 'passed'}")
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", help="trusted commit to archive, such as HEAD")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("run the package-absent probe on Linux")
    try:
        return run(args.commit)
    except (OSError, RuntimeError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print(f"Package-absent setup failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
