"""Native update's read-only and no-op leaves, with isolated Docker and HTTP.

Transaction fault acceptance is in test_update_transactions.py: its Python
and native arms assert the same ordered mutation trace and recovery state.
The release-archive/schema probes extend the oracle's release preparation;
they do not weaken its backup, rollback, health or client gates.
"""
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from ..core import Case, WINDOWS


class Health:
    def __init__(self, port=0):
        self.seen = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fixture.seen.append({"method": "GET", "target": self.path, "body": "", "headers": list(self.headers.items())})
                body = b'{"status":"ok","version":"0.21.0","schema":58,"auth":false}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def requests(self):
        return list(self.seen)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def listener():
    port = [0]

    def create():
        daemon = Health(port[0])
        port[0] = daemon.server.server_port
        return daemon

    return create


def setup(arm):
    from ..test_update_transactions import _program
    path = arm.home / "bin/docker"
    path.parent.mkdir()
    _program(path, 'import sys\nprint("{}" if any("Labels" in a for a in sys.argv) else "fixture-daemon")\n')


def after(arm, observation):
    # The generated launcher contains this test interpreter's absolute path;
    # it is fixture infrastructure, removed before public snapshots. Assert
    # the CLI has not replaced or deleted it before removing our own fixture.
    assert (arm.home / "bin/docker.py").is_file()
    shutil.rmtree(arm.home / "bin")


def cases():
    docker = "{HOME}/bin/docker.exe" if WINDOWS else "{HOME}/bin/docker"
    env = {"PSEUDOLIFE_DOCKER": docker, "PSEUDOLIFE_MCP_DATA_DIR": "{HOME}/data"}
    return [
        Case("update-halves-exclude", ["update", "--clients-only", "--daemon-only"]),
        Case("update-check-current", ["update", "--check", "--tag", "0.21.0"], env=env,
             setup=setup, after=after, daemon=listener(), expect_output=True),
        Case("update-check-newer", ["update", "--check", "--tag", "0.21.1"], env=env,
             setup=setup, after=after, daemon=listener(), expect_output=True),
        Case("update-current-noop", ["update", "--daemon-only", "--tag", "0.21.0"], env=env,
             setup=setup, after=after, daemon=listener(), expect_output=True, golden=False,
             note="OS update lock holder state is observed live; transaction faults run in the dedicated acceptance file"),
    ]
