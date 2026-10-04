"""Fixture-vs-serializer contract (2026-07-02 review, Console M2/M3).

The devserver fixtures are hand-written parallel truths, and they drifted
from the real service in ways that shipped broken Console features
invisibly: the Stream "Explain ranking" drawer QA'd green against invented
keys (``band``/numeric ``candidates``/``kept``/``text``) while production
rendered "undefined" and "[object Object]". This pins the exact keys the
Stream view consumes against BOTH the real trace/serializer output and the
fixtures, so drift fails CI instead of QA.

No Postgres, no embedder — file-mode CMS with raw unit vectors.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from pseudolife_memory.service import _entry_to_dict
from pseudolife_memory.memory.titans_memory import MemoryEntry
from pseudolife_memory.web.fixtures import FixtureService

# What the Console's Stream view (frontend/src/lib/api/stream.ts) reads.
_DRAWER_TIER_KEYS = {"name", "candidates"}
_DRAWER_CANDIDATE_KEYS = {"text_preview", "kept"}
_DRAWER_TOPK_KEYS = {"text_preview", "score"}
_ENTRY_CARD_KEYS = {"id", "text", "source", "bank", "tags", "superseded",
                    "access_count", "timestamp"}


def _unit(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return F.normalize(torch.randn(1024, generator=g), dim=0)


def _real_trace() -> dict:
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.utils.config import MemoryConfig

    cfg = MemoryConfig()
    cfg.surprise_threshold = 0.0
    cms = ContinuumMemorySystem(cfg)
    cms.store("alpha probe entry", _unit(1), source="t")
    cms.store("beta probe entry", _unit(2), source="t")
    _result, trace = cms.retrieve_with_trace(
        _unit(1), top_k=2, query_text="alpha probe entry")
    return trace


def _assert_drawer_shape(trace: dict, who: str) -> None:
    tiers = trace["tiers"]
    assert tiers, f"{who}: no tiers"
    for ti in tiers:
        assert _DRAWER_TIER_KEYS <= set(ti), f"{who} tier keys: {sorted(ti)}"
        assert isinstance(ti["candidates"], list), (
            f"{who}: tier candidates must be a list of dicts")
        for c in ti["candidates"]:
            assert _DRAWER_CANDIDATE_KEYS <= set(c), (
                f"{who} candidate keys: {sorted(c)}")
    for r in trace["final_topk"]:
        assert _DRAWER_TOPK_KEYS <= set(r), f"{who} final_topk keys: {sorted(r)}"


def test_real_trace_matches_drawer_contract():
    _assert_drawer_shape(_real_trace(), "real cms trace")


def test_fixture_trace_matches_drawer_contract():
    fx = FixtureService().trace("alpha")["trace"]
    _assert_drawer_shape(fx, "FixtureService.trace")


# What the Review view's curation panel (frontend/src/components/review/CurationPanel.svelte) reads per duplicate pair.
_CURATION_PAIR_KEYS = {"a_key", "b_key", "a", "b", "similarity"}
_CURATION_LESSON_SIDE_KEYS = {"entity", "attribute", "value",
                              "polarity", "outcome", "about"}
_CURATION_WORLD_SIDE_KEYS = {"entity", "attribute", "value", "source_url"}


def test_fixture_curation_duplicates_match_panel_contract():
    out = FixtureService().curation_duplicates()
    assert out["lesson_duplicates"] and out["world_duplicates"]
    for pair in out["lesson_duplicates"]:
        assert _CURATION_PAIR_KEYS <= set(pair), f"pair keys: {sorted(pair)}"
        for side in (pair["a"], pair["b"]):
            assert _CURATION_LESSON_SIDE_KEYS <= set(side), (
                f"lesson side keys: {sorted(side)}")
    for pair in out["world_duplicates"]:
        assert _CURATION_PAIR_KEYS <= set(pair), f"pair keys: {sorted(pair)}"
        for side in (pair["a"], pair["b"]):
            assert _CURATION_WORLD_SIDE_KEYS <= set(side), (
                f"world side keys: {sorted(side)}")


def test_entry_dicts_match_entry_card_contract():
    real = _entry_to_dict(MemoryEntry(
        text="probe", embedding=torch.zeros(4), surprise_score=0.0,
        timestamp=1.0))
    assert _ENTRY_CARD_KEYS <= set(real), f"real entry keys: {sorted(real)}"

    fx_entries = FixtureService().search("pseudolife")["entries"]
    assert fx_entries
    assert _ENTRY_CARD_KEYS <= set(fx_entries[0]), (
        f"fixture entry keys: {sorted(fx_entries[0])}")


# ── maintainer passkey and Board roles (spec 2026-10-04, section 4) ─────────
# What the Console's lib/maintainer.ts, SignedAction.svelte, RolesBand.svelte
# and PasskeysPanel.svelte read. The real side runs the service's own
# dict-building code over canned rows (a MaintainerStore whose two query
# helpers return them), so no Postgres is needed; the send answer, the
# repudiate answer and the completing routes need the database and are
# covered by tests/test_maintainer_messages.py and test_maintainer_roles.py.

_MX_STATUS_KEYS = {"available", "rp_id", "origin", "passkeys", "roles", "key_changes"}
_MX_KEY_CHANGE_KEYS = {"at", "principal", "change", "credential_id", "label", "by", "path",
                       "revoked"}
_MX_PASSKEY_KEYS = {"credential_id", "label", "state", "enrolled_by", "active_from",
                    "created_at", "last_used_at", "revoked_at", "revoked_by", "flagged_at"}
_MX_DELEGATE_KEYS = {"agent_id", "expires_at", "granted_by"}
_MX_COORDINATOR_KEYS = {"agent_id", "expires_at"}
_MX_RECIPIENT_KEYS = {"agent_id_prefix", "name", "label", "principal", "host", "client",
                      "project", "task", "last_activity", "duplicate_name"}
_MX_ROLE_PREVIEW_KEYS = {"role", "project", "action", "current_holder"}
_MX_GRANT_PREVIEW_KEYS = _MX_ROLE_PREVIEW_KEYS | {"replaces", "also_breaks"} | _MX_RECIPIENT_KEYS
_MX_SENT_KEYS = {"message_id", "recipient_agent_id", "recipient_label", "created_at", "label",
                 "text", "wake", "first_read_at", "acknowledged_at", "repudiated_at"}
_MX_INBOX_KEYS = {"message_id", "sender_agent_id", "sender_label", "sender_principal",
                  "reply_to", "text", "created_at", "acknowledged_at", "origin"}
_MX_AGENT = "a" * 32
_MX_DUMMY = {"id": "fixture", "response": {}}


def _real_maintainer_store():
    """A real MaintainerStore whose queries answer one canned row carrying
    every column its dict builders read."""
    import time

    from pseudolife_memory.storage.maintainer import MaintainerStore

    now = time.time()
    row = {
        # passkeys
        "credential_id": "cred", "label": "laptop", "state": "active",
        "enrolled_by": "bootstrap", "active_from": now - 60, "created_at": now - 120,
        "last_used_at": None, "revoked_at": None, "revoked_by": None, "flagged_at": None,
        # leases (roles, role_holder)
        "name": "delegate:p", "holder_agent_id": _MX_AGENT, "expires_at": now + 3600,
        "granted_by": "maintainer",
        # agents (recipient)
        "agent_id": _MX_AGENT, "principal": "laptop", "parent_thread": None,
        "capabilities": {}, "project": "p", "task": "", "last_activity": now,
        # messages (sent, inbox)
        "message_id": "m1", "recipient_agent_id": _MX_AGENT, "recipient_label": "worker",
        "maintainer_proof": {"label": "laptop"}, "text": "hi",
        "wake": {"decision": "rung", "reason": "maintainer_message", "ring_at": now},
        "first_read_at": None, "acknowledged_at": None, "repudiated_at": None,
        "sender_agent_id": _MX_AGENT, "sender_label": "worker", "sender_principal": "laptop",
        "reply_to": "m0",
        # audit events (key_changes)
        "payload": '{"by":"bootstrap","change":"enrol","credential_id":"cred",'
                   '"label":"laptop","path":"console","revoked":null}',
    }

    class Canned(MaintainerStore):
        def _one(self, sql, params=()):
            return row

        def _all(self, sql, params=()):
            return [row]

    return Canned(None, rp_id="localhost", origin="http://localhost:8765")


def _assert_status_shape(status, who):
    assert _MX_STATUS_KEYS <= set(status), f"{who} status keys: {sorted(status)}"
    if not status["available"]:
        assert status["reason"] == "maintainer_not_enrolled", who
    assert status["passkeys"], f"{who}: no passkey rows"
    for key in status["passkeys"]:
        assert set(key) == _MX_PASSKEY_KEYS, f"{who} passkey keys: {sorted(key)}"
    assert status["key_changes"], f"{who}: no key changes"
    for change in status["key_changes"]:
        assert set(change) == _MX_KEY_CHANGE_KEYS, f"{who} key change keys: {sorted(change)}"
        assert change["path"] in ("console", "host"), who
    assert status["roles"], f"{who}: no roles"
    for project, slots in status["roles"].items():
        assert set(slots) == {"delegate", "coordinator"}, f"{who} {project}: {sorted(slots)}"
        if slots["delegate"]:
            assert set(slots["delegate"]) == _MX_DELEGATE_KEYS, (
                f"{who} delegate keys: {sorted(slots['delegate'])}")
        if slots["coordinator"]:
            assert set(slots["coordinator"]) == _MX_COORDINATOR_KEYS, (
                f"{who} coordinator keys: {sorted(slots['coordinator'])}")


def test_maintainer_status_matches_console_contract():
    from pseudolife_memory.maintainer import _status
    _assert_status_shape(_status(None, None, _real_maintainer_store()), "real _status")
    _assert_status_shape(FixtureService().maintainer_status(), "FixtureService")


def test_maintainer_role_previews_match_console_contract():
    from pseudolife_memory.maintainer import _role_preview
    store = _real_maintainer_store()
    grant = _role_preview(store, "grant-delegate", {"project": "p", "agent_id": _MX_AGENT})
    revoke = _role_preview(store, "revoke-delegate", {"project": "p"})
    assert set(grant) == _MX_GRANT_PREVIEW_KEYS, f"real grant preview: {sorted(grant)}"
    assert set(revoke) == _MX_ROLE_PREVIEW_KEYS, f"real revoke preview: {sorted(revoke)}"

    fx = FixtureService()
    roles = fx.maintainer_status()["roles"]
    project = next(iter(roles))
    agent = roles[project]["coordinator"]["agent_id"]
    fx_grant = fx.maintainer_challenge({"purpose": "grant-delegate", "project": project,
                                        "agent_id": agent, "hold": 3600})
    fx_revoke = fx.maintainer_challenge({"purpose": "revoke-coordinator", "project": project})
    assert set(fx_grant["preview"]) == _MX_GRANT_PREVIEW_KEYS, sorted(fx_grant["preview"])
    assert set(fx_revoke["preview"]) == _MX_ROLE_PREVIEW_KEYS, sorted(fx_revoke["preview"])
    # replaces is an agent id (or null), also_breaks a lease name (or null).
    assert isinstance(fx_grant["preview"]["replaces"], str)
    assert fx_grant["preview"]["also_breaks"] == f"coordinator:{project}"


def test_maintainer_send_preview_matches_console_contract():
    real = _real_maintainer_store().recipient(_MX_AGENT)
    assert set(real) == _MX_RECIPIENT_KEYS, f"real recipient preview: {sorted(real)}"
    fx = FixtureService()
    agent = next(iter(fx.maintainer_status()["roles"].values()))["delegate"]["agent_id"]
    out = fx.maintainer_challenge({"purpose": "send", "to": agent, "text": "hi", "urgent": True})
    assert set(out) == {"payload", "mac", "publicKey", "preview"}
    assert set(out["preview"]) == _MX_RECIPIENT_KEYS, sorted(out["preview"])


def test_maintainer_sent_and_inbox_rows_match_console_contract():
    store = _real_maintainer_store()
    (sent,), (reply,) = store.sent(5), store.inbox(5)
    assert set(sent) == _MX_SENT_KEYS, f"real sent row: {sorted(sent)}"
    assert set(reply) == _MX_INBOX_KEYS, f"real inbox row: {sorted(reply)}"

    fx = FixtureService()
    fx_sent, fx_inbox = fx.maintainer_sent(50), fx.maintainer_inbox(50)
    assert set(fx_sent) == {"messages"} and set(fx_inbox) == {"messages"}
    assert fx_sent["messages"] and fx_inbox["messages"]
    for m in fx_sent["messages"]:
        assert set(m) == _MX_SENT_KEYS, f"fixture sent row: {sorted(m)}"
        assert m["wake"] is None or {"decision", "reason"} <= set(m["wake"])
    for m in fx_inbox["messages"]:
        assert set(m) == _MX_INBOX_KEYS, f"fixture inbox row: {sorted(m)}"


def test_fixture_maintainer_answers_match_the_real_service():
    """Fixture only: the real answers below need Postgres (they run inside
    the signed transaction)."""
    import json

    fx = FixtureService()
    roles = fx.maintainer_status()["roles"]
    project = next(iter(roles))
    delegate = roles[project]["delegate"]["agent_id"]

    def signed(purpose, route, **fields):
        c = fx.maintainer_challenge({"purpose": purpose, **fields})
        return getattr(fx, f"maintainer_{route}")(
            {"payload": c["payload"], "mac": c["mac"], "assertion": _MX_DUMMY}), c

    out, _ = signed("send", "send", to=delegate, text="hi", urgent=True)
    assert set(out) == {"message_id", "wake"} and out["wake"]["decision"] == "rung"
    rep, _ = signed("repudiate", "repudiate", message_id=out["message_id"])
    assert set(rep) == {"message_id", "repudiated_at", "follow_up"}

    # A role payload carries the holder the daemon read; a change in between
    # is role_changed, as the real route answers.
    c = fx.maintainer_challenge({"purpose": "revoke-delegate", "project": project})
    assert json.loads(c["payload"])["holder"] == delegate
    fx._mx()["roles"][project]["delegate"]["agent_id"] = roles[project]["coordinator"]["agent_id"]
    try:
        fx.maintainer_role({"payload": c["payload"], "mac": c["mac"], "assertion": _MX_DUMMY})
    except ValueError as exc:
        assert str(exc) == "role_changed"
    else:
        raise AssertionError("a moved holder was not refused")

    # Extra challenge fields are refused, as the real route refuses them.
    try:
        fx.maintainer_challenge({"purpose": "revoke-delegate", "project": project,
                                 "holder": delegate})
    except ValueError as exc:
        assert str(exc) == "invalid_request"
    else:
        raise AssertionError("an extra challenge field was accepted")
