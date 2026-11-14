"""Select ordinary-PR golden rows from their Python and Rust source owners.

Unknown runtime paths widen the selection. Nothing here imports product code
or evaluates a row while the scope job decides which work is needed.
"""
from __future__ import annotations

import argparse
import ast
import functools
import json
import os
from pathlib import Path, PurePosixPath
import subprocess

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOTS = {
    "mail": ("briefing_cli", "coordination_adapter"),
    "hook": ("briefing_cli", "coordination_adapter"),
    "episode": ("episode_cli",),
    "lease": ("lease_cli",),
    "lease_operator": ("lease_cli",),
    "transfer": ("transfer_cli",),
    "test_login": ("test_login_cli",),
    "doctor": ("doctor_cli", "wake_liveness"),
    "connect": ("connect_cli",),
    "maintainer": ("maintainer_cli",),
    "maintainer_setup": ("maintainer_cli",),
    "coordination_recovery": ("coordination_recovery",),
    "invite": ("invite_cli",),
    "pair": ("pair_cli",),
    "move": ("move_cli",),
    "tunnel": ("tunnel_cli",),
    "console": ("console_cli",),
    "restore": ("restore_cli", "docker_backup_restore"),
    "conductor_watch": ("conductor_watch_cli",),
    "update": ("update_cli",),
    "audit": ("board_audit_cli",),
    "backup": ("backup_cli",),
    "migrate_flat": ("migrate_flat_cli",),
}
ROWS = tuple(PYTHON_ROOTS)
JUDGE_ROOTS = {
    "lease-headers": ("lease_cli", "os_lock", "daemon_exec"),
    "phase1-judges": ("shim", "mcp_server", "daemon"),
    "cli-process": ("lease_cli", "runtimes"),
    "wait-mail-tests": ("wait_mail_cli", "coordination_adapter"),
}
JUDGES = ("wait-mail-tests", "lease-headers", "cli-argv", "phase1-judges", "phase2-dispatch", "cli-process")
# These rows need database seeders or the packaged PostgreSQL fixture.
POSTGRES_ROWS = ("lease", "lease_operator", "transfer", "test_login", "maintainer", "invite",
                 "audit", "backup", "migrate_flat")


def import_graph(sources: dict[str, str]) -> dict[str, set[str]]:
    """Static first-party imports, including imports inside functions."""
    modules = {}
    for path in sources:
        parts = list(PurePosixPath(path).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    graph = {}
    for path, text in sources.items():
        package = list(PurePosixPath(path).parent.parts)
        imports = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = package[:len(package) - node.level + 1] if node.level else []
                base = ".".join([*prefix, *([node.module] if node.module else [])])
                imports.add(base)
                imports.update(f"{base}.{alias.name}" for alias in node.names)
        reached = set()
        for name in imports:
            parts = name.split(".")
            for size in range(1, len(parts) + 1):
                if target := modules.get(".".join(parts[:size])):
                    reached.add(target)
        graph[path] = reached
    return graph


def dependencies(graph: dict[str, set[str]], roots) -> set[str]:
    pending, found = list(roots), set()
    while pending:
        path = pending.pop()
        if path not in found:
            found.add(path)
            pending.extend(graph.get(path, ()))
    return found


@functools.lru_cache(maxsize=None)
def source_graph(root: Path) -> dict[str, set[str]]:
    sources = {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
               for path in (root / "pseudolife_memory").rglob("*.py")}
    return import_graph(sources)


@functools.lru_cache(maxsize=None)
def python_owners(root: Path) -> dict[str, tuple[str, ...]]:
    graph = source_graph(root)
    rows = {row: dependencies(graph, [f"pseudolife_memory/{name}.py" for name in names])
            for row, names in PYTHON_ROOTS.items()}
    return {path: tuple(row for row, paths in rows.items() if path in paths) for path in graph
            if any(path in paths for paths in rows.values())}


def judges_for(path: str, graph: dict[str, set[str]], owners: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    if path in ("pseudolife_memory/cli.py", "pyproject.toml"):
        return JUDGES
    if path.startswith("pseudolife_memory/"):
        groups = [group for group, roots in JUDGE_ROOTS.items()
                  if path in dependencies(graph, [f"pseudolife_memory/{name}.py" for name in roots])]
        return tuple(groups) if groups else (JUDGES if path not in owners else ())
    if path.startswith("rust/shim/"):
        from parity_scope import reached
        return JUDGES if "judges" in reached(path) else ()
    if path.startswith("rust/ci/ci_budget"):
        return ()
    if path.startswith(("rust/shared/", "evals/rust_port/", "evals/rust_baseline/", "rust/ci/")):
        return JUDGES
    if path in ("tests/test_cli_dispatch.py", "tests/conftest.py", "tests/pg_defaults.py", "tests/fake_embedder.py"):
        return JUDGES
    if path.startswith("tests/test_shim") or path.startswith("tests/test_wait_mail"):
        return ("phase1-judges", "wait-mail-tests")
    # Shared build inputs and unknown runtime paths reach every row; they
    # can also change the stdio proxy that the frozen groups exercise.
    return JUDGES if set(rows_for(path, owners)) == set(ROWS) else ()


def rows_for(path: str, owners: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    # Match the port's existing Rust command ownership map; unknown shared
    # shim code reaches every row, as it does in the full parity selector.
    from parity_scope import SHIM_COMMANDS, harness_imports, is_docs, row_of
    parts = PurePosixPath(path).parts
    if not parts or path in ("pyproject.toml", "rust/Cargo.toml", "rust/Cargo.lock"):
        return ROWS
    if path.startswith("pseudolife_memory/"):
        return owners.get(path, ROWS)
    if path.startswith("rust/cli_harness/"):
        if len(parts) == 4 and parts[2] in ("rows", "goldens", "specs"):
            row = row_of(parts[2], parts[3])
            if row in PYTHON_ROOTS:
                return (row,)
        return ROWS
    if path.startswith("rust/shim/"):
        if len(parts) > 4 and parts[2:4] == ("src", "cli"):
            return SHIM_COMMANDS.get(parts[4], ROWS)
        return ROWS
    if path.startswith("rust/daemon/") and not path.startswith("rust/daemon/harness/"):
        # This row invokes the sibling native daemon to migrate the bank.
        return ("migrate_flat",)
    if path.startswith("rust/ci/ci_budget"):
        return ()  # Numeric limits, approvals and their reader are not CLI inputs.
    if path.startswith(("rust/shared/", "rust/ci/")) or path == ".github/workflows/rust.yml":
        return ROWS
    if path.startswith("tests/"):
        return ROWS if parts[-1] == "__init__.py" or len(parts) > 1 and parts[1].removesuffix(".py") in harness_imports("tests") else ()
    if is_docs(path) or path.startswith(("frontend/", "changelog.d/", ".github/")):
        return ()
    # Other production and deployment inputs may be consumed by a CLI row.
    return ROWS


def windows_differs(root: Path, row: str) -> bool:
    """Only observational data decides equality; capture clocks are metadata.

    Different case sets, streams, files, modes or requests retain Windows.
    Missing/malformed records cannot establish equivalence.
    """
    try:
        values = []
        for platform in ("linux", "windows"):
            record = json.loads((root / f"rust/cli_harness/goldens/{row}.{platform}.json").read_text(encoding="utf-8"))
            cases = record["cases"]
            if not isinstance(cases, dict) or not cases or any(not isinstance(case, dict) for case in cases.values()):
                return True
            values.append({key: {name: value for name, value in case.items()
                                  if name not in ("home", "window", "utc_offset", "arm")}
                           for key, case in cases.items()})
        return values[0] != values[1]
    except (OSError, ValueError, KeyError, TypeError):
        return True


def select(files: list[str] | None, root: Path = ROOT) -> dict:
    """The rows whose committed goldens this PR replays.

    Since the Python freeze (14 November 2026) nothing here compares against
    a live Python oracle: the frozen judge probes left CI (``judges`` is
    always empty), and so did the rows whose only CI replay read a recording
    made from the oracle in the same run (parity_scope.UNREPLAYED_ROWS).
    """
    from parity_scope import UNREPLAYED_ROWS
    owners = python_owners(root)
    reached = set(ROWS) if files is None else {row for path in files for row in rows_for(path, owners)}
    linux = [row for row in ROWS if row in reached and row not in UNREPLAYED_ROWS]
    windows = [row for row in linux if windows_differs(root, row)]
    return {"schema": 1, "linux": linux, "windows": windows, "judges": []}


def changed_files() -> list[str] | None:
    try:
        result = subprocess.run(["git", "diff", "--no-renames", "--name-only", "HEAD^1", "HEAD"],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        return result.stdout.splitlines() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--github-output", type=Path, required=True)
    options = parser.parse_args(argv)
    value = select(changed_files())
    platform = "windows" if os.name == "nt" else "linux"
    with options.github_output.open("a", encoding="utf-8") as output:
        output.write(f"precheck={json.dumps(value, separators=(',', ':'))}\n")
        output.write(f"enabled={str(bool(value[platform] or value['judges'])).lower()}\n")
        output.write(f"golden={str(bool(value[platform])).lower()}\n")
        output.write(f"judges={str(bool(value['judges'])).lower()}\n")
        output.write(f"full_runtime={str(bool(value['judges']) or bool(set(value[platform]) & set(POSTGRES_ROWS))).lower()}\n")
        output.write(f"test_login={str('test_login' in value[platform]).lower()}\n")
    print(json.dumps(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
