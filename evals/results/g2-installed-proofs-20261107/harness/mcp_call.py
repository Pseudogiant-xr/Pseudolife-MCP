"""Run the stdio MCP registration the installer recorded and call tools.

    python3 mcp_call.py <args-file> <tool> '<json args>' [<tool> '<json>' ...]

The args file is the stub claude's record of `claude mcp add`: KEY=VALUE
env pairs after --env, then `--` and the command. Prints one JSON line per
call: {"tool", "isError", "text"}. Exit 1 if any call errors.
"""
import json, os, subprocess, sys

args = open(sys.argv[1], encoding="utf-8").read().splitlines()
sep = args.index("--")
env = dict(os.environ)
for a in args[:sep]:
    if "=" in a and not a.startswith("-"):
        k, v = a.split("=", 1); env[k] = v
cmd = args[sep + 1:]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True)
n = 0
def rpc(method, params=None, notify=False):
    global n
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None: msg["params"] = params
    if not notify:
        n += 1; msg["id"] = n
    p.stdin.write(json.dumps(msg) + "\n"); p.stdin.flush()
    if notify: return None
    while True:
        line = p.stdout.readline()
        if not line: raise SystemExit(f"shim closed stdout during {method}")
        r = json.loads(line)
        if r.get("id") == n: return r
init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "g2-proof", "version": "1"}})
print(json.dumps({"server": init.get("result", {}).get("serverInfo")}))
rpc("notifications/initialized", notify=True)
bad = 0
calls = sys.argv[2:]
for tool, raw in zip(calls[::2], calls[1::2]):
    r = rpc("tools/call", {"name": tool, "arguments": json.loads(raw)})
    res = r.get("result") or {}
    text = "".join(c.get("text", "") for c in res.get("content", []))
    err = bool(res.get("isError")) or "error" in r
    bad += err
    print(json.dumps({"tool": tool, "isError": err, "text": text[:1500] or r.get("error")}))
p.stdin.close(); p.wait(timeout=30)
sys.exit(1 if bad else 0)
