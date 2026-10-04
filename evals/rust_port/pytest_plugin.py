"""External adapter: preserve selected assertions, run CLI main in a process."""
import base64
import json
from pathlib import Path
import sys
from contextlib import asynccontextmanager
import os

import pytest

from evals.rust_port.harness import isolated_env, run_cli

MANIFEST = json.loads(Path(__file__).with_name("oracle_tests.json").read_text(encoding="utf-8"))


def pytest_addoption(parser):
    parser.addoption("--port-cli-json", help="JSON argv prefix for the whole executable under test")
    parser.addoption("--port-stdio-json", help="JSON argv prefix for public shim process tests")
    parser.addoption("--port-full-suite", action="store_true",
                     help="Route mapped stdio tests and run all other tests against Python")


def pytest_configure(config):
    for option, attribute in (("--port-cli-json", "_port_cli_prefix"),
                              ("--port-stdio-json", "_port_stdio_prefix")):
        value = config.getoption(option)
        if value is None and option == "--port-stdio-json":
            value = os.environ.get("PSEUDOLIFE_PORT_STDIO_JSON")
        if value is None:
            continue
        try:
            prefix = json.loads(value)
        except ValueError:
            raise pytest.UsageError(option + " must be a JSON string array") from None
        if not isinstance(prefix, list) or not prefix or not all(isinstance(p, str) and p for p in prefix):
            raise pytest.UsageError(option + " must be a nonempty JSON string array")
        setattr(config, attribute, prefix)


def pytest_collection_finish(session):
    boundaries = set()
    if hasattr(session.config, "_port_cli_prefix"):
        boundaries.add("cli-main-process")
    if hasattr(session.config, "_port_stdio_prefix"):
        boundaries.add("stdio-shim-process")
    if not boundaries:
        if session.config.getoption("--port-full-suite"):
            raise pytest.UsageError("--port-full-suite requires --port-stdio-json")
        return
    if session.config.getoption("--port-full-suite"):
        if boundaries != {"stdio-shim-process"}:
            raise pytest.UsageError("--port-full-suite requires only --port-stdio-json")
        routed = sum(MANIFEST["mapped"].get(item.nodeid) == "stdio-shim-process"
                     for item in session.items)
        if not routed:
            raise pytest.UsageError("--port-full-suite selected no mapped stdio tests")
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"Rust shim routing: {routed} candidate nodes; "
                                f"{len(session.items) - routed} Python oracle nodes")
        return
    unmapped = [item.nodeid for item in session.items if MANIFEST["mapped"].get(item.nodeid) not in boundaries]
    if unmapped:
        # No silent skip/deselection: an explicit supported selection is required.
        raise pytest.UsageError("selected tests have no process adapter: " + ", ".join(unmapped))


@pytest.fixture(autouse=True)
def _port_selected_boundary(request):
    stdio_prefix = getattr(request.config, "_port_stdio_prefix", None)
    if stdio_prefix is not None and MANIFEST["mapped"].get(request.node.nodeid) == "stdio-shim-process":
        import mcp.client.stdio
        original = mcp.client.stdio.stdio_client

        @asynccontextmanager
        async def candidate_client(server, *args, **kwargs):
            # The selected tests keep their own daemon, environment and SDK assertions.
            # Fail closed if a test changes to a private Python entrypoint.
            modes = public_shim_arguments(server.command, server.args)
            env = dict(server.env or {})
            # Retain an intentional selector, including an empty no-Python override.
            env.setdefault("PSEUDOLIFE_MCP_PYTHON",
                           os.environ.get("PSEUDOLIFE_MCP_PYTHON", sys.executable))
            updated = server.model_copy(update={"command": stdio_prefix[0],
                                               "args": [*stdio_prefix[1:], *modes], "env": env})
            async with original(updated, *args, **kwargs) as streams:
                yield streams

        request.getfixturevalue("monkeypatch").setattr(mcp.client.stdio, "stdio_client", candidate_client)
        return
    prefix = getattr(request.config, "_port_cli_prefix", None)
    if prefix is None:
        return
    if MANIFEST["mapped"].get(request.node.nodeid) != "cli-main-process":
        raise pytest.UsageError("selected boundary is not implemented")
    monkeypatch = request.getfixturevalue("monkeypatch")
    tmp_path = request.getfixturevalue("tmp_path")

    def main():
        env = isolated_env(tmp_path)
        env["PSEUDOLIFE_MCP_PYTHON"] = os.environ.get("PSEUDOLIFE_MCP_PYTHON", sys.executable)
        result = run_cli(prefix, sys.argv[1:], cwd=Path.cwd(),
                         env=env, timeout=10)
        # capsys sees the candidate's streams; original test assertions and
        # expected SystemExit stay intact. No Python CLI implementation is run.
        sys.stdout.write(base64.b64decode(result["stdout_b64"]).decode("utf-8"))
        sys.stderr.write(base64.b64decode(result["stderr_b64"]).decode("utf-8"))
        raise SystemExit(result["exit_code"])

    monkeypatch.setattr(request.node.module, "main", main)


def public_shim_arguments(command, arguments):
    if Path(command).name.lower() in {"pseudolife-mcp", "pseudolife-mcp.exe"}:
        modes = arguments
    elif command == sys.executable and arguments[:2] == ["-m", "pseudolife_memory.cli"]:
        modes = arguments[2:]
    else:
        raise pytest.UsageError("stdio adapter requires the public Python CLI")
    if modes not in ([], ["shim"], ["channel"]):
        raise pytest.UsageError("stdio adapter maps only default, shim and channel")
    return modes
