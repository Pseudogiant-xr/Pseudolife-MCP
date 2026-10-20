"""Check that ops/requirements.lock.txt resolves the way the daemon image installs it.

The daemon image (ops/Dockerfile.daemon, step 2) installs pyproject's
dependencies plus the onnx extra under ``pip install -c requirements.lock.txt``.
A constraint file cannot be negotiated, so a lock whose pins contradict each
other fails the image build with ResolutionImpossible. Dependabot's #875 did
exactly that (aiohttp 3.14.4 requires yarl>=1.25.1; the lock held 1.24.2), and
its CI was green because no other job reads the lock.

This replays that install as ``pip install --dry-run``, so nothing is
installed. Run it on Linux under the image's Python (the Dockerfile's FROM
line), because environment markers decide what resolves:

    docker run --rm -v "$PWD:/src:ro" -w /src python:3.12-slim python ops/check_lock_resolves.py

Two deliberate differences from the image build, both needed to run it
outside the image: ``--ignore-installed`` (the host's packages must not count
as already satisfied) and ``--extra-index-url`` naming the CPU torch index
from the Dockerfile's step 1 (the image installs ``torch==X+cpu`` from there
before step 2, and that build exists only on that index).
"""
from __future__ import annotations

import argparse
import builtins
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
IMAGE_LOCK = "/app/requirements.lock.txt"
IMAGE_PYPROJECT = "/app/pyproject.toml"
CONFLICT_HEADER = "The conflict is caused by:"


def image_python(dockerfile: str) -> str:
    match = re.search(r"^FROM python:(\d+\.\d+)", dockerfile, re.M)
    if not match:
        raise SystemExit("ops/Dockerfile.daemon has no `FROM python:X.Y` line to read the image's Python from")
    return match.group(1)


def torch_index(dockerfile: str) -> str:
    match = re.search(r'--index-url (\S+) "torch==', dockerfile)
    if not match:
        raise SystemExit("ops/Dockerfile.daemon no longer installs torch from an --index-url in step 1; "
                         "update ops/check_lock_resolves.py to match")
    return match.group(1)


def image_install_args(dockerfile: str, pyproject: Path) -> list[str]:
    """The pip arguments the image's step 2 runs, read by executing its command
    with ``check_call`` captured, so this check follows any change to it."""
    match = re.search(r'python -c "(import subprocess,sys,tomllib;.*?)"', dockerfile, re.S)
    if not match:
        raise SystemExit("ops/Dockerfile.daemon's step 2 `python -c \"import subprocess,sys,tomllib;...\"` "
                         "install command was not found; update ops/check_lock_resolves.py to match")
    command = match.group(1).replace("\\\n", "")
    calls: list[list[str]] = []
    real_open = builtins.open

    def image_open(path, *args, **kwargs):
        return real_open(pyproject if path == IMAGE_PYPROJECT else path, *args, **kwargs)

    with mock.patch.object(subprocess, "check_call", calls.append), \
            mock.patch.object(builtins, "open", image_open):
        exec(command, {})
    if len(calls) != 1 or calls[0][1:4] != ["-m", "pip", "install"]:
        raise SystemExit(f"ops/Dockerfile.daemon's step 2 no longer runs one `pip install`: {calls!r}")
    return calls[0][4:]


def dry_run_command(dockerfile: str, root: Path) -> list[str]:
    args = image_install_args(dockerfile, root / "pyproject.toml")
    if args.count(IMAGE_LOCK) != 1 or args[args.index(IMAGE_LOCK) - 1] != "-c":
        raise SystemExit(f"ops/Dockerfile.daemon's step 2 no longer passes `-c {IMAGE_LOCK}` once: {args!r}")
    args[args.index(IMAGE_LOCK)] = str(root / "ops" / "requirements.lock.txt")
    return [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
            "--disable-pip-version-check", "--extra-index-url", torch_index(dockerfile), *args]


def failure_summary(output: str) -> str:
    """pip's conflict block when it has one, else its last lines."""
    lines = output.splitlines()
    if CONFLICT_HEADER in lines:
        start = lines.index(CONFLICT_HEADER)
        end = next((i for i in range(start + 1, len(lines)) if not lines[i].strip()), len(lines))
        block = lines[start:end]
        block += [line for line in lines if line.startswith("ERROR: ResolutionImpossible")]
        return "\n".join(block)
    return "\n".join(lines[-30:])


def running_python() -> tuple[str, str]:
    return sys.platform, f"{sys.version_info.major}.{sys.version_info.minor}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    root = parser.parse_args(argv).root
    dockerfile = (root / "ops" / "Dockerfile.daemon").read_text(encoding="utf-8")
    want = image_python(dockerfile)
    platform, have = running_python()
    if not platform.startswith("linux") or have != want:
        print(f"refusing: the daemon image resolves on Linux under Python {want}, and this is "
              f"{platform} Python {have}, whose environment markers can resolve differently. "
              f"Nothing was checked. Run: docker run --rm -v \"$PWD:/src:ro\" -w /src "
              f"python:{want}-slim python ops/check_lock_resolves.py", file=sys.stderr)
        return 2
    result = subprocess.run(dry_run_command(dockerfile, root), capture_output=True, text=True)
    output = result.stdout + result.stderr
    if result.returncode != 0:
        print(failure_summary(output), file=sys.stderr)
        print(f"\nops/requirements.lock.txt does not resolve the way ops/Dockerfile.daemon installs it "
              f"(pip exit {result.returncode}), so the daemon image build would fail. Move the "
              f"conflicting pins together in ops/requirements.lock.txt.", file=sys.stderr)
        return 1
    would = next((line for line in output.splitlines() if line.startswith("Would install ")), "")
    print(f"ops/requirements.lock.txt resolves the way the daemon image installs it "
          f"({len(would.split()) - 2} packages, Python {have}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
