"""Each CLI harness row's CI declarations, from its own ``rows/<row>.json``.

A port used to add its row to a set of shared lists (the row roster, the
Python modules and shim code that reach it, its source mutants), so any two
open ports edited the same lines. Each row now declares these in its own
file next to ``rows/<row>.py``, and the CI scripts read them here.

Every value is also stated somewhere independent, so dropping a declaration
still fails a check: the roster against the goldens, the W1-C shards in
``parity_scope.SHARDS`` and the workflow's source-mutant shard list; source
mutants against the row module's ``Mutant(...)`` calls; platform exclusions
against each mutant's runtime platforms; shim entries against the shim's own
imports (``test_parity_scope.py``).

Standard library only, and read as data: the scope job runs this with the
runner's own python3, and nothing here imports a row module.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROWS_DIR = "rust/cli_harness/rows"
# Every field a row declares, and its type. A file with another key set, or
# a value of another type, is refused rather than read in part.
FIELDS = {
    "parity": str,
    # The row's source mutants (the Mutant(...) ids in rows/<row>.py), in
    # the order coverage_plan cuts them into eight-mutant nightly shards.
    "source_mutants": list,
}
# The modules under rows/ that are not rows: a shared helper and a test
# module. Every other rows/<name>.py, less _private ones, declares
# rows/<name>.json.
HELPERS = ("lease_transport", "test_doctor_proof")


def problems(data) -> list[str]:
    """What is wrong with one row's declarations (empty when nothing is)."""
    if not isinstance(data, dict):
        return ["is not a JSON object"]
    if set(data) != set(FIELDS):
        return [f"declares {sorted(data)}, not {sorted(FIELDS)}"]
    found = []
    for name, kind in FIELDS.items():
        if type(data[name]) is not kind:
            found.append(f"{name} is not a {kind.__name__}")
    if found:
        return found
    if not data["parity"].strip():
        found.append("parity is empty")
    mutants = data["source_mutants"]
    if not mutants or any(type(name) is not str or not name for name in mutants):
        found.append("source_mutants is not a list of mutant ids")
    elif len(set(mutants)) != len(mutants):
        found.append("source_mutants repeats an id")
    return found


@functools.lru_cache(maxsize=None)
def declarations(root: Path = ROOT) -> dict[str, dict]:
    """Every row's declarations, by row name, in name order. A malformed
    file raises ValueError naming it."""
    found = {}
    for path in sorted((root / ROWS_DIR).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(f"{path.name}: not JSON ({error})") from None
        wrong = problems(data)
        if wrong:
            raise ValueError(f"{path.name}: {wrong[0]}")
        found[path.stem] = data
    return found


def rows(root: Path = ROOT) -> tuple[str, ...]:
    """The declared rows, in name order."""
    return tuple(declarations(root))


def source_mutants(root: Path = ROOT) -> dict[str, list[str]]:
    """Each row's source mutant ids, in its declared order."""
    return {row: list(data["source_mutants"]) for row, data in declarations(root).items()}


def roster_problems(root: Path = ROOT) -> list[str]:
    """Every row file without its declaration, and every declaration without
    its row: a row dropped from the roster leaves its module, spec or goldens
    behind, and each of those fails here."""
    directory = root / ROWS_DIR
    declared = set(rows(root))
    modules = {path.stem for path in directory.glob("*.py")
               if not path.stem.startswith("_") and path.stem not in HELPERS}
    goldens = {}
    for path in (root / "rust/cli_harness/goldens").glob("*.json"):
        goldens.setdefault(path.name.split(".", 1)[0], []).append(path.name)
    found = [f"rows/{name}.py has no rows/{name}.json" for name in sorted(modules - declared)]
    found += [f"rows/{name}.json has no rows/{name}.py" for name in sorted(declared - modules)]
    found += [f"goldens/{name} belongs to no declared row"
              for row in sorted(set(goldens) - declared) for name in sorted(goldens[row])]
    found += [f"rows/{path.name} belongs to no declared row"
              for path in sorted(directory.glob("*.md")) if path.stem not in declared]
    return found
