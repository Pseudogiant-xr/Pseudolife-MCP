"""Bind the phase 0b controls and platform captures into one acceptance receipt."""
import argparse
import hashlib
import json
from pathlib import Path

from .controls import EXPECTED_REASONS
from .provenance import ORACLE_HEAD, ORACLE_SCHEMA, ROOT
from .harness import write_new


def evidence(path):
    path = Path(path).resolve()
    return {"path": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def require_platform(receipt, expected):
    platform = receipt.get("capture_platform")
    if not isinstance(platform, dict) or platform.get("os") != expected or not platform.get("architecture"):
        raise ValueError("required capture platform absent or incorrect")


def summarize(full_bank, linux_selfcheck, windows_cli):
    for receipt, system in ((full_bank, "Linux"), (linux_selfcheck, "Linux"), (windows_cli, "Windows")):
        require_platform(receipt, system)
        environment = receipt["environment"]
        if environment.get("source_head") != ORACLE_HEAD or environment.get("source_schema") != ORACLE_SCHEMA:
            raise ValueError("receipt does not name the pinned current oracle")
    if (full_bank["status"] != "passed" or full_bank["differences"] or full_bank["cases"] != 25
            or full_bank["candidate"]["kind"] != "owned-command"
            or full_bank["url_candidate_validation"] != "passed"):
        raise ValueError("full-bank candidate lane did not pass")
    controls = full_bank["graded_controls"]
    if set(controls) != set(EXPECTED_REASONS):
        raise ValueError("required graded control missing")
    matrix = []
    for control, reason in EXPECTED_REASONS.items():
        observed = controls[control]
        if not observed["rejected"] or not observed["correct_status"] or reason not in {d["reason"] for d in observed["differences"]}:
            raise ValueError("graded control expected difference not observed")
        matrix.append({"control": control, "expected_difference": reason,
                       "observed_differences": observed["differences"], "rejected": True})
    for receipt in (linux_selfcheck, windows_cli):
        if not receipt["python_control"]["passed"] or receipt["python_control"]["differences"]:
            raise ValueError("same-platform Python replay did not pass")
        if receipt["broken_rust"]["passed"] or {d["case"] for d in receipt["broken_rust"]["differences"]} != {"cli-help", "cli-unknown"}:
            raise ValueError("compiled garbage Rust candidate was not rejected")
    return {"schema": 1, "status": "passed", "oracle_commit": ORACLE_HEAD, "oracle_schema": ORACLE_SCHEMA,
            "python_full_bank": {"cases": 25, "differences": [], "owned_command": "passed", "external_url": "passed"},
            "graded_controls": matrix,
            "same_platform_replays": {"linux_selfcheck": linux_selfcheck["python_control"],
                                      "windows_cli": windows_cli["python_control"]},
            "garbage_rust": {"rejected": True, "linux": linux_selfcheck["broken_rust"],
                             "windows": windows_cli["broken_rust"],
                             "source_sha256": linux_selfcheck["negative_control"]["source_sha256"]},
            "scope": "instrument acceptance on synthetic disposable data; no production Rust implementation parity claim"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-bank", type=Path, required=True)
    parser.add_argument("--linux-selfcheck", type=Path, required=True)
    parser.add_argument("--windows-cli", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    inputs = {"full_bank": args.full_bank, "linux_selfcheck": args.linux_selfcheck, "windows_cli": args.windows_cli}
    result = summarize(*(json.loads(path.read_text()) for path in inputs.values()))
    result["evidence"] = {name: evidence(path) for name, path in inputs.items()}
    write_new(args.out, result)
    print(json.dumps({"status": result["status"], "controls": len(result["graded_controls"])}))


if __name__ == "__main__":
    main()
