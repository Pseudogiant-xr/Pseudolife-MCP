# Rust eval instruments

No number from the Rust drivers is comparable with a published Python number.
The drivers measure a different instrument, including different context and
judging behavior. No measured Rust regression baseline exists yet.

## What the Rust drivers measure differently

- **Cortex and hybrid contexts.** Public Rust search serves up to five
  facts. The frozen Python LongMemEval bench selected up to 24 at a minimum
  score of 0.2. Rust fact lines use `entity / attribute: value`, with
  `previous values` for scalars and `former members` for sets. LongMemEval
  builds `rag` and `hybrid` from the same search result, honoring
  `--contiguity` and `--timeline`. It requests six entries and uses the raw
  list as served without a second cut to six. `rag_control` holds raw turns
  retrieved from a separate no-dream bank with those options off. The gate
  uses the pinned historical RAG bytes as `rag_control` and scores Rust
  search separately.
- **Question coverage.** The LongMemEval driver defaults to the full oracle
  selection across six question types. The frozen Python default was the
  knowledge-update slice. The Rust regression gate still uses its pinned
  knowledge-update slice.
- **Judging and tokens.** The first word of the case-folded judge reply is
  the verdict, ignoring surrounding whitespace and punctuation. `yes` and
  `no` are the accepted words. An unparseable reply is reported with its
  text and makes the run incomplete. Context tokens use the ceiling of
  character count divided by four. The ladder instead joins a query's
  returned text with newlines and uses the floor of character count divided
  by four, with a minimum of one. `lme_rejudge.py` reuses the recorded
  answers and reports disagreements without the frozen driver's stability
  sample, paired test, or leak-excluded means.
- **Extraction.** The Rust driver records a 90-second extractor timeout and
  a 2,048-token extractor output limit in the config. The frozen Python
  bench used 600 seconds and 4,096 tokens. The Rust MCP client waits up to
  120 seconds for a response. Direct answer and judge requests have a
  recorded 600-second timeout. A cell with quarantined entries stays scored
  and records the count. The gate replays projected slots without running
  extraction and records zero quarantines. A differing baseline count is
  reported as a warning. The public `memory_dream(action="run")` response
  has no marker for successful per-entry extraction fallback. A field in
  that response would let the driver record isolation independently of
  quarantines or held batches.
- **Ladder and gate controls.** Rust ladder rung labels are free text for an
  explicitly configured extractor. The old `floor` regex rung is frozen.
  The abstention sweep retains the five cortex guards (0.3, 0.5, 0.65,
  0.75, 0.85) and six raw confidence floors (0, 0.5, 0.65, 0.70, 0.75,
  0.80). Each grid cell extracts independently and records its own tally
  with the two abstention rates. The Rust gate refuses `-Replicates 1` and
  records the source digests of its acquisition, rendering, and scoring
  code. The default projected gate fixture is an ignored local file.

## Commands in evals/README.md that changed

These examples preserve historical entry points while the Python package
and its fixtures are present.

| Historical line | Behavior of the Rust entry point | Frozen entry point |
| --- | --- | --- |
| `ladder_sweep.py --list` | Uses explicit extractor settings and has no rung catalogue. | `python evals/_python_ladder_sweep.py --list` |
| `ladder_sweep.py --rung naive-rag` | Writes a raw control to `--out` using explicit daemon inputs. | `python evals/_python_ladder_sweep.py --rung naive-rag` |
| `ladder_sweep.py --rung floor` | Refuses the historical regex rung. | `python evals/_python_ladder_sweep.py --rung floor` |
| `ladder_sweep.py --rung gemma-e2b` and the other historical model labels | Uses `--out`, `--control` and an explicit extractor rather than resolving the label to a model. | `python evals/_python_ladder_sweep.py --rung gemma-e2b` or the historical label |
| `ladder_sweep.py --abstain gemma-e2b` | Takes explicit model, control and output paths and records each cell's tally. | `python evals/_python_ladder_sweep.py --abstain gemma-e2b` |
| `ladder_sweep.py --report` | Takes the raw control first, then rung artifacts, and an explicit `--out`. | `python evals/_python_ladder_sweep.py --report` |
