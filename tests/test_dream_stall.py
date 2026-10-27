"""A dream extractor that stops answering is reported, not silent.

On 2026-08-11 the primary extractor (a CLI shim whose ``claude -p`` calls
failed with an expired OAuth session; its /health answered 503 cli_error)
had been down for over a day and ten dream runs had served silently from
the fallback. On a machine with no fallback the dream holds its cursor and
simply stops. These pin the detection half of the stall signal: the error
classification (never carrying a response body), the tracker's thresholds
(two consecutive failures, or a due backlog with no successful dream for
three sweep intervals), the notice rate limit, and the service wiring
through ``dream_run_auto`` and ``dream_status``.
"""
from __future__ import annotations

import json
import socket
import urllib.error

import pytest

from pseudolife_memory.memory.dream import ExtractorError
from tests.dream_helpers import StubExtractor, stub_server
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

LOGIN_TEXT = "claude CLI failed: OAuth session expired and could not be refreshed"


# ── classification ────────────────────────────────────────────────────────

def _wrapped(cause: BaseException) -> ExtractorError:
    try:
        raise ExtractorError(f"extract failed: {cause}") from cause
    except ExtractorError as exc:
        return exc


def test_connection_refused_is_unreachable():
    from pseudolife_memory.memory.dream import classify_extractor_error
    exc = _wrapped(urllib.error.URLError(ConnectionRefusedError(111, "refused")))
    assert classify_extractor_error(exc) == ("extractor_unreachable", "connection refused")


def test_timeout_is_unreachable():
    from pseudolife_memory.memory.dream import classify_extractor_error
    reason, error = classify_extractor_error(_wrapped(socket.timeout("timed out")))
    assert (reason, error) == ("extractor_unreachable", "timed out")


def test_http_error_is_an_extractor_error_named_by_status_only():
    from pseudolife_memory.memory.dream import classify_extractor_error
    http = urllib.error.HTTPError("http://x/v1/chat/completions", 503,
                                  "Service Unavailable", {}, None)
    assert classify_extractor_error(_wrapped(http)) == ("extractor_error", "HTTP 503")


def test_http_401_is_login_expired():
    from pseudolife_memory.memory.dream import classify_extractor_error
    http = urllib.error.HTTPError("http://x/v1", 401, "Unauthorized", {}, None)
    assert classify_extractor_error(_wrapped(http))[0] == "login_expired"


def test_auth_words_classify_as_login_expired_without_echoing_them():
    from pseudolife_memory.memory.dream import classify_extractor_error
    reason, error = classify_extractor_error(RuntimeError(LOGIN_TEXT))
    assert reason == "login_expired"
    assert "OAuth" not in error and "refreshed" not in error


def test_unknown_failure_is_an_extractor_error_named_by_type():
    from pseudolife_memory.memory.dream import classify_extractor_error
    assert classify_extractor_error(_wrapped(KeyError("choices"))) == (
        "extractor_error", "KeyError")


def test_extractor_reads_the_error_body_for_auth_words_but_never_keeps_it():
    """The shim answers a failed CLI call with HTTP 500 and the CLI's error
    text in the body; the extractor peeks at it to classify the failure.
    Neither the classification nor the exception text carries the body."""
    from pseudolife_memory.memory.dream import (OpenAICompatExtractor,
                                                classify_extractor_error)
    with stub_server(lambda: (500, json.dumps({"error": LOGIN_TEXT}))) as base:
        ext = OpenAICompatExtractor(base + "/v1", "extractor", timeout_seconds=5)
        with pytest.raises(ExtractorError) as info:
            ext.extract(["a note"], [])
    reason, error = classify_extractor_error(info.value)
    assert (reason, error) == ("login_expired", "HTTP 500")
    assert "OAuth" not in str(info.value)


def test_a_plain_500_is_not_a_login_failure():
    from pseudolife_memory.memory.dream import (OpenAICompatExtractor,
                                                classify_extractor_error)
    with stub_server(lambda: (500, json.dumps({"error": "model crashed"}))) as base:
        ext = OpenAICompatExtractor(base + "/v1", "extractor", timeout_seconds=5)
        with pytest.raises(ExtractorError) as info:
            ext.extract(["a note"], [])
    assert classify_extractor_error(info.value) == ("extractor_error", "HTTP 500")


# ── the tracker ───────────────────────────────────────────────────────────

class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _tracker():
    from pseudolife_memory.service_dream import DreamStallTracker
    clock = _Clock()
    return DreamStallTracker(clock=clock), clock


def _failed(reason="login_expired", error="HTTP 500"):
    return {"pulled": 3, "claims": 0, "extractor_failed": True,
            "extractor_error": {"reason": reason, "error": error}}


OK = {"pulled": 3, "claims": 2}


def test_one_failure_is_noise_two_are_a_stall():
    t, clock = _tracker()
    t.record(_failed(), served_by_fallback=False)
    assert t.snapshot()["stall"] is None
    first = clock.t
    clock.t += 600
    t.record(_failed(), served_by_fallback=False)
    stall = t.snapshot()["stall"]
    assert stall == {"since": first, "reason": "login_expired",
                     "consecutive_failures": 2, "last_error": "HTTP 500",
                     "last_success_at": None}


def test_a_success_clears_the_stall_and_records_recovery():
    t, clock = _tracker()
    t.record(OK, served_by_fallback=False)
    ok_at = clock.t
    for _ in range(3):
        clock.t += 600
        t.record(_failed("extractor_unreachable", "connection refused"),
                 served_by_fallback=False)
    assert t.snapshot()["stall"]["last_success_at"] == ok_at
    assert t.snapshot()["stall"]["consecutive_failures"] == 3
    clock.t += 600
    t.record(OK, served_by_fallback=False)
    snap = t.snapshot()
    assert snap["stall"] is None
    assert snap["last_stall"]["recovered_at"] == clock.t
    assert snap["last_stall"]["reason"] == "extractor_unreachable"


def test_a_success_between_failures_resets_the_count():
    t, _ = _tracker()
    t.record(_failed(), served_by_fallback=False)
    t.record(OK, served_by_fallback=False)
    t.record(_failed(), served_by_fallback=False)
    assert t.snapshot()["stall"] is None


def test_skipped_empty_and_errored_runs_are_neutral():
    t, _ = _tracker()
    t.record(_failed(), served_by_fallback=False)
    for neutral in ({"skipped": "dream_in_progress", "pulled": 0, "claims": 0},
                    {"pulled": 0, "claims": 0},
                    {"error": "extractor_mode=fallback but no fallback", "pulled": 0}):
        t.record(neutral, served_by_fallback=False)
    assert t.snapshot()["stall"] is None
    t.record(_failed(), served_by_fallback=False)
    assert t.snapshot()["stall"]["consecutive_failures"] == 2


def test_fallback_serving_is_a_warning_that_only_the_primary_clears():
    t, _ = _tracker()
    t.record(OK, served_by_fallback=True)
    t.record(OK, served_by_fallback=True)
    stall = t.snapshot()["stall"]
    assert stall["reason"] == "served_by_fallback"
    t.record(OK, served_by_fallback=True)
    assert t.snapshot()["stall"]["consecutive_failures"] == 3
    t.record(OK, served_by_fallback=False)
    assert t.snapshot()["stall"] is None


def test_a_due_backlog_without_a_success_for_three_sweeps_is_a_stall():
    """Covers a primary that never answers at all: nothing reaches
    ``record`` while the sweep keeps finding the backlog due."""
    t, clock = _tracker()
    interval = 600.0
    t.check_overdue(backlog=8, min_batch=8, would_fire=True, interval=interval)
    due = clock.t
    clock.t += 3 * interval
    t.check_overdue(backlog=9, min_batch=8, would_fire=True, interval=interval)
    assert t.snapshot()["stall"] is None          # not past 3x yet
    clock.t += 1
    t.check_overdue(backlog=9, min_batch=8, would_fire=True, interval=interval)
    stall = t.snapshot()["stall"]
    assert stall["since"] == due and stall["reason"] == "extractor_unreachable"


def test_the_overdue_clock_restarts_when_the_backlog_is_not_due():
    t, clock = _tracker()
    t.check_overdue(backlog=8, min_batch=8, would_fire=True, interval=600)
    clock.t += 1000
    t.check_overdue(backlog=2, min_batch=8, would_fire=True, interval=600)
    clock.t += 1000
    t.check_overdue(backlog=8, min_batch=8, would_fire=True, interval=600)
    clock.t += 1000
    t.check_overdue(backlog=8, min_batch=8, would_fire=True, interval=600)
    assert t.snapshot()["stall"] is None
    t.check_overdue(backlog=8, min_batch=8, would_fire=False, interval=600)
    clock.t += 5000
    t.check_overdue(backlog=8, min_batch=8, would_fire=True, interval=600)
    assert t.snapshot()["stall"] is None


def test_notices_begin_once_repeat_after_the_window_and_clear_once():
    t, clock = _tracker()
    repeat = 6 * 3600
    assert t.take_notice(repeat) is None
    t.record(_failed(), served_by_fallback=False)
    t.record(_failed(), served_by_fallback=False)
    kind, record = t.take_notice(repeat)
    assert kind == "begin" and record["reason"] == "login_expired"
    t.mark_noticed(kind)
    clock.t += repeat - 1
    t.record(_failed(), served_by_fallback=False)
    assert t.take_notice(repeat) is None
    clock.t += 1
    kind, _ = t.take_notice(repeat)
    assert kind == "repeat"
    t.mark_noticed(kind)
    t.record(OK, served_by_fallback=False)
    kind, record = t.take_notice(repeat)
    assert kind == "clear" and record["recovered_at"] == clock.t
    t.mark_noticed(kind)
    assert t.take_notice(repeat) is None


def test_an_undelivered_begin_owes_no_recovery_notice():
    """With the board off nothing was said, so recovery says nothing."""
    t, _ = _tracker()
    t.record(_failed(), served_by_fallback=False)
    t.record(_failed(), served_by_fallback=False)
    assert t.take_notice(3600)[0] == "begin"      # taken, never marked
    t.record(OK, served_by_fallback=False)
    assert t.take_notice(3600) is None


# ── the one-line text ─────────────────────────────────────────────────────

@pytest.mark.parametrize("reason, needle", [
    ("login_expired", "`claude`, then `/login`"),
    ("extractor_unreachable", "extractor endpoint"),
    ("extractor_error", "extractor"),
    ("served_by_fallback", "fallback is serving"),
])
def test_the_stall_line_names_reason_and_remedy_in_one_short_line(reason, needle):
    from pseudolife_memory.memory.dream import dream_stall_line
    line = dream_stall_line({"since": 1_000_000.0, "reason": reason,
                             "consecutive_failures": 2, "last_error": "HTTP 500",
                             "last_success_at": None})
    assert "\n" not in line and len(line) <= 240
    assert reason in line and needle in line
    assert dream_stall_line(None) == ""


def test_login_remedy_names_both_cli_logins():
    from pseudolife_memory.memory.dream import stall_remedy
    text = stall_remedy("login_expired")
    assert "`claude`, then `/login`" in text and "codex login" in text


# ── service wiring (PG-backed) ────────────────────────────────────────────

class _FailsTwiceThenWorks:
    def __init__(self):
        self.calls = 0

    def extract(self, texts, vocab, known_facts=None):
        self.calls += 1
        if self.calls <= 2:
            raise ExtractorError(f"extract failed: {LOGIN_TEXT}")
        return [{"entity": "relay", "attribute": "port", "value": "4001"}]


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path, monkeypatch):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    s.config.memory.cortex.auto_promote = False
    s.config.memory.dream.literal_gate = "log"
    # No board in this test: the notice path must be a quiet no-op.
    s.config.coordination.enabled = False
    yield s
    s.flush()


def test_dream_run_auto_reports_a_stall_and_its_recovery(svc, monkeypatch):
    from pseudolife_memory.memory import dream as d

    ext = _FailsTwiceThenWorks()
    monkeypatch.setattr(d, "build_extractor_with_fallback",
                        lambda cfg: (ext, "primary"))
    svc.store("the relay port is 4001", source="notes")
    assert svc.dream_status()["stall"] is None
    first = svc.dream_run_auto()
    assert first.get("extractor_failed") is True
    assert first["extractor_error"] == {"reason": "login_expired",
                                        "error": "ExtractorError"}
    assert svc.dream_status()["stall"] is None     # one failure is noise
    svc.dream_run_auto()
    stall = svc.dream_status()["stall"]
    assert stall["reason"] == "login_expired" and stall["consecutive_failures"] == 2
    assert "OAuth" not in json.dumps(stall)
    ok = svc.dream_run_auto()
    assert not ok.get("extractor_failed") and ok["pulled"] >= 1
    status = svc.dream_status()
    assert status["stall"] is None
    assert status["last_stall"]["reason"] == "login_expired"
    assert status["last_stall"]["recovered_at"] >= stall["since"]


def test_a_stub_that_succeeds_never_stalls(svc, monkeypatch):
    from pseudolife_memory.memory import dream as d

    monkeypatch.setattr(d, "build_extractor_with_fallback", lambda cfg: (
        StubExtractor([{"entity": "relay", "attribute": "port", "value": "4001"}]),
        "primary"))
    svc.store("the relay port is 4001", source="notes")
    svc.dream_run_auto()
    assert svc.dream_status()["stall"] is None


# ── notices and the sweep (no PG) ─────────────────────────────────────────

class _NoticeService:
    """Only what the stall notifier touches."""

    def __init__(self, **dream_over):
        from types import MethodType, SimpleNamespace

        from pseudolife_memory.service import MemoryService
        from pseudolife_memory.service_dream import DreamStallTracker
        from pseudolife_memory.utils.config import DreamConfig
        self.dream_stall_notify = MethodType(MemoryService.dream_stall_notify, self)
        self.dream_stall_tick = MethodType(MemoryService.dream_stall_tick, self)
        self.config = SimpleNamespace(memory=SimpleNamespace(
            dream=DreamConfig(**dream_over)))
        self.clock = _Clock()
        self._dream_stall_tracker = DreamStallTracker(clock=self.clock)


def _stall(service):
    service._dream_stall_tracker.record(_failed(), served_by_fallback=False)
    service._dream_stall_tracker.record(_failed(), served_by_fallback=False)


def test_the_notifier_posts_one_begin_and_one_clear(monkeypatch):
    from pseudolife_memory import coordination
    sent = []
    monkeypatch.setattr(coordination, "daemon_notice",
                        lambda service, text: sent.append(text) or {"recipients": 1})
    svc = _NoticeService()
    _stall(svc)
    svc.dream_stall_notify()
    svc.dream_stall_notify()
    assert len(sent) == 1
    assert sent[0].startswith("Dream extraction stalled since ")
    assert "login_expired" in sent[0] and "/login" in sent[0]
    svc._dream_stall_tracker.record(OK, served_by_fallback=False)
    svc.dream_stall_notify()
    svc.dream_stall_notify()
    assert len(sent) == 2 and sent[1].startswith("Dream extraction recovered at ")


def test_the_notifier_repeats_only_after_stall_repeat_hours(monkeypatch):
    from pseudolife_memory import coordination
    sent = []
    monkeypatch.setattr(coordination, "daemon_notice",
                        lambda service, text: sent.append(text) or {"recipients": 1})
    svc = _NoticeService(stall_repeat_hours=1.0)
    _stall(svc)
    svc.dream_stall_notify()
    svc.clock.t += 3599
    svc.dream_stall_notify()
    assert len(sent) == 1
    svc.clock.t += 1
    svc.dream_stall_notify()
    assert len(sent) == 2


def test_stall_notice_off_sends_nothing(monkeypatch):
    from pseudolife_memory import coordination
    sent = []
    monkeypatch.setattr(coordination, "daemon_notice",
                        lambda service, text: sent.append(text) or {"recipients": 1})
    svc = _NoticeService(stall_notice=False)
    _stall(svc)
    svc.dream_stall_notify()
    assert sent == []


def test_an_undeliverable_notice_is_retried_at_the_next_tick(monkeypatch):
    """No board (disabled, no Postgres): ``daemon_notice`` returns None and
    the notice stays owed rather than being counted as said."""
    from pseudolife_memory import coordination
    answers = [None, {"recipients": 2}]
    sent = []

    def notice(service, text):
        sent.append(text)
        return answers.pop(0)

    monkeypatch.setattr(coordination, "daemon_notice", notice)
    svc = _NoticeService()
    _stall(svc)
    svc.dream_stall_notify()
    svc.dream_stall_notify()
    svc.dream_stall_notify()
    assert len(sent) == 2


def test_the_sweep_tick_checks_overdue_and_notifies(monkeypatch):
    from pseudolife_memory import coordination
    sent = []
    monkeypatch.setattr(coordination, "daemon_notice",
                        lambda service, text: sent.append(text) or {"recipients": 1})
    svc = _NoticeService(sweep_interval_seconds=600.0, min_batch=8)
    status = {"backlog": 9, "would_fire": True}
    svc.dream_stall_tick(status)
    svc.clock.t += 1801
    svc.dream_stall_tick(status)
    assert svc._dream_stall_tracker.snapshot()["stall"]["reason"] == "extractor_unreachable"
    assert len(sent) == 1


def test_run_sweep_once_runs_the_stall_tick():
    from pseudolife_memory.memory.dream import run_sweep_once
    from pseudolife_memory.utils.config import DreamConfig
    from types import SimpleNamespace

    seen = []

    class _Sweep:
        config = SimpleNamespace(memory=SimpleNamespace(dream=DreamConfig()))

        def compact_superseded(self):
            return {"total": 0}

        def prune_dream_runs(self):
            return 0

        def dream_status(self):
            return {"backlog": 0, "would_fire": False}

        def dream_stall_tick(self, status):
            seen.append(status)

    out = run_sweep_once(_Sweep())
    assert out["reason"] == "below_threshold"
    assert seen == [{"backlog": 0, "would_fire": False}]


# ── review of PR #456 (2026-09-29) ────────────────────────────────────────

@pytest.mark.parametrize("text", ["password authentication failed for user x",
                                  "the author field is missing",
                                  "catalog in an odd state"])
def test_auth_words_are_whole_words_not_substrings(text):
    from pseudolife_memory.memory.dream import classify_extractor_error
    assert classify_extractor_error(RuntimeError(text))[0] == "extractor_error"


@pytest.mark.parametrize("text", ["OAuth session expired", "Not logged in",
                                  "session expired, log in again", "401 Unauthorized",
                                  "please login"])
def test_login_failures_still_classify_as_login_expired(text):
    from pseudolife_memory.memory.dream import classify_extractor_error
    assert classify_extractor_error(RuntimeError(text))[0] == "login_expired"


def test_a_run_whose_every_memory_was_set_aside_is_a_failure_not_a_recovery():
    """After three failures of the same batch the per-memory retry sets a
    lone failing memory aside and returns ``pulled: 1`` without
    ``extractor_failed``; that is the extractor failing again."""
    t, _ = _tracker()
    t.record(_failed(), served_by_fallback=False)
    t.record(_failed(), served_by_fallback=False)
    t.record({"pulled": 1, "claims": 0, "quarantined": 1,
              "extractor_error": {"reason": "login_expired", "error": "HTTP 500"}},
             served_by_fallback=False)
    snap = t.snapshot()
    assert snap["last_stall"] is None
    assert snap["stall"]["consecutive_failures"] == 3
    assert snap["stall"]["reason"] == "login_expired"
    # A partial set-aside (siblings extracted) is still a success.
    t.record({"pulled": 3, "claims": 2, "quarantined": 1}, served_by_fallback=False)
    assert t.snapshot()["stall"] is None


def test_a_write_phase_hold_is_not_an_extractor_failure():
    t, _ = _tracker()
    for _ in range(3):
        t.record({"pulled": 2, "claims": 0, "extractor_failed": True,
                  "hold_phase": "write"}, served_by_fallback=False)
    assert t.snapshot()["stall"] is None


def _drain(t, repeat, sent):
    due = t.take_notice(repeat)
    if due is not None:
        sent.append(due[0])
        t.mark_noticed(due[0])


def test_a_flapping_extractor_begins_at_most_once_per_repeat_window():
    t, clock = _tracker()
    repeat = 6 * 3600
    sent = []
    for _ in range(20):                     # 20 cycles x 30 min = 10 h
        for result in (_failed(), _failed(), OK):
            clock.t += 600
            t.record(result, served_by_fallback=False)
            _drain(t, repeat, sent)
    assert sent.count("begin") == 2         # the first, and once past 6 h
    assert sent.count("clear") == 2         # only an announced incident clears
    assert "repeat" not in sent


def test_a_fallback_warning_escalates_at_once_when_the_fallback_fails_too():
    t, clock = _tracker()
    repeat = 6 * 3600
    sent = []
    t.record(OK, served_by_fallback=True)
    t.record(OK, served_by_fallback=True)
    _drain(t, repeat, sent)
    clock.t += 60
    t.record(_failed("extractor_unreachable", "connection refused"),
             served_by_fallback=True)
    _drain(t, repeat, sent)
    _drain(t, repeat, sent)
    assert sent == ["begin", "escalate"]


def test_escalation_crosses_incidents_but_the_same_reason_again_waits():
    t, clock = _tracker()
    repeat = 6 * 3600
    sent = []
    t.record(OK, served_by_fallback=True)
    t.record(OK, served_by_fallback=True)
    _drain(t, repeat, sent)                 # the warning is announced
    t.record(OK, served_by_fallback=False)
    _drain(t, repeat, sent)                 # and its recovery
    clock.t += 600
    t.record(OK, served_by_fallback=True)
    t.record(OK, served_by_fallback=True)
    _drain(t, repeat, sent)                 # the same warning again: waits
    t.record(OK, served_by_fallback=False)
    _drain(t, repeat, sent)                 # unannounced, so no recovery
    clock.t += 600
    t.record(_failed(), served_by_fallback=False)
    t.record(_failed(), served_by_fallback=False)
    _drain(t, repeat, sent)                 # a hard stall after a warning: at once
    assert sent == ["begin", "clear", "begin"]


def test_a_notice_nobody_received_stays_owed(monkeypatch):
    from pseudolife_memory import coordination
    answers = [{"recipients": 0}, {"recipients": 1}]
    sent = []

    def notice(service, text):
        sent.append(text)
        return answers.pop(0)

    monkeypatch.setattr(coordination, "daemon_notice", notice)
    svc = _NoticeService()
    _stall(svc)
    svc.dream_stall_notify()
    svc.dream_stall_notify()
    svc.dream_stall_notify()
    assert len(sent) == 2
    assert all(t.startswith("Dream extraction stalled") for t in sent)


@pytest.mark.parametrize("bad", [0, -1, 0.0, True, float("nan"), "6"])
def test_stall_repeat_hours_must_be_positive(bad):
    from pseudolife_memory.utils.config import DreamConfig
    with pytest.raises(ValueError, match="stall_repeat_hours"):
        DreamConfig(stall_repeat_hours=bad)


def _fallback_service(mode):
    from types import MethodType, SimpleNamespace

    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.utils.config import DreamConfig

    class _Svc:
        def __init__(self):
            self.config = SimpleNamespace(memory=SimpleNamespace(dream=DreamConfig(
                extractor_source="config", extractor_mode=mode,
                extractor_base_url="http://127.0.0.1:1/v1", extractor_model="p",
                fallback_base_url="http://127.0.0.1:2/v1", fallback_model="f")))
            self._last_dream_extractor = None
            self.dream_run_auto = MethodType(MemoryService.dream_run_auto, self)
            self.dream_stall_state = MethodType(MemoryService.dream_stall_state, self)

        def dream_run(self, extractor, *, limit=None):
            return {"pulled": 3, "claims": 2}

    return _Svc()


@pytest.mark.parametrize("mode, stalled", [("auto", True), ("fallback", False)])
def test_a_fallback_dream_warns_only_when_the_probe_chose_it(monkeypatch, mode, stalled):
    from pseudolife_memory.memory import dream as d
    monkeypatch.setattr(d, "build_extractor_with_fallback",
                        lambda cfg: (StubExtractor([]), "fallback"))
    svc = _fallback_service(mode)
    svc.dream_run_auto()
    svc.dream_run_auto()
    stall = svc.dream_stall_state()["stall"]
    if stalled:
        assert stall["reason"] == "served_by_fallback"
    else:
        assert stall is None


class _AlwaysFails:
    def extract(self, texts, vocab, known_facts=None):
        raise ExtractorError(f"extract failed: {LOGIN_TEXT}")


def test_a_one_memory_outage_never_announces_a_false_recovery(svc, monkeypatch):
    from pseudolife_memory import coordination
    from pseudolife_memory.memory import dream as d

    sent = []
    monkeypatch.setattr(coordination, "daemon_notice",
                        lambda service, text: sent.append(text) or {"recipients": 1})
    monkeypatch.setattr(d, "build_extractor_with_fallback",
                        lambda cfg: (_AlwaysFails(), "primary"))
    svc.store("the relay port is 4001", source="notes")
    for _ in range(3):
        svc.dream_run_auto()
    status = svc.dream_status()
    assert status["stall"] is not None and status["last_stall"] is None
    assert [t.split(" since")[0] for t in sent] == ["Dream extraction stalled"]


def test_database_write_failures_are_not_an_extractor_stall(svc, monkeypatch):
    """A claim write that fails holds the cursor like an extraction failure,
    but it is the database, not the extractor, and a psycopg password error
    must never read as an expired CLI login."""
    from pseudolife_memory.memory import dream as d

    monkeypatch.setattr(d, "build_extractor_with_fallback", lambda cfg: (
        StubExtractor([{"entity": "relay", "attribute": "port", "value": "4001"}]),
        "primary"))

    def refused(*a, **kw):
        raise RuntimeError("password authentication failed for user fixture")

    monkeypatch.setattr(svc, "cortex_write", refused)
    svc.store("the relay port is 4001", source="notes")
    results = [svc.dream_run_auto() for _ in range(2)]
    assert all(r.get("extractor_failed") and r["hold_phase"] == "write" for r in results)
    assert all("extractor_error" not in r for r in results)
    assert svc.dream_status()["stall"] is None


# ── the primary's own cause wins (2026-10-27) ─────────────────────────────
#
# Live: the primary CLI shim's sign-in expired (its /health answered 503),
# the auto-mode fallback named a sidecar this stack does not run, and every
# fallback dream failed on DNS. The stall said `extractor_unreachable` /
# `unreachable (gaierror)` and the session-start line said to check the
# network, while the fix was to sign the CLI in again.

LOGIN_PROBE = {"reason": "login_expired", "error": "/health HTTP 503 (login_required)",
               "fallback_resolves": True}


def test_a_failed_fallback_does_not_hide_the_primarys_cause():
    from pseudolife_memory.memory.dream import dream_stall_line
    t, clock = _tracker()
    dns = _failed("extractor_unreachable", "unreachable (gaierror)")
    t.record(dns, served_by_fallback=True, primary_probe=LOGIN_PROBE)
    clock.t += 600
    t.record(dns, served_by_fallback=True, primary_probe=LOGIN_PROBE)
    stall = t.snapshot()["stall"]
    assert stall["reason"] == "login_expired"
    assert stall["last_error"] == ("primary /health HTTP 503 (login_required); "
                                   "fallback unreachable (gaierror)")
    line = dream_stall_line(stall)
    assert "`claude`, then `/login`" in line and "reachable" not in line


def test_a_served_fallback_names_the_primarys_cause():
    t, clock = _tracker()
    t.record(OK, served_by_fallback=True, primary_probe=LOGIN_PROBE)
    clock.t += 600
    t.record(OK, served_by_fallback=True, primary_probe=LOGIN_PROBE)
    stall = t.snapshot()["stall"]
    assert stall["reason"] == "served_by_fallback"
    assert stall["last_error"] == "primary /health HTTP 503 (login_required)"


def test_a_skipped_fallback_is_named_in_the_error():
    # A fallback that does not resolve is never dialled: the dream runs on
    # the primary, whose own failure is the first-hand cause.
    t, clock = _tracker()
    probe = {**LOGIN_PROBE, "fallback_resolves": False}
    t.record(_failed(), served_by_fallback=False, primary_probe=probe)
    clock.t += 600
    t.record(_failed(), served_by_fallback=False, primary_probe=probe)
    stall = t.snapshot()["stall"]
    assert stall["reason"] == "login_expired"
    assert stall["last_error"] == "primary HTTP 500; fallback does not resolve"


def test_a_primary_that_serves_despite_its_probe_is_a_success():
    t, clock = _tracker()
    probe = {**LOGIN_PROBE, "fallback_resolves": False}
    t.record(_failed(), served_by_fallback=False, primary_probe=probe)
    clock.t += 600
    t.record(OK, served_by_fallback=False, primary_probe=probe)
    assert t.snapshot()["stall"] is None and t.failures == 0


def test_dream_run_auto_hands_the_probe_to_the_tracker(monkeypatch):
    from pseudolife_memory.memory import dream as d
    fallback = StubExtractor([])
    fallback.primary_probe = LOGIN_PROBE
    monkeypatch.setattr(d, "build_extractor_with_fallback",
                        lambda cfg: (fallback, "fallback"))
    svc = _fallback_service("auto")
    svc.dream_run = lambda extractor, *, limit=None: _failed(
        "extractor_unreachable", "unreachable (gaierror)")
    svc.dream_run_auto()
    svc.dream_run_auto()
    stall = svc.dream_stall_state()["stall"]
    assert stall["reason"] == "login_expired"
    assert stall["last_error"].startswith("primary /health HTTP 503")


# ── a signed-out provider, named where it lives (2026-10-27 maintainer) ──
#
# "We need a way to easily tell a user that their provider is signed out on
# the location where their bank and dream extractor live." The shim names
# the provider and its OS user; the stall record carries them; every
# surface says which CLI, on which machine, as whom, and the one command.

SIGNED_OUT = {"reason": "login_expired", "error": "/health HTTP 503 (login_required)",
              "provider": "claude", "shim_user": "svc", "fallback_resolves": True}


def test_a_signed_out_probe_carries_provider_and_user_into_the_stall():
    t, clock = _tracker()
    dns = _failed("extractor_unreachable", "unreachable (gaierror)")
    t.record(dns, served_by_fallback=True, primary_probe=SIGNED_OUT)
    clock.t += 600
    t.record(dns, served_by_fallback=True, primary_probe=SIGNED_OUT)
    stall = t.snapshot()["stall"]
    assert stall["provider"] == "claude" and stall["shim_user"] == "svc"


def test_a_probe_without_identity_adds_no_keys():
    # The stall-tracker goldens' records keep their five keys.
    t, clock = _tracker()
    t.record(_failed(), served_by_fallback=True, primary_probe=LOGIN_PROBE)
    clock.t += 600
    t.record(_failed(), served_by_fallback=True, primary_probe=LOGIN_PROBE)
    assert list(t.snapshot()["stall"]) == ["since", "reason", "consecutive_failures",
                                           "last_error", "last_success_at"]


def test_identity_is_carried_only_for_a_sign_in_failure():
    t, clock = _tracker()
    probe = {**SIGNED_OUT, "reason": "extractor_error",
             "error": "/health HTTP 503 (cli_error)"}
    t.record(_failed(), served_by_fallback=True, primary_probe=probe)
    clock.t += 600
    t.record(_failed(), served_by_fallback=True, primary_probe=probe)
    assert "provider" not in t.snapshot()["stall"]


def test_a_served_fallback_keeps_the_identity_too():
    t, clock = _tracker()
    t.record(OK, served_by_fallback=True, primary_probe=SIGNED_OUT)
    clock.t += 600
    t.record(OK, served_by_fallback=True, primary_probe=SIGNED_OUT)
    stall = t.snapshot()["stall"]
    assert stall["reason"] == "served_by_fallback" and stall["provider"] == "claude"


def _signed_out_stall(**over):
    return {"since": 1_792_891_234.0, "reason": "login_expired", "consecutive_failures": 2,
            "last_error": "x", "last_success_at": None, "provider": "claude",
            "shim_user": "svc", **over}


def test_the_line_names_provider_place_user_and_command():
    from pseudolife_memory.memory.dream import dream_stall_line
    line = dream_stall_line(_signed_out_stall())
    assert line.startswith("Pseudolife-MCP: dreams stalled since ")
    assert line.count("dreams stalled") == 1
    assert "the Claude Code CLI is signed out on the machine running the Pseudolife daemon" in line
    assert "as svc" in line and "`claude`, then `/login`" in line


def test_the_line_says_this_machine_when_told_so():
    from pseudolife_memory.memory.dream import dream_stall_line
    line = dream_stall_line(_signed_out_stall(), where="this machine")
    assert "signed out on this machine" in line


def test_the_codex_line_names_codex_login_and_no_user_when_unknown():
    from pseudolife_memory.memory.dream import dream_stall_line
    line = dream_stall_line(_signed_out_stall(provider="codex", shim_user=None))
    assert "the Codex CLI is signed out" in line and "`codex login`" in line
    assert " as " not in line.split("signed out", 1)[1].split(":", 1)[0]


def test_a_served_fallback_line_names_the_sign_in_too():
    from pseudolife_memory.memory.dream import dream_stall_line
    line = dream_stall_line(_signed_out_stall(reason="served_by_fallback"))
    assert line.startswith("Pseudolife-MCP: dream primary extractor down since ")
    assert "Claude Code CLI is signed out" in line and "fallback" in line


def test_the_board_notice_names_the_sign_in():
    from pseudolife_memory.memory.dream import dream_stall_notice_text
    text = dream_stall_notice_text("begin", _signed_out_stall())
    assert "Claude Code CLI is signed out on the machine running the Pseudolife daemon" in text
    assert "`claude`, then `/login`" in text


def test_records_without_a_provider_keep_todays_text():
    from pseudolife_memory.memory.dream import dream_stall_line, dream_stall_notice_text
    old = {k: v for k, v in _signed_out_stall().items() if k not in ("provider", "shim_user")}
    # Opus review of 1f782865: one sign-in command, provider or not.
    for text in (dream_stall_line(old), dream_stall_notice_text("begin", old)):
        assert "`claude`, then `/login`" in text and "`codex login`" in text
        assert "claude auth login" not in text


@pytest.mark.parametrize("host, where", [
    # Codex review of 1f782865: a loopback address may be an SSH tunnel to
    # a remote daemon, so it is no evidence the daemon runs here.
    ("127.0.0.1:8765", None),
    ("localhost:8765", None),
    ("[::1]:8765", None),
    ("Box.Example:8765", "the machine running the Pseudolife daemon (box.example)"),
    # A name that merely starts with "127." is not loopback.
    ("127.example.com:8765", "the machine running the Pseudolife daemon (127.example.com)"),
    ("[::ffff:127.0.0.1]:8765", None),
    ("evil host\nInjected", None),
    (None, None),
])
def test_the_hook_names_where_the_daemon_runs_from_the_host_header(host, where):
    from pseudolife_memory.web.session_hook import stall_where
    assert stall_where(host) == where


class _SignedOutFails:
    primary_probe = SIGNED_OUT

    def extract(self, texts, vocab, known_facts=None):
        raise ExtractorError("extract failed: <urlopen error [Errno -2] Name or service not known>")


def test_dream_status_carries_the_stall_notice(svc, monkeypatch):
    from pseudolife_memory.memory import dream as d

    monkeypatch.setattr(d, "build_extractor_with_fallback",
                        lambda cfg: (_SignedOutFails(), "fallback"))
    svc.store("the relay port is 4001", source="notes")
    assert svc.dream_status()["stall_line"] is None
    svc.dream_run_auto()
    svc.dream_run_auto()
    status = svc.dream_status()
    assert status["stall"]["provider"] == "claude"
    # ``stall_line``: not ``stall_notice``, the config switch's name.
    assert status["stall_line"].startswith("Pseudolife-MCP: dreams stalled since ")
    assert "Claude Code CLI is signed out" in status["stall_line"]
    keys = list(status)
    assert keys.index("stall_line") == keys.index("last_stall") + 1


def test_a_remote_shim_is_named_where_it_runs():
    # Codex review of 1f782865: the sign-in lives on the shim's machine,
    # which need not be the daemon's.
    from pseudolife_memory.memory.dream import dream_stall_line, dream_stall_notice_text
    stall = _signed_out_stall(shim_host="gpu-box.example")
    for text in (dream_stall_line(stall), dream_stall_line(stall, where="anything"),
                 dream_stall_notice_text("begin", stall)):
        assert "signed out on the machine running the CLI shim (gpu-box.example)" in text


def test_the_tracker_carries_the_shim_host_with_the_identity():
    t, clock = _tracker()
    probe = {**SIGNED_OUT, "shim_host": "gpu-box.example"}
    t.record(_failed(), served_by_fallback=True, primary_probe=probe)
    clock.t += 600
    t.record(_failed(), served_by_fallback=True, primary_probe=probe)
    assert t.snapshot()["stall"]["shim_host"] == "gpu-box.example"


def test_a_primary_only_setup_learns_the_identity_after_a_sign_in_failure(monkeypatch):
    # Opus review of 1f782865: the installer's claude-only and openai-only
    # modes (extractor_mode=primary) never probe, so they never learned who
    # to sign in. A dream that fails with login_expired asks /health once.
    from pseudolife_memory.memory import dream as d
    cause = d.ProbeCause("login_expired", "/health HTTP 503 (login_required)",
                         provider="claude", shim_user="svc")
    calls = []
    monkeypatch.setattr(d, "probe_endpoint_cause",
                        lambda *a, **k: calls.append(a) or cause)
    svc = _fallback_service("primary")
    svc.dream_run = lambda extractor, *, limit=None: _failed()
    svc.dream_run_auto()
    svc.dream_run_auto()
    stall = svc.dream_stall_state()["stall"]
    assert stall["reason"] == "login_expired" and stall["provider"] == "claude"
    assert stall["last_error"] == "HTTP 500"
    assert len(calls) == 2


def test_other_failures_do_not_probe_for_an_identity(monkeypatch):
    from pseudolife_memory.memory import dream as d

    def _boom(*a, **k):
        raise AssertionError("no probe for a failure that is not a sign-in")
    monkeypatch.setattr(d, "probe_endpoint_cause", _boom)
    svc = _fallback_service("primary")
    svc.dream_run = lambda extractor, *, limit=None: _failed("extractor_error", "HTTP 500")
    svc.dream_run_auto()


def test_the_session_start_route_hands_the_host_header_to_the_hook(monkeypatch):
    # Opus review of 1f782865: only stall_where itself was tested.
    from pseudolife_memory.web import api
    from tests.asgi_helpers import call, stub_mcp
    seen = {}

    def _hook(service, *a, **k):
        seen["host"] = k.get("host")
        return "ok"
    monkeypatch.setattr(api, "hook_session_start", _hook)
    class _Service:            # the route only hands the service to the hook
        def __getattr__(self, name):
            return None
    # A remote Host is served only with a token (a tokenless /api is
    # loopback-only), which is how a remote daemon is reached.
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "tok-a:codex")
    app = api.build_console_app(stub_mcp, None, lambda: {"status": "ok"}, _Service(),
                                token_map={"tok-a": "codex"})
    st, _ = call(app, "GET", "/api/hook/session-start",
                 headers=[(b"host", b"box.example:8765"),
                          (b"authorization", b"Bearer tok-a")])
    assert st == 200 and seen["host"] == "box.example:8765"


def test_the_remedy_never_raises_on_a_strange_record():
    from pseudolife_memory.memory.dream import dream_stall_line
    line = dream_stall_line(_signed_out_stall(provider=["claude"]))
    assert line.startswith("Pseudolife-MCP: dreams stalled since ")
