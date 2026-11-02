"""Enrolment child diagnostics keep Python's merged output stream."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from .fixture_daemon import FixtureDaemon, json_body


@pytest.mark.parametrize("arm", ["python", "rust"])
def test_enrolment_refusal_is_forwarded_to_stdout(tmp_path, arm):
    candidate = os.environ.get("R1_PAIR_CANDIDATE") or os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if arm == "rust" and not candidate:
        pytest.skip("R1_PAIR_CANDIDATE is required for the native arm")
    daemon = FixtureDaemon({"/health": json_body({"status": "ok", "auth": True})})
    try:
        port = daemon.url.rsplit(":", 1)[1]
        origin = f"http://localhost:{port}"
        daemon.routes["/api/maintainer"] = json_body({"rp_id": "localhost", "origin": origin})
        state = {"config_path": str(tmp_path / "config.yaml"), "rp_id": "localhost",
                 "origin": origin, "problem": None, "keys": [], "keys_error": None}
        script = tmp_path / "docker_fixture.py"
        script.write_text(
            "import sys\na=sys.argv[1:]\n"
            "if a[0]=='inspect':\n"
            " print('true' if a[2]=='{{.State.Running}}' else 'rust')\n"
            "elif a[-1]=='--host-state':\n"
            f" print({json.dumps(state)!r})\n"
            "elif a[-2:]==['maintainer','enrol-code']:\n"
            " print('fixture-before',flush=True)\n"
            " print('fixture-enrolment-refusal',file=sys.stderr,flush=True)\n"
            " print('fixture-after',flush=True)\n"
            " raise SystemExit(2)\n"
            "else: raise AssertionError(a)\n", encoding="utf-8")
        docker = tmp_path / ("docker.cmd" if os.name == "nt" else "docker")
        docker.write_text(
            f'@echo off\n"{sys.executable}" "{script}" %*\n' if os.name == "nt" else
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n',
            encoding="utf-8")
        docker.chmod(0o700)
        command = ([sys.executable, "-m", "pseudolife_memory.cli"] if arm == "python"
                   else [candidate])
        command += ["maintainer", "setup", "--local", "--yes", "--port", port]
        env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDOLIFE_")}
        env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path),
                   PSEUDOLIFE_MCP_DATA_DIR=str(tmp_path / "data"),
                   PSEUDOLIFE_DOCKER=str(docker), PSEUDOLIFE_MCP_TOKEN="fixture-console-token")
        if os.name == "nt":
            result_file = tmp_path / "result.json"
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            result = subprocess.run(
                [sys._base_executable, str(Path(__file__).with_name("_windows_pair_console.py")),
                 str(result_file), str(tmp_path / "stderr.txt"), "setup-refusal", *command],
                env=env, startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE,
                timeout=30, check=True)
            observation = json.loads(result_file.read_text(encoding="utf-8"))
            assert "error" not in observation, observation
            code, out, err = (observation[k] for k in ("exit_code", "stdout", "stderr"))
        else:
            import pty
            master, slave = pty.openpty()
            try:
                process = subprocess.Popen(command, stdin=slave, stdout=slave,
                                           stderr=subprocess.PIPE, env=env)
                try:
                    _, errors = process.communicate(timeout=20)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
                out = os.read(master, 65536).decode()
                code, err = process.returncode, errors.decode()
            finally:
                os.close(slave)
                os.close(master)
        assert code == 4, (out, err)
        assert out.index("fixture-before") < out.index("fixture-enrolment-refusal") < out.index("fixture-after")
        assert "fixture-enrolment-refusal" not in err
        assert "no passkey was enrolled" in err
    finally:
        daemon.close()
