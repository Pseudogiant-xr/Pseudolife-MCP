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


@pytest.mark.parametrize("path", ("new-area/new-guide.md", "new-area/llms-new.txt"))
def test_documentation_fallback_cannot_classify_code(path):
    rules = ledger.load_rules(ledger.ROOT)
    direct = ledger.scan(path, "Run python -m pseudolife_memory.cli to check the client.")
    indirect = ledger.scan(path, "Run python new_tool.py to check the client.",
                           frozenset({"new_tool.py"}))
    for hits in (direct, indirect):
        assert hits == [ledger.Consumer(path, 1, "documentation")]
        assert ledger.classify(hits, rules)[0] == []
    code = ledger.scan("new-area/new_tool.py", "from pseudolife_memory import cli")
    assert code == [ledger.Consumer("new-area/new_tool.py", 1, "python-import")]
    assert ledger.classify(code, rules)[0] == code


@pytest.mark.parametrize(("path", "category"), [
    ("evals/rust_port/phase1_ci.py", ledger.TESTING),
    ("evals/prompts/live.md", ledger.ACTIVE),
    ("evals/results/old.md", ledger.HISTORY),
    ("evals/unresolved_driver.py", ledger.EVALS),
])
def test_eval_classes(path, category):
    source = ("python -m pseudolife_memory.cli" if path.endswith(".md")
              else "from pseudolife_memory import cli")
    hit, = ledger.scan(path, source)
    rule = next(rule for rule in ledger.load_rules(ledger.ROOT) if rule.matches(hit))
    assert rule.category == category


@pytest.mark.parametrize(("path", "source", "kind"), [
    ("install.sh", 'pipx install --force "$repo"; then', "command"),
    ("install.sh", 'python3 -m pip install --user --upgrade "$repo"; then', "command"),
    ("install.sh", 'python -m pip install --user --upgrade "$repo"; then', "command"),
    ("install.ps1", "pipx install --force $repo 2>&1 | Out-Host", "command"),
    ("install.ps1", "& $exe @exeArgs -m pip install --user --upgrade $repo 2>&1 | Out-Host", "command"),
    ("tool.sh", 'pip install -e ./', "command"),
    ("tool.sh", 'pip install -e "./[dev]"', "command"),
    ("tool.sh", 'pip install "${repo}"', "command"),
    ("tool.ps1", 'pip install "$env:GITHUB_WORKSPACE"', "command"),
    (".github/workflows/new.yml", 'run: pip install "$GITHUB_WORKSPACE"', "workflow-install"),
    (".github/workflows/new.yml", 'run: pip install "${{ github.workspace }}"', "workflow-install"),
    ("tool.sh", "pip install dist/*.whl", "command"),
    ("tool.ps1", r"pip install .\dist\pseudolife_mcp-0.24-py3-none-any.whl", "command"),
    ("tool.sh", 'uv pip install -e "./"', "command"),
    ("tool.sh", 'uv tool install "$repo"', "command"),
    ("tool.sh", "uv sync --frozen", "command"),
    (".github/workflows/new.yml", "run: uv run pytest", "workflow-install"),
    ("Dockerfile", "COPY . /app", "docker-copy"),
    ("Dockerfile", 'COPY ["./", "/app"]', "docker-copy"),
    ("Dockerfile", "ADD . .", "docker-copy"),
])
def test_checkout_install_and_copy_forms(path, source, kind):
    assert ledger.scan(path, source) == [ledger.Consumer(path, 1, kind)]


def test_invalid_docker_copy_names_the_source():
    with pytest.raises(ValueError, match=r"ops/Dockerfile\.new:2: invalid COPY/ADD"):
        ledger.scan("ops/Dockerfile.new", "FROM scratch\nCOPY ['.', '/app']")


def test_inventory_reaches_checkout_commands_without_package_markers(tmp_path, monkeypatch):
    sources = {"Dockerfile": "ADD . .", "tool.ps1": "UV SYNC --frozen"}
    for path, source in sources.items():
        (tmp_path / path).write_text(source, encoding="utf-8")
    monkeypatch.setattr(ledger, "tracked_files", lambda root: iter(sources))
    assert ledger.inventory(tmp_path) == [
        ledger.Consumer("Dockerfile", 1, "docker-copy"),
        ledger.Consumer("tool.ps1", 1, "command"),
    ]


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
    ("Dockerfile", "COPY --from=builder . /app"),
    ("tool.sh", "uv run --no-project --with pytest pytest"),
    ("tool.sh", "pip install ../other-project"),
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


@pytest.mark.parametrize(("path", "source", "line"), [
    (".github/workflows/new.yml",
     "run: python -c \"from pseudolife_memory import cli\"", 1),
    (".github/workflows/new.yml",
     "run: |\n  python - <<'PY'\n  import pseudolife_memory\n  PY\n", 3),
    (".github/workflows/new.yml",
     "run: |\r\n  python - <<'PY'\r\n  import pseudolife_memory\r\n  PY\r\n", 3),
    ("ops/unclassified.sh",
     "\"$loopback_py\" -c 'import sys\nfrom pseudolife_memory import cli\n' \"$repo\"", 2),
    ("ops/unclassified.ps1",
     '& $python -c "import sys; from pseudolife_memory import cli" $repo', 1),
])
def test_embedded_python_is_an_unclassified_consumer(path, source, line):
    hit = ledger.Consumer(path, line, "python-import")
    assert ledger.classify(ledger.scan(path, source), []) == ([hit], [])


def test_embedded_python_does_not_scan_shell_prose():
    assert ledger.scan("tool.sh", "echo 'from pseudolife_memory import cli'\n") == []


def test_probe_plan_uses_live_suite_manifest(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/suite_classes.txt").write_text(
        "# Suite policy\nlive test_current.py\nfrozen test_old.py\n", encoding="utf-8")
    labels = {probe.label for probe in package_absent.plan(tmp_path)}
    assert "import tests/test_current.py" in labels
    assert "import tests/test_old.py" not in labels
    assert "import tests/conftest.py" in labels
    assert "script rust/ci/ci_budget.py check" in labels
    assert "script rust/cli_harness/__main__.py --help" in labels
