"""Record the Python daemon's MCP tool catalogue for the Rust daemon to embed.

usage: python mcp_catalogue.py <python-port> <token> <out-dir>

The token's principal must resolve to the minimal tier with no override
(the catalogue script expands it twice, to core and then full, and collapses
it back). Writes, under <out-dir>:
  tools.jsonl  every tool object exactly as Python serializes it in tools/list
               at the full tier, one per line, in registration order;
  tiers.json   {tool: tier}, the lowest tier whose list shows the tool.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_wire import Session, json_spans, sse_data  # noqa: E402


def listing(s):
    data = sse_data(s.call("tools/list", {"_meta": {}}))[0]
    start = data.index('"tools":[') + len('"tools":')
    spans = json_spans(data, start)
    return spans, [json.loads(sp)["name"] for sp in spans]


def main(port, token, out):
    s = Session(port, token)
    seen = {}
    spans = None
    for step in ("minimal", "core", "full"):
        if step != "minimal":
            r = json.loads(sse_data(s.tool("memory_toolset", {"action": "expand"}))[0])
            assert r["result"]["structuredContent"]["current"] == step, r
        spans, names = listing(s)
        for n in names:
            seen.setdefault(n, step)
    for _ in range(2):
        s.tool("memory_toolset", {"action": "collapse"})
    s.close()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tools.jsonl").write_text("".join(sp + "\n" for sp in spans), encoding="utf-8", newline="\n")
    (out / "tiers.json").write_text(json.dumps(seen, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(spans)} tools; tiers: " + ", ".join(
        f"{t}={sum(1 for v in seen.values() if v == t)}" for t in ("minimal", "core", "full")))


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2], sys.argv[3])
