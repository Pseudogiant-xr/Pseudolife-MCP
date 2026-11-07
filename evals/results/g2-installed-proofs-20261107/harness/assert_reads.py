"""Assert on the entries a recorded mcp_call.py run returned, not its raw text.

    python3 assert_reads.py <log> [--count N] [--present TEXT]... [--absent TEXT]... [--fact VALUE]

A search reply echoes its own query, so grepping the log for a phrase
would always find the phrase being searched for; this reads the returned
entries' texts instead. Exit 1 on any failed condition.
"""
import argparse
import json
import sys

parser = argparse.ArgumentParser()
parser.add_argument("log")
parser.add_argument("--count", type=int)
parser.add_argument("--present", action="append", default=[])
parser.add_argument("--absent", action="append", default=[])
parser.add_argument("--fact")
args = parser.parse_args()

texts, facts, errors = [], [], []
for line in open(args.log, encoding="utf-8"):
    reply = json.loads(line)
    if "tool" not in reply:
        continue
    if reply["isError"]:
        errors.append(reply["tool"])
        continue
    body = json.loads(reply["text"])
    if reply["tool"] == "memory_search":
        texts.extend(entry["text"] for entry in body["entries"])
    elif reply["tool"] == "memory_fact_get":
        facts.append((body.get("record") or {}).get("value"))

failed = [f"{tool} returned an error" for tool in errors]
if args.count is not None and len(texts) != args.count:
    failed.append(f"expected {args.count} entries, got {len(texts)}")
failed += [f"no entry contains {p!r}" for p in args.present if not any(p in t for t in texts)]
failed += [f"an entry contains {a!r}" for a in args.absent if any(a in t for t in texts)]
if args.fact is not None and args.fact not in facts:
    failed.append(f"fact value {args.fact!r} not read back (got {facts})")
for message in failed:
    print("FAIL", message)
print(f"{len(texts)} entries, facts {facts}: {'ok' if not failed else 'failed'}")
sys.exit(1 if failed else 0)
