"""Capture bounded wait-mail pairs against the frozen master Python oracle."""
import argparse
import copy
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile

ALLOWED = frozenset({"wait-mail-long-ring-watermark", "wait-mail-bad-ring-reason", "wait-mail-unicode-delivery"})
# Historical receipts retain this identity; new captures select frozen_head.
WAIT_PIN = "eb0c13e9c5036aa2b95e7fccb77f41ca1c095493"


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, timeout=10).decode().strip()


def validate_selector(mode, case_list, frozen_head):
    if mode != "wait-mail":
        raise ValueError("mode must be wait-mail")
    names = case_list.split(",")
    if not names or len(names) > 3 or len(set(names)) != len(names) or not set(names) <= ALLOWED:
        raise ValueError("case-list must select one to three distinct supported wait-mail cases")
    if not re.fullmatch("[0-9a-f]{40}", frozen_head):
        raise ValueError("frozen-head must be a full lowercase commit ID")
    return names


def require_frozen_checkout(root, frozen_head):
    if git(root, "rev-parse", "HEAD") != frozen_head or git(root, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("capture requires the exact clean frozen checkout")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("wait-mail",), required=True)
    parser.add_argument("--case-list", required=True)
    parser.add_argument("--frozen-head", required=True)
    parser.add_argument("--oracle-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        args.case_ids = validate_selector(args.mode, args.case_list, args.frozen_head)
    except ValueError as error:
        parser.error(str(error))
    return args


def main():
    args = parse_args()
    names = args.case_ids
    root, candidate_root = args.oracle_root.resolve(), args.candidate_root.resolve()
    require_frozen_checkout(candidate_root, args.frozen_head)
    from evals.rust_port.cli_process import cli_binding, paired_cases, reset_home
    from evals.rust_port.cli_wait_mail import cases
    from evals.rust_port.harness import capture_platform, write_new
    from evals.rust_port.phase1_receipts import candidate_identity, command_identity
    from evals.rust_port.provenance import require_import_root, runtime_metadata
    from evals.rust_port.stdio_capture import require_phase1_source

    require_import_root(root)
    source = require_phase1_source(root)
    binary = args.candidate.resolve(strict=True)
    commands = {"oracle": [sys.executable, "-m", "pseudolife_memory.cli"], "candidate": [str(binary)]}
    identity = candidate_identity(commands["candidate"], candidate_root)
    oracle_identity = command_identity(commands["oracle"], root)
    if identity["public_cli_module"] or identity["executable_sha256"] == oracle_identity["executable_sha256"]:
        raise RuntimeError("candidate must be a distinct native executable")
    helpers = {"evals/rust_port/hosted_wait_mail_capture.py": [Path(__file__), main, parse_args,
               validate_selector, require_frozen_checkout, git],
               "evals/rust_port/cli_wait_mail.py": [cases]}
    for name in ("wait_mail_policy.py", "wait_mail_measurement.py", "wait_mail_candidate_contract.py",
                 "wait_mail_candidate_expectations.json"):
        helpers["evals/rust_port/" + name] = [candidate_root / "evals/rust_port" / name]
    binding = cli_binding(candidate_root, root, extra_helpers=helpers)
    all_cases = {case["id"]: case for case in cases()}
    selected = [all_cases[name] for name in names]
    original = copy.deepcopy(selected)
    result = None
    cleanup = {"fixture_home_absent": False, "port_reservation_released": False}
    try:
        with tempfile.TemporaryDirectory(prefix="hosted-wait-mail-") as directory:
            home = Path(directory) / "home"
            try:
                # Reserve an unused local port without listening or starting a daemon.
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 29871))
                    captures = []
                    for case in selected:
                        observed = paired_cases([case], commands, root=root, home=home, url="http://127.0.0.1:29871")
                        write_new(args.out.with_name(case["id"] + ".json"),
                                  {"oracle_head": source["oracle_head"], **observed})
                        captures.append(observed)
                    result = {"records": [record for value in captures for record in value["records"]],
                              "candidate_output_controls": [control for value in captures for control in value["candidate_output_controls"]],
                              "passed": all(value["passed"] for value in captures)}
                cleanup["port_reservation_released"] = True
            finally:
                reset_home(home)
                cleanup["fixture_home_absent"] = not home.exists()
    finally:
        write_new(args.out.with_name("cleanup.json"), {"oracle_head": source["oracle_head"], **cleanup})
    if selected != original or candidate_identity(commands["candidate"], candidate_root) != identity:
        raise RuntimeError("capture source input or candidate changed")
    require_frozen_checkout(candidate_root, args.frozen_head)
    if command_identity(commands["oracle"], root) != oracle_identity or require_phase1_source(root) != source:
        raise RuntimeError("capture oracle changed")
    if cli_binding(candidate_root, root, extra_helpers=helpers) != binding:
        raise RuntimeError("capture instrument changed")
    if len(result["records"]) != len(names) or len(result["candidate_output_controls"]) != 4 * len(names):
        raise RuntimeError("capture is incomplete")
    write_new(args.out, {"schema": 1, "frozen_head": args.frozen_head, "mode": args.mode,
        "case_ids": names, "capture_platform": capture_platform(), "capture_runtime": runtime_metadata(root),
        "oracle_head": source["oracle_head"], "oracle_source": source,
        "wait_mail_module_pin": source["oracle_head"], "cli_instrument_binding": binding,
        "candidate_identity": identity, "oracle_identity": oracle_identity, "cleanup": cleanup, **result})
    print(json.dumps({"passed": result["passed"], "cases": len(names), "receipt": args.out.name}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
