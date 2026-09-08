"""Rule mode for lesson synthesis (2026-09-08, "Learning on the Job" delta).

The shipped synthesis prompt clusters outcome signals into abstract lessons
keyed per ``(task-type, aspect)`` and paraphrases freely. A benchmark that
distils ONE situation-specific rule per episode, with decision-critical
values kept verbatim (Tablan et al., arXiv 2607.22157), needs the opposite:
one rule per signal, keyed per situation, values copied unchanged, no
clustering, no trivia skip. Rule mode is per-signal (``about`` starting
with ``rule:``) or global (``memory.lessons.rule_mode``), default off, and
leaves the shipped path byte-identical.

PG-backed service tests skip cleanly without a server (tests/pg_fixtures).
"""

from __future__ import annotations

import io
import json
import tempfile

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


# ── helpers ──────────────────────────────────────────────────────────────────

def _claim(task, lesson, *, aspect="rule", about="apply_for_card",
           polarity="+", outcome="success", confidence=0.7):
    return {"task": task, "aspect": aspect, "lesson": lesson, "about": about,
            "polarity": polarity, "outcome": outcome, "confidence": confidence}


class RuleStub:
    """Extractor with both paths; records which one saw which signals."""

    def __init__(self, rules=None, lessons=None):
        self.rules = rules or {}
        self.lessons = lessons or []
        self.seen_rules: list[list[dict]] = []
        self.seen_lessons: list[list[dict]] = []

    def extract(self, texts, vocab):
        return []

    def extract_lessons(self, signals):
        self.seen_lessons.append(list(signals))
        return list(self.lessons)

    def extract_rules(self, signals):
        self.seen_rules.append(list(signals))
        out = []
        for s in signals:
            c = self.rules.get(s["id"]) or self.rules.get(s.get("about"))
            if c is not None:
                out.append(c)
        return out


class PlainStub:
    """No extract_rules — models an extractor that predates rule mode."""

    def __init__(self):
        self.seen_lessons: list[list[dict]] = []

    def extract(self, texts, vocab):
        return []

    def extract_lessons(self, signals):
        self.seen_lessons.append(list(signals))
        return [_claim(s["task"], f"lesson for {s['task']}", aspect="approach")
                for s in signals]


@pytest.fixture()
def svc(pg_conn, pg_url):
    from pseudolife_memory.service import MemoryService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


# ── detection ────────────────────────────────────────────────────────────────

def test_rule_signal_detection_is_per_signal_or_global():
    from pseudolife_memory.memory.dream import is_rule_signal

    assert is_rule_signal({"about": "rule: apply_for_card"}, False)
    assert is_rule_signal({"about": "RULE:x"}, False)
    assert not is_rule_signal({"about": "tar --no-same-owner"}, False)
    assert not is_rule_signal({"about": None}, False)
    assert is_rule_signal({"about": None}, True)


def test_lessons_config_rule_mode_defaults_off():
    from pseudolife_memory.utils.config import LessonsConfig

    assert LessonsConfig().rule_mode is False


# ── service routing ──────────────────────────────────────────────────────────

def test_rule_signals_route_to_extract_rules_and_bypass_the_dedup_gate(svc):
    """Two rules for look-alike situations that differ in one decision-
    critical value must BOTH survive: the cross-key cosine dedup gate (0.88)
    would fold the second onto the first, which is exactly the loss the
    paper's per-situation store avoids. A plain signal in the same batch
    still goes through extract_lessons."""
    a = svc.record_outcome("customer asks for the gold rewards card", "success",
                           about="rule: apply_for_card",
                           detail="SITUATION: gold rewards card, income stated\n"
                                  "VERDICT: success")
    b = svc.record_outcome("customer asks for the gold rewards card", "success",
                           about="rule: apply_for_card",
                           detail="SITUATION: gold rewards card, income withheld\n"
                                  "VERDICT: success")
    svc.record_outcome("deploy engine to host", "success", about="tar")
    pending = svc._storage.pending_signals()
    assert len(pending) == 3
    ids = {p["task"]: p["id"] for p in pending}
    assert a["signal_id"] and b["signal_id"]
    rules = {
        a["signal_id"]: _claim(
            "gold rewards card request, income stated",
            "WHEN a customer asks for the gold rewards card and states their "
            "income THEN call apply_for_card with product=gold rewards card"),
        b["signal_id"]: _claim(
            "gold rewards card request, income withheld",
            "WHEN a customer asks for the gold rewards card and withholds their "
            "income THEN call apply_for_card with product=gold rewards card "
            "after collecting income"),
    }
    ext = RuleStub(rules=rules, lessons=[_claim(
        "deploy engine to host", "use tar --no-same-owner", aspect="approach",
        about="tar")])

    rep = svc.synthesize_lessons(ext)

    assert rep["signals"] == 3
    assert rep["lessons"] == 3, rep
    assert rep.get("deduped", 0) == 0
    # Routing: the two rule signals went to extract_rules, the plain one to
    # extract_lessons, and nothing crossed over.
    assert [len(x) for x in ext.seen_rules] == [2]
    assert [len(x) for x in ext.seen_lessons] == [1]
    assert ext.seen_lessons[0][0]["id"] == ids["deploy engine to host"]
    dump = svc.lessons_dump()
    tasks = sorted((r["task"], r["aspect"]) for r in dump["entries"])
    assert ("gold rewards card request, income stated", "rule") in tasks
    assert ("gold rewards card request, income withheld", "rule") in tasks
    assert ("deploy engine to host", "approach") in tasks
    assert svc._storage.pending_signals() == []


def test_global_rule_mode_makes_every_signal_a_rule(svc):
    svc.config.memory.lessons.rule_mode = True
    svc.record_outcome("deploy engine to host", "success", about="tar")
    ext = RuleStub(rules={"tar": _claim("deploy to host, tar", "WHEN … THEN …",
                                        about="tar")})
    rep = svc.synthesize_lessons(ext)
    assert rep["lessons"] == 1
    assert [len(x) for x in ext.seen_rules] == [1]
    assert ext.seen_lessons == []


def test_a_rule_signal_whose_call_failed_stays_pending(svc):
    """The batch consume is all-or-nothing today; with per-signal failure
    tolerance the signals whose call failed must be left OUT of the
    consume set, or their rule is lost for good."""
    a = svc.record_outcome("situation A", "success", about="rule: x",
                           detail="VERDICT: success")
    b = svc.record_outcome("situation B", "success", about="rule: y",
                           detail="VERDICT: success")

    class PartialStub(RuleStub):
        def extract_rules(self, signals):
            self.last_rule_failed_ids = [b["signal_id"]]
            self.last_rule_failures = 1
            return [_claim("situation A", "WHEN A THEN x", about="x")]

    rep = svc.synthesize_lessons(PartialStub())
    assert rep["lessons"] == 1
    assert rep["rules_failed"] == 1
    pending = [p["id"] for p in svc._storage.pending_signals()]
    assert pending == [b["signal_id"]]
    assert a["signal_id"] not in pending


def test_rule_signals_fall_back_to_extract_lessons_when_unsupported(svc):
    """An extractor without extract_rules (the sidecar floor, older code)
    must not strand rule signals pending forever: they ride the shipped
    path and the report says so."""
    svc.record_outcome("customer asks for the gold rewards card", "success",
                       about="rule: apply_for_card", detail="VERDICT: success")
    ext = PlainStub()
    rep = svc.synthesize_lessons(ext)
    assert rep["lessons"] == 1
    assert rep["rules_fallback"] == 1
    assert [len(x) for x in ext.seen_lessons] == [1]
    assert svc._storage.pending_signals() == []


# ── OpenAICompatExtractor.extract_rules ──────────────────────────────────────

class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_recorder(monkeypatch, replies):
    """Serve canned chat completions; record every request body."""
    import urllib.request

    calls: list[dict] = []
    it = iter(replies)

    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data.decode()))
        content = next(it)
        body = {"choices": [{"message": {"content": json.dumps(content)}}]}
        return _FakeResp(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _sig(id_, about="rule: apply_for_card", detail="VERDICT: success",
         outcome="success"):
    return {"id": id_, "task": "customer asks for the gold rewards card",
            "outcome": outcome, "about": about, "detail": detail,
            "polarity": None, "origin": "action", "created_at": 1.0}


def test_extract_rules_is_one_call_per_signal_with_the_rule_prompt(monkeypatch):
    from pseudolife_memory.memory import dream

    ext = dream.OpenAICompatExtractor("http://x/v1", "m")
    calls = _urlopen_recorder(monkeypatch, [
        {"lessons": [{"task": "gold card, income stated", "aspect": "approach",
                      "lesson": "WHEN … THEN …", "about": "apply_for_card",
                      "polarity": "+", "outcome": "success", "confidence": 0.9}]},
        {"lessons": [{"task": "gold card, income withheld", "aspect": "pitfall",
                      "lesson": "WHEN … do NOT …", "about": "rule: apply_for_card",
                      "polarity": "-", "outcome": "failure", "confidence": 0.8}]},
    ])
    out = ext.extract_rules([_sig(1), _sig(2, detail="VERDICT: failure",
                                             outcome="failure")])

    assert len(calls) == 2
    for c in calls:
        assert c["messages"][0]["content"] == dream._RULE_LESSON_SYSTEM_PROMPT
        assert c["messages"][0]["content"] != dream._LESSON_SYSTEM_PROMPT
    assert [c["messages"][1]["content"].count("[success]") for c in calls] == [1, 0]
    # aspect is forced to "rule" whatever the model said; the routing prefix
    # never leaks into the stored ``about``.
    assert [o["aspect"] for o in out] == ["rule", "rule"]
    assert [o["about"] for o in out] == ["apply_for_card", "apply_for_card"]
    assert [o["polarity"] for o in out] == ["+", "-"]


def test_extract_rules_retries_once_when_a_must_include_value_is_missing(
        monkeypatch):
    """The paper bounces a rule that drops a decision-critical value (up to
    twice); one retry here, then the rule is accepted as-is rather than
    lost — a slightly lossy rule beats a stranded signal."""
    from pseudolife_memory.memory import dream

    ext = dream.OpenAICompatExtractor("http://x/v1", "m")
    detail = ("VERDICT: success\nMUST INCLUDE: 4.5%; Gold Rewards Card")
    calls = _urlopen_recorder(monkeypatch, [
        {"lessons": [{"task": "t", "lesson": "WHEN … THEN offer the card",
                      "about": "apply_for_card", "polarity": "+",
                      "outcome": "success", "confidence": 0.9}]},
        {"lessons": [{"task": "t", "lesson": "WHEN … THEN offer the Gold Rewards "
                      "Card at 4.5%", "about": "apply_for_card",
                      "polarity": "+", "outcome": "success", "confidence": 0.9}]},
    ])
    out = ext.extract_rules([_sig(1, detail=detail)])
    assert len(calls) == 2
    assert "4.5%" in calls[1]["messages"][1]["content"]  # the nudge names it
    assert out[0]["lesson"].endswith("4.5%")

    # Both replies missing a value: two calls, the second is accepted.
    calls = _urlopen_recorder(monkeypatch, [
        {"lessons": [{"task": "t", "lesson": "WHEN … THEN offer the card",
                      "about": "a", "polarity": "+", "outcome": "success"}]},
        {"lessons": [{"task": "t", "lesson": "WHEN … THEN offer a card",
                      "about": "a", "polarity": "+", "outcome": "success"}]},
    ])
    out = ext.extract_rules([_sig(1, detail=detail)])
    assert len(calls) == 2 and out[0]["lesson"] == "WHEN … THEN offer a card"


def test_extract_rules_failure_raises_like_extract_lessons(monkeypatch):
    import urllib.request

    from pseudolife_memory.memory import dream

    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ext = dream.OpenAICompatExtractor("http://x/v1", "m")
    with pytest.raises(dream.ExtractorError):
        ext.extract_rules([_sig(1)])


def test_extract_rules_keeps_the_rules_that_succeeded_before_a_failure(
        monkeypatch):
    """One call per signal means a trial-boundary batch is ~97 calls; a
    transient failure on the 90th must not discard the 89 rules already
    extracted (the batch would be retried whole next sweep). Partial
    success returns what landed; only a batch with NO success raises."""
    import urllib.request

    from pseudolife_memory.memory import dream

    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("connection reset")
        body = {"choices": [{"message": {"content": json.dumps({"lessons": [
            {"task": f"situation {calls['n']}", "lesson": "WHEN … THEN …",
             "about": "a", "polarity": "+", "outcome": "success"}]})}}]}
        return _FakeResp(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    ext = dream.OpenAICompatExtractor("http://x/v1", "m")
    out = ext.extract_rules([_sig(1), _sig(2), _sig(3)])
    assert [o["task"] for o in out] == ["situation 1", "situation 3"]
    assert ext.last_rule_failures == 1


# ── the shipped path is untouched ────────────────────────────────────────────

def test_shipped_lesson_prompt_and_call_shape_are_unchanged(monkeypatch):
    """The bench keeps its own tuned copy of the prompt; the two must match,
    and plain signals must still be ONE batched call under it."""
    import ast
    from pathlib import Path

    from pseudolife_memory.memory import dream

    bench = Path(__file__).resolve().parents[1] / "evals" / "lesson_synthesis_bench.py"
    tree = ast.parse(bench.read_text(encoding="utf-8"))
    copies = [n for n in tree.body if isinstance(n, ast.Assign)
              and any(getattr(t, "id", "") == "_LESSON_SYSTEM_PROMPT"
                      for t in n.targets)]
    assert copies, "bench copy of _LESSON_SYSTEM_PROMPT not found"
    assert ast.literal_eval(copies[0].value) == dream._LESSON_SYSTEM_PROMPT

    ext = dream.OpenAICompatExtractor("http://x/v1", "m")
    calls = _urlopen_recorder(monkeypatch, [{"lessons": []}])
    ext.extract_lessons([_sig(1, about="tar"), _sig(2, about="gh-ost")])
    assert len(calls) == 1
    assert calls[0]["messages"][0]["content"] == dream._LESSON_SYSTEM_PROMPT


def test_bench_rule_prompt_copy_matches_the_engine_and_scores_value_survival():
    """The bench keeps a tunable copy of the rule prompt too; and its rule
    fixtures must discriminate on the corrected value surviving verbatim."""
    import ast
    import importlib.util
    from pathlib import Path

    from pseudolife_memory.memory import dream

    bench = Path(__file__).resolve().parents[1] / "evals" / "lesson_synthesis_bench.py"
    tree = ast.parse(bench.read_text(encoding="utf-8"))
    copies = [n for n in tree.body if isinstance(n, ast.Assign)
              and any(getattr(t, "id", "") == "_RULE_LESSON_SYSTEM_PROMPT"
                      for t in n.targets)]
    assert copies, "bench copy of _RULE_LESSON_SYSTEM_PROMPT not found"
    assert ast.literal_eval(copies[0].value) == dream._RULE_LESSON_SYSTEM_PROMPT

    spec = importlib.util.spec_from_file_location("lsb", bench)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fx = next(f for f in mod.RULE_FIXTURES if f["id"] == "verbatim_rule_correction")
    good = [{"task": "renewal, loyalty discount, insists on annual",
             "aspect": "rule", "about": "apply_discount", "polarity": "+",
             "outcome": "correction",
             "lesson": "WHEN a caller renews, asks for the loyalty discount and "
                       "insists on paying annually THEN apply_discount(code=LOYAL15, "
                       "term=annual)"}]
    lossy = [dict(good[0], lesson="WHEN a caller renews and asks for the loyalty "
                                  "discount THEN apply the loyalty discount")]
    assert mod.score_scenario(fx, good)["full_pass"] is True
    assert mod.score_scenario(fx, lossy)["full_pass"] is False
