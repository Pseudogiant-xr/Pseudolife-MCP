"""The synthetic oracle invokes production composition through its service seam."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def oracle():
    specification = importlib.util.spec_from_file_location("oracle_under_test", Path(__file__).with_name("oracle.py"))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def boundary(monkeypatch, *, use_service=True):
    import pseudolife_memory
    calls = []
    service = SimpleNamespace(MemoryService=object)
    daemon = SimpleNamespace(_build_health_payload=lambda *args: None)

    def console_builder(mcp_app, token, health, selected_service, **kwargs):
        calls.append(("console", mcp_app, token, health(), selected_service, kwargs))
        return "real-composed-app"

    web = SimpleNamespace(build_console_app=console_builder)

    def run_daemon(**kwargs):
        calls.append(("daemon", kwargs))
        selected = service.MemoryService() if use_service else object()
        if use_service:
            assert selected.config.memory.dream.enabled is False
            assert selected.config.memory.retrieval_log.enabled is False
            assert selected.config.updates.check_releases is False
            assert selected.warmup() is None
            assert selected.autosave_if_changed() is None
            assert selected.reap_idle_sessions(100) is None
            assert selected.search("memory", top_k=1)["count"] == 1
        assert daemon._build_health_payload(selected, False) == {"storage": "fixture"}
        app = web.build_console_app("production-mcp-app", None, lambda: None, selected, token_map={})
        uvicorn.run(app, host="127.0.0.1", port=1234)

    daemon.run_daemon = run_daemon

    class Server:
        def __init__(self, config):
            calls.append(("server", config))

        def run(self, *, sockets):
            calls.append(("sockets", sockets))

    uvicorn = SimpleNamespace(Config=lambda app, **kwargs: (app, kwargs), Server=Server,
                              run=lambda *args, **kwargs: pytest.fail("unowned bind"))
    for name, module in (("daemon", daemon), ("service", service), ("web", web)):
        monkeypatch.setattr(pseudolife_memory, name, module, raising=False)
        monkeypatch.setitem(sys.modules, "pseudolife_memory." + name, module)
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    return calls, service, daemon, web, uvicorn


def test_oracle_delegates_composition_and_retains_owned_socket(oracle, monkeypatch):
    calls, service, daemon, web, uvicorn = boundary(monkeypatch)
    originals = (service.MemoryService, daemon._build_health_payload, web.build_console_app, uvicorn.run)
    sock = SimpleNamespace(getsockname=lambda: ("127.0.0.1", 1234))
    oracle.run_fixture(sock)
    assert calls[0] == ("daemon", {"host": "127.0.0.1", "port": 1234})
    assert calls[1][:4] == ("console", "production-mcp-app", None, {"status": "ok", "fixture": True})
    assert calls[1][5] == {"token_map": {}}
    assert calls[2] == ("server", ("real-composed-app", {"log_level": "error", "access_log": False}))
    assert calls[3] == ("sockets", [sock])
    assert (service.MemoryService, daemon._build_health_payload, web.build_console_app, uvicorn.run) == originals


def test_oracle_refuses_entrypoint_ignoring_synthetic_service(oracle, monkeypatch):
    calls, service, daemon, web, uvicorn = boundary(monkeypatch, use_service=False)
    originals = (service.MemoryService, daemon._build_health_payload, web.build_console_app, uvicorn.run)
    sock = SimpleNamespace(getsockname=lambda: ("127.0.0.1", 1234))
    with pytest.raises(RuntimeError, match="did not use the synthetic service"):
        oracle.run_fixture(sock)
    assert [item[0] for item in calls] == ["daemon"]
    assert (service.MemoryService, daemon._build_health_payload, web.build_console_app, uvicorn.run) == originals
