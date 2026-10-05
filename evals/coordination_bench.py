"""Controlled local coordination overhead; no model, GPU, or live-bank access.

Uses the real adapter and ASGI coordination API with synthetic ordinary stats.
Channel timing ends at adapter event yield, not host receipt or model execution.
Run with ``python -m evals.coordination_bench --out <private-directory>``.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack, aclosing, contextmanager
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import threading
import tempfile
import time
import uuid

import httpx
import psycopg
from psycopg import sql


ARMS = ("disabled", "pull", "channel")
TRACE_FIELDS = frozenset({"arm", "kind", "action", "status", "elapsed_ms", "bytes", "sample"})
BENCH_ADMIN = "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres"


def _sdk_version():
    try:
        return importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        return None


def percentile(values, percent):
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * percent / 100) - 1)]


class Trace:
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.stream = self.path.open("x", encoding="utf-8")
        return self

    def write(self, **record):
        if set(record) - TRACE_FIELDS:
            raise ValueError("unapproved trace field")
        self.stream.write(json.dumps(record, sort_keys=True) + "\n")
        self.stream.flush()

    def __exit__(self, *exc):
        self.stream.close()


def summarize(arms):
    control = percentile(arms["disabled"]["ordinary_ms"], 95)
    result = {"schema": 1, "instrument": "local_asgi_actual_adapter_synthetic_stats",
              "ack_actor": "test_harness", "host_wake_ms": None, "model_ack_ms": None,
              "python_version": platform.python_version(), "mcp_sdk_version": _sdk_version(),
              # No MCP host runs in this instrument, so there is no host version
              # to report; the probe records one when a host actually connects.
              "host_version": None,
              "limitations": ["No model, host channel transport, network socket, embedding or GPU timing.",
                              "Fixed arm order; short synthetic workload is not a throughput claim.",
                              "Enqueue-to-event starts after the durable send response returns.",
                              "Ordinary latency measures fixture stats through the actual ASGI router."],
              "arms": {}}
    for name, data in arms.items():
        p95 = percentile(data["ordinary_ms"], 95)
        result["arms"][name] = {
            "samples": len(data["ordinary_ms"]), "ordinary_p50_ms": percentile(data["ordinary_ms"], 50),
            "ordinary_p95_ms": p95, "ordinary_p95_delta_ms": p95 - control,
            "enqueue_to_event_p95_ms": percentile(data["enqueue_to_event_ms"], 95),
            "enqueue_to_harness_ack_p95_ms": percentile(data["harness_ack_ms"], 95),
            "requests": data["requests"], "request_actions": data.get("request_actions", {}),
            "bootstrap_requests": data.get("bootstrap_requests", 0),
            "context_bytes": data["context_bytes"], "duplicate_ids": data["duplicate_ids"],
            "idempotent_retry_matches": data.get("idempotent_retry_matches", 0)}
        if "recovery" in data:
            result["arms"][name]["recovery"] = data["recovery"]
            result["limitations"].append(
                f"{name} recovery interleaves synthetic search and mail reads; "
                "it is outside the timed workload and does not measure memory persistence.")
    return result


def write_summary(path, summary):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, indent=2, sort_keys=True)
        stream.write("\n")


@contextmanager
def disposable_database(admin_url=None):
    # Never reinterpret a user's configured DSN as a disposable test target.
    if any(os.environ.get(key) for key in ("PSEUDOLIFE_TEST_DATABASE_URL", "PSEUDOLIFE_MCP_DATABASE_URL")):
        raise ValueError("database environment override refused; select the disposable server with --admin-url")
    # run_suffix tags the pid inside WSL, which shares the server with Windows.
    from tests.pg_defaults import run_suffix
    name = f"coordination_bench_{run_suffix()}_{uuid.uuid4().hex[:8]}"
    admin_url = admin_url or BENCH_ADMIN
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(admin_url)
    params["dbname"] = name
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(**params)
        finally:
            # The name is minted here and CREATE succeeded; no pre-existing bank
            # can be selected through arguments or environment configuration.
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


class MeasuredTransport(httpx.AsyncBaseTransport):
    def __init__(self, app, arm, trace):
        self.inner = httpx.ASGITransport(app=app)
        self.arm, self.trace = arm, trace
        self.actions = Counter()

    async def handle_async_request(self, request):
        start = time.perf_counter()
        response = await self.inner.handle_async_request(request)
        await response.aread()
        action = request.url.path.rsplit("/", 1)[-1]
        self.actions[action] += 1
        self.trace.write(arm=self.arm, kind="request", action=action, status=response.status_code,
                         elapsed_ms=(time.perf_counter() - start) * 1000, bytes=len(response.content))
        return response

    async def aclose(self):
        await self.inner.aclose()


async def run_recovery_cell(client, state_dir, tag, *, samples=3):
    """Reopen an actual adapter and interleave pending-mail reads with search.

    The caller owns the HTTP fixture and its disposable storage. Search uses
    the synthetic Console service; it does not measure retrieval or embedding.
    """
    from pseudolife_memory.coordination_adapter import CoordinationAdapter

    def adapter():
        return CoordinationAdapter("http://fixture", "synthetic-bearer", client=client,
                                   state_path=Path(state_dir) / "recovery.json", label="recovery")

    lease_name = "recovery:" + tag
    first = adapter()
    held = False
    try:
        async with first:
            identity = first.instance_headers["X-PL-Agent"]
            lease = await first._post("lease", {"name": lease_name, "ttl": 120})
            if lease.get("state") != "held" or type(lease.get("fence")) is not int:
                raise RuntimeError("recovery lease was not granted")
            held = True
            message = {"to": identity, "text": "synthetic recovery note", "request_id": tag}
            sent = await first._post("send", message)
        async with adapter() as reopened:
            if reopened.instance_headers["X-PL-Agent"] != identity:
                raise RuntimeError("recovery changed the persisted identity")
            renewed = await reopened._post("lease", {"name": lease_name, "ttl": 120})
            if renewed.get("state") != "held" or renewed.get("fence") != lease["fence"]:
                raise RuntimeError("recovery changed the owned lease fence")
            repeated = await reopened._post("send", message)
            message_id = sent.get("message_id")
            if not message_id or repeated.get("message_id") != message_id:
                raise RuntimeError("recovery changed the idempotent send receipt")
            for _ in range(samples):
                page, search = await asyncio.gather(
                    reopened._post("receive", {}),
                    client.get("http://fixture/api/search", params={"q": "synthetic recovery"},
                               headers={"Authorization": "Bearer synthetic-bearer"}))
                if [item["message_id"] for item in page["messages"]] != [message_id]:
                    raise RuntimeError("recovery did not replay pending mail")
                if search.status_code != 200 or not isinstance(search.json(), dict):
                    raise RuntimeError("recovery mixed search failed")
            await reopened._post("ack", {"message_id": message_id})
            if (await reopened._post("receive", {}))["messages"]:
                raise RuntimeError("recovery acknowledgment left pending mail")
    finally:
        if held:
            released = await first._post("release", {"name": lease_name})
            if released.get("released") is not True:
                raise RuntimeError("recovery lease cleanup failed")
    return {"identity_reopened": True, "pending_mail_replayed": True,
            "receipt_preserved": True, "lease_preserved": True,
            "mixed_search_calls": samples, "ack_actor": "test_harness"}


async def run_arm(storage, arm, samples, trace, *, recovery=False):
    from pseudolife_memory.coordination_adapter import CoordinationAdapter
    from pseudolife_memory.memory.hlc import HybridLogicalClock
    from pseudolife_memory.web.api import build_console_app
    from pseudolife_memory.web.fixtures import FixtureService
    from tests.asgi_helpers import stub_mcp

    from pseudolife_memory.storage.schema import assert_disposable_database
    assert_disposable_database(storage.conn)
    storage.conn.execute("TRUNCATE coordination_messages, coordination_agents, coordination_events")
    service = FixtureService()
    service.config.coordination.enabled = arm != "disabled"
    service.config.coordination.allowed_principals = ["default"]
    service._storage, service._lock, service._hlc = storage, threading.Lock(), HybridLogicalClock()
    service._ensure_init = lambda: None
    app = build_console_app(stub_mcp, "synthetic-bearer", lambda: {}, service)
    transport = MeasuredTransport(app, arm, trace)
    data = {"ordinary_ms": [], "enqueue_to_event_ms": [], "harness_ack_ms": [],
            "context_bytes": 0, "duplicate_ids": 0, "idempotent_retry_matches": 0}
    headers = {"Authorization": "Bearer synthetic-bearer"}
    seen = set()
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        async with AsyncExitStack() as stack:
            sender = recipient = inbox = None
            if arm != "disabled":
                sender = await stack.enter_async_context(CoordinationAdapter(
                    "http://fixture", "synthetic-bearer", client=client, label="sender"))
                recipient = await stack.enter_async_context(CoordinationAdapter(
                    "http://fixture", "synthetic-bearer", client=client, label="recipient", wake_enabled=arm == "channel"))
                if arm == "channel":
                    inbox = await stack.enter_async_context(aclosing(recipient.inbox()))

            async def post(agent, action, body):
                response = await client.post("/api/coordination/" + action,
                    headers={**headers, **(agent.instance_headers if agent else {})}, json=body)
                if response.status_code != 200:
                    raise RuntimeError(f"synthetic coordination {action} failed (HTTP {response.status_code})")
                return response.json()

            # Identical warm-up ordinary calls are excluded from the measured workload.
            for _ in range(3):
                response = await client.get("/api/stats", headers=headers)
                if response.status_code != 200:
                    raise RuntimeError("ordinary warm-up failed")
            data["bootstrap_requests"] = sum(transport.actions.values())
            transport.actions.clear()
            for sample in range(samples):
                start = time.perf_counter()
                response = await client.get("/api/stats", headers=headers)
                if response.status_code != 200:
                    raise RuntimeError("ordinary request failed")
                data["ordinary_ms"].append((time.perf_counter() - start) * 1000)
                message = {"to": recipient.instance_headers["X-PL-Agent"] if recipient else "disabled-recipient",
                           "text": f"Review synthetic patch {sample:03d}", "request_id": f"sample-{sample:03d}"}
                first = await post(sender, "send", message)
                enqueued = time.perf_counter()
                repeated = await post(sender, "send", message)
                if arm == "disabled":
                    if first != {"enabled": False} or repeated != first:
                        raise RuntimeError("disabled coordination changed state")
                    continue
                if first["message_id"] != repeated["message_id"]:
                    raise RuntimeError("idempotent retry changed message ID")
                data["idempotent_retry_matches"] += 1
                if inbox is not None:
                    event = await asyncio.wait_for(anext(inbox), 5)
                    message_id = event.meta["message_id"]
                    context = {"content": event.content, "meta": dict(event.meta)}
                else:
                    page = await post(recipient, "receive", {})
                    if len(page["messages"]) != 1:
                        raise RuntimeError("unexpected synthetic pending-mail count")
                    message_id = page["messages"][0]["message_id"]
                    context = page
                elapsed = (time.perf_counter() - enqueued) * 1000
                data["enqueue_to_event_ms"].append(elapsed)
                data["context_bytes"] += len(json.dumps(context, separators=(",", ":")).encode())
                data["duplicate_ids"] += int(message_id in seen)
                seen.add(message_id)
                if message_id != first["message_id"]:
                    raise RuntimeError("delivery returned the wrong message")
                trace.write(arm=arm, kind="adapter_event" if inbox else "pull_page", sample=sample, elapsed_ms=elapsed)
                # This explicit test-harness action is NOT a model acknowledgment.
                await post(recipient, "ack", {"message_id": message_id})
                elapsed = (time.perf_counter() - enqueued) * 1000
                data["harness_ack_ms"].append(elapsed)
                trace.write(arm=arm, kind="harness_ack", sample=sample, elapsed_ms=elapsed)
            data["requests"] = sum(transport.actions.values())
            data["request_actions"] = dict(transport.actions)
        if recovery and arm != "disabled":
            trace.write(arm=arm, kind="recovery_start")
            with tempfile.TemporaryDirectory(prefix="coordination-recovery-") as state_dir:
                data["recovery"] = await run_recovery_cell(
                    client, state_dir, uuid.uuid4().hex, samples=samples)
            trace.write(arm=arm, kind="recovery_complete")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--recovery", action="store_true",
                        help="also replay adapter reopen, mail/lease continuity and fixture search")
    parser.add_argument("--admin-url", help="admin URL for a separately provisioned disposable PG server")
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 30:
        parser.error("samples must be between 1 and 30 (sender rate bound)")
    out = Path(args.out).resolve()
    repository = next((parent for parent in (out, *out.parents) if (parent / ".git").exists()), None)
    if repository is not None and subprocess.run(
            ["git", "-C", str(repository), "check-ignore", "-q", "--", str(out)],
            capture_output=True).returncode != 0:
        parser.error("artifacts must be outside a repository or in a gitignored directory")
    out.mkdir(parents=True, exist_ok=False)
    with Trace(out / "trace.jsonl") as trace:
        trace.write(kind="launch")
        print("Trace opened; disposable local-bench database setup starting.", flush=True)
        with disposable_database(args.admin_url) as dsn:
            from pseudolife_memory.storage.postgres import PostgresStorage
            storage = PostgresStorage(dsn)
            try:
                async def drive():
                    arms = {}
                    for arm in ARMS:
                        trace.write(arm=arm, kind="arm_start")
                        arms[arm] = await run_arm(storage, arm, args.samples, trace, recovery=args.recovery)
                        print(f"{arm}: {args.samples} samples complete.", flush=True)
                    return summarize(arms)
                summary = asyncio.run(asyncio.wait_for(drive(), 60))
            finally:
                storage.close()
        write_summary(out / "summary.json", summary)
        trace.write(kind="complete")
    print("Summary written; disposable database removed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
