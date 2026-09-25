"""The memory-policy bench's disposable daemon: seeder and recording server.

Two subcommands, both run by ``evals/memory_policy_bench.py`` as child
processes against a per-run database the bench created:

* ``seed`` — builds the template bank from ``memory_policy_scenarios``
  through the real service write paths, backdates it, and writes a manifest
  of the planted ids and of the embedder that embedded the bank.
* ``serve`` — the normal daemon (``pseudolife_memory.daemon.run_daemon``)
  with a recording layer around its ASGI app. The layer appends one JSON
  line per MCP ``tools/call`` (tool, arguments, result) and per
  ``/api/hook/*`` request (path, served text) to a ledger file.

Why a ledger: the bank itself does not persist ``memory_lesson_search``
calls or the ``used_ids`` an outcome claimed that nothing served, and the
grader must never fall back on the agent's own account. The ledger is the
daemon's record of what reached it, written by the daemon process.

Both subcommands refuse a production database or a live daemon port before
touching anything, independently of the orchestrator's own checks.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DAY = 86_400.0
BENCH_DB_PREFIX = "plbench_"
# The live daemon (8765 in the compose stack), the deployed Claude/Codex
# judge shims (8082, 8086), the extractor sidecar (8081), the GPU bench
# server (1234) and Postgres itself: never a disposable daemon's port.
FORBIDDEN_PORTS = frozenset({8765, 8086, 8082, 8081, 1234, 5432, 5433})
_MAX_RESULT_CHARS = 60_000


class BenchSafetyError(RuntimeError):
    """The bench was pointed at something it must never touch."""


def check_database(dsn: str) -> str:
    """The run database name, or raise: it must be a ``plbench_`` database
    and not a production bank (name check here, server check on connect)."""
    from pseudolife_memory.storage.schema import dsn_database_name, refuse_production_database
    name = dsn_database_name(dsn)
    if not name or not name.startswith(BENCH_DB_PREFIX):
        raise BenchSafetyError(f"refusing database {name!r}: bench databases start with "
                               f"{BENCH_DB_PREFIX!r}")
    refuse_production_database(name)
    return name


def check_port(port: int) -> int:
    if port in FORBIDDEN_PORTS:
        raise BenchSafetyError(f"refusing port {port}: it belongs to a live service")
    return port


def _server_check(dsn: str) -> None:
    import psycopg

    from pseudolife_memory.storage.schema import assert_disposable_database
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        name = assert_disposable_database(conn)
        if not name.startswith(BENCH_DB_PREFIX):
            raise BenchSafetyError(f"server reports database {name!r}, not a bench database")


# ── seed ────────────────────────────────────────────────────────────────────

def _seed_store(svc, entry, episode) -> None:
    out = svc.store(entry.text, source=entry.source, origin="agent", episode=episode)
    if not out.get("stored"):
        raise RuntimeError(f"seed entry {entry.key!r} was not stored: {out}")


def seed(manifest_path: Path) -> dict:
    """Write the fixture bank into the database PSEUDOLIFE_MCP_DATABASE_URL
    names (a fresh ``plbench_`` template), then backdate it."""
    import psycopg

    import evals.embedder_stamp as embedder_stamp
    from evals import memory_policy_scenarios as fx
    from pseudolife_memory.service import MemoryService

    dsn = os.environ["PSEUDOLIFE_MCP_DATABASE_URL"]
    check_database(dsn)
    _server_check(dsn)
    now = time.time()
    svc = MemoryService(data_dir=os.environ["PSEUDOLIFE_MCP_DATA_DIR"], database_url=dsn)
    svc._ensure_init()
    manifest: dict = {"entries": {}, "lessons": {}, "facts": [], "sessions": {},
                      "seeded_at": now}
    # Session-less entries first: while an episode is open, every store
    # without a handle lands in it.
    for e in fx.SEED_ENTRIES:
        if e.session is None:
            _seed_store(svc, e, None)
    episodes = {}
    for prior in fx.PRIOR_SESSIONS:
        ep = svc.episode_start_session(prior.key, prior.title)
        episodes[prior.key] = ep["id"]
        manifest["sessions"][prior.key] = {"episode_id": ep["id"], "title": prior.title}
        for e in fx.SEED_ENTRIES:
            if e.session == prior.key:
                _seed_store(svc, e, ep["id"])
        svc.episode_end_session(prior.key, run_dream=False)
    for f in fx.SEED_FACTS:
        asserted = now - f.age_days * DAY
        out = svc.cortex_write(f.entity, f.attribute, f.value, confidence=0.8, now=asserted)
        manifest["facts"].append({"entity": f.entity, "attribute": f.attribute,
                                  "value": f.value, "action": out.get("action")})
        if f.contender:
            out = svc.cortex_write(f.entity, f.attribute, f.contender, confidence=0.8,
                                   now=asserted + DAY, force_contend=True)
            manifest["facts"].append({"entity": f.entity, "attribute": f.attribute,
                                      "value": f.contender, "action": out.get("action")})
    for lesson in fx.SEED_LESSONS:
        out = svc.lesson_write(lesson.task, lesson.aspect, lesson.lesson, about=lesson.about,
                               outcome=lesson.outcome, polarity=lesson.polarity,
                               confidence=lesson.confidence, origin="agent")
        manifest["lessons"][lesson.key] = {"task": lesson.task, "aspect": lesson.aspect,
                                           "action": out.get("action")}
    for w in fx.SEED_WORLD:
        svc.world_write(w.entity, w.attribute, w.value, confidence=0.9,
                        source_url=w.source_url, source_quote=w.source_quote,
                        freshness_class="slow")
    svc.flush()
    manifest[embedder_stamp.KEY] = embedder_stamp.describe(svc)
    try:
        svc._storage.close()
    except Exception:  # noqa: BLE001
        pass
    # Planted ids by exact text (store() returns none), then backdate:
    # entries and the prior session carry the ages the fixtures name.
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        for e in fx.SEED_ENTRIES:
            rows = conn.execute("SELECT id, episode_id FROM entries WHERE text = %s",
                                (e.text,)).fetchall()
            if len(rows) != 1:
                raise RuntimeError(f"seed entry {e.key!r}: {len(rows)} rows")
            got = rows[0][1]
            if (got != episodes[e.session]) if e.session else (got in episodes.values()):
                raise RuntimeError(f"seed entry {e.key!r} landed in episode {got!r}")
            manifest["entries"][e.key] = int(rows[0][0])
            conn.execute("UPDATE entries SET ts = %s WHERE id = %s",
                         (now - e.age_days * DAY, rows[0][0]))
        for prior in fx.PRIOR_SESSIONS:
            start = now - prior.age_days * DAY
            conn.execute("UPDATE episodes SET started_at = %s, ended_at = %s WHERE id = %s",
                         (start, start + 3_600.0, episodes[prior.key]))
        conn.commit()
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


# ── serve ───────────────────────────────────────────────────────────────────

class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _jsonrpc_messages(raw: bytes) -> list[dict]:
    """JSON-RPC messages from a streamable-HTTP body: plain JSON (one message
    or a batch) or an SSE stream of ``data:`` lines."""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return []
    try:
        value = json.loads(text)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        pass
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                continue
    return out


def _tool_result(result: dict) -> tuple[object, bool]:
    if not isinstance(result, dict):
        return result, False
    is_error = bool(result.get("isError"))
    if isinstance(result.get("structuredContent"), dict):
        value = result["structuredContent"]
        # FastMCP wraps non-dict returns as {"result": ...}.
        return value.get("result", value) if set(value) == {"result"} else value, is_error
    for part in result.get("content") or []:
        if part.get("type") == "text":
            try:
                return json.loads(part["text"]), is_error
            except (json.JSONDecodeError, TypeError):
                return str(part.get("text"))[:_MAX_RESULT_CHARS], is_error
    return None, is_error


def recording_app(app, ledger: Ledger):
    """Wrap the daemon's ASGI app; record tool calls and hook responses."""

    async def wrapped(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        watch = (path.startswith("/mcp") and method == "POST") or path.startswith("/api/hook/")
        if not watch:
            return await app(scope, receive, send)
        request = bytearray()
        response = bytearray()
        status = {"code": None}

        async def recv():
            message = await receive()
            if message.get("type") == "http.request":
                request.extend(message.get("body", b""))
            return message

        async def snd(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            elif message["type"] == "http.response.body":
                response.extend(message.get("body", b""))
            await send(message)

        started = time.time()
        try:
            await app(scope, recv, snd)
        finally:
            headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if path.startswith("/api/hook/"):
                ledger.write({"t": started, "kind": "hook", "path": path,
                              "query": scope.get("query_string", b"").decode(),
                              "status": status["code"],
                              "body": response.decode("utf-8", "replace")})
            else:
                calls = {m.get("id"): m for m in _jsonrpc_messages(bytes(request))
                         if isinstance(m, dict) and m.get("method") == "tools/call"}
                replies = {m.get("id"): m for m in _jsonrpc_messages(bytes(response))
                           if isinstance(m, dict) and "id" in m}
                for rid, call in calls.items():
                    params = call.get("params") or {}
                    reply = replies.get(rid) or {}
                    result, is_error = _tool_result(reply.get("result"))
                    if "error" in reply:
                        is_error, result = True, reply["error"]
                    text = json.dumps(result, default=str)
                    if len(text) > _MAX_RESULT_CHARS:
                        result = {"truncated": True, "head": text[:_MAX_RESULT_CHARS]}
                    ledger.write({"t": started, "t_end": time.time(), "kind": "tool",
                                  "name": params.get("name"),
                                  "arguments": params.get("arguments") or {},
                                  "result": result, "is_error": is_error,
                                  "session": headers.get("x-pl-session"),
                                  "status": status["code"]})

    return wrapped


def serve(ledger_path: Path) -> None:
    dsn = os.environ["PSEUDOLIFE_MCP_DATABASE_URL"]
    check_database(dsn)
    _server_check(dsn)
    check_port(int(os.environ["PSEUDOLIFE_MCP_PORT"]))
    if os.environ.get("PSEUDOLIFE_MCP_HOST", "127.0.0.1") != "127.0.0.1":
        raise BenchSafetyError("the bench daemon binds loopback only")
    ledger = Ledger(ledger_path)
    import pseudolife_memory.web as web
    original = web.build_console_app

    def build(mcp_app, *args, **kwargs):
        return recording_app(original(mcp_app, *args, **kwargs), ledger)

    web.build_console_app = build
    from pseudolife_memory.daemon import run_daemon
    run_daemon()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--manifest", type=Path, required=True)
    v = sub.add_parser("serve")
    v.add_argument("--ledger", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "seed":
        seed(args.manifest)
    else:
        serve(args.ledger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
