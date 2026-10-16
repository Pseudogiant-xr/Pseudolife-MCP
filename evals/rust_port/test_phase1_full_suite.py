"""Mixed-suite routing keeps unmapped assertions active and strict mode intact."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evals.rust_port import pytest_plugin
from evals.rust_port.harness import isolated_env


def session_for(nodes, *, stdio=False, cli=False, mixed=True):
    config = SimpleNamespace(
        getoption=lambda name: mixed,
        pluginmanager=SimpleNamespace(getplugin=lambda name: None),
    )
    if stdio:
        config._port_stdio_prefix = ["candidate"]
    if cli:
        config._port_cli_prefix = ["candidate"]
    return SimpleNamespace(config=config, items=[SimpleNamespace(nodeid=n) for n in nodes])


@pytest.mark.parametrize("stdio,cli", [(False, False), (False, True), (True, True)])
def test_mixed_suite_requires_only_stdio_selection(stdio, cli):
    with pytest.raises(pytest.UsageError, match="requires"):
        pytest_plugin.pytest_collection_finish(session_for([], stdio=stdio, cli=cli))


def test_mixed_suite_refuses_zero_candidate_nodes():
    with pytest.raises(pytest.UsageError, match="no mapped stdio"):
        pytest_plugin.pytest_collection_finish(session_for(["unmapped"], stdio=True))


def test_mixed_suite_retains_every_collected_item():
    mapped = next(n for n, boundary in pytest_plugin.MANIFEST["mapped"].items()
                  if boundary == "stdio-shim-process")
    session = session_for([mapped, "unmapped"], stdio=True)
    before = list(session.items)
    pytest_plugin.pytest_collection_finish(session)
    assert session.items == before


def test_mixed_suite_executes_candidate_boundary_and_unmapped_failure(tmp_path):
    (tmp_path / "conftest.py").write_text("""
from contextlib import asynccontextmanager
import mcp.client.stdio
from evals.rust_port import pytest_plugin

def pytest_configure(config):
    @asynccontextmanager
    async def original(server, *args, **kwargs):
        yield server.command
    mcp.client.stdio.stdio_client = original

def pytest_collection_modifyitems(items):
    for item in items:
        if item.name == 'test_candidate':
            pytest_plugin.MANIFEST['mapped'][item.nodeid] = 'stdio-shim-process'
""")
    source = tmp_path / "test_mixed.py"
    source.write_text("""
import asyncio
import sys
from mcp import StdioServerParameters
import mcp.client.stdio

def test_candidate():
    async def run():
        server = StdioServerParameters(command=sys.executable, args=['-m', 'pseudolife_memory.cli', 'shim'])
        async with mcp.client.stdio.stdio_client(server) as selected:
            assert selected == 'fixture-rust-candidate'
    asyncio.run(run())

def test_python_oracle():
    raise AssertionError('oracle assertion still executes')
""")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "evals.rust_port.pytest_plugin",
         str(source), "--port-stdio-json", json.dumps(["fixture-rust-candidate"]),
         "--port-full-suite", "--confcutdir", str(tmp_path), "-q"],
        cwd=Path(__file__).resolve().parents[2], env=isolated_env(tmp_path / "home"),
        capture_output=True, timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert b"1 failed, 1 passed" in result.stdout
    assert b"oracle assertion still executes" in result.stdout
    assert b"Rust shim routing: 1 candidate nodes; 1 Python oracle nodes" in result.stdout
