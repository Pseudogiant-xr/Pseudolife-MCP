"""ops/integrate.py: hand-built merge batches of `ready-to-batch` PRs.

The git side is real: a bare `origin` whose `refs/pull/<n>/head` refs carry
each PR's head, the way GitHub serves them, and a clone the script runs in.
Only `gh` is faked, by replacing the module's `gh()` with a recorder that
answers the handful of calls the script makes.
"""

from __future__ import annotations

import base64
import dataclasses
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATE = "2026-10-19"
LABEL = "ready-to-batch"


def _load():
    spec = importlib.util.spec_from_file_location(
        "integrate_under_test", ROOT / "ops" / "integrate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


integrate = _load()
_REAL_HARNESS_RECIPE = integrate._harness_recipe


def _model_harness_recipe(paths=None):
    reader = integrate._harness_reader()
    return reader.Recipe((reader.Command("pytest", tuple(paths or integrate.RUST_HARNESS)),))


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8")
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def _configure(repo: Path) -> None:
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"),
                       ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        _git(repo, "config", key, value)


class FakeGh:
    """Answers the script's gh calls from in-memory PR data and records them."""

    def __init__(self) -> None:
        self.ready: list[dict] = []
        self.labeled_at: dict[int, str] = {}
        self.history: list[dict] = []   # earlier PRs, for numbering and open batches
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail_create: str | None = None   # "before" or "after" creating
        self.fail_comments = 0
        self.fail_edits = 0
        self.unmerged: set[int] = set()   # members `pr merge` leaves unmarked
        self.lag: dict[int, int] = {}       # reads of OPEN before a member shows MERGED
        self.fail_merge: str | None = None  # "before" or "after" merging
        # A check run's annotation messages by job id; a missing id fails.
        self.annotations: dict[str, list[str]] = {}
        # The Actions API a failed gate is read through; a missing key fails.
        self.actions_jobs: dict[str, dict] = {}      # job id -> the job
        self.runs: dict[str, dict] = {}              # run id -> the run
        self.attempt_jobs: dict[tuple[str, str], list[dict]] = {}   # (run, attempt)
        self.files: dict[tuple[str, str], str] = {}  # (path, ref) -> file text
        # `gh run list --commit <sha>`: the workflow runs on a head; every
        # run completed unless a test says otherwise.
        self.run_lists: dict[str, list[dict]] = {}

    def add(self, number: int, sha: str, *, title: str | None = None,
            labeled_at: str | None = None, **extra) -> None:
        pr = {"number": number, "title": title or f"change {number}",
              "headRefOid": sha, "url": f"https://github.example/pr/{number}",
              "isDraft": False, "reviewDecision": "APPROVED",
              "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}
        pr.update(extra)
        self.ready.append(pr)
        if labeled_at:
            self.labeled_at[number] = labeled_at

    VIEW_FIELDS = {"headRefOid", "statusCheckRollup", "headRefName", "baseRefName",
                   "state", "isCrossRepository", "url", "mergeCommit", "labels"}
    VIEW_DEFAULTS = {"baseRefName": "master", "state": "OPEN", "isCrossRepository": False,
                     "mergeCommit": None, "labels": [],
                     "statusCheckRollup": [
                         {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}
                         for name in ("scope", "test", "Parity / ubuntu-latest",
                                      "Parity / windows-latest")]}

    def pr(self, number: int) -> dict:
        return next(r for r in self.ready + self.history if r["number"] == number)

    def mutations(self) -> list[list[str]]:
        return [args for args, _ in self.calls
                if args[:2] in (["pr", "create"], ["pr", "comment"], ["pr", "edit"],
                                ["pr", "merge"])]

    def bodies(self, verb: str) -> list[tuple[list[str], str]]:
        return [(args, body) for args, body in self.calls if args[:2] == ["pr", verb]]

    def __call__(self, *args: str, input: str | None = None) -> str:
        args = list(args)
        self.calls.append((args, input))
        if args[:2] == ["pr", "list"] and "--label" in args:
            assert args[args.index("--label") + 1] == LABEL
            assert args[args.index("--base") + 1] == "master"
            assert args[args.index("--state") + 1] == "open"
            return json.dumps(self.ready)
        if args[:2] == ["pr", "list"]:
            state = args[args.index("--state") + 1]
            return json.dumps([r for r in self.history
                               if state == "all" or r["state"].lower() == state])
        if args[:2] == ["pr", "view"]:
            assert args[3] == "--json" and len(args) == 5, args
            fields = args[4].split(",")
            assert set(fields) <= self.VIEW_FIELDS, fields
            number = int(args[2])
            pr = self.pr(number)
            view = {f: pr.get(f, self.VIEW_DEFAULTS.get(f)) for f in fields}
            if view.get("state") == "MERGED" and self.lag.get(number):
                self.lag[number] -= 1           # GitHub has not caught up yet
                view["state"] = "OPEN"
            return json.dumps(view)
        if args[:2] == ["pr", "merge"]:
            if self.fail_merge == "before":
                raise integrate.Refusal("`gh pr merge` failed (1): HTTP 502")
            pr = self.pr(int(args[2]))
            assert pr["state"] == "OPEN"
            head = args[args.index("--match-head-commit") + 1]
            # The merge GitHub would make: a merge commit of that head on master.
            _git(self.seed, "fetch", "-q", "origin")
            _git(self.seed, "switch", "-q", "-C", "master", "origin/master")
            _git(self.seed, "merge", "-q", "--no-ff", "-m",
                 f"Merge pull request #{args[2]}", head)
            _git(self.seed, "push", "-q", "origin", "master")
            pr["state"] = "MERGED"
            pr["mergeCommit"] = {"oid": _git(self.seed, "rev-parse", "HEAD")}
            for member in self.ready:
                if member["number"] not in self.unmerged:
                    member["state"] = "MERGED"
            if self.fail_merge == "after":       # merged, but the reply was lost
                raise integrate.Refusal("`gh pr merge` failed (1): HTTP 504")
            return ""
        if args[0] == "api" and "/check-runs/" in args[1]:
            # Every page: a timeout annotation can follow 30 others.
            assert args[1].endswith("/annotations")
            # One JSON string per message, so a multi-line message stays whole.
            assert args[2:] == ["--paginate", "--jq", ".[].message | @json"], args
            job = args[1].split("/")[-2]
            if job not in self.annotations:
                raise integrate.Refusal("`gh api` failed (1): HTTP 502")
            return "".join(f"{json.dumps(m)}\n" for m in self.annotations[job])
        if args[0] == "api" and any("/actions/" in a or "/contents/" in a for a in args):
            return self._actions(args)
        if args[0] == "api":
            number = int(next(a for a in args if "/issues/" in a).split("/")[-2])
            return self.labeled_at.get(number, "") + "\n"
        if args[:2] == ["pr", "create"]:
            if self.fail_create == "before":
                raise integrate.Refusal("`gh pr create` failed (1): HTTP 502")
            # 900 for the first batch, 901 for one built again after it.
            number = 900 + sum(1 for r in self.history if r["number"] >= 900)
            url = f"https://github.example/pr/{number}"
            head = args[args.index("--head") + 1]
            self.history.append({"number": number, "state": "OPEN", "url": url,
                                 "headRefName": head, "isCrossRepository": False,
                                 "headRefOid": _git(self.origin, "rev-parse", head)})
            if self.fail_create == "after":  # created, but the reply was lost
                raise integrate.Refusal("`gh pr create` failed (1): HTTP 504")
            return url + "\n"
        if args[:2] == ["run", "list"]:
            commit = args[args.index("--commit") + 1]
            return json.dumps(self.run_lists.get(
                commit, [{"status": "COMPLETED", "name": "CI", "databaseId": 1}]))
        if args[:2] == ["pr", "comment"] and self.fail_comments:
            self.fail_comments -= 1
            raise integrate.Refusal("`gh pr comment` failed (1): HTTP 502")
        if args[:2] == ["pr", "edit"] and self.fail_edits:
            self.fail_edits -= 1
            raise integrate.Refusal("`gh pr edit` failed (1): HTTP 502")
        if args[:2] in (["pr", "comment"], ["pr", "edit"]):
            return ""
        raise AssertionError(f"unexpected gh call: {args}")

    def _actions(self, args: list[str]) -> str:
        """The Actions REST calls that read a failed gate's run."""
        unreadable = integrate.Refusal("`gh api` failed (1): HTTP 502")
        prefix = "repos/{owner}/{repo}/"
        if args[1:3] == ["-H", "Accept: application/vnd.github.raw+json"]:
            assert len(args) == 4 and args[3].startswith(prefix + "contents/"), args
            path, ref = args[3].removeprefix(prefix + "contents/").split("?ref=")
            if (path, ref) not in self.files:
                raise unreadable
            return self.files[(path, ref)]
        if args[1] == "--paginate":
            # Every page: a matrix workflow lists more jobs than one page holds.
            assert args[3:] == ["--jq", ".jobs[] | @json"], args
            parts = args[2].removeprefix(prefix).split("/")
            assert parts[:2] == ["actions", "runs"] and parts[3] == "attempts", args
            # 100 a page made GitHub answer 502 on every try for a 140-job
            # attempt (run 40995606796, 2026-10-30); 50 a page worked.
            assert parts[5] == "jobs?per_page=50", args
            key = (parts[2], parts[4])
            if key not in self.attempt_jobs:
                raise unreadable
            # gh prints each `@json` string raw: one job's JSON per line.
            return "".join(f"{json.dumps(j)}\n" for j in self.attempt_jobs[key])
        assert len(args) == 2 and args[1].startswith(prefix + "actions/"), args
        kind, ident = args[1].removeprefix(prefix + "actions/").split("/")
        table = {"jobs": self.actions_jobs, "runs": self.runs}[kind]
        if ident not in table:
            raise unreadable
        return json.dumps(table[ident])


# Stands in for `cargo`: records its arguments, directory and the
# environment it was given, then exits with FAKE_CARGO_EXIT (0 by default),
# printing a compile error as cargo would when that is not 0.
_FAKE_CARGO = """\
import json, os, sys
with open(os.environ["FAKE_CARGO_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                          "env": dict(os.environ)}) + "\\n")
code = int(os.environ.get("FAKE_CARGO_EXIT", "0"))
if code and os.environ.get("FAKE_CARGO_OFFLINE"):
    sys.stderr.write("    Updating crates.io index\\n"
                     "error: failed to download from `https://index.crates.io/`\\n")
elif code:
    sys.stderr.write("    Checking pseudolife-daemon v0.19.0\\n"
                     "error[E0061]: this function takes 3 arguments but 2 arguments "
                     "were supplied\\n   --> daemon/src/health.rs:42:9\\n"
                     "error: could not compile `pseudolife-daemon` (lib) due to "
                     "1 previous error\\n")
sys.exit(code)
"""

# Stands in for `ssh` to the machine a remote Rust compile check runs on
# (rust-check.remote): records its arguments, the encoded command it was
# given (decoded) and the bundle that arrived on stdin (decoded to a file),
# then answers as ops/rust-check-remote.ps1 would for FAKE_SSH_MODE.
_FAKE_SSH = r'''
import base64, json, os, sys
argv = sys.argv[1:]
first, _, data = sys.stdin.read().partition("\n")
script = base64.b64decode(first).decode("utf-8")
log = os.environ["FAKE_SSH_LOG"]
calls = sum(1 for _ in open(log, encoding="utf-8")) if os.path.exists(log) else 0
bundle = f"{log}.{calls}.bundle"
with open(bundle, "wb") as out:
    out.write(base64.b64decode(data))
with open(log, "a", encoding="utf-8") as out:
    out.write(json.dumps({"argv": argv, "script": script, "bundle": bundle}) + "\n")
import re
if script.startswith("set -- "):
    import shlex
    nonce = shlex.split(script.splitlines()[0])[6]
else:
    nonce = re.search(r"-Nonce '([0-9a-f]+)'", script).group(1)
tag = f"rust-check {nonce}:"
mode = os.environ.get("FAKE_SSH_MODE", "pass")
if script.startswith("set -- ") and mode in ("pass", "red"):
    code = 0 if mode == "pass" else 100
    sys.stderr.write("Summary [1.2s] 12 tests run: 12 passed, 0 skipped\n"
                     f"shim-tests {nonce}: nextest exit {code}\n")
    sys.exit(code)
if "-PytestArgs" in script:
    tag = f"rust-harness {nonce}:"
    if mode == "pass":
        sys.stderr.write("rust-harness: Linux Docker engine up after 6 s\n"
                         "2876 passed, 20 skipped, 10 warnings, 153 subtests passed in 1004.76s (0:16:44)\n"
                         f"{tag} pytest exit 0\n")
        sys.exit(0)
    if mode == "red":
        sys.stderr.write("FAILED rust/ci/test_x.py::test_y\n"
                         "1 failed, 2875 passed, 20 skipped in 990.01s (0:16:30)\n"
                         f"{tag} pytest exit 1\n")
        sys.exit(1)
    if mode == "nohook":
        sys.stderr.write(f"{tag} not run: this machine has no services hook\n")
        sys.exit(3)
if mode == "unreachable":
    sys.stderr.write("ssh: connect to host vm port 22: Connection timed out\n")
    sys.exit(255)
if mode == "mismatch":
    sys.stderr.write(f"{tag} not run: the bundle's head is 'abc', not the batch head\n")
    sys.exit(3)
if mode == "compile":
    sys.stderr.write("    Checking pseudolife-daemon v0.19.0\n"
                     "error[E0061]: this function takes 3 arguments but 2 arguments "
                     "were supplied\n   --> daemon\\src\\health.rs:42:9\n"
                     "error: could not compile `pseudolife-daemon` (lib) due to "
                     "1 previous error\n"
                     f"{tag} cargo exit 101\n")
    sys.exit(101)
if mode == "silent":
    sys.exit(1)
if mode == "spoof":
    # A member's proc macro prints verdict-shaped lines, then the session
    # drops before the far half says anything.
    sys.stderr.write("rust-check: cargo exit 0\nrust-check 0123456789abcdef: cargo exit 0\n")
    sys.exit(255)
if mode == "trailing":
    # The real verdict, but something after it: not the far half's last word.
    sys.stderr.write(f"{tag} cargo exit 0\nwarning: something after\n")
    sys.exit(0)
sys.stderr.write(f"    Finished `dev` profile\n{tag} cargo exit 0\n")
'''

VM_CONFIG = "ssh=dev-vm\nrepo=C:\\dev\\Pseudolife-MCP\nrole=Windows dev VM\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return _make_world(tmp_path, monkeypatch)


@pytest.fixture
def world_without_guard(tmp_path, monkeypatch):
    """Master as it is before the pull request that adds the Python freeze
    guard lands: no commit on its history has ever touched the guard."""
    return _make_world(tmp_path, monkeypatch, guard=False)


def _make_world(tmp_path, monkeypatch, *, guard=True, harness_guard=False):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    _git(tmp_path, "init", "-q", "-b", "master", str(seed))
    _configure(seed)
    (seed / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (seed / "README.md").write_text("readme\n", encoding="utf-8")
    # The workspace manifest the Rust compile check runs beside (the census
    # does not scan it).
    (seed / "rust").mkdir()
    (seed / "rust" / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    if harness_guard:
        for name, text in {
            "ops/integrate.py": 'RUST_HARNESS = ("rust/test_base.py",)\n',
            ".github/workflows/rust.yml": json.dumps({"jobs": {
                "rust": {"steps": [{"run": "python -m pytest rust/test_base.py"}]},
                "parity-checks": {"steps": []}}}),
            "rust/ci/harness-test-selections.json": '{"schema": 1, "files": {}}\n',
            "rust/test_base.py": "def test_base(): pass\n",
        }.items():
            (seed / name).parent.mkdir(parents=True, exist_ok=True)
            (seed / name).write_text(text, encoding="utf-8")
        monkeypatch.setattr(integrate, "_harness_recipe", _REAL_HARNESS_RECIPE)
        _REAL_HARNESS_RECIPE.cache_clear()
    else:
        # This fixture models Git and admission, with no actual workflow/test
        # tree. Coverage-specific tests opt into the real batch-head reader.
        monkeypatch.setattr(integrate, "_harness_recipe", lambda head: _model_harness_recipe())
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "master")
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _configure(work)
    monkeypatch.chdir(work)
    fake = FakeGh()
    fake.origin, fake.seed = origin, seed
    monkeypatch.setattr(integrate, "gh", fake)
    clock = [0.0]                      # `merge` polls members on this clock

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(integrate, "_sleep", sleep, raising=False)
    monkeypatch.setattr(integrate, "_clock", lambda: clock[0], raising=False)
    # A batch touching rust/ runs the Rust compile check; tests never run
    # the real cargo, only this recorder in its place.
    cargo_log = tmp_path / "cargo-calls.jsonl"
    fake_cargo = tmp_path / "fake_cargo.py"
    fake_cargo.write_text(_FAKE_CARGO, encoding="utf-8")
    monkeypatch.setenv("FAKE_CARGO_LOG", str(cargo_log))
    monkeypatch.delenv("FAKE_CARGO_EXIT", raising=False)
    monkeypatch.delenv("FAKE_CARGO_OFFLINE", raising=False)
    monkeypatch.setattr(integrate, "RUST_CHECK",
                        (sys.executable, str(fake_cargo), "check", "--workspace",
                         "--all-targets", "--locked"), raising=False)
    # No remote compile check unless a test configures one: a
    # rust-check.remote on the machine running the tests never reaches them.
    remote_file = tmp_path / "rust-check.remote"
    ssh_log = tmp_path / "ssh-calls.jsonl"
    fake_ssh = tmp_path / "fake_ssh.py"
    fake_ssh.write_text(_FAKE_SSH, encoding="utf-8")
    monkeypatch.setenv("FAKE_SSH_LOG", str(ssh_log))
    monkeypatch.delenv("FAKE_SSH_MODE", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_RUST_CHECK_WHERE", raising=False)
    monkeypatch.setattr(integrate, "RUST_CHECK_REMOTE", remote_file, raising=False)
    monkeypatch.setattr(integrate, "RUST_CHECK_SSH", (sys.executable, str(fake_ssh)),
                        raising=False)
    monkeypatch.setattr(integrate, "SUITE_RESULTS", tmp_path / "suite-results", raising=False)
    monkeypatch.setattr(integrate, "SHIM_TESTS_REMOTE", tmp_path / "shim-tests.remote", raising=False)

    class World:
        pass

    w = World()
    w.origin, w.seed, w.work, w.gh = origin, seed, work, fake

    def cargo_calls() -> list[dict]:
        if not cargo_log.is_file():
            return []
        return [json.loads(line) for line in cargo_log.read_text(encoding="utf-8").splitlines()]

    def ssh_calls() -> list[dict]:
        if not ssh_log.is_file():
            return []
        return [json.loads(line) for line in ssh_log.read_text(encoding="utf-8").splitlines()]

    def configure_remote(text: str = VM_CONFIG) -> None:
        remote_file.write_text(text, encoding="utf-8")

    w.cargo_calls, w.ssh_calls, w.configure_remote = cargo_calls, ssh_calls, configure_remote

    def pr(number: int, files: dict[str, str], *, base: str = "origin/master") -> str:
        _git(seed, "fetch", "-q", "origin")
        _git(seed, "switch", "-q", "-C", f"pr-{number}", base)
        for name, text in files.items():
            (seed / name).parent.mkdir(parents=True, exist_ok=True)
            # LF, as repository blobs are (and as the census is written).
            (seed / name).write_text(text, encoding="utf-8", newline="\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", f"change {number}")
        _git(seed, "push", "-q", "-f", "origin", f"HEAD:refs/pull/{number}/head")
        return _git(seed, "rev-parse", "HEAD")

    def advance_master(files: dict[str, str]) -> None:
        _git(seed, "fetch", "-q", "origin")
        _git(seed, "switch", "-q", "-C", "master", "origin/master")
        for name, text in files.items():
            (seed / name).parent.mkdir(parents=True, exist_ok=True)
            # LF, as repository blobs are (and as the census is written).
            (seed / name).write_text(text, encoding="utf-8", newline="\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "master moves")
        _git(seed, "push", "-q", "origin", "master")
        _git(work, "fetch", "-q", "origin")
        _git(work, "reset", "-q", "--hard", "origin/master")

    def remote_branches() -> list[str]:
        out = _git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
        return [b for b in out.splitlines() if b.startswith("integrate/")]

    w.pr, w.advance_master, w.remote_branches = pr, advance_master, remote_branches
    # Master's Python freeze guard: build loads it from the base and refuses
    # without one (ops/integrate.py _freeze_module).
    if guard:
        advance_master({"rust/ci/python_freeze.py": GUARD})
    return w


GUARD = (ROOT / "rust" / "ci" / "python_freeze.py").read_text(encoding="utf-8")


def _build(*extra: str) -> int:
    return integrate.main(["build", "--date", DATE, *extra])


_UNSET = object()


def _suite_result(world, *, commit: str | None = None, exit_code: int = 0,
                  args=(), real_client=True, real_client_commit=None) -> Path:
    """A result as ops/remote-suite.ps1 writes it (with a BOM, as Windows
    PowerShell 5 would); `args=_UNSET` mimics a launcher too old to record
    them, and `real_client=_UNSET` one too old to record the native client.
    The client is built from the tested commit unless `real_client_commit`
    says otherwise."""
    tested = commit or _git(world.work, "rev-parse", "HEAD")
    result = {"machine": "local-wsl", "exit_code": exit_code, "commit": tested,
              "summary": "18868 passed, 81 skipped in 600s"}
    if args is not _UNSET:
        result["args"] = list(args)
    if real_client is not _UNSET:
        result["real_client"] = real_client
        result["real_client_commit"] = (real_client_commit or tested) if real_client else None
        result["real_client_path"] = ("~/.venvs/pseudolife/x/native-client/pseudolife-stdio"
                                      if real_client else None)
    path = world.work.parent / "suite-result.json"
    path.write_text(json.dumps(result), encoding="utf-8-sig")
    return path


def _gate_result(world, gate_kind="rust-harness", **edit) -> Path:
    harness = gate_kind == "rust-harness"
    head = _git(world.work, "rev-parse", "HEAD")
    recipe = integrate._harness_recipe(head) if harness else None
    result = {"kind": gate_kind, "commit": head,
              "command": recipe.display() if harness else integrate.SHIM_TESTS_COMMAND,
              "selection": recipe.record() if harness else list(integrate.SHIM_TESTS_ARGS),
              "machine": "Windows dev VM" if harness else "second Linux dev box",
              "exit_code": 0, "summary": "767 passed in 1.0s", "not_run": None,
              "started": "2026-11-17T01:00:00+00:00", "ended": "2026-11-17T01:00:01+00:00"}
    result.update(edit)
    path = world.work.parent / f"{gate_kind}-result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    return path


def _gate_args(world) -> list[str]:
    return ["--harness", str(_gate_result(world)),
            "--shim-tests", str(_gate_result(world, "shim-tests"))]


def _open(world, **result) -> int:
    return integrate.main(["open", "--suite", str(_suite_result(world, **result)), *_gate_args(world)])


def _publish(world) -> int:
    """build, then open with a passing suite result for the built head."""
    code = _build()
    return code if code else _open(world)


def test_old_builder_plan_opens_with_the_new_batch_script(world):
    # The replay selector landed before admission. The old builder knows
    # neither snapshot fields nor the new opener carried by this member.
    world.advance_master({
        "ops/integrate.py": "from dataclasses import dataclass\n@dataclass\nclass Member:\n    number: int\n",
        "rust/ci/pr_precheck.py": (ROOT / "rust/ci/pr_precheck.py").read_text(encoding="utf-8"),
        "rust/ci/parity_scope.py": (ROOT / "rust/ci/parity_scope.py").read_text(encoding="utf-8"),
    })
    base = _git(world.work, "rev-parse", "HEAD")
    script = (ROOT / "ops/integrate.py").read_text(encoding="utf-8")
    sha = world.pr(10, {"ops/integrate.py": script})
    checks = []
    for index, os in enumerate(("ubuntu-latest", "windows-latest"), 1):
        name = f"Rust / {os}"
        checks.append({"name": name, "status": "IN_PROGRESS", "detailsUrl":
                       f"https://github.example/actions/runs/9/job/{index}"})
        world.gh.actions_jobs[str(index)] = {
            "name": name, "head_sha": sha, "run_id": 9, "run_attempt": 1,
            "status": "in_progress", "conclusion": None,
            "steps": [{"name": "Replay touched PR goldens", "number": 1,
                       "status": "completed", "conclusion": "success"}],
        }
    world.gh.runs["9"] = {"head_sha": sha, "event": "pull_request",
                          "path": ".github/workflows/rust.yml", "run_attempt": 1}
    world.gh.add(10, sha, statusCheckRollup=checks)
    assert integrate._fetch_head(10) == sha
    branch = f"integrate/{DATE}-batch-1"
    _git(world.work, "switch", "-qc", branch)
    _git(world.work, "merge", "--no-ff", "--no-edit", sha)
    plan = integrate.Plan(branch, base, head=_git(world.work, "rev-parse", "HEAD"),
                          rust_check="not needed", merged=[integrate.Member(
                              10, "admission bootstrap", sha, "https://github.example/pr/10", "NONE", "pending")])
    raw = integrate.asdict(plan)
    for field in ("snapshot_required", "snapshot", "snapshot_why"):
        raw["merged"][0].pop(field)
    path = integrate.Plan.path(branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert integrate.Plan.load(branch).merged[0].snapshot_required is None
    assert integrate._has_snapshot_selector(base)
    assert (world.work / "ops/integrate.py").read_text(encoding="utf-8") == script

    assert _open(world) == 0
    saved = integrate.Plan.load(branch)
    assert saved.merged[0].snapshot_required is True
    assert saved.merged[0].snapshot == "satisfied"
    assert _git(world.origin, "rev-parse", branch) == plan.head


def test_plan_load_ignores_unknown_fields_at_every_dataclass_level(tmp_path, monkeypatch):
    member = integrate.Member(10, "member", "b" * 40, "https://github.example/pr/10", "APPROVED", "passing")
    dropped = integrate.Dropped(member, "left out", comment=False)
    plan = integrate.Plan(f"integrate/{DATE}-batch-1", "a" * 40, head="c" * 40,
                          merged=[member], dropped=[dropped])
    raw = integrate.asdict(plan)
    raw["future_plan_field"] = "from a newer builder"
    raw["merged"][0]["future_member_field"] = True
    raw["dropped"][0]["future_drop_field"] = ["new metadata"]
    raw["dropped"][0]["member"]["future_member_field"] = True
    raw["dropped"][0]["waiting"] = True  # The historical census-waiter field.
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(integrate.Plan, "path", staticmethod(lambda branch: path))

    loaded = integrate.Plan.load(plan.branch)

    assert loaded.head == plan.head and loaded.base_sha == plan.base_sha
    assert loaded.merged[0].number == member.number and loaded.merged[0].sha == member.sha
    assert loaded.dropped[0].member.sha == member.sha
    assert loaded.dropped[0].reason == dropped.reason and loaded.dropped[0].comment is False
    assert not hasattr(loaded, "future_plan_field")
    assert not hasattr(loaded.merged[0], "future_member_field")
    assert not hasattr(loaded.dropped[0], "future_drop_field")


def _second_parents(origin: Path, branch: str) -> list[str]:
    """The heads merged into `branch`, in merge order (origin is bare)."""
    log = _git(origin, "log", "--first-parent", "--format=%P", f"master..{branch}")
    # A generated census commit merges nothing, so it has no second parent.
    return [line.split()[1] for line in reversed(log.splitlines()) if " " in line]


def test_build_merges_members_in_label_order_and_opens_the_batch_pr(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10, labeled_at="2026-10-19T09:00:00Z")
    world.gh.add(11, sha11, labeled_at="2026-10-19T08:00:00Z")

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert world.remote_branches() == [branch]
    _git(world.origin, "rev-parse", "--verify", branch)
    assert _second_parents(world.origin, branch) == [sha11, sha10]
    (args, body), = world.gh.bodies("create")
    assert args[args.index("--title") + 1] == f"integrate: {DATE} batch 1 (#11, #10)"
    assert args[args.index("--head") + 1] == branch
    assert args[args.index("--base") + 1] == "master"
    for sha, number in ((sha10, 10), (sha11, 11)):
        assert sha in body and f"https://github.example/pr/{number}" in body
    assert "Local full suite:" in body
    assert "merge commit" in body
    assert world.gh.bodies("comment") == []


def test_unknown_label_times_fall_back_to_pr_number_order(world, capsys):
    sha12 = world.pr(12, {"d.txt": "d\n"})
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(12, sha12)
    world.gh.add(11, sha11)
    world.gh.add(10, sha10)

    assert _publish(world) == 0, capsys.readouterr()

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha11, sha12]


def test_a_member_colliding_with_another_is_dropped_with_a_comment(world, capsys):
    sha10 = world.pr(10, {"a.txt": "one\nTEN\nthree\n"})
    sha11 = world.pr(11, {"a.txt": "one\nELEVEN\nthree\n"})
    sha12 = world.pr(12, {"d.txt": "d\n"})
    for n, sha in ((10, sha10), (11, sha11), (12, sha12)):
        world.gh.add(n, sha)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == [sha10, sha12]
    (args, body), = world.gh.bodies("comment")
    assert args[2] == "11"
    assert "conflicts with #10 in a.txt" in body and branch in body
    assert "master itself" not in body
    (edit_args, _), = world.gh.bodies("edit")
    assert edit_args[2] == "11" and edit_args[edit_args.index("--remove-label") + 1] == LABEL
    (create_args, create_body), = world.gh.bodies("create")
    assert create_args[create_args.index("--title") + 1] == f"integrate: {DATE} batch 1 (#10, #12)"
    assert "#11" in create_body  # listed as dropped
    # The checkout is left clean: the conflicted merge was aborted.
    assert _git(world.work, "status", "--porcelain", "--untracked-files=no") == ""
    assert subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          cwd=world.work, capture_output=True).returncode != 0


def test_a_member_conflicting_with_master_itself_is_told_so(world, capsys):
    sha10 = world.pr(10, {"a.txt": "one\nTEN\nthree\n"})
    world.advance_master({"a.txt": "one\nMASTER\nthree\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    sha12 = world.pr(12, {"d.txt": "d\n"})
    for n, sha in ((10, sha10), (11, sha11), (12, sha12)):
        world.gh.add(n, sha)

    assert _publish(world) == 0, capsys.readouterr()

    (args, body), = world.gh.bodies("comment")
    assert args[2] == "10"
    assert "conflicts with master itself in a.txt" in body
    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha11, sha12]


def test_dry_run_pushes_and_comments_nothing_and_restores_the_checkout(world, capsys):
    sha10 = world.pr(10, {"a.txt": "one\nTEN\nthree\n"})
    sha11 = world.pr(11, {"a.txt": "one\nELEVEN\nthree\n"})
    sha12 = world.pr(12, {"d.txt": "d\n"})
    for n, sha in ((10, sha10), (11, sha11), (12, sha12)):
        world.gh.add(n, sha)
    before = _git(world.work, "symbolic-ref", "-q", "HEAD")

    assert _build("--dry-run") == 0
    out = capsys.readouterr().out

    assert world.remote_branches() == []
    assert world.gh.mutations() == []
    assert f"integrate: {DATE} batch 1 (#10, #12)" in out
    assert "#11" in out and "#10" in out
    assert _git(world.work, "symbolic-ref", "-q", "HEAD") == before
    assert _git(world.work, "branch", "--list", "integrate/*") == ""
    assert _git(world.work, "rev-parse", "HEAD") == _git(world.work, "rev-parse", "origin/master")


def test_refuses_with_uncommitted_tracked_changes(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    (world.work / "README.md").write_text("edited\n", encoding="utf-8")

    assert _build() == 2
    err = capsys.readouterr().err
    assert "uncommitted" in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_refuses_when_head_is_not_origin_master(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    (world.work / "local.txt").write_text("x\n", encoding="utf-8")
    _git(world.work, "add", "local.txt")
    _git(world.work, "commit", "-q", "-m", "local work")

    assert _build() == 2
    err = capsys.readouterr().err
    assert "origin/master" in err and "git switch --detach origin/master" in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_refuses_while_another_batch_pr_is_open(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    world.gh.history = [{"number": 880, "headRefName": "integrate/2026-10-18-batch-1",
                         "state": "OPEN"}]

    assert _build() == 2
    assert "#880" in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_the_batch_number_skips_every_used_one(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    # batch-1 still exists on origin; batch-2 merged and its branch was deleted.
    _git(world.seed, "push", "-q", "origin", f"origin/master:refs/heads/integrate/{DATE}-batch-1")
    world.gh.history = [{"number": 870, "headRefName": f"integrate/{DATE}-batch-2",
                         "state": "MERGED"},
                        {"number": 871, "headRefName": f"integrate/{DATE}-batch-1",
                         "state": "CLOSED"},
                        {"number": 872, "headRefName": "integrate/2026-10-18-batch-7",
                         "state": "MERGED"}]

    assert _publish(world) == 0, capsys.readouterr()

    assert f"integrate/{DATE}-batch-3" in world.remote_branches()


def test_a_head_that_moved_after_listing_is_skipped_not_merged(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    sha12 = world.pr(12, {"d.txt": "d\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    world.gh.add(12, sha12)
    world.pr(11, {"c.txt": "c, revised\n"})  # pushed after the listing

    assert _publish(world) == 0
    out = capsys.readouterr().out

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha12]
    assert "#11" in out and "moved" in out
    assert world.gh.bodies("comment") == []


def test_a_lone_clean_member_is_a_batch_of_one(world, capsys):
    # A lone ready PR's own CI ran only the fast lanes, so it gets its full
    # CI as a batch of one rather than merging directly.
    sha10 = world.pr(10, {"a.txt": "one\nTEN\nthree\n"})
    sha11 = world.pr(11, {"a.txt": "one\nELEVEN\nthree\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert world.remote_branches() == [branch]
    assert _second_parents(world.origin, branch) == [sha10]
    (args, _), = world.gh.bodies("create")
    assert args[args.index("--title") + 1] == f"integrate: {DATE} batch 1 (#10)"
    (args, _), = world.gh.bodies("comment")  # the collision is still reported
    assert args[2] == "11"


def test_no_clean_member_builds_nothing_and_reports_at_once(world, capsys):
    sha10 = world.pr(10, {"a.txt": "one\nTEN\nthree\n"})
    world.advance_master({"a.txt": "one\nMASTER\nthree\n"})
    world.gh.add(10, sha10)
    before = _git(world.work, "symbolic-ref", "-q", "HEAD")

    assert _build() == 0
    out = capsys.readouterr().out

    assert world.remote_branches() == [] and world.gh.bodies("create") == []
    assert "no batch built" in out
    (args, _), = world.gh.bodies("comment")
    assert args[2] == "10"
    assert _git(world.work, "symbolic-ref", "-q", "HEAD") == before
    assert _git(world.work, "branch", "--list", "integrate/*") == ""


def _cut(version: str) -> str:
    return f'[project]\nname = "x"\nversion = "{version}"\n'


@pytest.mark.parametrize("cut_first", [True, False])
def test_a_release_cut_is_batched_alone(world, capsys, cut_first):
    # The release is tagged at the batch's merge, so anything beside the cut
    # would ship without being in its notes; the others keep their labels
    # and join the next batch.
    world.advance_master({"pyproject.toml": _cut("0.19.0")})
    sha10 = world.pr(10, {"pyproject.toml": _cut("0.19.1")})
    sha11 = world.pr(11, {"b.txt": "b\n"})
    world.gh.add(10, sha10, labeled_at="2026-10-19T09:00:00Z" if cut_first else
                 "2026-10-19T10:00:00Z")
    world.gh.add(11, sha11, labeled_at="2026-10-19T09:30:00Z")

    assert _publish(world) == 0, capsys.readouterr()
    out = capsys.readouterr().out

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10]
    assert "#11" in out and "release cut" in out
    assert "release notes" in out and "oracle" not in out and "parity" not in out
    assert world.gh.bodies("comment") == [] and world.gh.bodies("edit") == []


def _fold_cut(world, number: int, version: str, *, fold: tuple[str, ...] = (),
              add: dict[str, str] | None = None) -> str:
    """A version cut on master's tip: pyproject bumped, `fold` fragments
    deleted as ops/assemble_changelog.py would, `add` files written."""
    seed = world.gh.seed
    _git(seed, "fetch", "-q", "origin")
    _git(seed, "switch", "-q", "-C", f"pr-{number}", "origin/master")
    (seed / "pyproject.toml").write_text(_cut(version), encoding="utf-8", newline="\n")
    for name in fold:
        _git(seed, "rm", "-q", name)
    for name, text in (add or {}).items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(text, encoding="utf-8", newline="\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", f"cut {version}")
    _git(seed, "push", "-q", "-f", "origin", f"HEAD:refs/pull/{number}/head")
    return _git(seed, "rev-parse", "HEAD")


FRAGMENT_A = "changelog.d/2026-11-14-a.md"
FRAGMENT_B = "changelog.d/2026-11-14-b.md"


def test_a_cut_that_folded_every_fragment_on_master_joins(world, capsys):
    world.advance_master({"pyproject.toml": _cut("0.19.0"), FRAGMENT_A: "### Added (a)\n",
                          "changelog.d/README.md": "readme\n"})
    sha = _fold_cut(world, 10, "0.19.1", fold=(FRAGMENT_A,))
    world.gh.add(10, sha)

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " in out and "unfolded" not in out


@pytest.mark.parametrize("late, adds", [(True, False), (False, True)],
                         ids=["fragment-landed-after-the-fold", "cut-adds-a-fragment"])
def test_a_cut_leaving_a_fragment_unfolded_is_left_out_and_the_rest_batch(world, capsys, late, adds):
    # The release would ship a change its notes do not describe.
    world.advance_master({"pyproject.toml": _cut("0.19.0"), FRAGMENT_A: "### Added (a)\n"})
    sha10 = _fold_cut(world, 10, "0.19.1", fold=(FRAGMENT_A,),
                      add={FRAGMENT_B: "### Added (b)\n"} if adds else None)
    if late:
        world.advance_master({FRAGMENT_B: "### Added (b)\n"})
    sha11 = world.pr(11, {"b.txt": "b\n"})
    world.gh.add(10, sha10, labeled_at="2026-10-19T09:00:00Z")
    world.gh.add(11, sha11, labeled_at="2026-10-19T09:30:00Z")

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " not in out and "merged #11 " in out, out
    assert FRAGMENT_B in out and "assemble_changelog" in out


def test_open_refuses_when_a_members_own_ci_went_red_after_build(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[{"status": "IN_PROGRESS", "conclusion": None}])
    assert _build() == 0
    capsys.readouterr()
    world.gh.ready[1]["statusCheckRollup"] = [{"status": "COMPLETED", "conclusion": "FAILURE"}]

    assert _open(world) == 2

    err = capsys.readouterr().err
    assert "#11" in err and "failing" in err and "build again" in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_open_takes_a_member_whose_own_ci_was_cancelled_after_build(world, capsys):
    # The near miss of the test above: a cancelled run is no known failure.
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[{"status": "IN_PROGRESS", "conclusion": None}])
    assert _build() == 0
    capsys.readouterr()
    world.gh.ready[1]["statusCheckRollup"] = [{"status": "COMPLETED", "conclusion": "CANCELLED"}]

    assert _open(world) == 0, capsys.readouterr()

    assert world.remote_branches() == [f"integrate/{DATE}-batch-1"]


def _cancelled_job(job: str) -> dict:
    """A cancelled Actions job as `gh pr list --json statusCheckRollup` lists it."""
    return {"__typename": "CheckRun", "name": "Rust / windows-latest", "workflowName": "Rust shim",
            "status": "COMPLETED", "conclusion": "CANCELLED",
            "detailsUrl": f"https://github.com/o/r/actions/runs/7/job/{job}"}


# GitHub reports a job that ran past its timeout-minutes as CANCELLED, with
# this runner annotation (jobs 116301727515 and 116323387861, 2026-10-15).
TIMEOUT = "The job has exceeded the maximum execution time of 25m0s"


FAILING = "left out #11: CI on its head is failing"
# The unverified reason names the read that failed, then this.
UNVERIFIED = ("so a timeout or a failure is not ruled out; it joins a batch built "
              "once that read succeeds")


@pytest.mark.parametrize("messages, outcome", [
    ([TIMEOUT, "The operation was canceled."], FAILING),   # a hang: a known failure
    # The wording GitHub's runners used before (2023-24 public runs).
    (["The job running on runner GitHub Actions 7 has exceeded the maximum execution "
      "time of 10 minutes."], FAILING),
    (None, UNVERIFIED),                                      # annotations unreadable
    (["The operation was canceled."], None),                 # cancelled by hand
    ([], None),
    (["Note: The job has exceeded the maximum execution time of 25m0s"], None),
    (["step log:\nThe job has exceeded the maximum execution time of 25m0s"], None),
], ids=["timed-out", "older-wording", "unreadable", "cancelled", "no-annotations",
        "quoted-near-miss", "multi-line-near-miss"])
def test_a_cancelled_job_joins_unless_it_ran_past_its_timeout(world, capsys, messages, outcome):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[
        {"status": "COMPLETED", "conclusion": "SUCCESS"}, _cancelled_job("123")])
    if messages is not None:
        world.gh.annotations["123"] = messages

    assert _build() == 0, capsys.readouterr()
    out = capsys.readouterr().out

    for message in (FAILING, UNVERIFIED):
        assert (message in out) is (message == outcome), out
    if outcome == UNVERIFIED:
        # The true cause: which read failed, and how.
        assert ("left out #11: the annotations of job 123 could not be read "
                "(`gh api` failed (1): HTTP 502), so") in out, out


@pytest.mark.parametrize("details_url", ["https://ci.example/build/1", None,
                                         "https://github.com/o/r/actions/runs/7/job/"])
def test_a_cancelled_check_that_is_no_actions_job_joins_unprobed(world, capsys, details_url):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[
        dict(_cancelled_job("123"), detailsUrl=details_url)])

    assert _publish(world) == 0, capsys.readouterr()

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha11]
    assert not [args for args, _ in world.gh.calls if "/check-runs/" in " ".join(args)]


def test_a_failure_beside_a_cancelled_job_needs_no_annotations(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[
        _cancelled_job("123"), {"status": "COMPLETED", "conclusion": "FAILURE"}])

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out
    assert not [args for args, _ in world.gh.calls if "/check-runs/" in " ".join(args)]


def test_a_timed_out_attempt_a_rerun_superseded_does_not_keep_a_member_out(world, capsys):
    # gh can list an older attempt beside the rerun that replaced it; only the
    # latest attempt of a check counts, as in `merge`.
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    timed_out = dict(_cancelled_job("123"), startedAt="2026-10-29T01:00:00Z",
                     completedAt="2026-10-29T01:45:00Z")
    rerun = dict(_cancelled_job("456"), conclusion="SUCCESS", startedAt="2026-10-29T02:00:00Z",
                 completedAt="2026-10-29T02:20:00Z")
    world.gh.add(11, sha11, statusCheckRollup=[timed_out, rerun])
    world.gh.annotations["123"] = [TIMEOUT]

    assert _publish(world) == 0, capsys.readouterr()

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha11]


@pytest.mark.parametrize("suffix", ["?pr=11", "#step:4:1"])
def test_a_job_url_with_a_query_or_fragment_is_still_read(world, capsys, suffix):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    job = _cancelled_job("123")
    job["detailsUrl"] += suffix
    world.gh.add(11, sha11, statusCheckRollup=[job])
    world.gh.annotations["123"] = [TIMEOUT]

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


def test_a_rerun_that_timed_out_after_an_earlier_pass_keeps_a_member_out(world, capsys):
    # The reverse of the superseded case: the latest attempt is the timeout.
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    passed = dict(_cancelled_job("456"), conclusion="SUCCESS", startedAt="2026-10-29T01:00:00Z",
                  completedAt="2026-10-29T01:20:00Z")
    timed_out = dict(_cancelled_job("123"), startedAt="2026-10-29T02:00:00Z",
                     completedAt="2026-10-29T02:45:00Z")
    world.gh.add(11, sha11, statusCheckRollup=[timed_out, passed])
    world.gh.annotations["123"] = [TIMEOUT]

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


def _freeze_runs(*runs: tuple[str, str]) -> list[dict]:
    """Python freeze runs on one head, one per event: (conclusion, start)."""
    return [_run("Python freeze", None if conclusion == "IN_PROGRESS" else conclusion,
                 status="IN_PROGRESS" if conclusion == "IN_PROGRESS" else "COMPLETED",
                 started=f"2026-11-14T01:{minute}:00Z" if minute else "", workflow="Python freeze")
            for conclusion, minute in runs]


def test_entries_with_no_check_name_never_replace_each_others_failure(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[
        {"status": "COMPLETED", "conclusion": "FAILURE", "startedAt": "2026-11-14T01:00:00Z"},
        {"status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": "2026-11-14T01:05:00Z"}])

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


# #1537 at a5629d8fd: the push's run failed before the conductor added
# `freeze-exception`, two runs were cancelled, and the label event's run
# passed. gh pr checks shows only the last; build read every run.
LABELLED_AFTER_OPENING = (("FAILURE", "00"), ("CANCELLED", "01"), ("CANCELLED", "02"), ("SUCCESS", "03"))


def test_a_check_is_judged_by_its_latest_run_on_the_head(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=_freeze_runs(*LABELLED_AFTER_OPENING))

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #11 " in out and FAILING not in out


@pytest.mark.parametrize("runs", [
    (("SUCCESS", "00"), ("FAILURE", "05")),
    # A rerun of the pre-label run replays an event without the label: when
    # it fails again it is the latest run, and the head stays out.
    (*LABELLED_AFTER_OPENING, ("FAILURE", "10")),
    # Codex review of 2204f4382: an older run still going hides nothing.
    (("IN_PROGRESS", "00"), ("FAILURE", "05")),
    (("FAILURE", "05"), ("IN_PROGRESS", "00")),
    # A later run with no verdict replaces no failure.
    (("FAILURE", "00"), ("CANCELLED", "05")),
    (("FAILURE", "00"), ("SKIPPED", "05")),
    # Reviewer of 2204f4382: a tie or a missing start time replaces nothing.
    (("FAILURE", "05"), ("SUCCESS", "05")),
    (("FAILURE", ""), ("SUCCESS", "05")),
], ids=["newer-failure", "failed-rerun-of-the-old-run", "older-run-still-going", "listed-in-reverse",
        "later-cancelled", "later-skipped", "same-start", "failure-without-a-start"])
def test_a_newer_failure_still_outweighs_an_older_success(world, capsys, runs):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=_freeze_runs(*runs))

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert FAILING in out and "(Python freeze)" in out and "merged #11 " not in out


@pytest.mark.parametrize("runs, opens", [
    (LABELLED_AFTER_OPENING, True),
    ((*LABELLED_AFTER_OPENING, ("FAILURE", "10")), False),
], ids=["latest-passed", "latest-failed"])
def test_open_judges_a_members_checks_by_their_latest_run(world, capsys, runs, opens):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    assert _build() == 0
    capsys.readouterr()
    world.gh.ready[1]["statusCheckRollup"] = _freeze_runs(*runs)

    code = _open(world)

    err = capsys.readouterr().err
    if opens:
        assert code == 0, err
    else:
        assert code == 2 and "#11's own CI is now failing" in err
        assert world.remote_branches() == []


def test_open_refuses_a_member_whose_own_job_timed_out_after_build(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[{"status": "IN_PROGRESS", "conclusion": None}])
    assert _build() == 0
    capsys.readouterr()
    world.gh.ready[1]["statusCheckRollup"] = [_cancelled_job("123")]
    world.gh.annotations["123"] = [TIMEOUT]

    assert _open(world) == 2

    err = capsys.readouterr().err
    assert "#11" in err and "failing" in err
    assert world.remote_branches() == []


def test_open_refuses_without_dropping_when_annotations_cannot_be_read(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[{"status": "IN_PROGRESS", "conclusion": None}])
    assert _build() == 0
    capsys.readouterr()
    world.gh.ready[1]["statusCheckRollup"] = [_cancelled_job("123")]   # unreadable

    assert _open(world) == 2

    err = capsys.readouterr().err
    assert "#11" in err and "could not be read" in err and "run `open` again" in err
    assert "failing" not in err and "Drop it" not in err
    assert world.remote_branches() == []


# PR #1059's head 3dda95a1 (2026-10-30): its Rust run (40992819326) was
# cancelled after the `Rust / <os>` jobs started, and the `Parity / <os>` gate,
# which runs under `always()`, still ran and failed only because the jobs it
# needs were cancelled (job 123859514294). The run's jobs, as the Actions API
# lists them: (id, name, conclusion), omitting the retired source-mutant job
# so the fixture still describes the current workflow's gate dependencies.
RUN, ATTEMPT, HEAD = "7", "1", "3dda95a1"
_RUN_1059 = [
    ("500", "Scope", "success"),
    ("501", "Rust / ubuntu-latest", "cancelled"),
    ("502", "Rust / windows-latest", "cancelled"),
    ("503", "Rust daemon / disposable image", "cancelled"),
    ("504", "Candidate / ${{ matrix.os }}", "skipped"),
    ("505", "Parity checks / ${{ matrix.os }} / ${{ matrix.suite }}", "skipped"),
    ("507", "Stress report", "skipped"),
    ("508", "Parity / windows-latest", "failure"),
    ("509", "Parity / ubuntu-latest", "failure"),
]
GATES = ("508", "509")


def _actions_check(job: str, name: str, conclusion: str, workflow: str = "Rust shim") -> dict:
    """An Actions job as `gh pr list --json statusCheckRollup` lists it."""
    return {"__typename": "CheckRun", "name": name, "workflowName": workflow,
            "status": "COMPLETED", "conclusion": conclusion.upper(),
            "detailsUrl": f"https://github.com/o/r/actions/runs/{RUN}/job/{job}"}


# The Parity gate's steps when a job it needs was cancelled: its dedicated
# step fails and its verdict step skips (rust.yml since #1068).
CANCEL_STEP = "A needed job was cancelled"
VERDICT_STEP = "Require every Rust and parity job to pass"
GATE_STEPS = [("Set up job", "success"), (CANCEL_STEP, "failure"),
              (VERDICT_STEP, "skipped"), ("Complete job", "success")]
WORKFLOW = (ROOT / ".github" / "workflows" / "rust.yml").read_text(encoding="utf-8")
CANCEL_BLOCK = ("      - name: A needed job was cancelled\n"
                "        if: ${{ contains(needs.*.result, 'cancelled') }}\n"
                "        run: exit 1\n")
VERDICT_IF = "        if: ${{ !contains(needs.*.result, 'cancelled') }}\n"
assert WORKFLOW.count(CANCEL_BLOCK) == 1 and WORKFLOW.count(VERDICT_IF) == 1
# The gate as heads before #1068 carry it (#1059's 3dda95a1): no dedicated
# step, so a cancel fails the verdict step, as it did on job 123859514294.
OLD_WORKFLOW = WORKFLOW.replace(CANCEL_BLOCK, "").replace(VERDICT_IF, "")
OLD_GATE_STEPS = [("Set up job", "success"), (VERDICT_STEP, "failure"),
                  ("Complete job", "success")]


def _steps(steps) -> list[dict]:
    return [{"number": n, "name": name, "status": "completed", "conclusion": conclusion}
            for n, (name, conclusion) in enumerate(steps, 1)]


def _gate_world(world, run=_RUN_1059, rollup: list[str] | None = None,
                gate_steps=GATE_STEPS, head_workflow: str = WORKFLOW, checkout_workflow: str = WORKFLOW):
    """PR #11 whose head carries `run` (by default #1059's jobs), listed in
    its rollup (the given job ids, else every job that did not skip), with
    `head_workflow` (the real rust.yml) as that head's workflow, the real
    rust.yml in the checkout `build` runs from (untracked), the failed
    gates' steps `gate_steps`, and every job's annotations empty (none ran
    past its timeout)."""
    workflows = world.work / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "rust.yml").write_text(checkout_workflow, encoding="utf-8")
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    jobs = {job: (name, conclusion) for job, name, conclusion in run}
    ids = rollup if rollup is not None else [j for j, (_, c) in jobs.items() if c != "skipped"]
    world.gh.add(11, sha11, statusCheckRollup=[_actions_check(j, *jobs[j]) for j in ids])
    for job, (name, conclusion) in jobs.items():
        world.gh.actions_jobs[job] = {"id": int(job), "name": name, "conclusion": conclusion,
                                      "status": "completed", "run_id": int(RUN),
                                      "run_attempt": int(ATTEMPT), "head_sha": HEAD,
                                      "steps": _steps(
                                          gate_steps if job in GATES and conclusion == "failure"
                                          else [("Set up job", "success")])}
        world.gh.annotations[job] = []
    world.gh.runs[RUN] = {"id": int(RUN), "path": ".github/workflows/rust.yml",
                          "head_sha": HEAD, "run_attempt": int(ATTEMPT)}
    world.gh.attempt_jobs[(RUN, ATTEMPT)] = [
        {"id": int(job), "name": name, "status": "completed", "conclusion": conclusion}
        for job, (name, conclusion) in jobs.items()]
    world.gh.files[(".github/workflows/rust.yml", HEAD)] = head_workflow
    return sha10, sha11


def _with(run, **conclusions) -> list[tuple[str, str, str]]:
    """`run` with some jobs' conclusions replaced (by job id, as `j<id>`)."""
    return [(job, name, conclusions.get(f"j{job}", conclusion)) for job, name, conclusion in run]


def test_a_gate_that_failed_only_because_its_needed_jobs_were_cancelled_joins(world, capsys):
    # #1059: `build` refused it as failing although nothing on its head failed.
    sha10, sha11 = _gate_world(world)

    assert _publish(world) == 0, capsys.readouterr()

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha11]
    # Every cancelled job the gates need was read for a timeout.
    probed = {args[1].split("/")[-2] for args, _ in world.gh.calls
              if "/check-runs/" in args[1]}
    assert {"501", "502"} <= probed
    # `merge` is unchanged: on a batch the same checks are red.
    red, _ = integrate._merge_checks(world.gh.pr(11)["statusCheckRollup"])
    assert {"Parity / ubuntu-latest (FAILURE)", "Parity / windows-latest (FAILURE)"} <= set(red)


def test_the_gates_of_one_run_read_the_run_once(world, capsys):
    _gate_world(world)

    assert _build() == 0, capsys.readouterr()

    reads = [args for args, _ in world.gh.calls if "/contents/" in " ".join(args)
             or "/attempts/" in " ".join(args)]
    assert len(reads) == 2, reads


@pytest.mark.parametrize("run, rollup", [
    # A needed shard failed: a real failure, though the gate's rollup row is
    # all that names it here (and a cancel stopped the other shard).
    (_with(_RUN_1059, j502="failure"), list(GATES)),
    (_with(_RUN_1059, j502="timed_out"), list(GATES)),
    # Every needed job passed and only one the gate does not need was
    # cancelled: the gate failed on its own.
    (_with(_RUN_1059, j501="success", j502="success"), None),
    # A needed job never finished: nothing proves the cause.
    (_with(_RUN_1059, j502=None), list(GATES)),
], ids=["needed-failed", "needed-timed-out", "needs-passed", "needed-unfinished"])
def test_a_gate_failure_with_another_cause_keeps_a_member_out(world, capsys, run, rollup):
    _gate_world(world, run, rollup)

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


@pytest.mark.parametrize("run", [
    # `Rust / <os>` needs only `Scope`, which passed.
    _with(_RUN_1059, j501="failure", j508="success", j509="success"),
    # A `!cancelled()` job, no gate, beside a cancelled job it needs: only
    # `always()` lets a job run on after a cancel, so this failure is its own.
    _with(_RUN_1059, j501="success", j502="success", j508="success", j509="success")
    + [("510", "Fixtures / ubuntu-latest", "cancelled"),
       ("511", "Parity checks / ubuntu-latest / cli-store", "failure")],
], ids=["needs-passed", "need-cancelled"])
def test_a_non_gate_failure_keeps_a_member_out(world, capsys, run):
    _gate_world(world, run)

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


def test_a_gate_whose_needed_job_ran_past_its_timeout_keeps_a_member_out(world, capsys):
    # GitHub reports the timed-out shard as cancelled; its annotation says why.
    _gate_world(world, rollup=list(GATES))
    world.gh.annotations["502"] = [TIMEOUT]

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


@pytest.mark.parametrize("unreadable, what", [
    ("job", "job 509"),
    ("run", "run 7"),
    ("workflow", ".github/workflows/rust.yml at 3dda95a1"),
    ("attempt", "the jobs of run 7 attempt 1"),
    ("annotations", "the annotations of job 502"),
], ids=["job", "run", "workflow", "attempt", "annotations"])
def test_a_gate_whose_run_cannot_be_read_is_unverified(world, capsys, unreadable, what):
    _gate_world(world, rollup=list(GATES))
    if unreadable == "job":
        del world.gh.actions_jobs["509"]
    elif unreadable == "run":
        del world.gh.runs[RUN]
    elif unreadable == "workflow":
        world.gh.files.clear()
    elif unreadable == "attempt":
        world.gh.attempt_jobs.clear()
    else:
        del world.gh.annotations["502"]

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert UNVERIFIED in out and FAILING not in out, out
    # The read that failed and GitHub's answer, not a generic "retry".
    assert (f"left out #11: {what} could not be read (`gh api` failed (1): HTTP 502), "
            f"{UNVERIFIED}") in out, out


def test_open_refuses_without_dropping_when_a_gates_run_cannot_be_read(world, capsys):
    _gate_world(world, rollup=list(GATES))
    assert _build() == 0, capsys.readouterr()
    capsys.readouterr()
    world.gh.attempt_jobs.clear()

    assert _open(world) == 2

    err = capsys.readouterr().err
    assert "#11" in err and "the jobs of run 7 attempt 1 could not be read" in err
    assert "run `open` again" in err and "Drop it" not in err
    assert world.remote_branches() == []


@pytest.mark.parametrize("steps", [
    # A head that breaks the gate before its cancelled-need step.
    [("Set up job", "failure"), (CANCEL_STEP, "skipped"), (VERDICT_STEP, "skipped"),
     ("Complete job", "success")],
    # The verdict step failed (its own script, say: Codex's repro was a
    # `throw` there beside a cancelled need), not the cancelled-need step.
    [("Set up job", "success"), (CANCEL_STEP, "skipped"), (VERDICT_STEP, "failure"),
     ("Complete job", "success")],
    # Both failed.
    [("Set up job", "success"), (CANCEL_STEP, "failure"), (VERDICT_STEP, "failure"),
     ("Complete job", "success")],
    # The cancelled-need step failed, and so did a later one.
    [("Set up job", "success"), (CANCEL_STEP, "failure"), (VERDICT_STEP, "skipped"),
     ("Complete job", "failure")],
    # A failed step of another name.
    [("Set up job", "success"), ("Run exit 1", "failure"), (VERDICT_STEP, "skipped"),
     ("Complete job", "success")],
    # No step failed: the job's failure is not its cancelled-need step.
    [("Set up job", "success"), (CANCEL_STEP, "skipped"), (VERDICT_STEP, "success"),
     ("Complete job", "success")],
], ids=["setup-failed", "verdict-failed", "both-failed", "later-step-failed",
        "other-step", "no-step-failed"])
def test_a_gate_that_failed_outside_its_cancelled_need_step_keeps_a_member_out(
        world, capsys, steps):
    _gate_world(world, rollup=list(GATES), gate_steps=steps)

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


def test_a_head_whose_gate_has_no_cancelled_need_step_keeps_a_member_out(world, capsys):
    # #1059's own head predates the step: its gate failed at the verdict,
    # which a broken verdict script would do as well, so it stays failing.
    _gate_world(world, rollup=list(GATES), gate_steps=OLD_GATE_STEPS,
                head_workflow=OLD_WORKFLOW)

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


@pytest.mark.parametrize("change, admitted", [
    # Keys that change what the step does.
    (("        run: exit 1\n", "        run: exit 1\n        shell: bash\n"), False),
    (("        run: exit 1\n", "        run: exit 1\n        continue-on-error: false\n"), False),
    (("        run: exit 1\n", "        run: exit 1\n        env:\n          X: y\n"), False),
    (("        run: exit 1\n", "        run: exit 1 || true\n"), False),
    (("cancelled') }}\n        run", "cancelled') || always() }}\n        run"), False),
    (("      - name: A needed job was cancelled\n", "      -\n"), False),
    # An `id` changes nothing it does.
    (("        run: exit 1\n", "        run: exit 1\n        id: cancelled\n"), True),
], ids=["shell", "continue-on-error", "env", "run", "if", "unnamed", "id"])
def test_only_the_exact_cancelled_need_step_is_read_as_a_cancel(world, capsys, change, admitted):
    old, new = change
    assert WORKFLOW.count(old) >= 1
    block = CANCEL_BLOCK.replace(old, new)
    assert block != CANCEL_BLOCK, change
    _gate_world(world, rollup=list(GATES), head_workflow=WORKFLOW.replace(CANCEL_BLOCK, block))

    assert _build() == 0, capsys.readouterr()

    assert (FAILING not in capsys.readouterr().out) is admitted


@pytest.mark.parametrize("steps", [None, []], ids=["no-steps", "empty-steps"])
def test_a_gate_whose_steps_cannot_be_read_is_unverified(world, capsys, steps):
    _gate_world(world, rollup=list(GATES))
    for job in GATES:
        if steps is None:
            del world.gh.actions_jobs[job]["steps"]
        else:
            world.gh.actions_jobs[job]["steps"] = steps

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert UNVERIFIED in out and FAILING not in out, out
    assert "left out #11: the steps of job 508 could not be read (none listed), " in out, out


@pytest.mark.parametrize("run_conclusion, run_attempt, admitted", [
    # #1074's 7cb16da6: cancelling the run while its gate job was starting
    # left the gate `failure` with no steps; the attempt itself concluded
    # `cancelled`, so the gate never ran a verdict.
    ("cancelled", ATTEMPT, True),
    # The same record in an attempt that did not conclude cancelled proves
    # no cancel reached the gate.
    ("failure", ATTEMPT, False),
    (None, ATTEMPT, False),
    # The run's record describes a later attempt, not this one.
    ("cancelled", str(int(ATTEMPT) + 1), False),
])
def test_a_gate_with_no_steps_in_a_cancelled_attempt_carries_no_verdict(
        world, capsys, run_conclusion, run_attempt, admitted):
    _gate_world(world, rollup=list(GATES))
    for job in GATES:
        world.gh.actions_jobs[job]["steps"] = []
    world.gh.runs[RUN].update(conclusion=run_conclusion, run_attempt=int(run_attempt))

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert FAILING not in out, out
    assert (UNVERIFIED not in out) is admitted, out
    assert ("merged #11 " in out) is admitted, out


@pytest.mark.parametrize("sibling", [
    [("Set up job", "failure")],
    OLD_GATE_STEPS,
    [("Set up job", "success"), (CANCEL_STEP, "skipped"), (VERDICT_STEP, "failure"),
     ("Complete job", "success")],
], ids=["setup-failed", "old-gate", "verdict-failed"])
def test_a_stepless_leg_does_not_excuse_a_sibling_leg_that_failed(world, capsys, sibling):
    _gate_world(world, rollup=list(GATES))
    world.gh.actions_jobs["508"]["steps"] = []
    world.gh.actions_jobs["509"]["steps"] = _steps(sibling)
    world.gh.runs[RUN]["conclusion"] = "cancelled"

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert FAILING in out and "merged #11 " not in out, out


@pytest.mark.parametrize("conclusion", ["timed_out", "action_required"])
def test_only_a_failed_stepless_leg_gives_no_verdict(world, capsys, conclusion):
    # A sibling leg the rollup omits, stepless but timed out or needing
    # action, is no cancel and must not be excused (Codex review of 758b7806).
    _gate_world(world, rollup=["508"])
    world.gh.actions_jobs["509"].update(conclusion=conclusion, steps=[])
    for job in world.gh.attempt_jobs[(RUN, ATTEMPT)]:
        if job["id"] == 509:
            job["conclusion"] = conclusion
    world.gh.runs[RUN]["conclusion"] = "cancelled"

    assert _build() == 0, capsys.readouterr()

    assert "merged #11 " not in capsys.readouterr().out


def test_a_stepless_gate_with_no_cancelled_need_is_unverified(world, capsys):
    # The cancel landed after every job the gate needs had passed: nothing
    # failed, but the gate gave no verdict, so it is not admitted either.
    _gate_world(world, run=_with(_RUN_1059, j501="success", j502="success", j503="success"),
                rollup=list(GATES))
    for job in GATES:
        world.gh.actions_jobs[job]["steps"] = []
    world.gh.runs[RUN]["conclusion"] = "cancelled"

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert FAILING not in out and "merged #11 " not in out, out
    assert "gate job 508 was cancelled before it ran a step" in out, out


def test_a_stepless_gate_still_needs_its_other_jobs_clean(world, capsys):
    # A cancelled attempt does not excuse a job that failed beside the gate.
    _gate_world(world, run=_with(_RUN_1059, j500="failure"), rollup=list(GATES))
    for job in GATES:
        world.gh.actions_jobs[job]["steps"] = []
    world.gh.runs[RUN]["conclusion"] = "cancelled"

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


@pytest.mark.parametrize("sibling, outcome", [
    # The rollup lists only one leg; the other failed before its
    # cancelled-need step ran.
    ([("Set up job", "failure"), (CANCEL_STEP, "skipped"), (VERDICT_STEP, "skipped"),
      ("Complete job", "success")], FAILING),
    # It failed at its verdict instead.
    ([("Set up job", "success"), (CANCEL_STEP, "skipped"), (VERDICT_STEP, "failure"),
      ("Complete job", "success")], FAILING),
    # Its steps cannot be read.
    (None, UNVERIFIED),
    # It failed at its cancelled-need step too: the near miss that joins.
    (GATE_STEPS, None),
], ids=["setup-failed", "verdict-failed", "unreadable", "cancelled"])
def test_a_failed_gate_leg_the_rollup_omits_is_read_too(world, capsys, sibling, outcome):
    sha10, sha11 = _gate_world(world, rollup=["509"])
    if sibling is None:
        del world.gh.actions_jobs["508"]["steps"]
    else:
        world.gh.actions_jobs["508"]["steps"] = _steps(sibling)

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    for message in (FAILING, UNVERIFIED):
        assert (message in out) is (message == outcome), out


# Text edits to the head's rust.yml (the checkout keeps the real one).
TOP_DEFAULTS = ("\njobs:\n", "\ndefaults:\n  run:\n    working-directory: nowhere\njobs:\n")
TOP_ENV = ("\nenv:\n", "\nenv:\n  BASH_ENV: ./broken.sh\n")
GATE_NAME = "    name: Parity / ${{ matrix.os }}\n"


def _gate_key(text: str) -> tuple[str, str]:
    return GATE_NAME, GATE_NAME + text


@pytest.mark.parametrize("edit, admitted", [
    (TOP_DEFAULTS, False),
    (TOP_ENV, False),
    (_gate_key("    defaults:\n      run:\n        working-directory: nowhere\n"), False),
    (_gate_key("    env:\n      BASH_ENV: ./broken.sh\n"), False),
    (_gate_key("    container: ubuntu:24.04\n"), False),
    (_gate_key("    services:\n      db:\n        image: postgres:16\n"), False),
    (("    timeout-minutes: 5\n    steps:\n      # A cancelled",
      "    timeout-minutes: 6\n    steps:\n      # A cancelled"), False),
    # A change outside the gate and the workflow's defaults and env.
    (("    name: Rust daemon / disposable image\n",
      "    name: Rust daemon / disposable image\n    timeout-minutes: 61\n"), True),
], ids=["workflow-defaults", "workflow-env", "job-defaults", "job-env", "container",
        "services", "timeout", "other-job"])
def test_a_gate_whose_steps_inherit_other_settings_keeps_a_member_out(
        world, capsys, edit, admitted):
    old, new = edit
    assert WORKFLOW.count(old) == 1, old
    _gate_world(world, rollup=list(GATES), head_workflow=WORKFLOW.replace(old, new))

    assert _build() == 0, capsys.readouterr()

    assert (FAILING not in capsys.readouterr().out) is admitted


@pytest.mark.parametrize("edit, steps", [
    # A step named like the cancelled-need step, placed before it.
    (("      - name: A needed job was cancelled\n        if:",
      "      - name: A needed job was cancelled\n        run: exit 1\n"
      "      - name: A needed job was cancelled\n        if:"), None),
    # A templated name.
    (("      - name: A needed job was cancelled\n",
      "      - name: ${{ 'A needed job was cancelled' }}\n"), None),
    # An unnamed step, or one running an action.
    (("        run: exit 1\n      - name: Require",
      "        run: exit 1\n      - run: echo here\n      - name: Require"), None),
    (("        run: exit 1\n      - name: Require",
      "        run: exit 1\n      - uses: actions/checkout@v4\n      - name: Require"), None),
    # The workflow is the real one, but the failed step sits where the
    # verdict step is.
    (None, [("Set up job", "success"), (VERDICT_STEP, "skipped"), (CANCEL_STEP, "failure"),
            ("Complete job", "success")]),
    (None, [("Set up job", "success"), ("Initialize containers", "success"),
            (CANCEL_STEP, "failure"), (VERDICT_STEP, "skipped"), ("Complete job", "success")]),
], ids=["duplicate-name", "templated-name", "unnamed-step", "action-step",
        "swapped-positions", "shifted-positions"])
def test_the_failed_step_must_be_the_cancelled_need_step_by_position(world, capsys, edit, steps):
    head = WORKFLOW
    if edit:
        assert WORKFLOW.count(edit[0]) == 1, edit[0]
        head = WORKFLOW.replace(*edit)
    _gate_world(world, rollup=list(GATES), head_workflow=head,
                gate_steps=steps or GATE_STEPS)

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


@pytest.mark.parametrize("extra", [
    # A job the gate needs only through `parity-checks` failed, so
    # `parity-checks` skipped; its own row is absent from the rollup.
    ("510", "Fixtures / ubuntu-latest", "failure"),
    ("510", "Candidate harness / windows-latest", "timed_out"),
    # A job the gate does not need at all.
    ("510", "Parity live (non-gating) / ubuntu-latest / cli-store", "failure"),
], ids=["fixtures-failed", "harness-timed-out", "unneeded-failed"])
def test_a_failure_anywhere_in_the_gates_run_keeps_a_member_out(world, capsys, extra):
    _gate_world(world, _RUN_1059 + [extra], rollup=list(GATES))

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


def test_a_run_that_lists_a_job_the_workflow_read_lacks_keeps_a_member_out(world, capsys):
    # GitHub ran the merge ref's copy of the workflow, which the API does not
    # expose; a job the head's copy does not define shows they differ.
    _gate_world(world, _RUN_1059 + [("510", "Parity extra / ubuntu-latest", "cancelled")],
                rollup=list(GATES))

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


def test_a_failure_the_checkout_defines_no_gate_for_reads_nothing(world, capsys):
    # `Rust / <os>` runs under no `always()` in the checkout's rust.yml: a
    # known failure with no API call, even when the API would not answer.
    _gate_world(world, _with(_RUN_1059, j501="failure", j508="success", j509="success"),
                rollup=["501"])
    world.gh.actions_jobs.clear()

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out
    assert not [args for args, _ in world.gh.calls if args[0] == "api"
                and "/issues/" not in " ".join(args)]


def test_a_gate_in_another_workflow_of_the_checkout_is_no_match(world, capsys):
    # The same check name from a workflow with a different name.
    _gate_world(world, rollup=list(GATES))
    rollup = world.gh.pr(11)["statusCheckRollup"]
    for check in rollup:
        check["workflowName"] = "CI"

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


def test_a_cancelled_job_is_read_for_a_timeout_once(world, capsys):
    _gate_world(world)

    assert _build() == 0, capsys.readouterr()

    reads = [args[1] for args, _ in world.gh.calls if "/check-runs/" in args[1]]
    assert len(reads) == len(set(reads)), reads


def test_a_gate_check_without_pyyaml_refuses_with_its_fix(world, capsys, monkeypatch):
    _gate_world(world)
    monkeypatch.setitem(sys.modules, "yaml", None)     # `import yaml` raises

    assert _build() == 2

    err = capsys.readouterr().err
    assert "PyYAML" in err and "pip install pyyaml" in err


def test_conflict_markers_a_member_committed_are_flagged_in_the_body(world, capsys):
    sha10 = world.pr(10, {"b.txt": "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> x\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _publish(world) == 0, capsys.readouterr()

    (_, body), = world.gh.bodies("create")
    assert "conflict markers" in body and "b.txt" in body


def test_drafts_are_left_out(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    sha12 = world.pr(12, {"d.txt": "d\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, isDraft=True)
    world.gh.add(12, sha12)

    assert _publish(world) == 0, capsys.readouterr()

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha12]


def test_status_lists_each_labelled_pr_and_its_mergeability(world, capsys):
    sha10 = world.pr(10, {"a.txt": "one\nTEN\nthree\n"})
    world.advance_master({"a.txt": "one\nMASTER\nthree\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, reviewDecision="REVIEW_REQUIRED",
                 statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
    (world.work / "README.md").write_text("dirty is fine for status\n", encoding="utf-8")

    assert integrate.main(["status"]) == 0
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()
             if line.startswith("#")}

    assert "conflicts with master" in lines["#10"] and "a.txt" in lines["#10"]
    assert "merges cleanly" in lines["#11"]
    assert "failing" in lines["#11"] and "REVIEW_REQUIRED" in lines["#11"]
    assert "green" in lines["#10"]
    assert world.gh.mutations() == []


@pytest.mark.parametrize("rollup", [
    [{"status": "IN_PROGRESS", "conclusion": None}],   # pending
    [{"state": "PENDING"}],                            # a pending StatusContext
    [],                                                # no checks yet
    [{"status": "COMPLETED", "conclusion": "SUCCESS"}],  # green
    [{"status": "COMPLETED", "conclusion": "CANCELLED"}],  # superseded or cancelled
    [{"status": "COMPLETED", "conclusion": "SUCCESS"},
     {"status": "COMPLETED", "conclusion": "CANCELLED"}],
], ids=["in-progress", "status-pending", "none", "green", "cancelled", "partly-cancelled"])
def test_a_member_whose_own_ci_is_not_red_joins_the_batch(world, capsys, rollup):
    # The batch's CI is its members' CI, so a member waits on no run of its own.
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=rollup)

    assert _publish(world) == 0, capsys.readouterr()

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha11]
    (_, body), = world.gh.bodies("create")
    assert f"`{sha11}`" in body and f"| {integrate._checks(rollup)} |" in body


def test_a_member_with_red_ci_on_its_head_sits_out_and_keeps_its_label(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    sha12 = world.pr(12, {"d.txt": "d\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "FAILURE"}])
    world.gh.add(12, sha12, statusCheckRollup=[{"status": "IN_PROGRESS", "conclusion": None}])

    assert _publish(world) == 0
    out = capsys.readouterr().out

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha12]
    assert "left out #11: CI on its head is failing" in out
    # A rerun of the failed job clears it as well as a new push.
    assert "rerun" in out and "push" in out
    assert world.gh.bodies("comment") == [] and world.gh.bodies("edit") == []


def test_no_member_without_red_ci_builds_nothing_and_says_why(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10, statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
    world.gh.add(11, sha11, statusCheckRollup=[{"state": "ERROR"}])

    assert _build() == 0
    out = capsys.readouterr().out

    assert "0 of 2" in out and "#11 CI on its head is failing" in out
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_any_open_integrate_branch_pr_blocks_a_build(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    world.gh.history = [{"number": 860, "headRefName": "integrate/2026-10-07-rust-modes",
                         "state": "OPEN"},
                        {"number": 861, "headRefName": "claude/unrelated", "state": "OPEN"}]

    assert _build() == 2
    err = capsys.readouterr().err
    assert "#860" in err and "#861" not in err
    assert world.remote_branches() == []


def test_titles_cannot_break_the_member_table(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10, title="fix | APPROVED | green |\n## Fake heading `x`")
    world.gh.add(11, sha11)

    assert _publish(world) == 0, capsys.readouterr()

    (_, body), = world.gh.bodies("create")
    row = next(line for line in body.splitlines() if line.startswith("| #10 "))
    assert row.count(" | ") == 3 and "\\|" in row and "`x`" not in row
    assert "\n## Fake heading" not in body


def _three_with_a_collision(world) -> None:
    sha10 = world.pr(10, {"a.txt": "one\nTEN\nthree\n"})
    sha11 = world.pr(11, {"a.txt": "one\nELEVEN\nthree\n"})
    sha12 = world.pr(12, {"d.txt": "d\n"})
    for n, sha in ((10, sha10), (11, sha11), (12, sha12)):
        world.gh.add(n, sha)


def test_a_failed_pr_create_can_be_finished_by_rerunning_open(world, capsys):
    _three_with_a_collision(world)
    world.gh.fail_create = "before"

    assert _publish(world) == 2
    err = capsys.readouterr().err

    branch = f"integrate/{DATE}-batch-1"
    assert "HTTP 502" in err and "rerun `open`" in err
    assert world.remote_branches() == [branch]
    assert world.gh.bodies("comment") == []  # nothing claims a batch that has no PR

    world.gh.fail_create = None
    assert _open(world) == 0, capsys.readouterr()
    # The failed attempt and the one that opened it; the drop is reported once.
    assert len(world.gh.bodies("create")) == 2 and len(world.gh.bodies("comment")) == 1


def test_a_lost_create_reply_resumes_on_its_own_pull_request(world, capsys):
    _three_with_a_collision(world)
    world.gh.fail_create = "after"

    assert _publish(world) == 2
    assert "HTTP 504" in capsys.readouterr().err

    world.gh.fail_create = None
    assert _open(world) == 0
    out = capsys.readouterr().out

    assert "resuming https://github.example/pr/900" in out
    assert len(world.gh.bodies("create")) == 1   # no second batch pull request
    (args, body), = world.gh.bodies("comment")
    assert args[2] == "11" and "https://github.example/pr/900" in body


def test_a_failed_drop_report_resumes_without_repeating_itself(world, capsys):
    sha10 = world.pr(10, {"a.txt": "one\nTEN\nthree\n"})
    sha11 = world.pr(11, {"a.txt": "one\nELEVEN\nthree\n"})
    sha12 = world.pr(12, {"a.txt": "one\nTWELVE\nthree\n"})
    sha13 = world.pr(13, {"d.txt": "d\n"})
    for n, sha in ((10, sha10), (11, sha11), (12, sha12), (13, sha13)):
        world.gh.add(n, sha)
    assert _build() == 0
    world.gh.fail_comments = 1     # #11's comment goes through, #12's fails
    world.gh.calls.clear()
    real = world.gh.__call__

    first_ok = []

    def flaky(*args, input=None):
        if list(args[:2]) == ["pr", "comment"] and not first_ok:
            first_ok.append(True)
            world.gh.calls.append((list(args), input))
            return ""
        return real(*args, input=input)

    integrate.gh = flaky
    try:
        assert _open(world) == 2
        assert "rerun `open`" in capsys.readouterr().err
        assert _open(world) == 0, capsys.readouterr()
    finally:
        integrate.gh = world.gh

    comments = [args[2] for args, _ in world.gh.bodies("comment")]
    edits = [args[2] for args, _ in world.gh.bodies("edit")]
    # #12's failed attempt is recorded too; each member ends with one
    # delivered comment and one label removal, and #11 is not repeated.
    assert comments == ["11", "12", "12"] and edits == ["11", "12"]
    assert len(world.gh.bodies("create")) == 1


def test_a_failed_label_removal_resumes_without_a_second_comment(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0
    world.gh.fail_edits = 1

    assert _open(world) == 2
    assert "rerun `open`" in capsys.readouterr().err
    assert _open(world) == 0, capsys.readouterr()

    assert [args[2] for args, _ in world.gh.bodies("comment")] == ["11"]
    assert [args[2] for args, _ in world.gh.bodies("edit")] == ["11", "11"]


def test_a_fork_pr_named_like_a_batch_does_not_block_build(world, capsys):
    _three_with_a_collision(world)
    world.gh.history = [{"number": 870, "headRefName": f"integrate/{DATE}-batch-1",
                         "state": "OPEN", "url": "https://github.example/pr/870",
                         "isCrossRepository": True, "headRefOid": "f" * 40}]

    assert _publish(world) == 0, capsys.readouterr()


def test_a_fork_pr_on_the_built_branch_name_is_not_resumed(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0
    branch = f"integrate/{DATE}-batch-1"
    assert _git(world.work, "symbolic-ref", "--short", "HEAD") == branch
    # The fork appears after build, under the exact branch name `open` publishes.
    world.gh.history = [{"number": 870, "headRefName": branch, "state": "OPEN",
                         "url": "https://github.example/pr/870",
                         "isCrossRepository": True, "headRefOid": "f" * 40}]
    capsys.readouterr()

    assert _open(world) == 0, capsys.readouterr()

    assert "resuming" not in capsys.readouterr().out
    (args, _), = world.gh.bodies("create")
    (_, comment), = world.gh.bodies("comment")
    assert "pr/870" not in comment and "pr/900" in comment


def test_a_dropped_member_that_moved_since_build_keeps_its_label(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0
    # #11's author pushes a fix and relabels while the batch suite runs.
    next(r for r in world.gh.ready if r["number"] == 11)["headRefOid"] = "c" * 40
    capsys.readouterr()

    assert _open(world) == 0, capsys.readouterr()

    assert "#11 moved to cccccccccccc" in capsys.readouterr().out
    assert world.gh.bodies("comment") == [] and world.gh.bodies("edit") == []


def test_a_resume_finishes_the_batch_after_master_moved(world, capsys):
    _three_with_a_collision(world)
    world.gh.fail_create = "after"
    assert _publish(world) == 2
    capsys.readouterr()
    # Another PR lands on master before the rerun; the local batch stays put.
    _git(world.seed, "fetch", "-q", "origin")
    _git(world.seed, "switch", "-q", "-C", "master", "origin/master")
    (world.seed / "m.txt").write_text("m\n", encoding="utf-8")
    _git(world.seed, "add", "-A")
    _git(world.seed, "commit", "-q", "-m", "master moves")
    _git(world.seed, "push", "-q", "origin", "master")

    world.gh.fail_create = None
    assert _open(world) == 0, capsys.readouterr()

    assert "resuming https://github.example/pr/900" in capsys.readouterr().out
    assert len(world.gh.bodies("create")) == 1
    (args, _), = world.gh.bodies("comment")
    assert args[2] == "11"


def test_open_will_not_resume_on_its_own_branch_pr_at_another_head(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0
    world.gh.history = [{"number": 871, "headRefName": f"integrate/{DATE}-batch-1",
                         "state": "OPEN", "url": "https://github.example/pr/871",
                         "isCrossRepository": False, "headRefOid": "e" * 40}]
    capsys.readouterr()

    assert _open(world) == 2
    assert "#871" in capsys.readouterr().err
    assert world.gh.mutations() == [] and world.remote_branches() == []


def test_open_accepts_only_a_full_suite_result(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0
    capsys.readouterr()

    assert _open(world, args=["tests/test_integrate_script.py"]) == 2
    assert "selected run" in capsys.readouterr().err
    assert _open(world, args=_UNSET) == 2
    assert "does not record its pytest arguments" in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_build_alone_publishes_nothing_so_the_suite_runs_before_ci(world, capsys):
    _three_with_a_collision(world)

    assert _build() == 0
    out = capsys.readouterr().out

    branch = f"integrate/{DATE}-batch-1"
    assert world.remote_branches() == [] and world.gh.mutations() == []
    assert _git(world.work, "symbolic-ref", "--short", "HEAD") == branch
    assert "remote-suite.ps1" in out and "open --suite" in out
    plan = Path(_git(world.work, "rev-parse", "--git-path",
                     f"integrate-plans/{DATE}-batch-1.json"))
    assert (world.work / plan).is_file()

    assert _open(world) == 0, capsys.readouterr()
    (_, body), = world.gh.bodies("create")
    head = _git(world.work, "rev-parse", "HEAD")
    assert (f"Local full suite: passed at `{head}` on local-wsl, with the real native "
            "client built from it: 18868 passed") in body
    # The plan outlives `open`: `merge` reads it, and removes it once merged.
    assert json.loads((world.work / plan).read_text(encoding="utf-8"))["pr"] == 900


@pytest.mark.parametrize("case", ["no-evidence", "other-commit", "failed",
                                  "head-moved", "master-moved", "not-a-batch"])
def test_open_refuses_without_a_passing_suite_on_the_unchanged_batch(world, capsys, case):
    _three_with_a_collision(world)
    assert _build() == 0
    capsys.readouterr()
    if case == "no-evidence":
        code = integrate.main(["open"])
        expect = "--suite"
    elif case == "other-commit":
        code = _open(world, commit="0" * 40)
        expect = "not this batch head"
    elif case == "failed":
        code = _open(world, exit_code=1)
        expect = "suite failed"
    elif case == "head-moved":
        (world.work / "fix.txt").write_text("a fix\n", encoding="utf-8")
        _git(world.work, "add", "fix.txt")
        _git(world.work, "commit", "-q", "-m", "fix inside the batch")
        code = _open(world)
        expect = "nothing is fixed inside a batch"
    elif case == "master-moved":
        _git(world.seed, "fetch", "-q", "origin")
        _git(world.seed, "switch", "-q", "-C", "master", "origin/master")
        (world.seed / "e.txt").write_text("e\n", encoding="utf-8")
        _git(world.seed, "add", "e.txt")
        _git(world.seed, "commit", "-q", "-m", "master moves")
        _git(world.seed, "push", "-q", "origin", "master")
        code = _open(world)
        expect = "moved since"
    else:
        _git(world.work, "switch", "-q", "--detach", "origin/master")
        code = _open(world)
        expect = "build` first"

    assert code == 2
    assert expect in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_open_takes_a_stated_suite_result_and_says_it_was_not_checked(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0

    capsys.readouterr()
    assert integrate.main(["open", "--suite-note", "passed, trust me"]) == 2
    assert "must name the batch head" in capsys.readouterr().err
    assert world.gh.mutations() == []

    head = _git(world.work, "rev-parse", "HEAD")
    note = f"passed at {head[:8]} on the Linux dev box, 18868 passed, real client: yes"
    assert integrate.main(["open", "--suite-note", note,
                           *_gate_args(world)]) == 0, capsys.readouterr()

    (_, body), = world.gh.bodies("create")
    assert f"Local full suite: {note} (stated by the operator, not checked)" in body


# ── the real native client (#1634) ─────────────────────────────────────────

def test_build_says_the_batch_suite_builds_the_native_client(world, capsys):
    _three_with_a_collision(world)

    assert _build() == 0
    out = capsys.readouterr().out

    assert integrate.SUITE_COMMAND in out and integrate.REAL_CLIENT_NOTE in out
    assert "cargo build --locked -p pseudolife-stdio" in out and "8 GB" in out


@pytest.mark.parametrize("case", ["no", "not-recorded", "null", "other-commit"])
def test_open_refuses_a_suite_result_without_the_real_client_built_from_the_head(
        world, capsys, case):
    _three_with_a_collision(world)
    assert _build() == 0
    capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")
    result = {"no": {"real_client": False},
              "not-recorded": {"real_client": _UNSET},
              "null": {"real_client": None},
              "other-commit": {"real_client_commit": "0" * 40}}[case]

    assert _open(world, **result) == 2

    err = capsys.readouterr().err
    assert ("built from 000000000000" in err) is (case == "other-commit")
    assert ("ran without" in err) is (case == "no")
    # The refusal names the rerun and what it needs.
    assert integrate.SUITE_COMMAND in err and "PSEUDOLIFE_SUITE_NATIVE_CLIENT" in err
    assert head[:8] in err and "8 GB" in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_open_refuses_a_stated_result_that_does_not_say_real_client_yes(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0
    capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")

    for note in (f"passed at {head[:8]} on the Linux dev box, 18868 passed",
                 f"passed at {head[:8]}, real client: no",
                 f"passed at {head[:8]}, real client: yes, given by the caller (not built by this run)"):
        assert integrate.main(["open", "--suite-note", note,
                               *_gate_args(world)]) == 2
        err = capsys.readouterr().err
        assert "real client: yes" in err and integrate.SUITE_COMMAND in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_build_names_the_rust_harness_run_the_suite_does_not_cover(world, capsys):
    _three_with_a_collision(world)

    assert _build() == 0
    out = capsys.readouterr().out

    assert integrate.RUST_HARNESS_COMMAND in out and "--harness" in out and "shim-tests" in out


def _coverage_workflow(*paths):
    return json.dumps({"jobs": {
        "rust": {"steps": [{"run": "python -m pytest " + " ".join(paths)}]},
        "parity-checks": {"steps": []}}})


def test_build_drops_a_member_with_an_unlisted_batch_head_test_before_gating(tmp_path, monkeypatch, capsys):
    world = _make_world(tmp_path, monkeypatch, harness_guard=True)
    world.gh.add(11, world.pr(11, {"README.md": "independent change\n"}))
    world.gh.add(12, world.pr(12, {
        ".github/workflows/rust.yml": _coverage_workflow("rust/test_base.py", "rust/test_new.py"),
        "rust/test_new.py": "def test_new(): pass\n",
    }))
    assert _build() == 0, capsys.readouterr()
    plan = _plan(world)
    assert [member["number"] for member in plan["merged"]] == [11]
    assert len(plan["dropped"]) == 1 and plan["dropped"][0]["member"]["number"] == 12
    assert "RUST_HARNESS omits" in plan["dropped"][0]["reason"]
    assert world.cargo_calls() == []  # The admitted README change needs no build.
    assert "rust/test_base.py" in integrate._harness_recipe(plan["head"]).display()
    assert integrate.main(["open", "--suite", str(_suite_result(world)), *_gate_args(world)]) == 0, capsys.readouterr()
    assert any("RUST_HARNESS omits" in body for _, body in world.gh.bodies("comment"))


def test_build_accepts_a_workflow_and_its_matching_literal_list_together(tmp_path, monkeypatch, capsys):
    world = _make_world(tmp_path, monkeypatch, harness_guard=True)
    world.gh.add(12, world.pr(12, {
        ".github/workflows/rust.yml": _coverage_workflow("rust/test_base.py", "rust/test_new.py"),
        "ops/integrate.py": 'RUST_HARNESS = ("rust/test_base.py", "rust/test_new.py")\n',
        "rust/test_new.py": "def test_new(): pass\n",
    }))
    assert _build() == 0, capsys.readouterr()
    assert [member["number"] for member in _plan(world)["merged"]] == [12]
    assert "rust/test_new.py" in integrate._harness_recipe(_plan(world)["head"]).commands[0].paths


def test_an_unreadable_base_workflow_refuses_without_blaming_a_member(tmp_path, monkeypatch, capsys):
    world = _make_world(tmp_path, monkeypatch, harness_guard=True)
    world.advance_master({".github/workflows/rust.yml": "jobs: ["})
    world.gh.add(11, world.pr(11, {"README.md": "independent change\n"}))
    assert _build() == 2
    assert "batch-head harness coverage refused" in capsys.readouterr().err
    assert world.cargo_calls() == [] and world.remote_branches() == []
    assert not world.gh.bodies("comment")
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_an_unreadable_base_can_be_repaired_by_a_readable_batch_head(tmp_path, monkeypatch, capsys):
    world = _make_world(tmp_path, monkeypatch, harness_guard=True)
    world.advance_master({".github/workflows/rust.yml": "jobs: ["})
    world.gh.add(11, world.pr(11, {"README.md": "independent change\n"}))
    world.gh.add(12, world.pr(12, {".github/workflows/rust.yml": _coverage_workflow("rust/test_base.py")}))
    assert _build() == 0, capsys.readouterr()
    output = capsys.readouterr()
    assert "warning" in output.err.lower() and "base" in output.err.lower()
    assert "cannot read batch-head rust.yml" in output.err
    plan = _plan(world)
    assert [member["number"] for member in plan["merged"]] == [11, 12]
    assert not plan["dropped"]
    assert integrate._harness_recipe(plan["head"]).commands


@pytest.mark.parametrize("invalid", [False, True])
def test_census_conflict_resolution_still_checks_the_tentative_harness_selection(monkeypatch, invalid):
    from types import SimpleNamespace

    member = integrate.Member(12, "new test", "a" * 40, "https://github.example/pr/12", "NONE", "passing")
    plan = integrate.Plan("integrate/2026-11-17-batch-1", "b" * 40)
    before, merged = "c" * 40, "d" * 40
    current = [before]
    calls = []
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: member.sha)
    monkeypatch.setattr(integrate, "_budget_member_evidence", lambda *args: [])
    monkeypatch.setattr(integrate, "_undo_merge_if_frozen", lambda *args: False)

    def out(*args):
        return current[0] if args == ("rev-parse", "HEAD") else integrate.CENSUS_JSON
    def git(*args, **kwargs):
        if args[:2] == ("reset", "--hard"):
            assert args[2] == before
            current[0] = before
            return subprocess.CompletedProcess(args, 0, "", "")
        assert "merge" in args
        return subprocess.CompletedProcess(args, 1, "", "")
    def resolve(*args):
        current[0] = merged
        return True
    def recipe(head):
        calls.append(head)
        if head == merged and invalid:
            raise integrate.Refusal("RUST_HARNESS omits the new test")
    monkeypatch.setattr(integrate, "_out", out)
    monkeypatch.setattr(integrate, "git", git)
    monkeypatch.setattr(integrate, "_merge_census_conflict", resolve)
    monkeypatch.setattr(integrate, "_harness_recipe", recipe)

    integrate._merge_members(plan, [member], freeze=object(), prepass=SimpleNamespace(refused={}))

    if invalid:
        assert not plan.merged and not plan.census_conflicts
        assert len(plan.dropped) == 1 and plan.dropped[0].member == member
        assert "RUST_HARNESS omits" in plan.dropped[0].reason
        assert current[0] == before
    else:
        assert plan.merged == [member] and plan.census_conflicts == [member.number]
        assert not plan.dropped and current[0] == merged
    assert calls == [before, merged]


def test_open_refuses_without_the_rust_harness_result_for_this_head(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0
    assert integrate.main(["open", "--suite", str(_suite_result(world))]) == 2
    assert "--harness" in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_harness_note_is_no_longer_an_open_bypass(world):
    with pytest.raises(SystemExit) as error:
        integrate.main(["open", "--harness-note", "767 passed at abcdef12"])
    assert error.value.code == 2
    assert world.gh.mutations() == []


@pytest.mark.parametrize("result", ["passed", "0 passed", "3 failed, 764 passed",
                                    "764 passed, 1 error", "764 passed, 2 errors"])
def test_open_refuses_a_harness_result_without_a_clean_pass_count(world, capsys, result):
    _three_with_a_collision(world)
    assert _build() == 0
    args = _gate_args(world)
    _gate_result(world, summary=result)
    assert integrate.main(["open", "--suite", str(_suite_result(world)), *args]) == 2
    assert "failed" in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_open_records_both_gate_results_beside_the_suite(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0
    assert _open(world) == 0, capsys.readouterr()
    (_, body), = world.gh.bodies("create")
    assert "Windows dev VM: 767 passed" in body
    assert "second Linux dev box: 767 passed" in body
    assert "stated by the operator" not in body


def test_the_rust_harness_run_mirrors_ci_and_what_the_suite_skips():
    # The local full suite collects only tests/, so nothing under rust/.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'testpaths = ["tests"]' in pyproject
    workflow = (ROOT / ".github" / "workflows" / "rust.yml").read_text(encoding="utf-8")
    paths = [a for a in integrate.RUST_HARNESS if not a.startswith("--")
             and "::" not in a]
    for path in paths:
        assert (ROOT / path).exists(), path
        # Each one is a path CI's own pytest steps name.
        assert f" {path} " in workflow or f" {path}\n" in workflow, path
    # Without w2e lifecycle this would miss #877's break again.
    assert "rust/daemon/harness/test_w2e_lifecycle.py" in paths
    # Census currency is CI's `--ci-check` verdict, not a test, so nothing
    # under rust/ fails on a branch that leaves the census to the batch.
    assert "committed_census_matches_current_tree" not in workflow
    assert "--deselect" not in integrate.RUST_HARNESS
    assert "python rust/producer_census.py --ci-check" in workflow


def _rust_member(world, number: int = 10, text: str = "pub fn record() {}\n") -> str:
    sha = world.pr(number, {"rust/daemon/src/lib.rs": text})
    world.gh.add(number, sha)
    return sha


def _plan(world) -> dict:
    path = _git(world.work, "rev-parse", "--git-path", f"integrate-plans/{DATE}-batch-1.json")
    return json.loads((world.work / path).read_text(encoding="utf-8"))


def test_a_batch_changing_rust_is_compile_checked_by_build(world, capsys):
    # #991 changed record()'s signature and #994 called it the old way:
    # each compiled alone, the batch did not, and neither the suite nor the
    # harness run compiles Rust (#1017, 2026-10-28).
    _rust_member(world)

    assert _build() == 0, capsys.readouterr()
    out = capsys.readouterr().out

    head = _git(world.work, "rev-parse", "HEAD")
    (call,) = world.cargo_calls()
    assert call["argv"] == ["check", "--workspace", "--all-targets", "--locked"]
    assert Path(call["cwd"]).resolve() == (world.work / "rust").resolve()
    assert integrate.RUST_CHECK_COMMAND in out and head[:8] in out
    assert _plan(world)["rust_check"] == "passed"
    assert world.remote_branches() == [] and world.gh.mutations() == []

    assert _open(world) == 0, capsys.readouterr()
    (_, body), = world.gh.bodies("create")
    assert (f"Rust compile check (`{integrate.RUST_CHECK_COMMAND}` in `rust/`, which "
            f"checks all workspace targets): passed at `{head}`") in body


def test_a_batch_that_changes_nothing_under_rust_is_not_compile_checked(world, capsys):
    _three_with_a_collision(world)

    assert _build() == 0
    assert world.cargo_calls() == []
    assert _plan(world)["rust_check"] == "not needed"

    assert _open(world) == 0, capsys.readouterr()
    (_, body), = world.gh.bodies("create")
    assert "Rust compile check" in body and "changes nothing under `rust/`" in body


def test_a_batch_that_does_not_compile_is_red_and_never_opens(world, capsys, monkeypatch):
    _rust_member(world)
    monkeypatch.setenv("FAKE_CARGO_EXIT", "101")

    assert _build() == 1
    captured = capsys.readouterr()

    out = captured.out + captured.err
    branch = f"integrate/{DATE}-batch-1"
    # Nothing is dropped and the branch stays, so the operator can bisect it.
    assert _git(world.work, "symbolic-ref", "--short", "HEAD") == branch
    assert "E0061" in out and "bisect" in out and "merge-batches.md" in out
    assert _plan(world)["rust_check"].startswith("failed")
    assert _plan(world)["dropped"] == []
    assert world.remote_branches() == [] and world.gh.mutations() == []

    monkeypatch.delenv("FAKE_CARGO_EXIT")
    assert _open(world) == 2
    err = capsys.readouterr().err
    assert "does not compile" in err and "build again" in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_a_batch_whose_check_could_not_run_never_opens(world, capsys, monkeypatch):
    _rust_member(world)
    monkeypatch.setattr(integrate, "RUST_CHECK",
                        ("cargo-is-not-installed-here", "check", "--workspace",
                         "--all-targets", "--locked"))

    assert _build() == 1
    out = capsys.readouterr()
    assert "not on PATH" in out.out + out.err
    assert _plan(world)["rust_check"].startswith("not run")

    assert _open(world) == 2
    assert "build again" in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_open_refuses_a_plan_built_before_the_rust_check(world, capsys):
    _rust_member(world)
    assert _build() == 0
    path = Path(_git(world.work, "rev-parse", "--git-path",
                     f"integrate-plans/{DATE}-batch-1.json"))
    raw = json.loads((world.work / path).read_text(encoding="utf-8"))
    del raw["rust_check"]
    (world.work / path).write_text(json.dumps(raw), encoding="utf-8")
    capsys.readouterr()

    assert _open(world) == 2
    assert "build again" in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_the_rust_check_uses_cargo_target_dir_and_no_tokens(world, capsys, monkeypatch,
                                                           tmp_path):
    _rust_member(world)
    target = tmp_path / "shared-target"
    monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
    monkeypatch.setenv("GH_TOKEN", "secret-gh")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "secret-daemon")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-aws")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "secret-aws-id")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL",
                       "postgresql://pseudolife:hunter2@127.0.0.1:5432/pseudolife")
    monkeypatch.setenv("SOME_SERVICE", "https://user:hunter2@example.com/x")
    # Near misses the toolchain needs, which must survive.
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "cargo-home"))
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "1.94.0")
    monkeypatch.setenv("CARGO_REGISTRIES_CRATES_IO_PROTOCOL", "sparse")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:3128")

    assert _build() == 0, capsys.readouterr()

    (call,) = world.cargo_calls()
    env = call["env"]
    assert env["CARGO_TARGET_DIR"] == str(target)
    assert not {"GH_TOKEN", "PSEUDOLIFE_MCP_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
                "PSEUDOLIFE_MCP_DATABASE_URL", "SOME_SERVICE"} & set(env)
    assert not any("hunter2" in value for value in env.values())
    # The toolchain, the linker and the network still find what they need.
    assert env["PATH"] and env["CARGO_HOME"] == str(tmp_path / "cargo-home")
    assert env["RUSTUP_TOOLCHAIN"] == "1.94.0"
    assert env["CARGO_REGISTRIES_CRATES_IO_PROTOCOL"] == "sparse"
    assert env["HTTPS_PROXY"] == "http://proxy.example.com:3128"
    assert env["CARGO_TERM_COLOR"] == "never"


def test_a_failed_check_sends_the_operator_to_master_before_bisecting(world, capsys,
                                                                     monkeypatch):
    # cargo's words cannot tell the batch from the machine: a lock file two
    # members left inconsistent fails like a download, and a missing linker
    # prints "could not compile". Master decides, as for the census.
    _rust_member(world)
    monkeypatch.setenv("FAKE_CARGO_EXIT", "101")
    monkeypatch.setenv("FAKE_CARGO_OFFLINE", "1")

    assert _build() == 1
    captured = capsys.readouterr()
    out = captured.out + captured.err

    base = _git(world.work, "rev-parse", "origin/master")
    assert _plan(world)["rust_check"].startswith("failed")
    assert "failed to download" in out
    assert f"origin/master ({base[:12]})" in out and "this machine" in out
    assert "bisect" in out

    monkeypatch.delenv("FAKE_CARGO_EXIT")
    assert _open(world) == 2
    assert "this machine" in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_an_interrupted_check_still_leaves_a_plan_open_refuses(world, capsys, monkeypatch):
    _rust_member(world)

    def interrupted(plan):
        raise KeyboardInterrupt

    real = integrate._rust_check
    monkeypatch.setattr(integrate, "_rust_check", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _build()
    monkeypatch.setattr(integrate, "_rust_check", real)
    capsys.readouterr()

    assert _plan(world)["rust_check"].startswith("not run")
    assert _open(world) == 2
    assert "did not run" in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_open_resumes_a_batch_opened_from_a_plan_before_the_rust_check(world, capsys):
    # A plan from before the check whose batch pull request is already open
    # (a failure after the push) still finishes; only an unpublished one
    # must build again.
    _three_with_a_collision(world)
    world.gh.fail_comments = 1
    assert _build() == 0
    assert _open(world) == 2      # pushed and opened, the drop comment failed
    path = Path(_git(world.work, "rev-parse", "--git-path",
                     f"integrate-plans/{DATE}-batch-1.json"))
    raw = json.loads((world.work / path).read_text(encoding="utf-8"))
    del raw["rust_check"]
    (world.work / path).write_text(json.dumps(raw), encoding="utf-8")
    capsys.readouterr()

    assert _open(world) == 0, capsys.readouterr()
    assert len(world.gh.bodies("comment")) == 2   # the failed try, then the comment


def test_a_dry_run_names_the_rust_check_without_running_it(world, capsys):
    _rust_member(world)

    assert _build("--dry-run") == 0
    out = capsys.readouterr().out

    assert world.cargo_calls() == []
    assert integrate.RUST_CHECK_COMMAND in out and "would run" in out


def test_the_rust_check_is_cis_own_compile_step():
    # CI's `Rust` job runs this from rust/ (its working directory), where
    # rustup reads rust-toolchain.toml; clippy and fmt stay CI's.
    workflow = (ROOT / ".github" / "workflows" / "rust.yml").read_text(encoding="utf-8")
    assert "cargo check --locked --all-targets" in workflow
    assert integrate.RUST_CHECK_COMMAND == "cargo check --workspace --all-targets --locked"
    assert integrate.RUST_CHECK == tuple(integrate.RUST_CHECK_COMMAND.split())
    for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml"):
        assert (ROOT / integrate.RUST_CHECK_DIR / name).is_file(), name


# The compile check on a dev machine (rust-check.remote): the maintainer's
# host is not for heavy work (his rule, 16 November 2026), so `build` sends
# the batch to the Windows dev VM as a git bundle over ssh and never pushes it.

def _vm_script(call: dict) -> str:
    """The encoded command's script, the far half's parameters included."""
    assert call["script"], call
    return call["script"]


def test_a_configured_remote_runs_the_compile_check_there_and_pushes_nothing(world, capsys):
    _rust_member(world)
    world.configure_remote()

    assert _build() == 0, capsys.readouterr()
    out = capsys.readouterr().out

    head = _git(world.work, "rev-parse", "HEAD")
    base = _git(world.work, "rev-parse", "origin/master")
    assert world.cargo_calls() == [], "the check ran on this host too"
    (call,) = world.ssh_calls()
    # ssh never prompts, gives up on a dead link, and runs the far half in
    # PowerShell 7 as one encoded command.
    argv = call["argv"]
    assert "BatchMode=yes" in argv and "dev-vm" in argv
    # Nothing is forwarded to the machine where member build scripts run.
    for option in ("ForwardAgent=no", "ClearAllForwardings=yes", "ForwardX11=no"):
        assert option in argv, option
    assert argv[-1] == ("pwsh -NoProfile -NonInteractive -EncodedCommand "
                        + integrate.RUST_CHECK_BOOTSTRAP)
    script = _vm_script(call)
    assert integrate.RUST_CHECK_REMOTE_SCRIPT.strip() in script
    for value in (head, base, f"refs/heads/integrate/{DATE}-batch-1", "C:\\dev\\Pseudolife-MCP"):
        assert f"'{value}'" in script, value
    # The bundle carries the batch on top of master, and only that.
    bundle = call["bundle"]
    heads = _git(world.work, "bundle", "list-heads", bundle)
    assert heads == f"{head} refs/heads/integrate/{DATE}-batch-1"
    # Its one prerequisite is the base, which the far side's master holds.
    assert base in _git(world.work, "bundle", "verify", bundle)
    assert integrate.RUST_CHECK_COMMAND in out and "Windows dev VM" in out
    assert "dev-vm" not in out          # an alias names a host; the role does not
    plan = _plan(world)
    assert plan["rust_check"] == "passed" and plan["rust_check_where"] == "Windows dev VM"
    assert world.remote_branches() == [] and world.gh.mutations() == []

    assert _open(world) == 0, capsys.readouterr()
    (_, body), = world.gh.bodies("create")
    assert f"passed at `{head}` on the Windows dev VM, run by `build`" in body
    assert "dev-vm" not in body


def test_the_bundle_needs_only_master_on_the_far_side(world, capsys, tmp_path):
    _rust_member(world)
    world.configure_remote()
    assert _build() == 0, capsys.readouterr()
    (call,) = world.ssh_calls()
    head = _git(world.work, "rev-parse", "HEAD")

    far = tmp_path / "far"
    _git(tmp_path, "clone", "-q", str(world.origin), str(far))
    _git(far, "bundle", "verify", "-q", call["bundle"])
    _git(far, "fetch", "-q", call["bundle"],
         f"+refs/heads/integrate/{DATE}-batch-1:refs/rust-check/{head}")
    assert _git(far, "rev-parse", f"refs/rust-check/{head}") == head


def test_an_unreachable_remote_is_not_run_and_never_falls_back_here(world, capsys, monkeypatch):
    _rust_member(world)
    world.configure_remote()
    monkeypatch.setenv("FAKE_SSH_MODE", "unreachable")

    assert _build() == 1
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert world.cargo_calls() == [], "fell back to this host"
    record = _plan(world)["rust_check"]
    assert record.startswith("not run") and "Windows dev VM" in record
    assert "ssh" in record and "Connection timed out" in record
    assert "PSEUDOLIFE_RUST_CHECK_WHERE=local" in out

    assert _open(world) == 2
    err = capsys.readouterr().err
    assert "did not run" in err and "Windows dev VM" in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_a_remote_compile_error_is_a_red_batch_naming_its_first_error(world, capsys,
                                                                      monkeypatch):
    _rust_member(world)
    world.configure_remote()
    monkeypatch.setenv("FAKE_SSH_MODE", "compile")

    assert _build() == 1
    captured = capsys.readouterr()
    out = captured.out + captured.err
    record = _plan(world)["rust_check"]
    assert record.startswith("failed: error[E0061]: this function takes 3 arguments")
    assert "E0061" in out and "bisect" in out
    assert "cargo exit 101" not in out   # the far half's verdict line is not cargo's

    assert _open(world) == 2
    err = capsys.readouterr().err
    assert "does not compile" in err and "Windows dev VM" in err
    assert world.gh.mutations() == []


@pytest.mark.parametrize("mode, cause", [
    ("mismatch", "the bundle's head is 'abc', not the batch head"),
    ("silent", "exit 1"),
    # Verdict-shaped lines without this run's nonce, then a dropped session:
    # a member's proc macro can print them, so they never count.
    ("spoof", "ssh exit 255"),
    # The real verdict must be the far half's last word.
    ("trailing", "something after"),
])
def test_a_far_side_that_stops_before_cargo_is_not_run(world, capsys, monkeypatch, mode, cause):
    _rust_member(world)
    world.configure_remote()
    monkeypatch.setenv("FAKE_SSH_MODE", mode)

    assert _build() == 1
    capsys.readouterr()
    record = _plan(world)["rust_check"]
    assert record.startswith("not run") and cause in record, record
    assert world.cargo_calls() == []
    assert _open(world) == 2
    assert world.gh.mutations() == []


def test_forcing_local_runs_here_and_the_body_says_so(world, capsys, monkeypatch):
    _rust_member(world)
    world.configure_remote()
    monkeypatch.setenv("PSEUDOLIFE_RUST_CHECK_WHERE", "local")

    assert _build() == 0, capsys.readouterr()
    capsys.readouterr()
    assert world.ssh_calls() == [] and len(world.cargo_calls()) == 1
    assert _plan(world)["rust_check_where"] == "local"

    assert _open(world) == 0, capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")
    (_, body), = world.gh.bodies("create")
    assert f"passed at `{head}` on the maintainer's host, run by `build`" in body


def test_forcing_remote_without_a_config_is_not_run(world, capsys, monkeypatch):
    _rust_member(world)
    monkeypatch.setenv("PSEUDOLIFE_RUST_CHECK_WHERE", "remote")

    assert _build() == 1
    capsys.readouterr()
    record = _plan(world)["rust_check"]
    assert record.startswith("not run") and "rust-check.remote" in record
    assert world.cargo_calls() == [] and world.ssh_calls() == []


def test_an_unknown_where_is_not_run(world, capsys, monkeypatch):
    _rust_member(world)
    monkeypatch.setenv("PSEUDOLIFE_RUST_CHECK_WHERE", "vm")

    assert _build() == 1
    capsys.readouterr()
    record = _plan(world)["rust_check"]
    assert record.startswith("not run") and "local or remote" in record
    assert world.cargo_calls() == [] and world.ssh_calls() == []


def test_without_a_remote_config_the_check_runs_here_exactly_as_before(world, capsys):
    # The near miss that must keep working: an outside contributor, or this
    # host before its config is written.
    _rust_member(world)

    assert _build() == 0, capsys.readouterr()
    capsys.readouterr()
    assert world.ssh_calls() == [] and len(world.cargo_calls()) == 1
    path = Path(_git(world.work, "rev-parse", "--git-path",
                     f"integrate-plans/{DATE}-batch-1.json"))
    raw = json.loads((world.work / path).read_text(encoding="utf-8"))
    # Left out, so a plan loader from before the field still reads it.
    assert "rust_check_where" not in raw
    assert _open(world) == 0, capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")
    (_, body), = world.gh.bodies("create")
    assert f"passed at `{head}`, run by `build`" in body


@pytest.mark.parametrize("text, cause", [
    ("repo=C:\\dev\\Pseudolife-MCP\nrole=Windows dev VM\n", "ssh="),
    ("ssh=dev-vm\nrole=Windows dev VM\n", "repo="),
    # A role reaches the public batch body, so it may not look like a host.
    ("ssh=dev-vm\nrepo=C:\\dev\\x\nrole=build-host-7\n", "role="),
    ("ssh=dev-vm\nrepo=C:\\dev\\x\nrole=vm.example.com\n", "role="),
    # Letters only, yet a machine's name: only the listed roles pass.
    ("ssh=dev-vm\nrepo=C:\\dev\\x\nrole=athena\n", "role="),
    # PowerShell reads a typographic quote as a quote too.
    ("ssh=dev-vm\nrepo=C:\\dev\\x\u2019; calc; \u2019\n", "repo="),
    ("ssh=dev-vm\nrepo=C:\\dev\\$(calc)\n", "repo="),
    ("ssh=-oProxyCommand=calc\nrepo=C:\\dev\\x\n", "ssh="),
    # A typo or a repeated key is named, not silently ignored or overridden.
    ("ssh=dev-vm\nrepo=C:\\dev\\x\nrol=Windows dev VM\n", "line 3 is not one of"),
    ("ssh=dev-vm\nssh=other\nrepo=C:\\dev\\x\n", "ssh= twice"),
])
def test_a_malformed_remote_config_is_not_run(world, capsys, text, cause):
    _rust_member(world)
    world.configure_remote(text)

    assert _build() == 1
    capsys.readouterr()
    record = _plan(world)["rust_check"]
    assert record.startswith("not run") and cause in record, record
    assert world.cargo_calls() == [] and world.ssh_calls() == []


def test_a_utf16_config_is_refused_by_name_not_a_traceback(world, capsys):
    # Windows PowerShell 5.1's `>` and Out-File write UTF-16 with a BOM.
    _rust_member(world)
    integrate.RUST_CHECK_REMOTE.write_bytes(VM_CONFIG.encode("utf-16"))

    assert _build("--dry-run") == 0
    out = capsys.readouterr().out
    assert "would not run" in out and "as UTF-8" in out

    assert _build() == 1
    capsys.readouterr()
    record = _plan(world)["rust_check"]
    assert record.startswith("not run") and "as UTF-8" in record and "Set-Content" in record
    assert world.cargo_calls() == [] and world.ssh_calls() == []


def test_a_directory_in_place_of_the_config_is_refused(world, capsys):
    _rust_member(world)
    integrate.RUST_CHECK_REMOTE.mkdir()

    assert _build() == 1
    capsys.readouterr()
    assert "is not a file" in _plan(world)["rust_check"]
    assert world.cargo_calls() == [] and world.ssh_calls() == []


def test_a_plan_field_this_loader_does_not_know_is_dropped(world, capsys):
    # A plan written by a newer tool (or read by a reverted one) still opens.
    _rust_member(world)
    world.configure_remote()
    assert _build() == 0, capsys.readouterr()
    path = Path(_git(world.work, "rev-parse", "--git-path", f"integrate-plans/{DATE}-batch-1.json"))
    raw = json.loads((world.work / path).read_text(encoding="utf-8"))
    raw["a_field_from_a_later_tool"] = {"anything": 1}
    (world.work / path).write_text(json.dumps(raw), encoding="utf-8")
    capsys.readouterr()

    assert _open(world) == 0, capsys.readouterr()


def test_a_non_ascii_value_never_reaches_the_far_half():
    # PowerShell takes U+2018 to U+201B as quotes too; `--date` feeds the
    # branch name, which reaches the far half's -Ref.
    assert integrate._ps_literal("integrate/2026-11-16-batch-1") == "'integrate/2026-11-16-batch-1'"
    assert integrate._ps_literal("it's") == "'it''s'"
    for value in ("x’; calc; ’", "x‘y", "x‛y"):
        with pytest.raises(integrate.Refusal, match="non-ASCII"):
            integrate._ps_literal(value)


def test_a_bad_config_names_its_own_fix(world, capsys):
    _rust_member(world)
    world.configure_remote("ssh=dev-vm\nrepo=C:\\dev\\x\nrole=athena\n")
    assert _build() == 1
    capsys.readouterr()

    assert _open(world) == 2
    err = capsys.readouterr().err
    assert "rust-check.remote" in err and "Batch compile check" in err
    assert "rustup" not in err


def test_the_remote_config_is_only_read(world, capsys):
    _rust_member(world)
    world.configure_remote("# the VM\nssh=dev-vm\nrepo=C:\\dev\\Pseudolife-MCP\n")
    before = integrate.RUST_CHECK_REMOTE.read_bytes()

    assert _build() == 0, capsys.readouterr()
    capsys.readouterr()
    assert integrate.RUST_CHECK_REMOTE.read_bytes() == before
    # No role= gives a neutral one.
    assert _plan(world)["rust_check_where"] == "remote dev machine"


def test_a_dry_run_names_the_remote_without_contacting_it(world, capsys):
    _rust_member(world)
    world.configure_remote()

    assert _build("--dry-run") == 0
    out = capsys.readouterr().out
    assert world.ssh_calls() == [] and world.cargo_calls() == []
    assert "would run" in out and "on the Windows dev VM" in out


# The harness run on the dev machine (`harness`), and `open --harness`
# checking its result instead of an operator's note.

def _harness(world, capsys, mode: str = "pass") -> tuple[int, dict, Path]:
    os.environ["FAKE_SSH_MODE"] = mode
    try:
        code = integrate.main(["harness"])
    finally:
        os.environ.pop("FAKE_SSH_MODE", None)
    capsys.readouterr()
    (result,) = sorted(integrate.SUITE_RESULTS.glob("*-harness-*.json"))[-1:]
    return code, json.loads(result.read_text(encoding="utf-8")), result


def _open_with_harness(world, result: Path) -> int:
    return integrate.main(["open", "--suite", str(_suite_result(world)), "--harness", str(result),
                           "--shim-tests", str(_gate_result(world, "shim-tests"))])


def test_the_harness_runs_on_the_dev_machine_and_open_checks_its_result(world, capsys):
    _rust_member(world)
    world.configure_remote()
    assert _build() == 0, capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")

    code, result, path = _harness(world, capsys)

    assert code == 0
    assert result["kind"] == "rust-harness" and result["commit"] == head
    assert result["machine"] == "Windows dev VM"
    assert result["command"] == integrate.RUST_HARNESS_COMMAND and result["exit_code"] == 0
    assert result["summary"].startswith("2876 passed, 20 skipped")
    assert Path(result["log"]).is_file()
    harness_call = world.ssh_calls()[-1]
    assert "-PytestArgs @(" in harness_call["script"]
    for test_path in integrate.RUST_HARNESS:
        assert f"'{test_path}'" in harness_call["script"], test_path
    assert "Start-HarnessServices" in harness_call["script"]
    assert "Enter-DevDatabaseLock" in harness_call["script"]
    assert world.remote_branches() == [] and world.gh.mutations() == []

    assert _open_with_harness(world, path) == 0, capsys.readouterr()
    (_, body), = world.gh.bodies("create")
    assert f"passed at `{head}` on the Windows dev VM: 2876 passed, 20 skipped" in body
    assert "stated by the operator" not in body.split("Rust harness")[1].splitlines()[0]


@pytest.mark.parametrize("edit, cause", [
    ({"commit": "0" * 40}, "not this batch head"),
    ({"command": "python -m pytest rust/ci"}, "another selection"),
    ({"exit_code": 1, "summary": "1 failed, 2875 passed in 9.0s"}, "the harness failed"),
    ({"exit_code": 0, "summary": "2875 passed, 1 error in 9.0s"}, "the harness failed"),
    ({"exit_code": None, "not_run": "on the Windows dev VM: ssh exit 255: x"}, "did not run"),
    ({"machine": "athena"}, "no known dev machine role"),
    ({"kind": "suite"}, "is not a result"),
])
def test_open_refuses_a_harness_result_that_proves_less(world, capsys, edit, cause):
    _rust_member(world)
    world.configure_remote()
    assert _build() == 0, capsys.readouterr()
    _, result, path = _harness(world, capsys)
    result.update(edit)
    path.write_text(json.dumps(result), encoding="utf-8")

    assert _open_with_harness(world, path) == 2
    assert cause in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_open_refuses_a_missing_harness_result(world, capsys, tmp_path):
    _rust_member(world)
    world.configure_remote()
    assert _build() == 0, capsys.readouterr()
    capsys.readouterr()

    assert _open_with_harness(world, tmp_path / "nowhere.json") == 2
    assert "cannot read the harness result" in capsys.readouterr().err
    assert world.gh.mutations() == []


@pytest.mark.parametrize("mode, code", [("red", 1), ("nohook", 3), ("unreachable", 3)])
def test_a_red_or_unrun_harness_writes_a_result_open_refuses(world, capsys, mode, code):
    _rust_member(world)
    world.configure_remote()
    assert _build() == 0, capsys.readouterr()

    got, result, path = _harness(world, capsys, mode)

    assert got == code and result["commit"] == _git(world.work, "rev-parse", "HEAD")
    if mode == "red":
        assert result["exit_code"] == 1 and result["summary"].startswith("1 failed")
    else:
        assert result["exit_code"] is None and result["not_run"]
    assert _open_with_harness(world, path) == 2
    assert world.gh.mutations() == []


def test_the_harness_without_a_dev_machine_refuses_local_fallback(world, capsys):
    _rust_member(world)
    assert _build() == 0, capsys.readouterr()
    capsys.readouterr()

    assert integrate.main(["harness"]) == 2
    err = capsys.readouterr().err
    assert "rust-check.remote" in err and "no local fallback" in err
    assert world.ssh_calls() == []


def test_the_harness_needs_a_built_batch(world, capsys):
    world.configure_remote()
    assert integrate.main(["harness"]) == 2
    assert "run `python ops/integrate.py build` first" in capsys.readouterr().err
    assert world.ssh_calls() == []


@pytest.mark.parametrize("mode,expected", [("pass", 0), ("red", 100), ("unreachable", 3),
                                           ("spoof", 3), ("trailing", 3)])
def test_shim_gate_records_only_the_remote_verdict(world, capsys, monkeypatch, mode, expected):
    _rust_member(world)
    assert _build() == 0
    integrate.SHIM_TESTS_REMOTE.write_text("ssh=dev-box-two\nrepo=/tmp/gate-repo\nrole=second Linux dev box\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_SSH_MODE", mode)
    assert integrate.main(["shim-tests"]) == expected, capsys.readouterr()
    (path,) = integrate.SUITE_RESULTS.glob("*-shim-tests-*.json")
    result = json.loads(path.read_text())
    assert result["kind"] == "shim-tests"
    assert result["command"] == integrate.SHIM_TESTS_COMMAND
    assert result["selection"] == list(integrate.SHIM_TESTS_ARGS)
    assert result["commit"] == _git(world.work, "rev-parse", "HEAD")
    args = ["open", "--suite", str(_suite_result(world)), "--harness", str(_gate_result(world)),
            "--shim-tests", str(path)]
    assert integrate.main(args) == (0 if mode == "pass" else 2), capsys.readouterr()


@pytest.mark.parametrize("kind", ["rust-harness", "shim-tests"])
@pytest.mark.parametrize("edit", [
    {"selection": []}, {"commit": "f" * 40}, {"kind": "suite"}, {"command": "true"},
    {"machine": "remote dev machine"}, {"exit_code": False}, {"exit_code": "0"},
    {"exit_code": None}, {"not_run": "offline"}, {"summary": "0 passed"},
    {"summary": "10 passed, 1 failed"}, {"summary": None}, {"started": None},
    {"ended": "2000-01-01T00:00:00+00:00"}, {"started": "2026-11-17T01:00:00"},
])
def test_gate_result_requires_the_complete_run_contract(world, kind, edit):
    path = _gate_result(world, kind, **edit)
    with pytest.raises(integrate.Refusal):
        integrate._gate_result_line(str(path), _git(world.work, "rev-parse", "HEAD"), kind)


def test_shim_gate_without_configuration_never_uses_this_machine(world, capsys):
    _rust_member(world)
    assert _build() == 0
    assert integrate.main(["shim-tests"]) == 2
    assert "shim-tests.remote" in capsys.readouterr().err
    assert world.ssh_calls() == []


def test_the_far_half_is_read_from_the_checkout_build_started_in():
    # Read once, at load: a member that changes the script cannot change the
    # check that judges its own batch.
    script = (ROOT / "ops" / "rust-check-remote.ps1").read_text(encoding="utf-8")
    assert integrate.RUST_CHECK_REMOTE_SCRIPT == script
    assert "cargo check --workspace --all-targets --locked" in script
    # Through Windows' ssh.exe an -EncodedCommand of 16 KB is refused (8 KB
    # ran; 16 November 2026), so only the fixed bootstrap rides on it.
    assert len(integrate.RUST_CHECK_BOOTSTRAP) < 1000
    assert "[Console]::In.ReadLine()" in base64.b64decode(
        integrate.RUST_CHECK_BOOTSTRAP).decode("utf-16-le")
    # Beside full-suite.remote, which names a host too.
    assert integrate.RUST_CHECK_REMOTE == (Path.home() / ".pseudolife-mcp" / "locks"
                                           / "rust-check.remote")


# The far half itself (ops/rust-check-remote.ps1), run for real by pwsh
# against a local clone standing in for the dev machine's, with a cargo
# stand-in on PATH; and once end to end through `_remote_rust_check`, with
# an ssh stand-in that runs the encoded command here.
_PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(_PWSH is None, reason="the far half needs PowerShell 7")

_FAKE_FAR_CARGO = r'''
import json, os, sys

def suite_lock_free():
    """Whether a full suite could take its lock now, as tests/suite_lock.py
    takes it (_try_lock), while this stand-in runs as cargo."""
    path = os.path.join(os.environ["PSEUDOLIFE_SUITE_LOCK_DIR"], "full-suite.lock")
    try:
        handle = open(path, "a+b")
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        handle.close()

with open(os.environ["FAKE_FAR_CARGO_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                          "target": os.environ.get("CARGO_TARGET_DIR"),
                          "env": dict(os.environ),
                          "suite_lock_free": suite_lock_free()}) + "\n")
code = int(os.environ.get("FAKE_FAR_CARGO_EXIT", "0"))
if sys.argv[1:3] == ["nextest", "run"]:
    sys.stderr.write("Summary [1.2s] 12 tests run: 12 passed, 0 skipped\n")
if code:
    sys.stderr.write("error[E0061]: this function takes 3 arguments\n")
sys.exit(code)
'''

# Stands in for ssh by running the remote command here, stdin passed on.
_LOCAL_SSH = r'''
import shlex, subprocess, sys
sys.exit(subprocess.run(shlex.split(sys.argv[-1]), stdin=sys.stdin).returncode)
'''


class Far:
    """A batch built on top of origin's master, and a far clone of origin."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.origin = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(self.origin))
        _git(tmp_path, "init", "-q", "-b", "master", str(seed))
        _configure(seed)
        (seed / "rust").mkdir()
        (seed / "rust" / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "base")
        _git(seed, "remote", "add", "origin", str(self.origin))
        _git(seed, "push", "-q", "origin", "master")
        self.seed = seed
        self.base = _git(seed, "rev-parse", "HEAD")
        self.repo = tmp_path / "far"
        _git(tmp_path, "clone", "-q", str(self.origin), str(self.repo))
        # cargo's stand-in, first on the far half's PATH.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "fake_cargo.py").write_text(_FAKE_FAR_CARGO, encoding="utf-8")
        if os.name == "nt":
            (bin_dir / "cargo.cmd").write_text(
                f'@"{sys.executable}" "%~dp0fake_cargo.py" %*\r\n', encoding="utf-8")
        else:
            cargo = bin_dir / "cargo"
            cargo.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/fake_cargo.py" "$@"\n',
                             encoding="utf-8")
            cargo.chmod(0o755)
        self.cargo_log = tmp_path / "far-cargo.jsonl"
        monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
        monkeypatch.setenv("FAKE_FAR_CARGO_LOG", str(self.cargo_log))
        monkeypatch.delenv("FAKE_FAR_CARGO_EXIT", raising=False)
        # The far half probes the machine's full-suite lock; never this
        # machine's real one (a suite running these tests holds it).
        self.locks = tmp_path / "locks"
        self.locks.mkdir()
        monkeypatch.setenv("PSEUDOLIFE_SUITE_LOCK_DIR", str(self.locks))
        self.n = 0

    def batch(self) -> tuple[str, str, str]:
        """A new integrate/* commit on the base: (head, ref, base64 bundle)."""
        self.n += 1
        branch = f"integrate/{DATE}-batch-{self.n}"
        _git(self.seed, "switch", "-q", "-C", branch, self.base)
        (self.seed / "rust" / f"f{self.n}.rs").write_text("fn f() {}\n", encoding="utf-8",
                                                          newline="\n")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-q", "-m", f"batch {self.n}")
        head = _git(self.seed, "rev-parse", "HEAD")
        bundle = self.tmp / f"b{self.n}.bundle"
        _git(self.seed, "bundle", "create", "-q", str(bundle), f"refs/heads/{branch}",
             f"^{self.base}")
        return head, f"refs/heads/{branch}", base64.b64encode(bundle.read_bytes()).decode()

    def run(self, head: str, ref: str, data: str, *, base: str | None = None,
            extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        return subprocess.run(
            [_PWSH, "-NoProfile", "-NonInteractive", "-File",
             str(ROOT / "ops" / "rust-check-remote.ps1"), "-Repo", str(self.repo),
             "-Base", base or self.base, "-Head", head, "-Ref", ref, *extra],
            input=data, capture_output=True, text=True, encoding="utf-8", timeout=180)

    def cargo_calls(self) -> list[dict]:
        if not self.cargo_log.is_file():
            return []
        return [json.loads(line)
                for line in self.cargo_log.read_text(encoding="utf-8").splitlines()]

    def leftovers(self) -> list[str]:
        refs = _git(self.repo, "for-each-ref", "--format=%(refname)", "refs/rust-check/")
        bundles = [p.name for p in self.tmp.glob("far.rust-check-*.bundle")]
        return refs.splitlines() + bundles


@pytest.fixture
def far(tmp_path, monkeypatch):
    return Far(tmp_path, monkeypatch)


def _verdict(proc: subprocess.CompletedProcess) -> str:
    lines = proc.stderr.strip().splitlines()
    return lines[-1] if lines else ""


@needs_pwsh
def test_the_far_half_checks_exactly_the_batch_head_in_its_gate_copy(far):
    head, ref, data = far.batch()

    proc = far.run(head, ref, data)

    assert proc.returncode == 0, proc.stderr
    assert _verdict(proc) == "rust-check: cargo exit 0"
    (call,) = far.cargo_calls()
    assert call["argv"] == ["check", "--workspace", "--all-targets", "--locked"]
    gate = Path(f"{far.repo}.rust-check")
    assert Path(call["cwd"]).resolve() == (gate / "rust").resolve()
    # One target directory for that one copy, never a shared one.
    assert Path(call["target"]) == Path(f"{far.repo}.rust-check-target")
    assert _git(gate, "rev-parse", "HEAD") == head
    assert far.leftovers() == []


@needs_pwsh
def test_the_far_half_withholds_credentials_from_cargo(far, monkeypatch):
    # Member build scripts run under cargo there, as they do here, so the
    # ssh session's credential-shaped variables are withheld the same way.
    head, ref, data = far.batch()
    monkeypatch.setenv("GH_TOKEN", "secret-gh")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-aws")
    monkeypatch.setenv("SOME_SERVICE", "https://user:hunter2@example.com/x")
    monkeypatch.setenv("CARGO_NEAR_MISS", "kept")
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "1.94.0")

    assert far.run(head, ref, data).returncode == 0

    (call,) = far.cargo_calls()
    env = call["env"]
    assert not {"GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "SOME_SERVICE"} & set(env)
    assert not any("hunter2" in value for value in env.values())
    assert env["RUSTUP_TOOLCHAIN"] == "1.94.0" and env["CARGO_NEAR_MISS"] == "kept"


def test_the_far_half_withholds_what_the_local_check_withholds():
    script = (ROOT / "ops" / "rust-check-remote.ps1").read_text(encoding="utf-8")
    name = re.search(r"\$secretName = '\(\?i\)(.*)'", script).group(1)
    value = re.search(r"\$secretValue = '(.*)'", script).group(1)
    assert name == integrate.SECRET_ENV_RE.pattern
    assert value == integrate.URL_CREDENTIALS_RE.pattern


@needs_pwsh
def test_a_drive_under_the_floor_is_not_run_never_a_red_batch(far):
    head, ref, data = far.batch()

    proc = far.run(head, ref, data, extra=("-MinFreeGB", "1000000000"))

    assert proc.returncode == 3, proc.stderr
    assert "GB free where the gate copy and its target live" in _verdict(proc)
    assert "the floor is" in _verdict(proc)
    assert far.cargo_calls() == [] and far.leftovers() == []


@needs_pwsh
@pytest.mark.skipif(os.name == "nt", reason="needs a mount other than / (a tmpfs)")
def test_the_floor_is_judged_on_the_filesystem_beside_the_clone(far):
    # The root's free space is not the clone's when the clone sits on its own
    # mount (GetPathRoot gives "/" for every Linux path): a floor between the
    # two decides differently for each.
    root = os.stat("/").st_dev
    mount = next((d for d in (far.tmp, Path("/dev/shm"))
                  if d.is_dir() and os.stat(d).st_dev != root), None)
    if mount is None:
        pytest.skip("no filesystem but / to put the clone on")
    root_free = shutil.disk_usage("/").free / 2**30
    there = shutil.disk_usage(mount).free / 2**30
    if abs(root_free - there) < 4:
        pytest.skip(f"{mount} ({there:.1f} GB free) and / ({root_free:.1f} GB) are too close")
    home = Path(tempfile.mkdtemp(prefix="w26-far-floor-", dir=mount))
    try:
        far.repo = home / "far"
        _git(far.tmp, "clone", "-q", str(far.origin), str(far.repo))
        head, ref, data = far.batch()
        floor = (root_free + there) / 2

        proc = far.run(head, ref, data, extra=("-MinFreeGB", f"{floor:.1f}"))

        if there < floor:
            assert proc.returncode == 3, proc.stderr
            assert "GB free where the gate copy and its target live" in _verdict(proc)
            assert far.cargo_calls() == []
        else:
            assert proc.returncode == 0, proc.stderr
            assert _verdict(proc) == "rust-check: cargo exit 0"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _hold_suite_lock(path: Path):
    """Take the full-suite lock as tests/suite_lock.py's _try_lock takes it."""
    handle = open(path, "a+b")
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


@needs_pwsh
def test_a_held_full_suite_lock_is_not_run(far):
    # A compile check never starts while a full suite runs on the machine.
    head, ref, data = far.batch()
    held = _hold_suite_lock(far.locks / "full-suite.lock")
    try:
        proc = far.run(head, ref, data)
    finally:
        held.close()

    assert proc.returncode == 3, proc.stderr
    assert "a full suite holds this machine" in _verdict(proc)
    assert far.cargo_calls() == []


@needs_pwsh
def test_a_suite_that_starts_during_the_check_waits_for_it(far):
    # Codex (final-head review): a probe let go at once let a suite take its
    # lock while cargo ran. The far half holds it until its cleanup, in a way
    # the suite's own lock sees, and lets it go after; also when the lock
    # file did not exist before.
    head, ref, data = far.batch()
    assert not (far.locks / "full-suite.lock").exists()

    proc = far.run(head, ref, data)

    assert proc.returncode == 0, proc.stderr
    (call,) = far.cargo_calls()
    assert call["suite_lock_free"] is False, "a suite could start beside the check"
    _hold_suite_lock(far.locks / "full-suite.lock").close()   # free again afterwards


@needs_pwsh
def test_a_hook_a_batch_left_in_the_clone_does_not_run(far, tmp_path):
    # A member's build script can write the clone's .git/hooks; the next
    # checkout in the gate must not run what it wrote.
    head, ref, data = far.batch()
    marker = tmp_path / "hook-ran"
    hooks = far.repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-checkout", "reference-transaction"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\n", encoding="utf-8",
                        newline="\n")
        hook.chmod(0o755)

    proc = far.run(head, ref, data)

    assert proc.returncode == 0, proc.stderr
    assert not marker.exists(), marker.read_text(encoding="utf-8")


@needs_pwsh
def test_a_no_hooks_path_that_exists_is_refused(far):
    head, ref, data = far.batch()
    Path(f"{far.repo}.rust-check-no-hooks").mkdir()

    proc = far.run(head, ref, data)

    assert proc.returncode == 3 and "must not" in _verdict(proc), proc.stderr
    assert far.cargo_calls() == []


@needs_pwsh
def test_the_far_half_tags_its_verdict_with_the_runs_nonce(far):
    head, ref, data = far.batch()
    nonce = "00ff" * 8

    proc = far.run(head, ref, data, extra=("-Nonce", nonce))
    assert _verdict(proc) == f"rust-check {nonce}: cargo exit 0", proc.stderr

    proc = far.run("0" * 40, ref, data, extra=("-Nonce", nonce))
    assert _verdict(proc).startswith(f"rust-check {nonce}: not run: "), proc.stderr


@needs_pwsh
def test_a_killed_runs_leftovers_are_swept_by_the_next(far):
    head, ref, data = far.batch()
    _git(far.repo, "update-ref", "refs/rust-check/" + "a" * 40, far.base)
    stale = far.tmp / "far.rust-check-aaaaaaaaaaaa.bundle"
    stale.write_bytes(b"left by a killed run")

    assert far.run(head, ref, data).returncode == 0
    assert far.leftovers() == []


@needs_pwsh
def test_a_trailing_separator_keeps_the_gate_beside_the_clone(far):
    head, ref, data = far.batch()
    proc = subprocess.run(
        [_PWSH, "-NoProfile", "-NonInteractive", "-File",
         str(ROOT / "ops" / "rust-check-remote.ps1"), "-Repo", str(far.repo) + os.sep,
         "-Base", far.base, "-Head", head, "-Ref", ref],
        input=data, capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert Path(f"{far.repo}.rust-check").is_dir()
    assert not (far.repo / ".rust-check").exists()


@needs_pwsh
def test_the_next_batch_reuses_the_gate_copy_clean(far):
    head1, ref1, data1 = far.batch()
    assert far.run(head1, ref1, data1).returncode == 0
    gate = Path(f"{far.repo}.rust-check")
    (gate / "rust" / "stray.rs").write_text("left by the last run\n", encoding="utf-8")

    head2, ref2, data2 = far.batch()
    proc = far.run(head2, ref2, data2)

    assert proc.returncode == 0, proc.stderr
    assert _git(gate, "rev-parse", "HEAD") == head2
    assert not (gate / "rust" / "stray.rs").exists()
    assert not (gate / "rust" / "f1.rs").exists() and (gate / "rust" / "f2.rs").exists()
    assert far.leftovers() == []


@needs_pwsh
def test_the_far_half_reports_cargos_own_exit(far, monkeypatch):
    head, ref, data = far.batch()
    monkeypatch.setenv("FAKE_FAR_CARGO_EXIT", "101")

    proc = far.run(head, ref, data)

    assert proc.returncode == 101
    assert _verdict(proc) == "rust-check: cargo exit 101"
    assert "error[E0061]" in proc.stderr
    assert far.leftovers() == []


@needs_pwsh
def test_a_bundle_that_is_not_the_batch_head_is_refused_before_cargo(far):
    _, ref, data = far.batch()
    other, _, _ = far.batch()

    proc = far.run(other, ref, data)

    assert proc.returncode == 3, proc.stderr
    assert _verdict(proc).startswith("rust-check: not run: the bundle's head is")
    assert other in _verdict(proc)
    assert far.cargo_calls() == [] and far.leftovers() == []


@needs_pwsh
def test_a_base_origin_lacks_is_refused(far):
    # The batch was built on a commit origin never had.
    _git(far.seed, "switch", "-q", "-C", "local-only", far.base)
    (far.seed / "x.txt").write_text("x\n", encoding="utf-8")
    _git(far.seed, "add", "-A")
    _git(far.seed, "commit", "-q", "-m", "never pushed")
    far.base = _git(far.seed, "rev-parse", "HEAD")
    head, ref, data = far.batch()

    proc = far.run(head, ref, data)

    assert proc.returncode == 3
    assert "is not on origin" in _verdict(proc), proc.stderr
    assert far.cargo_calls() == [] and far.leftovers() == []


@needs_pwsh
def test_a_clone_that_cannot_reach_origin_is_refused(far):
    head, ref, data = far.batch()
    _git(far.repo, "remote", "set-url", "origin", str(far.tmp / "gone.git"))

    proc = far.run(head, ref, data)

    assert proc.returncode == 3
    assert "the clone cannot fetch origin" in _verdict(proc), proc.stderr
    assert far.cargo_calls() == []


@needs_pwsh
@pytest.mark.parametrize("data, cause", [("", "no bundle arrived"),
                                         ("not base64!", "not base64")])
def test_no_usable_bundle_is_refused(far, data, cause):
    head, ref, _ = far.batch()

    proc = far.run(head, ref, data)

    assert proc.returncode == 3 and cause in _verdict(proc), proc.stderr
    assert far.cargo_calls() == []


@needs_pwsh
def test_only_an_integrate_ref_is_taken(far):
    head, _, data = far.batch()

    proc = far.run(head, "refs/heads/master", data)

    assert proc.returncode == 3 and "integrate/*" in _verdict(proc), proc.stderr


@needs_pwsh
def test_a_held_gate_lock_waits_then_refuses(far):
    head, ref, data = far.batch()
    lock = f"{far.repo}.rust-check.lock"
    holder = subprocess.Popen(
        [_PWSH, "-NoProfile", "-NonInteractive", "-Command",
         f"$f = [IO.File]::Open('{lock}', 'OpenOrCreate', 'ReadWrite', 'None'); "
         "[Console]::Out.WriteLine('held'); [Console]::Out.Flush(); Start-Sleep -Seconds 60"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        proc = far.run(head, ref, data, extra=("-LockWaitSeconds", "2"))
    finally:
        holder.kill()
        holder.wait(timeout=30)

    assert proc.returncode == 3
    assert "waiting for the gate lock" in proc.stderr
    assert "another build held the gate lock" in _verdict(proc)
    assert far.cargo_calls() == []


@needs_pwsh
def test_build_and_the_far_half_agree_end_to_end(far, monkeypatch, tmp_path):
    # integrate.py's own encoding of the command, its bundle on stdin and
    # its reading of the verdict, with the far half run by pwsh here.
    head, ref, _ = far.batch()
    monkeypatch.chdir(far.seed)
    ssh = tmp_path / "local_ssh.py"
    ssh.write_text(_LOCAL_SSH, encoding="utf-8")
    monkeypatch.setattr(integrate, "RUST_CHECK_SSH", (sys.executable, str(ssh)))
    plan = integrate.Plan(branch=ref.removeprefix("refs/heads/"), base_sha=far.base,
                          label=LABEL, head=head)
    cfg = {"ssh": "dev-vm", "repo": str(far.repo), "role": "Windows dev VM"}

    assert integrate._remote_rust_check(plan, cfg) == ("passed", "")
    assert far.leftovers() == []

    monkeypatch.setenv("FAKE_FAR_CARGO_EXIT", "101")
    record, errors = integrate._remote_rust_check(plan, cfg)
    assert record == "failed: error[E0061]: this function takes 3 arguments", errors
    assert len(far.cargo_calls()) == 2


# The harness's far half (ops/rust-harness-remote.ps1) for real under pwsh,
# against a local clone, with stand-ins for uv (a real venv that sees this
# interpreter's packages) and for the machine's services hook.
_FAKE_UV = r'''
import json, os, subprocess, sys
with open(os.environ["FAKE_UV_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_UV_ENV_LOG"):
    names = ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "SERVICE_DSN", "SOME_SERVICE",
             "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTHONHOME")
    with open(os.environ["FAKE_UV_ENV_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({name: name in os.environ for name in names}) + "\n")
if sys.argv[1] == "venv":
    import glob, site
    target = sys.argv[-1]
    code = subprocess.run([sys.executable, "-m", "venv", "--copies", target]).returncode
    if code == 0:
        # Match the Windows environment's regular-file layout so the far
        # half's no-reparse cleanup is exercised on Linux too.
        compatibility = os.path.join(target, "lib64")
        if os.path.islink(compatibility):
            os.unlink(compatibility)
        # The new environment sees this interpreter's packages (pytest),
        # also when this interpreter is itself a virtual environment.
        # lib64 is a link to lib on some Linux venvs: one is enough.
        lib = sorted(glob.glob(os.path.join(target, "**", "site-packages"), recursive=True))[0]
        with open(os.path.join(lib, "parent.pth"), "w", encoding="utf-8") as pth:
            pth.write("\n".join(site.getsitepackages()) + "\n")
    sys.exit(code)
sys.exit(int(os.environ.get("FAKE_UV_PIP_EXIT", "0")))
'''

_FAKE_HOOK = r'''
function Start-HarnessServices {
    Add-Content -LiteralPath $env:FAKE_HOOK_LOG -Value 'start'
    if ($env:FAKE_HOOK_FAIL) { throw 'the test PostgreSQL did not start' }
    $env:FAKE_SERVICE = 'up'
}
function Stop-HarnessServices { Add-Content -LiteralPath $env:FAKE_HOOK_LOG -Value 'stop' }
'''

# The batch's own tests, standing in for RUST_HARNESS: they see the
# services' environment, and the option rust/conftest.py defines.
_GATE_CONFTEST = '''
def pytest_addoption(parser):
    parser.addoption("--require-databases", action="store_true")
'''
_GATE_TEST = '''
import os
def test_the_services_are_up(request):
    assert os.environ["FAKE_SERVICE"] == "up"
    assert request.config.getoption("--require-databases")
'''


class HarnessFar(Far):
    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.turn_marker = tmp_path / "test-postgres.turn-enabled"
        self.turn_marker.touch()
        bin_dir = tmp_path / "bin"
        (bin_dir / "fake_uv.py").write_text(_FAKE_UV, encoding="utf-8")
        if os.name == "nt":
            (bin_dir / "uv.cmd").write_text(f'@"{sys.executable}" "%~dp0fake_uv.py" %*\r\n',
                                            encoding="utf-8")
        else:
            uv = bin_dir / "uv"
            uv.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/fake_uv.py" "$@"\n',
                          encoding="utf-8")
            uv.chmod(0o755)
        self.uv_log = tmp_path / "uv.jsonl"
        self.hook_log = tmp_path / "hook.log"
        self.hook = tmp_path / "rust-harness-services.ps1"
        self.hook.write_text(_FAKE_HOOK, encoding="utf-8")
        monkeypatch.setenv("FAKE_UV_LOG", str(self.uv_log))
        monkeypatch.setenv("FAKE_HOOK_LOG", str(self.hook_log))
        monkeypatch.delenv("FAKE_HOOK_FAIL", raising=False)
        monkeypatch.delenv("FAKE_SERVICE", raising=False)

    def batch(self, body: str = _GATE_TEST, *, unit_body: str | None = None,
              unit_name: str = "unit_gate.py") -> tuple[str, str, str]:
        self.n += 1
        branch = f"integrate/{DATE}-batch-{self.n}"
        _git(self.seed, "switch", "-q", "-C", branch, self.base)
        gate_tests = self.seed / "gate_tests"
        gate_tests.mkdir(exist_ok=True)
        (gate_tests / "conftest.py").write_text(_GATE_CONFTEST, encoding="utf-8", newline="\n")
        (gate_tests / "test_gate.py").write_text(body, encoding="utf-8", newline="\n")
        if unit_body is not None:
            (gate_tests / unit_name).write_text(unit_body, encoding="utf-8", newline="\n")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-q", "-m", f"batch {self.n}")
        head = _git(self.seed, "rev-parse", "HEAD")
        bundle = self.tmp / f"h{self.n}.bundle"
        _git(self.seed, "bundle", "create", "-q", str(bundle), f"refs/heads/{branch}",
             f"^{self.base}")
        return head, f"refs/heads/{branch}", base64.b64encode(bundle.read_bytes()).decode()

    def run_harness(self, head: str, ref: str, data: str, *, hook: Path | None = None,
                    extra: tuple[str, ...] = (), pytest_path: str = "gate_tests") -> subprocess.CompletedProcess:
        return subprocess.run(
            [_PWSH, "-NoProfile", "-NonInteractive", "-File",
             str(ROOT / "ops" / "rust-harness-remote.ps1"), "-Repo", str(self.repo),
             "-Base", self.base, "-Head", head, "-Ref", ref,
             "-ServicesHook", str(hook or self.hook), "-DatabaseLockPath", str(self.tmp / "own-database.lock"),
             "-TurnMarkerPath", str(self.turn_marker),
             "-PytestArgs", pytest_path, *extra],
            input=data, capture_output=True, text=True, encoding="utf-8", timeout=600)

    def hook_calls(self) -> list[str]:
        return self.hook_log.read_text(encoding="utf-8").split() if self.hook_log.is_file() else []

    def harness_leftovers(self) -> list[str]:
        refs = _git(self.repo, "for-each-ref", "--format=%(refname)", "refs/rust-harness/")
        return refs.splitlines() + [p.name for p in self.tmp.glob("far.rust-harness-*.bundle")]


@pytest.fixture
def harness_far(tmp_path, monkeypatch):
    return HarnessFar(tmp_path, monkeypatch)


@needs_pwsh
@pytest.mark.parametrize("requirements", [(), ("onnx==1.22.0",)])
def test_the_harness_far_half_runs_the_batch_tests_with_its_services_up(harness_far, requirements):
    head, ref, data = harness_far.batch()

    encoded = base64.b64encode(json.dumps(requirements).encode()).decode()
    proc = harness_far.run_harness(head, ref, data, extra=("-ExtraRequirements", encoded))

    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert _verdict(proc) == "rust-harness: pytest exit 0"
    assert "1 passed" in proc.stdout
    assert harness_far.hook_calls() == ["start", "stop"]
    gate = Path(f"{harness_far.repo}.rust-harness")
    assert _git(gate, "rev-parse", "HEAD") == head
    installs = [json.loads(line) for line in harness_far.uv_log.read_text(encoding="utf-8").splitlines()]
    assert installs[0][0] == "venv"
    environment = Path(installs[0][-1])
    assert environment.name.startswith(harness_far.repo.name + ".rust-harness-env-")
    assert not environment.exists()
    assert installs[1][:2] == ["pip", "install"]
    assert installs[1][installs[1].index("-e") + 1:] == [f"{gate}[dev]", *requirements]
    assert harness_far.harness_leftovers() == []


@needs_pwsh
def test_a_failing_batch_test_is_pytests_exit_and_the_services_still_stop(harness_far):
    head, ref, data = harness_far.batch("def test_red():\n    assert False\n")

    proc = harness_far.run_harness(head, ref, data)

    assert proc.returncode == 1
    assert _verdict(proc) == "rust-harness: pytest exit 1"
    assert harness_far.hook_calls() == ["start", "stop"]
    assert harness_far.harness_leftovers() == []


@needs_pwsh
def test_services_that_do_not_start_are_not_run_and_are_stopped(harness_far, monkeypatch):
    head, ref, data = harness_far.batch()
    monkeypatch.setenv("FAKE_HOOK_FAIL", "1")

    proc = harness_far.run_harness(head, ref, data)

    assert proc.returncode == 3
    assert "could not start this machine's services" in _verdict(proc), proc.stderr[-2000:]
    assert "did not start" in _verdict(proc)
    assert harness_far.hook_calls() == ["start", "stop"]


@needs_pwsh
def test_a_machine_without_a_services_hook_is_not_run(harness_far, tmp_path):
    head, ref, data = harness_far.batch()

    proc = harness_far.run_harness(head, ref, data, hook=tmp_path / "no-such-hook.ps1")

    assert proc.returncode == 3 and "no services hook" in _verdict(proc), proc.stderr[-2000:]
    assert harness_far.hook_calls() == []


@needs_pwsh
def test_the_harness_far_half_refuses_a_bundle_that_is_not_the_batch_head(harness_far):
    _, ref, data = harness_far.batch()
    other, _, _ = harness_far.batch()

    proc = harness_far.run_harness(other, ref, data)

    assert proc.returncode == 3 and "not the batch head" in _verdict(proc), proc.stderr[-2000:]
    assert harness_far.hook_calls() == [] and harness_far.harness_leftovers() == []


@needs_pwsh
def test_the_harness_far_half_tags_its_verdict_with_the_runs_nonce(harness_far):
    head, ref, data = harness_far.batch()
    nonce = "0a" * 16

    proc = harness_far.run_harness(head, ref, data, extra=("-Nonce", nonce))

    assert _verdict(proc) == f"rust-harness {nonce}: pytest exit 0", proc.stderr[-2000:]


@needs_pwsh
@pytest.mark.parametrize("outcome", ["pass", "fail", "empty"])
@pytest.mark.parametrize("runner", ["unittest-file", "unittest-discover"])
def test_harness_far_half_requires_every_unittest_command_to_execute_and_pass(harness_far, outcome, runner):
    body = "import unittest\n"
    if outcome != "empty":
        body += "class UnitCase(unittest.TestCase):\n    def test_unit(self):\n        self.assertTrue(" + str(outcome == "pass") + ")\n"
    body += "if __name__ == '__main__': unittest.main()\n"
    unit_name = "test_unit.py" if runner == "unittest-discover" else "unit_gate.py"
    head, ref, data = harness_far.batch(unit_body=body, unit_name=unit_name)
    units = [{"runner": runner, "paths": ["gate_tests/" + unit_name],
              "start": "gate_tests" if runner == "unittest-discover" else "",
              "pattern": "test_unit.py" if runner == "unittest-discover" else ""}]
    encoded = base64.b64encode(json.dumps(units).encode()).decode()
    proc = harness_far.run_harness(head, ref, data, extra=("-UnitCommands", encoded),
                                   pytest_path="gate_tests/test_gate.py")
    assert "1 passed" in proc.stdout and harness_far.hook_calls() == ["start", "stop"]
    assert proc.returncode == {"pass": 0, "fail": 1, "empty": 3}[outcome], proc.stdout + proc.stderr
    if outcome == "pass":
        assert "(unittest)" in proc.stdout and "Ran 1 test" in proc.stdout
    elif outcome == "empty":
        assert "no executed cases" in proc.stderr


@needs_pwsh
def test_harness_far_half_refuses_an_unknown_unit_runner_before_services(harness_far):
    head, ref, data = harness_far.batch()
    encoded = base64.b64encode(json.dumps([{"runner": "unknown", "paths": ["gate_tests/unit_gate.py"]}]).encode()).decode()
    proc = harness_far.run_harness(head, ref, data, extra=("-UnitCommands", encoded))
    assert proc.returncode == 3 and "unknown or empty unittest selection" in proc.stderr
    assert harness_far.hook_calls() == [] and not harness_far.uv_log.exists()


@needs_pwsh
@pytest.mark.parametrize("requirement", ["onnx>=1.22", "--index-url=https://packages.invalid"])
def test_harness_far_half_refuses_an_unpinned_fixture_requirement_before_services(harness_far, requirement):
    head, ref, data = harness_far.batch()
    encoded = base64.b64encode(json.dumps([requirement]).encode()).decode()
    proc = harness_far.run_harness(head, ref, data, extra=("-ExtraRequirements", encoded))
    assert proc.returncode == 3 and "literal package==version pins" in proc.stderr
    assert harness_far.hook_calls() == [] and not harness_far.uv_log.exists()


@needs_pwsh
def test_harness_far_half_scrubs_ambient_environment_before_member_code(harness_far, monkeypatch):
    seen = harness_far.tmp / "uv-environment.jsonl"
    monkeypatch.setenv("FAKE_UV_ENV_LOG", str(seen))
    for name in ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "SERVICE_DSN"):
        monkeypatch.setenv(name, "planted-disposable-value")
    monkeypatch.setenv("SOME_SERVICE", "https://user:disposable@example.com/x")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k must_not_select_anything")
    monkeypatch.setenv("PYTEST_PLUGINS", "must_not_load_this_plugin")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "1.94.0")
    hook_check = """
    if ($env:GH_TOKEN -or $env:SERVICE_DSN -or $env:PYTEST_ADDOPTS) { throw 'ambient environment reached the services hook' }
    $env:PSEUDOLIFE_TEST_PG_PASSWORD = 'hook-test-login'
"""
    harness_far.hook.write_text(_FAKE_HOOK.replace("$env:FAKE_SERVICE = 'up'", hook_check + "$env:FAKE_SERVICE = 'up'"), encoding="utf-8")
    head, ref, data = harness_far.batch(_GATE_TEST + """
    for name in ('GH_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'SERVICE_DSN', 'SOME_SERVICE',
                 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD'):
        assert name not in os.environ, name
    assert os.environ['PSEUDOLIFE_TEST_PG_PASSWORD'] == 'hook-test-login'
    assert os.environ['RUSTUP_TOOLCHAIN'] == '1.94.0'
""")
    proc = harness_far.run_harness(head, ref, data)
    observed = [json.loads(line) for line in seen.read_text(encoding="utf-8").splitlines()]
    assert observed and all(not any(row.values()) for row in observed), observed
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "1 passed" in proc.stdout and harness_far.hook_calls() == ["start", "stop"]


@needs_pwsh
@pytest.mark.skipif(os.name != "nt", reason="real Windows junctions")
@pytest.mark.parametrize("placement", ["root", "child", "nested"])
def test_harness_far_half_refuses_junctions_without_touching_the_target(harness_far, placement):
    head, ref, data = harness_far.batch()
    first = harness_far.run_harness(head, ref, data)
    assert first.returncode == 0, first.stderr
    gate = Path(f"{harness_far.repo}.rust-harness")
    target = harness_far.tmp / "junction-target"
    if placement == "root":
        gate.rename(target)
        junction = gate
    else:
        target.mkdir()
        junction = gate / ("untracked-junction" if placement == "child" else "untracked-directory/junction")
        junction.parent.mkdir(exist_ok=True)
    (target / "must-stay.txt").write_text("outside data\n", encoding="utf-8")
    before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    script = harness_far.tmp / "junction.ps1"
    script.write_text("param($path, $target)\nNew-Item -ItemType Junction -Path $path -Target $target -ErrorAction Stop | Out-Null\n", encoding="utf-8")
    made = subprocess.run([_PWSH, "-NoProfile", "-NonInteractive", "-File", str(script), str(junction), str(target)], capture_output=True, text=True, timeout=30)
    assert made.returncode == 0, made.stderr
    try:
        proc = harness_far.run_harness(head, ref, data)
        assert proc.returncode == 3, proc.stdout[-2000:] + proc.stderr[-2000:]
        reported = re.search(r"reparse point at '([^']+)'", proc.stderr)
        assert reported and os.path.samefile(reported.group(1), junction), proc.stderr
        assert "remove" in proc.stderr and "retry" in proc.stderr
        assert {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()} == before
    finally:
        if junction.exists():
            os.rmdir(junction)  # Remove the owned junction itself, never its target.
        if placement == "root":
            target.rename(gate)


@needs_pwsh
def test_harness_far_half_cleans_an_ordinary_untracked_file(harness_far):
    head, ref, data = harness_far.batch()
    first = harness_far.run_harness(head, ref, data)
    assert first.returncode == 0, first.stderr
    file = Path(f"{harness_far.repo}.rust-harness") / "ordinary-untracked.txt"
    file.write_text("clean this owned file\n", encoding="utf-8")
    proc = harness_far.run_harness(head, ref, data)
    assert proc.returncode == 0, proc.stderr
    assert not file.exists()


def _environment_cleanup_probe(tmp_path, environment, *, created=True, expected=None, run_id="a" * 32):
    """Exercise the real cleanup function without running a gate or its services."""
    script = tmp_path / "cleanup-probe.ps1"
    script.write_text('''
param($source, $path, $expected, $created, $id, $repo)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'source parse failed' }
foreach ($name in @('Assert-HarnessWorktreeHasNoReparsePoint', 'Get-HarnessLongPath', 'Remove-OwnedHarnessEnvironment')) {
    $definition = $ast.Find({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    . ([scriptblock]::Create($definition.Extent.Text))
}
Remove-OwnedHarnessEnvironment -EnvironmentPath $path -ExpectedPath ([IO.Path]::GetFullPath($expected)) -Created ($created -eq 'yes') -RunId $id -Repository $repo
''', encoding="utf-8")
    return subprocess.run([_PWSH, "-NoProfile", "-NonInteractive", "-File", str(script),
                           str(ROOT / "ops/rust-harness-remote.ps1"), str(environment),
                           str(expected or environment), "yes" if created else "no", run_id,
                           str(tmp_path / "repo")], capture_output=True, text=True, timeout=45)


@needs_pwsh
@pytest.mark.parametrize("case", ["not-created", "wrong-run-name", "wrong-path", "ordinary"])
def test_harness_environment_cleanup_removes_only_its_own_created_path(tmp_path, case):
    environment = tmp_path / ("repo.rust-harness-env-" + "a" * 32)
    environment.mkdir()
    (environment / "ordinary-file").write_text("owned", encoding="utf-8")
    run = _environment_cleanup_probe(tmp_path, environment, created=case != "not-created",
        expected=tmp_path / "another-path" if case == "wrong-path" else environment,
        run_id="b" * 32 if case == "wrong-run-name" else "a" * 32)
    if case in ("wrong-run-name", "wrong-path"):
        assert run.returncode != 0 and "path this run created" in run.stderr
    else:
        assert run.returncode == 0, run.stdout + run.stderr
    assert environment.exists() == (case != "ordinary")


@needs_pwsh
def test_harness_environment_cleanup_refuses_a_link_without_touching_its_target(tmp_path):
    environment = tmp_path / ("repo.rust-harness-env-" + "a" * 32)
    environment.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    content = target / "must-stay"
    content.write_text("outside data", encoding="utf-8")
    link = environment / "link"
    if os.name == "nt":
        script = tmp_path / "junction.ps1"
        script.write_text("param($path,$target)\nNew-Item -ItemType Junction -Path $path -Target $target -ErrorAction Stop | Out-Null\n", encoding="utf-8")
        made = subprocess.run([_PWSH, "-NoProfile", "-NonInteractive", "-File", str(script),
                               str(link), str(target)], capture_output=True, text=True, timeout=30)
        assert made.returncode == 0, made.stderr
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        run = _environment_cleanup_probe(tmp_path, environment)
        assert run.returncode != 0 and "reparse point" in run.stderr, run.stdout + run.stderr
        assert content.read_text(encoding="utf-8") == "outside data"
        assert environment.exists()
    finally:
        if os.path.lexists(link):
            os.rmdir(link) if os.name == "nt" else link.unlink()


@needs_pwsh
@pytest.mark.parametrize("alias", [False, True])
def test_harness_environment_cleanup_refuses_an_active_process(tmp_path, alias):
    import psutil

    environment = tmp_path / ("repo.rust-harness-env-" + "a" * 32)
    environment.mkdir()
    argument = str(environment)
    if alias:
        if os.name != "nt":
            pytest.skip("Windows 8.3 path aliases")
        import ctypes
        from ctypes import wintypes
        short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        short_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        short_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = short_path(str(environment.parent), buffer, len(buffer))
        assert 0 < length < len(buffer), ctypes.get_last_error()
        assert "~" in buffer.value and os.path.samefile(environment.parent, buffer.value)
        # The cleaner receives the short spelling, while the process names
        # the long spelling of this same directory.
        environment = Path(buffer.value) / environment.name
    # Use the base executable so this Popen owns the sleeper itself, rather
    # than a Windows venv launcher whose child would outlive terminate().
    process = subprocess.Popen([sys._base_executable, "-c", "import time; time.sleep(90)", argument],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    identity = psutil.Process(process.pid).create_time()
    try:
        run = _environment_cleanup_probe(tmp_path, environment)
        assert run.returncode != 0 and "process still uses" in run.stderr, run.stdout + run.stderr
        assert environment.exists()
    finally:
        if process.poll() is None:
            assert psutil.Process(process.pid).create_time() == identity
            process.terminate()
        process.wait(timeout=15)


@needs_pwsh
@pytest.mark.skipif(os.name != "nt", reason="Windows owned-job cleanup")
def test_harness_environment_cleanup_refuses_an_owned_child_without_an_argv_path(harness_far, monkeypatch):
    import psutil

    marker = harness_far.tmp / "indirect-child.json"
    monkeypatch.setenv("FAKE_INDIRECT_READY", str(marker))
    child_code = '''import json,os,psutil,time
from pathlib import Path
os.chdir(os.environ['FAKE_GATE_ENVIRONMENT'])
ready = Path(os.environ['FAKE_INDIRECT_READY'])
pending = ready.with_suffix('.writing')
pending.write_text(json.dumps({'pid':os.getpid(),'started':psutil.Process().create_time()}))
pending.replace(ready)
time.sleep(90)
'''
    body = _GATE_TEST + f'''
    import runpy, subprocess, sys, time
    runpy.run_path({str(ROOT / "tests/windowless_children.py")!r})['install']()
    env = dict(os.environ, FAKE_GATE_ENVIRONMENT=sys.prefix, PYTHONPATH=os.pathsep.join(sys.path))
    child = subprocess.Popen([sys._base_executable, '-c', {child_code!r}], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while not os.path.exists(os.environ['FAKE_INDIRECT_READY']) and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    assert os.path.exists(os.environ['FAKE_INDIRECT_READY'])
'''
    head, ref, data = harness_far.batch(body)
    proc = harness_far.run_harness(head, ref, data)
    assert proc.returncode == 3 and "owned run process remains" in proc.stderr, proc.stdout + proc.stderr
    # Refuse before deleting even the first file, then the owned job ends
    # the leaked child when its launcher exits. No name-based cleanup.
    environment = Path(json.loads(harness_far.uv_log.read_text().splitlines()[0])[-1])
    assert (environment / "pyvenv.cfg").is_file()
    assert (environment / "Scripts/python.exe").is_file()
    identity = json.loads(marker.read_text())
    try:
        child = psutil.Process(identity["pid"])
        assert child.create_time() == identity["started"]
        child.wait(timeout=15)
    except psutil.NoSuchProcess:
        pass


@needs_pwsh
def test_harness_controller_ships_its_lock_helper_over_stdin(harness_far, monkeypatch, tmp_path, capsys):
    head, ref, _ = harness_far.batch()
    monkeypatch.chdir(harness_far.seed)
    ssh = tmp_path / "local_ssh.py"
    ssh.write_text(_LOCAL_SSH, encoding="utf-8")
    monkeypatch.setattr(integrate, "RUST_CHECK_SSH", (sys.executable, str(ssh)))
    plan = integrate.Plan(branch=ref.removeprefix("refs/heads/"), base_sha=harness_far.base,
                          label=LABEL, head=head, rust_check="passed")
    monkeypatch.setattr(integrate, "_built_batch", lambda: (plan.branch, plan, head))
    cfg = {"ssh": "dev-vm", "repo": str(harness_far.repo), "role": "Windows dev VM"}
    monkeypatch.setattr(integrate, "_remote_config", lambda *args: (cfg, ""))
    script = integrate.RUST_HARNESS_REMOTE_SCRIPT.replace("$ServicesHook = ''", "$ServicesHook = " + integrate._ps_literal(str(harness_far.hook)))
    script = script.replace("$DatabaseLockPath = ''", "$DatabaseLockPath = " + integrate._ps_literal(str(tmp_path / "own-database.lock")))
    script = script.replace("$TurnMarkerPath = ''", "$TurnMarkerPath = " + integrate._ps_literal(str(harness_far.turn_marker)))
    monkeypatch.setattr(integrate, "RUST_HARNESS_REMOTE_SCRIPT", script)
    monkeypatch.setattr(integrate, "RUST_HARNESS", ("gate_tests",))
    monkeypatch.setattr(integrate, "RUST_HARNESS_COMMAND", "python -m pytest -q -p no:cacheprovider --require-databases gate_tests")
    monkeypatch.setattr(integrate, "_harness_recipe", lambda head: _model_harness_recipe(("gate_tests",)))
    monkeypatch.setattr(integrate, "SUITE_RESULTS", tmp_path / "results")
    assert integrate.main(["harness", "--lock-wait-seconds", "0"]) == 0, capsys.readouterr()
    result, = integrate.SUITE_RESULTS.glob("*.json")
    assert "passed" in integrate._gate_result_line(str(result), head, "rust-harness")
    assert harness_far.hook_calls() == ["start", "stop"]
    assert harness_far.harness_leftovers() == []
    environments = [json.loads(line)[-1] for line in harness_far.uv_log.read_text(encoding="utf-8").splitlines()
                    if json.loads(line)[0] == "venv"]
    assert environments and all(not Path(path).exists() for path in environments)


@needs_pwsh
def test_harness_environment_cannot_retain_a_removed_dependency(harness_far):
    first, first_ref, first_bundle = harness_far.batch("""
def test_install_old_dependency():
    from pathlib import Path
    import site
    (Path(site.getsitepackages()[0]) / 'removed_gate_dependency.py').write_text('old = True\\n')
""")
    old = harness_far.run_harness(first, first_ref, first_bundle)
    assert old.returncode == 0, old.stdout[-2000:] + old.stderr[-2000:]
    second, second_ref, second_bundle = harness_far.batch("""
def test_removed_dependency_is_not_available():
    import importlib.util
    assert importlib.util.find_spec('removed_gate_dependency') is None
""")
    new = harness_far.run_harness(second, second_ref, second_bundle)
    assert new.returncode == 0, new.stdout[-2000:] + new.stderr[-2000:]
    environments = [Path(row[-1]) for row in map(json.loads, harness_far.uv_log.read_text(encoding="utf-8").splitlines())
                    if row[0] == "venv"]
    assert len(environments) == len(set(environments)) == 2
    assert all(not path.exists() for path in environments)


@needs_pwsh
def test_service_managing_harness_refuses_a_missing_turn_marker(harness_far):
    head, ref, data = harness_far.batch()
    harness_far.turn_marker.unlink()
    proc = harness_far.run_harness(head, ref, data)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "marker is missing" in proc.stderr and "New-Item -ItemType File" in proc.stderr
    assert harness_far.hook_calls() == [] and not harness_far.uv_log.exists()


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="Linux shim far half")
def test_shim_controller_sends_its_script_and_bundle_over_stdin(far, monkeypatch, tmp_path, capsys):
    head, ref, _ = far.batch()
    monkeypatch.chdir(far.seed)
    ssh = tmp_path / "local_ssh.py"
    ssh.write_text(_LOCAL_SSH, encoding="utf-8")
    monkeypatch.setattr(integrate, "RUST_CHECK_SSH", (sys.executable, str(ssh)))
    plan = integrate.Plan(branch=ref.removeprefix("refs/heads/"), base_sha=far.base,
                          label=LABEL, head=head, rust_check="passed")
    monkeypatch.setattr(integrate, "_built_batch", lambda: (plan.branch, plan, head))
    cfg = {"ssh": "dev-box-two", "repo": str(far.repo), "role": "second Linux dev box"}
    monkeypatch.setattr(integrate, "_remote_config", lambda *args: (cfg, ""))
    monkeypatch.setattr(integrate, "SUITE_RESULTS", tmp_path / "results")
    assert integrate.main(["shim-tests", "--lock-wait-seconds", "0"]) == 0, capsys.readouterr()
    result, = integrate.SUITE_RESULTS.glob("*.json")
    assert "passed" in integrate._gate_result_line(str(result), head, "shim-tests")
    assert not any(c["suite_lock_free"] for c in far.cargo_calls() if c["argv"][:2] == ["nextest", "run"])


def _lock_command(lock, *command, wait=0):
    return [_PWSH, "-NoProfile", "-NonInteractive", "-File",
            str(ROOT / "ops/dev-database-lock.ps1"), "-LockPath", str(lock),
            "-Holder", "test worker", "-WaitSeconds", str(wait), "--", *command]


@needs_pwsh
@pytest.mark.parametrize("failure", ["none", "cleanup", "disk"])
def test_harness_database_lock_covers_services_and_releases_after_failure(harness_far, failure, monkeypatch):
    lock = harness_far.tmp / "own-database.lock"
    monkeypatch.setenv("FAKE_DATABASE_LOCK", str(lock))
    monkeypatch.setenv("FAKE_LOCK_HELPER", str(ROOT / "ops/dev-database-lock.ps1"))
    monkeypatch.setenv("FAKE_PWSH", _PWSH)
    # Both service boundaries must be inside the same lock as the tests.
    check = r'''
    & $env:FAKE_PWSH -NoProfile -NonInteractive -File $env:FAKE_LOCK_HELPER -LockPath $env:FAKE_DATABASE_LOCK -WaitSeconds 0 -- $env:FAKE_PWSH -NoProfile -NonInteractive -Command 'exit 0' 2>$null
    if ($LASTEXITCODE -ne 3) { throw 'database lock was not held' }
'''
    hook = _FAKE_HOOK.replace("$env:FAKE_SERVICE = 'up'", check + "$env:FAKE_SERVICE = 'up'")
    hook = hook.replace("function Stop-HarnessServices {", "function Stop-HarnessServices {" + check)
    if failure == "cleanup":
        hook += "\nfunction Stop-HarnessServices { throw 'cleanup failed' }\n"
    harness_far.hook.write_text(hook, encoding="utf-8")
    body = _GATE_TEST + '''
    import subprocess
    p = subprocess.run([os.environ["FAKE_PWSH"], "-NoProfile", "-NonInteractive", "-File",
        os.environ["FAKE_LOCK_HELPER"], "-LockPath", os.environ["FAKE_DATABASE_LOCK"],
        "-WaitSeconds", "0", "--", "missing-command-must-not-run"], capture_output=True, text=True)
    assert p.returncode == 3 and "wait exhausted" in p.stderr
'''
    head, ref, data = harness_far.batch(body)
    proc = harness_far.run_harness(head, ref, data, extra=("-MinFreeGB", "1000000") if failure == "disk" else ())
    assert proc.returncode == (0 if failure == "none" else 3), proc.stdout + proc.stderr
    if failure == "cleanup":
        assert "stopping the services failed" in _verdict(proc)
    if failure == "disk":
        assert "floor" in _verdict(proc) and harness_far.hook_calls() == []
    again = subprocess.run(_lock_command(lock, sys.executable, "-c", "pass"), capture_output=True, text=True, timeout=30)
    assert again.returncode == 0, again.stderr
    _hold_suite_lock(harness_far.locks / "full-suite.lock").close()


@needs_pwsh
def test_harness_waiting_for_worker_database_turn_leaves_suite_lock_free(harness_far, tmp_path):
    import time

    ready, release = tmp_path / "worker-ready", tmp_path / "worker-release"
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        "from pathlib import Path\nimport time\n"
        f"Path({str(ready)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.05)\n",
        encoding="utf-8")
    lock = tmp_path / "own-database.lock"
    head, ref, data = harness_far.batch()
    command = [_PWSH, "-NoProfile", "-NonInteractive", "-File",
               str(ROOT / "ops/rust-harness-remote.ps1"), "-Repo", str(harness_far.repo),
               "-Base", harness_far.base, "-Head", head, "-Ref", ref,
               "-ServicesHook", str(harness_far.hook), "-DatabaseLockPath", str(lock),
               "-TurnMarkerPath", str(harness_far.turn_marker),
               "-LockWaitSeconds", "60", "-DatabaseTurnWaitSeconds", "60", "-PytestArgs", "gate_tests"]
    gate = None
    output = tmp_path / "gate.log"
    with (tmp_path / "worker.log").open("w", encoding="utf-8") as worker_log, output.open("w", encoding="utf-8") as gate_log:
        worker = subprocess.Popen(_lock_command(lock, sys.executable, str(worker_script)),
                                  stdout=worker_log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while not ready.exists() and worker.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            assert ready.exists(), (tmp_path / "worker.log").read_text(encoding="utf-8")
            gate = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=gate_log,
                                    stderr=subprocess.STDOUT, text=True)
            gate.stdin.write(data)
            gate.stdin.close()
            deadline = time.monotonic() + 30
            while "database lock: waiting" not in output.read_text(encoding="utf-8") and gate.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            assert "database lock: waiting" in output.read_text(encoding="utf-8")
            # A wrapped full suite reaches this lock after taking its database
            # turn. The queued harness must not hold it while waiting on us.
            _hold_suite_lock(harness_far.locks / "full-suite.lock").close()
            release.touch()
            assert worker.wait(timeout=30) == 0
            assert gate.wait(timeout=180) == 0, output.read_text(encoding="utf-8")
            assert harness_far.hook_calls() == ["start", "stop"]
        finally:
            release.touch()
            if worker.poll() is None:
                worker.wait(timeout=30)
            if gate is not None and gate.poll() is None:
                gate.kill()  # the owned Popen handle, never a process-name search
                gate.wait(timeout=30)


@needs_pwsh
def test_database_lock_excludes_another_worker_and_names_holder(tmp_path):
    import time
    lock = tmp_path / "own-database.lock"
    marker = tmp_path / "entered"
    script = tmp_path / "hold.ps1"
    script.write_text("[IO.File]::WriteAllText($args[0], 'ready'); Start-Sleep -Seconds 60", encoding="utf-8")
    with subprocess.Popen(_lock_command(lock, str(script), str(marker)),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as holder:
        try:
            deadline = time.monotonic() + 30
            while not marker.exists() and holder.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            assert marker.exists(), holder.communicate(timeout=5)
            run = subprocess.run(_lock_command(lock, sys.executable, "-c", "print('overlapped')", wait=1),
                                 capture_output=True, text=True, timeout=30)
            assert run.returncode == 3
            assert "test worker" in run.stderr and "held for" in run.stderr
            assert "overlapped" not in run.stdout
        finally:
            if holder.poll() is None:
                holder.kill()  # this Popen owns the process handle, never a name search
            holder.communicate(timeout=10)
    # The killed owner's PID and creation time are now verifiably stale.
    resumed = subprocess.run(_lock_command(lock, sys.executable, "-c", "print('resumed')"),
                             capture_output=True, text=True, timeout=30)
    assert resumed.returncode == 0, resumed.stderr
    assert "resumed" in resumed.stdout


@needs_pwsh
@pytest.mark.parametrize("code", [0, 7])
def test_database_lock_wrapper_preserves_exit_and_releases(tmp_path, code):
    lock = tmp_path / "own-database.lock"
    run = subprocess.run(_lock_command(lock, sys.executable, "-c", f"import sys; sys.exit({code})"),
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == code, run.stderr
    again = subprocess.run(_lock_command(lock, sys.executable, "-c", "pass"),
                           capture_output=True, text=True, timeout=30)
    assert again.returncode == 0, again.stderr


@needs_pwsh
@pytest.mark.skipif(os.name != "nt", reason="Windows process lifetime after TerminateProcess")
@pytest.mark.parametrize("entrypoint", ["worker", "harness"])
def test_database_lock_owner_death_stops_its_child_before_reuse(harness_far, tmp_path, entrypoint):
    import time
    import psutil
    lock = tmp_path / "own-database.lock"
    marker = tmp_path / "child.json"
    child_code = ("import json, os, psutil, time\n"
                  f"with open({str(marker)!r}, 'w') as f:\n"
                  "    json.dump({'pid': os.getpid(), 'started': psutil.Process().create_time()}, f)\n"
                  "time.sleep(90)\n")
    if entrypoint == "worker":
        script = tmp_path / "child.py"
        script.write_text(child_code, encoding="utf-8")
        command = _lock_command(lock, sys.executable, str(script))
        data = None
    else:
        head, ref, data = harness_far.batch("def test_child():\n" + "\n".join("    " + line for line in child_code.splitlines()) + "\n")
        command = [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(ROOT / "ops/rust-harness-remote.ps1"),
                   "-Repo", str(harness_far.repo), "-Base", harness_far.base, "-Head", head, "-Ref", ref,
                   "-ServicesHook", str(harness_far.hook), "-DatabaseLockPath", str(lock),
                   "-TurnMarkerPath", str(harness_far.turn_marker), "-PytestArgs", "gate_tests"]
    child = None
    identity = None
    with (tmp_path / "owner.log").open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True) as owner:
            try:
                if data:
                    owner.stdin.write(data)
                owner.stdin.close()
                deadline = time.monotonic() + 60
                while not marker.exists() and owner.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                assert marker.exists(), (tmp_path / "owner.log").read_text(encoding="utf-8")
                # The child closes the marker before sleeping. Retry a partial write.
                for _ in range(100):
                    try:
                        identity = json.loads(marker.read_text())
                        break
                    except ValueError:
                        time.sleep(.01)
                assert identity
                child = psutil.Process(identity["pid"])
                assert child.create_time() == identity["started"]
                owner.kill()  # this exact Popen handle, never a process-name search
                owner.wait(timeout=10)
                child.wait(timeout=10)
                resumed = subprocess.run(_lock_command(lock, sys.executable, "-c", "pass"), capture_output=True, text=True, timeout=30)
                assert resumed.returncode == 0, resumed.stderr
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=10)
                if child is not None:
                    try:
                        if child.is_running() and child.create_time() == identity["started"]:
                            child.kill()
                            child.wait(timeout=10)
                    except psutil.NoSuchProcess:
                        pass


@needs_pwsh
@pytest.mark.parametrize("state", ["alive", "reused", "unreadable"])
def test_database_lock_free_handle_overwrites_old_metadata(tmp_path, state):
    lock = tmp_path / "own-database.lock"
    script = tmp_path / "identity.ps1"
    script.write_text("""
param($helper, $lock, $state)
. $helper
$record = @{ holder='previous worker'; pid=$PID; process_started=[Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().Ticks.ToString(); acquired=[DateTimeOffset]::UtcNow.AddDays(-10).ToString('o') }
if ($state -eq 'reused') { $record.process_started = ([long]$record.process_started - 1).ToString() }
if ($state -eq 'unreadable') { $record.Remove('process_started') }
[IO.File]::WriteAllText($lock, ($record | ConvertTo-Json -Compress))
try {
    $hold = Enter-DevDatabaseLock -Path $lock -Holder 'next worker' -WaitSeconds 0
    Exit-DevDatabaseLock $hold
    exit 0
} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 3 }
""", encoding="utf-8")
    run = subprocess.run([_PWSH, "-NoProfile", "-NonInteractive", "-File", str(script),
                          str(ROOT / "ops/dev-database-lock.ps1"), str(lock), state],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert lock.read_bytes() == b""


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="Linux shim far half")
@pytest.mark.parametrize("mismatch", [False, True])
def test_shim_far_half_checks_head_and_holds_the_suite_lock(far, mismatch):
    head, ref, data = far.batch()
    if mismatch:
        head, _, _ = far.batch()
    proc = subprocess.run(["bash", str(ROOT / "ops/shim-tests-remote.sh"), str(far.repo), far.base,
                           head, ref, "ab" * 16, "0"], input=data, capture_output=True,
                          text=True, timeout=60)
    if mismatch:
        assert proc.returncode == 3 and "batch head" in proc.stderr, proc.stderr
        assert not any(call["argv"][:2] == ["nextest", "run"] for call in far.cargo_calls())
    else:
        assert proc.returncode == 0, proc.stderr
        call = [c for c in far.cargo_calls() if c["argv"][:2] == ["nextest", "run"]][0]
        assert call["argv"] == list(integrate.SHIM_TESTS_ARGS)
        assert not call["suite_lock_free"]
        assert call["target"] == str(far.repo) + ".shim-tests-target"
        assert "Summary [" in proc.stderr
        assert _verdict(proc) == "shim-tests " + "ab" * 16 + ": nextest exit 0"
    assert not list(far.tmp.glob("shim-tests-*.bundle"))


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="Linux shim far half")
@pytest.mark.parametrize("cause", ["busy", "disk", "nextest"])
def test_shim_far_half_refuses_unavailable_resources(far, monkeypatch, cause):
    head, ref, data = far.batch()
    held = _hold_suite_lock(far.locks / "full-suite.lock") if cause == "busy" else None
    if cause == "disk":
        fake = far.tmp / "bin" / "df"
        fake.write_text("#!/bin/sh\nprintf 'Avail\\n1\\n'\n", encoding="utf-8")
        fake.chmod(0o755)
    if cause == "nextest":
        monkeypatch.setenv("FAKE_FAR_CARGO_EXIT", "101")
    try:
        proc = subprocess.run(["bash", str(ROOT / "ops/shim-tests-remote.sh"), str(far.repo), far.base,
                               head, ref, "ab" * 16, "0"], input=data, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert {"busy": "lock wait exhausted", "disk": "under 8 GB", "nextest": "cargo-nextest is missing"}[cause] in proc.stderr
        assert not any(call["argv"][:2] == ["nextest", "run"] for call in far.cargo_calls())
    finally:
        if held:
            held.close()
    _hold_suite_lock(far.locks / "full-suite.lock").close()


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="Linux shim far half")
def test_shim_far_half_refuses_loaded_tmpfs_before_running_member_code(far):
    head, ref, data = far.batch()
    for name, body in {"stat": "printf 'tmpfs\\n'\n",
                       "df": "printf 'Used\\n5368709120\\n'\n"}.items():
        path = far.tmp / "bin" / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
    proc = subprocess.run(["bash", str(ROOT / "ops/shim-tests-remote.sh"), str(far.repo), far.base,
                           head, ref, "ab" * 16, "0"], input=data, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "memory-backed" in proc.stderr and "above 4 GiB" in proc.stderr
    assert "remove only your owned" in proc.stderr and "retry" in proc.stderr
    assert far.cargo_calls() == []
    assert not Path(str(far.repo) + ".shim-tests").exists()
    assert not list(far.tmp.glob("shim-tests-*.bundle"))


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="Linux shim far half")
def test_shim_far_half_scrubs_ambient_environment_before_member_code(far, monkeypatch):
    head, ref, data = far.batch()
    for name in ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "SERVICE_DSN"):
        monkeypatch.setenv(name, "planted-disposable-value")
    monkeypatch.setenv("SOME_SERVICE", "https://user:disposable@example.com/x")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k must_not_select_anything")
    monkeypatch.setenv("NEXTEST_FILTER_EXPR", "test(must_not_select_anything)")
    monkeypatch.setenv("NEXTEST_PROFILE", "must-not-exist")
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "1.94.0")
    proc = subprocess.run(["bash", str(ROOT / "ops/shim-tests-remote.sh"), str(far.repo), far.base,
                           head, ref, "ab" * 16, "0"], input=data, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    call = next(c for c in far.cargo_calls() if c["argv"][:2] == ["nextest", "run"])
    visible = set(call["env"])
    for name in ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "SERVICE_DSN", "SOME_SERVICE",
                 "PYTEST_ADDOPTS", "NEXTEST_FILTER_EXPR", "NEXTEST_PROFILE"):
        assert name not in visible, name
    assert call["env"]["RUSTUP_TOOLCHAIN"] == "1.94.0"


# The real census module with only its snapshot cut down to its input
# hashes and manual drift (the real SCOPES, EXTENSIONS, producer_paths and
# manual_drift, with no contract sources), so the census `build` writes and
# the `--ci-check` decision `open` makes are the real ones.
_CENSUS_STUB = """\
import functools, importlib.util, os
from pathlib import Path

if __name__ == "__main__" and "GH_TOKEN" in os.environ:
    raise SystemExit("the census ran with the operator's GH_TOKEN")
spec = importlib.util.spec_from_file_location("census_real", {real!r})
census = importlib.util.module_from_spec(spec)
spec.loader.exec_module(census)
ROOT = Path(__file__).resolve().parents[1]
CURRENCY_INPUTS = census.CURRENCY_INPUTS
census.CONTRACT_SOURCES = CONTRACT_SOURCES = ()
census.MANUAL_SHA256 = MANUAL_SHA256 = {{}}
producer_paths = census.producer_paths
census.OUTPUT = ROOT / "rust/producer-census.json"
census.snapshot = lambda: {{
    "input_sha256": census.input_hashes(ROOT, census.producer_paths(ROOT)),
    "manual_drift": census.manual_drift(ROOT),
    "surface": [], "inventory_gaps": [], "no_observed_producer": [], "unresolved": []}}
census.ci_currency_required = functools.partial(census.ci_currency_required, ROOT)

if __name__ == "__main__":
    census.main()
"""


def _stub(manual: dict[str, str] | None = None) -> str:
    text = _CENSUS_STUB.format(real=str(ROOT / "rust" / "producer_census.py"))
    if manual:
        text = text.replace("MANUAL_SHA256 = {}", f"MANUAL_SHA256 = {manual!r}")
    return text


def _census(**scanned: str) -> str:
    """The census `rust/producer_census.py` would write for these `ops/*.sh`
    (a name with a slash is a path as it stands)."""
    import hashlib
    hashes = {(name if "/" in name else f"ops/{name}.sh"):
              hashlib.sha256(text.encode()).hexdigest()
              for name, text in sorted(scanned.items())}
    return json.dumps({"input_sha256": hashes, "manual_drift": [], "surface": [],
                       "inventory_gaps": [], "no_observed_producer": [],
                       "unresolved": []}, indent=2) + "\n"


def _with_census(world, stub: str | None = None) -> None:
    """Master gains the census module, the port inventory it reads, a
    scanned `ops/tool.sh` and a census current for it."""
    world.advance_master({"rust/producer_census.py": stub or _stub(), "ops/tool.sh": "t\n",
                          "rust/producer-census.json": _census(tool="t\n"),
                          "rust/contract-inventory.json": "{}\n"})
    if stub:   # a custom stub writes its own census (or none, if it is broken)
        census = world.seed / "rust" / "producer-census.json"
        before = census.read_text(encoding="utf-8")
        subprocess.run([sys.executable, "rust/producer_census.py"], cwd=world.seed,
                       capture_output=True)
        after = census.read_text(encoding="utf-8")
        if after != before:
            world.advance_master({"rust/producer-census.json": after})


def _census_commits(origin: Path, branch: str) -> list[tuple[str, str, list[str]]]:
    """(sha, subject, files) of the batch's own commits that merge nothing."""
    log = _git(origin, "log", "--first-parent", "--no-merges", "--format=%H %s",
               f"master..{branch}")
    commits = []
    for line in reversed(log.splitlines()):
        sha, subject = line.split(" ", 1)
        files = _git(origin, "show", "--name-only", "--format=", sha).splitlines()
        commits.append((sha, subject, files))
    return commits


def _census_at(origin: Path, branch: str) -> str:
    return _git(origin, "show", f"{branch}:rust/producer-census.json") + "\n"


def test_the_batch_regenerates_a_census_its_members_staled(world, capsys):
    # #960 and #974 on 2026-10-26: a member touching a hashed file had to
    # regenerate the census, and then went stale whenever master moved.
    # Now no member regenerates it; the batch does, in one commit of its own.
    _with_census(world)
    sha10 = world.pr(10, {"ops/ten.sh": "10\n"})
    sha11 = world.pr(11, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == [sha10, sha11]
    (sha, subject, files), = _census_commits(world.origin, branch)
    assert subject == integrate.CENSUS_SUBJECT and files == ["rust/producer-census.json"]
    assert _census_at(world.origin, branch) == _census(tool="t\n", ten="10\n")
    assert world.gh.bodies("comment") == [] and world.gh.bodies("edit") == []
    (_, create_body), = world.gh.bodies("create")
    assert "## Generated CI metadata" in create_body and sha in create_body
    assert f"regenerated CI metadata in {sha[:12]}" in capsys.readouterr().out
    # The generated census alone is no Rust change: nothing to compile.
    assert world.cargo_calls() == []
    assert "changes nothing under `rust/`" in create_body


def test_a_member_whose_census_master_staled_still_lands(world, capsys):
    # #865 on 2026-10-20 and #974 three times on 2026-10-26: a member's own
    # regenerated census went stale when master moved, and the batch dropped
    # or held it. Regenerating on the batch head keeps every member in.
    _with_census(world)
    sha10 = world.pr(10, {"ops/ten.sh": "10\n",
                          "rust/producer-census.json": _census(tool="t\n", ten="10\n")})
    world.advance_master({"ops/other.sh": "o\n"})        # census now stale on master
    sha11 = world.pr(11, {"ops/eleven.sh": "11\n"})
    sha12 = world.pr(12, {"rust/contract-inventory.json": '{"tools": []}\n'})
    for n, sha in ((10, sha10), (11, sha11), (12, sha12)):
        world.gh.add(n, sha)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == [sha10, sha11, sha12]
    assert len(_census_commits(world.origin, branch)) == 1
    assert _census_at(world.origin, branch) == _census(
        tool="t\n", ten="10\n", other="o\n", eleven="11\n")
    assert world.gh.bodies("comment") == [] and world.gh.bodies("edit") == []


def _conflicting_censuses(world, *, source_conflict=False, broken=False):
    _with_census(world)
    first = {"ops/ten.sh": "10\n",
             "rust/producer-census.json": _census(tool="t\n", ten="10\n")}
    second = {"ops/eleven.sh": "11\n",
              "rust/producer-census.json": _census(tool="t\n", eleven="11\n")}
    if source_conflict:
        first["a.txt"], second["a.txt"] = "first\n", "second\n"
    if broken:
        second["rust/producer_census.py"] = (
            'raise SystemExit("conflicting census regeneration broken")\n' + _stub())
    heads = [world.pr(10, first), world.pr(11, second)]
    for number, sha in zip((10, 11), heads):
        world.gh.add(number, sha)
    return heads


def test_census_only_conflicts_are_regenerated_and_keep_the_batch_shape(world, capsys):
    heads = _conflicting_censuses(world)
    assert _publish(world) == 0, capsys.readouterr()
    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == heads
    (sha, subject, files), = _census_commits(world.origin, branch)
    assert subject == integrate.CENSUS_SUBJECT and files == [integrate.CENSUS_JSON]
    expected = _census(tool="t\n", ten="10\n", eleven="11\n")
    assert _census_at(world.origin, branch) == expected
    subprocess.run([sys.executable, integrate.CENSUS], cwd=world.work, check=True,
                   capture_output=True)
    assert (world.work / integrate.CENSUS_JSON).read_text(encoding="utf-8") == expected
    assert _git(world.work, "status", "--porcelain") == ""
    assert _plan(world)["census_conflicts"] == [11]
    assert "Regenerated census conflict for #11" in capsys.readouterr().out
    (_, created), = world.gh.bodies("create")
    assert "Regenerated census conflict for #11" in created
    assert not world.gh.bodies("comment")
    assert integrate.main(["status"]) == 0
    assert "Regenerated census conflict for #11" in capsys.readouterr().out
    # A valid published batch still refuses a second generated non-merge commit.
    _git(world.work, "commit", "--allow-empty", "-qm", integrate.CENSUS_SUBJECT)
    plan = integrate.Plan.load(branch)
    plan.head = _git(world.work, "rev-parse", "HEAD")
    plan.save()
    assert _open(world) == 2
    assert "neither a member merge nor the one generated commit" in capsys.readouterr().err


def test_census_plus_source_conflict_still_drops_the_member(world, capsys):
    first, second = _conflicting_censuses(world, source_conflict=True)
    assert _publish(world) == 0, capsys.readouterr()
    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == [first]
    (args, comment), = world.gh.bodies("comment")
    assert args[2] == "11" and "a.txt" in comment and "conflicts with #10" in comment
    assert _census_at(world.origin, branch) == _census(tool="t\n", ten="10\n")


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_census_conflict_rejects_non_regular_index_stages(world, monkeypatch, stage):
    _with_census(world)
    original = integrate._out

    def index(*args):
        if args == ("ls-files", "--stage", "--", integrate.CENSUS_JSON):
            return "\n".join(f"{'120000' if n == stage else '100644'} {'a' * 40} {n}\t"
                             + integrate.CENSUS_JSON for n in (1, 2, 3))
        return original(*args)

    monkeypatch.setattr(integrate, "_out", index)
    assert not integrate._merge_census_conflict(
        _git(world.work, "rev-parse", "HEAD"), [integrate.CENSUS_JSON])
    assert _git(world.work, "status", "--porcelain") == ""


def test_census_conflict_regeneration_failure_rebuilds_without_resolved_members(world, capsys):
    _conflicting_censuses(world, broken=True)
    third = world.pr(12, {"b.txt": "retained\n"})
    world.gh.add(12, third)
    assert _publish(world) == 0, capsys.readouterr()
    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == [third]
    assert _census_at(world.origin, branch) == _census(tool="t\n")
    comments = {args[2]: text for args, text in world.gh.bodies("comment")}
    assert set(comments) == {"10", "11"}
    assert all("conflicting census regeneration broken" in text for text in comments.values())
    assert not _plan(world).get("census_conflicts")
    (_, created), = world.gh.bodies("create")
    assert "Regenerated census conflict" not in created


@pytest.mark.parametrize("files", [
    {"b.txt": "b\n"},                                   # nothing the census hashes
    {"ops/tool.sh.bak": "x\n"},                         # a near miss beside a hashed file
    {"ops/ten.sh": "10\n",                              # a member with a current census
     "rust/producer-census.json": _census(tool="t\n", ten="10\n")},
])
def test_a_current_census_adds_no_commit(world, capsys, files):
    _with_census(world)
    sha10 = world.pr(10, files)
    world.gh.add(10, sha10)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == [sha10]
    assert _census_commits(world.origin, branch) == []
    (_, create_body), = world.gh.bodies("create")
    assert "## Generated CI metadata" not in create_body


def test_the_census_runs_without_the_operators_credentials(world, capsys, monkeypatch):
    # Member code: the stub exits non-zero if it sees GH_TOKEN, which `build`
    # would take as a broken census and refuse.
    monkeypatch.setenv("GH_TOKEN", "operator-secret")
    _with_census(world)
    sha10 = world.pr(10, {"ops/ten.sh": "10\n"})
    world.gh.add(10, sha10)

    assert _publish(world) == 0, capsys.readouterr()
    branch = f"integrate/{DATE}-batch-1"
    assert _census_at(world.origin, branch) == _census(tool="t\n", ten="10\n")


def test_a_member_that_breaks_the_census_is_dropped(world, capsys):
    _with_census(world)
    sha10 = world.pr(10, {"rust/producer_census.py":
                          'raise SystemExit("census broken")\n' + _stub()})
    sha11 = world.pr(11, {"ops/eleven.sh": "11\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == [sha11]
    assert _census_at(world.origin, branch) == _census(tool="t\n", eleven="11\n")
    (args, body), = world.gh.bodies("comment")
    assert args[2] == "10" and "rust/producer_census.py" in body
    assert "census broken" in body
    (edit_args, _), = world.gh.bodies("edit")
    assert edit_args[2] == "10"


def test_a_census_breaker_a_conflict_hid_is_dropped_too(world, capsys):
    # #10 and #11 both break the census script and conflict with each other,
    # so #11 merges only once #10 has left; it must leave too, and #12 lands.
    _with_census(world)
    sha10 = world.pr(10, {"rust/producer_census.py":
                          'raise SystemExit("broken by 10")\n' + _stub()})
    sha11 = world.pr(11, {"rust/producer_census.py":
                          'raise SystemExit("broken by 11")\n' + _stub()})
    sha12 = world.pr(12, {"ops/twelve.sh": "12\n"})
    for n, sha in ((10, sha10), (11, sha11), (12, sha12)):
        world.gh.add(n, sha)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == [sha12]
    comments = {args[2]: body for args, body in world.gh.bodies("comment")}
    assert set(comments) == {"10", "11"}
    assert "broken by 10" in comments["10"] and "broken by 11" in comments["11"]


def test_a_member_deleting_the_census_gets_it_back_in_the_batch(world, capsys):
    # The census is the batch's to write: a member deleting it does not
    # leave the batch without one (an untracked rewrite would never be pushed).
    _with_census(world)
    (world.seed / "rust" / "producer-census.json").unlink()
    sha10 = world.pr(10, {"ops/ten.sh": "10\n"})
    world.gh.add(10, sha10)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert _census_at(world.origin, branch) == _census(tool="t\n", ten="10\n")


def test_a_census_broken_on_master_refuses_the_build(world, capsys):
    # No member changes the census inputs, so no member is to blame.
    _with_census(world, 'raise SystemExit("census broken")\n')
    sha10 = world.pr(10, {"ops/ten.sh": "10\n"})
    world.gh.add(10, sha10)

    assert _build() == 2
    err = capsys.readouterr().err

    assert "census broken" in err and "master" in err
    assert _git(world.work, "branch", "--list", "integrate/*") == ""


def test_open_refuses_a_batch_whose_census_is_stale(world, capsys, monkeypatch):
    # A plan built without regenerating the census (an older script) is
    # caught before the push: CI gates every batch on census currency.
    _with_census(world)
    sha10 = world.pr(10, {"ops/ten.sh": "10\n"})
    world.gh.add(10, sha10)
    real = integrate._regenerate_census
    monkeypatch.setattr(integrate, "_regenerate_census", lambda plan: None)
    assert _build() == 0
    monkeypatch.setattr(integrate, "_regenerate_census", real)
    capsys.readouterr()

    assert _open(world) == 2
    err = capsys.readouterr().err

    assert "census" in err and "build again" in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_without_a_census_on_master_nothing_is_regenerated(world, capsys):
    sha10 = world.pr(10, {"rust/producer_census.py": _stub(), "ops/tool.sh": "t\n",
                          "rust/producer-census.json": _census(tool="t\n"),
                          "rust/contract-inventory.json": "{}\n"})
    sha11 = world.pr(11, {"ops/eleven.sh": "11\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == [sha10, sha11]
    assert _census_commits(world.origin, branch) == []


def test_the_batch_lists_the_manual_shapes_its_census_withdrew(world, capsys):
    # A member changing a source with a trusted manual shape makes the
    # regenerated census withdraw that shape (manual_drift); the batch says
    # so, since no member's own diff shows it. Drift master already had is
    # not this batch's.
    import hashlib
    _with_census(world, _stub({"ops/tool.sh": hashlib.sha256(b"t\n").hexdigest(),
                               "ops/old.sh": "0" * 64}))
    sha10 = world.pr(10, {"ops/tool.sh": "changed\n"})
    world.gh.add(10, sha10)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    drift = json.loads(_census_at(world.origin, branch))["manual_drift"]
    assert {d["path"] for d in drift} == {"ops/tool.sh", "ops/old.sh"}
    (_, create_body), = world.gh.bodies("create")
    metadata_section = create_body.split("## Generated CI metadata", 1)[1].split("\n## ", 1)[0]
    assert "`ops/tool.sh`" in metadata_section and "ops/old.sh" not in metadata_section


def test_withdrawn_manual_shapes_are_listed_without_a_census_commit(world, capsys):
    # The member regenerated its own census, current on the batch head, so
    # `build` commits nothing; the withdrawn shape is still the batch's news.
    import hashlib
    _with_census(world, _stub({"ops/tool.sh": hashlib.sha256(b"t\n").hexdigest()}))
    world.pr(10, {"ops/tool.sh": "changed\n"})
    subprocess.run([sys.executable, "rust/producer_census.py"], cwd=world.seed,
                   check=True, capture_output=True)
    sha10 = world.pr(10, {}, base="HEAD")
    world.gh.add(10, sha10)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert _census_commits(world.origin, branch) == []
    (_, create_body), = world.gh.bodies("create")
    metadata_section = create_body.split("## Generated CI metadata", 1)[1].split("\n## ", 1)[0]
    assert "`ops/tool.sh`" in metadata_section and "one generated commit" not in metadata_section


def test_a_census_failure_posts_no_path_of_this_machine(world, capsys):
    # The failure lands in a public comment; the checkout's path is replaced.
    _with_census(world)
    sha10 = world.pr(10, {"rust/producer_census.py":
                          "raise SystemExit('cannot read ' + __file__)\n" + _stub()})
    sha11 = world.pr(11, {"ops/eleven.sh": "11\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _publish(world) == 0, capsys.readouterr()

    (args, body), = world.gh.bodies("comment")
    assert args[2] == "10" and "cannot read <checkout>" in body
    assert str(world.work.parent).lower() not in body.lower()
    assert world.work.parent.as_posix().lower() not in body.lower()


def test_a_dry_run_shows_the_census_commit_and_leaves_nothing(world, capsys):
    _with_census(world)
    sha10 = world.pr(10, {"ops/ten.sh": "10\n"})
    world.gh.add(10, sha10)
    master = _git(world.work, "rev-parse", "origin/master")

    assert _build("--dry-run") == 0
    out = capsys.readouterr().out

    assert "## Generated CI metadata" in out
    assert _git(world.work, "rev-parse", "HEAD") == master
    assert _git(world.work, "status", "--porcelain") == ""
    assert _git(world.work, "branch", "--list", "integrate/*") == ""


def test_census_inputs_come_from_masters_census_module(world):
    spec = importlib.util.spec_from_file_location(
        "census_for_test", ROOT / "rust" / "producer_census.py")
    census = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(census)
    first = _git(world.work, "rev-list", "--max-parents=0", "HEAD")
    _with_census(world)
    base = _git(world.work, "rev-parse", "origin/master")
    # The checked-out tree's script lists one more input than master's.
    stub = world.work / "rust" / "producer_census.py"
    stub.write_text(stub.read_text(encoding="utf-8")
                    + 'CURRENCY_INPUTS = CURRENCY_INPUTS | {"extra.txt"}\n',
                    encoding="utf-8")
    _git(world.work, "commit", "-q", "-am", "a member widens the census inputs")

    assert integrate._census_inputs(base) == set(census.CURRENCY_INPUTS)
    assert integrate._census_inputs(first) is None


def test_a_census_too_deep_to_parse_lists_no_drift(world):
    world.advance_master({"rust/producer-census.json": "[" * 200000})
    assert integrate._census_drift(_git(world.work, "rev-parse", "origin/master")) == set()


@pytest.mark.parametrize("rollup, expected", [
    (None, "none"),
    ([], "none"),
    ([{"status": "COMPLETED", "conclusion": "SUCCESS"},
      {"status": "COMPLETED", "conclusion": "SKIPPED"},
      {"status": "COMPLETED", "conclusion": "NEUTRAL"}], "green"),
    ([{"state": "SUCCESS"}], "green"),                       # a StatusContext
    ([{"state": "PENDING"}], "pending"),
    ([{"state": "ERROR"}], "failing"),
    ([{"status": "IN_PROGRESS", "conclusion": None}], "pending"),
    ([{"status": "QUEUED", "conclusion": ""}], "pending"),
    ([{"status": "IN_PROGRESS", "conclusion": None},
      {"status": "COMPLETED", "conclusion": "FAILURE"}], "failing"),
    # A run cancelled by hand or by a re-trigger gave no verdict: not green,
    # but no known failure either (a timeout is: see below).
    ([{"status": "COMPLETED", "conclusion": "CANCELLED"}], "cancelled"),
    ([{"status": "COMPLETED", "conclusion": "SUCCESS"},
      {"status": "COMPLETED", "conclusion": "CANCELLED"}], "cancelled"),
    ([{"status": "IN_PROGRESS", "conclusion": None},
      {"status": "COMPLETED", "conclusion": "CANCELLED"}], "pending"),
    ([{"status": "COMPLETED", "conclusion": "CANCELLED"},
      {"status": "COMPLETED", "conclusion": "FAILURE"}], "failing"),
    ([{"status": "COMPLETED", "conclusion": "TIMED_OUT"}], "failing"),
])
def test_ci_rollup_summary(rollup, expected):
    assert integrate._checks(rollup) == expected


# `merge`: the one path from an open batch pull request to master. Every
# merge is an admin bypass, so GitHub would merge an ordinary pull request
# just as readily; the subcommand merges only the batch `open` published.

REQUIRED = ("test", "Parity / ubuntu-latest", "Parity / windows-latest")


def _run(name: str, conclusion: str | None = "SUCCESS", *, status: str = "COMPLETED",
         started: str = "2026-10-21T10:00:00Z", workflow: str = "CI") -> dict:
    """A CheckRun as `gh pr view --json statusCheckRollup` reports it."""
    done = "0001-01-01T00:00:00Z" if status != "COMPLETED" else started
    return {"__typename": "CheckRun", "name": name, "workflowName": workflow,
            "status": status, "conclusion": conclusion, "startedAt": started,
            "completedAt": done}


def _green(*extra: dict) -> list[dict]:
    return [_run("scope"), *(_run(name) for name in REQUIRED), *extra]


def _opened(world, capsys) -> tuple[str, dict]:
    """A published batch of two clean members: (its head, its pull request)."""
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    assert _publish(world) == 0, capsys.readouterr()
    capsys.readouterr()
    return _git(world.work, "rev-parse", "HEAD"), world.gh.pr(900)


def _plan_file(world) -> Path:
    return world.work / _git(world.work, "rev-parse", "--git-path",
                             f"integrate-plans/{DATE}-batch-1.json")


def _merge_calls(world) -> list[list[str]]:
    return [args for args, _ in world.gh.calls if args[:2] == ["pr", "merge"]]


def _master(world) -> str:
    return _git(world.origin, "rev-parse", "master")


def test_merge_merges_a_green_batch_and_confirms_its_members(world, capsys):
    head, batch = _opened(world, capsys)

    assert integrate.main(["merge"]) == 0, capsys.readouterr()
    out = capsys.readouterr().out

    (args,) = _merge_calls(world)
    assert args == ["pr", "merge", "900", "--merge", "--admin",
                    "--match-head-commit", head]
    assert batch["state"] == "MERGED"
    assert _master(world) in out                             # the merge commit
    assert "#10 MERGED" in out and "#11 MERGED" in out
    assert "git switch --detach origin/master" in out
    assert f"git branch -D integrate/{DATE}-batch-1" in out
    assert "CI on master" in out                             # watch master's run
    assert not _plan_file(world).exists()


def test_merge_uses_a_merge_commit_never_a_squash(world, capsys):
    _opened(world, capsys)

    assert integrate.main(["merge"]) == 0, capsys.readouterr()

    (args,) = _merge_calls(world)
    assert "--merge" in args
    assert "--squash" not in args and "--rebase" not in args


@pytest.mark.parametrize("rollup", [
    _green(_run("test-lite-macos", None, status="IN_PROGRESS")),
    _green({"__typename": "StatusContext", "context": "CodeQL", "state": "PENDING",
            "startedAt": "2026-10-21T10:00:00Z"}),
    [],
    [_run("scope")],                       # the fast lanes alone pass nothing
    [_run("scope"), _run("guards")],
    [_run("scope"), _run("test"), _run("Parity / ubuntu-latest")],
    _green(_run("frontend", "")),          # completed without a conclusion yet
], ids=["in-progress", "status-pending", "no-checks-yet", "only-scope",
        "only-fast-lanes", "a-required-lane-absent", "no-conclusion"])
def test_merge_waits_for_every_check_and_every_required_lane(world, capsys, rollup):
    _, batch = _opened(world, capsys)
    batch["statusCheckRollup"] = rollup

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "wait" in err and "rerun" in err
    assert _merge_calls(world) == [] and _plan_file(world).exists()


def test_merge_names_the_required_lanes_not_reported(world, capsys):
    _, batch = _opened(world, capsys)
    batch["statusCheckRollup"] = [_run("scope"), _run("test")]

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "Parity / ubuntu-latest (not reported yet)" in err
    assert "Parity / windows-latest (not reported yet)" in err


@pytest.mark.parametrize("conclusion", ["FAILURE", "CANCELLED", "TIMED_OUT", "STALE",
                                        "ACTION_REQUIRED", "STARTUP_FAILURE"])
def test_merge_refuses_any_conclusion_but_success_skipped_or_neutral(world, capsys,
                                                                       conclusion):
    _, batch = _opened(world, capsys)
    batch["statusCheckRollup"] = _green(_run("test-lite-windows", conclusion))

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert f"test-lite-windows ({conclusion})" in err and "bisect" in err
    assert _merge_calls(world) == []


def test_merge_refuses_a_red_check_and_names_it(world, capsys):
    _, batch = _opened(world, capsys)
    batch["statusCheckRollup"] = [
        _run("guards"), _run("test"),
        _run("test-lite-windows", "FAILURE"),
        _run("Parity / ubuntu-latest", "CANCELLED", workflow="Rust shim"),
        {"__typename": "StatusContext", "context": "CodeQL", "state": "ERROR",
         "startedAt": "2026-10-21T10:00:00Z"}]

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "test-lite-windows" in err and "Parity / ubuntu-latest" in err
    assert "CodeQL" in err
    assert "guards" not in err                     # the green one is not named
    assert "bisect" in err
    assert _merge_calls(world) == []


def test_merge_accepts_skipped_and_neutral_checks(world, capsys):
    # The near miss: a docs-only batch skips the lanes its diff does not
    # reach, and a skipped required lane is reported, not absent.
    _, batch = _opened(world, capsys)
    batch["statusCheckRollup"] = [
        _run("scope"), _run("test", "SKIPPED"),
        _run("Parity / ubuntu-latest", workflow="Rust shim"),
        _run("Parity / windows-latest", "SKIPPED", workflow="Rust shim"),
        _run("label-check", "NEUTRAL"),
        {"__typename": "StatusContext", "context": "CodeQL", "state": "SUCCESS",
         "startedAt": "2026-10-21T10:00:00Z"}]

    assert integrate.main(["merge"]) == 0, capsys.readouterr()
    assert len(_merge_calls(world)) == 1


def test_merge_counts_only_the_latest_run_of_each_check(world, capsys):
    # A rerun that superseded a failure counts, whatever order gh lists them in.
    _, batch = _opened(world, capsys)
    batch["statusCheckRollup"] = _green(
        _run("test-lite-windows", "SUCCESS", started="2026-10-21T12:00:00Z"),
        _run("test-lite-windows", "FAILURE", started="2026-10-21T10:00:00Z"),
        {"__typename": "StatusContext", "context": "CodeQL", "state": "FAILURE",
         "startedAt": "2026-10-21T09:00:00Z"},
        {"__typename": "StatusContext", "context": "CodeQL", "state": "SUCCESS",
         "startedAt": "2026-10-21T11:00:00Z"})

    assert integrate.main(["merge"]) == 0, capsys.readouterr()


@pytest.mark.parametrize("started", ["", "0001-01-01T00:00:00Z", None])
def test_merge_waits_for_a_queued_rerun_beside_an_older_success(world, capsys, started):
    # A queued rerun may carry no start time yet; it still supersedes the
    # older completed run, so the batch waits instead of merging.
    _, batch = _opened(world, capsys)
    queued = _run("test", None, status="QUEUED")
    queued["startedAt"] = started
    batch["statusCheckRollup"] = _green(queued)

    assert integrate.main(["merge"]) == 2
    err = capsys.readouterr().err
    assert "wait" in err and "test" in err
    assert _merge_calls(world) == []


def test_merge_refuses_when_the_latest_run_of_a_check_failed(world, capsys):
    _, batch = _opened(world, capsys)
    batch["statusCheckRollup"] = _green(
        _run("test-lite-windows", "SUCCESS", started="2026-10-21T10:00:00Z"),
        _run("test-lite-windows", "FAILURE", started="2026-10-21T12:00:00Z"))

    assert integrate.main(["merge"]) == 2
    assert "test-lite-windows (FAILURE)" in capsys.readouterr().err


def test_the_required_lanes_are_the_rulesets():
    # The merge-queue spec's "Required check" table, read from the ruleset.
    spec = (ROOT / "docs" / "specs" / "2026-10-19-merge-queue.md").read_text(encoding="utf-8")
    rows = spec.split("| Required check | Workflow | Job |", 1)[1].split("\n\n", 1)[0]
    table = {line.split("`")[1] for line in rows.splitlines() if line.startswith("| `")}
    assert set(integrate.REQUIRED_CHECKS) == set(REQUIRED) == table


def test_merge_refuses_a_head_the_plan_did_not_build(world, capsys):
    head, batch = _opened(world, capsys)
    batch["headRefOid"] = "e" * 40          # a commit pushed after `open`

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "eeeeeeeeeeee" in err and head[:12] in err
    assert "nothing is fixed inside a batch" in err
    assert _merge_calls(world) == []


def test_merge_refuses_when_master_moved_past_the_batch(world, capsys):
    _opened(world, capsys)
    _git(world.seed, "fetch", "-q", "origin")
    _git(world.seed, "switch", "-q", "-C", "master", "origin/master")
    (world.seed / "m.txt").write_text("m\n", encoding="utf-8")
    _git(world.seed, "add", "-A")
    _git(world.seed, "commit", "-q", "-m", "master moves")
    _git(world.seed, "push", "-q", "origin", "master")

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "moved since" in err and "merge-forward" in err and "build" in err
    assert _merge_calls(world) == []


def test_merge_accepts_a_master_that_moved_within_the_batch(world, capsys):
    # The near miss: master moved, but only to a commit the batch head holds.
    _opened(world, capsys)
    sha10 = world.gh.pr(10)["headRefOid"]
    _git(world.seed, "push", "-q", "-f", "origin", f"{sha10}:refs/heads/master")

    assert integrate.main(["merge"]) == 0, capsys.readouterr()
    assert len(_merge_calls(world)) == 1


def test_merge_refuses_a_plan_for_an_ordinary_branch(world, capsys):
    # The accident the subcommand exists to stop: an ordinary pull request.
    sha = world.pr(861, {"b.txt": "b\n"})
    world.gh.history = [{"number": 861, "headRefName": "claude/feature", "state": "OPEN",
                         "url": "https://github.example/pr/861",
                         "isCrossRepository": False, "headRefOid": sha}]
    _git(world.work, "fetch", "-q", "origin", "pull/861/head")
    _git(world.work, "switch", "-q", "-c", "claude/feature", "FETCH_HEAD")
    base = _git(world.work, "rev-parse", "origin/master")
    integrate.Plan(branch="claude/feature", base_sha=base, head=sha, pr=861).save()

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "claude/feature is not an `integrate/` batch branch" in err
    assert world.gh.mutations() == []


def test_merge_refuses_a_batch_plan_whose_pr_comes_from_another_branch(world, capsys):
    _, batch = _opened(world, capsys)
    batch["headRefName"] = "claude/feature"

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "claude/feature" in err and f"integrate/{DATE}-batch-1" in err
    assert _merge_calls(world) == []


@pytest.mark.parametrize("field, value, expect", [
    ("baseRefName", "release", "targets release"),
    ("state", "CLOSED", "is closed"),
    ("isCrossRepository", True, "in a fork"),
])
def test_merge_refuses_a_batch_pr_it_cannot_merge(world, capsys, field, value, expect):
    _, batch = _opened(world, capsys)
    batch[field] = value

    assert integrate.main(["merge"]) == 2

    assert expect in capsys.readouterr().err
    assert _merge_calls(world) == [] and _plan_file(world).exists()


def test_merge_refuses_without_a_plan_and_names_the_way_out(world, capsys):
    _git(world.work, "switch", "-q", "--detach", "origin/master")

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "`open`" in err and "docs/dev/merge-batches.md" in err
    assert "build again" in err
    assert world.gh.mutations() == []


def test_merge_refuses_a_plan_that_was_never_opened(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    assert _build() == 0
    capsys.readouterr()

    assert integrate.main(["merge"]) == 2

    assert "built but not published" in capsys.readouterr().err
    assert world.gh.mutations() == []


def test_a_failed_gh_merge_leaves_the_plan_for_a_rerun(world, capsys):
    _opened(world, capsys)
    world.gh.fail_merge = "before"

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "HTTP 502" in err and "rerun `merge`" in err
    assert world.gh.pr(900)["state"] == "OPEN" and _plan_file(world).exists()


def test_a_rerun_after_the_merge_went_through_finishes_cleanly(world, capsys):
    _opened(world, capsys)
    world.gh.fail_merge = "after"
    assert integrate.main(["merge"]) == 2
    assert "HTTP 504" in capsys.readouterr().err

    world.gh.fail_merge = None
    assert integrate.main(["merge"]) == 0, capsys.readouterr()
    out = capsys.readouterr().out

    assert len(_merge_calls(world)) == 1          # only the first attempt merged
    assert "already merged" in out and "#10 MERGED" in out
    assert "CI on master" in out and "git switch --detach origin/master" in out
    assert not _plan_file(world).exists()


def test_merge_waits_for_github_to_mark_a_member_merged(world, capsys):
    _opened(world, capsys)
    world.gh.lag = {11: 2}

    assert integrate.main(["merge"]) == 0, capsys.readouterr()

    assert "#11 MERGED" in capsys.readouterr().out
    assert world.gh.lag == {11: 0}               # it asked again until it was


def test_a_member_whose_merged_head_github_never_marks_still_counts(world, capsys):
    _opened(world, capsys)
    world.gh.unmerged = {11}                     # still OPEN after the poll window

    assert integrate.main(["merge"]) == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "#11 open" in out and "is in master" in out
    assert "close" not in out.lower().replace("closed", "")
    assert not _plan_file(world).exists()


def test_a_hand_closed_member_whose_head_merged_counts(world, capsys):
    _opened(world, capsys)
    world.gh.unmerged = {11}
    world.gh.pr(11)["state"] = "CLOSED"

    assert integrate.main(["merge"]) == 0, capsys.readouterr()

    assert "#11 closed" in capsys.readouterr().out
    assert not _plan_file(world).exists()


def test_a_member_that_pushed_after_build_joins_a_later_batch(world, capsys):
    _opened(world, capsys)
    later = world.pr(11, {"c.txt": "c, revised\n"})        # not in the batch
    world.gh.pr(11)["headRefOid"] = later
    world.gh.unmerged = {11}

    assert integrate.main(["merge"]) == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "#11" in out and "has commits after the batch" in out
    assert "joins a later batch" in out
    assert "close" not in out.lower()
    assert not _plan_file(world).exists()


def _rust_job_pytest_paths() -> list[str]:
    """Every path the pytest commands of rust.yml's `rust` job name."""
    import yaml
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "rust.yml").read_text(encoding="utf-8"))
    paths = []
    for step in workflow["jobs"]["rust"]["steps"]:
        for line in (step.get("run") or "").splitlines():
            words = line.split()
            if "pytest" in words:
                paths += [w for w in words[words.index("pytest") + 1:] if w.startswith("rust/")]
    return paths


def test_harness_agreement_guards_run_on_every_pull_request():
    import yaml
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    step, = [s for s in workflow["jobs"]["guards"]["steps"]
             if s.get("name") == "Tree-wide guards"]
    assert "if" not in step
    for name in ("test_every_pytest_path_of_the_rust_job_is_in_the_rust_harness",
                 "test_the_rust_harness_run_mirrors_ci_and_what_the_suite_skips"):
        assert f"tests/test_integrate_script.py::{name}" in step["run"].split()


def test_every_pytest_path_of_the_rust_job_is_in_the_rust_harness():
    """The reverse of the check above: a test CI's `rust` job runs is also
    in the batch's local harness run, which has the bench login hosted
    runners lack, so its database cases run somewhere routinely."""
    paths = _rust_job_pytest_paths()
    assert "rust/daemon/harness/mirror/test_seed.py" in paths  # the parse found the steps
    missing = sorted(set(paths) - set(integrate.RUST_HARNESS))
    assert missing == [], f"add to RUST_HARNESS in ops/integrate.py: {missing}"


def test_the_rust_harness_run_fails_a_database_case_it_cannot_run():
    """A database case the local run skips would pass unseen: the printed
    command turns those skips into failures naming the cause and the fix."""
    assert "--require-databases" in integrate.RUST_HARNESS_COMMAND.split()


# --- the report-only frozen-probe check (rust.yml's frozen-probes job) ---
# The job left rust.yml with the Python freeze (14 November 2026); the
# exemption stays in ops/integrate.py for heads built before it, so these
# tests run it against a workflow that still carries a report-only job.

PROBE = "Frozen judge probes (report only) / ubuntu-latest"


def _with_probe_job(text: str) -> str:
    import yaml
    data = yaml.safe_load(text)
    data["jobs"]["frozen-probes"] = {
        "needs": "scope", "continue-on-error": True,
        "if": "${{ !cancelled() && github.event_name == 'pull_request' && needs.scope.outputs.full == 'false' }}",
        "name": "Frozen judge probes (report only) / ${{ matrix.os }}",
        "strategy": {"matrix": {"os": ["ubuntu-latest", "windows-latest"]}},
        "runs-on": "${{ matrix.os }}", "steps": [{"run": "true"}]}
    return yaml.safe_dump(data, sort_keys=False)


PROBE_WORKFLOW = _with_probe_job(WORKFLOW)


def _probe_world(world, *checks):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[
        _actions_check("601", "Rust / ubuntu-latest", "success"), *checks])


def test_a_member_whose_only_red_check_is_the_report_only_probe_is_admitted(world, capsys):
    _probe_world(world, _actions_check("602", PROBE, "failure"))

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #11 " in out and FAILING not in out


def test_the_probe_does_not_hide_another_red_check(world, capsys):
    _probe_world(world, _actions_check("602", PROBE, "failure"),
                 _actions_check("603", "Rust / windows-latest", "failure"))

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert FAILING in out and "Rust / windows-latest" in out and "merged #11 " not in out


@pytest.mark.parametrize("check", [
    _actions_check("602", "Frozen judge probes / ubuntu-latest", "failure"),
    _actions_check("602", "frozen judge probes (report only) / ubuntu-latest", "failure"),
    _actions_check("602", PROBE, "failure", workflow="CI"),
    {"context": PROBE, "state": "FAILURE"},
], ids=["no-report-only", "lower-case", "other-workflow", "status-context"])
def test_a_red_check_that_only_resembles_the_probe_is_refused(world, capsys, check):
    _probe_world(world, check)

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert FAILING in out and "merged #11 " not in out


@pytest.mark.parametrize("listed", [False, True], ids=["run-only", "in-rollup"])
def test_a_red_probe_beside_a_cancelled_gate_does_not_keep_a_member_out(world, capsys, listed):
    # The gate failed only because jobs it needs were cancelled, and the
    # report-only probe of the same run is red: the second read of the
    # run's jobs must leave the probe out as the rollup read does.
    sha10, sha11 = _gate_world(world, _RUN_1059 + [("510", PROBE, "failure")],
                               rollup=list(GATES) + (["510"] if listed else []),
                               head_workflow=PROBE_WORKFLOW, checkout_workflow=PROBE_WORKFLOW)

    assert _publish(world) == 0, capsys.readouterr()

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha11]


def test_a_job_that_only_resembles_the_probe_beside_a_cancelled_gate_keeps_a_member_out(
        world, capsys):
    _gate_world(world, _RUN_1059 + [("510", "Fixtures / ubuntu-latest", "failure"),
                                    ("511", PROBE, "failure")], rollup=list(GATES))

    assert _build() == 0, capsys.readouterr()

    assert FAILING in capsys.readouterr().out


def test_status_shows_a_red_probe_as_not_counted(world, capsys):
    _probe_world(world, _actions_check("602", PROBE, "failure"))

    assert integrate.main(["status"]) == 0
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()
             if line.startswith("#")}

    assert "CI green" in lines["#11"]
    assert f"report-only red, not counted: {PROBE}" in lines["#11"]


def test_no_current_job_produces_the_report_only_name():
    """The report-only job left rust.yml with the Python freeze (14 November
    2026). The exemption now exempts nothing a current workflow can
    produce; a job that adopts the prefix must be pinned here first."""
    import yaml
    workflow_name, prefix = integrate.REPORT_ONLY_CHECK
    assert (workflow_name, prefix) == ("Rust shim", "Frozen judge probes (report only) / ")
    produced = {}
    for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, job in data["jobs"].items():
            template = str(job.get("name") or job_id)
            systems = ((job.get("strategy") or {}).get("matrix") or {}).get("os") or [""]
            for system in systems if isinstance(systems, list) else [""]:
                name = template.replace("${{ matrix.os }}", str(system))
                if name.startswith(prefix):
                    produced.setdefault((data["name"], job_id), []).append(name)
    assert produced == {}
    assert "frozen-probes" not in yaml.safe_load(WORKFLOW)["jobs"]


# --- the Python freeze (rust/ci/python_freeze.py) at batch admission ---
# A batch is exempt from the `Python freeze` check because its members were
# checked, but a member's own check can be absent (opened before the guard),
# queued or cancelled, so `build` applies the same decision to each member's
# net change: a frozen path passes only with the freeze-exception label.

def _freeze_on_master(world):
    """Master carries the guard (the world fixture seeds it)."""
    assert (world.work / "rust" / "ci" / "python_freeze.py").is_file()


@pytest.mark.parametrize("labels, path, merged", [
    ([], "pseudolife_memory/update_cli.py", False),
    ([{"name": "freeze-exception"}], "pseudolife_memory/update_cli.py", True),
    ([{"name": "freeze-exceptions"}], "pseudolife_memory/update_cli.py", False),
    ([], "pseudolife_memory/web/static/index.html", True),
    ([], "docs/pseudolife_memory_notes.md", True),
], ids=["unlabelled", "labelled", "near-miss-label", "console-build", "name-only"])
def test_a_member_changing_frozen_python_joins_only_with_the_exception_label(world, capsys, labels, path, merged):
    _freeze_on_master(world)
    sha = world.pr(10, {path: "changed\n"})
    world.gh.add(10, sha, labels=labels)

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert ("merged #10 " in out) is merged, out
    if not merged:
        assert "Python is frozen since 14 November 2026" in out and "freeze-exception" in out


def test_an_earlier_member_cannot_switch_the_freeze_off_for_a_later_one(world, capsys):
    # Codex review of a49c9635: the decision runs master's copy of the
    # guard, never one a member merged into the batch checkout first.
    _freeze_on_master(world)
    # The member's copy switches the path rule off: whatever frozen_paths
    # says, it returns nothing.
    opened = GUARD.replace(
        "def frozen_paths(paths: list[str]) -> list[str]:\n",
        "def frozen_paths(paths: list[str]) -> list[str]:\n    return []  # switched off\n", 1)
    assert "return []  # switched off" in opened
    # `build` runs in the batch checkout, whose tree changes as members
    # merge, so a loader that resolved the guard beside __file__ read the
    # merged member's copy; pretend to be that file (reviewer of bf3de877).
    import os
    monkeypatch_file = world.work / "ops" / "integrate.py"
    integrate_file = integrate.__file__
    integrate.__file__ = os.fspath(monkeypatch_file)
    sha10 = world.pr(10, {"rust/ci/python_freeze.py": opened})
    sha11 = world.pr(11, {"pseudolife_memory/update_cli.py": "changed\n"})
    world.gh.add(10, sha10, labeled_at="2026-11-14T00:00:01Z")
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:02Z")

    try:
        assert _build() == 0, capsys.readouterr()
    finally:
        integrate.__file__ = integrate_file

    out = capsys.readouterr().out
    assert "merged #10 " in out and "merged #11 " not in out, out
    assert "Python is frozen since 14 November 2026" in out


def test_a_master_without_the_guard_refuses_to_build(world, capsys):
    # Reviewer of bf3de877: the guard and this check land together, so a
    # master without the guard means it was deleted or renamed; build then
    # refuses instead of admitting Python changes silently.
    seed = world.gh.seed
    _git(seed, "fetch", "-q", "origin")
    _git(seed, "switch", "-q", "-C", "master", "origin/master")
    _git(seed, "rm", "-q", "rust/ci/python_freeze.py")
    _git(seed, "commit", "-q", "-m", "drop the guard")
    _git(seed, "push", "-q", "origin", "master")
    _git(world.work, "fetch", "-q", "origin")
    _git(world.work, "reset", "-q", "--hard", "origin/master")
    sha = world.pr(10, {"pseudolife_memory/update_cli.py": "changed\n"})
    world.gh.add(10, sha)

    assert _build() == 2

    captured = capsys.readouterr()
    assert "rust/ci/python_freeze.py" in captured.err and "merged #10 " not in captured.out


def test_a_criss_cross_member_cannot_revert_a_frozen_file_unseen(world, capsys):
    # Reviewer of bf3de877: with two merge bases, the member's diff from the
    # one `git merge-base` prints is empty, yet merging it sets the frozen
    # file back. build judges what the merge changes on the batch branch.
    seed = world.gh.seed
    world.advance_master({"pseudolife_memory/core.py": "v1\n"})
    _git(seed, "switch", "-q", "-c", "xp", "master")
    (seed / "pseudolife_memory" / "core.py").write_text("v2\n", encoding="utf-8", newline="\n")
    _git(seed, "commit", "-qam", "Xp: v2")
    _git(seed, "switch", "-q", "master")
    (seed / "README.md").write_text("readme\nyq\n", encoding="utf-8", newline="\n")
    _git(seed, "commit", "-qam", "Yq")
    yq = _git(seed, "rev-parse", "HEAD")
    _git(seed, "merge", "-q", "--no-ff", "-m", "master takes Xp", "xp")
    _git(seed, "push", "-q", "origin", "master")
    _git(world.work, "fetch", "-q", "origin")
    _git(world.work, "reset", "-q", "--hard", "origin/master")
    _git(seed, "switch", "-q", "-c", "member", yq)
    _git(seed, "merge", "-q", "--no-ff", "--no-commit", "xp")
    _git(seed, "checkout", yq, "--", "pseudolife_memory/core.py")
    _git(seed, "commit", "-qm", "member keeps v1")
    _git(seed, "push", "-q", "-f", "origin", "HEAD:refs/pull/10/head")
    sha = _git(seed, "rev-parse", "HEAD")
    world.gh.add(10, sha)
    _git(world.work, "fetch", "-q", "origin", "refs/pull/10/head")
    base = _git(world.work, "rev-parse", "origin/master")
    chosen = _git(world.work, "merge-base", base, sha)
    assert _git(world.work, "diff", "--name-only", chosen, sha) == ""

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " not in out, out
    assert "pseudolife_memory/core.py" in out and "Python is frozen" in out
    assert (world.work / "pseudolife_memory" / "core.py").read_text(encoding="utf-8") == "v2\n"


def test_open_refuses_a_batch_commit_that_is_neither_a_member_merge_nor_generated(world, capsys, monkeypatch):
    # Reviewer of bf3de877 (S3): the census script is member code; a commit
    # it makes on its own becomes part of the built head and was never judged.
    sha = world.pr(10, {"b.txt": "b\n"})
    world.gh.add(10, sha)
    original = integrate._merge_members

    def rogue(plan, members, *rest):
        result = original(plan, members, *rest)
        (world.work / "pseudolife_memory").mkdir(exist_ok=True)
        (world.work / "pseudolife_memory" / "core.py").write_text("rogue\n", encoding="utf-8")
        _git(world.work, "add", "-A")
        _git(world.work, "commit", "-qm", "rogue")
        return result

    # A stray commit before build records its head, as a member's census
    # script committing on its own would leave one.
    monkeypatch.setattr(integrate, "_merge_members", rogue)
    assert _build() == 0, capsys.readouterr()
    capsys.readouterr()

    assert _open(world) == 2

    err = capsys.readouterr().err
    assert "neither a member merge nor the one generated commit" in err
    assert not world.gh.bodies("create")


def test_open_refuses_a_member_whose_exception_label_was_removed(world, capsys):
    # Reviewer of bf3de877 (S5): the label is read at build; a revoked
    # exception during the batch's CI must not land.
    sha = world.pr(10, {"pseudolife_memory/update_cli.py": "bridge\n"})
    world.gh.add(10, sha, labels=[{"name": "freeze-exception"}])
    assert _build() == 0, capsys.readouterr()
    assert "merged #10 " in capsys.readouterr().out
    world.gh.pr(10)["labels"] = []

    assert _open(world) == 2

    err = capsys.readouterr().err
    assert "#10" in err and "freeze-exception" in err
    assert not world.gh.bodies("create")


def test_merge_refuses_a_member_whose_exception_label_was_removed(world, capsys):
    # Reviewer of 6953d0ba (N7): the same re-read at `merge`.
    sha = world.pr(10, {"pseudolife_memory/update_cli.py": "bridge\n"})
    world.gh.add(10, sha, labels=[{"name": "freeze-exception"}])
    assert _publish(world) == 0, capsys.readouterr()
    capsys.readouterr()
    world.gh.pr(10)["labels"] = []

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "#10" in err and "freeze-exception" in err
    assert not _merge_calls(world)


# --- the batch that introduces the guard (reviewer of 6953d0ba, B1) ---
# `open` and `merge` run the batch's own ops/integrate.py, which is the
# pull request adding the guard, against a master that has never had it.

def test_the_batch_that_introduces_the_guard_builds_opens_and_merges(world_without_guard, capsys):
    world = world_without_guard
    sha10 = world.pr(10, {"rust/ci/python_freeze.py": GUARD, "b.txt": "b\n"})
    sha11 = world.pr(11, {"pseudolife_memory/core.py": "bridge\n"})
    sha12 = world.pr(12, {"pseudolife_memory/cli.py": "unruled\n"})
    world.gh.add(10, sha10, labeled_at="2026-11-14T00:00:01Z")
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:02Z", labels=[{"name": "freeze-exception"}])
    world.gh.add(12, sha12, labeled_at="2026-11-14T00:00:03Z")

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "this batch introduces it" in out and "#10 adds" in out
    assert "merged #10 " in out and "merged #11 " in out and "merged #12 " not in out
    assert "Python is frozen since 14 November 2026" in out
    assert _open(world) == 0, capsys.readouterr()
    capsys.readouterr()
    assert integrate.main(["merge"]) == 0, capsys.readouterr()
    assert len(_merge_calls(world)) == 1


def test_a_member_adding_the_guard_with_an_unlabelled_python_change_is_refused(world_without_guard, capsys):
    world = world_without_guard
    sha = world.pr(10, {"rust/ci/python_freeze.py": GUARD, "pseudolife_memory/core.py": "changed\n"})
    world.gh.add(10, sha)

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " not in out and "Python is frozen since 14 November 2026" in out


def test_a_master_that_never_had_the_guard_refuses_a_batch_that_does_not_add_it(world_without_guard, capsys):
    world = world_without_guard
    sha = world.pr(10, {"pseudolife_memory/core.py": "changed\n"})
    world.gh.add(10, sha, labels=[{"name": "freeze-exception"}])

    assert _build() == 2

    captured = capsys.readouterr()
    assert "never had rust/ci/python_freeze.py" in captured.err and "merged #10 " not in captured.out


def test_members_introducing_different_copies_of_the_guard_refuse(world_without_guard, capsys):
    world = world_without_guard
    sha10 = world.pr(10, {"rust/ci/python_freeze.py": GUARD})
    sha11 = world.pr(11, {"rust/ci/python_freeze.py": GUARD.replace("return [path", "return [] or [path")})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _build() == 2

    assert "different copies" in capsys.readouterr().err


def test_open_and_merge_refuse_a_master_that_lost_the_guard(world, capsys):
    # A deletion still fails closed: master's history had the guard.
    seed = world.gh.seed
    _git(seed, "fetch", "-q", "origin")
    _git(seed, "switch", "-q", "-C", "master", "origin/master")
    _git(seed, "rm", "-q", "rust/ci/python_freeze.py")
    _git(seed, "commit", "-q", "-m", "drop the guard")
    _git(seed, "push", "-q", "origin", "master")
    _git(world.work, "fetch", "-q", "origin")
    base = _git(world.work, "rev-parse", "origin/master")
    member = integrate.Member(number=10, title="carries no guard", sha=base, url="u",
                              review="APPROVED", checks="SUCCESS")

    with pytest.raises(integrate.Refusal, match="although it had one"):
        integrate._freeze_module(base, [member], base)


# --- a member the batch already contains (reviewer of 6953d0ba, S6) ---

@pytest.mark.parametrize("child_first", [True, False], ids=["child-first", "parent-first"])
def test_a_stacked_pair_merges_and_opens_in_either_label_order(world, capsys, child_first):
    # The parent touches nothing frozen and has no label; the child, stacked
    # on it, carries an exception. Merged child first, the parent's merge is
    # a no-op, which must not be judged as the child's merge.
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"pseudolife_memory/core.py": "bridge\n"}, base="pr-10")
    first, second = ("2026-11-14T00:00:02Z", "2026-11-14T00:00:01Z") if child_first else \
        ("2026-11-14T00:00:01Z", "2026-11-14T00:00:02Z")
    world.gh.add(10, sha10, labeled_at=first)
    world.gh.add(11, sha11, labeled_at=second, labels=[{"name": "freeze-exception"}])

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " in out and "merged #11 " in out, out
    assert "dropped" not in out and "Python is frozen" not in out
    assert ("already contained" in out) is child_first
    assert not world.gh.bodies("comment")
    assert _open(world) == 0, capsys.readouterr()


def test_a_member_already_on_master_is_contained_and_the_batch_stays_above_master(world, capsys):
    # Its head reached master with the previous batch, whose merge changed
    # frozen paths under an exception. Judging base^1..base with this
    # member's labels used to reset the batch below master.
    seed = world.gh.seed
    _git(seed, "fetch", "-q", "origin")
    _git(seed, "switch", "-q", "-C", "prev", "origin/master")
    (seed / "pseudolife_memory").mkdir(exist_ok=True)
    (seed / "pseudolife_memory" / "core.py").write_text("ruled\n", encoding="utf-8", newline="\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "previous batch's exception")
    landed = _git(seed, "rev-parse", "HEAD")
    _git(seed, "switch", "-q", "-C", "master", "origin/master")
    _git(seed, "merge", "-q", "--no-ff", "-m", "previous batch", "prev")
    _git(seed, "push", "-q", "origin", "master")
    _git(seed, "push", "-q", "-f", "origin", f"{landed}:refs/pull/10/head")
    _git(world.work, "fetch", "-q", "origin")
    _git(world.work, "reset", "-q", "--hard", "origin/master")
    master = _git(world.work, "rev-parse", "origin/master")
    world.gh.add(10, landed, labeled_at="2026-11-14T00:00:01Z")
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:02Z")

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "already contained" in out and "merged #11 " in out, out
    assert "Python is frozen" not in out
    head = _git(world.work, "rev-parse", "HEAD")
    assert _git(world.work, "merge-base", master, head) == master
    assert _open(world) == 0, capsys.readouterr()


# --- after the guard landed (reviewer of 18e0babaa, S7) ---

def test_a_guard_dropped_in_a_merge_resolution_still_reads_as_had(world, capsys):
    # An old branch, cut before the guard, merges master forward and drops
    # the guard while resolving; default history simplification then
    # follows the side that never had it.
    seed = world.gh.seed
    _git(seed, "fetch", "-q", "origin")
    _git(seed, "switch", "-q", "-C", "old", "origin/master~1")
    (seed / "d.txt").write_text("d\n", encoding="utf-8", newline="\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "old work")
    _git(seed, "merge", "-q", "--no-ff", "--no-commit", "origin/master")
    _git(seed, "rm", "-q", "-f", "rust/ci/python_freeze.py")
    _git(seed, "commit", "-qm", "merge master, dropping the guard")
    _git(seed, "push", "-q", "-f", "origin", "HEAD:master")
    _git(world.work, "fetch", "-q", "origin")
    _git(world.work, "reset", "-q", "--hard", "origin/master")
    base = _git(world.work, "rev-parse", "origin/master")
    assert _git(world.work, "rev-list", "-1", base, "--", "rust/ci/python_freeze.py") == ""
    # A member re-adds a copy that refuses nothing, beside a Python change.
    switched_off = GUARD.replace("def frozen_paths(paths: list[str]) -> list[str]:\n",
                                 "def frozen_paths(paths: list[str]) -> list[str]:\n    return []\n", 1)
    sha = world.pr(10, {"rust/ci/python_freeze.py": switched_off, "pseudolife_memory/core.py": "x\n"})
    world.gh.add(10, sha)

    assert _build() == 2

    captured = capsys.readouterr()
    assert "although it had one" in captured.err and "merged #10 " not in captured.out
    member = integrate.Member(number=10, title="re-adds", sha=sha, url="u", review="APPROVED",
                              checks="SUCCESS")
    with pytest.raises(integrate.Refusal, match="although it had one"):
        integrate._freeze_module(base, [member], sha)     # what open and merge load


def test_a_shallow_checkout_refuses_to_guess_whether_master_had_the_guard(world_without_guard, capsys,
                                                                            monkeypatch):
    world = world_without_guard
    sha = world.pr(10, {"rust/ci/python_freeze.py": GUARD})
    world.gh.add(10, sha)
    real = integrate._out
    monkeypatch.setattr(integrate, "_out", lambda *args: "true"
                        if args == ("rev-parse", "--is-shallow-repository") else real(*args))

    assert _build() == 2

    assert "shallow clone" in capsys.readouterr().err


def test_the_first_landing_path_is_dead_once_master_has_the_guard(world, capsys):
    # One member deletes the guard and another re-adds a copy that refuses
    # nothing: master's own copy still decides every member.
    switched_off = GUARD.replace("def frozen_paths(paths: list[str]) -> list[str]:\n",
                                 "def frozen_paths(paths: list[str]) -> list[str]:\n    return []\n", 1)
    seed = world.gh.seed
    _git(seed, "fetch", "-q", "origin")
    _git(seed, "switch", "-q", "-C", "pr-10", "origin/master")
    _git(seed, "rm", "-q", "rust/ci/python_freeze.py")
    _git(seed, "commit", "-qm", "delete the guard")
    _git(seed, "push", "-q", "-f", "origin", "HEAD:refs/pull/10/head")
    sha10 = _git(seed, "rev-parse", "HEAD")
    sha11 = world.pr(11, {"rust/ci/python_freeze.py": switched_off}, base="pr-10")
    sha12 = world.pr(12, {"pseudolife_memory/core.py": "unruled\n"})
    world.gh.add(10, sha10, labeled_at="2026-11-14T00:00:01Z")
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:02Z")
    world.gh.add(12, sha12, labeled_at="2026-11-14T00:00:03Z")

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "this batch introduces it" not in out
    assert "merged #12 " not in out and "Python is frozen since 14 November 2026" in out


# --- a stacked parent needs its own label (reviewer of 6953d0ba0 and 18e0babaa, N10) ---

@pytest.mark.parametrize("child_first", [True, False], ids=["child-first", "parent-first"])
def test_a_stacked_parents_unlabelled_python_change_lands_under_no_label(world, capsys, child_first):
    # The parent writes the Python change and carries no label; the child,
    # stacked on it, carries one. Each pull request is judged on its own
    # change against master, so neither lands the parent's exception.
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    first, second = ("2026-11-14T00:00:02Z", "2026-11-14T00:00:01Z") if child_first else \
        ("2026-11-14T00:00:01Z", "2026-11-14T00:00:02Z")
    world.gh.add(10, sha10, labeled_at=first)
    world.gh.add(11, sha11, labeled_at=second, labels=[{"name": "freeze-exception"}])

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " not in out and "merged #11 " not in out, out
    assert "Python is frozen since 14 November 2026" in out
    assert "carries #10's change to the frozen Python package" in out
    assert not (world.work / "pseudolife_memory" / "core.py").exists()


def test_a_stacked_child_needs_the_label_for_its_parents_python_change_too(world, capsys):
    # Labelled parent, unlabelled child: the child's own change against
    # master holds the parent's, so it waits; the parent lands alone.
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    world.gh.add(10, sha10, labeled_at="2026-11-14T00:00:01Z", labels=[{"name": "freeze-exception"}])
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:02Z")

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " in out and "merged #11 " not in out, out
    # Reviewer of the third task: it waits for #10 rather than being told
    # to rewrite in Rust a change it did not make, and keeps its label.
    assert "left out #11" in out and "joins once #10 has landed" in out
    assert not [args for args, _ in world.gh.bodies("comment") if args[2] == "11"]


def test_open_refuses_a_contained_member_whose_label_was_removed(world, capsys):
    # The parent is contained in its child's merge; open still reads its label.
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    world.gh.add(10, sha10, labeled_at="2026-11-14T00:00:02Z", labels=[{"name": "freeze-exception"}])
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:01Z", labels=[{"name": "freeze-exception"}])
    assert _build() == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "already contained" in out and "merged #11 " in out, out
    world.gh.pr(10)["labels"] = []

    assert _open(world) == 2

    err = capsys.readouterr().err
    assert "#10" in err and "freeze-exception" in err
    assert not world.gh.bodies("create")


def test_a_ready_child_carrying_a_waiting_parents_python_change_is_left_out(world, capsys):
    # Security review of the stacked-member rule: the parent is listed but
    # not ready (its CI is failing), so it never merges; its unlabelled
    # Python change must not land under its ready, labelled child.
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    world.gh.add(10, sha10, labeled_at="2026-11-14T00:00:01Z",
                 statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:02Z", labels=[{"name": "freeze-exception"}])

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #11 " not in out and "carries #10's change to the frozen Python package" in out, out
    assert not (world.work / "pseudolife_memory" / "core.py").exists()


def test_a_second_cut_is_still_batched_alone_when_the_first_leaves_a_fragment_unfolded(world, capsys):
    world.advance_master({"pyproject.toml": _cut("0.19.0"), FRAGMENT_A: "### Added (a)\n"})
    sha10 = _fold_cut(world, 10, "0.19.1")                       # folds nothing
    sha11 = _fold_cut(world, 11, "0.20.0", fold=(FRAGMENT_A,))
    sha12 = world.pr(12, {"b.txt": "b\n"})
    world.gh.add(10, sha10, labeled_at="2026-10-19T09:00:00Z")
    world.gh.add(11, sha11, labeled_at="2026-10-19T09:10:00Z")
    world.gh.add(12, sha12, labeled_at="2026-10-19T09:20:00Z")

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " not in out and "merged #11 " in out and "merged #12 " not in out, out
    assert "#11 is a release cut; it is batched alone" in out


@pytest.mark.parametrize("labelled", [False, True])
def test_a_release_cut_editing_the_packaged_compose_needs_only_the_label(world, capsys, labelled):
    # The dry run of a real cut showed the guard's "make the change in Rust"
    # advice, which a cut cannot follow: its next step is the label.
    world.advance_master({"pyproject.toml": _cut("0.19.0"),
                          "pseudolife_memory/compose/docker-compose.yml": "image: x:0.19.0\n"})
    sha = _fold_cut(world, 10, "0.19.1", add={"pseudolife_memory/compose/docker-compose.yml": "image: x:0.19.1\n"})
    world.gh.add(10, sha, labels=[{"name": "freeze-exception"}] if labelled else [])

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert ("merged #10 " in out) is labelled, out
    if not labelled:
        assert "This is a release cut" in out and "freeze-exception" in out
        assert "then re-apply `ready-to-batch`" in out


# --- the third task's review round ---

def test_a_release_cut_stacked_on_an_unlabelled_python_parent_does_not_land_it(world, capsys):
    # Codex and the reviewer: the cut search narrowed the members to the cut
    # before the freeze looked at its parent.
    world.advance_master({"pyproject.toml": _cut("0.19.0")})
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    seed = world.gh.seed
    _git(seed, "switch", "-q", "-C", "pr-11", "pr-10")
    (seed / "pyproject.toml").write_text(_cut("0.19.1"), encoding="utf-8", newline="\n")
    _git(seed, "commit", "-qam", "cut 0.19.1")
    _git(seed, "push", "-q", "-f", "origin", "HEAD:refs/pull/11/head")
    sha11 = _git(seed, "rev-parse", "HEAD")
    world.gh.add(10, sha10, labeled_at="2026-11-14T00:00:01Z")
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:02Z", labels=[{"name": "freeze-exception"}])

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #11 " not in out and "merged #10 " not in out, out
    assert not (world.work / "pseudolife_memory" / "core.py").exists()


def test_a_cut_the_freeze_refuses_does_not_evict_the_rest(world, capsys):
    world.advance_master({"pyproject.toml": _cut("0.19.0"),
                          "pseudolife_memory/compose/docker-compose.yml": "image: x:0.19.0\n"})
    sha10 = _fold_cut(world, 10, "0.19.1", add={"pseudolife_memory/compose/docker-compose.yml": "image: x:0.19.1\n"})
    sha12 = world.pr(12, {"b.txt": "b\n"})
    world.gh.add(10, sha10, labeled_at="2026-10-19T09:00:00Z")
    world.gh.add(12, sha12, labeled_at="2026-10-19T09:30:00Z")

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " not in out and "merged #12 " in out, out
    assert "batched alone" not in out


def test_the_cut_hint_is_only_for_the_version_file(world, capsys):
    # A cut that also edits package code is not told a label covers it.
    world.advance_master({"pyproject.toml": _cut("0.19.0")})
    sha = _fold_cut(world, 10, "0.19.1", add={"pseudolife_memory/compose/docker-compose.yml": "image: x\n",
                                               "pseudolife_memory/core.py": "code\n"})
    world.gh.add(10, sha)

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " not in out and "it is a release cut" in out
    assert "pseudolife_memory/core.py" in out and "label or no label" in out
    assert "This is a release cut" not in out


def test_a_child_carrying_an_unlisted_unlabelled_parent_is_left_out(world, capsys):
    # The reviewer's P1: the parent is an open pull request with no batch
    # label at all, so build never lists it.
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    world.gh.history.append({"number": 10, "state": "OPEN", "headRefName": "pr-10", "headRefOid": sha10,
                             "labels": [], "baseRefName": "master"})
    world.gh.add(11, sha11, labels=[{"name": "freeze-exception"}])

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #11 " not in out and "carries #10's change to the frozen Python package" in out, out
    assert not (world.work / "pseudolife_memory" / "core.py").exists()


def test_open_refuses_a_carried_parent_whose_label_was_removed(world, capsys):
    # The reviewer's P5: a labelled parent that did not merge itself (its CI
    # is failing) rides in its labelled child; its label is then removed.
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    world.gh.add(10, sha10, labeled_at="2026-11-14T00:00:01Z", labels=[{"name": "freeze-exception"}],
                 statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:02Z", labels=[{"name": "freeze-exception"}])
    assert _build() == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "merged #11 " in out, out
    world.gh.pr(10)["labels"] = []

    assert _open(world) == 2

    err = capsys.readouterr().err
    assert "#10" in err and "no longer carries the freeze-exception" in err
    assert not world.gh.bodies("create")



# --- the fourth review round (independent review of 17f9c8d54) ---

def _three_stack(world, middle: dict[str, str]) -> None:
    """#10 (labelled) changes the package; #11, unlabelled, is stacked on it
    with ``middle``; #12, labelled, is stacked on #11."""
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, middle, base="pr-10")
    sha12 = world.pr(12, {"d.txt": "d\n"}, base="pr-11")
    label = [{"name": "freeze-exception"}]
    world.gh.add(10, sha10, labeled_at="2026-11-14T00:00:01Z", labels=label)
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:02Z")
    world.gh.add(12, sha12, labeled_at="2026-11-14T00:00:03Z", labels=label)


def test_a_labelled_child_joins_beside_its_labelled_grandparent_over_a_waiting_middle(world, capsys):
    # The reviewer's P4: #11 only waits for #10 (its Python change is
    # #10's), so it does not keep #12 out.
    _three_stack(world, {"c.txt": "c\n"})

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " in out and "merged #12 " in out, out
    assert "left out #11" in out and "joins once #10 has landed" in out
    assert _open(world) == 0, capsys.readouterr()


def test_a_middle_with_its_own_python_change_still_keeps_its_labelled_child_out(world, capsys):
    # The near miss: #11 edits the file #10 labelled, so its own change is
    # not #10's. It is refused, and #12, which carries it, is left out with
    # a next step that grants nothing.
    _three_stack(world, {"pseudolife_memory/core.py": "middle\n"})

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " in out and "merged #11 " not in out and "merged #12 " not in out, out
    assert "dropped #11: Python is frozen since 14 November 2026" in out
    assert "left out #12: its head carries #11's change to the frozen Python package" in out
    assert "the maintainer rules #11's change an exception" in out
    assert "once #11 carries the label" not in out
    assert (world.work / "pseudolife_memory" / "core.py").read_text(encoding="utf-8") == "parent\n"


def test_a_parent_targeting_another_branch_is_still_judged(world, capsys):
    # The reviewer's P2: #10 targets a middle-of-stack branch, not master.
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    world.gh.history.append({"number": 10, "state": "OPEN", "headRefName": "pr-10", "headRefOid": sha10,
                             "labels": [], "baseRefName": "pr-9"})
    world.gh.add(11, sha11, labels=[{"name": "freeze-exception"}])

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #11 " not in out and "carries #10's change to the frozen Python package" in out, out
    assert not (world.work / "pseudolife_memory" / "core.py").exists()


@pytest.mark.parametrize("labelled", [False, True])
def test_a_release_cut_changing_package_code_is_refused_label_or_not(world, capsys, labelled):
    # The reviewer's P3: a labelled cut skipped every scope check. A cut may
    # change only the version file inside the package; the rest batch.
    world.advance_master({"pyproject.toml": _cut("0.19.0"),
                          "pseudolife_memory/compose/docker-compose.yml": "image: x:0.19.0\n"})
    sha10 = _fold_cut(world, 10, "0.19.1", add={"pseudolife_memory/compose/docker-compose.yml": "image: x:0.19.1\n",
                                                 "pseudolife_memory/core.py": "code\n"})
    sha12 = world.pr(12, {"b.txt": "b\n"})
    world.gh.add(10, sha10, labeled_at="2026-10-19T09:00:00Z",
                 labels=[{"name": "freeze-exception"}] if labelled else [])
    world.gh.add(12, sha12, labeled_at="2026-10-19T09:30:00Z")

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " not in out and "merged #12 " in out and "batched alone" not in out, out
    assert ("dropped #10: it is a release cut, and its change against master also changes "
            "pseudolife_memory/core.py") in out
    assert "label or no label" in out
    assert not (world.work / "pseudolife_memory" / "core.py").exists()


def test_a_plan_without_carried_parents_saves_no_carried_key(world, capsys):
    # A revert of this change, landed as a batch, runs the old loader
    # (Plan(**raw), which knows no `carried`) on a plan the new build saved.
    sha10 = world.pr(10, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    assert _build() == 0, capsys.readouterr()
    path = integrate.Plan.path(f"integrate/{DATE}-batch-1")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "carried" not in saved
    old_fields = {f.name for f in dataclasses.fields(integrate.Plan)} - {"carried"}
    assert set(saved) <= old_fields
    assert integrate.Plan.load(f"integrate/{DATE}-batch-1").carried == []


def test_a_plan_with_carried_parents_keeps_them(world, capsys):
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    world.gh.add(10, sha10, labeled_at="2026-11-14T00:00:01Z", labels=[{"name": "freeze-exception"}],
                 statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
    world.gh.add(11, sha11, labeled_at="2026-11-14T00:00:02Z", labels=[{"name": "freeze-exception"}])
    assert _build() == 0, capsys.readouterr()
    plan = integrate.Plan.load(f"integrate/{DATE}-batch-1")
    assert plan.carried == [10]
    saved = json.loads(integrate.Plan.path(plan.branch).read_text(encoding="utf-8"))
    assert saved["carried"] == [10]


def test_a_member_whose_change_cannot_be_read_refuses_the_build_and_keeps_its_label(world, capsys):
    # The reviewer's N4: the cause is the checkout or the branch, not the
    # author's change, so nothing is posted and no label is removed.
    seed = world.gh.seed
    _git(seed, "switch", "-q", "--orphan", "pr-13")
    (seed / "x.txt").write_text("x\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "unrelated history")
    _git(seed, "push", "-q", "-f", "origin", "HEAD:refs/pull/13/head")
    world.gh.add(13, _git(seed, "rev-parse", "HEAD"))

    assert _build() == 2

    err = capsys.readouterr().err
    assert "#13" in err and "no history in common" in err and "shallow" in err, err
    assert not world.gh.mutations()


def _failing_open_listing(world, monkeypatch, *, rows=None):
    real = world.gh

    def gh(*args, input=None):
        args = list(args)
        if args[:2] == ["pr", "list"] and "--label" not in args and "number,headRefOid,labels" in args:
            if rows is None:
                raise integrate.Refusal("`gh pr list --state open` failed (1): HTTP 502")
            return json.dumps(rows)
        return real(*args, input=input)

    monkeypatch.setattr(integrate, "gh", gh)


def test_a_failed_open_listing_names_its_cause(world, capsys, monkeypatch):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    _failing_open_listing(world, monkeypatch)

    assert _build() == 2

    err = capsys.readouterr().err
    assert "HTTP 502" in err and "every open pull request" in err and "gh auth status" in err, err
    assert not world.gh.mutations()


def test_an_open_listing_cut_off_at_its_limit_refuses_the_build(world, capsys, monkeypatch):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    rows = [{"number": 2000 + i, "headRefOid": "", "labels": []} for i in range(integrate.OPEN_PR_LIMIT)]
    _failing_open_listing(world, monkeypatch, rows=rows)

    assert _build() == 2

    err = capsys.readouterr().err
    assert f"limit of {integrate.OPEN_PR_LIMIT}" in err and "OPEN_PR_LIMIT" in err, err
    assert not world.gh.mutations()


def test_an_open_listing_just_under_its_limit_builds(world, capsys, monkeypatch):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    rows = [{"number": 2000 + i, "headRefOid": "", "labels": []} for i in range(integrate.OPEN_PR_LIMIT - 1)]
    _failing_open_listing(world, monkeypatch, rows=rows)

    assert _build() == 0, capsys.readouterr()
    assert "merged #10 " in capsys.readouterr().out


# --- a release cut over a parent that only waits is still batched alone ---

def _cut_over_a_waiting_parent(world):
    """#10 (freeze-exception) edits the packaged compose copy; #11, unlabelled,
    is stacked on it with nothing frozen of its own, so it only waits for
    #10; #12, a labelled release cut, is stacked on #11; #13 is ordinary."""
    compose = "pseudolife_memory/compose/docker-compose.yml"
    world.advance_master({"pyproject.toml": _cut("0.19.0"), compose: "image: x:0.19.0\n"})
    label = [{"name": "freeze-exception"}]
    sha10 = world.pr(10, {compose: "image: x:0.19.0-a\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    sha12 = world.pr(12, {"pyproject.toml": _cut("0.19.1"), compose: "image: x:0.19.1\n"}, base="pr-11")
    sha13 = world.pr(13, {"b.txt": "b\n"})
    world.gh.add(10, sha10, labeled_at="2026-11-15T00:00:01Z", labels=label)
    world.gh.add(11, sha11, labeled_at="2026-11-15T00:00:02Z")
    world.gh.add(12, sha12, labeled_at="2026-11-15T00:00:03Z", labels=label)
    world.gh.add(13, sha13, labeled_at="2026-11-15T00:00:04Z")


@pytest.mark.parametrize("dry_run", [False, True], ids=["build", "dry-run"])
def test_a_release_cut_carrying_a_waiting_parent_is_still_batched_alone(world, capsys, dry_run):
    # Codex (W5's run over a range including adcdb1cba): the cut search
    # skipped a cut whose head carried any refused entry, a waiting one
    # included, while the merge skips only refused entries that are not
    # waiting; so the skipped cut merged beside the others.
    _cut_over_a_waiting_parent(world)

    assert _build(*(["--dry-run"] if dry_run else [])) == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "#12 is a release cut; it is batched alone" in out, out
    assert "merged #12 " in out
    for other in (10, 11, 13):
        assert f"merged #{other} " not in out, out


def test_a_cut_that_reaches_the_merge_beside_others_refuses_the_build(world, capsys, monkeypatch):
    # The backstop: whatever lets a cut through the cut search beside other
    # members (here the search is bypassed), the build refuses.
    world.advance_master({"pyproject.toml": _cut("0.19.0")})
    sha10 = _fold_cut(world, 10, "0.19.1")
    sha11 = world.pr(11, {"b.txt": "b\n"})
    world.gh.add(10, sha10, labeled_at="2026-11-15T00:00:01Z")
    world.gh.add(11, sha11, labeled_at="2026-11-15T00:00:02Z")
    monkeypatch.setattr(integrate, "_release_cut_alone", lambda plan, members, refused=None: members)

    assert _build() == 2

    err = capsys.readouterr().err
    assert "release cut #10 merged beside #11" in err and "nothing was built" in err, err
    assert not world.remote_branches()


# --- the dry run names what would refuse a waiting member once it is ready ---

_FAILING = [{"status": "COMPLETED", "conclusion": "FAILURE"}]


def test_a_waiting_member_shows_its_freeze_refusal_too(world, capsys):
    # The conductor's report: a pending replay printed only "replay
    # pending" and hid the refusals that followed it for an hour.
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "unruled\n"})
    world.gh.add(10, sha10, statusCheckRollup=_FAILING)

    assert _build("--dry-run") == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "#10 CI on its head is failing" in out, out
    assert "#10, once ready: Python is frozen since 14 November 2026" in out
    assert out.count("Python is frozen") == 1      # the own diff and the merge agree: said once


def test_a_waiting_member_beside_a_batch_shows_its_budget_refusal(world, capsys, monkeypatch):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    sha12 = world.pr(12, {"d.txt": "d\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    world.gh.add(12, sha12, statusCheckRollup=_FAILING)

    def budget(base, m):
        if m.sha == sha12:
            raise ValueError("ci.yml/test-lite-linux, ci.yml/guards changed without a reviewed "
                             "cost reason; run ci_budget.py accept for each")
        return []

    monkeypatch.setattr(integrate, "_budget_member_evidence", budget)

    assert _build("--dry-run") == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " in out and "merged #11 " in out and "merged #12 " not in out, out
    assert ("#12, once ready: CI budget metadata refused: ci.yml/test-lite-linux, ci.yml/guards "
            "changed without a reviewed cost reason") in out
    assert "#10, once ready" not in out and "#11, once ready" not in out


def test_a_clean_waiting_member_is_said_to_have_nothing_else(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    world.gh.add(10, sha10, statusCheckRollup=_FAILING)

    assert _build("--dry-run") == 0, capsys.readouterr()

    assert "#10, once ready: nothing else refuses it" in capsys.readouterr().out


def test_a_waiting_member_shows_its_conflict_with_master(world, capsys):
    sha10 = world.pr(10, {"a.txt": "one\nTWO\nthree\n"})
    world.advance_master({"a.txt": "one\nzwei\nthree\n"})
    world.gh.add(10, sha10, statusCheckRollup=_FAILING)

    assert _build("--dry-run") == 0, capsys.readouterr()

    assert "#10, once ready: its merge onto master conflicts in a.txt" in capsys.readouterr().out


def test_a_waiting_member_carrying_a_refused_parent_says_so(world, capsys):
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    world.gh.add(10, sha10, statusCheckRollup=_FAILING)
    world.gh.add(11, sha11, labels=[{"name": "freeze-exception"}], statusCheckRollup=_FAILING)

    assert _build("--dry-run") == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "#11, once ready: its head carries #10's change to the frozen Python package" in out, out


def test_a_waiting_head_that_moved_is_said_and_the_build_stands(world, capsys, monkeypatch):
    # Both reviews: the report ran the budget check on a head the checkout
    # never fetched, and its Refusal ended a valid build with exit 2.
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    world.gh.add(12, "f" * 40, statusCheckRollup=_FAILING)

    def budget(base, m):
        if m.sha == "f" * 40:
            raise integrate.Refusal("`git merge-base` failed (128): Not a valid commit name")
        return []

    monkeypatch.setattr(integrate, "_budget_member_evidence", budget)

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "merged #10 " in out and "merged #11 " in out, out
    assert "#12, once ready: its listed head ffffffffffff is not in this checkout" in out
    assert integrate.Plan.load(f"integrate/{DATE}-batch-1") is not None


def test_a_waiting_child_of_a_labelled_parent_is_not_told_to_rewrite_in_rust(world, capsys):
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"}, base="pr-10")
    world.gh.add(10, sha10, labels=[{"name": "freeze-exception"}], statusCheckRollup=_FAILING)
    world.gh.add(11, sha11, statusCheckRollup=_FAILING)

    assert _build("--dry-run") == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "#11, once ready: its own change against master holds #10's labelled change" in out, out
    assert "make the change in Rust" not in out
    assert "#10, once ready: nothing else refuses it" in out


def test_a_waiting_member_carrying_a_refused_parent_and_its_own_change_names_both(world, capsys):
    sha10 = world.pr(10, {"pseudolife_memory/core.py": "parent\n"})
    sha11 = world.pr(11, {"pseudolife_memory/other.py": "child\n"}, base="pr-10")
    world.gh.history.append({"number": 10, "state": "OPEN", "headRefName": "pr-10", "headRefOid": sha10,
                             "labels": [], "baseRefName": "master"})
    world.gh.add(11, sha11, statusCheckRollup=_FAILING)

    assert _build("--dry-run") == 0, capsys.readouterr()

    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("  #11, once ready:"))
    assert "Python is frozen" in line and "carries #10's change" in line, line


def test_a_budget_only_conflict_is_not_reported_as_one(world, capsys):
    world.advance_master({integrate.CI_BUDGET: '{"a": 1}\n'})
    sha10 = world.pr(10, {integrate.CI_BUDGET: '{"a": 2}\n'})
    world.advance_master({integrate.CI_BUDGET: '{"a": 3}\n'})
    world.gh.add(10, sha10, statusCheckRollup=_FAILING)

    assert _build("--dry-run") == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "conflicts in" not in out and "#10, once ready:" in out, out


def test_a_waiting_release_cut_shows_its_unfolded_fragments(world, capsys):
    world.advance_master({"pyproject.toml": _cut("0.19.0")})
    sha10 = _fold_cut(world, 10, "0.19.1")
    world.advance_master({FRAGMENT_A: "### Added (a)\n"})
    world.gh.add(10, sha10, statusCheckRollup=_FAILING)

    assert _build("--dry-run") == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "#10, once ready: it is a release cut, but merging it onto master leaves 1 changelog fragment" in out, out


# ── `suite`: the batch's full suite on the remote machine (16 November 2026) ──

def _origin_refs(world) -> list[str]:
    return _git(world.origin, "for-each-ref", "--format=%(refname)").splitlines()


def _fake_remote_suite(world, monkeypatch, tmp_path, *, code=0, writes=True):
    """Stand in for ops/remote-suite.ps1 (which carries the batch head as a
    bundle): record what origin holds while it runs, and write its result."""
    results = tmp_path / "suite-results"
    results.mkdir(exist_ok=True)
    seen = []

    def run(head):
        seen.append(_origin_refs(world))
        if writes:
            (results / f"20261116-150000-remote-{head[:8]}.json").write_text(
                json.dumps({"machine": "remote", "commit": head, "args": [], "exit_code": code}),
                encoding="utf-8")
        return code

    monkeypatch.setattr(integrate, "_run_remote_suite", run)
    monkeypatch.setattr(integrate, "_suite_results_dir", lambda: results)
    return seen, results


def test_suite_runs_the_batch_remotely_and_pushes_nothing(world, monkeypatch, tmp_path, capsys):
    _three_with_a_collision(world)
    assert _build() == 0
    capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")
    before = _origin_refs(world)
    seen, results = _fake_remote_suite(world, monkeypatch, tmp_path)

    assert integrate.main(["suite"]) == 0

    out = capsys.readouterr().out
    assert seen == [before] and _origin_refs(world) == before   # nothing pushed
    assert out.splitlines()[0] == (f"integrate: running {head[:12]}'s full suite on the remote machine, "
                                   "with the real native client; the batch head travels as a bundle, "
                                   "nothing is pushed")
    assert f"open --suite {results / f'20261116-150000-remote-{head[:8]}.json'}" in out
    assert world.remote_branches() == [] and world.gh.mutations() == []


@pytest.mark.parametrize("failure", ["red", "no-result"])
def test_suite_fails_with_the_run_and_names_no_result_to_open(world, monkeypatch, tmp_path, capsys, failure):
    _three_with_a_collision(world)
    assert _build() == 0
    capsys.readouterr()
    if failure == "red":
        _fake_remote_suite(world, monkeypatch, tmp_path, code=1)
        assert integrate.main(["suite"]) == 1
        assert "open --suite" not in capsys.readouterr().out
    else:
        _fake_remote_suite(world, monkeypatch, tmp_path, writes=False)
        assert integrate.main(["suite"]) == 1
        assert "wrote no result" in capsys.readouterr().err


@pytest.fixture
def stack_parent(world, capsys):
    world.gh.add(10, world.pr(10, {"parent.txt": "first batch\n"}))
    assert _publish(world) == 0, capsys.readouterr()
    parent = world.gh.pr(900)
    work = world.work.parent / "stack workspace"
    _git(world.work, "worktree", "add", "-q", "--detach", str(work), "origin/master")
    world.gh.add(11, world.pr(11, {"rust/daemon/src/lib.rs": "pub fn next() {}\n",
                                  "next.txt": "next\n"}))
    return parent, work


def _land_stack_parent(world, parent):
    world.gh("pr", "merge", "900", "--match-head-commit", parent["headRefOid"])
    world.gh.ready = [r for r in world.gh.ready if r["number"] != 10]
    _git(world.origin, "update-ref", "-d", f"refs/heads/{parent['headRefName']}")
    _git(world.work, "fetch", "-q", "origin", "master")
    _git(world.work, "switch", "-q", "--detach", "origin/master")


@pytest.mark.parametrize("case, reason", [
    ("no-parent", "exactly one open"),
    ("two-parents", "exactly one open"),
    ("same-worktree", "plan is in this checkout"),
    ("missing-plan", "plan"),
    ("local-head-moved", "head moved"),
    ("remote-head-moved", "origin"),
    ("dirty", "uncommitted changes"),
])
def test_stacked_build_refuses_an_unverified_parent(world, stack_parent, monkeypatch, capsys,
                                                    case, reason):
    parent, work = stack_parent
    original_refs = _origin_refs(world)
    if case == "no-parent":
        parent["state"] = "CLOSED"
    elif case == "two-parents":
        world.gh.history.append({**parent, "number": 999})
    elif case == "missing-plan":
        integrate.Plan.path(parent["headRefName"]).unlink()
    elif case == "local-head-moved":
        _git(world.work, "commit", "--allow-empty", "-qm", "moves local head")
    elif case == "remote-head-moved":
        _git(world.origin, "update-ref", f"refs/heads/{parent['headRefName']}", "master")
    elif case == "dirty":
        (work / "a.txt").write_text("dirty", encoding="utf-8")
    if case != "same-worktree":
        monkeypatch.chdir(work)

    assert _build("--stacked") == 2
    assert reason in capsys.readouterr().err
    assert _origin_refs(world) == original_refs
    assert len(world.gh.mutations()) == 1  # only the fixture's parent open


@pytest.fixture
def stacked(world, stack_parent, monkeypatch, capsys):
    parent, work = stack_parent
    with monkeypatch.context() as checkout:
        checkout.chdir(work)
        assert _build("--stacked") == 0, capsys.readouterr()
        branch = _git(work, "branch", "--show-current")
        head = _git(work, "rev-parse", "HEAD")
        plan = integrate.Plan.load(branch)
        assert branch.startswith(f"stacked/{DATE}-batch-")
        assert plan.base_sha == parent["headRefOid"]
        assert [m.number for m in plan.merged] == [11]
        assert plan.rust_check == "passed"
        assert not _git(world.origin, "for-each-ref", "refs/heads/stacked/")
        assert len(world.gh.mutations()) == 1
    return parent, work, branch, head


@pytest.mark.parametrize("case, reason", [
    ("closed", "closed without merging"),
    ("missing-plan", "plan"),
    ("source-moved", "head moved or its branch was deleted"),
    ("parent-moved", "head moved"),
    ("member-moved", "head moved"),
    ("dirty", "uncommitted changes"),
    ("different-tree", "different tree"),
])
def test_from_stacked_refuses_invalid_source(world, stacked, monkeypatch, capsys, case, reason):
    parent, work, branch, head = stacked
    if case == "closed":
        parent["state"] = "CLOSED"
        _git(world.work, "switch", "-q", "--detach", "origin/master")
    else:
        _land_stack_parent(world, parent)
    if case == "missing-plan":
        with monkeypatch.context() as checkout:
            checkout.chdir(work)
            integrate.Plan.path(branch).unlink()
    elif case == "source-moved":
        _git(work, "commit", "--allow-empty", "-qm", "moves source head")
    elif case == "parent-moved":
        parent["headRefOid"] = head
    elif case == "member-moved":
        world.gh.pr(11)["headRefOid"] = head
    elif case == "dirty":
        (work / "next.txt").write_text("dirty", encoding="utf-8")
    elif case == "different-tree":
        world.advance_master({"extra.txt": "one extra file\n"})
    original = _git(world.work, "rev-parse", "HEAD")

    assert _build("--from-stacked", branch) == 2
    assert reason in capsys.readouterr().err
    assert _git(world.work, "rev-parse", "HEAD") == original
    assert len(world.cargo_calls()) == 1


@pytest.mark.parametrize("bad_result, reason", [
    (None, ""),
    ("commit", "not this batch head"),
    ("kind", "not a result"),
    ("command", "another selection"),
    ("selection", "another selection"),
    ("exit_code", "failed at"),
    ("started", "valid run timestamps"),
    ("machine", "known dev machine role"),
    ("summary", "failed at"),
])
def test_open_checks_stacked_results_against_their_source(world, stacked, capsys, bad_result, reason):
    parent, work, branch, source = stacked
    _land_stack_parent(world, parent)
    assert _build("--from-stacked", branch) == 0, capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")
    tree = _git(work, "rev-parse", f"{source}^{{tree}}")
    assert head != source and _git(world.work, "rev-parse", f"{head}^{{tree}}") == tree
    assert len(world.cargo_calls()) == 1
    harness = _gate_result(world, commit=source)
    if bad_result:
        record = json.loads(harness.read_text(encoding="utf-8"))
        record[bad_result] = {"commit": "0" * 40, "exit_code": 1,
                              "summary": "0 passed in 1.0s"}.get(bad_result)
        harness.write_text(json.dumps(record), encoding="utf-8")
    suite = _suite_result(world, commit=source)
    shim = _gate_result(world, "shim-tests", commit=source)

    result = integrate.main(["open", "--suite", str(suite), "--harness", str(harness),
                             "--shim-tests", str(shim)])

    if bad_result:
        assert result == 2 and reason in capsys.readouterr().err
        assert len(world.gh.bodies("create")) == 1
    else:
        assert result == 0, capsys.readouterr()
        _, body = world.gh.bodies("create")[-1]
        assert source in body and f"same tree `{tree}`" in body


def test_stacked_open_refuses_and_suite_still_runs(world, stacked, monkeypatch, tmp_path, capsys):
    _, work, _, _ = stacked
    monkeypatch.chdir(work)
    capsys.readouterr()
    seen, _ = _fake_remote_suite(world, monkeypatch, tmp_path)

    assert integrate.main(["open"]) == 2
    assert "--from-stacked" in capsys.readouterr().err
    assert integrate.main(["suite"]) == 0
    assert seen == [_origin_refs(world)]
    assert "--from-stacked" in capsys.readouterr().out


@pytest.mark.parametrize("case, reason", [("source-moved", "head moved"),
                                          ("member-moved", "head moved"),
                                          ("wrong-tree", "another tree"),
                                          ("compile-recipe", "another recipe"),
                                          ("client-head", "native client was built from")])
def test_open_rechecks_the_stacked_proof(world, stacked, monkeypatch, capsys, case, reason):
    parent, work, branch, source = stacked
    _land_stack_parent(world, parent)
    assert _build("--from-stacked", branch) == 0, capsys.readouterr()
    if case == "source-moved":
        _git(work, "commit", "--allow-empty", "-qm", "moves source after rebuild")
    elif case == "member-moved":
        world.gh.pr(11)["headRefOid"] = source
    elif case == "wrong-tree":
        batch = _git(world.work, "branch", "--show-current")
        plan = integrate.Plan.load(batch)
        plan.stacked_source.tree = "f" * 40
        plan.save()
    elif case == "compile-recipe":
        monkeypatch.setattr(integrate, "RUST_CHECK_COMMAND", "cargo check --locked")
    suite = _suite_result(world, commit=source,
                          real_client_commit="f" * 40 if case == "client-head" else source)

    assert integrate.main(["open", "--suite", str(suite), *_gate_args(world)]) == 2
    assert reason in capsys.readouterr().err
    assert len(world.gh.bodies("create")) == 1


def test_status_lists_stacked_plans_after_their_branch_is_deleted(world, stacked, capsys):
    parent, work, branch, _ = stacked
    assert integrate.main(["status"]) == 0
    assert f"waiting for parent #{parent['number']}" in capsys.readouterr().out
    _git(work, "switch", "-q", "--detach", "origin/master")
    _git(work, "branch", "-qD", branch)

    assert integrate.main(["status"]) == 0
    assert f"invalid stacked plan {branch}" in capsys.readouterr().out


def test_stacked_shim_gate_bundles_the_literal_ref_and_unmerged_parent(world, stacked,
                                                                       monkeypatch, tmp_path):
    parent, work, branch, head = stacked
    published_base = _git(world.origin, "rev-parse", "master")
    far = tmp_path / "master-only"
    _git(tmp_path, "clone", "-q", "--no-local", "--single-branch", "--branch", "master",
         str(world.origin), str(far))
    assert parent["headRefOid"] not in _git(far, "rev-list", "--all").splitlines()
    ref = f"refs/heads/{branch}"

    def ssh(argv, payload, timeout, log):
        encoded_script, encoded_bundle = payload.splitlines()
        script = base64.b64decode(encoded_script).decode("utf-8")
        bundle = tmp_path / "stacked.bundle"
        bundle.write_bytes(base64.b64decode(encoded_bundle))
        assert f"{published_base} {head} {ref} " in script.splitlines()[0]
        assert _git(far, "bundle", "list-heads", str(bundle)) == f"{head} {ref}"
        _git(far, "fetch", "-q", str(bundle), ref)
        assert _git(far, "rev-parse", "FETCH_HEAD^1") == parent["headRefOid"]
        nonce = script.splitlines()[0].split()[-2]
        return subprocess.CompletedProcess(argv, 0, f"shim-tests {nonce}: nextest exit 0\n", "")

    monkeypatch.chdir(work)
    monkeypatch.setattr(integrate, "_gate_ssh", ssh)
    plan = integrate.Plan.load(branch)
    result = integrate._remote_far(plan, {"repo": str(far), "ssh": "example.com", "role": "second Linux dev box"},
                                   "# stub", "shim-tests", "0", 1, shell="bash")

    assert result[:2] == (0, "")


@pytest.mark.parametrize("extra", [("--after-drop", "900"), ("--from-stacked", "stacked/x")])
def test_stacked_build_modes_are_mutually_exclusive(extra):
    with pytest.raises(SystemExit, match="2"):
        _build("--stacked", *extra)


@pytest.mark.parametrize("path, marker", [
    ("rust-check-remote.ps1", r"\$Ref -cnotmatch '([^']+)'"),
    ("rust-harness-remote.ps1", r"\$Ref -cnotmatch '([^']+)'"),
    ("shim-tests-remote.sh", r'"\$ref" =~ (\S+) \]\]'),
])
def test_far_half_ref_pattern(path, marker):
    # Exercise just the literal ERE used by the argument check, without a
    # real PowerShell process, clone, or lock on the hosted Windows runner.
    script = (ROOT / "ops" / path).read_text(encoding="utf-8")
    pattern = re.search(marker, script)[1]
    for ref in ("refs/heads/integrate/x", "refs/heads/stacked/x"):
        assert re.fullmatch(pattern, ref)
    for ref in ("refs/heads/master", "refs/tags/stacked/x", "refs/heads/stacked/x/y",
                "refs/heads/Stacked/x", "refs/heads/stacked/", "stacked/x"):
        assert not re.fullmatch(pattern, ref)


def test_the_remote_run_is_asked_for_the_remote_and_the_real_client(world, monkeypatch):
    calls = []

    class Done:
        returncode = 7

    monkeypatch.setattr(integrate, "_out", lambda *args: str(world.work))
    monkeypatch.setattr(integrate.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or Done())
    assert integrate._run_remote_suite("0" * 40) == 7
    ((argv, kw),) = calls
    assert argv[:3] == ["pwsh", "-NoProfile", "-File"] and argv[3].endswith("remote-suite.ps1")
    assert kw["env"]["PSEUDOLIFE_SUITE_WHERE"] == "remote"
    assert kw["env"]["PSEUDOLIFE_SUITE_NATIVE_CLIENT"] == "1"


@pytest.mark.parametrize("case", ["head-moved", "not-a-batch", "dirty"])
def test_suite_refuses_anything_but_the_built_batch(world, monkeypatch, tmp_path, capsys, case):
    _three_with_a_collision(world)
    assert _build() == 0
    capsys.readouterr()
    seen, _ = _fake_remote_suite(world, monkeypatch, tmp_path)
    if case == "head-moved":
        (world.work / "fix.txt").write_text("a fix\n", encoding="utf-8")
        _git(world.work, "add", "fix.txt")
        _git(world.work, "commit", "-q", "-m", "fix inside the batch")
        expect = "nothing is fixed inside a batch"
    elif case == "not-a-batch":
        _git(world.work, "switch", "-q", "--detach", "origin/master")
        expect = "build` first"
    else:
        (world.work / "a.txt").write_text("edited\n", encoding="utf-8")
        expect = "uncommitted changes"

    assert integrate.main(["suite"]) == 2

    assert expect in capsys.readouterr().err
    assert seen == []


def _uncompiled_batch(world, monkeypatch, capsys, case: str) -> None:
    """A batch `build` left after its Rust compile check failed or could not run."""
    _rust_member(world)
    if case == "failed":
        monkeypatch.setenv("FAKE_CARGO_EXIT", "101")
    else:
        monkeypatch.setattr(integrate, "RUST_CHECK",
                            ("cargo-is-not-installed-here", "check", "--workspace",
                             "--all-targets", "--locked"))
    assert _build() == 1
    capsys.readouterr()
    monkeypatch.delenv("FAKE_CARGO_EXIT", raising=False)
    assert _plan(world)["rust_check"].startswith("failed" if case == "failed" else "not run")


@pytest.mark.parametrize("case", ["failed", "not-run"])
def test_suite_refuses_a_batch_that_did_not_pass_the_compile_check(world, monkeypatch, tmp_path,
                                                                    capsys, case):
    # It never opens, so its full suite would spend the remote machine for nothing.
    _uncompiled_batch(world, monkeypatch, capsys, case)
    seen, _ = _fake_remote_suite(world, monkeypatch, tmp_path)

    assert integrate.main(["suite"]) == 2

    err = capsys.readouterr().err
    assert ("does not compile" if case == "failed" else "did not run") in err
    assert "build again" in err and "does not run on a head" in err
    assert seen == []
    assert world.remote_branches() == [] and world.gh.mutations() == []


@pytest.mark.parametrize("case", ["failed", "not-run"])
@pytest.mark.parametrize("suite", ["none", "no-real-client", "passing"])
def test_open_judges_the_compile_check_before_the_suite_result(world, monkeypatch, capsys,
                                                              case, suite):
    # The operator is told the batch did not compile, not to go and run a
    # suite that `suite` then refuses.
    _uncompiled_batch(world, monkeypatch, capsys, case)
    if suite == "none":
        code = integrate.main(["open", *_gate_args(world)])
    else:
        code = _open(world, **({"real_client": False} if suite == "no-real-client" else {}))

    assert code == 2
    err = capsys.readouterr().err
    assert ("does not compile" if case == "failed" else "did not run") in err
    assert "give the local full suite" not in err and integrate.SUITE_COMMAND not in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_build_names_the_remote_suite_step_first(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0
    out = capsys.readouterr().out
    suite = out.index("1. python ops/integrate.py suite")
    assert suite < out.index("2. python ops/integrate.py harness") < out.index("3. python ops/integrate.py shim-tests") < out.index("4. python ops/integrate.py open --suite")
    assert "carried as a bundle" in out and integrate.SUITE_LOCAL_NOTE in out


# ── Rule 1: a batch built again after a drop (maintainer ruling, 17 November 2026) ──

def _linked(check: dict, url: str) -> dict:
    return {**check, "detailsUrl": url}


OLD_RUN = "https://github.example/actions/runs/1/job/"
NEW_RUN = "https://github.example/actions/runs/2/job/"


def _closed_red(world, capsys, files: dict[int, dict[str, str]] | None = None, drop: int = 11,
                rollup: list[dict] | None = None) -> str:
    """#10, #11 and #12 built and opened as #900, which went red in hosted CI
    and was closed; `drop` loses its label, and the checkout is back on
    master with the plan kept. Returns the closed batch's head."""
    files = files or {10: {"b.txt": "b\n"}, 11: {"c.txt": "c\n"}, 12: {"d.txt": "d\n"}}
    for n, changed in files.items():
        world.gh.add(n, world.pr(n, changed))
    assert _publish(world) == 0, capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")
    closed = world.gh.pr(900)
    closed["state"] = "CLOSED"
    closed["statusCheckRollup"] = rollup if rollup is not None else [
        _linked(_run("scope"), OLD_RUN + "1"), _linked(_run("test", "FAILURE"), OLD_RUN + "2"),
        _linked(_run("Parity / ubuntu-latest"), OLD_RUN + "3"),
        _linked(_run("Parity / windows-latest"), OLD_RUN + "4"),
        _linked(_run("Rust / windows-latest"), OLD_RUN + "5")]
    world.gh.ready = [r for r in world.gh.ready if r["number"] != drop]
    _git(world.work, "switch", "-q", "--detach", "origin/master")
    _git(world.work, "branch", "-q", "-D", f"integrate/{DATE}-batch-1")
    capsys.readouterr()
    return head


def _batch_plan_path(world, n: int) -> Path:
    return world.work / _git(world.work, "rev-parse", "--git-path",
                             f"integrate-plans/{DATE}-batch-{n}.json")


def _after_drop_opened(world, capsys, checks: list[dict], **closed) -> str:
    """A batch built `--after-drop 900` and opened as #901, its head's checks
    `checks`; returns its head."""
    _closed_red(world, capsys, **closed)
    assert _build("--after-drop", "900") == 0, capsys.readouterr()
    assert integrate.main(["open"]) == 0, capsys.readouterr()
    world.gh.pr(901)["statusCheckRollup"] = checks
    head = _git(world.work, "rev-parse", "HEAD")
    # As GitHub lists them: a workflow run is in progress while any of its
    # checks is.
    running = any(integrate._unfinished(c) for c in checks)
    world.gh.run_lists[head] = [{"status": "IN_PROGRESS" if running else "COMPLETED",
                                 "name": "CI", "databaseId": 1}]
    capsys.readouterr()
    return head


def _comments_on(world, number: int) -> list[str]:
    return [body for args, body in world.gh.bodies("comment") if args[2] == str(number)]


def test_a_batch_after_a_drop_opens_without_a_suite_and_merges_on_test(world, capsys):
    old_head = _closed_red(world, capsys)

    assert _build("--after-drop", "900") == 0, capsys.readouterr()
    out = capsys.readouterr().out
    record = json.loads(_batch_plan_path(world, 2).read_text(encoding="utf-8"))["after_drop"]
    assert record["batch"] == 900 and record["head"] == old_head
    assert [d["number"] for d in record["dropped"]] == [11]
    assert record["dropped"][0]["files"] == ["c.txt"]
    assert [r["number"] for r in record["remaining"]] == [10, 12]
    assert "after a drop" in out and "No local suite, harness or shim run" in out
    assert "open --suite" not in out

    # No suite result and no harness note: the mode needs neither.
    assert integrate.main(["open"]) == 0, capsys.readouterr()
    (_, body) = world.gh.bodies("create")[-1]
    assert "## After a drop" in body and f"`{old_head}`" in body
    assert "- Dropped: #11" in body and "`c.txt`" in body
    assert "Local full suite: not run: a batch built after a drop" in body
    head = _git(world.work, "rev-parse", "HEAD")
    # `test` passed here; the Parity lanes and a Rust job still run, and
    # each succeeded on the closed batch's head.
    world.gh.pr(901)["statusCheckRollup"] = [
        _linked(_run("scope"), NEW_RUN + "1"), _linked(_run("test"), NEW_RUN + "2"),
        _run("Parity / ubuntu-latest", None, status="IN_PROGRESS"),
        _run("Parity / windows-latest", None, status="IN_PROGRESS"),
        _run("Rust / windows-latest", None, status="QUEUED")]
    capsys.readouterr()

    assert integrate.main(["merge"]) == 0, capsys.readouterr()

    out = capsys.readouterr().out
    (args,) = _merge_calls(world)
    assert args[2] == "901" and args[args.index("--match-head-commit") + 1] == head
    (comment,) = _comments_on(world, 901)
    for name, job in (("Parity / ubuntu-latest", 3), ("Parity / windows-latest", 4),
                      ("Rust / windows-latest", 5)):
        assert (f"- {name}: carried, success on #900's head {old_head[:12]} "
                f"({OLD_RUN}{job})") in comment
    assert f"- test: success on this head ({NEW_RUN}2)" in comment
    assert "- Rust / windows-latest: running" in comment     # every check, as it stands
    assert comment.strip() in out


def test_an_ordinary_build_ignores_a_closed_batch(world, capsys):
    # The near miss: without --after-drop nothing changes.
    _closed_red(world, capsys)

    assert _build() == 0, capsys.readouterr()

    out = capsys.readouterr().out
    assert "after a drop" not in out and "open --suite" in out
    assert "after_drop" not in json.loads(_batch_plan_path(world, 2).read_text(encoding="utf-8"))


@pytest.mark.parametrize("state, words", [("OPEN", "close it first"),
                                          ("MERGED", "nothing to repeat")])
def test_after_drop_refuses_a_closed_batch_that_is_open_or_merged(world, capsys, state, words):
    _closed_red(world, capsys)
    world.gh.pr(900)["state"] = state

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert f"#900 is {state.lower()}, not closed unmerged" in err and words in err


def test_after_drop_refuses_without_the_closed_batchs_plan_here(world, capsys):
    _closed_red(world, capsys)
    _batch_plan_path(world, 1).unlink()

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "holds no plan for" in err and "went red before it opened" in err


def test_after_drop_refuses_a_closed_batch_whose_head_is_not_the_plans(world, capsys):
    _closed_red(world, capsys)
    world.gh.pr(900)["headRefOid"] = "f" * 40

    assert _build("--after-drop", "900") == 2

    assert "its hosted results are not the plan's head's" in capsys.readouterr().err


def test_after_drop_refuses_once_master_moved(world, capsys):
    _closed_red(world, capsys)
    world.advance_master({"e.txt": "e\n"})

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "not the closed batch's base" in err and "tested another master" in err


def test_after_drop_refuses_when_nothing_was_dropped(world, capsys):
    _closed_red(world, capsys)
    # #11 kept its label after all.
    world.gh.add(11, _git(world.seed, "rev-parse", "pr-11"))

    assert _build("--after-drop", "900") == 2

    assert "nothing was dropped" in capsys.readouterr().err
    assert _git(world.work, "branch", "--list", f"integrate/{DATE}-batch-2") == ""


def test_after_drop_refuses_a_member_that_joined_since(world, capsys):
    _closed_red(world, capsys)
    world.gh.add(13, world.pr(13, {"f.txt": "f\n"}))

    assert _build("--after-drop", "900") == 2

    assert "#13 joined since #900" in capsys.readouterr().err


def test_after_drop_refuses_a_remaining_member_that_moved(world, capsys):
    _closed_red(world, capsys)
    world.gh.pr(12)["headRefOid"] = world.pr(12, {"d.txt": "d2\n"})

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "#12 moved from" in err and "tested the old head" in err


@pytest.mark.parametrize("path", [
    "tests/conftest.py", "tests/pg_fixtures.py", "tests/data/cases.json",
    "rust/daemon/harness/conftest.py", "rust/ci/parity_scope.py", "rust/ci/ci_budget.json",
    "rust/ci/ci_budget_accept/2026-11-17-abc.json",
    ".github/workflows/ci.yml", "ops/integrate.py", "pyproject.toml",
    "ops/requirements.lock.txt", "rust/shim/Cargo.toml", "rust/Cargo.lock"])
def test_after_drop_refuses_a_dropped_member_touching_shared_code(world, capsys, path):
    _closed_red(world, capsys, {10: {"b.txt": "b\n"}, 11: {path: "x = 1\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "changes code every test or job shares" in err and path in err


@pytest.mark.parametrize("path", [
    "tests/test_new_thing.py", "rust/daemon/harness/test_slice.py",
    "rust/shim/src/lib.rs", "docs/dev/merge-batches.md",
    "rust/producer-census.json", "rust/ci/ci_budget_fingerprints.json",
    "rust/ci/ci_budget_accept/imported-abc.json"])
def test_after_drop_takes_a_dropped_member_that_touches_only_its_own_files(world, capsys, path):
    # The near misses: an ordinary test file, a test under rust/, source,
    # docs, and the generated metadata the batch's own commit rewrites.
    _closed_red(world, capsys, {10: {"b.txt": "b\n"}, 11: {path: "x = 1\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 0, capsys.readouterr()


def test_after_drop_refuses_a_file_the_dropped_and_a_remaining_member_share(world, capsys):
    _closed_red(world, capsys, {10: {"b.txt": "b\n"},
                                11: {"c.txt": "c\n", "README.md": "x\n"},
                                12: {"README.md": "x\n", "d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 2

    assert "dropped #11 and #12 both change README.md" in capsys.readouterr().err


def test_after_drop_refuses_heads_that_differ_beyond_the_dropped_files(world, capsys):
    # The carried jobs ran on the closed head's tree: here that head had a
    # file the new one lacks although no member changes it.
    old_head = _closed_red(world, capsys)
    _git(world.work, "switch", "-q", "--detach", old_head)
    (world.work / "stray.txt").write_text("stray\n", encoding="utf-8")
    _git(world.work, "add", "stray.txt")
    _git(world.work, "commit", "-q", "-m", "stray")
    stray = _git(world.work, "rev-parse", "HEAD")
    _git(world.work, "switch", "-q", "--detach", "origin/master")
    path = _batch_plan_path(world, 1)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["head"] = stray
    path.write_text(json.dumps(raw), encoding="utf-8")
    world.gh.pr(900)["headRefOid"] = stray

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "differs from #900's head" in err and "stray.txt" in err


@pytest.mark.parametrize("conclusion", ["FAILURE", "CANCELLED", "TIMED_OUT"])
def test_after_drop_merge_refuses_a_finished_failure_even_of_a_carried_job(world, capsys,
                                                                          conclusion):
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest"), _run("Rust / windows-latest", conclusion)])

    assert integrate.main(["merge"]) == 2

    assert f"Rust / windows-latest ({conclusion})" in capsys.readouterr().err
    assert _merge_calls(world) == [] and _comments_on(world, 901) == []


def test_after_drop_merge_waits_for_test_on_the_new_head(world, capsys):
    # `test` succeeded on the closed head (a Rust job went red there), and
    # still it is never carried: it must pass on the new head.
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test", None, status="IN_PROGRESS"),
        _run("Parity / ubuntu-latest"), _run("Parity / windows-latest")],
        rollup=[_run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
                _run("Parity / windows-latest"), _run("Rust / windows-latest", "FAILURE")])

    assert integrate.main(["merge"]) == 2

    assert "cannot carry (test" in capsys.readouterr().err and _merge_calls(world) == []


@pytest.mark.parametrize("old", ["SKIPPED", "NEUTRAL", "FAILURE", "missing"])
def test_after_drop_carries_only_a_job_that_succeeded_on_the_closed_head(world, capsys, old):
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest"), _run("Rust / windows-latest", None, status="QUEUED")],
        rollup=[_run("scope"), _run("test", "FAILURE"), _run("Parity / ubuntu-latest"),
                _run("Parity / windows-latest"),
                *([] if old == "missing" else [_run("Rust / windows-latest", old)])])

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "cannot carry (Rust / windows-latest)" in err and _merge_calls(world) == []


@pytest.mark.parametrize("settled", [False, True])
def test_after_drop_carries_an_absent_required_lane_only_once_every_run_finished(
        world, capsys, settled):
    head = _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest")])
    world.gh.run_lists[head] = [{"status": "COMPLETED", "name": "CI", "databaseId": 1},
                                {"status": "COMPLETED" if settled else "IN_PROGRESS",
                                 "name": "Rust", "databaseId": 2}]

    code = integrate.main(["merge"])

    captured = capsys.readouterr()
    if settled:
        assert code == 0, captured
        assert "Parity / windows-latest: carried (absent here" in captured.out
    else:
        assert code == 2 and "Parity / windows-latest (not reported yet)" in captured.err


def test_after_drop_merges_a_rust_only_head_whose_test_scope_skipped(world, capsys):
    # The maintainer's ruling: `test` skipped because a successful `scope`
    # judged the head Rust-only is accepted; the rest carries as ever.
    head = _after_drop_opened(world, capsys, [
        _run("scope"), _run("test", "SKIPPED"),
        _run("Parity / ubuntu-latest", None, status="IN_PROGRESS"),
        _run("Parity / windows-latest")])

    assert integrate.main(["merge"]) == 0, capsys.readouterr()

    (args,) = _merge_calls(world)
    assert args[args.index("--match-head-commit") + 1] == head
    (comment,) = _comments_on(world, 901)
    assert "- test: skipped on this head" in comment
    assert "- Parity / ubuntu-latest: carried, success on #900's head" in comment


@pytest.mark.parametrize("scope, expect", [
    (_run("scope", None, status="IN_PROGRESS"), "cannot carry (scope)"),
    (_run("scope", "FAILURE"), "scope (FAILURE)"),
    (_run("scope", "SKIPPED"), "`scope` did not succeed there (skipped)"),
    (None, "`scope` did not succeed there (missing)")])
def test_after_drop_takes_a_skipped_test_only_behind_a_successful_scope(world, capsys,
                                                                        scope, expect):
    _after_drop_opened(world, capsys, [
        *([] if scope is None else [scope]), _run("test", "SKIPPED"),
        _run("Parity / ubuntu-latest"), _run("Parity / windows-latest")])

    assert integrate.main(["merge"]) == 2

    assert expect in capsys.readouterr().err and _merge_calls(world) == []


def test_after_drop_refuses_a_neutral_test(world, capsys):
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test", "NEUTRAL"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest")])

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "`test` was neutral" in err and "without --after-drop" in err


def test_after_drop_waits_for_a_test_not_reported_yet(world, capsys):
    head = _after_drop_opened(world, capsys, [
        _run("scope"), _run("Parity / ubuntu-latest"), _run("Parity / windows-latest")])
    world.gh.run_lists[head] = [{"status": "COMPLETED", "name": "CI", "databaseId": 1}]

    assert integrate.main(["merge"]) == 2

    assert "test (not reported yet)" in capsys.readouterr().err and _merge_calls(world) == []


def test_after_drop_merge_refuses_when_the_closed_batch_moved(world, capsys):
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest", None, status="IN_PROGRESS")])
    world.gh.pr(900)["headRefOid"] = "e" * 40

    assert integrate.main(["merge"]) == 2

    assert "closed batch #900 now reads head" in capsys.readouterr().err
    assert _merge_calls(world) == []


def test_after_drop_merge_reads_every_check_again_before_the_click(world, capsys):
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest"), _run("Rust / windows-latest", None, status="QUEUED")])
    original = world.gh.__class__.__call__
    reads = []

    def turning_red(self, *args, input=None):
        out = original(self, *args, input=input)
        if list(args[:3]) == ["pr", "view", "901"] and "statusCheckRollup" in args[-1]:
            reads.append(1)
            if len(reads) == 1:
                # The carried job fails between the first read and the second.
                self.pr(901)["statusCheckRollup"][-1] = _run(
                    "Rust / windows-latest", "FAILURE", started="2026-10-21T11:00:00Z")
        return out

    world.gh.__class__ = type("TurningRed", (world.gh.__class__,), {"__call__": turning_red})

    assert integrate.main(["merge"]) == 2

    assert "Rust / windows-latest (FAILURE)" in capsys.readouterr().err
    # Nothing said and nothing clicked: the comment follows the second read.
    assert _merge_calls(world) == [] and _comments_on(world, 901) == []


def test_after_drop_open_still_refuses_a_failed_compile_check(world, capsys, monkeypatch):
    _closed_red(world, capsys, {10: {"b.txt": "b\n"}, 11: {"c.txt": "c\n"},
                                12: {"rust/x.rs": "fn x() {}\n"}})
    monkeypatch.setenv("FAKE_CARGO_EXIT", "101")
    assert _build("--after-drop", "900") == 1
    monkeypatch.delenv("FAKE_CARGO_EXIT")
    capsys.readouterr()
    created = len(world.gh.bodies("create"))

    assert integrate.main(["open"]) == 2

    assert "does not compile" in capsys.readouterr().err
    assert len(world.gh.bodies("create")) == created


def test_after_drop_refuses_a_closed_pull_request_that_is_not_a_batch(world, capsys):
    _closed_red(world, capsys)
    world.gh.pr(900)["headRefName"] = "feature/not-a-batch"

    assert _build("--after-drop", "900") == 2

    assert "not an integrate/ batch into master" in capsys.readouterr().err


def _record_plans(world, capsys) -> tuple:
    """The closed batch's plan and a stand-in for the batch built again."""
    _closed_red(world, capsys)
    old = integrate.Plan.load(f"integrate/{DATE}-batch-1")
    new = integrate.Plan(branch=f"integrate/{DATE}-batch-2", base_sha=old.base_sha,
                         head=old.head, merged=[m for m in old.merged if m.number != 11])
    return old, new


def test_after_drop_refuses_a_build_that_itself_left_a_member_out(world, capsys):
    old, new = _record_plans(world, capsys)
    new.dropped = [integrate.Dropped(old.merged[0], "conflicts with #12 in b.txt")]

    with pytest.raises(integrate.Refusal, match="this build itself left out #10"):
        integrate._after_drop_record(new, old)


def test_after_drop_refuses_a_head_this_checkout_lacks(world, capsys):
    old, new = _record_plans(world, capsys)
    gone = next(m for m in old.merged if m.number == 11)
    gone.sha = "0" * 40

    with pytest.raises(integrate.Refusal, match="#11's head 000000000000 from #900 is not in this checkout"):
        integrate._after_drop_record(new, old)

    old.merged = [m for m in old.merged if m.number != 11] + [
        integrate.Member(**{**dataclasses.asdict(gone), "sha": _git(world.seed, "rev-parse", "pr-11")})]
    old.head = "1" * 40
    with pytest.raises(integrate.Refusal, match="closed batch's head 111111111111 is not in this checkout"):
        integrate._after_drop_record(new, old)


@pytest.mark.parametrize("path", [
    "frontend/package.json", "frontend/package-lock.json", "rust/rust-toolchain.toml",
    "rust/.cargo/config.toml"])
def test_after_drop_refuses_a_dropped_member_changing_a_build_input(world, capsys, path):
    # Codex review of f299e9604: the frontend's manifests and the pinned Rust
    # toolchain are build inputs every job of theirs shares.
    _closed_red(world, capsys, {10: {"b.txt": "b\n"}, 11: {path: "x = 1\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "changes code every test or job shares" in err and path in err


@pytest.mark.parametrize("importer", [
    "from test_fixture_mod import thing\n",
    "def test_it():\n    from tests.test_fixture_mod import thing\n",
    "import test_fixture_mod\n"])
def test_after_drop_refuses_a_dropped_test_module_other_tests_import(world, capsys, importer):
    # Codex review of f299e9604: a test module another test imports is a
    # shared fixture, though test-named.
    world.advance_master({"tests/test_user.py": importer})
    _closed_red(world, capsys, {10: {"b.txt": "b\n"},
                                11: {"tests/test_fixture_mod.py": "thing = 2\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "changes code every test or job shares" in err and "tests/test_fixture_mod.py" in err


def test_after_drop_takes_a_dropped_test_module_nothing_imports(world, capsys):
    # The near miss: a name that merely starts the same is not an import.
    world.advance_master({"tests/test_user.py": "from test_fixture_mod_other import thing\n"})
    _closed_red(world, capsys, {10: {"b.txt": "b\n"},
                                11: {"tests/test_fixture_mod.py": "thing = 2\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 0, capsys.readouterr()


@pytest.mark.parametrize("first", ["FAILURE", "CANCELLED", "TIMED_OUT"])
def test_after_drop_merge_refuses_a_failure_whose_rerun_still_runs(world, capsys, first):
    # Codex review of f299e9604 (P1): the latest run alone reads as pending,
    # which the fast path would carry from the closed head.
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest"),
        _run("Rust / windows-latest", first, started="2026-10-21T10:00:00Z"),
        _run("Rust / windows-latest", None, status="IN_PROGRESS",
             started="2026-10-21T11:00:00Z")])

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "no finished rerun has replaced the failure" in err and "Rust / windows-latest" in err
    assert _merge_calls(world) == [] and _comments_on(world, 901) == []


def test_after_drop_merge_takes_a_failure_a_finished_rerun_replaced(world, capsys):
    # The near miss: the rerun finished green, so the failure is history.
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest"),
        _run("Rust / windows-latest", "FAILURE", started="2026-10-21T10:00:00Z"),
        _run("Rust / windows-latest", "SUCCESS", started="2026-10-21T11:00:00Z")])

    assert integrate.main(["merge"]) == 0, capsys.readouterr()


def test_after_drop_refuses_a_closed_batch_already_rebuilt_after_a_drop(world, capsys):
    # Opus review of f299e9604: #901, built from #900 after a drop, went red
    # too; its checks are newer, so #900's must not be carried again.
    _after_drop_opened(world, capsys, [_run("scope"), _run("test", "FAILURE")])
    world.gh.pr(901)["state"] = "CLOSED"
    world.gh.ready = [r for r in world.gh.ready if r["number"] != 12]
    _git(world.work, "switch", "-q", "--detach", "origin/master")
    _git(world.work, "branch", "-q", "-D", f"integrate/{DATE}-batch-2")

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "already built again after a drop and opened as #901" in err
    assert "`build --after-drop 901`" in err


def test_after_drop_refuses_a_closed_batch_from_a_fork(world, capsys):
    _closed_red(world, capsys)
    world.gh.pr(900)["isCrossRepository"] = True

    assert _build("--after-drop", "900") == 2

    assert "not an integrate/ batch into master" in capsys.readouterr().err


def test_after_drop_merge_refuses_a_closed_batch_reopened(world, capsys):
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest", None, status="IN_PROGRESS")])
    world.gh.pr(900)["state"] = "OPEN"

    assert integrate.main(["merge"]) == 2

    assert "closed batch #900 now reads open, not closed" in capsys.readouterr().err
    assert _merge_calls(world) == []


def test_after_drop_refuses_a_dropped_file_a_workflow_names(world, capsys):
    # Opus review of f299e9604: a CI script or Dockerfile a workflow runs is
    # an input of every job that runs it.
    world.advance_master({".github/workflows/ci.yml": "jobs:\n  t:\n    steps:\n"
                                                      "      - run: bash ops/ci_tests.sh\n"})
    _closed_red(world, capsys, {10: {"b.txt": "b\n"}, 11: {"ops/ci_tests.sh": "exit 0\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "changes code every test or job shares" in err and "ops/ci_tests.sh" in err


def test_after_drop_refusal_leaves_the_checkout_as_it_was(world, capsys):
    _closed_red(world, capsys, {10: {"b.txt": "b\n"}, 11: {"tests/conftest.py": "x = 1\n"},
                                12: {"d.txt": "d\n"}})
    before = _git(world.work, "rev-parse", "HEAD")

    assert _build("--after-drop", "900") == 2

    assert _git(world.work, "rev-parse", "HEAD") == before
    assert _git(world.work, "branch", "--list", f"integrate/{DATE}-batch-2") == ""
    assert _git(world.work, "status", "--porcelain") == ""
    assert not _batch_plan_path(world, 2).exists()


def test_after_drop_waits_when_no_run_on_the_new_head_is_listed(world, capsys):
    # No workflow run listed yet: nothing has settled, so an absent required
    # lane waits rather than being carried.
    head = _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest")])
    world.gh.run_lists[head] = []

    assert integrate.main(["merge"]) == 2

    assert "Parity / windows-latest (not reported yet)" in capsys.readouterr().err


@pytest.mark.parametrize("result", [{"exit_code": 1}, {"commit": "f" * 40}])
def test_after_drop_open_still_checks_a_suite_result_it_is_given(world, capsys, result):
    _closed_red(world, capsys)
    assert _build("--after-drop", "900") == 0, capsys.readouterr()
    capsys.readouterr()

    before = world.gh.bodies("create")
    assert _open(world, **result) == 2, capsys.readouterr()
    assert world.gh.bodies("create") == before


@pytest.mark.parametrize("kind", ["rust-harness", "shim-tests"])
@pytest.mark.parametrize("edit", [{"exit_code": 1}, {"commit": "f" * 40},
                                 {"exit_code": None, "not_run": "offline"}])
def test_after_drop_open_checks_each_supplied_gate_result(world, capsys, kind, edit):
    _closed_red(world, capsys)
    assert _build("--after-drop", "900") == 0, capsys.readouterr()
    flag = "--harness" if kind == "rust-harness" else "--shim-tests"
    before = world.gh.bodies("create")
    assert integrate.main(["open", flag, str(_gate_result(world, kind, **edit))]) == 2
    assert world.gh.bodies("create") == before


@pytest.mark.parametrize("kind,label", [("suite", "Local full suite"),
    ("rust-harness", "Rust harness Python tests"), ("shim-tests", "Shim unit tests")])
def test_after_drop_open_records_a_passing_supplied_result(world, capsys, kind, label):
    _closed_red(world, capsys)
    assert _build("--after-drop", "900") == 0, capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")
    flag = {"suite": "--suite", "rust-harness": "--harness", "shim-tests": "--shim-tests"}[kind]
    path = _suite_result(world) if kind == "suite" else _gate_result(world, kind)
    assert integrate.main(["open", flag, str(path)]) == 0, capsys.readouterr()
    (_, body) = world.gh.bodies("create")[-1]
    line = next(line for line in body.splitlines() if line.startswith(f"- {label}"))
    assert f"passed at `{head}`" in line and "not run" not in line


def test_after_drop_body_says_the_harness_was_not_run(world, capsys):
    _after_drop_opened(world, capsys, [_run("scope")])

    (_, body) = world.gh.bodies("create")[-1]
    assert "Rust harness Python tests" in body
    line = next(l for l in body.splitlines() if l.startswith("- Rust harness Python tests"))
    assert "not run: a batch built after a drop skips the suite, harness and shim tests" in line
    assert "- Shim unit tests" in body
    assert "not run:" in next(l for l in body.splitlines() if l.startswith("- Shim unit tests"))


def test_after_drop_carries_a_status_context_that_succeeded(world, capsys):
    status = {"__typename": "StatusContext", "context": "CodeQL", "state": "SUCCESS",
              "targetUrl": OLD_RUN + "9"}
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"), _run("Parity / windows-latest"),
        {**status, "state": "PENDING", "targetUrl": NEW_RUN + "9"}],
        rollup=[_run("scope"), _run("test", "FAILURE"), _run("Parity / ubuntu-latest"),
                _run("Parity / windows-latest"), status])

    assert integrate.main(["merge"]) == 0, capsys.readouterr()

    (comment,) = _comments_on(world, 901)
    assert f"- CodeQL: carried, success on #900's head" in comment and OLD_RUN + "9" in comment


def test_after_drop_never_carries_a_job_across_workflows(world, capsys):
    # The same job name in another workflow is another check.
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"), _run("Parity / windows-latest"),
        _run("lint", None, status="IN_PROGRESS", workflow="Rust shim")],
        rollup=[_run("scope"), _run("test", "FAILURE"), _run("Parity / ubuntu-latest"),
                _run("Parity / windows-latest"), _run("lint", workflow="CI")])

    assert integrate.main(["merge"]) == 2

    assert "cannot carry (lint)" in capsys.readouterr().err


def test_after_drop_judges_only_ci_s_own_test_and_scope(world, capsys):
    # A job named `test` in another workflow is not the required `test`.
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test", "SKIPPED", workflow="Other"),
        _run("Parity / ubuntu-latest"), _run("Parity / windows-latest")])

    assert integrate.main(["merge"]) == 2

    assert "test (not reported yet)" in capsys.readouterr().err


SUMMARY_WORKFLOW = """name: Rust shim
on: pull_request
jobs:
  parity-checks:
    name: Parity checks / ${{ matrix.os }}
    runs-on: ubuntu-latest
    steps:
      - run: echo shard
  parity:
    name: Parity / ${{ matrix.os }}
    needs: [parity-checks]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - run: echo summary
"""


def test_after_drop_says_a_summary_job_waits_for_the_carried_jobs_it_needs(world, capsys):
    # The conductor's finding on #1671: a summary job that failed on the
    # closed head (one shard went red) must pass anew, so it waits for the
    # carried shards it needs, and the fast path saves no time; `merge`
    # says so, naming them.
    world.advance_master({".github/workflows/rust.yml": SUMMARY_WORKFLOW})
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / windows-latest"),
        _run("Parity checks / ubuntu-latest", None, status="IN_PROGRESS", workflow="Rust shim"),
        _run("Parity / ubuntu-latest", None, status="QUEUED", workflow="Rust shim")],
        rollup=[_run("scope"), _run("test"), _run("Parity / windows-latest"),
                _run("Parity checks / ubuntu-latest", workflow="Rust shim"),
                _run("Parity checks / macos-latest", "FAILURE", workflow="Rust shim"),
                _run("Parity / ubuntu-latest", "FAILURE", workflow="Rust shim")])

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "cannot carry (Parity / ubuntu-latest" in err
    assert ("Parity / ubuntu-latest did not pass on #900's head, so it must pass on this one; "
            "it needs Parity checks / ubuntu-latest, which is carried but still running") in err
    assert "the fast path saves no time here" in err


def test_after_drop_names_no_summary_for_a_job_that_needs_nothing_carried(world, capsys):
    # The near miss: a waiting job whose inputs are not carried gets no note.
    world.advance_master({".github/workflows/rust.yml": SUMMARY_WORKFLOW})
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / windows-latest"),
        _run("Parity / ubuntu-latest", None, status="QUEUED", workflow="Rust shim")],
        rollup=[_run("scope"), _run("test"), _run("Parity / windows-latest"),
                _run("Parity / ubuntu-latest", "FAILURE", workflow="Rust shim")])

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "cannot carry (Parity / ubuntu-latest)" in err and "saves no time" not in err


@pytest.mark.parametrize("importer", [
    "from tests import test_fixture_mod\n",
    "from tests import test_fixture_mod as fm\n",
    "from tests import (\n    other,\n    test_fixture_mod,\n)\n"])
def test_after_drop_refuses_a_test_module_imported_through_its_package(world, capsys, importer):
    # Codex review of a219cbb26: `from cli_harness.rows import test_login`.
    world.advance_master({"tests/test_user.py": importer})
    _closed_red(world, capsys, {10: {"b.txt": "b\n"},
                                11: {"tests/test_fixture_mod.py": "thing = 2\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 2

    assert "tests/test_fixture_mod.py" in capsys.readouterr().err


def test_after_drop_takes_a_file_a_workflow_names_only_in_a_comment(world, capsys):
    # Codex review of a219cbb26: a comment is not a workflow input.
    world.advance_master({".github/workflows/ci.yml": "# ops/ci_tests.sh is described elsewhere\n"
                                                      "jobs:\n  t:\n    steps:\n"
                                                      "      - run: echo ok\n"})
    _closed_red(world, capsys, {10: {"b.txt": "b\n"}, 11: {"ops/ci_tests.sh": "exit 0\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 0, capsys.readouterr()


@pytest.mark.parametrize("path", [
    "rust/.config/nextest.toml", "pytest.ini", "rust/clippy.toml", "rust/rustfmt.toml",
    "frontend/tsconfig.json", "frontend/vite.config.ts", ".python-version"])
def test_after_drop_refuses_a_dropped_member_changing_a_tools_own_settings(world, capsys, path):
    # Codex review of 5291a869 (P1): `cargo nextest` reads .config/nextest.toml
    # with no workflow naming it.
    _closed_red(world, capsys, {10: {"b.txt": "b\n"}, 11: {path: "x = 1\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "changes code every test or job shares" in err and path in err


@pytest.mark.parametrize("settled", [False, True])
def test_after_drop_needs_a_job_that_failed_on_the_closed_head_to_run_here(world, capsys,
                                                                          settled):
    # Codex review of 5291a869 (P2): a job that failed on the closed head and
    # is not reported on the new one yet is waited for, and refused once
    # every run there finished without it.
    head = _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"), _run("Parity / windows-latest")],
        rollup=[_run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
                _run("Parity / windows-latest"), _run("Rust daemon / disposable image", "FAILURE")])
    world.gh.run_lists[head] = [{"status": "COMPLETED" if settled else "IN_PROGRESS",
                                 "name": "Rust", "databaseId": 2}]

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    if settled:
        assert "Rust daemon / disposable image did not pass on #900's head (failure)" in err
        assert "never ran on #901's head" in err
    else:
        assert "Rust daemon / disposable image (not reported here yet" in err
    assert _merge_calls(world) == []


def test_after_drop_ignores_a_job_that_passed_on_the_closed_head_and_is_absent_here(world, capsys):
    # The near miss: absent here and successful there is not waited for.
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"), _run("Parity / windows-latest")],
        rollup=[_run("scope"), _run("test", "FAILURE"), _run("Parity / ubuntu-latest"),
                _run("Parity / windows-latest"), _run("Rust daemon / disposable image")])

    assert integrate.main(["merge"]) == 0, capsys.readouterr()


def test_after_drop_merge_reads_every_check_again_after_the_comment(world, capsys):
    # Codex review of 5291a869 (P1): a carried job that fails while the
    # comment is posted refuses the click.
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest"), _run("Rust / windows-latest", None, status="QUEUED")])
    original = world.gh.__class__.__call__

    def red_on_comment(self, *args, input=None):
        out = original(self, *args, input=input)
        if list(args[:2]) == ["pr", "comment"]:
            self.pr(901)["statusCheckRollup"][-1] = _run(
                "Rust / windows-latest", "FAILURE", started="2026-10-21T11:00:00Z")
        return out

    world.gh.__class__ = type("RedOnComment", (world.gh.__class__,), {"__call__": red_on_comment})

    assert integrate.main(["merge"]) == 2

    assert "Rust / windows-latest (FAILURE)" in capsys.readouterr().err
    assert _merge_calls(world) == []


@pytest.mark.parametrize("path", ["rust/shim/tests/common/mod.rs", "rust/shim/tests/common/cli.rs",
                                  "rust/daemon/tests/support/fixtures.json"])
def test_after_drop_refuses_a_dropped_member_changing_a_shared_rust_test_module(world, capsys,
                                                                               path):
    # Codex review of 1a47d8cb (P1): tests/common/ is compiled into many
    # Rust test binaries.
    _closed_red(world, capsys, {10: {"b.txt": "b\n"}, 11: {path: "x\n"}, 12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "changes code every test or job shares" in err and path in err


def test_after_drop_takes_a_dropped_member_changing_one_rust_test_binary(world, capsys):
    # The near miss: a top-level tests/*.rs is one test binary of its own.
    _closed_red(world, capsys, {10: {"b.txt": "b\n"}, 11: {"rust/shim/tests/one_case.rs": "x\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 0, capsys.readouterr()


def test_after_drop_merge_does_not_take_a_skipped_rerun_for_a_success(world, capsys):
    # Codex review of 1a47d8cb (P1): a failure followed by a skipped run of
    # the same check passed nothing.
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest"),
        _run("Rust / windows-latest", "FAILURE", started="2026-10-21T10:00:00Z"),
        _run("Rust / windows-latest", "SKIPPED", started="2026-10-21T11:00:00Z")])

    assert integrate.main(["merge"]) == 2

    assert "no finished rerun has replaced the failure" in capsys.readouterr().err
    assert _merge_calls(world) == []


@pytest.mark.parametrize("declaration", ["mod auth_fixture;\n", "#[path = \"x\"]\npub mod auth_fixture;\n"])
def test_after_drop_refuses_a_rust_test_file_other_binaries_compile_in(world, capsys,
                                                                      declaration):
    # Codex review of 28d22ac2 (P1): tests/auth_fixture.rs, pulled into other
    # test binaries with `mod auth_fixture;`.
    world.advance_master({"rust/shim/tests/uses_it.rs": declaration + "fn t() {}\n"})
    _closed_red(world, capsys, {10: {"b.txt": "b\n"},
                                11: {"rust/shim/tests/auth_fixture.rs": "pub fn f() {}\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 2

    err = capsys.readouterr().err
    assert "changes code every test or job shares" in err
    assert "rust/shim/tests/auth_fixture.rs" in err


def test_after_drop_takes_a_rust_test_file_no_binary_compiles_in(world, capsys):
    # The near miss: a module name that merely starts the same.
    world.advance_master({"rust/shim/tests/uses_it.rs": "mod auth_fixture_two;\nfn t() {}\n"})
    _closed_red(world, capsys, {10: {"b.txt": "b\n"},
                                11: {"rust/shim/tests/auth_fixture.rs": "pub fn f() {}\n"},
                                12: {"d.txt": "d\n"}})

    assert _build("--after-drop", "900") == 0, capsys.readouterr()


@pytest.mark.parametrize("first", ["STALE", "ACTION_REQUIRED"])
def test_after_drop_merge_holds_any_non_pass_until_a_success(world, capsys, first):
    # Codex review of 28d22ac2 (P2): a stale run whose rerun is pending.
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
        _run("Parity / windows-latest"),
        _run("Rust / windows-latest", first, started="2026-10-21T10:00:00Z"),
        _run("Rust / windows-latest", None, status="IN_PROGRESS",
             started="2026-10-21T11:00:00Z")])

    assert integrate.main(["merge"]) == 2

    assert "no finished rerun has replaced the failure" in capsys.readouterr().err
    assert _merge_calls(world) == []


def _skipped_lane_after_a_failure(world, capsys) -> str:
    """#901's head skips `test-lite-windows`, which failed on #900's."""
    return _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"), _run("Parity / windows-latest"),
        _run("test-lite-windows", "SKIPPED")],
        rollup=[_run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
                _run("Parity / windows-latest"), _run("test-lite-windows", "FAILURE")])


def test_after_drop_counts_a_skip_as_a_pass_as_merge_does(world, capsys):
    # AFTER_DROP_SKIP_PASSES (True, pending the conductor): scope skips a
    # lane once the dropped change that triggered it is gone.
    _skipped_lane_after_a_failure(world, capsys)

    assert integrate.main(["merge"]) == 0, capsys.readouterr()


def test_after_drop_demands_a_success_when_skips_do_not_pass(world, capsys, monkeypatch):
    # Codex review of f68d07e7 (P1): with the flag off, a lane that failed
    # on the closed head and was skipped here refuses.
    monkeypatch.setattr(integrate, "AFTER_DROP_SKIP_PASSES", False)
    _skipped_lane_after_a_failure(world, capsys)

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "test-lite-windows did not pass on #900's head and was skipped on #901's head" in err
    assert "not a pass for a job that failed before" in err
    assert _merge_calls(world) == []


@pytest.mark.parametrize("skips_pass", [True, False])
def test_after_drop_carries_an_absent_job_skipped_on_the_closed_head_only_by_the_flag(
        world, capsys, monkeypatch, skips_pass):
    monkeypatch.setattr(integrate, "AFTER_DROP_SKIP_PASSES", skips_pass)
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"), _run("Parity / windows-latest")],
        rollup=[_run("scope"), _run("test", "FAILURE"), _run("Parity / ubuntu-latest"),
                _run("Parity / windows-latest"), _run("test-lite-macos", "SKIPPED")])

    code = integrate.main(["merge"])

    err = capsys.readouterr().err
    if skips_pass:
        assert code == 0, err
    else:
        assert code == 2 and "test-lite-macos did not pass on #900's head (skipped)" in err


def _lane_after_a_failure(world, capsys, *new_checks: dict) -> str:
    """#901's head with `new_checks` beside the required lanes; `test-lite-windows`
    failed on #900's head."""
    return _after_drop_opened(world, capsys, [
        _run("test"), _run("Parity / ubuntu-latest"), _run("Parity / windows-latest"), *new_checks],
        rollup=[_run("scope"), _run("test"), _run("Parity / ubuntu-latest"),
                _run("Parity / windows-latest"), _run("test-lite-windows", "FAILURE")])


def test_after_drop_takes_a_lane_its_successful_scope_skipped_in_a_clean_run(world, capsys):
    # The conductor's ruling: a skip stands for a pass only behind the
    # workflow's own successful scope, in a run with nothing red.
    _lane_after_a_failure(world, capsys, _run("scope"), _run("test-lite-windows", "SKIPPED"))

    assert integrate.main(["merge"]) == 0, capsys.readouterr()


def test_after_drop_refuses_a_skip_while_its_scope_failed(world, capsys):
    _lane_after_a_failure(world, capsys, _run("scope", "FAILURE"),
                          _run("test-lite-windows", "SKIPPED"))

    assert integrate.main(["merge"]) == 2

    assert "scope (FAILURE)" in capsys.readouterr().err and _merge_calls(world) == []


def test_after_drop_refuses_a_skip_in_a_run_with_a_cancelled_job(world, capsys):
    _lane_after_a_failure(world, capsys, _run("scope"), _run("test-lite-windows", "SKIPPED"),
                          _run("test-lite-macos", "CANCELLED"))

    assert integrate.main(["merge"]) == 2

    assert "test-lite-macos (CANCELLED)" in capsys.readouterr().err and _merge_calls(world) == []


def test_after_drop_refuses_a_neutral_job_after_a_failure(world, capsys):
    _lane_after_a_failure(world, capsys, _run("scope"), _run("test-lite-windows", "NEUTRAL"))

    assert integrate.main(["merge"]) == 2

    err = capsys.readouterr().err
    assert "test-lite-windows did not pass on #900's head and was neutral" in err
    assert "not a pass for a job that failed before" in err


def test_after_drop_takes_a_job_neutral_on_both_heads(world, capsys):
    # The near miss: neutral on the closed head too, so it did not fail before.
    _after_drop_opened(world, capsys, [
        _run("scope"), _run("test"), _run("Parity / ubuntu-latest"), _run("Parity / windows-latest"),
        _run("CodeQL", "NEUTRAL")],
        rollup=[_run("scope"), _run("test", "FAILURE"), _run("Parity / ubuntu-latest"),
                _run("Parity / windows-latest"), _run("CodeQL", "NEUTRAL")])

    assert integrate.main(["merge"]) == 0, capsys.readouterr()


def _skip_plan() -> "integrate.Plan":
    return integrate.Plan(branch=f"integrate/{DATE}-batch-2", base_sha="0" * 40, head="1" * 40,
                          pr=901, after_drop={"batch": 900})


def _keyed(*checks: dict) -> dict:
    return {integrate._check_key(c): c for c in checks}


@pytest.mark.parametrize("scope, cause", [
    (_run("scope", "FAILURE"), "scope job did not succeed (failure)"),
    (_run("scope", None, status="IN_PROGRESS"), "scope job did not succeed (unfinished)"),
    (None, "scope job did not succeed (missing)")])
def test_after_drop_names_the_scope_condition_a_skip_fails(scope, cause):
    lane = _run("test-lite-windows", "SKIPPED")
    new = _keyed(lane, *([scope] if scope else []))

    assert cause in integrate._after_drop_skipped(lane, new, _skip_plan())


def test_after_drop_names_the_red_job_that_voids_a_skip():
    lane = _run("test-lite-windows", "SKIPPED")
    new = _keyed(lane, _run("scope"), _run("test-lite-macos", "CANCELLED"))

    why = integrate._after_drop_skipped(lane, new, _skip_plan())

    assert "CI's run there has test-lite-macos red or cancelled" in why


def test_after_drop_judges_a_rust_lane_skip_by_the_rust_workflows_scope():
    lane = _run("Parity checks / ubuntu-latest", "SKIPPED", workflow="Rust shim")
    plan = _skip_plan()

    assert integrate._after_drop_skipped(lane, _keyed(lane, _run("Scope", workflow="Rust shim")),
                                         plan) is None
    # CI's `scope` is not the Rust workflow's.
    assert "scope job did not succeed (missing)" in integrate._after_drop_skipped(
        lane, _keyed(lane, _run("scope")), plan)
