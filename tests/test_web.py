"""Tests for the Cortex Console web layer — config_io, routes, ASGI.

These use the lightweight ``FixtureService`` (no Postgres, no warm-service
fixture or database). Note: ``FixtureService`` constructs ``AppConfig``, which
transitively imports torch (``preset_bands`` -> the memory package -> ``cms``),
so these tests require torch installed and run under ``.venv``.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml

from pseudolife_memory.web import config_io
from pseudolife_memory.web.api import build_console_app
from pseudolife_memory.web.fixtures import FixtureService
from pseudolife_memory.web.routes import ConsoleRoutes


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
    s = FixtureService()
    s.data_dir = tmp_path          # config writes land in tmp, not the package
    return s


# ── config_io ───────────────────────────────────────────────────────────────

def test_read_config_groups(svc):
    cfg = config_io.read_config(svc)
    paths = [k["path"] for g in cfg["groups"] for k in g["knobs"]]
    assert "memory.surprise_threshold" in paths
    assert len(paths) == len(config_io.KNOBS)
    for g in cfg["groups"]:
        for k in g["knobs"]:
            assert "value" in k and "type" in k


def test_write_config_roundtrip_live(svc):
    res = config_io.write_config(svc, {"memory.top_k": 11})
    assert "memory.top_k" in res["applied"]
    assert svc.config.memory.top_k == 11
    with open(res["config_path"], encoding="utf-8") as f:
        assert yaml.safe_load(f)["memory"]["top_k"] == 11


def test_write_config_restart_classification(svc):
    res = config_io.write_config(svc, {"memory.dream.sweep_interval_seconds": 300})
    assert "memory.dream.sweep_interval_seconds" in res["restart_required"]
    assert "memory.dream.sweep_interval_seconds" not in res["applied"]


def test_write_config_makes_backup_on_second_write(svc):
    config_io.write_config(svc, {"memory.top_k": 9})
    res = config_io.write_config(svc, {"memory.top_k": 10})
    assert res["backup"] and res["backup"].endswith(".bak")


def test_write_config_preserves_unmanaged_keys(svc):
    cfg_path = config_io.config_path_for(svc)
    cfg_path.write_text(yaml.safe_dump({"backend": "lmstudio", "memory": {"top_k": 8}}), encoding="utf-8")
    config_io.write_config(svc, {"memory.surprise_threshold": 0.2})
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["backend"] == "lmstudio"                 # untouched
    assert data["memory"]["top_k"] == 8                  # untouched
    assert data["memory"]["surprise_threshold"] == 0.2   # written


@pytest.mark.parametrize("patch", [
    {"memory.nonexistent": 1},                       # unknown knob
    {"memory.cortex.guard_min_score": 5},            # > max
    {"memory.top_k": -1},                            # < min
    {"memory.recall.driver": "bogus"},               # invalid enum
    {},                                              # empty patch
])
def test_write_config_rejects_bad_input(svc, patch):
    with pytest.raises(ValueError):
        config_io.write_config(svc, patch)


def test_write_config_bool_coercion(svc):
    config_io.write_config(svc, {"memory.show_superseded": "true"})
    assert svc.config.memory.show_superseded is True


# ── routes ──────────────────────────────────────────────────────────────────

def test_routes_dispatch_reads(svc):
    r = ConsoleRoutes(svc)
    ov = r.dispatch("GET", "/api/overview", {}, {})
    assert ov["counts"]["facts"] == len(svc.cortex_dump()["entries"])
    assert "entries" in r.dispatch("GET", "/api/facts", {}, {})
    assert "nodes" in r.dispatch("GET", "/api/graph", {}, {})
    assert "would_fire" in r.dispatch("GET", "/api/dream/status", {}, {})


def test_overview_has_facts_by_origin(svc):
    ov = ConsoleRoutes(svc).dispatch("GET", "/api/overview", {}, {})
    assert "facts_by_origin" in ov["counts"]
    assert isinstance(ov["counts"]["facts_by_origin"], dict)


def test_routes_search_params(svc):
    out = ConsoleRoutes(svc).dispatch("GET", "/api/search", {"q": "recall"}, {})
    assert "entries" in out and "count" in out


def test_routes_graph_insight_dispatch(svc):
    r = ConsoleRoutes(svc)
    dig = r.dispatch("GET", "/api/graph/digest", {}, {})
    assert "available" in dig
    comms = r.dispatch("GET", "/api/graph/communities", {}, {})
    assert "communities" in comms
    members = r.dispatch("GET", "/api/graph/communities", {"id": "0"}, {})
    assert "members" in members
    path = r.dispatch("GET", "/api/graph/path", {"source": "a", "target": "b"}, {})
    assert "found" in path and "path" in path


def test_graph_scope_param_dispatches(svc):
    r = ConsoleRoutes(svc)
    out = r.dispatch("GET", "/api/graph", {"scope": "all"}, {})
    assert out["found"] is True
    assert all("sources" in n for n in out["nodes"])


def test_graph_projects_route(svc):
    r = ConsoleRoutes(svc)
    out = r.dispatch("GET", "/api/graph/projects", {}, {})
    assert "projects" in out and isinstance(out["projects"], list)


def test_routes_entry_and_reinforce(svc):
    r = ConsoleRoutes(svc)
    entry = r.dispatch("GET", "/api/entry", {"id": "1"}, {})
    assert "consolidated_into" in entry and "reinforcements" in entry
    out = r.dispatch("POST", "/api/reinforce", {}, {"entry_id": 1})
    assert isinstance(out, dict)


def test_routes_unknown_raises_keyerror(svc):
    with pytest.raises(KeyError):
        ConsoleRoutes(svc).dispatch("GET", "/api/bogus", {}, {})


def test_routes_has(svc):
    r = ConsoleRoutes(svc)
    assert r.has("/api/facts")
    assert not r.has("/api/bogus")


def test_routes_config_write_via_dispatch(svc):
    out = ConsoleRoutes(svc).dispatch("POST", "/api/config", {}, {"patch": {"memory.top_k": 13}})
    assert "memory.top_k" in out["applied"]


# ── ASGI app ────────────────────────────────────────────────────────────────

async def _stub_mcp(scope, receive, send):
    await send({"type": "http.response.start", "status": 501, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _app(svc, token=None):
    return build_console_app(_stub_mcp, token, lambda: {"status": "ok"}, svc)


def _call(app, method, path, headers=None, body=b""):
    async def run():
        scope = {"type": "http", "method": method, "path": path,
                 "query_string": b"", "headers": headers or []}

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        out = {"status": None, "body": bytearray()}

        async def send(m):
            if m["type"] == "http.response.start":
                out["status"] = m["status"]
            elif m["type"] == "http.response.body":
                out["body"].extend(m.get("body", b""))

        await app(scope, receive, send)
        return out["status"], bytes(out["body"])

    return asyncio.run(run())


def test_asgi_health_open(svc):
    st, _ = _call(_app(svc), "GET", "/health")
    assert st == 200


def test_devserver_health_reports_real_schema():
    import json

    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION
    from pseudolife_memory.web.devserver import build_dev_app

    st, body = _call(build_dev_app(), "GET", "/health")
    assert st == 200
    assert json.loads(body)["schema"] == SCHEMA_META_VERSION


def test_asgi_api_overview(svc):
    st, body = _call(_app(svc), "GET", "/api/overview")
    assert st == 200 and b"counts" in body


def test_asgi_unknown_api_404(svc):
    st, _ = _call(_app(svc), "GET", "/api/bogus")
    assert st == 404


def test_asgi_wrong_verb_405(svc):
    # /api/facts is GET-only
    st, _ = _call(_app(svc), "POST", "/api/facts")
    assert st == 405


def test_asgi_auth_gate(svc):
    app = _app(svc, token="secret")
    assert _call(app, "GET", "/api/overview")[0] == 401
    assert _call(app, "GET", "/api/overview",
                 headers=[(b"authorization", b"Bearer secret")])[0] == 200
    # static + health stay open even with a token set
    assert _call(app, "GET", "/health")[0] == 200
    assert _call(app, "GET", "/ui/")[0] == 200


def test_asgi_static_index(svc):
    st, body = _call(_app(svc), "GET", "/ui/")
    assert st == 200 and b"Cortex Console" in body


def test_asgi_static_traversal_blocked(svc):
    st, _ = _call(_app(svc), "GET", "/ui/../../../etc/passwd")
    assert st == 403


def test_asgi_root_redirects(svc):
    st, _ = _call(_app(svc), "GET", "/")
    assert st == 307
