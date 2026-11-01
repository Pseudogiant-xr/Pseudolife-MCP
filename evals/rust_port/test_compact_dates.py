"""Compact search dates vary by capture arm but keep their calendar contract."""
import copy
from datetime import datetime, timedelta, timezone
import json

import pytest

from . import full_bank
from .harness import compare


def search_payload(day):
    return {"count": 3, "entries": [
        {"id": i, "text": "synthetic entry", "date": day, "score": 0.5}
        for i in (1, 2, 3)]}


def observe_search(monkeypatch, start, end, payload):
    class Clock(datetime):
        moments = iter((start, end))

        @classmethod
        def now(cls):
            return next(cls.moments)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self, request, surface, runtime_headers):
            body = {"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": copy.deepcopy(payload)}}
            raw = json.dumps(body).encode()
            return {"status": 200, "headers": {"content-type": "application/json",
                    "content-length": str(len(raw))}, "body": body, "_wire_body": raw}

    monkeypatch.setattr(full_bank, "datetime", Clock)
    monkeypatch.setattr(full_bank, "HttpClient", Client)
    corpus = full_bank.full_corpus()
    corpus["cases"] = [case for case in corpus["cases"] if case["id"] == "search-mcp"]
    return full_bank.observe(corpus, "http://127.0.0.1:1", "synthetic-token")[0]


def test_search_mcp_arms_either_side_of_midnight(monkeypatch):
    before = datetime(2026, 10, 31, 23, 59, 54, tzinfo=timezone.utc)
    after = datetime(2026, 11, 1, 0, 0, 2, tzinfo=timezone.utc)
    left = observe_search(monkeypatch, before, before, search_payload("2026-10-31"))
    right = observe_search(monkeypatch, after, after, search_payload("2026-11-01"))
    assert compare(left["response"], right["response"], full_bank.case_policy(left)) == []
    result = left["response"]["body"]["result"]
    assert result["structuredContent"]["entries"][0]["date"] == "<local-date:YYYY-MM-DD>"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


@pytest.mark.parametrize("bad", ["2026/11/01", "2026-11-1", "2026-11-01T00:00:00Z",
                                  "2026-02-30", "2026-10-31", "2026-11-02", "",
                                  None, 20261101, "<local-date:YYYY-MM-DD>"])
def test_wrong_search_dates_remain_differences_in_both_mcp_copies(monkeypatch, bad):
    now = datetime(2026, 11, 1, 0, 0, 2, tzinfo=timezone.utc)
    left = observe_search(monkeypatch, now, now, search_payload("2026-11-01"))
    payload = search_payload("2026-11-01")
    payload["entries"][1]["date"] = bad
    right = observe_search(monkeypatch, now, now, payload)
    differences = compare(left["response"], right["response"], full_bank.case_policy(left))
    assert {item["path"] for item in differences if item["path"].endswith("/date")} == {
        "/body/result/structuredContent/entries/1/date",
        "/body/result/content/0/text/entries/1/date"}


def test_missing_search_date_is_not_dropped_from_comparison(monkeypatch):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    left = observe_search(monkeypatch, now, now, search_payload("2026-11-01"))
    payload = search_payload("2026-11-01")
    del payload["entries"][0]["date"]
    right = observe_search(monkeypatch, now, now, payload)
    assert "field_presence" in {item["reason"] for item in compare(
        left["response"], right["response"], full_bank.case_policy(left))}


def test_window_spanning_midnight_accepts_both_endpoint_dates(monkeypatch):
    start = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
    end = start + timedelta(seconds=2)
    payload = search_payload("2026-10-31")
    payload["entries"][1]["date"] = "2026-11-01"
    record = observe_search(monkeypatch, start, end, payload)
    assert {entry["date"] for entry in record["response"]["body"]["result"][
        "structuredContent"]["entries"]} == {"<local-date:YYYY-MM-DD>"}


def test_dates_outside_declared_search_shape_stay_exact():
    payload = search_payload("2026-11-01")
    payload["date"] = "2026-10-31"
    payload["entries"][0]["metadata"] = {"date": "2026-10-31"}
    assert full_bank.normalize_payload(full_bank.Normalizer(), payload, "unrelated") == payload
    assert full_bank.Normalizer().apply(payload, {}) == payload
    now = datetime(2026, 11, 1)
    normalized = full_bank.normalize_payload(full_bank.Normalizer(), payload, "search",
                                            capture_window=(now, now))
    assert normalized["date"] == payload["date"]
    assert normalized["entries"][0]["metadata"] == payload["entries"][0]["metadata"]
    assert payload["entries"][0]["date"] == "2026-11-01"


def test_search_dates_require_a_window_but_rest_epochs_do_not():
    with pytest.raises(ValueError, match="capture window"):
        full_bank.normalize_payload(full_bank.Normalizer(), search_payload("2026-11-01"), "search")
    now = datetime(2026, 11, 1)
    with pytest.raises(ValueError, match="capture window"):
        full_bank.normalize_payload(full_bank.Normalizer(), search_payload("2026-11-01"), "search",
                                    capture_window=(now, now - timedelta(seconds=1)))
    payload = {"entries": [{"id": i, "timestamp": 1790000000.0, "superseded_at": None}
                            for i in (1, 2, 3)]}
    normalized = full_bank.normalize_payload(full_bank.Normalizer(), payload, "search")
    assert all(entry["timestamp"].startswith("<epoch:float:epoch-unit=seconds:")
               and entry["superseded_at"] is None for entry in normalized["entries"])


def test_date_window_uses_local_calendar_rather_than_utc(monkeypatch):
    # The same instant is still October 31 in UTC, but November 1 locally.
    now = datetime(2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=11)))
    result = observe_search(monkeypatch, now, now, search_payload("2026-11-01"))
    assert result["response"]["body"]["result"]["structuredContent"]["entries"][0][
        "date"] == "<local-date:YYYY-MM-DD>"


def test_phase1_generic_controls_across_midnight(monkeypatch):
    from contextlib import contextmanager
    import tempfile
    from evals.rust_baseline import common, daemon
    from . import controls, stdio_capture

    moments = [datetime(2026, 10, 31, 23, 59, 54),
               datetime(2026, 11, 1, 0, 0, 2), datetime(2026, 11, 1, 0, 0, 7)]

    class Clock(datetime):
        ticks = iter([moment for moment in moments for _ in range(2)])

        @classmethod
        def now(cls):
            return next(cls.ticks)

    class Client:
        arms = iter(moments)

        def __init__(self, *args, **kwargs):
            self.day = next(self.arms).date().isoformat()

        def execute(self, request, surface, runtime_headers):
            payload = search_payload(self.day)
            body = {"result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                               "structuredContent": payload}}
            raw = json.dumps(body).encode()
            return {"status": 200, "body": body, "_wire_body": raw,
                    "headers": {"content-type": "application/json", "content-length": str(len(raw))}}

    @contextmanager
    def database():
        yield "synthetic-disposable-bank"

    @contextmanager
    def launcher(dsn, private, *, env_extra, source_root, child_module):
        assert child_module == "evals.rust_port.stdio_daemon"
        yield None, "http://127.0.0.1:1", {}

    @contextmanager
    def proxy(url, mutation):
        assert mutation == "identity"
        yield url, {"mutations": 0}

    monkeypatch.setattr(full_bank, "datetime", Clock)
    monkeypatch.setattr(full_bank, "HttpClient", Client)
    monkeypatch.setattr(daemon, "disposable_database", database)
    monkeypatch.setattr(daemon, "private_directory", tempfile.TemporaryDirectory)
    monkeypatch.setattr(daemon, "launched_daemon", launcher)
    monkeypatch.setattr(common, "lease_gate", lambda stamp: {})
    monkeypatch.setattr(controls, "mutation_proxy", proxy)
    monkeypatch.setattr(controls, "capture_controls", lambda *args, **kwargs: {})
    monkeypatch.setattr(stdio_capture, "require_phase1_source", lambda root: {})
    monkeypatch.setattr(full_bank, "environment_metadata", lambda root: {})
    monkeypatch.setattr(full_bank, "private_home_overrides", lambda private: {})
    corpus = full_bank.full_corpus()
    corpus["cases"] = [case for case in corpus["cases"] if case["id"] == "search-mcp"]
    _, receipt = full_bank.run(corpus, "verified", validate_controls=True, phase1_protocol_fixture=True)
    assert receipt["status"] == "passed" and receipt["differences"] == []
    assert receipt["identity_proxy_validation"] == "passed"
    assert full_bank.COMPACT_DATE_RULE in receipt["normalization_rules"]
