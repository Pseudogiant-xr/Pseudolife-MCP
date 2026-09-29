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
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(tmp_path / "rt"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_LAUNCHER", str(tmp_path / "bin" / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")))
    return w


@pytest.fixture
def clients(monkeypatch):
    """The client side, recorded not run."""
    seen: list[dict] = []

    def run_steps(steps, *, repo, source, daemon_digest=None):
        seen.append({"steps": tuple(steps), "repo": repo, "source": source, "daemon_digest": daemon_digest})
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
                        "daemon_digest": "e" * 64}]
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
                        "daemon_digest": "d" * 64}]
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


def test_a_pip_upgrade_never_lands_on_a_shim_runtime_or_an_editable_checkout(world, monkeypatch, capsys):
    world.pypi = "99.0.0"
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(Path(sys.prefix).parent))
    assert _run([]) == 2
    assert "shim runtime" in capsys.readouterr().err
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(world.tmp / "elsewhere"))
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
    assert clients == [{"steps": ("shim", "plugin", "codex"), "repo": root, "source": str(root), "daemon_digest": None}]


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
