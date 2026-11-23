"""Acceptance probes for the published Linux arm64 client and Docker peer."""

from contextlib import contextmanager
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import tomllib
from threading import Event, Thread
from time import monotonic, sleep
from traceback import extract_tb

from . import matrix
from .hosted import (
    CLIENT, DAEMON_URL, DAEMONS, INSTALL, REDACTOR, STOCK_CODEX_NOTE, TOKEN_FILE, Failed, Peer, Profile, Receipt,
    Result, Unavailable, codex_probe, command, explain, get, native_path, prepare_codex, published_release,
    read_tail, registered_session, require, session, sha256, write_json,
)


LOOPBACK = "http://127.0.0.1:8765"
DAEMON = "pseudolife-mcp-daemon"
POSTGRES = "pseudolife-mcp-postgres"
DOCKER_FLAGS = ["--yes", "--client", "codex", "--extractor", "openai-only", "--firewall-rule", "no"]
CLIENT_FLAGS = ["--yes", "--client-only", "--client", "codex", "--claude-plugin", "skip",
                "--codex-hooks", "manual", "--codex-hook-trust", "yes", "--client-update-schedule", "no"]
BASELINE_MESSAGES = (
    (b"no matching manifest for linux/arm64", "no matching manifest for linux/arm64"),
    (b"does not match the detected host platform", "The image platform does not match the host platform."),
    (b"exec format error", "exec format error"),
    (b"Cannot pull ghcr.io/", "Cannot pull the release daemon image."),
    (b"The default Rust daemon is unavailable, so this install uses the Python daemon.",
     "The default Rust daemon is unavailable, so this install uses the Python daemon."),
    (b"Daemon engine: python", "Daemon engine: python"),
    (b"Daemon engine: rust", "Daemon engine: rust"),
    (b"no match for platform in manifest", "no match for platform in manifest"),
    (b"Preflight failed", "Preflight failed. <details redacted>"),
    (b"could not be fetched", "A release asset could not be fetched. <details redacted>"),
    (b"does not match the release's SHA256SUMS", "The release asset did not match SHA256SUMS."),
    (b"Permission denied", "Permission denied. <details redacted>"),
    (b"No space left on device", "No space left on device."),
    (b"address already in use", "Address already in use."),
    (b"Port 8765 is already in use", "Port 8765 is already in use by another service."),
    (b"Port 5433 is already in use", "Port 5433 is already in use by another service."),
    (b"Cannot read installer ownership records", "Cannot read installer ownership records."),
    (b"already exists", "A resource already exists. <details redacted>"),
    (b"foreign container", "The installer detected a foreign container."),
    (b"owner-only", "The installer reported an owner-only path requirement. <details redacted>"),
    (b"no native client is published", "The installer reported that no native client was published for the platform."),
    (b"Native client install (PSEUDOLIFE_SHIM_ENGINE=native)", "Native client install selected."),
    (b"Unattended install:", "Unattended install: no prompts; documented defaults apply."),
    (b"nothing was deployed", "Nothing was deployed. <details redacted>"),
    (b"rollback", "Rollback: <details redacted>"),
    (b"docker compose up -d --build", "docker compose up -d --build (checkout or images changed)..."),
    (b"daemon not healthy yet", "WARNING: daemon not healthy yet."),
    (b"Volumes ready:", "Volumes ready."),
    (b"AppArmor", "Docker reported an AppArmor error."),
    (b"not enough free space", "The installer reported insufficient free space."),
    (b"Healthy: http://127.0.0.1:8765/health", "Healthy: http://127.0.0.1:8765/health"),
    (b"What got wired, per agent:", "What got wired, per agent:"),
    (b"OpenAI Codex", "OpenAI Codex"),
    (b"Memory hooks", "Memory hooks: <details redacted>"),
    (b"MCP transport        registration FAILED", "MCP transport        registration FAILED - see the warning above and re-run"),
    (b"MCP transport", "MCP transport: <details redacted>"),
    (b"Server instructions", "Server instructions  automatic (MCP instructions field)"),
    (b"Verify runtime: codex mcp get pseudolife-memory",
     "Verify runtime: codex mcp get pseudolife-memory; run doctor from that command's environment."),
    (b"Runtime defaults", "Runtime defaults: <details redacted>"),
    (b"Standing file", "Standing file: <details redacted>"),
    (b"Agent board", "Agent board: <details redacted>"),
    (b'Verify: memory_dream(action="status")', 'Verify: memory_dream(action="status") - <endpoint redacted>'),
    (b"Note: dreams pause (and retry next sweep) whenever the shim is down or the CLI is logged out.",
     "Note: dreams pause (and retry next sweep) whenever the shim is down or the CLI is logged out."),
    (b"Note: Codex-served extraction quality is unmeasured", "Note: Codex-served extraction quality is unmeasured."),
    (b"Update later with one command:", "Update later with one command: <launcher> update."),
    (b"Keep this checkout: ops/.env is this install's configuration and credential source",
     "Keep this checkout: ops/.env is this install's configuration and credential source, and extractor autostart may reference its files."),
    (b"Optional ChatGPT access:", "Optional ChatGPT access: pseudolife-mcp tunnel setup (guided private key and app setup; docs/guide/tunnels.md)."),
    (b"Done. First session: tell your coding agent to remember something.",
     "Done. First session: tell your coding agent to remember something."),
)
CODEX_MESSAGES = (
    (b"Codex MCP registration was skipped because credential setup failed.",
     "WARNING: Codex MCP registration was skipped because credential setup failed."),
    (b"Codex was not registered:", "WARNING: Codex was not registered: <details redacted>."),
    (b"Codex authentication requires the stdio shim; HTTP fallback was not registered.",
     "WARNING: Codex authentication requires the stdio shim; HTTP fallback was not registered."),
    (b"Codex CLI has no env flag", "WARNING: this Codex CLI has no env flag; the stdio registration was skipped."),
    (b"Wired into Codex via the pseudolife-mcp shim", "Wired into Codex via the pseudolife-mcp shim."),
    (b"Codex credential setup failed.", "Codex credential setup failed."),
)
OFFICIAL_IMAGE = re.compile(
    r"(?:ghcr\.io/pseudogiant-xr/)?pseudolife-(?:daemon(?:-rust)?|pg|extractor)"
    r"(?::[A-Za-z0-9_.-]{1,64})?(?:@sha256:[a-f0-9]{64})?"
)
SENTINEL = "Hosted acceptance disposable bank marker"
# A legacy client installs through pipx, whose default Python on the Intel
# runner has no torch build (trial run 45131084076). The job's Python 3.12
# is one those releases supported.
LEGACY_PYTHON = {"PIPX_DEFAULT_PYTHON": sys.executable}
LEGACY_PYTHON_NOTE = "The legacy Python clients were installed with the job's Python 3.12."


def install(repo, profile, flags, *, overrides=None, engine="native", timeout=300):
    env = profile.env | {"PSEUDOLIFE_SHIM_ENGINE": engine} | (overrides or {})
    if engine == "native":
        require(all(shutil.which(name, path=env["PATH"]) is None
                    for name in ("python", "python3", "python3.12", "pip", "pip3", "py")),
                "Python and pip were absent from the native installer PATH.")
    else:
        env["PATH"] += os.pathsep + os.environ["PATH"]
    return command(["bash", "ops/install.sh", *flags], cwd=repo, env=env, timeout=timeout)


def docker(*args, env=None, timeout=45):
    use_env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
               "DOCKER_HOST": "unix:///var/run/docker.sock"} | (env or {})
    return command(["docker", *args], env=use_env, timeout=timeout)


class DockerFixture:
    """Inventory and release only the Docker objects added in this job."""

    def __init__(self):
        self.before = self.inventory()

    @staticmethod
    def inventory():
        resources = {}
        for resource, flags, assertion in (
            ("container", ("--all", "--no-trunc"), "The runner could not list Docker containers."),
            ("network", ("--no-trunc",), "The runner could not list Docker networks."),
            ("volume", (), "The runner could not list Docker volumes."),
        ):
            result = docker(resource, "ls", *flags, "--quiet")
            require(result.code == 0, assertion)
            resources[resource] = set(result.output.decode().splitlines())
        return resources

    def added(self):
        return {kind: names - self.before[kind] for kind, names in self.inventory().items()}

    def clean(self):
        added = self.added()
        for kind, names in added.items():
            if names:
                flags = ["-f"] if kind == "container" else []
                docker(kind, "rm", *flags, *sorted(names))
        return self.inventory() == self.before


def register_settings_secrets(repo):
    """Register every credential the stock installer wrote, before its output is kept."""
    settings = repo / "ops" / ".env"
    if not settings.is_file():
        return
    for line in settings.read_text(encoding="utf-8", errors="replace").splitlines():
        name, separator, value = line.partition("=")
        if separator and re.search(r"(?i)token|secret|passw|key|credential", name):
            REDACTOR.secret(value.strip().strip("\"'"))


def free_space(path):
    if shutil.disk_usage(path).free < 8 * 1024**3:
        raise Unavailable("The runner had less than 8 GiB of free space.")


def wait_stock_ports(row):
    start = monotonic()
    while monotonic() - start < 90:
        try:
            # The v0.24.0 preflight uses this exact bind test. TCP state
            # from our previous daemon can survive Docker object cleanup.
            for port in (8765, 5433):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    listener.bind(("127.0.0.1", port))
            row.steps.append({"command": "bind both stock Docker loopback ports", "exit_code": 0,
                              "seconds": round(monotonic() - start, 3),
                              "assertion": "The v0.24.0 stock ports were available."})
            return
        except OSError:
            sleep(1)
    raise Unavailable("The stock ports did not become available after cleaning the earlier fixture.")


def readiness(url):
    evidence, health = get(url + "/health")
    return evidence["status"] == 200 and health.get("status") == "ok" and health.get("ready", True)


def stop_units(profile):
    units = set()
    for name in ("config", ".config"):
        unit_folder = profile.folder / name / "systemd" / "user"
        units.update(path.name for path in unit_folder.glob("pseudolife-*") if path.is_file())
    if not units:
        return True
    command(["systemctl", "--user", "disable", "--now", *sorted(units)], env=profile.env)
    command(["systemctl", "--user", "daemon-reload"], env=profile.env)
    for unit in sorted(units):
        active = command(["systemctl", "--user", "is-active", unit], env=profile.env, stdout_only=True)
        enabled = command(["systemctl", "--user", "is-enabled", unit], env=profile.env, stdout_only=True)
        if active.code not in (3, 4) or active.output.strip() not in (b"inactive", b"unknown", b"failed"):
            return False
        if enabled.output.strip() not in (b"disabled", b"not-found", b"masked") or enabled.code == 0:
            return False
    return True


def installer_tail(output):
    lines = (line.strip() for line in output.splitlines())
    return [
        next((safe for fragment, safe in (*CODEX_MESSAGES, *BASELINE_MESSAGES)
              if fragment.lower() in line.lower()), "<unrecognized line redacted>")
        for line in list(filter(None, lines))[-18:]
    ]


def binfmt_status(folder=Path("/proc/sys/fs/binfmt_misc")):
    listed = command(["ls", "-1", folder])
    require(listed.code == 0, "The runner listed its binfmt registrations.")
    # Shell output can contain non-ASCII diagnostics. Inspect the kernel
    # entries directly so those bytes cannot hide a registered interpreter.
    handlers = [entry for entry in folder.iterdir() if entry.name not in ("status", "register")]
    amd64 = any(
        re.search(r"x86[-_]?64|amd64", handler.name, re.IGNORECASE)
        or re.search(rb"^interpreter .*?(x86[-_]?64|amd64)", handler.read_bytes(), re.IGNORECASE | re.MULTILINE)
        for handler in handlers
    )
    return {
        "command": "ls /proc/sys/fs/binfmt_misc", "exit_code": listed.code,
        "handlers": len(handlers), "amd64_handler_registered": amd64,
    }


def image_observation(container_id, existing_images):
    result = docker("inspect", container_id)
    require(result.code == 0, "The runner inspected each installed container.")
    container = result.json()[0]
    if not container["State"]["Running"]:
        return None
    inspected = docker("image", "inspect", container["Image"])
    require(inspected.code == 0, "The runner inspected the running container's image.")
    image = inspected.json()[0]
    labels = image["Config"].get("Labels") or {}
    reference = container["Config"]["Image"]
    role = container["Name"].lstrip("/")
    source = labels.get("pseudolife.build.source")
    entrypoint = (image["Config"].get("Entrypoint") or [""])[0]
    architecture = image.get("Architecture")
    operating_system = image.get("Os")
    engine = None
    if role == DAEMON:
        if entrypoint == "python":
            engine = "python"
        elif entrypoint.endswith("/provision-daemon-models"):
            engine = "rust"
    return {
        "container": role if role in (DAEMON, POSTGRES, "pseudolife-mcp-extractor") else "<unrecognized container>",
        "image": reference if OFFICIAL_IMAGE.fullmatch(reference) else "<unrecognized image reference>",
        "architecture": architecture if architecture in ("arm64", "amd64") else None,
        "os": operating_system if operating_system in ("linux", "windows") else None,
        "has_repository_digest": bool(image.get("RepoDigests")),
        "present_before_install": container["Image"].removeprefix("sha256:") in existing_images,
        "build_source": source if source in ("checkout", "release") else None,
        "engine_from_image": engine,
    }


def clean_stock(scope, profile, *, remove=()):
    checks = [lambda: stop_units(profile), scope.clean]
    for path in remove:
        checks.append(lambda path=path: shutil.rmtree(path) is None)
    cleaned = True
    for check in checks:
        try:
            cleaned = check() and cleaned
        except (Failed, OSError, ValueError):
            cleaned = False
    return cleaned


@contextmanager
def baseline(release, path, scratch, output):
    """Observe the tag's stock Docker install before any emulation."""
    binfmt = binfmt_status()
    if binfmt["amd64_handler_registered"]:
        raise Unavailable("The runner already had an amd64 emulator before the default install.")
    repo = release.checkout(scratch / "baseline-checkout")
    profile = Profile(scratch / "baseline-home", path, codex_started=False)
    docker_state = DockerFixture()
    images_before = docker("image", "ls", "--all", "--quiet", "--no-trunc")
    require(images_before.code == 0, "The runner inventoried its images before the install.")
    existing_images = {item.removeprefix("sha256:") for item in images_before.output.decode().splitlines()}
    receipt = None
    try:
        result = install(repo, profile, DOCKER_FLAGS, timeout=600)
        register_settings_secrets(repo)
        added = docker_state.added()
        images = [observed for container_id in sorted(added["container"])
                  if (observed := image_observation(container_id, existing_images)) is not None]
        http, health = get(LOOPBACK + "/health")
        engine = health.get("engine") or health.get("daemon_engine")
        source = (health.get("build") or {}).get("source")
        codex = command(["codex", "mcp", "get", "pseudolife-memory", "--json"], cwd=repo, env=profile.env)
        codex_lines = [line for fragment, line in CODEX_MESSAGES if fragment in result.output]
        selected_engine = [line for fragment, line in BASELINE_MESSAGES
                           if fragment in result.output and (line.startswith("Daemon engine:")
                                                             or "default Rust daemon is unavailable" in line)]
        receipt = {
            "platform": "linux-arm64",
            "command": f"{INSTALL} --yes --client codex --extractor openai-only --firewall-rule no",
            "exit_code": result.code, "seconds": result.seconds,
            "last_lines_sanitized": installer_tail(result.output),
            "installer_engine_lines": selected_engine,
            "binfmt_before_install": binfmt,
            "running_containers": images,
            "health": {
                "http_status": http["status"],
                "engine_reported": engine if engine in ("rust", "python") else None,
                "build_source": source if source in ("checkout", "release") else None,
                "ready": health.get("ready") is True,
            },
            "daemon_healthy": http["status"] == 200 and health.get("status") == "ok",
            "created_containers": len(added["container"]),
            "created_networks": len(added["network"]),
            "created_volumes": len(added["volume"]),
            "wrote_deployment_settings": (repo / "ops" / ".env").is_file(),
            "installed_native_runtime": profile.native_marker(release),
            "codex": {
                "configured_location": "CODEX_HOME/config.toml",
                "wrote_configuration": (profile.codex / "config.toml").is_file(),
                "mcp_get_exit_code": codex.code,
                "installer_lines": codex_lines,
            },
        }
        if result.code != 0:
            receipt["failure"] = explain(receipt["command"], result)
        receipt["fixture_notes"] = [STOCK_CODEX_NOTE]
        if not receipt["codex"]["wrote_configuration"]:
            receipt["codex"]["diagnostic"] = codex_probe(profile.env, repo)
        receipt["cleanup_verified"] = False
        write_json(output / "default-install.json", receipt, replace=True)
        yield receipt, result, repo, profile
    finally:
        primary = sys.exception()
        cleaned = clean_stock(docker_state, profile, remove=(profile.folder, repo))
        if receipt is not None:
            receipt["cleanup_verified"] = cleaned
            try:
                write_json(output / "default-install.json", receipt, replace=True)
            except OSError:
                if primary is None:
                    raise
        if primary is None:
            require(cleaned, "The disposable environment's Docker objects and user services were removed.")


def native_images(images):
    return (
        {image["container"] for image in images} == {DAEMON, POSTGRES}
        and all(image["architecture"] == "arm64" and image["os"] == "linux" and
                not image["has_repository_digest"] for image in images)
        and any(image["container"] == DAEMON and image["build_source"] == "checkout" for image in images)
    )


def stock_connection(release, profile, repo, scratch, engine, *, legacy=False):
    if engine not in matrix.DAEMON_ENGINES:
        raise Unavailable("The stock daemon engine was not identified from the running image.")
    if legacy:
        require(profile.installed_launcher.is_file(), "The stock v0.24.0 installer installed its launcher.")
    else:
        require(profile.native_marker(release), "The stock installer activated the verified native client.")
    values = (repo / "ops" / ".env").read_text().splitlines()
    token = next((line.split("=", 1)[1] for line in values if line.startswith("PSEUDOLIFE_MCP_TOKEN=")), "")
    require(bool(token), "The installer generated a disposable owner token.")
    REDACTOR.secret(token)
    token_file = scratch / "peer.token"
    with token_file.open("x", encoding="utf-8") as stream:
        token_file.chmod(0o600)
        stream.write(token + "\n")
    env = profile.env | {TOKEN_FILE: str(token_file), DAEMON_URL: LOOPBACK, "PSEUDOLIFE_MCP_NO_SPAWN": "1"}
    if legacy:
        env["PATH"] += os.pathsep + os.environ["PATH"]
    peer = Peer(DAEMONS[1], LOOPBACK, token_file, str(profile.installed_launcher), env, repo, engine)
    require(peer.sample()["status"] == 200, "The stock daemon accepted its disposable token.")
    return peer


@contextmanager
def stock_peer(row, release, path, scratch):
    free_space(scratch)
    scope = DockerFixture()
    repo = release.checkout(scratch / "peer-checkout")
    profile = Profile(scratch / "peer-home", path, codex_started=False)
    legacy = row.scenario == "upgrade-and-rollback"
    installer_engine = "python" if legacy else "native"
    installer_label = "bash ops/install.sh <stock v0.24.0 Docker install>" if legacy else f"{INSTALL} <stock Docker>"
    try:
        result = install(repo, profile, DOCKER_FLAGS, engine=installer_engine, timeout=600)
        register_settings_secrets(repo)
        row.observe(installer_label, result, result.code == 0 and readiness(LOOPBACK),
                    "The tagged stock installer left a healthy daemon.",
                    template="<legacy-installer> <0.24-python-fixture>" if legacy else None,
                    required=False)
        row.steps[-1]["last_lines_sanitized"] = installer_tail(result.output)
        row.steps[-1]["codex_installer_lines"] = [line for fragment, line in CODEX_MESSAGES
                                                  if fragment in result.output]
        require(result.code == 0 and readiness(LOOPBACK), "The tagged stock installer left a healthy daemon.")
        images = [observed for container_id in sorted(scope.added()["container"])
                  if (observed := image_observation(container_id, set())) is not None]
        if not native_images(images):
            raise Unavailable("The stock installation did not supply the identified native arm64 checkout stack.")
        row.daemon = DAEMONS[1]
        row.daemon_engine = next(image["engine_from_image"] for image in images if image["container"] == DAEMON)
        peer = stock_connection(release, profile, repo, scratch, row.daemon_engine, legacy=legacy)
        yield peer, profile
    finally:
        primary = sys.exception()
        row.cleanup_failed = True
        row.cleanup_failed = not clean_stock(scope, profile)
        if primary is None and row.status != "fail":
            require(not row.cleanup_failed, "The disposable environment's Docker objects and user services were removed.")


def paired_install(receipt, repo, profile, peer, *, template=None, try_schedule=False):
    start = monotonic()
    with session(peer.owner_executable, [], peer.owner_env, peer.checkout) as mcp:
        mcp.tool("memory_agents", action="update", project="hosted-acceptance", status="fixture")
    deadline = monotonic() + 75
    while monotonic() < deadline:
        _, health = get(peer.url + "/health")
        if health.get("bank"):
            break
        sleep(2)
    else:
        raise Unavailable("The first-session pairing workaround did not publish the bank fingerprint.")
    receipt.observe("MCP memory_agents update and health fingerprint", Result(mcp.process.returncode, b"", monotonic() - start),
                    mcp.process.returncode == 0, "The known pairing prerequisite completed.")
    result = command([peer.owner_executable, "invite", f"hosted-{receipt.scenario}", "--json"],
                     env=peer.owner_env, cwd=peer.checkout)
    code = result.json().get("code")
    if isinstance(code, str):
        REDACTOR.secret(code)
    receipt.observe(f"{CLIENT} invite <fixture> --json", result,
                    result.code == 0 and isinstance(code, str), "The disposable daemon issued a pairing code.")
    flags = ["--daemon-url", peer.url, "--pairing-code", code]
    if try_schedule:
        flags.extend(["--client-update-schedule", "yes"])
    result = peer.during(receipt, lambda: live_install(peer, repo, profile, flags))
    receipt.observe(f"{INSTALL} <client-only pairing>", result, result.code == 0,
                    "The published client-only installer completed.", template=template)
    receipt.steps.append({"command": f"{INSTALL} <codex-hook-verification>", "exit_code": result.code,
                          "assertion": "The installer reported trusted and verified Codex hooks.",
                          "observed": b"Memory hooks         trusted and verified" in result.output})
    if try_schedule:
        receipt.steps.append({"command": f"{INSTALL} --client-update-schedule yes", "exit_code": result.code,
                              "assertion": "The installer reported the documented macOS scheduling limit.",
                              "observed": b"the daily job needs systemd (Linux) or Task Scheduler (Windows)" in result.output})


def live_install(peer, repo, profile, flags, *, overrides=None, engine="native"):
    return install(repo, profile, CLIENT_FLAGS + flags, overrides=overrides, engine=engine)


def first_session(receipt, profile, repo, template=None):
    start = monotonic()
    listed = command(["codex", "mcp", "get", "pseudolife-memory", "--json"], env=profile.env, cwd=repo)
    receipt.observe("codex mcp get pseudolife-memory --json", listed, listed.code == 0,
                    "Codex reported the installed stdio registration.")
    with registered_session(profile, repo) as mcp:
        mcp.tool("memory_agents", action="update", project="hosted-acceptance", status="fixture")
        mcp.tool("memory_search", query=SENTINEL, top_k=1)
    result = Result(mcp.process.returncode, b"", round(monotonic() - start, 3))
    receipt.observe("MCP initialize, board update and bank read", result, result.code == 0,
                    "The registered shim initialized and completed a board write and a memory read.",
                    template=template)


def clean_install(receipt, release, repo, profile, peer, scratch):
    first, session_template = matrix.commands(receipt.scenario, receipt.platform)
    if peer.kind == DAEMONS[1]:
        first_session(receipt, profile, repo, session_template)
        return
    paired_install(receipt, repo, profile, peer, template=first)
    require(profile.native_marker(release), "The installed runtime matched the candidate archive.")
    first_session(receipt, profile, repo, session_template)


def changed_files(before, after):
    """Profile-relative names of fingerprinted files a step added, removed or changed."""
    return sorted(REDACTOR.text(str(name)) for name in set(before) | set(after) if before.get(name) != after.get(name))


def changed_settings(before, after):
    """Dotted names of Codex settings a step added, removed or changed. Values stay out."""
    def flatten(table, prefix=()):
        for key, value in table.items():
            if isinstance(value, dict):
                yield from flatten(value, (*prefix, key))
            else:
                yield ".".join((*prefix, key)), value

    try:
        old, new = (dict(flatten(tomllib.loads(text))) if text else {} for text in (before, after))
    except tomllib.TOMLDecodeError:
        return ["<unreadable Codex configuration>"]
    return sorted(REDACTOR.text(name) for name in set(old) | set(new) if old.get(name) != new.get(name))


def upgrade(receipt, release, repo, path, peer, scratch):
    templates = iter(matrix.commands(receipt.scenario, receipt.platform))
    for tag in ("v0.23.1", "v0.24.0"):
        if tag == release.tag:
            raise Unavailable("The selected candidate is also a required legacy starting tag.")
        previous = scratch / tag
        checkout = command(["git", "clone", "--quiet", "--depth=1", "--branch", tag,
                            "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git", previous], timeout=120)
        require(checkout.code == 0, "The previous published installer could be checked out.")
        profile = Profile(scratch / f"legacy-{tag}", path)
        flags = ["--daemon-url", peer.url, "--token-file", str(peer.token_file)]
        result = live_install(peer, previous, profile, flags, engine="python", overrides=LEGACY_PYTHON)
        receipt.fixture_notes.append(LEGACY_PYTHON_NOTE)
        receipt.observe("Published legacy installer <client-only>", result, result.code == 0,
                        "The previous release installed its Python client.", template=next(templates))
        if b"Error installing pseudolife-mcp from spec" in result.output:
            # The old installer reports a failed pipx install and still exits
            # 0. The upgrade then has no starting client to move from (Intel
            # macOS in trial run 45135001202).
            raise Unavailable(f"The {tag} Python client could not be installed on this runner, so the upgrade "
                              "had no starting client.", explain("Published legacy installer <client-only>", result))
        first_session(receipt, profile, repo)
        python_env = profile.env | {"PATH": profile.env["PATH"] + os.pathsep + os.environ["PATH"],
                                    "PSEUDOLIFE_SHIM_ENGINE": "native"}
        result = peer.during(receipt, lambda: command(
            [profile.launcher(), "update", "--clients-only", "--tag", release.version],
            env=python_env | profile.registration().get("env", {}), cwd=repo, timeout=300))
        receipt.observe(f"{CLIENT} update --clients-only --tag <release>", result,
                        result.code == 0 and profile.native_marker(release),
                        "The previous client handed off to the verified native runtime.", template=next(templates))
        first_session(receipt, profile, repo)
        before = profile.fingerprint()
        config = profile.codex / "config.toml"
        config_before = config.read_text(encoding="utf-8") if config.is_file() else ""
        result = live_install(peer, repo, profile, flags)
        after = profile.fingerprint()
        receipt.observe(f"{INSTALL} <rerun>", result, result.code == 0 and before == after,
                        "The native installer rerun kept the installed runtime and registration.", template=next(templates),
                        required=False)
        receipt.steps[-1]["changed_files"] = changed_files(before, after)
        if config.is_file():
            receipt.steps[-1]["changed_codex_settings"] = changed_settings(
                config_before, config.read_text(encoding="utf-8"))
        require(result.code == 0 and before == after,
                "The native installer rerun kept the installed runtime and registration.")
        result = live_install(peer, repo, profile, flags, engine="python", overrides=LEGACY_PYTHON)
        restored = profile.fingerprint()
        receipt.observe("PSEUDOLIFE_SHIM_ENGINE=python bash ops/install.sh <rollback>", result,
                        result.code == 0 and restored != before and not profile.native_marker(release),
                        "The documented installer rollback replaced the native runtime.", template=next(templates))
        first_session(receipt, profile, repo)


def daemon_snapshot(peer, profile):
    inspected = docker("inspect", DAEMON)
    container = inspected.json()[0] if inspected.code == 0 else {}
    inspected = docker("inspect", POSTGRES)
    database = inspected.json()[0] if inspected.code == 0 else {}

    def volumes(service):
        return sorted(mount["Name"] for mount in service.get("Mounts", []) if mount.get("Type") == "volume")

    _, health = get(peer.url + "/health")
    version = health.get("version")
    config = peer.checkout / "ops" / ".env"
    return {
        "id": container.get("Id"), "image": container.get("Image"),
        "state_volumes": volumes(container), "database_volumes": volumes(database),
        "config": sha256(config) if config.is_file() else None,
        "runtime": profile.fingerprint(),
        "version": version if isinstance(version, str) and re.fullmatch(matrix.VERSION, version) else None,
        "authenticated": peer.sample()["status"] == 200,
    }


def docker_rollback(receipt, peer, previous, report):
    details = report.get("rollback")
    if not isinstance(details, dict):
        raise Failed("The update changed the daemon without a structured rollback record.")
    tag = details.get("tag")
    if not isinstance(tag, str) or not OFFICIAL_IMAGE.fullmatch(tag) or "pre-update" not in tag:
        raise Failed("The updater did not identify a usable rollback tag.")
    image = docker("image", "inspect", "--format", "{{.Id}}", tag)
    require(image.code == 0 and image.output.strip().decode() == previous["image"],
            "The reported rollback tag matched the original daemon image.")
    image_tag = details.get("image_tag")
    env = {}
    config = peer.checkout / "ops"
    recipe = ["-f", str(config / "docker-compose.yml")]
    if (config / "docker-compose.override.yml").is_file():
        recipe.extend(["-f", str(config / "docker-compose.override.yml")])
    if isinstance(image_tag, str) and OFFICIAL_IMAGE.fullmatch(image_tag):
        result = docker("tag", tag, image_tag)
        receipt.observe("docker tag <reported rollback> <reported checkout image>", result, result.code == 0,
                        "The documented checkout rollback restored the previous tag.")
    elif registry_tag := re.fullmatch(r"ghcr\.io/pseudogiant-xr/pseudolife-daemon:([A-Za-z0-9_.-]{1,64})", tag):
        recipe.extend(["-f", str(config / "docker-compose.ghcr.yml")])
        env["PSEUDOLIFE_IMAGE_TAG"] = registry_tag[1]
    else:
        raise Failed("The recorded rollback did not match a documented Compose route.")
    result = docker("compose", "--env-file", str(config / ".env"), "-p", "pseudolife-mcp",
                    *recipe, "up", "-d", "--no-deps", "--no-build", "pseudolife-daemon",
                    env=env, timeout=180)
    deadline = monotonic() + 90
    while result.code == 0 and monotonic() < deadline and not readiness(peer.url):
        sleep(2)
    restored = docker("inspect", "--format", "{{.Image}}", DAEMON)
    _, health = get(peer.url + "/health")
    receipt.observe("docker compose <documented rollback> up -d --no-deps --no-build pseudolife-daemon",
                    result, result.code == 0 and restored.code == 0
                    and restored.output.strip().decode() == previous["image"]
                    and health.get("version") == previous["version"] and peer.sample()["status"] == 200,
                    "The previous image and authenticated daemon returned.")


def stock_upgrade(receipt, release, repo, profile, peer, scratch):
    previous = daemon_snapshot(peer, profile)
    require(previous["version"] == "0.24.0" and previous["authenticated"],
            "The stock v0.24.0 daemon served the upgrade's starting bank.")
    result_file = scratch / "update-result.json"
    result = peer.during(receipt, lambda: command(
        [peer.owner_executable, "update", "--tag", release.version, "--json", "--result-file", result_file],
        env=peer.owner_env | {"PSEUDOLIFE_SHIM_ENGINE": "native"}, cwd=repo, timeout=600, stdout_only=True))
    report = result.json()
    if not isinstance(report, dict):
        report = {}
    lines = report.get("steps")
    output = "\n".join(line for line in lines if isinstance(line, str)).encode() if isinstance(lines, list) else result.output
    saved_exit = result_file.read_bytes().strip() if result_file.is_file() else b""
    after = daemon_snapshot(peer, profile)
    database_unchanged = bool(previous["database_volumes"]) and after["database_volumes"] == previous["database_volumes"]
    upgraded = result.code == 0 and after["version"] == release.version and after["authenticated"]
    receipt.observe(f"{CLIENT} update --tag <selected release>", result, upgraded,
                    "Release-mode update reached the selected version and served an authenticated read.",
                    template="<native-opt-in> <0.24-python-fixture>", required=False)
    receipt.steps[-1].update({
        "last_lines_sanitized": installer_tail(output),
        "saved_exit_code": int(saved_exit) if re.fullmatch(rb"-?[0-9]{1,4}", saved_exit) else None,
        "reported_daemon_version": after["version"],
        "previous_daemon_still_serves": after["image"] == previous["image"]
        and after["version"] == previous["version"] and after["authenticated"],
        "daemon_container_unchanged": after["id"] == previous["id"],
        "database_volumes_unchanged": database_unchanged,
        "state_volumes_unchanged": bool(previous["state_volumes"]) and after["state_volumes"] == previous["state_volumes"],
        "deployment_settings_unchanged": after["config"] == previous["config"],
        "client_runtime_unchanged": after["runtime"] == previous["runtime"],
    })
    daemon_changed = after["image"] != previous["image"] or after["version"] != previous["version"]
    if upgraded or daemon_changed or not after["authenticated"]:
        require(database_unchanged, "The bank volume was unchanged, so no database rollback was needed.")
        docker_rollback(receipt, peer, previous, report)
    require(upgraded, "The unmodified v0.24.0 release updater reached a healthy selected release.")
    raise Unavailable("The stock v0.24.0 transition ran. The ledger also requires the separate v0.23.x client start.")


def ordinary(receipt, release, repo, profile, peer, scratch):
    if peer.kind == DAEMONS[1]:
        start = monotonic()
        with session(peer.owner_executable, [], peer.owner_env, repo) as mcp:
            mcp.tool("memory_agents", action="update", project="hosted-acceptance", status="fixture")
            mcp.tool("memory_search", query=SENTINEL, top_k=1)
        observed = Result(mcp.process.returncode, b"", round(monotonic() - start, 3))
        receipt.observe("MCP initialize, board update and bank read", observed, observed.code == 0,
                        "The published shim completed a board write and a memory read.")
        env, launcher = peer.owner_env, peer.owner_executable
    else:
        paired_install(receipt, repo, profile, peer, try_schedule=receipt.platform.startswith("macos"))
        first_session(receipt, profile, repo)
        env, launcher = profile.env | profile.registration().get("env", {}), profile.launcher()
    templates = set(matrix.commands(receipt.scenario, receipt.platform))
    failures = []
    for name, args, allowed, description in (
        ("connect", [peer.url, "--client", "codex", "--token-file", env[TOKEN_FILE], "--yes", "--json"],
         {0}, "Native connect accepted the disposable daemon and token."),
        ("doctor", ["--host", "codex"], {0}, "Native doctor accepted the installed client configuration."),
        ("console", ["--no-open", "--json"], {0}, "Native Console sign-in created a local session link."),
        ("update", ["--clients-only", "--tag", release.version, "--json"], {0, 3},
         "The native release updater accepted the installed release."),
    ):
        invoke = lambda: command([launcher, name, *args], cwd=repo, env=env, timeout=180)
        result = peer.during(receipt, invoke) if name == "update" else invoke()
        if name == "console":
            # The sign-in link carries a one-time code; register it before
            # any of this output can reach a receipt.
            fields = result.json() if isinstance(result.json(), dict) else {}
            for key, value in fields.items():
                if isinstance(value, str) and re.search(r"(?i)code|url|link|token|secret", key):
                    REDACTOR.secret(value)
        label = f"{CLIENT} {name} <fixture-options>"
        receipt.observe(label, result, result.code in allowed, description,
                        template=label if label in templates else None, required=False)
        if result.code not in allowed:
            failures.append(name)
            if b"docker is installed but did not answer" in result.output:
                # The updater's own Docker question, asked the same way, so the
                # receipt shows what Docker answered (trial run 45138027385).
                asked = command(["docker", "inspect", "-f", "{{.Id}}", DAEMON], cwd=repo, env=env)
                receipt.steps.append({"command": "docker inspect -f {{.Id}} pseudolife-mcp-daemon <diagnostic>",
                                      "exit_code": asked.code, "observed": True,
                                      "assertion": "What Docker answered the updater's daemon check.",
                                      "failure": explain("docker inspect <diagnostic>", asked)})
    if failures:
        raise Failed("One or more installed ordinary-use commands failed.")
    raise Unavailable("The live tunnel step needs a public tunnel, which these hosted runs may not open; "
                      "the other ordinary-use steps were observed.")


@contextmanager
def paused_download(release):
    requested = Event()
    released = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            if self.path == "/SHA256SUMS":
                data = (release.folder / "SHA256SUMS").read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path == f"/{release.archive.name}":
                requested.set()
                released.wait(20)
            else:
                self.send_error(404)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requested
        finally:
            released.set()
            server.shutdown()
            thread.join()


def cancel_download(release, repo, profile, flags):
    start = monotonic()
    with paused_download(release) as (url, requested), tempfile.TemporaryFile() as transcript:
        env = profile.env | {"PSEUDOLIFE_SHIM_ENGINE": "native", "PSEUDOLIFE_NATIVE_ARCHIVE_URL": url}
        with subprocess.Popen(["bash", "ops/install.sh", *flags],
                              cwd=repo, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                              stdout=transcript, stderr=subprocess.STDOUT) as process:
            observed = requested.wait(20)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT if observed else signal.SIGKILL)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    observed = False
        output = read_tail(transcript)
    return Result(process.returncode, output, round(monotonic() - start, 3)), observed


@contextmanager
def displaced_archive(release, profile, scratch):
    cached = profile.folder / ".pseudolife-mcp" / "downloads" / release.version / release.archive.name
    backup = scratch / "verified-archive-before-fault"
    if cached.is_file():
        cached.rename(backup)
    try:
        yield
    finally:
        if backup.is_file():
            backup.replace(cached)


def failure(receipt, release, repo, profile, peer, scratch):
    local = peer.kind == DAEMONS[1]
    if local:
        flags = DOCKER_FLAGS
        docker_before = DockerFixture.inventory()
    else:
        paired_install(receipt, repo, profile, peer)
        flags = CLIENT_FLAGS + ["--daemon-url", peer.url, "--token-file", str(peer.token_file)]

    def unchanged():
        return (before == profile.fingerprint()
                and (not local or DockerFixture.inventory() == docker_before)
                and readiness(peer.url))

    before = profile.fingerprint()
    templates = matrix.commands(receipt.scenario, receipt.platform)
    with displaced_archive(release, profile, scratch):
        for index, (defect, diagnostic) in enumerate((
            ("corrupt", b"does not match the release"),
            ("missing", b"although its SHA256SUMS lists it"),
        )):
            assets = scratch / defect
            assets.mkdir()
            shutil.copyfile(release.folder / "SHA256SUMS", assets / "SHA256SUMS")
            if defect == "corrupt":
                (assets / release.archive.name).write_bytes(b"not a release archive\n")
            result = install(repo, profile, flags, overrides={"PSEUDOLIFE_NATIVE_ARCHIVE_URL": str(assets)}, timeout=600)
            receipt.observe(f"{INSTALL} <{defect}-archive>", result,
                            result.code not in (0, 124) and diagnostic in result.output and unchanged(),
                            "The installer rejected the fault before changing the working runtime.",
                            template=templates[index])
        result, observed = cancel_download(release, repo, profile, flags)
    receipt.observe(f"{INSTALL} <cancelled-install>", result,
                    observed and result.code != 0 and unchanged(),
                    "Interrupting the archive download preserved the working runtime.", template=templates[2])

    locks = list(profile.folder.rglob(".native-update.lock"))
    require(len(locks) == 1, "The native runtime lock was present.")
    with locks[0].open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = command([profile.installed_launcher, "runtime", "install", "--archive", release.archive,
                          "--sha256sums", release.folder / "SHA256SUMS", "--version", release.version],
                         cwd=repo, env=profile.env)
        receipt.observe(f"{CLIENT} runtime install <locked-runtime>", result,
                        result.code not in (0, 124) and b"another native runtime update" in result.output
                        and unchanged(),
                        "The installed lock prevented concurrent activation.", template=templates[3])
    result = install(repo, profile, flags, timeout=600)
    receipt.observe(f"{INSTALL} <retry-options>", result,
                    result.code == 0 and profile.native_marker(release) and readiness(peer.url),
                    "The clean retry selected the verified runtime and left a healthy daemon.", template=templates[-1])
    if local:
        require(peer.sample()["status"] == 200, "The daemon served an authenticated read after retry.")
    else:
        first_session(receipt, profile, repo)
    raise Unavailable("The launch and interruption fault probes are not injected yet; the archive, "
                      "cancellation, lock and retry steps were observed.")


def bank_recovery(receipt, release, repo, profile, peer, scratch):
    with session(peer.owner_executable, [], peer.owner_env, repo) as mcp:
        mcp.tool("memory_store", text=SENTINEL, source="hosted-acceptance")
    backup = scratch / "backup"
    backup.mkdir(mode=0o700)
    result = command([peer.owner_executable, "backup", "--out", backup],
                     cwd=peer.checkout, env=peer.owner_env, timeout=180)
    dumps = list(backup.glob("*.sql.gz"))
    archives = list(backup.glob("*.tgz"))
    templates = matrix.commands(receipt.scenario, receipt.platform)
    receipt.observe(f"{CLIENT} backup <fixture-options>", result,
                    result.code == 0 and len(dumps) == len(archives) == 1,
                    "Native backup produced one database dump and one state archive.", template=templates[0])
    post_backup = "Hosted acceptance change made after backup"
    with session(peer.owner_executable, [], peer.owner_env, repo) as mcp:
        mcp.tool("memory_store", text=post_backup, source="hosted-acceptance")
        found = mcp.tool("memory_search", query=post_backup, top_k=5)
        require(post_backup in json.dumps(found.get("entries", [])), "The disposable bank was changed after backup.")
    result = command([peer.owner_executable, "restore", "--backup-file", dumps[0],
                      "--state-archive", archives[0], "--yes"],
                     cwd=peer.checkout, env=peer.owner_env, timeout=240)
    receipt.observe(f"{CLIENT} restore <fixture-options>", result, result.code == 0 and readiness(peer.url),
                    "Native restore replayed the backup and restarted the disposable daemon.", template=templates[1])
    with session(peer.owner_executable, [], peer.owner_env, repo) as mcp:
        found = mcp.tool("memory_search", query=SENTINEL, top_k=5)
        require(SENTINEL in json.dumps(found.get("entries", [])), "The published client read the restored bank marker.")
        found = mcp.tool("memory_search", query=post_backup, top_k=5)
        # The query can also appear in response metadata. Inspect actual hits.
        require(post_backup not in json.dumps(found.get("entries", [])),
                "The restored bank no longer contained the post-backup change.")
    raise Unavailable("Backup and restore were exercised on the stock arm64 bank. Move and legacy import-state witnesses remain pending.")


def source_recovery(receipt, release, repo, profile, peer, scratch):
    """Back up the test peer's bank with the file-tier backup a client can run."""
    with session(peer.owner_executable, [], peer.owner_env, repo) as mcp:
        mcp.tool("memory_store", text=SENTINEL, source="hosted-acceptance")
    backup = scratch / "backup"
    backup.mkdir(mode=0o700)
    env = peer.owner_env | {"PATH": os.pathsep.join((peer.tools, peer.owner_env["PATH"])),
                            "PSEUDOLIFE_MCP_DATABASE_URL": peer.database_url}
    result = command([peer.owner_executable, "backup", "--data-dir", peer.data_dir, "--out", backup],
                     cwd=peer.checkout, env=env, timeout=180)
    dumps = list(backup.glob("*.sql.gz"))
    archives = list(backup.glob("*.tar.gz"))
    receipt.observe(f"{CLIENT} backup <fixture-options>", result,
                    result.code == 0 and len(dumps) == len(archives) == 1,
                    "Native backup produced one database dump and one state archive.",
                    template=matrix.commands(receipt.scenario, receipt.platform)[0])
    raise Unavailable("Restore replaces only a Docker bank, and no published daemon image runs as one on this "
                      "runner.")


def run_case(row, probe, *, confirm_pass=True):
    try:
        probe()
        if confirm_pass:
            row.status = "pass"
            row.note = matrix.SCENARIOS[row.scenario].outcome
    except Unavailable as error:
        row.note = str(error)
        row.explain(error)
    except Failed as error:
        row.status = "fail"
        row.note = str(error)
        row.explain(error)
    except (OSError, ValueError, KeyError, IndexError) as error:
        row.status = "fail"
        row.note = diagnostic_note(error)
    if row.cleanup_failed:
        if row.status != "fail":
            row.note = "The disposable environment could not be cleaned up and verified."
        row.status = "fail"


def diagnostic_note(error):
    sources = ("hosted.py", "hosted_linux.py", "hosted_macos.py", "hosted_source.py", "matrix.py", "sanitize.py")
    frame = next((frame for frame in reversed(extract_tb(error.__traceback__))
                  if Path(frame.filename).name in sources), None)
    diagnostic = (f" ({type(error).__name__}, {Path(frame.filename).name} line {frame.lineno})"
                  if frame is not None else "")
    return f"The harness could not complete the observation{diagnostic}."


def probe_stock(release, provenance, result, repo, profile, scratch, by_name, output):
    """The stock clean install row. The client rows use the test peer instead."""
    clean = by_name["clean-install"]
    clean.fixture_notes.append(STOCK_CODEX_NOTE)
    ready = result.code == 0 and provenance["daemon_healthy"]
    clean.observe(f"{INSTALL} <stock Docker>", result, ready,
                  "The tagged stock installer exited 0 and left a healthy daemon.",
                  template=matrix.commands(clean.scenario, clean.platform)[0], required=False)
    if not ready:
        clean.status = "fail"
        clean.note = ("The stock installer exited non-zero." if provenance["daemon_healthy"]
                      else "The stock installer did not leave a healthy daemon.")
        return
    if not native_images(provenance["running_containers"]):
        clean.note = "The stock installer did not produce the identified native arm64 checkout stack."
        return
    try:
        engine = next(image["engine_from_image"] for image in provenance["running_containers"]
                      if image["container"] == DAEMON)
        clean.daemon, clean.daemon_engine = DAEMONS[1], engine
        peer = stock_connection(release, profile, repo, scratch, engine)
    except Unavailable as error:
        clean.note = str(error)
        return
    except Failed as error:
        clean.status, clean.note = "fail", str(error)
        return
    clean.daemon, clean.daemon_engine = peer.kind, peer.engine
    scope = scratch / clean.scenario
    scope.mkdir()
    run_case(clean, lambda: clean_install(clean, release, repo, profile, peer, scope))
    clean.checkpoint(output, release)


# The maintainer's decision of 23 November 2026: the daemon is not supported
# on Linux arm64 for now (no arm64 daemon image, issue #1872), so these
# client rows are not run here. A client on arm64 against a daemon on a
# supported host is not ruled out. The two stock rows stay as witnesses.
CLIENT_ROWS = ("ordinary-native-use", "failure-and-retry", "bank-recovery")
NOT_SUPPORTED = ("Not run: the daemon is not supported on Linux arm64 for now, because no arm64 daemon image is "
                 "published (issue #1872).")


def run(tag, platform, output):
    receipts = [Receipt(scenario, platform) for scenario in matrix.SCENARIOS]
    # Set first, so no later failure can relabel rows that never run.
    stock = [receipt for receipt in receipts if receipt.scenario not in CLIENT_ROWS]
    for receipt in receipts:
        if receipt.scenario in CLIENT_ROWS:
            receipt.note = NOT_SUPPORTED
    release = None
    cleaned = False
    before_docker = None
    try:
        require(platform == "linux-arm64" and os.environ.get("GITHUB_ACTIONS") == "true"
                and os.uname().machine == "aarch64", "The job ran on the selected disposable Linux arm64 runner.")
        with tempfile.TemporaryDirectory(prefix="acceptance-") as folder:
            scratch = Path(folder)
            REDACTOR.path(scratch, "<scratch>")
            free_space(scratch)
            release = published_release(tag, platform, scratch / "release")
            path = native_path(scratch, prepare_codex(platform, scratch))
            before_docker = DockerFixture()
            by_name = {row.scenario: row for row in receipts}
            with baseline(release, path, scratch, output) as (provenance, result, repo, profile):
                probe_stock(release, provenance, result, repo, profile, scratch, by_name, output)
                for row in receipts:
                    if row.scenario == "clean-install":
                        row.checkpoint(output, release)

            def do_upgrade():
                if release.tag == "v0.24.0":
                    raise Unavailable("The selected release is also the required upgrade starting tag.")
                scope = scratch / "upgrade"
                scope.mkdir()
                old = published_release("v0.24.0", platform, scope / "earlier-release")
                row = by_name["upgrade-and-rollback"]
                wait_stock_ports(row)
                with stock_peer(row, old, path, scope) as (peer, profile):
                    run_case(row, lambda: stock_upgrade(row, release, peer.checkout, profile, peer, scope))
                    row.checkpoint(output, release)

            run_case(by_name["upgrade-and-rollback"], do_upgrade, confirm_pass=False)
            by_name["upgrade-and-rollback"].checkpoint(output, release)
            cleaned = before_docker.clean()
    except Unavailable as error:
        for receipt in stock:
            if receipt.note == "The scenario was not selected.":
                receipt.note = str(error)
                receipt.explain(error)
    except Failed as error:
        for receipt in stock:
            if receipt.status != "fail":
                receipt.status = "fail"
                receipt.note = str(error)
                receipt.explain(error)
    except (OSError, ValueError, KeyError, IndexError, matrix.RecordError) as error:
        for receipt in stock:
            if receipt.status != "fail":
                receipt.status = "fail"
                receipt.note = diagnostic_note(error)
    except Exception as error:  # noqa: BLE001 - a traceback would print unfiltered output
        for receipt in stock:
            if receipt.status != "fail":
                receipt.status = "fail"
                receipt.note = diagnostic_note(error)
    finally:
        for receipt in receipts:
            if receipt.status == "pass" and not cleaned:
                receipt.status = "fail"
                receipt.note = "The disposable environment could not be cleaned up and verified."
                receipt.cleanup_failed = True
            receipt.write(output, release, cleaned)
            print(REDACTOR.text(f"{receipt.scenario}: {receipt.status}. {receipt.note}"))
    return int(any(receipt.status == "fail" for receipt in receipts))
