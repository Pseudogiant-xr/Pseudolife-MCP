"""Allowlisted provenance and repeated-control statistics."""
from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def percentile(values, percent):
    return sorted(values)[max(0, math.ceil(len(values) * percent / 100) - 1)] if values else None


def distribution(values):
    return {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95),
            "median": statistics.median(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def controls(blocks):
    medians = [statistics.median(block) for block in blocks if block]
    return {"repeat_medians": medians,
            "noise_floor_abs": max(medians) - min(medians) if len(medians) >= 2 else None,
            "available": len(medians) >= 2,
            "method": "max minus min of medians of identical-input repeated control blocks",
            "interpretation": "A delta below this descriptive observed floor is not a finding; not a confidence interval."}


def provenance(host_label="local-cpu-01", *, source_root=None):
    import psutil
    from evals.rust_port import provenance as runtime_provenance
    from evals.rust_port.processes import execution_sources
    source_root = Path(source_root or ROOT).resolve()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source_root, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=source_root, text=True).strip())
    schema_file = source_root / "pseudolife_memory" / "storage" / "schema.py"
    declarations = ast.parse(schema_file.read_text(encoding="utf-8")).body
    schema = next(ast.literal_eval(node.value) for node in declarations
                  if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                  and target.id == "SCHEMA_META_VERSION" for target in node.targets))
    dependencies = {}
    for name in ("psutil", "mcp", "httpx", "torch", "sentence-transformers", "psycopg"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    return {"measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_head": head, "source_schema": schema, "source_dirty": dirty,
            "parent_runtime": {"python": platform.python_version(), "dependencies": dependencies},
            "host": {"label": host_label, "os": platform.system(), "os_release": platform.release(),
                     "architecture": platform.machine(), "logical_cpus": psutil.cpu_count(),
                     "physical_cpus": psutil.cpu_count(logical=False),
                     "ram_bytes": psutil.virtual_memory().total},
            "instrument_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"):
                                  hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted([*Path(__file__).parent.glob("*.py"),
                                      *(ROOT / name for name in runtime_provenance.PARENT_ISOLATION_HELPERS)])},
            "process_helper_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in execution_sources()},
            "runtime_provenance_sha256": {
                "provenance.py": hashlib.sha256(Path(runtime_provenance.__file__).read_bytes()).hexdigest()}}


def child_environment(private_dir):
    # Installed bank, token files, delivery endpoints and hook session identities
    # never flow into the disposable child. Keep runtime and model cache settings.
    from evals.memory_policy_bench import scrubbed_env
    env = scrubbed_env()
    env.update({"CUDA_VISIBLE_DEVICES": "-1", "HF_HUB_OFFLINE": "1",
                "TOKENIZERS_PARALLELISM": "false", "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1", "PSEUDOLIFE_MCP_NO_SPAWN": "1",
                "PSEUDOLIFE_AGENT_COORDINATION": "0", "PSEUDOLIFE_CODEX_DOORBELL": "0",
                "PSEUDOLIFE_MCP_DATA_DIR": str(private_dir),
                "PSEUDOLIFE_AGENT_STATE_DIR": str(private_dir)})
    return env


def lease_gate(board_checked_at=None, *, offline_resource_checked_at=None):
    if board_checked_at and offline_resource_checked_at:
        raise ValueError("board and offline resource attestations are mutually exclusive")
    result = subprocess.run(["pseudolife-mcp", "lease", "check", "full-suite", "--json"],
                            capture_output=True, text=True, timeout=30)
    try:
        report = json.loads(result.stdout)
        # The CLI reports an absent lock as null and confirms freedom through
        # exit zero and held=false; a fresh hosted runner has no lock file yet.
        local_free = report["local"]["state"] in (None, "free")
        board_available = report["board"]["available"] is True
        held = report["held"] is not False
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("full-suite resource check malformed; no quiet baseline launched") from None
    if result.returncode != 0 or not local_free or held:
        raise RuntimeError("full-suite resource check held; no quiet baseline launched")
    if not board_available and not (board_checked_at or offline_resource_checked_at):
        raise RuntimeError("full-suite board unavailable without independent resource clearance")
    return {"checked_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "local_lease_check_exit": result.returncode, "local_lock_free": local_free,
            "cli_board_available": board_available,
            "lead_board_checked_free_at_utc": board_checked_at,
            "offline_resource_checked_at_utc": offline_resource_checked_at,
            "offline_resource_clearance_used": not board_available and bool(offline_resource_checked_at),
            "offline_resource_clearance_scope": "independent local/WSL lock and peer workload clearance"
                if offline_resource_checked_at else None}


def write_result(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"result": path.name, "status": result.get("status"), "written": True}))


def memory_tree(pid):
    import psutil
    root = psutil.Process(pid)
    rows = []
    for process in [root, *root.children(recursive=True)]:
        try:
            rows.append(process.memory_info().rss)
        except psutil.NoSuchProcess:
            pass
    return {"rss_bytes": sum(rows), "processes": len(rows)}
