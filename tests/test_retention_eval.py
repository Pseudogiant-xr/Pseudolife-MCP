"""retention_interval_eval: pure unit tests — no GPU, no Postgres, no files
beyond tmp_path.

Pins the load-bearing harness mechanics from the 2026-08-08 preregistration
(docs/superpowers/specs/2026-08-08-retention-interval-eval-design.md):

* ``frozen_now`` must govern the DEFAULT-clock read path (records called with
  ``now=None``), not just explicit ``now=`` calls — the whole Study A claim
  rests on the injection covering serving-time reads.
* ``strip_flags`` must remove exactly ``effective_confidence`` and ``stale``
  at every depth and nothing else — the only difference between Study B's
  arms is those two keys.
* The deterministic stale-answer classifier and the canonical context hash
  are pinned so a rerun scores byte-identically.
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import retention_interval_eval as rie  # noqa: E402

from pseudolife_memory.memory import freshness  # noqa: E402

DAY = 86400.0


# ── clock injection ──────────────────────────────────────────────────────

def test_frozen_now_governs_explicit_freshness_reads():
    t0 = 1_700_000_000.0
    with rie.frozen_now(t0):
        assert freshness.effective_confidence(0.9, t0, "volatile") == 0.9
        assert freshness.is_stale("volatile", t0) is False
    with rie.frozen_now(t0 + 90 * DAY):
        # volatile: floor 0.4 reached at 21d, stale past 2x21d.
        assert freshness.effective_confidence(
            0.9, t0, "volatile") == pytest.approx(0.36)
        assert freshness.is_stale("volatile", t0) is True
        # slow: TTL 270d — decayed but neither floored nor stale at 90d.
        eff = freshness.effective_confidence(0.9, t0, "slow")
        assert 0.9 * 0.5 < eff < 0.9
        assert freshness.is_stale("slow", t0) is False


def test_frozen_now_covers_the_default_clock_path():
    """Records are read with now=None in serving — the shim must catch that."""
    from pseudolife_memory.memory.world_cortex import WorldRecord

    t0 = 1_700_000_000.0
    rec = WorldRecord(
        entity="llama-server", attribute="active version", value="v1",
        confidence=0.9, retrieved_at=t0, asserted_at=t0,
        freshness_class="volatile")
    with rie.frozen_now(t0 + 90 * DAY):
        assert rec.is_stale() is True            # no now= passed
        assert rec.effective_confidence() == pytest.approx(0.36)
    with rie.frozen_now(t0):
        assert rec.is_stale() is False
        assert rec.effective_confidence() == 0.9


def test_frozen_now_restores_the_real_clock():
    before = freshness._time
    with rie.frozen_now(123.0):
        assert freshness._time is not before
    assert freshness._time is before
    # And the restored clock is live, not frozen.
    assert abs(freshness._time.time() - time.time()) < 5.0


def test_frozen_now_restores_on_exception():
    before = freshness._time
    try:
        with rie.frozen_now(123.0):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert freshness._time is before


# ── flag stripping (Study B arm difference) ──────────────────────────────

def test_strip_flags_removes_exactly_the_two_keys_at_every_depth():
    payload = {
        "entity": "e", "value": "v", "effective_confidence": 0.36,
        "stale": True, "age": "90d",
        "nested": [{"stale": False, "effective_confidence": 1.0,
                    "confidence": 0.9, "provenance": ["x"]}],
    }
    out = rie.strip_flags(payload)
    assert "effective_confidence" not in out and "stale" not in out
    assert "effective_confidence" not in out["nested"][0]
    assert "stale" not in out["nested"][0]
    # Everything else survives — including confidence and age.
    assert out["age"] == "90d"
    assert out["nested"][0]["confidence"] == 0.9
    assert out["nested"][0]["provenance"] == ["x"]
    # Input is not mutated.
    assert payload["stale"] is True
    assert payload["nested"][0]["effective_confidence"] == 1.0


# ── canonical context hash (Study A identity claim) ──────────────────────

def test_context_hash_is_deterministic_and_content_sensitive():
    a = {"q": "port?", "entries": [{"value": "9090"}]}
    b = {"entries": [{"value": "9090"}], "q": "port?"}   # key order differs
    c = {"q": "port?", "entries": [{"value": "8080"}]}
    assert rie.context_hash(a) == rie.context_hash(b)
    assert rie.context_hash(a) != rie.context_hash(c)
    assert len(rie.context_hash(a)) == 64


# ── deterministic stale-answer classifier ────────────────────────────────

def test_unqualified_stale_value_is_flagged():
    assert rie.unqualified_stale("The default port is 8080.", "8080") is True


def test_hedged_answers_are_not_unqualified():
    assert rie.unqualified_stale(
        "It was 8080 as of March, but that may be outdated.", "8080") is False
    assert rie.unqualified_stale(
        "The stored value 8080 is flagged stale — re-verify before use.",
        "8080") is False


def test_absent_value_is_not_flagged():
    assert rie.unqualified_stale("I don't have that information.", "8080") is False


# ── artifact writing (every bench writes a file) ─────────────────────────

def test_result_artifact_written_by_default(tmp_path):
    out = tmp_path / "retention-interval-test.json"
    rie.write_artifact(out, {"tag": "test", "study_a": {"offsets": {}}})
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["tag"] == "test"
    assert data["preregistration"].endswith(
        "2026-08-08-retention-interval-eval-design.md")


# ── Study H3 pure helpers (staleness-policy arms, PR #121 prereg) ────────

def test_value_recovered_accepts_hedged_statements():
    """Gate 3 asks for the LAST RECORDED value — hedging is correct there,
    so recovery must not require an unqualified serve."""
    assert rie.value_recovered(
        "The last recorded value was 3.8.1, though it may be outdated.",
        "3.8.1") is True
    assert rie.value_recovered("It was 500.", "500") is True


def test_value_recovered_rejects_absent_value():
    assert rie.value_recovered(
        "The value is quarantined and I cannot recover it.", "3.8.1") is False


def test_fresh_payloads_identical_ignores_order_catches_rewrites():
    fresh_a = {"entity": "dns-zone", "value": "gandi", "stale": False}
    fresh_b = {"entity": "vault", "value": "aes", "stale": False}
    stale = {"entity": "proxy", "value": "5c31f2", "stale": True}
    base = [{"context": [fresh_a, stale, fresh_b]}]
    reordered = [{"context": [fresh_b, {**stale, "value": "(wrapped)",
                                        "last_known_value": "5c31f2"},
                              fresh_a]}]
    assert rie.fresh_payloads_identical(base, reordered) is True
    rewritten = [{"context": [fresh_a, stale,
                              {**fresh_b, "value": "TOUCHED"}]}]
    assert rie.fresh_payloads_identical(base, rewritten) is False


def test_recovery_rows_target_stale_facts_with_last_recorded_question():
    rows = [
        {"entity": "billing-api", "attribute": "deployed version",
         "seeded_value": "3.8.1", "freshness_class": "volatile",
         "question": "orig?", "context": []},
        {"entity": "dns-zone", "attribute": "registrar",
         "seeded_value": "gandi", "freshness_class": "slow",
         "question": "orig?", "context": []},
    ]
    got = rie._recovery_rows(rows)
    assert [r["entity"] for r in got] == ["billing-api"]
    assert got[0]["question"] == (
        "What was the last recorded billing-api deployed version?")


def test_answered_other_fact_flags_substituted_answers_only():
    ctx = [{"value": "(stale — re-verify; last known value below)",
            "last_known_value": "5c31f2", "stale": True},
           {"value": "netlify", "stale": False}]
    # Seeded value absent, another entry's value served: the confound.
    assert rie.answered_other_fact(
        "The hosting vendor is netlify.", "5c31f2", ctx) is True
    # Seeded value present: not a substitution, whatever else is said.
    assert rie.answered_other_fact(
        "Serial 5c31f2, hosted on netlify.", "5c31f2", ctx) is False
    # Neither present (a hedge/abstention): not a substitution.
    assert rie.answered_other_fact(
        "The memory looks unreliable here.", "5c31f2", ctx) is False
    # The quarantine wrapper itself never counts as "another value".
    assert rie.answered_other_fact(
        "The value is stale — re-verify.", "5c31f2",
        [ctx[0]]) is False


def test_fresh_payloads_identical_never_passes_vacuously():
    all_stale = [{"context": [{"value": "x", "stale": True}]}]
    assert rie.fresh_payloads_identical(all_stale, all_stale) is False
