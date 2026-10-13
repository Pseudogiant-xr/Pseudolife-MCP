"""Run the MCP-surface harness end to end on disposable banks.

usage: python mcp_run.py <rust-binary> <out-dir> [--mutant NAME --baseline <compare.json>]
                         [--tokenless | --config2] [--record <golden.json>]

Creates pl_cf_w2g_py and pl_cf_w2g_rs through the test login, starts the
Python oracle (from this checkout) and the Rust daemon on them with the
harness environment (mcp_compare.py's docstring), runs `mcp_compare.py
compare`, stops both daemons by PID and drops the banks. `--mutant NAME`
sets PSEUDOLIFE_DAEMON_MUTANT for the Rust side (a `--features mutants`
build) and requires an ordinary baseline report: the run passes only if the
strict comparison finds changed or additional differences beyond that baseline.
Exit 0 means the expected verdict.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daemons  # noqa: E402

sys.path.insert(0, str(daemons.REPO))

os.environ["PL_HARNESS_SLICE"] = "w2g"
import pgdisposable as pg  # noqa: E402

ENV = {
    "PSEUDOLIFE_MCP_TOKEN": "tok-default-0001",
    "PSEUDOLIFE_MCP_TOKENS": "tok-alice-0001:alice,tok-bob-0001:bob,tok-carol-0001:carol",
    "PSEUDOLIFE_MCP_TIER_MAP": "alice:minimal,bob:core,writer-m:minimal",
    "PSEUDOLIFE_MCP_TOOLSET": "core",
}


# Stored principals, seeded on both banks through Python's own write path
# (principal_store.create_invite, then redeem): name -> (token, tier).
STORED = {"dave": ("tok-dave-0001", "minimal"), "erin": ("tok-erin-0001", None),
          "frank": ("tok-frank-0001", "full")}


def seed_stored(dsn):
    import psycopg
    from pseudolife_memory.principal_store import create_invite, redeem
    from pseudolife_memory.principals import secret_sha256
    with psycopg.connect(dsn, autocommit=True) as conn:
        for name, (token, tier) in STORED.items():
            invite = create_invite(conn, name, tier=tier, ttl_seconds=600, replace=False)
            row = redeem(dsn, secret_sha256(invite["code"]), secret_sha256(token))
            assert row is not None and row.principal == name, (name, row)


def mutant_caught(code, result, baseline):
    return (code == 1 and result["summary"]["diffs"] > 0
            and any(diff not in baseline["diffs"] for diff in result["diffs"]))


def main():
    args = sys.argv[1:]
    binary, out = Path(args[0]), Path(args[1])
    mutant = args[args.index("--mutant") + 1] if "--mutant" in args else None
    baseline = None
    if mutant:
        if "--baseline" not in args:
            print("--mutant requires --baseline from an ordinary comparison", file=sys.stderr)
            return 2
        try:
            baseline = json.loads(Path(args[args.index("--baseline") + 1]).read_text(encoding="utf-8"))
            if not isinstance(baseline["diffs"], list):
                raise ValueError("baseline diffs must be a list")
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            print(f"cannot load mutant baseline: {exc}", file=sys.stderr)
            return 2
    record = args[args.index("--record") + 1] if "--record" in args else None
    env = dict(ENV)
    if "--tokenless" in args:
        env = {"PSEUDOLIFE_MCP_TIER_MAP": ENV["PSEUDOLIFE_MCP_TIER_MAP"],
               "PSEUDOLIFE_MCP_TOOLSET": "core"}
    if "--config2" in args:
        env = dict(ENV, PSEUDOLIFE_WRITER_ID="writer-m", PSEUDOLIFE_MCP_TOOLSET=" Bogus ",
                   PSEUDOLIFE_MCP_TIER_MAP=" Alice : Minimal ,bad,:core,writer-m:minimal,bob:huge,"
                                           "bob:core, x:y:z")
    out.mkdir(parents=True, exist_ok=True)
    root = daemons.scratch_root() / "w2g"
    started = []
    dbs = {side: pg.name(f"pl_cf_w2g_{side}") for side in ("py", "rs")}
    try:
        py_home = daemons.make_home(root, "py", None)
        rs_home = daemons.make_home(root, "rs", None)
        py_port, rs_port = daemons.free_port(), daemons.free_port()
        py_dsn, rs_dsn = pg.create(dbs["py"]), pg.create(dbs["rs"])
        py_env = daemons.base_env(py_home, dict(env, PSEUDOLIFE_MCP_DATABASE_URL=py_dsn))
        rs_extra = dict(env, PSEUDOLIFE_MCP_DATABASE_URL=rs_dsn)
        if mutant:
            rs_extra["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
        rs_env = daemons.base_env(rs_home, rs_extra)
        py = daemons.python_daemon(py_home, py_port, py_env).start(wait_s=300)
        started.append(py)
        rs = daemons.rust_daemon(binary, rs_home, rs_port, rs_env).start(wait_s=60)
        started.append(rs)
        if "--tokenless" not in args and "--config2" not in args:
            # Both daemons created the principals table at start; seed, then
            # outwait both 10 s snapshot refreshes.
            import time
            time.sleep(3)
            seed_stored(py_dsn)
            seed_stored(rs_dsn)
            time.sleep(13)
        if record:
            cmd = [sys.executable, str(HERE / "mcp_compare.py"), "record", str(py_port), record]
        else:
            cmd = [sys.executable, str(HERE / "mcp_compare.py"), "compare", str(py_port),
                   str(rs_port), str(out / ("mutant-" + mutant + ".json" if mutant else "compare.json"))]
        for flag in ("--tokenless", "--config2"):
            if flag in args:
                cmd.append(flag)
        code = subprocess.run(cmd).returncode
    finally:
        for d in reversed(started):
            d.stop()
        for name in dbs.values():
            try:
                pg.drop(name)
            except Exception as exc:  # noqa: BLE001
                print(f"drop {name} failed: {exc}", file=sys.stderr)
    if mutant:
        # Existing declared differences cannot establish that a mutation ran.
        try:
            result = json.loads((out / f"mutant-{mutant}.json").read_text(encoding="utf-8"))
            caught = mutant_caught(code, result, baseline)
            count = result["summary"]["diffs"]
        except (OSError, ValueError, KeyError, TypeError):
            caught, count = False, 0
        print(f"mutant {mutant}: harness exit {code}, {count} diffs "
              f"({'caught' if caught else 'MISSED'})")
        return 0 if caught else 1
    return code


if __name__ == "__main__":
    sys.exit(main())
