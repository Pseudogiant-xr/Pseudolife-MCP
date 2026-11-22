"""Checks for the hosted acceptance evidence and workflow boundary."""

from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import yaml

from ops.acceptance import hosted, matrix


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
    assert len(rendered.splitlines()) == 2 + len(matrix.SCENARIOS)


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


def test_guest_arguments_have_no_patch_markers():
    root = Path(__file__).resolve().parents[2]
    shell = (root / "ops/acceptance/hosted_lima_guest.sh").read_text(encoding="utf-8")
    for line in shell.splitlines():
        if "apt-get -qq install" in line or "iptables" in line:
            assert "+" not in line.split()


@pytest.mark.skipif(os.name != "posix", reason="The guest fixture requires Bash")
@pytest.mark.parametrize("matches", [True, False])
def test_guest_checks_the_running_image_repository_digest(tmp_path, matches):
    root = Path(__file__).resolve().parents[2]
    shell = (root / "ops/acceptance/hosted_lima_guest.sh").read_text(encoding="utf-8")
    start = shell.index("verify_published_daemon() {")
    verify = shell[start:shell.index("\n}", start) + 2]
    readiness = shell.split('chmod 700 "$root/owner"\n', 1)[1]
    repository = "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust"
    expected = f"{repository}@sha256:{'a' * 64}"
    observed = expected if matches else f"{repository}@sha256:{'b' * 64}"
    script = tmp_path / "verify.sh"
    script.write_text(
        "set -euo pipefail\n"
        "sudo() {\n"
        "  case \"$3\" in\n"
        "    inspect) test \"$6\" = pseudolife-mcp-daemon || return 2; printf 'running-image\\n';;\n"
        f"    image) test \"$7\" = running-image || return 2; printf '%s\\n' '{observed}';;\n"
        "    *) return 2;;\n"
        "  esac\n"
        "}\n"
        f"published_image='{expected}'\n{verify}\n"
        "guest_address=127.0.0.1\ntoken=fixture\n"
        f"{readiness}", encoding="utf-8",
    )
    result = subprocess.run(["bash", script], timeout=5, check=False, capture_output=True)
    if matches:
        assert (result.returncode, result.stdout.splitlines()[0]) == (0, b"peer-ready")
    else:
        assert (result.returncode, result.stdout, result.stderr.strip()) == (1, b"", b"peer-image-unverified")


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


@pytest.mark.skipif(os.name != "posix", reason="The Mac acceptance harness is POSIX-only")
def test_mac_attempts_share_time_and_checkpoint_rows(tmp_path, monkeypatch):
    from ops.acceptance import hosted_macos as mac

    output = tmp_path / "output"
    (output / "ledger").mkdir(parents=True)
    release = SimpleNamespace(commit="0" * 40, digest="1" * 64, archive=Path("release.tar.gz"),
                              checkout=lambda _: tmp_path)
    clock, starts = [0], []

    @contextmanager
    def guest(release, repo, path, scratch, budget):
        starts.append(budget.remaining(720))
        clock[0] += 10
        if len(starts) == 1:
            raise hosted.Unavailable("Fixture guest failed to start.")
        yield SimpleNamespace(kind=hosted.DAEMONS[2], engine="rust")
        assert all((output / f"{scenario}.json").is_file() for scenario in matrix.SCENARIOS)

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(sysname="Darwin", machine="x86_64"))
    monkeypatch.setattr(mac, "published_release", lambda *args: release)
    monkeypatch.setattr(mac, "prepare_codex", lambda *args: tmp_path)
    monkeypatch.setattr(mac, "native_path", lambda *args: "")
    monkeypatch.setattr(mac, "free_space", lambda *args: None)
    monkeypatch.setattr(mac, "command", lambda *args, **kwargs: hosted.Result(1, b"", 0))
    monkeypatch.setattr(mac, "lima_peer", guest)
    monkeypatch.setattr(mac, "observe", lambda *args: None)
    monkeypatch.setattr(mac, "monotonic", lambda: clock[0])
    assert mac.run("v0.25.0", "macos-x64", output) == 0
    assert starts == [720, 710]


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


@pytest.mark.skipif(os.name != "posix", reason="The Mac acceptance harness is POSIX-only")
def test_apple_postgres_password_is_piped_to_initdb(tmp_path, monkeypatch):
    from ops.acceptance import hosted_macos as mac

    monkeypatch.setattr(mac, "build_source_peer", lambda *args: (tmp_path, None, {}))
    monkeypatch.setattr(mac.secrets, "token_hex", lambda _: "fixture")
    calls = []

    def initdb(argv, **options):
        calls.append((argv, options))
        assert list(tmp_path.iterdir()) == []
        return hosted.Result(1, b"", 0)

    with pytest.raises(hosted.Unavailable, match="fixture could not initialize"):
        with mac.apple_peer(None, None, None, tmp_path, SimpleNamespace(run=initdb)):
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
    clean = json.loads((tmp_path / "clean-install.json").read_bytes())
    assert clean["status"] == "fail"
    upgrade = json.loads((tmp_path / "upgrade-and-rollback.json").read_bytes())
    assert upgrade["status"] == "not yet run"
