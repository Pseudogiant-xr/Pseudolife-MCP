"""Run the MCP-surface harness end to end on disposable banks.

usage: python mcp_run.py <rust-binary> <out-dir> [--mutant NAME --baseline <compare.json>]
                         [--tokenless | --config2] [--record <golden.json>] [--mount-probe] [--validation-sweep | --validation-coercions]

Creates pl_cf_w2g_py and pl_cf_w2g_rs through the test login, starts the
Python oracle (from this checkout) and the Rust daemon on them with the
harness environment (mcp_compare.py's docstring), runs `mcp_compare.py
compare`, stops both daemons by PID and drops the banks. `--mutant NAME`
sets PSEUDOLIFE_DAEMON_MUTANT for the Rust side (a `--features mutants`
build) and requires an ordinary baseline report: the run passes only if the
strict comparison finds changed or additional differences beyond that baseline.
Exit 0 means the expected verdict.

Each invocation retains its unique directory under PL_HARNESS_SCRATCH for
diagnostics, including after success. These directories accumulate; callers
may remove their own completed runs after collecting the evidence they need.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
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


# Bound on waiting for each daemon to create public.principals. A fixed 3 s
# pause after /health was too short twice on the Windows dev VM (2026-10-18,
# freshly built binaries); 60 s matches the Rust daemon's start bound.
PRINCIPALS_WAIT_S = 60.0


def wait_for_principals(banks, timeout_s=PRINCIPALS_WAIT_S, poll_s=0.25):
    """Poll until every bank (side name -> DSN) has public.principals.

    Raises RuntimeError naming each side still without the table at the
    deadline, which main() reports as an infrastructure failure."""
    import psycopg
    deadline = time.monotonic() + timeout_s
    pending = dict(banks)
    while True:
        for side, dsn in list(pending.items()):
            with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
                if conn.execute("SELECT to_regclass('public.principals')").fetchone()[0] is not None:
                    del pending[side]
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"public.principals never appeared in the {' and '.join(pending)} "
                               f"bank {timeout_s:g} s after both daemons answered /health")
        time.sleep(poll_s)


def seed_stored(dsn):
    import psycopg
    from pseudolife_memory.principal_store import create_invite, redeem
    from pseudolife_memory.principals import secret_sha256
    with psycopg.connect(dsn, autocommit=True) as conn:
        for name, (token, tier) in STORED.items():
            invite = create_invite(conn, name, tier=tier, ttl_seconds=600, replace=False)
            row = redeem(dsn, secret_sha256(invite["code"]), secret_sha256(token))
            assert row is not None and row.principal == name, (name, row)


def expected_summary(args):
    from mcp_compare import UNSERVED
    if "--config2" in args:
        return {"responses": 10, "cases": 2, "unserved_checked": []}
    if "--tokenless" in args:
        return {"responses": 30, "cases": 3, "unserved_checked": []}
    return {"responses": 325, "cases": 18, "unserved_checked": sorted(UNSERVED)}


def report_complete(report, expected):
    try:
        summary, diffs = report["summary"], report["diffs"]
        if any(summary[key] != expected[key] for key in ("responses", "cases", "unserved_checked")):
            return False
        if summary["diffs"] != len(diffs) or not all(isinstance(d, str) for d in diffs):
            return False
        return not any('{"exception":' in d or d.startswith(("stub calls raised:", "sanity "))
                       or re.fullmatch(r"[^:]+: \d+ vs \d+ responses", d) for d in diffs)
    except (KeyError, TypeError):
        return False


def mutant_caught(code, result, baseline):
    return (code == 1 and report_complete(result, baseline["summary"])
            and result["summary"]["diffs"] > 0
            and any(diff not in baseline["diffs"] for diff in result["diffs"]))


def main():
    args = sys.argv[1:]
    binary, out = Path(args[0]).resolve(), Path(args[1]).resolve()
    mutant = args[args.index("--mutant") + 1] if "--mutant" in args else None
    baseline = None
    if mutant:
        if "--baseline" not in args:
            print("--mutant requires --baseline from an ordinary comparison", file=sys.stderr)
            return 2
        try:
            baseline = json.loads(Path(args[args.index("--baseline") + 1]).read_text(encoding="utf-8"))
            if not report_complete(baseline, expected_summary(args)):
                raise ValueError("baseline comparison is incomplete or contains execution failures")
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            print(f"cannot load mutant baseline: {exc}", file=sys.stderr)
            return 2
    record = args[args.index("--record") + 1] if "--record" in args else None
    validation_flags = [flag for flag in ('--validation-sweep', '--validation-coercions') if flag in args]
    if validation_flags and (len(validation_flags) != 1 or mutant or record or '--tokenless' in args or '--config2' in args):
        raise ValueError('validation sweep requires the ordinary bearer configuration and one mode')
    if validation_flags:
        from mcp_validation_schema import source_versions
        source_versions()
    env = dict(ENV)
    if "--tokenless" in args:
        env = {"PSEUDOLIFE_MCP_TIER_MAP": ENV["PSEUDOLIFE_MCP_TIER_MAP"],
               "PSEUDOLIFE_MCP_TOOLSET": "core"}
    if "--config2" in args:
        env = dict(ENV, PSEUDOLIFE_WRITER_ID="writer-m", PSEUDOLIFE_MCP_TOOLSET=" Bogus ",
                   PSEUDOLIFE_MCP_TIER_MAP=" Alice : Minimal ,bad,:core,writer-m:minimal,bob:huge,"
                                           "bob:core, x:y:z")
    out.mkdir(parents=True, exist_ok=True)
    if record:
        Path(record).unlink(missing_ok=True)
    else:
        (out / (f"mutant-{mutant}.json" if mutant else "compare.json")).unlink(missing_ok=True)
    if "--mount-probe" in args:
        (out / "foundation-mount.json").unlink(missing_ok=True)
    if validation_flags:
        (out / "validation-sweep.json").unlink(missing_ok=True)
    root = Path(tempfile.mkdtemp(prefix="w2g-", dir=daemons.scratch_root()))
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
        py = daemons.python_daemon(py_home, py_port, py_env)
        started.append(py)
        py.start(wait_s=300)
        rs = daemons.rust_daemon(binary, rs_home, rs_port, rs_env)
        started.append(rs)
        rs.start(wait_s=60)
        if "--tokenless" not in args and "--config2" not in args:
            # Seed once both daemons have created the principals table, then
            # outwait both 10 s snapshot refreshes.
            wait_for_principals({"python": py_dsn, "rust": rs_dsn})
            seed_stored(py_dsn)
            seed_stored(rs_dsn)
            time.sleep(13)
        if "--mount-probe" in args:
            from run import Tokens, bearer, call, compare_case
            case = next(c for c in Tokens().cases() if c["name"] == "mcp path, auth")
            case["headers"] = [bearer(ENV["PSEUDOLIFE_MCP_TOKEN"])]
            case["expected_status"] = 406
            replies = [call(port, case["method"], case["path"], case["headers"], case["body"])
                       for port in (py_port, rs_port)]
            mount = compare_case(case, *replies)
            (out / "foundation-mount.json").write_text(json.dumps(mount, indent=2) + "\n", encoding="utf-8")
            if mount["diffs"]:
                raise RuntimeError("authenticated foundation MCP mount differs")
        if record:
            cmd = [sys.executable, str(HERE / "mcp_compare.py"), "record", str(py_port), record]
        else:
            cmd = [sys.executable, str(HERE / "mcp_compare.py"), "compare", str(py_port),
                   str(rs_port), str(out / ("mutant-" + mutant + ".json" if mutant else "compare.json"))]
        for flag in ("--tokenless", "--config2"):
            if flag in args:
                cmd.append(flag)
        code = subprocess.run(cmd).returncode
        if validation_flags and code in (0, 1):
            from mcp_validation_sweep import ValidationMismatch, run as validation_sweep
            try:
                validation_sweep(py_port, rs_port, out / 'validation-sweep.json',
                                 coercions_only='--validation-coercions' in args)
            except ValidationMismatch as exc:
                print(f'MCP validation parity failed: {exc}', file=sys.stderr)
                code = 1
    except Exception as exc:  # noqa: BLE001 — infrastructure is never a parity verdict.
        print(f"MCP harness infrastructure failed: {exc}", file=sys.stderr)
        code = 2
    finally:
        for d in reversed(started):
            try:
                d.stop()
            except Exception as exc:  # noqa: BLE001 — attempt every owned cleanup.
                print(f"daemon stop failed: {exc}", file=sys.stderr)
                code = 2
        for name in dbs.values():
            try:
                pg.drop(name)
            except Exception as exc:  # noqa: BLE001
                print(f"drop {name} failed: {exc}", file=sys.stderr)
                code = 2
    if code not in (0, 1):
        return 2
    if record:
        from mcp_compare import recording_errors
        try:
            if code != 0:
                return 2
            recorded = json.loads(Path(record).read_text(encoding='utf-8'))
            if errors := recording_errors(recorded):
                raise ValueError('; '.join(errors))
        except (OSError, ValueError, TypeError) as exc:
            print(f'MCP recording failed: {exc}', file=sys.stderr)
            return 2
    else:
        report_file = out / (f"mutant-{mutant}.json" if mutant else "compare.json")
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
            if not report_complete(report, expected_summary(args)):
                raise ValueError("comparison is incomplete or contains execution failures")
        except (OSError, ValueError, TypeError) as exc:
            print(f"MCP comparison failed: {exc}", file=sys.stderr)
            return 2
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
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — setup failures are infrastructure errors too.
        print(f"MCP harness setup failed: {exc}", file=sys.stderr)
        sys.exit(2)
