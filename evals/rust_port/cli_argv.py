"""Compare OS-native invalid argv against selected Python, retaining every byte."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile

from .candidate import command_identity
from .harness import capture_platform, isolated_env, run_cli, write_new
from .provenance import ROOT
from .stdio_capture import require_phase1_source

CORPUS = Path(__file__).with_name("cli_argv_corpus.json")


def cases():
    return [case for case in json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
            if case["platform"] == os.name]


def native_argv(case):
    if case["platform"] != os.name:
        raise ValueError("argv case requires its native operating system")
    if os.name == "posix":
        return [bytes.fromhex(value) for value in case["argv_bytes_hex"]]
    return [b"".join(unit.to_bytes(2, "little") for unit in units)
            .decode("utf-16-le", errors="surrogatepass")
            for units in case["argv_utf16_units"]]


def expected_result(case):
    newline = "\r\n" if case["platform"] == "nt" else "\n"
    diagnostic = f"unknown mode {case['mode_repr']}; see: pseudolife-mcp --help{newline}"
    return {"exit_code": 2, "stdout_b64": "",
            "stderr_b64": base64.b64encode(diagnostic.encode("ascii")).decode("ascii")}


def compare_case(case, oracle, candidate):
    expected = expected_result(case)
    return [{"arm": arm, "path": "/" + key, "reason": "value"}
            for arm, observed in (("oracle", oracle), ("candidate", candidate))
            for key in expected if observed.get(key) != expected[key]]


def oracle_command(root):
    # The source is selected explicitly; cwd or an editable install cannot
    # silently replace it. Invalid dispatch does not import the daemon.
    code = ("import runpy,sys; "
            f"sys.path.insert(0, {str(Path(root).resolve())!r}); "
            "runpy.run_module('pseudolife_memory.cli',run_name='__main__')")
    return [sys.executable, "-c", code]


def run(root, candidate, out, public_out=None):
    root = Path(root).resolve()
    source = require_phase1_source(root)
    identity = command_identity(candidate)
    if identity["executable_sha256"] is None:
        raise ValueError("argv candidate must name an existing executable")
    rows = []
    with tempfile.TemporaryDirectory(prefix="cli-argv-") as private:
        for case in cases():
            observations = {}
            for arm, prefix in (("oracle", oracle_command(root)), ("candidate", candidate)):
                env = isolated_env(Path(private) / case["id"] / arm)
                observations[arm] = run_cli(prefix, native_argv(case), cwd=root, env=env, timeout=10)
            rows.append({"case": case, **observations,
                         "differences": compare_case(case, **observations)})
    if not rows:
        raise RuntimeError("CLI argv corpus has no cases for this platform")
    receipt = {"schema": 1, "policy": "cli-argv-exact-bytes", "normalizations": [],
               "status": "passed" if all(not row["differences"] for row in rows) else "difference",
               "capture_platform": capture_platform(), **source,
               "candidate": identity,
               "oracle_python": {"version": platform.python_version(),
                                 "executable_basename": Path(sys.executable).name,
                                 "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()},
               "argv_instrument_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                          for path in (Path(__file__), CORPUS)},
               "cases": rows}
    write_new(Path(out), receipt)
    if public_out is not None:
        write_new(Path(public_out), receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-root", type=Path, default=ROOT)
    parser.add_argument("--candidate-json", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--public-out", type=Path)
    args = parser.parse_args()
    receipt = run(args.oracle_root, json.loads(args.candidate_json), args.out, args.public_out)
    print(json.dumps({"status": receipt["status"], "cases": len(receipt["cases"])}), flush=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
