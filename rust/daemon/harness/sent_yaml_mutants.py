"""Catch four real source breaks in a disposable copy of the Rust workspace."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
MUTANTS = {
    "skip-tab-validation": ("if text.contains('\\t') {", "if false {"),
    "allow-unicode-anchors": ("b.is_ascii_alphanumeric() || b\"-_\".contains(&b)",
                              "b.is_ascii_alphanumeric() || b >= 128 || b\"-_\".contains(&b)"),
    "allow-dot-anchors": ("b\"-_\".contains(&b)", "b\"-_.\".contains(&b)"),
    "allow-plain-scalar-tabs": ("*style != TScalarStyle::Plain", "true"),
}
CASES = {"skip-tab-validation": 0, "allow-unicode-anchors": 9,
         "allow-dot-anchors": 7, "allow-plain-scalar-tabs": 5}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", required=True, type=Path)
    args = parser.parse_args()
    scratch = args.scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    source = scratch / "source"
    # Never replace or remove an existing source directory.
    shutil.copytree(ROOT / "rust", source, ignore=shutil.ignore_patterns("target"))
    config = source / "shim/src/sent_config.rs"
    original = config.read_text(encoding="utf-8")
    results = {}
    for name, (needle, replacement) in MUTANTS.items():
        if original.count(needle) != 1:
            raise RuntimeError("source mutant no longer has one target: " + name)
        config.write_text(original.replace(needle, replacement), encoding="utf-8")
        log = scratch / (name + ".unit.log")
        env = dict(os.environ)
        env.setdefault("CARGO_TARGET_DIR", str(scratch / "target"))
        with log.open("wb") as output:
            result = subprocess.run(
                ["cargo", "test", "--manifest-path", str(source / "Cargo.toml"),
                 "-p", "pseudolife-stdio", "--lib", "sent_config::tests", "--locked", "-j", "3"],
                env=env, stdout=output, stderr=subprocess.STDOUT, check=False,
            )
        text = log.read_text(encoding="utf-8", errors="replace")
        unit_caught = result.returncode == 101 and "test result: FAILED." in text
        with (scratch / (name + ".build.log")).open("wb") as output:
            build = subprocess.run(
                ["cargo", "build", "--manifest-path", str(source / "Cargo.toml"),
                 "-p", "pseudolife-stdio", "--bin", "pseudolife-stdio", "--locked", "-j", "3"],
                env=env, stdout=output, stderr=subprocess.STDOUT, check=False,
            )
        if build.returncode:
            raise RuntimeError("mutant executable did not compile: " + name)
        suffix = ".exe" if os.name == "nt" else ""
        env["PSEUDOLIFE_SENT_YAML_BIN"] = str(Path(env["CARGO_TARGET_DIR"]) / "debug" / ("pseudolife-stdio" + suffix))
        case = str(ROOT / "rust/daemon/harness/test_sent_yaml.py") + f"::test_scanner_refusals[refusal-{CASES[name]}]"
        with (scratch / (name + ".differential.log")).open("wb") as output:
            differential = subprocess.run([sys.executable, "-m", "pytest", case, "-q"],
                                          env=env, cwd=ROOT, stdout=output,
                                          stderr=subprocess.STDOUT, check=False)
        caught = unit_caught and differential.returncode == 1
        results[name] = {"unit_exit": result.returncode, "differential_exit": differential.returncode,
                         "case": "refusal-" + str(CASES[name]), "caught": caught}
        print(name + ": " + ("caught" if caught else "FAILED"), flush=True)
    config.write_text(original, encoding="utf-8")
    (scratch / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if all(row["caught"] for row in results.values()) else 1)


if __name__ == "__main__":
    main()
