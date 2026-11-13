"""The frozen Python Codex hook setup against the golden the native
`pseudolife-mcp installer codex-hooks` is held to.

Codex approves a hook by its definition (command, commandWindows, timeout,
async, statusMessage, matcher) and by its key (file, event, group and
handler index), so the native setup must write exactly what
``ops/setup-codex-hooks.py`` writes. This test pins the Python output into
``rust/shim/src/cli/installer/codex_hooks/testdata/definitions-<os>.json``;
``codex_hooks::tests`` holds the native output to the same file. Record a
missing or changed golden with ``PSEUDOLIFE_RECORD_CODEX_HOOK_GOLDEN=1``
(on Windows and on Linux), then review the diff: a changed definition means
every Codex user approves the hooks again.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_hook_setup_golden", ROOT / "ops/setup-codex-hooks.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)

FAMILY = "windows" if os.name == "nt" else "posix"
GOLDEN = ROOT / "rust/shim/src/cli/installer/codex_hooks/testdata" / f"definitions-{FAMILY}.json"
# Literal homes for the pure definitions: quoting of a space, an apostrophe
# and a dollar sign in both shells' commands.
LITERAL_HOMES = {
    "windows": ["C:\\Users\\u\\.codex", "C:\\Users\\o'brien $x\\.codex"],
    "posix": ["/home/u/.codex", "/home/o'brien $x/.codex", "/home/u/plain.codex"],
}[FAMILY]
OLD = "0123456789abcdef0123"


def _strings(value, change):
    if isinstance(value, str):
        return change(value)
    if isinstance(value, list):
        return [_strings(v, change) for v in value]
    if isinstance(value, dict):
        return {k: _strings(v, change) for k, v in value.items()}
    return value


def _scenario_input(home: Path, old: Path) -> dict:
    """A hooks.json an older install and a user left: an unrelated hook, the
    bundle-named commands setup wrote before the launcher (with and without
    the Codex marker), a shipped legacy prompt command, and another event."""
    current = setup.manual_definitions(old)
    unmarked = setup.manual_definitions(old, codex_marker=False)
    unrelated = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo unrelated", "timeout": 7}]}
    return {"description": "user file", "hooks": {
        "SessionStart": [unrelated] + [{"hooks": [current[r]]} for r in setup.MANUAL_ROLES["sessionStart"]],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "pseudolife-mcp prompt-hook"}]},
                             {"hooks": [current["UserPromptSubmit"]]},
                             {"hooks": [current["CoordinationPrompt"]]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": unmarked["SessionEnd"]["command"]}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
    }}


def _record(tmp_path: Path) -> dict:
    definitions = {}
    for literal in LITERAL_HOMES:
        home = Path(literal)
        bundle = home / "pseudolife" / "hooks" / OLD
        definitions[literal] = {
            "launcher": setup.launcher_definitions(home),
            "manual_marker": setup.manual_definitions(bundle),
            "manual_plain": setup.manual_definitions(bundle, codex_marker=False),
        }
    scenarios = {}
    for name, plugin, seeded in (("fresh", False, False), ("migrate", False, True), ("plugin", True, True)):
        home = tmp_path / name / "w7 hooks" / "codex"
        home.mkdir(parents=True)
        home = home.resolve()
        old = setup.hooks_root(home) / OLD
        placeholder = {str(old): "{OLD}", str(home): "{HOME}"}

        def abstract(text):
            for real, mark in placeholder.items():
                text = text.replace(real, mark)
            return text

        def abstract_json(text):
            # The file spells each path JSON-escaped.
            for real, mark in placeholder.items():
                text = text.replace(json.dumps(real)[1:-1], mark)
            return text

        given = None
        if seeded:
            old.mkdir(parents=True)
            given = _scenario_input(home, old)
            (home / "hooks.json").write_text(json.dumps(given), encoding="utf-8")
        setup.install_manual(home, {"backups": []}, plugin=plugin)
        first = (home / "hooks.json").read_bytes()
        setup.install_manual(home, {"backups": []}, plugin=plugin)
        assert (home / "hooks.json").read_bytes() == first, "a rerun changed hooks.json"
        scenarios[name] = {
            "plugin": plugin,
            "input": None if given is None else _strings(given, abstract),
            "output": _strings(json.loads(first), abstract),
            "output_text": abstract_json(first.decode("utf-8")),
        }
    recorded = {"family": FAMILY, "definitions": definitions, "scenarios": scenarios}
    # The golden is committed: no real temporary path may survive in it.
    assert str(tmp_path) not in json.dumps(recorded) and json.dumps(str(tmp_path))[1:-1] not in json.dumps(recorded)
    return recorded


def test_the_python_hook_definitions_match_the_native_golden(tmp_path):
    recorded = _record(tmp_path)
    if os.environ.get("PSEUDOLIFE_RECORD_CODEX_HOOK_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(recorded, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    if not GOLDEN.is_file():
        pytest.fail(f"{GOLDEN} is missing; record it with PSEUDOLIFE_RECORD_CODEX_HOOK_GOLDEN=1")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert recorded["definitions"] == golden["definitions"]
    for name, scenario in golden["scenarios"].items():
        assert recorded["scenarios"][name]["output"] == scenario["output"], name
        assert recorded["scenarios"][name]["output_text"] == scenario["output_text"], name


def test_the_golden_scenarios_name_a_quoted_home():
    """The scenario home has a space, so its commands quote it in both
    shells on every OS (a test temp folder alone may not need quoting)."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    commands = [h["command"] for groups in golden["scenarios"]["fresh"]["output"]["hooks"].values()
                for g in groups for h in g["hooks"]]
    assert commands and all("'{HOME}" in c for c in commands), commands
