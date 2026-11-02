"""Engine-labelled operator dispatch without a database or Docker daemon."""
import io
import subprocess

import pytest

from pseudolife_memory import daemon_exec


@pytest.mark.parametrize("engine", ["rust", "python", "<no value>", ""])
@pytest.mark.parametrize("terminal", [False, True])
def test_engine_selects_operator_and_preserves_arguments(monkeypatch, engine, terminal):
    calls = []
    source = io.BytesIO(b"input\x00\xff\n")
    sink = io.BytesIO()

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "inspect":
            answer = engine if "Labels" in argv[3] else "true"
            return subprocess.CompletedProcess(argv, 0, answer + "\n", "")
        assert kwargs["stdin"] is (None if terminal else source)
        assert kwargs["stdout"] is (None if terminal else sink)
        return subprocess.CompletedProcess(argv, 23)

    monkeypatch.delenv(daemon_exec.NESTED_ENV, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_DOCKER", "fixture-docker")
    monkeypatch.setattr(daemon_exec, "run", run)
    monkeypatch.setattr(daemon_exec, "_terminal", lambda: terminal)
    args = ["revoke", "--", "-prefix with spaces", "$(literal)"]
    result = daemon_exec.run_in_daemon(
        "maintainer", args, stdin=None if terminal else source,
        stdout=None if terminal else sink)
    assert result.returncode == 23
    executable = ["pseudolife-mcp"] if engine == "rust" else ["python", "-m", "pseudolife_memory.cli"]
    assert calls[-1][0] == ["fixture-docker", "exec", "-it" if terminal else "-i",
                            "-e", "PSEUDOLIFE_DAEMON_EXEC=1", daemon_exec.CONTAINER,
                            *executable, "maintainer", *args]


def test_nested_operator_does_not_probe_or_execute(monkeypatch):
    monkeypatch.setenv(daemon_exec.NESTED_ENV, "1")
    monkeypatch.setattr(daemon_exec, "run", lambda *a, **k: pytest.fail("nested docker"))
    assert daemon_exec.run_in_daemon("lease", ["break", "gpu"]) is None


@pytest.mark.parametrize("engine", ["rust", "python", "<no value>"])
@pytest.mark.parametrize("exit_code, expected", [(0, 0), (4, 4), (23, 1)])
def test_invite_selects_engine_and_guards_both_execs(monkeypatch, capsys, engine, exit_code, expected):
    from pseudolife_memory import invite_cli
    calls = []

    def run(argv, input=None):
        calls.append(argv)
        if argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 0, engine if "Labels" in argv[3] else "true", "")
        if "--version-check" in argv:
            return subprocess.CompletedProcess(argv, 0, "pseudolife-mcp invite 1\n", "")
        return subprocess.CompletedProcess(argv, exit_code, "container report\n", "container diagnostic\n")

    monkeypatch.delenv(daemon_exec.NESTED_ENV, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_DOCKER", "fixture-docker")
    monkeypatch.setattr(invite_cli, "local_database", lambda: (None, None))
    monkeypatch.setattr(invite_cli, "run", run)
    assert invite_cli.main(["laptop", "--url", "https://example.com", "--json"]) == expected
    output = capsys.readouterr()
    assert output.out == "container report\n"
    assert output.err == "container diagnostic\n"
    cli = ["pseudolife-mcp"] if engine == "rust" else ["python", "-m", "pseudolife_memory.cli"]
    assert calls[2] == ["fixture-docker", "exec", "-e", "PSEUDOLIFE_DAEMON_EXEC=1",
                        daemon_exec.CONTAINER, *cli, "invite", "--version-check"]
    assert calls[3] == ["fixture-docker", "exec", "-i", "-e", "PSEUDOLIFE_DAEMON_EXEC=1",
                        daemon_exec.CONTAINER, *cli, "invite", "laptop", "--expires", "900s",
                        "--url", "https://example.com", "--json"]


def test_invite_nested_missing_bank_never_runs_docker(monkeypatch):
    from pseudolife_memory import invite_cli
    monkeypatch.setenv(daemon_exec.NESTED_ENV, "1")
    monkeypatch.setenv("PSEUDOLIFE_DOCKER", "fixture-docker")
    monkeypatch.setattr(invite_cli, "local_database", lambda: (None, None))
    monkeypatch.setattr(invite_cli, "run", lambda *a, **k: pytest.fail("nested docker"))
    assert invite_cli.main(["--list", "--json"]) == 4


def test_native_stopped_daemon_sql_matches_the_oracle():
    from pathlib import Path
    from pseudolife_memory import invite_cli
    root = Path(__file__).resolve().parents[1] / "rust/shim/src/cli/pairing"
    for name in ("LIST_SQL", "LIST_SQL_V53", "ACTIVITY_SQL", "BANK_SQL"):
        assert (root / (name.lower() + ".sql")).read_text() == getattr(invite_cli, name)


def test_native_help_snapshots_match_python_parsers():
    from contextlib import redirect_stdout
    from pathlib import Path
    from pseudolife_memory import invite_cli, pair_cli, maintainer_cli, test_login_cli
    root = Path(__file__).resolve().parents[1] / "rust/shim/src/cli"
    for name, module, directory in [("invite", invite_cli, "pairing"), ("pair", pair_cli, "pairing"),
                                     ("test_login", test_login_cli, "test_login")]:
        assert (root / directory / (name + "_help.txt")).read_text() == module._parser().format_help()
    for action in ("", "enrol-code", "confirm", "revoke", "reset", "list"):
        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as result:
            maintainer_cli.main(([action] if action else []) + ["--help"])
        assert result.value.code == 0
        assert (root / "maintainer" / ((action or "top") + "_help.txt")).read_text() == out.getvalue()
    out = io.StringIO()
    with redirect_stdout(out):
        assert test_login_cli.main(["create", "--help"]) == 0
    assert (root / "test_login/create_help.txt").read_text() == out.getvalue()
