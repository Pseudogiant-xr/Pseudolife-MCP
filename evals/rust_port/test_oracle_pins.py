"""A PR must not leave the next PR's selected Python outside its byte pins."""
import json
import subprocess
import sys

import pytest

from . import oracle_selection as oracle
from . import oracle_pins as pins
from .test_release_oracle import commit, git, write


@pytest.fixture
def pinned_repo(tmp_path):
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "core.autocrlf", "false")
    for name in ("cli.py", "wait_mail_cli.py"):
        relative = "pseudolife_memory/" + name
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "show", "HEAD:" + relative], cwd=oracle.ROOT))
    base = commit(tmp_path)
    return tmp_path, base


def select_cli(monkeypatch, root, base, head, tmp_path, event_name="pull_request", checkout=None):
    event = ({"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
             if event_name == "pull_request" else {"after": head})
    payload, receipt, environment = (tmp_path / name for name in ("event.json", "selection.json", "env"))
    payload.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["selection", "--root", str(root), "--event-name", event_name,
                                     "--event-path", str(payload), "--checkout-head", checkout or head,
                                     "--out", str(receipt), "--github-env", str(environment)])
    oracle.main()
    return json.loads(receipt.read_text(encoding="utf-8"))


@pytest.mark.parametrize("event_name", ["pull_request", "push"])
@pytest.mark.parametrize("name", ["cli.py", "wait_mail_cli.py"])
def test_originating_change_fails_before_exporting_oracle(
        pinned_repo, monkeypatch, tmp_path, name, event_name):
    root, base = pinned_repo
    with (root / "pseudolife_memory" / name).open("a", encoding="utf-8") as source:
        source.write("# New production bytes need an explicit oracle acceptance.\n")
    head = commit(root)
    # The historical PR oracle still selects the accepted base, which used to
    # hide the incompatible head until the next PR selected it as its base.
    if event_name == "pull_request":
        assert oracle.select(root, event_name, {
            "pull_request": {"base": {"sha": base}, "head": {"sha": head}}}, head)["oracle_head"] == base
    with pytest.raises(ValueError, match="unaccepted Python oracle blob.*" + name):
        select_cli(monkeypatch, root, base, head, tmp_path, event_name)
    assert not (tmp_path / "selection.json").exists()
    assert not (tmp_path / "env").exists()


def test_unrelated_change_preserves_merge_base_and_export(pinned_repo, monkeypatch, tmp_path):
    root, base = pinned_repo
    write(root, "rust/change.txt", "Native-only change.\n")
    head = commit(root)
    selected = select_cli(monkeypatch, root, base, head, tmp_path)
    assert selected["oracle_head"] == base
    assert selected["candidate_head"] == head


def test_deleted_protected_source_is_rejected(pinned_repo, monkeypatch, tmp_path):
    root, base = pinned_repo
    (root / "pseudolife_memory/cli.py").unlink()
    head = commit(root)
    with pytest.raises(ValueError, match="unaccepted Python oracle blob.*cli.py"):
        select_cli(monkeypatch, root, base, head, tmp_path)


@pytest.mark.parametrize("changed_head", ["candidate", "checkout"])
def test_candidate_and_integrated_checkout_are_checked_independently(
        pinned_repo, monkeypatch, tmp_path, changed_head):
    root, base = pinned_repo
    write(root, "pseudolife_memory/cli.py", "# Unaccepted production bytes.\n")
    changed = commit(root)
    if changed_head == "candidate":
        git(root, "checkout", "--quiet", "--detach", base)
        head, checkout = changed, base
    else:
        head, checkout = base, changed
    with pytest.raises(ValueError, match="unaccepted Python oracle blob.*cli.py"):
        select_cli(monkeypatch, root, base, head, tmp_path, checkout=checkout)


def test_explicit_acceptance_updates_the_gate_and_judge_together(pinned_repo, monkeypatch, tmp_path):
    root, base = pinned_repo
    relative = "pseudolife_memory/cli.py"
    (root / relative).write_bytes(b"# Reviewed production bytes.\n")
    head = commit(root)
    accepted = (*pins.ACCEPTED_BLOBS[relative], git(root, "rev-parse", head + ":" + relative))
    monkeypatch.setitem(pins.ACCEPTED_BLOBS, relative, accepted)
    assert select_cli(monkeypatch, root, base, head, tmp_path)["oracle_head"] == base
    pins.require_pinned_files(root)


@pytest.mark.parametrize("head", ["eb0c13e9c5036aa2b95e7fccb77f41ca1c095493",
                                  "39e8d7ad548302e158ea4d8d2d87190aa8204ee2"])
def test_existing_wait_mail_acceptances_are_preserved(head):
    pins.require_pinned_head(oracle.ROOT, head)


def test_judge_preserves_crlf_normalization_and_rejects_dirty_source(pinned_repo):
    root, _ = pinned_repo
    for relative in pins.ACCEPTED_BLOBS:
        source = root / relative
        source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    pins.require_pinned_files(root)
    with (root / "pseudolife_memory/cli.py").open("ab") as source:
        source.write(b"# Dirty source.\n")
    with pytest.raises(ValueError, match="unaccepted Python oracle blob.*cli.py"):
        pins.require_pinned_files(root)
