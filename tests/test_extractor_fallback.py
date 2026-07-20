"""Primary/fallback extractor selection — config, resolution, probe, builder.

Spec: docs/superpowers/specs/2026-07-11-sonnet-sidecar-cutover-design.md.
Pure config/HTTP-stub tests — no embedder, no PG."""

from __future__ import annotations

import contextlib
import http.server
import threading

import pytest

from pseudolife_memory.utils.config import DreamConfig


def test_dreamconfig_fallback_defaults_inert():
    c = DreamConfig()
    assert c.fallback_base_url is None
    assert c.fallback_model is None
    assert c.extractor_mode == "auto"


def test_resolve_endpoints_env_mode(monkeypatch):
    from pseudolife_memory.memory.dream import resolve_endpoints
    monkeypatch.setenv("PSEUDOLIFE_DREAM_BASE_URL", "http://p:1/v1")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_MODEL", "pm")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL", "http://f:2/v1")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_FALLBACK_MODEL", "fm")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_EXTRACTOR_MODE", "fallback")
    r = resolve_endpoints(DreamConfig())
    assert r["primary_url"] == "http://p:1/v1" and r["primary_model"] == "pm"
    assert r["fallback_url"] == "http://f:2/v1" and r["fallback_model"] == "fm"
    assert r["mode"] == "fallback"


def test_resolve_endpoints_config_mode_ignores_env(monkeypatch):
    from pseudolife_memory.memory.dream import resolve_endpoints
    monkeypatch.setenv("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL", "http://env:9/v1")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_EXTRACTOR_MODE", "primary")
    cfg = DreamConfig(extractor_source="config",
                      extractor_base_url="http://cp:1/v1", extractor_model="m",
                      fallback_base_url="http://cf:2/v1", fallback_model="m2",
                      extractor_mode="auto")
    r = resolve_endpoints(cfg)
    assert r["fallback_url"] == "http://cf:2/v1"
    assert r["mode"] == "auto"


def test_resolve_endpoints_bad_mode_falls_back_to_auto(monkeypatch):
    from pseudolife_memory.memory.dream import resolve_endpoints
    monkeypatch.setenv("PSEUDOLIFE_DREAM_EXTRACTOR_MODE", "bogus")
    assert resolve_endpoints(DreamConfig())["mode"] == "auto"


# ── probe + builder ───────────────────────────────────────────────────────

class _HealthHandler(http.server.BaseHTTPRequestHandler):
    health_status = 200   # per-subclass override
    models_status = 200

    def do_GET(self):  # noqa: N802
        status = (type(self).health_status if self.path == "/health"
                  else type(self).models_status if self.path.endswith("/models")
                  else 404)
        self.send_response(status)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@contextlib.contextmanager
def _health_server(health_status=200, models_status=200):
    handler = type("H", (_HealthHandler,),
                   {"health_status": health_status,
                    "models_status": models_status})
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    finally:
        srv.shutdown()


def test_probe_health_ok():
    from pseudolife_memory.memory.dream import probe_endpoint
    with _health_server() as base:
        assert probe_endpoint(base) is True


def test_probe_health_503_is_down():
    # A shim whose CLI is logged out answers /health with 503 -> primary down.
    from pseudolife_memory.memory.dream import probe_endpoint
    with _health_server(health_status=503) as base:
        assert probe_endpoint(base) is False


def test_probe_404_health_falls_back_to_models():
    # Plain llama-server: no /health at root, but /v1/models answers.
    from pseudolife_memory.memory.dream import probe_endpoint
    with _health_server(health_status=404, models_status=200) as base:
        assert probe_endpoint(base) is True


def test_probe_unreachable_is_down():
    from pseudolife_memory.memory.dream import probe_endpoint
    assert probe_endpoint("http://127.0.0.1:9/v1", timeout=0.3) is False


def _cfg(base, fb=None, mode="auto"):
    return DreamConfig(extractor_source="config",
                       extractor_base_url=base, extractor_model="m",
                       fallback_base_url=fb, fallback_model="m2",
                       extractor_mode=mode)


def test_builder_no_fallback_skips_probe(monkeypatch):
    from pseudolife_memory.memory import dream as d

    def _boom(*a, **k):
        raise AssertionError("probe must not run when fallback is unset")
    monkeypatch.setattr(d, "probe_endpoint", _boom)
    ext, which = d.build_extractor_with_fallback(_cfg("http://p:1/v1"))
    assert which == "primary" and ext.base_url == "http://p:1/v1"


def test_builder_auto_primary_up(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: True)
    ext, which = d.build_extractor_with_fallback(
        _cfg("http://p:1/v1", fb="http://f:2/v1"))
    assert which == "primary" and ext.base_url == "http://p:1/v1"


def test_builder_auto_primary_down(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: False)
    monkeypatch.setattr(d, "_probe_retry_delay", 0.0)
    ext, which = d.build_extractor_with_fallback(
        _cfg("http://p:1/v1", fb="http://f:2/v1"))
    assert which == "fallback" and ext.base_url == "http://f:2/v1"
    assert ext.model == "m2"


def test_builder_auto_retries_probe_once(monkeypatch):
    # 2026-07-19: the FIRST probe after a daemon container restart reliably
    # fails (host-gateway cold start) while the shim is actually healthy —
    # two consecutive live dreams fell back on a healthy primary. One retry
    # after a short delay rides out the transient instead of silently
    # degrading the dream to the fallback extractor.
    from pseudolife_memory.memory import dream as d
    calls = []
    monkeypatch.setattr(d, "probe_endpoint",
                        lambda *a, **k: bool(calls.append(1)) or len(calls) > 1)
    monkeypatch.setattr(d, "_probe_retry_delay", 0.0)
    ext, which = d.build_extractor_with_fallback(
        _cfg("http://p:1/v1", fb="http://f:2/v1"))
    assert which == "primary" and ext.base_url == "http://p:1/v1"
    assert len(calls) == 2


def test_builder_forced_primary_never_probes(monkeypatch):
    from pseudolife_memory.memory import dream as d

    def _boom(*a, **k):
        raise AssertionError("probe must not run in forced modes")
    monkeypatch.setattr(d, "probe_endpoint", _boom)
    ext, which = d.build_extractor_with_fallback(
        _cfg("http://p:1/v1", fb="http://f:2/v1", mode="primary"))
    assert which == "primary" and ext.base_url == "http://p:1/v1"


def test_builder_forced_fallback(monkeypatch):
    from pseudolife_memory.memory import dream as d
    ext, which = d.build_extractor_with_fallback(
        _cfg("http://p:1/v1", fb="http://f:2/v1", mode="fallback"))
    assert which == "fallback" and ext.base_url == "http://f:2/v1"


def test_builder_forced_fallback_without_url_raises():
    from pseudolife_memory.memory.dream import build_extractor_with_fallback
    with pytest.raises(ValueError, match="fallback"):
        build_extractor_with_fallback(_cfg("http://p:1/v1", mode="fallback"))


def test_builder_unconfigured_is_noop():
    from pseudolife_memory.memory.dream import (NoOpExtractor,
                                                build_extractor_with_fallback)
    ext, which = build_extractor_with_fallback(DreamConfig())
    assert which == "primary" and isinstance(ext, NoOpExtractor)


# ── service.dream_run_auto ────────────────────────────────────────────────

class _FakeService:
    """Only what dream_run_auto touches — avoids embedder/PG bring-up."""
    from pseudolife_memory.service import MemoryService as _MS
    dream_run_auto = _MS.dream_run_auto

    def __init__(self, cfg):
        from types import SimpleNamespace
        self.config = SimpleNamespace(memory=SimpleNamespace(dream=cfg))
        self._last_dream_extractor = None
        self.ran_with = None

    def dream_run(self, extractor, *, limit=None):
        self.ran_with = extractor
        return {"pulled": 0, "claims": 0}


def test_dream_run_auto_records_selection(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: False)
    svc = _FakeService(_cfg("http://p:1/v1", fb="http://f:2/v1"))
    res = svc.dream_run_auto()
    assert res["extractor"] == "fallback"
    assert svc._last_dream_extractor["which"] == "fallback"
    assert svc._last_dream_extractor["base_url"] == "http://f:2/v1"
    assert svc.ran_with.base_url == "http://f:2/v1"


def test_dream_run_auto_surfaces_config_error():
    svc = _FakeService(_cfg("http://p:1/v1", mode="fallback"))
    res = svc.dream_run_auto()
    assert "error" in res and "fallback" in res["error"]
    assert svc.ran_with is None


# ── dream_status extractor fields ─────────────────────────────────────────

def test_dream_status_fields_no_fallback(monkeypatch):
    from pseudolife_memory.memory.dream import _status_extractor_fields
    fields = _status_extractor_fields(_cfg("http://p:1/v1"), None)
    assert fields["extractor_mode"] == "auto"
    assert fields["primary_url"] == "http://p:1/v1"
    assert fields["fallback_url"] is None
    assert fields["primary_healthy"] is None          # no probe when inert
    assert fields["last_dream_extractor"] is None


def test_dream_status_fields_with_fallback(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: True)
    last = {"which": "primary", "base_url": "http://p:1/v1", "at": 123.0}
    fields = d._status_extractor_fields(
        _cfg("http://p:1/v1", fb="http://f:2/v1"), last)
    assert fields["primary_healthy"] is True
    assert fields["last_dream_extractor"] == last


# ── startup misconfig warnings (issues #11/#12) ──────────────────────────

def test_startup_warnings_stock_default_is_clean(monkeypatch):
    # The shipped default (in-stack sidecar primary, no fallback, mode=auto)
    # must not warn — and must not pay a DNS lookup.
    from pseudolife_memory.memory import dream as d

    def _boom(*a, **k):
        raise AssertionError("no DNS lookup for the in-stack sidecar URL")
    monkeypatch.setattr(d, "_host_resolves", _boom)
    assert d.startup_extractor_warnings(DreamConfig()) == []


def test_startup_warns_when_host_docker_internal_unresolvable(monkeypatch):
    # Linux Docker Engine without extra_hosts: every probe fails silently.
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "_host_resolves", lambda h: False)
    warnings = d.startup_extractor_warnings(
        _cfg("http://host.docker.internal:8082/v1", fb="http://f:2/v1"))
    assert any("extra_hosts" in w for w in warnings)


def test_startup_no_dns_warning_when_resolvable(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "_host_resolves", lambda h: True)
    warnings = d.startup_extractor_warnings(
        _cfg("http://host.docker.internal:8082/v1", fb="http://f:2/v1"))
    assert not any("extra_hosts" in w for w in warnings)


def test_startup_warns_auto_host_primary_without_fallback(monkeypatch):
    # Host-side primary + mode=auto + no fallback: auto is inert and a down
    # endpoint means dreams fail — the #11/#12 half-configured cutover.
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "_host_resolves", lambda h: True)
    warnings = d.startup_extractor_warnings(
        _cfg("http://host.docker.internal:8082/v1"))
    assert any("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL" in w for w in warnings)


def test_startup_warns_primary_equals_fallback(monkeypatch):
    # The inverse half-config: fallback set but primary left at the sidecar
    # default — both sides point at the same endpoint, the intended primary
    # is never used.
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "_host_resolves", lambda h: True)
    warnings = d.startup_extractor_warnings(
        _cfg("http://f:2/v1", fb="http://f:2/v1"))
    assert any("same endpoint" in w for w in warnings)


# ── console schema entries ────────────────────────────────────────────────

def test_config_io_has_fallback_knobs():
    from pseudolife_memory.web.config_io import KNOBS
    paths = {e["path"] for e in KNOBS}
    assert "memory.dream.extractor_mode" in paths
    assert "memory.dream.fallback_base_url" in paths
    assert "memory.dream.fallback_model" in paths
    mode = next(e for e in KNOBS if e["path"] == "memory.dream.extractor_mode")
    assert mode["type"] == "enum"
    assert mode["options"] == ["auto", "primary", "fallback"]
