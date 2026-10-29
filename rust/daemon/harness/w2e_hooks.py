"""Hook scenarios for the write-path harness (``w2e.py``): the SessionStart
hook (``GET /api/hook/session-start``), the SessionEnd close (``POST
/api/hook/session-end``), the separate memory-policy hook (``GET
/api/hook/memory-policy``) and the per-turn memory-change note (``GET
/api/hook/memory-changes``), CUTOVER B3.

``w2e.py`` registers ``register(sys.modules[__name__])`` and dispatches the
``hook`` op here: one request with the bearer unless ``"auth": false``,
answered as ``{"isError", "status", "headers", "value"}``, so the status,
the content type and cache policy, and the body (JSON by value, text by
bytes) are all compared. ``"@root:<key>:<n>"`` in a JSON body resolves per
side as ``w2e.resolve_args`` does. A session-start answer's episode handle
(the first 12 characters of a random id) is renamed by w2e's episode-id
normalizer like any other id (``<ep#i>[:12]``), so the two answers compare
by the episode they name. The ``write_file`` home op writes a file into each side's data directory
(the daemon-side ``hook-instructions.md``).
"""
from __future__ import annotations

import json
import re
import time
import tomllib
from pathlib import Path

# Deliberate breaks (``--features mutants``) these scenarios must catch.
MUTANTS = ["session-end-no-client-stamp", "session-end-keeps-empty",
           "session-end-clears-foreign", "session-end-no-dream", "policy-arm-shifted",
           "changes-counts-own-session", "changes-from-open-leaf",
           "changes-cursor-echoes-since", "changes-counts-superseded",
           "session-start-no-registration", "session-start-no-pointer",
           "session-start-no-briefing", "briefing-no-avoid-first",
           "briefing-recap-includes-open", "episode-end-ignores-header",
           "episode-end-stamps-guarded", "episode-end-no-dream",
           "briefing-route-no-coordination"]

HOME_OPS = ("write_file",)

# Both daemons report the package version (Python from its installed
# metadata, Rust from its crate); the version notices are asked about it.
VERSION = tomllib.loads(
    (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]

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
# cli-episode-end-dream's notes, in store order: the first in cd-1, the
# others in cd-2; the one dream pulls all three.
CLI_DREAM_NOTES = ["The invoice job retries three times.",
                   "Marta reviews the invoice job's alerts.",
                   "The invoice job runs on the batch cluster."]

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
    hdrs += [tuple(x) for x in op.get("headers", ())]
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


def home_op(op: dict, home: Path) -> dict:
    """``write_file``: ``op["text"]`` (or nothing, to remove it) as
    ``op["name"]`` in the side's data directory."""
    path = home / "data" / op["name"]
    if op.get("text") is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(op["text"].encode("utf-8"))
    return {"isError": False, "value": None}


def write_file(name: str, text: str | None) -> dict:
    return {"op": "write_file", "name": name, "text": text}


def hook(method: str, path: str, body: dict | None = None, auth: bool = True) -> dict:
    op = {"op": "hook", "method": method, "path": path, "auth": auth}
    if body is not None:
        op["body"] = body
    return op


def end(session_id) -> dict:
    return hook("POST", "/api/hook/session-end", {"session_id": session_id})


def policy(query: str = "", auth: bool = True) -> dict:
    return hook("GET", "/api/hook/memory-policy" + (f"?{query}" if query else ""), auth=auth)


def session_start(query: str = "", auth: bool = True, headers=()) -> dict:
    op = hook("GET", "/api/hook/session-start" + (f"?{query}" if query else ""), auth=auth)
    if headers:
        op["headers"] = [list(x) for x in headers]
    return op


def briefing(query: str = "", headers=()) -> dict:
    op = hook("GET", "/api/briefing" + (f"?{query}" if query else ""))
    if headers:
        op["headers"] = [list(h) for h in headers]
    return op


def episode_start(body: dict) -> dict:
    return hook("POST", "/api/episode/start", body)


def episode_end(body: dict, headers=()) -> dict:
    op = hook("POST", "/api/episode/end", body)
    if headers:
        op["headers"] = [list(h) for h in headers]
    return op


def changes(query: str = "", auth: bool = True) -> dict:
    return {**hook("GET", "/api/hook/memory-changes" + (f"?{query}" if query else ""), auth=auth),
            "cursor": True}


def register(h) -> dict[str, dict]:
    # cli-episode-routes stamps a registration behind the daemons' backs,
    # as the idle reaper would have.
    h.EXTERNAL_WRITES.add("cli-episode-routes")
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
        # The SessionStart hook over a bank with lessons (avoid-first, one
        # stale against a newer fact, one superseded), world facts (one
        # stale), a closed session, a session the reaper closed a minute
        # ago with a session digest, and a merge proposal pending for 30
        # days with the judges off. Each answer is compared byte for byte
        # (the handle by the episode it names), and the registration, the
        # pointer and the reopened root by bank state.
        "hook-session-start": {
            "config": START_CONFIG,
            "seed": [
                h.store("A plain note the seeded lessons copy their embedding from."),
                h.relate("Alpha Service", "depends-on", "Beta Service"),
                h.fact("Deploy Pipeline", "owner", "the platform team"),
                *START_WORLD(h),
                seed_start("ss-old"),
                h.store("A note in the old session about the release train.", [h.sess("ss-old")]),
                h.store("A second note in the old session.", [h.sess("ss-old")]),
                {"op": "http", "method": "POST", "path": "/api/hook/session-end",
                 "body": {"session_id": "ss-old"}},
                seed_start("ss-r"),
                h.store("A note in session R about the hook port.", [h.sess("ss-r")]),
                *[h.sql(q, after_stop=True) for q in START_SQL],
            ],
            "ops": [
                # A fresh start: the hooks notice (same version, other
                # hooks), the review-queue line, the handle, the compact
                # policy and the briefing (lessons, the recap of R with its
                # digest, world facts); the session registers and takes
                # the pointer.
                session_start(f"session_id=ss-a&source=startup&plugin_version={VERSION}"
                              f"&plugin_hooks_digest={'0' * 64}"),
                # A resume: the handle again (the same root), a pointer line,
                # no review-queue line; the registration appends its start.
                session_start("session_id=ss-a&source=resume"),
                # A compaction from inside a sub-task: the handle names the
                # open leaf, and the daemon-side instructions follow.
                h.ep_start("ss-a sub-task", [h.sess("ss-a")]),
                write_file("hook-instructions.md", CUSTOM_SHORT),
                session_start("session_id=ss-a&source=compact"),
                # A new session with oversized instructions: whole paragraphs
                # within their share, then the partial warning.
                write_file("hook-instructions.md", CUSTOM_LONG),
                session_start("session_id=ss-b&source=startup"),
                write_file("hook-instructions.md", None),
                # The reaped session comes back: its root reopens, its
                # registration's close clears, and the recap moves to the
                # old session.
                session_start("session_id=ss-r&source=startup"),
                # Version notices: plugin older (and an unauthorized caller,
                # whose session id is dropped: nothing registers), newer
                # with a launcher path to quote, and equal by number.
                session_start("session_id=ss-z&plugin_version=0.1.0", auth=False),
                # A rejected bearer: the same answer after the rejected-token
                # notice, naming the launcher; nothing registers.
                session_start("session_id=ss-y&plugin_version=0.1.0"
                              "&launcher=/opt/pl/bin/pseudolife-mcp", auth=False,
                              headers=[("Authorization", "Bearer w2e-rotated-token")]),
                session_start("plugin_version=99.0.0"
                              "&launcher=C:/Program%20Files/pl/pseudolife-mcp.exe"),
                session_start(f"plugin_version={VERSION}.dev1&launcher=relative/pseudolife-mcp"),
                # Authorized with no session id: nothing registers.
                session_start("source=startup"),
                h.ep_summary("@root:ss-a:32"),
            ],
        },
        # The CLI's episode routes (B10f): POST /api/episode/start opens or
        # re-finds a session's episode and registers it as `api`; POST
        # /api/episode/end closes an explicit key, or with none only the
        # caller's own session (header, else the active-session pointer).
        "cli-episode-routes": {
            "config": "memory:\n  dream:\n    enabled: false\n    extractor_mode: fallback\n",
            "seed": [
                h.store("A note in session A about the cache.", [h.sess("es-a")]),
                h.store("A note in session B.", [h.sess("es-b")]),
                h.ep_start("es-b sub-task", [h.sess("es-b")]),
                h.store("A note in es-b's sub-task.", [h.sess("es-b")]),
                # Registered through the route, and left empty. Seeded, so
                # both banks hold its id after the end prunes it.
                {"op": "http", "method": "POST", "path": "/api/episode/start",
                 "body": {"session_key": "es-empty", "title": "Empty CLI session"}},
                seed_start("es-ptr"),
                h.store("A note in the pointer's session.", [h.sess("es-ptr")]),
            ],
            "ops": [
                # A new keyed root with its title and hint, registered as api.
                episode_start({"session_key": "es-new", "title": "CLI start", "hint": "a hint"}),
                # Again: the same open episode; the registration appends.
                episode_start({"session_key": "es-new", "title": "Ignored"}),
                # An open sub-episode is handed back; the row names the root.
                episode_start({"session_key": "es-b"}),
                # Keyless, and an empty key with an empty title.
                episode_start({}),
                episode_start({"session_key": "", "title": ""}),
                # An explicit end: closed and titled from its entries.
                episode_end({"session_key": "es-a"}),
                h.ep_summary("@root:es-a:32"),
                # Nothing open any more, and a key never seen: {}.
                episode_end({"session_key": "es-a"}),
                episode_end({"session_key": "never-seen"}),
                # Empty: pruned, its registration stamped; no dream asked.
                episode_end({"session_key": "es-empty", "run_dream": False}),
                # Guarded: the header's session (the sub-task closes too).
                episode_end({}, headers=[("X-PL-Session", "es-b")]),
                # Guarded: no header, so the active-session pointer's.
                episode_end({}),
                # As if the reaper had closed it: its registration reads idle.
                h.external("UPDATE client_sessions SET end_reason = 'idle' "
                           "WHERE session_key = 'es-ptr'"),
                # Guarded again: the pointer's session has nothing open, and
                # the guarded path stamps nothing (still idle).
                episode_end({}),
                # The same key, explicit: nothing open, the end is stamped.
                episode_end({"session_key": "es-ptr"}),
            ],
        },
        # The route's session-end dream, against a stub extractor: a close
        # with run_dream falsy fires nothing (the notes stay pending); a
        # close with entries fires one dream, whose requests are compared
        # and which acknowledges every pending note on both sides.
        "cli-episode-end-dream": {
            "config": DREAM_CONFIG,
            "extractor": {"model": "stub-model",
                          "replies": [{**r, "times": 1}
                                      for r in h.batch_replies(CLI_DREAM_NOTES, DREAM_CLAIMS)]},
            "extractor_all_used": True,
            "seed": [
                {"op": "http", "method": "POST", "path": "/api/episode/start",
                 "body": {"session_key": "cd-1", "title": "First"}},
                h.store(CLI_DREAM_NOTES[0], [h.sess("cd-1")], source="conversation"),
                {"op": "http", "method": "POST", "path": "/api/episode/start",
                 "body": {"session_key": "cd-2", "title": "Second"}},
                h.store(CLI_DREAM_NOTES[1], [h.sess("cd-2")], source="conversation"),
                h.store(CLI_DREAM_NOTES[2], [h.sess("cd-2")], source="conversation"),
            ],
            "ops": [
                {**episode_end({"session_key": "cd-1", "run_dream": 0}),
                 "settle": dict(DREAM_SETTLE),
                 "expect": ["SELECT EXISTS (SELECT 1 FROM entries WHERE dream_state = 'pending')"]},
                {**episode_end({"session_key": "cd-2"}), "settle": dict(DREAM_SETTLE),
                 "expect": ["SELECT NOT EXISTS (SELECT 1 FROM entries "
                            "WHERE dream_state = 'pending')"]},
            ],
        },
        # GET /api/briefing (B10f): the whole briefing as JSON over the
        # session-start seeds, plus the graph digest's unsure items and two
        # open sessions for the coordination section. The caps parse as
        # Python's int() and slice ([:-1] drops the last), and the caller's
        # own X-PL-Session is left out of its peers.
        "cli-briefing": {
            "config": START_CONFIG,
            "seed": [
                h.store("A plain note the seeded lessons copy their embedding from."),
                h.relate("Alpha Service", "depends-on", "Beta Service"),
                h.fact("Deploy Pipeline", "owner", "the platform team"),
                *START_WORLD(h),
                seed_start("ss-old"),
                h.store("A note in the old session about the release train.", [h.sess("ss-old")]),
                {"op": "http", "method": "POST", "path": "/api/hook/session-end",
                 "body": {"session_id": "ss-old"}},
                seed_start("ss-r"),
                h.store("A note in session R about the hook port.", [h.sess("ss-r")]),
                seed_start("peer-a"),
                h.store("Peer A is working on the installer.", [h.sess("peer-a")]),
                h.session_title("Installer   work\nfor `release`", [h.sess("peer-a")]),
                seed_start("peer-b"),
                h.store("Peer B is reviewing.", [h.sess("peer-b")]),
                *[h.sql(q, after_stop=True) for q in START_SQL + [GRAPH_DIGEST_SQL]],
            ],
            "ops": [
                briefing(""),
                briefing("max_unsure=1&max_lessons=-1&max_world=0"),
                briefing("max_unsure=abc&max_lessons=&max_world=+2"),
                # Python's ints are unbounded: past i64 a cap keeps
                # everything (or nothing), and Unicode digits read as digits.
                briefing("max_lessons=99999999999999999999&max_world=-99999999999999999999"
                         "&max_unsure=-99999999999999999999"),
                briefing("max_lessons=%D9%A1&max_world=%EF%BC%92&max_unsure=1_0"),
                briefing("", headers=[("X-PL-Session", "peer-a")]),
            ],
        },
        # Coordination off: no coordination key and no peers section.
        "cli-briefing-no-coordination": {
            "config": START_CONFIG + "coordination:\n  enabled: false\n",
            "seed": [
                seed_start("peer-c"),
                h.store("Peer C is working.", [h.sess("peer-c")]),
            ],
            "ops": [briefing("")],
        },
        # A cold bank and the A/B arms: sess-a gets compact (policy and
        # onboarding), sess-b full_separate_hook (onboarding only), sess-c
        # none (the handle alone). Each registration records its variant.
        "hook-session-start-cold": {
            "config": ("memory_policy:\n  variant: compact\n"
                       "  ab_arms: [none, compact, full_separate_hook]\n"),
            "seed": [],
            "ops": [
                session_start("session_id=sess-a&source=startup"),
                session_start("session_id=sess-b&source=startup"),
                session_start("session_id=sess-c&source=startup"),
                session_start("session_id=sess-c&source=compact"),
            ],
        },
    }


START_CONFIG = ("memory:\n  dream:\n    enabled: false\n    extractor_mode: fallback\n"
                "  deep_dream:\n    judges_enabled: false\n    review_queue_alert_pending: 1\n")


def START_WORLD(h) -> list[dict]:  # noqa: N802 (a seed list built with w2e's helpers)
    return [
        h.world_set("Rust", "latest stable version", "1.91.0", confidence=0.9,
                    source_url="https://blog.rust-lang.org/2026/09/18/Rust-1.91.0/",
                    freshness_class="slow"),
        h.world_set("PostgreSQL", "license", "PostgreSQL License", confidence=0.95,
                    freshness_class="evergreen",
                    source_url="https://www.postgresql.org/about/licence/"),
        h.world_set("Python", "latest version", "3.13.0", source_url="https://www.python.org/",
                    retrieved_at=1.0),
        h.world_set("Node.js", "active LTS", "22", confidence=0.5,
                    source_url="https://nodejs.org/en/about/previous-releases"),
        # Ties PostgreSQL's confidence; the case-folded entity order puts it
        # first, and the cut of three then drops Node.js.
        h.world_set("apache httpd", "license", "Apache-2.0", confidence=0.95,
                    freshness_class="evergreen", source_url="https://httpd.apache.org/"),
    ]


def _lesson_sql(task: str, aspect: str, text: str, pol: str, outcome: str, status: str,
                at: int) -> str:
    return ("INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, value, "
            "polarity, outcome, status, confidence, origin, support, provenance, "
            "asserted_at, last_confirmed, embedding) SELECT "
            f"'{task}', '{aspect}', '{task.lower()}', '{aspect}', '{text}', '{pol}', "
            f"'{outcome}', '{status}', 0.8, 'agent', '[]', '[]', {at}, {at}, embedding "
            "FROM entries WHERE text = 'A plain note the seeded lessons copy their embedding from.'")


# The graph digest the dream's insight pass would leave: two surprises (one
# with a reason, one with defaults), a question, and a blank question.
GRAPH_DIGEST_SQL = (
    "INSERT INTO meta (key, value) VALUES ('graph_digest', '"
    + json.dumps({"computed_at": 1700000000.5,
                  "surprises": [{"src": "Alpha Service", "dst": "Beta  Service",
                                 "relation": "depends-on", "why": "rare\nbridge"},
                                {"src": None, "dst": "Gamma", "relation": "", "why": ""}],
                  "questions": [{"question": "Is Alpha still on the old cluster?"},
                                {"question": ""}]}).replace("'", "''")
    + "') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
)

START_SQL = [
    # Sorted by case-folded task: apple workflow, Benchmarks, Code Review,
    # Deploy Pipeline (avoid, and stale: its fact is newer), Release (a
    # correction); avoid-first keeps Deploy Pipeline, Release, apple
    # workflow (a case-sensitive sort would keep Benchmarks instead).
    _lesson_sql("apple workflow", "approach", "Batch the small edits.", "+", "success",
                "current", 1700000050),
    _lesson_sql("Deploy Pipeline", "pitfall", "Avoid deploying on a stale cache.", "-",
                "failure", "current", 1700000000),
    _lesson_sql("Code Review", "approach", "Read the Python oracle before signing off.", "+",
                "success", "current", 1700000100),
    _lesson_sql("Release", "approach", "Cut the release from master.", "+", "correction",
                "current", 1700000200),
    _lesson_sql("Benchmarks", "approach", "Pin the interpreter before recording goldens.",
                "+", "success", "current", 1700000300),
    _lesson_sql("Archive", "approach", "A superseded lesson.", "+", "success", "superseded",
                1700000400),
    # The reaper closed R a minute ago (inside the resume window).
    "UPDATE episodes SET ended_at = extract(epoch from now()) - 60 "
    "WHERE session_key = 'ss-r' AND ended_at IS NULL",
    "UPDATE client_sessions SET ended_at = extract(epoch from now()) - 60, "
    "end_reason = 'idle' WHERE session_key = 'ss-r'",
    # R's session digest: a header line, then the body the recap shows.
    "INSERT INTO entries (band, text, embedding, ts, source, episode_id, episode_title) "
    "SELECT band, 'Session digest: ss-r' || chr(10) || 'Worked through the hook port "
    "and its   harness.', embedding, ts + 1, 'digest', episode_id, episode_title "
    "FROM entries WHERE text = 'A note in session R about the hook port.'",
    # A merge proposal pending for 30 days.
    "INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, status, "
    "created_at) SELECT 'merge', a.id, b.id, 0.7, 'harness', 'pending', "
    "extract(epoch from now()) - 30 * 86400 FROM entities a, entities b "
    "WHERE a.display = 'Alpha Service' AND b.display = 'Beta Service'",
]

# Written as Windows text: Python's text mode turns CRLF and a lone CR into
# newlines before the paragraphs split.
CUSTOM_SHORT = "Prefer small PRs.\r\n\r\nAlways cite\rthe tested head."
CUSTOM_LONG = "\n\n".join(f"Rule {i}: " + "keep this paragraph whole " * 12 for i in range(20))
