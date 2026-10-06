"""Every timed block reuses the installed images that its untimed starts warmed."""
from pathlib import Path

import pytest

from evals.rust_baseline import cli_measurement
from evals.rust_baseline.test_cli_measurement import installed_instrument, instrument


def test_every_repeat_warms_both_reused_installed_images_before_timing(tmp_path, monkeypatch):
    args, case, prepare, prepared, calls = installed_instrument(tmp_path, monkeypatch)
    events = []
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: events.append("timer") or 1.0)

    def observed(command, argv, **kwargs):
        path = Path(command[0])
        metadata = path.stat()
        events.append((path.name, metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns))
        calls.append(argv)
        return {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    receipt = cli_measurement.measure(args, {}, prepare=prepare, case=case)
    assert len(prepared) == 2
    assert len(calls) == 68  # Two controls, six warm starts, sixty timed starts.
    assert len(receipt["warmups"]) == 6
    timed = events[2:]
    for repeat in range(3):
        block = timed[repeat * 62:(repeat + 1) * 62]
        assert [cell[0] for cell in block[:2]] == ["oracle", "candidate"]
        assert block[2] == "timer"
        warmed = {cell[0]: cell for cell in block[:2]}
        assert all(cell == warmed[cell[0]] for cell in block[2:] if cell != "timer")
    assert all(row["warmed_file_identity_matches"] for rows in receipt["runs"].values() for row in rows)


def test_same_byte_image_rewrite_during_warmup_is_rejected(tmp_path, monkeypatch):
    import os
    args, case, prepare, _, calls = installed_instrument(tmp_path, monkeypatch)

    def observed(command, argv, **kwargs):
        calls.append(argv)
        if len(calls) == 3:
            path = Path(command[0])
            metadata = path.stat()
            path.write_bytes(path.read_bytes())
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000))
        return {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(RuntimeError, match="warmed executable file identity changed"):
        cli_measurement.measure(args, {}, prepare=prepare, case=case)
    assert len(calls) == 3


@pytest.mark.parametrize("opt_in", [False, True])
def test_help_retains_historical_resets_until_warm_images_opt_in(tmp_path, monkeypatch, opt_in):
    args, calls = instrument(tmp_path, monkeypatch)
    args.mode, args.argv_json, args.warm_images = "help", None, opt_in
    receipt = cli_measurement.measure(args, {})
    assert len(calls) == (68 if opt_in else 62)
    assert len(receipt["warmups"]) == (6 if opt_in else 0)
    assert all(("warmed_file_identity" in row) == opt_in for rows in receipt["runs"].values() for row in rows)


@pytest.mark.parametrize("mode", ["lease", "episode"])
def test_mutating_mode_retains_per_invocation_preparation_and_state_reset(tmp_path, monkeypatch, mode):
    import json
    args, case, prepare, prepared, calls = installed_instrument(tmp_path, monkeypatch)
    args.mode, args.argv_json = mode, json.dumps([mode])
    case["mode"], case["argv"] = mode, [mode]

    def observed(command, argv, **kwargs):
        home = Path(kwargs["env"]["HOME"])
        assert not (home / "dirty").exists()
        (home / "dirty").write_bytes(b"mutable fixture state")
        calls.append(argv)
        return {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    receipt = cli_measurement.measure(args, {}, prepare=prepare, case=case)
    assert len(prepared) == len(calls) == 62
    assert receipt["warmups"] == []
