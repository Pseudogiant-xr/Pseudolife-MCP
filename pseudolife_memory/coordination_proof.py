"""Tagged coordination proof in a newly minted bank on an explicit fixture server.

No real host, model, saved client settings or production agents participate.
The real authenticated ASGI API, durable store and adapter produce the evidence.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import uuid


class FixtureCleanupError(RuntimeError):
    """The exact bank minted by this invocation still needs removal."""

    def __init__(self, name):
        self.name = name
        super().__init__("owned_fixture_cleanup_failed")

    def report(self):
        return {"ok": False, "error": type(self).__name__, "fixture_removed": False,
                "fixture_bank": self.name,
                "recovery": f'On the explicitly selected fixture server, run DROP DATABASE "{self.name}" WITH (FORCE); no host delivery has been verified.'}


@contextmanager
def _disposable_bank(dsn):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from pseudolife_memory.storage.schema import refuse_production_database

    options = conninfo_to_dict(dsn)
    if not options.get("dbname"):
        raise ValueError("explicit_fixture_database_required")
    refuse_production_database(options["dbname"])
    name = "pseudolife_memory_test_proof_" + uuid.uuid4().hex
    admin_dsn = make_conninfo(dsn, dbname="postgres", connect_timeout=5)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("SET statement_timeout = '5s'")
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(dsn, dbname=name, connect_timeout=5)
    finally:
        # Ownership begins only after CREATE succeeded. Cleanup reconnects
        # instead of depending on an admin backend left idle through the proof.
        for attempt in range(2):
            try:
                with psycopg.connect(admin_dsn, autocommit=True) as cleanup:
                    # Two constructor-failure proofs on 2026-10-01 each timed out both
                    # 5 s DROP attempts. A separate 30 s fixture-DDL budget leaves
                    # cleanup headroom while retaining bounded retries.
                    cleanup.execute("SET statement_timeout = '30s'")
                    cleanup.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                break
            except Exception:
                if attempt == 1:
                    raise FixtureCleanupError(name) from None


async def _proof(storage, digest_root, tag):
    import httpx
    from pseudolife_memory.coordination_adapter import CoordinationAdapter
    from pseudolife_memory.memory.hlc import HybridLogicalClock
    from pseudolife_memory.web.api import build_console_app
    from pseudolife_memory.web.fixtures import FixtureService

    async def no_mcp(scope, receive, send):
        await send({"type": "http.response.start", "status": 501, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    service = FixtureService(data_dir=digest_root / "console-state")
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["default"]
    service._storage, service._lock, service._hlc = storage, threading.Lock(), HybridLogicalClock()
    service._ensure_init = lambda: None
    app = build_console_app(no_mcp, "synthetic-proof-bearer", lambda: {}, service)
    hub = service._coordination_notifier.__self__
    stages = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as client:
            async with CoordinationAdapter("http://fixture", "synthetic-proof-bearer", client=client,
                                           label="proof-sender", project=tag) as sender:
                for path in ("hint", "ring"):
                    digest = digest_root / (uuid.uuid4().hex * 2 + ".txt")
                    async with CoordinationAdapter("http://fixture", "synthetic-proof-bearer", client=client,
                        digest_path=digest, label="proof-recipient", project=tag) as recipient:
                        if path == "ring":
                            # Synthetic listener evidence only: no CLI doorbell or host is launched.
                            recipient.ring_listener = lambda: True
                            await recipient._post("update", {"park_reason": "waiting_peer",
                                "park_needs": "fixture ready", "park_clear_by": "anyone"})
                        await recipient._heartbeat()
                        sent = await sender._post("send", {"to": recipient.instance_headers["X-PL-Agent"],
                            "text": "Synthetic fixture ready", "clears": "fixture ready", "request_id": tag + "-" + path})
                        expected = "rung" if path == "ring" else "hinted"
                        if sent.get("wake", {}).get("decision") != expected:
                            raise RuntimeError("fixture_wake_decision_missing")
                        await recipient._heartbeat()
                        signalled = recipient.ring_due() is not None if path == "ring" else bool(recipient.deliver_hint())
                        if not signalled:
                            raise RuntimeError("fixture_signal_missing")
                        received = await recipient._post("receive", {})
                        if [row["message_id"] for row in received.get("messages", [])] != [sent["message_id"]]:
                            raise RuntimeError("fixture_receive_mismatch")
                        await recipient._post("ack", {"message_id": sent["message_id"]})
                        if (await recipient._post("receive", {})).get("messages") != []:
                            raise RuntimeError("fixture_ack_missing")
                        stages.append({"path": path, "message_id": sent["message_id"], "enqueued": True,
                                       "wake_decision": expected, "signal_observed": True, "received": True,
                                       "harness_ack": True})
    finally:
        await asyncio.to_thread(hub.executor.shutdown, wait=True)
        mailbox = getattr(service, "_coordination_storage", None)
        if mailbox is not None:
            mailbox.close()
    return {"ok": True, "tag": tag, "instrument": "disposable_asgi_actual_adapter",
            "stages": stages, "host_delivery": "unverified", "model_ack": "unverified"}


def run_disposable_proof(dsn, *, timeout=20):
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.storage.schema import assert_disposable_database

    tag = "coordination-proof-" + uuid.uuid4().hex
    with _disposable_bank(dsn) as fixture_dsn:
        storage = PostgresStorage(fixture_dsn)
        try:
            assert_disposable_database(storage.conn)
            with tempfile.TemporaryDirectory(prefix="pseudolife-proof-") as root:
                result = asyncio.run(asyncio.wait_for(_proof(storage, Path(root), tag), timeout))
        finally:
            storage.close()
    result["fixture_removed"] = True
    return result
