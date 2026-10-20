"""Abandon a store, signal shutdown while it is pending, then restart.

The same raw request and owned-process signal run against Python and Rust.
The entry must be absent when shutdown is requested, present after exit,
and present after restart; full database states use the existing W2-E
comparator and tolerances. Signal exit codes are reported, not normalized.

``--mutant NAME`` (a ``--features mutants`` build) also runs a Rust arm with
that deliberate break. It passes only when the Python oracle persisted the
store, the unmutated Rust arm matches it, and the mutated arm differs: the
``store-cancellable`` control, whose store is not tracked for the shutdown
drain, is observable only here (over MCP a disconnect never cancels a call).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

import psycopg

import w2e as h


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rust-bin", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mutant")
    args = parser.parse_args()
    args.rust_bin = args.rust_bin.resolve()
    root = h.run_root("w2e-shutdown")
    scenario = {"config": "memory:\n  dream:\n    enabled: false\n  retrieval_log:\n    enabled: false\n", "seed": []}
    procs = {}
    owned = []
    results = {}
    text = ("A disconnected store must finish before daemon shutdown. " * 160).strip()
    call_args = {"text": text, "source": "fixture"}
    arms = [("python", None), ("rust", None)] + ([("mutant", args.mutant)] if args.mutant else [])
    try:
        template = h.prepare_template(scenario, "shutdown", root, owned)
        for label, mutant in arms:
            side = "python" if label == "python" else "rust"
            name = h.pg.name(f"pl_cf_w2e_shutdown_{label}")
            owned.append(name)
            dsn = h.pg.create(name, template=template)
            before = h.dbstate.dump(dsn)
            home = h.daemons.make_home(root, label, scenario["config"])
            env = h.side_env(scenario, dsn)
            if side == "rust":
                env = h.w1a.rust_env(env)
                if mutant:
                    env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
            env = h.daemons.base_env(home, env)
            port = h.daemons.free_port()
            daemon = (h.daemons.python_daemon(home, port, env) if side == "python"
                      else h.daemons.rust_daemon(args.rust_bin, home, port, env))
            if side == "python":
                # Match background_sessions.py's normal-return fixture:
                # uvicorn rethrows signals to the previous handler. Let
                # that handler return so Python's registered exit save runs.
                # Ordinary CLI signal exit semantics remain a separate test.
                daemon.argv = [sys.executable, "-c", """
import signal
from pseudolife_memory import mcp_server
for sig in (signal.SIGTERM, getattr(signal, 'SIGBREAK', signal.SIGTERM)):
    signal.signal(sig, lambda *_: None)
from pseudolife_memory.daemon import run_daemon
run_daemon()
"""]
            procs[label] = daemon
            daemon.start(300)
            h.w1a.wait_settled([port], token=h.TOKEN)
            mcp = h.Mcp(port)
            mcp.init()
            mcp.abandon("memory_store", call_args)
            with psycopg.connect(dsn) as conn:
                pending = conn.execute("SELECT count(*) FROM entries WHERE text=%s", (text,)).fetchone()[0] == 0
            if not pending:
                raise RuntimeError(f"{label}: store completed before the shutdown probe")
            daemon.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            exit_code = daemon.proc.wait(timeout=120)
            daemon.stop()
            stopped = h.dbstate.dump(dsn)
            with psycopg.connect(dsn) as conn:
                persisted = conn.execute("SELECT count(*) FROM entries WHERE text=%s", (text,)).fetchone()[0]
            daemon.start(300)
            h.w1a.wait_settled([port], token=h.TOKEN)
            restarted = h.dbstate.dump(dsn)
            with psycopg.connect(dsn) as conn:
                after_restart = conn.execute("SELECT count(*) FROM entries WHERE text=%s", (text,)).fetchone()[0]
            results[label] = {"pending_at_signal": pending, "exit": exit_code,
                             "persisted": persisted, "after_restart": after_restart,
                             "states": [h.normalize_state(state, before) for state in (stopped, restarted)]}
            daemon.stop()
        tolerance = json.loads(h.TOLERANCE_FILE.read_text(encoding="utf-8"))
        stats = {}

        def survived(label):
            r = results[label]
            return r["persisted"] == 1 and r["after_restart"] == 1

        def compare(label):
            """Differences of one Rust arm against the Python oracle."""
            out = []
            for milestone in range(2):
                a, b = (results[x]["states"][milestone] for x in ("python", label))
                out += h.state_diff(a[0], b[0])
                out += h.compare_numeric(a[1], b[1], tolerance, stats)
            if not survived(label):
                out.append(f"{label}: admitted store did not survive shutdown/restart")
            return out

        differences = compare("rust")
        if not survived("python"):
            differences.append("python: admitted store did not survive shutdown/restart")
        mutant_differences = compare("mutant") if args.mutant else []
        for result in results.values():
            del result["states"]
        result = {"arms": results, "differences": differences,
                  "mutant_differences": mutant_differences, "stats": stats}
        args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(json.dumps({"arms": results, "difference_count": len(differences),
                          "mutant_difference_count": len(mutant_differences), "stats": stats,
                          "mutant": args.mutant}))
        if args.mutant:
            # A kill counts only against a healthy oracle and a clean baseline.
            if differences:
                print(f"mutant {args.mutant}: INCONCLUSIVE (the unmutated baseline differs)")
                return 1
            caught = bool(mutant_differences)
            print(f"mutant {args.mutant}: {'caught' if caught else 'MISSED'}")
            return int(not caught)
        return int(bool(differences))
    finally:
        for daemon in procs.values():
            daemon.stop()
        for name in reversed(owned):
            h.pg.drop(name)


if __name__ == "__main__":
    raise SystemExit(main())
