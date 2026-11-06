"""Check the proofs' contract over summary.json; exit 1 on any broken claim.

    python verify.py <summary.json>

proof.sh records what each step read back; this asserts what the README
claims about it, so a pass is not just "every command exited 0".
"""
import json
import sys

s = json.load(open(sys.argv[1], encoding="utf-8"))
failures = []


def check(ok, claim):
    print(("ok   " if ok else "FAIL ") + claim)
    if not ok:
        failures.append(claim)


def texts(reads):
    return [t for r in reads if r["tool"] == "memory_search" for t in r["texts"]]


def fact(reads):
    return next(r["value"] for r in reads if r["tool"] == "memory_fact_get")


def no_errors(reads):
    return all(not r["isError"] for r in reads)


for name in ("proof_1_fresh_install", "proof_2_update_python_0_22_1_to_rust", "proof_3_backup_restore_rollback"):
    bad = {k: v for k, v in s[name]["steps"].items() if v != 0 and not k.endswith("-default")}
    check(not bad, f"{name}: every step but the recorded default activation exited 0 {bad or ''}")

p1 = s["proof_1_fresh_install"]
check(p1["health_after_install"]["status"] == "ok", "P1: the fresh install reports healthy")
check(p1["engine_after_select"]["engine_label"] == "rust", "P1: Rust is the selected engine")
check(p1["health_after_restart"]["status"] == "ok" and p1["health_after_restart"]["build_git_sha"] == s["tested_head"],
      "P1: after a restart Rust is healthy at the tested head")
check(p1["engine_after_restart"] == p1["engine_after_select"], "P1: the restart kept the same Rust image")
r = p1["reads_after_restart_via_registered_shim"]
check(no_errors(r) and "G2 fresh-install note served by Rust" in texts(r) and fact(r) == "rust",
      "P1: the registered shim reads the note and fact written before the restart")

p2 = s["proof_2_update_python_0_22_1_to_rust"]
check(p2["engine_before"]["engine_label"] is None and p2["engine_before"]["image"] == "pseudolife-daemon:0.22.1",
      "P2: the starting daemon is the v0.22.1 install")
check(p2["engine_after"]["engine_label"] == "rust" and p2["health_after"]["build_git_sha"] == s["tested_head"],
      "P2: Rust serves at the tested head after the update")
check(p2["bank_before"] == p2["bank_after"], "P2: the bank fingerprint is unchanged by the switch")
check(any(t.startswith("pseudolife-daemon-python:0.22.1-pre-update-") and t.endswith(p2["engine_before"]["image_id"][7:19])
          for t in p2["rollback_tags"]), "P2: a rollback tag names the original v0.22.1 image")
r = p2["reads_via_registered_shim"]
check(no_errors(r) and {"G2 proof note one, written by Python 0.22.1",
                        "G2 proof note two: the bank must survive the engine switch"} <= set(texts(r))
      and fact(r) == "python-0.22.1", "P2: Rust reads v0.22.1's notes and fact through the registered shim")

p3 = s["proof_3_backup_restore_rollback"]
check(p3["bank_after_restore"] == p3["bank_at_backup"], "P3: the restored bank equals the backup-time fingerprint")
r = p3["reads_after_restore"]
check(no_errors(r) and not any("AFTER the backup" in t for t in texts(r)) and fact(r) == "rust",
      "P3: the marker written after the backup is gone; the Rust-era fact is back")
check(p3["engine_after_restore"]["engine_label"] == "rust", "P3: Rust serves after the restore")
check(p3["engine_after_printed_rollback"]["image_id"] == p2["engine_before"]["image_id"]
      and p3["health_after_printed_rollback"]["version"] == "0.22.1"
      and p3["health_after_printed_rollback"]["bank"] == p2["health_after"]["bank"],
      "P3: the printed rollback brings back the original v0.22.1 image on the same bank id")
check(p3["bank_after_printed_rollback"] == p3["bank_at_backup"], "P3: the rollback leaves the bank fingerprint as Rust left it")
r = p3["python_0_22_1_reads_rust_writes"]
check(no_errors(r) and "G2 proof note three, written by the Rust daemon" in texts(r) and fact(r) == "rust",
      "P3: v0.22.1 reads the note and fact Rust wrote")
check(p3["steps"].get("python-0.22.1-writes") == 0, "P3: v0.22.1 writes after the rollback")
check(p3["bank_after_engine_python_from_master"]["entries"]["rows"] == p3["bank_at_backup"]["entries"]["rows"] + 1,
      "P3: master's Python sees v0.22.1's new entry after --engine python")

for failure in s["default_activation_gate_failures"]:
    check(any("failed its version, schema or authenticated-read gate" in line for line in failure["updater"]),
          f"gap: the default activation failed its gate on {failure['host']}")
    if "daemon_at_failure" in failure:
        check(any("model provisioning phase" in line for line in failure["daemon_at_failure"]),
              f"gap: the daemon was still provisioning models on {failure['host']}")

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
