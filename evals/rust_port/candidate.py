"""Candidate launch and external-server disposable-bank binding protocol."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import re
import subprocess
import time

from .harness import HttpClient, _base_url, isolated_env
from .processes import owned_process


def command_identity(command):
    if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
        raise ValueError("candidate command must be a nonempty JSON string array")
    executable = Path(command[0])
    return {"kind": "owned-command", "executable_sha256":
            hashlib.sha256(executable.read_bytes()).hexdigest() if executable.is_file() else None}


@contextmanager
def launched_candidate(command, dsn, private, *, source_root, cache_overrides, startup_timeout=180):
    from evals.rust_baseline.daemon import free_port
    from evals.rust_baseline.transport import TOKEN
    command_identity(command)
    private = Path(private)
    port, nonce = free_port(), secrets.token_hex(32)
    config = private / "config.yaml"
    config.write_text("embedding:\n  device: cpu\n  backend: torch\n  cpu_dtype: fp32\n"
                      "memory:\n  dream:\n    enabled: false\nupdates:\n  check_releases: false\n", encoding="utf-8")
    env = isolated_env(private / "home")
    from evals.memory_policy_bench import production_database
    from pseudolife_memory.storage.schema import PRODUCTION_DATABASE_ENV
    # Preserve the pre-scrub bank refusal identity, as the Python launcher does.
    # The supplied disposable DSN must not redefine the live bank for guards.
    env[PRODUCTION_DATABASE_ENV] = production_database()
    env.update(cache_overrides)
    env.update({"PSEUDOLIFE_MCP_DATABASE_URL": dsn, "PSEUDOLIFE_MCP_CONFIG": str(config),
        "PSEUDOLIFE_MCP_HOST": "127.0.0.1", "PSEUDOLIFE_MCP_PORT": str(port),
        "PSEUDOLIFE_MCP_TOKEN": TOKEN, "PSEUDOLIFE_EMBEDDING_CPU_DTYPE": "fp32",
        "PSEUDOLIFE_BASELINE_NONCE": nonce, "PSEUDOLIFE_MCP_NO_SPAWN": "1",
        "PSEUDOLIFE_AGENT_COORDINATION": "0", "PSEUDOLIFE_CODEX_DOORBELL": "0"})
    url = f"http://127.0.0.1:{port}"
    cleanup = {"readiness_identity_verified": False}
    with (private / "candidate.log").open("wb") as log:
        with owned_process(command, cwd=source_root, env=env, stdin=subprocess.DEVNULL,
                           stdout=log, stderr=log) as process:
            deadline = time.monotonic() + startup_timeout
            client = HttpClient(url, timeout=1)
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("owned candidate exited before readiness")
                try:
                    response = client.execute({"path": "/health"}, "http")
                    body = response["body"]
                    identity = body.get("baseline_instance", {}) if isinstance(body, dict) else {}
                    observed = identity.get("nonce")
                    if response["status"] == 200:
                        if (not isinstance(observed, str) or not secrets.compare_digest(observed, nonce)
                                or not process.owns_runtime_pid(identity.get("pid")) or body.get("status") != "ok"):
                            raise RuntimeError("owned candidate readiness identity mismatch")
                        cleanup["readiness_identity_verified"] = True
                        break
                except OSError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError("owned candidate readiness timed out")
            try:
                yield url, cleanup
            finally:
                # owned_process completes reclamation before the caller reads it.
                cleanup["ownership"] = process.owned_cleanup
        cleanup.update(daemon_stopped=process.owned_cleanup["process_stopped"],
                       children_stopped=process.owned_cleanup["subtree_stopped"])


@contextmanager
def external_candidate(url, dsn, token, *, candidate_nonce):
    """An eval adapter must bind, attest, then release the newly minted bank.

    The externally owned server is never terminated. A candidate URL with no
    adapter is refused rather than exercising an existing bank by assumption.
    """
    from psycopg.conninfo import conninfo_to_dict
    name = conninfo_to_dict(dsn)["dbname"]
    fingerprint = hashlib.sha256(name.encode()).hexdigest()
    nonce = secrets.token_hex(32)
    client = HttpClient(_base_url(url), timeout=180)
    headers = {"Authorization": "Bearer " + token}
    if not isinstance(candidate_nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate_nonce):
        raise ValueError("external candidate requires the isolated adapter's readiness nonce")
    ready = client.execute({"path": "/_rust_port/ready"}, "http", runtime_headers=headers)
    if ready["status"] != 200 or ready["body"] != {"nonce": candidate_nonce, "disposable": True}:
        raise RuntimeError("external candidate preflight identity not verified; bank credentials withheld")
    expected = {"nonce": nonce, "bank_sha256": fingerprint, "disposable": True}
    cleanup = {"externally_owned_server": True, "bank_binding_verified": False, "bank_released": False}
    failed = False
    try:
        response = client.execute({"path": "/_rust_port/disposable-bank", "method": "POST", "body": {
            "database_url": dsn, "token": token, **expected}}, "http", runtime_headers=headers)
        if response["status"] != 200 or response["body"] != expected:
            raise RuntimeError("external candidate disposable bank binding not verified")
        cleanup["bank_binding_verified"] = True
        yield url, cleanup
    except BaseException:
        failed = True
        raise
    finally:
        try:
            released = client.execute({"path": "/_rust_port/disposable-bank/release", "method": "POST",
                "body": {"nonce": nonce}}, "http", runtime_headers=headers)
            if released["status"] != 200 or released["body"] != {"nonce": nonce, "released": True}:
                raise RuntimeError("external candidate disposable bank release not verified")
            cleanup["bank_released"] = True
        except BaseException as exc:
            cleanup["bank_release_error"] = type(exc).__name__
            if not failed:
                raise
