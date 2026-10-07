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
CLI_VERSION_NODES = {
    "tests/test_cli_dispatch.py::test_version_prints_the_package_version[--version]",
    "tests/test_cli_dispatch.py::test_version_prints_the_package_version[version]",
    "tests/test_cli_dispatch.py::test_version_from_a_runtime_names_its_directory_and_commit",
}


def boundary(node):
    return MANIFEST["mapped"].get(node)


def sent_eligible(item):
    """Recorder success assertions remain internal Python oracle tests."""
    return (item.nodeid.startswith("tests/test_maintainer_web.py::test_a_tokenless_daemon_refuses_every_maintainer_route[")
            and getattr(item, "callspec", None) is not None
            and item.callspec.params.get("method") == "GET"
            and item.callspec.params.get("path") == "/api/maintainer/sent")


def public_cli_arguments(command):
    if not isinstance(command, (list, tuple)) or not command:
        return None
    if command[0] == sys.executable and list(command[1:3]) == ["-m", "pseudolife_memory.cli"]:
        return list(command[3:])
    if Path(command[0]).name.lower() in {"pseudolife-mcp", "pseudolife-mcp.exe"}:
        return list(command[1:])
    return None


def pytest_addoption(parser):
    parser.addoption("--port-cli-json", help="JSON argv prefix for the whole executable under test")
    parser.addoption("--port-stdio-json", help="JSON argv prefix for public shim process tests")
    parser.addoption("--port-sent-json", help="JSON argv prefix for native sent HTTP process tests")
    parser.addoption("--port-full-suite", action="store_true",
                     help="Route mapped stdio tests and run all other tests against Python")


def pytest_configure(config):
    for option, attribute in (("--port-cli-json", "_port_cli_prefix"),
                              ("--port-stdio-json", "_port_stdio_prefix"),
                              ("--port-sent-json", "_port_sent_prefix")):
        value = config.getoption(option)
        if value is None:
            value = os.environ.get({"--port-cli-json": "PSEUDOLIFE_PORT_CLI_JSON",
                                    "--port-stdio-json": "PSEUDOLIFE_PORT_STDIO_JSON",
                                    "--port-sent-json": "PSEUDOLIFE_PORT_SENT_JSON"}[option])
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
    if hasattr(session.config, "_port_sent_prefix"):
        boundaries.add("sent-http-process")
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
    unmapped = [item.nodeid for item in session.items if boundary(item.nodeid) not in boundaries
               and not ("sent-http-process" in boundaries and (sent_eligible(item)
                        or item.nodeid.startswith(("evals/rust_port/test_sent_http_process.py::",
                                                  "evals/rust_port/test_sent_review_controls.py::",
                                                  "evals/rust_port/test_sent_jsonb_keys.py::"))))]
    if unmapped:
        # No silent skip/deselection: an explicit supported selection is required.
        raise pytest.UsageError("selected tests have no process adapter: " + ", ".join(unmapped))


@pytest.fixture(autouse=True)
def _port_selected_boundary(request):
    sent_prefix = getattr(request.config, "_port_sent_prefix", None)
    if sent_prefix is not None and sent_eligible(request.node):
        from evals.rust_baseline.daemon import disposable_database
        from evals.rust_port.sent_http import native_sent, asgi_http_adapter
        from psycopg.conninfo import make_conninfo
        # The original test's Recorder is never called; only its assertions run.
        # Tokenless admission requires no schema or application-service startup.
        with disposable_database() as generated:
            with native_sent(sent_prefix, request.getfixturevalue("tmp_path"),
                             make_conninfo(generated, sslmode="disable")) as (client, _):
                adapter = asgi_http_adapter(client)
                def app(service, token=None):
                    if token is not None:
                        raise ValueError("tokenless routing adapter received a bearer fixture")
                    return adapter
                request.getfixturevalue("monkeypatch").setattr(request.node.module, "_app", app)
                yield
        return
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
        yield
        return
    prefix = getattr(request.config, "_port_cli_prefix", None)
    if prefix is None:
        yield
        return
    if boundary(request.node.nodeid) != "cli-main-process":
        raise pytest.UsageError("selected boundary is not implemented")
    monkeypatch = request.getfixturevalue("monkeypatch")
    tmp_path = request.getfixturevalue("tmp_path")

    def main():
        env = isolated_env(tmp_path)
        env["PSEUDOLIFE_MCP_PYTHON"] = os.environ.get("PSEUDOLIFE_MCP_PYTHON", sys.executable)
        command = prefix
        if request.node.nodeid in CLI_VERSION_NODES:
            from pseudolife_memory import runtimes
            from evals.rust_port.cli_version import checked_dispatch_text, prepare_dispatch_runtime
            # The immutable test supplies this Runtime or None through its stub.
            # Only its fixture data crosses into the process, never Python main.
            runtime = runtimes.running_runtime(None)
            command = prepare_dispatch_runtime(prefix, runtime, env, Path.cwd())
        result = run_cli(command, sys.argv[1:], cwd=Path.cwd(),
                         env=env, timeout=10)
        # capsys sees the candidate's streams; original test assertions and
        # expected SystemExit stay intact. No Python CLI implementation is run.
        text = checked_dispatch_text(result, runtime, Path.cwd()) if request.node.nodeid in CLI_VERSION_NODES \
            else base64.b64decode(result["stdout_b64"]).decode("utf-8")
        sys.stdout.write(text)
        sys.stderr.write(base64.b64decode(result["stderr_b64"]).decode("utf-8"))
        raise SystemExit(result["exit_code"])

    monkeypatch.setattr(request.node.module, "main", main)
    yield


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
