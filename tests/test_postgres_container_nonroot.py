"""The PostgreSQL container runs nothing as root.

Compose starts it as the image's postgres account (999:999) with every
capability dropped, so the upstream entrypoint skips its root bootstrap; the
healthcheck calls the real pg_isready, not Debian's Perl pg_wrapper (which
reads system account files on every probe); the image carries no gosu
binary; and the tooling that copies files into the container still works when
the container user cannot read or remove a root-owned file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HEALTHCHECK = "exec /usr/lib/postgresql/$$PG_MAJOR/bin/pg_isready -U pseudolife -d pseudolife_memory"
COMPOSE_FILES = ["ops/docker-compose.yml", "pseudolife_memory/compose/docker-compose.yml",
                 "ops/docker-compose.stack.yml"]


def _pg(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))["services"]["pseudolife-pg"]


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_postgres_service_runs_as_postgres_with_no_capabilities(path):
    service = _pg(path)
    assert str(service.get("user")) == "999:999"
    assert service.get("cap_drop") == ["ALL"]
    assert "cap_add" not in service
    assert "no-new-privileges:true" in service.get("security_opt", [])


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_postgres_healthcheck_bypasses_the_perl_wrapper(path):
    # "$$" is Compose's escape: the container shell expands $PG_MAJOR, which
    # the image sets, so a major-version bump needs no edit here.
    assert _pg(path)["healthcheck"]["test"] == ["CMD-SHELL", HEALTHCHECK]


def test_mirror_postgres_healthcheck_bypasses_the_perl_wrapper():
    text = (ROOT / "ops/docker-compose.rust-mirror.yml").read_text(encoding="utf-8")
    test = yaml.safe_load(text)["services"]["mirror-pg"]["healthcheck"]["test"]
    assert test[1].startswith("exec /usr/lib/postgresql/$$PG_MAJOR/bin/pg_isready ")


def test_image_replaces_gosu_with_a_setpriv_shim_and_prefers_real_clients():
    dockerfile = (ROOT / "ops/Dockerfile.pg").read_text(encoding="utf-8")
    assert "rm -f /usr/local/bin/gosu" in dockerfile
    assert "exec setpriv --reuid=" in dockerfile
    # The build itself proves the shim drops to the postgres account.
    assert "gosu postgres id -u | grep -qx 999" in dockerfile
    assert re.search(r"^ENV PATH=/usr/lib/postgresql/\$\{PG_MAJOR\}/bin:\$PATH$", dockerfile, re.M)


def test_restore_sh_streams_the_backup_in_as_the_container_user():
    script = (ROOT / "ops/restore.sh").read_text(encoding="utf-8")
    assert 'docker cp "$BACKUP_FILE"' not in script
    assert re.search(r'docker exec -i "\$CONTAINER" sh -c \'umask 077 && cat > "\$1"\' _ "\$tmp" < "\$BACKUP_FILE"',
                     script)


def test_restore_ps1_stages_in_a_container_user_directory_and_checks_it_can_read():
    script = (ROOT / "ops/restore.ps1").read_text(encoding="utf-8")
    stage = script.index("docker exec $Container mkdir -m 700 $stage")
    copy = script.index('docker cp $BackupFile "${Container}:$tmp"')
    readable = script.index("docker exec $Container test -r $tmp")
    destructive = script.index("DROP DATABASE")
    assert stage < copy < readable < destructive
    assert "docker exec $Container rm -rf -- $stage" in script
    assert "rm -f $tmp" not in script
