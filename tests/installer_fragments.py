"""Helpers the installer tests share, free of the frozen Python package.

The marker blocks and literal payloads of the installers, the Bash a test runs
installer fragments under, and that Bash's spelling of a fixture path. The
native installers' tests import them from here, not from the Python client's
installer tests (tests/test_client_install_ux.py,
tests/test_installer_existing_upgrade.py), which import them back and go when
the Python client goes.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def _marker_block(text: str, name: str) -> list[str]:
    """Lines strictly between ``# >>> <name> >>>`` and ``# <<< <name> <<<``."""
    lines = text.splitlines()
    return lines[lines.index(f"# >>> {name} >>>") + 1:
                 lines.index(f"# <<< {name} <<<")]


def _heredoc_payload(block: list[str]) -> list[str]:
    """The literal payload inside a marker block: a bash quoted heredoc
    (``cat <<'TAG'`` ... ``TAG``) or a PowerShell literal here-string
    (``@'`` ... ``'@``). Both terminators must sit at column 0."""
    for i, line in enumerate(block):
        stripped = line.strip()
        if stripped.endswith("@'"):
            term = "'@"
        elif "<<'" in stripped:
            term = stripped.split("<<'", 1)[1].split("'", 1)[0]
        else:
            continue
        for j in range(i + 1, len(block)):
            if block[j].rstrip() == term:
                return block[i + 1:j]
    raise AssertionError("no heredoc/here-string payload in marker block")


def _bash_fixture_path(
    bash: str, path: Path, env: dict[str, str],
) -> str:
    """Return the path spelling understood by the selected Bash runtime."""
    if os.name != "nt":
        return str(path)
    converted = subprocess.run(
        [bash, "-c", 'cygpath -u -- "$1"', "fixture", str(path)],
        capture_output=True, check=True, text=True, timeout=5,
        env=env,
    )
    return converted.stdout.strip()


def _between(path: str, start: str, end: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    return start + text.split(start, 1)[1].split(end, 1)[0]


def _bash_variants() -> list[str]:
    variants: list[str] = []
    for candidate in (
        shutil.which("bash") if os.name != "nt" else None,
        r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else None,
    ):
        if candidate and Path(candidate).is_file() and candidate not in variants:
            variants.append(candidate)
    return variants
