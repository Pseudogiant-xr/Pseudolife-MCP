"""``pseudolife-mcp maintainer setup``: maintainer passkeys in one guided command.

The host steps of the passkey setup (an HTTPS name for the Console, the
file-only ``coordination.maintainer`` keys, a daemon restart, the one-time
enrolment code and the host-side confirm) run from one command that asks
once before it changes anything, and only the passkey tap and the prefix
check are left to the maintainer (2026-10-05).

A fake host stands in for Docker (``inspect``, ``restart``, ``exec`` of the
setup's own state/write helpers and of ``maintainer enrol-code`` /
``confirm`` / ``revoke``), and a fake ``tailscale`` for the tailnet; the
daemon's ``/health`` and the terminal are seams too. The host-side helpers
(``state`` and ``write``) run for real against a config file in tmp_path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pseudolife_memory import expose_cli, maintainer_cli  # noqa: E402
from pseudolife_memory import maintainer_setup as ms  # noqa: E402
from pseudolife_memory.utils.config import MaintainerConfig  # noqa: E402

DNS = "box.tailnet.example"   # any tailnet DNS name; .ts.net names are screened (test_release_ux)
ORIGIN = f"https://{DNS}:8443"
PREFIX = "abcdef123456"


class Host:
    """Docker, the daemon's environment and the terminal, as a table."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.container: str | None = "true"      # docker inspect .State.Running; None: no container
        self.rp_id, self.origin = "", ""
        self.keys: list[dict] = []
        self.state_error: str | None = None      # stderr of a failing state helper
        self.write_rc = 0
        self.enrol_lines = ["One-time enrolment code: ABCDE23456",
                            "Valid for 10 minutes. Enter it in the Console with a label, then "
                            "register your passkey.",
                            "Waiting for the Console to redeem it...",
                            f"Enrolled (pending): {PREFIX}  label: laptop",
                            "Check that the Console shows the same prefix and label, then run:",
                            f"  pseudolife-mcp maintainer confirm {PREFIX}"]
        self.enrol_rc = 0
        self.health = {"status": "ok", "auth": True}
        self.answers: list[str] = []
        self.questions: list[str] = []
        self.tty = True

    # the subprocess seams ---------------------------------------------------
    def run(self, argv, *, timeout=120.0, input=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[0] == "fake-docker" and argv[1] == "inspect":
            if self.container is None:
                return _done(argv, 1, "", "Error: No such object: pseudolife-mcp-daemon")
            return _done(argv, 0, self.container + "\n")
        if argv[0] == "fake-docker" and argv[1] == "restart":
            return _done(argv, 0, "pseudolife-mcp-daemon\n")
        if ms.MODULE in argv:
            sub = argv[argv.index(ms.MODULE) + 1:]
            if sub[0] == "state":
                if self.state_error:
                    return _done(argv, 1, "", self.state_error)
                problem = MaintainerConfig(rp_id=self.rp_id, origin=self.origin).problem()
                return _done(argv, 0, json.dumps({
                    "config_path": "/data/config.yaml", "rp_id": self.rp_id,
                    "origin": self.origin, "problem": problem, "keys": self.keys,
                    "keys_error": None}))
            if sub[0] == "write":
                if self.write_rc:
                    return _done(argv, self.write_rc, "", "PermissionError")
                self.rp_id, self.origin = sub[1], sub[2]
                return _done(argv, 0, json.dumps({"config_path": "/data/config.yaml",
                                                  "backup": "/data/config.yaml.bak"}))
        if "maintainer" in argv:
            action, prefix = argv[argv.index("maintainer") + 1], argv[-1]
            for key in self.keys:
                if key["prefix"] == prefix:
                    key["state"] = {"confirm": "active", "revoke": "revoked"}[action]
            return _done(argv, 0, f"{action}ed: {prefix}\n")
        return _done(argv, 91, "", f"unexpected {argv}")

    def stream(self, argv, on_line):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for line in self.enrol_lines:
            if line.startswith("Enrolled (pending)"):
                self.keys.append({"prefix": PREFIX, "label": "laptop", "state": "pending"})
            on_line(line)
        return self.enrol_rc

    def ask(self, question):
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else ""

    def named(self, word: str) -> list[list[str]]:
        return [c for c in self.calls if word in c]


def _done(argv, rc, out, err=""):
    import subprocess
    return subprocess.CompletedProcess(argv, rc, out, err)


class Tailnet:
    """``tailscale status --json`` and ``serve`` as a table."""

    def __init__(self):
        self.present = True
        self.backend = "Running"
        self.cert_domains = [DNS]
        self.serve: dict = {}
        self.serve_takes = True
        self.calls: list[list[str]] = []

    def run(self, binary, args):
        import subprocess
        self.calls.append(list(args))
        if args == ["status", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "BackendState": self.backend, "CertDomains": self.cert_domains,
                "Self": {"DNSName": DNS + ".", "TailscaleIPs": ["100.64.0.9"]}}), "")
        if args == ["serve", "status", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.serve), "")
        if args[:2] == ["serve", "--bg"]:
            port = args[2].split("=", 1)[1]
            if self.serve_takes:
                self.serve.setdefault("TCP", {})[port] = {"HTTPS": True}
                self.serve.setdefault("Web", {})[f"{DNS}:{port}"] = {
                    "Handlers": {"/": {"Proxy": args[3]}}}
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:1] == ["serve"] and args[-1] == "off":
            port = args[1].split("=", 1)[1]
            self.serve.get("TCP", {}).pop(port, None)
            self.serve.get("Web", {}).pop(f"{DNS}:{port}", None)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 64, "", "unexpected")

    def changing(self) -> list[list[str]]:
        return [c for c in self.calls if c[:1] == ["serve"] and c[1:2] != ["status"]]


@pytest.fixture
def host(monkeypatch):
    h = Host()
    monkeypatch.setenv("PSEUDOLIFE_DOCKER", "fake-docker")
    monkeypatch.setattr(ms, "run", h.run)
    monkeypatch.setattr(ms, "stream", h.stream)
    monkeypatch.setattr(ms, "ask", h.ask)
    monkeypatch.setattr(ms, "interactive", lambda: h.tty)
    monkeypatch.setattr(ms, "sleep", lambda s: None)
    monkeypatch.setattr(ms, "bearer", lambda: None)
    monkeypatch.setattr(ms, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ms, "health", lambda url: h.health)
    return h


@pytest.fixture
def tailnet(monkeypatch):
    t = Tailnet()
    monkeypatch.setattr(expose_cli, "find_tailscale", lambda: "tailscale" if t.present else None)
    monkeypatch.setattr(expose_cli, "run_tailscale", t.run)
    return t


def _ours(port: int = 8443) -> dict:
    return {"TCP": {str(port): {"HTTPS": True}},
            "Web": {f"{DNS}:{port}": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}}}


# ── a first setup ────────────────────────────────────────────────────────────

def test_a_tailnet_host_is_set_up_end_to_end_after_one_question(host, tailnet, capsys):
    host.answers = ["y", "y"]
    assert ms.main([]) == 0
    out = capsys.readouterr().out
    # the plan names every persistent change before the one question
    assert len(host.questions) == 2
    assert "tailscale serve --bg --https=8443 http://127.0.0.1:8765" in out
    assert f"rp_id {DNS}" in out and f"origin {ORIGIN}" in out
    assert tailnet.changing() == [["serve", "--bg", "--https=8443", "http://127.0.0.1:8765"]]
    [write] = [c for c in host.calls if "write" in c]
    assert write[-2:] == [DNS, ORIGIN]
    order = [i for i, c in enumerate(host.calls)
             if "write" in c or "restart" in c or "enrol-code" in c or "confirm" in c]
    kinds = [next(k for k in ("write", "restart", "enrol-code", "confirm") if k in host.calls[i])
             for i in order]
    assert kinds == ["write", "restart", "enrol-code", "confirm"]
    [confirm] = host.named("confirm")
    assert confirm[-2:] == ["--", PREFIX]
    # the human steps are named: where to go, and what to check
    assert f"{ORIGIN}/ui/" in out
    assert PREFIX in host.questions[1] and "laptop" in host.questions[1]
    # enrol-code's own "then run confirm" advice is not echoed: setup asks instead
    assert "pseudolife-mcp maintainer confirm" not in out
    assert "active" in out.lower()


def test_the_docker_tier_runs_the_helpers_and_enrolment_in_the_daemon_container(host, tailnet):
    host.answers = ["y", "y"]
    assert ms.main([]) == 0
    for call in [c for c in host.calls if ms.MODULE in c or "maintainer" in c]:
        assert call[:3] == ["fake-docker", "exec", ms.DAEMON_CONTAINER], call
    assert ["fake-docker", "restart", ms.DAEMON_CONTAINER] in host.calls


def test_answering_no_at_the_prefix_check_revokes_the_key(host, tailnet, capsys):
    host.answers = ["y", "n"]
    assert ms.main([]) == ms.EXIT_REFUSED
    assert host.named("revoke") and not host.named("confirm")
    assert host.keys[0]["state"] == "revoked"
    assert "pseudolife-mcp maintainer setup" in capsys.readouterr().out


def test_without_tailscale_the_console_is_named_on_localhost(host, tailnet, capsys):
    tailnet.present = False
    host.answers = ["y", "y"]
    assert ms.main([]) == 0
    [write] = [c for c in host.calls if "write" in c]
    assert write[-2:] == ["localhost", "http://localhost:8765"]
    assert tailnet.calls == []
    assert "this machine only" in capsys.readouterr().out


def test_local_forces_localhost_on_a_tailnet_host(host, tailnet):
    host.answers = ["y", "y"]
    assert ms.main(["--local"]) == 0
    [write] = [c for c in host.calls if "write" in c]
    assert write[-2:] == ["localhost", "http://localhost:8765"]
    assert tailnet.changing() == []


def test_a_custom_daemon_port_is_served_and_named(host, tailnet):
    host.answers = ["y", "y"]
    assert ms.main(["--port", "9876", "--https-port", "443"]) == 0
    assert tailnet.changing() == [["serve", "--bg", "--https=443", "http://127.0.0.1:9876"]]
    [write] = [c for c in host.calls if "write" in c]
    assert write[-2:] == [DNS, f"https://{DNS}"]


def test_an_invalid_config_is_replaced_and_the_problem_named(host, tailnet, capsys):
    host.rp_id, host.origin = DNS, f"http://{DNS}:8443"
    host.answers = ["y", "y"]
    assert ms.main([]) == 0
    assert "plain http is allowed for rp_id localhost only" in capsys.readouterr().out
    assert (host.rp_id, host.origin) == (DNS, ORIGIN)


# ── refusals before any change ───────────────────────────────────────────────

def _unchanged(host: Host, tailnet: Tailnet) -> None:
    assert tailnet.changing() == []
    assert not [c for c in host.calls if "write" in c or "restart" in c or "enrol-code" in c]


def test_https_certificates_off_refuses_and_names_both_ways_out(host, tailnet, capsys):
    tailnet.cert_domains = []
    assert ms.main(["--yes"]) == ms.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "HTTPS" in err and "--local" in err
    _unchanged(host, tailnet)


def test_tailscale_installed_but_stopped_refuses_rather_than_guess(host, tailnet, capsys):
    tailnet.backend = "Stopped"
    assert ms.main(["--yes"]) == ms.EXIT_REFUSED
    assert "--local" in capsys.readouterr().err
    _unchanged(host, tailnet)


def test_another_serve_on_the_https_port_is_never_replaced(host, tailnet, capsys):
    tailnet.serve = {"TCP": {"8443": {"HTTPS": True}},
                     "Web": {f"{DNS}:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}
    assert ms.main(["--yes"]) == ms.EXIT_REFUSED
    assert "--https-port" in capsys.readouterr().err
    _unchanged(host, tailnet)


def test_a_daemon_without_a_bearer_is_refused(host, tailnet, capsys):
    host.health = {"status": "ok", "auth": False}
    assert ms.main(["--yes"]) == ms.EXIT_REFUSED
    assert "token" in capsys.readouterr().err
    _unchanged(host, tailnet)


def test_no_daemon_is_refused(host, tailnet, capsys):
    host.health = None
    assert ms.main(["--yes"]) == ms.EXIT_REFUSED
    assert "127.0.0.1:8765" in capsys.readouterr().err
    _unchanged(host, tailnet)


def test_a_daemon_that_predates_the_command_says_to_update(host, tailnet, capsys):
    host.state_error = "/usr/local/bin/python: No module named pseudolife_memory.maintainer_setup"
    assert ms.main(["--yes"]) == ms.EXIT_REFUSED
    assert "pseudolife-mcp update" in capsys.readouterr().err
    _unchanged(host, tailnet)


def test_no_terminal_and_no_yes_changes_nothing(host, tailnet, capsys):
    host.tty = False
    assert ms.main([]) == ms.EXIT_USAGE
    assert "--yes" in capsys.readouterr().err
    _unchanged(host, tailnet)


def test_a_declined_plan_changes_nothing(host, tailnet):
    host.answers = ["n"]
    assert ms.main([]) == ms.EXIT_USAGE
    _unchanged(host, tailnet)


def test_a_serve_that_does_not_take_is_undone_and_nothing_written(host, tailnet, capsys):
    tailnet.serve_takes = False
    assert ms.main(["--yes"]) == ms.EXIT_UNDONE
    assert not [c for c in host.calls if "write" in c or "restart" in c]


def test_a_failed_config_write_takes_the_new_serve_back_off(host, tailnet):
    host.write_rc = 1
    assert ms.main(["--yes"]) == ms.EXIT_UNDONE
    assert tailnet.changing()[-1] == ["serve", "--https=8443", "off"]
    assert tailnet.serve.get("TCP", {}) == {}
    assert not host.named("restart")


# ── unattended, and re-runs ──────────────────────────────────────────────────

def test_yes_without_a_terminal_applies_the_config_and_leaves_enrolment(host, tailnet, capsys):
    host.tty = False
    assert ms.main(["--yes"]) == 0
    assert (host.rp_id, host.origin) == (DNS, ORIGIN)
    assert host.named("restart") and not host.named("enrol-code")
    assert "pseudolife-mcp maintainer setup" in capsys.readouterr().out


def test_a_configured_host_with_an_active_key_changes_nothing(host, tailnet, capsys):
    host.rp_id, host.origin = DNS, ORIGIN
    host.keys = [{"prefix": PREFIX, "label": "laptop", "state": "active"}]
    tailnet.serve = _ours()
    host.tty = False
    assert ms.main([]) == 0
    out = capsys.readouterr().out
    assert "in place" in out and ORIGIN in out and "1 active" in out
    assert host.questions == []
    _unchanged(host, tailnet)


def test_a_configured_host_missing_only_its_serve_gets_just_the_serve(host, tailnet):
    host.rp_id, host.origin = DNS, ORIGIN
    host.keys = [{"prefix": PREFIX, "label": "laptop", "state": "active"}]
    assert ms.main(["--yes"]) == 0
    assert tailnet.changing() == [["serve", "--bg", "--https=8443", "http://127.0.0.1:8765"]]
    assert not [c for c in host.calls if "write" in c or "restart" in c or "enrol-code" in c]


def test_a_configured_name_is_kept_even_when_detection_would_differ(host, tailnet):
    """A passkey is bound to its name: a re-run never renames under it."""
    host.rp_id, host.origin = "localhost", "http://localhost:8765"
    host.keys = [{"prefix": PREFIX, "label": "laptop", "state": "active"}]
    assert ms.main(["--yes"]) == 0
    assert (host.rp_id, host.origin) == ("localhost", "http://localhost:8765")
    assert tailnet.changing() == []


def test_a_pending_key_from_an_earlier_run_is_offered_for_confirmation(host, tailnet):
    host.rp_id, host.origin = DNS, ORIGIN
    host.keys = [{"prefix": PREFIX, "label": "laptop", "state": "pending"}]
    tailnet.serve = _ours()
    host.answers = ["y"]
    assert ms.main([]) == 0
    assert host.named("confirm") and not host.named("enrol-code")
    assert PREFIX in host.questions[0]


def test_a_daemon_still_running_the_old_name_is_restarted(host, tailnet, monkeypatch):
    """The file says one thing, the running daemon (asked as doctor does)
    another: an interrupted earlier run. Only the restart is due."""
    host.rp_id, host.origin = DNS, ORIGIN
    host.keys = [{"prefix": PREFIX, "label": "laptop", "state": "active"}]
    tailnet.serve = _ours()
    monkeypatch.setattr(ms, "bearer", lambda: "fixture-token")
    from pseudolife_memory import doctor_cli
    monkeypatch.setattr(doctor_cli, "maintainer_probe",
                        lambda url, token, timeout=2.0: {"state": "off"})
    assert ms.main(["--yes"]) == 0
    assert host.named("restart")
    assert not [c for c in host.calls if "write" in c]


def test_the_lite_tier_writes_locally_and_names_the_restart(host, tailnet, capsys):
    host.container = None
    host.answers = ["y"]
    assert ms.main(["--local"]) == 0
    calls = [c for c in host.calls if ms.MODULE in c]
    assert calls and all(c[0] == sys.executable for c in calls)
    assert not host.named("restart") and not host.named("enrol-code")
    out = capsys.readouterr().out
    assert "restart" in out.lower() and "pseudolife-mcp maintainer setup" in out


def test_passkeys_set_up_is_what_update_asks(host, tailnet):
    assert ms.passkeys_set_up() is False
    host.rp_id, host.origin = DNS, ORIGIN
    assert ms.passkeys_set_up() is False            # configured, nobody enrolled
    host.keys = [{"prefix": PREFIX, "label": "laptop", "state": "active"}]
    assert ms.passkeys_set_up() is True
    host.state_error = "boom"
    assert ms.passkeys_set_up() is None             # unknown: say nothing


def test_check_answers_without_changing_anything(host, tailnet, capsys):
    assert ms.main(["--check"]) == 1
    host.rp_id, host.origin = DNS, ORIGIN
    host.keys = [{"prefix": PREFIX, "label": "laptop", "state": "active"}]
    assert ms.main(["--check"]) == 0
    assert "in place" in capsys.readouterr().out
    host.state_error = "boom"
    assert ms.main(["--check"]) == 2
    assert host.questions == [] and tailnet.calls == []
    _unchanged(host, tailnet)


def test_an_interrupted_enrolment_says_how_to_resume(host, tailnet, monkeypatch, capsys):
    def interrupted(argv, on_line):
        raise KeyboardInterrupt
    monkeypatch.setattr(ms, "stream", interrupted)
    host.answers = ["y"]
    assert ms.main([]) == 130
    assert "pseudolife-mcp maintainer setup" in capsys.readouterr().err


# ── the helpers that run in the daemon's environment ─────────────────────────

def test_write_sets_the_two_keys_keeps_the_rest_and_backs_up(tmp_path, monkeypatch, capsys):
    import yaml
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"coordination": {"enabled": True, "maintainer": {
        "maintainer_per_recipient_per_hour": 12}}, "memory": {"preset": "flat"}}), encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_CONFIG", str(config))
    assert ms.host_main(["write", DNS, ORIGIN]) == 0
    answer = json.loads(capsys.readouterr().out)
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["coordination"]["maintainer"] == {"maintainer_per_recipient_per_hour": 12,
                                                  "rp_id": DNS, "origin": ORIGIN}
    assert data["memory"] == {"preset": "flat"} and data["coordination"]["enabled"] is True
    assert Path(answer["backup"]).is_file() and answer["config_path"] == str(config)


def test_write_refuses_a_name_the_daemon_would_refuse(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    monkeypatch.setenv("PSEUDOLIFE_MCP_CONFIG", str(config))
    assert ms.host_main(["write", DNS, f"http://{DNS}"]) == ms.EXIT_USAGE
    assert not config.exists()


def test_write_creates_a_missing_config(tmp_path, monkeypatch, capsys):
    import yaml
    config = tmp_path / "data" / "config.yaml"
    monkeypatch.setenv("PSEUDOLIFE_MCP_CONFIG", str(config))
    assert ms.host_main(["write", "localhost", "http://localhost:8765"]) == 0
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == {
        "coordination": {"maintainer": {"rp_id": "localhost", "origin": "http://localhost:8765"}}}
    assert json.loads(capsys.readouterr().out)["backup"] is None


def test_state_reports_the_file_with_the_daemons_own_rule(tmp_path, monkeypatch, capsys):
    import yaml
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"coordination": {"maintainer": {
        "rp_id": DNS, "origin": f"https://{DNS}:8443/"}}}), encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_CONFIG", str(config))
    monkeypatch.setattr(ms, "_bank_keys", lambda: ([{"prefix": PREFIX, "label": "x",
                                                     "state": "active"}], None))
    assert ms.host_main(["state"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["problem"] == MaintainerConfig(rp_id=DNS, origin=f"https://{DNS}:8443/").problem()
    assert answer["problem"] is not None
    assert answer["keys"] == [{"prefix": PREFIX, "label": "x", "state": "active"}]


def test_state_of_a_missing_file_is_unset(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PSEUDOLIFE_MCP_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.setattr(ms, "_bank_keys", lambda: (None, "no bank found"))
    assert ms.host_main(["state"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["problem"] == "unset" and answer["keys"] is None
    assert answer["keys_error"] == "no bank found"


# ── the command line ─────────────────────────────────────────────────────────

def test_maintainer_setup_is_reached_without_opening_the_bank(monkeypatch):
    seen = []
    monkeypatch.setattr(ms, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(maintainer_cli, "_bank",
                        lambda: (_ for _ in ()).throw(AssertionError("bank opened")))
    assert maintainer_cli.main(["setup", "--yes", "--local"]) == 0
    assert seen == [["--yes", "--local"]]


def test_the_cli_help_names_the_setup_command():
    text = (ROOT / "pseudolife_memory" / "cli.py").read_text(encoding="utf-8")
    assert "maintainer setup" in text


def test_a_lite_rerun_before_the_restart_says_restart_again_instead_of_enrolling(
        host, tailnet, monkeypatch, capsys):
    """Review 2026-10-05: the file is configured, the running lite daemon
    (asked as doctor does) has not loaded it: no enrolment against it."""
    host.container = None
    host.rp_id, host.origin = "localhost", "http://localhost:8765"
    monkeypatch.setattr(ms, "bearer", lambda: "fixture-token")
    from pseudolife_memory import doctor_cli
    monkeypatch.setattr(doctor_cli, "maintainer_probe",
                        lambda url, token, timeout=2.0: {"state": "off"})
    assert ms.main([]) == ms.EXIT_REFUSED
    assert not host.named("enrol-code") and host.questions == []
    assert "restart" in capsys.readouterr().out.lower()


def test_a_serve_waiting_for_the_tailnet_admin_names_the_enable_link(host, tailnet, monkeypatch, capsys):
    import subprocess
    real = tailnet.run

    def run(binary, args):
        if args[:2] == ["serve", "--bg"]:
            tailnet.calls.append(list(args))
            return subprocess.CompletedProcess(args, 124, "Serve is not enabled on your tailnet.\n"
                                               "To enable, visit:\n\n https://login.tailscale.com/f/serve?node=x\n",
                                               "timed out after 30s")
        return real(binary, args)

    monkeypatch.setattr(expose_cli, "run_tailscale", run)
    assert ms.main(["--yes"]) == ms.EXIT_UNDONE
    assert "https://login.tailscale.com/f/serve?node=x" in capsys.readouterr().err
