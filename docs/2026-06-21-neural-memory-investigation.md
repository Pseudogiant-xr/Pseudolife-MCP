# Why the neural memory (TITANS/HOPE) doesn't earn its keep here — root-cause investigation

**Date:** 2026-06-21 · **Trigger:** F1 from the fresh-eyes review — an A/B eval showed
the MIRAS neural retrieval blend losing to plain cosine. The question this doc answers:
**is that our implementation, or are the TITANS/HOPE papers inapplicable to how we use them?**

**Answer (short):** Both, but the dominant factor is a **regime mismatch**. TITANS/HOPE
are *sequence-model* components trained *end-to-end* on a language objective; we transplanted
the *mechanism* into *offline retrieval re-ranking of frozen sentence embeddings* with a
*self-reconstruction* loss and **no training loop**. In that regime the neural memory has
nothing useful to learn beyond what the frozen embedder + cosine already encode — its
theoretical ceiling is "tie cosine," and in practice it underperforms. This is not a bug to
patch; it is the wrong tool for this job. **Recommendation: default `neural_blend_weight=0`
(pure cosine), keep the bands purely as the dream's episodic substrate, and stop billing the
neural blend as a retrieval feature.**

---

## 1. The evidence (measured, reproducible)

Harness: `evals/neural_blend_bench.py` — ingest a paraphrase-recall corpus once (training the
per-band MLPs through the real `store` path), then re-run the SAME queries on the SAME trained
state toggling only the blend weight `w`. Cortex empty, reranker/BM25/recency off, so only the
blend differs. File-mode, CPU, fixed seed; never touches the live bank.

| corpus | OFF (w=0, cosine) MRR | ON (w=0.6, shipped) MRR | PURE (w=1, MLP-only) MRR | ON − OFF |
|---|---|---|---|---|
| n=73 (real-bank scale) | **0.979** | 0.934 | 0.044 | ΔMRR −0.045 |
| n=120 | **0.944** | 0.892 | 0.024 | ΔMRR −0.052 |
| n=150 | **0.936** | 0.875 | 0.018 | ΔMRR −0.061 |

- The blend **hurts** at every scale, and the gap **widens** with more data.
- It hurts even on the **low-overlap subset** (where cosine is weakest and an associative
  memory should help most): n=150 cosine MRR 0.910 vs blend 0.842.
- **PURE (MLP-only) ranking is ≈ random** (R@1 ≈ 0.01 → 0 at scale): the memory's predicted
  query carries essentially no rankable signal on its own.
- Switching to the **`kv` objective** (the registry's "closer to attention's K→V" option)
  does **not** rescue it (ΔMRR −0.064) — it is not a one-objective-knob fix.

**Mechanism probe** — `cos(M(x), x)` per trained band ≈ **0.35–0.48** (not ~1.0, not ~0):

```
instant  n=  2 updates=120  mean cos(M(x),x)=+0.421
fast     n=  7 updates=118  mean cos(M(x),x)=+0.478
medium   n= 60 updates=111  mean cos(M(x),x)=+0.387
slow     n= 51 updates= 51  mean cos(M(x),x)=+0.350
```

So `M` is neither an identity map (which would make the blend *harmlessly redundant* with
cosine) nor pure noise — it's a **lossy, rotated reconstruction**. Blending in 60% of a
query vector that's only ~0.4-cosine-faithful drags the ranking below clean cosine.

---

## 2. What the code actually implements

- **The memory is a per-item autoencoder.** `band.update_memory` trains on
  `loss = objective.loss(M(x), x)` — predict the stored embedding *from itself*
  (`pseudolife_memory/memory/miras/band.py`). Key = value = the frozen sentence embedding.
  Default objective `l2` = `MSE(M(x), x)` (`objectives.py`).
- **No learned projections.** There is no `W_k`/`W_q`/`W_v`; the module
  (`MLP3Module`, dim→512→512→dim) maps the raw embedding to itself. A `KVAssociationObjective`
  exists but only splits one embedding into coordinate halves (predict the V-half of `x` from
  `x`) — still self-derived, not a learned cross-item association.
- **The learned gates are inert — the code says so.** `SurpriseModulatedUpdate`'s own
  docstring: *"Pseudolife has no training loop (memory is updated only at inference time), so
  the gate weights stay at their xavier-init values forever and contribute essentially random
  multiplicative noise rather than learned modulation."* TITANS' learned surprise/forget gate
  is replaced by a hand-rolled deterministic sigmoid because there is nothing to train it with.
- **The inner optimizer IS faithful to TITANS** — SGD + momentum 0.9 + weight-decay (forgetting)
  + grad-clip, η-modulated (`update_rules.py`). This is the one part that ports cleanly… but an
  optimizer is only as useful as the objective it descends, and self-reconstruction has no
  associative content.
- **Retrieval** (`band.retrieve`): `scores = w·cos(stored, M(query)) + (1−w)·cos(stored, query)`.
  With `M` a lossy autoencoder, `M(query)` is a degraded query, so the `w`-weighted term can
  only tie (if `M`→identity) or corrupt (as measured) the clean cosine term.

---

## 3. What TITANS / HOPE actually are

- **TITANS** ([arXiv:2501.00663](https://arxiv.org/abs/2501.00663), Google, 2024): a neural
  long-term memory module **integrated into a transformer** (Memory-as-Context / -Gate / -Layer)
  to let **attention** use information from **sequences beyond 2M tokens**. The memory learns
  *what to memorize* at test time via a surprise signal, with **learned key/value/query
  projections**, trained **end-to-end** with the rest of the network on a **language-modeling
  objective**. Value is measured as long-context **perplexity / needle-in-haystack recall**.
- **HOPE / Nested Learning** ([research.google](https://research.google/blog/introducing-nested-learning-a-new-ml-paradigm-for-continual-learning/),
  Google, 2025): a **self-modifying recurrent sequence model**. Its **Continuum Memory System**
  — a spectrum of memory modules each updating at a different frequency — is the direct source
  of our 8 frequency-bands. Again evaluated as a **language model** (lower perplexity, higher
  reasoning accuracy).

Both derive the neural memory's value from **in-context sequence compression, trained
end-to-end, working alongside attention, judged on next-token prediction.**

---

## 4. The gap — regime mismatch

| Dimension | TITANS / HOPE (where it works) | This repo (where it's used) |
|---|---|---|
| Setting | sequence model over a token stream | offline store of discrete, independent docs |
| What the memory does | compress/recall earlier tokens to predict later ones, in-context | re-rank stored sentence-embedding vectors |
| Training | **end-to-end** on an LM/task loss | **no training loop**; self-reconstruction MSE only |
| Keys/values | **learned** `W_k`,`W_q`,`W_v` projections | none — key = value = frozen embedding |
| Gates (surprise/forget) | learned end-to-end | inert (xavier-init forever) → hand-rolled sigmoid |
| What's "learned" | which associations help the downstream task | approximate identity of each vector in isolation |
| Metric of value | long-context perplexity / recall | retrieval recall@k / MRR |
| Inputs | jointly-learned representations | **frozen** sentence-transformer embeddings |

We faithfully ported the **CMS band structure** and the **inner test-time optimizer**, but the
features that generate TITANS/HOPE's value — learned projections, learned gates, end-to-end
task training, a sequence to compress — are **absent, inert, or inapplicable** to standalone
embedding retrieval. The frozen sentence-transformer already does the representation learning;
cosine over it is near-optimal at personal-memory scale. There is no residual associative
structure for a self-reconstruction memory to add, so the blend can at best tie and in practice
corrupts.

**Is it fixable in-regime?** Only by abandoning self-reconstruction for a *genuinely
associative* objective — learned `Q/K/V` + a relevance/contrastive **training signal** over
(query, relevant-doc) pairs. But (a) a personal memory bank has no such labels at scale,
(b) it would compete against an already-strong frozen embedder + cosine, and (c) it still
wouldn't be TITANS-as-published (no sequence, no end-to-end LM task). Expected upside is low;
this is a research project, not a patch.

---

## 5. Recommendation

1. **Default `neural_blend_weight = 0.0`** (pure cosine retrieval ranking). Measured win
   (+0.05–0.06 MRR, +7–9 pts R@1) and removes a per-query MLP forward pass.
2. **Keep the MIRAS bands** as the multi-frequency **episodic substrate** the dream consolidates
   from — the dream reads stored **text**, not MLP weights, so the hippocampus→dream→cortex
   pipeline is unaffected. What we drop is only the **MLP test-time-learning contribution to
   retrieval ranking**.
3. **Reframe the docs**: the headline is the **cortex + cosine-ranked episodic bands +
   dream consolidation**, not an "8-tier neural memory" whose neural ranking is off by default.
4. This resolves the standing continuum "on trial" pin (re-eval was due ~2026-07-06): associative
   recall did **not** fire; simplify toward cortex-first + cosine bands.
5. Leave the TITANS/HOPE machinery in the tree (config-gated, off) for a future *sequence-model*
   experiment where it would actually apply — not as a shipped retrieval re-ranker.

**Caveat on validity:** the corpus is generated (templated paraphrases, transparent in the
harness) and the per-run MLP is freshly trained; a long-lived production MLP sees more steps.
But the ceiling argument is config-independent — a self-reconstruction memory over frozen
embeddings cannot exceed cosine by construction — and the effect is large and consistent across
scales, objectives, and overlap buckets. Re-run anytime with
`PYTHONPATH=. .venv/Scripts/python evals/neural_blend_bench.py --diagnose`.
