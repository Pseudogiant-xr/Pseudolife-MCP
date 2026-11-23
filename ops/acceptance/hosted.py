"""Run release acceptance in a disposable GitHub-hosted job."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
from threading import Thread
from time import monotonic
import tomllib
import uuid
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import matrix
from .sanitize import runner_redactor


PROJECT = "Pseudogiant-xr/Pseudolife-MCP"
RELEASE = f"https://github.com/{PROJECT}/releases/download"
API = f"https://api.github.com/repos/{PROJECT}/releases/tags"
PLATFORMS = ("linux-arm64", "macos-x64", "macos-arm64")
SELECTIONS = {**{platform: (platform,) for platform in PLATFORMS},
              "all": PLATFORMS, "macos": PLATFORMS[1:]}
DAEMONS = ("none", *matrix.DAEMON_KINDS)
CODEX_HOME_NOTE = ("The Codex home folder existed before each install, as after a first Codex start, so this row "
                   "does not cover a never-started Codex (issue #1897).")
STOCK_CODEX_NOTE = ("The home had no Codex folder, as before a first Codex start. Issue #1897 is the Codex setup "
                    "failure that follows.")
PEER_NOTE = ("The daemon is a test peer built from the tagged source on this runner. Only the client "
             "steps are evidence about the published release artifact.")
OUTCOMES = ("pass", "fail", "not yet run")
INSTALL = "PSEUDOLIFE_SHIM_ENGINE=native bash ops/install.sh"
CLIENT = "pseudolife-mcp"
TOKEN_FILE = "PSEUDOLIFE_MCP_TOKEN_FILE"
DAEMON_URL = "PSEUDOLIFE_MCP_DAEMON_URL"
CODEX_ASSETS = {
    "linux-arm64": ("rust-v0.171.0", "aarch64-unknown-linux-musl",
                    "bcf50b836385e51bd2976ab2bbe92b1b08a1cbe37712eb8dbe378aa78d24871c"),
    "macos-x64": ("rust-v0.171.0", "x86_64-apple-darwin",
                  "6f9b1144ffaa64aa12bf30f312405b266b84c433bae9f070be10245762c7aa44"),
    "macos-arm64": ("rust-v0.171.0", "aarch64-apple-darwin",
                    "9414c8c967f110b11e8324c0cfa2b20f012094ef890110a561c92ee535a2681a"),
}


# Every value a job generates (tokens, passwords, pairing codes) and every
# private directory is registered here before anything can print it.
REDACTOR = runner_redactor()


class Observation(Exception):
    """A reason sentence, plus the sanitised output that explains it."""

    def __init__(self, reason, failure=None):
        super().__init__(reason)
        self.failure = failure


class Unavailable(Observation):
    """The runner cannot supply a prerequisite for an observation."""


class Failed(Observation):
    """An assertion about the release failed."""


class CleanupFailed(Failed):
    """A fixture the job started could not be stopped and verified."""


@dataclass(frozen=True)
class Result:
    code: int
    output: bytes
    seconds: float

    def json(self):
        try:
            return json.loads(self.output)
        except (UnicodeError, ValueError):
            return {}


def command(argv, *, env=None, cwd=None, timeout=60, data=None, stdout_only=False) -> Result:
    """Keep arbitrary subprocess output and argv outside published evidence."""
    started = monotonic()
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, start_new_session=True,
        stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE if stdout_only else subprocess.STDOUT,
    )
    try:
        try:
            output, _ = process.communicate(data, timeout=timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            code = 124
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired as error:
                # A detached descendant can keep the pipe open after killpg.
                output = error.output or b""
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        if process.returncode is None:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
    return Result(code, output, round(monotonic() - started, 3))


NOT_MET = "Not met: "


def require(ok, expectation, failure=None):
    """Fail with the expectation that was not met, worded as a failure."""
    if not ok:
        raise Failed(NOT_MET + expectation, failure)


def read_tail(stream, limit=65536):
    """The last bytes of a private output file, for the redactor.

    A cut inside a line could split a registered token or a key block into a
    fragment no rule recognises, so a partial first line is dropped.
    """
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - limit))
    data = stream.read()
    if size > limit:
        data = data.partition(b"\n")[2]
    return data


CODEX_PROBE = b"".join(json.dumps(message).encode() + b"\n" for message in (
    {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "hosted_acceptance_probe", "version": "1"},
                                                  "capabilities": {"experimentalApi": True}}},
    {"method": "initialized"},
    {"id": 2, "method": "config/read", "params": {"includeLayers": True, "cwd": "."}},
))


def codex_probe(env, cwd, hold=8):
    """After the installer reports a Codex setup failure, ask Codex itself.

    The installer keeps Codex's own output out of its diagnostics, so this
    replays the setup's first app-server requests in the same environment
    and keeps Codex's sanitised answer and stderr in the receipt.
    """
    with tempfile.NamedTemporaryFile(suffix=".jsonl") as requests:
        requests.write(CODEX_PROBE)
        requests.flush()
        # Stdin stays open while Codex answers, as the installer's client keeps it.
        result = command(["sh", "-c", '{ cat "$1"; sleep "$2"; } | codex app-server --stdio', "probe",
                          requests.name, str(hold)], env=env, cwd=cwd, timeout=45)
    answered = [line for line in result.output.splitlines() if line.startswith(b'{"id":2,') and b'"result"' in line]
    probe = explain("codex app-server --stdio <setup requests replayed>", result)
    probe["config_read_answered"] = bool(answered)
    probe["last_lines"] = [line[:200] for line in probe["last_lines"]]
    return probe


def explain(label, result):
    """The receipt entry for a step whose exit or output decided a failure."""
    return REDACTOR.failure(label, result.code, result.output)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, path, digest=None, *, timeout=180, peer_asset=False, release_metadata=False):
    result = command(
        ["curl", "--disable", "--fail", "--silent", "--show-error", "--location",
         "--proto", "=https", "--proto-redir", "=https", "--max-time", str(timeout),
         "--retry", "2", "--retry-all-errors", "--retry-max-time", str(timeout),
         "--write-out", "%{http_code}",
         "--output", path, url],
        timeout=timeout + 5, stdout_only=True,
    )
    label = path.name if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", path.name) else "<asset>"
    status = result.output[-3:].decode("ascii") if re.fullmatch(rb"[0-9]{3}", result.output[-3:]) else "unknown"
    if result.code != 0:
        message = f"Public download failed for {label} (curl exit {result.code}, HTTP {status})."
        transient = result.code in (5, 6, 7, 18, 28, 35, 52, 55, 56, 124)
        if result.code == 22:
            transient = peer_asset or status in ("403", "429") or status.startswith("5")
        if transient and (peer_asset or release_metadata):
            raise Unavailable(message)
        raise Failed(message)
    if digest is not None:
        if sha256(path) != digest:
            # A release asset that disagrees is a release failure. A fixture
            # download that disagrees (a re-packed tag archive) leaves the
            # rows it serves unrun.
            raise (Unavailable if peer_asset else Failed)("A download disagreed with its published SHA-256.")


@dataclass(frozen=True)
class Release:
    tag: str
    platform: str
    folder: Path
    commit: str
    archive: Path
    digest: str
    daemon_digest: str | None = None
    verified_by: str = "release API and SHA256SUMS"

    @property
    def version(self):
        return self.tag[1:]

    def checkout(self, destination):
        repository = f"https://github.com/{PROJECT}.git"
        result = command(["git", "clone", "--quiet", "--depth=1", "--branch",
                          self.tag, repository, destination], timeout=120)
        require(result.code == 0, "Published tag checkout succeeded.")
        head = command(["git", "rev-parse", "HEAD"], cwd=destination)
        require(head.code == 0 and head.output.strip() == self.commit.encode(),
                "Checkout matched the release commit.")
        return destination


def published_release(tag, platform, folder):
    require(re.fullmatch(rf"v{matrix.VERSION}", tag), "Release tag has a supported version.")
    folder.mkdir()
    metadata = folder / "release.json"
    try:
        download(f"{API}/{tag}", metadata, release_metadata=True)
    except Unavailable:
        # Hosted Mac runners share addresses, so the anonymous API limit can
        # refuse the metadata (HTTP 403 in trial run 45128102045). The public
        # download path serves only published releases, and every archive is
        # still checked against the release's SHA256SUMS below.
        metadata.unlink(missing_ok=True)
    assets = None
    if metadata.is_file():
        release = json.loads(metadata.read_bytes())
        require(release.get("tag_name") == tag and release.get("draft") is False,
                "The selected tag is a published release.")
        assets = {asset["name"]: asset.get("digest", "") for asset in release["assets"]}

    def fetch(name, digest=None):
        if assets is None:
            download(f"{RELEASE}/{tag}/{name}", folder / name, digest, release_metadata=digest is None)
            return
        official = assets.get(name) or ""
        require(re.fullmatch(r"sha256:[a-f0-9]{64}", official),
                "Release asset has a published digest.")
        if digest is not None:
            require(official == f"sha256:{digest}", "Release digest matches SHA256SUMS.")
        download(f"{RELEASE}/{tag}/{name}", folder / name, official[7:])

    fetch("SHA256SUMS")
    digests = matrix.read_manifest(folder / "SHA256SUMS")
    archive = f"pseudolife-mcp-{tag[1:]}-{matrix.TARGETS[platform]}.tar.gz"
    bundle = f"pseudolife-compose-{tag[1:]}.tar.gz"
    for name in (archive, bundle):
        require(name in digests, "SHA256SUMS contains the required release asset.")
        fetch(name, digests[name])
    if assets is not None and "RELEASE-READY.json" in assets:
        fetch("RELEASE-READY.json")
    remote = command(["git", "ls-remote", "--exit-code", f"https://github.com/{PROJECT}.git",
                      f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"], stdout_only=True)
    require(remote.code == 0, "Published tag resolved.")
    refs = {ref.decode("ascii"): sha.decode("ascii") for sha, ref in
            re.findall(rb"^([a-f0-9]{40})\t(refs/tags/[a-zA-Z0-9.^{}-]+)$", remote.output, re.MULTILINE)}
    commit = refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}", ""))
    require(re.fullmatch(r"[a-f0-9]{40}", commit), "Published tag resolved to a full commit.")
    ready = folder / "RELEASE-READY.json"
    daemon_digest = None
    if ready.is_file():
        marker = json.loads(ready.read_bytes())
        require(marker["commit"] == commit, "Release readiness names the tag commit.")
        daemon_digest = marker.get("images", {}).get("pseudolife-daemon-rust")
        require(daemon_digest is None or isinstance(daemon_digest, str)
                and re.fullmatch(r"sha256:[a-f0-9]{64}", daemon_digest),
                "Release readiness contains a valid published Rust daemon digest.")
    return Release(tag, platform, folder, commit, folder / archive, digests[archive], daemon_digest,
                   "release API and SHA256SUMS" if assets is not None else "SHA256SUMS only (release API refused)")


def prepare_codex(platform, scratch):
    tag, target, digest = CODEX_ASSETS[platform]
    name = f"codex-{target}"
    archive = scratch / "codex.tar.gz"
    download(f"https://github.com/openai/codex/releases/download/{tag}/{name}.tar.gz",
             archive, digest)
    target_dir = scratch / "tools"
    target_dir.mkdir()
    with tarfile.open(archive) as compressed:
        matching = [member for member in compressed.getmembers()
                    if member.isfile() and Path(member.name).name == name]
        require(len(matching) == 1, "Codex release includes the native executable.")
        with compressed.extractfile(matching[0]) as source, (target_dir / "codex").open("xb") as destination:
            shutil.copyfileobj(source, destination)
    (target_dir / "codex").chmod(0o755)
    return target_dir


def native_path(scratch, tools):
    # /usr/bin contains Python even on the clean runner. Expose the ordinary
    # tools under a staged PATH, for the installer and all of its children.
    public_tools = scratch / "native-bin"
    public_tools.mkdir()
    for directory in map(Path, os.defpath.split(os.pathsep) + ["/usr/sbin", "/sbin", "/usr/local/bin"]):
        if not directory.is_dir():
            continue
        for item in sorted(directory.iterdir()):
            name = item.name
            if re.match(r"(?i)(python|pypy|pip|uv|gh|codex|tailscale|xdg-open|open$|sensible-browser)", name):
                continue
            destination = public_tools / name
            try:
                executable = item.is_file() and os.access(item, os.X_OK)
            except PermissionError:
                continue
            if executable and not destination.is_symlink():
                destination.symlink_to(item)
    return os.pathsep.join(map(str, (tools, public_tools)))


@dataclass
class Profile:
    folder: Path
    path: str
    # A Codex that has started once has its home folder. The published
    # v0.25.1 installer fails Codex setup without it (trial run 45125267933),
    # so only the stock rows, which witness that, start without one.
    codex_started: bool = True

    def __post_init__(self):
        self.folder.mkdir(mode=0o700)
        for name in ("tmp", ".local/bin", "config", "cache", "data"):
            (self.folder / name).mkdir(parents=True)
        if self.codex_started:
            self.codex.mkdir(mode=0o700)

    @property
    def codex(self):
        return self.folder / ".codex"

    @property
    def env(self):
        env = {
            "HOME": str(self.folder),
            "CODEX_HOME": str(self.codex),
            "PATH": self.path + os.pathsep + str(self.folder / ".local/bin"),
            "TMPDIR": str(self.folder / "tmp"),
            "XDG_CONFIG_HOME": str(self.folder / "config"),
            "XDG_CACHE_HOME": str(self.folder / "cache"),
            "XDG_DATA_HOME": str(self.folder / "data"),
            "LC_ALL": "C",
            "PSEUDOLIFE_UNATTENDED": "1",
            "BROWSER": "/usr/bin/false",
        }
        for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def registration(self):
        config = self.codex / "config.toml"
        if config.is_file():
            return tomllib.loads(config.read_text())["mcp_servers"]["pseudolife-memory"]
        raise Failed("Codex registered the installed launcher.")

    def launcher(self):
        return self.registration()["command"]

    @property
    def installed_launcher(self):
        return self.folder / "data" / "pseudolife-mcp" / "bin" / CLIENT

    def native_marker(self, release):
        launcher = self.installed_launcher
        if not launcher.is_file():
            return False
        active_digest = sha256(launcher)
        markers = [
            json.loads(marker.read_bytes())
            for marker in self.folder.rglob("runtime.json")
            if marker.is_file()
        ]
        return any(marker.get("kind") == "native" and marker.get("version") == release.version
                   and marker.get("archive_sha256") == release.digest
                   and marker.get("binary_sha256") == active_digest for marker in markers)

    def fingerprint(self):
        files = [self.installed_launcher, self.codex / "config.toml", *self.folder.rglob("runtime.json")]
        return {file.relative_to(self.folder): sha256(file) for file in files if file.is_file()}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get(url, token=None):
    start = monotonic()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(Request(url, headers=headers), timeout=5) as response:
            status = response.status
            first = response.read(1)
            ttfb = monotonic() - start
            content = first + response.read(1048576)
    except HTTPError as error:
        return {"status": error.code, "elapsed_ms": round((monotonic() - start) * 1000)}, {}
    except OSError:
        return {"status": None, "elapsed_ms": round((monotonic() - start) * 1000)}, {}
    evidence = {"status": status, "ttfb_ms": round(ttfb * 1000),
                "elapsed_ms": round((monotonic() - start) * 1000)}
    try:
        return evidence, json.loads(content)
    except ValueError:
        return evidence, {}


@dataclass
class Peer:
    kind: str
    url: str
    token_file: Path
    owner_executable: str
    owner_env: dict[str, str]
    checkout: Path
    engine: str
    # A source-built peer's bank, for the client's file-tier backup.
    database_url: str | None = None
    data_dir: Path | None = None
    tools: str | None = None

    def sample(self):
        return get(self.url + "/api/episodes?limit=1", self.token_file.read_text().strip())[0]

    def during(self, receipt, run):
        samples = []
        sampler = Thread(target=lambda: samples.append(self.sample()), daemon=True)
        sampler.start()
        result = run()
        sampler.join(timeout=10)
        receipt.steps.append({
            "command": "GET /api/episodes?limit=1",
            "assertion": "Authenticated read sampled at the start of the operation.",
            "http": samples[0] if samples else {"status": None},
        })
        return result


def mcp_failure(step, payload):
    """A sanitised MCP error or error result, for the row that met it."""
    text = json.dumps(payload, indent=1) if not isinstance(payload, str) else payload
    failure = REDACTOR.failure(step, None, text.encode(), lines=TAIL_MCP)
    failure["error_class"] = "mcp-error"
    return failure


TAIL_MCP = 16


class Mcp:
    def __init__(self, process):
        self.process = process
        self.lines = Queue()
        self.id = 0
        # Codex names its thread in each tool call's _meta, and a shim
        # registered for Codex refuses board calls without one (trial run
        # 45131084076), so the harness speaks for one thread per session.
        self.thread = str(uuid.uuid4())
        Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, method, params, *, notification=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        self.id += 1
        if not notification:
            message["id"] = self.id
        try:
            self.process.stdin.write(json.dumps(message).encode() + b"\n")
            self.process.stdin.flush()
        except OSError:
            raise Failed("The installed stdio shim accepted the request.") from None
        if notification:
            return None
        deadline = monotonic() + 40
        try:
            while True:
                line = self.lines.get(timeout=max(0, deadline - monotonic()))
                require(line is not None, "The installed stdio shim returned a response.")
                answer = json.loads(line)
                if answer.get("id") == self.id:
                    require("result" in answer, "The MCP request returned a result.",
                            mcp_failure(f"MCP {method} {params.get('name', '')}".strip(), answer.get("error")))
                    return answer["result"]
        except (Empty, ValueError):
            raise Failed("The installed stdio shim answered with valid MCP JSON before its deadline.") from None

    def tool(self, name, **arguments):
        result = self.send("tools/call", {"name": name, "arguments": arguments,
                                          "_meta": {"threadId": self.thread}})
        require(not result.get("isError"), "The MCP tool completed successfully.",
                mcp_failure(f"MCP tools/call {name}", result.get("content")))
        if "structuredContent" in result:
            return result["structuredContent"]
        try:
            return json.loads(result["content"][0]["text"])
        except (IndexError, KeyError, ValueError):
            raise Failed("The MCP response contained the tool's structured result.") from None


@contextmanager
def session(executable, args, env, cwd):
    # The shim's stderr is the only place its refusals appear. It goes to a
    # private file and reaches a receipt only through the redactor.
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
            [executable, *args], env=env, cwd=cwd, start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors) as process:
        try:
            mcp = Mcp(process)
            result = mcp.send("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "hosted-acceptance", "version": "1"},
            })
            require("protocolVersion" in result, "The installed shim negotiated the MCP protocol.")
            mcp.send("notifications/initialized", {}, notification=True)
            yield mcp
        except Failed as error:
            if error.failure is None:
                with suppress(OSError):
                    process.stdin.close()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                error.failure = explain("installed stdio shim <stderr>",
                                        Result(process.returncode, read_tail(errors), 0))
            raise
        finally:
            with suppress(OSError):
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)


@contextmanager
def registered_session(profile, checkout):
    registration = profile.registration()
    env = profile.env | registration.get("env", {})
    with session(registration["command"], registration.get("args", []), env, checkout) as mcp:
        yield mcp


@dataclass
class Receipt:
    scenario: str
    platform: str
    daemon: str = "none"
    daemon_engine: str | None = None
    steps: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    status: str = "not yet run"
    note: str = "The scenario was not selected."
    cleanup_failed: bool = False
    fixture_notes: list = field(default_factory=list)

    def observe(self, label, result, ok, assertion, *, template=None, required=True):
        # The labels and assertions are fixed call-site strings. Never
        # interpolate subprocess data, endpoints, paths, or credentials into
        # them. Output reaches the receipt only through the redactor.
        step = {"command": label, "exit_code": result.code, "observed": bool(ok),
                "assertion": assertion, "seconds": result.seconds}
        if not ok:
            step["failure"] = explain(label, result)
        elif warnings := REDACTOR.cause_lines(result.output):
            # A step that met its assertion can still warn about what a later
            # step then fails on (a client the installer did not register).
            step["warning_lines"] = warnings
        self.steps.append(step)
        if template is not None:
            self.commands.append({"command": template, "exit_code": result.code})
        if required:
            require(ok, assertion)

    def explain(self, error):
        """Keep the sanitised output an exception carried into this row."""
        if getattr(error, "failure", None) is not None:
            self.steps.append({"command": error.failure["step"], "exit_code": error.failure["exit_code"],
                               "observed": False, "assertion": str(error), "failure": error.failure})

    def checkpoint(self, output, release):
        self._write_receipt(output, release, False, provisional=True)

    def write(self, output, release, cleanup):
        cleanup = cleanup and not self.cleanup_failed
        identifies_daemon = self.daemon in matrix.DAEMON_KINDS and self.daemon_engine in matrix.DAEMON_ENGINES
        if self.status == "pass":
            if release is None or not self.commands or not identifies_daemon:
                self.status, self.note = "fail", "The scenario lacked release, command or daemon evidence."
            elif not cleanup:
                self.status, self.note = "fail", "Fixture cleanup was not verified."
        row = matrix.pending(self.scenario, self.platform)
        if release is not None and self.commands and self.status != "not yet run":
            row.update({
                "status": self.status, "commit": release.commit, "archive": release.archive.name,
                "sha256": release.digest, "machine_role": "ephemeral hosted runner",
                "commands": self.commands,
                "outcome": (matrix.SCENARIOS[self.scenario].outcome if self.status == "pass"
                            else matrix.FAILURE_OUTCOMES[3 if self.cleanup_failed else 2]),
                "cleanup": {"done": cleanup, "verified": cleanup},
            })
            if identifies_daemon:
                row.update(daemon=self.daemon, daemon_engine=self.daemon_engine)
        matrix.write_record(output / "ledger", row)
        self._write_receipt(output, release, cleanup)

    def _write_receipt(self, output, release, cleanup, *, provisional=False):
        status, note = self.status, self.note
        if provisional and status == "pass":
            status, note = "not yet run", "The scenario met its assertions. Fixture cleanup remains unverified."
        receipt = {
            "scenario": self.scenario, "platform": self.platform, "daemon": self.daemon,
            "daemon_engine": self.daemon_engine, "status": status, "assertion": REDACTOR.text(note),
            "steps": self.steps, "cleanup_verified": cleanup,
        }
        if self.fixture_notes:
            receipt["fixture_notes"] = list(dict.fromkeys(self.fixture_notes))
        if self.daemon in matrix.DAEMON_KINDS[2:]:
            receipt["peer_note"] = PEER_NOTE
        if release is not None:
            receipt.update(release_commit=release.commit, archive=release.archive.name, archive_sha256=release.digest,
                           release_verified_by=getattr(release, "verified_by", None))
        write_json(output / f"{self.scenario}.json", receipt, replace=True)


def write_json(path, payload, *, replace=False):
    target = path.with_suffix(".tmp") if replace else path
    with target.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    if replace:
        target.replace(path)


LABELS = {"linux-arm64": "Linux arm64", "macos-x64": "macOS Intel", "macos-arm64": "macOS Apple silicon"}
# A reason is printed only when it is one plain sentence, so a receipt cannot
# smuggle output or markup into the public summary.
REASON = re.compile(r"[A-Z][A-Za-z0-9 _,.;:()'/#+=-]{8,298}[.]")


def cell(path, selected):
    """One summary cell: its display text, its count category and its reason."""
    if not selected:
        return "not selected", None, None
    if not path.is_file():
        return "result unavailable", "missing", "The job uploaded no receipt for this cell."
    try:
        result = json.loads(path.read_bytes())
    except ValueError:
        return "invalid result", "missing", "The receipt was not valid JSON."
    status = result.get("status") if isinstance(result, dict) else None
    daemon = result.get("daemon") if isinstance(result, dict) else None
    engine = result.get("daemon_engine") if isinstance(result, dict) else None
    known = status in OUTCOMES and daemon in DAEMONS and (engine in matrix.DAEMON_ENGINES or engine is None)
    pass_provenance = status != "pass" or (daemon in matrix.DAEMON_KINDS and engine in matrix.DAEMON_ENGINES)
    if not (known and pass_provenance):
        return "invalid result", "missing", "The receipt used values outside the published vocabulary."
    shown = "cannot run" if status == "not yet run" else status
    text = f"{shown} / {daemon}" + (f" / {engine}" if engine else "")
    reason = result.get("assertion")
    if status == "pass":
        reason = None
    elif not (isinstance(reason, str) and REASON.fullmatch(reason)):
        reason = "The receipt's reason was not a plain sentence."
    return text, {"pass": "pass", "fail": "fail"}.get(status, "cannot run"), reason


def summary(selection, folder):
    rows, counts, reasons = [], dict.fromkeys(("pass", "fail", "cannot run", "missing"), 0), []
    for scenario in matrix.SCENARIOS:
        statuses = []
        for platform in PLATFORMS:
            text, category, reason = cell(folder / f"acceptance-{platform}" / f"{scenario}.json",
                                          platform in SELECTIONS[selection])
            statuses.append(text)
            if category is not None:
                counts[category] += 1
            if reason is not None:
                reasons.append(f"- {LABELS[platform]}, {scenario} ({category}): {reason}")
        rows.append(f"| {scenario} | {' | '.join(statuses)} |")
    selected = sum(counts.values())
    verdict = "pass" if selected and counts["pass"] == selected else "no pass"
    # A green job is not a pass: only every selected cell passing is.
    print(f"**Verdict: {verdict}.** {counts['pass']} pass, {counts['fail']} fail, "
          f"{counts['cannot run']} cannot run and {counts['missing']} missing, of {selected} selected cells.")
    print()
    print("| Scenario | Linux arm64 | macOS Intel | macOS Apple silicon |")
    print("| --- | --- | --- | --- |")
    for row in rows:
        print(row)
    if reasons:
        print()
        print("Cells without a pass, one reason each:")
        print()
        for reason in reasons:
            print(reason)
    return verdict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("tag")
    run.add_argument("selection", choices=SELECTIONS)
    run.add_argument("platform", choices=PLATFORMS)
    run.add_argument("output", type=Path)
    show = subparsers.add_parser("summary")
    show.add_argument("selection", choices=SELECTIONS)
    show.add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.action == "summary":
        summary(args.selection, args.folder)
        return 0
    if args.platform not in SELECTIONS[args.selection]:
        return 0
    args.output.mkdir()
    (args.output / "ledger").mkdir()
    if args.platform == "linux-arm64":
        from . import hosted_linux

        return hosted_linux.run(args.tag, args.platform, args.output)
    from . import hosted_macos

    return hosted_macos.run(args.tag, args.platform, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
