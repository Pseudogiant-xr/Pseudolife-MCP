"""MCP tool-surface consolidation (2026-07-02 review, final item).

The full-mode manifest was 55 tools / ~37k chars of descriptions (~10k tokens
of agent context every session) and split single workflows across many verbs.
These tests pin the consolidated contract:

* the dream lifecycle is ONE verb-dispatched tool: ``memory_dream(action=...)``
  (status / pull / commit / run / deep — absorbing memory_deep_dream);
* deletion is ONE tool across all four stores: ``memory_forget(scope=...)``
  (memory / fact / world / lesson);
* the graph review queue is ONE tool: ``memory_graph_review(action=...)``;
* dump/introspection tools left the MCP surface — the Cortex Console and the
  ``pseudolife-mcp briefing`` CLI cover them; ``memory_path`` folded into
  ``memory_graph(to=...)``;
* every remaining description is terse: <=1600 chars each, <=18k total.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


def _reload(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    return mod


def _invoke(tool_name: str, args: dict) -> dict:
    from pseudolife_memory import mcp_server  # noqa: PLC0415

    result = asyncio.run(mcp_server.mcp.call_tool(tool_name, args))
    if isinstance(result, tuple):
        content, structured = result
    else:
        content, structured = result, None
    if structured is not None:
        return structured
    return json.loads("".join(i.text for i in content if hasattr(i, "text")))


# ── memory_dream(action=...) ──────────────────────────────────────────────


def test_dream_status_pull_commit_run_via_one_tool(tmp_path: Path, monkeypatch) -> None:
    _reload(tmp_path, monkeypatch)

    _invoke("memory_store", {"text": "the beacon port is 7777", "source": "notes"})

    status = _invoke("memory_dream", {"action": "status"})
    assert "backlog" in status and "would_fire" in status

    pulled = _invoke("memory_dream", {"action": "pull"})
    assert "cursor" in pulled and "entries" in pulled

    ran = _invoke("memory_dream", {"action": "run"})
    assert "pulled" in ran and "cursor" in ran

    committed = _invoke("memory_dream", {"action": "commit", "cursor": pulled["cursor"]})
    assert "dream_cursor" in committed


def test_dream_commit_requires_cursor(tmp_path: Path, monkeypatch) -> None:
    _reload(tmp_path, monkeypatch)
    out = _invoke("memory_dream", {"action": "commit"})
    assert out.get("error") == "cursor_required"


def test_dream_deep_delegates_with_apply_flag(tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    seen: list[bool] = []
    monkeypatch.setattr(
        mod.service, "deep_dream",
        lambda apply=False, include_snippets=True:
            (seen.append(apply), {"dry_run": not apply})[1])
    assert _invoke("memory_dream", {"action": "deep"})["dry_run"] is True
    assert _invoke("memory_dream", {"action": "deep", "apply": True})["dry_run"] is False
    assert seen == [False, True]


def test_dream_unknown_action_is_a_structured_error(tmp_path: Path, monkeypatch) -> None:
    _reload(tmp_path, monkeypatch)
    out = _invoke("memory_dream", {"action": "snooze"})
    assert out.get("error") == "unknown_action"
    assert "status" in out.get("actions", [])


# ── memory_forget(scope=...) ──────────────────────────────────────────────


def test_forget_scope_fact_purges_the_slot(tmp_path: Path, monkeypatch) -> None:
    _reload(tmp_path, monkeypatch)
    _invoke("memory_fact_set", {"entity": "project", "attribute": "language",
                                "value": "rust", "origin": "user"})
    out = _invoke("memory_forget", {"scope": "fact", "entity": "project"})
    assert out["removed"] == 1
    got = _invoke("memory_fact_get", {"entity": "project", "attribute": "language"})
    assert got["record"] is None


def test_forget_scope_memory_deletes_matching_entries(tmp_path: Path, monkeypatch) -> None:
    _reload(tmp_path, monkeypatch)
    _invoke("memory_store", {"text": "Junk", "source": "test"})
    _invoke("memory_store", {"text": "Keep", "source": "test"})
    out = _invoke("memory_forget", {"scope": "memory", "text": "Junk"})
    assert out["deleted_count"] == 1
    texts = [e["text"] for e in _invoke("memory_recent", {"n": 10})["entries"]]
    assert "Junk" not in texts and "Keep" in texts


def test_forget_scope_world_and_lesson(tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _invoke("memory_world_set", {"entity": "acme", "attribute": "ceo",
                                 "value": "jane", "source_url": "https://x.test/a"})
    out = _invoke("memory_forget", {"scope": "world", "entity": "acme"})
    assert out["removed"] == 1

    mod.service.lesson_write("deploy-thing", "approach", "backup first")
    out = _invoke("memory_forget", {"scope": "lesson", "entity": "deploy-thing"})
    assert out["removed"] >= 1


def test_forget_validates_scope_and_required_args(tmp_path: Path, monkeypatch) -> None:
    _reload(tmp_path, monkeypatch)
    assert _invoke("memory_forget", {"scope": "everything"}).get("error") == "unknown_scope"
    assert _invoke("memory_forget", {"scope": "fact"}).get("error") == "entity_required"
    # scope=memory with no filter: service refuses wholesale deletion.
    out = _invoke("memory_forget", {"scope": "memory"})
    assert "error" in out


# ── memory_graph_review(action=...) ───────────────────────────────────────


def test_graph_review_actions_route_to_the_right_service_calls(
    tmp_path: Path, monkeypatch,
) -> None:
    mod = _reload(tmp_path, monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(mod.service, "graph_review",
                        lambda scope=None: calls.append(("list", scope)) or {"findings": []})
    monkeypatch.setattr(mod.service, "graph_propose_links",
                        lambda proposals: calls.append(("propose", len(proposals))) or {"proposed": len(proposals)})
    monkeypatch.setattr(mod.service, "graph_accept_proposal",
                        lambda pid: calls.append(("accept_link", pid)) or {"accepted": True})
    monkeypatch.setattr(mod.service, "graph_reject_proposal",
                        lambda pid: calls.append(("reject_link", pid)) or {"rejected": True})
    # accept_merge / reject_entity are decision actions: the MCP layer stamps
    # decided_by="agent" so the audit trail attributes model-driven folds.
    monkeypatch.setattr(mod.service, "graph_accept_entity_merge",
                        lambda pid, decided_by=None: calls.append(
                            ("accept_merge", pid, decided_by)) or {"accepted": True})
    monkeypatch.setattr(mod.service, "graph_accept_entity_junk",
                        lambda pid: calls.append(("accept_junk", pid)) or {"accepted": True})
    monkeypatch.setattr(mod.service, "graph_reject_entity_proposal",
                        lambda pid, decided_by=None: calls.append(
                            ("reject_entity", pid, decided_by)) or {"rejected": True})

    _invoke("memory_graph_review", {"action": "list"})
    _invoke("memory_graph_review", {
        "action": "propose",
        "proposals": [{"src": "a", "relation": "uses", "dst": "b"}]})
    for action in ("accept_link", "reject_link", "accept_merge",
                   "accept_junk", "reject_entity"):
        _invoke("memory_graph_review", {"action": action, "proposal_id": 7})

    assert calls == [("list", None), ("propose", 1), ("accept_link", 7),
                     ("reject_link", 7), ("accept_merge", 7, "agent"),
                     ("accept_junk", 7), ("reject_entity", 7, "agent")]


def test_graph_review_validates_inputs(tmp_path: Path, monkeypatch) -> None:
    _reload(tmp_path, monkeypatch)
    assert _invoke("memory_graph_review", {"action": "bless"}).get("error") == "unknown_action"
    assert _invoke("memory_graph_review", {"action": "accept_link"}).get("error") == "proposal_id_required"
    assert _invoke("memory_graph_review", {"action": "propose"}).get("error") == "proposals_required"
    assert _invoke("memory_graph_review", {"action": "dismiss_pair"}).get("error") == "src_dst_required"
    assert _invoke("memory_graph_review",
                   {"action": "dismiss_pair", "src": "a"}).get("error") == "src_dst_required"


def test_graph_review_dismiss_pair_routes_to_service(tmp_path: Path, monkeypatch) -> None:
    # Step-C driver verb: an agent working deep-dream candidates must be able
    # to record "these are distinct" so the pair stops resurfacing.
    mod = _reload(tmp_path, monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(mod.service, "graph_dismiss_duplicate",
                        lambda a, b: calls.append(("dismiss", a, b)) or {"dismissed": True})
    out = _invoke("memory_graph_review",
                  {"action": "dismiss_pair", "src": "accept-link", "dst": "reject-merge"})
    assert out == {"dismissed": True}
    assert calls == [("dismiss", "accept-link", "reject-merge")]


def test_dream_deep_routes_snippets_param(tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        mod.service, "deep_dream",
        lambda apply=False, include_snippets=True:
            calls.append({"apply": apply, "include_snippets": include_snippets})
            or {"dry_run": True})
    _invoke("memory_dream", {"action": "deep", "snippets": False})
    _invoke("memory_dream", {"action": "deep"})
    assert calls == [{"apply": False, "include_snippets": False},
                     {"apply": False, "include_snippets": True}]


# ── surface shape: removals + description budget ──────────────────────────

_REMOVED = [
    # dump/introspection tools -> Cortex Console / briefing CLI
    "memory_facts", "memory_world_facts", "memory_lessons",
    "memory_list_sources", "memory_list_tags", "memory_episode_list",
    "memory_communities", "memory_digest", "memory_briefing",
    # folded into surviving tools
    "memory_path",             # memory_graph(to=...)
    "memory_save",             # autosave loop + exit flush
    "memory_delete", "memory_fact_forget", "memory_world_forget",
    "memory_lesson_forget",    # -> memory_forget(scope=...)
    "memory_dream_status", "memory_dream_pull", "memory_dream_commit",
    "memory_dream_run", "memory_deep_dream",  # -> memory_dream(action=...)
    "memory_graph_propose_links", "memory_graph_accept_proposal",
    "memory_graph_reject_proposal", "memory_graph_accept_entity_merge",
    "memory_graph_accept_entity_junk", "memory_graph_reject_entity_proposal",
    # -> memory_graph_review(action=...)
]


def test_removed_tools_are_gone() -> None:
    from pseudolife_memory import mcp_server  # noqa: PLC0415

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    still_there = sorted(names & set(_REMOVED))
    assert still_there == [], f"tools that should have left the surface: {still_there}"


def test_descriptions_are_terse() -> None:
    """The manifest is loaded into agent context every session. Pre-
    consolidation it cost ~37k chars; the consolidated surface must stay
    at half that or less, with no single tool ballooning."""
    from pseudolife_memory import mcp_server  # noqa: PLC0415

    tools = asyncio.run(mcp_server.mcp.list_tools())
    fat = [(t.name, len(t.description or "")) for t in tools
           if len(t.description or "") > 1600]
    assert fat == [], f"over-long tool descriptions: {fat}"
    total = sum(len(t.description or "") for t in tools)
    assert total <= 18_000, f"tool-description payload too large: {total} chars"
