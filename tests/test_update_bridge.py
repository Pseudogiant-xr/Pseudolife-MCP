"""The update bridge (pseudolife_memory/update_bridge.py): a client update
that asks for it (PSEUDOLIFE_SHIM_ENGINE=native) moves to this release's
native client; otherwise, or when it cannot, it keeps Python and says why. The native binary is faked behind the ``run`` / ``popen`` seams;
the downloads are real HTTP against a loopback server named by the same
override the native installer uses."""
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
                        "--client-handoff-protocols": (0, f"{version}\n{protocols}\n", "")}
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


def bridge(env, native=None, args=ARGS, lines=None):
    native = native or Native()
    return update_bridge.bridge(list(args), update_cli.build_parser, env=env, run=native.run,
                                popen=native.popen, out=(lines.append if lines is not None else lambda _l: None))


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
    assert len(lines) == 2 and "an update without PSEUDOLIFE_SHIM_ENGINE=native keeps it" in lines[1]
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
    # Not a hand-off, and not asked for: untouched.
    assert bridge({**handed(release, PSEUDOLIFE_SHIM_ENGINE=None),
                   "PSEUDOLIFE_UPDATE_HANDOFF": "\u0664\u0662"}, native) is None
    # Not a hand-off but asked for: a hand-run, whose child sees no
    # hand-off variable at all.
    assert bridge({**handed(release), "PSEUDOLIFE_UPDATE_HANDOFF": "\u0664\u0662"}, native) == 7
    (_, child_env, _), = native.started
    assert not any(key.startswith("PSEUDOLIFE_UPDATE_HANDOFF") for key in child_env)
    native = Native()
    before = len(release.seen)
    # Only the exact "native" asks for the move; "Python" is no choice of
    # Python either (runtimes.py honours exactly "python"), so it keeps the
    # default, which is Python.
    for value in ("Python", "Native", "NATIVE", " native", "native ", ""):
        note = bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=value), native)
        assert note.startswith("the native client is opt-in in"), (value, note)
    assert release.seen[before:] == [] and native.started == []
    assert bridge(handed(release), Native()) == 7


def test_by_default_a_handoff_keeps_python_and_says_how_to_opt_in(release):
    # 0.24.0: the move to the native client happens only when asked for, so
    # nothing is downloaded and the Python child finishes the client steps.
    release.publish()
    native = Native()
    note = bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=None), native)
    # The command is given as typed, in both shells: "set X and run" is no
    # command an agent can run literally (Opus review).
    assert note == (
        f"the native client is opt-in in {__version__}: this machine keeps the Python client, and this "
        f"run's own code finishes the client steps; to move it to the native client, run "
        f"`PSEUDOLIFE_SHIM_ENGINE=native pseudolife-mcp update --clients-only` (in PowerShell: "
        f"`$env:PSEUDOLIFE_SHIM_ENGINE='native'; pseudolife-mcp update --clients-only`)")
    assert release.seen == [] and native.probes == [] and native.started == []


def test_a_platform_with_no_native_build_is_never_told_to_opt_in(release, monkeypatch):
    release.publish()
    monkeypatch.setattr(update_bridge, "target", lambda *a: None)
    native = Native()
    assert bridge(handed(release, PSEUDOLIFE_SHIM_ENGINE=None), native) is None
    # Near miss: asked for, it says why it kept Python.
    assert "no native client is built for" in bridge(handed(release), native)
    assert release.seen == [] and native.started == []


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
def test_a_hand_run_clients_update_moves_when_the_native_client_is_asked_for(release, args, monkeypatch):
    # A machine that kept Python at this release moves without waiting for
    # the next one: its own `update --clients-only`, with the opt-in set.
    monkeypatch.delenv("PSEUDOLIFE_MCP_DAEMON_URL", raising=False)
    monkeypatch.setattr(update_bridge, "_python_shim_runtime", lambda: True)
    release.publish(b"the native binary")
    env = {update_bridge.ARCHIVE_URL_ENV: release.base, update_bridge.ENGINE_ENV: update_bridge.NATIVE}
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
    (["--clients-only"], None),                                       # not asked for
    (["--clients-only"], "Native"),
    (["--tag", __version__, "--daemon-url", URL], "native"),          # a full update
    (["--clients-only", "--reinstall"], "native"),
    (["--clients-only", "--json"], "native"),
    (["--clients-only", "--tag", "0.0.1"], "native"),                 # another release
    (["--unattended"], "native"),
    (["--clients-only", "--unattended"], "native"),
    (["--check"], "native"),
    (["--daemon-only"], "native"),
    (["--clients-only", "--result-file", "r"], "native"),
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
