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

    class World:
        pass

    w = World()
    w.origin, w.seed, w.work, w.gh = origin, seed, work, fake

    def pr(number: int, files: dict[str, str], *, base: str = "origin/master") -> str:
        _git(seed, "fetch", "-q", "origin")
        _git(seed, "switch", "-q", "-C", f"pr-{number}", base)
        for name, text in files.items():
            (seed / name).write_text(text, encoding="utf-8")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", f"change {number}")
        _git(seed, "push", "-q", "-f", "origin", f"HEAD:refs/pull/{number}/head")
        return _git(seed, "rev-parse", "HEAD")

    def advance_master(files: dict[str, str]) -> None:
        _git(seed, "fetch", "-q", "origin")
        _git(seed, "switch", "-q", "-C", "master", "origin/master")
        for name, text in files.items():
            (seed / name).write_text(text, encoding="utf-8")
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
    return [line.split()[1] for line in reversed(log.splitlines())]


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
], ids=["in-progress", "status-pending", "none", "green"])
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
    # CI deselects the census currency test and runs --ci-check instead.
    assert "-k 'not committed_census_matches_current_tree'" in workflow
    deselected = integrate.RUST_HARNESS[integrate.RUST_HARNESS.index("--deselect") + 1]
    module, name = deselected.split("::")
    assert f"def {name}(" in (ROOT / module).read_text(encoding="utf-8")


# The real census module with only its snapshot faked (a hash of each
# scanned `ops/*.sh`), so the gate under test is the real `--ci-check`
# decision: drift is fatal only when the diff from the event's base touches
# CURRENCY_INPUTS.
_CENSUS_STUB = """\
import functools, hashlib, importlib.util, os
from pathlib import Path

if __name__ == "__main__" and "GH_TOKEN" in os.environ:
    raise SystemExit("the census ran with the operator's GH_TOKEN")
spec = importlib.util.spec_from_file_location("census_real", {real!r})
census = importlib.util.module_from_spec(spec)
spec.loader.exec_module(census)
ROOT = Path(__file__).resolve().parents[1]
CURRENCY_INPUTS = census.CURRENCY_INPUTS
census.OUTPUT = ROOT / "rust/producer-census.json"
census.snapshot = lambda: {{
    "scanned": {{p.name: hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest()
                for p in sorted((ROOT / "ops").glob("*.sh"))}},
    "surface": [], "inventory_gaps": [], "no_observed_producer": [], "unresolved": []}}
census.ci_currency_required = functools.partial(census.ci_currency_required, ROOT)

if __name__ == "__main__":
    census.main()
"""


def _census(**scanned: str) -> str:
    """The census `rust/producer_census.py` would write for these `ops/*.sh`."""
    import hashlib
    hashes = {f"{name}.sh": hashlib.sha256(text.encode()).hexdigest()
              for name, text in sorted(scanned.items())}
    return json.dumps({"scanned": hashes, "surface": [], "inventory_gaps": [],
                       "no_observed_producer": [], "unresolved": []}, indent=2) + "\n"


def _with_census(world) -> None:
    """Master gains the census module, the port inventory it reads, a
    scanned `ops/tool.sh` and a census current for it."""
    (world.seed / "rust").mkdir(exist_ok=True)
    (world.seed / "ops").mkdir(exist_ok=True)
    stub = _CENSUS_STUB.format(real=str(ROOT / "rust" / "producer_census.py"))
    world.advance_master({"rust/producer_census.py": stub, "ops/tool.sh": "t\n",
                          "rust/producer-census.json": _census(tool="t\n"),
                          "rust/contract-inventory.json": "{}\n"})


def test_a_member_whose_census_is_stale_is_dropped_before_ci_sees_it(world, capsys):
    # #865 on 2026-10-20: #842 regenerated the census for its own change, but
    # #851 then added scanned files to master without regenerating it (report
    # only there). The batch diff touched the census, so CI's gate was fatal.
    _with_census(world)
    sha10 = world.pr(10, {"ops/ten.sh": "10\n",
                          "rust/producer-census.json": _census(tool="t\n", ten="10\n")})
    world.advance_master({"ops/other.sh": "o\n"})        # census now stale on master
    sha11 = world.pr(11, {"b.txt": "b\n"})
    sha12 = world.pr(12, {"c.txt": "c\n"})
    for n, sha in ((10, sha10), (11, sha11), (12, sha12)):
        world.gh.add(n, sha)

    assert _publish(world) == 0, capsys.readouterr()

    branch = f"integrate/{DATE}-batch-1"
    assert _second_parents(world.origin, branch) == [sha11, sha12]
    (args, body), = world.gh.bodies("comment")
    assert args[2] == "10" and branch in body
    assert "rust/producer-census.json" in body and "census is stale" in body
    assert "python rust/producer_census.py" in body and "also" not in body
    (edit_args, _), = world.gh.bodies("edit")
    assert edit_args[2] == "10"
    (_, create_body), = world.gh.bodies("create")
    assert "#10" in create_body and "census" in create_body


def test_every_member_touching_the_census_inputs_is_named_and_dropped(world, capsys):
    _with_census(world)
    sha10 = world.pr(10, {"rust/contract-inventory.json": '{"tools": []}\n'})
    sha11 = world.pr(11, {"ops/ten.sh": "10\n",
                          "rust/producer-census.json": _census(tool="t\n", ten="10\n")})
    world.advance_master({"ops/other.sh": "o\n"})
    sha12 = world.pr(12, {"b.txt": "b\n"})
    sha13 = world.pr(13, {"c.txt": "c\n"})
    for n, sha in ((10, sha10), (11, sha11), (12, sha12), (13, sha13)):
        world.gh.add(n, sha)

    assert _publish(world) == 0, capsys.readouterr()

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha12, sha13]
    comments = {args[2]: body for args, body in world.gh.bodies("comment")}
    assert set(comments) == {"10", "11"}
    assert "rust/contract-inventory.json" in comments["10"] and "#11" in comments["10"]
    assert "rust/producer-census.json" in comments["11"] and "#10" in comments["11"]


def test_a_stale_census_no_member_touches_is_reported_not_dropped(world, capsys):
    # The near miss: drift outside the census inputs is report-only in CI,
    # so a member that changes a scanned file but neither the census nor the
    # inventory stays in, even with the census already stale on master.
    _with_census(world)
    world.advance_master({"ops/other.sh": "o\n"})        # stale on master itself
    sha10 = world.pr(10, {"ops/ten.sh": "10\n"})
    sha11 = world.pr(11, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _publish(world) == 0
    out = capsys.readouterr().out

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha11]
    assert "report only" in out
    assert world.gh.bodies("comment") == [] and world.gh.bodies("edit") == []


def test_a_member_with_a_current_census_joins_the_batch(world, capsys):
    _with_census(world)
    sha10 = world.pr(10, {"ops/ten.sh": "10\n",
                          "rust/producer-census.json": _census(tool="t\n", ten="10\n")})
    sha11 = world.pr(11, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _publish(world) == 0, capsys.readouterr()

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha11]
    assert world.gh.bodies("comment") == []


def test_open_refuses_a_batch_whose_census_ci_would_fail(world, capsys, monkeypatch):
    # A plan built without the gate (an older script) is caught before the push.
    _with_census(world)
    sha10 = world.pr(10, {"ops/ten.sh": "10\n",
                          "rust/producer-census.json": _census(tool="t\n", ten="10\n")})
    world.advance_master({"ops/other.sh": "o\n"})
    sha11 = world.pr(11, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    real = integrate._census_culprits
    monkeypatch.setattr(integrate, "_census_culprits", lambda plan: [])
    assert _build() == 0
    monkeypatch.setattr(integrate, "_census_culprits", real)
    capsys.readouterr()

    assert _open(world) == 2
    err = capsys.readouterr().err

    assert "#10" in err and "rust/producer-census.json" in err and "build again" in err
    assert world.remote_branches() == [] and world.gh.mutations() == []


def test_the_census_runs_without_the_operators_credentials(world, capsys, monkeypatch):
    # Member code: the stub exits non-zero (a crash, not "stale") if it sees
    # GH_TOKEN, which `build` would report as a refusal.
    monkeypatch.setenv("GH_TOKEN", "operator-secret")
    _with_census(world)
    sha10 = world.pr(10, {"ops/ten.sh": "10\n",
                          "rust/producer-census.json": _census(tool="t\n", ten="10\n")})
    sha11 = world.pr(11, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _publish(world) == 0, capsys.readouterr()
    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha11]


def test_a_census_drop_leaves_a_batch_of_the_rest(world, capsys):
    _with_census(world)
    sha10 = world.pr(10, {"ops/ten.sh": "10\n",
                          "rust/producer-census.json": _census(tool="t\n", ten="10\n")})
    world.advance_master({"ops/other.sh": "o\n"})
    sha11 = world.pr(11, {"b.txt": "b\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)

    assert _publish(world) == 0, capsys.readouterr()

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha11]
    (args, body), = world.gh.bodies("comment")
    assert args[2] == "10" and body.startswith("Left out of integration batch")
    assert "once master holds the rest of the batch" in body


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
    ([{"status": "COMPLETED", "conclusion": "CANCELLED"}], "failing"),
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
