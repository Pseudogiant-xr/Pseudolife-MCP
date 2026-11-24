"""The update bridge (pseudolife_memory/update_bridge.py): since 0.26.0 an
attended client update moves to this release's native client by default;
PSEUDOLIFE_SHIM_ENGINE=python, a recorded Python install, an unattended run,
or a machine the native client cannot serve keeps Python and says why. The
native binary is faked behind the ``run`` / ``popen`` seams and the hand-off
parent's argv behind ``parent_argv``; the downloads are real HTTP against a
loopback server named by the same override the native installer uses."""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pseudolife_memory import __version__, update_bridge, update_cli

TRIPLE = update_bridge.target()
pytestmark = pytest.mark.skipif(TRIPLE is None, reason="no native target for this host")
HANDOFF = {"PSEUDOLIFE_UPDATE_HANDOFF": "4242",
           "PSEUDOLIFE_UPDATE_HANDOFF_REPORT": "/tmp/handoff/report.json",
           "PSEUDOLIFE_UPDATE_HANDOFF_RUNTIME": "/runtimes/000003"}
URL = "http://127.0.0.1:8765"
ARGS = ["--clients-only", "--tag", __version__, "--daemon-url", URL]


@pytest.fixture(autouse=True)
def _own_home(tmp_path_factory, monkeypatch):
    # The bridge reads ~/.pseudolife-mcp/client-engine and the shim layout
    # under the home: never the real ones.
    home = tmp_path_factory.mktemp("home")
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(home))
    for key in ("XDG_DATA_HOME", "LOCALAPPDATA", "PSEUDOLIFE_SHIM_RUNTIMES", "PSEUDOLIFE_SHIM_LAUNCHER"):
        monkeypatch.delenv(key, raising=False)


class Release:
    """A loopback directory of release files: path -> (status, body, headers)."""

    def __init__(self):
        self.files: dict[str, tuple[int, bytes, dict]] = {}
        self.seen: list[str] = []
        release = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                release.seen.append(self.path)
                status, body, headers = release.files.get(self.path, (404, b"missing", {}))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}/v{__version__}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, binary: bytes = b"native-binary", *, extra: bool = False, digest: str | None = None):
        name = update_bridge.archive_name(__version__, TRIPLE)
        member = update_bridge.binary_name(TRIPLE.endswith("-windows-msvc"))
        data = io.BytesIO()
        if name.endswith(".zip"):
            with zipfile.ZipFile(data, "w") as archive:
                archive.writestr(member, binary)
                if extra:
                    archive.writestr("README", b"x")
        else:
            with tarfile.open(fileobj=data, mode="w:gz") as archive:
                for item, body in [(member, binary)] + ([("README", b"x")] if extra else []):
                    info = tarfile.TarInfo(item)
                    info.size = len(body)
                    info.mode = 0o755
                    archive.addfile(info, io.BytesIO(body))
        archive_bytes = data.getvalue()
        sums = f"{digest or hashlib.sha256(archive_bytes).hexdigest()}  {name}\n"
        self.files[f"/v{__version__}/{name}"] = (200, archive_bytes, {})
        self.files[f"/v{__version__}/SHA256SUMS"] = (200, sums.encode(), {})
        return name

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def release():
    served = Release()
    yield served
    served.close()


class Native:
    """The fake native binary: probe answers and the child it starts."""

    def __init__(self, version: str = __version__, protocols: str = "client-handoff-v1", exit_code: int = 7):
        self.answers = {"--native-capability": (0, "pseudolife-mcp native-update-v1\n", ""),
                        "--client-handoff-protocols": (0, f"{version}\n{protocols}\n", ""),
                        # runtime install --check: no tunnel autostart here.
                        "install": (0, "", "")}
        self.probes: list[list[str]] = []
        self.started: list[tuple[list[str], dict, dict]] = []
        self.binary_seen: bytes | None = None
        self.exit_code = exit_code
        self.start_error: OSError | None = None

    def run(self, argv, **kwargs):
        self.probes.append(list(argv))
        self.binary_seen = Path(argv[0]).read_bytes()
        code, out, err = self.answers[argv[2]]
        return subprocess.CompletedProcess(argv, code, out, err)

    def popen(self, argv, **kwargs):
        if self.start_error:
            raise self.start_error
        # What the process environment holds at the moment the child starts.
        self.started.append((list(argv), dict(kwargs["env"]), dict(os.environ)))
        code = self.exit_code

        class Proc:
            def wait(self, timeout=None):
                return code
        return Proc()


# What a person's `pseudolife-mcp update` looks like from its hand-off child.
ATTENDED = ["/home/u/.local/share/pseudolife-mcp/runtimes/000002/bin/python", "-m", "pseudolife_memory.cli", "update"]


def bridge(env, native=None, args=ARGS, lines=None, parent=ATTENDED):
    native = native or Native()
    return update_bridge.bridge(list(args), update_cli.build_parser, env=env, run=native.run,
                                popen=native.popen, out=(lines.append if lines is not None else lambda _l: None),
                                parent_argv=lambda pid: parent)


def handed(release, **extra):
    """A hand-off child's environment that asked for the native client;
    an ``extra`` value of None removes that variable."""
    env = {**HANDOFF, update_bridge.ARCHIVE_URL_ENV: release.base,
           update_bridge.ENGINE_ENV: update_bridge.NATIVE, **extra}
    return {key: value for key, value in env.items() if value is not None}


def test_the_bridge_runs_the_native_client_steps_with_the_handoff_unchanged(release, monkeypatch):
    release.publish(b"the native binary")
    env = handed(release)
    for key, value in HANDOFF.items():
        monkeypatch.setenv(key, value)
    native = Native(exit_code=7)
    lines: list[str] = []
    assert bridge(env, native, lines=lines) == 7
    assert native.binary_seen == b"the native binary"
    assert [p[1:] for p in native.probes] == [["update", "--native-capability"],
                                              ["update", "--client-handoff-protocols"]]
    (argv, child_env, process_env), = native.started
    assert argv[1:] == ["update", "--clients-only", "--tag", __version__, "--daemon-url", URL]
    assert Path(argv[0]).is_absolute()
    for key, value in HANDOFF.items():
        assert child_env[key] == value, key
        # Not consumed before the child starts.
        assert process_env[key] == value, key
    assert "moving this machine to the native client" in lines[0]
    # A failed native run says what keeps Python while the machine is still
    # on it; the native updater reads no setting (Opus reviews).
    assert len(lines) == 2 and "an update with PSEUDOLIFE_SHIM_ENGINE=python keeps it" in lines[1]
    assert "without PSEUDOLIFE_SHIM_ENGINE=native" not in lines[1]
    # The private scratch copy is gone once the child ended.
    assert not Path(argv[0]).exists()


def test_allow_downgrade_is_passed_on(release):
    release.publish()
    native = Native()
    assert bridge(handed(release), native, args=ARGS + ["--allow-downgrade"]) == 7
    assert native.started[0][0][-1] == "--allow-downgrade"


@pytest.mark.parametrize("marker", [None, "", "abc", "-1"])
def test_a_run_that_was_not_handed_off_is_untouched(release, marker):
    release.publish()
    env = {update_bridge.ARCHIVE_URL_ENV: release.base}
    if marker is not None:
        env["PSEUDOLIFE_UPDATE_HANDOFF"] = marker
    native = Native()
    assert bridge(env, native) is None
    assert release.seen == [] and native.probes == [] and native.started == []


@pytest.mark.parametrize("args", [
    ["--tag", __version__, "--daemon-url", URL],                     # not --clients-only
    ARGS + ["--reinstall"],
    ARGS + ["--json"],
    ARGS + ["--result-file", "r"],
    ["--clients-only", "--daemon-url", URL],                         # no --tag
    ARGS + ["--no-such-option"],
])
def test_a_handoff_with_other_options_is_not_bridged(release, args):
    release.publish()
    native = Native()
    assert bridge(handed(release), native, args=args) is None
    assert release.seen == [] and native.started == []


def test_the_python_engine_choice_is_honoured(release):
    release.publish()
    native = Native()
    note = bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE="python"), native)
    assert note.startswith("PSEUDOLIFE_SHIM_ENGINE=python keeps the Python client")
    assert release.seen == [] and native.started == []


def kept(note):
    assert isinstance(note, str), note
    assert note.startswith("the native client was not installed (")
    assert note.endswith("the Python client was kept and this run's own code finishes the client steps")
    return note


def test_no_release_assets_keeps_python(release):
    native = Native()
    note = kept(bridge(handed(release), native))
    assert f"no native client is published in release v{__version__}" in note
    # The override that chose the source is named (as install.rs override_note).
    assert f"from {update_bridge.ARCHIVE_URL_ENV}={release.base}, unset it" in note
    assert native.started == []


def test_no_archive_for_this_platform_keeps_python(release):
    release.publish()
    release.files[f"/v{__version__}/SHA256SUMS"] = (200, f"{'0' * 64}  pseudolife-mcp-x-other.tar.gz\n".encode(), {})
    native = Native()
    note = kept(bridge(handed(release), native))
    assert f"no native client is published for {TRIPLE} in release v{__version__}" in note
    assert native.probes == []


def test_a_digest_mismatch_never_executes_the_binary(release):
    release.publish(digest="f" * 64)
    native = Native()
    note = kept(bridge(handed(release), native))
    assert "does not match its SHA256SUMS digest" in note
    assert native.probes == [] and native.started == []


def test_an_archive_with_anything_but_the_binary_keeps_python(release):
    release.publish(extra=True)
    native = Native()
    assert "something other than the one native binary" in kept(bridge(handed(release), native))
    assert native.probes == []


@pytest.mark.parametrize("failure", ["exec", "glibc", "foreign", "version", "protocol"])
def test_a_binary_that_does_not_run_here_keeps_python(release, failure):
    release.publish()
    native = Native()
    if failure == "exec":
        def run(argv, **kwargs):
            raise OSError(8, "Exec format error")
        native.run = run
    elif failure == "glibc":
        native.answers["--native-capability"] = (1, "", "/lib64/libc.so.6: version `GLIBC_2.39' not found\n")
    elif failure == "foreign":
        native.answers["--native-capability"] = (0, "something else\n", "")
    elif failure == "version":
        native.answers["--client-handoff-protocols"] = (0, "0.0.1\nclient-handoff-v1\n", "")
    else:
        native.answers["--client-handoff-protocols"] = (0, f"{__version__}\n\n", "")
    note = kept(bridge(handed(release), native))
    assert native.started == []
    if failure == "glibc":
        assert "GLIBC_2.39" in note and "does not run on this machine" in note


def test_a_child_that_cannot_start_keeps_python(release):
    release.publish()
    native = Native()
    native.start_error = OSError(13, "Permission denied")
    assert "it did not start" in kept(bridge(handed(release), native))


def test_another_release_tag_keeps_python(release):
    release.publish()
    native = Native()
    args = ["--clients-only", "--tag", "0.0.1", "--daemon-url", URL]
    assert "not 0.0.1" in kept(bridge(handed(release), native, args=args))
    assert release.seen == []


def test_downloads_are_https_and_redirects_stay_on_github_hosts(release):
    with pytest.raises(update_bridge.Kept, match="non-HTTPS"):
        update_bridge.fetch(f"{release.base}/SHA256SUMS", 1024, overridden=False)
    # An override may redirect only within its own host.
    release.files[f"/v{__version__}/SHA256SUMS"] = (302, b"", {"Location": "http://example.com/SHA256SUMS"})
    with pytest.raises(update_bridge.Kept, match="refused a redirect to http://example.com"):
        update_bridge.fetch(f"{release.base}/SHA256SUMS", 1024, overridden=True)
    handler = update_bridge._Redirects(None)
    for allowed in ("https://objects.githubusercontent.com/x", "https://release-assets.githubusercontent.com/x"):
        assert handler.redirect_request(_Request(), None, 302, "Found", {}, allowed) is not None
    for refused in ("http://objects.githubusercontent.com/x", "https://example.com/x"):
        with pytest.raises(Exception, match="refused a redirect"):
            handler.redirect_request(_Request(), None, 302, "Found", {}, refused)


class _Request:
    """Enough of urllib's Request for HTTPRedirectHandler.redirect_request."""
    def __init__(self):
        import urllib.request
        self._r = urllib.request.Request("https://github.com/x")

    def __getattr__(self, name):
        return getattr(self._r, name)


def test_downloads_are_size_capped(release):
    release.files["/big"] = (200, b"x" * 2048, {})
    base = release.base.rsplit("/", 1)[0]
    with pytest.raises(update_bridge.Kept, match="larger than"):
        update_bridge.fetch(f"{base}/big", 1024, overridden=True)


@pytest.mark.parametrize("value", ["ftp://example.com/x", "file:///tmp/x", "https://u:p@example.com/x",
                                   "https://example.com/x?y=1"])
def test_a_bad_override_keeps_python(value):
    native = Native()
    note = kept(bridge({**HANDOFF, update_bridge.ARCHIVE_URL_ENV: value,
                        update_bridge.ENGINE_ENV: update_bridge.NATIVE}, native))
    assert update_bridge.ARCHIVE_URL_ENV in note


def test_the_archive_name_matches_the_native_installer():
    # rust/shim/src/runtime/install.rs archive_name and target_triple.
    assert update_bridge.archive_name("1.2.3", "x86_64-pc-windows-msvc") == \
        "pseudolife-mcp-1.2.3-x86_64-pc-windows-msvc.zip"
    assert update_bridge.archive_name("1.2.3", "aarch64-apple-darwin") == \
        "pseudolife-mcp-1.2.3-aarch64-apple-darwin.tar.gz"
    assert update_bridge.target("Windows", "AMD64") == "x86_64-pc-windows-msvc"
    assert update_bridge.target("Linux", "x86_64") == "x86_64-unknown-linux-gnu"
    assert update_bridge.target("Linux", "aarch64") == "aarch64-unknown-linux-gnu"
    assert update_bridge.target("Darwin", "arm64") == "aarch64-apple-darwin"
    assert update_bridge.target("Linux", "riscv64") is None
    # Windows reports no machine without PROCESSOR_ARCHITECTURE (found by the
    # end-to-end proof's scrubbed environment); a 64-bit interpreter decides.
    expected = "x86_64-pc-windows-msvc" if sys.maxsize > 2 ** 32 else None
    assert update_bridge.target("Windows", "") == expected
    assert update_bridge.target("Linux", "") is None
    source = (Path(__file__).resolve().parents[1] / "rust/shim/src/runtime/install.rs").read_text(encoding="utf-8")
    for triple in ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-apple-darwin",
                   "aarch64-apple-darwin", "x86_64-pc-windows-msvc"):
        assert f'"{triple}"' in source, triple
    assert '"pseudolife-mcp-{version}-{}.{}"' in source


def test_main_bridges_before_it_removes_the_handoff_variables(monkeypatch):
    for key, value in HANDOFF.items():
        monkeypatch.setenv(key, value)
    seen = {}

    def fake(arguments, parser_factory, **_kw):
        seen["env"] = {key: os.environ.get(key) for key in HANDOFF}
        seen["arguments"] = arguments
        return 9

    monkeypatch.setattr(update_bridge, "bridge", fake)
    assert update_cli.main(list(ARGS)) == 9
    assert seen["env"] == HANDOFF
    assert seen["arguments"] == ARGS
    # The native child's exit code is the run's: Python ran no client step.
    assert os.environ.get("PSEUDOLIFE_UPDATE_HANDOFF") == "4242"


def test_main_records_why_python_was_kept(monkeypatch):
    monkeypatch.setattr(update_bridge, "bridge", lambda *a, **k: "the native client was not installed (x); kept")
    recorded = []

    class Stub:
        def __init__(self, options):
            self.options = options

        def step(self, text):
            recorded.append(text)

        def run(self):
            return 0

    monkeypatch.setattr(update_cli, "Update", Stub)
    for key in HANDOFF:
        monkeypatch.delenv(key, raising=False)
    assert update_cli.main(list(ARGS)) == 0
    assert recorded == ["the native client was not installed (x); kept"]


def test_a_truncated_download_keeps_python(release):
    # Codex review: http.client.IncompleteRead escaped fetch().
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nshort")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}/v{__version__}"
    native = Native()
    note = kept(bridge({**HANDOFF, update_bridge.ARCHIVE_URL_ENV: base,
                        update_bridge.ENGINE_ENV: update_bridge.NATIVE}, native))
    assert "SHA256SUMS" in note
    assert native.started == []
    listener.close()


@pytest.mark.parametrize("stage", ["mkdtemp", "write"])
def test_a_staging_failure_keeps_python(release, monkeypatch, stage):
    # Codex review: an OSError while staging the binary aborted the run.
    release.publish()
    if stage == "mkdtemp":
        def mkdtemp(*a, **k):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(update_bridge.tempfile, "mkdtemp", mkdtemp)
    else:
        def write_bytes(self, data):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(update_bridge.Path, "write_bytes", write_bytes)
    native = Native()
    assert "No space left on device" in kept(bridge(handed(release), native))
    assert native.started == []


def test_a_successful_native_run_says_nothing_more(release):
    release.publish()
    lines: list[str] = []
    assert bridge(handed(release), Native(exit_code=0), lines=lines) == 0
    assert len(lines) == 1


@pytest.mark.parametrize("sums, why", [
    ("{digest}  {name}\nnot a record\n", "invalid record"),
    ("{digest} {name}\n", "invalid record"),
    ("{digest}  dir/{name}\n", "invalid record"),
    ("{upper}  {name}\n", "invalid record"),
    ("{digest}  {name}\n{digest}  {name}\n", "lists the archive twice"),
])
def test_the_checksum_file_follows_the_native_installers_rules(release, sums, why):
    name = release.publish()
    _, archive, _ = release.files[f"/v{__version__}/{name}"]
    digest = hashlib.sha256(archive).hexdigest()
    body = sums.format(digest=digest, upper=digest.upper(), name=name)
    release.files[f"/v{__version__}/SHA256SUMS"] = (200, body.encode(), {})
    native = Native()
    assert why in kept(bridge(handed(release), native))
    assert native.probes == []


def test_an_override_redirect_never_leaves_http_or_drops_to_http():
    handler = update_bridge._Redirects("mirror.example.com")
    import urllib.request
    https = urllib.request.Request("https://mirror.example.com/v1/SHA256SUMS")
    for refused in ("http://mirror.example.com/x", "ftp://mirror.example.com/x", "https://other.example.com/x"):
        with pytest.raises(Exception, match="refused a redirect"):
            handler.redirect_request(https, None, 302, "Found", {}, refused)
    assert handler.redirect_request(https, None, 302, "Found", {}, "https://mirror.example.com/y") is not None
    http = urllib.request.Request("http://mirror.example.com/v1/SHA256SUMS")
    assert handler.redirect_request(http, None, 302, "Found", {}, "https://mirror.example.com/y") is not None


def test_only_an_ascii_pid_and_the_exact_engine_value_count(release, monkeypatch):
    monkeypatch.setattr(update_bridge, "_python_shim_runtime", lambda: True)
    release.publish()
    native = Native()
    # Not a hand-off: a hand-run, whose child sees no hand-off variable at
    # all, and which moves by default as when asked for.
    for engine in (None, update_bridge.NATIVE):
        native = Native()
        assert bridge({**handed(release, PSEUDOLIFE_SHIM_ENGINE=engine),
                       "PSEUDOLIFE_UPDATE_HANDOFF": "\u0664\u0662"}, native) == 7
        (_, child_env, _), = native.started
        assert not any(key.startswith("PSEUDOLIFE_UPDATE_HANDOFF") for key in child_env)
    # Only the exact "python" keeps Python (runtimes.py honours exactly
    # that); any other value is no choice and takes the default, the move.
    for value in ("Python", "PYTHON", " python", "python ", "Native", ""):
        native = Native()
        assert bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=value), native) == 7, value
        assert len(native.started) == 1
    assert bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE="python"), Native()).startswith(
        "PSEUDOLIFE_SHIM_ENGINE=python keeps the Python client")


def test_by_default_an_attended_handoff_moves_to_the_native_client(release):
    # 0.26.0: a person's update moves the machine unless told otherwise; the
    # default first asks the staged binary about a tunnel autostart.
    release.publish(b"the native binary")
    native = Native()
    lines: list[str] = []
    assert bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=None), native, lines=lines) == 7
    assert [p[1:] for p in native.probes] == [["update", "--native-capability"],
                                              ["update", "--client-handoff-protocols"],
                                              ["runtime", "install", "--check"]]
    (argv, _, _), = native.started
    assert argv[1:] == ["update", "--clients-only", "--tag", __version__, "--daemon-url", URL]
    assert "moving this machine to the native client" in lines[0]


def test_asked_for_the_native_client_skips_the_tunnel_check(release):
    # The native updater refuses a tunnel autostart itself, naming the fix.
    release.publish()
    native = Native()
    native.answers["install"] = (3, "", "this machine stays on the Python client: a tunnel is registered\n")
    assert bridge(handed(release), native) == 7
    assert ["runtime", "install", "--check"] not in [p[1:] for p in native.probes]


def test_a_platform_with_no_native_build_keeps_python_and_says_so(release, monkeypatch):
    release.publish()
    monkeypatch.setattr(update_bridge, "target", lambda *a: None)
    for engine in (None, update_bridge.NATIVE):
        native = Native()
        assert "no native client is built for" in kept(bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=engine), native))
        assert native.started == []
    assert release.seen == []


UNATTENDED_NOTE = ("this update ran unattended, so this machine keeps the Python client, and this run's own code "
                   "finishes the client steps; to move it to the native client, run "
                   "`pseudolife-mcp update --clients-only`")


@pytest.mark.parametrize("parent", [
    ["/x/pseudolife-mcp", "update", "--unattended"],                                 # the scheduled run
    ["/x/pseudolife-mcp.exe", "update", "--unattended", "--daemon-url", URL],
    ["/x/python", "-m", "pseudolife_memory.cli", "update", "--clients-only", "--tag", "9.9.9",
     "--result-file", "/s/update-clients.9.9.9.result"],                             # the shim's session start
    ["/x/python", "update", "--clients-only", "--result-file=/s/r"],
    None,                                                                            # cannot be read
    [],
], ids=["scheduled", "scheduled-windows", "session-start", "result-file-equals", "unreadable", "empty"])
@pytest.mark.parametrize("engine", [None, "native"])
def test_an_unattended_update_never_changes_the_client(release, parent, engine):
    release.publish()
    native = Native()
    assert bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=engine), native, parent=parent) == UNATTENDED_NOTE
    # Nothing downloaded, nothing run.
    assert release.seen == [] and native.probes == [] and native.started == []


@pytest.mark.parametrize("parent", [
    ATTENDED,
    ["/opt/--unattended/bin/pseudolife-mcp", "update"],          # a path, not the flag
    ["--unattended", "update"],                                  # argv[0] is the program
    ["/x/pseudolife-mcp", "update", "--unattended-x"],
    ["/x/pseudolife-mcp", "update", "--result-files"],
], ids=["person", "path-holds-the-word", "program-name", "longer-flag", "other-flag"])
def test_an_attended_update_moves_even_beside_near_miss_words(release, parent):
    release.publish()
    native = Native()
    assert bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=None), native, parent=parent) == 7


def test_the_parent_is_read_by_the_handoff_pid_and_a_hand_run_reads_none(release, monkeypatch):
    monkeypatch.setattr(update_bridge, "_python_shim_runtime", lambda: True)
    release.publish()
    asked: list[int] = []

    def parent(pid):
        asked.append(pid)
        return ATTENDED

    native = Native()
    update_bridge.bridge(list(ARGS), update_cli.build_parser, env=handed(release, PSEUDOLIFE_SHIM_ENGINE=None),
                         run=native.run, popen=native.popen, out=lambda _l: None, parent_argv=parent)
    assert asked == [4242]
    asked.clear()
    env = {update_bridge.ARCHIVE_URL_ENV: release.base}
    update_bridge.bridge(["--clients-only"], update_cli.build_parser, env=env, run=Native().run,
                         popen=Native().popen, out=lambda _l: None, parent_argv=parent)
    assert asked == []


def test_this_process_argv_is_read_in_place():
    if not (sys.platform.startswith("linux") or sys.platform == "darwin" or os.name == "nt"):
        pytest.skip("no reader for this platform")
    argv = update_bridge.process_argv(os.getpid())
    assert argv, argv
    # The interpreter's own arguments are there, word for word.
    assert all(word in argv for word in sys.argv[1:]), (argv, sys.argv)
    assert update_bridge.ran_unattended(str(os.getpid())) is False


def test_an_argv_that_cannot_be_read_counts_as_unattended():
    assert update_bridge.ran_unattended("4242", lambda pid: None) is True
    if sys.platform.startswith("linux") or os.name == "nt":
        assert update_bridge.process_argv(0) is None


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert update_bridge.engine_record() == tmp_path / ".pseudolife-mcp" / "client-engine"
    return tmp_path


def _record(home, text):
    record = home / ".pseudolife-mcp" / "client-engine"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(text, encoding="utf-8")
    return record


def test_a_recorded_python_install_keeps_python_by_default(release, home):
    record = _record(home, "python\n")
    release.publish()
    native = Native()
    note = bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=None), native)
    assert note == (
        f"{record} records an earlier PSEUDOLIFE_SHIM_ENGINE=python install, so this machine keeps the Python "
        f"client, and this run's own code finishes the client steps; to move it to the native client, run "
        f"`PSEUDOLIFE_SHIM_ENGINE=native pseudolife-mcp update --clients-only` (in PowerShell: "
        f"`$env:PSEUDOLIFE_SHIM_ENGINE='native'; pseudolife-mcp update --clients-only`)")
    assert release.seen == [] and native.started == []
    assert record.exists()


@pytest.mark.parametrize("code, removed", [(0, True), (7, False)])
def test_asked_for_native_moves_over_the_record_and_ends_it_once_moved(release, home, code, removed):
    record = _record(home, "python\n")
    release.publish()
    assert bridge(handed(release), Native(exit_code=code)) == code
    assert record.exists() is not removed


@pytest.mark.parametrize("text", ["native\n", "", "Python\n", "python python\n"])
def test_a_record_that_does_not_say_exactly_python_is_no_choice(release, home, text):
    _record(home, text)
    release.publish()
    assert bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=None), Native()) == 7


def test_by_default_a_tunnel_autostart_keeps_python_and_prints_the_guards_line(release):
    release.publish()
    native = Native()
    guard = ("this machine stays on the Python client: a tunnel is registered to start by itself. "
             "Nothing was changed; to stay on it, rerun the installer with PSEUDOLIFE_SHIM_ENGINE=python")
    native.answers["install"] = (3, "", guard + "\n")
    lines: list[str] = []
    note = kept(bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=None), native, lines=lines))
    assert lines == [guard]
    assert ("a tunnel the Python client registered to start by itself keeps it; the line above says how to "
            "remove that autostart, then run `PSEUDOLIFE_SHIM_ENGINE=native pseudolife-mcp update --clients-only`") in note
    assert native.started == []


def test_a_hand_run_moves_only_from_a_python_shim_runtime(release, monkeypatch):
    # A pip install, a checkout venv, or a machine whose launcher is native
    # already (reached only through --python-runtime) stays as it is.
    release.publish()
    env = {update_bridge.ARCHIVE_URL_ENV: release.base, update_bridge.ENGINE_ENV: update_bridge.NATIVE}
    monkeypatch.setattr(update_bridge, "_python_shim_runtime", lambda: False)
    native = Native()
    assert bridge(env, native, args=["--clients-only"]) is None
    assert release.seen == [] and native.started == []
    monkeypatch.setattr(update_bridge, "_python_shim_runtime", lambda: True)
    assert bridge(env, native, args=["--clients-only"]) == 7


def test_the_setting_never_doubles_help_or_a_usage_error(monkeypatch, capsys):
    monkeypatch.setenv("PSEUDOLIFE_SHIM_ENGINE", "native")
    monkeypatch.setattr(update_bridge, "_python_shim_runtime", lambda: True)
    for argv, stream in ((["--help"], "out"), (["--tag"], "err")):
        with pytest.raises(SystemExit):
            update_cli.main(list(argv))
        captured = capsys.readouterr()
        text = captured.out if stream == "out" else captured.err
        assert text.count("usage:") == 1, text


@pytest.mark.parametrize("args", [
    ["--clients-only"],
    ["--clients-only", "--daemon-url", URL],
    ARGS,
    ARGS + ["--allow-downgrade"],
])
@pytest.mark.parametrize("engine", [None, "native"])
def test_a_hand_run_clients_update_moves_by_default_and_when_asked_for(release, args, engine, monkeypatch):
    # A machine that kept Python at this release moves without waiting for
    # the next one: its own `update --clients-only`.
    monkeypatch.delenv("PSEUDOLIFE_MCP_DAEMON_URL", raising=False)
    monkeypatch.setattr(update_bridge, "_python_shim_runtime", lambda: True)
    release.publish(b"the native binary")
    env = {update_bridge.ARCHIVE_URL_ENV: release.base}
    if engine:
        env[update_bridge.ENGINE_ENV] = engine
    native = Native(exit_code=0)
    lines: list[str] = []
    assert bridge(env, native, args=args, lines=lines) == 0
    (argv, child_env, _), = native.started
    url = URL if "--daemon-url" in args else update_cli.build_parser().parse_args([]).daemon_url
    # No --tag given: the native updater picks the release as this one would
    # (the daemon's, or the newest published), never this runtime's own,
    # which a moved daemon would refuse as a downgrade (Opus review).
    want = ["update", "--clients-only"] + (["--tag", __version__] if "--tag" in args else []) + ["--daemon-url", url]
    assert argv[1:] == want + (["--allow-downgrade"] if "--allow-downgrade" in args else [])
    # An ordinary top-level native run: no hand-off variables, so it takes
    # its own update lock.
    assert not any(key in child_env for key in HANDOFF)
    assert "moving this machine to the native client" in lines[0]


@pytest.mark.parametrize("args, engine", [
    (["--clients-only"], "python"),                                   # asked for Python
    (["--tag", __version__, "--daemon-url", URL], "native"),          # a full update
    (["--tag", __version__, "--daemon-url", URL], None),
    (["--clients-only", "--reinstall"], "native"),
    (["--clients-only", "--json"], "native"),
    (["--clients-only", "--tag", "0.0.1"], "native"),                 # another release
    (["--unattended"], "native"),
    (["--clients-only", "--unattended"], "native"),
    (["--check"], "native"),
    (["--daemon-only"], "native"),
    (["--clients-only", "--result-file", "r"], "native"),
    (["--clients-only", "--result-file", "r"], None),                 # the shim's own unattended run
    (["--unattended"], None),
])
def test_other_hand_runs_stay_python_and_say_nothing(release, args, engine, monkeypatch):
    monkeypatch.setattr(update_bridge, "_python_shim_runtime", lambda: True)
    release.publish()
    env = {update_bridge.ARCHIVE_URL_ENV: release.base}
    if engine is not None:
        env[update_bridge.ENGINE_ENV] = engine
    native = Native()
    assert bridge(env, native, args=args) is None
    assert release.seen == [] and native.probes == [] and native.started == []


def test_interrupt_stops_the_native_child_even_on_a_second_interrupt():
    calls = []

    class Proc:
        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            self.waits += 1
            if self.waits <= 2:
                raise KeyboardInterrupt
            return -9

        def kill(self):
            calls.append(("kill", None))

    with pytest.raises(KeyboardInterrupt):
        update_bridge._wait(Proc())
    assert calls == [("wait", None), ("wait", update_bridge.INTERRUPT_GRACE), ("kill", None), ("wait", None)]
    # Shorter than the Python parent's own 10 s wait for this process.
    assert update_bridge.INTERRUPT_GRACE < 10


# ---- which machine a hand-run moves: real scratch layouts, both shapes ----

def _shim_layout(tmp_path, shape):
    from pseudolife_memory import runtimes
    launcher = tmp_path / "bin" / ("pseudolife-mcp.exe" if shape == "windows" else "pseudolife-mcp")
    launcher.parent.mkdir(parents=True)
    root = tmp_path / "runtimes"
    (root / "000002").mkdir(parents=True)
    return runtimes.Layout(root, launcher)


def _python_launcher(layout, shape):
    from pseudolife_memory import runtimes
    if shape == "windows":
        # A distlib launcher: an MZ stub with a stored zip appended whose
        # __main__.py is the selector.
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as made:
            made.writestr("__main__.py", runtimes.selector_content(layout))
        layout.launcher.write_bytes(b"MZ" + b"\0" * 512 + archive.getvalue())
    else:
        layout.launcher.write_text(runtimes.launcher_content(layout), encoding="utf-8")


@pytest.mark.parametrize("shape", ["posix", "windows"])
def test_a_hand_run_moves_only_a_shim_runtime_behind_the_python_launcher(tmp_path, shape):
    from pseudolife_memory import runtimes
    layout = _shim_layout(tmp_path, shape)
    inside = str(layout.root / "000002")
    _python_launcher(layout, shape)
    assert runtimes._launcher_kind(layout) == "python"
    assert update_bridge._python_shim_runtime(layout, inside) is True
    # Near misses on the interpreter: a pip install and an editable
    # checkout's venv, both outside the runtimes root.
    assert update_bridge._python_shim_runtime(layout, str(tmp_path / "pip-site")) is False
    assert update_bridge._python_shim_runtime(layout, str(tmp_path / "checkout" / ".venv")) is False


@pytest.mark.parametrize("shape, native", [
    ("posix", b"\x7fELF\x02\x01\x01" + b"\0" * 64),
    ("posix", b"\xcf\xfa\xed\xfe" + b"\0" * 64),            # Mach-O
    ("windows", b"MZ\x90\0" + b"\0" * 1024),                 # a native PE reads as unknown
], ids=["elf", "mach-o", "pe"])
def test_a_native_launcher_is_left_alone_on_each_platform(tmp_path, shape, native):
    from pseudolife_memory import runtimes
    layout = _shim_layout(tmp_path, shape)
    layout.launcher.write_bytes(native)
    assert runtimes._launcher_kind(layout) in ("native", "unknown")
    assert update_bridge._python_shim_runtime(layout, str(layout.root / "000002")) is False


@pytest.mark.parametrize("shape", ["posix", "windows"])
def test_a_missing_or_unreadable_launcher_is_left_alone(tmp_path, shape):
    from pseudolife_memory import runtimes
    layout = _shim_layout(tmp_path, shape)
    inside = str(layout.root / "000002")
    assert runtimes._launcher_kind(layout) == "missing"
    assert update_bridge._python_shim_runtime(layout, inside) is False
    layout.launcher.mkdir()                                  # not a regular file
    assert runtimes._launcher_kind(layout) == "unreadable"
    assert update_bridge._python_shim_runtime(layout, inside) is False
