"""Prepare real, bounded Mac peers for the public client acceptance probes."""

from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import subprocess
import tarfile
import tempfile
from time import monotonic, sleep
import zipfile

from . import matrix
from .hosted import (
    DAEMON_URL, DAEMONS, INSTALL, TOKEN_FILE, Failed, Peer, Profile, Receipt, Unavailable,
    command, download, native_path, prepare_codex, published_release, require,
)
from .hosted_linux import clean_install, free_space, install, live_install, ordinary, readiness


LIMA_VERSION = "2.3.0"
LIMA_BASE = f"https://github.com/lima-vm/lima/releases/download/v{LIMA_VERSION}"
LIMA_SUMS = "5f9eaa93d2b58986273347d0f139b95f8fb87f117d98897760dee8d5b9d45ca6"
GUEST_IMAGE = ("https://cloud-images.ubuntu.com/releases/resolute/release-20261016/"
               "ubuntu-26.04-server-cloudimg-amd64.img")
GUEST_DIGEST = "5645d0318ad21f643ebb1ac4567514620e9893380b07b0201fda163baa5b7eb5"
ONNX_WHEEL = ("https://files.pythonhosted.org/packages/c3/b7/dd3a524ed93a820dff1af902d0412957ab12499953333e9daa01af5bc480/"
              "onnxruntime-1.27.0-cp312-cp312-macosx_14_0_arm64.whl")
ONNX_DIGEST = "a14c2ce45312def86b77aea651f46565e45960cf5f0721bfdff449165086ab76"
GUEST_SCRIPT = Path(__file__).with_name("hosted_lima_guest.sh")


class Budget:
    def __init__(self, seconds):
        self.deadline = monotonic() + seconds

    def remaining(self, maximum):
        left = int(self.deadline - monotonic())
        if left <= 0:
            raise Unavailable("Peer preparation reached its time limit.")
        return min(left, maximum)

    def run(self, argv, *, timeout=120, **kwargs):
        return command(argv, timeout=self.remaining(timeout), **kwargs)


def private_file(path, value):
    with path.open("x") as file:
        path.chmod(0o600)
        file.write(value)
    return path


def wait_ready(url, budget, alive=lambda: True):
    while budget.remaining(1):
        if not alive():
            raise Unavailable("The source-built daemon exited before it became healthy.")
        if readiness(url):
            return
        sleep(1)


def loopback_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def isolated_env(scratch):
    return {"HOME": str(scratch), "PATH": os.environ["PATH"], "LC_ALL": "C",
            "TMPDIR": os.environ.get("RUNNER_TEMP", "/tmp")}


def lima_tool(scratch, budget):
    sums = scratch / "SHA256SUMS"
    download(f"{LIMA_BASE}/SHA256SUMS", sums, LIMA_SUMS, timeout=budget.remaining(45))
    name = f"lima-{LIMA_VERSION}-Darwin-x86_64.tar.gz"
    digest = next(
        checksum for checksum, asset in (line.split() for line in sums.read_text().splitlines())
        if asset.lstrip("*") == name
    )
    archive = scratch / name
    download(f"{LIMA_BASE}/{name}", archive, digest, timeout=budget.remaining(90))
    destination = scratch / "lima"
    destination.mkdir()
    with tarfile.open(archive) as compressed:
        compressed.extractall(destination, filter="data")
    return destination / "bin" / "limactl"


@contextmanager
def lima_peer(release, repo, path, scratch, budget):
    tool = lima_tool(scratch, budget)
    config = scratch / "guest.yaml"
    (scratch / "instances").mkdir()
    env = isolated_env(scratch) | {"LIMA_HOME": str(scratch / "instances")}
    name = "acceptance-peer"
    config.write_text(json.dumps({
        "vmType": "vz", "cpus": 1, "memory": "4GiB", "disk": "14GiB",
        "images": [{"location": GUEST_IMAGE, "arch": "x86_64", "digest": f"sha256:{GUEST_DIGEST}"}],
        "ssh": {"overVsock": True, "forwardAgent": False, "loadDotSSHPubKeys": False},
        "mounts": [], "containerd": {"system": False, "user": False},
        "networks": [{"vzNAT": True}],
        "portForwards": [{"guestIP": "0.0.0.0", "guestIPMustBeZero": False,
                          "proto": "any", "ignore": True}],
    }))
    try:
        started = budget.run([tool, "start", "--tty=false", "--name", name, config],
                             env=env, timeout=420)
        if started.code != 0:
            raise Unavailable("The Intel runner did not start the verified VZ guest.")
        shell = [str(tool), "shell", name]
        setup = budget.run([*shell, "bash", "-s", "--", release.tag, release.commit],
                           env=env, data=GUEST_SCRIPT.read_bytes(), timeout=budget.remaining(600))
        if setup.code != 0:
            raise Unavailable("The verified Linux guest did not finish its daemon setup.")
        details = setup.output.splitlines()[-3:]
        if len(details) != 3 or details[0] != b"peer-ready":
            raise Unavailable("The guest did not return the expected private connection metadata.")
        address = ipaddress.IPv4Address(details[1].decode("ascii"))
        require(address.is_private and not address.is_loopback and not address.is_link_local,
                "The Lima daemon address was on its private VM network.")
        token = details[2].decode("ascii")
        url = f"http://{address}:8765"
        wait_ready(url, budget)
        token_file = private_file(scratch / "peer.token", token + "\n")
        wrapper = scratch / "guest-owner"
        remote = 'exec "$HOME/.acceptance/owner" "$@"'
        wrapper.write_text("#!/bin/sh\nexec " + shlex.join([*shell, "bash", "-c", remote, "owner"]) + ' "$@"\n')
        wrapper.chmod(0o700)
        peer = Peer(DAEMONS[2], url, token_file, str(wrapper), env, repo)
        if peer.sample()["status"] != 200:
            raise Unavailable("The Intel runner could not reach the private guest as its ordinary user.")
        yield peer
    finally:
        if (scratch / "instances" / name).is_dir():
            command([tool, "delete", "--force", name], env=env, timeout=60)
            require(not (scratch / "instances" / name).exists(), "The owned Linux guest was removed.")


def build_source_peer(release, repo, scratch, budget):
    env = isolated_env(scratch)
    env.update({"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1"})
    for formula in ("postgresql@18", "pgvector"):
        result = budget.run(["brew", "install", "--force-bottle", formula], env=env, timeout=150)
        if result.code != 0:
            raise Unavailable("Homebrew PostgreSQL or pgvector was unavailable.")
    binary = budget.run(["brew", "--prefix", "postgresql@18"], env=env)
    require(binary.code == 0, "Homebrew located its PostgreSQL 18 installation.")
    postgres = Path(os.fsdecode(binary.output.strip())) / "bin"
    wheel = scratch / "onnxruntime.whl"
    download(ONNX_WHEEL, wheel, ONNX_DIGEST, timeout=budget.remaining(90))
    onnx = scratch / "libonnxruntime.1.27.0.dylib"
    with zipfile.ZipFile(wheel) as archive:
        library = next(member for member in archive.namelist() if Path(member).name == onnx.name)
        onnx.write_bytes(archive.read(library))
    env.update({
        "CARGO_HOME": os.environ.get("CARGO_HOME", str(Path.home() / ".cargo")),
        "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
        "CARGO_TARGET_DIR": str(scratch / "cargo-target"), "CARGO_INCREMENTAL": "0",
        "CARGO_PROFILE_DEV_DEBUG": "0",
    })
    free_space(scratch)
    compiled = budget.run(
        ["cargo", "+1.94.0", "build", "--manifest-path", str(repo / "rust/Cargo.toml"),
         "--locked", "--bin", "pseudolife-daemon", "-j", "2"],
        env=env, cwd=repo, timeout=budget.remaining(1500),
    )
    if compiled.code != 0:
        raise Unavailable("The released daemon source did not compile on the Apple silicon runner.")
    executable = scratch / "cargo-target" / "debug" / "pseudolife-daemon"
    env.update({
        "PSEUDOLIFE_DAEMON_MODELS_DIR": str(scratch / "models"), "ORT_DYLIB_PATH": str(onnx),
        "PSEUDOLIFE_DAEMON_STATIC_DIR": str(repo / "frontend/dist"),
        "PSEUDOLIFE_PLUGIN_DIR": str(repo / "plugin"),
        "PSEUDOLIFE_BUILD_GIT_SHA": release.commit, "PSEUDOLIFE_BUILD_SOURCE": "checkout",
    })
    models = budget.run([executable, "provision", "models", "--if-missing"], env=env,
                        timeout=budget.remaining(300))
    if models.code != 0:
        raise Unavailable("The source-built daemon could not provision its pinned models.")
    return postgres, executable, env


@contextmanager
def apple_peer(release, repo, path, scratch, budget):
    postgres, executable, env = build_source_peer(release, repo, scratch, budget)
    password = secrets.token_hex(24)
    pwfile = private_file(scratch / "postgres-password", password + "\n")
    database = scratch / "postgres"
    initialized = budget.run([postgres / "initdb", "-D", database, "-U", "acceptance",
                              "--auth=scram-sha-256", "--pwfile", pwfile, "--no-locale"], env=env)
    pwfile.unlink()
    if initialized.code != 0:
        raise Unavailable("The native PostgreSQL fixture could not initialize.")
    pgport = loopback_port()
    port = loopback_port()
    url = f"http://127.0.0.1:{port}"
    token = secrets.token_hex(32)
    token_file = private_file(scratch / "peer.token", token + "\n")
    state = scratch / "state"
    state.mkdir(mode=0o700)
    env.update({
        "PSEUDOLIFE_MCP_DATABASE_URL": f"postgresql://acceptance:{password}@127.0.0.1:{pgport}/postgres?sslmode=disable",
        "PSEUDOLIFE_MCP_TOKEN": token, "PSEUDOLIFE_MCP_DATA_DIR": str(state),
        "PSEUDOLIFE_MCP_HOST": "127.0.0.1", "PSEUDOLIFE_MCP_PORT": str(port),
    })
    try:
        pg = budget.run([postgres / "pg_ctl", "-D", database, "-l", scratch / "pg.log",
                         "-o", f"-h 127.0.0.1 -p {pgport}", "-w", "start"], env=env)
        if pg.code != 0:
            raise Unavailable("The native PostgreSQL fixture did not start.")
        with subprocess.Popen([executable], cwd=repo, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as daemon:
            try:
                wait_ready(url, budget, lambda: daemon.poll() is None)
                profile = Profile(scratch / "owner", path)
                owner = budget.remaining(130)
                installed = install(repo, profile, ["--yes", "--client-only", "--client", "codex",
                                    "--daemon-url", url, "--token-file", str(token_file)], timeout=owner)
                if installed.code != 0:
                    raise Unavailable("The source peer did not accept the disposable owner install.")
                owner_env = profile.env | profile.registration().get("env", {})
                owner_env.update({DAEMON_URL: url, TOKEN_FILE: str(token_file),
                                  "PSEUDOLIFE_MCP_DATABASE_URL": env["PSEUDOLIFE_MCP_DATABASE_URL"]})
                yield Peer(DAEMONS[3], url, token_file, profile.launcher(), owner_env, repo)
            finally:
                if daemon.poll() is None:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
    finally:
        if (database / "postmaster.pid").is_file():
            stopped = command([postgres / "pg_ctl", "-D", database, "-w", "stop", "-m", "fast"],
                              env=env, timeout=45)
            require(stopped.code == 0 and not (database / "postmaster.pid").exists(),
                    "The owned native PostgreSQL process stopped.")


def offline(receipt, release, repo, profile, scratch):
    token_file = private_file(scratch / "offline.token", secrets.token_hex(32) + "\n")
    url = f"http://127.0.0.1:{loopback_port()}"
    result = live_install(None, repo, profile, ["--daemon-url", url, "--token-file", str(token_file)])
    receipt.observe(f"{INSTALL} <closed-loopback>", result,
                    result.code not in (0, 124) and not (profile.codex / "config.toml").is_file(),
                    "The published installer refused the closed endpoint without registering a client.")


def run(tag, platform, output):
    rows = [Receipt(scenario, platform) for scenario in matrix.SCENARIOS]
    probes = {"clean-install": clean_install, "ordinary-native-use": ordinary}
    release = None
    cleaned = False
    try:
        machine = "x86_64" if platform == "macos-x64" else "arm64"
        require(os.environ.get("GITHUB_ACTIONS") == "true" and os.uname().sysname == "Darwin"
                and os.uname().machine == machine, "The job ran on the selected disposable macOS runner.")
        # Lima's socket paths include the instance directory, so keep the
        # private parent short enough for macOS's Unix socket length limit.
        with tempfile.TemporaryDirectory(prefix="pla-", dir="/tmp") as temporary:
            scratch = Path(temporary).resolve()
            free_space(scratch)
            release = published_release(tag, platform, scratch / "release")
            repo = release.checkout(scratch / "release-checkout")
            path = native_path(scratch, prepare_codex(platform, scratch))
            console = command(["stat", "-f", "%u", "/dev/console"])
            launchd = command(["launchctl", "print", f"gui/{os.getuid()}"])
            gui = console.code == launchd.code == 0 and console.output.strip() == str(os.getuid()).encode()
            manager, cap = (lima_peer, 720) if platform == "macos-x64" else (apple_peer, 1500)
            for index in (1, 2):
                attempt = scratch / f"peer-{index}"
                attempt.mkdir(mode=0o700)
                try:
                    free_space(attempt)
                    with manager(release, repo, path, attempt, Budget(cap)) as peer:
                        for row in rows:
                            if row.scenario in probes:
                                observe(row, release, repo, path, peer, scratch, (launchd, gui), probes[row.scenario])
                    break
                except Unavailable as error:
                    if index == 2:
                        for row in rows:
                            if row.scenario in probes:
                                observe(row, release, repo, path, None, scratch, (launchd, gui), offline, str(error))
            cleaned = True
    except (Failed, OSError, ValueError, KeyError, IndexError, StopIteration) as error:
        for row in rows:
            if row.scenario in probes:
                row.status, row.note = "fail", (str(error) if isinstance(error, Failed) else
                                                "The macOS observation harness could not complete setup.")
    except Unavailable as error:
        for row in rows:
            row.note = str(error)
    for row in rows:
        row.write(output, release, cleaned)
        print(f"{row.scenario}: {row.status}. {row.note}")
    return int(any(row.status == "fail" for row in rows))


def observe(row, release, repo, path, peer, scratch, launchd, probe, pending_reason=None):
    scope = scratch / row.scenario
    scope.mkdir()
    profile = Profile(scope / "home", path)
    launchd_result, gui = launchd
    row.steps.append({"command": "launchctl print gui/<uid>", "exit_code": launchd_result.code,
                      "assertion": "A launchd GUI domain was available.", "observed": gui})
    try:
        if peer is None:
            probe(row, release, repo, profile, scope)
            row.note = pending_reason
        else:
            row.daemon = peer.kind
            probe(row, release, repo, profile, peer, scope)
            row.status, row.note = "pass", matrix.SCENARIOS[row.scenario].outcome
    except Failed as error:
        row.status, row.note = "fail", str(error)
    except Unavailable as error:
        row.note = str(error)
    except (OSError, ValueError, KeyError, IndexError):
        row.status, row.note = "fail", "The observation harness could not complete the scenario."
