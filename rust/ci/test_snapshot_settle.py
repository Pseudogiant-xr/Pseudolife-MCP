"""Early HTTP witnesses finish before readiness gates the bank snapshot."""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rust" / "daemon" / "harness"))
import run


@pytest.fixture
def snapshot_harness(monkeypatch, tmp_path):
    events = []
    initialized = set()
    empty = {"catalog": {"sequences": []}, "rows": {}}
    complete = dict(empty, catalog={"sequences": [["public", "entries_id_seq", "bigint", 1, 1, None]]})
    ports = {"python": 1, "rust": 2}

    def daemon(side, home):
        return SimpleNamespace(port=ports[side], cwd=home,
                               launch=lambda: None,
                               wait=lambda timeout, stages=None: events.append(("start", side, stages)),
                               stop=lambda: events.append(("stop", side)))

    def dump(dsn):
        events.append(("dump", dsn))
        return copy.deepcopy(complete if dsn in initialized else empty)

    def wait(selected, **kwargs):
        events.append(("wait", selected, kwargs))
        initialized.update(side for side, port in ports.items() if port in selected)

    def call(port, method, path, headers=(), *args, **kwargs):
        events.append(("request", port))
        return {"status": 200, "headers": {}, "json": {"ok": True}}

    monkeypatch.setattr(run.pg, "name", lambda label: label)
    monkeypatch.setattr(run.pg, "create", lambda name, **kw: name)
    monkeypatch.setattr(run.pg, "dsn", lambda name: "rust" if name.endswith("_rs") else "python")
    monkeypatch.setattr(run.daemons, "make_home", lambda *args: tmp_path)
    monkeypatch.setattr(run.daemons, "python_daemon", lambda home, *args: daemon("python", home))
    monkeypatch.setattr(run.daemons, "rust_daemon", lambda binary, home, *args: daemon("rust", home))
    monkeypatch.setattr(run.dbstate, "dump", dump)
    monkeypatch.setattr(run, "wait_settled", wait)
    monkeypatch.setattr(run, "call", call)
    monkeypatch.setattr(run, "GOLDENS", tmp_path / "goldens")
    run.GOLDENS.mkdir()
    return events, complete


@pytest.mark.parametrize("scenario", [run.PairBudget, run.Encodings, run.MapOrder, run.MapOrderReversed])
@pytest.mark.parametrize("mode", ["live", "golden"])
def test_early_requests_then_settle_then_snapshot_and_record(snapshot_harness, tmp_path, scenario, mode):
    events, complete = snapshot_harness
    scn = scenario()
    cases = scn.cases()
    if mode == "golden":
        # Preserve the real recorder/comparator; only the daemons and PG are fake.
        rows = [{"_python": {"status": 200, "headers": {}, "json": {"ok": True}}} for c in cases]
        run.save_golden(scn.name, rows, complete)
    result = run._run_scenario(scn, tmp_path / "daemon", tmp_path, mode, mode == "live")
    assert not result["db_diffs"]
    wait_index = next(i for i, event in enumerate(events) if event[0] == "wait")
    assert all(i < wait_index for i, event in enumerate(events) if event[0] == "request")
    assert all(i > wait_index for i, event in enumerate(events) if event[0] == "stop")
    assert all(i > wait_index for i, event in enumerate(events) if event[0] == "dump" and event[1] in {"python", "rust"})
    expected_ports = [1, 2] if mode == "live" else [2]
    assert events[wait_index][1] == expected_ports
    assert events[wait_index][2]["timeout"] > 0
    expected_token = scn.env.get("PSEUDOLIFE_MCP_TOKEN")
    if isinstance(scn, run.MapOrder):
        expected_token = scn.tokens.split(",")[0].rpartition(":")[0]
    assert events[wait_index][2]["token"] == expected_token
    if mode == "live":
        assert run.load_golden(scn.name)["db_state"] == run.golden_scrub(complete)


@pytest.mark.parametrize("scenario", [run.DbDown, run.BodyLimits, run.StaticBuild,
                                      run.SecurityOpen, run.DimMismatch, run.StampedBank,
                                      run.NullEmbedding, run.UnconstrainedDims,
                                      run.SCENARIOS["sweep-pruning"], run.SCENARIOS["sweep-recovery"]])
def test_uninitializable_scenarios_keep_bank_comparisons_without_wait(snapshot_harness, tmp_path, scenario):
    events, _ = snapshot_harness
    scn = scenario()
    scn.prepare_template = lambda dsn: None
    scn.timeline = lambda procs, holders: []
    scn.cases = lambda: [run.case("refusal witness", "GET", "/nope")]
    scn.normalize_db = lambda state, before, side: state
    result = run._run_scenario(scn, tmp_path / "daemon", tmp_path, "live", False)
    assert not any(event[0] == "wait" for event in events)
    assert not result["db_diffs"]
    snapshots = [event[1] for event in events if event[0] == "dump"]
    if scenario is run.DbDown:
        assert len(snapshots) == 1
    else:
        assert snapshots[-2:] == ["python", "rust"]


def test_snapshot_timeout_is_a_failure_and_stops_both_arms(snapshot_harness, tmp_path, monkeypatch):
    events, _ = snapshot_harness
    monkeypatch.setattr(run, "wait_settled", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("not settled")))
    with pytest.raises(RuntimeError, match="not settled"):
        run._run_scenario(run.PairBudget(), tmp_path / "daemon", tmp_path, "live", False)
    assert {event[1] for event in events if event[0] == "stop"} == {"python", "rust"}
    assert not any(event[0] == "dump" and event[1] in {"python", "rust"} for event in events)


@pytest.mark.parametrize("scenario", [run.DimMismatch, run.StampedBank, run.NullEmbedding,
                                      run.UnconstrainedDims, run.BodyLimits, run.SecurityOpen,
                                      run.SCENARIOS["sweep-pruning"], run.SCENARIOS["sweep-recovery"]])
def test_no_wait_policy_still_detects_bank_changes(snapshot_harness, tmp_path, monkeypatch, scenario):
    scn = scenario()
    scn.prepare_template = lambda dsn: None
    scn.timeline = lambda procs, holders: []
    scn.cases = lambda: [run.case("refusal witness", "GET", "/nope")]
    scn.normalize_db = lambda state, before, side: state
    dump = run.dbstate.dump

    def changed(dsn):
        state = dump(dsn)
        if dsn == "rust":
            state["catalog"]["unexpected_table"] = ["mutation"]
        return state

    monkeypatch.setattr(run.dbstate, "dump", changed)
    result = run._run_scenario(scn, tmp_path / "daemon", tmp_path, "live", False)
    assert result["db_diffs"]


def test_dim_mismatch_waits_for_rust_to_refuse_before_its_first_case(snapshot_harness, tmp_path, monkeypatch):
    """Rust records its init refusal in a warmup task that starts after its
    listener answers, and /health says ok until then. Golden mode has no
    Python arm to delay the first case, so the scenario must wait for the
    refusal itself: Linux run 38699015278 compared Rust's pre-warmup 200 ok
    with the recorded 503."""
    events, _ = snapshot_harness
    refusal = {"status": 503, "headers": {}, "json": {"status": "degraded", "init_refusal": "dimension"}}
    warming = {"status": 200, "headers": {}, "json": {"status": "ok"}}
    rust_health = []

    def call(port, method, path, *args, **kwargs):
        events.append(("request", port))
        if port == 2 and path == "/health":
            rust_health.append(path)
            if len(rust_health) <= 2:
                return copy.deepcopy(warming)
        return copy.deepcopy(refusal)

    monkeypatch.setattr(run, "call", call)
    scn = run.DimMismatch()
    scn.prepare_template = lambda dsn: None
    run.save_golden(scn.name, [{"_python": run.normalize_response(refusal, c["path"], [])}
                               for c in scn.cases()], None)
    result = run._run_scenario(scn, tmp_path / "daemon", tmp_path, "golden", False)
    assert [c["diffs"] for c in result["cases"]] == [[], []]
    assert len(rust_health) > 2
