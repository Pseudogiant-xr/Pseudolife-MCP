"""Captured graph-refresh observations from the frozen lifecycle test."""
import hashlib
import json
from pathlib import Path


REFERENCE = Path(__file__).with_name("fixtures") / "graph-refresh-reference.json"
REFERENCE_SHA256 = "f155f15b82d040edade7118fbeeef6e1a6f2d628cc100fa8f39ed9179443e5df"


def refresh_inputs(graphs):
    for name, refreshes in graphs.items():
        # Entity ids are minted first for the fact subjects, then for each
        # relate's endpoints as they appear.
        ids = {"Ledger API": 1, "Orphan Host": 2}
        for edges in refreshes:
            for a, _, b, _ in edges:
                ids.setdefault(a, len(ids) + 1)
                ids.setdefault(b, len(ids) + 1)
        for edges in refreshes:
            yield name, {
                "entities": [{"id": i, "display": d} for d, i in ids.items()],
                "edges": [{"src_id": ids[a], "dst_id": ids[b], "relation": relation,
                           "confidence": kw.get("confidence", 0.8), "origin": kw.get("origin")}
                          for a, relation, b, kw in edges],
            }


def refresh_references(graphs):
    captured = REFERENCE.read_text(encoding="utf-8")
    if hashlib.sha256(captured.encode("utf-8")).hexdigest() != REFERENCE_SHA256:
        raise ValueError("graph refresh reference differs from the pinned capture")
    references = json.loads(captured)["refreshes"]
    for (name, case), ref in zip(refresh_inputs(graphs), references, strict=True):
        encoded = json.dumps(case, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != ref["input_sha256"]:
            raise ValueError(f"{name}: asserted graph differs from the captured refresh")
        yield name, ref["exact"], ref["seed_42"]
