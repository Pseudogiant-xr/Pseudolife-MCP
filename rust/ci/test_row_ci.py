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
    (lambda data: data.update(python_roots=[]), "python_roots is not a list of module names"),
    (lambda data: data.update(python_roots=["restore_cli.py"]), "python_roots is not a list of module names"),
    (lambda data: data.update(postgres="false"), "postgres is not a bool"),
    (lambda data: data.update(source_mutants=[]), "source_mutants is not a list of mutant ids"),
    (lambda data: data.update(source_mutants=[1]), "source_mutants is not a list of mutant ids"),
    (lambda data: data["source_mutants"].append(data["source_mutants"][0]), "source_mutants repeats an id"),
    (lambda data: data["platform_exclusions"].pop("windows"), "platform_exclusions does not give a reason"),
    (lambda data: data["platform_exclusions"]["linux"].update({"restore-yes-ignored": " "}),
     "platform_exclusions does not give a reason"),
    (lambda data: data["platform_exclusions"]["windows"].update({"backup-omits-fifo": "x"}),
     "platform_exclusions names a mutant the row does not declare"),
])
def test_a_malformed_declaration_is_refused_by_name(tree, change, message):
    path = tree / row_ci.ROWS_DIR / "restore.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=f"restore.json: {message}"):
        row_ci.declarations(tree)


def test_source_mutants_keep_their_declared_order():
    # coverage_plan cuts each row's list into eight-mutant shards in this
    # order, so a reordering would move mutants between shards.
    from coverage_plan import SOURCE_MUTANTS, SOURCE_SHARDS
    assert SOURCE_MUTANTS == row_ci.source_mutants()
    for name, (row, ids) in SOURCE_SHARDS.items():
        index = int(name.rsplit("-", 1)[1])
        assert ids == SOURCE_MUTANTS[row][index * 8:index * 8 + 8]


def test_a_dropped_platform_exclusion_fails_the_runtime_registration_check(monkeypatch):
    # The declaration and each Mutant's own platforms state the same fact;
    # dropping the declared exclusion makes the registered roster disagree.
    import copy
    import control_selection
    from rust.cli_harness import core
    excluded = row_ci.platform_exclusions()
    owners = {name: row for row, ids in row_ci.source_mutants().items() for name in ids}
    name = sorted(excluded[core.PLATFORM])[0]
    control_selection.runtime_controls(owners[name], core.PLATFORM)
    reduced = copy.deepcopy(excluded)
    del reduced[core.PLATFORM][name]
    monkeypatch.setattr(control_selection, "PLATFORM_EXCLUSIONS", reduced)
    with pytest.raises(AssertionError, match="eligibility drift"):
        control_selection.runtime_controls(owners[name], core.PLATFORM)


def test_every_python_root_is_an_oracle_module():
    # A misspelt root would leave its row unselected when its module changes.
    for row, roots in row_ci.python_roots().items():
        for name in roots:
            assert (ROOT / "pseudolife_memory" / f"{name}.py").is_file(), (row, name)


def test_the_scope_and_precheck_readers_agree_with_the_validated_declarations():
    # parity_scope and pr_precheck read the files themselves, since
    # ops/integrate.py runs them frozen with no other rust/ci module.
    import parity_scope
    import pr_precheck
    assert parity_scope.row_declarations() == row_ci.declarations()
    assert parity_scope.shim_commands() == row_ci.shim_users()
    assert pr_precheck.PYTHON_ROOTS == row_ci.python_roots()
    assert pr_precheck.POSTGRES_ROWS == row_ci.postgres_rows()
