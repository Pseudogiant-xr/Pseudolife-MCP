"""Provision an owned [lite] PostgreSQL instance for the bounded Phase 1 judge."""
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import sys
import time
import uuid

from .harness import write_new


@contextmanager
def postgres():
    from evals.rust_baseline.daemon import private_directory
    from pseudolife_memory.storage import embedded_pg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    with private_directory() as private:
        previous_name = embedded_pg._DB_NAME
        try:
            # The existing provider owns startup/locking/major-version checks.
            # A disposable fixture name prevents creating a production-named
            # empty bank on this newly owned test server.
            embedded_pg._DB_NAME = "plbench_phase1_bootstrap"
            dsn, instance = embedded_pg.attach_or_start(Path(private))
        finally:
            embedded_pg._DB_NAME = previous_name
        if instance is None:
            raise RuntimeError("fresh private PostgreSQL unexpectedly attached to an existing instance")
        params = conninfo_to_dict(dsn)
        params["dbname"] = "postgres"
        admin = make_conninfo(**params)
        params["dbname"] = "plbench_phase1_tests_" + uuid.uuid4().hex[:12]
        test_url = make_conninfo(**params)
        overrides = {"PSEUDOLIFE_BENCH_ADMIN_URL": admin,
                     "PSEUDOLIFE_TEST_DATABASE_URL": test_url,
                     "PSEUDOLIFE_REQUIRE_TEST_POSTGRES": "1"}
        previous = {name: os.environ.get(name) for name in overrides}
        os.environ.update(overrides)
        cleanup = {"owned": True, "stopped": False}
        try:
            yield cleanup
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            instance.stop()
            for _ in range(40):
                with socket.socket() as probe:
                    probe.settimeout(.1)
                    closed = probe.connect_ex((params.get("host", "127.0.0.1"), int(params["port"]))) != 0
                if closed:
                    cleanup["stopped"] = True
                    break
                time.sleep(.05)
            if not cleanup["stopped"]:
                raise RuntimeError("owned embedded PostgreSQL listener survived cleanup")


def main():
    from .phase1 import main as judge
    # Forward the judge's public arguments unchanged. This extra receipt is
    # written after the owned PostgreSQL instance has actually stopped.
    if "--out" not in sys.argv:
        raise SystemExit("--out is required")
    output = Path(sys.argv[sys.argv.index("--out") + 1])
    cleanup = None
    try:
        with postgres() as cleanup:
            result = judge()
    finally:
        if cleanup is not None:
            write_new(output.with_suffix(".postgres.json"), cleanup)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
