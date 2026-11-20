"""Readiness clients and harness imports need no product Python package."""
import importlib
import io
import sys
import urllib.error

import pytest

from rust.harness_support.import_blocker import cold_harness_imports


@pytest.mark.parametrize("module", [
    "pgdisposable",
    "daemons",
    "dbstate",
    "board.run",
    "run",
    "rust.cli_harness.pg_create",
    "rust.cli_harness.run_names",
])
def test_harness_imports_without_product_package(module, monkeypatch):
    """Evict cached harness/package modules so transitive imports cannot hide."""
    original = sys.modules.get(module)
    # Warm the cache inside an outer scope so run.py's path changes and the
    # modules it imports are restored together when this test runs alone.
    with cold_harness_imports():
        importlib.import_module(module)
        with cold_harness_imports():
            assert module not in sys.modules
            loaded = importlib.import_module(module)
            if module == "dbstate":
                monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pseudolife_memory")
                monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
                monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
                with pytest.raises(RuntimeError, match="production bank"):
                    loaded._base_hashes("dbname=unused")
            if module == "run":
                scenario = loaded.SCENARIOS["mcp-dream-rollback"]
                assert scenario.__module__ == "run_scenarios.mcp_dream_rollback"
    assert sys.modules.get(module) is original


def test_cold_import_blocker_rejects_product_and_evicts_cached_fixture():
    with cold_harness_imports():
        importlib.import_module("graph_fixtures")
        with cold_harness_imports():
            assert "graph_fixtures" not in sys.modules
            with pytest.raises(AssertionError, match="unexpected product package import: pseudolife_memory"):
                importlib.import_module("pseudolife_memory")


@pytest.mark.parametrize("module", [
    "rust.harness_support",
    "rust.daemon.harness",
    "rust.daemon.harness.dream_fixture_data",
])
def test_cold_import_restores_namespace_binding(module):
    parent, _, child = module.rpartition(".")
    original = importlib.import_module(module)
    with cold_harness_imports():
        assert module not in sys.modules
        assert not hasattr(importlib.import_module(parent), child)
        fresh = importlib.import_module(module)
        assert fresh is not original
        assert getattr(importlib.import_module(parent), child) is fresh
    assert getattr(importlib.import_module(parent), child) is sys.modules[module] is original


@pytest.mark.parametrize("status,body,want", [
    (200, {"ready": True}, True), (503, {"ready": True}, False),
    (200, {"ready": False}, False), (200, {"ready": 1}, False),
    (200, {}, False), (200, None, False), (200, [], False),
])
def test_ready_requires_success_and_true(status, body, want):
    from rust.harness_support import readiness
    assert readiness.is_ready(status, body) is want


@pytest.mark.parametrize("body,stages,want", [
    (None, (), True), ({}, ("init",), False),
    ({"pending": "init"}, ("init",), False),
    ({"pending": ["init"]}, ("init",), False),
    ({"pending": ["principals"]}, ("init",), True),
    ({"pending": ["principals"]}, ("init", "principals"), False),
    ({"pending": []}, ("init", "principals"), True),
])
def test_stage_completion_is_distinct_from_readiness(body, stages, want):
    from rust.harness_support import readiness
    assert readiness.stages_done(body, iter(stages)) is want


@pytest.mark.parametrize("status,raw,want", [
    (200, b'{"ready": true}', {"ready": True}),
    (503, b'{"pending": ["init"]}', {"pending": ["init"]}),
    (200, b'not json', None), (503, b'\xff', None),
])
def test_read_health_bypasses_proxies_and_preserves_http_errors(status, raw, want, monkeypatch):
    from rust.harness_support import readiness
    seen = []

    class Response(io.BytesIO):
        pass

    class Opener:
        def open(self, url, timeout):
            seen.append((url, timeout))
            if status != 200:
                raise urllib.error.HTTPError(url, status, "unavailable", {}, io.BytesIO(raw))
            response = Response(raw)
            response.status = status
            return response

    def build(handler):
        assert handler.proxies == {}
        return Opener()

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:1")
    monkeypatch.setattr(readiness.urllib.request, "build_opener", build)
    assert readiness.read_health("http://127.0.0.1:1234/", 2) == (status, want)
    assert seen == [("http://127.0.0.1:1234/health", 2)]


def test_transport_failure_propagates_to_the_harness(monkeypatch):
    from rust.harness_support import readiness

    class Opener:
        def open(self, *args, **kwargs):
            raise OSError("no listener")

    monkeypatch.setattr(readiness.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(OSError, match="no listener"):
        readiness.read_health("http://127.0.0.1:1", 2)


def test_timeout_diagnostics_distinguish_unanswered_malformed_and_pending():
    from rust.harness_support import readiness
    assert readiness.describe(None, None) == "no HTTP answer"
    assert readiness.describe(503, None) == "HTTP 503 without a JSON object"
    assert "no 'ready' key" in readiness.describe(200, {})
    pending = readiness.describe(200, {"status": "ok", "ready": False,
                                       "pending": ["init"], "not_ready": "loading"})
    assert "ready False" in pending and "pending ['init']" in pending and "loading" in pending
