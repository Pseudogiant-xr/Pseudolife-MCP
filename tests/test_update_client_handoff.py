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
        self.invokers: list = []

    def update_shim(self, source, repo=None, reinstall=False, invoker=None):
        self.in_process.append("shim")
        self.invokers.append(invoker)
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
    monkeypatch.setattr(up, "run_handoff", s.run_handoff)
    from pseudolife_memory import tunnel_profiles
    monkeypatch.setattr(tunnel_profiles, "default_root", lambda: tmp_path / "no-tunnels")
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(tmp_path / "rt"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_LAUNCHER",
                       str(tmp_path / "bin" / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")))
    monkeypatch.delenv(up.HANDOFF_ENV, raising=False)
    monkeypatch.delenv(up.HANDOFF_RUNTIME_ENV, raising=False)
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
    assert argv[0] == str(side.python) and argv[1:3] == ["-c", up._HANDOFF_BOOTSTRAP]
    assert argv[3:5] == ["pseudolife_memory.cli", "update"]
    assert "--clients-only" in argv and argv[argv.index("--tag") + 1] == TARGET
    assert argv[argv.index("--daemon-url") + 1] == "http://10.0.0.5:8765"
    # The shim step already ran here: the child must not install a third runtime,
    # and the result file is this run's to write.
    assert "--reinstall" not in argv and "--result-file" not in argv
    # The caller's working directory: a relative CLAUDE_CONFIG_DIR or
    # CODEX_HOME must name the same place in the child (Codex review, 2026-11-06).
    assert call["cwd"] is None
    env = call["env"]
    assert env[up.HANDOFF_ENV] == str(os.getpid())
    # The runtime this updater runs from, which the child's prune must keep
    # while this process lives (0.22.1 to 0.23.0 deleted it under it).
    assert env[up.HANDOFF_RUNTIME_ENV] == sys.prefix
    assert side.invokers == [None], "the run that hands off was handed nothing"
    assert "PYTHONPATH" not in env and env["HTTPS_PROXY"] == "http://proxy.example.com:3128"
    # The probe imports nothing from the working directory or PYTHONPATH.
    assert side.probes and side.probes[0][:2] == [str(side.python), "-I"]

    # The child's result is this run's result; the child's ladder was its own output.
    assert update.report.ok is True and update.report.exit_code == 0
    assert update.report.clients["plugin"]["state"] == "refreshed:abc"
    assert update.report.clients["shim"]["state"] == f"installed:{TARGET}", "the install this run did is reported"
    assert "  [x] Plugin refreshed:abc - new code refreshed it" in update.lines
    assert any("runtime 000002" in line and TARGET in line for line in update.lines)


def test_the_child_s_codex_steps_become_this_run_s(side, tmp_path):
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


@pytest.mark.parametrize("runtime", [None, "/runtimes/000021"])
def test_a_handed_off_shim_step_never_prunes_the_runtime_its_parent_runs_from(side, runtime):
    """The parent waits on the child and imports lazily afterwards: its pid
    (all an updater through 0.23.0 sends) and its runtime (from 0.23.1)
    reach the shim step's prune, which holds that runtime while it lives."""
    update = _update(clients_only=True, tag=TARGET, handoff_from="4242", handoff_runtime=runtime)
    update.clients(None, SOURCE, HEALTH)
    assert side.invokers == [(4242, runtime)]


def test_main_takes_the_marker_out_of_the_environment(side, monkeypatch):
    """Read once and removed, so nothing the child starts (pip, claude, a
    tunnel refresh) inherits it."""
    seen = {}

    def run(self):
        seen["from"], seen["report"] = self.o.handoff_from, self.o.handoff_report
        seen["runtime"] = self.o.handoff_runtime
        seen["env"] = (os.environ.get(up.HANDOFF_ENV), os.environ.get(up.HANDOFF_REPORT_ENV),
                       os.environ.get(up.HANDOFF_RUNTIME_ENV))
        return 0

    monkeypatch.setattr(up.Update, "run", run)
    monkeypatch.setenv(up.HANDOFF_ENV, "4242")
    monkeypatch.setenv(up.HANDOFF_REPORT_ENV, str(side.tmp / "r.json"))
    monkeypatch.setenv(up.HANDOFF_RUNTIME_ENV, str(side.tmp / "rt" / "000001"))
    assert up.main(["--clients-only", "--tag", TARGET]) == 0
    assert seen == {"from": "4242", "report": side.tmp / "r.json", "runtime": str(side.tmp / "rt" / "000001"),
                    "env": (None, None, None)}


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
    # This working interpreter, not the launcher, which starts the runtime that just failed.
    assert f"-m pseudolife_memory.cli update --clients-only --tag {TARGET} --reinstall" in handoff["detail"]
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


def test_a_child_that_stops_before_its_client_step_names_its_own_cause(side):
    """Opus review, 2026-11-06: the child started (it wrote a report) but its
    own /health read failed; that is the cause, not "did not start"."""
    side.child_code = 2
    side.child_report = {"steps": ["WARNING: the configured daemon did not report its release on /health"],
                         "clients": None, "ok": False, "exit_code": 2, "codex_reapproval": ""}
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert update.report.ok is False and update.report.exit_code == 2
    detail = update.report.clients["handoff"]["detail"]
    assert "stopped before the client steps" in detail and "/health" in detail and "--reinstall" not in detail


def test_a_child_that_exits_zero_but_reports_a_failure_fails_the_run(side):
    side.child_report["ok"] = False
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert update.report.ok is False and update.report.exit_code == up.CLIENT_STEP_FAILED


def test_an_older_interpreter_finding_the_runtime_current_still_hands_off(side):
    """The "second run" case of the old two-run shape: the new runtime is
    already there, but this process is an older install's."""
    side.shim = {"state": f"current:{TARGET}", "detail": "already installed", "runtime": str(side.runtime),
                 "version": TARGET}
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert side.handoffs and side.in_process == ["shim"] and update.report.ok is True


def test_a_failed_registration_move_still_hands_off_to_the_installed_runtime(side):
    """The runtime is installed even though one registration did not move:
    the new code retries the move rather than the old code going on."""
    side.shim = {"state": "failed", "detail": "Codex: config.toml is read-only", "runtime": str(side.runtime),
                 "version": TARGET}
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert side.handoffs and side.in_process == ["shim"]
    assert update.report.clients["shim"]["state"] == f"current:{TARGET}", "the child's retry is the shim result"
    assert update.report.ok is True


def test_a_child_that_installed_again_keeps_its_own_shim_row(side):
    """Opus review, 2026-11-06: this run's older dependency ranges left the
    runtime outside the release's, and the child built another one; the
    report names the runtime the launcher now starts."""
    side.child_report["clients"]["shim"] = {"state": f"installed:{TARGET}", "detail": "runtime 000003 installed"}
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    assert update.report.clients["shim"]["detail"] == "runtime 000003 installed"


def test_the_finish_hint_is_dropped_when_the_hand_off_does_the_finish(side):
    hint = (f"runtime 000002 ({TARGET}) still has mcp 2.1.1: the pseudolife-mcp running this update installs "
            f"an older dependency range; finish with: pseudolife-mcp update --clients-only")
    side.shim.update(detail=f"runtime 000002 installed; {hint}; sessions keep their runtime", finish_hint=hint)
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    shim = update.report.clients["shim"]
    assert "finish with" not in shim["detail"] and "finish_hint" not in shim
    assert shim["detail"] == "runtime 000002 installed; sessions keep their runtime"


def test_the_shim_row_is_printed_once_when_the_child_cannot_start(side, monkeypatch, capsys):
    monkeypatch.setattr(up, "run_handoff", lambda argv, **kw: (None, "OSError: gone"))
    update = _update(clients_only=True, tag=TARGET)
    update.clients(None, SOURCE, HEALTH)
    out = capsys.readouterr().out
    assert out.count(" Shim ") == 1 and out.count(" Hand-off ") == 1


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


# ── the real probe and bootstrap ────────────────────────────────────────────

def _fake_release(root: Path, version: str, update_cli: str | None) -> Path:
    package = root / "pseudolife_memory"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"__version__ = {version!r}\n", encoding="utf-8")
    if update_cli is not None:
        (package / "update_cli.py").write_text(update_cli, encoding="utf-8")
    return root


def _probe(root: Path) -> tuple[int, str]:
    import subprocess
    # Not -I here: the fake release is found through the working directory.
    # -S: no site-packages, so an editable install's import finder cannot
    # answer pseudolife_memory.update_cli from some other tree.
    proc = subprocess.run([sys.executable, "-S", "-c", up._HANDOFF_PROBE], cwd=root, capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")})
    return proc.returncode, proc.stdout + proc.stderr


def test_the_probe_lists_the_protocols_of_a_release_that_has_them(tmp_path):
    root = _fake_release(tmp_path, TARGET, f"CLIENT_HANDOFF_PROTOCOLS = ({up.CLIENT_HANDOFF!r}, 'later-v2')\n")
    assert _probe(root) == (0, f"{TARGET}\n{up.CLIENT_HANDOFF} later-v2\n")


def test_the_probe_answers_none_for_a_release_without_an_updater(tmp_path):
    """Codex review, 2026-11-06: a release older than update_cli itself is a
    target that predates the hand-off, not a broken runtime."""
    assert _probe(_fake_release(tmp_path, "0.14.0", None)) == (0, "0.14.0\n\n")
    assert _probe(_fake_release(tmp_path / "b", "0.16.0", "X = 1\n")) == (0, "0.16.0\n\n")


def test_the_probe_fails_for_an_updater_that_cannot_import(tmp_path):
    code, out = _probe(_fake_release(tmp_path, TARGET, "import a_dependency_this_runtime_lacks\n"))
    assert code != 0 and "a_dependency_this_runtime_lacks" in out


def test_the_bootstrap_keeps_the_working_directory_off_the_import_path(tmp_path):
    """The child runs where the caller ran (relative client paths keep their
    meaning), yet a package in that directory never answers for the
    runtime's own: here a shadowing calendar.py beside the stdlib one."""
    import subprocess
    (tmp_path / "calendar.py").write_text("print('SHADOW')\n", encoding="utf-8")
    # The bootstrap's own import too (security review, 2026-11-06): runpy is
    # imported only after the working directory left the path.
    (tmp_path / "runpy.py").write_text("print('SHADOW')\nrun_module = None\n", encoding="utf-8")
    # frozen_modules=off: 3.11+ freezes runpy, 3.10 (a supported runtime) does not.
    proc = subprocess.run([sys.executable, "-X", "frozen_modules=off", "-c", up._HANDOFF_BOOTSTRAP,
                           "calendar", "2026", "1"], cwd=tmp_path,
                          capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")})
    assert proc.returncode == 0, proc.stderr
    assert "SHADOW" not in proc.stdout and "January 2026" in proc.stdout


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
