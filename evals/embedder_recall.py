"""Compare embedding backbones on OUR corpus before swapping the backbone.

Swapping the bi-encoder is a schema migration: the pgvector columns are
declared ``vector(384)`` in four tables, every stored row must be
re-embedded, and every committed eval artifact's embeddings stop being
comparable. That is too much to spend on a literature claim, so this
measures the thing the swap is supposed to buy — can retrieval find the
answer — on the LongMemEval `s` questions we actually evaluate against.

Metric: recall@k of the turns LongMemEval marks ``has_answer``, ranking
every haystack turn of a question by cosine to the question text. Pure
retrieval, no reader, no judge. Runs on GPU when torch sees one; quality
is device-independent, so bench on GPU and deploy on CPU.

Candidates live in ``CANDIDATES`` with their card-verbatim query/passage
prefixes — instruction-tuned embedders swing on exact wording, so an arm
run with the wrong prefix understates that model and the comparison stops
being fair. The first ``--arms`` entry is the paired-McNemar baseline.

    python evals/embedder_recall.py --questions 30
    python evals/embedder_recall.py --arms minilm bge-base-prefix qwen3-0.6b \
        --out evals/results/embedder-recall-<tag>.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parent / "data"
RESULTS = Path(__file__).resolve().parent / "results"
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
QWEN3_QUERY_PREFIX = ("Instruct: Given a web search query, retrieve relevant "
                      "passages that answer the query\nQuery:")

# key -> (label, repo, query_prefix, passage_prefix). Prefixes are pinned
# VERBATIM from each model card because instruction-tuned embedders swing
# on exact wording — an arm run with the wrong (or no) prefix understates
# that model and the comparison stops being fair. The committed artifact
# records the strings, not a bool, for exactly that reason.
CANDIDATES = {
    "minilm":          ("all-MiniLM-L6-v2 (shipped)",
                        "sentence-transformers/all-MiniLM-L6-v2", "", ""),
    "bge-base":        ("bge-base-en-v1.5",
                        "BAAI/bge-base-en-v1.5", "", ""),
    "bge-base-prefix": ("bge-base-en-v1.5 (query prefix)",
                        "BAAI/bge-base-en-v1.5", BGE_QUERY_PREFIX, ""),
    "bge-large":       ("bge-large-en-v1.5 (query prefix)",
                        "BAAI/bge-large-en-v1.5", BGE_QUERY_PREFIX, ""),
    "qwen3-0.6b":      ("Qwen3-Embedding-0.6B (instructed)",
                        "Qwen/Qwen3-Embedding-0.6B", QWEN3_QUERY_PREFIX, ""),
    "granite-r2":      ("granite-embedding-english-r2",
                        "ibm-granite/granite-embedding-english-r2", "", ""),
    "arctic-l-v2":     ("snowflake-arctic-embed-l-v2.0 (query prefix)",
                        "Snowflake/snowflake-arctic-embed-l-v2.0", "query: ", ""),
    "nemotron-1b":     ("Nemotron-3-Embed-1B (bf16, native 2048d)",
                        "nvidia/Nemotron-3-Embed-1B-BF16", "query: ", "passage: "),
    "nemotron-1b-d1024": ("Nemotron-3-Embed-1B (bf16, truncated 1024d)",
                          "nvidia/Nemotron-3-Embed-1B-BF16", "query: ", "passage: "),
}
# ST arms that use card-sanctioned Matryoshka truncation (slice + L2 renorm,
# handled by sentence-transformers' truncate_dim). pgvector's HNSW index caps
# at 2000 dims, so a 2048-d native model is only DEPLOYABLE truncated.
TRUNCATE_DIM = {"nemotron-1b-d1024": 1024}

# ── GGUF arms: quantized models served by llama-server ────────────────────
# Weights quantization is what these arms measure (Q4_K_M vs fp32 vectors).
# Served with --pooling last (Qwen3-Embedding is last-token pooled; GGUF
# metadata does not always carry it, and the WRONG pooling silently degrades
# recall rather than erroring). out_dim < native uses the same slice+renorm
# rule as Matryoshka. Truncation to --max-seq-length happens client-side
# with the real Qwen3 tokenizer so token budgets match the ST arms.
LLAMA_SERVER = Path.home() / "ClaudeCode" / "llama.ccp" / "llama-server.exe"
MODELS_DIR = Path(__file__).resolve().parent / "models"
GGUF_CANDIDATES = {
    "qwen3-0.6b-q8-gguf": ("Qwen3-Embedding-0.6B Q8_0 (gguf)",
                           "Qwen3-Embedding-0.6B-Q8_0.gguf", 1024, 1024,
                           QWEN3_QUERY_PREFIX, ""),
    "qwen3-4b-q4-gguf":   ("Qwen3-Embedding-4B Q4_K_M (gguf, native 2560d)",
                           "Qwen3-Embedding-4B-Q4_K_M.gguf", 2560, 2560,
                           QWEN3_QUERY_PREFIX, ""),
    "qwen3-4b-q4-d1024":  ("Qwen3-Embedding-4B Q4_K_M (gguf, truncated 1024d)",
                           "Qwen3-Embedding-4B-Q4_K_M.gguf", 2560, 1024,
                           QWEN3_QUERY_PREFIX, ""),
    "qwen3-8b-q4-d1024":  ("Qwen3-Embedding-8B Q4_K_M (gguf, truncated 1024d)",
                           "Qwen3-Embedding-8B-Q4_K_M.gguf", 4096, 1024,
                           QWEN3_QUERY_PREFIX, ""),
}


class LlamaCppEmbedder:
    """Minimal SentenceTransformer-shaped adapter over llama-server
    /v1/embeddings, so GGUF arms run through the identical harness path as
    the ST arms (same truncation budget, same normalize-then-rank)."""

    def __init__(self, gguf: Path, native_dim: int, out_dim: int,
                 port: int = 8093, max_tokens: int = 512):
        import subprocess
        import urllib.request
        self._urllib = urllib.request
        self.native_dim, self.out_dim = native_dim, out_dim
        self.max_seq_length = max_tokens
        from transformers import AutoTokenizer
        # All Qwen3-Embedding sizes share the tokenizer; used ONLY to apply
        # the same 512-token truncation budget the ST arms get.
        self._tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-Embedding-0.6B")
        self._url = f"http://127.0.0.1:{port}/v1/embeddings"
        self._proc = subprocess.Popen(
            [str(LLAMA_SERVER), "-m", str(gguf), "--embeddings",
             "--pooling", "last", "-ngl", "99", "-c", "2048",
             "-b", "2048", "-ub", "2048",
             "--host", "127.0.0.1", "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        health = f"http://127.0.0.1:{port}/health"
        for _ in range(240):
            try:
                with self._urllib.urlopen(health, timeout=2) as r:
                    if b"ok" in r.read():
                        break
            except Exception:  # noqa: BLE001 -- still loading
                time.sleep(1)
        else:
            raise RuntimeError(f"llama-server never became healthy for {gguf}")

    def _truncate(self, text: str) -> str:
        ids = self._tok(text, truncation=True,
                        max_length=self.max_seq_length)["input_ids"]
        return self._tok.decode(ids, skip_special_tokens=True)

    def encode(self, texts, batch_size=32, show_progress_bar=False,
               normalize_embeddings=True, convert_to_numpy=True):
        single = isinstance(texts, str)
        items = [texts] if single else list(texts)
        out = np.empty((len(items), self.out_dim), dtype=np.float32)
        for i in range(0, len(items), batch_size):
            chunk = [self._truncate(t) for t in items[i:i + batch_size]]
            body = json.dumps({"input": chunk}).encode()
            req = self._urllib.Request(
                self._url, data=body,
                headers={"Content-Type": "application/json"})
            with self._urllib.urlopen(req, timeout=600) as r:
                data = json.loads(r.read())["data"]
            vecs = np.asarray([d["embedding"] for d in data],
                              dtype=np.float32)[:, :self.out_dim]
            # slice + L2 renorm == Matryoshka truncation; also renormalizes
            # full-dim vectors, which is a no-op on the already-unit output
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
            out[i:i + len(chunk)] = vecs
        return out[0] if single else out

    def get_sentence_embedding_dimension(self) -> int:
        return self.out_dim

    def close(self) -> None:
        if getattr(self, "_proc", None) is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                self._proc.kill()
            self._proc = None


def load_questions(n: int) -> list[dict]:
    src = DATA / "longmemeval_s_cleaned.json"
    rows = json.loads(src.read_text(encoding="utf-8"))
    out = []
    for q in rows:
        turns, gold = [], []
        for date, session in zip(q["haystack_dates"], q["haystack_sessions"]):
            for t in session:
                content = (t.get("content") or "").strip()
                if not content:
                    continue
                turns.append(f"[{date}] {t['role']}: {content}")
                gold.append(str(t.get("has_answer", "False")).lower() == "true")
        if any(gold):
            out.append({"qid": q["question_id"], "question": q["question"],
                        "turns": turns, "gold": np.array(gold)})
        if len(out) >= n:
            break
    return out


def recall_at(model, questions, ks, query_prefix="", passage_prefix="",
              batch_size=32):
    """Returns (recall_by_k, n_gold, per_gold_hits).

    ``per_gold_hits[k]`` is one bool per gold turn, in a stable order, so
    two arms can be compared with a PAIRED test. Report hits alongside
    rates: gold turns are scarce (roughly one per question), so a recall
    gap of a few points over a small slice can be a single document, and
    the rate alone hides that.
    """
    hits = {k: [] for k in ks}
    total = 0
    for q in questions:
        docs = model.encode([passage_prefix + t for t in q["turns"]],
                            batch_size=batch_size, show_progress_bar=False,
                            normalize_embeddings=True, convert_to_numpy=True)
        qv = model.encode([query_prefix + q["question"]],
                          normalize_embeddings=True, convert_to_numpy=True)[0]
        order = np.argsort(-(docs @ qv))
        gold_idx = sorted(np.where(q["gold"])[0].tolist())
        total += len(gold_idx)
        for k in ks:
            topk = set(order[:k].tolist())
            hits[k].extend(g in topk for g in gold_idx)
    return ({k: sum(hits[k]) / total for k in ks}, total,
            {k: hits[k] for k in ks})


def mcnemar(a: list[bool], b: list[bool]) -> tuple[int, int, float]:
    """Exact two-sided McNemar on paired hit vectors -> (b_only, a_only, p)."""
    from math import comb
    only_b = sum(1 for x, y in zip(a, b) if y and not x)
    only_a = sum(1 for x, y in zip(a, b) if x and not y)
    n = only_a + only_b
    if n == 0:
        return only_b, only_a, 1.0
    lo = min(only_a, only_b)
    tail = sum(comb(n, i) for i in range(lo + 1)) / (2 ** n)
    return only_b, only_a, min(1.0, 2 * tail)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--questions", type=int, default=500)
    ap.add_argument("--arms", nargs="+", default=["minilm", "bge-base"],
                    choices=sorted(CANDIDATES) + sorted(GGUF_CANDIDATES),
                    help="candidate keys to run; the FIRST is the paired-test "
                         "baseline")
    ap.add_argument("--with-prefix", action="store_true",
                    help="legacy alias: append the bge-base-prefix arm")
    ap.add_argument("--ks", type=int, nargs="+", default=[5, 10, 20])
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--max-seq-length", type=int, default=512,
                    help="cap every arm's tokenizer truncation. Memory safety "
                         "AND fairness: a 32k-context arm reading whole long "
                         "turns that 512-token arms cannot see is measuring "
                         "context length, not embedding quality -- and one "
                         "long batch at 32k OOMs a 24GB GPU")
    ap.add_argument("--out", type=Path,
                    default=RESULTS / "embedder-recall-comparison.json",
                    help="rerun rule: pass a TAGGED path; never overwrite the "
                         "canonical artifact")
    args = ap.parse_args()

    from sentence_transformers import SentenceTransformer

    questions = load_questions(args.questions)
    turns = sum(len(q["turns"]) for q in questions)
    keys = list(args.arms) + (["bge-base-prefix"] if args.with_prefix
                              and "bge-base-prefix" not in args.arms else [])
    print(f"{len(questions)} questions, {turns} haystack turns; "
          f"arms: {', '.join(keys)}\n")

    def flush(rows, tests, n_gold):
        """Write after EVERY arm: a crash on arm 5 must not lose arms 1-4's
        paired hit vectors -- they only exist in-process."""
        args.out.write_text(json.dumps(
            {"questions": len(questions), "haystack_turns": turns,
             "gold_turns": n_gold, "max_seq_length": args.max_seq_length,
             "batch_size": args.batch_size,
             "arms": rows, "mcnemar_vs_shipped": tests},
            indent=2), encoding="utf-8")

    rows, hit_vectors = [], {}
    n_gold = 0
    for key in keys:
        if key in GGUF_CANDIDATES:
            label, fname, native_dim, out_dim, prefix, passage_prefix = \
                GGUF_CANDIDATES[key]
            repo = fname
            model = LlamaCppEmbedder(MODELS_DIR / fname, native_dim, out_dim,
                                     max_tokens=args.max_seq_length)
            device = "llama-server(cuda)"
        else:
            label, repo, prefix, passage_prefix = CANDIDATES[key]
            model = SentenceTransformer(repo,
                                        truncate_dim=TRUNCATE_DIM.get(key))
            model.max_seq_length = min(int(model.max_seq_length or 512),
                                       args.max_seq_length)
            device = str(getattr(model, "device", "cpu"))
        t0 = time.perf_counter()
        rec, n_gold, per_gold = recall_at(model, questions, args.ks, prefix,
                                          passage_prefix, args.batch_size)
        dt = time.perf_counter() - t0
        hit_vectors[label] = per_gold
        rows.append({"arm": label, "model": repo,
                     # exact strings, not bools: instruction-tuned embedders
                     # swing on wording, so the artifact must pin what ran
                     "query_prefix": prefix, "passage_prefix": passage_prefix,
                     "device": device,
                     "max_seq_length": int(model.max_seq_length),
                     "recall": {str(k): v for k, v in rec.items()},
                     "hits": {str(k): sum(per_gold[k]) for k in args.ks},
                     # Per-gold hit vectors (stable order): ANY pairwise
                     # McNemar is computable from the artifact post-hoc,
                     # instead of only the baseline comparisons chosen at
                     # run time. 299 bools per k is cheap; a rerun is not.
                     "per_gold_hits": {str(k): [bool(h) for h in per_gold[k]]
                                       for k in args.ks},
                     "encode_seconds": round(dt, 1),
                     "dim": model.get_sentence_embedding_dimension()})
        print(f"{label:32} " +
              "  ".join(f"R@{k}={rec[k]:.3f} ({sum(per_gold[k])}/{n_gold})"
                        for k in args.ks) +
              f"   ({dt:.0f}s, dim {rows[-1]['dim']})")
        flush(rows, [], n_gold)
        # Free the arm's VRAM before the next model loads: seven arms of
        # retained weights plus allocator cache is how a 24GB card fills.
        # GGUF arms hold a llama-server child process — terminate it.
        if hasattr(model, "close"):
            model.close()
        del model
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 -- CPU-only bench venv
            pass

    # Paired significance against the first arm. Aggregate rates over a
    # few dozen gold turns look decisive and are not.
    labels = [r["arm"] for r in rows]
    base = labels[0]
    tests = []
    print(f"\npaired McNemar vs {base} (n_gold={n_gold}):")
    for label in labels[1:]:
        for k in args.ks:
            won, lost, p = mcnemar(hit_vectors[base][k], hit_vectors[label][k])
            tests.append({"arm": label, "k": k, "gained": won, "lost": lost,
                          "p_value": p})
            print(f"  {label:32} @{k:<3} +{won} -{lost}  p={p:.3f}"
                  + ("" if p < 0.05 else "   (not significant)"))

    flush(rows, tests, n_gold)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
