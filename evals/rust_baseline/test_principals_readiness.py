"""A launched daemon is ready only once its /health says ``ready: true``
(pseudolife_memory/readiness.py): initialized and its stored-principal view
loaded. Before that, a bearer no token matches is refused 503
principals_unavailable instead of 401 (hosted Windows run 39116824665,
full_bank search-wrong-bearer)."""
import sys
import time
from unittest.mock import patch

import httpx
import pytest

from . import daemon
from .test_baseline import launcher_directory

# The first ``early`` /health answers are not ready (``early_status``, with
# or without the ``ready`` key), later ones are; /api/search refuses 503
# principals_unavailable until then, like a daemon whose principal refresher
# has not finished its first read. Counted, not timed, so a slow runner
# cannot close the window before the launcher looks.
STAND_IN = """
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
early, early_status, early_ready = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
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
        with lock:
            ready = seen[0] >= early
            if self.path == '/health':
                seen[0] += 1
        if self.path == '/health':
            payload = {'status': 'ok', 'baseline_instance': {
                'nonce': os.environ.get('PSEUDOLIFE_BASELINE_NONCE'), 'pid': os.getpid()}}
            if ready:
                return self.answer(200, dict(payload, ready=True))
            if early_ready != 'absent':
                payload['ready'] = False
            if early_status != 200:
                payload['status'] = 'degraded'
            return self.answer(early_status, payload)
        if not ready:
            return self.answer(503, {'error': 'principals_unavailable'})
        return self.answer(401, {'error': 'unauthorized'})
ThreadingHTTPServer(('127.0.0.1', int(os.environ['PSEUDOLIFE_MCP_PORT'])), Handler).serve_forever()
"""


def stand_in(early, status=200, ready="false"):
    return [sys.executable, "-c", STAND_IN, str(early), str(status), ready]


def wrong_bearer(url):
    response = httpx.get(url + "/api/search?q=apricot", headers={"Authorization": "Bearer synthetic-invalid"},
                         trust_env=False, timeout=5)
    return response.status_code, response.json().get("error")


@pytest.mark.parametrize("status, ready", [(200, "false"), (503, "false"), (200, "absent")])
def test_launch_waits_for_ready(status, ready):
    with launcher_directory() as directory, \
            patch.object(daemon, "_daemon_command", return_value=stand_in(3, status, ready)):
        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=30) as (_, url, cleanup):
            assert wrong_bearer(url) == (401, "unauthorized")
            assert cleanup["health_answers_before_ready"] == 3


def test_a_daemon_that_never_becomes_ready_fails_the_launch():
    started = time.monotonic()
    with launcher_directory() as directory, \
            patch.object(daemon, "_daemon_command", return_value=stand_in(10**9)), \
            pytest.raises(RuntimeError, match="startup deadline exceeded.*ready False"):
        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=3):
            pass
    assert time.monotonic() - started < 30
