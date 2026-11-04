"""Read-only Docker installer ownership checks, shared by both preflights.

Names reserve a namespace; they do not prove ownership. Only the Compose
project label plus the recorded local environment permits an installer rerun.
No Docker output (which may contain credentials) is included in errors.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

PROJECT = "pseudolife-mcp"
BEGIN = "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"
END = "# <<< pseudolife-mcp install <<<"
FIELDS = ("PSEUDOLIFE_BANK_VOLUME", "PSEUDOLIFE_STATE_VOLUME", "POSTGRES_PASSWORD")


class Refusal(Exception):
    """A failed safety check with an actionable, non-destructive fix."""


def docker(*args: str) -> str:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("Cannot inspect Docker safely. Run docker info, fix Docker access, then rerun.") from None
    if result.returncode:
        raise Refusal("Cannot inspect Docker safely. Run docker info, fix Docker access, then rerun.")
    return result.stdout


def port_busy(port: int) -> bool:
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue  # IPv6 may be disabled.
        with sock:
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind((address, port))
            except OSError as error:
                if error.errno == errno.EADDRNOTAVAIL:
                    continue
                return True
    return False


def environment(path: Path) -> tuple[dict[str, str], bool]:
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    values = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in FIELDS:
            value = value.strip()
            if value[:1] in ("'", '"') and value[-1:] == value[:1]:
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            values[key] = value
    return values, BEGIN in text.splitlines() and END in text.splitlines()


def check(env_file: Path) -> None:
    ids = docker("ps", "-a", "--format", "{{.ID}}").split()
    containers = json.loads(docker("inspect", *ids)) if ids else []
    values, managed = environment(env_file)
    selected = {
        "PSEUDOLIFE_BANK_VOLUME": values.get("PSEUDOLIFE_BANK_VOLUME") or "pseudolife-mcp-bank",
        "PSEUDOLIFE_STATE_VOLUME": values.get("PSEUDOLIFE_STATE_VOLUME") or "pseudolife-mcp-state",
    }
    # Compose's exported settings take precedence over --env-file. Refuse a
    # disagreement rather than create one volume and attach another.
    for key, value in selected.items():
        if os.environ.get(key) and os.environ[key] != value:
            raise Refusal(f"{key} disagrees with ops/.env. Record its intended value in ops/.env and unset {key}, then rerun.")
    for c in containers:
        name = c["Name"].lstrip("/")
        project = (c.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project", "")
        ours = project == PROJECT
        if name.startswith("pseudolife-mcp-") and not ours:
            raise Refusal(f"Container {name} belongs to another stack (Compose project: {project or 'unlabelled'}). "
                          "Use that stack's own compose file to update it; for clients only, rerun with "
                          "--client-only --daemon-url <url> (PowerShell: -ClientOnly -DaemonUrl <url>). Nothing changed.")
        mounted = {m.get("Name") for m in c.get("Mounts", []) if m.get("Type") == "volume"}
        if mounted.intersection(selected.values()) and not ours:
            raise Refusal("The selected bank/state volume is attached to another stack. "
                          "Use that stack's own compose file; connect clients with --client-only --daemon-url <url> "
                          "(PowerShell: -ClientOnly -DaemonUrl <url>). Nothing changed.")
        if name == "pseudolife-mcp-postgres" and ours:
            pg_env = dict(v.split("=", 1) for v in c.get("Config", {}).get("Env", []) if "=" in v)
            expected = os.environ.get("POSTGRES_PASSWORD", values.get("POSTGRES_PASSWORD") or "pseudolife")
            # Compose reduces doubled dollars before passing the value to Docker.
            if "POSTGRES_PASSWORD" not in os.environ:
                expected = expected.replace("$$", "$")
            if pg_env.get("POSTGRES_PASSWORD") != expected:
                raise Refusal("POSTGRES_PASSWORD differs from the existing Postgres container; reusing its bank will fail authentication. "
                              "Restore POSTGRES_PASSWORD from the original ops/.env, then rerun. Nothing changed.")
            if mounted and selected["PSEUDOLIFE_BANK_VOLUME"] not in mounted:
                raise Refusal("PSEUDOLIFE_BANK_VOLUME does not name the existing Postgres bank. "
                              "Restore PSEUDOLIFE_BANK_VOLUME from the original ops/.env, then rerun. Nothing changed.")
    volumes = set(docker("volume", "ls", "--format", "{{.Name}}").splitlines())
    for key, legacy in (("PSEUDOLIFE_BANK_VOLUME", "ops_pseudolife_pgdata"),
                        ("PSEUDOLIFE_STATE_VOLUME", "ops_pseudolife_data")):
        if legacy in volumes and selected[key] not in volumes and not values.get(key):
            raise Refusal(f"Pre-rename volume {legacy} exists. Set {key}={legacy} in the original ops/.env, "
                          "keep its POSTGRES_PASSWORD, then rerun. Nothing changed.")
    for key, volume in selected.items():
        if volume in volumes and not (values.get(key) or managed):
            raise Refusal(f"Existing volume {volume} was not recorded by this install; its bank and password may belong to another install. "
                          "Restore the original ops/.env (including POSTGRES_PASSWORD and PSEUDOLIFE_BANK_VOLUME/STATE_VOLUME), "
                          "then rerun. Nothing changed; do not delete the volume.")
    for port in (8765, 5433):
        bound_by_us = any(
            c.get("State", {}).get("Running")
            and (c.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project") == PROJECT
            and any(str(port) == binding.get("HostPort")
                    for bindings in (c.get("NetworkSettings", {}).get("Ports") or {}).values()
                    for binding in (bindings or []))
            for c in containers
        )
        if port_busy(port) and not bound_by_us:
            raise Refusal(f"Port {port} is already in use by another service. "
                          f"Stop or reconfigure the service listening on {port}, then rerun. Nothing changed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        check(args.env_file)
    except (Refusal, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, Refusal) else "Cannot read installer ownership records. Restore the original ops/.env and check docker info, then rerun. Nothing changed."
        print(f"FAIL {message}", file=sys.stderr)
        return 1
    print("  OK   Docker stack, recorded volumes and ports belong to this install (or are free)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
