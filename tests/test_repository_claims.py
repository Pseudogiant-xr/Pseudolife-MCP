"""Exact file claims use one lease key across linked Git worktrees."""
import subprocess

import pytest


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repositories(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init")
    git(main, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
        "commit", "--allow-empty", "-m", "Fixture")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "--detach", str(linked))
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init")
    return main, linked, other


def test_repository_claim_tool_dispatches_exact_key(monkeypatch):
    from pseudolife_memory import coordination
    seen = []
    monkeypatch.setattr(coordination, "dispatch", lambda svc, action, args:
                        seen.append((action, args)) or {"state": "held"})
    out = coordination.agents(None, action="claim", repository_id="a" * 64,
                              path="src/file.py")
    assert seen[0][0] == "lease"
    assert seen[0][1]["name"].startswith("claim:file:")
    assert len(seen[0][1]["name"]) <= 120
    assert seen[0][1]["ttl"] == 86400
    assert out["file_claim"] == {"repository_id": "a" * 64, "path": "src/file.py"}
    coordination.agents(None, action="release", **out["file_claim"])
    assert seen[1] == ("release", {"name": seen[0][1]["name"]})


def test_two_worktrees_collide_but_unrelated_repository_does_not(repositories):
    from pseudolife_memory.repository_claims import prepare_file_claim, file_claim_name
    main, linked, other = repositories
    a = prepare_file_claim(str(main), "src/./file.py")
    b = prepare_file_claim(str(linked), "src\\file.py")
    c = prepare_file_claim(str(other), "src/file.py")
    assert a == b
    assert file_claim_name(**a) == file_claim_name(**b)
    assert file_claim_name(**a) != file_claim_name(**c)
    assert a["path"] == "src/file.py"
    assert str(main) not in repr(a)


@pytest.mark.parametrize("path", ["../file", "src/../file", "/file", "C:/file",
                                     "C:file", "\\\\server\\share\\file", "src/**",
                                     "src/[ab]", "src/{a,b}", "", ".", ".git/config",
                                     "file.", "file ", "NUL.txt", "CON .txt",
                                     "file:stream", "file\x00"])
def test_unsafe_or_unsupported_paths_fail_closed(repositories, path):
    from pseudolife_memory.repository_claims import prepare_file_claim
    with pytest.raises(ValueError, match="invalid_claim_path"):
        prepare_file_claim(str(repositories[0]), path)


def test_deleted_and_renamed_paths_keep_separate_exact_names(repositories):
    from pseudolife_memory.repository_claims import prepare_file_claim, file_claim_name
    main, _, _ = repositories
    old = main / "old.txt"
    old.write_text("fixture", encoding="utf-8")
    before = prepare_file_claim(str(main), "old.txt")
    old.rename(main / "new.txt")
    assert before == prepare_file_claim(str(main), "old.txt")
    after = prepare_file_claim(str(main), "new.txt")
    assert file_claim_name(**before) != file_claim_name(**after)
    (main / "new.txt").unlink()
    assert after == prepare_file_claim(str(main), "new.txt")


def test_case_policy_is_shared_across_worktrees(repositories):
    from pseudolife_memory.repository_claims import prepare_file_claim
    main, linked, _ = repositories
    git(main, "config", "core.ignorecase", "true")
    assert prepare_file_claim(str(main), "SRC/File.py") == prepare_file_claim(
        str(linked), "src/file.py")


def test_posix_case_sensitive_policy_keeps_distinct_names(repositories):
    import os
    if os.name == "nt":
        pytest.skip("Windows always uses a conservative case-insensitive claim policy")
    from pseudolife_memory.repository_claims import prepare_file_claim
    main, _, _ = repositories
    git(main, "config", "core.ignorecase", "false")
    assert prepare_file_claim(str(main), "SRC/File.py") != prepare_file_claim(
        str(main), "src/file.py")


@pytest.mark.parametrize("outside", [False, True])
def test_symlink_aliases_are_refused(repositories, tmp_path, outside):
    from pseudolife_memory.repository_claims import prepare_file_claim
    main, _, _ = repositories
    target = tmp_path / "outside" if outside else main / "target"
    target.mkdir()
    link = main / "alias"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("this host cannot create directory symlinks")
    with pytest.raises(ValueError, match="invalid_claim_path"):
        prepare_file_claim(str(main), "alias/file.py")


def test_existing_directory_is_not_an_exact_file_claim(repositories):
    from pseudolife_memory.repository_claims import prepare_file_claim
    main, _, _ = repositories
    (main / "src").mkdir()
    with pytest.raises(ValueError, match="invalid_claim_path"):
        prepare_file_claim(str(main), "src")


def test_separate_git_directory_and_linked_worktree_metadata_are_refused(tmp_path):
    from pseudolife_memory.repository_claims import FileClaimError, prepare_file_claim
    root = tmp_path / "checkout"
    root.mkdir()
    metadata = root / "_gitstate"
    git(root, "init", "--separate-git-dir=" + str(metadata))
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
        "commit", "--allow-empty", "-m", "fixture")
    linked = root / "linked"
    git(root, "worktree", "add", "-b", "linked", str(linked))
    for path in ("_gitstate/config", "_gitstate/missing/file", "_gitstate/worktrees/linked/gitdir"):
        with pytest.raises(FileClaimError, match="^invalid_claim_path$"):
            prepare_file_claim(str(root), path)
    assert prepare_file_claim(str(root), "ordinary/missing.py") == prepare_file_claim(
        str(linked), "ordinary/missing.py")


def test_windows_junction_is_refused(repositories, tmp_path):
    import os
    if os.name != "nt":
        pytest.skip("Windows junction behavior")
    from pseudolife_memory.repository_claims import prepare_file_claim
    main, _, _ = repositories
    target = tmp_path / "junction-target"
    target.mkdir()
    alias = main / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)],
                   check=True, capture_output=True)
    with pytest.raises(ValueError, match="invalid_claim_path"):
        prepare_file_claim(str(main), "junction/file.py")


def test_unicode_equivalent_spellings_collide(repositories):
    from pseudolife_memory.repository_claims import prepare_file_claim
    main, _, _ = repositories
    assert prepare_file_claim(str(main), "caf\u00e9.py") == prepare_file_claim(
        str(main), "cafe\u0301.py")


def test_missing_long_path_has_a_bounded_lease_name(repositories):
    from pseudolife_memory.repository_claims import prepare_file_claim, file_claim_name
    path = "/".join(["missing-directory"] * 35) + "/file.py"
    claim = prepare_file_claim(str(repositories[0]), path)
    assert len(file_claim_name(**claim)) == 75


def test_invalid_repository_does_not_echo_local_path(tmp_path):
    from pseudolife_memory.repository_claims import prepare_file_claim
    with pytest.raises(ValueError) as caught:
        prepare_file_claim(str(tmp_path), "file.py")
    assert str(caught.value) == "invalid_repository"


def test_casefold_result_is_canonical_unicode(repositories):
    from pseudolife_memory.repository_claims import prepare_file_claim, file_claim_name
    main, _, _ = repositories
    git(main, "config", "core.ignorecase", "true")
    assert len(file_claim_name(**prepare_file_claim(str(main), "\u01f0.py"))) == 75


def test_distinct_repositories_with_casefold_equivalent_names_stay_separate(tmp_path):
    from pseudolife_memory.repository_claims import prepare_file_claim
    a, b = tmp_path / "repo-ss", tmp_path / "repo-\u00df"
    a.mkdir()
    b.mkdir()
    git(a, "init")
    git(b, "init")
    assert prepare_file_claim(str(a), "file.py")["repository_id"] != prepare_file_claim(
        str(b), "file.py")["repository_id"]


def test_local_arguments_are_prepared_without_forwarding_checkout(repositories):
    from pseudolife_memory.repository_claims import prepare_claim_arguments
    main, linked, _ = repositories
    a = prepare_claim_arguments("memory_agents", {
        "action": "claim", "worktree": str(main), "path": "file.py", "status": "editing"})
    b = prepare_claim_arguments("memory_agents", {
        "action": "release", "worktree": str(linked), "path": "file.py"})
    assert "worktree" not in a and "worktree" not in b
    assert a["repository_id"] == b["repository_id"]
    assert a["path"] == b["path"]
    assert a["status"] == "editing"


def test_ambient_git_directory_does_not_redirect_identity(repositories, monkeypatch):
    from pseudolife_memory.repository_claims import prepare_file_claim
    main, _, other = repositories
    before = prepare_file_claim(str(main), "file.py")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert before == prepare_file_claim(str(main), "file.py")


@pytest.mark.parametrize("kwargs,code", [
    ({"action": "update", "repository_id": "a" * 64, "path": "file"}, "unexpected_parameter"),
    ({"action": "claim", "lease": "gpu", "repository_id": "a" * 64, "path": "file"},
     "unexpected_parameter"),
    ({"action": "claim", "repository_id": "bad", "path": "file"}, "invalid_repository_id"),
    ({"action": "claim", "repository_id": "a" * 64, "path": "src/../file"}, "invalid_claim_path"),
    ({"action": "claim", "worktree": "/checkout", "path": "file"}, "file_claim_requires_local_client"),
])
def test_model_refuses_unsupported_claim_inputs(kwargs, code):
    from pseudolife_memory import coordination
    with pytest.raises(ValueError, match=code):
        coordination.agents(None, **kwargs)


def test_conflict_holder_includes_fence_without_changing_generic_leases():
    from pseudolife_memory.storage.coordination import CoordinationStore
    row = {"name": "claim:file:fixture", "holder_agent_id": "fixture-agent",
           "holder_principal": "alice", "purpose": "editing", "acquired_at": 1000,
           "expires_at": 1120, "expected_end": None, "fence": 42}
    holder = CoordinationStore._holder(row)
    assert holder["agent_id"] == "fixture-agent"
    assert holder["fence"] == 42 and holder["expires_at"] == 1120
    assert "fence" not in CoordinationStore._holder({**row, "name": "gpu"})
