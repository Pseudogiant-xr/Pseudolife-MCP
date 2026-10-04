"""``pseudolife-mcp expose``: make this host's daemon reachable from the tailnet.

    pseudolife-mcp expose tailscale [--port 8765] [--yes] [--json]
    pseudolife-mcp expose off [--port 8765] [--yes] [--json]
    pseudolife-mcp expose status [--port 8765] [--json]

Runs on the daemon host and drives the ``tailscale`` CLI. ``tailscale``:

1. ``GET http://127.0.0.1:<port>/health`` must answer ``status: ok`` and
   ``"auth": true``: an unauthenticated bank is never exposed, because a
   tunnel to loopback looks local to the daemon and bypasses its bind guard.
2. The ``tailscale`` CLI is found (PATH, then the Windows and macOS default
   install locations) and ``tailscale status --json`` must report
   ``BackendState: Running``.
3. ``tailscale serve status --json`` is read. A TCP forward on the port to
   ``127.0.0.1:<port>`` is "already exposed" (exit 0, nothing run); any
   other serve on the port, and Funnel (public internet) on it, is someone
   else's and is never replaced.
4. The plan (the exact command and the client URL,
   ``http://<tailscale ip -4>:<port>``) is shown and confirmed (``--yes``
   skips the question; a non-interactive run without it exits 2), then
   ``tailscale serve --bg --tcp=<port> tcp://127.0.0.1:<port>`` runs.
5. The serve status must now show the forward, or the change is undone.
   The tailnet URL's ``/health`` is then probed and its ``bank`` fingerprint
   compared with the local one. The probe is advisory: a host cannot always
   reach its own tailnet address through serve, so a failed probe is
   reported and nothing is rolled back.

``off`` removes only a forward to ``127.0.0.1:<port>``; anything else on the
port is left alone and reported. ``status`` prints the client URL, or why
there is none.

Exit codes: 0 done or already in that state; 2 usage, declined, or no TTY
without ``--yes``; 4 refused before any change; 5 changed but the serve did
not take (undone), or its result could not be read back to verify.

Standard library only, so it runs from a shim runtime: the daemon probe is
a plain ``urllib`` GET with proxies and redirects disabled.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 4
EXIT_UNDONE = 5

DEFAULT_PORT = 8765
# Design bounds, not measurements: a loopback /health answers in
# milliseconds; the tailnet probe may cross a relay; a tailscale CLI call
# that takes longer than this is wedged.
LOCAL_HEALTH_TIMEOUT_S = 3.0
TAILNET_HEALTH_TIMEOUT_S = 5.0
TAILSCALE_TIMEOUT_S = 30.0

OPERATOR_FIX = "sudo tailscale set --operator=$USER"
INSTALL_FIX = "install Tailscale (https://tailscale.com/download), then run `tailscale up`"
# How the tailscale CLI words a refusal for this user ("Access denied: serve
# config denied" on Linux without root or an operator grant, and on Windows
# for a user other than the one logged in to Tailscale; or a hint naming
# `--operator`). The bare word "operator" is not enough.
_PERMISSION = re.compile(r"access denied|permission denied|not permitted|--operator\b|"
                         r"unauthori[sz]ed|must be (?:root|admin)", re.IGNORECASE)


# ── seams (the tests replace these) ──────────────────────────────────────────

def platform() -> str:
    return sys.platform


def default_locations() -> list[Path]:
    """Where the Tailscale installers put the CLI when it is not on PATH.
    On Windows the 64-bit Program Files (``ProgramW6432``) first: under a
    32-bit Python, ``ProgramFiles`` names the x86 folder instead."""
    if os.name == "nt":
        base = (os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles")
                or r"C:\Program Files")
        return [Path(base) / "Tailscale" / "tailscale.exe"]
    if platform() == "darwin":
        return [Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")]
    return []


def interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(question: str) -> bool:
    print(question, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The daemon's ``/health`` never redirects: a 3xx surfaces as an
    ``HTTPError`` instead of being followed somewhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_health(url: str, timeout: float) -> tuple[str, dict | None]:
    """``(kind, payload)`` for ``<url>/health``: ``("ok", dict)`` when a
    JSON object answers, ``("none", None)`` when nothing answers, and
    ``("other", None)`` when something answers that is not the daemon (a
    redirect, or a body that is not a JSON object). A non-2xx answer with a
    JSON body is still the daemon's (``/health`` serves a degraded payload
    as 503). No proxies, no redirects."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(url + "/health", timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return "other", None
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            return "other", None
    except Exception:  # noqa: BLE001 — refused, reset, timed out, unreadable
        return "none", None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "other", None
    return ("ok", payload) if isinstance(payload, dict) else ("other", None)


def probe_health(url: str, timeout: float) -> dict | None:
    """``<url>/health`` as a dict, or ``None`` when the daemon does not
    answer it (see :func:`fetch_health`)."""
    return fetch_health(url, timeout)[1]


def find_tailscale() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for path in default_locations():
        if path.is_file():
            return str(path)
    return None


def run_tailscale(binary: str, args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([binary, *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=TAILSCALE_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        # What it printed before the timeout: a serve waiting for the admin
        # to turn Serve on prints the link that does it, then waits.
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return subprocess.CompletedProcess([binary, *args], 124, out,
                                           f"timed out after {TAILSCALE_TIMEOUT_S:g}s")
    except OSError as exc:
        return subprocess.CompletedProcess([binary, *args], 127, "", str(exc))


# ── reading tailscale's state ────────────────────────────────────────────────

class _Refused(Exception):
    """A check failed before anything changed."""


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _running(binary: str) -> dict:
    """``tailscale status --json``; raises _Refused unless the backend runs."""
    proc = run_tailscale(binary, ["status", "--json"])
    try:
        status = json.loads(proc.stdout) if proc.stdout.strip() else None
    except ValueError:
        status = None
    if not isinstance(status, dict):
        detail = _first_line(proc.stderr) or f"exit {proc.returncode}"
        raise _Refused(f"tailscale is not running ({detail}): start Tailscale and run `tailscale up`")
    state = status.get("BackendState")
    if state != "Running":
        raise _Refused(f"tailscale reports BackendState {state!r}, not 'Running': run `tailscale up` "
                       "(and log in) first")
    return status


def _tailnet_ipv4(binary: str, status: dict) -> str | None:
    proc = run_tailscale(binary, ["ip", "-4"])
    candidates = proc.stdout.split() if proc.returncode == 0 else []
    own = (status.get("Self") or {}).get("TailscaleIPs") if isinstance(status.get("Self"), dict) else None
    candidates += [ip for ip in own or [] if isinstance(ip, str)]
    for ip in candidates:
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip):
            return ip
    return None


def _serve_config(binary: str) -> dict:
    """``tailscale serve status --json`` (an ``ipn.ServeConfig``: ``TCP``
    keyed by port as a string, ``Web``, ``Services``, ``Foreground`` keyed
    by session). Empty output or ``null`` is an empty config."""
    proc = run_tailscale(binary, ["serve", "status", "--json"])
    if proc.returncode != 0:
        raise _Refused("could not read `tailscale serve status --json` "
                       f"({_first_line(proc.stderr) or f'exit {proc.returncode}'})")
    text = proc.stdout.strip()
    if not text or text == "null":
        return {}
    try:
        config = json.loads(text)
    except ValueError:
        raise _Refused("`tailscale serve status --json` printed something that is not JSON") from None
    if not isinstance(config, dict):
        raise _Refused("`tailscale serve status --json` printed an unexpected shape")
    return config


def _handler(config, port: int):
    tcp = config.get("TCP") if isinstance(config, dict) else None
    return tcp.get(str(port)) if isinstance(tcp, dict) else None


def _is_ours(handler, port: int) -> bool:
    """A plain TCP forward to the daemon's loopback port: no TLS termination,
    no HTTP(S) handling, no PROXY protocol header the daemon would choke on."""
    return (isinstance(handler, dict) and handler.get("TCPForward") == f"127.0.0.1:{port}"
            and not handler.get("TerminateTLS") and not handler.get("HTTPS")
            and not handler.get("HTTP") and not handler.get("ProxyProtocol"))


def _describe(handler) -> str:
    if not isinstance(handler, dict):
        return "an unreadable handler"
    parts = []
    if handler.get("TCPForward"):
        parts.append(f"a TCP forward to {handler['TCPForward']}")
    if handler.get("TerminateTLS"):
        parts.append(f"TLS terminated for {handler['TerminateTLS']}")
    if handler.get("HTTPS"):
        parts.append("HTTPS")
    if handler.get("HTTP"):
        parts.append("HTTP")
    if handler.get("ProxyProtocol"):
        parts.append(f"PROXY protocol v{handler['ProxyProtocol']}")
    return ", ".join(parts) or "a handler of an unknown kind"


def _funnelled(config, port: int) -> bool:
    """A truthy ``AllowFunnel`` entry (keyed ``host:port``) for the port."""
    funnel = config.get("AllowFunnel") if isinstance(config, dict) else None
    return isinstance(funnel, dict) and any(
        on and str(key).endswith(f":{port}") for key, on in funnel.items())


def _port_state(config: dict, port: int) -> tuple[str, str | None]:
    """``("ours" | "foreign" | "none", detail)`` for the port. A foreground
    serve (one a terminal holds open) is never ours to keep or remove, and a
    port open to Funnel is on the public internet, never "exposed on the
    tailnet", whatever forwards it."""
    foreground = config.get("Foreground")
    sessions = list(foreground.values()) if isinstance(foreground, dict) else []
    if _funnelled(config, port) or any(_funnelled(s, port) for s in sessions):
        return "foreign", f"Funnel (public internet) on port {port}"
    background = _handler(config, port)
    if background is not None and _is_ours(background, port):
        return "ours", None
    if background is not None:
        return "foreign", _describe(background)
    for session in sessions:
        held = _handler(session, port)
        if held is not None:
            return "foreign", f"a foreground serve ({_describe(held)})"
    return "none", None


# ── the report ───────────────────────────────────────────────────────────────

class _Report:
    def __init__(self, action: str, port: int, as_json: bool):
        self.as_json = as_json
        self.data = {"action": action, "port": port, "tailscale": None, "state": None,
                     "url": None, "detail": None, "command": None, "daemon": None,
                     "probe": None, "warnings": [], "notes": [], "error": None, "exit": None}

    def say(self, line: str = "") -> None:
        if not self.as_json:
            print(line)

    def warn(self, line: str) -> None:
        self.data["warnings"].append(line)
        if not self.as_json:
            print(line, file=sys.stderr)

    def note(self, line: str) -> None:
        self.data["notes"].append(line)
        self.say(line)

    def fail(self, code: int, line: str) -> int:
        self.data["error"] = line
        if not self.as_json:
            print(f"expose: {line}", file=sys.stderr)
        return self.finish(code)

    def finish(self, code: int) -> int:
        self.data["exit"] = code
        if self.as_json:
            print(json.dumps(self.data, indent=2, default=str))
        return code


def _confirm(args, report: _Report, question: str) -> int | None:
    """``None`` to go ahead, else the exit code."""
    if args.yes:
        return None
    if not interactive():
        return report.fail(EXIT_USAGE, "no terminal to confirm on: rerun with --yes")
    if not _ask(question):
        return report.fail(EXIT_USAGE, "declined; nothing changed")
    return None


def _permission_refusal(proc: subprocess.CompletedProcess) -> bool:
    return proc.returncode != 0 and bool(_PERMISSION.search(proc.stderr or ""))


def _permission_fix() -> str:
    """What lets this user change the serve config, per OS."""
    system = platform()
    if system == "win32":
        return ("run it as the Windows user logged in to Tailscale, or from an elevated "
                "(administrator) shell")
    if system == "darwin":
        return "run it as the macOS user logged in to the Tailscale app"
    return f"grant your user operator rights once with `{OPERATOR_FIX}`, or run this as root"


def _permission_message(proc: subprocess.CompletedProcess) -> str:
    return (f"tailscale refused the change for this user ({_first_line(proc.stderr)}): "
            f"{_permission_fix()}")


# ── the actions ──────────────────────────────────────────────────────────────

def _expose(args, report: _Report) -> int:
    port = args.port
    local = f"http://127.0.0.1:{port}"
    kind, health = fetch_health(local, LOCAL_HEALTH_TIMEOUT_S)
    if kind == "other":
        return report.fail(EXIT_REFUSED, "something that is not the Pseudolife daemon answers on "
                                         f"127.0.0.1:{port} (a redirect or a body that is not its "
                                         "/health JSON); pass --port for the daemon's port")
    if health is None:
        return report.fail(EXIT_REFUSED, f"no daemon answers at {local}/health: start it first "
                                         "(or pass --port)")
    report.data["daemon"] = {"status": health.get("status"), "auth": health.get("auth"),
                             "bank": health.get("bank")}
    if health.get("status") != "ok":
        return report.fail(EXIT_REFUSED, f"the daemon at {local} reports status "
                                         f"{health.get('status')!r}: fix it before exposing it")
    if health.get("auth") is not True:
        return report.fail(EXIT_REFUSED, f'the daemon at {local} does not report "auth": true, so '
                                         "anyone on the tailnet could read and write the bank. Give it "
                                         "a token first (PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKENS, "
                                         "see docs/guide/remote-bank.md); an unauthenticated bank is "
                                         "never exposed")
    binary = find_tailscale()
    if binary is None:
        return report.fail(EXIT_REFUSED, f"the tailscale CLI was not found: {INSTALL_FIX}")
    report.data["tailscale"] = binary
    try:
        status = _running(binary)
        config = _serve_config(binary)
    except _Refused as exc:
        return report.fail(EXIT_REFUSED, str(exc))
    state, detail = _port_state(config, port)
    if state == "foreign":
        return report.fail(EXIT_REFUSED, f"tailnet port {port} already serves {detail}; expose never "
                                         "replaces another serve. Remove it yourself, or pass "
                                         "--port for the daemon's port if this is not it")
    ip = _tailnet_ipv4(binary, status)
    if ip is None:
        return report.fail(EXIT_REFUSED, "tailscale reports no tailnet IPv4 address for this host")
    url = f"http://{ip}:{port}"
    if state == "ours":
        report.data.update(state="already_exposed", url=url)
        report.say(f"already exposed: clients use {url}")
        return report.finish(EXIT_OK)

    serve_args = ["serve", "--bg", f"--tcp={port}", f"tcp://127.0.0.1:{port}"]
    report.data["command"] = ["tailscale", *serve_args]
    report.say("plan:")
    report.say(f"  run      tailscale {' '.join(serve_args)}")
    report.say(f"  clients  {url}")
    stop = _confirm(args, report, f"expose this daemon on the tailnet at {url}? [y/N] ")
    if stop is not None:
        return stop

    proc = run_tailscale(binary, serve_args)
    if _permission_refusal(proc):
        return report.fail(EXIT_REFUSED, _permission_message(proc))
    try:
        after, _detail = _port_state(_serve_config(binary), port)
    except _Refused as exc:
        # Nothing to judge an undo by: removing a serve that may have taken,
        # or reporting an undo that may not have been needed, would both
        # mislead. Say what is known and where to look.
        ran = f"exit {proc.returncode}" + (
            f", {_first_line(proc.stderr)}" if _first_line(proc.stderr) else "")
        return report.fail(EXIT_UNDONE, f"the serve command ran ({ran}) but could not verify its "
                                        f"result: {exc}. Check `tailscale serve status`; if port "
                                        f"{port} forwards to 127.0.0.1:{port} and you do not want "
                                        f"it, run `pseudolife-mcp expose off --port {port}`")
    if proc.returncode != 0 and after == "none":
        return report.fail(EXIT_REFUSED, "tailscale refused the serve "
                                         f"({_first_line(proc.stderr) or f'exit {proc.returncode}'})")
    if proc.returncode != 0 or after != "ours":
        # The port was free before the command, so whatever is on it now is
        # this run's doing; take it back off.
        undone = "nothing to undo"
        if after != "none":
            undo = run_tailscale(binary, ["serve", f"--tcp={port}", "off"])
            undone = ("undone" if undo.returncode == 0 else
                      f"the undo failed ({_first_line(undo.stderr) or f'exit {undo.returncode}'}): run "
                      f"`tailscale serve --tcp={port} off` yourself")
        reason = (_first_line(proc.stderr) or f"exit {proc.returncode}") if proc.returncode else (
            "the serve status does not show the forward")
        return report.fail(EXIT_UNDONE, f"the serve did not take ({reason}); {undone}")

    report.data.update(state="exposed", url=url)
    report.say(f"exposed: clients use {url}")
    _probe(report, url, health.get("bank"))
    report.note("next, for each joining machine: run `pseudolife-mcp invite <machine>` here, then "
                "give the code it prints to that machine's installer (option 2), or run "
                f"`pseudolife-mcp pair {url} <code>` there")
    report.note(f"to remove the forward: pseudolife-mcp expose off --port {port}")
    return report.finish(EXIT_OK)


def _probe(report: _Report, url: str, bank) -> None:
    """The advisory check through the tailnet address. Never rolls back."""
    remote = probe_health(url, TAILNET_HEALTH_TIMEOUT_S)
    if remote is None:
        report.data["probe"] = {"reachable": False, "same_bank": None}
        report.warn(f"this host could not reach {url}/health through the tailnet, which is common "
                    f"for a host's own serve address; check from another machine: curl {url}/health")
        return
    other = remote.get("bank")
    same = (other == bank) if (bank and other) else None
    report.data["probe"] = {"reachable": True, "same_bank": same}
    if same is True:
        report.say(f"checked: {url}/health answers with the same bank ({bank})")
    elif same is False:
        report.warn(f"{url}/health answers with a different bank ({other}, this daemon has {bank}); "
                    "something else may be serving that address")
    else:
        report.say(f"checked: {url}/health answers (bank fingerprint not reported yet)")


def _off(args, report: _Report) -> int:
    port = args.port
    binary = find_tailscale()
    if binary is None:
        return report.fail(EXIT_REFUSED, f"the tailscale CLI was not found: {INSTALL_FIX}")
    report.data["tailscale"] = binary
    try:
        _running(binary)
        state, detail = _port_state(_serve_config(binary), port)
    except _Refused as exc:
        return report.fail(EXIT_REFUSED, str(exc))
    if state == "none":
        report.data["state"] = "not_exposed"
        report.say(f"not exposed: nothing serves tailnet port {port}")
        return report.finish(EXIT_OK)
    if state == "foreign":
        report.data.update(state="foreign", detail=detail)
        return report.fail(EXIT_REFUSED, f"tailnet port {port} serves {detail}, not this daemon's "
                                         f"forward to 127.0.0.1:{port}; left alone")
    off_args = ["serve", f"--tcp={port}", "off"]
    report.data["command"] = ["tailscale", *off_args]
    report.say("plan:")
    report.say(f"  run      tailscale {' '.join(off_args)}")
    stop = _confirm(args, report, f"remove the tailnet forward on port {port}? [y/N] ")
    if stop is not None:
        return stop
    proc = run_tailscale(binary, off_args)
    if _permission_refusal(proc):
        return report.fail(EXIT_REFUSED, _permission_message(proc))
    try:
        after, _detail = _port_state(_serve_config(binary), port)
    except _Refused as exc:
        return report.fail(EXIT_REFUSED, str(exc))
    if after == "ours":
        return report.fail(EXIT_REFUSED, "tailscale did not remove the forward "
                                         f"({_first_line(proc.stderr) or f'exit {proc.returncode}'})")
    report.data["state"] = "removed"
    report.say(f"removed: tailnet port {port} no longer forwards to this daemon")
    return report.finish(EXIT_OK)


def _status(args, report: _Report) -> int:
    port = args.port
    binary = find_tailscale()
    if binary is None:
        report.data.update(state="unavailable", detail="the tailscale CLI was not found")
        report.say("not exposed: the tailscale CLI was not found")
        return report.finish(EXIT_OK)
    report.data["tailscale"] = binary
    try:
        status = _running(binary)
        state, detail = _port_state(_serve_config(binary), port)
    except _Refused as exc:
        report.data.update(state="unavailable", detail=str(exc))
        report.say(f"not exposed: {exc}")
        return report.finish(EXIT_OK)
    if state == "foreign":
        report.data.update(state="foreign", detail=detail)
        report.say(f"not exposed: tailnet port {port} serves {detail}")
        return report.finish(EXIT_OK)
    if state == "none":
        report.data["state"] = "not_exposed"
        report.say("not exposed")
        return report.finish(EXIT_OK)
    ip = _tailnet_ipv4(binary, status)
    if ip is None:
        report.data.update(state="unavailable", detail="no tailnet IPv4 address for this host")
        report.say("not exposed: tailscale reports no tailnet IPv4 address for this host")
        return report.finish(EXIT_OK)
    url = f"http://{ip}:{port}"
    report.data.update(state="exposed", url=url)
    report.say(f"exposed: clients use {url}")
    return report.finish(EXIT_OK)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port: {value!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"not a port: {value!r}")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp expose",
        description="Make this host's daemon reachable from the tailnet (Tailscale Serve, TCP).")
    sub = parser.add_subparsers(dest="action", required=True)
    for name, text, changes in (
            ("tailscale", "forward tailnet port <port> to the daemon on 127.0.0.1:<port>", True),
            ("off", "remove that forward (never anything else on the port)", True),
            ("status", "print the client URL, or why there is none", False)):
        action = sub.add_parser(name, help=text, description=text)
        action.add_argument("--port", type=_port, default=DEFAULT_PORT,
                            help=f"the daemon's port, and the tailnet port (default {DEFAULT_PORT})")
        if changes:
            action.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        action.add_argument("--json", action="store_true", help="print a JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(sys.argv[2:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    report = _Report(args.action, args.port, args.json)
    if args.action == "tailscale":
        return _expose(args, report)
    if args.action == "off":
        return _off(args, report)
    return _status(args, report)
