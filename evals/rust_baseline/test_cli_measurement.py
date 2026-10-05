"""Selected CLI mode and exact byte gate remain load-bearing for timing."""
from types import SimpleNamespace

import pytest

from evals.rust_baseline import cli_measurement


def instrument(tmp_path, monkeypatch, *, mismatch=False):
    args = SimpleNamespace(mode="version", argv_json='["--version"]', oracle_root=tmp_path,
                           candidate_root=tmp_path, candidate=tmp_path / "candidate",
                           candidate_sha256=None, repeats=3, samples=10, smoke=False,
                           board_checked_at=None, offline_resource_checked_at="fixture")
    monkeypatch.setattr(cli_measurement, "require_phase1_source", lambda root: {}, raising=False)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.17.0"\n')
    monkeypatch.setattr(cli_measurement, "require_import_root", lambda root: None)
    monkeypatch.setattr(cli_measurement, "runtime_metadata", lambda root: {
        "source_origin_matches_selected_root": True, "package_runtime_version": "0.17.0",
        "distribution_versions": {"pseudolife-mcp": "0.17.0"}})
    monkeypatch.setattr(cli_measurement, "candidate_identity", lambda *args: {"executable_bytes": 1})
    monkeypatch.setattr(cli_measurement, "command_identity", lambda *args: {"executable_bytes": 2})
    monkeypatch.setattr(cli_measurement, "artifact_identity", lambda *args: {})
    monkeypatch.setattr(cli_measurement, "provenance", lambda **kwargs: {})
    monkeypatch.setattr(cli_measurement, "cli_binding", lambda *args, **kwargs: {})
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


def installed_instrument(tmp_path, monkeypatch):
    args, calls = instrument(tmp_path, monkeypatch)
    oracle = tmp_path / "python"
    oracle.write_bytes(b"python fixture")
    args.candidate.write_bytes(b"native fixture")
    monkeypatch.setattr(cli_measurement.sys, "executable", str(oracle))
    from evals.rust_port.phase1_receipts import command_identity
    monkeypatch.setattr(cli_measurement, "command_identity", command_identity)
    monkeypatch.setattr(cli_measurement, "candidate_identity", command_identity)
    case = {"id": "version-installed", "mode": "version", "argv": ["--version"],
            "stdin_b64": "", "environment_deltas": {}, "pre_files_b64": {}, "normalizations": []}
    prepared = []

    def prepare(case, home, env, command, commands):
        import shutil
        prepared.append((home, dict(env), dict(commands)))
        for arm, prefix in commands.items():
            target = home / arm
            shutil.copy2(prefix[0], target)
        return [str(home / ("oracle" if command == commands["oracle"] else "candidate")), *command[1:]]

    return args, case, prepare, prepared, calls


def test_installed_measurement_resets_one_home_before_timer_and_retains_binding(tmp_path, monkeypatch):
    args, case, prepare, prepared, calls = installed_instrument(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: started.append(len(prepared)) or 1.0)

    def observed(command, argv, **kwargs):
        home = Path(kwargs["env"]["HOME"])
        assert not (home / "dirty").exists()
        (home / "dirty").write_bytes(b"same response state")
        calls.append(argv)
        import base64
        return {"exit_code": 0, "stdout_b64": base64.b64encode(str(home).encode()).decode(), "stderr_b64": ""}

    from pathlib import Path
    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    receipt = cli_measurement.measure(args, {}, prepare=prepare, case=case)
    assert len(prepared) == len(calls) == 62
    assert len({home for home, _, _ in prepared}) == 1
    assert started[::2] == list(range(3, 63))
    assert all(env["CUDA_VISIBLE_DEVICES"] == "-1" and env["PSEUDOLIFE_MCP_NO_SPAWN"] == "1"
               for _, env, _ in prepared)
    assert all(set(commands) == {"oracle", "candidate"} for _, _, commands in prepared)
    assert receipt["prepared_case"] == case
    assert all(len(rows) == 30 for rows in receipt["runs"].values())
    assert all("execution" in row for rows in receipt["runs"].values() for row in rows)


@pytest.mark.parametrize("mutation", ["bytes", "tail", "cpu", "spawn"])
def test_installed_measurement_refuses_changed_prepared_binding_before_launch(tmp_path, monkeypatch, mutation):
    args, case, prepare, prepared, calls = installed_instrument(tmp_path, monkeypatch)

    def changed(*arguments):
        command = prepare(*arguments)
        if mutation == "bytes":
            from pathlib import Path
            Path(command[0]).write_bytes(b"substituted")
        elif mutation == "tail":
            command.append("substituted")
        else:
            arguments[2]["CUDA_VISIBLE_DEVICES" if mutation == "cpu" else "PSEUDOLIFE_MCP_NO_SPAWN"] = "0"
        return command

    monkeypatch.setattr(cli_measurement, "run_cli", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError):
        cli_measurement.measure(args, {}, prepare=changed, case=case)
    assert not calls


def test_installed_control_file_difference_refuses_before_timing(tmp_path, monkeypatch):
    args, case, prepare, prepared, calls = installed_instrument(tmp_path, monkeypatch)

    def observed(command, argv, **kwargs):
        from pathlib import Path
        home = Path(kwargs["env"]["HOME"])
        (home / "state").write_bytes(b"candidate" if len(command) == 1 else b"oracle")
        calls.append(argv)
        return {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(RuntimeError, match="byte control failed"):
        cli_measurement.measure(args, {}, prepare=prepare, case=case)
    assert len(calls) == 2


def test_installed_layout_selects_public_installer_case_without_private_adapter(tmp_path, monkeypatch):
    args, _, prepare, prepared, calls = installed_instrument(tmp_path, monkeypatch)
    args.layout = "installed"
    monkeypatch.setattr(cli_measurement, "require_phase1_source", lambda root: {"oracle_head": "f" * 40}, raising=False)
    from evals.rust_port import cli_version
    selected = []
    monkeypatch.setattr(cli_version, "make_prepare", lambda root, pin: selected.append((root, pin)) or prepare)
    receipt = cli_measurement.measure(args, {})
    assert selected == [(tmp_path, "f" * 40)]
    assert receipt["prepared_case"]["layout_kind"] == "default"
    assert receipt["prepared_case"]["argv"] == ["--version"]
    assert len(calls) == len(prepared) == 62


def test_installed_output_change_during_sampling_is_rejected(tmp_path, monkeypatch):
    args, case, prepare, prepared, calls = installed_instrument(tmp_path, monkeypatch)

    def changed(command, argv, **kwargs):
        calls.append(argv)
        return {"exit_code": 0, "stdout_b64": "" if len(calls) <= 2 else "YQ==", "stderr_b64": ""}

    monkeypatch.setattr(cli_measurement, "run_cli", changed)
    with pytest.raises(RuntimeError, match="bytes changed during measurement"):
        cli_measurement.measure(args, {}, prepare=prepare, case=case)
    assert len(calls) == 3


def test_measurement_binds_loaded_helpers_before_launch_and_after_capture(tmp_path, monkeypatch):
    args, calls = instrument(tmp_path, monkeypatch)
    phases = []
    monkeypatch.setattr(cli_measurement, "cli_binding", lambda *a, **k: phases.append(len(calls)) or {"fixture": True},
                        raising=False)
    receipt = cli_measurement.measure(args, {})
    assert phases == [0, 62]
    assert receipt["cli_instrument_binding"] == {"fixture": True}


def test_changed_helper_binding_refuses_final_measurement_receipt(tmp_path, monkeypatch):
    args, calls = instrument(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_measurement, "cli_binding", lambda *a, **k: {"fixture": len(calls)}, raising=False)
    with pytest.raises(RuntimeError, match="instrument.*changed"):
        cli_measurement.measure(args, {})


def test_missing_helper_refuses_before_any_process_launch(tmp_path, monkeypatch):
    args, calls = instrument(tmp_path, monkeypatch)

    def missing(*args, **kwargs):
        raise RuntimeError("CLI instrument loaded helper unavailable")

    monkeypatch.setattr(cli_measurement, "cli_binding", missing)
    with pytest.raises(RuntimeError, match="helper unavailable"):
        cli_measurement.measure(args, {})
    assert calls == []


def test_selected_phase1_guard_and_dynamic_version_bind_before_and_after_capture(tmp_path, monkeypatch):
    args, calls = instrument(tmp_path, monkeypatch)
    phases = []
    monkeypatch.setattr(cli_measurement, "require_phase1_source",
                        lambda root: phases.append(len(calls)) or {"oracle_head": "f" * 40}, raising=False)
    receipt = cli_measurement.measure(args, {})
    assert phases == [0, 62]
    assert receipt["python_oracle"] == {"oracle_head": "f" * 40}
    assert receipt["capture_runtime"]["package_runtime_version"] == "0.17.0"


def test_old_installed_package_cannot_match_new_selected_source(tmp_path, monkeypatch):
    args, calls = instrument(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_measurement, "runtime_metadata", lambda root: {
        "source_origin_matches_selected_root": True, "package_runtime_version": "0.16.1",
        "distribution_versions": {"pseudolife-mcp": "0.16.1"}})
    with pytest.raises(RuntimeError, match="genuine pinned installed package"):
        cli_measurement.measure(args, {})
    assert calls == []


def test_selected_source_guard_refuses_before_launch(tmp_path, monkeypatch):
    args, calls = instrument(tmp_path, monkeypatch)

    def changed(root):
        raise RuntimeError("selected phase1 source changed")

    monkeypatch.setattr(cli_measurement, "require_phase1_source", changed, raising=False)
    with pytest.raises(RuntimeError, match="selected phase1 source changed"):
        cli_measurement.measure(args, {})
    assert calls == []


@pytest.mark.parametrize("key", ["PSEUDOLIFE_MCP_TOKEN_FILE", "pseudolife_lease_lock_dir", "PYTHONPATH"])
def test_prepared_measurement_refuses_uncaptured_file_source_before_timing(tmp_path, monkeypatch, key):
    args, case, prepare, _, calls = installed_instrument(tmp_path, monkeypatch)
    from evals.rust_port import cli_version
    monkeypatch.setattr(cli_version, "seed_context",
                        lambda root: {"pythonpath": str(tmp_path / "verified-source")})
    outside = tmp_path / "sibling"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")

    def redirect(*arguments):
        command = prepare(*arguments)
        arguments[2][key] = str(outside)
        return command

    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(ValueError):
        cli_measurement.measure(args, {}, prepare=redirect, case=case)
    assert calls == []
    assert sentinel.read_bytes() == b"unchanged"
