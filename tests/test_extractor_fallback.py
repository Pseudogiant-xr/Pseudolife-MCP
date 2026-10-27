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
    # Shim-shaped /models listing: launch model first, aliases after.
    models_body = (b'{"object":"list","data":[{"id":"claude-opus-5"},'
                   b'{"id":"extractor"},{"id":"bench"}]}')

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            status, body = type(self).models_status, type(self).models_body
        elif self.path == "/health":
            status, body = type(self).health_status, b"{}"
        else:
            status, body = 404, b"{}"
        self.send_response(status)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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


_REFUSED = ("extractor_unreachable", "connection refused")

from pseudolife_memory.memory import dream as _dream  # noqa: E402

# The real lookup, kept before the hermetic fixture below patches it.
_REAL_HOST_RESOLVES = _dream._host_resolves


@pytest.fixture(autouse=True)
def _fresh_resolution_cache(monkeypatch):
    # Hermetic by default: no real DNS lookup of the tests' made-up hosts
    # (a single-label name can take seconds on Windows). Tests about
    # resolution patch _host_resolves themselves.
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "_host_resolves", lambda h: True)
    d._resolves_cache.clear()
    d._fallback_skip_warned.clear()
    yield
    d._resolves_cache.clear()
    d._fallback_skip_warned.clear()


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
    monkeypatch.setattr(d, "probe_endpoint_cause", _boom)
    ext, which = d.build_extractor_with_fallback(_cfg("http://p:1/v1"))
    assert which == "primary" and ext.base_url == "http://p:1/v1"


def test_builder_auto_primary_up(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint_cause", lambda *a, **k: None)
    ext, which = d.build_extractor_with_fallback(
        _cfg("http://p:1/v1", fb="http://f:2/v1"))
    assert which == "primary" and ext.base_url == "http://p:1/v1"


def test_builder_auto_primary_down(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint_cause", lambda *a, **k: _REFUSED)
    monkeypatch.setattr(d, "_probe_retry_delay", 0.0)
    monkeypatch.setattr(d, "_host_resolves", lambda h: True)
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
    monkeypatch.setattr(d, "probe_endpoint_cause",
                        lambda *a, **k: None if calls.append(1) or len(calls) > 1
                        else _REFUSED)
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
    monkeypatch.setattr(d, "probe_endpoint_cause", _boom)
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
    monkeypatch.setattr(d, "probe_endpoint_cause", lambda *a, **k: _REFUSED)
    monkeypatch.setattr(d, "_probe_retry_delay", 0.0)
    monkeypatch.setattr(d, "_host_resolves", lambda h: True)
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


# ── model override (console dreamer picker, 2026-08-04) ──────────────────

def test_dreamconfig_model_override_default_none():
    assert DreamConfig().extractor_model_override is None


def test_model_override_wins_over_env(monkeypatch):
    # The whole point of the override: switch the dreamer model without
    # flipping extractor_source to "config" (which would orphan the
    # env-owned endpoint/fallback wiring).
    from pseudolife_memory.memory.dream import resolve_endpoints
    monkeypatch.setenv("PSEUDOLIFE_DREAM_BASE_URL", "http://p:1/v1")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_MODEL", "extractor")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL", "http://f:2/v1")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_FALLBACK_MODEL", "fm")
    r = resolve_endpoints(DreamConfig(extractor_model_override="claude-fable-5"))
    assert r["primary_model"] == "claude-fable-5"
    # Only the primary model is overridden — the fallback sidecar keeps
    # serving its own model name, and URLs stay env-owned.
    assert r["fallback_model"] == "fm"
    assert r["primary_url"] == "http://p:1/v1"


def test_model_override_wins_over_config():
    from pseudolife_memory.memory.dream import resolve_endpoints
    cfg = _cfg("http://p:1/v1", fb="http://f:2/v1")
    cfg.extractor_model_override = "claude-sonnet-5"
    r = resolve_endpoints(cfg)
    assert r["primary_model"] == "claude-sonnet-5"
    assert r["fallback_model"] == "m2"


def test_model_override_unset_is_inert(monkeypatch):
    from pseudolife_memory.memory.dream import resolve_endpoints
    monkeypatch.setenv("PSEUDOLIFE_DREAM_MODEL", "extractor")
    r = resolve_endpoints(DreamConfig())
    assert r["primary_model"] == "extractor"


def test_build_extractor_applies_model_override(monkeypatch):
    # The builder that constructs the ACTUAL dream extractor must resolve
    # through the same path the status display does — an override that only
    # showed in dream_status while build_extractor ignored it would be the
    # console lying about what the next dream runs.
    from pseudolife_memory.memory.dream import build_extractor
    monkeypatch.setenv("PSEUDOLIFE_DREAM_BASE_URL", "http://p:1/v1")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_MODEL", "extractor")
    ext = build_extractor(DreamConfig(extractor_model_override="claude-fable-5"))
    assert ext.model == "claude-fable-5"


def test_builder_primary_path_applies_model_override(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint_cause", lambda *a, **k: None)
    cfg = _cfg("http://p:1/v1", fb="http://f:2/v1")
    cfg.extractor_model_override = "claude-haiku-4-5"
    ext, which = d.build_extractor_with_fallback(cfg)
    assert which == "primary" and ext.model == "claude-haiku-4-5"


def test_status_fields_carry_models_source_and_override(monkeypatch):
    # The console dreamer card needs the EFFECTIVE models, not just URLs.
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: True)
    cfg = _cfg("http://p:1/v1", fb="http://f:2/v1")
    cfg.extractor_model_override = "claude-opus-5"
    fields = d._status_extractor_fields(cfg, None)
    assert fields["primary_model"] == "claude-opus-5"
    assert fields["fallback_model"] == "m2"
    assert fields["extractor_source"] == "config"
    assert fields["model_override"] == "claude-opus-5"


def test_status_fields_no_fallback_hides_fallback_model():
    from pseudolife_memory.memory.dream import _status_extractor_fields
    fields = _status_extractor_fields(_cfg("http://p:1/v1"), None)
    assert fields["primary_model"] == "m"
    assert fields["fallback_model"] is None
    assert fields["model_override"] is None


def test_config_io_has_model_override_knob():
    from pseudolife_memory.web.config_io import KNOBS
    knob = next(e for e in KNOBS
                if e["path"] == "memory.dream.extractor_model_override")
    assert knob["type"] == "string"
    assert knob["restart"] is False


# ── served-model alias resolution (dreamer-card follow-up) ───────────────

def test_fetch_served_model_returns_first_listing():
    from pseudolife_memory.memory import dream as d
    d._served_model_cache.clear()
    with _health_server() as base:
        assert d.fetch_served_model(base) == "claude-opus-5"


def test_fetch_served_model_caches_within_ttl():
    # Status is polled by the console and session hooks — the TTL cache
    # bounds the cost to one small GET per endpoint per window.
    from pseudolife_memory.memory import dream as d
    d._served_model_cache.clear()
    with _health_server() as base:
        assert d.fetch_served_model(base) == "claude-opus-5"
    # Server is gone; the cached verdict still answers inside the TTL.
    assert d.fetch_served_model(base) == "claude-opus-5"


def test_fetch_served_model_failure_is_none():
    from pseudolife_memory.memory import dream as d
    d._served_model_cache.clear()
    assert d.fetch_served_model("http://127.0.0.1:9/v1", timeout=0.3) is None


def test_status_resolves_alias_to_served_model(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: True)
    monkeypatch.setattr(d, "fetch_served_model",
                        lambda *a, **k: "claude-opus-5")
    cfg = _cfg("http://p:1/v1", fb="http://f:2/v1")
    cfg.extractor_model = "extractor"          # launch-default alias
    fields = d._status_extractor_fields(cfg, None)
    assert fields["primary_model_served"] == "claude-opus-5"


def test_status_skips_resolution_for_concrete_model(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: True)

    def _boom(*a, **k):
        raise AssertionError("no /models fetch for a concrete model name")
    monkeypatch.setattr(d, "fetch_served_model", _boom)
    fields = d._status_extractor_fields(
        _cfg("http://p:1/v1", fb="http://f:2/v1"), None)   # model "m"
    assert fields["primary_model_served"] is None


def test_status_skips_resolution_when_override_set(monkeypatch):
    # An override IS the concrete model — the alias never reaches the wire.
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: True)

    def _boom(*a, **k):
        raise AssertionError("no /models fetch when the override decides")
    monkeypatch.setattr(d, "fetch_served_model", _boom)
    cfg = _cfg("http://p:1/v1", fb="http://f:2/v1")
    cfg.extractor_model = "extractor"
    cfg.extractor_model_override = "claude-fable-5"
    fields = d._status_extractor_fields(cfg, None)
    assert fields["primary_model_served"] is None
    assert fields["primary_model"] == "claude-fable-5"


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


# ── cache_prompt pin on every extractor the daemon builds ────────────────
#
# Deferred decision from the 2026-08-09 warm-cache root cause, resolved by
# measurement. warm-cache-probe-0809 proved llama-server's default prompt
# cache changes extractor OUTPUT once populated; sidecar-cache-latency-0809
# measured the cost of pinning it off on the live sidecar at +7.25s/call
# (3.4s -> 10.65s) — noise for a 600s background sweep with a 480s timeout
# budget. The daemon therefore pins ``cache_prompt: false`` by default;
# ``memory.dream.extractor_cache_prompt = None`` restores the server default
# for deployments that prefer the latency.

def _pin_cfg(**over) -> DreamConfig:
    cfg = _cfg("http://127.0.0.1:9/v1")
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_daemon_extractor_pins_cache_off_by_default():
    from pseudolife_memory.memory.dream import build_extractor
    ex = build_extractor(_pin_cfg())
    assert ex.extra_body == {"cache_prompt": False}


def test_none_restores_the_server_default():
    from pseudolife_memory.memory.dream import build_extractor
    ex = build_extractor(_pin_cfg(extractor_cache_prompt=None))
    assert ex.extra_body == {}


def test_true_forces_the_cache_on_explicitly():
    from pseudolife_memory.memory.dream import build_extractor
    ex = build_extractor(_pin_cfg(extractor_cache_prompt=True))
    assert ex.extra_body == {"cache_prompt": True}


# ── reasoning-effort pass-through (2026-09-01 dreamer effort knob) ─────────

def test_dreamconfig_reasoning_effort_defaults_off():
    assert DreamConfig().extractor_reasoning_effort is None


def test_primary_extractor_threads_reasoning_effort():
    from pseudolife_memory.memory.dream import build_extractor
    ex = build_extractor(_pin_cfg(extractor_reasoning_effort="high"))
    assert ex.extra_body["reasoning_effort"] == "high"
    assert ex.extra_body["cache_prompt"] is False   # merged, not replaced


def test_unset_effort_sends_nothing():
    # Empty knob == pre-knob behavior: the field never appears on the wire,
    # so the endpoint / CLI / host-config default keeps serving.
    from pseudolife_memory.memory.dream import build_extractor
    ex = build_extractor(_pin_cfg())
    assert "reasoning_effort" not in ex.extra_body
    ex = build_extractor(_pin_cfg(extractor_reasoning_effort=""))
    assert "reasoning_effort" not in ex.extra_body


def test_fallback_extractor_never_carries_reasoning_effort():
    # Same philosophy as the model override: the fallback sidecar's measured
    # config is never perturbed by primary-side tuning.
    from pseudolife_memory.memory.dream import build_extractor_with_fallback
    cfg = _cfg("http://127.0.0.1:9/v1", fb="http://127.0.0.1:10/v1",
               mode="fallback")
    cfg.extractor_reasoning_effort = "high"
    ex, which = build_extractor_with_fallback(cfg)
    assert which == "fallback"
    assert "reasoning_effort" not in ex.extra_body


# ── the primary's own cause and a fallback that does not resolve ─────────
# (2026-10-27: a signed-out primary shim answered /health 503, the daemon
# fell back to a sidecar this stack does not run, and the stall named the
# fallback's DNS failure instead of the expired sign-in.)

@contextlib.contextmanager
def _answering(status, body):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            code, payload = (status, body) if self.path == "/health" else (404, b"{}")
            self.send_response(code)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    finally:
        srv.shutdown()
        srv.server_close()


def test_probe_cause_names_a_signed_out_shim():
    from pseudolife_memory.memory import dream as d
    body = (b'{"status": "login_required", "detail": "stdout: Failed to '
            b'authenticate: OAuth session expired", "fix": "sign in"}')
    with _answering(503, body) as base:
        assert d.probe_endpoint_cause(base) == (
            "login_expired", "/health HTTP 503 (login_required)")
        assert d.probe_endpoint(base) is False


def test_probe_cause_names_other_shim_failures_by_status_only():
    # The detail can carry a path or a prompt: only the status token, from
    # a fixed shape, reaches the cause.
    from pseudolife_memory.memory import dream as d
    body = b'{"status": "cli_error", "detail": "stdout: /home/someone/secret"}'
    with _answering(503, body) as base:
        assert d.probe_endpoint_cause(base) == (
            "extractor_error", "/health HTTP 503 (cli_error)")


@pytest.mark.parametrize("body", [
    b'{"status": "Bearer abc.def", "detail": "x"}',
    b"not json at all",
    b'{"detail": "OAuth session expired"}',       # words alone decide nothing
])
def test_probe_cause_drops_a_status_outside_the_vocabulary(body):
    from pseudolife_memory.memory import dream as d
    with _answering(503, body) as base:
        assert d.probe_endpoint_cause(base) == ("extractor_error", "/health HTTP 503")


def test_probe_cause_401_is_login_expired():
    from pseudolife_memory.memory import dream as d
    with _answering(401, b"{}") as base:
        assert d.probe_endpoint_cause(base) == ("login_expired", "/health HTTP 401")


def test_probe_cause_refused_and_healthy():
    from pseudolife_memory.memory import dream as d
    # Windows retries a refused connect past a short timeout, so either
    # unreachable form is the answer here.
    reason, error = d.probe_endpoint_cause("http://127.0.0.1:9/v1", timeout=0.3)
    assert reason == "extractor_unreachable"
    assert error in ("connection refused", "timed out")
    with _health_server() as base:
        assert d.probe_endpoint_cause(base) is None


def test_builder_hands_the_primary_cause_to_the_fallback(monkeypatch):
    from pseudolife_memory.memory import dream as d
    cause = ("login_expired", "/health HTTP 503 (login_required)")
    monkeypatch.setattr(d, "probe_endpoint_cause", lambda *a, **k: cause)
    monkeypatch.setattr(d, "_probe_retry_delay", 0.0)
    ext, which = d.build_extractor_with_fallback(
        _cfg("http://p:1/v1", fb="http://f:2/v1"))
    assert which == "fallback" and ext.base_url == "http://f:2/v1"
    assert ext.primary_probe == {"reason": "login_expired",
                                 "error": "/health HTTP 503 (login_required)",
                                 "fallback_resolves": True}


def test_builder_skips_a_fallback_that_does_not_resolve(monkeypatch, caplog):
    import logging

    from pseudolife_memory.memory import dream as d
    cause = ("login_expired", "/health HTTP 503 (login_required)")
    monkeypatch.setattr(d, "probe_endpoint_cause", lambda *a, **k: cause)
    monkeypatch.setattr(d, "_probe_retry_delay", 0.0)
    monkeypatch.setattr(d, "_host_resolves", lambda h: h != "pseudolife-extractor")
    cfg = _cfg("http://host.docker.internal:8082/v1",
               fb="http://pseudolife-extractor:8081/v1")
    with caplog.at_level(logging.WARNING, logger="pseudolife_memory.memory.dream"):
        for _ in range(3):
            ext, which = d.build_extractor_with_fallback(cfg)
            assert which == "primary"
            assert ext.base_url == "http://host.docker.internal:8082/v1"
            assert ext.primary_probe["fallback_resolves"] is False
    said = [r.getMessage() for r in caplog.records if "does not resolve" in r.getMessage()]
    assert len(said) == 1                       # reported once, not per sweep
    assert "remove the fallback" in said[0]


def test_dream_status_flags_a_fallback_that_does_not_resolve(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: False)
    monkeypatch.setattr(d, "_host_resolves", lambda h: False)
    fields = d._status_extractor_fields(
        _cfg("http://p:1/v1", fb="http://pseudolife-extractor:8081/v1"), None)
    warning = fields["fallback_warning"]
    assert "does not resolve" in warning
    assert "remove the fallback" in warning and "start" in warning
    keys = list(fields)
    assert keys.index("fallback_warning") == keys.index("fallback_model") + 1


def test_dream_status_fallback_warning_is_null_when_fine_or_absent(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: True)
    assert d._status_extractor_fields(
        _cfg("http://p:1/v1", fb="http://f:2/v1"), None)["fallback_warning"] is None

    def _boom(*a, **k):
        raise AssertionError("no DNS lookup without a fallback")
    monkeypatch.setattr(d, "_host_resolves", _boom)
    assert d._status_extractor_fields(_cfg("http://p:1/v1"), None)["fallback_warning"] is None


def test_dream_status_caches_the_resolution(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(d, "_host_resolves", lambda h: bool(calls.append(h)))
    cfg = _cfg("http://p:1/v1", fb="http://pseudolife-extractor:8081/v1")
    d._status_extractor_fields(cfg, None)
    d._status_extractor_fields(cfg, None)
    assert calls == ["pseudolife-extractor"]


def test_startup_warns_when_the_fallback_does_not_resolve(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "_host_resolves", lambda h: h != "pseudolife-extractor")
    warnings = d.startup_extractor_warnings(
        _cfg("http://host.docker.internal:8082/v1",
             fb="http://pseudolife-extractor:8081/v1"))
    said = [w for w in warnings if "does not resolve" in w]
    assert len(said) == 1 and "remove the fallback" in said[0]


# ── review fixes for 5599ae3a (Codex P1/P2, Opus 1-4) ──────────────────

def test_a_fallback_only_setup_still_dials_an_unresolvable_fallback(monkeypatch):
    # Codex P1 / Opus 1: with no usable primary, skipping the fallback chose
    # the no-op extractor, whose empty extraction acknowledged the pulled
    # backlog. The dream must dial the fallback, fail and hold instead.
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "_host_resolves", lambda h: False)
    cfg = DreamConfig(extractor_source="config", extractor_mode="auto",
                      fallback_base_url="http://pseudolife-extractor:8081/v1",
                      fallback_model="m2")
    ext, which = d.build_extractor_with_fallback(cfg)
    assert which == "fallback" and ext.base_url == "http://pseudolife-extractor:8081/v1"
    assert ext.primary_probe["fallback_resolves"] is False


def test_a_primary_without_a_model_is_not_usable(monkeypatch):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint_cause", lambda *a, **k: _REFUSED)
    monkeypatch.setattr(d, "_probe_retry_delay", 0.0)
    monkeypatch.setattr(d, "_host_resolves", lambda h: False)
    cfg = DreamConfig(extractor_source="config", extractor_mode="auto",
                      extractor_base_url="http://p:1/v1", extractor_model=None,
                      fallback_base_url="http://pseudolife-extractor:8081/v1",
                      fallback_model="m2")
    ext, which = d.build_extractor_with_fallback(cfg)
    assert which == "fallback"


@pytest.mark.parametrize("host", ["a..b", "x" * 64 + ".example"])
def test_a_malformed_host_is_judged_not_raised(host):
    # Opus 2: getaddrinfo raises UnicodeError (idna) for these; it reached
    # dream_status and the sweep unguarded.
    assert _REAL_HOST_RESOLVES(host) in (True, False)


@pytest.mark.parametrize("err, resolves", [
    ("EAI_NONAME", False),      # a definitive not-found answer
    ("EAI_AGAIN", True),        # temporary: cannot judge, so not skipped
])
def test_only_a_definitive_not_found_counts(monkeypatch, err, resolves):
    import socket

    from pseudolife_memory.memory import dream as d
    code = getattr(socket, err)

    def _gai(*a, **k):
        raise socket.gaierror(code, err)
    monkeypatch.setattr(d.socket if hasattr(d, "socket") else socket, "getaddrinfo", _gai)
    assert _REAL_HOST_RESOLVES("pseudolife-extractor") is resolves


@pytest.mark.parametrize("var", ["HTTP_PROXY", "http_proxy", "ALL_PROXY"])
def test_a_proxied_fallback_is_never_judged_by_local_dns(monkeypatch, var):
    # Codex P2 / Opus 3: urllib and reqwest reach a proxied host without
    # resolving it here.
    from pseudolife_memory.memory import dream as d
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                 "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(var, "http://proxy.example:3128")

    def _boom(h):
        raise AssertionError("no DNS judgement behind a proxy")
    monkeypatch.setattr(d, "_host_resolves", _boom)
    assert d._url_resolves("http://pseudolife-extractor:8081/v1") is True


@pytest.mark.parametrize("url, host", [
    ("http://pseudolife-extractor:8081/v1", "pseudolife-extractor"),
    ("http://User:Pass@Host.Example:1/v1", "host.example"),
    ("http://[::1]:8/v1", "::1"),
    ("http:///v1", None),
    ("http://h:99999/v1", "h"),
])
def test_url_host_matches_urlsplit(url, host):
    from urllib.parse import urlsplit

    from pseudolife_memory.memory import dream as d
    assert urlsplit(url).hostname == host == d._url_host(url)


@pytest.mark.parametrize("mode, primary, want", [
    ("auto", True, "never used"),
    ("auto", False, "dreams are sent to it"),
    ("fallback", True, "dreams are sent to it"),
    ("primary", True, None),
])
def test_the_fallback_warning_says_what_the_mode_does(monkeypatch, mode, primary, want):
    # Opus 4: "never used" is true only where the builder skips it.
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint", lambda *a, **k: True)
    monkeypatch.setattr(d, "_host_resolves", lambda h: False)
    cfg = DreamConfig(extractor_source="config", extractor_mode=mode,
                      extractor_base_url="http://p:1/v1" if primary else None,
                      extractor_model="m" if primary else None,
                      fallback_base_url="http://pseudolife-extractor:8081/v1",
                      fallback_model="m2")
    warning = d._status_extractor_fields(cfg, None)["fallback_warning"]
    assert (warning is None) if want is None else (want in warning)
    startup = [w for w in d.startup_extractor_warnings(cfg) if "does not resolve" in w]
    assert (startup == []) if want is None else (want in startup[0])


def test_logged_urls_are_redacted(monkeypatch, caplog):
    import logging

    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "probe_endpoint_cause", lambda *a, **k: _REFUSED)
    monkeypatch.setattr(d, "_probe_retry_delay", 0.0)
    monkeypatch.setattr(d, "_host_resolves", lambda h: False)
    cfg = _cfg("http://p:1/v1", fb="http://user:secret@fallback.example:8081/v1")
    with caplog.at_level(logging.WARNING, logger="pseudolife_memory.memory.dream"):
        d.build_extractor_with_fallback(cfg)
        d.startup_extractor_warnings(cfg)
    text = "\n".join(r.getMessage() for r in caplog.records) + "\n".join(
        d.startup_extractor_warnings(cfg))
    assert "secret" not in text and "fallback.example" in text


# ── the signed-out shim's identity rides on the probe (PR 3) ─────────────

def test_probe_cause_carries_a_signed_out_shims_provider_and_user():
    from pseudolife_memory.memory import dream as d
    body = (b'{"status": "login_required", "detail": "x", "fix": "y", '
            b'"provider": "claude", "user": "svc-user"}')
    with _answering(503, body) as base:
        cause = d.probe_endpoint_cause(base)
    assert cause == ("login_expired", "/health HTTP 503 (login_required)")
    assert (cause.provider, cause.shim_user) == ("claude", "svc-user")


@pytest.mark.parametrize("body", [
    # identity only for a sign-in answer, and only of the fixed shapes
    b'{"status": "cli_error", "provider": "claude", "user": "svc"}',
    b'{"status": "login_required", "provider": "other", "user": "svc"}',
])
def test_probe_cause_drops_identity_outside_its_shape(body):
    from pseudolife_memory.memory import dream as d
    with _answering(503, body) as base:
        cause = d.probe_endpoint_cause(base)
    assert getattr(cause, "provider", None) is None


@pytest.mark.parametrize("user, kept", [
    ("svc-user", "svc-user"), ("DOMAIN\\\\alice", "DOMAIN\\alice"),
    ("a b", None), ("x" * 65, None), ("", None),
])
def test_probe_cause_keeps_only_a_username_shaped_user(user, kept):
    from pseudolife_memory.memory import dream as d
    body = ('{"status": "login_required", "provider": "codex", "user": "%s"}' % user).encode()
    with _answering(503, body) as base:
        cause = d.probe_endpoint_cause(base)
    assert cause.provider == "codex" and cause.shim_user == kept


def test_builder_puts_the_identity_on_the_probe(monkeypatch):
    from pseudolife_memory.memory import dream as d
    cause = d.ProbeCause("login_expired", "/health HTTP 503 (login_required)",
                         provider="claude", shim_user="svc")
    monkeypatch.setattr(d, "probe_endpoint_cause", lambda *a, **k: cause)
    monkeypatch.setattr(d, "_probe_retry_delay", 0.0)
    ext, which = d.build_extractor_with_fallback(_cfg("http://p:1/v1", fb="http://f:2/v1"))
    assert ext.primary_probe["provider"] == "claude"
    assert ext.primary_probe["shim_user"] == "svc"
