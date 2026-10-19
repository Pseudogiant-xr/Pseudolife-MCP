"""A launched daemon is started only once it can serve: its /health says
``ready: true`` (the readiness contract, initialized and stored principals
loaded), or, on an oracle older than the contract, its stored-principal view
has loaded. Before that, a bearer no token matches is refused 503
principals_unavailable instead of 401 (hosted Windows run 39116824665,
full_bank search-wrong-bearer)."""
import sys
import time
from unittest.mock import patch

import httpx
import pytest

from . import daemon
from .test_baseline import launcher_directory

# mode "contract": the first ``early`` /health answers are not ready
# (``early_status``, ``ready: false``), later ones are; /api/search refuses
# 503 principals_unavailable until then. mode "legacy" (an oracle older than
# the contract): /health is ok with no ``ready`` key at once, and the first
# ``early`` searches are refused (``early_status`` / ``early_error``). Counted,
# not timed, so a slow runner cannot close the window before the launcher looks.
STAND_IN = """
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
mode, early, early_status, early_error = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
health, searches, lock = [0], [0], threading.Lock()
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def answer(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        identity = {'nonce': os.environ.get('PSEUDOLIFE_BASELINE_NONCE'), 'pid': os.getpid()}
        with lock:
            if self.path == '/health':
                health[0] += 1
                seen = health[0]
            else:
                searches[0] += 1
                seen = searches[0]
            ready = health[0] > early
        if self.path == '/health':
            if mode == 'legacy':
                return self.answer(200, {'status': 'ok', 'baseline_instance': identity})
            if seen > early:
                return self.answer(200, {'status': 'ok', 'ready': True, 'pending': [],
                                         'baseline_instance': identity})
            return self.answer(early_status, {'status': 'ok' if early_status == 200 else 'degraded',
                                              'ready': False, 'pending': ['principals'],
                                              'baseline_instance': identity})
        if (mode == 'legacy' and seen <= early) or (mode == 'contract' and not ready):
            return self.answer(early_status if mode == 'legacy' else 503,
                               {'error': early_error if mode == 'legacy' else 'principals_unavailable'})
        return self.answer(401, {'error': 'unauthorized'})
ThreadingHTTPServer(('127.0.0.1', int(os.environ['PSEUDOLIFE_MCP_PORT'])), Handler).serve_forever()
"""


def stand_in(early, status=200, error="principals_unavailable", mode="contract"):
    return [sys.executable, "-c", STAND_IN, mode, str(early), str(status), error]


def wrong_bearer(url):
    response = httpx.get(url + "/api/search?q=apricot", headers={"Authorization": "Bearer synthetic-invalid"},
                         trust_env=False, timeout=5)
    return response.status_code, response.json().get("error")


@pytest.mark.parametrize("status", [200, 503])
def test_launch_waits_for_ready(status):
    with launcher_directory() as directory, \
            patch.object(daemon, "_daemon_command", return_value=stand_in(3, status)):
        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=30) as (_, url, cleanup):
            assert wrong_bearer(url) == (401, "unauthorized")
            assert cleanup["health_answers_before_ready"] == 3
            assert "legacy_startup" not in cleanup


def test_a_daemon_that_never_becomes_ready_fails_the_launch():
    started = time.monotonic()
    with launcher_directory() as directory, \
            patch.object(daemon, "_daemon_command", return_value=stand_in(10**9)), \
            pytest.raises(RuntimeError, match="startup deadline exceeded.*ready False"):
        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=3):
            pass
    assert time.monotonic() - started < 30


def test_an_oracle_older_than_the_contract_waits_for_its_stored_principals():
    # The merge-base oracle CI selects for a pull request may predate /health
    # `ready`: its own protocol is the principals probe.
    with launcher_directory() as directory, \
            patch.object(daemon, "_daemon_command", return_value=stand_in(3, 503, mode="legacy")):
        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=30) as (_, url, cleanup):
            assert wrong_bearer(url) == (401, "unauthorized")
            assert cleanup["legacy_startup"] is True
            assert cleanup["principals_unavailable_before_ready"] == 3


def test_an_old_oracle_whose_view_never_loads_fails_the_launch():
    with launcher_directory() as directory, \
            patch.object(daemon, "_daemon_command", return_value=stand_in(10**9, 503, mode="legacy")), \
            pytest.raises(RuntimeError, match="stored-principal view did not load"):
        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=3):
            pass


@pytest.mark.parametrize("status, error", [(503, "memory bank not ready"), (500, "principals_unavailable"),
                                           (200, "")])
def test_only_the_exact_refusal_delays_an_old_oracle(status, error):
    # The near miss: another 503, or the code under another status, is the
    # daemon's own answer for the corpus contract to judge, not a load wait.
    with launcher_directory() as directory, \
            patch.object(daemon, "_daemon_command", return_value=stand_in(10**9, status, error, mode="legacy")):
        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=30) as (_, url, cleanup):
            assert cleanup["principals_unavailable_before_ready"] == 0
