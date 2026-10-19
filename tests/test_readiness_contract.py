"""The readiness contract: ``/health`` reports ``ready: true`` only once the
daemon can serve, and the test helper that starts daemons waits for it.

A daemon answers ``/health`` before its stored-principal view has loaded
(a background thread) and before the warm-up has finished initializing, so
a helper that returned on any answer let tests race the startup: a bearer
the environment does not match got 503 ``principals_unavailable`` right
after ``/health`` answered (PRs #736, #762, #781, #826, #827).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from pseudolife_memory import daemon, readiness
from pseudolife_memory.principals import install_store, installed_store


class _Store:
    def __init__(self, loaded: bool):
        self._loaded = loaded

    def loaded(self) -> bool:
        return self._loaded

    def available(self) -> bool:  # stale or not, readiness reads loaded()
        return False


@pytest.fixture
def no_store():
    previous = installed_store()
    install_store(None)
    yield
    install_store(previous)


def _svc(initialized: bool):
    return SimpleNamespace(coordination_tier_ready=lambda: initialized)


def _readiness(payload):
    return payload["ready"], payload["pending"]


def test_ready_needs_initialization(no_store):
    assert _readiness(daemon._build_health_payload(_svc(False), False)) == (False, ["init"])
    assert _readiness(daemon._build_health_payload(_svc(True), False)) == (True, [])


def test_ready_needs_the_stored_principal_view_loaded_once(no_store):
    install_store(_Store(loaded=False))
    assert _readiness(daemon._build_health_payload(_svc(False), False)) == (
        False, ["init", "principals"])
    assert _readiness(daemon._build_health_payload(_svc(True), False)) == (False, ["principals"])
    # A view that loaded and later went stale is a runtime refusal (503 per
    # request), not an unfinished startup.
    install_store(_Store(loaded=True))
    assert _readiness(daemon._build_health_payload(_svc(True), False)) == (True, [])


def test_ready_is_false_while_degraded_and_never_changes_status(no_store):
    svc = SimpleNamespace(coordination_tier_ready=lambda: True,
                          _not_ready="writer lease held elsewhere")
    payload = daemon._build_health_payload(svc, False)
    assert (payload["status"], *_readiness(payload)) == ("degraded", False, [])
    # Not ready but serving correctly otherwise: status stays ok, so the
    # Docker healthcheck and `update` keep treating a warming daemon as up.
    assert daemon._build_health_payload(_svc(False), False)["status"] == "ok"


def test_ready_and_pending_are_the_last_keys(no_store):
    # The Rust daemon inserts them last too (rust/daemon/src/health.rs).
    assert list(daemon._build_health_payload(_svc(True), False))[-2:] == ["ready", "pending"]


def test_a_stand_in_without_the_predicate_is_not_initialized(no_store):
    assert _readiness(daemon._build_health_payload(SimpleNamespace(), False)) == (False, ["init"])


def test_the_snapshot_reports_its_first_read():
    from pseudolife_memory.principal_store import PrincipalSnapshot

    snapshot = PrincipalSnapshot()
    assert not snapshot.loaded()
    snapshot.refresh(lambda: ([], None))
    assert snapshot.loaded()


# ---- the client half -------------------------------------------------------

def _serve(answers):
    """A /health server replaying ``answers`` ((status, body) pairs; the last
    repeats). Returns (server, base_url, hits)."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            status, body = answers[min(len(hits), len(answers) - 1)]
            hits.append(status)
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}", hits


def test_wait_ready_skips_every_not_ready_answer():
    answers = [(503, {"status": "degraded", "ready": False}),
               (200, {"status": "ok"}),
               (200, {"status": "ok", "ready": False}),
               (200, {"status": "ok", "ready": True})]
    server, url, hits = _serve(answers)
    try:
        assert readiness.wait_ready(url, 20, poll=0.01)["ready"] is True
        assert hits == [503, 200, 200, 200]
    finally:
        server.shutdown()
        server.server_close()


def test_wait_ready_for_stages_accepts_any_status_once_they_are_done():
    answers = [(200, {"status": "ok"}),
               (503, {"status": "degraded", "ready": False, "pending": ["init", "principals"]}),
               (503, {"status": "degraded", "ready": False, "pending": ["init"]})]
    server, url, hits = _serve(answers)
    try:
        body = readiness.wait_ready(url, 20, stages=("principals",), poll=0.01)
        assert body["pending"] == ["init"] and hits == [200, 503, 503]
        # No stages: the first HTTP answer, whatever it says.
        assert readiness.wait_ready(url, 20, stages=(), poll=0.01) is not None
        with pytest.raises(ValueError, match="unknown readiness stages"):
            readiness.wait_ready(url, 20, stages=("storage",))
    finally:
        server.shutdown()
        server.server_close()


def test_wait_ready_lets_check_refuse_an_answer():
    server, url, _hits = _serve([(200, {"status": "ok", "ready": True, "pending": []})])

    def refuse(status, body):
        raise RuntimeError("not my daemon")
    try:
        with pytest.raises(RuntimeError, match="not my daemon"):
            readiness.wait_ready(url, 20, check=refuse)
    finally:
        server.shutdown()
        server.server_close()


def test_wait_ready_names_the_last_answer_at_the_deadline():
    server, url, _hits = _serve([(200, {"status": "ok"})])
    try:
        with pytest.raises(readiness.NotReady, match="no 'ready' key"):
            readiness.wait_ready(url, 0.3, poll=0.01)
    finally:
        server.shutdown()
        server.server_close()


def test_wait_ready_stops_when_the_daemon_exits():
    with pytest.raises(readiness.NotReady, match="exited"):
        readiness.wait_ready("http://127.0.0.1:9", 30, alive=lambda: False)


# ---- end to end: a real daemon whose stored principals load late -----------

TOKEN = "readiness-contract-token"

# The daemon, with its first stored-principal read held until the test
# creates the gate file: the window in which /health already answers, and
# even init has finished, but a bearer the environment does not match is
# refused 503 principals_unavailable.
_GATED_PRINCIPALS = """
import os, sys, time
from pseudolife_memory import principal_store
original = principal_store.PrincipalRefresher.refresh_once
gate = os.environ["PL_TEST_PRINCIPALS_GATE"]
def refresh_once(self):
    deadline = time.monotonic() + 600
    while not os.path.exists(gate) and time.monotonic() < deadline:
        time.sleep(0.05)
    return original(self)
principal_store.PrincipalRefresher.refresh_once = refresh_once
from pseudolife_memory.cli import main
sys.argv = ["pseudolife-mcp", "serve"]
main()
"""


def _unknown_bearer_status(url: str) -> tuple[int, dict]:
    request = urllib.request.Request(url + "/api/search?q=",
                                     headers={"Authorization": "Bearer matches-nothing"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_spawn_serve_waits_for_the_stored_principals_after_init(tmp_path, monkeypatch):
    from tests import helpers
    from tests.helpers import pg_reachable, private_bank, spawn_serve, stop_daemon
    from tests.pg_fixtures import resolve_test_db_url

    url = resolve_test_db_url()
    if not pg_reachable(url):
        pytest.skip("no test Postgres reachable")
    port = helpers.free_port()
    monkeypatch.setattr(helpers, "free_port", lambda: port)
    gate = tmp_path / "principals-gate"
    base = f"http://127.0.0.1:{port}"
    with private_bank(url, "readiness") as bank_url:
        result = {}

        def spawn():
            try:
                result["started"] = spawn_serve(
                    tmp_path, bank_url, wait_s=240, argv=("-c", _GATED_PRINCIPALS),
                    env_extra={"PSEUDOLIFE_MCP_TOKEN": TOKEN, "PL_TEST_PRINCIPALS_GATE": str(gate)})
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                result["error"] = exc
        worker = threading.Thread(target=spawn, daemon=True)
        worker.start()
        try:
            try:
                # Init finishes while the principals stay held: the helper
                # must still be waiting, on that stage alone.
                body = readiness.wait_ready(base, 240, stages=("init",))
                assert body["pending"] == ["principals"], body
                time.sleep(1.0)  # several of spawn_serve's 0.5 s polls
                assert worker.is_alive() and not result, (
                    "spawn_serve returned while the stored principals were still loading")
                assert _unknown_bearer_status(base)[0] == 503
            finally:
                gate.touch()
                worker.join(timeout=240)
            if "error" in result:
                raise result["error"]
            _proc, health, _port = result["started"]
            assert (health.get("ready"), health.get("pending")) == (True, [])
            status, refused = _unknown_bearer_status(base)
            assert (status, refused.get("error")) == (401, "unauthorized")
        finally:
            # Whichever assertion failed, the gate is open, so the daemon
            # finished starting: stop it before its bank is dropped.
            if "started" in result:
                stop_daemon(result["started"][0])
