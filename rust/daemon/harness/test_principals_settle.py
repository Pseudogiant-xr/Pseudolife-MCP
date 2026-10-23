"""Daemons start ready: ``Daemon.start`` waits for /health ``ready: true``
(pseudolife_memory/readiness.py), which both daemons report only once
initialized with their stored-principal view loaded. Before that view's
first read, a bearer no environment token matches is answered 503
principals_unavailable instead of 401, which scenarios once had to wait for
one by one (PRs #826, #827)."""
import json
import os
import socket
import sys
import urllib.request

import pytest

import daemons
import run
from pseudolife_memory.principals import env_auth, resolve_principal_detailed


def store_bearers(scn) -> list[str]:
    """Cases whose bearer the environment does not match, so Python and
    Rust consult the stored-principal view (principals.py)."""
    mapped, singular = env_auth(scn.env)
    if not mapped and singular is None:
        return []
    names = []
    for c in scn.cases():
        for key, value in c["headers"]:
            if key.lower() != "authorization":
                continue
            text = value.decode("latin-1") if isinstance(value, bytes) else value
            scheme, _, presented = text.partition(" ")
            if scheme.lower() == "bearer" and presented.strip(" \t") \
                    and resolve_principal_detailed(text, mapped, singular)[0] is None:
                names.append(c["name"])
    return names


def test_scenarios_reaching_the_store_start_ready():
    # An unreachable bank's view never loads, so principals_unavailable is
    # its recorded answer. Blind spot: only cases() is read, so a timeline
    # scenario's own requests are not.
    reaching = {}
    for name, cls in run.SCENARIOS.items():
        scn = cls()
        if scn.unreachable_database:
            continue
        found = store_bearers(scn)
        if found:
            reaching[name] = found
            assert scn.start_stages is None or "principals" in scn.start_stages, (
                f"{name}: {found} reach the stored principals before they load")
    # The near miss that flaked on hosted runners (run 38855230133).
    assert reaching["encodings"] == ["non-ASCII bearer matching nothing"]
    assert "stored principal" in reaching["tokens"]


def test_only_never_ready_scenarios_wait_for_less_than_ready():
    # Opting out is for daemons that cannot become ready by design, and
    # each still waits for every stage it can finish, so /health `pending`
    # never depends on how fast the runner is.
    for name, cls in run.SCENARIOS.items():
        scn = cls()
        if scn.start_stages is None:
            continue
        assert scn.unreachable_database or scn.hold_lease or not scn.snapshot_settle, name
        reachable = () if scn.unreachable_database else ("principals",)
        assert scn.start_stages == reachable, name


@pytest.mark.parametrize("settle", [False, True])
def test_settle_before_cases_runs_only_the_declared_settle(monkeypatch, settle):
    waits = []
    monkeypatch.setattr(run, "wait_settled", lambda ports, **kw: waits.append((ports, kw)))
    scn = run.Scenario()
    scn.env = {"PSEUDOLIFE_MCP_TOKEN": "tok"}
    scn.settle = settle
    run.settle_before_cases(scn, [5555])
    assert waits == ([([5555], {"token": "tok", "check_db": True})] if settle else [])


# ---- Daemon.start against a stand-in daemon ---------------------------------

# Answers /health like a daemon that starts in stages: the first half of the
# `early` answers are a 503 with both stages pending, the rest `ready: false`
# with only init pending, then `ready: true`; every answer is logged to a
# file, so the test sees how many came before start returned.
STAND_IN = """
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
early, log = int(sys.argv[1]), sys.argv[2]
seen = [0]
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        seen[0] += 1
        if seen[0] <= early // 2:
            status, body = 503, {'status': 'degraded', 'ready': False, 'pending': ['init', 'principals']}
        elif seen[0] <= early:
            status, body = 200, {'status': 'ok', 'ready': False, 'pending': ['init']}
        else:
            status, body = 200, {'status': 'ok', 'ready': True, 'pending': []}
        with open(log, 'a') as f:
            f.write(json.dumps(body['ready']) + '\\n')
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
"""


def stand_in(tmp_path, early):
    port = daemons.free_port()
    log = tmp_path / "answers.log"
    env = dict(os.environ, PORT=str(port))
    return daemons.Daemon("stand-in", [sys.executable, "-c", STAND_IN, str(early), str(log)],
                          env, tmp_path, port, tmp_path / "stand-in.log"), log


def answers(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_start_waits_through_every_not_ready_answer(tmp_path):
    d, log = stand_in(tmp_path, early=6)
    try:
        d.start(30)
        # Returned on the first ready answer, not on the first 503.
        assert answers(log) == [False] * 6 + [True]
    finally:
        d.stop()


def test_start_with_no_stages_returns_on_the_first_answer(tmp_path):
    d, log = stand_in(tmp_path, early=6)
    try:
        d.start(30, stages=())
        assert answers(log) == [False]
    finally:
        d.stop()


def test_start_for_a_stage_returns_once_it_is_no_longer_pending(tmp_path):
    d, log = stand_in(tmp_path, early=6)
    try:
        d.start(30, stages=("principals",))
        # The 503s list both stages; the ready-false answers only init.
        assert answers(log) == [False] * 4
    finally:
        d.stop()


def test_a_daemon_that_never_becomes_ready_fails_start(tmp_path):
    d, _log = stand_in(tmp_path, early=10**9)
    with pytest.raises(RuntimeError, match=r"did not report ready within 2s.*pending \['init', 'principals'\]"):
        d.start(2)
    assert d.proc is None  # stopped by PID before raising
