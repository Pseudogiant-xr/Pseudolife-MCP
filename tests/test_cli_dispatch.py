"""Console-script dispatch (pseudolife_memory.cli) — the torch-free entry point.

The dispatcher's job is tiny (pick a mode, import late), but it is also the
first thing a newcomer pokes at after ``pip install pseudolife-mcp`` — and
``pseudolife-mcp --help`` answering "unknown mode" was an observed
first-contact papercut (2026-07-16 publish smoke test).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from pseudolife_memory import cli
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


# Modes main() dispatches but --help deliberately leaves out. `delegate-watch`
# shipped in v0.18.2 without a help line; this list is the guard against the
# next one.
UNLISTED_MODES = {
    "shim": "the no-argument mode by name",
    "doorbell-prompt-seen": "the doorbell helper the plugin's prompt hook runs, not run by hand",
}


def dispatched_modes() -> set[str]:
    """Every mode string main() compares ``mode`` against (flags excluded)."""
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    body = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "main")
    modes: set[str] = set()
    for node in ast.walk(body):
        if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == "mode"):
            continue
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
                modes.add(right.value)
            elif isinstance(op, ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
                modes.update(e.value for e in right.elts if isinstance(e, ast.Constant))
    return {m for m in modes if isinstance(m, str) and not m.startswith("-")}


def listed_modes() -> set[str]:
    """The mode names --help lists: the indented entries under ``modes:``."""
    section = cli._USAGE.split("\nmodes:\n", 1)[1].split("\n\n", 1)[0]
    return {m.group(1) for line in section.splitlines()
            if (m := re.match(r"  ([a-z][a-z-]*)\s", line))}


def test_every_dispatched_mode_is_listed_in_help_or_exempt():
    dispatched = dispatched_modes()
    assert {"serve", "delegate-watch", "lease"} <= dispatched  # the parser sees the chain
    assert dispatched - listed_modes() - set(UNLISTED_MODES) == set()


def test_every_listed_mode_is_dispatched():
    assert listed_modes() - dispatched_modes() == set()


def test_exempt_modes_stay_dispatched_and_unlisted():
    for mode in UNLISTED_MODES:
        assert mode in dispatched_modes()
        assert mode not in listed_modes()
