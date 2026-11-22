"""Checks for the hosted acceptance evidence and workflow boundary."""

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from ops.acceptance import hosted, matrix


def test_hosted_acceptance_is_dispatch_only():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/hosted-acceptance.yml").read_text(encoding="utf-8"))
    events = workflow.get(True) or workflow["on"]
    assert "workflow_dispatch" in events
    assert {"release", "columns"} == set(events["workflow_dispatch"]["inputs"])
    assert workflow["permissions"] == {"contents": "read"}
    # The authorized hosted rehearsal has a separate branch-limited trigger.
    if "push" in events:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=root, text=True,
        ).strip()
        assert branch == "acceptance-scratch/w85-hosted"
        assert set(events) == {"workflow_dispatch", "push"}
        assert events["push"] == {"branches": [branch]}
    else:
        assert set(events) == {"workflow_dispatch"}


def test_pending_receipt_does_not_copy_process_output(tmp_path):
    (tmp_path / "ledger").mkdir()
    row = hosted.Receipt("ordinary-native-use", "linux-arm64", daemon=hosted.DAEMONS[1])
    row.observe("approved command", hosted.Result(0, b"fixture raw output", 0.2), True, "Fixed assertion.")
    row.note = "A required witness remains pending."
    row.write(tmp_path, None, True)
    receipt = json.loads((tmp_path / "ordinary-native-use.json").read_text(encoding="utf-8"))
    assert "fixture raw output" not in json.dumps(receipt)
    assert receipt["daemon"] == "built on the runner by the tag's installer (native arm64)"
    assert receipt["steps"][0]["exit_code"] == 0
    record = tmp_path / "ledger" / "ordinary-native-use--linux-arm64.json"
    assert json.loads(record.read_text(encoding="utf-8")) == matrix.pending(row.scenario, row.platform)


def test_summary_allows_only_known_status_and_peer_names(tmp_path, capsys):
    folder = tmp_path / "acceptance-macos-arm64"
    folder.mkdir()
    (folder / "clean-install.json").write_text(
        json.dumps({"status": "pass", "daemon": hosted.DAEMONS[3]}), encoding="utf-8")
    (folder / "ordinary-native-use.json").write_text(
        json.dumps({"status": "pass", "daemon": "fixture raw output"}), encoding="utf-8")
    hosted.summary("macos-arm64", tmp_path)
    rendered = capsys.readouterr().out
    assert "pass / source-built from the tag" in rendered
    assert "invalid result" in rendered
    assert "fixture raw output" not in rendered
    assert len(rendered.splitlines()) == 2 + len(matrix.SCENARIOS)


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_docker_volume_inventory_uses_a_supported_format(monkeypatch):
    from ops.acceptance import hosted_linux

    def docker(*args):
        if args[0] == "volume" and "--no-trunc" in args:
            return hosted.Result(125, b"unknown flag", 0)
        return hosted.Result(0, b"fixture\n", 0)

    monkeypatch.setattr(hosted_linux, "docker", docker)
    assert hosted_linux.DockerFixture.inventory() == {
        "container": {"fixture"}, "network": {"fixture"}, "volume": {"fixture"},
    }


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_install_tail_redacts_fixture_details():
    from ops.acceptance.hosted_linux import installer_tail

    assert installer_tail(b"Update later with one command: /tmp/private-fixture update\n"
                          b"unknown command output: fixture-value\n"
                          b"WARNING: Codex MCP registration was skipped because credential setup failed.\n") == [
        "Update later with one command: <launcher> update.",
        "<unrecognized line redacted>",
        "WARNING: Codex MCP registration was skipped because credential setup failed.",
    ]


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_binfmt_gate_does_not_parse_listing_output(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux

    (tmp_path / "status").write_text("enabled\n")
    (tmp_path / "register").touch()
    (tmp_path / "emulator").write_text("enabled\ninterpreter /usr/bin/qemu-x86_64\n")
    monkeypatch.setattr(hosted_linux, "command", lambda *_: hosted.Result(0, "ls output —\n".encode(), 0))
    assert hosted_linux.binfmt_status(tmp_path) == {
        "command": "ls /proc/sys/fs/binfmt_misc", "exit_code": 0,
        "handlers": 1, "amd64_handler_registered": True,
    }
