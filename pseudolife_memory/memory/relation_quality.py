"""Deterministic, on-box relation quality scoring — the single source of truth
for structural relation type rules. Pure (stdlib only); no model calls.

`infer_type` returns a coarse entity type or None (unknown). `edge_confidence`
computes a per-edge confidence that penalizes type-violations + the vague
`related-to` catch-all. `unknown` types are always NEUTRAL (never penalize), so a
correctly-extracted edge whose entities we can't type keeps full confidence.
"""
from __future__ import annotations

import re

# Allowed (src_types, dst_types) per STRUCTURAL relation. The single source of
# truth — evals/relation_extraction_bench.py imports this. depends-on / uses /
# configures / related-to are intentionally absent (any->any, no type penalty).
TYPE_CONSTRAINTS: dict[str, tuple[set[str], set[str]]] = {
    "runs-on":        ({"service", "process", "component", "tool", "file", "datastore"}, {"runtime", "host"}),
    "hosts":          ({"runtime", "host"}, {"service", "process", "component", "datastore"}),
    "stores-data-in": ({"service", "process", "tool"}, {"datastore", "file"}),
    "part-of":        ({"component", "service", "file", "datastore"}, {"component", "service"}),
}

_CMD_PREFIXES = ("docker compose", "docker ", "git ", "pip ", "npm ", "curl ",
                 "kubectl ", "psql ", "pg_dump", "wsl ")
_FILE_EXT = (".py", ".yaml", ".yml", ".json", ".md", ".txt", ".sql", ".ps1",
             ".sh", ".gguf", ".toml", ".ini", ".cfg", ".fx")
_PERSON = {"user", "the user", "i", "me", "admin", "operator"}
_RUNTIME = {"docker", "docker-desktop", "windows", "windows 11", "windows box",
            "windows host", "linux", "wsl", "host", "vm", "kubernetes", "k8s",
            "container", "4090", "gpu", "cpu", "dx11", "dx12"}
_DATASTORE = {"postgres", "postgresql", "pg", "chromadb", "redis", "valkey",
              "sqlite", "kafka", "rabbitmq", "bank"}


def infer_type(name: str) -> str | None:
    """Coarse entity type, or None if we can't confidently type it. Order matters:
    command-strings/concepts before the runtime glob; file-suffix before tool."""
    n = (name or "").strip().lower()
    if not n:
        return None
    # concept / non-entity — FIRST so "docker compose -f ..." != runtime
    if any(n.startswith(p) for p in _CMD_PREFIXES):
        return "concept"
    if re.fullmatch(r"v?\d+(?:\.\d+)*", n):                 # "11", "v8", "0.2.0"
        return "concept"
    # NOTE: 'schema' / command-strings deliberately shadow later rules — e.g. 'schema.sql' types as concept (not file), 'docker-db' as runtime (not datastore). Intentional per the partial-lexicon design.
    if n.startswith("schema") or n in ("branch", "master", "main"):
        return "concept"
    # file (by extension) before tool (identifier)
    if n.endswith(_FILE_EXT):
        return "file"
    if n in _PERSON:
        return "person"
    if n in _RUNTIME or n.startswith(("docker-", "windows")):
        return "runtime"
    if n in _DATASTORE or n.endswith("-db") or "database" in n:
        return "datastore"
    if "daemon" in n or n.endswith(("-service", "-server", "-worker")) \
            or "sidecar" in n or n == "gateway":
        return "service"
    if (re.fullmatch(r"[a-z][a-z0-9_]*", n) and "_" in n) or n.endswith("()"):
        return "tool"
    return None


def edge_confidence(src: str, relation: str, dst: str) -> float:
    """Deterministic per-edge confidence. 0.70 clean / 0.45 related-to /
    0.175 known type-violation. Unknown types never penalize."""
    base = 0.45 if relation == "related-to" else 0.70
    constraint = TYPE_CONSTRAINTS.get(relation)
    if constraint:
        st, dt = infer_type(src), infer_type(dst)
        if st and dt:                      # only when BOTH endpoints are typed
            src_ok, dst_ok = constraint
            if st not in src_ok or dt not in dst_ok:
                base *= 0.25
    return round(base, 3)
