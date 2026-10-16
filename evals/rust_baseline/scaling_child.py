"""Disposable measurement-only observations around the unchanged oracle."""
import os

from .daemon_child import install_readiness_identity


def main():
    from evals.memory_policy_daemon import _server_check, check_database, check_port
    dsn = os.environ["PSEUDOLIFE_MCP_DATABASE_URL"]
    check_database(dsn)
    _server_check(dsn)
    check_port(int(os.environ["PSEUDOLIFE_MCP_PORT"]))
    import torch
    from sentence_transformers import SentenceTransformer
    counts = {"model_encode_calls": 0, "model_encode_texts": 0}
    original_encode = SentenceTransformer.encode

    def counted_encode(self, sentences, *args, **kwargs):
        counts["model_encode_calls"] += 1
        counts["model_encode_texts"] += 1 if isinstance(sentences, str) else len(sentences)
        return original_encode(self, sentences, *args, **kwargs)

    SentenceTransformer.encode = counted_encode
    from pseudolife_memory import daemon, mcp_server
    from evals.rust_port.provenance import runtime_metadata
    original_health = daemon._build_health_payload

    def observed_health(*args, **kwargs):
        payload = original_health(*args, **kwargs)
        service = mcp_server.service
        cms, embedder = service._cms, service._embedder
        bands = cms.bands if cms is not None else []
        payload["baseline_observation"] = {
            **counts, "torch_threads": torch.get_num_threads(),
            "torch_interop_threads": torch.get_num_interop_threads(),
            "resident_entries": sum(len(b.entries) for b in bands),
            "entry_vector_bytes": sum(e.embedding.numel() * e.embedding.element_size()
                                      for b in bands for e in b.entries),
            "cache_size": embedder._cache_size if embedder else None,
            "cache_entries": len(embedder._cache) if embedder else None,
            "parameter_dtypes": sorted({str(p.dtype) for p in embedder.model.parameters()})
                if embedder else [],
        }
        return payload

    daemon._build_health_payload = observed_health
    install_readiness_identity(daemon, os.environ["PSEUDOLIFE_BASELINE_NONCE"], runtime_metadata(os.getcwd()))
    daemon.run_daemon()


if __name__ == "__main__":
    main()
