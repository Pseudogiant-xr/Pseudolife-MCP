"""Build hand-rolled merge batches of reviewed pull requests.

Master takes one pull request at a time, and each one must pass CI on its
head merged with current master, so every merge sends the other open pull
requests back for a fresh CI run. A batch merges several reviewed pull
requests into one `integrate/<YYYY-MM-DD>-batch-N` branch that is validated
and merged once. The rule that governs batches is in CLAUDE.md ("Running
tests"); the walkthrough is `docs/dev/merge-batches.md`.

    python ops/integrate.py status            # labelled PRs, CI, mergeability
    python ops/integrate.py build --dry-run   # what a build would do
    python ops/integrate.py build             # push the batch, open its PR

A pull request joins by carrying the `ready-to-batch` label. `build` runs
in a clean checkout whose HEAD is origin/master, merges each labelled head
with `git merge --no-ff` in the order the label was applied (pull request
number when unknown), and drops a member that conflicts: it aborts that
merge, comments on the pull request naming what it collided with, and
removes its label. It never resolves a conflict and never force-pushes.
Needs git 2.38+ (`git merge-tree --write-tree`) and an authenticated `gh`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

LABEL = "ready-to-batch"
BASE = "master"
REMOTE = "origin"
PREFIX = "integrate/"
# A batch of one is just its pull request; merge that directly instead.
MIN_MEMBERS = 2
BATCH_RE = re.compile(r"^integrate/(\d{4}-\d{2}-\d{2})-batch-(\d+)$")
MARKER_RE = r"^(<<<<<<<|>>>>>>>)( |$)"


class Refusal(Exception):
    """A precondition failed; nothing was changed."""


def _run(argv: list[str], *, input: str | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", input=input)
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


@dataclass
class Plan:
    branch: str
    base_sha: str
    merged: list[Member] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        date, n = BATCH_RE.match(self.branch).groups()
        numbers = ", ".join(f"#{m.number}" for m in self.merged)
        return f"integrate: {date} batch {n} ({numbers})"


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


def _batches(date: str) -> tuple[list[tuple[int, str]], set[int]]:
    """(open batch PRs, batch numbers already used on `date`)."""
    rows = json.loads(gh("pr", "list", "--state", "all", "--limit", "300",
                         "--search", "integrate in:title",
                         "--json", "number,headRefName,state") or "[]")
    open_prs, used = [], set()
    names = [(r.get("headRefName", ""), r) for r in rows]
    remote = _out("ls-remote", "--heads", REMOTE, f"{PREFIX}*")
    names += [(line.split("refs/heads/", 1)[1], None)
              for line in remote.splitlines() if "refs/heads/" in line]
    local = _out("for-each-ref", "--format=%(refname:short)", f"refs/heads/{PREFIX}")
    names += [(name, None) for name in local.splitlines()]
    for name, row in names:
        match = BATCH_RE.match(name)
        if not match:
            continue
        if row is not None and row.get("state") == "OPEN":
            open_prs.append((row["number"], name))
        if match.group(1) == date:
            used.add(int(match.group(2)))
    return open_prs, used


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
                f"{BASE} forward, let CI pass, then re-apply the label")
    culprits = [f"#{m.number}" for m in merged
                if _changed(m.sha, base_sha) & set(conflicted)]
    who = ", ".join(culprits) or ("the batch so far ("
                                  + ", ".join(f"#{m.number}" for m in merged) + ")")
    return (f"conflicts with {who} in {', '.join(conflicted)}; once that "
            f"merges, merge {BASE} forward, let CI pass, then re-apply the label")


def _merge_members(plan: Plan, members: list[Member]) -> None:
    for m in members:
        if m.draft:
            plan.dropped.append(Dropped(m, "is a draft", comment=False))
            continue
        head = _fetch_head(m.number)
        if head != m.sha:
            plan.dropped.append(Dropped(
                m, f"head moved from {m.sha[:12]} to {head[:12]} after listing; "
                   "rebuild to include it", comment=False))
            continue
        proc = git("merge", "--no-ff", "--no-edit", "-m",
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


def _new_markers(base_sha: str) -> list[str]:
    def files(rev: str) -> set[str]:
        proc = git("grep", "-l", "-E", MARKER_RE, rev, check=False)
        return {line.split(":", 1)[1] for line in proc.stdout.splitlines() if ":" in line}
    return sorted(files("HEAD") - files(base_sha))


def body(plan: Plan, label: str) -> str:
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
        lines.append(f"| #{m.number} {m.title} | `{m.sha}` | {m.url} "
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
        "- Local full suite: <fill in: `pwsh ops/wsl-suite.ps1` on this head; "
        "machine, commit and result>",
        "- CI: this pull request's run is the combined check of its members.",
        "- A red batch is bisected locally (master plus half the members); the "
        f"culprit is dropped with a comment and loses `{label}`, and nothing is "
        "fixed inside the batch.",
    ]
    return "\n".join(lines) + "\n"


def build(args: argparse.Namespace) -> int:
    restore, base_sha = _preflight()
    open_prs, used = _batches(args.date)
    if open_prs:
        raise Refusal("a batch is already open: " + ", ".join(
            f"#{n} ({name})" for n, name in open_prs)
            + "; merge or close it before building the next one")
    members = ready(args.label)
    if len([m for m in members if not m.draft]) < MIN_MEMBERS:
        print(f"{len(members)} pull request(s) carry `{args.label}`; a batch needs "
              f"{MIN_MEMBERS}. Merge a lone ready pull request directly.")
        return 0
    n = max(used, default=0) + 1
    plan = Plan(branch=f"{PREFIX}{args.date}-batch-{n}", base_sha=base_sha)
    print(f"{'[dry run] ' if args.dry_run else ''}building {plan.branch} "
          f"from {REMOTE}/{BASE} {base_sha[:12]}")
    if args.dry_run:
        git("switch", "-q", "--detach", base_sha)
    else:
        git("switch", "-q", "-c", plan.branch, base_sha)
    done = False
    try:
        _merge_members(plan, members)
        plan.markers = _new_markers(base_sha)
        done = True
    finally:
        if git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            git("merge", "--abort", check=False)
        # Keep the checkout on the branch only when it will be pushed; it was
        # created by this run and never pushed otherwise, so it goes.
        if not (done and not args.dry_run and len(plan.merged) >= MIN_MEMBERS):
            git("switch", "-q", *([restore] if restore != base_sha
                                  else ["--detach", base_sha]))
            if not args.dry_run:
                git("branch", "-q", "-D", plan.branch)

    for d in plan.dropped:
        print(f"  left out #{d.member.number}: {d.reason}")
    text = body(plan, args.label)
    if len(plan.merged) < MIN_MEMBERS:
        merged = ", ".join(f"#{m.number}" for m in plan.merged) or "none"
        print(f"only {merged} merged cleanly; no batch opened. Merge a lone "
              "ready pull request directly.")
        url = None
    elif args.dry_run:
        print(f"\nwould push {plan.branch} and open:\n  {plan.title}\n\n{text}")
        url = None
    else:
        git("push", "-q", "-u", REMOTE, plan.branch)
        url = gh("pr", "create", "--base", BASE, "--head", plan.branch,
                 "--title", plan.title, "--body-file", "-", input=text).strip()
        print(f"opened {url}: {plan.title}\nthis checkout is on {plan.branch}; "
              "run the local full suite on it next (`pwsh ops/wsl-suite.ps1`)")
    _report_drops(plan, url, args)
    return 0


def _report_drops(plan: Plan, url: str | None, args: argparse.Namespace) -> None:
    for d in plan.dropped:
        if not d.comment:
            continue
        if url or args.dry_run:
            where = f"Left out of integration batch `{plan.branch}`" + (
                f" ({url})" if url else "")
        else:
            where = "Could not join an integration batch"
        text = (f"{where}: merging this head (`{d.member.sha}`) "
                f"{d.reason}. `ops/integrate.py` removed the `{args.label}` label; "
                "nothing was changed on this branch.")
        if args.dry_run:
            print(f"would comment on #{d.member.number} and remove `{args.label}`:"
                  f"\n  {text}")
            continue
        gh("pr", "comment", str(d.member.number), "--body-file", "-", input=text)
        gh("pr", "edit", str(d.member.number), "--remove-label", args.label)


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
                           ("build", "build and open the next batch")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--label", default=LABEL)
    b = sub.choices["build"]
    b.add_argument("--dry-run", action="store_true",
                   help="merge locally on a detached HEAD, then print the plan; "
                        "push, open and comment on nothing")
    b.add_argument("--date", default=dt.date.today().isoformat(),
                   help="batch date (default: today)")
    args = parser.parse_args(argv)
    try:
        return build(args) if args.command == "build" else status(args)
    except Refusal as exc:
        print(f"integrate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
