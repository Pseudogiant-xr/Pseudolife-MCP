"""CLI-MOVE-REFUSALS: the refusals ``pseudolife-mcp move`` makes before any effect.

Answered: ``--help`` / ``-h`` alone and a missing ``--to`` (argparse, at
COLUMNS=80); an invalid ``--target-url``; an empty ``--to`` or one holding
whitespace or a control character; a ``--target-checkout`` holding CR or LF.
Everything else defers, and every argv that passes the checks defers before
any effect (the spec is ``move.md`` beside this file).

Safety. The oracle's move reads the hardcoded ``http://127.0.0.1:8765``
(the developer's live daemon) and, with ``--yes``, starts a real move, so
the oracle runs ONLY argv that refuse before any effect. Each such case
runs with ``PATH`` set to an empty directory (no docker or ssh) and every
``*_proxy`` variable pointing at a recording listener, which a urllib GET
to 127.0.0.1 would reach instead of the live daemon (the positive control
``_guard_selftest`` proves that once per process); the recorded requests
are compared, and must be empty. A case whose argv passes validation
(``ORACLE_NOT_RUN``) never runs the oracle: its Python arm runs a stub
``pseudolife_memory.cli`` placed first on ``PYTHONPATH``, verified before
use, and the expectation is the Rust deferral with no request.
"""

from __future__ import annotations

import atexit
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import normalize, producers
from ..core import WINDOWS, Case
from ..mutants import Mutant

DEFERRAL = "pseudolife-stdio: mode 'move' is deferred in this candidate\n"
STUB_MARKER = "move row: the oracle was not run for this argv\n"
BAD_TO = "move: --to takes an ssh target such as root@box or a ~/.ssh/config alias\n"
FILE = "shim/src/cli/move_cli.rs"


def _native(text: str) -> bytes:
    return (text.replace("\n", "\r\n") if WINDOWS else text).encode("utf-8")


# --- the recording listener ---------------------------------------------------

class _Recorder:
    """An HTTP listener that records every request and answers 502. It is
    every arm's proxy, so any urllib request the oracle makes lands here."""

    def __init__(self) -> None:
        seen: list[dict] = []
        self._seen = seen

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                seen.append({"method": self.command, "target": self.path,
                             "headers": [[k, v] for k, v in self.headers.items()],
                             "body": base64.b64encode(body).decode()})
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_CONNECT = _record

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def requests(self) -> list[dict]:
        return list(self._seen)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


_PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")

_CHECKED: dict[str, bool] = {}


def _guard_selftest() -> None:
    """Positive control, once per process: the oracle's own HTTP reader
    (``move_cli.Runner.get_json``, what reads ``SOURCE_URL``) under the
    cases' proxy variables reaches the recorder for a loopback URL. The URL
    is a closed port, never 8765."""
    if _CHECKED.get("guard"):
        return
    recorder = _Recorder()
    try:
        env = {k: os.environ[k] for k in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if k in os.environ}
        env.update({key: recorder.url for key in _PROXY_KEYS})
        env.update({"PYTHONPATH": str(producers._SOURCE), "PATH": tempfile.gettempdir()})
        code = ("from pseudolife_memory.move_cli import Runner; "
                "Runner().get_json('http://127.0.0.1:9/health', timeout=5.0)")
        subprocess.run([sys.executable, "-P", "-c", code], env=env, check=True, timeout=60,
                       capture_output=True)
        targets = [r["target"] for r in recorder.requests()]
    finally:
        recorder.close()
    if targets != ["http://127.0.0.1:9/health"]:
        raise RuntimeError(f"move row: the proxy guard did not capture the oracle's GET ({targets})")
    _CHECKED["guard"] = True


# --- the stub oracle for argv the real one must never run ----------------------

def _stub_dir() -> str:
    """A ``pseudolife_memory`` package whose ``cli`` prints a marker and
    exits 0, checked before first use: if PYTHONPATH did not put it ahead of
    every installed copy, the row refuses to run."""
    if "stub" in _CHECKED:
        return _CHECKED["stub"]  # type: ignore[return-value]
    root = Path(tempfile.mkdtemp(prefix="pl-move-stub-"))
    atexit.register(shutil.rmtree, root, True)
    package = root / "pseudolife_memory"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "import sys\nsys.stderr.write(%r)\nraise SystemExit(0)\n" % STUB_MARKER, encoding="utf-8")
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if k in os.environ}
    env.update({"PYTHONPATH": str(root), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    probe = subprocess.run([sys.executable, "-P", "-m", "pseudolife_memory.cli", "version"],
                           env=env, capture_output=True, timeout=60)
    if probe.returncode != 0 or probe.stdout or probe.stderr.decode("utf-8") != _native(STUB_MARKER).decode():
        raise RuntimeError(f"move row: the stub oracle did not shadow the real one: {probe!r}")
    _CHECKED["stub"] = str(root)  # type: ignore[assignment]
    return str(root)


# --- named rules -----------------------------------------------------------------

@normalize.rule("move-oracle-not-run")
def oracle_not_run(obs: dict) -> None:
    """A valid argv: the oracle is not run (it would contact the live
    daemon). The Python arm must show it ran the stub and nothing else (exit
    0, the marker, no request, no file); it becomes the expectation that
    the candidate prints the dispatcher's deferral and exits 1."""
    if obs.get("arm") != "python":
        return
    if (obs["exit"], base64.b64decode(obs["stdout"]), base64.b64decode(obs["stderr"])) != (
            0, b"", _native(STUB_MARKER)):
        return  # left as observed: the comparison then fails
    obs["exit"] = 1
    obs["stderr"] = base64.b64encode(_native(DEFERRAL)).decode()


@normalize.rule("move-declared-deferral")
def declared_deferral(obs: dict) -> None:
    """The oracle answers (a refusal or argparse's help or usage, all before
    any effect) but the candidate defers by design: the oracle's exit must
    be 0 or 2 and it must have made no request; the expectation becomes the
    deferral line, exit 1, the home unchanged."""
    if obs.get("arm") != "python" or obs["exit"] not in (0, 2) or obs.get("requests"):
        return
    obs["exit"] = 1
    obs["stdout"] = ""
    obs["stderr"] = base64.b64encode(_native(DEFERRAL)).decode()


# --- cases -----------------------------------------------------------------------

def _case(case_id: str, argv: list[str], *, rules: tuple[str, ...] = (), columns: str = "80",
          oracle: bool = True, platforms: tuple[str, ...] = ("windows", "linux")) -> Case:
    env: dict[str, str | None] = {"PATH": "{CWD}", "COLUMNS": columns, "PSEUDOLIFE_DOCKER": None,
                                  "NO_PROXY": None, "no_proxy": None}
    case = Case(case_id, ["move", *argv], env=env, daemon=_Recorder, rules=rules,
                platforms=platforms)

    def setup(arm) -> None:
        _guard_selftest()
        for key in _PROXY_KEYS:
            case.env[key] = arm.daemon.url
        if not oracle:
            case.env["PYTHONPATH"] = _stub_dir()

    def after(arm, observation: dict) -> None:
        observation["arm"] = arm.name

    case.setup = setup
    case.after = after
    return case


def _valid(case_id: str, argv: list[str]) -> Case:
    return _case(case_id, argv, rules=("move-oracle-not-run",), oracle=False)


def _deferred(case_id: str, argv: list[str], **kwargs) -> Case:
    return _case(case_id, argv, rules=("move-declared-deferral",), **kwargs)


GOOD_URL = "http://100.64.0.2:8765"

# Cases whose argv passes validation: the oracle never runs them.
ORACLE_NOT_RUN = ("valid-minimal", "valid-dry-run", "valid-resume-yes", "valid-json",
                  "valid-every-option", "valid-url-trailing-slash", "valid-checkout-tab",
                  "valid-to-del", "defer-url-bracketed", "defer-url-empty-userinfo",
                  "defer-url-empty-port", "defer-joined-to", "defer-abbreviation",
                  "defer-repeated-flag")


def cases() -> list[Case]:
    return [
        # tests/test_move_cli.py::test_usage_errors_exit_2, argv for argv
        _case("oracle-test-no-arguments", []),
        _deferred("oracle-test-proxycommand", ["--to", "-oProxyCommand=evil"]),
        _case("oracle-test-ftp-url", ["--to", "root@box", "--target-url", "ftp://x/y"]),
        # argparse, reproduced at COLUMNS=80
        _case("help", ["--help"]),
        _case("help-short", ["-h"]),
        _case("missing-to-with-flags", ["--dry-run", "--yes", "--target-url", GOOD_URL]),
        # --target-url (main, before the --to check)
        _case("url-path", ["--to", "root@box", "--target-url", GOOD_URL + "/api"]),
        _case("url-query", ["--to", "root@box", "--target-url", GOOD_URL + "/?x=1"]),
        _case("url-fragment", ["--to", "root@box", "--target-url", GOOD_URL + "#f"]),
        _case("url-credentials", ["--to", "root@box", "--target-url", "http://u:p@box:8765"]),
        _case("url-scheme", ["--to", "root@box", "--target-url", "ftp://box"]),
        _case("url-no-scheme", ["--to", "root@box", "--target-url", "100.64.0.2:8765"]),
        _case("url-empty", ["--to", "root@box", "--target-url", ""]),
        _case("url-port-range", ["--to", "root@box", "--target-url", "http://box:99999"]),
        _case("url-port-letters", ["--to", "root@box", "--target-url", "http://box:http"]),
        _case("url-empty-host", ["--to", "root@box", "--target-url", "http://:8765"]),
        _case("url-space", ["--to", "root@box", "--target-url", "http://box :8765"]),
        _case("url-nbsp", ["--to", "root@box", "--target-url", "http://box "]),
        _case("url-before-to", ["--to", "", "--target-url", "http://box/x", "--json"]),
        # --to (main: empty or whitespace; Mover.run: a control character)
        _case("to-empty", ["--to", ""]),
        _case("to-space", ["--to", "root@box extra"]),
        _case("to-tab", ["--to", "root@box\t"]),
        _case("to-newline", ["--to", "root@box\n"]),
        _case("to-ideographic-space", ["--to", "root@　box"]),
        _case("to-separator-json", ["--to", "root\x1cbox", "--json"]),
        _case("to-control", ["--to", "root\x01box"]),
        _case("to-escape", ["--to", "root\x1bbox", "--dry-run", "--yes"]),
        _case("to-after-valid-url", ["--to", "a b", "--target-url", GOOD_URL + "/"]),
        _case("to-before-checkout", ["--to", "a\x01b", "--target-checkout", "x\ny"]),
        # --target-checkout (Mover.run)
        _case("checkout-newline", ["--to", "root@box", "--target-checkout", "/srv/x\ny"]),
        _case("checkout-cr", ["--to", "root@box", "--target-checkout", "/srv/x\r"]),
        _case("checkout-cr-slashes", ["--to", "root@box", "--target-checkout", "\r//", "--yes"]),
        # the oracle refuses before any effect; the candidate defers by design
        _deferred("defer-help-columns", ["--help"], columns="100"),
        _deferred("defer-missing-to-columns", ["--yes"], columns="120"),
        _deferred("defer-missing-value", ["--to"]),
        _deferred("defer-to-dash", ["--to", "-"]),
        _deferred("defer-to-negative-number", ["--to", "-1"]),
        _deferred("defer-to-joined-dash", ["--to=-x"]),
        _deferred("defer-unknown-flag", ["--to", "root@box", "--bogus"]),
        _deferred("defer-positional", ["--to", "root@box", "extra"]),
        _deferred("defer-control-json", ["--json", "--to", "a\x01b"]),
        _deferred("defer-checkout-json", ["--to", "root@box", "--target-checkout", "x\n", "--json"]),
        _deferred("defer-url-invalid-unsure", ["--to", "a b", "--target-url", "http://[::1"]),
        # argv that passes validation: the candidate defers; the oracle is never run
        _valid("valid-minimal", ["--to", "root@box"]),
        _valid("valid-dry-run", ["--to", "root@box", "--dry-run"]),
        _valid("valid-resume-yes", ["--to", "box", "--resume", "--yes"]),
        _valid("valid-json", ["--to", "box", "--json"]),
        _valid("valid-every-option", ["--to", "root@box", "--target-checkout", "/srv/pl",
                                      "--target-url", GOOD_URL, "--no-keep-tokens", "--resume",
                                      "--dry-run", "--yes", "--json"]),
        _valid("valid-url-trailing-slash", ["--to", "box", "--target-url", "https://box.example.com/"]),
        _valid("valid-checkout-tab", ["--to", "box", "--target-checkout", "a\tb"]),
        _valid("valid-to-del", ["--to", "a\x7fb"]),
        _valid("defer-url-bracketed", ["--to", "box", "--target-url", "http://[::1]:8765"]),
        _valid("defer-url-empty-userinfo", ["--to", "box", "--target-url", "http://@box"]),
        _valid("defer-url-empty-port", ["--to", "box", "--target-url", "http://box:"]),
        _valid("defer-joined-to", ["--to=root@box"]),
        _valid("defer-abbreviation", ["--dry", "--to", "root@box"]),
        _valid("defer-repeated-flag", ["--yes", "--yes", "--to", "root@box"]),
    ]


MUTANTS = [
    Mutant("move-url-message", "move", FILE, "(no path, query or credentials)",
           "(no path, query, or credentials)", ("url-path",)),
    Mutant("move-refusal-exit", "move", FILE, "            Some(2)\n", "            Some(1)\n",
           ("to-empty",)),
    Mutant("move-isspace-separators", "move", FILE,
           "c.is_whitespace() || ('\\u{1c}'..='\\u{1f}').contains(&c)", "c.is_whitespace()",
           ("to-separator-json",)),
    Mutant("move-json-control", "move", FILE,
           "    if to.chars().any(|c| c < ' ') {\n        return if args.json {",
           "    if to.chars().any(|c| c < ' ') {\n        return if false {",
           ("defer-control-json",)),
    Mutant("move-checkout-cr", "move", FILE, "path.contains(['\\n', '\\r'])", "path.contains('\\n')",
           ("checkout-cr",)),
    Mutant("move-unsure-as-valid", "move", FILE,
           "Some(Origin::Unsure) => return Decision::Defer,", "Some(Origin::Unsure) => {}",
           ("defer-url-invalid-unsure",)),
    Mutant("move-help-any-width", "move", FILE, "let wrapped_at_80 = columns == Some(\"80\");",
           "let wrapped_at_80 = columns.is_some();", ("defer-help-columns",)),
    Mutant("move-valid-refused", "move", FILE,
           "    }\n    Decision::Defer\n}", "    }\n    Decision::Refuse(BAD_TO.to_owned())\n}",
           ("valid-minimal",)),
    Mutant("move-url-checked-after-to", "move", FILE,
           "        Some(Origin::Invalid) => return Decision::Refuse(BAD_URL.to_owned()),",
           "        Some(Origin::Invalid) if !to.is_empty() => return Decision::Refuse(BAD_URL.to_owned()),\n"
           "        Some(Origin::Invalid) => return Decision::Refuse(BAD_TO.to_owned()),",
           ("url-before-to",)),
]


def _assert_safe() -> None:
    """Every case that runs the real oracle must refuse there: none of them
    is a canonical valid argv (a cheap check against an edit that adds one
    without the stub)."""
    for case in cases():
        if case.id in ORACLE_NOT_RUN:
            assert "move-oracle-not-run" in case.rules, case.id
        else:
            assert "move-oracle-not-run" not in case.rules, case.id


_assert_safe()
