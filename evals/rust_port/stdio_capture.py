"""Capture Python shim parity on generated banks through existing ownership guards."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from .harness import capture_platform, write_new
from .provenance import ROOT, SOURCE_PATHS, runtime_metadata, schema_version, source_metadata
from .stdio import capture

ORACLE_HEAD = "686b3f95c4a4d4e6c2d81e1b76be9901945a8da1"
ORACLE_SCHEMA = 55
BEHAVIOR_TESTS = ("tests/test_shim.py", "tests/test_shim_transport_recovery.py",
    "tests/test_shim_board_retry.py", "tests/test_version_handshake.py", "tests/test_update_offer.py",
    "tests/test_mcp_client_neutrality.py", "tests/test_mcp_stdio_errlog.py",
    "tests/test_connection_loss_recovery.py", "tests/test_shim_channel.py",
    "tests/fake_embedder.py", "tests/pg_fixtures.py")


def require_phase1_source(root):
    checked_paths = (*SOURCE_PATHS, *BEHAVIOR_TESTS)
    result = subprocess.run(["git", "diff", "--quiet", ORACLE_HEAD, "--", *checked_paths],
                            cwd=root, capture_output=True, timeout=10)
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard",
                                        "--", *checked_paths], cwd=root)
    if result.returncode != 0 or untracked or schema_version(root / "pseudolife_memory/storage/schema.py") != ORACLE_SCHEMA:
        raise RuntimeError("phase-1 pinned production source required")
    return {**source_metadata(root, oracle_head=ORACLE_HEAD, oracle_schema=ORACLE_SCHEMA),
            "oracle_head": ORACLE_HEAD, "oracle_schema": ORACLE_SCHEMA,
            "production_source_matches_pin": True, "behavior_test_sources_match_pin": True}


def initialize(wire, era="2025-11-25"):
    if era == "2026-07-28":
        wire.send({"jsonrpc": "2.0", "id": "open", "method": "server/discover", "params": {
            "_meta": modern_meta()}})
    else:
        wire.send({"jsonrpc": "2.0", "id": "open", "method": "initialize", "params": {
            "protocolVersion": era, "capabilities": {},
            "clientInfo": {"name": "disposable-stdio-proof", "version": "1"}}})
    wire.response("open")
    if era != "2026-07-28":
        wire.send({"jsonrpc": "2.0", "method": "notifications/initialized"})


def modern_meta():
    return {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "disposable-stdio-proof", "version": "1"}}


def proof(wire):
    initialize(wire)
    wire.send({"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}})
    value = wire.response("list")
    if not value.get("result", {}).get("tools"):
        raise RuntimeError("real disposable daemon returned no tools")


def run_proof(root, out, *, offline_resource_checked_at=None):
    from evals.rust_baseline.common import lease_gate
    from evals.rust_baseline.daemon import disposable_database, launched_daemon, private_directory
    from evals.rust_baseline.transport import TOKEN
    from .full_bank import private_home_overrides
    source = require_phase1_source(root)
    resource = lease_gate(offline_resource_checked_at=offline_resource_checked_at)
    arms = []
    for arm in ("oracle", "python-replay"):
        with disposable_database() as dsn, private_directory() as private:
            with launched_daemon(dsn, private, source_root=root,
                                 env_extra=private_home_overrides(private),
                                 child_module="evals.rust_port.stdio_daemon", startup_timeout=90) as (_, url, cleanup):
                observed = capture([sys.executable, "-m", "pseudolife_memory.cli"], cwd=root,
                                   home=Path(private) / "shim-home", url=url, token=TOKEN, exercise=proof)
                observed["arm"] = arm
                observed["daemon_cleanup"] = cleanup
                arms.append(observed)
        cleanup["database_dropped"] = True
    differences = []
    for key in ("stdout_frames_b64", "stderr_b64", "exit_code"):
        if arms[0][key] != arms[1][key]:
            differences.append({"path": "/" + key, "reason": "stdio_raw_bytes" if key != "exit_code" else "exit_code"})
    receipt = {"schema": 1, "status": "passed" if not differences else "difference",
               **source, "platform": capture_platform(), "resource_check": resource,
               "capture_runtime": runtime_metadata(root),
               "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "policy": "stdio-raw-compared", "normalizations": [],
               "limitation": "Real daemon/storage/stdio; deterministic hashing test embeddings, no model or retrieval parity.",
               "instrument_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in sorted(Path(__file__).parent.glob("stdio*.py"))},
               "arms": arms, "differences": differences}
    write_new(out, receipt)
    print(json.dumps({"receipt": out.name, "status": receipt["status"], "differences": len(differences),
                      "cleanup_verified": all(a["daemon_cleanup"].get("database_dropped") and
                                              a["daemon_cleanup"].get("daemon_stopped") and
                                              all(a["cleanup"].values()) for a in arms)}), flush=True)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--offline-resource-checked-at")
    args = parser.parse_args()
    result = run_proof(args.oracle_root.resolve(), args.out,
                       offline_resource_checked_at=args.offline_resource_checked_at)
    if result["differences"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
