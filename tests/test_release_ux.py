"""Release-readiness UX hardening (2026-07-04 review).

Pins the fixes from the pre-release UI/UX pass:

* ``memory_outcome`` REJECTS an unknown outcome instead of silently coercing
  it to ``"success"`` (which could invert a failure signal into a do-this
  lesson — the worst kind of silent failure);
* verb-dispatch and enum-shaped params expose ``enum`` values in the JSON
  schema (``typing.Literal``), so dispatch is discoverable from the manifest
  alone, not just the docstring prose;
* tool bodies that raise map to the same structured ``{"error": ...}`` shape
  the dispatch tools already return, instead of leaking raw exceptions;
* the Console's list endpoints report ``total``/``truncated`` so big banks
  don't silently cap at the fetch limit;
* README version claims are mechanically guarded against drift (the schema
  version went stale three separate times when hand-edited).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_tool_consolidation import _invoke, _reload


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

_EXPECTED_ENUMS = {
    "memory_dream": ("action", ["status", "pull", "commit", "run", "deep"]),
    "memory_forget": ("scope", ["memory", "fact", "world", "lesson"]),
    "memory_graph_review": (
        "action",
        ["list", "propose", "dismiss_pair", "accept_link", "reject_link",
         "accept_merge", "accept_junk", "reject_entity"],
    ),
    "memory_outcome": ("outcome", ["success", "failure", "correction"]),
    "memory_world_set": ("freshness_class", ["evergreen", "slow", "volatile"]),
    "memory_store": ("origin", ["user", "action", "agent"]),
    "memory_fact_set": ("origin", ["user", "action", "agent"]),
}


def test_enum_params_are_enums_in_the_input_schema(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    for tool_name, (param, values) in _EXPECTED_ENUMS.items():
        schema = json.dumps(tools[tool_name].inputSchema["properties"][param])
        for v in values:
            assert f'"{v}"' in schema, (
                f"{tool_name}.{param}: {v!r} not in schema — dispatch values "
                f"must be Literal-typed, not docstring-only")


# ── uniform failure contract ──────────────────────────────────────────────


def test_tool_exceptions_become_structured_errors(tmp_path: Path, monkeypatch) -> None:
    """A service-level raise must surface as the same ``{"error": ...}``
    shape the dispatch tools return — not a raw exception string."""
    mod = _reload(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.service, "ingest_document",
        lambda path, source=None: (_ for _ in ()).throw(
            FileNotFoundError(f"Not found: {path}")))
    out = _invoke("document_ingest", {"path": "Z:/missing.pdf"})
    assert out["error"] == "FileNotFoundError"
    assert "Z:/missing.pdf" in out["message"]


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


# ── README version claims: mechanical drift guard ─────────────────────────

_README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_schema_version_matches_code() -> None:
    """The schema version in README went stale three times (v11→13→19/20 vs
    21) when hand-edited. Every explicit 'current schema' claim must match
    ``SCHEMA_META_VERSION``."""
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    text = _README.read_text(encoding="utf-8")
    claims = re.findall(r"\| Schema version \| v(\d+)", text)
    assert claims, "README capabilities table must state the schema version"
    assert all(int(c) == SCHEMA_META_VERSION for c in claims), (
        f"README says schema v{claims}, code says v{SCHEMA_META_VERSION}")
    dsn = re.findall(r"source of truth \(schema v(\d+)\)", text)
    assert all(int(c) == SCHEMA_META_VERSION for c in dsn), (
        f"README DSN row says v{dsn}, code says v{SCHEMA_META_VERSION}")


def test_readme_makes_no_hardcoded_test_count_claims() -> None:
    """Test counts (384→514→547→834...) go stale within weeks. The README
    must not claim a specific suite size."""
    text = _README.read_text(encoding="utf-8")
    stale = re.findall(r"\b\d{3,4}(?:\+)? tests\b", text)
    assert stale == [], f"hardcoded test-count claims in README: {stale}"


def test_readme_documents_claude_mcp_wiring() -> None:
    """A newcomer must be able to wire the daemon into Claude Code from the
    README alone — the one-liner and/or the config file name."""
    text = _README.read_text(encoding="utf-8")
    assert "claude mcp add" in text
    assert ".mcp.json" in text


def test_tracked_tree_carries_no_maintainer_identifiers() -> None:
    """The 2026-07-03 history scrub regressed within a week: a test fixture
    re-asserted the maintainer's scrubbed email verbatim, and docs/eval
    harnesses accumulated ``C:\\Users\\<username>`` paths. A history rewrite
    is one-shot; keeping the tree clean is a treadmill — so guard the tracked
    tree mechanically. The needles are assembled from fragments so this file
    passes its own check.

    The maintainer's homelab subnet is banned wholesale: synthetic RFC1918
    fixtures must use ``192.168.1.x`` (or ``192.168.x.x`` placeholders),
    never the real ``.0.x`` subnet that leaked via eval-harness defaults."""
    needles = [("HAM" "O9").lower(), "pseudogiant" + "92", "192.168." + "0."]
    repo = Path(__file__).resolve().parents[1]
    try:
        proc = subprocess.run(["git", "ls-files"], cwd=repo, check=True,
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    hits = []
    for rel in proc.stdout.splitlines():
        try:
            text = (repo / rel).read_text(encoding="utf-8",
                                          errors="ignore").lower()
        except OSError:
            continue
        if any(n in text for n in needles):
            hits.append(rel)
    assert hits == [], f"maintainer identifiers in tracked files: {hits}"


def test_changelog_mentions_current_schema_version() -> None:
    """A schema bump must be chronicled: v22 initially shipped with no
    CHANGELOG entry (2026-07-12), caught only in post-deploy review. Every
    bump of ``SCHEMA_META_VERSION`` forces a matching ``vNN`` mention in
    CHANGELOG.md — old mentions accumulate harmlessly; only the current
    version is checked."""
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    changelog = (_README.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"\bv{SCHEMA_META_VERSION}\b", changelog), (
        f"schema is v{SCHEMA_META_VERSION} but CHANGELOG.md never mentions "
        f"v{SCHEMA_META_VERSION} — add an entry under [Unreleased]")
