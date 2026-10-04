"""Distinguish historical oracle evidence from the checkout a process imports."""
from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import importlib.util
from pathlib import Path
import platform
import subprocess
import sys

from evals.rust_port.processes import execution_sources

ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_HEAD = "136a34ae95e981a691fcc31ba9fb4f35d83d4249"
HISTORICAL_SCHEMA = 52
ORACLE_HEAD = "3691f5cb75487d3fda54a6bde6fab35dcf32c681"
ORACLE_SCHEMA = 53
SOURCE_PATHS = ("pseudolife_memory", "pyproject.toml", "tests/pg_defaults.py",
                "tests/test_cli_dispatch.py", "tests/conftest.py")


def schema_version(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "SCHEMA_META_VERSION"
                      for target in node.targets)]
    if len(values) != 1 or not isinstance(values[0], ast.Constant) or type(values[0].value) is not int:
        raise ValueError("schema version must be one literal integer")
    return values[0].value


def _git(root, *arguments):
    return subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, timeout=10)


def source_metadata(root=ROOT, *, oracle_head=ORACLE_HEAD, oracle_schema=ORACLE_SCHEMA):
    head = _git(root, "rev-parse", "HEAD")
    difference = _git(root, "diff", "--quiet", HISTORICAL_HEAD, "--", *SOURCE_PATHS)
    current_difference = _git(root, "diff", "--quiet", oracle_head, "--", *SOURCE_PATHS)
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", *SOURCE_PATHS)
    if head.returncode or current_difference.returncode not in (0, 1) or untracked.returncode:
        raise RuntimeError("historical oracle source comparison unavailable")
    instrument = _git(ROOT, "rev-parse", "HEAD")
    instrument_status = _git(ROOT, "status", "--porcelain", "--", "evals/rust_port", "evals/rust_baseline")
    if instrument.returncode or instrument_status.returncode:
        raise RuntimeError("instrument source provenance unavailable")
    return {"source_head": head.stdout.strip(), "instrument_head": instrument.stdout.strip(),
            "instrument_dirty": bool(instrument_status.stdout.strip()),
            "instrument_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in sorted([*Path(__file__).parent.glob("*.py"),
                                                      *Path(__file__).parent.glob("*.json")])},
            "process_helper_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in execution_sources()},
            "source_schema": schema_version(Path(root) / "pseudolife_memory/storage/schema.py"),
            "historical_oracle": {"source_head": HISTORICAL_HEAD, "source_schema": HISTORICAL_SCHEMA},
            "historical_source_comparison_available": difference.returncode in (0, 1),
            "phase_oracle": {"source_head": oracle_head, "source_schema": oracle_schema},
            "production_source_matches_phase_oracle": current_difference.returncode == 0 and not untracked.stdout.strip(),
            "production_source_matches_historical": difference.returncode == 0 and not untracked.stdout.strip(),
            "source_comparison_paths": list(SOURCE_PATHS)}


def require_historical_source(root=ROOT):
    metadata = source_metadata(root)
    if metadata["source_schema"] != HISTORICAL_SCHEMA or not metadata["production_source_matches_historical"]:
        raise RuntimeError("historical schema-52 oracle source is required; use a checkout of " + HISTORICAL_HEAD
                           + " selected with --oracle-root; current-source rebaselining is not enabled")
    return metadata


def require_oracle_source(root=ROOT):
    metadata = source_metadata(root)
    if metadata["source_schema"] != ORACLE_SCHEMA or not metadata["production_source_matches_phase_oracle"]:
        raise RuntimeError("phase oracle source and schema required; select the pinned checkout with --oracle-root")
    return metadata


def module_command(module, root, arguments=()):
    """Run current eval code with production imports and cwd in the selected tree."""
    root = Path(root).resolve()
    code = ("import sys,runpy,types,importlib.machinery; "
            f"sys.path[:0]=[{str(root)!r},{str(ROOT)!r}]; "
            "evals=types.ModuleType('evals'); "
            f"evals.__path__=[{str(ROOT / 'evals')!r}]; "
            "evals.__package__='evals'; "
            "evals.__spec__=importlib.machinery.ModuleSpec('evals',loader=None,is_package=True); "
            "evals.__spec__.submodule_search_locations=evals.__path__; "
            "sys.modules['evals']=evals; "
            f"sys.argv={[module, *map(str, arguments)]!r}; "
            f"runpy.run_module({module!r},run_name='__main__')")
    return [sys.executable, "-c", code]


def require_import_root(root):
    specification = importlib.util.find_spec("pseudolife_memory")
    if specification is None or specification.origin is None or Path(specification.origin).resolve().parent != Path(root).resolve() / "pseudolife_memory":
        raise RuntimeError("production import root does not match the selected oracle source")


def runtime_metadata(source_root):
    """Observe this process's interpreter, source and installed distributions."""
    import pseudolife_memory
    source = Path(pseudolife_memory.__file__).resolve()
    expected = Path(source_root).resolve() / "pseudolife_memory" / "__init__.py"
    versions = {}
    for name in ("pseudolife-mcp", "mcp", "httpx", "torch", "psycopg", "sentence-transformers"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    executable = Path(sys.executable)
    return {"python": platform.python_version(),
            "executable_basename": executable.name,
            "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "environment_kind": "virtualenv" if sys.prefix != sys.base_prefix else "base",
            "distribution_versions": versions,
            "package_runtime_version": getattr(pseudolife_memory, "__version__", None),
            "source_origin_matches_selected_root": source == expected,
            "source_file": "pseudolife_memory/__init__.py" if source == expected else None,
            "source_file_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


def runtime_probe_command(root):
    """Use the same interpreter and trusted instrument routing as owned children."""
    return module_command("evals.rust_port.provenance", root, ["--runtime-root", root])


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Observe an isolated child runtime without loading models.")
    parser.add_argument("--runtime-root", type=Path, required=True)
    print(json.dumps(runtime_metadata(parser.parse_args().runtime_root)))
