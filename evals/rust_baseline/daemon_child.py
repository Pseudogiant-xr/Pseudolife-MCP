"""Refuse a live target before importing or starting the real Python daemon."""
import os

from evals.memory_policy_daemon import _server_check, check_database, check_port


def install_readiness_identity(daemon, nonce, runtime=None):
    original = getattr(daemon, "_build_health_payload", None)
    if not nonce or not callable(original):
        raise RuntimeError("readiness identity hook unavailable")

    def identified_health(*args, **kwargs):
        payload = original(*args, **kwargs)
        if not isinstance(payload, dict):
            raise RuntimeError("readiness identity hook unavailable")
        payload["baseline_instance"] = {"nonce": nonce, "pid": os.getpid()}
        if runtime is not None:
            payload["baseline_instance"]["runtime"] = runtime
        return payload
    daemon._build_health_payload = identified_health


def main():
    dsn = os.environ["PSEUDOLIFE_MCP_DATABASE_URL"]
    check_database(dsn)
    _server_check(dsn)
    check_port(int(os.environ["PSEUDOLIFE_MCP_PORT"]))
    from pseudolife_memory import daemon
    from evals.rust_port.provenance import runtime_metadata
    # Only the disposable instrument adds identity; normal health computation,
    # status and application handlers remain the selected source's own code.
    install_readiness_identity(daemon, os.environ.get("PSEUDOLIFE_BASELINE_NONCE"), runtime_metadata(os.getcwd()))
    daemon.run_daemon()


if __name__ == "__main__":
    main()
