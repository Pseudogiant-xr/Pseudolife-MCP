"""judge_merges request-payload contract around the thinking kwargs.

The shipped default pins ``chat_template_kwargs: {enable_thinking: false}``
on every judge call — server-side reasoning defaults are inert (verified
2026-08-17: a reasoning_effort=xhigh server produced a byte-identical judge
ladder). The ``judge_thinking`` experiment knob removes the pin so the
server/template default governs, and adds reasoning headroom to the token
budget. The daemon never passes the knob, so default behaviour must stay
byte-identical.
"""
import json
from unittest import mock

import pytest

from pseudolife_memory.memory import dream as D


@pytest.fixture()
def captured():
    bodies: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": '{"verdicts": []}'}}]}
            ).encode()

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data.decode()))
        return _Resp()

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        yield bodies


_PROPOSAL = {"n": 1, "from": {"display": "a"}, "into": {"display": "b"},
             "reason": "test"}


def test_default_payload_still_pins_thinking_off(captured):
    ex = D.OpenAICompatExtractor("http://x/v1", "m")
    ex.judge_merges([_PROPOSAL])
    body = captured[0]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == max(ex.max_tokens, 120)


def test_judge_thinking_unpins_and_adds_reasoning_headroom(captured):
    ex = D.OpenAICompatExtractor("http://x/v1", "m", judge_thinking=True)
    ex.judge_merges([_PROPOSAL])
    body = captured[0]
    assert "chat_template_kwargs" not in body
    assert body["max_tokens"] == max(ex.max_tokens, 120) + 4096


def test_judge_thinking_default_is_off():
    ex = D.OpenAICompatExtractor("http://x/v1", "m")
    assert ex.judge_thinking is False


def test_constructor_defaults_match_shipped_config():
    # The constructor defaults were a stale pre-2026-06-22 remnant (400, 20s)
    # while the shipped DreamConfig moved to (2048, 240s) — direct
    # constructors silently got a quarter of the deployed budget, and syncing
    # only max_tokens would have recreated the documented big-budget/
    # tiny-timeout claims:0 failure (config.py's own history note). Keep BOTH
    # in lockstep so "the default" means one thing.
    from pseudolife_memory.utils.config import DreamConfig
    ex = D.OpenAICompatExtractor("http://x/v1", "m")
    cfg = DreamConfig()
    assert ex.max_tokens == cfg.extractor_max_tokens
    assert ex.timeout == cfg.extractor_timeout_seconds


def test_judge_thinking_effort_string_sets_reasoning_effort(captured):
    ex = D.OpenAICompatExtractor("http://x/v1", "m", judge_thinking="low")
    ex.judge_merges([_PROPOSAL])
    body = captured[0]
    assert body["chat_template_kwargs"] == {"reasoning_effort": "low"}
    assert body["max_tokens"] == max(ex.max_tokens, 120) + 4096


def test_extractor_reasoning_effort_never_reaches_the_judge_payload(captured):
    # With judge_url unset the Step-C judge reuses the PRIMARY dream
    # extractor, whose extra_body may carry the Console's dreamer effort
    # knob (extractor_reasoning_effort). The judge owns its own thinking
    # dimension (judge_thinking / the enable_thinking pin), so the dreamer
    # knob must be stripped here — otherwise the CLI shims, which honour a
    # top-level reasoning_effort, would silently override the judge's
    # thinking-off pin the moment an operator tunes the dreamer.
    ex = D.OpenAICompatExtractor(
        "http://x/v1", "m",
        extra_body={"cache_prompt": False, "reasoning_effort": "low"})
    ex.judge_merges([_PROPOSAL])
    body = captured[0]
    assert "reasoning_effort" not in body
    assert body["cache_prompt"] is False        # the rest still spreads
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
