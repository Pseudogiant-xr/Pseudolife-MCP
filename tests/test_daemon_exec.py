"""Operator commands that open the bank directly re-run themselves inside the
bundled daemon container when this shell has no bank (2026-10-05).

On a Docker-tier host the bank's database URL lives in the
``pseudolife-mcp-daemon`` container's environment, not the operator's shell:
``pseudolife-mcp lease delegate`` from a root shell on the box printed "no bank
found" and only worked as ``docker exec pseudolife-mcp-daemon pseudolife-mcp
lease delegate ...``. ``lease break|delegate``, every ``maintainer`` action and
every ``board-audit`` action that reads the bank now do that ``docker exec``
themselves. A fake docker stands in for the real one: it records each argv,
answers ``inspect`` and plays the container's output and exit code.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from pseudolife_memory import board_audit_cli, daemon_exec, lease_cli, maintainer_cli

DOCKER = "fake-docker"
PREFIX = [DOCKER, "exec"]
TAIL = ["-e", f"{daemon_exec.NESTED_ENV}=1", daemon_exec.CONTAINER,
        "python", "-m", "pseudolife_memory.cli"]


class Docker:
    """``running``: True, False (stopped) or None (no such container)."""

    def __init__(self, running=True, code=0, out=""):
        self.running, self.code, self.out = running, code, out
        self.calls, self.stdin = [], None

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[1] == "inspect":
            if self.running is None:
                return subprocess.CompletedProcess(
                    argv, 1, "", f"Error: No such object: {daemon_exec.CONTAINER}\n")
            return subprocess.CompletedProcess(argv, 0, f"{str(self.running).lower()}\n", "")
        stdin = kw.get("stdin")
        if stdin is not None:
            self.stdin = stdin.read()
        stdout = kw.get("stdout")
        if stdout is subprocess.PIPE:
            return subprocess.CompletedProcess(argv, self.code, self.out.encode(), None)
        if stdout is None:
            sys.stdout.write(self.out)   # inherited: the container writes to our stdout
        else:
            stdout.write(self.out.encode())
        return subprocess.CompletedProcess(argv, self.code)

    @property
    def execs(self):
        return [call for call in self.calls if call[1] == "exec"]


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def docker(monkeypatch, tmp_path):
    """No bank in this shell (no DSN, an empty lite data dir); a fake docker."""
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv(daemon_exec.NESTED_ENV, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path / "no-bank-here"))
    monkeypatch.setenv("PSEUDOLIFE_DOCKER", DOCKER)
    fake = Docker()
    monkeypatch.setattr(daemon_exec, "run", fake)
    return fake


# ── lease ───────────────────────────────────────────────────────────────────

def test_lease_delegate_runs_inside_the_daemon_container(docker, capsys):
    answer = {"name": "delegate:Pseudolife-MCP", "holder": "abc", "fence": 7}
    docker.out = json.dumps(answer) + "\n"
    code = lease_cli.main(["delegate", "Pseudolife-MCP", "abc", "--for", "168h"])
    assert code == 0
    assert docker.execs == [PREFIX + ["-i"] + TAIL
                            + ["lease", "delegate", "Pseudolife-MCP", "abc", "--for", "168h"]]
    output = capsys.readouterr()
    assert json.loads(output.out) == answer          # stdout is the container's, untouched
    [line] = output.err.splitlines()                 # one line says where it ran
    assert daemon_exec.CONTAINER in line


def test_lease_break_propagates_the_containers_exit_code(docker):
    docker.code = 1
    assert lease_cli.main(["break", "gpu"]) == 1
    assert docker.execs[0][-3:] == ["lease", "break", "gpu"]


def test_the_deprecated_designate_runs_inside_as_delegate(docker):
    assert lease_cli.main(["designate", "P", "abc"]) == 0
    assert docker.execs[0][-4:] == ["lease", "delegate", "P", "abc"]


# ── maintainer ──────────────────────────────────────────────────────────────

def test_maintainer_passes_a_terminal_through(docker, monkeypatch):
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(sys, "stdout", Terminal())
    assert maintainer_cli.main(["reset", "--yes"]) == 0
    assert docker.execs == [PREFIX + ["-it"] + TAIL + ["maintainer", "reset", "--yes"]]


def test_maintainer_without_a_terminal_uses_plain_stdin(docker):
    # pytest's captured stdin and stdout are not terminals.
    maintainer_cli.main(["list"])
    assert docker.execs == [PREFIX + ["-i"] + TAIL + ["maintainer", "list"]]


@pytest.mark.parametrize("code", [1, 2, 3])
def test_maintainer_propagates_the_containers_exit_code(docker, code):
    docker.code = code
    assert maintainer_cli.main(["confirm", "AbCdEf123456"]) == code


def test_maintainer_forwards_a_prefix_that_looks_like_an_option(docker):
    maintainer_cli.main(["revoke", "-AbCdEf12"])
    [call] = docker.execs
    assert call[-4:] == ["maintainer", "revoke", "--", "-AbCdEf12"]


# ── board-audit ─────────────────────────────────────────────────────────────

def test_board_audit_export_streams_from_the_container(docker, capsys):
    docker.out = '{"seq":1}\n{"seq":2}\n'
    assert board_audit_cli.main(["export", "--project", "P", "--since", "100"]) == 0
    [call] = docker.execs
    assert call[call.index("board-audit"):] == ["board-audit", "export", "--project", "P",
                                                "--since", "100.0"]
    assert capsys.readouterr().out == docker.out


def test_board_audit_export_out_writes_the_file_on_this_host(docker, tmp_path):
    docker.out = '{"seq":1}\n'
    target = tmp_path / "board.jsonl"
    assert board_audit_cli.main(["export", "--out", str(target)]) == 0
    [call] = docker.execs
    assert "--out" not in call                       # the container cannot see this host's path
    assert target.read_text(encoding="utf-8") == docker.out


def test_board_audit_export_out_leaves_no_empty_file_when_the_container_fails(docker, tmp_path):
    docker.code = 2
    target = tmp_path / "board.jsonl"
    assert board_audit_cli.main(["export", "--out", str(target)]) == 2
    assert not target.exists()


def test_board_audit_stats_sends_this_hosts_durations_and_writes_here(docker, tmp_path, capsys):
    durations = tmp_path / "durations.jsonl"
    durations.write_text('{"waited": 3}\n', encoding="utf-8")
    report = '{"window":{}}\n'
    docker.out = report
    out, log = tmp_path / "stats.json", tmp_path / "stats.jsonl"
    code = board_audit_cli.main(["stats", "--since", "10", "--until", "20",
                                 "--durations", str(durations),
                                 "--out", str(out), "--append", str(log)])
    assert code == 0
    [call] = docker.execs
    assert call[call.index("board-audit"):] == ["board-audit", "stats", "--since", "10.0",
                                                "--until", "20.0", "--durations", "/dev/stdin"]
    assert docker.stdin == durations.read_bytes()
    assert out.read_text(encoding="utf-8") == report
    assert log.read_text(encoding="utf-8") == report
    assert capsys.readouterr().out == ""


def test_board_audit_verify_and_redact_forward_their_arguments(docker):
    head = "a" * 64
    board_audit_cli.main(["verify", "--expect-head", f"5:{head}"])
    board_audit_cli.main(["redact", "--message-id", "m1", "--reason", "pasted a key"])
    first, second = docker.execs
    assert first[first.index("board-audit"):] == ["board-audit", "verify",
                                                  "--expect-head", f"5:{head}"]
    assert second[second.index("board-audit"):] == ["board-audit", "redact", "--message-id",
                                                    "m1", "--reason", "pasted a key"]


# ── when it does not re-run ─────────────────────────────────────────────────

@pytest.mark.parametrize("running", [None, False])
@pytest.mark.parametrize("command, code", [
    (lambda: lease_cli.main(["delegate", "P", "abc"]), 1),
    (lambda: maintainer_cli.main(["list"]), 2),
    (lambda: board_audit_cli.main(["export"]), 2),
])
def test_no_running_container_keeps_the_message_and_names_docker_exec(
        docker, capsys, running, command, code):
    docker.running = running
    assert command() == code
    assert docker.execs == []
    err = capsys.readouterr().err
    assert "no bank found" in err and "PSEUDOLIFE_MCP_DATABASE_URL" in err
    assert f"docker exec -it {daemon_exec.CONTAINER} pseudolife-mcp" in err


def test_no_docker_at_all_keeps_the_message(docker, monkeypatch, capsys):
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(daemon_exec, "run", missing)
    assert lease_cli.main(["break", "gpu"]) == 1
    assert "no bank found" in capsys.readouterr().err


def test_a_database_url_never_re_runs(docker, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL",
                       "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1")
    lease_cli.main(["break", "gpu"])
    maintainer_cli.main(["list"])
    board_audit_cli.main(["verify"])
    assert docker.calls == []


def test_inside_the_container_it_never_loops(docker, monkeypatch, capsys):
    monkeypatch.setenv(daemon_exec.NESTED_ENV, "1")
    assert lease_cli.main(["break", "gpu"]) == 1
    assert docker.calls == []
    assert "no bank found" in capsys.readouterr().err
