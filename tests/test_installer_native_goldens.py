"""The installers' Python helpers and their native replacements answer alike.

The installers run ``pseudolife-mcp installer state|credentials ...`` (native)
where they ran ``ops/installer_state.py`` and ``ops/client_credentials.py``.
``tests/fixtures/installer_native_goldens.json`` records what the Python
helpers print, exit with and leave on disk for each case; this test holds the
Python helpers to it, and ``rust/shim/tests/cli_installer_goldens.rs`` holds
the native steps to the same record, so a difference on either side fails.

Re-record after a deliberate change to a Python helper with
``PSEUDOLIFE_RECORD_INSTALLER_GOLDENS=1 python -m pytest
tests/test_installer_native_goldens.py``; the Rust test then shows whether the
native step still matches.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDENS = ROOT / "tests" / "fixtures" / "installer_native_goldens.json"
# Every step is answered as the sliced installer tests answer it: the ops
# helper it replaced, or the installer's old inline reader.
STANDIN = ROOT / "tests" / "native_helper_standin.py"
RECORD = os.environ.get("PSEUDOLIFE_RECORD_INSTALLER_GOLDENS") == "1"


def _cases():
    return json.loads(GOLDENS.read_text(encoding="utf-8"))["cases"]


def _home_forms(home: str) -> list[str]:
    """The spellings of the home path an output can carry, longest first."""
    forms = {home, home.replace("\\", "/"), json.dumps(home)[1:-1]}
    return sorted(forms, key=len, reverse=True)


# client_config._backup's stamp: the time a backup was taken.
STAMP = re.compile(r"\.bak-pseudolife-\d{8}-\d{6}-\d{6}")


def placeholder(text: str, home: str, paths: bool) -> str:
    for form in _home_forms(home):
        text = text.replace(form, "{HOME}")
    text = STAMP.sub(".bak-pseudolife-<stamp>", text)
    if paths:
        # A path printed after the home uses the host's separator.
        text = text.replace("\\\\", "/").replace("\\", "/")
    return text


def expand(value: str, home: str) -> str:
    return value.replace("{HOME}", home)


def _environment(home: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
    (home / "empty-path").mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "Local"),
               APPDATA=str(home / "Roaming"), PATH=str(home / "empty-path"),
               PSEUDOLIFE_MCP_DAEMON_URL="http://127.0.0.1:1", PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1")
    env.update({key: expand(value, str(home)) for key, value in extra.items()})
    return env


def docker_outside_path() -> Path | None:
    """A ``docker.exe`` the recorded Python helpers would start with ``PATH``
    pointed at an empty folder. They start ``docker`` by bare name, and on
    Windows ``CreateProcess`` also searches the Python interpreter's own
    folder and the system and Windows directories, where the hosted
    ``windows-latest`` image keeps ``docker.exe``. The native steps search
    absolute ``PATH`` directories only, so ``cli_installer_goldens.rs`` runs
    every case on every host."""
    if os.name != "nt":
        return None
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    for folder in (Path(sys.executable).parent, root / "System32", root / "System", root):
        if (folder / "docker.exe").is_file():
            return folder / "docker.exe"
    return None


def run_case(case: dict) -> dict:
    """The case run against the Python helpers: each step's exit, stdout and
    stderr, and the files it names afterwards, placeholdered."""
    home = Path(tempfile.mkdtemp(prefix="installer-goldens-"))
    try:
        if os.name != "nt":
            home.chmod(0o700)
        for name, text in case.get("files", {}).items():
            target = home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(expand(text, str(home)).encode("utf-8"))
        paths = bool(case.get("paths"))
        steps = []
        for step in case["steps"]:
            argv = [expand(arg, str(home)) for arg in step["argv"]]
            stdin = step.get("stdin")
            result = subprocess.run(
                [sys.executable, "-I", str(STANDIN), str(ROOT), *argv], cwd=home,
                env=_environment(home, case.get("env", {})),
                input=None if stdin is None else expand(stdin, str(home)).encode("utf-8"),
                stdin=subprocess.DEVNULL if stdin is None else None,
                capture_output=True, timeout=120, check=False)
            steps.append({
                "argv": step["argv"],
                "stdin": stdin,
                "exit": result.returncode,
                "stdout": placeholder(result.stdout.decode("utf-8", "replace").replace("\r\n", "\n"), str(home), paths),
                # A case may leave stderr out: the Python helpers' logging
                # warnings (its token-map parser's) are not part of the
                # contract, and the native steps do not print them.
                "stderr": (placeholder(result.stderr.decode("utf-8", "replace").replace("\r\n", "\n"), str(home), paths)
                           if case.get("compare_stderr", True) else None),
            })
        after = {}
        for name in case.get("files_after", {}):
            target = home / name
            # Compared as text: Python's write_text writes CRLF on Windows
            # where the native steps write LF, and every reader of these
            # files reads either.
            after[name] = (placeholder(target.read_bytes().decode("utf-8", "replace").replace("\r\n", "\n"),
                                       str(home), paths)
                           if target.is_file() else None)
        return {"steps": steps, "files_after": after}
    finally:
        shutil.rmtree(home, ignore_errors=True)


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_python_helper_matches_the_recorded_answer(case):
    if case.get("needs") == "no-docker" and (docker := docker_outside_path()):
        pytest.skip(f"the Python helpers would start {docker} even with an empty PATH, so they cannot "
                    "replay a case without Docker here; the native steps still run it")
    observed = run_case(case)
    if RECORD:
        recorded = json.loads(GOLDENS.read_text(encoding="utf-8"))
        for entry in recorded["cases"]:
            if entry["name"] == case["name"]:
                entry["steps"] = observed["steps"]
                if "files_after" in entry:
                    entry["files_after"] = observed["files_after"]
        GOLDENS.write_text(json.dumps(recorded, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        return
    assert observed["steps"] == case["steps"]
    if "files_after" in case:
        assert observed["files_after"] == case["files_after"]


def test_every_case_is_recorded():
    for case in _cases():
        assert case["steps"], case["name"]
        assert all("exit" in step and "stdout" in step for step in case["steps"]), case["name"]
