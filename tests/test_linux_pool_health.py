"""Linux health publication never changes Windows or the comparison allowlist."""
import importlib.util
import json
from pathlib import Path
import sys

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
    rows = [runner(n) for n in range(100)]
    pages = iter([rows, [runner(0), runner(100)]])
    frame = health.sample(lambda *a: (200, {"runners": next(pages)}), "owner/repo", now=1000)
    assert frame["online"] == frame["idle"] == 101
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
    assert len(calls) == 1 and calls[0][1].endswith("actions/runners?per_page=100&page=1")
    assert json.loads(capsys.readouterr().out)["idle"] == 3


def test_invalid_token_is_not_logged(tmp_path, capsys):
    path = tmp_path / "token"
    path.write_text("fixture\ninjected")
    assert health.main(["--token-file", str(path)]) == 1
    assert "fixture" not in capsys.readouterr().err
