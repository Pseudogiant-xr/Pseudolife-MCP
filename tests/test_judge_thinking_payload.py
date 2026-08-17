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
