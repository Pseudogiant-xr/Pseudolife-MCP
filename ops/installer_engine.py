"""Docker-tier daemon engine for the installers: detect, prepare, accept.

Both installers call this so the engine labels and the accepted-selection
record keep the exact format ``pseudolife-mcp update`` verifies
(``pseudolife_memory/update_engine.py``).

- ``detect`` prints ``none``, ``python``, ``rust`` (a release-mode Rust
  selection) or ``rust-checkout`` (one made by checkout-mode update).
- ``prepare --engine <compose engine>`` prints the Compose overlay files the
  stack is brought up with, one per line. For Rust it writes the installer's
  daemon overlay: the accepted selection on a rerun, or a new activation of
  the checkout's release, whose image it pulls and whose pinned models it
  provisions into the project's model volume before any container starts.
- ``accept`` records the installer's Rust activation after the health gate,
  as update does after its own; a rerun on an accepted host changes nothing.

Progress goes to stderr; stdout carries only the machine-readable answer.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pseudolife_memory import update_cli as up  # noqa: E402
from pseudolife_memory import update_engine as eng  # noqa: E402

OVERLAY = "docker-compose.engine-installer.yml"
PROJECT = "pseudolife-mcp"
MODELS_VOLUME = "pseudolife_rust_models"
MODELS_DIR = "/models"


class Refusal(Exception):
    pass


def docker(*args, capture=True, timeout=60):
    try:
        return subprocess.run([up.docker_cmd(), *args], capture_output=capture, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, check=False,
                              stdout=None if capture else sys.stderr)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("Cannot reach Docker; start Docker and rerun the installer. Nothing was started.") from None


def labels() -> dict | None:
    """The daemon container's labels, or None when there is no container."""
    result = docker("inspect", "-f", "{{json .Config.Labels}}", up.DAEMON_CONTAINER)
    if result.returncode:
        if "no such" in (result.stderr + result.stdout).lower():
            return None
        raise Refusal("Cannot inspect the existing daemon container; check docker info and rerun. Nothing was started.")
    try:
        value = json.loads(result.stdout.strip() or "null")
    except ValueError:
        value = None
    return value if isinstance(value, dict) else {}


def accepted(found: dict) -> dict | None:
    """The accepted release selection matching the running container, else None."""
    if not found.get(eng.RELEASE_LABEL):
        return None
    try:
        selected = eng._selection(up.data_dir(), eng.RELEASE_ACCEPTED)
    except up.UpdateError:
        return None
    if (selected and selected["labels"].get(eng.ENGINE_LABEL) == found.get(eng.ENGINE_LABEL)
            and found.get(eng.ACTIVATION_LABEL)
            and selected["labels"].get(eng.ACTIVATION_LABEL) == found.get(eng.ACTIVATION_LABEL)
            and selected["labels"].get(eng.RELEASE_LABEL) == found.get(eng.RELEASE_LABEL)):
        return selected
    return None


def detect() -> str:
    found = labels()
    if found is None:
        return "none"
    if found.get(eng.ENGINE_LABEL) != "rust":
        return "python"
    return "rust" if found.get(eng.RELEASE_LABEL) or not found.get(eng.CHECKOUT_LABEL) else "rust-checkout"


def checkout_version(repo: Path) -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', (repo / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    if not match or not up.plain_release(match.group(1)):
        raise Refusal("Cannot read a release version from this checkout's pyproject.toml; rerun with --engine python. "
                      "Nothing was started.")
    return match.group(1)


def overlay_path() -> Path:
    return up.data_dir() / "update-engine" / OVERLAY


def write_overlay(service: dict) -> Path:
    """A YAML daemon overlay: JSON strings are YAML scalars; ``!reset`` drops the build.

    The models volume matches ``docker-compose.rust-ghcr.yml``, which later
    release updates apply, so both keep the models outside the backed-up /data.
    """
    lines = ["# Written by the installer from the accepted engine selection; rerunning rewrites it.",
             "services:", f"  {up.DAEMON_SERVICE}:", f"    image: {json.dumps(service['image'])}",
             "    build: !reset null",
             "    environment:", f"      PSEUDOLIFE_DAEMON_MODELS_DIR: {json.dumps(MODELS_DIR)}",
             "    volumes:", f"      - {json.dumps(MODELS_VOLUME + ':' + MODELS_DIR)}",
             "    labels:"]
    lines += [f"      {json.dumps(key)}: {json.dumps(value)}" for key, value in sorted(service["labels"].items())]
    lines += ["volumes:", f"  {MODELS_VOLUME}: {{}}"]
    body = "\n".join(lines) + "\n"
    path = overlay_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_text(encoding="utf-8") != body:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(path)
    return path


def read_overlay() -> dict:
    """The service the installer overlay names (image and labels only)."""
    service = {"labels": {}}
    section = None
    for line in overlay_path().read_text(encoding="utf-8").splitlines():
        key, _, value = line.strip().partition(": ")
        if line.startswith("    ") and not line.startswith("      "):
            section = line.strip().rstrip(":")
        if line.startswith("    image: "):
            service["image"] = json.loads(value)
        elif section == "labels" and line.startswith("      ") and key.startswith('"'):
            service["labels"][json.loads(key)] = json.loads(value)
    return service


def provision(image: str) -> None:
    print(f"Pulling the Rust daemon image {image}...", file=sys.stderr)
    if docker("pull", image, capture=False, timeout=7200).returncode:
        raise Refusal(f"Cannot pull {image}: this release may not publish a Rust daemon image. Rerun with "
                      "--engine python (-Engine python) to install the Python daemon. Nothing was started.")
    volume = f"{PROJECT}_{MODELS_VOLUME}"
    found = docker("volume", "inspect", volume)
    if found.returncode:
        # Created the way Compose would, so `up` adopts it quietly.
        if docker("volume", "create", "--label", f"com.docker.compose.project={PROJECT}",
                  "--label", f"com.docker.compose.volume={MODELS_VOLUME}", volume).returncode:
            raise Refusal(f"Cannot create the model volume {volume}; check docker info and rerun. Nothing was started.")
    print("Provisioning the Rust daemon's pinned models (the first install downloads them)...", file=sys.stderr)
    if docker("run", "--rm", "-e", f"PSEUDOLIFE_DAEMON_MODELS_DIR={MODELS_DIR}", "-v", f"{volume}:{MODELS_DIR}",
              "--entrypoint", "pseudolife-daemon", image,
              "provision", "models", "--if-missing", capture=False, timeout=3600).returncode:
        raise Refusal("Cannot provision the Rust daemon's models; check network access and disk space, then rerun "
                      "(or rerun with --engine python). Nothing was started.")


def prepare(repo: Path, engine: str) -> list[Path]:
    if engine == "python":
        return []
    if engine == "rust-checkout":
        found = labels() or {}
        origin = Path(found.get(eng.CHECKOUT_LABEL, ""))
        if origin.resolve() != repo.resolve():
            raise Refusal(f"The Rust daemon was selected by checkout-mode update from {origin}; rerun that checkout's "
                          "installer, or switch with pseudolife-mcp update --engine python --checkout <repo>. "
                          "Nothing was started.")
        selection = repo / eng.SELECTION
        if not selection.is_file():
            raise Refusal("The checkout's Rust selection is missing; restore data/update-engine or switch back with "
                          "pseudolife-mcp update --engine python --checkout <repo>. Nothing was started.")
        return [repo / "ops/docker-compose.rust-daemon.yml", selection]
    found = labels() or {}
    kept = accepted(found) if found.get(eng.ENGINE_LABEL) == "rust" else None
    if kept is not None:
        return [write_overlay({"image": kept["image"], "labels": kept["labels"]})]
    version = checkout_version(repo)
    image = f"{eng.RUST_IMAGE}:{version}"
    provision(image)
    return [write_overlay({"image": image, "labels": {eng.ENGINE_LABEL: "rust", eng.ACTIVATION_LABEL: secrets.token_hex(16),
                                                      eng.RELEASE_LABEL: version}})]


def accept() -> str:
    found = labels() or {}
    if not overlay_path().is_file() or found.get(eng.ENGINE_LABEL) != "rust" or not found.get(eng.RELEASE_LABEL):
        return "not-applicable"
    if accepted(found) is not None:
        return "unchanged"
    service = read_overlay()
    if service["labels"].get(eng.ACTIVATION_LABEL) != found.get(eng.ACTIVATION_LABEL):
        raise Refusal("The running daemon is not the one this install started; rerun the installer. "
                      "The Rust selection was not recorded.")
    eng._write_overlay(eng.release_accepted(), service)
    return "accepted"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("detect", "prepare", "accept"))
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--engine", choices=("python", "rust", "rust-checkout"))
    args = parser.parse_args(argv)
    try:
        if args.action == "detect":
            print(detect())
        elif args.action == "prepare":
            if not args.repo or not args.engine:
                raise Refusal("prepare needs --repo and --engine.")
            for path in prepare(args.repo, args.engine):
                print(path)
        else:
            print(accept())
        return 0
    except (Refusal, OSError, ValueError, KeyError) as error:
        print(str(error) if isinstance(error, Refusal) else
              "Cannot read the installer's engine overlay; rerun the installer. Nothing was started.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())
