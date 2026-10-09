"""Judge the spike against the Python daemon on one disposable bank.

usage: python compare.py <python-port> <spike-port> <tokens.env> <out.json>

tokens.env holds T_DEFAULT (PSEUDOLIFE_MCP_TOKEN), T_ALICE (mapped env
principal), T_CAROL (stored, paired) and T_DAVE (stored, revoked).

Part 1 (contract): the same requests go to both daemons; status codes and
JSON shape (keys and value types, recursively) are diffed, and the bodies of
error responses are diffed by value. Health keys that Python emits only for
subsystems outside the slice are reported separately as declared omissions.

Part 2 (ranking): queries.json at top_k=8 with recency disabled; top-8 set
overlap and Kendall tau-b per query, over the dense path (bm25=false), and,
when the spike serves BM25, over the default path too.
"""
import http.client
import json
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Health keys owned by subsystems the spike does not carry (spec: "Conditional keys").
DECLARED_HEALTH_OMISSIONS = {"coordination", "updates", "extractor", "stall", "hooks_digest", "build",
                             "init_refusal", "not_ready", "migration_partial", "last_backup",
                             "dream_tracking_error", "capacity_warning", "lesson_reconciliation_required"}
# Value-free fields: compared by type only (spec: "Free").
FREE_VALUES = {"version", "memory", "access_count", "score", "timestamp"}


def tokens(path):
    out = {}
    for line in open(path):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            out[k] = v
    return out


def call(port, method, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    c.putrequest(method, path, skip_accept_encoding=True)
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    raw = r.read()
    ctype = r.getheader("content-type")
    try:
        body = json.loads(raw)
    except ValueError:
        body = {"<non-json>": raw[:200].decode("latin-1")}
    return r.status, ctype, body


def shape(v):
    if isinstance(v, dict):
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        # A list's shape is the union of its element shapes (entries vary by optional keys only).
        return ["list", sorted({json.dumps(shape(x), sort_keys=True) for x in v})]
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "int" if isinstance(v, int) else "float"
    return type(v).__name__


def diff_shape(a, b, path=""):
    """Differences between two shapes, as strings."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in b:
                out.append(f"{path}.{k}: only in python")
            elif k not in a:
                out.append(f"{path}.{k}: only in spike")
            else:
                out += diff_shape(a[k], b[k], f"{path}.{k}")
    elif a != b:
        out.append(f"{path}: python {json.dumps(a)[:160]} vs spike {json.dumps(b)[:160]}")
    return out


def entry_shapes(body):
    """Per-entry key/type maps, merged (so empty-vs-nonempty lists still compare)."""
    merged = {}
    for e in body.get("entries", []):
        for k, v in e.items():
            merged.setdefault(k, set()).add(shape(v) if not isinstance(v, (dict, list)) else json.dumps(shape(v)))
    return {k: sorted(v) for k, v in merged.items()}


def contract_cases(t):
    auth = {"Authorization": f"Bearer {t['T_DEFAULT']}"}
    s = "/api/search?"
    q = lambda **kw: s + urllib.parse.urlencode(kw)
    return [
        ("health", "GET", "/health", {}),
        ("health POST", "POST", "/health", {}),
        ("no auth header", "GET", q(q="deploy"), {}),
        ("basic scheme", "GET", q(q="deploy"), {"Authorization": f"Basic {t['T_DEFAULT']}"}),
        ("bearer, no token", "GET", q(q="deploy"), {"Authorization": "Bearer"}),
        ("bearer, blank token", "GET", q(q="deploy"), {"Authorization": "Bearer  \t "}),
        ("unknown token", "GET", q(q="deploy"), {"Authorization": "Bearer not-a-real-token"}),
        ("lowercase scheme, tabbed token", "GET", q(q="deploy"), {"Authorization": f"bearer \t{t['T_DEFAULT']}\t"}),
        ("mapped env principal", "GET", q(q="deploy"), {"Authorization": f"Bearer {t['T_ALICE']}"}),
        ("stored principal", "GET", q(q="deploy"), {"Authorization": f"Bearer {t['T_CAROL']}"}),
        ("revoked stored principal", "GET", q(q="deploy"), {"Authorization": f"Bearer {t['T_DAVE']}"}),
        ("latin-1 token bytes", "GET", q(q="deploy"), {"Authorization": "Bearer caf\xe9"}),
        ("PUT search", "PUT", q(q="deploy"), auth),
        ("POST search (wrong verb)", "POST", q(q="deploy"), auth),
        ("unknown /api path", "GET", "/api/nope", auth),
        ("GET on POST-only path", "GET", "/api/delete", auth),
        ("non-api path, no auth", "GET", "/whatever", {}),
        ("search: blank q", "GET", q(q=""), auth),
        ("search: whitespace q", "GET", q(q="   "), auth),
        ("search: no q", "GET", s, auth),
        ("search: top_k=3", "GET", q(q="memory bands", top_k=3), auth),
        ("search: top_k=abc", "GET", q(q="memory bands", top_k="abc"), auth),
        ("search: top_k=0", "GET", q(q="memory bands", top_k=0), auth),
        ("search: top_k=-1", "GET", q(q="memory bands", top_k=-1), auth),
        ("search: top_k=' 4 '", "GET", q(q="memory bands", top_k=" 4 "), auth),
        ("search: band=flat", "GET", q(q="memory bands", band="flat"), auth),
        ("search: band=nope", "GET", q(q="memory bands", band="nope,flat"), auth),
        ("search: source match", "GET", q(q="memory bands", source="spike-seed"), auth),
        ("search: source miss", "GET", q(q="memory bands", source="other"), auth),
        ("search: tag miss", "GET", q(q="memory bands", tag="x"), auth),
        ("search: min_score=0.6", "GET", q(q="memory bands", min_score=0.6), auth),
        ("search: min_score=abc", "GET", q(q="memory bands", min_score="abc"), auth),
        ("search: min_score=0.99", "GET", q(q="memory bands", min_score=0.99), auth),
        ("search: repeated q (last wins)", "GET", s + "q=first&q=install+the+plugin", auth),
        ("search: plus as space", "GET", s + "q=install+the+plugin&disable_recency_boost=yes", auth),
        ("search: bm25=false", "GET", q(q="schema version bump", bm25="false"), auth),
    ]


def run_contract(pp, sp, t):
    rows = []
    for name, method, path, headers in contract_cases(t):
        ps, pct, pb = call(pp, method, path, headers)
        ss, sct, sb = call(sp, method, path, headers)
        diffs = []
        if ps != ss:
            diffs.append(f"status: python {ps} vs spike {ss}")
        if pct != sct:
            diffs.append(f"content-type: python {pct} vs spike {sct}")
        declared = []
        if path.startswith("/health"):
            declared = sorted(k for k in pb if k not in sb and k in DECLARED_HEALTH_OMISSIONS)
            pb = {k: v for k, v in pb.items() if k not in DECLARED_HEALTH_OMISSIONS}
        if isinstance(pb, dict) and "entries" in pb and isinstance(sb, dict) and "entries" in sb:
            top_p = {k: shape(v) for k, v in pb.items() if k != "entries"}
            top_s = {k: shape(v) for k, v in sb.items() if k != "entries"}
            diffs += diff_shape(top_p, top_s)
            pe, se = entry_shapes(pb), entry_shapes(sb)
            if pb["entries"] and sb["entries"]:
                diffs += diff_shape(pe, se, ".entries[]")
            elif bool(pb["entries"]) != bool(sb["entries"]):
                diffs.append(f"entries: python {len(pb['entries'])} vs spike {len(sb['entries'])}")
            if pb.get("count") != sb.get("count") or pb.get("low_confidence") != sb.get("low_confidence"):
                diffs.append(f"count/low_confidence: python {pb.get('count')}/{pb.get('low_confidence')} "
                             f"vs spike {sb.get('count')}/{sb.get('low_confidence')}")
            if pb.get("query") != sb.get("query"):
                diffs.append(f"query: python {pb.get('query')!r} vs spike {sb.get('query')!r}")
        else:
            diffs += diff_shape(shape(pb), shape(sb))
            # Error bodies: compare values too, except free wording on 400/500.
            if ps >= 400 and ps == ss and ps not in (400, 500):
                if pb != sb:
                    diffs.append(f"body: python {pb} vs spike {sb}")
            for k in set(pb) & set(sb):
                if k not in FREE_VALUES and k != "error" and not isinstance(pb[k], (dict, list)) \
                        and pb[k] != sb[k] and not path.startswith("/health"):
                    diffs.append(f"value {k}: python {pb[k]!r} vs spike {sb[k]!r}")
            if path.startswith("/health"):
                for k in ("status", "schema", "storage", "auth", "bank", "persist_errors", "db"):
                    if pb.get(k) != sb.get(k):
                        diffs.append(f"health {k}: python {pb.get(k)!r} vs spike {sb.get(k)!r}")
        rows.append({"case": name, "method": method, "python_status": ps, "spike_status": ss,
                     "diffs": diffs, "declared_omissions": declared})
    return rows


def kendall_tau_b(a, b):
    """Tau-b over items ranked by both lists (ranks taken within each list)."""
    common = [x for x in a if x in b]
    n = len(common)
    if n < 2:
        return None
    ra = {x: i for i, x in enumerate(a)}
    rb = {x: i for i, x in enumerate(b)}
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            x, y = common[i], common[j]
            s = (ra[x] - ra[y]) * (rb[x] - rb[y])
            conc += s > 0
            disc += s < 0
    return (conc - disc) / (n * (n - 1) / 2)


def run_ranking(pp, sp, t, queries, extra):
    auth = {"Authorization": f"Bearer {t['T_DEFAULT']}"}
    rows = []
    for qtext in queries:
        path = "/api/search?" + urllib.parse.urlencode(
            {"q": qtext, "top_k": 8, "disable_recency_boost": "true", **extra})
        _, _, pb = call(pp, "GET", path, auth)
        t0 = time.perf_counter()
        _, _, sb = call(sp, "GET", path, auth)
        spike_ms = (time.perf_counter() - t0) * 1000
        pi = [e["id"] for e in pb["entries"]]
        si = [e["id"] for e in sb["entries"]]
        denom = max(len(pi), len(si)) or 1
        overlap = len(set(pi) & set(si)) / denom if (pi or si) else 1.0
        pscore = {e["id"]: e["score"] for e in pb["entries"]}
        sscore = {e["id"]: e["score"] for e in sb["entries"]}
        common = set(pscore) & set(sscore)
        rows.append({"query": qtext, "python_ids": pi, "spike_ids": si, "overlap": round(overlap, 4),
                     "kendall_tau_b": kendall_tau_b(pi, si),
                     "max_abs_score_delta": max((abs(pscore[i] - sscore[i]) for i in common), default=None),
                     "spike_ms": round(spike_ms, 1)})
    ov = [r["overlap"] for r in rows]
    taus = [r["kendall_tau_b"] for r in rows if r["kendall_tau_b"] is not None]
    return {"params": {"top_k": 8, "disable_recency_boost": True, **extra},
            "mean_overlap": round(sum(ov) / len(ov), 4), "min_overlap": min(ov),
            "mean_kendall_tau_b": round(sum(taus) / len(taus), 4) if taus else None,
            "queries_below_0_75": [r["query"] for r in rows if r["overlap"] < 0.75],
            "per_query": rows}


def main():
    pp, sp, tok_path, out = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
    t = tokens(tok_path)
    queries = json.loads((HERE / "queries.json").read_text(encoding="utf-8"))
    contract = run_contract(pp, sp, t)
    ranking = {"dense": run_ranking(pp, sp, t, queries, {"bm25": "false"})}
    if "--with-default" in sys.argv:
        ranking["default"] = run_ranking(pp, sp, t, queries, {})
    result = {"contract": contract,
              "contract_cases": len(contract),
              "contract_diff_cases": sum(1 for r in contract if r["diffs"]),
              "ranking": ranking}
    Path(out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("contract cases", len(contract), "with diffs", result["contract_diff_cases"])
    for r in contract:
        if r["diffs"]:
            print(" ", r["case"], r["diffs"])
    for k, v in ranking.items():
        print(k, "mean overlap", v["mean_overlap"], "min", v["min_overlap"], "tau", v["mean_kendall_tau_b"],
              "below 0.75:", v["queries_below_0_75"])


if __name__ == "__main__":
    main()
