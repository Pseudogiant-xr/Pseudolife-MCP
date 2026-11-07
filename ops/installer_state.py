"""Private installer choices and successful-deployment fingerprints.

Only named non-secret choices or status words reach stdout. Compose configuration
and Docker inspection output stay inside the process, including on failures.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pseudolife_memory.client_config import _write_private  # noqa: E402
from pseudolife_memory.credentials import CredentialError  # noqa: E402

BEGIN = "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"
END = "# <<< pseudolife-mcp install <<<"
PREFIX = "PSEUDOLIFE_INSTALL_"
CHOICES = ("EXTRACTOR", "MODEL", "EXTRACTOR_URL", "SHIM_PORT", "CLIENT", "BANK_LOCATION",
           "INSTRUCTIONS", "CODEX_HOOKS", "CLAUDE_PLUGIN", "CLAUDE_LEGACY_HOOKS", "TRANSPORT", "NO_TOKEN",
           "INSTRUCTIONS_GEMINI", "INSTRUCTIONS_GENERIC", "GENERIC_TARGET_B64")
FINGERPRINTS = ("SOURCE_HASH", "BUILD_HASH", "COMPOSE_HASH", "IMAGES_HASH")
INPUT_HASHES = ("SOURCE_HASH", "BUILD_HASH", "COMPOSE_HASH")
RESOLVED_CHOICES = ("INSTRUCTIONS_GEMINI", "INSTRUCTIONS_GENERIC", "GENERIC_TARGET_B64")
MODES = {"sidecar", "claude-only", "claude-fallback", "openai-only", "openai-fallback", "endpoint", "endpoint-fallback"}


class Refusal(Exception):
    pass


def fields(lines):
    values = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Z][A-Z0-9_]+", key):
            value = value.strip()
            if value[:1] in ("'", '"') and value[-1:] == value[:1]:
                value = value[1:-1]
            values[key] = value
    return values


def read(path):
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    lines = text.splitlines()
    if lines.count(BEGIN) > 1 or lines.count(END) > 1 or lines.count(BEGIN) != lines.count(END):
        raise Refusal("ops/.env has incomplete or duplicate installer blocks. Restore its original single managed block, then rerun; nothing changed.")
    if BEGIN not in lines:
        return text, [], [], False
    if lines.index(END) < lines.index(BEGIN):
        raise Refusal("ops/.env has reversed installer markers. Restore its original managed block, then rerun; nothing changed.")
    start, finish = lines.index(BEGIN), lines.index(END)
    return text, lines[:start] + lines[finish + 1:], lines[start + 1:finish], True


def choices(path):
    text, _, block, managed = read(path)
    if not managed:
        return {}
    saved = fields(block)
    selected = {key: saved[PREFIX + key] for key in CHOICES if PREFIX + key in saved}
    selected.setdefault("BANK_LOCATION", "local")
    # An older open-loopback install must not acquire a token on a rerun.
    all_values = fields(text.splitlines())
    if "NO_TOKEN" not in selected and not (all_values.get("PSEUDOLIFE_MCP_TOKEN") or all_values.get("PSEUDOLIFE_MCP_TOKENS")):
        selected["NO_TOKEN"] = "1"
    if not selected.get("EXTRACTOR"):
        base = saved.get("PSEUDOLIFE_DREAM_BASE_URL", "")
        model = saved.get("PSEUDOLIFE_DREAM_MODEL", "")
        fallback = bool(saved.get("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL"))
        if not base:
            selected["EXTRACTOR"] = "sidecar"
        elif model and model != "extractor":
            parts = urlsplit(base)
            if parts.hostname == "host.docker.internal":
                base = urlunsplit((parts.scheme, "127.0.0.1" + (f":{parts.port}" if parts.port else ""), parts.path, "", ""))
            selected.update(EXTRACTOR="endpoint-fallback" if fallback else "endpoint", MODEL=model, EXTRACTOR_URL=base)
        else:
            # CLI families cannot be inferred from a port number: custom ports
            # and endpoint models called 'extractor' are legitimate. The shim's
            # own recorded model+port provide the exact family signal.
            all_values = fields(text.splitlines())
            port = urlsplit(base).port
            matches = []
            for family, mode in (("CLAUDE", "claude"), ("CODEX", "openai")):
                prefix = f"PSEUDOLIFE_{family}_SHIM_"
                if str(port) == all_values.get(prefix + "PORT") and all_values.get(prefix + "MODEL"):
                    matches.append((mode, all_values[prefix + "MODEL"]))
            if len(matches) == 1:
                mode, model = matches[0]
                selected.update(EXTRACTOR=mode + ("-fallback" if fallback else "-only"), MODEL=model, SHIM_PORT=str(port))
    writer = saved.get("PSEUDOLIFE_WRITER_ID")
    if "CLIENT" not in selected and writer in ("claude-code", "claude-desktop", "codex", "gemini"):
        selected["CLIENT"] = "claude" if writer == "claude-code" else writer
    if selected.get("EXTRACTOR", "").startswith(("claude-", "openai-")):
        family = "CLAUDE" if selected["EXTRACTOR"].startswith("claude-") else "CODEX"
        all_values = fields(text.splitlines())
        for setting in ("MODEL", "PORT"):
            value = all_values.get(f"PSEUDOLIFE_{family}_SHIM_{setting}")
            if value:
                selected["MODEL" if setting == "MODEL" else "SHIM_PORT"] = value
    for key, value in selected.items():
        if not re.fullmatch(r"[A-Za-z0-9._:/@+,\[\]-]*", value):
            raise Refusal("Recorded installer choices are invalid. Repeat the original installer flags to replace the managed block; nothing changed.")
        if key == "EXTRACTOR_URL" and value:
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                raise Refusal("The recorded extractor URL contains credentials. Remove URL credentials from ops/.env and keep API keys outside the managed block; nothing changed.")
    if selected.get("EXTRACTOR") and selected["EXTRACTOR"] not in MODES:
        raise Refusal("The recorded extractor mode is unknown. Repeat the original --extractor/-Extractor flag; nothing changed.")
    allowed = {"BANK_LOCATION": {"local", "shared"}, "INSTRUCTIONS": {"", "auto", "append", "skip"},
               "CODEX_HOOKS": {"auto", "manual", "plugin", "skip"}, "CLAUDE_PLUGIN": {"auto", "skip"},
               "CLAUDE_LEGACY_HOOKS": {"ask", "remove", "keep"}}
    allowed.update(TRANSPORT={"shim", "http"}, NO_TOKEN={"0", "1"},
                   INSTRUCTIONS_GEMINI={"append", "skip"}, INSTRUCTIONS_GENERIC={"append", "skip"})
    if any(key in selected and selected[key] not in options for key, options in allowed.items()):
        raise Refusal("Recorded installer choices are invalid. Restore the original managed block or repeat the original installer flags.")
    if "CLIENT" in selected and not set(selected["CLIENT"].split(",")).issubset({"claude", "claude-desktop", "codex", "gemini", "generic"}):
        raise Refusal("The recorded client list is invalid. Repeat the original --client/-Client flag; nothing changed.")
    if selected.get("SHIM_PORT") and not (selected["SHIM_PORT"].isdigit() and 0 < int(selected["SHIM_PORT"]) < 65536):
        raise Refusal("The recorded shim port is invalid. Repeat the original --shim-port/-ShimPort flag; nothing changed.")
    result = {key: selected[key] for key in CHOICES if key in selected and key != "GENERIC_TARGET_B64"}
    if selected.get("GENERIC_TARGET_B64"):
        try:
            value = selected["GENERIC_TARGET_B64"]
            target = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode("utf-8")
            if any(ord(char) < 32 for char in target):
                raise ValueError("control character")
            result["GENERIC_TARGET_JSON"] = json.dumps(target, ensure_ascii=True)
        except (ValueError, UnicodeError):
            raise Refusal("The recorded instruction target is invalid; restore the original managed block in ops/.env, then rerun.") from None
    return result


def write_file(path, content):
    content = content.replace("\r\n", "\n")
    if path.is_file() and path.read_text(encoding="utf-8-sig") == content:
        return "unchanged"
    _write_private(path, content.encode("utf-8"))
    return "changed"


def write_env(path, incoming, template=None):
    previous, kept, old_block, managed = read(path)
    if not path.is_file() and template:
        previous = template.read_text(encoding="utf-8-sig")
    new_lines = incoming.replace("\r\n", "\n").splitlines()
    if not new_lines or new_lines[0] != BEGIN or new_lines[-1] != END:
        raise Refusal("Installer managed-block input is incomplete; rerun the installer. Nothing changed.")
    old_values, new_values = fields(old_block), fields(new_lines[1:-1])
    for key in (*FINGERPRINTS, *RESOLVED_CHOICES):
        full = PREFIX + key
        if full in old_values and full not in new_values:
            new_lines.insert(-1, full + "=" + old_values[full])
            new_values[full] = old_values[full]
    if managed and old_values == new_values:
        return "unchanged"
    outside = "\n".join(kept) if managed else previous.rstrip("\r\n")
    return write_file(path, outside.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n")


def run(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=30, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("Cannot check installer state. Check docker info and git status in this checkout, then rerun.") from None
    if result.returncode:
        raise Refusal("Cannot check installer state. Check docker info and git status in this checkout, then rerun.")
    return result.stdout


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def deployment(repo, env_file, override, overlays=()):
    head = run(["git", "-C", str(repo), "rev-parse", "HEAD"]).strip()
    delta = run(["git", "-C", str(repo), "diff", "--binary", "HEAD"])
    untracked = run(["git", "-C", str(repo), "ls-files", "-z", "--others", "--exclude-standard"])
    extra = []
    for name in sorted(filter(None, untracked.split("\0"))):
        path = repo / name
        content = hashlib.sha256()
        if path.is_file():
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    content.update(chunk)
        extra.append([name, content.hexdigest()])
    source_hash = digest([str(repo.resolve()), head, delta, extra])
    compose = ["docker", "compose", "-p", "pseudolife-mcp", "--env-file", str(env_file), "-f", str(repo / "ops/docker-compose.yml")]
    if override.is_file():
        compose += ["-f", str(override)]
    for overlay in overlays:
        # The engine overlays the stack is brought up with (installer_engine.py).
        compose += ["-f", str(overlay)]
    config = json.loads(run([*compose, "config", "--format", "json"]))
    service_hashes = {}
    for line in run([*compose, "config", "--hash", "*"]).splitlines():
        name, value = line.split(None, 1)
        service_hashes[name] = value.strip()
    enabled_profiles = set(filter(None, os.environ.get("COMPOSE_PROFILES", "").split(",")))
    active = {name: service for name, service in config["services"].items()
              if not service.get("profiles") or enabled_profiles.intersection(service["profiles"])}
    names = {service.get("container_name", f"pseudolife-mcp-{name}-1"): name for name, service in active.items()}
    ids = run(["docker", "ps", "-a", "--format", "{{.ID}}"]).split()
    inventory = json.loads(run(["docker", "inspect", *ids])) if ids else []
    found = {c["Name"].lstrip("/"): c for c in inventory if c["Name"].lstrip("/") in names}
    for c in found.values():
        if (c.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project") != "pseudolife-mcp":
            raise Refusal("The configured containers belong to another Compose project. Use that stack's own compose file; nothing was started or removed.")
    complete = set(names) == set(found)
    running = complete and all(c.get("State", {}).get("Running") for c in found.values())
    applied = complete and all(
        service_hashes.get(names[name]) and
        (container.get("Config", {}).get("Labels") or {}).get("com.docker.compose.config-hash") == service_hashes[names[name]]
        for name, container in found.items())
    hashes = {"SOURCE_HASH": source_hash, "COMPOSE_HASH": digest(config),
              "BUILD_HASH": digest({name: {"build": service["build"], "image": service.get("image")}
                                    for name, service in active.items() if service.get("build")}),
              "IMAGES_HASH": digest({name: c.get("Image") for name, c in found.items()})}
    return hashes, complete, running, applied


def plan(repo, path, override, *, snapshot=False, overlays=()):
    hashes, complete, running, applied = deployment(repo, path, override, overlays)
    _, _, block, _ = read(path)
    old = fields(block)
    if not complete or any(old.get(PREFIX + key) != hashes[key] for key in ("SOURCE_HASH", "BUILD_HASH", "IMAGES_HASH")):
        action = "build"
    else:
        action = "unchanged" if running and applied and old.get(PREFIX + "COMPOSE_HASH") == hashes["COMPOSE_HASH"] else "start"
    return {"action": action, **hashes} if snapshot else action


def _same_inputs(expected, hashes):
    return isinstance(expected, dict) and all(expected.get(key) == hashes[key] for key in INPUT_HASHES)


def started(repo, path, override, *, expected, overlays=()):
    hashes, complete, _, applied = deployment(repo, path, override, overlays)
    if not _same_inputs(expected, hashes):
        raise Refusal("Checkout or Compose inputs changed during startup; rerun the installer with a stable checkout, and no success record has been saved.")
    if not complete or not applied:
        raise Refusal("The running containers do not match the requested Compose configuration; check docker compose logs and rerun, and no success record has been saved.")
    if expected.get("action") == "unchanged" and expected.get("IMAGES_HASH") != hashes["IMAGES_HASH"]:
        raise Refusal("Docker images changed while checking the existing stack; rerun the installer, and no success record has been saved.")
    return {**expected, "STARTED_IMAGES_HASH": hashes["IMAGES_HASH"]}


def record(repo, path, override, *, expected, instructions=None, generic_target=None, overlays=()):
    hashes, complete, running, applied = deployment(repo, path, override, overlays)
    if not _same_inputs(expected, hashes) or expected.get("STARTED_IMAGES_HASH") != hashes["IMAGES_HASH"]:
        raise Refusal("Checkout, Compose inputs or images changed after startup; rerun the installer with stable inputs, and no success record has been saved.")
    if not complete or not running or not applied:
        raise Refusal("The verified Docker stack changed during setup; rerun the installer, and no success record has been saved.")
    _, _, block, managed = read(path)
    if not managed:
        raise Refusal("The installer managed block is missing; restore the original ops/.env and rerun.")
    updates = {PREFIX + key: value for key, value in hashes.items()}
    for client, value in (instructions or {}).items():
        if client not in ("gemini", "generic") or value not in ("append", "skip"):
            raise Refusal("The resolved instruction choice is invalid; rerun the installer with explicit instruction flags.")
        updates[PREFIX + "INSTRUCTIONS_" + client.upper()] = value
    if generic_target:
        target = str(Path(generic_target).expanduser().absolute())
        updates[PREFIX + "GENERIC_TARGET_B64"] = base64.urlsafe_b64encode(target.encode("utf-8")).decode().rstrip("=")
    lines = [line for line in block if line.partition("=")[0] not in updates]
    lines += [key + "=" + value for key, value in updates.items()]
    return write_env(path, "\n".join([BEGIN, *lines, END]) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("choices", "write-env", "write-file", "plan", "started", "record", "target"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--path", type=Path)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--override", type=Path)
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--expected-stdin", action="store_true")
    parser.add_argument("--instruction-gemini", choices=("append", "skip"))
    parser.add_argument("--instruction-generic", choices=("append", "skip"))
    parser.add_argument("--generic-target")
    parser.add_argument("--overlay", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        if args.action == "target":
            print(json.dumps(str(args.path.expanduser().absolute()), ensure_ascii=True))
            return 0
        if args.action == "choices":
            for key, value in choices(args.env_file).items():
                print(key + "=" + value)
            return 0
        if args.action in ("write-env", "write-file"):
            incoming = sys.stdin.buffer.read().decode("utf-8")
            result = write_env(args.env_file, incoming, args.template) if args.action == "write-env" else write_file(args.path, incoming)
        elif args.action == "plan":
            result = plan(args.repo, args.env_file, args.override, snapshot=args.snapshot, overlays=args.overlay)
        else:
            if not args.expected_stdin:
                raise Refusal("The deployment plan is missing; rerun the installer from startup before recording success.")
            expected = json.loads(sys.stdin.buffer.read().decode("utf-8"))
            if args.action == "started":
                result = started(args.repo, args.env_file, args.override, expected=expected, overlays=args.overlay)
            else:
                instructions = {client: value for client, value in (("gemini", args.instruction_gemini),
                                ("generic", args.instruction_generic)) if value}
                result = record(args.repo, args.env_file, args.override, expected=expected,
                                instructions=instructions, generic_target=args.generic_target,
                                overlays=args.overlay)
        print(json.dumps(result, separators=(",", ":")) if isinstance(result, dict) else result)
        return 0
    except UnicodeError:
        print("Installer configuration must be UTF-8. Save ops/.env as UTF-8 while retaining its existing values, then rerun; nothing changed.", file=sys.stderr)
        return 1
    except CredentialError:
        print("Cannot secure installer configuration for this user. Use a checkout owned by your user, then rerun.", file=sys.stderr)
        return 1
    except (Refusal, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        message = str(error) if isinstance(error, Refusal) else "Cannot read installer records. Repeat the original installer flags or restore the original ops/.env, then rerun."
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())
