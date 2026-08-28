"""Provenance-span gate — Feature B of the 2026-08-12 stance+span-gate
design (no schema change).

Generalizes the literal-faithfulness gate (2026-08-02, digit-bearing
tokens only) to whole claims: the extractor emits a ``quote`` — a verbatim
span from the note the claim cites — and the claim loop verifies
containment against THAT note (source scope is correct by construction
for a quote; the literal gate's measured batch-scope false-drop classes
were about checking the VALUE against a corpus, not a quote against its
note).

Modes (``memory.dream.span_gate``): ``off`` (shipped default — the live
v5 prompt emits no quotes, so any other default would fire on 100% of
claims and the counters would be noise), ``log`` (count + log, write
unchanged; what the gate-4 audit runs beside the v9 prompt), ``contend``
(a scalar claim whose quote is missing or unverifiable parks as a
contender via ``force_contend`` with a ``span:unbacked`` provenance
marker — visible, resolvable, never silently dropped; member ops are
counted but not parked in v1, set writes have no contender path).

Boundary honesty (same as the two-man rule spec): this checks
fidelity-to-source, NOT trustworthiness-of-source — a poisoned note
quotes itself perfectly. The composition tests proving span-fidelity
cannot launder trust past the quarantine live in
``test_dream_quarantine.py``.
"""
from __future__ import annotations

import tempfile

import pytest

from pseudolife_memory.service import MemoryService
from tests.dream_helpers import (StubExtractor as _StubExtractor,
                                 chat_payload as _chat_payload,
                                 stub_server as _stub_server)


@pytest.fixture()
def svc():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d)
        yield s


def _svc_with_span_mode(svc, mode):
    svc.config.memory.dream.span_gate = mode
    return svc


# ── the containment check (pure) ─────────────────────────────────────────

def test_span_unbacked_reasons():
    from pseudolife_memory.memory.dream import span_unbacked

    note = 'The health probe now polls every 30 seconds (see ops/probe.ps1).'
    assert span_unbacked("polls every 30 seconds", note) is None
    # Case, whitespace and punctuation runs are normalized on both sides.
    assert span_unbacked("HEALTH PROBE  now polls", note) is None
    assert span_unbacked('polls every 30 seconds (see ops/probe.ps1)',
                         note) is None
    # Paraphrase is not a quote.
    assert span_unbacked("checks twice a minute", note) == "quote_unverified"
    # Missing / blank quote.
    assert span_unbacked(None, note) == "quote_missing"
    assert span_unbacked("   ", note) == "quote_missing"
    # Empty corpus: a quote with nothing to verify against is unverified —
    # an UNCITED claim's routing is the loop's decision, but this function
    # never passes what it cannot check.
    assert span_unbacked("anything", "") == "quote_unverified"


def test_span_norm_is_unicode_safe():
    """Review fix (2026-08-13): the first cut used an ASCII-only class,
    which fragmented accented words and reduced CJK notes to nothing —
    a verbatim quote then failed containment. NFKC + \\w keeps letters
    of every script and equates Unicode normal forms."""
    import unicodedata

    from pseudolife_memory.memory.dream import span_unbacked

    nfc = unicodedata.normalize("NFC", "the café's hours changed")
    nfd = unicodedata.normalize("NFD", "café's hours")
    assert span_unbacked(nfd, nfc) is None          # NFC vs NFD verbatim
    assert span_unbacked("居酒屋は木曜定休", "メモ: 居酒屋は木曜定休です") is None
    assert span_unbacked("居酒屋は金曜定休", "メモ: 居酒屋は木曜定休です") \
        == "quote_unverified"
    # A model that STRIPS diacritics altered the quote — documented
    # residual false-drop class, deliberately not equated.
    assert span_unbacked("cafe s hours", nfc) == "quote_unverified"


def test_span_unbacked_is_source_scoped_by_caller():
    """The gate hands this function the CITED note only — a quote lifted
    from a different note in the batch must fail against it."""
    from pseudolife_memory.memory.dream import span_unbacked

    cited = "we moved the deploy target to prod-eu"
    other = "the health probe polls every 30 seconds"
    assert span_unbacked("polls every 30 seconds", cited) == "quote_unverified"
    assert span_unbacked("polls every 30 seconds", other) is None


# ── parse boundary ────────────────────────────────────────────────────────

def test_openai_extractor_carries_quote_through_parse():
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    payload = _chat_payload([
        {"entity": "health probe", "attribute": "poll interval",
         "value": "every 30 seconds", "confidence": 0.9,
         "quote": "polls every 30 seconds", "source": 1},
        {"entity": "svc", "attribute": "port", "value": "8080",
         "confidence": 0.9, "source": 1},
    ])
    with _stub_server(lambda: (200, payload)) as base_url:
        claims = OpenAICompatExtractor(base_url, "m").extract(
            ["whatever"], vocab=[])
    assert claims[0]["quote"] == "polls every 30 seconds"
    assert "quote" not in claims[1]


def test_quote_parse_caps_and_drops_junk():
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    payload = _chat_payload([
        {"entity": "a", "attribute": "b", "value": "v1",
         "confidence": 0.5, "quote": "x" * 500},
        {"entity": "a", "attribute": "c", "value": "v2",
         "confidence": 0.5, "quote": ["not", "a", "string"]},
    ])
    with _stub_server(lambda: (200, payload)) as base_url:
        claims = OpenAICompatExtractor(base_url, "m").extract(
            ["whatever"], vocab=[])
    by_attr = {c["attribute"]: c for c in claims}
    assert len(by_attr["b"]["quote"]) == 200
    assert "quote" not in by_attr["c"]


# ── routing: off / log / contend ─────────────────────────────────────────

def test_span_gate_defaults_off_and_writes_unchanged(svc):
    assert svc.config.memory.dream.span_gate == "off"
    svc.store("the deploy target moved to prod-eu", source="pseudolife")
    res = svc.dream_run(_StubExtractor([
        {"entity": "deploy target", "attribute": "environment",
         "value": "prod-eu", "confidence": 0.9, "origin": "agent",
         "source": 0},
    ]))
    assert res["inserted"] == 1
    assert res.get("span_flagged", 0) == 0 and res.get("span_parked", 0) == 0


def test_log_mode_counts_but_still_writes(svc):
    _svc_with_span_mode(svc, "log")
    svc.store("the deploy target moved to prod-eu", source="pseudolife")
    res = svc.dream_run(_StubExtractor([
        # verified quote — clean
        {"entity": "deploy target", "attribute": "environment",
         "value": "prod-eu", "confidence": 0.9, "origin": "agent",
         "quote": "moved to prod-eu", "source": 0},
        # missing quote — flagged, still written
        {"entity": "svc", "attribute": "port", "value": "8080",
         "confidence": 0.9, "origin": "agent", "source": 0},
        # unverifiable quote — flagged, still written
        {"entity": "svc", "attribute": "os", "value": "linux",
         "confidence": 0.9, "origin": "agent",
         "quote": "runs on linux boxes", "source": 0},
    ]))
    assert res["inserted"] == 3
    assert res["span_flagged"] == 2
    assert res.get("span_parked", 0) == 0


def test_contend_mode_parks_unbacked_scalar_as_contender(svc):
    _svc_with_span_mode(svc, "contend")
    svc.cortex_write("deploy target", "environment", "staging",
                     confidence=0.9, support="user")
    svc.store("chatter that does not back the claim", source="pseudolife")
    res = svc.dream_run(_StubExtractor([
        {"entity": "deploy target", "attribute": "environment",
         "value": "prod-eu", "confidence": 0.95, "origin": "agent",
         "quote": "we definitely moved to prod-eu", "source": 0},
    ]))
    assert res["span_parked"] == 1
    # Current value untouched; the unbacked claim is a visible contender.
    rec = svc.cortex_lookup("deploy target", "environment")
    assert rec["value"] == "staging"
    conts = svc.cortex_contenders("deploy target", "environment")
    assert conts["contenders"] and \
        conts["contenders"][0]["value"] == "prod-eu"
    assert "span:unbacked" in conts["contenders"][0]["provenance"]


def test_contend_mode_verified_quote_writes_normally(svc):
    _svc_with_span_mode(svc, "contend")
    svc.store("the deploy target moved to prod-eu today",
              source="pseudolife")
    res = svc.dream_run(_StubExtractor([
        {"entity": "deploy target", "attribute": "environment",
         "value": "prod-eu", "confidence": 0.9, "origin": "agent",
         "quote": "deploy target moved to prod-eu", "source": 0},
    ]))
    assert res["inserted"] == 1 and res["span_parked"] == 0
    assert svc.cortex_lookup("deploy target", "environment")["value"] == "prod-eu"


def test_contend_mode_counts_member_ops_but_does_not_park_them(svc):
    """v1 scope: member ops have no contender path — an unbacked quote on
    an op:"add" claim is counted (span_flagged) and the add proceeds under
    its existing guards."""
    _svc_with_span_mode(svc, "contend")
    svc.store("tried a new diner tonight", source="pseudolife")
    res = svc.dream_run(_StubExtractor([
        {"entity": "user", "attribute": "restaurants tried",
         "value": "Rosa's Diner", "confidence": 0.8, "origin": "agent",
         "op": "add", "quote": "a totally different span", "source": 0},
    ]))
    assert res["span_flagged"] == 1
    assert res.get("span_parked", 0) == 0
    slot = svc.cortex_lookup("user", "restaurants tried")
    assert slot["kind"] == "set"
    assert any(m["value"] == "Rosa's Diner" for m in slot["members"])
