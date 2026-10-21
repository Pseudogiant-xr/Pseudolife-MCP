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
    python ops/integrate.py open --suite <result.json>   # push it, open its PR

A pull request joins by carrying the `ready-to-batch` label. `build` runs
in a clean checkout whose HEAD is origin/master, merges each labelled head
with `git merge --no-ff` in the order the label was applied (pull request
number when unknown) onto a new local branch, and drops a member that
conflicts, aborting that merge. It pushes nothing, so the local full suite
runs on the batch head before any CI. `open` then checks the suite passed
on that exact head, pushes the branch, opens the batch pull request, and
comments on each dropped member naming what it collided with (removing its
label). A member whose own diff changes the producer census inputs is
dropped the same way when the batch head would fail CI's census check
(`rust/producer_census.py --ci-check`). Nothing resolves a conflict and
nothing force-pushes. Needs git 2.38+ (`git merge-tree --write-tree`) and an
authenticated `gh`.
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
from dataclasses import asdict, dataclass, field
from pathlib import Path

LABEL = "ready-to-batch"
BASE = "master"
REMOTE = "origin"
PREFIX = "integrate/"
# A batch of one is just its pull request; merge that directly instead.
MIN_MEMBERS = 2
BATCH_RE = re.compile(r"^integrate/(\d{4}-\d{2}-\d{2})-batch-(\d+)$")
MARKER_RE = r"^(<<<<<<<|>>>>>>>)( |$)"
CENSUS = "rust/producer_census.py"


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


def _checks(rollup: list[dict] | None) -> str:
    """One word for a head's CI: green, pending, failing or none."""
    if not rollup:
        return "none"
    failed = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
              "STARTUP_FAILURE", "ERROR"}
    pending = False
    for check in rollup:
        state = (check.get("conclusion") or check.get("state") or "").upper()
        if state in failed:
            return "failing"
        if (check.get("status") not in (None, "COMPLETED")
                or state in ("", "PENDING", "EXPECTED")):
            pending = True
    return "pending" if pending else "green"


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
        return ("CI on its head is failing; it joins a batch once a push "
                "replaces that head")
    return None


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
        plan.merged, plan.dropped = [], list(census_drops)
        _merge_members(plan, [m for m in members if m.number not in out])


def _new_markers(base_sha: str) -> list[str]:
    def files(rev: str) -> set[str]:
        proc = git("grep", "-l", "-E", MARKER_RE, rev, check=False)
        return {line.split(":", 1)[1] for line in proc.stdout.splitlines() if ":" in line}
    return sorted(files("HEAD") - files(base_sha))


def _cell(text: str) -> str:
    """A pull request title made safe for one Markdown table cell."""
    return " ".join(text.split()).replace("|", "\\|").replace("`", "'")


def body(plan: Plan, suite: str | None = None) -> str:
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
        "- CI: this pull request's run is the combined check of its members.",
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
              f"failing); a batch needs {MIN_MEMBERS}. Merge a lone ready pull "
              "request directly.")
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
        print(f"only {merged} merged cleanly; no batch built. Merge a lone "
              "ready pull request directly.")
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
          "  2. python ops/integrate.py open --suite <that .json>\n"
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
    text = body(plan, _suite_line(args, head))
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
    try:
        _report_drops(plan, url, False, persist=True)
    except Refusal as exc:
        raise Refusal(f"{exc}\nThe batch pull request is open at {url}; rerun "
                      "`open` with the same arguments to finish reporting the "
                      "dropped members.") from None
    Plan.path(plan.branch).unlink(missing_ok=True)
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
    args = parser.parse_args(argv)
    commands = {"status": status, "build": build, "open": open_}
    try:
        return commands[args.command](args)
    except Refusal as exc:
        print(f"integrate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
