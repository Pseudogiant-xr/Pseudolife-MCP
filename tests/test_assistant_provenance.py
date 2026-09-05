"""Assistant-stated facts: labelled at the parse boundary, unable to
overwrite a user-origin value, and demoted in fact ranking.

Background (2026-09-05). On LongMemEval the cortex arm scored 0.054 on
``single-session-assistant`` and 0.233 on ``single-session-preference``
against naive RAG's 0.911 / 0.533, because 50 of 56 SSA sessions
extracted ZERO claims: the shipped claims prompt asks for "durable,
current-state facts ... skip narrative, opinions" and every worked
example is a user-stated fact, so the model reads assistant-stated
content as non-facts. The prompt variants that ask for them live in
``evals/prompts/assistant_facts_*.txt``; this suite pins the ENGINE side
that makes extracting them safe.

The contract:

* ``speaker`` is an optional claim field, whitelisted to
  ``"user"``/``"assistant"`` at the parse boundary exactly like ``op``
  and ``stance``. The shipped prompt has asked for it since 2026-09-05
  (the provenance prompt), so a live dream normally carries it; a claim
  WITHOUT the field still writes exactly as it did before, which is what
  keeps an older prompt or a custom ``--system-prompt-file`` safe.
* ``memory.dream.assistant_claims`` (default ``"contender"``) decides
  what a ``speaker == "assistant"`` claim becomes: an ``assistant``-origin
  write that may fill an empty slot but parks as a contender against a
  stronger-tier current (``contender``), an ordinary agent-tier write
  (``supersede`` — the naive arm), or nothing at all (``drop``).
* An ``assistant``-origin fact ranks below an equally-similar
  user-origin one, on the live cortex ranking and on the offline
  rebuild that mirrors it.
"""
from __future__ import annotations

import json
import re
import tempfile

import pytest
import torch

from pseudolife_memory.memory.cortex import CortexStore
from pseudolife_memory.memory.dream import OpenAICompatExtractor
from pseudolife_memory.memory.slots import Slot
from pseudolife_memory.service import MemoryService
from tests.dream_helpers import StubExtractor, chat_payload, stub_server
from tests.helpers import unit_vec as _unit


@pytest.fixture()
def svc():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        yield MemoryService(data_dir=d)


def _claim(value, *, speaker=None, entity="miss-bee-providore",
           attribute="cuisine"):
    c = {"entity": entity, "attribute": attribute, "value": value,
         "confidence": 0.9, "origin": "agent", "source": 0}
    if speaker is not None:
        c["speaker"] = speaker
    return c


# ── parse boundary ───────────────────────────────────────────────────────

def _extract_one(raw_claim: dict) -> dict:
    """Run one raw model claim through the real HTTP parse path."""
    with stub_server(lambda: (200, chat_payload([raw_claim]))) as url:
        got = OpenAICompatExtractor(url, "m").extract(["note"], [])
    assert len(got) == 1
    return got[0]


def test_speaker_rides_through_the_parse_boundary():
    for who in ("user", "assistant"):
        c = _extract_one({"entity": "e", "attribute": "a", "value": "v",
                          "confidence": 0.8, "speaker": who})
        assert c["speaker"] == who


def test_speaker_is_normalised_like_op_and_stance():
    c = _extract_one({"entity": "e", "attribute": "a", "value": "v",
                      "confidence": 0.8, "speaker": "  ASSISTANT "})
    assert c["speaker"] == "assistant"


@pytest.mark.parametrize("bad", ["system", "", "   ", 3, None, ["user"]])
def test_an_unknown_speaker_is_ignored_not_carried(bad):
    """Same parse-boundary rule as ``op``: anything outside the whitelist
    degrades to absent rather than reaching the write path as a value the
    routing logic has never seen."""
    c = _extract_one({"entity": "e", "attribute": "a", "value": "v",
                      "confidence": 0.8, "speaker": bad})
    assert "speaker" not in c


def test_a_claim_without_speaker_parses_exactly_as_before():
    """Behaviour-neutrality at the parse boundary for a claim that carries
    no ``speaker`` — an older prompt, a custom ``--system-prompt-file``, or
    a model that dropped the field. It must parse byte-identically to the
    pre-2026-09-05 shape."""
    c = _extract_one({"entity": "e", "attribute": "a", "value": "v",
                      "confidence": 0.8, "source": 1})
    assert c == {"entity": "e", "attribute": "a", "value": "v",
                 "confidence": 0.8, "origin": "agent", "source": 0}


# ── write path: the three policies ───────────────────────────────────────

def test_the_shipped_default_is_contender(svc):
    assert svc.config.memory.dream.assistant_claims == "contender"


def test_an_assistant_claim_fills_an_empty_slot(svc):
    svc.store("Miss Bee Providore serves Indonesian brunch", source="agent")
    svc.dream_run(StubExtractor([_claim("Indonesian brunch",
                                        speaker="assistant")]))
    rec = svc.cortex_lookup("miss-bee-providore", "cuisine")
    assert rec["value"] == "Indonesian brunch"
    assert rec["origin"] == "assistant"


def test_an_assistant_claim_parks_against_a_user_origin_current(svc):
    svc.cortex_write("miss-bee-providore", "cuisine", "Sundanese",
                     support="user", provenance=["seed"])
    svc.store("Miss Bee Providore serves Indonesian brunch", source="agent")
    svc.dream_run(StubExtractor([_claim("Indonesian brunch",
                                        speaker="assistant")]))

    assert svc.cortex_lookup(
        "miss-bee-providore", "cuisine")["value"] == "Sundanese"
    conts = svc.cortex_contenders("miss-bee-providore", "cuisine")["contenders"]
    assert [c["value"] for c in conts] == ["Indonesian brunch"]


def test_an_assistant_claim_parks_against_an_agent_origin_current(svc):
    """"Non-assistant origin" is the rule, not "user origin": an ordinary
    agent-tier fact outranks an assistant restatement too."""
    svc.cortex_write("miss-bee-providore", "cuisine", "Sundanese",
                     support="agent", provenance=["seed"])
    svc.store("Miss Bee Providore serves Indonesian brunch", source="agent")
    svc.dream_run(StubExtractor([_claim("Indonesian brunch",
                                        speaker="assistant")]))

    assert svc.cortex_lookup(
        "miss-bee-providore", "cuisine")["value"] == "Sundanese"
    assert [c["value"] for c in svc.cortex_contenders(
        "miss-bee-providore", "cuisine")["contenders"]] == ["Indonesian brunch"]


def test_the_assistant_guard_holds_with_protect_provenance_off(svc):
    """The bench turns ``protect_provenance`` off to isolate extraction
    quality from the parking policy (``ladder_sweep.build_service``), and
    with it off the legacy path DROPS a conflicting value instead of
    parking it. The assistant guard is not part of that trade: it holds in
    both configurations, or the contender arm measures nothing."""
    svc.config.memory.cortex.protect_provenance = False
    svc.cortex_write("miss-bee-providore", "cuisine", "Sundanese",
                     support="user", provenance=["seed"])
    svc.store("Miss Bee Providore serves Indonesian brunch", source="agent")
    svc.dream_run(StubExtractor([_claim("Indonesian brunch",
                                        speaker="assistant")]))

    assert svc.cortex_lookup(
        "miss-bee-providore", "cuisine")["value"] == "Sundanese"
    assert [c["value"] for c in svc.cortex_contenders(
        "miss-bee-providore", "cuisine")["contenders"]] == ["Indonesian brunch"]


def test_a_user_claim_supersedes_an_assistant_origin_current(svc):
    """Assistant-origin values may be superseded by anything — the guard is
    one-directional."""
    svc.cortex_write("miss-bee-providore", "cuisine", "Indonesian brunch",
                     support="assistant", provenance=["seed"])
    svc.store("Miss Bee Providore is a Sundanese place", source="user")
    svc.dream_run(StubExtractor([_claim("Sundanese", speaker="user")]))

    assert svc.cortex_lookup(
        "miss-bee-providore", "cuisine")["value"] == "Sundanese"


def test_an_assistant_claim_supersedes_an_assistant_origin_current(svc):
    svc.cortex_write("miss-bee-providore", "cuisine", "Indonesian brunch",
                     support="assistant", provenance=["seed"])
    svc.store("Miss Bee Providore serves Sundanese food", source="agent")
    svc.dream_run(StubExtractor([_claim("Sundanese", speaker="assistant")]))

    assert svc.cortex_lookup(
        "miss-bee-providore", "cuisine")["value"] == "Sundanese"


def test_drop_writes_nothing_at_all(svc):
    svc.config.memory.dream.assistant_claims = "drop"
    svc.store("Miss Bee Providore serves Indonesian brunch", source="agent")
    svc.dream_run(StubExtractor([_claim("Indonesian brunch",
                                        speaker="assistant")]))

    assert svc.cortex_lookup("miss-bee-providore", "cuisine") is None
    assert svc.cortex_contenders(
        "miss-bee-providore", "cuisine")["contenders"] == []


def test_supersede_is_the_naive_arm_an_ordinary_agent_write(svc):
    """"Naive" means the label is never applied: the claim writes as an
    ordinary agent-tier dream claim, so it takes agent-tier routing —
    superseding an agent-origin current, and (as any agent claim does under
    the shipped ``protect_provenance``) parking against a user-origin one.
    Nothing about it is assistant-specific, which is the point of the arm."""
    svc.config.memory.dream.assistant_claims = "supersede"
    svc.cortex_write("miss-bee-providore", "cuisine", "Sundanese",
                     support="agent", provenance=["seed"])
    svc.store("Miss Bee Providore serves Indonesian brunch", source="agent")
    svc.dream_run(StubExtractor([_claim("Indonesian brunch",
                                        speaker="assistant")]))

    rec = svc.cortex_lookup("miss-bee-providore", "cuisine")
    assert rec["value"] == "Indonesian brunch"
    assert rec["origin"] == "agent"


def test_supersede_overwrites_the_user_value_in_the_bench_configuration(svc):
    """The naive arm as the BENCH runs it: ``build_service`` turns
    ``protect_provenance`` off, so the assistant claim overwrites the user's
    value outright — the pollution the contender arm exists to prevent."""
    svc.config.memory.dream.assistant_claims = "supersede"
    svc.config.memory.cortex.protect_provenance = False
    svc.cortex_write("miss-bee-providore", "cuisine", "Sundanese",
                     support="user", provenance=["seed"])
    svc.store("Miss Bee Providore serves Indonesian brunch", source="agent")
    svc.dream_run(StubExtractor([_claim("Indonesian brunch",
                                        speaker="assistant")]))

    assert svc.cortex_lookup(
        "miss-bee-providore", "cuisine")["value"] == "Indonesian brunch"


def test_a_user_speaker_claim_is_an_ordinary_agent_write(svc):
    """``speaker: "user"`` is a label about the TURN, not a tier promotion —
    it must not let model output claim user tier (2026-08-09 review: support
    is never taken from steerable claim text)."""
    svc.store("I always order the kopi susu", source="agent")
    svc.dream_run(StubExtractor([_claim("kopi susu", speaker="user",
                                        entity="user",
                                        attribute="usual order")]))
    assert svc.cortex_lookup("user", "usual order")["origin"] == "agent"


@pytest.mark.parametrize("policy", ["contender", "supersede", "drop"])
def test_a_speakerless_claim_writes_exactly_as_before(svc, policy):
    """Behaviour-neutrality at the write path, under every policy value:
    a claim with no ``speaker`` writes as it did before the label existed,
    whatever the policy is set to. Both routing outcomes are pinned — the
    same-tier supersede and the weaker-tier park."""
    svc.config.memory.dream.assistant_claims = policy

    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent",
                     provenance=["seed"])
    svc.store("payments-db host is db-prod-2", source="agent")
    svc.dream_run(StubExtractor([_claim("db-prod-2", entity="payments-db",
                                        attribute="host")]))
    rec = svc.cortex_lookup("payments-db", "host")
    assert (rec["value"], rec["origin"]) == ("db-prod-2", "agent")

    svc.cortex_write("cache", "host", "redis-1", support="user",
                     provenance=["seed"])
    svc.store("cache host is redis-9", source="agent")
    svc.dream_run(StubExtractor([_claim("redis-9", entity="cache",
                                        attribute="host")]))
    assert svc.cortex_lookup("cache", "host")["value"] == "redis-1"
    assert [c["value"] for c in svc.cortex_contenders(
        "cache", "host")["contenders"]] == ["redis-9"]


def test_an_unknown_policy_falls_back_to_the_guarded_behaviour(svc):
    """A typo'd config must never open the overwrite path — fail safe."""
    svc.config.memory.dream.assistant_claims = "supercede"
    svc.cortex_write("miss-bee-providore", "cuisine", "Sundanese",
                     support="user", provenance=["seed"])
    svc.store("Miss Bee Providore serves Indonesian brunch", source="agent")
    svc.dream_run(StubExtractor([_claim("Indonesian brunch",
                                        speaker="assistant")]))
    assert svc.cortex_lookup(
        "miss-bee-providore", "cuisine")["value"] == "Sundanese"


# ── ranking ──────────────────────────────────────────────────────────────

def test_an_assistant_fact_ranks_below_a_user_fact_at_equal_similarity():
    """Identical embeddings, so cosine cannot break the tie — only the
    origin demotion can. The assistant fact is written FIRST so the stable
    sort would keep it ahead without the multiplier."""
    store = CortexStore()
    emb = _unit(7)
    store.write_fact(Slot("brunch", "spot", "Miss Bee Providore"), emb,
                     support="assistant", now=1000.0)
    store.write_fact(Slot("brunch", "place", "Warung Ibu"), emb,
                     support="user", now=1001.0)

    ranked = store.search(emb, top_k=2)
    assert [r.value for r, _ in ranked] == ["Warung Ibu",
                                            "Miss Bee Providore"]
    assert ranked[0][1] > ranked[1][1]


def test_the_offline_fact_rebuild_mirrors_the_assistant_demotion():
    """``evals/rebuild_contexts.py`` re-implements the cortex ranking over a
    dumped bank; the lockstep that guards the BM25 fusion applies here too.
    A dumped fact carries ``origin``, so the mirror is origin-driven and a
    legacy bank (no assistant facts) ranks byte-identically."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from rebuild_contexts import rebuild_fact_lines

    class _Emb:
        """Every text and the query share one unit vector: a forced tie."""

        def encode(self, texts):
            return torch.stack([_unit(7) for _ in texts])

        def encode_query(self, text):
            return _unit(7)

    bank = {"question": "brunch", "facts": [
        {"entity": "brunch", "attribute": "spot",
         "value": "Miss Bee Providore", "origin": "assistant"},
        {"entity": "brunch", "attribute": "place",
         "value": "Warung Ibu", "origin": "user"},
    ]}
    lines = rebuild_fact_lines(bank, _Emb(), top_k=2, min_score=0.0)
    assert [ln.split(" — ")[0] for ln in lines] == ["brunch", "brunch"]
    assert "Warung Ibu" in lines[0] and "Miss Bee Providore" in lines[1]


# ── bench env knob ───────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ladder():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import ladder_sweep
    return ladder_sweep


@pytest.fixture()
def dream_cfg():
    from pseudolife_memory.utils.config import DreamConfig
    return DreamConfig()


def test_no_env_leaves_the_shipped_dream_default(ladder, dream_cfg,
                                                 monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_BENCH_ASSISTANT_CLAIMS", raising=False)
    ladder.apply_dream_env(dream_cfg)
    assert dream_cfg.assistant_claims == "contender"
    assert ladder.dream_env_knobs() == {"assistant_claims": None}


@pytest.mark.parametrize("policy", ["contender", "supersede", "drop"])
def test_every_accepted_policy_applies_and_stamps(ladder, dream_cfg,
                                                  monkeypatch, policy):
    monkeypatch.setenv("PSEUDOLIFE_BENCH_ASSISTANT_CLAIMS", policy)
    ladder.apply_dream_env(dream_cfg)
    assert dream_cfg.assistant_claims == policy
    assert ladder.dream_env_knobs() == {"assistant_claims": policy}


@pytest.mark.parametrize("bad", ["park", "SUPERSEDE", "1", "contender,drop"])
def test_a_bad_policy_aborts_rather_than_serving_the_default(
        ladder, dream_cfg, monkeypatch, bad):
    monkeypatch.setenv("PSEUDOLIFE_BENCH_ASSISTANT_CLAIMS", bad)
    with pytest.raises(SystemExit):
        ladder.apply_dream_env(dream_cfg)


def test_a_rung_stamp_reports_the_resolved_policy_not_the_override(
        ladder, dream_cfg, monkeypatch):
    """``dream_env_knobs`` answers "was an override given" (None = no); a
    rung artifact needs the policy that was actually in force, because a
    reader asking why a rung extracted more claims than it inserted has to
    know whether an assistant-labelled claim parked, dropped or
    superseded."""
    monkeypatch.delenv("PSEUDOLIFE_BENCH_ASSISTANT_CLAIMS", raising=False)
    ladder.apply_dream_env(dream_cfg)
    assert ladder.rung_bench_env(dream_cfg) == {
        "dream": {"assistant_claims": "contender"}}
    monkeypatch.setenv("PSEUDOLIFE_BENCH_ASSISTANT_CLAIMS", "drop")
    ladder.apply_dream_env(dream_cfg)
    assert ladder.rung_bench_env(dream_cfg) == {
        "dream": {"assistant_claims": "drop"}}


def test_the_rung_artifact_carries_the_stamp(ladder, monkeypatch):
    """Load-bearing half: a stamp helper nobody calls is decoration. The
    2026-09-05 `e4b-v3` post arm reported 19 claims / 18 inserted and its
    artifact could not say which policy produced that gap."""
    from types import SimpleNamespace

    from pseudolife_memory.utils.config import DreamConfig

    dream = DreamConfig()
    dream.assistant_claims = "drop"
    svc = SimpleNamespace(
        config=SimpleNamespace(memory=SimpleNamespace(dream=dream)))
    monkeypatch.setattr(ladder, "build_service", lambda _td: svc)
    monkeypatch.setattr(ladder, "ingest", lambda _svc: None)
    monkeypatch.setattr(ladder, "measure_naive",
                        lambda _svc: {"gold_recoverable": 1.0})
    out = ladder.run_rung("naive-rag")
    assert out["bench_env"] == {"dream": {"assistant_claims": "drop"}}


def test_the_policy_rides_in_the_bench_summary_stamp(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import longmemeval_bench as B

    monkeypatch.setenv("PSEUDOLIFE_BENCH_ASSISTANT_CLAIMS", "supersede")
    assert B.bench_env_knobs()["dream"] == {"assistant_claims": "supersede"}


# ── the prompt artifacts ─────────────────────────────────────────────────

def test_the_provenance_variant_is_the_shipped_prompt():
    """The provenance variant SHIPPED on 2026-09-05, so the measured
    artifact and the live constant are one string. Pinned byte-exact in
    both directions: an edit to `dream._SYSTEM_PROMPT` that does not reach
    the file (or the reverse) means the ladder gate and the 164-question
    run describe a prompt that is not the one running."""
    from pathlib import Path

    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT
    path = (Path(__file__).resolve().parents[1] / "evals" / "prompts"
            / "assistant_facts_provenance.txt")
    assert path.read_text(encoding="utf-8") == _SYSTEM_PROMPT, (
        "assistant_facts_provenance.txt is no longer the shipped prompt — "
        "re-run `PYTHONPATH=. python evals/gen_assistant_facts_prompts.py` "
        "instead of hand-editing either side")


def test_the_naive_variant_extends_the_pre_ship_base_verbatim():
    """The unguarded comparison arm stays anchored to the base the guard
    was measured against — it is what the guard's "costs nothing" read
    rests on, so it must not silently acquire the speaker rule."""
    from pathlib import Path

    from pseudolife_memory.memory.dream import _BASE_SYSTEM_PROMPT
    path = (Path(__file__).resolve().parents[1] / "evals" / "prompts"
            / "assistant_facts_naive.txt")
    text = path.read_text(encoding="utf-8")
    assert text.startswith(_BASE_SYSTEM_PROMPT), (
        "assistant_facts_naive.txt no longer opens with the pre-2026-09-05 "
        "base verbatim — regenerate it instead of hand-editing")
    assert len(text) > len(_BASE_SYSTEM_PROMPT)


def test_regenerating_would_reproduce_both_files_exactly():
    """The files are generated, so the generator is the thing under test:
    a regeneration must be a no-op on a clean tree. Asserted against the
    generator's own composition rather than by re-running it, so a
    hand-edited ``.txt`` fails here instead of being silently overwritten
    by the guard that is supposed to catch it."""
    from pathlib import Path

    g = _gen_module()
    prompts = Path(__file__).resolve().parents[1] / "evals" / "prompts"
    for name, text in g.FILES.items():
        assert (prompts / name).read_text(encoding="utf-8") == text, (
            f"{name} differs from what the generator would write — re-run "
            "`PYTHONPATH=. python evals/gen_assistant_facts_prompts.py`")


def test_importing_the_generator_does_not_rewrite_the_artifacts():
    """The module used to write on import, so any suite that imported it
    regenerated the committed prompts underneath its own assertions — a
    drifted artifact was repaired by the guard instead of failing it
    (found 2026-09-05). Writing is behind ``__main__`` now."""
    from pathlib import Path

    import importlib

    prompts = Path(__file__).resolve().parents[1] / "evals" / "prompts"
    names = ("assistant_facts_naive.txt", "assistant_facts_provenance.txt")
    stamps = {n: (prompts / n).stat().st_mtime_ns for n in names}
    # reload, not import: import_module is cached, so a cached module would
    # make this assertion pass without ever re-running the body.
    importlib.reload(_gen_module())
    assert {n: (prompts / n).stat().st_mtime_ns for n in names} == stamps


def test_only_the_provenance_variant_asks_for_a_speaker_field():
    from pathlib import Path
    prompts = Path(__file__).resolve().parents[1] / "evals" / "prompts"
    naive = (prompts / "assistant_facts_naive.txt").read_text(encoding="utf-8")
    prov = (prompts / "assistant_facts_provenance.txt").read_text(
        encoding="utf-8")
    assert "speaker" not in naive
    assert '"speaker":"user"' in prov.replace(" ", "")
    assert '"speaker":"assistant"' in prov.replace(" ", "")


def test_a_prompt_file_reaches_the_extractor_with_the_hints_appended(
        monkeypatch):
    """``--system-prompt-file`` must render the file AS the system prompt
    with the vocab/known-facts hints appended exactly as the shipped prompt
    gets them — otherwise a prompt-variant run measures a different call
    shape, not a different prompt. Asserted on the wire body, via the same
    ``_make_extractor`` the bench builds its extractor with."""
    import io
    import sys
    import urllib.request
    from pathlib import Path

    from pseudolife_memory.memory.dream import _facts_hint, _vocab_hint
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import longmemeval_bench as B

    path = (Path(__file__).resolve().parents[1] / "evals" / "prompts"
            / "assistant_facts_provenance.txt")
    text = path.read_text(encoding="utf-8")
    sent: list[dict] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        return _Resp(chat_payload([]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    ex = B._make_extractor("http://127.0.0.1:0", str(path))
    ex.extract(["note one"], ["vocab-term"], known_facts=[("e", "a", "v")])

    system = sent[0]["messages"][0]["content"]
    assert system == text + _vocab_hint(["vocab-term"]) + _facts_hint(
        [("e", "a", "v")])
    assert sent[0]["messages"][1]["content"] == "[1] note one"


# ── set-valued slots (review fold, 2026-09-05) ───────────────────────────
#
# The first cut of the guard covered SCALAR writes only: ``op: "add"``
# routed to ``set_add(..., origin="assistant")`` and ``add_member`` never
# consulted the tier ladder, so an assistant-stated claim silently
# one-way-converted a user's scalar into a set and landed as a current
# member; ``set_remove`` took no origin at all, so an assistant claim could
# retract a user's member. The property the guard is supposed to have is
# "an assistant-origin write may never change a non-assistant current value
# OR member set", and these pin it on the member model too.

def _member_claim(value, op, *, speaker=None, entity="the-quillon-larder",
                  attribute="menu"):
    c = {"entity": entity, "attribute": attribute, "value": value,
         "confidence": 0.9, "origin": "agent", "source": 0, "op": op}
    if speaker is not None:
        c["speaker"] = speaker
    return c


def test_an_assistant_add_does_not_convert_a_user_scalar(svc):
    """The scalar→set conversion is one-way and destroys the user's value as
    a scalar. An assistant-stated member add must not be able to trigger it."""
    svc.cortex_write("the-quillon-larder", "menu", "seasonal tasting menu",
                     support="user", provenance=["seed"])
    svc.store("The Quillon Larder also serves an ember-plum tart",
              source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("ember-plum tart", "add", speaker="assistant")]))

    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert got["kind"] == "scalar"
    assert got["value"] == "seasonal tasting menu"
    assert got["origin"] == "user"
    assert [c["value"] for c in svc.cortex_contenders(
        "the-quillon-larder", "menu")["contenders"]] == ["ember-plum tart"]


def test_an_assistant_add_parks_against_a_user_origin_member_set(svc):
    svc.set_add("the-quillon-larder", "menu", "kaya toast", origin="user")
    svc.store("The Quillon Larder also serves an ember-plum tart",
              source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("ember-plum tart", "add", speaker="assistant")]))

    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert [m["value"] for m in got["members"]] == ["kaya toast"]
    assert [c["value"] for c in svc.cortex_contenders(
        "the-quillon-larder", "menu")["contenders"]] == ["ember-plum tart"]


def test_an_assistant_add_fills_an_empty_slot(svc):
    """Same rule as the scalar path: nothing is being taken from anyone, so
    the claim is written — at the ``assistant`` origin."""
    svc.store("The Quillon Larder serves an ember-plum tart", source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("ember-plum tart", "add", speaker="assistant")]))

    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert [m["value"] for m in got["members"]] == ["ember-plum tart"]
    assert [m["origin"] for m in got["members"]] == ["assistant"]


def test_an_assistant_add_joins_an_all_assistant_member_set(svc):
    """The guard is one-directional here too: a set the assistant alone
    built is the assistant's to extend."""
    svc.set_add("the-quillon-larder", "menu", "kaya toast", origin="assistant")
    svc.store("The Quillon Larder also serves an ember-plum tart",
              source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("ember-plum tart", "add", speaker="assistant")]))

    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert sorted(m["value"] for m in got["members"]) == [
        "ember-plum tart", "kaya toast"]
    assert svc.cortex_contenders(
        "the-quillon-larder", "menu")["contenders"] == []


def test_an_assistant_add_of_an_existing_user_member_confirms_it(svc):
    """Corroboration is not a change to the member set — the same rule the
    scalar path already follows (``_confirm`` runs before the guard)."""
    svc.set_add("the-quillon-larder", "menu", "kaya toast", origin="user")
    svc.store("The Quillon Larder serves kaya toast", source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("kaya toast", "add", speaker="assistant")]))

    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert [m["value"] for m in got["members"]] == ["kaya toast"]
    assert [m["origin"] for m in got["members"]] == ["user"]
    assert svc.cortex_contenders(
        "the-quillon-larder", "menu")["contenders"] == []


def test_an_assistant_remove_of_a_user_member_is_dropped(svc):
    svc.set_add("the-quillon-larder", "menu", "kaya toast", origin="user")
    svc.store("The Quillon Larder no longer serves kaya toast", source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("kaya toast", "remove", speaker="assistant")]))

    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert [m["value"] for m in got["members"]] == ["kaya toast"]
    assert got["removed"] == []


def test_an_assistant_remove_of_an_assistant_member_is_allowed(svc):
    svc.set_add("the-quillon-larder", "menu", "kaya toast", origin="assistant")
    svc.store("The Quillon Larder no longer serves kaya toast", source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("kaya toast", "remove", speaker="assistant")]))

    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert got["members"] == []
    assert [m["value"] for m in got["removed"]] == ["kaya toast"]


def test_a_user_speaker_remove_still_retracts_a_user_member(svc):
    """``speaker: "user"`` is never a demotion — the claim stays an ordinary
    agent-tier write, and agent-tier removes are unguarded as before."""
    svc.set_add("the-quillon-larder", "menu", "kaya toast", origin="user")
    svc.store("The Quillon Larder no longer serves kaya toast", source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("kaya toast", "remove", speaker="user")]))

    assert svc.cortex_lookup("the-quillon-larder", "menu")["members"] == []


def test_the_set_guard_holds_with_protect_provenance_off(svc):
    """Same trade as the scalar guard: the bench turns ``protect_provenance``
    off to isolate extraction quality, and the assistant guard is not part
    of that trade."""
    svc.config.memory.cortex.protect_provenance = False
    svc.set_add("the-quillon-larder", "menu", "kaya toast", origin="user")
    svc.store("The Quillon Larder also serves an ember-plum tart",
              source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("ember-plum tart", "add", speaker="assistant")]))

    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert [m["value"] for m in got["members"]] == ["kaya toast"]


def test_supersede_keeps_the_legacy_set_behaviour(svc):
    """The naive arm: the label is never applied, so member ops route as
    ordinary agent-tier dream claims — conversion and retraction included."""
    svc.config.memory.dream.assistant_claims = "supersede"
    svc.cortex_write("the-quillon-larder", "menu", "seasonal tasting menu",
                     support="user", provenance=["seed"])
    svc.store("The Quillon Larder also serves an ember-plum tart",
              source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("ember-plum tart", "add", speaker="assistant")]))

    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert got["kind"] == "set"
    assert sorted(m["value"] for m in got["members"]) == [
        "ember-plum tart", "seasonal tasting menu"]

    svc.set_add("kedai-sembilir", "menu", "kaya toast", origin="user")
    svc.store("Kedai Sembilir no longer serves kaya toast", source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("kaya toast", "remove", speaker="assistant",
                      entity="kedai-sembilir")]))
    assert svc.cortex_lookup("kedai-sembilir", "menu")["members"] == []


def test_drop_writes_no_member_and_retracts_none(svc):
    svc.config.memory.dream.assistant_claims = "drop"
    svc.set_add("the-quillon-larder", "menu", "kaya toast", origin="user")
    svc.store("The Quillon Larder also serves an ember-plum tart",
              source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("ember-plum tart", "add", speaker="assistant"),
        _member_claim("kaya toast", "remove", speaker="assistant")]))

    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert [m["value"] for m in got["members"]] == ["kaya toast"]
    assert svc.cortex_contenders(
        "the-quillon-larder", "menu")["contenders"] == []


@pytest.mark.parametrize("policy", ["contender", "supersede", "drop"])
def test_a_speakerless_set_claim_writes_exactly_as_before(svc, policy):
    """Behaviour-neutrality extended to the member model: a claim with no
    ``speaker`` must still convert and retract under every policy value."""
    svc.config.memory.dream.assistant_claims = policy

    svc.cortex_write("the-quillon-larder", "menu", "seasonal tasting menu",
                     support="user", provenance=["seed"])
    svc.store("The Quillon Larder also serves an ember-plum tart",
              source="agent")
    svc.dream_run(StubExtractor([_member_claim("ember-plum tart", "add")]))
    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert got["kind"] == "set"
    assert sorted(m["value"] for m in got["members"]) == [
        "ember-plum tart", "seasonal tasting menu"]

    svc.set_add("kedai-sembilir", "menu", "kaya toast", origin="user")
    svc.store("Kedai Sembilir no longer serves kaya toast", source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("kaya toast", "remove", entity="kedai-sembilir")]))
    assert svc.cortex_lookup("kedai-sembilir", "menu")["members"] == []


def test_a_set_slot_contender_cannot_be_resolved_in_either_direction(svc):
    """Characterisation, not a fix (2026-09-05 review). The guard parks
    through the SCALAR contender machinery, and ``CortexStore.resolve``
    refuses any slot holding current members in BOTH directions — so an
    assistant contender parked against a live member set can be read but
    neither promoted nor rejected. Only promotion needs that refusal;
    narrowing it is a decision about the review queue's contract and was
    deliberately left outside this fold. Pinned here so the limit is a
    watched fact rather than a surprise, and so narrowing it later is a
    conscious edit to this test."""
    svc.set_add("the-quillon-larder", "menu", "kaya toast", origin="user")
    svc.store("The Quillon Larder also serves an ember-plum tart",
              source="agent")
    svc.dream_run(StubExtractor([
        _member_claim("ember-plum tart", "add", speaker="assistant")]))
    assert [c["value"] for c in svc.cortex_contenders(
        "the-quillon-larder", "menu")["contenders"]] == ["ember-plum tart"]

    for accept in (True, False):
        res = svc.cortex_resolve("the-quillon-larder", "menu", accept=accept)
        assert res["resolved"] is False and res["reason"] == "slot_holds_set"

    # Nothing moved in either direction: the user's member is still the
    # only current one, and the contender is still parked.
    got = svc.cortex_lookup("the-quillon-larder", "menu")
    assert [m["value"] for m in got["members"]] == ["kaya toast"]
    assert [c["value"] for c in svc.cortex_contenders(
        "the-quillon-larder", "menu")["contenders"]] == ["ember-plum tart"]


# ── the same guard, at the engine boundary ───────────────────────────────

def test_add_member_refuses_to_convert_a_non_assistant_scalar():
    store = CortexStore()
    emb = _unit(3)
    store.write_fact(Slot("larder", "menu", "seasonal tasting menu"), emb,
                     support="user", now=1000.0)
    res = store.add_member(Slot("larder", "menu", "ember-plum tart"),
                           _unit(4), support="assistant", now=1001.0)

    assert res.action == "contested"
    assert store.slot_kind("larder", "menu") == "scalar"
    assert store.lookup("larder", "menu").value == "seasonal tasting menu"
    assert [c.value for c in store.contenders_for("larder", "menu")] == [
        "ember-plum tart"]


def test_add_member_parks_against_a_non_assistant_member_set():
    store = CortexStore()
    store.add_member(Slot("larder", "menu", "kaya toast"), _unit(3),
                     support="user", now=1000.0)
    res = store.add_member(Slot("larder", "menu", "ember-plum tart"),
                           _unit(4), support="assistant", now=1001.0)

    assert res.action == "contested"
    assert [m.value for m in store.members("larder", "menu")] == ["kaya toast"]


def test_remove_member_refuses_an_assistant_retraction_of_a_user_member():
    store = CortexStore()
    store.add_member(Slot("larder", "menu", "kaya toast"), _unit(3),
                     support="user", now=1000.0)
    res = store.remove_member("larder", "menu", "kaya toast",
                              support="assistant", now=1001.0)

    assert res.action == "member_remove_refused"
    assert [m.value for m in store.members("larder", "menu")] == ["kaya toast"]
    assert store.members("larder", "menu")[0].status == "current"


def test_remove_member_without_a_support_argument_is_unguarded():
    """The MCP tool and the rollback replay call it positionally, with no
    origin — an explicit human retraction is never second-guessed."""
    store = CortexStore()
    store.add_member(Slot("larder", "menu", "kaya toast"), _unit(3),
                     support="user", now=1000.0)
    assert store.remove_member(
        "larder", "menu", "kaya toast", now=1001.0).action == "member_removed"


# ── the worked examples must not name anything the bench measures ────────
#
# Review finding, 2026-09-05: the first cut of these prompts built its
# worked example around "Miss Bee Providore ... Bandung", which is the gold
# answer of LongMemEval question ``c4f10528`` — a single-session-assistant
# question in the measured slice, and a counted win for cortex, hybrid and
# cascade in BOTH variants. The prompt was handing the model an answer.
# These two tests are the guard that makes the class un-repeatable: every
# capitalised word in a worked example must be a registered invented token
# (or ordinary sentence English), and no registered token may occur
# anywhere in either dataset file.

# Ordinary sentence-initial / pronoun capitals that carry no identity. Any
# OTHER capitalised word must be registered in EXAMPLE_TOKENS, which is what
# forces the dataset check below to see it.
_SENTENCE_CAPITALS = frozenset({"Example", "Notes", "Output", "For", "I"})


def _gen_module():
    import importlib
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    return importlib.import_module("gen_assistant_facts_prompts")


def _example_blocks():
    g = _gen_module()
    return {"naive": g.NAIVE_EXAMPLE, "provenance": g.PROVENANCE_EXAMPLE}


@pytest.mark.parametrize("which", ["naive", "provenance"])
def test_every_proper_noun_in_a_worked_example_is_a_registered_token(which):
    """Structural half of the guard — runs with or without the datasets. A
    new proper noun in an example cannot reach the prompt files without
    being registered, and registering it is what subjects it to the
    dataset check below."""
    import re

    block = _example_blocks()[which]
    for token in _gen_module().EXAMPLE_TOKENS:
        block = block.replace(token, " ")
    leftovers = sorted({
        m.group(0) for m in re.finditer(r"[A-Z][A-Za-z]*", block)
        if m.group(0) not in _SENTENCE_CAPITALS
    })
    assert leftovers == [], (
        f"unregistered proper noun(s) in the {which} example: {leftovers} — "
        "add them to EXAMPLE_TOKENS (which grep-checks them against the "
        "LongMemEval datasets) or rewrite the example")


@pytest.mark.parametrize("dataset", ["longmemeval_oracle.json",
                                     "longmemeval_s_cleaned.json"])
def test_no_worked_example_token_occurs_in_the_measured_dataset(dataset):
    """Evidence half of the guard. Streamed in chunks with an overlap — the
    ``_s`` file is ~277 MB and must not be read into RAM whole. Skipped when
    the (gitignored) data is absent; the structural test above still runs."""
    from pathlib import Path

    path = (Path(__file__).resolve().parents[1] / "evals" / "data" / dataset)
    if not path.exists():
        pytest.skip(f"{dataset} not present (evals/data is gitignored)")

    tokens = [t.encode("utf-8") for t in _gen_module().EXAMPLE_TOKENS]
    assert tokens, "EXAMPLE_TOKENS is empty — the guard would be vacuous"
    overlap = max(len(t) for t in tokens)
    counts = {t: 0 for t in tokens}
    prev = b""
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(8 << 20)
            if not chunk:
                break
            buf = prev + chunk
            for t in tokens:
                counts[t] += buf.count(t)
            prev = buf[-overlap:]
    hits = {t.decode(): n for t, n in counts.items() if n}
    assert hits == {}, (
        f"worked-example token(s) occur in {dataset}: {hits} — the prompt is "
        "naming content the bench measures; re-cut the example on invented "
        "names")


def test_the_shipped_prompt_is_covered_by_the_example_token_guard():
    """Since 2026-09-05 the SHIPPED ``_SYSTEM_PROMPT`` carries the worked
    example, so it is the live carrier of these invented names — not an
    eval-only file. Asserting each registered token occurs in the shipped
    prompt is what makes the dataset grep above a guard on the extractor
    rather than on a variant nobody runs."""
    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT

    missing = [t for t in _gen_module().EXAMPLE_TOKENS
               if t not in _SYSTEM_PROMPT]
    assert missing == [], (
        f"registered token(s) absent from the shipped prompt: {missing} — "
        "the dataset grep would no longer be guarding what runs")


def test_every_registered_token_is_actually_used_by_a_prompt_file():
    """A registry that has drifted away from the examples guards nothing."""
    from pathlib import Path

    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT
    prompts = Path(__file__).resolve().parents[1] / "evals" / "prompts"
    text = _SYSTEM_PROMPT + "".join(
        (prompts / name).read_text(encoding="utf-8")
        for name in ("assistant_facts_naive.txt",
                     "assistant_facts_provenance.txt"))
    unused = [t for t in _gen_module().EXAMPLE_TOKENS if t not in text]
    assert unused == [], f"EXAMPLE_TOKENS entries no example uses: {unused}"


# ── the same guard, over the WHOLE shipped prompt ────────────────────────
#
# Review finding, 2026-09-05 (the shim-prompt fold). The two tests above
# scan `NAIVE_EXAMPLE` and `PROVENANCE_EXAMPLE` — the blocks the provenance
# change ADDED — while the comment above them claims they "make the class
# un-repeatable". They do not: `_BASE_SYSTEM_PROMPT` carries three worked
# examples of its own, written before the invented-token rule existed, and
# nothing was checking them. One of them names a LongMemEval answer (see
# `KNOWN_CORPUS_COLLISIONS`), and it has been in the prompt every extractor
# receives since 2026-08-01 — the sidecar, the shim, and every ladder rung
# alike, not only the shim path.
#
# The prompt uses ALL-CAPS for emphasis throughout ("COUNTS, TOTALS, AND
# QUANTITIES", "OMIT", "CURRENT"), so a caps-inclusive scan would need a
# ~20-entry exemption list per prompt that would rot faster than it guards.
# The scan is therefore over TITLECASE phrases — which is how names are
# written both here and in the corpus — and that limit is stated rather
# than implied away.

# Titlecase word, optional possessive, optionally chained. Non-ASCII
# initials included (the corpus carries accented names); ALL-CAPS runs
# deliberately excluded, see above.
_TITLECASE = re.compile(
    r"[A-Z\u00C0-\u00DE][a-z\u00DF-\u00FF]+(?:['\u2019][A-Za-z]+)?"
    r"(?:\s+[A-Z\u00C0-\u00DE][a-z\u00DF-\u00FF]+(?:['\u2019][A-Za-z]+)?)*")

# Ordinary sentence-initial capitals in the shipped prompt. Every OTHER
# titlecase phrase must be a registered token or a recorded pre-rule name.
_SHIPPED_SENTENCE_CAPITALS = frozenset({
    "Example", "Extract", "For", "Key", "Notes", "One", "Output", "Return",
    "Reuse", "What", "When", "You",
})


def _shipped_titlecase() -> set:
    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT
    g = _gen_module()
    text = _SYSTEM_PROMPT
    for token in g.EXAMPLE_TOKENS:
        text = text.replace(token, " ")
    return {m.group(0) for m in _TITLECASE.finditer(text)
            if m.group(0) not in _SHIPPED_SENTENCE_CAPITALS}


def test_every_titlecase_name_in_the_shipped_prompt_is_accounted_for():
    """Structural half, over the whole shipped prompt rather than the two
    blocks the provenance change added."""
    g = _gen_module()
    found = _shipped_titlecase()
    assert found, "no name found in the shipped prompt — the scan is vacuous"
    unaccounted = sorted(found - set(g.PRE_RULE_PROPER_NOUNS))
    assert unaccounted == [], (
        f"unregistered proper noun(s) in the shipped prompt: {unaccounted} — "
        "add them to EXAMPLE_TOKENS (which grep-checks them against the "
        "LongMemEval datasets) or rewrite the example")


def test_the_pre_rule_names_are_really_in_the_shipped_prompt():
    """The exemption list must stay a statement about the prompt, not a
    parking bay: every entry has to actually occur in it."""
    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT
    g = _gen_module()
    absent = sorted(n for n in g.PRE_RULE_PROPER_NOUNS
                    if n not in _SYSTEM_PROMPT)
    assert absent == [], (
        f"listed as a pre-rule prompt name but absent from it: {absent}")
    extra = sorted(set(g.KNOWN_CORPUS_COLLISIONS) - set(g.PRE_RULE_PROPER_NOUNS))
    assert extra == [], (
        f"collision list names a phrase the prompt does not carry: {extra}")


@pytest.mark.parametrize("dataset", ["longmemeval_oracle.json",
                                     "longmemeval_s_cleaned.json"])
def test_the_shipped_prompts_names_hit_only_known_collisions(dataset):
    """Evidence half, over the whole shipped prompt.

    EQUALITY, not a subset: a newly contaminated name fails, and so does a
    listed collision that has stopped hitting. Both directions matter — an
    allowlist nobody can retire is how a disclosure turns into decoration.
    """
    from pathlib import Path

    path = (Path(__file__).resolve().parents[1] / "evals" / "data" / dataset)
    if not path.exists():
        pytest.skip(f"{dataset} not present (evals/data is gitignored)")

    g = _gen_module()
    names = sorted(_shipped_titlecase())
    probes = [n.encode("utf-8") for n in names]
    assert probes, "no name reaches the scan — vacuous"
    overlap = max(len(p) for p in probes)
    counts = {p: 0 for p in probes}
    prev = b""
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(8 << 20)
            if not chunk:
                break
            buf = prev + chunk
            for p in probes:
                counts[p] += buf.count(p)
            prev = buf[-overlap:]
    hits = sorted(p.decode() for p, n in counts.items() if n)
    assert hits == sorted(g.KNOWN_CORPUS_COLLISIONS), (
        f"shipped-prompt name(s) occur in {dataset}: {hits}; known and "
        f"allowed: {sorted(g.KNOWN_CORPUS_COLLISIONS)}. A name not on that "
        "list means the SHIPPED prompt is handing the model content the "
        "bench measures. A listed one that no longer hits means the debt is "
        "paid: delete its entry.")
