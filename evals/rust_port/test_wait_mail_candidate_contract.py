"""Approved producer expectations retain raw evidence and reject scope drift."""
import base64
import copy
import os
import time

import pytest

from evals.rust_port.wait_mail_candidate_contract import EXPECTATIONS
from evals.rust_port.wait_mail_policy import LEDGER, compare_record


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


def contract_record(case_id, platform):
    fixture = EXPECTATIONS[case_id]
    newline = "\r\n" if platform == "windows" else "\n"
    files = {".pseudolife-mcp/token": encoded(b"seed"), **fixture["request"]["pre_files_b64"]}
    oracle = {"exit_code": 3, "stdout_b64": "", "stderr_b64": encoded(b"historical Python response\n"),
              "post_files_b64": copy.deepcopy(files)}
    epoch = 1791242386
    stderr = fixture["stderr"]
    post = copy.deepcopy(files)
    if "delivery" in fixture:
        clock = time.strftime("%H:%M:%S", time.localtime(epoch))
        stderr = stderr.replace("<local-clock>", clock)
        post[fixture["delivery"]["seen_path"]] = encoded(b"12\n")
        post[LEDGER] = encoded((str(epoch) + "\twait\taaaaaaaa\t12\t5\trung anyone" + newline).encode())
    stdout = fixture["stdout"] if "delivery" in fixture else fixture["stdout"].replace("\n", newline)
    candidate = {"exit_code": fixture["exit_code"], "stdout_b64": encoded(stdout.encode()),
                 "stderr_b64": encoded(stderr.replace("\n", newline).encode()), "post_files_b64": post}
    def observation(response):
        return {"request": copy.deepcopy(fixture["request"]), "pre_files_b64": copy.deepcopy(files),
                "environment": {"fixture": "same"}, "response": response,
                "execution": {"wall_window": [epoch, epoch + 0.5]}}
    return {"id": case_id, "mode": "wait-mail", "oracle": observation(oracle),
            "candidate": observation(candidate)}


@pytest.mark.parametrize("platform", ["linux", "windows"])
@pytest.mark.parametrize("case_id", sorted(EXPECTATIONS))
def test_explicit_candidate_contract_retains_raw_oracle_and_rejects_mutations(case_id, platform):
    record = contract_record(case_id, platform)
    original = copy.deepcopy(record)
    checked = compare_record(record, platform)
    assert checked["passed"], checked
    assert checked["candidate_contract_substitution"] == EXPECTATIONS[case_id]["substitution"]
    assert checked["raw_oracle_differences"]
    assert record == original
    for field in ("exit_code", "stdout_b64", "stderr_b64", "post_files_b64"):
        changed = copy.deepcopy(record)
        response = changed["candidate"]["response"]
        response[field] = response[field] + 1 if field == "exit_code" else {} if field == "post_files_b64" \
            else encoded(base64.b64decode(response[field]) + b"\x00")
        assert not compare_record(changed, platform)["passed"], field
    changed = copy.deepcopy(record)
    changed["id"] = "unlisted-producer-case"
    assert not compare_record(changed, platform)["passed"]
    for arm in ("oracle", "candidate"):
        changed = copy.deepcopy(record)
        changed[arm]["request"]["argv"].append("extra")
        assert not compare_record(changed, platform)["passed"]
        changed = copy.deepcopy(record)
        changed[arm]["pre_files_b64"][".pseudolife-mcp/token"] = encoded(b"different")
        assert not compare_record(changed, platform)["passed"]


def test_declared_record_bytes_and_delivery_clock_cannot_drift():
    record = contract_record("wait-mail-long-ring-watermark", "linux")
    for arm in ("oracle", "candidate"):
        record[arm]["pre_files_b64"][EXPECTATIONS[record["id"]]["delivery"]["seen_path"]] = encoded(b"1\n")
    assert not compare_record(record, "linux")["passed"]
    record = contract_record("wait-mail-long-ring-watermark", "linux")
    record["candidate"]["execution"]["wall_window"] = [1791242390, 1791242390.5]
    assert not compare_record(record, "linux")["passed"]
    record = contract_record("wait-mail-long-ring-watermark", "linux")
    for arm in ("oracle", "candidate"):
        record[arm]["pre_files_b64"][LEDGER] = encoded(b"old ledger\n")
    assert not compare_record(record, "linux")["passed"]


def test_full_pair_lane_uses_contract_and_keeps_actual_oracle(monkeypatch):
    from evals.rust_port import cli_process
    platform = "windows" if os.name == "nt" else "linux"
    records = {case_id: contract_record(case_id, platform) for case_id in EXPECTATIONS}
    before = copy.deepcopy(records)
    def captured(case, command, commands, **kwargs):
        return copy.deepcopy(records[case["id"]][command[0]])
    monkeypatch.setattr(cli_process, "observe", captured)
    result = cli_process.paired_cases([value["request"] for value in EXPECTATIONS.values()],
                                    {"oracle": ["oracle"], "candidate": ["candidate"]},
                                    root=None, home=None, url=None)
    assert result["passed"], result
    assert len(result["candidate_output_controls"]) == 76
    assert all(control["rejected"] for control in result["candidate_output_controls"])
    for record in result["records"]:
        assert record["oracle"] == before[record["id"]]["oracle"]
        assert record["candidate"] == before[record["id"]]["candidate"]
        assert record["raw_oracle_differences"]
        assert record["candidate_expected_response"]
