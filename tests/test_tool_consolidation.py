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
* every remaining description is terse: <=1600 chars each, each input
  schema <=4000 compact bytes, and both the descriptions and the
  inputSchema param descriptions are metered per toolset tier (see
  ``test_descriptions_fit_tier_budgets``).
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

    committed = _invoke(
        "memory_dream",
        {"action": "commit", "commit_token": pulled["commit_token"]},
    )
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


def test_dream_commit_requires_token(tmp_path: Path, monkeypatch) -> None:
    _reload(tmp_path, monkeypatch)
    out = _invoke("memory_dream", {"action": "commit"})
    assert out.get("error") == "commit_token_required"


def test_dream_commit_explains_legacy_cursor_rejection(
        tmp_path: Path, monkeypatch) -> None:
    _reload(tmp_path, monkeypatch)
    out = _invoke("memory_dream", {"action": "commit", "cursor": 123.0})
    assert out.get("error") == "legacy_cursor_unsupported"
    assert "commit_token" in out.get("detail", "")


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
    mod = _reload(tmp_path, monkeypatch)
    refused = _invoke("memory_dream", {"action": "snooze"})
    assert refused["error"] == "invalid_argument" and "'status'" in refused["message"]
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


def test_forget_scope_memory_filters_narrow(tmp_path: Path, monkeypatch) -> None:
    """text + source over MCP deletes the intersection, never the union
    (the 2026-09-29 status-source wipe)."""
    _reload(tmp_path, monkeypatch)
    _invoke("memory_store", {"text": "probe", "source": "status"})
    _invoke("memory_store", {"text": "probe", "source": "notes"})
    _invoke("memory_store", {"text": "status line", "source": "status"})
    out = _invoke("memory_forget",
                  {"scope": "memory", "text": "probe", "source": "status"})
    assert out["deleted_count"] == 1
    left = {(e["text"], e["source"])
            for e in _invoke("memory_recent", {"n": 10})["entries"]}
    assert left == {("probe", "notes"), ("status line", "status")}


def test_forget_scope_memory_bulk_needs_confirm_bulk(tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.service.config.memory, "delete_confirm_threshold", 2)
    for i in range(3):
        _invoke("memory_store", {"text": f"bulk {i}", "source": "bulk"})
    out = _invoke("memory_forget", {"scope": "memory", "source": "bulk"})
    assert out["error"] == "bulk_confirm_required"
    assert out["would_delete"] == 3 and out["deleted_count"] == 0
    assert len(_invoke("memory_recent", {"n": 10})["entries"]) == 3
    out = _invoke("memory_forget",
                  {"scope": "memory", "source": "bulk", "confirm_bulk": True})
    assert out["deleted_count"] == 3
    assert _invoke("memory_recent", {"n": 10})["entries"] == []


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
    mod = _reload(tmp_path, monkeypatch)
    refused = _invoke("memory_forget", {"scope": "everything"})  # Literal schema gate
    assert refused["error"] == "invalid_argument" and "'memory'" in refused["message"]
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
    mod = _reload(tmp_path, monkeypatch)
    refused = _invoke("memory_graph_review", {"action": "bless"})  # Literal schema gate
    assert refused["error"] == "invalid_argument" and "'list'" in refused["message"]
    assert mod.memory_graph_review("bless").get("error") == "unknown_action"
    assert _invoke("memory_graph_review", {"action": "accept_link"}).get("error") == "proposal_id_required"
    assert _invoke("memory_graph_review", {"action": "propose"}).get("error") == "proposals_required"
    assert _invoke("memory_graph_review", {"action": "dismiss_pair"}).get("error") == "src_dst_required"
    assert _invoke("memory_graph_review",
                   {"action": "dismiss_pair", "src": "a"}).get("error") == "src_dst_required"


def test_graph_review_scope_is_served_as_a_source_filter(
    tmp_path: Path, monkeypatch,
) -> None:
    """``list``'s ``scope`` keeps the entities that carry that memory
    SOURCE (the Atlas project switcher's value) — it is not a finding-kind
    filter. The served description said "keep only findings of this kind"
    until the 2026-09-20 review of PR #316, which had briefly told agents to
    page with ``scope=<finding kind>`` (an empty entity set, so an empty
    analyzer listing). Pin the served text to the real contract; the
    service side is pinned by tests/test_graph.py::
    test_graph_review_scope_filters_by_memory_source_not_finding_kind."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)
    tools = asyncio.run(mod.mcp.list_tools())
    desc = {t.name: t for t in tools}["memory_graph_review"].input_schema[
        "properties"]["scope"].get("description") or ""
    assert "source" in desc, desc
    assert "of this kind" not in desc, desc
    assert "all" in desc, desc  # the documented "everything" spelling


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
    tier's visible descriptions must fit its budget (spec 2026-07-11).

    Why each limit exists (re-based 2026-10-04, maintainer decision):

    * Per tool, the hard limits are the clients'. Claude Code truncates each
      MCP tool description (and server instructions) at 2,048 characters by
      default (code.claude.com/docs/en/mcp, CLAUDE_CODE_MAX_MCP_DESCRIPTION_
      LENGTH), so the 1,600-character cap keeps a margin under it. Codex CLI
      strips every parameter description from a tool whose input schema,
      serialized compactly, exceeds 5,000 bytes (openai/codex
      codex-rs/tools/src/json_schema/compaction.rs,
      DEFAULT_COMPACT_TOOL_SCHEMA_BYTES; per server
      ``tool_input_schema_max_bytes``), so the 4,000-byte schema cap keeps a
      margin under that. Both read 2026-10-04; neither the Anthropic API nor
      Gemini CLI documents a limit.
    * The tier totals are no client's limit: they are a deliberate wall on
      context cost (eager context for clients that do not defer tools, and
      tool-selection quality). They leave a few hundred description
      characters of headroom on core and full (about 1,000 when re-based,
      ~400 after the 2026-10-04 rewrite), proportionally less on minimal
      (every minimal tool counts against core and full too), and move only
      with a recorded reason below."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)

    tools = asyncio.run(mod.mcp.list_tools())
    sizes = {t.name: len(t.description or "") for t in tools}
    fat = [(n, s) for n, s in sizes.items() if s > 1600]
    assert fat == [], f"over-long tool descriptions: {fat}"
    # Codex measures serde_json's compact encoding of the normalized schema
    # (UTF-8, no whitespace); this approximates what Codex counts, and the
    # 1,000-byte margin covers normalization. Largest on 2026-10-04:
    # memory_search, 1,956 bytes; later that day, once the board tools
    # described their parameters, memory_agents at 3,459.
    schema_bytes = {t.name: len(json.dumps(t.input_schema or {}, separators=(",", ":"),
                                           ensure_ascii=False).encode("utf-8"))
                    for t in tools}
    big = [(n, b) for n, b in schema_bytes.items() if b > 4000]
    assert big == [], f"input schemas Codex would strip of parameter descriptions: {big}"
    # Bumped for Task 5 (memory_set_add / memory_set_remove, both minimal
    # tier, so their descriptions count against core/full too) — the prior
    # caps (4500/9500/15500) left only a few dozen chars of headroom.
    # Bumped again 2026-07-31: memory_set_add's description gained the
    # aggregate-conversion-guard contract (number-led scalars park as a
    # contender instead of converting), already trimmed to its minimum.
    # Full bumped 2026-08-05: memory_graph_review gained the relate verdict
    # and proposal_ids batching; 16250 left zero headroom after trimming.
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
    # Measured 2026-09-22 after adding the full-tier memory_reinstate
    # contract: full is 17,238 chars across 38 tools. The 17,250 cap leaves
    # 12 chars and keeps the minimal/core ceilings unchanged.
    # Measured 2026-09-23 after memory_search's supersession sentence
    # changed from "prefer superseded_by_text" (a contract the review
    # found wrong for ~4 in 10 legacy links) to the replaced_by pointer
    # contract (field, verified flag, preview test, no chains): +242 chars
    # on a minimal-tier tool, so minimal is 5,222 and full 17,480. Both
    # caps move deliberately rather than cut another sentence of the same
    # description; core (11,172) still fits.
    # 2026-09-23 follow-up (replaced_by.current, memory_get's pointer):
    # paid for inside the same descriptions (memory_search's min(5, top_k)
    # rule moved into its top_k param description), caps unchanged —
    # minimal 5,215, core 11,178, full 17,466.
    # The audit-log notice adds five characters to core/full descriptions.
    # 2026-09-25: memory_search's low_confidence and cortex sentences made
    # truthful (+17, caps unchanged): minimal 5,232, core 11,200, full
    # 17,488.
    # 2026-09-26: memory_agents gained claim/release for v45 resource leases
    # and update's expect (+295 on a core-tier tool, after trimming its own
    # docstring): minimal 5,232, core 11,495, full 17,783. Core fits with 5
    # to spare; the opt-in full cap moves deliberately, 17,500 -> 17,800.
    # 2026-09-27: memory_agents gained update's children (v47), paid for by
    # trimming the same docstring, caps unchanged: minimal 5,232, core
    # 11,496, full 17,784.
    # 2026-09-28: the park record (five update fields) landed on
    # memory_agents and the send's wake decision (its seven values, clears
    # and urgent) on memory_message, both core-tier tools, +199 after each
    # docstring was tightened to pay for its own line; with the park
    # expiry's default and cap (review fix, same day), then merged with
    # #430's fan-out/prefix sentences in one tightened send docstring:
    # minimal 5,232, core 11,706, full 17,994. Core and full move
    # deliberately, 11,500 -> 11,750 and 17,800 -> 18,000, rather than cut
    # the decision table that tells a sender what would wake a parked peer.
    # 2026-09-29: memory_forget's filters now narrow (AND) and a match over
    # memory.delete_confirm_threshold needs confirm_bulk, after a text +
    # source delete under the old OR removed a whole source; +160 on a
    # full-tier tool after tightening its docstring: minimal 5,232, core
    # 11,706, full 18,154. Full moves deliberately, 18,000 -> 18,250.
    # 2026-10-04: memory_message's done-park sentence names the designated
    # coordinator (+11 for the word, +7 net), paid for by reflowing the
    # same docstring one line shorter, caps unchanged: minimal 5,232, core
    # 11,729, full 18,248 (2 to spare).
    # 2026-10-04, re-based (maintainer decision): about eight bumps had left
    # the caps a few characters above the totals, so every contract landed by
    # cutting another, and the per-tool client limits above were found to be
    # the real constraints. After the delegate rename (memory_message names
    # the maintainer's delegate): minimal 5,232, core 11,728, full 18,247.
    # Caps 5,750 / 12,750 / 19,250 leave 518 / 1,022 / 1,003.
    # 2026-10-04, later (cross-model review): memory_agents, memory_message,
    # memory_search and memory_toolset rewritten to lead with purpose and
    # sibling routing, then review fixes (request_conflict, the receive
    # cursor contract): minimal 5,501, core 12,352, full 18,871 — caps
    # unchanged, 249 / 398 / 379 to spare.
    budgets = {"minimal": 5750, "core": 12750, "full": 19250}
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
    # Measured 2026-08-25 immediately after the migration above:
    # minimal 1826, core 4304, full 7413. Caps are those plus ~775 of
    # headroom each — room for a real contract to land without a bump,
    # not room to drift.
    #
    # core/full bumped 2026-09-05 for memory_outcome's `used_ids` (minimal
    # tier, so it counts against all three): the 2026-08-25 headroom had
    # been spent down to 53 chars on core and 4 on full, so no argument
    # contract of any length could land. The caps below leave ~120 again,
    # deliberately less than 775: still a wall, not a licence. Measured
    # 2026-09-05 after the review fold (used_ids gained a cap and an
    # error/miss split, documented in both places): minimal 2405, core
    # 5159, full 8308 here, and minimal 4985 / core 9570 / full 15064
    # against the description budgets above — the six used_ids result keys
    # were listed by trimming memory_outcome's own docstring, not by
    # moving a cap. 2026-09-08: a seventh key (used_ids_served_elsewhere)
    # and the window invariant on the param were paid for the same way.
    # 2026-09-20: memory_graph_review's corrected `scope` contract (a
    # memory-source filter, not a finding kind — full tier only) was paid
    # for by trimming the same tool's dst / store / proposal_id /
    # proposal_ids / relation wording;
    # full stood at 8398 before the fix.
    # Measured 2026-09-22 after memory_reinstate's nine exact-input
    # descriptions: full is 8,832 chars. The 8,925 cap leaves 93 chars.
    # 2026-09-24: memory_search's top_k gained the min(5, top_k) rule its
    # description dropped (+13): minimal 2,494, core 5,248, full 8,845 —
    # core has 2 chars left.
    # 2026-09-29: memory_forget's confirm_bulk (full tier only) was paid
    # for by trimming the same tool's five memory-scope filter descriptions
    # ("delete entries ..." to "entries ...", the docstring already says
    # they delete); caps unchanged: minimal 2,494, core 5,248, full 8,909.
    # 2026-10-04, re-based with the description caps (same reason, about a
    # tenth of headroom each): minimal 2,494, core 5,248, full 8,909 under
    # 2,750 / 5,750 / 9,750.
    # 2026-10-04, later: the cross-model review (Claude + Codex) gave the
    # two board tools their first parameter descriptions (26 parameters,
    # +2,540, carrying the caps Codex strips from the schema) and
    # memory_search its filter-scope wording: minimal 2,739, core 8,050,
    # full 11,711. Re-based to 3,000 / 8,750 / 12,500 — the same tenth of
    # headroom, measured on the text that landed rather than assumed.
    param_budgets = {"minimal": 3000, "core": 8750, "full": 12500}
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
    # the listed "entity|attribute" keys; the MCP layer decodes them
    # (service._parse_slot_key: listing keys spell a literal "|" in a name
    # as "%7C", so each has exactly one bare "|").
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
    out = mod.memory_graph_review("dismiss_slot_pair", store="lesson",
                                  src="ci%7Ccd-deploy|approach",
                                  dst="ci-cd-deploy|approach")
    assert out == {"dismissed": True}
    assert calls == [("lesson", "deploy-daemon", "approach",
                      "deploy-host", "pitfall"),
                     ("lesson", "ci|cd-deploy", "approach",
                      "ci-cd-deploy", "approach")]
    bad = mod.memory_graph_review("dismiss_slot_pair", store="lesson", src="no-pipe")
    assert bad.get("error") == "store_src_dst_required"
    # Two bare pipes are not a listed key: refuse rather than guess a split.
    bad = mod.memory_graph_review("dismiss_slot_pair", store="lesson",
                                  src="ci|cd deploy|approach", dst="x|y")
    assert bad.get("error") == "store_src_dst_required"
    assert len(calls) == 2


def test_graph_review_restore_slot_routes_to_service(tmp_path: Path, monkeypatch) -> None:
    """The undo for a lesson/world forget over MCP (2026-09-03): ``src`` is
    the retired "entity|attribute" key as listed by the retired listing, or
    a bare entity to restore every retired aspect of it."""
    mod = _reload(tmp_path, monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(
        mod.service, "lesson_restore",
        lambda task, aspect=None, **kw: calls.append(("lesson", task, aspect, kw))
        or {"restored": 1})
    monkeypatch.setattr(
        mod.service, "world_restore",
        lambda entity, attribute=None, **kw: calls.append(("world", entity, attribute, kw))
        or {"restored": 1})
    out = _invoke("memory_graph_review",
                  {"action": "restore_slot", "store": "lesson",
                   "src": "deploy-daemon|approach"})
    assert out == {"restored": 1}
    out = _invoke("memory_graph_review",
                  {"action": "restore_slot", "store": "world", "src": "acme"})
    assert out == {"restored": 1}
    # A literal "|" in a name is listed as "%7C", in a key or its entity half.
    for src in ("ci%7Ccd-deploy|approach", "ci%7Ccd-deploy"):
        assert mod.memory_graph_review(
            "restore_slot", store="lesson", src=src) == {"restored": 1}
    assert calls == [("lesson", "deploy-daemon", "approach", {"decided_by": "agent"}),
                     ("world", "acme", None, {"decided_by": "agent"}),
                     ("lesson", "ci|cd-deploy", "approach", {"decided_by": "agent"}),
                     ("lesson", "ci|cd-deploy", None, {"decided_by": "agent"})]
    bad = mod.memory_graph_review("restore_slot", store="fact", src="x|y")
    assert bad.get("error") == "store_src_required"
    bad = mod.memory_graph_review("restore_slot", store="lesson")
    assert bad.get("error") == "store_src_required"
    bad = mod.memory_graph_review("restore_slot", store="lesson",
                                  src="ci|cd deploy|approach")
    assert bad.get("error") == "store_src_required"
    assert len(calls) == 4


# ── served policy text (2026-09-02) ───────────────────────────────────────

def _descriptions(tmp_path: Path, monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)
    tools = asyncio.run(mod.mcp.list_tools())
    return {t.name: " ".join((t.description or "").split()) for t in tools}


def test_store_description_carries_the_write_policy_boundary(
        tmp_path: Path, monkeypatch) -> None:
    """MCB (arXiv 2608.19564) measured agents over-persisting ambiguous
    interaction-derived information; a policy prompt naming the four
    options cut erroneous persistence 0.243 -> 0.100. The boundary is
    served on ``memory_store`` itself, where the decision is made — a
    future trim that drops it should go red rather than quietly restore
    the un-gated "store anything worth keeping" surface."""
    d = _descriptions(tmp_path, monkeypatch)["memory_store"]
    assert "PERSIST" in d
    assert "CONTEXT ONLY" in d
    assert "RE-VERIFY" in d
    assert 'memory_fact_set(..., freshness_class="volatile")' in d
    assert "ASK when the claim is ambiguous" in d


def test_recall_surface_carries_trap_avoidance_guidance(
        tmp_path: Path, monkeypatch) -> None:
    """MemTrapBench (arXiv 2608.20202) found faithfully-recorded, relevant
    memories still anchoring models on a stale framing (Reasoning Fixation
    / Belief Distortion), with every framework tested underperforming
    no-memory; an inference-time instruction recovered the loss. Pin it on
    the two retrieval surfaces where the anchoring shape is strongest —
    the always-visible entry point and prior lessons."""
    d = _descriptions(tmp_path, monkeypatch)
    assert "leads about the PAST" in d["memory_search"]
    assert "re-derive" in d["memory_search"]
    assert "anchors you on a stale framing" in d["memory_lesson_search"]
    # The prose is anchored to the flag the tool already returns, so the
    # guidance points at a computed signal rather than a vague heuristic.
    assert "re_verify" in d["memory_lesson_search"]


def test_message_description_names_the_maintainers_delegate(
        tmp_path: Path, monkeypatch) -> None:
    """Since #550 (2026-10-03) only the session the operator granted the
    live ``delegate:<project>`` lease (``designated:coordinator:<project>``
    before 0.16.1) reopens a done park; holding the open
    ``coordinator:<project>`` lease, which the memory_agents description
    tells sessions to claim, grants nothing. A "coordinator" in the send's
    done-park sentence reads, beside that claim line, as if claiming the
    lease conferred the authority (maintainer decision 2026-10-04: the
    authority is the maintainer's delegate)."""
    d = _descriptions(tmp_path, monkeypatch)
    assert "coordinator:<project>" in d["memory_agents"]
    assert "maintainer's delegate" in d["memory_message"]
    assert "coordinator" not in d["memory_message"]


def test_store_and_outcome_teach_status_notes_and_the_credit_window(
        tmp_path: Path, monkeypatch) -> None:
    """Peers' memory-change notes and the dream's exclusion both key on
    ``source="status"`` (``memory_changes_since``, ``DreamConfig
    .exclude_sources``), but ``memory_store``'s ``source`` never named it, so
    a session following the per-turn tail stored progress under the default
    ``agent``. And ``used_ids`` credits only this session's searches from the
    last hour (``use_window_seconds``), a rule that sat at the end of a
    parameter description while 43% of Codex outcomes in the 2026-10-04
    review carried no ``used_ids``: it leads the docstring now."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    source = tools["memory_store"].input_schema["properties"]["source"]["description"]
    assert '"status"' in source and "dream skips" in source
    outcome = " ".join(tools["memory_outcome"].description.split())
    lead = outcome.split("Returns")[0]
    assert "used_ids" in lead and "last hour" in lead
    used = tools["memory_outcome"].input_schema["properties"]["used_ids"]["description"]
    assert "hour" not in used


def test_board_tools_describe_every_parameter_with_its_cap(
        tmp_path: Path, monkeypatch) -> None:
    """The 2026-10-04 cross-model review (Claude + Codex, both reading the
    served surface) found the two most-called tools shipping 26 parameters
    with no description at all, and Codex strips ``maxLength`` / ``minimum``
    / ``maximum`` from MCP schemas, so a cap that is not in the description
    text never reaches an OpenAI model: 63 of its 110 model-caused errors in
    five weeks were ``string_too_long`` on these fields. Every parameter of
    both board tools carries a description, and the capped ones say the cap
    in words."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    for name in ("memory_agents", "memory_message"):
        props = tools[name].input_schema["properties"]
        bare = [p for p, v in props.items() if len(v.get("description") or "") < 20]
        assert bare == [], f"{name} parameters without a description: {bare}"
    agents = tools["memory_agents"].input_schema["properties"]
    assert "240" in agents["status"]["description"]
    assert "120" in agents["task"]["description"]
    assert "40" in agents["children"]["description"]
    assert "epoch" in agents["park_expires"]["description"]
    message = tools["memory_message"].input_schema["properties"]
    assert "8192" in message["text"]["description"]
    assert "1..50" in message["limit"]["description"]
    assert "required" in message["request_id"]["description"]
    d = _descriptions(tmp_path, monkeypatch)
    # Both families sent ``limit`` to memory_search (140 calls, silently
    # ignored) and read a withheld wake receipt as a failed send.
    assert "not limit" in tools["memory_search"].input_schema["properties"]["top_k"]["description"]
    assert "send needs to, text and request_id" in d["memory_message"]
    assert "not delivery failure" in d["memory_message"]
    # Expanding the server tier does not refresh a client's callable list
    # (observed from Codex 2026-10-04: status said full, catalog stayed 24).
    assert "not client refresh" in d["memory_toolset"]
