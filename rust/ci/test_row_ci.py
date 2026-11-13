"""Each harness row's CI declarations, and the checks that a dropped one fails."""
import json
import shutil
from pathlib import Path

import pytest

import row_ci

ROOT = Path(__file__).resolve().parents[2]


def test_every_row_declares_well_formed_ci_data():
    declared = row_ci.declarations()
    assert declared, "no row declarations found"
    assert row_ci.roster_problems() == []


def test_the_harness_roster_is_the_declared_one():
    from rust.cli_harness import rows
    assert rows.ROWS == {row: data["parity"] for row, data in row_ci.declarations().items()}


@pytest.fixture
def tree(tmp_path):
    """A copy of the harness's rows and goldens to break."""
    for part in ("rows", "goldens"):
        shutil.copytree(ROOT / "rust/cli_harness" / part, tmp_path / "rust/cli_harness" / part,
                        ignore=shutil.ignore_patterns("__pycache__"))
    row_ci.declarations.cache_clear()
    yield tmp_path
    row_ci.declarations.cache_clear()


def test_a_copied_tree_is_clean(tree):
    assert row_ci.roster_problems(tree) == []


def test_a_dropped_declaration_fails_the_roster(tree):
    (tree / row_ci.ROWS_DIR / "restore.json").unlink()
    assert "rows/restore.py has no rows/restore.json" in row_ci.roster_problems(tree)


def test_a_dropped_row_module_fails_the_roster(tree):
    (tree / row_ci.ROWS_DIR / "restore.py").unlink()
    assert "rows/restore.json has no rows/restore.py" in row_ci.roster_problems(tree)


def test_a_row_dropped_with_its_module_leaves_its_spec_and_goldens_failing(tree):
    for name in ("restore.json", "restore.py"):
        (tree / row_ci.ROWS_DIR / name).unlink()
    assert row_ci.roster_problems(tree) == [
        "goldens/restore.linux.json belongs to no declared row",
        "goldens/restore.windows.json belongs to no declared row",
        "rows/restore.md belongs to no declared row",
    ]


@pytest.mark.parametrize("change, message", [
    (lambda data: data.pop("parity"), "declares"),
    (lambda data: data.update(extra=1), "declares"),
    (lambda data: data.update(parity=1), "parity is not a str"),
    (lambda data: data.update(parity=" "), "parity is empty"),
])
def test_a_malformed_declaration_is_refused_by_name(tree, change, message):
    path = tree / row_ci.ROWS_DIR / "restore.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=f"restore.json: {message}"):
        row_ci.declarations(tree)
