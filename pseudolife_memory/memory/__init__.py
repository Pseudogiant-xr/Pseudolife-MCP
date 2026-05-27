"""Neural memory subsystem — MIRAS continuum + ChromaDB reference bank.

Slimmed copy from PseudoLife: only the memory layer travels — no LLM
backends, no chat engine, no Electron. Built to be wrapped by an MCP
server so Claude can use the bank as a persistent cross-session memory.
"""

from pseudolife_memory.memory.cms import ContinuumMemorySystem, SCHEMA_VERSION
from pseudolife_memory.memory.context_builder import ContextBuilder
from pseudolife_memory.memory.embedding import EmbeddingPipeline
from pseudolife_memory.memory.reference_bank import ReferenceBank
from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult

__all__ = [
    "ContinuumMemorySystem",
    "ContextBuilder",
    "EmbeddingPipeline",
    "MemoryEntry",
    "ReferenceBank",
    "RetrievalResult",
    "SCHEMA_VERSION",
]
