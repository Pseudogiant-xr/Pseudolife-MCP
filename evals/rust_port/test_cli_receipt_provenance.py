"""CLI receipts refuse missing, changed or foreign executed helper files."""
from pathlib import Path
import subprocess

import pytest

from evals.rust_port import provenance


def committed_helper(tmp_path):
    root = tmp_path / "instrument"
    root.mkdir()
    helper = root / "evals/rust_port/harness.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("# committed fixture helper\n")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "add", "evals/rust_port/harness.py"], cwd=root, check=True)
    tree = subprocess.check_output(["git", "write-tree"], cwd=root).decode().strip()
    # Disposable fixture objects only; the candidate worktree is never committed.
    commit = subprocess.check_output(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                      "commit-tree", tree, "-m", "fixture"], cwd=root).decode().strip()
    subprocess.run(["git", "update-ref", "HEAD", commit], cwd=root, check=True)
    return root, helper


def test_loaded_helper_has_exact_committed_tree_and_actual_file_hash(tmp_path):
    import hashlib
    root, helper = committed_helper(tmp_path)
    binding = provenance.require_instrument_binding(root, {"evals/rust_port/harness.py": [helper]})
    assert binding["source_tree"] == subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root).decode().strip()
    assert binding["source_files_sha256"]["evals/rust_port/harness.py"] == hashlib.sha256(helper.read_bytes()).hexdigest()


@pytest.mark.parametrize("mutation", ["missing", "dirty", "foreign"])
def test_uncommitted_or_foreign_helper_refuses_receipt_binding(tmp_path, mutation):
    root, helper = committed_helper(tmp_path)
    loaded = helper
    if mutation == "missing":
        helper.unlink()
    elif mutation == "dirty":
        helper.write_text("# changed preparation policy\n")
    else:
        loaded = tmp_path / "foreign.py"
        loaded.write_bytes(helper.read_bytes())
    with pytest.raises(RuntimeError):
        provenance.require_instrument_binding(root, {"evals/rust_port/harness.py": [loaded]})


def test_foreign_callable_refuses_even_when_expected_file_is_clean(tmp_path):
    root, helper = committed_helper(tmp_path)
    foreign = tmp_path / "foreign.py"
    foreign.write_text("def capture():\n    return None\n")
    namespace = {}
    exec(compile(foreign.read_text(), str(foreign), "exec"), namespace)
    with pytest.raises(RuntimeError, match="another tree"):
        provenance.require_instrument_binding(root, {"evals/rust_port/harness.py": [namespace["capture"]]})
