import base64
import copy
import json
from pathlib import Path

import pytest

from evals.rust_port.stdio_judge import concurrent_policy, judge
from evals.rust_port.stdio_scenarios import concurrent_difference, expected_stderr, startup_difference


def transcript(order=("open", "A", "B")):
    frames = {name: ('{"jsonrpc":"2.0","id":"' + name + '","result":{}}\n').encode()
              for name in ("open", "A", "B")}
    return {"stdout_frames_b64": [base64.b64encode(frames[name]).decode() for name in order],
            "stderr_b64": "", "exit_code": 0}


def evidence():
    return json.loads(Path(__file__).with_name("stdio_concurrent_orders.json").read_text())


@pytest.mark.parametrize("platform", ["windows", "linux"])
@pytest.mark.parametrize("era", ["2025-11-25", "2026-07-28"])
def test_shared_release_accepts_only_observed_final_pair_permutations(platform, era):
    policy = concurrent_policy(evidence(), platform, era, "together")
    assert judge(transcript(), transcript(("open", "B", "A")), policy) == []
    assert judge(transcript(), transcript(("A", "open", "B")), policy)
    assert judge(transcript(), transcript(("open", "A", "A")), policy)
    assert judge(transcript(), transcript(("open", "A")), policy)


@pytest.mark.parametrize("release", ["AB", "BA"])
def test_individual_release_keeps_its_causal_response_order(release):
    policy = concurrent_policy(evidence(), "windows", "2025-11-25", release)
    expected = transcript(("open", *release))
    assert judge(expected, transcript(("open", *reversed(release))), policy)


@pytest.mark.parametrize("mutation", ["spacing", "payload", "stderr", "exit", "duplicate"])
def test_shared_release_does_not_mask_byte_or_envelope_changes(mutation):
    policy = concurrent_policy(evidence(), "windows", "2025-11-25", "together")
    actual = transcript(("open", "B", "A"))
    raw = base64.b64decode(actual["stdout_frames_b64"][-1])
    if mutation == "spacing":
        raw = b" " + raw
    elif mutation == "payload":
        raw = raw.replace(b'"result":{}', b'"result":{"changed":true}')
    elif mutation == "stderr":
        actual["stderr_b64"] = base64.b64encode(b"unexpected\n").decode()
    elif mutation == "exit":
        actual["exit_code"] = 1
    else:
        raw = b'{"id":"duplicate",' + raw[1:]
    actual["stdout_frames_b64"][-1] = base64.b64encode(raw).decode()
    assert judge(transcript(), actual, policy)


def test_responses_after_eof_do_not_prove_non_eof_concurrency():
    events = [{"event": "stdin-sent", "id": "A"}, {"event": "upstream-arrived", "id": "A"},
              {"event": "stdin-sent", "id": "B"}, {"event": "upstream-arrived", "id": "B"},
              {"event": "released-both"},
              {"event": "stdout-response", "id": "A", "stdin_open": True},
              {"event": "stdout-response", "id": "B", "stdin_open": True}]
    assert concurrent_difference({"events": events}) == []
    late = copy.deepcopy(events)
    late[-1]["stdin_open"] = False
    assert concurrent_difference({"events": late})
    assert concurrent_difference({"events": events[1:]})


def test_startup_contract_compares_exact_parameters_bytes_exit_and_traffic():
    contract = {"cases": [{"case": "refusal", "stderr_lf_template": "at {fixture_url}: {credential_file}\n",
                           "exit_code": 1, "stdout_frame_count": 0}]}
    expected = expected_stderr(contract, "refusal", "fixture-origin", "fixture-file")
    result = {"stderr_b64": base64.b64encode(expected).decode(), "stdout_frames_b64": [], "exit_code": 1}
    assert startup_difference(result, contract, "refusal", "fixture-origin", "fixture-file", []) == []
    assert startup_difference(result, contract, "refusal", "wrong-origin", "fixture-file", [])
    assert startup_difference(result, contract, "refusal", "fixture-origin", "fixture-file", [{"method": "POST"}])
    result["exit_code"] = 0
    assert startup_difference(result, contract, "refusal", "fixture-origin", "fixture-file", [])
