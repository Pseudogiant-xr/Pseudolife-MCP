"""Run board lifecycle regressions on an owned disposable bank.

Usage from the harness directory: python -m board.boundary
The fixture deliberately requires the loopback test PostgreSQL on port 5434.
"""
from __future__ import annotations

import argparse
import os
import subprocess

from board.run import REPO, pg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clock", action="store_true", help="exercise failed clock seeding before send")
    args = parser.parse_args()
    test = ("board_send_refuses_an_incomplete_clock_seed" if args.clock
            else "board_heartbeat_during_paused_initialization")
    if pg.HOST_PORT != "127.0.0.1:5434":
        raise SystemExit("board boundary fixture requires test PostgreSQL at 127.0.0.1:5434")
    name = pg.name("pl_cf_w2f_init_boundary")
    try:
        dsn = pg.create(name)
        env = dict(os.environ, PL_W2F_BOARD_DSN=dsn)
        result = subprocess.run([
            "cargo", "test", "--locked", "--manifest-path", str(REPO / "rust" / "Cargo.toml"),
            "-p", "pseudolife-daemon", "-j", "4",
            f"service::tests::{test}",
            "--", "--exact", "--ignored", "--nocapture",
        ], env=env, check=False)
        return result.returncode
    finally:
        pg.drop(name)


if __name__ == "__main__":
    raise SystemExit(main())
