"""The Python freeze guard decides on an ordinary pull request's changed paths only."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import python_freeze  # noqa: E402


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "master")
    git(tmp_path, "config", "user.email", "ci@example.com")
    git(tmp_path, "config", "user.name", "ci")
    for path in ("pseudolife_memory/cli.py", "pseudolife_memory/web/static/index.html",
                 "pseudolife_memory/web/api.py", "docs/guide.md", "rust/shim/src/cli.rs"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{path}\n" * 20, encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def change(repo: Path, edit) -> list[str]:
    """Commit one edit on top of the base and return the guard's verdict."""
    edit(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    paths = python_freeze.changed_paths(repo, "HEAD^1", "HEAD")
    assert paths is not None
    return python_freeze.frozen_paths(paths)


def write(path: str, text: str = "changed\n"):
    def edit(repo: Path):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return edit


def move(old: str, new: str):
    def edit(repo: Path):
        (repo / new).parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", old, new)
    return edit


def test_a_change_inside_the_package_is_refused(repo):
    assert change(repo, write("pseudolife_memory/cli.py")) == ["pseudolife_memory/cli.py"]


def test_a_new_file_inside_the_package_is_refused(repo):
    assert change(repo, write("pseudolife_memory/new_module.py")) == ["pseudolife_memory/new_module.py"]


def test_a_deletion_inside_the_package_is_refused(repo):
    assert change(repo, lambda r: git(r, "rm", "-q", "pseudolife_memory/web/api.py")) == [
        "pseudolife_memory/web/api.py"]


def test_the_console_build_output_is_exempt(repo):
    assert change(repo, write("pseudolife_memory/web/static/index.html")) == []
    assert change(repo, write("pseudolife_memory/web/static/assets/app-123.js")) == []


def test_a_rename_out_of_the_console_build_output_is_exempt(repo):
    assert change(repo, move("pseudolife_memory/web/static/index.html", "frontend/dist/index.html")) == []


def test_a_rename_out_of_the_package_is_refused_by_its_old_path(repo):
    # --no-renames: the old path is a deletion under the package.
    assert change(repo, move("pseudolife_memory/web/api.py", "legacy/api.py")) == [
        "pseudolife_memory/web/api.py"]


def test_paths_that_merely_contain_the_package_name_pass(repo):
    edits = [write("docs/pseudolife_memory/notes.md"), write("tests/test_pseudolife_memory_cli.py"),
             write("pseudolife_memory_tools/x.py"), write("rust/pseudolife_memory/web/static.rs"),
             write("pseudolife_memory.md")]
    for edit in edits:
        assert change(repo, edit) == []


def test_a_sibling_of_the_static_directory_is_not_exempt(repo):
    assert change(repo, write("pseudolife_memory/web/static_files.py")) == ["pseudolife_memory/web/static_files.py"]


def test_a_rename_between_unfrozen_paths_passes(repo):
    assert change(repo, move("docs/guide.md", "docs/pseudolife_memory_guide.md")) == []


def test_only_an_exact_same_repository_integrate_branch_is_exempt():
    assert python_freeze.exempt("integrate/2026-11-14-batch-1", "o/r", "o/r")
    assert not python_freeze.exempt("Integrate/2026-11-14-batch-1", "o/r", "o/r")
    assert not python_freeze.exempt("integrate/2026-11-14-batch-1", "fork/r", "o/r")
    assert not python_freeze.exempt("ci/integrate/x", "o/r", "o/r")
    assert not python_freeze.exempt("", "o/r", "o/r")


def test_the_refusal_is_one_line_naming_the_date_the_paths_and_the_fix():
    line = python_freeze.refusal(["pseudolife_memory/cli.py", "pseudolife_memory/web/api.py"])
    assert "\n" not in line
    assert "14 November 2026" in line
    assert "pseudolife_memory/cli.py" in line and "pseudolife_memory/web/api.py" in line
    assert "in Rust" in line
    assert "freeze-exception" in line


def frozen_repo(repo, monkeypatch):
    monkeypatch.chdir(repo)
    write("pseudolife_memory/update_cli.py")(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "update bridge")


ARGS = ["--head-ref", "fix/update-bridge", "--head-repo", "o/r", "--base-repo", "o/r"]


def test_the_freeze_exception_label_admits_a_maintainer_ruled_python_change(repo, monkeypatch, capsys):
    frozen_repo(repo, monkeypatch)
    assert python_freeze.main([*ARGS, "--labels", '["ready-to-batch", "freeze-exception"]']) == 0
    assert "freeze-exception" in capsys.readouterr().out


@pytest.mark.parametrize("labels", ['[]', '["freeze-exceptions"]', '["Freeze-Exception"]', '["freeze exception"]',
                                    '["ready-to-batch"]', '"freeze-exception"', 'not json', ''])
def test_any_other_label_or_none_keeps_the_refusal(repo, monkeypatch, labels):
    # A near miss, a label list without it (as after the label is removed
    # again: the unlabeled event reruns the check), or an unreadable list.
    frozen_repo(repo, monkeypatch)
    assert python_freeze.main([*ARGS, "--labels", labels]) == 1


def test_the_label_on_a_change_that_touches_nothing_frozen_changes_nothing(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    write("rust/shim/src/cli.rs")(repo)
    git(repo, "commit", "-qam", "rust")
    assert python_freeze.main([*ARGS, "--labels", '["freeze-exception"]']) == 0
    assert "none under pseudolife_memory/" in capsys.readouterr().out


def test_main_fails_on_a_frozen_change_and_passes_otherwise(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    write("pseudolife_memory/web/static/index.html")(repo)
    git(repo, "commit", "-qam", "static")
    assert python_freeze.main(["--head-ref", "fix/x", "--head-repo", "o/r", "--base-repo", "o/r"]) == 0
    write("pseudolife_memory/cli.py")(repo)
    git(repo, "commit", "-qam", "python")
    assert python_freeze.main(["--head-ref", "fix/x", "--head-repo", "o/r", "--base-repo", "o/r"]) == 1
    assert "14 November 2026" in capsys.readouterr().out
    assert python_freeze.main(["--head-ref", "integrate/b", "--head-repo", "o/r", "--base-repo", "o/r"]) == 0


def test_an_unreadable_diff_fails_closed(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert python_freeze.main(["--head-ref", "fix/x", "--head-repo", "o/r", "--base-repo", "o/r",
                               "--base", "no-such-ref"]) == 1
    assert "could not read" in capsys.readouterr().out


def test_the_workflow_reruns_on_label_changes_and_passes_the_labels():
    import yaml
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/python-freeze.yml")
                              .read_text(encoding="utf-8"))
    # edited: a pull request retargeted to master is judged again.
    assert workflow[True] == {"pull_request": {"types": ["opened", "synchronize", "reopened", "labeled",
                                                         "unlabeled", "edited"]}}
    assert list(workflow["jobs"]) == ["python-freeze"]
    job = workflow["jobs"]["python-freeze"]
    assert job["name"] == "Python freeze" and "if" not in job and not job.get("continue-on-error")
    checkout, guard = job["steps"]
    assert checkout["with"]["fetch-depth"] == 2
    # The labels as they are when the job runs, never the event's: a rerun
    # replays its event, which may predate the label or its removal.
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert guard["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    assert guard["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "PR_LABELS" not in guard["env"]
    assert "pull_request.labels" not in yaml.safe_dump(workflow)
    assert guard["env"]["HEAD_REPO"] == "${{ github.event.pull_request.head.repo.full_name }}"
    # The base commit's script decides (a PR editing the guard cannot pass
    # its own check); only a base without one falls back to the head's.
    assert 'git show HEAD^1:rust/ci/python_freeze.py > "$script"' in guard["run"]
    assert 'python3 "$script" --head-ref' in guard["run"]
    assert '--labels "$labels"' in guard["run"]


def _label_lines():
    """The guard step's lines that read the labels, up to the script."""
    import yaml
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/python-freeze.yml")
                              .read_text(encoding="utf-8"))
    run = workflow["jobs"]["python-freeze"]["steps"][1]["run"]
    return run.split('script="$RUNNER_TEMP/python_freeze.py"', 1)[0]


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                    reason="the guard step runs in bash on ubuntu-latest; on Windows `bash` may be WSL's")
@pytest.mark.parametrize("answer, code, expected", [
    ('["ready-to-batch","freeze-exception"]', 0, '["ready-to-batch","freeze-exception"]'),
    ("", 1, "[]"),   # the read failed: judge with no labels
], ids=["read", "read-failed"])
def test_the_guard_reads_the_labels_when_it_runs(tmp_path, answer, code, expected):
    fake = tmp_path / "gh"
    fake.write_text(f"#!/usr/bin/env bash\necho \"$*\" > {tmp_path.as_posix()}/argv\n"
                    f"printf '%s' '{answer}'\nexit {code}\n", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    script = _label_lines() + 'printf "%s" "$labels" > "$OUT"\n'
    env = {**os.environ, "BASE_REPO": "owner/repo", "PR_NUMBER": "1537", "OUT": str(tmp_path / "labels"),
           "PATH": f"{tmp_path.as_posix()}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "labels").read_text() == expected
    assert (tmp_path / "argv").read_text().split()[:2] == ["api", "repos/owner/repo/pulls/1537"]
    assert ("could not read" in result.stdout) == bool(code)


@pytest.mark.parametrize("path", [
    "Pseudolife_memory/core.py", "PSEUDOLIFE_MEMORY/cli.py", "pseudolife_Memory/web/api.py",
    "pseudolife_memory/web/Static/index.html", "Pseudolife_memory/web/static/index.html",
    "p\u017feudolife_memory/core.py",   # long s: NFKC-folds to a plain s
])
def test_a_case_or_compatibility_variant_of_the_package_is_refused(path):
    # Reviewer of bf3de877 (N1): on a case-insensitive checkout (Windows, a
    # default macOS volume) a variant writes into the real package directory.
    # The Console build stays exempt only under its exact spelling.
    assert python_freeze.frozen_paths([path]) == [path]


@pytest.mark.parametrize("path", ["PSEUDO~1/core.py", "pseudo~2/cli.py", "Pseudo~10/web/api.py"])
def test_a_windows_short_name_of_the_package_is_refused(path):
    # Reviewer of 6953d0ba: with 8.3 names enabled a Windows checkout writes
    # PSEUDO~1/ into the real package directory.
    assert python_freeze.frozen_paths([path]) == [path]


@pytest.mark.parametrize("path", ["pseudolife_memory_tools/x.py", "docs/Pseudolife_memory.md",
                                  "pseudolife_memory/web/static/assets/App.JS",
                                  "docs/PSEUDO~1/x.py", "pseudo~x/y.py", "pseudo~1.txt", "PSEUDOX~1/a.py"])
def test_case_variants_outside_the_package_still_pass(path):
    assert python_freeze.frozen_paths([path]) == []
