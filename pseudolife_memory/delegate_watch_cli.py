"""Wake a delegate session with board mail only when something needs a decision.

A delegate session (the one the maintainer talks to, which dispatches work,
orders suites and merges) pays a full-context request for every wake.
Measured 2026-10-14 over six such sessions (Sep 23 - Oct 13): 2,504 turns
began with a background-task notification and 557 with board mail, and about
a third of all their turns took no action. Cache reads were 96% of their
cost-weighted input, so cost was the number of requests times the context
size. This watcher does the waiting without a model: it polls GitHub, the
suite results directory and the suite leases, and sends its owner one board
message per batch of events.

The events, each reported once (state persists in --state):
- a pull request opened, merged or closed;
- a pull request's checks all finished green (with its merge state), or a
  check failed (on that head), or they all finished with some cancelled
  (once per head, never as a failure);
- a pull request newly conflicting with its base;
- the newest completed master run of a workflow (push or schedule) failing,
  or passing again after a failure (cancelled runs are neither);
- a new result in the suite results directory;
- a full-suite lease going from held to free.

Transport is board mail. The watcher registers its own board agent and sends
the owner each batch with clears="decision events", so an owner parked
waiting_peer with park_needs="decision events" and park_clear_by=<watcher id>
is rung by the daemon; mid-turn it gets the mail as context instead. After
--quiet-after seconds without a message it sends one quiet line, which keeps
the owner's prompt cache warm, unless the owner is parked done.

It exits 0 when the owner leaves the board, when --until-lease names a lease
the owner no longer holds, after --max-age, or on --stop; it exits 2 when gh
is missing or fails on 5 polls in a row (after telling the owner), or when
the board refuses it. "Leaves the board" includes going idle: the board
lists an attached session for three hours after its last action
(ATTACHED_IDLE_WINDOW in storage/coordination.py), and an owner parked done
gets no quiet mail, so after about three hours of a done park the watcher
exits. That matches done (no follow-up expected); --detach is idempotent,
so an owner runs it again on any turn that waits. The first run without a
state file records a baseline and reports nothing for what is already true.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from pseudolife_memory.os_lock import OsLock
from pseudolife_memory.lease_cli import (
    MAX_SCOPE, _connect, _pid_alive, _printable, _project, _Refused, _Transient,
    parse_duration,
)

EXIT_OK = 0
EXIT_SETUP = 2

LABEL = "delegate-watch"
CLEARS = "decision events"
REMINDER = "Delegate: raw output goes to a subagent; a quiet wake gets one line."
# 55 minutes keeps a quiet wake inside the one-hour prompt-cache lifetime
# with room for the harness to start the turn.
DEFAULT_QUIET_AFTER = 3300.0
DEFAULT_MAX_AGE = "14d"
DEFAULT_INTERVAL = 60.0
# CI jobs and suite runs finish in clusters; one wake should carry them.
DEFAULT_SETTLE = 45.0
MAX_GH_FAILURES = 5
# How long --detach waits for the child to register and write its pid file.
DETACH_WAIT = 15.0
DETACH_POLL = 0.2
# The daemon refuses a message over 8192 UTF-8 bytes (MAX_TEXT_BYTES in
# pseudolife_memory/storage/coordination.py); a batch is cut to fit.
MAX_TEXT_BYTES = 8192
DEFAULT_LEASES = ("full-suite", "full-suite@box")
PR_FIELDS = "number,title,headRefOid,mergeable,mergeStateStatus,isDraft,statusCheckRollup"
RUN_FIELDS = "attempt,databaseId,workflowName,status,conclusion,headSha,event"
# CANCELLED is not a failure: a cancelled run is usually one the delegate
# stopped itself, and each of its jobs concluding woke it again as "failing"
# (2026-10-16, three wakes in 15 minutes for one head of PR #784).
FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
MASTER_FAILED = {"failure", "timed_out", "startup_failure"}
TITLE = 70
# gh paginates up to this; a pull request missing from the list is looked up
# by number before it counts as closed, so a full page cannot fake one.
PR_LIMIT = 500
# The daemon forgot this watcher's address (an ephemeral agent is pruned an
# hour after its last activity): register again, once, as lease_cli does.
_FORGOTTEN = frozenset({"instance_not_found", "invalid_credential"})
_AGENT_ID = re.compile(r"[0-9a-f]{32}")


class GhError(Exception):
    """A gh call failed; the message is its stderr."""


class GhMissing(Exception):
    """gh is not installed or not on PATH."""


class OwnerError(Exception):
    """No owner could be resolved; the message says why."""


def run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


# ── pure classification: JSON in, exact lines out ────────────────────────────
# tests/fixtures/delegate_watch_events.json holds cases for each function;
# the Rust port's contract tests read the same file.

def checks_state(rollup) -> tuple[str, list[str]]:
    """``failing`` with the failed names, ``pending``, ``cancelled`` with the
    cancelled names once every check has finished, ``green``, or ``none``
    for a head with no checks. One failure is enough: waiting for the rest
    would delay the decision it forces. A cancellation waits for the rest,
    so a cancelled run is one state however many of its jobs conclude."""
    failed, cancelled, pending = [], [], False
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
        elif (check.get("conclusion") or "").upper() == "CANCELLED":
            cancelled.append(check.get("name") or check.get("workflowName") or "check")
    if failed:
        return "failing", sorted(set(failed))
    if pending:
        return "pending", []
    if cancelled:
        return "cancelled", sorted(set(cancelled))
    return ("green" if rollup else "none"), []


def pr_snapshot(old, prs) -> dict:
    """The open pull requests from ``gh pr list --json``, keyed by number.
    ``failed`` holds the names behind a ``failing`` or ``cancelled`` state.
    ``cancelled_head``, present only once a head has been seen cancelled,
    stays while that head re-runs, so cancelled -> pending -> cancelled is
    reported once (Codex review, 2026-10-16)."""
    snap = {}
    for p in prs:
        checks, failed = checks_state(p.get("statusCheckRollup"))
        number = str(p["number"])
        mergeable = p.get("mergeable") or ""
        if mergeable == "UNKNOWN" and number in (old or {}):
            # GitHub recomputes mergeability lazily after the base moves;
            # keep the last known answer so CONFLICTING -> UNKNOWN ->
            # CONFLICTING is not news (review, 2026-10-14).
            mergeable = old[number]["mergeable"]
        head = p.get("headRefOid") or ""
        snap[number] = {
            "title": (p.get("title") or "")[:TITLE], "head": head,
            "checks": checks, "failed": failed, "mergeable": mergeable,
            "merge_state": (p.get("mergeStateStatus") or "unknown").lower()}
        if checks == "cancelled" or (old or {}).get(number, {}).get("cancelled_head") == head:
            snap[number]["cancelled_head"] = head
    return snap


def pr_events(old: dict, new: dict, closed: dict) -> list[str]:
    """Lines for ``old`` -> ``new``. ``closed`` maps a number missing from
    ``new`` to ``merged`` or ``closed``; a missing number not in it is not
    reported (the caller keeps tracking it until gh says how it ended)."""
    out = []
    for number, p in new.items():
        before = old.get(number)
        if before is None:
            # Its checks may have finished, or it may conflict, before the
            # first poll saw it; that result is reported now or never
            # (Codex review, 2026-10-14).
            out.append(f"PR #{number} opened: {p['title']}")
            before = {"head": "", "checks": "none", "failed": [], "mergeable": ""}
        same_head = before["head"] == p["head"]
        if p["checks"] == "failing" and not (same_head and before["checks"] == "failing"
                                             and set(p["failed"]) <= set(before["failed"])):
            out.append(f"PR #{number} failing at {p['head'][:8]}: {', '.join(p['failed'][:4])}")
        elif p["checks"] == "green" and not (same_head and before["checks"] == "green"):
            out.append(f"PR #{number} green at {p['head'][:8]}, merge state {p['merge_state']}")
        elif p["checks"] == "cancelled" and not (same_head and (
                before["checks"] == "cancelled" or before.get("cancelled_head") == p["head"])):
            out.append(f"PR #{number} cancelled at {p['head'][:8]}: {', '.join(p['failed'][:4])}")
        if p["mergeable"] == "CONFLICTING" and before["mergeable"] != "CONFLICTING":
            out.append(f"PR #{number} conflicts with its base")
    for number in sorted(old.keys() - new.keys(), key=int):
        if number in closed:
            out.append(f"PR #{number} {closed[number]}: {old[number]['title']}")
    return out


def master_snapshot(old, runs) -> dict:
    """The newest completed, non-cancelled push or schedule run per
    workflow; a workflow whose newest such run is out of the page keeps its
    previous entry. An older run never replaces the one held, and a held run
    being re-run stays as it was until its new attempt finishes, so a re-run
    is neither a false recovery nor missed (review, 2026-10-14)."""
    snap = dict(old or {})
    seen = set()
    for r in runs:
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


def master_events(old: dict, new: dict) -> list[str]:
    out = []
    for name, run_ in new.items():
        before = old.get(name)
        if before and all(before.get(k) == run_.get(k) for k in ("id", "attempt", "conclusion")):
            continue
        if run_["conclusion"] in MASTER_FAILED and (not before or before["conclusion"] == "success"):
            out.append(f"master {name} failed at {run_['sha'][:8]} ({run_['event']})")
        elif run_["conclusion"] == "success" and before and before["conclusion"] in MASTER_FAILED:
            out.append(f"master {name} green again at {run_['sha'][:8]} ({run_['event']})")
    return out


def suite_events(old: dict, new: dict) -> list[str]:
    return [f"suite {s['machine']} {s['commit']} exit {s['exit']}: {s['summary']}"
            for name, s in new.items() if name not in old]


def lease_events(old: dict, new: dict) -> list[str]:
    return [f"{name} free" for name, state in new.items()
            if state == "free" and old.get(name) == "held"]


def quiet_line(minutes: int, prs: dict) -> str:
    running = sum(1 for p in prs.values() if p["checks"] == "pending")
    plural = "" if len(prs) == 1 else "s"
    return f"quiet {minutes}m: {len(prs)} open PR{plural}, {running} with checks running"


def next_batch(found: list[str]) -> tuple[str, list[str], list[str]]:
    """The next message's text, the event lines it carries, and the lines
    left for later messages. The lines each lead with their decision (the
    wake preview shows about 100 characters of each of five messages); the
    reminder closes the message. A batch over the board's size limit is cut,
    says how many follow, and the rest go in the next message."""
    budget = MAX_TEXT_BYTES - len(REMINDER.encode("utf-8")) - 64  # room for the "follow" line
    kept: list[str] = []
    for line in found:
        size = len(line.encode("utf-8")) + 1
        if kept and size > budget:
            break
        kept.append(line)
        budget -= size
    rest = found[len(kept):]
    tail = ([f"{len(rest)} more events follow"] if rest else []) + [REMINDER]
    return "\n".join(kept + tail), kept, rest


# ── the sources ──────────────────────────────────────────────────────────────

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
            raise GhMissing("gh is not installed or not on PATH") from None
        except subprocess.TimeoutExpired:
            raise GhError("gh timed out") from None
        except OSError as exc:
            raise GhError(f"gh could not run ({exc})") from None
        if code != 0:
            lines = (err or out).strip().splitlines()
            raise GhError(lines[-1] if lines else f"gh exited {code}")
        return json.loads(out or "null")

    def prs(self, old):
        args = ["pr", "list", "--state", "open", "--limit", str(PR_LIMIT), "--json", PR_FIELDS]
        if self.args.author:
            args += ["--author", self.args.author]
        return pr_snapshot(old, self.gh(*args))

    def master(self, old):
        return master_snapshot(old, self.gh("run", "list", "--branch", "master", "--limit", "30",
                                            "--json", RUN_FIELDS))

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
                    _say(f"{self.args.lease_cli} not found; leases unknown")
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
        failed. Returns the snapshot and the gh error, if any; raises
        GhMissing."""
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

    def events(self, old, new):
        """Lines for ``old`` -> ``new``. A pull request missing from the new
        list is looked up by number; while its end is unknown it is kept in
        ``new``, so it is asked about again next poll."""
        out = []
        if old.get("prs") is not None and new.get("prs") is not None:
            closed = {}
            for number in sorted(old["prs"].keys() - new["prs"].keys(), key=int):
                how = self.closed_as(number)
                if how is None:
                    new["prs"][number] = old["prs"][number]
                else:
                    closed[number] = how
            out += pr_events(old["prs"], new["prs"], closed)
        out += master_events(old.get("master") or {}, new.get("master") or {})
        out += suite_events(old.get("suites") or {}, new.get("suites") or {})
        out += lease_events(old.get("leases") or {}, new.get("leases") or {})
        return out

    def project(self, environ) -> str:
        project = _project(environ)
        if project:
            return project
        if self.args.repo:
            return _printable(self.args.repo.rsplit("/", 1)[-1])[:MAX_SCOPE]
        try:
            return _printable(str(self.gh("repo", "view", "--json", "name").get("name", "")))[:MAX_SCOPE]
        except (GhError, GhMissing, ValueError, AttributeError):
            return ""


# ── owner, state and the pid file ────────────────────────────────────────────

def _check_id(value: str) -> str:
    if not _AGENT_ID.fullmatch(value):
        raise OwnerError(f"--owner {value!r} is not an agent id (32 lowercase hex)")
    return value


def resolve_owner(flag: str | None, environ) -> str:
    """``--owner``, else the board address the shim names in the ``.agent``
    file beside this session's coordination digest. The file must be a
    regular file (never a link) whose first line is 32 lowercase hex, as
    the plugin's hooks read it."""
    if flag is not None:
        return _check_id(flag)
    from pseudolife_memory.coordination_identity import resolve_digest_path

    session = environ.get("CLAUDE_CODE_SESSION_ID")
    if not session:
        raise OwnerError("no session to read the owner from (CLAUDE_CODE_SESSION_ID is unset)")
    digest = resolve_digest_path(session, env=environ)
    if digest is None:
        raise OwnerError("no coordination digest for this session")
    path = digest.with_suffix(".agent")
    try:
        if os.path.islink(path):
            raise OwnerError(f"{path} is a link, not a regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        raise OwnerError(f"no board address file at {path}") from None
    except OSError as exc:
        raise OwnerError(f"{path} could not be read ({exc.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OwnerError(f"{path} is not a regular file")
        first = os.read(fd, 256).split(b"\n")[0].rstrip(b"\r")
    finally:
        os.close(fd)
    if not re.fullmatch(rb"[0-9a-f]{32}", first):
        raise OwnerError(f"{path} does not hold an agent id (32 lowercase hex)")
    return first.decode("ascii")


def default_state(environ, owner: str | None = None) -> Path | None:
    """Beside the session's digest (one watcher per session), else one per
    owner in the home directory; None with neither. Keyed by owner, two
    sessions without a digest never share a watcher (review, 2026-10-14)."""
    session = environ.get("CLAUDE_CODE_SESSION_ID")
    if session:
        from pseudolife_memory.coordination_identity import resolve_digest_path

        digest = resolve_digest_path(session, env=environ)
        if digest is not None:
            return digest.with_suffix(".dwatch.json")
    if owner is None:
        return None
    return Path.home() / ".pseudolife-mcp" / "watch" / f"{owner}.json"


def _process_start(pid: int) -> str | None:
    """An opaque token for when ``pid`` started, or None when it cannot be
    read. With the pid it tells a running watcher from a reused pid; stdlib
    only, since a shim runtime has no psutil."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    try:
        # Field 22 of /proc/<pid>/stat, counted after the parenthesised name.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def _write_pid(state: Path, *, agent_id: str, owner: str, nonce: str | None = None) -> None:
    _save(state.with_suffix(".pid"), {"pid": os.getpid(), "process_start": _process_start(os.getpid()),
                                      "agent_id": agent_id, "owner": owner, "started": time.time(),
                                      "nonce": nonce})


def _running(state: Path) -> dict | None:
    """The pid file's record when it names a live process that started when
    the record says, else None."""
    try:
        record = json.loads(state.with_suffix(".pid").read_text(encoding="utf-8"))
        pid = int(record["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not _pid_alive(pid):
        return None
    recorded, current = record.get("process_start"), _process_start(pid)
    if recorded is not None and current is not None and recorded != current:
        return None
    return record


def _remove_own_pid(state: Path) -> None:
    path = state.with_suffix(".pid")
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def _park_hint(agent_id: str) -> str:
    return f'park with park_needs="{CLEARS}" park_clear_by={agent_id}'


# ── arguments and small helpers ──────────────────────────────────────────────

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


def _age(text):
    try:
        value = parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be above 0")
    return value


def parser():
    home = Path.home() / ".pseudolife-mcp"
    p = argparse.ArgumentParser(
        prog="pseudolife-mcp delegate-watch", description=__doc__.split("\n\n")[0],
        epilog=("Start it from the delegate session with --detach; it prints its board id. "
                f'Then park waiting_peer with park_needs="{CLEARS}" and park_clear_by=<that id>. '
                "Stop it with --stop."))
    p.add_argument("--owner", help="the board agent id to send to (32 lowercase hex; default: "
                                   "this session's, from the .agent file beside its digest)")
    p.add_argument("--detach", action="store_true",
                   help="start the watcher as a detached process and print its board id")
    p.add_argument("--stop", action="store_true",
                   help="ask the watcher for --state to exit at its next poll")
    p.add_argument("--until-lease", metavar="NAME",
                   help="exit once the owner no longer holds this board lease")
    p.add_argument("--repo", help="OWNER/NAME for gh (default: the current checkout's)")
    p.add_argument("--author", help="watch only pull requests by this author")
    p.add_argument("--quiet-after", type=_positive, default=DEFAULT_QUIET_AFTER,
                   help=f"seconds without a message before a quiet line (default {DEFAULT_QUIET_AFTER:g})")
    p.add_argument("--max-age", type=_age, default=_age(DEFAULT_MAX_AGE),
                   help=f"how long to run: 90, 90s, 20m, 2h or 7d (default {DEFAULT_MAX_AGE})")
    p.add_argument("--interval", type=_positive, default=DEFAULT_INTERVAL,
                   help=f"seconds between polls (default {DEFAULT_INTERVAL:g})")
    p.add_argument("--settle", type=_non_negative, default=DEFAULT_SETTLE,
                   help=f"seconds to keep collecting after the first event (default {DEFAULT_SETTLE:g})")
    p.add_argument("--state", type=Path,
                   help="state file (default: <session digest>.dwatch.json, else "
                        "~/.pseudolife-mcp/watch/<owner>.json); .pid, .lock, .stop, .log and .ledger "
                        "files go beside it")
    p.add_argument("--suite-results", type=Path, default=home / "suite-results")
    p.add_argument("--lease", action="append", default=None,
                   help=f"full-suite lease to watch, repeatable (default: {', '.join(DEFAULT_LEASES)})")
    p.add_argument("--lease-cli", default="pseudolife-mcp")
    # Set by --detach for the child it starts, which writes it into the pid
    # file: in a Windows venv python.exe is a launcher, so the watcher's own
    # pid is not the one Popen returned (review, 2026-10-14).
    p.add_argument("--nonce", help=argparse.SUPPRESS)
    return p


def _say(message: str) -> None:
    print(f"delegate-watch: {message}", file=sys.stderr, flush=True)


def _save(path: Path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _ledger(path: Path, kind: str, detail: str):
    """One line per message and per exit. A ledger that cannot be written
    never changes the exit code (Codex review, 2026-10-14: a fresh install's
    first setup failure came before any state file created the directory)."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".ledger"), "a", encoding="utf-8") as f:
            f.write(f"{stamp} {kind} {detail}\n")
    except OSError as exc:
        _say(f"ledger not written ({exc})")


def _utf8_stdio(out=None, err=None):
    """A detached watcher's output is a log file, which Python before 3.15
    encodes with the ANSI code page on Windows; a PR title with an arrow
    would then crash the watcher before the state is saved (review,
    2026-10-14)."""
    for stream in (out or sys.stdout, err or sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _spawn_detached(argv: list[str], log: Path) -> int:
    """Start ``argv`` so it outlives the session that asked, appending its
    output to ``log``; returns the pid. The same flags as
    ``pseudolife_memory.shim._spawn_detached``, so Claude Code's 2-hour cap
    on background commands does not apply."""
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "ab")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": handle, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(argv, **kwargs).pid
    finally:
        handle.close()


def _log_tail(log: Path) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""


# ── the commands ─────────────────────────────────────────────────────────────

def _stop(state: Path) -> int:
    record = _running(state)
    if record is None:
        print(f"delegate-watch: no watcher running for {state}; nothing changed")
        return EXIT_OK
    state.with_suffix(".stop").parent.mkdir(parents=True, exist_ok=True)
    state.with_suffix(".stop").write_text("", encoding="utf-8")
    print(f"delegate-watch {record.get('agent_id')} asked to stop; it exits at its next poll")
    return EXIT_OK


def _foreign(record: dict, owner: str, state: Path) -> bool:
    """Say so when the watcher on ``state`` serves another owner."""
    if record.get("owner") == owner:
        return False
    _say(f"{state} belongs to the watcher for {record.get('owner')}; "
         f"pass --state <another path> to watch for {owner}")
    return True


def _already(record: dict) -> int:
    agent_id = record.get("agent_id")
    print(f"delegate-watch {agent_id} already running for {record.get('owner')}; "
          f"{_park_hint(agent_id)}")
    return EXIT_OK


def _detach(argv: list[str], args, owner: str, state: Path, spawn, clock, sleep) -> int:
    """Start the watcher detached and wait for its pid record, matched by a
    nonce. The pid file is never removed here: a start racing this one may
    already have written its record, and the state's lock decides which
    watcher runs."""
    nonce = uuid.uuid4().hex
    child = [a for a in argv if a != "--detach"] + ["--nonce", nonce]
    if args.owner is None:
        child += ["--owner", owner]
    if args.state is None:
        child += ["--state", str(state)]
    pid_path = state.with_suffix(".pid")
    log = state.with_suffix(".log")
    pid = spawn([sys.executable, "-m", "pseudolife_memory.cli", "delegate-watch", *child], log)
    deadline = clock() + DETACH_WAIT
    while True:
        try:
            record = json.loads(pid_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if isinstance(record, dict) and record.get("nonce") == nonce:
            agent_id = record.get("agent_id")
            print(f"delegate-watch {agent_id} watching for {owner}; {_park_hint(agent_id)}")
            return EXIT_OK
        other = _running(state)
        if other is not None:                # another start won the state's lock
            return EXIT_SETUP if _foreign(other, owner, state) else _already(other)
        if not _pid_alive(pid):
            break
        if clock() >= deadline:
            print(f"delegate-watch: still starting; see {log}")
            return EXIT_OK
        sleep(DETACH_POLL)
    tail = _log_tail(log)
    _say(f"the watcher did not start ({tail or 'no output'}); its log is {log}")
    return EXIT_SETUP


def main(argv=None, *, runner=run, clock=time.monotonic, sleep=time.sleep, board=None,
         environ=None, spawn=_spawn_detached) -> int:
    _utf8_stdio()
    argv = list(sys.argv[2:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    args = parser().parse_args(argv)
    args.lease = args.lease or list(DEFAULT_LEASES)
    try:
        owner = resolve_owner(args.owner, environ)
    except OwnerError as exc:
        owner, why = None, exc
    state = args.state or default_state(environ, owner)
    if args.stop:
        if state is None:
            _say("no session to find the watcher by; pass --owner <id> or --state <path>")
            return EXIT_SETUP
        return _stop(state)
    if owner is None:
        _say(f"{why}; pass --owner <32-hex board agent id of the session to wake>")
        return EXIT_SETUP
    record = _running(state)
    if record is not None:
        return EXIT_SETUP if _foreign(record, owner, state) else _already(record)
    if args.detach:
        return _detach(argv, args, owner, state, spawn, clock, sleep)
    # One watcher per state, held for its whole life: two starts racing past
    # the pid check above both get here, and only one takes the lock.
    lock = OsLock(state.with_suffix(".lock"))
    try:
        if not lock.acquire():
            _say(f"another watcher holds {state}; this one exits")
            return EXIT_OK
    except OSError as exc:
        _say(f"cannot lock {state.with_suffix('.lock')} ({exc})")
        return EXIT_SETUP
    try:
        if board is None:
            board, why = _connect(False, None, environ)
            if board is None:
                _say(f"no board: {why}")
                _ledger(state, "setup", f"no board: {why}")
                return EXIT_SETUP
        try:
            return _Loop(args, owner, state, Watcher(args, runner), board, environ, clock, sleep).run()
        finally:
            _remove_own_pid(state)
            close = getattr(board, "close", None)
            if close is not None:
                close()
    finally:
        lock.release()


class _Loop:
    """One loop, no threads: poll, classify, deliver, sleep."""

    def __init__(self, args, owner, state, watcher, board, environ, clock, sleep):
        self.args, self.owner, self.state = args, owner, state
        self.watcher, self.board, self.environ = watcher, board, environ
        self.clock, self.sleep = clock, sleep
        self.task = f"decision events for {owner[:8]}"
        self.project = None
        self.last_event = None
        self.last_row: dict = {}

    # board calls ─────────────────────────────────────────────────────────

    def register(self):
        if self.project is None:
            self.project = self.watcher.project(self.environ)
        self.board.register(task=self.task, project=self.project, label=LABEL, status="starting")
        _write_pid(self.state, agent_id=self.board.agent_id, owner=self.owner, nonce=self.args.nonce)

    def call(self, action):
        """``action()``, registering again once if the daemon forgot this
        address. Raises _Refused or _Transient. A registration that failed
        last poll is tried again first: a call without an address would
        crash the watcher without a word to the owner (review, 2026-10-14)."""
        if not self.board.registered:
            self.register()
        try:
            return action()
        except _Refused as exc:
            if exc.code not in _FORGOTTEN:
                raise
            _say(f"the board forgot this watcher ({exc.code}); registering again")
            self.board.forget()
            self.register()
            return action()

    def owner_row(self):
        """The owner's board row; None when the board no longer lists it. A
        cut page without it proves nothing, so that reads as the last row
        seen (its park still decides the quiet mail)."""
        agents, truncated = self.call(self.board.agents_page)
        for agent in agents:
            if agent.get("agent_id") == self.owner:
                self.last_row = agent
                return agent
        return self.last_row if truncated else None

    def owner_holds(self, name):
        leases, _ = self.call(lambda: self.board.leases(name))
        for lease in leases:
            if lease.get("name") == name:
                return (lease.get("holder") or {}).get("agent_id") == self.owner
        return False

    def send(self, text, request_id=None):
        """Every message clears the park: after a re-registration the owner's
        park_clear_by names the old id, and only the need still matches."""
        request_id = request_id or uuid.uuid4().hex
        self.call(lambda: self.board.send(self.owner, text, request_id, clears=CLEARS))

    def give_up(self, why):
        """Tell the owner the watcher stopped, best effort, then exit 2: it
        never goes silent."""
        _say(why)
        _ledger(self.state, "setup", why)
        try:
            self.send(f"delegate-watch stopped: {why}. Restart it with "
                      f"`pseudolife-mcp delegate-watch --detach` once that is fixed.")
        except Exception as exc:  # noqa: BLE001 — the exit code must stand
            _say(f"could not tell the owner ({type(exc).__name__})")
        return EXIT_SETUP

    def leave(self, reason):
        _ledger(self.state, "exit", reason)
        _say(f"exiting: {reason}")
        return EXIT_OK

    # the loop ────────────────────────────────────────────────────────────

    def run(self) -> int:
        args, stop = self.args, self.state.with_suffix(".stop")
        try:
            stop.unlink()                     # a request meant for an earlier watcher
        except OSError:
            pass
        try:
            self.register()
        except (_Refused, _Transient) as exc:
            _say(f"could not register on the board: {exc}")
            _ledger(self.state, "setup", f"could not register on the board: {exc}")
            return EXIT_SETUP
        print(f"delegate-watch {self.board.agent_id} watching for {self.owner}; "
              f"{_park_hint(self.board.agent_id)}", flush=True)
        try:
            state = json.loads(self.state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = None
        start = self.clock()
        deadline = start + args.max_age
        last_sent = start
        found, settle_until, failures = [], None, 0
        # The batch being delivered: its text and request id stay fixed
        # across retries, so a send the daemon committed but whose reply was
        # lost is deduplicated, not delivered twice (review, 2026-10-14).
        pending = None
        while True:
            if stop.exists():
                try:
                    stop.unlink()
                except OSError:
                    pass
                return self.leave("stopped")
            try:
                snap, error = self.watcher.snapshot(state)
            except GhMissing as exc:
                return self.give_up(str(exc))
            if error is not None:
                failures += 1
                _say(f"gh failed ({failures}/{MAX_GH_FAILURES}): {error}")
                if failures >= MAX_GH_FAILURES:
                    return self.give_up(f"gh failed on {failures} polls in a row; last: {error}")
            else:
                failures = 0
            if state is None:
                if error is None:
                    _say("baseline recorded; reporting changes from now")
                    _save(self.state, snap)
                    state = snap
            else:
                new = self.watcher.events(state, snap)
                state = snap
                if new:
                    found += new
                    if settle_until is None:
                        settle_until = self.clock() + args.settle
            now = self.clock()
            try:
                row = self.owner_row()
                if row is None:
                    return self.leave("owner gone")
                if args.until_lease and not self.owner_holds(args.until_lease):
                    return self.leave(f"lease {args.until_lease} no longer held by the owner")
                if pending is None and found and now >= min(settle_until, deadline):
                    text, kept, found = next_batch(found)
                    pending = {"text": text, "request_id": uuid.uuid4().hex, "lines": kept}
                    settle_until = now if found else None
                if pending is not None:
                    self.send(pending["text"], pending["request_id"])
                    _ledger(self.state, "sent", f"{len(pending['lines'])}: " + "; ".join(pending["lines"]))
                    self.last_event = time.strftime("%H:%M")
                    pending, last_sent = None, now
                elif (not found and now - last_sent >= args.quiet_after
                      and row.get("park_reason") != "done"):
                    minutes = round((now - last_sent) / 60)
                    self.send(quiet_line(minutes, (state or {}).get("prs") or {}))
                    _ledger(self.state, "quiet", f"{minutes}m")
                    last_sent = now
                self.call(lambda: self.board.update(status=self._status(state)))
            except _Transient as exc:
                _say(f"the board failed this poll ({exc}); trying again next poll")
            except _Refused as exc:
                _say(f"the board refused this watcher: {exc}")
                _ledger(self.state, "setup", f"board refused: {exc}")
                return EXIT_SETUP
            if state is not None and not found and pending is None:
                _save(self.state, state)
            if now >= deadline:
                unsent = (pending["lines"] if pending else []) + found
                if unsent:
                    _ledger(self.state, "lost", f"{len(unsent)} unsent at exit: " + "; ".join(unsent))
                return self.leave("max age")
            wait = args.interval
            for due in (settle_until if found else None, deadline,
                        None if found or pending else last_sent + args.quiet_after):
                if due is not None and due > now:
                    wait = min(wait, due - now)
            self.sleep(wait)

    def _status(self, state) -> str:
        prs = (state or {}).get("prs") or {}
        plural = "" if len(prs) == 1 else "s"
        last = f"last event {self.last_event}" if self.last_event else "no events yet"
        return f"watching {len(prs)} open PR{plural}; {last}"
