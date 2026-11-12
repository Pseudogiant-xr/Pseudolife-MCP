"""Release-mode ``pseudolife-mcp update`` brings the PostgreSQL container in
line with the release's Compose definition (pseudolife_memory/update_postgres.py).

The fake world of tests/test_update_cli.py stands in for Docker, PyPI and the
daemon; :class:`Postgres` adds the PostgreSQL container, Compose's config hash
for its service and what a recreate does, so every path runs without Docker.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pseudolife_memory import update_cli as up, update_postgres  # noqa: E402
from tests.test_update_cli import World, _project, clients, world  # noqa: E402,F401

PG = up.PG_CONTAINER
RELEASE_IMAGE = "ghcr.io/pseudogiant-xr/pseudolife-pg:18"
IDENTITY = "7431000000000000001"


class Postgres:
    """The PostgreSQL container and what Compose does to it."""

    def __init__(self, world: World, ops: Path):
        self.world = world
        self.ops = ops
        self.exists = True
        self.container = {"Id": "pg-old", "Image": "sha256:pg-old", "status": "running", "health": "healthy",
                          "identity": IDENTITY}
        self.labels = {"com.docker.compose.project": "pseudolife-mcp",
                       "com.docker.compose.service": "pseudolife-pg",
                       "com.docker.compose.config-hash": "a" * 64,
                       "com.docker.compose.project.config_files": str(ops / "docker-compose.yml"),
                       "com.docker.compose.project.working_dir": str(ops),
                       "com.docker.compose.project.environment_file": str(ops / ".env")}
        self.env = ["PG_MAJOR=18", "PGDATA=/var/lib/postgresql/18/docker"]
        self.mounts = [{"Type": "volume", "Name": "pseudolife-mcp-bank", "Destination": "/var/lib/postgresql"}]
        self.release_hash = "b" * 64
        self.volume_name = "pseudolife-mcp-bank"
        self.owner = "999"
        self.pull_rc = 0
        self.new = {"up_rc": 0, "status": "running", "health": "healthy", "identity": IDENTITY}
        self.restore = {"up_rc": 0, "status": "running", "health": "healthy", "identity": IDENTITY}
        self.calls: list[list[str]] = []
        self.counter = 0

    def config(self) -> dict:
        return {"name": "pseudolife-mcp",
                "services": {up.DAEMON_SERVICE: {"environment": {}},
                             "pseudolife-pg": {"image": RELEASE_IMAGE, "user": "999:999",
                                               "volumes": [{"type": "volume", "source": "pseudolife_pgdata",
                                                            "target": "/var/lib/postgresql"}]}},
                "volumes": {"pseudolife_pgdata": {"name": self.volume_name, "external": True}}}

    def docker(self, a: list[str]):
        c = self.container
        if a[:3] == ["inspect", "-f", "{{json .}}"] and a[-1] == PG:
            if not self.exists:
                return 1, "Error: No such object: " + PG
            # The base world's answer (the backup resolves its connection
            # from it) plus what a real inspect also carries.
            _code, base = self.world.docker(a)
            return 0, json.dumps({**json.loads(base), "Id": c["Id"], "Image": c["Image"],
                                  "Config": {"Labels": self.labels, "Env": self.env}, "Mounts": self.mounts})
        if a[:2] == ["inspect", "-f"] and a[-1] == PG and "State.Status" in a[2]:
            return 0, f"{c['Id']}|{c['status']}|{c['health']}\n"
        if a[:2] == ["exec", PG]:
            if "stat" in a:
                return 0, self.owner + "\n"
            if "pg_controldata" in " ".join(a):
                return 0, c["identity"] + "\n"
            if "pg_isready" in a:
                return 0, ""
            return None         # the backup and disk probes: the base world's
        if a[:2] == ["logs", "--tail"]:
            return 0, "FATAL:  data directory has wrong ownership\n"
        if a[:1] == ["pull"] and a[-1] == RELEASE_IMAGE:
            return self.pull_rc, "" if self.pull_rc == 0 else "Error response from daemon: Get https://ghcr.io: timeout"
        if a[:1] == ["compose"] and "--hash" in a:
            return 0, f"pseudolife-pg {self.release_hash}\n"
        if a[:1] == ["compose"] and "config" in a:
            return 0, json.dumps(self.config())
        if a[:1] == ["compose"] and a[-1] == "pseudolife-pg" and "up" in a:
            restoring = any("docker-compose.rollback-pg-" in v for v in a)
            outcome = self.restore if restoring else self.new
            if outcome["up_rc"]:
                return outcome["up_rc"], "Error response from daemon: container failed to start"
            self.counter += 1
            self.container = {"Id": f"pg-{'restored' if restoring else 'new'}-{self.counter}",
                              "Image": "sha256:pg-old" if restoring else "sha256:pg-new",
                              "status": outcome["status"], "health": outcome["health"],
                              "identity": outcome["identity"]}
            return 0, ""
        return None

    def run_cli(self, argv, **kw):
        argv = [str(v) for v in argv]
        if Path(argv[0]).name.lower() in ("docker", "fake-docker"):
            self.calls.append(argv[1:])
            answer = self.docker(argv[1:])
            if answer is not None:
                self.world.calls.append(argv)
                self.world.envs.append(kw.get("env"))
                return answer
        return self.world.run_cli(argv, **kw)


@pytest.fixture
def pg(world, tmp_path, monkeypatch):
    """An installer host: the daemon and PostgreSQL from <clone>/ops, whose
    files are the old release's; this updater's package is 0.15.1."""
    project = _project(world, tmp_path, checkout=True)
    ops = project / "ops"
    world.container["labels"]["com.docker.compose.project"] = "pseudolife-mcp"
    fake = Postgres(world, ops)
    monkeypatch.setattr(up, "run_cli", fake.run_cli)
    monkeypatch.setattr(update_postgres, "__version__", "0.15.1")
    world.health = [{"status": "ok", "version": "0.15.0", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64},
                    {"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64}]
    return fake


def _release_dir(tmp_path: Path) -> Path:
    found = list((tmp_path / "data" / "compose").glob("0.15.1-*"))
    assert len(found) == 1, found
    return found[0]


def _calls(world: World) -> list[str]:
    return world.docker_calls()


def _index(calls, predicate) -> int:
    return next(i for i, c in enumerate(calls) if predicate(c))


def _pg_up(c: str) -> bool:
    return c.startswith("compose ") and " up " in c and c.endswith(" pseudolife-pg")


def _daemon_up(c: str) -> bool:
    return c.startswith("compose ") and " up " in c and c.endswith(" pseudolife-daemon")


def _never_destructive(calls) -> None:
    for c in calls:
        words = c.split()
        assert "down" not in words and "-v" not in words and "--volumes" not in words, c
        assert not c.startswith(("volume rm", "rm ", "system prune", "volume prune")), c
        assert "chown" not in words, c


def test_an_out_of_line_postgres_is_pulled_and_recreated_after_the_backup_and_before_the_daemon(
        pg, world, clients, tmp_path, capsys):
    override = pg.ops / "docker-compose.override.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    pg.labels["com.docker.compose.project.config_files"] += f",{override}"
    assert up.main(["--health-delay-ms", "1"]) == 0
    calls = _calls(world)
    daemon_pull = calls.index(f"pull {up.GHCR_IMAGE}:0.15.1")
    dump = _index(calls, lambda c: "pg_dump" in c)
    pg_pull = calls.index(f"pull {RELEASE_IMAGE}")
    pg_tag = _index(calls, lambda c: c.startswith(f"tag sha256:pg-old {RELEASE_IMAGE}-pre-update-"))
    pg_up = _index(calls, _pg_up)
    daemon_tag = _index(calls, lambda c: c.startswith("tag sha256:running "))
    daemon_up = _index(calls, _daemon_up)
    assert daemon_pull < dump < pg_pull < pg_tag < pg_up < daemon_tag < daemon_up, calls
    release = _release_dir(tmp_path)
    assert calls[pg_up].startswith(f"compose -p pseudolife-mcp --project-directory {pg.ops} --env-file {pg.ops / '.env'} "
                                   f"-f {release / 'docker-compose.yml'} -f {override} "
                                   f"-f {release / 'docker-compose.ghcr.yml'} ")
    assert calls[pg_up].endswith("up -d --no-deps --no-build pseudolife-pg")
    # the release's own definition, not the clone's old file
    assert (release / "docker-compose.yml").read_bytes() == (up.BUNDLED_COMPOSE / "docker-compose.yml").read_bytes()
    assert pg.container["Id"].startswith("pg-new")
    _never_destructive(calls)
    overlay = next((tmp_path / "data" / "update-engine").glob("docker-compose.rollback-pg-*.json"))
    assert json.loads(overlay.read_text())["services"]["pseudolife-pg"]["image"].startswith(
        f"{RELEASE_IMAGE}-pre-update-")
    out = capsys.readouterr().out
    assert f"pulling {RELEASE_IMAGE} (this release's PostgreSQL image)..." in out
    assert f"recreating the PostgreSQL container on {RELEASE_IMAGE} as 999:999 (same bank volume; the daemon waits)..." in out
    assert "PostgreSQL is healthy on this release's definition (same cluster)" in out
    assert "(the extractor untouched; PostgreSQL was brought in line above)..." in out


def test_a_host_already_in_line_changes_nothing_and_says_so_once(pg, world, clients, capsys):
    pg.release_hash = pg.labels["com.docker.compose.config-hash"]
    assert up.main(["--health-delay-ms", "1"]) == 0
    calls = _calls(world)
    assert f"pull {RELEASE_IMAGE}" not in calls
    assert not any(_pg_up(c) or c.startswith("tag sha256:pg-old") for c in calls)
    assert any(_daemon_up(c) for c in calls)
    out = capsys.readouterr().out
    assert out.count("PostgreSQL container already matches this release's definition; nothing to change") == 1
    assert "(Postgres + extractor untouched)..." in out
    assert "pg_controldata" not in " ".join(calls)


def test_a_failed_postgres_pull_changes_nothing(pg, world, clients, capsys):
    pg.pull_rc = 1
    assert up.main(["--health-delay-ms", "1"]) == 1
    calls = _calls(world)
    assert not any(_pg_up(c) or _daemon_up(c) or c.startswith("tag ") for c in calls)
    assert pg.container["Id"] == "pg-old"
    err = capsys.readouterr().err
    assert f"docker pull {RELEASE_IMAGE} failed" in err and "nothing was changed" in err and "ghcr.io" in err


@pytest.mark.parametrize("failure", ["unhealthy", "exited", "up", "other-cluster"])
def test_a_failed_recreate_restores_the_previous_container_and_fails_the_update(pg, world, clients, capsys, failure):
    if failure == "up":
        pg.new["up_rc"] = 1
    elif failure == "other-cluster":
        pg.new["identity"] = "9999"
    else:
        pg.new.update(status="running" if failure == "unhealthy" else "exited",
                      health="unhealthy" if failure == "unhealthy" else "")
    assert up.main(["--health-delay-ms", "1"]) == 1
    calls = _calls(world)
    ups = [c for c in calls if _pg_up(c)]
    assert len(ups) == 2
    restore = ups[1]
    # the previous definition: its own files, plus the image it ran
    assert f"-f {pg.ops / 'docker-compose.yml'} -f " in restore and "docker-compose.rollback-pg-" in restore
    assert "compose/0.15.1-" not in restore.replace("\\", "/")
    assert restore.endswith("up -d --no-deps --no-build --force-recreate pseudolife-pg")
    assert not any(_daemon_up(c) for c in calls)        # the daemon was not changed
    assert pg.container["Id"].startswith("pg-restored")
    _never_destructive(calls)
    captured = capsys.readouterr()
    assert "restoring the previous one..." in captured.err
    assert "its previous container is restored and healthy, and the daemon was not changed" in captured.err


def test_a_failed_restore_says_postgres_is_down_and_how_to_bring_it_back(pg, world, clients, capsys):
    pg.new.update(health="unhealthy")
    pg.restore.update(up_rc=1)
    assert up.main(["--health-delay-ms", "1"]) == 1
    err = capsys.readouterr().err
    assert "restoring its previous container failed too" in err
    assert "The bank volume is unchanged and the backup above holds it" in err
    assert "up -d --no-deps --no-build --force-recreate pseudolife-pg" in err


@pytest.mark.parametrize("owner", ["0", "70"])
def test_a_data_directory_the_postgres_account_does_not_own_is_refused_before_anything_changes(
        pg, world, clients, capsys, owner):
    pg.owner = owner
    assert up.main(["--health-delay-ms", "1"]) == 2
    calls = _calls(world)
    assert not any(c.startswith(("pull ", "tag ")) or "pg_dump" in c or " up " in c for c in calls), calls
    err = capsys.readouterr().err
    assert f"is owned by uid {owner}, but this release runs PostgreSQL as uid 999" in err
    assert "Nothing was changed" in err and f"chown -R 999:999 /var/lib/postgresql/18/docker" in err


def test_a_definition_that_would_mount_another_volume_is_refused(pg, world, clients, capsys):
    pg.volume_name = "pseudolife-mcp-bank-typo"
    assert up.main(["--health-delay-ms", "1"]) == 2
    assert not any(c.startswith(("pull ", "tag ")) or " up " in c for c in _calls(world))
    err = capsys.readouterr().err
    assert "would start PostgreSQL on other data" in err and "PSEUDOLIFE_BANK_VOLUME" in err


def test_an_image_volume_under_the_bank_mount_is_not_mistaken_for_data(pg, world, clients):
    # Near miss for the mount guard: an anonymous image VOLUME elsewhere.
    pg.mounts.append({"Type": "volume", "Name": "f" * 64, "Destination": "/var/run/postgresql"})
    assert up.main(["--health-delay-ms", "1"]) == 0
    assert any(_pg_up(c) for c in _calls(world))


def test_no_postgres_container_is_an_external_database_left_alone(pg, world):
    # The whole update needs the container for its backup; the step alone
    # must still say why it changed nothing.
    pg.exists = False
    update = up.Update(up.Options())
    assert update_postgres.plan(update, "pseudolife-mcp", [], {}, "0.15.1") is None
    assert update.report.steps == [f"PostgreSQL container left as it is: there is no {PG} container here (an "
                                   "external database is not this update's to change)"]
    assert not any(c.startswith(("pull ", "tag ")) or " up " in c for c in _calls(world))


@pytest.mark.parametrize("case", ["no-compose", "foreign-project", "other-service", "stack-file", "files-gone"])
def test_a_postgres_container_this_update_does_not_own_is_left_alone_with_one_line(
        pg, world, clients, tmp_path, capsys, case):
    if case == "no-compose":
        pg.labels = {}
    elif case == "foreign-project":
        pg.labels["com.docker.compose.project"] = "pseudolife-stack"
    elif case == "other-service":
        pg.labels["com.docker.compose.service"] = "db"
    elif case == "stack-file":
        stack = tmp_path / "stack" / "docker-compose.yml"
        stack.parent.mkdir()
        stack.write_text("services: {}\n", encoding="utf-8")
        pg.labels["com.docker.compose.project.config_files"] = str(stack)
    else:
        pg.labels["com.docker.compose.project.config_files"] += f",{pg.ops / 'docker-compose.gone.yml'}"
    assert up.main(["--health-delay-ms", "1"]) == 0
    calls = _calls(world)
    assert f"pull {RELEASE_IMAGE}" not in calls and not any(_pg_up(c) for c in calls)
    assert any(_daemon_up(c) for c in calls)
    lines = [line for line in capsys.readouterr().out.splitlines() if "PostgreSQL container left as it is" in line]
    assert len(lines) == 1
    why = {"no-compose": "not created by Docker Compose",
           "foreign-project": "'pseudolife-stack'", "other-service": "'db'",
           "stack-file": "a stack file this update does not manage", "files-gone": "no longer on this host"}[case]
    assert why in lines[0]


def test_the_daemon_already_current_still_brings_postgres_in_line_after_a_backup(pg, world, clients, capsys):
    world.health = [{"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64}]
    assert up.main([]) == 0
    calls = _calls(world)
    dump = _index(calls, lambda c: "pg_dump" in c)
    assert dump < calls.index(f"pull {RELEASE_IMAGE}") < _index(calls, _pg_up)
    assert not any(_daemon_up(c) or c.startswith(f"pull {up.GHCR_IMAGE}:") for c in calls)
    out = capsys.readouterr().out
    assert "the daemon already runs 0.15.1; nothing to do" in out
    assert "PostgreSQL is healthy on this release's definition" in out


def test_the_daemon_already_current_and_postgres_in_line_takes_no_backup(pg, world, clients, capsys):
    world.health = [{"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64}]
    pg.release_hash = pg.labels["com.docker.compose.config-hash"]
    assert up.main([]) == 0
    calls = _calls(world)
    assert not any("pg_dump" in c or c.startswith(("pull ", "tag ")) or " up " in c for c in calls)
    assert "nothing to change" in capsys.readouterr().out


def test_an_updater_newer_than_the_target_leaves_postgres_alone(pg, world, clients, monkeypatch, capsys):
    monkeypatch.setattr(update_postgres, "__version__", "0.16.0")
    assert up.main(["--health-delay-ms", "1"]) == 0
    assert not any(_pg_up(c) for c in _calls(world))
    assert "this updater (0.16.0) is newer than 0.15.1" in capsys.readouterr().out


def test_a_daemon_failure_after_postgres_moved_names_both_rollbacks(pg, world, clients, capsys):
    world.health = [{"status": "ok", "version": "0.15.0", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64}]
    world.compose_rc = 1
    assert up.main(["--health-delay-ms", "1"]) == 1
    err = capsys.readouterr().err
    assert "daemon recreate failed" in err and "PSEUDOLIFE_IMAGE_TAG=0.15.0-pre-update-" in err
    assert "PostgreSQL's previous container, if it is wanted back too:" in err
    assert "docker-compose.rollback-pg-" in err


def test_a_rerun_after_the_recreate_is_quiet(pg, world, clients, capsys):
    assert up.main(["--health-delay-ms", "1"]) == 0
    # What the recreate left: its labels name the release's files and hash.
    release = _release_dir(Path(str(up.data_dir())).parent)
    pg.labels["com.docker.compose.project.config_files"] = ",".join(
        [str(release / "docker-compose.yml"), str(release / "docker-compose.ghcr.yml")])
    pg.labels["com.docker.compose.config-hash"] = pg.release_hash
    world.health = [{"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64}]
    capsys.readouterr()
    before = len(world.calls)
    assert up.main([]) == 0
    later = [" ".join(c[1:]) for c in world.calls[before:]]
    assert not any("pg_dump" in c or c.startswith(("pull ", "tag ")) or " up " in c for c in later)
    assert capsys.readouterr().out.count("nothing to change") == 1


@pytest.mark.parametrize("refused", [False, True])
def test_the_engine_release_path_plans_before_the_pull_and_applies_after_the_backup(monkeypatch, refused):
    from pseudolife_memory import update_engine as eng
    events: list[str] = []
    update = up.Update(up.Options(daemon_url="http://127.0.0.1:18765"), log=lambda s: None)
    monkeypatch.setattr(update, "compose_context", lambda **kw: {
        "args": ["-p", "pseudolife-mcp"], "env_file": [], "checkout": None, "project": "pseudolife-mcp",
        "rust_overlay": None, "files": [], "original_files": [], "image_pin_overlay_masked": False})
    monkeypatch.setattr(update, "verify_compose_authentication", lambda *a: None)
    monkeypatch.setattr(update, "daemon_health", lambda *a, **k: {"status": "ok", "schema": 58})
    monkeypatch.setattr(update, "preflight_disk", lambda *a, **k: None)

    class Stop(Exception):
        pass

    def running_image():
        events.append("daemon")
        raise Stop

    monkeypatch.setattr(update, "running_image", running_image)
    release = eng.ReleaseEngineUpdate(update, "0.23.1", "0.23.0", None, "python")

    def command(args, **kw):
        if args[:1] == ["pull"]:
            events.append("pull")
            return 0, ""
        if "config" in args:
            return 0, json.dumps(_engine_config())
        return 0, json.dumps([{"Id": "daemon", "Image": "sha256:d", "Config": {"Labels": {}}}])

    monkeypatch.setattr(release, "command", command)
    monkeypatch.setattr(release, "verify_project", lambda config, running: ("t", "t"))
    monkeypatch.setattr(release, "probe_schema", lambda image: 58)
    monkeypatch.setattr(release, "backup", lambda checkout: events.append("backup"))
    monkeypatch.setattr(eng, "authenticated_read", lambda url, token: True)

    def plan(u, project, env_files, environment, target):
        events.append("plan")
        if refused:
            raise up.UpdateError("refused; Nothing was changed", 2)
        return "the plan"

    monkeypatch.setattr(update_postgres, "plan", plan)
    monkeypatch.setattr(update_postgres, "apply", lambda u, p: events.append(f"apply {p}"))
    if refused:
        with pytest.raises(up.UpdateError, match="refused"):
            release.run(lambda: events.append("before_recreate"))
        assert events == ["plan"]
    else:
        with pytest.raises(Stop):
            release.run(lambda: events.append("before_recreate"))
        assert events == ["plan", "pull", "backup", "before_recreate", "apply the plan", "daemon"]


def _engine_config() -> dict:
    return {"name": "pseudolife-mcp", "services": {up.DAEMON_SERVICE: {
        "environment": {"PSEUDOLIFE_MCP_TOKEN": "t"},
        "volumes": [{"type": "volume", "source": "state", "target": "/data"}],
        "ports": [{"target": 8765, "published": "18765", "host_ip": "127.0.0.1"}]}},
        "volumes": {"state": {"name": "fixture-state"}}}


def test_restoring_reads_the_files_the_previous_container_was_created_from(pg, world, clients, tmp_path,
                                                                            monkeypatch, capsys):
    # A rebuilt package of the same version with another PostgreSQL
    # definition must not rewrite the files the running container came from.
    assert up.main(["--health-delay-ms", "1"]) == 0
    first = _release_dir(tmp_path)
    created_from = (first / "docker-compose.yml").read_bytes()
    pg.labels["com.docker.compose.project.config_files"] = ",".join(
        [str(first / "docker-compose.yml"), str(first / "docker-compose.ghcr.yml")])
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in update_postgres.PACKAGED_FILES:
        (bundle / name).write_bytes((up.BUNDLED_COMPOSE / name).read_bytes())
    with (bundle / "docker-compose.yml").open("a", encoding="utf-8") as handle:
        handle.write("# a changed definition\n")
    monkeypatch.setattr(up, "BUNDLED_COMPOSE", bundle)
    monkeypatch.setattr(up, "_stamp", lambda: "20990101-000000")    # a later run's rollback tag
    pg.labels["com.docker.compose.config-hash"] = "c" * 64
    pg.new.update(health="unhealthy")
    world.health = [{"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64}]
    before = len(world.calls)
    assert up.main([]) == 1
    later = [" ".join(c[1:]) for c in world.calls[before:]]
    ups = [c for c in later if _pg_up(c)]
    assert len(ups) == 2 and f"-f {first / 'docker-compose.yml'} " in ups[1]
    assert f"-f {first / 'docker-compose.yml'} " not in ups[0]
    assert (first / "docker-compose.yml").read_bytes() == created_from
    assert "its previous container is restored and healthy" in capsys.readouterr().err
