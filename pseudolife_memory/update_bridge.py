"""The update bridge: a client update that asks for it moves the machine to
the native client.

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
Python code finishes the client steps as before.

Since 0.26.0 the move is the default (it was opt-in, with
``PSEUDOLIFE_SHIM_ENGINE=native``, in 0.24.0 and 0.25.x). It does not happen,
and the Python client is kept with one line saying why:
- with ``PSEUDOLIFE_SHIM_ENGINE=python``, or when
  ``~/.pseudolife-mcp/client-engine`` records that the installer was run with
  it (unless ``PSEUDOLIFE_SHIM_ENGINE=native`` asks again);
- when the update runs without a person (maintainer, 20 November 2026): the
  hand-off parent's own argv holds ``--unattended`` (the scheduled run) or
  ``--result-file`` (the shim's session-start client update), or cannot be
  read;
- when the native client cannot serve the machine (no build for it, a
  download or digest failure, a binary that does not run here, or, unless
  asked for, a tunnel the Python client registered to start by itself).
A machine already on this release's Python client moves when it runs
``pseudolife-mcp update --clients-only``, as an ordinary native run with no
hand-off variables.
Standard library only: a shim runtime carries ``runtimes.SHIM_REQUIREMENTS``
and nothing else.
"""
from __future__ import annotations

import contextlib
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
# The value that asks for the move to the native client even where the
# default keeps Python (a recorded Python choice, a tunnel autostart).
NATIVE = "native"
# What an update without a person puts in the hand-off parent's argv: the
# scheduled run (unattended_update._command) and the shim's session-start
# client update (shim.py, --result-file).
UNATTENDED_FLAGS = ("--unattended", "--result-file")
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


def _python_shim_runtime(layout=None, prefix: str | None = None) -> bool:
    """Whether this interpreter runs from a Python shim runtime whose stable
    launcher is the Python one: the only machine a hand-run moves. Only a
    launcher that reads as Python counts. On Windows a native launcher
    reads as ``unknown`` (an MZ header alone cannot tell it from a distlib
    launcher), and a missing or unreadable one is left alone too."""
    try:
        from pseudolife_memory import runtimes
        layout = runtimes.default_layout() if layout is None else layout
        prefix = sys.prefix if prefix is None else prefix
        return (runtimes._under(str(Path(prefix)), layout.root)
                and runtimes._launcher_kind(layout) == "python")
    except Exception:  # noqa: BLE001 - a layout that cannot be read is not one to move
        return False


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


def engine_record() -> Path:
    """Where the installers record an explicit PSEUDOLIFE_SHIM_ENGINE=python
    install (ops/install.sh, ops/install.ps1: client engine)."""
    return Path.home() / ".pseudolife-mcp" / "client-engine"


def _recorded_python() -> bool:
    try:
        return engine_record().read_text(encoding="utf-8").strip() == "python"
    except (OSError, UnicodeDecodeError):
        return False


def process_argv(pid: int) -> list[str] | None:
    """Process ``pid``'s argv, or None when it cannot be read. Read in place,
    starting no process: /proc on Linux, KERN_PROCARGS2 on macOS, the PEB's
    command line on Windows (split as the C runtime splits it)."""
    try:
        if sys.platform.startswith("linux"):
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            return [os.fsdecode(part) for part in raw.split(b"\0")[:-1]] if raw.endswith(b"\0") else None
        import ctypes
        if sys.platform == "darwin":
            import ctypes.util
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            mib = (ctypes.c_int * 3)(1, 49, pid)            # CTL_KERN, KERN_PROCARGS2
            size = ctypes.c_size_t(0)
            if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 4:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
                return None
            data = buf.raw[:size.value]
            argc = int.from_bytes(data[:4], sys.byteorder)
            rest = data[4:].split(b"\0", 1)[1].lstrip(b"\0")    # past the executable path
            words = rest.split(b"\0")
            return [os.fsdecode(word) for word in words[:argc]] if len(words) > argc else None
        if os.name == "nt":
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll")
            shell32 = ctypes.WinDLL("shell32")
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            ntdll.NtQueryInformationProcess.restype = ctypes.c_long
            ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                        wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
            shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
            shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]

            class UnicodeString(ctypes.Structure):
                _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                            ("Buffer", ctypes.c_void_p)]

            handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                size = wintypes.ULONG(0)
                ntdll.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))  # command line
                if size.value < ctypes.sizeof(UnicodeString):
                    return None
                buf = ctypes.create_string_buffer(size.value)
                if ntdll.NtQueryInformationProcess(handle, 60, buf, size, ctypes.byref(size)) != 0:
                    return None
                text = UnicodeString.from_buffer(buf)
                line = ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
            finally:
                kernel32.CloseHandle(handle)
            count = ctypes.c_int(0)
            words = shell32.CommandLineToArgvW(line, ctypes.byref(count))
            if not words:
                return None
            try:
                return [words[i] for i in range(count.value)]
            finally:
                kernel32.LocalFree(words)
    except (OSError, ValueError, AttributeError):
        return None
    return None


def ran_unattended(marker: str, parent_argv: Callable[[int], list[str] | None] = process_argv) -> bool:
    """Whether the hand-off parent (``marker`` is its pid) runs without a
    person: its argv holds one of UNATTENDED_FLAGS, or cannot be read (then
    it might, so the default keeps the client as it is)."""
    argv = parent_argv(int(marker))
    if not argv:
        return True
    return any(word in UNATTENDED_FLAGS or word.startswith("--result-file=") for word in argv[1:])


def bridge(arguments: list[str], parser_factory, *, env: dict | None = None,
           run: Runner = subprocess.run, popen=subprocess.Popen,
           out: Callable[[str], None] | None = None,
           parent_argv: Callable[[int], list[str] | None] = process_argv) -> int | str | None:
    """Run the client steps in this release's native updater when this
    process is a handed-off client update, or a hand-run ``update
    --clients-only``, that asked for the native client. Returns the native
    child's exit code once it ran; a one-line note when the Python client
    was kept and this process finishes the steps itself; ``None`` when this
    is neither (nothing looked at, nothing said)."""
    env = os.environ if env is None else env
    marker = env.get(HANDOFF_ENV) or ""
    handed_off = marker.isascii() and marker.isdigit()
    engine = env.get(ENGINE_ENV)
    # A hand-run moves unless it asks for the Python client, from a shim
    # runtime whose launcher is not native already (a native machine reaches
    # this code only through its explicit --python-runtime legacy updater).
    if not handed_off and (engine == "python" or not _python_shim_runtime()):
        return None
    try:
        parser = parser_factory()
        # Silent: main() parses these arguments again and reports their help
        # or their usage error itself, once.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            args = parser.parse_args(arguments)
            defaults = vars(parser.parse_args([]))
    except SystemExit:
        return None
    version = __version__
    # Exactly what Update._hand_off starts, or a hand-run `update
    # --clients-only` for this release, every other option at its default;
    # anything else stays Python.
    sent = {"clients_only", "tag", "daemon_url", "allow_downgrade"}
    if not args.clients_only or any(
            value != defaults[key] for key, value in vars(args).items() if key not in sent):
        return None
    if handed_off and args.tag is None:
        return None
    if not handed_off and args.tag not in (None, version):
        return None
    if engine == "python":     # the exact value runtimes.py honours
        return f"{ENGINE_ENV}=python keeps the Python client; this run's own code finishes the client steps"
    # An update that runs without a person never changes the client
    # (maintainer, 20 November 2026), whatever the setting.
    if handed_off and ran_unattended(marker, parent_argv):
        return ("this update ran unattended, so this machine keeps the Python client, and this run's own code "
                "finishes the client steps; to move it to the native client, run "
                "`pseudolife-mcp update --clients-only`")
    move = (f"`{ENGINE_ENV}={NATIVE} pseudolife-mcp update --clients-only` (in PowerShell: "
            f"`$env:{ENGINE_ENV}='{NATIVE}'; pseudolife-mcp update --clients-only`)")
    if engine != NATIVE and _recorded_python():
        return (f"{engine_record()} records an earlier {ENGINE_ENV}=python install, so this machine keeps the "
                f"Python client, and this run's own code finishes the client steps; to move it to the native "
                f"client, run {move}")
    triple = target()
    kept = "the Python client was kept and this run's own code finishes the client steps"
    if handed_off and args.tag != version:
        return f"the native client was not installed (this runtime is {version}, not {args.tag}); {kept}"
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
            if engine != NATIVE:
                # By default a tunnel the Python client registered to start
                # by itself keeps that client (#1632): the staged binary's
                # check exits 3 and prints the guard's line, which names the
                # removal; asked for, the native updater refuses it itself.
                check = run([str(binary), "runtime", "install", "--check"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120, stdin=subprocess.DEVNULL)
                if check.returncode == 3:
                    for line in f"{check.stderr or ''}\n{check.stdout or ''}".splitlines():
                        if line.strip():
                            (out or (lambda text: print(text, flush=True)))(line.rstrip())
                    raise Kept(f"a tunnel the Python client registered to start by itself keeps it; the line "
                               f"above says how to remove that autostart, then run {move}")
        except Kept as exc:
            note = (f"; from {ARCHIVE_URL_ENV}={env.get(ARCHIVE_URL_ENV, '').strip()}, unset it to use the "
                    f"GitHub release" if (env.get(ARCHIVE_URL_ENV) or "").strip() else "")
            return f"the native client was not installed ({exc}{note}); {kept}"
        except Exception as exc:  # noqa: BLE001 - nothing has changed yet: Python is always a safe fallback
            return f"the native client was not installed ({type(exc).__name__}: {exc}); {kept}"
        # A hand-run without --tag leaves the release to the native updater,
        # which picks it as this one would (the daemon's release on a client
        # machine, the newest published on a Docker host) and checks that
        # release's archive itself. Pinning the release verified here would
        # turn the common case, a daemon ahead of this release, into a
        # downgrade refusal. A picked release with no native client for this
        # platform fails the native shim step before anything changes, so
        # the machine stays on the Python client and the run says why.
        argv = [str(binary), "update", "--clients-only"]
        if args.tag is not None:
            argv += ["--tag", args.tag]
        argv += ["--daemon-url", args.daemon_url]
        if args.allow_downgrade:
            argv.append("--allow-downgrade")
        say = out or (lambda line: print(line, flush=True))
        say(
            f"==> moving this machine to the native client: native {version} ({triple}) runs the client steps")
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            # A hand-off child's variables pass through unchanged: the native
            # child borrows the lock from the same parent and writes its
            # report. A hand-run's child is an ordinary top-level run, so no
            # hand-off variable (a malformed one included) reaches it.
            child_env = dict(env) if handed_off else {
                key: value for key, value in env.items() if not key.startswith(HANDOFF_ENV)}
            proc = popen(argv, stdin=subprocess.DEVNULL, env=child_env)
        except OSError as exc:
            return f"the native client was not installed (it did not start: {exc}); {kept}"
        code = _wait(proc)
        if code != 0:
            # The native updater does not read the setting, so this holds only
            # while the launcher is still the Python one.
            say(f"==> the native client steps ended with exit {code} (see above); while this machine still "
                f"runs the Python client, an update with {ENGINE_ENV}=python keeps it")
        elif engine == NATIVE:
            # An explicit native choice ends a recorded Python one, as the
            # native installer's does.
            with contextlib.suppress(OSError):
                engine_record().unlink()
        return code
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
