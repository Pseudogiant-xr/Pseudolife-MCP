"""CPU-light checks for paired shim receipts and source identity refusal."""
import json
from pathlib import Path

import pytest

from .shim_measurement import artifact_identity, metric_cells, require_pinned_source


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
