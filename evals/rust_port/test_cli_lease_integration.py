"""Version preparation and lease lock contexts retain one capture boundary."""
from contextlib import contextmanager
import copy
from pathlib import Path
import sys

import pytest

from evals.rust_port import cli_process, cli_version, lease_corpus, provenance
from evals.rust_port.test_cli_public_summary import receipt


def combined_receipt():
    raw, private_path, encoded = receipt()
    original_record = raw["records"][0]
    original_controls = raw["candidate_output_controls"][:4]
    for case in lease_corpus.cases():
        record = copy.deepcopy(original_record)
        record.update(id=case["id"], mode=case["mode"])
        for arm in ("oracle", "candidate"):
            record[arm]["request"] = copy.deepcopy(case)
        raw["records"].append(record)
        for original in original_controls:
            control = copy.deepcopy(original)
            control.update(case=case["id"], mode=case["mode"])
            raw["candidate_output_controls"].append(control)
    raw["coverage"] = {"modes": sorted({row["mode"] for row in raw["records"]})}
    return raw, private_path, encoded


def test_complete_lease_projection_omits_private_child_argv_and_preserves_help_version():
    import json
    raw, private_path, encoded = combined_receipt()
    original, _, _ = receipt()
    original_summary = cli_process.public_summary(original)
    before = copy.deepcopy(raw)
    summary = cli_process.public_summary(raw)
    assert raw == before
    assert summary["cases"][:len(original["records"])] == original_summary["cases"]
    assert len(summary["cases"]) == len(raw["records"])
    assert len(summary["candidate_controls"]) == 4 * len(raw["records"])
    lease_rows = [row for row in summary["cases"] if row["mode"].startswith("lease-")]
    assert len(lease_rows) == len(lease_corpus.cases())
    assert all("argv" not in row for row in lease_rows)
    serialized = json.dumps(summary)
    assert private_path not in serialized and encoded not in serialized and sys.executable not in serialized


@pytest.mark.parametrize("missing", ["one-case", "all-lease-cases", "control", "changed-request"])
def test_incomplete_or_changed_lease_projection_is_refused(missing):
    raw, _, _ = combined_receipt()
    if missing == "one-case":
        raw["records"].pop()
    elif missing == "all-lease-cases":
        raw["records"] = [row for row in raw["records"] if not row["mode"].startswith("lease-")]
        raw["candidate_output_controls"] = [row for row in raw["candidate_output_controls"]
                                            if not row["mode"].startswith("lease-")]
    elif missing == "control":
        raw["candidate_output_controls"].pop()
    else:
        raw["records"][-1]["candidate"]["request"]["argv"].append("changed")
    with pytest.raises(ValueError):
        cli_process.public_summary(raw)


def test_binding_retains_actual_lease_case_and_capture_helpers(monkeypatch):
    maps = []
    monkeypatch.setattr(cli_process, "require_instrument_binding",
                        lambda root, helpers: maps.append(helpers) or {})
    cli_process.cli_binding(provenance.ROOT, provenance.ROOT)
    instrument = maps[0]
    assert lease_corpus.cases in instrument["evals/rust_port/lease_corpus.py"]
    for actual in (cli_process.observe, cli_process.paired_cases, cli_process.run,
                   cli_process.fixture_env, cli_process.file_path, cli_process.checked_files,
                   cli_process.byte_payload, cli_process.candidate_controls):
        assert actual in instrument["evals/rust_port/cli_process.py"]


def test_binding_retains_actual_preparer_and_context_generator(monkeypatch):
    maps = []
    monkeypatch.setattr(cli_process, "require_instrument_binding",
                        lambda root, helpers: maps.append(helpers) or {})

    def prepare(*args):
        return args[3]

    @contextmanager
    def scope(*args):
        yield

    cli_process.cli_binding(provenance.ROOT, provenance.ROOT, callbacks=(prepare, scope))
    loaded = maps[0]["evals/rust_port/test_cli_lease_integration.py"]
    assert prepare in loaded
    assert scope.__wrapped__ in loaded


def test_foreign_context_source_refuses_binding(tmp_path, monkeypatch):
    foreign = tmp_path / "foreign.py"
    foreign.write_text("def scope(*args):\n    yield\n")
    namespace = {}
    exec(compile(foreign.read_text(), str(foreign), "exec"), namespace)
    monkeypatch.setattr(cli_process, "require_instrument_binding", lambda *args: {})
    with pytest.raises(RuntimeError, match="another instrument tree"):
        cli_process.cli_binding(provenance.ROOT, provenance.ROOT, callbacks=(namespace["scope"],))


@pytest.mark.parametrize("field", ["source_files_sha256", "source_files_git_blob"])
def test_public_projection_requires_the_lease_case_identity(field):
    raw, _, _ = receipt()
    raw["cli_instrument_binding"]["instrument"][field].pop("evals/rust_port/lease_corpus.py")
    with pytest.raises(ValueError):
        cli_process.public_summary(raw)


def test_public_projection_cannot_omit_an_extra_bound_callback():
    raw, _, _ = receipt()
    binding = raw["cli_instrument_binding"]["instrument"]
    binding["source_files_sha256"]["evals/custom_capture.py"] = "a" * 64
    binding["source_files_git_blob"]["evals/custom_capture.py"] = "b" * 40
    with pytest.raises(ValueError):
        cli_process.public_summary(raw)


@pytest.mark.parametrize("mutation", [None, "PYTHONPATH", "PSEUDOLIFE_SHIM_RUNTIMES",
                                      "PSEUDOLIFE_SHIM_LAUNCHER", "pythonpath"])
def test_verified_version_preparation_is_frozen_before_context(tmp_path, monkeypatch, mutation):
    verified = str(tmp_path / "verified-source")
    monkeypatch.setattr(cli_version, "seed_context", lambda root: {"pythonpath": verified})
    saved, calls = {}, []

    def prepare(_case, home, env, prefix, _commands):
        env.update(PYTHONPATH=verified, PSEUDOLIFE_SHIM_RUNTIMES=str(home / "runtimes"),
                   PSEUDOLIFE_SHIM_LAUNCHER=str(home / "launcher"))
        saved["env"] = env
        return prefix

    @contextmanager
    def scope(_case, _home):
        if mutation:
            saved["env"][mutation] = "changed-after-verified-prepare"
        yield

    def capture(*args, **kwargs):
        calls.append(dict(kwargs["env"]))
        return {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}

    monkeypatch.setattr(cli_process, "run_cli", capture)
    case = cli_version.cases()[0]
    options = dict(root=tmp_path, home=tmp_path / "home", url="http://127.0.0.1:49152",
                   prepare=prepare, process_scope=scope)
    if mutation:
        with pytest.raises(ValueError, match="environment"):
            cli_process.observe(case, [sys.executable], {"oracle": [sys.executable]}, **options)
        assert calls == []
    else:
        observation = cli_process.observe(case, [sys.executable], {"oracle": [sys.executable]}, **options)
        assert calls == [observation["environment"]]
        assert calls[0]["PYTHONPATH"] == verified
        assert Path(calls[0]["PSEUDOLIFE_SHIM_RUNTIMES"]).is_relative_to(tmp_path / "home")
        assert Path(calls[0]["PSEUDOLIFE_SHIM_LAUNCHER"]).is_relative_to(tmp_path / "home")
