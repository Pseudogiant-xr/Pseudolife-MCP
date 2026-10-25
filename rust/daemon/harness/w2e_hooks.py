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

# Deliberate breaks (``--features mutants``) these scenarios must catch.
MUTANTS = ["session-end-no-client-stamp", "session-end-keeps-empty",
           "session-end-clears-foreign", "policy-arm-shifted"]

OPS = ("hook",)

_HEADERS = ("content-type", "cache-control")


def run(op: dict, port: int, dsn: str, harness) -> dict:
    body = None
    if "body" in op:
        resolved = {k: harness.resolve_args({"v": v}, dsn)["v"] if isinstance(v, str) else v
                    for k, v in op["body"].items()}
        body = json.dumps(resolved).encode()
    hdrs = [harness.AUTH] if op.get("auth", True) else []
    if body is not None:
        hdrs.append(("Content-Type", "application/json"))
    r = harness.w1a.call(port, op["method"], op["path"], hdrs, body)
    return {"isError": r["status"] != 200, "status": r["status"],
            "headers": {k: r["headers"].get(k) for k in _HEADERS},
            "value": r.get("json", r.get("bytes"))}


def hook(method: str, path: str, body: dict | None = None, auth: bool = True) -> dict:
    op = {"op": "hook", "method": method, "path": path, "auth": auth}
    if body is not None:
        op["body"] = body
    return op


def end(session_id) -> dict:
    return hook("POST", "/api/hook/session-end", {"session_id": session_id})


def policy(query: str = "", auth: bool = True) -> dict:
    return hook("GET", "/api/hook/memory-policy" + (f"?{query}" if query else ""), auth=auth)


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
    }
