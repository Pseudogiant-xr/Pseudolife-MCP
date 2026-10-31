"""Console dream/run differential cases, including the shared single flight.

Hold the first extractor request with events, rather than relying on a sleep
or a slow model, then exercise the Console and MCP refusals on that flight.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from w2e_episodes import rest, run_rest

PATH = "/api/dream/run"


def concurrent(port, dsn, client, stub, h):
    entered, release = threading.Event(), threading.Event()
    original = stub._handle

    def blocked(handler, method):
        if method == "POST" and handler.path.endswith("/chat/completions"):
            entered.set()
            if not release.wait(60):
                raise TimeoutError("dream fixture was not released")
        return original(handler, method)

    stub._handle = blocked
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(run_rest, rest(PATH), port, dsn, h)
            try:
                if not entered.wait(30):
                    raise AssertionError("first Console dream never reached the extractor")
                refused = run_rest(rest(PATH), port, dsn, h)
                mcp = client.call("memory_dream", {"action": "run"}, [])
                expected = {"skipped": "dream_in_progress", "pulled": 0,
                            "claims": 0, "extractor": "primary"}
                assert refused == {"isError": False, "status": 200, "value": expected}, refused
                assert mcp == {"isError": False, "value": expected}, mcp
            finally:
                release.set()
            result = first.result(timeout=60)
        return {"isError": False, "value": {"first": result, "console": refused, "mcp": mcp}}
    finally:
        release.set()
        stub._handle = original


def seed_replay(dsn, golden):
    """Replay the oracle's initial random secret and migration stamp.

    Only constructor metadata is restored, before Rust starts; all changes
    during the replay still compare against the recorded oracle.
    """
    import psycopg
    from psycopg.types.json import Jsonb

    meta = golden["steps"][0]["state"]["rows"]["public.meta"]
    with psycopg.connect(dsn) as conn:
        for key, value in meta["rows"]:
            if key in ("dream_ack_secret_v1", "curation_listing_spelling_v2"):
                conn.execute("UPDATE meta SET value = %s WHERE key = %s", (Jsonb(value), key))


def gate(op, port, h):
    headers = [] if op.get("anonymous") else [h.AUTH]
    headers += [tuple(v) for v in op.get("headers", [])]
    r = h.w1a.call(port, op.get("method", "POST"), PATH, headers,
                   op.get("raw", "").encode())
    assert r["status"] == op["status"], r
    return {"isError": r["status"] != 200, "status": r["status"],
            "value": r.get("json", r.get("bytes"))}


def is_dream(op):
    return op.get("path") == PATH or op["op"] == "console_dream_concurrent"


def register(h):
    cases = {}
    # Reuse the real dream scenarios and their extractor scripts, changing
    # only the run entry point. Full bank and outbound requests still compare.
    for name in ("dream-run", "dream-run-gates", "dream-run-empty", "dream-run-fallback"):
        scn = h.SCENARIOS[name]
        cases["console-" + name] = {**scn, "ops": [
            rest(PATH, {k: v for k, v in op.get("args", {}).items() if k != "action"})
            if op.get("name") == "memory_dream" and op["args"].get("action") == "run"
            else op for op in scn["ops"]]}
    cases["console-dream-bodies"] = {
        "config": h.DREAM_RUN_CONFIG, "extractor": {"model": "stub-model", "replies": []},
        "seed": [], "ops": [rest(PATH), *[rest(PATH, {"limit": v}) for v in (
            None, "", 0, False, True, -1, 2.9, " +2_0 ", "٢", "999999999999999999999999",
            "-999999999999999999999999", "bad", "1.5", [], {}, [1])],
            *[{"op": "rest", "method": "POST", "path": PATH, "body_json": raw}
              for raw in ("{", "[]", "null", '"body"', '{"limit":NaN}', '{"limit":Infinity}')]]}
    notes = ["The Console limit fixture has a first note.",
             "The Console limit fixture has a second note."]
    cases["console-dream-limits"] = {
        "config": h.DREAM_RUN_CONFIG,
        "extractor": {"model": "stub-model", "replies": [reply
            for note in notes for reply in (h.claims_reply([note], []),
                                            h.events_reply([note], []), h.relations_reply([note], []))]},
        "seed": [], "ops": [*[h.store(t, source="conversation") for t in notes],
            *[rest(PATH, {"limit": v}) for v in (-1, 0, False, "-999999999999999999999999", 1.9, "١")],
            h.dream("pull"), h.dream("runs")]}
    cases["console-dream-http-gates"] = {
        "config": h.DREAM_RUN_CONFIG, "extractor": {"model": "stub-model", "replies": []},
        "seed": [], "ops": [
            {"op": "console_dream_gate", "anonymous": True, "status": 401},
            {"op": "console_dream_gate", "headers": [["Origin", "https://evil.example"]], "status": 200},
            {"op": "console_dream_gate", "headers": [["Content-Type", "text/plain"]],
             "raw": "{}", "status": 415},
            {"op": "console_dream_gate", "method": "GET", "status": 405},
        ]}
    cases["console-dream-tokenless-gates"] = {
        "config": h.DREAM_RUN_CONFIG, "env": {"PSEUDOLIFE_MCP_TOKEN": ""}, "seed": [],
        "ops": [{"op": "console_dream_gate", "headers": [[name, value]], "status": 403}
                for name, value in (("Origin", "https://evil.example"), ("Host", "evil.example"))]}
    note = "The Console flight fixture records no claims."
    cases["console-dream-concurrent"] = {
        "config": h.DREAM_RUN_CONFIG,
        "extractor": {"model": "stub-model", "replies": [
            h.claims_reply([note], []), h.events_reply([note], []), h.relations_reply([note], [])]},
        "seed": [], "ops": [h.store(note, source="conversation"), {"op": "console_dream_concurrent"},
                             rest(PATH)]}
    return cases
