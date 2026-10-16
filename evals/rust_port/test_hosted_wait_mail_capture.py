"""Dispatch selectors refuse unbounded inputs and changed source checkouts."""
import subprocess

import pytest

from evals.rust_port.hosted_wait_mail_capture import parse_args, require_frozen_checkout, validate_selector

HEAD = "a" * 40
LONG_RING = "wait-mail-long-ring-watermark"


@pytest.mark.parametrize("case_list", [LONG_RING, "wait-mail-unicode-delivery," + LONG_RING,
    LONG_RING + ",wait-mail-bad-ring-reason,wait-mail-unicode-delivery"])
def test_named_case_order_is_preserved(case_list):
    assert validate_selector("wait-mail", case_list, HEAD) == case_list.split(",")


@pytest.mark.parametrize("case_list", ["", "*", "../case.json", "wait-mail-help", LONG_RING + ",",
    LONG_RING + "," + LONG_RING, LONG_RING + ", wait-mail-bad-ring-reason",
    LONG_RING + ",wait-mail-bad-ring-reason,wait-mail-unicode-delivery," + LONG_RING,
    LONG_RING + "\n", "$(whoami)"])
def test_case_list_rejects_unknown_duplicate_or_unbounded_inputs(case_list):
    with pytest.raises(ValueError, match="case-list"):
        validate_selector("wait-mail", case_list, HEAD)


@pytest.mark.parametrize("head", ["", "a" * 39, "a" * 41, "A" * 40, "g" * 40, HEAD + "\n", "master", "--help"])
def test_frozen_head_requires_exact_full_lowercase_hex(head):
    with pytest.raises(ValueError, match="frozen-head"):
        validate_selector("wait-mail", LONG_RING, head)


def test_mode_and_cli_validation_refuse_before_capture(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        validate_selector("lease", LONG_RING, HEAD)
    arguments = ["--mode", "wait-mail", "--case-list", "*", "--frozen-head", HEAD,
                 "--oracle-root", str(tmp_path), "--candidate-root", str(tmp_path),
                 "--candidate", str(tmp_path / "native.exe"), "--out", str(tmp_path / "receipt.json")]
    with pytest.raises(SystemExit) as error:
        parse_args(arguments)
    assert error.value.code == 2
    assert not (tmp_path / "receipt.json").exists()


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked", "wrong-head"])
def test_changed_checkout_is_refused(tmp_path, dirty_kind):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    tracked = tmp_path / "fixture.txt"
    tracked.write_text("original\n")
    subprocess.run(["git", "add", "fixture.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                    "commit", "--quiet", "-m", "fixture"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path).decode().strip()
    require_frozen_checkout(tmp_path, head)
    if dirty_kind == "tracked":
        tracked.write_text("changed\n")
    elif dirty_kind == "untracked":
        (tmp_path / "extra.txt").write_text("untracked\n")
    else:
        head = "0" * 40
    with pytest.raises(RuntimeError, match="exact clean frozen checkout"):
        require_frozen_checkout(tmp_path, head)
