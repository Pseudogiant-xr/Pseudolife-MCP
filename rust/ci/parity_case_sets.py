"""Prove a startup-shard consolidation preserves each OS's case selections.

Compare bound run scripts, their environments and timeouts, while allowing
only the physical startup label and receipt filenames to change. The case
generators and fixtures must be unchanged, so equal selections imply equal
runtime case sets; CI-layout guard tests are outside those generators.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess

import yaml

from ci_budget import Evaluator, UNKNOWN, condition_text, truthy

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ".github/workflows/rust.yml"
SCOPE = "rust/ci/parity_scope.py"
GENERATORS = ("rust/cli_harness", "rust/daemon/harness", "evals/rust_port", "evals/rust_baseline", "pseudolife_memory", "tests")
RENAMED = {"cli-daemon": "cli-startup", "cli-daemon-2": "cli-startup"}
MATRIX_VALUE = re.compile(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8")


def assignments(source: str) -> dict:
    return {target.id: node.value for node in ast.parse(source).body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)}


def literal(values: dict, name: str):
    return ast.literal_eval(values[name])


def scope_data(before: str, after: str) -> tuple[dict, dict]:
    old, new = assignments(before), assignments(after)
    def structure(source):
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in
                    ("SUITES", "SHARDS", "FLOOR", "DAEMON") for target in node.targets):
                node.value = ast.Constant(None)
        return ast.dump(tree)
    if structure(before) != structure(after):
        raise ValueError("scope code, OS exclusions or unrelated routing data changed")
    for name in ("FLOOR", "DAEMON"):
        mapped = {RENAMED.get(item, item) for item in literal(old, name)}
        if mapped != set(literal(new, name)):
            raise ValueError(f"{name} changed beyond the startup grouping")
    mapped = list(dict.fromkeys(RENAMED.get(item, item) for item in literal(old, "SUITES")))
    if mapped != literal(new, "SUITES") or "cli-startup" in literal(old, "SUITES"):
        raise ValueError("suite membership changed beyond the startup grouping")
    remaining = [{entry["suite"]: entry for entry in literal(values, "SHARDS")
                  if entry["suite"] not in {*RENAMED, *RENAMED.values()}} for values in (old, new)]
    if remaining[0] != remaining[1]:
        raise ValueError("unrelated shard configuration changed")
    return old, new


def frames(workflow: dict, values: dict) -> list[dict]:
    matrix = workflow["jobs"]["parity-checks"]["strategy"]["matrix"]
    if matrix["suite"] != literal(values, "SUITE_EXPRESSION") or matrix["include"] != literal(values, "INCLUDE_EXPRESSION"):
        raise ValueError("unrecognized scope matrix")
    if matrix["exclude"] != literal(values, "EXCLUDE_EXPRESSION"):
        raise ValueError("unrecognized scope exclusions")
    systems = literal(values, "SYSTEMS")
    if matrix["os"] != list(systems):
        raise ValueError("OS matrix changed")
    # Identical static-exclusion code was checked; only current code is loaded.
    from parity_scope import STATIC_EXCLUDE
    excluded = {(entry["os"], entry["suite"]) for entry in STATIC_EXCLUDE}
    shards = {entry["suite"]: entry for entry in literal(values, "SHARDS")}
    return [{"os": os_name, "suite": suite, **shards.get(suite, {})}
            for os_name in systems for suite in literal(values, "SUITES") if (os_name, suite) not in excluded]


class Context(Evaluator):
    def __init__(self, matrix, environment):
        super().__init__("workflow_dispatch", [], set(), matrix)
        self.environment = environment

    def lookup(self, path):
        if path == ["runner", "os"]:
            return "Windows" if self.matrix["os"] == "windows-latest" else "Linux"
        if path == ["runner", "environment"]:
            return self.environment
        if path == ["inputs", "mode"]:
            return "ci"
        if path == ["steps", "startup_setup", "outcome"]:
            return "success"  # Successful selection proof; failure admission is tested separately.
        return super().lookup(path)


def bound(value, matrix):
    if isinstance(value, dict):
        return {key: bound(item, matrix) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    return MATRIX_VALUE.sub(lambda match: str(matrix.get(match.group(1), "")), value)


def canonical(script: str) -> str:
    output = r"(--out\s+(?:\(Join-Path\s+\$env:RUNNER_TEMP\s+)?['\"]?)daemon-startup-parity-part[12]\.json"
    return re.sub(output, r"\1daemon-startup-parity.json", script).strip()


def manifest(workflow: dict, plans: list[dict], environment="github-hosted") -> tuple[dict[str, set[str]], dict[str, list[int]]]:
    selected, parts = {}, {}
    job = workflow["jobs"]["parity-checks"]
    for matrix in plans:
        os_name = matrix["os"]
        selected.setdefault(os_name, set())
        parts.setdefault(os_name, [])
        context = Context(matrix, environment)
        for step in job["steps"]:
            if not step.get("run"):
                continue
            result = context.run(condition_text(step.get("if", "true")))
            if truthy(result) is False:
                continue
            script = bound(step["run"], matrix)
            if "python rust/daemon/harness/startup_ci.py " in script:
                found = re.findall(r"startup_ci\.py[^\n]* --part (\d+)\b", script)
                if len(found) != 1:
                    raise ValueError("startup selection is missing or unreadable")
                parts[os_name].append(int(found[0]))
            entry = {"run": canonical(script), "env": bound(step.get("env", {}), matrix),
                     "shell": step.get("shell"), "working-directory": step.get("working-directory"),
                     "timeout": step.get("timeout-minutes"), "continue-on-error": step.get("continue-on-error", False)}
            # Unknown predicates retain their identity rather than assuming they run.
            if result is UNKNOWN:
                entry["condition"] = canonical(bound(str(step.get("if", "")), matrix))
            selected[os_name].add(json.dumps(entry, sort_keys=True))
    return selected, parts


def compare(before_workflow: dict, before_scope: str, after_workflow: dict, after_scope: str) -> dict:
    old_jobs, new_jobs = before_workflow["jobs"], after_workflow["jobs"]
    if old_jobs.keys() != new_jobs.keys():
        raise ValueError("workflow job identities changed")
    for name in old_jobs:
        if {key: value for key, value in old_jobs[name].items() if key != "steps"} != {
                key: value for key, value in new_jobs[name].items() if key != "steps"}:
            raise ValueError(f"job configuration changed: {name}")
        if old_jobs[name].get("steps") != new_jobs[name].get("steps") and (
                old_jobs[name].get("steps") != old_jobs["parity-checks"]["steps"]
                or new_jobs[name].get("steps") != new_jobs["parity-checks"]["steps"]):
            raise ValueError(f"unrelated job execution changed: {name}")
    old, new = scope_data(before_scope, after_scope)
    before_plans, after_plans = frames(before_workflow, old), frames(after_workflow, new)
    environments = {}
    for environment in ("github-hosted", "self-hosted"):
        before_sets, before_parts = manifest(before_workflow, before_plans, environment)
        after_sets, after_parts = manifest(after_workflow, after_plans, environment)
        if before_parts != after_parts or any(sorted(parts) != [1, 2] for parts in after_parts.values()):
            raise ValueError("startup parts dropped or duplicated")
        for os_name in before_sets:
            if before_sets[os_name] != after_sets.get(os_name):
                removed = before_sets[os_name] - after_sets.get(os_name, set())
                added = after_sets.get(os_name, set()) - before_sets[os_name]
                raise ValueError(f"{environment}/{os_name}: selections changed ({len(removed)} removed, {len(added)} added)")
        environments[environment] = {"startup_parts": after_parts,
            "selection_counts": {os_name: len(values) for os_name, values in before_sets.items()},
            "selection_sha256": {os_name: hashlib.sha256("\n".join(sorted(values)).encode()).hexdigest()
                                 for os_name, values in before_sets.items()}}
    return {"before_jobs": len(before_plans), "after_jobs": len(after_plans), "environments": environments}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, help="reviewed parent ref or full commit ID")
    options = parser.parse_args(argv)
    before = git("rev-parse", "--verify", "--end-of-options", options.before + "^{commit}").strip()
    changed = git("diff", "--name-only", "--no-renames", before, "--", *GENERATORS).splitlines()
    changed += git("ls-files", "--others", "--exclude-standard", "--", *GENERATORS).splitlines()
    if changed:
        parser.error("case generators or fixtures changed: " + ", ".join(changed))
    try:
        report = compare(yaml.safe_load(git("show", before + ":" + WORKFLOW)), git("show", before + ":" + SCOPE),
                         yaml.safe_load((ROOT / WORKFLOW).read_text(encoding="utf-8")),
                         (ROOT / SCOPE).read_text(encoding="utf-8"))
    except (KeyError, TypeError, ValueError, StopIteration) as error:
        parser.error(str(error))
    print(json.dumps({"before": before, "case_generators_unchanged": True, **report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
