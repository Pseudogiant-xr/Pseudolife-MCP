"""Every test file is classified frozen or live, and only live ones collect by default."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests import suite_classes, suite_lock

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def test_every_test_file_is_classified_exactly_once_and_every_listed_one_exists():
    classes = suite_classes.load()
    on_disk = {path.name for path in TESTS.glob("test_*.py")}
    assert sorted(on_disk - set(classes)) == [], "classify each new test file in tests/suite_classes.txt"
    assert sorted(set(classes) - on_disk) == [], "a listed test file no longer exists; remove its line"
    names = [line.split()[1] for line in suite_classes.CLASSES_FILE.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    assert names == sorted(names), "keep tests/suite_classes.txt in name order"


def test_this_guard_and_the_classifier_are_live():
    classes = suite_classes.load()
    assert classes["test_suite_classes.py"] == "live"
    assert classes["test_suite_lock.py"] == "live"


@pytest.mark.parametrize("line, message", [
    ("live tests/test_x.py", "want"), ("frozen test_x.txt", "want"), ("kept test_x.py", "want"),
    ("live test_x.py extra", "want"), ("live x.py", "want"),
])
def test_a_malformed_line_is_refused_with_its_number(tmp_path, line, message):
    path = tmp_path / "suite_classes.txt"
    path.write_text(f"# header\nlive test_a.py\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"suite_classes.txt:3: {message}"):
        suite_classes.load(path)


def test_a_repeated_file_is_refused(tmp_path):
    path = tmp_path / "suite_classes.txt"
    path.write_text("live test_a.py\nfrozen test_a.py\n", encoding="utf-8")
    with pytest.raises(ValueError, match="classified twice"):
        suite_classes.load(path)


def _collect(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                           *args], cwd=ROOT, capture_output=True, text=True, timeout=600,
                          env={**__import__("os").environ, "PSEUDOLIFE_SUITE_LOCK": "off"})


def test_naming_a_frozen_file_without_the_switch_is_refused_with_its_name():
    frozen = sorted(suite_classes.frozen())[0]
    result = _collect(f"tests/{frozen}")
    assert result.returncode != 0
    assert f"tests/{frozen}" in result.stderr + result.stdout
    assert suite_classes.SWITCH in result.stderr + result.stdout


def test_the_switch_collects_a_frozen_file():
    frozen = sorted(suite_classes.frozen())[0]
    result = _collect(suite_classes.SWITCH, f"tests/{frozen}")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert f"tests/{frozen}::" in result.stdout


def test_a_directory_run_collects_exactly_the_live_files():
    live = {name for name, kind in suite_classes.load().items() if kind == "live"}
    result = _collect("tests")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    collected = {line.split("::", 1)[0].removeprefix("tests/") for line in result.stdout.splitlines()
                 if line.startswith("tests/test_")}
    # A live file that defines no test (or skips at import) collects nothing.
    assert collected <= live, sorted(collected - live)
    assert len(collected) >= len(live) - 5, sorted(live - collected)


def test_naming_a_live_file_is_not_refused():
    result = _collect("tests/test_suite_classes.py")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "tests/test_suite_classes.py::" in result.stdout


def test_naming_one_node_of_a_frozen_file_is_refused():
    frozen = sorted(suite_classes.frozen())[0]
    result = _collect(f"tests/{frozen}::test_anything")
    assert result.returncode != 0
    assert f"tests/{frozen}" in result.stderr + result.stdout


def test_a_frozen_name_outside_tests_is_not_refused(tmp_path):
    # Only tests/<frozen file> is frozen; the same basename elsewhere is not.
    frozen = sorted(suite_classes.frozen())[0]
    elsewhere = tmp_path / frozen
    elsewhere.write_text("def test_ok():\n    pass\n", encoding="utf-8")
    result = _collect("--rootdir", str(ROOT), str(elsewhere))
    assert suite_classes.SWITCH not in result.stderr + result.stdout


def test_the_refusal_points_a_glob_at_the_directory():
    message = suite_classes.refusal(sorted(suite_classes.frozen()))
    assert "name tests/" in message and suite_classes.SWITCH in message
    assert "more" in message and len(message) < 1000


def test_the_workflows_name_only_live_files():
    """A workflow's pytest lists name only live files (a frozen one is
    refused at collection). The launchers name none: ops/wsl-suite.ps1 and
    ops/remote-suite.ps1 pass their arguments through, and rust/ci/stress.py
    adds --include-frozen itself (test_stress_forwards_the_frozen_switch)."""
    classes = suite_classes.load()
    named = set()
    for workflow in (ROOT / ".github" / "workflows").glob("*.y*ml"):
        named |= set(re.findall(r"tests/(test_[a-z0-9_]+\.py)", workflow.read_text(encoding="utf-8")))
    frozen = sorted(name for name in named if classes.get(name) == "frozen")
    assert frozen == [], f"a workflow names frozen test files (refused at collection): {frozen}"


def test_stress_forwards_the_frozen_switch(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("stress", ROOT / "rust" / "ci" / "stress.py")
    stress = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stress)
    monkeypatch.chdir(ROOT)
    argv = stress.command("pytest", ["tests/test_abstain.py::test_x"], tmp_path, tmp_path)
    assert suite_classes.SWITCH in argv
    monkeypatch.chdir(tmp_path)        # a checkout without the switch
    assert suite_classes.SWITCH not in stress.command("pytest", ["tests/test_x.py"], tmp_path, tmp_path)


def test_no_test_file_hides_in_a_subdirectory():
    """Only tests/test_*.py is classified; one deeper would escape the list."""
    deeper = sorted(str(p.relative_to(ROOT)) for p in TESTS.rglob("test_*.py") if p.parent != TESTS)
    assert deeper == [], f"move these into tests/ or classify them: {deeper}"


# What a frozen file may not name (tests/suite_classes.txt rules 1 and 2):
# lexical, so not proof, but it catches the classification's known misses.
SURVIVING = re.compile(
    r"""["'](?:ops|plugin|\.github|docs|\.claude|examples|frontend|rust|evals)/[^"'\s]*["']"""
    r"""|/\s*["'](?:ops|plugin|\.github|docs|\.claude|examples|frontend|rust|evals)["']"""
    r"""|^\s*(?:from|import)\s+(?:evals|ops)\b"""
    r"""|\btests\.conftest\b|^\s*import conftest\b|\bfrom tests import conftest\b"""
    r"""|\btests\.fake_embedder\b|\bfrom tests import fake_embedder\b|\bpg_fixtures\._[a-z]"""
    r"""|\btests\.(?:report_redaction|suite_profile_plugin|suite_lock|client_environment)\b"""
    r"""|["'](?:README\.md|CLAUDE\.md|CHANGELOG\.md|llms(?:-full)?\.txt|server\.json|pyproject\.toml|"""
    r"""Dockerfile[^"']*|docker-compose[^"']*\.yml)["']""", re.M)


def _helper_sources(path: Path, classes: dict) -> list[tuple[str, str]]:
    """(module.name, source) of each helper this file imports from a live
    test module: one level, which is how a frozen file reached the real
    plugin/ tree through test_codex_reapproval.py's _clone."""
    import ast
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        module = node.module.removeprefix("tests.") if node.module.startswith("tests.") else None
        if module is None or classes.get(f"{module}.py") != "live":
            continue
        source = (TESTS / f"{module}.py").read_text(encoding="utf-8")
        definitions = {n.name: n for n in ast.parse(source).body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for alias in node.names:
            if alias.name in definitions:
                found.append((f"{module}.{alias.name}",
                              ast.get_source_segment(source, definitions[alias.name]) or ""))
    return found


def _frozen_offences(classes: dict) -> list[str]:
    offences = []
    for name, kind in sorted(classes.items()):
        if kind != "frozen":
            continue
        path = TESTS / name
        hit = SURVIVING.search(path.read_text(encoding="utf-8"))
        if hit:
            offences.append(f"{name} names {hit.group(0).strip()!r}")
        for helper, source in _helper_sources(path, classes):
            hit = SURVIVING.search(source)
            if hit:
                offences.append(f"{name} imports {helper}, which names {hit.group(0).strip()!r}")
    return offences


def test_a_frozen_file_names_nothing_that_survives():
    offences = _frozen_offences(suite_classes.load())
    assert offences == [], ("a frozen file tests something that survives; classify it live "
                            f"(tests/suite_classes.txt rules 1-2): {offences}")


def test_the_frozen_guard_catches_the_files_it_was_written_for():
    # The classification at 2ba7a69f8 froze these; the guard must name them.
    classes = {**suite_classes.load(), **{name: "frozen" for name in (
        "test_codex_hook_runtime_trust.py", "test_fake_embedder.py", "test_pg_conn_reset_files.py",
        "test_pg_fixture_dream_wait.py")}}
    offences = " ".join(_frozen_offences(classes))
    for name in ("test_codex_hook_runtime_trust.py", "test_fake_embedder.py", "test_pg_conn_reset_files.py",
                 "test_pg_fixture_dream_wait.py"):
        assert name in offences, name


# A live test that reads a rust/ file runs in the guards job, since a
# Rust-only batch skips the Python lanes on its pull request and its push.
RUST_PATH = re.compile(r"""["']rust/[^"'\s]*["']|/\s*["']rust["']""")


def test_every_live_test_of_rust_files_runs_in_guards():
    classes = suite_classes.load()
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
    guards = " ".join(step.get("run", "") for step in jobs["guards"]["steps"])
    named = set(re.findall(r"tests/(test_[a-z0-9_]+\.py)", guards))
    readers = {name for name, kind in classes.items() if kind == "live"
               and RUST_PATH.search((TESTS / name).read_text(encoding="utf-8"))}
    assert sorted(readers - named) == [], "add these to ci.yml's guards job: they read rust/ files"
    # They run on a Rust-only batch, push or merge group (scope's rust_only),
    # not on every ordinary pull request.
    step = next(s for s in jobs["guards"]["steps"] if s.get("name") == "Live tests that read rust/ files")
    assert step["if"] == "needs.scope.outputs.rust_only == 'true'"
    assert readers <= set(re.findall(r"tests/(test_[a-z0-9_]+\.py)", step["run"]))
    # They need the guards job's PostgreSQL, and fail rather than skip without it.
    assert jobs["guards"]["env"]["PSEUDOLIFE_REQUIRE_TEST_POSTGRES"] == "1"
    assert "postgres" in jobs["guards"]["services"]


def test_the_eval_lanes_name_only_live_files():
    """The eval tooling passes these node ids to pytest in this checkout."""
    import json

    from evals.rust_port import pytest_plugin

    classes = suite_classes.load()
    manifest = json.loads((ROOT / "evals/rust_port/oracle_tests.json").read_text(encoding="utf-8"))
    nodes = [*manifest["mapped"], *manifest["unmapped"], *pytest_plugin.CLI_SUBPROCESS_NODES,
             *pytest_plugin.CLI_HOOK_NODES, *pytest_plugin.CLI_VERSION_NODES]
    files = {node.split("::", 1)[0].removeprefix("tests/") for node in nodes}
    assert files, "the eval node sets moved; point this guard at them"
    frozen = sorted(name for name in files if classes.get(name) == "frozen")
    assert frozen == [], f"an eval lane names frozen test files (refused at collection): {frozen}"


def test_the_suite_lock_counts_a_full_run_against_the_live_files():
    live = sorted(TESTS / name for name, kind in suite_classes.load().items() if kind == "live")
    excluded = suite_classes.frozen()
    half = live[: (len(live) + 1) // 2]
    assert suite_lock.is_full_run([str(path) for path in half], ROOT, TESTS, excluded=excluded)
    assert not suite_lock.is_full_run([str(path) for path in half[:-1]], ROOT, TESTS, excluded=excluded)
    # Frozen files named beside them count for nothing.
    padded = [str(path) for path in half[:-1]] + [str(TESTS / name) for name in sorted(excluded)]
    assert not suite_lock.is_full_run(padded, ROOT, TESTS, excluded=excluded)
    assert suite_lock.is_full_run(["tests"], ROOT, TESTS, excluded=excluded)
