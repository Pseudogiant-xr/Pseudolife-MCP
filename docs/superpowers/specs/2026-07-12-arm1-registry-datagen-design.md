# Arm-1 registry-datagen — Sonnet+v1 teacher, automated via shim

**Status**: approved-by-delegation (autonomous session, user asleep — see Provenance) · **Date**: 2026-07-12
**Predecessor**: `docs/specs/2026-07-11-learned-consolidation-scoping.md` (Arm-1 definition),
`docs/superpowers/specs/2026-07-07-extractor-stage15-jepa-sonnet-design.md` (asymmetric
teacher/student split, arm B' key-canonicalization), `2026-07-11-sonnet-v1-prompt-record.md`
memory (v1 prompt KU-oracle cortex 0.808, the new teacher).

## Provenance (why this doc has no interactive Q&A section)

The user explicitly asked me to proceed with the Arm-1 datagen thread autonomously
overnight ("please proceed autonomously with the Arm-1 datagen and anything else in this
thread you deem worthy") and is not available to answer brainstorming questions. Per
`using-superpowers`, user instructions override skill process defaults, so this design
skips the interactive brainstorming loop but keeps its structure (approaches considered,
design written down, self-reviewed) for auditability. Nothing here is irreversible or
consumes shared resources at scale without an explicit stop — see Gates below.

## What Arm-1 actually needs (recap)

Regenerate distillation training data with a per-chain **key registry** enforced at
*generation* time: the teacher must reuse each chain's established `entity.attribute`
keys and restate them when a session still evidences them, instead of rewording the
same fact into a new key each time. This targets a measured, specific failure: Stage
1.5's arm B' found Sonnet's raw labels reused keys exactly 6.8% of the time vs Qwen's
24.1% — the post-hoc `--canonical-keys` merge (arm B') patches this at ingest time but
can't recover claims that were never extracted because a session's fact looked "new"
to the teacher. Showing the registry at generation time and instructing reuse attacks
the root cause instead of the symptom.

This is **not** the known-facts-window intervention that failed on 2026-07-11 (cortex
−0.08): that experiment showed facts to the **student at inference**. Arm-1 shows the
registry to the **teacher at datagen time only** — the student's stored training rows
keep the plain production prompt, so the model learns key-reuse as an SFT prior, never
sees a registry at inference. Same asymmetric-split principle Stage 1.5 Arm B already
used successfully (teacher gets a privileged prompt; stored rows get the production one).

## Decision: swap teacher to Sonnet-5 + v1 prompt, drop manual subagent dispatch

Two changes from the original Arm-1 sketch (which assumed the Qwen3.6-27B teacher):

1. **Teacher = Sonnet-5 + `sonnet_extractor_v1.md`**, not Qwen. This was decided the
   same day the scoping doc was written (see memory `sonnet-v1-prompt-record`): v1 is
   the best labeler on record (KU-oracle cortex 0.808 vs Qwen's much lower ceiling), so
   it is also the better *datagen* teacher. Confirms "option 3" from that memory: v1
   serves as both the live dream sidecar (shipped 2026-07-11) and the datagen teacher.

2. **Automated via the shim, not manual subagent briefs.** `distill_datagen_sonnet.py`
   (Stage 1.5) used a `--emit-briefs` / dispatch-subagents / `--ingest` file-handoff
   loop because at the time there was no programmatic way to call Sonnet at volume —
   each session had to be hand-answered by a Claude subagent reading a brief file. The
   sonnet-sidecar-cutover (merged 2026-07-11) now runs `evals/sonnet_shim.py` as a
   live OpenAI-compatible HTTP server wrapping `claude -p` on the Max plan. That shim
   is exactly what `evals/distill_datagen.py` (the original Qwen pipeline) already
   expects: a `POST /v1/chat/completions` endpoint. Arm-1's datagen script is therefore
   a **direct extension of `distill_datagen.py`**, pointed at the shim, not a rebuild of
   the subagent-dispatch machinery.

   Bonus: the shim's existing prompt-substitution contract
   (`evals/sonnet_shim.py::ClaudeCli.chat` — swaps a `_SYSTEM_PROMPT`-prefixed system
   message for the v1 override, preserving whatever suffix the caller appended) means
   the datagen script can send `_SYSTEM_PROMPT + _vocab_hint(...) + registry_hint(...)`
   exactly like the Qwen path does, and the shim transparently substitutes in the v1
   prompt server-side. No new prompt-plumbing code needed.

## Design

### New script: `evals/distill_datagen_arm1.py`

Mirrors `distill_datagen.py`'s architecture (dataset load → forbidden-session KU guard
→ per-question chronological session iteration → resumable JSONL append), adding one
new piece of state per chain: a **registry** — `dict[(norm_entity, norm_attribute),
value]` built from the chain's own accepted claims, in chronological order, reset per
question (matches the existing "sequential within a question, independent across
questions" rule both prior scripts already follow).

**Two system-message variants per call** (the asymmetric split):

- `teacher_system` (sent to the shim): `_SYSTEM_PROMPT + _vocab_hint(vocab_list) +
  _registry_hint(registry)`. The shim substitutes the v1 prompt for the
  `_SYSTEM_PROMPT` prefix and forwards the rest untouched.
- `stored_system` (written into the training row): `_SYSTEM_PROMPT +
  _vocab_hint(vocab_list)` — no registry block. Identical to what
  `distill_datagen.py` already stores; the registry never reaches the student.

`_registry_hint`:

```python
def _registry_hint(registry: dict[tuple[str, str], str]) -> str:
    if not registry:
        return ""
    items = list(registry.items())[:VOCAB_MAX]
    lines = "\n".join(f"- {e} | {a}: {v}" for (e, a), v in items)
    return (
        "\n\nCHAIN REGISTRY (facts already established earlier in this "
        "conversation history — if this session's notes still evidence one "
        "of these, reuse the EXACT SAME entity/attribute key; restate the "
        "same value if unchanged, or the new value if this session updates "
        "it. Do not invent a new key for a fact you already have a key for. "
        "Never emit a claim this session's notes don't evidence.):\n" + lines
    )
```

The trailing sentence is the fabrication guard: the registry raises reuse discipline,
it must not raise recall by inventing unevidenced claims. `validate_claims`'s existing
citation-range check (`source` must cite a note within the current session) already
enforces this mechanically — a claim with no session evidence has nowhere valid to cite.

**Registry update** after each session's claims are validated and accepted (same point
`vocab` is updated today): `registry[(_norm_key(e), _norm_key(a))] = value` for each
claim, last-write-wins in chronological order — so a later session's update naturally
overwrites an earlier value, exactly matching how supersession should look downstream.

**Everything else is reused verbatim, not reimplemented**: `_parse_date`,
`validate_claims`, `VOCAB_MAX`, the KU forbidden-session guard, the resumable
`done_rows` replay, the `--max-empty-share` cap, the `_SYSTEM_PROMPT`/`_vocab_hint`
imports from `dream.py`.

### CLI

```
python evals/distill_datagen_arm1.py [--out PATH] [--teacher-url URL]
    [--questions N] [--limit-rows N] [--max-empty-share F]
```

- `--teacher-url` defaults to `http://127.0.0.1:8082/v1` (the live shim), not the Qwen
  port — this pipeline has no Qwen dependency.
- `--questions N` caps the number of source questions processed (pilot control,
  mirrors `distill_datagen_sonnet.py --questions`), independent of `--limit-rows`
  (existing row cap, kept for parity/resume behavior).
- Output: `evals/data/distill-extract-arm1.jsonl` (gitignored, matches existing
  `distill-extract*.jsonl` convention).

### Comparison tooling

Reuse `distill_datagen_sonnet.py`'s `_cmd_compare` shape (claims/session ratio, slot-key
Jaccard vs a reference set) rather than writing new analysis code — the underlying
metrics are format-agnostic (both scripts store identical row shapes). A tiny
standalone comparison script isn't warranted; the existing `--compare` mode reads any
two JSONL files by row `id`, so pointing it at the arm1 output vs the existing
`distill-extract-sonnet-clean.jsonl` (Stage 1.5, ungoverned keys) answers the "did the
registry actually raise reuse" question directly via the Jaccard/ratio it already
prints — this doc does not propose new metrics.

## Gates (pre-registered, unchanged from the scoping doc — restated for this run)

1. **Pilot first.** Run on a small, bounded number of source questions before any
   full-scale fan-out — mirrors Stage 1.5 Arm B's "label ~10 questions, hand-audit ~30
   rows" gate. This autonomous session runs the pilot only and stops there: a full
   fan-out is hours of serialized shim calls against the user's Max-plan session quota
   (the same quota their own Claude Code usage draws from) — spending that unsupervised
   overnight, at a scale that could exhaust tomorrow's usage, is exactly the kind of
   costly/hard-to-reverse-in-effect action the safety rules ask me to check before
   taking, so it is left as an explicit decision for the user, not taken automatically.
2. **Full fan-out** (user decision): only after reviewing the pilot's `--compare`
   numbers and a hand-audit sample, per the existing Stage 1.5 protocol.
3. **Training + deploy gate** (unchanged, pre-registered in the scoping doc): cortex ≥
   same-sitting e4b-ft baseline + 0.05, hybrid no regression, ladder clean
   (gold ≥ 0.9, stale_leak = 0.0). Not reached this session — no datagen-complete,
   no training triggered.

## Risks

- **Shim call volume vs Max-plan quota**: serialized (~10-15s/call from observed
  health-check timing), so a pilot of ~10 questions × ~5-8 sessions ≈ 50-80 calls ≈
  10-20 minutes — small next to a subagent's own usage. Full fan-out would be the
  concerning scale; gated per above.
- **Registry-induced over-restatement**: guarded by the explicit "never emit a claim
  this session's notes don't evidence" instruction plus the unchanged citation-range
  validation — a hallucinated restatement has no valid `source` to cite and fails
  `validate_claims`.
- **Distribution shift vs the existing Sonnet-clean set**: expected and desired (that's
  the treatment); `--compare` against `distill-extract-sonnet-clean.jsonl` quantifies it
  before any training commitment.

## Out of scope this session

- Arm 2 (GRPO write-policy) — gated behind Arm 1 per the scoping doc.
- Full-scale datagen, cleaning, training, deployment — all downstream of the pilot gate.
- Any change to the live dream sidecar or production prompts.
