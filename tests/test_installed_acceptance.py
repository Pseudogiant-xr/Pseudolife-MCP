import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile

import pytest

from ops.acceptance import matrix, runner


CELL = "clean-install", "linux-x64"
ARCHIVE = "pseudolife-mcp-0.24.0-x86_64-unknown-linux-gnu.tar.gz"
OTHER_ARCHIVE = "pseudolife-mcp-0.24.0-aarch64-unknown-linux-gnu.tar.gz"
REDIRECTED = (
    "HOME", "USERPROFILE", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
)
CLEARED = (
    "PSEUDOLIFE_SHIM_RUNTIMES", "PSEUDOLIFE_SHIM_LAUNCHER", "PSEUDOLIFE_SHIM_USER_BIN",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "PSEUDOLIFE_MCP_TOKEN",
)


def completed():
    scenario, platform = CELL
    return {
        **matrix.pending(scenario, platform),
        "status": "pass",
        "commit": "a" * 40,
        "archive": ARCHIVE,
        "sha256": "b" * 64,
        "machine_role": "disposable VM",
        "commands": [
            {"command": command, "exit_code": 0}
            for command in matrix.commands(scenario, platform)
        ],
        "outcome": matrix.SCENARIOS[scenario].outcome,
        "cleanup": {"done": True, "verified": True},
    }


def ledger(tmp_path, replacement=None, *, omit=None):
    for cell in matrix.cells():
        if cell != omit:
            record = replacement if cell == CELL and replacement else matrix.pending(*cell)
            matrix.write_record(tmp_path, record)


@pytest.mark.parametrize("field", ["machine_role", "outcome", "command", "archive", "extra"])
def test_writer_refuses_identifier(tmp_path, field):
    record = completed()
    private = "account@host.example"
    if field == "command":
        record.update(status="fail", outcome=matrix.FAILURE_OUTCOMES[0])
        record["commands"][0]["command"] += f" {private}"
    else:
        record[field] = private
    with pytest.raises(matrix.RecordError) as error:
        matrix.write_record(tmp_path, record)
    assert private not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_complete_ledger_counts_only_passes(tmp_path, capsys):
    ledger(tmp_path, completed())
    assert matrix.main(["check", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "1 passed, 0 failed, 24 not yet run. Not yet run does not count as a pass."
    )


def test_init_writes_explicit_pending_rows(tmp_path, capsys):
    folder = tmp_path / "records"
    assert matrix.main(["init", str(folder)]) == 0
    assert matrix.main(["check", str(folder)]) == 0
    output = capsys.readouterr().out
    assert "0 passed, 0 failed, 25 not yet run." in output
    assert "windows-x64" in output and "macos-arm64" in output


def test_describe_shows_host_effects_and_the_bounded_linux_candidate(capsys):
    assert matrix.main(["describe", "clean-install", "linux-arm64"]) == 0
    output = capsys.readouterr().out
    for expected in (
        "changes the real user service manager", "needs its own Docker engine",
        "--client-only --daemon-url <fixture-url> --client claude,codex,gemini",
        "--client-update-schedule no --claude-plugin skip --token-file <fixture-token-file>",
        "ufw", "Tailscale", "passkey", "browser", "not yet run",
    ):
        assert expected in output
    assert matrix.main(["describe", "upgrade-and-rollback", "windows-x64"]) == 0
    output = capsys.readouterr().out
    assert "changes the real task scheduler" in output
    assert "--client-only" not in output


def test_missing_cell_fails_checker(tmp_path, capsys):
    ledger(tmp_path, omit=CELL)
    assert matrix.main(["check", str(tmp_path)]) == 1
    assert "clean-install / linux-x64 has no row" in capsys.readouterr().err


def test_malformed_record_fails_checker_without_echoing_fields(tmp_path, capsys):
    ledger(tmp_path)
    path = next(tmp_path.glob("clean-install--linux-x64.json"))
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace('"status":', '"status":"private","status":'), encoding="utf-8")
    assert matrix.main(["check", str(tmp_path)]) == 1
    error = capsys.readouterr().err
    assert "duplicate JSON key" in error and "private" not in error


def test_duplicate_cell_under_another_filename_is_malformed(tmp_path, capsys):
    ledger(tmp_path)
    original = tmp_path / "clean-install--linux-x64.json"
    duplicate = tmp_path / "clean-install--macos-x64.json"
    shutil.copyfile(original, duplicate)
    assert matrix.main(["check", str(tmp_path)]) == 1
    assert "filename must match" in capsys.readouterr().err


def test_failed_cell_is_valid_evidence(tmp_path, capsys):
    record = completed()
    record.update(status="fail", outcome=matrix.FAILURE_OUTCOMES[0])
    record["commands"] = [{**record["commands"][0], "exit_code": 1}]
    record["cleanup"] = {"done": False, "verified": False}
    ledger(tmp_path, record)
    assert matrix.main(["check", str(tmp_path)]) == 0
    assert "0 passed, 1 failed, 24 not yet run." in capsys.readouterr().out


@pytest.mark.parametrize("change", ["cleanup", "command", "exit_code", "platform", "sha256"])
def test_inconsistent_pass_is_malformed(tmp_path, change):
    record = completed()
    if change == "cleanup":
        record["cleanup"]["verified"] = False
    elif change == "command":
        record["commands"].pop()
    elif change == "exit_code":
        record["commands"][0]["exit_code"] = 7
    elif change == "platform":
        record["platform"] = "windows-x64"
    else:
        record[change] = "not a digest"
    with pytest.raises(matrix.RecordError):
        matrix.write_record(tmp_path, record)


def release_folder(tmp_path):
    source = tmp_path / "release"
    source.mkdir()
    fixture = tmp_path / "fake-client"
    fixture.write_bytes(b"stand-in client")
    with tarfile.open(source / ARCHIVE, "w:gz") as archive:
        archive.add(fixture, arcname="pseudolife-mcp")
    payload = (source / ARCHIVE).read_bytes()
    (source / OTHER_ARCHIVE).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = f"{digest}  {ARCHIVE}\n{digest} *{OTHER_ARCHIVE}\n"
    (source / "SHA256SUMS").write_text(manifest, encoding="ascii")
    return source, payload, digest


def test_archive_folder_verifies_and_stages_selected_asset(tmp_path):
    source, payload, digest = release_folder(tmp_path)
    with runner.DisposableHome(tmp_path) as home:
        folder = home.prepare_archives(source, [ARCHIVE])
        assert (folder / ARCHIVE).read_bytes() == payload
        assert (folder / "SHA256SUMS").read_text(encoding="ascii") == f"{digest}  {ARCHIVE}\n"
        assert not (folder / OTHER_ARCHIVE).exists()


def test_tampered_archive_is_unavailable_to_installer(tmp_path):
    source, _, _ = release_folder(tmp_path)
    (source / ARCHIVE).write_bytes(b"tampered")
    with runner.DisposableHome(tmp_path) as home:
        with pytest.raises(runner.PreparationError, match="checksum mismatch"):
            home.prepare_archives(source, [ARCHIVE])
        assert not home.archives.exists()


def test_archive_manifest_refuses_duplicate_names(tmp_path):
    source, _, digest = release_folder(tmp_path)
    with (source / "SHA256SUMS").open("a", encoding="ascii") as manifest:
        manifest.write(f"{digest}  {ARCHIVE}\n")
    with runner.DisposableHome(tmp_path) as home:
        with pytest.raises(runner.PreparationError, match="duplicate"):
            home.prepare_archives(source, [ARCHIVE])


def test_cleanup_refuses_path_it_did_not_create(tmp_path):
    other = tmp_path / "unowned"
    other.mkdir()
    (other / "keep").touch()
    with runner.DisposableHome(tmp_path) as home:
        (home.root / "fixture-output").touch()
        with pytest.raises(runner.PreparationError, match="owned"):
            home.cleanup(other)
        assert (other / "keep").is_file()
    assert home.cleanup_receipt.before == ("unowned",)
    assert home.cleanup_receipt.after == ("unowned",)
    assert home.cleanup_receipt.verified
    assert not home.root.exists()


def test_cleanup_refuses_replaced_root(tmp_path):
    home = runner.DisposableHome(tmp_path)
    moved = tmp_path / "moved-owned-root"
    home.root.rename(moved)
    home.root.mkdir()
    (home.root / "keep").touch()
    with pytest.raises(runner.PreparationError, match="replaced"):
        home.cleanup(home.root)
    assert (home.root / "keep").is_file()


def test_cleanup_refuses_replaced_symlink(tmp_path):
    if os.name == "nt":
        pytest.skip("creating a Windows symlink requires a separate host capability")
    home = runner.DisposableHome(tmp_path)
    moved = tmp_path / "moved-owned-root"
    home.root.rename(moved)
    home.root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(runner.PreparationError, match="link"):
        home.cleanup(home.root)
    assert moved.is_dir() and home.root.is_symlink()


def test_disposable_environment_and_allowlisted_tools(tmp_path, monkeypatch):
    source = tmp_path / "host-tools"
    source.mkdir()
    monkeypatch.setenv("PATH", str(source))
    for name in REDIRECTED + CLEARED:
        monkeypatch.setenv(name, str(tmp_path / name))
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("tar", "claude", "codex", "gemini", "python", "python3", "pip", "py"):
        (source / (name + suffix)).write_bytes(b"fixture")
        (source / (name + suffix)).chmod(0o700)

    with runner.DisposableHome(tmp_path) as home:
        needed = ("tar", "claude", "codex", "gemini")
        path = home.prepare_path({name: source / (name + suffix) for name in needed})
        env = home.environment()
        assert env["PATH"] == str(path)
        assert not set(CLEARED) & env.keys()
        for key in (*REDIRECTED, "APPDATA", "LOCALAPPDATA", "TMPDIR"):
            assert Path(env[key]).is_relative_to(home.root)
        with monkeypatch.context() as search:
            search.chdir(home.root / "work")
            search.setenv("PATHEXT", env["PATHEXT"])
            for name in needed:
                assert shutil.which(name + suffix, path=env["PATH"])
            for name in ("python", "python3", "pip", "py"):
                assert shutil.which(name + suffix, path=env["PATH"]) is None


def test_path_refuses_python_even_when_supplied_as_a_tool(tmp_path):
    python = tmp_path / "python"
    python.touch()
    with runner.DisposableHome(tmp_path) as home:
        with pytest.raises(runner.PreparationError, match="allowlist"):
            home.prepare_path({"python": python})


@pytest.mark.skipif(os.name == "nt", reason="stand-in uses Bash on a POSIX host")
def test_run_installer_only_executes_the_stand_in(tmp_path):
    bash, tar = shutil.which("bash"), shutil.which("tar")
    assert bash and tar
    source, _, _ = release_folder(tmp_path)
    with runner.DisposableHome(tmp_path) as home:
        home.prepare_path({"bash": Path(bash), "tar": Path(tar)})
        home.prepare_archives(source, [ARCHIVE])
        assert runner.run_installer(home) == 0
        assert (home.profile / "fixture-installed").read_text() == "stand-in installed\n"
    assert home.cleanup_receipt.verified
