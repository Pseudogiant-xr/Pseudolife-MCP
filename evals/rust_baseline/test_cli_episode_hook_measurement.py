"""The producer fixture admits one exact string input with the same state guards."""
import base64
import copy

import pytest

from evals.rust_baseline import cli_measurement
from evals.rust_baseline.test_cli_episode_measurement import episode_instrument, measure


@pytest.mark.parametrize("mutation", [None, "numeric", "different-string", "extra-field", "wrong-id"])
def test_exact_hook_string_fixture_keeps_admission_and_readback_guards(tmp_path, monkeypatch, mutation):
    args, case, prepare, verify, calls, state = episode_instrument(tmp_path, monkeypatch)
    case["id"] = "episode-hook-string42-episode-end"
    case["stdin_b64"] = base64.b64encode(b'{"session_id": "42"}').decode("ascii")
    original = cli_measurement.run_cli

    def observed(command, argv, **kwargs):
        assert kwargs["stdin"] == b'{"session_id": "42"}'
        # Reuse the independent fake state transition, whose legacy input stays intact.
        return original(command, argv, **{**kwargs, "stdin": b'{"session_id": 42}'})

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    altered = copy.deepcopy(case)
    if mutation == "wrong-id":
        altered["id"] += "-other"
    elif mutation is not None:
        raw = {"numeric": b'{"session_id": 42}', "different-string": b'{"session_id": "43"}',
               "extra-field": b'{"session_id": "42", "cwd": null}'}[mutation]
        altered["stdin_b64"] = base64.b64encode(raw).decode("ascii")
    if mutation is not None:
        with pytest.raises(ValueError, match="recorded authenticated episode-end"):
            measure(args, altered, prepare, verify)
        assert calls == [] and state["history"] == []
    else:
        receipt = measure(args, altered, prepare, verify)
        assert len(calls) == len(state["history"]) == 62
        assert len(receipt["independent_state_checks"]) == 62
