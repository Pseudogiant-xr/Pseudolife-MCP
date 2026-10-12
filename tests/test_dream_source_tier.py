"""A dream-consolidated fact takes its provenance tier from the backing
entry's ``source`` tag — the same trust base the consolidation quarantine's
witness check stands on — never from the claim's own ``origin`` field.

The shipped extractors stamp every claim ``origin="agent"`` (it is model
output, so it is never trusted to raise a tier), so before this rule a fact
consolidated from a user-stated note (``source="user"`` / ``"conversation"``)
was stored at agent tier, and an ordinary agent-sourced note could then
supersede it. The member path carries the same tier: an agent-tier dream op
may neither convert a stronger tier's scalar into a set nor retract a member
a stronger tier put there.

Stub claims below carry ``origin="agent"`` exactly as the shipped
extractors do, so these tests exercise the shipped labelling.
"""
from __future__ import annotations

import tempfile

import pytest

from pseudolife_memory.memory.dream import RegexExtractor
from pseudolife_memory.service import MemoryService
from tests.dream_helpers import StubExtractor


@pytest.fixture()
def svc():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        yield MemoryService(data_dir=d)


def _claim(value, *, op=None, origin="agent", entity="editor-pref",
           attribute="editor"):
    c = {"entity": entity, "attribute": attribute, "value": value,
         "confidence": 0.9, "origin": origin, "source": 0}
    if op is not None:
        c["op"] = op
    return c


def _seed_user_fact(svc, value="vim"):
    svc.store(f"my preferred editor is {value}", source="user")
    svc.dream_run(StubExtractor([_claim(value)]))
    cur = svc.cortex_lookup("editor-pref", "editor")
    assert cur is not None and cur["value"] == value
    return cur


@pytest.mark.parametrize("source", ["user", "conversation"])
def test_user_sourced_note_consolidates_at_user_tier(svc, source):
    svc.store("my preferred editor is vim", source=source)
    svc.dream_run(StubExtractor([_claim("vim")]))
    assert svc.cortex_lookup("editor-pref", "editor")["origin"] == "user"


def test_regex_extractor_user_note_consolidates_at_user_tier(svc):
    """The reported reproduction, with the shipped RegexExtractor and no
    stub: the extractor labels the claim agent, the note says user."""
    svc.store("my editor is vim", source="user", origin="user")
    svc.dream_run(RegexExtractor())
    facts = [f for f in svc.cortex_dump()["entries"] if f["value"] == "vim"]
    assert facts and all(f["origin"] == "user" for f in facts)


def test_agent_note_cannot_supersede_user_sourced_fact(svc):
    _seed_user_fact(svc)
    svc.store("my preferred editor is emacs", source="agent")
    svc.dream_run(StubExtractor([_claim("emacs")]))
    assert svc.cortex_lookup("editor-pref", "editor")["value"] == "vim"
    contenders = svc.cortex_contenders("editor-pref", "editor")["contenders"]
    assert [c["value"] for c in contenders] == ["emacs"]


def test_two_agent_witnesses_cannot_supersede_user_sourced_fact(svc):
    """Quarantine on: the second, independent agent witness must not
    promote over a user-sourced value (dreaming.md's two-man-rule claim)."""
    svc.config.memory.dream.quarantine_low_trust = True
    _seed_user_fact(svc)
    svc.store("my preferred editor is emacs", source="agent")
    svc.dream_run(StubExtractor([_claim("emacs")]))
    svc.store("the preferred editor is emacs now", source="claude")
    out = svc.dream_run(StubExtractor([_claim("emacs")]))
    assert out.get("quarantine_promoted", 0) == 0
    assert svc.cortex_lookup("editor-pref", "editor")["value"] == "vim"


def test_claim_origin_field_never_raises_the_tier(svc):
    """The claim's own origin is model output: an agent-sourced note whose
    claim says ``origin="user"`` still lands at agent tier."""
    svc.store("my preferred editor is vim", source="agent")
    svc.dream_run(StubExtractor([_claim("vim", origin="user")]))
    assert svc.cortex_lookup("editor-pref", "editor")["origin"] == "agent"


def test_unmapped_source_tag_stays_agent_tier(svc):
    svc.store("my preferred editor is vim", source="some-project")
    svc.dream_run(StubExtractor([_claim("vim")]))
    assert svc.cortex_lookup("editor-pref", "editor")["origin"] == "agent"


def test_user_note_supersedes_user_note(svc):
    """Same tier, newer wins: a later user statement still updates."""
    _seed_user_fact(svc)
    svc.store("I switched my preferred editor to helix", source="user")
    svc.dream_run(StubExtractor([_claim("helix")]))
    cur = svc.cortex_lookup("editor-pref", "editor")
    assert cur["value"] == "helix" and cur["origin"] == "user"


def test_agent_member_add_cannot_convert_user_scalar(svc):
    _seed_user_fact(svc)
    svc.store("editors in use: emacs", source="agent")
    svc.dream_run(StubExtractor([_claim("emacs", op="add")]))
    cur = svc.cortex_lookup("editor-pref", "editor")
    assert cur is not None and cur["value"] == "vim"


def test_agent_member_remove_cannot_retract_user_member(svc):
    svc.set_add("editor-pref", "editor", "vim", origin="user")
    svc.store("vim is no longer one of the editors", source="agent")
    out = svc.dream_run(StubExtractor([_claim("vim", op="remove")]))
    assert out.get("member_remove_refused") == 1
    got = svc.cortex_lookup("editor-pref", "editor")
    assert [m["value"] for m in got["members"]] == ["vim"]


def test_user_member_remove_still_retracts_user_member(svc):
    svc.set_add("editor-pref", "editor", "vim", origin="user")
    svc.store("vim is no longer an editor I use", source="user")
    svc.dream_run(StubExtractor([_claim("vim", op="remove")]))
    got = svc.cortex_lookup("editor-pref", "editor")
    assert got is None or got.get("members", []) == []


def test_unguarded_callers_keep_their_behaviour(svc):
    """No origin passed (the MCP set tools, the rollback replay): the set
    tools stay unguarded by design."""
    svc.set_add("editor-pref", "editor", "vim", origin="user")
    assert svc.set_remove("editor-pref", "editor", "vim")["action"] \
        == "member_removed"


def test_user_witness_promotion_keeps_user_tier(svc):
    """Quarantine on: a user-sourced note that promotes a parked value must
    leave it at user tier — the same tier a direct write would store — or a
    later agent note could supersede what the user just confirmed."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.store("my preferred editor is emacs", source="agent")
    svc.dream_run(StubExtractor([_claim("emacs")]))
    svc.store("yes, my preferred editor is emacs", source="user")
    out = svc.dream_run(StubExtractor([_claim("emacs")]))
    assert out.get("quarantine_promoted") == 1
    cur = svc.cortex_lookup("editor-pref", "editor")
    assert cur["value"] == "emacs" and cur["origin"] == "user"

    svc.store("the preferred editor is nano", source="notes")
    svc.dream_run(StubExtractor([_claim("nano")]))
    assert svc.cortex_lookup("editor-pref", "editor")["value"] == "emacs"


def test_unbacked_claim_is_low_trust_whatever_its_origin_says(svc):
    """A claim with no resolvable backing note writes at agent tier, so the
    quarantine treats it as agent too — its origin field is never read."""
    from pseudolife_memory.service import (_quarantine_low_trust,
                                           _quarantine_witness)
    for origin in ("agent", "user", "action"):
        c = _claim("vim", origin=origin)
        assert _quarantine_low_trust(c, None, set()) is True
        assert _quarantine_witness(c, None) == "unbacked"


def test_assistant_speaker_still_demotes_a_user_note(svc):
    svc.store("I said my preferred editor is vim", source="user")
    claim = dict(_claim("vim"), speaker="assistant")
    svc.dream_run(StubExtractor([claim]))
    assert svc.cortex_lookup("editor-pref", "editor")["origin"] == "assistant"


def test_member_tier_guards_follow_protect_provenance(svc):
    """With the provenance guard off (the bench's isolation setting) the
    member tier guards are off too; the assistant floor is separate."""
    svc.config.memory.cortex.protect_provenance = False
    _seed_user_fact(svc)
    svc.store("editors in use: emacs", source="agent")
    svc.dream_run(StubExtractor([_claim("emacs", op="add")]))
    assert svc.cortex_lookup("editor-pref", "editor")["kind"] == "set"


# ── citation check: the extractor picks the backing note (in a one-note
# batch too), so a tier above agent needs the cited note to state the value
# and name the slot.

def _batch(svc, *notes):
    for text, source in notes:
        svc.store(text, source=source)


class _CitingStub(StubExtractor):
    """Cites the batch note containing ``cite`` by its position in the texts
    the dream passes in. Notes stored within one clock tick (15.6 ms on
    Windows) share a timestamp, so their batch order is not insertion order
    and a hard-coded index would cite the wrong note."""

    def __init__(self, claim, cite):
        super().__init__([claim])
        self._cite = cite

    def extract(self, texts, vocab, known_facts=None):
        idx = next(i for i, t in enumerate(texts) if self._cite in t)
        return [dict(c, source=idx) for c in self._claims]


def test_miscited_claim_does_not_borrow_a_user_notes_tier(svc):
    _seed_user_fact(svc)
    _batch(svc, ("unrelated: I had a sandwich for lunch", "user"),
                ("my preferred editor is emacs", "agent"))
    svc.dream_run(_CitingStub(_claim("emacs"), "sandwich"))  # cites the user note
    assert svc.cortex_lookup("editor-pref", "editor")["value"] == "vim"


def test_correctly_cited_claim_keeps_user_tier_in_a_batch(svc):
    _batch(svc, ("the build server is slow today", "agent"),
                ("my preferred editor is vim", "user"))
    svc.dream_run(_CitingStub(_claim("vim"), "preferred editor is vim"))
    assert svc.cortex_lookup("editor-pref", "editor")["origin"] == "user"


def test_single_note_batch_applies_the_citation_check(svc):
    """One note is no exemption: the extractor can still restate a value
    the note never states. A paraphrase stays agent tier."""
    svc.store("I mostly write code in the vi improved editor", source="user")
    svc.dream_run(StubExtractor([_claim("vim")]))
    assert svc.cortex_lookup("editor-pref", "editor")["origin"] == "agent"


def _deploy(value):
    return _claim(value, entity="deploy", attribute="target")


def test_unrelated_single_note_cannot_raise_an_agent_facts_tier(svc):
    """A one-note batch holding an unrelated user note, whose claim restates
    a known agent fact, must not confirm that fact up to user tier (a later
    agent update would then be blocked)."""
    svc.store("the deploy target is staging", source="agent")
    svc.dream_run(StubExtractor([_deploy("staging")]))
    svc.store("I had a sandwich for lunch", source="user")
    svc.dream_run(StubExtractor([_deploy("staging")]))
    assert svc.cortex_lookup("deploy", "target")["origin"] == "agent"
    svc.store("deploy target is prod now", source="agent")
    svc.dream_run(StubExtractor([_deploy("prod")]))
    assert svc.cortex_lookup("deploy", "target")["value"] == "prod"


def test_miscited_claim_is_low_trust_under_quarantine(svc):
    svc.config.memory.dream.quarantine_low_trust = True
    _batch(svc, ("unrelated: I had a sandwich for lunch", "user"),
                ("my preferred editor is emacs", "agent"))
    out = svc.dream_run(_CitingStub(_claim("emacs"), "sandwich"))
    assert out.get("quarantine_parked") == 1
    assert svc.cortex_lookup("editor-pref", "editor") is None


def test_cited_note_must_name_the_slot(svc):
    """The value alone is not enough: a user note that happens to contain
    the value but says nothing about the slot does not back the claim."""
    svc.store("the deploy target is staging", source="agent")
    svc.dream_run(StubExtractor([_deploy("staging")]))
    _batch(svc, ("I think prod looks fine today", "user"),
                ("set the deploy target to prod", "agent"))
    svc.dream_run(_CitingStub(_deploy("prod"), "looks fine"))
    cur = svc.cortex_lookup("deploy", "target")
    assert cur["value"] == "prod" and cur["origin"] == "agent"


class _BoolCitingStub(StubExtractor):
    """Cites the note containing ``cite`` with a bool, which ``isinstance``
    counts as an int (True == 1, False == 0)."""

    def __init__(self, claim, cite):
        super().__init__([claim])
        self._cite = cite

    def extract(self, texts, vocab, known_facts=None):
        idx = next(i for i, t in enumerate(texts) if self._cite in t)
        assert idx in (0, 1)
        return [dict(c, source=bool(idx)) for c in self._claims]


def test_bool_source_is_not_a_note_index(svc):
    _batch(svc, ("an agent remark about nothing in particular", "agent"),
                ("the deploy target is prod", "user"))
    svc.dream_run(_BoolCitingStub(_deploy("prod"), "deploy target is prod"))
    assert svc.cortex_lookup("deploy", "target")["origin"] == "agent"


@pytest.mark.parametrize("bad", [True, False])
def test_bool_source_is_dropped_at_the_parse_boundary(bad):
    """JSON true/false from the model is not a note number (int(True) is 1):
    the shipped extractor's claim and event parsers carry no source."""
    from pseudolife_memory.memory.dream import (OpenAICompatExtractor,
                                                events_from_parsed)
    from tests.dream_helpers import chat_payload, stub_server
    raw = {"entity": "e", "attribute": "a", "value": "v",
           "confidence": 0.8, "source": bad}
    with stub_server(lambda: (200, chat_payload([raw]))) as url:
        got = OpenAICompatExtractor(url, "m").extract(["n1", "n2"], [])
    assert len(got) == 1 and "source" not in got[0]
    ev = events_from_parsed(
        {"events": [{"description": "moved house", "source": bad}]}, 2)
    assert len(ev) == 1 and "source" not in ev[0]


def test_assistant_speaker_cannot_promote_a_parked_value_over_user(svc):
    """Quarantine on: the promote route's tier check uses the claim's
    write tier after the assistant-speaker demotion, so the two-man rule
    is never weaker than the direct write's provenance guard."""
    svc.config.memory.dream.quarantine_low_trust = True
    svc.cortex_write("editor-pref", "editor", "vim", support="user")
    svc.store("my preferred editor is emacs", source="agent")
    svc.dream_run(StubExtractor([_claim("emacs")]))
    svc.store("assistant: you should use emacs as your editor",
              source="user")
    out = svc.dream_run(StubExtractor([dict(_claim("emacs"),
                                            speaker="assistant")]))
    assert out.get("quarantine_promoted", 0) == 0
    cur = svc.cortex_lookup("editor-pref", "editor")
    assert cur["value"] == "vim" and cur["origin"] == "user"


@pytest.mark.parametrize("protect,action", [
    (False, "member_removed"), (True, "member_remove_refused")])
def test_member_remove_tier_guard_follows_protect_provenance(svc, protect,
                                                            action):
    svc.config.memory.cortex.protect_provenance = protect
    svc.set_add("editor-pref", "editor", "vim", origin="user")
    out = svc.set_remove("editor-pref", "editor", "vim", origin="agent")
    assert out["action"] == action


@pytest.mark.parametrize("value,note,entity,attribute,backed", [
    ("vim", "my preferred editor is vim", "editor-pref", "editor", True),
    ("Berlin", "I moved to berlin last week", "user", "moved_to", True),
    ("Berlin", "I moved to berlin last week", "user", "home", False),
    ("32", "I turned 32 in May", "user", "age.turned", True),
    ("33", "I turned 32 in May", "user", "age.turned", False),
    ("emacs", "I had a sandwich for lunch", "editor-pref", "editor", False),
    ("New York office", "the team works from the new york office",
     "team", "office", True),
    ("vi", "no editor named here", "editor-pref", "editor", False),
    ("prod", "I think prod looks fine today", "deploy", "target", False),
    # Short tokens and negators are part of the value.
    ("US office", "We relocated to the UK office.", "user", "office", False),
    ("not retired", "The server is retired.", "server", "status", False),
    ("not retired", "The server status is retired.", "server", "status",
     False),
    # Word boundaries are Unicode-aware.
    ("caf", "I went to the cafe\u0301 for coffee", "user", "went", False),
    ("caf", "I went to the caf\u00e9", "user", "went", False),
    ("vi", "We saw a vi\u00f1edo on vacation", "user", "saw", False),
    ("caf\u00e9", "I went to the CAFE\u0301 again", "user", "went", True),
    # No slot names given: nothing to check, so not backed.
    ("vim", "my preferred editor is vim", "", "", False),
])
def test_citation_backs(value, note, entity, attribute, backed):
    from pseudolife_memory.service_dream import _citation_backs
    assert _citation_backs(value, note, entity, attribute) is backed
