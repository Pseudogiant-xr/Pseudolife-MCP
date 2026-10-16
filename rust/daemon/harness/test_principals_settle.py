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


def test_scenarios_reaching_the_store_wait_for_it():
    # Settled scenarios too: the readiness settle searches with an environment
    # token, which never consults the stored view. An unreachable bank's view
    # never loads, so principals_unavailable is its recorded answer. Blind
    # spot: only cases() is read, so a timeline scenario's own requests are not.
    reaching = {}
    for name, cls in run.SCENARIOS.items():
        scn = cls()
        if scn.unreachable_database:
            continue
        found = store_bearers(scn)
        if found:
            reaching[name] = found
            assert scn.principals_settle, f"{name}: {found} reach the stored principals unwaited"
    # The near miss that flaked on hosted runners (run 38855230133).
    assert reaching["encodings"] == ["non-ASCII bearer matching nothing"]
    assert "stored principal" in reaching["tokens"]


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


@pytest.mark.parametrize("answer", [{"status": 200, "json": {"entries": []}},
                                    {"status": 503, "json": {"error": "db_unavailable"}}])
def test_any_other_answer_is_a_misconfigured_scenario(monkeypatch, answer):
    # 200 means an open daemon ran the search: the probe would not be inert.
    monkeypatch.setattr(run, "call", lambda *a, **k: answer)
    with pytest.raises(RuntimeError, match="expected 401 or 503 principals_unavailable"):
        run.principals_ready(5555)


@pytest.mark.parametrize("settle,principals,expected", [
    (False, False, []), (True, False, ["settled"]), (False, True, ["principals"]),
    (True, True, ["settled", "principals"])])
def test_settle_before_cases_runs_the_declared_waits(monkeypatch, settle, principals, expected):
    waits = []
    monkeypatch.setattr(run, "wait_settled",
                        lambda ports, **kw: waits.append(("settled", ports, kw)))
    monkeypatch.setattr(run, "wait_principals", lambda ports: waits.append(("principals", ports)))
    scn = run.Scenario()
    scn.env = {"PSEUDOLIFE_MCP_TOKEN": "tok"}
    scn.settle, scn.principals_settle = settle, principals
    run.settle_before_cases(scn, [5555])
    assert [w[0] for w in waits] == expected
    for w in waits:
        assert w[1] == [5555]
        if w[0] == "settled":
            assert w[2] == {"token": "tok", "check_db": not scn.settle_with_search}


def test_wait_principals_gives_up(monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(run, "call", lambda *a, **k: UNAVAILABLE)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(run.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not load within 60"):
        run.wait_principals([5555], timeout=60)
