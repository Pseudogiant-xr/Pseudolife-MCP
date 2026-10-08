"""Oracle preparation validates real metadata against the selected source."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.rust_port import phase1_oracle


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    state = SimpleNamespace(version="0.17.0", runtime_version="0.17.0",
                            distribution_version="0.17.0", source_matches=True, commands=[])
    site = tmp_path / "site-packages"
    site.mkdir()

    def run(command, **kwargs):
        state.commands.append(command)
        if command[:2] == ["git", "clone"]:
            source = Path(command[-1])
            source.mkdir()
            (source / "pyproject.toml").write_text(
                f'[project]\nversion = "{state.version}"\n', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    def checked_source(source):
        assert source == tmp_path / "prepared" / "source"
        assert (source / "pyproject.toml").is_file()
        return {"oracle_head": phase1_oracle.ORACLE_HEAD, "production_source_matches_pin": True}

    def check_output(command, **kwargs):
        if "sysconfig" in command[-1]:
            return str(site) + "\n"
        assert kwargs["cwd"] == tmp_path / "prepared" / "source"
        return json.dumps({
            "source_origin_matches_selected_root": state.source_matches,
            "package_runtime_version": state.runtime_version,
            "distribution_versions": {"pseudolife-mcp": state.distribution_version}})

    monkeypatch.setattr(phase1_oracle.subprocess, "run", run)
    monkeypatch.setattr(phase1_oracle.subprocess, "check_output", check_output)
    monkeypatch.setattr(phase1_oracle, "require_phase1_source", checked_source)
    monkeypatch.setattr(phase1_oracle, "selected_oracle", lambda: {
        "event": "pull_request", "oracle_head": phase1_oracle.ORACLE_HEAD})
    monkeypatch.setattr(phase1_oracle.shutil, "copytree", lambda *args, **kwargs: None)
    state.destination = tmp_path / "prepared"
    return state


@pytest.mark.parametrize("version", ["0.17.0", "0.18.0"])
def test_preparation_accepts_the_selected_source_version(preparation, version):
    preparation.version = preparation.runtime_version = preparation.distribution_version = version
    result = phase1_oracle.prepare(preparation.destination)
    assert result["runtime"]["package_runtime_version"] == version
    assert result["runtime"]["distribution_versions"]["pseudolife-mcp"] == version
    assert result["production_source_matches_pin"] is True
    assert ["git", "checkout", "--quiet", "--detach", phase1_oracle.ORACLE_HEAD] \
        in preparation.commands
    install = next(command for command in preparation.commands if "install" in command)
    assert install[1:] == ["-m", "pip", "install", "--no-index", "--no-deps",
                           "--no-build-isolation", "-e", result["source"]]


@pytest.mark.parametrize("runtime,distribution", [
    ("0.16.1", "0.16.1"), ("0.17.0", "0.16.1"), ("0.16.1", "0.17.0")])
def test_preparation_rejects_metadata_from_a_different_release(preparation, runtime, distribution):
    preparation.runtime_version = runtime
    preparation.distribution_version = distribution
    with pytest.raises(RuntimeError, match="selected checkout/version"):
        phase1_oracle.prepare(preparation.destination)


def test_preparation_rejects_an_import_from_another_checkout(preparation):
    preparation.source_matches = False
    with pytest.raises(RuntimeError, match="selected checkout/version"):
        phase1_oracle.prepare(preparation.destination)
