"""Blind judged preference over the live-bank flat-vs-banded replay.

Edge case 5, escalation stage (preregistered G-E5): the read-path replay
found 87.6% top-6 divergence on real recorded queries, far over the 20%
screen, so the divergent selections get a blind position-swapped LLM
judge. Runs ONLY against the reproducible bench server (Start-Qwen q8_0
config — verify before launching; the fast TBQ4_0 config flips ~7% of
verdicts and invalidates the run).

Each top-k-divergent query is judged in both presentation orders
(banded-first and flat-first) × ``--replicates`` passes at temperature 0
with the prompt cache pinned off. The judge sees anonymous "Context X" /
"Context Y" and never learns which arm is which. Per-query preference =
banded-win fraction over all (order, replicate) verdicts; the headline is
the mean preference vs the 0.5 null with a paired sign-flip permutation
test. The aggregate artifact carries no query text (private corpus).

    python evals/live_replay_judge.py --limit 5     # smoke
    python evals/live_replay_judge.py               # full divergent set
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RESULTS = Path(__file__).resolve().parent / "results"
DETAIL = RESULTS / "abl25-e5-live-replay-detail.jsonl"
CORPUS = RESULTS / "abl25-e5-queries.jsonl"
ENDPOINT = "http://127.0.0.1:1234/v1/chat/completions"

PROMPT = """\
A memory system retrieved two candidate context sets for an agent's query.
Judge which set would better help the agent — more relevant, more
complete, less noise. The sets may overlap.

Query:
{query}

Context X:
{x}

Context Y:
{y}

Reply with exactly one token: X, Y, or tie."""


def _chat(model: str, prompt: str, timeout: float = 120.0) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 2048,
        # Qwen3.8 servers default thinking ON with no budget cap (the 3.6-era
        # --reasoning-budget flag is retired); unpinned, the reasoning trace
        # consumes the whole budget and _verdict gets empty content.
        "chat_template_kwargs": {"enable_thinking": False},
        # Warm-cache determinism pin (2026-08-09 probe): identical
        # temperature-0 inputs drift on a warm llama-server cache.
        "cache_prompt": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.load(resp)
    return out["choices"][0]["message"]["content"].strip()


def _verdict(raw: str) -> float | None:
    """1.0 = X wins, 0.0 = Y wins, 0.5 = tie, None = unparseable."""
    lines = raw.strip().splitlines()
    if not lines:      # reasoning-only response with empty final content
        return None
    tail = lines[-1].strip().strip(".").lower()
    if tail == "x":
        return 1.0
    if tail == "y":
        return 0.0
    if tail == "tie":
        return 0.5
    return None


def _fmt(entries: list[str]) -> str:
    return "\n\n".join(f"- {e}" for e in entries) if entries else "(empty)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="qwen")
    ap.add_argument("--replicates", type=int, default=5)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out",
                    default=str(RESULTS / "abl25-e5-judged-preference.json"))
    args = ap.parse_args(argv)

    queries = [json.loads(x) for x in
               CORPUS.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [json.loads(x) for x in
            DETAIL.read_text(encoding="utf-8").splitlines() if x.strip()]
    divergent = [r for r in rows if r.get("divergent_topk")
                 and (r.get("a") or r.get("b"))]
    if args.limit:
        divergent = divergent[: args.limit]
    if not divergent:
        sys.exit("no divergent rows with selections — rerun the replay "
                 "with --dump-selections")

    from band_ablation import _paired_permutation_p  # noqa: PLC0415

    per_query, unparsed = [], 0
    consecutive_failures = 0
    for n, r in enumerate(divergent):
        q = queries[r["i"]]["query"]
        votes = []          # each vote: banded preference in [0, 1]
        for order in ("ab", "ba"):
            x, y = (r["a"], r["b"]) if order == "ab" else (r["b"], r["a"])
            prompt = PROMPT.format(query=q, x=_fmt(x), y=_fmt(y))
            for _rep in range(args.replicates):
                try:
                    v = _verdict(_chat(args.model, prompt))
                    consecutive_failures = 0
                except Exception as exc:  # noqa: BLE001 — one bad call
                    consecutive_failures += 1
                    print(f"  call failed ({exc}) — counted unparseable",
                          flush=True)
                    # A dead server must ABORT, not degrade every verdict
                    # to a fake tie (the 2026-08-15 OOM run wrote a
                    # 177-tie artifact exactly that way).
                    if consecutive_failures >= 5:
                        sys.exit("5 consecutive call failures — server "
                                 "down? Aborting without an artifact.")
                    v = None
                if v is None:
                    unparsed += 1
                    continue
                votes.append(v if order == "ab" else 1.0 - v)
        pref = sum(votes) / len(votes) if votes else 0.5
        per_query.append({"i": r["i"], "banded_pref": round(pref, 3),
                          "n_votes": len(votes)})
        if (n + 1) % 10 == 0:
            running = sum(p["banded_pref"] for p in per_query) / len(per_query)
            print(f"[{n + 1}/{len(divergent)}] mean banded pref "
                  f"{running:.3f}", flush=True)

    if unparsed > 0.2 * len(per_query) * 2 * args.replicates:
        sys.exit(f"{unparsed} unparseable verdicts (>20%) — refusing to "
                 "write an artifact from a degraded run")
    prefs = [p["banded_pref"] for p in per_query]
    mean_pref = sum(prefs) / len(prefs)
    # Sign-flip test on (pref - 0.5): does either arm win systematically?
    p_val = _paired_permutation_p([x - 0.5 for x in prefs])
    agg = {
        "n_judged": len(per_query),
        "replicates": args.replicates,
        "orders": 2,
        "unparseable_verdicts": unparsed,
        "mean_banded_preference": round(mean_pref, 4),
        "p_vs_null_paired_perm_10k_seed0": round(p_val, 4),
        "banded_wins": sum(1 for x in prefs if x > 0.5),
        "flat_wins": sum(1 for x in prefs if x < 0.5),
        "ties": sum(1 for x in prefs if x == 0.5),
        "per_query": per_query,   # indices only — no query text
        "corpus_note": "202 real transcript queries; divergent top-6 "
                       "subset judged; texts withheld (private)",
    }
    Path(args.out).write_text(json.dumps(agg, indent=2), encoding="utf-8")
    print(json.dumps({k: agg[k] for k in
                      ("n_judged", "mean_banded_preference",
                       "p_vs_null_paired_perm_10k_seed0",
                       "banded_wins", "flat_wins", "ties")}, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
