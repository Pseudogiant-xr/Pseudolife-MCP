"""Wake a long-running delegate session only when something needs a decision.

A delegate session (the one the maintainer talks to, which dispatches work,
orders suites and merges) pays a full-context request for every wake.
Measured 2026-10-14 over six such sessions (Sep 23 - Oct 13): 2,504 turns
began with a background-task notification and 557 with board mail, and about
a third of all their turns took no action. Cache reads were 96% of their
cost-weighted input, so cost was the number of requests times the context
size. This watcher does the waiting without a model. It polls GitHub, the
suite results directory and the suite leases, and exits when something needs
a decision, printing one short line per event.

Run it from the delegate as a background command and run it again after
each wake. In Claude Code that means the Bash tool with run_in_background
and a timeout above --timeout, since the tool stops a background command at
its own timeout (2 hours at most):

    python ops/delegate_watch.py [--repo OWNER/NAME] [--timeout 3300]

Exit codes:
    0  events, one line each on stdout, then a mail line;
    3  a quiet timeout, one status line. The wake also keeps the prompt
       cache warm, so it replaces a separate keep-warm timer;
    2  setup failure (gh missing, or failing on 5 polls in a row), named
       on stderr. Arguments argparse refuses also exit 2.

Each event is reported once; state persists in --state between runs:
- a pull request opened, merged or closed;
- a pull request's checks all finished green (with its merge state), or a
  check failed (on that head);
- a pull request newly conflicting with its base;
- the newest completed master run of a workflow (push or schedule) failing,
  or passing again after a failure (cancelled runs are neither);
- a new result in the suite results directory;
- a full-suite lease going from held to free.

Board mail does not wake it. The daemon's ring already wakes a Claude Code
session through the plugin's Stop hook, and plain mail never wakes anyone
(maintainer decision 2026-10-02). Instead, every exit reports the pending
count from this session's coordination digest.

The first run without a state file records a baseline and reports nothing
for what is already true. Run one watcher per state file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

EXIT_EVENTS = 0
EXIT_SETUP = 2
EXIT_QUIET = 3

# 55 minutes keeps a quiet wake inside the one-hour prompt-cache lifetime
# with room for the harness to start the turn.
DEFAULT_TIMEOUT = 3300.0
DEFAULT_INTERVAL = 60.0
# CI jobs and suite runs finish in clusters; one wake should carry them.
DEFAULT_SETTLE = 45.0
MAX_GH_FAILURES = 5
DEFAULT_LEASES = ("full-suite", "full-suite@box")
PR_FIELDS = "number,title,headRefOid,mergeable,mergeStateStatus,isDraft,statusCheckRollup"
RUN_FIELDS = "attempt,databaseId,workflowName,status,conclusion,headSha,event"
FAILED_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
MASTER_FAILED = {"failure", "timed_out", "startup_failure"}
TITLE = 70
# gh paginates up to this; a pull request missing from the list is looked up
# by number before it counts as closed, so a full page cannot fake one.
PR_LIMIT = 500


class GhError(Exception):
    """A gh call failed; the message is its stderr."""


def run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def checks_state(rollup) -> tuple[str, list[str]]:
    """``failing`` with the failed names, ``pending``, ``green``, or ``none``
    for a head with no checks. One failure is enough: waiting for the rest
    would delay the decision it forces."""
    failed, pending = [], False
    for check in rollup or []:
        if check.get("__typename") == "StatusContext":
            state = (check.get("state") or "").upper()
            if state in {"FAILURE", "ERROR"}:
                failed.append(check.get("context") or "status")
            elif state != "SUCCESS":
                pending = True
            continue
        if (check.get("status") or "").upper() != "COMPLETED":
            pending = True
        elif (check.get("conclusion") or "").upper() in FAILED_CONCLUSIONS:
            failed.append(check.get("name") or check.get("workflowName") or "check")
    if failed:
        return "failing", sorted(set(failed))
    if pending:
        return "pending", []
    return ("green" if rollup else "none"), []


class Watcher:
    def __init__(self, args, runner):
        self.args = args
        self.runner = runner
        self.repo = ["-R", args.repo] if args.repo else []
        self.warned_cli = False

    def gh(self, *argv):
        try:
            code, out, err = self.runner(["gh", *argv, *self.repo])
        except FileNotFoundError:
            raise SystemExit(self.setup("gh is not installed or not on PATH")) from None
        except subprocess.TimeoutExpired:
            raise GhError("gh timed out") from None
        except OSError as exc:
            raise GhError(f"gh could not run ({exc})") from None
        if code != 0:
            lines = (err or out).strip().splitlines()
            raise GhError(lines[-1] if lines else f"gh exited {code}")
        return json.loads(out or "null")

    @staticmethod
    def setup(message):
        print(f"delegate_watch: {message}", file=sys.stderr)
        return EXIT_SETUP

    # ── sources: each returns its snapshot, or raises GhError ──────────────

    def prs(self, old):
        args = ["pr", "list", "--state", "open", "--limit", str(PR_LIMIT), "--json", PR_FIELDS]
        if self.args.author:
            args += ["--author", self.args.author]
        snap = {}
        for p in self.gh(*args):
            checks, failed = checks_state(p.get("statusCheckRollup"))
            number = str(p["number"])
            mergeable = p.get("mergeable") or ""
            if mergeable == "UNKNOWN" and number in (old or {}):
                # GitHub recomputes mergeability lazily after the base moves;
                # keep the last known answer so CONFLICTING -> UNKNOWN ->
                # CONFLICTING is not news (review, 2026-10-14).
                mergeable = old[number]["mergeable"]
            snap[number] = {
                "title": (p.get("title") or "")[:TITLE], "head": p.get("headRefOid") or "",
                "checks": checks, "failed": failed, "mergeable": mergeable,
                "merge_state": (p.get("mergeStateStatus") or "unknown").lower()}
        return snap

    def master(self, old):
        """The newest completed, non-cancelled push or schedule run per
        workflow; a workflow whose newest such run is out of the page keeps
        its previous entry. An older run never replaces the one held, and a
        held run being re-run stays as it was until its new attempt finishes,
        so a re-run is neither a false recovery nor missed (review,
        2026-10-14)."""
        snap = dict(old or {})
        seen = set()
        for r in self.gh("run", "list", "--branch", "master", "--limit", "30", "--json", RUN_FIELDS):
            name = r.get("workflowName") or "workflow"
            if name in seen or r.get("event") not in {"push", "schedule"}:
                continue
            held = snap.get(name)
            run_id = r.get("databaseId") or 0
            if held and (run_id < held["id"] or (run_id == held["id"]
                                                 and r.get("status") != "completed")):
                seen.add(name)
                continue
            if r.get("status") != "completed":
                continue
            conclusion = r.get("conclusion") or ""
            if conclusion != "success" and conclusion not in MASTER_FAILED:
                continue
            seen.add(name)
            snap[name] = {"id": run_id, "attempt": r.get("attempt"), "conclusion": conclusion,
                          "sha": r.get("headSha") or "", "event": r.get("event")}
        return snap

    def suites(self, old):
        """Readable result files. One that does not parse yet (still being
        written) stays out, so a later poll reports it."""
        snap = dict(old or {})
        directory = Path(self.args.suite_results)
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            if path.name in snap:
                continue
            try:
                body = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            snap[path.name] = {"machine": str(body.get("machine", "?")),
                               "commit": str(body.get("commit", ""))[:8],
                               "exit": body.get("exit_code"),
                               "summary": str(body.get("summary", ""))[:120]}
        return snap

    def leases(self, old):
        """held or free per lease, keeping the previous value when the answer
        is unknown. Free counts only when the board answered: on the Windows
        host both suite leases live on the board (WSL and the box hold their
        own locks), and `lease check` reads a free local lock as free when
        the board is down (review, 2026-10-14)."""
        snap = dict(old or {})
        for name in self.args.lease:
            try:
                code, out, _ = self.runner([self.args.lease_cli, "lease", "check", "--json", name])
                body = json.loads(out)
            except FileNotFoundError:
                if not self.warned_cli:
                    print(f"delegate_watch: {self.args.lease_cli} not found; leases unknown",
                          file=sys.stderr)
                    self.warned_cli = True
                continue
            except (OSError, subprocess.TimeoutExpired, ValueError):
                continue
            board_up = (body.get("board") or {}).get("available") is True
            if code == 1 and body.get("held") is True:
                snap[name] = "held"
            elif code == 0 and body.get("held") is False and board_up:
                snap[name] = "free"
        return snap

    def closed_as(self, number):
        """``merged`` or ``closed``, or None while that is not known: the
        lookup failed, or the pull request is still open (it fell off the
        list). None keeps it tracked and asks again next poll (Codex review,
        2026-10-14)."""
        try:
            state = self.gh("pr", "view", number, "--json", "state").get("state", "")
        except (GhError, ValueError, AttributeError):
            return None
        return {"MERGED": "merged", "CLOSED": "closed"}.get(state)

    def snapshot(self, old):
        """Every source, keeping the old value of a source whose gh call
        failed. Returns the snapshot and the gh error, if any."""
        old = old or {}
        snap, error = {}, None
        for key, fetch in (("prs", lambda: self.prs(old.get("prs"))),
                           ("master", lambda: self.master(old.get("master")))):
            try:
                snap[key] = fetch()
            except (GhError, ValueError) as exc:
                error = exc
                snap[key] = old.get(key)
        snap["suites"] = self.suites(old.get("suites"))
        snap["leases"] = self.leases(old.get("leases"))
        return snap, error

    # ── events ────────────────────────────────────────────────────────────

    def events(self, old, new):
        out = []
        if old.get("prs") is not None and new.get("prs") is not None:
            out += self.pr_events(old["prs"], new["prs"])
        for name, run_ in (new.get("master") or {}).items():
            before = (old.get("master") or {}).get(name)
            if before and all(before.get(k) == run_.get(k) for k in ("id", "attempt", "conclusion")):
                continue
            if run_["conclusion"] in MASTER_FAILED and (not before or before["conclusion"] == "success"):
                out.append(f"master {name} failed at {run_['sha'][:8]} ({run_['event']})")
            elif run_["conclusion"] == "success" and before and before["conclusion"] in MASTER_FAILED:
                out.append(f"master {name} green again at {run_['sha'][:8]} ({run_['event']})")
        for name, s in (new.get("suites") or {}).items():
            if name not in (old.get("suites") or {}):
                out.append(f"suite {s['machine']} {s['commit']} exit {s['exit']}: {s['summary']}")
        for name, state in (new.get("leases") or {}).items():
            if state == "free" and (old.get("leases") or {}).get(name) == "held":
                out.append(f"{name} free")
        return out

    def pr_events(self, old, new):
        out = []
        for number, p in new.items():
            before = old.get(number)
            if before is None:
                out.append(f"PR #{number} opened: {p['title']}")
                continue
            same_head = before["head"] == p["head"]
            if p["checks"] == "failing" and not (same_head and before["checks"] == "failing"
                                                 and set(p["failed"]) <= set(before["failed"])):
                out.append(f"PR #{number} failing at {p['head'][:8]}: {', '.join(p['failed'][:4])}")
            elif p["checks"] == "green" and not (same_head and before["checks"] == "green"):
                out.append(f"PR #{number} green at {p['head'][:8]}, merge state {p['merge_state']}")
            if p["mergeable"] == "CONFLICTING" and before["mergeable"] != "CONFLICTING":
                out.append(f"PR #{number} conflicts with its base")
        for number in sorted(old.keys() - new.keys(), key=int):
            how = self.closed_as(number)
            if how is None:
                new[number] = old[number]
            else:
                out.append(f"PR #{number} {how}: {old[number]['title']}")
        return out

    def mail(self):
        """The pending count from the coordination digest the shim rewrites
        (pseudolife_memory.coordination_adapter.render_digest)."""
        path = self.args.digest
        if path is None:
            session = os.environ.get("CLAUDE_CODE_SESSION_ID")
            try:
                from pseudolife_memory.coordination_identity import resolve_digest_path
            except ImportError:
                return "mail: unknown (pseudolife_memory not importable)"
            path = resolve_digest_path(session) if session else None
            if path is None:
                return "mail: unknown (no session digest)"
        try:
            body = Path(path).read_text(encoding="utf-8").partition("\n")[2]
        except OSError:
            return "mail: unknown (no session digest)"
        agent = sum(int(n) for n in re.findall(r"^Coordination: (\d+) addressed message", body, re.M))
        maintainer = any(line.startswith("Coordination: ") and "addressed message" not in line
                         for line in body.splitlines())
        if not agent and not maintainer:
            return "mail: none pending"
        parts = [f"{agent} pending"] if agent else []
        if maintainer:
            parts.append("maintainer mail waiting")
        return "mail: " + ", ".join(parts)


def _positive(text):
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError("must be above 0")
    return value


def _non_negative(text):
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def parser():
    home = Path.home() / ".pseudolife-mcp"
    p = argparse.ArgumentParser(prog="delegate_watch", description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", help="OWNER/NAME for gh (default: the current checkout's)")
    p.add_argument("--author", help="watch only pull requests by this author")
    p.add_argument("--timeout", type=_positive, default=DEFAULT_TIMEOUT,
                   help=f"seconds before a quiet exit (default {DEFAULT_TIMEOUT:g})")
    p.add_argument("--interval", type=_positive, default=DEFAULT_INTERVAL,
                   help=f"seconds between polls (default {DEFAULT_INTERVAL:g})")
    p.add_argument("--settle", type=_non_negative, default=DEFAULT_SETTLE,
                   help=f"seconds to keep collecting after the first event (default {DEFAULT_SETTLE:g})")
    p.add_argument("--state", type=Path, default=home / "watch" / "delegate.json",
                   help="state file; a ledger line per exit goes beside it (.ledger)")
    p.add_argument("--suite-results", type=Path, default=home / "suite-results")
    p.add_argument("--lease", action="append", default=None,
                   help=f"full-suite lease to watch, repeatable (default: {', '.join(DEFAULT_LEASES)})")
    p.add_argument("--lease-cli", default="pseudolife-mcp")
    p.add_argument("--digest", type=Path, help="coordination digest file (default: this session's)")
    return p


def _save(path: Path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _ledger(path: Path, kind: str, detail: str):
    """One line per exit. A ledger that cannot be written never changes the
    exit code (Codex review, 2026-10-14: a fresh install's first setup
    failure came before any state file created the directory)."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".ledger"), "a", encoding="utf-8") as f:
            f.write(f"{stamp} {kind} {detail}\n")
    except OSError as exc:
        print(f"delegate_watch: ledger not written ({exc})", file=sys.stderr)


def _utf8_stdio(out=None, err=None):
    """A background command's stdout is a file, which Python before 3.15
    encodes with the ANSI code page on Windows; a PR title with an arrow
    would then crash every run before the state is saved (review,
    2026-10-14)."""
    for stream in (out or sys.stdout, err or sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv=None, *, runner=run, clock=time.monotonic, sleep=time.sleep) -> int:
    _utf8_stdio()
    args = parser().parse_args(argv)
    args.lease = args.lease or list(DEFAULT_LEASES)
    watcher = Watcher(args, runner)
    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    start = clock()
    deadline = start + args.timeout
    found, settle_until, failures = [], None, 0
    while True:
        snap, error = watcher.snapshot(state)
        if error is not None:
            failures += 1
            print(f"delegate_watch: gh failed ({failures}/{MAX_GH_FAILURES}): {error}", file=sys.stderr)
            if failures >= MAX_GH_FAILURES:
                _ledger(args.state, "setup", f"gh failing: {error}")
                return watcher.setup(f"gh failed on {failures} polls in a row; last: {error}")
        else:
            failures = 0
        if state is None:
            if error is None:
                print("delegate_watch: baseline recorded; reporting changes from now",
                      file=sys.stderr)
                _save(args.state, snap)
                state = snap
        else:
            new = watcher.events(state, snap)
            state = snap
            if new:
                found += new
                if settle_until is None:
                    settle_until = clock() + args.settle
        now = clock()
        if found and now >= min(settle_until, deadline):
            print("\n".join(found + [watcher.mail()]))
            _save(args.state, state)
            _ledger(args.state, "events", f"{len(found)}: " + "; ".join(found)[:300])
            return EXIT_EVENTS
        if now >= deadline:
            prs = (state or {}).get("prs") or {}
            running = sum(1 for p in prs.values() if p["checks"] == "pending")
            minutes = round((now - start) / 60)
            plural = "" if len(prs) == 1 else "s"
            print(f"quiet {minutes}m: {len(prs)} open PR{plural}, {running} with checks running; "
                  f"{watcher.mail()}")
            if state is not None:
                _save(args.state, state)
            _ledger(args.state, "quiet", f"{minutes}m")
            return EXIT_QUIET
        until = settle_until if found else deadline
        sleep(max(0.0, min(args.interval, until - now)))


if __name__ == "__main__":
    sys.exit(main())
