"""Selected CLI mode and exact byte gate remain load-bearing for timing."""
from types import SimpleNamespace

import pytest

from evals.rust_baseline import cli_measurement


def instrument(tmp_path, monkeypatch, *, mismatch=False):
    args = SimpleNamespace(mode="version", argv_json='["--version"]', oracle_root=tmp_path,
                           candidate_root=tmp_path, candidate=tmp_path / "candidate",
                           candidate_sha256=None, repeats=3, samples=10, smoke=False,
                           board_checked_at=None, offline_resource_checked_at="fixture")
    monkeypatch.setattr(cli_measurement, "require_pinned_source", lambda root: {})
    monkeypatch.setattr(cli_measurement, "require_import_root", lambda root: None)
    monkeypatch.setattr(cli_measurement, "runtime_metadata", lambda root: {
        "source_origin_matches_selected_root": True, "package_runtime_version": "0.16.1",
        "distribution_versions": {"pseudolife-mcp": "0.16.1"}})
    monkeypatch.setattr(cli_measurement, "candidate_identity", lambda *args: {"executable_bytes": 1})
    monkeypatch.setattr(cli_measurement, "command_identity", lambda *args: {"executable_bytes": 2})
    monkeypatch.setattr(cli_measurement, "artifact_identity", lambda *args: {})
    monkeypatch.setattr(cli_measurement, "provenance", lambda **kwargs: {})
    monkeypatch.setattr(cli_measurement, "lease_gate", lambda *args, **kwargs: {})
    calls = []

    def observed(command, argv, **kwargs):
        calls.append(argv)
        return {"exit_code": 0, "stdout_b64": "YQ==" if mismatch and len(command) == 1 else "",
                "stderr_b64": ""}

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    return args, calls


def test_version_measurement_uses_selected_arguments_and_existing_repeat_floors(tmp_path, monkeypatch):
    args, calls = instrument(tmp_path, monkeypatch)
    receipt = cli_measurement.measure(args, {})
    assert len(calls) == 62
    assert all(argv == ["--version"] for argv in calls)
    assert receipt["mode"] == "version"
    assert receipt["argv"] == ["--version"]
    assert all(len(rows) == 30 for rows in receipt["runs"].values())
    assert all(receipt["metrics"][arm]["cold_start_to_exit_ms"]["p50"]["noise_floor_abs"] is not None
               for arm in ("python", "rust"))


def test_mismatched_version_bytes_refuse_before_any_timed_samples(tmp_path, monkeypatch):
    args, calls = instrument(tmp_path, monkeypatch, mismatch=True)
    with pytest.raises(RuntimeError, match="byte control failed"):
        cli_measurement.measure(args, {})
    assert len(calls) == 2
