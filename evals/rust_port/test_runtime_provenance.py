"""Runtime provenance comes from the selected child's environment."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

from evals.rust_port import provenance
from evals.rust_port.harness import isolated_env


def source_tree(root):
    package = root / "pseudolife_memory"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from importlib.metadata import version\n__version__=version('pseudolife-mcp')\n")
    stale = root / "evals" / "rust_port"
    stale.mkdir(parents=True)
    (root / "evals" / "__init__.py").write_text("")
    (stale / "__init__.py").write_text("")
    (stale / "provenance.py").write_text("raise SystemExit(91)\n")
    return package / "__init__.py"


def test_actual_child_runtime_uses_current_probe_and_selected_source(tmp_path):
    source = source_tree(tmp_path)
    result = subprocess.run(provenance.runtime_probe_command(tmp_path), cwd=tmp_path,
                            env=isolated_env(tmp_path / "home"), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    runtime = json.loads(result.stdout)
    assert runtime["source_origin_matches_selected_root"] is True
    assert runtime["source_file"] == "pseudolife_memory/__init__.py"
    assert runtime["source_file_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert runtime["executable_basename"] == Path(sys.executable).name
    assert runtime["executable_sha256"] == hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    assert runtime["distribution_versions"]["mcp"] == importlib.metadata.version("mcp")
    assert runtime["package_runtime_version"] == runtime["distribution_versions"]["pseudolife-mcp"]
    assert str(tmp_path) not in result.stdout
    assert str(Path.home()) not in result.stdout


def test_isolated_appdata_child_metadata_drift_cannot_be_labeled_as_parent(tmp_path):
    source_tree(tmp_path)
    # The selected source root is first on the child's import path. Keep its
    # synthetic distribution metadata there too, ahead of editable-checkout
    # metadata in the instrument root on every platform.
    metadata = tmp_path / "pseudolife_mcp-0.7.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: pseudolife-mcp\nVersion: 0.7.0\n")
    env = isolated_env(tmp_path / "isolated-appdata")
    # Synthetic fallback metadata mirrors the stale base installation exposed
    # when APPDATA isolation hides a newer user-site distribution.
    result = subprocess.run(provenance.runtime_probe_command(tmp_path), cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    runtime = json.loads(result.stdout)
    assert importlib.metadata.version("pseudolife-mcp") != "0.7.0"
    assert runtime["distribution_versions"]["pseudolife-mcp"] == "0.7.0"
    assert runtime["package_runtime_version"] == "0.7.0"
    assert runtime["source_origin_matches_selected_root"] is True


def test_runtime_observation_exposes_source_mismatch_as_a_boolean(tmp_path, monkeypatch):
    import pseudolife_memory
    runtime = provenance.runtime_metadata(tmp_path)
    assert runtime["source_origin_matches_selected_root"] is False
    assert runtime["source_file"] is None
    assert str(Path(pseudolife_memory.__file__).parent) not in json.dumps(runtime)
