"""Opt-in unattended daemon updates: ``pseudolife-mcp update --unattended``.

A scheduled task (Windows) or a systemd ``--user`` timer (Linux), installed
with ``pseudolife-mcp update --schedule HH:MM``, runs ``pseudolife-mcp
update --unattended`` once a day. The run applies a new release only when
BOTH hold: ``config.yaml`` ``updates.unattended_daemon`` is on (read from
the daemon's ``/health``, so installing the timer alone changes nothing),
and the agent board lists no active session. It then takes the same path
as an attended ``pseudolife-mcp update`` (backup, rollback tag, recreate
only the daemon, health, the client side), and posts a board notice from
the daemon's reserved principal saying it updated, or that it held off and
why. Every run appends to ``~/.pseudolife-mcp/unattended-update.log``.

Exit codes: 0 updated, 3 current (nothing to do), 4 held off (the knob is
off, a session is active, the board cannot be read, or another
``pseudolife-mcp update`` holds the update lock), 2 the run could
not even check, 1 the update failed (the notice and the log carry the
rollback).

Idle sessions do not hold an update off: the board omits a session with no
activity in its active window, such a session keeps its shim runtime, and
its next call reconnects to the recreated daemon.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from pseudolife_memory import update_cli as _cli
from pseudolife_memory.update_cli import UpdateError, run_cli, version_key


def data_dir() -> Path:
    """``update_cli.data_dir`` read through the module, so one seam serves
    both (the tests point it at a temporary directory)."""
    return _cli.data_dir()

TASK_NAME = "Pseudolife Unattended Update"
UNIT_NAME = "pseudolife-update"
LOG_NAME = "unattended-update.log"
BOARD_LABEL = "unattended update"
NOTICE_LIMIT = 4000


# ── the board and the daemon ────────────────────────────────────────────────

def bearer() -> str | None:
    """The bearer this host's clients send, or ``None``."""
    from pseudolife_memory.credentials import CredentialProvider

    try:
        return CredentialProvider.from_environment().snapshot().token
    except Exception:  # noqa: BLE001 - a bad credential file reads as no bearer
        return None


def board_activity(url: str, token: str | None) -> tuple[list[dict], int, str]:
    """``(active peers, idle count, reason)`` for the board at ``url``: a
    non-empty ``reason`` means the board could not be read (no bearer, the
    board off, this principal not admitted, the daemon unreachable). A
    throwaway address is registered for the read and left to expire."""
    if not token:
        return [], 0, "no bearer token in this environment (PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKEN_FILE)"
    from pseudolife_memory.lease_cli import _Board, _Refused, _Transient

    board = _Board(url, token)
    try:
        board.register(task="update check", project="pseudolife-mcp", label=BOARD_LABEL,
                       status="checking whether a release can be applied")
        reply = board._post("agents", {"limit": 50})
    except (_Refused, _Transient) as exc:
        return [], 0, str(exc)
    finally:
        board.close()
    listed = reply.get("agents")
    if not isinstance(listed, list):
        # Fail closed: a reply without the list is not "nobody is active".
        return [], 0, "the daemon's agents reply was not understood"
    # This run's own throwaway address, and a previous run's within the
    # active window, are not sessions.
    agents = [a for a in listed if isinstance(a, dict) and isinstance(a.get("agent_id"), str)
              and a.get("label") != BOARD_LABEL]
    idle = reply.get("idle_omitted")
    return agents, int(idle) if isinstance(idle, int) else 0, ""


def post_json(url: str, token: str | None, body: dict, timeout: float = 10.0) -> dict | None:
    """POST ``body`` as JSON; the reply as a dict, or ``None`` when the
    daemon did not answer. A refusal (an HTTP error status) is the
    daemon's JSON error body with ``http_status`` added, so the caller can
    say why. A redirect is refused rather than followed."""
    from pseudolife_memory.daemon_url import _NoRedirectHandler

    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - the configured daemon URL
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            refusal = json.loads(exc.read().decode("utf-8"))
        except (OSError, ValueError):
            refusal = None
        error = refusal.get("error") if isinstance(refusal, dict) else None
        return {"error": error if isinstance(error, str) else f"HTTP {exc.code}", "http_status": exc.code}
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) else None


def post_notice(update, text: str) -> None:
    """A board notice from the daemon's reserved principal to every attached
    session; best effort, one step line either way."""
    text = text[:NOTICE_LIMIT]
    reply = post_json(update.o.daemon_url.rstrip("/") + "/api/daemon-notice", bearer(), {"text": text}) or {}
    recipients = reply.get("recipients")
    error = str(reply.get("error") or "")
    if isinstance(recipients, int):
        update.step(f"board notice posted from the daemon principal ({recipients} recipient(s))")
    elif error.startswith("principal_not_allowed"):
        update.step("board notice refused: this run's bearer principal is not listed in "
                    "coordination.daemon_notice_principals (empty by default; see the unattended-update section "
                    "of docs/guide/configuration.md); the log carries the same text")
    elif reply.get("reason") == "board_unavailable":
        update.step("board notice not posted: the board is off or has no Postgres; the log carries the same text")
    else:
        why = f"the daemon refused it: {error[:200]}" if error else "the daemon did not answer"
        update.step(f"board notice not posted ({why}); the log carries the same text")


# ── the run ─────────────────────────────────────────────────────────────────

def _describe(peers: list[dict]) -> str:
    names = []
    for peer in peers[:5]:
        label = str(peer.get("label") or peer.get("principal") or peer.get("agent_id", ""))[:40]
        task = str(peer.get("task") or "")[:60]
        names.append(f"{label}" + (f" ({task})" if task else ""))
    more = len(peers) - len(names)
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


def run_unattended(update) -> int:
    """The unattended run; see the module docstring for the exit codes.
    ``update`` is an ``update_cli.Update`` whose options came from
    ``--unattended``."""
    log_path = data_dir() / LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")
    original_log, original_warn = update._log, update._warn

    def tee(prefix: str, sink):
        def write(line: str) -> None:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {prefix}{line}\n")
            handle.flush()
            sink(line)
        return write

    update._log, update._warn = tee("", original_log), tee("WARNING: ", original_warn)
    try:
        return _run(update)
    except UpdateError as exc:
        # Into the log while it is still tee'd: run() prints the warning
        # after the tee is gone, and on a headless host the log is the record.
        update._log(f"stopped: {exc}")
        raise
    finally:
        update._log, update._warn = original_log, original_warn
        handle.close()


def _run(update) -> int:
    o = update.o
    update.step("unattended run: checking the daemon, the newest release and the board")
    tier = update.detect_tier()
    update.report.tier = tier
    if tier != "docker":
        raise UpdateError("unattended updates apply to the Docker tier (a daemon container); this is a pip "
                          "install, which `pseudolife-mcp update` upgrades when you run it. Nothing was changed", 2)
    health = update.daemon_health()
    if not health:
        raise UpdateError("the daemon did not answer /health; nothing was changed", 2)
    current = health.get("version") if isinstance(health.get("version"), str) else None
    update.report.current = current
    latest = o.tag or update.latest_release()
    update.report.target = latest
    if latest is None:
        raise UpdateError("could not read the newest release from PyPI; nothing was changed", 2)
    if not current or not version_key(current):
        raise UpdateError("the daemon's /health carries no version; nothing was changed", 2)
    if not (version_key(latest) and version_key(latest) > version_key(current)):
        update.step(f"current: {current} is the newest release; nothing to do")
        update.report.exit_code = 3
        return 3
    knob = (health.get("updates") or {}).get("unattended_daemon") is True
    if not knob:
        update.step(f"held off: release {latest} is available (the daemon runs {current}) but "
                    f"updates.unattended_daemon is off in the daemon's config.yaml. Run pseudolife-mcp update "
                    f"yourself, or set the knob for the next scheduled run")
        update.report.exit_code = 4
        return 4
    active, idle, reason = board_activity(o.daemon_url.rstrip("/"), bearer())
    if reason:
        text = (f"Pseudolife-MCP: release {latest} is available (the daemon runs {current}); the unattended "
                f"update held off because the board could not be read ({reason}), so it cannot tell whether a "
                f"session is working. Run pseudolife-mcp update yourself.")
        update.step(text)
        post_notice(update, text)
        update.report.exit_code = 4
        return 4
    if active:
        text = (f"Pseudolife-MCP: release {latest} is available (the daemon runs {current}); the unattended "
                f"update held off because {len(active)} session(s) are active on the board: {_describe(active)}"
                + (f" ({idle} idle, not counted)" if idle else "") +
                ". It tries again at the next scheduled run; run pseudolife-mcp update yourself to take it now.")
        update.step(text)
        post_notice(update, text)
        update.report.exit_code = 4
        return 4
    lock = _cli.UpdateLock()
    try:
        lock.acquire()
    except _cli.UpdateBusy as busy:
        text = (f"Pseudolife-MCP: release {latest} is available (the daemon runs {current}); the unattended "
                f"update held off because another pseudolife-mcp update is running (pid {busy.pid}). It tries "
                f"again at the next scheduled run.")
        update.step(text)
        post_notice(update, text)
        update.report.exit_code = 4
        return 4
    try:
        return _apply(update, latest, idle)
    finally:
        lock.release()


def _apply(update, latest: str, idle: int) -> int:
    """The update itself, under the update lock. The daemon's version is
    read again first: an attended update that finished between the first
    read and the lock has already moved it, and a rollback tag named for
    the old version would then hold the new image."""
    o = update.o
    health = update.daemon_health()
    if not health:
        raise UpdateError("the daemon did not answer /health after the update lock was taken; nothing was changed", 2)
    current = health.get("version") if isinstance(health.get("version"), str) else None
    if not current or not version_key(current):
        raise UpdateError("the daemon's /health carries no version; nothing was changed", 2)
    update.report.current = current
    if version_key(current) >= version_key(latest):
        update.step(f"current: the daemon already runs {current} (another update moved it); nothing to do")
        update.report.exit_code = 3
        return 3
    update.step(f"applying release {latest} unattended: no session is active on the board"
                + (f" ({idle} idle)" if idle else ""))
    def board_still_idle() -> None:
        # The backup can take minutes: a session that started meanwhile
        # must not get its daemon recreated under it. It runs before the
        # rollback tag moves, so a hold-off leaves only the backup behind.
        active, _idle, reason = board_activity(o.daemon_url.rstrip("/"), bearer())
        if reason or active:
            why = f"the board could not be read ({reason})" if reason else \
                f"{len(active)} session(s) became active during the backup: {_describe(active)}"
            text = (f"Pseudolife-MCP: the unattended update to {latest} held off after the backup because {why}; "
                    f"the daemon was not recreated. It tries again at the next scheduled run.")
            post_notice(update, text)
            raise UpdateError(text, 4)

    try:
        update.deploy_release(current=current, before_recreate=board_still_idle,
                              previous_digest=health.get("hooks_digest"))
    except UpdateError as exc:
        if exc.exit_code == 4:
            raise
        message = str(exc)
        rollback = "" if "to roll back" in message.lower() or not update.rollback_lines else \
            "\n".join(["", "To roll back:"] + update.rollback_lines)
        text = (f"Pseudolife-MCP: the unattended update to {latest} FAILED: {message}. The log is "
                f"{data_dir() / LOG_NAME}.{rollback}")
        update._log(text)          # into the log while it is still tee'd; run() prints the warning after
        post_notice(update, text)
        raise
    text = _updated_text(update, current, latest)
    update.step(text)
    post_notice(update, text)
    return 0


def _updated_text(update, current: str | None, latest: str) -> str:
    from pseudolife_memory import client_updates

    clients = update.report.clients or {}
    parts = []
    for key, label in (("shim", "shim"), ("plugin", "plugin cache")):
        state = (clients.get(key) or {}).get("state")
        if state:
            parts.append(f"{label} {state}")
    rollback = update.report.rollback.get("tag") if isinstance(update.report.rollback, dict) else None
    text = (f"Pseudolife-MCP: the daemon was updated unattended from {current} to {latest} (backup taken; "
            f"rollback tag {rollback or 'none'}; {', '.join(parts) or 'clients not moved'}). Start a new "
            f"session to run on the new shim runtime; the log is {data_dir() / LOG_NAME}.")
    codex = client_updates.codex_reapproval_text(clients.get("codex"))
    if codex:
        text += "\n" + codex
    return text


# ── the schedule ────────────────────────────────────────────────────────────

def _command(daemon_url: str | None = None) -> list[str]:
    """What the task or unit runs: the stable shim launcher when one is
    installed, else this interpreter's module; a daemon URL other than the
    default rides along, since the task or unit inherits no shell."""
    tail = ["update", "--unattended"]
    if daemon_url and daemon_url.rstrip("/") != _cli.DEFAULT_DAEMON_URL:
        tail += ["--daemon-url", daemon_url]
    try:
        from pseudolife_memory import runtimes
        launcher = runtimes.default_layout().launcher
        if launcher.is_file():
            return [str(launcher), *tail]
    except Exception:  # noqa: BLE001 - no runtime layout here
        pass
    return [sys.executable, "-m", "pseudolife_memory.cli", *tail]


def _at(when: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", when.strip())
    if not match:
        raise UpdateError(f"--schedule takes a time of day as HH:MM (got {when!r})", 2)
    return int(match.group(1)), int(match.group(2))


def _systemd_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _unit_environment() -> list[str]:
    """``Environment=`` lines so the timer's run finds the daemon and its
    bearer: a systemd --user unit inherits nothing from a shell. A token
    given only as PSEUDOLIFE_MCP_TOKEN is written to a private file, never
    into the unit."""
    lines = []
    url = os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL")
    if url:
        lines.append(f"Environment=PSEUDOLIFE_MCP_DAEMON_URL={url}")
    token_file = os.environ.get("PSEUDOLIFE_MCP_TOKEN_FILE")
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN")
    if not token_file and token:
        from pseudolife_memory.credentials import _write_token_file

        path = data_dir() / "unattended-update.token"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _write_token_file(path, token)      # owner-only, atomically, whatever was there before
        except Exception as exc:  # noqa: BLE001 - CredentialError and OSError alike: say it, exit 2
            raise UpdateError(f"could not write the private token file {path} ({exc}); nothing was installed", 2)
        token_file = str(path)
    if token_file:
        lines.append(f"Environment=PSEUDOLIFE_MCP_TOKEN_FILE={token_file}")
    return lines


def schedule(update, when: str, platform: str = sys.platform) -> int:
    hour, minute = _at(when)
    command = _command(getattr(update.o, "daemon_url", None))
    if platform.startswith("win"):
        quoted = " ".join(f'"{part}"' if " " in part else part for part in command)
        code, out = run_cli(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", f"{hour:02d}:{minute:02d}",
                             "/TN", TASK_NAME, "/TR", quoted])
        if code != 0:
            raise UpdateError(f"schtasks /Create failed ({out.strip() or code}). Task Scheduler refuses per-user "
                              f"registration from an unelevated administrator account: open an elevated "
                              f"PowerShell fresh from the Start menu (never from a shell inside a Store-packaged "
                              f"app) and run this command there once", 2)
        update.step(f"registered the scheduled task '{TASK_NAME}' (daily at {hour:02d}:{minute:02d}): {quoted}")
    elif platform.startswith("linux"):
        unit_dir = _systemd_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        exec_start = " ".join(f'"{part}"' if " " in part else part for part in command)
        service = "\n".join(["[Unit]", "Description=Pseudolife-MCP unattended daemon update", "",
                             "[Service]", "Type=oneshot", f"ExecStart={exec_start}",
                             # 3 = current, 4 = held off: not failures of the unit.
                             "SuccessExitStatus=3 4", *_unit_environment(), ""])
        timer = "\n".join(["[Unit]", "Description=Pseudolife-MCP unattended daemon update (daily)", "",
                           "[Timer]", f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00", "Persistent=true",
                           "RandomizedDelaySec=300", "", "[Install]", "WantedBy=timers.target", ""])
        (unit_dir / f"{UNIT_NAME}.service").write_text(service, encoding="utf-8")
        (unit_dir / f"{UNIT_NAME}.timer").write_text(timer, encoding="utf-8")
        for argv in (["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", "--now", f"{UNIT_NAME}.timer"]):
            code, out = run_cli(argv)
            if code != 0:
                raise UpdateError(f"{' '.join(argv)} failed ({out.strip() or code}); the unit files are under "
                                  f"{unit_dir}", 2)
        update.step(f"installed {UNIT_NAME}.timer under {unit_dir} (daily at {hour:02d}:{minute:02d}): {exec_start}")
    else:
        raise UpdateError("--schedule installs a Windows scheduled task or a systemd --user timer; on this "
                          "platform run `pseudolife-mcp update --unattended` from your own scheduler", 2)
    update.step("the run applies a release only while updates.unattended_daemon is on in the daemon's "
                "config.yaml (off by default) and no session is active on the board; the log is "
                f"{data_dir() / LOG_NAME}")
    update.step("its board notice needs its own principal: a PSEUDOLIFE_MCP_TOKENS entry token:<name> on the "
                "daemon, <name> in coordination.daemon_notice_principals and allowed_principals, and that token "
                "as this run's bearer; until then the notice is refused and the log is the only record")
    return 0


def unschedule(update, platform: str = sys.platform) -> int:
    if platform.startswith("win"):
        code, _out = run_cli(["schtasks", "/Query", "/TN", TASK_NAME])
        if code != 0:
            update.step(f"no scheduled task '{TASK_NAME}' is registered; nothing to remove")
            return 0
        code, out = run_cli(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
        if code != 0:
            raise UpdateError(f"schtasks /Delete failed ({out.strip() or code})", 2)
        update.step(f"removed the scheduled task '{TASK_NAME}'")
    elif platform.startswith("linux"):
        unit_dir = _systemd_dir()
        run_cli(["systemctl", "--user", "disable", "--now", f"{UNIT_NAME}.timer"])
        for name in (f"{UNIT_NAME}.timer", f"{UNIT_NAME}.service"):
            try:
                (unit_dir / name).unlink()
            except FileNotFoundError:
                pass
        run_cli(["systemctl", "--user", "daemon-reload"])
        try:
            (data_dir() / "unattended-update.token").unlink()
        except FileNotFoundError:
            pass
        update.step(f"removed {UNIT_NAME}.timer and its service from {unit_dir}")
    else:
        raise UpdateError("nothing to remove on this platform", 2)
    return 0
