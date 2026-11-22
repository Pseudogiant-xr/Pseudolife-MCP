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
import subprocess
import tempfile
from threading import Event, Thread
from time import monotonic, sleep
from traceback import extract_tb

from . import matrix
from .hosted import (
    CLIENT, DAEMON_URL, DAEMONS, INSTALL, TOKEN_FILE, Failed, Peer, Profile, Receipt,
    Result, Unavailable, command, get, native_path, prepare_codex, published_release,
    registered_session, require, session, write_json,
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


def free_space(path):
    if shutil.disk_usage(path).free < 8 * 1024**3:
        raise Unavailable("The runner had less than 8 GiB of free space.")


def readiness(url):
    evidence, health = get(url + "/health")
    return evidence["status"] == 200 and health.get("status") == "ok" and health.get("ready", True)


def stop_units(profile):
    units = set()
    for name in ("config", ".config"):
        unit_folder = profile.folder / name / "systemd" / "user"
        units.update(path.name for path in unit_folder.glob("pseudolife-*") if path.is_file())
    if units:
        command(["systemctl", "--user", "disable", "--now", *sorted(units)], env=profile.env)
        command(["systemctl", "--user", "daemon-reload"], env=profile.env)


def installer_tail(output):
    lines = (line.strip() for line in output.splitlines())
    return [
        next((safe for fragment, safe in (*CODEX_MESSAGES, *BASELINE_MESSAGES)
              if fragment.lower() in line.lower()), "<unrecognized line redacted>")
        for line in list(filter(None, lines))[-18:]
    ]


def binfmt_status():
    folder = Path("/proc/sys/fs/binfmt_misc")
    listed = command(["ls", "-1", folder])
    require(listed.code == 0, "The runner could not list binfmt registrations.")
    entries = listed.output.decode("ascii").splitlines()
    handlers = [folder / name for name in entries if name not in ("status", "register")]
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
    require(result.code == 0, "The runner could not inspect an installed container.")
    container = result.json()[0]
    if not container["State"]["Running"]:
        return None
    inspected = docker("image", "inspect", container["Image"])
    require(inspected.code == 0, "The runner could not inspect the running container's image.")
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


def baseline(release, path, scratch, output):
    """Observe the tag's stock Docker install before any emulation."""
    binfmt = binfmt_status()
    if binfmt["amd64_handler_registered"]:
        raise Unavailable("The runner already had an amd64 emulator before the default install.")
    repo = release.checkout(scratch / "baseline-checkout")
    profile = Profile(scratch / "baseline-home", path)
    docker_state = DockerFixture()
    images_before = docker("image", "ls", "--all", "--quiet", "--no-trunc")
    require(images_before.code == 0, "The runner could not inventory images before the install.")
    existing_images = {item.removeprefix("sha256:") for item in images_before.output.decode().splitlines()}
    try:
        result = install(repo, profile, DOCKER_FLAGS, timeout=600)
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
    finally:
        stop_units(profile)
        require(docker_state.clean(), "Default-install Docker objects were removed.")
        shutil.rmtree(profile.folder)
        shutil.rmtree(repo)
    receipt["cleanup_verified"] = True
    write_json(output / "default-install.json", receipt)


def enable_amd64():
    update = command(["sudo", "-n", "apt-get", "-qq", "update"], timeout=90)
    install_qemu = command(["sudo", "-n", "apt-get", "-qq", "install", "-y", "qemu-user-static",
                            "binfmt-support"], timeout=120) if update.code == 0 else update
    binfmt = Path("/proc/sys/fs/binfmt_misc/qemu-x86_64")
    if install_qemu.code != 0 or not binfmt.is_file() or not binfmt.read_text().startswith("enabled"):
        raise Unavailable("Signed Ubuntu QEMU packages did not enable amd64 execution.")


@contextmanager
def linux_peer(release, path, scratch):
    free_space(scratch)
    scope = DockerFixture()
    repo = release.checkout(scratch / "peer-checkout")
    profile = Profile(scratch / "peer-home", path)
    emulated = {"DOCKER_DEFAULT_PLATFORM": "linux/amd64"}
    try:
        owner_env = profile.env | emulated
        result = install(repo, profile, DOCKER_FLAGS + ["--engine", "rust"],
                         overrides=emulated, timeout=540)
        if result.code != 0 or not readiness(LOOPBACK):
            raise Unavailable("The emulated published Docker peer did not become ready.")
        for name in (DAEMON, POSTGRES):
            image = docker("inspect", "--format", "{{.Image}}", name)
            require(image.code == 0, "The expected Docker container existed.")
            platform = docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}} {{json .RepoDigests}}",
                              image.output.strip().decode())
            require(platform.code == 0 and platform.output.startswith(b"linux/amd64 "),
                    "The running Docker container used the amd64 platform.")
            if name == DAEMON and (release.folder / "RELEASE-READY.json").is_file():
                expected = json.loads((release.folder / "RELEASE-READY.json").read_bytes())["images"]["pseudolife-daemon-rust"]
                require(expected.encode() in platform.output, "The running daemon matched the published release image digest.")
        registration = profile.registration()
        owner_env |= registration.get("env", {})
        token = Path(owner_env[TOKEN_FILE])
        peer = Peer(DAEMONS[1], LOOPBACK, token, profile.launcher(), owner_env, repo)
        require(peer.sample()["status"] == 200, "The published peer accepted its disposable token.")
        yield peer
    finally:
        stop_units(profile)
        require(scope.clean(), "Emulated peer Docker objects were removed.")


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
    if try_schedule and receipt.platform != "linux-arm64":
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
    paired_install(receipt, repo, profile, peer, template=first)
    require(profile.native_marker(release), "The installed runtime matched the candidate archive.")
    first_session(receipt, profile, repo, session_template)
    if receipt.platform == "linux-arm64":
        raise Unavailable("Published client installation worked. The stock local arm64 Docker stack and default extractor are still unproven.")


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
        result = live_install(peer, previous, profile, flags, engine="python")
        receipt.observe("Published legacy installer <client-only>", result, result.code == 0,
                        "The previous release installed its Python client.", template=next(templates))
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
        result = live_install(peer, repo, profile, flags)
        receipt.observe(f"{INSTALL} <rerun>", result, result.code == 0 and before == profile.fingerprint(),
                        "The native installer rerun kept the installed runtime and registration.", template=next(templates))
        result = live_install(peer, repo, profile, flags, engine="python")
        restored = profile.fingerprint()
        receipt.observe("PSEUDOLIFE_SHIM_ENGINE=python bash ops/install.sh <rollback>", result,
                        result.code == 0 and restored != before and not profile.native_marker(release),
                        "The documented installer rollback replaced the native runtime.", template=next(templates))
        first_session(receipt, profile, repo)
    if receipt.platform == "linux-arm64":
        raise Unavailable("Both legacy client paths were exercised. The published arm64 daemon update and rollback cannot be observed.")


def ordinary(receipt, release, repo, profile, peer, scratch):
    paired_install(receipt, repo, profile, peer, try_schedule=True)
    first_session(receipt, profile, repo)
    if receipt.platform == "linux-arm64":
        timer = command(["systemctl", "--user", "is-active", "pseudolife-client-update.timer"], env=profile.env)
        receipt.steps.append({"command": "systemctl --user is-active pseudolife-client-update.timer",
                              "exit_code": timer.code, "assertion": "The client update timer was active.",
                              "observed": timer.code == 0, "seconds": timer.seconds})
    registration = profile.registration()
    env = profile.env | registration.get("env", {})
    for name, args, allowed, description in (
        ("connect", [peer.url, "--client", "codex", "--token-file", env[TOKEN_FILE], "--yes", "--json"],
         {0}, "Native connect accepted the disposable daemon and token."),
        ("doctor", ["--json"], {0}, "Native doctor accepted the installed client configuration."),
        ("console", ["--no-open", "--json"], {0}, "Native Console sign-in created a local session link."),
        ("update", ["--clients-only", "--tag", release.version, "--json"], {0, 3},
         "The native release updater accepted the installed release."),
    ):
        invoke = lambda: command([profile.launcher(), name, *args], cwd=repo, env=env, timeout=180)
        result = peer.during(receipt, invoke) if name == "update" else invoke()
        receipt.observe(f"{CLIENT} {name} <fixture-options>", result, result.code in allowed, description)
    raise Unavailable("The hosted run exercises only the ordinary-use operations listed in this receipt.")


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
    with paused_download(release) as (url, requested):
        env = profile.env | {"PSEUDOLIFE_SHIM_ENGINE": "native", "PSEUDOLIFE_NATIVE_ARCHIVE_URL": url}
        with subprocess.Popen(["bash", "ops/install.sh", *CLIENT_FLAGS, *flags],
                              cwd=repo, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
            observed = requested.wait(20)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT if observed else signal.SIGKILL)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    observed = False
    return Result(process.returncode, b"", round(monotonic() - start, 3)), observed


def failure(receipt, release, repo, profile, peer, scratch):
    paired_install(receipt, repo, profile, peer)
    before = profile.fingerprint()
    flags = ["--daemon-url", peer.url, "--token-file", str(peer.token_file)]
    for defect, diagnostic in (("corrupt", b"does not match the release"),
                               ("missing", b"although its SHA256SUMS lists it")):
        assets = scratch / defect
        assets.mkdir()
        shutil.copyfile(release.folder / "SHA256SUMS", assets / "SHA256SUMS")
        if defect == "corrupt":
            (assets / release.archive.name).write_bytes(b"not a release archive\n")
        result = live_install(peer, repo, profile, flags, overrides={"PSEUDOLIFE_NATIVE_ARCHIVE_URL": str(assets)})
        receipt.observe(f"{INSTALL} <{defect}-archive>", result,
                        result.code not in (0, 124) and diagnostic in result.output and before == profile.fingerprint(),
                        "The installer rejected the fault before changing the working runtime.")
    result, observed = cancel_download(release, repo, profile, flags)
    receipt.observe(f"{INSTALL} <cancelled-install>", result,
                    observed and result.code != 0 and before == profile.fingerprint(),
                    "Interrupting the archive download preserved the working runtime.")

    locks = list(profile.folder.rglob(".native-update.lock"))
    require(len(locks) == 1, "The native runtime lock was present.")
    with locks[0].open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = live_install(peer, repo, profile, flags)
        receipt.observe(f"{INSTALL} <locked-runtime>", result,
                        result.code not in (0, 124) and b"another native runtime update" in result.output
                        and before == profile.fingerprint(),
                        "The installed lock prevented concurrent activation.")
    result = live_install(peer, repo, profile, flags)
    receipt.observe(f"{INSTALL} <retry-options>", result,
                    result.code == 0 and profile.native_marker(release), "The clean retry selected the verified runtime.")
    first_session(receipt, profile, repo)
    raise Unavailable("Unobserved interruption points remain pending after the successful retry.")


def bank_recovery(receipt, release, repo, profile, peer, scratch):
    paired_install(receipt, repo, profile, peer)
    with registered_session(profile, repo) as mcp:
        mcp.tool("memory_store", text=SENTINEL, source="hosted-acceptance")
    backup = scratch / "backup"
    backup.mkdir(mode=0o700)
    result = command([peer.owner_executable, "backup", "--out", backup],
                     cwd=peer.checkout, env=peer.owner_env, timeout=180)
    dumps = list(backup.glob("*.sql.gz"))
    archives = list(backup.glob("*.tar.gz"))
    receipt.observe(f"{CLIENT} backup <fixture-options>", result,
                    result.code == 0 and len(dumps) == len(archives) == 1,
                    "Native backup produced one database dump and one state archive.")
    post_backup = "Hosted acceptance change made after backup"
    with registered_session(profile, repo) as mcp:
        mcp.tool("memory_store", text=post_backup, source="hosted-acceptance")
        found = mcp.tool("memory_search", query=post_backup, top_k=5)
        require(post_backup in json.dumps(found.get("entries", [])), "The disposable bank was changed after backup.")
    result = command([peer.owner_executable, "restore", "--backup-file", dumps[0],
                      "--state-archive", archives[0], "--yes"],
                     cwd=peer.checkout, env=peer.owner_env, timeout=240)
    receipt.observe(f"{CLIENT} restore <fixture-options>", result, result.code == 0 and readiness(peer.url),
                    "Native restore replayed the backup and restarted the disposable daemon.")
    with registered_session(profile, repo) as mcp:
        found = mcp.tool("memory_search", query=SENTINEL, top_k=5)
        require(SENTINEL in json.dumps(found.get("entries", [])), "The published client read the restored bank marker.")
        found = mcp.tool("memory_search", query=post_backup, top_k=5)
        # The query can also appear in response metadata. Inspect actual hits.
        require(post_backup not in json.dumps(found.get("entries", [])),
                "The restored bank no longer contained the post-backup change.")
    raise Unavailable("The emulated recovery is observable. The native arm64 bank and applicable move/import-state cases remain unproven.")


def run(tag, platform, output):
    receipts = [Receipt(scenario, platform) for scenario in matrix.SCENARIOS]
    release = None
    cleaned = False
    before_docker = None
    try:
        require(platform == "linux-arm64" and os.environ.get("GITHUB_ACTIONS") == "true"
                and os.uname().machine == "aarch64", "The job ran on the selected disposable Linux arm64 runner.")
        with tempfile.TemporaryDirectory(prefix="acceptance-") as folder:
            scratch = Path(folder)
            free_space(scratch)
            release = published_release(tag, platform, scratch / "release")
            path = native_path(scratch, prepare_codex(platform, scratch))
            repo = release.checkout(scratch / "release-checkout")
            before_docker = DockerFixture()
            baseline(release, path, scratch, output)
            if os.environ.get("HOSTED_ACCEPTANCE_IDENTIFY_ONLY") == "1":
                for receipt in receipts:
                    receipt.note = "This rehearsal only identified the stock install's containers."
                cleaned = before_docker.clean()
                return 0
            enable_amd64()
            functions = {
                "clean-install": clean_install, "upgrade-and-rollback": upgrade,
                "ordinary-native-use": ordinary, "failure-and-retry": failure, "bank-recovery": bank_recovery,
            }
            for receipt in receipts:
                scope = scratch / receipt.scenario
                scope.mkdir()
                try:
                    with linux_peer(release, path, scope) as peer:
                        receipt.daemon = peer.kind
                        if receipt.scenario == "upgrade-and-rollback":
                            upgrade(receipt, release, repo, path, peer, scope)
                        else:
                            profile = Profile(scope / "client-home", path)
                            try:
                                functions[receipt.scenario](receipt, release, repo, profile, peer, scope)
                            finally:
                                stop_units(profile)
                        receipt.status = "pass"
                        receipt.note = matrix.SCENARIOS[receipt.scenario].outcome
                except Unavailable as error:
                    receipt.note = str(error)
                except Failed as error:
                    receipt.status = "fail"
                    receipt.note = str(error)
                except (OSError, ValueError, KeyError, IndexError):
                    receipt.status = "fail"
                    receipt.note = "The observation harness could not complete the scenario."
            cleaned = before_docker.clean()
    except Unavailable as error:
        for receipt in receipts:
            receipt.note = str(error)
    except Failed as error:
        for receipt in receipts:
            receipt.status = "fail"
            receipt.note = str(error)
    except (OSError, ValueError, KeyError, IndexError, matrix.RecordError) as error:
        frame = next((frame for frame in reversed(extract_tb(error.__traceback__))
                      if Path(frame.filename).name in ("hosted.py", "hosted_linux.py", "matrix.py")), None)
        diagnostic = (f" ({type(error).__name__}, {Path(frame.filename).name} line {frame.lineno})"
                      if frame is not None else "")
        for receipt in receipts:
            receipt.status = "fail"
            receipt.note = f"A precondition for collecting hosted observations failed{diagnostic}."
    finally:
        for receipt in receipts:
            if receipt.status == "pass" and not cleaned:
                receipt.status = "fail"
                receipt.note = "The disposable environment could not be cleaned up and verified."
            receipt.write(output, release, cleaned)
            print(f"{receipt.scenario}: {receipt.status}. {receipt.note}")
    return int(any(receipt.status == "fail" for receipt in receipts))
