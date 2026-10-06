"""Disposable daemon adapter with deterministic test embeddings, no model parity."""
from pathlib import Path


def main():
    from evals.memory_policy_daemon import _server_check, check_database, check_port
    import os
    check_database(os.environ["PSEUDOLIFE_MCP_DATABASE_URL"])
    _server_check(os.environ["PSEUDOLIFE_MCP_DATABASE_URL"])
    check_port(int(os.environ["PSEUDOLIFE_MCP_PORT"]))
    from tests.fake_embedder import FakeSentenceTransformer
    from pseudolife_memory.memory import embedding
    from pseudolife_memory.service import MemoryService
    if not Path(embedding.__file__).resolve().is_relative_to(Path.cwd().resolve()):
        raise RuntimeError("daemon production import root differs")
    embedding.SentenceTransformer = FakeSentenceTransformer

    def protocol_warmup(service):
        with service._lock:
            service._ensure_init()

    MemoryService.warmup = protocol_warmup
    from evals.rust_baseline.daemon_child import main as guarded_main
    guarded_main()


if __name__ == "__main__":
    main()
