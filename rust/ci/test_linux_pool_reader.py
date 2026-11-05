"""Availability reads have narrow scope, no credential redirects and a hosted fallback."""
from contextlib import nullcontext
import io
import json
import urllib.error
import urllib.request

import pytest

import linux_pool as pool


CONTEXT = dict(event="push", ref="refs/heads/master", head_ref="",
               head_repository="", repository="owner/repo")


def test_reader_uses_only_the_selected_variable_and_refuses_redirects(monkeypatch):
    def opener(handler):
        assert isinstance(handler, pool.NoRedirect)
        class Reader:
            def open(self, request, timeout):
                assert request.full_url.endswith("/owner/repo/actions/variables/" + pool.HEALTH_VARIABLE)
                assert request.get_method() == "GET" and timeout == 30
                assert request.get_header("Authorization") == "Bearer fixture-status-token"
                return nullcontext(io.StringIO(json.dumps({"value": "frame"})))
        return Reader()
    monkeypatch.setattr(pool.urllib.request, "build_opener", opener)
    assert pool.fetch_variable("owner/repo", pool.HEALTH_VARIABLE, "fixture-status-token") == "frame"
    request = urllib.request.Request("https://api.github.com/example")
    assert pool.NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://example.com") is None


@pytest.mark.parametrize("code", [301, 401, 403, 500])
def test_failed_reads_are_hosted_fallbacks_without_credentials_in_messages(monkeypatch, code):
    class Reader:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "fixture", {}, None)
    monkeypatch.setattr(pool.urllib.request, "build_opener", lambda *a: Reader())
    result = pool.live_choice(CONTEXT, "fixture-status-token")
    assert result["selfhosted_suites"] == []
    assert str(code) in result["reason"] and "fixture-status-token" not in result["reason"]


def test_missing_variable_is_not_an_error(monkeypatch):
    class Reader:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 404, "fixture", {}, None)
    monkeypatch.setattr(pool.urllib.request, "build_opener", lambda *a: Reader())
    assert pool.live_choice(CONTEXT, "fixture-status-token")["selfhosted_suites"] == []


@pytest.mark.parametrize("repository,name", [("../repo", pool.HEALTH_VARIABLE),
    ("owner/..", pool.HEALTH_VARIABLE), ("owner/repo", "UNRELATED_VARIABLE")])
def test_reader_cannot_fetch_other_configuration(monkeypatch, repository, name):
    monkeypatch.setattr(pool.urllib.request, "build_opener", lambda *a: pytest.fail("must not make a request"))
    with pytest.raises(pool.ReadError):
        pool.fetch_variable(repository, name, "fixture-status-token")


def test_kill_switch_is_read_live_and_avoids_the_health_request(monkeypatch):
    calls = []
    def read(repository, name, token):
        calls.append(name)
        return "hosted"
    monkeypatch.setattr(pool, "fetch_variable", read)
    assert pool.live_choice(CONTEXT, "fixture-status-token")["selfhosted_suites"] == []
    assert calls == [pool.POOL_VARIABLE]


def test_fork_event_never_uses_the_reader_token(monkeypatch):
    context = dict(CONTEXT, event="pull_request", head_ref="integrate/example", head_repository="fork/repo")
    monkeypatch.setattr(pool, "fetch_variable", lambda *a: pytest.fail("fork must not make a request"))
    assert pool.live_choice(context, "fixture-status-token")["selfhosted_suites"] == []


def test_live_healthy_choice_and_missing_token(monkeypatch):
    frame = json.dumps(dict(schema=1, observed_at=1000, online=3, idle=3))
    monkeypatch.setattr(pool.time, "time", lambda: 1000)
    monkeypatch.setattr(pool, "fetch_variable", lambda repository, name, token:
                        "self-hosted" if name == pool.POOL_VARIABLE else frame)
    assert pool.live_choice(CONTEXT, "")["selfhosted_suites"] == []
    assert pool.live_choice(CONTEXT, "fixture-status-token")["selfhosted_suites"] == [pool.SUITE]
