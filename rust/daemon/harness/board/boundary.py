"""Run board lifecycle regressions on an owned disposable bank.

Usage from the harness directory: python -m board.boundary
The fixture deliberately requires the loopback test PostgreSQL on port 5434.
``--shared-clock`` initializes the full service, so it also writes the offline
embedding fixture and its config (``embedding_fixture``, ``writer_session_cases``).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

from board.run import REPO, pg


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--clock", action="store_true", help="exercise failed clock seeding before send")
    group.add_argument("--json-keys", action="store_true", help="exercise reserved JSON keys in capabilities")
    group.add_argument("--shared-clock", action="store_true",
                       help="board sends on the service clock, interleaved with memory ticks")
    group.add_argument("--handover", action="store_true",
                       help="a send while the bank changes hands waits for the reseed")
    args = parser.parse_args()
    if args.json_keys:
        test = "service::tests::board_json_capabilities_roundtrip"
    elif args.clock:
        raise SystemExit("--clock retired: the board ticks the service clock; run --shared-clock")
    elif args.shared_clock:
        test = "board::clock_tests::board_sends_tick_the_service_clock"
    elif args.handover:
        test = "board::clock_tests::a_send_during_a_bank_handover_stamps_past_the_other_writer"
    else:
        test = "service::tests::board_heartbeat_during_paused_initialization"
    if pg.HOST_PORT != "127.0.0.1:5434":
        raise SystemExit("board boundary fixture requires test PostgreSQL at 127.0.0.1:5434")
    name = pg.name("pl_cf_w2f_init_boundary")
    try:
        dsn = pg.create(name)
        env = dict(os.environ, PL_W2F_BOARD_DSN=dsn)
        with tempfile.TemporaryDirectory(prefix="pl-w2f-boundary-") as scratch:
            if args.shared_clock or args.handover:
                env.update(_fixture_service(Path(scratch)))
            result = subprocess.run([
                "cargo", "test", "--locked", "--manifest-path", str(REPO / "rust" / "Cargo.toml"),
                "-p", "pseudolife-daemon", "-j", "4", test,
                "--", "--exact", "--ignored", "--nocapture",
            ], env=env, check=False)
        return result.returncode
    finally:
        pg.drop(name)


# The writer-session fixture's service config (writer_session_cases.CONFIG).
CONFIG = """embedding:
  model_name: fixture-board-clock
  backend: onnx
  device: cpu
  cpu_dtype: fp32
memory:
  embedding_dim: 4
  miras:
    preset: flat
"""


def runtime_library() -> Path:
    import onnxruntime
    root = Path(onnxruntime.__file__).parent / "capi"
    if os.name == "nt":
        return root / "onnxruntime.dll"
    return next(root.glob("libonnxruntime.so.*"))


def _fixture_service(root: Path) -> dict[str, str]:
    """A full service with no real model: the 4-dimension fixture graph."""
    from embedding_fixture import create
    create(root / "model")
    config = root / "data" / "config.yaml"
    config.parent.mkdir()
    config.write_text(CONFIG, encoding="utf-8")
    return {"PL_W2F_BOARD_CONFIG": str(config), "PSEUDOLIFE_DAEMON_ONNX_DIR": str(root / "model"),
            "ORT_DYLIB_PATH": str(runtime_library()), "OMP_NUM_THREADS": "1"}


if __name__ == "__main__":
    raise SystemExit(main())
