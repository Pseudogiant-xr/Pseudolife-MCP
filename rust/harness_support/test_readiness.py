"""Readiness clients and harness imports need no product Python package."""
import importlib
import importlib.abc
import io
from pathlib import Path
import sys
import urllib.error

import pytest


class BlockPackage(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pseudolife_memory" or fullname.startswith("pseudolife_memory."):
            raise AssertionError(f"unexpected product package import: {fullname}")


@pytest.mark.parametrize("module", ["pgdisposable", "daemons", "dbstate", "board.run"])
def test_harness_imports_without_product_package(module, monkeypatch):
    """Evict cached harness/package modules so transitive imports cannot hide."""
    harness = Path(__file__).resolve().parents[1] / "daemon" / "harness"
    support = Path(__file__).resolve().parent

    def owned(name, value):
        filename = getattr(value, "__file__", None)
        return (name == "pseudolife_memory" or name.startswith("pseudolife_memory.")
                or filename is not None and (
                    Path(filename).resolve().is_relative_to(harness)
                    or Path(filename).resolve().parent == support
                    and not Path(filename).name.startswith("test_")))

    with monkeypatch.context() as patch:
        for name, value in list(sys.modules.items()):
            if owned(name, value):
                patch.delitem(sys.modules, name)
        patch.syspath_prepend(str(harness.parents[2]))
        patch.syspath_prepend(str(harness))
        patch.setattr(sys, "meta_path", [BlockPackage(), *sys.meta_path])
        try:
            loaded = importlib.import_module(module)
            if module == "dbstate":
                patch.setenv("PL_HARNESS_BASE_DATABASE", "pseudolife_memory")
                patch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
                patch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
                with pytest.raises(RuntimeError, match="production bank"):
                    loaded._base_hashes("dbname=unused")
        finally:
            for name, value in list(sys.modules.items()):
                if owned(name, value):
                    del sys.modules[name]


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
