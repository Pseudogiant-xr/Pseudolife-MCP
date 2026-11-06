"""Linux health publication never changes Windows or the comparison allowlist."""
import importlib.util
import json
from pathlib import Path
import sys
import subprocess

import pytest

PATH = Path(__file__).resolve().parents[1] / "ops/selfhosted/linux_pool_health.py"
spec = importlib.util.spec_from_file_location("linux_pool_health", PATH)
health = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = health
spec.loader.exec_module(health)


def runner(identifier, *, online=True, busy=False, label=health.LABEL):
    return {"id": identifier, "status": "online" if online else "offline", "busy": busy,
            "labels": [{"name": label}]}


def test_only_online_idle_linux_workers_count():
    rows = [runner(1), runner(2, busy=True), runner(3, online=False),
            runner(4, label="pseudolife-parity-windows"), runner(5, busy=None)]
    frame = health.sample(lambda *a: (200, {"runners": rows}), "owner/repo", now=1000)
    assert frame == {"schema": 1, "observed_at": 1000, "online": 3, "idle": 1}


def test_pages_are_deduplicated_and_do_not_publish_names():
    rows = [runner(n) for n in range(30)]
    pages = iter([rows, [runner(0), runner(30)]])
    frame = health.sample(lambda *a: (200, {"runners": next(pages)}), "owner/repo", now=1000)
    assert frame["online"] == frame["idle"] == 31
    assert set(frame) == {"schema", "observed_at", "online", "idle"}


@pytest.mark.parametrize("old_status,method,code", [(404, "POST", 201), (200, "PATCH", 204)])
def test_publisher_writes_only_the_linux_health_variable(old_status, method, code):
    calls = []
    def call(verb, path, payload):
        calls.append((verb, path, payload))
        return (old_status, None) if verb == "GET" else (code, None)
    frame = dict(schema=1, observed_at=1000, online=3, idle=3)
    health.publish(call, "owner/repo", frame)
    assert [call[0] for call in calls] == ["GET", method]
    assert calls[0][1].endswith("/" + health.VARIABLE)
    payload = calls[1][2]
    assert payload["name"] == health.VARIABLE and json.loads(payload["value"]) == frame
    assert "WINDOWS" not in str(calls) and "PARITY_SELFHOSTED_RUNNERS" not in str(calls)


def test_failed_sampling_never_refreshes_a_heartbeat(capsys):
    calls = []
    def call(verb, path, payload):
        calls.append(verb)
        raise health.ApiError("fixture")
    assert health.main([], call=call) == 1
    assert calls == ["GET"]
    assert "could not be verified" in capsys.readouterr().err


def test_dry_run_does_not_create_a_variable(capsys):
    calls = []
    def call(verb, path, payload):
        calls.append((verb, path))
        return 200, {"runners": [runner(1), runner(2), runner(3)]}
    assert health.main(["--dry-run"], call=call) == 0
    assert len(calls) == 1 and calls[0][1].endswith("actions/runners?per_page=30&page=1")
    assert json.loads(capsys.readouterr().out)["idle"] == 3


def test_invalid_token_is_not_logged(tmp_path, capsys):
    path = tmp_path / "token"
    path.write_text("fixture\ninjected")
    assert health.main(["--token-file", str(path)]) == 1
    assert "fixture" not in capsys.readouterr().err


def test_sampling_uses_start_time_so_slow_reads_cannot_refresh_old_observations(monkeypatch):
    clock = [1000]
    monkeypatch.setattr(health.time, "time", lambda: clock[0])
    def call(*args):
        clock[0] = 1190
        return 200, {"runners": [runner(1), runner(2), runner(3)]}
    assert health.sample(call, "owner/repo")["observed_at"] == 1000


def test_existing_gh_credential_is_read_without_logging_or_replacing_it(monkeypatch, capsys):
    calls = []
    def credential(args, **kwargs):
        assert args == ["gh", "auth", "token"]
        assert kwargs == dict(capture_output=True, text=True, timeout=10, check=True)
        return subprocess.CompletedProcess(args, 0, "fixture-controller-token\n", "")
    def client(token):
        assert token == "fixture-controller-token"
        calls.append("existing credential read")
        return lambda *a: (200, {"runners": [runner(1), runner(2), runner(3)]})
    monkeypatch.setattr(health.subprocess, "run", credential)
    monkeypatch.setattr(health, "github", client)
    assert health.main(["--gh-auth", "--dry-run"]) == 0
    output = capsys.readouterr()
    assert "fixture-controller-token" not in output.out + output.err
    assert calls == ["existing credential read"]


def test_gh_auth_failure_does_not_log_credential_output(monkeypatch, capsys):
    def credential(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], output="fixture-private-output")
    monkeypatch.setattr(health.subprocess, "run", credential)
    assert health.main(["--gh-auth"]) == 1
    output = capsys.readouterr()
    assert "could not be verified" in output.err
    assert "fixture-private-output" not in output.out + output.err


def test_publisher_transport_refuses_credential_redirects(monkeypatch):
    def opener(handler):
        assert isinstance(handler, health.NoRedirect)
        class Reader:
            def open(self, request, timeout):
                assert request.full_url == "https://api.github.com/repos/owner/repo/actions/runners"
                assert request.get_header("Authorization") == "Bearer fixture-controller-token"
                assert timeout == 30
                raise health.urllib.error.HTTPError(request.full_url, 302, "redirect", {}, None)
        return Reader()
    monkeypatch.setattr(health.urllib.request, "build_opener", opener)
    with pytest.raises(health.ApiError, match="request failed"):
        health.github("fixture-controller-token")("GET", "/repos/owner/repo/actions/runners")
    request = health.urllib.request.Request("https://api.github.com/example")
    assert health.NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://example.com") is None


@pytest.mark.skipif(sys.platform != "linux", reason="native Linux systemd installer proof")
def test_installer_is_confined_to_new_health_service_and_keeps_existing_credentials(tmp_path, monkeypatch):
    import shutil
    source = PATH.parent
    home = tmp_path / "checkout"
    home.mkdir()
    for name in ("linux_pool_health.py", "runner_watch.py"):
        shutil.copyfile(source / name, home / name)
    script = (source / "setup-linux-pool-health.sh").read_text()
    lib = tmp_path / "lib"
    units = tmp_path / "units"
    units.mkdir()
    script = script.replace("/usr/local/lib/pseudolife-linux-pool-health", str(lib))
    script = script.replace("/etc/systemd/system", str(units))
    assert "/etc/" not in script and "/usr/local/" not in script
    path = home / "setup.sh"
    path.write_text(script)
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "systemctl.log"
    for name, body in {
        "id": '#!/bin/sh\nif [ "$1" = -u ]; then if [ "$#" = 1 ]; then echo 0; else echo 1000; fi; fi\n',
        "gh": '#!/bin/sh\nexit 0\n',
        "runuser": '#!/bin/sh\nexit 0\n',
        "systemctl": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$POOL_TEST_SYSTEMCTL_LOG"\n',
    }.items():
        shim = binary / name
        shim.write_text(body)
        shim.chmod(0o755)
    import os
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("POOL_TEST_SYSTEMCTL_LOG", str(log))
    for _ in range(2):
        result = subprocess.run(["bash", str(path), "--account", "fixture-controller"], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    service = (units / "pseudolife-linux-pool-health.service").read_text()
    timer = (units / "pseudolife-linux-pool-health.timer").read_text()
    assert "User=fixture-controller" in service and " --gh-auth" in service
    assert "ProtectSystem=strict" in service and "NoNewPrivileges=yes" in service
    assert "OnUnitInactiveSec=60s" in timer
    assert log.read_text().splitlines() == ["daemon-reload", "enable --now pseudolife-linux-pool-health.timer"] * 2
    assert set(p.name for p in lib.iterdir()) == {"linux_pool_health.py", "runner_watch.py"}
