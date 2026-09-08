# pseudolife-taubench

A `pl_memory` agent for the taubench harness, backed by the Pseudolife memory
daemon. The agent reads its own experience during a task and writes back what it
learned once the task has been graded.

The plugin never imports `pseudolife_memory`. It is an ordinary external client:
MCP over streamable HTTP for the tools, two REST endpoints for episode
lifecycle. A guard test enforces that (`tests/test_taubench_plugin.py`).

## Install

```
uv venv --python 3.12 .venv-taubench
# the harness is already installed editable from reference/tau2-bench[knowledge]
cd reference/tau2-bench && patch -p1 < ../../evals/taubench_pseudolife/patches/tau2_pseudolife.patch
#   (alternative, if you prefer git's applier: git apply -p1 ../../evals/taubench_pseudolife/patches/tau2_pseudolife.patch)
uv pip install --python .venv-taubench/Scripts/python.exe -e evals/taubench_pseudolife
```

The venv is created by `uv` and carries no `pip`, so use `uv pip install`;
`.venv-taubench/Scripts/python -m pip install -e evals/taubench_pseudolife`
works only if you seed pip into it first.

`reference/tau2-bench` was unpacked from a tarball and has no `.git`, so
`patch -p1` is the primary command. `patches/apply_patch.ps1` and
`patches/apply_patch.sh` wrap it; run either from the checkout root.

The patch touches four files and nothing else:

| file | hook |
| --- | --- |
| `src/tau2/registry.py` | registers `pl_memory` behind a lazy importer |
| `src/tau2/runner/build.py` | attaches the memory read tools to the environment before the agent is built |
| `src/tau2/runner/simulation.py` | calls `agent.on_simulation_end(simulation)` after evaluation |
| `src/tau2/runner/batch.py` | stamps the per-simulation telemetry context; fires the `PL_DREAM=episode` trigger |

Every hook is agent-name agnostic or lazily imported, so an unpatched-plugin
venv still runs the stock agents.

## Configuration

All of it is environment variables, read once per simulation in
`create_pl_agent`.

| variable | values | default | meaning |
| --- | --- | --- | --- |
| `PL_MCP_URL` | URL | `http://127.0.0.1:8765` | the daemon |
| `PL_MCP_TOKEN` | token | unset | bearer token, if the daemon requires one |
| `PL_ROUTE` | `entries` \| `lessons` | `entries` | where the reflection writes |
| `PL_SUPERVISION` | `experience` \| `instruction` | `experience` | what the reflection may see on a failure |
| `PL_READ_ONLY` | flag (`1`/`true`/`yes`/`on`) | off | frozen store: reads only — no reflection, no episode rows, no dream |
| `PL_DREAM` | `off` \| `episode` \| `trial` | `off` | consolidation cadence (`episode` is fired by the batch hook; `trial` is the orchestrator's job) |
| `PL_RUN_TAG` | string | empty | tags every stored rule `run:<tag>` |
| `PL_USE_WINDOW_SECONDS` | seconds | `3600` | must match the daemon's use window |
| `PL_REFLECTION_LLM` | model string | the agent's own `llm` | model for the reflection call |
| `PL_REFLECTION_LLM_ARGS` | JSON object | the agent's own `llm_args` | args for that call |
| `PL_TAUBENCH_LOG` | path | `local/data/taubench_memory_events.jsonl` | telemetry sink |
| `PL_TRIAL_OFFSET` | integer | `0` | added to the harness's trial index when the run context is stamped |

`PL_TRIAL_OFFSET` exists for orchestrators that consolidate BETWEEN trials.
That cannot be expressed inside one harness run, so they run the harness once
per trial with a single trial each — and it then reports trial 0 every time.
The offset is the real column; without it every record of such a run is stamped
trial 0 and four trials of telemetry fold into one. Give each of those
invocations its own `PL_TAUBENCH_LOG` as well, or the same collision happens in
the file.

### Routes

`entries` saves each rule as a standalone note (`memory_store`, source
`taubench`, `distortion_tolerance: constraint`) and then sends ONE
`memory_outcome`. `lessons` skips the note entirely and sends only the outcome,
with `about` prefixed `"rule: "` — the prefix that routes the signal into the
daemon's rule-mode synthesis.

Either way there is exactly one outcome per episode, because `used_ids` credit
is per-signal: splitting it would divide the credit for one retrieval among
rules that did not each depend on it.

### Frozen stores

`PL_READ_ONLY` is a BOOLEAN flag, not a store selector — anything that is not
one of its truth words reads as off, and the arm that was meant to leave the
store alone writes into it. Which bank is served comes from `PL_MCP_URL`
pointing at a daemon serving the frozen copy. When it is on the plugin makes no
write of any kind: the reflection is skipped, `episode_start`/`episode_end` are
refused at the client, and the `PL_DREAM=episode` trigger returns without
consolidating.

### Supervision

`experience` hands the reflection only the correct/incorrect bit; on a failure
it may write a prohibition and is explicitly forbidden from inventing a right
answer. `instruction` additionally reveals the verified action sequence and its
difference from what actually happened, and labels the outcome `correction`
rather than `failure`. The verified sequence is read only after grading, never
during the conversation.

## What the agent can and cannot do

It sees exactly two memory tools, both reads: `memory_search` and
`memory_lesson_search`. Write tools are never advertised — but they *are*
intercepted, so a guessed name gets a defer message instead of an unknown-tool
error. Nothing is written mid-conversation: at that point the outcome is
unknown, so a write would be a guess that later reads treat as experience.

Memory calls bypass the domain toolkit and its DB, so they cannot move the
db-hash the reward is computed from.

## Telemetry

One JSONL record per memory call and one per episode, both stamped with the
run context (task, trial, seed, agent, run). The episode record carries the
verdict, the use-window check, and the daemon's full `used_ids_*` accounting —
a signal whose ids all land in `used_ids_unmatched` taught nothing, and only
those keys say so.

## Attribution

`src/pl_taubench/reflection_diff.py` is vendored verbatim from
[memcoai/spark-continual-learning-paper-data](https://github.com/memcoai/spark-continual-learning-paper-data)
(MIT), unmodified except for an attribution header. The module structure of the
plugin follows that harness; all prompt text is our own and deliberately
domain-neutral.
