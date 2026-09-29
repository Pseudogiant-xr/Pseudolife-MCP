"""``pseudolife-mcp update --unattended`` and ``--schedule``: opt-in
unattended daemon updates.

A scheduled task or systemd timer runs the check once a day. It applies a
new release only when ``updates.unattended_daemon`` is on (read from the
daemon's ``/health``, so the timer alone changes nothing) and the agent
board lists no active session, with the same backup and rollback tag as an
attended update, and posts a board notice from the daemon's principal
saying it updated or why it held off. The fake world of
tests/test_update_cli.py stands in for Docker, PyPI and the daemon; the
board and the notice go through seams.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pseudolife_memory import client_updates, unattended_update as uu, update_cli as up  # noqa: E402
from tests.test_update_cli import World, _project, clients, world  # noqa: E402,F401

REAL_COMMAND = uu._command          # the scheduler fixture replaces it; one test wants the real one


@pytest.fixture
def board(monkeypatch, tmp_path):
    """The board (active peers, idle count, reason) and the notices posted."""
    state = {"active": [], "idle": 0, "reason": "", "notices": [], "reply": {"recipients": 2}}
    monkeypatch.setattr(uu, "board_activity", lambda url, token: (state["active"], state["idle"], state["reason"]))
    monkeypatch.setattr(uu, "bearer", lambda: "tok")

    def post_json(url, token, body, timeout=10.0):
        state["notices"].append((url, token, body["text"]))
        return state["reply"]

    monkeypatch.setattr(uu, "post_json", post_json)
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "data")
    return state


def _daemon(world, tmp_path, *, knob=True, version="0.15.0", reads=1):
    """``reads``: how many /health reads answer ``version`` before any
    answers a test appends (2 for a run that takes the update lock and
    reads the daemon's version again)."""
    _project(world, tmp_path, version=version)
    world.health = [{"status": "ok", "version": version, "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64,
                     "updates": {"unattended_daemon": knob, "unattended_clients": False}}] * reads


def _log(tmp_path) -> str:
    return (tmp_path / "data" / uu.LOG_NAME).read_text(encoding="utf-8")


# ── the run ─────────────────────────────────────────────────────────────────

def test_a_pip_install_cannot_be_updated_unattended(world, board, capsys):
    assert up.main(["--unattended"]) == 2
    assert "Docker tier" in capsys.readouterr().err
    assert board["notices"] == []


def test_current_means_nothing_to_do_and_no_notice(world, board, tmp_path, capsys):
    _daemon(world, tmp_path)
    world.pypi = "0.15.0"
    assert up.main(["--unattended"]) == 3
    assert "nothing to do" in capsys.readouterr().out
    assert board["notices"] == [] and not any("pull" in c for c in world.docker_calls())
    assert "nothing to do" in _log(tmp_path)


def test_the_knob_off_holds_off_without_a_notice(world, board, tmp_path, capsys):
    _daemon(world, tmp_path, knob=False)
    assert up.main(["--unattended"]) == 4
    out = capsys.readouterr().out
    assert "held off" in out and "updates.unattended_daemon is off" in out
    assert board["notices"] == [] and not any("pull" in c for c in world.docker_calls())


def test_an_active_session_holds_off_and_is_told(world, board, tmp_path, capsys):
    _daemon(world, tmp_path)
    board["active"] = [{"agent_id": "a1", "label": "claude-code", "task": "reviewing PR 461"}]
    board["idle"] = 3
    assert up.main(["--unattended"]) == 4
    assert not any("pull" in c for c in world.docker_calls())
    (url, token, text), = board["notices"]
    assert url.endswith("/api/daemon-notice") and token == "tok"
    assert text.startswith("Pseudolife-MCP: release 0.15.1 is available")
    assert "held off because 1 session(s) are active" in text and "claude-code (reviewing PR 461)" in text
    assert "(3 idle, not counted)" in text and "pseudolife-mcp update" in text
    assert "board notice posted" in capsys.readouterr().out
    assert "held off" in _log(tmp_path)


def test_flags_that_skip_a_safety_step_are_refused_unattended(world, board, tmp_path, capsys):
    """The scheduled command passes none of these; a hand-run must not skip
    the backup or half the update and then report a full one."""
    _daemon(world, tmp_path)
    for flag in ("--no-backup", "--clients-only", "--daemon-only", "--force-rollback-tag", "--allow-downgrade",
                 "--reinstall", "--check"):
        assert up.main(["--unattended", flag]) == 2, flag
        assert "cannot be combined with --unattended" in capsys.readouterr().err
    assert board["notices"] == [] and not any("pull" in c for c in world.docker_calls())


def test_a_session_that_starts_during_the_backup_holds_the_recreate_off(world, board, clients, tmp_path):
    """The board is read again right before the daemon is recreated: the
    backup can take minutes."""
    _daemon(world, tmp_path, reads=2)
    world.images[f"{up.GHCR_IMAGE}:0.15.1"] = "sha256:new"
    reads = []

    def activity(url, token):
        reads.append(url)
        return ([{"agent_id": "late", "label": "codex", "task": "started during the backup"}] if len(reads) > 1
                else [], 0, "")

    board["active"] = []
    import pseudolife_memory.unattended_update as uu_module
    original = uu_module.board_activity
    uu_module.board_activity = activity
    try:
        assert up.main(["--unattended", "--health-delay-ms", "1"]) == 4
    finally:
        uu_module.board_activity = original
    assert len(reads) == 2
    calls = world.docker_calls()
    assert any("pg_dump" in c for c in calls)                       # the backup ran
    assert not any(c.startswith("tag ") for c in calls)             # the rollback tag did not move
    assert not any("up -d --no-deps pseudolife-daemon" in c for c in calls)   # the recreate did not
    (_, _, text), = board["notices"]
    assert "held off after the backup" in text and "codex (started during the backup)" in text
    assert "the daemon was not recreated" in text


def test_an_exit_2_reason_reaches_the_log(world, board, tmp_path):
    _daemon(world, tmp_path)
    world.pypi = None
    assert up.main(["--unattended"]) == 2
    assert "could not read the newest release from PyPI" in _log(tmp_path)


def test_a_missing_daemon_version_is_an_error_not_current(world, board, tmp_path):
    _project(world, tmp_path)
    world.health = [{"status": "ok"}]
    assert up.main(["--unattended"]) == 2


def test_an_unreadable_board_holds_off(world, board, tmp_path):
    _daemon(world, tmp_path)
    board["reason"] = "no bearer token in this environment"
    assert up.main(["--unattended"]) == 4
    (_, _, text), = board["notices"]
    assert "board could not be read (no bearer token" in text
    assert not any("pull" in c for c in world.docker_calls())


def test_an_idle_board_applies_the_release_with_backup_and_rollback_then_tells(world, board, clients, tmp_path, capsys):
    _daemon(world, tmp_path, reads=2)
    world.health.append({"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64})
    world.images[f"{up.GHCR_IMAGE}:0.15.1"] = "sha256:new"
    assert up.main(["--unattended", "--health-delay-ms", "1"]) == 0
    calls = world.docker_calls()
    pull = next(i for i, c in enumerate(calls) if c.startswith(f"pull {up.GHCR_IMAGE}:0.15.1"))
    dump = next(i for i, c in enumerate(calls) if "pg_dump" in c)
    tag = next(i for i, c in enumerate(calls) if c.startswith("tag ") and "pre-update" in c)
    recreate = next(i for i, c in enumerate(calls) if "up -d --no-deps pseudolife-daemon" in c)
    assert pull < dump < tag < recreate
    assert clients and clients[0]["source"] == "pseudolife-mcp==0.15.1"
    (_, _, text), = board["notices"]
    assert text.startswith("Pseudolife-MCP: the daemon was updated unattended from 0.15.0 to 0.15.1")
    assert "rollback tag" in text and "pre-update" in text and "shim installed:0.15.1" in text
    assert "Codex" not in text                                    # hooks unchanged: nothing about Codex
    log = _log(tmp_path)
    assert "applying release 0.15.1 unattended" in log and "updated unattended" in log


def test_the_notice_carries_the_codex_steps_when_the_hooks_changed(world, board, tmp_path, monkeypatch):
    _daemon(world, tmp_path, reads=2)
    world.health.append({"status": "ok", "version": "0.15.1"})
    world.images[f"{up.GHCR_IMAGE}:0.15.1"] = "sha256:new"
    monkeypatch.setattr(client_updates, "run_steps", lambda steps, **kw: {
        "shim": {"state": "installed:0.15.1", "detail": "ok"}, "plugin": {"state": "refreshed:0.15.1", "detail": "ok"},
        "codex": {"state": "stale", "changed_files": ["session-start.sh"], "detail": "stale"}, "ok": True})
    assert up.main(["--unattended", "--health-delay-ms", "1"]) == 0
    (_, _, text), = board["notices"]
    assert "updated unattended" in text
    assert "Codex: its hook copy needs re-approval (changed: session-start.sh)" in text and "/hooks" in text


def test_a_failed_update_is_told_with_the_rollback(world, board, clients, tmp_path, capsys):
    _daemon(world, tmp_path, reads=2)
    world.images[f"{up.GHCR_IMAGE}:0.15.1"] = "sha256:new"
    world.health.append(None)                                    # the daemon never comes back
    assert up.main(["--unattended", "--health-retries", "1", "--health-delay-ms", "1"]) == 1
    (_, _, text), = board["notices"]
    assert text.startswith("Pseudolife-MCP: the unattended update to 0.15.1 FAILED")
    assert text.count("PSEUDOLIFE_IMAGE_TAG=") == 2                # the rollback lines once (pwsh + sh forms)
    assert "FAILED" in _log(tmp_path)


def test_a_pinned_tag_and_the_json_report(world, board, clients, tmp_path, capsys):
    _daemon(world, tmp_path, reads=2)
    world.health.append({"status": "ok", "version": "0.15.2"})
    world.images[f"{up.GHCR_IMAGE}:0.15.2"] = "sha256:new"
    assert up.main(["--unattended", "--tag", "0.15.2", "--health-delay-ms", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "unattended" and report["target"] == "0.15.2" and report["exit_code"] == 0


def test_the_notice_not_taken_is_said_and_never_fatal(world, board, tmp_path, capsys):
    _daemon(world, tmp_path)
    board["active"] = [{"agent_id": "a1", "label": "x"}]
    board["reply"] = None
    assert up.main(["--unattended"]) == 4
    assert "board notice not posted" in capsys.readouterr().out


def test_the_notice_refused_says_which_key_admits_it(world, board, tmp_path, capsys):
    """A daemon notice needs a principal listed in
    coordination.daemon_notice_principals: the run says so, and goes on."""
    _daemon(world, tmp_path)
    board["active"] = [{"agent_id": "a1", "label": "x"}]
    board["reply"] = {"error": "principal_not_allowed: a daemon notice needs ..."}
    assert up.main(["--unattended"]) == 4
    out = capsys.readouterr().out
    assert "board notice refused" in out and "coordination.daemon_notice_principals" in out
    board["reply"] = {"recipients": None, "reason": "board_unavailable"}
    assert up.main(["--unattended"]) == 4
    assert "board notice not posted: the board is off or has no Postgres" in capsys.readouterr().out


def test_post_json_returns_a_refusals_body(monkeypatch):
    """The daemon's 400 carries the reason; the notice line names it."""
    import io
    import urllib.error

    class Opener:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                         io.BytesIO(b'{"error": "principal_not_allowed: no"}'))

    monkeypatch.setattr(uu.urllib.request, "build_opener", lambda *handlers: Opener())
    assert uu.post_json("http://127.0.0.1:8765/api/daemon-notice", "tok", {"text": "x"}) == \
        {"error": "principal_not_allowed: no", "http_status": 400}


# ── the update lock ─────────────────────────────────────────────────────────

def test_another_update_holding_the_lock_holds_the_unattended_run_off(world, board, clients, tmp_path, capsys):
    from pseudolife_memory import os_lock

    _daemon(world, tmp_path)
    world.images[f"{up.GHCR_IMAGE}:0.15.1"] = "sha256:new"
    lock = os_lock.OsLock(tmp_path / "data" / up.LOCK_NAME)
    assert lock.acquire()
    (tmp_path / "data" / up.LOCK_HOLDER_NAME).write_text("4242\n", encoding="utf-8")
    try:
        assert up.main(["--unattended", "--health-delay-ms", "1"]) == 4
    finally:
        lock.release()
    assert not any(c.startswith(("pull", "tag", "compose", "exec")) for c in world.docker_calls())
    assert clients == []
    (_, _, text), = board["notices"]
    assert "held off because another pseudolife-mcp update is running (pid 4242)" in text
    assert "another pseudolife-mcp update is running" in _log(tmp_path)


def test_a_version_that_moved_before_the_lock_is_nothing_to_do(world, board, clients, tmp_path, capsys):
    """An attended update that finished between the first /health read and
    the lock: the daemon already runs the release, so nothing is tagged
    (a rollback tag named for the old version would hold the new image)."""
    _daemon(world, tmp_path)
    world.health.append({"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0,
                         "hooks_digest": "e" * 64, "updates": {"unattended_daemon": True}})
    world.images[f"{up.GHCR_IMAGE}:0.15.1"] = "sha256:new"
    assert up.main(["--unattended", "--health-delay-ms", "1"]) == 3
    assert not any(c.startswith(("pull", "tag", "compose", "exec")) for c in world.docker_calls())
    assert "already runs 0.15.1" in capsys.readouterr().out and board["notices"] == []


def test_the_unattended_run_releases_the_lock_on_every_exit_path(world, board, clients, tmp_path, monkeypatch):
    from pseudolife_memory import os_lock

    path = tmp_path / "data" / up.LOCK_NAME
    # release() removes the pid file; the probe alone cannot tell, since a
    # collected handle drops the OS lock too
    holder = tmp_path / "data" / up.LOCK_HOLDER_NAME
    _daemon(world, tmp_path, reads=2)
    world.health.append({"status": "ok", "version": "0.15.1"})
    world.images[f"{up.GHCR_IMAGE}:0.15.1"] = "sha256:new"
    assert up.main(["--unattended", "--health-delay-ms", "1"]) == 0
    assert os_lock.probe(path) is False and not holder.exists()
    _daemon(world, tmp_path, reads=2)
    world.pull_rc = 1
    assert up.main(["--unattended"]) == 1
    assert os_lock.probe(path) is False and not holder.exists()
    world.pull_rc = 0
    _daemon(world, tmp_path, reads=2)
    monkeypatch.setattr(up.Update, "deploy_release", lambda self, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        up.main(["--unattended"])
    assert os_lock.probe(path) is False and not holder.exists()


# ── the board read ──────────────────────────────────────────────────────────

def test_board_activity_registers_a_throwaway_address_and_reads_the_page(monkeypatch):
    posted = []

    class FakeBoard:
        def __init__(self, url, token):
            self.url, self.token = url, token

        def register(self, **kw):
            posted.append(("register", kw))

        def _post(self, action, body, **kw):
            posted.append((action, body))
            return {"agents": [{"agent_id": "a1", "label": "codex"}, {"nope": 1}], "idle_omitted": 4}

        def close(self):
            posted.append(("close", None))

    from pseudolife_memory import lease_cli
    monkeypatch.setattr(lease_cli, "_Board", FakeBoard)
    active, idle, reason = uu.board_activity("http://127.0.0.1:8765", "tok")
    assert [a["agent_id"] for a in active] == ["a1"] and idle == 4 and reason == ""
    assert posted[0][0] == "register" and posted[0][1]["label"] == uu.BOARD_LABEL
    assert posted[-1] == ("close", None)
    assert uu.board_activity("http://127.0.0.1:8765", None)[2].startswith("no bearer token")


def test_board_activity_fails_closed_and_ignores_its_own_address(monkeypatch):
    class FakeBoard:
        reply = {"agents": [{"agent_id": "me", "label": uu.BOARD_LABEL}], "idle_omitted": 0}

        def __init__(self, url, token):
            pass

        def register(self, **kw):
            pass

        def _post(self, action, body, **kw):
            return self.reply

        def close(self):
            pass

    from pseudolife_memory import lease_cli
    monkeypatch.setattr(lease_cli, "_Board", FakeBoard)
    active, _, reason = uu.board_activity("http://127.0.0.1:8765", "tok")
    assert active == [] and reason == ""                          # a previous run's address is not a session
    FakeBoard.reply = {"ok": True}
    active, _, reason = uu.board_activity("http://127.0.0.1:8765", "tok")
    assert active == [] and "not understood" in reason            # no list: fail closed, never "idle"


# ── the schedule ────────────────────────────────────────────────────────────

@pytest.fixture
def scheduler(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(uu, "run_cli", lambda argv, **kw: (calls.append([str(a) for a in argv]) or (0, "")))
    monkeypatch.setattr(uu, "_command",
                        lambda daemon_url=None: [str(tmp_path / "bin" / "pseudolife-mcp"), "update", "--unattended"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "data")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DAEMON_URL", raising=False)
    return calls


def _update():
    return up.Update(up.Options(schedule="03:30"))


def test_schedule_registers_a_daily_windows_task(scheduler, tmp_path, capsys):
    assert uu.schedule(_update(), "03:30", platform="win32") == 0
    (argv,) = scheduler
    assert argv[:2] == ["schtasks", "/Create"] and "/SC" in argv and argv[argv.index("/SC") + 1] == "DAILY"
    assert argv[argv.index("/ST") + 1] == "03:30" and argv[argv.index("/TN") + 1] == uu.TASK_NAME
    assert argv[argv.index("/TR") + 1].endswith("update --unattended")
    out = capsys.readouterr().out
    assert "updates.unattended_daemon" in out
    notice = [line for line in out.splitlines() if "coordination.daemon_notice_principals" in line]
    assert len(notice) == 1 and "PSEUDOLIFE_MCP_TOKENS" in notice[0]


def test_schedule_writes_a_systemd_timer_with_the_bearer_file(scheduler, tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "secret-token")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:8765")
    assert uu.schedule(_update(), "3:05", platform="linux") == 0
    unit_dir = tmp_path / "xdg" / "systemd" / "user"
    service = (unit_dir / "pseudolife-update.service").read_text(encoding="utf-8")
    timer = (unit_dir / "pseudolife-update.timer").read_text(encoding="utf-8")
    assert "Type=oneshot" in service and "update --unattended" in service
    assert "Environment=PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765" in service
    token_file = tmp_path / "data" / "unattended-update.token"
    assert f"Environment=PSEUDOLIFE_MCP_TOKEN_FILE={token_file}" in service
    assert "secret-token" not in service and token_file.read_text(encoding="utf-8") == "secret-token"
    if os.name != "nt":
        assert oct(token_file.stat().st_mode & 0o777) == "0o600"
    assert "SuccessExitStatus=3 4" in service                     # current / held off are not unit failures
    assert "OnCalendar=*-*-* 03:05:00" in timer and "Persistent=true" in timer
    assert scheduler == [["systemctl", "--user", "daemon-reload"],
                         ["systemctl", "--user", "enable", "--now", "pseudolife-update.timer"]]


def test_a_daemon_url_rides_into_the_scheduled_command(scheduler, tmp_path, monkeypatch):
    monkeypatch.setattr(uu, "_command", REAL_COMMAND)
    monkeypatch.setattr(uu.sys, "executable", str(tmp_path / "py"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_LAUNCHER", str(tmp_path / "no-launcher"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(tmp_path / "rt"))
    assert uu._command("http://127.0.0.1:8765") == [str(tmp_path / "py"), "-m", "pseudolife_memory.cli", "update", "--unattended"]
    assert uu._command("http://10.0.0.7:8765")[-2:] == ["--daemon-url", "http://10.0.0.7:8765"]


def test_unschedule_on_windows_is_idempotent(scheduler, monkeypatch):
    monkeypatch.setattr(uu, "run_cli", lambda argv, **kw: (1, "ERROR: The system cannot find the file specified.")
                        if argv[:2] == ["schtasks", "/Query"] else (0, ""))
    assert uu.unschedule(_update(), platform="win32") == 0


def test_schedule_refuses_a_bad_time_and_an_unsupported_platform(scheduler):
    with pytest.raises(up.UpdateError, match="HH:MM"):
        uu.schedule(_update(), "3pm", platform="linux")
    with pytest.raises(up.UpdateError, match="scheduler"):
        uu.schedule(_update(), "03:30", platform="darwin")
    assert scheduler == []


def test_a_refused_task_registration_names_the_elevated_shell(scheduler, monkeypatch):
    monkeypatch.setattr(uu, "run_cli", lambda argv, **kw: (1, "ERROR: Access is denied."))
    with pytest.raises(up.UpdateError, match="elevated PowerShell"):
        uu.schedule(_update(), "03:30", platform="win32")


def test_unschedule_removes_the_task_or_the_timer(scheduler, tmp_path):
    uu.schedule(_update(), "03:30", platform="linux")
    scheduler.clear()
    assert uu.unschedule(_update(), platform="linux") == 0
    unit_dir = tmp_path / "xdg" / "systemd" / "user"
    assert not (unit_dir / "pseudolife-update.timer").exists() and not (unit_dir / "pseudolife-update.service").exists()
    assert scheduler[0] == ["systemctl", "--user", "disable", "--now", "pseudolife-update.timer"]
    scheduler.clear()
    assert uu.unschedule(_update(), platform="win32") == 0
    assert scheduler == [["schtasks", "/Query", "/TN", uu.TASK_NAME], ["schtasks", "/Delete", "/F", "/TN", uu.TASK_NAME]]


# ── the route ───────────────────────────────────────────────────────────────

def test_the_daemon_notice_route_admits_only_a_listed_notice_principal(monkeypatch):
    """The reserved daemon sender is otherwise unforgeable: the route admits
    only a principal named in coordination.daemon_notice_principals (empty
    by default), never the board's ordinary ``default`` principal, and
    stamps the notice with the principal."""
    from pseudolife_memory import coordination
    from pseudolife_memory.web.api import build_console_app
    from pseudolife_memory.web.fixtures import FixtureService
    from tests.asgi_helpers import call, stub_mcp

    svc = FixtureService()
    monkeypatch.setattr(svc.config.coordination, "daemon_notice_principals", [])
    sent = []
    monkeypatch.setattr(coordination, "daemon_notice", lambda service, text: sent.append(text) or {"recipients": 2})
    app = build_console_app(stub_mcp, "secret", lambda: {"status": "ok"}, svc)
    body = json.dumps({"text": "Pseudolife-MCP: updated"}).encode()
    headers = [(b"content-type", b"application/json"), (b"authorization", b"Bearer secret")]
    # no principal resolver on the stand-in: refused
    st, reply = call(app, "POST", "/api/daemon-notice", body=body, headers=headers)
    assert st == 400 and b"principal_not_allowed" in reply and sent == []
    # every ordinary session's principal: refused while the list is empty, and the refusal names the key
    svc._request_principal = lambda: "default"
    st, reply = call(app, "POST", "/api/daemon-notice", body=body, headers=headers)
    assert st == 400 and b"principal_not_allowed" in reply and sent == []
    assert b"coordination.daemon_notice_principals" in reply and b"PSEUDOLIFE_MCP_TOKENS" in reply
    monkeypatch.setattr(svc.config.coordination, "daemon_notice_principals", ["updater", "daemon"])
    st, reply = call(app, "POST", "/api/daemon-notice", body=body, headers=headers)
    assert st == 400 and b"principal_not_allowed" in reply and sent == []   # default is still not listed
    svc._request_principal = lambda: "updater"
    st, reply = call(app, "POST", "/api/daemon-notice", body=body, headers=headers)
    assert st == 200 and json.loads(reply)["recipients"] == 2
    assert sent == ["Pseudolife-MCP: updated\n(posted by the unattended updater, principal updater)"]
    for who in ("intruder", "default", "daemon"):                 # the reserved name is never a caller, even listed
        svc._request_principal = lambda who=who: who
        st, reply = call(app, "POST", "/api/daemon-notice", body=body, headers=headers)
        assert st == 400 and b"principal_not_allowed" in reply, who
    svc._request_principal = lambda: "updater"
    st, reply = call(app, "POST", "/api/daemon-notice", body=json.dumps({"text": "x\x07y"}).encode(), headers=headers)
    assert st == 400 and b"printable" in reply
    st, reply = call(app, "POST", "/api/daemon-notice", body=json.dumps({"text": "y" * 4001}).encode(), headers=headers)
    assert st == 400
    monkeypatch.setattr(coordination, "daemon_notice", lambda service, text: None)
    st, reply = call(app, "POST", "/api/daemon-notice", body=body, headers=headers)
    assert st == 200 and json.loads(reply) == {"recipients": None, "reason": "board_unavailable"}


def test_the_cli_routes_the_flags(scheduler, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(uu, "schedule", lambda update, when, platform=None: seen.append(("schedule", when)) or 0)
    monkeypatch.setattr(uu, "unschedule", lambda update, platform=None: seen.append(("unschedule", None)) or 0)
    assert up.main(["--schedule", "04:00"]) == 0
    assert up.main(["--unschedule"]) == 0
    assert seen == [("schedule", "04:00"), ("unschedule", None)]
