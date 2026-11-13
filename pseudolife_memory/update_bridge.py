"""The update bridge: a handed-off client update moves the machine to the
native client.

An installed Python updater (0.23.x) installs the next release as a Python
shim runtime and hands the client steps after the shim to it
(``update_cli.Update._hand_off``, ``client-handoff-v1``). That child is this
release's Python code. When it starts as such a child, and this release
publishes a native client for this platform, it fetches that client's
archive and ``SHA256SUMS`` from the release, verifies the digest, and runs
the native updater's client steps in its place with the hand-off variables
passed through unchanged:

    <native> update --clients-only --tag <version> --daemon-url <url> [--allow-downgrade]

The native child installs the native runtime, activates the native
launcher, migrates the registrations, runs the client steps and writes the
report the Python parent reads; its exit code is this process's.

Nothing here changes anything until the native binary has been verified and
has answered both probes: a release with no archive for this platform, a
failed download or digest check, or a binary that will not run here (an
old glibc) leaves the Python client in place, says why in one line, and the
Python code finishes the client steps as before. ``PSEUDOLIFE_SHIM_ENGINE=
python`` keeps the Python client. Standard library only: a shim runtime
carries ``runtimes.SHIM_REQUIREMENTS`` and nothing else.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from pseudolife_memory import __version__

HANDOFF_ENV = "PSEUDOLIFE_UPDATE_HANDOFF"
ENGINE_ENV = "PSEUDOLIFE_SHIM_ENGINE"
# The same override the native installer honours
# (rust/shim/src/runtime/install.rs, release_base): the http(s) directory
# holding the archives and SHA256SUMS, for a mirror or a test server.
ARCHIVE_URL_ENV = "PSEUDOLIFE_NATIVE_ARCHIVE_URL"
RELEASE_BASE = "https://github.com/Pseudogiant-xr/Pseudolife-MCP/releases/download/v{version}"
# GitHub serves a release asset from github.com by a redirect to one of
# these; a redirect anywhere else is refused.
GITHUB_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                          "release-assets.githubusercontent.com"})
# The native installer's own limits (install.rs ARCHIVE_LIMIT, BINARY_LIMIT).
ARCHIVE_LIMIT = 128 * 1024 * 1024
BINARY_LIMIT = 256 * 1024 * 1024
SUMS_LIMIT = 1024 * 1024
START_PROBE_ANSWER = "pseudolife-mcp native-update-v1"
CLIENT_HANDOFF = "client-handoff-v1"
_TARGETS = {
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "amd64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("windows", "amd64"): "x86_64-pc-windows-msvc",
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
}


class Kept(Exception):
    """The native client could not be used; the message says why."""


def target(system: str | None = None, machine: str | None = None) -> str | None:
    """The native release target for this host, or ``None``."""
    system = (system or platform.system()).lower()
    machine = (machine if machine is not None else platform.machine()).lower()
    if not machine and system == "windows" and sys.maxsize > 2 ** 32:
        # Windows' platform.machine() reads PROCESSOR_ARCHITECTURE, which a
        # scrubbed environment lacks. A 64-bit interpreter means a 64-bit
        # Windows; an x64 binary on ARM64 runs emulated, and the probe still
        # refuses one that does not run.
        machine = "amd64"
    return _TARGETS.get((system, machine))


def binary_name(windows: bool | None = None) -> str:
    return "pseudolife-mcp.exe" if (os.name == "nt" if windows is None else windows) else "pseudolife-mcp"


def archive_name(version: str, triple: str) -> str:
    return f"pseudolife-mcp-{version}-{triple}.{'zip' if triple.endswith('-windows-msvc') else 'tar.gz'}"


def release_base(env: dict, version: str) -> tuple[str, bool]:
    """The directory URL and whether the override chose it. The override
    follows the native installer's rules: http(s), a host, no credentials,
    query or fragment."""
    configured = (env.get(ARCHIVE_URL_ENV) or "").strip()
    if not configured:
        return RELEASE_BASE.format(version=version), False
    parsed = urllib.parse.urlsplit(configured)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
        raise Kept(f"{ARCHIVE_URL_ENV} must be an http:// or https:// directory URL with a host and no "
                   f"query or fragment")
    if parsed.username or parsed.password:
        raise Kept(f"{ARCHIVE_URL_ENV} must not carry a user name or password")
    return configured.rstrip("/"), True


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to where this download may come from: over
    HTTPS to GitHub's release hosts, or for an override, to its own host."""

    def __init__(self, own_host: str | None):
        super().__init__()
        self.own_host = own_host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target_url = urllib.parse.urlsplit(newurl)
        if self.own_host is not None:
            # The override's own host, never off http(s), never from https down to http.
            source = urllib.parse.urlsplit(req.full_url).scheme
            allowed = (target_url.hostname == self.own_host and target_url.scheme in ("http", "https")
                       and not (source == "https" and target_url.scheme == "http"))
        else:
            allowed = target_url.scheme == "https" and target_url.hostname in GITHUB_HOSTS
        if not allowed:
            raise urllib.error.URLError(f"refused a redirect to {target_url.scheme}://{target_url.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, limit: int, *, overridden: bool, timeout: float = 300) -> bytes:
    """GET ``url`` (HTTPS unless an override names plain http) and return at
    most ``limit`` bytes; raises ``Kept``. A 404 means not published."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and not (overridden and parsed.scheme == "http"):
        raise Kept(f"refused a non-HTTPS download ({parsed.scheme}://{parsed.hostname})")
    opener = urllib.request.build_opener(_Redirects(parsed.hostname if overridden else None))
    request = urllib.request.Request(url, headers={"User-Agent": "pseudolife-mcp"})
    try:
        with opener.open(request, timeout=timeout) as reply:
            data = reply.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise Kept("not published") from None
        raise Kept(f"HTTP {exc.code} from {parsed.hostname}") from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise Kept(f"download from {parsed.hostname} failed: {reason}") from None
    if len(data) > limit:
        raise Kept(f"{url.rsplit('/', 1)[-1]} is larger than {limit // (1024 * 1024)} MiB")
    return data


def checksum(sums: str, name: str) -> str | None:
    """The SHA-256 ``SHA256SUMS`` lists for ``name``, or ``None`` when it
    names no such file; raises ``Kept`` for a file the native installer
    would refuse (``install.rs`` ``checksum``: every record "<64 lowercase
    hex>  <bare name>", no duplicate for the archive)."""
    lines = sums.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    found = None
    for line in lines:
        digest, sep, file = line.removesuffix("\r").partition("  ")
        if not sep or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) \
                or not file or "/" in file or "\\" in file:
            raise Kept("its SHA256SUMS holds an invalid record")
        if file == name:
            if found is not None:
                raise Kept("its SHA256SUMS lists the archive twice")
            found = digest
    return found


def extract(data: bytes, triple: str, directory: Path) -> Path:
    """Write the archive's one permitted member, the native binary, into
    ``directory`` and return its path; raises ``Kept``."""
    name = binary_name(triple.endswith("-windows-msvc"))
    try:
        if triple.endswith("-windows-msvc"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) != 1 or members[0].filename != name or members[0].file_size > BINARY_LIMIT \
                        or members[0].is_dir():
                    raise Kept("the release zip holds something other than the one native binary")
                body = archive.read(members[0])
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                members = archive.getmembers()
                if len(members) != 1 or members[0].name != name or not members[0].isreg() \
                        or members[0].size > BINARY_LIMIT:
                    raise Kept("the release tar holds something other than the one native binary")
                handle = archive.extractfile(members[0])
                body = handle.read(BINARY_LIMIT + 1) if handle else b""
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
        raise Kept(f"the release archive could not be read: {exc}") from None
    if not body or len(body) > BINARY_LIMIT:
        raise Kept("the release archive's native binary is empty or too large")
    path = directory / name
    path.write_bytes(body)
    if os.name != "nt":
        path.chmod(0o700)
    return path


Runner = Callable[..., "subprocess.CompletedProcess"]


def _probe(binary: Path, version: str, run: Runner) -> None:
    """Prove the binary runs here and speaks the hand-off for this release."""
    def answer(flag: str) -> list[str]:
        try:
            done = run([str(binary), "update", flag], stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, errors="replace", timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Kept(f"the native binary does not run on this machine ({type(exc).__name__}: {exc})") from None
        if done.returncode != 0:
            last = ((done.stderr or "") + (done.stdout or "")).strip().splitlines()[-1:] or [""]
            raise Kept(f"the native binary does not run on this machine (exit {done.returncode}"
                       + (f": {last[0][:200]}" if last[0] else "") + ")")
        return [line.strip() for line in (done.stdout or "").strip().splitlines()]

    if answer("--native-capability") != [START_PROBE_ANSWER]:
        raise Kept("the native binary did not answer as a native updater")
    lines = answer("--client-handoff-protocols")
    if not lines or lines[0] != version or len(lines) < 2 or CLIENT_HANDOFF not in lines[1].split():
        raise Kept(f"the native binary is not release {version} with the client hand-off")


# Shorter than the 10 s the handing-off Python parent waits for this
# process after Ctrl+C (update_cli.run_handoff), so the native child is
# stopped here, never left running after the parent released the lock.
INTERRUPT_GRACE = 6


def _wait(proc: subprocess.Popen) -> int:
    try:
        return proc.wait()
    except BaseException:
        # Ctrl+C reaches the child through the shared console too: give it
        # a moment to stop between steps before it is killed, and kill it
        # even when a second interrupt cuts the moment short.
        try:
            proc.wait(timeout=INTERRUPT_GRACE)
        except BaseException:
            proc.kill()
            proc.wait()
        raise


def bridge(arguments: list[str], parser_factory, *, env: dict | None = None,
           run: Runner = subprocess.run, popen=subprocess.Popen,
           out: Callable[[str], None] | None = None) -> int | str | None:
    """Run the client steps in this release's native updater when this
    process is a handed-off client update. Returns the native child's exit
    code once it ran; a one-line note when the Python client was kept and
    this process finishes the steps itself; ``None`` when this is not a
    hand-off child (nothing looked at, nothing said)."""
    env = os.environ if env is None else env
    marker = env.get(HANDOFF_ENV) or ""
    if not (marker.isascii() and marker.isdigit()):
        return None
    try:
        parser = parser_factory()
        args = parser.parse_args(arguments)
        defaults = vars(parser.parse_args([]))
    except SystemExit:
        return None
    # Exactly what Update._hand_off starts, every other option at its
    # default; anything else is a hand-run and stays Python.
    sent = {"clients_only", "tag", "daemon_url", "allow_downgrade"}
    if not args.clients_only or args.tag is None or any(
            value != defaults[key] for key, value in vars(args).items() if key not in sent):
        return None
    if env.get(ENGINE_ENV) == "python":     # the exact value runtimes.py honours
        return f"{ENGINE_ENV}=python keeps the Python client; this run's own code finishes the client steps"
    version = __version__
    kept = "the Python client was kept and this run's own code finishes the client steps"
    if args.tag != version:
        return f"the native client was not installed (this runtime is {version}, not {args.tag}); {kept}"
    triple = target()
    if triple is None:
        return (f"the native client was not installed (no native client is built for "
                f"{platform.system()} {platform.machine()}); {kept}")
    scratch: Path | None = None
    try:
        try:
            scratch = Path(tempfile.mkdtemp(prefix="pseudolife-bridge-"))
            base, overridden = release_base(env, version)
            name = archive_name(version, triple)
            try:
                sums = fetch(f"{base}/SHA256SUMS", SUMS_LIMIT, overridden=overridden).decode("utf-8", "replace")
            except Kept as exc:
                raise Kept(f"no native client is published in release v{version}" if str(exc) == "not published"
                           else f"its SHA256SUMS: {exc}") from None
            digest = checksum(sums, name)
            if digest is None:
                raise Kept(f"no native client is published for {triple} in release v{version}")
            try:
                data = fetch(f"{base}/{name}", ARCHIVE_LIMIT, overridden=overridden)
            except Kept as exc:
                raise Kept(f"no native client is published for {triple} in release v{version}"
                           if str(exc) == "not published" else f"{name}: {exc}") from None
            if hashlib.sha256(data).hexdigest() != digest:
                raise Kept(f"{name} does not match its SHA256SUMS digest")
            binary = extract(data, triple, scratch)
            _probe(binary, version, run)
        except Kept as exc:
            note = (f"; from {ARCHIVE_URL_ENV}={env.get(ARCHIVE_URL_ENV, '').strip()}, unset it to use the "
                    f"GitHub release" if (env.get(ARCHIVE_URL_ENV) or "").strip() else "")
            return f"the native client was not installed ({exc}{note}); {kept}"
        except Exception as exc:  # noqa: BLE001 - nothing has changed yet: Python is always a safe fallback
            return f"the native client was not installed ({type(exc).__name__}: {exc}); {kept}"
        argv = [str(binary), "update", "--clients-only", "--tag", version, "--daemon-url", args.daemon_url]
        if args.allow_downgrade:
            argv.append("--allow-downgrade")
        say = out or (lambda line: print(line, flush=True))
        say(
            f"==> moving this machine to the native client: native {version} ({triple}) runs the client steps")
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            # The hand-off variables pass through unchanged: the native child
            # borrows the lock from the same parent and writes its report.
            proc = popen(argv, stdin=subprocess.DEVNULL, env=dict(env))
        except OSError as exc:
            return f"the native client was not installed (it did not start: {exc}); {kept}"
        code = _wait(proc)
        if code != 0:
            say(f"==> the native client steps ended with exit {code}; to keep the Python client instead, rerun "
                f"with {ENGINE_ENV}=python set")
        return code
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
