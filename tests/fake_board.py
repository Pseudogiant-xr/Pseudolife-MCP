"""A scripted agent board for the lease CLI and the suite lock's mirror.

The coordination REST actions a lease client calls (``register``, ``lease``,
``release``, ``leases``, ``agents``, ``send``), each answered from a list of
replies behind an ``httpx.MockTransport``: nothing here reaches a daemon.
Shared by tests/test_lease_cli.py and tests/test_suite_lock.py.
"""
from __future__ import annotations

import json
import time

import httpx

TOKEN = "tok-SECRET-bearer-5b1c"
CREDENTIAL = "cred-SECRET-instance-9f3e"
AGENT = "0123456789abcdef0123456789abcdef"


def holder_record(label="other-run", purpose="nightly eval", age=240.0, expected=600.0):
    now = time.time()
    return {"agent_id": "f" * 32, "label": label, "principal": "reviewer",
            "purpose": purpose, "acquired_at": now - age, "expires_at": now + 100,
            "expected_end": None if expected is None else now + expected}


def HELD(name="gpu"):
    now = time.time()
    return 200, {"name": name, "state": "held", "fence": 7, "expires_at": now + 120,
                 "expected_end": None, "position": None, "queued": 0,
                 "holder": {"agent_id": AGENT, "label": "lease-run", "principal": "default",
                            "purpose": "", "acquired_at": now, "expires_at": now + 120,
                            "expected_end": None}}


def QUEUED(position=2, queued=2, holder="default", name="gpu"):
    return 200, {"name": name, "state": "queued", "fence": None, "expires_at": None,
                 "expected_end": None, "position": position, "queued": queued,
                 "holder": holder_record() if holder == "default" else holder}


def peer(agent_id, label, status, *, project="Pseudolife-MCP", lifecycle="attached",
         **extra):
    """One row of the board's peer list, shaped as ``agents`` returns it."""
    return {"agent_id": agent_id, "principal": "claude-code", "label": label,
            "project": project, "task": "", "status": status, "episode": "",
            "capabilities": {}, "wake_enabled": False, "created_at": 0.0,
            "last_activity": time.time(), "lifecycle": lifecycle,
            "adapter_available": lifecycle == "attached", **extra}


def AGENTS(*peers):
    return 200, {"agents": list(peers), "truncated": False, "idle_omitted": 0,
                 "leases": [], "leases_truncated": False}


def SENT():
    return 200, {"message_id": "m" * 32, "state": "queued", "created_at": time.time(),
                 "expires_at": time.time() + 86400, "acknowledged_at": None}


class FakeDaemon:
    """The coordination REST actions the lease CLI calls, each scripted as a
    list of replies: ``(status, json)``, an exception to raise, or a callable
    taking the request. The last reply of a script repeats."""

    def __init__(self, *, register=None, lease=None, release=None, leases=None,
                 agents=None, send=None):
        self.calls: list[tuple[str, httpx.Headers, dict, float]] = []
        self.scripts = {
            "register": list(register or [(200, {"agent_id": AGENT, "credential": CREDENTIAL,
                                                 "label": "lease-run"})]),
            "lease": list(lease or [HELD()]),
            "release": list(release or [(200, {"name": "gpu", "released": True,
                                               "dequeued": False})]),
            "leases": list(leases or [(200, {"leases": [], "truncated": False})]),
            "agents": list(agents or [AGENTS()]),
            "send": list(send or [SENT()]),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.startswith("/api/coordination/")
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        self.calls.append((action, request.headers, body, time.time()))
        script = self.scripts[action]
        reply = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(request)
        status, payload = reply
        return httpx.Response(status, json=payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def actions(self) -> list[str]:
        return [call[0] for call in self.calls]

    def bodies(self, action: str) -> list[dict]:
        return [call[2] for call in self.calls if call[0] == action]


def released_last(daemon: FakeDaemon) -> bool:
    """Whether the board lease was released and nothing followed but the
    released notice (the peer listing and sends): the board lets go first,
    so a peer told "released" finds the lease free."""
    actions = daemon.actions()
    if "release" not in actions:
        return False
    last = len(actions) - 1 - actions[::-1].index("release")
    return all(action in ("agents", "send") for action in actions[last + 1:])
