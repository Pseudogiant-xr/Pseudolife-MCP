# "Learning on the Job" against the lessons tier — mapping, minimal delta, τ-bench adapter (2026-09-08)

## Problem

Tablan, Taylor and Bernhem ("Learning on the Job: Continual Learning from Deployment
Feedback for Frozen-Weights Agents", arXiv 2607.22157, 2026-07-24) pair a frozen model with
an external memory that distils every τ-bench banking episode into a retrievable
natural-language rule. On the banking domain, against a static-RAG control over the full
policy corpus, learning from the one-bit outcome verdict lifts single-trial success 1.6× and
learning from corrections 2.6×, converting 22 of the 84 tasks the baseline never solves;
the result replicates on Claude Sonnet 5 and the rule store transfers between models.

Pseudolife already has a procedural tier built for the same job: `memory_outcome` writes an
outcome signal, the dream distils signals into lessons, `memory_lesson_search` serves them,
and since PR #285 `memory_outcome(used_ids=)` credits every in-window search that served a
used memory. This spec answers three questions before anything is measured:

1. Which of the paper's mechanisms exist here, which are partial, which are missing.
2. Whether the existing batched, dream-time distillation reproduces their per-episode
   synchronous loop, or whether a synchronous distill-on-outcome path is required —
   answered from the paper's own cadence data, not assumed.
3. The smallest change set that runs their τ-bench banking protocol with Pseudolife as the
   memory backend, and the parity bar it must clear.

Constraints held throughout: no schema migration, no deploy, no eval or GPU launch without
an explicit go, no change to the dream sidecar timeouts, and no graph-layer work — the
2026-09-04 `graph_ablation` verdict stands (`memory_recall`'s expansion served 27× the
characters at 74× the wall time of plain `memory_search` and found the target no more
often).

## The paper's protocol (from the arXiv HTML and the released harness)

Source of record: `memcoai/spark-continual-learning-paper-data` (code MIT, data CC-BY-4.0).
The harness patches tau2-bench 1.0.1 at commit `fc0055dc4e0a316c3f83133267fbd6faaa770992`
with +73 lines (agent registration, memory tools attached as ordinary environment tools, a
write-back hook after evaluation, telemetry stamping, Anthropic prompt caching);
`src/spark_taubench/spark_agent.py` carries the prompts verbatim.

- **Benchmark.** 97 banking tasks, 4 trials each, trial-major scheduling with
  `--max-concurrency 1`; verdict = tau2 evaluator reward ≥ 0.999 (deterministic database
  and action checks; the sampled banking tasks carry `reward_basis: ["DB"]` and no natural-
  language assertions); customer simulated by gpt-5.2; agent Mistral Large 2512 or Claude
  Sonnet 5 at medium effort.
- **Control.** Static RAG: sparse BM25 over the ~700-document policy corpus, no memory, no
  state across trials. Baseline pass^1 0.064 (Mistral), 0.248 (Sonnet).
- **Retrieval.** Agent-driven ("nudge" mode): the first customer turn carries a reminder to
  call `memory_search` before acting and again when the request changes, and to re-read any
  `VERIFIED RULE` before a consequential action. Nothing is injected by the harness.
- **Write path.** One synchronous reflection turn after every episode, by the same agent,
  with the verdict in hand. Writes attempted mid-conversation are acknowledged and dropped.
- **Feedback arms.** *Experience*: the verdict only; on failure the rule is `AVOID: …`
  ("WHEN <situation> do NOT <exact actions taken> — verified wrong"), and the agent is told
  not to invent the right answer; on success `VERIFIED RULE: … WHEN <situation> THEN <exact
  action, decision-critical values verbatim>`. *Instruction*: the verdict plus the task's
  verified action sequence, delivered once after a failed episode, plus a harness-computed
  action diff (missing / wrong-argument / extra actions). A saved rule is validated: the
  decision-critical strings from the diff must appear verbatim, not inside an example, with
  customer identifiers excluded; up to two bounces.
- **Staleness and conflict.** Never contradict, weaken or add exceptions to a `VERIFIED
  RULE`; a conflict is a separate note. One exception: two verified rules for look-alike
  situations with different actions merge into one branch rule that ends "Supersedes:
  <title>". Server-side asynchronous curation dedups restatements into supporting evidence
  and prunes decayed insights.
- **Transfer.** Each model reads the other's frozen store read-only (`memory_search` only).
  Mistral reading Sonnet's store +0.224 pass^1; Sonnet reading Mistral's +0.066.
- **Metrics.** Per-trial success curve; pass^k with the unbiased per-task estimator
  Σ C(c,k)/C(n,k); hold rate P(pass at t+1 | pass at t); conversion on the floor stratum
  (tasks the baseline never solves); cluster-bootstrap percentile intervals, B = 10,000, one
  shared task-index draw per replicate across conditions.

### Cadence data, and the structural answer

The paper reports: "The three conditions are statistically indistinguishable on the cold
first trial (6, 5, and 9 tasks of 97)." The instruction curve is 9 → 18 → 16 → 23 against a
baseline of 6 → 7 → 5 → 7 (the analysis code's `per_trial`; the paper's pass^k row
0.064 / 0.038 / 0.034 / 0.031 and its 84-task floor follow from the same grid). Every lift
appears from trial 2 onward, and under trial-major
scheduling the rule written after task T's trial t is consumed by task T's trial t+1, about
97 episodes later. No batching ablation exists in the paper; their distillation is
per-episode and synchronous because their store is a remote service and their reflection is
agent-side, not because the learning needs it.

Consequence: a distillation batched at each trial boundary reproduces the loop the data
shows. A synchronous distill-on-outcome path is not required and is not built. The adapter
runs the two cadences as an arm (`--distill trial` and `--distill episode`) so the
assumption is measured rather than carried.

## Mapping onto what exists

| Paper element | Pseudolife | Status |
|---|---|---|
| Verdict captured after the episode | `MemoryService.record_outcome` (`pseudolife_memory/service.py`) → `storage.add_signal`; never writes a lesson | identical |
| Correction feedback (verified sequence + diff) | `memory_outcome(outcome="correction", detail=…)`, free text | partial — carrier exists, no diff or verbatim-value contract |
| Distillation prompt | `_LESSON_SYSTEM_PROMPT` (`pseudolife_memory/memory/dream.py`): clusters signals, skips trivia, one abstract lesson per `(task-type, aspect)` | mismatch — the paper wants one verbatim situation→action rule per episode |
| Store keyed per situation | `lessons` slot `(task, aspect)`, one current value, supersession (`pseudolife_memory/memory/lessons.py`) | mismatch — 97 situations under one task type would supersede each other |
| Distillation cadence | only `dream_run` synthesises (`pseudolife_memory/service_dream.py`); `pull`/`commit` never do; `would_fire` has no signal-count term; synthesis drains the global queue with no episode filter | partial — an explicit `memory_dream(action="run")` reproduces either cadence; nothing fires on a lone signal by itself |
| Agent-driven retrieval | `memory_lesson_search` (cosine on the query, top_k 5); `memory_search` never returns lessons | partial — two calls where the paper has one |
| Credit on retrieved rules | `used_ids` credits `retrieval_events` rows; `lesson_search` never logs an event | missing for lessons, identical for entries |
| Verified-rule protection, twin merge | none; `memory_store(distortion_tolerance="constraint")` pins an entry verbatim and serves it first; the synthesis dedup gate folds same-polarity near-duplicates at a different key | partial |
| Staleness | `_annotate_lesson_staleness` flags a lesson whose `about` entity's cortex changed | partial — different trigger |
| Rule validation | none | missing |
| Cross-model transfer | the bank is model-agnostic; no daemon read-only mode | partial — the harness narrows the tool set |
| Bench of the distiller | `evals/lesson_synthesis_bench.py` | exists; gains a rule rung |

## Change set

Engine, all default-off or additive, no schema change:

1. **Rule mode for synthesis.** `LessonsConfig.rule_mode` (default False) and a per-signal
   opt-in: an `about` starting with `rule:`. Rule signals go to
   `OpenAICompatExtractor.extract_rules`: one call per signal under
   `_RULE_LESSON_SYSTEM_PROMPT`, exactly one rule kept per signal, `aspect` forced to
   `rule`, the routing prefix stripped from `about`. The slot key becomes `(situation,
   "rule")`, so rules coexist instead of superseding. When the signal's `detail` carries a
   `MUST INCLUDE: a; b` line and the rule drops a value, the call is retried once naming
   the missing values (the paper bounces up to twice); the second answer is accepted.
   Rules are exempt from the cross-key cosine dedup gate: two look-alike situations with
   different actions are both information. An extractor without `extract_rules` folds rule
   signals back onto the clustering path and the synthesis report says so
   (`rules_fallback`). The shipped prompt, its batched call shape and the default slot
   semantics are unchanged; `tests/test_lesson_rule_mode.py` pins both.
2. **`detail` contract for corrections.** No signature change on `memory_outcome`. The
   adapter writes `detail` as a fixed block — `SITUATION`, `VERDICT`, `ACTIONS TAKEN`,
   `CORRECT SOLUTION`, `ACTION DIFF`, `MUST INCLUDE` — and the rule prompt reads those
   labels. Documented in the config comment and the memory-model guide; the tool
   description budget has no headroom for it.
3. **Not added: retrieval credit for lessons.** `retrieval_uses.entry_id` shares the id
   space with entries; logging lesson ids would poison the reranker training tuple without a
   `kind` column, which is a schema change. The lessons route therefore measures the rule
   store's effect on success, not per-rule relevance labels; the entries route measures
   both.
4. **Not added: a `would_fire` term for pending signals, twin-situation merge, a daemon
   read-only mode.** The orchestrator triggers dreams; the entries route's agent-side
   reflection supersedes a rule for the same situation by design; the harness narrows the
   tool set for transfer.

Eval tooling:

5. **Tool-call emulation in the Claude shim** (`evals/claude_shim.py --emulate-tools`). The
   shim runs `claude -p --tools ""`, a pure completion; tau2's agent loop reads OpenAI-format
   `tool_calls` and has no text fallback. In emulate mode the request's tools are rendered
   into the system prompt with a strict JSON reply schema, tool history is folded into the
   transcript, and a parsed reply becomes `tool_calls` with `arguments` always a valid JSON
   string; a near-JSON reply is retried once. The mode refuses to start on the production
   port; the adapter's endpoint is `PSEUDOLIFE_BENCH_TAUBENCH_AGENT_URL`.
6. **The tau2 plugin** `evals/taubench_pseudolife/` (Python 3.12, its own venv, never
   imports `pseudolife_memory`): a `pl_memory` agent registered by a four-hunk patch mirroring
   the paper's; memory tools as environment tools (`memory_search`, `memory_lesson_search`;
   writes deferred); a post-evaluation reflection hook; per-episode `X-PL-Session`
   identity with `/api/episode/start|end`; telemetry of every memory call including every
   `used_ids_*` key the outcome returns.
7. **The adapter** `evals/taubench_adapter.py` (repo venv): orchestrates conditions, trials
   and dreams, scores with the paper's estimators and bootstrap, writes append-only rows and
   a summary under `evals/results/taubench-*`, and refuses to publish ratios when any episode
   violated the use-window invariant.

### Semantics flags for current users

- `rule_mode` is off by default and the `rule:` prefix is inert unless the dream's extractor
  is an `OpenAICompatExtractor`; existing banks see no change.
- A rule signal on a bank whose extractor predates `extract_rules` is synthesised under the
  clustering prompt (reported), never stranded.
- Nothing about `memory_outcome`'s surface or `used_ids` semantics changes.

## Adapter protocol and invariants

- One session identity per episode: mint `session_uid`, `POST /api/episode/start`, send
  `X-PL-Session` on every MCP call, log the outcome before `/api/episode/end`.
- The outcome is logged inside `memory.retrieval_log.use_window_seconds` (3600) of the
  episode's first search. The plugin checks this and records `window_ok` per episode but
  still sends the outcome (refuse the label, never the signal); the adapter's report is
  where the invariant bites — it refuses to publish ratios if any episode violated it (a
  silent zero label is the failure mode PR #285 documented).
- Conditions: `baseline` (static RAG, no memory tools), `experience` (verdict),
  `instruction` (corrections). Routes: `entries` (the reflection turn writes each rule as a
  `constraint` entry, tagged per run, credited by `used_ids`) and `lessons` (only
  `memory_outcome` with the `rule:` prefix; the dream synthesises). Cadence:
  `--distill episode|trial` for the lessons route. Transfer: `--read-only --distill off`
  (the plugin sees `PL_READ_ONLY=1`, exposes only the two search tools, opens no episode,
  and a consolidating cadence is refused) against a daemon serving a copy of the producer
  run's bank.
- Instruments: agent = Claude Sonnet 5 at medium effort through the emulating shim on an
  eval-only port; customer = local Qwen under the reproducible server config (never
  `-Fast`); natural-language assertions, if any task carries them, graded by Qwen with a
  majority of three; every run binds to its own bench database.
- Every prompt the adapter or plugin authors is generic; no benchmark task, policy or
  dialogue text appears in any prompt, and the prompt-lift guard checks the adapter's prompt
  carriers against the banking task set.

## Parity bar (preregistered)

On the full 97-task set, 4 trials, against our own static-RAG baseline on the same
instrument (same customer model, same agent, same corpus):

- **instruction ≥ 2.6× baseline pass^1** and **experience ≥ 1.6× baseline pass^1**, with the
  paired cluster-bootstrap interval of the ratio excluding 1.0.
- Secondary: conversion on our floor stratum reported beside the paper's 22 of 84; hold rate
  reported; pass^4 reported but not gated (the paper's own pass^4 stays flat).

Absolute numbers are expected below the paper's: the customer is a local model, not gpt-5.2.
Only the paired ratios are read against the bar.

## What a miss would tell us

- Entries route clears the bar and the lessons route does not → the slot and paraphrase
  design of the lessons tier is the cause, not batching. Fix: per-situation keys and
  verbatim rules as the default for rule-shaped signals.
- Both routes clear the bar with `--distill trial` → batched distillation is sufficient;
  the sweep's cadence is not a bottleneck for same-task learning.
- `--distill episode` beats `--distill trial` by more than the bootstrap noise → cross-task
  transfer exists on this instrument; then the sweep needs a pending-signal term in
  `would_fire`, which is the deferred follow-up.
- Neither route clears the bar → the instrument (customer or agent) is read first against
  the paper's absolute baseline, before any conclusion about memory.

## Execution

Smoke (3 tasks × 2 trials, one condition, both routes) only on the maintainer's word; the
full grid only after the smoke shows zero agent errors and every episode `window_ok`. Cost
note: the CLI-backed shim serialises calls, so a full condition is tens of hours on the
Sonnet instrument.

### Smoke record (2026-09-08)

Four smokes ran (entries under nudge, twice; lessons under inject, twice — the second on a
daemon carrying rule mode): 24 episodes, zero agent errors, zero customer errors, every
episode `window_ok`. Findings that changed the adapter before any number: the local
customer's thinking mode returns empty turns (pinned off); tau2's data dir must be the
checkout's and absolute; the released task list is one line; the model imitated the
shim's history marker (forbidden and recovered); and retrieval compliance under `nudge` was
one search in twelve episodes, so **the headline runs use `--retrieval inject`** — the
paper's second mode — under which every episode served three to five memories, every
served id was credited, and the rule-mode daemon wrote one situation-keyed rule per
episode. A `nudge` arm stays available to measure compliance itself. The smoke's rewards
(two passes in 24 episodes on a bank that carried over between smokes) are not a number.
