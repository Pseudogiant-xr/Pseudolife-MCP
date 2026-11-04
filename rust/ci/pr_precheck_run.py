"""Replay selected goldens and frozen judges before an ordinary PR is batched."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from judge_summary import receipt_details
from pr_precheck import JUDGES, ROOT, ROWS

sys.path.insert(0, str(ROOT))

WAIT_MAIL_TESTS = (
    "evals/rust_port/test_cli_wait_mail.py", "evals/rust_port/test_wait_mail_phase2d_native.py",
    "evals/rust_port/test_wait_mail_reduction_native.py", "evals/rust_port/test_wait_mail_producer_native.py",
    "evals/rust_port/test_wait_mail_candidate_contract.py", "evals/rust_port/test_wait_mail_measurement.py",
    "evals/rust_port/test_wait_mail_listener_events.py",
)
BOOTSTRAP = """import pathlib, subprocess, sys
from evals.rust_port.provenance import module_command
root = pathlib.Path(sys.argv[1]).resolve()
raise SystemExit(subprocess.run(module_command(sys.argv[2], root, sys.argv[3:]), cwd=root).returncode)
"""
PROCESS_BOOTSTRAP = """import os, pathlib, subprocess, sys, tempfile
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pseudolife_memory.storage.embedded_pg import attach_or_start
from evals.rust_port.provenance import module_command
root = pathlib.Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix="cli-precheck-postgres-") as directory:
    dsn, owned = attach_or_start(pathlib.Path(directory))
    if owned is None:
        raise RuntimeError("fresh corpus PostgreSQL unexpectedly attached")
    try:
        options = conninfo_to_dict(dsn)
        options["dbname"] = "postgres"
        env = dict(os.environ, PSEUDOLIFE_BENCH_ADMIN_URL=make_conninfo(**options))
        command = module_command("evals.rust_port.cli_process", root, sys.argv[2:])
        result = subprocess.run(command, cwd=root, env=env)
    finally:
        owned.stop()
raise SystemExit(result.returncode)
"""


def plan_value(text: str) -> dict:
    value = json.loads(text)
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("invalid precheck schema")
    for key, allowed in (("linux", ROWS), ("windows", ROWS), ("judges", JUDGES)):
        selected = value.get(key)
        if (not isinstance(selected, list) or any(not isinstance(item, str) or item not in allowed for item in selected)
                or len(selected) != len(set(selected))):
            raise ValueError("invalid precheck selection")
    if not set(value["windows"]) <= set(value["linux"]):
        raise ValueError("Windows selection has no corresponding Linux row")
    return value


def golden_command(row: str, candidate: Path, receipt: Path) -> list[str]:
    return [sys.executable, "rust/cli_harness", "--row", row, "--golden", "--candidate", str(candidate),
            "--out", str(receipt)]


def recorded_actions(row: str) -> list[dict]:
    from coverage_plan import SUITES
    actions = [action for suite in SUITES.values() for action in suite
               if action.get("kind") == "cli" and action.get("row") == row and action["mode"] in ("record", "golden")]
    if actions and [action["mode"] for action in actions] != ["record", "golden"]:
        raise ValueError("recorded row needs one ordered record/golden pair")
    return actions


def row_commands(row: str, candidate: Path, private: Path) -> list[tuple[str, list[str], Path | None]]:
    from run_coverage import command
    actions = recorded_actions(row)
    if not actions:
        receipt = private / f"golden-{row}.json"
        return [(f"golden/{row}", golden_command(row, candidate, receipt), receipt)]
    result = []
    for action in actions:
        mode = action["mode"]
        receipt = private / f"{mode}-{row}.json"
        result.append((f"{mode}/{row}", command(action, candidate.parent, candidate.parent, receipt),
                       receipt if mode == "golden" else None))
    return result


@contextmanager
def golden_postgres(private: Path):
    """Use the same owned provider and test-role provisioning as parity."""
    from evals.rust_port.phase1_ci import postgres
    from pseudolife_memory.test_login_cli import main as test_login_main
    from psycopg.conninfo import conninfo_to_dict
    with postgres():
        admin = os.environ["PSEUDOLIFE_BENCH_ADMIN_URL"]
        login = private / "test-pg.env"
        configured = os.environ.pop("PSEUDOLIFE_MCP_DATABASE_URL", None)
        try:
            code = test_login_main(["create", "--rotate", "--file", str(login), "--admin-url", admin], out=io.StringIO())
        finally:
            if configured is not None:
                os.environ["PSEUDOLIFE_MCP_DATABASE_URL"] = configured
        if code:
            raise RuntimeError("owned golden test-role provisioning failed")
        options = conninfo_to_dict(admin)
        env = dict(os.environ, PSEUDOLIFE_TEST_PG_HOST_PORT=f"{options['host']}:{options['port']}",
                   PSEUDOLIFE_TEST_PG_LOGIN_FILE=str(login))
        env.pop("PSEUDOLIFE_MCP_DATABASE_URL", None)
        yield env


def judge_command(phase: str, python: Path, source: Path, candidate: Path, private: Path,
                  checked_at: str) -> tuple[list[str], Path | None]:
    prefix = [str(python), "-c", BOOTSTRAP, str(source)]
    command = json.dumps([str(candidate)])
    common = ["--oracle-root", str(source), "--candidate-json", command, "--candidate-root", str(ROOT)]
    resource = ["--offline-resource-checked-at", checked_at]
    receipt = private / f"{phase}.json"
    public = private / f"{phase}-public.json"
    if phase == "wait-mail-tests":
        receipt = private / "wait-mail-tests.xml"
        return [str(python), "-m", "pytest", "-q", *WAIT_MAIL_TESTS, "-ra", "--junitxml",
                str(receipt)], receipt
    if phase == "lease-headers":
        return [str(python), "-m", "evals.rust_port.lease_headers", "--root", str(ROOT),
                "--candidate", str(candidate), "--out", str(receipt)], receipt
    if phase == "cli-argv":
        return [str(python), "-m", "evals.rust_port.cli_argv", "--oracle-root", str(source),
                "--candidate-json", command, "--out", str(receipt), "--public-out", str(public)], public
    if phase == "phase1-judges":
        return [*prefix, "evals.rust_port.phase1_ci", *common, "--out", str(receipt),
                "--public-out", str(public), *resource], public
    if phase == "phase2-dispatch":
        return [*prefix, "evals.rust_port.cli_dispatch", *common, "--evidence-directory",
                str(private / "phase2-tests"), "--out", str(receipt), *resource], receipt
    if phase == "cli-process":
        return [str(python), "-c", PROCESS_BOOTSTRAP, str(source), *common, "--out", str(receipt),
                "--public-out", str(public), "--modes", "help", "version", "lease", *resource], public
    raise ValueError("unknown judge phase")


def case_ids(phase: str, receipt: Path | None) -> tuple[list[str], list[str]]:
    if receipt is None:
        return [], []
    if not phase.startswith("golden/"):
        _, ids, errors = receipt_details(phase, [receipt])
        return ids, errors
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8-sig"))
        rows = payload["rows"]
        if payload.get("complete") is not True or not isinstance(rows, dict):
            raise ValueError("incomplete golden receipt")
        return sorted(f"{row}/{case}" for row, record in rows.items() for case in record["diffs"]), []
    except (OSError, ValueError, KeyError, TypeError):
        return [], ["case receipt unavailable or incomplete"]


def execute(phase: str, command: list[str], private: Path, receipt: Path | None,
            *, cwd: Path = ROOT, env: dict | None = None) -> dict:
    started = time.monotonic()
    error = []
    try:
        with (private / f"{phase.replace('/', '-')}.log").open("w", encoding="utf-8") as log:
            code = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
    except OSError as failure:
        code, error = 1, [f"command could not start ({type(failure).__name__})"]
    ids, diagnostics = case_ids(phase, receipt)
    result = {"phase": phase, "exit_code": code, "seconds": round(time.monotonic() - started, 3),
              "failed_cases": ids, "diagnostic_errors": error + diagnostics}
    print(json.dumps(result), flush=True)
    return result


def write_summary(directory: Path, records: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    summary = {"schema": 1, "phases": records, "complete": all(type(row["exit_code"]) is int for row in records),
               "failed": [row["phase"] for row in records if row["exit_code"]]}
    temporary = directory / "summary.tmp"
    temporary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    temporary.replace(directory / "summary.json")


def run(plan: dict, candidate: Path, private: Path, public: Path) -> int:
    private.mkdir(parents=True, exist_ok=False)
    platform = "windows" if os.name == "nt" else "linux"
    fixture_rows = [row for row in plan[platform] if recorded_actions(row) or row == "test_login"]
    direct = [row for row in plan[platform] if row not in fixture_rows]
    recipes = {row: row_commands(row, candidate, private) for row in plan[platform]}
    phases = [phase for row in direct for phase, _, _ in recipes[row]]
    if fixture_rows:
        phases += ["golden-lease-check", "golden-postgres", *[phase for row in fixture_rows for phase, _, _ in recipes[row]],
                   "golden-postgres-cleanup"]
    if plan["judges"]:
        phases += ["oracle-setup", "lease-check", *plan["judges"]]
    records = [{"phase": phase, "exit_code": None, "seconds": None, "failed_cases": [],
                "diagnostic_errors": ["phase not started"]} for phase in phases]
    by_name = {row["phase"]: row for row in records}
    write_summary(public, records)

    def stage(phase, command, receipt=None, **kwargs):
        by_name[phase]["diagnostic_errors"] = []
        write_summary(public, records)
        result = execute(phase, command, private, receipt, **kwargs)
        by_name[phase].update(result)
        write_summary(public, records)
        return result["exit_code"]

    def rows(selected, env=None):
        for row in selected:
            for phase, command, receipt in recipes[row]:
                code = stage(phase, command, receipt, env=env)
                if phase.startswith("record/") and code:
                    break  # A fresh golden does not exist when its recording failed.

    rows(direct)
    if fixture_rows:
        if stage("golden-lease-check", [sys.executable, "-m", "pseudolife_memory.cli", "lease", "check", "full-suite"]):
            return 1
        setup, cleanup = by_name["golden-postgres"], by_name["golden-postgres-cleanup"]
        setup["diagnostic_errors"] = []
        started, cleanup_started = time.monotonic(), None
        try:
            with golden_postgres(private) as env:
                setup.update(exit_code=0, seconds=round(time.monotonic() - started, 3))
                write_summary(public, records)
                rows(fixture_rows, env)
                cleanup_started = time.monotonic()
        except Exception as error:
            if setup["exit_code"] is None:
                setup.update(exit_code=1, seconds=round(time.monotonic() - started, 3),
                             diagnostic_errors=[f"owned PostgreSQL setup failed ({type(error).__name__})"])
            else:
                cleanup.update(exit_code=1, seconds=None,
                               diagnostic_errors=[f"owned PostgreSQL scope failed ({type(error).__name__})"])
        else:
            cleanup.update(exit_code=0, seconds=round(time.monotonic() - cleanup_started, 3), diagnostic_errors=[])
        write_summary(public, records)
    if plan["judges"]:
        oracle = private / "oracle"
        if stage("oracle-setup", [sys.executable, "-m", "evals.rust_port.phase1_oracle", "--destination", str(oracle)]):
            return 1
        if stage("lease-check", [sys.executable, "-m", "pseudolife_memory.cli", "lease", "check", "full-suite"]):
            return 1
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        python = oracle / ("runtime/Scripts/python.exe" if os.name == "nt" else "runtime/bin/python")
        source = oracle / "source"
        env = dict(os.environ, PSEUDOLIFE_MCP_PYTHON=str(python), PSEUDOLIFE_RUST_BINARY=str(candidate),
                   PSEUDOLIFE_WAIT_MAIL_ORACLE_ROOT=str(source), PSEUDOLIFE_WAIT_MAIL_NATIVE_REQUIRED="1")
        env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
        for phase in plan["judges"]:
            command, receipt = judge_command(phase, python, source, candidate, private, checked_at)
            # The existing wait-mail and lease-header instruments run from
            # the head checkout; the other bootstrap commands bind the source.
            stage(phase, command, receipt, env=env)
    write_summary(public, records)
    return int(any(row["exit_code"] != 0 for row in records))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--public", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        plan = plan_value(options.plan_json)
    except (ValueError, TypeError) as error:
        parser.error(str(error))
    candidate = options.candidate.resolve(strict=True)
    return run(plan, candidate, options.private.resolve(), options.public.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
