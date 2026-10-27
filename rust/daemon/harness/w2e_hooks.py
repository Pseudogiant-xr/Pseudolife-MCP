"""Hook scenarios for the write-path harness (``w2e.py``): the SessionEnd
close (``POST /api/hook/session-end``) and the separate memory-policy hook
(``GET /api/hook/memory-policy``), CUTOVER B3.

``w2e.py`` registers ``register(sys.modules[__name__])`` and dispatches the
``hook`` op here: one request with the bearer unless ``"auth": false``,
answered as ``{"isError", "status", "headers", "value"}``, so the status,
the content type and cache policy, and the body (JSON by value, text by
bytes) are all compared. ``"@root:<key>:<n>"`` in a JSON body resolves per
side as ``w2e.resolve_args`` does.
"""
from __future__ import annotations

import json
import re
import time

# Deliberate breaks (``--features mutants``) these scenarios must catch.
MUTANTS = ["session-end-no-client-stamp", "session-end-keeps-empty",
           "session-end-clears-foreign", "session-end-no-dream", "policy-arm-shifted",
           "changes-counts-own-session", "changes-from-open-leaf",
           "changes-cursor-echoes-since", "changes-counts-superseded"]

# The session-end dream: everything but extraction off, and no sweep dream,
# so only the close's own trigger can dream.
DREAM_CONFIG = ("memory:\n  lessons:\n    enabled: false\n    infer_outcomes: false\n"
                "  graph_insight:\n    enabled: false\n"
                "  dream:\n    enabled: false\n    stall_notice: false\n")
DREAM_NOTES = ["The billing export runs nightly at 02:00.",
               "Priya maintains the billing export."]
# No claims: the dream still calls the model and commits its run (the
# trigger is what is compared), and writes no fact whose embedding each side
# computes for itself (two embedders agree only to the measured tolerance,
# which holds on the calibration host, not on every host).
DREAM_CLAIMS: list[dict] = []
DREAM_SETTLE = {"quiet_s": 3.0, "timeout_s": 120.0}

OPS = ("hook",)

# A status note whose excerpt exercises the JSON quoting (ensure_ascii=False),
# Python's str.split whitespace (U+001F included) and the 160-character cut.
MC_LONG_NOTE = ('Status from session C: the café said "ship it" \\ then\x1fpaused,   and\n'
                + "the release notes still need a pass over every section " * 4)

_HEADERS = ("content-type", "cache-control")


# The memory-change hook's last cursor per side (keyed by daemon port), for
# a later op's ``since=@prev``.
_LAST_CURSOR: dict[int, str] = {}
_CURSOR_LINE = re.compile(r"[0-9]{1,12}\.[0-9]{6}")


def _cursor(value, port: int, before: float, after: float) -> str:
    """Line 1 of a memory-change answer: a six-decimal cursor that must read
    this side's clock during the call; it becomes ``<cursor>`` and is kept
    for ``@prev``. Anything else is a difference."""
    if not isinstance(value, str) or not value:
        return value
    line, _, rest = value.partition("\n")
    # Python 3.11's time.time() on Windows is the coarse system clock: it
    # trails the precise clock a native daemon reads by up to a tick
    # (~16 ms), so only the upper bound needs room; the Python daemon reads
    # the same coarse clock as this harness.
    if not _CURSOR_LINE.fullmatch(line) or not before - 1e-3 <= float(line) <= after + 0.05:
        _LAST_CURSOR.pop(port, None)
        return f"<bad cursor {line!r}>" + "\n" + rest
    _LAST_CURSOR[port] = line
    return "<cursor>\n" + rest


def run(op: dict, port: int, dsn: str, harness) -> dict:
    body = None
    if "body" in op:
        resolved = {k: harness.resolve_args({"v": v}, dsn)["v"] if isinstance(v, str) else v
                    for k, v in op["body"].items()}
        body = json.dumps(resolved).encode()
    hdrs = [harness.AUTH] if op.get("auth", True) else []
    if body is not None:
        hdrs.append(("Content-Type", "application/json"))
    path = op["path"].replace("@prev", _LAST_CURSOR.get(port, ""))
    before = time.time()
    r = harness.w1a.call(port, op["method"], path, hdrs, body)
    after = time.time()
    value = r.get("json", r.get("bytes"))
    if op.get("cursor"):
        value = _cursor(value, port, before, after)
    return {"isError": r["status"] != 200, "status": r["status"],
            "headers": {k: r["headers"].get(k) for k in _HEADERS},
            "value": value}


def hook(method: str, path: str, body: dict | None = None, auth: bool = True) -> dict:
    op = {"op": "hook", "method": method, "path": path, "auth": auth}
    if body is not None:
        op["body"] = body
    return op


def end(session_id) -> dict:
    return hook("POST", "/api/hook/session-end", {"session_id": session_id})


def policy(query: str = "", auth: bool = True) -> dict:
    return hook("GET", "/api/hook/memory-policy" + (f"?{query}" if query else ""), auth=auth)


def changes(query: str = "", auth: bool = True) -> dict:
    return {**hook("GET", "/api/hook/memory-changes" + (f"?{query}" if query else ""), auth=auth),
            "cursor": True}


def register(h) -> dict[str, dict]:
    start = "/api/hook/session-start?source=startup&session_id="
    seed_start = lambda key: {"op": "http", "method": "GET", "path": start + key}  # noqa: E731
    return {
        # Seeded by Python: registered sessions (one root with entries and
        # its generic title, one empty, one whose entry is on an open
        # sub-episode, one already ended, one whose registered root was
        # pruned, one with a signal source, one titled by its agent), the
        # active-session pointer on the last.
        # A close with entries fires the session-end dream on both sides;
        # extractor_mode fallback with no fallback endpoint makes both
        # dreams refuse before they write (a NoOp extractor would
        # acknowledge every pending entry), so the close itself is compared.
        "hook-session-end": {
            "config": "memory:\n  dream:\n    enabled: false\n    extractor_mode: fallback\n",
            "seed": [
                seed_start("end-a"),
                h.store("A note in session A about the deploy checklist.", [h.sess("end-a")]),
                h.store("A second note in session A.", [h.sess("end-a")]),
                seed_start("end-b"),
                # C's only entry is on its open sub-episode, so the root is
                # kept, and titled, by its subtree.
                seed_start("end-c"),
                h.ep_start("C sub-task", [h.sess("end-c")]),
                h.store("A note in session C's sub-task.", [h.sess("end-c")]),
                # E ended before; its registration then reads as reaped, so
                # an end with nothing open shows in end_reason.
                seed_start("end-e"),
                h.store("A note in session E.", [h.sess("end-e")]),
                {"op": "http", "method": "POST", "path": "/api/hook/session-end",
                 "body": {"session_id": "end-e"}},
                h.sql("UPDATE client_sessions SET end_reason = 'idle' WHERE session_key = 'end-e'",
                      after_stop=True),
                # F's registered root was pruned empty; a later store opened
                # a second root the registration does not name yet.
                seed_start("end-f"),
                {"op": "http", "method": "POST", "path": "/api/hook/session-end",
                 "body": {"session_id": "end-f"}},
                h.store("A note in session F's second root.", [h.sess("end-f")]),
                # G: a signal source beside noise, and a first entry long
                # enough to cut at a word.
                seed_start("end-g"),
                h.store("The release checklist for the next version covers the installer, "
                        "the shim and the plugin cache.", [h.sess("end-g")], source="proj-g"),
                h.store("Status: halfway through.", [h.sess("end-g")], source="status"),
                # H: an empty root with an empty sub-episode, pruned whole.
                seed_start("end-h"),
                h.ep_start("H sub-task", [h.sess("end-h")]),
                seed_start("end-d"),
                h.store("A note in session D.", [h.sess("end-d")]),
                h.session_title("Session D, named by its agent", [h.sess("end-d")]),
            ],
            "ops": [
                # Entries and a generic title: closed, titled from its
                # entries, its registration stamped; the pointer is not its.
                end("end-a"),
                h.ep_summary("@root:end-a:32"),
                # Empty: deleted, its registration still stamped.
                end("end-b"),
                # The open sub-episode closes with its root.
                end("end-c"),
                h.ep_summary("@root:end-c:32"),
                # Nothing open: only the registration's end moves ('idle' to 'end').
                end("end-e"),
                # The second root is appended to the registration's roots.
                end("end-f"),
                # Titled from the signal source and the cut first entry.
                end("end-g"),
                h.ep_summary("@root:end-g:32"),
                # Empty root and sub-episode: both deleted.
                end("end-h"),
                # Named by its agent: kept; it owns the pointer, which clears.
                end("end-d"),
                h.ep_summary("@root:end-d:32"),
                # Nothing open any more: only the registration's end moves.
                end("end-a"),
                # Never registered, and not a string: nothing changes.
                end("never-registered"),
                end(5),
                end(True),
                # With no pointer an unkeyed episode start opens a root.
                h.ep_start("Started after the ends"),
            ],
        },
        # The close's dream trigger, against a stub extractor. An empty
        # close fires nothing (a dream would extract session A's notes and
        # call the model, and acknowledge them); a close with entries fires
        # one dream, whose requests are compared and which acknowledges the
        # pending notes on both sides.
        "hook-session-end-dream": {
            "config": DREAM_CONFIG,
            "extractor": {"model": "stub-model",
                          # Each reply answers once, on each side, by the end.
                          "replies": [{**r, "times": 1}
                                      for r in h.batch_replies(DREAM_NOTES, DREAM_CLAIMS)]},
            "extractor_all_used": True,
            "seed": [
                seed_start("dream-a"),
                h.store(DREAM_NOTES[0], [h.sess("dream-a")], source="conversation"),
                h.store(DREAM_NOTES[1], [h.sess("dream-a")], source="conversation"),
                seed_start("dream-b"),
            ],
            "ops": [
                {**end("dream-b"), "settle": dict(DREAM_SETTLE),
                 "expect": ["SELECT EXISTS (SELECT 1 FROM entries WHERE dream_state = 'pending')"]},
                {**end("dream-a"), "settle": dict(DREAM_SETTLE),
                 "expect": ["SELECT NOT EXISTS (SELECT 1 FROM entries WHERE dream_state = 'pending')"]},
            ],
        },
        # The separate policy hook. Arms by SHA-256 of the session id:
        # sess-a -> compact, sess-b -> full_separate_hook, sess-c -> none.
        "hook-memory-policy": {
            "config": ("memory_policy:\n  variant: full_separate_hook\n"
                       "  ab_arms: [none, compact, full_separate_hook]\n"),
            "seed": [],
            "ops": [
                policy(),
                policy("session_id=sess-a"),
                policy("session_id=sess-b"),
                policy("session_id=sess-c"),
                policy("session_id=sess-b&source=resume"),
                policy("session_id=sess-b&source=compact"),
                policy("session_id=sess-b&source=startup"),
                policy("source=resume"),
                policy("session_id="),
                # Unauthorized: the session id and source are dropped, so
                # the configured variant serves.
                policy("session_id=sess-c&source=resume", auth=False),
            ],
        },
        "hook-memory-policy-default": {
            "config": None,
            "seed": [],
            "ops": [policy(), policy("session_id=sess-b")],
        },
        # The per-turn memory-change note. Seeded by Python: session mc-a
        # registered first; status notes from mc-b and from mc-a itself,
        # a plain note, and two lessons asserted after mc-a's start (one
        # current, one superseded) and one before it.
        "hook-memory-changes": {
            "config": None,
            "seed": [
                seed_start("mc-a"),
                h.store("Status from another session: the deploy is halfway.",
                        [h.sess("mc-b")], source="status"),
                h.store("Status from this session: reading the spec.",
                        [h.sess("mc-a")], source="status"),
                h.store("A plain note, not a status.", [h.sess("mc-b")]),
                h.store("Status from another session, newer:   the deploy\n is done.",
                        [h.sess("mc-b")], source="status"),
                # The newest current note is the excerpt: quotes, a
                # backslash, non-ASCII, U+001F and runs of whitespace,
                # past 160 characters. A newer note superseded by SQL is
                # neither counted nor shown.
                h.store(MC_LONG_NOTE, [h.sess("mc-c")], source="status"),
                h.store("Status from session C, withdrawn.", [h.sess("mc-c")], source="status"),
                h.sql("UPDATE entries SET superseded_at = ts + 1, "
                      "superseded_by_text = 'Withdrawn.' "
                      "WHERE text = 'Status from session C, withdrawn.'", after_stop=True),
                # mc-a's open sub-episode starts after those notes: a first
                # turn counts from the session's root, not this leaf.
                h.ep_start("mc-a sub-task", [h.sess("mc-a")]),
                *[h.sql(
                    "INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, value, "
                    "polarity, outcome, status, confidence, origin, support, provenance, "
                    "asserted_at, last_confirmed, embedding) SELECT "
                    f"'{task}', 'pitfall', '{task}', 'pitfall', '{text}', '{pol}', 'failure', "
                    f"'{status}', 0.8, 'agent', '[]', '[]', "
                    f"(SELECT started_at FROM episodes WHERE session_key = 'mc-a' "
                    f"AND parent_id IS NULL) + {offset}, 1700000000, embedding "
                    "FROM entries WHERE text = 'A plain note, not a status.'", after_stop=True)
                  for task, text, pol, status, offset in [
                      ("deploy", "Avoid deploying on a stale cache.", "-", "current", 1),
                      ("review", "A superseded lesson.", "+", "superseded", 2),
                      ("old", "A lesson from before the session.", "+", "current", -100)]],
            ],
            "ops": [
                # First turn: counted from mc-a's start, its own status left out.
                changes("session_id=mc-a"),
                # Nothing new since that cursor.
                changes("session_id=mc-a&since=@prev"),
                # mc-b's first turn: its root opened with its first store.
                changes("session_id=mc-b"),
                # No session: everything since the given cursor.
                changes("since=1"),
                # A cursor ahead of this daemon's clock, a malformed one with
                # no session, an unknown session: baselines (a cursor, no note).
                changes("since=99999999999.5"),
                changes("since=not-a-cursor"),
                changes("session_id=no-such-session"),
                # An agent that is not the maintainer's delegate adds nothing.
                changes("session_id=mc-a&agent=0123456789abcdef"),
                # Unauthorized: the empty body.
                changes("session_id=mc-a", auth=False),
            ],
        },
    }
