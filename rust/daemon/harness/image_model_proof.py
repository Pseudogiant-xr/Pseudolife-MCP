"""Prove an installed model with network-disabled disposable containers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time


class Proof:
    def __init__(self, image: str, postgres_image: str, models: Path, scratch: Path, sudo: bool):
        self.image, self.postgres_image = image, postgres_image
        self.models, self.scratch = models.resolve(), scratch
        self.command = ["sudo", "-n", "docker"] if sudo else ["docker"]
        self.nonce = secrets.token_hex(8)
        self.token = secrets.token_hex(32)
        self.database = "pl_cf_image_" + self.nonce
        self.containers: list[str] = []
        self.volume: str | None = None
        self.envfile = scratch / "daemon.env"
        self.socket = scratch / "socket"
        self.socket.mkdir(mode=0o777)
        self.socket.chmod(0o777)

    def docker(self, *args, body: bytes | None = None, check: bool = True):
        try:
            result = subprocess.run([*self.command, *map(str, args)], input=body,
                                    capture_output=True, timeout=240)
        except (OSError, subprocess.TimeoutExpired):
            # Exceptions carry argv, which can contain the disposable token.
            raise RuntimeError(f"docker {args[0]} could not complete") from None
        if check and result.returncode:
            # Docker output may include the disposable login or environment.
            raise RuntimeError(f"docker {args[0]} failed (exit {result.returncode})")
        return result

    def container(self, *args) -> str:
        # Record ownership before launch: Docker can create it then time out.
        cid = f"image-proof-{self.nonce}-{len(self.containers)}"
        self.containers.append(cid)
        self.docker("run", "-d", "--name", cid, "--network", "none", *args)
        return cid

    def remove(self, cid: str) -> None:
        self.docker("rm", "-f", cid)
        self.containers.remove(cid)

    def request(self, cid: str, method: str, path: str, body=None, session=None, auth=True):
        args = ["exec", "-i", cid, "curl", "--silent", "--show-error", "--max-time", "180",
                "-D", "-", "-X", method, "-H", "Content-Type: application/json",
                "-H", "Accept: application/json, text/event-stream"]
        if auth:
            args += ["-H", "Authorization: Bearer " + self.token]
        if session:
            args += ["-H", "Mcp-Session-Id: " + session,
                     "-H", "MCP-Protocol-Version: 2025-11-25"]
        if body is not None:
            args += ["--data-binary", "@-"]
        args += ["http://127.0.0.1:8765" + path]
        response = self.docker(*args, body=None if body is None else json.dumps(body).encode())
        headers, _, raw = response.stdout.partition(b"\r\n\r\n")
        status = int(headers.split()[1])
        fields = dict(line.split(": ", 1) for line in headers.decode().split("\r\n")[1:]
                      if ": " in line)
        if raw.startswith((b"event:", b"data:")):
            messages = [json.loads(line[6:]) for line in raw.decode().splitlines()
                        if line.startswith("data: ")]
            value = messages[-1] if messages else None
        else:
            value = json.loads(raw) if raw else None
        return status, {key.lower(): value for key, value in fields.items()}, value

    def session(self, cid: str) -> str:
        code, headers, _ = self.request(cid, "POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "image-model-proof", "version": "1"}}})
        if code != 200:
            raise RuntimeError(f"MCP initialization returned {code}")
        session = headers["mcp-session-id"]
        code, _, _ = self.request(cid, "POST", "/mcp", {
            "jsonrpc": "2.0", "method": "notifications/initialized"}, session=session)
        if code not in (200, 202, 204):
            raise RuntimeError(f"MCP initialized notification returned {code}")
        return session

    def tool(self, cid: str, session: str, name: str, arguments: dict):
        code, _, body = self.request(cid, "POST", "/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}, session=session)
        if code != 200 or "error" in body or body["result"].get("isError"):
            raise RuntimeError(f"{name} did not complete successfully (HTTP {code})")
        return body["result"]

    def start_daemon(self) -> str:
        cid = self.container("--env-file", self.envfile,
            "--mount", f"type=bind,src={self.models},dst=/models,readonly",
            "--mount", f"type=volume,src={self.volume},dst=/data",
            "--mount", f"type=bind,src={self.socket},dst=/run/postgresql,readonly", self.image)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.docker("exec", cid, "curl", "--silent", "--fail", "--max-time", "5",
                           "http://127.0.0.1:8765/health", check=False).returncode == 0:
                return cid
            if json.loads(self.docker("inspect", cid).stdout)[0]["State"]["Status"] == "exited":
                raise RuntimeError("offline daemon exited before becoming healthy")
            time.sleep(1)
        raise RuntimeError("offline daemon health timed out")

    def run(self) -> dict:
        import psycopg
        from pseudolife_memory.storage.schema import ensure_schema

        pg = self.container("--tmpfs", "/var/lib/postgresql",
            # The image entrypoint chowns /var/run/postgresql. Use a dedicated
            # socket directory so the caller retains ownership for cleanup.
            "--mount", f"type=bind,src={self.socket},dst=/proof-socket",
            "-e", "POSTGRES_DB=" + self.database, "-e", "POSTGRES_USER=imageproof",
            "-e", "POSTGRES_PASSWORD=" + self.token, self.postgres_image,
            # Keep the default socket too: the entrypoint's initialization
            # queries explicitly clear PGHOST and use that default path.
            "postgres", "-c", "unix_socket_directories=/var/run/postgresql,/proof-socket")
        for _ in range(60):
            # The entrypoint also starts a temporary server; wait until it
            # execs the final server before creating the proof's schema.
            ready = self.docker("exec", pg, "cat", "/proc/1/comm", check=False)
            if ready.stdout.strip() == b"postgres" and self.docker("exec", pg, "pg_isready", "-h", "/proof-socket",
                           "-U", "imageproof", "-d", self.database,
                           check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("disposable PostgreSQL initialization timed out")
        with psycopg.connect(host=str(self.socket), dbname=self.database, user="imageproof") as conn:
            ensure_schema(conn)
        env = {"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models", "PSEUDOLIFE_MCP_TOKEN": self.token,
               "PSEUDOLIFE_MCP_NO_SPAWN": "1", "PSEUDOLIFE_MCP_DATABASE_URL":
               f"postgresql://imageproof@localhost/{self.database}?host=/run/postgresql"}
        with os.fdopen(os.open(self.envfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in env.items()))
        self.volume = "image-proof-" + self.nonce
        self.docker("volume", "create", self.volume)
        cid = self.start_daemon()
        text = "Verified model evidence persists across an offline cold start."
        self.tool(cid, self.session(cid), "memory_store", {"text": text, "source": "image-proof"})
        self.docker("stop", "--time", "30", cid)
        self.remove(cid)
        cid = self.start_daemon()
        result = self.tool(cid, self.session(cid), "memory_search", {
            "query": "verified model evidence offline cold start", "top_k": 3, "rerank": True})
        if text not in json.dumps(result):
            raise RuntimeError("authenticated search did not return the stored seed")
        code, _, health = self.request(cid, "GET", "/health")
        if code != 200 or health.get("embedder", {}).get("backend") != "onnx" or health["embedder"].get("device") != "cpu":
            raise RuntimeError("authenticated read did not load the CPU ONNX embedder")
        code, _, _ = self.request(cid, "POST", "/mcp", {
            "jsonrpc": "2.0", "id": 3, "method": "tools/list"}, auth=False)
        if code != 401:
            raise RuntimeError("unauthenticated MCP request was not refused")
        for container in (cid, pg):
            state = json.loads(self.docker("inspect", container).stdout)[0]
            if state["HostConfig"]["NetworkMode"] != "none" or set(state["NetworkSettings"]["Networks"]) != {"none"}:
                raise RuntimeError("proof container unexpectedly has networking")
        return {"status": "pass", "network": "none", "cold_restart": True,
                "authenticated_memory_search": True, "returned_seed": True,
                "unauthenticated_status": code, "cpu_onnx_health": True,
                "image_id": json.loads(self.docker("image", "inspect", self.image).stdout)[0]["Id"]}

    def cleanup(self) -> bool:
        ok = True
        for cid in reversed(self.containers):
            try:
                ok = self.docker("rm", "-f", cid, check=False).returncode == 0 and ok
            except (OSError, subprocess.TimeoutExpired, RuntimeError):
                ok = False
        if self.volume:
            try:
                ok = self.docker("volume", "rm", self.volume, check=False).returncode == 0 and ok
            except (OSError, subprocess.TimeoutExpired, RuntimeError):
                ok = False
        self.envfile.unlink(missing_ok=True)
        return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--postgres-image", default="pseudolife-pg:18")
    parser.add_argument("--models", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--sudo", action="store_true", help="run Docker through sudo -n on a dev machine")
    args = parser.parse_args()
    # Read schema code from this checkout; no installed product package is required.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "fail"}
    directory = tempfile.TemporaryDirectory(prefix="image-model-proof-")
    proof = None
    try:
        proof = Proof(args.image, args.postgres_image, args.models, Path(directory.name), args.sudo)
        report = proof.run()
    finally:
        report["cleanup"] = proof.cleanup() if proof else True
        try:
            directory.cleanup()
        except OSError:
            report["cleanup"] = False
        if not report["cleanup"]:
            report["status"] = "fail"
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
