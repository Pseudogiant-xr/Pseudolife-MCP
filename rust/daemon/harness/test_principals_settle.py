"""Scenarios whose bearers reach the stored-principal view wait for its
first read: before it, both daemons answer principals_unavailable."""
import pytest

import run
from pseudolife_memory.principals import env_auth, resolve_principal_detailed

UNAVAILABLE = {"status": 503, "json": {"error": "principals_unavailable"}}
REFUSED = {"status": 401, "json": {"error": "unauthorized"}}


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


def test_unsettled_scenarios_reaching_the_store_wait_for_it():
    reaching = {}
    for name, cls in run.SCENARIOS.items():
        scn = cls()
        if scn.settle or scn.unreachable_database:
            continue
        found = store_bearers(scn)
        if found:
            reaching[name] = found
            assert scn.principals_settle, f"{name}: {found} reach the stored principals unwaited"
    # The near miss that flaked on hosted runners (run 38855230133).
    assert reaching["encodings"] == ["non-ASCII bearer matching nothing"]


def test_wait_principals_waits_through_the_unavailable_window(monkeypatch):
    answers = iter([UNAVAILABLE, UNAVAILABLE, REFUSED])
    calls = []

    def fake_call(port, method, path, headers=(), **kw):
        calls.append((port, path, headers))
        return next(answers)

    monkeypatch.setattr(run, "call", fake_call)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    run.wait_principals([5555])
    assert len(calls) == 3
    assert calls[0] == (5555, "/api/search?q=", [("Authorization", run.PRINCIPAL_PROBE)])


def test_other_refusals_count_as_loaded(monkeypatch):
    # A 503 for another reason is the scenario's to compare, not a wait.
    monkeypatch.setattr(run, "call", lambda *a, **k: {"status": 503, "json": {"error": "db_unavailable"}})
    assert run.principals_ready(5555)


def test_wait_principals_gives_up(monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(run, "call", lambda *a, **k: UNAVAILABLE)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(run.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not load within 60"):
        run.wait_principals([5555], timeout=60)
