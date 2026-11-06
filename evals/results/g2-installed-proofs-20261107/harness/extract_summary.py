"""Build summary.json from a proof run's raw evidence directory.

    python extract_summary.py <evidence-dir> <summary.json> [<earlier-run-dir>]

<evidence-dir> is run_proofs.sh's output (the ledgers plus upgrade-logs/
and fresh-logs/). The raw logs stay private; this keeps what the checks
read back plus short excerpts of the log lines the README cites. An
optional <earlier-run-dir> adds that run's fresh-host gate failure.
"""
import json
import os
import re
import sys

# The driver passes the master sha it tested; the default is this record's.
TESTED_HEAD = os.environ.get("G2PROOF_HEAD", "5274b0b9269305f304f2b41ec756e408b60cac42")
D, OUT = sys.argv[1], sys.argv[2]
EARLIER = sys.argv[3] if len(sys.argv) > 3 else None
SECRET = re.compile(r"(?i)(token|password|secret|bearer)\S*\s*[=:]\s*\S+")


def log(mode, step, base=D):
    path = os.path.join(base, f"{mode}-logs", f"{mode}-{step}.log")
    return open(path, encoding="utf-8", errors="replace").read()


def excerpt(text, *patterns):
    """The lines matching any pattern, with anything credential-shaped dropped."""
    return [line.strip()[:300] for line in text.splitlines()
            if any(p in line for p in patterns) and not SECRET.search(line)]


def health(mode, step):
    h = json.loads(log(mode, step))
    keep = ("status", "version", "schema", "storage", "auth", "bank", "persist_errors")
    return {k: h.get(k) for k in keep} | {"build_git_sha": h.get("build", {}).get("git_sha")}


def engine(mode, step):
    parts = log(mode, step).split()
    if len(parts) == 2:  # a plain install carries no engine label
        return {"engine_label": None, "image": parts[0], "image_id": parts[1][:19]}
    return {"engine_label": parts[0], "image": parts[1], "image_id": parts[2][:19]}


def snap(mode, step):
    rows = (line.split("|") for line in log(mode, step).split())
    return {name: {"rows": int(n), "fingerprint": fp} for name, n, fp in rows}


def reads(mode, step):
    out = []
    for line in log(mode, step).splitlines():
        r = json.loads(line)
        if "tool" not in r:
            continue
        body = json.loads(r["text"])
        if r["tool"] == "memory_search":
            out.append({"tool": r["tool"], "isError": r["isError"], "count": body["count"],
                        "texts": [e["text"] for e in body["entries"]]})
        else:
            out.append({"tool": r["tool"], "isError": r["isError"],
                        "value": (body.get("record") or {}).get("value")})
    return out


def ledger(mode):
    return [json.loads(line) for line in open(os.path.join(D, f"{mode}-ledger.jsonl"))]


def steps(mode, proof=None):
    return {r["step"]: r["exit"] for r in ledger(mode) if proof is None or r["proof"] == proof}


PROVISIONING = ("Up ", "health:", "model provisioning", "downloading", "Recv failure", "Connection reset")
summary = {
    "tested_head": TESTED_HEAD,
    "python_release_for_upgrade": "v0.22.1",
    "venue": "Windows host Docker Desktop 29.9.2, one disposable Docker-in-Docker host per proof "
             "(debian:bookworm, docker-ce 29.9.3, nested cgroup v2), removed after each run; its own "
             "PostgreSQL, bank and state volumes; never the live bank",
    "run_window_utc": [ledger("upgrade")[0]["start"], ledger("fresh")[-1]["end"]],
    "snapshot_scope": "entries: count and md5 over id:md5(text) per row; facts: count and one md5 over "
                      "entity|attribute|value|status of every row; meta.schema_version. Two of the bank's "
                      "tables; embeddings, tags, episodes, graph, counters and the /data state volume are "
                      "not fingerprinted.",
    "proof_1_fresh_install": {
        "steps": steps("fresh"),
        "health_after_install": health("fresh", "health-after-install"),
        "engine_after_select": engine("fresh", "engine-rust"),
        "health_after_restart": health("fresh", "health-after-restart"),
        "engine_after_restart": engine("fresh", "engine-after-restart"),
        "reads_after_restart_via_registered_shim": reads("fresh", "read-after-restart"),
        "rerun_excerpt": excerpt(log("fresh", "rerun-select-rust"), "backing up", "rollback image",
                                 "health gate passed", "selected Rust"),
    },
    "proof_2_update_python_0_22_1_to_rust": {
        "steps": steps("upgrade", "P2"),
        "engine_before": engine("upgrade", "engine-python"),
        "engine_after_failed_default_rollback": engine("upgrade", "update-to-rust-engine-after-failed-rollback"),
        "engine_after": engine("upgrade", "engine-rust"),
        "health_after": health("upgrade", "health-rust"),
        "bank_before": snap("upgrade", "snapshot-before"),
        "bank_after": snap("upgrade", "snapshot-after"),
        "rollback_tags": log("upgrade", "rollback-tags").split("\n")[:-1],
        "reads_via_registered_shim": reads("upgrade", "read-via-registered-shim"),
        "update_backup_excerpt": excerpt(log("upgrade", "update-to-rust"), "backing up", "Dumping",
                                         "State volume", "Recorded in the daemon", "Backup complete",
                                         "rollback image", "health gate passed"),
    },
    "proof_3_backup_restore_rollback": {
        "steps": steps("upgrade", "P3"),
        "bank_at_backup": snap("upgrade", "snapshot-rust"),
        "backup_excerpt": excerpt(log("upgrade", "backup-under-rust"), "bank dump", "state archive"),
        "restore_excerpt": excerpt(log("upgrade", "restore-under-rust"), "Safety", "Dropping", "Restore complete"),
        "bank_after_restore": snap("upgrade", "snapshot-after-restore"),
        "engine_after_restore": engine("upgrade", "engine-after-restore"),
        "reads_after_restore": reads("upgrade", "read-after-restore"),
        "engine_after_printed_rollback": engine("upgrade", "engine-after-printed-rollback"),
        "health_after_printed_rollback": health("upgrade", "health-python-0.22.1-after-rollback"),
        "bank_after_printed_rollback": snap("upgrade", "snapshot-after-printed-rollback"),
        "python_0_22_1_reads_rust_writes": reads("upgrade", "python-0.22.1-reads-rust-writes"),
        "engine_python_from_master": engine("upgrade", "engine-python-master"),
        "bank_after_engine_python_from_master": snap("upgrade", "snapshot-python-master"),
    },
    "default_activation_gate_failures": [
        {"host": "upgrade (this run)",
         "updater": excerpt(log("upgrade", "update-to-rust-default"), "failed its version"),
         "daemon_at_failure": excerpt(log("upgrade", "diagnose-update-to-rust-default"), *PROVISIONING)},
        {"host": "fresh (this run)",
         "updater": excerpt(log("fresh", "select-rust-default"), "failed its version"),
         "daemon_at_failure": excerpt(log("fresh", "diagnose-select-rust-default"), *PROVISIONING)},
    ],
}
if EARLIER:
    summary["default_activation_gate_failures"].append(
        {"host": "fresh (the run before, 16:40-16:49Z, harness without diagnostics)",
         "updater": excerpt(log("fresh", "select-rust", EARLIER), "failed its version")})
json.dump(summary, open(OUT, "w", encoding="utf-8", newline="\n"), indent=2)
