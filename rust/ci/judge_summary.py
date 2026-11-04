"""Keep judge-phase diagnostics without changing the original command verdicts.

Exit codes decide the gate. Receipts add case identifiers and never expose
raw streams, environment values or private paths in the completion records.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

PHASES = ("oracle-setup", "lease-check", "wait-mail-tests", "lease-headers", "cli-argv",
          "phase1-judges", "phase2-dispatch", "cli-process")


def receipt_paths(checkout: Path, temporary: Path, platform: str) -> dict:
    public = checkout / "evals/results"
    return {
        "wait-mail-tests": [temporary / "junit/run-unchanged-candidates-and-differential-judges.xml"],
        "lease-headers": [temporary / "lease-header-policy.json"],
        "cli-argv": [public / f"rust-cli-argv-{platform}.json", temporary / "rust-cli-argv.json"],
        "phase1-judges": [public / f"rust-phase1-ci-{platform}.json", temporary / "rust-parity.json"],
        "phase2-dispatch": [public / f"rust-phase2-ci-{platform}.json"],
        "cli-process": [temporary / "phase2b-cli-process-public.json", temporary / "phase2b-cli-process.json"],
    }


def failed_cases(phase: str, payload: dict) -> list[str]:
    """Read explicit failed case/node IDs, excluding successful negative controls."""
    if not isinstance(payload, dict):
        raise ValueError("receipt must be an object")
    failed = set()

    def add(value):
        if isinstance(value, str) and value:
            failed.add(value)

    def differences(rows):
        for row in rows:
            add(row.get("case"))

    def outcomes(result):
        for row in result.get("outcomes", []):
            if row.get("outcome") != "passed":
                add(row.get("nodeid"))

    def controls(rows):
        for row in rows:
            if row.get("rejected") is False or row.get("correct_status") is False:
                add(row.get("case"))

    if phase == "lease-headers":
        for row in payload["cases"]:
            if row.get("passed") is not True or not all(row.get("controls_rejected", [])):
                add(row["case"].get("id"))
    elif phase == "cli-argv":
        for row in payload["cases"]:
            if row["differences"]:
                add(row["case"].get("id"))
    elif phase == "phase1-judges":
        differences(payload["differences"])
        outcomes(payload.get("process_tests", {}))
        for name, control in payload.get("judge_sensitivity_controls", {}).items():
            if control.get("rejected") is False:
                add(name)
        process = payload.get("process_controls", {})
        for name, control in process.get("controls", process).items():
            if control.get("rejected") is False:
                add(name)
        generic = payload.get("generic_controls", {})
        differences(generic.get("differences", []))
        for name, control in generic.get("graded_controls", {}).items():
            if control.get("rejected") is False or control.get("correct_status") is False:
                add(name)
    elif phase == "phase2-dispatch":
        differences(payload["python_self_replay"]["differences"])
        differences(payload["candidate_replay"]["differences"])
        controls(payload.get("judge_sensitivity_controls", []))
        for result in payload.get("process_tests", {}).values():
            outcomes(result)
    elif phase == "cli-process":
        for row in payload["cases"] if "cases" in payload else payload["records"]:
            if row.get("passed") is not True:
                add(row.get("id"))
        controls(payload.get("candidate_controls", payload.get("candidate_output_controls", [])))
    return sorted(failed)


def receipt_details(phase: str, paths: list[Path]) -> tuple[str | None, list[str], list[str]]:
    errors = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            if phase == "wait-mail-tests":
                root = ET.parse(path).getroot()
                failed = sorted({"::".join(filter(None, (node.get("classname"), node.get("name"))))
                                 for node in root.iter("testcase")
                                 if node.find("failure") is not None or node.find("error") is not None})
            else:
                failed = failed_cases(phase, json.loads(path.read_text(encoding="utf-8-sig")))
            return path.name, failed, errors
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as error:
            errors.append(f"{path.name}: unreadable case details ({type(error).__name__})")
    return None, [], errors or ["receipt was not produced"]


def summarize(manifest, checkout: Path, temporary: Path, platform: str) -> dict:
    def valid_seconds(value, code):
        if value is None:
            return code is None
        if type(value) not in (int, float) or value < 0:
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    rows, invalid, errors = {}, set(), []
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
            or manifest["schema"] != 1 or not isinstance(manifest.get("phases"), list)):
        errors.append("phase manifest is missing or unreadable")
    else:
        for row in manifest["phases"]:
            if not isinstance(row, dict) or row.get("name") not in PHASES:
                errors.append("phase manifest contains an unknown record")
                continue
            name, code, seconds = row["name"], row.get("exit_code"), row.get("seconds")
            if name in rows or code is not None and type(code) is not int or not valid_seconds(seconds, code):
                invalid.add(name)
                errors.append(f"{name}: invalid or duplicate completion record")
            rows[name] = row
    reports, first_failure = [], 0
    paths = receipt_paths(checkout, temporary, platform)
    for name in PHASES:
        row = rows.get(name)
        code = row.get("exit_code") if row else None
        if name in invalid:
            outcome, failure = "invalid", 1
        elif row is None:
            outcome, failure = ("unverified" if errors else "not_run"), 1
        elif code is None:
            outcome, failure = "incomplete", 1
        else:
            outcome, failure = ("passed", 0) if code == 0 else ("failed", code)
        if failure and not first_failure:
            first_failure = failure
        receipt, cases, issues = receipt_details(name, paths[name]) if name in paths and row else (None, [], [])
        if outcome == "failed" and name in paths and not cases:
            issues.append("phase failed without a recorded case identifier; inspect its job log")
        reports.append({"phase": name, "outcome": outcome, "exit_code": code if type(code) is int else None,
                        "seconds": row.get("seconds") if row and name not in invalid else None,
                        "receipt": receipt, "failed_cases": cases, "diagnostic_errors": issues})
    return {"schema": 1, "phases": reports, "manifest_errors": errors,
            "failed_phases": [row["phase"] for row in reports if row["outcome"] != "passed"],
            "exit_code": first_failure or int(bool(errors))}


def markdown(report: dict) -> str:
    def safe(value):
        return str(value).encode("utf-8", errors="backslashreplace").decode("utf-8").replace("|", "\\|").replace("\n", "\\n").replace("\r", "\\r")
    lines = ["### Judge phase results", "", "| Phase | Exit | Outcome | Failed case IDs | Diagnostics |",
             "| --- | --- | --- | --- | --- |"]
    for row in report["phases"]:
        lines.append("| " + " | ".join(safe(value) for value in (
            row["phase"], row["exit_code"] if row["exit_code"] is not None else "",
            row["outcome"], ", ".join(row["failed_cases"]), "; ".join(row["diagnostic_errors"]))) + " |")
    lines += ["", *report["manifest_errors"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--temporary", type=Path, required=True)
    parser.add_argument("--platform", choices=("Windows", "Linux", "macOS"), required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        manifest = None
    report = summarize(manifest, args.checkout, args.temporary, args.platform)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in [("summary", report), *((row["phase"], row) for row in report["phases"])]:
        (args.out_dir / f"{name}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=True, allow_nan=False) + "\n",
                                                encoding="utf-8")
    text = markdown(report)
    print(text)
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as output:
            output.write(text)
    return int(bool(report["exit_code"]))


if __name__ == "__main__":
    raise SystemExit(main())
