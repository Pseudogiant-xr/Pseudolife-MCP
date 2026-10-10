"""Forked children must refuse inherited names and fixture caches."""

import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable on Windows")
@pytest.mark.parametrize("seeded", [False, True], ids=["after-import", "after-seeding"])
def test_fork_refuses_inherited_harness_state(seeded):
    # A separate interpreter keeps the real fork away from pytest's threads.
    script = r'''
import atexit
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from rust.cli_harness import core
from rust.cli_harness.rows import _bank, _daemon, audit, backup, invite, maintainer, transfer
from rust.daemon.harness import pgdisposable as pg
from tests import test_transfer_cli

# All database effects are mocked; do not clean up a synthetic seeded bank.
for cleanup in (backup._drop_source, invite._drop_all, maintainer._drop_all,
                audit._drop_all, _daemon._shutdown_all):
    atexit.unregister(cleanup)
seeded = __SEEDED__
cli_name = backup.DB
daemon_name = pg.name(pg.PREFIX + "fork")
seed_calls = []

class Connection:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def commit(self):
        assert seed_calls == ["seed"]

def no_database(*args, **kwargs):
    raise AssertionError("fork accessed PostgreSQL")

with patch.object(_bank, "_login", return_value=("pseudolife_test", "fixture")), \
     patch.object(pg, "_login", return_value=("pseudolife_test", "fixture")):
    parent_dsn = _bank.url(cli_name)
    if seeded:
        # Exercise the fixture's seeding and cached-return path without a server.
        with patch.object(_bank, "create"), \
             patch.object(_bank, "connect", return_value=Connection()), \
             patch.object(_bank, "dump", return_value={"seeded": True}), \
             patch.object(test_transfer_cli, "_seed_bank", side_effect=lambda conn: seed_calls.append("seed")):
            assert backup._source() == parent_dsn
            invite.ensure("noschema")
            maintainer.ensure("notables")
            audit.ensure("empty")
        assert backup._SEEDED and backup._BASELINE == {"seeded": True}
        transfer._ARCHIVES["current"] = b"parent archive"
        audit._ARCHIVES["rich"] = b"parent archive"

    labels = dict(_bank.NAMES.labels)
    # A cached Case can carry a DSN without calling a row accessor again.
    case = core.Case("fork", [], env={"PSEUDOLIFE_MCP_DATABASE_URL": parent_dsn})
    factory = _daemon.shared("fork")
    _daemon._POOL["fork_python"] = SimpleNamespace(settle=lambda: None, shutdown=lambda: None)
    daemon = _daemon.RealDaemon.__new__(_daemon.RealDaemon)
    daemon.proc = SimpleNamespace(poll=lambda: None, terminate=no_database)
    with patch.object(_bank, "create", side_effect=no_database), \
         patch.object(_bank, "_connect", side_effect=no_database), \
         patch.object(pg.psycopg, "connect", side_effect=no_database):
        child = os.fork()
        if child == 0:
            failures = []
            checks = {
                "allocate": lambda: _bank.name("pl_cf_child"),
                "owns": lambda: _bank.NAMES.owns(cli_name),
                "cleanup": lambda: _bank.drop(cli_name),
                "normalize": lambda: _bank.NAMES.normalize(cli_name),
                "url": lambda: _bank.url(cli_name),
                "admin": _bank._admin,
                "backup": backup._source,
                "transfer": transfer._sources,
                "invite": lambda: invite.ensure("noschema"),
                "maintainer": lambda: maintainer.ensure("notables"),
                "audit": lambda: audit.export_bytes("rich"),
                "daemon cache": lambda: factory("python"),
                "daemon settle": daemon.settle,
                "daemon shutdown": daemon.shutdown,
                "cached case": lambda: core.run_arm(case, None, None),
                "daemon name": lambda: pg.name(pg.PREFIX + "child"),
                "daemon dsn": lambda: pg.dsn(daemon_name),
                "daemon cleanup": lambda: pg.drop(daemon_name),
                "daemon existing": pg.existing,
            }
            for key, operation in checks.items():
                try:
                    operation()
                except RuntimeError as exc:
                    if "fork" not in str(exc) or "process" not in str(exc):
                        failures.append(key + ": unclear error")
                except BaseException as exc:
                    failures.append(key + ": " + type(exc).__name__)
                else:
                    failures.append(key + ": returned parent state or accepted inherited allocator")
            print(json.dumps(failures), flush=True)
            # Never run the parent's atexit cleanup in the child.
            os._exit(bool(failures))
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    assert _bank.url(cli_name) == parent_dsn
    assert _bank.NAMES.labels == labels
    assert pg.NAMES.owns(daemon_name)
    backup._CREATED = False
    _daemon._POOL.clear()
'''
    result = subprocess.run([sys.executable, "-c", script.replace("__SEEDED__", repr(seeded))],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
