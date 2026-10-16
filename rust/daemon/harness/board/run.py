"""Board differential harness (slice W2-F): the Python daemon against the
Rust daemon, one scripted scenario at a time, each arm on its own
disposable bank.

usage (from rust/daemon/harness):
  python -m board.run live    --rust-bin PATH [--only NAME ...] [--out FILE] [--record]
  python -m board.run golden  --rust-bin PATH [--only NAME ...] [--out FILE]
  python -m board.run mutants --rust-bin PATH_BUILT_WITH_FEATURE_MUTANTS [--only NAME ...]

A scenario is a list of steps; each runs on the Python arm and then on the
Rust arm, and a step can use what earlier steps returned on its own arm
(credentials, ids). Per step it compares the status, the headers clients
read and the body by value after the rule-based normalizers in ``norm.py``.
After every write it dumps the board's tables on both arms and diffs them
row for row. After every scenario it checks each arm's audit chain with
Python's own ``verify_audit_chain``, recomputes every row's hash with
Python's ``audit_hash``, and requires every stored payload to equal
Python's ``_canonical`` of itself byte for byte.

Exit status: 0 only when no step, no state and no chain check differs.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import http.client
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
REPO = HARNESS.parents[2]
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(REPO))
import daemons  # noqa: E402
# The shared helper admits only the current slice's disposable database names.
os.environ["PL_HARNESS_SLICE"] = "w2f"
import pgdisposable as pg  # noqa: E402
from board import scenarios as S  # noqa: E402
from board.norm import Normalizer, Windows  # noqa: E402

GOLDENS = HERE / "goldens"
HEADERS_COMPARED = ("content-type", "cache-control", "x-pl-board")
BOARD_TABLES = ("coordination_agents", "coordination_messages", "coordination_events",
                "coordination_leases", "coordination_lease_waiters", "coordination_wakes")
SEQUENCES = ("coordination_lease_fence", "coordination_lease_waiters_ticket_seq",
             "coordination_wakes_wake_id_seq")
META_KEYS = ("coordination_bank_id", "coordination_hlc_highwater")
MUTANTS = ["board-chain-skip-seq", "board-clock-behind", "board-lease-never-expires",
           "board-canonical-unsorted", "board-urgent-cap-off", "board-no-read-event",
           "board-receipt-path-live", "board-park-default-ttl",
           "mcp-agents-no-file-claim", "mcp-agents-no-awareness",
           "mcp-agents-awareness-self", "mcp-agents-awareness-order"]
# The MCP tool mutants live in the mcp-tools scenario; the rest in the REST ones.
MCP_MUTANTS = {"mcp-agents-no-file-claim", "mcp-agents-no-awareness",
               "mcp-agents-awareness-self", "mcp-agents-awareness-order"}


# ---- HTTP -----------------------------------------------------------------------

def call(port: int, method: str, path: str, headers, body: bytes | None, timeout=180.0) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.putrequest(method, path, skip_accept_encoding=True)
    for k, v in headers:
        c.putheader(k, v)
    if body is not None:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    raw = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders() if k.lower() in HEADERS_COMPARED}
    out = {"status": r.status, "headers": hdrs}
    if hdrs.get("content-type", "").startswith("application/json"):
        try:
            out["json"] = json.loads(raw)
        except ValueError:
            out["bytes"] = raw.decode("latin-1")
    else:
        out["bytes"] = raw.decode("utf-8", "replace")
    c.close()
    return out


GENERIC_TITLE = re.compile(r"session - \d{4}-\d{2}-\d{2} \d{2}:\d{2}")


def mcp_call(port: int, headers, tool: str, args: dict, timeout=180.0) -> dict:
    """One MCP tools/call on a fresh session: the structured result, an
    error result as status 422 (the board's REST statuses never use it)."""
    from mcp_wire import ACCEPT, PROTOCOL, header, request, sse_data
    base = [("Content-Type", "application/json"), ("Accept", ACCEPT), *headers]
    init = request(port, "POST", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                   "clientInfo": {"name": "board-harness", "version": "0"}}}, base, timeout)
    sid = header(init, "mcp-session-id")
    session = base + [("mcp-session-id", sid or ""), ("mcp-protocol-version", PROTOCOL)]
    request(port, "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}, session, timeout)
    r = request(port, "POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": tool, "arguments": args}}, session, timeout)
    request(port, "DELETE", None, session, timeout)
    try:
        res = json.loads(sse_data(r)[0])["result"]
    except (IndexError, KeyError, ValueError):
        # A broken session flow must not compare equal on both arms.
        raise RuntimeError(f"{tool}: no tools/call result (HTTP {r['status']}): {r['body'][:300]!r}")
    text = res["content"][0]["text"]
    value = res.get("structuredContent")
    if json.loads(text) != value:
        raise RuntimeError(f"{tool}: text and structuredContent differ")
    if isinstance(value, dict) and isinstance(value.get("peers"), list):
        # Awareness: each arm's own wall clock and minute-stamped titles.
        for peer in value["peers"]:
            if isinstance(peer.get("last_reported_at"), (int, float)):
                peer["last_reported_at"] = "<time>"
            if GENERIC_TITLE.fullmatch(peer.get("title") or ""):
                peer["title"] = "<session - date time>"
    return {"status": 422 if res.get("isError") else 200, "headers": {"content-type": "mcp"},
            "json": value}


# ---- bank state ----------------------------------------------------------------------

def dump(dsn: str) -> dict:
    import psycopg
    from psycopg.rows import dict_row
    out = {}
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET extra_float_digits = 3")
        for t in BOARD_TABLES:
            out[t] = conn.execute(f"SELECT * FROM {t}").fetchall()
        out["meta"] = conn.execute("SELECT key, value FROM meta WHERE key = ANY(%s) ORDER BY key",
                                   (list(META_KEYS),)).fetchall()
        out["sequences"] = conn.execute(
            "SELECT sequencename, last_value FROM pg_sequences WHERE sequencename = ANY(%s) "
            "ORDER BY sequencename", (list(SEQUENCES),)).fetchall()
    return out


def normalize_state(state: dict, norm: Normalizer) -> dict:
    out = {}
    for t in BOARD_TABLES:
        rows = []
        for r in state[t]:
            r = dict(r)
            payload = r.pop("payload") if t == "coordination_events" else None
            row = norm.value(r)
            if payload is not None:
                row["payload"] = norm.payload(payload)
            rows.append(json.dumps(row, sort_keys=True, default=str))
        out[t] = sorted(rows)
    meta = {}
    for r in state["meta"]:
        if r["key"] == "coordination_hlc_highwater" and isinstance(r["value"], list):
            phys, logical = r["value"]
            meta[r["key"]] = [norm.windows.hlc(phys) or "<unmatched hlc>", logical]
        else:
            meta[r["key"]] = norm.value(r["value"], "bank_id")
    out["meta"] = meta
    out["sequences"] = {r["sequencename"]: r["last_value"] for r in state["sequences"]}
    return out


def diff_state(a: dict, b: dict) -> list[str]:
    diffs = []
    for t in a:
        if a[t] == b.get(t):
            continue
        if isinstance(a[t], list):
            sa, sb = list(a[t]), list(b.get(t) or [])
            only_a = [r for r in sa if r not in sb]
            only_b = [r for r in sb if r not in sa]
            for r in only_a[:4]:
                diffs.append(f"{t}: row only in python: {r[:400]}")
            for r in only_b[:4]:
                diffs.append(f"{t}: row only in rust: {r[:400]}")
            if not only_a and not only_b:
                diffs.append(f"{t}: row multiplicity differs ({len(sa)} vs {len(sb)})")
        else:
            diffs.append(f"{t}: python {json.dumps(a[t], default=str)[:300]} vs rust "
                         f"{json.dumps(b.get(t), default=str)[:300]}")
    return diffs


def chain_check(dsn: str) -> list[str]:
    """Python's verifier, hash and canonical form over every row of a chain."""
    import psycopg
    from pseudolife_memory.storage.coordination import (
        _canonical, audit_events, audit_hash, verify_audit_chain)
    problems = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = [dict(r) for r in audit_events(conn)]
    verdict = verify_audit_chain(rows)
    if not verdict.get("ok"):
        problems.append(f"verify_audit_chain: {verdict}")
    for r in rows:
        if audit_hash(r["prev_hash"], r) != r["hash"]:
            problems.append(f"seq {r['seq']}: audit_hash differs")
        if r["payload"] != _canonical(json.loads(r["payload"])):
            problems.append(f"seq {r['seq']}: payload is not Python's canonical text: {r['payload'][:200]}")
    return problems


def credential_check(responses: list[dict], state: dict) -> list[str]:
    """Every credential a register returned hashes to its stored hash."""
    stored = {r["agent_id"]: r["credential_hash"] for r in state["coordination_agents"]}
    problems = []
    for resp in responses:
        body = resp.get("json")
        if isinstance(body, dict) and "credential" in body and "agent_id" in body:
            digest = hashlib.sha256(body["credential"].encode()).hexdigest()
            if stored.get(body["agent_id"], digest) != digest:
                problems.append(f"credential of {body['agent_id'][:8]} does not hash to its row")
    return problems


# ---- running ---------------------------------------------------------------------

class Arm:
    def __init__(self, name: str, daemon, dsn: str, retention_days: int = 90):
        self.name, self.daemon, self.dsn = name, daemon, dsn
        self.windows = Windows()
        self.norm = Normalizer(self.windows, retention_days)
        self.ctx = S.Ctx()
        self.responses: list[dict] = []

    def run(self, step: S.Step) -> dict:
        t0 = time.time()
        self.ctx.now = t0
        if step.kind == "sleep":
            time.sleep(step.seconds)
            resp = {"status": 0}
        elif step.kind == "age":
            S.age_bank(self.dsn, step.seconds)
            self.windows.shifts.append((len(self.windows.steps) - 1, step.seconds))
            resp = {"status": 0}
        elif step.kind == "mcp":
            _, _, headers, _ = step.request(self.ctx)
            tool, args = step.mcp(self.ctx) if callable(step.mcp) else step.mcp
            resp = mcp_call(self.daemon.port, headers, tool, args, timeout=step.timeout)
        else:
            method, path, headers, body = step.request(self.ctx)
            if path in ("/api/coordination/attach", "/api/coordination/heartbeat") and body is not None:
                try:
                    self.norm.request(json.loads(body), relative=callable(step.body))
                except (ValueError, UnicodeDecodeError):
                    pass  # Malformed request cases have no deadline to preserve.
            resp = call(self.daemon.port, method, path, headers, body, timeout=step.timeout)
        self.windows.steps.append((t0, time.time()))
        if step.save and resp.get("json") is not None:
            step.save(self.ctx, resp["json"])
        if "json" in resp:
            self.norm.learn(resp["json"])
        self.refresh_seqs()
        self.responses.append(resp)
        return resp

    def refresh_seqs(self) -> None:
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            rows = conn.execute(
                "SELECT seq, agent_id, recipient_agent_id, created_at, payload::jsonb->>'request_id', "
                "message_id FROM coordination_events WHERE event='send' ORDER BY seq").fetchall()
        self.norm.refresh_seqs([{"seq": r[0], "agent_id": r[1], "recipient_agent_id": r[2],
                                 "created_at": r[3], "request_id": r[4], "message_id": r[5]}
                                for r in rows])


def normalize_response(resp: dict, norm: Normalizer) -> dict:
    out = {"status": resp["status"], "headers": resp.get("headers", {})}
    if "json" in resp:
        out["json"] = norm.value(resp["json"])
    if "bytes" in resp:
        out["bytes"] = resp["bytes"]
    return out


def diff_values(a, b, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in b:
                out.append(f"{path}.{k}: only in python")
            elif k not in a:
                out.append(f"{path}.{k}: only in rust")
            else:
                out += diff_values(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff_values(x, y, f"{path}[{i}]")]
    numbers = (int, float, bool)
    if isinstance(a, numbers) and isinstance(b, numbers) and type(a) is not type(b):
        return [f"{path}: python {json.dumps(a)} vs rust {json.dumps(b)} (type)"]
    return [] if a == b else [f"{path}: python {json.dumps(a)[:240]} vs rust {json.dumps(b)[:240]}"]


def start(kind: str, scn: S.Scenario, binary: Path, root: Path, dsn: str, tag: str):
    home = daemons.make_home(root, f"w2f-{tag}-{kind}", scn.config_yaml)
    env = {"PSEUDOLIFE_RELEASE_CHECK": "0", "PSEUDOLIFE_PLUGIN_DIR": str(REPO / "plugin"),
           "PSEUDOLIFE_MCP_DATABASE_URL": dsn, **scn.env}
    env = daemons.base_env(home, env)
    port = daemons.free_port()
    if kind == "python":
        d = daemons.python_daemon(home, port, env)
    else:
        env["PSEUDOLIFE_DAEMON_DEBUG_BOARD"] = "1"
        # base_env scrubs these; the warmed-up scenarios need the model.
        for key in ("ORT_DYLIB_PATH", "PSEUDOLIFE_DAEMON_ONNX_DIR", "PSEUDOLIFE_DAEMON_RERANK_DIR",
                    "PSEUDOLIFE_DAEMON_ORT_THREADS"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        if os.environ.get("PSEUDOLIFE_DAEMON_MUTANT"):
            env["PSEUDOLIFE_DAEMON_MUTANT"] = os.environ["PSEUDOLIFE_DAEMON_MUTANT"]
        d = daemons.rust_daemon(binary, home, port, env)
    return d.start(240)


def wait_ready(port: int, timeout: float = 600.0, *, require_embedder: bool = True) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            body = call(port, "GET", "/health", [], None, timeout=30).get("json") or {}
            if body.get("db") == "ok" and (not require_embedder or "embedder" in body):
                return
        except OSError:
            pass
        time.sleep(2)
    raise RuntimeError("the daemon did not reach the required readiness tier")


def run_scenario(scn: S.Scenario, binary: Path, root: Path, mode: str, record: bool) -> dict:
    import yaml

    config = yaml.safe_load(scn.config_yaml or "") or {}
    retention_days = config.get("coordination", {}).get("audit_retention_days", 90)
    tag = scn.name.replace("-", "_")
    names = {side: pg.name(f"pl_cf_w2f_{tag}_{suffix}")
             for side, suffix in (("python", "py"), ("rust", "rs"))}
    sides = ["rust"] if mode == "golden" else ["python", "rust"]
    golden = load_golden(scn.name) if mode == "golden" else None
    arms: dict[str, Arm] = {}
    rows, state_diffs, procs = [], [], {}
    with ExitStack() as cleanup:
        for side in sides:
            cleanup.callback(pg.drop, names[side])
            dsn = pg.create(names[side])
            procs[side] = start(side, scn, binary, root, dsn, tag)
            cleanup.callback(procs[side].stop)
            arms[side] = Arm(side, procs[side], dsn, retention_days)
        for side, proc in procs.items():
            # Both arms need storage before read-only Console snapshots.
            # Python also finishes embedding warmup, which holds its service
            # lock; the Rust board intentionally needs only the storage tier.
            wait_ready(proc.port, require_embedder=side == "python" or getattr(scn, "needs_bank", False))
        recorded = {"responses": {}, "states": {}}
        for i, step in enumerate(scn.steps()):
            out = {side: arms[side].run(step) for side in sides}
            if step.kind not in ("http", "mcp") or not getattr(step, "compare", True):
                continue
            py = (normalize_response(out["python"], arms["python"].norm) if "python" in out
                  else golden["responses"][str(i)])
            rs = normalize_response(out["rust"], arms["rust"].norm)
            diffs = diff_values(py, rs)
            rows.append({"step": i, "name": step.name, "python_status": py["status"],
                         "rust_status": rs["status"], "diffs": diffs})
            if record:
                recorded["responses"][str(i)] = py
            if step.write:
                states = {side: normalize_state(dump(arms[side].dsn), arms[side].norm) for side in sides}
                ref = states.get("python") or golden["states"].get(str(i))
                if record:
                    recorded["states"][str(i)] = states["python"]
                for d in diff_state(ref, states["rust"]) if ref is not None else []:
                    state_diffs.append(f"after step {i} ({step.name}): {d}")
        chain = {side: chain_check(arms[side].dsn) for side in sides}
        creds = {side: credential_check(arms[side].responses, dump(arms[side].dsn)) for side in sides}
    if record:
        save_golden(scn.name, recorded)
    problems = [f"{side} chain: {p}" for side, ps in chain.items() for p in ps]
    problems += [f"{side}: {p}" for side in sides for p in sorted(set(arms[side].norm.problems))]
    problems += [f"{side}: {p}" for side, ps in creds.items() for p in ps]
    return {"scenario": scn.name, "steps": rows, "state_diffs": state_diffs, "chain": problems}


def run_mixed(binary: Path, root: Path, first: str) -> dict:
    """One bank, two writers: ``first`` appends, stops, the other appends
    to the same chain, and Python's verifier must accept the whole chain."""
    scn = S.MIXED
    name = pg.name(f"pl_cf_w2f_mixed_{first}")
    dsn = pg.create(name)
    problems = []
    try:
        for kind in ((first, "rust" if first == "python" else "python")):
            d = start(kind, scn, binary, root, dsn, f"mixed-{first}")
            arm = Arm(kind, d, dsn)
            try:
                for step in scn.steps():
                    r = arm.run(step)
                    if step.kind == "http" and r["status"] != 200:
                        problems.append(f"{kind} {step.name}: {r['status']} {r.get('json')}")
            finally:
                d.stop()
        problems += chain_check(dsn)
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            n = conn.execute("SELECT count(*) FROM coordination_events").fetchone()[0]
        if n < 8:
            problems.append(f"only {n} events written")
    finally:
        pg.drop(name)
    return {"scenario": f"mixed-chain ({first} first)", "steps": [], "state_diffs": [],
            "chain": problems}


def load_golden(name: str) -> dict:
    return json.loads((GOLDENS / f"{name}.json").read_text(encoding="utf-8"))


def save_golden(name: str, data: dict) -> None:
    GOLDENS.mkdir(exist_ok=True)
    data = {"normalizers": "board/norm.py (ids by first appearance, T<step>+<offset>, H<step>)",
            **data}
    (GOLDENS / f"{name}.json").write_text(json.dumps(data, indent=1, sort_keys=True) + "\n",
                                          encoding="utf-8", newline="\n")


def summarize(results) -> tuple[int, int, int, int]:
    steps = sum(len(r["steps"]) for r in results)
    diff_steps = sum(1 for r in results for s in r["steps"] if s["diffs"])
    state = sum(len(r["state_diffs"]) for r in results)
    chain = sum(len(r["chain"]) for r in results)
    return steps, diff_steps, state, chain


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live", "golden", "mutants"])
    ap.add_argument("--rust-bin", required=True, type=Path)
    ap.add_argument("--only", nargs="+", choices=[*S.SCENARIOS, "mixed"])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()
    names = args.only or list(S.SCENARIOS) + ["mixed"]
    if args.mode == "golden" and not any(n in S.SCENARIOS for n in names):
        ap.error("golden mode requires at least one recorded scenario")
    root = daemons.scratch_root()

    def one_pass(mode: str):
        results = [run_scenario(S.SCENARIOS[n](), args.rust_bin, root, mode, args.record)
                   for n in names if n in S.SCENARIOS]
        if "mixed" in names and mode != "golden":
            results += [run_mixed(args.rust_bin, root, "python"), run_mixed(args.rust_bin, root, "rust")]
        return results

    if args.mode == "mutants":
        outcome = {}
        original = os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
        try:
            baseline_results = one_pass("live")
            steps, diff_steps, state, chain = summarize(baseline_results)
            baseline = {"steps": steps, "diff_steps": diff_steps, "state_diffs": state, "chain": chain}
            print("unmutated baseline:", baseline, flush=True)
            if diff_steps or state or chain:
                if args.out:
                    args.out.write_text(json.dumps({"baseline": baseline,
                        "baseline_results": baseline_results, "mutants": {}, "survivors": [],
                        "error": "unmutated_baseline_differs"}, indent=1, default=str), encoding="utf-8")
                return 1
            only = set(args.only or [])
            for m in MUTANTS:
                # Each group runs when any of its scenarios was selected.
                wanted = ("mcp-tools" in only) if m in MCP_MUTANTS else bool(only - {"mcp-tools"})
                if only and not wanted:
                    continue
                os.environ["PSEUDOLIFE_DAEMON_MUTANT"] = m
                steps, diff_steps, state, chain = summarize(one_pass("live"))
                outcome[m] = {"diff_steps": diff_steps, "state_diffs": state, "chain": chain}
                print(f"mutant {m}: {diff_steps} step diffs, {state} state diffs, {chain} chain problems",
                      flush=True)
        finally:
            if original is None:
                os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
            else:
                os.environ["PSEUDOLIFE_DAEMON_MUTANT"] = original
        survivors = [m for m, o in outcome.items() if not any(o.values())]
        if args.out:
            args.out.write_text(json.dumps({"baseline": baseline, "mutants": outcome,
                                           "survivors": survivors}, indent=1), encoding="utf-8")
        print("survivors:", survivors or "none")
        return 1 if survivors else 0

    results = one_pass(args.mode)
    steps, diff_steps, state, chain = summarize(results)
    for r in results:
        for s in r["steps"]:
            if s["diffs"]:
                print(f"DIFF [{r['scenario']}] #{s['step']} {s['name']}: {s['diffs'][:4]}")
        for d in r["state_diffs"][:12]:
            print(f"STATE [{r['scenario']}] {d}")
        for p in r["chain"][:12]:
            print(f"CHAIN [{r['scenario']}] {p}")
    summary = {"steps": steps, "diff_steps": diff_steps, "state_diffs": state,
               "chain_problems": chain, "scenarios": len(results)}
    print(json.dumps(summary))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=1,
                                       default=str), encoding="utf-8")
    return 1 if diff_steps or state or chain else 0


if __name__ == "__main__":
    raise SystemExit(main())
