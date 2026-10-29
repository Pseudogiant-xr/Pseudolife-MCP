"""ops/selfhosted/runner_cleanup.py: what the parity runners' disk cleanup
removes, what it never touches, and that it logs every pass."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "ops" / "selfhosted" / "runner_cleanup.py"


@pytest.fixture
def cleanup(monkeypatch):
    spec = importlib.util.spec_from_file_location("runner_cleanup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Caps in bytes rather than gigabytes, so a few small files exceed them.
    monkeypatch.setattr(module, "GB", 1)
    monkeypatch.setattr(module, "HOME_CACHES", {".cache/pip": 100, ".rustup": 100})
    monkeypatch.setattr(module, "WORK_CAP_GB", 100)
    return module


def write(path: Path, size: int = 10, age_days: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if age_days:
        then = time.time() - age_days * 86400
        os.utime(path, (then, then))
    return path


def runner_tree(tmp_path: Path) -> tuple[Path, Path]:
    runner, home = tmp_path / "runners" / "1", tmp_path / "homes" / "1"
    write(runner / "_work" / "Repo" / "Repo" / "src" / "a.py")
    write(runner / "_work" / "_temp" / "_runner_file_commands" / "set_env")
    write(runner / "_work" / "_actions" / "actions" / "checkout" / "index.js")
    write(runner / "_diag" / "Worker_new.log")
    write(runner / "_diag" / "Worker_old.log", age_days=30)
    write(home / ".cache" / "pip" / "wheel")
    write(home / ".rustup" / "toolchains" / "rustc")
    return runner, home


def run(cleanup, *argv) -> list[dict]:
    log = Path(argv[argv.index("--log") + 1])
    assert cleanup.main(list(argv)) == 0
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_a_runner_under_its_caps_loses_only_old_diagnostic_logs(cleanup, tmp_path):
    runner, home = runner_tree(tmp_path)
    log = tmp_path / "cleanup.jsonl"
    records = run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(log), "--floor-gb", "0")
    assert [Path(item["path"]).name for item in records[0]["removed"]] == ["Worker_old.log"]
    assert (runner / "_diag" / "Worker_new.log").exists()
    assert (home / ".cache" / "pip" / "wheel").exists()
    assert (runner / "_work" / "Repo" / "Repo" / "src" / "a.py").exists()
    assert records[0]["mode"] == "runner 1" and not records[0]["aggressive"]


def test_a_cache_over_its_cap_is_removed_whole_and_only_it(cleanup, tmp_path):
    runner, home = runner_tree(tmp_path)
    write(home / ".cache" / "pip" / "big", 200)
    run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(tmp_path / "log"), "--floor-gb", "0")
    assert not (home / ".cache" / "pip").exists()
    assert (home / ".rustup" / "toolchains" / "rustc").exists()


def test_an_oversized_work_directory_keeps_what_the_starting_job_already_uses(cleanup, tmp_path):
    runner, home = runner_tree(tmp_path)
    write(runner / "_work" / "Repo" / "Repo" / "rust" / "target" / "big", 200)
    run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(tmp_path / "log"), "--floor-gb", "0")
    checkout = runner / "_work" / "Repo" / "Repo"
    # The checkout's contents go; the directory the first step runs in stays.
    assert checkout.is_dir() and not any(checkout.iterdir())
    assert (runner / "_work" / "_temp" / "_runner_file_commands" / "set_env").exists()
    assert (runner / "_work" / "_actions" / "actions" / "checkout" / "index.js").exists()


def test_below_the_floor_every_cap_is_zero(cleanup, tmp_path):
    runner, home = runner_tree(tmp_path)
    records = run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(tmp_path / "log"),
                  "--floor-gb", str(10 ** 18))
    assert records[0]["aggressive"]
    assert not (home / ".cache" / "pip").exists() and not (home / ".rustup").exists()
    assert not any((runner / "_work" / "Repo" / "Repo").iterdir())
    assert (runner / "_work" / "_temp" / "_runner_file_commands" / "set_env").exists()


def test_a_dry_run_reports_and_removes_nothing(cleanup, tmp_path):
    runner, home = runner_tree(tmp_path)
    records = run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(tmp_path / "log"),
                  "--floor-gb", str(10 ** 18), "--dry-run")
    assert records[0]["dry_run"] and len(records[0]["removed"]) >= 4
    assert all(Path(item["path"]).exists() for item in records[0]["removed"])


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_planted_link_never_redirects_a_removal(cleanup, tmp_path):
    runner, home = runner_tree(tmp_path)
    outside = write(tmp_path / "elsewhere" / "pip" / "precious", 200)
    (home / ".cache" / "pip" / "wheel").unlink()
    (home / ".cache" / "pip").rmdir()
    (home / ".cache").rmdir()
    (home / ".cache").symlink_to(tmp_path / "elsewhere")
    records = run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(tmp_path / "log"), "--floor-gb", "0")
    assert outside.exists()
    assert any("is not under" in error for error in records[0]["errors"])


def test_every_pass_is_logged_and_the_log_rotates(cleanup, tmp_path, monkeypatch, capsys):
    runner, home = runner_tree(tmp_path)
    log = tmp_path / "cleanup.jsonl"
    monkeypatch.setattr(cleanup, "LOG_LIMIT", 10)
    run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(log), "--floor-gb", "0")
    run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(log), "--floor-gb", "0")
    assert log.with_name("cleanup.jsonl.1").exists()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1
    assert capsys.readouterr().out.count("runner_cleanup runner 1: freed") == 2


def test_a_failure_never_fails_the_job_and_is_logged(cleanup, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cleanup, "runner_pass", lambda *args: 1 / 0)
    records = run(cleanup, "--runner", str(tmp_path), "--home", str(tmp_path), "--log", str(tmp_path / "log"))
    assert records[0]["errors"] == ["pass ended early: ZeroDivisionError: division by zero"]
    assert "1 errors" in capsys.readouterr().out


def test_the_work_cap_counts_only_the_checkouts(cleanup, tmp_path):
    # The starting job's actions and temp files cannot be freed, so they
    # never push the checkout over its cap.
    runner, home = runner_tree(tmp_path)
    write(runner / "_work" / "_actions" / "big", 500)
    run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(tmp_path / "log"), "--floor-gb", "0")
    assert (runner / "_work" / "Repo" / "Repo" / "src" / "a.py").exists()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="the shared temp pass is Linux only")
def test_the_shared_pass_removes_only_the_accounts_old_temp_entries(cleanup, tmp_path, monkeypatch):
    temp = tmp_path / "tmp"
    old = write(temp / "pl-cf-testlogin-1" / "x", age_days=2)
    old.parent.touch()
    os.utime(old.parent, (time.time() - 2 * 86400,) * 2)
    new = write(temp / "pl-cf-testlogin-2" / "x")
    monkeypatch.setattr(cleanup.shutil, "which", lambda name: None)
    run(cleanup, "--shared", "--temp", str(temp), "--disk", str(tmp_path), "--log", str(tmp_path / "log"), "--floor-gb", "0")
    assert not old.parent.exists() and new.exists()


def test_without_a_posix_owner_the_temp_pass_removes_nothing(cleanup, tmp_path, monkeypatch):
    # Windows has no cheap owner check, and a shared temp directory holds
    # other accounts' files, so there the pass leaves temp alone.
    temp = tmp_path / "tmp"
    old = write(temp / "someone-elses" / "x", age_days=2)
    os.utime(old.parent, (time.time() - 2 * 86400,) * 2)
    monkeypatch.delattr(cleanup.os, "getuid", raising=False)
    monkeypatch.setattr(cleanup.shutil, "which", lambda name: None)
    run(cleanup, "--shared", "--temp", str(temp), "--disk", str(tmp_path), "--log", str(tmp_path / "log"), "--floor-gb", "0")
    assert old.exists()


@pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() == 0,
                    reason="needs POSIX permissions that bind the account (root ignores them)")
def test_an_unreadable_checkout_listing_is_logged_and_the_pass_goes_on(cleanup, tmp_path):
    runner, home = runner_tree(tmp_path)
    write(home / ".cache" / "pip" / "big", 200)
    repo = runner / "_work" / "Repo"
    repo.chmod(0)
    try:
        records = run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(tmp_path / "log"),
                      "--floor-gb", "0")
    finally:
        repo.chmod(0o755)
    assert any(error.startswith(f"{repo}: ") and "Permission denied" in error for error in records[0]["errors"])
    assert not (home / ".cache" / "pip").exists()


@pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() == 0,
                    reason="needs POSIX permissions that bind the account (root ignores them)")
def test_an_unreadable_runner_directory_still_lets_the_home_caches_go(cleanup, tmp_path):
    # Probing _work inside an unsearchable runner directory raises
    # PermissionError; the pass logs it and trims the HOME caches anyway.
    runner, home = runner_tree(tmp_path)
    write(home / ".cache" / "pip" / "big", 200)
    runner.chmod(0)
    try:
        records = run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(tmp_path / "log"),
                      "--floor-gb", "0", "--disk", str(tmp_path))
    finally:
        runner.chmod(0o755)
    assert any(error.startswith(f"{runner / '_work'}: ") for error in records[0]["errors"])
    assert not (home / ".cache" / "pip").exists()


def test_a_pass_that_cannot_start_makes_its_log_directory(cleanup, tmp_path):
    log = tmp_path / "not-yet" / "log"
    assert cleanup.main(["--runner", str(tmp_path / "gone"), "--home", str(tmp_path), "--log", str(log)]) == 0
    assert json.loads(log.read_text(encoding="utf-8"))["errors"][0].startswith("pass did not start: ")


def docker_stub(cleanup, monkeypatch, images: list[str]) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def docker(*args):
        calls.append(args)
        out = "\n".join(images) if args[:2] == ("image", "ls") else "Images 1GB"
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(cleanup, "docker", docker)
    monkeypatch.setattr(cleanup.shutil, "which", lambda name: "docker" if name == "docker" else None)
    return calls


@pytest.mark.parametrize("floor, removed", [("0", ["old1"]), (str(10 ** 18), ["new1", "old1"])])
def test_docker_spares_the_pinned_image(cleanup, tmp_path, monkeypatch, floor, removed):
    stamp = lambda days: time.strftime("%Y-%m-%d %H:%M:%S +0000 UTC", time.gmtime(time.time() - days * 86400))
    calls = docker_stub(cleanup, monkeypatch, [
        f"pin1 pseudolife-pg:18 {stamp(30)}", f"old1 pgvector/pgvector:pg18 {stamp(30)}",
        f"new1 other:latest {stamp(1)}"])
    run(cleanup, "--shared", "--temp", str(tmp_path / "none"), "--disk", str(tmp_path),
        "--log", str(tmp_path / "log"), "--floor-gb", floor)
    assert sorted(call[2] for call in calls if call[:2] == ("image", "rm")) == removed
    prunes = [call for call in calls if call[:2] == ("builder", "prune")]
    assert all("--all" in call for call in prunes) and len(prunes) == 2


def test_a_failed_image_listing_removes_no_image_and_says_so(cleanup, tmp_path, monkeypatch):
    calls: list[tuple[str, ...]] = []

    def docker(*args):
        calls.append(args)
        failed = args[:2] == ("image", "ls")
        # A failed listing's partial output names an old image all the same.
        out = "old1 other:latest 2000-01-01 00:00:00 +0000 UTC" if failed else ""
        return subprocess.CompletedProcess(args, 1 if failed else 0, out, "daemon gone" if failed else "")

    monkeypatch.setattr(cleanup, "docker", docker)
    monkeypatch.setattr(cleanup.shutil, "which", lambda name: "docker" if name == "docker" else None)
    records = run(cleanup, "--shared", "--temp", str(tmp_path / "none"), "--disk", str(tmp_path),
                  "--log", str(tmp_path / "log"), "--floor-gb", str(10 ** 18))
    assert not [call for call in calls if call[:2] == ("image", "rm")]
    assert records[0]["errors"] == ["docker image ls: daemon gone"]


@pytest.mark.parametrize("hours_old, removed", [(7 * 24 + 8, True), (7 * 24 - 8, False)])
def test_an_images_age_counts_its_time_zone(cleanup, tmp_path, monkeypatch, hours_old, removed):
    # Shown in the client's zone (+10:00 here). Read as UTC, or with the
    # offset's sign flipped, an image 7 days 8 hours old would look 10 or
    # 20 hours younger and stay.
    built = time.time() - hours_old * 3600
    local = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(built + 10 * 3600)) + " +1000 AEST"
    calls = docker_stub(cleanup, monkeypatch, [f"img1 other:latest {local}"])
    run(cleanup, "--shared", "--temp", str(tmp_path / "none"), "--disk", str(tmp_path),
        "--log", str(tmp_path / "log"), "--floor-gb", "0")
    assert [call[2] for call in calls if call[:2] == ("image", "rm")] == (["img1"] if removed else [])


def test_a_pass_that_cannot_start_is_still_logged(cleanup, tmp_path, capsys):
    log = tmp_path / "log"
    assert cleanup.main(["--runner", str(tmp_path / "gone"), "--home", str(tmp_path), "--log", str(log)]) == 0
    record = json.loads(log.read_text(encoding="utf-8"))
    assert record["mode"] == "runner gone" and record["errors"][0].startswith("pass did not start: ")
    assert "nothing was removed" in capsys.readouterr().err


def test_the_script_runs_standalone_with_the_standard_library(tmp_path):
    runner, home = runner_tree(tmp_path)
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "--runner", str(runner), "--home", str(home),
                             "--log", str(tmp_path / "log"), "--dry-run"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("runner_cleanup runner 1: would free")


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows only")
def test_a_junction_is_neither_measured_through_nor_emptied(cleanup, tmp_path):
    # A job can make a junction without any privilege; it counts as a link.
    runner, home = runner_tree(tmp_path)
    outside = write(tmp_path / "elsewhere" / "precious", 500)
    junction = home / ".cache" / "pip" / "into-elsewhere"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside.parent)],
                          capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    assert cleanup.size(home / ".cache" / "pip") == 10
    write(home / ".cache" / "pip" / "big", 200)
    run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(tmp_path / "log"), "--floor-gb", "0")
    assert not (home / ".cache" / "pip").exists()
    assert outside.read_bytes() == b"x" * 500


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows only")
def test_a_junction_in_a_checkout_goes_and_its_target_stays(cleanup, tmp_path):
    # Here the cleanup removes the junction itself, not CPython's rmtree.
    runner, home = runner_tree(tmp_path)
    outside = write(tmp_path / "elsewhere" / "precious", 500)
    checkout = runner / "_work" / "Repo" / "Repo"
    write(checkout / "big", 200)
    junction = checkout / "into-elsewhere"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside.parent)],
                          capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    records = run(cleanup, "--runner", str(runner), "--home", str(home), "--log", str(tmp_path / "log"),
                  "--floor-gb", "0")
    assert not os.path.lexists(junction) and outside.read_bytes() == b"x" * 500
    assert {"path": str(junction), "bytes": 0, "why": "checkout over its cap"} in records[0]["removed"]
