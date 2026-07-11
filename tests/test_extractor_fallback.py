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
