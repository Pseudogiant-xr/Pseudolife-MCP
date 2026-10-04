"""``pseudolife-mcp update`` (pseudolife_memory/update_cli.py): one deploy
implementation behind ops/update.ps1, ops/update.sh and the installed
command.

Every docker call goes through ``run_cli`` and every HTTP read through
``fetch_json``, so a fake world stands in for Docker, PyPI and the daemon;
the checkout-mode tests use real git on a sandbox tree, as the shell
tests they replace did (the clean-tree guard, the build stamp, the
rollback-tag guard, credential scoping and the honest rollback text are
the contracts carried over from tests/test_ops_update_*.py).
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pseudolife_memory import client_updates, update_cli as up  # noqa: E402

DAEMON = up.DAEMON_CONTAINER
PG = up.PG_CONTAINER
_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_TRACE")


class World:
    """Docker, PyPI, the daemon and the clock, as a table the tests set."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.container: dict | None = None      # {"id", "ref", "labels"}
        self.images: dict[str, str] = {}        # tag -> image id
        self.image_list: list[tuple[str, str, str]] = []   # (created, ref, id) for prune
        self.pull_rc = 0
        self.compose_rc = 0
        self.tag_rc = 0
        self.dump_complete = True
        self.dump_rc = 0
        self.tar_rc = 0
        self.health: list = []                  # successive /health answers
        self.pypi: str | None = "0.15.1"
        self.slept: list[float] = []
        self.pip_rc = 0
        self.on_image_inspect = None
        self.docker_down = False

    # docker -----------------------------------------------------------------
    def run_cli(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.envs.append(kw.get("env"))
        name = Path(argv[0]).name.lower()
        if name in ("docker", "fake-docker"):
            return self.docker(argv[1:])
        if argv[1:4] == ["-m", "pip", "install"] or name == "pipx":
            return self.pip_rc, "Successfully installed" if self.pip_rc == 0 else "ERROR: [WinError 32] locked"
        if name in ("pwsh", "bash"):
            return 0, "script ran"
        return 91, f"unexpected {argv}"

    def docker(self, a: list[str]):
        if self.docker_down:
            return 1, "error during connect: this error may indicate that the docker daemon is not running"
        if a[:2] == ["inspect", "-f"] or a[:2] == ["inspect", "--format"]:
            fmt, target = a[2], a[3]
            if target == DAEMON:
                if self.container is None:
                    return 1, "Error: No such object"
                if "Labels" in fmt:
                    return 0, json.dumps(self.container.get("labels", {})) + "\n"
                if "{{.Image}}|{{.Config.Image}}" in fmt:
                    return 0, f"{self.container['id']}|{self.container['ref']}\n"
                if "{{.Image}}" in fmt:
                    return 0, self.container["id"] + "\n"
                return 0, self.container["id"] + "\n"
            return 1, "Error: No such object"
        if a[:1] == ["inspect"]:      # prune: images of running containers
            return 0, "\n".join(self.container["id"] for _ in a[2:] if self.container) + "\n"
        if a[:2] == ["image", "inspect"]:
            if self.on_image_inspect:
                self.on_image_inspect()
            fmt = a[3] if a[2] in ("-f", "--format") else ""
            ref = a[-1]
            if ref in self.images:
                if "Created" in fmt:
                    created = next((c for c, r, _ in self.image_list if r == ref), "2026-01-01T00:00:00Z")
                    return 0, f"{created}|{self.images[ref]}\n"
                return 0, self.images[ref] + "\n"
            return 1, "Error: No such image"
        if a[:2] == ["image", "ls"]:
            return 0, "\n".join(ref for _, ref, _ in self.image_list) + "\n"
        if a[:1] == ["pull"]:
            return self.pull_rc, "Status: Downloaded" if self.pull_rc == 0 else "Error response from daemon: manifest unknown"
        if a[:1] == ["tag"]:
            if self.tag_rc == 0:
                self.images[a[2]] = self.images.get(a[1], a[1])
            return self.tag_rc, ""
        if a[:1] == ["rmi"]:
            return 0, "Untagged"
        if a[:1] == ["ps"]:
            return 0, ("c1\n" if self.container else "")
        if a[:1] == ["compose"]:
            return self.compose_rc, "" if self.compose_rc == 0 else "error: build failed"
        if a[:1] == ["builder"]:
            return 0, ""
        if a[:1] == ["exec"] and "pg_dump" in " ".join(a):
            return self.dump_rc, "" if self.dump_rc == 0 else "pg_dump: error: connection failed"
        if a[:1] == ["exec"] and "tar czf" in " ".join(a):
            return self.tar_rc, ""
        if a[:1] == ["exec"]:
            return 0, ""
        if a[:1] == ["cp"]:
            target = Path(a[2])
            if a[1].startswith(f"{PG}:"):
                body = "SET x;\nCOPY public.entries (id) FROM stdin;\n-- PostgreSQL database dump complete\n\\.\n"
                if self.dump_complete:
                    body += "-- PostgreSQL database dump complete\n"
                target.write_bytes(gzip.compress(body.encode()))
            else:
                target.write_bytes(b"state-tar")
            return 0, ""
        return 91, f"unexpected docker {a}"

    # http -------------------------------------------------------------------
    def fetch_json(self, url: str, timeout: float = 5.0):
        if url.startswith(up.PYPI_JSON):
            return {"info": {"version": self.pypi}} if self.pypi else None
        if url.endswith("/health"):
            if not self.health:
                return None
            answer = self.health[0]
            if len(self.health) > 1:
                self.health.pop(0)
            return answer

    def docker_calls(self) -> list[str]:
        return [" ".join(c[1:]) for c in self.calls if Path(c[0]).name.lower() in ("docker", "fake-docker")]


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(up, "run_cli", w.run_cli)
    monkeypatch.setattr(up, "fetch_json", w.fetch_json)
    monkeypatch.setattr(up, "sleep", lambda s: w.slept.append(s))
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setenv("PSEUDOLIFE_DOCKER", "fake-docker")
    monkeypatch.setattr(up, "which", lambda name: {"git": "git", "bash": "bash", "pwsh": "pwsh",
                                                   "pipx": None}.get(name, f"/usr/bin/{name}"))
    for name in _GIT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    # the pip tier's own-install checks: a plain site install, a runtime layout elsewhere
    monkeypatch.setattr(client_updates, "install_kind", lambda interpreter: ("site", ""))
    # this interpreter's own extras are not the test's: no lite extra unless a test says so
    monkeypatch.setattr(up, "lite_installed", lambda: False)
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(tmp_path / "rt"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_LAUNCHER", str(tmp_path / "bin" / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")))
    return w


@pytest.fixture
def clients(monkeypatch):
    """The client side, recorded not run."""
    seen: list[dict] = []

    def run_steps(steps, *, repo, source, daemon_digest=None, reinstall=False):
        seen.append({"steps": tuple(steps), "repo": repo, "source": source, "daemon_digest": daemon_digest,
                     "reinstall": reinstall})
        return {"shim": {"state": "installed:0.15.1", "detail": "ok"}, "plugin": {"state": "current:0.15.1", "detail": "ok"},
                "codex": {"state": "current", "detail": "ok"}, "ok": True}

    monkeypatch.setattr(client_updates, "run_steps", run_steps)
    return seen


def _project(world: World, tmp_path: Path, *, files_exist: bool = True, checkout: bool = False,
             version: str = "0.15.0") -> Path:
    """A daemon container created from a compose project under tmp_path."""
    project = tmp_path / "project"
    ops = project / "ops" if checkout else project
    ops.mkdir(parents=True, exist_ok=True)
    compose = ops / "docker-compose.yml"
    overlay = ops / "docker-compose.ghcr.yml"
    env_file = ops / ".env"
    if files_exist:
        compose.write_text(f"image: pseudolife-daemon:{version}\n", encoding="utf-8")
        overlay.write_text("overlay\n", encoding="utf-8")
        env_file.write_text("# nothing\n", encoding="utf-8")
    if checkout:
        (project / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    world.container = {"id": "sha256:running", "ref": f"pseudolife-daemon:{version}",
                       "labels": {"com.docker.compose.project.config_files": str(compose),
                                  "com.docker.compose.project.working_dir": str(ops),
                                  "com.docker.compose.project.environment_file": str(env_file)}}
    world.images[f"pseudolife-daemon:{version}"] = "sha256:running"
    world.health = [{"status": "ok", "version": version, "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64}]
    return project


def _run(argv: list[str], capsys=None) -> int:
    return up.main(argv)


# ── release mode ────────────────────────────────────────────────────────────

def test_a_release_update_pulls_backs_up_tags_recreates_and_moves_the_clients(world, clients, tmp_path, capsys):
    project = _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, None, {"status": "ok", "version": "0.15.1", "schema": 50,
                                                                    "persist_errors": 0, "hooks_digest": "e" * 64}]
    assert _run(["--health-delay-ms", "1"]) == 0
    calls = world.docker_calls()
    assert f"pull {up.GHCR_IMAGE}:0.15.1" in calls
    pull = calls.index(f"pull {up.GHCR_IMAGE}:0.15.1")
    dump = next(i for i, c in enumerate(calls) if "pg_dump" in c)
    tag = next(i for i, c in enumerate(calls) if c.startswith("tag "))
    compose = next(i for i, c in enumerate(calls) if c.startswith("compose "))
    assert pull < dump < tag < compose, calls
    assert calls[tag].startswith(f"tag sha256:running {up.GHCR_IMAGE}:0.15.0-pre-update-")
    assert calls[compose].endswith("up -d --no-deps pseudolife-daemon") and "--build" not in calls[compose]
    assert calls[compose].startswith("compose -p pseudolife-mcp ")
    assert f"-f {project / 'docker-compose.yml'} -f {project / 'docker-compose.ghcr.yml'}" in calls[compose]
    assert f"--env-file {project / '.env'}" in calls[compose]
    assert any(c.startswith(f"image ls {up.GHCR_IMAGE}") for c in calls)       # retention in the GHCR repository
    assert not any(c.startswith("builder") for c in calls)                     # nothing was built
    env = world.envs[[i for i, c in enumerate(world.calls) if c[1:2] == ["compose"]][0]]
    assert env["PSEUDOLIFE_IMAGE_TAG"] == "0.15.1"
    backups = sorted(p.name for p in (tmp_path / "data" / "backups").iterdir())
    assert any(n.startswith("pseudolife_memory-") and n.endswith(".sql.gz") for n in backups)
    assert any(n.startswith("pseudolife_state-") for n in backups)
    assert not any(n.endswith(".part") for n in backups)
    assert clients == [{"steps": ("shim", "plugin", "codex"), "repo": None, "source": "pseudolife-mcp==0.15.1",
                        "daemon_digest": "e" * 64, "reinstall": False}]
    out = capsys.readouterr().out
    assert "healthy. version=0.15.1" in out
    # the overlay selects the image through PSEUDOLIFE_IMAGE_TAG: that is the rollback
    assert "PSEUDOLIFE_IMAGE_TAG=0.15.0-pre-update-" in out and "$env:PSEUDOLIFE_IMAGE_TAG='0.15.0-pre-update-" in out
    assert "docker tag " not in out
    assert world.slept == [0.001]


def test_the_same_version_is_not_redeployed_without_reinstall(world, clients, tmp_path):
    _project(world, tmp_path, version="0.15.1")
    assert _run([]) == 0
    assert not any(c.startswith(("pull", "compose", "tag")) for c in world.docker_calls())
    assert clients == []
    assert _run(["--reinstall", "--health-delay-ms", "1"]) == 0
    assert any(c.startswith("compose") for c in world.docker_calls())
    # the shim step installs the release again too, not only the daemon
    assert clients[-1]["reinstall"] is True


def test_a_pinned_tag_wins_over_pypi_and_a_failed_pull_changes_nothing(world, clients, tmp_path, capsys):
    _project(world, tmp_path)
    world.pull_rc = 1
    assert _run(["--tag", "0.16.0"]) == 1
    calls = world.docker_calls()
    assert f"pull {up.GHCR_IMAGE}:0.16.0" in calls
    assert not any(c.startswith(("tag", "compose", "exec")) for c in calls)
    assert "nothing was changed" in capsys.readouterr().err


def test_without_pypi_nothing_is_guessed(world, clients, tmp_path, capsys):
    """A shim at 0.14 with PyPI unreachable must not pull 0.14 over a 0.15
    daemon: no target means no update."""
    _project(world, tmp_path, version="0.0.1")
    world.pypi = None
    assert _run(["--health-delay-ms", "1"]) == 2
    assert not any(c.startswith("pull") for c in world.docker_calls())
    assert "name one with --tag" in capsys.readouterr().err


def test_a_downgrade_is_refused_unless_allowed(world, clients, tmp_path, capsys):
    _project(world, tmp_path, version="0.15.2")
    assert _run(["--tag", "0.15.1"]) == 2
    assert "older than the running 0.15.2" in capsys.readouterr().err
    assert not any(c.startswith("pull") for c in world.docker_calls())
    world.health = [{"status": "ok", "version": "0.15.2"}, {"status": "ok", "version": "0.15.1"}]
    assert _run(["--tag", "0.15.1", "--allow-downgrade", "--health-delay-ms", "1"]) == 0
    assert f"pull {up.GHCR_IMAGE}:0.15.1" in world.docker_calls()


def test_docker_present_but_not_answering_stops_the_update(world, clients, tmp_path, capsys):
    """Docker Desktop stopped is not "no daemon container": the shim
    runtime this runs from must never be pip-upgraded in its place."""
    _project(world, tmp_path)
    world.docker_down = True
    assert _run([]) == 2
    assert "did not answer" in capsys.readouterr().err
    assert not any(c[1:4] == ["-m", "pip", "install"] for c in world.calls)


def test_the_bundled_compose_files_are_used_when_the_projects_are_gone(world, clients, tmp_path, capsys):
    """Compose files gone: the bundled copies. The env file gone too: stop,
    since recreating without it resets the Postgres password, the volume
    names and the bearer; --env-file names a replacement."""
    _project(world, tmp_path, files_exist=False)
    assert _run(["--health-delay-ms", "1"]) == 2
    assert "no longer exist" in capsys.readouterr().err and not any(c.startswith("pull") for c in world.docker_calls())
    env_file = tmp_path / "kept.env"
    env_file.write_text("POSTGRES_PASSWORD=fixture\n", encoding="utf-8")
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    assert _run(["--health-delay-ms", "1", "--env-file", str(env_file)]) == 0
    compose = next(c for c in world.docker_calls() if c.startswith("compose"))
    bundled = tmp_path / "data" / "compose"
    assert f"-f {bundled / 'docker-compose.yml'} -f {bundled / 'docker-compose.ghcr.yml'}" in compose
    assert f"--env-file {env_file}" in compose
    assert (bundled / "docker-compose.yml").read_bytes() == (ROOT / "ops" / "docker-compose.yml").read_bytes()
    assert "bundled" in capsys.readouterr().out


def test_the_env_file_falls_back_to_the_working_directorys(world, clients, tmp_path):
    """No environment_file label (compose loaded <working dir>/.env
    implicitly): that file is what the recreate gets, and its bearer is
    scoped out of the caller's environment."""
    project = _project(world, tmp_path)
    del world.container["labels"]["com.docker.compose.project.environment_file"]
    (project / ".env").write_text("PSEUDOLIFE_MCP_TOKEN=daemon-fixture\n", encoding="utf-8")
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    os.environ["PSEUDOLIFE_MCP_TOKEN"] = "client-bearer"
    try:
        assert _run(["--health-delay-ms", "1"]) == 0
    finally:
        os.environ.pop("PSEUDOLIFE_MCP_TOKEN", None)
    argv, env = _compose_call(world)
    assert f"--env-file {project / '.env'}" in " ".join(argv)
    assert "PSEUDOLIFE_MCP_TOKEN" not in env and env["PSEUDOLIFE_IMAGE_TAG"] == "0.15.1"


def test_bundled_compose_files_match_the_checkouts():
    for name in ("docker-compose.yml", "docker-compose.ghcr.yml"):
        assert (up.BUNDLED_COMPOSE / name).read_bytes() == (ROOT / "ops" / name).read_bytes(), name


def test_a_projects_own_backup_script_is_used_when_its_checkout_still_exists(world, clients, tmp_path):
    project = _project(world, tmp_path, checkout=True)
    script = project / "ops" / ("backup.ps1" if os.name == "nt" else "backup.sh")
    script.write_text("# fake\n", encoding="utf-8")
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    assert _run(["--health-delay-ms", "1"]) == 0
    assert any(str(script) in " ".join(c) for c in world.calls)
    assert not any("pg_dump" in c for c in world.docker_calls())
    # the clients compare with the daemon just deployed, never with that checkout at some commit
    assert clients[0]["repo"] is None and clients[0]["source"] == "pseudolife-mcp==0.15.1"


def test_an_incomplete_dump_stops_the_update_before_the_daemon_moves(world, clients, tmp_path, capsys):
    _project(world, tmp_path)
    world.dump_complete = False
    assert _run([]) == 1
    calls = world.docker_calls()
    assert any(c.startswith("pull") for c in calls) and not any(c.startswith(("tag", "compose")) for c in calls)
    err = capsys.readouterr().err
    assert "INCOMPLETE" in err and ".part" in err
    part = list((tmp_path / "data" / "backups").glob("*.part"))
    assert len(part) == 1


def test_a_failed_pg_dump_stops_the_update(world, clients, tmp_path, capsys):
    _project(world, tmp_path)
    world.dump_rc = 1
    assert _run([]) == 1
    assert "pg_dump failed" in capsys.readouterr().err
    assert not any(c.startswith("compose") for c in world.docker_calls())


def test_an_unhealthy_release_deploy_prints_the_rollback_and_exits_1(world, clients, tmp_path, capsys):
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, None, None, None]
    assert _run(["--health-retries", "2", "--health-delay-ms", "1"]) == 1
    err = capsys.readouterr().err
    assert "did not report healthy" in err and "PSEUDOLIFE_IMAGE_TAG=0.15.0-pre-update-" in err
    assert clients == []


def test_clients_only_and_daemon_only(world, clients, tmp_path):
    _project(world, tmp_path)
    assert _run(["--clients-only"]) == 0
    assert clients == [{"steps": ("shim", "plugin", "codex"), "repo": None, "source": "pseudolife-mcp==0.15.1",
                        "daemon_digest": "d" * 64, "reinstall": False}]
    assert not any(c.startswith(("pull", "compose")) for c in world.docker_calls())
    clients.clear()
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    assert _run(["--daemon-only", "--health-delay-ms", "1"]) == 0
    assert clients == [] and any(c.startswith("compose") for c in world.docker_calls())


@pytest.fixture
def held_update_lock(world, tmp_path):
    """Another ``pseudolife-mcp update`` on this host: the OS lock on
    ``update.lock`` held by a second handle, with its pid recorded."""
    from pseudolife_memory import os_lock

    lock = os_lock.OsLock(tmp_path / "data" / up.LOCK_NAME)
    assert lock.acquire()
    (tmp_path / "data" / up.LOCK_HOLDER_NAME).write_text("4242\n", encoding="utf-8")
    yield lock
    lock.release()


def test_an_attended_update_refuses_while_another_update_holds_the_lock(world, clients, tmp_path, capsys,
                                                                        held_update_lock):
    """Two updates at once: the second could tag the first one's new image
    as the rollback of the old version, so it stops before anything runs."""
    _project(world, tmp_path)
    for argv in ([], ["--clients-only"], ["--checkout", str(tmp_path / "project")]):
        assert _run(argv) == 2, argv
        assert "another pseudolife-mcp update is running (pid 4242)" in capsys.readouterr().err
    assert not any(c.startswith(("pull", "tag", "compose", "exec")) for c in world.docker_calls())
    assert clients == []
    world.health = [{"status": "ok", "version": "0.15.0"}]
    assert _run(["--check"]) == 0                                # a read-only check takes no lock


def test_the_update_lock_is_released_on_every_exit_path(world, clients, tmp_path, monkeypatch):
    from pseudolife_memory import os_lock

    path = tmp_path / "data" / up.LOCK_NAME
    # release() removes the pid file; the probe alone cannot tell, since a
    # collected handle drops the OS lock too
    holder = tmp_path / "data" / up.LOCK_HOLDER_NAME
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    assert _run(["--health-delay-ms", "1"]) == 0                   # success
    assert os_lock.probe(path) is False and not holder.exists()
    _project(world, tmp_path)
    world.pull_rc = 1
    assert _run([]) == 1                                           # an UpdateError
    assert os_lock.probe(path) is False and not holder.exists()
    world.pull_rc = 0
    real = world.run_cli

    def boom(argv, **kw):
        if "pull" in [str(a) for a in argv]:
            raise RuntimeError("unexpected")
        return real(argv, **kw)

    monkeypatch.setattr(up, "run_cli", boom)
    with pytest.raises(RuntimeError):
        _run([])                                                   # an unexpected exception
    assert os_lock.probe(path) is False and not holder.exists()


def test_a_version_mismatch_after_the_recreate_fails_and_moves_no_client(world, clients, tmp_path, capsys):
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.0"}]
    assert _run(["--health-delay-ms", "1"]) == 1
    err = capsys.readouterr().err
    assert "not the 0.15.1 that was pulled" in err and "PSEUDOLIFE_IMAGE_TAG=0.15.0-pre-update-" in err
    assert clients == []


def test_release_mode_options_are_refused_on_a_checkout_deploy(world, clients, tmp_path, capsys):
    root, _ = _checkout(world, tmp_path)
    for flag in (["--clients-only"], ["--tag", "1.0"], ["--reinstall"]):
        assert _run(["--checkout", str(root), *flag]) == 2
    assert "release-mode option" in capsys.readouterr().err
    assert not any(Path(c[0]).name == "fake-docker" for c in world.calls)


def test_the_halves_are_refused_on_a_pip_install(world, capsys):
    assert _run(["--clients-only"]) == 2 and _run(["--daemon-only"]) == 2
    assert "no daemon container here" in capsys.readouterr().err
    assert not any(c[1:4] == ["-m", "pip", "install"] for c in world.calls)


def test_the_codex_step_is_printed_when_the_hook_copy_is_stale(world, tmp_path, monkeypatch, capsys):
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    monkeypatch.setattr(client_updates, "run_steps", lambda steps, **kw: {
        "shim": {"state": "installed:0.15.1", "detail": "ok"}, "plugin": {"state": "current:0.15.1", "detail": "ok"},
        "codex": {"state": "stale", "detail": "stale"}, "ok": True})
    assert _run(["--health-delay-ms", "1"]) == 0
    assert "/hooks" in capsys.readouterr().out


def test_the_json_report_carries_the_run(world, clients, tmp_path, capsys):
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    assert _run(["--json", "--health-delay-ms", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tier"] == "docker" and report["mode"] == "release" and report["target"] == "0.15.1"
    assert report["rollback"]["state"] == "tagged" and report["clients"]["ok"] is True and report["ok"] is True


# ── --check ─────────────────────────────────────────────────────────────────

def test_check_reports_a_newer_release_or_current(world, tmp_path, capsys):
    _project(world, tmp_path)
    assert _run(["--check"]) == 0
    assert "update available: 0.15.0 -> 0.15.1" in capsys.readouterr().out
    world.health = [{"status": "ok", "version": "0.15.1"}]
    assert _run(["--check"]) == 3
    assert "is the newest release" in capsys.readouterr().out
    world.pypi = None
    assert _run(["--check"]) == 2
    world.pypi = "0.15.1"
    world.health = []
    assert _run(["--check"]) == 2


def test_check_on_a_pip_install_compares_the_package(world, capsys):
    world.pypi = "99.0.0"
    assert _run(["--check"]) == 0
    assert f"update available: {up.__version__} -> 99.0.0" in capsys.readouterr().out


# ── pip tier ────────────────────────────────────────────────────────────────

def test_a_pip_install_is_upgraded_where_it_is_and_nothing_is_restarted(world, capsys):
    """POSIX: pip upgrades the running install in place. Windows: pip cannot
    replace the running console script, so the exact command is printed
    for a shell where nothing runs."""
    world.pypi = "99.0.0"
    assert _run([]) == 0
    pip = [c for c in world.calls if c[1:4] == ["-m", "pip", "install"]]
    out = capsys.readouterr().out
    command = f"{sys.executable} -m pip install --upgrade pseudolife-mcp==99.0.0"
    if os.name == "nt":
        assert pip == [] and command in out and "run this from a shell" in out
    else:
        assert pip == [[sys.executable, "-m", "pip", "install", "--upgrade", "pseudolife-mcp==99.0.0"]]
        assert "Nothing running was restarted" in out
    assert "back it up first" in out
    world.calls.clear()
    world.pypi = up.__version__
    assert _run([]) == 0
    assert not [c for c in world.calls if c[1:4] == ["-m", "pip", "install"]]
    assert "already the target version" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="on Windows the command is printed, not run")
def test_a_failed_pip_upgrade_is_named(world, capsys):
    world.pypi = "99.0.0"
    world.pip_rc = 1
    assert _run([]) == 1
    assert "WinError 32" in capsys.readouterr().err


def test_a_lite_install_is_upgraded_with_its_embedded_postgres(world, monkeypatch, capsys):
    """pipx --force rebuilds the venv and pip installs only what it is
    asked for: a requirement without [lite] leaves the upgraded install
    without pg0-embedded, and the daemon then refuses to start. The step
    line names the requirement; the printed command quotes it, since the
    brackets are a glob in zsh."""
    world.pypi = "99.0.0"
    monkeypatch.setattr(up, "lite_installed", lambda: True)
    assert _run([]) == 0
    out = capsys.readouterr().out
    assert "installs pseudolife-mcp[lite]==99.0.0" in out
    pip = [c for c in world.calls if c[1:4] == ["-m", "pip", "install"]]
    if os.name == "nt":
        assert pip == [] and '-m pip install --upgrade "pseudolife-mcp[lite]==99.0.0"' in out
    else:
        assert pip == [[sys.executable, "-m", "pip", "install", "--upgrade", "pseudolife-mcp[lite]==99.0.0"]]
    # pipx: the command is printed on every platform, with the extra
    monkeypatch.setenv("PIPX_HOME", sys.prefix)
    world.calls.clear()
    assert _run([]) == 0
    out = capsys.readouterr().out
    assert 'pipx install --force "pseudolife-mcp[lite]==99.0.0"' in out
    assert not [c for c in world.calls if Path(c[0]).name.lower().startswith("pipx")]
    # without the extra the requirement is the bare package
    monkeypatch.setattr(up, "lite_installed", lambda: False)
    assert _run([]) == 0
    out = capsys.readouterr().out
    assert "pipx install --force pseudolife-mcp==99.0.0" in out and "[lite]" not in out


def test_a_pip_upgrade_never_lands_on_an_editable_checkout(world, monkeypatch, capsys):
    world.pypi = "99.0.0"
    monkeypatch.setattr(client_updates, "install_kind", lambda interpreter: ("editable", str(ROOT)))
    assert _run([]) == 2
    assert "editable install" in capsys.readouterr().err
    assert not any(c[1:4] == ["-m", "pip", "install"] for c in world.calls)


# ── checkout mode ───────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.com",
                           "-c", "core.autocrlf=false", *args], check=True, capture_output=True, text=True).stdout.strip()


def _checkout(world: World, tmp_path: Path, *, repo: bool = True, dirty: int = 0, version: str = "0.1.0") -> tuple[Path, str]:
    root = tmp_path / "checkout"
    (root / "ops").mkdir(parents=True)
    (root / "ops" / "docker-compose.yml").write_text(f"image: pseudolife-daemon:{version}\n", encoding="utf-8", newline="\n")
    (root / "ops" / ".env.example").write_text("# PSEUDOLIFE_MCP_TOKEN=\n", encoding="utf-8", newline="\n")
    (root / "README.md").write_text("sandbox\n", encoding="utf-8", newline="\n")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8", newline="\n")
    # as in the repository: the scaffolded ops/.env is gitignored, so it
    # never makes a clean tree dirty between the guard and the build
    (root / ".gitignore").write_text("ops/.env\n", encoding="utf-8", newline="\n")
    sha = ""
    if repo:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "sandbox")
        sha = _git(root, "rev-parse", "HEAD")
    if dirty:
        (root / "README.md").write_text("edited by another session\n", encoding="utf-8", newline="\n")
        for i in range(dirty - 1):
            (root / f"notes-{i:02d}.md").write_text("x\n", encoding="utf-8")
    world.container = {"id": "sha256:running", "ref": f"pseudolife-daemon:{version}", "labels": {}}
    world.images[f"pseudolife-daemon:{version}"] = "sha256:running"
    world.health = [{"status": "ok", "version": version, "schema": 49, "persist_errors": 0}]
    return root, sha


def _compose_call(world: World):
    for argv, env in zip(world.calls, world.envs):
        if argv[1:2] == ["compose"]:
            return argv, env
    return None, None


def test_a_clean_checkout_is_built_with_its_commit_stamp(world, clients, tmp_path):
    root, sha = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune", "--rollback-tag", "unittest"]) == 0
    argv, env = _compose_call(world)
    assert argv[-5:] == ["up", "-d", "--no-deps", "--build", "pseudolife-daemon"]
    assert f"-f {root / 'ops' / 'docker-compose.yml'}" in " ".join(argv)
    assert env["PSEUDOLIFE_BUILD_GIT_SHA"] == sha and env["PSEUDOLIFE_BUILD_DIRTY"] == "false"
    assert env["PSEUDOLIFE_BUILD_TIME"].endswith("Z") and "T" in env["PSEUDOLIFE_BUILD_TIME"]
    assert "PSEUDOLIFE_BUILD_GIT_SHA" not in os.environ      # the caller's environment is left alone
    assert "tag pseudolife-daemon:0.1.0 pseudolife-daemon:0.1.0-unittest" in world.docker_calls()
    assert (root / "ops" / ".env").is_file()                # scaffolded from the example
    assert clients == []                                    # no --all


def test_all_moves_the_clients_from_the_checkout(world, clients, tmp_path):
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune", "--all"]) == 0
    assert clients == [{"steps": ("shim", "plugin", "codex"), "repo": root, "source": str(root), "daemon_digest": None,
                       "reinstall": False}]


def test_a_dirty_tree_is_refused_before_anything_runs(world, clients, tmp_path, capsys):
    """Before the backup too: the guard is step 0."""
    root, _ = _checkout(world, tmp_path, dirty=2)
    assert _run(["--checkout", str(root)]) == 1
    err = capsys.readouterr().err
    assert "REFUSING to deploy" in err and "2 uncommitted or untracked path(s)" in err
    assert " M README.md" in err and "?? notes-00.md" in err and "--allow-dirty" in err
    assert not any(Path(c[0]).name == "fake-docker" for c in world.calls)


def test_a_long_refusal_says_how_many_paths_it_left_out(world, clients, tmp_path, capsys):
    root, _ = _checkout(world, tmp_path, dirty=25)
    assert _run(["--checkout", str(root)]) == 1
    assert "... and 5 more" in capsys.readouterr().err


def test_allow_dirty_deploys_and_stamps_the_image_dirty(world, clients, tmp_path, capsys):
    root, sha = _checkout(world, tmp_path, dirty=2)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune", "--allow-dirty"]) == 0
    _, env = _compose_call(world)
    assert env["PSEUDOLIFE_BUILD_GIT_SHA"] == sha and env["PSEUDOLIFE_BUILD_DIRTY"] == "true"
    assert "--allow-dirty: deploying anyway" in capsys.readouterr().err


def test_a_tree_git_cannot_describe_is_refused_with_gits_reason(world, clients, tmp_path, capsys):
    root, _ = _checkout(world, tmp_path, repo=False)
    assert _run(["--checkout", str(root), "--no-backup"]) == 1
    err = capsys.readouterr().err
    assert "cannot tell whether" in err and "not a git repository" in err
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune", "--allow-dirty"]) == 0
    _, env = _compose_call(world)
    assert env["PSEUDOLIFE_BUILD_GIT_SHA"] == "unknown" and env["PSEUDOLIFE_BUILD_DIRTY"] == "unknown"


def test_git_chatter_on_stderr_is_not_mistaken_for_dirty_paths(world, clients, tmp_path, monkeypatch):
    root, sha = _checkout(world, tmp_path)
    monkeypatch.setenv("GIT_TRACE", "1")
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    _, env = _compose_call(world)
    assert env["PSEUDOLIFE_BUILD_GIT_SHA"] == sha and env["PSEUDOLIFE_BUILD_DIRTY"] == "false"


def test_a_tree_that_changes_during_the_deploy_is_not_built(world, clients, tmp_path, capsys):
    root, _ = _checkout(world, tmp_path)
    world.on_image_inspect = lambda: (root / "late-edit.txt").write_text("late", encoding="utf-8")
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 1
    assert "changed during this deploy" in capsys.readouterr().err
    assert _compose_call(world) == (None, None)


def test_a_missing_compose_file_is_refused(world, clients, tmp_path, capsys):
    assert _run(["--checkout", str(tmp_path / "nowhere")]) == 2
    assert "must name a Pseudolife-MCP checkout" in capsys.readouterr().err


# ── the rollback tag guard (2026-08-13) ─────────────────────────────────────

def test_no_rollback_image_means_no_rollback_promise(world, clients, tmp_path, capsys):
    root, _ = _checkout(world, tmp_path)
    world.images.clear()
    world.container = None
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    out, err = capsys.readouterr()
    assert not any(c.startswith("tag ") for c in world.docker_calls())
    assert "NO rollback image" in err
    assert "docker tag" not in out and "nothing was tagged" in out and "Rebuild the last-good code" in out


def test_an_unhealthy_deploy_without_a_rollback_image_is_honest(world, clients, tmp_path, capsys):
    root, _ = _checkout(world, tmp_path)
    world.images.clear()
    world.container = None
    world.health = [None]
    assert _run(["--checkout", str(root), "--no-backup", "--health-retries", "2", "--health-delay-ms", "1"]) == 1
    err = capsys.readouterr().err
    assert "did not report healthy" in err and "docker tag" not in err and "nothing was tagged" in err


def test_the_rollback_tag_is_not_moved_onto_an_unvalidated_build(world, clients, tmp_path, capsys):
    root, _ = _checkout(world, tmp_path)
    world.images["pseudolife-daemon:0.1.0"] = "sha256:newbuild"     # the tag moved; the container runs lastgood
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    out, err = capsys.readouterr()
    assert not any(c.startswith("tag ") for c in world.docker_calls())
    assert "REFUSING to move the rollback tag" in err and "--force-rollback-tag" in err
    assert "NOT moved this run" in out and "docker image ls pseudolife-daemon" in out


def test_the_force_flag_moves_the_tag_anyway(world, clients, tmp_path, capsys):
    root, _ = _checkout(world, tmp_path)
    world.images["pseudolife-daemon:0.1.0"] = "sha256:newbuild"
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune", "--force-rollback-tag", "--rollback-tag", "x"]) == 0
    assert "tag pseudolife-daemon:0.1.0 pseudolife-daemon:0.1.0-x" in world.docker_calls()
    assert "--force-rollback-tag: tagged" in capsys.readouterr().err


def test_an_absent_daemon_container_still_tags(world, clients, tmp_path):
    root, _ = _checkout(world, tmp_path)
    world.container = None
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune", "--rollback-tag", "x"]) == 0
    assert "tag pseudolife-daemon:0.1.0 pseudolife-daemon:0.1.0-x" in world.docker_calls()


def test_an_unhealthy_deploy_never_prunes_the_cache_and_a_prune_failure_is_not_fatal(world, clients, tmp_path, monkeypatch):
    root, _ = _checkout(world, tmp_path)
    world.health = [None]
    assert _run(["--checkout", str(root), "--no-backup", "--health-retries", "1", "--health-delay-ms", "1"]) == 1
    assert not any(c.startswith("builder") for c in world.docker_calls())
    world.calls.clear()
    world.health = [{"status": "ok", "version": "0.1.0"}]
    original = world.docker

    def failing_builder(a):
        if a[:1] == ["builder"]:
            return 1, "error: cannot prune"
        return original(a)

    monkeypatch.setattr(world, "docker", failing_builder)
    assert _run(["--checkout", str(root), "--no-backup"]) == 0
    assert any(c.startswith("builder prune") for c in world.docker_calls())


# ── credentials never leak into the build ───────────────────────────────────

@pytest.mark.parametrize("file_keys,cleared", [
    ("PSEUDOLIFE_MCP_TOKEN=\nPSEUDOLIFE_MCP_TOKENS=fixture:replacement\n", True),
    ("PSEUDOLIFE_MCP_TOKEN=fixture-replacement\n", True),
    ("PSEUDOLIFE_MCP_TOKENS=fixture:replacement\n", True),
    ("# PSEUDOLIFE_MCP_TOKEN=ignored\n", False),
])
def test_the_env_files_own_authentication_is_never_shadowed_by_the_callers(world, clients, tmp_path, monkeypatch,
                                                                             file_keys, cleared):
    root, _ = _checkout(world, tmp_path)
    (root / "ops" / ".env").write_text(file_keys, encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-old")
    monkeypatch.setenv("pseudolife_mcp_tokens", "fixture:old-map")
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune", "--allow-dirty"]) == 0
    _, env = _compose_call(world)
    present = {k for k in env if k.upper() in ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS")}
    assert (present == set()) is cleared, present
    assert os.environ["PSEUDOLIFE_MCP_TOKEN"] == "fixture-old"     # the caller's environment is intact


def test_the_compose_image_tag_is_read_from_the_compose_file(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  pseudolife-daemon:\n    image: pseudolife-daemon:0.15.0\n", encoding="utf-8")
    assert up._compose_image_tag(compose) == "pseudolife-daemon:0.15.0"
    compose.write_text("services:\n", encoding="utf-8")
    assert up._compose_image_tag(compose) is None


def test_the_dump_marker_is_only_read_outside_copy_data(tmp_path):
    path = tmp_path / "d.sql.gz"
    path.write_bytes(gzip.compress(b"COPY public.entries (id) FROM stdin;\n-- PostgreSQL database dump complete\n\\.\n"))
    assert not up._dump_complete(path)
    path.write_bytes(gzip.compress(b"COPY x FROM stdin;\n\\.\n-- PostgreSQL database dump complete\n"))
    assert up._dump_complete(path)
    path.write_bytes(b"not gzip")
    assert not up._dump_complete(path)


def test_the_cli_mode_and_the_checkout_script_reach_the_same_code(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "pseudolife_memory.cli", "update", "--help"],
                          capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0 and "--checkout" in proc.stdout and "--check" in proc.stdout
    proc = subprocess.run([sys.executable, str(ROOT / "ops" / "update.py"), "--help"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "--all" in proc.stdout


# ── carried over from the shell tests: the checkout's own scripts ────────────

def _scripts(root: Path) -> dict:
    """The checkout's backup and retention scripts, as files the fake
    runner records (pwsh on Windows, bash elsewhere)."""
    suffix = ".ps1" if os.name == "nt" else ".sh"
    scripts = {}
    for stem in ("backup", "prune-rollbacks", "prune-build-cache"):
        path = root / "ops" / f"{stem}{suffix}"
        path.write_text("# fake\n", encoding="utf-8")
        scripts[stem] = path
    # committed: the clean-tree guard must see a clean checkout
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "scripts")
    return scripts


def _script_calls(world: World, script: Path) -> list[list[str]]:
    return [c for c in world.calls if str(script) in c]


def test_the_checkouts_scripts_run_with_their_mapped_flags(world, clients, tmp_path):
    root, _ = _checkout(world, tmp_path)
    scripts = _scripts(root)
    assert _run(["--checkout", str(root), "--keep-rollbacks", "5", "--keep-cache-hours", "24"]) == 0
    assert len(_script_calls(world, scripts["backup"])) == 1
    [prune] = _script_calls(world, scripts["prune-rollbacks"])
    flag, repo_flag = ("-Keep", "-Repository") if os.name == "nt" else ("--keep", "--repository")
    assert prune[-4:] == [flag, "5", repo_flag, "pseudolife-daemon"]
    [cache] = _script_calls(world, scripts["prune-build-cache"])
    assert cache[-2:] == ["-MaxAgeHours" if os.name == "nt" else "--max-age-hours", "24"]
    assert not any("pg_dump" in c for c in world.docker_calls())
    kinds = []
    for c in world.calls:
        if str(scripts["backup"]) in c:
            kinds.append("backup")
        elif c[1:2] == ["compose"]:
            kinds.append("compose")
        elif str(scripts["prune-build-cache"]) in c:
            kinds.append("prune-build-cache")
    assert kinds == ["backup", "compose", "prune-build-cache"]


def test_an_unhealthy_checkout_deploy_never_runs_the_checkouts_cache_prune(world, clients, tmp_path):
    root, _ = _checkout(world, tmp_path)
    scripts = _scripts(root)
    world.health = [None]
    assert _run(["--checkout", str(root), "--health-retries", "1", "--health-delay-ms", "1"]) == 1
    assert _script_calls(world, scripts["prune-build-cache"]) == []
    assert len(_script_calls(world, scripts["backup"])) == 1


def test_a_failed_backup_script_stops_the_deploy(world, clients, tmp_path, monkeypatch, capsys):
    root, _ = _checkout(world, tmp_path)
    scripts = _scripts(root)
    original = world.run_cli

    def failing(argv, **kw):
        if str(scripts["backup"]) in [str(a) for a in argv]:
            world.calls.append([str(a) for a in argv])
            return 1, "backup is INCOMPLETE"
        return original(argv, **kw)

    monkeypatch.setattr(up, "run_cli", failing)
    assert _run(["--checkout", str(root)]) == 1
    assert "backup failed" in capsys.readouterr().err
    assert _compose_call(world) == (None, None) and not any(c.startswith("tag") for c in world.docker_calls())


def test_credentials_are_scoped_even_when_compose_fails(world, clients, tmp_path, monkeypatch, capsys):
    root, _ = _checkout(world, tmp_path)
    (root / "ops" / ".env").write_text("PSEUDOLIFE_MCP_TOKEN=daemon-secret\n", encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-old")
    world.compose_rc = 1
    assert _run(["--checkout", str(root), "--no-backup", "--allow-dirty"]) == 1
    _, env = _compose_call(world)
    assert "PSEUDOLIFE_MCP_TOKEN" not in env and os.environ["PSEUDOLIFE_MCP_TOKEN"] == "fixture-old"
    assert "daemon rebuild failed" in capsys.readouterr().err


def test_a_release_deployed_daemon_is_the_rollback_of_the_next_checkout_deploy(world, clients, tmp_path, capsys):
    """After a `pseudolife-mcp update` the container runs a GHCR image while
    the compose file names the local tag: that image is the last-good one,
    tagged by id, not an unvalidated build to refuse."""
    root, _ = _checkout(world, tmp_path)
    world.container = {"id": "sha256:ghcr-running", "ref": f"{up.GHCR_IMAGE}:0.1.0", "labels": {}}
    world.images["pseudolife-daemon:0.1.0"] = "sha256:oldlocal"
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune", "--rollback-tag", "x"]) == 0
    assert "tag sha256:ghcr-running pseudolife-daemon:0.1.0-x" in world.docker_calls()
    out, err = capsys.readouterr()
    assert "REFUSING" not in err and "tagging it by id" in out


def _foreign_owner_env(sdir: Path) -> dict:
    empty = sdir / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    return {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty)}


def test_a_foreign_owned_checkout_names_the_git_fix(world, clients, tmp_path, monkeypatch, capsys):
    """git's safe.directory refusal is quoted, not read as a dirty tree."""
    root, _ = _checkout(world, tmp_path)
    owner = _foreign_owner_env(tmp_path)
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                           env={**os.environ, **owner})
    if probe.returncode == 0:
        pytest.skip("this git ignores GIT_TEST_ASSUME_DIFFERENT_OWNER, so a foreign owner cannot be faked")
    for key, value in owner.items():
        monkeypatch.setenv(key, value)
    assert _run(["--checkout", str(root), "--no-backup"]) == 1
    err = capsys.readouterr().err
    assert "cannot tell whether" in err and "safe.directory" in err


def test_the_builtin_backup_rotates_only_its_own_old_files(world, clients, tmp_path):
    """Files older than a week go, except the newest three of each kind
    (updates are usually further apart than a week: an age-only rule would
    leave one dump behind after each), and never a file this tool did not
    write."""
    _project(world, tmp_path)
    backups = tmp_path / "data" / "backups"
    backups.mkdir(parents=True)
    old_dumps = [backups / f"pseudolife_memory-2020010{i}-000000.sql.gz" for i in range(1, 5)]
    old_states = [backups / f"pseudolife_state-2020010{i}-000000.tgz" for i in range(1, 3)]
    foreign = backups / "pseudolife_manifest-20200101-000000.json"
    recent = backups / "pseudolife_memory-20990101-000000.sql.gz"
    for path in old_dumps + old_states + [foreign, recent]:
        path.write_bytes(b"x")
        if path is not recent:
            os.utime(path, (0, 0))
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    assert _run(["--health-delay-ms", "1"]) == 0
    # dumps: the run's own, the 2099 one and 2020-01-04 are the newest three
    assert [p.exists() for p in old_dumps] == [False, False, False, True]
    assert recent.exists() and foreign.exists()
    # states: the run's own plus the two old ones are three; nothing goes
    assert all(p.exists() for p in old_states)


# ── a client-only machine (2026-09-29 first-update findings) ───────────────

def _on_a_shim_runtime(monkeypatch):
    """This interpreter runs from a shim runtime (the real check: the
    runtimes root holds ``sys.prefix``)."""
    monkeypatch.setattr(up, "_on_shim_runtime", lambda: True)


def test_a_client_only_machine_updates_its_clients_to_the_daemons_release(world, clients, monkeypatch, capsys):
    """No daemon container here and the command runs from a shim runtime:
    the daemon is on another host, so `update` and `--clients-only` move
    this machine's clients to the daemon's release and never mention
    Docker (they used to refuse with "Start Docker and retry")."""
    _on_a_shim_runtime(monkeypatch)
    world.health = [{"status": "ok", "version": "0.15.1", "hooks_digest": "d" * 64}]
    for argv in ([], ["--clients-only"]):
        clients.clear()
        assert _run(argv + ["--daemon-url", "http://10.0.0.7:8765"]) == 0, argv
        assert clients == [{"steps": ("shim", "plugin", "codex"), "repo": None,
                            "source": "pseudolife-mcp==0.15.1", "daemon_digest": "d" * 64, "reinstall": False}]
        captured = capsys.readouterr()
        assert "docker" not in (captured.out + captured.err).lower()
    assert not any(c[1:4] == ["-m", "pip", "install"] for c in world.calls)


def test_a_client_only_machine_takes_the_newest_release_when_the_daemon_is_silent(world, clients, monkeypatch):
    _on_a_shim_runtime(monkeypatch)
    world.health = []
    world.pypi = "0.16.0"
    assert _run(["--clients-only"]) == 0
    assert clients[0]["source"] == "pseudolife-mcp==0.16.0" and clients[0]["daemon_digest"] is None


def test_a_client_only_machine_refuses_daemon_only_naming_the_daemon_host(world, clients, monkeypatch, capsys):
    _on_a_shim_runtime(monkeypatch)
    world.health = [{"status": "ok", "version": "0.15.1"}]
    assert _run(["--daemon-only", "--daemon-url", "http://10.0.0.7:8765"]) == 2
    err = capsys.readouterr().err
    assert "10.0.0.7" in err and "docker" not in err.lower()
    assert clients == []


def _runtime(world: World, *, version: str, source: str, commit: str | None) -> Path:
    """A complete shim runtime in the fixture's layout, as runtimes.install leaves it."""
    windows = os.name == "nt"
    path = world.tmp / "rt" / "000001"
    scripts = path / ("Scripts" if windows else "bin")
    scripts.mkdir(parents=True)
    (scripts / ("pseudolife-mcp.exe" if windows else "pseudolife-mcp")).write_text("console", encoding="utf-8")
    (path / "runtime.json").write_text(json.dumps({"version": version, "source": source,
                                                   "source_commit": commit}), encoding="utf-8")
    return path


def test_a_checkout_runtime_is_not_replaced_by_the_same_release(world, clients, tmp_path, capsys):
    """A checkout-built daemon reports the last release's version, so
    `--clients-only --tag <that version>` would install PyPI's older build
    as the newest runtime, the one the launcher starts, and shims through
    0.15.0 cannot read PSEUDOLIFE_MCP_TOKEN_FILE."""
    _project(world, tmp_path, version="0.15.0")
    _runtime(world, version="0.15.0", source=str(tmp_path / "checkout"), commit="f" * 40)
    assert _run(["--clients-only", "--tag", "0.15.0"]) == 2
    err = capsys.readouterr().err
    assert "checkout" in err and "ops/update.sh --all" in err and "--reinstall" in err
    assert clients == []
    assert _run(["--clients-only", "--tag", "0.15.0", "--reinstall"]) == 0
    assert clients[0]["source"] == "pseudolife-mcp==0.15.0" and clients[0]["reinstall"] is True


def test_a_release_runtime_or_a_newer_release_is_not_refused(world, clients, tmp_path):
    _project(world, tmp_path, version="0.15.0")
    _runtime(world, version="0.15.0", source="pseudolife-mcp==0.15.0", commit=None)
    assert _run(["--clients-only", "--tag", "0.15.0"]) == 0
    runtime_json = world.tmp / "rt" / "000001" / "runtime.json"
    runtime_json.write_text(json.dumps({"version": "0.15.0", "source": str(tmp_path / "checkout"),
                                        "source_commit": "f" * 40}), encoding="utf-8")
    world.health = [{"status": "ok", "version": "0.15.1"}]
    assert _run(["--clients-only", "--tag", "0.15.1"]) == 0
    assert [c["source"] for c in clients] == ["pseudolife-mcp==0.15.0", "pseudolife-mcp==0.15.1"]


def test_the_shim_runtime_check_reads_the_runtimes_root(world, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(Path(sys.prefix).parent))
    assert up._on_shim_runtime() is True
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(world.tmp / "elsewhere"))
    assert up._on_shim_runtime() is False


def test_a_client_only_machine_keeps_a_checkout_runtime_too(world, clients, monkeypatch, capsys):
    _on_a_shim_runtime(monkeypatch)
    _runtime(world, version="0.15.0", source="/src/checkout", commit="f" * 40)
    world.health = [{"status": "ok", "version": "0.15.0"}]
    assert _run(["--clients-only"]) == 2
    assert "checkout" in capsys.readouterr().err and clients == []


# ── the step lines and a streamed child interleave on a redirected stdout ────

_STREAM_ORDER_DRIVER = """
\"\"\"The update entry point with this file's fakes, in its own interpreter,
so stdout is the pipe the test reads: block-buffered, as a log file is.\"\"\"
import os
import sys
from pathlib import Path

root, tests, tmp, mode = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]
sys.path[:0] = [root, tests]
import test_update_cli as t  # noqa: E402
from pseudolife_memory import client_updates, update_cli as up  # noqa: E402

world = t.World(tmp)
real_run_cli = up.run_cli


def run_cli(argv, **kw):
    code, out = world.run_cli(argv, **kw)
    if kw.get("stream"):
        # A real child through the real run_cli, as docker is: it writes to
        # fd 1 directly, past this interpreter's stdout buffer.
        script = next((Path(a).stem for a in argv if str(a).endswith((".ps1", ".sh"))), None)
        marker = script or " ".join(str(a) for a in argv[1:3])
        real_run_cli([sys.executable, "-c", "import sys; print('<<child>> ' + sys.argv[1])", marker], stream=True)
        return code, ""
    return code, out


if mode == "builtin-prune-fails":
    fake_docker = world.docker
    world.docker = lambda a: (1, "error: prune failed") if a[:1] == ["builder"] else fake_docker(a)

up.run_cli = run_cli
up.fetch_json = world.fetch_json
up.sleep = lambda s: world.slept.append(s)
up.data_dir = lambda: tmp / "data"
up.which = lambda name: {"git": "git", "bash": "bash", "pwsh": "pwsh", "pipx": None}.get(name, f"/usr/bin/{name}")
client_updates.install_kind = lambda interpreter: ("site", "")
os.environ["PSEUDOLIFE_DOCKER"] = "fake-docker"
os.environ["PSEUDOLIFE_SHIM_RUNTIMES"] = str(tmp / "rt")
os.environ["PSEUDOLIFE_SHIM_LAUNCHER"] = str(tmp / "bin" / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp"))
for name in t._GIT_ENV + ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS"):
    os.environ.pop(name, None)

checkout, _ = t._checkout(world, tmp)
if mode == "scripts":
    t._scripts(checkout)
sys.exit(up.main(["--checkout", str(checkout), "--rollback-tag", "unittest", "--health-delay-ms", "1"]))
"""


@pytest.mark.parametrize("mode, markers", [
    # the checkout's own scripts stream: every step line, and the rollback
    # text, lands before the child that follows it
    ("scripts", ("==> backing up the bank", "<<child>> backup", "==> tagged rollback image",
                 "<<child>> prune-rollbacks", "==> rebuilding the daemon only", "==> build stamp",
                 "<<child>> compose", "==> waiting for the daemon", "==> healthy.",
                 "Rolled-back deploy if ever needed", "<<child>> prune-build-cache")),
    # no scripts: the builtin backup, and a failed builtin cache prune warns
    # on stderr after the rollback text went to stdout
    ("builtin-prune-fails", ("==> backing up the bank", "==> tagged rollback image", "==> rebuilding the daemon only",
                             "==> build stamp", "<<child>> compose", "==> waiting for the daemon", "==> healthy.",
                             "Rolled-back deploy if ever needed", "WARNING: build-cache retention failed")),
])
def test_step_lines_and_streamed_children_land_in_the_order_they_happened(tmp_path, mode, markers):
    """``ops/update.sh > log 2>&1`` on a headless host (2026-09-29) showed
    every step line after the whole docker build: the child wrote to the
    file directly while Python's block-buffered stdout held the steps
    until exit, so the log read as if the backup followed the build."""
    driver = tmp_path / "driver.py"
    driver.write_text(_STREAM_ORDER_DRIVER, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}   # the log-file case, not a tty
    proc = subprocess.run([sys.executable, str(driver), str(ROOT), str(ROOT / "tests"), str(tmp_path), mode],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300, env=env,
                          cwd=str(ROOT))
    assert proc.returncode == 0, proc.stdout
    lines = proc.stdout.splitlines()

    def first(marker: str) -> int:
        found = [i for i, line in enumerate(lines) if marker in line]
        assert found, f"{marker!r} missing from:\n" + "\n".join(lines)
        return found[0]

    order = [first(m) for m in markers]
    assert order == sorted(order), "\n".join(lines)


# ── contributor hosts: the test suite's own Postgres login ─────────────────

@pytest.fixture
def test_login(world, tmp_path, monkeypatch):
    """The test-login command, recorded not run, with its file in tmp_path
    and the bundled Postgres container local and running."""
    from pseudolife_memory import test_login_cli
    seen: list[list[str]] = []
    answer = {"exit": 0, "error": None,
              "changes": ["  role pseudolife_test: created: LOGIN CREATEDB, not superuser"]}

    def create(argv):
        seen.append(list(argv))
        return dict(answer)

    monkeypatch.setattr(up, "create_test_login", create)
    monkeypatch.setenv(test_login_cli.FILE_ENV, str(tmp_path / "test-pg.env"))
    # The suite says where it connects: the bundled server (an explicit
    # setting; the suite's bare default only asks, see below).
    monkeypatch.setenv("PSEUDOLIFE_TEST_PG_HOST_PORT", "127.0.0.1:5433")
    monkeypatch.setattr(up, "home", lambda: tmp_path / "home")      # no suite env file
    state = {"tty": False, "answer": "", "asked": []}
    monkeypatch.setattr(up, "interactive", lambda: state["tty"])
    monkeypatch.setattr(up, "ask", lambda q: state["asked"].append(q) or state["answer"])
    world.pg_running = "true"
    world.pg_published = "127.0.0.1:5433"
    return {"seen": seen, "answer": answer, "file": tmp_path / "test-pg.env", "term": state}


def _pg_aware(world: World) -> None:
    """``docker inspect`` of the Postgres container answers the table's state."""
    inner = world.docker

    def docker(a):
        if a[:2] == ["inspect", "-f"] and a[3] == PG:
            state = getattr(world, "pg_running", None)
            return (0, state + "\n") if state else (1, "Error: No such object")
        if a[:2] == ["port", PG]:
            published = getattr(world, "pg_published", None)
            return (0, published + "\n") if published else (1, "Error: no public port '5432/tcp'")
        return inner(a)

    world.docker = docker


def test_a_checkout_deploy_creates_the_missing_test_login(world, clients, test_login, tmp_path, capsys):
    _pg_aware(world)
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert test_login["seen"] == [["create"]]                 # never --rotate
    out = capsys.readouterr().out
    assert "role pseudolife_test: created" in out and "test login" in out
    compose = next(i for i, c in enumerate(world.calls) if c[1:2] == ["compose"])
    inspect = next(i for i, c in enumerate(world.calls) if c[-1] == PG)
    assert compose < inspect                                  # after the daemon is back


def test_a_present_test_login_file_is_left_alone(world, clients, test_login, tmp_path):
    _pg_aware(world)
    test_login["file"].write_text("PSEUDOLIFE_TEST_PG_USER=pseudolife_test\n", encoding="utf-8")
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert test_login["seen"] == []


def test_no_local_bundled_postgres_no_test_login(world, clients, test_login, tmp_path):
    _pg_aware(world)
    world.pg_running = None
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert test_login["seen"] == []
    world.pg_running = "false"
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert test_login["seen"] == []


def test_a_refused_test_login_warns_with_the_fix_and_the_deploy_stands(world, clients, test_login,
                                                                       tmp_path, capsys):
    _pg_aware(world)
    test_login["answer"].update(exit=4, changes=[], error=(
        "role pseudolife_test already exists and ~/.pseudolife-mcp/test-pg.env holds no "
        "password for it"))
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    fix = [line for line in (captured.out + captured.err).splitlines() if "--rotate" in line]
    assert len(fix) == 1 and "test-login create" in fix[0]


def test_no_test_login_skips_it(world, clients, test_login, tmp_path):
    _pg_aware(world)
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune", "--no-test-login"]) == 0
    assert test_login["seen"] == []


def test_a_release_update_never_creates_a_test_login(world, clients, test_login, tmp_path):
    """End users' bank servers get no CREATEDB password login (review,
    2026-10-04): only checkout mode, a contributor's host, makes one."""
    _pg_aware(world)
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    assert _run(["--health-delay-ms", "1"]) == 0
    assert _run(["--reinstall", "--health-delay-ms", "1"]) == 0
    assert test_login["seen"] == []


def test_no_test_login_is_a_checkout_option(world, clients, tmp_path, capsys):
    _project(world, tmp_path)
    assert _run(["--no-test-login"]) == 2
    assert "--no-test-login" in capsys.readouterr().err


def test_create_test_login_runs_the_command_and_reads_its_report(monkeypatch):
    from pseudolife_memory import test_login_cli

    def fake_main(argv, *, out=None, executor=None):
        assert argv == ["create", "--json"]
        print(json.dumps({"exit": 0, "error": None, "changes": ["x"]}), file=out)
        return 0

    monkeypatch.setattr(test_login_cli, "main", fake_main)
    assert up.create_test_login(["create"]) == {"exit": 0, "error": None, "changes": ["x"]}


# ── maintainer passkeys: a reminder, and an offer at a terminal ─────────────

@pytest.fixture
def passkeys(monkeypatch):
    from pseudolife_memory import maintainer_setup
    state = {"set_up": False, "ran": [], "tty": False, "answer": "n", "asked": []}
    monkeypatch.setattr(maintainer_setup, "passkeys_set_up", lambda: state["set_up"])
    monkeypatch.setattr(maintainer_setup, "main", lambda argv: state["ran"].append(argv) or 0)
    monkeypatch.setattr(up, "interactive", lambda: state["tty"])
    monkeypatch.delenv("PSEUDOLIFE_MCP_DAEMON_URL", raising=False)
    monkeypatch.setattr(up, "ask", lambda question: state["asked"].append(question) or state["answer"])
    return state


def _auth(world: World) -> None:
    for answer in world.health:
        if isinstance(answer, dict):
            answer["auth"] = True


def test_an_update_points_at_the_setup_until_passkeys_are_set_up(world, clients, passkeys, tmp_path, capsys):
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    _auth(world)
    assert _run(["--health-delay-ms", "1"]) == 0
    out = capsys.readouterr().out
    [line] = [line for line in out.splitlines() if "maintainer setup" in line]
    assert "pseudolife-mcp maintainer setup" in line
    assert passkeys["ran"] == []                               # no terminal: no offer
    passkeys["set_up"] = True
    assert _run(["--health-delay-ms", "1"]) == 0               # already current: still checked
    assert "maintainer setup" not in capsys.readouterr().out


def test_a_checkout_deploy_reminds_too(world, clients, passkeys, tmp_path, capsys):
    root, _ = _checkout(world, tmp_path)
    _auth(world)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert "pseudolife-mcp maintainer setup" in capsys.readouterr().out


def test_at_a_terminal_the_update_offers_to_run_it(world, clients, passkeys, tmp_path):
    _project(world, tmp_path, version="0.15.1")
    _auth(world)
    passkeys.update(tty=True, answer="y")
    assert _run([]) == 0
    assert passkeys["ran"] == [["--yes"]]                      # the one question answered for it
    assert "tailscale serve" in passkeys["asked"][0] and "restart" in passkeys["asked"][0]
    assert "tailnet" in passkeys["asked"][0] and ":8443" in passkeys["asked"][0]
    passkeys.update(answer="")
    assert _run([]) == 0
    assert passkeys["ran"] == [["--yes"]]                      # the default is no


def test_a_tokenless_or_unreadable_daemon_gets_no_reminder(world, clients, passkeys, tmp_path, capsys):
    _project(world, tmp_path, version="0.15.1")
    assert _run([]) == 0                                       # /health without "auth": true
    passkeys["set_up"] = None
    _auth(world)
    assert _run([]) == 0                                       # state unknown
    assert "maintainer setup" not in capsys.readouterr().out


def test_json_and_clients_only_runs_never_ask(world, clients, passkeys, tmp_path, capsys):
    _project(world, tmp_path, version="0.15.1")
    _auth(world)
    passkeys.update(tty=True, answer="y")
    assert _run(["--json"]) == 0
    assert _run(["--clients-only"]) == 0
    assert passkeys["ran"] == []


def test_the_passkey_offer_runs_after_the_update_lock_is_released(world, clients, passkeys, tmp_path, monkeypatch):
    """Review 2026-10-05: enrolment waits minutes for the Console; an
    unattended update must not find the lock held all that time."""
    _project(world, tmp_path, version="0.15.1")
    _auth(world)
    held = []
    from pseudolife_memory import maintainer_setup
    monkeypatch.setattr(maintainer_setup, "main", lambda argv: held.append(up.UpdateLock()._lock.acquire()) or 0)
    passkeys.update(tty=True, answer="y")
    assert _run([]) == 0
    assert held == [True]


def test_a_failing_passkey_check_never_fails_a_finished_update(world, clients, passkeys, tmp_path, monkeypatch, capsys):
    """Delegate review 2026-10-05: the deploy succeeded; a hint must not
    turn it into exit 1 with a traceback."""
    _project(world, tmp_path, version="0.15.1")
    _auth(world)
    from pseudolife_memory import maintainer_setup
    monkeypatch.setattr(maintainer_setup, "passkeys_set_up", lambda: {}["rp_id"])
    assert _run([]) == 0
    assert "maintainer setup" in capsys.readouterr().err


def test_a_failing_test_login_step_never_fails_a_finished_deploy(world, clients, test_login, tmp_path, monkeypatch, capsys):
    _pg_aware(world)
    root, _ = _checkout(world, tmp_path)
    scripts = _scripts(root)

    def boom(argv):
        raise OSError("disk full")
    monkeypatch.setattr(up, "create_test_login", boom)
    assert _run(["--checkout", str(root)]) == 0
    assert "disk full" in capsys.readouterr().err
    assert _script_calls(world, scripts["prune-build-cache"])        # the deploy still finished


def test_the_offer_runs_setup_against_the_updates_daemon_port(world, clients, passkeys, tmp_path):
    _project(world, tmp_path, version="0.15.1")
    _auth(world)
    passkeys.update(tty=True, answer="y")
    assert _run(["--daemon-url", "http://127.0.0.1:9876"]) == 0
    assert passkeys["ran"] == [["--yes", "--port", "9876"]]



# The maintainer's decision (2026-10-05): create the test login only where the
# suite actually connects to the bundled server; on the box the bundled
# Postgres holds the live bank and the suites use a separate server (5434).

def test_a_suite_pointed_at_another_server_gets_no_test_login(world, clients, test_login, tmp_path,
                                                               monkeypatch, capsys):
    _pg_aware(world)
    monkeypatch.setenv("PSEUDOLIFE_TEST_PG_HOST_PORT", "127.0.0.1:5434")
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert test_login["seen"] == []
    [line] = [l for l in capsys.readouterr().out.splitlines() if "test login" in l]
    assert "127.0.0.1:5434" in line and "5433" in line


def test_the_box_suite_env_file_counts_as_where_the_suite_connects(world, clients, test_login, tmp_path,
                                                                   monkeypatch):
    _pg_aware(world)
    monkeypatch.delenv("PSEUDOLIFE_TEST_PG_HOST_PORT", raising=False)
    env =tmp_path / "home" / ".config" / "pseudolife-suite" / "env"
    env.parent.mkdir(parents=True)
    env.write_text("# suite settings\nexport PSEUDOLIFE_TEST_PG_HOST_PORT='127.0.0.1:5434'\n"
                   "PSEUDOLIFE_TEST_PG_PASSWORD=x\n", encoding="utf-8")
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert test_login["seen"] == []


def test_a_bundled_postgres_the_suite_cannot_reach_gets_no_test_login(world, clients, test_login, tmp_path):
    _pg_aware(world)
    world.pg_published = None
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert test_login["seen"] == []


def test_the_suite_on_the_bundled_port_by_any_loopback_name_gets_it(world, clients, test_login, tmp_path,
                                                                     monkeypatch):
    _pg_aware(world)
    monkeypatch.setenv("PSEUDOLIFE_TEST_PG_HOST_PORT", "localhost:5433")
    world.pg_published = "0.0.0.0:5433\n[::]:5433"
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert test_login["seen"] == [["create"]]



# The box, measured by the delegate (2026-10-05): deploys run as root, which
# has no suite env file, so the suite's bare default (127.0.0.1:5433) matched
# the bundled container that holds the live bank. A default-only match is
# unconfirmed: no login without a person saying so (maintainer decision).

def test_a_root_deploy_without_the_suite_env_file_creates_no_test_login(world, clients, test_login,
                                                                        tmp_path, monkeypatch, capsys):
    _pg_aware(world)
    monkeypatch.delenv("PSEUDOLIFE_TEST_PG_HOST_PORT", raising=False)
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert test_login["seen"] == [] and test_login["term"]["asked"] == []
    [line] = [l for l in capsys.readouterr().out.splitlines() if "test login" in l]
    assert "pseudolife-mcp test-login create" in line and "PSEUDOLIFE_TEST_PG_HOST_PORT" in line


@pytest.mark.parametrize("answer,created", [("y", True), ("", False), ("n", False)])
def test_at_a_terminal_a_default_only_match_is_asked_default_no(world, clients, test_login, tmp_path,
                                                                 monkeypatch, answer, created):
    _pg_aware(world)
    monkeypatch.delenv("PSEUDOLIFE_TEST_PG_HOST_PORT", raising=False)
    test_login["term"].update(tty=True, answer=answer)
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    [question] = test_login["term"]["asked"]
    assert "[y/N]" in question and "bank" in question
    assert test_login["seen"] == ([["create"]] if created else [])


def test_an_explicit_suite_setting_decides_without_asking(world, clients, test_login, tmp_path):
    _pg_aware(world)
    test_login["term"].update(tty=True, answer="n")
    root, _ = _checkout(world, tmp_path)
    assert _run(["--checkout", str(root), "--no-backup", "--no-cache-prune"]) == 0
    assert test_login["seen"] == [["create"]] and test_login["term"]["asked"] == []
