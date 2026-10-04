"""``pseudolife-mcp maintainer setup``: maintainer passkeys in one guided command.

    pseudolife-mcp maintainer setup [--local] [--port 8765] [--https-port 8443] [--yes]
    pseudolife-mcp maintainer setup --check

Runs on the daemon host and does every host step of the passkey setup (see
the guide's "Maintainer messages and roles from the Console"), leaving only
what needs the maintainer: creating the passkey in the Console, and checking
that the id prefix the host prints is the one the Console shows.

1. ``GET http://127.0.0.1:<port>/health`` must answer with ``"auth": true``:
   the maintainer routes refuse a tokenless daemon.
2. The tier: the ``pseudolife-mcp-daemon`` container (Docker), else a daemon
   of this install (lite or pip). Everything that reads or writes the
   daemon's config, or reaches the bank, runs in the daemon's environment:
   ``docker exec`` into the container, or this interpreter. That is the
   maintainer CLI's trust boundary (the database owner's credentials, never
   a bearer token), and it needs no bearer.
3. The daemon's ``coordination.maintainer`` keys and passkeys are read
   (``python -m pseudolife_memory.maintainer_setup state``), judged by
   ``MaintainerConfig.problem()``, the rule the daemon (and so ``doctor``)
   applies. A valid name is kept: a passkey is bound to it.
4. Otherwise the name is chosen: Tailscale running with HTTPS certificates
   on gives ``https://<this host's tailnet name>:<https port>``, served by
   ``tailscale serve --bg --https=<https port> http://127.0.0.1:<port>``;
   no Tailscale, or ``--local``, gives ``http://localhost:<port>`` (the
   Console on this machine only). Tailscale installed but stopped, or
   without HTTPS certificates, is refused: guessing would bind passkeys to
   the wrong name.
5. The plan (the serve, the config write, the daemon restart) is shown and
   confirmed once (``--yes`` skips the question; a run without a terminal
   and without it exits 2). The serve is verified and undone if it does not
   take; the config is written with a backup beside it; the Docker tier's
   daemon is restarted (``docker restart``: the same container, image and
   volumes) and waited for. A lite daemon is not restarted here: the
   command says how, and a re-run after it enrols.
6. With the name in place and no active passkey, ``maintainer enrol-code``
   runs in the daemon's environment; the maintainer redeems the code in the
   Console, and one y/N question, after checking the printed prefix and
   label against the Console, confirms the key (N revokes it). That
   question is the host-side check of the security design, asked here
   instead of a separate ``maintainer confirm`` command.

Re-running on a configured host reports what is in place and changes
nothing; ``--check`` only answers (0 set up, 1 not, 2 unreadable), which
the installers ask before offering the setup again. Exit codes, as
``expose``: 0 done or already in place; 2 usage,
declined, or no terminal without ``--yes``; 4 refused before any change
(or the enrolment did not complete); 5 a change did not take: undone
where it could be, and the message names anything left in place (a
written config whose restart failed); 130 interrupted.
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
import tempfile
import time
from urllib.parse import urlsplit

from pseudolife_memory import expose_cli

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 4
EXIT_UNDONE = 5

MODULE = "pseudolife_memory.maintainer_setup"
DAEMON_CONTAINER = "pseudolife-mcp-daemon"
DEFAULT_PORT = 8765
DEFAULT_HTTPS_PORT = 8443
# Design bounds, not measurements: a restarted daemon loads its embedder
# before /health answers, which takes tens of seconds on a cold CPU host.
RESTART_TIMEOUT_S = 180
HEALTH_POLL_S = 2.0
_ENROLLED = re.compile(r"Enrolled \(pending\): (\S+)\s+label: (.*)$")
SETUP = "pseudolife-mcp maintainer setup"


# ── seams (the tests replace these) ──────────────────────────────────────────

def run(argv, *, timeout: float = 120.0, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                              errors="replace", timeout=timeout, check=False, input=input,
                              stdin=None if input is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, "", f"{type(exc).__name__}: {exc}")


def stream(argv, on_line) -> int:
    """Run ``argv``, handing each line of its output to ``on_line`` as it
    comes (``enrol-code`` waits minutes for the Console)."""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    try:
        proc = subprocess.Popen([str(a) for a in argv], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", env=env)
    except OSError as exc:
        on_line(f"could not run it: {exc}")
        return 127
    with proc:
        for line in proc.stdout:
            on_line(line.rstrip("\r\n"))
    return proc.returncode


def docker_cmd() -> str:
    return os.environ.get("PSEUDOLIFE_DOCKER") or "docker"


def which(name: str) -> str | None:
    return shutil.which(name)


def interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def ask(question: str) -> str:
    print(question, end="", flush=True)
    return sys.stdin.readline().strip().lower()


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def health(url: str) -> dict | None:
    return expose_cli.probe_health(url, expose_cli.LOCAL_HEALTH_TIMEOUT_S)


def bearer() -> str | None:
    """This environment's bearer, for asking the running daemon what it
    loaded (doctor's probe); setup works without one."""
    try:
        from pseudolife_memory.credentials import CredentialProvider
        return CredentialProvider.from_environment().snapshot().token
    except Exception:  # noqa: BLE001 - a bad credential file reads as none
        return None


# ── the helpers that run in the daemon's environment ─────────────────────────

def _config_path() -> Path:
    """Where the daemon reads ``config.yaml``: ``PSEUDOLIFE_MCP_CONFIG``, else
    its data dir's (``web.config_io.config_path_for``)."""
    env = os.environ.get("PSEUDOLIFE_MCP_CONFIG")
    if env:
        return Path(env)
    from pseudolife_memory.backup_cli import _default_data_dir
    return _default_data_dir(os.environ) / "config.yaml"


def _read_config(path: Path) -> dict:
    import yaml
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _bank_keys() -> tuple[list | None, str | None]:
    """``(keys, None)`` from the bank, or ``(None, why not)``."""
    from pseudolife_memory import maintainer_cli
    try:
        with maintainer_cli._bank() as storage:
            keys = maintainer_cli._store(storage).passkeys()
    except maintainer_cli.MaintainerCliError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - never a DSN
        return None, type(exc).__name__
    return [{"prefix": k["credential_id"][:12], "label": k["label"], "state": k["state"]}
            for k in keys], None


def _state() -> dict:
    from pseudolife_memory.utils.config import MaintainerConfig
    path = _config_path()
    section = ((_read_config(path).get("coordination") or {}).get("maintainer") or {})
    rp_id, origin = section.get("rp_id", ""), section.get("origin", "")
    try:
        problem = MaintainerConfig(rp_id=rp_id, origin=origin).problem()
    except ValueError as exc:
        problem = str(exc)
    keys, keys_error = _bank_keys()
    return {"config_path": str(path), "rp_id": rp_id, "origin": origin, "problem": problem,
            "keys": keys, "keys_error": keys_error}


def _write(rp_id: str, origin: str) -> dict:
    """Set the two keys in the daemon's config file, keeping the rest; a
    backup beside it and an atomic replace, as ``web.config_io`` writes."""
    import yaml
    path = _config_path()
    data = _read_config(path)
    coordination = data.setdefault("coordination", {})
    if not isinstance(coordination, dict):
        raise ValueError("coordination in the config file is not a mapping")
    section = coordination.setdefault("maintainer", {})
    if not isinstance(section, dict):
        raise ValueError("coordination.maintainer in the config file is not a mapping")
    section.update(rp_id=rp_id, origin=origin)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        fd, backup = tempfile.mkstemp(dir=path.parent, suffix=".bak",
                                      prefix=path.name + f".{time.strftime('%Y%m%d-%H%M%S')}.")
        os.close(fd)
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)
        if backup is not None:
            shutil.copymode(path, tmp)   # mkstemp's 0600 is not the file's mode
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return {"config_path": str(path), "backup": backup}


def host_main(argv: list[str] | None = None) -> int:
    """``state`` or ``write <rp_id> <origin>``, one JSON object on stdout."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["state"]:
        print(json.dumps(_state()))
        return EXIT_OK
    if len(argv) == 3 and argv[0] == "write":
        from pseudolife_memory.utils.config import MaintainerConfig
        problem = MaintainerConfig(rp_id=argv[1], origin=argv[2]).problem()
        if problem is not None:
            print(f"refused: {problem}", file=sys.stderr)
            return EXIT_USAGE
        print(json.dumps(_write(argv[1], argv[2])))
        return EXIT_OK
    print("usage: python -m pseudolife_memory.maintainer_setup state | write <rp_id> <origin>",
          file=sys.stderr)
    return EXIT_USAGE


# ── the guided setup ─────────────────────────────────────────────────────────

class _Refused(Exception):
    """Stop before any (further) change; the message says why."""

    def __init__(self, message: str, code: int = EXIT_REFUSED):
        super().__init__(message)
        self.code = code


def _tier() -> str:
    """``docker`` when the daemon container runs, ``local`` when there is
    none (or no Docker)."""
    docker = docker_cmd()
    if docker == "docker" and not which("docker"):
        return "local"
    proc = run([docker, "inspect", "-f", "{{.State.Running}}", DAEMON_CONTAINER], timeout=60)
    if proc.returncode == 0:
        if proc.stdout.strip() != "true":
            raise _Refused(f"the {DAEMON_CONTAINER} container is not running: start it first")
        return "docker"
    if "no such" in (proc.stderr + proc.stdout).lower():
        return "local"
    raise _Refused("docker did not answer ("
                   f"{expose_cli._first_line(proc.stderr) or f'exit {proc.returncode}'}), so "
                   f"whether the daemon is the {DAEMON_CONTAINER} container cannot be told: start "
                   "Docker (or run this as a user who can use it) and run this again")


def _python(tier: str) -> list[str]:
    if tier == "docker":
        return [docker_cmd(), "exec", DAEMON_CONTAINER, "python"]
    return [sys.executable]


def _maintainer(tier: str, *args: str) -> list[str]:
    return [*_python(tier), "-m", "pseudolife_memory.cli", "maintainer", *args]


def _host(tier: str, *args: str) -> dict:
    proc = run([*_python(tier), "-m", MODULE, *args], timeout=120)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if MODULE in detail and "No module named" in detail:
            raise _Refused("the daemon predates this command: run `pseudolife-mcp update` "
                           f"first, then `{SETUP}`")
        last = detail.splitlines()[-1] if detail else f"exit {proc.returncode}"
        if args[0] == "state":
            raise _Refused(f"reading the daemon's config failed: {last}")
        raise _Refused(f"writing the daemon's config failed: {last}", EXIT_UNDONE)
    try:
        answer = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise _Refused("the daemon's environment answered something that is not JSON") from None
    if not isinstance(answer, dict):
        raise _Refused("the daemon's environment answered an unexpected shape")
    return answer


def _tailnet():
    """``(binary, status)`` with Tailscale running, ``None`` when its CLI is
    not installed; raises ``_Refused`` when it is installed but not up."""
    binary = expose_cli.find_tailscale()
    if binary is None:
        return None
    try:
        return binary, expose_cli._running(binary)
    except expose_cli._Refused as exc:
        raise _Refused(f"{exc}; or pass --local to use http://localhost on this machine "
                       "only") from None


def _dns_name(status: dict) -> str:
    me = status.get("Self") if isinstance(status.get("Self"), dict) else {}
    name = me.get("DNSName") if isinstance(me.get("DNSName"), str) else ""
    return name.rstrip(".").lower()


def _https_state(config: dict, https_port: int, dns: str, port: int) -> tuple[str, str | None]:
    """``("ours" | "foreign" | "none", detail)`` for the HTTPS serve on
    ``https_port``: ours is TLS terminated for this host's name, proxying
    ``/`` to the daemon's loopback port and nothing else."""
    if expose_cli._funnelled(config, https_port):
        return "foreign", f"Funnel (public internet) on port {https_port}"
    tcp = expose_cli._handler(config, https_port)
    web = config.get("Web") if isinstance(config.get("Web"), dict) else {}
    site = web.get(f"{dns}:{https_port}")
    if tcp is None and site is None:
        foreground = config.get("Foreground")
        for session in (foreground.values() if isinstance(foreground, dict) else ()):
            if expose_cli._handler(session, https_port) is not None:
                return "foreign", "a foreground serve"
        return "none", None
    handlers = site.get("Handlers") if isinstance(site, dict) else None
    proxy = (handlers or {}).get("/", {}).get("Proxy") if isinstance(handlers, dict) else None
    wanted = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"127.0.0.1:{port}"}
    if (isinstance(tcp, dict) and tcp.get("HTTPS") and proxy in wanted
            and set(handlers) == {"/"}):
        return "ours", None
    if proxy:
        return "foreign", f"HTTPS proxying to {proxy}"
    return "foreign", expose_cli._describe(tcp) if tcp is not None else "another web serve"


def _origin_port(origin: str) -> int:
    parts = urlsplit(origin)
    return parts.port or (443 if parts.scheme == "https" else 80)


def _choose(args, ts) -> tuple[str, str]:
    """The name for a host with none: the tailnet's HTTPS name, or localhost."""
    if args.local or ts is None:
        return "localhost", f"http://localhost:{args.port}"
    _binary, status = ts
    dns = _dns_name(status)
    if not dns:
        raise _Refused("tailscale reports no DNS name for this host; pass --local to use "
                       "http://localhost on this machine only")
    certs = [c.rstrip(".").lower() for c in status.get("CertDomains") or [] if isinstance(c, str)]
    if dns not in certs:
        raise _Refused("HTTPS certificates are off for this tailnet, so the Console cannot be "
                       "served over HTTPS here: turn them on in the Tailscale admin console "
                       "(DNS, HTTPS Certificates) and run this again, or pass --local to use "
                       f"http://localhost:{args.port} on this machine only")
    suffix = "" if args.https_port == 443 else f":{args.https_port}"
    return dns, f"https://{dns}{suffix}"


def _running_name(port: int) -> tuple[str, str] | None:
    """What the running daemon loaded, asked as ``doctor`` asks it; ``None``
    without a bearer or an answer."""
    token = bearer()
    if not token:
        return None
    from pseudolife_memory import doctor_cli
    answer = doctor_cli.maintainer_probe(f"http://127.0.0.1:{port}", token, timeout=2.0)
    if answer.get("state") == "on":
        return answer.get("rp_id") or "", answer.get("origin") or ""
    if answer.get("state") in ("off", "invalid", "off_or_invalid"):
        return "", ""
    return None


def _confirm(args, question: str) -> None:
    if args.yes:
        return
    if not interactive():
        raise _Refused("no terminal to confirm on: run this in a terminal, or rerun with --yes",
                       EXIT_USAGE)
    if ask(question) not in ("y", "yes"):
        raise _Refused("declined; nothing changed", EXIT_USAGE)


def _serve(binary: str, https_port: int, dns: str, port: int) -> None:
    serve_args = ["serve", "--bg", f"--https={https_port}", f"http://127.0.0.1:{port}"]
    proc = expose_cli.run_tailscale(binary, serve_args)
    if expose_cli._permission_refusal(proc):
        raise _Refused(expose_cli._permission_message(proc))
    try:
        after, _ = _https_state(expose_cli._serve_config(binary), https_port, dns, port)
    except expose_cli._Refused as exc:
        raise _Refused(f"the serve command ran but its result could not be read ({exc}): "
                       "check `tailscale serve status`", EXIT_UNDONE) from None
    if proc.returncode == 0 and after == "ours":
        print(f"served: https://{dns}" + ("" if https_port == 443 else f":{https_port}")
              + f" -> http://127.0.0.1:{port}")
        return
    undone = "nothing to undo"
    if after != "none":
        undone = "undone" if _serve_off(binary, https_port) else (
            f"the undo failed: run `tailscale serve --https={https_port} off` yourself")
    reason = expose_cli._first_line(proc.stderr) or (
        f"exit {proc.returncode}" if proc.returncode else "the serve status does not show it")
    # Serve off for the whole tailnet: tailscale prints the admin link that
    # turns it on and waits for it, which the timeout cuts short.
    link = re.search(r"https://login\.tailscale\.com/\S+", proc.stdout or "")
    if link:
        reason += (f"; Serve may be off for this tailnet: turn it on at {link.group(0)}, then "
                   f"run `{SETUP}` again")
    raise _Refused(f"the HTTPS serve did not take ({reason}); {undone}", EXIT_UNDONE)


def _serve_off(binary: str, https_port: int) -> bool:
    return expose_cli.run_tailscale(binary, ["serve", f"--https={https_port}", "off"]).returncode == 0


def _restart(tier: str, port: int) -> None:
    proc = run([docker_cmd(), "restart", DAEMON_CONTAINER], timeout=300)
    if proc.returncode != 0:
        raise _Refused("the daemon restart failed "
                       f"({expose_cli._first_line(proc.stderr) or f'exit {proc.returncode}'}); "
                       f"the config is written: run `docker restart {DAEMON_CONTAINER}`, then "
                       f"`{SETUP}` again", EXIT_UNDONE)
    print("restarted the daemon; waiting for it to answer...", flush=True)
    deadline = time.monotonic() + RESTART_TIMEOUT_S
    while True:
        answer = health(f"http://127.0.0.1:{port}")
        if isinstance(answer, dict) and answer.get("status") == "ok":
            return
        if time.monotonic() >= deadline:
            raise _Refused(f"the daemon did not report healthy within {RESTART_TIMEOUT_S} s "
                           f"of its restart: check `docker logs {DAEMON_CONTAINER}`, then run "
                           f"`{SETUP}` again", EXIT_UNDONE)
        sleep(HEALTH_POLL_S)


def _check_key(tier: str, prefix: str, label: str, *, fresh: bool) -> int:
    """The host-side check: activate a pending key only once the maintainer
    has seen the same prefix and label in the Console."""
    no = "N revokes it" if fresh else "N leaves it pending"
    reply = ask(f"Does the Console (Settings, Your passkeys) show {prefix} labelled "
                f"{label!r}? Activate it [y/N] ({no}): ")
    if reply in ("y", "yes"):
        proc = run(_maintainer(tier, "confirm", "--", prefix))
        if proc.returncode != 0:
            print(f"maintainer setup: confirm failed: "
                  f"{expose_cli._first_line(proc.stderr) or proc.returncode}", file=sys.stderr)
            return EXIT_REFUSED
        print(f"active: passkey {prefix} ({label}) now signs your Console messages and roles")
        return EXIT_OK
    if fresh:
        proc = run(_maintainer(tier, "revoke", "--", prefix))
        state = ("revoked" if proc.returncode == 0 else
                 f"NOT revoked (run `pseudolife-mcp maintainer revoke {prefix}`)")
        print(f"passkey {prefix} {state}. If someone else redeemed the code, check "
              f"`pseudolife-mcp maintainer list`; to try again, run `{SETUP}`")
    else:
        print(f"left pending. Revoke it if it is not yours: pseudolife-mcp maintainer revoke {prefix}")
    return EXIT_REFUSED


def _enrol(tier: str, origin: str) -> int:
    print(f"\nNext, in your browser: open {origin}/ui/, then Settings, Your passkeys. Enter the "
          "code below with a label for this passkey, and create the passkey when the browser "
          "asks.", flush=True)
    seen: dict = {}

    def on_line(line: str) -> None:
        match = _ENROLLED.search(line)
        if match:
            seen.update(prefix=match.group(1), label=match.group(2).strip())
            print(line, flush=True)
        elif not seen:
            print(line, flush=True)   # after the key: setup asks instead of advising

    stream(_maintainer(tier, "enrol-code"), on_line)
    if not seen:
        print(f"maintainer setup: no passkey was enrolled; run `{SETUP}` again for a new code",
              file=sys.stderr)
        return EXIT_REFUSED
    return _check_key(tier, seen["prefix"], seen["label"], fresh=True)


def _setup(args) -> int:
    url = f"http://127.0.0.1:{args.port}"
    answer = health(url)
    if not isinstance(answer, dict):
        raise _Refused(f"no daemon answers at {url}/health: start it first (or pass --port)")
    if answer.get("auth") is not True:
        raise _Refused(f'the daemon at {url} does not report "auth": true; maintainer passkeys '
                       "need a daemon that requires a bearer token (an install without "
                       "--no-token, or PSEUDOLIFE_MCP_TOKEN(S) in its environment)")
    tier = _tier()
    state = _host(tier, "state")
    configured = state.get("problem") is None
    if configured:
        # Kept whatever detection would say now: a passkey is bound to it.
        rp_id, origin = state["rp_id"], state["origin"]
        ts = None
        if origin.startswith("https://"):
            try:
                ts = _tailnet()
            except _Refused:
                ts = None    # Tailscale down: its serve is not this run's to check
    else:
        ts = None if args.local else _tailnet()
        rp_id, origin = _choose(args, ts)

    plan: list[str] = []
    serve = None
    parts = urlsplit(origin)
    if parts.scheme == "https" and ts is not None and _dns_name(ts[1]) == rp_id:
        https_port = _origin_port(origin)
        try:
            config = expose_cli._serve_config(ts[0])
        except expose_cli._Refused as exc:
            raise _Refused(str(exc)) from None
        served, detail = _https_state(config, https_port, rp_id, args.port)
        if served == "foreign":
            raise _Refused(f"tailnet port {https_port} already serves {detail}; setup never "
                           "replaces another serve. Remove it yourself, or pass --https-port "
                           "for a free port" if not configured else
                           f"tailnet port {https_port} serves {detail}, not this daemon: the "
                           f"Console at {origin} may not reach it. Fix the serve yourself")
        if served == "none":
            serve = (ts[0], https_port)
            plan.append(f"run      tailscale serve --bg --https={https_port} "
                        f"http://127.0.0.1:{args.port}")
    write = not configured
    if write:
        problem = state.get("problem")
        if problem != "unset":
            plan.append(f"replace  the daemon's coordination.maintainer ({problem})")
        plan.append(f"write    coordination.maintainer rp_id {rp_id}, origin {origin} "
                    f"into {state.get('config_path')} (a backup beside it)")
    restart = write
    if configured and _running_name(args.port) not in (None, (rp_id, origin)):
        restart = True
    if restart and tier == "docker":
        plan.append(f"restart  the daemon container ({DAEMON_CONTAINER}) so it reads them")

    if origin.startswith("http://localhost"):
        print(f"Console name: {origin} (passkeys work in a browser on this machine only)")
    if plan:
        print("plan:")
        for line in plan:
            print(f"  {line}")
        _confirm(args, "make these changes? [y/N] ")
        if serve:
            _serve(serve[0], serve[1], rp_id, args.port)
        if write:
            try:
                written = _host(tier, "write", rp_id, origin)
            except _Refused:
                if serve and _serve_off(serve[0], serve[1]):
                    print(f"took the new serve on port {serve[1]} back off")
                raise
            print(f"wrote {written.get('config_path')}"
                  + (f" (previous: {written['backup']})" if written.get("backup") else ""))
        if restart and tier == "docker":
            _restart(tier, args.port)
    if restart and tier == "local":
        # Nothing here owns a lite daemon's process. Enrolling before it
        # reads the name would hand out a code the Console cannot redeem.
        print(f"Restart the daemon so it reads the name (stop the `pseudolife-mcp serve` process; "
              f"your next session starts it again), then run `{SETUP}` again to enrol your "
              "passkey.")
        return EXIT_OK if write else EXIT_REFUSED

    keys = state.get("keys")
    if keys is None:
        raise _Refused(f"could not read the passkeys ({state.get('keys_error')}); the name is in "
                       f"place: run `{SETUP}` again once the bank answers")
    active = [k for k in keys if k.get("state") == "active"]
    pending = [k for k in keys if k.get("state") == "pending"]
    if active:
        print(f"in place: maintainer passkeys on {origin} (rp_id {rp_id}), "
              f"{len(active)} active key(s)" + ("" if plan else "; nothing changed"))
        return EXIT_OK
    if not interactive():
        print(f"The name is in place ({origin}). Enrolling a passkey needs you at the Console: "
              f"run `{SETUP}` in a terminal.")
        return EXIT_OK
    if pending:
        key = pending[0]
        print(f"A passkey is pending: {key['prefix']} ({key['label']}).")
        return _check_key(tier, key["prefix"], key["label"], fresh=False)
    return _enrol(tier, origin)


def _port(value: str) -> int:
    return expose_cli._port(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=SETUP,
        description="Set up maintainer passkeys on this daemon host: the Console's HTTPS name, "
                    "the daemon's config, and your first passkey.")
    parser.add_argument("--local", action="store_true",
                        help="name the Console http://localhost:<port> (this machine only), "
                             "even on a tailnet")
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT,
                        help=f"the daemon's port (default {DEFAULT_PORT})")
    parser.add_argument("--https-port", type=_port, default=DEFAULT_HTTPS_PORT,
                        help=f"the tailnet HTTPS port for the Console (default {DEFAULT_HTTPS_PORT})")
    parser.add_argument("--yes", action="store_true",
                        help="make the host changes without asking (the passkey check is "
                             "always asked)")
    parser.add_argument("--check", action="store_true",
                        help="change nothing: exit 0 when passkeys are set up (a valid name and "
                             "an active key), 1 when not, 2 when that cannot be read")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.check:
        state = passkeys_set_up()
        print({True: "maintainer passkeys: in place", False: "maintainer passkeys: not set up",
               None: "maintainer passkeys: could not read the daemon's state"}[state])
        return {True: 0, False: 1, None: 2}[state]
    try:
        return _setup(args)
    except _Refused as exc:
        print(f"maintainer setup: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print(f"\nmaintainer setup: interrupted. What was done stays done; run `{SETUP}` again "
              "to pick up from there", file=sys.stderr)
        return 130


def passkeys_set_up() -> bool | None:
    """Whether this host's daemon has a valid name and an active passkey;
    ``None`` when that cannot be read (``pseudolife-mcp update`` then says
    nothing)."""
    try:
        state = _host(_tier(), "state")
    except _Refused:
        return None
    if state.get("problem") is not None:
        return False
    keys = state.get("keys")
    if keys is None:
        return None
    return any(k.get("state") == "active" for k in keys)


if __name__ == "__main__":
    raise SystemExit(host_main())
