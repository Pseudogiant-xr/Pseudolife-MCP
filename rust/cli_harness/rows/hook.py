"""CLI-HOOK: ``briefing``, ``prompt-hook`` and ``doorbell-prompt-seen``.

Canonical inputs are what the shipped producers pass:
- ``briefing --hook-json`` and ``briefing --hook-json --coordination``
  (``ops/install-hook.ps1`` / ``.sh``), ``briefing`` with ``--max-unsure N``,
  ``--max-lessons N``, ``--max-world N`` and ``briefing --coordination``
  (docs/guide/episodes.md, configuration.md), ``--help``;
- ``prompt-hook`` with the host's UserPromptSubmit JSON on stdin (installers);
- ``doorbell-prompt-seen`` with the Codex UserPromptSubmit JSON on stdin
  (``plugin/hooks/coordination-prompt.sh``, ``lifecycle.ps1``), against the
  pending record ``codex_doorbell_state.PendingNotice.reserve`` writes.
The daemon side is a fixture serving the real routes' shapes; both arms see
identical bytes, and the requests each arm sends are compared.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from .. import producers as p
from ..core import Case
from ..fixture_daemon import TLS_CA, Trickle, factory, json_body, redirect, text
from ..mutants import Mutant

HEALTH = {"status": "ok", "version": "0.17.0", "schema": 55, "storage": "postgres",
          "auth": True, "bank": "a1b2c3d4", "persist_errors": 0,
          "memory": {"source": "process", "rss_bytes": 812000000}}
SESSION_START = ("## Memory at session start\nUse the shared Pseudolife bank for every task. "
                 "First call `memory_search` with\nthe task in natural language.\n\n"
                 "## Lessons from past work\n- prefer: quote \"exact\" ids — ünïcödé ✓ 🚀\n")
CHECKIN = ("Pseudolife coordination: at the first task and on resume, use memory_agents"
           "(action=list) to check peers.\n")
MARKDOWN = "# Session briefing\n\n- **3** facts changed\n- naïve café — “quoted”\n\n"


def routes(**overrides):
    table = {
        "/health": json_body(HEALTH),
        "/api/briefing": json_body({"markdown": MARKDOWN, "lessons": [1, 2]}),
        "/api/hook/session-start": text(SESSION_START),
        "/api/hook/coordination-start": text(CHECKIN),
        "/api/hook/memory-changes": text("1791516000.25\nMemory changed: 2 new lessons — read "
                                         "them.\n"),
    }
    table.update(overrides)
    return {k: v for k, v in table.items() if v is not None}


def daemon(**overrides):
    return factory(routes(**overrides))


TOKEN = {"PSEUDOLIFE_MCP_TOKEN": "w1b-fixture-token"}


def _briefing_cases() -> list[Case]:
    c: list[Case] = []
    add = c.append
    add(Case("briefing-help", ["briefing", "--help"]))
    add(Case("briefing-daemon-down", ["briefing", "--hook-json"]))
    add(Case("briefing-plain", ["briefing"], daemon=daemon(), env=TOKEN))
    add(Case("briefing-caps", ["briefing", "--max-unsure", "5", "--max-lessons", "2",
                               "--max-world", "0"], daemon=daemon(), env=TOKEN))
    add(Case("briefing-hook-json", ["briefing", "--hook-json"], daemon=daemon(), env=TOKEN))
    add(Case("briefing-hook-json-no-token", ["briefing", "--hook-json"], daemon=daemon()))
    add(Case("briefing-hook-json-coordination", ["briefing", "--hook-json", "--coordination"],
             daemon=daemon(), env=TOKEN))
    add(Case("briefing-coordination", ["briefing", "--coordination"], daemon=daemon(),
             env=TOKEN))
    add(Case("briefing-coordination-opted-out", ["briefing", "--hook-json", "--coordination"],
             daemon=daemon(), env={**TOKEN, "PSEUDOLIFE_AGENT_COORDINATION": "0"}))
    add(Case("briefing-coordination-explicit-on", ["briefing", "--hook-json", "--coordination"],
             daemon=daemon(), env={**TOKEN, "PSEUDOLIFE_AGENT_COORDINATION": " Yes "}))
    add(Case("briefing-coordination-empty", ["briefing", "--hook-json", "--coordination"],
             daemon=daemon(**{"/api/hook/coordination-start": text("")}), env=TOKEN))
    add(Case("briefing-health-degraded-503", ["briefing", "--hook-json"],
             daemon=daemon(**{"/health": json_body({**HEALTH, "status": "degraded",
                                                     "not_ready": "warming"}, 503)}),
             env=TOKEN))
    add(Case("briefing-health-html-502", ["briefing", "--hook-json"],
             daemon=daemon(**{"/health": text("<html>bad gateway</html>", 502,
                                              "text/html")}), env=TOKEN))
    add(Case("briefing-health-null", ["briefing", "--hook-json"],
             daemon=daemon(**{"/health": text("null", 200, "application/json")}), env=TOKEN))
    add(Case("briefing-markdown-empty", ["briefing"],
             daemon=daemon(**{"/api/briefing": json_body({"markdown": ""})}), env=TOKEN))
    add(Case("briefing-markdown-null", ["briefing"],
             daemon=daemon(**{"/api/briefing": json_body({"markdown": None})}), env=TOKEN))
    add(Case("briefing-markdown-missing", ["briefing"],
             daemon=daemon(**{"/api/briefing": json_body({"facts": []})}), env=TOKEN))
    add(Case("briefing-payload-500", ["briefing"],
             daemon=daemon(**{"/api/briefing": text("Internal Server Error", 500)}), env=TOKEN))
    add(Case("briefing-session-start-401", ["briefing", "--hook-json"],
             daemon=daemon(**{"/api/hook/session-start": text("unauthorized", 401)}), env=TOKEN))
    add(Case("briefing-session-start-whitespace", ["briefing", "--hook-json"],
             daemon=daemon(**{"/api/hook/session-start": text(" \n\t\n")}), env=TOKEN))
    add(Case("briefing-payload-redirect", ["briefing", "--hook-json"],
             daemon=daemon(**{"/api/hook/session-start": redirect("/elsewhere")}), env=TOKEN))
    # Trickled replies: every gap inside the per-read timeout (health 0.25 s,
    # payload 5 s), the whole reply longer than it. urllib times each
    # receive, so Python completes; a total-time budget would not.
    add(Case("briefing-health-trickle", expect_output=True, argv=["briefing", "--hook-json"], env=TOKEN,
             daemon=daemon(**{"/health": Trickle(json_body(HEALTH), 16, 0.1)})))
    add(Case("briefing-payload-trickle", expect_output=True, argv=["briefing", "--hook-json"], env=TOKEN,
             daemon=daemon(**{"/api/hook/session-start": Trickle(text(SESSION_START), 24, 0.6)})))
    # Health redirects follow urllib's HTTPRedirectHandler limits: one URL
    # at most four times, at most ten distinct targets.
    add(Case("briefing-health-redirect-loop", ["briefing", "--hook-json"], env=TOKEN,
             daemon=daemon(**{"/health": redirect("/health")})))
    chain = {f"/h{i}": redirect(f"/h{i + 1}") for i in range(12)}
    add(Case("briefing-health-redirect-chain", ["briefing", "--hook-json"], env=TOKEN,
             daemon=daemon(**{"/health": redirect("/h0"), **chain})))
    add(Case("briefing-health-redirect-once", ["briefing", "--hook-json"], env=TOKEN,
             daemon=daemon(**{"/health": redirect("/healthz"), "/healthz": json_body(HEALTH)})))
    # https daemon origins (remote and tunnel connects): the fixture serves
    # the pg_tls test certificate; both arms trust its CA through
    # SSL_CERT_FILE (CPython's default verify paths; rustls-native-certs).
    tls_env = {**TOKEN, "SSL_CERT_FILE": str(TLS_CA)}
    add(Case("briefing-https-hook-json", expect_output=True, argv=["briefing", "--hook-json"], env=tls_env,
             daemon=factory(routes(), tls=True)))
    add(Case("briefing-https-health-trickle", expect_output=True, argv=["briefing", "--hook-json"], env=tls_env,
             daemon=factory(routes(**{"/health": Trickle(json_body(HEALTH), 16, 0.1)}),
                            tls=True)))
    add(Case("briefing-https-untrusted", ["briefing", "--hook-json"], env=TOKEN,
             daemon=factory(routes(), tls=True)))
    localhost = {**TOKEN, "PSEUDOLIFE_MCP_DAEMON_URL": "http://localhost:{DAEMON_PORT}"}
    add(Case("briefing-hook-json-localhost", expect_output=True, argv=["briefing", "--hook-json"], daemon=daemon(),
             env=localhost, note="localhost resolves ::1 first; the fixture listens on IPv4"))
    add(Case("briefing-plain-localhost", expect_output=True, argv=["briefing"], daemon=daemon(), env=localhost))
    add(Case("briefing-invalid-daemon-url", ["briefing", "--hook-json"],
             env={"PSEUDOLIFE_MCP_DAEMON_URL": "ftp://127.0.0.1:1"}))

    def launcher(arm):
        local = arm.home / "AppData" / "Local" if os.name == "nt" else arm.home / ".local" / "share"
        name = "pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp"
        path = local / "pseudolife-mcp" / "bin" / name
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
        path.chmod(0o755)
    add(Case("briefing-hook-json-launcher-query", ["briefing", "--hook-json"], daemon=daemon(),
             env=TOKEN, setup=launcher))
    return c


SESSION = "w1b-prompt-session"


def _mark(home: Path, session: str = SESSION) -> Path:
    return p.digest_dir(home) / f"{p.digest_key(session)}.mark"


def prompt_input(session=SESSION, **extra) -> bytes:
    payload = {"session_id": session, "transcript_path": "/tmp/t.jsonl", "cwd": "/tmp",
               "hook_event_name": "UserPromptSubmit", "prompt": "continue the gate", **extra}
    return json.dumps(payload).encode()


def _prompt_cases() -> list[Case]:
    c: list[Case] = []
    add = c.append
    add(Case("prompt-first-note", ["prompt-hook"], stdin=prompt_input(), daemon=daemon(),
             env=TOKEN))
    add(Case("prompt-existing-cursor", ["prompt-hook"], stdin=prompt_input(), daemon=daemon(),
             env=TOKEN,
             setup=lambda arm: p.write_private(_mark(arm.home), "1791515000.5\n")))
    add(Case("prompt-cursor-only", ["prompt-hook"], stdin=prompt_input(), env=TOKEN,
             daemon=daemon(**{"/api/hook/memory-changes": text("1791516000.25\n")})))
    add(Case("prompt-cursor-no-newline", ["prompt-hook"], stdin=prompt_input(), env=TOKEN,
             daemon=daemon(**{"/api/hook/memory-changes": text("1791516000.25")})))
    add(Case("prompt-crlf-body", ["prompt-hook"], stdin=prompt_input(), env=TOKEN,
             daemon=daemon(**{"/api/hook/memory-changes": text("17.5\r\nnote ü\r\n")})))
    add(Case("prompt-bad-cursor", ["prompt-hook"], stdin=prompt_input(), env=TOKEN,
             daemon=daemon(**{"/api/hook/memory-changes": text("soon\nnote\n")}),
             setup=lambda arm: p.write_private(_mark(arm.home), "12.0\n")))
    add(Case("prompt-payload-500", ["prompt-hook"], stdin=prompt_input(), env=TOKEN,
             daemon=daemon(**{"/api/hook/memory-changes": text("boom", 500)}),
             setup=lambda arm: p.write_private(_mark(arm.home), "12.0\n")))
    add(Case("prompt-daemon-down", ["prompt-hook"], stdin=prompt_input(), env=TOKEN))
    add(Case("prompt-trickle", expect_output=True, argv=["prompt-hook"], stdin=prompt_input(), env=TOKEN,
             daemon=daemon(**{"/api/hook/memory-changes": Trickle(
                 text("1791516000.25\nMemory changed: 2 new lessons.\n"), 12, 0.3)})))
    add(Case("prompt-https-trickle", expect_output=True, argv=["prompt-hook"], stdin=prompt_input(),
             env={**TOKEN, "SSL_CERT_FILE": str(TLS_CA)},
             daemon=factory(routes(**{"/api/hook/memory-changes": Trickle(
                 text("1791516000.25\nMemory changed: 2 new lessons.\n"), 12, 0.3)}),
                 tls=True)))
    add(Case("prompt-localhost", expect_output=True, argv=["prompt-hook"], stdin=prompt_input(), daemon=daemon(),
             env={**TOKEN, "PSEUDOLIFE_MCP_DAEMON_URL": "http://localhost:{DAEMON_PORT}"}))
    add(Case("prompt-no-token", ["prompt-hook"], stdin=prompt_input(), daemon=daemon()))
    add(Case("prompt-token-file", ["prompt-hook"], stdin=prompt_input(), daemon=daemon(),
             env={"PSEUDOLIFE_MCP_TOKEN_FILE": "{HOME}/tok"},
             setup=lambda arm: p.write_private(arm.home / "tok", "w1b-file-token\n")))
    add(Case("prompt-invalid-session", ["prompt-hook"], stdin=prompt_input("has space"),
             daemon=daemon(), env=TOKEN))
    add(Case("prompt-session-max-length", ["prompt-hook"], stdin=prompt_input("a" * 128),
             daemon=daemon(), env=TOKEN))
    add(Case("prompt-session-too-long", ["prompt-hook"], stdin=prompt_input("a" * 129),
             daemon=daemon(), env=TOKEN))
    add(Case("prompt-not-json", ["prompt-hook"], stdin=b"not json", daemon=daemon(), env=TOKEN))
    add(Case("prompt-non-object", ["prompt-hook"], stdin=b"[1, 2]", daemon=daemon(), env=TOKEN))
    add(Case("prompt-digest-dir-env", ["prompt-hook"], stdin=prompt_input(), daemon=daemon(),
             env={**TOKEN, "PSEUDOLIFE_DIGEST_DIR": "{HOME}/custom-digests"}))

    def stale_marks(arm):
        d = p.digest_dir(arm.home)
        old, fresh = d / ("0" * 64 + ".mark"), d / ("1" * 64 + ".mark")
        p.write_private(old, "1.0\n")
        p.write_private(fresh, "2.0\n")
        past = time.time() - 31 * 86400
        os.utime(old, (past, past))
    add(Case("prompt-first-note-prunes-old-marks", ["prompt-hook"], stdin=prompt_input(),
             daemon=daemon(), env=TOKEN, setup=stale_marks))
    add(Case("prompt-stdout-closed", ["prompt-hook"], stdin=prompt_input(), daemon=daemon(),
             env=TOKEN, stdout_closed=True, rules=("python-shutdown-flush-silent",)))
    return c


_PENDING_CACHE: dict[str, str] = {}


def pending_text(thread: str, *, count=2, maintainer=0, legacy=False) -> str:
    """The pending record bytes ``PendingNotice.reserve`` writes, made once
    in a scratch digest dir so both arms seed identical bytes."""
    key = f"{thread}:{count}:{maintainer}:{legacy}"
    if key not in _PENDING_CACHE:
        from pseudolife_memory.codex_doorbell_state import PendingNotice, notice_text  # noqa
        with tempfile.TemporaryDirectory() as scratch:
            notice = PendingNotice(Path(scratch), thread)
            if legacy:
                nonce = uuid.UUID(int=7).hex
                record = {"thread_id": thread, "nonce": nonce, "count": count,
                          "text": notice_text(count, nonce)}
                _PENDING_CACHE[key] = json.dumps(record, separators=(",", ":")) + "\n"
            else:
                # The real reserve(), with its nonce source pinned so goldens
                # recorded in one process replay in another.
                import pseudolife_memory.codex_doorbell_state as state  # noqa: PLC0415
                original = state.uuid.uuid4
                state.uuid.uuid4 = lambda: uuid.UUID(int=0x5EED + count + maintainer)
                try:
                    record = notice.reserve(count, maintainer=maintainer,
                                            expires_at=1_791_600_000.0, now=1_791_500_000.0)
                finally:
                    state.uuid.uuid4 = original
                _PENDING_CACHE[key] = notice.path.read_text(encoding="utf-8")
                assert record is not None
    return _PENDING_CACHE[key]


THREAD = "0199c3f0-2b7e-7a51-9c1d-4e5f6a7b8c9d"


def _bell(home: Path, suffix: str, thread: str = THREAD) -> Path:
    return p.digest_dir(home) / f"{p.digest_key(thread)}.{suffix}"


def seed_pending(**kwargs):
    def setup(arm):
        p.write_private(_bell(arm.home, "bell-pending"), pending_text(THREAD, **kwargs))
    return setup


def bell_input(prompt: str | None, thread=THREAD, **extra) -> bytes:
    payload = {"session_id": thread, "turn_id": "t1", "hook_event_name": "UserPromptSubmit",
               "cwd": "/w", "model": "gpt-6.1-sol", **extra}
    if prompt is not None:
        payload["prompt"] = prompt
    return json.dumps(payload).encode()


def _notice(**kwargs) -> str:
    return json.loads(pending_text(THREAD, **kwargs))["text"]


def _doorbell_cases() -> list[Case]:
    c: list[Case] = []
    add = c.append
    add(Case("doorbell-positive", ["doorbell-prompt-seen"],
             stdin=None, setup=seed_pending(), note="stdin set lazily"))
    add(Case("doorbell-positive-maintainer", ["doorbell-prompt-seen"], stdin=None,
             setup=seed_pending(count=3, maintainer=1)))
    add(Case("doorbell-ordinary-prompt", ["doorbell-prompt-seen"],
             stdin=bell_input("please continue"), setup=seed_pending()))
    add(Case("doorbell-no-pending", ["doorbell-prompt-seen"], stdin=bell_input("anything")))
    add(Case("doorbell-missing-prompt", ["doorbell-prompt-seen"], stdin=bell_input(None),
             setup=seed_pending()))
    add(Case("doorbell-prompt-not-string", ["doorbell-prompt-seen"],
             stdin=json.dumps({"session_id": THREAD, "prompt": 7}).encode(),
             setup=seed_pending()))
    add(Case("doorbell-session-not-uuid", ["doorbell-prompt-seen"],
             stdin=bell_input("x", thread="not-a-thread"), setup=seed_pending()))
    add(Case("doorbell-other-thread", ["doorbell-prompt-seen"],
             stdin=bell_input("x", thread="0199c3f0-2b7e-7a51-9c1d-000000000000"),
             setup=seed_pending()))
    add(Case("doorbell-not-json", ["doorbell-prompt-seen"], stdin=b"{", setup=seed_pending()))
    add(Case("doorbell-legacy-record", ["doorbell-prompt-seen"], stdin=None,
             setup=seed_pending(legacy=True), rules=("doorbell-legacy-first-seen",)))
    add(Case("doorbell-digest-dir-env", ["doorbell-prompt-seen"], stdin=None,
             env={"PSEUDOLIFE_DIGEST_DIR": "{HOME}/custom-digests"},
             setup=lambda arm: p.write_private(
                 arm.home / "custom-digests" / f"{p.digest_key(THREAD)}.bell-pending",
                 pending_text(THREAD))))
    return c


def _finish(cases: list[Case]) -> list[Case]:
    # Positive doorbell inputs carry the exact notice text, rendered from the
    # same cached record the setup seeds.
    for case in cases:
        if case.stdin is None:
            kwargs = {}
            if case.id == "doorbell-positive-maintainer":
                kwargs = {"count": 3, "maintainer": 1}
            elif case.id == "doorbell-legacy-record":
                kwargs = {"legacy": True}
            case.stdin = bell_input(_notice(**kwargs))
    return cases


def cases() -> list[Case]:
    return _finish(_briefing_cases() + _prompt_cases() + _doorbell_cases())


MUTANTS = [
    Mutant("hook-briefing-cap-order", "hook", "shim/src/cli/briefing_hook.rs",
           '"/api/briefing?max_unsure={}&max_lessons={}&max_world={}",\n'
           '            args.caps[0], args.caps[1], args.caps[2]',
           '"/api/briefing?max_unsure={}&max_lessons={}&max_world={}",\n'
           '            args.caps[1], args.caps[0], args.caps[2]', ("briefing-caps",)),
    Mutant("hook-event-name", "hook", "shim/src/cli/briefing_hook.rs",
           'payload("SessionStart", context)', 'payload("Sessionstart", context)',
           ("briefing-hook-json",)),
    Mutant("hook-token-dropped", "hook", "shim/src/cli/briefing_hook.rs",
           'let token = std::env::var("PSEUDOLIFE_MCP_TOKEN").ok();',
           'let token: Option<String> = None;', ("briefing-hook-json",)),
    Mutant("hook-first-address-only", "hook", "shim/src/cli/hook_http.rs",
           "for address in tokio::net::lookup_host((host.as_str(), port)).await.ok()? {",
           "for address in tokio::net::lookup_host((host.as_str(), port)).await.ok()?.take(1) {",
           ("briefing-hook-json-localhost",)),
    Mutant("hook-whole-reply-budget", "hook", "shim/src/cli/hook_http.rs",
           "            Poll::Ready(result) => {\n                this.read = None;\n",
           "            Poll::Ready(result) => {\n",
           ("briefing-health-trickle", "prompt-trickle")),
    Mutant("hook-redirect-repeat-limit", "hook", "shim/src/cli/hook_http.rs",
           "visited.get(&next).copied().unwrap_or(0) >= 4",
           "visited.get(&next).copied().unwrap_or(0) >= 5",
           ("briefing-health-redirect-loop",)),
    Mutant("hook-cursor-not-saved", "hook", "shim/src/cli/briefing_hook.rs",
           'file.write_all(format!("{next}\\n").as_bytes()).ok()?;',
           'file.write_all(b"").ok()?;', ("prompt-first-note",)),
    Mutant("hook-since-dropped", "hook", "shim/src/cli/briefing_hook.rs",
           'target.push_str(&format!("&since={since}"));', 'let _ = &since;',
           ("prompt-existing-cursor",)),
    Mutant("hook-doorbell-receipt-inverted", "hook", "shim/src/cli/doorbell_seen.rs",
           'if payload["prompt"] == record["text"] {', 'if payload["prompt"] != record["text"] {',
           ("doorbell-positive", "doorbell-ordinary-prompt")),
]
