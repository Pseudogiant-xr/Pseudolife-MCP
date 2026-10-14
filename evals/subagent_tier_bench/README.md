# Subagent tier bench

Which model should an orchestrating session hand a read-only exploration
brief to? This bench runs the same briefs over this repo on Claude Haiku
5.5, Sonnet 5.5 and Opus 5.5 as Claude Code `Explore` subagents, and scores
each answer against facts verified in the tree.

## Why

The maintainer's global instructions tier subagents by model: Haiku for
bulk, checkable reading; Opus for judgment and code; Sonnet in between. A
measurement on 2026-10-14 found subagents were about a third of the
delegate sessions' spend, so the tiering matters. The question this bench
answers first is whether the cheap tier loses facts on this repo's
exploration tasks. A later round asks whether Sonnet earns its price over
Haiku on anything.

## Method

- `briefs.json`: eight exploration briefs, each naming the files or
  functions to read and asking for five to thirteen facts with file:line
  references. Each brief carries `keys`, one regex per fact, and `traps`,
  regexes for wrong claims. Every fact was verified against
  `origin/master` `f21fe8e0` before the run. A key is written to require
  the fact's value in its context, never a word the prompt itself uses;
  a trap is written not to fire on a correct negation of the wrong claim.
  `tests/test_subagent_tier_bench.py` proves both on fixtures, for every
  brief: no key matches the prompt; a fixture stating every fact matches
  every key and no trap; dropping one fact misses exactly its key; a
  wrong value trips a trap and its negation does not.
- Each brief runs once per model as an `Explore` subagent (read-only tools)
  through Claude Code's Agent tool, with `effort: medium` on every model
  so the model is the only variable. Haiku's and Opus's default is
  `medium`; Sonnet's default is `high`, so Sonnet runs below its default.
- `score.py` reports two metrics, kept separate: recall (keys matched /
  keys) and the traps matched. Tokens, tool calls and wall time come from
  the harness's per-agent usage line, copied by hand into the `.json`
  beside each answer; the harness reports one token total that mixes
  input, output and cache tokens.
- Answers are committed under `results/`, verbatim except that machine
  paths are replaced by `<repo>` and `<home>`. Each round's scored table
  is its own file; `results-pilot-20261014.json` is the pilot's.

## Results, pilot round, 2026-10-14

Eight briefs, one replicate each, 24 runs. Head `f21fe8e0`.

| Model | Recall | Min | Traps | Tokens per run | Tool calls | Seconds | Answer chars |
|---|---|---|---|---|---|---|---|
| Haiku 5.5 | 1.000 | 1.00 | 0 | 54,240 | 7.4 | 25 | 2,202 |
| Sonnet 5.5 | 1.000 | 1.00 | 0 | 39,703 | 4.8 | 19 | 2,492 |
| Opus 5.5 | 1.000 | 1.00 | 0 | 39,186 | 4.8 | 22 | 2,599 |

### How the keys changed after the run

The run was scored three times; the answers never changed.

1. The first keys were loose. Four runs showed misses that were all key
   defects: all three models wrote `MAX_WAKES=20` where the key wanted
   "20 wakes"; the board-guard key for "fails open" rejected "fail-open"
   (Haiku) and "lets the call through" (Opus), and its "receive is
   allowed" key looked 60 characters when the answers put 120 between the
   words (Opus, Sonnet); the suite-lock key for the empty-string reason
   wanted "GPU" or "Windows" near "empty" (Opus). Widening those gave 1.000
   everywhere.
2. The review of that result (2026-10-14, Opus reviewer and Codex) found
   the opposite defect: several keys could pass on words copied from the
   question, or on a bare number, and one trap fired on a correct
   negation. Every key was rewritten to require the value in context, the
   traps were made negation-aware, and the fixture tests above were
   added. Under the strict keys the result is the same: 1.000 on every
   run, no trap matched.

### Reading

- **No misses were observed, in one run per brief.** All configured keys
  matched and no configured trap matched, for all three models. That is
  the whole of what the scorer shows: a trap list is a short blacklist,
  and an answer can match every key and still add an unchecked wrong
  sentence. The 24 answers were read by a person as well, and no wrong
  claim was found in them, but that reading is not a measurement.
- **The bench as run cannot rank the tiers on quality.** It says the
  cheapest tier lost nothing on this class of task, in this sample. With
  eight single runs and no misses, a per-brief miss rate up to about a
  third is still consistent with the data (the rule of three), so the
  reliability claim needs replicates.
- **Tokens:** Haiku reported about 38% more total tokens than the other
  two, with more tool calls (7.4 a run against 4.8). Sonnet and Opus
  reported the same totals. At the 2026-10-06 list prices for input
  ($0.10, $2 and $4 per million tokens), and assuming the same price ratio
  across input, output and cache tokens, Haiku's run costs about 1/15 of
  Sonnet's and 1/29 of Opus's. The ratio is an estimate from totals, not
  a billed figure.
- **On the "Sonnet spends more tokens per task" note** in the maintainer's
  instructions: not reproduced here, but this is a different comparison.
  Sonnet ran at `medium`, below its default `high`, and the task was
  read-only exploration, not implementation. It is a reason to measure
  that claim on implementation tasks, not a refutation.
- **What follows for the tiering:** for bulk reading with a precise brief
  and a checkable answer, Haiku is the right default, as the instructions
  already say. Whether Sonnet or Opus is worth its price is a question
  about briefs that need judgment, which this round did not contain.

### Limits

- One replicate per cell, and the ceiling gives no estimate of
  repeatability. The next round needs replicates.
- The briefs name the files to read. A vaguer brief ("find out why X")
  makes the model choose where to look, which is where tiers should
  separate.
- Token counts are one total per run from the harness, with no
  input/output/cache split, so cost is estimated, and "read more" means
  "reported more total tokens".
- Subscription usage, not API billing, paid for this run.

## Next round

After the weekly usage reset: the same scorer on eight harder briefs, three
replicates per cell, 72 runs. Brief kinds that should separate the tiers:

- vague: a symptom, no file named ("why would a lease read free when the
  board is down?");
- multi-hop: the answer needs three files joined;
- judgment: "is this a bug?", scored against a reviewed verdict;
- adversarial: a brief whose premise is wrong, scored on whether the
  model says so.

Add Fable 5.1 as a fourth arm on the judgment briefs only.

## Re-running

```
python evals/subagent_tier_bench/score.py evals/subagent_tier_bench/results --out evals/subagent_tier_bench/results-<round>.json
```

`--out` refuses an existing file: each round writes its own, and the
pilot's stays as committed. New runs go in `results/` as
`<brief>.<model>.<rep>.md` with a `.json` beside each holding
`{"tokens": N, "seconds": S, "tool_uses": T}`.
