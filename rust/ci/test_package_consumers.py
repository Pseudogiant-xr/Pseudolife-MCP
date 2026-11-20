"""The deletion ledger rejects unclassified dependencies and allows removals."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import package_absent  # noqa: E402
import package_consumers as ledger  # noqa: E402


def test_checkout_consumers_are_classified():
    consumers = ledger.inventory(ledger.ROOT)
    missing, _ = ledger.classify(consumers, ledger.load_rules(ledger.ROOT))
    assert not missing, "\n".join(hit.location for hit in missing)


@pytest.fixture
def known_rule():
    return ledger.Rule("tools/*.py", "python-import", ledger.ACTIVE, "Checkout tools.")


def test_new_consumer_needs_a_rule(known_rule):
    hits = ledger.scan("elsewhere/new.py", "from pseudolife_memory import cli\n")
    missing, _ = ledger.classify(hits, [known_rule])
    assert [hit.location for hit in missing] == ["elsewhere/new.py:1 (python-import)"]


def test_removed_consumer_stays_green(known_rule):
    hits = ledger.scan("tools/old.py", "from pseudolife_memory import cli\n")
    assert ledger.classify(hits, [known_rule]) == ([], [])
    assert ledger.classify([], [known_rule]) == ([], [known_rule])


def test_stale_rule_is_a_report_note(known_rule, capsys):
    assert ledger.report([], [known_rule]) == 0
    assert "Unused rule: tools/*.py (python-import)" in capsys.readouterr().out


@pytest.mark.parametrize(("path", "source", "kind"), [
    ("tool.py", "from pseudolife_memory.cli import main", "python-import"),
    ("tool.py", "import os, pseudolife_memory", "python-import"),
    ("tool.py", "from importlib import import_module as load\nload('pseudolife_memory.cli')", "python-import"),
    ("tool.py", "subprocess.run([python, '-m',\n 'pseudolife_memory.cli'])", "command"),
    ("tool.py", "src = root / 'pseudolife_memory' / 'cli.py'", "path"),
    ("tool.py", "src = os.path.join(root, 'pseudolife_memory')", "path"),
    ("tool.py", "src = root / 'pyproject.toml'", "metadata"),
    ("tool.sh", 'python -m pseudolife_memory.cli serve', "command"),
    ("tool.sh", 'sh -c "cat /app/pseudolife_memory/storage/schema.py"', "command"),
    ("tool.rs", 'include_str!(\n"../../ops/requirements.lock.txt")', "rust-include"),
    ("tool.rs", 'include_bytes!(concat!(env!("CARGO_MANIFEST_DIR"), "/pseudolife_memory/cli.py"))', "rust-include"),
    ("tool.rs", 'repo.join("pyproject.toml").is_file()', "metadata"),
    ("Dockerfile", 'COPY --chown=app ["pseudolife_memory/cli.py", "/app/cli.py"]', "docker-copy"),
    (".github/workflows/build.yml", 'run: pip install -e ".[dev]"', "workflow-install"),
    (".github/workflows/build.yml", "run: python -m build", "workflow-install"),
    ("docs/guide.md", 'Run `python -m pseudolife_memory.cli serve` to start.', "documentation"),
])
def test_consumer_syntax(path, source, kind):
    assert {hit.kind for hit in ledger.scan(path, source)} == {kind}


@pytest.mark.parametrize(("path", "source"), [
    ("docs/notes.md", "The former pseudolife_memory package was frozen."),
    ("history/run.log", "2026-11-01 DEBUG python -m pseudolife_memory.cli"),
    ("tool.py", '# from pseudolife_memory import cli\nlogger = "pseudolife_memory"'),
    ("tool.rs", '// repo.join("pyproject.toml") used to locate the checkout'),
    ("tool.rs", "let quote = '\"'; // Python writer: pseudolife_memory/web/api.py:42"),
    ("Dockerfile", 'COPY frontend/dist /app/pseudolife_memory/web/static'),
    (".github/workflows/build.yml", "run: python -m pip install torch"),
])
def test_prose_and_unrelated_inputs_are_not_consumers(path, source):
    assert ledger.scan(path, source) == []


def test_recipe_names_a_package_dependent_script():
    script = "ops/setup-codex-hooks.py"
    source = f'let help = r#"To repair: python {script}"#;'
    assert ledger.scan("tool.rs", source, frozenset({script})) == [
        ledger.Consumer("tool.rs", 1, "command"),
    ]


def test_probe_plan_uses_live_suite_manifest(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/suite_classes.txt").write_text(
        "# Suite policy\nlive test_current.py\nfrozen test_old.py\n", encoding="utf-8")
    labels = {probe.label for probe in package_absent.plan(tmp_path)}
    assert "import tests/test_current.py" in labels
    assert "import tests/test_old.py" not in labels
    assert "import tests/conftest.py" in labels
    assert "script rust/ci/ci_budget.py check" in labels
