"""Owned native HTTP fixture and an external ASGI-to-real-HTTP adapter."""
from contextlib import contextmanager
import json
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import urllib.request
import urllib.error

from evals.rust_baseline.daemon import free_port
from evals.rust_port.harness import isolated_env, HttpClient
from evals.rust_port.processes import owned_process


@contextmanager
def native_sent(prefix, private, dsn, *, configuration="{}", token=None, tokens=None):
    """No oracle implementation or serializer crosses into this process."""
    private = Path(private)
    private.mkdir(parents=True, exist_ok=True)
    config = private / "config.yaml"
    config.write_text(configuration, encoding="utf-8")
    nonce, port = secrets.token_hex(32), free_port()
    env = isolated_env(private / "home")
    env.update(PSEUDOLIFE_MCP_DATABASE_URL=dsn, PSEUDOLIFE_MCP_CONFIG=str(config),
               PSEUDOLIFE_MCP_HOST="127.0.0.1", PSEUDOLIFE_MCP_PORT=str(port),
               PSEUDOLIFE_BASELINE_NONCE=nonce)
    if token is not None:
        env["PSEUDOLIFE_MCP_TOKEN"] = token
    if tokens is not None:
        env["PSEUDOLIFE_MCP_TOKENS"] = tokens
    with owned_process([*prefix, "serve"], cwd=Path.cwd(), env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE) as process:
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        try:
            ready = json.loads(lines.get(timeout=20))
        except (ValueError, queue.Empty):
            raise RuntimeError("native sent affirmative readiness missing") from None
        if (not isinstance(ready, dict) or ready.get("candidate") != "rust-maintainer-sent"
                or ready.get("ready") is not True or ready.get("nonce") != nonce
                or ready.get("port") != port or not process.owns_runtime_pid(ready.get("pid"))):
            raise RuntimeError("native sent readiness identity mismatch")
        process.sent_readiness = {"candidate": ready["candidate"], "ready": True,
                                  "pid": ready["pid"], "port": port,
                                  "nonce_verified": True, "owned_pid_verified": True}
        yield HttpClient(f"http://127.0.0.1:{port}"), process
    if process.owned_cleanup != {"process_stopped": True, "subtree_stopped": True}:
        raise RuntimeError("native sent process cleanup failed")


def asgi_http_adapter(client):
    """Forward the original ASGI request to the candidate-owned TCP listener."""
    async def app(scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/api/maintainer/sent":
            raise ValueError("native sent adapter received an ineligible boundary")
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                raise ValueError("native sent adapter expected an HTTP request")
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        query = scope.get("query_string", b"").decode("ascii")
        url = client.base_url + scope["path"] + ("?" + query if query else "")
        headers = {key.decode("latin-1"): value.decode("latin-1")
                   for key, value in scope.get("headers", [])}
        request = urllib.request.Request(url, data=bytes(body) if body else None,
                                         headers=headers, method=scope["method"])
        try:
            response = client.opener.open(request, timeout=client.timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            observed = [(key.lower().encode("ascii"), value.encode("latin-1"))
                        for key, value in response.headers.items()]
            await send({"type": "http.response.start", "status": response.status,
                        "headers": observed})
            await send({"type": "http.response.body", "body": raw})
    return app
