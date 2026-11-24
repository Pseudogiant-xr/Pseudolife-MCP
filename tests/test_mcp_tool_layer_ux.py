"""The frozen Python package's MCP tool layer: release-readiness checks
moved out of tests/test_release_ux.py (2026-07-04 review).

* ``memory_outcome`` REJECTS an unknown outcome instead of silently coercing
  it to ``"success"`` (which could invert a failure signal into a do-this
  lesson — the worst kind of silent failure);
* verb-dispatch and enum-shaped params expose ``enum`` values in the JSON
  schema (``typing.Literal``), so dispatch is discoverable from the manifest
  alone, not just the docstring prose;
* tool bodies that raise map to a structured MCP tool error (``isError``,
  ``{"error": <code>, "message": ...}``) instead of leaking raw exceptions or
  Python class names;
* the Console's list endpoints report ``total``/``truncated`` so big banks
  don't silently cap at the fetch limit.

These exercise only the frozen package, so the file is frozen and runs with
--include-frozen.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.helpers import (
    invoke_tool as _invoke,
    reload_mcp_filemode as _reload,
)


# ── memory_outcome: no silent coercion ────────────────────────────────────


def test_record_outcome_rejects_unknown_value_without_init() -> None:
    """An invalid outcome must be refused up front — never coerced to
    success — and the refusal must not require service init (no embedder)."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService.__new__(MemoryService)  # no __init__: proves the
    # validation path runs before any state (lock/init/storage) is touched.
    out = MemoryService.record_outcome(svc, "deploy thing", "failed")
    assert out["recorded"] is False
    assert out["reason"] == "unknown_outcome"
    assert out["outcomes"] == ["success", "failure", "correction"]


# ── schema enums: dispatch discoverable from the manifest ─────────────────

_EXPECTED_ENUMS = [
    ("memory_dream", "action", ["status", "pull", "commit", "run", "deep"]),
    ("memory_forget", "scope", ["memory", "fact", "world", "lesson"]),
    (
        "memory_graph_review", "action",
        ["list", "propose", "dismiss_pair", "accept_link", "reject_link",
         "accept_merge", "accept_junk", "reject_entity"],
    ),
    ("memory_outcome", "outcome", ["success", "failure", "correction"]),
    ("memory_world_set", "freshness_class", ["evergreen", "slow", "volatile"]),
    ("memory_store", "origin", ["user", "action", "agent"]),
    ("memory_fact_set", "origin", ["user", "action", "agent"]),
    # "auto" is the schema-v24 sentinel meaning "infer from entity kind" — a
    # Literal edit that silently drops it breaks the inferred-default contract.
    ("memory_fact_set", "freshness_class",
     ["auto", "evergreen", "slow", "volatile"]),
    # v35 write-time label pair — "auto" is the deterministic-heuristic
    # sentinel; dropping it from either Literal breaks the default contract.
    ("memory_store", "authority",
     ["auto", "directive", "observation", "quoted"]),
    ("memory_store", "distortion_tolerance",
     ["auto", "constraint", "procedural", "belief", "preference", "episodic"]),
    ("memory_fact_set", "authority",
     ["auto", "directive", "observation", "quoted"]),
    ("memory_fact_set", "distortion_tolerance",
     ["auto", "constraint", "procedural", "belief", "preference", "episodic"]),
]


def test_enum_params_are_enums_in_the_input_schema(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    for tool_name, param, values in _EXPECTED_ENUMS:
        schema = json.dumps(tools[tool_name].input_schema["properties"][param])
        for v in values:
            assert f'"{v}"' in schema, (
                f"{tool_name}.{param}: {v!r} not in schema — dispatch values "
                f"must be Literal-typed, not docstring-only")


# ── uniform failure contract ──────────────────────────────────────────────


def _tool_error(mod, name: str, args: dict) -> dict:
    """Call a tool through FastMCP and return its refusal payload, asserting
    the refusal reached the client as an MCP tool error (``isError``)."""
    result = asyncio.run(mod.mcp.call_tool(name, args))
    text = "".join(item.text for item in result.content if hasattr(item, "text"))
    assert result.is_error, f"{name} refusal came back success-shaped: {text}"
    payload = json.loads(text)
    assert result.structured_content in (None, payload)
    return payload


def test_tool_exceptions_become_structured_errors(tmp_path: Path, monkeypatch) -> None:
    """A service-level raise must surface as a structured MCP tool error
    (``isError``) with a stable code, not a raw exception string or a Python
    class name. A missing file names its path, which the caller gave."""
    mod = _reload(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.service, "ingest_document",
        lambda path, source=None: (_ for _ in ()).throw(
            FileNotFoundError(f"Not found: {path}")))
    out = _tool_error(mod, "document_ingest", {"path": "Z:/missing.pdf"})
    assert out["error"] == "file_not_found"
    assert "Z:/missing.pdf" in out["message"]
    assert "server" in out["message"]


def test_prose_value_errors_become_invalid_argument(tmp_path: Path, monkeypatch) -> None:
    """A ValueError whose text is a sentence keeps the sentence as the
    message under the stable code ``invalid_argument``. The call binds
    cleanly and the tool body refuses it, so this tests the body path."""
    mod = _reload(tmp_path, monkeypatch)
    asyncio.run(mod.mcp.call_tool(
        "memory_set_add", {"entity": "probe", "attribute": "tags", "member": "rust"}))
    out = _tool_error(mod, "memory_fact_set",
                      {"entity": "probe", "attribute": "tags", "value": "go"})
    assert out["error"] == "invalid_argument"
    assert "memory_set_add" in out["message"]
    assert "ValueError" not in json.dumps(out)


# Pydantic's own framing: what a binding refusal must never carry.
_BINDING_LEAKS = ("input_value", "input_type", "errors.pydantic.dev", "validation error")


def test_binding_rejections_share_the_error_shape(tmp_path: Path, monkeypatch) -> None:
    """An argument the schema refuses (wrong type here) never reaches the
    tool body, yet comes back as the same JSON tool error as a body
    refusal: ``invalid_argument`` naming the parameter, with neither the
    value passed nor pydantic's framing (review of #587/#588, 2026-10-05)."""
    mod = _reload(tmp_path, monkeypatch)
    calls: list = []
    monkeypatch.setattr(mod.service, "search", lambda *a, **k: calls.append(k))
    value = "PROBE" + "VALUE" * 3  # built at runtime: must not be echoed
    out = _tool_error(mod, "memory_search", {"query": "q", "top_k": value})
    assert out["error"] == "invalid_argument"
    assert out["param"] == "top_k"
    assert "top_k" in out["message"] and "integer" in out["message"]
    text = json.dumps(out)
    for leak in (value, *_BINDING_LEAKS):
        assert leak not in text, text
    assert set(out) <= {"error", "message", "param"}
    assert calls == []


def test_binding_rejections_name_every_argument(tmp_path: Path, monkeypatch) -> None:
    """Several refused arguments are each named in the message; ``param``
    is the first of them."""
    mod = _reload(tmp_path, monkeypatch)
    out = _tool_error(mod, "memory_search", {"top_k": "many"})
    assert out["error"] == "invalid_argument"
    assert out["param"] in {"query", "top_k"}
    assert "query" in out["message"] and "top_k" in out["message"]
    assert "many" not in out["message"]


def test_unknown_parameter_is_its_own_code(tmp_path: Path, monkeypatch) -> None:
    """A misnamed argument keeps #584's sentence and gains the shape's
    fields: ``error: unknown_parameter``, the name, and what is accepted."""
    mod = _reload(tmp_path, monkeypatch)
    out = _tool_error(mod, "memory_search", {"query": "q", "limit": 3})
    assert out["error"] == "unknown_parameter"
    assert out["param"] == "limit"
    assert "top_k" in out["accepted"] and "query" in out["accepted"]
    assert "limit" not in out["accepted"]
    assert out["message"].startswith(
        "unknown parameter 'limit' for memory_search; did you mean 'top_k'?")
    assert out["message"].count("Accepted:") == 1
    for leak in _BINDING_LEAKS:
        assert leak not in json.dumps(out)


def test_an_unavailable_board_leaves_a_write_in_doubt(tmp_path: Path, monkeypatch) -> None:
    """``coordination_unavailable`` is what an unexpected failure inside a
    board call becomes. It is a ValueError like any refusal, but the write
    may have happened, so a write tool says so; a read-only one does not."""
    from pseudolife_memory.coordination import CoordinationRefused

    mod = _reload(tmp_path, monkeypatch)
    out = mod._error_payload("memory_message",
                             CoordinationRefused("coordination_unavailable"))
    assert out["error"] == "coordination_unavailable"
    assert out["mutation"] == "unknown"
    refused = mod._error_payload("memory_message", CoordinationRefused("missing_parameter"))
    assert "mutation" not in refused
    read = mod._error_payload("memory_search",
                              CoordinationRefused("coordination_unavailable"))
    assert "mutation" not in read


def test_coded_value_errors_keep_their_code(tmp_path: Path, monkeypatch) -> None:
    """A ValueError raised with a snake_case code keeps that code as
    ``error``: other callers already key on it."""
    mod = _reload(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.service, "ingest_document",
        lambda path, source=None: (_ for _ in ()).throw(
            ValueError("coordination_requires_postgres")))
    out = _tool_error(mod, "document_ingest", {"path": "Z:/x.pdf"})
    assert out["error"] == "coordination_requires_postgres"
    assert "coordination_requires_postgres" in out["message"]


def test_unexpected_exceptions_become_internal_error(tmp_path: Path, monkeypatch) -> None:
    """An unexpected exception names neither its class nor its text (both
    are internals) and tells the model not to retry blindly; a write tool
    says it cannot tell whether anything was written."""
    mod = _reload(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.service, "ingest_document",
        lambda path, source=None: (_ for _ in ()).throw(
            RuntimeError("row 42 holds private text")))
    out = _tool_error(mod, "document_ingest", {"path": "Z:/x.pdf"})
    assert out["error"] == "internal_error"
    assert "RuntimeError" not in json.dumps(out)
    assert "private text" not in json.dumps(out)
    assert "retry" in out["message"]
    assert out["mutation"] == "unknown"
    # A read-only tool's failure wrote nothing, so it says nothing about it.
    monkeypatch.setattr(
        mod.service, "search_documents",
        lambda query, top_k=5: (_ for _ in ()).throw(RuntimeError("boom")))
    read = _tool_error(mod, "document_search", {"query": "x"})
    assert read["error"] == "internal_error" and "mutation" not in read


def test_search_always_returns_cortex_key(tmp_path: Path, monkeypatch) -> None:
    """``cortex`` is documented in the return shape — it must be an empty
    list on a miss, not a missing key (``result["cortex"]`` KeyError'd)."""
    _reload(tmp_path, monkeypatch)
    out = _invoke("memory_search", {"query": "nothing stored about this"})
    assert out["cortex"] == []


def test_core_tier_can_close_its_own_loops(tmp_path: Path, monkeypatch) -> None:
    """Core-mode gaps: memory_fact_get (core) surfaces source_entries ids, so
    memory_get must be core to dereference them; the recommended workflow
    names the session early, so memory_session_title must be core."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "core")
    mod = _reload(tmp_path, monkeypatch)
    names = {t.name for t in asyncio.run(mod.mcp.list_tools())}
    assert {"memory_get", "memory_session_title"} <= names


# ── Console list endpoints: no silent truncation ──────────────────────────


@pytest.fixture()
def routes():
    from pseudolife_memory.web.fixtures import FixtureService
    from pseudolife_memory.web.routes import ConsoleRoutes

    return ConsoleRoutes(FixtureService())


@pytest.mark.parametrize("path", ["/api/facts", "/api/world", "/api/lessons"])
def test_list_endpoints_report_total_and_truncated(routes, path) -> None:
    full = routes.dispatch("GET", path, {}, {})
    assert full["total"] == full["count"]
    assert full["truncated"] is False
    assert full["total"] >= 2, f"fixture bank too small to exercise {path}"

    capped = routes.dispatch("GET", path, {"limit": "1"}, {})
    assert capped["count"] == 1
    assert capped["total"] == full["total"]
    assert capped["truncated"] is True
