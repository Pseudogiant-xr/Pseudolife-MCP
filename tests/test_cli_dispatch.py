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
