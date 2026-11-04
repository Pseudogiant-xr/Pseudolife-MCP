"""Read-only Docker installer ownership checks, shared by both preflights.

Names reserve a namespace; they do not prove ownership. Only the Compose
project label plus the recorded local environment permits an installer rerun.
No Docker output (which may contain credentials) is included in errors.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ops import preflight_lite
from pseudolife_memory.principals import env_auth  # noqa: E402

PROJECT = "pseudolife-mcp"
BEGIN = "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"
END = "# <<< pseudolife-mcp install <<<"
FIELDS = ("PSEUDOLIFE_BANK_VOLUME", "PSEUDOLIFE_STATE_VOLUME", "POSTGRES_PASSWORD",
          "PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS")


class Refusal(Exception):
    """A failed safety check with an actionable, non-destructive fix."""


def docker(*args: str, input: str | None = None, failure: str | None = None) -> str:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=20, check=False, input=input)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("Cannot inspect Docker safely. Run docker info, fix Docker access, then rerun.") from None
    if result.returncode:
        raise Refusal(failure or "Cannot inspect Docker safely. Run docker info, fix Docker access, then rerun.")
    return result.stdout


def port_busy(port: int) -> bool:
    # Both shipped publications bind IPv4 loopback only. An IPv6-only
    # listener on ::1 can coexist and is not an installer collision.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", port))
        except OSError:
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


def resolved_environment(path: Path) -> dict[str, str]:
    # Compose owns dotenv syntax, interpolation and exported-value precedence.
    # Keep all its output private; only this allowlist is used internally.
    ops = Path(__file__).resolve().parent
    source = path if path.is_file() else ops / ".env.example"
    output = docker("compose", "--env-file", str(source), "-f", str(ops / "docker-compose.yml"),
                    "config", "--environment",
                    failure="Cannot resolve ops/.env with Docker Compose. Run docker compose --env-file ops/.env -f ops/docker-compose.yml config --quiet, fix the reported configuration, then rerun. Nothing changed.")
    return {key: value for line in output.splitlines()
            for key, sep, value in [line.partition("=")] if sep and key in FIELDS}


def volume_names(values: dict[str, str]) -> tuple[str, str]:
    names = tuple(values.get(key) or default for key, default in (
        ("PSEUDOLIFE_BANK_VOLUME", "pseudolife-mcp-bank"),
        ("PSEUDOLIFE_STATE_VOLUME", "pseudolife-mcp-state")))
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", name) for name in names):
        raise Refusal("Invalid bank/state volume name. Set literal Docker volume names in ops/.env, then rerun. Nothing changed.")
    return names


def host_path(value: str) -> Path:
    # A checkout used through WSL and native Windows is still one checkout.
    if os.name == "nt":
        match = re.fullmatch(r"/(?:mnt/)?([A-Za-z])/(.*)", value)
        if match:
            value = match[1] + ":/" + match[2]
    elif os.environ.get("WSL_DISTRO_NAME"):
        match = re.fullmatch(r"([A-Za-z]):[\\/](.*)", value)
        if match:
            value = "/mnt/" + match[1].lower() + "/" + match[2].replace("\\", "/")
    return Path(value).expanduser()


def same_path(first: Path, second: Path) -> bool:
    try:
        return first.samefile(second)
    except OSError:
        return os.path.normcase(str(first.resolve())) == os.path.normcase(str(second.resolve()))


def authentication(values: dict[str, str]) -> tuple[dict[str, str], str | None, bool]:
    mapping, singular = env_auth(values)
    # Keep a malformed nonempty map distinct from an open configuration: the
    # daemon fails closed when no entry survives parsing.
    malformed = bool(values.get("PSEUDOLIFE_MCP_TOKENS")) and not mapping
    return mapping, singular, malformed


def check_daemon_authority(container: dict, env_file: Path, values: dict[str, str]) -> None:
    labels = container.get("Config", {}).get("Labels") or {}
    working = labels.get("com.docker.compose.project.working_dir")
    labelled_env = labels.get("com.docker.compose.project.environment_file")
    origin_matches = False
    if labelled_env:
        original_env = host_path(labelled_env)
        if not original_env.is_file() and "," in labelled_env:
            originals = [host_path(value) for value in labelled_env.split(",") if value]
            if len(originals) > 1 and all(path.is_file() for path in originals):
                raise Refusal("This stack uses multiple environment files; maintain it with pseudolife-mcp update, which retains that Compose context, instead of this single-env-file installer. Nothing changed.")
        if not original_env.is_file():
            raise Refusal(f"The recorded install configuration ({original_env}) is missing; restore it with its original checkout before rerunning. Nothing changed.")
        if not same_path(original_env, env_file):
            raise Refusal(f"This stack uses a different install directory ({original_env.parent}); rerun the installer from that directory with its original ops/.env. Nothing changed.")
        origin_matches = True
        if working and not host_path(working).is_dir():
            print("WARN The recorded Compose working directory is unavailable; keep the original ops/.env and use this checkout's installer to refresh the configuration paths.", file=sys.stderr)
    elif working:
        original = host_path(working)
        if not original.is_dir():
            raise Refusal(f"The recorded install directory ({original}) is missing; restore that checkout and its original ops/.env before rerunning. Nothing changed.")
        if not same_path(original, env_file.parent):
            raise Refusal(f"This stack uses a different install directory ({original}); rerun the installer from that checkout with its original ops/.env. Nothing changed.")
        origin_matches = True
    existing = dict(value.split("=", 1) for value in container.get("Config", {}).get("Env", []) if "=" in value)
    live, requested = authentication(existing), authentication(values)
    if live != requested and (not origin_matches or not any(requested)):
        raise Refusal("This configuration does not retain the existing daemon authentication; restore the original ops/.env instead of minting replacement credentials, then rerun. Nothing changed.")


def check(env_file: Path) -> None:
    ids = docker("ps", "-a", "--format", "{{.ID}}").split()
    containers = json.loads(docker("inspect", *ids)) if ids else []
    recorded, managed = environment(env_file)
    values = resolved_environment(env_file)
    selected = dict(zip(FIELDS[:2], volume_names(values)))
    for c in containers:
        name = c["Name"].lstrip("/")
        project = (c.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project", "")
        ours = project == PROJECT
        if name.startswith("pseudolife-mcp-") and not ours:
            raise Refusal(f"Container {name} belongs to another stack (Compose project: {project or 'unlabelled'}). "
                          "Use that stack's own compose file to update it; for clients only, rerun with "
                          "--client-only --daemon-url <url> (PowerShell: -ClientOnly -DaemonUrl <url>). Nothing changed.")
        if name == "pseudolife-mcp-daemon" and ours:
            check_daemon_authority(c, env_file, values)
        mounted = {m.get("Name") for m in c.get("Mounts", []) if m.get("Type") == "volume"}
        if mounted.intersection(selected.values()) and not ours:
            raise Refusal("The selected bank/state volume is attached to another stack. "
                          "Use that stack's own compose file; connect clients with --client-only --daemon-url <url> "
                          "(PowerShell: -ClientOnly -DaemonUrl <url>). Nothing changed.")
        if name == "pseudolife-mcp-postgres" and ours:
            pg_env = dict(v.split("=", 1) for v in c.get("Config", {}).get("Env", []) if "=" in v)
            expected = values.get("POSTGRES_PASSWORD") or "pseudolife"
            if c.get("State", {}).get("Running"):
                # Postgres initdb can trust localhost, including TCP loopback.
                # Its bridge address uses the same host rule as the daemon.
                addresses = [network.get("IPAddress") for network in
                             c.get("NetworkSettings", {}).get("Networks", {}).values()
                             if network.get("IPAddress")]
                if not addresses:
                    raise Refusal("Cannot verify Postgres authentication without its Docker bridge address. Run docker info and start the original stack with its own compose file, then rerun. Nothing changed.")
                # stdin keeps the credential out of argv and preserves dollars.
                # Config.Env records initialization, not a later ALTER USER.
                docker("exec", "-i", c["Id"], "sh", "-c",
                       'PGPASSWORD=$(cat; printf x); PGPASSWORD=${PGPASSWORD%x}; export PGPASSWORD; '
                       'exec psql -h "$1" -U pseudolife -d pseudolife_memory -Atqc "SELECT 1"',
                       "preflight", addresses[0],
                       input=expected,
                       failure="Postgres did not accept the configured POSTGRES_PASSWORD. Restore POSTGRES_PASSWORD from the original ops/.env, then rerun. Nothing changed.")
            elif pg_env.get("POSTGRES_PASSWORD") != expected:
                raise Refusal("The stopped Postgres container records a different initialization password; a later password rotation cannot be checked while stopped. "
                              "Start the original stack with its own compose file, then rerun this preflight. Nothing changed.")
            if mounted and selected["PSEUDOLIFE_BANK_VOLUME"] not in mounted:
                raise Refusal("PSEUDOLIFE_BANK_VOLUME does not name the existing Postgres bank. "
                              "Restore PSEUDOLIFE_BANK_VOLUME from the original ops/.env, then rerun. Nothing changed.")
    volumes = set(docker("volume", "ls", "--format", "{{.Name}}").splitlines())
    for key, legacy in (("PSEUDOLIFE_BANK_VOLUME", "ops_pseudolife_pgdata"),
                        ("PSEUDOLIFE_STATE_VOLUME", "ops_pseudolife_data")):
        if legacy in volumes and selected[key] not in volumes and not recorded.get(key):
            raise Refusal(f"Pre-rename volume {legacy} exists. Set {key}={legacy} in the original ops/.env, "
                          "keep its POSTGRES_PASSWORD, then rerun. Nothing changed.")
    for key, volume in selected.items():
        default = "pseudolife-mcp-bank" if key == FIELDS[0] else "pseudolife-mcp-state"
        if volume in volumes and not (recorded.get(key) or (managed and volume == default)):
            raise Refusal(f"Existing volume {volume} was not recorded by this install; its bank and password may belong to another install. "
                          "Restore the original ops/.env (including POSTGRES_PASSWORD and PSEUDOLIFE_BANK_VOLUME/STATE_VOLUME), "
                          "then rerun. Nothing changed; do not delete the volume.")
    if message := preflight_lite.refusal(
            containers, configured=env_file.is_file(),
            recorded_volumes=managed and all(volume in volumes for volume in selected.values()),
            port_busy=port_busy):
        raise Refusal(message)
    for port in (8765, 5433):
        bound_by_us = any(
            c.get("State", {}).get("Running")
            and (c.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project") == PROJECT
            and any(str(port) == binding.get("HostPort")
                    and binding.get("HostIp", "") in ("", "127.0.0.1", "0.0.0.0")
                    for publication, bindings in (c.get("NetworkSettings", {}).get("Ports") or {}).items()
                    if publication.endswith("/tcp")
                    for binding in (bindings or []))
            for c in containers
        )
        if port_busy(port) and not bound_by_us:
            raise Refusal(f"Port {port} is already in use by another service. "
                          f"Stop or reconfigure the service listening on {port}, then rerun. Nothing changed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--volume-names", action="store_true", help="print only resolved bank and state volume names")
    args = parser.parse_args()
    try:
        if args.volume_names:
            print(*volume_names(resolved_environment(args.env_file)), sep="\n")
            return 0
        check(args.env_file)
    except (Refusal, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, Refusal) else "Cannot read installer ownership records. Restore the original ops/.env and check docker info, then rerun. Nothing changed."
        print(f"FAIL {message}", file=sys.stderr)
        return 1
    print("  OK   Docker stack, recorded volumes and ports belong to this install (or are free)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
