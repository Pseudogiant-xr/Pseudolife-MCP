"""Launch boundaries bind the oracle interpreter and retain deliberate overrides."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from types import SimpleNamespace

from mcp import StdioServerParameters
import mcp.client.stdio
import pytest

from evals.rust_port import pytest_plugin
from evals.rust_port.stdio import capture, shim_environment


def test_stdio_environment_binds_current_runtime_without_ambient_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_PYTHON", "ambient-runtime")
    env = shim_environment(tmp_path, "https://remote.invalid")
    assert env["PSEUDOLIFE_MCP_PYTHON"] == sys.executable
    assert Path(env["PSEUDOLIFE_MCP_PYTHON"]).is_absolute()
    assert env["PSEUDOLIFE_MCP_NO_SPAWN"] == "1"
    assert env["PSEUDOLIFE_MCP_DAEMON_URL"] == "https://remote.invalid"


@pytest.mark.parametrize("override", [None, "explicit-runtime", ""])
def test_stdio_capture_passes_selected_runtime_and_reclaims_child(tmp_path, override):
    code = ("import json,os,sys; sys.stdin.readline(); "
            "print(json.dumps({'id':1,'python':os.environ.get('PSEUDOLIFE_MCP_PYTHON'),"
            "'no_spawn':os.environ.get('PSEUDOLIFE_MCP_NO_SPAWN')}),flush=True)")
    def exercise(wire):
        wire.send({"id": 1})
        frame = wire.response(1)
        assert frame["python"] == (sys.executable if override is None else override)
        assert frame["no_spawn"] == "1"
    result = capture([sys.executable, "-c", code], cwd=tmp_path, home=tmp_path / "home",
                     url="https://remote.invalid", exercise=exercise,
                     env_extra={} if override is None else {"PSEUDOLIFE_MCP_PYTHON": override})
    assert result["exit_code"] == 0
    assert result["cleanup"]["process_stopped"]
    assert result["cleanup"]["subtree_stopped"]


@pytest.mark.parametrize("child_selector,parent_selector,expected", [
    (None, None, None),
    (None, "parent-runtime", "parent-runtime"),
    (None, "", ""),
    ("child-runtime", "parent-runtime", "child-runtime"),
    ("", "parent-runtime", ""),
])
def test_stdio_adapter_preserves_environment_and_selector_precedence(
        monkeypatch, child_selector, parent_selector, expected):
    if parent_selector is None:
        monkeypatch.delenv("PSEUDOLIFE_MCP_PYTHON", raising=False)
    else:
        monkeypatch.setenv("PSEUDOLIFE_MCP_PYTHON", parent_selector)
    @asynccontextmanager
    async def original(server, *args, **kwargs):
        yield server
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", original)
    node = next(node for node, boundary in pytest_plugin.MANIFEST["mapped"].items()
                if boundary == "stdio-shim-process")
    request = SimpleNamespace(config=SimpleNamespace(_port_stdio_prefix=["candidate", "fixed"]),
                              node=SimpleNamespace(nodeid=node),
                              getfixturevalue=lambda name: monkeypatch)
    pytest_plugin._port_selected_boundary.__wrapped__(request)
    env = {"PSEUDOLIFE_MCP_DAEMON_URL": "https://remote.invalid", "PSEUDOLIFE_MCP_NO_SPAWN": "1"}
    if child_selector is not None:
        env["PSEUDOLIFE_MCP_PYTHON"] = child_selector
    server = StdioServerParameters(command=sys.executable, args=["-m", "pseudolife_memory.cli", "shim"], env=env)
    async def run():
        async with mcp.client.stdio.stdio_client(server) as selected:
            assert selected.command == "candidate"
            assert selected.args == ["fixed", "shim"]
            assert selected.env == {**env, "PSEUDOLIFE_MCP_PYTHON": sys.executable if expected is None else expected}
    asyncio.run(run())
    assert server.env == env
    assert server.command == sys.executable


def test_stdio_adapter_handles_an_absent_child_environment(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_PYTHON", raising=False)
    @asynccontextmanager
    async def original(server, *args, **kwargs):
        yield server
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", original)
    node = next(node for node, boundary in pytest_plugin.MANIFEST["mapped"].items()
                if boundary == "stdio-shim-process")
    request = SimpleNamespace(config=SimpleNamespace(_port_stdio_prefix=["candidate"]),
                              node=SimpleNamespace(nodeid=node),
                              getfixturevalue=lambda name: monkeypatch)
    pytest_plugin._port_selected_boundary.__wrapped__(request)
    server = StdioServerParameters(command=sys.executable, args=["-m", "pseudolife_memory.cli"])
    async def run():
        async with mcp.client.stdio.stdio_client(server) as selected:
            assert selected.env == {"PSEUDOLIFE_MCP_PYTHON": sys.executable}
    asyncio.run(run())
    assert server.env is None


@pytest.mark.parametrize("selector", [None, "explicit-runtime", ""])
def test_cli_adapter_binds_runtime_without_disabling_explicit_override(tmp_path, monkeypatch, selector):
    if selector is None:
        monkeypatch.delenv("PSEUDOLIFE_MCP_PYTHON", raising=False)
    else:
        monkeypatch.setenv("PSEUDOLIFE_MCP_PYTHON", selector)
    module = SimpleNamespace(main=lambda: None)
    node = next(node for node, boundary in pytest_plugin.MANIFEST["mapped"].items()
                if boundary == "cli-main-process")
    def run_cli(prefix, argv, **kwargs):
        assert prefix == ["candidate"]
        assert kwargs["env"]["PSEUDOLIFE_MCP_PYTHON"] == (sys.executable if selector is None else selector)
        assert kwargs["env"]["HOME"] == str(tmp_path)
        return {"stdout_b64": "", "stderr_b64": "", "exit_code": 0}
    monkeypatch.setattr(pytest_plugin, "run_cli", run_cli)
    request = SimpleNamespace(config=SimpleNamespace(_port_cli_prefix=["candidate"]),
                              node=SimpleNamespace(nodeid=node, module=module),
                              getfixturevalue=lambda name: monkeypatch if name == "monkeypatch" else tmp_path)
    pytest_plugin._port_selected_boundary.__wrapped__(request)
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 0
