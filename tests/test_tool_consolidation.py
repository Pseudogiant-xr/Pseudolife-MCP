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
* every remaining description is terse: <=1600 chars each, and both the
  descriptions and the inputSchema param descriptions are metered per
  toolset tier (see ``test_descriptions_fit_tier_budgets``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.helpers import (
    invoke_tool as _invoke,
    reload_mcp_filemode as _reload,
)


# ── memory_dream(action=...) ──────────────────────────────────────────────


def test_dream_status_pull_commit_via_one_tool(tmp_path: Path, monkeypatch) -> None:
    # action="run" is dispatched in test_mcp_server.py::
    # test_memory_dream_run_via_mcp_dispatch, which also carries the
    # no-extractor assertion; this pins the manual status/pull/commit cycle.
    _reload(tmp_path, monkeypatch)

    _invoke("memory_store", {"text": "the beacon port is 7777", "source": "notes"})

    status = _invoke("memory_dream", {"action": "status"})
    assert "backlog" in status and "would_fire" in status

    pulled = _invoke("memory_dream", {"action": "pull"})
    assert "cursor" in pulled and "entries" in pulled

    committed = _invoke("memory_dream", {"action": "commit", "cursor": pulled["cursor"]})
    assert "dream_cursor" in committed


def test_dream_run_passes_limit(tmp_path: Path, monkeypatch) -> None:
    """``limit`` reaches the service verbatim on action="run" — the knob
    that bounds how much backlog one server-side dream chews through."""
    mod = _reload(tmp_path, monkeypatch)
    seen = {}

    def fake_dream_run(extractor, *, limit=None):
        seen["limit"] = limit
        return {"pulled": 0, "claims": 0, "inserted": 0, "confirmed": 0,
                "contested": 0, "superseded": 0, "cursor": 0.0}

    monkeypatch.setattr(mod.service, "dream_run", fake_dream_run)
    mod.memory_dream(action="run", limit=500)
    assert seen["limit"] == 500


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


def test_dream_unknown_action_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """Over MCP the ``Literal`` schema rejects a bad action with a message
    that lists the legal values; direct (in-process) callers still get the
    structured ``unknown_action`` fallback."""
    from mcp.server.mcpserver.exceptions import ToolError

    mod = _reload(tmp_path, monkeypatch)
    with pytest.raises(ToolError, match="'status'"):
        _invoke("memory_dream", {"action": "snooze"})
    out = mod.memory_dream("snooze")
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
    from mcp.server.mcpserver.exceptions import ToolError

    mod = _reload(tmp_path, monkeypatch)
    with pytest.raises(ToolError, match="'memory'"):  # Literal schema gate
        _invoke("memory_forget", {"scope": "everything"})
    assert mod.memory_forget("everything").get("error") == "unknown_scope"
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
                        lambda pid, decided_by=None: calls.append(
                            ("accept_junk", pid, decided_by)) or {"accepted": True})
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
                     ("accept_junk", 7, "agent"), ("reject_entity", 7, "agent")]


def test_graph_review_validates_inputs(tmp_path: Path, monkeypatch) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    mod = _reload(tmp_path, monkeypatch)
    with pytest.raises(ToolError, match="'list'"):  # Literal schema gate
        _invoke("memory_graph_review", {"action": "bless"})
    assert mod.memory_graph_review("bless").get("error") == "unknown_action"
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


def test_graph_review_relate_writes_edge_and_dismisses_pair(
    tmp_path: Path, monkeypatch,
) -> None:
    # The third verdict on a duplicate pair: related, neither merge nor
    # unrelated. One call writes the typed edge AND retires the pair.
    mod = _reload(tmp_path, monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(
        mod.service, "graph_relate",
        lambda src, relation, dst, origin=None: calls.append(
            ("relate", src, relation, dst, origin)) or {
                "src": src, "relation": relation, "dst": dst})
    monkeypatch.setattr(
        mod.service, "graph_dismiss_duplicate",
        lambda a, b: calls.append(("dismiss", a, b)) or {"dismissed": True})
    out = _invoke("memory_graph_review",
                  {"action": "relate", "src": "cortex.py",
                   "relation": "part-of", "dst": "Cortex"})
    assert out["pair_dismissed"] is True and out["relation"] == "part-of"
    assert calls == [("relate", "cortex.py", "part-of", "Cortex", "agent"),
                     ("dismiss", "cortex.py", "Cortex")]
    # unknown relation: error propagates, pair NOT dismissed
    calls.clear()
    monkeypatch.setattr(
        mod.service, "graph_relate",
        lambda src, relation, dst, origin=None: {"error": "unknown_relation"})
    out = _invoke("memory_graph_review",
                  {"action": "relate", "src": "a", "relation": "zzz", "dst": "b"})
    assert out.get("error") == "unknown_relation" and calls == []
    assert mod.memory_graph_review(
        "relate", src="a", dst="b").get("error") == "src_relation_dst_required"


def test_graph_review_batch_verdicts(tmp_path: Path, monkeypatch) -> None:
    # Agent triage settles hundreds of proposals; proposal_ids batches them
    # in one call. A JSON-stringified list (MCP clients stringify untyped
    # list params) must coerce, and not_pending ids don't count as settled.
    mod = _reload(tmp_path, monkeypatch)
    seen: list[int] = []

    def _accept(pid):
        seen.append(pid)
        if pid == 13:
            return {"accepted": False, "reason": "not_pending", "id": pid}
        return {"accepted": True, "id": pid}

    monkeypatch.setattr(mod.service, "graph_accept_proposal", _accept)
    out = _invoke("memory_graph_review",
                  {"action": "accept_link", "proposal_ids": [11, 12, 13]})
    assert seen == [11, 12, 13]
    assert out["settled"] == 2 and len(out["results"]) == 3
    seen.clear()
    out = mod.memory_graph_review("accept_link", proposal_ids="[21, 22]")
    assert seen == [21, 22] and out["settled"] == 2
    # reject handlers report {"rejected": False} for a stale id (e.g. one
    # the deep dream's junk auto-delete already cascaded away) with no
    # error/reason keys — that must not count as settled.
    monkeypatch.setattr(
        mod.service, "graph_reject_proposal",
        lambda pid: {"rejected": pid != 30, "id": pid})
    out = mod.memory_graph_review("reject_link", proposal_ids=[30, 31])
    assert out["settled"] == 1


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

def test_descriptions_fit_tier_budgets(tmp_path: Path, monkeypatch) -> None:
    """The manifest is eager agent context for non-deferring clients; each
    tier's visible descriptions must fit its budget (spec 2026-07-11)."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)

    tools = asyncio.run(mod.mcp.list_tools())
    sizes = {t.name: len(t.description or "") for t in tools}
    fat = [(n, s) for n, s in sizes.items() if s > 1600]
    assert fat == [], f"over-long tool descriptions: {fat}"
    # Bumped for Task 5 (memory_set_add / memory_set_remove, both minimal
    # tier, so their descriptions count against core/full too) — the prior
    # caps (4500/9500/15500) left only a few dozen chars of headroom.
    # Bumped again 2026-07-31: memory_set_add's description gained the
    # aggregate-conversion-guard contract (number-led scalars park as a
    # contender instead of converting), already trimmed to its minimum.
    # Full bumped 2026-08-05: memory_graph_review gained the relate verdict
    # and proposal_ids batching; 16250 left zero headroom after trimming.
    # Full tier is opt-in (sessions start minimal/core), so it carries the
    # slack; the default surfaces stay tight.
    #
    # Restructured 2026-08-25. Three bumps in six weeks (2026-07-18 /
    # 07-31 / 08-05), each after trimming descriptions "to the minimum",
    # had left minimal at 4786/4800 and core at 10230/10250 — 14 and 20
    # chars. Issue #186's recall fix then lost its hops-ceiling and
    # cap-number documentation to that wall, which is the budget deciding
    # what the surface may promise rather than the other way round.
    # Maintainer decision: raise the description budgets AND move
    # arg-level contracts out of the description string into inputSchema
    # param descriptions (nothing on the surface used them before), with
    # schema accounting added below so the newly-used space stays metered
    # rather than becoming an unmetered escape hatch.
    budgets = {"minimal": 5000, "core": 11500, "full": 17000}
    for tier, cap in budgets.items():
        total = sum(sizes[n] for n in mod._visible_tool_names(tier))
        assert total <= cap, f"{tier} manifest {total} chars exceeds {cap}"

    # Schema accounting: a param description ships in the manifest exactly
    # like the tool description does, so it is metered on the same terms.
    param_sizes: dict[str, int] = {}
    for t in tools:
        props = (t.input_schema or {}).get("properties", {}) or {}
        param_sizes[t.name] = sum(
            len(p.get("description") or "") for p in props.values())
        over = [(f"{t.name}.{pn}", len(p.get("description") or ""))
                for pn, p in props.items()
                if len(p.get("description") or "") > 300]
        assert over == [], f"over-long param descriptions: {over}"
    # Measured 2026-08-31 after the build-scoped RE evidence tool landed:
    # minimal 1912, core 5871, full 8980. Caps retain roughly 600-800
    # characters of headroom per tier while metering the evidence tool's
    # deliberately explicit archive, scope, and address-query contracts.
    param_budgets = {"minimal": 2600, "core": 6700, "full": 9800}
    for tier, cap in param_budgets.items():
        total = sum(param_sizes[n] for n in mod._visible_tool_names(tier))
        assert total <= cap, (
            f"{tier} param-description total {total} chars exceeds {cap}")

    # Floor beside the ceiling: everything above is an upper bound, so if
    # the Annotated[..., Field(description=...)] -> inputSchema rendering
    # ever breaks (the mcp pin is wide, and the mechanism leans on
    # inspect.signature(eval_str=True) preserving Annotated extras), all
    # 35 tools would silently lose every argument contract while the caps
    # stayed green. One concrete pin plus a per-tier floor makes that
    # regression loud.
    hops = {t.name: t for t in tools}["memory_recall"].input_schema[
        "properties"]["hops"]
    assert "1..5" in (hops.get("description") or ""), (
        "param descriptions are not reaching inputSchema — the "
        "Field(description=) rendering path has broken")
    param_floors = {"minimal": 1500, "core": 3500, "full": 6000}
    for tier, floor in param_floors.items():
        total = sum(param_sizes[n] for n in mod._visible_tool_names(tier))
        assert total >= floor, (
            f"{tier} param-description total collapsed to {total} chars "
            f"(floor {floor}) — argument contracts are no longer shipping")


def test_graph_review_dismiss_slot_pair_routes_to_service(tmp_path: Path, monkeypatch) -> None:
    # Step-3c driver verb: an agent triaging the deep response's
    # lesson_duplicates / world_duplicates must be able to record "these
    # slots are distinct" over MCP (parity with dismiss_pair). src/dst are
    # the listed "entity|attribute" keys; the MCP layer splits at the FIRST
    # "|" (listing keys fold literal pipes, so the split is unambiguous).
    mod = _reload(tmp_path, monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(
        mod.service, "curation_dismiss_duplicate",
        lambda store, ae, aa, be, ba: calls.append((store, ae, aa, be, ba))
        or {"dismissed": True})
    out = _invoke("memory_graph_review",
                  {"action": "dismiss_slot_pair", "store": "lesson",
                   "src": "deploy-daemon|approach", "dst": "deploy-host|pitfall"})
    assert out == {"dismissed": True}
    assert calls == [("lesson", "deploy-daemon", "approach",
                      "deploy-host", "pitfall")]
    bad = mod.memory_graph_review("dismiss_slot_pair", store="lesson", src="no-pipe")
    assert bad.get("error") == "store_src_dst_required"
