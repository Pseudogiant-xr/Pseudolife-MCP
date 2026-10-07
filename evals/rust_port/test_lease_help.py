"""Every committed lease help asset must match Python's pinned parser at 80 columns."""
from pathlib import Path
import subprocess

import pytest

from evals.rust_port import lease_help


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def python_help(tmp_path_factory):
    return lease_help.pinned_help(ROOT, tmp_path_factory.mktemp("lease-help-oracle"))


def test_every_committed_lease_asset_has_a_python_help_guard():
    paths = subprocess.check_output([
        "git", "ls-tree", "-r", "--name-only", "HEAD", lease_help.ASSET_DIRECTORY],
        cwd=ROOT, text=True).splitlines()
    assert paths == sorted(f"{lease_help.ASSET_DIRECTORY}/{name}"
                           for name in lease_help.ASSETS)


@pytest.mark.parametrize("asset", lease_help.ASSETS)
def test_committed_lease_help_matches_pinned_python_parser(asset, python_help):
    relative = f"{lease_help.ASSET_DIRECTORY}/{asset}"
    actual = lease_help.committed_bytes(ROOT, "HEAD", relative).decode("utf-8")
    assert actual == python_help[asset], (
        f"{relative} differs from Python {lease_help.ORACLE_HEAD} with COLUMNS=80")
