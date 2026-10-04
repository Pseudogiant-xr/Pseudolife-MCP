"""Console-script dispatch (pseudolife_memory.cli) — the torch-free entry point.

The dispatcher's job is tiny (pick a mode, import late), but it is also the
first thing a newcomer pokes at after ``pip install pseudolife-mcp`` — and
``pseudolife-mcp --help`` answering "unknown mode" was an observed
first-contact papercut (2026-07-16 publish smoke test).
"""

from __future__ import annotations

import pytest

from pseudolife_memory.cli import main


ALL_MODES = (
    "serve",
    "embedded",
    "shim",
    "briefing",
    "episode-start",
    "episode-end",
    "wait-mail",
    "lease",
    "maintainer",
)


@pytest.mark.parametrize("flag", ["--help", "-h", "help"])
def test_help_prints_usage_and_exits_zero(flag, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", flag])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for mode in ALL_MODES:
        assert mode in out, f"usage must mention mode {mode!r}"
    assert "pseudolife-mcp" in out


def test_unknown_mode_points_at_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", "bogus"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "bogus" in err
    assert "--help" in err


@pytest.mark.parametrize("flag", ["--version", "version"])
def test_version_prints_the_package_version(flag, monkeypatch, capsys):
    """`pseudolife-mcp --version` answered "unknown mode" (2026-09-29)."""
    from pseudolife_memory import __version__, runtimes
    monkeypatch.setattr(runtimes, "running_runtime", lambda layout, prefix=None: None)
    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", flag])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"pseudolife-mcp {__version__}\n"


def test_version_from_a_runtime_names_its_directory_and_commit(monkeypatch, capsys, tmp_path):
    from pseudolife_memory import __version__, runtimes
    runtime = runtimes.Runtime(tmp_path / "000003", 3, __version__, "", "/src/checkout", "ab" * 20)
    monkeypatch.setattr(runtimes, "running_runtime", lambda layout, prefix=None: runtime)
    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", "--version"])
    with pytest.raises(SystemExit):
        main()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"pseudolife-mcp {__version__}"
    assert str(runtime.path) in lines[1] and "ab" * 20 in lines[1]


def test_help_lists_version(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", "--help"])
    with pytest.raises(SystemExit):
        main()
    assert "--version" in capsys.readouterr().out
