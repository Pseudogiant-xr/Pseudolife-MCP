"""The release-mode client step finishes in one run: the updater that
installs a new shim runtime hands the steps after the shim (the plugin
cache, the Codex hooks, saved tunnels) to that runtime's own code
(``update_cli.Update._hand_off``), instead of running them with the code of
the release being replaced. Before this, behaviour new in a release's client
step ran only on a second ``pseudolife-mcp update --clients-only`` (0.17.0's
marketplace move to HTTPS did nothing on the first run).

The seams: ``client_updates.update_shim`` (the shim step), ``update_cli.run_cli``
(the capability probe of the new runtime) and ``update_cli.run_handoff`` (the
handed-off run), so nothing here installs or starts a runtime; the last test
runs ``run_handoff`` itself against this interpreter.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pseudolife_memory import __version__, client_updates, update_cli as up  # noqa: E402

TARGET = "99.0.0"     # never this checkout's own version
SOURCE = f"pseudolife-mcp=={TARGET}"
HEALTH = {"status": "ok", "version": TARGET, "hooks_digest": "d" * 64}


class Side:
    """The client side as a table: what the shim step reports, what the
    new runtime's probe answers, what the handed-off run does, and which
    steps ran in this process."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.runtime = tmp_path / "rt" / "000002"
        python = self.runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        self.python = python
        self.shim = {"state": f"installed:{TARGET}", "detail": "runtime 000002 installed",
                     "runtime": str(self.runtime)}
        self.probe = (0, f"{TARGET}\n{up.CLIENT_HANDOFF}\n")
        self.child_code = 0
        self.child_report: dict | None = {
            "tier": "docker", "mode": "release", "steps": ["updating the client side...", "child step"],
            "clients": {"shim": {"state": f"current:{TARGET}", "detail": "already installed"},
                        "plugin": {"state": "refreshed:abc", "detail": "new code refreshed it"},
                        "codex": {"state": "current", "detail": "ok"}, "ok": True},
            "codex_reapproval": "", "ok": True, "exit_code": 0}
        self.child_lines = ["==> continuing", "  [x] Plugin refreshed:abc - new code refreshed it"]
        self.in_process: list[str] = []
        self.probes: list[list[str]] = []
        self.handoffs: list[dict] = []

    def update_shim(self, source, repo=None, reinstall=False):
        self.in_process.append("shim")
        return dict(self.shim)

    def update_plugin(self, repo=None):
        self.in_process.append("plugin")
        return {"state": "current:old", "detail": "old code"}

    def check_codex_hooks(self, repo=None, daemon_digest=None, refresh=False):
        self.in_process.append("codex")
        return {"state": "current", "detail": "old code"}

    def run_cli(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.probes.append(argv)
        return self.probe

    def run_handoff(self, argv, *, cwd, env, relay):
        self.handoffs.append({"argv": [str(a) for a in argv], "cwd": cwd, "env": dict(env)})
        if self.child_report is not None:
            Path(env[up.HANDOFF_REPORT_ENV]).write_text(json.dumps(self.child_report), encoding="utf-8")
        if relay is not None:
            for line in self.child_lines:
                relay(line)
        return self.child_code, ""


@pytest.fixture
def side(tmp_path, monkeypatch):
    s = Side(tmp_path)
    monkeypatch.setattr(client_updates, "update_shim", s.update_shim)
    monkeypatch.setattr(client_updates, "update_plugin", s.update_plugin)
    monkeypatch.setattr(client_updates, "check_codex_hooks", s.check_codex_hooks)
    monkeypatch.setattr(up, "run_cli", s.run_cli)
    monkeypatch.setattr(up, "run_handoff", s.run_handoff, raising=False)
    from pseudolife_memory import tunnel_profiles
    monkeypatch.setattr(tunnel_profiles, "default_root", lambda: tmp_path / "no-tunnels")
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(tmp_path / "rt"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_LAUNCHER",
                       str(tmp_path / "bin" / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")))
    monkeypatch.delenv(up.HANDOFF_ENV if hasattr(up, "HANDOFF_ENV") else "PSEUDOLIFE_UPDATE_HANDOFF", raising=False)
    return s


def _update(**options) -> up.Update:
    lines: list[str] = []
    update = up.Update(up.Options(**options), log=lines.append, warn=lambda t: lines.append(f"WARNING: {t}"))
    update.lines = lines
    update._relay = lines.append
    return update


# ── the hand-off ────────────────────────────────────────────────────────────

def test_a_new_runtime_runs_the_steps_after_the_shim_with_its_own_code(side, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))          # a checkout on the path must not reach the child
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:3128")
    update = _update(clients_only=True, tag=TARGET, daemon_url="http://10.0.0.5:8765", reinstall=True)
    update.clients(None, SOURCE, HEALTH)

    assert side.in_process == ["shim"], "the old code ran a step after the shim itself"
    assert len(side.handoffs) == 1
    call = side.handoffs[0]
    argv = call["argv"]
    assert argv[0] == str(side.python) and argv[1:4] == ["-m", "pseudolife_memory.cli", "update"]
    assert "--clients-only" in argv and argv[argv.index("--tag") + 1] == TARGET
    assert argv[argv.index("--daemon-url") + 1] == "http://10.0.0.5:8765"
    # The shim step already ran here: the child must not install a third runtime,
    # and the result file is this run's to write.
    assert "--reinstall" not in argv and "--result-file" not in argv
    assert Path(call["cwd"]) == side.runtime
    env = call["env"]
    assert env[up.HANDOFF_ENV] == str(os.getpid())
    assert "PYTHONPATH" not in env and env["HTTPS_PROXY"] == "http://proxy.example.com:3128"
    # The probe imports nothing from the working directory or PYTHONPATH.
    assert side.probes and side.probes[0][:2] == [str(side.python), "-I"]

    # The child's result is this run's result; the child's ladder was its own output.
    assert update.report.ok is True and update.report.exit_code == 0
    assert update.report.clients["plugin"]["state"] == "refreshed:abc"
    assert update.report.clients["shim"]["state"] == f"installed:{TARGET}", "the install this run did is reported"
    assert "  [x] Plugin refreshed:abc - new code refreshed it" in update.lines
    assert any("runtime 000002" in line and TARGET in line for line in update.lines)


def test_the_child_s_codex_steps_reach_this_run_s_result_file(side, tmp_path):
    side.child_report["codex_reapproval"] = "Codex: approve the hooks again (/hooks)"
    result = tmp_path / "out" / "update.result"
    update = _update(clients_only=True, tag=TARGET, result_file=result)
    update.clients(None, SOURCE, HEALTH)
    assert update.report.codex_reapproval == "Codex: approve the hooks again (/hooks)"


def test_json_mode_hands_off_without_printing_the_child(side):
    update = _update(clients_only=True, tag=TARGET, as_json=True)
    update.clients(None, SOURCE, HEALTH)
    assert side.handoffs and update.lines == []
    assert update.report.clients["plugin"]["state"] == "refreshed:abc"
    assert "child step" in update.report.steps


def test_a_downgrade_forwards_allow_downgrade(side):
    update = _update(clients_only=True, tag=TARGET, allow_downgrade=True)
    update.clients(None, SOURCE, HEALTH)
    assert "--allow-downgrade" in side.handoffs[0]["argv"]


# ── near misses: no hand-off ────────────────────────────────────────────────

def test_the_same_version_is_finished_here_without_a_hand_off(side):
    """--reinstall at this run's own version installs a runtime of the same
    code: nothing to hand to, so the steps run here as before."""
    side.shim = {"state": f"installed:{__version__}", "detail": "reinstalled", "runtime": str(side.runtime)}
    update = _update(clients_only=True, tag=__version__, reinstall=True)
    update.clients(None, f"pseudolife-mcp=={__version__}", HEALTH)
    assert side.handoffs == [] and side.probes == []
    assert side.in_process == ["shim", "plugin", "codex"]
    assert update.report.ok is True


def test_a_failed_shim_step_has_nothing_to_hand_to(side):
    side.shim = {"state": "failed", "detail": "pip failed"}
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert side.handoffs == [] and side.in_process == ["shim", "plugin", "codex"]
    assert update.report.exit_code == up.CLIENT_STEP_FAILED


def test_a_checkout_deploy_never_hands_off(side):
    update = _update(all_clients=True)
    update.clients(side.tmp, str(side.tmp), HEALTH)
    assert side.handoffs == [] and side.probes == []


def test_a_handed_off_run_never_hands_off_again(side):
    """The marker: the child's own shim step finds a runtime of another
    version than this process only when something is wrong, and it still
    must not start a third process."""
    update = _update(clients_only=True, tag=TARGET, handoff_from="4242")
    update.clients(None, SOURCE, HEALTH)
    assert side.handoffs == [] and side.probes == []
    assert side.in_process == ["shim", "plugin", "codex"]


def test_main_takes_the_marker_out_of_the_environment(side, monkeypatch):
    """Read once and removed, so nothing the child starts (pip, claude, a
    tunnel refresh) inherits it."""
    seen = {}

    def run(self):
        seen["from"], seen["report"] = self.o.handoff_from, self.o.handoff_report
        seen["env"] = os.environ.get(up.HANDOFF_ENV), os.environ.get(up.HANDOFF_REPORT_ENV)
        return 0

    monkeypatch.setattr(up.Update, "run", run)
    monkeypatch.setenv(up.HANDOFF_ENV, "4242")
    monkeypatch.setenv(up.HANDOFF_REPORT_ENV, str(side.tmp / "r.json"))
    assert up.main(["--clients-only", "--tag", TARGET]) == 0
    assert seen == {"from": "4242", "report": side.tmp / "r.json", "env": (None, None)}


def test_an_older_target_without_the_hand_off_is_finished_here(side):
    """A target that predates the hand-off (a downgrade, a client machine
    following an older daemon) would refuse the marker's lock: this run's
    code finishes, as before, and says so in one line."""
    side.probe = (0, f"{TARGET}\n\n")
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert side.handoffs == [] and side.in_process == ["shim", "plugin", "codex"]
    assert update.report.ok is True
    assert any("predates" in line for line in update.lines)


# ── failures ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", [up.CLIENT_STEP_FAILED, 2, 1])
def test_a_failing_child_fails_this_run_with_its_exit_code(side, code):
    side.child_code = code
    side.child_report.update(ok=False, exit_code=code)
    side.child_report["clients"]["plugin"] = {"state": "failed", "detail": "marketplace refused"}
    side.child_report["clients"]["ok"] = False
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert update.report.ok is False and update.report.exit_code == code
    assert side.in_process == ["shim"], "a failed child must not be retried with the old code"


def test_a_failing_child_after_a_daemon_deploy_is_a_client_step_failure(side):
    """The daemon moved; whatever the child's code, the run's code says the
    client half failed after it (5), as an in-process failure does."""
    side.child_code = 2
    side.child_report.update(ok=False, exit_code=2)
    update = _update(tag=TARGET)
    update.report.tier = "docker"
    update.clients(None, SOURCE, HEALTH)
    assert update.report.ok is False and update.report.exit_code == up.CLIENT_STEP_FAILED


@pytest.mark.parametrize("probe", [(1, "ModuleNotFoundError: No module named 'pseudolife_memory'"),
                                   (124, "TimeoutExpired: timed out after 120 seconds"),
                                   (0, "")])
def test_a_runtime_that_cannot_start_fails_with_the_cause_and_the_fix(side, probe):
    side.probe = probe
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert side.handoffs == [] and side.in_process == ["shim"], "no silent fallback to the old code"
    assert update.report.ok is False and update.report.exit_code == up.CLIENT_STEP_FAILED
    handoff = update.report.clients["handoff"]
    assert handoff["state"] == "failed"
    assert "000002" in handoff["detail"] and "did not start" in handoff["detail"]
    assert f"update --clients-only --tag {TARGET} --reinstall" in handoff["detail"]
    if probe[1]:
        assert probe[1].split(":")[0] in handoff["detail"]


def test_a_child_that_cannot_be_started_fails_the_run(side, monkeypatch):
    monkeypatch.setattr(up, "run_handoff", lambda argv, **kw: (None, "OSError: [WinError 2] not found"))
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert update.report.ok is False and update.report.exit_code == up.CLIENT_STEP_FAILED
    assert "WinError 2" in update.report.clients["handoff"]["detail"]
    assert side.in_process == ["shim"]


def test_a_child_that_exits_without_its_report_is_not_a_success(side):
    side.child_report = None
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert update.report.ok is False and update.report.exit_code == up.CLIENT_STEP_FAILED
    assert "report" in update.report.clients["handoff"]["detail"]


# ── the lock the child runs under ───────────────────────────────────────────

def test_the_child_runs_under_its_parent_s_update_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    parent = up.UpdateLock()
    parent.acquire()
    try:
        child = up.UpdateLock(borrow_from=str(os.getpid()))
        child.acquire()          # the holder is the parent that named itself: no refusal
        child.release()
        assert (tmp_path / "data" / up.LOCK_HOLDER_NAME).is_file(), "the child released its parent's lock"
        with pytest.raises(up.UpdateBusy):
            up.UpdateLock(borrow_from="1").acquire()     # another holder than the one named
        with pytest.raises(up.UpdateBusy):
            up.UpdateLock().acquire()
    finally:
        parent.release()
    free = up.UpdateLock(borrow_from=str(os.getpid()))
    free.acquire()               # nobody holds it: the child takes it itself
    assert free._lock.held
    free.release()


# ── the real hand-off process ───────────────────────────────────────────────

def test_run_handoff_relays_each_line_and_returns_the_exit_code(tmp_path):
    script = "import sys\nprint('==> one')\nprint('WARNING: two', file=sys.stderr)\nprint('caf\\u00e9')\nsys.exit(5)\n"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    lines: list[str] = []
    code, error = up.run_handoff([sys.executable, "-c", script], cwd=str(tmp_path), env=env, relay=lines.append)
    assert code == 5 and error == ""
    assert lines == ["==> one", "WARNING: two", "café"]


def test_run_handoff_names_a_missing_interpreter(tmp_path):
    code, error = up.run_handoff([str(tmp_path / "missing-python"), "-c", "pass"], cwd=str(tmp_path),
                                 env=dict(os.environ), relay=None)
    assert code is None and error
