"""Parity check for the Rust cross-encoder reranker (``src/read/rerank.rs``)
against the Python oracle (``pseudolife_memory/memory/reranker.py`` on
sentence-transformers' ``CrossEncoder``).

usage:
  python w2d_rerank_check.py --model-dir DIR [--keep DIR]

``DIR`` (or ``W2D_RERANK_MODEL_DIR``) holds the optimum text-classification
ONNX export of ``cross-encoder/ms-marco-MiniLM-L-6-v2`` (``model.onnx``,
``tokenizer.json``). ``ORT_DYLIB_PATH`` must name the ONNX Runtime library
for the Rust side. Python loads the model from the Hugging Face cache (set
``HF_HUB_OFFLINE=1``). ``CARGO_TARGET_DIR`` is passed through to cargo.

Per case it compares the raw logits (Rust ``Reranker::logits`` against
``CrossEncoder.predict``), the squashed scores (``Reranker::rerank`` against
``CrossEncoderReranker.rerank``) and the ranked candidate order (by text, so
identical candidates cannot flip it). Exit 1 when any order differs, any
rerank result differs in length, or the max sigmoid diff exceeds 1e-4.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SIGMOID_TOL = 1e-4

LOREM = (
    "Postgres stores the memory bank in a single database, and the daemon owns "
    "every write through one process. "
)
DOCS = [
    "The cross-encoder rescoring runs only when the pool fits the top-n budget.",
    "Paris is the capital and most populous city of France.",
    "The bi-encoder embeds the query and each passage independently.",
    "Bananas are an excellent source of potassium.",
    "To deploy, run pseudolife-mcp update; it backs up the bank first.",
    "The hybrid logical clock orders supersession across machines.",
    "A stale slot means re-verify at the source before acting.",
    "Water boils at 100 degrees Celsius at sea level.",
]


def build_cases() -> list[dict]:
    cases: list[dict] = []
    queries = [
        "when does the reranker run",
        "capital of France",
        "how do I deploy an update",
        "what orders supersession",
        "potassium rich fruit",
        "boiling point of water",
        "what does stale mean for a slot",
        "bi-encoder vs cross-encoder",
    ]
    # 1-8: each query against the shared doc pool (short candidates).
    for q in queries:
        cases.append({"query": q, "candidates": list(DOCS)})
    # 9-12: single candidate, two candidates, reversed pool, repeated pool.
    cases.append({"query": "capital of France", "candidates": [DOCS[1]]})
    cases.append({"query": "capital of France", "candidates": [DOCS[3], DOCS[1]]})
    cases.append({"query": "deploy", "candidates": list(reversed(DOCS))})
    cases.append({"query": "clock", "candidates": DOCS + DOCS})
    # 13-16: identical candidates.
    cases.append({"query": "France", "candidates": [DOCS[1]] * 4})
    cases.append({"query": "France", "candidates": [DOCS[1], DOCS[3], DOCS[1], DOCS[3]]})
    cases.append({"query": "x", "candidates": ["same", "same", "different text here"]})
    cases.append({"query": "bank backup", "candidates": [DOCS[4], DOCS[4], DOCS[0]]})
    # 17-21: long candidates; one far over 512 tokens so truncation matters.
    long_doc = LOREM * 60  # ~1200 tokens
    cases.append({"query": "who owns writes", "candidates": [long_doc, DOCS[0], DOCS[4]]})
    cases.append({"query": "who owns writes", "candidates": [long_doc]})
    cases.append({
        "query": "where is the answer",
        "candidates": [LOREM * 40 + " The answer is forty-two.", "The answer is forty-two." + LOREM * 40],
    })
    cases.append({"query": "postgres", "candidates": [LOREM * k for k in (1, 3, 10, 25, 50)]})
    cases.append({"query": "postgres daemon", "candidates": [LOREM * 5, LOREM * 5 + "Bananas."]})
    # 22-24: long query (longer than the candidate), and both sides long so
    # longest_first trims both.
    long_q = "Please explain in detail " + " ".join(DOCS)
    cases.append({"query": long_q, "candidates": ["Paris", "bananas", "clock"]})
    cases.append({"query": long_q * 10, "candidates": [long_doc, DOCS[1]]})
    cases.append({"query": (LOREM * 30), "candidates": [LOREM * 30, DOCS[2]]})
    # 25-30: unicode.
    cases.append({"query": "café crème brûlée", "candidates": ["Cafe creme brulee recipe", "Café au lait", "Tea"]})
    cases.append({"query": "東京の天気", "candidates": ["東京は晴れです", "大阪は雨です", "Tokyo weather is sunny"]})
    cases.append({"query": "emoji 🚀 launch", "candidates": ["The rocket 🚀 launched today", "No emoji here", "🎉🎉🎉"]})
    cases.append({"query": "Ünïcödé NFD é", "candidates": ["unicode nfd é", "ascii only", "é́"]})
    cases.append({"query": "Привет мир", "candidates": ["Hello world", "Привет, мир!", "Мир"]})
    cases.append({"query": "zero​width", "candidates": ["zero width", "zerowidth", "zero​width"]})
    # 31-34: whitespace / punctuation / near-empty candidates.
    cases.append({"query": "  padded query  ", "candidates": ["", " ", "padded"]})
    cases.append({"query": "?!", "candidates": ["...", "!!!", "question"]})
    cases.append({"query": "numbers 12345", "candidates": ["12345", "1 2 3 4 5", "twelve thousand"]})
    cases.append({"query": "tab\tnewline\n", "candidates": ["tab newline", "line\r\nbreak", "\t\t"]})
    # 35-36: more than one 32-pair batch, with mixed lengths.
    many = [f"{DOCS[i % len(DOCS)]} {'extra ' * (i % 7)}item {i}" for i in range(45)]
    cases.append({"query": "reranker budget", "candidates": many})
    cases.append({"query": "capital", "candidates": [LOREM * (1 + i % 9) + DOCS[i % 8] for i in range(40)]})
    # 37-38: blank queries: rerank() must return [] (logits still compared raw).
    cases.append({"query": "   ", "candidates": [DOCS[0], DOCS[1]]})
    cases.append({"query": "　\t\n", "candidates": [DOCS[0]]})
    # 39: empty candidates.
    cases.append({"query": "anything", "candidates": []})
    # 40: mixed realistic pool, CMS-sized (20).
    pool = [f"{d} (note {i})" for i, d in enumerate(DOCS * 3)][:20]
    cases.append({"query": "how is the memory bank deployed and backed up", "candidates": pool})
    return cases


def python_side(cases: list[dict]) -> list[dict]:
    sys.path.insert(0, str(REPO))
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    rr = CrossEncoderReranker()
    if not rr._ensure_loaded():
        raise SystemExit("python: cross-encoder failed to load")
    model = rr._model
    out = []
    for c in cases:
        pairs = [(c["query"], d) for d in c["candidates"]]
        logits = [float(x) for x in model.predict(pairs)] if pairs else []
        out.append({"logits": logits, "rerank": rr.rerank(c["query"], c["candidates"])})
    return out


def rust_side(cases_path: Path, out_path: Path, model_dir: str) -> list[dict]:
    env = dict(os.environ)
    env["W2D_RERANK_CASES"] = str(cases_path)
    env["W2D_RERANK_OUT"] = str(out_path)
    env["W2D_RERANK_MODEL_DIR"] = model_dir
    if not env.get("ORT_DYLIB_PATH"):
        raise SystemExit("ORT_DYLIB_PATH is not set")
    cmd = ["cargo", "test", "-p", "pseudolife-daemon", "-j", "2", "--bin", "pseudolife-daemon",
           "read::rerank::tests::rerank_dump", "--", "--ignored", "--exact"]
    proc = subprocess.run(cmd, cwd=REPO / "rust", env=env)
    if proc.returncode != 0:
        raise SystemExit(f"rust: cargo test exited {proc.returncode}")
    return json.loads(out_path.read_text(encoding="utf-8"))


def ranked(cands: list[str], scores: list[float]) -> list[str]:
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    return [cands[i] for i in order]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=os.environ.get("W2D_RERANK_MODEL_DIR"))
    ap.add_argument("--keep", help="directory to keep cases/py/rust JSON in")
    args = ap.parse_args()
    if not args.model_dir:
        ap.error("--model-dir or W2D_RERANK_MODEL_DIR is required")

    cases = build_cases()
    work = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="w2d-rerank-"))
    work.mkdir(parents=True, exist_ok=True)
    cases_path = work / "cases.json"
    cases_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    py = python_side(cases)
    (work / "python.json").write_text(json.dumps(py), encoding="utf-8")
    rs = rust_side(cases_path, work / "rust.json", args.model_dir)

    max_logit = 0.0
    max_sig = 0.0
    failures: list[str] = []
    order_ok = 0
    for n, (c, p, r) in enumerate(zip(cases, py, rs, strict=True), 1):
        if len(p["logits"]) != len(r["logits"]):
            failures.append(f"case {n}: logit count {len(p['logits'])} vs {len(r['logits'])}")
            continue
        if len(p["rerank"]) != len(r["rerank"]):
            failures.append(f"case {n}: rerank count {len(p['rerank'])} vs {len(r['rerank'])}")
            continue
        dl = max((abs(a - b) for a, b in zip(p["logits"], r["logits"])), default=0.0)
        ds = max((abs(a - b) for a, b in zip(p["rerank"], r["rerank"])), default=0.0)
        max_logit = max(max_logit, dl)
        max_sig = max(max_sig, ds)
        if ranked(c["candidates"], p["logits"]) != ranked(c["candidates"], r["logits"]):
            failures.append(f"case {n}: candidate order differs (query {c['query'][:40]!r})")
        else:
            order_ok += 1
        print(f"case {n:2d}: {len(c['candidates']):2d} cands  max|dlogit|={dl:.2e}  "
              f"max|dsigmoid|={ds:.2e}  rerank_len={len(p['rerank'])}")

    print(f"\ncases: {len(cases)}  order agreement: {order_ok}/{len(cases)}")
    print(f"max |logit diff|:   {max_logit:.3e}")
    print(f"max |sigmoid diff|: {max_sig:.3e}  (tolerance {SIGMOID_TOL})")
    if max_sig > SIGMOID_TOL:
        failures.append(f"max sigmoid diff {max_sig:.3e} > {SIGMOID_TOL}")
    for f in failures:
        print("FAIL", f)
    print("RESULT:", "FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
