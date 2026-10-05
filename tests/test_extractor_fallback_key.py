"""Each dream extractor endpoint receives only its own API key.

``PSEUDOLIFE_DREAM_API_KEY`` / ``extractor_api_key`` authenticates the
PRIMARY endpoint. Before 2026-09-25 the fallback extractor was built with the
same key, so a hosted primary's provider key rode every fallback dream as a
bearer header to the fallback host — typically the in-stack sidecar over plain
HTTP, which needs no key at all. The fallback now sends its own key
(``PSEUDOLIFE_DREAM_FALLBACK_API_KEY`` / ``fallback_api_key``) or none.

These tests stand up a real primary and a real fallback on loopback and
assert which ``Authorization`` header each one receives on the wire, through
every path that builds a fallback extractor: forced ``fallback`` mode, ``auto``
with the primary down, and the review-queue judge's model swap.
"""
from __future__ import annotations

import http.server
import json
import threading
from types import SimpleNamespace

import pytest

from pseudolife_memory.memory import dream as D
from pseudolife_memory.utils.config import DeepDreamConfig, DreamConfig

PRIMARY_KEY = "sk-primary-canary"
FALLBACK_KEY = "sk-fallback-canary"

_REPLY = json.dumps({"model": "m", "choices": [
    {"message": {"content": json.dumps({"claims": []})}}]}).encode()


class _Endpoint(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint that records every POST's bearer.
    ``health_status`` 503 makes the auto-mode probe read it as down (the
    shim's logged-out answer)."""
    health_status = 200
    posts: list[str | None] = []

    def do_GET(self):  # noqa: N802
        status = type(self).health_status if self.path == "/health" else 200
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        type(self).posts.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_REPLY)))
        self.end_headers()
        self.wfile.write(_REPLY)

    def log_message(self, *a):
        pass


class _Primary(_Endpoint):
    posts: list[str | None] = []


class _Fallback(_Endpoint):
    posts: list[str | None] = []


def _serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def servers():
    primary, fallback = _serve(_Primary), _serve(_Fallback)
    yield SimpleNamespace(
        primary=f"http://127.0.0.1:{primary.server_address[1]}/v1",
        fallback=f"http://127.0.0.1:{fallback.server_address[1]}/v1")
    for srv in (primary, fallback):
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def wire(servers, monkeypatch):
    _Primary.posts.clear()
    _Fallback.posts.clear()
    _Primary.health_status = 200
    monkeypatch.setattr(D, "_probe_retry_delay", 0.0)
    monkeypatch.setenv("PSEUDOLIFE_DREAM_API_KEY", PRIMARY_KEY)
    yield servers
    _Primary.health_status = 200


def _cfg(wire, mode="auto", **kw):
    return DreamConfig(extractor_source="config",
                       extractor_base_url=wire.primary, extractor_model="pm",
                       fallback_base_url=wire.fallback, fallback_model="fm",
                       extractor_mode=mode, **kw)


def _dream_once(cfg):
    ex, which = D.build_extractor_with_fallback(cfg)
    ex.extract(["the note"], [])
    return which


def test_forced_fallback_never_receives_the_primary_key(wire):
    assert _dream_once(_cfg(wire, mode="fallback")) == "fallback"
    assert _Fallback.posts == [None]
    assert _Primary.posts == []


def test_auto_fallback_never_receives_the_primary_key(wire):
    _Primary.health_status = 503            # primary down -> fallback
    assert _dream_once(_cfg(wire)) == "fallback"
    assert _Fallback.posts == [None]
    assert _Primary.posts == []


def test_primary_still_receives_its_own_key(wire, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_DREAM_FALLBACK_API_KEY", FALLBACK_KEY)
    assert _dream_once(_cfg(wire)) == "primary"
    assert _Primary.posts == [f"Bearer {PRIMARY_KEY}"]
    assert _Fallback.posts == []


@pytest.mark.parametrize("mode", ["auto", "primary"])
def test_primary_never_receives_the_fallback_key(wire, monkeypatch, mode):
    # The reverse direction: a keyless primary (a local model server) beside
    # a keyed fallback must not borrow the fallback's key either.
    monkeypatch.delenv("PSEUDOLIFE_DREAM_API_KEY")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_FALLBACK_API_KEY", FALLBACK_KEY)
    assert _dream_once(_cfg(wire, mode=mode)) == "primary"
    assert _Primary.posts == [None]
    assert _Fallback.posts == []


@pytest.mark.parametrize("mode", ["auto", "fallback"])
def test_fallback_env_key_goes_to_the_fallback_only(wire, monkeypatch, mode):
    monkeypatch.setenv("PSEUDOLIFE_DREAM_FALLBACK_API_KEY", FALLBACK_KEY)
    _Primary.health_status = 503
    assert _dream_once(_cfg(wire, mode=mode)) == "fallback"
    assert _Fallback.posts == [f"Bearer {FALLBACK_KEY}"]
    assert _Primary.posts == []


@pytest.mark.parametrize("source", ["env", "config"])
def test_fallback_api_key_config_field_is_honoured(wire, source):
    # Same ownership rule as the primary key: honoured in both settings-source
    # modes (in "env" mode the unset endpoint env vars defer to these config
    # values), the env var winning when set (secrets belong in the env).
    cfg = _cfg(wire, mode="fallback", fallback_api_key=FALLBACK_KEY)
    cfg.extractor_source = source
    assert _dream_once(cfg) == "fallback"
    assert _Fallback.posts == [f"Bearer {FALLBACK_KEY}"]


def test_fallback_env_key_wins_over_config_field(wire, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_DREAM_FALLBACK_API_KEY", "sk-env-wins")
    cfg = _cfg(wire, mode="fallback", fallback_api_key=FALLBACK_KEY)
    assert _dream_once(cfg) == "fallback"
    assert _Fallback.posts == ["Bearer sk-env-wins"]


def test_judge_model_swap_on_the_fallback_keeps_the_fallback_key(wire):
    # The review-queue judge rebuilds the selected extractor under its
    # second-opinion model and carries that extractor's key over — so on a
    # fallback dream it must carry the fallback's (here: none), never the
    # primary's.
    from pseudolife_memory.service_dream import DreamOps

    _Primary.health_status = 503
    host = SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(
        deep_dream=DeepDreamConfig(), dream=_cfg(wire))))
    ex = DreamOps._judge_extractor(host, method="extract", model="judge-2")
    assert ex.model == "judge-2" and ex.base_url == wire.fallback
    ex.extract(["the note"], [])
    assert _Fallback.posts == [None]
    assert _Primary.posts == []


def test_dreamconfig_fallback_api_key_defaults_unset():
    assert DreamConfig().fallback_api_key is None


def test_compose_forwards_the_fallback_key_to_the_daemon():
    # Compose passes the daemon only the variables its environment block
    # lists, so without this entry a key set in ops/.env never reaches the
    # Docker daemon and the fallback silently sends none.
    from pathlib import Path

    import yaml

    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "ops"
                              / "docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"]["pseudolife-daemon"]["environment"]
    assert env.get("PSEUDOLIFE_DREAM_FALLBACK_API_KEY") == (
        "${PSEUDOLIFE_DREAM_FALLBACK_API_KEY:-}")
