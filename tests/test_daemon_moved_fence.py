"""A daemon whose bank has moved refuses to start (``pseudolife-mcp move``).

``move`` stops the source daemon, fences its database (``ALLOW_CONNECTIONS
false``) and writes ``/data/moved.json`` into the stopped container. The
fence is what stops the old host; the file is the readable reason: a daemon
new enough to know it exits at startup naming the new location, instead of
failing later on a refused database connection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from pseudolife_memory import daemon
from tests.helpers import free_port


def test_no_marker_means_no_refusal(tmp_path):
    assert daemon.moved_refusal({"PSEUDOLIFE_MCP_DATA_DIR": str(tmp_path)}) is None
    assert daemon.moved_refusal({}) is None


def test_the_marker_names_the_new_location(tmp_path):
    (tmp_path / "moved.json").write_text(json.dumps({
        "moved_to": "http://100.64.0.7:8765", "move_id": "20261002-120000-0a1b2c3d",
        "moved_at": "2026-10-02T12:00:00Z"}), encoding="utf-8")
    message = daemon.moved_refusal({"PSEUDOLIFE_MCP_DATA_DIR": str(tmp_path)})
    assert message is not None
    assert "http://100.64.0.7:8765" in message
    assert "moved.json" in message


def test_an_unreadable_marker_still_refuses(tmp_path):
    """Presence is the fence: a damaged file must not let the old daemon
    start against a bank that now lives elsewhere."""
    (tmp_path / "moved.json").write_text("{not json", encoding="utf-8")
    message = daemon.moved_refusal({"PSEUDOLIFE_MCP_DATA_DIR": str(tmp_path)})
    assert message is not None and "moved.json" in message


def test_serve_exits_nonzero_before_touching_storage(tmp_path):
    """The real entry point: ``serve`` with the marker in its data dir exits
    with a clear message and a nonzero code, before storage resolution (the
    DSN here points at a port nothing listens on, so reaching storage would
    hang on retries or fail with a different message)."""
    (tmp_path / "moved.json").write_text(json.dumps({"moved_to": "http://100.64.0.7:8765"}),
                                         encoding="utf-8")
    env = {**os.environ, "PSEUDOLIFE_MCP_DATA_DIR": str(tmp_path),
           "PSEUDOLIFE_MCP_PORT": str(free_port()),
           "PSEUDOLIFE_MCP_DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none"}
    proc = subprocess.run([sys.executable, "-m", "pseudolife_memory.cli", "serve"], env=env,
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode != 0, proc.stderr
    assert "http://100.64.0.7:8765" in proc.stderr, proc.stderr
    assert "storage" not in proc.stderr.lower(), proc.stderr


def test_an_unfinished_move_into_this_bank_refuses_too(tmp_path):
    """``move`` leaves ``/data/move.json`` on the target from the restore
    until just before its own start: a target that starts outside the move
    (a reboot, an unattended update) must not serve a half-moved bank while
    the source may still run."""
    (tmp_path / "move.json").write_text(json.dumps({"move_id": "20261002-120000-0a1b2c3d"}),
                                        encoding="utf-8")
    message = daemon.moved_refusal({"PSEUDOLIFE_MCP_DATA_DIR": str(tmp_path)})
    assert message is not None
    assert "20261002-120000-0a1b2c3d" in message and "move.json" in message


def test_the_unfinished_move_refusal_says_how_to_finish_or_abandon_it(tmp_path):
    """The bank under the gate is a clone of the source's: abandoning the
    move means restoring the target's own pre-move safety dump (which
    replaces /data, gate included); a plain rm is only for finishing the move
    deliberately, and it has to work on a stopped container."""
    (tmp_path / "move.json").write_text("{}", encoding="utf-8")
    message = daemon.moved_refusal({"PSEUDOLIFE_MCP_DATA_DIR": str(tmp_path)})
    assert "docker run --rm --volumes-from pseudolife-mcp-daemon <image> rm -f /data/move.json" in message
    assert "clone" in message
    assert "restore.sh --apply" in message and "--state-archive" in message
