"""A metadata-only release changes identity without changing the Python contract."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from . import oracle_selection as oracle
from .test_phase1_inventory import (
    test_candidate_runtime_identity_matches_the_selected_python_pin as check_identity,
)


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, encoding="utf-8").strip()


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
        "commit", "--quiet", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


def bump(root, version):
    write(root, "pyproject.toml", '[project]\nname = "fixture"\nversion = "' + version + '"\n')
    write(root, "rust/shim/Cargo.toml", '[package]\nname = "pseudolife-stdio"\nversion = "' + version + '"\n')
    write(root, "rust/Cargo.lock", '[[package]]\nname = "pseudolife-stdio"\nversion = "' + version + '"\n')
    write(root, "rust/shim/src/lifecycle.rs", 'pub const PACKAGE_VERSION: &str = "' + version + '";\n')
    write(root, "plugin/release.json", json.dumps({"version": version}) + "\n")
    write(root, "server.json", json.dumps({"version": version, "packages": [{"version": version}]}) + "\n")
    write(root, "ops/docker-compose.yml", 'image: pseudolife-daemon:' + version + '\n')
    write(root, "pseudolife_memory/compose/docker-compose.yml", 'image: pseudolife-daemon:' + version + '\n')


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.delenv(oracle.SELECTION_ENV, raising=False)
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "core.autocrlf", "false")
    bump(tmp_path, "0.18.0")
    write(tmp_path, "pseudolife_memory/shim.py", "CONTRACT = 'unchanged'\n")
    write(tmp_path, "tests/test_shim.py", "def test_contract(): pass\n")
    base = commit(tmp_path)
    bump(tmp_path, "0.18.1")
    write(tmp_path, "CHANGELOG.md", "Release 0.18.1\n")
    head = commit(tmp_path)
    return tmp_path, base, head


def select_release(root, base, head):
    return oracle.select(root, "pull_request", {
        "pull_request": {"base": {"sha": base}, "head": {"sha": head}}}, head)


def test_version_cut_uses_new_identity_on_pr_and_push_without_followup(release, monkeypatch):
    root, base, head = release
    selected = select_release(root, base, head)
    assert selected["oracle_head"] == head
    assert selected["release_cut"] == {"base_version": "0.18.0", "version": "0.18.1"}
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selected))
    assert oracle.selected_oracle(root) == selected
    check_identity(SimpleNamespace(ROOT=root, PHASE1_ORACLE=base))
    pushed = oracle.select(root, "push", {"after": head}, head)
    assert pushed["oracle_head"] == head
    # A later native-only PR returns to the ordinary merge-base policy.
    write(root, "rust/shim/src/followup.rs", "// Native implementation change.\n")
    followup = commit(root)
    assert select_release(root, head, followup)["oracle_head"] == head


def test_local_runtime_identity_is_not_tied_to_historical_inventory(release):
    root, base, _ = release
    check_identity(SimpleNamespace(ROOT=root, PHASE1_ORACLE=base))


@pytest.mark.parametrize("path,text", [
    ("pseudolife_memory/shim.py", "CONTRACT = 'changed'\n"),
    ("tests/test_shim.py", "def test_weakened(): pass\n"),
    ("pyproject.toml", '[project]\nname = "fixture"\nversion = "0.18.1"\ndependencies = ["new"]\n'),
    (".github/workflows/rust.yml", "jobs: {}\n"),
    ("ops/install.sh", "echo changed\n"),
    ("plugin/hooks/start.sh", "echo changed\n"),
    ("plugin/release.json", '{"version": "0.18.1", "extra": true}\n'),
    ("ops/docker-compose.yml", "image: different/daemon:0.18.1\n"),
    ("pseudolife_memory/compose/docker-compose.yml", "image: different/daemon:0.18.1\n"),
    ("unknown.txt", "new input\n"),
    ("docs/helper.py", "print('new executable input')\n"),
    ("evals/rust_port/instrument.py", "print('changed instrument')\n"),
])
def test_behavior_or_unknown_changes_keep_the_merge_base(release, path, text):
    root, base, _ = release
    write(root, path, text)
    head = commit(root)
    selected = select_release(root, base, head)
    assert selected["oracle_head"] == base
    assert "release_cut" not in selected


def test_release_receipt_cannot_be_forged_for_a_behavior_change(release, monkeypatch):
    root, base, _ = release
    write(root, "pseudolife_memory/shim.py", "CONTRACT = 'changed'\n")
    head = commit(root)
    selected = select_release(root, base, head)
    selected.update(oracle_head=head, release_cut={"base_version": "0.18.0", "version": "0.18.1"})
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selected))
    with pytest.raises(ValueError, match="immutable event binding"):
        oracle.selected_oracle(root)


def test_runtime_identity_still_rejects_wrong_native_version(release):
    root, base, _ = release
    write(root, "rust/shim/src/lifecycle.rs", 'pub const PACKAGE_VERSION: &str = "99.0.0";\n')
    with pytest.raises(AssertionError):
        check_identity(SimpleNamespace(ROOT=root, PHASE1_ORACLE=base))


@pytest.mark.parametrize("path", ["tests/test_shim.py", "pseudolife_memory/shim.py"])
def test_removed_contract_source_blocks_release_selection(release, path):
    root, base, _ = release
    (root / path).unlink()
    head = commit(root)
    assert select_release(root, base, head)["oracle_head"] == base


@pytest.mark.parametrize("path", ["pyproject.toml", "ops/docker-compose.yml", "README.md"])
def test_executable_metadata_or_documentation_blocks_release_selection(release, path):
    root, base, _ = release
    if not (root / path).exists():
        write(root, path, "Release notes\n")
        git(root, "add", path)
    git(root, "update-index", "--chmod=+x", path)
    # Commit the index directly; git add would reset the mode on Windows.
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
        "commit", "--quiet", "-m", "fixture mode")
    head = git(root, "rev-parse", "HEAD")
    assert select_release(root, base, head)["oracle_head"] == base


def test_inventory_uses_event_identity_even_if_checkout_metadata_differs(release, monkeypatch):
    root, base, head = release
    # A behavior-changing release is ineligible: the selected version stays old.
    write(root, "pseudolife_memory/shim.py", "CONTRACT = 'changed'\n")
    head = commit(root)
    selected = select_release(root, base, head)
    assert selected["oracle_head"] == base
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selected))
    with pytest.raises(AssertionError):
        check_identity(SimpleNamespace(ROOT=root, PHASE1_ORACLE=base))


def test_historical_release_cut_is_recognized_without_rewriting_evidence():
    root = Path(__file__).resolve().parents[2]
    # #742 changed release metadata, compose image tags and documentation only.
    selected = oracle.release_cut(root, "1d444aa1bc432431e5fcbc07a55e1fe661c0f8e4^", "1d444aa1bc432431e5fcbc07a55e1fe661c0f8e4")
    assert selected == {"base_version": "0.17.0", "version": "0.18.0"}


def test_release_metadata_export_binds_new_version(release, monkeypatch, tmp_path):
    from . import phase1_oracle
    root, base, head = release
    selection = select_release(root, base, head)
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selection))
    monkeypatch.setattr(phase1_oracle, "ROOT", root)
    monkeypatch.setattr(phase1_oracle, "selected_oracle", lambda: oracle.selected_oracle(root))
    destination = tmp_path / "m\u00e9tadata with spaces"
    result = phase1_oracle.prepare_metadata(destination)
    assert result["version"] == "0.18.1"
    assert result["oracle_head"] == head
    assert (destination / "pyproject.toml").read_bytes() == (root / "pyproject.toml").read_bytes()


@pytest.mark.parametrize("path,old,new", [
    ("ops/docker-compose.yml", "environment: {VALUE: '0.18.0'}\n", "environment: {VALUE: '0.18.1'}\n"),
    ("server.json", '{"version":"0.18.0","description":"0.18.0"}\n',
     '{"version":"0.18.1","description":"0.18.1"}\n'),
    ("plugin/release.json", '{"version":"0.18.0","extra":"0.18.0"}\n',
     '{"version":"0.18.1","extra":"0.18.1"}\n'),
])
def test_version_substitution_cannot_change_non_version_fields(release, path, old, new):
    root, _, _ = release
    bump(root, "0.18.0")
    write(root, path, old)
    base = commit(root)
    bump(root, "0.18.1")
    write(root, path, new)
    head = commit(root)
    assert select_release(root, base, head)["oracle_head"] == base


def test_release_without_native_bump_still_fails_identity(release, monkeypatch):
    root, base, _ = release
    for path in ("rust/shim/Cargo.toml", "rust/Cargo.lock", "rust/shim/src/lifecycle.rs"):
        write(root, path, git(root, "show", base + ":" + path) + "\n")
    head = commit(root)
    selected = select_release(root, base, head)
    assert selected["oracle_head"] == head
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selected))
    with pytest.raises(AssertionError):
        check_identity(SimpleNamespace(ROOT=root, PHASE1_ORACLE=base))
