# Sonnet Sidecar Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daemon's dream pass uses Sonnet (host shim) as its primary extractor with automatic fallback to the in-stack E4B container, visible and overridable in the Cortex Console.

**Architecture:** A `build_extractor_with_fallback(cfg)` in `dream.py` probes the primary endpoint per invocation and returns `(extractor, "primary"|"fallback")`; a new `Service.dream_run_auto()` wraps selection + recording so all four live call sites share one path. `dream_status()` grows extractor-visibility fields consumed by an observatory badge; three new `config_io` schema entries give the console override for free. Spec: `docs/superpowers/specs/2026-07-11-sonnet-sidecar-cutover-design.md`.

**Tech Stack:** Python 3.11 (stdlib urllib only — no new deps), pytest, vanilla-JS console (`el()` helpers), PowerShell scheduled task.

## Global Constraints

- New env vars, exact names: `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL`, `PSEUDOLIFE_DREAM_FALLBACK_MODEL`, `PSEUDOLIFE_DREAM_EXTRACTOR_MODE`.
- `extractor_mode` values, exact: `auto` | `primary` | `fallback`; default `auto`.
- `fallback_base_url` unset ⇒ behavior byte-identical to today (single extractor, no probe).
- Timeout and max_tokens are SHARED by both endpoints — no fallback-specific copies.
- The daemon always sends the production `_SYSTEM_PROMPT`; prompt swapping stays in the shim. Do not touch `_SYSTEM_PROMPT`.
- Probe: GET `/health` at the base with any trailing `/v1` stripped; if HTTP 404, GET `{base_url}/models`; ~3s timeout; success = HTTP 200.
- `extractor_source` ("env"/"config") governs the new fields exactly as it governs the existing extractor fields; api_key stays env-only.
- Bench/eval harness (`evals/ladder_sweep.py`, `evals/longmemeval_bench.py`) is untouched.
- stdlib `urllib.request` for HTTP (matches `dream.py`); no new dependencies.
- Frequent commits; run `python -m pytest tests/ -x -q` (full suite) before the final task's commit.

---

### Task 1: Config fields + endpoint resolution

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (DreamConfig, after `extractor_timeout_seconds` at ~line 299)
- Modify: `pseudolife_memory/memory/dream.py` (new `resolve_endpoints` above `build_extractor` at ~line 409)
- Test: `tests/test_extractor_fallback.py` (create)

**Interfaces:**
- Consumes: existing `DreamConfig`, `build_extractor` env-vs-config pattern (`dream.py:409-450`).
- Produces: `DreamConfig.fallback_base_url: str | None`, `DreamConfig.fallback_model: str | None`, `DreamConfig.extractor_mode: str = "auto"`; `resolve_endpoints(cfg) -> dict` with keys `mode, primary_url, primary_model, fallback_url, fallback_model, max_tokens, timeout` (all env-vs-config resolved). Tasks 2 and 4 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extractor_fallback.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_extractor_fallback.py -x -q`
Expected: FAIL — `DreamConfig` has no `fallback_base_url`; `resolve_endpoints` import error.

- [ ] **Step 3: Add the config fields**

In `pseudolife_memory/utils/config.py`, inside `DreamConfig`, directly after the `extractor_timeout_seconds` field (~line 299), add:

```python
    # Primary/fallback extractor selection (2026-07-11 sonnet-sidecar-cutover
    # spec). fallback_base_url unset => single-extractor behavior identical
    # to before (no probe, no selection). extractor_mode: "auto" probes the
    # primary and falls back; "primary" never falls back (outages hold);
    # "fallback" skips the primary entirely (sovereign-only override).
    # Env: PSEUDOLIFE_DREAM_FALLBACK_BASE_URL / _FALLBACK_MODEL /
    # _EXTRACTOR_MODE (honoured when extractor_source == "env").
    # Timeout/max_tokens are shared with the primary — no fallback copies.
    fallback_base_url: str | None = None
    fallback_model: str | None = None
    extractor_mode: str = "auto"
```

- [ ] **Step 4: Add `resolve_endpoints` to dream.py**

In `pseudolife_memory/memory/dream.py`, directly above `build_extractor` (~line 409), add:

```python
_EXTRACTOR_MODES = ("auto", "primary", "fallback")


def resolve_endpoints(cfg) -> dict:
    """Resolve primary + fallback endpoint settings honouring the same
    env-vs-config ownership as ``build_extractor``: ``extractor_source ==
    "env"`` (the ops contract) lets PSEUDOLIFE_DREAM_* env vars override the
    dataclass; ``"config"`` uses the config values and ignores env. An
    unknown mode degrades to "auto" (never crash the sweep on a typo'd env
    var). Returns {mode, primary_url, primary_model, fallback_url,
    fallback_model, max_tokens, timeout}."""
    import os

    def _env_num(name, fallback, cast):
        raw = os.environ.get(name)
        if not raw:
            return fallback
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return fallback

    from_config = getattr(cfg, "extractor_source", "env") == "config"
    if from_config:
        out = {
            "primary_url": cfg.extractor_base_url,
            "primary_model": cfg.extractor_model,
            "fallback_url": cfg.fallback_base_url,
            "fallback_model": cfg.fallback_model,
            "mode": cfg.extractor_mode,
            "max_tokens": cfg.extractor_max_tokens,
            "timeout": cfg.extractor_timeout_seconds,
        }
    else:
        out = {
            "primary_url": (os.environ.get("PSEUDOLIFE_DREAM_BASE_URL")
                            or cfg.extractor_base_url),
            "primary_model": (os.environ.get("PSEUDOLIFE_DREAM_MODEL")
                              or cfg.extractor_model),
            "fallback_url": (os.environ.get("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL")
                             or cfg.fallback_base_url),
            "fallback_model": (os.environ.get("PSEUDOLIFE_DREAM_FALLBACK_MODEL")
                               or cfg.fallback_model),
            "mode": (os.environ.get("PSEUDOLIFE_DREAM_EXTRACTOR_MODE")
                     or cfg.extractor_mode),
            "max_tokens": _env_num("PSEUDOLIFE_DREAM_MAX_TOKENS",
                                   cfg.extractor_max_tokens, int),
            "timeout": _env_num("PSEUDOLIFE_DREAM_TIMEOUT_SECONDS",
                                cfg.extractor_timeout_seconds, float),
        }
    if out["mode"] not in _EXTRACTOR_MODES:
        out["mode"] = "auto"
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_extractor_fallback.py -x -q`
Expected: 4 passed. Also run the neighbours: `python -m pytest tests/test_dream.py tests/test_phase0_config.py -q` — expected all pass (fields are additive with defaults).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/memory/dream.py tests/test_extractor_fallback.py
git commit -m "feat(dream): fallback extractor config fields + resolve_endpoints"
```

---

### Task 2: Probe + build_extractor_with_fallback

**Files:**
- Modify: `pseudolife_memory/memory/dream.py` (after `resolve_endpoints`)
- Test: `tests/test_extractor_fallback.py` (extend)

**Interfaces:**
- Consumes: `resolve_endpoints(cfg)` (Task 1), existing `OpenAICompatExtractor(base_url, model, api_key=..., max_tokens=..., timeout_seconds=...)`, `NoOpExtractor`.
- Produces: `probe_endpoint(base_url: str, timeout: float = 3.0) -> bool`; `build_extractor_with_fallback(cfg) -> tuple[DreamExtractor, str]` where the str is `"primary"` or `"fallback"`; raises `ValueError` for mode `fallback` with fallback unset. Task 3 relies on both exact signatures.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extractor_fallback.py`:

```python
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
    ext, which = d.build_extractor_with_fallback(
        _cfg("http://p:1/v1", fb="http://f:2/v1"))
    assert which == "fallback" and ext.base_url == "http://f:2/v1"
    assert ext.model == "m2"


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_extractor_fallback.py -x -q`
Expected: FAIL — `probe_endpoint` / `build_extractor_with_fallback` not defined.

- [ ] **Step 3: Implement probe + builder**

In `pseudolife_memory/memory/dream.py`, directly after `resolve_endpoints`, add:

```python
def probe_endpoint(base_url: str, timeout: float = 3.0) -> bool:
    """Is an OpenAI-compatible endpoint alive? GET /health at the base with
    any trailing /v1 stripped (the sonnet shim serves /health at root and
    answers 503 when its CLI is logged out); a 404 there means a plain
    llama-server, so retry as GET {base_url}/models. Only HTTP 200 counts."""
    import urllib.error
    import urllib.request

    root = base_url.rstrip("/")
    root = root.removesuffix("/v1")
    for url in (f"{root}/health", f"{base_url.rstrip('/')}/models"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404 and url.endswith("/health"):
                continue                      # llama-server: try /models
            return False
        except Exception:  # noqa: BLE001 — connection refused, timeout, DNS
            return False
    return False


def build_extractor_with_fallback(cfg) -> tuple["DreamExtractor", str]:
    """Selection step for the LIVE dream path: returns (extractor, which)
    with which in {"primary", "fallback"}. Fallback unset => exactly
    ``build_extractor`` (no probe, single-extractor behavior). Mode "auto"
    probes the primary per invocation — recovery is automatic at the next
    sweep. Raises ValueError for mode "fallback" with no fallback URL.
    The bench/eval harness never calls this — it constructs extractors
    directly so runs stay pinned to one endpoint."""
    import os

    r = resolve_endpoints(cfg)
    api_key = os.environ.get("PSEUDOLIFE_DREAM_API_KEY") or cfg.extractor_api_key
    if r["mode"] == "fallback":
        if not (r["fallback_url"] and r["fallback_model"]):
            raise ValueError(
                "extractor_mode=fallback but no fallback endpoint is "
                "configured (fallback_base_url/fallback_model)")
        return OpenAICompatExtractor(
            r["fallback_url"], r["fallback_model"], api_key=api_key,
            max_tokens=r["max_tokens"], timeout_seconds=r["timeout"],
        ), "fallback"
    if not (r["fallback_url"] and r["fallback_model"]) or r["mode"] == "primary":
        return build_extractor(cfg), "primary"
    # mode == "auto" with a configured fallback: probe, then choose.
    if r["primary_url"] and probe_endpoint(r["primary_url"]):
        return build_extractor(cfg), "primary"
    logger.warning("dream primary extractor %s unreachable — using fallback %s",
                   r["primary_url"], r["fallback_url"])
    return OpenAICompatExtractor(
        r["fallback_url"], r["fallback_model"], api_key=api_key,
        max_tokens=r["max_tokens"], timeout_seconds=r["timeout"],
    ), "fallback"
```

Note: `OpenAICompatExtractor` must expose `base_url` and `model` attributes for the tests — it already stores them in `__init__` (see `dream.py` ~line 210); if the attribute names differ, adjust the TESTS to the real attribute names, not the extractor.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_extractor_fallback.py -x -q`
Expected: all pass (14 tests total so far).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/dream.py tests/test_extractor_fallback.py
git commit -m "feat(dream): probe_endpoint + build_extractor_with_fallback"
```

---

### Task 3: Service dream_run_auto + call-site switches

**Files:**
- Modify: `pseudolife_memory/service.py` (new method near `dream_run` ~line 2222; `_fire_and_forget_dream` ~line 2639)
- Modify: `pseudolife_memory/memory/dream.py` (`run_sweep_once` ~line 465)
- Modify: `pseudolife_memory/web/routes.py` (`_dream_run` ~line 261)
- Modify: `pseudolife_memory/mcp_server.py` (action "run" ~line 684)
- Modify: `pseudolife_memory/web/fixtures.py` (fixture service, near `dream_run` ~line 543)
- Test: `tests/test_extractor_fallback.py` (extend)

**Interfaces:**
- Consumes: `build_extractor_with_fallback(cfg) -> (extractor, which)` (Task 2), `Service.dream_run(extractor, *, limit=None) -> dict`.
- Produces: `Service.dream_run_auto(*, limit: int | None = None) -> dict` — the `dream_run` result plus `"extractor": "primary"|"fallback"`, or `{"error": str(ValueError)}` for misconfiguration; side effect: sets `self._last_dream_extractor = {"which": str, "base_url": str | None, "at": float}`. Task 4 reads `_last_dream_extractor`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_extractor_fallback.py`:

```python
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
```

(`MemoryService` is the service class — `pseudolife_memory/service.py:303`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_extractor_fallback.py -x -q`
Expected: FAIL — no attribute `dream_run_auto`.

- [ ] **Step 3: Implement `dream_run_auto`**

In `pseudolife_memory/service.py`, directly after `dream_run`'s method body ends, add:

```python
    def dream_run_auto(self, *, limit: int | None = None) -> dict[str, Any]:
        """dream_run with primary/fallback extractor selection (2026-07-11
        sonnet-sidecar-cutover spec): probe-and-choose per invocation, record
        which side served for dream_status, and stamp the result. The single
        entry point for every LIVE dream trigger (sweep, console, MCP tool,
        session-end); the bench harness keeps calling dream_run directly."""
        from pseudolife_memory.memory.dream import build_extractor_with_fallback
        try:
            extractor, which = build_extractor_with_fallback(
                self.config.memory.dream)
        except ValueError as e:
            return {"error": str(e), "pulled": 0, "claims": 0}
        import time as _t
        self._last_dream_extractor = {
            "which": which,
            "base_url": getattr(extractor, "base_url", None),
            "at": _t.time(),
        }
        result = self.dream_run(extractor, limit=limit)
        result["extractor"] = which
        return result
```

Also add the attribute default in the service `__init__` (find it with `grep -n "_last_dream" pseudolife_memory/service.py` — if absent, add `self._last_dream_extractor: dict | None = None` alongside the other instance attributes).

- [ ] **Step 4: Switch the four live call sites**

`pseudolife_memory/memory/dream.py` `run_sweep_once` (~line 465), replace:

```python
    result = service.dream_run(build_extractor(cfg))
```

with:

```python
    result = service.dream_run_auto()
```

`pseudolife_memory/service.py` `_fire_and_forget_dream` (~lines 2644-2647), replace the `_run` body:

```python
        def _run() -> None:
            try:
                self.dream_run_auto()
            except Exception:  # noqa: BLE001 — background best-effort
                logger.warning("session-end dream failed", exc_info=True)
```

`pseudolife_memory/web/routes.py` `_dream_run` (~lines 261-266), replace:

```python
    def _dream_run(self, b: dict) -> dict:
        from pseudolife_memory.memory.dream import build_extractor
        limit = b.get("limit")
        return self.svc.dream_run(
            build_extractor(self.svc.config.memory.dream),
            limit=int(limit) if limit not in (None, "") else None)
```

with:

```python
    def _dream_run(self, b: dict) -> dict:
        limit = b.get("limit")
        return self.svc.dream_run_auto(
            limit=int(limit) if limit not in (None, "") else None)
```

`pseudolife_memory/mcp_server.py` action "run" (~lines 684-688), replace:

```python
    if action == "run":
        from pseudolife_memory.memory.dream import build_extractor
        return service.dream_run(
            build_extractor(service.config.memory.dream), limit=limit,
        )
```

with:

```python
    if action == "run":
        return service.dream_run_auto(limit=limit)
```

`pseudolife_memory/web/fixtures.py`, directly after the fixture `dream_run` (~line 546), add:

```python
    def dream_run_auto(self, limit=None):
        return {**self.dream_run(None, limit=limit), "extractor": "primary"}
```

Leave `mcp_server.py:1095`'s `isinstance(build_extractor(...), NoOpExtractor)` check untouched — it only asks "is any extractor configured".

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_extractor_fallback.py tests/test_dream.py -q`
Expected: all pass (the `run_sweep_once` tests in test_dream.py exercise the switched path — if any constructed a stub service without `dream_run_auto`, add the method to that stub returning its `dream_run` result plus `{"extractor": "primary"}`).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/service.py pseudolife_memory/memory/dream.py pseudolife_memory/web/routes.py pseudolife_memory/mcp_server.py pseudolife_memory/web/fixtures.py tests/test_extractor_fallback.py
git commit -m "feat(service): dream_run_auto — selection + recording on all live dream paths"
```

---

### Task 4: dream_status extractor fields

**Files:**
- Modify: `pseudolife_memory/service.py` (`dream_status` ~line 2415)
- Modify: `pseudolife_memory/web/fixtures.py` (`dream_status` ~line 539)
- Test: `tests/test_extractor_fallback.py` (extend)

**Interfaces:**
- Consumes: `resolve_endpoints(cfg)`, `probe_endpoint(url)` (Tasks 1-2), `self._last_dream_extractor` (Task 3).
- Produces: `dream_status()` result gains `extractor_mode: str`, `primary_url: str | None`, `fallback_url: str | None`, `primary_healthy: bool | None` (None when no fallback configured — no probe cost on the common path), `last_dream_extractor: dict | None`. Task 5's frontend reads exactly these keys.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_extractor_fallback.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_extractor_fallback.py -x -q`
Expected: FAIL — `_status_extractor_fields` not defined.

- [ ] **Step 3: Implement the helper + wire dream_status**

In `pseudolife_memory/memory/dream.py`, after `build_extractor_with_fallback`, add:

```python
def _status_extractor_fields(cfg, last_dream_extractor) -> dict:
    """Extractor-visibility block for ``dream_status`` (console badge).
    Probes the primary ONLY when a fallback is configured — the inert
    single-extractor deploy pays no probe cost on a status poll."""
    r = resolve_endpoints(cfg)
    has_fallback = bool(r["fallback_url"] and r["fallback_model"])
    return {
        "extractor_mode": r["mode"],
        "primary_url": r["primary_url"],
        "fallback_url": r["fallback_url"] if has_fallback else None,
        "primary_healthy": (probe_endpoint(r["primary_url"], timeout=2.0)
                            if has_fallback and r["primary_url"] else None),
        "last_dream_extractor": last_dream_extractor,
    }
```

In `pseudolife_memory/service.py` `dream_status` (~line 2435), change the return to merge the block:

```python
        from pseudolife_memory.memory.dream import _status_extractor_fields
        return {"backlog": backlog, "idle_seconds": idle,
                "dream_cursor": cursor, "would_fire": would_fire,
                **_status_extractor_fields(
                    cfg, getattr(self, "_last_dream_extractor", None))}
```

In `pseudolife_memory/web/fixtures.py` `dream_status` (~line 539), replace the return with:

```python
        return {"backlog": 14, "idle_seconds": 2100.0, "dream_cursor": _NOW - 6 * _H,
                "would_fire": True, "extractor_mode": "auto",
                "primary_url": "http://host.docker.internal:8082/v1",
                "fallback_url": "http://pseudolife-extractor:8081/v1",
                "primary_healthy": True,
                "last_dream_extractor": {"which": "primary",
                                         "base_url": "http://host.docker.internal:8082/v1",
                                         "at": _NOW - 2 * _H}}
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_extractor_fallback.py tests/test_dream.py -q`
Expected: all pass (test_dream.py has dream_status assertions — additive keys must not break them; if one asserts the exact dict, extend that assertion with the new keys).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py pseudolife_memory/memory/dream.py pseudolife_memory/web/fixtures.py tests/test_extractor_fallback.py
git commit -m "feat(dream): extractor visibility fields on dream_status"
```

---

### Task 5: Console — override schema entries + extractor badge

**Files:**
- Modify: `pseudolife_memory/web/config_io.py` (Extractor group, after the max-tokens entry ~line 192)
- Modify: `pseudolife_memory/web/static/js/views/observatory.js` (`signalsStrip` ~line 60, `dreamPanel` ~line 131)
- Test: `tests/test_extractor_fallback.py` (extend — schema only; JS is devserver-verified)

**Interfaces:**
- Consumes: config paths `memory.dream.extractor_mode` / `memory.dream.fallback_base_url` / `memory.dream.fallback_model` (Task 1); `dream_status` keys `extractor_mode`, `primary_healthy`, `fallback_url`, `last_dream_extractor` (Task 4).
- Produces: three schema entries rendered by the existing settings UI; badge chips in the observatory.

- [ ] **Step 1: Write the failing schema test**

Append to `tests/test_extractor_fallback.py`:

```python
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
```

(The schema list in `config_io.py` is `KNOBS: list[dict[str, Any]]` at line 43.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_extractor_fallback.py::test_config_io_has_fallback_knobs -x -q`
Expected: FAIL — paths missing.

- [ ] **Step 3: Add the schema entries**

In `pseudolife_memory/web/config_io.py`, in the Extractor group directly after the `extractor_max_tokens` entry (~line 192), add:

```python
    {"path": "memory.dream.extractor_mode", "group": "Extractor",
     "label": "Extractor mode", "type": "enum", "default": "auto",
     "options": ["auto", "primary", "fallback"], "restart": False,
     "help": "auto = use the primary endpoint, fall back to the fallback "
             "endpoint when the primary probe fails; primary = never fall "
             "back (outages hold consolidation); fallback = skip the "
             "primary entirely (sovereign-only override). Effective only "
             "when settings source = config."},
    {"path": "memory.dream.fallback_base_url", "group": "Extractor",
     "label": "Fallback base URL", "type": "string", "format": "url",
     "default": None, "restart": False,
     "suggestions": ["http://pseudolife-extractor:8081/v1"],
     "help": "OpenAI-compatible /v1 endpoint used when the primary is "
             "unreachable (or mode = fallback). Empty disables selection "
             "entirely — single-extractor behavior. Effective only when "
             "settings source = config."},
    {"path": "memory.dream.fallback_model", "group": "Extractor",
     "label": "Fallback model", "type": "string", "default": None,
     "restart": False, "suggestions": ["extractor"],
     "help": "Model id the fallback endpoint expects (the bundled sidecar "
             "serves \"extractor\"). Effective only when settings source = "
             "config."},
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_extractor_fallback.py -q`
Expected: all pass.

- [ ] **Step 5: Add the badge to observatory.js**

In `pseudolife_memory/web/static/js/views/observatory.js`:

(a) In `signalsStrip` (~line 68), after the `dream ready`/`dream idle` chip, add one chip:

```javascript
    extractorChip(dream),
```

(b) In `dreamPanel`'s `panel-head` (~line 155), before the `fire ?` chip, add:

```javascript
      extractorChip(dream),
```

(c) Below `signalsStrip` (module scope), add the helper:

```javascript
// Which extractor serves the dream (2026-07-11 sonnet-sidecar-cutover).
// Renders nothing for single-extractor deploys (fallback_url unset).
function extractorChip(dream) {
  if (!dream.fallback_url) return null;
  const last = dream.last_dream_extractor;
  const mode = dream.extractor_mode || "auto";
  if (mode === "fallback")
    return el("span", { class: "chip warn", title: "forced via extractor mode" },
      "extractor: fallback (forced)");
  if (dream.primary_healthy === false)
    return el("span", { class: "chip bad",
      title: `primary ${dream.primary_url || ""} unreachable — dreams use the fallback` },
      "extractor: FALLBACK (primary down)");
  const lastNote = last ? ` · last dream: ${last.which}` : "";
  return el("span", { class: "chip ok", title: (dream.primary_url || "") + lastNote },
    "extractor: primary ✓");
}
```

- [ ] **Step 6: Verify in the devserver**

Run: `python -m pseudolife_memory.web.devserver` (fixtures mode), open the printed URL, check the Observatory: the signals strip and Dream consolidation panel each show "extractor: primary ✓" (fixtures set `primary_healthy: True`). Stop the devserver.

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/web/config_io.py pseudolife_memory/web/static/js/views/observatory.js tests/test_extractor_fallback.py
git commit -m "feat(console): extractor mode/fallback knobs + observatory badge"
```

---

### Task 6: Shim health honesty

**Files:**
- Modify: `evals/sonnet_shim.py` (`ClaudeCli` + `do_GET` /health branch)
- Test: `tests/test_sonnet_shim_health.py` (create)

**Interfaces:**
- Consumes: existing `ClaudeCli.chat`, `make_handler`.
- Produces: `ClaudeCli.health() -> tuple[bool, str]` (cached 300s); `/health` returns 200 `{"status": "ok"}` or 503 `{"status": "cli_error", "detail": ...}`. The Task 2 probe treats 503 as primary-down.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sonnet_shim_health.py`:

```python
"""sonnet_shim /health must reflect real CLI usability (a logged-out CLI
answers 503 so the daemon's fallback probe sees primary-down)."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "sonnet_shim", REPO / "evals" / "sonnet_shim.py")
shim = importlib.util.module_from_spec(spec)
sys.modules["sonnet_shim"] = shim
spec.loader.exec_module(shim)


def _cli(monkeypatch, chat_ok: bool):
    cli = shim.ClaudeCli(Path("claude.exe"), "m", 30.0)
    if chat_ok:
        monkeypatch.setattr(cli, "chat", lambda s, u: "OK")
    else:
        def _fail(s, u):
            raise RuntimeError("claude -p error result: Not logged in")
        monkeypatch.setattr(cli, "chat", _fail)
    return cli


def test_health_ok_when_cli_answers(monkeypatch):
    ok, detail = _cli(monkeypatch, True).health()
    assert ok is True


def test_health_fails_when_cli_errors(monkeypatch):
    ok, detail = _cli(monkeypatch, False).health()
    assert ok is False and "Not logged in" in detail


def test_health_result_is_cached(monkeypatch):
    cli = _cli(monkeypatch, True)
    assert cli.health()[0] is True
    calls = {"n": 0}

    def _boom(s, u):
        calls["n"] += 1
        raise RuntimeError("nope")
    monkeypatch.setattr(cli, "chat", _boom)
    assert cli.health()[0] is True          # served from cache
    assert calls["n"] == 0
    cli._health_at = time.monotonic() - 301  # expire the cache
    assert cli.health()[0] is False
    assert calls["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sonnet_shim_health.py -x -q`
Expected: FAIL — `ClaudeCli` has no `health`.

- [ ] **Step 3: Implement**

In `evals/sonnet_shim.py`:

(a) In `ClaudeCli.__init__`, add:

```python
        self._health_ok: bool | None = None
        self._health_detail = ""
        self._health_at = 0.0
```

(b) Add the method after `chat`:

```python
    _HEALTH_TTL = 300.0  # one trivial CLI call per 5 min keeps /health honest

    def health(self) -> tuple[bool, str]:
        """Real usability check: run a trivial completion so a logged-out or
        broken CLI turns /health into 503 (the daemon's fallback probe treats
        that as primary-down). Cached for _HEALTH_TTL seconds."""
        now = time.monotonic()
        if self._health_ok is not None and now - self._health_at < self._HEALTH_TTL:
            return self._health_ok, self._health_detail
        try:
            self.chat("", "Reply with exactly: OK")
            self._health_ok, self._health_detail = True, ""
        except Exception as e:  # noqa: BLE001 — any failure means unusable
            self._health_ok, self._health_detail = False, str(e)[:300]
        self._health_at = now
        return self._health_ok, self._health_detail
```

(c) In `make_handler`'s `do_GET`, replace the /health branch:

```python
            if self.path == "/health":
                ok, detail = cli.health()
                if ok:
                    self._json(200, {"status": "ok"})
                else:
                    self._json(503, {"status": "cli_error", "detail": detail})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_sonnet_shim_health.py tests/test_extractor_fallback.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add evals/sonnet_shim.py tests/test_sonnet_shim_health.py
git commit -m "feat(shim): /health runs a real CLI check (503 when logged out)"
```

---

### Task 7: Autostart script, docs, full suite

**Files:**
- Create: `ops/install-shim-autostart.ps1`
- Modify: `README.md` (extractor/upgrade section), `CHANGELOG.md` (Unreleased)

**Interfaces:**
- Consumes: `evals/sonnet_shim.py` CLI (`--port`, `--system-prompt-file`), the Task 1 env var names.
- Produces: Scheduled Task "Pseudolife Sonnet Shim"; documented cutover env values.

- [ ] **Step 1: Write the script**

Create `ops/install-shim-autostart.ps1`:

```powershell
# Register the Sonnet extractor shim to start at logon (Windows Task Scheduler).
#
#   ops\install-shim-autostart.ps1              # default port 8082, v1 prompt
#   ops\install-shim-autostart.ps1 -Port 8082 -PromptFile evals\prompts\sonnet_extractor_v1.md
#
# The shim wraps the Max-plan `claude` CLI as an OpenAI-compatible endpoint on
# 127.0.0.1 for the daemon's dream pass (primary extractor; the in-stack E4B
# container is the fallback — see docs/superpowers/specs/
# 2026-07-11-sonnet-sidecar-cutover-design.md). Requires a logged-in CLI.
param(
    [string]$PythonExe = "",
    [int]$Port = 8082,
    [string]$PromptFile = "evals\prompts\sonnet_extractor_v1.md",
    [string]$LogFile = "$env:USERPROFILE\.pseudolife-mcp\sonnet-shim.log"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if (-not $PythonExe) {
    $venv = Join-Path $repo ".venv\Scripts\python.exe"
    $PythonExe = (Test-Path $venv) ? $venv : (Get-Command python).Source
}
$promptPath = Join-Path $repo $PromptFile
if (-not (Test-Path $promptPath)) { throw "prompt file not found: $promptPath" }
New-Item -ItemType Directory -Force (Split-Path -Parent $LogFile) | Out-Null

$taskName = "Pseudolife Sonnet Shim"
$inner = "& '$PythonExe' '$repo\evals\sonnet_shim.py' --port $Port " +
         "--system-prompt-file '$promptPath' *>> '$LogFile'"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Registered + started '$taskName' (port $Port, log $LogFile)."
Write-Host "Cutover env for the daemon (.env or compose override):"
Write-Host "  PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$Port/v1"
Write-Host "  PSEUDOLIFE_DREAM_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto"
```

Note the autostart pattern is copied from `ops/install-autostart.ps1` — read it first and match any conventions this snippet missed (e.g. `-ErrorAction` styles).

- [ ] **Step 2: Verify the script parses**

Run: `pwsh -NoProfile -Command "& { $null = [scriptblock]::Create((Get-Content -Raw ops/install-shim-autostart.ps1)) ; 'parses' }"`
Expected: `parses`. (Do NOT run the script itself — registering the task is a deploy step for the operator.)

- [ ] **Step 3: Update README + CHANGELOG**

README.md: find the extractor/upgrading-the-extractor section (`grep -n "Upgrading the extractor" README.md`) and add a short subsection:

```markdown
### Optional: Sonnet primary with local fallback

With a Claude Max plan, the dream pass can use Claude Sonnet as its primary
extractor and keep the bundled local sidecar as an automatic fallback:

1. `ops\install-shim-autostart.ps1` — registers the CLI shim
   (`evals/sonnet_shim.py`) at logon on `127.0.0.1:8082` (requires a
   logged-in `claude` CLI).
2. Set in the daemon environment:
   `PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:8082/v1`,
   `PSEUDOLIFE_DREAM_MODEL=extractor`,
   `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1`,
   `PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor`,
   `PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto` (or `primary`/`fallback` to force
   a side — also switchable live in the Console's Extractor panel).

When the shim is unreachable or the CLI is logged out, dreams automatically
use the fallback; the Console's Observatory shows which extractor is active.
Leave `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL` unset to keep the existing
single-extractor behavior.
```

CHANGELOG.md, under Unreleased (create the section if absent):

```markdown
- Dream extractor primary/fallback selection: `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL`
  / `_FALLBACK_MODEL` / `_EXTRACTOR_MODE` (auto|primary|fallback), automatic
  fallback when the primary probe fails, extractor badge + override in the
  Console, `dream_status` extractor fields, shim `/health` CLI check,
  `ops/install-shim-autostart.ps1`. Inert until the fallback URL is set.
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -x -q`
Expected: all pass (724+ tests; PG-backed tests skip cleanly without a test server — that is fine).

- [ ] **Step 5: Commit**

```bash
git add ops/install-shim-autostart.ps1 README.md CHANGELOG.md
git commit -m "feat(ops): sonnet shim autostart script + cutover docs"
```

---

### Task 8: Deploy + live validation (environment-dependent — operator/controller runs this, not a sandboxed subagent)

**Files:** none (ops).

- [ ] **Step 1: Backup first** — `ops\backup.ps1` (per deploy discipline; see memory 2026-07-04: three backup-first deploys).
- [ ] **Step 2: Register the shim task** — `ops\install-shim-autostart.ps1`; confirm `curl http://127.0.0.1:8082/health` → 200.
- [ ] **Step 3: Set the five cutover env values** in the daemon's compose environment and recreate the daemon container (`docker compose up -d` — NEVER `down -v`).
- [ ] **Step 4: Validate primary path** — console Observatory shows "extractor: primary ✓"; trigger a dream (Run dream), result toast OK, daemon log shows the sonnet endpoint served it.
- [ ] **Step 5: Validate fallback** — stop the shim task (`Stop-ScheduledTask "Pseudolife Sonnet Shim"`), refresh Observatory (badge: "FALLBACK (primary down)"), run a dream (served by E4B), restart the task, badge returns to "primary ✓".
- [ ] **Step 6: Watch the first week** — eyeball dream logs for v1-prompt quality on live developer-workflow content (spec risk note).
