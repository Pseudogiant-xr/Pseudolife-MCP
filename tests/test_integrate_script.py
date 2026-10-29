"""ops/integrate.py: hand-built merge batches of `ready-to-batch` PRs.

The git side is real: a bare `origin` whose `refs/pull/<n>/head` refs carry
each PR's head, the way GitHub serves them, and a clone the script runs in.
Only `gh` is faked, by replacing the module's `gh()` with a recorder that
answers the handful of calls the script makes.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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
                   "state", "isCrossRepository", "url", "mergeCommit"}
    VIEW_DEFAULTS = {"baseRefName": "master", "state": "OPEN", "isCrossRepository": False,
                     "mergeCommit": None,
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
            assert args[1].endswith("/annotations") and args[2:] == ["--jq", ".[].message"]
            job = args[1].split("/")[-2]
            if job not in self.annotations:
                raise integrate.Refusal("`gh api` failed (1): HTTP 502")
            return "".join(f"{m}\n" for m in self.annotations[job])
        if args[0] == "api":
            number = int(next(a for a in args if "/issues/" in a).split("/")[-2])
            return self.labeled_at.get(number, "") + "\n"
        if args[:2] == ["pr", "create"]:
            if self.fail_create == "before":
                raise integrate.Refusal("`gh pr create` failed (1): HTTP 502")
            url = "https://github.example/pr/900"
            head = args[args.index("--head") + 1]
            self.history.append({"number": 900, "state": "OPEN", "url": url,
                                 "headRefName": head, "isCrossRepository": False,
                                 "headRefOid": _git(self.origin, "rev-parse", head)})
            if self.fail_create == "after":  # created, but the reply was lost
                raise integrate.Refusal("`gh pr create` failed (1): HTTP 504")
            return url + "\n"
        if args[:2] == ["pr", "comment"] and self.fail_comments:
            self.fail_comments -= 1
            raise integrate.Refusal("`gh pr comment` failed (1): HTTP 502")
        if args[:2] == ["pr", "edit"] and self.fail_edits:
            self.fail_edits -= 1
            raise integrate.Refusal("`gh pr edit` failed (1): HTTP 502")
        if args[:2] in (["pr", "comment"], ["pr", "edit"]):
            return ""
        raise AssertionError(f"unexpected gh call: {args}")


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


@pytest.fixture
def world(tmp_path, monkeypatch):
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

    class World:
        pass

    w = World()
    w.origin, w.seed, w.work, w.gh = origin, seed, work, fake

    def cargo_calls() -> list[dict]:
        if not cargo_log.is_file():
            return []
        return [json.loads(line) for line in cargo_log.read_text(encoding="utf-8").splitlines()]

    w.cargo_calls = cargo_calls

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
    return w


def _build(*extra: str) -> int:
    return integrate.main(["build", "--date", DATE, *extra])


_UNSET = object()


def _suite_result(world, *, commit: str | None = None, exit_code: int = 0,
                  args=()) -> Path:
    """A result as ops/remote-suite.ps1 writes it (with a BOM, as Windows
    PowerShell 5 would); `args=_UNSET` mimics a launcher too old to record them."""
    result = {"machine": "local-wsl", "exit_code": exit_code,
              "commit": commit or _git(world.work, "rev-parse", "HEAD"),
              "summary": "18868 passed, 81 skipped in 600s"}
    if args is not _UNSET:
        result["args"] = list(args)
    path = world.work.parent / "suite-result.json"
    path.write_text(json.dumps(result), encoding="utf-8-sig")
    return path


def _harness_note(world) -> str:
    head = _git(world.work, "rev-parse", "HEAD")
    return f"767 passed at {head[:8]} on the maintainer's Windows host"


def _open(world, **result) -> int:
    return integrate.main(["open", "--suite", str(_suite_result(world, **result)),
                           "--harness-note", _harness_note(world)])


def _publish(world) -> int:
    """build, then open with a passing suite result for the built head."""
    code = _build()
    return code if code else _open(world)


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
    # oracle_selection.release_cut() accepts a cut only when nothing else
    # changes, so a cut batched with code would run parity against the old
    # release; the others keep their labels and join the next batch.
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
    assert world.gh.bodies("comment") == [] and world.gh.bodies("edit") == []


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


@pytest.mark.parametrize("messages, joins", [
    ([TIMEOUT, "The operation was canceled."], False),   # a hang: a known failure
    (None, False),                                         # annotations unreadable
    (["The operation was canceled."], True),               # superseded or cancelled by hand
    ([], True),
    (["Note: The job has exceeded the maximum execution time of 25m0s"], True),
], ids=["timed-out", "unreadable", "cancelled", "no-annotations", "quoted-near-miss"])
def test_a_cancelled_job_joins_unless_it_ran_past_its_timeout(world, capsys, messages, joins):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[
        {"status": "COMPLETED", "conclusion": "SUCCESS"}, _cancelled_job("123")])
    if messages is not None:
        world.gh.annotations["123"] = messages

    assert _build() == 0, capsys.readouterr()
    out = capsys.readouterr().out

    assert ("left out #11: CI on its head is failing" in out) is not joins


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
    assert f"Local full suite: passed at `{head}` on local-wsl: 18868 passed" in body
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
    note = f"passed at {head[:8]} on the Linux dev box, 18868 passed"
    assert integrate.main(["open", "--suite-note", note,
                           "--harness-note", _harness_note(world)]) == 0, capsys.readouterr()

    (_, body), = world.gh.bodies("create")
    assert f"Local full suite: {note} (stated by the operator, not checked)" in body


def test_build_names_the_rust_harness_run_the_suite_does_not_cover(world, capsys):
    _three_with_a_collision(world)

    assert _build() == 0
    out = capsys.readouterr().out

    assert integrate.RUST_HARNESS_COMMAND in out and "--harness-note" in out
    assert world.remote_branches() == [] and world.gh.mutations() == []


@pytest.mark.parametrize("note", [None, "767 passed, trust me"])
def test_open_refuses_without_the_rust_harness_result_for_this_head(world, capsys, note):
    _three_with_a_collision(world)
    assert _build() == 0
    capsys.readouterr()

    argv = ["open", "--suite", str(_suite_result(world))]
    assert integrate.main(argv + (["--harness-note", note] if note else [])) == 2

    err = capsys.readouterr().err
    head = _git(world.work, "rev-parse", "HEAD")
    assert head[:8] in err and ("--harness-note" in err)
    if note is None:
        assert integrate.RUST_HARNESS_COMMAND in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


@pytest.mark.parametrize("result", ["passed at {head}", "0 passed at {head}",
                                    "3 failed, 764 passed at {head}",
                                    "764 passed, 1 error at {head}",
                                    "764 passed, 2 errors at {head}"])
def test_open_refuses_a_harness_note_without_a_clean_pass_count(world, capsys, result):
    _three_with_a_collision(world)
    assert _build() == 0
    capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")

    assert integrate.main(["open", "--suite", str(_suite_result(world)),
                           "--harness-note", result.format(head=head[:8])]) == 2

    assert "passed" in capsys.readouterr().err
    assert world.remote_branches() == [] and world.gh.mutations() == []


@pytest.mark.parametrize("result", ["767 passed at {head} (error-free)",
                                    "767 passed at {head}; log logs/errors.log",
                                    "767 passed, 0 failed at {head}"])
def test_open_accepts_a_clean_harness_note_that_mentions_errors_in_prose(world, capsys, result):
    _three_with_a_collision(world)
    assert _build() == 0
    capsys.readouterr()
    head = _git(world.work, "rev-parse", "HEAD")

    assert integrate.main(["open", "--suite", str(_suite_result(world)),
                           "--harness-note", result.format(head=head[:8])]) == 0, \
        capsys.readouterr()


def test_open_records_the_rust_harness_result_beside_the_suite(world, capsys):
    _three_with_a_collision(world)
    assert _build() == 0

    assert _open(world) == 0, capsys.readouterr()

    (_, body), = world.gh.bodies("create")
    assert (f"Rust harness Python tests (`{integrate.RUST_HARNESS_COMMAND}`, which the "
            f"full suite does not collect): {_harness_note(world)} (stated by the "
            "operator, not checked)") in body


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
            f"neither run above compiles): passed at `{head}`") in body


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
    assert "## Producer census" in create_body and sha in create_body
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
    assert "## Producer census" not in create_body


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
    census_section = create_body.split("## Producer census", 1)[1].split("\n## ", 1)[0]
    assert "`ops/tool.sh`" in census_section and "ops/old.sh" not in census_section


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
    census_section = create_body.split("## Producer census", 1)[1].split("\n## ", 1)[0]
    assert "`ops/tool.sh`" in census_section and "regenerates" not in census_section


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

    assert "## Producer census" in out
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
