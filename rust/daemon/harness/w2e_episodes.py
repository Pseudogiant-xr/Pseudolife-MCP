"""Episode curation scenarios for the write-path harness (``w2e.py``):
``POST /api/episodes/rename``, ``/merge`` and ``/prune`` (CUTOVER B2e).

``w2e.py`` registers ``register(sys.modules[__name__])`` and dispatches the
``rest`` and ``probe`` ops here. A ``rest`` op is the ``http`` op plus the
status: its answer is ``{"isError", "status", "value"}``, so a refusal is
compared by status as well as body. A ``probe`` op answers the rows of a
read-only query on the side's bank, keyless, so values the bank comparison
normalizes (the episode clocks, which merge widens) compare exactly. Body values ``"@ep:<title>"`` name an episode by
title on each side, top-level or inside a list. Episode ids in the answer
are renamed ``<before#i>`` from the side's episode table before the call,
in ``started_at`` order, because merge and prune answer with ids they have
just deleted; ids the call created are left to ``w2e``'s own renaming.
"""
from __future__ import annotations

import json


# Deliberate breaks (``--features mutants``) the ``episode-curation`` scenario
# must catch: merge leaving outcome signals on the deleted source, merge into
# an existing episode not widening its end, merge moving only the rows (not
# the resident entries), and prune counting digest entries.
MUTANTS = ["episodes-merge-keeps-signals", "episodes-merge-keeps-span",
           "episodes-merge-rows-only", "episodes-prune-counts-digests"]


def _before_map(dsn: str) -> dict[str, str]:
    import psycopg
    with psycopg.connect(dsn) as c:
        rows = c.execute("SELECT id FROM episodes ORDER BY started_at, id").fetchall()
    return {r[0]: f"<before#{i}>" for i, r in enumerate(rows)}


def _rename(v, emap: dict[str, str]):
    if isinstance(v, str):
        return emap.get(v, v)
    if isinstance(v, list):
        return [_rename(x, emap) for x in v]
    if isinstance(v, dict):
        return {k: _rename(x, emap) for k, x in v.items()}
    return v


def run_rest(op: dict, port: int, dsn: str, harness) -> dict:
    body = None
    if "body" in op:
        resolved = {}
        for k, v in op["body"].items():
            if isinstance(v, list):
                resolved[k] = [harness.resolve_args({"v": x}, dsn)["v"] if isinstance(x, str) else x
                               for x in v]
            elif isinstance(v, str):
                resolved[k] = harness.resolve_args({"v": v}, dsn)["v"]
            else:
                resolved[k] = v
        body = json.dumps(resolved).encode()
    if "body_json" in op:
        body = op["body_json"].encode()
    emap = _before_map(dsn)
    hdrs = [harness.AUTH] + ([("Content-Type", "application/json")] if body is not None else [])
    r = harness.w1a.call(port, op["method"], op["path"], hdrs, body)
    value = r.get("json", r.get("bytes"))
    return {"isError": r["status"] != 200, "status": r["status"], "value": _rename(value, emap)}


def run_probe(op: dict, dsn: str) -> dict:
    import psycopg
    with psycopg.connect(dsn) as c:
        rows = c.execute(op["sql"]).fetchall()
    return {"isError": False, "value": [list(r) for r in rows]}


OPS = ("rest", "probe")


def run(op: dict, port: int, dsn: str, harness) -> dict:
    if op["op"] == "probe":
        return run_probe(op, dsn)
    return run_rest(op, port, dsn, harness)


# The seeded spans (``after_stop``, so no later write-through replaces them)
# and the probe that reads them back exactly.
SPANS = {"Alpha": (1000.0, 1100.0), "Beta": (2000.0, 2100.0), "Gamma": (3000.0, 3300.0)}
SPAN_PROBE = {"op": "probe", "sql": "SELECT title, started_at, ended_at FROM episodes WHERE title IN "
                                    "('Alpha', 'Alpha renamed', 'Beta', 'Gamma', 'Rollup') ORDER BY title"}


def rest(path: str, body: dict | None = None) -> dict:
    op = {"op": "rest", "method": "POST", "path": path}
    if body is not None:
        op["body"] = body
    return op


def register(h) -> dict[str, dict]:
    def session(key: str, title: str, *texts: str, close: bool = True) -> list[dict]:
        ops = [h.store(t, [h.sess(key)]) for t in texts]
        ops.append(h.session_title(title, [h.sess(key)]))
        if close:
            ops.append({"op": "http", "method": "POST", "path": "/api/hook/session-end",
                        "body": {"session_id": key}})
        return ops

    rename = "/api/episodes/rename"
    merge = "/api/episodes/merge"
    prune = "/api/episodes/prune"
    return {
        # Seeded by Python: four closed sessions (one holding only a digest,
        # one with an outcome signal), an open one, and under it a closed and
        # an open empty sub-episode.
        "episode-curation": {
            "config": None,
            "seed": [
                *session("cur-a", "Alpha", "Alpha note one", "Alpha note two"),
                *session("cur-b", "Beta", "Beta note", close=False),
                h.outcome("curate the beta session", "success", [h.sess("cur-b")]),
                {"op": "http", "method": "POST", "path": "/api/hook/session-end",
                 "body": {"session_id": "cur-b"}},
                *session("cur-c", "Gamma", "Gamma note"),
                *session("cur-f", "Digest", "Digest only"),
                {"op": "sql", "sql": "UPDATE entries SET source = 'digest' WHERE text = 'Digest only'"},
                *session("cur-d", "Delta", "Delta note", close=False),
                h.ep_start("Delta sub", [h.sess("cur-d")]),
                h.ep_end([h.sess("cur-d")]),
                h.ep_start("Delta open sub", [h.sess("cur-d")]),
                *[h.sql(f"UPDATE episodes SET started_at = {a}, ended_at = {b} WHERE title = '{t}'",
                        after_stop=True) for t, (a, b) in SPANS.items()],
            ],
            "ops": [
                # rename: the route lambda's KeyError, the empty-title answer
                # before init, Python's exceptions, the misses, then a rename
                # that restamps the session's entries.
                rest(rename, {"title": "No id"}),
                rest(rename, {"id": "@ep:Alpha", "title": "   "}),
                rest(rename, {"id": "@ep:Alpha"}),
                rest(rename, {"id": "@ep:Alpha", "title": 5}),
                rest(rename, {"id": "no-such-episode", "title": "X"}),
                rest(rename, {"id": 7, "title": "X"}),
                rest(rename, {"id": 1.5, "title": "X"}),
                rest(rename, {"id": ["x"], "title": "X"}),
                rest(rename, {"id": "@ep:Alpha", "title": "  Alpha renamed  "}),
                # The resident entries carry the new title.
                h.ep_summary("@ep:Alpha renamed"),
                # merge: refusals first (each leaves the bank unchanged).
                rest(merge),
                rest(merge, {"sources": 5, "title": "R"}),
                rest(merge, {"sources": ["x", ["y"]], "title": "R"}),
                rest(merge, {"sources": ["x"], "title": 5}),
                rest(merge, {"sources": ["@ep:Gamma"], "into": "no-such-target"}),
                rest(merge, {"sources": ["@ep:Gamma"], "into": 3}),
                rest(merge, {"sources": ["@ep:Gamma"], "into": {"id": 1}}),
                rest(merge, {"sources": ["@ep:Alpha renamed"], "into": "@ep:Alpha renamed"}),
                rest(merge, {"sources": "", "title": "Empty"}),
                # A new rollup: an open source is skipped, unknown ones are
                # reported (a number kept as sent), a repeat counts once, and
                # the outcome signal follows its entry.
                rest(merge, {"sources": ["@ep:Beta", "@ep:Delta", "no-such-source", 3, 3.0,
                                         "@ep:Beta"],
                             "title": "  Rollup  ", "hint": "beta folded in"}),
                # The rollup holds the resident entry, and its span is Beta's.
                h.ep_summary("@ep:Rollup"),
                SPAN_PROBE,
                # Into an existing closed episode: its span widens to Gamma's end.
                rest(merge, {"sources": ["@ep:Gamma"], "into": "@ep:Alpha renamed"}),
                h.ep_summary("@ep:Alpha renamed"),
                SPAN_PROBE,
                # prune: closed empty episodes, a digest-only one included;
                # then the open empty one too.
                rest(prune, {"include_open": 0}),
                rest(prune, {"include_open": [1]}),
                # The pruned open sub-episode was current: an unkeyed episode
                # start nests under the current episode, so it shows where
                # current points now (no embedding involved).
                h.ep_start("Started after the prune"),
            ],
        },
    }
