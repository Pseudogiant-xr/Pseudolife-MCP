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
# Admit subprocess nodes only when their candidate modes are supported.
# Doctor remains deferred; the global CLI selector must fail closed for it.
CLI_SUBPROCESS_NODES = set()
CLI_HOOK_NODES = {
    "tests/test_codex_hooks.py::test_coordination_prompt_records_only_exact_doorbell_arrival"
    f"[{notice}-{hook}]"
    for notice in ("legacy", "unknown") for hook in ("windows", "posix")
}
CLI_VERSION_NODES = {
    "tests/test_cli_dispatch.py::test_version_prints_the_package_version[--version]",
    "tests/test_cli_dispatch.py::test_version_prints_the_package_version[version]",
    "tests/test_cli_dispatch.py::test_version_from_a_runtime_names_its_directory_and_commit",
}


def boundary(node):
    if node in CLI_SUBPROCESS_NODES or node in CLI_HOOK_NODES:
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
    if request.node.nodeid in CLI_HOOK_NODES:
        _route_doorbell_hook(request, monkeypatch, prefix)
        return
    if request.node.nodeid in CLI_SUBPROCESS_NODES:
        original = subprocess.run
        routed = []

        def candidate_run(command, *args, **kwargs):
            modes = public_cli_arguments(command)
            if modes is None:
                return original(command, *args, **kwargs)
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


def doorbell_hook_environment(command, env, root, tmp_path):
    """Recognize the unchanged hook, launcher and marker-writing runner."""
    if not isinstance(command, (list, tuple)) or not env:
        raise pytest.UsageError("doorbell adapter requires the fixture hook process")
    windows = list(map(str, command[1:])) == [
        "-NoProfile", "-File", str(root / "plugin/hooks/lifecycle.ps1"),
        "-Event", "CoordinationPrompt"]
    posix = list(map(str, command[1:])) == [str(root / "plugin/hooks/coordination-prompt.sh")]
    executable = Path(command[0]).name.lower() if command else ""
    if not ((windows and executable in {"pwsh", "pwsh.exe"}) or
            (posix and executable in {"bash", "bash.exe"})):
        raise pytest.UsageError("doorbell adapter observed a changed hook command")
    runner, marker = tmp_path / "consume.py", tmp_path / "helper-launched"
    expected_runner = (f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n"
                       "from pseudolife_memory.cli import main\nmain()\n")
    cmd = windows and os.name == "nt"
    launcher = tmp_path / ("consume.cmd" if cmd else "consume.sh")
    expected_launcher = (f'@"{sys.executable}" "{runner}" %*\r\n' if cmd else
                         f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" '
                         f'"{runner.as_posix()}" "$@"\n')
    if (env.get("PSEUDOLIFE_SHIM_LAUNCHER") != launcher.as_posix() or
            env.get("PYTHONPATH") != str(root) or
            runner.read_bytes() != expected_runner.replace("\n", os.linesep).encode("utf-8") or
            launcher.read_bytes() != expected_launcher.replace("\n", os.linesep).encode("utf-8")):
        raise pytest.UsageError("doorbell adapter observed changed fixture launcher or runner")
    return runner


def doorbell_child_source():
    # The original consume.py still runs and writes its own marker. Only its
    # imported public main is redirected to the selected executable process.
    return '''import json,os,pathlib,subprocess,sys
config=json.loads(pathlib.Path(os.environ["PSEUDOLIFE_PORT_HOOK_CONFIG"]).read_text(encoding="utf-8"))
if pathlib.Path(sys.argv[0]) == pathlib.Path(config["runner"]):
 import pseudolife_memory.cli as cli
 def main():
  if sys.argv[1:] != ["doorbell-prompt-seen"]:
   raise RuntimeError("doorbell adapter observed changed public mode")
  if pathlib.Path(cli.__file__) != pathlib.Path(config["cli_source"]):
   raise RuntimeError("doorbell adapter imported an unexpected public CLI")
  result=subprocess.run(config["prefix"]+sys.argv[1:],input=sys.stdin.buffer.read(),capture_output=True,timeout=config["timeout"])
  row={"argv":config["prefix"]+sys.argv[1:],"runner":sys.argv[0],"executable":sys.executable,"cli_source":cli.__file__,"exit":result.returncode,"marker":pathlib.Path(config["runner"]).with_name("helper-launched").read_text(encoding="utf-8")}
  with open(config["witness"],"a",encoding="utf-8") as handle:handle.write(json.dumps(row)+"\\n")
  sys.stdout.buffer.write(result.stdout);sys.stderr.buffer.write(result.stderr)
  raise SystemExit(result.returncode)
 cli.main=main
'''


def _route_doorbell_hook(request, monkeypatch, prefix):
    # Keep the original shell capability skip; it is not candidate coverage.
    if request.node.callspec.params["platform_hook"] == "windows":
        if not request.node.module.shutil.which("pwsh"):
            return
    else:
        request.node.module.bash_exe()
    tmp_path = request.getfixturevalue("tmp_path")
    root = request.node.module.ROOT
    adapter = tmp_path / "port-hook-adapter"
    adapter.mkdir()
    (adapter / "sitecustomize.py").write_text(doorbell_child_source(), encoding="utf-8")
    witness = adapter / "calls.jsonl"
    config = adapter / "config.json"
    original, routed = subprocess.run, []

    def candidate_hook(command, *args, **kwargs):
        runner = doorbell_hook_environment(command, kwargs.get("env"), root, tmp_path)
        config.write_text(json.dumps(dict(runner=str(runner), prefix=prefix,
                                         cli_source=str(root / "pseudolife_memory/cli.py"),
                                         timeout=kwargs.get("timeout"), witness=str(witness))), encoding="utf-8")
        env = dict(kwargs["env"])
        env.update(PYTHONPATH=str(adapter) + os.pathsep + env["PYTHONPATH"],
                   PSEUDOLIFE_PORT_HOOK_CONFIG=str(config), CUDA_VISIBLE_DEVICES="-1")
        routed.append(list(command))
        return original(command, *args, **{**kwargs, "env": env})

    def verified_calls():
        rows = [json.loads(line) for line in witness.read_text(encoding="utf-8").splitlines()] if witness.exists() else []
        assert len(routed) == 4 and len(rows) == 3, "doorbell adapter did not observe the original four hooks and three public child calls"
        assert all(row["argv"] == [*prefix, "doorbell-prompt-seen"] and
                   row["marker"] == "yes" and row["exit"] == 0 for row in rows)

    monkeypatch.setattr(subprocess, "run", candidate_hook)
    request.addfinalizer(verified_calls)


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
