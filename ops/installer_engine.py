"""Docker-tier daemon engine for the installers: detect, prepare, accept.

Both installers call this so the engine labels and the accepted-selection
record keep the exact format ``pseudolife-mcp update`` verifies
(``pseudolife_memory/update_engine.py``).

- ``detect`` prints ``none``, ``python``, ``rust`` (an accepted release-mode
  Rust selection, also when its container was removed), ``rust-unaccepted``
  (a Rust container whose selection was never accepted) or ``rust-checkout``
  (one made by checkout-mode update).
- ``prepare --engine <compose engine>`` prints the Compose overlay files the
  stack is brought up with, one per line. For Rust it writes the installer's
  daemon overlay: the accepted selection on a rerun, or a new activation of
  the checkout's release, whose image it pulls and whose pinned models it
  provisions into the project's model volume before any container starts.
- ``accept`` records the installer's Rust activation once the daemon is
  ready at its release with its counters imported, as update records its
  own after its gate; a rerun on an accepted host changes nothing.

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


def accepted_release() -> dict | None:
    """The accepted release-mode Rust selection, read without a container."""
    try:
        selected = eng._selection(up.data_dir(), eng.RELEASE_ACCEPTED)
    except up.UpdateError:
        return None
    if (selected and selected["labels"].get(eng.ENGINE_LABEL) == "rust"
            and selected["labels"].get(eng.RELEASE_LABEL) and selected.get("image")):
        return selected
    return None


def detect(repo: Path | None = None) -> str:
    found = labels()
    if found is None:
        # `docker compose down` removes the container, not the selection. A
        # checkout-mode selection made after it supersedes it.
        release = accepted_release()
        try:
            # A release-mode switch back to Python leaves a python record: it counts too.
            recorded = eng._selection(up.data_dir(), eng.RELEASE_ACCEPTED)
        except up.UpdateError:
            recorded = None
        stamp = eng.release_accepted().stat().st_mtime_ns if recorded else -1
        newest, choice = stamp, "rust" if release else "python" if recorded else "none"
        for name, engine in ((eng.PYTHON_ACCEPTED, "python"), (eng.SELECTION, "rust-checkout")):
            path = repo / name if repo else None
            if path and path.is_file() and path.stat().st_mtime_ns > newest:
                newest, choice = path.stat().st_mtime_ns, engine
        return choice
    if found.get(eng.ENGINE_LABEL) != "rust":
        return "python"
    if found.get(eng.CHECKOUT_LABEL) and not found.get(eng.RELEASE_LABEL):
        return "rust-checkout"
    return "rust" if accepted(found) else "rust-unaccepted"


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


def preflight_disk() -> None:
    """The updater's 8 GiB floor, before a multi-GB pull and model download."""
    from pseudolife_memory import disk_space
    try:
        disk_space.host(up.data_dir())
        disk_space.docker_storage(up.docker_cmd(), [up.DAEMON_CONTAINER, up.PG_CONTAINER], None,
                                  lambda text: print(f"WARNING: {text}", file=sys.stderr))
    except up.UpdateError as exc:
        raise Refusal(f"{exc} Nothing was started.") from None


def provision(image: str) -> None:
    preflight_disk()
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


def refuse_foreign_unaccepted(found: dict | None, version: str) -> None:
    """Only this checkout's own unfinished Rust install may be redone here:
    anything else would replace a daemon without update's safeguards."""
    if (found and found.get(eng.ENGINE_LABEL) == "rust" and accepted(found) is None
            and found.get(eng.RELEASE_LABEL) != version):
        release = found.get(eng.RELEASE_LABEL) or "of unknown release"
        raise Refusal(f"The running Rust daemon {release} has no accepted selection, and this checkout installs "
                      f"{version}; the installer will not replace it. Remove the container "
                      "(docker rm -f pseudolife-mcp-daemon; the bank volumes stay) and rerun. Nothing was started.")


def prepare(repo: Path, engine: str) -> list[Path]:
    if engine == "python":
        found = labels()
        if found and found.get(eng.ENGINE_LABEL) == "rust":
            refuse_foreign_unaccepted(found, checkout_version(repo))
        return []
    if engine == "rust-checkout":
        # A rerun would rebuild it from the checkout with none of update's
        # backup, rollback or health gate: checkout mode manages this daemon.
        raise Refusal("This host runs a Rust daemon selected by checkout-mode update, which the installer does not "
                      f"rebuild. Update it with pseudolife-mcp update --checkout {repo}, or switch it back with "
                      f"pseudolife-mcp update --engine python --checkout {repo} and rerun the installer. "
                      "Nothing was started.")
    found = labels()
    kept = accepted_release() if found is None else accepted(found)
    if kept is not None:
        return [write_overlay({"image": kept["image"], "labels": kept["labels"]})]
    version = checkout_version(repo)
    refuse_foreign_unaccepted(found, version)
    image =f"{eng.RUST_IMAGE}:{version}"
    provision(image)
    return [write_overlay({"image": image, "labels": {eng.ENGINE_LABEL: "rust", eng.ACTIVATION_LABEL: secrets.token_hex(16),
                                                      eng.RELEASE_LABEL: version}})]


def health(url: str) -> dict | None:
    return up.fetch_json(url.rstrip("/") + "/health", timeout=3)


def image_schema(image: str) -> int | None:
    result = docker("run", "--rm", "--network", "none", "--entrypoint", "/usr/local/bin/pseudolife-mcp", image,
                    "update", "--schema-version")
    text = (result.stdout or "").strip()
    return int(text) if result.returncode == 0 and text.isdigit() else None


def bearer() -> str:
    """The bearer the running daemon was started with: its container
    environment is what Compose resolved, from ops/.env, the caller's
    environment or interpolation alike. Never printed."""
    from pseudolife_memory.principals import parse_token_map
    result = docker("inspect", "-f", "{{json .Config.Env}}", up.DAEMON_CONTAINER)
    try:
        entries = json.loads(result.stdout.strip() or "[]") if result.returncode == 0 else []
    except ValueError:
        entries = []
    values = dict(item.split("=", 1) for item in entries if isinstance(item, str) and "=" in item)
    return (values.get("PSEUDOLIFE_MCP_TOKEN")
            or next(iter(parse_token_map(values.get("PSEUDOLIFE_MCP_TOKENS"))), ""))


def accept(url: str = up.DEFAULT_DAEMON_URL) -> str:
    found = labels() or {}
    if not overlay_path().is_file() or found.get(eng.ENGINE_LABEL) != "rust" or not found.get(eng.RELEASE_LABEL):
        return "not-applicable"
    if accepted(found) is not None:
        return "unchanged"
    service = read_overlay()
    if service["labels"].get(eng.ACTIVATION_LABEL) != found.get(eng.ACTIVATION_LABEL):
        raise Refusal("The running daemon is not the one this install started; rerun the installer. "
                      "The Rust selection was not recorded.")
    unrecorded = " The Rust selection was not recorded."
    state = health(url) or {}
    release = service["labels"].get(eng.RELEASE_LABEL)
    if state.get("status") != "ok" or state.get("ready") is not True:
        raise Refusal("The Rust daemon is not ready yet; check docker logs pseudolife-mcp-daemon and rerun." + unrecorded)
    if state.get("version") != release:
        raise Refusal(f"The daemon reports version {state.get('version')}, not the {release} this install started; "
                      "check docker compose ps for the daemon's image and rerun." + unrecorded)
    if state.get("counters_import_required"):
        raise Refusal("The Rust daemon reports counters still to import from memory_state/weights.pt; install with "
                      "--engine python, then switch with pseudolife-mcp update --engine rust." + unrecorded)
    schema = image_schema(service["image"])
    if schema is None or state.get("schema") != schema:
        raise Refusal(f"The daemon reports schema {state.get('schema')}, and the image's own schema could not be read "
                      f"or differs ({schema}); check docker logs pseudolife-mcp-daemon and rerun." + unrecorded)
    token = bearer()
    if not token or "\r" in token or "\n" in token:
        # update verifies a Rust daemon with an authenticated read: without a
        # bearer the host could never be updated.
        raise Refusal("The Rust daemon runs without a bearer token, so pseudolife-mcp update could not verify it; "
                      "rerun the installer without --no-token (its token step names any failure), or use "
                      "--engine python." + unrecorded)
    if not eng.authenticated_read(url, token):
        raise Refusal("The Rust daemon refused an authenticated bank read with its own bearer; check docker logs "
                      "pseudolife-mcp-daemon and rerun." + unrecorded)
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
            print(detect(args.repo))
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
