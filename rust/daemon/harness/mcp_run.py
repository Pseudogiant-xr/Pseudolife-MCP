"""Run the MCP-surface harness end to end on disposable banks.

usage: python mcp_run.py <rust-binary> <out-dir> [--mutant NAME] [--tokenless]
                         [--record <golden.json>]

Creates pl_cf_w2g_py and pl_cf_w2g_rs through the test login, starts the
Python oracle (from this checkout) and the Rust daemon on them with the
harness environment (mcp_compare.py's docstring), runs `mcp_compare.py
compare`, stops both daemons by PID and drops the banks. `--mutant NAME`
sets PSEUDOLIFE_DAEMON_MUTANT for the Rust side (a `--features mutants`
build) and inverts the verdict: the run passes only if the harness goes red.
Exit 0 means the expected verdict.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daemons  # noqa: E402

DISPOSABLE = re.compile(r"pl_cf_w2g_[a-z0-9_]{1,40}")
HOST_PORT = os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT", "127.0.0.1:5433")
LOGIN_FILE = Path(os.environ.get("PSEUDOLIFE_TEST_PG_LOGIN_FILE")
                  or Path.home() / ".pseudolife-mcp" / "test-pg.env")
ENV = {
    "PSEUDOLIFE_MCP_TOKEN": "tok-default-0001",
    "PSEUDOLIFE_MCP_TOKENS": "tok-alice-0001:alice,tok-bob-0001:bob,tok-carol-0001:carol",
    "PSEUDOLIFE_MCP_TIER_MAP": "alice:minimal,bob:core,writer-m:minimal",
    "PSEUDOLIFE_MCP_TOOLSET": "core",
}


def login():
    values = {}
    for line in LOGIN_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values["PSEUDOLIFE_TEST_PG_USER"], values["PSEUDOLIFE_TEST_PG_PASSWORD"]


def admin(statement):
    import psycopg
    user, password = login()
    host, port = HOST_PORT.rsplit(":", 1)
    with psycopg.connect(host=host, port=int(port), user=user, password=password,
                         dbname="postgres", autocommit=True, connect_timeout=10) as c:
        c.execute(statement)


def fresh(name):
    assert DISPOSABLE.fullmatch(name), name
    admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin(f'CREATE DATABASE "{name}"')
    user, password = login()
    host, port = HOST_PORT.rsplit(":", 1)
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{name}"


def drop(name):
    assert DISPOSABLE.fullmatch(name), name
    admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def main():
    args = sys.argv[1:]
    binary, out = Path(args[0]), Path(args[1])
    mutant = args[args.index("--mutant") + 1] if "--mutant" in args else None
    record = args[args.index("--record") + 1] if "--record" in args else None
    env = dict(ENV)
    if "--tokenless" in args:
        env = {"PSEUDOLIFE_MCP_TIER_MAP": ENV["PSEUDOLIFE_MCP_TIER_MAP"],
               "PSEUDOLIFE_MCP_TOOLSET": "core"}
    out.mkdir(parents=True, exist_ok=True)
    root = daemons.scratch_root() / "w2g"
    started = []
    try:
        py_home = daemons.make_home(root, "py", None)
        rs_home = daemons.make_home(root, "rs", None)
        py_port, rs_port = daemons.free_port(), daemons.free_port()
        py_env = daemons.base_env(py_home, dict(env, PSEUDOLIFE_MCP_DATABASE_URL=fresh("pl_cf_w2g_py")))
        rs_extra = dict(env, PSEUDOLIFE_MCP_DATABASE_URL=fresh("pl_cf_w2g_rs"))
        if mutant:
            rs_extra["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
        rs_env = daemons.base_env(rs_home, rs_extra)
        py = daemons.python_daemon(py_home, py_port, py_env).start(wait_s=300)
        started.append(py)
        rs = daemons.rust_daemon(binary, rs_home, rs_port, rs_env).start(wait_s=60)
        started.append(rs)
        if record:
            cmd = [sys.executable, str(HERE / "mcp_compare.py"), "record", str(py_port), record]
        else:
            cmd = [sys.executable, str(HERE / "mcp_compare.py"), "compare", str(py_port),
                   str(rs_port), str(out / ("mutant-" + mutant + ".json" if mutant else "compare.json"))]
        if "--tokenless" in args:
            cmd.append("--tokenless")
        code = subprocess.run(cmd).returncode
    finally:
        for d in reversed(started):
            d.stop()
        for name in ("pl_cf_w2g_py", "pl_cf_w2g_rs"):
            try:
                drop(name)
            except Exception as exc:  # noqa: BLE001
                print(f"drop {name} failed: {exc}", file=sys.stderr)
    if mutant:
        print(f"mutant {mutant}: harness exit {code} ({'caught' if code == 1 else 'MISSED'})")
        return 0 if code == 1 else 1
    return code


if __name__ == "__main__":
    sys.exit(main())
