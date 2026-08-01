# Pseudolife-MCP — project conventions

Conventions that aren't derivable from a quick read of the code. Follow them
exactly; they exist because each one was violated at least once.

## Shipping checklist (any change that lands on master)

1. **CHANGELOG.md entry under `[Unreleased]`** — every behavior, schema, or
   perf change gets one, in the existing dated-subsection style. Docs-only and
   test-only changes are exempt.
2. **Schema bumps** touch five places together: `SCHEMA_META_VERSION` in
   `pseudolife_memory/storage/schema.py`, the doc mentions (README
   capabilities table + the DSN row and version-history table in
   `docs/guide/configuration.md` — pinned by `tests/test_release_ux.py`),
   the version-pin tests (`test_schema_v13.py`, `test_schema_v16.py`,
   `test_temporal_stamp.py`, plus a new `test_schema_vNN.py` for the
   addition itself), a CHANGELOG mention of `vNN` (pinned by
   `test_release_ux.py`), and `docs/atlas/atlas.json` `meta.schema`
   (pinned by `tests/test_atlas_currency.py` — re-verify the affected
   storage cards, don't just renumber).
3. **Full suite before commit** — `HF_HUB_OFFLINE=1 python -m pytest tests/`
   with the bench Postgres up (127.0.0.1:5433); PG-backed tests skip silently
   without it, which is not a pass.
4. **Deploy only via `ops/update.ps1`** (backup → rollback tag → daemon-only
   `--no-deps` rebuild → health). Never `docker compose down -v` — the bank
   volumes are external precisely so that this is survivable, but don't test it.
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

## Review discipline

- Perf/cache/index changes get an independent review pass before commit
  (`/code-review` medium, or a reviewer subagent) — the 2026-07-12 slot-index
  audit found three of these classes post-deploy; the pass is cheaper.
- TDD with a watched RED per the superpowers skill; for invalidation contracts,
  spot-check that each hook is load-bearing by disabling it and confirming the
  test goes red (a hook that never fires red is decoration, and worth saying so).
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
