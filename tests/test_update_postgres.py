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
    """The PostgreSQL container and what Compose does to it.

    Clusters live on the bank volume, keyed by data directory, so a container
    started with another PGDATA finds another (or no) cluster there, and the
    old directory keeps answering with the old identifier. Compose's config
    hash is keyed by the files it renders: the labelled files give
    ``previous_hash`` (the label's own value unless a test changes the files
    after the container was created), any other set the release's."""

    def __init__(self, world: World, ops: Path):
        self.world = world
        self.ops = ops
        self.exists = True
        self.env = ["PG_MAJOR=18", "PGDATA=/var/lib/postgresql/18/docker"]
        self.container = {"Id": "pg-old", "Image": "sha256:pg-old", "status": "running", "health": "healthy",
                          "env": self.env}
        self.clusters = {"/var/lib/postgresql/18/docker": IDENTITY}
        self.labels = {"com.docker.compose.project": "pseudolife-mcp",
                       "com.docker.compose.service": "pseudolife-pg",
                       "com.docker.compose.config-hash": "a" * 64,
                       "com.docker.compose.project.config_files": str(ops / "docker-compose.yml"),
                       "com.docker.compose.project.working_dir": str(ops),
                       "com.docker.compose.project.environment_file": str(ops / ".env")}
        self.mounts = [{"Type": "volume", "Name": "pseudolife-mcp-bank", "Destination": "/var/lib/postgresql"}]
        self.release_hash = "b" * 64
        self.previous_hash: str | None = None       # None: the label's value
        self.volume_name = "pseudolife-mcp-bank"
        # What each side's files render for the data mount, and PGDATA.
        self.release_volumes = [{"type": "volume", "source": "pseudolife_pgdata", "target": "/var/lib/postgresql"}]
        self.previous_volumes: list | None = None   # None: the same as the release's
        self.release_environment: dict = {}
        self.image_env = ["PG_MAJOR=18", "PGDATA=/var/lib/postgresql/18/docker"]
        self.owner = "999"
        self.pull_rc = 0
        self.on_pull = None
        # What a recreate starts: its environment and, for a test that moves
        # the bank, the identifier its data directory then holds.
        self.new = {"up_rc": 0, "status": "running", "health": "healthy", "env": None, "identity": None}
        self.restore = {"up_rc": 0, "status": "running", "health": "healthy", "env": None, "identity": None}
        self.calls: list[list[str]] = []
        self.counter = 0

    def _files(self, a: list[str]) -> list[str]:
        return [a[i + 1] for i, v in enumerate(a[:-1]) if v == "-f"]

    def _previous(self, a: list[str]) -> bool:
        labelled = [f for f in self.labels.get("com.docker.compose.project.config_files", "").split(",") if f]
        return self._files(a) == labelled

    def config(self, previous: bool = False) -> dict:
        volumes = self.previous_volumes if previous and self.previous_volumes is not None else self.release_volumes
        return {"name": "pseudolife-mcp",
                "services": {up.DAEMON_SERVICE: {"environment": {}},
                             "pseudolife-pg": {"image": RELEASE_IMAGE, "user": "999:999",
                                               "environment": {} if previous else dict(self.release_environment),
                                               "volumes": volumes}},
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
                                  "Config": {"Labels": self.labels, "Env": c["env"]}, "Mounts": self.mounts})
        if a[:2] == ["inspect", "-f"] and a[-1] == PG and "State.Status" in a[2]:
            return 0, f"{c['Id']}|{c['status']}|{c['health']}\n"
        if a[:2] == ["image", "inspect"] and a[-1] == RELEASE_IMAGE and "Config.Env" in a[3]:
            return 0, json.dumps(self.image_env) + "\n"
        if a[:2] == ["exec", PG]:
            if "stat" in a:
                return 0, self.owner + "\n"
            if "pg_controldata" in " ".join(a):
                identity = c.get("identity") or self.clusters.get(a[-1], "")
                return 0, identity + "\n" if identity else ""
            if "pg_isready" in a:
                return 0, ""
            return None         # the backup and disk probes: the base world's
        if a[:2] == ["logs", "--tail"]:
            return 0, "FATAL:  data directory has wrong ownership\n"
        if a[:1] == ["pull"] and a[-1] == RELEASE_IMAGE:
            if self.on_pull:
                self.on_pull()
            return self.pull_rc, "" if self.pull_rc == 0 else "Error response from daemon: Get https://ghcr.io: timeout"
        if a[:1] == ["compose"] and "--hash" in a:
            if self._previous(a):
                return 0, f"pseudolife-pg {self.previous_hash or self.labels['com.docker.compose.config-hash']}\n"
            return 0, f"pseudolife-pg {self.release_hash}\n"
        if a[:1] == ["compose"] and "config" in a:
            return 0, json.dumps(self.config(previous=self._previous(a)))
        if a[:1] == ["compose"] and a[-1] == "pseudolife-pg" and "up" in a:
            restoring = any("docker-compose.rollback-pg-" in v for v in a)
            outcome = self.restore if restoring else self.new
            if outcome["up_rc"]:
                return outcome["up_rc"], "Error response from daemon: container failed to start"
            self.counter += 1
            self.container = {"Id": f"pg-{'restored' if restoring else 'new'}-{self.counter}",
                              "Image": "sha256:pg-old" if restoring else "sha256:pg-new",
                              "status": outcome["status"], "health": outcome["health"],
                              "env": outcome["env"] or (self.env if restoring else self.image_env),
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


def test_an_out_of_line_postgres_is_pulled_before_the_backup_and_recreated_before_the_daemon(
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
    # Both pulls before the backup: nothing slow sits between the unattended
    # run's board re-check (after the backup) and the recreate. The daemon's
    # rollback tag comes first: every refusal it can raise says nothing was
    # deployed, so PostgreSQL must not have moved yet.
    assert daemon_pull < pg_pull < dump < daemon_tag < pg_tag < pg_up < daemon_up, calls
    release = _release_dir(tmp_path)
    assert calls[pg_up].startswith(f"compose -p pseudolife-mcp --project-directory {pg.ops} --env-file {pg.ops / '.env'} "
                                   f"-f {release / 'docker-compose.yml'} -f {release / 'docker-compose.ghcr.yml'} "
                                   f"-f {override} ")
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
    assert not any(_pg_up(c) or _daemon_up(c) or c.startswith("tag ") or "pg_dump" in c for c in calls)
    assert pg.container["Id"] == "pg-old"
    err = capsys.readouterr().err
    assert f"docker pull {RELEASE_IMAGE} failed" in err and "nothing was changed" in err and "ghcr.io" in err


@pytest.mark.parametrize("failure", ["unhealthy", "exited", "up", "other-cluster"])
def test_a_failed_recreate_restores_the_previous_container_and_fails_the_update(pg, world, clients, capsys, failure):
    if failure == "up":
        pg.new["up_rc"] = 1
    elif failure == "other-cluster":
        pg.new["identity"] = "9999"         # the same path on other data
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
    assert restore.endswith("up -d --no-deps --no-build --pull never --force-recreate pseudolife-pg")
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
    assert "up -d --no-deps --no-build --pull never --force-recreate pseudolife-pg" in err


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
    assert calls.index(f"pull {RELEASE_IMAGE}") < dump < _index(calls, _pg_up)
    assert not any(_daemon_up(c) or c.startswith(f"pull {up.GHCR_IMAGE}:") for c in calls)
    out = capsys.readouterr().out
    assert "the daemon already runs 0.15.1; nothing to do" in out
    assert "PostgreSQL is healthy on this release's definition" in out
    # Only PostgreSQL moved: its restore command is shown, not left in a file.
    assert "Rolled-back PostgreSQL if ever needed:" in out
    assert "up -d --no-deps --no-build --pull never --force-recreate pseudolife-pg" in out


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
def test_the_engine_release_path_plans_before_the_pull_and_applies_after_the_backup(monkeypatch, tmp_path,
                                                                                   refused):
    from pseudolife_memory import update_engine as eng
    events: list[str] = []
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "data")
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
        return "sha256:d", "ref"

    def prune(*args, **kwargs):
        raise Stop

    monkeypatch.setattr(update, "running_image", running_image)
    monkeypatch.setattr(update, "prune_rollbacks", prune)
    release = eng.ReleaseEngineUpdate(update, "0.23.1", "0.23.0", None, "python")

    def command(args, **kw):
        if args[:1] == ["pull"]:
            events.append("pull")
            return 0, ""
        if args[:2] == ["image", "inspect"]:
            return 1, ""            # no image under the rollback tag yet
        if args[:1] == ["tag"]:
            events.append("daemon tag")
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
    monkeypatch.setattr(update_postgres, "pull", lambda u, p: events.append(f"pull {p}"))
    monkeypatch.setattr(update_postgres, "apply", lambda u, p: events.append(f"apply {p}"))
    if refused:
        with pytest.raises(up.UpdateError, match="refused"):
            release.run(lambda: events.append("before_recreate"))
        assert events == ["plan"]
    else:
        with pytest.raises(Stop):
            release.run(lambda: events.append("before_recreate"))
        # Both pulls before the backup, so nothing slow sits between the board
        # re-check (before_recreate) and the PostgreSQL recreate; and the
        # daemon's rollback tag, whose refusals say nothing was deployed,
        # comes before PostgreSQL moves.
        assert events == ["plan", "pull", "pull the plan", "backup", "before_recreate", "daemon", "daemon tag",
                          "apply the plan"]


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


def test_no_backup_leaves_an_out_of_line_postgres_alone_with_the_fix(pg, world, clients, capsys):
    assert up.main(["--no-backup", "--health-delay-ms", "1"]) == 0
    calls = _calls(world)
    assert f"pull {RELEASE_IMAGE}" not in calls and not any(_pg_up(c) for c in calls)
    assert any(_daemon_up(c) for c in calls)
    out = capsys.readouterr().out
    assert "this run takes no backup (--no-backup)" in out and "without --no-backup" in out


def test_a_current_daemon_is_checked_and_restarted_once_after_postgres_moved(pg, world, clients, capsys):
    current = {"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64}
    # /health: the no-op read, then silent until the restart, then healthy.
    world.health = [current, None, None, None, current]
    restarts = []
    original = pg.docker

    def docker(a):
        if a[:1] == ["restart"]:
            restarts.append(a)
            world.health[:] = [current]
            return 0, a[-1]
        return original(a)

    pg.docker = docker
    assert up.main(["--health-retries", "2", "--health-delay-ms", "1"]) == 0
    assert restarts == [["restart", up.DAEMON_CONTAINER]]
    out = capsys.readouterr().out
    assert "restarting pseudolife-mcp-daemon (same container and image)..." in out
    assert "the daemon reports healthy on the recreated PostgreSQL container" in out


def test_a_current_daemon_that_never_recovers_fails_with_the_logs_to_read(pg, world, clients, capsys):
    current = {"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64}
    world.health = [current, None]
    original = pg.docker
    pg.docker = lambda a: (0, a[-1]) if a[:1] == ["restart"] else original(a)
    assert up.main(["--health-retries", "1", "--health-delay-ms", "1"]) == 1
    err = capsys.readouterr().err
    assert "PostgreSQL is in line with this release, but the daemon still does not report healthy after a restart" in err
    assert f"docker logs {up.DAEMON_CONTAINER}" in err
    # The way back to the previous PostgreSQL container is in the error.
    assert "to roll back:" in err
    assert "up -d --no-deps --no-build --pull never --force-recreate pseudolife-pg" in err


def test_a_daemon_that_vanished_during_the_backup_refuses_before_postgres_moves(pg, world, clients, monkeypatch,
                                                                              capsys):
    # The refusal says nothing was deployed, so it must come before the
    # PostgreSQL recreate, not after it.
    real = up.Update.running_image

    def running_image(self):
        if any("pg_dump" in c for c in _calls(world)):
            return None, None
        return real(self)

    monkeypatch.setattr(up.Update, "running_image", running_image)
    assert up.main(["--health-delay-ms", "1"]) == 1
    calls = _calls(world)
    assert not any(_pg_up(c) or c.startswith("tag sha256:pg-old") for c in calls), calls
    assert "nothing was deployed" in capsys.readouterr().err


# ── the cluster the new container serves (PGDATA) ──────────────────────────

OTHER_PGDATA = "/var/lib/postgresql/18/other"


def test_a_definition_that_sets_another_data_directory_is_refused_before_anything_changes(pg, world, clients,
                                                                                         capsys):
    # An override setting PGDATA inside the same volume: PostgreSQL would
    # initialise an empty cluster there while the old directory still holds
    # the old identifier.
    pg.release_environment = {"PGDATA": OTHER_PGDATA}
    assert up.main(["--health-delay-ms", "1"]) == 2
    calls = _calls(world)
    assert not any(c.startswith(("pull ", "tag ")) or "pg_dump" in c or " up " in c for c in calls), calls
    err = capsys.readouterr().err
    assert f"runs PostgreSQL on the data directory {OTHER_PGDATA} (PGDATA, set by its definition)" in err
    assert "/var/lib/postgresql/18/docker" in err and "Nothing was changed" in err


def test_an_image_whose_default_data_directory_differs_is_refused_after_the_pull_and_before_the_backup(
        pg, world, clients, capsys):
    pg.image_env = ["PG_MAJOR=18", f"PGDATA={OTHER_PGDATA}"]
    assert up.main(["--health-delay-ms", "1"]) == 2
    calls = _calls(world)
    assert f"pull {RELEASE_IMAGE}" in calls
    assert not any(c.startswith("tag ") or "pg_dump" in c or " up " in c for c in calls), calls
    err = capsys.readouterr().err
    assert f"(PGDATA, {RELEASE_IMAGE}'s default)" in err and "Nothing was changed" in err


def test_the_same_data_directory_spelled_by_the_definition_is_accepted(pg, world, clients):
    # Near miss: a definition that names the directory the cluster is in.
    pg.release_environment = {"PGDATA": "/var/lib/postgresql/18/docker"}
    assert up.main(["--health-delay-ms", "1"]) == 0
    assert pg.container["Id"].startswith("pg-new")


def test_a_new_container_on_another_data_directory_is_restored_not_declared_healthy(pg, world, clients, capsys):
    # Neither the definition nor the image says beforehand (the image's
    # environment is unreadable): the check after the recreate reads the new
    # container's own PGDATA, where an empty cluster was initialised, instead
    # of the old directory, which still holds the old identifier.
    pg.image_env = []
    pg.clusters[OTHER_PGDATA] = "7431999999999999999"
    pg.new["env"] = ["PG_MAJOR=18", f"PGDATA={OTHER_PGDATA}"]
    assert up.main(["--health-delay-ms", "1"]) == 1
    assert pg.container["Id"].startswith("pg-restored")
    assert not any(_daemon_up(c) for c in _calls(world))
    err = capsys.readouterr().err
    assert f"it runs PostgreSQL on {OTHER_PGDATA}, not /var/lib/postgresql/18/docker" in err
    assert "its previous container is restored and healthy" in err


def test_a_restore_on_another_data_directory_is_not_declared_healthy(pg, world, clients, capsys):
    # The restore is checked the same way: a restored container naming
    # another PGDATA is a failed restore, with the command that brings the
    # previous container back.
    pg.new.update(health="unhealthy")
    pg.clusters[OTHER_PGDATA] = "7431999999999999999"
    pg.restore["env"] = ["PG_MAJOR=18", f"PGDATA={OTHER_PGDATA}"]
    assert up.main(["--health-delay-ms", "1"]) == 1
    err = capsys.readouterr().err
    assert "restoring its previous container failed too" in err
    assert f"it runs PostgreSQL on {OTHER_PGDATA}" in err


# ── the definition a restore would apply ───────────────────────────────────

def test_files_that_no_longer_render_the_running_definition_leave_postgres_alone(pg, world, clients, capsys):
    # The clone's compose file was edited after PostgreSQL was created: a
    # restore from it would not bring back the container that was running.
    pg.previous_hash = "e" * 64
    assert up.main(["--health-delay-ms", "1"]) == 0
    calls = _calls(world)
    assert f"pull {RELEASE_IMAGE}" not in calls and not any(_pg_up(c) or c.startswith("tag sha256:pg-old")
                                                           for c in calls)
    assert any(_daemon_up(c) for c in calls)
    lines = [line for line in capsys.readouterr().out.splitlines() if "PostgreSQL container left as it is" in line]
    assert len(lines) == 1
    assert "no longer render the definition it was created with" in lines[0]
    assert f"-f {pg.ops / 'docker-compose.yml'} up -d --no-deps --no-build pseudolife-pg" in lines[0]


def test_unchanged_files_render_the_running_definition_and_the_update_proceeds(pg, world, clients):
    # Near miss: the labelled files hash to the container's own label.
    pg.previous_hash = pg.labels["com.docker.compose.config-hash"]
    assert up.main(["--health-delay-ms", "1"]) == 0
    calls = _calls(world)
    previous = [c for c in calls if "--hash" in c and "compose/0.15.1-" not in c.replace("\\", "/")]
    assert previous, calls
    assert pg.container["Id"].startswith("pg-new")


# ── bind-mounted banks ─────────────────────────────────────────────────────

# Docker Desktop reports a bind's source translated; Compose renders the host
# path. Each side's own spelling of one directory.
BANK_DIR = r"D:\stacks\pseudolife-bank"
TRANSLATED = "/run/desktop/mnt/host/d/stacks/pseudolife-bank"


def _bind(pg, running: str, previous: str, release: str) -> None:
    pg.mounts = [{"Type": "bind", "Source": running, "Destination": "/var/lib/postgresql"}]
    pg.previous_volumes = [{"type": "bind", "source": previous, "target": "/var/lib/postgresql", "bind": {}}]
    pg.release_volumes = [{"type": "bind", "source": release, "target": "/var/lib/postgresql", "bind": {}}]


def test_a_bind_mounted_bank_moving_to_another_directory_is_refused(pg, world, clients, capsys):
    _bind(pg, TRANSLATED, BANK_DIR, BANK_DIR + "-copy")
    assert up.main(["--health-delay-ms", "1"]) == 2
    calls = _calls(world)
    assert not any(c.startswith(("pull ", "tag ")) or "pg_dump" in c or " up " in c for c in calls), calls
    err = capsys.readouterr().err
    assert f"mounts bind {BANK_DIR}-copy at /var/lib/postgresql" in err
    assert "would start PostgreSQL on other data" in err and "Nothing was changed" in err


@pytest.mark.parametrize("running", [TRANSLATED, BANK_DIR])
def test_the_same_bind_directory_spelled_by_each_side_is_accepted(pg, world, clients, running):
    # Near miss: inspect's translated (or verbatim) source and Compose's host
    # path name one directory; the comparison is Compose against Compose.
    _bind(pg, running, BANK_DIR, BANK_DIR)
    assert up.main(["--health-delay-ms", "1"]) == 0
    assert pg.container["Id"].startswith("pg-new")


# ── unattended ──────────────────────────────────────────────────────────────

@pytest.fixture
def unattended(pg, world, monkeypatch):
    from pseudolife_memory import unattended_update as uu
    state = {"activity": [([], 0, "")], "notices": []}

    def activity(url, token):
        return state["activity"].pop(0) if len(state["activity"]) > 1 else state["activity"][0]

    monkeypatch.setattr(uu, "board_activity", activity)
    monkeypatch.setattr(uu, "bearer", lambda: "tok")
    monkeypatch.setattr(uu, "post_json", lambda url, token, body, timeout=10.0: (
        state["notices"].append(body["text"]) or {"recipients": 1}))
    knob = {"status": "ok", "version": "0.15.0", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64,
            "updates": {"unattended_daemon": True, "unattended_clients": False}}
    world.health = [knob, knob, {"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0,
                                 "hooks_digest": "d" * 64}]
    return state


def test_an_unattended_hold_off_after_the_backup_leaves_postgres_alone(unattended, world, clients):
    # Idle before the lock, a session active once the backup is done.
    unattended["activity"] = [([], 0, ""), ([{"agent_id": "a1", "label": "claude-code", "task": "x"}], 0, "")]
    assert up.main(["--unattended", "--health-delay-ms", "1"]) == 4
    calls = _calls(world)
    assert calls.index(f"pull {RELEASE_IMAGE}") < _index(calls, lambda c: "pg_dump" in c)
    assert not any(_pg_up(c) or _daemon_up(c) or c.startswith("tag ") for c in calls)
    assert "held off after the backup" in unattended["notices"][-1]


def test_an_unattended_session_that_starts_during_the_postgres_pull_holds_the_recreate_off(
        unattended, pg, world, clients):
    # The board re-check must be the last thing before PostgreSQL moves: a
    # session that starts while its image downloads keeps its services.
    def session_starts():
        unattended["activity"][:] = [([{"agent_id": "a1", "label": "claude-code", "task": "x"}], 0, "")]

    pg.on_pull = session_starts
    assert up.main(["--unattended", "--health-delay-ms", "1"]) == 4
    calls = _calls(world)
    assert not any(_pg_up(c) or _daemon_up(c) or c.startswith("tag ") for c in calls), calls
    assert pg.container["Id"] == "pg-old"
    assert "held off after the backup" in unattended["notices"][-1]


def test_an_unattended_postgres_failure_is_restored_and_told(unattended, pg, world, clients):
    pg.new.update(health="unhealthy")
    assert up.main(["--unattended", "--health-delay-ms", "1"]) == 1
    assert not any(_daemon_up(c) for c in _calls(world))
    assert pg.container["Id"].startswith("pg-restored")
    text = unattended["notices"][-1]
    assert text.startswith("Pseudolife-MCP: the unattended update to 0.15.1 FAILED")
    assert "its previous container is restored and healthy" in text


def test_an_unattended_update_brings_postgres_in_line(unattended, pg, world, clients):
    assert up.main(["--unattended", "--health-delay-ms", "1"]) == 0
    calls = _calls(world)
    assert _index(calls, lambda c: "pg_dump" in c) < _index(calls, _pg_up) < _index(calls, _daemon_up)
    assert pg.container["Id"].startswith("pg-new")


def test_release_files_take_the_packaged_files_places_so_an_override_keeps_its_precedence(tmp_path):
    # Compose lets a later file win. A stack created from base, GHCR overlay,
    # then its own override must keep that order, or the overlay would replace
    # an image the override pins.
    ops = tmp_path / "clone" / "ops"
    ops.mkdir(parents=True)
    (ops.parent / "pyproject.toml").write_text("", encoding="utf-8")
    base, overlay, own = (ops / "docker-compose.yml", ops / "docker-compose.ghcr.yml",
                          ops / "docker-compose.override.yml")
    release = [tmp_path / "release" / "docker-compose.yml", tmp_path / "release" / "docker-compose.ghcr.yml"]
    assert update_postgres._release_order([base, overlay, own], release) == [release[0], release[1], own]
    # No overlay yet (an installer's clone): it goes straight after the base.
    assert update_postgres._release_order([base, own], release) == [release[0], release[1], own]
    assert update_postgres._release_order([base], release) == release
    # A file of the same name outside a packaged directory is the stack's own.
    foreign = tmp_path / "elsewhere" / "docker-compose.ghcr.yml"
    assert update_postgres._release_order([base, foreign], release) == [release[0], release[1], foreign]


@pytest.mark.parametrize("image, expected", [
    ("ghcr.io/pseudogiant-xr/pseudolife-pg:18", "ghcr.io/pseudogiant-xr/pseudolife-pg:18-pre"),
    # A digest is not a tag: "repo@sha256:...-pre" is no reference Docker accepts.
    ("ghcr.io/pseudogiant-xr/pseudolife-pg@sha256:" + "a" * 64, "ghcr.io/pseudogiant-xr/pseudolife-pg:latest-pre"),
    ("ghcr.io/pseudogiant-xr/pseudolife-pg:18@sha256:" + "a" * 64, "ghcr.io/pseudogiant-xr/pseudolife-pg:18-pre"),
    # A registry's host:port is not a tag either.
    ("registry.example.com:5000/pg", "registry.example.com:5000/pg:latest-pre"),
    ("registry.example.com:5000/pg@sha256:" + "b" * 64, "registry.example.com:5000/pg:latest-pre"),
    ("pg", "pg:latest-pre"),
])
def test_the_rollback_tag_is_a_valid_reference_for_a_digest_pinned_image(image, expected):
    assert update_postgres._rollback_reference(image, "pre") == expected


def test_a_stack_file_named_like_the_rollback_overlay_is_kept(pg, world, clients):
    # Only this step's own overlay, in the updaters' directory, is left out. A
    # stack's override that happens to share the name carries its settings
    # into the new container.
    override = pg.ops / "docker-compose.rollback-pg-custom.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    pg.labels["com.docker.compose.project.config_files"] += f",{override}"
    assert up.main(["--health-delay-ms", "1"]) == 0
    calls = _calls(world)
    assert f"-f {override} " in calls[_index(calls, _pg_up)]


def test_the_json_report_carries_the_postgres_rollback_beside_the_daemons(pg, world, clients, capsys):
    # --json prints no rollback lines: the same details are in the report.
    assert up.main(["--json", "--health-delay-ms", "1"]) == 0
    rollback = json.loads(capsys.readouterr().out)["rollback"]
    assert rollback["state"] == "tagged" and rollback["tag"]         # the daemon's own, kept
    postgres = rollback["postgres"]
    assert postgres["tag"].startswith(f"{RELEASE_IMAGE}-pre-update-")
    assert postgres["restore"].endswith("up -d --no-deps --no-build --pull never --force-recreate pseudolife-pg")
    assert Path(postgres["overlay"]).is_file()


def test_the_json_report_carries_the_postgres_rollback_when_only_postgres_moves(pg, world, clients, capsys):
    world.health = [{"status": "ok", "version": "0.15.1", "schema": 49, "persist_errors": 0, "hooks_digest": "d" * 64}]
    assert up.main(["--json"]) == 0
    postgres = json.loads(capsys.readouterr().out)["rollback"]["postgres"]
    assert postgres["restore"].endswith("up -d --no-deps --no-build --pull never --force-recreate pseudolife-pg")
    assert Path(postgres["overlay"]).is_file()
