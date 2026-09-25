# Pseudolife-MCP — project conventions

Conventions that aren't derivable from a quick read of the code. Follow them
exactly; they exist because each one was violated at least once.

## Shipping checklist (any change that lands on master)

1. **CHANGELOG.md entry under `[Unreleased]`** — every behavior, schema, or
   perf change gets one, in the existing dated-subsection style. Docs-only and
   test-only changes are exempt.
2. **Schema bumps** touch seven places together: `SCHEMA_META_VERSION` in
   `pseudolife_memory/storage/schema.py`, the doc mentions (README
   capabilities table + the DSN row and version-history table in
   `docs/guide/configuration.md` — both pinned by `tests/test_release_ux.py`,
   the history table including gap detection), the version pin (the single
   `CURRENT_SCHEMA` literal in `tests/test_schema_version.py` — no
   per-version file, no `>=` relaxation pass; the ladder is gap-checked by
   `test_release_ux.py` and `test_atlas_currency.py`. Whatever behavior the
   bump adds gets a test beside its consumer, or a row in
   `tests/test_schema_ddl_shape.py` if it is pure DDL shape — never a new
   `test_schema_vNN.py`),
   a CHANGELOG mention of `vNN` (pinned by `test_release_ux.py`), and
   `docs/atlas/atlas.json` `meta.schema` (pinned by
   `tests/test_atlas_currency.py` — re-verify the affected storage cards,
   don't just renumber), the two `assert meta[0] == NN` literal pins in
   `tests/test_migrate_embeddings.py`, and `python ops/gen_llms_txt.py`
   after any doc edit (`tests/test_llms_txt.py` pins the generated
   `llms-full.txt`). The v30 bump found the last two the hard way.
3. **Full suite before commit** — `HF_HUB_OFFLINE=1 python -m pytest tests/`
   with the bench Postgres up (127.0.0.1:5433); PG-backed tests skip silently
   without it, which is not a pass. Three exemptions, spelled out under
   "One full suite at a time per machine" below: **docs-only changes** run
   the doc guards instead, **test-only changes** run the touched test
   files, and **merging origin/master into a branch that already passed**,
   with no conflict in code resolved by hand, runs the tests covering the
   overlap; CI gates all three. A queued local suite doesn't hold the PR
   back: open it while you wait, and merge only after the suite passes.
4. **Deploy only via `ops/update.ps1`** (backup → rollback tag → daemon-only
   `--no-deps` rebuild → health). Never `docker compose down -v` — the bank
   volumes are external precisely so that this is survivable, but don't test it.
   **A change under `plugin/` or to the shim needs `-All`** (or
   `ops/update_clients.py` afterwards): the plugin cache and the shim are
   separate installs that a daemon deploy never touches, and the plugin's
   version string cannot move between releases, so `/plugin update` says
   "already latest" — the 2026-09-21 deploy ran an hour on the old hooks.
   Then restart the clients.
5. **After deploy, verify live**, not just `/health`: exercise the changed path
   through the daemon (an MCP call, a psql check of new DDL).

## Derived state / caches / indexes

When adding any derived structure over mutable state (an index over band
entries, a cached view of the graph, a memoized score):

- **Enumerate every mutation path first**, including the ones that bypass the
  normal write API: `hydrate_cms` / `load()` / legacy migration append to
  `band.entries` directly and never call `store()`. Grep for the state being
  mutated, not for the API you expect callers to use.
- **State the real workload's read/write interleave and check the maintenance
  policy preserves the win under it.** The daemon's steady state is
  store/search alternation: an invalidate-on-every-write policy rebuilds on
  every read and silently degrades to the cost you were optimizing away.
  Extend-in-place for additions; rebuild only on removals/replacement.
- **List what the replaced code provided implicitly** — iteration order
  (tie-break determinism), containment semantics (`bands=` filters on the band
  that holds the entry, not `entry.bank`, which goes stale across preset
  changes), live-object reads (supersession flags are read at query time).
  Preserve each one or change it consciously and say so in the commit.

## Running tests (exit-code discipline)

Never pipe a test run through a pager (`pytest ... | tail` / `head` /
`Select-Object`) — the pipe replaces the suite's exit code with the pager's,
which shipped two PRs on failing suites. On the maintainer's machine an
untracked hookify rule (`.claude/hookify.block-masked-test-exit.local.md`)
blocks the pattern outright; the convention applies everywhere regardless.
Use the redirect form from the start:

```bash
python -m pytest tests/ > /tmp/pytest-last.log 2>&1; ec=$?; tail -60 /tmp/pytest-last.log; echo "pytest exit: $ec"
```

or `set -o pipefail; ... | tee /tmp/pytest-last.log | tail -60`.

**One full suite at a time per machine — two on the maintainer's host** —
every session, worktree and harness (Claude Code, Codex, anything else) —
**and CPU-only.** Measured
2026-09-23 on the maintainer's Windows host: a full CPU suite commits ~20 GB
(pytest ~14.9 GB, its spawned test daemons ~4.5 GB) against a ~120 GB commit
limit of which the desktop, WSL and ~75 MCP shims already hold ~100 GB. Two
concurrent suites killed test daemons with os error 1455 ("paging file is too
small") where the same head passed alone, and a GPU run beside two suites
took 143 CUDA OOMs.

- `tests/conftest.py` enforces it. A full run (all of `tests/`, half or more
  of its files, or a `-k`/`-m` that only excludes, like `not slow`) takes
  the lock `~/.pseudolife-mcp/locks/full-suite.lock` and waits its turn,
  naming the holder about once a minute, so start full runs in the
  background. Waiters are served in arrival order: each holds a ticket in
  `full-suite.queue/` beside the lock, and only the earliest live waiter may
  try it (a worktree whose base predates the queue takes no ticket and
  still races for the lock — rebase it). `PSEUDOLIFE_SUITE_LOCK=fail` exits
  instead; `=off` skips the lock (the default on GitHub Actions: one job per
  VM). Targeted runs are never locked. The lock lives in your home
  directory: a WSL or other-user run does not see it.
- The lock has `PSEUDOLIFE_SUITE_SLOTS` slots, default 1 (every other
  machine; CI turns the lock off). The maintainer's Windows host runs 2
  since 2026-09-25, after trimming memory, with paging to NVMe accepted: up
  to two full runs hold the lock at once, waiters still take free slots in
  arrival order, and the waiting notice names every holder. The host's
  count lives in `~/.pseudolife-mcp/locks/full-suite.slots` (the variable
  overrides it for one run); a worktree whose base predates slots only ever
  uses the first.
- The suite sets `CUDA_VISIBLE_DEVICES=-1` itself (`PSEUDOLIFE_TEST_CUDA=1`
  opts back in). Never `""`: on Windows an empty value leaves the GPU usable.
- With several sessions active, still announce `SUITE-START` / `SUITE-END` on
  the coordination board (`memory_agents` / `memory_message`) and keep
  `suite=running|idle` in your status: the lock queues runs, the board lets
  peers plan around the queue.
- **CPU- or memory-saturating work never overlaps a full suite**
  (maintainer rule 2026-09-25). That means load or stress repros (CPU
  burners, memory hogs), benchmark sweeps, parallel stress loops, and
  anything else that pegs the CPU or commits several GB (a model server, a
  large in-memory eval) on the maintainer's host. First check no full suite
  is running: no `~/.pseudolife-mcp/locks/full-suite*.holder.json`, and no
  `suite=running` in `memory_agents` list. A run with
  `PSEUDOLIFE_SUITE_LOCK=off` leaves no holder file, so the board check is
  not optional. Announce the window on the board, bound it with a fixed
  duration and a stop switch (a sentinel file), and stop at once if a suite
  starts. On 2026-09-25 a 16-worker × 25-min burner ran at 100% CPU beside
  two full-suite gate runs. Timing flakes, or os error 1455 when commit
  runs out, invalidate gates and send sessions chasing false regressions.
- **Docs-only changes skip the local full suite** (maintainer decision
  2026-09-25). Docs-only means the diff against `origin/master`
  (`git diff --name-only origin/master...`) touches only documentation:
  `*.md` files anywhere (READMEs and their translations, `docs/**`,
  `CHANGELOG.md`, `CONTRIBUTING.md`, `examples/*.md`, `plugin/README.md`,
  skill `.md` files), `llms.txt` / `llms-full.txt`, and non-code files under
  `docs/` such as `docs/atlas/atlas.json`. Any `.py`, `.ps1` or `.sh`, any
  `.json` outside `docs/`, or any change under `tests/`, `ops/`, plugin
  hooks, workflows or packaging means it is NOT docs-only. Run locally
  instead the doc guards (`tests/test_release_ux.py`,
  `tests/test_llms_txt.py`, `tests/test_atlas_currency.py`,
  `tests/test_eval_evidence.py`, `tests/test_i18n_readme.py`) plus every
  test file that names a touched path (`git grep -l <file basename> tests/`).
  CI's full PostgreSQL job (`test`) must still be green before merge.
- **Merging origin/master forward needs no new local full suite**
  (maintainer decision 2026-09-25) when the branch already passed its local
  full suite and no conflict in code (`.py` / `.ps1` / `.sh`, tests, config)
  was resolved by hand. Run locally the test files covering the files both
  sides touched (plus the doc guards if docs overlapped), push, and let CI
  gate it: its `pull_request` jobs use `actions/checkout`'s default merge
  ref, so the run for the newly pushed head tests the PR merged with master
  as of that push, and it must be green before merge. Before clicking merge,
  if master has moved since the PR's last CI run, press **Update branch**
  (or push a new commit) and wait for that run to go green. Re-running the
  old run doesn't help: it reuses the original merge commit. If any conflict
  in code was resolved by hand, the local full suite is still required.
- **Test-only changes skip the local full suite** (maintainer decision
  2026-09-25): the diff touches only `tests/test_*.py` and test data or
  fixture files (non-code files such as `tests/fixtures/*.json`), with or
  without docs-only files beside them. Run the touched test files locally,
  watched RED as always (plus what the docs-only bullet runs, if docs
  changed too), and let CI's full PostgreSQL job gate it, pressing
  **Update branch** if master moves before merge. NOT test-only:
  `tests/conftest.py`, `tests/pg_fixtures.py`, `tests/suite_lock.py` or any
  other shared helper under `tests/` (a non-`test_` `.py` that test files
  import), or anything outside `tests/` that is not docs-only. A test file
  can still break other test files (module-level state, a fixture that
  leaks a service), which only CI's full job sees, so it must be green.
- **Open the PR while the local full suite is queued** (maintainer decision
  2026-09-25): push and open it so CI and review run in parallel. The body
  says "local full suite: queued" and is updated with the result; merge
  only after the local suite has passed.

## Review discipline

- **Recall precedes review.** Before reviewing code, docs, or a PR, search
  the bank first (`memory_search` + `memory_lesson_search` on the target
  area), then compare what memory says against the files and correct drift
  in both directions — fix stale memory on the spot (`memory_fact_set` +
  `memory_outcome`), and treat memory-vs-file mismatches as review
  findings, not noise. The served session-start block and the
  UserPromptSubmit hook (plugin + `ops/install-hook.*`, Claude client)
  enforce the same rule for any install.
- Every PR gets a review pass before the merge click — `/code-review` medium,
  or a reviewer subagent over the branch diff. The 2026-08-19 transcript audit
  found 1 of 59 merges across seven weeks carried any in-transcript review;
  the pass is not optional because CI was green.
- Perf/cache/index changes get an independent review pass before commit
  (`/code-review` medium, or a reviewer subagent) — the 2026-07-12 slot-index
  audit found three of these classes post-deploy; the pass is cheaper.
- TDD with a watched RED — write the failing test and watch it fail before
  writing the fix; never trust a test you have not seen red. For invalidation
  contracts, spot-check that each hook is load-bearing by disabling it and
  confirming the test goes red (a hook that never fires red is decoration, and
  worth saying so).
- **A tuning constant set or changed for a measured reason carries a comment
  naming the measurement** — what was measured, when, at what scale (the
  `recency_boost_enabled` comment in `utils/config.py` is the shape). Adopt
  as files are touched; no retrofit sweeps — a comment-only pass across the
  tree is merge-conflict noise with no behavior change.
- Eval- or retrieval-affecting changes run `evals/regression_gate.ps1`
  before commit (pinned replicated slice vs committed baseline; exit 1 =
  regression). Extraction/dream-path changes re-run the ladder instead —
  the gate deliberately does not cover them.
- **A comparator defined as "current/production X" is resolved from the
  deployed config** — grep `ops/` (launchers, compose, scheduled tasks) and
  say where you read it — never from a memory record (dated at write time)
  or from whichever variant has the most baseline artifacts. The 2026-07-26
  extractor smoke nearly ran against the v1 prompt because the v1 baseline
  existed and a 07-11 memory said "v1 deployed", while
  `ops/install-shim-autostart.ps1` had defaulted to v2 since 07-21.
- **Never hand-roll the GPU bench server launch — dot-source
  `evals/qwen_server.ps1`** and call `Start-Qwen` (or `Start-Qwen -Fast`).
  It owns the eval env protocol and the config choice, which is not a
  preference: `run-server-turboq.bat`'s fused TBQ4_0 flash-attention KV is
  not bit-reproducible (identical inputs flip ~7% of verdicts, ±0.05 accuracy
  per arm; MTP off and prompt cache off both change nothing —
  `evals/results/judge-determinism-check.json`), while the stock build with
  `--cache-type-k/v q8_0` reproduces exactly. Default is reproducible; `-Fast`
  is only for output that is never judged — it buys 2.4x on long-generation
  work (13.8s vs 33.5s/call) and nothing at all on answer/judge calls
  (0.5s/call either way). Both configs bind :1234, so "something answered the
  probe" is not proof the right one is running; the helper checks and
  replaces. A judged run whose replicates disagree has drifted onto the fast
  server — `replicate.py` warns on exactly that.
- **Every model-vs-model comparison carries a control arm whose input is
  identical across the runs** — the LME `rag` arm is built from raw turns and
  never touches the extractor, so any disagreement there is pure measurement
  noise and bounds what the other arms can claim. Report it next to the
  effect: a delta smaller than the control's spread is not a finding.
  `evals/judge_determinism_check.py` measures the floor directly;
  `evals/analyze_extractor_comparison.py` reports it beside each paired test.

## Pull requests

- **Open the body with 1–3 plain-language sentences: the problem as a user
  or maintainer would notice it, then what the change does about it.** Only
  then the dense technical body — evidence with dates, design decisions,
  what was measured and deliberately not shipped, verification. Keep writing
  that body; it is what makes old PRs reconstructable. The lead exists
  because bodies that open with a plan inventory are unreadable to a human
  six months later.
  - Bad lead (PR #137): "Phase 0+1 of the detector-precision plan, grounded
    in the 2026-08-11 full merge-queue triage…" — names the plan step, not
    the problem.
  - Good lead (PR #136): "`dream_run` had no concurrency guard … two
    triggers racing pulled and extracted the same cursor window twice." —
    problem first, in one breath.
- **Titles say why the change matters, not the mechanism**, within the
  existing `type(scope):` convention where one applies. "fix(dream): stop
  concurrent dreams double-extracting the same window" beats "fix(dream):
  add non-blocking guard to dream_run".

## Publishing a benchmark number

Whether a number is *right* needs a GPU and stays a human gate. Whether it
is *backed* is pure parsing, and `tests/test_eval_evidence.py` enforces it —
add a row there in the same change that adds the claim to the docs.

- **Commit the artifact with the claim.** A number whose evidence lives only
  in a terminal or an untracked working-copy file was never really measured:
  nothing contradicts it, so no guard test and no currency pass will ever
  surface it. Both audits (2026-07-17, 2026-07-21) found this same failure —
  the band-ablation significance table shipped with all five replicates
  untracked and no comparison artifact at all.
- **Every bench writes a file.** `replicate.py compare` and
  `lesson_synthesis_bench.py` both took `--out` retroactively because they
  printed and forgot. New harnesses persist by default.
- **A p-value needs its own artifact** — an aggregate of means cannot
  justify a significance claim.
- **Retire numbers at the old site, not just the new one.** The retired
  0.705 came back in a CHANGELOG entry written *after* its retirement,
  because the retirement note lived in a different file. Mark the superseded
  number where a reader will meet it.
- **Never overwrite a canonical result file on a rerun** — tag the run and
  promote deliberately. A v2 prompt run silently rewrote `sonnet-5.json`
  while also writing its own tagged file (2026-07-21).
- **A bench-instrument migration blocks the release gate until the
  docs-currency pass lands.** The judge and the answerer are terms in every
  published accuracy, so swapping either invalidates the docs the way a
  schema bump invalidates the version tables. The 2026-08-17 Qwen3.6→3.8
  migration shipped with the docs promotion deliberately deferred, and
  v0.14.0 then went out with a README headline (cascade 0.936) that the new
  instrument scores 0.846 — below its own control (#188).
- **Before promoting a claim to the README, run the headline slice under two
  independent judge families.** Determinism is not validity: the 0.936 run
  replicated at std 0.0000 three times and still did not survive a judge
  swap, because reproducibility measures the instrument's steadiness, not
  its agreement with any other instrument. A conclusion that moves when the
  judge moves is a finding about the bench, not about the memory.

## Release / publish procedure (four public surfaces)

Moved to the `release-procedure` project skill
(`.claude/skills/release-procedure/SKILL.md`) — invoke it for ANY release,
version cut, or publish work (GitHub release, PyPI, MCP registry, plugin
marketplace). It carries the docs currency pass and the five-file version
cut; do not release from memory of it.

## Repo hygiene — no PII, ever (public repo)

Anything pushed is public forever: GitHub keeps merged-PR commits reachable
via `refs/pull/*`, which owners cannot purge (Support ticket only) — one
leaked email already cost a full history rewrite plus a fresh-repo publish.

- **Never commit PII or machine identifiers**: emails, OS usernames
  (`C:\Users\<real name>`), hostnames, LAN IPs/subnets, tokens/keys. Docs and
  tests use placeholders (`<user>`, `example.com`) or the synthetic `10.0.0.x`
  examples already in the tree.
- **Extend the guard, don't just scrub**: a removal without a test regresses
  (2026-07-12 lesson). Any newly-spotted identifier class gets added to
  `tests/test_release_ux.py::test_tracked_tree_carries_no_maintainer_identifiers`
  with a watched RED before the scrub.
- **Commit identity stays the GitHub noreply address**
  (`Pseudogiant-xr@users.noreply.github.com`); tee'd script output
  (`deploy-*.log` etc.) stays out of the tree — it embeds absolute home paths.
- **Commit METADATA is a leak channel the guard test can't see**: GitHub
  web-UI edits stamp the account's real email unless Settings → Emails →
  "Keep my email addresses private" is ON (verified on, 2026-07-16 — it was
  off, and one web edit leaked; inspect any unexpected remote commit with
  `git show --format=%ae` before building on it).
- **If a real secret ever lands in a pushed commit: rotate it first.** A
  rewrite is tidiness, not remediation.

## Memory (Pseudolife MCP tools)

Log `memory_outcome` at task end — success/failure/correction signals are the
only feeder for the lessons surfaced at session start. Deploys and eval results
get a `memory_store` with source `pseudolife-mcp` (status chatter →
`source="status"`).

**In-flight work is written, not remembered.** Any session launching
long-running work (an eval run, a training job, a deploy, a sweep) writes a
`source="status"` entry at launch — what was launched, where it runs, when it
should finish — and another when it completes or is abandoned. "What is in
progress" exists only in the bank; the repo cannot answer it. The read side is
symmetric: any question about current status, in-progress work, or what other
sessions are doing is a memory question — `memory_search` (include
`sources=["status"]`) before or alongside git, which only answers what has
landed. This convention is harness-agnostic on purpose: it must hold for any
agent with the MCP tools, not only ones with reminder hooks.

## Glossary

Use these terms — and only these — when describing work back to the
maintainer, in PR bodies, and in commit messages. The point is less that you
understand them (you will infer them) and more that every session describes
the same thing with the same word.

- **continuum / bands** — the associative store's banded layout. Since
  2026-08-15 the default is ONE flat band (`preset: flat`, the measured
  tie from the flat-band verdict); the 8-band `working`→`forever`
  continuum survives as an opt-in preset and its machinery stays in the
  tree. An **entry** is one memory in a band.
- **cortex** — the slot-keyed canonical-fact store: one *current* value per
  `(entity, attribute)` **slot**; supersession, not decay. A **set-valued
  slot** holds many concurrent members (add/remove, not replace).
- **contender** — a competing value parked against a slot instead of
  overwriting it (low-trust dream claims and number-led set adds land here).
- **supersede** — replace a slot's value while keeping the old version as
  audit history. Nothing is ever silently overwritten.
- **freshness_class / stale** — how fast a slot rots: `evergreen` / `slow` /
  `volatile`. `stale: true` (past twice the TTL) means re-verify at the
  source before acting, never "the value is wrong".
- **HLC** — the hybrid logical clock; the monotonic ordering authority for
  supersession. Wall-clock times are display-only.
- **dream** — the consolidation pass: pull the unconsolidated stream →
  extract `(entity, attribute, value)` claims → `fact_set` → advance the
  **cursor** (monotonic; each memory is processed once).
- **session digest** — the mid-density layer: one narrative prose entry
  per closed session episode (`source="digest"`), generated in the idle
  dream cycle, competing in normal dense retrieval. Default-off
  (`digest_enabled`) pending the sidecar quality probe + BEAM verdict.
- **extractor / sidecar** — the model that does dream extraction: the
  bundled CPU sidecar, the Sonnet CLI shim, or any OpenAI-compatible
  endpoint.
- **deep dream** — the separate, manually-triggered full-corpus graph pass:
  safe self-clean plus merge/link candidates routed to review.
- **merge proposal / fold direction / merge veto** — graph entity dedup: a
  proposal folds one entity into another; fold direction says which side
  survives (re-derived from current evidence at review time); a veto is a
  name-shape rule that blocks a bad fold at filing.
- **review queue** — pending graph proposals awaiting accept/reject (the
  Console's Atlas Review view).
- **quarantine** — overloaded; qualify it: *serving-side* quarantine is the
  `stale_policy` that withholds a stale value; *consolidation* quarantine is
  the two-man rule parking low-trust dream claims as contenders.
- **world facts** — cited external facts (source URL + quote); a separate
  cortex from project facts.
- **lessons / outcomes** — `memory_outcome` success/failure/correction
  signals, distilled into the do/avoid guidance surfaced at session start.
- **episode** — the session-scoped attribution handle every write is
  stamped with.
- **bank / daemon / shim** — the durable memory volumes; the HTTP service
  that owns them (one process, one bank); the stdio MCP process bridging a
  client to the daemon.
- **principal / tier** — bearer-token caller identity; the principal keys
  the toolset tier.
- **Console** — the web UI (Cortex Console). The **System Atlas**
  (`docs/atlas/`) is the hand-curated *codebase* architecture map — distinct
  from the Console's Atlas view, which visualizes the memory bank's graph.
