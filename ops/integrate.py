"""Build hand-rolled merge batches of reviewed pull requests.

Master takes one pull request at a time, and each one must pass CI on its
head merged with current master, so every merge sends the other open pull
requests back for a fresh CI run. A batch merges several reviewed pull
requests into one `integrate/<YYYY-MM-DD>-batch-N` branch that is validated
and merged once. The rule that governs batches is in CLAUDE.md ("Running
tests"); the walkthrough is `docs/dev/merge-batches.md`.

    python ops/integrate.py status            # labelled PRs, CI, mergeability
    python ops/integrate.py build --dry-run   # what a build would do
    python ops/integrate.py build             # merge the batch locally
    python ops/integrate.py open --suite <result.json> --harness-note "<result>"
                                              # push it, open its PR
    python ops/integrate.py merge             # merge the open batch PR, once green

A pull request joins by carrying the `ready-to-batch` label. `build` runs
in a clean checkout whose HEAD is origin/master, merges each labelled head
with `git merge --no-ff` in the order the label was applied (pull request
number when unknown) onto a new local branch, and drops a member that
conflicts, aborting that merge. It pushes nothing, so the local full suite
runs on the batch head before any CI, with the Python tests under rust/ the
suite does not collect (`RUST_HARNESS`). `open` then checks the suite passed
on that exact head, records both results, pushes the branch, opens the batch pull request, and
comments on each dropped member naming what it collided with (removing its
label). A member whose own diff changes the producer census inputs is
dropped the same way when the batch head would fail CI's census check
(`rust/producer_census.py --ci-check`). Nothing resolves a conflict and
nothing force-pushes. `merge` merges the batch pull request `open` published,
with a merge commit, only while its head is the built head, every check on
it finished without a failure and master has not moved past it; every merge
is an admin bypass, so this is the check GitHub does not make. Needs git
2.38+ (`git merge-tree --write-tree`) and an authenticated `gh`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

LABEL = "ready-to-batch"
BASE = "master"
REMOTE = "origin"
PREFIX = "integrate/"
# A batch of one is allowed: a pull request's own CI runs only the fast lanes,
# so a lone ready one (an urgent fix, a revert, a release cut) gets its full
# CI as a batch of one instead of merging directly.
MIN_MEMBERS = 1
# A release cut (a member changing project.version in pyproject.toml) is
# batched alone: evals/rust_port/oracle_selection.py release_cut() accepts a
# cut only when nothing else changes, so a cut batched with code would run
# parity against the old release.
VERSION_LINE_RE = re.compile(r'version\s*=\s*"([^"]*)"\s*$')
BATCH_RE = re.compile(r"^integrate/(\d{4}-\d{2}-\d{2})-batch-(\d+)$")
MARKER_RE = r"^(<<<<<<<|>>>>>>>)( |$)"
CENSUS = "rust/producer_census.py"
# The Python tests under rust/ that the local full suite never collects
# (pytest's testpaths is tests/) and that need no built Rust binary: the
# pytest steps of rust.yml's `rust` job plus the eval parity shard's
# `pytest rust/ci`. #877 broke rust/daemon/harness/test_w2e_lifecycle.py and
# passed its batch's local suite this way (2026-10-21). The census currency
# test is deselected as CI deselects it: `build` runs CI's own verdict,
# `producer_census.py --ci-check`. 767 tests, about 140 s natively on
# Windows (2026-10-21).
RUST_HARNESS = (
    "rust/test_contract_inventory.py", "rust/test_producer_census.py",
    "rust/daemon/harness/test_w2e_lifecycle.py",
    "rust/daemon/harness/board/test_norm.py", "rust/daemon/harness/board/test_run.py",
    "rust/ci",
    "--deselect", "rust/test_producer_census.py::test_committed_census_matches_current_tree",
)
RUST_HARNESS_COMMAND = "python -m pytest -q -p no:cacheprovider " + " ".join(RUST_HARNESS)


class Refusal(Exception):
    """A precondition failed; nothing was changed."""


def _run(argv: list[str], *, input: str | None = None, check: bool = True,
         **kwargs) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", input=input,
                              **kwargs)
    except FileNotFoundError:
        raise Refusal(f"{argv[0]} is not on PATH") from None
    if check and proc.returncode != 0:
        raise Refusal(f"`{' '.join(argv)}` failed ({proc.returncode}): "
                      f"{proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["git", *args], check=check)


def gh(*args: str, input: str | None = None) -> str:
    """Run gh and return its stdout (tests replace this function)."""
    return _run(["gh", *args], input=input).stdout


def _out(*args: str) -> str:
    return git(*args).stdout.strip()


@dataclass
class Member:
    number: int
    title: str
    sha: str
    url: str
    review: str
    checks: str
    labeled_at: str | None = None
    draft: bool = False


@dataclass
class Dropped:
    member: Member
    reason: str
    comment: bool = True
    commented: bool = False
    unlabelled: bool = False


@dataclass
class Plan:
    branch: str
    base_sha: str
    label: str = LABEL
    head: str = ""
    pr: int = 0      # the batch pull request, once `open` published it
    merged: list[Member] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        date, n = BATCH_RE.match(self.branch).groups()
        numbers = ", ".join(f"#{m.number}" for m in self.merged)
        return f"integrate: {date} batch {n} ({numbers})"

    @staticmethod
    def path(branch: str) -> Path:
        """Where `build` leaves the plan for `open`: inside this worktree's
        git directory, so it is never tracked or shared between worktrees."""
        name = branch.removeprefix(PREFIX) + ".json"
        return Path(_out("rev-parse", "--git-path", f"integrate-plans/{name}"))

    def save(self) -> None:
        path = Plan.path(self.branch)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @staticmethod
    def load(branch: str) -> Plan | None:
        path = Plan.path(branch)
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["merged"] = [Member(**m) for m in raw["merged"]]
        raw["dropped"] = [Dropped(Member(**d.pop("member")), **d) for d in raw["dropped"]]
        return Plan(**raw)


# The ruleset's required checks (docs/specs/2026-10-19-merge-queue.md, read
# from ruleset 19138403): `merge` waits until each has reported, so a head
# where only the fast `scope` or `guards` finished never passes.
REQUIRED_CHECKS = ("test", "Parity / ubuntu-latest", "Parity / windows-latest")
# The only conclusions `merge` counts as passing; anything else is red or
# still running.
PASSING = {"SUCCESS", "SKIPPED", "NEUTRAL"}
# Members confirm on GitHub's own schedule after the merge: ask for up to
# MEMBER_WAIT_SECONDS before accepting a head that is on master unmarked.
MEMBER_WAIT_SECONDS = 120
MEMBER_POLL_SECONDS = 10
_sleep = time.sleep
_clock = time.monotonic

FAILED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
          "STARTUP_FAILURE", "ERROR"}


def _check_state(check: dict) -> str:
    """A CheckRun's conclusion or a StatusContext's state, upper-cased."""
    return (check.get("conclusion") or check.get("state") or "").upper()


def _checks(rollup: list[dict] | None) -> str:
    """One word for a head's CI: green, pending, failing or none."""
    if not rollup:
        return "none"
    pending = False
    for check in rollup:
        state = _check_state(check)
        if state in FAILED:
            return "failing"
        if _unfinished(check):
            pending = True
    return "pending" if pending else "green"


def _unfinished(check: dict) -> bool:
    return (check.get("status") not in (None, "COMPLETED")
            or _check_state(check) in ("", "PENDING", "EXPECTED"))


def _check_name(check: dict) -> str:
    return check.get("name") or check.get("context") or "an unnamed check"


def _labeled_at(number: int, label: str) -> str | None:
    """When the label was last applied (ISO 8601 sorts as text), if known."""
    jq = (f'.[] | select(.event == "labeled" and .label.name == "{label}")'
          " | .created_at")
    try:
        out = gh("api", "--paginate", f"repos/{{owner}}/{{repo}}/issues/{number}/events",
                 "--jq", jq)
    except Refusal:
        return None
    times = [line.strip() for line in out.splitlines() if line.strip()]
    return max(times) if times else None


def ready(label: str) -> list[Member]:
    """Open labelled pull requests on master, in label-applied order."""
    fields = "number,title,headRefOid,url,isDraft,reviewDecision,statusCheckRollup"
    rows = json.loads(gh("pr", "list", "--state", "open", "--label", label,
                         "--base", BASE, "--limit", "100", "--json", fields) or "[]")
    members = [Member(number=r["number"], title=r["title"], sha=r["headRefOid"],
                      url=r["url"], review=r.get("reviewDecision") or "NONE",
                      checks=_checks(r.get("statusCheckRollup")),
                      draft=bool(r.get("isDraft")))
               for r in rows]
    for m in members:
        m.labeled_at = _labeled_at(m.number, label)
    # Known label times first, oldest first; the rest by number.
    return sorted(members, key=lambda m: (m.labeled_at is None, m.labeled_at or "",
                                          m.number))


def _fetch_head(number: int) -> str:
    git("fetch", "-q", REMOTE, f"pull/{number}/head")
    return _out("rev-parse", "FETCH_HEAD")


def _merge_tree(ours: str, theirs: str) -> tuple[bool, list[str]]:
    """Would `theirs` merge into `ours` cleanly? Touches no working tree."""
    proc = git("merge-tree", "--write-tree", "--name-only", "--no-messages",
               ours, theirs, check=False)
    if proc.returncode not in (0, 1):
        raise Refusal("`git merge-tree --write-tree` failed (needs git 2.38+): "
                      + proc.stderr.strip())
    lines = proc.stdout.splitlines()[1:]
    return proc.returncode == 0, [line for line in lines if line]


def _changed(sha: str, base: str) -> set[str]:
    return set(_out("diff", "--name-only", f"{base}...{sha}").splitlines())


def _open_batches() -> list[tuple[int, str, str, str]]:
    """(number, branch, url, head sha) of every open pull request from an
    `integrate/` branch of this repository, however named. A fork's branch
    can carry any name, so fork pull requests are not batches."""
    rows = json.loads(gh("pr", "list", "--state", "open", "--limit", "500", "--json",
                         "number,headRefName,url,headRefOid,isCrossRepository")
                      or "[]")
    return [(r["number"], r["headRefName"], r.get("url", ""), r.get("headRefOid", ""))
            for r in rows
            if r.get("headRefName", "").startswith(PREFIX)
            and not r.get("isCrossRepository", False)]


def _used_numbers(date: str) -> set[int]:
    """Batch numbers taken on `date`: by earlier batch pull requests (whose
    branches GitHub deletes on merge), remote branches and local branches."""
    rows = json.loads(gh("pr", "list", "--state", "all", "--limit", "300",
                         "--search", "integrate in:title",
                         "--json", "number,headRefName") or "[]")
    names = [r.get("headRefName", "") for r in rows]
    remote = _out("ls-remote", "--heads", REMOTE, f"{PREFIX}*")
    names += [line.split("refs/heads/", 1)[1]
              for line in remote.splitlines() if "refs/heads/" in line]
    names += _out("for-each-ref", "--format=%(refname:short)",
                  f"refs/heads/{PREFIX}").splitlines()
    used = set()
    for name in names:
        match = BATCH_RE.match(name)
        if match and match.group(1) == date:
            used.add(int(match.group(2)))
    return used


def _preflight() -> tuple[str, str]:
    """Refuse unless the checkout is clean and at origin/master.

    Returns (what to switch back to, origin/master's sha)."""
    if _out("status", "--porcelain", "--untracked-files=no"):
        raise Refusal("the checkout has uncommitted changes to tracked files; "
                      "commit or discard them, or run from a fresh worktree")
    git("fetch", "-q", REMOTE, BASE)
    base_sha = _out("rev-parse", f"{REMOTE}/{BASE}")
    if _out("rev-parse", "HEAD") != base_sha:
        raise Refusal(f"HEAD is not {REMOTE}/{BASE} ({base_sha[:12]}); run "
                      f"`git switch --detach {REMOTE}/{BASE}` (or use a fresh "
                      "worktree) and try again")
    branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    return branch or base_sha, base_sha


def _collision(member: Member, conflicted: list[str], merged: list[Member],
               base_sha: str) -> str:
    clean, files = _merge_tree(base_sha, member.sha)
    if not clean:
        return (f"conflicts with {BASE} itself in {', '.join(files)}; merge "
                f"{BASE} forward, rerun its targeted tests, then re-apply the label")
    culprits = [f"#{m.number}" for m in merged
                if _changed(m.sha, base_sha) & set(conflicted)]
    who = ", ".join(culprits) or ("the batch so far ("
                                  + ", ".join(f"#{m.number}" for m in merged) + ")")
    return (f"conflicts with {who} in {', '.join(conflicted)}; once that "
            f"merges, merge {BASE} forward, rerun its targeted tests, then re-apply the label")


def _not_ready(m: Member) -> str | None:
    """Why a labelled pull request sits this batch out, keeping its label.

    Its own CI need not be green: the batch's CI is its CI, so pending or
    absent checks admit it. A failing head is a known failure and waits."""
    if m.draft:
        return "is a draft"
    if m.checks == "failing":
        return ("CI on its head is failing; it joins a batch once a rerun of "
                "the failed job passes or a push replaces that head")
    return None


def _project_version(rev: str) -> str | None:
    """`project.version` in pyproject.toml at `rev`, read as text (no tomllib
    on Python 3.10): the first `version = "..."` line of the [project] table."""
    shown = git("show", f"{rev}:pyproject.toml", check=False)
    if shown.returncode != 0:
        return None
    in_project = False
    for line in shown.stdout.splitlines():
        if line.startswith("["):
            in_project = line.strip() == "[project]"
        elif in_project and (match := VERSION_LINE_RE.match(line)):
            return match.group(1)
    return None


def _release_cut_alone(plan: Plan, members: list[Member]) -> list[Member]:
    """The members to merge: all of them, or only the first ready release cut
    (a member whose own diff changes project.version), with the others
    left out keeping their labels."""
    for m in members:
        if _not_ready(m) or _fetch_head(m.number) != m.sha:
            continue
        base = _out("merge-base", plan.base_sha, m.sha)
        if _project_version(m.sha) == _project_version(base):
            continue
        for other in members:
            if other is not m:
                plan.dropped.append(Dropped(
                    other, f"#{m.number} is a release cut, which is batched alone: "
                           "parity tests a cut against its own head only when nothing "
                           "else changes (release_cut() in "
                           "evals/rust_port/oracle_selection.py); this joins the next "
                           "batch", comment=False))
        print(f"  #{m.number} is a release cut; it is batched alone")
        return [m]
    return members


def _merge_members(plan: Plan, members: list[Member]) -> None:
    for m in members:
        reason = _not_ready(m)
        if reason:
            plan.dropped.append(Dropped(m, reason, comment=False))
            continue
        head = _fetch_head(m.number)
        if head != m.sha:
            plan.dropped.append(Dropped(
                m, f"head moved from {m.sha[:12]} to {head[:12]} after listing; "
                   "rebuild to include it", comment=False))
            continue
        # rerere off: a replayed resolution would be a conflict resolved
        # inside the batch, and would hide the conflict from --diff-filter=U.
        proc = git("-c", "rerere.enabled=false", "merge", "--no-ff", "--no-edit", "-m",
                   f"Merge #{m.number} into {plan.branch}: {m.title}", m.sha,
                   check=False)
        if proc.returncode == 0:
            plan.merged.append(m)
            print(f"  merged #{m.number} {m.sha[:12]} {m.title}")
            continue
        conflicted = _out("diff", "--name-only", "--diff-filter=U").splitlines()
        if git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            git("merge", "--abort")
        if not conflicted:
            raise Refusal(f"merging #{m.number} failed without a conflict: "
                          + (proc.stderr.strip() or proc.stdout.strip()))
        reason = _collision(m, conflicted, plan.merged, plan.base_sha)
        plan.dropped.append(Dropped(m, reason))
        print(f"  dropped #{m.number}: {reason}")


# The census script is member code, so it runs without the operator's
# credentials: only what Python and git need to start, on Windows or POSIX.
CENSUS_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
              "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL",
              "PYTHONUTF8", "PYTHONIOENCODING")


def _census_inputs(base_sha: str) -> set[str] | None:
    """The census module's own CURRENCY_INPUTS, read from master's copy of
    it, so no member code runs in this process (a member changing the script
    is still caught: the script is one of them). None when master has none."""
    shown = git("show", f"{base_sha}:{CENSUS}", check=False)
    if shown.returncode != 0:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "producer_census.py"
        path.write_text(shown.stdout, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("_batch_producer_census", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return set(module.CURRENCY_INPUTS)


def _census_ci_fails(root: Path, base_sha: str) -> bool:
    """Would CI's producer census check fail on HEAD as a pull request on
    `base_sha`? Runs the step CI runs, `rust/producer_census.py --ci-check`,
    with a pull request event naming that base, so the census module itself
    decides: drift is fatal only when the diff from the base touches its
    CURRENCY_INPUTS, and otherwise only reported."""
    with tempfile.TemporaryDirectory() as tmp:
        event = Path(tmp) / "event.json"
        event.write_text(json.dumps({"pull_request": {"base": {"sha": base_sha}}}),
                         encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k.upper() in CENSUS_ENV}
        proc = _run([sys.executable, CENSUS, "--ci-check"], check=False, cwd=root,
                    env={**env, "GITHUB_EVENT_PATH": str(event)})
    if proc.returncode == 1 and "producer census is stale" in proc.stderr:
        return True
    if proc.returncode != 0:
        raise Refusal(f"`python {CENSUS} --ci-check` failed ({proc.returncode}) on "
                      f"the batch head: {proc.stderr.strip() or proc.stdout.strip()}\n"
                      f"Run `python {CENSUS} --ci-check` on {REMOTE}/{BASE} first: "
                      "if it fails there too the cause is master or this machine. "
                      "Otherwise run it on each member's head; the one that breaks "
                      "it would fail CI the same way, so tell its author and leave "
                      "it out of the next build")
    if "report only" in proc.stdout:
        print("  producer census is stale on the batch head (report only, as in "
              "CI: no member changes its inputs)")
    return False


def _census_culprits(plan: Plan) -> list[tuple[Member, list[str]]]:
    """The merged members to drop so CI's census check passes on HEAD: none
    when it passes already (or master or HEAD has no census), otherwise
    every member whose own diff changes the census inputs."""
    root = Path(_out("rev-parse", "--show-toplevel"))
    inputs = _census_inputs(plan.base_sha)
    if (inputs is None or not (root / CENSUS).is_file()
            or not _census_ci_fails(root, plan.base_sha)):
        return []
    culprits = [(m, sorted(_changed(m.sha, plan.base_sha) & inputs)) for m in plan.merged]
    culprits = [(m, files) for m, files in culprits if files]
    if not culprits:
        raise Refusal(f"CI's producer census check would fail on the batch head, but "
                      f"no member's own diff changes {', '.join(sorted(inputs))}; "
                      f"check `python {CENSUS} --ci-check` by hand")
    return culprits


def _census_reason(member: Member, files: list[str], culprits: list[Member]) -> str:
    others = [f"#{m.number}" for m in culprits if m is not member]
    also = (f"; {' and '.join(others)} also change{'s' if len(others) == 1 else ''} "
            "them and left this batch too" if others else "")
    return (f"changes the producer census inputs ({', '.join(files)}) while the "
            "committed census is stale on the batch head, so CI's census check "
            f"(`python {CENSUS} --ci-check`) would fail{also}; once {BASE} holds "
            f"the rest of the batch, merge {BASE} forward, regenerate the census "
            f"with `python {CENSUS}`, rerun its targeted tests, then re-apply the label")


def _merge_batch(plan: Plan, members: list[Member]) -> None:
    """Merge the members, then drop any whose census CI would fail on and
    merge the rest again from the base; nothing is fixed inside a batch."""
    members = _release_cut_alone(plan, members)
    cut_drops = list(plan.dropped)
    census_drops: list[Dropped] = []
    _merge_members(plan, members)
    while culprits := _census_culprits(plan):
        left = [m for m, _ in culprits]
        for m, files in culprits:
            census_drops.append(Dropped(m, _census_reason(m, files, left)))
            print(f"  dropped #{m.number}: {census_drops[-1].reason}")
        out = {d.member.number for d in census_drops}
        print("  merging again without " + ", ".join(f"#{n}" for n in sorted(out)))
        git("reset", "-q", "--hard", plan.base_sha)
        plan.merged, plan.dropped = [], cut_drops + census_drops
        _merge_members(plan, [m for m in members if m.number not in out])


def _new_markers(base_sha: str) -> list[str]:
    def files(rev: str) -> set[str]:
        proc = git("grep", "-l", "-E", MARKER_RE, rev, check=False)
        return {line.split(":", 1)[1] for line in proc.stdout.splitlines() if ":" in line}
    return sorted(files("HEAD") - files(base_sha))


def _cell(text: str) -> str:
    """A pull request title made safe for one Markdown table cell."""
    return " ".join(text.split()).replace("|", "\\|").replace("`", "'")


def body(plan: Plan, suite: str | None = None, harness: str | None = None) -> str:
    lines = [
        f"Integration batch of {len(plan.merged)} reviewed pull requests, built by "
        f"`python ops/integrate.py build` from `{BASE}` at `{plan.base_sha}`. "
        f"Merge it with a merge commit, never squash, so every member becomes "
        f"reachable from `{BASE}` and GitHub marks it merged.",
        "",
        "## Members",
        "",
        "| PR | Head merged | Review evidence | CI on its head |",
        "| --- | --- | --- | --- |",
    ]
    for m in plan.merged:
        lines.append(f"| #{m.number} {_cell(m.title)} | `{m.sha}` | {m.url} "
                     f"({m.review.lower().replace('_', ' ')}) | {m.checks} |")
    if plan.dropped:
        lines += ["", "## Dropped", ""]
        lines += [f"- #{d.member.number}: {d.reason}" for d in plan.dropped]
    if plan.markers:
        lines += ["", "## Warning: conflict markers", "",
                  "These files carry conflict markers that master does not; "
                  "check them before merging: "
                  + ", ".join(f"`{f}`" for f in plan.markers)]
    lines += [
        "", "## Validation", "",
        f"- Local full suite: {suite or '<filled in by `open`>'}",
        f"- Rust harness Python tests (`{RUST_HARNESS_COMMAND}`, which the full "
        f"suite does not collect): {harness or '<filled in by `open`>'}",
        "- CI: this pull request's run is the combined check of its members, "
        "and their first full one: a pull request from any branch but "
        "`integrate/*` runs only the fast lanes.",
        "- A red batch is bisected locally (master plus half the members); the "
        f"culprit is dropped with a comment and loses `{plan.label}`, and nothing "
        "is fixed inside the batch.",
    ]
    return "\n".join(lines) + "\n"


def _switch_back(restore: str, base_sha: str) -> None:
    git("switch", "-q", *([restore] if restore != base_sha else ["--detach", base_sha]))


def build(args: argparse.Namespace) -> int:
    restore, base_sha = _preflight()
    open_prs = _open_batches()
    if open_prs:
        raise Refusal("a batch is already open: " + ", ".join(
            f"#{n} ({name})" for n, name, _, _ in open_prs)
            + "; merge or close it before building the next one")
    members = ready(args.label)
    waiting = [(m, _not_ready(m)) for m in members if _not_ready(m)]
    if len(members) - len(waiting) < MIN_MEMBERS:
        print(f"{len(members) - len(waiting)} of {len(members)} pull request(s) "
              f"carrying `{args.label}` are ready (not a draft, CI on the head not "
              f"failing); a batch needs {MIN_MEMBERS}.")
        for m, reason in waiting:
            print(f"  #{m.number} {reason}")
        return 0
    n = max(_used_numbers(args.date), default=0) + 1
    plan = Plan(branch=f"{PREFIX}{args.date}-batch-{n}", base_sha=base_sha,
                label=args.label)
    print(f"{'[dry run] ' if args.dry_run else ''}building {plan.branch} "
          f"from {REMOTE}/{BASE} {base_sha[:12]}")
    if args.dry_run:
        git("switch", "-q", "--detach", base_sha)
    else:
        git("switch", "-q", "-c", plan.branch, base_sha)
    done = False
    try:
        _merge_batch(plan, members)
        plan.markers = _new_markers(base_sha)
        plan.head = _out("rev-parse", "HEAD")
        done = True
    finally:
        if git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            git("merge", "--abort", check=False)
        # Stay on the branch only when `open` will publish it; otherwise it
        # was created by this run, never pushed, and goes.
        if not (done and not args.dry_run and len(plan.merged) >= MIN_MEMBERS):
            _switch_back(restore, base_sha)
            if not args.dry_run:
                git("branch", "-q", "-D", plan.branch)

    for d in plan.dropped:
        print(f"  left out #{d.member.number}: {d.reason}")
    if len(plan.merged) < MIN_MEMBERS:
        merged = ", ".join(f"#{m.number}" for m in plan.merged) or "none"
        print(f"{merged} merged cleanly; no batch built.")
        # No `open` follows, so the collisions are reported now.
        _report_drops(plan, None, args.dry_run)
        return 0
    if args.dry_run:
        print(f"\nwould build {plan.branch}, then after the suite open:\n"
              f"  {plan.title}\n\n{body(plan)}")
        _report_drops(plan, None, True)
        return 0
    plan.save()
    print(f"\nbuilt {plan.branch} at {plan.head[:12]}; nothing is pushed, so no CI "
          "has started. Next, on this checkout:\n"
          "  1. PSEUDOLIFE_SUITE_WHERE=local pwsh ops/remote-suite.ps1   "
          "(the local WSL full suite; writes ~/.pseudolife-mcp/suite-results/*.json)\n"
          f"  2. {RUST_HARNESS_COMMAND}   (the Rust harness Python tests the "
          "suite does not collect; natively is fine, with the project's dev "
          "dependencies)\n"
          "  3. python ops/integrate.py open --suite <that .json> --harness-note "
          f"\"<count> passed at {plan.head[:8]}\"\n"
          "A red suite: bisect locally, drop the culprit, switch back with "
          f"`git switch --detach {REMOTE}/{BASE}`, remove the local branch and build "
          "again.")
    return 0


def _suite_line(args: argparse.Namespace, head: str) -> str:
    """The "Local full suite" line, from a passing result for exactly `head`."""
    if args.suite_note:
        if head[:8] not in args.suite_note:
            raise Refusal(f"a --suite-note must name the batch head it ran on "
                          f"(at least `{head[:8]}`) with its machine and result")
        return f"{args.suite_note} (stated by the operator, not checked)"
    if not args.suite:
        raise Refusal("give the local full suite's result for this head: "
                      "`--suite <~/.pseudolife-mcp/suite-results/*.json>` from "
                      "`ops/remote-suite.ps1`, or `--suite-note \"<machine role, "
                      "commit, result>\"` for a run that wrote no JSON")
    try:
        # utf-8-sig: Windows PowerShell 5 writes the result with a BOM.
        result = json.loads(Path(args.suite).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise Refusal(f"cannot read the suite result {args.suite}: {exc}") from None
    if result.get("commit") != head:
        raise Refusal(f"the suite result tested {str(result.get('commit'))[:12]}, "
                      f"not this batch head {head[:12]}; run the suite on this "
                      "checkout")
    if "args" not in result:
        raise Refusal("the suite result does not record its pytest arguments "
                      "(written by an older ops/remote-suite.ps1), so it cannot "
                      "show a full run; rerun it, or give `--suite-note`")
    if result["args"]:
        raise Refusal(f"the suite result is a selected run ({result['args']}), not "
                      "the full suite; run `ops/remote-suite.ps1` with no pytest "
                      "arguments")
    if result.get("exit_code") != 0:
        raise Refusal(f"the suite failed on this head (exit {result.get('exit_code')}: "
                      f"{result.get('summary')}); bisect locally and drop the culprit")
    return (f"passed at `{head}` on {result.get('machine', 'an unnamed machine')}: "
            f"{result.get('summary', '')}")


def _harness_line(args: argparse.Namespace, head: str) -> str:
    """The "Rust harness Python tests" line: the operator's statement of the
    run `build` printed, naming the batch head it ran on."""
    if not args.harness_note:
        raise Refusal("give the Rust harness Python tests' result for this head: "
                      f"run `{RUST_HARNESS_COMMAND}` on this checkout, then "
                      f"`--harness-note \"<count> passed at {head[:8]}\"`; the local "
                      "full suite does not collect them")
    if head[:8] not in args.harness_note:
        raise Refusal(f"a --harness-note must name the batch head it ran on "
                      f"(at least `{head[:8]}`) with its result")
    note = args.harness_note.lower()
    passed = re.search(r"\b(\d+) passed\b", note)
    # Only pytest's own non-zero counts ("3 failed", "1 error") mark a red
    # run; the words alone also appear in prose and log paths.
    if (not passed or int(passed.group(1)) == 0
            or re.search(r"\b0*[1-9]\d*\s+(failed|errors?)\b", note)):
        raise Refusal("a --harness-note must state a clean pass, pytest's own "
                      f"`<count> passed` with no failed or error count, at {head[:8]}; "
                      "a red run is bisected and its culprit dropped")
    return f"{args.harness_note} (stated by the operator, not checked)"


def _refuse_red_members(plan: Plan) -> None:
    """Refuse when a merged member's own CI turned red after `build`: a
    known failure the batch would carry. A member whose head moved since is
    left to its next batch; the batch holds the head that was listed."""
    for m in plan.merged:
        view = json.loads(gh("pr", "view", str(m.number), "--json",
                             "headRefOid,statusCheckRollup"))
        if view["headRefOid"] == m.sha and _checks(view.get("statusCheckRollup")) == "failing":
            raise Refusal(f"#{m.number}'s own CI is now failing on {m.sha[:12]}, a known "
                          "failure this batch would carry. Drop it: switch back with "
                          f"`git switch --detach {REMOTE}/{BASE}`, remove the local "
                          "branch and build again (it sits out while its head is red)")


def open_(args: argparse.Namespace) -> int:
    if _out("status", "--porcelain", "--untracked-files=no"):
        raise Refusal("the checkout has uncommitted changes to tracked files; "
                      "nothing is fixed inside a batch")
    branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    plan = Plan.load(branch) if BATCH_RE.match(branch) else None
    if plan is None:
        raise Refusal(f"this checkout is not on a batch `build` left here "
                      f"({branch or 'detached HEAD'}); run `python ops/integrate.py "
                      "build` first")
    head = _out("rev-parse", "HEAD")
    if head != plan.head:
        raise Refusal(f"{branch} moved from {plan.head[:12]} to {head[:12]} after "
                      "build; nothing is fixed inside a batch, so rebuild it")
    open_prs = _open_batches()
    others = [(n, name) for n, name, _, _ in open_prs if name != plan.branch]
    if others:
        raise Refusal("a batch is already open: " + ", ".join(
            f"#{n} ({name})" for n, name in others))
    text = body(plan, _suite_line(args, head), _harness_line(args, head))
    # A rerun after a failure past the push resumes: this branch's own pull
    # request is the one to finish, not a second batch, but only while it
    # still points at the head the suite passed on. Once it is open, a moved
    # master is its CI's concern, so the base check applies only before then.
    own = [(n, u, sha) for n, name, u, sha in open_prs if name == plan.branch]
    url = None
    if own:
        number, url, sha = own[0]
        if sha != plan.head:
            raise Refusal(f"#{number} is open from {plan.branch} at {sha[:12]}, not "
                          f"the built head {plan.head[:12]}; check it by hand")
    else:
        git("fetch", "-q", REMOTE, BASE)
        if _out("rev-parse", f"{REMOTE}/{BASE}") != plan.base_sha:
            raise Refusal(f"{REMOTE}/{BASE} moved since {branch} was built; switch "
                          f"back with `git switch --detach {REMOTE}/{BASE}`, remove "
                          "the local branch and build again")
        # `build` drops these already; this catches a plan built without it.
        culprits = _census_culprits(plan)
        if culprits:
            who = ", ".join(f"#{m.number} ({', '.join(files)})" for m, files in culprits)
            raise Refusal(f"CI's producer census check would fail on {plan.head[:12]}: "
                          f"the census is stale and its inputs are changed by {who}. "
                          f"Nothing is fixed inside a batch: switch back with `git "
                          f"switch --detach {REMOTE}/{BASE}`, remove the local branch "
                          "and build again, which drops them with a comment")
        _refuse_red_members(plan)
    if url is None:
        try:
            git("push", "-q", "-u", REMOTE, plan.branch)
        except Refusal as exc:
            raise Refusal(f"{exc}\nNothing was pushed or commented on; fix the push "
                          "and rerun `open`.") from None
        try:
            url = gh("pr", "create", "--base", BASE, "--head", plan.branch,
                     "--title", plan.title, "--body-file", "-", input=text).strip()
        except Refusal as exc:
            raise Refusal(
                f"{exc}\n{plan.branch} is pushed, and no dropped member was "
                "commented on; rerun `open` with the same arguments once gh "
                "works (it finds the pull request if this one was opened).") from None
        print(f"opened {url}: {plan.title}")
    else:
        print(f"resuming {url}: {plan.title}")
    # `merge` reads the plan to know which pull request is the batch.
    plan.pr = int(url.rstrip("/").rsplit("/", 1)[-1])
    plan.save()
    try:
        _report_drops(plan, url, False, persist=True)
    except Refusal as exc:
        raise Refusal(f"{exc}\nThe batch pull request is open at {url}; rerun "
                      "`open` with the same arguments to finish reporting the "
                      "dropped members.") from None
    print(f"Once its CI is green and it has its review: python ops/integrate.py "
          f"merge   (on this checkout; it merges #{plan.pr} and nothing else)")
    return 0


def _report_drops(plan: Plan, url: str | None, dry_run: bool, *,
                  persist: bool = False) -> None:
    """Comment on each conflicting member and remove its label. With
    `persist`, each step done is saved to the plan, so a rerun of `open`
    after a failure here repeats nothing."""
    for d in plan.dropped:
        if not d.comment or (d.commented and d.unlabelled):
            continue
        if url or dry_run:
            where = f"Left out of integration batch `{plan.branch}`" + (
                f" ({url})" if url else "")
        else:
            where = "Could not join an integration batch"
        text = (f"{where}: merging this head (`{d.member.sha}`) "
                f"{d.reason}. `ops/integrate.py` removed the `{plan.label}` label; "
                "nothing was changed on this branch.")
        if dry_run:
            print(f"would comment on #{d.member.number} and remove `{plan.label}`:"
                  f"\n  {text}")
            continue
        # An author may push a fix and relabel while the batch suite runs;
        # the drop is about the head that collided, so a moved head keeps
        # its label and gets no comment.
        current = json.loads(gh("pr", "view", str(d.member.number), "--json",
                                "headRefOid"))["headRefOid"]
        if current != d.member.sha:
            print(f"#{d.member.number} moved to {current[:12]} since the build; "
                  f"left its `{plan.label}` label and posted nothing")
            d.commented = d.unlabelled = True
            if persist:
                plan.save()
            continue
        if not d.commented:
            gh("pr", "comment", str(d.member.number), "--body-file", "-", input=text)
            d.commented = True
            if persist:
                plan.save()
        gh("pr", "edit", str(d.member.number), "--remove-label", plan.label)
        d.unlabelled = True
        if persist:
            plan.save()


def _merge_plan() -> Plan:
    """The plan of the batch `open` published from this checkout."""
    branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    plan = Plan.load(branch) if branch else None
    if plan is None:
        raise Refusal(f"this checkout holds no batch plan ({branch or 'detached HEAD'}); "
                      "run `merge` on the checkout where `build` and `open` ran, which "
                      "stays on the batch branch. A batch opened before `open` kept its "
                      "plan has none: the maintainer merges it by hand after the checks "
                      "in docs/dev/merge-batches.md (\"Merging it\"), or close it and "
                      "build again. A pull request from any other branch is never "
                      f"merged directly: label it `{LABEL}` and let a batch carry it")
    if not branch.startswith(PREFIX) or not plan.branch.startswith(PREFIX):
        raise Refusal(f"{branch} is not an `{PREFIX}` batch branch; only a batch pull "
                      "request merges. A pull request from any other branch is never "
                      f"merged directly: label it `{plan.label}` and let a batch carry it")
    if plan.branch != branch:
        raise Refusal(f"the plan found for {branch} is for {plan.branch}; nothing was "
                      "merged. Run `merge` on the checkout where that batch was built "
                      "and opened")
    if not plan.pr:
        raise Refusal(f"{plan.branch} was built but not published; run `python "
                      "ops/integrate.py open` first, then wait for its CI")
    return plan


def _latest_checks(rollup: list[dict] | None) -> list[dict]:
    """The latest result of each check, so a rerun that superseded a failure
    counts: a CheckRun is keyed by workflow and name, a StatusContext by its
    context, and the later start (then completion) wins, list order on a tie."""
    latest: dict[tuple, dict] = {}
    for check in rollup or []:
        if check.get("__typename") == "StatusContext" or "context" in check:
            key = ("status", check.get("context", ""))
        else:
            key = ("check", check.get("workflowName", ""), check.get("name", ""))
        when = (check.get("startedAt") or "", check.get("completedAt") or "")
        held = latest.get(key)
        if held is None or when >= (held.get("startedAt") or "",
                                    held.get("completedAt") or ""):
            latest[key] = check
    return list(latest.values())


def _merge_checks(rollup: list[dict] | None) -> tuple[list[str], list[str]]:
    """(red, waiting) for a batch head: every latest check must have finished
    as SUCCESS, SKIPPED or NEUTRAL, and each required lane must be among
    them. Any other conclusion (STALE and TIMED_OUT included) is red."""
    red, waiting, names = [], [], set()
    for check in _latest_checks(rollup):
        name = _check_name(check)
        names.add(name)
        if check.get("__typename") == "StatusContext" or "context" in check or (
                "status" not in check):
            state = (check.get("state") or "").upper()
            if state in ("", "PENDING", "EXPECTED"):
                waiting.append(name)
            elif state != "SUCCESS":
                red.append(f"{name} ({state})")
            continue
        conclusion = (check.get("conclusion") or "").upper()
        if (check.get("status") or "").upper() != "COMPLETED" or not conclusion:
            waiting.append(name)
        elif conclusion not in PASSING:
            red.append(f"{name} ({conclusion})")
    waiting += [f"{name} (not reported yet)" for name in REQUIRED_CHECKS
                if name not in names]
    return red, waiting


def _refuse_unmergeable(plan: Plan, view: dict) -> None:
    """Refuse unless the pull request is this plan's batch at the built head,
    with every check finished and none failed, and master not past it."""
    n = plan.pr
    head_ref = view.get("headRefName", "")
    if (head_ref != plan.branch or not head_ref.startswith(PREFIX)
            or view.get("isCrossRepository")):
        raise Refusal(f"#{n} comes from {head_ref or 'an unknown branch'}"
                      f"{' in a fork' if view.get('isCrossRepository') else ''}, not "
                      f"this batch's {plan.branch}; nothing was merged. Find the batch's "
                      f"pull request with `gh pr list --head {plan.branch}`, and rerun "
                      "`open` with the same arguments to record it")
    if view.get("baseRefName") != BASE:
        raise Refusal(f"#{n} targets {view.get('baseRefName')}, not {BASE}; a batch "
                      f"merges into {BASE} only. Close it and build again")
    if view.get("state") != "OPEN":
        raise Refusal(f"#{n} is {str(view.get('state')).lower()}, not open; nothing "
                      "to merge. Switch back with `git switch --detach "
                      f"{REMOTE}/{BASE}`, remove the local branch and build again")
    if view.get("headRefOid") != plan.head:
        raise Refusal(f"#{n}'s head is {str(view.get('headRefOid'))[:12]}, not the head "
                      f"`build` made and the suite passed on ({plan.head[:12]}); nothing "
                      f"is fixed inside a batch. Close #{n}, switch back with `git switch "
                      f"--detach {REMOTE}/{BASE}`, remove the local branch and build "
                      "again (a batch forwarded under the `merge-forward` skill is the "
                      "maintainer's to merge)")
    red, waiting = _merge_checks(view.get("statusCheckRollup"))
    if red:
        raise Refusal(f"CI failed on #{n} at {plan.head[:12]}: {', '.join(red)}. Nothing "
                      "is fixed inside a batch: rerun once only for the flake classes "
                      "CLAUDE.md names (issues #830 and #855); otherwise drop the member "
                      "the failing job covers, or bisect, then close the batch and build "
                      "again (docs/dev/merge-batches.md, \"Validating it\")")
    if waiting:
        raise Refusal(f"CI on #{n} has not finished ({', '.join(waiting)}); wait for "
                      "every check, the required lanes included, to complete, then rerun "
                      "`python ops/integrate.py merge`")
    git("fetch", "-q", REMOTE, BASE)
    master = _out("rev-parse", f"{REMOTE}/{BASE}")
    if (master != plan.base_sha and git("merge-base", "--is-ancestor", master,
                                        plan.head, check=False).returncode != 0):
        raise Refusal(f"{REMOTE}/{BASE} moved since {plan.branch} was built "
                      f"({plan.base_sha[:12]} -> {master[:12]}) and #{n}'s head does not "
                      f"contain it, so its CI did not test what {BASE} would get. Close "
                      f"#{n}, switch back with `git switch --detach {REMOTE}/{BASE}`, "
                      "remove the local branch and build the next batch; only the "
                      "`merge-forward` skill's exceptions let it merge instead, and that "
                      "merge is the maintainer's")


def _on_master(sha: str, number: int, commit: str) -> bool:
    """Is `sha` reachable from the merge commit or master? Fetches the
    member's head first when this checkout lacks it (a push after `build`)."""
    if git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
        git("fetch", "-q", REMOTE, f"pull/{number}/head", check=False)
    tips = [t for t in (commit, f"{REMOTE}/{BASE}") if t
            and git("cat-file", "-e", f"{t}^{{commit}}", check=False).returncode == 0]
    return any(git("merge-base", "--is-ancestor", sha, tip, check=False).returncode == 0
               for tip in tips)


def _confirm_members(plan: Plan, commit: str) -> list[tuple[Member, str, str]]:
    """Report each member against its current head, asking GitHub again for
    up to MEMBER_WAIT_SECONDS while one sits on master unmarked. Returns the
    members not confirmed: (member, state, head) whose head is the one the
    batch carried and yet is not on master."""
    deadline = _clock() + MEMBER_WAIT_SECONDS
    pending = list(plan.merged)
    unconfirmed = []
    while pending:
        git("fetch", "-q", REMOTE, BASE, check=False)
        last = _clock() >= deadline
        waiting = []
        for m in pending:
            view = json.loads(gh("pr", "view", str(m.number), "--json",
                                 "state,headRefOid"))
            state, head = view.get("state", ""), view.get("headRefOid", "")
            if state == "MERGED":
                print(f"  #{m.number} MERGED {m.title}")
            elif _on_master(head, m.number, commit):
                if state == "OPEN" and not last:
                    waiting.append(m)   # GitHub marks it merged shortly
                else:
                    print(f"  #{m.number} {state.lower()}: its head {head[:12]} is in "
                          f"{BASE}; GitHub has not marked it merged - {m.title}")
            elif head != m.sha:
                print(f"  #{m.number} {state.lower()}: has commits after the batch "
                      f"({head[:12]}, the batch carried {m.sha[:12]}); it keeps its "
                      "label and joins a later batch")
            elif last:
                unconfirmed.append((m, state, head))
            else:
                waiting.append(m)
        pending = waiting
        if pending:
            _sleep(MEMBER_POLL_SECONDS)
    return unconfirmed


def merge(args: argparse.Namespace) -> int:
    plan = _merge_plan()
    n = plan.pr
    view = json.loads(gh("pr", "view", str(n), "--json",
                         "headRefName,headRefOid,baseRefName,state,"
                         "isCrossRepository,statusCheckRollup"))
    if view.get("state") == "MERGED" and view.get("headRefName") == plan.branch:
        # A rerun after the merge went through: only the confirmation is left.
        print(f"#{n} is already merged; confirming its members")
    else:
        _refuse_unmergeable(plan, view)
        try:
            gh("pr", "merge", str(n), "--merge", "--admin",
               "--match-head-commit", plan.head)
        except Refusal as exc:
            raise Refusal(f"{exc}\nIf #{n} still reads open, nothing was merged: fix the "
                          "cause and rerun `merge`; if it merged, rerun `merge` to "
                          "confirm its members") from None
    after = json.loads(gh("pr", "view", str(n), "--json", "state,mergeCommit"))
    if after.get("state") != "MERGED":
        print(f"integrate: `gh pr merge` returned, but #{n} reads "
              f"{after.get('state')}; check it on GitHub, then rerun `merge`",
              file=sys.stderr)
        return 1
    commit = (after.get("mergeCommit") or {}).get("oid", "")
    print(f"merged #{n} into {BASE} as {commit or '<merge commit unknown>'}")
    unconfirmed = _confirm_members(plan, commit)
    for m, state, head in unconfirmed:
        print(f"integrate: #{m.number} reads {state.lower()} and its head {head[:12]}, "
              f"the one this batch carried, is not on {REMOTE}/{BASE} yet; check #{n}'s "
              f"merge commit on GitHub, then rerun `merge` to confirm again",
              file=sys.stderr)
    if unconfirmed:
        return 1
    Plan.path(plan.branch).unlink(missing_ok=True)
    print("Next:\n"
          f"  1. Watch CI on master at {commit[:12] or 'the merge commit'}: `gh run list "
          f"--branch {BASE} --commit {commit or '<merge commit>'}`. A red run gets a "
          "revert or fix pull request, landed as a batch of one.\n"
          f"  2. Free this checkout: `git switch --detach {REMOTE}/{BASE}` after a "
          f"`git fetch`, then `git branch -D {plan.branch}`.")
    return 0


def status(args: argparse.Namespace) -> int:
    git("fetch", "-q", REMOTE, BASE)
    base_sha = _out("rev-parse", f"{REMOTE}/{BASE}")
    members = ready(args.label)
    if not members:
        print(f"no open pull request carries `{args.label}`")
        return 0
    print(f"{len(members)} labelled `{args.label}`, against {REMOTE}/{BASE} "
          f"{base_sha[:12]}, in batch order:")
    for m in members:
        head = _fetch_head(m.number)
        moved = "" if head == m.sha else f" (fetched head {head[:12]} differs)"
        clean, files = _merge_tree(base_sha, head)
        merge = "merges cleanly" if clean else f"conflicts with master in {', '.join(files)}"
        draft = " draft," if m.draft else ""
        print(f"#{m.number} {m.sha[:12]}{moved}:{draft} {merge}; CI {m.checks}; "
              f"review {m.review}; labelled {m.labeled_at or 'at an unknown time'} "
              f"- {m.title}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (("status", "list labelled pull requests"),
                           ("build", "merge the next batch onto a local branch")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--label", default=LABEL)
    b = sub.choices["build"]
    b.add_argument("--dry-run", action="store_true",
                   help="merge on a detached HEAD, print the plan and switch back; "
                        "creates, pushes and comments on nothing")
    b.add_argument("--date", default=dt.date.today().isoformat(),
                   help="batch date (default: today)")
    o = sub.add_parser("open", help="push the built batch and open its pull request")
    evidence = o.add_mutually_exclusive_group()
    evidence.add_argument("--suite", help="the passing suite result JSON for this head")
    evidence.add_argument("--suite-note",
                          help="the suite result in words, for a run without a JSON")
    o.add_argument("--harness-note",
                   help="the Rust harness Python tests' result in words, naming "
                        "this head (the command `build` printed)")
    sub.add_parser("merge", help="merge the published batch pull request once every "
                                 "check on its built head passed; the one path to "
                                 "master")
    args = parser.parse_args(argv)
    commands = {"status": status, "build": build, "open": open_, "merge": merge}
    try:
        return commands[args.command](args)
    except Refusal as exc:
        print(f"integrate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
