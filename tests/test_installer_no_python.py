"""The installers look up and start no Python on the host.

Every step they used to run through Python is a step of the native client
(``pseudolife-mcp installer ...``). Two guards hold that:

- ``test_installer_client_only.py::test_a_client_only_install_wires_clients_to_the_remote_daemon``
  runs a whole ``ops/install.sh`` client-only install with ``python``,
  ``python3``, ``py``, ``pip``, ``pip3`` and ``pipx`` on PATH as stubs that
  record any call and fail it, and asserts none was made;
- this file checks every script an install runs for a Python lookup or start
  that a stub cannot see (``command -v``, ``Get-Command``, a probe), while
  the words that name the Python *daemon engine* or run Python inside the
  daemon container are allowed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("ops/install.sh", "ops/install.ps1", "ops/preflight.sh", "ops/preflight.ps1",
           "ops/install-hook.sh", "ops/install-hook.ps1", "ops/add-user-path.ps1")

# A host interpreter or package tool, by name or by path, with or without a
# version or .exe.
NAME = r"(?:[\w./\\:-]*[/\\])?(?:python[\d.]*|py|pip[\d.]*|pipx)(?:\.exe)?"
START = r"(^|[\s;(|&=\"'])"
# A host interpreter or package tool, looked up or started.
LOOKUPS = [
    re.compile(r"\bcommand\s+-v\s+[\"']?(python[\d.]*|py|pip[\d.]*|pipx)\b"),
    re.compile(r"\bwhich\s+(python[\d.]*|pip[\d.]*|pipx)\b"),
    re.compile(r"Get-Command\s+[\"']?(python[\d.]*|py|pip[\d.]*|pipx)\b", re.I),
    re.compile(r"Start-Process\s+(-FilePath\s+)?[\"']?" + NAME + r"\b", re.I),
    re.compile(r"@\(\s*\"python\"\s*,\s*\"python3\""),        # a candidate list
    # Started with a flag, a script on stdin or a heredoc.
    re.compile(START + NAME + r"[\"']?\s+(-[cm3]\b|-\s|-$|<<)"),
    re.compile(START + NAME + r"[\"']?\s*<<"),
    # Started on a .py script.
    re.compile(START + NAME + r"[\"']?\s+[\"']?[\w$./{}\\-]*\.py\b"),
    re.compile(r"\b(installer_python|Get-InstallerPython|shim_tooling_ready|Test-ShimToolingReady)\b"),
    # A candidate loop, and a probe of its variable.
    re.compile(r"\bfor\s+\w+\s+in\s+(python[\d.]*|py)\b"),
    re.compile(r"\$\{?(candidate|c|py\w*|PYBIN)\}?[\"']?\s+(@\w+\s+)?(-c|-m)\b", re.I),
    re.compile(r"\bPYBIN\b|<<\s*'?PY'?\s*$"),
    # A variable named for the interpreter, run ("$resource_python" x, & $python x).
    re.compile(r"(^|[\s;(|&=])[\"']?\$\{?\w*python\w*\}?[\"']?\s+[\"'$\w@-]", re.I),
    re.compile(r"\bensurepip\b"),
    # pip or pipx in command position (a sentence that names them is text).
    re.compile(r"(^|[;&|(]\s*|\bthen\s+|&\s+)(pip[\d.]*|pipx)\s+install\b"),
]
# Python run inside the daemon container is the container's, not the host's.
IN_CONTAINER = re.compile(r"docker\s+exec\b")


def python_lookups(text: str) -> list[str]:
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or IN_CONTAINER.search(line):
            continue
        if any(pattern.search(line) for pattern in LOOKUPS):
            hits.append(f"{number}: {stripped[:120]}")
    return hits


@pytest.mark.parametrize("script", SCRIPTS)
def test_no_installer_script_looks_up_or_starts_python(script):
    assert python_lookups((ROOT / script).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("line", [
    'for candidate in python3 python; do',  # (only flagged with its probe below)
    '    if command -v "$candidate" >/dev/null 2>&1 && command -v python3 >/dev/null',
    'resource_python="$(installer_python)"',
    '"$resource_python" "$preflight_dir/preflight.py" --env-file "$preflight_dir/.env"',
    '& $python -c "import sys"',
    '$probe = & $candidate @probeArgs -c \'import sys\'',
    'foreach ($candidate in @("python", "python3", "py")) {',
    'if (Get-Command pipx -ErrorAction SilentlyContinue) {',
    'python3 -m venv "$runtime"',
    'pipx install --force "$repo"',
    'if "$c" -c "" >/dev/null 2>&1; then PYBIN="$c"; break; fi',
    # The forms a review found the first version of this scan missed.
    "python3 - <<'EOF'",
    'out=$(python3 <<EOF',
    '"$PYTHON" "$repo/ops/x.py"',
    '/usr/bin/python3 -c "import sys"',
    'python3.12 -m venv "$runtime"',
    'py.exe -3 -c "import sys"',
    'Start-Process python -ArgumentList "x.py"',
])
def test_the_scan_finds_the_lookups_the_installers_used_to_make(line):
    if line.startswith("for candidate in"):
        line += '\n    "$candidate" -c "import sys"'
    if 'PYBIN' in line:
        line = 'for c in python3 python; do\n  if "$c" -c "" >/dev/null 2>&1; then PYBIN="$c"; break; fi'
    assert python_lookups(line), line


@pytest.mark.parametrize("line", [
    '#   --engine rust|python             the Docker daemon',
    'COMPOSE_ENGINE=python',
    'engine_helper choose --engine python >/dev/null || exit 1',
    '[ "${COMPOSE_ENGINE:-python}" = python ] || health_tries=120',
    "            docker exec pseudolife-mcp-daemon python -c 'import sys, urllib.request'",
    '    docker exec pseudolife-mcp-daemon python -c $probe $probeUrl 2>&1 | Out-Null',
    'Step "Daemon engine: python (no bearer token for the default Rust daemon)"',
    '# daily job that does (pseudolife_memory/unattended_update.py). That is a',
    '"$NATIVE_CLI" installer hook-settings install || exit 1',
])
def test_the_scan_leaves_the_python_daemon_and_in_container_python_alone(line):
    assert python_lookups(line) == [], line
