"""Select Rust CI inputs; other Python behavior is checked by nightly parity.

Keep the workflow itself active so its aggregate check can report success for
unrelated changes. PRs compare the tested merge tree with its base parent;
pushes compare the whole pushed range. Missing history runs the checks.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess


PREFIXES = (
    "rust/", "evals/rust_port/", "evals/rust_baseline/", "frontend/",
    "pseudolife_memory/web/static/", "plugin/hooks/", "ops/",
)
EXACT_INPUTS = {
    ".github/workflows/rust.yml", ".dockerignore", "pyproject.toml",
    "pseudolife_memory/storage/schema.py", "pseudolife_memory/mcp_server.py",
    "pseudolife_memory/web/api.py", "pseudolife_memory/web/routes.py",
    "pseudolife_memory/coordination.py", "pseudolife_memory/utils/config.py",
    "evals/claude_shim.py", "evals/codex_shim.py",
    "tests/conftest.py", "tests/fake_embedder.py", "tests/suite_lock.py",
    "tests/pg_defaults.py", "tests/pg_fixtures.py", "tests/asgi_helpers.py",
    "tests/test_transfer_cli.py", "tests/test_coordination_audit.py",
    "tests/maintainer_authenticator.py", "tests/test_cli_dispatch.py",
    "evals/memory_policy_bench.py", "evals/memory_policy_daemon.py",
}
WORKFLOW_INPUTS = {
    ".github/workflows/rust.yml", "rust/ci/scope.py", "rust/ci/inventory.py",
    "rust/ci/test_scope.py", "rust/ci/test_workflow.py", "rust/ci/test_h5_inventory.py",
}


def relevant(path: str) -> bool:
    return path in EXACT_INPUTS or path.startswith(PREFIXES)


def workflow_input(path: str) -> bool:
    return path in WORKFLOW_INPUTS


def classify(paths: list[str]) -> str:
    if not paths:
        return "full"
    # Only the workflow and its own contract tests may use this lane. Any
    # additional code, fixture or dependency change requires runtime parity.
    if any(workflow_input(path) for path in paths) and all(
        workflow_input(path) or path == "CHANGELOG.md" or path.startswith("changelog.d/")
        for path in paths
    ):
        return "contracts"
    return "full" if any(relevant(path) for path in paths) else "none"


def select_lane(root: Path, event: str, base: str, head: str) -> str:
    if event not in ("pull_request", "push") or not base or set(base) == {"0"}:
        return "full"
    try:
        # No rename detection: moving a Rust input out of scope must still run.
        paths = subprocess.check_output(
            ["git", "diff", "--no-renames", "--name-only", "-z", base, head],
            cwd=root, timeout=60).decode("utf-8").split("\0")
    except (subprocess.SubprocessError, OSError, UnicodeError):
        print("Cannot determine changed inputs; running Rust CI")
        return "full"
    paths = [path for path in paths if path]
    return classify(paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    base = "HEAD^1" if args.event == "pull_request" else args.base
    lane = select_lane(Path.cwd(), args.event, base, args.head)
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"needed={str(lane == 'full').lower()}\nlane={lane}\n")
    print(f"Rust CI lane: {lane}")


if __name__ == "__main__":
    main()
