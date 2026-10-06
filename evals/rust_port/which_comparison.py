"""Compare the removed registry crate with the included production resolver."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tomllib


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True, help="Private command/compiler log")
    parser.add_argument("--target-dir", type=Path, required=True, help="Existing reusable Cargo target")
    parser.add_argument("--resolver-reference-head", required=True, help="Git reference verified by the caller; not the instrument execution head")
    parser.add_argument("--smoke", action="store_true")
    arguments = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    crate = Path(__file__).with_suffix("")
    manifest = crate / "Cargo.toml"
    environment = dict(os.environ, CARGO_INCREMENTAL="0", CARGO_TARGET_DIR=str(arguments.target_dir.resolve()))
    sources = [Path(__file__).resolve(), manifest, crate / "Cargo.lock", crate / "src/main.rs", repo / "rust/shim/src/lifecycle_executable.rs"]
    hashes = {path.relative_to(repo).as_posix(): sha256(path) for path in sources}
    lock = tomllib.loads((crate / "Cargo.lock").read_text())
    package = next(package for package in lock["package"] if package["name"] == "which")
    assert package["version"] == "8.0.6"
    assert package["checksum"] == "bae2f2b2b816647a1cab1acc91f5bd20812d53cb344382635ec2181940c8034f"
    command = ["cargo", "+1.94.0", "metadata", "--manifest-path", str(manifest), "--offline", "--locked", "--format-version", "1"]
    metadata = json.loads(subprocess.check_output(command, env=environment))
    oracle = next(package for package in metadata["packages"] if package["name"] == "which")
    assert oracle["version"] == "8.0.6" and oracle["source"].startswith("registry+")
    oracle_root = Path(oracle["manifest_path"]).parent
    checksum = json.loads((oracle_root / ".cargo-checksum.json").read_text())
    assert checksum["package"] == package["checksum"]
    oracle_hashes = {}
    for name, expected in checksum["files"].items():
        actual = sha256(oracle_root / name)
        assert actual == expected, name
        oracle_hashes[name] = actual
    build = ["cargo", "+1.94.0", "build", "--manifest-path", str(manifest), "--offline", "--locked", "-j2"]
    with arguments.log.open("x", encoding="utf-8") as log:
        log.write("START registry which 8.0.6 versus production resolver\n" + json.dumps(build) + "\n")
        log.flush()
        print(f"first output: {arguments.log}", flush=True)
        result = subprocess.run(build, env=environment, stdout=log, stderr=subprocess.STDOUT)
        log.write(f"actual build exit: {result.returncode}\n")
        log.flush()
        if result.returncode:
            raise SystemExit(result.returncode)
        binary = arguments.target_dir.resolve() / "debug" / ("which-resolver-comparison.exe" if os.name == "nt" else "which-resolver-comparison")
        run = subprocess.run([str(binary), *(["--smoke"] if arguments.smoke else [])], capture_output=True)
        log.write(f"actual comparison exit: {run.returncode}\n")
        log.write(run.stdout.decode("utf-8", errors="replace") + run.stderr.decode("utf-8", errors="replace"))
        if run.returncode:
            raise SystemExit(run.returncode)
    receipt = json.loads(run.stdout)
    for path in sources:
        assert sha256(path) == hashes[path.relative_to(repo).as_posix()]
    receipt.update({
        "resolver_reference_head": arguments.resolver_reference_head,
        "reference_note": "Caller-verified resolver Git reference; instrument source identified by exact hashes, not claimed committed at this reference.",
        "source_sha256": hashes,
        "oracle_registry_checksum": package["checksum"],
        "oracle_verified_file_sha256": oracle_hashes,
        "comparison_binary_sha256": sha256(binary),
        "build_exit": result.returncode,
        "comparison_exit": run.returncode,
        "public_api_equal_cases": sum(row["relation"] == "public_api_equal" for row in receipt["rows"]),
        "intentional_owned_config_differences": sum(row["relation"] == "intentional_owned_config_difference" for row in receipt["rows"]),
        "unavailable_cases": [row for row in receipt["rows"] if row["relation"] == "unavailable"],
        "scope": "Ordinary lookup paths/acceptance, not error-string/type equivalence or exhaustive platform coverage; no full suite, service or installation.",
    })
    with arguments.output.open("x", encoding="utf-8") as output:
        json.dump(receipt, output, indent=2)
        output.write("\n")
    print(f"result: {arguments.output}; equal cases: {receipt['public_api_equal_cases']}; owned-config differences: {receipt['intentional_owned_config_differences']}; unavailable: {len(receipt['unavailable_cases'])}", flush=True)


if __name__ == "__main__":
    main()
