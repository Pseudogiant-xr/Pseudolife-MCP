"""Distil an oracle recording into the committed CI goldens for the MCP surface.

usage: python mcp_goldens.py <record.json> <goldens/mcp.json>

<record.json> is `mcp_compare.py record` output from the Python daemon (the
harness environment, tokens configured). The goldens keep what the Rust unit
tests can check without a daemon: the SHA-256 of each tier's tools/list
result, every initialize result, and the result object of each
memory_toolset call in the `toolset` and `calls` cases. Full list bytes live
in src/mcp/tools.jsonl already, so the lists are stored as hashes.
"""
import hashlib
import json
import sys
from pathlib import Path

PROTOCOL_CASES = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28",
                  "2030-01-01", "x"]
# mcp_compare.toolset: per identity, 5 calls, a list, a second session's list,
# 3 collapses and a status (11 responses); then 7 binding refusals.
TOOLSET_IDENTITIES = [
    {"principal": "carol", "writer": None},
    {"principal": "alice", "writer": None},
    {"principal": "bob", "writer": None},
    {"principal": "default", "writer": "writer-x"},
]
TOOLSET_ARGS = [{"action": "status"}, {"action": "expand"}, {"action": "status"},
                {"action": "expand"}, {"action": "expand"}, None, None,
                {"action": "collapse"}, {"action": "collapse"}, {"action": "collapse"},
                {"action": "status"}]
BINDING_ARGS = [{"action": "bogus"}, {}, {"actoin": "status"}, {"actoin": "x", "zzz": 1},
                {"action": "status", "verbose": True}, {"action": 3}, {"action": None}]


def result_text(resp):
    """The JSON text of a response's `result` (or `error`) member, exactly."""
    data = resp["body"]["sse"][0]
    for key in ('"result":', '"error":'):
        i = data.find(key)
        if i >= 0:
            return data[i + len(key):-1]
    raise ValueError(data)


def main(record, out):
    rec = json.loads(Path(record).read_text(encoding="utf-8"))
    lists = rec["lists"]
    tiers = {"minimal": result_text(lists[2]), "core": result_text(lists[0]),
             "full": result_text(rec["toolset"][5])}
    goldens = {
        "list_sha256": {t: hashlib.sha256(v.encode("utf-8")).hexdigest() for t, v in tiers.items()},
        "initialize": {v: result_text(r) for v, r in zip(PROTOCOL_CASES, rec["protocol_versions"])},
        "toolset": [],
        "binding": [],
    }
    ts = rec["toolset"]
    for n, ident in enumerate(TOOLSET_IDENTITIES):
        steps = []
        for k, args in enumerate(TOOLSET_ARGS):
            if args is not None:
                steps.append({"args": args, "result": result_text(ts[n * 11 + k])})
        goldens["toolset"].append({**ident, "steps": steps})
    base = len(TOOLSET_IDENTITIES) * 11
    for k, args in enumerate(BINDING_ARGS):
        goldens["binding"].append({"args": args, "result": result_text(ts[base + k])})
    goldens["binding"].append({"args": None, "result": result_text(rec["calls"][3])})
    goldens["unknown_tool"] = result_text(rec["calls"][0])
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(goldens, indent=1, ensure_ascii=False) + "\n",
                         encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
