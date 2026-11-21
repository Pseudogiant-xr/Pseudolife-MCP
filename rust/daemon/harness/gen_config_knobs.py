"""Run the native Console knob golden comparison with a daemon test binary.

Build one with `cargo test -p pseudolife-daemon --bin pseudolife-daemon --no-run`
from rust/. The native test owns observation and deliberate recording.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

TARGET = Path(__file__).resolve().parent / "goldens" / "config-knobs.json"
RECORD_TEST = "config_io::tests::record_config_knobs"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-test-bin", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="check the golden (the default)")
    args = parser.parse_args(argv)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PSEUDOLIFE_", "PL_CONFIG_KNOBS_"))}
    result = subprocess.run([str(args.rust_test_bin.resolve()), "--exact", RECORD_TEST],
                            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(result.stdout, end="")
    print(result.stderr, file=sys.stderr, end="")
    if result.returncode == 0 and f"test {RECORD_TEST} ... ok" not in result.stdout:
        print(f"candidate did not run {RECORD_TEST}", file=sys.stderr)
        return 1
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
