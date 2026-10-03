"""Disposable real Python HTTP/MCP handlers with a synthetic service seam."""
import argparse
import json
import socket
from pathlib import Path

from evals.rust_port.fixtures import FixtureService
from evals.rust_port.provenance import ROOT, require_historical_source, require_import_root, runtime_metadata


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
    from pseudolife_memory import mcp_server
    from pseudolife_memory.web.api import build_console_app
    import uvicorn

    service = FixtureService()
    mcp_server.service = service
    mcp_server.apply_transport_security(False)
    app = build_console_app(mcp_server.build_streamable_http_app(), None,
                            lambda: {"status": "ok", "fixture": True}, service)
    uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)).run(sockets=[sock])


if __name__ == "__main__":
    main()
