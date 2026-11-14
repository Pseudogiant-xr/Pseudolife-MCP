"""Which ``tests/test_*.py`` files test the frozen Python package.

Python is frozen since 14 November 2026 (maintainer ruling): nothing under
``pseudolife_memory/`` changes again, so a test that exercises only that
package can find only a dependency regression, which the weekly
``--include-frozen`` nightly looks for. Every test file is classified in
``tests/suite_classes.txt``, one ``<class> <file>`` line each:

- ``live``: it tests something that still changes or still ships (the
  installers and update wrappers, ``ops/`` tooling, the plugin hooks, docs,
  release and repository guards, CI tooling, the Console frontend, the
  container images, eval tooling, the suite's own infrastructure), a module
  an open freeze exception changes, or a frozen module and a survivor both;
  the header of ``tests/suite_classes.txt`` states the rules.
- ``frozen``: it exercises ``pseudolife_memory`` and nothing that survives.

``tests/conftest.py`` collects only live files unless pytest is given
``--include-frozen`` (:data:`SWITCH`); naming a frozen file without the switch
is refused with the switch's name. ``tests/test_suite_classes.py`` fails when
a test file is unclassified or a listed one is missing, so a new test file
adds its own line in the pull request that adds it, and a deleted one
removes its line.

Standard library only: the suite lock reads it before anything else loads.
"""
from __future__ import annotations

from pathlib import Path

CLASSES_FILE = Path(__file__).resolve().with_name("suite_classes.txt")
CLASSES = ("live", "frozen")
SWITCH = "--include-frozen"


def load(path: Path = CLASSES_FILE) -> dict[str, str]:
    """``{file name: class}``; ``ValueError`` naming the line for a malformed
    or repeated entry."""
    classes: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] not in CLASSES or not (
                parts[1].startswith("test_") and parts[1].endswith(".py") and "/" not in parts[1]):
            raise ValueError(f"{path.name}:{number}: want '<live|frozen> test_<name>.py', got {raw!r}")
        if parts[1] in classes:
            raise ValueError(f"{path.name}:{number}: {parts[1]} is classified twice")
        classes[parts[1]] = parts[0]
    return classes


def frozen(path: Path = CLASSES_FILE) -> frozenset[str]:
    """The file names classified frozen."""
    return frozenset(name for name, kind in load(path).items() if kind == "frozen")


def refusal(names: list[str]) -> str:
    shown = names if len(names) <= 5 else [*names[:5], f"and {len(names) - 5} more"]
    return (", ".join(f"tests/{name}" if name.startswith("test_") else name for name in shown)
            + " test only the frozen Python package (tests/suite_classes.txt), which a run "
            + "leaves out since the Python freeze. For the whole live suite name tests/ "
            + f"(a glob such as tests/test_*.py names frozen files too); pass {SWITCH} to "
            + "run the frozen ones as well")
