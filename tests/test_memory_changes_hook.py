"""The per-turn memory-change note (UserPromptSubmit).

Until 2026-09-26 the plugin's prompt hook echoed the same 614-character
discipline line on every turn: 170 turns of one session cost about 25k
tokens and told the agent nothing new. The maintainer's decision
(2026-09-26): print only when memory changed since this session's last
note, meaning new lessons or new ``source="status"`` entries written by
other sessions. Mail keeps the coordination digest it already has.

The hook keeps the cursor (``<digest dir>/<sha256(session id)>.mark``) and
saves the daemon's next cursor only after printing, so a request the hook
gave up on is asked again next turn: a note is delivered at least once.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from pseudolife_memory.web.api import build_console_app
from pseudolife_memory.web.fixtures import FixtureService
from pseudolife_memory.web.session_hook import (MEMORY_CHANGES_TAIL,
                                                hook_memory_changes)
from tests.asgi_helpers import call, call_with_headers, stub_mcp
from tests.test_codex_hooks import (HOOK_PROCESS_TIMEOUT, ROOT, bash_exe,
                                    isolated_env, pwsh_run)


# ── service: what counts as a change ───────────────────────────────────────

def _entry_ts(svc, text):
    for band in svc._cms.bands:
        for entry in band.entries:
            if entry.text == text:
                return entry.timestamp
    raise AssertionError(text)


def test_changes_are_other_sessions_status_notes_and_new_lessons(pristine_service):
    svc = pristine_service
    mine = svc.episode_start_session("sess-mine", "session - mine")["id"][:12]
    other = svc.episode_start_session("sess-other", "session - other")["id"][:12]
    svc.store("old status note from another session", source="status", episode=other)
    svc.lesson_write("old task", "approach", "an old lesson", now=time.time() - 3600)
    since = _entry_ts(svc, "old status note from another session")
    time.sleep(0.05)

    svc.store("my own status note", source="status", episode=mine)
    svc.store("a plain memory, not status", source="agent", episode=other)
    svc.store("first new status note elsewhere", source="status", episode=other)
    time.sleep(0.02)
    svc.store("newest status note elsewhere", source="status", episode=other)
    svc.lesson_write("new task", "pitfall", "avoid the new dead end", polarity="-")

    out = svc.memory_changes_since(since, session_key="sess-mine")
    assert out["now"] >= _entry_ts(svc, "newest status note elsewhere")
    assert out["status_count"] == 2
    assert [s["text"] for s in out["status"]] == ["newest status note elsewhere"]
    assert out["lesson_count"] == 1
    assert out["lessons"][0]["lesson"] == "avoid the new dead end"
    assert out["lessons"][0]["polarity"] == "-"

    # Another session sees this session's note too; nothing is new after now.
    assert svc.memory_changes_since(since, session_key="sess-other")["status_count"] == 1
    later = svc.memory_changes_since(out["now"], session_key="sess-mine")
    assert later["status_count"] == 0 and later["lesson_count"] == 0


def test_a_confirmed_lesson_or_superseded_status_is_not_new(pristine_service):
    svc = pristine_service
    other = svc.episode_start_session("sess-b", "session - b")["id"][:12]
    svc.lesson_write("confirm task", "approach", "same lesson", now=time.time() - 3600)
    since = time.time()
    time.sleep(0.05)
    svc.lesson_write("confirm task", "approach", "same lesson")   # confirmation
    svc.store("a status note later replaced", source="status", episode=other)
    for band in svc._cms.bands:
        for entry in band.entries:
            if entry.text == "a status note later replaced":
                entry.superseded_at = time.time()
    out = svc.memory_changes_since(since, session_key="sess-a")
    assert out["lesson_count"] == 0 and out["status_count"] == 0


def test_no_since_for_an_unknown_session_is_a_baseline_without_a_scan(pristine_service):
    before = time.time()
    out = pristine_service.memory_changes_since(None, session_key="never-registered")
    assert out["now"] >= before and out["since"] is None
    assert out["status_count"] == 0 and out["lesson_count"] == 0


def test_a_registered_sessions_first_turn_counts_from_its_session_start(pristine_service):
    """The first prompt has no cursor yet. Counting from the moment the
    session's episode opened keeps what landed between SessionStart (whose
    briefing the session saw) and that prompt (2026-09-26 review)."""
    svc = pristine_service
    other = svc.episode_start_session("sess-early", "session - early")["id"][:12]
    svc.store("status note from before this session started", source="status", episode=other)
    time.sleep(0.05)
    started = svc.episode_start_session("sess-new", "session - new")
    time.sleep(0.05)
    svc.store("status note after SessionStart, before the first prompt",
              source="status", episode=other)
    out = svc.memory_changes_since(None, session_key="sess-new")
    assert out["since"] == pytest.approx(started["started_at"])
    assert out["status_count"] == 1
    assert out["status"][0]["text"] == "status note after SessionStart, before the first prompt"


# ── rendering: cursor line, then a note only on change ─────────────────────

def _service(**changes):
    calls = []

    def memory_changes_since(since, *, session_key=None):
        calls.append((since, session_key))
        base = {"now": 2000.5, "since": since, "status_count": 0, "status": [],
                "lesson_count": 0, "lessons": []}
        return {**base, **changes}
    return SimpleNamespace(memory_changes_since=memory_changes_since), calls


@pytest.mark.parametrize("since", [None, "", "abc", "nan", "inf", "-1", "1e3",
                                   "1" * 13, "12.3.4", "3000.25"])
def test_missing_invalid_or_future_since_answers_only_a_cursor(since):
    """A mangled mark file, a cursor from the future (a clock stepped back,
    a forged value), or a first turn the daemon cannot date: baseline,
    nothing printed."""
    svc, calls = _service(status_count=3, status=[{"text": "x"}])
    assert hook_memory_changes(svc, "s1", since) == "2000.500000\n"
    expected = 3000.25 if since == "3000.25" else None
    assert calls == [(expected, "s1")]


def test_first_turn_dated_by_the_daemon_reports_since_session_start():
    svc, _ = _service(since=1500.0, lesson_count=1,
                      lessons=[{"lesson": "new", "polarity": "+"}])
    token, note = hook_memory_changes(svc, "s1", None).split("\n", 1)
    assert token == "2000.500000"
    assert note.startswith("Memory changed since this session started:\n- 1 new lesson")


def test_the_cursor_rounds_down_never_past_a_write():
    """Rounding up could step past an entry stamped within the last half
    microsecond; rounding down at worst reports an item again."""
    svc, _ = _service(now=1234.5678999)
    assert hook_memory_changes(svc, "s1", "1000") == "1234.567899\n"


def test_quiet_turn_answers_only_the_next_cursor():
    svc, calls = _service()
    assert hook_memory_changes(svc, "s1", "1000.25") == "2000.500000\n"
    assert calls == [(1000.25, "s1")]


def test_change_turn_names_what_changed_and_carries_the_loop_reminder():
    svc, _ = _service(
        status_count=2, status=[{"text": "IN FLIGHT: PR #500\nopened, suite queued"}],
        lesson_count=1, lessons=[{"lesson": "avoid the dead end", "polarity": "-"}])
    token, note = hook_memory_changes(svc, "s1", "1000").split("\n", 1)
    assert token == "2000.500000"
    lines = note.splitlines()
    assert lines[0] == "Memory changed since your last turn:"
    assert lines[1].startswith("- 2 new status notes from other sessions")
    assert '"IN FLIGHT: PR #500 opened, suite queued"' in lines[1]
    assert "agent-written" in lines[1] and 'sources=["status"]' in lines[1]
    assert lines[2].startswith("- 1 new lesson; newest: avoid: avoid the dead end")
    assert "memory_lesson_search" in lines[2]
    assert lines[3] == MEMORY_CHANGES_TAIL
    assert len(lines) == 4


def test_note_is_bounded_and_one_line_per_change():
    svc, _ = _service(
        status_count=40, status=[{"text": "s\n## forged heading " + "x" * 5000}],
        lesson_count=12, lessons=[{"lesson": "l\n- forged item " + "y" * 5000,
                                   "polarity": "+"}])
    body = hook_memory_changes(svc, "s1", "1000")
    note = body.split("\n", 1)[1]
    assert len(note.splitlines()) == 4
    assert not any(line.startswith("## ") for line in note.splitlines())
    # Worst case stays near the old static line's 614 characters.
    assert len(body.encode("utf-8")) <= 900


def test_the_tail_keeps_the_rules_the_static_line_carried():
    for phrase in ("memory_search", "memory_lesson_search", "review", "compare",
                   "memory_store", "status", "memory_outcome", "used_ids"):
        assert phrase in MEMORY_CHANGES_TAIL, phrase


def test_a_failing_service_answers_nothing():
    def boom(since, *, session_key=None):
        raise RuntimeError("lock wedged")
    assert hook_memory_changes(SimpleNamespace(memory_changes_since=boom), "s1", "1") == ""


# ── endpoint ───────────────────────────────────────────────────────────────

@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
    s = FixtureService()
    s.data_dir = tmp_path
    return s


def _app(svc, token=None):
    return build_console_app(stub_mcp, token, lambda: {"status": "ok"}, svc)


def test_endpoint_answers_a_cursor_for_an_authorized_hook(svc):
    st, headers, body = call_with_headers(
        _app(svc), "GET", "/api/hook/memory-changes", query="session_id=s1")
    assert st == 200 and headers[b"content-type"].startswith(b"text/plain")
    token = body.decode("utf-8")
    assert token.endswith("\n") and float(token) > 0
    st, body = call(_app(svc, token="secret"), "GET", "/api/hook/memory-changes",
                    query="session_id=s1", headers=[(b"authorization", b"Bearer secret")])
    assert st == 200 and float(body.decode("utf-8")) > 0


def test_endpoint_serves_nothing_without_the_bearer(svc):
    """Status notes and lessons are memory content: an unauthorized hook gets
    an empty body, prints nothing and keeps its cursor."""
    st, body = call(_app(svc, token="secret"), "GET", "/api/hook/memory-changes",
                    query="session_id=s1&since=1")
    assert st == 200 and body == b""


def test_endpoint_rejects_post(svc):
    st, _ = call(_app(svc), "POST", "/api/hook/memory-changes")
    assert st == 405


# ── the hook scripts ───────────────────────────────────────────────────────

class _Daemon:
    """Answers each GET with the next scripted body; records query strings
    and the Authorization header. A ``(status, location)`` tuple answers a
    redirect instead."""

    def __init__(self, bodies):
        self.bodies, self.queries, self.auth = list(bodies), [], []
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                daemon.queries.append((parts.path, parse_qs(parts.query)))
                daemon.auth.append(self.headers.get("Authorization"))
                body = daemon.bodies.pop(0) if daemon.bodies else b""
                if isinstance(body, tuple):
                    self.send_response(body[0])
                    self.send_header("Location", body[1])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)


HOOKS = ["bash", "native", "cli"]


def _run_prompt_hook(hook, url, digest_dir, payload, tmp_path, **env_overrides):
    """``bash`` and ``native`` are the plugin's hooks; ``cli`` is
    ``pseudolife-mcp prompt-hook``, which the installers write for installs
    without the plugin (Claude Code settings.json, Codex hooks.json)."""
    env = isolated_env(tmp_path / "codex-home")
    env.update({"PSEUDOLIFE_MCP_DAEMON_URL": url, "PSEUDOLIFE_MCP_TOKEN": "fixture-token",
                "PSEUDOLIFE_DIGEST_DIR": str(digest_dir)})
    env.update(env_overrides)
    if hook == "bash":
        return subprocess.run(
            [bash_exe(), str(ROOT / "plugin/hooks/user-prompt-submit.sh")],
            input=payload, env=env, capture_output=True, text=True,
            timeout=HOOK_PROCESS_TIMEOUT, check=True).stdout
    if hook == "cli":
        out = subprocess.run(
            [sys.executable, "-m", "pseudolife_memory.cli", "prompt-hook"],
            input=payload, env=env, cwd=ROOT, capture_output=True, text=True,
            timeout=HOOK_PROCESS_TIMEOUT, check=True).stdout
    else:
        out = pwsh_run("-Command",
                       f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event UserPromptSubmit",
                       input=payload, env=env).stdout
    if not out.strip():
        return ""
    context = json.loads(out)["hookSpecificOutput"]
    assert context["hookEventName"] == "UserPromptSubmit"
    return context["additionalContext"] + "\n"


def _mark(digest_dir, session_id):
    return digest_dir / (hashlib.sha256(session_id.encode()).hexdigest() + ".mark")


@pytest.mark.parametrize("hook", HOOKS)
def test_prompt_hook_prints_only_changes_and_advances_its_cursor(tmp_path, hook):
    note = "Memory changed since your last turn:\n- 1 new lesson; newest: prefer: x"
    daemon = _Daemon([b"100.500000\n", ("200.250000\n" + note + "\n").encode(),
                      b"300.000000\n"])
    digest_dir = tmp_path / "digests"
    # The prompt text quotes a session_id of its own; only the top-level key counts.
    payload = json.dumps({"prompt": 'look at "session_id":"evil-id" please',
                          "session_id": "sess-1", "hook_event_name": "UserPromptSubmit"})
    try:
        first = _run_prompt_hook(hook, daemon.url, digest_dir, payload, tmp_path)
        second = _run_prompt_hook(hook, daemon.url, digest_dir, payload, tmp_path)
        third = _run_prompt_hook(hook, daemon.url, digest_dir, payload, tmp_path)
    finally:
        daemon.close()
    assert (first, second, third) == ("", note + "\n", "")
    paths = {path for path, _ in daemon.queries}
    assert paths == {"/api/hook/memory-changes"}
    assert [q.get("session_id") for _, q in daemon.queries] == [["sess-1"]] * 3
    assert [q.get("since") for _, q in daemon.queries] == [None, ["100.500000"], ["200.250000"]]
    assert _mark(digest_dir, "sess-1").read_text().strip() == "300.000000"


@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("answer", [b"", b"not-a-cursor\nnote\n"])
def test_prompt_hook_keeps_its_cursor_on_an_empty_or_malformed_answer(tmp_path, hook, answer):
    """An unauthorized hook's empty body, or garbage: print nothing and keep
    the cursor, so the next turn asks for the same window again."""
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()
    _mark(digest_dir, "sess-1").write_text("100.500000\n")
    daemon = _Daemon([answer])
    try:
        out = _run_prompt_hook(hook, daemon.url, digest_dir,
                               json.dumps({"session_id": "sess-1"}), tmp_path)
    finally:
        daemon.close()
    assert out == ""
    assert daemon.queries[0][1]["since"] == ["100.500000"]
    assert _mark(digest_dir, "sess-1").read_text().strip() == "100.500000"


@pytest.mark.parametrize("hook", HOOKS)
def test_prompt_hook_prints_nothing_when_it_cannot_save_its_cursor(tmp_path, hook):
    """A cursor that can be read but not written would repeat the same note
    on every turn; better to stay silent (2026-09-26 review)."""
    import os
    import stat
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()
    mark = _mark(digest_dir, "sess-1")
    mark.write_text("100.500000\n")
    os.chmod(mark, stat.S_IREAD)
    daemon = _Daemon([b"200.000000\nMemory changed since your last turn:\n- x\n"])
    try:
        out = _run_prompt_hook(hook, daemon.url, digest_dir,
                               json.dumps({"session_id": "sess-1"}), tmp_path)
    finally:
        daemon.close()
        os.chmod(mark, stat.S_IREAD | stat.S_IWRITE)
    assert out == ""
    assert mark.read_text().strip() == "100.500000"


@pytest.mark.parametrize("hook", HOOKS)
def test_prompt_hook_is_silent_and_keeps_its_cursor_when_the_daemon_is_down(tmp_path, hook):
    daemon = _Daemon([])
    url = daemon.url
    daemon.close()
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()
    _mark(digest_dir, "sess-1").write_text("100.500000\n")
    out = _run_prompt_hook(hook, url, digest_dir, json.dumps({"session_id": "sess-1"}),
                           tmp_path)
    assert out == ""
    assert _mark(digest_dir, "sess-1").read_text().strip() == "100.500000"


@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("session_id", ["", "bad id", "x" * 129, "a/b"])
def test_prompt_hook_asks_nothing_for_a_missing_or_unsafe_session_id(tmp_path, hook, session_id):
    daemon = _Daemon([b"100.000000\n"])
    try:
        out = _run_prompt_hook(hook, daemon.url, tmp_path / "digests",
                               json.dumps({"session_id": session_id}), tmp_path)
    finally:
        daemon.close()
    assert out == "" and daemon.queries == []


@pytest.mark.parametrize("hook", HOOKS)
def test_a_sessions_first_note_clears_marks_idle_for_a_month(tmp_path, hook):
    import os
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()
    stale, recent = _mark(digest_dir, "sess-gone"), _mark(digest_dir, "sess-live")
    for mark in (stale, recent):
        mark.write_text("100.000000\n")
    month_ago = time.time() - 40 * 86400
    os.utime(stale, (month_ago, month_ago))
    daemon = _Daemon([b"200.000000\n"])
    try:
        _run_prompt_hook(hook, daemon.url, digest_dir,
                         json.dumps({"session_id": "sess-new"}), tmp_path)
    finally:
        daemon.close()
    assert not stale.exists() and recent.exists()
    assert _mark(digest_dir, "sess-new").read_text().strip() == "200.000000"


# ── pseudolife-mcp prompt-hook (installs without the plugin) ──────────────

def test_cli_prompt_hook_refuses_a_redirect_and_keeps_its_cursor(tmp_path):
    """urllib would carry the bearer to a redirect's target: refused, like
    the briefing command and the plugin's curl --max-redirs 0."""
    elsewhere = _Daemon([b"200.000000\nMemory changed since your last turn:\n- x\n"])
    daemon = _Daemon([(302, elsewhere.url + "/api/hook/memory-changes?session_id=sess-1")])
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()
    _mark(digest_dir, "sess-1").write_text("100.500000\n")
    try:
        out = _run_prompt_hook("cli", daemon.url, digest_dir,
                               json.dumps({"session_id": "sess-1"}), tmp_path)
    finally:
        daemon.close()
        elsewhere.close()
    assert out == ""
    assert len(daemon.queries) == 1 and elsewhere.queries == []
    assert _mark(digest_dir, "sess-1").read_text().strip() == "100.500000"


def test_cli_prompt_hook_sends_the_bearer_and_prefers_the_token_file(tmp_path):
    from pseudolife_memory.credentials import _write_token_file
    token_file = tmp_path / "secrets" / "token"
    _write_token_file(token_file, "file-token")
    daemon = _Daemon([b"100.000000\n", b"200.000000\n"])
    payload = json.dumps({"session_id": "sess-1"})
    try:
        _run_prompt_hook("cli", daemon.url, tmp_path / "digests", payload, tmp_path)
        _run_prompt_hook("cli", daemon.url, tmp_path / "digests", payload, tmp_path,
                         PSEUDOLIFE_MCP_TOKEN_FILE=str(token_file))
    finally:
        daemon.close()
    assert daemon.auth == ["Bearer fixture-token", "Bearer file-token"]


@pytest.mark.parametrize("payload", ["not json", "[]", '{"session_id": 7}',
                                     '{"prompt": "\\"session_id\\": \\"sess-1\\""}'])
def test_cli_prompt_hook_asks_nothing_without_a_top_level_session_id(tmp_path, payload):
    daemon = _Daemon([b"100.000000\n"])
    try:
        out = _run_prompt_hook("cli", daemon.url, tmp_path / "digests", payload, tmp_path)
    finally:
        daemon.close()
    assert out == "" and daemon.queries == []


def test_cli_prompt_hook_prints_nothing_through_a_symlinked_cursor(tmp_path):
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()
    target = tmp_path / "elsewhere.mark"
    target.write_text("100.500000\n")
    try:
        _mark(digest_dir, "sess-1").symlink_to(target)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    daemon = _Daemon([b"200.000000\nMemory changed since your last turn:\n- x\n"])
    try:
        out = _run_prompt_hook("cli", daemon.url, digest_dir,
                               json.dumps({"session_id": "sess-1"}), tmp_path)
    finally:
        daemon.close()
    assert out == ""
    assert target.read_text().strip() == "100.500000"


def test_cli_prompt_hook_is_a_listed_mode():
    out = subprocess.run([sys.executable, "-m", "pseudolife_memory.cli", "--help"],
                         cwd=ROOT, capture_output=True, text=True, check=True,
                         timeout=HOOK_PROCESS_TIMEOUT).stdout
    assert "\n  prompt-hook " in out


def test_cli_prompt_hook_reads_its_payload_as_utf8(tmp_path):
    """A prompt quoting non-ASCII text must not cost the turn its note:
    the payload is UTF-8 whatever the locale's code page. Read as cp1252
    (a Windows host's), the 0x81 in ``ā`` (C4 81) is undefined and the
    whole payload fails to decode."""
    note = "Memory changed since your last turn:\n- 1 new lesson; newest: prefer: x"
    daemon = _Daemon([("200.250000\n" + note + "\n").encode()])
    env = isolated_env(tmp_path / "codex-home")
    env.update({"PSEUDOLIFE_MCP_DAEMON_URL": daemon.url, "PSEUDOLIFE_MCP_TOKEN": "t",
                "PSEUDOLIFE_DIGEST_DIR": str(tmp_path / "digests"),
                "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
    payload = json.dumps({"prompt": "naïve café ☕ 🚀 Rīga ā", "session_id": "sess-1"},
                         ensure_ascii=False).encode("utf-8")
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pseudolife_memory.cli", "prompt-hook"],
            input=payload, env=env, cwd=ROOT, capture_output=True,
            timeout=HOOK_PROCESS_TIMEOUT, check=True).stdout
    finally:
        daemon.close()
    context = json.loads(out)["hookSpecificOutput"]
    assert context["additionalContext"] == note
