"""A launched daemon is ready only once its stored-principal view has loaded:
before that, a bearer no token matches is refused 503 principals_unavailable
instead of 401 (hosted Windows run 39116824665, full_bank search-wrong-bearer)."""
import json
import sys
import time
from unittest.mock import patch

import httpx
import pytest

from . import daemon
from .test_baseline import launcher_directory

# /health answers at once; the first ``early`` /api/search requests get
# ``early_status``/``early_error``, later ones 401, like a daemon whose
# principal refresher has not finished its first read yet. Counted, not
# timed, so a slow runner cannot close the window before the probe arrives.
STAND_IN = """
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
early, early_status, early_error = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
seen, lock = [0], threading.Lock()
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def answer(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path == '/health':
            return self.answer(200, {'status': 'ok', 'baseline_instance': {
                'nonce': os.environ.get('PSEUDOLIFE_BASELINE_NONCE'), 'pid': os.getpid()}})
        with lock:
            seen[0] += 1
            refused = seen[0] <= early
        if refused:
            return self.answer(early_status, {'error': early_error})
        return self.answer(401, {'error': 'unauthorized'})
ThreadingHTTPServer(('127.0.0.1', int(os.environ['PSEUDOLIFE_MCP_PORT'])), Handler).serve_forever()
"""


def stand_in(early, status=503, error="principals_unavailable"):
    return [sys.executable, "-c", STAND_IN, str(early), str(status), error]


def wrong_bearer(url):
    response = httpx.get(url + "/api/search?q=apricot", headers={"Authorization": "Bearer synthetic-invalid"},
                         trust_env=False, timeout=5)
    return response.status_code, response.json().get("error")


def test_launch_waits_for_the_stored_principal_view():
    with launcher_directory() as directory, \
            patch.object(daemon, "_daemon_command", return_value=stand_in(3)):
        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=30) as (_, url, cleanup):
            assert wrong_bearer(url) == (401, "unauthorized")
            assert cleanup["principals_unavailable_before_ready"] == 3
            assert "principals_unavailable_before_ready" in json.loads(json.dumps(cleanup))


def test_view_that_never_loads_fails_the_launch():
    started = time.monotonic()
    with launcher_directory() as directory, \
            patch.object(daemon, "_daemon_command", return_value=stand_in(10**9)), \
            pytest.raises(RuntimeError, match="stored-principal view did not load"):
        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=3):
            pass
    assert time.monotonic() - started < 30


@pytest.mark.parametrize("status, error", [(503, "memory bank not ready"), (500, "principals_unavailable"),
                                           (200, "")])
def test_only_the_exact_refusal_delays_readiness(status, error):
    # The near miss: another 503, or the code under another status, is the
    # daemon's own answer for the corpus contract to judge, not a load wait.
    with launcher_directory() as directory, \
            patch.object(daemon, "_daemon_command", return_value=stand_in(10**9, status, error)):
        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=30) as (_, url, cleanup):
            assert cleanup["principals_unavailable_before_ready"] == 0
