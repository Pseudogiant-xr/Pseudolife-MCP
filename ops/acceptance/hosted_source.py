"""A test daemon built from the tagged source, for the published client rows.

Both macOS columns run the published native client against this peer. The
daemon, its PostgreSQL and its ONNX Runtime are fixtures, so a row's result
is evidence about the client only (`hosted.PEER_NOTE` says so in each
receipt). Each setup step that exits non-zero keeps its sanitised output in
`peer-setup.json`.
"""

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
from time import monotonic, sleep
import zipfile

from .hosted import (
    CODEX_HOME_NOTE, DAEMON_URL, DAEMONS, REDACTOR, TOKEN_FILE, Failed, Peer, Profile, Result, Unavailable,
    CleanupFailed, codex_probe, command, download, explain, read_tail,
)


def need(ok, reason):
    """A fixture the runner could not supply leaves its rows unrun, not failed."""
    if not ok:
        raise Unavailable(reason)


RUST = "1.94.0"
PYPI = "https://files.pythonhosted.org/packages"
# The tag's Rust image loads ONNX Runtime 1.27.0 (ops/Dockerfile.daemon-rust).
# PyPI publishes no Intel macOS wheel after 1.23.2, which the daemon's
# runtime-loaded ort 2.0.0-rc.10 also accepts, so the Intel peer uses it.
ONNX = {
    "macos-arm64": ("1.27.0", f"{PYPI}/c3/b7/dd3a524ed93a820dff1af902d0412957ab12499953333e9daa01af5bc480/"
                    "onnxruntime-1.27.0-cp312-cp312-macosx_14_0_arm64.whl",
                    "a14c2ce45312def86b77aea651f46565e45960cf5f0721bfdff449165086ab76",
                    "libonnxruntime.1.27.0.dylib"),
    "macos-x64": ("1.23.2", f"{PYPI}/91/9d/a81aafd899b900101988ead7fb14974c8a58695338ab6a0f3d6b0100f30b/"
                  "onnxruntime-1.23.2-cp312-cp312-macosx_13_0_x86_64.whl",
                  "218295a8acae83905f6f1aed8cacb8e3eb3bd7513a13fe4ba3b2664a19fc4a6b",
                  "libonnxruntime.1.23.2.dylib"),
}
# Source releases for the Intel peer. PostgreSQL publishes the archive's
# SHA-256 beside it; pgvector's tag archive digest was taken on 23 November 2026.
POSTGRES_SOURCE = ("https://ftp.postgresql.org/pub/source/v18.7/postgresql-18.7.tar.bz2",
                   "441dfae4280cae3e0425e5f876f80559fb0731c140f1aae22b3e5a54d0411bfd",
                   "postgresql-18.7.tar.bz2")
PGVECTOR_SOURCE = ("https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.7.tar.gz",
                   "cac0b10c360f05b2d521200105ba3697e773d4cd3731f5a915a7e37ebe0bea85",
                   "pgvector-0.8.7.tar.gz")
# Provisioning streams 2.4 GB from the model host. A dropped stream fails
# the whole step, which keeps nothing, so a few retries cost one download each.
PROVISION_ATTEMPTS = 3
BUDGETS = {"macos-arm64": 1500, "macos-x64": 2100}


class Budget:
    """One deadline for every setup step, and their sanitised receipts."""

    def __init__(self, seconds):
        self.deadline = monotonic() + seconds
        self.steps = []

    def remaining(self, maximum):
        left = int(self.deadline - monotonic())
        if left <= 0:
            raise Unavailable("The test peer's setup reached its time limit.")
        return min(left, maximum)

    def run(self, argv, *, timeout=120, label=None, **kwargs):
        try:
            result = command(argv, timeout=self.remaining(timeout), **kwargs)
        except FileNotFoundError:
            result = Result(127, f"{Path(str(argv[0])).name}: command not found".encode(), 0)
        if label is not None:
            step = {"command": label, "exit_code": result.code, "seconds": result.seconds}
            if result.code != 0:
                step["failure"] = explain(label, result)
            self.steps.append(step)
        return result

    def note(self, label, **observed):
        self.steps.append({"command": label, **observed})

    def last_failure(self):
        return next((step["failure"] for step in reversed(self.steps) if "failure" in step), None)

    def refuse(self, reason):
        """Unavailable, carrying the most recent setup failure's output."""
        return Unavailable(reason, self.last_failure())


def private_file(path, value):
    with path.open("x") as file:
        path.chmod(0o600)
        file.write(value)
    return path


def loopback_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def isolated_env(scratch):
    return {"HOME": str(scratch), "PATH": os.environ["PATH"], "LC_ALL": "C",
            "TMPDIR": os.environ.get("RUNNER_TEMP", "/tmp")}


def free_bytes(path):
    return shutil.disk_usage(path).free


@dataclass(frozen=True)
class Build:
    platform: str
    executable: Path
    postgres: Path
    env: dict
    onnx_version: str


def source_postgres(env, scratch, budget):
    """PostgreSQL 18 and pgvector built from source, where no package has them."""
    prefix = scratch / "pg"
    jobs = str(max(2, os.cpu_count() or 2))
    archives = []
    for url, digest, name in (POSTGRES_SOURCE, PGVECTOR_SOURCE):
        archive = scratch / name
        download(url, archive, digest, timeout=budget.remaining(180), peer_asset=True)
        unpacked = scratch / name.split(".tar.")[0]
        unpacked.mkdir()
        result = budget.run(["tar", "-xf", archive, "-C", unpacked, "--strip-components", "1"], env=env)
        need(result.code == 0, "The verified PostgreSQL source archive did not unpack on this runner.")
        archives.append(unpacked)
    postgres, pgvector = archives
    for label, argv, cwd in (
        ("configure <PostgreSQL 18 source>", ["./configure", f"--prefix={prefix}", "--without-icu",
                                               "--without-readline", "--without-zlib"], postgres),
        ("make <PostgreSQL 18 source>", ["make", "-s", "-j", jobs], postgres),
        ("make install <PostgreSQL 18 source>", ["make", "-s", "install"], postgres),
        ("make <pgvector source>", ["make", "-s", f"PG_CONFIG={prefix}/bin/pg_config"], pgvector),
        ("make install <pgvector source>", ["make", "-s", "install", f"PG_CONFIG={prefix}/bin/pg_config"],
         pgvector),
    ):
        result = budget.run(argv, env=env, cwd=cwd, timeout=900, label=label)
        if result.code != 0:
            raise budget.refuse("PostgreSQL 18 or pgvector did not build from verified source on this runner.")
    return prefix / "bin"


def postgres_tools(platform, env, scratch, budget):
    """PostgreSQL 18 with pgvector, from the platform's package source."""
    if platform == "macos-x64":
        # Homebrew publishes no PostgreSQL 18 bottle for Intel macOS (trial
        # run 45121674952), and building its formula pulls a long dependency
        # chain, so the Intel peer builds PostgreSQL itself.
        return source_postgres(env, scratch, budget)
    for formula in ("postgresql@18", "pgvector"):
        result = budget.run(["brew", "install", "--force-bottle", formula], env=env, timeout=600,
                            label=f"brew install --force-bottle {formula}")
        if result.code != 0:
            raise budget.refuse("Homebrew did not install a PostgreSQL 18 or pgvector bottle on this runner.")
    prefix = budget.run(["brew", "--prefix", "postgresql@18"], env=env)
    need(prefix.code == 0, "Homebrew did not report its PostgreSQL 18 installation.")
    return Path(os.fsdecode(prefix.output.strip())) / "bin"


def onnx_library(platform, scratch, budget):
    version, url, digest, name = ONNX[platform]
    wheel = scratch / "onnxruntime.whl"
    download(url, wheel, digest, timeout=budget.remaining(180), peer_asset=True)
    library = scratch / name
    try:
        with zipfile.ZipFile(wheel) as archive:
            members = [member for member in archive.namelist() if Path(member).name == name]
            need(len(members) == 1, "The ONNX Runtime wheel did not contain its shared library.")
            library.write_bytes(archive.read(members[0]))
    except zipfile.BadZipFile:
        raise Unavailable("The ONNX Runtime wheel was not a readable archive.") from None
    wheel.unlink()
    budget.note("ONNX Runtime from its PyPI wheel", version=version, verified_sha256=True)
    return version, library


def prepare(release, repo, scratch, budget, platform):
    """Build the tagged daemon once and provision its models."""
    env = isolated_env(scratch)
    env.update({"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1",
                "HOMEBREW_NO_ENV_HINTS": "1"})
    budget.note("free disk before setup", free_bytes=free_bytes(scratch))
    postgres = postgres_tools(platform, env, scratch, budget)
    onnx_version, onnx = onnx_library(platform, scratch, budget)
    env.update({
        "CARGO_HOME": os.environ.get("CARGO_HOME", str(Path.home() / ".cargo")),
        "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
        "CARGO_TARGET_DIR": str(scratch / "cargo-target"), "CARGO_INCREMENTAL": "0",
        "CARGO_PROFILE_DEV_DEBUG": "0",
    })
    env["PATH"] = os.pathsep.join((str(Path(env["CARGO_HOME"]) / "bin"), env["PATH"]))
    if shutil.which("rustup", path=env["PATH"]) is None:
        raise Unavailable("The runner image had no rustup to install the pinned Rust toolchain.")
    compiler = budget.run(["rustup", "toolchain", "install", RUST, "--profile", "minimal"],
                          env=env, timeout=300, label=f"rustup toolchain install {RUST} --profile minimal")
    if compiler.code != 0:
        raise budget.refuse("The pinned Rust toolchain could not be installed on the runner.")
    compiled = budget.run(
        ["cargo", f"+{RUST}", "build", "--manifest-path", str(repo / "rust/Cargo.toml"),
         "--locked", "--bin", "pseudolife-daemon", "-j", str(max(2, os.cpu_count() or 2))],
        env=env, cwd=repo, timeout=budget.remaining(1800),
        label=f"cargo +{RUST} build --locked --bin pseudolife-daemon",
    )
    if compiled.code != 0:
        raise budget.refuse("The tagged daemon source did not compile on this runner.")
    executable = scratch / "cargo-target" / "debug" / "pseudolife-daemon"
    budget.note("free disk after the daemon build", free_bytes=free_bytes(scratch))
    env.update({
        "PSEUDOLIFE_DAEMON_MODELS_DIR": str(scratch / "models"), "ORT_DYLIB_PATH": str(onnx),
        "PSEUDOLIFE_DAEMON_STATIC_DIR": str(repo / "frontend/dist"),
        "PSEUDOLIFE_PLUGIN_DIR": str(repo / "plugin"),
        "PSEUDOLIFE_BUILD_GIT_SHA": release.commit, "PSEUDOLIFE_BUILD_SOURCE": "checkout",
    })
    for attempt in range(1, PROVISION_ATTEMPTS + 1):
        models = budget.run([executable, "provision", "models", "--if-missing"], env=env,
                            timeout=budget.remaining(1200),
                            label=f"pseudolife-daemon provision models --if-missing <attempt {attempt}>")
        if models.code == 0:
            break
        budget.note("free disk after a failed provision", free_bytes=free_bytes(scratch))
    else:
        raise budget.refuse("The source-built daemon could not provision its pinned models.")
    return Build(platform, executable, postgres, env, onnx_version)


@contextmanager
def daemon_process(build, repo, scratch, env):
    log = scratch / "daemon.log"
    with log.open("wb") as output, subprocess.Popen(
            [build.executable], cwd=repo, env=env, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True) as daemon:
        try:
            yield daemon, log
        finally:
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    with suppress(subprocess.TimeoutExpired):
                        daemon.wait(timeout=10)


def log_tail(path):
    with suppress(OSError), path.open("rb") as stream:
        return read_tail(stream)
    return b""


def wait_healthy(url, budget, daemon, log):
    from .hosted_linux import readiness

    while True:
        if daemon.poll() is not None:
            failure = explain("pseudolife-daemon <test peer>", Result(daemon.returncode, log_tail(log), 0))
            raise Unavailable("The source-built daemon exited before it became healthy.", failure)
        if readiness(url):
            return
        try:
            budget.remaining(1)
        except Unavailable:
            failure = explain("pseudolife-daemon <test peer>", Result(None, log_tail(log), 0))
            raise Unavailable("The source-built daemon did not report healthy before the setup limit.",
                              failure) from None
        sleep(1)


@contextmanager
def source_peer(build, release, repo, path, scratch, budget):
    """Start PostgreSQL and the daemon on loopback, then an owner client."""
    from .hosted_linux import install

    env = dict(build.env)
    password = REDACTOR.secret(secrets.token_hex(24))
    database = scratch / "postgres"
    initialized = budget.run([build.postgres / "initdb", "-D", database, "-U", "acceptance",
                              "--auth=scram-sha-256", "--pwfile", "/dev/stdin", "--no-locale"],
                             env=env, data=(password + "\n").encode("ascii"), label="initdb <test peer>")
    if initialized.code != 0:
        raise budget.refuse("The PostgreSQL fixture could not initialize.")
    pgport = loopback_port()
    port = loopback_port()
    url = f"http://127.0.0.1:{port}"
    token = REDACTOR.secret(secrets.token_hex(32))
    token_file = private_file(scratch / "peer.token", token + "\n")
    state = scratch / "state"
    state.mkdir(mode=0o700)
    database_url = f"postgresql://acceptance:{password}@127.0.0.1:{pgport}/postgres?sslmode=disable"
    REDACTOR.secret(database_url)
    env.update({
        "PSEUDOLIFE_MCP_DATABASE_URL": database_url,
        "PSEUDOLIFE_MCP_TOKEN": token, "PSEUDOLIFE_MCP_DATA_DIR": str(state),
        "PSEUDOLIFE_MCP_HOST": "127.0.0.1", "PSEUDOLIFE_MCP_PORT": str(port),
    })
    pg_log = scratch / "pg.log"
    try:
        pg = budget.run([build.postgres / "pg_ctl", "-D", database, "-l", pg_log,
                         # The socket stays in the scratch folder, never a shared
                         # system directory the runner user may not write.
                         "-o", f"-h 127.0.0.1 -p {pgport} -k {scratch}", "-w", "start"],
                        env=env, label="pg_ctl start <test peer>")
        if pg.code != 0:
            budget.steps[-1]["failure"] = explain("pg_ctl start <test peer>", Result(pg.code, log_tail(pg_log), 0))
            raise budget.refuse("The PostgreSQL fixture did not start.")
        with daemon_process(build, repo, scratch, env) as (daemon, log):
            wait_healthy(url, budget, daemon, log)
            probe = Peer(DAEMONS[3], url, token_file, "", {}, repo, "rust")
            sampled = probe.sample()
            budget.note("GET /api/episodes?limit=1 <test peer>", http=sampled)
            if sampled["status"] != 200:
                raise Unavailable("The source-built daemon refused its disposable token.")
            budget.note("test peer ready", daemon=DAEMONS[3], engine="rust", onnxruntime=build.onnx_version)
            # From here the published installer and client are under test.
            profile = Profile(scratch / "owner", path)
            budget.note("owner Codex home", fixture_note=CODEX_HOME_NOTE)
            label = "PSEUDOLIFE_SHIM_ENGINE=native bash ops/install.sh <owner client-only>"
            installed = install(repo, profile, ["--yes", "--client-only", "--client", "codex",
                                "--daemon-url", url, "--token-file", str(token_file)], timeout=300)
            budget.note(label, exit_code=installed.code, seconds=installed.seconds)
            if installed.code != 0 or not (profile.codex / "config.toml").is_file():
                failure = explain(label, installed)
                budget.steps[-1]["failure"] = failure
                if b"Codex" in installed.output:
                    budget.note("codex app-server --stdio <diagnostic>", **codex_probe(profile.env, repo))
                raise Failed("The published client-only installer did not register Codex against the healthy "
                             "test peer.", failure)
            owner_env = profile.env | profile.registration().get("env", {})
            # The owner runs on the daemon's host, so it shares the daemon's
            # port setting, which `invite` reads (trial run 45135001202).
            owner_env.update({DAEMON_URL: url, TOKEN_FILE: str(token_file), "PSEUDOLIFE_MCP_PORT": str(port),
                              "PSEUDOLIFE_MCP_DATABASE_URL": database_url})
            yield Peer(DAEMONS[3], url, token_file, profile.launcher(), owner_env, repo, "rust",
                       database_url=database_url, data_dir=state, tools=str(build.postgres))
    finally:
        if (database / "postmaster.pid").is_file():
            stopped = command([build.postgres / "pg_ctl", "-D", database, "-w", "stop", "-m", "fast"],
                              env=env, timeout=45)
            if stopped.code != 0 or (database / "postmaster.pid").exists():
                raise CleanupFailed("The owned PostgreSQL fixture did not stop.",
                                    explain("pg_ctl stop <test peer>", stopped))
