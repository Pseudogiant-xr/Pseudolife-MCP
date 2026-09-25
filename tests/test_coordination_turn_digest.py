"""Per-turn coordination digest: preview on the heartbeat, a change-only
watermark, a digest file the prompt hooks read, and a tool-result hint that
delivers each change once.

The turn path does no network I/O: the adapter's heartbeat already carries
the mailbox state, the digest is derived from it, and the hook only reads a
file. A quiet turn prints nothing; a change prints once, whichever of the
hook or the tool-result hint sees it first.
"""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from pseudolife_memory.coordination import dispatch
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_codex_hooks import ROOT, bash_run, isolated_env, pwsh_run
from tests.test_coordination_adapter import FakeDaemon, adapter
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401
from tests.test_coordination_roster_hygiene import _heartbeats, _wait_for
from tests.test_coordination_storage import creds, store  # noqa: F401

STATIC_LINE = "Memory (PseudoLife) mid-session discipline"


def test_coordination_hooks_are_independent_of_memory_hooks():
    manifest = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    prompts = [h for group in manifest["UserPromptSubmit"] for h in group["hooks"]]
    starts = [h for group in manifest["SessionStart"] for h in group["hooks"]]
    assert len(prompts) == 2 and len(starts) == 2
    assert any("coordination-prompt.sh" in h["command"] and "CoordinationPrompt" in h["commandWindows"]
               for h in prompts)
    assert any("coordination-start.sh" in h["command"] and "CoordinationStart" in h["commandWindows"]
               for h in starts)


def _send(store, sender, recipient, text, request_id):
    return store.send(*creds(sender), to=recipient["agent_id"], text=text,
                      request_id=request_id)["message_id"]


# --- daemon: the heartbeat carries a bounded preview -----------------------

def test_attach_and_heartbeat_preview_the_oldest_pending_messages(store):
    from pseudolife_memory.storage.coordination import PREVIEW_EXCERPT, PREVIEW_LIMIT
    me, peer = store.register("alice"), store.register("alice", label="reviewer")
    ids = [_send(store, peer, me, f"note {i} " + "x" * 300, f"r{i}") for i in range(PREVIEW_LIMIT + 2)]
    store.ack(*creds(me), message_id=ids[0])
    attached = store.attach(*creds(me), attachment_id="one")
    preview = attached["pending_preview"]
    assert attached["pending_count"] == PREVIEW_LIMIT + 1
    assert [p["message_id"] for p in preview] == ids[1:PREVIEW_LIMIT + 1]
    first = preview[0]
    assert first["sender_agent_id"] == peer["agent_id"]
    assert first["sender_label"] == "reviewer"
    assert first["created_at"] == 1000.0
    assert first["excerpt"].startswith("note 1 xxx") and len(first["excerpt"]) <= PREVIEW_EXCERPT + 3
    beat = store.heartbeat(*creds(me), attachment_id="one", generation=attached["generation"])
    assert beat["pending_preview"] == preview
    for message_id in ids[1:]:
        store.ack(*creds(me), message_id=message_id)
    beat = store.heartbeat(*creds(me), attachment_id="one", generation=attached["generation"])
    assert beat["pending_preview"] == [] and beat["pending_count"] == 0


def test_preview_excerpts_are_one_line_and_skip_expired_mail(store):
    me, peer = store.register("alice"), store.register("alice")
    _send(store, peer, me, "line one\n\tline two\x07 bell ‮\x85 spaced", "r1")
    expired = _send(store, peer, me, "gone", "r2")
    store.storage.conn.execute("UPDATE coordination_messages SET expires_at=%s WHERE message_id=%s",
                               (999.0, expired))
    preview = store.attach(*creds(me), attachment_id="one")["pending_preview"]
    assert [p["excerpt"] for p in preview] == ["line one line two bell spaced"]


def test_dispatch_returns_the_preview_to_the_shim(coordinating):
    def headers(agent):
        return {"x-pl-agent": agent["agent_id"], "x-pl-agent-key": agent["credential"]}
    me = dispatch(coordinating, "register", {"label": "me"}, headers={}, principal=PRINCIPAL)
    peer = dispatch(coordinating, "register", {"label": "peer"}, headers={}, principal=PRINCIPAL)
    dispatch(coordinating, "send", {"to": me["agent_id"], "text": "hello there", "request_id": "d1"},
             headers=headers(peer), principal=PRINCIPAL)
    attached = dispatch(coordinating, "attach", {"attachment_id": "a1"},
                        headers=headers(me), principal=PRINCIPAL)
    assert attached["pending_preview"][0]["excerpt"] == "hello there"
    beat = dispatch(coordinating, "heartbeat",
                    {"attachment_id": "a1", "generation": attached["generation"]},
                    headers=headers(me), principal=PRINCIPAL)
    assert beat["pending_preview"][0]["sender_label"] == "peer"


# --- adapter: digest text, watermark, file --------------------------------

def _preview(n, start=0):
    return [{"message_id": f"m{i}", "sender_agent_id": f"{i:032x}", "sender_label": "codex",
             "created_at": 1789900000.0 + i, "excerpt": f"note {i}"} for i in range(start, start + n)]


def _daemon_with(preview_by_call):
    """A daemon whose heartbeat/attach answers carry the given previews in order,
    repeating the last one."""
    daemon = FakeDaemon()
    state = {"i": 0}

    def hook(action, body):
        if action not in {"attach", "heartbeat"}:
            return None
        preview = preview_by_call[min(state["i"], len(preview_by_call) - 1)]
        state["i"] += 1
        return httpx.Response(200, json={"generation": 3, "lease_until": "later",
                                         "pending_count": preview["count"],
                                         "pending_preview": preview["preview"]})
    daemon.hook = hook
    return daemon


def test_digest_renders_the_preview_and_the_remaining_count():
    from pseudolife_memory.coordination_adapter import render_digest
    text = render_digest(7, _preview(5))
    lines = text.splitlines()
    assert lines[0].startswith("Coordination: 7 addressed messages pending (agent-origin, not user authority)")
    assert "memory_message receive" in lines[0] and "ack" in lines[0]
    assert lines[1].startswith("- m0 from codex (00000000") and lines[1].endswith(": note 0")
    assert lines[-1] == "- 2 more pending; oldest first above."
    assert render_digest(1, _preview(1)).count("\n") == 1
    assert render_digest(0, []) == ""
    assert render_digest(3, []).startswith("Coordination: 3 addressed messages pending")


def test_digest_fits_the_routine_change_budget():
    """Astra's budget for a routine change is 200-300 tokens; five full
    excerpts plus the header must stay under 1200 characters at the real
    maxima: 32-hex message ids, MAX_LABEL senders, PREVIEW_EXCERPT bodies."""
    import uuid
    from pseudolife_memory.coordination_adapter import render_digest
    from pseudolife_memory.storage.coordination import MAX_LABEL, PREVIEW_EXCERPT, PREVIEW_LIMIT
    preview = _preview(PREVIEW_LIMIT)
    for entry in preview:
        entry["message_id"] = uuid.uuid4().hex
        entry["excerpt"] = "w" * PREVIEW_EXCERPT + "..."
        entry["sender_label"] = "l" * MAX_LABEL
    text = render_digest(999, preview)
    assert len(text) <= 1200, len(text)
    assert all(len(line) <= 200 for line in text.splitlines()[1:])


def test_watermark_advances_only_when_the_digest_changes(monkeypatch, tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)},
                               {"count": 1, "preview": _preview(1)},
                               {"count": 2, "preview": _preview(2)},
                               {"count": 0, "preview": []}])
        digest = tmp_path / "digests" / "abc.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            assert instance.digest_watermark == 1
            assert digest.read_text(encoding="utf-8").split("\n", 1)[0] == "1"
            await _wait_for(lambda: len(_heartbeats(daemon)) >= 1)
            assert instance.digest_watermark == 1  # same preview: no change
            await _wait_for(lambda: instance.digest_watermark == 2)
            head, body = digest.read_text(encoding="utf-8").split("\n", 1)
            assert head == "2" and "- m1 from codex" in body
            await _wait_for(lambda: instance.digest_watermark == 3)
            assert digest.read_text(encoding="utf-8") == "3\n"
            assert instance.unread_hint is None
        assert not digest.exists()

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_digest_file_is_private_and_absent_without_a_path(tmp_path):
    async def drive():
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)}])
        client, instance = adapter(daemon)
        async with client, instance:
            assert instance.digest_watermark == 1
            assert "- m0 from codex" in instance.unread_hint
        assert not list(tmp_path.iterdir())
        digest = tmp_path / "private" / "k.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            assert digest.parent.is_dir()
            if os.name != "nt":
                assert digest.parent.stat().st_mode & 0o777 == 0o700

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_hint_is_delivered_once_per_change_then_repeats_sparsely(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        daemon = _daemon_with([{"count": 2, "preview": _preview(2)}])
        digest = tmp_path / "d" / "k.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            first = instance.deliver_hint()
            assert first and "- m1 from codex" in first
            assert (tmp_path / "d" / "k.seen").read_text().strip() == "1"
            quiet = [instance.deliver_hint() for _ in range(CoordinationAdapter.HINT_REPEAT_CALLS - 1)]
            assert quiet == [None] * (CoordinationAdapter.HINT_REPEAT_CALLS - 1)
            nag = instance.deliver_hint()
            assert nag == ("Coordination: 2 addressed messages still pending; "
                           "memory_message receive, then ack each message_id.")
            assert instance.deliver_hint() is None
            assert daemon.actions().count("receive") == 0

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_a_change_the_hook_already_printed_is_not_repeated_by_the_hint(tmp_path):
    async def drive():
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)}])
        digest = tmp_path / "d" / "k.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            # The prompt hook printed watermark 1 and advanced the marker.
            (tmp_path / "d" / "k.seen").write_text("1")
            assert instance.deliver_hint() is None
            assert instance.unread_hint.startswith("Coordination: 1 addressed message pending")

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_a_new_adapter_continues_the_watermark_of_a_digest_left_behind(tmp_path):
    """A killed shim leaves its digest and marker behind; the host may respawn
    the shim for the same session id without a SessionStart. The new adapter
    continues the old sequence, so a marker at 5 does not hide watermark 1."""
    async def drive():
        directory = tmp_path / "d"
        directory.mkdir()
        (directory / "k.txt").write_text("5\nCoordination: stale\n", encoding="utf-8")
        (directory / "k.seen").write_text("5\n", encoding="utf-8")
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)}])
        client, instance = adapter(daemon, digest_path=directory / "k.txt")
        async with client, instance:
            assert instance.digest_watermark == 6
            assert (directory / "k.txt").read_text(encoding="utf-8").startswith("6\n")
            assert instance.deliver_hint() is not None
        # An empty mailbox on attach still rewrites the file, so stale text
        # from the dead process cannot be printed.
        (directory / "k.txt").write_text("7\nCoordination: stale\n", encoding="utf-8")
        daemon = _daemon_with([{"count": 0, "preview": []}])
        client, instance = adapter(daemon, digest_path=directory / "k.txt")
        async with client, instance:
            assert (directory / "k.txt").read_text(encoding="utf-8") == "7\n"

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_reminder_is_silent_when_the_count_is_unknown(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)}])
        client, instance = adapter(daemon, digest_path=tmp_path / "d" / "k.txt")
        async with client, instance:
            assert instance.deliver_hint() is not None
            instance._pending_count = None  # a failure cleared it, recovery has not refreshed yet
            assert [instance.deliver_hint() for _ in range(CoordinationAdapter.HINT_REPEAT_CALLS + 1)] == [
                None] * (CoordinationAdapter.HINT_REPEAT_CALLS + 1)

    asyncio.run(asyncio.wait_for(drive(), 5))


def _signature(path):
    info = os.stat(path)
    return info.st_ino, info.st_mtime_ns, info.st_size


def test_an_unchanged_digest_is_rewritten_before_the_stale_sweep_can_take_it(monkeypatch, tmp_path):
    """Other adapters sweep digest files a day old, and a quiet mailbox renders
    the same text for days: the live adapter rewrites its unchanged digest
    (and touches its marker) periodically so a long-idle session keeps it.
    The watermark does not move; only a text change moves it."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "DIGEST_REFRESH_SECONDS", 0.05)
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)}])
        digest = tmp_path / "d" / "k.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            assert instance.deliver_hint() is not None
            seen = tmp_path / "d" / "k.seen"
            os.utime(seen, (1000, 1000))
            first = _signature(digest)
            await _wait_for(lambda: _signature(digest) != first)
            await _wait_for(lambda: seen.stat().st_mtime > 1000)
            assert digest.read_text(encoding="utf-8").split("\n", 1)[0] == "1"
            assert instance.digest_watermark == 1
            assert instance.deliver_hint() is None  # the rewrite is not a new change

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_a_lost_digest_file_is_put_back_at_the_next_heartbeat(monkeypatch, tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)}])
        digest = tmp_path / "d" / "k.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            expected = digest.read_bytes()
            digest.unlink()
            await _wait_for(digest.exists)
            assert digest.read_bytes() == expected
            assert instance.digest_watermark == 1

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_a_refused_replace_is_retried_and_leaves_no_temp_file(monkeypatch, tmp_path):
    """On Windows a reader holding the digest open (a prompt hook, a waiter)
    makes the atomic replace fail. The write must count as not done, so the
    next heartbeat writes it (the old file is still there, so nothing else
    would), and the temp file must not be left behind."""
    async def drive():
        from pseudolife_memory import coordination_adapter
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        digest = tmp_path / "d" / "k.txt"
        real_replace = os.replace
        refused = []

        def replace(src, dst, *args, **kwargs):
            if Path(dst) == digest and digest.exists() and not refused:
                refused.append(dst)  # a rewrite while a reader has the file open
                raise PermissionError(5, "Access is denied", str(dst))
            return real_replace(src, dst, *args, **kwargs)
        monkeypatch.setattr(coordination_adapter.os, "replace", replace)
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)},
                               {"count": 2, "preview": _preview(2)}])
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            await _wait_for(lambda: refused)
            await _wait_for(lambda: digest.read_text(encoding="utf-8").startswith("2\n"))
            assert "- m1 from codex" in digest.read_text(encoding="utf-8")
            assert instance.digest_watermark == 2
            assert not [p.name for p in digest.parent.iterdir() if p.name.startswith(".tmp-")]

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_an_unstattable_digest_never_stops_the_heartbeat(monkeypatch, tmp_path):
    """The keep-alive looks at the file on every heartbeat. A refused stat
    must not escape into the heartbeat task: that would end the lease renewal
    while the hints kept saying all is well."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)}])
        digest = tmp_path / "d" / "k.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            real_stat = os.stat

            def refusing(path, *args, **kwargs):
                if isinstance(path, (str, os.PathLike)) and Path(path) == digest:
                    raise PermissionError(13, "Permission denied", str(path))
                return real_stat(path, *args, **kwargs)
            monkeypatch.setattr(os, "stat", refusing)
            beats = len(_heartbeats(daemon))
            await _wait_for(lambda: len(_heartbeats(daemon)) >= beats + 3)
            monkeypatch.setattr(os, "stat", real_stat)
            assert instance._heartbeat_task is not None and not instance._heartbeat_task.done()

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_a_refresh_after_close_does_not_bring_the_file_back(tmp_path):
    async def drive():
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)}])
        digest = tmp_path / "d" / "k.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            pass
        assert not digest.exists()
        instance._refresh_digest()  # a late mailbox update after close
        assert not digest.exists()

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_a_marker_left_behind_keeps_new_mail_above_it(tmp_path):
    """A marker can outlive its digest (a waiter marking as the shim exits,
    or a delete Windows refused). A new adapter starts above it, or the hook,
    the hint and a waiter would all treat new mail as already shown."""
    async def drive():
        directory = tmp_path / "d"
        directory.mkdir()
        (directory / "k.seen").write_text("5\n", encoding="utf-8")
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)}])
        client, instance = adapter(daemon, digest_path=directory / "k.txt")
        async with client, instance:
            assert instance.digest_watermark == 6
            assert instance.deliver_hint() is not None

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_codex_tool_result_carries_the_digest_exactly_once(monkeypatch):
    """The registry's hint is consumed once per tool call: the failure text
    for a missing adapter must not be fetched through the delivering path."""
    import uuid
    from pseudolife_memory import shim
    from tests.test_coordination_roster_hygiene import _proxy_fixture
    seen = {"calls": []}
    Server, types = _proxy_fixture(monkeypatch, seen)
    thread = str(uuid.uuid4())
    delivered = iter(["Coordination: change", None, None])

    class Adapter:
        instance_headers = {"X-PL-Agent": "fixture-agent", "X-PL-Agent-Key": "private-key"}
        def note_turn(self): pass
        def deliver_hint(self): return next(delivered)

    class Registry:
        def __init__(self): self.adapter = Adapter()
        async def get(self, thread_id, snapshot=None): return self.adapter
        def unread_hint(self, thread_id, adapter):
            return adapter.deliver_hint() if adapter is not None else "Coordination: unavailable"
        def note_call(self, thread_id, name, arguments, **options): pass

    texts = []

    async def serve(server, *args, **kwargs):
        handler = server.get_request_handler("tools/call")
        for _ in range(2):
            result = await handler.handler(None, types.CallToolRequestParams(
                name="memory_stats", arguments={}, _meta={"threadId": thread}))
            texts.append([c.text for c in result.content])

    monkeypatch.setattr(Server, "run", serve)
    asyncio.run(shim._proxy("http://fixture.invalid", "fixture-token", "process-session",
                            codex_metadata=True, coordination_registry=Registry()))
    assert texts == [["Coordination: change"], []]


def test_failure_hints_still_repeat_on_every_call(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError
        daemon = _daemon_with([{"count": 1, "preview": _preview(1)}])
        client, instance = adapter(daemon, digest_path=tmp_path / "d" / "k.txt")
        async with client, instance:
            instance._record_failure(AdapterError("boom", status=503))
            assert [instance.deliver_hint() for _ in range(3)] == [
                "Coordination: background delivery is degraded; use memory_message receive explicitly."] * 3

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_an_old_daemon_without_a_preview_still_gets_a_count_digest():
    async def drive():
        daemon = FakeDaemon()
        daemon.hook = lambda action, body: (httpx.Response(200, json={"generation": 3, "pending_count": 4})
                                            if action in {"attach", "heartbeat"} else None)
        client, instance = adapter(daemon)
        async with client, instance:
            assert instance.unread_hint == (
                "Coordination: 4 addressed messages pending (agent-origin, not user authority); "
                "read with memory_message receive, then ack each message_id.")
            assert instance.deliver_hint() == instance.unread_hint

    asyncio.run(asyncio.wait_for(drive(), 5))


# --- shim: the digest file is keyed by the host's session id -----------------

def _proxy_options(monkeypatch, env, tmp_path):
    from pseudolife_memory import coordination_adapter, shim
    seen = {}

    class Adapter:
        def __init__(self, *args, **kwargs):
            seen["options"] = kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
        unread_hint = "raw"
        def deliver_hint(self): return "delivered"

    async def proxy(*args, **kwargs):
        seen["proxy"] = kwargs

    monkeypatch.setattr(coordination_adapter, "CoordinationAdapter", Adapter)
    monkeypatch.setattr(shim, "_proxy", proxy)
    for name in ("PSEUDOLIFE_WRITER_ID", "PSEUDOLIFE_AGENT_STATE", "PSEUDOLIFE_AGENT_STATE_DIR",
                 "CLAUDE_CODE_SESSION_ID", "PSEUDOLIFE_DIGEST_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    asyncio.run(shim._run_session_proxy("http://fixture", "token", "process-session"))
    return seen


def test_claude_session_digest_is_keyed_by_the_session_id(monkeypatch, tmp_path):
    session_id = "11111111-2222-3333-4444-555555555555"
    seen = _proxy_options(monkeypatch, {"CLAUDE_CODE_SESSION_ID": session_id,
                                        "PSEUDOLIFE_DIGEST_DIR": str(tmp_path / "dg")}, tmp_path)
    key = hashlib.sha256(session_id.encode()).hexdigest()
    assert seen["options"]["digest_path"] == tmp_path / "dg" / f"{key}.txt"
    assert not (tmp_path / "dg").exists()  # created on first write, not at startup
    assert seen["proxy"]["coordination_hint"]() == "delivered"  # gated, not the raw digest


def test_digest_defaults_under_the_home_directory_and_needs_a_session_id(monkeypatch, tmp_path):
    from pseudolife_memory.shim import default_digest_dir
    monkeypatch.delenv("PSEUDOLIFE_DIGEST_DIR", raising=False)
    assert default_digest_dir() == Path.home() / ".pseudolife-mcp" / "digests"
    seen = _proxy_options(monkeypatch, {"PSEUDOLIFE_DIGEST_DIR": str(tmp_path)}, tmp_path)
    assert seen["options"]["digest_path"] is None


def test_codex_threads_get_one_digest_file_each(monkeypatch, tmp_path):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry
    thread = "0199c3aa-1234-7abc-8def-0123456789ab"
    seen = {}

    class Adapter:
        def __init__(self, *args, **kwargs):
            seen["options"] = kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        unread_hint = None
        def deliver_hint(self): return "delivered"

    async def drive():
        registry = CodexCoordinationRegistry("http://fixture", "token", state_dir=tmp_path / "agents",
                                             digest_dir=tmp_path / "dg", adapter_factory=Adapter)
        instance = await registry.get(thread)
        assert instance is not None
        assert registry.unread_hint(thread, instance) == "delivered"
        await registry.aclose()

    asyncio.run(drive())
    key = hashlib.sha256(thread.encode()).hexdigest()
    assert seen["options"]["digest_path"] == tmp_path / "dg" / f"{key}.txt"


def test_shim_attaches_the_delivered_hint_not_the_raw_one(monkeypatch):
    from pseudolife_memory import shim
    from tests.test_coordination_roster_hygiene import _proxy_fixture
    seen = {"calls": []}
    Server, types = _proxy_fixture(monkeypatch, seen)
    delivered = iter(["Coordination: change", None])

    class Adapter:
        instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
        unread_hint = "Coordination: raw"
        async def validate_snapshot(self, snapshot): pass
        def note_turn(self): pass
        def deliver_hint(self): return next(delivered)

    texts = []

    async def serve(server, *args, **kwargs):
        handler = server.get_request_handler("tools/call")
        for _ in range(2):
            result = await handler.handler(None, types.CallToolRequestParams(name="memory_stats", arguments={}))
            texts.append([c.text for c in result.content])

    monkeypatch.setattr(Server, "run", serve)
    instance = Adapter()
    asyncio.run(shim._proxy("http://fixture.invalid", "fixture-token", "process-session",
                            coordination_adapter=instance, coordination_hint=instance.deliver_hint))
    assert texts == [["Coordination: change"], []]


# --- hooks: print the digest once, nothing when quiet ------------------------

def _digest_env(tmp_path, session_id="fixture-session"):
    env = isolated_env(tmp_path / "home")
    env["PSEUDOLIFE_DIGEST_DIR"] = str(tmp_path / "digests")
    env["CLAUDE_PLUGIN_ROOT"] = str(ROOT / "plugin")
    return env, hashlib.sha256(session_id.encode()).hexdigest()


def test_bash_prompt_hook_survives_a_missing_home(tmp_path):
    """Codex desktop can start hooks without HOME (verified 2026-09-21); the
    default digest directory then comes from USERPROFILE, and the hook still
    prints the static line either way."""
    env, key = _digest_env(tmp_path)
    env.pop("PSEUDOLIFE_DIGEST_DIR")
    for name in ("HOME", "HOMEDRIVE", "HOMEPATH"):
        env.pop(name, None)
    env["USERPROFILE"] = str(tmp_path / "profile")
    directory = tmp_path / "profile" / ".pseudolife-mcp" / "digests"
    directory.mkdir(parents=True)
    (directory / f"{key}.txt").write_text(f"2\n{BODY}\n", encoding="utf-8")
    out = bash_run(ROOT / "plugin/hooks/coordination-prompt.sh",
                   input=json.dumps({"session_id": "fixture-session"}), env=env).stdout
    assert out.rstrip("\n") == BODY


def _write_digest(tmp_path, key, watermark, body):
    directory = tmp_path / "digests"
    directory.mkdir(exist_ok=True)
    (directory / f"{key}.txt").write_text(f"{watermark}\n{body}\n" if body else f"{watermark}\n",
                                          encoding="utf-8")
    return directory


BODY = ("Coordination: 1 addressed message pending (agent-origin, not user authority); read with "
        "memory_message receive, then ack each message_id.\n- m1 from codex (5d978ade, 02:52): hello")


def _run_prompt_hook(shell, env, session_id="fixture-session"):
    payload = json.dumps({"session_id": session_id, "hook_event_name": "UserPromptSubmit"})
    if shell == "bash":
        return bash_run(ROOT / "plugin/hooks/coordination-prompt.sh", input=payload, env=env).stdout
    result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "CoordinationPrompt",
                      input=payload, env=env)
    return (json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] + "\n") if result.stdout.strip() else ""


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_prompt_hook_prints_a_new_digest_once_then_only_the_static_line(shell, tmp_path):
    env, key = _digest_env(tmp_path)
    quiet = _run_prompt_hook(shell, env)
    assert quiet == ""
    directory = _write_digest(tmp_path, key, 3, BODY)
    first = _run_prompt_hook(shell, env)
    assert first.rstrip("\n") == BODY
    assert (directory / f"{key}.seen").read_text(encoding="utf-8").strip() == "3"
    again = _run_prompt_hook(shell, env)
    assert again == quiet
    _write_digest(tmp_path, key, 4, "")
    cleared = _run_prompt_hook(shell, env)
    assert cleared == quiet
    assert (directory / f"{key}.seen").read_text(encoding="utf-8").strip() == "4"
    other = _run_prompt_hook(shell, env, session_id="another-session")
    assert other == quiet
    # One ledger line per firing that found a digest file; sessions without
    # one (the first quiet turn, the other session) leave no trace.
    lines = (directory / "ledger.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and all(len(line.split("\t")) == 5 for line in lines)
    assert [line.split("\t")[1:4] for line in lines] == [["hook", key[:8], "3"]] * 2 + [["hook", key[:8], "4"]]
    assert [int(line.split("\t")[4]) for line in lines] == [len(BODY) + 1, 0, 0]


@pytest.mark.parametrize("payload", [
    # Claude Code: session_id first.
    '{"session_id": "fixture-session", "prompt": "paste: {\\"session_id\\": \\"other-session\\"}"}',
    # Codex serialises fields alphabetically, so the prompt comes first
    # (verified 2026-09-21 on both Codex runtimes).
    '{"cwd": "x", "prompt": "paste: {\\"session_id\\": \\"other-session\\"}", "session_id": "fixture-session"}',
    '{"prompt":"{\\"session_id\\":\\"other-session\\"}","session_id":"fixture-session"}',
    # Escaped backslashes and quotes: JSON escaping means a quote can never
    # directly follow {, , or whitespace inside the prompt string, so the
    # boundary rule cannot be fooled by any user text.
    json.dumps({"prompt": 'x {\\"session_id\\": \\"other-session\\"} , "session_id": "other-session"',
                "session_id": "fixture-session"}),
    json.dumps({"prompt": '{"outer": {"session_id": "other-session"}}', "session_id": "fixture-session"}),
    json.dumps({"prompt": "\\\\", "session_id": "fixture-session", "turn_id": "t"}),
])
def test_bash_prompt_hook_takes_the_top_level_session_id(tmp_path, payload):
    """The prompt payload carries the user's text, which can quote the
    literal "session_id"; only the top-level key counts, whatever the
    field order."""
    env, key = _digest_env(tmp_path)
    _write_digest(tmp_path, key, 3, BODY)
    other = hashlib.sha256(b"other-session").hexdigest()
    _write_digest(tmp_path, other, 3, "Coordination: the other session's mail")
    out = bash_run(ROOT / "plugin/hooks/coordination-prompt.sh", input=payload, env=env).stdout
    assert out.rstrip("\n").endswith(BODY)
    assert not (tmp_path / "digests" / f"{other}.seen").exists()


def test_bash_prompt_hook_rejects_odd_session_id_shapes(tmp_path):
    env, key = _digest_env(tmp_path)
    odd = "fixture session/../x"
    _write_digest(tmp_path, hashlib.sha256(odd.encode()).hexdigest(), 3, BODY)
    out = bash_run(ROOT / "plugin/hooks/coordination-prompt.sh",
                   input=json.dumps({"session_id": odd}), env=env).stdout
    assert out == ""


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_prompt_hook_ignores_a_malformed_digest_and_never_fails(shell, tmp_path):
    env, key = _digest_env(tmp_path)
    directory = _write_digest(tmp_path, key, "not-a-number", BODY)
    assert _run_prompt_hook(shell, env) == ""
    (directory / f"{key}.seen").write_text("garbage")
    _write_digest(tmp_path, key, 2, BODY)
    assert _run_prompt_hook(shell, env).rstrip("\n").endswith(BODY)


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("source,kept", [("compact", False), ("resume", False), ("clear", False),
                                         ("startup", True)])
def test_session_start_on_resume_or_compact_forces_a_fresh_digest(shell, source, kept, tmp_path):
    env, key = _digest_env(tmp_path)
    env["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:9"
    directory = _write_digest(tmp_path, key, 3, BODY)
    seen = directory / f"{key}.seen"
    seen.write_text("3")
    payload = json.dumps({"session_id": "fixture-session", "source": source})
    if shell == "bash":
        bash_run(ROOT / "plugin/hooks/coordination-start.sh", input=payload, env=env)
    else:
        pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "CoordinationStart",
                 input=payload, env=env)
    assert seen.exists() is kept


# --- Claude Code: the digest key survives /clear and in-session /resume -----
#
# Claude Code keeps a stdio MCP server's CLAUDE_CODE_SESSION_ID for the life
# of the process, while hooks get the current session id, which /clear and
# /resume change (code.claude.com/docs/en/env-vars.md, verified 2026-09-23).
# The shim writes its digest under its spawn-time id, so the hooks map every
# later session of the same process back to that key through a record named
# by CLAUDE_PID, which Claude Code exports to hooks but not to MCP servers.

def _sha(session_id):
    return hashlib.sha256(session_id.encode()).hexdigest()


def _plant(path, text):
    """A record or marker as the bash hooks write one: LF line ends, which a
    Windows text-mode write would turn into CRLF (and the hooks reject)."""
    path.write_bytes(text.encode("utf-8"))


def _claude_env(tmp_path, pid=None):
    """A live PID by default (this test process), so the hooks can read the
    process creation identity a /clear or /resume handoff is bound to."""
    pid = str(os.getpid()) if pid is None else pid
    env, _ = _digest_env(tmp_path)
    env["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:9"  # session hooks' curl fails fast
    env["CLAUDE_PID"] = pid
    # A suite run from a Claude Code session inherits that session's id.
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    return env


def _claude_hook(script, env, session_id, **fields):
    """Run one plugin hook the way Claude Code does: the env id matches the
    stdin session_id unless a test overrides it."""
    env = {**env}
    env.setdefault("CLAUDE_CODE_SESSION_ID", session_id)
    payload = json.dumps({"session_id": session_id, **fields})
    return bash_run(ROOT / f"plugin/hooks/{script}", input=payload, env=env).stdout


def test_prompt_hook_follows_the_shim_digest_across_clear(tmp_path):
    env = _claude_env(tmp_path)
    directory = _write_digest(tmp_path, _sha("launch-session"), 3, BODY)
    _claude_hook("coordination-start.sh", env, "launch-session", source="startup")
    first = _claude_hook("coordination-prompt.sh", env, "launch-session")
    assert first.rstrip("\n").endswith(BODY)

    _claude_hook("session-end.sh", env, "launch-session", reason="clear")
    _claude_hook("coordination-start.sh", env, "cleared-session", source="clear")
    # The cleared conversation never saw the pending mail: it prints again.
    after_clear = _claude_hook("coordination-prompt.sh", env, "cleared-session")
    assert after_clear.rstrip("\n").endswith(BODY)
    assert _claude_hook("coordination-prompt.sh", env, "cleared-session") == ""
    # Compaction keeps the session id and the process's record.
    _claude_hook("coordination-start.sh", env, "cleared-session", source="compact")
    after_compact = _claude_hook("coordination-prompt.sh", env, "cleared-session")
    assert after_compact.rstrip("\n").endswith(BODY)

    _claude_hook("session-end.sh", env, "cleared-session", reason="clear")
    _claude_hook("coordination-start.sh", env, "cleared-again", source="clear")
    _claude_hook("coordination-prompt.sh", env, "cleared-again")
    _write_digest(tmp_path, _sha("launch-session"), 4, "Coordination: newer mail")
    newer = _claude_hook("coordination-prompt.sh", env, "cleared-again")
    assert newer.rstrip("\n").endswith("Coordination: newer mail")
    assert (directory / f"{_sha('launch-session')}.seen").read_text(encoding="utf-8").strip() == "4"
    assert not (directory / f"{_sha('cleared-session')}.seen").exists()
    assert not (directory / f"{_sha('cleared-again')}.seen").exists()


@pytest.mark.parametrize("left_behind", [False, True])
def test_first_clear_after_a_hook_upgrade_links_the_running_session(left_behind, tmp_path):
    """A session launched under the previous hooks (or before the plugin was
    enabled) has no record of its own; its first /clear names the shim's key
    from the session that is ending, replacing any record another process
    with this PID left behind."""
    env = _claude_env(tmp_path)
    directory = _write_digest(tmp_path, _sha("launch-session"), 3, BODY)
    if left_behind:
        _write_digest(tmp_path, _sha("dead-launch"), 9, "Coordination: a dead shim's mail")
        _plant(directory / f"claude-{env['CLAUDE_PID']}.host", f"{_sha('dead-launch')}\n{_sha('dead-last')}\n")
    _claude_hook("session-end.sh", env, "launch-session", reason="clear")
    _claude_hook("coordination-start.sh", env, "cleared-session", source="clear")
    assert _claude_hook("coordination-prompt.sh", env, "cleared-session").rstrip("\n").endswith(BODY)


def test_in_session_resume_keeps_the_shim_digest_over_a_leftover_file(tmp_path):
    """/resume inside a running session switches the hook's session id; the
    resumed conversation's own digest, left by the shim of the process that
    last ran it, is stale."""
    env = _claude_env(tmp_path)
    _write_digest(tmp_path, _sha("launch-session"), 3, BODY)
    _write_digest(tmp_path, _sha("older-session"), 9, "Coordination: a dead shim's mail")
    _claude_hook("coordination-start.sh", env, "launch-session", source="startup")
    _claude_hook("coordination-prompt.sh", env, "launch-session")
    _claude_hook("session-end.sh", env, "launch-session", reason="resume")
    _claude_hook("coordination-start.sh", env, "older-session", source="resume")
    resumed = _claude_hook("coordination-prompt.sh", env, "older-session")
    assert resumed.rstrip("\n").endswith(BODY)
    assert "dead shim" not in resumed


def _genuine_resume_handoff(tmp_path, env):
    """This process's launch session, then its SessionEnd for /resume: a
    record and a handoff exactly as the hooks write them."""
    directory = _write_digest(tmp_path, _sha("launch-session"), 9, "Coordination: the launch session's mail")
    _claude_hook("coordination-start.sh", env, "launch-session", source="startup")
    _claude_hook("session-end.sh", env, "launch-session", reason="resume")
    return directory, directory / f"claude-{env['CLAUDE_PID']}.switch"


@pytest.mark.parametrize("line,value", [
    (0, "stale"),       # older than a minute
    (0, "future"),      # dated ahead of the clock
    (0, "08"),          # not a date +%s stamp; bash would abort on it as octal
    (1, "elsewhere"),   # written by another process with this PID (Codex review, 2026-09-24)
    (2, "elsewhere"),   # handed off from a session the record is not confirmed for
])
def test_resume_carries_the_record_only_on_this_process_handoff(line, value, tmp_path):
    env = _claude_env(tmp_path)
    directory, switch = _genuine_resume_handoff(tmp_path, env)
    lines = switch.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    lines[line] = {"stale": "1000000000", "future": str(int(time.time()) + 3600), "08": "08",
                   "elsewhere": _sha("elsewhere")}[value]
    _plant(switch, "\n".join(lines) + "\n")
    _write_digest(tmp_path, _sha("resumed-session"), 3, BODY)
    # No daemon answers here, so the gated check-in adds nothing; the record
    # handling below is what this test is about.
    assert _claude_hook("coordination-start.sh", env, "resumed-session", source="resume") == ""
    resumed = _claude_hook("coordination-prompt.sh", env, "resumed-session")
    assert resumed.rstrip("\n").endswith(BODY)
    assert "launch session" not in resumed
    assert not switch.exists()


@pytest.mark.parametrize("source", ["startup", "fork"])
def test_a_launch_never_carries_a_record(source, tmp_path):
    env = _claude_env(tmp_path)
    _, switch = _genuine_resume_handoff(tmp_path, env)
    _write_digest(tmp_path, _sha("new-session"), 3, BODY)
    _claude_hook("coordination-start.sh", env, "new-session", source=source)
    fresh = _claude_hook("coordination-prompt.sh", env, "new-session")
    assert fresh.rstrip("\n").endswith(BODY)
    assert "launch session" not in fresh
    assert not switch.exists()


def test_a_handoff_written_by_another_live_process_is_refused(tmp_path):
    """PID reuse as the creation identity sees it: a record and handoff this
    process wrote, presented under another live process's PID."""
    env = _claude_env(tmp_path)
    directory, switch = _genuine_resume_handoff(tmp_path, env)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        for suffix in ("host", "switch"):
            source = directory / f"claude-{env['CLAUDE_PID']}.{suffix}"
            source.rename(directory / f"claude-{other.pid}.{suffix}")
        env = {**env, "CLAUDE_PID": str(other.pid)}
        _write_digest(tmp_path, _sha("resumed-session"), 3, BODY)
        _claude_hook("coordination-start.sh", env, "resumed-session", source="resume")
        resumed = _claude_hook("coordination-prompt.sh", env, "resumed-session")
    finally:
        other.kill()
        other.wait()
    assert resumed.rstrip("\n").endswith(BODY)
    assert "launch session" not in resumed


def test_without_a_creation_identity_the_record_is_not_carried(tmp_path):
    """Where this process cannot be told apart from a later one with its PID,
    /clear and /resume fail closed: the next session starts from its own key."""
    env = _claude_env(tmp_path, pid="4000000000")  # no such process
    directory = _write_digest(tmp_path, _sha("launch-session"), 3, BODY)
    _claude_hook("coordination-start.sh", env, "launch-session", source="startup")
    _claude_hook("session-end.sh", env, "launch-session", reason="clear")
    switch = directory / "claude-4000000000.switch"
    assert not switch.exists()
    # Nor does a handoff naming the empty identity get through.
    _plant(switch, f"{int(time.time())}\n{_sha('')}\n{_sha('launch-session')}\n")
    _claude_hook("coordination-start.sh", env, "cleared-session", source="clear")
    assert _claude_hook("coordination-prompt.sh", env, "cleared-session") == ""


def test_compaction_does_not_adopt_a_record_confirmed_for_another_session(tmp_path):
    env = _claude_env(tmp_path)
    record = tmp_path / "digests" / f"claude-{env['CLAUDE_PID']}.host"
    _write_digest(tmp_path, _sha("other-launch"), 9, "Coordination: another session's mail")
    _plant(record, f"{_sha('other-launch')}\n{_sha('other-session')}\n")
    _write_digest(tmp_path, _sha("this-session"), 3, BODY)
    _claude_hook("coordination-start.sh", env, "this-session", source="compact")
    own = _claude_hook("coordination-prompt.sh", env, "this-session")
    assert own.rstrip("\n").endswith(BODY)
    assert record.read_text(encoding="utf-8").split() == [_sha("this-session")] * 2


def test_a_resume_handoff_left_by_a_dead_process_with_the_same_pid_is_not_trusted(tmp_path):
    """Codex review of #351 (2026-09-24): a process that wrote its /resume
    handoff and exited, then a new process that reused the PID and launched
    with --resume within the minute, kept the dead process's record and
    printed its mail. Files as the bash hooks write them (LF)."""
    env = _claude_env(tmp_path)
    pid = env["CLAUDE_PID"]
    directory = _write_digest(tmp_path, _sha("dead-launch"), 9, "Coordination: a dead shim's mail")
    _plant(directory / f"claude-{pid}.host", f"{_sha('dead-launch')}\n{_sha('dead-last')}\n")
    _plant(directory / f"claude-{pid}.switch",
           f"{int(time.time())}\n{_sha('another process')}\n{_sha('dead-last')}\n")
    _write_digest(tmp_path, _sha("new-session"), 3, BODY)
    _claude_hook("coordination-start.sh", env, "new-session", source="resume")
    fresh = _claude_hook("coordination-prompt.sh", env, "new-session")
    assert fresh.rstrip("\n").endswith(BODY)
    assert "dead shim" not in fresh


@pytest.mark.parametrize("pid", ["12x", "4242 "])
def test_a_malformed_claude_pid_names_no_record(pid, tmp_path):
    env = _claude_env(tmp_path, pid=pid)
    directory = _write_digest(tmp_path, _sha("other-session"), 3, "Coordination: another process's mail")
    record = directory / f"claude-{pid}.host"
    _write_digest(tmp_path, _sha("launch-session"), 3, BODY)
    # Confirmed for this session: only the digits check keeps the prompt
    # hook from following it.
    _plant(record, f"{_sha('other-session')}\n{_sha('launch-session')}\n")
    own = _claude_hook("coordination-prompt.sh", env, "launch-session")
    assert own.rstrip("\n").endswith(BODY)
    # Confirmed for no session here: only the digits check keeps the session
    # hooks from rewriting it.
    unconfirmed = f"{_sha('other-session')}\n{_sha('elsewhere')}\n"
    _plant(record, unconfirmed)
    _claude_hook("coordination-start.sh", env, "launch-session", source="startup")
    _claude_hook("session-end.sh", env, "launch-session", reason="resume")
    assert not (directory / f"claude-{pid}.switch").exists()
    assert record.read_text(encoding="utf-8") == unconfirmed


def test_prompt_hook_ignores_a_symlinked_process_record(tmp_path):
    env = _claude_env(tmp_path)
    directory = _write_digest(tmp_path, _sha("other-session"), 3, "Coordination: another process's mail")
    target = tmp_path / "elsewhere.host"
    _plant(target, f"{_sha('other-session')}\n{_sha('launch-session')}\n")
    try:
        (directory / f"claude-{env['CLAUDE_PID']}.host").symlink_to(target)
    except OSError:
        pytest.skip("this platform or account cannot create symlinks")
    _write_digest(tmp_path, _sha("launch-session"), 3, BODY)
    assert _claude_hook("coordination-prompt.sh", env, "launch-session").rstrip("\n").endswith(BODY)


def test_a_nested_host_that_inherited_claude_pid_neither_writes_nor_follows_it(tmp_path):
    """A Codex run started from a Claude Bash command inherits CLAUDE_PID and
    CLAUDE_CODE_SESSION_ID from the outer session; its own session_id differs,
    so the outer session's record is left alone and not followed."""
    env = _claude_env(tmp_path)
    env["CLAUDE_CODE_SESSION_ID"] = "outer-session"
    record = tmp_path / "digests" / f"claude-{env['CLAUDE_PID']}.host"
    directory = _write_digest(tmp_path, _sha("outer-session"), 3, "Coordination: the outer session's mail")
    _claude_hook("session-end.sh", env, "codex-thread", reason="clear")
    _claude_hook("session-end.sh", env, "codex-thread", reason="resume")
    assert not record.exists()
    assert not (directory / f"claude-{env['CLAUDE_PID']}.switch").exists()
    # Confirmed for the nested session's id, so only the env==stdin guard
    # keeps it from being followed.
    planted = f"{_sha('outer-session')}\n{_sha('codex-thread')}\n"
    _plant(record, planted)
    _write_digest(tmp_path, _sha("codex-thread"), 3, BODY)
    _claude_hook("coordination-start.sh", env, "codex-thread", source="startup")
    own = _claude_hook("coordination-prompt.sh", env, "codex-thread")
    assert own.rstrip("\n").endswith(BODY)
    assert record.read_text(encoding="utf-8") == planted


@pytest.mark.parametrize("record", [
    "../../escape\n{own}\n", "\n{own}\n", "ABCDEF" * 11 + "\n{own}\n", "abc\n{own}\n",
    "../" + "a" * 61 + "\n{own}\n",  # 64 characters, not all hex
    # Well formed, but confirmed for another session: the state a record
    # left by a dead process with this PID is in until a hook replaces it.
    "{other}\n{elsewhere}\n",
])
def test_prompt_hook_ignores_a_malformed_or_unconfirmed_process_record(record, tmp_path):
    env = _claude_env(tmp_path)
    directory = _write_digest(tmp_path, _sha("launch-session"), 3, BODY)
    _write_digest(tmp_path, _sha("other"), 9, "Coordination: another session's mail")
    _plant(directory / f"claude-{env['CLAUDE_PID']}.host",
           record.format(own=_sha("launch-session"), other=_sha("other"), elsewhere=_sha("elsewhere")))
    assert _claude_hook("coordination-prompt.sh", env, "launch-session").rstrip("\n").endswith(BODY)


def test_session_start_sweeps_only_month_old_process_records(tmp_path):
    env = _claude_env(tmp_path)
    directory = _write_digest(tmp_path, _sha("launch-session"), 3, BODY)
    stale_key = _sha("stale-session")
    _write_digest(tmp_path, stale_key, 1, "Coordination: old")
    swept = [directory / name for name in (
        "claude-1.host", "claude-1.switch", "claude-1.host.777", "claude-1.switch.778")]
    kept = [directory / name for name in (
        "claude-2.host", f"{stale_key}.txt", f"{stale_key}.seen", "ledger.log")]
    for path in swept + kept[:1] + kept[2:]:
        _plant(path, _sha("x") + "\n")
    month_ago, ten_days_ago = time.time() - 40 * 86400, time.time() - 10 * 86400
    for path in swept + kept[1:]:
        os.utime(path, (month_ago, month_ago))
    os.utime(kept[0], (ten_days_ago, ten_days_ago))
    _claude_hook("coordination-start.sh", env, "launch-session", source="startup")
    assert [path.name for path in swept if path.exists()] == []
    assert [path.name for path in kept if not path.exists()] == []
    assert (directory / f"claude-{env['CLAUDE_PID']}.host").exists()


def test_both_hook_scripts_render_the_same_digest(tmp_path):
    if not shutil.which("pwsh"):
        pytest.skip("PowerShell 7 is not installed")
    env, key = _digest_env(tmp_path)
    _write_digest(tmp_path, key, 5, BODY)
    from_bash = _run_prompt_hook("bash", env)
    (tmp_path / "digests" / f"{key}.seen").unlink()
    from_pwsh = _run_prompt_hook("powershell", env)
    assert from_bash == from_pwsh


# --- The startup check-in is served only where the board works ---------------
#
# A check-in the agent cannot complete (coordination off on the daemon, no
# bearer, an unlisted principal, or an explicit client opt-out) is a failed
# tool call on every session start. The daemon serves the text per bearer
# (GET /api/hook/coordination-start); the hooks print whatever it returns.

CHECKIN = "Pseudolife coordination: fixture check-in served by the daemon."


class _CheckinDaemon:
    """Serves the check-in only to the expected bearer, like the daemon."""

    def __init__(self, body=CHECKIN):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        self.requests = []
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                daemon.requests.append((self.path, self.headers.get("Authorization")))
                served = (body if self.path == "/api/hook/coordination-start"
                          and self.headers.get("Authorization") == "Bearer fixture-token"
                          else "")
                self.send_response(200)
                self.end_headers()
                self.wfile.write((served + "\n" if served else "").encode("utf-8"))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.worker = Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)


def _run_start_hook(shell, env, source="startup", session_id="fixture-session"):
    payload = json.dumps({"session_id": session_id, "source": source})
    if shell == "bash":
        return bash_run(ROOT / "plugin/hooks/coordination-start.sh", input=payload, env=env).stdout
    result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "CoordinationStart",
                      input=payload, env=env)
    if not result.stdout.strip():
        return ""
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    return output["additionalContext"] + "\n"


@pytest.fixture
def checkin_daemon():
    daemon = _CheckinDaemon()
    try:
        yield daemon
    finally:
        daemon.close()


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("source", ["startup", "resume", "compact", "clear"])
def test_start_hook_prints_the_checkin_the_daemon_serves(shell, source, checkin_daemon, tmp_path):
    env, _ = _digest_env(tmp_path)
    env["PSEUDOLIFE_MCP_DAEMON_URL"] = checkin_daemon.url
    env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-token"
    env.pop("PSEUDOLIFE_AGENT_COORDINATION", None)
    assert _run_start_hook(shell, env, source) == CHECKIN + "\n"
    assert checkin_daemon.requests == [("/api/hook/coordination-start", "Bearer fixture-token")]


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_start_hook_prints_nothing_when_the_board_is_unavailable(shell, checkin_daemon, tmp_path):
    """No bearer here, so the daemon serves an empty body: no check-in."""
    env, _ = _digest_env(tmp_path)
    env["PSEUDOLIFE_MCP_DAEMON_URL"] = checkin_daemon.url
    env.pop("PSEUDOLIFE_AGENT_COORDINATION", None)
    assert _run_start_hook(shell, env) == ""
    assert checkin_daemon.requests == [("/api/hook/coordination-start", None)]


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_start_hook_prints_nothing_when_the_daemon_does_not_answer(shell, tmp_path):
    env, _ = _digest_env(tmp_path)
    env["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:9"
    env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-token"
    env.pop("PSEUDOLIFE_AGENT_COORDINATION", None)
    started = time.monotonic()
    assert _run_start_hook(shell, env) == ""
    assert time.monotonic() - started < 5  # the hook's budget in hooks.json


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("value", ["0", "off", "False"])
def test_start_hook_honours_an_explicit_client_opt_out(shell, value, checkin_daemon, tmp_path):
    env, key = _digest_env(tmp_path)
    env["PSEUDOLIFE_MCP_DAEMON_URL"] = checkin_daemon.url
    env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-token"
    env["PSEUDOLIFE_AGENT_COORDINATION"] = value
    directory = _write_digest(tmp_path, key, 3, BODY)
    (directory / f"{key}.seen").write_text("3")
    assert _run_start_hook(shell, env, "resume") == ""
    assert checkin_daemon.requests == []
    # The digest bookkeeping still runs.
    assert not (directory / f"{key}.seen").exists()
