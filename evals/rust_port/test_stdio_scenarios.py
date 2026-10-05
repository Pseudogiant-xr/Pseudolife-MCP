import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib

import pytest

from evals.rust_port.stdio_judge import concurrent_policy, judge
from evals.rust_port.stdio_scenarios import concurrent_difference, expected_stderr, startup_difference
from evals.rust_port.stdio_capture import ORACLE_HEAD


def transcript(order=("open", "A", "B")):
    frames = {name: ('{"jsonrpc":"2.0","id":"' + name + '","result":{}}\n').encode()
              for name in ("open", "A", "B")}
    return {"stdout_frames_b64": [base64.b64encode(frames[name]).decode() for name in order],
            "stderr_b64": "", "exit_code": 0}


def evidence():
    return json.loads(Path(__file__).with_name("stdio_concurrent_orders.json").read_text())


def test_current_startup_contract_binds_selected_pin_and_literal_version():
    root = Path(__file__).resolve().parents[2]
    contract = json.loads(Path(__file__).with_name("stdio_startup_contract.json").read_text())
    pinned = subprocess.check_output(["git", "show", ORACLE_HEAD + ":pyproject.toml"], cwd=root)
    version = tomllib.loads(pinned.decode())["project"]["version"]
    assert contract["oracle_head"] == ORACLE_HEAD
    assert contract["package_version"] == version
    case = next(item for item in contract["cases"] if item["case"] == "version-mismatch")
    assert "this shim is pseudolife-mcp " + version + " but the daemon" in case["stderr_lf_template"]
    assert "{package_version}" not in case["stderr_lf_template"]


@pytest.mark.parametrize("wrong_version", ["0.16.1", "99.0.0"])
def test_current_version_mismatch_keeps_the_exact_version_assertion(wrong_version):
    contract = json.loads(Path(__file__).with_name("stdio_startup_contract.json").read_text())
    expected = expected_stderr(contract, "version-mismatch", "fixture-origin")
    result = {"stderr_b64": base64.b64encode(expected).decode(),
              "stdout_frames_b64": ["initialize-frame"], "exit_code": 0}
    records = [{"method": "GET", "path": "/health"}]
    assert startup_difference(result, contract, "version-mismatch", "fixture-origin", None, records) == []
    mutated = expected.replace(b"pseudolife-mcp 0.17.0", b"pseudolife-mcp " + wrong_version.encode())
    result["stderr_b64"] = base64.b64encode(mutated).decode()
    assert startup_difference(result, contract, "version-mismatch", "fixture-origin", None, records) == [
        {"path": "/stderr", "reason": "startup_stderr_bytes"}]


def test_historical_startup_contract_preserves_the_frozen_git_blob():
    root = Path(__file__).resolve().parents[2]
    current = json.loads(Path(__file__).with_name("stdio_startup_contract.json").read_text())
    history = current["historical_contract"]
    frozen = subprocess.check_output(["git", "show", history["git_blob"]], cwd=root)
    historical = Path(__file__).with_name(history["file"])
    assert json.loads(historical.read_text()) == json.loads(frozen)
    assert hashlib.sha256(frozen).hexdigest() == history["git_blob_sha256"]
    assert json.loads(frozen)["package_version"] == "0.16.1"
    assert json.loads(frozen)["oracle_head"] == "f709abb54f7912ae9cd767998d0926ca33df4bcd"


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_current_startup_proofs_cover_all_cases_and_both_arms(platform):
    directory = Path(__file__).parent
    contract = json.loads((directory / "stdio_startup_contract.json").read_text())
    proof = contract["proofs"][platform]
    path = directory / proof["public_receipt"]
    captured = json.loads(path.read_text())
    root = directory.resolve().parents[1]
    relative = path.resolve().relative_to(root).as_posix()
    committed = subprocess.check_output(["git", "show", ":" + relative], cwd=root)
    assert json.loads(committed) == captured
    assert hashlib.sha256(committed).hexdigest() == proof["public_receipt_sha256"]
    assert captured["oracle_head"] == ORACLE_HEAD
    assert captured["package_version"] == captured["runtime"]["package_runtime_version"] == "0.17.0"
    assert captured["case_inventory"] == [case["case"] for case in contract["cases"]]
    assert captured["case_count"] == 7 and captured["arm_cell_count"] == 14
    assert {(cell["case"], cell["arm"]) for cell in captured["cells"]} == {
        (case["case"], arm) for case in contract["cases"] for arm in ("oracle", "candidate")}
    assert all(cell["cleanup_verified"] and cell["expected_stderr_matches"] for cell in captured["cells"])
    assert captured["current_expectation_differences"] == []
    assert captured["cleanup_verified"] and not captured["storage_daemon_or_postgres_launched"]


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
