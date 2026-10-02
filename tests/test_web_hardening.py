"""REST identity, bounded bodies, and serialized Console configuration writes."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest
import yaml

from pseudolife_memory import writer_context as wc
from pseudolife_memory.service import MemoryService
from pseudolife_memory.web import config_io
from pseudolife_memory.web.api import build_console_app
from pseudolife_memory.web.fixtures import FixtureService
from tests.asgi_helpers import call, stub_mcp


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
    service = FixtureService()
    service.data_dir = tmp_path
    return service


def app_for(svc, token_map=None):
    return build_console_app(stub_mcp, None, lambda: {"status": "ok"},
                             svc, token_map=token_map)


def test_rest_fact_writer_matches_mcp_identity(svc, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "tok-a:alice,tok-b:bob")
    svc.cortex_write = lambda *a, **k: {"identity": wc.resolve_writer("daemon-default")}
    headers = {"authorization": "Bearer tok-a", "x-pl-writer": "spoofed",
               "x-pl-session": "session-a"}
    # MCP's transport binder carries these same headers into its worker.
    binding = wc.bind_request_headers(headers)
    try:
        expected = wc.resolve_writer("daemon-default")
    finally:
        wc.unbind_request_headers(binding)
    status, raw = call(app_for(svc, {"tok-a": "alice"}), "POST", "/api/facts/set",
                       headers=[(k.encode(), v.encode()) for k, v in headers.items()]
                       + [(b"content-type", b"application/json")],
                       body=b'{"entity":"project","attribute":"state","value":"ready"}')
    assert status == 200
    assert json.loads(raw)["identity"] == list(expected) == ["alice", "session-a"]


def test_rest_uses_validated_map_and_resets_worker_after_failure(svc, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "tok-a:wrong-env-principal")
    seen = []

    def write(*a, **k):
        seen.append((wc.resolve_writer("daemon-default"), wc.current_principal()))
        if len(seen) == 1:
            raise ValueError("synthetic failure")
        return {"ok": True}

    svc.cortex_write = write
    app = app_for(svc, {"tok-a": "alice", "tok-b": "bob"})

    async def run():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            loop.set_default_executor(pool)
            statuses = []
            for bearer, session in [("tok-a", "a"), ("tok-b", "b")]:
                out = []

                async def receive():
                    return {"type": "http.request", "body":
                            b'{"entity":"p","attribute":"a","value":"v"}'}

                async def send(message):
                    out.append(message)

                await app({"type": "http", "method": "POST", "path": "/api/facts/set",
                           "headers": [(b"authorization", f"Bearer {bearer}".encode()),
                                       (b"x-pl-session", session.encode()),
                                       (b"content-type", b"application/json")]}, receive, send)
                statuses.append(out[0]["status"])
                assert await loop.run_in_executor(pool, wc._http_request_headers) is None
                assert await loop.run_in_executor(pool, wc.request_principal) is None
            return statuses

    assert asyncio.run(run()) == [400, 200]
    assert seen == [(("alice", "a"), "alice"), (("bob", "b"), "bob")]


def test_rest_parallel_requests_keep_separate_identities(svc):
    barrier = threading.Barrier(2)

    def write(*a, **k):
        before = wc.resolve_writer("daemon-default")
        barrier.wait(timeout=5)
        assert wc.resolve_writer("daemon-default") == before
        return {"identity": before}

    svc.cortex_write = write
    app = app_for(svc, {"tok-a": "alice", "tok-b": "bob"})

    def request(token, session):
        status, raw = call(app, "POST", "/api/facts/set",
                           headers=[(b"authorization", f"Bearer {token}".encode()),
                                    (b"x-pl-session", session.encode()),
                                    (b"content-type", b"application/json")],
                           body=b'{"entity":"p","attribute":"a","value":"v"}')
        assert status == 200
        return json.loads(raw)["identity"]

    with ThreadPoolExecutor(2) as pool:
        alice = pool.submit(request, "tok-a", "a")
        bob = pool.submit(request, "tok-b", "b")
        assert alice.result(5) == ["alice", "a"]
        assert bob.result(5) == ["bob", "b"]


def test_rest_default_principal_keeps_legacy_writer_header(svc, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "single:wrong-env-principal")
    svc.cortex_write = lambda *a, **k: {"identity": wc.resolve_writer("daemon-default")}
    app = build_console_app(stub_mcp, "single", lambda: {}, svc)
    status, raw = call(app, "POST", "/api/facts/set",
                       headers=[(b"authorization", b"Bearer single"),
                                (b"x-pl-writer", b"legacy-writer"),
                                (b"x-pl-session", b"legacy-session"),
                                (b"content-type", b"application/json")],
                       body=b'{"entity":"p","attribute":"a","value":"v"}')
    assert status == 200
    assert json.loads(raw)["identity"] == ["legacy-writer", "legacy-session"]


@pytest.mark.parametrize("path", ["/api/facts/set", "/api/hook/session-start"])
def test_rest_keeps_authenticated_identity_for_utf8_token(svc, monkeypatch, path):
    from pseudolife_memory.web import api
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    svc.cortex_write = lambda *a, **k: {"identity": wc.resolve_writer("daemon-default")}
    monkeypatch.setattr(api, "hook_session_start", lambda *a, **k:
                        json.dumps({"identity": wc.resolve_writer("daemon-default")}))
    app = app_for(svc, {"tök": "alice"})
    headers = [(b"authorization", "Bearer tök".encode("utf-8")),
               (b"x-pl-writer", b"spoofed"), (b"x-pl-session", b"a"),
               (b"content-type", b"application/json")]
    method = "GET" if path.endswith("session-start") else "POST"
    status, raw = call(app, method, path, headers=headers,
                       body=b'{"entity":"p","attribute":"a","value":"v"}')
    assert status == 200
    assert json.loads(raw)["identity"] == ["alice", "a"]


def test_active_session_clear_is_atomic_at_lock_release():
    # Start the new session at the exact ownership-check lock release. The old
    # implementation delegated an unconditional clear after this point.
    svc = object.__new__(MemoryService)
    svc._ensure_init = lambda: None
    svc._active_session = ("old", 1.0)
    from unittest.mock import Mock
    svc._storage = Mock()
    lock = threading.Lock()

    class InterleavingLock:
        fired = False

        def __enter__(self):
            lock.acquire()

        def __exit__(self, *args):
            lock.release()
            if not self.fired:
                self.fired = True
                svc.set_active_session("new")

    svc._lock = InterleavingLock()
    assert svc.clear_active_session("old") is True
    assert svc._active_session[0] == "new"
    assert svc._storage.set_meta.call_args.args[1]["session_id"] == "new"
    assert svc.clear_active_session("old") is False


@pytest.mark.parametrize("bounded", [True, False])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"),
                                  "NaN", "Infinity", "-Infinity"])
def test_config_rejects_nonfinite_without_persisting(svc, monkeypatch, bounded, value):
    # Existing floats all have maxima; also pin coercion without that ceiling.
    path = "memory.surprise_threshold"
    if not bounded:
        knob = dict(config_io._KNOB_BY_PATH[path])
        knob.pop("max")
        monkeypatch.setitem(config_io._KNOB_BY_PATH, path, knob)
    with pytest.raises(ValueError, match="finite"):
        config_io.write_config(svc, {path: value})
    assert not config_io.config_path_for(svc).exists()


def _knob(cfg: dict, path: str) -> dict:
    return next(k for g in cfg["groups"] for k in g["knobs"] if k["path"] == path)


def test_config_reports_the_saved_value_of_a_restart_knob(svc):
    """A restart knob is written to config.yaml but not applied, so the live
    value alone hides what the next boot will run (a mistaken save could not
    even be undone: typing the old value back looked like no change). The
    read reports the persisted value beside the live one when they differ."""
    path = "memory.reranker.fusion_weight"
    assert config_io._KNOB_BY_PATH[path]["restart"] is True
    live = _knob(config_io.read_config(svc), path)["value"]
    assert "saved" not in _knob(config_io.read_config(svc), path)

    config_io.write_config(svc, {path: 0.4})
    after = _knob(config_io.read_config(svc), path)
    assert after["value"] == live            # still the running value
    assert after["saved"] == 0.4             # what the next start reads

    config_io.write_config(svc, {path: live})  # the undo is a real edit
    assert "saved" not in _knob(config_io.read_config(svc), path)


def test_config_saved_value_ignores_live_knobs_and_a_broken_file(svc):
    path = "memory.top_k"
    assert not config_io._KNOB_BY_PATH[path]["restart"]
    config_io.write_config(svc, {path: 11})
    assert "saved" not in _knob(config_io.read_config(svc), path)
    config_io.config_path_for(svc).write_text("memory: [unclosed", encoding="utf-8")
    cfg = config_io.read_config(svc)          # still answers
    assert all("saved" not in k for g in cfg["groups"] for k in g["knobs"])


def test_config_concurrent_patches_preserve_both_updates(svc, monkeypatch):
    cfg = config_io.config_path_for(svc)
    cfg.write_text("unmanaged: keep\nmemory:\n  top_k: 8\n", encoding="utf-8")
    first_read, release, second_read, second_started = (threading.Event() for _ in range(4))
    load = config_io.yaml.safe_load

    def controlled_load(stream):
        data = load(stream)
        if threading.current_thread().name.startswith("first"):
            first_read.set()
            assert release.wait(5)
        else:
            second_read.set()
        return data

    monkeypatch.setattr(config_io.yaml, "safe_load", controlled_load)

    def second_write():
        second_started.set()
        return config_io.write_config(svc, {"memory.surprise_threshold": 0.2})

    with ThreadPoolExecutor(1, thread_name_prefix="first") as one, \
            ThreadPoolExecutor(1, thread_name_prefix="second") as two:
        first = one.submit(config_io.write_config, svc, {"memory.top_k": 9})
        assert first_read.wait(5)
        second = two.submit(second_write)
        try:
            assert second_started.wait(5)
            assert not second_read.wait(0.3), "second writer read a stale configuration"
        finally:
            release.set()
        first.result(5)
        second.result(5)
    saved = load(cfg.read_text(encoding="utf-8"))
    assert saved == {"unmanaged": "keep", "memory": {"top_k": 9, "surprise_threshold": 0.2}}
    assert (svc.config.memory.top_k, svc.config.memory.surprise_threshold) == (9, 0.2)


def test_config_serializes_runtime_publication(svc, monkeypatch):
    paused, release, second_started, second_done = (threading.Event() for _ in range(4))
    publish = config_io._set_by_path

    def controlled_publish(obj, path, value):
        if value == 9:
            paused.set()
            assert release.wait(5)
        publish(obj, path, value)

    monkeypatch.setattr(config_io, "_set_by_path", controlled_publish)

    def second_write():
        second_started.set()
        result = config_io.write_config(svc, {"memory.top_k": 10})
        second_done.set()
        return result

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(config_io.write_config, svc, {"memory.top_k": 9})
        assert paused.wait(5)
        second = pool.submit(second_write)
        try:
            assert second_started.wait(5)
            assert not second_done.wait(0.3), "runtime publication escaped serialization"
        finally:
            release.set()
        first.result(5)
        second.result(5)
    saved = yaml.safe_load(config_io.config_path_for(svc).read_text(encoding="utf-8"))
    assert saved["memory"]["top_k"] == svc.config.memory.top_k == 10


def test_config_backups_and_temporary_files_are_unique(svc, monkeypatch):
    monkeypatch.setattr(config_io.time, "strftime", lambda *a: "same-second")
    cfg = config_io.config_path_for(svc)
    legacy_temp = cfg.with_suffix(".yaml.tmp")
    legacy_temp.write_text("another writer", encoding="utf-8")
    config_io.write_config(svc, {"memory.top_k": 8})
    one = config_io.write_config(svc, {"memory.top_k": 9})
    two = config_io.write_config(svc, {"memory.top_k": 10})
    assert one["backup"] != two["backup"]
    from pathlib import Path
    assert yaml.safe_load(Path(one["backup"]).read_text())["memory"]["top_k"] == 8
    assert yaml.safe_load(Path(two["backup"]).read_text())["memory"]["top_k"] == 9
    assert legacy_temp.read_text() == "another writer"


@pytest.mark.parametrize("failure", ["dump", "replace", "backup"])
def test_config_io_failure_keeps_disk_runtime_and_cleans_temp(svc, monkeypatch, failure):
    config_io.write_config(svc, {"memory.top_k": 8})
    cfg = config_io.config_path_for(svc)
    before = cfg.read_bytes()

    def fail(*a, **k):
        raise OSError("synthetic failure")

    target, name = {"dump": (config_io.yaml, "safe_dump"),
                    "replace": (config_io.os, "replace"),
                    "backup": (config_io.shutil, "copy2")}[failure]
    with monkeypatch.context() as patcher:
        patcher.setattr(target, name, fail)
        with pytest.raises(OSError, match="synthetic"):
            config_io.write_config(svc, {"memory.top_k": 9})
    assert cfg.read_bytes() == before
    assert svc.config.memory.top_k == 8
    assert not list(cfg.parent.glob("*.tmp"))
    # A failed operation must release the lock for the next ordinary write.
    config_io.write_config(svc, {"memory.top_k": 10})
    assert svc.config.memory.top_k == 10


@pytest.mark.parametrize("path,limit", [("/api/config", 256 * 1024),
                                       ("/api/facts/set", 4 * 1024 * 1024),
                                       ("/api/consolidate", 4 * 1024 * 1024),
                                       ("/api/supersede", 4 * 1024 * 1024),
                                       ("/api/hook/session-end", 16 * 1024)])
def test_rest_body_limits_accept_boundary_and_reject_next_byte(svc, monkeypatch, path, limit):
    from pseudolife_memory.web.routes import ConsoleRoutes
    monkeypatch.setattr(ConsoleRoutes, "dispatch", lambda *a: {"ok": True})
    svc.episode_end_session = lambda *a, **k: {"ok": True}
    svc.clear_active_session = lambda *a: True
    app = app_for(svc)
    body = b'{"session_id":"' + b'x' * (limit - 17) + b'"}'
    assert len(body) == limit
    headers = [(b"content-type", b"application/json")]
    assert call(app, "POST", path, headers=headers, body=body)[0] == 200
    status, raw = call(app, "POST", path, headers=headers, body=body + b' ')
    assert status == 413 and json.loads(raw)["error"] == "request_too_large"


def test_rest_chunked_body_limit_stops_reading(svc):
    app = app_for(svc)

    async def run():
        messages = iter([{"type": "http.request", "body": b' ' * (200 * 1024), "more_body": True},
                         {"type": "http.request", "body": b' ' * (60 * 1024), "more_body": True}])
        out = []

        async def receive():
            return next(messages)  # A third read fails: reject immediately.

        async def send(message):
            out.append(message)

        await app({"type": "http", "method": "POST", "path": "/api/config",
                   "headers": [(b"content-type", b"application/json"),
                               (b"content-length", b"1")]}, receive, send)
        assert out[0]["status"] == 413

    asyncio.run(run())
