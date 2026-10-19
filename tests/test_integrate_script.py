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
        self.fail_create = False

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

    def mutations(self) -> list[list[str]]:
        return [args for args, _ in self.calls
                if args[:2] in (["pr", "create"], ["pr", "comment"], ["pr", "edit"])]

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
        if args[0] == "api":
            number = int(next(a for a in args if "/issues/" in a).split("/")[-2])
            return self.labeled_at.get(number, "") + "\n"
        if args[:2] == ["pr", "create"]:
            if self.fail_create:
                raise integrate.Refusal("`gh pr create` failed (1): HTTP 502")
            return "https://github.example/pr/900\n"
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
    monkeypatch.setattr(integrate, "gh", fake)

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


def _suite_result(world, *, commit: str | None = None, exit_code: int = 0) -> Path:
    path = world.work.parent / "suite-result.json"
    path.write_text(json.dumps({
        "machine": "local-wsl", "exit_code": exit_code,
        "commit": commit or _git(world.work, "rev-parse", "HEAD"),
        "summary": "18868 passed, 81 skipped in 600s"}), encoding="utf-8-sig")
    return path


def _open(world, **result) -> int:
    return integrate.main(["open", "--suite", str(_suite_result(world, **result))])


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


def test_fewer_than_two_clean_members_opens_no_batch(world, capsys):
    sha10 = world.pr(10, {"a.txt": "one\nTEN\nthree\n"})
    sha11 = world.pr(11, {"a.txt": "one\nELEVEN\nthree\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11)
    before = _git(world.work, "symbolic-ref", "-q", "HEAD")

    assert _build() == 0
    out = capsys.readouterr().out

    assert world.remote_branches() == []
    assert world.gh.bodies("create") == []
    assert "#10" in out and "directly" in out
    (args, _), = world.gh.bodies("comment")  # the collision is still reported
    assert args[2] == "11"
    assert _git(world.work, "symbolic-ref", "-q", "HEAD") == before
    assert _git(world.work, "branch", "--list", "integrate/*") == ""


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


def test_a_member_without_green_ci_on_its_head_sits_out_and_keeps_its_label(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    sha12 = world.pr(12, {"d.txt": "d\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[{"status": "IN_PROGRESS", "conclusion": None}])
    world.gh.add(12, sha12)

    assert _publish(world) == 0
    out = capsys.readouterr().out

    assert _second_parents(world.origin, f"integrate/{DATE}-batch-1") == [sha10, sha12]
    assert "#11" in out and "pending" in out
    assert world.gh.bodies("comment") == [] and world.gh.bodies("edit") == []


def test_too_few_green_members_builds_nothing_and_says_why(world, capsys):
    sha10 = world.pr(10, {"b.txt": "b\n"})
    sha11 = world.pr(11, {"c.txt": "c\n"})
    world.gh.add(10, sha10)
    world.gh.add(11, sha11, statusCheckRollup=[])

    assert _build() == 0
    out = capsys.readouterr().out

    assert "1 of 2" in out and "#11 CI on its head is none" in out
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
    world.gh.fail_create = True

    assert _publish(world) == 2
    err = capsys.readouterr().err

    branch = f"integrate/{DATE}-batch-1"
    assert "HTTP 502" in err and "rerun `open`" in err
    assert world.remote_branches() == [branch]
    assert world.gh.bodies("comment") == []  # nothing claims a batch that has no PR

    world.gh.fail_create = False
    assert _open(world) == 0, capsys.readouterr()
    # The failed attempt and the one that opened it; the drop is reported once.
    assert len(world.gh.bodies("create")) == 2 and len(world.gh.bodies("comment")) == 1


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
    assert not (world.work / plan).exists()


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

    note = "passed on the Linux dev box, 18868 passed"
    assert integrate.main(["open", "--suite-note", note]) == 0, capsys.readouterr()

    (_, body), = world.gh.bodies("create")
    assert f"Local full suite: {note} (stated by the operator, not checked)" in body


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
