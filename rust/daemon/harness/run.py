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
import tempfile
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "embedding":
    import embedding
    raise SystemExit(embedding.main(sys.argv[2:]))
import daemons  # noqa: E402
import dbstate  # noqa: E402
import pgdisposable as pg  # noqa: E402

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
MUTANTS = ["del-unescaped", "auth-candidate-order", "serde-json-writer", "skip-relation-seed", "skip-lease-epoch", "drop-alter-tail", "pair-ignores-origin",
           "health-omits-db", "route-405-as-404", "body-limit-off", "tokenless-maintainer-open",
           "no-backoff", "static-redirect", "static-no-csp", "static-traversal-open", "static-wrong-type",
           "static-json-whitespace", "static-string-prefix"]

T_DEFAULT = "tok-default-w1a-0001"
T_ALICE = "tok-alice-w1a-0002"
T_COLON = "tok:with:colons-0003"


# ---- HTTP ---------------------------------------------------------------------------

def call(port: int, method: str, path: str, headers=(), body: bytes | None = None,
         timeout: float = 120.0, compare_length: bool = False, body_bytes: bool = False) -> dict:
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
    hdrs = {k.lower(): v for k, v in r.getheaders() if k.lower() in HEADERS_COMPARED
            or (compare_length and k.lower() == "content-length")}
    ctype = hdrs.get("content-type", "")
    if ctype.startswith("application/json") and not body_bytes:
        try:
            payload = {"json": json.loads(raw), "raw": raw.decode("utf-8")}
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
    lb = body.get("last_backup")
    if isinstance(lb, dict) and isinstance(lb.get("age_hours"), (int, float)) \
            and not isinstance(lb.get("age_hours"), bool):
        lb["age_hours"] = "<free number>"  # measured against the clock at answer time
    if isinstance(body.get("memory"), dict):
        body["memory"] = {"source": body["memory"].get("source")}
    if isinstance(body.get("db"), str) and body["db"].startswith("error: "):
        body["db"] = "error: <free>"
    for key in ("init_refusal", "not_ready"):
        if isinstance(body.get(key), str):
            # The reason text is free (spec L); its kind, before the first ": ", is not.
            body[key] = body[key].split(": ", 1)[0][:60] + ": <free>"
    emb = body.get("embedder")
    if isinstance(emb, dict) and set(emb) == {"backend", "device", "dtype"} \
            and emb["backend"] in ("torch", "onnx") and (emb["dtype"] is None or isinstance(emb["dtype"], str)):
        # Backend-dependent: Python reports dtype null for ONNX and a string for torch.
        body["embedder"] = {"device": emb["device"], "backend": "<torch|onnx>", "dtype": "<str|null>"}
    return body


def seed_clock_scrub(resp: dict) -> dict:
    """Golden replay only: entries seeded into a template carry the moment
    they were seeded, which a later replay cannot reproduce."""
    resp = json.loads(json.dumps(resp))
    body = resp.get("json")
    if isinstance(body, dict) and isinstance(body.get("entries"), list):
        for e in body["entries"]:
            if isinstance(e, dict):
                for k in ("timestamp", "superseded_at"):
                    if isinstance(e.get(k), (int, float)) and not isinstance(e.get(k), bool):
                        e[k] = "<seed clock>"
    return resp


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


def _raw_tree(text: str):
    """The body as written: objects as ordered pairs, numbers as their text."""
    return json.loads(text, object_pairs_hook=lambda pairs: ("obj", pairs),
                      parse_float=lambda t: ("num", t), parse_int=lambda t: ("num", t),
                      parse_constant=lambda t: ("num", t))


def _free(v) -> bool:
    return isinstance(v, str) and v.startswith("<") and (">" in v)


def raw_diffs(py: dict, rs: dict, normalized: dict) -> list[str]:
    """What a by-value comparison cannot see: number spellings, key order
    and ``ensure_ascii``, everywhere the normalized value is not free."""
    if "raw" not in py or "raw" not in rs:
        return []
    out = []
    if py["raw"].isascii() != rs["raw"].isascii():
        out.append(f"raw: ensure_ascii differs (python ascii={py['raw'].isascii()}, rust ascii={rs['raw'].isascii()})")

    def walk(a, b, n, path):
        if _free(n):
            return
        if isinstance(n, dict) and isinstance(a, tuple) and a[0] == "obj" and isinstance(b, tuple) and b[0] == "obj":
            ka = [k for k, _ in a[1] if k in n]
            kb = [k for k, _ in b[1] if k in n]
            if ka != kb:
                out.append(f"raw {path}: key order python {ka} vs rust {kb}")
            da, db = dict(a[1]), dict(b[1])
            for k, v in n.items():
                if k in da and k in db:
                    walk(da[k], db[k], v, f"{path}.{k}")
        elif isinstance(n, list) and isinstance(a, list) and isinstance(b, list):
            for i, (x, y, m) in enumerate(zip(a, b, n)):
                walk(x, y, m, f"{path}[{i}]")
        elif isinstance(a, tuple) and a[0] == "num" and isinstance(b, tuple) and b[0] == "num":
            if a[1] != b[1]:
                out.append(f"raw {path}: number python {a[1]} vs rust {b[1]}")
        elif isinstance(n, str) and isinstance(a, str) and a == n:
            # The literal as json.dumps (ensure_ascii) writes it must be in
            # the Rust body: isascii() alone cannot see DEL or escape style.
            want = json.dumps(n)
            if want in py["raw"] and want not in rs["raw"]:
                out.append(f"raw {path}: string not written as json.dumps writes it: {want[:80]}")
    walk(_raw_tree(py["raw"]), _raw_tree(rs["raw"]), normalized.get("json"), "")
    return out


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
    unreachable_database = False  # HTTP-only cases never initialize storage/models
    loopback_bind_fixture = False
    files: dict[str, str] = {}  # extra files in each data dir
    golden_replay = False
    golden_db_state = False

    def timeline(self, procs: dict, holders: list) -> list[dict]:
        """Scenarios whose answers depend on time (lease, backoff, reaper):
        return cases already answered (``_answers``), or [] to use cases()."""
        return []

    def prepare_template(self, dsn: str) -> None:
        """Seed the template bank through Python's own write paths."""

    def prepare_home(self, home: Path) -> dict[str, str]:
        """Optional per-arm files/environment, confined to the disposable home."""
        return {}

    def cleanup_home(self, home: Path) -> None:
        """Restore fixture permissions before a later pass removes the home."""

    def cases(self) -> list[dict]:
        return []

    def normalize_db(self, state: dict, before: dict, side: str) -> dict:
        return dbstate.normalize(state, DB_NONDETERMINISTIC, before)

    def configure_daemons(self, procs: dict) -> None:
        """Optional explicit dependency seams, declared by the scenario."""



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
            case("board check-in: no bearer", "GET", "/api/hook/coordination-start"),
            case("board check-in: default principal (allowed)", "GET", "/api/hook/coordination-start", [auth]),
            case("board check-in: env principal not allowed", "GET", "/api/hook/coordination-start", [bearer(T_ALICE)]),
            case("board check-in: stored principal with board", "GET", "/api/hook/coordination-start",
                 [bearer("stored-carol-token-w1a")]),
            case("board check-in: revoked stored principal", "GET", "/api/hook/coordination-start",
                 [bearer("stored-dave-token-w1a")]),
            case("coordination hub (authenticated)", "POST", "/api/coordination/receive",
                 [auth, ("Content-Type", "application/json")], b"{}", declared="W2-F: board hub"),
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
            case("tokenless coordination hub", "POST", "/api/coordination/receive",
                 [("Content-Type", "application/json")], b"{}"),
            case("tokenless coordination hub, no body", "POST", "/api/coordination/agents"),
            case("tokenless board check-in", "GET", "/api/hook/coordination-start"),
            case("DEL in a query echoed back", "GET", "/api/search?q=%7F"),
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
            case("board check-in with the board disabled", "GET", "/api/hook/coordination-start", [auth]),
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
    unreachable_database = True

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

        out = self.partial = []
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
        out = self.partial = [c]
        release(holders)
        wait_settled([d.port for d in procs.values()], timeout=240, token=self.env.get("PSEUDOLIFE_MCP_TOKEN"))
        c = case("health after reaper retry", "GET", "/health")
        c["_answers"] = (call(procs["python"].port, "GET", "/health"), call(procs["rust"].port, "GET", "/health"))
        out.append(c)
        return out


def assert_disposable(conn) -> None:
    """The server's own name for this database must be a disposable one
    before the harness alters or deletes anything in it."""
    from pseudolife_memory.storage.schema import assert_disposable_database
    name = assert_disposable_database(conn)
    if not pg.DISPOSABLE_NAME.fullmatch(name):
        raise RuntimeError(f"refusing to modify non-disposable database {name!r}")


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
            assert_disposable(conn)
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("CREATE TABLE entries (id BIGSERIAL PRIMARY KEY, embedding vector(384))")

    def cases(self):
        return [case("health, dim refusal", "GET", "/health"),
                case("search during refusal", "GET", "/api/search?q=x")]


class StampedBank(Scenario):
    """Future schema: Python downgrades; Rust deliberately refuses unchanged."""
    name = "stamped"
    settle = False

    def prepare_template(self, dsn: str) -> None:
        import psycopg
        sys.path.insert(0, str(REPO))
        from pseudolife_memory.storage.postgres import PostgresStorage
        PostgresStorage(dsn).close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            assert_disposable(conn)
            conn.execute("UPDATE meta SET value = '99'::jsonb WHERE key = 'schema_version'")
            conn.execute("UPDATE meta SET value = '\"x\"'::jsonb WHERE key = 'writer_lease_epoch'")
            conn.execute("DELETE FROM relations WHERE name = 'uses'")

    def cases(self):
        c = case("health on stamped bank", "GET", "/health")
        c["future_schema_refusal"] = True
        return [c]

    def timeline(self, procs, holders):
        if "python" in procs:
            wait_settled([procs["python"].port])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            answer = call(procs["rust"].port, "GET", "/health")
            if answer.get("json", {}).get("init_refusal"):
                return self.cases()
            time.sleep(0.1)
        raise RuntimeError("future-schema Rust startup did not refuse")


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
        # Number spellings Python never writes but a bank can hold (another
        # writer, a migration): jsonb keeps 1.10's scale and 1E2 as 100.
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            assert_disposable(conn)
            conn.execute(
                "UPDATE entries SET slots = '[[\"daemon\", \"port\", 1.10, true], "
                "[\"daemon\", \"ratio\", 2.50, true], [\"daemon\", \"hundred\", 1E2, true], "
                "[\"daemon\", \"tiny\", 1e-7, true], [\"daemon\", \"huge\", 12345678901234567890, true], "
                "[\"caf\u00e9\", \"note\", \"d\u00e9ploy\", true]]'::jsonb "
                "WHERE text LIKE 'Deploy only via%'")

    def cases(self):
        auth = bearer(T_DEFAULT)
        s = "/api/search?"
        q = lambda **kw: s + urllib.parse.urlencode(kw)  # noqa: E731
        return [case(f"seeded search {i}", "GET", q(q=text, top_k=5, disable_recency_boost="true"), [auth])
                for i, text in enumerate(["how do I deploy the daemon", "what is a contender",
                                          "bearer tokens for remote clients", "schema version bump"])]


CONFIG_PROFILES = {
    "no file": None,
    "empty": "",
    "comments only": "# nothing\n",
    "top-level list": "- a\n- b\n",
    "memory knobs": "memory:\n  top_k: 3\n  hide_superseded: true\n  search_confidence_floor: 0.2\n  recency_boost_enabled: true\n",
    "half-life set": "memory:\n  recency_base_half_life_s: 120.5\n",
    # "recency_base_half_life_s: {}" keeps a dict in Python and refuses here:
    # a declared wrong-type divergence (divergences.md), not a profile.
    "continuum": "memory:\n  miras:\n    preset: continuum\n",
    "alias memora": "memory:\n  miras:\n    preset: memora\n",
    "custom bands": "memory:\n  miras:\n    preset: custom\n    bands:\n      - name: hot\n      - {max_entries: 3}\n",
    "search block": "memory:\n  search:\n    min_score: 0\n    fusion: rrf\n    candidate_pool_multiplier: 2\n    contiguity_neighbors: 1\n    timeline_channel: true\n",
    "bm25 block": "memory:\n  bm25:\n    enabled: false\n    k1: 1.2\n    b: 0.5\n    weight: 0.4\n    top_n: 7\n    min_score: 0.2\n",
    "reranker and log": "memory:\n  reranker:\n    enabled: true\n  retrieval_log:\n    enabled: false\n",
    "embedding": "embedding:\n  model_name: m\n  device: cpu\n  query_prefix: ''\n  max_seq_length: 256\n",
    "coordination": "coordination:\n  enabled: false\n  allowed_principals: [' Alice ', bob]\n  wake:\n    nightly_total: 9\n",
    "updates": "updates:\n  check_releases: false\n  unattended_clients: true\n  check_interval_seconds: 600\n",
    "dream from config": "memory:\n  dream:\n    enabled: true\n    extractor_source: config\n    extractor_base_url: http://x\n    extractor_model: m\n    extractor_model_override: o\n",
    "duplicate keys": "memory:\n  top_k: 3\n  top_k: 5\n",
    "anchors and merge": "base: &b {top_k: 4}\nmemory:\n  <<: *b\n  hide_superseded: true\n",
}


def python_config_dump(home: Path, config_yaml: str | None) -> dict:
    """The same keys as the Rust dump, read from Python's AppConfig after
    the MCP overlay (MemoryService construction does both, no model load)."""
    import subprocess
    code = r'''
import json, sys
from pseudolife_memory.service import MemoryService
c = MemoryService(data_dir=sys.argv[1]).config
m, s, b, e, co, u, d = c.memory, c.memory.search, c.memory.bm25, c.embedding, c.coordination, c.updates, c.memory.dream
from dataclasses import asdict
print(json.dumps({
 "memory.top_k": m.top_k, "memory.hide_superseded": m.hide_superseded,
 "memory.search_confidence_floor": m.search_confidence_floor, "memory.recency_boost_enabled": m.recency_boost_enabled,
 "memory.recency_base_half_life_s": m.recency_base_half_life_s, "memory.miras.preset": m.miras.preset,
 "memory.miras.bands": [x.name for x in m.miras.bands],
 "memory.search.min_score": s.min_score, "memory.search.fusion": s.fusion,
 "memory.search.candidate_pool_multiplier": s.candidate_pool_multiplier,
 "memory.search.contiguity_neighbors": s.contiguity_neighbors, "memory.search.timeline_channel": s.timeline_channel,
 "memory.bm25.enabled": b.enabled, "memory.bm25.k1": b.k1, "memory.bm25.b": b.b, "memory.bm25.weight": b.weight,
 "memory.bm25.top_n": b.top_n, "memory.bm25.min_score": b.min_score,
 "memory.reranker.enabled": c.memory.reranker.enabled, "memory.retrieval_log.enabled": c.memory.retrieval_log.enabled,
 "memory.retrieval_log.retention_days": float(c.memory.retrieval_log.retention_days),
 "memory.compaction.enabled": c.memory.compaction.enabled,
 "memory.compaction.keep_per_slot": c.memory.compaction.keep_per_slot,
 "memory.compaction.min_age_days": float(c.memory.compaction.min_age_days),
 "embedding.model_name": e.model_name, "embedding.device": e.device, "embedding.query_prefix": e.query_prefix,
 "embedding.max_seq_length": e.max_seq_length,
 "embedding.backend": e.backend,
 "embedding.onnx_file_name": e.onnx_file_name,
 "embedding.batch_size": e.batch_size,
 "embedding.cache_size": e.cache_size,
 "embedding.cpu_dtype": e.cpu_dtype,
 "coordination.enabled": co.enabled, "coordination.wake": asdict(co.wake), "coordination.allowed_principals": co.allowed_principals,
 "updates.check_releases": u.check_releases, "updates.unattended_clients": u.unattended_clients,
 "updates.unattended_daemon": u.unattended_daemon, "updates.check_interval_seconds": u.check_interval_seconds,
 "memory.dream.enabled": d.enabled, "memory.dream.extractor_source": d.extractor_source,
 "memory.dream.extractor_base_url": d.extractor_base_url, "memory.dream.extractor_model": d.extractor_model,
 "memory.dream.fallback_base_url": d.fallback_base_url, "memory.dream.fallback_model": d.fallback_model,
 "memory.dream.extractor_model_override": d.extractor_model_override}))
'''
    env = daemons.base_env(home, {})
    r = subprocess.run([sys.executable, "-c", code, str(home / "data")], env=dict(env, PYTHONPATH=str(REPO)),
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return {"<exit>": r.returncode}
    return json.loads(r.stdout.strip().splitlines()[-1])


def run_config_differential(binary: Path, root: Path) -> list[dict]:
    import subprocess
    rows = []
    for name, text in CONFIG_PROFILES.items():
        home = daemons.make_home(root, "config-diff", text)
        py = python_config_dump(home, text)
        env = daemons.base_env(home, {"PSEUDOLIFE_MCP_DATABASE_URL": f"postgresql://x@127.0.0.1:9/{pg.PREFIX}none",
                                      "PSEUDOLIFE_DAEMON_DUMP_CONFIG": "1"})
        r = subprocess.run([str(binary)], env=env, cwd=home, capture_output=True, text=True, timeout=60)
        rs = json.loads(r.stdout) if r.returncode == 0 else {"<exit>": r.returncode}
        # Python lower-cases and strips but keeps duplicates out; order is the list's.
        if isinstance(py.get("coordination.allowed_principals"), list):
            py["coordination.allowed_principals"] = sorted(set(py["coordination.allowed_principals"]))
        if isinstance(rs.get("coordination.allowed_principals"), list):
            rs["coordination.allowed_principals"] = sorted(set(rs["coordination.allowed_principals"]))
        rows.append({"case": f"config: {name}", "diffs": diff_values(py, rs)})
    return rows


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
            assert_disposable(conn)
            conn.execute("ALTER TABLE entries ALTER COLUMN embedding DROP NOT NULL")
            conn.execute("INSERT INTO entries (band, text, embedding, ts, source) "
                         "VALUES ('flat', 'no vector', NULL, 1.0, 'agent')")

    def timeline(self, procs, holders):
        time.sleep(90)  # both have loaded their embedders and failed hydration
        c = case("health after a NULL-vector hydration failure", "GET", "/health")
        c["_answers"] = (call(procs["python"].port, "GET", "/health"), call(procs["rust"].port, "GET", "/health"))
        self.partial = [c]
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
            assert_disposable(conn)
            conn.execute("ALTER TABLE entries ALTER COLUMN embedding TYPE vector")
            conn.execute("INSERT INTO entries (band, text, embedding, ts, source) VALUES "
                         "('flat', 'old model', ('[' || array_to_string(array_fill(0.1::real, ARRAY[384]), ',') || ']')::vector, 1.0, 'agent')")

    def timeline(self, procs, holders):
        time.sleep(90)
        c = case("health after a stale-dimension refusal", "GET", "/health")
        c["_answers"] = (call(procs["python"].port, "GET", "/health"), call(procs["rust"].port, "GET", "/health"))
        self.partial = [c]
        return [c]


class DbLost(Scenario):
    """Storage exists, then the database refuses new connections: /health
    pings fail (degraded, 503); allowed again, both recover."""
    name = "db-lost"

    def __init__(self):
        self.env = {"PSEUDOLIFE_MCP_TOKEN": T_DEFAULT}

    def timeline(self, procs, holders):
        import psycopg
        out = self.partial = []
        dbs = [f"{pg.PREFIX}db_lost_{side}" for side in ("py", "rs")]
        with psycopg.connect(pg.dsn(f"{pg.PREFIX}db_lost_t"), autocommit=True) as conn:
            assert_disposable(conn)
            for db in dbs:
                if not pg.DISPOSABLE_NAME.fullmatch(db):
                    raise RuntimeError(f"refusing to fence non-disposable database {db!r}")
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


class Encodings(Scenario):
    """Token precedence across a bearer's two candidate encodings
    (principals.py:290-300), made visible by the board check-in: the map
    principal alice is admitted, the singular token's default is not."""
    name = "encodings"
    config_yaml = "coordination:\n  allowed_principals: [alice]\n"
    settle = False

    def __init__(self):
        # The singular token is UTF-8 "caf\u00e9" read as latin-1; the map's is "caf\u00e9".
        self.env = {"PSEUDOLIFE_MCP_TOKEN": "caf\u00c3\u00a9", "PSEUDOLIFE_MCP_TOKENS": "caf\u00e9:alice"}

    def cases(self):
        hook = "/api/hook/coordination-start"
        return [
            case("UTF-8 bearer: the map wins over the singular token", "GET", hook,
                 [("Authorization", b"Bearer caf\xc3\xa9")]),
            case("latin-1 bearer matching the map", "GET", hook, [("Authorization", b"Bearer caf\xe9")]),
            case("non-ASCII bearer matching nothing", "GET", hook, [("Authorization", b"Bearer \xff\xfe")]),
        ]


class MapOrder(Scenario):
    """Two map tokens, each matching one of a bearer's two candidate
    encodings: the earlier map entry wins whichever candidate it matched
    (principals.py:290-293). alice first here; MapOrderReversed swaps them."""
    name = "map-order"
    config_yaml = "coordination:\n  allowed_principals: [alice]\n"
    settle = False
    tokens = "caf\u00e9:alice,caf\u00c3\u00a9:bob"

    def __init__(self):
        self.env = {"PSEUDOLIFE_MCP_TOKENS": self.tokens}

    def cases(self):
        return [case("UTF-8 bearer matching both entries", "GET", "/api/hook/coordination-start",
                     [("Authorization", b"Bearer caf\xc3\xa9")])]


class MapOrderReversed(MapOrder):
    """The control: the same two entries in the other order, so bob wins and
    the board refuses him (alice is the one allowed)."""
    name = "map-order-reversed"
    tokens = "caf\u00c3\u00a9:bob,caf\u00e9:alice"


class TrustBind(Scenario):
    """Tokenless on 0.0.0.0, allowed by PSEUDOLIFE_MCP_TRUST_BIND."""
    name = "trust-bind"
    loopback_bind_fixture = True

    def __init__(self):
        self.env = {"PSEUDOLIFE_MCP_HOST": "0.0.0.0", "PSEUDOLIFE_MCP_TRUST_BIND": "On"}

    def cases(self):
        return [case("health on a trusted wildcard bind", "GET", "/health"),
                case("browser gate still applies", "GET", "/api/nope", [("Host", "evil.example")])]


class StaticBuild(Scenario):
    """Every file in the committed Console build, with no storage/model dependency."""
    name = "static-build"
    settle = False
    unreachable_database = True
    env = {"PSEUDOLIFE_MCP_TOKEN": T_DEFAULT}

    def cases(self):
        root = REPO / "pseudolife_memory" / "web" / "static"
        assert not (root / "next").exists(), "the oracle has no /ui/next build"
        c = [case("root " + method, method, "/") for method in ("GET", "POST", "PUT")]
        c += [case("static " + path, "GET", path) for path in
              ("/ui", "/ui/", "/ui/assets/", "/ui/next/", "/ui/graph/view",
               "/ui/%2e%2e/pyproject.toml", "/ui/../pyproject.toml", "/ui/a%00b")]
        c += [case("built " + p.relative_to(root).as_posix(), "GET",
                   "/ui/" + urllib.parse.quote(p.relative_to(root).as_posix()))
              for p in sorted(root.rglob("*")) if p.is_file()]
        c += [case("static " + method, method, "/ui/theme.js") for method in ("POST", "HEAD")]
        return c


class StaticPaths(StaticBuild):
    name = "static-paths"

    def prepare_home(self, home):
        root = home / "static"
        (root / "sub").mkdir(parents=True)
        for name, body in {"index.html": b"shell", "notice.txt": b"notice",
                           "sub/index.html": b"directory", "image.svg": b"<svg/>",
                           "data.json": b'{"a": 1}', "unknown.pl_http_unknown": b"opaque"}.items():
            (root / name).write_bytes(body)
        outside = home / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"outside")
        sibling = home / "static-x"
        sibling.mkdir()
        (sibling / "secret.txt").write_bytes(b"sibling")
        # Junctions are available without Windows symlink privileges. File
        # symlinks are exercised on Linux; both arms get the same fixtures.
        if os.name == "nt":
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "escape"), str(outside)],
                           check=True, capture_output=True)
        else:
            (root / "escape").symlink_to(outside, target_is_directory=True)
            (root / "notice.js").symlink_to(root / "notice.txt")
            (root / "loop-a").symlink_to("loop-b")
            (root / "loop-b").symlink_to("loop-a")
            (root / "unreadable.txt").write_bytes(b"unreadable")
            (root / "unreadable.txt").chmod(0)
            (root / "linked-index").mkdir()
            (root / "linked-index" / "index.html").symlink_to(outside / "secret.txt")
            (root / "unsearchable").mkdir()
            (root / "unsearchable" / "child.txt").write_bytes(b"hidden")
            (root / "unsearchable").chmod(0)
        return {"PL_HARNESS_STATIC_DIR": str(root), "PSEUDOLIFE_DAEMON_STATIC_DIR": str(root)}

    def cases(self):
        paths = ["/ui/", "/ui/sub", "/ui/sub/", "/ui/missing", "/ui/image.svg",
                 "/ui/data.json", "/ui/unknown.pl_http_unknown", "/ui/../outside/secret.txt",
                 "/ui/%2e%2e/outside/secret.txt", "/ui/escape/secret.txt",
                 "/ui/escape/missing", "/ui/escape/../static/index.html",
                 "/ui/escape/../index.html"]
        paths += ["/ui/../static-x/secret.txt", "/ui/%2e%2e%2fstatic-x%2fsecret.txt",
                  "/ui/%2e%2e%5cstatic-x%5csecret.txt", "/ui/%2f__pl_http_outside%2ffile",
                  "/ui/%5c__pl_http_outside%5cfile", "/ui/notice.txt.", "/ui/notice.txt%20",
                  "/ui/C:index.html", "/ui/C:%5c__pl_http_outside%5cfile",
                  "/ui/%5c%5c127.0.0.1%5cpl_http_missing_share%5cfile",
                  "/ui/%5c%5c.%5cpipe%5cpl_http_missing_pipe"]
        paths += ["/ui/a*b", "/ui/a%7Cb", "/ui/%3C", "/ui/..%20/outside/secret.txt",
                  "/ui/..%20/static/index.html", "/ui/sub/..%20/notice.txt"]
        odd_segments = (".. .", ".. ..", "... ", ".... ", ". .")
        paths += ["/ui/" + urllib.parse.quote(p) + "/notice.txt" for p in odd_segments]
        if os.name != "nt":
            paths += ["/ui/notice.js", "/ui/loop-a", "/ui/unreadable.txt",
                      "/ui/linked-index", "/ui/linked-index/index.html"]
            paths.append("/ui/unsearchable/child.txt")
        out = [case("path " + p, "GET", p) for p in paths]
        if os.name == "nt":
            for c in out:
                if c["path"] == "/ui/C:index.html":
                    c["refusal_policy"] = "lexical-outside-root"
                if "..%20/" in c["path"]:
                    c["refusal_policy"] = "parent-space-refusal"
                if any("/" + urllib.parse.quote(p) + "/" in c["path"] for p in odd_segments):
                    c["refusal_policy"] = "parent-space-refusal"
        else:
            for c in out:
                if c["path"] == "/ui/linked-index":
                    c["refusal_policy"] = "directory-index-containment"
        return out

    def cleanup_home(self, home):
        if os.name != "nt":
            blocked = home / "static" / "unsearchable"
            if blocked.exists():
                blocked.chmod(0o700)


class StaticMissing(StaticPaths):
    name = "static-missing"

    def prepare_home(self, home):
        env = super().prepare_home(home)
        (home / "static" / "index.html").unlink()
        return env

    def cases(self):
        return [case("no index " + p, "GET", p) for p in ("/ui", "/ui/", "/ui/missing", "/ui/sub")]


class StaticRootLink(StaticPaths):
    name = "static-root-link"

    def prepare_home(self, home):
        super().prepare_home(home)
        (home / "serve").mkdir()
        alias = home / "serve" / "console"
        if os.name == "nt":
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(home / "static")],
                           check=True, capture_output=True)
        else:
            alias.symlink_to(home / "static", target_is_directory=True)
        return {"PL_HARNESS_STATIC_DIR": str(alias), "PSEUDOLIFE_DAEMON_STATIC_DIR": str(alias)}

    def cases(self):
        out = [case("linked root " + p, "GET", p) for p in
                ("/ui", "/ui/notice.txt", "/ui/missing", "/ui/escape/missing",
                 "/ui/../static/index.html", "/ui/%2e%2e/static/index.html")]
        if os.name != "nt":
            for c in out:
                if "/../" in c["path"] or "%2e%2e" in c["path"]:
                    c["refusal_policy"] = "lexical-outside-root"
        return out


SCENARIOS = {s.name: s for s in (Tokens, Tokenless, PairBudget, CustomConfig, ExtractorConfigured,
                                  DbDown, LeaseHeld, Reaper, DimMismatch, StampedBank, SeededBank,
                                  TrustBind, NullEmbedding, UnconstrainedDims, DbLost, Encodings,
                                  MapOrder, MapOrderReversed, StaticBuild, StaticPaths, StaticMissing,
                                  StaticRootLink)}

from background_sessions import register as register_background_sessions
SCENARIOS.update(register_background_sessions(sys.modules[__name__]))
from background_maintenance import register as register_background_maintenance
SCENARIOS.update(register_background_maintenance(sys.modules[__name__]))


# ---- running ---------------------------------------------------------------------------

def rust_env(extra: dict[str, str]) -> dict[str, str]:
    out = dict(extra)
    for key in ("ORT_DYLIB_PATH", "PSEUDOLIFE_DAEMON_ONNX_DIR", "PSEUDOLIFE_DAEMON_MUTANT",
                "PSEUDOLIFE_DAEMON_ORT_THREADS"):
        if os.environ.get(key):
            out[key] = os.environ[key]
    out.setdefault("PSEUDOLIFE_DAEMON_STATIC_DIR", str(REPO / "pseudolife_memory" / "web" / "static"))
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
    if c.get("refusal_policy") in {"lexical-outside-root", "directory-index-containment", "parent-space-refusal"}:
        sys.path.insert(0, str(REPO))
        from pseudolife_memory.web.api import CONSOLE_SECURITY_HEADERS
        headers = {k.decode(): v.decode() for k, v in CONSOLE_SECURITY_HEADERS}
        headers.update({"content-type": "text/plain", "cache-control": "no-store", "content-length": "9"})
        want = {"status": 403, "headers": headers, "bytes": "forbidden"}
        row["substitution"] = c["refusal_policy"]
        row["diffs"] = diff_values(want, rs)
        return row
    if c.get("future_schema_refusal"):
        from pseudolife_memory.storage.schema import SCHEMA_META_VERSION
        row["declared"] = "future-schema refusal: Python downgrades; Rust preserves the bank"
        body = rs.get("json", {})
        message = body.get("init_refusal", "")
        if rs["status"] != 503 or body.get("status") != "degraded" \
                or "99" not in message or str(SCHEMA_META_VERSION) not in message \
                or "newer than" not in message or "db" in body:
            row["diffs"].append("Rust must refuse future schema 99 before opening storage")
        if py is not None and (py["status"] != 200 or py.get("json", {}).get("db") != "ok"):
            row["diffs"].append("Python oracle must retain its recorded downgrade behavior")
        for header, expected in [("content-type", "application/json; charset=utf-8"),
                                 ("cache-control", "no-store"), ("x-content-type-options", "nosniff")]:
            if rs["headers"].get(header) != expected:
                row["diffs"].append(f"future-schema refusal has incorrect {header}")
        return row
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
    a.pop("raw", None)
    b.pop("raw", None)
    row["diffs"] = diff_values(a, b)
    if not row["diffs"]:
        row["diffs"] = raw_diffs(py, rs, a)
    row["declared_omissions"] = declared
    if row["diffs"]:
        row["raw"] = {"python": py, "rust": rs}
    return row


def run_scenario(scn: Scenario, binary: Path, root: Path, mode: str, record: bool) -> dict:
    if scn.loopback_bind_fixture:
        import subprocess
        probe = subprocess.run([str(binary)], capture_output=True, timeout=10,
                               env=daemons.base_env(root, {"PSEUDOLIFE_DAEMON_HARNESS_CAPABILITIES": "1"}))
        if probe.returncode != 0 or probe.stdout.strip() != b"true":
            raise RuntimeError("trust-bind requires a debug --features mutants binary: refuse before any wildcard listener")
    tag = scn.name.replace("-", "_")
    try:
        return _run_scenario(scn, binary, root, mode, record)
    finally:
        # Only this scenario's three exact names, including seed failure.
        for suffix in ("t", "py", "rs"):
            pg.drop(f"{pg.PREFIX}{tag}_{suffix}")


def _run_scenario(scn: Scenario, binary: Path, root: Path, mode: str, record: bool) -> dict:
    tag = scn.name.replace("-", "_")
    template = f"{pg.PREFIX}{tag}_t"
    dsn_t = pg.create(template)
    scn.prepare_template(dsn_t)
    # The banks' state before either daemon touched them: declared writes and
    # clock values are judged against it, so changes to existing rows show.
    before = dbstate.dump(dsn_t)
    dbs = {"python": f"{pg.PREFIX}{tag}_py", "rust": f"{pg.PREFIX}{tag}_rs"}
    dsns = {k: pg.create(v, template=template) for k, v in dbs.items()}
    if scn.unreachable_database:
        dsns = {k: f"postgresql://nobody:nothing@127.0.0.1:{daemons.free_port()}/{pg.PREFIX}down" for k in dsns}
    procs = {}
    holders = take_leases(list(dsns.values())) if scn.hold_lease else []
    try:
        if mode in ("live", "record"):
            home = daemons.make_home(root, f"{tag}-py", scn.config_yaml)
            for name, text in scn.files.items():
                (home / "data" / name).write_text(text, encoding="utf-8")
            env = common_env(scn, dsns["python"])
            env.update(scn.prepare_home(home))
            if scn.loopback_bind_fixture:
                env["PL_HARNESS_LOOPBACK_BIND"] = "1"
            procs["python"] = daemons.python_daemon(home, daemons.free_port(), daemons.base_env(home, env))
        home = daemons.make_home(root, f"{tag}-rs", scn.config_yaml)
        for name, text in scn.files.items():
            (home / "data" / name).write_text(text, encoding="utf-8")
        env = common_env(scn, dsns["rust"])
        env.update(scn.prepare_home(home))
        if scn.loopback_bind_fixture:
            env["PSEUDOLIFE_DAEMON_TEST_LOOPBACK_BIND"] = "1"
        procs["rust"] = daemons.rust_daemon(binary, home, daemons.free_port(),
                                            daemons.base_env(home, rust_env(env)))
        scn.configure_daemons(procs)
        for d in procs.values():
            d.start(240)
        if scn.settle:
            wait_settled([d.port for d in procs.values()], token=scn.env.get("PSEUDOLIFE_MCP_TOKEN"))
        rows = []
        golden_name = scn.name
        if scn.name in {"static-build", "static-paths", "static-root-link"}:
            golden_name += "-windows" if os.name == "nt" else "-linux"
        golden = load_golden(golden_name) if mode == "golden" else None
        cases = scn.timeline(procs, holders) or scn.cases()
        for i, c in enumerate(cases):
            if c.get("_answers"):
                py_r, rs_r = c["_answers"]
                if py_r is None and mode == "golden":
                    py_r, rs_r = golden["responses"][i], golden_scrub(rs_r)
            else:
                # The empty root redirect is chunked by uvicorn, fixed-length
                # by hyper. Static entities use fixed lengths in both arms.
                compare_length = isinstance(scn, StaticBuild) and c["path"] != "/"
                py_r = call(procs["python"].port, c["method"], c["path"], c["headers"], c["body"],
                            compare_length=compare_length, body_bytes=isinstance(scn, StaticBuild)) if "python" in procs else golden["responses"][i]
                rs_r = call(procs["rust"].port, c["method"], c["path"], c["headers"], c["body"],
                            compare_length=compare_length, body_bytes=isinstance(scn, StaticBuild))
                if "python" not in procs:
                    rs_r = golden_scrub(rs_r)
                    if type(scn).prepare_template is not Scenario.prepare_template:
                        # A seeded template's entries carry the seeding moment.
                        py_r, rs_r = seed_clock_scrub(py_r), seed_clock_scrub(rs_r)
            rows.append(compare_case(c, py_r, rs_r))
            if record:
                rows[-1]["_python"] = normalize_response(py_r, c["path"], [])
    finally:
        release(holders)
        for d in procs.values():
            d.stop()
            scn.cleanup_home(d.cwd)
    states, scrubbed = {}, {}
    unchanged_diffs = []
    if scn.name != "db-down":
        sides = dbs if mode != "golden" else {"rust": dbs["rust"]}
        for side, name in sides.items():
            raw = dbstate.dump(pg.dsn(name))
            out = scrub_declared_rows(raw, before)
            states[side] = scn.normalize_db(out["state"], before, side)
            scrubbed[side] = out["declared"]
            if isinstance(scn, StaticBuild):
                # Static serving has no bank writes. Compare the complete
                # catalog/rows to this arm's pre-start state, rather than pin
                # the fixture server's template extensions into its golden.
                changes = dbstate.diff(before, raw)
                unchanged_diffs += [f"{side} changed bank: {d}" for d in changes]
                states[side] = {"unchanged_from_before": not changes}
    db_diffs = dbstate.diff(states["python"], states["rust"]) if len(states) == 2 else []
    if scn.name == "stamped":
        # This declared divergence has an independent postcondition: Rust
        # must leave every row/catalog entry identical to the prepared bank.
        db_diffs = dbstate.diff(dbstate.normalize(before, DB_NONDETERMINISTIC, before), states["rust"])
    db_diffs += unchanged_diffs
    # A template seeded at record time holds run-specific values (pairing
    # hashes, seeding timestamps) a replay cannot reproduce: golden mode
    # checks bank state only for scenarios whose bank starts empty.
    seeded_template = type(scn).prepare_template is not Scenario.prepare_template
    if mode == "golden" and golden.get("db_state") and (not seeded_template or scn.golden_db_state):
        db_diffs += dbstate.diff(golden["db_state"], golden_scrub(states["rust"]))
    result = {"scenario": scn.name, "cases": rows, "db_diffs": db_diffs, "declared_db_writes": scrubbed}
    if record:
        save_golden(golden_name, rows, states.get("python"))
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
                # A sequence absent before the run (a fresh bank) started unused.
                was = prior[table][5] if table in prior else None
                if row[1] == table and row[5] != was:
                    row[5] = was
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
            e = daemons.base_env(home, common_env(Scenario(), f"postgresql://nobody@127.0.0.1:9/{pg.PREFIX}none"))
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
    data = json.loads((GOLDENS / f"{name}.json").read_text(encoding="utf-8"))
    if name.startswith("static-build-"):
        for c, response in zip(StaticBuild().cases(), data["responses"], strict=True):
            platform_static_type(c, response)
    return data


def platform_static_type(c: dict, response: dict) -> None:
    """Replay the oracle's two platform MIME mappings, leaving bytes exact."""
    if response["status"] != 200 or not c["path"].endswith((".webp", ".md")):
        return
    sys.path.insert(0, str(REPO))
    from pseudolife_memory.web.api import mimetypes
    kind = mimetypes.guess_type(c["path"])[0] or "application/octet-stream"
    if kind.startswith("text/") and "charset" not in kind:
        kind += "; charset=utf-8"
    response["headers"]["content-type"] = kind
    response["headers"]["cache-control"] = ("max-age=86400" if kind.startswith(("font/", "image/"))
                                             else "no-store")


def _machine_paths() -> list[str]:
    """Paths of this machine that a response may echo (data dirs, homes),
    in every escaping a golden can hold them in."""
    out = []
    for path in {str(Path.home()), str(daemons.scratch_root()), str(REPO), tempfile.gettempdir()}:
        for form in {path, path.replace("\\", "/"), json.dumps(path)[1:-1], json.dumps(json.dumps(path)[1:-1])[1:-1]}:
            if len(form) > 3:
                out.append(form)
    return sorted(out, key=len, reverse=True)


def golden_scrub(value):
    """What a committed golden may hold: no raw bodies, no machine paths
    (refusal texts and /api/config echo the data dir), and vectors as a
    digest, so the file stays small and portable."""
    import hashlib
    paths = _machine_paths()

    def digest(v) -> str:
        return hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()[:24]

    def walk(v):
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if k == "raw":
                    continue
                if k == "bytes" and isinstance(x, str) and len(x) > 4096:
                    out[k] = f"<{len(x)} bytes sha256:{digest(x)}>"
                elif k == "catalog" and isinstance(x, dict) and all(isinstance(r, list) for r in x.values()):
                    out[k] = {name: {"rows": len(rows), "sha256": digest(walk(rows))} for name, rows in x.items()}
                else:
                    out[k] = walk(x)
            return out
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            if v.startswith("[") and v.count(",") >= 255 and len(v) > 2000:
                return "<vector sha256:" + hashlib.sha256(v.encode()).hexdigest()[:16] + ">"
            for path in paths:
                v = v.replace(path, "<machine path>")
            return v
        return v
    return walk(value)


def save_golden(name: str, rows: list[dict], state: dict | None) -> None:
    GOLDENS.mkdir(exist_ok=True)
    data = golden_scrub({"normalizers": {"db_nondeterministic": [list(k) + [v] for k, v in DB_NONDETERMINISTIC.items()],
                                         "db_declared": [list(r) for r in DB_DECLARED],
                                         "health": "normalize_health in run.py",
                                         "golden": "golden_scrub in run.py"},
                         "responses": [r.pop("_python") for r in rows], "db_state": state})
    text = json.dumps(data, indent=1, sort_keys=True) + "\n"
    leaked = [p for p in _machine_paths() if p in text]
    if leaked:
        raise RuntimeError(f"golden {name} would carry machine paths {leaked}; refusing to write it")
    (GOLDENS / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")


def summarize(results: list[dict], refusals: list[dict]) -> tuple[int, int, int]:
    cases = sum(len(r["cases"]) for r in results) + len(refusals)
    diff_cases = (sum(1 for r in results for c in r["cases"] if c["diffs"])
                  + sum(1 for r in refusals if r["diffs"]))
    db = sum(1 for r in results if r["db_diffs"])
    return cases, diff_cases, db


def main() -> int:
    if sys.argv[1:2] == ["schema"]:
        import schema_cases
        return schema_cases.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "embedding":
        import embedding
        return embedding.main(sys.argv[2:])
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live", "golden", "mutants"])
    ap.add_argument("--rust-bin", required=True, type=Path)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--no-refusals", action="store_true")
    ap.add_argument("--mutants", nargs="*", help="run only these mutants (default: all)")
    args = ap.parse_args()
    args.rust_bin = args.rust_bin.resolve()  # the daemon's cwd is its disposable home
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
            scn = SCENARIOS[n]()
            if mode == "golden" and type(scn).timeline is not Scenario.timeline and not scn.golden_replay:
                # Timing scenarios ask both daemons the same question at the
                # same moment: live-only (README).
                print(f"[{n}] live-only: skipped in golden mode", flush=True)
                continue
            try:
                r = run_scenario(scn, args.rust_bin, root, mode, args.record)
            except Exception as exc:  # a scenario that cannot complete is a failure, never a pass
                answered = [compare_case(c, *c["_answers"]) for c in getattr(scn, "partial", [])]
                r = {"scenario": n, "cases": answered + [{"case": "scenario error",
                                                          "diffs": [f"{type(exc).__name__}: {exc}"],
                                                          "declared": None}],
                     "db_diffs": [], "declared_db_writes": {}}
                for name in pg.existing():
                    if name.startswith(f"{pg.PREFIX}{n.replace('-', '_')}_"):
                        pg.drop(name)
            results.append(r)
            bad = [c["case"] for c in r["cases"] if c["diffs"]]
            print(f"[{n}] cases {len(r['cases'])} diffs {len(bad)} db_diffs {len(r['db_diffs'])} {bad[:6]}",
                  flush=True)
            for d in r["db_diffs"][:12]:
                print(f"  DB DIFF {d[:400]}", flush=True)
            if args.out:
                args.out.write_text(json.dumps({"partial": results}, indent=1), encoding="utf-8")
        refusals = (run_refusals(args.rust_bin, root) + run_config_differential(args.rust_bin, root)
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
        for m in (args.mutants or MUTANTS):
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


def rescrub_goldens() -> None:
    """Apply golden_scrub to committed goldens in place (no re-record)."""
    for path in sorted(GOLDENS.glob("*.json")):
        if path.name == "routes.json":
            continue
        data = golden_scrub(json.loads(path.read_text(encoding="utf-8")))
        data.setdefault("normalizers", {})["golden"] = "golden_scrub in run.py"
        text = json.dumps(data, indent=1, sort_keys=True) + "\n"
        assert not [p for p in _machine_paths() if p in text], path
        path.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
