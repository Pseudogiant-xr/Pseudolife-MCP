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
  `origin/master` `f21fe8e0` before the run.
- Each brief runs once per model as an `Explore` subagent (read-only tools)
  through Claude Code's Agent tool, with `effort: medium` on every model
  so the model is the only variable. Haiku's default is `medium`; Sonnet's
  default is `high`, so Sonnet runs below its default here.
- `score.py` scores recall (keys matched / keys), counts traps, and reads
  tokens and wall time from the harness's per-agent usage line.
- Answers are committed verbatim under `results/`, one `.md` per run with
  its `.json` metadata; `results.json` is the scored table.

## Results, pilot round, 2026-10-14

Eight briefs, one replicate each, 24 runs. Head `f21fe8e0`.

| Model | Recall | Min | Traps | Tokens per run | Seconds | Answer chars |
|---|---|---|---|---|---|---|
| Haiku 5.5 | 1.000 | 1.00 | 0 | 54,240 | 25 | 2,208 |
| Sonnet 5.5 | 1.000 | 1.00 | 0 | 39,703 | 19 | 2,492 |
| Opus 5.5 | 1.000 | 1.00 | 0 | 39,186 | 22 | 2,641 |

The first scoring pass showed four misses. Each was a key regex that did
not accept a correct phrasing (`MAX_WAKES=20` for "20 wakes", "lets the
call through" for "fails open", and two like them). The keys were widened
to the correct phrasings and the run rescored; no answer was changed.

### Reading

- **On briefs with precise pointers, all three tiers hit the ceiling.**
  Every model recovered every verified fact and wrote no wrong claim. The
  bench as run cannot rank the models on quality; it can only say the
  cheapest tier is not losing facts on this class of task.
- **Tokens:** Haiku read 38% more than the other two (more tool calls: up
  to 16 against Opus's 10 on the same brief). At the 2026-10-06 list
  prices, Haiku's run still costs roughly a tenth of Sonnet's and a
  twentieth of Opus's. Sonnet and Opus used the same tokens, so the
  "Sonnet spends more tokens per task" claim in the maintainer's
  instructions is not reproduced on this workload. Sonnet at effort
  `medium` was also the fastest.
- **What follows for the tiering:** for bulk reading with a precise brief
  and a checkable answer, Haiku is the right default, as the instructions
  already say. Whether Sonnet or Opus is worth its price is a question
  about briefs that need judgment, which this round did not contain.

### Limits

- One replicate per cell. The ceiling makes replicates moot for this
  round; the next round needs them.
- The briefs name the files to read. A vaguer brief ("find out why X")
  makes the model choose where to look, which is where tiers should
  separate.
- Token counts come from the harness's usage line, which does not split
  cache reads from writes, so cost is estimated from list prices rather
  than measured.
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
python evals/subagent_tier_bench/score.py evals/subagent_tier_bench/results --out evals/subagent_tier_bench/results.json
```

New runs go in `results/` as `<brief>.<model>.<rep>.md` with a `.json`
beside each holding `{"tokens": N, "seconds": S}`.
