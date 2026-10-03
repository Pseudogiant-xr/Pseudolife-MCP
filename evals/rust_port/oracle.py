"""Disposable real Python HTTP/MCP handlers with a synthetic service seam."""
import argparse
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.rust_port.fixtures import FixtureService
from evals.rust_port.provenance import ROOT, require_historical_source, require_import_root, runtime_metadata


def run_fixture(sock):
    """Let the production daemon own transport composition above the service seam."""
    from pseudolife_memory import daemon, service, web
    import uvicorn

    fixture = FixtureService()
    fixture.config.memory.dream = SimpleNamespace(enabled=False)
    fixture.config.memory.retrieval_log = SimpleNamespace(enabled=False)
    fixture.config.updates = SimpleNamespace(check_releases=False)
    # The real entry point starts durability workers; this in-memory service
    # has neither a model nor persistent state for those workers to operate on.
    fixture.warmup = lambda: None
    fixture.autosave_if_changed = lambda: None
    fixture.reap_idle_sessions = lambda *args: None
    console_builder = web.build_console_app

    def build_fixture_console(mcp_app, token, health, selected_service, **kwargs):
        if selected_service is not fixture:
            raise RuntimeError("production daemon did not use the synthetic service")
        return console_builder(mcp_app, token, lambda: {"status": "ok", "fixture": True},
                               selected_service, **kwargs)

    def serve_owned_socket(app, **kwargs):
        uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)).run(sockets=[sock])

    with patch.object(service, "MemoryService", lambda **kwargs: fixture), \
            patch.object(daemon, "_build_health_payload", lambda *args: {"storage": "fixture"}), \
            patch.object(web, "build_console_app", build_fixture_console), \
            patch.object(uvicorn, "run", serve_owned_socket):
        daemon.run_daemon(host="127.0.0.1", port=sock.getsockname()[1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--oracle-root", type=Path, default=ROOT)
    args = parser.parse_args()
    require_historical_source(args.oracle_root.resolve())
    require_import_root(args.oracle_root.resolve())
    # Reserve the origin before importing handlers: accidental collision fails
    # this owned process, rather than probing an unrelated local service.
    sock = socket.socket()
    sock.bind(("127.0.0.1", args.port))
    print(json.dumps({"port": sock.getsockname()[1],
                      "actual_child_runtime": runtime_metadata(args.oracle_root)}), flush=True)
    run_fixture(sock)


if __name__ == "__main__":
    main()
