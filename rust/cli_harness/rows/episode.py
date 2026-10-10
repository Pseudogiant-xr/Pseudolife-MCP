"""CLI-EPISODE: ``episode-start`` and ``episode-end``.

Canonical inputs: the mode-only legacy hook commands (``ops/install-hook.ps1``
and ``.sh`` name them to remove them as obsolete; earlier installers wrote
them) with the host's SessionStart/SessionEnd JSON on stdin: a string
``session_id`` and a string or absent ``cwd`` (``episode_cli.py::_read_stdin``;
PARITY "Episode hook producer grammar"). Two kinds of case:

- wire cases against a fixture daemon: the health probe and the POSTed body
  bytes, headers and silence rules;
- bank cases against two real Python daemons on disposable banks, one per
  arm, comparing the episodes and client_sessions rows each case's keys own.
Source-vote titles are out of scope until their producer lands on master.
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import normalize
from ..core import Case
from ..fixture_daemon import TLS_CA, Trickle, factory, json_body, redirect, text
from ..mutants import Mutant
from . import _daemon

HEALTH = {"status": "ok", "version": "0.17.0", "schema": 55, "storage": "postgres",
          "auth": False, "bank": None, "persist_errors": 0, "memory": {"source": "process"}}
EPISODE = {"id": "0" * 32, "title": "x", "started_at": 1.0}


def routes(**overrides):
    table = {"/health": json_body(HEALTH), "/api/episode/start": json_body(EPISODE),
             "/api/episode/end": json_body({})}
    table.update(overrides)
    return {k: v for k, v in table.items() if v is not None}


def daemon(**overrides):
    return factory(routes(**overrides))


def hook(session="w1b-ep-wire", event="SessionStart", **fields):
    payload = {"session_id": session, "transcript_path": "{HOME}/t.jsonl",
               "hook_event_name": event, **fields}
    if event == "SessionStart":
        payload.setdefault("source", "startup")
    else:
        payload.setdefault("reason", "exit")
    return payload


TITLE = ("episode-title-minute",)
TOKEN = {"PSEUDOLIFE_MCP_TOKEN": "w1b-fixture-token"}


def git_repo(arm):
    (arm.cwd / "proj" / ".git").mkdir(parents=True)
    (arm.cwd / "proj" / "src" / "deep").mkdir(parents=True)


def _wire_cases() -> list[Case]:
    c: list[Case] = []
    add = c.append
    start, end = ["episode-start"], ["episode-end"]
    add(Case("start-plain-dir", start, daemon=daemon(), rules=TITLE,
             stdin_json=hook(cwd="{CWD}"), setup=lambda arm: None))
    add(Case("start-git-subdir", start, daemon=daemon(), rules=TITLE, setup=git_repo,
             stdin_json=hook(cwd="{CWD}/proj/src/deep")))
    add(Case("start-cwd-home", start, daemon=daemon(), rules=TITLE,
             stdin_json=hook(cwd="{HOME}")))
    add(Case("start-cwd-home-lowercase-drive", start, daemon=daemon(), rules=TITLE,
             stdin_json=hook(cwd="{HOME_LOWER_DRIVE}"), platforms=("windows",),
             note="VS Code-launched hosts report c:\...; Python compares abspath strings"))
    add(Case("start-cwd-missing", start, daemon=daemon(), rules=TITLE, stdin_json=hook()))
    add(Case("start-cwd-null", start, daemon=daemon(), rules=TITLE,
             stdin_json=hook(cwd=None)))
    add(Case("start-cwd-non-ascii", start, daemon=daemon(), rules=TITLE,
             setup=lambda arm: (arm.cwd / "projé ✓ 🚀").mkdir(),
             stdin_json=hook(cwd="{CWD}/projé ✓ 🚀")))
    add(Case("start-cwd-system-dir", start, daemon=daemon(), rules=TITLE,
             stdin_json=hook(cwd="C:\\Windows\\System32")))
    add(Case("start-cwd-windows-project", start, daemon=daemon(), rules=TITLE,
             stdin_json=hook(cwd="C:\\work\\client-app")))
    add(Case("start-token", start, daemon=daemon(), rules=TITLE, env=TOKEN,
             stdin_json=hook(cwd="{CWD}")))
    add(Case("end-plain", end, daemon=daemon(), stdin_json=hook(event="SessionEnd")))
    add(Case("end-token", end, daemon=daemon(), env=TOKEN,
             stdin_json=hook(event="SessionEnd", cwd="{CWD}")))
    add(Case("start-no-session", start, daemon=daemon(), stdin_json={"cwd": "{CWD}"}))
    add(Case("start-empty-session", start, daemon=daemon(), stdin_json=hook(session="")))
    add(Case("start-not-json", start, daemon=daemon(), stdin=b"{not json"))
    add(Case("start-empty-stdin", start, daemon=daemon(), stdin=b""))
    add(Case("start-daemon-down", start, stdin_json=hook(cwd="{CWD}")))
    add(Case("start-health-degraded", start, rules=TITLE, stdin_json=hook(cwd="{CWD}"),
             daemon=daemon(**{"/health": json_body({**HEALTH, "status": "degraded"}, 503)})))
    add(Case("start-health-html", start, stdin_json=hook(cwd="{CWD}"),
             daemon=daemon(**{"/health": text("<html>502</html>", 502, "text/html")})))
    add(Case("start-post-500", start, rules=TITLE, stdin_json=hook(cwd="{CWD}"),
             daemon=daemon(**{"/api/episode/start": text("boom", 500)})))
    add(Case("start-post-redirect", start, rules=TITLE, env=TOKEN,
             stdin_json=hook(cwd="{CWD}"),
             daemon=daemon(**{"/api/episode/start": redirect("/elsewhere")})))
    add(Case("start-trailing-args", ["episode-start", "--ignored", "x"], daemon=daemon(),
             rules=TITLE, stdin_json=hook(cwd="{CWD}")))

    # Transport (urllib semantics, hook_http): each case must reach the POST.
    add(Case("start-localhost", start, rules=TITLE, stdin_json=hook(cwd="{CWD}"),
             daemon=daemon(), after=require_post,
             env={"PSEUDOLIFE_MCP_DAEMON_URL": "http://localhost:{DAEMON_PORT}"}))
    # On Windows, 30 runs on 2026-10-10 took 4.14-4.67 s for both arms with
    # 100 ms gaps; one extra 180 ms fixture wakeup reproduced the hosted abort.
    # One-byte pieces keep the whole reply past 250 ms while 5 ms gaps leave
    # scheduler headroom inside that per-read deadline. Still require the POST
    # and a duration beyond the deadline, so a total-timeout client cannot pass.
    add(Case("start-health-trickle", start, rules=TITLE, stdin_json=hook(cwd="{CWD}"),
             daemon=daemon(**{"/health": Trickle(json_body(HEALTH), 1, 0.005)}),
             after=require_post_read(0.25)))
    # The reply takes ~8 s in 8-byte pieces 0.7 s apart: past the 5 s timeout
    # in total, inside it per receive. Both arms must wait for the whole reply.
    add(Case("end-post-trickle", end, stdin_json=hook(event="SessionEnd"), timeout=60,
             after=require_post_read(6.0),
             daemon=daemon(**{"/api/episode/end": Trickle(json_body({}), 8, 0.7)})))
    add(Case("start-https", start, rules=TITLE, stdin_json=hook(cwd="{CWD}"),
             daemon=factory(routes(), tls=True), after=require_post,
             env={**TOKEN, "SSL_CERT_FILE": str(TLS_CA)}))
    add(Case("start-health-redirect-loop", start, stdin_json=hook(cwd="{CWD}"),
             daemon=daemon(**{"/health": redirect("/health")})))
    return c


def require_post_read(seconds: float):
    """The POST happened and the arm stayed until its slow reply was read."""
    def after(arm, observation):
        require_post(arm, observation)
        lo, hi = observation["window"]
        if hi - lo < seconds:
            observation["vacuous"] = (f"left after {hi - lo:.1f} s, before the {seconds:g} s "
                                      "reply was read")
    return after


def require_post(arm, observation):
    """A transport case exists to show the POST happened in this arm."""
    if not any(r["method"] == "POST" for r in observation.get("requests", [])):
        observation["vacuous"] = "no episode POST where the case requires one"


# Bank cases: both arms' daemons see the same sequence of operations; after
# each case the rows owned by the case's session keys are compared.
_RUN_START = time.time()


def bank_rows(keys: tuple[str, ...], expect=None):
    """After the CLI runs: the rows the case's keys own, and the case's
    expected state, so a start both arms silently dropped is a difference
    rather than a match of two empty banks."""
    def after(arm, observation):
        bank = arm.daemon.bank
        episodes = _daemon.rows(
            bank, "SELECT to_jsonb(e)::text FROM episodes e WHERE session_key = ANY(%s) "
                  "ORDER BY session_key, started_at", (list(keys),))
        sessions = _daemon.rows(
            bank, "SELECT to_jsonb(s)::text FROM client_sessions s "
                  "WHERE session_key = ANY(%s) ORDER BY session_key", (list(keys),))
        lo, hi = observation["window"]
        offset = observation["utc_offset"]
        ids: dict[str, str] = {}
        db = {"episodes": normalize.episode_rows(episodes, _RUN_START, lo, hi, offset, ids),
              "client_sessions": normalize.episode_rows(sessions, _RUN_START, lo, hi, offset,
                                                         ids)}
        observation["db"] = db
        problem = expect(db) if expect else None
        if problem:
            observation["vacuous"] = f"bank state not as the case requires: {problem}"
    return after


def _episode(db, key):
    return [r for r in db["episodes"] if r["session_key"] == key]


def _session(db, key):
    return next((r for r in db["client_sessions"] if r["session_key"] == key), None)


def _open_episode(key, title_prefix=None, starts=None, roots=None):
    def check(db):
        rows = _episode(db, key)
        session = _session(db, key)
        if len(rows) != 1 or rows[0]["ended_at"] is not None:
            return f"{key}: expected one open episode, got {rows}"
        if title_prefix and not rows[0]["title"].startswith(title_prefix):
            return f"{key}: title {rows[0]['title']!r}"
        if session is None or session["ended_at"] is not None:
            return f"{key}: expected an open client session, got {session}"
        if starts is not None and len(session["start_times"]) != starts:
            return f"{key}: expected {starts} start times, got {session['start_times']}"
        if roots is not None and len(session["episode_ids"]) != roots:
            return f"{key}: expected {roots} root episodes, got {session['episode_ids']}"
        return None
    return check


def _ended(key, refreshed: bool):
    def check(db):
        session = _session(db, key)
        if _episode(db, key):
            return f"{key}: expected the empty episode deleted, got {_episode(db, key)}"
        if session is None or session["end_reason"] != "end":
            return f"{key}: expected an ended client session, got {session}"
        stamp = "<t:case>" if refreshed else "<t:earlier>"
        if session["ended_at"] != stamp:
            return f"{key}: expected ended_at {stamp}, got {session['ended_at']}"
        return None
    return check


def _both(*checks):
    def check(db):
        return next((p for p in (c(db) for c in checks) if p), None)
    return check


def _bank_cases() -> list[Case]:
    real = _daemon.shared("episode")
    c: list[Case] = []
    add = c.append

    def bank(case_id, mode, key, cwd=None, setup=None, keys=None, rules=(), expect=None):
        fields = {} if cwd is None else {"cwd": cwd}
        event = "SessionStart" if mode == "episode-start" else "SessionEnd"
        add(Case(case_id, [mode], daemon=real, stdin_json=hook(session=key, event=event,
                                                                **fields),
                 setup=setup, after=bank_rows(keys or (key,), expect), rules=rules,
                 timeout=60, bank=True))

    a, b = "w1b-bank-a", "w1b-bank-b"
    bank("bank-start-git", "episode-start", a, cwd="{CWD}/proj/src", setup=git_repo,
         expect=_open_episode(a, "proj - ", starts=1, roots=1))
    bank("bank-start-repeat", "episode-start", a, cwd="{CWD}/other",
         expect=_open_episode(a, "proj - ", starts=2, roots=1))
    bank("bank-start-second", "episode-start", b, cwd="{HOME}", keys=(a, b),
         expect=_both(_open_episode(a), _open_episode(b, "session - ", starts=1)))
    bank("bank-end-first", "episode-end", a, keys=(a, b),
         expect=_both(_ended(a, refreshed=True), _open_episode(b)))
    bank("bank-end-again", "episode-end", a, expect=_ended(a, refreshed=True))
    bank("bank-end-unknown", "episode-end", "w1b-bank-never-started",
         expect=lambda db: (None if not db["episodes"] and not db["client_sessions"]
                            else f"unexpected rows {db}"))
    bank("bank-restart-after-end", "episode-start", a, cwd="{CWD}",
         expect=_open_episode(a, "cwd - ", roots=2))
    return c


def cases() -> list[Case]:
    return _wire_cases() + _bank_cases()


MUTANTS = [
    Mutant("episode-start-path", "episode", "shim/src/cli/episode.rs",
           '"start",\n            format!', '"begin",\n            format!',
           ("start-plain-dir", "bank-start-git")),
    Mutant("episode-body-spacing", "episode", "shim/src/cli/episode.rs",
           'format!("{{\\"session_key\\": {key}}}")', 'format!("{{\\"session_key\\":{key}}}")',
           ("end-plain",)),
    Mutant("episode-system-dir-titled", "episode", "shim/src/cli/episode.rs",
           '"system32" | "syswow64"', '"system33" | "syswow64"', ("start-cwd-system-dir",)),
    Mutant("episode-home-drive-insensitive", "episode", "shim/src/cli/episode.rs",
           ".is_some_and(|(home, cwd)| home.as_os_str() == cwd.as_os_str());",
           ".is_some_and(|(home, cwd)| home == cwd);", ("start-cwd-home-lowercase-drive",),
           platforms=("windows",)),
    Mutant("episode-default-name", "episode", "shim/src/cli/episode.rs",
           '"session".chars()', '"sessions".chars()', ("start-cwd-home", "bank-start-second")),
]
