"""Checks for the hosted acceptance evidence and workflow boundary."""

from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import yaml

from ops.acceptance import hosted, matrix


@pytest.fixture(autouse=True)
def isolated_redactor():
    """Values a test registers as secrets stay out of the next test's output."""
    saved = dict(hosted.REDACTOR.values)
    yield
    hosted.REDACTOR.values.clear()
    hosted.REDACTOR.values.update(saved)


def test_hosted_acceptance_is_dispatch_only():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/hosted-acceptance.yml").read_text(encoding="utf-8"))
    events = workflow.get(True) or workflow["on"]
    assert set(events) == {"workflow_dispatch"}
    assert {"release", "columns"} == set(events["workflow_dispatch"]["inputs"])
    assert workflow["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in workflow["jobs"].values())


@pytest.mark.skipif(os.name != "posix", reason="The hosted subprocess harness is POSIX-only")
def test_command_timeout_bounds_pipe_drain_and_closes_streams(monkeypatch):
    process = Mock(pid=123, stdin=BytesIO(), stdout=BytesIO(), stderr=BytesIO(), returncode=None)
    process.communicate.side_effect = [subprocess.TimeoutExpired("fixture", 1),
                                       subprocess.TimeoutExpired("fixture", 5, output=b"partial")]
    process.wait.side_effect = subprocess.TimeoutExpired("fixture", 5)
    kill = Mock()
    monkeypatch.setattr(hosted.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(hosted.os, "killpg", kill)
    result = hosted.command(["fixture"], timeout=1)
    assert (result.code, result.output) == (124, b"partial")
    assert process.communicate.call_args_list == [call(None, timeout=1), call(timeout=5)]
    process.wait.assert_called_once_with(timeout=5)
    kill.assert_called_once_with(process.pid, hosted.signal.SIGKILL)
    assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


def test_pending_receipt_does_not_copy_process_output(tmp_path):
    (tmp_path / "ledger").mkdir()
    row = hosted.Receipt("ordinary-native-use", "linux-arm64", daemon=hosted.DAEMONS[1])
    row.observe("approved command", hosted.Result(0, b"fixture raw output", 0.2), True, "Fixed assertion.")
    row.note = "A required witness remains pending."
    row.write(tmp_path, None, True)
    receipt = json.loads((tmp_path / "ordinary-native-use.json").read_text(encoding="utf-8"))
    assert "fixture raw output" not in json.dumps(receipt)
    assert receipt["daemon"] == "Linux arm64 checkout build"
    assert receipt["steps"][0]["exit_code"] == 0
    record = tmp_path / "ledger" / "ordinary-native-use--linux-arm64.json"
    assert json.loads(record.read_text(encoding="utf-8")) == matrix.pending(row.scenario, row.platform)


def test_summary_allows_only_known_status_and_peer_names(tmp_path, capsys):
    folder = tmp_path / "acceptance-macos-arm64"
    folder.mkdir()
    (folder / "clean-install.json").write_text(
        json.dumps({"status": "pass", "daemon": hosted.DAEMONS[3], "daemon_engine": "rust"}), encoding="utf-8")
    (folder / "ordinary-native-use.json").write_text(
        json.dumps({"status": "not yet run", "daemon": "fixture raw output"}), encoding="utf-8")
    (folder / "bank-recovery.json").write_text(
        json.dumps({"status": "pass", "daemon": hosted.DAEMONS[3]}), encoding="utf-8")
    hosted.summary("macos-arm64", tmp_path)
    rendered = capsys.readouterr().out
    assert "pass / source-built from the tag / rust" in rendered
    assert "invalid result" in rendered
    assert "| bank-recovery | not selected | not selected | invalid result |" in rendered.splitlines()
    assert "fixture raw output" not in rendered
    assert len([line for line in rendered.splitlines() if line.startswith("| ")]) == 2 + len(matrix.SCENARIOS)


def test_checkpoint_keeps_pass_pending_until_cleanup_and_records_daemon(tmp_path):
    (tmp_path / "ledger").mkdir()
    release = SimpleNamespace(commit="0" * 40, digest="1" * 64,
                              archive=Path("pseudolife-mcp-0.25.0-aarch64-unknown-linux-gnu.tar.gz"))
    row = hosted.Receipt("clean-install", "linux-arm64", daemon=hosted.DAEMONS[1], daemon_engine="python")
    row.commands = [{"command": command, "exit_code": 0} for command in matrix.commands(row.scenario, row.platform)]
    row.status = "pass"
    row.note = matrix.SCENARIOS[row.scenario].outcome
    path = tmp_path / "clean-install.json"
    row.checkpoint(tmp_path, release)
    provisional = json.loads(path.read_bytes())
    assert (provisional["status"], provisional["cleanup_verified"]) == ("not yet run", False)
    row.write(tmp_path, release, True)
    assert json.loads(path.read_bytes())["status"] == "pass"
    recorded = json.loads((tmp_path / "ledger" / "clean-install--linux-arm64.json").read_bytes())
    assert (recorded["status"], recorded["daemon"], recorded["daemon_engine"]) == ("pass", row.daemon, "python")


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


@pytest.mark.skipif(os.name != "posix", reason="The staged executable path is POSIX-only")
def test_native_path_skips_a_protected_system_entry(tmp_path, monkeypatch):
    folder = tmp_path / "system"
    folder.mkdir()
    protected = folder / "protected"
    protected.touch()
    tool = folder / "fixture-tool"
    tool.touch(mode=0o755)
    original = Path.is_file

    def is_file(path):
        if path == protected:
            raise PermissionError
        return original(path)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(os, "defpath", str(folder))
    hosted.native_path(tmp_path, tmp_path)
    assert (tmp_path / "native-bin" / tool.name).is_symlink()
    assert not (tmp_path / "native-bin" / protected.name).is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="The installer fixture is POSIX-only")
def test_fault_fixture_restores_the_previously_verified_archive(tmp_path):
    from types import SimpleNamespace

    from ops.acceptance.hosted_linux import displaced_archive

    release = SimpleNamespace(version="0.25.0", archive=Path("fixture.tar.gz"))
    profile = SimpleNamespace(folder=tmp_path / "home")
    archive = profile.folder / ".pseudolife-mcp" / "downloads" / release.version / release.archive.name
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"verified archive fixture")
    with displaced_archive(release, profile, tmp_path):
        assert not archive.exists()
        archive.write_bytes(b"disposable replacement")
    assert archive.read_bytes() == b"verified archive fixture"


def test_peer_fetch_transport_failure_allows_offline_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(hosted, "command", lambda *_, **__: hosted.Result(22, b"503", 0))
    with pytest.raises(hosted.Unavailable, match="HTTP 503"):
        hosted.download("https://example.com/asset", tmp_path / "fixture.tar.gz", peer_asset=True)


@pytest.mark.parametrize(("code", "status", "error"), [
    (6, b"000", hosted.Unavailable), (22, b"403", hosted.Unavailable),
    (22, b"429", hosted.Unavailable), (22, b"503", hosted.Unavailable), (22, b"404", hosted.Failed),
])
def test_release_metadata_distinguishes_temporary_failure_from_missing_tag(tmp_path, monkeypatch, code, status, error):
    monkeypatch.setattr(hosted, "command", lambda *_, **__: hosted.Result(code, status, 0))
    with pytest.raises(error, match=f"HTTP {status.decode()}"):
        hosted.published_release("v0.25.0", "linux-arm64", tmp_path / "release")


@pytest.mark.skipif(os.name != "posix", reason="The Mac acceptance harness is POSIX-only")
def test_offline_installer_rejects_a_false_endpoint_witness(tmp_path, monkeypatch):
    from ops.acceptance import hosted_macos

    profile = hosted.Profile(tmp_path / "home", "")
    result = hosted.Result(1, b"release archive checksum mismatch", 0)
    monkeypatch.setattr(hosted_macos, "live_install", lambda *a, **k: result)
    row = hosted.Receipt("clean-install", "macos-arm64")
    with pytest.raises(hosted.Failed, match="closed endpoint"):
        hosted_macos.offline(row, None, tmp_path, profile, tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_stock_update_uses_json_for_its_requested_rollback(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    previous = {"version": "0.24.0", "authenticated": True, "id": "old", "image": "old",
                "volumes": ["state"], "state_volumes": ["state"], "database_volumes": ["bank"],
                "config": "unchanged", "runtime": {}}
    upgraded = previous | {"version": "0.25.0", "id": "new", "image": "new"}
    snapshots = iter((previous, upgraded))
    monkeypatch.setattr(linux, "daemon_snapshot", lambda *_: next(snapshots))
    reports = []
    monkeypatch.setattr(linux, "docker_rollback", lambda row, peer, before, report: reports.append(report))

    def update(arguments, **_):
        result_file = Path(arguments[arguments.index("--result-file") + 1])
        result_file.write_text("0\n")
        return hosted.Result(0, json.dumps({"rollback": {"tag": "fixture"}, "steps": ["updated"]}).encode(), 0)

    monkeypatch.setattr(linux, "command", update)
    peer = SimpleNamespace(owner_executable="fixture", owner_env={}, during=lambda _, run: run())
    row = hosted.Receipt("upgrade-and-rollback", "linux-arm64")
    with pytest.raises(hosted.Unavailable, match="v0.23.x"):
        linux.stock_upgrade(row, SimpleNamespace(version="0.25.0"), tmp_path, None, peer, tmp_path)
    assert reports == [{"rollback": {"tag": "fixture"}, "steps": ["updated"]}]


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_daemon_snapshot_tracks_bank_and_state_mounts(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    def inspect(_, name):
        mount = "bank" if name == linux.POSTGRES else "state"
        return hosted.Result(0, json.dumps([{"Mounts": [{"Type": "volume", "Name": mount}]}]).encode(), 0)

    monkeypatch.setattr(linux, "docker", inspect)
    monkeypatch.setattr(linux, "get", lambda _: ({}, {"version": "0.24.0"}))
    peer = SimpleNamespace(url="fixture", checkout=tmp_path, sample=lambda: {"status": 200})
    snapshot = linux.daemon_snapshot(peer, SimpleNamespace(fingerprint=lambda: {}))
    assert snapshot["database_volumes"] == ["bank"]
    assert snapshot["state_volumes"] == ["state"]


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
@pytest.mark.parametrize("digest_suffix", ["", "@sha256:" + "a" * 64])
def test_release_rollback_uses_a_tag_only_ghcr_reference(tmp_path, monkeypatch, digest_suffix):
    from ops.acceptance import hosted_linux as linux

    calls = []

    def docker(*argv, **options):
        calls.append((argv, options))
        return hosted.Result(0, b"old\n", 0)

    monkeypatch.setattr(linux, "docker", docker)
    monkeypatch.setattr(linux, "readiness", lambda _: True)
    monkeypatch.setattr(linux, "get", lambda _: ({}, {"version": "0.24.0"}))
    peer = SimpleNamespace(checkout=tmp_path, url="fixture", sample=lambda: {"status": 200})
    row = hosted.Receipt("upgrade-and-rollback", "linux-arm64")
    report = {"rollback": {"tag": f"ghcr.io/pseudogiant-xr/pseudolife-daemon:0.24.0-pre-update-fixture{digest_suffix}"}}
    if digest_suffix:
        with pytest.raises(hosted.Failed, match="documented Compose route"):
            linux.docker_rollback(row, peer, {"image": "old", "version": "0.24.0"}, report)
        assert all(argv[0] != "compose" for argv, _ in calls)
    else:
        linux.docker_rollback(row, peer, {"image": "old", "version": "0.24.0"}, report)
        assert calls[1][1]["env"] == {"PSEUDOLIFE_IMAGE_TAG": "0.24.0-pre-update-fixture"}


@pytest.mark.skipif(os.name != "posix", reason="The peer harness is POSIX-only")
def test_peer_postgres_password_is_piped_to_initdb(tmp_path, monkeypatch):
    from ops.acceptance import hosted_source as source

    monkeypatch.setattr(source.secrets, "token_hex", lambda _: "fixture")
    calls = []

    def initdb(argv, **options):
        calls.append((argv, options))
        assert list(tmp_path.iterdir()) == []
        return hosted.Result(1, b"", 0)

    build = source.Build("macos-arm64", tmp_path / "daemon", tmp_path, {}, "1.27.0")
    budget = SimpleNamespace(run=initdb, refuse=hosted.Unavailable)
    with pytest.raises(hosted.Unavailable, match="fixture could not initialize"):
        with source.source_peer(build, None, None, None, tmp_path, budget):
            pytest.fail("initdb's failure should prevent the peer from starting")
    argv, options = calls[0]
    assert argv[argv.index("--pwfile") + 1] == "/dev/stdin"
    assert options["data"] == b"fixture\n"


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_service_cleanup_checks_owned_unit_state(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    profile = hosted.Profile(tmp_path / "home", "")
    units = profile.folder / "config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "pseudolife-fixture.service").touch()
    monkeypatch.setattr(linux, "command", lambda *a, **k: hosted.Result(0, b"active\n", 0))
    assert linux.stop_units(profile) is False


def test_service_cleanup_failure_cannot_be_marked_verified(tmp_path):
    (tmp_path / "ledger").mkdir()
    row = hosted.Receipt("upgrade-and-rollback", "linux-arm64", cleanup_failed=True)
    row.status = "fail"
    row.write(tmp_path, None, True)
    receipt = json.loads((tmp_path / "upgrade-and-rollback.json").read_bytes())
    assert receipt["cleanup_verified"] is False


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_stock_peer_keeps_installer_failure_when_cleanup_fails(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    release = SimpleNamespace(tag="v0.24.0", checkout=lambda _: tmp_path)
    row = hosted.Receipt("upgrade-and-rollback", "linux-arm64")
    monkeypatch.setattr(linux, "DockerFixture", lambda: SimpleNamespace(clean=lambda: False))
    monkeypatch.setattr(linux, "stop_units", lambda _: False)
    monkeypatch.setattr(linux, "install", lambda *args, **kwargs: hosted.Result(1, b"", 0))
    with pytest.raises(hosted.Failed, match="The tagged stock installer left a healthy daemon"):
        with linux.stock_peer(row, release, "", tmp_path):
            pytest.fail("the stock installer failed")
    assert row.cleanup_failed is True


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_failed_backup_retains_its_ledger_command(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    @contextmanager
    def session(*_):
        yield SimpleNamespace(tool=lambda *a, **k: {})

    monkeypatch.setattr(linux, "session", session)
    monkeypatch.setattr(linux, "command", lambda *a, **k: hosted.Result(4, b"", 0))
    peer = SimpleNamespace(owner_executable="fixture", owner_env={}, checkout=tmp_path)
    row = hosted.Receipt("bank-recovery", "linux-arm64")
    with pytest.raises(hosted.Failed):
        linux.bank_recovery(row, None, tmp_path, None, peer, tmp_path)
    assert row.commands == [{"command": matrix.commands(row.scenario, row.platform)[0], "exit_code": 4}]


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_failed_stock_install_does_not_hide_the_upgrade_row(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    @contextmanager
    def baseline(*_):
        yield ({"running_containers": [], "daemon_healthy": False},
               hosted.Result(1, b"", 0), tmp_path, None)

    calls = []

    @contextmanager
    def stock_peer(*_):
        calls.append("legacy")
        yield SimpleNamespace(checkout=tmp_path), object()

    def not_run(*_):
        raise hosted.Unavailable("fixture")

    candidate = SimpleNamespace(tag="v0.25.0", commit="0" * 40, digest="0" * 64,
                                archive=Path("pseudolife-mcp-0.25.0-aarch64-unknown-linux-gnu.tar.gz"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(machine="aarch64"))
    monkeypatch.setattr(linux, "baseline", baseline)
    monkeypatch.setattr(linux, "stock_peer", stock_peer)
    monkeypatch.setattr(linux, "wait_stock_ports", lambda *_: None)
    monkeypatch.setattr(linux, "stock_upgrade", not_run)
    monkeypatch.setattr(linux, "DockerFixture", lambda: SimpleNamespace(clean=lambda: True))
    monkeypatch.setattr(linux, "free_space", lambda *_: None)
    monkeypatch.setattr(linux, "published_release", lambda *a: candidate)
    monkeypatch.setattr(linux, "prepare_codex", lambda *a: tmp_path)
    monkeypatch.setattr(linux, "native_path", lambda *a: "")
    (tmp_path / "ledger").mkdir()
    assert linux.run("v0.25.0", "linux-arm64", tmp_path) == 1
    assert calls == ["legacy"]
    for name in linux.CLIENT_ROWS:
        row = json.loads((tmp_path / f"{name}.json").read_bytes())
        assert (row["status"], row["assertion"]) == ("not yet run", linux.NOT_SUPPORTED)
    clean = json.loads((tmp_path / "clean-install.json").read_bytes())
    assert clean["status"] == "fail"
    upgrade = json.loads((tmp_path / "upgrade-and-rollback.json").read_bytes())
    assert upgrade["status"] == "not yet run"


# --- Receipts that explain a failure (sanitised tails) ---

def planted_output():
    """Values a failed step's output can carry, each with its removing rule."""
    token = "f" * 8 + "0123456789abcdef" * 3
    github = "gh" + "p_" + "Fixture0Token1Value2Abc3"
    windows_home = "C" + ":" + "\\" + "Users" + "\\" + "fixture-person"
    planted = {
        "values": "registered-pairing-code",
        "pem": "MIIEfixtureprivatekeybody",
        "userinfo": "fixture-db-password",
        "authorization": "fixture.bearer.value",
        "assignment": "fixture-assigned-secret",
        "prefixed": github,
        "hex": token,
        "opaque": "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "keyline": "kfixtureBaseSixtyFourKEYmaterialLineWithoutAHeaderXYZ",
        "query": "X-Amz-Signature=fixture",
        "email": "fixture.person@example.com",
        "home": "fixture-person",
        "mdns": "fixture-mac-runner.local",
        "address": "10.0.0.7",
    }
    lines = [
        "model provisioning phase: installing pinned artifacts",
        "pairing code registered-pairing-code issued at 10.0.0.7:8765",
        "-----BEGIN PRIVATE KEY-----",
        "MIIEfixtureprivatekeybody",
        "-----END PRIVATE KEY-----",
        "kfixtureBaseSixtyFourKEYmaterialLineWithoutAHeaderXYZ",
        "connecting to postgresql://acceptance:fixture-db-password@127.0.0.1:5432/postgres",
        "Authorization: Bearer fixture.bearer.value",
        "PSEUDOLIFE_MCP_TOKEN=fixture-assigned-secret",
        f"pushed with {github} as fixture.person@example.com",
        f"token {token} and AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        f"cache {windows_home}\\AppData and /Users/fixture-person/Library on fixture-mac-runner.local",
        "redirected to https://cas-bridge.example.com/xet/model?X-Amz-Signature=fixture&Expires=1",
        "could not download https://huggingface.co/onnx-community/ONNX_Qwen3-Embedding-0.6B/resolve/main/"
        "model.onnx_data (HTTP status client error (429 Too Many Requests)); exit code: 5",
        "write failed: No space left on device",
    ]
    return "\n".join(lines).encode(), planted


KEPT = ("HTTP status client error (429 Too Many Requests)", "No space left on device",
        "huggingface.co/onnx-community/ONNX_Qwen3-Embedding-0.6B/resolve/main/model.onnx_data",
        "exit code: 5", "127.0.0.1:5432", "model provisioning phase")
RULES = ("values", "pem", "keyline", "userinfo", "authorization", "assignment", "prefixed", "hex", "opaque", "query",
         "email", "home", "mdns", "address")


def fixture_redactor(disabled=frozenset()):
    from ops.acceptance.sanitize import Redactor

    redactor = Redactor(disabled=frozenset(disabled))
    redactor.secret("registered-pairing-code")
    return redactor


def test_sanitised_tail_removes_planted_secrets_and_keeps_the_cause():
    output, planted = planted_output()
    failure = fixture_redactor().failure("pseudolife-daemon provision models", 1, output, lines=40)
    text = json.dumps(failure)
    for rule, value in planted.items():
        assert value not in text, rule
    for kept in KEPT:
        assert kept in text, kept
    assert failure["exit_code"] == 1
    assert failure["error_class"] == "disk-full"


@pytest.mark.parametrize("rule", RULES)
def test_each_redaction_rule_removes_its_planted_value(rule):
    from ops.acceptance.sanitize import Redactor

    assert [name for name, _ in Redactor().rules()] == list(RULES)
    output, planted = planted_output()
    assert planted[rule] not in json.dumps(fixture_redactor().tail(output, 40))
    # With only this rule disabled, its planted value comes back: the rule,
    # not a neighbour, is what removes it.
    assert planted[rule] in json.dumps(fixture_redactor({rule}).tail(output, 40))


@pytest.mark.parametrize(("code", "line", "expected"), [
    (124, "anything", "timeout"),
    (1, "could not download x (error sending request: connection reset by peer)", "network"),
    (1, "HTTP status client error (429 Too Many Requests)", "http"),
    (1, "the download does not match its pinned sha256", "integrity"),
    (101, "error[E0425]: cannot find value", "compile"),
    (5, "WARNING: Codex MCP registration was skipped because credential setup failed.", "exit-status"),
])
def test_failure_class_names_the_cause(code, line, expected):
    from ops.acceptance.sanitize import classify

    assert classify(code, [line]) == expected


def test_failed_step_receipt_keeps_a_sanitised_tail(tmp_path):
    (tmp_path / "ledger").mkdir()
    secret = hosted.REDACTOR.secret("c0ffee" * 11)
    row = hosted.Receipt("clean-install", "macos-arm64")
    output = f"installer: token {secret}\nCodex credential setup failed.\n".encode()
    with pytest.raises(hosted.Failed):
        row.observe("approved command", hosted.Result(5, output, 0.2), False, "Fixed assertion.")
    row.status, row.note = "fail", "Fixed assertion."
    row.write(tmp_path, None, True)
    receipt = json.loads((tmp_path / "clean-install.json").read_text(encoding="utf-8"))
    failure = receipt["steps"][0]["failure"]
    assert failure["exit_code"] == 5
    assert failure["last_lines"][-1] == "Codex credential setup failed."
    assert secret not in json.dumps(receipt)


def test_setup_step_failure_keeps_its_tail(monkeypatch):
    from ops.acceptance import hosted_source as source

    planted = "Dropped stream: error decoding response body"
    monkeypatch.setattr(source, "command", lambda *a, **k: hosted.Result(1, planted.encode(), 3.0))
    budget = source.Budget(60)
    budget.run(["daemon", "provision"], label="pseudolife-daemon provision models")
    error = budget.refuse("The source-built daemon could not provision its pinned models.")
    assert budget.steps[0]["failure"]["last_lines"] == [planted]
    assert error.failure["last_lines"] == [planted]


def test_setup_step_without_its_tool_is_recorded(monkeypatch):
    from ops.acceptance import hosted_source as source

    def missing(*_, **__):
        raise FileNotFoundError

    monkeypatch.setattr(source, "command", missing)
    budget = source.Budget(60)
    assert budget.run(["brew", "install"], label="brew install").code == 127
    assert budget.steps[0]["failure"]["error_class"] == "missing-tool"


@pytest.mark.skipif(os.name != "posix", reason="The stdio session uses POSIX process groups")
def test_shim_session_failure_carries_its_stderr(tmp_path):
    script = "import sys; sys.stderr.write('native shim refused: no daemon at fixture' + chr(10)); sys.exit(3)"
    with pytest.raises(hosted.Failed) as raised:
        with hosted.session(sys.executable, ["-c", script], dict(os.environ), tmp_path):
            pytest.fail("the shim exited before initialize")
    assert raised.value.failure["last_lines"] == ["native shim refused: no daemon at fixture"]
    assert raised.value.failure["exit_code"] == 3


# --- The summary counts cannot-run cells apart from passes and fails ---

def write_cells(folder, platform, cells):
    target = folder / f"acceptance-{platform}"
    target.mkdir()
    for scenario, (status, reason) in cells.items():
        payload = {"status": status, "daemon": hosted.DAEMONS[3], "daemon_engine": "rust", "assertion": reason}
        (target / f"{scenario}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_summary_counts_cannot_run_apart_and_reads_unrun_as_no_pass(tmp_path, capsys):
    reason = "Restore replaces only a Docker bank, and no published daemon image runs as one on this runner."
    cells = {scenario: ("pass", None) for scenario in matrix.SCENARIOS}
    cells["bank-recovery"] = ("not yet run", reason)
    cells["failure-and-retry"] = ("fail", "Fixture <installer>" + chr(10) + "raw | output")
    write_cells(tmp_path, "macos-arm64", cells)
    assert hosted.summary("macos-arm64", tmp_path) == "no pass"
    rendered = capsys.readouterr().out
    assert rendered.splitlines()[0] == ("**Verdict: no pass.** 3 pass, 1 fail, 1 cannot run and 0 missing, "
                                        "of 5 selected cells.")
    assert ("| bank-recovery | not selected | not selected | cannot run / source-built from the tag / rust |"
            in rendered.splitlines())
    assert f"- macOS Apple silicon, bank-recovery (cannot run): {reason}" in rendered.splitlines()
    assert "raw | output" not in rendered
    assert ("- macOS Apple silicon, failure-and-retry (fail): The receipt's reason was not a plain sentence."
            in rendered.splitlines())


def test_summary_passes_only_when_every_selected_cell_passed(tmp_path, capsys):
    write_cells(tmp_path, "macos-x64", {scenario: ("pass", None) for scenario in matrix.SCENARIOS})
    assert hosted.summary("macos-x64", tmp_path) == "pass"
    assert capsys.readouterr().out.startswith("**Verdict: pass.** 5 pass, 0 fail, 0 cannot run and 0 missing")
    assert hosted.summary("macos", tmp_path) == "no pass"
    assert "5 pass, 0 fail, 0 cannot run and 5 missing, of 10 selected cells." in capsys.readouterr().out


# --- Peer selection ---

def mac_fixture(tmp_path, monkeypatch, platform):
    from ops.acceptance import hosted_macos as mac

    output = tmp_path / "output"
    (output / "ledger").mkdir(parents=True)
    release = SimpleNamespace(commit="0" * 40, digest="1" * 64, archive=Path("release.tar.gz"),
                              checkout=lambda _: tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(
        sysname="Darwin", machine="x86_64" if platform == "macos-x64" else "arm64"))
    monkeypatch.setattr(mac, "published_release", lambda *args: release)
    monkeypatch.setattr(mac, "prepare_codex", lambda *args: tmp_path)
    monkeypatch.setattr(mac, "native_path", lambda *args: "")
    monkeypatch.setattr(mac, "free_space", lambda *args: None)
    monkeypatch.setattr(mac, "command", lambda *args, **kwargs: hosted.Result(1, b"", 0))
    return mac, output


@pytest.mark.skipif(os.name != "posix", reason="The Mac acceptance harness is POSIX-only")
@pytest.mark.parametrize("platform", ["macos-x64", "macos-arm64"])
def test_both_mac_columns_use_the_source_peer_and_record_it(tmp_path, monkeypatch, platform):
    mac, output = mac_fixture(tmp_path, monkeypatch, platform)
    used, observed = [], []

    def prepare(release, repo, scratch, budget, chosen):
        used.append(chosen)
        return SimpleNamespace(onnx_version="fixture")

    @contextmanager
    def peer(build, release, repo, path, scratch, budget):
        yield SimpleNamespace(kind=hosted.DAEMONS[3], engine="rust")

    monkeypatch.setattr(mac, "prepare", prepare)
    monkeypatch.setattr(mac, "source_peer", peer)
    monkeypatch.setattr(mac, "observe", lambda row, *args: observed.append((row.scenario, args[3].kind)))
    assert mac.run("v0.25.0", platform, output) == 0
    assert used == [platform]
    assert observed == [(scenario, hosted.DAEMONS[3]) for scenario in matrix.SCENARIOS]
    setup = json.loads((output / "peer-setup.json").read_bytes())
    assert setup["peer_note"] == hosted.PEER_NOTE
    assert setup["assertion"] == "The test peer became ready."


@pytest.mark.skipif(os.name != "posix", reason="The Mac acceptance harness is POSIX-only")
def test_mac_peer_setup_failure_keeps_offline_rows_and_its_reason(tmp_path, monkeypatch):
    mac, output = mac_fixture(tmp_path, monkeypatch, "macos-arm64")
    offline = []

    def prepare(*_):
        raise hosted.Unavailable("The source-built daemon could not provision its pinned models.",
                                 {"step": "provision", "exit_code": 1, "error_class": "network", "last_lines": []})

    monkeypatch.setattr(mac, "prepare", prepare)
    monkeypatch.setattr(mac, "OFFLINE", {name: (lambda row, *a: offline.append(row.scenario))
                                         for name in mac.OFFLINE})
    assert mac.run("v0.25.0", "macos-arm64", output) == 0
    assert offline == ["clean-install", "ordinary-native-use", "failure-and-retry"]
    upgrade = json.loads((output / "upgrade-and-rollback.json").read_bytes())
    assert upgrade["status"] == "not yet run"
    assert upgrade["assertion"] == "The source-built daemon could not provision its pinned models."


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_stock_clean_install_exit_is_reported_as_the_installer_exit(tmp_path):
    from ops.acceptance import hosted_linux as linux

    (tmp_path / "ledger").mkdir()
    by_name = {scenario: hosted.Receipt(scenario, "linux-arm64") for scenario in matrix.SCENARIOS}
    result = hosted.Result(5, b"WARNING: Codex MCP registration was skipped because credential setup failed.\n", 1)
    linux.probe_stock(None, {"daemon_healthy": True, "running_containers": []}, result, tmp_path, None,
                      tmp_path, by_name, tmp_path)
    clean = by_name["clean-install"]
    assert (clean.status, clean.note) == ("fail", "The stock installer exited non-zero.")
    assert clean.fixture_notes == [hosted.STOCK_CODEX_NOTE]
    assert "#1897" in hosted.STOCK_CODEX_NOTE and "#1897" in hosted.CODEX_HOME_NOTE
    assert clean.steps[0]["failure"]["last_lines"] == [
        "WARNING: Codex MCP registration was skipped because credential setup failed."]
    assert all(by_name[name].note == "The scenario was not selected." for name in linux.CLIENT_ROWS)


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_source_recovery_backs_up_and_names_the_restore_limit(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    @contextmanager
    def session(*_):
        yield SimpleNamespace(tool=lambda *a, **k: {})

    calls = []

    def backup(argv, **options):
        calls.append((argv, options))
        out = Path(argv[argv.index("--out") + 1])
        (out / "pseudolife_lite_memory-fixture.sql.gz").touch()
        (out / "pseudolife_lite_state-fixture.tar.gz").touch()
        return hosted.Result(0, b"", 0)

    monkeypatch.setattr(linux, "session", session)
    monkeypatch.setattr(linux, "command", backup)
    peer = SimpleNamespace(owner_executable="client", owner_env={"PATH": "/usr/bin"}, checkout=tmp_path,
                           tools="/opt/pg/bin", database_url="postgresql://fixture", data_dir=tmp_path / "state")
    row = hosted.Receipt("bank-recovery", "macos-arm64")
    with pytest.raises(hosted.Unavailable, match="Restore replaces only a Docker bank"):
        linux.source_recovery(row, None, tmp_path, None, peer, tmp_path)
    argv, options = calls[0]
    assert argv[1:3] == ["backup", "--data-dir"]
    assert options["env"]["PATH"].split(os.pathsep)[0] == "/opt/pg/bin"
    assert row.commands == [{"command": matrix.commands(row.scenario, row.platform)[0], "exit_code": 0}]


def test_failure_keeps_cause_lines_from_before_the_tail():
    output = "\n".join(["Requested client setup incomplete (Codex). Codex exited before setup completed."]
                       + [f"summary line {index}" for index in range(30)]).encode()
    failure = fixture_redactor().failure("installer", 5, output)
    assert failure["cause_lines"] == ["Requested client setup incomplete (Codex). Codex exited before setup completed."]
    assert failure["last_lines"][-1] == "summary line 29"
    assert len(failure["last_lines"]) == 12


@pytest.mark.parametrize(("platform", "source"), [("macos-x64", "source"), ("macos-arm64", "brew")])
def test_intel_peer_builds_postgres_from_source(tmp_path, monkeypatch, platform, source):
    from ops.acceptance import hosted_source

    used = []
    monkeypatch.setattr(hosted_source, "source_postgres", lambda *a: used.append("source") or tmp_path)

    def run(argv, **_):
        used.append(argv[0])
        return hosted.Result(0, str(tmp_path).encode(), 0)

    hosted_source.postgres_tools(platform, {}, tmp_path, SimpleNamespace(run=run))
    assert used[0] == source


@pytest.mark.skipif(os.name != "posix", reason="The peer harness is POSIX-only")
def test_peer_postgres_socket_stays_in_the_scratch_folder(tmp_path, monkeypatch):
    from ops.acceptance import hosted_source as source

    calls = []

    def run(argv, **options):
        calls.append(argv)
        return hosted.Result(0 if len(calls) == 1 else 1, b"", 0)

    build = source.Build("linux-arm64", tmp_path / "daemon", tmp_path, {}, "1.27.0")
    budget = SimpleNamespace(run=run, refuse=hosted.Unavailable, steps=[{}])
    with pytest.raises(hosted.Unavailable, match="did not start"):
        with source.source_peer(build, None, None, None, tmp_path, budget):
            pytest.fail("pg_ctl's failure should prevent the peer from starting")
    options = calls[1][calls[1].index("-o") + 1]
    assert f"-k {tmp_path}" in options


@pytest.mark.skipif(os.name != "posix", reason="The Codex probe runs a POSIX shell")
def test_codex_probe_reports_whether_codex_answered(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    fake = tools / "codex"
    fake.write_text("#!/bin/sh\nread first\nread second\nread third\n"
                    "echo '{\"id\":1,\"result\":{}}'\necho 'codex: fixture failure' >&2\nexit 7\n")
    fake.chmod(0o755)
    env = {"PATH": f"{tools}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    probe = hosted.codex_probe(env, tmp_path, hold=0)
    assert (probe["exit_code"], probe["config_read_answered"]) == (7, False)
    assert "codex: fixture failure" in probe["last_lines"]


def test_test_peer_profiles_have_a_started_codex_home_and_stock_ones_do_not(tmp_path):
    started = hosted.Profile(tmp_path / "peer", "")
    stock = hosted.Profile(tmp_path / "stock", "", codex_started=False)
    assert started.codex.is_dir()
    assert not stock.codex.exists()
    (tmp_path / "ledger").mkdir()
    row = hosted.Receipt("clean-install", "macos-arm64", fixture_notes=[hosted.CODEX_HOME_NOTE])
    row.write(tmp_path, None, True)
    receipt = json.loads((tmp_path / "clean-install.json").read_bytes())
    assert receipt["fixture_notes"] == [hosted.CODEX_HOME_NOTE]


def test_release_api_refusal_falls_back_to_the_checksum_manifest(tmp_path, monkeypatch):
    import hashlib

    archive = "pseudolife-mcp-0.25.0-aarch64-unknown-linux-gnu.tar.gz"
    bundle = "pseudolife-compose-0.25.0.tar.gz"
    bodies = {archive: b"fixture archive", bundle: b"fixture bundle"}
    manifest = "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in bodies.items())
    bodies["SHA256SUMS"] = manifest.encode()
    commit = "a" * 40

    def command(argv, **_):
        if argv[0] == "git":
            return hosted.Result(0, f"{commit}\trefs/tags/v0.25.0\n".encode(), 0)
        url, path = argv[-1], Path(argv[argv.index("--output") + 1])
        if url.startswith(hosted.API):
            return hosted.Result(22, b"403", 0)
        path.write_bytes(bodies[url.rsplit("/", 1)[1]])
        return hosted.Result(0, b"200", 0)

    monkeypatch.setattr(hosted, "command", command)
    release = hosted.published_release("v0.25.0", "linux-arm64", tmp_path / "release")
    assert (release.commit, release.archive.name) == (commit, archive)
    assert release.digest == hashlib.sha256(bodies[archive]).hexdigest()
    assert release.verified_by == "SHA256SUMS only (release API refused)"
    bodies[archive] = b"tampered archive"
    with pytest.raises(hosted.Failed, match="disagreed with its published SHA-256"):
        hosted.published_release("v0.25.0", "linux-arm64", tmp_path / "again")


def test_a_passing_step_keeps_its_warning_lines(tmp_path):
    row = hosted.Receipt("upgrade-and-rollback", "macos-x64")
    output = b"installing\nWARNING: Codex was not registered: fixture reason.\ndone\n"
    row.observe("Published legacy installer <client-only>", hosted.Result(0, output, 1), True, "Fixed.")
    assert row.steps[0]["warning_lines"] == ["WARNING: Codex was not registered: fixture reason."]
    assert "failure" not in row.steps[0]


def test_mcp_error_reaches_the_row(monkeypatch):
    class Process:
        stdin = SimpleNamespace(write=lambda _: None, flush=lambda: None)
        stdout = iter([json.dumps({"jsonrpc": "2.0", "id": 1,
                                   "error": {"code": -32602, "message": "unknown tool: fixture"}}).encode()])

    mcp = hosted.Mcp(Process())
    with pytest.raises(hosted.Failed, match="returned a result") as raised:
        mcp.send("tools/call", {"name": "memory_agents", "arguments": {}})
    assert raised.value.failure["error_class"] == "mcp-error"
    assert any("unknown tool: fixture" in line for line in raised.value.failure["last_lines"])


def test_tool_calls_name_a_codex_thread_as_codex_does():
    sent = []

    class Process:
        stdin = SimpleNamespace(write=sent.append, flush=lambda: None)
        stdout = iter([json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"ok": True}}}).encode()])

    mcp = hosted.Mcp(Process())
    assert mcp.tool("memory_agents", action="update") == {"ok": True}
    params = json.loads(sent[0])["params"]
    assert params["_meta"] == {"threadId": mcp.thread}
    assert str(__import__("uuid").UUID(mcp.thread)) == mcp.thread


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_legacy_clients_install_with_the_job_python(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    envs = []
    monkeypatch.setattr(linux, "command", lambda argv, **options: envs.append(options["env"]) or
                        hosted.Result(0, b"", 0))
    profile = hosted.Profile(tmp_path / "home", "/usr/bin")
    linux.live_install(None, tmp_path, profile, [], engine="python", overrides=linux.LEGACY_PYTHON)
    assert envs[0]["PIPX_DEFAULT_PYTHON"] == sys.executable


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_rerun_names_the_files_it_changed():
    from ops.acceptance import hosted_linux as linux

    before = {Path(".codex/config.toml"): "a", Path("runtime.json"): "b"}
    after = {Path(".codex/config.toml"): "c", Path("runtime.json"): "b", Path("new.json"): "d"}
    assert linux.changed_files(before, after) == [".codex/config.toml", "new.json"]


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_a_legacy_client_that_did_not_install_leaves_the_upgrade_unrun(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    output = b"error: metadata-generation-failed\nError installing pseudolife-mcp from spec\n"
    monkeypatch.setattr(linux, "command", lambda *a, **k: hosted.Result(0, b"", 0))
    monkeypatch.setattr(linux, "live_install", lambda *a, **k: hosted.Result(0, output, 1))
    row = hosted.Receipt("upgrade-and-rollback", "macos-x64")
    peer = SimpleNamespace(url="http://127.0.0.1:1", token_file=tmp_path / "token")
    with pytest.raises(hosted.Unavailable, match="v0.23.1 Python client could not be installed") as raised:
        linux.upgrade(row, SimpleNamespace(tag="v0.25.1"), tmp_path, "", peer, tmp_path)
    assert "Error installing pseudolife-mcp from spec" in raised.value.failure["last_lines"]


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_rerun_names_changed_codex_settings_without_their_values():
    from ops.acceptance import hosted_linux as linux

    before = '[mcp_servers.pseudolife-memory]\ncommand = "old"\n[mcp_servers.pseudolife-memory.env]\nA = "1"\n'
    after = ('[mcp_servers.pseudolife-memory]\ncommand = "fixture-secret-path"\n'
             '[mcp_servers.pseudolife-memory.env]\nA = "1"\nB = "2"\n')
    changed = linux.changed_settings(before, after)
    assert changed == ["mcp_servers.pseudolife-memory.command", "mcp_servers.pseudolife-memory.env.B"]
    assert "fixture-secret-path" not in json.dumps(changed)
    kinds = linux.setting_changes(after, before.replace('A = "1"', ''))
    assert kinds == {"mcp_servers.pseudolife-memory.command": "changed",
                     "mcp_servers.pseudolife-memory.env.A": "removed",
                     "mcp_servers.pseudolife-memory.env.B": "removed"}
    assert linux.setting_changes(before, after)["mcp_servers.pseudolife-memory.env.B"] == "added"
    assert "fixture-secret-path" not in json.dumps(kinds)


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_the_first_rerun_after_the_handoff_may_change_only_the_expected_settings(monkeypatch):
    # From v0.23.1 and v0.24.0: the lost memory_message approval added as
    # "approve" (#1903) and the token file re-pointed (#1908, not fixed yet);
    # anything else, another value, another kind or nothing fails the row.
    from ops.acceptance import hosted_linux as linux

    config, runtime = Path(".codex/config.toml"), Path("data/runtime.json")
    before = {config: "a", runtime: "r"}
    changed = {config: "b", runtime: "r"}

    def kept(tag, after_text, prints=changed, before_text=LEGACY_CONFIG, start=before):
        changes = linux.setting_changes(before_text, after_text)
        return linux.first_rerun_kept(tag, start, prints, changes, after_text, config)

    for tag in ("v0.23.1", "v0.24.0"):
        assert kept(tag, RERUN_CONFIG)
        assert not kept(tag, APPROVED_CONFIG), "the #1908 entry must go when its fix ships"
        assert not kept(tag, RERUN_CONFIG.replace('"approve"', '"prompt"'))
        assert not kept(tag, RERUN_CONFIG + '[mcp_servers.other]\ncommand = "x"\n')
        assert not kept(tag, LEGACY_CONFIG, prints=before)
        assert not kept(tag, RERUN_CONFIG, prints={config: "b", runtime: "s"})
        # An approval the start already had, removed, is not the lost one added.
        assert not kept(tag, LEGACY_CONFIG.replace('"copy"', '"operator"'), before_text=APPROVED_CONFIG)
    # A start whose installer carries both fixes expects no change at all.
    monkeypatch.setitem(linux.FIRST_RERUN_SETTINGS, "v9.9.9", {})
    assert linux.first_rerun_kept("v9.9.9", before, dict(before), {}, LEGACY_CONFIG, config)
    assert not linux.first_rerun_kept("v9.9.9", before, changed, {}, LEGACY_CONFIG, config)
    assert not kept("v9.9.9", RERUN_CONFIG)


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_the_second_rerun_needs_the_installers_own_nothing_changed_line():
    from ops.acceptance import hosted_linux as linux

    installer = (Path(__file__).resolve().parents[2] / "ops" / "install-native.sh").read_text(encoding="utf-8")
    assert 'step "Native client $NATIVE_VERSION already installed at $SHIM_PATH; nothing changed"' in installer
    line = b"==> Native client 0.25.1 already installed at /tmp/home/data/pseudolife-mcp/bin/pseudolife-mcp; nothing changed\n"
    assert linux.UNCHANGED_LINE.search(b"Installing\n" + line)
    assert not linux.UNCHANGED_LINE.search(b"==> Native client 0.25.1 installed at /tmp/x (running sessions keep it)\n")


LEGACY_CONFIG = ('[mcp_servers.pseudolife-memory]\ncommand = "launcher"\nargs = []\n'
                 '[mcp_servers.pseudolife-memory.env]\nPSEUDOLIFE_MCP_TOKEN_FILE = "copy"\n')
APPROVED_CONFIG = LEGACY_CONFIG + '[mcp_servers.pseudolife-memory.tools.memory_message]\napproval_mode = "approve"\n'
# What run 1 from a legacy start does on a release without #1908's fix.
RERUN_CONFIG = APPROVED_CONFIG.replace('"copy"', '"operator"')
UNCHANGED = b"==> Native client 0.25.1 already installed at /fixture/bin/pseudolife-mcp; nothing changed\n"


def _upgrade_with(tmp_path, monkeypatch, first, second):
    """Run hosted_linux.upgrade from one legacy start, with every external step
    faked: the legacy install writes LEGACY_CONFIG, the first native rerun
    `first` and the second `second` (each a config text and its output)."""
    from ops.acceptance import hosted_linux as linux

    calls = []

    def live_install(peer, repo, profile, flags, *, overrides=None, engine="native"):
        calls.append(engine)
        step = len(calls) % 4  # per starting tag: legacy, run 1, run 2, rollback
        config, launcher = profile.codex / "config.toml", profile.installed_launcher
        if step == 1:
            config.write_text(LEGACY_CONFIG, encoding="utf-8")
            return hosted.Result(0, b"legacy installed\n", 1)
        if step == 0:  # the rollback
            launcher.unlink()
            return hosted.Result(0, b"rolled back\n", 1)
        text, output = first if step == 2 else second
        config.write_text(text, encoding="utf-8")
        return hosted.Result(0, output, 1)

    def update(argv, **options):
        if "update" in argv:
            launcher = Path(options["env"]["HOME"]) / "data" / "pseudolife-mcp" / "bin" / "pseudolife-mcp"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_bytes(b"native")
        return hosted.Result(0, b"", 1)

    monkeypatch.setattr(linux, "live_install", live_install)
    monkeypatch.setattr(linux, "command", update)
    monkeypatch.setattr(linux, "first_session", lambda *a, **k: None)
    monkeypatch.setattr(hosted.Profile, "native_marker", lambda self, release: self.installed_launcher.is_file())
    monkeypatch.setattr(hosted.Profile, "launcher", lambda self: "launcher")
    monkeypatch.setattr(hosted.Profile, "registration", lambda self: {"env": {}})
    row = hosted.Receipt("upgrade-and-rollback", "macos-arm64")
    peer = SimpleNamespace(url="http://127.0.0.1:1", token_file=tmp_path / "token", during=lambda r, fn: fn())
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    try:
        linux.upgrade(row, SimpleNamespace(tag="v0.25.1", version="0.25.1"), tmp_path, "", peer, scratch)
    except hosted.Observation as error:
        return row, error, calls
    return row, None, calls


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_the_upgrade_row_completes_the_lost_approval_once_then_holds_run_two_to_run_one(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    row, error, calls = _upgrade_with(tmp_path, monkeypatch, (RERUN_CONFIG, UNCHANGED),
                                      (RERUN_CONFIG, UNCHANGED))
    assert error is None, error
    # Both starts: legacy install, two native reruns, rollback.
    assert calls == ["python", "native", "native", "python"] * 2
    reruns = [step for step in row.steps if step["command"].endswith(("<rerun>", "<second rerun>"))]
    assert [step["observed"] for step in reruns] == [True] * 4, reruns
    assert reruns[0]["changed_codex_settings"] == sorted([linux.LOST_APPROVAL, linux.REPOINTED_TOKEN_FILE])
    assert reruns[0]["codex_setting_changes"] == {linux.LOST_APPROVAL: "added",
                                                  linux.REPOINTED_TOKEN_FILE: "changed"}
    assert reruns[0]["expected_codex_settings"] == {linux.LOST_APPROVAL: "added",
                                                   linux.REPOINTED_TOKEN_FILE: "changed"}
    assert "approve" not in json.dumps(reruns[0]) and "operator" not in json.dumps(reruns[0])
    assert reruns[1]["changed_codex_settings"] == []
    # The ledger's rerun step records run 1, as before the second run existed.
    assert [entry["command"] for entry in row.commands].count("<native-installer> <rerun-from-0.23.x>") == 1


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
@pytest.mark.parametrize(("first", "second", "failed"), [
    # Run 1 changing more than expected.
    ((RERUN_CONFIG + "# extra\n[mcp_servers.other]\ncommand = \"x\"\n", UNCHANGED), (RERUN_CONFIG, UNCHANGED),
     "<rerun>"),
    # Run 1 approving with another value.
    ((RERUN_CONFIG.replace('"approve"', '"prompt"'), UNCHANGED), (RERUN_CONFIG, UNCHANGED), "<rerun>"),
    # Run 1 changing nothing where the approval was lost.
    ((LEGACY_CONFIG, UNCHANGED), (LEGACY_CONFIG, UNCHANGED), "<rerun>"),
    # Run 2 changing the registration again.
    ((RERUN_CONFIG, UNCHANGED), (RERUN_CONFIG + "# moved\n", UNCHANGED), "<second rerun>"),
    # Run 2 without the installer's "nothing changed" line.
    ((RERUN_CONFIG, UNCHANGED), (RERUN_CONFIG, b"==> Native client 0.25.1 installed at /x\n"),
     "<second rerun>"),
])
def test_the_upgrade_row_fails_any_other_rerun_change(tmp_path, monkeypatch, first, second, failed):
    row, error, _ = _upgrade_with(tmp_path, monkeypatch, first, second)
    assert isinstance(error, hosted.Failed), error
    reruns = [step for step in row.steps if step["command"].endswith(("<rerun>", "<second rerun>"))]
    assert reruns[-1]["command"].endswith(failed), reruns
    assert reruns[-1]["observed"] is False
    assert reruns[-1]["assertion"] in str(error)


# --- Review fixes ---

@pytest.mark.parametrize(("line", "secret", "kept"), [
    ('{"url": "http://127.0.0.1:8765/#/signin?code=K7MQ2X9ZRT", "code": "K7MQ2X9ZRT"}', "K7MQ2X9ZRT",
     '"code": "<redacted>"'),
    ('{"token": "fixture-token-value", "PSEUDOLIFE_MCP_TOKEN": "other-fixture-value"}', "fixture-token-value",
     '"token": <redacted>'),
    ('{"password": "fixture-password-value"}', "fixture-password-value", '"password": <redacted>'),
    ('{"code": -32603, "message": "Coordination identity is unavailable"}', None, '"code": -32603'),
    ("authorization failed for the fixture", None, "authorization failed"),
    ("listening on 2001:db8::7 and ::1", "2001:db8::7", "::1"),
])
def test_json_keys_links_and_addresses_are_redacted_without_eating_prose(line, secret, kept):
    from ops.acceptance.sanitize import Redactor

    cleaned = Redactor().text(line)
    if secret is not None:
        assert secret not in cleaned
    assert kept in cleaned


def test_a_registered_secret_is_removed_even_when_no_pattern_would(tmp_path):
    (tmp_path / "ledger").mkdir()
    secret = hosted.REDACTOR.secret("plain-words-registered-value")
    row = hosted.Receipt("clean-install", "macos-arm64")
    output = "installer printed plain-words-registered-value\nWARNING: Codex setup failed.\n".encode()
    with pytest.raises(hosted.Failed):
        row.observe("approved command", hosted.Result(5, output, 0.2), False, "Fixed assertion.")
    assert secret not in json.dumps(row.steps)


def test_a_peer_asset_digest_mismatch_leaves_rows_unrun(tmp_path, monkeypatch):
    def command(argv, **_):
        Path(argv[argv.index("--output") + 1]).write_bytes(b"re-packed archive")
        return hosted.Result(0, b"200", 0)

    monkeypatch.setattr(hosted, "command", command)
    with pytest.raises(hosted.Unavailable, match="disagreed"):
        hosted.download("https://example.com/x.tar.gz", tmp_path / "x.tar.gz", "0" * 64, peer_asset=True)
    with pytest.raises(hosted.Failed, match="disagreed"):
        hosted.download("https://example.com/y.tar.gz", tmp_path / "y.tar.gz", "0" * 64)


@pytest.mark.skipif(os.name != "posix", reason="The Mac acceptance harness is POSIX-only")
def test_a_fixture_left_running_fails_cleanup_for_every_mac_row(tmp_path, monkeypatch):
    mac, output = mac_fixture(tmp_path, monkeypatch, "macos-arm64")

    @contextmanager
    def peer(*_):
        try:
            yield SimpleNamespace(kind=hosted.DAEMONS[3], engine="rust")
        finally:
            raise hosted.CleanupFailed("The owned PostgreSQL fixture did not stop.",
                                       {"step": "pg_ctl stop", "exit_code": 1, "error_class": "exit-status",
                                        "cause_lines": [], "last_lines": []})

    def passed(row, *_):
        row.status, row.note = "pass", matrix.SCENARIOS[row.scenario].outcome

    monkeypatch.setattr(mac, "prepare", lambda *a: SimpleNamespace(onnx_version="fixture"))
    monkeypatch.setattr(mac, "source_peer", peer)
    monkeypatch.setattr(mac, "observe", passed)
    assert mac.run("v0.25.0", "macos-arm64", output) == 1
    receipt = json.loads((output / "clean-install.json").read_bytes())
    assert (receipt["status"], receipt["cleanup_verified"]) == ("fail", False)


@pytest.mark.skipif(os.name != "posix", reason="The Docker acceptance harness is POSIX-only")
def test_an_early_arm64_failure_keeps_the_client_rows_not_run(tmp_path, monkeypatch):
    from ops.acceptance import hosted_linux as linux

    def refused(*_):
        raise hosted.Failed("Published tag checkout succeeded.")

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(machine="aarch64"))
    monkeypatch.setattr(linux, "free_space", lambda *_: None)
    monkeypatch.setattr(linux, "published_release", refused)
    (tmp_path / "ledger").mkdir()
    assert linux.run("v0.25.0", "linux-arm64", tmp_path) == 1
    for name in linux.CLIENT_ROWS:
        row = json.loads((tmp_path / f"{name}.json").read_bytes())
        assert (row["status"], row["assertion"]) == ("not yet run", linux.NOT_SUPPORTED)
    assert json.loads((tmp_path / "clean-install.json").read_bytes())["status"] == "fail"


def test_disk_floor_is_checked_again_before_each_heavy_writer(tmp_path, monkeypatch):
    from ops.acceptance import hosted_source as source

    budget = source.Budget(60)
    monkeypatch.setattr(source, "free_bytes", lambda _: source.FLOOR)
    source.enough_disk(tmp_path, budget, "the daemon build")
    monkeypatch.setattr(source, "free_bytes", lambda _: source.FLOOR - 1)
    with pytest.raises(hosted.Unavailable, match="less than 8 GiB free before model provisioning"):
        source.enough_disk(tmp_path, budget, "model provisioning")
    assert [step["command"] for step in budget.steps] == ["free disk before the daemon build",
                                                          "free disk before model provisioning"]


# --- Conductor fix round ---

def test_a_failed_expectation_reads_as_a_failure_in_the_summary(tmp_path, capsys):
    with pytest.raises(hosted.Failed) as raised:
        hosted.require(False, "The MCP tool completed successfully.")
    assert str(raised.value) == "Not met: The MCP tool completed successfully."
    write_cells(tmp_path, "macos-arm64", {"clean-install": ("fail", str(raised.value))})
    hosted.summary("macos-arm64", tmp_path)
    assert ("- macOS Apple silicon, clean-install (fail): Not met: The MCP tool completed successfully."
            in capsys.readouterr().out.splitlines())


def test_the_workflow_holds_no_secret_environment_or_identity_token():
    root = Path(__file__).resolve().parents[2]
    text = (root / ".github/workflows/hosted-acceptance.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert "secrets." not in text
    assert "id-token" not in text

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for value in node:
                yield from keys(value)

    assert "environment" not in set(keys(workflow))


@pytest.mark.skipif(os.name != "posix", reason="The peer harness is POSIX-only")
def test_the_test_peer_and_its_postgres_listen_on_loopback_only(tmp_path, monkeypatch):
    from ops.acceptance import hosted_source as source

    calls, envs = [], []

    def run(argv, **options):
        calls.append(argv)
        return hosted.Result(0, b"", 0)

    @contextmanager
    def daemon(build, repo, scratch, env):
        envs.append(env)
        raise hosted.Unavailable("Fixture daemon stopped here.")
        yield

    monkeypatch.setattr(source, "daemon_process", daemon)
    build = source.Build("macos-arm64", tmp_path / "daemon", tmp_path, {}, "1.27.0")
    budget = SimpleNamespace(run=run, refuse=hosted.Unavailable, steps=[{}])
    with pytest.raises(hosted.Unavailable, match="Fixture daemon"):
        with source.source_peer(build, None, None, None, tmp_path, budget):
            pytest.fail("the fixture daemon stops before the peer is ready")
    options = calls[1][calls[1].index("-o") + 1]
    assert options.split()[:2] == ["-h", "127.0.0.1"]
    assert envs[0]["PSEUDOLIFE_MCP_HOST"] == "127.0.0.1"
    assert envs[0]["PSEUDOLIFE_MCP_DATABASE_URL"].split("@", 1)[1].startswith("127.0.0.1:")


def test_the_checkout_must_be_the_tag_commit(tmp_path, monkeypatch):
    def command(argv, **_):
        if argv[:2] == ["git", "clone"]:
            return hosted.Result(0, b"", 0)
        return hosted.Result(0, b"b" * 40 + b"\n", 0)

    monkeypatch.setattr(hosted, "command", command)
    release = hosted.Release("v0.25.1", "macos-arm64", tmp_path, "a" * 40, tmp_path / "x.tar.gz", "0" * 64)
    with pytest.raises(hosted.Failed, match="Checkout matched the release commit"):
        release.checkout(tmp_path / "checkout")


def test_every_peer_receipt_says_what_it_is_and_how_the_release_was_checked(tmp_path):
    (tmp_path / "ledger").mkdir()
    release = hosted.Release("v0.25.1", "macos-arm64", tmp_path, "a" * 40,
                             Path("pseudolife-mcp-0.25.1-aarch64-apple-darwin.tar.gz"), "0" * 64,
                             verified_by="SHA256SUMS only (release API refused)")
    row = hosted.Receipt("ordinary-native-use", "macos-arm64", daemon=hosted.DAEMONS[3], daemon_engine="rust")
    row.write(tmp_path, release, True)
    receipt = json.loads((tmp_path / "ordinary-native-use.json").read_bytes())
    assert receipt["peer_note"] == hosted.PEER_NOTE
    assert receipt["release_verified_by"] == "SHA256SUMS only (release API refused)"


def test_a_tail_cut_inside_a_secret_leaves_no_fragment(tmp_path):
    secret = hosted.REDACTOR.secret("Zq" + "w9x" * 30)
    with (tmp_path / "out").open("w+b") as stream:
        stream.write(b"line before\n" + secret.encode() + b"\nWARNING: the cause\n")
        stream.flush()
        tail = hosted.read_tail(stream, limit=len(secret) // 2 + len(b"\nWARNING: the cause\n"))
    assert tail == b"WARNING: the cause\n"
    assert not any(secret[index:index + 12] in tail.decode() for index in range(len(secret) - 12))


@pytest.mark.skipif(os.name != "posix", reason="The Mac acceptance harness is POSIX-only")
def test_an_unexpected_mac_error_still_writes_redacted_receipts(tmp_path, monkeypatch, capsys):
    mac, output = mac_fixture(tmp_path, monkeypatch, "macos-arm64")

    def prepare(*_):
        raise RuntimeError("raw output that must not reach the log")

    monkeypatch.setattr(mac, "prepare", prepare)
    assert mac.run("v0.25.0", "macos-arm64", output) == 1
    assert "raw output" not in capsys.readouterr().out
    assert (output / "peer-setup.json").is_file()
    for scenario in matrix.SCENARIOS:
        receipt = json.loads((output / f"{scenario}.json").read_bytes())
        assert receipt["status"] == "fail"
        assert "RuntimeError" in receipt["assertion"]


# Failure reasons the harness raises as written, each already worded as a failure.
FAILURE_WORDED = {
    "The update changed the daemon without a structured rollback record.",
    "The updater did not identify a usable rollback tag.",
    "The recorded rollback did not match a documented Compose route.",
    "One or more installed ordinary-use commands failed.",
    "The published client-only installer did not register Codex against the healthy test peer.",
    "The owned PostgreSQL fixture did not stop.",
}
# The one call whose reason is computed: `download` builds "Public download
# failed for ..." (`require` and the direct raises add the NOT_MET prefix).
COMPUTED_REASONS = {("hosted.py", "download")}


def test_every_failure_reason_reads_as_a_failure():
    import ast

    root = Path(__file__).resolve().parents[2] / "ops" / "acceptance"
    computed = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]:
            for node in ast.walk(function):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in ("Failed", "CleanupFailed") and node.args):
                    continue
                reason = node.args[0]
                if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
                    assert reason.value in FAILURE_WORDED, (path.name, reason.value)
                elif (isinstance(reason, ast.BinOp) and isinstance(reason.left, ast.Name)
                      and reason.left.id == "NOT_MET"):
                    continue
                else:
                    computed.add((path.name, function.name))
    assert computed == COMPUTED_REASONS


def test_a_digest_line_keeps_the_plain_marker_and_is_not_called_key_material():
    from ops.acceptance.sanitize import Redactor

    digest = "441dfae4280cae3e0425e5f876f80559fb0731c140f1aae22b3e5a54d0411bfd"
    cleaned = Redactor().text(f"expected\n{digest}\n")
    assert digest not in cleaned
    assert "<redacted>" in cleaned and "key material" not in cleaned
