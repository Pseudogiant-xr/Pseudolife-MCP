"""Live differential harness: the Python daemon (oracle) against the Rust
daemon, scenario by scenario, each side on its own disposable bank.

usage:
  python run.py live   --rust-bin PATH [--only NAME ...] [--out FILE] [--record]
  python run.py golden --rust-bin PATH [--only NAME ...] [--out FILE]
  python run.py mutants --rust-bin PATH_BUILT_WITH_FEATURE_MUTANTS [--only NAME ...]

``live`` runs both daemons and compares, per case: the status, the headers
clients read, and the body by value (JSON) or by bytes (text), after the
declared normalizers below. After every scenario it dumps both banks
(``dbstate``) and diffs them. Declared divergences are cases whose Rust side
must answer exactly ``501 {"error": "not_implemented", "path": P}``; they are
counted, never silently passed. ``--record`` writes the oracle's normalized
answers and bank states to ``goldens/`` so ``golden`` can check the Rust side
without a Python daemon. ``mutants`` runs each deliberate break compiled into
a ``--features mutants`` build and requires every one to produce a diff.

Exit status: 0 only when no case and no bank state differs.

Environment for the Rust daemon: ``ORT_DYLIB_PATH`` (ONNX Runtime library)
and ``PSEUDOLIFE_DAEMON_ONNX_DIR`` (the verified fp32 Qwen3 export).
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daemons  # noqa: E402
import dbstate  # noqa: E402
import pgdisposable as pg  # noqa: E402

REPO = HERE.parents[2]
GOLDENS = HERE / "goldens"
HEADERS_COMPARED = ("content-type", "cache-control", "location", "content-security-policy",
                    "x-frame-options", "referrer-policy", "x-content-type-options", "x-pl-board")
# (table, column) values that are wall-clock or random by construction.
DB_NONDETERMINISTIC = {("relations", "created_at"): "clock: wall clock per row (storage/postgres.py:765)"}
# State only the Python side writes during init, owned by later slices. Kind
# "row": a meta row by key; "table": every row; "sequence": its last_value.
DB_DECLARED = [
    ("row", "meta", "dream_ack_secret_v1", "dream tracking init writes a random secret (W3-H)"),
    ("row", "meta", "curation_listing_spelling_v2", "the listing-spelling carry-over stamps the clock (W2-E)"),
    ("table", "retrieval_events", None, "the warmup search logs a retrieval event (W2-D telemetry)"),
    ("sequence", "retrieval_events_id_seq", None, "advanced by that retrieval event (W2-D telemetry)"),
]
# /health keys only Python can emit, owned by later slices (each is conditional there).
HEALTH_DECLARED_ONLY_PYTHON = {"stall", "migration_partial", "dream_tracking_error",
                               "capacity_warning", "lesson_reconciliation_required"}
NOT_IMPLEMENTED = "not_implemented"
MUTANTS = ["skip-relation-seed", "skip-lease-epoch", "drop-alter-tail", "pair-ignores-origin",
           "health-omits-db", "route-405-as-404", "body-limit-off", "tokenless-maintainer-open",
           "no-backoff"]

T_DEFAULT = "tok-default-w1a-0001"
T_ALICE = "tok-alice-w1a-0002"
T_COLON = "tok:with:colons-0003"


# ---- HTTP ---------------------------------------------------------------------------

def call(port: int, method: str, path: str, headers=(), body: bytes | None = None,
         timeout: float = 120.0) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.putrequest(method, path, skip_host=any(k.lower() == "host" for k, _ in headers),
                 skip_accept_encoding=True)
    for k, v in headers:
        c.putheader(k, v)
    if body is not None:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    raw = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders() if k.lower() in HEADERS_COMPARED}
    ctype = hdrs.get("content-type", "")
    if ctype.startswith("application/json"):
        try:
            payload = {"json": json.loads(raw)}
        except ValueError:
            payload = {"bytes": raw.decode("latin-1")}
    else:
        payload = {"bytes": raw.decode("latin-1")}
    c.close()
    return {"status": r.status, "headers": hdrs, **payload}


def bearer(token: str) -> tuple[str, str]:
    return ("Authorization", f"Bearer {token}")


# ---- normalizers (rule-based, recorded with the goldens) -------------------------------

def normalize_health(body: dict, declared: list[str]) -> dict:
    body = json.loads(json.dumps(body))
    for k in sorted(HEALTH_DECLARED_ONLY_PYTHON & set(body)):
        declared.append(f"health.{k} (python only)")
        body.pop(k)
    # Free values are replaced only where present: a missing key must still diff.
    if isinstance(body.get("version"), str):
        body["version"] = "<free str>"
    if isinstance(body.get("updates"), dict):
        u = body["updates"]
        if isinstance(u.get("checked_at"), (int, float)) and not isinstance(u.get("checked_at"), bool):
            u["checked_at"] = "<free number>"
        if "latest_release" in u and (u["latest_release"] is None or isinstance(u["latest_release"], str)):
            u["latest_release"] = "<free str|null>"
    if isinstance(body.get("memory"), dict):
        body["memory"] = {"source": body["memory"].get("source")}
    if isinstance(body.get("db"), str) and body["db"].startswith("error: "):
        body["db"] = "error: <free>"
    for key in ("init_refusal", "not_ready"):
        if isinstance(body.get(key), str):
            body[key] = body[key][:40] + "<free>"
    emb = body.get("embedder")
    if isinstance(emb, dict) and set(emb) == {"backend", "device", "dtype"} \
            and emb["backend"] in ("torch", "onnx") and (emb["dtype"] is None or isinstance(emb["dtype"], str)):
        # Backend-dependent: Python reports dtype null for ONNX and a string for torch.
        body["embedder"] = {"device": emb["device"], "backend": "<torch|onnx>", "dtype": "<str|null>"}
    return body


def normalize_search(body: dict) -> dict:
    """Scores and access counts are free (spec.md "Free"); presence and type are not."""
    for e in body.get("entries", []) if isinstance(body.get("entries"), list) else []:
        if isinstance(e, dict):
            if isinstance(e.get("score"), (int, float)) and not isinstance(e["score"], bool):
                e["score"] = "<free number>"
            if isinstance(e.get("access_count"), int) and not isinstance(e["access_count"], bool):
                e["access_count"] = "<free int>"
    return body


def normalize_response(resp: dict, path: str, declared: list[str]) -> dict:
    resp = json.loads(json.dumps(resp))
    if urllib.parse.unquote(path.split("?")[0]) == "/health" and "json" in resp:
        resp["json"] = normalize_health(resp["json"], declared)
    if urllib.parse.unquote(path.split("?")[0]) == "/api/search" and isinstance(resp.get("json"), dict):
        resp["json"] = normalize_search(resp["json"])
    if "json" in resp and isinstance(resp["json"], dict) and resp["status"] in (400, 500):
        err = resp["json"].get("error")
        if isinstance(err, str) and err.startswith("memory bank not ready: "):
            # service.py:1430: the reason text is free, the shape is not.
            ok = re.fullmatch(r"memory bank not ready: .+ \(next retry in \d+s\)", err, re.S)
            resp["json"]["error"] = "memory bank not ready: <reason> (next retry in <n>s)" if ok else err
            return resp
        # Free wording for 400/500 messages that are not public codes.
        if isinstance(err, str) and (" " in err or ":" in err):
            resp["json"]["error"] = "<free text>"
    return resp


def diff_values(a, b, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in b:
                out.append(f"{path}.{k}: only in python")
            elif k not in a:
                out.append(f"{path}.{k}: only in rust")
            else:
                out += diff_values(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff_values(x, y, f"{path}[{i}]")]
    # Type-strict: Python's True == 1 and 1 == 1.0 must still diff on the wire.
    if type(a) is not type(b) or a != b:
        return [f"{path}: python {json.dumps(a)[:200]} vs rust {json.dumps(b)[:200]}"]
    return []


# ---- scenarios ------------------------------------------------------------------------

class Scenario:
    name = ""
    config_yaml: str | None = None
    env: dict[str, str] = {}
    seeds_principals = False
    seed_entries = False
    settle = True          # wait until both report db + embedder
    hold_lease = False     # take each bank's writer lease before the daemons start
    files: dict[str, str] = {}  # extra files in each data dir

    def timeline(self, procs: dict, holders: list) -> list[dict]:
        """Scenarios whose answers depend on time (lease, backoff, reaper):
        return cases already answered (``_answers``), or [] to use cases()."""
        return []

    def prepare_template(self, dsn: str) -> None:
        """Seed the template bank through Python's own write paths."""

    def cases(self) -> list[dict]:
        return []



def case(name, method, path, headers=(), body=None, declared=None):
    return {"name": name, "method": method, "path": path, "headers": list(headers),
            "body": body, "declared": declared}


def gate_cases(auth: tuple[str, str] | None) -> list[dict]:
    a = [auth] if auth else []
    js = ("Content-Type", "application/json")
    c = [
        case("health GET", "GET", "/health"),
        case("health POST", "POST", "/health"),
        case("health percent-encoded", "GET", "/%68ealth"),
        case("root redirect", "GET", "/"),
        case("ui index", "GET", "/ui"),
        case("ui slash", "GET", "/ui/"),
        case("ui asset js", "GET", "/ui/theme.js"),
        case("ui asset css", "GET", "/ui/assets/index-BFOM-F8H.css"),
        case("ui asset font", "GET", "/ui/assets/Geist-Variable-Bj2R_7yk.woff2"),
        case("ui asset png", "GET", "/ui/assets/pseudolife-mark-CiStlepW.png"),
        case("ui asset webp", "GET", "/ui/assets/pseudolife-logo-CWwQVyJT.webp"),
        case("ui asset txt", "GET", "/ui/assets/Geist-LICENSE.txt"),
        case("ui spa fallback", "GET", "/ui/graph/view"),
        case("ui traversal", "GET", "/ui/../../pyproject.toml"),
        case("ui traversal encoded", "GET", "/ui/%2e%2e/%2e%2e/pyproject.toml"),
        case("non-api unknown, no auth", "GET", "/whatever"),
        case("non-api unknown, auth", "GET", "/whatever", a),
        case("api root", "GET", "/api", a),
        case("api unknown path", "GET", "/api/nope", a),
        case("api POST unknown path, no body", "POST", "/api/nope", a),
        case("api GET on POST-only", "GET", "/api/delete", a),
        case("api POST on GET-only", "POST", "/api/stats", a),
        case("api PUT", "PUT", "/api/stats", a),
        case("api DELETE unknown", "DELETE", "/api/nope", a),
        case("api POST text/plain body", "POST", "/api/nope", a + [("Content-Type", "text/plain")], b"{}"),
        case("api POST json invalid", "POST", "/api/nope", a + [js], b"{nope"),
        case("api POST json array", "POST", "/api/nope", a + [js], b"[1,2]"),
        case("api POST json charset param", "POST", "/api/nope", a + [("Content-Type", "Application/JSON ; charset=utf-8")], b"{}"),
        case("api POST over control limit", "POST", "/api/nope", a + [js], b"{" + b" " * (256 * 1024) + b"}"),
        case("api POST at text limit path", "POST", "/api/facts/set", a + [js], b"{\"x\": \"" + b"a" * (300 * 1024) + b"\"}",
             declared="W2-E: facts/set handler"),
        case("api POST invalid utf-8", "POST", "/api/nope", a + [js], b"{\"x\": \"\xff\"}"),
        case("api POST coordination invalid utf-8", "POST", "/api/coordination/receive", a + [js], b"{\"x\": \"\xff\"}"),
        case("api GET coordination", "GET", "/api/coordination/receive", a),
        case("api POST coordination over 32K", "POST", "/api/coordination/receive", a + [js], b"{\"x\": \"" + b"a" * 40000 + b"\"}"),
        case("api maintainer unknown", "GET", "/api/maintainer/nope", a),
        case("api maintainer wrong verb", "GET", "/api/maintainer/send", a),
        case("api agents coordination view", "GET", "/api/agents?view=coordination", a,
             declared="W2-F: board snapshot" if auth else None),
        case("api agents coordination view POST", "POST", "/api/agents?view=coordination", a),
        case("api known GET (not ported)", "GET", "/api/stats", a, declared="W2-D: stats"),
        case("api config GET (not ported)", "GET", "/api/config", a, declared="W2-D: config read"),
        case("hook unknown under /api/hook", "GET", "/api/hook/nope", a),
        case("hook memory-changes wrong verb", "POST", "/api/hook/memory-changes", a),
        case("hook session-end wrong verb", "GET", "/api/hook/session-end", a),
        case("hook woke wrong verb", "GET", "/api/hook/woke", a),
        case("hook subagent wrong verb", "GET", "/api/hook/subagent", a),
        case("hook park-gate wrong verb", "POST", "/api/hook/park-gate", a),
        case("hook session-start wrong verb", "POST", "/api/hook/session-start", a),
        case("hook memory-policy wrong verb", "PUT", "/api/hook/memory-policy", a),
        case("hook coordination-start wrong verb", "POST", "/api/hook/coordination-start", a),
        case("pair GET", "GET", "/api/pair"),
        case("pair with Origin", "POST", "/api/pair", [("Origin", "http://localhost"), js], b"{}"),
        case("pair text/plain", "POST", "/api/pair", [("Content-Type", "text/plain")], b"{}"),
        case("pair over 1 KiB", "POST", "/api/pair", [js], b"{\"code\": \"" + b"a" * 2000 + b"\"}"),
        case("pair invalid json", "POST", "/api/pair", [js], b"{"),
        case("pair extra key", "POST", "/api/pair", [js], json.dumps({"code": "ABCD-EFGH-JKMN", "token_sha256": "a" * 64, "x": 1}).encode()),
        case("pair bad code", "POST", "/api/pair", [js], json.dumps({"code": "UUUU-UUUU-UUUU", "token_sha256": "a" * 64}).encode()),
        case("pair bad hash", "POST", "/api/pair", [js], json.dumps({"code": "ABCD-EFGH-JKMN", "token_sha256": "A" * 64}).encode()),
        case("mcp path, no auth", "GET", "/mcp", declared=None if auth else "W2-G: MCP transport"),
    ]
    return c


class Tokens(Scenario):
    """Auth configured: singular token plus a map; two stored principals
    (one paired, one revoked) written by Python's principal store."""
    name = "tokens"
    seeds_principals = True

    def __init__(self):
        self.env = {"PSEUDOLIFE_MCP_TOKEN": T_DEFAULT,
                    "PSEUDOLIFE_MCP_TOKENS": f"{T_ALICE}:Alice, {T_COLON}:bob, junk, x:default"}

    def prepare_template(self, dsn: str) -> None:
        import psycopg
        sys.path.insert(0, str(REPO))
        from pseudolife_memory import principal_store
        from pseudolife_memory.principals import secret_sha256
        from pseudolife_memory.storage.postgres import PostgresStorage
        PostgresStorage(dsn).close()   # the bank exists before principals are written
        with psycopg.connect(dsn, autocommit=True) as conn:
            for name in ("carol", "dave", "alice"):
                inv = principal_store.create_invite(conn, name, tier="core", board=True,
                                                    ttl_seconds=3600, replace=False)
                token = f"stored-{name}-token-w1a"
                principal_store.redeem(dsn, secret_sha256(inv["code"]), secret_sha256(token))
            principal_store.revoke(conn, "dave")

    def cases(self):
        auth = bearer(T_DEFAULT)
        c = gate_cases(auth)
        s = "/api/search?"
        q = lambda **kw: s + urllib.parse.urlencode(kw)  # noqa: E731
        c += [
            case("no auth header", "GET", "/api/stats"),
            case("basic scheme", "GET", "/api/stats", [("Authorization", f"Basic {T_DEFAULT}")]),
            case("bearer, no token", "GET", "/api/stats", [("Authorization", "Bearer")]),
            case("unknown token", "GET", "/api/nope", [bearer("not-a-real-token")]),
            case("mapped principal", "GET", "/api/nope", [bearer(T_ALICE)]),
            case("mapped token with colons", "GET", "/api/nope", [bearer(T_COLON)]),
            case("stored principal", "GET", "/api/nope", [bearer("stored-carol-token-w1a")]),
            case("stored but shadowed by env name", "GET", "/api/nope", [bearer("stored-alice-token-w1a")]),
            case("revoked stored principal", "GET", "/api/nope", [bearer("stored-dave-token-w1a")]),
            case("stored principal POST /api/config", "POST", "/api/config", [bearer("stored-carol-token-w1a")]),
            case("stored principal POST /api/daemon-notice", "POST", "/api/daemon-notice", [bearer("stored-carol-token-w1a")]),
            case("env principal POST /api/config", "POST", "/api/config", [auth], declared="W2-D: config write"),
            case("duplicate authorization, valid last", "GET", "/api/nope", [bearer("junk"), auth]),
            case("duplicate authorization, valid first", "GET", "/api/nope", [auth, bearer("junk")]),
            case("browser gate off with token", "GET", "/api/nope", [auth, ("Origin", "http://evil.example")]),
            case("hook memory-changes unauthorized", "GET", "/api/hook/memory-changes"),
            case("hook park-gate unauthorized", "GET", "/api/hook/park-gate"),
            case("hook woke unauthorized", "POST", "/api/hook/woke"),
            case("hook subagent unauthorized", "POST", "/api/hook/subagent"),
            case("hook session-end unauthorized", "POST", "/api/hook/session-end"),
            case("hook session-end over 16K", "POST", "/api/hook/session-end", [auth, ("Content-Type", "application/json")], b"{\"x\":\"" + b"a" * 17000 + b"\"}"),
            case("hook session-start", "GET", "/api/hook/session-start", declared="W2-D/W1-B: briefing text"),
            case("hook memory-changes authorized", "GET", "/api/hook/memory-changes", [auth], declared="W2-E: memory change note"),
            case("pair valid shape, unknown code", "POST", "/api/pair", [("Content-Type", "application/json")],
                 json.dumps({"code": "ABCD-EFGH-JKMN", "token_sha256": "b" * 64}).encode(),
                 declared="W2-F: pairing redemption"),
            case("maintainer known GET", "GET", "/api/maintainer", [auth], declared="W2-F: maintainer"),
            case("mcp path, auth", "GET", "/mcp", [auth], declared="W2-G: MCP transport"),
            case("search blank q", "GET", q(q=""), [auth]),
            case("search unknown band", "GET", q(q="memory", band="nope"), [auth]),
            case("search unknown band, blank q", "GET", q(band="nope"), [auth]),
            case("search basic", "GET", q(q="how does the daemon start", top_k=3), [auth]),
        ]
        return c


class Tokenless(Scenario):
    """No token: open loopback mode and the browser gate."""
    name = "tokenless"

    def cases(self):
        c = gate_cases(None)
        c += [
            case("browser gate: foreign origin", "GET", "/api/nope", [("Origin", "http://evil.example")]),
            case("browser gate: loopback origin", "GET", "/api/nope", [("Origin", "http://localhost:5173")]),
            case("browser gate: userinfo origin", "GET", "/api/nope", [("Origin", "http://user@localhost")]),
            case("browser gate: ipv6 origin", "GET", "/api/nope", [("Origin", "http://[::1]:8765")]),
            case("browser gate: foreign host", "GET", "/api/nope", [("Host", "evil.example:8765")]),
            case("browser gate: hook foreign origin", "GET", "/api/hook/memory-changes", [("Origin", "http://evil.example")]),
            case("browser gate: pair ignores gate", "POST", "/api/pair", [("Host", "evil.example"), ("Content-Type", "application/json")],
                 json.dumps({"code": "ABCD-EFGH-JKMN", "token_sha256": "b" * 64}).encode()),
            case("tokenless pair refused", "POST", "/api/pair", [("Content-Type", "application/json")],
                 json.dumps({"code": "ABCD-EFGH-JKMN", "token_sha256": "b" * 64}).encode()),
            case("tokenless maintainer", "GET", "/api/maintainer", []),
            case("tokenless maintainer POST", "POST", "/api/maintainer/send", []),
            case("tokenless agents coordination", "GET", "/api/agents?view=coordination", []),
            case("tokenless hook woke authorized", "POST", "/api/hook/woke", [], declared="W2-F: woke marker"),
            case("tokenless mcp", "GET", "/mcp", [("Host", "127.0.0.1:1")], declared="W2-G: MCP transport"),
            case("origin: unclosed IPv6 bracket", "GET", "/api/nope", [("Origin", "http://[::1")]),
            case("origin: bracketed name", "GET", "/api/nope", [("Origin", "http://[localhost]")]),
            case("origin: bracketed IPv4", "GET", "/api/nope", [("Origin", "http://[127.0.0.1]")]),
            case("origin: invalid scheme", "GET", "/api/nope", [("Origin", "x y://localhost")]),
            case("origin: tab inside host", "GET", "/api/nope", [("Origin", "http://local\thost")]),
            case("origin: hook, unclosed bracket", "GET", "/api/hook/park-gate", [("Origin", "http://[::1")]),
            case("origin: duplicate, foreign first", "GET", "/api/nope",
                 [("Origin", "http://evil.example"), ("Origin", "http://localhost")]),
            case("origin: duplicate, loopback first", "GET", "/api/nope",
                 [("Origin", "http://localhost"), ("Origin", "http://evil.example")]),
            case("ui: embedded NUL", "GET", "/ui/a%00b"),
        ]
        return c


class PairBudget(Scenario):
    """The 20-per-minute failed-redemption budget, then 429."""
    name = "pair-budget"
    settle = False

    def cases(self):
        body = json.dumps({"code": "UUUU", "token_sha256": "a"}).encode()
        return [case(f"pair attempt {i}", "POST", "/api/pair", [("Content-Type", "application/json")], body)
                for i in range(22)]


class CustomConfig(Scenario):
    """A config.yaml that moves every knob /health and the gate expose."""
    name = "config"
    config_yaml = """
memory:
  top_k: 3
  hide_superseded: true
  miras:
    preset: continuum
  search:
    min_score: 0.4
  bm25:
    enabled: false
  dream:
    enabled: false
coordination:
  enabled: false
  wake:
    per_recipient_per_hour: 7
    urgent_per_sender_per_hour: 2
    nightly_total: 50
    fan_out_stagger_seconds: 5
    active_seconds: 9
    authority_per_sender_per_hour: 3
    nudge_interval_seconds: 99
updates:
  check_releases: false
  unattended_clients: true
  unattended_daemon: true
unknown_section:
  anything: 1
"""

    def __init__(self):
        self.env = {"PSEUDOLIFE_MCP_TOKEN": T_DEFAULT}

    def cases(self):
        auth = bearer(T_DEFAULT)
        s = "/api/search?"
        q = lambda **kw: s + urllib.parse.urlencode(kw)  # noqa: E731
        return [
            case("health with custom config", "GET", "/health"),
            case("band of continuum", "GET", q(q="memory", band="working,forever"), [auth]),
            case("band of flat refused", "GET", q(q="memory", band="flat"), [auth]),
            case("search top_k=0 uses config", "GET", q(q="memory", top_k=0), [auth]),
        ]


class ExtractorConfigured(Scenario):
    name = "extractor"
    config_yaml = "memory:\n  dream:\n    enabled: true\n"

    def __init__(self):
        self.env = {"PSEUDOLIFE_DREAM_BASE_URL": "http://127.0.0.1:9/v1", "PSEUDOLIFE_DREAM_MODEL": "m",
                    "PSEUDOLIFE_BUILD_GIT_SHA": "abc123", "PSEUDOLIFE_BUILD_DIRTY": "maybe",
                    "PSEUDOLIFE_BUILD_SOURCE": ""}
        self.files = {"last-backup.json": '\ufeff{"created_at": "2026-10-9t01:02:03z", "rotation": "ok"}'}

    def cases(self):
        return [case("health: extractor configured, build stamp", "GET", "/health")]


class DbDown(Scenario):
    """A DSN nothing listens on: Python keeps /health at 200 with no db key,
    and the stored-principal view never loads."""
    name = "db-down"
    settle = False

    def __init__(self):
        self.env = {"PSEUDOLIFE_MCP_TOKEN": T_DEFAULT}

    def cases(self):
        u = [bearer("unknown-w1a")]
        return [case("health, db unreachable", "GET", "/health"),
                case("health again", "GET", "/health"),
                case("env bearer still authenticates", "GET", "/api/nope", [bearer(T_DEFAULT)]),
                case("unknown bearer: principals unavailable", "GET", "/api/nope", u),
                case("missing bearer still 401", "GET", "/api/nope"),
                case("non-api, unknown bearer", "GET", "/whatever", u),
                case("session-end, unknown bearer", "POST", "/api/hook/session-end", u),
                case("memory-changes, unknown bearer (unauthorized)", "GET", "/api/hook/memory-changes", u),
                case("search, db unreachable", "GET", "/api/search?q=x", [bearer(T_DEFAULT)])]


class LeaseHeld(Scenario):
    """Another session holds the writer lease from before start: not_ready,
    503, the backoff refusal on a call, then recovery once released."""
    name = "lease-held"
    settle = False
    hold_lease = True

    def __init__(self):
        self.env = {"PSEUDOLIFE_MCP_TOKEN": T_DEFAULT}

    def timeline(self, procs, holders):
        auth = [bearer(T_DEFAULT)]

        def both(m, p, h=()):
            return (call(procs["python"].port, m, p, list(h)), call(procs["rust"].port, m, p, list(h)))

        out = []
        time.sleep(8)  # both warmups were refused once and are backing off
        for label, (m, p, h) in [("health while held", ("GET", "/health", ())),
                                 ("search while backing off", ("GET", "/api/search?q=x", auth)),
                                 ("health while held again", ("GET", "/health", ()))]:
            c = case(label, m, p, h)
            c["_answers"] = both(m, p, h)
            out.append(c)
        release(holders)
        wait_settled([d.port for d in procs.values()], timeout=240, token=self.env.get("PSEUDOLIFE_MCP_TOKEN"))
        c = case("health after release and backoff", "GET", "/health")
        c["_answers"] = both("GET", "/health")
        out.append(c)
        return out


class Reaper(Scenario):
    """The lease is held until both warmups give up (backoff at its 60 s
    cap); after release nothing calls the daemons, yet the session reaper's
    init retry recovers them (PSEUDOLIFE_SESSION_REAP_SECONDS=5)."""
    name = "reaper"
    settle = False
    hold_lease = True

    def __init__(self):
        self.env = {"PSEUDOLIFE_SESSION_REAP_SECONDS": "5"}

    def timeline(self, procs, holders):
        time.sleep(110)  # failures near 0, 5, 15, 35, 75 s: the fifth arms 60 s; warmup stops
        c = case("health after warmup gave up", "GET", "/health")
        c["_answers"] = (call(procs["python"].port, "GET", "/health"), call(procs["rust"].port, "GET", "/health"))
        out = [c]
        release(holders)
        wait_settled([d.port for d in procs.values()], timeout=240, token=self.env.get("PSEUDOLIFE_MCP_TOKEN"))
        c = case("health after reaper retry", "GET", "/health")
        c["_answers"] = (call(procs["python"].port, "GET", "/health"), call(procs["rust"].port, "GET", "/health"))
        out.append(c)
        return out


def take_leases(dsns):
    import psycopg
    holders = []
    for dsn in dsns:
        conn = psycopg.connect(dsn, autocommit=True)
        conn.execute("SELECT pg_advisory_lock(hashtextextended('pseudolife-bank-writer', 0))")
        holders.append(conn)
    return holders


def release(holders):
    for h in holders:
        h.close()
    holders.clear()


class DimMismatch(Scenario):
    """entries.embedding is vector(384): init_refusal before any DDL."""
    name = "dim-mismatch"
    settle = False

    def prepare_template(self, dsn: str) -> None:
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("CREATE TABLE entries (id BIGSERIAL PRIMARY KEY, embedding vector(384))")

    def cases(self):
        return [case("health, dim refusal", "GET", "/health"),
                case("search during refusal", "GET", "/api/search?q=x")]


class StampedBank(Scenario):
    """A current bank stamped schema 99 with a non-numeric lease epoch."""
    name = "stamped"

    def prepare_template(self, dsn: str) -> None:
        import psycopg
        sys.path.insert(0, str(REPO))
        from pseudolife_memory.storage.postgres import PostgresStorage
        PostgresStorage(dsn).close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("UPDATE meta SET value = '99'::jsonb WHERE key = 'schema_version'")
            conn.execute("UPDATE meta SET value = '\"x\"'::jsonb WHERE key = 'writer_lease_epoch'")
            conn.execute("DELETE FROM relations WHERE name = 'uses'")

    def cases(self):
        return [case("health on stamped bank", "GET", "/health")]


class SeededBank(Scenario):
    """Entries stored by Python under the continuum preset, served flat:
    hydration rewrites the stale band stamps, and search answers by value."""
    name = "seeded"

    def __init__(self):
        self.env = {"PSEUDOLIFE_MCP_TOKEN": T_DEFAULT}

    def prepare_template(self, dsn: str) -> None:
        seed = HERE / "seed_entries.py"
        import subprocess
        env = daemons.base_env(daemons.scratch_root() / "seed-home", {"PSEUDOLIFE_MCP_DATABASE_URL": dsn})
        (daemons.scratch_root() / "seed-home" / "data").mkdir(parents=True, exist_ok=True)
        r = subprocess.run([sys.executable, str(seed), dsn], env=dict(env, PYTHONPATH=str(REPO)),
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"seeding failed: {r.stderr[-2000:]}")

    def cases(self):
        auth = bearer(T_DEFAULT)
        s = "/api/search?"
        q = lambda **kw: s + urllib.parse.urlencode(kw)  # noqa: E731
        return [case(f"seeded search {i}", "GET", q(q=text, top_k=5, disable_recency_boost="true"), [auth])
                for i, text in enumerate(["how do I deploy the daemon", "what is a contender",
                                          "bearer tokens for remote clients", "schema version bump"])]


STARTUP_REFUSALS = [
    ("token map parses to nothing", {"PSEUDOLIFE_MCP_TOKENS": "junk,,x:default"}, None, 2),
    ("non-loopback bind without token", {"PSEUDOLIFE_MCP_HOST": "0.0.0.0"}, None, 2),
    ("moved bank", {}, "moved.json", 2),
    ("invalid fusion", {}, "memory:\n  search:\n    fusion: nope\n", 1),
    ("unknown preset", {}, "memory:\n  miras:\n    preset: nope\n", 1),
    ("wake cap not int", {}, "coordination:\n  wake:\n    nightly_total: 1.5\n", 1),
    ("port not an integer", {"PSEUDOLIFE_MCP_PORT": "abc"}, None, 1),
    ("unfinished move", {}, "move.json", 2),
    ("port out of range, non-loopback, no token", {"PSEUDOLIFE_MCP_PORT": "70000", "PSEUDOLIFE_MCP_HOST": "0.0.0.0"}, None, 2),
    ("autosave seconds not a number", {"PSEUDOLIFE_MCP_AUTOSAVE_SECONDS": "x"}, None, 1),
    ("reap seconds not a number", {"PSEUDOLIFE_SESSION_REAP_SECONDS": "x"}, None, 1),
    ("dream sweep interval not a number", {}, "memory:\n  dream:\n    sweep_interval_seconds: abc\n", 1),
]

class NullEmbedding(Scenario):
    """A stored row with a NULL vector: hydration fails, not_ready, backoff."""
    name = "null-embedding"
    settle = False

    def prepare_template(self, dsn: str) -> None:
        import psycopg
        sys.path.insert(0, str(REPO))
        from pseudolife_memory.storage.postgres import PostgresStorage
        PostgresStorage(dsn).close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("ALTER TABLE entries ALTER COLUMN embedding DROP NOT NULL")
            conn.execute("INSERT INTO entries (band, text, embedding, ts, source) "
                         "VALUES ('flat', 'no vector', NULL, 1.0, 'agent')")

    def timeline(self, procs, holders):
        time.sleep(90)  # both have loaded their embedders and failed hydration
        c = case("health after a NULL-vector hydration failure", "GET", "/health")
        c["_answers"] = (call(procs["python"].port, "GET", "/health"), call(procs["rust"].port, "GET", "/health"))
        return [c]


class UnconstrainedDims(Scenario):
    """entries.embedding as untyped vector holding a 384-d row: the schema
    guard passes, the hydrated-dimension guard refuses (init_refusal)."""
    name = "unconstrained-dims"
    settle = False

    def prepare_template(self, dsn: str) -> None:
        import psycopg
        sys.path.insert(0, str(REPO))
        from pseudolife_memory.storage.postgres import PostgresStorage
        PostgresStorage(dsn).close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("ALTER TABLE entries ALTER COLUMN embedding TYPE vector")
            conn.execute("INSERT INTO entries (band, text, embedding, ts, source) VALUES "
                         "('flat', 'old model', ('[' || array_to_string(array_fill(0.1::real, ARRAY[384]), ',') || ']')::vector, 1.0, 'agent')")

    def timeline(self, procs, holders):
        time.sleep(90)
        c = case("health after a stale-dimension refusal", "GET", "/health")
        c["_answers"] = (call(procs["python"].port, "GET", "/health"), call(procs["rust"].port, "GET", "/health"))
        return [c]


class DbLost(Scenario):
    """Storage exists, then the database refuses new connections: /health
    pings fail (degraded, 503); allowed again, both recover."""
    name = "db-lost"

    def __init__(self):
        self.env = {"PSEUDOLIFE_MCP_TOKEN": T_DEFAULT}

    def timeline(self, procs, holders):
        import psycopg
        out = []
        dbs = [f"pl_cf_w1a_db_lost_{side}" for side in ("py", "rs")]
        with psycopg.connect(pg.dsn("pl_cf_w1a_db_lost_t"), autocommit=True) as conn:
            for db in dbs:
                conn.execute(f'ALTER DATABASE "{db}" WITH ALLOW_CONNECTIONS false')
            try:
                c = case("health while the database refuses connections", "GET", "/health")
                c["_answers"] = (call(procs["python"].port, "GET", "/health"),
                                 call(procs["rust"].port, "GET", "/health"))
                out.append(c)
            finally:
                for db in dbs:
                    conn.execute(f'ALTER DATABASE "{db}" WITH ALLOW_CONNECTIONS true')
        c = case("health after connections are allowed again", "GET", "/health")
        c["_answers"] = (call(procs["python"].port, "GET", "/health"), call(procs["rust"].port, "GET", "/health"))
        out.append(c)
        return out


class TrustBind(Scenario):
    """Tokenless on 0.0.0.0, allowed by PSEUDOLIFE_MCP_TRUST_BIND."""
    name = "trust-bind"

    def __init__(self):
        self.env = {"PSEUDOLIFE_MCP_HOST": "0.0.0.0", "PSEUDOLIFE_MCP_TRUST_BIND": "On"}

    def cases(self):
        return [case("health on a trusted wildcard bind", "GET", "/health"),
                case("browser gate still applies", "GET", "/api/nope", [("Host", "evil.example")])]


SCENARIOS = {s.name: s for s in (Tokens, Tokenless, PairBudget, CustomConfig, ExtractorConfigured,
                                  DbDown, LeaseHeld, Reaper, DimMismatch, StampedBank, SeededBank,
                                  TrustBind, NullEmbedding, UnconstrainedDims, DbLost)}


# ---- running ---------------------------------------------------------------------------

def rust_env(extra: dict[str, str]) -> dict[str, str]:
    out = dict(extra)
    for key in ("ORT_DYLIB_PATH", "PSEUDOLIFE_DAEMON_ONNX_DIR", "PSEUDOLIFE_DAEMON_MUTANT"):
        if os.environ.get(key):
            out[key] = os.environ[key]
    out["PSEUDOLIFE_DAEMON_STATIC_DIR"] = str(REPO / "pseudolife_memory" / "web" / "static")
    return out


def common_env(scn: Scenario, dsn: str) -> dict[str, str]:
    env = {"PSEUDOLIFE_RELEASE_CHECK": "0", "PSEUDOLIFE_PLUGIN_DIR": str(REPO / "plugin")}
    if dsn:
        env["PSEUDOLIFE_MCP_DATABASE_URL"] = dsn
    env.update(scn.env)
    return env


def settled(port: int, token: str | None) -> bool:
    """Init finished: health is ok with a db and an embedder, and a blank
    search (which runs ``ensure_init`` under the service lock, then answers
    without touching the bank) succeeds."""
    try:
        body = call(port, "GET", "/health", timeout=30).get("json") or {}
        if not (body.get("status") == "ok" and body.get("db") == "ok" and "embedder" in body):
            return False
        r = call(port, "GET", "/api/search?q=", [bearer(token)] if token else [], timeout=300)
        return r["status"] == 200
    except OSError:
        return False


def wait_settled(ports: list[int], timeout: float = 600.0, token: str | None = None) -> None:
    deadline = time.monotonic() + timeout
    pending = set(ports)
    while pending and time.monotonic() < deadline:
        pending = {p for p in pending if not settled(p, token)}
        if pending:
            time.sleep(2)
    if pending:
        raise RuntimeError(f"daemons on {sorted(pending)} did not settle within {timeout}s")


def compare_case(c: dict, py: dict | None, rs: dict) -> dict:
    declared: list[str] = []
    row = {"case": c["name"], "method": c["method"], "path": c["path"][:120],
           "python_status": py and py["status"], "rust_status": rs["status"], "diffs": [], "declared": None}
    if c["declared"]:
        row["declared"] = c["declared"]
        want = {"error": NOT_IMPLEMENTED, "path": urllib.parse.unquote(c["path"].split("?")[0])}
        if rs["status"] != 501 or rs.get("json") != want:
            row["diffs"].append(f"declared case: rust must answer 501 {want}, got {rs['status']} {rs.get('json') or rs.get('bytes', '')[:120]}")
        h = rs["headers"]
        if (h.get("content-type"), h.get("cache-control"), h.get("x-content-type-options")) != \
                ("application/json; charset=utf-8", "no-store", "nosniff"):
            row["diffs"].append(f"declared case: JSON transport headers wrong: {h}")
        return row
    a = normalize_response(py, c["path"], declared)
    b = normalize_response(rs, c["path"], [])
    row["diffs"] = diff_values(a, b)
    row["declared_omissions"] = declared
    return row


def run_scenario(scn: Scenario, binary: Path, root: Path, mode: str, record: bool) -> dict:
    tag = scn.name.replace("-", "_")
    template = f"pl_cf_w1a_{tag}_t"
    dsn_t = pg.create(template)
    scn.prepare_template(dsn_t)
    # The banks' state before either daemon touched them: declared writes and
    # clock values are judged against it, so changes to existing rows show.
    before = dbstate.dump(dsn_t)
    dbs = {"python": f"pl_cf_w1a_{tag}_py", "rust": f"pl_cf_w1a_{tag}_rs"}
    dsns = {k: pg.create(v, template=template) for k, v in dbs.items()}
    if scn.name == "db-down":
        dsns = {k: f"postgresql://nobody:nothing@127.0.0.1:{daemons.free_port()}/pl_cf_w1a_down" for k in dsns}
    procs = {}
    holders = take_leases(list(dsns.values())) if scn.hold_lease else []
    try:
        if mode in ("live", "record"):
            home = daemons.make_home(root, f"{tag}-py", scn.config_yaml)
            for name, text in scn.files.items():
                (home / "data" / name).write_text(text, encoding="utf-8")
            procs["python"] = daemons.python_daemon(home, daemons.free_port(),
                                                    daemons.base_env(home, common_env(scn, dsns["python"])))
        home = daemons.make_home(root, f"{tag}-rs", scn.config_yaml)
        for name, text in scn.files.items():
            (home / "data" / name).write_text(text, encoding="utf-8")
        procs["rust"] = daemons.rust_daemon(binary, home, daemons.free_port(),
                                            daemons.base_env(home, rust_env(common_env(scn, dsns["rust"]))))
        for d in procs.values():
            d.start(240)
        if scn.settle:
            wait_settled([d.port for d in procs.values()], token=scn.env.get("PSEUDOLIFE_MCP_TOKEN"))
        rows = []
        golden = load_golden(scn.name) if mode == "golden" else None
        cases = scn.timeline(procs, holders) or scn.cases()
        for i, c in enumerate(cases):
            if c.get("_answers"):
                py_r, rs_r = c["_answers"]
            else:
                py_r = call(procs["python"].port, c["method"], c["path"], c["headers"], c["body"]) if "python" in procs else golden["responses"][i]
                rs_r = call(procs["rust"].port, c["method"], c["path"], c["headers"], c["body"])
            rows.append(compare_case(c, py_r, rs_r))
            if record:
                rows[-1]["_python"] = normalize_response(py_r, c["path"], [])
    finally:
        release(holders)
        for d in procs.values():
            d.stop()
    states, scrubbed = {}, {}
    if scn.name != "db-down":
        sides = dbs if mode != "golden" else {"rust": dbs["rust"]}
        for side, name in sides.items():
            raw = dbstate.dump(pg.dsn(name))
            out = scrub_declared_rows(raw, before)
            states[side] = dbstate.normalize(out["state"], DB_NONDETERMINISTIC, before)
            scrubbed[side] = out["declared"]
    db_diffs = dbstate.diff(states["python"], states["rust"]) if len(states) == 2 else []
    if mode == "golden" and golden.get("db_state"):
        db_diffs = dbstate.diff(golden["db_state"], states["rust"])
    result = {"scenario": scn.name, "cases": rows, "db_diffs": db_diffs, "declared_db_writes": scrubbed}
    if record:
        save_golden(scn.name, rows, states.get("python"))
    for name in [template, *dbs.values()]:
        pg.drop(name)
    return result


def scrub_declared_rows(state: dict, before: dict) -> dict:
    """Remove declared Python-only init writes from a state (either side),
    only where they are NEW against the pre-start state ``before``; a change
    to or a deletion of an existing row is never scrubbed. The record is
    reported, never diffed."""
    declared = []
    for kind, table, key, why in DB_DECLARED:
        if kind == "sequence":
            prior = {r[1]: r for r in before["catalog"]["sequences"]}
            for row in state["catalog"]["sequences"]:
                if row[1] == table and prior.get(table) is not None and row[5] != prior[table][5]:
                    row[5] = prior[table][5]
                    declared.append(f"sequence {table}: {why}")
            continue
        t = state["rows"].get(f"public.{table}")
        if not t:
            continue
        old = {json.dumps(r, sort_keys=True) for r in before["rows"].get(f"public.{table}", {}).get("rows", [])}
        keep = [r for r in t["rows"]
                if json.dumps(r, sort_keys=True) in old or (kind == "row" and r[0] != key)]
        if len(keep) != len(t["rows"]):
            declared.append(f"{table}{'.' + key if key else ''}: {why}")
            t["rows"] = keep
    return {"state": state, "declared": declared}


def run_refusals(binary: Path, root: Path) -> list[dict]:
    rows = []
    for name, env, extra, code in STARTUP_REFUSALS:
        exits = {}
        for side in ("python", "rust"):
            home = daemons.make_home(root, f"refusal-{side}", None)
            if extra in ("moved.json", "move.json"):
                (home / "data" / extra).write_text('{"moved_to": "elsewhere", "move_id": "m1"}', encoding="utf-8")
            elif extra:
                (home / "data" / "config.yaml").write_text(extra, encoding="utf-8")
            e = daemons.base_env(home, common_env(Scenario(), "postgresql://nobody@127.0.0.1:9/pl_cf_w1a_none"))
            e.update(env)
            port = daemons.free_port()
            d = (daemons.python_daemon(home, port, e) if side == "python"
                 else daemons.rust_daemon(binary, home, port, rust_env(e)))
            try:
                d.start(120, expect_exit=True)
                exits[side] = d.wait_exit(60)
            finally:
                d.stop()
        rows.append({"case": f"startup: {name}", "python_exit": exits["python"], "rust_exit": exits["rust"],
                     "diffs": [] if exits["python"] == exits["rust"] == code
                     else [f"exit: python {exits['python']} rust {exits['rust']} (want {code})"]})
    return rows


def load_golden(name: str) -> dict:
    return json.loads((GOLDENS / f"{name}.json").read_text(encoding="utf-8"))


def save_golden(name: str, rows: list[dict], state: dict | None) -> None:
    GOLDENS.mkdir(exist_ok=True)
    data = {"normalizers": {"db_nondeterministic": [list(k) + [v] for k, v in DB_NONDETERMINISTIC.items()],
                            "db_declared": [list(r) for r in DB_DECLARED],
                            "health": "normalize_health in run.py"},
            "responses": [r.pop("_python") for r in rows], "db_state": state}
    (GOLDENS / f"{name}.json").write_text(json.dumps(data, indent=1, sort_keys=True) + "\n",
                                          encoding="utf-8", newline="\n")


def summarize(results: list[dict], refusals: list[dict]) -> tuple[int, int, int]:
    cases = sum(len(r["cases"]) for r in results) + len(refusals)
    diff_cases = (sum(1 for r in results for c in r["cases"] if c["diffs"])
                  + sum(1 for r in refusals if r["diffs"]))
    db = sum(1 for r in results if r["db_diffs"])
    return cases, diff_cases, db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live", "golden", "mutants"])
    ap.add_argument("--rust-bin", required=True, type=Path)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--no-refusals", action="store_true")
    args = ap.parse_args()
    root = daemons.scratch_root()
    names = args.only or list(SCENARIOS) + ["refusals"]
    unknown = [n for n in names if n not in SCENARIOS and n != "refusals"]
    if unknown or not names:
        ap.error(f"unknown scenario(s) {unknown}; choose from {sorted(SCENARIOS)} or refusals")

    def one_pass(mode: str) -> tuple[list[dict], list[dict]]:
        results = []
        for n in names:
            if n not in SCENARIOS:
                continue
            r = run_scenario(SCENARIOS[n](), args.rust_bin, root, mode, args.record)
            results.append(r)
            bad = [c["case"] for c in r["cases"] if c["diffs"]]
            print(f"[{n}] cases {len(r['cases'])} diffs {len(bad)} db_diffs {len(r['db_diffs'])} {bad[:6]}",
                  flush=True)
            if args.out:
                args.out.write_text(json.dumps({"partial": results}, indent=1), encoding="utf-8")
        refusals = (run_refusals(args.rust_bin, root)
                    if "refusals" in names and mode != "golden" and not args.no_refusals else [])
        return results, refusals

    if args.mode == "mutants":
        os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
        control, control_refusals = one_pass("live")
        c_cases, c_diff, c_db = summarize(control, control_refusals)
        print(f"control: {c_diff} case diffs, {c_db} bank-state diffs", flush=True)
        if c_diff or c_db:
            print("control run is not clean; mutant results would not be attributable")
            return 1
        outcome = {}
        for m in MUTANTS:
            os.environ["PSEUDOLIFE_DAEMON_MUTANT"] = m
            results, refusals = one_pass("live")
            cases, diff_cases, db = summarize(results, refusals)
            outcome[m] = {"diff_cases": diff_cases, "db_diff_scenarios": db}
            print(f"mutant {m}: {diff_cases} case diffs, {db} bank-state diffs", flush=True)
        os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
        survivors = [m for m, o in outcome.items() if not (o["diff_cases"] or o["db_diff_scenarios"])]
        if args.out:
            args.out.write_text(json.dumps({"mutants": outcome, "survivors": survivors}, indent=1), encoding="utf-8")
        print("survivors:", survivors or "none")
        return 1 if survivors else 0

    results, refusals = one_pass(args.mode)
    cases, diff_cases, db = summarize(results, refusals)
    declared = sum(1 for r in results for c in r["cases"] if c["declared"])
    for r in results:
        for c in r["cases"]:
            if c["diffs"]:
                print(f"DIFF [{r['scenario']}] {c['case']}: {c['diffs'][:4]}")
        for d in r["db_diffs"][:20]:
            print(f"DB DIFF [{r['scenario']}] {d}")
    for r in refusals:
        if r["diffs"]:
            print(f"DIFF {r['case']}: {r['diffs']}")
    summary = {"cases": cases, "diff_cases": diff_cases, "declared_cases": declared,
               "db_state_scenarios": sum(1 for r in results if r["scenario"] != "db-down"),
               "db_diff_scenarios": db}
    print(json.dumps(summary))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "scenarios": results, "refusals": refusals},
                                       indent=1), encoding="utf-8")
    return 1 if diff_cases or db else 0


if __name__ == "__main__":
    raise SystemExit(main())
