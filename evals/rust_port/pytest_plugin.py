"""External adapter: preserve selected assertions, run CLI main in a process."""
import base64
import json
from pathlib import Path
import sys

import pytest

from evals.rust_port.harness import isolated_env, run_cli

MANIFEST = json.loads(Path(__file__).with_name("oracle_tests.json").read_text(encoding="utf-8"))


def pytest_addoption(parser):
    parser.addoption("--port-cli-json", help="JSON argv prefix for the whole executable under test")


def pytest_configure(config):
    value = config.getoption("--port-cli-json")
    if value is None:
        return
    try:
        prefix = json.loads(value)
    except ValueError:
        raise pytest.UsageError("--port-cli-json must be a JSON string array") from None
    if not isinstance(prefix, list) or not prefix or not all(isinstance(p, str) for p in prefix):
        raise pytest.UsageError("--port-cli-json must be a nonempty JSON string array")
    config._port_cli_prefix = prefix


def pytest_collection_finish(session):
    if not hasattr(session.config, "_port_cli_prefix"):
        return
    unmapped = [item.nodeid for item in session.items if item.nodeid not in MANIFEST["mapped"]]
    if unmapped:
        # No silent skip/deselection: an explicit supported selection is required.
        raise pytest.UsageError("selected tests have no process adapter: " + ", ".join(unmapped))


@pytest.fixture(autouse=True)
def _port_selected_boundary(request):
    prefix = getattr(request.config, "_port_cli_prefix", None)
    if prefix is None:
        return
    if MANIFEST["mapped"].get(request.node.nodeid) != "cli-main-process":
        raise pytest.UsageError("selected boundary is not implemented")
    monkeypatch = request.getfixturevalue("monkeypatch")
    tmp_path = request.getfixturevalue("tmp_path")

    def main():
        result = run_cli(prefix, sys.argv[1:], cwd=Path.cwd(),
                         env=isolated_env(tmp_path), timeout=10)
        # capsys sees the candidate's streams; original test assertions and
        # expected SystemExit stay intact. No Python CLI implementation is run.
        sys.stdout.write(base64.b64decode(result["stdout_b64"]).decode("utf-8"))
        sys.stderr.write(base64.b64decode(result["stderr_b64"]).decode("utf-8"))
        raise SystemExit(result["exit_code"])

    monkeypatch.setattr(request.node.module, "main", main)
