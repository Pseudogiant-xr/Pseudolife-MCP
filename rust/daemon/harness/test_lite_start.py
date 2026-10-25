"""The lite tier end to end with the real daemon binary: provision the
pinned PostgreSQL runtime, start on the per-user default data dir with no
DSN, answer /health from the embedded bank, attach on a second start, and
stop the server the daemon started when it exits after a startup failure.

A network test, run by hand on each platform's dev machine (it downloads
the runtime once into a scratch home): set PL_LITE_DAEMON_BIN to a built
`pseudolife-daemon`."""
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

import run

BIN = os.environ.get("PL_LITE_DAEMON_BIN")
pytestmark = pytest.mark.skipif(not BIN, reason="set PL_LITE_DAEMON_BIN to a built daemon")


def lite_env(base: Path) -> dict[str, str]:
    """A scratch per-user home: the runtime and the default data dir land
    under it, never under the caller's."""
    env = run.daemons.base_env(base / "home", {
        "LOCALAPPDATA": str(base / "local"),
        "XDG_DATA_HOME": str(base / "xdg"),
    })
    for name in ("PSEUDOLIFE_MCP_DATA_DIR", "PSEUDOLIFE_MCP_DATABASE_URL",
                 "PSEUDOLIFE_MCP_STORAGE"):
        env.pop(name, None)
    return env


def data_dir(base: Path) -> Path:
    if os.name == "nt":
        return base / "local" / "pseudolife-mcp"
    if os.uname().sysname == "Darwin":
        return base / "home" / "Library" / "Application Support" / "pseudolife-mcp"
    return base / "xdg" / "pseudolife-mcp"


def health(port: int) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return json.loads(error.read())


def wait_db_ok(daemon, port: int, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        assert daemon.proc.poll() is None, daemon.log.read_text(errors="replace")[-3000:]
        try:
            last = health(port)
            if last.get("db") == "ok":
                return last
        except OSError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"no db ok within {timeout}s: {last}\n"
                         + daemon.log.read_text(errors="replace")[-3000:])


def stop_server(base: Path) -> None:
    """Leave no server behind (a hard-killed daemon cannot stop its own)."""
    pgdata = data_dir(base) / "embedded_pg"
    if not (pgdata / "postmaster.pid").exists():
        return
    root = data_dir(base) / "native" / "postgresql"
    pg_ctl = next(root.glob("18.1.0-*/bin/pg_ctl*"))
    subprocess.run([str(pg_ctl), "stop", "-D", str(pgdata), "-m", "fast", "-w"],
                   capture_output=True, timeout=120)


def test_lite_provisions_starts_attaches_and_stops(tmp_path):
    env = lite_env(tmp_path)
    provision = subprocess.run([BIN, "provision", "postgresql"], env=env,
                               capture_output=True, timeout=900)
    assert provision.returncode == 0, provision.stderr.decode(errors="replace")
    assert b"installed the PostgreSQL 18.1.0 runtime" in provision.stdout
    again = subprocess.run([BIN, "provision", "postgresql"], env=env,
                           capture_output=True, timeout=120)
    assert again.returncode == 0
    assert b"already installed" in again.stdout

    port = run.daemons.free_port()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    first = run.daemons.rust_daemon(Path(BIN), home, port, env).launch()
    try:
        reply = wait_db_ok(first, port)
        assert reply["storage"] == "postgres"
        log = first.log.read_text(errors="replace")
        assert f"storage: embedded postgres (lite tier) — data dir {data_dir(tmp_path)}" in log
        assert (data_dir(tmp_path) / "embedded_pg" / "PG_VERSION").read_text().strip() == "18"
        assert (data_dir(tmp_path) / "embedded_pg.secret").is_file()
    finally:
        first.stop()
    if os.name != "nt":
        # SIGTERM: the daemon stops the server it started on its way out.
        assert not (data_dir(tmp_path) / "embedded_pg" / "postmaster.pid").exists()

    # A second start attaches to a running server, or starts it again.
    second = run.daemons.rust_daemon(Path(BIN), home, port, env).launch()
    try:
        wait_db_ok(second, port)
        assert "installing" not in second.log.read_text(errors="replace")
    finally:
        second.stop()
        stop_server(tmp_path)

    # A startup failure after the server started (an unreadable config)
    # still stops it: fail() stops the server this process started.
    (data_dir(tmp_path) / "config.yaml").write_text("memory: [unclosed\n", encoding="utf-8")
    failed = subprocess.run([BIN], env=dict(env, PSEUDOLIFE_MCP_PORT=str(port)),
                            capture_output=True, timeout=300)
    assert failed.returncode == 1, failed.stderr.decode(errors="replace")
    assert b"storage: embedded postgres (lite tier)" in failed.stderr
    assert not (data_dir(tmp_path) / "embedded_pg" / "postmaster.pid").exists()
    shutil.rmtree(tmp_path, ignore_errors=True)
