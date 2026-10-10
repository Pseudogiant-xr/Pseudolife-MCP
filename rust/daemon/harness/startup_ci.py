"""Run startup hydration parity on CI's prepared disposable PostgreSQL fixture.

The caller supplies the test-login environment and may set CARGO_TARGET_DIR.
This helper checks generated schema inputs, runs schema and startup fixture unit tests,
builds the native daemon test executable, and checks startup goldens and mutants.
It does not provision PostgreSQL or load an embedding model.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from schema_ci import PACKAGE, test_artifact

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def writer_guard(binary: Path, env: dict, output: Path) -> int:
    """A canceled writer cannot absorb the next hydration stamp writes."""
    sys.path.insert(0, str(REPO))
    os.environ["PL_HARNESS_SLICE"] = "pgs"
    import pgdisposable as pg
    if pg.SLICE != "pgs":
        raise ValueError("writer guard requires the pgs fixture slice")
    result = {}
    for variant in ("control", "mutant"):
        name = pg.name(pg.PREFIX + str(time.time_ns()))
        dsn = pg.create(name)
        try:
            child = dict(env, PL_PGS_WRITER_GUARD_DSN=dsn)
            child.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
            if variant == "mutant":
                child["PSEUDOLIFE_DAEMON_MUTANT"] = "startup-skip-writer-guard"
            run = subprocess.run([str(binary), "--exact",
                "bank::startup_tests::db_seating_recovers_an_abandoned_writer_transaction", "--nocapture"],
                cwd=REPO, env=child, capture_output=True, text=True, timeout=60)
            accepted = (run.returncode == 0 and "1 passed" in run.stdout if variant == "control" else
                        run.returncode != 0 and "seating inherited the abandoned transaction" in run.stderr
                        and '("retired", 1)' in run.stderr and "1 failed" in run.stdout)
            result[variant] = {"exit_code": run.returncode, "accepted": accepted}
        finally:
            pg.drop(name)
    name = pg.PREFIX + "mutation_" + str(time.time_ns())
    dsn = pg.create(name)
    try:
        child = dict(env, PL_PGS_MUTATION_DSN=dsn, PL_PGS_CASE="zero-timestamp-seating")
        child.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
        run = subprocess.run([str(binary), "--exact",
            "bank::startup_tests::db_hydration_preserves_rows_vectors_and_seating_stamps", "--nocapture"],
            cwd=REPO, env=child, capture_output=True, text=True, timeout=60)
        accepted = run.returncode == 0 and "1 passed" in run.stdout
        result["hydration_rows"] = {"exit_code": run.returncode, "accepted": accepted}
        if not accepted:
            print(run.stdout, end="")
            print(run.stderr, end="", file=sys.stderr)
    finally:
        pg.drop(name)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"writer guard: {'0 diffs / mutant caught' if all(v['accepted'] for v in result.values()) else 'FAIL'}", flush=True)
    return int(not all(value["accepted"] for value in result.values()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="startup evidence JSON path")
    parser.add_argument("--no-build", action="store_true", help="use an existing daemon test executable")
    parser.add_argument("--rust-test-bin", type=Path, help="test executable required with --no-build")
    parser.add_argument("--record-goldens", action="store_true", help="record oracle fixtures, then check replay")
    args = parser.parse_args(argv)
    if args.no_build != (args.rust_test_bin is not None):
        parser.error("--no-build and --rust-test-bin must be supplied together")

    env = dict(os.environ, PL_HARNESS_SLICE="pgs", CUDA_VISIBLE_DEVICES="-1",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        for command in (
            [sys.executable, str(HERE / "gen_schema_sql.py"), "--check"],
            [sys.executable, "-m", "unittest", "discover", "-s", str(HERE), "-p", "test_schema_*.py"],
            [sys.executable, "-m", "unittest", "discover", "-s", str(HERE), "-p", "test_startup_*.py"],
        ):
            code = subprocess.run(command, cwd=REPO, env=env).returncode
            if code:
                return code

        if args.no_build:
            binary = args.rust_test_bin.resolve()
        else:
            build = subprocess.run(
                ["cargo", "test", "--locked", "--manifest-path", "Cargo.toml", "-p", PACKAGE,
                 "--no-run", "--message-format=json", "--features", "mutants", "-j", "3"],
                cwd=REPO / "rust", env=env, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            )
            if build.returncode:
                for line in build.stdout.splitlines():
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    rendered = message.get("message", {}).get("rendered")
                    if rendered:
                        print(rendered, end="", file=sys.stderr)
                return build.returncode
            binary = test_artifact(build.stdout)
        if not binary.is_file():
            raise ValueError("daemon test executable does not exist")

        output = args.out.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(HERE / "startup_cases.py"), "--rust-test-bin", str(binary)]
        if args.record_goldens:
            code = subprocess.run(
                [*command, "--record-goldens", "--mutants", "--out", str(output)],
                cwd=REPO, env=env,
            ).returncode
            if code:
                return code
            output = output.with_name(output.stem + "-replay" + output.suffix)
            flags = ["--check-goldens"]
        else:
            flags = ["--check-goldens", "--mutants"]
        code = subprocess.run([*command, *flags, "--out", str(output)], cwd=REPO, env=env).returncode
        if code:
            return code
        retry_output = args.out.resolve().with_name(args.out.stem + "-clock-retry" + args.out.suffix)
        retry = [sys.executable, str(HERE / "clock_retry_cases.py"), "--rust-test-bin", str(binary)]
        if args.record_goldens:
            code = subprocess.run([*retry, "--record-goldens", "--mutants", "--out", str(retry_output)],
                                  cwd=REPO, env=env).returncode
            if code:
                return code
            retry_output = retry_output.with_name(retry_output.stem + "-replay" + retry_output.suffix)
            retry_flags = ["--check-goldens"]
        else:
            retry_flags = ["--check-goldens", "--mutants"]
        code = subprocess.run([*retry, *retry_flags, "--out", str(retry_output)], cwd=REPO, env=env).returncode
        if code:
            return code
        return writer_guard(binary, env, args.out.resolve().with_name(args.out.stem + "-writer-guard" + args.out.suffix))
    except ValueError as error:
        print(f"startup CI refused: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"startup CI could not run ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
