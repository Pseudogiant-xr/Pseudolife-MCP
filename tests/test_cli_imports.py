"""Light CLI paths must not load the embedding stack or MCP SDK."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


_HELP_MODES = (
    "coordination-recovery", "board-audit", "briefing", "doctor", "tunnel",
    "connect", "invite", "test-login", "pair", "expose", "move", "update",
    "backup", "export", "import", "wait-mail", "lease", "maintainer",
)
_HEAVY = ("torch", "sentence_transformers", "numpy", "transformers", "chromadb", "mcp")
_PROBE = """
import contextlib
import io
import sys

from pseudolife_memory.cli import main

command = sys.argv[1:]
if command == ["config-defaults"]:
    from pseudolife_memory.utils.config import AppConfig
    AppConfig()
elif command:
    sys.argv = ["pseudolife-mcp", *command]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            main()
        except SystemExit as exc:
            refused = command[0] in ("doctor", "coordination-recovery") and "--help" not in command
            assert exc.code == (1 if refused else 0), exc.code
heavy = HEAVY
loaded = sorted(set(name.split(".")[0] for name in sys.modules) & set(heavy))
assert not loaded, loaded
""".replace("HEAVY", repr(_HEAVY))


def _run_probe(arguments, *, cwd, env):
    return subprocess.run([sys.executable, "-X", "utf8=0", *arguments],
                          cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def test_probe_retains_exit_code_with_legacy_stderr_encoding(tmp_path):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env["PYTHONIOENCODING"] = "cp1252"
    proc = _run_probe(["-c", "import sys; sys.stderr.write('caf\\u00e9'); sys.exit(7)"],
                      cwd=tmp_path, env=env)
    assert proc.returncode == 7
    assert proc.stderr.startswith("caf")


@pytest.mark.parametrize("command", [(), ("--help",), ("--version",),
                                    *((mode, "--help") for mode in _HELP_MODES),
                                    ("config-defaults",), ("lease", "check", "full-suite"),
                                    ("doctor", "--timeout", "0.1"),
                                    ("board-audit", "verify"), ("coordination-recovery", "recover")],
                         ids=lambda command: " ".join(command) or "import-cli")
def test_light_cli_imports_in_fresh_process(command, tmp_path):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PSEUDOLIFE_", "CLAUDE_", "CODEX_"))}
    # Exercise this checkout, with no installed client credentials or daemon.
    repo = Path(__file__).resolve().parents[1]
    env.update(PYTHONPATH=str(repo), HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               CODEX_HOME=str(tmp_path / "codex"), CLAUDE_CONFIG_DIR=str(tmp_path / "claude"),
               PSEUDOLIFE_MCP_DAEMON_URL="http://127.0.0.1:1", PSEUDOLIFE_MCP_NO_SPAWN="1")
    if command == ("board-audit", "verify"):
        archive = tmp_path / "audit.jsonl"
        archive.write_text("", encoding="utf-8")
        command = (*command, "--input", str(archive))
    elif command == ("coordination-recovery", "recover"):
        config = tmp_path / "config.yaml"
        config.write_text("coordination:\n  enabled: false\n", encoding="utf-8")
        # Refuse before any database operation, after reading configuration.
        command = (*command, "--config", str(config))
    proc = _run_probe(["-c", _PROBE, *command], cwd=tmp_path, env=env)
    assert proc.returncode == 0, proc.stderr
