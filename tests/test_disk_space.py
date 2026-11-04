"""Pure preflight boundaries; Docker and filesystem probes are replaced."""
from types import SimpleNamespace

import pytest

from pseudolife_memory import disk_space as disk, update_cli as up


def test_host_checks_nearest_existing_ancestor_without_creating_it(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda path: seen.append(path) or SimpleNamespace(free=disk.MIN_FREE))
    target = tmp_path / "missing" / "backups"
    disk.host(target)
    assert seen == [tmp_path] and not target.parent.exists()


@pytest.mark.parametrize("output", ["", "not a df response", "overlay 1 1 -1 1% /", "overlay 1 1 18446744073709551615 1% /"])
def test_malformed_df_is_refused(output):
    with pytest.raises(up.UpdateError, match="df -Pk"):
        disk._available(output, "fixture")


@pytest.mark.parametrize("free", [disk.MIN_FREE - 1, disk.MIN_FREE])
def test_exact_floor(free):
    if free == disk.MIN_FREE:
        disk.require(free, "fixture")
    else:
        with pytest.raises(up.UpdateError, match="8 GiB"):
            disk.require(free, "fixture")


def test_first_build_without_an_image_warns_without_creating_a_helper(monkeypatch):
    calls, warnings = [], []
    monkeypatch.setattr(up, "run_cli", lambda argv, **kw: (calls.append(argv) or 1, "stopped"))
    disk.docker_storage("docker", ["fixture-pg", "fixture-daemon"], warn=warnings.append)
    assert warnings and all(call[1] == "exec" for call in calls)


def test_timed_out_create_cleans_up_only_the_known_helper_name(monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return (0, "") if argv[1] == "rm" else (1, "timed out")
    monkeypatch.setattr(up, "run_cli", run)
    with pytest.raises(up.UpdateError, match="create"):
        disk.docker_storage("docker", [], "sha256:fixture")
    create = calls[0]
    assert calls[-1] == ["docker", "rm", "-f", "-v", create[create.index("--name") + 1]]


def test_cleanup_failure_names_the_owned_helper(monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return (1, "") if argv[1] == "rm" else (0, "overlay 99999999 1 8388608 1% /")
    monkeypatch.setattr(up, "run_cli", run)
    with pytest.raises(up.UpdateError, match="docker rm -f -v pseudolife-diskcheck-"):
        disk.docker_storage("docker", [], "sha256:fixture")


def test_stopped_daemon_keeps_the_ordinary_sql_only_backup_path(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(up.Update, "running_image", lambda self: ("sha256:fixture", "fixture:old"))
    def run(argv, **kw):
        calls.append(argv)
        if "inspect" in argv:
            return 0, "false"
        if argv[1:3] == ["exec", up.DAEMON_CONTAINER]:
            return 1, "stopped"
        return 0, "overlay 99999999 1 8388608 1% /"
    monkeypatch.setattr(up, "run_cli", run)
    up.Update(up.Options()).preflight_disk(tmp_path, temporary=True)
    assert not any(call[1:3] == ["exec", up.DAEMON_CONTAINER] and call[-1] == "/tmp" for call in calls)
