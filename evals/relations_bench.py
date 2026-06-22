#!/usr/bin/env python
"""Relation-extraction benchmark (dev-only) for GAM #2 graph-from-text.

Decides the SHIPPED extraction shape — does the dream extract relation triples in
ONE combined call ({claims, relations}) or a SEPARATE relations-only call? — by
scoring both prompt variants against a hand-annotated gold corpus on the actual
shipped Gemma-2B floor (and an optional Qwen ceiling).

Per variant x reachable rung it reports:
  * precision / recall / F1  — triple match = src & dst entities match after
    norm_name AND the relation matches after closed-vocab resolution.
  * pair_recall              — entity-pair recall ignoring the relation label
    (isolates entity extraction from relation typing).
  * related_to_share         — fraction of kept edges that fell back to
    related-to (the signal for whether to expand the registry later).
  * parse_fail / latency     — JSON robustness + wall-time per snippet.

No memory bank needed — calls the OpenAI-compatible endpoint and scores directly.
Forces CPU + offline so it never touches the 4090 except an explicit Qwen call.

Usage:
  HF_HUB_OFFLINE=1 python evals/relations_bench.py            # all reachable rungs
  python evals/relations_bench.py --rungs gemma-e2b           # one rung
"""
from __future__ import annotations

import os

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pseudolife_memory.graph import norm_name, resolve_relation  # noqa: E402

# Closed vocabulary for graph-from-text (the infra builtins + the catch-all;
# prefers/avoids are lesson-only and excluded). (name, description) for the prompt.
RELATIONS: list[tuple[str, str]] = [
    ("depends-on", "src requires dst to function"),
    ("part-of", "src is a component of dst"),
    ("runs-on", "src executes on host/platform dst"),
    ("hosts", "src is the host/platform for dst"),
    ("uses", "src makes use of dst"),
    ("configures", "src sets configuration for dst"),
    ("stores-data-in", "src persists its data in dst"),
    ("related-to", "generic association when nothing else fits"),
]
KNOWN = [r[0] for r in RELATIONS]

RUNGS: dict[str, dict] = {
    "gemma-e2b": {"label": "Gemma 4 E2B (shipped CPU floor)",
                  "base_url": os.environ.get("PSEUDOLIFE_BENCH_GEMMA_URL",
                                             "http://127.0.0.1:8081/v1"),
                  "model": "extractor"},
    "qwen-a3b": {"label": "Qwen3.6-35B-A3B (homelab)",
                 "base_url": "http://192.168.0.130:1236/v1",
                 "model": "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"},
    "qwen-27b": {"label": "Qwen3.6-27B (4090 ceiling)",
                 "base_url": "http://192.168.0.10:1234/v1",
                 "model": "Qwen3.6-27B-UD-Q4_K_XL.gguf"},
}

# --- Gold corpus: realistically-phrased snippets + their gold triples (in vocab).
#     Includes no-relation snippets (precision/hallucination check) and ones that
#     should fall back to related-to.
GOLD: list[dict] = [
    {"text": "The checkout-service runs on host-1, and it stores its data in the "
             "payments-db. It also depends on the auth-service for login.",
     "triples": [("checkout-service", "runs-on", "host-1"),
                 ("checkout-service", "stores-data-in", "payments-db"),
                 ("checkout-service", "depends-on", "auth-service")]},
    {"text": "We migrated the analytics pipeline onto the new spark-cluster; "
             "the dashboard uses the analytics pipeline's output.",
     "triples": [("analytics-pipeline", "runs-on", "spark-cluster"),
                 ("dashboard", "uses", "analytics-pipeline")]},
    {"text": "The embedder module is part of the memory service, which persists "
             "everything in postgres.",
     "triples": [("embedder", "part-of", "memory-service"),
                 ("memory-service", "stores-data-in", "postgres")]},
    {"text": "nginx sits in front of and configures the web-app's routing.",
     "triples": [("nginx", "configures", "web-app")]},
    {"text": "The daemon is hosted on the homelab box and depends on redis for "
             "its job queue.",
     "triples": [("daemon", "runs-on", "homelab-box"),
                 ("daemon", "depends-on", "redis")]},
    {"text": "Grafana talks to Prometheus, which scrapes the api-gateway.",
     "triples": [("grafana", "related-to", "prometheus"),
                 ("prometheus", "related-to", "api-gateway")]},
    {"text": "I think the new UI looks much cleaner than last quarter.",
     "triples": []},
    {"text": "Remember to buy milk on the way home tonight.",
     "triples": []},
    {"text": "The billing-worker uses the stripe-sdk and runs on the worker-pool; "
             "the worker-pool is part of the prod-cluster.",
     "triples": [("billing-worker", "uses", "stripe-sdk"),
                 ("billing-worker", "runs-on", "worker-pool"),
                 ("worker-pool", "part-of", "prod-cluster")]},
    {"text": "Our ETL job configures the warehouse schema and stores its "
             "checkpoints in s3.",
     "triples": [("etl-job", "configures", "warehouse"),
                 ("etl-job", "stores-data-in", "s3")]},
    {"text": "The mobile-app depends on the graphql-gateway, and the "
             "graphql-gateway depends on the user-service.",
     "triples": [("mobile-app", "depends-on", "graphql-gateway"),
                 ("graphql-gateway", "depends-on", "user-service")]},
    {"text": "Kafka is wired up with the ingestion-service somehow in our stack.",
     "triples": [("kafka", "related-to", "ingestion-service")]},
]

_SYS_SEPARATE = (
    "You extract durable RELATIONSHIPS between named entities from notes, as JSON: "
    '{"relations":[{"src":..,"relation":..,"dst":..}]}. Use ONLY these relation '
    "names:\n" + "\n".join(f"- {n}: {d}" for n, d in RELATIONS) + "\n"
    "If a real connection fits none of the specific ones, use 'related-to'. "
    "src and dst are entity names (services, hosts, tools, components). Skip "
    "opinions, chit-chat, and anything with no entity-to-entity relationship. "
    'Return {"relations":[]} if nothing qualifies.'
)

_SYS_COMBINED = (
    "You consolidate notes into canonical FACTS and entity RELATIONSHIPS, as JSON: "
    '{"claims":[{"entity":..,"attribute":..,"value":..}],'
    '"relations":[{"src":..,"relation":..,"dst":..}]}. '
    "For relations use ONLY these names:\n"
    + "\n".join(f"- {n}: {d}" for n, d in RELATIONS) + "\n"
    "If a real connection fits none of the specific ones, use 'related-to'. Skip "
    'opinions and chit-chat. Return empty arrays if nothing qualifies.'
)


def call(base_url: str, model: str, system: str, text: str,
         timeout: float) -> tuple[list[dict], bool]:
    """Return (relations, parse_ok). relations = list of {src,relation,dst}."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": text}],
        "response_format": {"type": "json_object"},
        "max_tokens": 1024, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(f"{base_url}/chat/completions", data=body,
                                 headers={"content-type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    content = data["choices"][0]["message"]["content"] or ""
    s, e = content.find("{"), content.rfind("}")
    if s != -1 and e > s:
        content = content[s:e + 1]
    try:
        parsed = json.loads(content)
    except Exception:  # noqa: BLE001
        return [], False
    raw = parsed.get("relations", []) if isinstance(parsed, dict) else []
    out = []
    for r in raw if isinstance(raw, list) else []:
        if isinstance(r, dict) and r.get("src") and r.get("relation") and r.get("dst"):
            out.append({"src": str(r["src"]), "relation": str(r["relation"]),
                        "dst": str(r["dst"])})
    return out, True


def normalize_triple(src: str, relation: str, dst: str) -> tuple[str, str, str] | None:
    """Map a raw triple to (src_norm, resolved_relation, dst_norm); related-to
    fallback; drop self-loops."""
    s, d = norm_name(src), norm_name(dst)
    if not s or not d or s == d:
        return None
    name, _ = resolve_relation(KNOWN, relation)
    return (s, name or "related-to", d)


def run_variant(rung: dict, system: str, timeout: float) -> dict:
    tp = pred = gold = 0
    pair_tp = 0
    related_to = 0
    parse_fail = 0
    t0 = time.time()
    for item in GOLD:
        gold_set = {(norm_name(a), rel, norm_name(b)) for a, rel, b in item["triples"]}
        gold_pairs = {(norm_name(a), norm_name(b)) for a, rel, b in item["triples"]}
        gold += len(gold_set)
        try:
            rels, ok = call(rung["base_url"], rung["model"], system, item["text"], timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"    ! call failed: {exc}", file=sys.stderr)
            parse_fail += 1
            continue
        if not ok:
            parse_fail += 1
            continue
        seen = set()
        for r in rels:
            t = normalize_triple(r["src"], r["relation"], r["dst"])
            if t is None or t in seen:
                continue
            seen.add(t)
            pred += 1
            if t[1] == "related-to":
                related_to += 1
            if t in gold_set:
                tp += 1
            if (t[0], t[2]) in gold_pairs:
                pair_tp += 1
    secs = time.time() - t0
    prec = tp / pred if pred else 0.0
    rec = tp / gold if gold else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"tp": tp, "pred": pred, "gold": gold, "precision": round(prec, 3),
            "recall": round(rec, 3), "f1": round(f1, 3),
            "pair_recall": round(pair_tp / gold, 3) if gold else 0.0,
            "related_to_share": round(related_to / pred, 3) if pred else 0.0,
            "parse_fail": parse_fail, "seconds": round(secs, 1)}


def reachable(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url + "/models", timeout=4):
            return True
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rungs", nargs="*", default=list(RUNGS))
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    print(f"Relation-extraction bench — {len(GOLD)} snippets, "
          f"{sum(len(g['triples']) for g in GOLD)} gold triples\n")
    rows = []
    for name in args.rungs:
        rung = RUNGS[name]
        if not reachable(rung["base_url"]):
            print(f"[{name}] {rung['label']}: UNREACHABLE ({rung['base_url']}) — skip")
            continue
        for variant, system in (("separate", _SYS_SEPARATE), ("combined", _SYS_COMBINED)):
            print(f"[{name}/{variant}] running …")
            m = run_variant(rung, system, args.timeout)
            m["rung"] = name
            m["variant"] = variant
            rows.append(m)
            print(f"    P={m['precision']} R={m['recall']} F1={m['f1']} "
                  f"pairR={m['pair_recall']} related-to={m['related_to_share']} "
                  f"parse_fail={m['parse_fail']} {m['seconds']}s")

    if not rows:
        print("\nNo reachable rungs.")
        return
    print("\n=== summary ===")
    print(f"{'rung':<12}{'variant':<10}{'P':>6}{'R':>6}{'F1':>6}"
          f"{'pairR':>7}{'rel-to':>8}{'pf':>4}{'sec':>7}")
    for m in rows:
        print(f"{m['rung']:<12}{m['variant']:<10}{m['precision']:>6}{m['recall']:>6}"
              f"{m['f1']:>6}{m['pair_recall']:>7}{m['related_to_share']:>8}"
              f"{m['parse_fail']:>4}{m['seconds']:>7}")
    gem = [m for m in rows if m["rung"] == "gemma-e2b"]
    if gem:
        win = max(gem, key=lambda m: m["f1"])
        print(f"\nVERDICT (Gemma floor): '{win['variant']}' wins "
              f"(F1={win['f1']} vs "
              f"{[ (m['variant'], m['f1']) for m in gem if m is not win ]}).")


if __name__ == "__main__":
    main()
