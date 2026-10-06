"""CPU-light checks for paired shim receipts and source identity refusal."""
import json
import hashlib
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from .shim_measurement import artifact_identity, metric_cells, require_pinned_source


def test_paired_history_binding_uses_the_pinned_git_blob_without_a_measurement(monkeypatch):
    from . import common, shim_measurement
    root = Path(__file__).resolve().parents[2]
    artifact = "evals/results/rust-rewrite-baseline-shim-20261003-r5.json"
    blob = subprocess.check_output(["git", "rev-parse", f"{shim_measurement.ORACLE_HEAD}:{artifact}"],
                                   cwd=root, text=True).strip()
    canonical = subprocess.check_output(["git", "cat-file", "blob", blob], cwd=root)
    monkeypatch.setattr(shim_measurement, "require_pinned_source", lambda root: {})
    monkeypatch.setattr(shim_measurement, "candidate_source_identity", lambda root: {})
    monkeypatch.setattr(shim_measurement, "artifact_identity", lambda *args: {"sha256": "synthetic", "bytes": 1})
    monkeypatch.setattr(shim_measurement, "metric_cells", lambda rows: {})
    monkeypatch.setattr(common, "provenance", lambda **kwargs: {})
    args = SimpleNamespace(source_root=root, candidate=root / "unused", candidate_root=root,
                           candidate_sha256=None, repeats=0, samples=0, smoke=False,
                           measurement_status="final")
    result = shim_measurement.measure_pair(args, {})
    assert result["runs"] == {"python": [], "rust": []}
    assert result["historical_method"]["sha256"] == hashlib.sha256(canonical).hexdigest()
    assert result["historical_method"]["git_blob_oid"] == blob


def test_cells_report_separate_quantile_floors():
    runs = [{"repeat": repeat, "first_frame_ms": value, "startup_ms": value + 1,
             "peak_rss_bytes": value * 100, "executable_bytes": 123}
            for repeat, values in enumerate(([1, 2, 3], [4, 5, 6], [7, 8, 9]))
            for value in values]
    cells = metric_cells(runs)
    assert cells["first_frame_ms"]["p50"]["noise_floor_abs"] == 6
    assert cells["first_frame_ms"]["p95"]["noise_floor_abs"] == 6
    assert cells["peak_rss_bytes"]["p50"]["noise_floor_abs"] == 600
    assert cells["executable_bytes"]["p50"]["noise_floor_abs"] == 0
    assert metric_cells(runs[:3])["first_frame_ms"]["p50"]["noise_floor_abs"] is None


def test_artifact_identity_is_path_free_and_checks_expected_hash(tmp_path):
    binary = tmp_path / "candidate"
    binary.write_bytes(b"synthetic executable")
    identity = artifact_identity(binary)
    assert identity["bytes"] == len(b"synthetic executable")
    assert str(tmp_path) not in json.dumps(identity)
    assert artifact_identity(binary, identity["sha256"]) == identity
    with pytest.raises(RuntimeError, match="identity"):
        artifact_identity(binary, "0" * 64)


def test_changed_checkout_cannot_be_used_as_pinned_oracle(tmp_path):
    # A directory without the pinned git history must fail before any launch.
    with pytest.raises(RuntimeError, match="pinned"):
        require_pinned_source(Path(tmp_path))


@pytest.mark.parametrize("package_version,distribution_version,origin_matches,accepted", [
    ("0.16.1", "0.16.1", True, True),
    ("0.16.0", "0.16.1", True, False),
    ("0.16.1", "0.16.0", True, False),
    ("0.16.1", "0.16.1", False, False)])
def test_paired_measurement_requires_the_actual_pinned_runtime_before_any_launch(
        tmp_path, monkeypatch, package_version, distribution_version, origin_matches, accepted):
    from . import common, shim_measurement, transport
    from evals.rust_port import processes

    class FixtureReached(Exception):
        pass

    @contextmanager
    def probe(*args, **kwargs):
        class Child:
            returncode = 0
            def communicate(self, timeout):
                return json.dumps({"source_origin_matches_selected_root": origin_matches,
                    "package_runtime_version": package_version,
                    "distribution_versions": {"pseudolife-mcp": distribution_version}}).encode(), b""
        yield Child()

    def fixture():
        raise FixtureReached()

    monkeypatch.setattr(shim_measurement, "require_pinned_source", lambda root: {})
    monkeypatch.setattr(shim_measurement, "artifact_identity", lambda *args: {"sha256": "synthetic", "bytes": 1})
    monkeypatch.setattr(shim_measurement, "candidate_source_identity", lambda root: {})
    monkeypatch.setattr(common, "child_environment", lambda private: {})
    monkeypatch.setattr(shim_measurement, "historical_method", lambda root: {})
    monkeypatch.setattr(processes, "owned_process", probe)
    monkeypatch.setattr(transport, "shim_fixture", fixture)
    args = SimpleNamespace(source_root=tmp_path, candidate=tmp_path / "candidate", candidate_root=tmp_path,
                           candidate_sha256=None, repeats=1, samples=1, smoke=False)
    with pytest.raises(FixtureReached if accepted else RuntimeError,
                       match=None if accepted else "pinned oracle runtime/version"):
        shim_measurement.measure_pair(args, {})
