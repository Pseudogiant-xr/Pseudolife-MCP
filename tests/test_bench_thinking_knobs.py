"""Request-payload contract for the bench answerer/judge thinking knobs.

Synthesis plan (2026-08-17): the shipped default — ``enable_thinking:false``,
``temperature 0`` — is the permanent regression-gate config and must stay
byte-identical. Two env knobs let labeled experiment arms deviate:

* ``PSEUDOLIFE_BENCH_THINKING=low|medium`` replaces the pin with
  ``{"reasoning_effort": <v>}`` and adds reasoning token headroom.
* ``PSEUDOLIFE_BENCH_SAMPLER=<json>`` merges sampler fields (e.g. the
  official instruct params + a fixed seed) into the request body, last.

Import note: longmemeval_bench imports ladder_sweep/torch at module level,
so this file keeps to one module-scoped import.
"""
import json
from unittest import mock

import pytest


@pytest.fixture(scope="module")
def bench():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import longmemeval_bench as B
    return B


@pytest.fixture()
def captured(bench):
    bodies: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data.decode()))
        return _Resp()

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        yield bodies


def test_default_payload_unchanged(bench, captured, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_BENCH_THINKING", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_BENCH_SAMPLER", raising=False)
    bench._chat("sys", "user")
    body = captured[0]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["temperature"] == 0
    assert body["max_tokens"] == 256
    assert "seed" not in body


def test_thinking_env_sets_reasoning_effort_and_headroom(bench, captured,
                                                         monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_BENCH_THINKING", "low")
    monkeypatch.delenv("PSEUDOLIFE_BENCH_SAMPLER", raising=False)
    bench._chat("sys", "user")
    body = captured[0]
    assert body["chat_template_kwargs"] == {"reasoning_effort": "low"}
    assert body["max_tokens"] == 256 + 4096
    assert body["temperature"] == 0


def test_thinking_env_rejects_unknown_level(bench, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_BENCH_THINKING", "xhigh-typo")
    with pytest.raises(ValueError):
        bench._chat("sys", "user")


def test_sampler_env_merges_last(bench, captured, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_BENCH_THINKING", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_BENCH_SAMPLER", json.dumps(
        {"temperature": 0.7, "top_p": 0.8, "presence_penalty": 1.5,
         "seed": 7}))
    bench._chat("sys", "user")
    body = captured[0]
    assert body["temperature"] == 0.7
    assert body["top_p"] == 0.8
    assert body["presence_penalty"] == 1.5
    assert body["seed"] == 7
    # The no-think pin survives the sampler merge — the pilot is greedy-vs-
    # sampled under the SAME thinking config, not a thinking change.
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
