"""External adapter: preserve selected assertions, run CLI main in a process."""
import base64
import json
from pathlib import Path
import sys
import subprocess
from contextlib import asynccontextmanager
import os

import pytest

from evals.rust_port.harness import isolated_env, run_cli

MANIFEST = json.loads(Path(__file__).with_name("oracle_tests.json").read_text(encoding="utf-8"))
# Admit subprocess nodes only when their candidate modes are supported.
# Doctor remains deferred; the global CLI selector must fail closed for it.
CLI_SUBPROCESS_NODES = {
    "tests/test_memory_changes_hook.py::test_prompt_hook_prints_only_changes_and_advances_its_cursor[cli]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_prompt_hook_keeps_its_cursor_on_an_empty_or_malformed_answer[-cli]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_prompt_hook_keeps_its_cursor_on_an_empty_or_malformed_answer[not-a-cursor\\nnote\\n-cli]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_prompt_hook_prints_nothing_when_it_cannot_save_its_cursor[cli]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_prompt_hook_is_silent_and_keeps_its_cursor_when_the_daemon_is_down[cli]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_prompt_hook_asks_nothing_for_a_missing_or_unsafe_session_id[-cli]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_prompt_hook_asks_nothing_for_a_missing_or_unsafe_session_id[bad id-cli]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_prompt_hook_asks_nothing_for_a_missing_or_unsafe_session_id[xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-cli]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_prompt_hook_asks_nothing_for_a_missing_or_unsafe_session_id[a/b-cli]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_a_sessions_first_note_clears_marks_idle_for_a_month[cli]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_cli_prompt_hook_refuses_a_redirect_and_keeps_its_cursor": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_cli_prompt_hook_sends_the_bearer_and_prefers_the_token_file": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_cli_prompt_hook_asks_nothing_without_a_top_level_session_id[not json]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_cli_prompt_hook_asks_nothing_without_a_top_level_session_id[[]]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_cli_prompt_hook_asks_nothing_without_a_top_level_session_id[{\"session_id\": 7}]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_cli_prompt_hook_asks_nothing_without_a_top_level_session_id[{\"prompt\": \"\\\\\"session_id\\\\\": \\\\\"sess-1\\\\\"\"}]": ["prompt-hook"],
    "tests/test_memory_changes_hook.py::test_cli_prompt_hook_is_a_listed_mode": ["--help"],
    "tests/test_memory_changes_hook.py::test_cli_prompt_hook_reads_its_payload_as_utf8": ["prompt-hook"],
}

CLI_VERSION_NODES = {
    "tests/test_cli_dispatch.py::test_version_prints_the_package_version[--version]",
    "tests/test_cli_dispatch.py::test_version_prints_the_package_version[version]",
    "tests/test_cli_dispatch.py::test_version_from_a_runtime_names_its_directory_and_commit",
}


def boundary(node):
    if node in CLI_SUBPROCESS_NODES:
        return "cli-main-process"
    return MANIFEST["mapped"].get(node)


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
    parser.addoption("--port-full-suite", action="store_true",
                     help="Route mapped stdio tests and run all other tests against Python")


def pytest_configure(config):
    for option, attribute in (("--port-cli-json", "_port_cli_prefix"),
                              ("--port-stdio-json", "_port_stdio_prefix")):
        value = config.getoption(option)
        if value is None:
            value = os.environ.get({"--port-cli-json": "PSEUDOLIFE_PORT_CLI_JSON",
                                    "--port-stdio-json": "PSEUDOLIFE_PORT_STDIO_JSON"}[option])
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
    unmapped = [item.nodeid for item in session.items if boundary(item.nodeid) not in boundaries]
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
    if boundary(request.node.nodeid) != "cli-main-process":
        raise pytest.UsageError("selected boundary is not implemented")
    monkeypatch = request.getfixturevalue("monkeypatch")
    if request.node.nodeid in CLI_SUBPROCESS_NODES:
        original = subprocess.run
        routed = []

        def candidate_run(command, *args, **kwargs):
            modes = public_cli_arguments(command)
            if modes is None:
                return original(command, *args, **kwargs)
            if modes != CLI_SUBPROCESS_NODES[request.node.nodeid]:
                raise pytest.UsageError("CLI subprocess adapter observed unsupported argv: " + repr(modes))
            env = dict(kwargs.get("env") or os.environ)
            env["CUDA_VISIBLE_DEVICES"] = "-1"
            routed.append(modes)
            return original([*prefix, *modes], *args, **{**kwargs, "env": env})

        monkeypatch.setattr(subprocess, "run", candidate_run)
        request.addfinalizer(lambda: routed or pytest.fail("CLI subprocess adapter observed no public CLI call"))
        return
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
