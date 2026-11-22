"""Run release acceptance in a disposable GitHub-hosted job."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
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
from threading import Thread
from time import monotonic
import tomllib
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import matrix


PROJECT = "Pseudogiant-xr/Pseudolife-MCP"
RELEASE = f"https://github.com/{PROJECT}/releases/download"
API = f"https://api.github.com/repos/{PROJECT}/releases/tags"
PLATFORMS = ("linux-arm64", "macos-x64", "macos-arm64")
SELECTIONS = {**{platform: (platform,) for platform in PLATFORMS},
              "all": PLATFORMS, "macos": PLATFORMS[1:]}
DAEMONS = ("none", "built on the runner by the tag's installer (native arm64)",
           "published image in a Linux guest", "source-built from the tag")
OUTCOMES = ("pass", "fail", "not yet run")
INSTALL = "PSEUDOLIFE_SHIM_ENGINE=native bash ops/install.sh"
CLIENT = "pseudolife-mcp"
TOKEN_FILE = "PSEUDOLIFE_MCP_TOKEN_FILE"
DAEMON_URL = "PSEUDOLIFE_MCP_DAEMON_URL"
CODEX_ASSETS = {
    "linux-arm64": ("rust-v0.134.0", "aarch64-unknown-linux-musl",
                    "8e066f998111eb8b44250ac11df004daa07fadf276c5942a7183cb8e421091a3"),
    "macos-x64": ("rust-v0.171.0", "x86_64-apple-darwin",
                  "6f9b1144ffaa64aa12bf30f312405b266b84c433bae9f070be10245762c7aa44"),
    "macos-arm64": ("rust-v0.171.0", "aarch64-apple-darwin",
                    "9414c8c967f110b11e8324c0cfa2b20f012094ef890110a561c92ee535a2681a"),
}


class Unavailable(Exception):
    """The runner cannot supply a prerequisite for an observation."""


class Failed(Exception):
    """An assertion about the release failed."""


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


def command(argv, *, env=None, cwd=None, timeout=60, data=None) -> Result:
    """Keep arbitrary subprocess output and argv outside published evidence."""
    started = monotonic()
    with subprocess.Popen(
        argv, cwd=cwd, env=env, start_new_session=True,
        stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    ) as process:
        try:
            output, _ = process.communicate(data, timeout=timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
            code = 124
    return Result(code, output, round(monotonic() - started, 3))


def require(ok, reason):
    if not ok:
        raise Failed(reason)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, path, digest=None, *, timeout=180):
    result = command(
        ["curl", "--disable", "--fail", "--silent", "--show-error", "--location",
         "--proto", "=https", "--proto-redir", "=https", "--max-time", str(timeout),
         "--output", path, url],
        timeout=timeout + 5,
    )
    require(result.code == 0, "A required public download failed.")
    if digest is not None:
        require(sha256(path) == digest, "A download disagreed with its published SHA-256.")


@dataclass(frozen=True)
class Release:
    tag: str
    platform: str
    folder: Path
    commit: str
    archive: Path
    digest: str

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
    download(f"{API}/{tag}", metadata)
    release = json.loads(metadata.read_bytes())
    require(release.get("tag_name") == tag and release.get("draft") is False,
            "The selected tag is a published release.")
    assets = {asset["name"]: asset.get("digest", "") for asset in release["assets"]}

    def fetch(name, digest=None):
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
    if "RELEASE-READY.json" in assets:
        fetch("RELEASE-READY.json")
    remote = command(["git", "ls-remote", "--exit-code", f"https://github.com/{PROJECT}.git",
                      f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"])
    require(remote.code == 0, "Published tag resolved.")
    refs = {ref: sha for sha, ref in (line.split() for line in remote.output.decode("ascii").splitlines())}
    commit = refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}", ""))
    require(re.fullmatch(r"[a-f0-9]{40}", commit), "Published tag resolved to a full commit.")
    ready = folder / "RELEASE-READY.json"
    if ready.is_file():
        require(json.loads(ready.read_bytes())["commit"] == commit, "Release readiness names the tag commit.")
    return Release(tag, platform, folder, commit, folder / archive, digests[archive])


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
            if not destination.is_symlink() and item.is_file() and os.access(item, os.X_OK):
                destination.symlink_to(item)
    return os.pathsep.join(map(str, (tools, public_tools)))


@dataclass
class Profile:
    folder: Path
    path: str

    def __post_init__(self):
        self.folder.mkdir(mode=0o700)
        for name in ("tmp", ".local/bin", "config", "cache", "data"):
            (self.folder / name).mkdir(parents=True)

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


class Mcp:
    def __init__(self, process):
        self.process = process
        self.lines = Queue()
        self.id = 0
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
                    require("result" in answer, "The MCP request returned a result.")
                    return answer["result"]
        except (Empty, ValueError):
            raise Failed("The installed stdio shim answered with valid MCP JSON before its deadline.") from None

    def tool(self, name, **arguments):
        result = self.send("tools/call", {"name": name, "arguments": arguments})
        require(not result.get("isError"), "The MCP tool completed successfully.")
        if "structuredContent" in result:
            return result["structuredContent"]
        try:
            return json.loads(result["content"][0]["text"])
        except (IndexError, KeyError, ValueError):
            raise Failed("The MCP response contained the tool's structured result.") from None


@contextmanager
def session(executable, args, env, cwd):
    with subprocess.Popen([executable, *args], env=env, cwd=cwd, start_new_session=True,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        try:
            mcp = Mcp(process)
            result = mcp.send("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "hosted-acceptance", "version": "1"},
            })
            require("protocolVersion" in result, "The installed shim negotiated the MCP protocol.")
            mcp.send("notifications/initialized", {}, notification=True)
            yield mcp
        finally:
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
    steps: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    status: str = "not yet run"
    note: str = "The scenario was not selected."

    def observe(self, label, result, ok, assertion, *, template=None, required=True):
        # The labels and assertions are fixed call-site strings. Never
        # interpolate subprocess data, endpoints, paths, or credentials.
        self.steps.append({"command": label, "exit_code": result.code, "observed": bool(ok),
                           "assertion": assertion, "seconds": result.seconds})
        if template is not None:
            self.commands.append({"command": template, "exit_code": result.code})
        if required:
            require(ok, assertion)

    def write(self, output, release, cleanup):
        row = matrix.pending(self.scenario, self.platform)
        if release is not None and self.commands and self.status != "not yet run":
            row.update({
                "status": self.status, "commit": release.commit, "archive": release.archive.name,
                "sha256": release.digest, "machine_role": "ephemeral hosted runner",
                "commands": self.commands,
                "outcome": (matrix.SCENARIOS[self.scenario].outcome if self.status == "pass"
                            else matrix.FAILURE_OUTCOMES[3 if self.note.startswith("The disposable environment") else 2]),
                "cleanup": {"done": cleanup, "verified": cleanup},
            })
        matrix.write_record(output / "ledger", row)
        receipt = {
            "scenario": self.scenario, "platform": self.platform, "daemon": self.daemon,
            "status": self.status, "assertion": self.note, "steps": self.steps, "cleanup_verified": cleanup,
        }
        if release is not None:
            receipt.update(release_commit=release.commit, archive=release.archive.name, archive_sha256=release.digest)
        write_json(output / f"{self.scenario}.json", receipt)


def write_json(path, payload):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")


def summary(selection, folder):
    print("| Scenario | Linux arm64 | macOS Intel | macOS Apple silicon |")
    print("| --- | --- | --- | --- |")
    for scenario in matrix.SCENARIOS:
        statuses = []
        for platform in PLATFORMS:
            path = folder / f"acceptance-{platform}" / f"{scenario}.json"
            if platform not in SELECTIONS[selection]:
                statuses.append("not selected")
            elif not path.is_file():
                statuses.append("result unavailable")
            else:
                result = json.loads(path.read_bytes())
                status = result.get("status")
                daemon = result.get("daemon")
                statuses.append(f"{status} / {daemon}" if status in OUTCOMES and daemon in DAEMONS
                                else "invalid result")
        print(f"| {scenario} | {' | '.join(statuses)} |")


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
