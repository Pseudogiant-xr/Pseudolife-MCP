"""Positive receipt work and private pre-state remain load-bearing for timing."""
import base64
import copy
import os
from pathlib import Path

import pytest

from evals.rust_baseline import cli_measurement
from evals.rust_baseline.test_cli_measurement import installed_instrument


def doorbell_instrument(tmp_path, monkeypatch):
    args, _, _, _, calls = installed_instrument(tmp_path, monkeypatch)
    args.mode = "doorbell-prompt-seen"
    args.argv_json = None
    args.layout = "bare"
    case, prepare = cli_measurement.doorbell_fixture()
    monkeypatch.setattr(cli_measurement, "require_instrument_binding", lambda *args: {})
    before = []

    def observed(command, argv, **kwargs):
        from pseudolife_memory.codex_doorbell_state import PendingNotice
        from pseudolife_memory.private_state import open_private
        from evals.rust_port.cli_doorbell_seen import NONCE, THREAD
        home = Path(kwargs["env"]["HOME"])
        assert kwargs["env"]["PSEUDOLIFE_DIGEST_DIR"] == str(home / "digests")
        assert kwargs["stdin"] == base64.b64decode(case["stdin_b64"])
        assert argv == case["argv"]
        assert cli_measurement.snapshot(home) == case["pre_files_b64"]
        before.append(cli_measurement.snapshot(home))
        pending = PendingNotice(home / "digests", THREAD)
        for path, value in ((pending.prompt_seen_path, (NONCE + "\n").encode()),
                            (pending.path.with_suffix(".bell-lock"), b"0")):
            fd = open_private(path, os.O_WRONLY | os.O_CREAT)
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
        calls.append(list(argv))
        return {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    return args, case, prepare, calls, before, observed


def test_positive_doorbell_case_preserves_seed_and_receipt_across_repeat_floors(tmp_path, monkeypatch):
    args, case, prepare, calls, before, _ = doorbell_instrument(tmp_path, monkeypatch)
    clocks = []
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: clocks.append(len(calls)) or 1.0)
    receipt = cli_measurement.measure(args, {}, case=case, prepare=prepare)
    assert len(calls) == len(before) == 62
    assert clocks[::2] == list(range(2, 62))
    assert receipt["prepared_case"] == case
    assert case["pre_files_b64"]
    assert all(len(rows) == 30 for rows in receipt["runs"].values())
    assert all(receipt["metrics"][arm]["cold_start_to_exit_ms"]["p50"]["noise_floor_abs"] is not None
               for arm in ("python", "rust"))
    assert all(row["files_and_environment_match_control"] for rows in receipt["runs"].values() for row in rows)
    assert all(control["private_metadata"] for control in receipt["prepared_controls"].values())


@pytest.mark.parametrize("field", ["id", "stdin_b64", "pre_files_b64", "environment_deltas", "argv", "normalizations"])
def test_doorbell_admission_rejects_changed_recorded_case_before_launch(tmp_path, monkeypatch, field):
    args, case, prepare, calls, _, _ = doorbell_instrument(tmp_path, monkeypatch)
    altered = copy.deepcopy(case)
    altered[field] = {} if isinstance(case[field], dict) else ["changed"] if isinstance(case[field], list) else "changed"
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(ValueError, match="doorbell.*case|exact mode/argv"):
        cli_measurement.measure(args, {}, case=altered, prepare=prepare)
    assert not calls


@pytest.mark.parametrize("change", ["seed", "extra-file", "hardlink", "environment", "prefix", "stdin"])
def test_doorbell_preparation_cannot_change_recorded_inputs_or_private_metadata(tmp_path, monkeypatch, change):
    args, case, prepare, calls, _, _ = doorbell_instrument(tmp_path, monkeypatch)

    def changed(cell, home, env, command, commands):
        selected = prepare(cell, home, env, command, commands)
        pending = home / next(iter(cell["pre_files_b64"]))
        if change == "seed":
            pending.write_bytes(b"different")
        elif change == "extra-file":
            (home / "extra").write_bytes(b"extra")
        elif change == "hardlink":
            os.link(pending, home / "linked")
        elif change == "environment":
            env["PSEUDOLIFE_DIGEST_DIR"] = str(home / "different")
        elif change == "prefix":
            import shutil
            relocated = home / "relocated"
            shutil.copy2(command[0], relocated)
            return [str(relocated), *command[1:]]
        else:
            cell["stdin_b64"] = ""
        return selected

    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises((ValueError, RuntimeError, OSError)):
        cli_measurement.measure(args, {}, case=case, prepare=changed)
    assert not calls


@pytest.mark.parametrize("change", ["no-receipt", "receipt-bytes", "pending-bytes", "extra-file", "metadata", "environment", "case", "binary"])
def test_doorbell_checks_both_control_and_timed_state(tmp_path, monkeypatch, change):
    args, case, prepare, calls, _, observed = doorbell_instrument(tmp_path, monkeypatch)

    def changed(command, argv, **kwargs):
        response = observed(command, argv, **kwargs)
        home = Path(kwargs["env"]["HOME"])
        if len(calls) == 3:
            pending = home / next(iter(case["pre_files_b64"]))
            receipt = pending.with_suffix(".bell-prompt-seen")
            if change == "no-receipt":
                receipt.unlink()
            elif change == "receipt-bytes":
                receipt.write_bytes(b"wrong\n")
            elif change == "pending-bytes":
                pending.write_bytes(b"wrong")
            elif change == "extra-file":
                (home / "extra").write_bytes(b"extra")
            elif change == "metadata":
                os.link(receipt, home / "linked")
            elif change == "environment":
                kwargs["env"]["PSEUDOLIFE_DIGEST_DIR"] = str(home / "other")
            elif change == "case":
                case["stdin_b64"] = ""
            else:
                Path(command[0]).write_bytes(b"changed executable")
        return response

    monkeypatch.setattr(cli_measurement, "run_cli", changed)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        cli_measurement.measure(args, {}, case=case, prepare=prepare)
    assert len(calls) == 3


def test_doorbell_default_cli_path_selects_only_positive_bare_case(tmp_path, monkeypatch):
    args, case, prepare, calls, _, _ = doorbell_instrument(tmp_path, monkeypatch)
    receipt = cli_measurement.measure(args, {})
    assert receipt["prepared_case"] == case
    assert len(calls) == 62
    args.layout = "installed"
    with pytest.raises(ValueError, match="bare"):
        cli_measurement.measure(args, {})
