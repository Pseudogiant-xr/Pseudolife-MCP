"""Separate-pass event extraction (design doc
2026-08-04-separate-pass-events-design.md).

The claims call runs the shipped v5 prompt byte-identically; events come
from their OWN extractor call (`extract_events`, events-only prompt) so
interference with claims is zero by construction — the property the
overnight gate's claims-inertness tripwire measures as an exact zero.
Event writes reuse the already-tested chronicle path (literal gate, date
guard, dedup, journal, rollback). An events-pass failure is non-fatal:
claims commit normally, `events_pass_failed: true` is reported, and the
lost batch is not retried.

PG-backed service tests skip without the bench server.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


def test_events_prompt_artifact_matches_shipped_constant():
    """Same pin pattern as the v5 claims prompt: the measured artifact
    and the constant the daemon runs must be byte-identical."""
    from pseudolife_memory.memory.dream import _EVENTS_SYSTEM_PROMPT
    path = (Path(__file__).resolve().parents[1] / "evals" / "prompts"
            / "events_pass_v1.txt")
    assert path.read_text(encoding="utf-8") == _EVENTS_SYSTEM_PROMPT


def test_events_v2_candidate_is_v1_plus_the_quantity_rule():
    """The UNSHIPPED v2 candidate (2026-08-06 quantity+coverage design,
    gate run held) must stay v1 + one appended rule, so the measured v1
    body cannot drift inside it unnoticed. The shipped constant remains
    v1 until v2 passes its preregistered gate."""
    prompts = Path(__file__).resolve().parents[1] / "evals" / "prompts"
    v1 = (prompts / "events_pass_v1.txt").read_text(encoding="utf-8")
    v2 = (prompts / "events_pass_v2.txt").read_text(encoding="utf-8")
    assert v2.startswith(v1.rstrip("\n"))
    assert "KEEP QUANTITIES AND BE EXHAUSTIVE" in v2
    assert "earning $225" in v2          # quantity kept verbatim in example
    from pseudolife_memory.memory.dream import _EVENTS_SYSTEM_PROMPT
    assert _EVENTS_SYSTEM_PROMPT == v1   # v2 NOT shipped by this change


def test_extract_events_prompt_is_overridable(monkeypatch):
    """A/B harness plumbing (2026-08-06 quantity+coverage design): the
    UNSHIPPED events_pass_v2 candidate must be runnable through the bench
    without touching the shipped constant — mirroring the claims-prompt
    ``system_prompt`` override. Default stays byte-identical to v1."""
    import json

    from pseudolife_memory.memory import dream as dream_mod
    from pseudolife_memory.memory.dream import (
        _EVENTS_SYSTEM_PROMPT, OpenAICompatExtractor,
    )

    sent = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {
                "content": '{"events": []}'}}]}).encode()

    def _fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        return _Resp()

    monkeypatch.setattr(
        "urllib.request.urlopen", _fake_urlopen)
    ex = OpenAICompatExtractor("http://x/v1", "m",
                               events_prompt="CUSTOM EVENTS PROMPT")
    ex.extract_events(["note one"])
    assert sent[-1]["messages"][0]["content"] == "CUSTOM EVENTS PROMPT"

    default = OpenAICompatExtractor("http://x/v1", "m")
    default.extract_events(["note one"])
    assert sent[-1]["messages"][0]["content"] == _EVENTS_SYSTEM_PROMPT
    assert dream_mod._EVENTS_SYSTEM_PROMPT == _EVENTS_SYSTEM_PROMPT


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    yield s
    s.flush()


class _TwoPass:
    """Stub with a separate events pass, mirroring OpenAICompatExtractor's
    surface."""

    def __init__(self, claims, events, fail_events=False):
        self._claims = claims
        self._events = events
        self.fail_events = fail_events
        self.events_calls = 0

    def extract(self, texts, vocab, known_facts=None):
        return [dict(c) for c in self._claims]

    def extract_events(self, texts):
        self.events_calls += 1
        if self.fail_events:
            from pseudolife_memory.memory.dream import ExtractorError
            raise ExtractorError("events endpoint down")
        return [dict(e) for e in self._events]


_CLAIM = {"entity": "user", "attribute": "pet", "value": "kitten",
          "confidence": 0.8, "origin": "agent", "source": 0}
_EVENT = {"kind": "event", "description": "adopted a kitten",
          "actor": "user", "date": "2023-05-13",
          "date_phrase": "yesterday", "source": 0}


def _chronicle_rows(svc):
    return svc._storage.conn.execute(
        "SELECT description, to_char(occurred_at, 'YYYY-MM-DD') "
        "FROM chronicle_events ORDER BY id").fetchall()


def test_separate_pass_writes_and_journals_events(svc):
    svc.config.memory.dream.chronicle = True
    svc.store("[2023/05/14 (Sun) 10:02] user: adopted the kitten "
              "yesterday", source="notes")
    ex = _TwoPass([_CLAIM], [_EVENT])
    out = svc.dream_run(ex)
    assert ex.events_calls == 1
    assert out["inserted"] == 1                 # the claim landed
    assert out["events_inserted"] == 1
    assert out["events_pass_failed"] is False
    assert _chronicle_rows(svc) == [("adopted a kitten", "2023-05-13")]
    runs = svc._storage.recent_dream_runs(limit=5)
    assert runs[0]["status"] == "committed"
    assert runs[0]["tallies"]["events_pass_failed"] is False
    journal = svc._storage.dream_run_journal(runs[0]["id"])
    kinds = [r["kind"] for r in journal]
    assert kinds.count("scalar") == 1 and kinds.count("event") == 1
    ev = next(r for r in journal if r["kind"] == "event")
    assert ev["chronicle_event_id"] is not None
    assert ev["src_entry_id"] is not None       # source idx resolved


def test_chronicle_off_never_calls_the_events_pass(svc):
    svc.config.memory.dream.chronicle = False
    svc.store("user: adopted the kitten", source="notes")
    ex = _TwoPass([_CLAIM], [_EVENT])
    out = svc.dream_run(ex)
    assert ex.events_calls == 0
    assert out["events_inserted"] == 0
    assert _chronicle_rows(svc) == []


def test_events_pass_failure_is_non_fatal(svc):
    """A broken events endpoint must never stall consolidation: claims
    commit, the cursor advances, the failure is reported."""
    svc.config.memory.dream.chronicle = True
    svc.store("[2023/05/14] user: adopted the kitten yesterday",
              source="notes")
    ex = _TwoPass([_CLAIM], [_EVENT], fail_events=True)
    out = svc.dream_run(ex)
    assert out["inserted"] == 1
    assert out["events_pass_failed"] is True
    assert out["events_inserted"] == 0
    assert _chronicle_rows(svc) == []
    runs = svc._storage.recent_dream_runs(limit=5)
    assert runs[0]["status"] == "committed"     # claims committed
    # Persisted, not just returned: the 2026-08-12 soak review could not
    # tally pass-failures because the run row never recorded them (and
    # container recreation had discarded the warning logs).
    assert runs[0]["tallies"]["events_pass_failed"] is True
    assert out["cursor"] > 0                    # cursor advanced


def test_events_pass_events_are_gated_and_deduped_like_inline(svc):
    """The separate pass reuses the same write path: literal gate drops a
    fabricated number; an exact restatement dedupes without a second
    journal row."""
    svc.config.memory.dream.chronicle = True
    assert svc.config.memory.dream.literal_gate == "enforce"
    svc.store("[2023/05/14] user: bought a laptop and adopted the kitten "
              "yesterday", source="notes")
    fabricated = {**_EVENT, "description": "bought a laptop for $4500"}
    ex = _TwoPass([], [fabricated, _EVENT, dict(_EVENT)])
    out = svc.dream_run(ex)
    assert out["literal_dropped"] == 1
    assert out["events_inserted"] == 1
    assert out["events_duplicate"] == 1
    assert _chronicle_rows(svc) == [("adopted a kitten", "2023-05-13")]


def test_extractor_without_events_pass_still_works(svc):
    """Extractors lacking extract_events (stubs, older integrations) are
    valid with chronicle on — they simply contribute no events."""
    svc.config.memory.dream.chronicle = True

    class _ClaimsOnly:
        def extract(self, texts, vocab, known_facts=None):
            return [dict(_CLAIM)]

    svc.store("user: adopted the kitten", source="notes")
    out = svc.dream_run(_ClaimsOnly())
    assert out["inserted"] == 1
    assert out["events_inserted"] == 0
    assert out["events_pass_failed"] is False
