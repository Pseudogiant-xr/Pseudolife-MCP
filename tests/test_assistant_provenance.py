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
  and ``stance``. The shipped prompt never asks for it, so every shipped
  path is byte-identical.
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
    """Behaviour-neutrality at the parse boundary: the shipped prompt never
    asks for ``speaker``, so a shipped claim must be byte-identical."""
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
    the shipped prompt emits no ``speaker``, so nothing about the shipped
    dream moves. Both routing outcomes are pinned — the same-tier
    supersede and the weaker-tier park."""
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


def test_the_policy_rides_in_the_bench_summary_stamp(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import longmemeval_bench as B

    monkeypatch.setenv("PSEUDOLIFE_BENCH_ASSISTANT_CLAIMS", "supersede")
    assert B.bench_env_knobs()["dream"] == {"assistant_claims": "supersede"}


# ── the prompt artifacts ─────────────────────────────────────────────────

def test_both_prompt_variants_extend_the_shipped_prompt_verbatim():
    """The variants are the shipped ``_SYSTEM_PROMPT`` plus an appended
    block — pinned so a shipped-prompt edit cannot silently leave the
    measured variants describing a different extractor."""
    from pathlib import Path

    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT
    prompts = Path(__file__).resolve().parents[1] / "evals" / "prompts"
    for name in ("assistant_facts_naive.txt",
                 "assistant_facts_provenance.txt"):
        text = (prompts / name).read_text(encoding="utf-8")
        assert text.startswith(_SYSTEM_PROMPT), (
            f"{name} no longer opens with the shipped _SYSTEM_PROMPT "
            "verbatim — regenerate it instead of hand-editing")
        assert len(text) > len(_SYSTEM_PROMPT)


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
