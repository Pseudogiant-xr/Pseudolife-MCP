"""Run schema parity using CI's provisioned disposable PostgreSQL fixture.

The caller supplies the test-login environment from ``lease_ci.py`` and may
set ``CARGO_TARGET_DIR``. No PostgreSQL service is provisioned here.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PACKAGE = "pseudolife-daemon"


def test_artifact(output: str) -> Path:
    artifacts = []
    for line in output.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        if message.get("reason") == "compiler-message":
            rendered = message.get("message", {}).get("rendered")
            if rendered:
                print(rendered, end="", file=sys.stderr)
        if message.get("reason") != "compiler-artifact":
            continue
        package_id = message.get("package_id", "")
        # Cargo emits a package-id URL; older Cargo uses "name version (url)".
        package_name = (package_id.rsplit("#", 1)[1].split("@", 1)[0]
                        if "#" in package_id else package_id.split(" ", 1)[0])
        if package_name != PACKAGE or message.get("profile", {}).get("test") is not True:
            continue
        executable = message.get("executable")
        if executable:
            artifacts.append(Path(executable))
    if len(artifacts) != 1:
        raise ValueError(f"expected one {PACKAGE} test executable, found {len(artifacts)}")
    return artifacts[0].resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="schema evidence JSON path")
    parser.add_argument("--no-build", action="store_true", help="use an existing daemon test executable")
    parser.add_argument("--rust-test-bin", type=Path, help="test executable required with --no-build")
    args = parser.parse_args(argv)
    if args.no_build != (args.rust_test_bin is not None):
        parser.error("--no-build and --rust-test-bin must be supplied together")

    env = dict(os.environ, PL_HARNESS_SLICE="pgs")
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        checks = [
            [sys.executable, str(HERE / "gen_schema_sql.py"), "--check"],
            [sys.executable, "-m", "unittest", "discover", "-s", str(HERE),
             "-p", "test_schema_*.py"],
        ]
        for command in checks:
            code = subprocess.run(command, cwd=REPO, env=env).returncode
            if code:
                return code

        if args.no_build:
            binary = args.rust_test_bin.resolve()
        else:
            build = subprocess.run(
                ["cargo", "test", "--manifest-path", "rust/Cargo.toml", "-p", PACKAGE,
                 "--no-run", "--message-format=json", "--features", "mutants", "-j", "3"],
                cwd=REPO, env=env, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            )
            if build.returncode:
                # Keep Cargo's failure code, including when the JSON stream is partial.
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
        return subprocess.run(
            [sys.executable, str(HERE / "run.py"), "schema", "--rust-test-bin", str(binary),
             "--all-versions", "--refusals", "--mutants", "--out", str(output)],
            cwd=REPO, env=env,
        ).returncode
    except ValueError as exc:
        print(f"schema CI refused: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"schema CI could not run ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
