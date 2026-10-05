"""Real Python daemon baseline on a generated, disposable PostgreSQL bank."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .common import ROOT, child_environment, controls, distribution, lease_gate, memory_tree, provenance, write_result
from .transport import PROTOCOL, TOKEN

SEED = 61003
TOPICS = ("apricot", "birch", "cobalt", "dahlia", "elm", "fern", "granite", "hazel")


def corpus(count, seed=SEED):
    rng = random.Random(seed)
    return [{"text": f"Project {TOPICS[i % len(TOPICS)]} record {i:05d} has calibration value {rng.randrange(1000000):06d}.",
             "source": "synthetic-baseline", "origin": "agent"} for i in range(count)]


@contextmanager
def disposable_database():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from tests.pg_defaults import default_admin_url
    from evals.memory_policy_daemon import check_database
    from pseudolife_memory.storage.schema import assert_disposable_database
    # No target DSN argument: only this freshly minted name is ever handed to
    # the daemon. The existing production refusal remains independently active.
    admin_url = os.environ.get("PSEUDOLIFE_BENCH_ADMIN_URL") or default_admin_url()
    name = "plbench_rust_baseline_" + uuid.uuid4().hex[:12]
    params = conninfo_to_dict(admin_url)
    params["dbname"] = name
    dsn = make_conninfo(**params)
    check_database(dsn)
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(dsn, connect_timeout=5) as connection:
                if assert_disposable_database(connection) != name:
                    raise RuntimeError("disposable database identity mismatch")
            yield dsn
        finally:
            check_database(dsn)
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            if admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
                raise RuntimeError("disposable database cleanup failed")


class MCP:
    def __init__(self, url):
        import httpx
        self.client = httpx.Client(base_url=url, timeout=120, follow_redirects=False,
                                   trust_env=False, headers={"Authorization": "Bearer " + TOKEN,
                                   "Accept": "application/json, text/event-stream"})
        self.counter = 0

    def rpc(self, method, params=None, notification=False):
        self.counter += 1
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            body["id"] = self.counter
        response = self.client.post("/mcp", json=body)
        response.raise_for_status()
        if response.headers.get("mcp-session-id"):
            self.client.headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        if notification:
            return None
        messages = ([response.json()] if "application/json" in response.headers.get("content-type", "")
                    else [json.loads(line[5:].strip()) for line in response.text.splitlines()
                          if line.startswith("data:")])
        found = next((m for m in messages if m.get("id") == self.counter), None)
        if found is None or "error" in found:
            raise RuntimeError("daemon MCP response failed")
        return found["result"]

    def initialize(self):
        initialized = self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                               "clientInfo": {"name": "cpu-baseline", "version": "1"}})
        self.client.headers["Mcp-Protocol-Version"] = initialized["protocolVersion"]
        self.rpc("notifications/initialized", notification=True)
        return initialized

    def call(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError("daemon tool returned error: " + name)
        if "structuredContent" in result:
            return result["structuredContent"]
        texts = [item["text"] for item in result.get("content", []) if item["type"] == "text"]
        return json.loads("".join(texts))

    def coordination(self, action, payload, identity=None):
        headers = {} if identity is None else {"X-PL-Agent": identity["agent_id"],
                                               "X-PL-Agent-Key": identity["credential"]}
        response = self.client.post("/api/coordination/" + action, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


def free_port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def _daemon_command(source_root):
    # The instrument may live on a newer revision than the Python oracle.
    # Insert the selected checkout first before executing this instrument file.
    from evals.rust_port.provenance import module_command
    return module_command("evals.rust_baseline.daemon_child", source_root)


@contextmanager
def launched_daemon(dsn, private, *, env_extra=None, source_root=None, startup_timeout=180):
    import httpx
    from evals.rust_port.processes import owned_process
    source_root = Path(source_root or ROOT).resolve()
    port = free_port()
    config = Path(private) / "config.yaml"
    config.write_text("embedding:\n  device: cpu\n  backend: torch\n  cpu_dtype: fp32\n"
                      "memory:\n  dream:\n    enabled: false\nupdates:\n  check_releases: false\n", encoding="utf-8")
    env = child_environment(private)
    env.update({"PSEUDOLIFE_MCP_DATABASE_URL": dsn, "PSEUDOLIFE_MCP_CONFIG": str(config),
                "PSEUDOLIFE_MCP_HOST": "127.0.0.1", "PSEUDOLIFE_MCP_PORT": str(port),
                "PSEUDOLIFE_MCP_TOKEN": TOKEN, "PSEUDOLIFE_EMBEDDING_CPU_DTYPE": "fp32"})
    # The differential caller may isolate configuration homes/model-cache paths
    # and supply its own synthetic bearer. The target and CPU policy stay pinned.
    allowed = {"HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
               "XDG_DATA_HOME", "XDG_CACHE_HOME", "HF_HOME", "HF_HUB_CACHE",
               "TRANSFORMERS_CACHE", "PSEUDOLIFE_MCP_TOKEN"}
    if set(env_extra or {}) - allowed:
        raise ValueError("unsupported disposable daemon environment override")
    env.update(env_extra or {})
    nonce = secrets.token_hex(32)
    env["PSEUDOLIFE_BASELINE_NONCE"] = nonce
    url = f"http://127.0.0.1:{port}"
    cleanup = {"readiness_identity_verified": False}
    with (Path(private) / "daemon.log").open("w", encoding="utf-8") as log:
        process = None
        try:
            with owned_process(_daemon_command(source_root), cwd=source_root, env=env,
                               stdin=subprocess.DEVNULL, stdout=log, stderr=log) as process:
                deadline = time.monotonic() + startup_timeout
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("disposable daemon exited during startup")
                    try:
                        response = httpx.get(url + "/health", timeout=min(1, max(0.001, deadline - time.monotonic())),
                                             trust_env=False, follow_redirects=False)
                        if response.status_code == 200:
                            try:
                                payload = response.json()
                            except ValueError:
                                raise RuntimeError("disposable daemon readiness identity mismatch") from None
                            identity = payload.get("baseline_instance") if isinstance(payload, dict) else None
                            observed = identity.get("nonce") if isinstance(identity, dict) else None
                            verified = (isinstance(observed, str) and observed.isascii()
                                        and secrets.compare_digest(observed, nonce)
                                        and process.owns_runtime_pid(identity.get("pid"))
                                        and payload.get("status") == "ok")
                            if not verified:
                                raise RuntimeError("disposable daemon readiness identity mismatch")
                            if process.poll() is not None:
                                raise RuntimeError("disposable daemon exited during startup")
                            process.verified_runtime_pid = identity["pid"]
                            cleanup["readiness_identity_verified"] = True
                            if isinstance(identity.get("runtime"), dict):
                                cleanup["actual_child_runtime"] = identity["runtime"]
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("disposable daemon startup deadline exceeded")
                print(json.dumps({"daemon_health_observed": True, "readiness_identity_verified": True}), flush=True)
                yield process, url, cleanup
        finally:
            if process is not None:
                cleanup.update({"daemon_stopped": process.owned_cleanup["process_stopped"],
                                "children_stopped": process.owned_cleanup["subtree_stopped"],
                                "shutdown": "owned-process termination; bank disposable, no durability claim"})


def time_call(function):
    started = time.perf_counter()
    result = function()
    return (time.perf_counter() - started) * 1000, result


@contextmanager
def private_directory():
    temporary = tempfile.TemporaryDirectory(prefix="plbench_daemon_")
    try:
        yield temporary.name
    finally:
        # The owned process and Job have already stopped. Windows may briefly
        # retain the closed log handle; retry filesystem cleanup only.
        for attempt in range(50):
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.1)


def run_repeat(args, repeat):
    import psutil
    cpu_before = psutil.cpu_percent(interval=1)
    with disposable_database() as dsn, private_directory() as private:
        with launched_daemon(dsn, private, source_root=args.source_root) as (process, url, cleanup):
            client = MCP(url)
            try:
                initialized = client.initialize()
                planted = corpus(args.bank_size)
                for entry in planted:
                    if client.call("memory_store", entry).get("stored") is not True:
                        raise RuntimeError("synthetic seed store was rejected")
                # One query warmup per corpus topic; cache policy stays at defaults.
                queries = [{"query": f"Project {topic} calibration", "top_k": 8} for topic in TOPICS]
                for query in queries:
                    client.call("memory_search", query)
                health = client.client.get("/health").json()
                idle = []
                for _ in range(20):
                    idle.append(memory_tree(process.verified_runtime_pid)["rss_bytes"])
                    time.sleep(0.1)
                timed = {name: [] for name in ("memory_search", "memory_store", "memory_fact_set",
                                               "coordination_send", "coordination_receive", "mail_roundtrip")}
                rss = []
                stop = threading.Event()
                def sample_memory():
                    while not stop.is_set():
                        rss.append(memory_tree(process.verified_runtime_pid)["rss_bytes"])
                        stop.wait(0.02)
                sampler = threading.Thread(target=sample_memory, daemon=True)
                sampler.start()
                try:
                    for i in range(args.samples):
                        elapsed, result = time_call(lambda: client.call("memory_search", queries[i % len(queries)]))
                        if not isinstance(result.get("entries"), list):
                            raise RuntimeError("search result shape changed")
                        timed["memory_search"].append(elapsed)
                finally:
                    stop.set()
                    sampler.join(timeout=5)
                for i in range(args.samples):
                    entry = corpus(args.bank_size + args.samples)[args.bank_size + i]
                    elapsed, result = time_call(lambda: client.call("memory_store", entry))
                    if result.get("stored") is not True:
                        raise RuntimeError("timed store was rejected")
                    timed["memory_store"].append(elapsed)
                    fact = {"entity": f"synthetic-record-{i}", "attribute": "calibration",
                            "value": str(i), "origin": "agent"}
                    elapsed, result = time_call(lambda: client.call("memory_fact_set", fact))
                    if not result.get("action"):
                        raise RuntimeError("timed fact set returned no action")
                    timed["memory_fact_set"].append(elapsed)
                sender = client.coordination("register", {"label": "synthetic-sender"})
                recipient = client.coordination("register", {"label": "synthetic-recipient"})
                for i in range(args.samples):
                    started = time.perf_counter()
                    elapsed, sent = time_call(lambda: client.coordination("send", {
                        "to": recipient["agent_id"], "text": "synthetic baseline note",
                        "request_id": f"baseline-{i}"}, sender))
                    timed["coordination_send"].append(elapsed)
                    elapsed, received = time_call(lambda: client.coordination("receive", {}, recipient))
                    timed["coordination_receive"].append(elapsed)
                    timed["mail_roundtrip"].append((time.perf_counter() - started) * 1000)
                    if [m["message_id"] for m in received["messages"]] != [sent["message_id"]]:
                        raise RuntimeError("mail send-receive mismatch")
                    client.coordination("ack", {"message_id": sent["message_id"]}, recipient)
                result = {"repeat": repeat, "protocol": initialized["protocolVersion"],
                          "host_cpu_percent_before": cpu_before, "idle_rss_bytes": idle,
                          "search_load_rss_bytes": rss, "latency_ms": timed,
                          "embedder": health.get("embedder"), "schema": health.get("schema"),
                          "actual_child_runtime": cleanup.get("actual_child_runtime"),
                          "initial_bank_size": args.bank_size, "final_seed_and_store_count": args.bank_size + args.samples,
                          "cleanup": cleanup}
            finally:
                client.client.close()
        result["cleanup"]["database_dropped"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--bank-size", type=int, default=128)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--source-root", type=Path, default=ROOT,
                        help="Python source checkout; default is this instrument's repository")
    parser.add_argument("--board-checked-at", help="UTC time of lead's explicit board/peer resource-free verification")
    parser.add_argument("--offline-resource-checked-at",
                        help="UTC time of independent local/WSL lock and peer clearance when board unavailable")
    args = parser.parse_args()
    if min(args.repeats, args.samples, args.bank_size) < 1:
        parser.error("positive repeats, samples and bank size required")
    # Even a real-model smoke consumes GB: it obeys the same admission gate.
    resource = lease_gate(args.board_checked_at, offline_resource_checked_at=args.offline_resource_checked_at)
    if args.smoke:
        args.repeats, args.samples, args.bank_size = 1, 1, 2
    runs = []
    for repeat in range(args.repeats):
        lease_gate(args.board_checked_at, offline_resource_checked_at=args.offline_resource_checked_at)
        runs.append(run_repeat(args, repeat))
        print(json.dumps({"completed_repeat": repeat}), flush=True)
    summary = {}
    for name in runs[0]["latency_ms"]:
        blocks = [run["latency_ms"][name] for run in runs]
        summary[name] = {"ms": distribution([v for block in blocks for v in block]), "control": controls(blocks)}
    synthetic = json.dumps(corpus(args.bank_size), sort_keys=True).encode()
    write_result(args.out, {"schema": 1, "status": "plumbing-smoke" if args.smoke else "quiet-cpu-baseline",
                           "provenance": provenance(source_root=args.source_root), "resource_check": resource, "seed": SEED,
                           "synthetic_corpus_sha256": hashlib.sha256(synthetic).hexdigest(),
                           "method": {"repeats": args.repeats, "samples_per_repeat": args.samples,
                                      "bank_size": args.bank_size, "embedding_cache_size": 1024,
                                      "embedding_backend": "torch", "embedding_cpu_dtype": "fp32",
                                      "embedding_model": "Qwen/Qwen3-Embedding-0.6B", "threads": 1,
                                      "rss_root": "identity-verified owned runtime; excludes interpreter redirector",
                                      "load_rss_interval_s": 0.02, "search_query_cycle": list(TOPICS)},
                           "runs": runs, "summary": summary,
                           "idle_rss_bytes": distribution([v for run in runs for v in run["idle_rss_bytes"]]),
                           "search_load_rss_bytes": distribution([v for run in runs for v in run["search_load_rss_bytes"]]),
                           "limitations": ["Warm query embedding cache; fixed eight-query loop, sequential requests.",
                                           "Each repeat starts from a fresh bank with identical synthetic inputs; store/fact phases grow it.",
                                           "Memory is sampled process-tree RSS; excludes PostgreSQL and shared-page double-count adjustment.",
                                           "Dream and release network checks disabled; CPU only, one model thread.",
                                           "Mail timing is durable HTTP send plus immediate receive; no host delivery or wake timing.",
                                           "Owned daemon is terminated after timing; no graceful-shutdown or durability assertion.",
                                           "Smoke is plumbing only; never a performance baseline." if args.smoke else
                                           "Full-suite lease checked before each repeat; ambient CPU load recorded, no speedup claim."]})


if __name__ == "__main__":
    main()
